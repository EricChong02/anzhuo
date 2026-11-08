// 管理 · 设置：把 config.toml 里常改的项摆出来改（后端见 prefs.py，分组、显隐、高级都照那张表）。
// 像手机系统设置：一组一张卡片，一行一项，右边是现在的值；开关点了就存，其余点一下在底部弹层里改，点“完成”就存。
// 只在打开“设置”页时才加载；用主页面的 esc / toast / api / sheet。
(() => {
  const CSS = `
#admCol > .st{display:flex;flex-direction:column;gap:6px;padding-bottom:24px;max-width:680px;width:100%;margin:0 auto}
.st-note{font-size:var(--t2);color:var(--amber);background:var(--amber-soft);border-radius:var(--r2);padding:10px 14px;line-height:1.5}
.st-gh{font-size:var(--t1);color:var(--faint);padding:14px 16px 6px;letter-spacing:.06em}
.st-card{background:var(--card);box-shadow:var(--rim),var(--lift);border-radius:var(--r2);overflow:hidden}
.st-r{display:flex;align-items:center;gap:12px;width:100%;min-height:54px;padding:10px 16px;box-sizing:border-box;text-align:left;
  color:var(--ink);font:inherit;background:none;border:0;position:relative}
.st-r+.st-r::before{content:"";position:absolute;left:16px;right:0;top:0;height:1px;background:var(--line)}
button.st-r{cursor:pointer} @media (hover:hover){button.st-r:hover{background:var(--glass-lo)}}
.st-r .tx{flex:1;min-width:0} .st-r .tx b{display:block;font-weight:400;font-size:var(--t3)}
.st-r .tx small{display:block;color:var(--mute);font-size:var(--t1);margin-top:2px;line-height:1.45}
.st-r .tx small.one{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.st-r .val{flex:none;max-width:48%;color:var(--mute);font-size:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.st-r .chev{flex:none;width:8px;height:8px;border:solid var(--faint);border-width:1.5px 1.5px 0 0;transform:rotate(45deg);margin-right:2px}
.st-sw{flex:none;position:relative;width:46px;height:28px;border-radius:14px;background:var(--hair);transition:background .2s}
.st-sw::after{content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.2);transition:transform .2s}
.st-sw.on{background:var(--acc-hi)} .st-sw.on::after{transform:translateX(18px)}
.st-adv{margin-top:6px} .st-adv>summary{list-style:none;cursor:pointer;font-size:var(--t2);color:var(--mute);padding:12px 16px}
.st-adv>summary::-webkit-details-marker{display:none} .st-adv>summary::after{content:" ›"} .st-adv[open]>summary::after{content:" ⌄"}
.amenu .box.st-sheet{width:min(480px,100%);padding:6px 16px 16px;max-height:86vh;overflow:auto;box-sizing:border-box}
.st-sh{display:flex;align-items:center;gap:8px;height:52px} .st-sh b{flex:1;font-size:var(--t4);font-weight:500}
.amenu .st-sh button{width:auto;height:34px;padding:0 14px;border-radius:var(--rf);font-size:var(--t2)}
.amenu .st-sh .ok{background:var(--acc);color:var(--on-acc)} .amenu .st-sh .ok:disabled{opacity:.5}
.st-help{color:var(--mute);font-size:var(--t2);line-height:1.55;margin:0 0 12px}
.st-err{color:var(--red,#c0392b);font-size:var(--t2);margin:8px 0 0}
.st-sheet textarea,.st-sheet .st-in{width:100%;box-sizing:border-box;font:inherit;font-size:max(16px,var(--t3));color:var(--ink);background:var(--bg);
  border:1px solid var(--line);border-radius:var(--r1);padding:10px 12px;line-height:1.55}
.st-sheet textarea{min-height:180px;resize:vertical} .amenu .st-sheet input.st-in{height:46px;margin:0}
.st-lab{font-size:var(--t1);color:var(--faint);margin:14px 2px 8px;letter-spacing:.06em}
.st-chips{display:flex;flex-wrap:wrap;gap:8px}
.amenu .st-chips button{width:auto;height:36px;padding:0 14px;border-radius:var(--rf);font-size:var(--t2);color:var(--ink);box-shadow:inset 0 0 0 1px var(--line)}
.amenu .st-chips button.on{background:var(--accsoft);color:var(--acc);box-shadow:inset 0 0 0 1.5px var(--acc-hi);font-weight:500}
.amenu .st-opts button{height:50px;font-size:var(--t3)} .amenu .st-opts button.on::after{content:"✓";margin-left:auto;color:var(--acc)}`;
  if (!document.getElementById('st-css')) {
    const s = document.createElement('style'); s.id = 'st-css'; s.textContent = CSS; document.head.appendChild(s);
  }
  const CLAUDE = new Set(['opus', 'sonnet', 'haiku', 'fable']);
  const GROUP_HINT = {'模型': '新对话没点名模型时照这里选'};
  let data = null, col = null;
  const byKey = () => Object.fromEntries(data.fields.map(f => [f.key, f]));
  const label = (list, v) => (list.find(([o]) => String(o) === String(v ?? '')) || [, v || '—'])[1];
  const wanTok = n => n >= 10000 ? (n / 10000) + ' 万' : String(n);

  // 行右边显示的值：一眼看出现在是什么，不把整段内容摊出来
  function summary(f, v) {
    if (f.kind === 'select') return label(f.options, v);
    if (f.kind === 'int') return f.key.startsWith('relay') ? wanTok(v) : String(v);
    if (f.kind === 'lines') return (v || []).length ? `${v.length} 个` : '无';
    if (f.kind === 'folders') return Object.keys(v || {}).length ? `${Object.keys(v).length} 个` : '无';
    const t = String(v || '').trim().split('\n')[0];
    return t || '未设置';
  }
  // 选项名去掉括号里的说明（“DeepSeek（Claude Code）”→“DeepSeek”），这台电脑用不了的照实标上
  const pickName = (f, k) => { const t = label(f.picks, k); return t.replace(/（.*?）/g, '') + (/用不了/.test(t) ? '（用不了）' : ''); };
  const tierValue = (f, r) => !CLAUDE.has(r.use) && r.model ? r.model : pickName(f, r.use);

  function row(f, v) {
    const help = f.help && (f.kind === 'bool') ? `<small>${esc(f.help)}</small>` : '';
    if (f.kind === 'bool') return `<div class="st-r" role="switch" tabindex="0" aria-checked="${!!v}" data-tg="${f.key}"><span class="tx"><b>${esc(f.label)}</b>${help}</span><span class="st-sw ${v ? 'on' : ''}"></span></div>`;
    return `<button class="st-r" data-k="${f.key}"><span class="tx"><b>${esc(f.label)}</b></span><span class="val">${esc(summary(f, v))}</span><span class="chev"></span></button>`;
  }
  function tierRows(f, v) {
    return f.rows.map(([kind, name, help]) => `<button class="st-r" data-tier="${kind}"><span class="tx"><b>${esc(name)}</b><small class="one">${esc(help)}</small></span>
      <span class="val">${esc(tierValue(f, (v || {})[kind] || {}))}</span><span class="chev"></span></button>`).join('');
  }
  const shown = f => !f.when || data.values[f.when];
  function group(name, fields) {
    const rows = fields.filter(shown).map(f => f.kind === 'split' ? tierRows(f, data.values[f.key]) : row(f, data.values[f.key])).join('');
    return rows ? `<div class="st-gh">${esc(name)}${GROUP_HINT[name] ? ' · ' + esc(GROUP_HINT[name]) : ''}</div><div class="st-card">${rows}</div>` : '';
  }

  function draw() {
    const main = data.fields.filter(f => !f.adv), adv = data.fields.filter(f => f.adv);
    const groups = [...new Set(main.map(f => f.group))];
    const advOpen = col.querySelector('.st-adv')?.open;
    col.innerHTML = `<div class="st">${data.restart.length ? `<div class="st-note">已保存，重启后生效：${esc(data.restart.join('、'))}</div>` : ''}
      ${groups.map(g => group(g, main.filter(f => f.group === g))).join('')}
      <details class="st-adv" ${advOpen ? 'open' : ''}><summary>高级</summary><div class="st-card">${adv.filter(shown).map(f => row(f, data.values[f.key])).join('')}</div></details></div>`;
    const keys = byKey();
    col.querySelectorAll('[data-tg]').forEach(el => {
      const flip = () => save({[el.dataset.tg]: !data.values[el.dataset.tg]});
      el.onclick = flip; el.onkeydown = e => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); flip(); } };
    });
    col.querySelectorAll('[data-k]').forEach(el => el.onclick = () => edit(keys[el.dataset.k]));
    col.querySelectorAll('[data-tier]').forEach(el => el.onclick = () => editTier(keys.split, el.dataset.tier));
  }

  // 存：只交改了的这一项；成功就重读（显示文件里的值和“重启后生效”），失败把原因交回给弹层
  async function save(change) {
    let r;
    try { r = await api('admin/prefs', change); } catch (e) { r = {ok: false, error: '没连上，再试一次'}; }
    if (!r.ok) { toast(r.error || '没存上'); return r.error || '没存上'; }
    if (r.changed.length) toast(r.restart.length && Object.keys(change).some(k => !byKey()[k].live) ? '已保存，重启后生效' : '已保存');
    await load(true);
    return '';
  }

  function openSheet(title, body, withOk = true) {
    const el = sheet(`<div class="st-sh"><b>${esc(title)}</b><button data-x>取消</button>${withOk ? '<button class="ok" data-ok>完成</button>' : ''}</div>${body}<div class="st-err" hidden></div>`);
    el.querySelector('.box').classList.add('st-sheet');
    el.querySelector('[data-x]').onclick = () => el.remove();
    el.addEventListener('keydown', e => { if (e.key === 'Escape') el.remove(); });
    return el;
  }
  async function commit(el, change) {
    const ok = el.querySelector('[data-ok]'); if (ok) ok.disabled = true;
    const err = await save(change);
    if (!err) return el.remove();
    if (ok) ok.disabled = false;
    const box = el.querySelector('.st-err'); box.hidden = false; box.textContent = err;
  }

  function edit(f) {
    const v = data.values[f.key], help = f.help ? `<p class="st-help">${esc(f.help)}</p>` : '';
    if (f.kind === 'select') {   // 点一项就存，不用再点完成
      const el = openSheet(f.label, help + `<div class="st-opts">${f.options.map(([o, t]) => `<button data-o="${esc(o)}" class="${String(o) === String(v ?? '') ? 'on' : ''}">${esc(t)}</button>`).join('')}</div>`, false);
      el.querySelectorAll('[data-o]').forEach(b => b.onclick = () => commit(el, {[f.key]: b.dataset.o}));
      return;
    }
    const text = f.kind === 'lines' ? (v || []).join('\n') : f.kind === 'folders' ? Object.entries(v || {}).map(([k, p]) => `${k} = ${p}`).join('\n') : String(v ?? '');
    const multi = ['textarea', 'lines', 'folders'].includes(f.kind);
    const input = multi ? `<textarea spellcheck="false">${esc(text)}</textarea>`
      : `<input class="st-in" ${f.kind === 'int' ? `type="number" inputmode="numeric" min="${f.range[0]}" max="${f.range[1]}"` : 'type="text"'} value="${esc(text)}">`;
    const el = openSheet(f.label, help + input), inp = el.querySelector('textarea, input');
    setTimeout(() => inp.focus(), 60);
    const go = () => commit(el, {[f.key]: f.kind === 'int' ? Number(inp.value) : inp.value});
    el.querySelector('[data-ok]').onclick = go;
    if (!multi) inp.onkeydown = e => { if (e.key === 'Enter') go(); };
  }

  // 分工表的一档：模型、别家的具体型号、深度、用不了时换谁。改完整张表一起交（后端按整张表校验）
  function editTier(f, kind) {
    const all = data.values.split || {}, r = {use: '', model: '', effort: '', backup: '', ...(all[kind] || {})};
    const [, name, help] = f.rows.find(x => x[0] === kind);
    const chips = (list, cur, attr) => `<div class="st-chips">${list.map(([o, t]) => `<button data-${attr}="${esc(o)}" class="${String(o) === String(cur ?? '') ? 'on' : ''}">${esc(t)}</button>`).join('')}</div>`;
    const el = openSheet(name, `<p class="st-help">${esc(help)}</p><div class="st-body"></div>`), body = el.querySelector('.st-body');
    const paint = () => {
      const mine = !CLAUDE.has(r.use), models = (f.engine_models || {})[r.use] || [];
      body.innerHTML = `<div class="st-lab">模型</div>${chips(f.picks.map(([k]) => [k, pickName(f, k)]), r.use, 'use')}
        ${mine ? `<div class="st-lab">具体型号</div>${models.length ? chips([['', '它默认的'], ...[...new Set([...models, r.model].filter(Boolean))].map(m => [m, m])], r.model, 'model')
          : `<input class="st-in" data-mi placeholder="留空用它默认的" value="${esc(r.model)}">`}` : ''}
        <div class="st-lab">思考深度</div>${chips(f.efforts, r.effort, 'effort')}
        <div class="st-lab">首选用不了时换成</div>${chips([['', '不换'], ...f.picks.filter(([k]) => k !== r.use).map(([k]) => [k, pickName(f, k)])], r.backup, 'backup')}`;
      body.querySelectorAll('[data-use]').forEach(b => b.onclick = () => { r.use = b.dataset.use; r.model = ''; if (r.backup === r.use) r.backup = ''; paint(); });
      body.querySelectorAll('[data-model]').forEach(b => b.onclick = () => { r.model = b.dataset.model; paint(); });
      body.querySelectorAll('[data-effort]').forEach(b => b.onclick = () => { r.effort = b.dataset.effort; paint(); });
      body.querySelectorAll('[data-backup]').forEach(b => b.onclick = () => { r.backup = b.dataset.backup; paint(); });
      const mi = body.querySelector('[data-mi]'); if (mi) mi.oninput = () => { r.model = mi.value.trim(); };
    };
    paint();
    el.querySelector('[data-ok]').onclick = () => commit(el, {split: {...all, [kind]: {...r, model: CLAUDE.has(r.use) ? '' : r.model}}});
  }

  async function load(quiet) {
    if (!quiet) col.innerHTML = '<div class="ac-empty">读取中…</div>';
    try {
      const r = await fetch('/_x/api/admin/prefs');
      if (r.status === 401) { location.href = '/_x/login?next=' + encodeURIComponent('/_x/#admin'); return; }
      if (!r.ok) throw new Error();
      data = await r.json();
    } catch (e) { if (!quiet) col.innerHTML = '<div class="ac-empty">没读到设置，稍后再试</div>'; return; }
    if (col.isConnected) draw();
  }

  window.drawSettings = c => { col = c; load(); };
})();
