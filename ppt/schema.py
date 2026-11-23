"""版式 JSON 的规范化、校验与素材需求抽取。

设计原则：这一层只定义**通用版式原语**，不预设任何具体版式。
「5 根柱子拼一张连续全景」只是 mask_picture 的一种配置
(layout=columns, continuous=true, count=5)，绝不是默认版式。
LLM 输出不稳定是常态：这里负责把「差不多对」的 JSON 变成 Builder 能吃的标准结构，
并把越界、缺字段、非法颜色挡在生成 PPTX 之前。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

import config
from ppt.item_images import background_framing, item_targets
from services.font_guard import resolve_font
from services.opening_focus import OPENING_BG_PREFIX, OPENING_LIMIT, POSITIONS, composition

ALLOWED_TYPES = {"text", "shape", "mask_picture", "picture", "decorate", "branch", "scrim", "toc"}
SCRIM_WASHES = {"left", "right", "top", "bottom", "bottomleft", "bottomright", "topleft", "topright"}
BRANCH_TYPES = {"arrow", "timeline", "icon_grid", "metric_grid", "radial"}
ALLOWED_SHAPES = {
    "rect", "round_rect", "oval", "line", "triangle", "chevron", "hexagon",
    "diamond", "parallelogram", "trapezoid",
}
ALLOWED_PRIMITIVES = {"ring", "dashed_line", "line", "diagonal_bar", "dot_grid"}
MASK_LAYOUTS = {"single", "columns", "rows", "grid"}
MASK_ALIGNS = {"start", "center", "end"}
_ALIGN_ALIAS = {"top": "start", "left": "start", "bottom": "end", "right": "end"}
LAYER_ORDER = {"bg": 0, "picture": 1, "mask": 2, "decoration": 3, "overlay": 4, "text": 5}
DEFAULT_LAYER = {
    "text": "text",
    "shape": "decoration",
    "mask_picture": "mask",
    "picture": "picture",
    "decorate": "decoration",
    "branch": "decoration",
    "scrim": "mask",        # 渐变遮罩盖在背景图上、文字之下
    "toc": "text",          # 目录是原生文字条目
}
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _color(value: Any, fallback: str) -> str:
    if isinstance(value, str) and HEX_RE.match(value):
        return value.upper()
    return fallback.upper()


def _num(value: Any, default: float, low: float = 0.0) -> float:
    try:
        return max(low, float(value))
    except (TypeError, ValueError):
        return default


def _position(raw: Any) -> dict[str, float]:
    p = raw if isinstance(raw, dict) else {}
    x = _num(p.get("x"), 0.5)
    y = _num(p.get("y"), 0.5)
    w = _num(p.get("w"), 1.0, 0.05)
    h = _num(p.get("h"), 0.5, 0.05)
    x = min(x, config.SLIDE_W_IN - 0.05)
    y = min(y, config.SLIDE_H_IN - 0.05)
    w = min(w, config.SLIDE_W_IN - x)
    h = min(h, config.SLIDE_H_IN - y)
    return {"x": round(x, 3), "y": round(y, 3), "w": round(w, 3), "h": round(h, 3)}


def _auto_fit_text(el: dict, right_limit: float) -> None:
    """确定性自动收口：避免中文段落末行只剩 1~2 个字（孤行）。

    用加权宽度估算（CJK 1em，ASCII/标点 0.5em），小幅加宽文本框；
    最多加 0.8in、不越过画布、也不侵入右侧的图片区域，仍不行就降 1pt 字号。
    调整会记录在元素的 auto_fit 字段里，报告可追溯，不偷偷改设计。
    """
    if el["type"] != "text" or not el["content"]:
        return
    p = el["position"]
    fs = el["font"]["size"]
    chars = [c for c in el["content"] if c != "\n"]
    total_em = sum(1.0 if ord(c) > 0x2E80 else 0.5 for c in chars)

    def last_line_em(width: float, size: float) -> tuple[int, float]:
        per = max(1.0, width * 72 / size)
        lines = max(1, math.ceil(total_em / per))
        return lines, total_em - (lines - 1) * per

    lines, tail = last_line_em(p["w"], fs)
    if lines < 2 or tail >= 3.0:
        return
    before_w, before_size = p["w"], fs
    max_w = min(
        config.SLIDE_W_IN - p["x"] - 0.2,
        right_limit - p["x"] - 0.15,
        before_w + 0.8,
    )
    if max_w <= before_w:
        return  # 加宽空间不足，交给人工收口，不硬改
    width = before_w
    for _ in range(12):
        width = min(max_w, width + 0.08)
        lines2, tail2 = last_line_em(width, fs)
        if tail2 >= 3.0 or width >= max_w:
            break
    p["w"] = round(width, 3)
    lines, tail = last_line_em(p["w"], fs)
    if tail < 3.0 and fs > 9:
        el["font"]["size"] = fs - 1
        lines, tail = last_line_em(p["w"], el["font"]["size"])
    el["auto_fit"] = {
        "reason": "避免段落末行孤字",
        "width_in": {"before": before_w, "after": p["w"]},
        "font_size": {"before": before_size, "after": el["font"]["size"]},
        "estimated_last_line_em": round(tail, 2),
    }


# 带斜边的预设形状：文字压在其上时，首尾字可能落到斜边外的异色底上而隐形。
# 这些形状的 DrawingML 默认 adj 见 python-pptx 的 MSO_SHAPE 调整值（平行四边形/梯形 0.25 等）。
_SKEW_SHAPES = {"parallelogram", "trapezoid", "chevron", "diamond", "hexagon"}


def _skew_safe_horizontal(shape: dict, ay: float, by: float) -> tuple[float, float] | None:
    """形状在垂直带 [ay, by] 内的水平安全范围 (left, right)。

    按各预设形状的边界随高度线性收缩计算，取该垂直带内最保守的左右边界；
    这样文字框整段都落在实色区内，首字/尾字不会骑在斜边上。
    """
    sp = shape["position"]
    sx, sy, sw, sh = sp["x"], sp["y"], sp["w"], sp["h"]
    lo, hi = max(ay, sy), min(by, sy + sh)
    if hi - lo < 0.08:
        return None
    t0, t1 = (lo - sy) / sh, (hi - sy) / sh  # 0=顶 1=底
    kind = shape.get("shape", "")
    if kind in {"parallelogram", "hexagon"}:
        dx = 0.25 * min(sw, sh)                 # 左边界顶内缩、底归零
        return sx + dx * (1 - t0), sx + sw - dx * t1
    if kind == "trapezoid":                      # 默认上窄下宽
        dx = 0.25 * sw
        return sx + dx * (1 - t0), sx + sw - dx * (1 - t0)
    if kind == "chevron":                        # 人字：右侧 V 口，两端都内缩
        dx = 0.5 * sw
        return sx + dx * (1 - t0), sx + sw - dx * (1 - t0)
    if kind == "diamond":                        # 菱形：保守取中心带
        return sx + sw * 0.25, sx + sw * 0.75
    return None


def _constrain_text_to_skew_shape(text_el: dict, elements: list[dict]) -> None:
    """若文字压在更底层的斜切形状上，把文字框收进该形状的内接安全区。

    只改 x/w、不换字色：整段字落在同一实色区内，统一原字色即可读；
    记录到 auto_fit.shape_inset，报告可追溯，返修改坐标后重新 normalize 自动生效。
    """
    if text_el["type"] != "text" or not text_el["content"]:
        return
    tp = text_el["position"]
    cx = tp["x"] + tp["w"] / 2
    cy = tp["y"] + tp["h"] / 2
    for host in elements:
        if host is text_el or host.get("z", 0) >= text_el.get("z", 0):
            continue
        if host["type"] != "shape":
            continue
        if host.get("shape", "") not in _SKEW_SHAPES:
            continue
        hp = host["position"]
        if not (hp["x"] <= cx <= hp["x"] + hp["w"] and hp["y"] <= cy <= hp["y"] + hp["h"]):
            continue
        bounds = _skew_safe_horizontal(host, tp["y"], tp["y"] + tp["h"])
        if not bounds:
            continue
        left, right = bounds
        safe_w = right - left
        if safe_w < 1.0:                 # 安全区过窄，收口反而放不下，交给视觉评审报警
            continue
        before = (tp["x"], tp["w"])
        nx = max(tp["x"], left)
        nr = min(tp["x"] + tp["w"], right)
        if nx >= nr:
            nx, nr = (left + right) / 2 - safe_w / 2, (left + right) / 2 + safe_w / 2
        tp["x"], tp["w"] = round(nx, 3), round(nr - nx, 3)
        text_el.setdefault("auto_fit", {})["shape_inset"] = {
            "host": host["element_id"],
            "host_shape": host["shape"],
            "x": {"before": before[0], "after": tp["x"]},
            "w": {"before": before[1], "after": tp["w"]},
            "safe_right": round(right, 3),  # 后续孤字加宽不得越过此界
        }
        return  # 一段文字只收进它所在的第一个斜切形状


def _normalize_mask(raw: dict, aid: str) -> dict:
    """把 mask 规格规范化为「一组形状单元格」的通用描述。

    layout: single(单形状裁图) / columns(横排) / rows(竖排) / grid(网格)
    continuous: true=所有格从同一张图连续裁切；false=每格用独立素材
    align: 交叉轴对齐 start/center/end（columns 时 end=底部对齐，rows 时 end=右对齐）
    """
    layout = str(raw.get("layout") or "").strip().lower()
    raw_count = int(_num(raw.get("count"), 0, 0))
    if layout not in MASK_LAYOUTS:
        # 兼容旧版式 JSON：没写 layout 时，count>1 视为横排柱子，count<=1 视为单形状
        layout = "columns" if raw_count > 1 else "single"

    rows = min(8, max(1, int(_num(raw.get("rows"), 2, 1))))
    cols = min(8, max(1, int(_num(raw.get("cols"), 2, 1))))
    if layout == "grid":
        count = rows * cols
    elif layout == "single":
        count, rows, cols = 1, 1, 1
    else:
        count = min(12, max(1, raw_count or 1))
        rows, cols = (count, 1) if layout == "rows" else (1, count)

    ratios = raw.get("ratios")
    if not isinstance(ratios, list) or len(ratios) != count:
        ratios = (
            [round(0.6 + 0.4 * (i / max(1, count - 1)), 2) for i in range(count)]
            if count > 1 else [1.0]
        )
    ratios = [round(min(1.0, max(0.2, _num(r, 0.8))), 3) for r in ratios]

    align = _ALIGN_ALIAS.get(
        str(raw.get("align") or "").strip().lower(),
        str(raw.get("align") or "").strip().lower(),
    )
    if align not in MASK_ALIGNS:
        align = "end"  # 兼容旧版横排柱子的底部对齐约定

    continuous = raw.get("continuous")
    if not isinstance(continuous, bool):
        continuous = layout != "grid"  # 网格默认是多张独立素材，其余默认连续全景
    shape = str(raw.get("shape") or "rect").lower()
    if shape not in ALLOWED_SHAPES:
        shape = "rect"

    asset_ids = raw.get("asset_ids")
    if not isinstance(asset_ids, list) or len(asset_ids) != count:
        asset_ids = [f"{aid}_c{i}" for i in range(count)] if not continuous else []
    cell_prompts = raw.get("cell_prompts") if isinstance(raw.get("cell_prompts"), list) else []

    return {
        "shape": shape,
        "layout": layout,
        "count": count,
        "rows": rows,
        "cols": cols,
        "ratios": ratios,
        "gap_in": round(_num(raw.get("gap_in"), 0.1), 3),
        "radius_in": round(_num(raw.get("radius_in"), 0.0), 3),
        "align": align,
        "continuous": continuous,
        "asset_ids": [str(a) for a in asset_ids],
        "cell_prompts": [str(c) for c in cell_prompts],
    }


def _normalize_element(raw: dict, idx: int, page_idx: int, vi: dict) -> dict:
    etype = str(raw.get("type", "")).strip().lower()
    if etype not in ALLOWED_TYPES:
        # 容错：带内容就当文本，否则当装饰形状
        etype = "text" if raw.get("content") else "decorate"
    secondary = vi["secondary"]
    accent = vi["accent"]
    el: dict[str, Any] = {
        "element_id": f"p{page_idx + 1}_e{idx + 1}_{etype}",
        "type": etype,
        "content": str(raw.get("content") or raw.get("text") or ""),
        "position": _position(raw.get("position")),
        "layer": str(raw.get("layer") or DEFAULT_LAYER[etype]).lower(),
        "note": str(raw.get("note") or ""),
    }
    if etype in {'picture', 'mask_picture', 'text'} and raw.get('item_key'):
        el['item_key'] = str(raw['item_key']).strip()
    if el["layer"] not in LAYER_ORDER:
        el["layer"] = DEFAULT_LAYER[etype]
    el["z"] = LAYER_ORDER[el["layer"]]

    font = raw.get("font") if isinstance(raw.get("font"), dict) else {}
    family, substitution = resolve_font(str(font.get("family") or "微软雅黑"))
    el["font"] = {
        "family": family,
        "weight": "bold" if str(font.get("weight", "")).lower() in {"bold", "700", "heavy"} else "normal",
        "color": _color(font.get("color"), "#FFFFFF"),
        "size": max(8, min(96, _num(font.get("size"), 18))),
    }
    if substitution:
        el["font"]["substitution"] = substitution
    el["align"] = str(raw.get("align") or "left").lower()
    if el["align"] not in {"left", "center", "right"}:
        el["align"] = "left"

    if etype == "shape":
        shape = str(raw.get("shape") or "rect").lower()
        el["shape"] = shape if shape in ALLOWED_SHAPES else "rect"
        el["fill"] = _color(raw.get("fill"), secondary) if raw.get("fill") else None
        el["fill_opacity"] = min(1.0, _num(raw.get("fill_opacity"), 1.0))
        el["line"] = _color(raw.get("line"), accent) if raw.get("line") else None
        if raw.get("role") == "photo_metric_card":
            el["role"] = "photo_metric_card"

    elif etype == "mask_picture":
        el["asset_id"] = str(raw.get("asset_id") or f"hero_p{page_idx + 1}_{idx + 1}")
        el["mask"] = _normalize_mask(raw.get("mask") or {}, el["asset_id"])

    elif etype == "picture":
        el["asset_id"] = str(raw.get("asset_id") or f"pic_p{page_idx + 1}_{idx + 1}")
        if raw.get('asset_kind') in ('emblem', 'icon', 'decoration'):
            el['asset_kind'] = raw['asset_kind']

    elif etype == "decorate":
        prim = str(raw.get("primitive") or raw.get("shape") or "ring").lower()
        el["primitive"] = prim if prim in ALLOWED_PRIMITIVES else "ring"
        el["color"] = _color(raw.get("color"), secondary)

    elif etype == "branch":
        btype = str(raw.get("branch_type") or raw.get("branch") or "icon_grid").lower()
        el["branch_type"] = btype if btype in BRANCH_TYPES else "icon_grid"
        el["title"] = str(raw.get("title") or "")
        items = []
        raw_items = raw.get("items") if isinstance(raw.get("items"), list) else []
        for j, it in enumerate(raw_items):
            if not isinstance(it, dict):
                continue
            items.append({
                "num": str(it.get("num") or f"{j + 1:02d}"),
                "label": str(it.get("label") or it.get("title") or "")[:16],
                "desc": str(it.get("desc") or it.get("body") or "")[:80],
            })
        if not items:
            # 兜底：用 content 拆出 3 个空项，保证不空渲染
            items = [{"num": f"{j+1:02d}", "label": "", "desc": ""} for j in range(3)]
        el["items"] = items[:8]

    elif etype == "scrim":
        wash = str(raw.get("wash") or "left").lower()
        el["wash"] = wash if wash in SCRIM_WASHES else "left"
        el["color"] = _color(raw.get("color"), "#FFFFFF")
        try:
            el["strength"] = max(0.0, min(1.0, float(raw.get("strength", 0.96))))
        except (TypeError, ValueError):
            el["strength"] = 0.96

    elif etype == "toc":
        el["title"] = str(raw.get("title") or "目录")
        items = []
        raw_items = raw.get("items") if isinstance(raw.get("items"), list) else []
        for j, it in enumerate(raw_items):
            if not isinstance(it, dict):
                continue
            items.append({
                "num": str(it.get("num") or f"{j + 1:02d}"),
                "label": str(it.get("label") or it.get("title") or "")[:20],
                "desc": str(it.get("desc") or "")[:60],
            })
        el["items"] = items[:12]
        # 目录列数：1/2/3，默认 2；与 scrim 同侧时建议 1 列
        try:
            cols = int(raw.get("cols", 2))
        except (TypeError, ValueError):
            cols = 2
        el["cols"] = cols if cols in {1, 2, 3} else 2
        wash = str(raw.get("wash") or "").lower()
        el["wash"] = wash if wash in SCRIM_WASHES else ""

    return el


def style_photo_metric_cards(bg: dict, elements: list[dict]) -> None:
    """Style only designated photo cards and the editable text contained in them."""
    if bg.get("type") != "image":
        return
    for card in elements:
        if card.get("type") != "shape" or card.get("shape") not in {"rect", "round_rect"}:
            continue
        # Older layouts described the data strip in the planner note instead of a role.
        note = card.get("note", "")
        if card.get("role") != "photo_metric_card" and not (
            "半透明" in note and any(term in note for term in ("数据条", "数据卡", "指标卡"))
        ):
            continue
        box = card["position"]
        card.update(fill="#F7EEDC", fill_opacity=0.58)
        for text in elements:
            if text.get("type") != "text" or text["z"] <= card["z"]:
                continue
            p = text["position"]
            # Allow for rounding to thousandths of an inch in normalized coordinates.
            if all(box[a] - .002 <= p[a] and p[a] + p[d] <= box[a] + box[d] + .002
                   for a, d in (("x", "w"), ("y", "h"))):
                text["font"]["color"] = "#20160F"


def _lift_card_graphics(elements: list[dict]) -> None:
    """仅修复明确的卡底与前景图反序，不跨过其他相交的遮挡物。"""
    cards = [e for e in elements if e['type'] == 'shape' and e['shape'] == 'rect'
             and e['layer'] == 'decoration' and e['fill'] and e['fill_opacity'] == 1
             and re.search(r'底板|卡片背景|卡底|\b(?:card background|backplate)\b', e['note'], re.I)]
    if not cards:
        return
    for pic in elements:
        if pic['type'] != 'picture' or pic['layer'] != 'picture':
            continue
        desc = ' '.join((pic['asset_id'].replace('_', ' '), pic['content'], pic['note']))
        if (not re.search(r'图标|图表|环形图|饼图|柱状图|折线图|\b(?:icon|chart|donut)\b', desc, re.I)
                or re.search(r'摄影|照片|背景图|底图|\b(?:photo\w*|background|bg)\b', desc, re.I)):
            continue
        p = pic['position']
        axes = (('x', 'w'), ('y', 'h'))
        # 坐标已规范化至千分之一英寸；只消除浮点加法误差。
        hosts = [c for c in cards if all(c['position'][a] <= p[a] and
                 round(p[a] + p[d], 3) <= round(c['position'][a] + c['position'][d], 3)
                 for a, d in axes)]
        if not hosts:
            continue
        # 保留遮罩、局部装饰等原叠放关系；不抢到 overlay 或文字上方。
        if any(pic['z'] < e['z'] <= LAYER_ORDER['decoration'] and e not in hosts and
               all(e['position'][a] < round(p[a] + p[d], 3) and
                   p[a] < round(e['position'][a] + e['position'][d], 3) for a, d in axes)
               for e in elements):
            continue
        pic['z'] = (LAYER_ORDER['decoration'] + LAYER_ORDER['overlay']) / 2


def normalize_layout(raw: Any, brief: dict | None = None) -> dict:
    brief = brief or {}
    raw = raw if isinstance(raw, dict) else {}
    raw_vi = raw.get("vi") if isinstance(raw.get("vi"), dict) else (brief.get("vi") or {})
    vi = {
        "primary": _color(raw_vi.get("primary"), "#0B2A4A"),
        "secondary": _color(raw_vi.get("secondary"), "#C9A86A"),
        "accent": _color(raw_vi.get("accent"), "#E8EEF5"),
    }

    if isinstance(raw.get("pages"), list) and raw["pages"]:
        raw_pages = raw["pages"]
    elif isinstance(raw, list):
        raw_pages = raw
    elif raw.get("elements") or raw.get("page_name"):
        raw_pages = [raw]
    else:
        raise ValueError("版式 JSON 缺少 pages / elements 字段，无法构建")

    pages: list[dict] = []
    for pi, rp in enumerate(raw_pages):
        rp = rp if isinstance(rp, dict) else {}
        bg_raw = rp.get("bg") if isinstance(rp.get("bg"), dict) else {}
        bg_type = "image" if bg_raw.get("type") == "image" and bg_raw.get("image") else "color"
        bg = {
            "type": bg_type,
            "color": _color(bg_raw.get("color"), vi["primary"]),
            "image": str(bg_raw.get("image")) if bg_type == "image" else None,
        }
        if bg_type == "image":
            bg["measure_focus"] = len(raw_pages) > 1 or bool(bg_raw.get("measure_focus"))
            for field in ("asset_source", "asset_sha256"):
                if bg_raw.get(field):
                    bg[field] = bg_raw[field]
            if bg_raw.get("subject") or bg_raw.get("content"):
                bg["subject"] = str(bg_raw.get("subject") or bg_raw["content"])
        pos = bg_raw.get("subject_position")
        if (bg_type == "image" and 1 < len(raw_pages) and pi < OPENING_LIMIT
                and isinstance(pos, str) and pos in POSITIONS):
            bg["subject_position"] = pos
        elements: list[dict] = []
        raw_elements = rp.get("elements") if isinstance(rp.get("elements"), list) else []
        for el in raw_elements:
            if isinstance(el, dict):
                elements.append(_normalize_element(el, len(elements), pi, vi))
        # 兼容原提示词里的独立 decorations 数组
        for deco in rp.get("decorations") or []:
            if isinstance(deco, dict):
                d = dict(deco)
                d.setdefault("type", "decorate")
                elements.append(_normalize_element(d, len(elements), pi, vi))
        if not elements and bg["type"] != "image":
            # 整页背景图本身就是内容（如艺术字封面），允许零元素；纯色空页无意义。
            raise ValueError(f"第 {pi + 1} 页没有任何元素")

        style_photo_metric_cards(bg, elements)

        # 斜切块文字收口：压在平行四边形/梯形/菱形等形状上的文字，先收进内接安全区
        for el in elements:
            if el["type"] == "text":
                _constrain_text_to_skew_shape(el, elements)

        # 自动收口：只在不侵入同页「位于文字右侧」的图片区域时，小幅加宽正文框
        image_lefts = [
            e["position"]["x"] for e in elements if e["type"] in {"mask_picture", "picture"}
        ]
        for el in elements:
            if el["type"] != "text":
                continue
            tx = el["position"]["x"]
            to_right = [x for x in image_lefts if x >= tx + 0.2]
            right_limit = min(to_right) if to_right else config.SLIDE_W_IN - 0.2
            inset = el.get("auto_fit", {}).get("shape_inset")
            if inset:
                right_limit = min(right_limit, inset["safe_right"])
            _auto_fit_text(el, right_limit)
        # 逐项插图属于前景内容：渐变只保护文字，不能顺手洗淡独立小图。
        if bg['type'] == 'image' and any(e['type'] == 'scrim' for e in elements):
            try:
                page = {'elements': elements}
                item_ids = set(page_item_targets(page, storefront_photos(page)))
            except ValueError:
                item_ids = set()
            if len(item_ids) >= 2:
                for el in elements:
                    if el['type'] == 'picture' and el['asset_id'] in item_ids:
                        el['layer'] = 'overlay'
                        el['z'] = LAYER_ORDER['overlay']
        _lift_card_graphics(elements)
        elements.sort(key=lambda e: e["z"])  # 稳定排序，图层顺序即写入顺序
        pages.append(
            {
                "page_name": str(rp.get("page_name") or f"第 {pi + 1} 页"),
                "bg": bg,
                "elements": elements,
            }
        )

    return {"vi": vi, "pages": pages}


def bind_page_assets(layout: dict) -> None:
    """批次生成的同名 ID 仅在本页共享（例如背景和连续全景蒙版）。"""
    used = set()
    for i, page in enumerate(layout["pages"], 1):
        local = {}

        def bind(value):
            if value not in local:
                key = value
                while key.casefold() in used:
                    key = f"p{i}_{key}"
                used.add(key.casefold())
                local[value] = key
            return local[value]

        bg = page["bg"]
        if bg["type"] == "image":
            bg["image"] = bind(bg["image"])
            bg["measure_focus"] = True
        for el in page["elements"]:
            if "asset_id" in el:
                el["asset_id"] = bind(el["asset_id"])
            if el.get("mask", {}).get("asset_ids"):
                el["mask"]["asset_ids"] = [bind(v) for v in el["mask"]["asset_ids"]]


def clean_closing_title(layout: dict) -> None:
    if len(layout["pages"]) != 8:
        return
    page = layout["pages"][7]
    if page["page_name"].strip() == "明天见。":
        page["page_name"] = "明天见"
    choices = [e for e in page["elements"] if e["type"] == "text" and e["content"].strip() == "明天见。"]
    if choices:
        title = max(choices, key=lambda e: e["font"]["size"])
        note = title.get("note", "")
        if ("标题" in note and "副标题" not in note or
                page["page_name"] == "明天见" and title["font"]["size"] == max(
                    e["font"]["size"] for e in page["elements"] if e["type"] == "text")) and not any(
                s in note for s in ("正文", "落款", "图注")):
            title["content"] = "明天见"


def emblem_pictures(page: dict) -> dict:
    # 旧版没有 asset_kind，只识别素材描述开头明确以徽标为主体的 picture。
    marks = {}
    for el in page['elements']:
        if el['type'] != 'picture' or el.get('asset_id') == 'cover_title_art':
            continue
        subject = re.split(r'[，,。；;\n]', el.get('content', ''), maxsplit=1)[0].strip()
        legacy_mark = (not el.get('item_key') and re.search(r'徽标$|徽章$|品牌标志$|品牌标识$|\b(?:emblem|logo)$', subject, re.I)
                       and not re.search(r'不要|禁止|不含|无徽标|没有|含有|带有|印有|拍摄|照片|门头', subject))
        if el.get('asset_kind') == 'emblem' or legacy_mark:
            marks[el['asset_id']] = el
    return marks


def emblem_design(el: dict) -> str:
    # 首个短句是主体；后续只收图形本身的描述，不照抄布局、文案或「品牌提供原件」的来源声明。
    text = re.split(r'本页主题[：:]|文案语境|标题[：:]|纠正截图中的错误|返修[：:]|\n', el.get('content', ''), maxsplit=1)[0]
    clauses = [s.strip() for s in re.split(r'[，,。；;]', text) if s.strip()]
    design = clauses[:1]
    for s in clauses[1:]:
        if re.search(r'页眉|页脚|目录|页面|标题|文案|排版|右接|左接', s):
            break
        if '原件' not in s:
            design.append(s)
    return '，'.join(design) or '单个无字品牌图形'


def storefront_photos(page: dict) -> dict:
    """仅按独立照片自身的拍摄描述识别，不拿页名、文件名或邻近文案判定。"""
    marks = emblem_pictures(page)
    return {e['asset_id']: e for e in page['elements'] if e['type'] == 'picture' and e['asset_id'] not in marks
            and not e.get('item_key') and e.get('asset_id') != 'cover_title_art' and re.search(
                r'门面|门头|店面外观|店铺外观|门店外观|店铺入口|门店入口|店铺正立面|\b(?:storefront|shopfront)\b',
                e.get('content', ''), re.I)}


def page_item_targets(page: dict, storefronts: dict) -> dict:
    return item_targets(page, standalone=storefronts.keys() | emblem_pictures(page).keys())


def asset_requests(layout: dict, brief: dict | None = None) -> list[dict]:
    """从版式 JSON 抽出所有需要生图的素材，与元素一一对应。

    continuous=false 的网格会为每个格子单独出一张图（prompt 可取 cell_prompts）。
    """
    brief = brief or {}
    tone = str(brief.get("tone") or "")
    industry = str(brief.get("industry") or "")
    reqs: list[dict] = []
    seen: set[str] = set()
    folded: set[str] = set()

    specs = brief.get("page_specs") or []

    item_ids = set()

    def add(aid: str, prompt: str, kind: str, size: str = "1536x1024", target=None, *, page_context=True) -> None:
        key = aid.casefold()
        if key in folded and (target is not None or kind in ('storefront', 'emblem') or key in item_ids):
            raise ValueError(f'逐图审核的图片不能复用背景或其他素材 ID：{aid}')
        if aid in seen:
            return
        seen.add(aid)
        folded.add(key)
        if target is not None or kind in ('storefront', 'emblem'):
            item_ids.add(key)
        reqs.append({
            "asset_id": aid, "prompt": (json.dumps(target, ensure_ascii=False) if target is not None
                                        else prompt + ("。" + context if page_context else "")),
            **({'item_target': target} if target is not None else {}), "kind": kind, "size": size,
            "tone": tone, "industry": industry, "page": i + 1, "theme": theme,
        })

    for i, page in enumerate(layout["pages"]):
        spec = specs[i] if i < len(specs) else {}
        copy = spec.get("copy") or (brief.get("copy") if len(layout["pages"]) == 1 else {}) or {}
        theme = str(spec.get("name") or page["page_name"])
        texts = [e["content"] for e in page["elements"] if e["type"] == "text"]
        headings = [e for e in page["elements"] if e["type"] == "text" and e.get("content")]
        largest = max(headings, key=lambda e: e.get("font", {}).get("size", 0), default={})
        title = str(copy.get("title") or largest.get("content") or theme)
        excerpt = str(copy.get("body") or " / ".join(texts))[:240]
        context = f"本页主题：{theme}；标题：{title}；文案语境（不入画）：{excerpt}。"
        emblems = emblem_pictures(page)
        storefronts = storefront_photos(page)
        targets = page_item_targets(page, storefronts)
        if page['bg']['type'] == 'image' and len(targets) >= 2:
            subject = str(page['bg'].get('subject') or spec.get('subject') or '').strip()
            if not subject:
                raise ValueError(f'第{i + 1}页逐项配图缺少主摄影描述，请设置 bg.subject')
            shot = {'subject': subject.rstrip('。'), 'framing': background_framing(page, targets)}
            add(page['bg']['image'], json.dumps(shot, ensure_ascii=False), 'bg', page_context=False)
        elif page["bg"]["type"] == "image":
            # 若本页有 scrim，按其 wash 反推图片焦点应在的相反侧，写进生图提示词，
            # 保证遮罩洗白文字侧时不会毁掉图片焦点。
            wash = next((e.get("wash") for e in page["elements"] if e["type"] == "scrim"), None)
            focus_desc = ""
            if wash:
                opposite = {
                    "left": "右侧", "right": "左侧", "top": "下方", "bottom": "上方",
                    "bottomleft": "右上角", "bottomright": "左上角",
                    "topleft": "右下角", "topright": "左下角",
                }.get(wash, "一侧")
                focus_desc = f"，主体/焦点清晰地位于画面{opposite}，文字侧留干净浅色空间"
            description = "品牌摄影背景图，大面积自然留白" + focus_desc
            position = page["bg"].get("subject_position")
            if len(layout["pages"]) > 1 and i < OPENING_LIMIT and position in POSITIONS:
                description = OPENING_BG_PREFIX + composition(position)
            scene = str(page["bg"].get("subject") or spec.get("subject") or spec.get("visual") or "")
            creative = str(spec.get("creative") or "")
            style = str(spec.get("style") or "")
            if any(key in theme + title for key in ("来坐坐", "到店", "门店与订阅")):
                scene = "街道上的门店入口、门头和橱窗全景，不以饮品静物代替到店场景。" + scene
            elif any(key in theme + title for key in ("坐下来的理由", "座位", "空间一瞥")):
                scene = "展示座位、桌椅布局与空间纵深的室内全景，不以饮品静物代替空间。" + scene
            description += f"。摄影主体：{scene or title}。本页拍摄设定（只取摄影内容，不画版面）：{creative}；{style}"
            add(page["bg"]["image"] or "bg_global", description, "bg")
        for el in page["elements"]:
            if el["type"] == "picture":
                if el['asset_id'] in emblems:
                    design = json.dumps({'design': emblem_design(el)}, ensure_ascii=False)
                    add(el['asset_id'], design, 'emblem', size='1024x1024', page_context=False)
                    continue
                if el['asset_id'] in storefronts:
                    # 只传受控的取景特征；不把可能含店名、标题的整段摄影文案交给模型。
                    cues = ('平视', '正立面', '居中', '店门', '暖光', '橱窗', '长椅', '街面',
                            '暮色', '低反差', '胶片颗粒', '砖墙', '木门', '玻璃', '晨光', '逆光')
                    shot = {'framing': '、'.join(c for c in cues if c in el['content'])}
                    add(el['asset_id'], json.dumps(shot, ensure_ascii=False), 'storefront', page_context=False)
                    continue
                target = targets.get(el['asset_id'])
                add(el["asset_id"], el["content"] or "品牌主视觉", 'item' if target else 'photo', target=target)
            elif el["type"] == "mask_picture":
                mask = el["mask"]
                if mask["continuous"]:
                    target = targets.get(el['asset_id'])
                    add(el["asset_id"], el["content"] or "品牌主视觉", 'item' if target else 'hero', target=target)
                else:
                    prompts = mask["cell_prompts"]
                    ids = mask["asset_ids"] or [
                        f"{el['asset_id']}_c{i}" for i in range(mask["count"])
                    ]
                    for cell, cid in enumerate(ids):
                        base = prompts[cell] if cell < len(prompts) else (
                            f"{el['content']} · 第 {cell + 1} 格，主体居中"
                        )
                        add(cid, base, "cell")
    return reqs


def vi_palette(layout: dict) -> set[str]:
    return {c.upper() for c in layout["vi"].values()} | {"#000000", "#FFFFFF"}


def font_substitutions(layout: dict) -> list[dict]:
    """列出所有被本机字体守卫替换过的文字元素，供自检报告留痕。"""
    out = []
    for page in layout["pages"]:
        for el in page["elements"]:
            note = el.get("font", {}).get("substitution")
            if note:
                out.append(
                    {"page": page["page_name"], "element_id": el["element_id"],
                     "resolved": el["font"]["family"], "note": note}
                )
    return out


def auto_fit_adjustments(layout: dict) -> list[dict]:
    """列出被自动收口过的文字元素，供自检报告留痕。"""
    out = []
    for page in layout["pages"]:
        for el in page["elements"]:
            if el.get("auto_fit"):
                out.append({"page": page["page_name"],
                            "element_id": el["element_id"],
                            "adjustment": el["auto_fit"]})
    return out


def layout_primitives(layout: dict) -> dict:
    """统计本套版式实际用到的原语，供视觉评审动态选择检查规则。"""
    counts: dict[str, int] = {}
    mask_layouts: set[str] = set()
    has_image = False
    for page in layout["pages"]:
        if page["bg"]["type"] == "image":
            has_image = True
        for el in page["elements"]:
            counts[el["type"]] = counts.get(el["type"], 0) + 1
            if el["type"] in {"picture", "mask_picture"}:
                has_image = True
            if el["type"] == "mask_picture":
                mask_layouts.add(el["mask"]["layout"])
    return {
        "type_counts": counts,
        "mask_layouts": sorted(mask_layouts),
        "has_mask": "mask_picture" in counts,
        "has_continuous_mask": any(
            el["type"] == "mask_picture" and el["mask"]["continuous"]
            for page in layout["pages"] for el in page["elements"]
        ),
        "has_image": has_image,
    }
