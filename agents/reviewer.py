"""Agent 4 / 步骤 4：自检评审，不合格则把问题交给 Planner 返修。

四层证据，逐层加码：
  1. OOXML 结构自检（ppt/inspector.py）——文字独立、blipFill 蒙版、图层、VI、越界
  2. LibreOffice 真实渲染逐页 PNG（ppt/render.py）——不是近似预览
  3. PDF 文本层比对 —— 硬证据：文字是原生文本对象，没被烤进图片
  4. 多模态 LLM 视觉评审 —— 按步骤 4 规则看真实截图，输出结构化意见
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from inspect import signature
from pathlib import Path

from PIL import Image
from ppt.schema import page_item_targets, storefront_photos
import shutil

import config
from a2a.base import BaseAgent
from a2a.types import AgentSkill, Task, data_part, file_part, json_dump, text_part
from ppt.inspector import check_pdf_text_layer, inspect
from ppt.preview import render_layout_preview
from ppt.render import try_render
from ppt.schema import auto_fit_adjustments, font_substitutions, layout_primitives
from services.prompts import (
    REVIEWER_SYSTEM,
    vision_review_system,
    vision_review_user,
)


def _item_asset_errors(page, targets, assets):
    """同一张图即便换文件名、不同 PNG 编码也不能充当另一行的插图。"""
    bg = page.get('bg', {}).get('image')
    errors, groups = {}, {}
    for aid in dict.fromkeys(([bg] if bg else []) + list(targets)):
        try:
            with Image.open(assets[aid]) as im:
                signature = (im.size, hashlib.sha256(im.convert('RGBA').tobytes()).digest())
            groups.setdefault(signature, []).append(aid)
        except (KeyError, OSError, AttributeError) as exc:
            if aid in targets:
                errors[aid] = f'素材缺失或不可读取：{exc}'
    for ids in groups.values():
        if len(ids) > 1:
            for aid in ids:
                if aid in targets:
                    errors[aid] = '与背景或其他条目像素相同：' + ', '.join(ids)
    return errors


_OVERLAY_RULE = 'overlay_reading_zones'


def _overlay_regions(page, targets):
    """从有效版式定位小图旁的行、其余正文和页脚；独立摄影分栏不适用。"""
    bg = page.get('bg', {})
    if bg.get('type') != 'image' or not bg.get('image') or len(targets or {}) < 4:
        return None
    elements = page['elements']
    width, height = config.SLIDE_W_IN, config.SLIDE_H_IN
    if not any(e['type'] == 'scrim' and abs(e['position']['x']) < .05 and
               abs(e['position']['y']) < .05 and e['position']['w'] >= width-.05 and
               e['position']['h'] >= height-.05 for e in elements):
        return None
    texts = [e for e in elements if e['type'] == 'text' and e.get('content')]
    regions = {}
    for pic in (e for e in elements if e.get('asset_id') in targets):
        p = pic['position']
        if p['w'] > width*.17 or p['h'] > height*.22:
            return None
        side = 'left' if p['x'] + p['w']/2 < width/2 else 'right'
        adjacent = []
        for text in texts:
            t = text['position']
            gap = max(t['x']-p['x']-p['w'], p['x']-t['x']-t['w'], 0)
            if gap <= max(1., p['w']) and p['y']-.04 <= t['y']+min(.1, t['h']/2) <= p['y']+p['h']:
                adjacent.append(text)
        if not adjacent:
            return None
        for text in adjacent:
            eid = text['element_id']
            if eid in regions:
                return None
            regions[eid] = dict(zone=side, text=text['content'], position=text['position'],
                                picture_id=pic['asset_id'], picture_position=p)
    if any(len({r['picture_id'] for r in regions.values() if r['zone'] == side}) < 2
           for side in ('left', 'right')):
        return None
    for text in texts:
        p = text['position']
        regions.setdefault(text['element_id'], dict(zone='footer' if p['y'] >= .82*height else 'body',
                                                   text=text['content'], position=p))
    return regions


def _overlay_decision(check, regions, bg):
    reason, target = check.get('reason'), check.get('target_id')
    observations = check.get('regions')
    if not isinstance(observations, dict) or observations.keys() != regions.keys():
        raise ValueError('缺少实际图文区或页脚的逐区截图证据')
    states = {}
    for eid, obs in observations.items():
        if (not isinstance(obs, dict) or obs.get('state') not in ('clear', 'row_wide', 'bg_object', 'uncertain')
                or not isinstance(obs.get('seen'), str) or not obs['seen'].strip()):
            raise ValueError('无效的区域证据：' + eid)
        states[eid] = obs['state']
    affected = [eid for eid, state in states.items() if state != 'clear']
    if reason == 'clear' and check['ok'] and not affected and target is None:
        return {'reason': reason}
    if not check['ok']:
        if reason == 'row_wide' and affected == [target] and states[target] == 'row_wide' and 'picture_id' in regions[target]:
            width = check.get('safe_width')
            if type(width) in (int, float) and .25 <= width < regions[target]['position']['w']:
                return dict(reason=reason, target_id=target, safe_width=width)
        if (reason == 'bg_object' and target == bg and all(states[e] == reason for e in affected)
                and len({regions[e]['zone'] for e in affected}) >= 2
                and any('picture_id' in regions[e] for e in affected)):
            return dict(reason=reason, target_id=bg, affected=affected)
        if reason == 'uncertain' and target is None:
            return {'reason': reason}
    raise ValueError('原因、目标 ID 与逐区证据不一致')


def _unresolved_layout_issues(ctx, report):
    """把构建阶段没能安全安置的元素提升为可派发的版式问题。"""
    path = ctx.path('03_build_report.json')
    if not path.is_file():
        return []
    try:
        build = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    found = []
    for idx, evidence in enumerate(build.get('focus_evidence') or [], 1):
        ids = evidence.get('unresolved') or []
        if not ids or evidence.get('status') not in ('measured', 'uncertain'):
            continue
        found.append(f"第{idx}页 布局待重排：{', '.join(ids)} 无法在实测留白内安全安置")
    return found


def _visual_rules(page, targets=None):
    critical = {"text_readable", "text_visibility", "no_overlap"}
    system = vision_review_system(page) + ('\n无论首审还是返修，均从头核对所有适用规则，一次列出本页所有可见问题；'
                                          '不要发现首个问题就停止，也不要只检查上次返修项。')
    types = {e['type'] for e in page.get('elements', [])}
    if 'scrim' in types:
        critical.add('scrim_focus_protected')
    if 'toc' in types:
        critical.add('toc_readable')
    if page.get('bg', {}).get('type') == 'image' or types & {'picture', 'mask_picture'}:
        critical.update({'image_filled', 'image_relevance', 'focus_clear'})
        system += ('\n另加两项必检规则：image_relevance（实际摄影主体是否符合本页原始文案及场景；'
                   '不符时在该 check 的 asset_ids 数组列出错图 ID），focus_clear（文字或组件没有盖住摄影焦点）。')
    signs = storefront_photos(page)
    if signs:
        rules = ['storefront_sign:' + aid for aid in signs]
        critical.update(rules)
        system += ('\n逐图必检，各规则恰好一条：' + ', '.join(rules) + '。按素材 ID 对照照片原图与截图中的对应框，'
                   '只判断照片内部：必须是门面；可辨识的门头字、店名、中外文、数字、商标或伪招牌均不合格，'
                   '即便字与本页标题相同；图框外原生标题和指标不算。detail 写明实际可见位置和字样或主体。'
                   '门面错图只记入对应 storefront_sign 规则，不要在 image_relevance 中重复归因。')
    if targets:
        rules = ['item_relevance:' + aid for aid in targets]
        system += '\nimage_relevance 仅评背景和非条目图片；条目错图只记在对应 item_relevance 规则下。'
        critical.update(rules)
        system += ('\n另加以下必检规则，每个规则必须恰有一条 check。按对应行核对截图小图，detail 描述'
                   '实际可见主体；用背景代替、复用另一项、组图或看不清主体一律不可通过。'
                   '背景正确不能代替小图正确。规则名：' + ', '.join(rules))
    if _overlay_regions(page, targets):
        critical.add(_OVERLAY_RULE)
        system += ('\n另加唯一一条 overlay_reading_zones 检查。第一张为成稿，若有第二张则为主背景原图，'
                   '区分独立小图与背景物体。按给出的每个文本框 ID（包括结语、页码）返回 '
                   'regions:{"元素ID":{"state":"clear|row_wide|bg_object|uncertain","seen":"具体可见字、物体及相对位置"}}。'
                   'check.reason：全部清晰为 clear；仅一条步骤文字伸入焦点为 row_wide，target_id 为该文本框 ID，'
                   'safe_width 是保持起点时该行可用宽度（英寸）；仅当背景本身的物体侵占多个阅读区域为 bg_object，'
                   'target_id 必须为主背景素材 ID。混合或证据不足用 uncertain。clear/uncertain 的 target_id 为 null；'
                   '只有 clear 的 ok=true。不得以硬边分栏替代满幅照片与焦点渐变。')
    return system, critical


def _validate_visual(result, critical):
    if not isinstance(result, dict) or result.get('overall') not in ('pass', 'needs_rework'):
        raise ValueError('缺少有效的视觉结论')
    checks = result.get('checks')
    if not isinstance(checks, list) or any(not isinstance(c, dict) or
            not isinstance(c.get('rule'), str) or type(c.get('ok')) is not bool or
            not isinstance(c.get('detail'), str) or not c['detail'].strip() for c in checks):
        raise ValueError('缺少可核对的逐项视觉证据')
    if len({c['rule'] for c in checks}) != len(checks):
        raise ValueError('同一规则返回重复视觉证据')
    missing = critical - {c['rule'] for c in checks}
    if missing:
        raise ValueError('未核验关键规则：' + ', '.join(sorted(missing)))
    item_rules = {r for r in critical if r.startswith('item_relevance:')}
    returned = [c['rule'] for c in checks if c['rule'].startswith('item_relevance:')]
    if set(returned) != item_rules or len(returned) != len(item_rules):
        raise ValueError('逐项视觉证据重复或引用未知素材')
    sign_rules = {r for r in critical if r.startswith('storefront_sign:')}
    returned = [c['rule'] for c in checks if c['rule'].startswith('storefront_sign:')]
    if set(returned) != sign_rules or len(returned) != len(sign_rules):
        raise ValueError('门面招牌证据重复或引用未知照片')
    for key in ('issues', 'rework_hints'):
        if not isinstance(result.get(key), list) or not all(isinstance(s, str) for s in result[key]):
            raise ValueError('视觉结果格式错误：' + key)
    return result


@dataclass
class _PageReview:
    index: int
    screenshot: str | None
    regions: dict | None
    bg_id: str | None
    error: str | None = None
    system: str = ''
    user: str = ''
    critical: set = field(default_factory=set)
    images: list = field(default_factory=list)
    native_inputs: list[bytes] = field(default_factory=list)
    options: dict = field(default_factory=dict)
    fingerprint: str | None = None
    response: dict | None = None
    decision: dict = field(default_factory=lambda: {'reason': 'unverified'})
    calls: int = 0
    reused: bool = False


def _page_screenshots(images, count):
    # 正式渲染文件自带页码，绝不能把缺页后向前移位的截图审在错误页上。
    slots = {}
    paths = Counter(str(Path(p).resolve()) for p in images if p)
    for position, path in enumerate(images, 1):
        if not path:
            continue
        match = re.fullmatch(r'slide_(\d+)_real\.png', Path(path).name, re.I)
        if not match and len(images) != count:
            continue
        index = int(match[1]) if match else position
        if index in slots or paths[str(Path(path).resolve())] != 1:
            slots[index] = None
        else:
            slots[index] = path
    return slots


def _is_limited(exc):
    return (getattr(exc, 'status_code', None) == 429 or
            getattr(getattr(exc, 'response', None), 'status_code', None) == 429 or
            bool(re.search(r'\b429\b|rate[ _-]?limit|too many requests', str(exc), re.I)))


def _native_vision(llm):
    from services.llm import LLMClient
    return getattr(type(llm), 'chat_vision_json', None) is LLMClient.chat_vision_json


def _input_hash(job, llm, page, static_errors):
    # 协议版本覆盖本地判据（含遮罩解释、阻塞/修图归因）；改变其语义时递增。
    # 正式客户端直接发一次确定提示的请求，重试由外层计数，不藏在 JSON 修复调用里。
    model = getattr(llm, 'llm_model', None)
    endpoint = getattr(llm, 'base_url', None)
    hashes = []
    for path in job.images:
        data = Path(path).read_bytes()
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        hashes.append(hashlib.sha256(data).hexdigest())
        if _native_vision(llm):
            job.native_inputs.append(data)  # 实际请求使用同一份字节，避免哈希后路径被覆盖。
    if not isinstance(model, str) or not model.strip():
        return None
    payload = dict(protocol=1, model=model, endpoint=endpoint if isinstance(endpoint, str) else None,
                   client=type(llm).__module__ + '.' + type(llm).__qualname__, options=job.options,
                   blocking=config.VISION_BLOCKING, system=job.system, user=job.user, images=hashes,
                   critical=sorted(job.critical), regions=job.regions, page=page, static_errors=static_errors)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _validate_page(job, candidate):
    candidate = _validate_visual(candidate, job.critical)
    if 'page_index' in candidate and (type(candidate['page_index']) is not int or candidate['page_index'] != job.index):
        raise ValueError('视觉响应页码与本页不符')
    if job.regions:
        check, = [c for c in candidate['checks'] if c['rule'] == _OVERLAY_RULE]
        job.decision = _overlay_decision(check, job.regions, job.bg_id)
    job.response = deepcopy(candidate)


def _call_page(llm, job):
    job.calls += 1
    try:
        if _native_vision(llm):
            from services.llm import _extract_json
            inputs = [io.BytesIO(data) for data in job.native_inputs]
            candidate = _extract_json(llm.chat_vision(job.system, job.user, inputs, **job.options))
        else:
            candidate = llm.chat_vision_json(job.system, job.user, job.images, **job.options)
        _validate_page(job, candidate)
        job.error = None
        return False
    except Exception as exc:
        job.error = f'{type(exc).__name__}: {exc}'
        return _is_limited(exc)


def _run_reviews(llm, jobs, previous, serial, log):
    pending = []
    for job in jobs:
        if job.error:
            continue
        entry = previous.get(job.index)
        if job.fingerprint and entry and entry[0] == job.fingerprint:
            _validate_page(job, entry[1])
            job.reused = True
        else:
            pending.append(job)
    limited = False
    # 每次最多放行两个请求；等在途请求都结束后再重试，限流后不再派发并行任务。
    with ThreadPoolExecutor(max_workers=2) as pool:
        while pending:
            width = 1 if serial else 2
            batch, pending = pending[:width], pending[width:]
            futures = [pool.submit(_call_page, llm, j) for j in batch]
            retry = []
            for job, future in zip(batch, futures):
                if future.result():
                    limited = serial = True
                if job.error:
                    log('视觉评审失败', page=job.index, attempt=job.calls, error=job.error)
                    if job.calls < 2:
                        retry.append(job)
            pending = retry + pending
    return serial, limited


class ReviewerAgent(BaseAgent):
    NAME = "reviewer"
    VERSION = "1.1.0"
    DESCRIPTION = "结构自检 + LibreOffice 真实截图 + PDF 文本层比对 + 多模态视觉评审，产出报告与返修建议"
    SKILLS = [
        AgentSkill(
            id="review_deck",
            name="自检返修评审",
            description="逐页检查并输出报告；把 blocking 问题写成 rework hints 回退给规划 Agent",
            input_schema={"pptx": "file", "layout": "object"},
            output_schema={"review": "04_review.json", "screenshots": "04_screenshots/*.png",
                           "visual_review": "04_visual_review.json", "rework_hints": "list[str]"},
        ),
    ]

    def handle(self, task: Task) -> None:
        attempt = int(task.metadata.get("attempt", 1))
        layout = self.ctx.layout or {}
        self.log("开始自检", attempt=attempt, pptx=self.ctx.deck_path)
        report = inspect(self.ctx.deck_path, layout, attempt=attempt)

        # ---- 1b. 画册级结构硬规则（仅真实流水线；驱动返修，修复用户明确要的目录/遮罩/历程说明/条带图） ----
        if not self.ctx.mock:
            from ppt.inspector import brochure_rule_issues
            brochure = brochure_rule_issues(layout)
            if brochure:
                report["issues"].extend(brochure)
                self.log("画册级规则未达标", count=len(brochure), items=brochure)

        # 构建器无法在实图留白中安置的元素不能只报警；交给整册清单分派重排。
        pending_layout = _unresolved_layout_issues(self.ctx, report)
        if pending_layout:
            report['issues'].extend(pending_layout)
            report['unresolved_layout'] = pending_layout
            self.log('布局未解决，需重新排版', count=len(pending_layout), items=pending_layout)
            report['ok'] = False
            report.setdefault('summary', {})['blocking_issues'] = len(report['issues'])

        # ---- 2. 真实渲染 ----
        rendering = try_render(self.ctx.deck_path, self.ctx.path('04_screenshots', f'attempt{attempt}'))
        report["rendering"] = rendering
        self.log("渲染引擎", status=rendering["status"], renderer=rendering.get("renderer"),
                 pages=rendering.get("page_count", 0))

        # 近似预览始终生成，用于和真实截图做对照（明确标注不是正式渲染）
        preview = render_layout_preview(layout, self.ctx.assets, self.ctx.path("04_layout_preview.png"))
        report["layout_preview"] = {
            "path": str(preview),
            "kind": "approximate",
            "note": "按版式 JSON 用 Pillow 重绘的对照图，不是 PowerPoint 实际渲染；以 04_screenshots 为准",
        }

        visual_hints: list[str] = []
        image_repairs = []
        overlay_reviews = []
        reviews = []
        expected = len(layout['pages'])
        images = rendering.get('images', [])
        unverified = []
        if rendering["status"] == "ok":
            report["screenshots"] = rendering["images"]
            for img in images:
                if img and Path(img).is_file():
                    shutil.copy2(img, self.ctx.path('04_screenshots', Path(img).name))

            # ---- 3. PDF 文本层：文字没被烤进图片的硬证据 ----
            pdf_check = check_pdf_text_layer(rendering["page_texts"], layout)
            report["pdf_text_layer"] = pdf_check
            if not pdf_check["ok"]:
                report["issues"].append(
                    "PDF 文本层缺失文字：" + "、".join(pdf_check["missing"][:4])
                )

        else:
            report['screenshots'] = []

        # 视觉响应成功 ≠ 视觉通过；逐页记载覆盖，不能以部分成功冒充整册通过。
        specs = self.ctx.brief.get('page_specs') or []
        slots = _page_screenshots(images, expected)
        jobs = []
        for i, page in enumerate(layout['pages']):
            idx = i + 1
            signs = storefront_photos(page)
            targets = page_item_targets(page, signs)
            static_errors = _item_asset_errors(page, targets, self.ctx.assets) if targets else {}
            for aid, detail in static_errors.items():
                report['issues'].append(f'第{idx}页 插图 {aid}：{detail}')
                image_repairs.append({'page': idx, 'asset_ids': [aid], 'detail': detail})
            regions = _overlay_regions(page, targets)
            img = slots.get(idx)
            job = _PageReview(idx, img, regions, page['bg'].get('image'))
            jobs.append(job)
            reason = None
            if self.ctx.mock:
                reason = 'mock 模式未做真实视觉评审'
            elif not config.VISION_REVIEW:
                reason = '视觉评审已关闭'
            elif rendering['status'] != 'ok' or not img or not Path(img).is_file():
                reason = '真实渲染或本页截图缺失'
            if reason is None:
                system, critical = _visual_rules(page, targets)
                assets = [e.get('asset_id') for e in page['elements'] if e.get('asset_id')]
                if page['bg'].get('image'):
                    assets.append(page['bg']['image'])
                user = (vision_review_user(idx, layout.get('vi', {}), page) +
                        f'\n本页原始要求：{specs[i] if i < len(specs) else page.get("page_name")}；素材 ID：{assets}')
                if targets:
                    bindings = [{**targets[e['asset_id']], 'asset_id': e['asset_id'], 'position': e['position']}
                                for e in page['elements'] if e.get('asset_id') in targets]
                    user += '\n逐行截图核验目标（位置单位英寸）：' + json.dumps(bindings, ensure_ascii=False)
                visual_inputs = [img]
                if regions:
                    bg_id = page['bg']['image']
                    user += f'\n主背景 ID：{bg_id}；实际阅读区（英寸）：' + json.dumps(regions, ensure_ascii=False)
                    raw_bg = self.ctx.assets.get(bg_id)
                    if raw_bg and Path(raw_bg).is_file():
                        visual_inputs.append(raw_bg)
                    else:
                        reason = f'缺少阅读区对照用主背景原图：{bg_id}'
                for aid, photo in signs.items():
                    raw = self.ctx.assets.get(aid)
                    if not raw or not Path(raw).is_file():
                        reason = f'缺少门面照片原图：{aid}'
                        break
                    visual_inputs.append(raw)
                    user += f'\n第{len(visual_inputs)}张输入是门面原图 {aid}；截图中的图片框（英寸）：{photo["position"]}'
                job.system, job.user, job.critical, job.images = system, user, critical, visual_inputs
                if regions:
                    job.options['max_tokens'] = 5000
                elif _native_vision(self.ctx.llm):
                    job.options['max_tokens'] = 2500
                else:
                    param = signature(self.ctx.llm.chat_vision_json).parameters.get('max_tokens')
                    if param and isinstance(param.default, int):
                        job.options['max_tokens'] = param.default
                if not reason:
                    try:
                        job.fingerprint = _input_hash(job, self.ctx.llm, page, static_errors)
                    except (OSError, ValueError, SyntaxError) as exc:
                        reason = f'无法读取本页视觉输入：{exc}'
            job.error = reason

        # 不从磁盘/其他 run 加载；仅保存紧邻上一轮、经程序验证的原始响应。
        run_id = self.ctx.run_id
        last_run, last_attempt, previous, serial = getattr(self, '_visual_cache', (None, 0, {}, False))
        if attempt == 1 or (last_run, last_attempt) != (run_id, attempt - 1):
            previous, serial = {}, False
        serial, limited = _run_reviews(self.ctx.llm, jobs, previous, serial, self.log)
        self._visual_cache = (run_id, attempt, {j.index: (j.fingerprint, deepcopy(j.response))
                              for j in jobs if j.fingerprint and j.response is not None}, serial)
        for job in jobs:
            idx, img, regions, bg_id = job.index, job.screenshot, job.regions, job.bg_id
            critical, decision, vr, reason = job.critical, job.decision, job.response, job.error
            if vr is None:
                unverified.append(idx)
                vr = {'status': 'unverified', 'overall': 'unknown', 'error': reason}
            else:
                vr['status'] = 'ok'
                failed = [c for c in vr['checks'] if not c['ok']]
                blockers = [c for c in failed if c['rule'] in critical or config.VISION_BLOCKING]
                if blockers or vr['overall'] == 'needs_rework':
                    # 模型明确要求返修而未给出可归类规则，也不可判为通过。
                    blocking = bool(blockers or config.VISION_BLOCKING or not failed)
                    dest = report['issues' if blocking else 'warnings']
                    findings = [f"{c['rule']}: {c['detail']}" for c in blockers] or vr['issues'] or ['需返修']
                    for finding in findings:
                        dest.append(f'第{idx}页 视觉评审: {finding}')
                    if blocking:
                        visual_hints.extend(f'第{idx}页：{h}' for h in vr['rework_hints'])
                for c in blockers:
                    if c['rule'].startswith('storefront_sign:'):
                        aid = c['rule'].split(':', 1)[1]
                        image_repairs.append({'page': idx, 'asset_ids': [aid], 'reason': 'storefront',
                                              'detail': c['detail']})
                    elif c['rule'].startswith('item_relevance:'):
                        aid = c['rule'].split(':', 1)[1]
                        image_repairs.append({'page': idx, 'asset_ids': [aid], 'detail': c['detail']})
                    elif c['rule'] == _OVERLAY_RULE and decision['reason'] == 'bg_object':
                        evidence = '；'.join(f"{eid}：{c['regions'][eid]['seen']}" for eid in decision['affected'])
                        image_repairs.append({'page': idx, 'asset_ids': [decision['target_id']],
                                              'reason': 'bg_object', 'detail': evidence})
                    elif c['rule'] == 'image_relevance' and (not regions or (
                            decision['reason'] == 'clear' and c.get('asset_ids') == [bg_id])):
                        image_repairs.append({'page': idx, 'asset_ids': c.get('asset_ids', []),
                                              'detail': c['detail']})
            if regions:
                overlay_reviews.append(dict(page=idx, **(decision if vr['status'] == 'ok' else {'reason': 'unverified'})))
            reviews.append(dict(vr, page_index=idx, screenshot=img, reused=job.reused,
                                review_input_hash=job.fingerprint if job.response is not None else None))

        coverage_ok = bool(expected and not unverified and len(images) == expected
                           and rendering.get('page_count') == expected and len(set(images)) == expected)
        report['unverified_issues'] = [] if coverage_ok else [
            f'视觉未核验：应审 {expected} 页，截图 {len(images)} 张，未核验页 {unverified}（详见逐页原因）']
        report['issues'].extend(report['unverified_issues'])
        vpath = self.ctx.path('04_visual_review.json')
        report['visual_review'] = {'path': str(vpath), 'status': 'ok' if coverage_ok else 'unverified',
                                   'verified_pages': expected - len(unverified), 'expected_pages': expected,
                                   'pages': reviews, 'model': getattr(self.ctx.llm, 'llm_model', '?'),
                                   'stats': dict(reviewed_pages=sum(j.calls > 0 for j in jobs),
                                                 reused_pages=sum(j.reused for j in jobs),
                                                 model_calls=sum(j.calls for j in jobs),
                                                 unverified_pages=len(unverified), rate_limited=limited,
                                                 serial_fallback=serial)}
        vpath.write_text(json_dump(report['visual_review']), encoding='utf-8')
        report['image_repairs'] = image_repairs
        report['overlay_reviews'] = overlay_reviews

        report["reviewer_system_prompt"] = REVIEWER_SYSTEM
        report["font_substitutions"] = font_substitutions(layout)
        report["auto_fit_adjustments"] = auto_fit_adjustments(layout)
        report["layout_primitives"] = layout_primitives(layout)
        report["ok"] = not report["issues"]
        mask_checks = [
            c for p in report["pages"] for c in p["checks"]
            if c["check"].startswith("mask_picture[")
        ]
        report["editable_baseline"] = {
            "text_is_native_textbox": all(
                c["ok"] for p in report["pages"] for c in p["checks"] if c["check"].startswith("text[")
            ),
            "pdf_text_layer_confirms": report.get("pdf_text_layer", {}).get("ok", False),
            # 本套版式没用形状图片填充时标为 not_applicable，而不是伪装成 True
            "mask_is_picture_fill": (
                all(c["ok"] for c in mask_checks) if mask_checks else "not_applicable"
            ),
            "layer_order_ok": all(
                c["ok"] for p in report["pages"] for c in p["checks"] if c["check"] == "layer_order"
            ),
            "real_screenshots": rendering["status"] == "ok",
            "note": "双击可改字 / 形状可选中 / 图片可单独换 / 装饰可改色",
        }

        hints = [s for s in report['issues'] if s not in report['unverified_issues']]
        self.ctx.rework_hints = list(dict.fromkeys(hints + visual_hints))
        report['rework_hints'] = self.ctx.rework_hints
        report['quality_status'] = ('unverified' if not coverage_ok else
                                    'passed' if report['ok'] else 'needs_rework')
        report['summary'].update(blocking_issues=len(report['issues']), warnings=len(report['warnings']),
                                 visual_verified_pages=expected - len(unverified), visual_expected_pages=expected)
        self.ctx.review = report

        out = self.ctx.path("04_review.json")
        out.write_text(json_dump(report), encoding="utf-8")
        self.log("自检完成", ok=report["ok"], blocking=len(report["issues"]),
                 warnings=len(report["warnings"]), renderer=rendering["status"],
                 visual=report.get("visual_review", {}).get("status"))
        self.artifact(
            task, "review_report",
            data_part(report),
            file_part(out, "application/json"),
            text_part(f"ok={report['ok']}, issues={len(report['issues'])}, renderer={rendering['status']}"),
            stage=4, kind="report", ok=report["ok"],
        )
