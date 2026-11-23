"""返修提案按原页证据授权；归一化 ID 只用于指认原件，不用于跨版本匹配。"""
from collections import Counter, defaultdict
from copy import deepcopy
import json
import math
import re
import unicodedata

import config
from ppt.checklist_layout import _text_height


_PICTURE = r'(?:装饰图(?:片|案)?|装饰|配图|图片|插图|图案)'
_XY = re.compile(r'\bx\s*=\s*(-?\d+(?:\.\d+)?)\s*,\s*y\s*=\s*(-?\d+(?:\.\d+)?)(?![\w.%])', re.I | re.ASCII)


def decoration_overlap(page, entry):
    """None 表示普通返修；空元组表示装饰叠字但无法核对，不能退回通用授权。"""
    if entry['rule'] != 'no_overlap':
        return None
    detail = unicodedata.normalize('NFKC', entry['detail'])
    elements = page['elements']
    pictures = [e for e in elements if e['type'] == 'picture' and e.get('layer') in ('overlay', 'decoration')]
    ids = entry.get('target_ids') or []
    quotes = re.findall(r'[「“‘"`]([^」”’"`]+)[」”’"`]', detail)
    if not ((re.search(_PICTURE, detail) or any(e['element_id'] in ids for e in pictures)) and
            (re.search(r'标题|文字|字形|文本', detail) or quotes)):
        return None

    def words(value):
        return set(re.findall(r'[a-z][a-z0-9]{2,}', value.casefold()))

    # 旧报告中的主题词必须紧邻图片称谓，同时能在素材 ID 和实际描述中核对。
    labels = set(re.findall(r'(?<![a-z0-9_])([a-z][a-z0-9]{2,})\s*' + _PICTURE, detail.casefold()))
    anchor = _XY.findall(detail)
    has_xy = re.search(r'(?<![a-z0-9_])[xy]\s*[=≈:]', detail, re.I)
    if has_xy and (len(anchor) != 1 or len(re.findall(r'[xy]\s*[=≈:]', detail, re.I)) != 2):
        return ()
    if (not ids and not (labels and anchor)) or len(set(ids)) > 1 or len(labels) > 1:
        return ()
    if ids and sum(bool({e['element_id'], e.get('asset_id')}.intersection(ids)) for e in elements) != 1:
        return ()
    matches = [e for e in pictures if (not ids or {e['element_id'], e.get('asset_id')}.intersection(ids))
               and labels <= (words(e.get('asset_id', '')) & words(e.get('content', '')))
               and (not anchor or all(abs(e['position'][k]-float(v)) <= .005001 for k,v in zip(('x','y'),anchor[0])))]
    if len(matches) != 1 or len({e['element_id'] for e in elements}) != len(elements):
        return ()
    pic = matches[0]
    texts = [e for e in elements if e['type'] == 'text' and e.get('content','').strip() and _overlap(e['position'], pic['position']) > 0]
    if quotes:
        texts = [e for e in texts if all(q in unicodedata.normalize('NFKC', e['content']) for q in quotes)]
    return (pic, texts[0]) if len(texts) == 1 else ()


