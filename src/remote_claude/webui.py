"""小E 网页对话框（单页）：左边对话列表，右边这个对话的一来一回；手机上一栏，先列表、点进去看对话。
每 3–4 秒拉一次接口刷新；发话、插话、停下、点选项都走 /_x/api/*。"""

APP_HTML = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="小E">
<title>小E</title>
<style>
:root{--bg:#f5f5f3;--panel:#fff;--ink:#1d1d1f;--mute:#6e6e73;--line:#e4e4e1;--acc:#2f6fdf;--accbg:#e8f0fd;--warn:#c2410c;--ok:#15803d;--err:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--panel:#1d1d1d;--ink:#ececec;--mute:#9a9a9a;--line:#2e2e2e;--acc:#7aa7ff;--accbg:#1f2a40;--warn:#fb923c;--ok:#4ade80;--err:#f87171}}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font:16px/1.6 -apple-system,"PingFang SC",sans-serif}
button{font:inherit;color:inherit;cursor:pointer}
a{color:var(--acc);text-decoration:none}
.app{display:grid;grid-template-columns:320px 1fr;height:100dvh}
.side{border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0;background:var(--panel)}
.side header{padding:14px 14px 8px;display:flex;align-items:center;gap:8px}
.side h1{font-size:19px;margin:0;flex:1}
.btn{border:1px solid var(--line);background:var(--panel);border-radius:9px;padding:6px 12px;font-size:15px}
.btn.pri{background:var(--acc);border-color:var(--acc);color:#fff}
.btn:disabled{opacity:.5}
.drafts{padding:0 14px 8px;display:flex;flex-wrap:wrap;gap:6px}
.chip{font-size:13px;border:1px solid var(--line);border-radius:999px;padding:3px 10px;color:var(--ink)}
.list{overflow:auto;flex:1;padding:4px 8px 16px}
.row{display:block;width:100%;text-align:left;border:0;background:none;border-radius:10px;padding:10px 10px;margin:2px 0}
.row:hover{background:rgba(127,127,127,.08)} .row.sel{background:var(--accbg)}
.row b{font-weight:600} .row .d{font-size:13px;color:var(--mute);display:flex;gap:6px;align-items:center;margin-top:2px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--mute);flex:none}
.s-running .dot{background:var(--acc)} .s-awaiting .dot{background:var(--warn)} .s-error .dot{background:var(--err)} .s-queued .dot{background:var(--mute)}
.s-awaiting .d{color:var(--warn)}
.main{display:flex;flex-direction:column;min-height:0;min-width:0}
.top{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:10px;background:var(--panel)}
.top .t{flex:1;min-width:0} .top .t b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis} .top .t span{font-size:13px;color:var(--mute)}
.back{display:none}
.feed{flex:1;overflow:auto;padding:16px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:780px;width:100%;margin:0 auto}
.you{display:flex;justify-content:flex-end} .you div{background:var(--accbg);border-radius:14px 14px 4px 14px;padding:8px 12px;max-width:85%;white-space:pre-wrap;word-break:break-word}
.cl{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:10px 14px;word-break:break-word}
.cl .md{white-space:pre-wrap} .cl .md table{border-collapse:collapse;margin:6px 0;white-space:normal;font-size:14px;display:block;overflow-x:auto}
.cl .md td,.cl .md th{border:1px solid var(--line);padding:4px 8px;text-align:left}
.cl .ft{font-size:13px;color:var(--mute);margin-top:6px;display:flex;flex-wrap:wrap;gap:10px}
.note{text-align:center;font-size:13px;color:var(--mute)}
code{font:13px ui-monospace,Menlo,monospace;background:rgba(127,127,127,.12);padding:1px 4px;border-radius:4px}
.live{border:1px solid var(--acc);border-radius:14px;padding:10px 14px;background:var(--panel)}
.live .h{display:flex;align-items:center;gap:8px;font-weight:600} .live .h span{flex:1}
.live ol{margin:6px 0 0;padding-left:22px;color:var(--mute);font-size:14px} .live ol li:last-child{color:var(--ink)}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--acc);animation:p 1.4s infinite}
@keyframes p{50%{opacity:.25}}
.ask{border:1px solid var(--warn);border-radius:14px;padding:10px 14px;background:var(--panel)}
.ask .opts{display:flex;flex-direction:column;gap:6px;margin-top:8px}
.ask .opts button{text-align:left;border:1px solid var(--line);background:var(--bg);border-radius:10px;padding:8px 12px}
.ask .opts button.rec{border-color:var(--acc)}
.compose{border-top:1px solid var(--line);background:var(--panel);padding:10px 16px calc(10px + env(safe-area-inset-bottom))}
.compose .in{max-width:780px;margin:0 auto;display:flex;gap:8px;align-items:flex-end}
textarea{flex:1;font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:9px 12px;resize:none;max-height:40vh;min-height:44px}
.thumbs{max-width:780px;margin:0 auto 6px;display:flex;gap:6px;flex-wrap:wrap} .thumbs img{height:56px;border-radius:8px;border:1px solid var(--line)}
.empty{margin:auto;color:var(--mute);text-align:center;padding:24px}
.newbox{max-width:620px;margin:24px auto;width:100%;padding:0 16px;display:flex;flex-direction:column;gap:10px}
select{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:8px}
.toast{position:fixed;left:50%;bottom:90px;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 14px;border-radius:10px;font-size:14px;opacity:0;transition:opacity .2s;pointer-events:none}
.toast.on{opacity:.92}
@media (max-width:820px){
  .app{grid-template-columns:1fr} .side{border-right:0}
  .app.in-thread .side{display:none} .app:not(.in-thread) .main{display:none} .back{display:inline-block}
}
</style></head><body>
<div class="app" id="app">
  <aside class="side">
    <header><h1>小E</h1><a class="btn" href="/_x/history">历史</a><button class="btn pri" id="newBtn">＋ 新对话</button></header>
    <div class="drafts" id="drafts"></div>
    <div class="list" id="list"></div>
  </aside>
  <section class="main" id="main"><div class="empty">左边选一个对话，或者点“＋ 新对话”。</div></section>
