"""从用户输入读取封面主标题授权；版式、文案和模型补全都不是授权来源。"""
from __future__ import annotations

import json
import re

COVER = re.compile(r"封面|扉页|首页|第\s*[一1]\s*[页张]|\bcover\b|01_cover", re.I)
_INNER = re.compile(r"内页|正文|目录|章节|封底|尾页|末页|副标题|落款|(?:其[他余]|后续)(?:页|文字|文本)|"
                    r"第\s*(?![一1]\s*[页张])[\d一二三四五六七八九十两]+\s*[页张]|"
                    r"\b(?:p|page)\s*(?!0*1\b)\d+\b|\b(?:inner|subtitle)\b", re.I)
_STYLE = re.compile(r"金属(?:质感|效果|风格|立体)?(?:的)?(?:艺术字|字(?:体)?|标题)|metallic\s+lettering|"
                    r"毛笔(?:书法)?字(?:体)?|书法(?:艺术)?字(?:体)?|brush\s+lettering|艺术字|特效字|\bwordart\b", re.I)
_DENY = re.compile(r"不|无|勿|别|禁|避免|拒绝|去掉|取消|不要|无需|原生|可编辑|普通(?:字|标题)|\b(?:no|not|without|avoid|editable|native)\b", re.I)
_ACTION = re.compile(r"(?:用|要|做成?|设为|换成|改成|添加|制作|设计成|呈现|\b(?:use|apply|add)\b)\s*(?:一[个种款行]|醒目的?|精致的?)?\s*$", re.I)
_QUOTES = re.compile(r'「[^」]*」|“[^”]*”|《[^》]*》|"[^"\n]*"')


def _scan(text: str, *, scoped=False) -> tuple[str | None, bool]:
    style, inner = None, False
    for part in re.split(r"[，,。；;！!？?\n]+", _QUOTES.sub('', text)):
        # 同一句交叉指定多个对象时宁可不触发。逗号后的补充沿用前一页范围。
        if _INNER.search(part):
            inner = True
        elif COVER.search(part):
            inner = False
        if inner:
            continue
        effect = _STYLE.search(part)
        if _DENY.search(part) and (effect or re.search(r'原生|可编辑|普通字|native|editable', part, re.I)
                                   or (style and re.search(r'(?:不要|不用|取消|不需要|除外)\s*$', part))):
            return None, True
        if effect:
            before = part[:effect.start()]
            # “封面展示毛笔字作品”是图片主题，不是标题样式。
            shorthand = re.search(r'(?:封面|扉页|首页|主标题|标题)\s*[:：]?\s*$', before)
            if not (_ACTION.search(before) or shorthand or part.strip() == effect[0]
                    or (scoped and re.fullmatch(r'[\s:：]*', before))):
                continue
            # 关于艺术字的课程/标题文案等陈述不是设计要求。
            if re.search(r'主题|文案|题为|标题(?:为|是|叫)|介绍|讲解|课程|教程|培训|发展史|是否|要不要|如果', part):
                continue
            style = ('metallic' if re.search(r'金属|metallic', effect[0], re.I) else
                     'calligraphy' if re.search(r'毛笔|书法|brush', effect[0], re.I) else 'lettering')
    return style, False


def requested_cover_style(source: str | dict | None) -> str | None:
    if isinstance(source, str):
        if not source.lstrip().startswith('{'):
            return _scan(source)[0]
        try:
            source = json.loads(source)
        except ValueError:
            return None
    if not isinstance(source, dict):
        return None
    # 结构化 brief 只看设计要求，不读 copy、品牌名、素材描述或其它页的 style。
    specs = source.get('page_specs')
    first = specs[0] if isinstance(specs, list) and specs and isinstance(specs[0], dict) else {}
    if _INNER.search(str(first.get('name') or '')):
        return None
    # 归档的 brief 会记录已核准的选择，重跑时不能从模型写的 creative 反推授权。
    choice = source.get('cover_title_art')
    if 'cover_title_art' in source and choice is not True and choice not in ('lettering', 'calligraphy', 'metallic'):
        return None
    selected = 'lettering' if choice is True else choice
    for spec in (source, first):
        for key in ('creative', 'style'):
            value = spec.get(key)
            if isinstance(value, str):
                style, denied = _scan(value, scoped=(spec is first or key == 'style'))
                if denied:
                    return None
                if choice is None:
                    selected = style or selected
    return selected
