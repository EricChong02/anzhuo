const modelLimits = (ls, model) => ls.filter(l => l.name !== '本周 Fable' || model === 'fable' || /^claude-fable-/.test(model || ''));
// 额度的一格：有重置时间就显示；用量过线（默认八成）时变琥珀色
function quotaResetLabel(l, now = Date.now()) {
  if (l.name === '5 小时') return ((l.reset || '').match(/\b\d{1,2}:\d{2}\b/) || [''])[0];
  if (!['本周', '本周 Fable'].includes(l.name)) return l.reset || '';
  let d = l.reset_at == null ? null : new Date(Number(l.reset_at) * 1000);
  if (!d || !Number.isFinite(d.getTime())) {
    const m = (l.reset || '').match(/^(\d{1,2})月(\d{1,2})日 (\d{1,2}):(\d{2})$/);
    if (!m) return '';
    const year = new Date(now).getFullYear();
    d = [year - 1, year, year + 1].map(y => new Date(y, +m[1] - 1, +m[2], +m[3], +m[4]))
      .sort((a, b) => Math.abs(a.getTime() - now) - Math.abs(b.getTime() - now))[0];
  }
  const day = d.toDateString() === new Date(now).toDateString() ? '今天' : ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][d.getDay()];
  return `${day} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
// 按这速度何时用完（后端 quota.window 算好的 eta / level）：只在偏紧、告急且重置前会用完时才说一句，平时不说，免得每行都是预测
const quotaTight = l => l.level === '偏紧' || l.level === '告急';
function quotaPace(l, now = Date.now()) {
  if (l.eta == null || !quotaTight(l)) return null;
  const d = new Date(Number(l.eta) * 1000), tomorrow = new Date(now); tomorrow.setDate(tomorrow.getDate() + 1);
  const day = d.toDateString() === new Date(now).toDateString() ? '今天' : d.toDateString() === tomorrow.toDateString() ? '明天' : ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][d.getDay()];
  return {text: `按这速度${day} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')} 用完`, warn: true};
}
function qcell(l, hot = 80) {
  const reset = quotaResetLabel(l), pace = quotaPace(l);
  const h = l.pct != null && l.pct >= hot || pace?.warn;
  return `<div class="qm ${h ? 'hot' : ''}" title="${reset ? esc(reset) + ' 重置' : ''}"><div class="ql"><span>${esc(l.name)}</span><b>${l.pct == null ? '—' : l.pct + '%'}</b></div>
    <div class="qb"><i style="width:${Math.max(l.pct ? 2 : 0, Math.min(100, l.pct || 0))}%"></i></div>${reset ? `<div class="qr">${esc(reset)} 重置</div>` : ''}${pace?.warn ? `<div class="qr qp">${esc(pace.text)}</div>` : ''}</div>`;
}
function quotaGroups() {
  return [['Claude', limits], ...(limitsGpt.length && hasGpt ? [['Codex', limitsGpt]] : [])].filter(([, ls]) => ls.length);
}
function quotaReset(l, now = Date.now()) {
  const at = l.reset_at == null ? NaN : Number(l.reset_at) * 1000;
  if (!Number.isFinite(at)) return {relative: l.reset ? '重置时间' : '暂未提供重置时间', absolute: quotaResetLabel(l, now)};
  const mins = Math.ceil((at - now) / 60000);
  let relative = '等待额度刷新';
  if (mins > 0) {
    const days = Math.floor(mins / 1440), hours = Math.floor(mins % 1440 / 60), minutes = mins % 60;
    relative = (days ? `${days} 天${hours ? ` ${hours} 小时` : ''}` : hours ? `${hours} 小时${minutes ? ` ${minutes} 分钟` : ''}` : `${minutes} 分钟`) + '后重置';
  }
  const d = new Date(at), today = new Date(now), tomorrow = new Date(now); tomorrow.setDate(tomorrow.getDate() + 1);
  const sameDay = x => d.toDateString() === x.toDateString();
  const day = sameDay(today) ? '今天' : sameDay(tomorrow) ? '明天' : `${d.getMonth() + 1}月${d.getDate()}日`;
  const clock = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  return {relative, absolute: l.name === '5 小时' ? clock : l.name === '本周' ? quotaResetLabel(l, now) : `${day} ${clock}`};
}
function renderQuotaDetails() {
  const el = $('#quotaDetailBody'); if (!el) return;
  el.innerHTML = quotaGroups().map(([who, ls]) => `<section class="quota-detail-group"><h3>${who}</h3>${ls.map(l => {
    const when = quotaReset(l), pace = quotaPace(l);
    return `<div class="quota-detail-row ${l.pct > 90 ? 'critical' : l.pct >= 80 || pace?.warn ? 'hot' : ''}"><div class="quota-detail-top"><span>${esc(l.name)}</span><b class="${l.pct > 90 ? 'critical' : l.pct >= 80 ? 'hot' : ''}">已用 ${l.pct == null ? '—' : l.pct + '%'}</b></div>
      <div class="qb"><i style="width:${Math.max(0, Math.min(100, l.pct || 0))}%"></i></div>
      <div class="quota-reset"><span>${esc(when.relative)}</span>${when.absolute ? `<span>${esc(when.absolute)}</span>` : ''}</div>${pace ? `<div class="quota-pace ${pace.warn ? 'warn' : ''}">${esc(pace.text)}</div>` : ''}</div>`;
  }).join('')}</section>`).join('');
}
function openQuotaDetails() {
  if ($('#quotaDetailBody')) return;
  const el = sheet(`<div class="quota-detail-head"><h2 id="quotaTitle">额度用量</h2><button class="quota-close" aria-label="关闭">${ICON.close || '<svg viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg>'}</button></div><div id="quotaDetailBody"></div>${me.role !== 'user' ? '<button class="pill quota-manage" id="quotaManage">查看详细用量 · 进入管理</button>' : ''}`);
  el.classList.add('quota-dialog');
  const panel = el.querySelector('.box'); panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-modal', 'true'); panel.setAttribute('aria-labelledby', 'quotaTitle');
  el.querySelector('.quota-close').onclick = () => el.remove();
  const manageButton = el.querySelector('#quotaManage');
  if (manageButton) manageButton.onclick = () => { el.remove(); openAdmin('usage'); };
  el.addEventListener('keydown', e => { if (e.key === 'Escape') el.remove(); });
  renderQuotaDetails(); el.querySelector('.quota-close').focus();
}
function renderQuota() {
  const q = $('#quota'), groups = quotaGroups(); q.hidden = !groups.length;
  q.innerHTML = groups.length ? `<button class="quota-summary" aria-label="本周已用额度，点击查看详情"><span class="quota-caption">周已用</span>${groups.map(([who, ls]) => {
    const week = ls.find(l => l.name === '本周');
    return `<span class="quota-week"><span>${who}</span><b class="${week?.pct > 90 ? 'critical' : week?.pct >= 80 || (week && quotaPace(week)) ? 'hot' : ''}">${week?.pct == null ? '—' : week.pct + '%'}</b></span>`;
  }).join('')}${ICON.chev}</button>` : '';
  const button = q.querySelector('button'); if (button) button.onclick = openQuotaDetails;
  renderQuotaDetails();
}

