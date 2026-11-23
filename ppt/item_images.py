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


def item_targets(page):
    elements = page.get('elements', [])
    pictures = [e for e in elements if e['type'] == 'picture' and e.get('asset_id') != 'cover_title_art']
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
                raise ValueError(f'插图行键 {key} 必须唯一对应一幅图与一个行标题')
            keys.add(key)
            label = labels[0]
        elif legacy_list:
            if not adjacent:
                raise ValueError(f"无法定位插图 {el['asset_id']} 的对应行，请显式设置 item_key")
            label = adjacent[0]
            if label.isdecimal() and len(adjacent) > 1:
                label += ' ' + adjacent[1]
            label = re.split('[｜|\n]', label, maxsplit=1)[0].strip()
            if not label or label in legacy_labels:
                raise ValueError(f'无法唯一定位插图行 {label!r}，请显式设置 item_key')
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
        raise ValueError('有行标题没有对应插图：' + ', '.join(sorted(label_keys - keys)))
    return targets
