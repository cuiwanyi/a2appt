"""目录版式原语：编号 + 章节名 + 一句话说明，全部原生文本框，双击可改。

配合 bg.image + scrim（渐变遮罩洗一侧）使用：目录整体放在被洗白的一侧，
图片焦点在另一侧保持清晰。版式参考企业画册的编号目录页。
"""
from __future__ import annotations

from pptx.enum.text import MSO_ANCHOR

from .branch_diagram import _text, _set_line
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches

_ALIGN = {"left": "left", "center": "center", "right": "right"}


def reconcile_contents(layout, specs=()):
    """Bind the opening contents to the assembled deck, never to a suggested quota."""
    import re
    import unicodedata

    def key(value):
        return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())

    pages = layout.get('pages', [])
    if len(pages) < 3:
        return []
    notes = []
    page = pages[1]

    # 模型或本地兜底可能把目录写成一只文本框；没有 toc 原语就没有列数和目标页可言。
    if not any(e['type'] == 'toc' for e in page['elements']):
        for i, el in enumerate(page['elements']):
            if el['type'] != 'text':
                continue
            rows = [line.strip() for line in str(el.get('content', '')).splitlines()
                    if re.match(r'^\d{2}\s+\S', line.strip())]
            if len(rows) < 2:
                continue
            items = []
            for row in rows:
                m = re.match(r'^(\d{2})\s+([^｜|]+?)(?:[｜|]\s*(.*))?$', row)
                if m:
                    items.append({'num': m[1], 'label': m[2].strip(), 'desc': (m[3] or '').strip()})
            if len(items) < 2:
                continue
            wash = next((e.get('wash') for e in page['elements'] if e['type'] == 'scrim'), '')
            page['elements'][i] = {
                'type': 'toc', 'element_id': el.get('element_id', 'p2_index').replace('_text', '_toc'),
                'title': '目录', 'cols': 2, 'items': items[:12], 'position': dict(el['position']),
                'layer': 'text', 'z': el.get('z', 5), 'align': el.get('align', 'left'), 'wash': wash,
            }
            notes.append({'toc_page': 2, 'converted_from_text': True})
            break

    for toc in [e for e in page['elements'] if e['type'] == 'toc']:
        old = toc.get('items', [])
        unused = list(range(len(old)))
        targets, mapping = [], {}
        page_numbers = bool(old and str(old[0].get('num', '')).lstrip('0') == '3')
        for i, content_page in enumerate(pages[2:], start=2):
            name = content_page.get('page_name', '').strip()
            spec = specs[i] if i < len(specs) else {}
            texts = [e for e in content_page.get('elements', []) if e['type'] == 'text' and e.get('content', '').strip()]
            heading = max(texts, key=lambda e: e.get('font', {}).get('size', 0), default={}).get('content', '').strip()
            written = {key(e['content']) for e in texts}
            proposed = spec.get('copy', {}).get('title', '')
            aliases = {key(name), key(heading)} - {''}
            if key(proposed) in written:
                aliases.add(key(proposed))
            match = next((j for j in unused if key(old[j]['label']) in aliases), None)
            num = i + 1 if page_numbers else i - 1
            if match is not None:
                unused.remove(match)
                item = dict(old[match]); mapping[match + 1] = num
            else:
                label = name if name and not re.fullmatch(r'第\s*\d+\s*页', name) else (heading or name or f'第{i+1}页')
                item = {'label': label, 'desc': ''}
            item.update(num=f'{num:02d}', target_page=i + 1)
            targets.append(item)
        if [i['label'] for i in old] != [i['label'] for i in targets]:
            notes.append({'toc_page': 2, 'removed': [old[j]['label'] for j in unused],
                          'resolved': [i['label'] for i in targets]})
        # 分组标注必须跟随有效项；原稿范围可能超出实际保留下来的章节数。
        # 只改独立的“标签 + 两位范围”文本，不碰含斜杠的页码或正文。
        for e in list(page['elements']):
            if e['type'] != 'text' or not e.get('content'):
                continue
            changed, kept = False, []
            for line in str(e['content']).splitlines():
                out = []
                for part in re.split(r'[\u3000]+', line):
                    m = re.fullmatch(r'([^\d\n]{1,30}?)\s+(\d{2})\s*[—–-]\s*(\d{2})', part.strip())
                    if not m:
                        out.append(part)
                        continue
                    start, end = int(m[2]), int(m[3])
                    group = [mapping.get(k) for k in range(start, min(end, len(old)) + 1)] if start <= len(old) else []
                    if not group or any(v is None for v in group) or sorted(group) != list(range(min(group), max(group) + 1)):
                        changed = True
                    else:
                        new = f'{m[1]}  {min(group):02d} — {max(group):02d}'
                        out.append(new)
                        changed = changed or new != part
                kept.append('\u3000'.join(out))
            if not changed:
                continue
            content = '\n'.join(kept)
            if content.strip():
                e['content'] = content
            else:
                page['elements'].remove(e)
        # 明确写出的列数优先；行数按最终项数推导，不能沿用原稿的 4 行空格。
        toc_spec = specs[1] if len(specs) > 1 else {}
        request = str(toc_spec.get('creative', '')) + str(toc_spec.get('style', ''))
        digits = {'两': 2, '三': 3, '四': 4, '五': 5, '六': 6}
        match = re.search(r'(两|三|四|五|六|\d)\s*列', request)
        if match:
            want = digits.get(match[1]) or int(match[1])
            if 2 <= want <= len(targets):
                toc['cols'] = want
                toc['preserve_cols'] = True
        toc['items'] = targets
    return notes


