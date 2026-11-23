"""分支版式原语：把「一个主句 + 几个分支」画成 PPT 原生矢量图示。

四种 branch_type（全部原生形状/文本框，双击可改，放大不糊）：
- arrow      横向编号箭头流程：01 → 02 → 03 …
- timeline   时间轴：一条主线 + 若干节点，节点上下交替放标签
- icon_grid  图标网格：N 个统一图标卡片（编号圆 + 标题 + 说明）
- radial     中心放射：中心主题 + 四周分支节点，连线指向中心

配色角色（严格分工，避免出现「深底深字」隐形）：
- brand   品牌主色：用于填充节点圆/中心块，配白字
- accent  强调色：用于连线、箭头、描边
- ink     浅底正文墨色：用于浅色页面上的标题与说明
- white   白色：只用在 brand/accent 等深色填充之上
"""
from __future__ import annotations

import math

from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches, Pt

_ALIGN = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}
_WHITE = "#FFFFFF"
_INK = "#243029"          # 浅底上的深墨色，保证可读
_CARD = "#FBFAF7"          # 浅卡片底，比常见浅背景略亮，能与底色区分


def _rgb(v: str) -> RGBColor:
    return RGBColor.from_string(v.lstrip("#").upper())


def _no_line(shape) -> None:
    shape.line.fill.background()


def _set_line(shape, color: str, width: float = 1.6) -> None:
    shape.line.color.rgb = _rgb(color)
    shape.line.width = Pt(width)


def _text(slide, x, y, w, h, content, size, color, bold=False,
          align="center", anchor=MSO_ANCHOR.MIDDLE, font="微软雅黑"):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    para = tf.paragraphs[0]
    para.alignment = _ALIGN[align]
    para.space_after = Pt(0)
    for i, line in enumerate(str(content or "").split("\n")):
        p = para if i == 0 else tf.add_paragraph()
        p.alignment = _ALIGN[align]
        run = p.add_run()
        run.text = line
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = _rgb(color)
        run.font.name = font
        rPr = run._r.get_or_add_rPr()
        for tag in ("a:ea", "a:cs"):
            node = rPr.find(qn(tag))
            if node is None:
                node = OxmlElement(tag)
                rPr.append(node)
            node.set("typeface", font)
    return tb


def _connector(slide, x1, y1, x2, y2, color, width=1.8):
    ln = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2)
    )
    _set_line(ln, color, width)
    return ln


def _circle_label(slide, cx, cy, d, text, fill, text_color, size=13):
    s = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(cx - d / 2), Inches(cy - d / 2), Inches(d), Inches(d))
    s.fill.solid()
    s.fill.fore_color.rgb = _rgb(fill)
    _no_line(s)
    if text:
        _text(slide, cx - d / 2, cy - d / 2, d, d, text, size, text_color, bold=True)
    return s


def _draw_arrow(slide, p, items, brand, ink, accent):
    """横向编号箭头流程：编号圆 + 箭头连接 + 标题 + 说明。"""
    x, y, w, h = p["x"], p["y"], p["w"], p["h"]
    n = max(1, len(items))
    gap = 0.25
    cell_w = (w - gap * (n - 1)) / n
    cy = y + h * 0.28
    node_d = min(0.72, cell_w * 0.5, h * 0.22)
    for i, it in enumerate(items):
        cx = x + cell_w / 2 + i * (cell_w + gap)
        _circle_label(slide, cx, cy, node_d, it.get("num") or f"{i+1:02d}", brand, _WHITE)
        if i < n - 1:
            ax = cx + node_d / 2 + 0.04
            aw = cell_w + gap - node_d - 0.08
            if aw > 0.1:
                _connector(slide, ax, cy, ax + aw, cy, accent, 2.0)
                tri = slide.shapes.add_shape(MSO_SHAPE.ISOSCELES_TRIANGLE,
                                             Inches(ax + aw - 0.04), Inches(cy - 0.09), Inches(0.18), Inches(0.18))
                tri.rotation = 90
                tri.fill.solid()
                tri.fill.fore_color.rgb = _rgb(accent)
                _no_line(tri)
        ty = cy + node_d / 2 + 0.18
        _text(slide, x + i * (cell_w + gap), ty, cell_w, 0.5, it.get("label", ""),
              15, ink, bold=True, align="center")
        _text(slide, x + i * (cell_w + gap) + 0.06, ty + 0.5, cell_w - 0.12, max(0.2, h - (ty - y) - 0.5),
              it.get("desc", ""), 10.5, ink, align="center", anchor=MSO_ANCHOR.TOP)


