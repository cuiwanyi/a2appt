"""Local-only A2A PPT studio. Start with the pipeline's Python interpreter."""
import argparse
import json
import os
import re
import shutil
import sys
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from run_paths import ARCHIVE, LEGACY_STUDIO, RUNS
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'), '/index.html': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}


def checked(path, base):
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError('无效路径')
    return path


@lru_cache(maxsize=512)
def parsed(path, version, size):
    if size > 16 * 1024 * 1024:
        return {}
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    return data if isinstance(data, dict) else {}


def read(run, name):
    try:
        p = checked(run / name, run)
        s = p.stat()
        return parsed(p, s.st_mtime_ns, s.st_size)
    except (OSError, ValueError):
        return {}


def roots():
    return {'runs': RUNS, 'studio': LEGACY_STUDIO, 'workspace': ARCHIVE}


def lookup(key):
    bases = roots()
    if isinstance(key, str) and ':' in key:
        scope, key = key.split(':', 1)
        if scope not in bases:
            raise ValueError('无效的任务来源')
        bases = {scope: bases[scope]}
    if not isinstance(key, str) or not key or key.startswith('.') or re.search(r'[\\/:\x00-\x1f]', key) or Path(key).name != key:
        raise ValueError('无效的任务编号')
    for base in bases.values():
        p = checked(base / key, base)
        if p.is_dir() and ((p / 'brief.json').is_file() or (p / 'studio_job.json').is_file()):
            return p
    raise FileNotFoundError


def previews(p):
    folder = checked(p / '04_screenshots', p)
    images = {'real': [], 'approx': []}
    if folder.is_dir():
        for f in folder.iterdir():
            m = re.fullmatch(r'slide_(\d+)_(real|approx)\.png', f.name)
            if m and checked(f, p).is_file():
                images[m[2]].append((int(m[1]), f))
    for key, label in [('real', '实际渲染'), ('approx', '近似版式预览')]:
        if images[key]:
            return [f for _, f in sorted(images[key])], label
    f = checked(p / '04_layout_preview.png', p)
    return ([f], '近似版式预览') if f.is_file() else ([], '等待预览')


def public_id(p):
    if p.parent == RUNS:
        return p.name
    return next(scope for scope, base in roots().items() if base == p.parent) + ':' + p.name


def asset_url(p, f):
    return '/api/file?' + urlencode({'id': public_id(p), 'name': f.relative_to(p).as_posix(), 'v': f.stat().st_mtime_ns})


def describe(p, detail=False):
    brief, summary, job = [read(p, n) for n in ('brief.json', '05_summary.json', 'studio_job.json')]
    state = job.get('state')
    status = state if state in ('active','passed','failed') else 'passed' if summary.get('ok') is True else 'failed' if summary.get('ok') is False or (p/'error.txt').is_file() else 'partial'
    shots, kind = previews(p)
    try:
        pages = max(0, int(brief.get('pages') or (summary.get('review_summary') or {}).get('pages') or 0))
    except (ValueError,TypeError):
        pages = 0
    title = (brief.get('copy') if isinstance(brief.get('copy'),dict) else {}).get('title') or job.get('title') or brief.get('brand') or p.name
    updated = max([p.stat().st_mtime] + [(p / n).stat().st_mtime for n in ('trace.jsonl','studio_job.json','05_summary.json') if (p / n).is_file()])
    row = dict(id=public_id(p), title=str(title), brand=str(brief.get('brand') or ''), pages=pages, updated=updated, status=status, cover=asset_url(p,shots[0]) if shots else None, source='history' if p.parent!=RUNS else 'studio' if job else 'cli')
    if not detail:
        return row
    review = read(p,'04_review.json')
    issues = []
    for key in ('error','blocking_issues','warnings'):
        item = summary.get(key) or review.get(key) or (job.get(key) if key=='error' else [])
        for s in item if isinstance(item,list) else [item]:
            if s: issues.append(str(s))
    # Pipeline PASS is not a verdict that every rendered page looks correct.
    visual = read(p, '04_visual_review.json')
    visual_notes = []
    for page in visual.get('pages', []):
        if not isinstance(page, dict): continue
        for check in page.get('checks', []):
            if isinstance(check, dict) and check.get('ok') is False:
                visual_notes.append(f"第 {page.get('page_index', '?')} 页 · {check.get('detail') or check.get('rule') or '视觉审查未通过'}")
    issues = list(dict.fromkeys(visual_notes + issues))
    if not review and not summary and not issues:
        issues.append('尚未产生审查报告。')
    elif not visual and not issues:
        issues.append('结构检查未报告问题；尚无逐页视觉评审结论。')
    deck = checked(p/'deliverable.pptx',p)
    stages = [('需求整理','brief.json'),('版式规划','01_layout.json'),('素材准备','02_assets/manifest.json'),('构建演示稿','03_deck.pptx'),('质量审查','04_review.json'),('交付文件','deliverable.pptx')]
    return dict(row,previews=[asset_url(p,f) for f in shots],previewKind=kind,stages=[dict(label=l,done=(p/n).is_file()) for l,n in stages],issues=issues,download=asset_url(p,deck) if deck.is_file() and status!='active' else None,canRebuild=status in ('passed','failed') and (p/'01_layout.json').is_file() and (p/'02_assets').is_dir(),brief=brief,mode=summary.get('mode') or job.get('mode') or '')


