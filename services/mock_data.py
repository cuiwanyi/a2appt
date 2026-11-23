"""离线 mock 数据：不调用 LLM / 生图也能跑通整条 A2A 链路。

按 brief 选「版式配方」，覆盖不同原语组合，用来离线验证编排器是通用的：
  centered    居中海报 + 独立图片图层（无形状图片填充）
  pure_type   纯文字极简（全页零图片、零素材请求）
  full_bleed  全幅背景图 + 遮罩形状 + 文字
  single_oval 单个圆形异形裁图（mask layout=single）
  grid        2x2 多图网格，每格独立素材（continuous=false）
  columns     5 柱连续全景（continuous=true，只是众多版式之一）
  rows        3 行横向条带（layout=rows）
  diagonal    斜切/三角形分栏（只用原生形状）

配方由 brief.layout_recipe 指定，或从 creative/tone 关键词推断；默认是 centered，
不会默认假定任何柱状蒙版。
"""
from __future__ import annotations

import colorsys
import random
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

RECIPES = (
    "centered", "pure_type", "full_bleed", "single_oval",
    "grid", "columns", "rows", "diagonal",
)

_RECIPE_KEYWORDS = (
    ("columns", ("柱状", "柱子", "5 根", "条带蒙版", "连续全景")),
    ("grid", ("网格", "九宫", "多图", "产品矩阵", "2x2")),
    ("single_oval", ("圆形裁", "异形裁", "圆形构图")),
    ("full_bleed", ("全幅", "整版背景", "满版", "大背景")),
    ("pure_type", ("纯文字", "极简", "零图片", "无图")),
    ("diagonal", ("对角", "斜切", "切分")),
    ("rows", ("横向条带", "横条", "多行拼图")),
)


def choose_recipe(brief: dict[str, Any]) -> str:
    from ppt.image_layout import full_bleed_rule, text_only
    explicit = str(brief.get("layout_recipe") or "").strip().lower()
    if text_only(brief):
        return "pure_type"
    rule = full_bleed_rule(brief)
    if rule is True:
        return "full_bleed"
    if explicit in RECIPES and not (explicit == 'full_bleed' and rule is False):
        return explicit
    text = (str(brief.get("creative", "")) + " " + str(brief.get("tone", ""))).lower()
    for name, keys in _RECIPE_KEYWORDS:
        if any(k in text for k in keys):
            return name
    return "centered"


def _text(content, x, y, w, h, size, color, weight="normal", align="left", note=""):
    return {
        "type": "text", "content": content,
        "position": {"x": x, "y": y, "w": w, "h": h},
        "font": {"family": "微软雅黑", "weight": weight, "color": color, "size": size},
        "align": align, "layer": "text", "note": note,
    }


def _shape(shape, x, y, w, h, fill, layer="decoration", note=""):
    return {
        "type": "shape", "shape": shape, "fill": fill, "line": None,
        "position": {"x": x, "y": y, "w": w, "h": h},
        "layer": layer, "note": note,
    }


def _deco(primitive, x, y, w, h, color, note=""):
    return {
        "type": "decorate", "primitive": primitive, "color": color,
        "position": {"x": x, "y": y, "w": w, "h": h},
        "layer": "decoration", "note": note,
    }


def _base(brief: dict[str, Any]) -> tuple[dict, dict, dict, str]:
    vi = brief.get("vi") or {}
    v = {
        "primary": vi.get("primary", "#0B2A4A"),
        "secondary": vi.get("secondary", "#C9A86A"),
        "accent": vi.get("accent", "#E8EEF5"),
    }
    copy = brief.get("copy") or {}
    c = {
        "title": copy.get("title", "让想法成为作品"),
        "subtitle": copy.get("subtitle", "为每一次表达留出空间"),
        "body": copy.get("body", "简洁的层次、克制的色彩与恰到好处的留白，"
                                "让信息自己说话，也让品牌被记住。"),
        "cta": copy.get("cta", "了解更多  ·  即刻开始"),
    }
    return v, c, brief, str(brief.get("brand", "BRAND"))


