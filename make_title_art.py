"""封面「主标题艺术字」生成 + 应用（独立可选命令，不改主流水线）。

与 make_cover.py 的区别（重要）：
    make_cover.py --apply  = 把【整页】清空铺成一张设计图（字在图里，整页不可编辑）。
    make_title_art.py      = 只把封面【主标题那一行】换成艺术字图片，其余背景摄影、
                             深绿色块、细线、副标题、引言、落款全部保留为原生可编辑元素。

原理：
    1) 生图模型出「白底 + 深色书法字」——白底便于本地抠图，深字笔画清晰。
    2) 本地按到白底的距离生成 alpha（飞白半透自然保留），再把字重着色成
       原封面上主标题的颜色（如米白 #EDE6D6），裁掉透明边 → 透明 PNG。
       这样艺术字可叠在任意底色上都不会隐形。
    3) 应用时读回原 01_layout.json，找到主标题 text，只把它替换成一个 picture
       元素（沿用原 position），其他元素一个不动。

用法：
    # 1) 生成候选（读运行目录封面主标题的文字与颜色，自动匹配）
    python make_title_art.py --run workspace/<run_id> --n 3

    # 2) 打开 workspace/_title_art_<run>/pick_gallery.html 挑一张

    # 3) 应用选中的那张（透明 PNG）
    python make_title_art.py --apply workspace/<run_id> --image <选中的 title_rgba_*.png>

产出 deliverable_titleart.pptx；封面只有主标题是图片，其余原生可编辑。
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from services.llm import LLMClient
from services.mock_data import mock_image
from ppt.schema import normalize_layout
from ppt.builder import PPTBuilder
from ppt.tools import PPTToolkit
from ppt.inspector import inspect
from ppt.render import try_render

TITLE_ASSET = "cover_title_art"


# ---------------- 图片处理：白底深色字 → 透明 + 重着色 ----------------
def matte_and_recolor(src: str | Path, dst: str | Path, hex_color: str) -> tuple[int, int]:
    """白底艺术字 → 透明底、字重着色为 hex_color，裁掉透明边。返回裁剪后尺寸。"""
    im = Image.open(src).convert("RGB")
    a = np.asarray(im).astype(np.float32)
    mn = a.min(axis=2)                      # 到白底的距离：白色≈255，深色笔画低
    dist = np.clip(248.0 - mn, 0, 255)
    alpha = np.clip(dist / 170.0 * 255, 0, 255)

    hx = hex_color.lstrip("#")
    r0, g0, b0 = (int(hx[i:i + 2], 16) for i in (0, 2, 4))
    t = np.clip(dist / 190.0, 0, 1)         # 笔画中心略提亮，保留笔锋层次
    col = np.zeros_like(a)
    col[..., 0] = r0 + (255 - r0) * 0.14 * t
    col[..., 1] = g0 + (255 - g0) * 0.14 * t
    col[..., 2] = b0 + (255 - b0) * 0.14 * t

    rgba = np.dstack([np.clip(col, 0, 255), alpha]).astype(np.uint8)
    out = Image.fromarray(rgba, "RGBA")
    bbox = out.getbbox()
    if bbox:
        out = out.crop(bbox)
    out.save(dst)
    return out.size


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    hx = hex_color.lstrip("#")
    return tuple(int(hx[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def make_preview(rgba_path: Path, preview_path: Path, bg_hex: str) -> None:
    """在指定底色上合成预览，便于挑图时判断字是否隐形。"""
    im = Image.open(rgba_path).convert("RGBA")
    bg = Image.new("RGBA", im.size, _hex_to_rgb(bg_hex) + (255,))
    bg.alpha_composite(im)
    bg.convert("RGB").save(preview_path)


# ---------------- 布局读写 ----------------
def _cover_title(layout: dict) -> dict | None:
    """找第一页主标题 text：字号最大者优先（主标题通常远大于副标题/引言）。"""
    first = layout["pages"][0]
    texts = [e for e in first.get("elements", [])
             if e.get("type") == "text" and str(e.get("content", "")).strip()]
    if not texts:
        return None

    def rank(e: dict):
        size = float((e.get("font") or {}).get("size") or 0)
        content = str(e.get("content", ""))
        han = sum("\u4e00" <= c <= "\u9fff" for c in content)
        return (size, han, len(content))   # 字号优先，其次中文字数

    return max(texts, key=rank)


def cmd_generate(args) -> int:
    run = Path(args.run).resolve()
    lpath = run / "01_layout.json"
    if not lpath.exists():
        print("运行目录缺少 01_layout.json:", run)
        return 2
    layout = json.loads(lpath.read_text(encoding="utf-8"))
    t = _cover_title(layout)
    if not t:
        print("封面没有可用的 text 主标题，无法生成艺术字")
        return 2

    title = str(t["content"]).strip()
    color = str((t.get("font") or {}).get("color") or "#EDE6D6")
    # 深绿底用米白字；浅底标题若本身是深色，取原色即可
    print("封面主标题:", title)
    print("原字色:", color, "| 艺术字将重着色为此色")
    print("候选数:", args.n, "\n")

    out_dir = config.WORKSPACE / f"_title_art_{run.name}"
    cand_dir = out_dir / "candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)

    # 白底 + 深色书法字：白底便于抠图，深色笔画轮廓清晰
    prompt = (
        f"A premium Chinese calligraphy-art logotype of EXACTLY these characters: 「{title}」. "
        "Elegant deep forest-green ink brush strokes with strong slanted strokes and flying-white "
        "texture, refined artistic typography, centered, large, occupying the middle band of the "
        "image, on a PURE SOLID WHITE (#FFFFFF) flat background with even lighting, no shadows, "
        "no background gradient, no watermark, no frame, no other text. "
        f"The {len(title)} characters must be written 100% correct with complete strokes, no gibberish, "
        "no missing or extra characters."
    )

    llm = None if args.mock else LLMClient()
    manifest: list[dict] = []
    for i in range(args.n):
        raw = cand_dir / f"raw_{i + 1}.png"
        rgba = cand_dir / f"title_rgba_{i + 1}.png"
        prev = cand_dir / f"preview_{i + 1}.png"
        try:
            if llm:
                llm.image(prompt, raw)
            else:
                mock_image(raw, size=(1536, 1024), seed=20 + i)
            matte_and_recolor(raw, rgba, color)
            make_preview(rgba, prev, "#1F3A34")
            print(f"  候选 {i + 1}: OK -> {rgba.name}")
        except Exception as exc:  # noqa: BLE001
            print(f"  候选 {i + 1}: 失败({type(exc).__name__}): {str(exc)[:120]}")
            continue
        manifest.append({
            "asset_id": "cover_title_art",
            "path": str(rgba),
            "candidates": [str(rgba)],
            "preview": str(prev),
            "kind": "title_art",
            "source": "mock" if args.mock else "llm",
            "prompt": prompt,
            "similar_groups": [],
        })

    if not manifest:
        print("没有成功生成任何候选")
        return 1
    (out_dir / "02_assets").mkdir(parents=True, exist_ok=True)
    (out_dir / "02_assets" / "manifest.json").write_text(
        json.dumps({"images": manifest, "mask_slices": [], "decoration_svg": []},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    gallery = _write_title_gallery(out_dir, manifest)
    print("\n挑图页:", gallery)
    print("选中后: python make_title_art.py --apply", run, "--image <选中的 title_rgba_*.png>")
    return 0


def _write_title_gallery(root: Path, manifest: list[dict]) -> Path:
    cards = []
    for i, entry in enumerate(manifest):
        rgba = Path(entry["path"])
        prev = Path(entry["preview"])
        rel_a = rgba.relative_to(root).as_posix()
        rel_p = prev.relative_to(root).as_posix()
        cards.append(
            f'<section><h3>候选 {i + 1}</h3>'
            f'<div class="onbg"><img src="{html.escape(rel_p)}" alt="深绿底预览"/></div>'
            f'<div class="onwhite"><img src="{html.escape(rel_a)}" alt="透明原图"/></div>'
            f'<code>{html.escape(rel_a)}</code></section>'
        )
    page = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>封面主标题艺术字 · 挑图</title><style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;background:#f4f4f2;color:#222;margin:0;padding:24px}
h1{font-size:20px} .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:18px}
section{background:#fff;border:1px solid #ddd;border-radius:10px;padding:14px}
h3{margin:0 0 10px;font-size:15px}
.onbg{background:#1F3A34;border-radius:6px;padding:8px} .onwhite{background:#fff;border:1px dashed #ccc;border-radius:6px;padding:8px;margin-top:8px}
img{width:100%;height:auto;display:block;object-fit:contain}
code{display:block;margin-top:8px;font-size:11px;color:#666;word-break:break-all}
</style></head><body><h1>封面主标题艺术字 · 挑图（上：深绿封面底预览 / 下：透明原图）</h1>
<div class="grid">""" + "".join(cards) + "</div></body></html>"
    p = root / "pick_gallery.html"
    p.write_text(page, encoding="utf-8")
    return p


