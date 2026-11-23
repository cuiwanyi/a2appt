"""Agent 3 / 步骤 3：按版式 JSON 调工具层构建原生可编辑 PPTX。"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from run_paths import DRAFT_DECK, FIRST_DRAFT
from ppt.preview import render_layout_preview

from a2a.base import BaseAgent
from a2a.types import AgentSkill, Task, data_part, file_part, json_dump, text_part
from ppt.builder import PPTBuilder
from ppt.tools import PPTToolkit, TOOL_SPECS
from services.prompts import BUILDER_SYSTEM

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class BuilderAgent(BaseAgent):
    NAME = "builder"
    VERSION = "1.0.0"
    DESCRIPTION = "把版式 JSON 转成原生文本框/形状/图片填充蒙版/矢量装饰，图层顺序严格从底到顶"
    SKILLS = [
        AgentSkill(
            id="build_pptx",
            name="构建可编辑 PPTX",
            description="按元素类型调用 MCP 风格工具；文字永不进图；蒙版用 OOXML 图片填充",
            input_schema={"layout": "object", "assets": "map"},
            output_schema={"pptx": "file", "build_report": "03_build_report.json"},
        ),
    ]

    def handle(self, task: Task) -> None:
        layout = self.ctx.layout or {}
        attempt = int(task.metadata.get("attempt", 1))
        builder = PPTBuilder()
        # 把版式里的 VI 主/辅色注入 builder，分支图示原语据此配色
        vi = layout.get("vi") or {}
        builder._vi_primary = str(vi.get("primary") or "#0B2A4A")
        builder._vi_secondary = str(vi.get("secondary") or "#C9A86A")
        tool_log: list[dict] = []
        tools = TOOL_SPECS

        def on_call(name: str, payload: dict) -> None:
            el = payload.get("element")
            tool_log.append({"tool": name, "element_id": el["element_id"] if el else None})
            self.bus.emit("tool.call", agent=self.NAME, tool=name,
                          element_id=el["element_id"] if el else None, attempt=attempt)

        from ppt.schema import clean_closing_title
        clean_closing_title(layout)
        specs = self.ctx.brief.get('page_specs') or []
        for i, page in enumerate(layout["pages"]):
            # 多页总述可能同时列出满幅和独立信息栏，只传当前页的要求。
            spec = specs[i] if i < len(specs) else {}
            if not specs and len(layout['pages']) == 1:
                spec = self.ctx.brief
            if len(layout['pages']) > 1 and page['bg']['type'] == 'image':
                page['bg']['measure_focus'] = True
            slide = builder.prs.slides.add_slide(builder.blank_layout)
            builder.set_page_context(page, spec)
            tk = PPTToolkit(builder, slide, self.ctx.assets, on_call=on_call)
            tk.call("set_background", bg=page["bg"])
            for el in page["elements"]:  # 已按 z 排好序，写入顺序即图层顺序
                tk.call_for_element(el)
            tk.call("set_layer_order")

        out = self.ctx.path("03_deck.pptx")
        builder.prs.save(str(out))
        attempt_copy = self.ctx.path(f"03_deck_attempt{attempt}.pptx")
        shutil.copy2(out, attempt_copy)
        self.ctx.deck_path = str(out)
        if attempt == 1 and not (self.ctx.root / FIRST_DRAFT).exists():
            # 发布目录是一次提交点：下载和截图不会读到半份文件，返修不再进入此分支。
            target = self.ctx.root / FIRST_DRAFT
            with tempfile.TemporaryDirectory(dir=self.ctx.root, prefix='.draft-') as work:
                stage = Path(work) / FIRST_DRAFT
                stage.mkdir()
                shutil.copyfile(attempt_copy, stage / DRAFT_DECK)
                try:
                    for i, page in enumerate(layout['pages'], 1):
                        render_layout_preview(dict(layout, pages=[page]), self.ctx.assets,
                                              stage / f'slide_{i}_approx.png', scale=80)
                except Exception as exc:
                    # 近似预览失败不应阻止 PPTX 发布和后续实际渲染。
                    for image in stage.glob('*.png'):
                        image.unlink()
                    self.log('首稿近似预览不可用，可下载 PPTX 查看', reason=str(exc))
                stage.rename(target)
            self.bus.emit('draft.ready', agent=self.NAME, attempt=1, status='pending_review',
                          pptx=str(target / DRAFT_DECK))
            print(f'[A2A] 首稿已生成（待审核，非交付稿）：{target / DRAFT_DECK}', flush=True)

        report = {
            "pptx": str(out),
            "attempt": attempt,
            "tool_specs": [t["name"] for t in tools],
            "tool_calls": tool_log,
            "shape_map": {k: [s.name for s in v] for k, v in builder.shape_map.items()},
            "slice_usage": builder.slice_usage,
            "warnings": builder.warnings,
            "focus_evidence": builder.focus_evidence,
            "builder_system_prompt": BUILDER_SYSTEM,
        }
        self.ctx.path("03_effective_layout.json").write_text(json_dump(layout), encoding="utf-8")
        rpath = self.ctx.path("03_build_report.json")
        rpath.write_text(json_dump(report), encoding="utf-8")
        self.log("PPTX 已生成", shapes=sum(len(v) for v in builder.shape_map.values()),
                 tool_calls=len(tool_log), warnings=len(builder.warnings), attempt=attempt)
        self.artifact(
            task, "deck_pptx",
            file_part(out, PPTX_MIME),
            data_part(report),
            file_part(rpath, "application/json"),
            text_part(f"attempt={attempt}, tools={len(tool_log)}"),
            stage=3, kind="file",
        )