def _footer(brand: str, accent: str) -> dict:
    return _text("© 2026 " + brand + "  ·  形象广告示意",
                 0.85, 6.85, 6.0, 0.3, 10, accent, note="页脚")


def _recipe_centered(v, c, brand, accent):
    return {
        "page_name": "居中海报 · 独立图片图层",
        "bg": {"type": "color", "color": v["primary"], "image": None},
        "elements": [
            _text(brand.upper(), 1.0, 0.6, 11.33, 0.4, 15, v["secondary"], "bold", "center", "品牌名"),
            _deco("ring", 0.6, 0.45, 0.9, 0.9, v["secondary"], "左上圆环"),
            _text(c["title"], 1.0, 2.0, 11.33, 1.3, 46, "#FFFFFF", "bold", "center", "主标题"),
            _shape("rect", 5.87, 3.55, 1.6, 0.06, v["secondary"], note="分隔金线"),
            _text(c["subtitle"], 1.0, 3.85, 11.33, 0.6, 21, v["secondary"], "normal", "center", "副标题"),
            _text(c["body"], 2.0, 4.55, 9.33, 0.8, 14, accent, "normal", "center", "正文"),
            {
                "type": "picture", "asset_id": "photo_1", "content": "品牌主题静物或场景特写",
                "position": {"x": 1.0, "y": 5.55, "w": 11.33, "h": 0.85},
                "layer": "picture", "note": "底部横向图带",
            },
            _text(c["cta"], 1.0, 6.45, 11.33, 0.45, 16, "#FFFFFF", "bold", "center", "行动号召"),
            _footer(brand, accent),
        ],
    }


def _recipe_pure_type(v, c, brand, accent):
    return {
        "page_name": "纯文字极简 · 零图片",
        "bg": {"type": "color", "color": v["primary"], "image": None},
        "elements": [
            _text(brand.upper(), 0.9, 0.7, 6.0, 0.4, 15, v["secondary"], "bold", note="品牌名"),
            _shape("rect", 0.9, 1.35, 0.75, 0.055, v["secondary"], note="品牌分隔线"),
            _text(c["title"], 0.9, 2.15, 11.2, 1.5, 52, "#FFFFFF", "bold", note="主标题"),
            _text(c["subtitle"], 0.9, 3.85, 10.0, 0.6, 22, v["secondary"], note="副标题"),
            _text(c["body"], 0.9, 4.65, 8.8, 1.0, 15, accent, note="正文"),
            _text(c["cta"], 0.9, 6.05, 6.0, 0.5, 17, "#FFFFFF", "bold", note="行动号召"),
            _deco("dashed_line", 0.9, 6.72, 11.0, 0.02, v["secondary"], "底部虚线"),
            _footer(brand, accent),
        ],
    }


def _photo_page(v, c, spec):
    """摄影兜底：只排用户文案；方向是拍摄意图，建稿时仍以成图为准。"""
    import re
    creative = str(spec.get('creative') or '')
    upper = bool(re.search(r'(?:文字|信息|标题|短笺)[^。；]{0,20}(?:上方|置上)', creative))
    right = bool(re.search(r'右侧[^。；]{0,16}(?:文字|节点)|文字在右', creative))
    dark = bool(re.search(r'深色渐隐|深[^。；，]{0,8}色渐隐|米白字|深色遮罩', creative))
    wash = 'top' if upper else 'right' if right else 'left'
    bg, ink = (v['secondary'], '#FFFFFF') if dark else ('#FFFFFF', '#20160F')
    x, w = (.8, 11.7) if upper else ((7.1, 5.5) if right else (.8, 6.3))
    ys = (.35, 1.12, 1.65, 2.55) if upper else (.85, 2.05, 2.8, 5.75)
    hs = (.76, .48, .87, 1.05) if upper else (1.12, .65, 2.8, 1.35)
    els = [_text(c[k], x, y, w, h, size, ink, 'bold' if k == 'title' else 'normal', note=k)
           for k, y, h, size in zip(('title', 'subtitle', 'body', 'cta'), ys, hs, (38, 16, 14, 11)) if c[k]]
    return {'page_name': spec.get('name') or '摄影页',
            'bg': {'type': 'image', 'image': 'bg_full', 'color': bg},
            'elements': [dict(type='scrim', wash=wash, color=bg, position=dict(x=0,y=0,w=13.333,h=7.5)), *els]}


