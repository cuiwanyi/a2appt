"""目录意图只来自需求或页角色，不来自册长、页码或普通正文里的编号。"""
import re


_TOC = r'(?:目录(?:页)?|章节(?:导览|导航|一览|列表)|table of contents|contents|\bTOC\b)'
_NO = re.compile(
    r'(?:不(?:需要|要|用|做|设|含|是|加|包含|设置|安排|保留|插入|展示)|无需|无须|无|取消|去掉|删除|跳过|省略|移除|排除)'
    r'\s*(?:单独|额外|专门)?\s*(?:制作|增加|添加|安排|生成|设置|插入|保留|做|加|设)?\s*(?:一页|一个|独立的?)?\s*' + _TOC
    + r'|' + _TOC + r'\s*(?:不(?:需要|要|做|设)|可省略|可选|非必需)'
    + r'|(?:\bno|without|omit|skip)\s+(?:a\s+)?' + _TOC, re.I)
_LABEL = re.compile(r'(?:(?:全册|创意|编号|章节)?目录(?:页)?(?:\s*[/｜|·—-]?\s*contents)?|creative_toc|' + _TOC + r')', re.I)
_REQUEST = re.compile(
    r'(?:需要?|要求|包含|增加|加入|添加|安排|保留|生成|制作|设置|给出|插入|设|做|加|要)\s*(?:一个|一[页张]|独立的?)?\s*' + _TOC
    + r'|第\s*[\d一二三四五六七八九十]+\s*页\s*(?:[：:]|是|为|用|做)?\s*' + _TOC
    + r'|(?:^|[，,；;。\n])\s*' + _TOC + r'\s*(?:[，,；;。:：]|$)', re.I)


def toc_label(text):
    return bool(_LABEL.fullmatch(str(text or '').strip()))


def toc_intent(spec):
    """返回明确要求、明确排除或未指定；不把可见正文当成版式指令。"""
    instructions = '\n'.join(str(spec.get(k) or '') for k in ('prompt', 'creative', 'style', 'brief'))
    excludes = spec.get('exclude') or spec.get('no') or []
    if isinstance(excludes, str):
        excludes = [excludes]
    if spec.get('toc_required') is False or any(toc_label(x) for x in excludes) or _NO.search(instructions):
        return False
    if spec.get('toc_required') is True:
        return True
    if spec.get('layout'):
        return toc_label(spec['layout']) or spec['layout'] == '章节页'
    copy = spec.get('copy') or {}
    titles = [spec.get('name'), spec.get('page_name'), spec.get('title')]
    if isinstance(copy, dict):
        titles.append(copy.get('title'))
    if any(toc_label(x) for x in titles) or any(spec.get(k) for k in ('creative_toc', 'toc_items', 'chapters')) or _REQUEST.search(instructions):
        return True
    return None


def is_contents_page(page):
    return (toc_label(page.get('page_name')) or
            any(el.get('type') == 'toc' or el.get('type') == 'text' and toc_label(el.get('content'))
                for el in page.get('elements', [])))


def missing_contents(layout, brief=None):
    brief = layout if brief is None else brief
    intent = toc_intent(brief)
    pages = layout.get('pages', [])
    if intent is False:
        # 全册明示不要目录时，模型误加的目录同样需要返修；不新增或顺延页。
        if brief.get('toc_required') is False or _NO.search(str(brief.get('creative') or '')):
            return [f'第{i}页 需求明确无目录，请恢复该页正文并保持页数与页序。'
                    for i, page in enumerate(pages, 1) if is_contents_page(page)]
        return []
    requested = [i for i, spec in enumerate(brief.get('page_specs') or []) if toc_intent(spec) is True]
    if requested:
        return [f'第{i+1}页 整deck缺目录页：此页需求明确指定目录，条目应对应真实后续页。'
                for i in requested if i >= len(pages) or not is_contents_page(pages[i])]
    if intent is True and not any(map(is_contents_page, pages)):
        return ['整deck缺目录页：需求明确指定目录，请按原定页序安排，不覆盖已指定的正文页。']
    return []
