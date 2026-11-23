"""形状图片填充的通用几何与素材预裁切。

为什么不直接用 OOXML a:srcRect：
  srcRect 是标准属性，PowerPoint 支持；但 LibreOffice 实测会忽略它，
  于是每个形状都把整张图压缩显示（画面重复、错位）。
  预裁成独立 PNG 后，任何渲染器都只需「把这张图填满形状」，结果一致。

几何与版式无关：mask.layout 决定单元格怎么排，
  - single   单形状（圆形/圆角/异形裁图）
  - columns  横排 N 格，高度按 ratios，align 控制上下对齐
  - rows     竖排 N 格，宽度按 ratios，align 控制左右对齐
  - grid     rows × cols 网格
continuous=true  所有格从同一张图连续裁切（跨格画面衔接）
continuous=false 每格用自己那张素材，按 cover 裁到格子比例
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image


def _cover_view(iw: int, ih: int, fw: float, fh: float) -> tuple[float, float, float, float]:
    """把 iw×ih 的图等比 cover 到 fw×fh 的框里，返回原图上可见区域的 (l,t,r,b) 分数。"""
    ia = iw / max(1, ih)
    fa = fw / max(0.001, fh)
    if ia > fa:
        vis = fa / ia
        l = (1 - vis) / 2
        return l, 0.0, 1 - l, 1.0
    if ia < fa:
        vis = ia / fa
        t = (1 - vis) / 2
        return 0.0, t, 1.0, 1 - t
    return 0.0, 0.0, 1.0, 1.0


def mask_geometry(el: dict, image_size: tuple[int, int] | None = None) -> list[dict]:
    """返回每个单元格的相对外框矩形与源图裁切框。

    字段：index / dx,dy,w,h（英寸，相对外框左上角）/ asset_id / src{l,t,r,b}（0~1 分数）
    image_size 为共享原图像素尺寸，用于 cover 裁切；None 表示不校正宽高比（等价拉伸）。
    构建器和近似预览只用 dx/dy/w/h，与宽高比无关。
    """
    p = el["position"]
    mask = el["mask"]
    W, H = p["w"], p["h"]
    gap = mask["gap_in"]
    layout = mask["layout"]
    align = mask["align"]
    aid = el["asset_id"]
    continuous = mask["continuous"]
    ids = mask.get("asset_ids") or []

    cells: list[dict] = []

    def put(dx: float, dy: float, w: float, h: float) -> None:
        i = len(cells)
        cells.append({
            "index": i,
            "dx": round(dx, 4), "dy": round(dy, 4),
            "w": round(w, 4), "h": round(h, 4),
            "asset_id": ids[i] if (not continuous and i < len(ids)) else aid,
        })

    def offset(full: float, piece: float) -> float:
        if align == "start":
            return 0.0
        if align == "center":
            return (full - piece) / 2
        return full - piece

    if layout == "single":
        put(0.0, 0.0, W, H)

    elif layout == "columns":
        n = mask["count"]
        g = gap
        cw = (W - g * (n - 1)) / n
        if cw <= 0.05:
            g, cw = 0.0, W / n
        for i in range(n):
            h = H * mask["ratios"][i]
            put(i * (cw + g), offset(H, h), cw, h)

    elif layout == "rows":
        n = mask["count"]
        g = gap
        rh = (H - g * (n - 1)) / n
        if rh <= 0.05:
            g, rh = 0.0, H / n
        for i in range(n):
            w = W * mask["ratios"][i]
            put(offset(W, w), i * (rh + g), w, rh)

    else:  # grid
        r, c = mask["rows"], mask["cols"]
        g = gap
        cw = (W - g * (c - 1)) / c
        rh = (H - g * (r - 1)) / r
        if cw <= 0.05 or rh <= 0.05:
            g, cw, rh = 0.0, W / c, H / r
        for i in range(r):
            for j in range(c):
                put(j * (cw + g), i * (rh + g), cw, rh)

    # 连续模式：所有格共享一张图，按「整图铺满外框」的可见区域做子矩形裁切
    if continuous:
        if image_size:
            view = _cover_view(image_size[0], image_size[1], W, H)
        else:
            view = (0.0, 0.0, 1.0, 1.0)
        vl, vt, vr, vb = view
        for cell in cells:
            cell["src"] = {
                "l": vl + (cell["dx"] / W) * (vr - vl),
                "t": vt + (cell["dy"] / H) * (vb - vt),
                "r": vl + ((cell["dx"] + cell["w"]) / W) * (vr - vl),
                "b": vt + ((cell["dy"] + cell["h"]) / H) * (vb - vt),
            }
    else:
        # 独立模式：src 由 prepare_mask_slices 按每格自己的图算，这里保持整图
        for cell in cells:
            cell["src"] = {"l": 0.0, "t": 0.0, "r": 1.0, "b": 1.0}
    return cells


def src_rect_for(el: dict, image_size: tuple[int, int] | None = None) -> list[dict]:
    """兼容用：没有预裁切片时，退回 OOXML a:srcRect 的千分数参数（PowerPoint 精确）。"""
    out = []
    for cell in mask_geometry(el, image_size):
        s = cell["src"]
        out.append({
            "l": round(s["l"] * 100000),
            "t": round(s["t"] * 100000),
            "r": round((1 - s["r"]) * 100000),
            "b": round((1 - s["b"]) * 100000),
        })
    return out


def _crop_box(iw: int, ih: int, src: dict) -> tuple[int, int, int, int]:
    x0 = int(iw * src["l"])
    y0 = int(ih * src["t"])
    x1 = int(iw * src["r"])
    y1 = int(ih * src["b"])
    return (
        max(0, min(x0, iw - 1)),
        max(0, min(y0, ih - 1)),
        min(iw, max(x0 + 1, x1)),
        min(ih, max(y0 + 1, y1)),
    )


def prepare_mask_slices(el: dict, assets: dict[str, str], out_dir: str | Path,
                        quality: int = 92) -> list[str]:
    """按版式规格为每个单元格准备一张裁好的 PNG，返回路径列表（顺序对应格序）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    aid = el["asset_id"]
    mask = el["mask"]
    paths: list[str] = []

    if mask["continuous"]:
        whole = assets.get(aid)
        if not whole or not Path(whole).exists():
            return []
        with Image.open(whole) as src:
            im = src.convert("RGB")
            iw, ih = im.size
        cells = mask_geometry(el, image_size=(iw, ih))
        for cell in cells:
            slice_path = out / f"{aid}__slice{cell['index']}.png"
            im.crop(_crop_box(iw, ih, cell["src"])).save(slice_path, "PNG", optimize=True)
            paths.append(str(slice_path))
    else:
        cells = mask_geometry(el)
        for cell in cells:
            src_path = assets.get(cell["asset_id"]) or assets.get(aid)
            if not src_path or not Path(src_path).exists():
                paths.append("")
                continue
            with Image.open(src_path) as f:
                im = f.convert("RGB")
                iw, ih = im.size
            l, t, r, b = _cover_view(iw, ih, cell["w"], cell["h"])
            box = _crop_box(iw, ih, {"l": l, "t": t, "r": r, "b": b})
            slice_path = out / f"{aid}__slice{cell['index']}.png"
            im.crop(box).save(slice_path, "PNG", optimize=True)
            paths.append(str(slice_path))
    return paths


def slice_mask_asset(image_path: str | Path, el: dict, out_dir: str | Path,
                     asset_id: str, quality: int = 92) -> list[str]:
    """旧接口保留：连续模式下从单张图裁出所有格切片。"""
    assets = {asset_id: str(image_path)}
    return prepare_mask_slices(el, assets, out_dir, quality)
