"""逐项插图的行绑定；不依赖主题词、页码、素材命名或元素数组顺序。"""
from __future__ import annotations

import re

import config


def background_framing(page, targets):
    """只用叠印框的几何位置规划主摄影，不把行标签、插图描述带入背景。"""
    boxes = []
    zones = set()
    reserved = set()
    for el in page['elements']:
        text = el.get('content', '').strip() if el['type'] == 'text' else ''
        if not text and not (el['type'] == 'picture' and el['asset_id'] in targets):
            continue
        p = el['position']
        x, y = p['x'] / config.SLIDE_W_IN, p['y'] / config.SLIDE_H_IN
        right = min(1., x + p['w'] / config.SLIDE_W_IN)
        bottom = min(1., y + p['h'] / config.SLIDE_H_IN)
        boxes.append((x, y, right, bottom))
        cx, cy = (x + right) / 2, (y + bottom) / 2
        col = '左侧' if cx < 1/3 else '右侧' if cx > 2/3 else '中央'
        row = '上部' if cy < 1/3 else '底部' if cy > .8 else '中段'
        number = text and re.fullmatch(r'\d+\s*(?:[/／]\s*\d+)?', text)
        zone = row + col + ('页码区' if number and cy > .8 else '')
        zones.add(zone)
        # 只下发几何框，不把文案内容带进生图提示，避免模型把文字画进照片。
        if text:
            reserved.add(f'{zone} x={x:.0%}–{right:.0%}, y={y:.0%}–{bottom:.0%}')

    # 枚举叠印框边缘之间的空矩形，优先能容下完整主体的区域，避免选中狭长缝隙。
    edges = sorted({0., 1., *(v for x, _, r, _ in boxes for v in (x, r))})
    best, focus = (0., 0.), ''
    for i, left in enumerate(edges):
        for right in edges[i+1:]:
            spans = sorted((y, b) for x, y, r, b in boxes if x < right and r > left)
            top = 0.
            for y, b in [*spans, (1., 1.)]:
                w, h = right - left, y - top
                score = (min(w, h), w * h)
                if score > best:
                    best = score
                    cx, cy = (left + right)/2, (top + y)/2
                    col = '左侧' if cx < .4 else '右侧' if cx > .6 else '中央'
                    row = '偏上' if cy < 1/3 else '偏下' if cy > 2/3 else ''
                    focus = (f'主体与动作保持完整，置于画面{col}{row}的净区内'
                             f'（距左 {left:.0%}–{right:.0%}，距顶 {top:.0%}–{y:.0%}），边缘留余量。')
                top = max(top, b)
    zones_text = '以下叠印区保留低细节自然留白：' + '、'.join(sorted(zones)) + '。'
    if reserved:
        zones_text += ('高纹理主体、器物边缘与手部不得进入这些实际文字框：'
                       + '；'.join(sorted(reserved)) + '。返修只改主摄影构图，不重画独立小图或文字。')
    return focus + zones_text


# 仅去掉系列裁切/笔触说明，不能吞掉同句中真正的主体描述。
_SERIES = re.compile(r'[一二三四五六七八九十百\d]+幅均(?=[等比居中裁切])|与首幅相同(?:比例)?[、，]?')


def _nearby_text(picture, texts):
    """旧版无行键：只取同一高度带、紧邻图片的文字，不跨栏。"""
    box = picture.get('position', {})
    if not box:
        return []
    found = []
    for text in texts:
        p = text['position']
        if not (box['y'] - .04 <= p['y'] + min(.1, p['h']/2) <= box['y'] + box['h']):
            continue
        gap = max(p['x'] - box['x'] - box['w'], box['x'] - p['x'] - p['w'], 0)
        if gap <= max(1.0, box['w']):
            found.append((gap, p['y'], text['content'].strip()))
    return [s for _, _, s in sorted(found)]


class ItemBindingError(ValueError):
    """只有缺失或冲突的行键才进入绑定返修。"""


