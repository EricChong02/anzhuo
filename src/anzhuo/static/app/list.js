// 只给 GPT 做的对话在状态行前标个灰色小字；Claude 是默认的，不标，列表就干净
const engTag = e => e === 'codex' ? '<span class="eng" title="用 Codex 做的">Codex</span>'
  : e && e !== 'claude' ? `<span class="eng" title="用 ${esc(e)} 做的">${({kimi: 'Kimi', deepseek: 'DeepSeek'})[e] || esc(e)}</span>` : '';
// 总管（super_agent）：给总管的消息开头的标记；“期间的情况”拆成一张折叠卡，后面的话照常显示
const SA_TAGS = ['【回报】', '【退回】', '【要用户定】', '【重启】'];
function saSplit(it) {
  const P = '【期间的情况】';
  if (it.who !== 'you' || !(it.text || '').startsWith(P)) return [it];
  const cut = it.text.indexOf('\n\n');
  const notes = (cut < 0 ? it.text : it.text.slice(0, cut)).slice(P.length).trim();
  const rest = cut < 0 ? '' : it.text.slice(cut + 2);
  return [{who: 'note', text: `期间的情况 · ${notes.split('\n').length} 条`, sa: notes}].concat(rest ? [{...it, text: rest}] : []);
}
let taskGrouping = false, taskGroupAttempt = 0;
function groupPreferences() {
  try { return JSON.parse(localStorage.getItem('taskGroupOpen:' + (me.name || '')) || '{}'); } catch (_) { return {}; }
}
async function organizeTasks(force = false) {
  if (me.role !== 'admin' || taskGrouping || (!force && Date.now() - taskGroupAttempt < 300000)) return;
  taskGroupAttempt = Date.now(); taskGrouping = true; renderSide();
  try {
    const r = await api('task-groups', {force});
    if (r.ok && r.count) toast(`已用 ${r.model} 整理 ${r.count} 个对话`);
    else if (!r.ok && force) toast(r.error || '正在整理，请稍后查看');
  } catch (_) { if (force) toast('整理暂未完成，请稍后重试'); }
  finally { taskGrouping = false; await loadSessions(); }
}
// 侧栏只分三块：总管（固定在最上面）、现在（要你看 / 在做 / 刚做完的，按急缓排）、任务（其余按任务收起来）
// 平时的对话一行：名字左、时间右；有状态（在做、等你、出错、做完了）才多一行说明
const ROW_STATE = s => ['running', 'queued', 'awaiting', 'error', 'paused'].includes(s.status) ? statusText(s) : s.suggest ? '有建议' : s.unread ? '做完了' : '';
const NOW = s => ['awaiting', 'error', 'running', 'queued', 'paused'].includes(s.status) || s.unread || s.suggest;
const RANK = s => ['awaiting', 'error', 'running', 'queued', 'paused', 'idle'].indexOf(s.status);
function sideRow(s) {
  const state = ROW_STATE(s), boss = s.role === 'router';
  return `${boss ? '' : `<div class="swipe"><button class="swdel" data-d="${s.num}" tabindex="-1">删除</button>`}<button class="item ${s.status} ${s.suggest ? 'suggest' : ''} ${s.unread && s.num !== sel ? 'unread' : ''} ${s.num === sel ? 'sel' : ''} ${state ? '' : 'one'} ${boss ? 'boss' : ''}" data-n="${s.num}">
      ${boss ? `<span class="bossic">${ICON.boss}</span>` : ''}<span class="tx"><div class="nl"><span class="nm">${esc(s.name)}</span>${engTag(s.engine)}<span class="tm">${esc(ago(s.updated))}</span></div>${state ? `<div class="st"><span class="dot"></span><span class="sx">${esc(state)}</span></div>` : ''}</span><span class="more" data-m="${s.num}">⋯</span></button>${boss ? '' : '</div>'}`;
}
function renderSide() {
  XE.emit('side', {query});
  if (query) { $('#boss').innerHTML = ''; return renderSearch(); }
  const prefs = groupPreferences(), buckets = new Map(), routers = [], now = [];
  for (const s of sessions) {
    if (s.role === 'router') routers.push(s);
    else if (NOW(s)) now.push(s);
    else {
      const name = s.task_group || '待整理';
      if (!buckets.has(name)) buckets.set(name, []);
      buckets.get(name).push(s);
    }
  }
  now.sort((a, b) => RANK(a) - RANK(b) || b.updated - a.updated);
  const groups = [...buckets].sort((a, b) => Math.max(...b[1].map(s => s.updated)) - Math.max(...a[1].map(s => s.updated)));
  const group = ([name, xs]) => {
    const open = prefs[name] ?? xs.some(s => s.num === sel);
    xs.sort((a, b) => Number(b.pinned) - Number(a.pinned) || b.updated - a.updated);
    return `<details class="task-group" data-group="${esc(name)}" ${open ? 'open' : ''}><summary>${ICON.chev}<b>${esc(name)}</b><small>${xs.length}</small></summary><div class="task-rows">${xs.map(sideRow).join('')}</div></details>`;
  };
  $('#boss').innerHTML = routers.map(sideRow).join('');
  const html = (now.length ? `<div class="label">现在</div>${now.map(sideRow).join('')}` : '')
    + (groups.length ? `<div class="label">${taskGrouping ? '任务 · 正在整理…' : '任务'}</div>${groups.map(group).join('')}` : '');
  $('#list').innerHTML = html || (routers.length ? '' : `<div class="note" style="padding:10px">还没有对话</div>`);
  document.querySelectorAll('.task-group').forEach(el => el.ontoggle = () => {
    const prefs = groupPreferences(); prefs[el.dataset.group] = el.open;
    try { localStorage.setItem('taskGroupOpen:' + (me.name || ''), JSON.stringify(prefs)); } catch (_) {}
  });
  wireRows();
  renderSum();
}

