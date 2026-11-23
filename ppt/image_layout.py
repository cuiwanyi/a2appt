"""在建稿时量测当前背景的低细节边带；不把构图指令当成图像证据。"""
from __future__ import annotations

import hashlib
import re
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter


def text_only(spec):
    import re
    text = '；'.join(str(spec.get(k) or '') for k in ('creative', 'style'))
    return spec.get('layout_recipe') == 'pure_type' or bool(re.search(
        r'(?<!非)(?<!不做)(?<!不是)(?<!不要)(?:纯文字|纯文本|零图片|无配图|不要配图|不放图片|无图(?!标|案|片背景))', text))


_ZONE = r'(?:横幅|横条|图片区|照片区|侧栏|[上下左右]半[部页区幅]|(?:顶部|底部|上部|下部)(?:区域|图带)|卡片(?:内|中|底部|上部|下部)?)'
_PHOTO = r'(?:摄影|照片|实拍|背景图)'
_LOCAL_PHOTO = re.compile(_ZONE + r'(?:\s*[，,：:]\s*(?=采用|使用|用|放置|配))?[^，,。；;：:\n→]{0,20}?' + _PHOTO + r'(?:背景|铺底)?|' +
                          _PHOTO + r'(?:背景|仅|只|位于|用于|放在|置于|铺在|限于|的|作为)*' + _ZONE)


def _local_clause(spec):
    text = '；'.join(str(spec.get(k) or '') for k in ('creative', 'style'))
    return _LOCAL_PHOTO.search(text)


def full_bleed_rule(spec):
    """只读取本页构图要求，不从页名、正文或默认 scrim 尺寸猜测。"""
    import re
    if text_only(spec):
        return False
    if type(spec.get('full_bleed')) is bool:
        return spec['full_bleed']
    creative = '；'.join(str(spec.get(k) or '') for k in ('creative', 'style'))
    # 只匹配明确的主构图要求。单独小图要求等比裁切不构成背景规则。
    if re.search(r'(?:不要求|无需|不做|不用|禁止)(?:摄影|照片|主图|背景)?(?:满幅|全幅|满版)', creative):
        return False
    photo = r'(?:主摄影|摄影|主照片|照片|主图|背景图?)'
    extent = r'(?:满幅|全幅|满版|整页|铺满四边|铺满全页)'
    if re.search(r'(?:不用|不要|无需|禁止|不使用|不做)(?:满幅|整页)?(?:摄影背景|照片背景|背景图)|(?:摄影背景|满幅摄影)(?:不适用|除外)', creative):
        return False
    # “摄影背景”只有未被横幅/局部区域限定时，才是页级指令；独立的整页要求仍有效。
    local = _local_clause(spec)
    if local:
        creative = _LOCAL_PHOTO.sub('局部区域', creative)
    if re.search(r'(?:摄影|照片)(?:背景|铺底)|整页[^；。，]{0,12}(?:摄影|全景)', creative):
        return True
    if re.search(photo + r'(?:背景|连续|等比裁切|必须|需|要|为|应|采用|，)*' + extent + '|' +
                 extent + r'(?:用一张|用|主|连续|完整|的)*' + photo, creative):
        return True
    if local or re.search(r'(?<!不)(?<!禁止)(?:可以|可|允许|采用|改为)(?:独立)?分栏', creative):
        return False
    return None


