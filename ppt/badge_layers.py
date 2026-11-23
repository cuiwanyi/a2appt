"""在实际绘制前，保护同心实色圆章内的原生文本。"""
from .inspector import _contrast


def _center(p, axis, size):
    return p[axis] + p[size] / 2


def _overlap(a, b):
    return all(a[k] < b[k] + b[s] and b[k] < a[k] + a[s] for k, s in (("x", "w"), ("y", "h")))


def _inside(p, disk):
    # 用离圆心最远的角验证整个文本框，而不只是框中心。
    return sum(((abs(_center(p, k, s) - _center(disk, k, s)) + p[s] / 2) / (disk[s] / 2)) ** 2
               for k, s in (("x", "w"), ("y", "h"))) <= 1


def protect_badge_layers(page):
    elements = page.get('elements', [])
    disks = [e for e in elements if e.get('type') == 'shape' and e.get('shape') == 'oval'
             and e.get('fill') and e.get('fill_opacity', 1) > 0
             and .95 <= e['position']['w'] / e['position']['h'] <= 1.05]
    done = set()
    for outer in sorted(disks, key=lambda e: -e['position']['w']):
        if id(outer) in done:
            continue
        box = outer['position']
        group = [e for e in disks if id(e) not in done and all(
            .65 <= e['position'][s] / box[s] <= 1.001 and
            abs(_center(e['position'], k, s) - _center(box, k, s)) <= .01 * box[s]
            for k, s in (('x', 'w'), ('y', 'h')))]
        if len(group) < 2 or min(e['position']['w'] for e in group) > .98 * box['w']:
            continue
        members = {id(e) for e in group}
        done.update(members)
        slots = [i for i, e in enumerate(elements) if id(e) in members]
        labels = [e for e in elements if e.get('type') == 'text' and e.get('content', '').strip()
                  and all(_inside(e['position'], d['position']) for d in group)]
        if not labels:
            continue
        face = elements[slots[-1]]
        if face.get('fill_opacity', 1) != 1:
            continue  # 半透明章面的实际底色未知，不能据此推断对比度。
        ordered = sorted(group, key=lambda e: -e['position']['w'])
        inner = ordered[-1]
        # 只在最内圆唯一、换序能救回字色、不会跨过相交物件时调整形状。
        if (face is not inner and inner.get('fill_opacity', 1) == 1
                and inner['position']['w'] < .98 * ordered[-2]['position']['w']
                and any(_contrast(e['font']['color'], face['fill']) < 3 for e in labels)
                and all(_contrast(e['font']['color'], inner['fill']) >= 4.5 for e in labels)
                and not any(id(e) not in members and _overlap(box, e['position'])
                            for e in elements[slots[0]:slots[-1]])
                and not any(e not in labels and _overlap(box, e['position'])
                            for e in elements[slots[-1]+1:])):
            levels = [(elements[i]['z'], elements[i]['layer']) for i in slots]
            for i, e, (z, layer) in zip(slots, ordered, levels):
                elements[i] = e
                e.update(z=z, layer=layer)
            face = inner
        for label in labels:
            i, j = elements.index(label), elements.index(face)
            # 不把文字拉过相交的照片、遮罩、其他文本等；此时无法推断实际底色。
            if any(e is not label and id(e) not in members and _overlap(label['position'], e['position'])
                   for e in elements[min(i, j) + 1:]):
                continue
            if i < j:
                elements.pop(i)
                elements.insert(j, label)
                label.update(z=face['z'], layer=face['layer'])
            if _contrast(label['font']['color'], face['fill']) < 3:
                ink = max((d['fill'] for d in group), key=lambda c: _contrast(c, face['fill']))
                if _contrast(ink, face['fill']) >= 4.5:
                    label['font']['color'] = ink