def _draw_timeline_vertical(slide, p, items, brand, ink, accent):
    """纵向时间轴（窄洗白区专用）：左侧竖线 + 每节点「年份 标题 / 两行说明」。

    横向版在窄框里每个节点不足 1.55 英寸时，说明文字会被挤成竖条不可读；
    这里改成一行一个阶段，说明横向铺开，仍然全部是原生文本框。
    """
    x, y, w, h = p["x"], p["y"], p["w"], p["h"]
    n = max(1, len(items))
    axis_x = x + 0.22
    _connector(slide, axis_x, y + 0.10, axis_x, y + h - 0.10, accent, 2.0)
    row_h = h / n
    tx = x + 0.50
    tw = max(1.6, w - 0.56)
    for i, it in enumerate(items):
        cy = y + row_h * (i + 0.5)
        _circle_label(slide, axis_x, cy, 0.20, "", brand, _WHITE)
        head = (str(it.get("num", "")) + "   " if it.get("num") else "") + str(it.get("label", ""))
        _text(slide, tx, cy - row_h / 2 + 0.01, tw, 0.29, head,
              12.5, ink, bold=True, align="left")
        _text(slide, tx, cy - row_h / 2 + 0.31, tw, max(0.24, row_h - 0.34),
              str(it.get("desc", "")), 9.5, ink,
              align="left", anchor=MSO_ANCHOR.TOP)


def _draw_timeline(slide, p, items, brand, ink, accent):
    """时间轴：宽框横向排节点（上下交替），窄框自动切纵向列表，保证说明可读。"""
    x, y, w, h = p["x"], p["y"], p["w"], p["h"]
    n = max(1, len(items))
    step = (w - 0.4) / n
    if step < 1.55:
        _draw_timeline_vertical(slide, p, items, brand, ink, accent)
        return
    axis_y = y + h / 2
    _connector(slide, x + 0.2, axis_y, x + w - 0.2, axis_y, accent, 2.4)
    for i, it in enumerate(items):
        cx = x + 0.2 + step * (i + 0.5)
        node_d = 0.24
        _circle_label(slide, cx, axis_y, node_d, "", brand, _WHITE)
        above = (i % 2 == 0)
        label_h, desc_h = 0.42, h * 0.26
        ly = axis_y - 0.2 - label_h - desc_h if above else axis_y + 0.2
        _text(slide, cx - step / 2 + 0.05, ly, step - 0.1, label_h,
              (it.get("num", "") + "  " if it.get("num") else "") + it.get("label", ""),
              13, ink, bold=True, align="center")
        _text(slide, cx - step / 2 + 0.05, ly + label_h, step - 0.1, desc_h,
              it.get("desc", ""), 10, ink, align="center", anchor=MSO_ANCHOR.TOP)


