"""识别整册视觉评审因同类连接故障而全部未核验的情况。

只识别“服务不可用”，不把版式、证据、渲染或程序错误混入；任何一页有
不同原因都不能算统一故障。保留原始错误文本，方便人工复盘。
"""
from __future__ import annotations

import re


def _signature(error):
    text = str(error or '')
    if not text:
        return None
    if '10054' in text or 'Connection reset by peer' in text or '远程主机强迫关闭' in text:
        return 'connection_reset'
    if 'ProxyError' in text or 'Unable to connect to proxy' in text:
        return 'proxy_connect_failed'
    if re.search(r'timed?\s*out|Timeout|ReadTimeout|ConnectTimeout', text, re.I):
        return 'network_timeout'
    if 'ConnectionError' in text or 'Connection broken' in text or 'ConnectionRefusedError' in text:
        return 'connection_broken'
    return None


def detect_review_outage(report):
    """返回可写进 stop_reason 的故障信息；证据不足时返回 None。"""
    if not isinstance(report, dict):
        return None
    visual = report.get('visual_review') or {}
    pages = visual.get('pages') or []
    expected = visual.get('expected_pages')
    if not isinstance(expected, int) or expected <= 0 or len(pages) != expected:
        return None
    if any(p.get('status') != 'unverified' for p in pages):
        return None
    rendering = report.get('rendering') or {}
    if rendering and rendering.get('status') not in (None, 'ok'):
        return None
    errors = [p.get('error') for p in pages]
    signatures = {_signature(e) for e in errors}
    if len(signatures) != 1 or None in signatures:
        return None
    signature = next(iter(signatures))
    return {
        'code': 'review_outage',
        'signature': signature,
        'affected_pages': [p.get('page_index') for p in pages],
        'detail': f'评审服务不可用（{signature}）：{expected} 页均未获得有效视觉结论，保留候选稿，不进行版式返修',
        'errors': errors,
    }