</div>
<div class="toast" id="toast"></div>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let sessions = [], folders = [], sel = null, lastThread = '', attach = [], sending = false, threadData = null;

function toast(t) { const el = $('#toast'); el.textContent = t; el.classList.add('on'); setTimeout(() => el.classList.remove('on'), 2200); }

async function api(path, body) {
  const r = await fetch('/_x/api/' + path, body ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {});
  if (r.status === 401) { location.href = '/_x/login?next=' + encodeURIComponent('/_x/' + location.hash); throw new Error('login'); }
  return r.json();
}

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
    const h = l.match(/^#{1,4}\s+(.*)/);
    out.push(h ? `<b>${inline(h[1])}</b>` : inline(l));
  }
  flush();
  return out.join('\n').replace(/\n(<table>)/g, '$1').replace(/(<\/table>)\n/g, '$1');
}

function renderList() {
  $('#list').innerHTML = sessions.map(s => `<button class="row s-${s.status} ${s.num === sel ? 'sel' : ''}" data-n="${s.num}">
      <b>#${s.num} ${esc(s.name)}</b>
      <div class="d"><span class="dot"></span><span>${esc(s.detail)}</span></div>
      ${s.last_step ? `<div class="d">最新：${esc(s.last_step)}</div>` : ''}</button>`).join('') || '<div class="empty">还没有对话</div>';
  document.querySelectorAll('.row').forEach(b => b.onclick = () => openSession(+b.dataset.n));
}

async function loadSessions() {
  try {
    const d = await api('sessions');
    sessions = d.sessions; folders = d.folders;
    $('#drafts').innerHTML = d.drafts.map(x => `<a class="chip" href="${esc(x.url)}" target="_blank">试课：${esc(x.title)}</a>`).join('');
    renderList();
  } catch (e) { if (e.message !== 'login') console.warn(e); }
}

function openSession(num) {
  sel = num; lastThread = ''; attach = []; location.hash = 's' + num;
  $('#app').classList.add('in-thread');
  const s = sessions.find(x => x.num === num) || {name: '', cwd: ''};
  $('#main').innerHTML = `<div class="top"><button class="btn back" id="back">‹</button>
      <div class="t"><b>#${num} ${esc(s.name)}</b><span id="cwd">${esc(s.cwd)}</span></div><span id="tops"></span></div>
    <div class="feed" id="feed"><div class="empty">读取中…</div></div>
    <div class="compose"><div class="thumbs" id="thumbs"></div><div class="in">
      <label class="btn" title="图片">图片<input type="file" accept="image/*" multiple id="file" hidden></label>
      <textarea id="ta" rows="1" placeholder="跟 #${num} 说…"></textarea>
      <button class="btn pri" id="send">发送</button></div></div>`;
  $('#back').onclick = () => { sel = null; location.hash = ''; $('#app').classList.remove('in-thread'); renderList(); };
  wireCompose(text => api('send', {num, text, images: attach}));
  renderList(); loadThread();
}