const wan = n => n >= 10000 ? (n / 10000).toFixed(n >= 100000 ? 0 : 1) + ' 万' : String(n);
let relayReady = 0;
let relayWait = 0;
let usageRelay = {num: 0, targets: []};   // 点了接力、还在这个对话里等：新对话开好就自动过去
// to = 换到哪（sonnet / gpt …，不填原样接力）；go = 开好新对话就接着做（额度用完时“换 GPT 接着做”）
async function startRelay(num, to = '', go = false) {
  if (!to && relayReady !== num) return;
  relayReady = 0;
  document.querySelectorAll('#relayGo, #relayBtn, [data-relay]').forEach(b => { b.disabled = true; });
  relayWait = num;
  const r = await api('relay/' + num, to ? {to, go} : {}).catch(() => null);
  if (!r) { relayWait = 0; toast('没交出去，再点一次'); }
  else { if (!r.ok) relayWait = 0; if (r.message) toast(r.message); }
  lastKey = ''; loadThread(); loadSessions();
}
// 对话长了、或隔了一个多小时回来：在输入框上方提示接力到新对话（点“不用”就记住，直到对话再长一截或又隔了一阵）
function renderRelay(d) {
  const el = $('#relay'); if (!el) return;
  const r = d.limit ? null : d.usage && d.usage.relay;   // 额度用完了：同一家接力也接不上，只看额度卡片上的“换一家接着做”
  relayReady = d.can_relay && !d.live && !d.usage?.busy && !d.limit ? d.num : 0;
  usageRelay = {num: d.num, targets: d.can_relay && !d.live && !d.limit ? d.relay_targets || [] : []};   // 换模型接力放进“上下文”小窗
  if (relayWait === d.num && d.relay_to) { relayWait = 0; el.hidden = true; return setTimeout(() => followRelay(d.relay_to)); }
  const off = !r || (r.key && localStorage.getItem('relayOff:' + d.num) === r.key);
  el.hidden = off; el.className = 'relay' + (r && r.kind === 'running' ? ' running' : '');
  if (off) return;
  if (r.kind === 'running') { el.innerHTML = '<i class="spin"></i> 正在整理上下文…'; return; }
  // 一句话说清现在该做什么，只给一个主按钮；换模型、另一种做法都在右上角“上下文”小窗里
  const t = wan(r.tokens), squeeze = r.kind === 'compact';
  el.innerHTML = `<span class="rt">${r.kind === 'cold' ? `隔了 ${r.hours} 小时，缓存已过期。要换件事，接力到新对话更省。`
    : squeeze ? `上下文用了 ${r.pct}%，快满了。没做完先压缩，做完了再接力。`
    : `对话已约 ${t} token。这一段做完了，就接力到新对话。`}</span>
    <button class="on" id="${squeeze ? 'relayCompact' : 'relayGo'}">${squeeze ? '压缩' : '接力'}</button>
    <button class="x" id="relayNo" aria-label="不用" title="不用">${ICON.close || '×'}</button>`;
  if ($('#relayGo')) $('#relayGo').onclick = e => { e.currentTarget.disabled = true; startRelay(d.num); };
  if ($('#relayCompact')) $('#relayCompact').onclick = async e => {
    e.currentTarget.disabled = true; localStorage.setItem('relayOff:' + d.num, r.key); el.hidden = true;
    const res = await api('compact/' + d.num, {}).catch(() => null); toast(res?.message || (res ? '已交给它，进度看对话里' : '没交出去，再点一次'));
    lastKey = ''; loadThread();
  };
  $('#relayNo').onclick = () => { localStorage.setItem('relayOff:' + d.num, r.key); el.hidden = true; };
}

