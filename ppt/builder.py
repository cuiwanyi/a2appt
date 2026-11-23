"""python-pptx 构建器：全部元素原生可编辑，蒙版用 OOXML 图片填充实现。

重要事实（已核验 python-pptx 1.0.2 / pptx-mcp-server 0.5.1）：
    两者都没有 shape.fill.picture / fill_picture 这类接口。
    形状图片填充必须手写 a:blipFill + r:embed 图片关系，并用 a:srcRect 做裁切。
    本文件的 _picture_fill() 就是那个「底层接口」。
"""
from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches, Pt

import config
from .decorations import draw_decoration
from .branch_diagram import draw_branch
from .slicing import mask_geometry, src_rect_for

_SHAPE_MAP = {
    "rect": MSO_SHAPE.RECTANGLE,
    "round_rect": MSO_SHAPE.ROUNDED_RECTANGLE,
    "oval": MSO_SHAPE.OVAL,
    "triangle": MSO_SHAPE.ISOSCELES_TRIANGLE,
    "chevron": MSO_SHAPE.CHEVRON,
    "hexagon": MSO_SHAPE.HEXAGON,
    "diamond": MSO_SHAPE.DIAMOND,
    "parallelogram": MSO_SHAPE.PARALLELOGRAM,
    "trapezoid": MSO_SHAPE.TRAPEZOID,
}
_ALIGN_MAP = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}


def _rgb(value: str) -> RGBColor:
    return RGBColor.from_string(value.lstrip("#").upper())


