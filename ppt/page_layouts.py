"""版式库：每个版式只负责「产出内容元素」，不碰构建流程。

统一签名（引擎只查表调用，不写 if/else）：
    elements(ctx) -> list[dict]   返回 layout 元素（原生 text / shape / picture / ...）
    draw(slide, ctx) -> list      需要直接画复杂原生形状时用（如创意目录）

ctx 由 make_page.py 统一注入：
    spec, page_no, total, wash, wash_w, focus, art_pos,
    assets(dict), gen_asset(prompt, name)->asset_id, palette, INK, INK_SOFT, VI

所有版式都遵守同一套硬规范：整页摄影图 + 焦点自适应渐变遮罩；
内容落在洗白侧；scrim 由引擎统一放在 mask 层，配图放 overlay 层避免被洗白。
"""
from __future__ import annotations

import copy
import re

PAGE_W = 13.333

# 洗白侧在左时的基准坐标（焦点在左时由引擎整体镜像）
X0 = 0.85
PAL_KEYS = ("primary", "secondary", "soft", "faint")


def _pal(ctx):
    p = ctx.get("palette") or {}
    return {
        "primary": p.get("primary", "#3B2A21"),
        "secondary": p.get("secondary", "#A8763E"),
        "soft": p.get("soft", "#6B5744"),
        "faint": p.get("faint", "#C9B49A"),
    }


def _text(content, x, y, w, h, size, color, *, bold=False,
          align="left", family="微软雅黑", layer="text"):
    return {"type": "text", "content": content,
            "position": {"x": x, "y": y, "w": w, "h": h},
            "font": {"family": family, "size": size, "color": color,
                     "weight": "bold" if bold else "normal"},
            "align": align, "layer": layer}


def _bar(x, y, w, color, h=0.018):
    return {"type": "shape", "shape": "rect", "fill": color, "line": None,
            "position": {"x": x, "y": y, "w": w, "h": h}, "layer": "decoration"}


def _page_no_els(ctx, y=6.86):
    c = _pal(ctx)
    return [_text(f"{ctx['page_no']:02d}  /  {ctx['total']:02d}",
                  X0, y, 2.0, 0.3, 10, c["soft"])]


def _dynamic_page_no(els, ctx):
    """把 spec 里写死的「01 / 08」替换成真实页码，保证换页时不用改文案。"""
    want = f"{ctx['page_no']:02d}  /  {ctx['total']:02d}"
    for e in els:
        if e.get("type") == "text" and re.fullmatch(
                r"\s*\d{1,2}\s*/\s*\d{1,2}\s*", str(e.get("content", ""))):
            e["content"] = want
    return els


# ---------------------------------------------------------------- 封面
def cover_elements(ctx) -> list[dict]:
    spec = ctx["spec"]
    els = _dynamic_page_no(copy.deepcopy(spec.get("elements") or []), ctx)
    if ctx["zone"]["axis"] == "h":
        # 封面文案是固定宽度；先收进基准安全区，再由引擎按需移到另一侧。
        for el in els:
            pos = el["position"]
            if el["type"] in {"text", "shape"}:
                pos["w"] = min(pos["w"], ctx["wash_w"] - pos["x"] - 0.25)
    art = ctx.get("art_pos")
    if art:
        els.append({"type": "picture", "asset_id": "title_art",
                    "position": art, "layer": "text"})
    if not els:
        c = _pal(ctx)
        copy_ = spec.get("copy") or {}
        els = [
            _text(copy_.get("title") or spec.get("name") or "品牌画册",
                  X0, 2.0, 7.0, 1.2, 44, c["primary"], bold=True, family="宋体"),
            _text(copy_.get("subtitle") or "", X0, 3.3, 7.0, 0.5, 18, c["secondary"]),
            _bar(X0, 1.6, 5.9, c["secondary"]),
            _text(copy_.get("body") or "", X0, 4.1, 6.5, 1.0, 13, c["soft"]),
            *_page_no_els(ctx),
        ]
    return els


# ---------------------------------------------------------------- 创意目录
def toc_elements(ctx) -> list[dict]:
    """目录主体由 toc_draw 直接画；这里只放页眉之外的通用底图元素。"""
    return []


def toc_draw(slide, ctx) -> list:
    from .creative_toc import draw_creative_toc
    spec = ctx["spec"]
    toc = dict(spec.get("creative_toc") or spec.get("chapters_spec") or {})
    # 结构化字段 chapters 也能直接当目录条目，避免换内容时还得另写一份
    if not toc.get("items") and spec.get("chapters"):
        toc["items"] = [
            {"num": c.get("num") or f"{i + 1:02d}",
             "label": c.get("label") or c.get("title") or "",
             "desc": c.get("desc") or c.get("body") or ""}
            for i, c in enumerate(spec["chapters"])
        ]
    toc.setdefault("title", "目录")
    toc["page_no"] = ctx["page_no"]
    toc["page_total"] = ctx["total"]
    wash_w = ctx["wash_w"]
    made = draw_creative_toc(slide, toc, wash_w, _pal(ctx))
    if ctx["wash"] == "right":
        from pptx.util import Inches
        # 整块平移，不翻转列序、编号或引线。
        offset = Inches(PAGE_W - wash_w)
        for shp in made:
            shp.left += offset
    return made


