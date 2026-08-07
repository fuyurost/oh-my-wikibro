/* oh-my-wikibro Web UI — 单页应用逻辑(原生 JS,无框架)
 * 视图:站点列表(#/)、站点详情(#/site/<name>)、校验报告(#/site/<name>/verify)
 * 实时进度:EventSource (SSE) 接收抓取事件
 */
'use strict';

/* ---------------- helpers ---------------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;

const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, opts) {
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { /* 非 JSON 响应 */ }
  if (!r.ok) {
    let msg = data && (data.detail || data.message);
    if (msg && typeof msg !== 'string') msg = JSON.stringify(msg);
    throw new Error(msg || `HTTP ${r.status}`);
  }
  return data;
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleString('zh-CN', { hour12: false });
}
function fmtSize(n) {
  if (n == null) return '';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}

let toastTimer = null;
function toast(msg, kind = 'info') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show ' + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3400);
}

function animateCount(el, target, dur = 750) {
  if (REDUCED) { el.textContent = String(target); return; }
  const t0 = performance.now();
  const step = now => {
    const t = Math.min(1, (now - t0) / dur);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = String(Math.round(target * eased));
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function skeleton(n, cls) {
  const frag = document.createDocumentFragment();
  for (let i = 0; i < n; i++) {
    const s = document.createElement('div');
    s.className = 'skeleton ' + cls;
    s.style.animationDelay = (i * 80) + 'ms';
    frag.append(s);
  }
  return frag;
}

function emptyState(title, sub) {
  const d = document.createElement('div');
  d.className = 'empty';
  d.innerHTML = `<h2>${esc(title)}</h2><p class="muted">${esc(sub || '')}</p>`;
  return d;
}

/* ---------------- 抓取状态 + SSE ---------------- */
const STAGE_LABELS = {
  discover: '发现页面', page: '抓取页面',
  md: '生成 Markdown', json: '生成 JSON',
  jsonl: '生成语料 (JSONL/MD)', pdf: '转换 PDF',
};
const fetchState = {
  running: false, site: '', url: '', done: 0, total: 0, stage: 'discover',
  finished: null, sawActive: false,
};

function updateProgressUI() {
  const panel = $('#progress-panel');
  const status = $('#fetch-status');
  if (!fetchState.running) {
    panel.hidden = true;
    status.hidden = true;
    return;
  }
  panel.hidden = false;
  status.hidden = false;
  status.textContent = `抓取中: ${fetchState.site}`;
  $('#progress-site').textContent = `正在抓取 ${fetchState.site}`;
  $('#progress-stage').textContent = STAGE_LABELS[fetchState.stage] || fetchState.stage;
  const pct = fetchState.total > 0 ? Math.round(fetchState.done / fetchState.total * 100) : 0;
  $('#progress-fill').style.width = pct + '%';
  $('#progress-count').textContent = `${fetchState.done} / ${fetchState.total}`;
  $('#progress-pct').textContent = pct + '%';
  $('.progress-track').setAttribute('aria-valuenow', String(pct));
}

function refreshBadges() {
  if (parseRoute().view !== 'list') return;
  $$('.card').forEach(card => {
    const nameEl = card.querySelector('.card-name');
    const meta = card.querySelector('.card-meta');
    if (!nameEl || !meta) return;
    const running = fetchState.running && fetchState.site === nameEl.textContent;
    const old = card.querySelector('.badge.running');
    if (running && !old) {
      const b = document.createElement('span');
      b.className = 'badge running';
      b.innerHTML = '<span class="pulse-dot"></span>抓取中';
      meta.append(b);
    } else if (!running && old) {
      old.remove();
    }
  });
}

function handleEvent(ev) {
  switch (ev.type) {
    case 'start':
      Object.assign(fetchState, {
        running: true, site: ev.site, url: ev.url,
        done: 0, total: 0, stage: 'discover', finished: null, sawActive: true,
      });
      updateProgressUI();
      refreshBadges();
      break;
    case 'page_done':
      fetchState.running = true;
      fetchState.site = ev.site || fetchState.site;
      fetchState.done = ev.done; fetchState.total = ev.total;
      fetchState.stage = 'page';
      fetchState.sawActive = true;
      updateProgressUI();
      break;
    case 'stage':
      fetchState.running = true;
      fetchState.site = ev.site || fetchState.site;
      fetchState.done = ev.done; fetchState.total = ev.total;
      fetchState.stage = ev.stage;
      fetchState.sawActive = true;
      updateProgressUI();
      break;
    case 'done':
      fetchState.running = false;
      fetchState.finished = ev;
      updateProgressUI();
      refreshBadges();
      if (fetchState.sawActive) {
        const cur = parseRoute();
        if (ev.ok) {
          toast(`抓取完成: ${ev.site}(成功 ${ev.pages} 页 / 失败 ${ev.failed} 页)`, 'success');
        } else {
          toast(`抓取失败: ${ev.site} — ${ev.error || '未知错误'}`, 'error');
        }
        if (cur.view === 'list') render();
        else if (cur.site === ev.site) render();
      }
      fetchState.sawActive = false;
      break;
  }
}

let es = null;
function connectSSE() {
  if (es) es.close();
  es = new EventSource('/api/events');
  es.onmessage = e => {
    try { handleEvent(JSON.parse(e.data)); } catch (err) { /* 忽略坏事件 */ }
  };
  es.onerror = () => { /* 浏览器自动重连 */ };
}

/* ---------------- 路由 ---------------- */
const view = $('#view');

function parseRoute() {
  const h = location.hash.replace(/^#\/?/, '');
  const parts = h.split('/').filter(Boolean);
  if (parts[0] === 'site') {
    const site = decodeURIComponent(parts[1] || '');
    if (site) return parts[2] === 'verify' ? { view: 'verify', site } : { view: 'detail', site };
  }
  return { view: 'list' };
}

async function render() {
  view.innerHTML = '';
  view.classList.remove('view-enter');
  void view.offsetWidth;
  view.classList.add('view-enter');
  const route = parseRoute();
  try {
    if (route.view === 'list') await renderList();
    else if (route.view === 'detail') await renderDetail(route.site);
    else await renderVerify(route.site);
  } catch (e) {
    view.append(emptyState('加载失败', e.message));
  }
}
window.addEventListener('hashchange', render);

/* ---------------- 站点列表 ---------------- */
async function renderList() {
  const h = document.createElement('div');
  h.className = 'sites-header';
  h.innerHTML = '<h1>已抓取站点</h1><span id="sites-meta" class="muted"></span>';
  view.append(h);
  const grid = document.createElement('div');
  grid.className = 'grid';
  view.append(grid);
  grid.append(skeleton(6, 'skeleton-card'));
  try {
    const data = await api('/api/sites');
    const sites = data.sites || [];
    grid.innerHTML = '';
    $('#sites-meta').textContent = `共 ${sites.length} 个站点`;
    if (!sites.length) {
      grid.replaceWith(emptyState('还没有抓取任何站点', '点击右上角「抓取新站点」开始'));
      return;
    }
    sites.forEach((s, i) => grid.append(siteCard(s, i)));
  } catch (e) {
    grid.replaceWith(emptyState('站点列表加载失败', e.message));
  }
}

function siteCard(s, i) {
  const card = document.createElement('article');
  card.className = 'card';
  card.style.animationDelay = `${Math.min(i * 70, 620)}ms`;
  card.tabIndex = 0;
  card.setAttribute('role', 'button');
  card.setAttribute('aria-label', `查看站点 ${s.name}`);
  const running = fetchState.running && fetchState.site === s.name;
  card.innerHTML = `
    <div class="card-top">
      <h3 class="card-name" title="${esc(s.name)}">${esc(s.name)}</h3>
      <span class="badge adapter">${esc(s.adapter || '—')}</span>
    </div>
    <div class="card-url" title="${esc(s.url)}">${esc(s.url || '—')}</div>
    <div class="card-stats">
      <div class="stat"><b data-count="${s.pages || 0}">0</b><span>页面</span></div>
      <div class="stat ${s.failed ? 'fail' : 'ok'}"><b data-count="${s.failed || 0}">0</b><span>失败</span></div>
      <div class="stat"><b data-count="${s.chunks || 0}">0</b><span>chunks</span></div>
    </div>
    <div class="card-meta">
      <span>来源 ${esc(s.source || '—')}${running ? '<span class="badge running"><span class="pulse-dot"></span>抓取中</span>' : ''}</span>
      <span>${fmtTime(s.updated)}</span>
    </div>`;
  const go = () => { location.hash = `#/site/${encodeURIComponent(s.name)}`; };
  card.addEventListener('click', go);
  card.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
  });
  requestAnimationFrame(() => {
    $$('[data-count]', card).forEach(el => animateCount(el, Number(el.dataset.count)));
  });
  return card;
}

/* ---------------- 站点详情 ---------------- */
let activeSite = null;
let activeRow = null;

async function renderDetail(name) {
  activeSite = name;
  const head = document.createElement('div');
  head.className = 'sites-header';
  head.innerHTML = '<h1>站点详情</h1>';
  view.append(head);
  const sk = document.createElement('div');
  sk.className = 'stat-cards';
  sk.append(skeleton(4, 'skeleton-stat'));
  view.append(sk);
  const body = document.createElement('div');
  body.className = 'detail-body';
  const p1 = document.createElement('div');
  p1.className = 'panel';
  p1.append(skeleton(1, 'skeleton-panel'));
  body.append(p1);
  view.append(body);

  const [site, files] = await Promise.all([
    api(`/api/site/${encodeURIComponent(name)}`),
    api(`/api/site/${encodeURIComponent(name)}/files?fmt=md`),
  ]);
  buildDetail(site, files);
}

function buildDetail(site, files) {
  const f = site.formats || {};
  const corp = f.corpus || [];
  view.innerHTML = '';

  /* 头部 */
  const head = document.createElement('div');
  head.className = 'detail-head';
  const badges = [site.adapter, site.source].filter(Boolean)
    .map(b => `<span class="badge ${b === site.adapter ? 'adapter' : 'source'}">${esc(b)}</span>`).join(' ');
  head.innerHTML = `
    <div class="detail-title">
      <h1>${esc(site.name)}</h1>
      <div><span class="url">${esc(site.url || '—')}</span> ${badges}</div>
    </div>
    <div class="detail-actions">
      <a class="btn btn-ghost" href="#/site/${encodeURIComponent(site.name)}/verify">校验报告</a>
      <a class="btn btn-ghost" href="#/">← 返回列表</a>
    </div>`;
  view.append(head);

  /* 统计卡 */
  const cards = document.createElement('div');
  cards.className = 'stat-cards';
  const stats = [
    { label: '总页数', value: site.pages, cls: 'accent' },
    { label: '成功', value: site.pages - site.failed, cls: 'ok' },
    { label: '失败', value: site.failed, cls: site.failed ? 'fail' : 'ok' },
    { label: 'chunks', value: site.chunks || 0, cls: '' },
    { label: 'md 文件', value: f.md || 0, cls: '' },
    { label: 'json 文件', value: f.json || 0, cls: '' },
    { label: 'pdf 文件', value: f.pdf || 0, cls: '' },
    { label: 'corpus 文件', value: corp.length, cls: '' },
  ];
  stats.forEach((st, i) => {
    const c = document.createElement('div');
    c.className = 'stat-card ' + st.cls;
    c.style.animationDelay = `${i * 60}ms`;
    c.innerHTML = `<div class="label">${st.label}</div><div class="value" data-count="${st.value}">0</div>`;
    cards.append(c);
  });
  view.append(cards);
  requestAnimationFrame(() => {
    $$('[data-count]', cards).forEach(el => animateCount(el, Number(el.dataset.count)));
  });

  /* corpus 下载 */
  if (corp.length) {
    const row = document.createElement('div');
    row.className = 'panel-body corpus-links';
    row.style.padding = '12px 16px';
    row.innerHTML = '<span class="muted" style="align-self:center">语料下载:</span>';
    for (const name of corp) {
      const a = document.createElement('a');
      a.className = 'download-btn';
      a.href = `/api/site/${encodeURIComponent(site.name)}/file?path=${encodeURIComponent(name)}&download=1`;
      a.textContent = '⬇ ' + name;
      row.append(a);
    }
    view.append(row);
  }

  /* 文件浏览 + 预览 */
  const body = document.createElement('div');
  body.className = 'detail-body';
  body.innerHTML = `
    <div class="panel" id="files-panel">
      <div class="panel-head">
        <div class="tabs" id="fmt-tabs">
          ${['md', 'json', 'pdf', 'corpus'].map(fmt =>
            `<button class="tab ${fmt === 'md' ? 'active' : ''}" data-fmt="${fmt}">${fmt}</button>`).join('')}
        </div>
      </div>
      <div class="file-tree" id="file-tree"></div>
    </div>
    <div class="panel">
      <div class="panel-head">
        <h3 id="preview-title">文件预览</h3>
        <span class="muted" id="preview-meta"></span>
      </div>
      <div class="preview" id="preview"><div class="tree-empty">← 点击左侧文件查看内容</div></div>
    </div>`;
  view.append(body);

  $('#fmt-tabs').addEventListener('click', e => {
    const btn = e.target.closest('.tab');
    if (!btn) return;
    $$('#fmt-tabs .tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    loadTree(btn.dataset.fmt);
  });

  $('#file-tree').addEventListener('click', e => {
    const btn = e.target.closest('[data-dl]');
    if (btn) {
      e.stopPropagation();
      window.location.href = `/api/site/${encodeURIComponent(site.name)}/file?path=${encodeURIComponent(btn.dataset.dl)}&download=1`;
      return;
    }
    const row = e.target.closest('.file-row');
    if (row) selectFile(row);
  });

  loadTree('md', files);

  /* 失败页 */
  const failed = site.failed_urls || [];
  if (failed.length) {
    const sec = document.createElement('section');
    sec.className = 'failed-section';
    const items = failed.slice(0, 50).map((u, i) => `
      <details class="failed-item" style="animation-delay:${Math.min(i * 40, 500)}ms">
        <summary>${esc(u.url)}</summary>
        <pre class="err">${esc(u.error || '无错误信息')}</pre>
      </details>`).join('');
    const more = failed.length > 50 ? `<p class="muted">… 另有 ${failed.length - 50} 个失败页</p>` : '';
    sec.innerHTML = `
      <div class="panel-head" style="border:1px solid var(--border);border-bottom:none;border-radius:10px 10px 0 0">
        <h3>失败页面 (${failed.length})</h3>
      </div>
      <div class="failed-list" style="border:1px solid var(--border);border-radius:0 0 10px 10px;padding:12px">${items}${more}</div>`;
    view.append(sec);
  }
}

async function loadTree(fmt, cached) {
  const tree = $('#file-tree');
  if (fmt === 'md' && cached) {
    renderTreeInto(tree, cached.files);
    return;
  }
  tree.innerHTML = '';
  tree.append(skeleton(5, 'skeleton-panel'));
  tree.querySelectorAll('.skeleton-panel').forEach(s => {
    s.style.minHeight = '28px'; s.style.border = 'none'; s.style.marginBottom = '8px';
  });
  try {
    const data = await api(`/api/site/${encodeURIComponent(activeSite)}/files?fmt=${fmt}`);
    renderTreeInto(tree, data.files);
  } catch (e) {
    tree.innerHTML = `<div class="tree-empty">加载失败: ${esc(e.message)}</div>`;
  }
}

function renderTreeInto(tree, files) {
  tree.innerHTML = '';
  if (!files || !files.length) {
    tree.innerHTML = '<div class="tree-empty">该格式下暂无文件</div>';
    return;
  }
  const root = {};
  for (const f of files) {
    const parts = f.path.split('/');
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) node = (node[parts[i]] ??= {});
    (node.__files ??= []).push(f);
  }
  tree.append(renderTree(root, 0));
}

function renderTree(node, depth) {
  const frag = document.createDocumentFragment();
  for (const key of Object.keys(node).sort()) {
    if (key === '__files') continue;
    const d = document.createElement('details');
    if (depth === 0) d.open = true;
    const sum = document.createElement('summary');
    sum.textContent = key;
    d.append(sum, renderTree(node[key], depth + 1));
    frag.append(d);
  }
  for (const f of (node.__files || []).sort((a, b) => a.path.localeCompare(b.path))) {
    const row = document.createElement('div');
    row.className = 'file-row';
    row.dataset.path = f.path;
    row.innerHTML = `
      <span class="fname" title="${esc(f.path)}">${esc(f.name)}</span>
      <span class="fsize">${fmtSize(f.size)}</span>
      <button class="download-btn mini-dl" data-dl="${esc(f.path)}" title="下载 ${esc(f.name)}">⬇</button>`;
    frag.append(row);
  }
  return frag;
}

async function selectFile(row) {
  const path = row.dataset.path;
  const preview = $('#preview');
  $('#preview-title').textContent = path.split('/').pop();
  if (activeRow) activeRow.classList.remove('active');
  row.classList.add('active');
  activeRow = row;
  preview.innerHTML = '<div class="skeleton skeleton-panel" style="min-height:120px;border:none;border-radius:8px"></div>';
  try {
    const data = await api(`/api/site/${encodeURIComponent(activeSite)}/file?path=${encodeURIComponent(path)}`);
    $('#preview-meta').textContent = fmtSize(data.size);
    if (path.endsWith('.md')) {
      preview.innerHTML = renderMarkdown(data.content);
    } else {
      const pre = document.createElement('pre');
      pre.style.cssText = 'margin:0;white-space:pre-wrap;word-break:break-all';
      pre.textContent = data.content;
      preview.innerHTML = '';
      preview.append(pre);
    }
  } catch (e) {
    preview.innerHTML = `<div class="tree-empty">加载失败: ${esc(e.message)}</div>`;
  }
}

/* 轻量 Markdown 渲染:先转义再结构化,覆盖标题/代码/列表/引用/链接/强调/分隔线 */
function renderMarkdown(src) {
  let md = String(src || '').replace(/^\uFEFF/, '').replace(/^---\n[\s\S]*?\n---\n?/, '');
  const out = [];
  let inCode = false;
  let listOpen = null;
  const inline = s => s
    .replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, '<img alt="$1" src="$2">')
    .replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/`([^`\n]+)`/g, '<code>$1</code>');
  const closeList = () => {
    if (listOpen) { out.push(`</${listOpen}>`); listOpen = null; }
  };
  for (const raw of md.split('\n')) {
    const line = raw;
    if (/^```/.test(line)) {
      closeList();
      if (!inCode) { inCode = true; out.push('<pre><code>'); }
      else { inCode = false; out.push('</code></pre>'); }
      continue;
    }
    if (inCode) { out.push(esc(line)); continue; }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      closeList();
      out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`);
      continue;
    }
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    if (ul) {
      if (listOpen !== 'ul') { closeList(); listOpen = 'ul'; out.push('<ul>'); }
      out.push(`<li>${inline(ul[1])}</li>`);
      continue;
    }
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ol) {
      if (listOpen !== 'ol') { closeList(); listOpen = 'ol'; out.push('<ol>'); }
      out.push(`<li>${inline(ol[1])}</li>`);
      continue;
    }
    closeList();
    const bq = line.match(/^\s*>\s?(.*)$/);
    if (bq) { out.push(`<blockquote>${inline(bq[1])}</blockquote>`); continue; }
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) { out.push('<hr>'); continue; }
    if (/^\s*$/.test(line)) continue;
    out.push(`<p>${inline(line)}</p>`);
  }
  if (inCode) out.push('</code></pre>');
  closeList();
  return out.join('\n');
}

/* ---------------- 校验报告 ---------------- */
async function renderVerify(name) {
  activeSite = name;
  const h = document.createElement('div');
  h.className = 'sites-header';
  h.innerHTML = '<h1>校验报告</h1><a class="btn btn-ghost" href="#/">← 返回列表</a>';
  view.append(h);
  const sk = document.createElement('div');
  sk.className = 'stat-cards';
  sk.append(skeleton(3, 'skeleton-stat'));
  view.append(sk);

  let report;
  try {
    report = await api(`/api/verify?site=${encodeURIComponent(name)}`);
  } catch (e) {
    view.append(emptyState('校验失败', e.message));
    return;
  }
  view.innerHTML = '';
  view.append(h);

  const head = document.createElement('div');
  head.className = 'detail-head';
  head.innerHTML = `
    <div class="detail-title">
      <h1>校验报告</h1>
      <div><span class="url">${esc(report.site)}</span>
        <span class="badge adapter">${esc((report.manifest && report.manifest.adapter) || '—')}</span></div>
    </div>
    <div class="detail-actions">
      <a class="btn btn-ghost" href="#/site/${encodeURIComponent(name)}">← 返回详情</a>
      <button class="btn btn-primary" id="btn-reverify">重新校验</button>
    </div>`;
  view.append(head);

  const cards = document.createElement('div');
  cards.className = 'stat-cards';
  const missingCount = (report.missing || []).length;
  const stats = [
    { label: '校验页数', value: report.checked, cls: 'accent' },
    { label: '通过', value: report.ok, cls: 'ok' },
    { label: '缺失', value: missingCount, cls: missingCount ? 'fail' : 'ok' },
  ];
  stats.forEach((st, i) => {
    const c = document.createElement('div');
    c.className = 'stat-card ' + st.cls;
    c.style.animationDelay = `${i * 60}ms`;
    c.innerHTML = `<div class="label">${st.label}</div><div class="value" data-count="${st.value}">0</div>`;
    cards.append(c);
  });
  view.append(cards);
  requestAnimationFrame(() => {
    $$('[data-count]', cards).forEach(el => animateCount(el, Number(el.dataset.count)));
  });

  const missing = report.missing || [];
  const banner = document.createElement('div');
  banner.className = 'verify-banner ' + (missing.length ? 'fail' : 'pass');
  banner.textContent = missing.length
    ? `校验未通过:${missing.length} 个页面缺少对应输出文件`
    : `校验通过:${report.checked} 个页面均有对应 md/json 输出`;
  view.append(banner);

  if (missing.length) {
    const sec = document.createElement('section');
    sec.className = 'panel';
    sec.innerHTML = '<div class="panel-head"><h3>缺失文件</h3></div>';
    const list = document.createElement('div');
    list.className = 'missing-list';
    list.style.padding = '12px';
    for (const m of missing) {
      const item = document.createElement('div');
      item.className = 'missing-item';
      item.innerHTML = `<div class="u">${esc(m.url)}</div><div class="p">期望输出: ${esc(m.expected)}.md / .json</div>`;
      list.append(item);
    }
    sec.append(list);
    view.append(sec);
  }

  const meta = document.createElement('div');
  meta.className = 'panel';
  meta.style.marginTop = '18px';
  const m = report.manifest || {};
  meta.innerHTML = `
    <div class="panel-head"><h3>站点信息</h3></div>
    <div class="panel-body" style="font-size:12.5px;color:var(--muted)">
      抓取时间: ${fmtTime(m.finished_at)} · 来源: ${esc(m.source || '—')} ·
      manifest 页数: ${m.pages} · 失败: ${m.failed}
    </div>`;
  view.append(meta);

  $('#btn-reverify').addEventListener('click', () => renderVerify(name));
}

/* ---------------- 抓取表单 ---------------- */
$('#btn-new').addEventListener('click', () => {
  $('#drawer').hidden = false;
  $('#f-url').focus();
});
$$('[data-close-drawer]').forEach(el =>
  el.addEventListener('click', () => { $('#drawer').hidden = true; }));

$('#fetch-form').addEventListener('submit', async e => {
  e.preventDefault();
  const url = $('#f-url').value.trim();
  const formats = $$('#f-formats input:checked').map(i => i.value);
  if (!url) return toast('请输入站点 URL', 'error');
  if (!formats.length) return toast('请至少选择一种输出格式', 'error');
  const btn = $('#f-submit');
  btn.disabled = true;
  try {
    const res = await api('/api/fetch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url,
        formats,
        limit: Number($('#f-limit').value) || 0,
        combined: $('#f-combined').checked,
        fresh: $('#f-fresh').checked,
      }),
    });
    toast(res.message || '已开始抓取', 'success');
    $('#drawer').hidden = true;
    $('#f-url').value = '';
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    btn.disabled = false;
  }
});

/* ---------------- 列表自动刷新 ---------------- */
setInterval(async () => {
  if (parseRoute().view !== 'list' || fetchState.running) return;
  try {
    const data = await api('/api/sites');
    const old = JSON.stringify($$('.card').map(c => ({
      n: c.querySelector('.card-name')?.textContent,
      p: c.querySelector('[data-count]')?.dataset.count,
      t: c.querySelector('.card-meta span:last-child')?.textContent,
    })));
    const neu = JSON.stringify(data.sites.map(s => ({
      n: s.name, p: String(s.pages), t: fmtTime(s.updated),
    })));
    if (old !== neu) render();
  } catch (e) { /* 下次再试 */ }
}, 30000);

/* ---------------- 启动 ---------------- */
connectSSE();
render();
