"""Studio 写任务：服务只负责启动，所有流水线状态和临时文件归属新任务。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import traceback
from uuid import uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from run_paths import ARCHIVE, LEGACY_STUDIO, RUNS as DATA, allocate_run
_lock = threading.Lock()
_process: subprocess.Popen | None = None
_current: Path | None = None
_LAYOUTS = ('03_effective_layout.json', '01_layout_rework.json', '01_layout_coverart.json', '01_layout.json')


def _read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _save(path, obj):
    tmp = path.with_name('.' + uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def _watch(proc, root, meta):
    code = proc.wait()
    if _read(root / 'studio_job.json')['state'] == 'active':
        _save(root / 'studio_job.json', dict(meta, state='failed', error=f'工作进程提前退出（{code}），详见 studio_worker.log'))


def _launch(request, title):
    global _process, _current
    with _lock:
        if _process is not None and _process.poll() is None:
            if _read(_current / 'studio_job.json')['state'] == 'active':
                raise RuntimeError('已有 Studio 写任务正在执行，请稍后再试')
            # 状态是工作进程最后一次写入；等待其真正退出再接受下一单。
            _process.wait(timeout=5)
        root = allocate_run(DATA, title)
        meta = dict(state='active', title=title[:100], mode=request['mode'])
        _save(root / 'studio_job.json', meta)
        try:
            _save(root / 'studio_input.json', request)
            scratch = root / '.tmp'
            scratch.mkdir()
            env = dict(os.environ, TMP=str(scratch), TEMP=str(scratch), TMPDIR=str(scratch),
                       A2APPT_RUNS=str(DATA), A2A_RENDERER='libreoffice',
                       A2A_MOCK='0' if request['mode'] == 'live' else '1',
                       PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
            with (root / 'studio_worker.log').open('wb') as log:
                proc = subprocess.Popen([sys.executable, '-B', str(HERE / 'jobs.py'), 'worker'],
                                        cwd=root, env=env, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT)
            _process, _current = proc, root
            threading.Thread(target=_watch, args=(proc, root, meta), daemon=True).start()
        except OSError as exc:
            _save(root / 'studio_job.json', dict(meta, state='failed', error=str(exc)))
            raise
        return root.name


def create(payload: dict) -> str:
    if not isinstance(payload, dict) or set(payload) - {'brief', 'text', 'mode', 'maxRework'}:
        raise ValueError('无效需求参数')
    mode, limit = payload.get('mode', 'mock'), payload.get('maxRework', 1)
    if mode not in ('mock', 'live') or type(limit) is not int or not 0 <= limit <= 3:
        raise ValueError('mode 须为 mock 或 live；maxRework 须为 0 到 3 的整数')
    if ('text' in payload) == ('brief' in payload):
        raise ValueError('须且只能提供 text 或 brief')
    if 'text' in payload:
        text = payload['text']
        if not isinstance(text, str) or not text.strip():
            raise ValueError('text 须为非空文字')
        title = text.strip().splitlines()[0]
    else:
        brief = payload['brief']
        if not isinstance(brief, dict) or not brief:
            raise ValueError('brief 须为非空 JSON 对象')
        title = str((brief.get('copy') or {}).get('title') if isinstance(brief.get('copy'), dict) else '')
        title = title if title not in ('', 'None') else str(brief.get('brand') or '结构化简报')
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 65536:
        raise ValueError('简报不能超过 64 KB')
    return _launch(dict(payload, mode=mode, maxRework=limit), title)


def _terminal(source):
    if source.parent not in (DATA, ARCHIVE, LEGACY_STUDIO) or source.resolve(strict=True) != source:
        raise ValueError('来源不在已知任务目录中')
    job = source / 'studio_job.json'
    if not job.resolve().is_relative_to(source):
        raise ValueError('无效任务状态路径')
    if job.exists():
        if _read(job).get('state') not in ('passed', 'failed'):
            raise ValueError('不能重建进行中的任务')
    elif not (source / '05_summary.json').is_file():
        raise ValueError('来源任务尚未结束，不能重建')


def rebuild(source: Path) -> str:
    source = Path(source).resolve(strict=True)
    if not (source / '01_layout.json').is_file() or not (source / '02_assets').is_dir():
        raise ValueError('重建需要 01_layout.json 和 02_assets')
    _terminal(source)
    return _launch({'source': str(source), 'mode': 'rebuild'}, '重建 · ' + source.name)


def _guard_writes(root):
    # 模型给出的素材 ID 也会被下游用作文件名；在真实写入前统一检查解析后的路径。
    def check(path):
        if path is not None and not isinstance(path, int) and not Path(os.fsdecode(path)).resolve().is_relative_to(root):
            raise PermissionError('拒绝写入任务目录以外的位置')

    def audit(event, args):
        if event == 'open' and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            check(args[0])
        elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.utime'):
            check(args[0])
        elif event == 'os.rename':
            check(args[0]); check(args[1])
        elif event in ('os.link', 'os.symlink'):
            raise PermissionError('任务目录不允许创建文件链接')
    sys.addaudithook(audit)


def _snapshot(source, root):
    """只复制重建输入。绝不使用历史 manifest 里的绝对路径作为活动素材。"""
    _terminal(source)
    def copy(p):
        if not p.resolve(strict=True).is_relative_to(source) or p.is_symlink() or getattr(p.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('重建输入不能包含链接：' + p.name)
        dest = root / p.relative_to(source)
        if p.is_dir():
            dest.mkdir()
            for child in p.iterdir():
                copy(child)
        elif p.is_file():
            shutil.copyfile(p, dest)
        else:
            raise ValueError('无效重建输入：' + p.name)
    for name in (*_LAYOUTS, '02_assets', 'brief.json', '00_request.txt'):
        if (source / name).exists():
            copy(source / name)
    folder = root / '02_assets'
    assets = {p.stem: str(p) for p in folder.glob('*') if p.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp')}
    manifest = folder / 'manifest.json'
    data = _read(manifest) if manifest.exists() else {'images': [], 'mask_slices': []}

    def local(value):
        parts = Path(str(value).replace('\\', '/')).parts
        indices = [i for i, part in enumerate(parts) if part.lower() == '02_assets']
        rel = parts[indices[-1]+1:] if indices else parts
        p = folder.joinpath(*rel).resolve()
        if not p.is_relative_to(folder) or not p.is_file():
            raise ValueError('缺少任务内素材：' + str(value))
        return str(p)
    for entry in data.get('images', []):
        assets[entry['asset_id']] = entry['path'] = local(entry['path'])
        if 'candidates' in entry:
            entry['candidates'] = [local(p) for p in entry['candidates']]
    for entry in data.get('mask_slices', []):
        entry['slices'] = [local(p) for p in entry['slices']]
        for i, p in enumerate(entry['slices']):
            assets[f"{entry['asset_id']}__{i}"] = p
    data['decoration_svg'] = [str(p) for p in folder.glob('*.svg')]
    _save(manifest, data)
    return _read(next(root / name for name in _LAYOUTS if (root / name).exists())), assets


def _run(root, request):
    # LibreOffice 转换失败（例如 Windows 路径过长）也须留下结构自检和近似预览。
    from functools import partial
    from types import SimpleNamespace
    from ppt import render
    from agents import reviewer
    # 临时 profile 有时含超长路径，清理错误不应丢弃已渲染的图片与文本层。
    render.tempfile = SimpleNamespace(mkdtemp=tempfile.mkdtemp, TemporaryDirectory=partial(
        tempfile.TemporaryDirectory, ignore_cleanup_errors=True))
    def isolated_render(*args, **kwargs):
        try:
            return render.try_render(*args, **kwargs)
        except Exception as exc:
            return dict(status='skipped', reason=f'独立渲染失败：{exc}', renderer=None,
                        images=[], page_texts=[], page_count=0)
    reviewer.try_render = isolated_render
    from make_ppt import make_brief, normalize_brief
    from services.llm import MockLLM, LLMClient
    from a2a.orchestrator import Orchestrator
    from a2a.types import text_part

    mode = request['mode']
    client = LLMClient() if mode == 'live' else None
    if mode == 'rebuild':
        layout, assets = _snapshot(Path(request['source']), root)
        brief = _read(root / 'brief.json') if (root / 'brief.json').exists() else {'pages': len(layout['pages'])}
    elif 'brief' in request:
        brief = normalize_brief(request['brief'])
    else:
        brief, _ = make_brief(request['text'], client)
    orch = Orchestrator(brief, llm=client or MockLLM(brief), mock=client is None,
                        root=root, max_rework=request.get('maxRework', 0))
    if mode == 'rebuild':
        orch.ctx.layout, orch.ctx.assets = layout, assets
        for stage, skill in ((3, 'build_pptx'), (4, 'review_deck')):
            orch.bus.send(skill, text_part('使用已复制的版式和素材离线重建'), stage=stage, attempt=1)
        review = orch.ctx.review
        summary = dict(mode=mode, root=str(root), run_id=root.name, attempts=1, ok=review['ok'],
                       candidate=orch.ctx.deck_path, review_summary=review['summary'],
                       quality_status=review['quality_status'], blocking_issues=review['issues'],
                       warnings=review['warnings'], rendering=review['rendering'], review=str(root / '04_review.json'))
        orch.bus.write_trace(root / 'trace_full.json')
    else:
        (root / '00_request.txt').write_text(request.get('text') or json.dumps(request['brief'], ensure_ascii=False), encoding='utf-8')
        summary = orch.run()
        review = orch.ctx.review or {}
    if summary.get('error') or not review:
        raise RuntimeError(summary.get('error') or '流程未产生自检报告')
    # 离线产物可下载，但不能借此改写标准流水线的视觉未核验结论。
    if mode != 'live':
        final = root / 'deliverable.pptx'
        shutil.copyfile(orch.ctx.deck_path, final)
        summary['deliverable'] = str(final)
    if review['rendering']['status'] != 'ok':
        from ppt.preview import render_layout_preview
        shots = root / '04_screenshots'
        approx = [str(render_layout_preview(dict(orch.ctx.layout, pages=[page]), orch.ctx.assets,
                      shots / f'slide_{i}_approx.png')) for i, page in enumerate(orch.ctx.layout['pages'], 1)]
        review['approximate_previews'] = approx
        summary['warnings'].append('真实渲染不可用，已生成逐页近似预览：' + review['rendering'].get('reason', ''))
        _save(root / '04_review.json', review)
    _save(root / '05_summary.json', summary)
    if not summary['ok']:
        raise RuntimeError('；'.join(summary.get('blocking_issues', [])) or '未通过自检')


def _worker():
    root = Path.cwd().resolve()
    if root.parent != DATA or not root.name.startswith('studio_'):
        raise ValueError('工作目录不是 Studio 任务')
    meta = _read(root / 'studio_job.json')
    if meta['state'] != 'active':
        raise ValueError('任务已结束')
    _guard_writes(root)
    # 强制使用隔离 profile 的 LibreOffice；禁止回退到可能绑定现有演示窗口的 COM。
    for name in ('win32com', 'win32com.client', 'comtypes', 'comtypes.client', 'pythoncom'):
        sys.modules[name] = None
    tempfile.tempdir = str(root / '.tmp')
    try:
        _run(root, _read(root / 'studio_input.json'))
        meta['state'] = 'passed'
    except Exception as exc:
        traceback.print_exc()
        meta.update(state='failed', error=str(exc) or type(exc).__name__)
    finally:
        shutil.rmtree(root / '.tmp', ignore_errors=True)
        _save(root / 'studio_job.json', meta)


if __name__ == '__main__' and sys.argv[1:] == ['worker']:
    _worker()