# ---------------------------------------------------------------- 分点配图
def points_elements(ctx) -> list[dict]:
    """「分几点来讲」→ 每个点一张独立插图 + 标题 + 说明，配图在洗白侧上层。"""
    spec = ctx["spec"]
    c = _pal(ctx)
    points = spec.get("points") or []
    copy_ = spec.get("copy") or {}
    wash_w = ctx["wash_w"]
    els = [
        _text(copy_.get("title") or spec.get("name") or "要点",
              X0, 0.62, wash_w - 1.6, 0.75, 32, c["primary"], bold=True, family="宋体"),
        _text(copy_.get("subtitle") or "", X0, 1.42, wash_w - 1.6, 0.32, 11, c["soft"]),
        _bar(X0, 1.86, min(6.0, wash_w - 1.6), c["secondary"]),
        *_page_no_els(ctx),
    ]
    n = max(1, len(points))
    top, bottom = 2.12, 6.55
    row_h = (bottom - top) / n
    # 尺寸/间距也来自内容数据（spec.points_style），模块只保留通用默认值；
    # 任何一页想换比例，填数据即可，不必改这里的代码。
    style = spec.get("points_style") or {}
    img_w = float(style.get("img_w", 1.80))
    img_h = float(style.get("img_h", min(0.95, row_h - 0.16)))
    gap = float(style.get("gap", 0.24))
    rule_h = float(style.get("rule_h", 0.008))
    for i, pt in enumerate(points):
        y = top + i * row_h
        label = str(pt.get("label") or pt.get("title") or f"第 {i + 1} 点")
        desc = str(pt.get("desc") or pt.get("body") or "")
        # 每个点一张真实生图（独立素材、独立提示词，互不重复）
        prompt = pt.get("image_prompt") or (
            f"A premium editorial photograph illustrating 「{label}」 for a specialty "
            "coffee brand brochure, warm ivory and deep brown palette, soft natural light, "
            "clean composition with the subject centered, shallow depth of field, "
            "no text, no watermark, no letters, one single photograph."
        )
        aid = ctx["gen_asset"](prompt, f"point_{ctx['page_no']:02d}_{i + 1}")
        els.append({"type": "picture", "asset_id": aid,
                    "position": {"x": X0, "y": y + 0.05, "w": img_w, "h": img_h},
                    "layer": "overlay"})
        tx = X0 + img_w + gap
        tw = max(2.0, wash_w - tx - 0.55)
        els.append(_text(f"{i + 1:02d}   {label}", tx, y + 0.10, tw, 0.34,
                         14, c["primary"], bold=True))
        els.append(_text(desc, tx, y + 0.48, tw, min(0.62, row_h - 0.52),
                         10.5, c["soft"]))
        # 默认靠图片间隔与编号自然分隔，不画线；需要横线的页在 points_style
        # 里写 {"rule": true} 即可（长度/粗细同样可配）。
        if bool(style.get("rule", False)):
            els.append(_bar(tx, y + row_h - 0.06,
                            min(tw, float(style.get("rule_w", 3.5))),
                            c["soft"], h=rule_h))
    return els


# ---------------------------------------------------------------- 历程时间轴
def timeline_elements(ctx) -> list[dict]:
    spec = ctx["spec"]
    c = _pal(ctx)
    copy_ = spec.get("copy") or {}
    wash_w = ctx["wash_w"]
    items = []
    for i, m in enumerate(spec.get("milestones") or spec.get("timeline_items") or []):
        items.append({
            "num": str(m.get("num") or m.get("year") or f"{i + 1:02d}"),
            "label": str(m.get("label") or m.get("title") or "")[:16],
            "desc": str(m.get("desc") or m.get("body") or "")[:80],
        })
    els = [
        _text(copy_.get("title") or spec.get("name") or "发展历程",
              X0, 0.62, wash_w - 1.6, 0.75, 32, c["primary"], bold=True, family="宋体"),
        _text(copy_.get("subtitle") or "", X0, 1.42, wash_w - 1.6, 0.32, 11, c["soft"]),
        _bar(X0, 1.86, min(6.0, wash_w - 1.6), c["secondary"]),
        *_page_no_els(ctx),
    ]
    if items:
        els.append({"type": "branch", "branch_type": "timeline", "items": items,
                    "title": "", "position": {"x": X0, "y": 2.25,
                                              "w": max(4.5, wash_w - 1.5), "h": 4.1},
                    "layer": "overlay"})
    return els