// ---------- 列表：搜索、长按菜单（改名 / 删除） ----------
let query = '', searchSeq = 0;
// 手机上往左滑一条，后面露出“删除”，点一下就删（能撤销）；再点这条或滑别的就收回去
let swOpen = 0, swipedAt = 0;
const SW = 76;
function swSet(b, x, anim) {
  const tr = anim ? '.28s cubic-bezier(.2,.8,.2,1)' : '0s';
  b.style.transition = 'transform ' + tr; b.style.transform = x ? `translateX(${x}px)` : '';
  const d = b.parentElement.querySelector('.swdel');
  if (d) { d.style.transition = 'opacity ' + tr; d.style.opacity = Math.min(1, -x / SW); d.style.pointerEvents = x <= -SW / 2 ? 'auto' : 'none'; }
}
function swCloseAll(keep) {
  document.querySelectorAll('#list .swipe > .item').forEach(b => { if (b !== keep && b.style.transform) swSet(b, 0, true); });
  if (!keep) swOpen = 0;
}
async function delSession(num) {
  const s = sessions.find(x => x.num === num); if (!s) return;
  swOpen = 0;
  if (await manage(num, 'delete')) {
    speechPlayer.deleted(num);
    if (sel === num) goList();
    toast(`已删除“${s.name.slice(0, 10)}${s.name.length > 10 ? '…' : ''}”`, '撤销', () => restore(num));
  }
}
function wireRows() {
  document.querySelectorAll('#list .swdel').forEach(d => d.onclick = e => { e.stopPropagation(); delSession(+d.dataset.d); });
  document.querySelectorAll('#boss .item[data-n], #list .item[data-n]').forEach(b => {
    let timer = 0, longed = false, x0 = 0, y0 = 0, axis = '', base = 0, dx = 0;
    const sw = b.parentElement.classList.contains('swipe');
    if (sw && +b.dataset.n === swOpen) swSet(b, -SW, false);
    b.onclick = e => {
      if (longed) { longed = false; return; }
      if (Date.now() - swipedAt < 400) return;
      if (swOpen) { swCloseAll(); return; }
      const m = e.target.closest('.more'); if (m) { e.stopPropagation(); return rowMenu(+b.dataset.n); }
      if (b.dataset.del) return restore(+b.dataset.n, true);
      openSession(+b.dataset.n);
    };
    // 手机上长按 0.5 秒出菜单；手指挪动（在滚列表）就不算
    b.addEventListener('touchstart', e => { longed = false; axis = ''; dx = 0; x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
      base = sw && b.style.transform ? -SW : 0;
      timer = setTimeout(() => { longed = true; navigator.vibrate && navigator.vibrate(10); rowMenu(+b.dataset.n); }, 500); }, {passive: true});
    b.addEventListener('touchmove', e => {
      const mx = e.touches[0].clientX - x0, my = e.touches[0].clientY - y0;
      if (!axis && Math.hypot(mx, my) > 8) { clearTimeout(timer); axis = Math.abs(mx) > Math.abs(my) ? 'x' : 'y'; if (axis === 'x' && sw) swCloseAll(b); }
      if (axis !== 'x' || !sw) return;
      e.preventDefault(); dx = mx;
      let x = Math.min(0, base + mx); if (x < -SW) x = -SW + (x + SW) / 4;   // 拉过头只走一点点
      swSet(b, x, false);
    }, {passive: false});
    b.addEventListener('touchend', () => { clearTimeout(timer);
      if (axis !== 'x' || !sw) return;
      swipedAt = Date.now();
      const open = base + dx < -SW / 2; swSet(b, open ? -SW : 0, true); swOpen = open ? +b.dataset.n : 0;
    });
    b.oncontextmenu = e => { e.preventDefault(); rowMenu(+b.dataset.n); };
  });
}
async function manage(num, action, value) {
  const r = await api('manage/' + num, {action, value});
  if (!r.ok) { toast(r.error || '没改成，再试一次'); return false; }
  await loadSessions(); if (query) renderSearch(); return true;
}
function sheet(html) {
  const el = document.createElement('div'); el.className = 'amenu'; el.innerHTML = `<div class="box">${html}</div>`;
  el.onclick = e => { if (e.target === el) el.remove(); };
  document.body.appendChild(el); return el;
}
function scheduleDate(ts) {
  return ts ? new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Taipei', month:'numeric', day:'numeric', weekday:'short', hour:'2-digit', minute:'2-digit', hourCycle:'h23'}).format(new Date(ts * 1000)) : '—';
}
function scheduleLocal(ts) {
  return new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Taipei', year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23'}).format(new Date(ts * 1000)).replace(' ', 'T');
}
function scheduleModal(html) {
  const el = sheet(html); el.classList.add('schedule-dialog');
  const box = el.querySelector('.box'); box.setAttribute('role','dialog'); box.setAttribute('aria-modal','true'); box.setAttribute('aria-label','定时任务');
  el.addEventListener('keydown', e => { if (e.key === 'Escape') el.remove(); });
  return el;
}
async function openSchedules() {
  const el = scheduleModal(`<div class="schedule-head"><h2>定时任务</h2><button id="scheduleClose" aria-label="关闭"><svg viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button></div><div id="scheduleRows" class="schedule-rows">正在读取…</div><button class="on" id="scheduleNew">新建定时任务</button>`);
  el.querySelector('#scheduleClose').onclick = () => el.remove();
  el.querySelector('#scheduleNew').onclick = () => { el.remove(); scheduleForm(); };
  el.querySelector('#scheduleClose').focus();
  const states = {idle:'待执行',dispatching:'正在提交',submitted:'排队中',running:'执行中',completed:'已完成',failed:'执行失败',uncertain:'待确认'};
  let tasks = [];
  async function load() {
    try {
      const d = await api('schedules'); if (!el.isConnected) return;
      tasks = d.tasks || [];
      if (d.error) { el.querySelector('#scheduleRows').textContent = d.error; return; }
      el.querySelector('#scheduleRows').innerHTML = tasks.length ? tasks.map(t => `<article class="schedule-card"><div class="schedule-card-top"><h3>${esc(t.title)}</h3><span class="${['failed','uncertain'].includes(t.state) ? 'bad' : ''}">${['failed','uncertain'].includes(t.state) ? states[t.state] : !t.enabled && !['submitted','running','dispatching'].includes(t.state) ? (t.kind === 'once' && t.state === 'completed' ? '已完成' : '已暂停') : states[t.state] || t.state}</span></div>
        <div class="schedule-meta">${t.kind === 'once' ? '单次' : t.kind === 'daily' ? '每天' : '每周' + ['一','二','三','四','五','六','日'][t.weekday]} · ${t.kind === 'once' ? scheduleDate(Date.parse(t.run_at)/1000) : esc(t.time)} · ${esc((modelOpts.find(m => m[0] === t.model) || [, '继承 / 自动'])[1])}</div>
        ${t.enabled && t.next_at ? `<div class="schedule-meta">下次：${scheduleDate(t.next_at)}</div>` : ''}
        ${t.error ? `<div class="schedule-error">${esc(t.error)}</div>` : ''}
        <div class="schedule-tools">${t.target ? `<button data-open="${t.target}">查看对话</button>` : ''}<button data-edit="${t.id}">${t.kind === 'once' && !t.next_at ? '重新安排' : '修改'}</button>${t.kind === 'once' && !t.next_at ? '' : `<button data-action="${t.enabled ? 'pause' : 'resume'}" data-id="${t.id}">${t.enabled ? '暂停' : '恢复'}</button>`}<button class="bad" data-action="delete" data-id="${t.id}">删除</button></div></article>`).join('') : '<div class="schedule-empty">还没有定时任务</div>';
    } catch (e) { if (el.isConnected) el.querySelector('#scheduleRows').textContent = '暂时读不到任务，请稍后重试'; }
  }
  el.querySelector('#scheduleRows').onclick = async e => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.open) { el.remove(); openSession(+b.dataset.open); return; }
    if (b.dataset.edit) { const t = tasks.find(t => t.id === b.dataset.edit); el.remove(); scheduleForm(t); return; }
    if (b.dataset.action) {
      if (b.dataset.action === 'delete' && b.dataset.confirm !== 'yes') { b.dataset.confirm = 'yes'; b.textContent = '再次点击删除'; return; }
      b.disabled = true;
      const d = await api('schedules', {action:b.dataset.action,id:b.dataset.id}).catch(() => null);
      if (!d?.ok) toast(d?.error || '没改成功，请重试');
      await load();
    }
  };
  await load();
  const timer = setInterval(() => { if (!el.isConnected) clearInterval(timer); else load(); }, 5000);
}
async function scheduleForm(task = {}) {
  await loadSessions();
  const el = scheduleModal(`<div class="schedule-head"><h2>${task.id ? '修改定时任务' : '新建定时任务'}</h2><button id="scheduleBack" aria-label="返回"><svg viewBox="0 0 24 24"><path d="m15 5-7 7 7 7"/></svg></button></div>
    <form class="schedule-form"><label>名称<input name="title" maxlength="80" placeholder="例如：早间要闻" value="${esc(task.title || '')}"></label>
    <label>要做的事<textarea name="prompt" required maxlength="20000" rows="3" placeholder="写清希望远窗完成的任务">${esc(task.prompt || '')}</textarea></label>
    <div class="schedule-grid"><label>执行频率<select name="kind"><option value="once">单次</option><option value="daily">每天</option><option value="weekly">每周</option></select></label><label id="scheduleWeek">星期<select name="weekday">${['周一','周二','周三','周四','周五','周六','周日'].map((t,i)=>`<option value="${i}">${t}</option>`).join('')}</select></label></div>
    <label id="scheduleOnce">执行日期与时间<input type="datetime-local" name="run_at"></label><label id="scheduleClock">执行时刻<input type="time" name="time"></label>
    <div class="schedule-meta">按台北时间执行</div>
    <label>在哪个对话执行<select name="target"><option value="">新开专用对话</option>${sessions.map(s=>`<option value="${s.num}">#${s.num} ${esc(s.name)}</option>`).join('')}</select></label>
    <label id="scheduleFolder">工作文件夹<select name="folder"><option value="">独立临时文件夹</option>${folders.map(f=>`<option value="${esc(f)}">${esc(f)}</option>`).join('')}</select></label>
    <div class="schedule-grid"><label>模型<select name="model"><option value="auto">继承 / 自动</option>${modelOpts.map(([k,t])=>`<option value="${k}">${esc(t)}</option>`).join('')}</select></label><label>思考深度<select name="effort">${EFFORT_OPTS.map(([k,t])=>`<option value="${k}">${t}</option>`).join('')}</select></label></div>
    <div class="schedule-error" id="scheduleError" role="alert" hidden></div><button class="on" type="submit">保存任务</button></form>`);
  const form = el.querySelector('form'), field = k => form.elements.namedItem(k);
  field('kind').value = task.kind || 'once'; field('weekday').value = task.weekday ?? 0; field('time').value = task.time || '08:00';
  field('run_at').value = scheduleLocal(task.kind === 'once' && task.run_at ? Date.parse(task.run_at)/1000 : Date.now()/1000+3600);
  field('target').value = task.target || ''; field('folder').value = task.folder || ''; field('model').value = task.model || 'auto'; field('effort').value = task.effort || 'medium';
  const change = () => { el.querySelector('#scheduleOnce').hidden = field('kind').value !== 'once'; el.querySelector('#scheduleClock').hidden = field('kind').value === 'once'; el.querySelector('#scheduleWeek').hidden = field('kind').value !== 'weekly'; el.querySelector('#scheduleFolder').hidden = !!field('target').value; };
  field('kind').onchange = change; field('target').onchange = change; change();
  el.querySelector('#scheduleBack').onclick = () => { el.remove(); openSchedules(); };
  form.onsubmit = async e => {
    e.preventDefault(); const b = form.querySelector('button[type=submit]'); b.disabled = true;
    const data = Object.fromEntries(new FormData(form)); data.action = 'save'; if (task.id) data.id = task.id;
    const d = await api('schedules', data).catch(() => null);
    if (!d?.ok) { const err = el.querySelector('#scheduleError'); err.hidden = false; err.textContent = d?.error || '没保存成功，请重试'; b.disabled = false; return; }
    el.remove(); openSchedules();
  };
}

function rowMenu(num) {
  const s = sessions.find(x => x.num === num); if (!s) return;
  const el = sheet(`<div class="t">${esc(s.name)}</div>
    <button data-a="rename"><svg viewBox="0 0 24 24" style="width:18px;height:18px"><path d="M4 20h4L19 9l-4-4L4 16z"/></svg>改名</button>
    <button data-a="task_group">${ICON.list}移到任务分组</button>
    ${['awaiting', 'error'].includes(s.status) && !(s.detail || '').startsWith('等你确认') ? `<button data-a="dismiss"><svg viewBox="0 0 24 24" style="width:18px;height:18px"><path d="m6 6 12 12M18 6 6 18"/></svg>不用回答了（清掉这个状态）</button>` : ''}
    <div class="sep"></div><button class="del" data-a="del"><svg viewBox="0 0 24 24" style="width:18px;height:18px"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>删除</button>`);
  el.querySelectorAll('button').forEach(b => b.onclick = async () => {
    const a = b.dataset.a;
    if (a === 'rename') {
      el.querySelector('.box').innerHTML = `<div class="t">改名</div><input id="rn" value="${esc(s.name)}" maxlength="40" enterkeyhint="done"><button class="on" id="rok">好</button>`;
      const inp = el.querySelector('#rn'); inp.focus(); inp.select();
      const ok = async () => { const v = inp.value.trim(); if (!v) return; el.remove(); manage(num, 'rename', v); };
      el.querySelector('#rok').onclick = ok; inp.onkeydown = e => { if (e.key === 'Enter' && !e.isComposing) ok(); };
    } else if (a === 'task_group') {
      const names = [...new Set(sessions.map(x => x.task_group).filter(Boolean))];
      el.querySelector('.box').innerHTML = `<div class="t">移到任务分组</div><input id="tg" list="taskNames" value="${esc(s.task_group || '')}" maxlength="24" placeholder="选择已有任务，或输入新任务名"><datalist id="taskNames">${names.map(n => `<option value="${esc(n)}"></option>`).join('')}</datalist><button class="on" id="tgok">保存</button>`;
      const inp = el.querySelector('#tg'); inp.focus();
      const save = async () => { const value = inp.value.trim(); if (value.length < 2) return toast('任务名至少2个字'); if (await manage(num, 'task_group', value)) { el.remove(); toast('已归组，自动整理会保留你的选择'); } };
      el.querySelector('#tgok').onclick = save;
      inp.onkeydown = e => { if (e.key === 'Enter' && !e.isComposing) save(); };
    } else if (a === 'dismiss') {
      el.remove(); if (await manage(num, 'dismiss')) { if (sel === num) { lastKey = ''; loadThread(); } toast('已清掉这个状态'); }
    } else if (a === 'del') {
      el.remove();
      if (await manage(num, 'delete')) {
        speechPlayer.deleted(num);
        if (sel === num) goList();
        toast(`已删除“${s.name.slice(0, 10)}${s.name.length > 10 ? '…' : ''}”`, '撤销', () => restore(num));
      }
    }
  });
}
async function restore(num, open) {
  if (await manage(num, 'restore') && open) { $('#q').value = ''; setQuery(''); openSession(num); }
}
let toastEl = null, toastTimer = 0;
function toast(text, btn, fn, ms = 5000) {
  if (toastEl) toastEl.remove(); clearTimeout(toastTimer);
  toastEl = document.createElement('div'); toastEl.className = 'toast';
  toastEl.innerHTML = `<span>${esc(text)}</span>${btn ? `<button>${esc(btn)}</button>` : ''}`;
  if (btn) toastEl.querySelector('button').onclick = () => { toastEl.remove(); toastEl = null; fn(); };
  document.body.appendChild(toastEl);
  toastTimer = setTimeout(() => { if (toastEl) { toastEl.remove(); toastEl = null; } }, ms);
}
// 搜索：标题、你说过的话、它的回复；删除 30 天内的也搜得到（点一下找回）
function setQuery(v) {
  query = v.trim(); $('#find').classList.toggle('has', !!v); renderSide();
}
const hl = (t, q) => { const i = t.toLowerCase().indexOf(q.toLowerCase()); return i < 0 ? esc(t) : esc(t.slice(0, i)) + '<mark>' + esc(t.slice(i, i + q.length)) + '</mark>' + esc(t.slice(i + q.length)); };
async function renderSearch() {
  const q = query, seq = ++searchSeq;
  if (!$('#list').querySelector('.sres')) $('#list').innerHTML = '<div class="note sres" style="padding:10px">搜索中…</div>';
  let d; try { d = await api('search?q=' + encodeURIComponent(q)); } catch (e) { return; }
  if (seq !== searchSeq || q !== query) return;
  const row = (r, del) => `<button class="item ${del || r.snippet ? '' : 'one'}" data-n="${r.num}" ${del ? 'data-del="1"' : ''}><span class="tx"><div class="nl"><span class="nm">${hl(r.name, q)}</span>${engTag(r.engine)}<span class="tm">${del ? '' : esc(ago(r.updated))}</span></div>
    ${del ? `<div class="st">${r.hidden_at ? agoDays(r.hidden_at).replace('动过', '') + '删除 · 点一下找回' : '已删除 · 点一下找回'}</div>` : ''}
    ${r.snippet ? `<div class="hit">${r.who === 'you' ? '你' : '回复'}：${hl(r.snippet, q)}</div>` : ''}</span></button>`;
  const parts = [['标题里有', d.title, false], ['说过的话里有', d.said, false], ['已删除里也有', d.deleted, true]].filter(([, xs]) => xs.length);
  const extra = XE.collect('search', q), lib = extra.map(x => x.html || '').join('');
  $('#list').innerHTML = `<div class="sres"></div>` + (lib + parts.map(([t, xs, del]) => `<div class="label">${t}（${xs.length}）</div>` + xs.map(r => row(r, del)).join('')).join('')
    || `<div class="note" style="padding:10px">没找到“${esc(q)}”</div>`);
  wireRows();
  extra.forEach(x => x.wire && x.wire());
}
{ let t = 0; $('#q').oninput = e => { clearTimeout(t); const v = e.target.value; $('#find').classList.toggle('has', !!v); t = setTimeout(() => setQuery(v), 300); }; }
$('#q').onkeydown = e => { if (e.key === 'Escape') { e.target.value = ''; setQuery(''); } };
$('#qclr').onclick = e => { e.preventDefault(); $('#q').value = ''; setQuery(''); $('#q').blur(); };

// 标题下一行：问候 + 几件在做、几件等你，一眼看清现在的情况
const OWNER = '';   // 想在问候里带称呼就填上，比如 '张老师'
function renderSum() {
  const h = new Date().getHours(), hi = h < 5 ? '夜深了' : h < 11 ? '早上好' : h < 13 ? '中午好' : h < 18 ? '下午好' : '晚上好';
  const run = sessions.filter(s => ['running', 'queued', 'paused'].includes(s.status)).length, wait = sessions.filter(s => ['awaiting', 'error'].includes(s.status)).length;
  const bits = [esc(hi + '，' + OWNER)];
  if (run) bits.push(`<span class="r">${run} 件在做</span>`);
  if (wait) bits.push(`<span class="w">${wait} 件等你</span>`);
  if (!run && !wait) bits.push('都做完了');
  if ($('#sum')) $('#sum').innerHTML = bits.join(' · ');
}

async function loadSessions() {
  try { const d = await api('sessions'); if (d.me) { me = d.me; document.documentElement.classList.toggle('fenced', me.role === 'user'); } sessions = d.sessions; folders = d.folders; autoWhere = d.auto_where !== false; limits = d.limits || []; limitsGpt = d.limits_gpt || []; hasGpt = !!d.has_gpt; modelOpts = d.models || modelOpts; XE.emit('sessions', d); renderSide(); renderQuota(); headStatus(); watchStatus(); if (d.task_grouping && me.role === 'admin' && sessions.some(s => !s.task_group)) organizeTasks(); }
  catch (e) { if (e.message !== 'login') console.warn(e); }
}