def _recipe_full_bleed(v, c, brand, accent):
    return {
        "page_name": "全幅背景图 · 遮罩 + 文字",
        "bg": {"type": "image", "color": v["primary"], "image": "bg_full"},
        "elements": [
            _shape("rect", 0.0, 0.0, 6.4, 7.5, v["primary"], layer="overlay", note="左侧深色遮罩"),
            _text(brand.upper(), 0.9, 0.7, 5.0, 0.4, 15, v["secondary"], "bold", note="品牌名"),
            _text(c["title"], 0.9, 2.0, 5.2, 1.8, 44, "#FFFFFF", "bold", note="主标题"),
            _text(c["subtitle"], 0.9, 3.95, 5.0, 0.6, 20, v["secondary"], note="副标题"),
            _text(c["body"], 0.9, 4.7, 5.0, 1.0, 14, accent, note="正文"),
            _text(c["cta"], 0.9, 6.1, 5.0, 0.5, 16, "#FFFFFF", "bold", note="行动号召"),
            _deco("ring", 11.4, 0.5, 1.0, 1.0, v["secondary"], "右上圆环"),
            _footer(brand, accent),
        ],
    }


def _recipe_single_oval(v, c, brand, accent):
    return {
        "page_name": "圆形异形裁图 · 单形状图片填充",
        "bg": {"type": "color", "color": v["primary"], "image": None},
        "elements": [
            {
                "type": "mask_picture", "asset_id": "hero_1",
                "content": "品牌主题主视觉，主体居中，柔和自然光",
                "position": {"x": 7.5, "y": 1.15, "w": 5.0, "h": 5.0},
                "mask": {"layout": "single", "shape": "oval", "continuous": True,
                         "gap_in": 0.0, "radius_in": 0.0},
                "layer": "mask", "note": "圆形裁切主视觉",
            },
            _text(brand.upper(), 0.85, 0.65, 5.6, 0.4, 15, v["secondary"], "bold", note="品牌名"),
            _shape("rect", 0.85, 1.25, 0.75, 0.055, v["secondary"], note="品牌分隔线"),
            _text(c["title"], 0.85, 1.85, 6.0, 1.6, 42, "#FFFFFF", "bold", note="主标题"),
            _text(c["subtitle"], 0.85, 3.6, 5.8, 0.6, 21, v["secondary"], note="副标题"),
            _text(c["body"], 0.85, 4.35, 5.6, 1.1, 14, accent, note="正文"),
            _text(c["cta"], 0.85, 5.9, 5.0, 0.5, 16, "#FFFFFF", "bold", note="行动号召"),
            _deco("ring", 11.9, 0.3, 0.9, 0.9, v["secondary"], "右上圆环"),
            _footer(brand, accent),
        ],
    }


def _recipe_grid(v, c, brand, accent):
    return {
        "page_name": "2x2 多图网格 · 每格独立素材",
        "bg": {"type": "color", "color": v["primary"], "image": None},
        "elements": [
            {
                "type": "mask_picture", "asset_id": "grid_1",
                "content": "四款不同场景的产品系列",
                "position": {"x": 7.05, "y": 1.2, "w": 5.4, "h": 5.35},
                "mask": {
                    "layout": "grid", "rows": 2, "cols": 2, "shape": "round_rect",
                    "continuous": False, "gap_in": 0.14, "radius_in": 0.0,
                    "cell_prompts": [
                        "系列产品之一，浅色背景，主体居中",
                        "系列产品之二，暖调光线，主体居中",
                        "系列产品之三，冷调光线，主体居中",
                        "系列产品之四，深色背景，主体居中",
                    ],
                },
                "layer": "mask", "note": "四张不同素材组成的网格",
            },
            _text(brand.upper(), 0.85, 0.65, 5.6, 0.4, 15, v["secondary"], "bold", note="品牌名"),
            _text(c["title"], 0.85, 1.8, 5.9, 1.5, 40, "#FFFFFF", "bold", note="主标题"),
            _text(c["subtitle"], 0.85, 3.5, 5.6, 0.6, 20, v["secondary"], note="副标题"),
            _text(c["body"], 0.85, 4.25, 5.5, 1.1, 14, accent, note="正文"),
            _text(c["cta"], 0.85, 5.9, 5.0, 0.5, 16, "#FFFFFF", "bold", note="行动号召"),
            _deco("dot_grid", 0.85, 6.6, 3.0, 0.4, v["secondary"], "点阵装饰"),
            _footer(brand, accent),
        ],
    }