# ---------------------------------------------------------------- 图片拼接
def collage_elements(ctx) -> list[dict]:
    """斜向平行四边形连续拼贴：一张长图切成若干斜块，画面贯通。"""
    spec = ctx["spec"]
    c = _pal(ctx)
    copy_ = spec.get("copy") or {}
    cfg = spec.get("collage") or {}
    wash_w = ctx["wash_w"]
    prompt = cfg.get("image_prompt") or (
        "A wide continuous panoramic editorial photograph for a specialty coffee brochure, "
        "showing a coffee bar and roasting scene from left to right, warm ivory to deep brown "
        "tones, cinematic soft light, no text, no watermark, one single continuous panorama."
    )
    aid = ctx["gen_asset"](prompt, f"collage_{ctx['page_no']:02d}")
    els = [
        _text(copy_.get("title") or spec.get("name") or "空间一瞥",
              X0, 0.55, wash_w - 1.6, 0.7, 30, c["primary"], bold=True, family="宋体"),
        _text(copy_.get("subtitle") or "", X0, 1.30, wash_w - 1.6, 0.3, 11, c["soft"]),
        *_page_no_els(ctx),
        {"type": "mask_picture", "asset_id": aid,
         "position": {"x": 0.7, "y": 1.95, "w": min(11.9, PAGE_W - 1.4), "h": 4.55},
         "mask": {"layout": "columns", "shape": "parallelogram",
                  "continuous": True, "count": cfg.get("count", 4),
                  "gap_in": cfg.get("gap_in", 0.22), "align": "center",
                  "rows": 1, "cols": 4, "ratios": [1.0] * 6},
         "layer": "overlay"},
    ]
    return els


# ---------------------------------------------------------------- 对开/数据
def spread_elements(ctx) -> list[dict]:
    spec = ctx["spec"]
    c = _pal(ctx)
    copy_ = spec.get("copy") or {}
    wash_w = ctx["wash_w"]
    stats = spec.get("spread", {}).get("stats") or spec.get("stats") or []
    els = [
        _text(copy_.get("title") or spec.get("name") or "关键数字",
              X0, 0.62, wash_w - 1.6, 0.75, 32, c["primary"], bold=True, family="宋体"),
        _text(copy_.get("body") or "", X0, 1.55, wash_w - 1.6, 0.9, 13, c["soft"]),
        _bar(X0, 2.5, min(6.0, wash_w - 1.6), c["secondary"]),
        *_page_no_els(ctx),
    ]
    for i, st in enumerate(stats[:4]):
        y = 2.85 + i * 0.9
        els.append(_text(str(st.get("value") or ""), X0, y, 2.1, 0.6,
                         30, c["secondary"], bold=True, family="Georgia"))
        els.append(_text(str(st.get("label") or ""), X0 + 2.3, y + 0.12,
                         wash_w - 3.0, 0.4, 12, c["primary"]))
        els.append(_bar(X0, y + 0.68, min(6.0, wash_w - 1.6), c["faint"], h=0.012))
    return els


# ---------------------------------------------------------------- 致谢封底
def thanks_elements(ctx) -> list[dict]:
    spec = ctx["spec"]
    c = _pal(ctx)
    copy_ = spec.get("copy") or {}
    wash_w = ctx["wash_w"]
    return [
        _text(copy_.get("eyebrow") or "SANYU COFFEE  ·  THANK YOU",
              X0, 1.20, wash_w - 1.6, 0.32, 11, c["soft"], bold=True),
        _bar(X0, 1.72, 5.9, c["secondary"]),
        _text(copy_.get("title") or "明天见。",
              X0, 2.15, wash_w - 1.6, 1.25, 48, c["primary"], bold=True, family="宋体"),
        _text(copy_.get("subtitle") or "SEE YOU TOMORROW",
              X0, 3.48, wash_w - 1.6, 0.4, 14, c["secondary"]),
        _text(copy_.get("body") or
              "谢谢你把这段时间留给自己，也留给我们。\n下一杯，我们仍在城南巷 17 号。",
              X0, 4.15, wash_w - 1.6, 1.0, 13, c["soft"]),
        _bar(X0, 5.55, 5.9, c["secondary"]),
        _text(copy_.get("cta") or "城南巷 17 号  ·  每日 09:00–19:00",
              X0, 5.75, wash_w - 1.6, 0.34, 11.5, c["soft"]),
        *_page_no_els(ctx),
    ]


# ---------------------------------------------------------------- 通用图文
def editorial_elements(ctx) -> list[dict]:
    spec = ctx["spec"]
    c = _pal(ctx)
    copy_ = spec.get("copy") or {}
    wash_w = ctx["wash_w"]
    return [
        _text(copy_.get("title") or spec.get("name") or "",
              X0, 0.85, wash_w - 1.6, 0.85, 34, c["primary"], bold=True, family="宋体"),
        _text(copy_.get("subtitle") or "", X0, 1.78, wash_w - 1.6, 0.34, 12, c["secondary"]),
        _bar(X0, 2.25, min(6.0, wash_w - 1.6), c["secondary"]),
        _text(copy_.get("body") or "", X0, 2.62, wash_w - 1.6, 2.2, 14, c["soft"]),
        _text(copy_.get("cta") or "", X0, 5.55, wash_w - 1.6, 0.4, 12, c["primary"]),
        *_page_no_els(ctx),
    ]