def cmd_apply(args) -> int:
    run = Path(args.apply).resolve()
    lpath = run / "01_layout.json"
    if not lpath.exists():
        print("运行目录缺少 01_layout.json:", run)
        return 2
    art = Path(args.image).resolve()
    if not art.exists():
        print("艺术字图片不存在:", art)
        return 2

    layout = json.loads(lpath.read_text(encoding="utf-8"))
    t = _cover_title(layout)
    if not t:
        print("封面没有主标题 text，无法替换")
        return 2
    first = layout["pages"][0]
    pos = dict(t["position"])

    # 等比放大到接近整页宽度（原标题框只有 1.4in 高会显得偏小），居中于原标题中心
    from services.cover_title_art import enlarged_art_position
    with Image.open(art) as im:
        aw, ah = im.size
    pos = enlarged_art_position(pos, aw, ah)

    # 只替换这一个元素：删掉主标题 text，插入 picture（其余原封不动）
    new_el = {
        "element_id": "p1_title_art",
        "type": "picture",
        "asset_id": TITLE_ASSET,
        "content": "",
        "position": pos,
        "layer": "text",
        "note": "封面主标题艺术字图片（其余封面元素保持原生可编辑）",
        "z": 6,
    }
    elements = first["elements"]
    idx = elements.index(t)
    elements[idx] = new_el
    print(f"已替换主标题「{str(t.get('content', ''))}」为艺术字图片")
    print("其余封面元素保留:", len(elements) - 1, "个")

    layout = normalize_layout(layout, brief={"vi": layout.get("vi", {})})
    (run / "01_layout_titleart.json").write_text(
        json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")

    # 复制艺术字图为素材并注册
    assets_dir = run / "02_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    saved = assets_dir / f"{TITLE_ASSET}.png"
    shutil.copy2(art, saved)
    assets = {TITLE_ASSET: str(saved)}
    mpath = assets_dir / "manifest.json"
    if mpath.exists():
        mani = json.loads(mpath.read_text(encoding="utf-8"))
        for e in mani.get("images", []):
            assets.setdefault(e["asset_id"], e["path"])
        for s in mani.get("mask_slices", []):
            for i, sp in enumerate(s.get("slices", [])):
                if Path(sp).exists():
                    assets[f"{s['asset_id']}__{i}"] = sp

    b = PPTBuilder()
    vi = layout.get("vi") or {}
    b._vi_primary = str(vi.get("primary") or "#0B2A4A")
    b._vi_secondary = str(vi.get("secondary") or "#C9A86A")
    for page in layout["pages"]:
        slide = b.prs.slides.add_slide(b.blank_layout)
        b.set_page_context(page)
        tk = PPTToolkit(b, slide, assets)
        tk.call("set_background", bg=page["bg"])
        for el in sorted(page["elements"], key=lambda e: e.get("z", 0)):
            tk.call_for_element(el)

    out = run / "deliverable_titleart.pptx"
    b.prs.save(str(out))
    rep = inspect(str(out), layout, attempt=1)
    print("结构自检:", rep["summary"], "| native_text_boxes=", rep["summary"].get("native_text_boxes"))
    shots = run / "04_screenshots_titleart"
    shots.mkdir(parents=True, exist_ok=True)
    rv = try_render(str(out), shots)
    print("真实渲染:", rv.get("status"), rv.get("page_count"))
    print("★ 交付:", out)
    return 0 if rep["summary"]["blocking_issues"] == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="封面主标题艺术字：只换标题那一行，其余保持原生可编辑")
    g = ap.add_argument_group("生成候选")
    g.add_argument("--run", default="", help="目标运行目录（读其封面主标题文字与颜色）")
    g.add_argument("--n", type=int, default=3, help="候选数")
    g.add_argument("--mock", action="store_true", help="离线占位图，验证流程用")
    a = ap.add_argument_group("应用挑图")
    a.add_argument("--apply", default="", help="目标运行目录")
    a.add_argument("--image", default="", help="选中的 title_rgba_*.png")
    args = ap.parse_args()
    if args.apply:
        if not args.image:
            print("--apply 需要同时给 --image")
            return 2
        return cmd_apply(args)
    if not args.run:
        ap.print_help()
        return 2
    return cmd_generate(args)


if __name__ == "__main__":
    raise SystemExit(main())
