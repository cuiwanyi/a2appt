"""生图候选（best-of-N）+ 人工挑图检查点。

设计原则：
- 默认 IMAGE_CANDIDATES=1，链路与原来完全一致，不破坏任何既有行为。
- 开启候选后，AssetAgent 为每个 asset_id 生成 N 张图，落到 02_assets/candidates/<aid>/。
- 生成 pick_gallery.html：一个本地可打开的挑图页，每张候选可点选；
  选中结果写入 02_assets/picks.json（asset_id -> 候选文件名）。
- apply_picks() 读 picks.json，把选中的候选复制回 02_assets/<aid>.png，
  重建蒙版切片，返回更新后的 assets 映射，供重新构建 PPTX。
- 去重：用感知哈希（dHash）算候选间汉明距离，过于相似的候选在联系表里标注，
  辅助用户判断"是否重复"，但不自动删除（最终决定权在人）。
"""
from __future__ import annotations

import hashlib
import html
import json
import shutil
from pathlib import Path

from PIL import Image


def dhash(path: str | Path, hash_size: int = 16) -> int | None:
    """感知哈希：返回 hash_size^2 位整数；读不了返回 None。"""
    try:
        im = Image.open(path).convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    except Exception:  # noqa: BLE001
        return None
    px = list(im.getdata())
    bits = 0
    for row in range(hash_size):
        for col in range(hash_size):
            a = px[row * (hash_size + 1) + col]
            b = px[row * (hash_size + 1) + col + 1]
            bits = (bits << 1) | (1 if a > b else 0)
    return bits


def hamming(a: int | None, b: int | None) -> int | None:
    if a is None or b is None:
        return None
    return bin(a ^ b).count("1")


def similarity_groups(paths: list[str], threshold: int = 6) -> list[list[int]]:
    """返回相似度过高（汉明距离 <= threshold）的候选索引分组。"""
    hashes = [dhash(p) for p in paths]
    groups: list[list[int]] = []
    seen: set[int] = set()
    total = len(paths)
    for i in range(total):
        if i in seen or hashes[i] is None:
            continue
        grp = [i]
        for j in range(i + 1, total):
            if j in seen or hashes[j] is None:
                continue
            d = hamming(hashes[i], hashes[j])
            if d is not None and d <= threshold:
                grp.append(j)
                seen.add(j)
        if len(grp) > 1:
            seen.add(i)
            groups.append(grp)
    return groups