function renderThread(d) {
  const feed = $('#feed'); if (!feed) return;
  const near = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
  let h = d.items.map(it => {
    if (it.who === 'you') return `<div class="msg you"><div>${esc(it.text)}</div></div>`;
    if (it.who === 'note') return `<div class="msg note">${esc(it.text)}</div>`;
    const links = [it.steps ? `${it.steps} 步` : '', it.tid ? `<a href="/_x/t/${it.tid}" target="_blank">完整过程</a>` : '',
      ...(it.previews || []).map(([n, u]) => `<a href="${esc(u)}" target="_blank">试课：${esc(n)}</a>`)].filter(Boolean).join('');
    return `<div class="msg cl"><div class="md">${md(it.text)}</div>${links ? `<div class="ft">${links}</div>` : ''}</div>`;
  }).join('');
  if (!d.items.length && !d.live) h = '<div class="empty">这个对话还没有内容。</div>';
  if (d.live) h += `<div class="msg live"><div class="h"><i class="pulse"></i><span>在做 · ${d.live.minutes} 分钟 · 第 ${d.live.total} 步${d.live.injected ? ` · 你插话 ${d.live.injected} 次` : ''}</span>
      <button class="btn" id="stop">停下</button></div>
      <div style="font-size:14px;margin-top:4px">${esc(d.live.request)}</div>
      ${d.live.steps.length ? `<ol start="${d.live.total - d.live.steps.length + 1}">${d.live.steps.map(x => `<li>${esc(x)}</li>`).join('')}</ol>` : ''}</div>`;
  if (d.decision) h += `<div class="msg ask"><b>${esc(d.decision.question)}</b><div class="opts">${d.decision.options.map(([k, v]) =>
      `<button class="${k === d.decision.recommend ? 'rec' : ''}" data-k="${k}" data-v="${esc(v)}"><b>${k}</b>　${esc(v)}${k === d.decision.recommend ? '（推荐）' : ''}</button>`).join('')}</div></div>`;
  else if (d.awaiting) h += `<div class="msg note">它在等你回答：${esc(d.awaiting)}</div>`;
  feed.innerHTML = h;
  const last = [...d.items].reverse().find(it => it.who === 'claude' && (it.previews || []).length);
  $('#tops').innerHTML = last ? last.previews.map(([n, u]) => `<a class="btn" href="${esc(u)}" target="_blank">试课</a>`).join('') : '';
  $('#ta').placeholder = d.live ? `插话给 #${d.num}：它手上这一步做完就会看到` : `跟 #${d.num} 说…`;
  if ($('#stop')) $('#stop').onclick = async () => { await api('stop/' + d.num, {}); toast('已让它停下'); loadThread(); };
  document.querySelectorAll('.ask .opts button').forEach(b => b.onclick = async () => {
    await api('send', {num: d.num, text: `选 ${b.dataset.k}：${b.dataset.v}`}); toast(`已选 ${b.dataset.k}`); loadThread(); loadSessions();
  });
  if (near) feed.scrollTop = feed.scrollHeight;
}

async function loadThread() {
  if (!sel) return;
  try {
    const d = await api('thread/' + sel);
    if (d.num !== sel) return;
    const key = JSON.stringify(d);
    if (key !== lastThread) { lastThread = key; threadData = d; renderThread(d); }
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

function wireCompose(sendFn) {
  const ta = $('#ta');
  const grow = () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, window.innerHeight * .4) + 'px'; };
  ta.oninput = grow;
  const add = async files => { for (const f of files) if (f.type.startsWith('image/')) attach.push(await shrink(f));
    $('#thumbs').innerHTML = attach.map(u => `<img src="${u}">`).join(''); };
  if ($('#file')) $('#file').onchange = e => add(e.target.files);
  ta.onpaste = e => { const fs = [...e.clipboardData.items].filter(i => i.type.startsWith('image/')).map(i => i.getAsFile()); if (fs.length) add(fs); };
  $('#send').onclick = async () => {
    const text = ta.value.trim();
    if ((!text && !attach.length) || sending) return;
    sending = true; $('#send').disabled = true;
    try {
      const r = await sendFn(text);
      if (r.ok) { ta.value = ''; attach = []; $('#thumbs').innerHTML = ''; grow(); toast(r.injected ? '已插话，它这一步做完就会看到' : '已发送');
        if (r.num) { await loadSessions(); openSession(r.num); } }
      else toast(r.error || '没发出去');
    } catch (e) { toast('没发出去，再试一次'); }
    sending = false; $('#send').disabled = false; loadThread(); loadSessions();
  };
}

$('#newBtn').onclick = () => {
  sel = null; location.hash = 'new'; $('#app').classList.add('in-thread'); renderList();
  $('#main').innerHTML = `<div class="top"><button class="btn back" id="back">‹</button><div class="t"><b>新对话</b><span>在哪个文件夹做、做什么</span></div></div>
    <div class="newbox"><select id="folder"><option value="">不指定文件夹（临时文件夹）</option>${folders.map(f => `<option>${esc(f)}</option>`).join('')}</select>
    <div class="thumbs" id="thumbs"></div><textarea id="ta" rows="5" placeholder="要做的事…"></textarea>
    <div style="display:flex;gap:8px"><label class="btn">图片<input type="file" accept="image/*" multiple id="file" hidden></label>
    <span style="flex:1"></span><button class="btn pri" id="send">开工</button></div></div>`;
  $('#back').onclick = () => { location.hash = ''; $('#app').classList.remove('in-thread'); };
  wireCompose(text => api('send', {text, folder: $('#folder').value, images: attach}));
};

(async () => {
  await loadSessions();
  const m = location.hash.match(/^#s(\d+)/);
  if (m) openSession(+m[1]);
  setInterval(() => { if (!document.hidden) loadSessions(); }, 4000);
  setInterval(() => { if (!document.hidden) loadThread(); }, 3000);
})();
</script></body></html>"""