class Handler(BaseHTTPRequestHandler):
    server_version = 'A2AStudio/1'

    def headers_for(self, code, mime, size, extra=()):
        self.send_response(code)
        for k,v in [('Content-Type',mime),('Content-Length',str(size)),('X-Content-Type-Options','nosniff'),('Cache-Control','no-cache'),('Referrer-Policy','no-referrer'),*extra]:
            self.send_header(k,v)
        self.end_headers()

    def reply(self, data, code=200):
        buf = json.dumps(data,ensure_ascii=False).encode('utf-8')
        self.headers_for(code,'application/json; charset=utf-8',len(buf))
        self.wfile.write(buf)

    def send_file(self,f,mime,extra=()):
        with f.open('rb') as stream:
            self.headers_for(200,mime,os.fstat(stream.fileno()).st_size,extra)
            shutil.copyfileobj(stream,self.wfile,256*1024)

    def allowed_host(self):
        port=self.server.server_port
        return self.headers.get('Host') in {f'localhost:{port}', f'127.0.0.1:{port}'}

    def do_GET(self):
        if not self.allowed_host(): return self.reply({'error':'仅支持本机访问'},403)
        u=urlsplit(self.path)
        q=parse_qs(u.query)
        try:
            if u.path in STATIC:
                name,mime=STATIC[u.path]
                return self.send_file(HERE/name,mime)
            if u.path=='/api/runs':
                rows=[]
                for base in roots().values():
                    if not base.is_dir(): continue
                    for p in base.iterdir():
                        try:
                            checked(p,base)
                            if p.is_dir() and ((p/'brief.json').is_file() or (p/'studio_job.json').is_file()): rows.append(describe(p))
                        except (ValueError,OSError): continue
                return self.reply({'runs':sorted(rows,key=lambda r:r['updated'],reverse=True)})
            if u.path in ('/api/run','/api/file'):
                p=lookup(q.get('id',[''])[0])
                if u.path=='/api/run': return self.reply(describe(p,True))
                name=q.get('name',[''])[0]
                if name=='deliverable.pptx':
                    filename=quote(str(describe(p)['title'])[:70]+'.pptx',safe='')
                    return self.send_file(checked(p/name,p),'application/vnd.openxmlformats-officedocument.presentationml.presentation',[('Content-Disposition',"attachment; filename=deck.pptx; filename*=UTF-8''"+filename)])
                if name=='04_layout_preview.png' or re.fullmatch(r'04_screenshots/slide_\d+_(?:real|approx)\.png',name):
                    return self.send_file(checked(p/name,p),'image/png')
            raise FileNotFoundError
        except (ConnectionResetError,BrokenPipeError): pass
        except FileNotFoundError: self.reply({'error':'文件或任务不存在'},404)
        except ValueError as e: self.reply({'error':str(e)},400)
        except OSError: self.reply({'error':'文件暂时无法读取，请稍后刷新'},503)

    def do_POST(self):
        if not self.allowed_host() or self.headers.get('X-Studio')!='1' or self.headers.get('Origin') not in (None,'http://'+self.headers.get('Host','')):
            return self.reply({'error':'只接受本机创作台操作'},403)
        try:
            if self.headers.get_content_type()!='application/json': return self.reply({'error':'需要 JSON 请求'},415)
            n=int(self.headers.get('Content-Length',0))
            if not 0<n<=65536: return self.reply({'error':'需求不能为空或超过 64 KB'},413)
            obj=json.loads(self.rfile.read(n))
            if not isinstance(obj,dict): raise ValueError('需求必须是对象')
            import jobs
            if self.path=='/api/create': key=jobs.create(obj)
            elif self.path=='/api/rebuild':
                run=lookup(obj.get('id'))
                if not describe(run,True)['canRebuild']: raise ValueError('只可重建已有结果且输入完整的任务')
                key=jobs.rebuild(run)
            else: raise FileNotFoundError
            self.reply({'id':key},202)
        except FileNotFoundError: self.reply({'error':'任务或必要文件不存在'},404)
        except (TypeError,ValueError) as e: self.reply({'error':str(e)},400)
        except RuntimeError as e: self.reply({'error':str(e)},409)
        except Exception as e:
            self.log_error('job error: %s',type(e).__name__)
            self.reply({'error':'任务未能启动，请检查项目 Python 环境'},500)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='本地 PPT 创作台')
    parser.add_argument('--port',type=int,default=8765)
    port=parser.parse_args().port
    try: http=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    except OSError: sys.exit(f'端口 {port} 不可用。请用 --port 指定其他空闲端口。')
    print(f'A2A Studio: http://127.0.0.1:{http.server_port}/',flush=True)
    try: http.serve_forever()
    except KeyboardInterrupt: pass
    finally: http.server_close()
