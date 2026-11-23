"""在申请素材前补齐逐项配图页的主照片，不重新规划整页。"""
from __future__ import annotations

import json
import re

from ppt.image_layout import photo_spec
from ppt.schema import asset_requests, page_item_targets, storefront_photos


_SYSTEM = (
    '你是主摄影选片编辑。仅从给出的本页摄影要求逐字摘取一个连续短语，描述主照片实际拍什么，'
    '可含原文明确给出的环境、镜头或动作。只返回 {"subject":"原文短语"}。'
    '不要另造物件，不要选独立配图、步骤、文案或版式指令。若原文没有明确的主照片主体，返回 {"subject":null}。'
)
# 把明确属于其他图层的条款排除在可摘取的证据之外。
_OTHER_LAYER = re.compile(r'卡片|小图|插图|逐项配图|图标|横幅|页码|标题|文案|文字框|信息栏')
_LAYOUT = re.compile(r'满幅|全幅|满版|整页|背景图?|主摄影|摄影|照片|主图|铺底|留白|版式|渐变|遮罩|布局|等比裁切|设置|使用|一张|连续|要求|采用|画面|主体|具体|清晰|的|为|铺满|四边|[:：\s]')


def _photo_fragments(spec):
    fragments = []
    for key in ('visual', 'creative', 'style'):
        value = spec.get(key)
        if not isinstance(value, str):
            continue
        for sentence in re.split(r'[。；;\n]', value):
            # 如「三张卡片，分别画……，主摄影拍……」，不能选中卡片的续句。
            other = False
            for clause in re.split(r'[，,]', sentence):
                if re.search(r'主摄影|主照片|整页摄影|背景(?:摄影|照片|拍摄|图|为)', clause):
                    other = False
                if _OTHER_LAYER.search(clause):
                    other = True
                if not other and clause.strip():
                    fragments.append(clause.strip())
    return fragments


def _extract_subject(spec, client):
    explicit = spec.get('subject')
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    fragments = _photo_fragments(spec)
    if not fragments:
        raise ValueError('本页没有可摘取的主摄影要求')
    prompt = json.dumps({'本页摄影要求': fragments}, ensure_ascii=False)
    for attempt in (1, 2):
        try:
            answer = client.chat_json(_SYSTEM, prompt)
            value = answer.get('subject') if isinstance(answer, dict) else None
            subject = value.strip().rstrip('。') if isinstance(value, str) else ''
            if not (subject and len(subject) <= 180 and re.search(r'\w', _LAYOUT.sub('', subject))
                    and any(subject in fragment for fragment in fragments)):
                raise ValueError('未返回可核对的本页主摄影主体原文')
            return subject
        except Exception as exc:
            if attempt == 2:
                raise ValueError(f'定向补齐尝试两次仍失败：{exc}') from exc
            prompt += '\n上次未得到有效结果。仅摘取可见主体原文，不要返回整页版式；没有依据就返回 null。'


def preflight_background_subjects(layout, brief, client):
    """以最终图层判断；仅缺失字段的页面调用客户端，最终走真实素材契约复验。"""
    pages = list(layout['pages'])
    for i, page in enumerate(pages):
        bg = page['bg']
        if bg['type'] != 'image' or str(bg.get('subject') or '').strip():
            continue
        if len(page_item_targets(page, storefront_photos(page))) < 2:
            continue
        try:
            subject = _extract_subject(photo_spec(brief, i), client)
        except ValueError as exc:
            raise ValueError(f'第{i + 1}页逐项配图缺少 bg.subject；{exc}。请在该页需求中明确主照片拍什么。') from exc
        pages[i] = dict(page, bg=dict(bg, subject=subject))
    candidate = dict(layout, pages=pages)
    asset_requests(candidate, brief)  # 本地校验；不调用生图接口。
    return candidate