def localize_photo(page, spec, index, vi, request_box):
    """把仅服务于局部区域的页背景还原为图片；不改已有横幅、条目图或遮罩。"""
    local = _local_clause(spec)
    bg = page['bg']
    if not local or bg['type'] != 'image':
        return
    import config
    from .schema import _normalize_element

    width, height = config.SLIDE_W_IN, config.SLIDE_H_IN
    text = local.string
    # 像素范围必须紧邻本块说明，并有参考画布；不能借用日期栏、卡片等其它块的坐标。
    canvas = re.search(r'(\d{2,5})\s*[×xX*]\s*(\d{2,5})\s*(?:像素|px)', text, re.I)
    bounds = re.search(r'x\s*=\s*(\d+)\s*[-–—~至]\s*(\d+)\s*[,，、\s]+y\s*=\s*(\d+)\s*[-–—~至]\s*(\d+)[^，,；;。\n=]{0,12}$',
                       text[:local.start()], re.I)
    box = None
    if canvas and bounds:
        cw, ch = map(int, canvas.groups())
        l, r, t, b = map(int, bounds.groups())
        if 0 <= l < r <= cw and 0 <= t < b <= ch and (r-l < cw or b-t < ch):
            box = dict(x=l/cw*width, y=t/ch*height, w=(r-l)/cw*width, h=(b-t)/ch*height)

    photos = [e for e in page['elements'] if e['type'] in ('picture', 'mask_picture')
              and not e.get('item_key') and e.get('layer') in ('picture', 'bg')]

    def matches(p):
        # 允许模型的四舍五入及小幅内缩，但不能把覆盖全页的框或图标误当横幅。
        return all(abs(p[k] - box[k]) <= max(.035, box[d]*.08)
                   for k, d in (('x','w'), ('w','w'), ('y','h'), ('h','h')))

    if box is None and '横幅' in local.group():
        banners = [e['position'] for e in photos if e['position']['w'] >= .7*width
                   and .08*height <= e['position']['h'] <= .6*height
                   and re.search(_PHOTO, e.get('content', ''))]
        if len(banners) == 1:
            box = banners[0]

    subject = bg.get('subject') or spec.get('subject') or spec.get('visual')
    if box is None:
        # 一次有界返修只索取主图框及拍摄内容，不让模型重排整页。失败保留原输入并报错。
        plan = request_box('只确定指定局部摄影的图片框，不改其余版式。输出 JSON 对象，position 含 x,y,w,h（英寸），'
                           'subject 仅为无文字摄影内容。',
                           f'画布 {width}×{height} 英寸。局部要求：{local.group()}。本页构图：{text}。'
                           f'现有局部图片（若对应则采用原框）：{[(e["position"], e["content"]) for e in photos]}')
        p = plan.get('position') if isinstance(plan, dict) else None
        if not isinstance(p, dict) or not all(type(p.get(k)) in (int, float) for k in ('x','y','w','h')):
            raise ValueError(f'第{index+1}页局部摄影缺少有效位置，未删除主摄影')
        if not (0 <= p['x'] < p['x']+p['w'] <= width+.001 and 0 <= p['y'] < p['y']+p['h'] <= height+.001
                and (p['w'] < width*.97 or p['h'] < height*.97)):
            raise ValueError(f'第{index+1}页局部摄影位置越界或仍为整页，未删除主摄影')
        box = {k:p[k] for k in ('x','y','w','h')}
        subject = subject or plan.get('subject')

    if not any(matches(e['position']) for e in photos):
        # 原 bg ID 与拍摄说明转移到普通图片，素材申请不会再丢主摄影或重复申请一张背景。
        # 一块摄影说明可能跨多个分号（例如先写场景，再写人物）；不吞入下一分区。
        section = re.split(r'[①-⑳\n]|[。；;]\s*(?:[xy]\s*=|(?:下方|底部|下半部)?(?:卡片区|分隔区))',
                           text[local.start():], maxsplit=1)[0].strip('；。')
        el = _normalize_element(dict(type='picture', position=box, asset_id=bg['image'],
                                     content=subject or f'仅绘制此区域的无字摄影，不绘制文字及版式：{section}'),
                                len(page['elements']), index, vi)
        page['elements'].insert(0, el)
        page['elements'].sort(key=lambda e: e['z'])
    page['bg'] = dict(type='color', color=bg['color'], image=None)


def photo_spec(brief, index):
    """逐页要求优先；多页总述只继承明确指向本页的摄影条款。"""
    import re
    specs = brief.get('page_specs') or []
    if not specs:
        return brief if brief.get('pages', 1) == 1 else {}
    spec = dict(specs[index]) if index < len(specs) else {}
    if (full_bleed_rule(spec) is not None or spec.get('layout_recipe') or
            re.search(r'独立(?:照片|右栏|信息栏)|(?:照片|图文)分栏', str(spec.get('creative', '')))):
        return spec
    for clause in re.split(r'[，。；：]', str(brief.get('creative') or '')):
        scope = re.match(r'(第[第\d页至到–—\-、及和与\s]+页|每页|全册各页|所有页)', clause.strip())
        if not scope or full_bleed_rule({'creative': clause}) is not True:
            continue
        ranges = re.findall(r'(\d+)(?:\s*[至到–—\-]\s*(\d+))?', scope[1])
        if not ranges or any(int(a) <= index+1 <= int(b or a) for a,b in ranges):
            spec['full_bleed'] = True
            break
    return spec


