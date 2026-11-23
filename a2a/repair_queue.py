"""以页、规则和目标建立返修清单；只有同一检查的新证据能关闭旧问题。"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from copy import deepcopy

from a2a.types import json_dump, text_part


PAGE = re.compile(r'第\s*(\d+)\s*页\s*[：:]?\s*')
RULE = re.compile(r'(?:视觉评审\s*[:：]\s*)?([\w.\[\]-]+(?::[\w.-]+)?)\s*[:：]\s*(.+)', re.S)
LEDGER = '04_repair_ledger.json'


def _observations(ctx, report):
    failures, passes = {}, set()
    visual = {p['page_index']: p for p in report.get('visual_review', {}).get('pages', [])}
    evidence = {}
    for source, pages in [('visual', visual.values()), ('structure', report.get('pages', []))]:
        for p in pages:
            if source == 'visual' and p.get('status') != 'ok':
                continue
            for c in p.get('checks', []):
                key = (p['page_index'], source, c.get('rule') or c.get('check'))
                evidence.setdefault(key, []).append(c)
    for key, checks in evidence.items():
        if all(c['ok'] for c in checks):
            passes.add(key)
    pdf = report.get('pdf_text_layer', {}).get('details', [])
    for row in pdf:
        key = (row['page_index'], 'pdf', 'pdf_text:'+row['element_id']+':'+row['expected'])
        if row['found_in_pdf_text_layer']:
            passes.add(key)
        else:
            failures[key] = 'PDF 文本层缺失：'+row['expected']
    for text in report.get('issues', []):
        if (text in report.get('repair_limits', []) or text in report.get('unverified_issues', []) or text.startswith('待复核：') or
                (pdf and text.startswith('PDF 文本层缺失文字'))):
            continue
        m = PAGE.match(text)
        n = int(m[1]) if m else (1 if text.startswith('封面') else len(ctx.layout['pages']) if text.startswith('末页') else 2 if text.startswith('整deck缺目录页') else 0)
        body = text[m.end():] if m else text
        match = RULE.fullmatch(body)
        rule = match[1] if match else body.split('：')[0]
        src = 'visual' if body.startswith('视觉评审') else ('structure' if (n, 'structure', rule) in evidence else 'inspection')
        key = (n, src, rule)
        failures[key] = '；'.join(dict.fromkeys(filter(None, [failures.get(key), match[2] if match else body])))
    for overlay in report.get('overlay_reviews', []):
        n, reason = overlay['page'], overlay['reason']
        key = (n, 'visual', 'overlay_reading_zones')
        if reason == 'clear':
            passes.add(key)
        elif reason != 'unverified':
            failures.setdefault(key, reason)
    for f in report.get('image_repairs', []):
        n, ids = f['page'], f.get('asset_ids') or []
        kind = f.get('reason')
        rule = ('overlay_reading_zones' if kind == 'bg_object' else 'storefront_sign:' + ','.join(ids) if kind == 'storefront' else 'image_relevance')
        if kind is None and len(ids) == 1:
            item_rule = 'item_relevance:' + ids[0]
            if (n, 'visual', item_rule) in failures:
                rule = item_rule
            if any(k[0] == n and k[2] == '插图 ' + ids[0] for k in failures):
                continue
        failures.setdefault((n, 'visual', rule), f.get('detail', rule))
    return failures, passes, visual


def _save(ctx, ledger):
    ctx.path(LEDGER).write_text(json_dump(ledger), encoding='utf-8')
    ctx.path(f'04_repair_ledger_attempt{ledger["attempt"]}.json').write_text(json_dump(ledger), encoding='utf-8')


def update_review_ledger(ctx, report, attempt):
    """在每次审核后调用（含最终轮），同步报告门禁并写出本轮快照。"""
    old = getattr(ctx, '_repair_ledger', {'items': []})
    current, passes, visual = _observations(ctx, report)
    items = {(r['page'], r['source'], r['rule']): r for r in old['items']}
    for key, detail in current.items():
        n, source, rule = key
        r = items.setdefault(key, dict(page=n, source=source, rule=rule, observations=[], actions=[]))
        r['observations'] = [x for x in r['observations'] if x['attempt'] != attempt] + [dict(attempt=attempt, detail=detail)]
        r.update(status='open', detail=detail, last_seen=attempt)
    for key, r in items.items():
        if key in current or r['status'] == 'resolved':
            continue
        n, source, rule = key
        inspection_complete = ('pages' in report and len(report['pages']) == len(ctx.layout['pages']))
        # 非逐项诊断不能靠一次列表消失关闭：仅对每轮确实重跑的检查承认阴性结果。
        checked_anew = (rule.startswith('插图 ') or (not ctx.mock and (
            rule.startswith(('整deck缺目录页','封面是纯色背景','末页图片','时间轴节点')))))
        positive = key in passes or (source == 'inspection' and inspection_complete and (
            report.get('pdf_text_layer', {}).get('ok') if rule.startswith('PDF') else checked_anew))
        if rule.startswith('插图 ') and not (n in visual and visual[n].get('status') == 'ok'):
            positive = False
        if r['actions'] and r['actions'][-1]['result'] == 'asset_failed':
            positive = False
        r['status'] = 'resolved' if positive else 'awaiting_evidence'
        if positive:
            r['resolved_at'] = attempt
        else:
            msg = f'待复核：第{n}页 {rule}：{r["detail"]}'
            if msg not in report.setdefault('issues', []):
                report['issues'].append(msg)
            report.update(ok=False, quality_status='needs_manual_repair' if report.get('quality_status') != 'unverified' else 'unverified')
    report.setdefault('summary', {})['blocking_issues'] = len(report.get('issues', []))
    from ppt.image_layout import full_bleed_rule
    contracts = []
    for n, pg in enumerate(ctx.layout['pages'], 1):
        checks = visual.get(n, {}).get('checks', [])
        spec = _spec(ctx, n)
        contracts.append(dict(page=n, name=pg.get('page_name'), original_spec=spec,
            full_bleed=full_bleed_rule(spec), rules=[c['rule'] for c in checks],
            protected_rules=[c['rule'] for c in checks if c['ok']], screenshot=visual.get(n, {}).get('screenshot')))
    ledger = dict(attempt=attempt, items=list(items.values()), pages=contracts, unverified=report.get('unverified_issues', []))
    ctx._repair_ledger = ledger
    report['repair_ledger'] = str(ctx.path(LEDGER))
    _save(ctx, ledger)
    return ledger


def _spec(ctx, n):
    specs = ctx.brief.get('page_specs') or []
    if n <= len(specs):
        return specs[n-1]
    return {k: ctx.brief[k] for k in ('copy','creative','style','full_bleed') if k in ctx.brief} if len(ctx.layout['pages']) == 1 else {}


def _page_context(ctx, report, n, entries, cycle):
    page = ctx.layout['pages'][n-1]
    from ppt.image_layout import full_bleed_rule
    spec = _spec(ctx, n)
    review = next((p for p in report.get('visual_review', {}).get('pages', []) if p['page_index'] == n), {})
    return json.dumps(dict(page=n, 原始要求=spec, 满幅规则=full_bleed_rule(spec), 原文=[e['content'] for e in page['elements'] if e['type']=='text'],
        有效规则=[c.get('rule') for c in review.get('checks', [])], 已通过需保护=[c['rule'] for c in review.get('checks', []) if c['ok']],
        需修=[dict(rule=r['rule'], evidence=r['detail'], prior=r['actions'][-2:]) for r in entries],
        评审建议=review.get('rework_hints', []), 保护='不得改动原文、正确素材、其他页及已通过项；不删除已有元素。',
        策略='上轮修改后仍失败；不重复原参数，优先重新分配可用空间、调整文本框与相邻元素的位置，而非继续缩字。' if cycle else '按证据只修改有问题的元素参数。'), ensure_ascii=False)


def _merge_page(page, proposed, locked):
    # Planner 返回的是全册；只接纳原页同 ID、同类型元素的版式参数，拒绝页序错位和素材替换。
    if proposed.get('page_name') != page.get('page_name'):
        return
    candidates = {el['element_id']: el for el in proposed['elements']}
    allowed = {'position','font','align','z','wash','color','strength','boundary','cols','style'}
    for el in page['elements']:
        if el['element_id'] in locked:
            continue
        candidate = candidates.get(el['element_id'], {})
        if candidate.get('type') != el['type'] or candidate.get('asset_id') != el.get('asset_id'):
            continue
        for k in allowed & candidate.keys():
            el[k] = deepcopy(candidate[k])
    page['elements'].sort(key=lambda el: el.get('z', 0))


def _scrim_patch(page, detail, repeated):
    """仅在截图明示遮罩过强/不足时调节，不从文本遮挡反推其成因。"""
    scrims = [e for e in page['elements'] if e['type'] == 'scrim']
    if len(scrims) != 1:
        return False
    weak = re.search(r'未充分淡化|不够浓|遮罩(?:强度)?不足|渐变(?:太弱|过弱)|遮罩(?:太弱|过弱)', detail)
    strong = re.search(r'洗白(?:过度|了主体)|主体.{0,8}(?:被洗白|被冲淡)|遮罩(?:过强|太浓)|渐变(?:过强|太强)', detail)
    if bool(weak) == bool(strong) or repeated:
        return False
    el = scrims[0]
    old = el.get('strength', .96)
    new = round(min(1., old+.12) if weak else max(.1, old-.16), 3)
    if old == new:
        return False
    el['strength'] = new
    page['_overlay_rework'] = 'scrim_focus_protected'
    return True


def _row_patch(page, decision):
    from ppt.checklist_layout import _text_height
    import config
    target, width = decision.get('target_id'), decision.get('safe_width')
    matches = [e for e in page['elements'] if e['element_id'] == target and e['type'] == 'text']
    if len(matches) != 1 or not isinstance(width, (int, float)) or not .25 <= width < matches[0]['position']['w']:
        return '缺少可用的文本框及收窄宽度证据'
    el = matches[0]; box = el['position']
    bottom = box['y'] + max(box['h'], _text_height(el, width))
    ceiling = min([config.SLIDE_H_IN] + [p['y'] for e in page['elements'] if e['type'] != 'scrim' and
        (p := e['position'])['y'] >= box['y']+box['h']-.001 and p['x'] < box['x']+width and p['x']+p['w'] > box['x']])
    if bottom > ceiling:
        return '收窄后没有足够换行空间'
    box.update(w=width, h=round(bottom-box['y'], 4))
    page['_overlay_rework'] = 'row_wide'


def dispatch(orch, report, attempt):
    ctx = orch.ctx
    ledger = update_review_ledger(ctx, report, attempt-1)
    old = deepcopy(ctx.layout)
    layout = ctx.layout
    limits, jobs, images = [], defaultdict(list), []
    findings = report.get('image_repairs', [])
    decisions = {r['page']: r for r in report.get('overlay_reviews', [])}
    def record(r, strategy, result):
        r['actions'].append(dict(attempt=attempt, strategy=strategy, result=result))
    for r in ledger['items']:
        if r['status'] != 'open':
            continue
        n, rule = r['page'], r['rule']
        page = layout['pages'][n-1] if 1 <= n <= len(layout['pages']) else None
        decision = decisions.get(n, {})
        reason = decision.get('reason')
        tried = [a for a in r['actions'] if a['attempt'] > r.get('resolved_at', 0)]
        why = None
        if not page:
            why = '无法唯一定位受影响页'
        elif len(tried) >= 2:
            why = '两种修法未获复核通过，需人工处理'
        elif rule == 'overlay_reading_zones' and reason == 'row_wide':
            why = _row_patch(page, decision) if not tried else '相同收窄修法未闭环，需要新的截屏定位'
            if not why:
                record(r, '收窄被点名的文本框', 'pending_review'); continue
        elif rule.startswith(('storefront_sign:', 'item_relevance:', '插图 ')) or rule == 'image_relevance' or (rule == 'overlay_reading_zones' and reason == 'bg_object'):
            fs = [f for f in findings if f['page'] == n]
            if rule.startswith('storefront_sign:'):
                fs = [f for f in fs if f.get('reason')=='storefront' and ','.join(f.get('asset_ids') or [])==rule.partition(':')[2]]
            elif rule == 'overlay_reading_zones':
                target = decision.get('target_id')
                fs = [f for f in fs if f.get('reason')=='bg_object' and f.get('asset_ids') == [target] and target == page['bg'].get('image')]
            elif rule.startswith(('item_relevance:', '插图 ')):
                fs = [f for f in fs if f.get('asset_ids') == [rule.split(':',1)[-1].removeprefix('插图 ')]]
            else:
                fs = [f for f in fs if not f.get('reason') and not any(r2['page'] == n and r2['rule'] in ('item_relevance:'+aid, '插图 '+aid) for aid in f.get('asset_ids') or [] for r2 in ledger['items'])]
            if fs:
                for f in fs:
                    images.append((r, dict(f, alternative=bool(tried))))
                continue
            why = '缺少可核对的独立素材目标'
        elif rule == 'overlay_reading_zones' or (reason in ('uncertain','unverified') and r['source'] == 'visual' and rule != 'scrim_focus_protected'):
            why = '阅读区证据不足，未自动改动'
        elif rule == 'scrim_focus_protected':
            scrims = [e for e in page['elements'] if e['type'] == 'scrim']
            if len(scrims) != 1:
                why = '无法唯一定位渐变遮罩'
            elif _scrim_patch(page, r['detail'], tried):
                record(r, '按遮罩证据修改强度', 'pending_review'); continue
            elif reason in ('uncertain', 'unverified'):
                why = '阅读区成因未确认且缺少明确的遮罩修正方向'
            else:
                jobs[n].append(r); continue
        else:
            jobs[n].append(r); continue
        limits.append(f'第{n}页 {rule}：{why}'); record(r, '转人工', why)
    if jobs:
        original_hints = ctx.rework_hints
        ctx.rework_hints = [_page_context(ctx, report, n, entries, any(r['actions'] for r in entries)) for n,entries in jobs.items()]
        proposed = layout
        try:
            # 隔离 Planner 对共享上下文的全册覆盖。
            ctx.layout = deepcopy(layout)
            orch.bus.send('rework_layout', text_part('\n'.join(ctx.rework_hints)), stage=5, attempt=attempt-1)
            proposed = ctx.layout
        except Exception as exc:
            limits.append(f'第{",".join(map(str,jobs))}页 版式返修未完成：{exc}')
        finally:
            ctx.layout, ctx.rework_hints = layout, original_hints
        for n, entries in jobs.items():
            pg, candidate = layout['pages'][n-1], proposed['pages'][n-1]
            if all(r['rule'] == 'scrim_focus_protected' for r in entries):
                scrim = next(e for e in pg['elements'] if e['type']=='scrim')
                el = next((e for e in candidate['elements'] if e['type']=='scrim' and e['element_id']==scrim['element_id']), {})
                if candidate.get('page_name') == pg.get('page_name'):
                    scrim.update({k:el[k] for k in ('strength','wash','boundary','color') if k in el})
                if scrim != next(e for e in old['pages'][n-1]['elements'] if e['element_id']==scrim['element_id']):
                    pg['_overlay_rework'] = 'scrim_focus_protected'
            else:
                baseline = {e['element_id']: e for e in old['pages'][n-1]['elements']}
                locked = {e['element_id'] for e in pg['elements'] if e != baseline[e['element_id']]}
                _merge_page(pg, candidate, locked)
            for r in entries:
                record(r, '改用空间重排' if r['actions'] else '限定本页版式参数', 'pending_review' if pg != old['pages'][n-1] else 'no_change')
    mask_changed = any(el['type'] == 'mask_picture' and el != prev
                       for page, previous in zip(layout['pages'], old['pages'])
                       for el in page['elements'] for prev in previous['elements'] if prev['element_id']==el['element_id'])
    repair = (orch._repair_assets(old, [f for _, f in images], attempt, targeted_only=True)
              if images or mask_changed else dict(attempt=attempt, images=[], limits=[]))
    for r,f in images:
        matches = [c for c in repair['images'] if c['asset_id'] in (f.get('asset_ids') or []) or (not f.get('asset_ids') and c.get('page') == r['page'])]
        success = bool(matches) and all(c.get('applied') for c in matches)
        record(r, '改用不同拍摄构图' if f['alternative'] else '定向重生成', 'pending_review' if success else 'asset_failed')
        if success and f.get('reason') == 'bg_object':
            pg = layout['pages'][r['page']-1]
            pg.setdefault('_overlay_rework', 'bg_object')
    repair['limits'].extend(limits)
    _save(ctx, ledger)
    return repair
