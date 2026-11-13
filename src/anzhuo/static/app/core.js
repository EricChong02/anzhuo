// iPad（接了妙控键盘也算）：字号、按钮按 iPad 的手持距离放大一档（见样式里的 html.ipad），不再整页缩放
if (/iPad/.test(navigator.userAgent) || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1)) document.documentElement.classList.add('ipad');

const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ICON = {
  play: '<svg viewBox="0 0 24 24"><path d="M7 5v14l11-7z"/></svg>',
  list: '<svg viewBox="0 0 24 24"><path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01"/></svg>',
  img: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/></svg>',
  up: '<svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/></svg>',
  back: '<svg viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></svg>',
  close: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  down: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12l7 7 7-7"/></svg>',
  clip: '<svg viewBox="0 0 24 24"><path d="m21 11-8.5 8.5a5 5 0 0 1-7-7L14 4a3.3 3.3 0 0 1 4.7 4.7L10.2 17.2a1.7 1.7 0 0 1-2.4-2.4L15 7.6"/></svg>',
  doc: '<svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>',
  boss: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><circle cx="5" cy="5" r="1.8"/><circle cx="19" cy="5" r="1.8"/><circle cx="5" cy="19" r="1.8"/><circle cx="19" cy="19" r="1.8"/><path d="m6.5 6.5 3.4 3.4M17.5 6.5l-3.4 3.4M6.5 17.5l3.4-3.4M17.5 17.5l-3.4-3.4"/></svg>',
  chev: '<svg viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg>',
  bulb: '<svg viewBox="0 0 24 24"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z"/></svg>',
  step: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/></svg>',
  say: '<svg viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.4A8 8 0 1 1 21 12z"/></svg>',
  dots: '<svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/></svg>',
};
// 扩展挂接点：扩展的脚本排在这个脚本后面加载（只给管理员），用 XE.on 订阅界面上的时刻、XE.lib 用核心的小工具。
//   sessions(d)        刚拿到对话列表（d.ext.名字 是后端扩展附的数据），在画侧栏之前
//   side({query})      画侧栏：扩展往 #extSide 里自己的那块画（搜索时 query 不空）
//   search(q) → {html, wire}   搜索结果里加一段（排在对话结果前面）；wire() 在放进页面后调
//   thread(d)          对话打开 / 刷新完（d.ext.名字 是后端附的；#headActs 是标题栏右边留给扩展的位置）
//   link(x, it, d) → x 回复底下的链接按钮画之前改一改（比如换地址、换按钮上的字）
//   where(preset) → [{v, ic, t, send}]   新对话“在哪做”菜单临时加的项；选了它，发送时带上 send（不带 folder）
const XE = {
  _subs: {},
  on(name, fn) { (this._subs[name] ||= []).push(fn); },
  emit(name, ...a) { for (const fn of this._subs[name] || []) { try { fn(...a); } catch (e) { console.warn('扩展', name, e); } } },
  collect(name, ...a) { const out = []; for (const fn of this._subs[name] || []) { try { const r = fn(...a); if (r) out.push(...[].concat(r)); } catch (e) { console.warn('扩展', name, e); } } return out; },
  pipe(name, x, ...a) { for (const fn of this._subs[name] || []) { try { x = fn(x, ...a) || x; } catch (e) { console.warn('扩展', name, e); } } return x; },
  lib: {},   // 核心的小工具，脚本末尾填好（esc、api、sheet、openSession、openNew……）
};
let openProc = new Set(), anchor = null;   // 点开/收起时让点的那一行留在原位
// 工作过程：展开过的思考记在 unclamp 里（按内容认），重画时保持展开；还在翻译的显示“翻译中…”，译好一次出中文
const unclamp = new Set();
// 这件事拆成的几步：做完打勾、正在做的转圈，标题写到第几步
// 收起时只看正在做的和下一步，做完的并成一行（点一下看全部）
function planHTML(list, compact) {
  if (!list.length) return '';
  const done = list.filter(x => x.status === 'done').length;
  const row = (x, i) => `<div class="ps ${x.status}">${x.status === 'done' ? '<span class="ok">✓</span>' : x.status === 'doing' ? '<i class="spin"></i>' : `<span class="n">${i + 1}</span>`}<span class="an">${esc(x.text)}</span></div>`;
  let at = list.findIndex(x => x.status !== 'done'); if (at < 0) at = list.length;
  const shown = compact && list.length > 3 ? list.map((x, i) => [x, i]).filter(([, i]) => i >= at && i < at + 2) : list.map((x, i) => [x, i]);
  const rest = compact && list.length > 3 ? list.length - at - shown.length : 0;
  return `<div class="plan" data-p="plan"><div class="ah">共 ${list.length} 步 · 做完 ${done} 步</div>${shown.map(([x, i]) => row(x, i)).join('')}${rest > 0 ? `<div class="ps more"><span class="n">…</span><span class="an">还有 ${rest} 步</span></div>` : ''}</div>`;
}
// 派出去的助手（子代理、后台命令）：一行一个，在跑的转圈、做完打勾
function agentsHTML(list) {
  if (!list.length) return '';
  const run = list.filter(a => a.status === 'running').length;
  const mark = {running: '<i class="spin"></i>', done: '<span class="ok">✓</span>', failed: '<span class="bad">✕</span>', stopped: '<span class="bad">■</span>'};
  if (!run) return '';   // 都做完了就不占地方
  list = list.filter(a => a.status === 'running').slice(-3);
  return `<div class="agents"><div class="ah">${run} 个助手在跑</div>${list.map(a =>
    `<div class="ag ${a.status}">${mark[a.status] || ''}<span class="an">${esc(a.name)}</span><span class="am">${a.status === 'running' && a.last ? esc(a.last) + ' · ' : ''}${dur(a.secs)}</span></div>`).join('')}</div>`;
}
function procHTML(list, pid, tid) {
  const ck = t => esc(String(t).slice(0, 80));
  return `<div class="plist" data-pid="${esc(pid)}">${list.map(e => e.wait
    ? `<div class="pi ${e.k}">${e.k === 'think' ? ICON.bulb : ICON.say}<span class="pt wait">翻译中…</span></div>`
    : e.k === 'think' ? `<div class="pi think">${ICON.bulb}<span class="pt ${unclamp.has(ck(e.text)) ? '' : 'clamp'}" data-c="${ck(e.text)}">${esc(e.text)}</span></div>`
    : e.k === 'say' ? `<div class="pi say">${ICON.say}<div class="pt md">${md(e.text)}</div></div>`
    : `<div class="pi do${e.sub ? ' sub' : ''}">${ICON.step}<span class="pt">${e.sub ? `<em class="who">${esc(e.sub)}</em>` : ''}<b>${esc(e.text)}</b>${e.detail && e.detail !== e.text ? `<code class="pd ${unclamp.has(ck(e.detail)) ? '' : 'clamp'}" data-c="${ck(e.detail)}">${esc(e.detail)}</code>` : ''}</span></div>`).join('')}${tid ? `<a class="pfull" href="/_x/t/${tid}" target="_blank">完整过程</a>` : ''}${pid === 'live' ? '<button class="pjump" type="button">↓ 回到底部</button>' : ''}</div>`;
}
let limitsGpt = [], hasGpt = false;
let limits = [], usageOpen = false, prevStatus = null, noticeTimer = null;

