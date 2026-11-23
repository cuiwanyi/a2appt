"""CLI 与创作台共用的任务目录；导入时不加载模型配置、不创建目录。"""
import os
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / 'workspace'
LEGACY_STUDIO = ROOT / 'studio' / 'runs'


def external_runs(path):
    base = Path(path).resolve()
    if base.is_relative_to(ROOT) or ROOT.is_relative_to(base):
        raise ValueError('新任务目录必须独立于项目目录')
    return base


def allocate_run(parent, title):
    """直接以 mkdir 预占版本号，避免并发任务选中同一路径。"""
    parent = external_runs(parent)
    parent.mkdir(parents=True, exist_ok=True)
    name = unicodedata.normalize('NFKC', str(title or ''))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '-', name)
    name = re.sub(r'\s+', ' ', name).strip(' #.-')[:52].rstrip(' .') or '未命名画册'
    if Path(name).is_reserved(): name = '_' + name
    for version in range(1, 100000):
        candidate = parent / f'{name}（第{version:02d}版）'
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError('同名任务版本数已达上限，请使用新的任务名')


RUNS = external_runs(os.getenv('A2APPT_RUNS') or os.getenv('A2APPT_STUDIO_RUNS') or 'F:/a2appt-data/runs')
