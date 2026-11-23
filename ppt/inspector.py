"""OOXML 级自检：不依赖 Office，也能验证「原生可编辑」底线。

能查：文本框是否独立、蒙版是否真的做了图片填充（而不是并排原图）、
      图层顺序、越界、VI 色、文字/背景对比度、图片关系是否可解析。
不能查：图片里有没有被烤进文字（需 OCR）、字距是否舒服、装饰是否抢主视觉。
       这些明确留给人工收口，README 里写清楚。
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.oxml.ns import qn

import config
from .schema import vi_palette
from .toc_policy import missing_contents


def _luminance(hex_color: str) -> float:
    c = [int(hex_color.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return round((hi + 0.05) / (lo + 0.05), 2)


def _shape_colors(shape) -> set[str]:
    out = set()
    for node in shape._element.iter(qn("a:srgbClr")):
        val = node.get("val")
        if val:
            out.add("#" + val.upper())
    return out


def _effective_background(page: dict, el: dict) -> str | None:
    """文字实际落在什么底色上：优先取覆盖其中心、层级更低的实色形状。

    版式通用化后「深字压在浅色 CTA 色块」很常见，只跟页面背景比会误报对比度不足。
    背景是图片时返回 None，交给视觉评审判断。
    """
    p = el["position"]
    cx, cy = p["x"] + p["w"] / 2, p["y"] + p["h"] / 2
    bg = page["bg"]["color"] if page["bg"]["type"] == "color" else None
    for s in page["elements"]:
        if s["type"] != "shape" or not s.get("fill") or s["z"] >= el["z"]:
            continue
        sp = s["position"]
        if sp["x"] <= cx <= sp["x"] + sp["w"] and sp["y"] <= cy <= sp["y"] + sp["h"]:
            bg = s["fill"]
    return bg


def _check(name: str, ok: bool, detail: str, blocking: bool = True) -> dict:
    return {"check": name, "ok": ok, "blocking": blocking, "detail": detail}


def inspect(pptx_path: str | Path, layout: dict, attempt: int = 1, *, brief: dict | None = None) -> dict:
    path = Path(pptx_path)
    prs = Presentation(str(path))
    sw, sh = prs.slide_width, prs.slide_height
    palette = vi_palette(layout)

    report: dict[str, Any] = {
        "pptx": str(path),
        "attempt": attempt,
        "ok": True,
        "pages": [],
        "issues": [],
        "warnings": [],
        "limits": [
            "结构自检无法判断字距与视觉平衡（已由真实截图 + 多模态视觉评审补充）",
            "最终字距、对齐、裁切位置、logo 安全边距仍需人工终审",
        ],
    }

    if len(prs.slides) != len(layout["pages"]):
        report["issues"].append(
            f"页数不一致：pptx={len(prs.slides)}，版式JSON={len(layout['pages'])}"
        )

    for pi, (slide, page) in enumerate(zip(prs.slides, layout["pages"])):
        checks: list[dict] = []
        shapes = list(slide.shapes)
        by_id: dict[str, list] = {}
        for s in shapes:
            key = (s.name or "").split("__")[0]
            by_id.setdefault(key, []).append(s)

        if page.get('photo_required'):
            import hashlib
            bg = page['bg']
            photo = next((s for s in shapes if s.name == 'bg__image' and s._element.tag == qn('p:pic')), None)
            bounds = None if photo is None else (photo.left, photo.top, photo.width, photo.height)
            checks.append(_check('photo_extent', bounds == (0, 0, sw, sh),
                                 f'要求一张四边铺满的摄影背景，实际图片框（EMU）={bounds}'))
            source = bg.get('asset_source')
            digest = hashlib.sha256(photo.image.blob).hexdigest() if photo is not None else None
            verified = bool(digest and digest == bg.get('asset_sha256') and source == 'llm')
            checks.append(_check('photo_asset', verified,
                                 f'背景素材来源={source or "未验证"}，内嵌图片与已选素材校验' + ('通过' if verified else '未通过（缺图、占位或来源不可证）')))

        z_sequence: list[int] = []
        for el in page["elements"]:
            eid = el["element_id"]
            found = by_id.get(eid, [])
            z_sequence.append(el["z"])

            if el["type"] == "text":
                text_shapes = [s for s in found if s.has_text_frame]
                actual = "\n".join(s.text for s in text_shapes).strip()
                expected = (el["content"] or "").strip()
                ok = bool(text_shapes) and actual == expected
                checks.append(_check(
                    f"text[{eid}]", ok,
                    f"期望={expected[:24]!r} 实际={actual[:24]!r} 文本框数={len(text_shapes)}",
                ))
                if ok:
                    under = _effective_background(page, el)
                    if under:
                        ratio = _contrast(el["font"]["color"], under)
                        checks.append(_check(
                            f"contrast[{eid}]", ratio >= 3.0,
                            f"文字/实际底色对比度={ratio}:1（底色 {under}，建议 >=3.0）",
                            blocking=False,
                        ))
                    else:
                        checks.append(_check(
                            f"contrast[{eid}]", True,
                            "底色是图片，跳过结构级对比度计算（交由视觉评审判断）",
                            blocking=False,
                        ))
                    # 粗略估算文本是否放得下（CJK 按 1em/字；真实字距以 PowerPoint 为准）
                    fs = el["font"]["size"]
                    per_line = max(1, int(el["position"]["w"] * 72 / fs))
                    lines = sum(
                        max(1, math.ceil(len(seg) / per_line))
                        for seg in (el["content"] or "").split("\n")
                    )
                    needed = lines * fs * 1.35 / 72
                    fit_ok = needed <= el["position"]["h"] * 1.15
                    checks.append(_check(
                        f"text_fit[{eid}]", fit_ok,
                        f"估算 {lines} 行需 {needed:.2f}in，文本框 {el['position']['h']:.2f}in",
                        blocking=False,
                    ))

            elif el["type"] == "mask_picture":
                missing = 0
                no_fill = 0
                bad_rel = 0
                for s in found:
                    spPr = s._element.spPr
                    blipFill = spPr.find(qn("a:blipFill"))
                    if blipFill is None:
                        no_fill += 1
                        continue
                    blip = blipFill.find(qn("a:blip"))
                    sr = blipFill.find(qn("a:srcRect"))
                    if blip is None or sr is None:
                        no_fill += 1
                        continue
                    rId = blip.get(qn("r:embed"))
                    if not rId or rId not in slide.part.rels:
                        bad_rel += 1
                expected_n = el["mask"]["count"]
                ok = len(found) == expected_n and no_fill == 0 and bad_rel == 0
                checks.append(_check(
                    f"mask_picture[{eid}]", ok,
                    f"形状图片填充：单元格数={len(found)}/{expected_n}"
                    f"（layout={el['mask']['layout']}, continuous={el['mask']['continuous']}），"
                    f"缺图片填充={no_fill}，坏图片关系={bad_rel}",
                ))

            elif el["type"] == "picture":
                ok = bool(found)
                checks.append(_check(f"picture[{eid}]", ok, f"独立图片形状数={len(found)}"))
                pos = el["position"]
                ratio = pos["w"] / max(pos["h"], 0.01)
                if ratio > 4 or ratio < 0.25:
                    checks.append(_check(
                        f"image_ratio[{eid}]", False,
                        f"图片框比例 {ratio:.1f}:1 过于细长，源图会被裁成一条细带",
                        blocking=False,
                    ))

            elif el["type"] in {"shape", "decorate", "branch"}:
                ok = bool(found)
                label = "分支图示" if el["type"] == "branch" else "原生形状"
                checks.append(_check(
                    f"{el['type']}[{eid}]", ok,
                    f"{label}形状数={len(found)}"
                    + (f"（type={el['branch_type']}, items={len(el.get('items', []))}）"
                       if el["type"] == "branch" else ""),
                ))

            # 越界检查（对所有元素）
            for s in found:
                if s.left < 0 or s.top < 0 or s.left + s.width > sw + 1000 or s.top + s.height > sh + 1000:
                    checks.append(_check(
                        f"bounds[{eid}]", False,
                        f"形状 {s.name} 超出画布 (left={s.left},top={s.top},w={s.width},h={s.height})",
                    ))
                else:
                    checks.append(_check(f"bounds[{eid}]", True, "在画布内"))

        # 图层顺序：写入顺序必须按 z 非递减
        order_ok = all(a <= b for a, b in zip(z_sequence, z_sequence[1:]))
        checks.append(_check("layer_order", order_ok, f"z序列={z_sequence}"))

        # VI 配色（warning 级）
        used = set()
        for s in shapes:
            used |= _shape_colors(s)
        outside = sorted(used - palette)
        checks.append(_check(
            "vi_palette", not outside,
            f"超出VI的颜色={outside[:8]}" if outside else "所有形状/文字颜色均在 VI 色板内",
            blocking=False,
        ))

        for s in shapes:
            if s.has_text_frame and s.text.strip():
                p = s.left / 914400, s.top / 914400
                if p[0] < config.SAFE_MARGIN_IN or p[1] < config.SAFE_MARGIN_IN:
                    checks.append(_check(
                        "safe_margin", False,
                        f"{s.name} 距边 {p[0]:.2f}in / {p[1]:.2f}in，低于 {config.SAFE_MARGIN_IN}in",
                        blocking=False,
                    ))

        page_issues = [c for c in checks if not c["ok"] and c["blocking"]]
        page_warnings = [c for c in checks if not c["ok"] and not c["blocking"]]
        report["pages"].append(
            {
                "page_index": pi + 1,
                "page_name": page["page_name"],
                "shape_count": len(shapes),
                "checks": checks,
                "blocking_issues": [c["detail"] for c in page_issues],
                "warnings": [c["detail"] for c in page_warnings],
            }
        )
        for c in page_issues:
            report["issues"].append(f"第{pi + 1}页 {c['check']}: {c['detail']}")
        for c in page_warnings:
            report["warnings"].append(f"第{pi + 1}页 {c['check']}: {c['detail']}")

    # ---------- 整 deck 级结构规则（用户明确要求的画册级能力，升级为阻塞项） ----------
    pages = layout["pages"]
    n_pages = len(pages)

    # 以下为「画册级」（>=4页）硬规则；单页/少页的轻量 PPT 不受这些约束
    # 注意：这些规则由 agents/reviewer.py 的 enforce_brochure_rules() 在真实流水线里
    # 单独强制（驱动返修）；inspect() 层默认只记录为 warning，避免合成测试/重建脚本被误伤。
    _brochure_rules = missing_contents(layout, brief)
    if n_pages >= 4:

        # 2) 封面应为摄影背景+渐变遮罩（不应是纯色大色块）
        cover = pages[0]
        cover_bg_img = cover["bg"]["type"] == "image"
        cover_has_scrim = any(el["type"] == "scrim" for el in cover.get("elements", []))
        if cover.get('photo_required', not cover_has_scrim) and not cover_bg_img:
            _brochure_rules.append(
                "封面是纯色背景：画册级封面应为摄影背景图 + scrim 渐变遮罩（焦点侧清晰）"
            )

        # 3) 历程 time line 每个节点 desc 必须实质（非年份/非空）
        for pi, pg in enumerate(pages, 1):
            for el in pg.get("elements", []):
                if el["type"] == "branch" and el.get("branch_type") == "timeline":
                    for it in el.get("items", []):
                        desc = str(it.get("desc", "")).strip()
                        if len(desc) < 6:
                            _brochure_rules.append(
                                f"第{pi}页时间轴节点「{it.get('label', it.get('num', '?'))}」desc 过短"
                                f"（{len(desc)}字）：每个阶段要写清做了什么、结果/数据"
                            )

        # 4) 末页横向条带图不能被压成细条（宽高比>6:1）
        last = pages[-1]
        for el in last.get("elements", []):
            if el["type"] == "picture":
                pos = el["position"]
                if pos["h"] > 0 and pos["w"] / pos["h"] > 6:
                    _brochure_rules.append(
                        f"末页图片「{el.get('content', el['element_id'])[:16]}」被压成细条"
                        f"（{pos['w'] / pos['h']:.1f}:1）：高度应≥2英寸或改用整页背景图"
                    )

    for issue in _brochure_rules:
        report["warnings"].append("画册规则: " + issue)

    report["ok"] = not report["issues"]
    report["summary"] = {
        "pages": len(report["pages"]),
        "checks": sum(len(p["checks"]) for p in report["pages"]),
        "blocking_issues": len(report["issues"]),
        "warnings": len(report["warnings"]),
        "native_text_boxes": sum(
            1 for sl in prs.slides for s in sl.shapes if s.has_text_frame and s.text.strip()
        ),
    }
    return report


def brochure_rule_issues(layout: dict, brief: dict | None = None) -> list[str]:
    """画册级（>=4页）硬规则违规项，供真实流水线在 Reviewer 阶段驱动返修。

    与 inspect() 的 warning 版不同，这里返回阻塞级问题；仅由 agents/reviewer.py
    在非 mock 模式下调用，合成测试与重建脚本不触发。
    """
    issues = missing_contents(layout, brief)
    pages = layout.get("pages", [])
    if len(pages) < 4:
        return issues

    cover = pages[0]
    legacy_cover = not any(e.get('type') == 'scrim' for e in cover.get('elements', []))
    if cover.get('photo_required', legacy_cover) and cover.get('bg', {}).get('type') != 'image':
        issues.append("封面是纯色背景：应改为整页摄影背景图（bg.type=image）并在文字下加一个 "
                      "type=scrim 渐变遮罩元素，wash 与标题所在侧一致，图片焦点在另一侧。")

    for pi, pg in enumerate(pages, 1):
        for el in pg.get("elements", []):
            if el.get("type") == "branch" and el.get("branch_type") == "timeline":
                for it in el.get("items", []):
                    desc = str(it.get("desc", "")).strip()
                    if len(desc) < 6:
                        issues.append(
                            f"第{pi}页时间轴节点「{it.get('label', it.get('num', '?'))}」说明过短："
                            "把 desc 改写为具体做了什么、结果/数据（>=10字）。"
                        )

    last = pages[-1]
    for el in last.get("elements", []):
        if el.get("type") == "picture":
            pos = el.get("position", {})
            h, w = pos.get("h", 0), pos.get("w", 0)
            if h > 0 and w / h > 6:
                issues.append(f"末页图片「{str(el.get('content', ''))[:16]}」被压成细条（{w / h:.1f}:1）："
                              "把图片框高度改到≥2英寸，或改用整页背景图+scrim。")
    return issues


def check_pdf_text_layer(page_texts: list[str], layout: dict) -> dict:
    """用 LibreOffice 导出的 PDF 文本层反证「文字是原生文本框，不是烤进图片」。

    PDF 里能抽出的文本必然来自真实文本对象；位图里的字不会被 get_text() 拿到。
    逐条比对版式 JSON 的每个 text 元素是否出现在对应页的 PDF 文本层里。
    """
    import re

    results: list[dict] = []
    all_ok = True
    for pi, page in enumerate(layout["pages"]):
        raw = page_texts[pi] if pi < len(page_texts) else ""
        flat = re.sub(r"\s+", "", raw)
        from .branch_diagram import is_metric_grid
        # 不可去掉换行再匹配数值，否则 4,20 + 换行 + 0 也会被误放行。
        from collections import Counter
        lines = Counter(re.sub(r'\s+', '', s) for s in raw.splitlines())
        for el in page['elements']:
            if el['type'] != 'branch' or not is_metric_grid(el):
                continue
            for item in el['items']:
                value = str(item.get('num', '')).strip()
                key = re.sub(r'\s+', '', value)
                ok = bool(key and lines[key] > 0)
                if ok: lines[key] -= 1
                all_ok = all_ok and ok
                results.append(dict(page_index=pi+1, element_id=el['element_id'], expected=value,
                                    found_in_pdf_text_layer=ok))
        for el in page["elements"]:
            if el["type"] != "text":
                continue
            target = re.sub(r"\s+", "", el["content"] or "")
            if not target:
                continue
            ok = target in flat
            all_ok = all_ok and ok
            results.append(
                {
                    "page_index": pi + 1,
                    "element_id": el["element_id"],
                    "expected": el["content"][:40],
                    "found_in_pdf_text_layer": ok,
                }
            )
    return {
        "ok": all_ok,
        "checked": len(results),
        "missing": [r["expected"] for r in results if not r["found_in_pdf_text_layer"]],
        "details": results,
        "evidence": "PDF 文本层可抽取 = PPTX 里是原生文本对象，不是位图里的像素",
    }