def _draw_icon_grid(slide, p, items, brand, ink, accent, card=_CARD):
    """图标网格：浅色圆角卡片，编号图标 + 标题 + 说明。"""
    x, y, w, h = p["x"], p["y"], p["w"], p["h"]
    n = max(1, len(items))
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    gx, gy = 0.22, 0.2
    cw = (w - gx * (cols - 1)) / cols
    ch = (h - gy * (rows - 1)) / rows
    for i, it in enumerate(items):
        r, c = divmod(i, cols)
        bx = x + c * (cw + gx)
        by = y + r * (ch + gy)
        card_shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                            Inches(bx), Inches(by), Inches(cw), Inches(ch))
        card_shape.fill.solid()
        card_shape.fill.fore_color.rgb = _rgb(card)
        _set_line(card_shape, accent, 1.2)
        icon_d = min(0.62, cw * 0.32, ch * 0.3)
        icx = bx + cw / 2
        icy = by + 0.16 + icon_d / 2
        icon = slide.shapes.add_shape(MSO_SHAPE.OVAL,
                                      Inches(icx - icon_d / 2), Inches(icy - icon_d / 2),
                                      Inches(icon_d), Inches(icon_d))
        icon.fill.solid()
        icon.fill.fore_color.rgb = _rgb(brand)
        _no_line(icon)
        _text(slide, icx - icon_d / 2, icy - icon_d / 2, icon_d, icon_d,
              it.get("num") or f"{i+1:02d}", 12, _WHITE, bold=True)
        ty = icy + icon_d / 2 + 0.1
        _text(slide, bx + 0.08, ty, cw - 0.16, 0.38, it.get("label", ""),
              13, ink, bold=True, align="center")
        _text(slide, bx + 0.12, ty + 0.38, cw - 0.24, max(0.2, by + ch - (ty + 0.38) - 0.1),
              it.get("desc", ""), 9.5, ink, align="center", anchor=MSO_ANCHOR.TOP)


def is_metric_grid(el):
    """兼容旧版把实际指标放入 icon_grid.num；不改变普通 01/02 序号网格。"""
    import re
    if el.get('branch_type') == 'metric_grid':
        return True
    rows = el.get('items') or []
    nums = [str(it.get('num', '')).strip() for it in rows]
    return (el.get('branch_type') == 'icon_grid' and len(rows) >= 2
            and all(re.fullmatch(r'[+-]?\d[\d,.]*(?:\s*[%％A-Za-z\u4e00-\u9fff]+)?', n) for n in nums)
            and not all(n.isdecimal() and int(n)==i for i,n in enumerate(nums,1)))


def _metric_icon(slide, x, y, side, label, color):
    # 单个可编辑自由形状，以标签语义而不是第几项决定图标。
    if any(s in label for s in ('座位','座椅','席位')):
        paths = [[(1,1),(2,6),(8,6),(8,8),(2,8),(2,10)],[(7,8),(8,10)]]
    elif any(s in label for s in ('会员','客户','订户','用户')):
        paths = [[(5,1),(7,2),(7,4),(5,5),(3,4),(3,2),(5,1)],[(1,10),(2,7),(4,6),(6,6),(8,7),(9,10)]]
    elif any(s in label for s in ('烘焙','重量','产量','质量','批量')):
        paths = [[(2,4),(8,4),(10,10),(0,10),(2,4)],[(3,4),(3,2),(4,1),(6,1),(7,2),(7,4)]]
    elif any(s in label for s in ('年','日期','月份','天数')):
        paths = [[(1,2),(9,2),(9,10),(1,10),(1,2)],[(1,5),(9,5)],[(3,0),(3,3)],[(7,0),(7,3)]]
    else:
        paths = [[(1,1),(1,9),(9,9)],[(3,7),(5,4),(7,5),(9,2)]]
    f = slide.shapes.build_freeform(*paths[0][0], scale=Inches(side)/10)
    for i,path in enumerate(paths):
        if i: f.move_to(*path[0])
        f.add_line_segments(path[1:], close=False)
    sh = f.convert_to_shape(Inches(x), Inches(y)); sh.fill.background(); _set_line(sh,color,1.25)


def _draw_metrics(slide, p, items, ink, accent):
    if not items: return
    from unicodedata import east_asian_width
    cols = math.ceil(math.sqrt(len(items))); rows = math.ceil(len(items)/cols)
    cw = p['w']/cols; ch = p['h']/rows
    icon = min(.28, cw*.16, ch*.22); tw = cw-icon-.18
    for i,it in enumerate(items):
        row,col = divmod(i,cols); x=p['x']+col*cw; y=p['y']+row*ch
        value = str(it.get('num','')).strip(); label=str(it.get('label','')).lstrip(' ｜|·'); desc=str(it.get('desc','')).lstrip(' ｜|·')
        em = sum(1 if east_asian_width(c) in 'WF' else .65 for c in value)
        size = min(30, ch*.35*72/1.2, tw*72/max(1,em))
        if size < 12: raise ValueError('指标数字的单行可用空间不足：'+value)
        # 固定图标槽，不依赖数值字数；同列两行的图标和数字分别对齐。
        _metric_icon(slide,x,y+(ch*.39-icon)/2,icon,label,accent)
        tb = _text(slide,x+icon+.10,y,tw,ch*.39,value,size,ink,align='left',font='Times New Roman'); tb.text_frame.word_wrap=False
        _text(slide,x,y+ch*.39,cw-.12,ch*.24,label,min(13,ch*13),ink,bold=True,align='left')
        _text(slide,x,y+ch*.65,cw-.12,ch*.33,desc,min(11.5,ch*11.5),ink,align='left',anchor=MSO_ANCHOR.TOP)


