"""版式 JSON 的近似预览（Pillow 渲染，不是 PowerPoint 实际渲染）。

用途：无 Office/LibreOffice 时，给「步骤 4 自检」提供一张可看的图。
它按与 ppt/builder.py 相同的坐标、颜色、蒙版切片规则重画一遍，
能检查构图/分层/对比度，但不能替代 PowerPoint 的真实字体与字距渲染。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import config
from .slicing import mask_geometry

FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
]


def _font(size_pt: float, scale: float, bold: bool = False):
    px = max(9, int(size_pt * scale / 72))
    candidates = FONT_CANDIDATES[1:2] + FONT_CANDIDATES[0:1] + FONT_CANDIDATES[2:] if bold else FONT_CANDIDATES
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, px)
            except OSError:
                continue
    return ImageFont.load_default()


def _hex(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return tuple(int(v[i : i + 2], 16) for i in (0, 2, 4))


def _rect(pos: dict, scale: float) -> tuple[int, int, int, int]:
    return (
        int(pos["x"] * scale), int(pos["y"] * scale),
        int((pos["x"] + pos["w"]) * scale), int((pos["y"] + pos["h"]) * scale),
    )


_PUNCT = "，。、；：！？）】》”’,.;:!?)"


def _wrap(text: str, font, max_px: int) -> list[str]:
    lines: list[str] = []
    for raw in text.split("\n"):
        cur = ""
        for ch in raw:
            # 标点不允许单独起新行，避免预览出现行首句号（PPTX 实际排版不受影响）
            if font.getlength(cur + ch) > max_px and cur and ch not in _PUNCT:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        lines.append(cur)
    # 避免末行只剩 1~2 个字符（中文断词难看），从上一行借几个字回来
    if len(lines) >= 2 and len(lines[-1]) <= 2:
        while len(lines[-1]) < 3 and len(lines[-2]) > 2:
            lines[-1] = lines[-2][-1] + lines[-1]
            lines[-2] = lines[-2][:-1]
    return lines


def render_layout_preview(
    layout: dict, assets: dict[str, str], out_path: str | Path, scale: int = 120
) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    W, H = int(config.SLIDE_W_IN * scale), int(config.SLIDE_H_IN * scale)
    hero_cache: dict[str, Image.Image] = {}

    def hero(aid: str) -> Image.Image | None:
        if aid in hero_cache:
            return hero_cache[aid]
        p = assets.get(aid)
        if not p or not Path(p).exists():
            return None
        im = Image.open(p).convert("RGB")
        hero_cache[aid] = im
        return im

    images: list[Image.Image] = []
    for page in layout["pages"]:
        canvas = Image.new("RGB", (W, H), _hex(page["bg"]["color"]))
        if page["bg"]["type"] == "image":
            im = hero(page["bg"]["image"] or "")
            if im:
                canvas = im.resize((W, H))
        d = ImageDraw.Draw(canvas)

        for el in page["elements"]:
            p = el["position"]
            box = _rect(p, scale)
            etype = el["type"]

            if etype == "text":
                font = _font(el["font"]["size"], scale, el["font"]["weight"] == "bold")
                color = _hex(el["font"]["color"])
                lines = _wrap(el["content"], font, p["w"] * scale)
                line_h = font.size * 1.35
                y = p["y"] * scale
                for line in lines:
                    tw = font.getlength(line)
                    x = p["x"] * scale
                    if el["align"] == "center":
                        x = p["x"] * scale + (p["w"] * scale - tw) / 2
                    elif el["align"] == "right":
                        x = p["x"] * scale + p["w"] * scale - tw
                    d.text((x, y), line, fill=color, font=font)
                    y += line_h

            elif etype == "shape":
                fill = _hex(el["fill"]) if el.get("fill") else None
                if el.get("shape") == "oval":
                    d.ellipse(box, fill=fill, outline=_hex(el["line"]) if el.get("line") else None, width=2)
                else:
                    d.rounded_rectangle(box, radius=6 if el.get("shape") == "round_rect" else 0, fill=fill,
                                        outline=_hex(el["line"]) if el.get("line") else None, width=2)

            elif etype == "picture":
                im = hero(el["asset_id"])
                if im:
                    canvas.paste(im.resize((box[2] - box[0], box[3] - box[1])), (box[0], box[1]))
                    d = ImageDraw.Draw(canvas)

            elif etype == "mask_picture":
                im = hero(el["asset_id"])
                # 几何与 ppt/slicing.py 完全一致，支持 single/columns/rows/grid
                for cell in mask_geometry(el):
                    i = cell["index"]
                    cx = p["x"] * scale + cell["dx"] * scale
                    cy = p["y"] * scale + cell["dy"] * scale
                    cw, chh = int(cell["w"] * scale), int(cell["h"] * scale)
                    if cw <= 0 or chh <= 0:
                        continue
                    slice_im = hero(f"{el['asset_id']}__{i}")
                    if slice_im:
                        # 与 PPTX 一致：直接用 AssetAgent 预裁好的切片
                        canvas.paste(slice_im.resize((cw, chh)), (int(cx), int(cy)))
                    elif im:
                        iw, ih = im.size
                        s = cell["src"]
                        crop = im.crop(
                            (int(iw * s["l"]), int(ih * s["t"]),
                             int(iw * s["r"]), int(ih * s["b"]))
                        )
                        canvas.paste(crop.resize((cw, chh)), (int(cx), int(cy)))
                    else:
                        d.rectangle([int(cx), int(cy), int(cx + cw), int(cy + chh)], fill=_hex("#0B2A4A"))
                    d = ImageDraw.Draw(canvas)

            elif etype == "decorate":
                c = _hex(el["color"])
                prim = el["primitive"]
                if prim == "ring":
                    for k in (0.0, 0.22, 0.42):
                        d.ellipse(
                            [box[0] + (box[2] - box[0]) * k, box[1] + (box[3] - box[1]) * k,
                             box[2] - (box[2] - box[0]) * k, box[3] - (box[3] - box[1]) * k],
                            outline=c, width=2,
                        )
                elif prim in {"line", "dashed_line"}:
                    yy = (box[1] + box[3]) // 2
                    if prim == "line":
                        d.line([box[0], yy, box[2], yy], fill=c, width=2)
                    else:
                        for xx in range(box[0], box[2], 14):
                            d.line([xx, yy, min(xx + 8, box[2]), yy], fill=c, width=2)
                elif prim == "diagonal_bar":
                    d.polygon(
                        [(box[0], box[3]), (box[0] + 40, box[1]), (box[2], box[1]), (box[2] - 40, box[3])],
                        fill=c,
                    )
                else:
                    for i in range(6):
                        for j in range(3):
                            x = box[0] + (box[2] - box[0]) * i / 5
                            y = box[1] + (box[3] - box[1]) * j / 2
                            d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=c)
        images.append(canvas)

    if len(images) == 1:
        images[0].save(out, "PNG")
    else:
        sheet = Image.new("RGB", (W, H * len(images)), (245, 245, 245))
        for i, im in enumerate(images):
            sheet.paste(im, (0, i * H))
        sheet.save(out, "PNG")
    return out