def write_gallery(root: Path, manifest: list[dict]) -> Path:
    """生成可本地打开的挑图联系表 HTML + 初始 picks.json。"""
    cand_root = root / "02_assets" / "candidates"
    cand_root.mkdir(parents=True, exist_ok=True)
    picks_path = root / "02_assets" / "picks.json"

    # 已有 picks 则保留用户选择，否则默认全选第 1 张
    if picks_path.exists():
        try:
            picks = json.loads(picks_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            picks = {}
    else:
        picks = {}

    cards: list[str] = []
    for entry in manifest:
        aid = entry["asset_id"]
        cands = entry.get("candidates") or []
        if not cands:
            continue
        dups = entry.get("similar_groups", [])
        dup_set = {idx for grp in dups for idx in grp}
        current = picks.get(aid, Path(cands[0]).name)
        tiles: list[str] = []
        for i, cpath in enumerate(cands):
            name = Path(cpath).name
            tag = " · 疑似重复" if i in dup_set else ""
            checked = " checked" if name == current else ""
            rel = Path(cpath).relative_to(root).as_posix()
            tiles.append(
                f'<label class="tile"><img src="{html.escape(rel)}" alt="{html.escape(name)}"/>'
                f'<span class="cap"><input type="radio" name="{html.escape(aid)}" value="{html.escape(name)}"{checked}/>'
                f'{i + 1}{html.escape(tag)}</span></label>'
            )
        cards.append(
            f'<section><h3>{html.escape(aid)} <small>{html.escape(entry.get("kind", ""))}</small></h3>'
            f'<div class="row">{"".join(tiles)}</div></section>'
        )

    page = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>挑图检查点 · 生图候选</title><style>
body{font-family:"Microsoft YaHei",sans-serif;background:#f5f3ee;color:#243029;margin:24px;max-width:1200px}
h1{font-size:22px} h3{font-size:16px;margin:22px 0 8px} h3 small{color:#888;font-weight:400}
section{border-top:1px solid #ddd;padding-top:6px} .row{display:flex;gap:12px;flex-wrap:wrap}
.tile{border:2px solid transparent;border-radius:8px;padding:4px;background:#fff;cursor:pointer}
.tile:has(input:checked){border-color:#C97B4A} .tile img{width:220px;height:150px;object-fit:cover;display:block}
.cap{display:block;font-size:13px;padding:4px 2px} .cap input{vertical-align:middle}
button{margin-top:20px;padding:10px 20px;font-size:15px;background:#1F3A34;color:#fff;border:0;border-radius:6px;cursor:pointer}
p.note{color:#6b6b6b;font-size:13px} textarea{width:100%;height:80px;font-family:monospace;font-size:12px}
</style></head><body><h1>生图候选 · 人工挑图检查点</h1>
<p class="note">每个素材选一张 → 点「生成 picks.json」→ 复制下方文本，覆盖保存到
<code>02_assets/picks.json</code> → 运行 <code>python pick_assets.py --run &lt;本次运行目录&gt;</code> 重建 PPTX。
标注「疑似重复」的是感知哈希判定过于相似的候选，建议避开。</p>
__CARDS__
<button id="gen">生成 picks.json</button><textarea id="out" readonly></textarea>
<script>
document.getElementById('gen').onclick=function(){
  var p={};document.querySelectorAll('input[type=radio]:checked').forEach(function(r){p[r.name]=r.value;});
  document.getElementById('out').value=JSON.stringify(p,null,2);
};
</script></body></html>"""
    gpath = root / "pick_gallery.html"
    gpath.write_text(page.replace("__CARDS__", "\n".join(cards)), encoding="utf-8")

    if not picks_path.exists():
        picks_path.write_text(json.dumps(picks, ensure_ascii=False, indent=2), encoding="utf-8")
    return gpath


def apply_picks(root: Path) -> dict:
    """应用挑图结果：把选中候选复制成正式素材，重建蒙版切片。返回统计。"""
    mpath = root / "02_assets" / "manifest.json"
    ppath = root / "02_assets" / "picks.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    picks = json.loads(ppath.read_text(encoding="utf-8")) if ppath.exists() else {}
    applied: list[str] = []
    missing: list[str] = []

    for entry in manifest["images"]:
        aid = entry["asset_id"]
        cands = entry.get("candidates") or []
        if not cands:
            continue
        chosen = picks.get(aid)
        if not chosen:
            continue
        src = next((c for c in cands if Path(c).name == chosen), None)
        if not src or not Path(src).exists():
            missing.append(aid)
            continue
        dst = root / "02_assets" / f"{aid}.png"
        shutil.copy2(src, dst)
        entry["path"] = str(dst)
        entry["picked"] = chosen
        entry["source"] = entry.get("candidate_sources", {}).get(chosen, "picked-unknown")
        entry["sha256"] = hashlib.sha256(dst.read_bytes()).hexdigest()
        applied.append(aid)

    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # 重建蒙版切片（用挑过的图重新预裁）
    from ppt.schema import asset_requests  # noqa: F401  (保持依赖可见)
    slices_rebuilt = 0
    assets = {e["asset_id"]: e["path"] for e in manifest["images"]}
    layout_p = root / "01_layout.json"
    if layout_p.exists():
        from ppt.slicing import prepare_mask_slices
        layout = json.loads(layout_p.read_text(encoding="utf-8"))
        for page in layout.get("pages", []):
            for el in page["elements"]:
                if el["type"] != "mask_picture":
                    continue
                paths = prepare_mask_slices(el, assets, root / "02_assets" / "slices")
                for i, sp in enumerate(paths):
                    if sp:
                        assets[f"{el['asset_id']}__{i}"] = sp
                slices_rebuilt += len([p for p in paths if p])

    return {"applied": applied, "missing": missing, "slices_rebuilt": slices_rebuilt,
            "assets": assets}