def _item_picture(el, standalone, *, repair=False):
    if el['type'] == 'mask_picture':
        if not el['mask']['continuous'] or not (repair or el.get('item_key')):
            return False
    elif el['type'] != 'picture':
        return False
    aid = el['asset_id']
    if aid == 'cover_title_art' or aid in standalone or el.get('asset_kind') in ('emblem', 'icon', 'decoration'):
        return False
    if el.get('layer') == 'decoration' and not el.get('item_key'):
        return False
    # 只看首句的主体，不因照片要求「无图标」或「门上有徽标」而排除照片。
    subject = re.split(r'[，,。；;\n]', el.get('content', ''), maxsplit=1)[0].strip()
    if (re.search(r'(?:图标|徽标|徽章|装饰纹样|\b(?:icon|emblem|logo))$', subject, re.I)
            and not re.search(r'不要|禁止|不含|无|没有|含有|带有|印有|拍摄|照片|门头', subject)):
        return False
    p = el.get('position') or {}
    small = 0 < p.get('w', 0) <= .6 and 0 < p.get('h', 0) <= .6
    return not small or bool(el.get('item_key'))


def _contains(outer, inner):
    return all(outer[a] - .01 <= inner[a] and inner[a] + inner[s] <= outer[a] + outer[s] + .01
               for a, s in (('x', 'w'), ('y', 'h')))


def _card_images(title, titles, pictures, elements):
    """返回有卡片证据的候选；None 表示不是可识别的卡片结构。"""
    box = title['position']
    hosts = [e['position'] for e in elements if e['type'] == 'shape' and e.get('shape') in
             ('rect', 'round_rect', 'parallelogram', 'trapezoid') and _contains(e['position'], box)
             and sum(_contains(e['position'], t['position']) for t in titles) == 1]
    if hosts:
        # 嵌套或重叠卡底给出不同候选时不以元素顺序决定，也不借用邻卡的图。
        return [p for p in pictures if any(_contains(h, p['position']) for h in hosts)]

    # 无原生卡底的横排卡：至少两标题同一高度且横向分离。只接受紧邻且独占一栏的图。
    peers = sorted((t['position'] for t in titles), key=lambda p: p['x'])
    if (len(peers) < 2 or any(abs(b['y'] - box['y']) > .5 * box['h'] for b in peers)
            or any(b['x'] < a['x'] + a['w'] - .01 for a, b in zip(peers, peers[1:]))):
        return None
    found = []
    for pic in pictures:
        p = pic['position']
        overlap = lambda b: min(b['x'] + b['w'], p['x'] + p['w']) - max(b['x'], p['x'])
        gap = max(box['y'] - p['y'] - p['h'], p['y'] - box['y'] - box['h'], 0)
        if (gap <= min(1., p['h']) and overlap(box) >= .6 * min(box['w'], p['w'])
                and sum(overlap(b) > .01 for b in peers) == 1):
            found.append(pic)
    return found


