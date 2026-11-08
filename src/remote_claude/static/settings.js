// 管理 · 设置：把 config.toml 里常改的项摆出来改（后端见 prefs.py）。
// 只在打开“设置”页时才加载；用主页面的 esc / toast / api 和 .plate 样式，自己只带一点样式。
(() => {
  const CSS = `
.st-group h3{margin:0 0 10px;font-size:var(--t3);font-weight:600}
.st-row{padding:10px 0;border-top:1px solid var(--line)} .st-row:first-of-type{border-top:0}
.st-row label{display:flex;align-items:center;gap:10px;justify-content:space-between;font-size:var(--t3)}
.st-row small{display:block;color:var(--mute);font-size:var(--t1);margin-top:4px;line-height:1.5}
.st-row .rs{font-size:var(--t1);color:var(--amber);margin-left:6px;white-space:nowrap}
.st-row input[type=text],.st-row input[type=number],.st-row select,.st-row textarea{font:inherit;font-size:max(16px,var(--t3));color:var(--ink);
  background:var(--ctl);border:0;box-shadow:var(--rim);border-radius:10px;padding:8px 10px;box-sizing:border-box}
.st-row input[type=text],.st-row textarea{width:100%;margin-top:8px} .st-row textarea{min-height:88px;resize:vertical;line-height:1.5}
.st-row input[type=number]{width:120px} .st-row select{max-width:60%}
.st-split{display:grid;grid-template-columns:auto 1fr 1fr auto 1fr;gap:8px 8px;align-items:center;margin-top:10px;font-size:var(--t2)}
.st-split .hd{color:var(--mute);font-size:var(--t1)} .st-split b{font-weight:600;font-size:var(--t3);white-space:nowrap}
.st-split input:disabled{opacity:.45} .st-split .sc{display:block;min-width:0} .st-split .sc i{display:none}
.st-row .st-split select,.st-row .st-split input[type=text]{max-width:none;width:100%;margin:0;min-width:0}
@media (max-width:640px){.st-split{grid-template-columns:1fr 1fr}.st-split .hd{display:none}
  .st-split b{grid-column:1/-1;margin-top:6px}
  .st-split .sc i{display:block;font-style:normal;color:var(--mute);font-size:var(--t1);margin:0 0 4px 2px}}
.st-row input[type=checkbox]{width:22px;height:22px;flex:none}
.st-bar{position:sticky;bottom:8px;display:flex;gap:10px;align-items:center;padding:10px 12px 10px 16px;margin-top:4px;
  background:var(--pop,var(--panel));-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);border-radius:16px;box-shadow:var(--rim),var(--shadow)}
.st-bar .msg{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.st-bar .msg{flex:1;font-size:var(--t2);color:var(--mute)} .st-bar .msg.bad{color:var(--red,#c0392b)}
.st-note{font-size:var(--t2);color:var(--amber);padding:2px 2px 0}
.st-path{font-size:var(--t1);color:var(--faint);word-break:break-all}`;
  if (!document.getElementById('st-css')) {
    const s = document.createElement('style'); s.id = 'st-css'; s.textContent = CSS; document.head.appendChild(s);
  }
  let data = null, dirty = {};

  const asText = (f, v) => f.kind === 'lines' ? (v || []).join('\n')
    : f.kind === 'folders' ? Object.entries(v || {}).map(([k, p]) => `${k} = ${p}`).join('\n') : (v ?? '');

  const opts = (list, v) => list.map(([o, t]) =>
    `<option value="${esc(o)}" ${String(o) === String(v ?? '') ? 'selected' : ''}>${esc(t)}</option>`).join('');

  // 分工表：每档一行，首选 / 型号 / 深度 / 备选。型号只有别家（GPT、DeepSeek…）才填，给候选；选的是 Claude 型号时这一格不用填
  const CLAUDE = new Set(['opus', 'sonnet', 'haiku', 'fable']);
  function splitTable(f, v) {
    const lists = Object.entries(f.engine_models || {}).map(([k, ms]) =>
      `<datalist id="st-ml-${esc(k)}">${ms.map(m => `<option value="${esc(m)}">`).join('')}</datalist>`).join('');
    const rows = f.rows.map(([kind, name, help]) => {
      const r = (v || {})[kind] || {}, mine = !CLAUDE.has(r.use);
      return `<b title="${esc(help)}">${esc(name)}</b>
        <label class="sc"><i>首选</i><select data-s="${kind}.use" aria-label="${esc(name)}首选">${opts(f.picks, r.use)}</select></label>
        <label class="sc"><i>型号</i><input type="text" data-s="${kind}.model" aria-label="${esc(name)}型号" list="st-ml-${esc(r.use)}"
          placeholder="${mine ? '默认' : '—'}" value="${esc(mine ? r.model || '' : '')}" ${mine ? '' : 'disabled'}></label>
        <label class="sc"><i>深度</i><select data-s="${kind}.effort" aria-label="${esc(name)}深度">${opts(f.efforts, r.effort)}</select></label>
        <label class="sc"><i>备选</i><select data-s="${kind}.backup" aria-label="${esc(name)}备选">${opts([['', '不设'], ...f.picks], r.backup)}</select></label>`;
    }).join('');
    const notes = f.rows.map(([, name, help]) => `${esc(name)}：${esc(help)}`).join('；');
    return `<div class="st-split" id="st-${f.key}" data-k="${f.key}">
      <span class="hd"></span><span class="hd">首选</span><span class="hd">型号</span><span class="hd">深度</span><span class="hd">备选</span>
      ${rows}</div>${lists}<small>${notes}</small>`;
  }

  // 首选换了：型号那一格跟着换候选；换成 Claude 就清空变灰
  function splitPicked(el) {
    const [kind, k] = el.dataset.s.split('.');
    if (k !== 'use') return;
    const m = el.closest('.st-split').querySelector(`[data-s="${kind}.model"]`), mine = !CLAUDE.has(el.value);
    m.disabled = !mine; m.placeholder = mine ? '默认' : '—'; m.setAttribute('list', 'st-ml-' + el.value);
    if (!mine) m.value = '';
  }

  function input(f, v) {
    const id = 'st-' + f.key;
    if (f.kind === 'split') return splitTable(f, v);
    if (f.kind === 'bool') return `<input type="checkbox" id="${id}" data-k="${f.key}" ${v ? 'checked' : ''}>`;
    if (f.kind === 'int') { const [lo, hi] = f.range || [1, 10];
      return `<input type="number" min="${lo}" max="${hi}" id="${id}" data-k="${f.key}" value="${esc(v)}">`; }
    if (f.kind === 'select') return `<select id="${id}" data-k="${f.key}">${opts(f.options, v)}</select>`;
    if (f.kind === 'text') return `<input type="text" id="${id}" data-k="${f.key}" value="${esc(v)}">`;
    return `<textarea id="${id}" data-k="${f.key}" spellcheck="false">${esc(asText(f, v))}</textarea>`;
  }

  function row(f) {
    const v = data.values[f.key];
    const inline = ['bool', 'int', 'select'].includes(f.kind);
    const rs = f.live ? '' : '<span class="rs">重启生效</span>';
    return `<div class="st-row">${inline
      ? `<label for="st-${f.key}"><span>${esc(f.label)}${rs}</span>${input(f, v)}</label>`
      : `<label for="st-${f.key}"><span>${esc(f.label)}${rs}</span></label>${input(f, v)}`}${f.help ? `<small>${esc(f.help)}</small>` : ''}</div>`;
  }

  function draw(col) {
    const groups = [...new Set(data.fields.map(f => f.group))];
    col.innerHTML = (data.restart.length ? `<div class="st-note">已存、重启远窗后生效：${esc(data.restart.join('、'))}</div>` : '')
      + groups.map(g => `<div class="plate st-group"><h3>${esc(g)}</h3>${data.fields.filter(f => f.group === g).map(row).join('')}</div>`).join('')
      + `<div class="st-bar"><span class="msg" id="stMsg"><span class="st-path">${esc(data.path)}</span></span>
         <button class="pill" id="stReset" hidden>撤销</button><button class="pill" id="stSave" disabled>保存</button></div>`;
    const byKey = Object.fromEntries(data.fields.map(f => [f.key, f]));
    const touched = () => {
      $('#stSave').disabled = false; $('#stReset').hidden = false;
      const m = $('#stMsg'); m.className = 'msg'; m.textContent = `改了 ${Object.keys(dirty).length} 项，还没保存`;
    };
    col.querySelectorAll('[data-k]').forEach(el => {
      const f = byKey[el.dataset.k];
      if (f.kind === 'split') {   // 表里任何一格改了，整张表照现在的样子交上去
        const read = () => { const t = {};
          el.querySelectorAll('[data-s]').forEach(x => { const [kind, k] = x.dataset.s.split('.'); (t[kind] ||= {})[k] = x.value.trim(); });
          return t; };
        el.addEventListener('change', e => { if (e.target.dataset.s) splitPicked(e.target); dirty[f.key] = read(); touched(); });
        el.addEventListener('input', () => { dirty[f.key] = read(); touched(); });
        return;
      }
      const ev = el.type === 'checkbox' || el.tagName === 'SELECT' ? 'change' : 'input';
      el.addEventListener(ev, () => {
        dirty[f.key] = f.kind === 'bool' ? el.checked : f.kind === 'int' ? Number(el.value) : el.value;
        touched();
      });
    });
    $('#stReset').onclick = () => { dirty = {}; draw(col); };
    $('#stSave').onclick = () => save(col);
  }

  async function save(col) {
    const btn = $('#stSave'); btn.disabled = true; btn.textContent = '保存中…';
    let r;
    try { r = await api('admin/prefs', dirty); } catch (e) { r = {ok: false, error: '没连上，再试一次'}; }
    if (!r.ok) {
      btn.disabled = false; btn.textContent = '保存';
      const m = $('#stMsg'); m.className = 'msg bad'; m.textContent = r.error || '没存上';
      return;
    }
    dirty = {};
    toast(r.changed.length ? (r.restart.length ? '已保存，部分设置重启后生效' : '已保存，已生效') : '没有改动');
    await load(col);
  }

  async function load(col) {
    col.innerHTML = '<div class="ac-empty">读取中…</div>';
    try {
      const r = await fetch('/_x/api/admin/prefs');
      if (r.status === 401) { location.href = '/_x/login?next=' + encodeURIComponent('/_x/#admin'); return; }
      if (!r.ok) throw new Error();
      data = await r.json();
    } catch (e) { col.innerHTML = '<div class="ac-empty">没读到设置，稍后再试</div>'; return; }
    draw(col);
  }

  window.drawSettings = col => { dirty = {}; load(col); };
})();
