"""内容信号 → 版式路由：让【同一套引擎】根据每页内容自动选版式。

设计原则（用户明确要求）：
    每一页都用一样的代码；差异全部来自「这一页的内容」，不是页码，也不是
    `if page_no == 2` 这种按页特判。换掉内容，版式就跟着换。

优先级（高 → 低）：
  1. 显式指定 spec["layout"]  / 排除 spec["exclude"]（如用户说「这页不要目录」）
  2. 结构化内容字段：chapters / points / milestones / collage / spread
     —— 结构信号最可靠，不会被文案里的「十年历程」这类词误判
  3. 自然语言信号：扫描 spec["prompt"] + 该页 copy 文案
  4. 位置默认值：第 1 页 → cover，最后一页 → thanks（内容信号可覆盖）
  5. 兜底 → editorial

返回 {"layout", "reason", "evidence", "overridden"}，写进 result.json 便于追溯。
"""
from __future__ import annotations

import re

from ppt.toc_policy import toc_intent

FIRST_LAYOUT = "cover"      # 首页默认：封面
LAST_LAYOUT = "thanks"      # 末页默认：致谢
FALLBACK_LAYOUT = "editorial"

# 用户/提示词里可能出现的版式别名（显式指定用）
ALIASES = {
    "封面": "cover", "cover": "cover", "扉页": "cover",
    "目录": "creative_toc", "toc": "creative_toc", "创意目录": "creative_toc",
    "章节页": "creative_toc", "章节导览": "creative_toc",
    "分点配图": "points_with_images", "每点配图": "points_with_images",
    "分支配图": "points_with_images", "points": "points_with_images",
    "历程": "timeline", "时间轴": "timeline", "发展史": "timeline",
    "timeline": "timeline", "roadmap": "timeline",
    "拼接": "collage", "多图拼接": "collage", "拼贴": "collage",
    "collage": "collage", "斜切拼贴": "collage",
    "对开": "spread", "跨页": "spread", "spread": "spread",
    "致谢": "thanks", "感谢页": "thanks", "thanks": "thanks", "封底": "thanks",
    "图文": "editorial", "普通页": "editorial", "editorial": "editorial",
}

# 结构化字段 → 版式（最可靠）
STRUCT_KEYS = (
    ("creative_toc", "creative_toc", "含创意目录内容块"),
    ("chapters", "creative_toc", "含编号章节列表"),
    ("toc_items", "creative_toc", "含目录条目"),
    ("points", "points_with_images", "含并列分点（每点配图）"),
    ("milestones", "timeline", "含年份里程碑"),
    ("timeline_items", "timeline", "含时间轴节点"),
    ("collage", "collage", "含图片拼接配置"),
    ("spread", "spread", "含对开跨页配置"),
)

# 自然语言信号：(正则, 版式, 权重, 说明)。权重高者优先。
SIGNAL_RULES: list[tuple[str, str, int, str]] = [
    (r"致谢|感谢观看|谢谢观看|THANKS|Thanks|明天见|下次见|再会|敬请期待",
     "thanks", 96, "出现致谢/告别信号"),
    (r"分几点|几点来讲|每一点|每个点|每点都?配图|每个分支都?配图|"
     r"四款|三大|四大|五大|几大优势|若干点|并列几点|每项都配",
     "points_with_images", 92, "出现并列分点且要配图"),
    (r"发展历程|成长历程|时间轴|里程碑|roadmap|Roadmap|阶段划分|"
     r"20\d\d\s*[—–-]\s*20\d\d|自\s*20\d\d\s*年",
     "timeline", 86, "出现年份/历程信号"),
    (r"图片拼接|多图拼接|连续裁切|全景拼贴|斜切拼贴|拼图|"
     r"collage|continuous crop", "collage", 82, "出现图片拼接信号"),
    (r"对开|跨页连续|数据条|关键指标|经营数据", "spread", 72, "出现对开/数据信号"),
    (r"纯文字|零图片|极简|宣言|主张", "editorial", 60, "出现极简图文信号"),
]


def _collect_text(spec: dict) -> str:
    """汇总这一页所有自然语言内容，供信号扫描。"""
    parts: list[str] = []
    for key in ("name", "prompt", "creative", "style", "brief", "title", "subtitle"):
        v = spec.get(key)
        if v:
            parts.append(str(v))
    copy = spec.get("copy")
    if isinstance(copy, dict):
        parts.extend(str(v) for v in copy.values() if v)
    elif copy:
        parts.append(str(copy))
    for e in spec.get("elements") or []:
        if isinstance(e, dict) and e.get("content"):
            parts.append(str(e["content"]))
    # 目录/分点条目只取标题，避免章节说明里的「十年历程」等词干扰
    for key in ("chapters", "items", "points", "milestones"):
        for it in spec.get(key) or []:
            if isinstance(it, dict):
                parts.append(str(it.get("label") or it.get("title") or ""))
    return "\n".join(parts)