// 网页里的提醒：某个对话做完、等你回答或出错时，顶上轻轻弹一条（点一下进去）；标签页标题写着几件要你看
function notice(s) {
  const el = $('#notice'), word = {awaiting: (s.detail || '').startsWith('等你确认') ? '等你确认' : '等你定', error: '出错了', paused: '额度用完了', idle: s.suggest ? '做完了 · 有建议' : '做完了'}[s.status] || '有新进展';
  el.className = 'notice ' + s.status;
  el.innerHTML = `<span class="dot"></span><span>${esc(s.name)} · ${word}</span>`;
  el.onclick = () => { el.classList.remove('on'); openSession(s.num); };
  requestAnimationFrame(() => el.classList.add('on'));
  clearTimeout(noticeTimer); noticeTimer = setTimeout(() => el.classList.remove('on'), 5000);
}
function watchStatus() {
  const now = Object.fromEntries(sessions.map(s => [s.num, s.status]));
  if (prevStatus) for (const s of sessions) {
    const was = prevStatus[s.num];
    if ((was === 'running' || was === 'queued') && !['running', 'queued'].includes(s.status)) notice(s);
  }
  prevStatus = now;
  const n = sessions.filter(s => s.status === 'awaiting' || s.status === 'error' || (s.unread && s.num !== sel)).length;
  if (navigator.setAppBadge) (n ? navigator.setAppBadge(n) : navigator.clearAppBadge()).catch(() => {});   // 主屏幕图标上的小红数字
  document.title = n ? `(${n}) 远窗` : '远窗';
}
const size = n => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
let me = {name: '', role: 'admin', root: ''};
let stick = false, sessions = [], folders = [], autoWhere = true, sel = null, lastKey = '', pending = {}, opened = new Set(), lastSaid = {}, lastYou = {};
const coarsePhone = () => matchMedia('(pointer:coarse) and (max-width:600px)').matches || (matchMedia('(pointer:coarse) and (max-height:500px)').matches && !document.documentElement.classList.contains('ipad'));
const saidBase = (num, text) => lastSaid[num] ? lastSaid[num](text.trim()) : 0;   // 发出时记录里已有几条同样的话

