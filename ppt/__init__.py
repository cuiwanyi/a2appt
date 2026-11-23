"""PPT 底层能力：构建、工具层、自检、可选渲染。"""
from .builder import PPTBuilder
from .decorations import decoration_svg, svg_source_for_page
from .inspector import check_pdf_text_layer, inspect
from .preview import render_layout_preview
from .render import (
    detect_renderer, soffice_executable, try_render,
    wps_converter_executable, wps_diagnostics,
)
from .schema import (
    asset_requests, auto_fit_adjustments, font_substitutions, layout_primitives,
    normalize_layout, vi_palette,
)
from .slicing import mask_geometry, prepare_mask_slices, slice_mask_asset, src_rect_for
from .tools import PPTToolkit, TOOL_SPECS

__all__ = [
    "PPTBuilder", "PPTToolkit", "TOOL_SPECS", "normalize_layout", "asset_requests",
    "vi_palette", "font_substitutions", "auto_fit_adjustments", "layout_primitives",
    "inspect", "check_pdf_text_layer", "try_render", "detect_renderer",
    "soffice_executable", "wps_converter_executable", "wps_diagnostics",
    "render_layout_preview",
    "mask_geometry", "slice_mask_asset", "src_rect_for", "prepare_mask_slices",
    "decoration_svg", "svg_source_for_page",
]
