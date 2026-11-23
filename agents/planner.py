"""Agent 1 / 步骤 1：需求拆解与版式规划 -> 版式 JSON。

多页需求走「分批规划」：每 2 页一次模型调用，逐页给构图与文案，
最后合并成一个 N 页版式 JSON；单批失败用本地配方兜底，页数硬保证。
"""
from __future__ import annotations

from copy import deepcopy
import random

from a2a.base import BaseAgent
from a2a.types import AgentSkill, Task, data_part, file_part, json_dump, text_part
from ppt.image_layout import full_bleed_rule, photo_spec, text_only
from ppt.schema import bind_page_assets, clean_closing_title, normalize_layout
from services.opening_focus import OPENING_LIMIT, POSITIONS
from services.mock_data import build_mock_layout
from services.prompts import (
    PAGE_BATCH_SYSTEM,
    PLANNER_SYSTEM,
    REWORK_SYSTEM,
    page_batch_user,
    planner_user,
    rework_user,
)

BATCH_SIZE = 2


class PlannerAgent(BaseAgent):
    NAME = "planner"
    VERSION = "1.1.0"
    DESCRIPTION = "把品牌需求、文案、VI 色、创意版式拆成结构化版式 JSON；多页分批规划；接收自检反馈做单页返修"
    SKILLS = [
        AgentSkill(
            id="plan_layout",
            name="需求拆解与版式规划",
            description="输入品牌 brief（可含逐页 page_specs），输出 vi/pages/elements 版式 JSON（文字与图片严格分层）",
            input_schema={"brief": "object"},
            output_schema={"layout": "object", "file": "01_layout.json"},
        ),
        AgentSkill(
            id="rework_layout",
            name="按自检报告返修版式",
            description="只修被点名的元素参数，输出完整版式 JSON，不整套重做、不丢页",
            input_schema={"layout": "object", "issues": "list[str]"},
            output_schema={"layout": "object"},
        ),
    ]

    def _fallback_page(self, spec: dict) -> dict:
        """本地配方兜底：按该页 creative 关键词挑一套构图，保证不缺页。"""
        sub = dict(self.ctx.brief)
        sub.update(spec)
        sub["creative"] = spec.get("creative") or ""
        if spec.get('layout_recipe') is None and self.ctx.brief.get('page_specs'):
            sub.pop('layout_recipe', None)
        sub["copy"] = {**(self.ctx.brief.get("copy") or {}), **(spec.get("copy") or {})}
        raw = build_mock_layout(sub)
        page = normalize_layout(raw, self.ctx.brief)["pages"][0]
        page["page_name"] = spec.get("name") or page["page_name"]
        return page

    def _plan_multipage(self, specs: list[dict], positions: tuple[str, ...]) -> dict:
        """逐批规划，任何一批失败都不影响最终页数。"""
        vi = normalize_layout(build_mock_layout(self.ctx.brief), self.ctx.brief)["vi"]
        if self.ctx.mock:
            self.log("离线多页规划", pages=len(specs), note="mock 走本地配方，不调 API")
            return {"vi": vi, "pages": [self._fallback_page(sp) for sp in specs]}

        pages: list[dict] = []
        for start in range(0, len(specs), BATCH_SIZE):
            batch = specs[start:start + BATCH_SIZE]
            user = page_batch_user(self.ctx.brief, batch, start, len(specs), positions)
            got: list[dict] = []
            for attempt in (1, 2):
                try:
                    raw = self.ctx.llm.chat_json(PAGE_BATCH_SYSTEM, user)
                    part = normalize_layout(raw, self.ctx.brief)
                    got = part["pages"][:len(batch)]
                    if got:
                        break
                except Exception as exc:  # noqa: BLE001
                    self.log(f"第 {start + 1} 批规划失败，重试",
                             attempt=attempt, error=str(exc))
                    user = user + "\n\n请严格只输出一个合法 JSON 对象，pages 长度等于本次要求页数。"
            for j in range(len(got), len(batch)):
                self.log("该页降级本地配方", page=start + j + 1)
                got.append(self._fallback_page(batch[j]))
            pages.extend(got)
            self.log("多页分批完成", start=start + 1, count=len(got))
        return {"vi": vi, "pages": pages}

    def handle(self, task: Task) -> None:
        rework = task.skill == "rework_layout"
        specs = [photo_spec(self.ctx.brief, i) for i in range(len(self.ctx.brief.get('page_specs') or []))]
        multi = not rework and len(specs) >= 2
        old_pages = (self.ctx.layout or {}).get("pages", [])
        system = REWORK_SYSTEM if rework else (PAGE_BATCH_SYSTEM if multi else PLANNER_SYSTEM)

        if rework:
            user = rework_user(self.ctx.layout or {}, self.ctx.rework_hints)
            if old_pages:
                user += (f"\n\n原版式共 {len(old_pages)} 页，返修后必须保持完全相同的页数与页序，"
                         "只改被点名的元素参数。")
        else:
            user = "" if multi else planner_user(self.ctx.brief)

        self.log("版式规划开始", model=getattr(self.ctx.llm, "llm_model", "?"),
                 rework=rework, multi=multi,
                 pages=len(specs) if multi else self.ctx.brief.get("pages", 1))

        layout = None
        last_error = None
        positions = ()
        if multi:
            positions = tuple(random.choice(tuple(POSITIONS)) for _ in specs[:OPENING_LIMIT])
            try:
                layout = self._plan_multipage(specs, positions)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self.log("多页规划异常", error=str(exc))
        else:
            for attempt in (1, 2):
                try:
                    raw = self.ctx.llm.chat_json(system, user)
                    layout = normalize_layout(raw, self.ctx.brief)
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    self.log(f"第 {attempt} 次版式解析失败，重试", error=str(exc))
                    user = user + "\n\n请严格只输出一个合法 JSON 对象，字段遵循契约，不要解释。"
        if layout is None:
            self.log("LLM 版式连续失败，降级为内置模板", error=str(last_error))
            if rework and old_pages:
                layout = deepcopy(self.ctx.layout)
            elif multi:
                layout = {
                    "vi": normalize_layout(build_mock_layout(self.ctx.brief), self.ctx.brief)["vi"],
                    "pages": [self._fallback_page(sp) for sp in specs],
                }
            else:
                layout = normalize_layout(build_mock_layout(self.ctx.brief), self.ctx.brief)

        # 返修可能让模型少给几页：缺的页沿用旧版式，多出的页截掉，页数始终守恒。
        if rework and old_pages:
            if len(layout["pages"]) < len(old_pages):
                layout["pages"].extend(old_pages[len(layout["pages"]):])
            elif len(layout["pages"]) > len(old_pages):
                layout["pages"] = layout["pages"][:len(old_pages)]

        # 在申请素材前落实页规则；同样约束模型结果与本地降级结果。
        for i, page in enumerate(layout['pages']):
            spec = photo_spec(self.ctx.brief, i)
            rule = full_bleed_rule(spec)
            if text_only(spec):
                page['bg'] = dict(type='color', color=page['bg']['color'], image=None)
                page['elements'] = [e for e in page['elements'] if e['type'] not in ('scrim','picture','mask_picture')]
            elif rule is True:
                bg = page['bg']
                if bg['type'] != 'image':
                    bg.update(type='image', image=f'page_{i+1}_background')
                bg['full_bleed'] = True
                page['elements'] = [e for e in page['elements'] if not (
                    e['type'] == 'picture' and not e.get('item_key') and
                    e['position']['w'] > 6 * e['position']['h'] and
                    e['position']['w'] >= 9)]
                if '渐变' in str(spec.get('creative', '')) and not any(e['type'] == 'scrim' for e in page['elements']):
                    template = self._fallback_page(dict(spec, layout_recipe='full_bleed'))
                    scrim = next(e for e in template['elements'] if e['type'] == 'scrim')
                    scrim['element_id'] = f'p{i+1}_background_scrim'
                    page['elements'].append(scrim)
                    page['elements'].sort(key=lambda e: e['z'])
            page['photo_required'] = rule is True

        if not rework and len(layout["pages"]) > 1:
            bind_page_assets(layout)
            for page, pos in zip(layout["pages"], positions):
                if page["bg"]["type"] == "image" and not page['photo_required']:
                    page["bg"]["subject_position"] = pos  # 生图意图，不是成图证据
        elif rework:
            for page, old in zip(layout["pages"], old_pages):
                if old["bg"]["type"] == "image" and page['bg']['type'] == 'image':
                    page["bg"] = dict(old["bg"])  # 返修不重新生图，保留全部页面的已选背景

        clean_closing_title(layout)
        from ppt.toc_diagram import reconcile_contents
        for note in reconcile_contents(layout, specs):
            self.log("目录与实际页序不一致，已校准", **note)

        self.ctx.layout = layout
        name = "01_layout_rework.json" if rework else "01_layout.json"
        out = self.ctx.path(name)
        out.write_text(json_dump(layout), encoding="utf-8")
        self.log(
            "版式 JSON 已产出",
            pages=len(layout["pages"]),
            elements=sum(len(p["elements"]) for p in layout["pages"]),
        )
        self.artifact(
            task, "layout_json",
            data_part(layout),
            file_part(out, "application/json"),
            text_part(f"pages={len(layout['pages'])}"),
            stage=1, kind="data",
        )