// Mac 连不上（睡着、断网、远窗在更新）时外网服务器回 502/504 或干脆不通：顶上挂一条提示，连上了自己消失
function setConn(st) {
  const c = $('#conn'); if (!c) return;
  c.className = 'conn ' + st; c.querySelector('.cs').textContent = {on: '已连接', off: '连不上', wait: '连接中…'}[st];
}
function setOffline(off) {
  setConn(off ? 'off' : 'on');
  let el = $('#offline');
  if (!off) { if (el) el.remove(); return; }
  if (el) return;
  el = document.createElement('div'); el.id = 'offline'; el.className = 'offline';
  el.innerHTML = '<i></i><span></span><button type="button">重连</button>';
  el.querySelector('span').textContent = navigator.onLine === false ? '设备已离线，联网后自动恢复' : '暂时连不上 Mac，正在自动重连';
  el.querySelector('button').onclick = () => resumeConnection();
  document.body.appendChild(el);
}
async function api(path, body) {
  let r;
  try { r = await fetch('/_x/api/' + path, body ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {}); }
  catch (e) { setOffline(true); throw e; }
  if (r.status === 401) { clearThreads(); location.href = '/_x/login?next=' + encodeURIComponent('/_x/' + location.hash); throw new Error('login'); }
  if (r.status === 502 || r.status === 503 || r.status === 504) { setOffline(true); throw new Error('offline'); }
  if (r.status === 413) throw new Error('big');
  setOffline(false);
  return r.json();
}

// 带附件的发送：fetch 看不到上传进度，改用 XHR，边传边报“已传多少”
function upload(path, body, onProg) {
  return new Promise((ok, bad) => {
    const x = new XMLHttpRequest();
    x.open('POST', '/_x/api/' + path);
    const rawFiles = (body.files || []).some(f => f instanceof File);
    if (!rawFiles) x.setRequestHeader('Content-Type', 'application/json');
    x.upload.onprogress = e => { if (e.lengthComputable) onProg(e.loaded, e.total); };
    x.onerror = () => { setOffline(true); bad(new Error('上传连接中断，附件已保留，请保持页面打开后重试')); };
    x.onabort = () => bad(new Error('上传已中止，附件已保留'));
    x.ontimeout = () => bad(new Error('上传超时，附件已保留，请重试'));
    x.onload = () => {
      if (x.status === 401) { clearThreads(); location.href = '/_x/login?next=' + encodeURIComponent('/_x/' + location.hash); return bad(new Error('login')); }
      if ([502, 503, 504].includes(x.status)) { setOffline(true); return bad(new Error('offline')); }
      if (x.status === 413) return bad(new Error('big'));
      setOffline(false);
      if (x.status >= 400) return bad(new Error(`服务器接收失败（${x.status}），附件已保留`));
      try { ok(JSON.parse(x.responseText)); } catch (e) { bad(new Error('服务器返回异常，附件已保留，请重试')); }
    };
    if (rawFiles) {
      const form = new FormData();
      const {files, ...message} = body;
      form.append('message', JSON.stringify(message));
      for (const f of files) form.append('files', f, f.name);
      x.send(form);
    } else x.send(JSON.stringify(body));
  });
}
const sendApi = (body, onProg) => (body.files || []).length || (body.images || []).length ? upload('send', body, onProg) : api('send', body);

function ago(t) {
  const s = Date.now() / 1000 - t, d = new Date(t * 1000);
  if (s < 60) return '刚刚';
  if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
  if (new Date().toDateString() === d.toDateString()) return d.toTimeString().slice(0, 5);
  if (s < 2 * 86400) return '昨天';
  return (d.getMonth() + 1) + '月' + d.getDate() + '日';
}
const statusText = s => ({running: (s.detail || '').startsWith('正在压缩') ? '正在压缩上下文' : `在做 · 第 ${s.steps} 步`, queued: '排队中',
  awaiting: (s.detail || '').startsWith('等你确认') ? '等你确认' : '等你定', error: '出错了', paused: s.detail}[s.status]
  || (s.suggest ? '有建议 · ' + ago(s.updated) : s.unread ? '做完了 · ' + ago(s.updated) : ago(s.updated)));

