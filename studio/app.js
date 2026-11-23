'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const labels = new Map(Object.entries({passed:'已通过', failed:'未通过', active:'进行中', partial:'部分完成'}));
  const ns = 'http://www.w3.org/2000/svg';
  const dateFmt = new Intl.DateTimeFormat('zh-CN', {year:'numeric', month:'2-digit', day:'2-digit'});
  let runs = [], featured = [], heroId = null, selected = null, data = null, page = 0;
  let listBusy = false, seq = 0, detailBusy = false, posting = false, hasList = false, rowsKey = '';
  const drafts = {text:'', brief:''}; let format = 'text';
  function node(tag, text, cls) { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; }
  function msg(id, text = '') { const n = $(id); n.textContent = text; n.hidden = !text; }
  function url(raw) { if (typeof raw !== 'string' || !raw.trim()) return ''; try { const u = new URL(raw, document.baseURI); return /^https?:$/.test(u.protocol) && u.origin === location.origin && !u.username && !u.password ? u.href : ''; } catch { return ''; } }
  function badge(n, status, draftReady) { n.className = 'badge' + (labels.has(status) ? ' ' + status : ''); n.textContent = draftReady && ['active','partial'].includes(status) ? '首稿待审核' : labels.get(status) || '状态未知'; }
  function date(seconds) { const t = new Date(Number(seconds) * 1000); return seconds && Number.isFinite(t.getTime()) ? dateFmt.format(t) : '—'; }
  function pages(r) { return Number.isInteger(r.pages) && r.pages > 0 ? r.pages + ' 页' : '页数待定'; }
  function cover(img, src, alt, fallback) {
    img.alt = alt; if (src && img.dataset.current === src) return;
    img.onload = img.onerror = null; img.hidden = true; img.dataset.current = src;
    if (!src) { img.removeAttribute('src'); return; }
    img.onload = () => { img.hidden = false; if (fallback) fallback.hidden = true; };
    img.onerror = () => { img.hidden = true; if (fallback) { fallback.textContent = '截图未能加载'; fallback.hidden = false; } };
    img.src = src;
  }
  async function api(path, body) {
    const ctrl = new AbortController(), timer = setTimeout(() => ctrl.abort(), 20000);
    try {
      const init = {signal:ctrl.signal, cache:'no-store', credentials:'same-origin', redirect:'error'};
      if (body !== undefined) Object.assign(init, {method:'POST', headers:{'Content-Type':'application/json','X-Studio':'1'}, body:JSON.stringify(body)});
      const response = await fetch(path, init); const info = await response.json().catch(() => null);
      if (!response.ok || !info || info.error) throw new Error(typeof info?.error === 'string' ? info.error : '接口返回异常（HTTP ' + response.status + '）');
      return info;
    } catch (e) { if (e.name === 'AbortError' || e instanceof TypeError) throw new Error(body ? '连接中断，提交结果未确认。请先刷新档案核对，再决定是否重试。' : '连接超时或服务不可达，请重试。'); throw e; }
    finally { clearTimeout(timer); }
  }
  for (let i = 0; i < 33; i++) {
    const p = document.createElementNS(ns, 'path'); p.setAttribute('d', `M${80+i*4} -18 C${395+i*5} ${42-i*2},${40+i*2} ${135+i*2},${270+i*7} 246`); $('fan').append(p);
  }
  function renderHero() {
    const idx = featured.findIndex(r => r.id === heroId), r = featured[idx];
    $('hero-count').textContent = `${idx+1} / ${featured.length}`; $('folio-no').textContent = 'NO. ' + (r ? String(idx+1).padStart(2,'0') : '—');
    $('hero-prev').disabled = $('hero-next').disabled = featured.length < 2;
    $('hero-status').hidden = !r; if (r) badge($('hero-status'), r.status, r.draftReady);
    $('hero-title').textContent = r ? r.title || '未命名演示' : hasList ? '留给下一份好作品' : '暂时无法读取作品';
    $('hero-meta').textContent = r ? [r.brand || '未标注品牌',pages(r),date(r.updated)].join(' · ') : hasList ? '新建一份演示，从简报开始。' : '请检查连接后刷新工作台。';
    $('hero-open').disabled = !r; cover($('hero-image'), url(r?.cover), r ? (r.title || '作品') + '封面' : '');
  }
  function stepHero(delta) { if (!featured.length) return; const i = featured.findIndex(r => r.id === heroId); heroId = featured[(i + delta + featured.length) % featured.length].id; renderHero(); }
  $('hero-prev').onclick = () => stepHero(-1); $('hero-next').onclick = () => stepHero(1); $('hero-open').onclick = () => openDetail(heroId);
  function renderList() {
    const q = $('query').value.trim().toLocaleLowerCase(), status = $('status').value;
    const matches = runs.filter(r => (!status || status === r.status) && [r.title,r.brand,r.id].some(s => String(s ?? '').toLocaleLowerCase().includes(q)));
    $('archive-count').textContent = (status || q ? matches.length + ' / ' : '') + runs.length + ' 个项目';
    $('empty').hidden = matches.length > 0; $('empty').textContent = !hasList ? '尚未取得档案，请刷新重试。' : runs.length ? '没有符合条件的作品，试试其他关键词或状态。' : '档案还是空的。点击「新建演示」开始第一份作品。';
    const key = JSON.stringify(matches); if (key === rowsKey) return; rowsKey = key;
    const fragment = document.createDocumentFragment(), focused = document.activeElement?.closest('.row')?.dataset.id; let nextFocus;
    for (const r of matches) {
      const row = node('button', null, 'row'); row.type = 'button'; row.dataset.id = r.id;
      const project = node('span',null,'project'), thumb = node('span','F.','thumb'), names = node('div'); thumb.setAttribute('aria-hidden','true');
      if (url(r.cover)) { const image = node('img'); image.loading = 'lazy'; cover(image,url(r.cover),''); thumb.append(image); }
      names.append(node('strong',r.title || '未命名演示'),node('small',[r.brand,r.source === 'history' ? '历史导入' : r.source === 'cli' ? '命令行' : '工作台'].filter(Boolean).join(' · '))); project.append(thumb,names);
      const state = node('span'); badge(state,r.status,r.draftReady); const icon = document.createElementNS(ns,'svg'), use = document.createElementNS(ns,'use'); use.setAttribute('href','#i-arrow'); icon.append(use); icon.setAttribute('aria-hidden','true');
      row.append(project,node('span',pages(r),'cell'),node('span',date(r.updated),'cell'),state,icon); row.onclick = () => openDetail(r.id); fragment.append(row); if (row.dataset.id === focused) nextFocus = row;
    }
    $('rows').replaceChildren(fragment); nextFocus?.focus({preventScroll:true});
  }
  async function refreshList() {
    if (listBusy) return; listBusy = true; $('refresh').setAttribute('aria-busy','true');
    try {
      const result = await api('/api/runs'); if (!Array.isArray(result.runs)) throw new Error('作品列表格式异常。');
      runs = result.runs.filter(r => r && typeof r.id === 'string' && r.id).sort((a,b) => (b.updated || 0)-(a.updated || 0)); hasList = true;
      const withCover = runs.filter(r => url(r.cover)); featured = withCover.length ? [...withCover.filter(r => r.status === 'passed'), ...withCover.filter(r => r.status !== 'passed')] : runs;
      if (!featured.some(r => r.id === heroId)) heroId = featured[0]?.id ?? null;
      $('sync').textContent = '自动同步'; $('sync').className = 'sync online'; msg('list-error');
    } catch (e) { $('sync').textContent = '连接异常'; $('sync').className = 'sync offline'; msg('list-error', (hasList ? '同步失败，保留上次档案。' : '档案读取失败。') + e.message); }
    finally { listBusy = false; $('refresh').removeAttribute('aria-busy'); renderHero(); renderList(); }
  }
  $('query').oninput = $('status').onchange = renderList; $('refresh').onclick = refreshList;
  let viewFirstDraft = false;
  function drawPage() {
    const pics = (viewFirstDraft ? data?.draftPreviews : data?.previews) || []; page = Math.max(0,Math.min(page,pics.length-1));
    $('slide-count').textContent = (pics.length ? page+1 : 0) + ' / ' + pics.length;
    $('slide-prev').disabled = !page; $('slide-next').disabled = page+1 >= pics.length;
    const src = url(pics[page]), fallback = $('slide-empty');
    if (!src || src !== $('slide').dataset.current) { fallback.hidden = false; fallback.textContent = src ? '正在加载截图…' : pics.length ? '截图地址不可用' : '暂无页面截图'; }
    cover($('slide'),src,`第 ${page+1} 张作品截图`,fallback);
  }
  function drawDetail() {
    const d = data; $('detail-title').textContent = d.title || '未命名演示'; $('detail-meta').textContent = [d.brand, labels.get(d.status),pages(d),date(d.updated),d.id].filter(Boolean).join(' · ');
    $('switch-preview').hidden = !d.draftPreviews?.length || d.showingDraft;
    $('switch-preview').textContent = viewFirstDraft ? '查看最新预览' : '查看首稿';
    $('preview-kind').textContent = viewFirstDraft ? '首稿（待审核）· 近似版式预览' : d.previewKind || '页面预览'; drawPage();
    const stages = Array.isArray(d.stages) ? d.stages : [], issues = Array.isArray(d.issues) ? d.issues : [];
    $('stages').replaceChildren(...(stages.length ? stages.map(s => node('li',(s.done ? '已完成 · ' : '待完成 · ') + s.label,s.done ? 'done' : '')) : [node('li','尚无阶段记录')]));
    $('issues').replaceChildren(...(issues.length ? issues.map(s => node('li','— ' + s)) : [node('li','暂无审查问题记录')]));
    $('brief-view').textContent = Object.keys(d.brief || {}).length ? JSON.stringify(d.brief,null,2) : '等待简报生成。'; $('download').textContent = d.status === 'failed' ? '下载候选稿' : '下载 PPTX';
    const draftLink = url(d.draftDownload); $('draft-download').hidden = !draftLink;
    if (draftLink) $('draft-download').href = draftLink; else $('draft-download').removeAttribute('href');
    const link = url(d.download); $('download').hidden = !link; $('no-download').hidden = !!(link || draftLink);
    if (link) $('download').href = link; else $('download').removeAttribute('href'); $('rebuild').disabled = !d.canRebuild || posting; $('detail-body').hidden = false;
  }
  async function refreshDetail() {
    if (!selected || detailBusy) return; const token = ++seq, id = selected; detailBusy = true;
    try {
      const d = await api('/api/run?id=' + encodeURIComponent(id)); if (token !== seq) return;
      if (d.id !== id) throw new Error('返回的作品编号不匹配。'); data = d; msg('detail-error'); $('detail-retry').hidden = true; drawDetail();
    } catch (e) { if (token === seq) { msg('detail-error','详情读取失败：' + e.message); if (!data) $('detail-meta').textContent = '未取得作品详情'; $('detail-retry').hidden = false; } }
    finally { if (token === seq) detailBusy = false; }
  }
  function openDetail(id) { if (!id) return; seq++; detailBusy = false; selected = id; data = null; page = 0; viewFirstDraft = false; cover($('slide'),'',''); $('detail-body').hidden = true; $('detail-retry').hidden = true; msg('detail-error'); $('detail-title').textContent = runs.find(r => r.id === id)?.title || '作品详情'; $('detail-meta').textContent = '正在读取详情…'; if (!$('detail').open) $('detail').showModal(); refreshDetail(); }
  $('detail').addEventListener('close',() => { selected = data = null; seq++; detailBusy = false; }); $('detail-retry').onclick = refreshDetail;
  $('switch-preview').onclick = () => { viewFirstDraft = !viewFirstDraft; page = 0; drawDetail(); };
  function turn(delta) { page += delta; drawPage(); } $('slide-prev').onclick = () => turn(-1); $('slide-next').onclick = () => turn(1);
  for (const b of document.querySelectorAll('[data-close]')) b.onclick = () => { if (!posting) b.closest('dialog').close(); };
  for (const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('cancel',e => { if (posting) e.preventDefault(); });
  $('new').onclick = () => { msg('form-error'); $('compose').showModal(); $('brief-input').focus(); };
  $('format').onchange = () => { drafts[format] = $('brief-input').value; format = $('format').value; $('brief-input').value = drafts[format]; $('input-label').textContent = format === 'brief' ? '简报 JSON 对象' : '创作简报'; $('brief-input').placeholder = format === 'brief' ? '{"brand":"品牌名","copy":{"title":"演示标题"},"pages":1}' : '写下主题、受众和希望传达的结论。'; msg('form-error'); };
  $('mode').onchange = () => { $('mode-hint').textContent = $('mode').value === 'live' ? '真实模式将调用已配置的生成服务，可能产生费用。' : '模拟模式适合验证创作流程。'; };
  function lock(on) { posting = on; for (const e of $('form').elements) e.disabled = on; for (const b of document.querySelectorAll('[data-close]')) b.disabled = on; $('rebuild').disabled = on || !data?.canRebuild; }
  $('form').onsubmit = async e => {
    e.preventDefault(); if (posting) return; msg('form-error'); const input = $('brief-input').value.trim(), mode = $('mode').value, body = {mode,maxRework:Number($('rework').value)};
    if (!input) { msg('form-error','请先填写创作简报。'); $('brief-input').focus(); return; }
    if (format === 'brief') { try { body.brief = JSON.parse(input); if (!body.brief || typeof body.brief !== 'object' || Array.isArray(body.brief)) throw new Error(); } catch { msg('form-error','简报必须是有效的 JSON 对象，例如 {"title":"演示标题"}。'); $('brief-input').focus(); return; } } else body.text = input;
    if (mode === 'live' && !window.confirm('真实生成将提交简报至已配置的模型服务，可能产生费用。确认创建？')) return;
    lock(true); $('submit').textContent = '正在提交…';
    try { const res = await api('/api/create', body); if (typeof res.id !== 'string' || !res.id) throw new Error('未返回有效编号，请刷新档案核对。'); $('compose').close(); drafts[format] = ''; $('brief-input').value = ''; openDetail(res.id); refreshList(); }
    catch (err) { msg('form-error',err.message); } finally { lock(false); $('submit').textContent = '创建演示'; }
  };
  $('rebuild').onclick = async () => {
    if (posting || !data?.canRebuild) return;
    if (!window.confirm('将复制现有版式和素材，在新目录离线重建；不重新生图，也不会覆盖原作品。继续？')) return;
    lock(true); $('rebuild').textContent = '正在提交…';
    try { const res = await api('/api/rebuild',{id:selected}); if (typeof res.id !== 'string' || !res.id) throw new Error('未返回有效编号，请刷新档案核对。'); openDetail(res.id); refreshList(); }
    catch (e) { msg('detail-error','重建请求：' + e.message); } finally { lock(false); $('rebuild').textContent = '重新构建'; }
  };
  document.addEventListener('keydown',e => { if (e.ctrlKey || e.metaKey || e.altKey || e.isComposing || e.target.closest('input,select,textarea,[contenteditable=true]')) return;
    if ($('detail').open && data && ['ArrowLeft','ArrowRight'].includes(e.key)) { e.preventDefault(); turn(e.key === 'ArrowLeft' ? -1 : 1); }
    else if (e.key === '/' && !document.querySelector('dialog[open]')) { e.preventDefault(); $('query').focus(); }
  });
  function pollDetail() { if (!document.hidden && !posting && $('detail').open && (!data || ['active','partial'].includes(data.status))) refreshDetail(); }
  function pollList() { if (!document.hidden && !posting) refreshList(); }
  document.addEventListener('visibilitychange',() => { pollDetail(); pollList(); });
  setInterval(pollDetail,2000); setInterval(pollList,10000); refreshList();
})();
