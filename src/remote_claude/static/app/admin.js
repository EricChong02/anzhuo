// ═══ 管理（只有 admin）：用量、账号。是主区里的一个视图，和对话页一样进出 ═══
let admTab = 'usage', admDays = 'week', admData = null;
const ADM_ICON = {
  phone: '<svg viewBox="0 0 24 24"><rect x="7" y="3" width="10" height="18" rx="2.5"/><path d="M11 18h2"/></svg>',
  desk: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
};
const ownerName = o => o === 'admin' ? '主账号' : o;
const tokn = v => v >= 1e9 ? (v / 1e9).toFixed(1) + 'B' : v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? Math.round(v / 1e3) + 'K' : String(v || 0);
const admOwners = d => [...new Set(['admin', ...d.accounts.map(a => a.owner)])];
const ownerColor = (d, o) => admOwners(d).indexOf(o) % 2 ? 'var(--s2)' : 'var(--s1)';
function openAdmin(tab, fromPop) {
  if (me.role === 'user') return goList();
  if (sel) presence(false);
  sel = null; admTab = tab || admTab;
  if (!fromPop) navTo({a: admTab}, '#admin');
  $('#app').classList.add('in');
  $('#main').innerHTML = `<div class="head"><button class="icon-btn back" id="back">${ICON.back}</button><div class="ti"><b>管理</b></div>
      <div class="adm-tabs" id="admTabs" role="tablist"><button role="tab" data-t="usage">用量</button><button role="tab" data-t="accounts">账号</button><button role="tab" data-t="settings">设置</button></div></div>
    <div class="adm" id="adm"><div class="col" id="admCol"></div></div>`;
  $('#back').onclick = () => history.state && history.state.a ? history.back() : goList();
  $('#admTabs').onclick = e => { const b = e.target.closest('button'); if (!b || b.dataset.t === admTab) return; admTab = b.dataset.t; history.replaceState({a: admTab}, '', '#admin'); drawAdmin(); };
  renderSide(); drawAdmin(); loadAdmin();
}
async function loadAdmin() {
  try {
    const range = admDays;
    const r = await fetch('/_x/api/admin?' + (range === 'week' ? 'period=week' : 'days=' + range));
    if (r.status === 401) { location.href = '/_x/login?next=' + encodeURIComponent('/_x/#admin'); return; }
    if (!r.ok) throw new Error();
    const data = await r.json();
    if (range !== admDays) return;
    admData = data;
  } catch (e) { if (!admData && $('#admCol')) $('#admCol').innerHTML = '<div class="ac-empty">没读到，下拉或稍后再试</div>'; return; }
  drawAdmin();
}
async function admAct(body) {
  const r = await api('admin', body).catch(() => null);
  toast(r && r.ok ? '好了' : (r && r.error) || '没成功，再试一次');
  await loadAdmin();
}
function drawAdmin() {
  const col = $('#admCol'); if (!col) return;
  document.querySelectorAll('#admTabs button').forEach(b => { b.classList.toggle('on', b.dataset.t === admTab); b.setAttribute('aria-selected', b.dataset.t === admTab); });
  if (admTab === 'settings') return openSettings(col);
  if (!admData) { col.innerHTML = '<div class="ac-empty">读取中…</div>'; return; }
  if (admTab === 'accounts') return drawAccounts(col, admData);
  drawUsage(col, admData);
}

// ── 设置：界面在 settings.js（打开这一页时才加载），后端见 prefs.py ──
let settingsShown = false;
function openSettings(col) {
  if (settingsShown && col.querySelector('.st')) return;   // 用量数据刷新时别把正在改的设置冲掉
  settingsShown = true;
  if (window.drawSettings) return window.drawSettings(col);
  col.innerHTML = '<div class="ac-empty">读取中…</div>';
  const s = document.createElement('script');
  s.src = '/_x/admin/settings.js';
  s.onload = () => window.drawSettings(col);
  s.onerror = () => { settingsShown = false; col.innerHTML = '<div class="ac-empty">设置页没加载出来，刷新再试</div>'; };
  document.head.appendChild(s);
}

