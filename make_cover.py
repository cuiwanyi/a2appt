"""封面艺术字生成 + 人工挑图（独立可选命令，不改主流水线）。

封面艺术字是「整张设计位图、不可双击改字」，与主流水线「全部原生可编辑」的底线不同，
所以做成独立命令：先在候选里挑一张满意的封面，再按需要使用。

用法：
    # 1) 生成候选封面（N 张，默认 3），出挑图页
    python make_cover.py --title "以专业培训，回应真实业务挑战" --brand "教育培训有限公司" \
        --subtitle "产品宣传画册" --style 3d_gradient --n 3

    # 2) 打开 workspace/_cover_<...>/pick_gallery.html 挑一张，记下文件名

    # 3) 把选中的封面铺进某次运行的第一页（作为整页背景图，封面文字已在图里）
    python make_cover.py --apply workspace/<run_id> --image workspace/_cover_<...>/candidates/c2.png

步骤 3 会把封面页 bg 改成这张图、移除封面页原生 text 元素（因为字已在艺术字图里），
重建 deliverable_cover.pptx，其余页保持不变。

注意：生成候选需要出网调用生图模型；被沙箱拦截时会降级为本地占位图并标注。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from services.cover_art import cover_prompt, STYLE_LABELS
from services.llm import LLMClient
from services.mock_data import mock_image
from services.asset_candidates import write_gallery
from ppt.schema import normalize_layout
from ppt.builder import PPTBuilder
from ppt.tools import PPTToolkit
from ppt.inspector import inspect
from ppt.render import try_render

def cmd_generate(args) -> int:
    safe = "".join(c if c.isalnum() else "_" for c in args.title)[:24] or "cover"
    out_dir = config.WORKSPACE / f"_cover_{safe}"
    cand_dir = out_dir / "candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)

    prompt = cover_prompt(args.title, args.subtitle, args.brand, args.style)
    print("风格:", STYLE_LABELS.get(args.style, args.style))
    print("标题:", args.title)
    print("候选数:", args.n, "| 提示词已锁定标题逐字正确\n")

    llm = None if args.mock else LLMClient()
    cands: list[str] = []
    for i in range(args.n):
        target = cand_dir / f"c{i + 1}.png"
        if llm:
            try:
                llm.image(prompt, target)
                print(f"  候选 {i + 1}: OK")
            except Exception as exc:  # noqa: BLE001
                print(f"  候选 {i + 1}: 生图失败({type(exc).__name__})，降级占位图")
                mock_image(target, size=(1536, 1024), seed=7 + i)
        else:
            mock_image(target, size=(1536, 1024), seed=7 + i)
            print(f"  候选 {i + 1}: mock 占位图")
        cands.append(str(target))

    manifest = [{"asset_id": "cover_art", "path": cands[0], "prompt": prompt,
                 "kind": "cover", "source": "mock" if args.mock else "llm",
                 "candidates": cands, "similar_groups": []}]
    (out_dir / "02_assets").mkdir(parents=True, exist_ok=True)
    (out_dir / "02_assets" / "manifest.json").write_text(
        json.dumps({"images": manifest, "mask_slices": [], "decoration_svg": []},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    gallery = write_gallery(out_dir, manifest)
    print("\n挑图页:", gallery)
    print("选中后运行: python make_cover.py --apply <运行目录> --image <选中的候选.png>")
    return 0


def cmd_apply(args) -> int:
    run = Path(args.apply).resolve()
    lpath = run / "01_layout.json"
    if not lpath.exists():
        print("运行目录缺少 01_layout.json:", run)
        return 2
    cover_img = Path(args.image).resolve()
    if not cover_img.exists():
        print("封面图不存在:", cover_img)
        return 2

    layout = json.loads(lpath.read_text(encoding="utf-8"))
    # 封面图作为新素材，铺满第一页；封面文字已在艺术字图里，移除第一页原生 text
    cover_aid = "cover_art"
    assets_dir = run / "02_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    saved = assets_dir / f"{cover_aid}.png"
    shutil.copy2(cover_img, saved)

    first = layout["pages"][0]
    first["bg"] = {"type": "image", "image": cover_aid, "color": None}
    # 艺术字封面是「整张设计图」，自带标题/图形/背景；原来封面的 text/色块/装饰
    # 若保留会盖在图上（尤其全页深绿色块），所以第一页元素全部清空。
    removed = [e["element_id"] for e in first.get("elements", [])]
    first["elements"] = []
    print("封面页已改为整页艺术字图；移除原封面元素:", removed)

    # 重新 normalize（补 ID/图层），再构建
    layout = normalize_layout(layout, brief={"vi": layout.get("vi", {})})
    (run / "01_layout_cover.json").write_text(
        json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")

    b = PPTBuilder()
    vi = layout.get("vi") or {}
    b._vi_primary = str(vi.get("primary") or "#0B2A4A")
    b._vi_secondary = str(vi.get("secondary") or "#C9A86A")
    assets = {cover_aid: str(saved)}
    mpath = assets_dir / "manifest.json"
    if mpath.exists():
        mani = json.loads(mpath.read_text(encoding="utf-8"))
        for e in mani.get("images", []):
            assets.setdefault(e["asset_id"], e["path"])
        # 蒙版预裁切片：mask_picture 按 <aid>__<i> 取图，必须一并注册，否则缺图片填充
        for s in mani.get("mask_slices", []):
            for i, sp in enumerate(s.get("slices", [])):
                if Path(sp).exists():
                    assets[f"{s['asset_id']}__{i}"] = sp
    for page in layout["pages"]:
        slide = b.prs.slides.add_slide(b.blank_layout)
        tk = PPTToolkit(b, slide, assets)
        tk.call("set_background", bg=page["bg"])
        for el in sorted(page["elements"], key=lambda e: e.get("z", 0)):
            tk.call_for_element(el)

    out = run / "deliverable_cover.pptx"
    b.prs.save(str(out))
    rep = inspect(str(out), layout, attempt=1)
    print("结构自检:", rep["summary"])
    if rep["issues"][:5]:
        print("  问题:", rep["issues"][:5])
    shots = run / "04_screenshots_cover"
    shots.mkdir(parents=True, exist_ok=True)
    rv = try_render(str(out), shots)
    print("真实渲染:", rv.get("status"), rv.get("page_count"))
    print("★ 交付:", out)
    print("  截图:", shots)
    return 0 if rep["summary"]["blocking_issues"] == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="专用艺术字命令：生成整页位图封面，标题不可双击编辑")
    g = ap.add_argument_group("生成候选")
    g.add_argument("--title", default="", help="封面主标题（逐字写入艺术字）")
    g.add_argument("--subtitle", default="", help="副标题/信息层级")
    g.add_argument("--brand", default="", help="公司/机构名")
    g.add_argument("--style", default="3d_gradient",
                   choices=tuple(STYLE_LABELS))
    g.add_argument("--n", type=int, default=3, help="候选数")
    g.add_argument("--mock", action="store_true", help="离线占位图，验证流程用")
    a = ap.add_argument_group("应用挑图")
    a.add_argument("--apply", default="", help="目标运行目录")
    a.add_argument("--image", default="", help="选中的封面候选图路径")
    args = ap.parse_args()

    if args.apply:
        if not args.image:
            print("--apply 需要同时给 --image")
            return 2
        return cmd_apply(args)
    if not args.title:
        ap.print_help()
        return 2
    return cmd_generate(args)


if __name__ == "__main__":
    raise SystemExit(main())