def named_targets(page, entry):
    pair = decoration_overlap(page, entry)
    if pair is not None:
        return {pair[0]['element_id']} if pair else set()
    evidence = entry['rule'] + '：' + entry['detail']
    quoted = set(re.findall(r'[「“‘\"`]([^」”’\"`]+)[」”’\"`]', evidence))
    elements = page['elements']
    if entry.get('target_ids'):
        ids = set(entry['target_ids'])
        return {e['element_id'] for e in elements if e['element_id'] in ids or e.get('asset_id') in ids}
    names = Counter(e.get('content') for e in elements if e['type'] == 'text')
    # 素材别名必须有原始描述和素材标识双重佐证。
    aliases = defaultdict(set)
    for e in elements:
        if e['type'] in ('picture', 'mask_picture'):
            words = set(re.findall(r'[a-z]{3,}', e.get('content', '').lower()))
            for word in words.intersection(re.findall(r'[a-z]{3,}', e.get('asset_id', '').lower())):
                aliases[word].add(e['element_id'])
    result = set()
    for word in re.findall(r'(?<![a-z0-9_])[a-z]{3,}(?![a-z0-9_])', entry['detail'].lower()):
        if len(aliases[word]) == 1:
            result.update(aliases[word])

    def mentions(name):
        return name and re.search(r'(?<![A-Za-z0-9_])' + re.escape(name) + r'(?![A-Za-z0-9_])', evidence)
    # 先消费完整文案，避免 Table Contents 连带授权 Table 和 Contents。
    cited = set(quoted)
    remainder = re.sub(r'[「“‘\"`][^」”’\"`]+[」”’\"`]', '', evidence)
    for name in sorted(filter(None, names), key=len, reverse=True):
        if len(name) < 2:
            continue
        remainder, hits = re.subn(r'(?<![A-Za-z0-9_])' + re.escape(name) + r'(?![A-Za-z0-9_])', ' ', remainder)
        if hits:
            cited.add(name)
    for e in elements:
        name = e.get('content')
        if mentions(e['element_id']) or mentions(e.get('asset_id')) or (
                e['type'] == 'text' and names[name] == 1 and name in cited):
            result.add(e['element_id'])
    if entry['rule'] == 'scrim_focus_protected':
        scrims = [e['element_id'] for e in elements if e['type'] == 'scrim']
        if len(scrims) == 1:
            result.update(scrims)
    return result


def _identity(e):
    kind = e['type']
    content = e.get('content', '')
    if kind == 'text' and not content.strip():
        return None
    if kind in ('picture', 'mask_picture') and not e.get('asset_id'):
        return None
    if kind in ('shape', 'decorate') and not (e.get('note') or e.get('role')):
        return None
    keys = ('type', 'content', 'asset_id', 'item_key', 'layer', 'role', 'shape', 'primitive',
            'region_id', 'branch_type', 'items', 'title')
    if kind in ('shape', 'decorate'):
        keys += ('note',)
    if kind == 'mask_picture':
        keys += ('mask',)
    return json.dumps([e.get(k) for k in keys], sort_keys=True, ensure_ascii=False)


def _bounds(p):
    return p['x'], p['y'], p['x'] + p['w'], p['y'] + p['h']


def _inside(a, b):
    l, t, r, d = _bounds(a); x, y, u, v = _bounds(b)
    return l >= x-.006 and t >= y-.006 and r <= u+.006 and d <= v+.006


def _overlap(a, b):
    l, t, r, d = _bounds(a); x, y, u, v = _bounds(b)
    return max(0., min(r,u)-max(l,x)) * max(0., min(d,v)-max(t,y))


def _safe(el, patch, original, current):
    a, b = el['position'], patch.get('position', el['position'])
    if (not isinstance(b, dict) or not all(isinstance(b.get(k), (int,float)) and math.isfinite(b[k]) for k in ('x','y','w','h'))
            or min(b['w'], b['h']) <= 0 or not _inside(b, dict(x=0, y=0, w=config.SLIDE_W_IN, h=config.SLIDE_H_IN))):
        return False
    if el['type'] == 'text' and el.get('font', {}).get('size'):
        after = dict(el, **patch)
        if _text_height(after, b['w']) - b['h'] > max(0., _text_height(el, a['w']) - a['h']) + .02:
            return False
    if a == b:
        return True
    has_local_host = False
    for eid, other in original.items():
        if eid == el['element_id']:
            continue
        p, q = other['position'], current[eid]['position']
        host_types = ('shape', 'picture', 'mask_picture', 'scrim')
        if other['type'] in host_types and other.get('z', 0) <= el.get('z', 0) and _inside(a,p):
            if not _inside(b,q):
                return False
            if p['w'] * p['h'] < .65 * config.SLIDE_W_IN * config.SLIDE_H_IN:
                has_local_host = True
            continue
        if el['type'] in host_types and el.get('z', 0) <= other.get('z', 0) and _inside(p,a):
            if not _inside(q,b):
                return False
            continue
        before, after = _overlap(a,p), _overlap(b,q)
        if after > before + .0001:
            return False
        # 已有重叠不可因缩小文字框而吞没其中更大比例。
        if after / min(b['w']*b['h'], q['w']*q['h']) > before / min(a['w']*a['h'], p['w']*p['h']) + .002:
            return False
        if el['type'] == other['type'] == 'text':
            # 同列已分隔的文字行不能互换角色，即使最终两框没有相交。
            if min(a['x']+a['w'],p['x']+p['w']) > max(a['x'],p['x']):
                if ((a['y']+a['h'] <= p['y'] and b['y'] >= q['y']+q['h']) or
                    (p['y']+p['h'] <= a['y'] and q['y'] >= b['y']+b['h'])):
                    return False
            if min(a['y']+a['h'],p['y']+p['h']) > max(a['y'],p['y']):
                if ((a['x']+a['w'] <= p['x'] and b['x'] >= q['x']+q['w']) or
                    (p['x']+p['w'] <= a['x'] and q['x'] >= b['x']+b['w'])):
                    return False
    # 没有卡片、页眉条、照片等区域依据时，只接受能与原槽位相交的局部修正。
    return bool(has_local_host or _overlap(a,b))


