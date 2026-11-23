"""内容安全区：焦点确定之后，算出「非焦点区」矩形并把版式内容放进去。

设计原则（与 ppt/layout_router.py 一样，不按页特判）：
    版式函数只按「洗白侧在左」的基准坐标写内容；
    引擎根据真实焦点方位把内容适配到安全区：
      axis="h"（焦点在左/右）-> 竖长区，保留原坐标 / 左右镜像
      axis="v"（焦点在上/下）-> 横宽区，横向铺满整页、纵向压缩进上/下半

这样「先看图定焦点 -> 再把内容放到不聚焦的部分」是同一段通用代码，
任何版式、任何页都走它。
"""
from __future__ import annotations

PAGE_W, PAGE_H = 13.333, 7.5
_BASE_W = PAGE_W
_BASE_H = PAGE_H


def content_zone(focus: dict, page_w: float = PAGE_W,
                 page_h: float = PAGE_H) -> dict:
    """由焦点结果算内容安全区。boundary 语义：从洗白侧算起，焦点开始的位置占比。"""
    wash = str(focus.get("wash", "left")).lower()
    b = float(focus.get("boundary", 0.60))
    b = max(0.40, min(0.80, b))
    if wash in {"left", "right"}:
        w = page_w * b
        x = 0.0 if wash == "left" else page_w - w
        return {"axis": "h", "x": round(x, 3), "y": 0.0,
                "w": round(w, 3), "h": page_h, "boundary": b}
    if wash == "center":
        # 中心焦点：四周都是留白，文字放在上半横带（径向遮罩已把边缘洗成纯色）
        return {"axis": "v", "x": 0.0, "y": 0.0,
                "w": page_w, "h": round(page_h * 0.42, 3),
                "boundary": 0.42, "center": True}
    h = page_h * b
    y = 0.0 if wash == "top" else page_h - h
    return {"axis": "v", "x": 0.0, "y": round(y, 3),
            "w": page_w, "h": round(h, 3), "boundary": b}


def layout_width(zone: dict) -> float:
    """版式可用的基准宽度：上下型铺满整页，左右型只用洗白侧。"""
    return PAGE_W if zone["axis"] == "v" else zone["w"]


def fit_to_zone(els: list[dict], zone: dict) -> list[dict]:
    """把版式元素适配到安全区。左右型原样返回；上下型横向铺满、纵向压缩。

    文字框高度按字号给下限，避免纵向压缩后文字被裁；图片按原比例缩放，
    绝不被压扁。整页 scrim 遮罩不参与适配。
    """
    if zone["axis"] != "v":
        return els
    sy = zone["h"] / _BASE_H
    out = []
    for e in els:
        ne = dict(e)
        ne["position"] = dict(e["position"])
        if e.get("asset_id") == "scrim":
            out.append(ne)
            continue
        p = ne["position"]
        old_w, old_h = p.get("w", 0), p.get("h", 0)
        p["y"] = round(zone["y"] + p.get("y", 0) * sy, 3)
        p["h"] = round(old_h * sy, 3)

        if ne.get("type") == "text":
            ne["font"] = dict(e.get("font") or {})
            size = float(ne["font"].get("size") or 12)
            new_size = max(9.0, round(size * 0.82, 1))
            ne["font"]["size"] = new_size
            # 单行至少要能容纳字号本身（含行距）
            p["h"] = max(p["h"], round(new_size / 72 * 1.45, 3))
        elif ne.get("type") == "picture" and old_h > 0:
            # 等比：以压缩后的高度为准反推宽度，保持原始宽高比
            p["w"] = round(min(_BASE_W, p["h"] * (old_w / old_h)), 3)
        elif ne.get("type") == "shape" and old_h < 0.03:
            # 细线保持细线，不因压缩变得过粗或消失
            p["h"] = max(0.008, p["h"])
        out.append(ne)
    return out
