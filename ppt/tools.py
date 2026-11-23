"""PPT 工具层：把 python-pptx 能力封成 MCP 风格的工具清单。

路线 B 原方案是让 Agent 调 pptx-mcp-server 的 MCP 工具。这里保留同样的
「按 JSON 调工具」语义，但工具实现放在进程内（因为 pptx-mcp-server 0.5.1
同样不支持形状图片填充，见 README）。TOOL_SPECS 可直接贴给 MCP 客户端使用，
换成真实 MCP server 时只需把 call() 换成 MCP tools/call 请求。
"""
from __future__ import annotations

from typing import Any, Callable

from .builder import PPTBuilder

TOOL_SPECS: list[dict] = [
    {
        "name": "set_background",
        "description": "设置页面背景（VI 纯色或独立背景图层）",
        "input_schema": {"type": "object", "properties": {"bg": {"type": "object"}}, "required": ["bg"]},
    },
    {
        "name": "add_text",
        "description": "新增原生文本框（双击可改字，绝不写进图片）",
        "input_schema": {"type": "object", "properties": {"element": {"type": "object"}}, "required": ["element"]},
    },
    {
        "name": "add_shape",
        "description": "新增原生形状（色块/圆角容器/圆环/线），可改颜色与描边",
        "input_schema": {"type": "object", "properties": {"element": {"type": "object"}}, "required": ["element"]},
    },
    {
        "name": "set_picture_fill",
        "description": "对形状做 OOXML 图片填充蒙版（a:blipFill + a:srcRect 横向切片）",
        "input_schema": {"type": "object", "properties": {"element": {"type": "object"}}, "required": ["element"]},
    },
    {
        "name": "add_picture_layer",
        "description": "插入独立图片图层，可单独删除或替换",
        "input_schema": {"type": "object", "properties": {"element": {"type": "object"}}, "required": ["element"]},
    },
    {
        "name": "add_decoration",
        "description": "把装饰 SVG 原语画成原生矢量形状（可选中、可改色）",
        "input_schema": {"type": "object", "properties": {"element": {"type": "object"}}, "required": ["element"]},
    },
    {
        "name": "add_branch",
        "description": "画矢量分支图示：编号箭头/时间轴/图标网格/中心放射，全原生形状双击可改",
        "input_schema": {"type": "object", "properties": {"element": {"type": "object"}}, "required": ["element"]},
    },
    {
        "name": "add_scrim",
        "description": "焦点保护渐变遮罩：洗白文字侧、保留图片焦点侧的原生可编辑版式",
        "input_schema": {"type": "object", "properties": {"element": {"type": "object"}}, "required": ["element"]},
    },
    {
        "name": "add_toc",
        "description": "目录页：编号+章节名+说明的原生文字条目，配背景图+渐变遮罩",
        "input_schema": {"type": "object", "properties": {"element": {"type": "object"}}, "required": ["element"]},
    },
    {
        "name": "set_layer_order",
        "description": "按 layer 字段排序写入，保证 背景→蒙版→装饰→遮罩→文字",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "save",
        "description": "保存为原生可编辑 .pptx",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
]

_TOOL_BY_TYPE = {
    "text": "add_text",
    "shape": "add_shape",
    "mask_picture": "set_picture_fill",
    "picture": "add_picture_layer",
    "decorate": "add_decoration",
    "branch": "add_branch",
    "scrim": "add_scrim",
    "toc": "add_toc",
}


class PPTToolkit:
    def __init__(self, builder: PPTBuilder, slide, assets: dict[str, str], on_call: Callable | None = None):
        self.builder = builder
        self.slide = slide
        self.assets = assets
        self.on_call = on_call
        self.calls: list[dict] = []

    def call(self, name: str, **payload) -> Any:
        self.calls.append({"tool": name, "args": {k: (v.get("element_id") if isinstance(v, dict) else v) for k, v in payload.items()}})
        if self.on_call:
            self.on_call(name, payload)
        if name == "set_background":
            return self.builder.set_background(self.slide, payload["bg"], self.assets)
        if name == "add_text":
            return self.builder.add_text(self.slide, payload["element"])
        if name == "add_shape":
            return self.builder.add_shape(self.slide, payload["element"])
        if name == "set_picture_fill":
            return self.builder.add_mask_picture(self.slide, payload["element"], self.assets)
        if name == "add_picture_layer":
            return self.builder.add_picture_layer(self.slide, payload["element"], self.assets)
        if name == "add_decoration":
            return self.builder.add_decoration(self.slide, payload["element"])
        if name == "add_branch":
            return self.builder.add_branch(self.slide, payload["element"])
        if name == "add_scrim":
            return self.builder.add_scrim(self.slide, payload["element"])
        if name == "add_toc":
            return self.builder.add_toc(self.slide, payload["element"])
        if name == "set_layer_order":
            return []  # 元素已按 z 排序后逐层写入
        if name == "save":
            return self.builder.prs.save(payload["path"])
        raise KeyError(f"unknown tool: {name}")

    def call_for_element(self, element: dict) -> Any:
        return self.call(_TOOL_BY_TYPE[element["type"]], element=element)

    @staticmethod
    def specs() -> list[dict]:
        return TOOL_SPECS