// ── 用量 ──
function aqRow(name, pct, reset) {
  const cls = pct == null ? '' : pct > 90 ? 'critical' : pct >= 80 ? 'hot' : '';
  return `<div class="aq-row ${cls}"><div class="aq-top"><span>${esc(name)}</span><b class="num">${pct == null ? '—' : Math.round(pct) + '%'}</b></div>
    <div class="qb"><i style="width:${Math.max(pct ? 2 : 0, Math.min(100, pct || 0))}%"></i></div>${reset ? `<div class="qr">${esc(reset)}</div>` : ''}</div>`;
}
function quotaPlate(title, ls) {
  const order = ['5 小时', '本周'], rows = order.map(n => ls.find(l => l.name === n)).filter(Boolean);
  return `<div class="plate aq"><h3>${esc(title)}</h3>${rows.length ? rows.map(l => aqRow(l.name, l.pct, [quotaReset(l).relative, quotaPace(l)?.text].filter(Boolean).join(' · '))).join('')
    : `<div class="aq-row">${aqRow('本周', null, '')}</div>`}</div>`;
}
// 金额只在后台用来换算：× 每美元约合周额度的百分比 = 占主账号周额度的 %；还没校准出比例时返回 null
function wkv(u, cost) { return u.rate == null ? null : cost * u.rate; }
function fpct(p) { return p <= 0 ? '0%' : p < 0.1 ? '<0.1%' : p < 10 ? (Math.round(p * 10) / 10) + '%' : Math.round(p) + '%'; }
function wk(u, cost) { const p = wkv(u, cost); return p == null ? '' : fpct(p); }
// 用量页三块：额度（每家一块板，其他账号的周上限并在同一块里）→ 消耗（大数 + 图例 + 每天柱子）→ 哪些对话用得多（先看前 8 个）
let admAllConvs = false;
function drawUsage(col, d) {
  const u = d.usage, os = admOwners(d), many = os.length > 1;
  const fenced = d.accounts.filter(a => a.quota);
  const others = fenced.length ? `<div class="plate aq aq-wide"><h3>其他账号 · 本周</h3>${fenced.map(a => aqRow(`${a.name} · 上限 ${Math.round(a.quota.share * 100)}%`, a.quota.calibrating ? null : a.quota.pct,
      a.quota.reset_at ? quotaReset({name: '本周', reset_at: a.quota.reset_at}).relative : '')).join('')}</div>` : '';
  const turns = u.days.reduce((s, x) => s + x.turns, 0), toks = u.days.reduce((s, x) => s + x.tokens, 0);
  const gptToks = u.days.reduce((s, x) => s + x.gpt_tokens, 0);
  const legend = many ? `<div class="au-legend">${os.map(o => { const r = u.owners.find(x => x.owner === o) || {cost: 0, tokens: 0};
    return `<span><i class="sw" style="background:${ownerColor(d, o)}"></i>${esc(ownerName(o))}<b class="num">${wk(u, r.cost) || tokn(r.tokens)}</b></span>`; }).join('')}</div>` : '';
  const facts = [wk(u, u.total_cost) ? `周额度 ${wk(u, u.total_cost)}` : '', `${turns} 轮`, gptToks ? `Codex ${tokn(gptToks)}` : ''].filter(Boolean);
  const convs = u.conversations, shown = admAllConvs ? convs : convs.slice(0, 8);
  col.innerHTML = `<div class="adm-h"><span>额度</span></div><div class="aq-grid">${quotaPlate('Claude', d.limits) + quotaPlate('Codex', d.limits_gpt) + others}</div>
    <div class="adm-h"><span>消耗</span><div class="adm-tabs" id="admDays">${['week', 7, 30, 90].map(n => `<button data-d="${n}" class="${n === admDays ? 'on' : ''}">${n === 'week' ? '本周' : n + ' 天'}</button>`).join('')}</div></div>
    ${d.period === 'week' && !d.week_available ? '<div class="ac-empty">本周数据暂不可用</div>' : ''}
    <div class="plate"><div class="au-big"><b class="num">${tokn(toks)}</b><span>tokens · ${facts.join(' · ')}</span></div>${legend}
      <div class="au-chart" id="auChart"><svg id="auSvg" role="img" aria-label="每天用掉的周额度"></svg><div class="au-tip" id="auTip" hidden></div></div></div>
    <div class="adm-h"><span>用得多的对话</span><span>${convs.length ? convs.length + ' 个' : ''}</span></div>
    <div class="plate ac-list">${convs.length ? shown.map(c => {
      const gpt = c.engine === 'codex', mine = c.owner === 'admin' && sessions.some(s => s.num === c.num);
      const ms = c.models.map(m => m.replace(/^claude-/, '').replace(/-\d+(-\d+)?$/, '')), main = ms.filter(m => m !== 'haiku');
      const bits = [many ? ownerName(c.owner) : '', ...(main.length ? main : ms), c.turns + ' 轮', ago(c.last)].filter(Boolean), p = gpt ? null : wkv(u, c.cost);
      return `<button class="ac" ${mine ? `data-n="${c.num}"` : 'tabindex="-1"'}>
        <span class="nm">${gpt ? engTag('codex') : ''}<span>${esc(c.name)}</span></span><span class="v num">${p == null ? tokn(c.tokens) : fpct(p)}</span>
        <span class="st">${esc([...new Set(bits)].join(' · '))}</span><span class="pc num">${p == null ? 'tokens' : tokn(c.tokens)}</span>
        <span class="bar"><i style="width:${p == null ? 0 : Math.max(1, Math.min(100, p))}%;background:${ownerColor(d, c.owner)}"></i></span></button>`;
    }).join('') + (convs.length > shown.length ? `<button class="ac-more" id="acMore">显示全部 ${convs.length} 个</button>` : '') : '<div class="ac-empty">还没有记录</div>'}</div>`;
  $('#admDays').onclick = e => { const b = e.target.closest('button'); if (!b) return; admDays = b.dataset.d === 'week' ? 'week' : +b.dataset.d; loadAdmin(); };
  if ($('#acMore')) $('#acMore').onclick = () => { admAllConvs = true; drawUsage(col, d); };
  col.querySelectorAll('.ac[data-n]').forEach(b => b.onclick = () => openSession(+b.dataset.n));
  drawChart(d);
}
// 每天一根柱子，按账号叠（主账号在下）；段与段之间留 2px 缝，只有顶端是圆角；悬停 / 点一下看当天
function drawChart(d) {
  const svg = $('#auSvg'); if (!svg) return;
  const days = d.usage.days, os = admOwners(d);
  const W = Math.max(280, svg.clientWidth || 600), H = 200, L = 40, R = 4, T = 8, B = 24;
  const byPct = d.usage.rate != null, val = (x, o) => byPct ? (x.by_owner[o] || 0) * d.usage.rate : (x.by_owner_tok[o] || 0);
  const fmt = v => byPct ? (Math.round(v * 10) / 10) + '%' : tokn(v);
  const tot = x => os.reduce((s, o) => s + val(x, o), 0);
  const max = Math.max(0.01, ...days.map(tot));
  const step = (() => { const raw = max / 3, p = Math.pow(10, Math.floor(Math.log10(raw))); return [1, 2, 2.5, 5, 10].map(m => m * p).find(v => v >= raw); })();
  const top = step * Math.ceil(max / step), y = v => T + (H - T - B) * (1 - v / top);
  const slot = (W - L - R) / days.length, bw = Math.max(3, Math.min(22, slot * .62));
  let g = '';
  for (let v = 0; v <= top + 1e-9; v += step) g += `<line class="gl" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${fmt(v)}</text>`;
  g += `<rect class="cur" id="auCur" x="0" y="${T}" width="${slot}" height="${H - T - B}" rx="6" opacity="0"/>`;
  const every = Math.ceil(days.length / (W < 500 ? 5 : 10));
  days.forEach((x, i) => {
    const bx = L + slot * i + (slot - bw) / 2, parts = os.map(o => val(x, o));
    const last = parts.reduce((k, v, j) => v > 0 ? j : k, -1);
    let base = 0;
    parts.forEach((v, k) => {
      if (v <= 0) return;
      const y1 = y(base + v), h = Math.max(1.5, y(base) - y1 - (base > 0 ? 2 : 0)), r = k === last ? Math.min(4, h, bw / 2) : 0;
      g += r ? `<path d="M${bx},${y1 + h}V${y1 + r}Q${bx},${y1} ${bx + r},${y1}H${bx + bw - r}Q${bx + bw},${y1} ${bx + bw},${y1 + r}V${y1 + h}Z" fill="${ownerColor(d, os[k])}"/>`
        : `<rect x="${bx}" y="${y1}" width="${bw}" height="${h}" fill="${ownerColor(d, os[k])}"/>`;
      base += v;
    });
    if ((days.length - 1 - i) % every === 0) g += `<text x="${bx + bw / 2}" y="${H - 6}" text-anchor="middle">${x.day.replace(/^0/, '').replace('-', '/')}</text>`;
  });
  days.forEach((x, i) => { g += `<rect class="hit" data-i="${i}" x="${L + slot * i}" y="${T}" width="${slot}" height="${H - T - B}" fill="transparent"/>`; });
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.innerHTML = g;
  const tip = $('#auTip'), cur = $('#auCur');
  const show = e => {
    const t = e.target.closest && e.target.closest('.hit');
    if (!t) { tip.hidden = true; cur.setAttribute('opacity', 0); return; }
    const i = +t.dataset.i, x = days[i];
    cur.setAttribute('x', L + slot * i); cur.setAttribute('opacity', 1);
    tip.innerHTML = `<b>${x.day.replace('-', '月').replace(/^0/, '')}日</b>　<span class="num">${byPct ? fpct(tot(x)) : tokn(tot(x))}</span>`
      + os.map(o => `<div class="r"><span><span class="sw" style="background:${ownerColor(d, o)}"></span>${esc(ownerName(o))}</span><span class="num">${tokn(x.by_owner_tok[o] || 0)}${byPct ? ' · ' + fpct(val(x, o)) : ''}</span></div>`).join('')
      + `<div class="r"><span>轮数</span><span class="num">${x.turns}</span></div>` + (x.gpt_tokens ? `<div class="r"><span>Codex</span><span class="num">${tokn(x.gpt_tokens)}</span></div>` : '')
      + (x.week_pct != null ? `<div class="r"><span>当天周额度</span><span class="num">${Math.round(x.week_pct)}%</span></div>` : '');
    tip.hidden = false;
    const k = svg.getBoundingClientRect().width / W, cx = (L + slot * (i + .5)) * k, w = svg.getBoundingClientRect().width;
    tip.style.left = Math.max(80, Math.min(w - 80, cx)) + 'px'; tip.style.top = Math.max(60, y(tot(x)) * k) + 'px';
  };
  svg.onpointermove = show; svg.onclick = show; svg.onpointerleave = () => { tip.hidden = true; cur.setAttribute('opacity', 0); };
}
addEventListener('resize', () => { if ($('#auSvg') && admData) drawChart(admData); });