def _recipe_columns(v, c, brand, accent):
    return {
        "page_name": "柱状连续全景 · 示例版式之一",
        "bg": {"type": "color", "color": v["primary"], "image": None},
        "elements": [
            _text(brand.upper(), 0.85, 0.55, 5.4, 0.4, 15, v["secondary"], "bold", note="品牌名"),
            _shape("rect", 0.85, 1.15, 0.75, 0.055, v["secondary"], note="品牌分隔金线"),
            _text(c["title"], 0.85, 1.55, 6.1, 1.5, 42, "#FFFFFF", "bold", note="主标题"),
            _text(c["subtitle"], 0.85, 3.15, 5.8, 0.6, 21, v["secondary"], note="副标题"),
            _text(c["body"], 0.85, 3.95, 5.5, 1.1, 14, accent, note="正文"),
            _text(c["cta"], 0.85, 5.75, 4.2, 0.5, 16, "#FFFFFF", "bold", note="行动号召"),
            {
                "type": "mask_picture", "asset_id": "hero_1",
                "content": "品牌主题连续全景，画面有横向延展性",
                "position": {"x": 7.15, "y": 0.85, "w": 5.55, "h": 5.95},
                "mask": {
                    "layout": "columns", "shape": "rect", "count": 5,
                    "ratios": [0.55, 0.78, 1.0, 0.86, 0.65],
                    "gap_in": 0.11, "align": "end", "continuous": True, "radius_in": 0.0,
                },
                "layer": "mask", "note": "同一张主视觉按柱连续裁切",
            },
            _deco("ring", 11.9, 0.25, 1.0, 1.0, v["secondary"], "右上细线圆环"),
            _deco("dashed_line", 0.85, 6.65, 5.6, 0.02, v["secondary"], "左下虚线"),
            _footer(brand, accent),
        ],
    }


def _recipe_rows(v, c, brand, accent):
    return {
        "page_name": "横向条带拼图 · layout=rows",
        "bg": {"type": "color", "color": v["primary"], "image": None},
        "elements": [
            {
                "type": "mask_picture", "asset_id": "stripe_1",
                "content": "宽幅品牌主题全景，适合横向分条",
                "position": {"x": 0.85, "y": 4.15, "w": 11.6, "h": 2.35},
                "mask": {"layout": "rows", "shape": "rect", "count": 3,
                         "ratios": [1.0, 0.85, 0.7], "gap_in": 0.12,
                         "align": "start", "continuous": True, "radius_in": 0.0},
                "layer": "mask", "note": "三条横向条带共用一张连续全景",
            },
            _text(brand.upper(), 0.85, 0.6, 6.0, 0.4, 15, v["secondary"], "bold", note="品牌名"),
            _text(c["title"], 0.85, 1.5, 11.4, 1.3, 44, "#FFFFFF", "bold", note="主标题"),
            _text(c["subtitle"], 0.85, 3.0, 9.0, 0.6, 21, v["secondary"], note="副标题"),
            _text(c["body"], 0.85, 3.55, 10.0, 0.55, 13, accent, note="正文"),
            _deco("ring", 11.6, 0.4, 1.0, 1.0, v["secondary"], "右上圆环"),
            _footer(brand, accent),
        ],
    }


