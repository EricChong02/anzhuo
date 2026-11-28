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
// 上下文小窗里的一行：名字和重置时间同一行，下面一条细进度条；偏紧时再补一句何时用完
function qline(l, hot = 80) {
  const reset = quotaResetLabel(l), pace = quotaPace(l), h = l.pct != null && l.pct >= hot || pace?.warn;
  return `<div class="qm ${h ? 'hot' : ''}"><div class="ql"><span>${esc(l.name)}${reset ? `<em>${esc(reset)} 重置</em>` : ''}</span><b>${l.pct == null ? '—' : l.pct + '%'}</b></div>
    <div class="qb"><i style="width:${Math.max(l.pct ? 2 : 0, Math.min(100, l.pct || 0))}%"></i></div>${pace?.warn ? `<div class="qr qp">${esc(pace.text)}</div>` : ''}</div>`;
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
  document.querySelectorAll('#relayBtn, [data-relay]').forEach(b => { b.disabled = true; });
  relayWait = num;
  const r = await api('relay/' + num, to ? {to, go} : {}).catch(() => null);
  if (!r) { relayWait = 0; toast('没交出去，再点一次'); }
  else { if (!r.ok) relayWait = 0; if (r.message) toast(r.message); }
  lastKey = ''; loadThread(); loadSessions();
}
// 输入框上方的状态条：只在正在写交接说明时出现。接力、压缩都在右上角“上下文”小窗里手动点，不主动提示
function renderRelay(d) {
  const el = $('#relay'); if (!el) return;
  const running = !d.limit && d.usage?.relay?.kind === 'running';
  relayReady = d.can_relay && !d.live && !d.usage?.busy && !d.limit ? d.num : 0;
  usageRelay = {num: d.num, targets: d.can_relay && !d.live && !d.limit ? d.relay_targets || [] : []};   // 换模型接力放进“上下文”小窗
  if (relayWait === d.num && d.relay_to) { relayWait = 0; el.hidden = true; return setTimeout(() => followRelay(d.relay_to)); }
  el.hidden = !running; el.className = 'relay' + (running ? ' running' : '');
  if (running) el.innerHTML = '<i class="spin"></i> 正在整理上下文…';
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
  const ls = u.has_quota === false ? [] : u.mkey === 'gpt' ? limitsGpt : modelLimits(limits, u.mkey);
  const hotL = ls.filter(l => l.pct >= 90).sort((a, b) => b.pct - a.pct)[0];
  const label = hotL ? `${u.mkey === 'gpt' ? 'Codex ' : ''}${esc(hotL.name)} ${hotL.pct}%` : u.context != null ? `<span class="um">上下文 </span>${u.context}%` : '用量';
  const running = u.relay?.kind === 'running', canRelay = !u.busy && !running && usageRelay.num === sel;
  const relayBtn = running ? '<button class="pill" disabled><i class="spin"></i>写交接说明</button>'
    : `<button class="pill" id="relayBtn" ${canRelay ? '' : 'disabled'} title="${u.busy ? '回答结束后才能接力' : '写交接说明，换到新对话接着做'}">接力到新对话</button>`;
  const compactBtn = `<button class="pill" id="compactBtn" ${u.compacting || u.busy ? 'disabled' : ''}>${
    u.compacting === 'running' ? '<i class="spin"></i>压缩中' : u.compacting === 'queued' ? '已排队' : '压缩'}</button>`;
  const alts = canRelay && usageRelay.targets.length ? `<div class="ux">换模型接力${usageRelay.targets.map(a => relayAlt(a, a.label, 'lk')).join('')}</div>` : '';
  const ctx = u.context != null ? qline({name: u.tokens ? `上下文 · 约 ${wan(u.tokens)} token` : '上下文', pct: u.context}, 70) : '';
  el.innerHTML = `<button class="pill ${hotL || ctxWarn ? 'warn' : ''}" id="usagePill" title="上下文和额度">${label}</button>`
    + (usageOpen ? `<div class="usage">${ctx}<div class="ua">${relayBtn}${compactBtn}</div>${alts}
      ${u.has_quota === false ? '<div class="uh">按量计费，不占订阅额度</div>' : ls.length ? `<div class="usep"></div>${ls.map(l => qline(l)).join('')}` : ''}</div>` : '');
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