function md(src) {
  // Codex 回复里的出处标记 :codex-file-citation{path="…"} 显示成“（出处：文件名）”
  src = String(src).replace(/:codex-file-citation\{[^}]*?path="([^"]+)"[^}]*\}/g, (m, p) => `（出处：${p.split('/').pop()}）`);
  const lines = esc(src).split('\n'); const out = [];
  // 很长的本机路径只显示文件名（按住或悬停看全路径），读着不被一长串目录打断
  const short = c => /^(~|\/(Users|private|tmp|var|Volumes|opt))\/[^\s]*\/[^\s]*\/[^\s]+$/.test(c) && c.length > 32
    ? `<code title="${c}">${c.split('/').filter(Boolean).pop()}</code>` : `<code>${c}</code>`;
  const inline = t => t.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`([^`\n]+)`/g, (m, c) => short(c))
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+|\/[^)\s]*)\)/g, (m, t, u) => /^\/(Users|private|tmp|var|Volumes|opt)\//.test(u)
      ? `<b>${t}</b>` : `<a href="${u}" target="_blank">${t}</a>`);  // Mac 上的本机路径在手机上点不开，只留文字
  // 按块排：段落、标题、列表、代码块、表格、引用；汇报里“要你做的：”这类开头单独成小标题
  let para = [], list = null, tbl = [], code = null, quote = [];
  const endPara = () => { if (para.length) out.push(`<p>${para.map(inline).join('<br>')}</p>`); para = []; };
  const endList = () => { if (list) out.push(`<${list.t}>${list.items.map(x => `<li>${inline(x)}</li>`).join('')}</${list.t}>`); list = null; };
  const endTbl = () => {
    if (!tbl.length) return;
    const rows = tbl.filter(r => !/^\|\s*:?-{2,}/.test(r)).map(r => r.replace(/^\||\|$/g, '').split('|'));
    out.push('<div class="tw"><table>' + rows.map((r, i) => '<tr>' + r.map(c => `<${i ? 'td' : 'th'}>${inline(c.trim())}</${i ? 'td' : 'th'}>`).join('') + '</tr>').join('') + '</table></div>');
    tbl = [];
  };
  const endQuote = () => { if (quote.length) out.push(`<blockquote>${quote.map(inline).join('<br>')}</blockquote>`); quote = []; };
  const endAll = () => { endPara(); endList(); endTbl(); endQuote(); };
  // 代码块超过 8 行先收起（第一行当标题），点开再看
  const pre = c => c.length > 8 ? `<details class="code"><summary><span>${c[0].trim().slice(0, 60) || '代码'}</span><small>${c.length} 行</small><button class="ib cpy" data-tip="复制" aria-label="复制"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M15 9V6.5A2.5 2.5 0 0 0 12.5 4h-6A2.5 2.5 0 0 0 4 6.5v6A2.5 2.5 0 0 0 6.5 15H9"/></svg></button></summary><pre>${c.join('\n')}</pre></details>` : `<div class="cw"><button class="ib cpy" data-tip="复制" aria-label="复制"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M15 9V6.5A2.5 2.5 0 0 0 12.5 4h-6A2.5 2.5 0 0 0 4 6.5v6A2.5 2.5 0 0 0 6.5 15H9"/></svg></button><pre>${c.join('\n')}</pre></div>`;
  for (const l of lines) {
    if (code) { if (/^\s*```/.test(l)) { out.push(pre(code)); code = null; } else code.push(l); continue; }
    if (/^\s*```/.test(l)) { endAll(); code = []; continue; }
    if (/^\s*\|.*\|\s*$/.test(l)) { endPara(); endList(); endQuote(); tbl.push(l.trim()); continue; }
    endTbl();
    if (!l.trim()) { endPara(); endQuote(); continue; }
    if (/^\s*-{3,}\s*$/.test(l)) { endAll(); out.push('<hr>'); continue; }
    const h = l.match(/^#{1,4}\s+(.*)/);
    if (h) { endAll(); out.push(`<h4>${inline(h[1])}</h4>`); continue; }
    const q = l.match(/^&gt;\s?(.*)/);
    if (q) { endPara(); endList(); quote.push(q[1]); continue; }
    const u = l.match(/^\s*[-*•]\s+(.*)/), o = l.match(/^\s*\d+[.、)]\s+(.*)/);
    if (u || o) {
      endPara(); endQuote(); const t = u ? 'ul' : 'ol';
      if (list && list.t !== t && !/^\s{2,}/.test(l)) endList();
      if (!list) list = {t, items: []};
      list.items.push((u || o)[1]); continue;
    }
    if (list && /^\s{2,}\S/.test(l)) { list.items[list.items.length - 1] += '<br>' + l.trim(); continue; }
    const sec = l.match(/^(要你做的|要你定|做了什么|改了哪些|自查结果|截图|结论|原因|下一步)[：:]\s*(.*)$/);
    if (sec) { endAll(); out.push(`<h5>${sec[1]}</h5>`); if (sec[2]) para.push(sec[2]); continue; }
    endList(); endQuote(); para.push(l);
  }
  if (code) out.push(pre(code));
  endAll();
  return out.join('');
}
