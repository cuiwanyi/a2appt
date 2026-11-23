"""创意目录版式（对齐用户给的三张企业画册目录示例）。

与普通 toc 的区别：
    普通 toc = 两列圆角卡片 / 单列编号列表。
    creative_toc = 超大衬线斜体数字 01–08 当视觉主体 + 数字后细虚线 +
                   错落网格(2列×4行) + 章节名 + 每行要点小字 +
                   左侧品牌宣言区 + 页眉「目录/CONTENTS」+ 底部分组标签。

全部是 python-pptx 原生文本框/形状/连接线，双击可改字、改色、改位置。
配合整页摄影 + 焦点自适应渐变遮罩：目录整体落在洗白侧，图片焦点保持清晰。
"""
from __future__ import annotations

from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches, Pt

_ALIGN = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}


def _rgb(v: str) -> RGBColor:
    return RGBColor.from_string(v.lstrip("#").upper())


def _text(slide, x, y, w, h, content, size, color, *, bold=False,
          italic=False, align="left", font="微软雅黑", anchor=MSO_ANCHOR.TOP,
          spacing=1.0):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    for i, line in enumerate(str(content or "").split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = _ALIGN[align]
        p.space_after = Pt(0)
        p.line_spacing = spacing
        run = p.add_run()
        run.text = line
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.italic = italic
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


def _rule(slide, x1, y1, x2, y2, color, width=0.9, dashed=False):
    """细线；dashed=True 时用 OOXML prstDash 画虚线（示例里的点线引线）。"""
    ln = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2)
    )
    ln.line.color.rgb = _rgb(color)
    ln.line.width = Pt(width)
    if dashed:
        ln_el = ln._element.spPr.find(qn("a:ln"))
        if ln_el is not None:
            for old in ln_el.findall(qn("a:prstDash")):
                ln_el.remove(old)
            dash = OxmlElement("a:prstDash")
            dash.set("val", "dash")
            ln_el.append(dash)
    return ln


def _bar(slide, x, y, w, color, h=0.016):
    from pptx.enum.shapes import MSO_SHAPE
    s = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                               Inches(x), Inches(y), Inches(w), Inches(h))
    s.fill.solid()
    s.fill.fore_color.rgb = _rgb(color)
    s.line.fill.background()
    return s


