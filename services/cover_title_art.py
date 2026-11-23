"""封面主标题艺术字：生成 + 抠图 + 重着色（可被流水线自动调用）。

从 make_title_art.py 抽出的核心逻辑，供 Orchestrator 在交付前自动套用，
使艺术字成为生成流程的一环，而不是事后手动命令。

流程：白底深绿书法字 → 本地抠白底 alpha → 重着色成原字色 → 透明 PNG。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    hx = str(hex_color).lstrip("#")
    try:
        return int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
    except (ValueError, IndexError):
        return 237, 230, 214


def matte_and_recolor(src: str | Path, dst: str | Path, hex_color: str, *,
                      style: str = 'calligraphy') -> tuple[int, int]:
    im = Image.open(src).convert("RGB")
    a = np.asarray(im).astype(np.float32)
    mn = a.min(axis=2)
    dist = np.clip(248.0 - mn, 0, 255)
    alpha = np.clip(dist / 170.0 * 255, 0, 255)
    r0, g0, b0 = _hex_to_rgb(hex_color)
    t = np.clip(dist / 190.0, 0, 1)
    col = np.zeros_like(a)
    col[..., 0] = r0 + (255 - r0) * 0.14 * t
    col[..., 1] = g0 + (255 - g0) * 0.14 * t
    col[..., 2] = b0 + (255 - b0) * 0.14 * t
    if style == 'metallic':
        # 只抠白底，不把笔画内的金属反光和色阶压成单一 VI 色。
        col = a
        alpha = np.clip(dist / 18.0, 0, 1) * 255
    rgba = np.dstack([np.clip(col, 0, 255), alpha]).astype(np.uint8)
    out = Image.fromarray(rgba, "RGBA")
    bbox = out.getbbox()
    if bbox:
        out = out.crop(bbox)
    out.save(dst)
    return out.size


def title_prompt(title: str, style: str = 'calligraphy') -> str:
    treatment = {
        'calligraphy': 'Chinese calligraphy in deep forest-green ink, brush strokes with flying-white texture',
        'metallic': 'Sculpted metallic lettering with brushed metal facets and controlled specular highlights',
        'lettering': 'Custom editorial lettering with refined letterforms and a solid dark fill',
    }[style]
    return (
        f"A premium logotype of EXACTLY these characters: 「{title}」. {treatment}, "
        "centered, large, occupying the middle band of the "
        "image, on a PURE SOLID WHITE (#FFFFFF) flat background with even lighting, no shadows, "
        "no background gradient, no watermark, no frame, no other text. "
        f"The {len(title)} characters must be written 100% correct with complete strokes, no gibberish, "
        "no missing or extra characters."
    )


def enlarged_art_position(orig_pos: dict, art_w: int, art_h: int,
                          max_h: float = 2.6, width_frac: float = 0.74) -> dict:
    """把艺术字等比放大到接近整页宽度（默认占页宽 ~74%），居中在原标题中心。

    原文本框往往只有 1.4in 高，直接 fit 会让艺术字偏小；这里以页宽为基准放大，
    高度上限 max_h 防止越过上下细线装饰，最终居中于原标题框中心。
    """
    page_w = 13.333
    target_w = min(orig_pos["w"], page_w * width_frac)
    target_h = target_w * art_h / art_w
    if target_h > max_h:          # 高度封顶，反推宽度
        target_h = max_h
        target_w = target_h * art_w / art_h
    cx = orig_pos["x"] + orig_pos["w"] / 2
    cy = orig_pos["y"] + orig_pos["h"] / 2
    return {
        "x": round(cx - target_w / 2, 3),
        "y": round(cy - target_h / 2, 3),
        "w": round(target_w, 3),
        "h": round(target_h, 3),
    }


def find_cover_title(layout: dict) -> dict | None:
    first = layout["pages"][0]
    texts = [e for e in first.get("elements", [])
             if e.get("type") == "text" and str(e.get("content", "")).strip()]
    if not texts:
        return None

    def rank(e: dict):
        size = float((e.get("font") or {}).get("size") or 0)
        content = str(e.get("content", ""))
        han = sum("\u4e00" <= c <= "\u9fff" for c in content)
        return (size, han, len(content))

    return max(texts, key=rank)
