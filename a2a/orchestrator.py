"""A2A 编排器：路线 B 的 5 步流水线 + 单页返修循环。

    Planner  --plan_layout-->      版式 JSON (Artifact)
    Asset    --generate_assets-->  素材文件 (Artifact)
    Builder  --build_pptx-->       原生可编辑 pptx (Artifact)
    Reviewer --review_deck-->      自检报告 (Artifact)
       |-- 不合格 --> Planner(rework_layout) --> Builder(仅重建该页) --> Reviewer
       |-- 合格 / 达到最大返修次数 --> 交付

Orchestrator 只认 AgentCard 里的 skill id，不直接 import 具体 Agent 的方法，
这是 A2A 解耦的关键：换 Agent 实现不用改编排。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from copy import deepcopy
import traceback
from run_paths import allocate_run
from pathlib import Path
from uuid import uuid4

import config
from a2a.base import RunContext
from a2a.bus import A2ABus
from a2a.types import data_part, json_dump, text_part
from services.llm import LLMClient, MockLLM


def _unchanged_blockers(previous, current):
    """有完整两轮证据且所有剩余阻塞均原样未解才停；不以 PPTX 文件名/时间判断。"""
    if not previous or current.get('ok') or previous.get('ok'):
        return []
    issues = current.get('issues') or []
    if not issues or sorted(issues) != sorted(previous.get('issues') or []):
        return []
    def fingerprints(report):
        v = report.get('visual_review') or {}
        rows = v.get('pages') or []
        count = v.get('expected_pages', 0)
        if not count or v.get('status') != 'ok' or len(rows) != count:
            return {}
        out = {r.get('page_index'): r.get('review_input_hash') for r in rows if r.get('status') == 'ok'}
        return out if set(out) == set(range(1, count+1)) and all(out.values()) else {}
    old, new = fingerprints(previous), fingerprints(current)
    if not old or old.keys() != new.keys(): return []
    pages = set()
    for msg in issues:
        m = re.match(r'^第\s*(\d+)\s*页', msg)
        if not m:
            pages = set(new); break
        pages.add(int(m[1]))
    return list(issues) if all(i in new and old[i] == new[i] for i in pages) else []


class Orchestrator:
    def __init__(
        self,
        brief: dict,
        llm=None,
        mock: bool | None = None,
        root: str | Path | None = None,
        max_rework: int | None = None,
        transport: str = "inproc",
        name: str | None = None,
    ):
        self.brief = brief
        self.mock = config.MOCK if mock is None else mock
        self.llm = llm or (MockLLM(brief) if self.mock else LLMClient())
        self.max_rework = config.MAX_REWORK if max_rework is None else max_rework
        self.transport = transport
        self.http_servers: list = []
        self.run_id = uuid4().hex
        title = (brief.get('copy') or {}).get('title') or brief.get('brand')
        self.root = Path(root) if root else allocate_run(config.WORKSPACE, name or title)
        self.root.mkdir(parents=True, exist_ok=True)

        self.ctx = RunContext(
            run_id=self.run_id,
            root=self.root,
            brief=brief,
            llm=self.llm,
            mock=self.mock,
        )
        if transport == "http":
            from a2a.http_transport import HttpA2ABus, start_agent_servers

            self.bus = HttpA2ABus(context_id=self.run_id, trace_file=self.root / "trace.jsonl")
            servers, urls = start_agent_servers(self.ctx, self.bus)
            self.http_servers = servers
            self.bus.set_endpoints(urls)
            for skill in urls:
                self.bus.discover(skill)  # GET /.well-known/agent-card.json
            self.endpoints = {s.url: s.card["name"] for s in servers}
            self.bus.emit("a2a.http_servers", endpoints=self.endpoints, count=len(servers))
        else:
            self.bus = A2ABus(context_id=self.run_id, trace_file=self.root / "trace.jsonl")
            # 延迟导入：避免 a2a <-> agents 的循环依赖
            from agents import AGENT_CLASSES

            for cls in AGENT_CLASSES:
                self.bus.register(cls(self.ctx, self.bus))
            self.endpoints = {}
        (self.root / "agent_cards.json").write_text(json_dump(self.bus.cards()), encoding="utf-8")
        (self.root / "brief.json").write_text(json_dump(brief), encoding="utf-8")

    def _apply_cover_title_art(self) -> dict | None:
        """准备候选版式；由常规 Builder 构建，并在交付前审核同一份文件。"""
        from services.cover_title_art import (
            find_cover_title, title_prompt, matte_and_recolor, enlarged_art_position,
        )
        from services.asset_candidates import write_gallery

        layout = deepcopy(self.ctx.layout)
        t = find_cover_title(layout)
        if not t:
            return None
        title = str(t["content"]).strip()
        color = str((t.get("font") or {}).get("color") or "#EDE6D6")
        prompt = title_prompt(title)

        out_dir = self.root / "06_cover_art"
        cand_dir = out_dir / "candidates"
        cand_dir.mkdir(parents=True, exist_ok=True)
        n = config.COVER_TITLE_ART_N
        manifest = []
        raw_pngs: list[Path] = []
        for i in range(n):
            raw = cand_dir / f"raw_{i + 1}.png"
            rgba = cand_dir / f"title_rgba_{i + 1}.png"
            self.llm.image(prompt, raw)
            matte_and_recolor(raw, rgba, color)
            raw_pngs.append(rgba)
            manifest.append({"asset_id": "cover_title_art", "path": str(rgba),
                             "candidates": [str(rgba)], "kind": "title_art",
                             "source": "llm", "similar_groups": []})
        # 挑图页（候选>1 时留档供人工复核，默认采用第1张）
        gallery = write_gallery(out_dir, manifest)
        chosen = raw_pngs[0]

        # 按艺术字比例等比缩放，居中放进原主标题框
        from PIL import Image
        with Image.open(chosen) as im:
            aw, ah = im.size
        pos = enlarged_art_position(t["position"], aw, ah)

        first = layout["pages"][0]
        new_el = {"element_id": "p1_title_art", "type": "picture",
                  "asset_id": "cover_title_art", "content": "", "position": pos,
                  "layer": "text", "note": f"封面主标题艺术字，应清晰显示：{title}", "z": 6}
        elements = first["elements"]
        idx = elements.index(t)
        elements[idx] = new_el
        elements.sort(key=lambda e: e.get('z', 0))
        (self.root / "01_layout_coverart.json").write_text(json_dump(layout), encoding="utf-8")

        # 复制艺术字为素材并注册
        art_asset = self.root / "02_assets" / "cover_title_art.png"
        shutil.copy2(chosen, art_asset)
        self.ctx.assets['cover_title_art'] = str(art_asset)
        self.ctx.layout = layout
        return {"title": title, "candidates": n, "gallery": str(gallery)}

    def _rework(self, report, attempt):
        from a2a.repair_queue import dispatch
        return dispatch(self, report, attempt)

    def _repair_assets(self, old_layout, findings, attempt, *, targeted_only=False):
        from ppt.schema import asset_requests
        from ppt.slicing import mask_geometry, prepare_mask_slices, src_rect_for
        from services.prompts import asset_prompt
        from PIL import Image

        layout = self.ctx.layout
        def requests(plan):
            return {r['asset_id']: r for r in asset_requests(plan, self.brief)}

        old, new = requests(old_layout), requests(layout)
        targets = {} if targeted_only else {aid: '版式修改了生图要求' for aid in new if old.get(aid) != new[aid]}
        blocked, alternatives = [], set()
        owners = {}
        for n, pg in enumerate(layout['pages']):
            aids = [pg['bg'].get('image')] + [e.get('asset_id') for e in pg['elements']]
            aids.extend(a for e in pg['elements'] for a in (e.get('mask') or {}).get('asset_ids', []))
            for aid in aids:
                owners.setdefault(aid, set()).add(n)
        for finding in findings:
            idx = finding['page'] - 1
            page = layout['pages'][idx]
            if finding.get('reason') == 'storefront':
                ids = {aid for aid, req in new.items() if req['page'] == idx + 1}
                named = finding.get('asset_ids')
                if (not isinstance(named, list) or len(named) != 1 or not isinstance(named[0], str) or named[0] not in ids
                        or new[named[0]]['kind'] != 'storefront'):
                    blocked.append(f'第{idx+1}页缺少唯一的门面照片目标，未换图')
                    continue
            else:
                ids = {aid for aid, req in new.items() if req['page'] == idx+1 and aid != 'cover_title_art'}
            named = finding.get('asset_ids')
            chosen = ids.intersection(s for s in named if isinstance(s, str)) if isinstance(named, list) else set()
            if not named and len(ids) == 1:
                chosen = ids
            if named and (not isinstance(named, list) or len(chosen) != len(named)):
                chosen = set()
            if not chosen:
                blocked.append(f"第{idx+1}页错图未能定位到唯一素材：{finding.get('detail', '')}")
            for aid in sorted(chosen):
                if owners.get(aid, {idx}) != {idx}:
                    blocked.append(f'第{idx+1}页 {aid} 为跨页共享素材，未修改')
                    continue
                if finding.get('alternative'):
                    alternatives.add(aid)
                targets[aid] = f"第{idx+1}页 {page['page_name']}，纠正截图中的错误：{finding.get('detail', '')}"
        manifest_file = self.ctx.path('02_assets', 'manifest.json')
        manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
        entries = {entry['asset_id']: entry for entry in manifest['images'] if entry['asset_id'] in new}
        changes = []
        for aid, reason in targets.items():
            if aid == 'cover_title_art':
                continue
            req = new[aid]
            prior = self.ctx.assets.get(aid)
            before = hashlib.sha256(Path(prior).read_bytes()).hexdigest() if prior and Path(prior).is_file() else None
            pixel_changed = None
            out = self.ctx.path('02_assets', f'rework_{attempt}', f'{len(changes)+1}.png')
            source = req['prompt']
            if req['kind'] == 'bg' and source.lstrip().startswith('{'):
                # 结构化背景只消费首个 JSON；返修意见必须写进 framing，不能留在末尾被忽略。
                try:
                    target, _ = json.JSONDecoder().raw_decode(source)
                    retry = reason
                    if '更换机位' not in reason:
                        retry += '；只改变主摄影构图，避开文字框与页脚禁入区，不生成文字'
                    target['framing'] = (str(target.get('framing') or '').rstrip('。')
                                         + '。返修要求：' + retry).strip()
                    source = json.dumps(target, ensure_ascii=False)
                except ValueError:
                    source = req['prompt'] + '。' + reason
            else:
                source = req['prompt'] + '。' + reason
            prompt = asset_prompt(source, req['kind'],
                                  tone=req.get('tone'), industry=req.get('industry'))
            if aid in alternatives:
                prompt += '。更换机位与景别重新拍摄，不沿用上次的构图；保留指定主体与留白区。'
            after, applied = None, False
            try:
                self.llm.image(prompt, out, size=req['size'])
                with Image.open(out) as image:
                    image.verify()
                after = hashlib.sha256(out.read_bytes()).hexdigest()
                if before is not None:
                    with Image.open(prior) as a, Image.open(out) as b:
                        pixel_changed = a.size != b.size or a.convert('RGBA').tobytes() != b.convert('RGBA').tobytes()
                    if not pixel_changed:
                        raise ValueError('新旧图片像素相同')
                self.ctx.assets[aid] = str(out)
                applied = True
                source = 'mock' if self.mock else 'llm'
                entries[aid] = dict(req, prompt=prompt, source=source, path=str(out), sha256=after)
                for page in layout['pages']:
                    if page['bg'].get('image') == aid:
                        page['bg'].update(asset_sha256=after, asset_source=source)
            except Exception as exc:
                blocked.append(f'{aid} 未证实换图：{exc}')
            changes.append(dict(asset_id=aid, page=req['page'], applied=applied, before=before, after=after, pixel_changed=pixel_changed,
                                reason=reason, path=self.ctx.assets.get(aid)))
        manifest['images'] = ([entries.get(e['asset_id'], e) for e in manifest['images']] if targeted_only
                              else list(entries.values()))
        refreshed = {c['asset_id'] for c in changes if c['applied']}
        slices = {m['asset_id']: m for m in manifest.get('mask_slices', [])} if targeted_only else {}
        # 只刷新受影响的母图、子图或蒙版几何，不重写其他页切片。
        for n, page in enumerate(layout['pages']):
            previous = {e['element_id']: e for e in old_layout['pages'][n]['elements']}
            for el in page['elements']:
                if el['type'] == 'mask_picture':
                    aid = el['asset_id']
                    before_el = previous.get(el['element_id'], {})
                    if (targeted_only and not refreshed.intersection([aid] + list(el['mask'].get('asset_ids') or []))
                            and all(el.get(k) == before_el.get(k) for k in ('position','mask'))):
                        continue
                    paths = prepare_mask_slices(el, self.ctx.assets, self.ctx.path('02_assets', f'slices_{attempt}'))
                    if len(paths) != el['mask']['count'] or not all(paths):
                        blocked.append(f"{el['asset_id']} 未获得完整的新版蒙版切片")
                    for i, path in enumerate(paths):
                        if path:
                            self.ctx.assets[f"{el['asset_id']}__{i}"] = path
                    slices[aid] = dict(asset_id=aid, layout=el['mask']['layout'],
                        continuous=el['mask']['continuous'], count=len(paths), slices=paths,
                        geometry=mask_geometry(el), src_rect_fallback=src_rect_for(el))
        manifest['mask_slices'] = list(slices.values())
        manifest_file.write_text(json_dump(manifest), encoding='utf-8')
        return {'attempt': attempt, 'images': changes, 'limits': blocked}

    def run(self) -> dict:
        attempts = 0
        error = None
        repairs = []
        previous_review = None
        visual_stats = []
        stop_reason = None
        deliverable = None
        layout_file = self.root / '01_layout.json'
        try:
            self.bus.emit("pipeline.start", mode="mock" if self.mock else "live",
                          transport=self.transport,
                          llm_model=getattr(self.llm, "llm_model", "?"),
                          image_model=getattr(self.llm, "image_model", "?"),
                          max_rework=self.max_rework)

            # 步骤 1：需求拆解与版式规划
            self.bus.send(
                "plan_layout",
                data_part(self.brief),
                text_part("输出结构化版式 JSON，文字与图片严格分层"),
                stage=1,
            )

            # 步骤 2：素材生成
            self.bus.send(
                "generate_assets",
                text_part("按 asset_id 逐个生成无文字主视觉，装饰用原生矢量形状"),
                stage=2,
            )

            if config.COVER_TITLE_ART and not self.mock and self.ctx.layout:
                try:
                    art = self._apply_cover_title_art()
                    if art:
                        layout_file = self.root / '01_layout_coverart.json'
                        self.bus.emit('cover_art.applied', **art)
                except Exception as exc:
                    self.bus.emit('cover_art.skipped', reason=f'{type(exc).__name__}: {exc}')

            # 步骤 3/4：构建 -> 自检 -> 返修循环
            attempt = 1
            while True:
                attempts = attempt
                self.bus.send("build_pptx", text_part("按版式 JSON 调工具构建原生可编辑 PPTX"),
                              stage=3, attempt=attempt)
                self.bus.send("review_deck", text_part("逐页自检并输出报告"),
                              stage=4, attempt=attempt)
                report = self.ctx.review or {}
                stats = report.get('visual_review', {}).get('stats')
                if stats:
                    visual_stats.append(dict(attempt=attempt, **stats))
                limits = repairs[-1]['limits'] if repairs else []
                # 执行限制不是新的审查意见：不要把“转人工”再派给规划器。
                from a2a.repair_queue import update_review_ledger
                from a2a.review_outage import detect_review_outage
                ledger = update_review_ledger(self.ctx, report, attempt)
                outage = detect_review_outage(report)
                if outage:
                    stop_reason = dict(outage, attempt=attempt)
                    report['stop_reason'] = stop_reason
                    report['quality_status'] = 'unverified'
                if limits and not outage:
                    report['repair_limits'] = list(limits)
                    report['issues'] = list(dict.fromkeys([*report['issues'], *limits]))
                    report.update(ok=False, quality_status='needs_manual_repair')
                    report['summary']['blocking_issues'] = len(report['issues'])
                other_work = any(item['status'] == 'open' and item['page'] > 0 and
                                 (not item['actions'] or item['actions'][-1]['strategy'] != '转人工')
                                 for item in ledger['items'])
                stalled = None if outage else _unchanged_blockers(previous_review, report)
                if stalled:
                    stop_reason = {'code':'no_progress', 'attempt':attempt, 'issues':stalled,
                                   'detail':'返修后审查输入及阻塞项未变，停止重复返修，保留候选稿'}
                    report['stop_reason'] = stop_reason
                report['asset_rework'] = repairs.copy()
                self.ctx.path('04_review.json').write_text(json_dump(report), encoding='utf-8')
                self.ctx.path(f'04_review_attempt{attempt}.json').write_text(json_dump(report), encoding='utf-8')
                if outage:
                    self.bus.emit('review.outage', **stop_reason)
                    break
                if stalled:
                    self.bus.emit('rework.no_progress', **stop_reason)
                    break
                if (limits and not other_work) or (not report.get('ok') and not self.ctx.rework_hints and not other_work):
                    self.bus.emit('rework.manual_required', attempt=attempt, issues=report.get('issues'))
                    break
                if report.get("ok"):
                    # 上一轮自检已经没有阻塞项，不再无谓返修，直接准备交付。
                    self.bus.emit("rework.converged", attempt=attempt,
                                  note="本轮自检无阻塞项，提前结束返修循环")
                    break
                if attempt > self.max_rework:
                    self.bus.emit("rework.exhausted", attempt=attempt,
                                  max_rework=self.max_rework)
                    break
                self.bus.emit("rework.request", attempt=attempt, hints=self.ctx.rework_hints)
                attempt += 1
                previous_review = deepcopy(report)
                repair = self._rework(report, attempt)
                repairs.append(repair)
                self.bus.emit('rework.assets', **repair)
                layout_file = self.ctx.path('01_layout_rework.json')
                layout_file.write_text(json_dump(self.ctx.layout), encoding='utf-8')

            if report.get('ok') and self.ctx.deck_path and Path(self.ctx.deck_path).exists():
                final = self.root / 'deliverable.pptx'
                shutil.copy2(self.ctx.deck_path, final)
                deliverable = str(final)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            (self.root / "error.txt").write_text(
                error + "\n\n" + traceback.format_exc(), encoding="utf-8"
            )
            self.bus.emit("pipeline.error", error=error)
        finally:
            for srv in self.http_servers:
                srv.stop()
            if self.http_servers:
                self.bus.emit("a2a.http_servers.stopped", count=len(self.http_servers))

        review = self.ctx.review or {}
        summary = {
            "run_id": self.run_id,
            "root": str(self.root),
            "mode": "mock" if self.mock else "live",
            "transport": self.transport,
            "a2a_endpoints": self.endpoints,
            "ok": bool(review.get("ok")) and error is None,
            "error": error,
            "attempts": attempts,
            "max_rework": self.max_rework,
            "deliverable": deliverable,
            "candidate": self.ctx.deck_path,
            "quality_status": review.get('quality_status', 'unverified') if not error else 'error',
            "stop_reason": stop_reason,
            "visual_review_rounds": visual_stats,
            "repair_ledger": review.get('repair_ledger'),
            "rework_count": len(repairs),
            "asset_rework": repairs,
            "layout": str(self.root / '03_effective_layout.json') if (self.root / '03_effective_layout.json').is_file() else str(layout_file),
            "assets_dir": str(self.root / "02_assets"),
            "review": str(self.root / "04_review.json"),
            "review_summary": review.get("summary", {}),
            "blocking_issues": review.get("issues", []),
            "warnings": review.get("warnings", []),
            "rendering": review.get("rendering", {}),
            "layout_preview": review.get("layout_preview", {}).get("path"),
            "editable_baseline": review.get("editable_baseline", {}),
            "agent_cards": self.bus.cards(),
            "task_events": len(self.bus.events),
        }
        (self.root / "05_summary.json").write_text(json_dump(summary), encoding="utf-8")
        self.bus.emit("pipeline.end", ok=summary["ok"], attempts=attempts)
        self.bus.write_trace(self.root / "trace_full.json")
        return summary