// 换到别家 / 别的型号接力的按钮：那家额度偏紧、告急时悬停说一声
const relayAlt = (a, text, cls = '') => `<button class="${cls}" data-relay="${esc(a.to)}" title="${a.level && a.level !== '充足' ? esc(a.label + ' 额度' + a.level) : '接力到新对话，换 ' + esc(a.label)}">${esc(text)}</button>`;
function bindRelayAlts(root, num, go = false) {
  root.querySelectorAll('[data-relay]').forEach(b => b.onclick = () => startRelay(num, b.dataset.relay, go));
}

// 右上角一个“上下文 N%”：点开看这个对话多长、额度，以及能做的事（接力、换模型接力、压缩）
function renderUsage(u) {
  const el = $('#usageBox'); if (!el || !u) return;
  const ctxWarn = u.context != null && u.context >= 70;
  const ls = u.mkey === 'gpt' ? limitsGpt : modelLimits(limits, u.mkey);
  const hotL = ls.filter(l => l.pct >= 90).sort((a, b) => b.pct - a.pct)[0];
  const label = hotL ? `${u.mkey === 'gpt' ? 'Codex ' : ''}${esc(hotL.name)} ${hotL.pct}%` : u.context != null ? `<span class="um">上下文 </span>${u.context}%` : '用量';
  const running = u.relay?.kind === 'running', canRelay = !u.busy && !running && usageRelay.num === sel;
  const relayBtns = running ? '<button class="pill" disabled><i class="spin"></i>正在写交接说明</button>'
    : `<button class="pill ${u.relay_suggested ? 'on' : ''}" id="relayBtn" ${canRelay ? '' : 'disabled'}>${u.busy ? '回答结束后才能接力' : '接力到新对话'}</button>`
      + (canRelay ? usageRelay.targets.map(a => relayAlt(a, '接力并换 ' + a.label, 'pill')).join('') : '');
  el.innerHTML = `<button class="pill ${hotL || ctxWarn || u.relay_suggested ? 'warn' : ''}" id="usagePill" title="上下文和额度">${label}</button>`
    + (usageOpen ? `<div class="usage">${qcell({name: '这个对话的上下文', pct: u.context}, 70)}
      ${u.tokens ? `<div class="uh">约 ${wan(u.tokens)} token，每说一句都要重读一遍。一段做完就接力，没做完嫌长就压缩。</div>` : ''}
      <div class="ua">${relayBtns}<button class="pill" id="compactBtn" ${u.compacting || u.busy ? 'disabled' : ''}>${
        u.compacting === 'running' ? '<i class="spin"></i>正在压缩' : u.compacting === 'queued' ? '已排队压缩' : '压缩上下文'}</button></div>
      <div class="qh">${u.mkey === 'gpt' ? 'GPT' : 'Claude'} 额度</div>${ls.map(l => qcell(l)).join('')}</div>` : '');
  $('#usagePill').onclick = e => { e.stopPropagation(); usageOpen = !usageOpen; renderUsage(u); };
  if ($('#relayBtn')) $('#relayBtn').onclick = e => { e.stopPropagation(); usageOpen = false; startRelay(sel); };
  bindRelayAlts(el, sel);
  if ($('#compactBtn')) $('#compactBtn').onclick = async e => {
    e.stopPropagation(); const b = e.currentTarget; b.disabled = true; b.innerHTML = '<i class="spin"></i>压缩中';
    const r = await api('compact/' + sel, {}).catch(() => null); b.textContent = r ? '已交给它，进度看对话里' : '没交出去，再点一次';
    lastKey = ''; loadThread();
  };
}
document.addEventListener('click', e => { if (usageOpen && !e.target.closest('.usage')) { usageOpen = false; lastKey = ''; loadThread(); } });