// ── 账号 ──
// 设备一多就一长串：先列这台和最近用过的 3 台，其余点“其余 N 台”再展开
const admDevOpen = new Set();
function drawAccounts(col, d) {
  col.innerHTML = d.accounts.map(a => {
    const user = a.role !== 'admin', others = a.devices.filter(x => !x.me);
    const devs = [...a.devices].sort((x, y) => Number(y.me) - Number(x.me) || (y.seen || 0) - (x.seen || 0));
    const shown = admDevOpen.has(a.name) ? devs : devs.slice(0, 4);
    return `<div class="plate aa ${a.disabled ? 'off' : ''}" data-a="${esc(a.name)}">
      <div class="aa-hd"><span class="aa-av ${user ? 'u' : ''}">${esc(a.name.slice(0, 1).toUpperCase())}</span>
        <div class="tx"><b>${esc(a.name)}${a.disabled ? '<span class="aa-tag">已停用</span>' : ''}</b><small>${user ? esc(a.root) : '全部权限'}</small></div></div>
      ${user && a.quota ? `<div class="aa-line"><span>周上限</span><div class="adm-tabs">${[10, 20, 30, 50].map(p => `<button data-act="share" data-v="${p}" class="${Math.round(a.quota.share * 100) === p ? 'on' : ''}">${p}%</button>`).join('')}</div></div>` : ''}
      <div class="aa-devs">${shown.map(x => `<div class="aa-dev">${/iPhone|安卓/.test(x.name) ? ADM_ICON.phone : ADM_ICON.desk}
          <div class="tx"><b>${esc(x.name)}${x.me ? '<span class="me">这台</span>' : ''}</b><small>${x.me ? '正在用' : x.seen ? ago2(x.seen) : '—'}${x.ip ? ' · ' + esc(x.ip) : ''}</small></div>
          ${x.me ? '' : `<button class="pill bad" data-act="logout" data-id="${x.id}">退出</button>`}</div>`).join('') || '<div class="aa-dev"><small>没有登录着的设备</small></div>'}${devs.length > shown.length ? `<button class="ac-more" data-act="devs">其余 ${devs.length - shown.length} 台</button>` : ''}</div>
      <div class="aa-acts"><button class="pill" data-act="pw">改密码</button>${others.length > 1 ? '<button class="pill bad" data-act="logout_all">退出其他设备</button>' : ''}
        ${user ? `<button class="pill ${a.disabled ? '' : 'bad'}" data-act="${a.disabled ? 'enable' : 'disable'}">${a.disabled ? '启用' : '停用'}</button>` : ''}</div></div>`;
  }).join('');
  col.onclick = e => {
    const b = e.target.closest('[data-act]'); if (!b) return;
    const account = b.closest('[data-a]').dataset.a, act = b.dataset.act;
    if (act === 'pw') return pwSheet(account);
    if (act === 'devs') { admDevOpen.add(account); return drawAccounts(col, d); }
    if (act === 'share') return admAct({action: 'share', account, value: +b.dataset.v / 100});
    if (act === 'disable') return confirmSheet(`停用 ${account}？`, '停用', () => admAct({action: 'disable', account}));
    if (act === 'logout_all') return confirmSheet(`退出 ${account} 的其他设备？`, '退出其他设备', () => admAct({action: 'logout_all', account}));
    b.disabled = true;
    admAct({action: act, account, id: b.dataset.id});
  };
}
function confirmSheet(title, ok, go) {
  const el = sheet(`<div class="t">${esc(title)}</div><button class="del" data-ok>${esc(ok)}</button><button data-no>取消</button>`);
  el.querySelector('[data-ok]').onclick = () => { el.remove(); go(); };
  el.querySelector('[data-no]').onclick = () => el.remove();
}
function pwSheet(account) {
  const el = sheet(`<div class="pwbox"><b>${esc(account)} 的新密码</b><input type="password" autocomplete="new-password" placeholder="至少 6 位" id="pwIn">
    <button class="on" id="pwOk">保存</button><button id="pwNo">取消</button></div>`);
  const inp = el.querySelector('#pwIn'); setTimeout(() => inp.focus(), 50);
  const save = () => { if (inp.value.length < 6) { inp.focus(); return toast('至少 6 位'); } el.remove(); admAct({action: 'password', account, value: inp.value}); };
  el.querySelector('#pwOk').onclick = save; inp.onkeydown = e => { if (e.key === 'Enter') save(); };
  el.querySelector('#pwNo').onclick = () => el.remove();
}

