"""装饰素材：把 decorate 原语画成 PPT 原生矢量形状。

说明：python-pptx 不能把 .svg 插成可编辑矢量形状，因此这里的「SVG 装饰」
不走位图导入，而是直接生成原生椭圆/线条/矩形 —— 双击可选中、可改色改描边，
比导入 SVG 更彻底。同时输出一份 .svg 文本作为人工收口时的参考文件。
"""
from __future__ import annotations

from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches, Pt


def _rgb(value: str) -> RGBColor:
    return RGBColor.from_string(value.lstrip("#").upper())


def _set_line(shape, color: str, width_pt: float = 1.2, dash: str | None = None) -> None:
    shape.line.color.rgb = _rgb(color)
    shape.line.width = Pt(width_pt)
    if dash:
        ln = shape._element.spPr.find(qn("a:ln"))
        if ln is not None:
            for old in ln.findall(qn("a:prstDash")):
                ln.remove(old)
            el = OxmlElement("a:prstDash")
            el.set("val", dash)
            ln.append(el)


def draw_decoration(slide, el: dict) -> list:
    """返回创建的原生形状列表；每个形状 name 以 element_id 开头，便于自检映射。"""
    p = el["position"]
    x, y, w, h = p["x"], p["y"], p["w"], max(p["h"], 0.05)
    color = el["color"]
    prim = el["primitive"]
    eid = el["element_id"]
    made = []

    def _register(shape, i: int):
        shape.name = f"{eid}__{i}"
        made.append(shape)
        return shape

    if prim == "ring":
        for i, shrink in enumerate((0.0, 0.22, 0.42)):
            s = slide.shapes.add_shape(
                MSO_SHAPE.OVAL,
                Inches(x + w * shrink), Inches(y + h * shrink),
                Inches(w * (1 - 2 * shrink)), Inches(h * (1 - 2 * shrink)),
            )
            s.fill.background()
            _set_line(s, color, 1.1)
            _register(s, i)

    elif prim in {"line", "dashed_line"}:
        s = slide.shapes.add_connector(
            MSO_CONNECTOR.STRAIGHT,
            Inches(x), Inches(y + h / 2), Inches(x + w), Inches(y + h / 2),
        )
        _set_line(s, color, 1.4, dash="dash" if prim == "dashed_line" else None)
        _register(s, 0)

    elif prim == "diagonal_bar":
        s = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
        s.fill.solid()
        s.fill.fore_color.rgb = _rgb(color)
        s.line.fill.background()
        s.rotation = 35
        _register(s, 0)

    elif prim == "dot_grid":
        cols, rows = 6, 3
        for r in range(rows):
            for c in range(cols):
                dx = x + w * c / max(1, cols - 1)
                dy = y + h * r / max(1, rows - 1)
                s = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(dx), Inches(dy), Inches(0.045), Inches(0.045))
                s.fill.solid()
                s.fill.fore_color.rgb = _rgb(color)
                s.line.fill.background()
                _register(s, r * cols + c)

    return made


def decoration_svg(el: dict) -> str:
    """生成对应的 SVG 参考文件（人工收口 / 导入其它工具时用）。"""
    p = el["position"]
    w, h = int(p["w"] * 96), max(4, int(p["h"] * 96))
    c = el["color"]
    prim = el["primitive"]
    body = ""
    if prim == "ring":
        for k in (0.5, 0.39, 0.29):
            body += f'<circle cx="{w/2}" cy="{h/2}" r="{min(w,h)*k:.1f}" fill="none" stroke="{c}" stroke-width="1.2"/>'
    elif prim in {"line", "dashed_line"}:
        dash = ' stroke-dasharray="6 5"' if prim == "dashed_line" else ""
        body = f'<line x1="0" y1="{h/2}" x2="{w}" y2="{h/2}" stroke="{c}" stroke-width="1.4"{dash}/>'
    elif prim == "diagonal_bar":
        body = f'<rect x="0" y="{h*0.35:.1f}" width="{w}" height="{h*0.3:.1f}" fill="{c}" transform="rotate(-35 {w/2} {h/2})"/>'
    else:
        for i in range(6):
            for j in range(3):
                body += f'<circle cx="{w*i/5:.1f}" cy="{h*j/2:.1f}" r="2" fill="{c}"/>'
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">{body}</svg>'


def svg_source_for_page(page: dict) -> str:
    """把一页的装饰汇总成一个 SVG 文件，落到素材目录供人工取用。"""
    parts = []
    for el in page["elements"]:
        if el["type"] == "decorate":
            parts.append(decoration_svg(el))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">'
        + "".join(parts)
        + "</svg>"
    )