def measure(path, *, cover_ratio=None):
    if not path:
        return {"status": "missing"}
    try:
        data = Path(path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        with Image.open(BytesIO(data)) as image:
            box = None
            if cover_ratio:
                from .slicing import _cover_view
                l, t, r, b = _cover_view(*image.size, cover_ratio, 1)
                box = (l*image.width, t*image.height, r*image.width, b*image.height)
            small = image.convert("RGB").resize((128, 72), Image.Resampling.BOX, box=box)
    except FileNotFoundError:
        return {"status": "missing"}
    except (OSError, ValueError) as exc:
        return {"status": "unreadable", "reason": type(exc).__name__}
    # 背景写入 PPT 时铺满全幅。直接在同一归一化画布测量，四侧保持一致。
    a = np.asarray(small, dtype=float)
    blur = np.asarray(small.filter(ImageFilter.GaussianBlur(3)), dtype=float)
    energy = np.abs(a - blur).mean(axis=2)
    energy += np.abs(np.diff(a, axis=1, prepend=a[:, :1])).mean(axis=2)
    energy += np.abs(np.diff(a, axis=0, prepend=a[:1])).mean(axis=2)
    report = {"status": "uncertain", "raw_sha256": digest, "method": "pixel_detail_bands", "mean_detail": round(float(energy.mean()), 3)}
    if energy.mean() < 0.8:
        return report
    bounds = []
    zones = {}
    for axis, near, far in ((0, "left", "right"), (1, "top", "bottom")):
        profile = energy.sum(axis=axis)
        cdf = np.cumsum(profile) / profile.sum()
        n = len(profile)
        lo, hi = (int(np.searchsorted(cdf, q)) for q in (.10, .90))
        bounds.append((lo / n, (hi + 1) / n))
        for side, span in ((near, lo / n), (far, 1 - (hi + 1) / n)):
            # 只有能容纳文字且细节密度明显低于全图的边带才可选。
            if span >= .26:
                zones[side] = round(span - .015, 3)
    report["detail_box"] = [round(v, 3) for v in (bounds[0][0], bounds[1][0], bounds[0][1], bounds[1][1])]
    if zones:
        report.update(status="measured", safe_bands=zones)
    return report


def _rect(el):
    p = el['position']
    return p['x'], p['y'], p['x'] + p['w'], p['y'] + p['h']


def _overlap(a, b, gap=0):
    return all(a[i] < b[i+2]+gap and b[i] < a[i+2]+gap for i in (0, 1))


def check_full_bleed_rows(page, evidence, width, height):
    """不重排已规划的清单；只有实测边带内的图文框才视为避开主体。"""
    bands = evidence.get('safe_bands', {}) if evidence['status'] == 'measured' else {}
    unresolved = []
    for el in page['elements']:
        if el['type'] not in ('text', 'picture'):
            continue
        x, y, r, b = _rect(el)
        spans = dict(left=r/width, right=1-x/width, top=b/height, bottom=1-y/height)
        if not (0 <= x < r <= width and 0 <= y < b <= height and
                any(spans[side] <= band for side, band in bands.items())):
            unresolved.append(el['element_id'])
    evidence.update(layout='full_bleed_checklist', unresolved=unresolved, moved=[])


def adapt_page(page, evidence, width, height):
    """每次从原始框重算，尊重手工返修；不镜像，不能安全移动则明确报告。"""
    previous = page.pop('_focus_geometry', {})
    tracked = ('position', 'wash', 'boundary', 'measured_position')
    def snapshot(el):
        return {k: (dict(el[k]) if k == 'position' else el[k]) for k in tracked if k in el}
    for el in page['elements']:
        record = previous.get(el['element_id'])
        if record:
            for k in tracked:
                if el.get(k) == record['after'].get(k):
                    if k in record['before']:
                        el[k] = record['before'][k]
                    else:
                        el.pop(k, None)
    elements = page['elements']
    if page.get('bg', {}).get('type') == 'image':
        from .checklist_layout import checklist_rows, plan_checklist
        rows = checklist_rows(elements, width, height)
        if rows and page['bg'].get('full_bleed') is True:
            # 整组图文沿用规划框；不能证明安全时交回审核，而非挪窄主摄影。
            check_full_bleed_rows(page, evidence, width, height)
            return
        plan = (plan_checklist(elements, rows, evidence, width, height)
                if rows and evidence['status'] in ('measured', 'uncertain') else None)
        if plan:
            side, fraction, boxes = plan
            records = {}
            for el in elements:
                before = snapshot(el)
                el['position'] = {k: round(v, 4) for k,v in boxes[el['element_id']].items()}
                if el['type'] == 'scrim':
                    el.update(wash=side, boundary=fraction)
                if snapshot(el) != before:
                    records[el['element_id']] = dict(before=before, after=snapshot(el))
            page['_focus_geometry'] = records
            evidence.update(layout='checklist_columns', rows=len(rows), wash=side, boundary=fraction,
                            moved=list(records), unresolved=[])
            return
    if evidence['status'] != 'measured':
        return
    blocks = [e for e in elements if e['type'] in {'text', 'branch', 'toc'}]
    if not blocks:
        return
    before = {e['element_id']: snapshot(e) for e in elements}
    # 图片说明、卡片和编号不能与自己的底板、图标各走各的。
    movable = [e for e in elements if e['type'] != 'scrim']
    scrims = [e for e in elements if e['type'] == 'scrim']
    bands = evidence['safe_bands']
    old_wash = scrims[0].get('wash') if scrims else None
    wash = max(bands, key=lambda side: bands[side] + (.05 if side == old_wash else 0))
    boundary = bands[wash]
    evidence.update(wash=wash, boundary=boundary)
    for el in scrims:
        el.update(wash=wash, boundary=boundary)
    for el in blocks:
        if el['type'] == 'toc':
            el.update(wash=wash, measured_position=True)
    axis = int(wash in ('top', 'bottom'))
    span = (width, height)[axis]
    lo, hi = (0, span * boundary) if wash in ('left', 'top') else (span * (1-boundary), span)
    fx, fy, fr, fb = evidence['detail_box']
    focus = (fx*width, fy*height, fr*width, fb*height)
    key = ('x', 'y')[axis]
    text_ids = {id(e) for e in blocks}
    moved, unresolved = [], []

    # 满幅照片上的单列说明页：安全边带并非从画布边缘到焦点都可以写字。
    # 先预留两端呼吸空间，再按原字号重算自然换行；整列能容纳才提交。
    stack = sorted(movable, key=lambda e: e['position']['y'])
    if (axis == 0 and len(blocks) >= 3 and len(scrims) == 1
            and scrims[0]['position']['w'] >= width-.05 and scrims[0]['position']['h'] >= height-.05
            and all(e['type'] == 'text' or (e['type'] == 'decorate' and e.get('primitive') == 'line') for e in stack)
            and max(e['position']['x'] for e in stack)-min(e['position']['x'] for e in stack) <= width*.05
            and all(_rect(a)[3] <= _rect(b)[1]+.01 for a,b in zip(stack,stack[1:]))):
        from .checklist_layout import _text_height
        outer, inner = width*.035, width*.04
        start = lo + (outer if wash == 'left' else inner)
        end = hi - (inner if wash == 'left' else outer)
        origin = min(e['position']['x'] for e in stack)
        cursor, proposed = height*.035, []
        for e in stack:
            p = e['position']
            x = start + p['x']-origin
            w = min(p['w'], end-x)
            if w < .25:
                break
            y = max(p['y'], cursor)
            h = max(p['h'], _text_height(e, w)) if e['type'] == 'text' else p['h']
            if y+h > height*.965:
                break
            proposed.append(dict(x=round(x,4), y=round(y,4), w=round(w,4), h=round(h,4)))
            cursor = y+h+.055
        if len(proposed) == len(stack):
            for e,p in zip(stack,proposed):
                if e['position'] != p:
                    moved.append(e['element_id'])
                e['position'] = p
            evidence.update(layout='full_bleed_text_column', reading_interval=[round(start,4),round(end,4)])
        else:
            unresolved.extend(e['element_id'] for e in blocks)
        page['_focus_geometry'] = {e['element_id']: {'before': before[e['element_id']], 'after': snapshot(e)}
                                   for e in elements if snapshot(e) != before[e['element_id']]}
        evidence.update(moved=moved, unresolved=unresolved)
        return

    def shift(group):
        boxes = [_rect(e) for e in group]
        a, b = min(r[axis] for r in boxes), max(r[axis+2] for r in boxes)
        scale = min(1.0, (hi-lo) / max(.001, b-a))
        # 优先平移；只允许一次温和横向收口。竖向挤压会切掉文字，宁可请求返修。
        if scale < (.80 if axis == 0 else .9999):
            return False
        anchor = min(max(a, lo), hi-(b-a)*scale)
        others = [_rect(e) for e in movable if all(e is not g for g in group)]
        updates = []
        for e, box in zip(group, boxes):
            after = list(box)
            after[axis] = anchor + (box[axis]-a)*scale
            after[axis+2] = anchor + (box[axis+2]-a)*scale
            if any(_overlap(after, obstacle) and not _overlap(box, obstacle) for obstacle in others):
                return False
            updates.append((e, round(after[axis], 3), round(after[axis+2]-after[axis], 3)))
        dim = ('w', 'h')[axis]
        for e, start, size in updates:
            if (start, size) != (e['position'][key], e['position'][dim]):
                e['position'].update({key: start, dim: size})
                moved.append(e['element_id'])
        return True

    # 首选刚性平移整个内容版心；已经绕开中心主体的版式保持原样。
    if any(_overlap(_rect(e), focus) for e in blocks) and not shift(movable):
        pending = list(movable)
        while pending:
            group = [pending.pop(0)]
            for member in group:
                linked = [e for e in pending if _overlap(_rect(member), _rect(e), gap=.18)]
                group.extend(linked)
                pending = [e for e in pending if all(e is not v for v in linked)]
            impacted = [e for e in group if id(e) in text_ids and _overlap(_rect(e), focus)]
            if impacted and not shift(group):
                unresolved.extend(e['element_id'] for e in impacted)
    page['_focus_geometry'] = {e['element_id']: {'before': before[e['element_id']], 'after': snapshot(e)}
                               for e in elements if snapshot(e) != before[e['element_id']]}
    evidence.update(moved=moved, unresolved=unresolved)
