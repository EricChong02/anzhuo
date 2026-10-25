"""小E 网页对话框（单页）：左边对话列表，右边这个对话的一来一回；手机上一栏，先列表、点进去看对话。
每 3–4 秒拉一次接口刷新；发话、插话、停下、点选项都走 /_x/api/*。
样子按庄毅的界面口味：中性底 + 一个强调蓝、状态只用小圆点、不用左侧/顶部色条、不弹成功提示（进展写在按钮自己身上）、不放难看的小字。"""

APP_HTML = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="小E">
<meta name="theme-color" content="#fafaf9" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#161616" media="(prefers-color-scheme: dark)">
<title>小E</title>
<link rel="apple-touch-icon" href="/_x/icon.png"><link rel="manifest" href="/_x/manifest.json">
<style>
:root{--bg:#fafaf9;--side:#f2f2ef;--panel:#fff;--bubble:#ececE8;--ink:#1c1c1e;--mute:#6b6b70;--line:rgba(0,0,0,.08);
  --acc:#2f6fdf;--accsoft:rgba(47,111,223,.10);--amber:#b45309;--red:#c0392b;--glass:rgba(255,255,255,.78);--shadow:0 6px 24px rgba(0,0,0,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--side:#1b1b1b;--panel:#222;--bubble:#2b2b2b;--ink:#ececec;--mute:#9b9b9b;--line:rgba(255,255,255,.09);
  --acc:#7aa7ff;--accsoft:rgba(122,167,255,.14);--amber:#f0a35e;--red:#f08070;--glass:rgba(34,34,34,.78);--shadow:0 6px 24px rgba(0,0,0,.3)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;overflow:hidden;overscroll-behavior:none;background:var(--bg);color:var(--ink);font:16px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;-webkit-font-smoothing:antialiased}
button{font:inherit;color:inherit;cursor:pointer;background:none;border:0;padding:0}
a{color:var(--acc);text-decoration:none}
svg{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none}
.app{display:grid;grid-template-columns:296px 1fr;height:var(--vh,100dvh)}

/* 侧栏 */
.side{background:var(--side);display:flex;flex-direction:column;min-height:0}
.brand{display:flex;align-items:center;padding:18px 14px 10px 20px}
.brand b{font-size:20px;flex:1;letter-spacing:.5px}
.icon-btn{width:38px;height:38px;border-radius:10px;display:grid;place-items:center;color:var(--ink)}
.icon-btn:hover{background:var(--line)}
.scroll{overflow:auto;flex:1;padding:0 10px 20px}
.label{font-size:14px;color:var(--mute);padding:14px 10px 6px}
.item{display:flex;gap:10px;align-items:center;width:100%;text-align:left;border-radius:10px;padding:9px 10px;color:var(--ink)}
.item:hover{background:var(--line)} .item.sel{background:var(--accsoft)}
.item .tx{min-width:0;flex:1}
.item .nm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:15.5px}
.item.sel .nm{font-weight:600}
.item .st{font-size:14px;color:var(--mute);display:flex;align-items:center;gap:6px;margin-top:1px}
.dot{width:7px;height:7px;border-radius:50%;background:transparent;border:1.5px solid var(--mute);flex:none}
.running .dot{background:var(--acc);border-color:var(--acc);animation:pulse 1.6s ease-in-out infinite}
.awaiting .dot{background:var(--amber);border-color:var(--amber)} .awaiting .st{color:var(--amber)}
.error .dot{background:var(--red);border-color:var(--red)}
.idle .dot{border-color:var(--line);background:var(--line)}
.course svg{color:var(--acc)}
@keyframes pulse{50%{opacity:.35}}

/* 主区 */
.main{display:flex;flex-direction:column;min-height:0;min-width:0;position:relative}
.head{position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:8px;padding:10px 16px;min-height:58px;
  background:var(--glass);-webkit-backdrop-filter:saturate(1.4) blur(16px);backdrop-filter:saturate(1.4) blur(16px);border-bottom:1px solid var(--line)}
