"""摄影页的单列重复图文行：先试算全部文字，再一次性提交分栏。"""
from math import ceil
import re
from unicodedata import east_asian_width


def _p(el):
    return el['position']


def checklist_rows(elements, width, height):
    if any(e['type'] not in {'text', 'picture', 'scrim'} for e in elements):
        return None
    pictures = sorted((e for e in elements if e['type'] == 'picture'), key=lambda e: _p(e)['y'])
    if len(pictures) < 3:
        return None
    first = _p(pictures[0])
    rows = []
    first_signature = None
    texts = [e for e in elements if e['type'] == 'text']
    for pic in pictures:
        p = _p(pic)
        if (not 0 < p['w'] <= .17*width or not 0 < p['h'] <= .22*height
                or abs(p['x']-first['x']) > .025*width
                or not .75 <= p['h']/first['h'] <= 1.33
                or (rows and p['y'] < _p(rows[-1][0])['y'] + _p(rows[-1][0])['h'])):
            return None
        adjacent = [e for e in texts if p['y']-.08 <= _p(e)['y'] < p['y']+.98*p['h']]
        if not adjacent:
            return None
        after = all(_p(e)['x'] >= p['x']+p['w']-.05 for e in adjacent)
        before = all(_p(e)['x']+_p(e)['w'] <= p['x']+.05 for e in adjacent)
        signature = (after, tuple(map(len, _bands(adjacent))))
        if not (before or after) or (rows and signature != first_signature):
            return None
        first_signature = signature
        rows.append((pic, adjacent))
    paired = [id(e) for _, adjacent in rows for e in adjacent]
    return rows if len(set(paired)) == len(paired) else None


def _bands(texts):
    bands = []
    for el in sorted(texts, key=lambda e: _p(e)['y']):
        if not bands or _p(el)['y']-_p(bands[-1][0])['y'] > .09:
            bands.append([])
        bands[-1].append(el)
    return [sorted(b, key=lambda e: _p(e)['x']) for b in bands]


def _text_height(el, width):
    # 以 em 为单位，给字体替换、禁则标点和按整词换行留余量；不改文案/字号。
    size = el['font']['size']
    capacity = width * 72 / size * .92
    lines = 0
    for paragraph in el['content'].split('\n'):
        used = 0
        lines += 1
        for token in re.findall(r'[a-zA-Z0-9]+|.', paragraph):
            advance = sum(1 if east_asian_width(c) in 'WF' else .64 for c in token)
            if used and used + advance > capacity:
                lines += 1
                used = 0
            lines += max(0, ceil(advance/capacity)-1)
            used = advance % capacity if advance > capacity else used+advance
    return lines * size * 1.34 / 72 + .025


def plan_checklist(elements, rows, evidence, width, height):
    members = {id(e) for pic, texts in rows for e in [pic, *texts]}
    extra = [e for e in elements if e['type'] == 'text' and id(e) not in members]
    first_y = _p(rows[0][0])['y']
    header = [e for e in extra if _p(e)['y']+_p(e)['h'] <= first_y+.05]
    footer = [e for e in extra if _p(e)['y'] >= _p(rows[-1][0])['y']+_p(rows[-1][0])['h']-.05]
    if len(header)+len(footer) != len(extra):
        return None
    all_content = [e for e in elements if e['type'] != 'scrim']
    left = min(_p(e)['x'] for e in all_content)
    right = max(_p(e)['x']+_p(e)['w'] for e in all_content)
    bands = {k:v for k,v in evidence.get('safe_bands', {}).items() if k in ('left', 'right')}
    side = max(bands, key=bands.get) if bands else ('left' if left+right < width else 'right')
    if bands and (right if side == 'left' else width-left) <= bands[side]*width:
        return None

    pad = .025*width
    gap = .016*height
    for fraction in (.43, .47, .51, .55, .58):
        column = fraction*width
        x = pad if side == 'left' else width-column+pad
        inner = column-2*pad
        boxes = {}

        def flow(texts, x0, y, span):
            for band in _bands(texts):
                # 同一基线上的编号、标题或页脚保留顺序；短标签只占原有窄栏。
                widths = [_p(e)['w'] for e in band]
                wide = max(range(len(band)), key=widths.__getitem__)
                widths[wide] = span-sum(w for i,w in enumerate(widths) if i != wide)-gap*(len(band)-1)
                if min(widths) < .18:
                    return float('inf')
                cursor = x0
                heights = []
                for e,w in zip(band,widths):
                    h = _text_height(e,w)
                    boxes[e['element_id']] = dict(x=cursor,y=y,w=w,h=h)
                    cursor += w+gap
                    heights.append(h)
                y += max(heights)+gap/3
            return y

        y = flow(header,x,.04*height,inner) + gap
        foot_height = flow(footer,x,0,inner)
        bottom = height-.04*height-foot_height
        blocks = []
        for pic,texts in rows:
            p = _p(pic)
            tw = min(p['w'], .16*inner)
            th = tw*p['h']/p['w']
            thumb_first = p['x'] < min(_p(e)['x'] for e in texts)
            boxes[pic['element_id']] = dict(x=x if thumb_first else x+inner-tw,y=y,w=tw,h=th)
            end = flow(texts,x+tw+gap if thumb_first else x,y,inner-tw-gap)
            blocks.append([pic, *texts])
            y = max(end,y+th)+gap
        if y > bottom:
            continue
        slack = min((bottom-y)/max(1,len(rows)-1), .035*height)
        for i,block in enumerate(blocks):
            for e in block:
                boxes[e['element_id']]['y'] += i*slack
        flow(footer,x,bottom,inner)
        for e in elements:
            if e['type'] == 'scrim':
                boxes[e['element_id']] = dict(x=0 if side=='left' else width-column,y=0,w=column,h=height)
        return side, fraction, boxes
    return None