def route_layout(spec: dict, page_no: int, total_pages: int,
                 *, trace: bool = False) -> dict:
    """由内容决定版式。trace=True 时附带所有命中信号，便于调试。"""
    text = _collect_text(spec)
    intent = toc_intent(spec)
    hits: list[tuple[int, str, str]] = []
    if intent is True:
        hits.append((100, 'creative_toc', '本页明确要求目录/章节导览'))
    for pattern, layout, weight, why in SIGNAL_RULES:
        m = re.search(pattern, text)
        if m:
            hits.append((weight, layout, f"{why}（命中「{m.group(0)}」）"))
    hits.sort(key=lambda h: -h[0])
    evidence = [h[2] for h in hits]

    # ---- 1. 显式排除 / 指定 ----
    raw_excludes = spec.get('exclude') or spec.get('no') or []
    if isinstance(raw_excludes, str):
        raw_excludes = [raw_excludes]
    excludes = {ALIASES.get(str(x).strip().lower(), str(x).strip().lower()) for x in raw_excludes}
    if intent is False:
        excludes.add('creative_toc')
    explicit = spec.get("layout")
    if explicit:
        layout = ALIASES.get(str(explicit).strip().lower(), str(explicit).strip().lower())
        if layout not in excludes:
            return {"layout": layout, "reason": f"用户在提示词里指定「{explicit}」",
                    "evidence": evidence, "overridden": True, "excluded": sorted(excludes)}

    chosen: str | None = None
    reason = ""

    # ---- 2. 结构化字段 ----
    for key, layout, why in STRUCT_KEYS:
        val = spec.get(key)
        if val and layout not in excludes:
            count = len(val.get("items") or val) if isinstance(val, dict) else len(val)
            chosen, reason = layout, f"{why}（{count} 项）"
            break

    # ---- 3. 自然语言信号 ----
    if not chosen:
        for weight, layout, why in hits:
            if layout in excludes:
                continue
            chosen, reason = layout, why
            break

    # ---- 4. 位置默认值（内容信号优先，可被覆盖）----
    # 少页(<4)属轻量 PPT:不存在可靠的「首页/末页」语义,禁用位置默认,
    # 一律按内容信号选,无信号用通用图文版式。避免单页同时命中首页封面与末页致谢。
    # 多页(>=4)才启用画册三段式位置默认,且已被上方内容信号优先覆盖。
    if not chosen:
        if total_pages < 4:
            chosen, reason = FALLBACK_LAYOUT, "少页：内容信号优先，无信号用通用图文版式（不用位置默认）"
        elif page_no == 1 and FIRST_LAYOUT not in excludes:
            chosen, reason = FIRST_LAYOUT, "位置默认：第 1 页是封面"
        elif page_no == total_pages and LAST_LAYOUT not in excludes:
            chosen, reason = LAST_LAYOUT, f"位置默认：最后一页（{total_pages}）是致谢"
        else:
            chosen, reason = FALLBACK_LAYOUT, "无明确信号，用通用图文版式"

    # ---- 5. 排除命中但已被显式结构字段选中的情况 ----
    if chosen in excludes:
        chosen, reason = FALLBACK_LAYOUT, f"「{chosen}」被排除，回退通用图文版式"

    out = {"layout": chosen, "reason": reason, "evidence": evidence,
           "overridden": False, "excluded": sorted(excludes),
           "page_no": page_no, "total_pages": total_pages}
    if trace:
        out["all_hits"] = evidence
        out["text_preview"] = text[:300]
    return out


# 版式注册表：引擎只查表，不写 if/else。每个版式提供 elements(ctx) 与 draw(slide, ctx)。
def build_registry() -> dict:
    from . import page_layouts as L
    noop = lambda slide, ctx: []  # noqa: E731  统一签名，无分支
    return {
        "cover": {
            "label": "封面（可编辑主标题 + 品牌宣言 + 落款）",
            "elements": L.cover_elements, "draw": noop, "needs_art": False,
        },
        "creative_toc": {
            "label": "创意目录（超大斜体数字 + 虚线 + 错落网格）",
            "elements": L.toc_elements, "draw": L.toc_draw, "needs_art": False,
        },
        "points_with_images": {
            "label": "分点配图（每个点一张独立插图 + 标题 + 说明）",
            "elements": L.points_elements, "draw": noop, "needs_art": False,
        },
        "timeline": {
            "label": "历程时间轴（每节点年份 + 做了什么 + 结果）",
            "elements": L.timeline_elements, "draw": noop, "needs_art": False,
        },
        "collage": {
            "label": "图片拼接（斜向平行四边形连续裁切）",
            "elements": L.collage_elements, "draw": noop, "needs_art": False,
        },
        "spread": {
            "label": "对开跨页（整页图 + 数据条 / 压图横带）",
            "elements": L.spread_elements, "draw": noop, "needs_art": False,
        },
        "thanks": {
            "label": "致谢封底（大字感谢 + 落款 + 细线）",
            "elements": L.thanks_elements, "draw": noop, "needs_art": False,
        },
        "editorial": {
            "label": "通用图文（标题 + 正文 + 落款）",
            "elements": L.editorial_elements, "draw": noop, "needs_art": False,
        },
    }