def _recipe_diagonal(v, c, brand, accent):
    return {
        "page_name": "斜切分栏 · 原生异形形状",
        "bg": {"type": "color", "color": v["primary"], "image": None},
        "elements": [
            _shape("parallelogram", 0.0, 0.0, 5.2, 7.5, v["secondary"],
                   layer="decoration", note="左侧斜切色块"),
            _shape("triangle", 10.6, 0.0, 2.73, 2.6, v["accent"],
                   layer="decoration", note="右上三角"),
            _text(brand.upper(), 0.9, 0.7, 4.0, 0.4, 15, v["primary"], "bold", note="品牌名"),
            _text(c["title"], 5.7, 1.9, 6.7, 1.6, 42, "#FFFFFF", "bold", note="主标题"),
            _text(c["subtitle"], 5.7, 3.65, 6.5, 0.6, 20, v["secondary"], note="副标题"),
            _text(c["body"], 5.7, 4.4, 6.3, 1.0, 14, accent, note="正文"),
            _text(c["cta"], 5.7, 5.85, 6.0, 0.5, 16, "#FFFFFF", "bold", note="行动号召"),
            _deco("diagonal_bar", 0.6, 5.7, 3.8, 0.35, v["primary"], "左下斜条"),
            _footer(brand, accent),
        ],
    }


_BUILDERS = {
    "centered": _recipe_centered,
    "pure_type": _recipe_pure_type,
    "full_bleed": _recipe_full_bleed,
    "single_oval": _recipe_single_oval,
    "grid": _recipe_grid,
    "columns": _recipe_columns,
    "rows": _recipe_rows,
    "diagonal": _recipe_diagonal,
}


def build_mock_layout(brief: dict[str, Any]) -> dict:
    v, c, _brief, brand = _base(brief)
    recipe = choose_recipe(brief)
    from ppt.image_layout import full_bleed_rule
    if recipe == 'full_bleed' and full_bleed_rule(brief) is True:
        page = _photo_page(v, c, brief)
    else:
        page = _BUILDERS[recipe](v, c, brand, v["accent"])
    return {
        "vi": v,
        "layout_recipe": recipe,
        "pages": [page],
    }


def mock_image(out_path: str | Path, size: tuple[int, int] = (1536, 1024), seed: int = 7) -> Path:
    """画一张品牌中性的抽象占位图（无文字、无具体场景假定，Pillow 纯本地）。"""
    w, h = size
    rnd = random.Random(seed)
    img = Image.new("RGB", (w, h))
    d0 = ImageDraw.Draw(img)
    # 按 seed 换色相：多格独立素材在离线验证里也能明显区分
    hue = (seed * 0.137) % 1.0
    top = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.55, 0.28))
    bottom = tuple(int(c * 255) for c in colorsys.hsv_to_rgb((hue + 0.09) % 1.0, 0.40, 0.86))
    for y in range(h):
        k = y / h
        d0.line([(0, y), (w, y)],
                fill=tuple(int(top[i] + (bottom[i] - top[i]) * k) for i in range(3)))

    d = ImageDraw.Draw(img, "RGBA")
    # 柔和光带与几何块，不暗示任何具体行业或场景
    for _ in range(9):
        x0 = rnd.randint(-w // 3, w)
        bw = rnd.randint(w // 8, w // 2)
        d.polygon(
            [(x0, h), (x0 + bw, h), (x0 + bw + w // 5, 0), (x0 + w // 5, 0)],
            fill=(255, 226, 180, rnd.randint(10, 26)),
        )
    for _ in range(5):
        r = rnd.randint(w // 10, w // 4)
        cx, cy = rnd.randint(0, w), rnd.randint(0, h // 2)
        d.ellipse([cx - r, cy - r, cx + r, cy + r],
                  outline=(255, 235, 200, rnd.randint(30, 70)), width=2)
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gx, gy = int(w * 0.7), int(h * 0.3)
    for r in range(200, 0, -8):
        gd.ellipse([gx - r, gy - r, gx + r, gy + r],
                   fill=(255, 226, 180, int(2 * (200 - r) / 200)))
    img = Image.alpha_composite(img.convert("RGBA"),
                                glow.filter(ImageFilter.GaussianBlur(20))).convert("RGB")

    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    img.save(p, "PNG")
    return p