def draw_creative_toc(slide, spec: dict, wash_w: float, palette: dict) -> list:
    """在洗白侧画创意目录。wash_w = 洗白区可用宽度（英寸）。

    spec: title / eyebrow / manifesto(list[str]) / items[{num,label,desc}] /
          groups[list[str]] / page_no / page_total
    返回创建的形状列表。
    """
    made = []
    brand = palette.get("primary", "#3B2A21")      # 深墨棕：标题/章节名
    accent = palette.get("secondary", "#A8763E")  # 金棕：超大数字
    soft = palette.get("soft", "#6B5744")         # 灰棕：要点小字
    faint = palette.get("faint", "#C9B49A")      # 淡金：虚线/细线

    items = spec.get("items") or []
    n = len(items)
    cols = int(spec.get("cols", 2))
    rows = (n + cols - 1) // cols

    # ---- 自适应：洗白区够宽时左侧留独立品牌宣言列；不够宽则宣言移到顶部一行 ----
    full_grid = wash_w < 9.4
    if full_grid:
        left_w = 0.0
        gx = 0.82
        gw = max(4.6, wash_w - 1.30)
        head_y = 0.46
        body_y = 2.06          # 顶部页眉/眉标/细线/宣言占 ~2in，下面 4 行网格
    else:
        left_w = min(3.05, wash_w * 0.29)
        gx = 0.82 + left_w + 0.46
        gw = max(4.8, wash_w - gx - 0.45)
        head_y = 0.62
        body_y = 1.90

    # ---- 页眉：目录 + CONTENTS + 细线 ----
    made.append(_text(slide, 0.82, head_y, min(4.0, wash_w - 1.0), 0.72,
                      spec.get("title", "目录"), 34, brand,
                      bold=True, font="宋体"))
    made.append(_text(slide, 0.82, head_y + 0.78, min(4.4, wash_w - 1.0), 0.28,
                      spec.get("eyebrow", "CONTENTS  ·  SANYU COFFEE"), 10, soft,
                      font="微软雅黑"))
    # 页码放眉标同行右侧的「充分洗白区」（不落在渐变交界处），深色字保证对比
    made.append(_text(slide, 4.55, head_y + 0.78, 1.35, 0.28,
                      f"{spec.get('page_no', 2):02d}  /  {spec.get('page_total', 8):02d}",
                      10, brand, align="right", font="微软雅黑"))
    made.append(_bar(slide, 0.82, head_y + 1.16, min(7.6, wash_w - 1.2), accent))

    # ---- 左侧品牌宣言（示例1/2 的左半留白大字） ----
    man = spec.get("manifesto") or []
    if man and not full_grid:
        my = head_y + 1.62
        made.append(_text(slide, 0.82, my, left_w, 1.9,
                          "\n".join(man[:3]), 24, brand,
                          bold=True, font="宋体", spacing=1.18))
        made.append(_bar(slide, 0.82, my + 2.02, 0.72, accent, h=0.022))
        made.append(_text(slide, 0.82, my + 2.22, left_w, 1.2,
                          "从产区的一支豆子，\n到手里这一杯。", 11, soft,
                          font="微软雅黑", spacing=1.35))
    elif man and full_grid:
        made.append(_text(slide, 0.82, head_y + 1.24, gw, 0.30,
                          "  ·  ".join(man[:2]), 11, brand, font="宋体"))

    # ---- 目录网格：超大斜体数字 + 虚线 + 章节名 + 要点 ----
    col_w = gw / cols
    avail_h = 6.70 - body_y          # 6.70 以上留给底部分组标签
    row_h = min(1.18, avail_h / max(1, rows))
    for i, it in enumerate(items):
        c, r = i % cols, i // cols
        cx = gx + c * col_w
        cy = body_y + r * row_h
        pad = 0.12 if cols > 1 else 0.0
        inner_w = col_w - pad - 0.10

        num = str(it.get("num") or f"{i + 1:02d}")
        # 超大衬线斜体数字（视觉主体）；控制高度，避免压到上一行说明文字
        made.append(_text(slide, cx, cy - 0.04, 0.86, 0.44, num, 26, accent,
                          bold=False, italic=True, font="Georgia"))
        # 数字右侧细虚线，延伸到本格右缘（示例的点线引线）
        made.append(_rule(slide, cx + 0.90, cy + 0.24,
                          cx + inner_w, cy + 0.24, faint, 0.8, dashed=True))
        # 章节名
        made.append(_text(slide, cx, cy + 0.40, inner_w, 0.25,
                          str(it.get("label", "")), 11.5, brand,
                          bold=True, font="微软雅黑"))
        # 要点小字（内容充实，不是只有章节名）
        made.append(_text(slide, cx, cy + 0.65, inner_w, 0.36,
                          str(it.get("desc", "")), 8.5, soft,
                          font="微软雅黑", spacing=1.05))

    # ---- 底部分组标签 + 页码（页码移到右侧，避免与左列分组标签叠字）----
    groups = spec.get("groups") or []
    gy = 6.82
    if groups:
        gw_each = min(3.0, gw / max(1, len(groups)) - 0.25)
        for k, g in enumerate(groups[:2]):
            gx2 = gx + k * (gw_each + 0.35)
            made.append(_bar(slide, gx2, gy, 0.52, accent, h=0.022))
            made.append(_text(slide, gx2, gy + 0.14, gw_each, 0.28,
                              str(g), 10, soft, font="微软雅黑"))
    return made