def fit_decoration(page, pic):
    """在旧图框内等比取空位，故不会覆盖原先图框之外的摄影主体。"""
    a = pic['position']
    left, top, right, bottom = _bounds(a)
    gap = .02 * min(a['w'], a['h'])
    # 全部原生文字均受保护，不只避让被引用的那一行。
    obstacles = []
    for e in page['elements']:
        if e['type'] == 'text':
            l,t,r,b = _bounds(e['position'])
            box = dict(x=l-gap, y=t-gap, w=r-l+2*gap, h=b-t+2*gap)
            if _overlap(a, box):
                obstacles.append(box)
    cx, cy = (left+right)/2, (top+bottom)/2
    # 优先保留尺寸，然后尽量接近原中心；过小的装饰不能视为修好。
    for percent in range(100, 29, -5):
        w, h = a['w']*percent/100, a['h']*percent/100
        xs, ys = {left, right-w, cx-w/2}, {top, bottom-h, cy-h/2}
        for box in obstacles:
            l,t,r,b = _bounds(box)
            xs.update((l-w,r)); ys.update((t-h,b))
        positions = sorted(((x,y) for x in xs if left-1e-9 <= x <= right-w+1e-9
                            for y in ys if top-1e-9 <= y <= bottom-h+1e-9),
                           key=lambda xy: ((xy[0]+w/2-cx)**2+(xy[1]+h/2-cy)**2, xy))
        for x,y in positions:
            box = dict(x=x,y=y,w=w,h=h)
            if any(_overlap(box, obstacle) > 1e-10 for obstacle in obstacles):
                continue
            proposed = dict(page_name=page.get('page_name'), elements=[dict(pic, position=box)])
            if merge_page(page, proposed, set(), {pic['element_id']}):
                return True
    return False


def merge_page(page, proposed, locked, targets):
    changed = set()
    if not targets or proposed.get('page_name') != page.get('page_name'):
        return changed
    originals = {e['element_id']: deepcopy(e) for e in page['elements']}
    current = {e['element_id']: e for e in page['elements']}
    counts = Counter(_identity(e) for e in originals.values())
    candidates = defaultdict(list)
    for e in proposed.get('elements', []):
        candidates[_identity(e)].append(e)
    for eid, el in originals.items():
        key = _identity(el)
        if eid not in targets or eid in locked or key is None or counts[key] != 1 or len(candidates[key]) != 1:
            continue
        candidate = candidates[key][0]
        fields = {'position'}
        if el['type'] == 'text':
            fields.update(('font', 'align'))
        elif el['type'] == 'scrim':
            fields = {'strength', 'wash', 'boundary', 'color'}
        elif el['type'] in ('toc', 'branch'):
            fields.update(('style', 'cols'))
        patch = {k: candidate[k] for k in fields if k in candidate and candidate[k] != el.get(k)}
        if patch and _safe(el, patch, originals, current):
            current[eid].update(deepcopy(patch))
            changed.add(eid)
    return changed