class PPTBuilder:
    def __init__(self, slide_w_in: float | None = None, slide_h_in: float | None = None):
        self.prs = Presentation()
        self.slide_w = slide_w_in or config.SLIDE_W_IN
        self.slide_h = slide_h_in or config.SLIDE_H_IN
        self.prs.slide_width = Inches(self.slide_w)
        self.prs.slide_height = Inches(self.slide_h)
        self.blank_layout = self.prs.slide_layouts[6]
        self.shape_map: dict[str, list] = {}
        self.warnings: list[str] = []
        self.slice_usage: list[dict] = []
        self.focus_evidence: list[dict] = []
        self._page = None
        # 分支图示原语用的 VI 主/辅色，由 agents/builder.py 从 layout["vi"] 注入
        self._vi_primary = "#0B2A4A"
        self._vi_secondary = "#C9A86A"
        self._page_on_dark = False   # 当前页背景是否深色，供分支图选文字颜色
        import tempfile
        self._scrim_dir = Path(tempfile.gettempdir()) / "a2appt_scrims"  # scrim 渐变图落盘目录
        self._scrim_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 底层图片填充
    @staticmethod
    def _picture_fill(shape, slide, image_path: str, src_rect: dict[str, int]) -> None:
        """给任意自选图形设置图片填充（蒙版裁切），返回无。

        src_rect 单位：DrawingML 百分比的千分数，100000 = 100%。
        例：{"l":20000,"r":60000} 表示横向只显示原图 20%~40% 这一竖条。
        """
        spPr = shape._element.spPr
        for tag in ("a:noFill", "a:solidFill", "a:gradFill", "a:blipFill", "a:pattFill", "a:grpFill"):
            for node in spPr.findall(qn(tag)):
                spPr.remove(node)

        image_part, rId = slide.part.get_or_add_image_part(str(image_path))
        blipFill = OxmlElement("a:blipFill")
        blipFill.set("rotWithShape", "1")
        blip = OxmlElement("a:blip")
        blip.set(qn("r:embed"), rId)
        blipFill.append(blip)
        sr = OxmlElement("a:srcRect")
        for k in ("l", "t", "r", "b"):
            sr.set(k, str(int(src_rect.get(k, 0))))
        blipFill.append(sr)
        stretch = OxmlElement("a:stretch")
        stretch.append(OxmlElement("a:fillRect"))
        blipFill.append(stretch)

        # spPr 子元素顺序：xfrm -> prstGeom/custGeom -> fill -> ln ...
        insert_at = 0
        for i, child in enumerate(list(spPr)):
            if child.tag in (qn("a:xfrm"), qn("a:prstGeom"), qn("a:custGeom")):
                insert_at = i + 1
        spPr.insert(insert_at, blipFill)

    # ------------------------------------------------------------ 元素级工具
    def set_page_context(self, page: dict, spec: dict | None = None) -> None:
        """缓存当前页的 scrim 洗白方向，供 toc 等原语把内容收进洗白侧。"""
        from .badge_layers import protect_badge_layers
        protect_badge_layers(page)
        self._page_texts = {e.get("content", "").strip() for e in page.get("elements", [])
                            if e.get("type") == "text"}
        self._page = page
        from .image_layout import full_bleed_rule
        required = full_bleed_rule(spec or {})
        if required is None:
            required = full_bleed_rule(page)
        if required is not None and page['bg']['type'] == 'image':
            page['bg']['full_bleed'] = required
        self._page_wash = next(
            (e.get("wash", "") for e in page.get("elements", []) if e.get("type") == "scrim"), ""
        )

    @staticmethod
    def _is_dark_color(hex_color: str) -> bool:
        """判断背景色明暗：亮度低于阈值视为深色（供分支图选白字/墨字）。"""
        try:
            hx = str(hex_color).lstrip("#")
            r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
            return (0.299 * r + 0.587 * g + 0.114 * b) < 128
        except Exception:  # noqa: BLE001
            return False

    def set_background(self, slide, bg: dict, assets: dict[str, str]) -> list:
        made = []
        unusable = False
        checklist = None
        full_bleed = bg.get('type') == 'image' and bg.get('full_bleed') is True
        measure_bg = bg.get('measure_focus') or full_bleed
        if not measure_bg and bg.get('type') == 'image' and self._page:
            from .checklist_layout import checklist_rows
            if checklist_rows(self._page['elements'], self.slide_w, self.slide_h):
                bg['measure_focus'] = measure_bg = True
        if self._page is not None and self._page['bg'] is bg and measure_bg:
            from .image_layout import measure, adapt_page
            aid = bg.get('image')
            evidence = measure(assets.get(aid), cover_ratio=self.slide_w/self.slide_h if full_bleed else None)
            if bg.get('asset_source') in ('mock', 'local-fallback') and evidence.get('raw_sha256') == bg.get('asset_sha256'):
                evidence = {'status': 'placeholder', 'raw_sha256': bg['asset_sha256']}
            unusable = evidence['status'] in ('unreadable', 'missing')
            if self._page.get('_overlay_rework'):
                # 定向修复使用审核时的有效框，不再把图文搬进单侧栏。
                if self._page['_overlay_rework'] == 'bg_object' and evidence['status'] in ('measured', 'uncertain'):
                    from .focus_scrim import analyze_focus
                    focus = analyze_focus(assets[aid])
                    for el in self._page['elements']:
                        if el['type'] == 'scrim':
                            el.update(wash=focus['wash'], boundary=focus['boundary'])
                    evidence.update(wash=focus['wash'], boundary=focus['boundary'], basis=focus['basis'])
                if full_bleed:
                    from .checklist_layout import checklist_rows
                    from .image_layout import check_full_bleed_rows
                    if checklist_rows(self._page['elements'], self.slide_w, self.slide_h):
                        check_full_bleed_rows(self._page, evidence, self.slide_w, self.slide_h)
            else:
                adapt_page(self._page, evidence, self.slide_w, self.slide_h)
            if full_bleed:
                if evidence['status'] != 'measured' and 'unresolved' not in evidence:
                    evidence['unresolved'] = [e['element_id'] for e in self._page['elements']
                                              if e['type'] in ('text', 'branch', 'toc')]
                for el in self._page['elements']:
                    if el['type'] == 'scrim':
                        el['position'] = dict(x=0, y=0, w=self.slide_w, h=self.slide_h)
            evidence.update(page=self._page['page_name'], asset_id=aid)
            self.focus_evidence.append(evidence)
            if evidence.get('wash'):
                self._page_wash = evidence['wash']
            if evidence.get('layout') == 'checklist_columns':
                checklist = evidence
            elif evidence['status'] != 'measured' or evidence.get('unresolved'):
                self.warnings.append(f"{self._page['page_name']}：实图留白不足或未能安全移动全部文字，需复核（{evidence['status']}）")
        # 记录本页背景明暗：纯色用自身；图片页若是深色背景图，靠 layout 的 bg.color 兜底
        bg_color = bg.get("color")
        if bg["type"] == "image" and not bg_color:
            bg_color = "#FFFFFF"  # 图片背景默认按浅色处理，具体由文字层自身对比保证
        self._page_on_dark = self._is_dark_color(bg_color or "#FFFFFF")
        if bg["type"] == "image":
            path = assets.get(bg["image"] or "")
            if not unusable and path and Path(path).exists():
                if checklist:
                    # 页面底色延伸至文字栏；遮罩只在该栏内，摄影没有任何文字覆盖。
                    fill = slide.background.fill
                    fill.solid()
                    paper = next((e.get('color') for e in self._page['elements'] if e['type'] == 'scrim'), None)
                    fill.fore_color.rgb = _rgb(paper or bg_color)
                    shp = self._checklist_photo(slide, path, checklist)
                else:
                    shp = slide.shapes.add_picture(
                        str(path), Inches(0), Inches(0), Inches(self.slide_w), Inches(self.slide_h)
                    )
                    if full_bleed:
                        from .slicing import _cover_view
                        l, t, r, b = _cover_view(*shp.image.size, self.slide_w, self.slide_h)
                        shp.crop_left, shp.crop_top, shp.crop_right, shp.crop_bottom = l, t, 1-r, 1-b
                shp.name = "bg__image"
                made.append(shp)
            else:
                self.warnings.append("背景图缺失，改用 VI 主色纯色背景")
                bg = {"type": "color", "color": "#0B2A4A"}
        if bg["type"] == "color":
            fill = slide.background.fill
            fill.solid()
            fill.fore_color.rgb = _rgb(bg["color"])
        return made

    def _checklist_photo(self, slide, path, evidence):
        from PIL import Image
        with Image.open(path) as src:
            ratio = src.width / src.height
        panel = self.slide_w * (1-evidence['boundary'])
        x = self.slide_w-panel if evidence['wash'] == 'left' else 0
        # 只裁左右空处，细节包围框两端留余量；主体太宽则等比留边，不硬裁。
        left, _, right, _ = evidence.get('detail_box', (0, 0, 1, 1))
        kept = min(1, max(right-left+.12, panel/self.slide_h/ratio))
        start = max(0, min(1-kept, (left+right-kept)/2))
        h = min(self.slide_h, panel/(kept*ratio))
        w = h*kept*ratio
        shp = slide.shapes.add_picture(str(path), Inches(x+(panel-w)/2), Inches((self.slide_h-h)/2),
                                       Inches(w), Inches(h))
        shp.crop_left, shp.crop_right = start, max(0, 1-start-kept)
        return shp

    @staticmethod
    def _set_east_asian_font(run, family: str) -> None:
        rPr = run._r.get_or_add_rPr()
        for tag in ("a:ea", "a:cs"):
            node = rPr.find(qn(tag))
            if node is None:
                node = OxmlElement(tag)
                rPr.append(node)
            node.set("typeface", family)

    def add_text(self, slide, el: dict) -> list:
        p = el["position"]
        tb = slide.shapes.add_textbox(Inches(p["x"]), Inches(p["y"]), Inches(p["w"]), Inches(p["h"]))
        tb.name = el["element_id"]
        tf = tb.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = MSO_ANCHOR.TOP
        font = el["font"]
        lines = (el["content"] or " ").split("\n")
        para = tf.paragraphs[0]
        for i, line in enumerate(lines):
            if i:
                para = tf.add_paragraph()
            para.alignment = _ALIGN_MAP[el["align"]]
            para.space_after = Pt(0)
            run = para.add_run()
            run.text = line
            run.font.name = font["family"]
            run.font.size = Pt(font["size"])
            run.font.bold = font["weight"] == "bold"
            run.font.color.rgb = _rgb(font["color"])
            self._set_east_asian_font(run, font["family"])
        return [tb]

    def add_shape(self, slide, el: dict) -> list:
        p = el["position"]
        kind = _SHAPE_MAP.get(el.get("shape", "rect"), MSO_SHAPE.RECTANGLE)
        shp = slide.shapes.add_shape(
            kind, Inches(p["x"]), Inches(p["y"]), Inches(p["w"]), Inches(p["h"])
        )
        shp.name = el["element_id"]
        if el.get("fill"):
            shp.fill.solid()
            shp.fill.fore_color.rgb = _rgb(el["fill"])
            opacity = el.get("fill_opacity", 1.0)
            if opacity < 1.0:
                alpha = OxmlElement("a:alpha")
                alpha.set("val", str(round(opacity * 100000)))
                shp._element.spPr.solidFill.srgbClr.append(alpha)
        else:
            shp.fill.background()
        if el.get("line"):
            shp.line.color.rgb = _rgb(el["line"])
            shp.line.width = Pt(1.0)
        else:
            shp.line.fill.background()
        return [shp]

    def add_picture_layer(self, slide, el: dict, assets: dict[str, str]) -> list:
        p = el["position"]
        path = assets.get(el["asset_id"])
        if not path or not Path(path).exists():
            self.warnings.append(f"独立图片 {el['asset_id']} 缺失，退化为 VI 主色色块")
            return self.add_shape(slide, {**el, "shape": "rect", "fill": "#0B2A4A", "line": None})
        # 等比裁切：图片原始比例与元素框不一致时，居中裁掉多余部分再放入，
        # 绝不拉伸变形（用户明确要求「图片不能压缩」）。透明艺术字 PNG 跳过，
        # 它的框已按真实宽高比算好，裁切会破坏笔锋。
        x, y, w, h = (p[k] for k in ("x", "y", "w", "h"))
        if w > 0 and h > 0:
            from PIL import Image
            from .focus_scrim import cover_crop
            target = p["w"] / p["h"]
            with Image.open(path) as im:
                cur = im.size[0] / im.size[1]
                has_alpha = im.mode in {"RGBA", "LA"} or (
                    im.mode == "P" and "transparency" in im.info
                )
            if has_alpha and abs(cur - target) / target > 0.02:
                # 透明素材完整等比居中，不能把宽幅 PNG 的圆环强行拉进正方框。
                fit_w, fit_h = min(w, h * cur), min(h, w / cur)
                x, y = x + (w - fit_w) / 2, y + (h - fit_h) / 2
                w, h = fit_w, fit_h
            elif not has_alpha and abs(cur - target) / target > 0.02:
                cropped = self._scrim_dir / f"crop_{el['element_id']}.png"
                cover_crop(path, cropped, target)
                path = str(cropped)
        shp = slide.shapes.add_picture(
            str(path), Inches(x), Inches(y), Inches(w), Inches(h)
        )
        shp.name = el["element_id"]
        return [shp]

    def add_mask_picture(self, slide, el: dict, assets: dict[str, str]) -> list:
        """形状图片填充：按 mask.layout 排出若干单元格，每格一个无描边原生形状。

        支持 single / columns / rows / grid；continuous=true 时所有格取自同一张连续全景，
        false 时每格用自己的素材。优先用 AssetAgent 预裁的切片（跨渲染器一致，
        LibreOffice 实测会忽略 a:srcRect）；没有切片才退回 OOXML srcRect（PowerPoint 精确）。
        几何由 ppt/slicing.py::mask_geometry 统一定义，构建与预览不会算出两套坐标。
        """
        p = el["position"]
        mask = el["mask"]
        aid = el["asset_id"]
        whole = assets.get(aid)
        cells = mask_geometry(el)
        rects = src_rect_for(el)
        made = []
        for cell in cells:
            i = cell["index"]
            cx, cy = p["x"] + cell["dx"], p["y"] + cell["dy"]
            col_w, ch = cell["w"], cell["h"]
            kind = _SHAPE_MAP.get(mask["shape"], MSO_SHAPE.RECTANGLE)
            shp = slide.shapes.add_shape(
                kind, Inches(cx), Inches(cy), Inches(col_w), Inches(ch)
            )
            shp.name = f"{el['element_id']}__{i}"
            shp.line.fill.background()

            slice_path = assets.get(f"{aid}__{i}")
            if slice_path and Path(slice_path).exists():
                # 切片已经按格子几何裁好，形状只需原样填满，srcRect 全 0
                self._picture_fill(shp, slide, slice_path, {"l": 0, "t": 0, "r": 0, "b": 0})
                self.slice_usage.append(
                    {"element_id": el["element_id"], "cell": i, "mode": "pre-sliced",
                     "layout": mask["layout"]}
                )
            elif whole and Path(whole).exists():
                self._picture_fill(shp, slide, whole, rects[i])
                self.slice_usage.append(
                    {"element_id": el["element_id"], "cell": i, "mode": "srcRect",
                     "layout": mask["layout"]}
                )
            else:
                self.warnings.append(f"蒙版素材 {aid} 缺失，第 {i+1} 格退化为色块")
                shp.fill.solid()
                shp.fill.fore_color.rgb = _rgb("#0B2A4A")
            made.append(shp)
        return made

    def add_decoration(self, slide, el: dict) -> list:
        return draw_decoration(slide, el)

    def add_branch(self, slide, el: dict) -> list:
        return draw_branch(slide, el, {"primary": self._vi_primary,
                                       "secondary": self._vi_secondary},
                           on_dark=self._page_on_dark)

    def add_scrim(self, slide, el: dict) -> list:
        """焦点保护渐变遮罩：本地生成一张白色渐变 PNG，盖在背景图上、文字之下。"""
        from .scrim import make_scrim
        out = self._scrim_dir / f"{el['element_id']}.png"
        if 'boundary' in el:
            from .focus_scrim import make_adaptive_scrim
            make_adaptive_scrim(el, out, strength=el.get('strength', .96))
        else:
            make_scrim(el.get("wash", "left"), el.get("color", "#FFFFFF"),
                       el.get("strength", 0.96), out)
        p = el["position"]
        shp = slide.shapes.add_picture(
            str(out), Inches(p["x"]), Inches(p["y"]), Inches(p["w"]), Inches(p["h"])
        )
        shp.name = el["element_id"]
        return [shp]

    def add_toc(self, slide, el: dict) -> list:
        from .toc_diagram import draw_toc
        return draw_toc(slide, el, {"primary": self._vi_primary,
                                    "secondary": self._vi_secondary},
                        on_dark=self._page_on_dark,
                        page_wash=getattr(self, "_page_wash", ""),
                        show_title=(el.get("title") or "目录") not in getattr(self, "_page_texts", set()))

    # ------------------------------------------------------------ 组装
    def add_element(self, slide, el: dict, assets: dict[str, str]) -> list:
        etype = el["type"]
        if etype == "text":
            made = self.add_text(slide, el)
        elif etype == "shape":
            made = self.add_shape(slide, el)
        elif etype == "picture":
            made = self.add_picture_layer(slide, el, assets)
        elif etype == "mask_picture":
            made = self.add_mask_picture(slide, el, assets)
        elif etype == "decorate":
            made = self.add_decoration(slide, el)
        elif etype == "branch":
            made = self.add_branch(slide, el)
        elif etype == "scrim":
            made = self.add_scrim(slide, el)
        elif etype == "toc":
            made = self.add_toc(slide, el)
        else:  # normalize_layout 已过滤，兜底
            made = []
        self.shape_map[el["element_id"]] = made
        return made

    def build(self, layout: dict, assets: dict[str, str], out_path: str | Path) -> Path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        from .schema import clean_closing_title
        clean_closing_title(layout)
        for page in layout["pages"]:
            if len(layout['pages']) > 1 and page['bg']['type'] == 'image':
                page['bg']['measure_focus'] = True
            slide = self.prs.slides.add_slide(self.blank_layout)
            self.set_page_context(page)
            self.set_background(slide, page["bg"], assets)
            # elements 已按 z 排序，写入顺序 = 从底到顶的图层顺序
            for el in page["elements"]:
                self.add_element(slide, el, assets)
        self.prs.save(str(out))
        return out