.head .ti{flex:1;min-width:0} .head .ti b{display:block;font-size:17px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.head .ti span{font-size:14px;color:var(--mute);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center;gap:6px}
.head .ti span.running{color:var(--acc)} .head .ti span.awaiting{color:var(--amber)} .head .ti span.error{color:var(--red)}
.pill{display:inline-flex;align-items:center;gap:6px;height:34px;padding:0 13px;border-radius:17px;border:1px solid var(--line);background:var(--panel);font-size:14.5px;color:var(--ink)}
.pill:hover{border-color:var(--acc)} .pill svg{width:16px;height:16px}
.back{display:none}
.feed{flex:1;overflow:auto;padding:24px 20px 140px;scroll-behavior:auto}
.col{max-width:760px;margin:0 auto;display:flex;flex-direction:column;gap:22px}
.you{align-self:flex-end;max-width:82%;background:var(--bubble);border-radius:20px;padding:10px 16px;white-space:pre-wrap;word-break:break-word}
.you.pending{opacity:.55}
.cl{line-height:1.75;word-break:break-word}
.cl .md{white-space:pre-wrap}
.cl .md table{border-collapse:collapse;margin:8px 0;white-space:normal;font-size:15px;display:block;overflow-x:auto}
.cl .md td,.cl .md th{border-bottom:1px solid var(--line);padding:6px 12px 6px 0;text-align:left;vertical-align:top}
.cl .md th{font-weight:600}
.cl .acts{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.note{align-self:center;font-size:14px;color:var(--mute)}
code{font:14px ui-monospace,Menlo,monospace;background:var(--line);padding:1px 5px;border-radius:5px}
.live{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:14px 16px;box-shadow:var(--shadow)}
.live .lh{display:flex;align-items:center;gap:10px}
.live .lh span{flex:1;font-weight:600}
.spin{width:16px;height:16px;border-radius:50%;border:2px solid var(--accsoft);border-top-color:var(--acc);animation:rot .9s linear infinite;flex:none}
@keyframes rot{to{transform:rotate(360deg)}}
.live ol{margin:10px 0 0;padding:0;list-style:none;display:flex;flex-direction:column;gap:4px}
.live li{color:var(--mute);font-size:15px;display:flex;gap:10px} .live li i{font-style:normal;min-width:22px;text-align:right}
.live li:last-child{color:var(--ink)}
.stop{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 12px;border-radius:16px;border:1px solid var(--line);font-size:14.5px}
.stop:hover{border-color:var(--red);color:var(--red)} .stop svg{width:14px;height:14px}
.ask{display:flex;flex-direction:column;gap:8px}
.ask .q{font-weight:600}
.opt{display:flex;align-items:center;gap:12px;text-align:left;width:100%;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 14px}
.opt:hover{border-color:var(--acc)} .opt.rec{border-color:var(--acc)}
.opt .k{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;background:var(--accsoft);color:var(--acc);font-weight:600;font-size:14px;flex:none}
.opt .v{flex:1} .opt .tag{font-size:14px;color:var(--acc)}
.opt:disabled{opacity:.5}

/* 输入条 */
.dock{position:absolute;left:0;right:0;bottom:0;padding:28px 16px calc(14px + env(safe-area-inset-bottom));pointer-events:none;transition:bottom .15s;
  background:linear-gradient(to bottom,transparent,var(--bg) 26px)}
.quick{pointer-events:auto;max-width:760px;margin:0 auto 8px;display:flex;gap:8px;overflow-x:auto;scrollbar-width:none}
.quick::-webkit-scrollbar{display:none}
.quick button{flex:none;height:36px;padding:0 16px;border-radius:18px;background:var(--panel);border:1px solid var(--line);font-size:15px}
.jump{pointer-events:auto;margin:0 auto 8px;height:36px;padding:0 14px;border-radius:18px;
  background:var(--panel);border:1px solid var(--line);box-shadow:var(--shadow);font-size:14.5px;display:none;align-items:center;gap:4px}
.jump.on{display:inline-flex} .jump svg{width:16px;height:16px}
.fold .md{max-height:15em;overflow:hidden;-webkit-mask-image:linear-gradient(#000 65%,transparent);mask-image:linear-gradient(#000 65%,transparent)}
.more{margin-top:6px;font-size:15px;color:var(--acc)}
.fab{display:none;position:fixed;right:20px;bottom:calc(22px + env(safe-area-inset-bottom));width:56px;height:56px;border-radius:50%;background:var(--acc);color:#fff;
  box-shadow:0 8px 24px rgba(47,111,223,.35);place-items:center;z-index:5} .fab svg{width:24px;height:24px}
.composer{pointer-events:auto;max-width:760px;margin:0 auto;background:var(--panel);
  border:1px solid var(--line);border-radius:24px;box-shadow:var(--shadow);padding:8px}
.composer .row{display:flex;align-items:flex-end;gap:6px}
.composer textarea{flex:1;border:0;outline:none;background:transparent;font:inherit;font-size:16px;color:var(--ink);resize:none;padding:8px 4px;max-height:38vh;min-height:40px;line-height:1.5}
.send{width:38px;height:38px;border-radius:50%;background:var(--acc);color:#fff;display:grid;place-items:center;flex:none}
.send:disabled{background:var(--line);color:var(--mute)}
.send .spin{border-color:rgba(255,255,255,.35);border-top-color:#fff}
.thumbs{display:flex;gap:8px;flex-wrap:wrap;padding:4px 4px 6px}
.thumb{position:relative} .thumb img{height:64px;border-radius:10px;display:block}
.thumb button{position:absolute;top:-6px;right:-6px;width:22px;height:22px;border-radius:50%;background:var(--ink);color:var(--bg);font-size:14px;line-height:22px}
.warn{color:var(--amber);font-size:14px;padding:2px 8px 6px}

/* 空状态、新对话 */
.blank{margin:auto;text-align:center;color:var(--mute);padding:24px}
.blank b{display:block;color:var(--ink);font-size:22px;margin-bottom:6px}
.newv{flex:1;display:flex;flex-direction:column;justify-content:center;padding:24px 16px 140px}
.newv h1{text-align:center;font-size:28px;font-weight:600;margin:0 0 18px}
.chips{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin:0 auto 14px;max-width:760px}
.chip{height:34px;padding:0 14px;border-radius:17px;border:1px solid var(--line);background:var(--panel);font-size:14.5px}
.chip.on{background:var(--accsoft);border-color:transparent;color:var(--acc);font-weight:600}
.newv .dock{position:static;padding:0}

@media (max-width:820px){
  .app{grid-template-columns:1fr} .side{background:var(--bg)}
  .app.in .side{display:none} .app:not(.in) .main{display:none} .back{display:grid}
  .app:not(.in) .fab{display:grid} #newBtn{display:none}
  .brand{padding:calc(14px + env(safe-area-inset-top)) 14px 6px 20px} .brand b{font-size:28px}
  .head{padding-top:calc(10px + env(safe-area-inset-top))}
  .scroll{padding:0 8px 110px} .item{min-height:60px;padding:10px 12px} .item .nm{font-size:16.5px} .item .st{font-size:14.5px}
  .label{font-size:14.5px;padding:18px 12px 6px}
  .feed{padding:18px 16px 150px} .you{max-width:86%} .col{gap:20px}
  .live ol li:nth-last-child(n+4){display:none}
  .dock{padding:24px 10px calc(10px + env(safe-area-inset-bottom))}
}
</style></head><body>
<div class="app" id="app">
  <aside class="side">
    <div class="brand"><b>小E</b><button class="icon-btn" id="newBtn" title="新对话"><svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg></button></div>
    <div class="scroll"><div id="courses"></div><div id="list"></div>
      <a class="item" href="/_x/history" style="margin-top:12px"><svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/></svg><span class="nm">历史记录</span></a>
    </div>
  </aside>
  <button class="fab" id="fab" title="新对话"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></button>
  <section class="main" id="main"><div class="blank"><b>小E</b>选一个对话，或开个新的</div></section>
</div>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ICON = {
  play: '<svg viewBox="0 0 24 24"><path d="M7 5v14l11-7z"/></svg>',
  list: '<svg viewBox="0 0 24 24"><path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01"/></svg>',
  img: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/></svg>',
  up: '<svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/></svg>',
  back: '<svg viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></svg>',
  down: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12l7 7 7-7"/></svg>',
};
let sessions = [], folders = [], drafts = [], sel = null, lastKey = '', attach = [], sending = false, pending = {}, folder = '', opened = new Set();

async function api(path, body) {
  const r = await fetch('/_x/api/' + path, body ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {});
  if (r.status === 401) { location.href = '/_x/login?next=' + encodeURIComponent('/_x/' + location.hash); throw new Error('login'); }
  return r.json();
}

function ago(t) {
  const s = Date.now() / 1000 - t, d = new Date(t * 1000);
  if (s < 60) return '刚刚';
  if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
  if (new Date().toDateString() === d.toDateString()) return d.toTimeString().slice(0, 5);
  if (s < 2 * 86400) return '昨天';
  return (d.getMonth() + 1) + '月' + d.getDate() + '日';
}
const statusText = s => ({running: `在做 · 第 ${s.steps} 步`, queued: '排队中', awaiting: '等你回答', error: '出错了'}[s.status] || ago(s.updated));

function md(src) {
  const lines = esc(src).split('\n'); const out = []; let tbl = [];
  const inline = t => t.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+|\/[^)\s]*)\)/g, '<a href="$2" target="_blank">$1</a>');
  const flush = () => {
    if (!tbl.length) return;
    const rows = tbl.filter(r => !/^\|\s*:?-{2,}/.test(r)).map(r => r.replace(/^\||\|$/g, '').split('|'));
    out.push('<table>' + rows.map((r, i) => '<tr>' + r.map(c => `<${i ? 'td' : 'th'}>${inline(c.trim())}</${i ? 'td' : 'th'}>`).join('') + '</tr>').join('') + '</table>');
    tbl = [];
  };
  for (const l of lines) {
    if (/^\s*\|.*\|\s*$/.test(l)) { tbl.push(l.trim()); continue; }
    flush();
    if (/^\s*-{3,}\s*$/.test(l)) continue;
    const h = l.match(/^#{1,4}\s+(.*)/);
    out.push(h ? `<b>${inline(h[1])}</b>` : inline(l.replace(/^(\s*)[-*] /, '$1• ')));
  }
  flush();
  return out.join('\n').replace(/\n(<table>)/g, '$1').replace(/(<\/table>)\n/g, '$1').replace(/\n{3,}/g, '\n\n').trim();
}

function renderSide() {
  $('#courses').innerHTML = drafts.length ? '<div class="label">在做的课</div>' + drafts.map(x =>
    `<a class="item course" href="${esc(x.url)}" target="_blank">${ICON.play}<span class="tx"><div class="nm">${esc(x.title)}</div></span></a>`).join('') : '';
  const row = s => `<button class="item ${s.status} ${s.num === sel ? 'sel' : ''}" data-n="${s.num}">
      <span class="tx"><div class="nm">${esc(s.name)}</div><div class="st"><span class="dot"></span>${esc(statusText(s))}</div></span></button>`;
  const groups = [['需要你', s => ['awaiting', 'error'].includes(s.status)], ['在做', s => ['running', 'queued'].includes(s.status)], ['其他', s => s.status === 'idle']]
    .map(([t, f]) => [t, sessions.filter(f)]).filter(([, xs]) => xs.length);
  $('#list').innerHTML = groups.map(([t, xs]) => (groups.length > 1 || t !== '其他' ? `<div class="label">${t}</div>` : '') + xs.map(row).join('')).join('')
    || '<div class="note" style="padding:10px">还没有对话</div>';
  document.querySelectorAll('#list .item').forEach(b => b.onclick = () => openSession(+b.dataset.n));
}

async function loadSessions() {
  try { const d = await api('sessions'); sessions = d.sessions; folders = d.folders; drafts = d.drafts; renderSide(); headStatus(); }
  catch (e) { if (e.message !== 'login') console.warn(e); }
}

function composerHTML(ph) {
  return `<div class="dock"><div style="text-align:center"><button class="jump" id="jump">${ICON.down}最新</button></div><div class="quick" id="quick" hidden></div><div class="composer"><div class="warn" id="warn" hidden></div><div class="thumbs" id="thumbs" hidden></div>
    <div class="row"><label class="icon-btn" title="图片">${ICON.img}<input type="file" accept="image/*" multiple id="file" hidden></label>
    <textarea id="ta" rows="1" placeholder="${esc(ph)}"></textarea>
    <button class="send" id="send" disabled title="发送">${ICON.up}</button></div></div></div>`;
}

function headStatus() {
  const s = sessions.find(x => x.num === sel), el = $('#hst');
  if (!s || !el) return;
  el.className = s.status; el.textContent = s.status === 'idle' ? s.cwd : statusText(s);
}

function openSession(num, fromPop) {
  sel = num; lastKey = ''; attach = [];
  if (!fromPop) history.pushState({s: num}, '', '#s' + num);
  $('#app').classList.add('in');
  const s = sessions.find(x => x.num === num) || {name: '#' + num, cwd: ''};
  $('#main').innerHTML = `<div class="head"><button class="icon-btn back" id="back">${ICON.back}</button>
      <div class="ti"><b>${esc(s.name)}</b><span id="hst">${esc(s.cwd)}</span></div><span id="headActs"></span></div>
    <div class="feed" id="feed"><div class="col" id="col"><div class="note">读取中…</div></div></div>
    ${composerHTML('说点什么…')}`;
  $('#back').onclick = () => history.state && history.state.s ? history.back() : goList();
  const feed = $('#feed');
  feed.onscroll = () => $('#jump').classList.toggle('on', feed.scrollHeight - feed.scrollTop - feed.clientHeight > 500);
  $('#jump').onclick = () => feed.scrollTo({top: feed.scrollHeight, behavior: 'smooth'});
  wireComposer(text => api('send', {num, text, images: attach}), text => { (pending[num] ||= []).push({text, t: Date.now()}); });
  renderSide(); headStatus(); loadThread();
}

function goList() { sel = null; if (location.hash) history.replaceState(null, '', location.pathname); $('#app').classList.remove('in'); renderSide(); }
addEventListener('popstate', e => { const n = e.state && e.state.s; if (n) openSession(n, true); else if (e.state && e.state.n) openNew(true); else goList(); });

function renderThread(d) {
  const feed = $('#feed'); if (!feed) return;
  const near = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 160;
  const said = new Set(d.items.filter(i => i.who === 'you').map(i => i.text.trim()));
  pending[d.num] = (pending[d.num] || []).filter(p => !said.has(p.text) && Date.now() - p.t < 120000);
  const replies = d.items.filter(i => i.who === 'claude');
  const keep = new Set(replies.slice(-2));
  let h = d.items.map((it, idx) => {
    if (it.who === 'you') return `<div class="you">${esc(it.text)}</div>`;
    if (it.who === 'note') return `<div class="note">${esc(it.text)}</div>`;
    const acts = [it.tid ? `<a class="pill" href="/_x/t/${it.tid}" target="_blank">${ICON.list}过程</a>` : '',
      ...(it.previews || []).map(([n, u]) => `<a class="pill" href="${esc(u)}" target="_blank">${ICON.play}试课</a>`)].join('');
    const id = d.num + ':' + idx, fold = !keep.has(it) && it.text.length > 700 && !opened.has(id);
    return `<div class="cl ${fold ? 'fold' : ''}"><div class="md">${md(it.text)}</div>${fold ? `<button class="more" data-id="${id}">展开全部</button>` : ''}${acts ? `<div class="acts">${acts}</div>` : ''}</div>`;
  }).join('');
  h += pending[d.num].map(p => `<div class="you pending">${esc(p.text)}</div>`).join('');
  if (!d.items.length && !d.live && !pending[d.num].length) h = '<div class="note">这个对话还没有内容</div>';
  if (d.live) {
    const L = d.live, first = L.total - L.steps.length + 1;
    h += `<div class="live"><div class="lh"><i class="spin"></i><span>在做 · ${L.minutes} 分钟${L.injected ? ` · 插话 ${L.injected} 次` : ''}</span>
      <button class="stop" id="stop">${ICON.stop}停止</button></div>
      ${L.steps.length ? `<ol>${L.steps.map((x, i) => `<li><i>${first + i}</i>${esc(x)}</li>`).join('')}</ol>` : ''}</div>`;
  }
  if (d.decision) h += `<div class="ask"><div class="q">${esc(d.decision.question)}</div>${d.decision.options.map(([k, v]) =>
      `<button class="opt ${k === d.decision.recommend ? 'rec' : ''}" data-k="${k}" data-v="${esc(v)}"><span class="k">${k}</span><span class="v">${esc(v)}</span>${k === d.decision.recommend ? '<span class="tag">推荐</span>' : ''}</button>`).join('')}</div>`;
  $('#col').innerHTML = h;
  document.querySelectorAll('.more').forEach(b => b.onclick = () => { opened.add(b.dataset.id); lastKey = ''; renderThread(d); });
  const q = $('#quick');
  const quick = d.live ? [] : d.decision ? [] : d.awaiting ? ['好的', '就按你推荐的', '先不做'] : [];
  q.hidden = !quick.length;
  q.innerHTML = quick.map(t => `<button>${t}</button>`).join('');
  q.querySelectorAll('button').forEach(b => b.onclick = async () => {
    q.hidden = true; (pending[d.num] ||= []).push({text: b.textContent, t: Date.now()}); lastKey = ''; renderThread(d);
    await api('send', {num: d.num, text: b.textContent}); loadThread(); loadSessions();
  });
  const last = [...d.items].reverse().find(it => it.who === 'claude' && (it.previews || []).length);
  $('#headActs').innerHTML = last ? `<a class="pill" href="${esc(last.previews[0][1])}" target="_blank">${ICON.play}试课</a>` : '';
  $('#ta').placeholder = d.live ? '插话：它手上这一步做完就会看到' : d.awaiting ? '回答它的问题…' : '说点什么…';
  if ($('#stop')) $('#stop').onclick = async e => { e.currentTarget.innerHTML = '<i class="spin"></i>停止中'; e.currentTarget.disabled = true; await api('stop/' + d.num, {}); loadThread(); };
  document.querySelectorAll('.opt').forEach(b => b.onclick = async () => {
    document.querySelectorAll('.opt').forEach(x => x.disabled = true);
    b.querySelector('.k').innerHTML = '<i class="spin"></i>';
    const text = `选 ${b.dataset.k}：${b.dataset.v}`;
    (pending[d.num] ||= []).push({text, t: Date.now()});
    await api('send', {num: d.num, text}); lastKey = ''; loadThread(); loadSessions();
  });
  if (near) feed.scrollTop = feed.scrollHeight;
}

async function loadThread() {
  if (!sel) return;
  try {
    const d = await api('thread/' + sel);
    if (d.num !== sel) return;
    const key = JSON.stringify(d) + JSON.stringify(pending[d.num] || []);
    if (key !== lastKey) { lastKey = key; renderThread(d); }
  } catch (e) { if (e.message !== 'login') console.warn(e); }
}

function shrink(file) {
  return new Promise(res => {
    const img = new Image(), url = URL.createObjectURL(file);
    img.onload = () => {
      const k = Math.min(1, 1600 / Math.max(img.width, img.height));
      const c = document.createElement('canvas'); c.width = img.width * k; c.height = img.height * k;
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height); URL.revokeObjectURL(url);
      res(c.toDataURL('image/jpeg', .86));
    };
    img.src = url;
  });
}

function wireComposer(sendFn, onSent) {
  const ta = $('#ta'), btn = $('#send');
  const sync = () => {
    ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, innerHeight * .38) + 'px';
    btn.disabled = sending || (!ta.value.trim() && !attach.length);
    $('#thumbs').hidden = !attach.length;
    $('#thumbs').innerHTML = attach.map((u, i) => `<span class="thumb"><img src="${u}"><button data-i="${i}">×</button></span>`).join('');
    document.querySelectorAll('.thumb button').forEach(b => b.onclick = () => { attach.splice(+b.dataset.i, 1); sync(); });
  };
  const warn = t => { $('#warn').hidden = !t; $('#warn').textContent = t || ''; };
  const add = async files => { for (const f of files) if (f.type.startsWith('image/')) attach.push(await shrink(f)); sync(); };
  ta.oninput = sync;
  $('#file').onchange = e => add(e.target.files);
  ta.onpaste = e => { const fs = [...e.clipboardData.items].filter(i => i.type.startsWith('image/')).map(i => i.getAsFile()); if (fs.length) add(fs); };
  // 回车发送（iPad 连不连键盘都算、电脑也是），Shift+回车换行；打中文选字时的回车不算。只有手机上回车是换行。
  const phone = matchMedia('(pointer:coarse) and (max-width:600px)').matches;
  ta.setAttribute('enterkeyhint', phone ? 'enter' : 'send');
  ta.onkeydown = e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229 && !phone) { e.preventDefault(); go(); }
  };
  async function go() {
    const text = ta.value.trim();
    if ((!text && !attach.length) || sending) return;
    sending = true; warn(''); btn.disabled = true; btn.innerHTML = '<i class="spin"></i>';
    try {
      const r = await sendFn(text);
      if (r.ok) {
        if (onSent) onSent(text || '[图片]');
        ta.value = ''; attach = [];
        if (r.num) { sending = false; await loadSessions(); openSession(r.num); return; }
      } else warn(r.error || '没发出去，再试一次');
    } catch (e) { if (e.message !== 'login') warn('没发出去，再试一次'); }
    sending = false; btn.innerHTML = ICON.up; sync(); lastKey = ''; loadThread(); loadSessions();
  }
  btn.onclick = go; sync();
}