def _draw_radial(slide, p, items, brand, ink, accent, center_title):
    """中心放射：中心六边形主题 + 四周节点，连线指向中心；标签统一放节点下方避免压圆。"""
    x, y, w, h = p["x"], p["y"], p["w"], p["h"]
    n = max(1, len(items))
    cx, cy = x + w / 2, y + h / 2
    core_d = min(w, h) * 0.3
    core = slide.shapes.add_shape(MSO_SHAPE.HEXAGON,
                                  Inches(cx - core_d / 2), Inches(cy - core_d / 2),
                                  Inches(core_d), Inches(core_d))
    core.fill.solid()
    core.fill.fore_color.rgb = _rgb(brand)
    _no_line(core)
    _text(slide, cx - core_d / 2, cy - core_d / 2, core_d, core_d,
          center_title or "主题", 14, _WHITE, bold=True, align="center")

    rx = w * 0.38
    ry = h * 0.34
    for i, it in enumerate(items):
        ang = -math.pi / 2 + 2 * math.pi * i / n
        nx = cx + rx * math.cos(ang)
        ny = cy + ry * math.sin(ang)
        sx = cx + (core_d / 2) * math.cos(ang)
        sy = cy + (core_d / 2) * math.sin(ang)
        _connector(slide, sx, sy, nx, ny, accent, 1.6)
        nd = min(0.5, w * 0.08)
        _circle_label(slide, nx, ny, nd, it.get("num") or f"{i+1}", accent, _WHITE, size=11)
        # 标签块放在节点圆正下方，水平居中，避开圆与连线
        tw = min(2.1, w * 0.26)
        tx = nx - tw / 2
        ty = ny + nd / 2 + 0.05
        _text(slide, tx, ty, tw, 0.3, it.get("label", ""), 12, ink, bold=True, align="center")
        _text(slide, tx, ty + 0.32, tw, 0.5, it.get("desc", ""), 9, ink,
              align="center", anchor=MSO_ANCHOR.TOP)


def draw_branch(slide, el: dict, palette: dict, on_dark: bool = False) -> list:
    """返回创建的形状列表；name 以 element_id 开头，便于自检映射。

    on_dark=True 时正文/标签文字改用白色，避免深底深字隐形；
    icon_grid 的卡片底同步换成深色，保证浅字可读。
    """
    before = len(slide.shapes)
    p = el["position"]
    btype = el.get("branch_type", "icon_grid")
    items = el.get("items") or []
    brand = palette.get("primary", "#0B2A4A")
    accent = palette.get("secondary", "#C9A86A")
    ink = _WHITE if on_dark else _INK
    card = "#1A2B33" if on_dark else _CARD   # 深底上的卡片底

    if is_metric_grid(el):
        _draw_metrics(slide, p, items, ink, accent)
    elif btype == "arrow":
        _draw_arrow(slide, p, items, brand, ink, accent)
    elif btype == "timeline":
        _draw_timeline(slide, p, items, brand, ink, accent)
    elif btype == "radial":
        _draw_radial(slide, p, items, brand, ink, accent, el.get("title", ""))
    else:  # icon_grid
        _draw_icon_grid(slide, p, items, brand, ink, accent, card=card)

    made = list(slide.shapes)[before:]
    for i, sh in enumerate(made):
        try:
            sh.name = f"{el['element_id']}__{i}"
        except Exception:  # noqa: BLE001
            pass
    return made