def repair_item_bindings(layout, client, reason):
    """仅修复出错页的绑定元数据。已命名的条目不得被删除或换名。"""
    import json
    from copy import deepcopy
    from .schema import page_item_targets, storefront_photos, emblem_pictures
    fixed = deepcopy(layout)
    for page in fixed['pages']:
        shops = storefront_photos(page)
        try:
            page_item_targets(page, shops)
            continue
        except ItemBindingError as exc:
            reason = str(exc)
        elements = page['elements']
        standalone = shops.keys() | emblem_pictures(page).keys()
        pics = [e for e in elements if _item_picture(e, standalone, repair=True)
                and _SERIES.sub('', e.get('content', '')).strip()]
        texts = [e for e in elements if e['type'] == 'text' and e.get('content', '').strip()]
        required = {e['item_key'] for e in elements if e.get('item_key')}
        titles = [e for e in texts if e.get('item_key')]
        claimed = set()
        resolved = set()
        for key in sorted(required):
            ts = [e for e in titles if e['item_key'] == key]
            ps = [e for e in pics if e.get('item_key') == key]
            if len(ts) > 1 or len(ps) > 1:
                raise ItemBindingError(f'插图行键 {key} 的已有标题或主图不唯一')
            if not ts:
                continue
            if not ps:
                choices = _card_images(ts[0], titles, pics, elements)
                if choices is None:
                    continue
                if len(choices) != 1 or choices[0].get('item_key') not in (None, '', key):
                    raise ItemBindingError(f'插图行键 {key} 的同卡主配图缺失或不唯一')
                ps = choices
            ts[0]['item_key'] = ps[0]['item_key'] = key
            resolved.add(key)
            claimed.update((ts[0]['element_id'], ps[0]['element_id']))

        remaining = required - resolved
        if remaining or not required:
            eligible = {e['element_id']: e for e in texts + pics if e['element_id'] not in claimed}
            rows = [{k: e.get(k) for k in ('element_id', 'type', 'asset_id', 'item_key', 'content', 'position')}
                    for e in eligible.values()]
            answer = client.chat_json('只修复未解决条目，每个条目只选一个主标题与一幅主配图，保留已有行键。不选编号、翻译、装饰或图标。无法确定时返回空表。只返回 {"pairs":[{"key":"行键","title":"文本元素ID","image":"图片元素ID"}]}。',
                                      json.dumps({'error': reason, 'keys': sorted(remaining), 'elements': rows}, ensure_ascii=False))
            pairs = answer.get('pairs') if isinstance(answer, dict) else None
            if not isinstance(pairs, list) or not pairs:
                raise ItemBindingError('未能修复逐项配图绑定：缺少对应表')
            for pair in pairs:
                if not isinstance(pair, dict) or not all(isinstance(pair.get(k), str) and pair[k].strip()
                                                          for k in ('key', 'title', 'image')):
                    raise ItemBindingError('逐项配图绑定返修返回无效对应表')
                key, title, image = (pair[k] for k in ('key', 'title', 'image'))
                if (key in resolved or title in claimed or image in claimed or
                        title not in eligible or eligible[title]['type'] != 'text' or
                        image not in eligible or eligible[image]['type'] == 'text' or
                        any(eligible[i].get('item_key') not in (None, '', key) for i in (title, image)) or
                        any(e.get('item_key') == key and e['element_id'] not in (title, image)
                            for e in eligible.values())):
                    raise ItemBindingError('逐项配图绑定返修没有返回一一对应的现有元素')
                eligible[title]['item_key'] = eligible[image]['item_key'] = key
                claimed.update((title, image))
                resolved.add(key)
            if required and resolved != required:
                raise ItemBindingError('逐项配图绑定返修改变了原条目集合')
        # 仅移除明确被排除的装饰物上的误键，不清空其他元素的原有绑定。
        for e in elements:
            if e.get('item_key') in resolved and e['element_id'] not in claimed:
                e.pop('item_key')
        page_item_targets(page, storefront_photos(page))
    return fixed


def item_targets(page, standalone=()):
    elements = page.get('elements', [])
    pictures = [e for e in elements if _item_picture(e, standalone)]
    texts = [e for e in elements if e['type'] == 'text' and e.get('content')]
    targets = {}
    keys = set()
    label_keys = {e['item_key'] for e in texts if e.get('item_key')}
    # 无行键的独立多图页也可能只是图库，不用条目规则取代普通图片的语义。
    nearby = [_nearby_text(e, texts) if not label_keys else [] for e in pictures]
    legacy_list = not label_keys and len(pictures) > 1 and sum(bool(n) for n in nearby) >= 2
    legacy_labels = set()
    for el, adjacent in zip(pictures, nearby):
        key = el.get('item_key')
        if key:
            labels = [e['content'] for e in texts if e.get('item_key') == key]
            if len(labels) != 1 or key in keys:
                raise ItemBindingError(f'插图行键 {key} 必须唯一对应一幅图与一个行标题')
            keys.add(key)
            label = labels[0]
        elif legacy_list:
            if not adjacent:
                raise ItemBindingError(f"无法定位插图 {el['asset_id']} 的对应行，请显式设置 item_key")
            label = adjacent[0]
            if label.isdecimal() and len(adjacent) > 1:
                label += ' ' + adjacent[1]
            label = re.split('[｜|\n]', label, maxsplit=1)[0].strip()
            if not label or label in legacy_labels:
                raise ItemBindingError(f'无法唯一定位插图行 {label!r}，请显式设置 item_key')
            legacy_labels.add(label)
        else:
            continue
        subject = _SERIES.sub('', el.get('content', '')).strip()
        if not subject:
            raise ValueError(f"插图 {el['asset_id']} 缺少本条画面描述")
        aid = el['asset_id']
        if aid in targets:
            raise ValueError(f'逐项插图不能共用素材 ID：{aid}')
        targets[aid] = {'row': ' '.join(label.split()), 'subject': ' '.join(subject.split())}
    if keys != label_keys:
        raise ItemBindingError('有行标题没有对应插图：' + ', '.join(sorted(label_keys - keys)))
    return targets