function openNew(fromPop) {
  sel = null; if (!fromPop) history.pushState({n: 1}, '', '#new'); $('#app').classList.add('in'); renderSide(); folder = '';
  $('#main').innerHTML = `<div class="head"><button class="icon-btn back" id="back">${ICON.back}</button><div class="ti"><b>新对话</b></div></div>
    <div class="newv"><h1>要做什么？</h1><div class="chips" id="chips"></div>${composerHTML('比如：把第二课封面标题改大一点')}</div>`;
  $('#back').onclick = () => history.state && history.state.n ? history.back() : goList();
  const chips = () => { $('#chips').innerHTML = [['', '临时文件夹'], ...folders.map(f => [f, f])].map(([v, t]) =>
      `<button class="chip ${v === folder ? 'on' : ''}" data-v="${esc(v)}">${esc(t)}</button>`).join('');
    document.querySelectorAll('.chip').forEach(c => c.onclick = () => { folder = c.dataset.v; chips(); }); };
  chips();
  wireComposer(text => api('send', {text, folder, images: attach}));
  $('#ta').focus();
}
$('#newBtn').onclick = () => openNew();
$('#fab').onclick = () => openNew();

// 屏幕键盘弹出/收起：整页不滚，只让页面高度跟着“真正看得见的区域”走，输入条自然贴在键盘上面；
// 收起后 Safari 为露出输入框而滚走的那段也拉回来（不然会卡在半截、下面一片空白）
if (window.visualViewport) {
  const vv = visualViewport;
  const fit = () => {
    document.documentElement.style.setProperty('--vh', Math.round(vv.height) + 'px');
    if (scrollX || scrollY) scrollTo(0, 0);
  };
  vv.addEventListener('resize', fit); vv.addEventListener('scroll', fit); fit();
  addEventListener('focusout', () => setTimeout(fit, 60));
}

(async () => {
  await loadSessions();
  const m = location.hash.match(/^#s(\d+)/);
  if (m) { history.replaceState(null, '', location.pathname); openSession(+m[1]); } else if (location.hash === '#new') { history.replaceState(null, '', location.pathname); openNew(); }
  setInterval(() => { if (!document.hidden) loadSessions(); }, 4000);
  setInterval(() => { if (!document.hidden) loadThread(); }, 3000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { loadSessions(); loadThread(); } });
})();
</script></body></html>"""