def draw_toc(slide, el: dict, palette: dict, on_dark: bool = False,
             page_wash: str = "", show_title: bool = True) -> list:
    before = len(slide.shapes)
    p = el["position"]
    x, y, w, h = p["x"], p["y"], p["w"], p["h"]
    items = el.get("items") or []
    cols = el.get("cols", 2)
    wash = (el.get("wash") or page_wash or "").lower()
    title = el.get("title") or "目录"

    # 若本页有渐变遮罩（摄影背景焦点在另一侧），把目录收进被洗白的半侧，
    # 避免右半目录压在图片焦点上导致文字与图重叠/隐形。
    # 明确要求的多列在洗白侧内部分列；未明确时仍收成单列，避免压住摄影焦点。
    preserve = bool(el.get("preserve_cols")) and cols > 1
    if el.get("measured_position"):
        pass  # 使用实图阶段已定位的整体目录框，不再次夹进硬编码半幅
    elif wash in {"left", "right"}:
        half = 5.6
        if not preserve:
            cols = 1
        if wash == "left":
            x, w = max(x, 0.7), min(w, half)
        else:
            x = max(x, 13.333 - half - 0.7)
            w = min(w, half)
    elif wash in {"bottomleft", "topleft"}:
        if not preserve:
            cols = 1
        x, w = max(x, 0.7), min(w, 5.8)
    elif wash in {"bottomright", "topright"}:
        if not preserve:
            cols = 1
        x, w = max(x, 6.9), min(w, 5.8)
    brand = palette.get("primary", "#0B2A4A")
    accent = palette.get("secondary", "#C9A86A")
    # Some palettes call the paper color 'primary'; don't use pale ink on pale paper.
    rgb = bytes.fromhex(brand.lstrip("#"))
    dark_brand = sum(c * k for c, k in zip(rgb, (.299, .587, .114))) < 135
    ink = "#FFFFFF" if on_dark else (brand if dark_brand else "#30271F")

    if show_title:
        _text(slide, x, y, w, 0.6, title, 26, ink, bold=True, align="left")
        line = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                      Inches(x), Inches(y + 0.66), Inches(1.2), Inches(0.045))
        line.fill.solid()
        line.fill.fore_color.rgb = __import__("pptx").dml.color.RGBColor.from_string(accent.lstrip("#").upper())
        line.line.fill.background()

    n = max(1, len(items))
    rows_per_col = (n + cols - 1) // cols
    header_h = 0.95 if show_title else 0.0
    body_y = y + header_h
    avail_h = max(0.5, h - header_h)
    cell_h = avail_h / rows_per_col
    col_w = w / cols

    for i, it in enumerate(items):
        c, r = divmod(i, rows_per_col)   # 先竖后横填充，符合目录阅读顺序
        cx = x + c * col_w
        cy = body_y + r * cell_h
        num = it.get("num") or f"{i + 1:02d}"
        label = it.get("label", "")
        desc = it.get("desc", "")
        # 大号编号（衬线感：用宋体）；浅底用深色墨保证可读，不用浅色 accent（会在洗白底上隐形）
        num_size = 26 if len(items) <= 6 else 22
        if cell_h < 0.9:
            # Dense directory: separate columns in ONE row, never stack a description
            # under its label where it can run into the following item.
            _text(slide, cx, cy, col_w * .12, cell_h, num, min(19, cell_h * 44), ink,
                  bold=True, align="left", font="宋体")
            _text(slide, cx + col_w * .13, cy, col_w * .30, cell_h, label, 13, ink,
                  bold=True, align="left")
            if desc:
                _text(slide, cx + col_w * .45, cy, col_w * .55, cell_h, desc, 10, ink,
                      align="left")
            continue
        _text(slide, cx, cy, col_w * 0.24, cell_h, num, num_size, ink,
              bold=True, align="left", font="宋体")
        # 编号后竖排内容：章节名 + 说明
        tx = cx + col_w * 0.27
        tw = col_w * 0.70
        _text(slide, tx, cy + 0.02, tw, 0.42, label, 15, ink, bold=True, align="left")
        if desc:
            _text(slide, tx, cy + 0.44, tw, max(0.3, cell_h - 0.46), desc, 10.5,
                  ink, align="left", anchor=MSO_ANCHOR.TOP)

    made = list(slide.shapes)[before:]
    for i, sh in enumerate(made):
        try:
            sh.name = f"{el['element_id']}__{i}"
        except Exception:  # noqa: BLE001
            pass
    return made
