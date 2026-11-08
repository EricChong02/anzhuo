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
.st-row input[type=number]{width:80px} .st-row select{max-width:60%}
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

  function input(f, v) {
    const id = 'st-' + f.key;
    if (f.kind === 'bool') return `<input type="checkbox" id="${id}" data-k="${f.key}" ${v ? 'checked' : ''}>`;
    if (f.kind === 'int') return `<input type="number" min="1" max="10" id="${id}" data-k="${f.key}" value="${esc(v)}">`;
    if (f.kind === 'select') return `<select id="${id}" data-k="${f.key}">${f.options.map(([o, t]) =>
      `<option value="${esc(o)}" ${String(o) === String(v ?? '') ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>`;
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
    col.querySelectorAll('[data-k]').forEach(el => {
      const ev = el.type === 'checkbox' || el.tagName === 'SELECT' ? 'change' : 'input';
      el.addEventListener(ev, () => {
        const f = byKey[el.dataset.k];
        dirty[f.key] = f.kind === 'bool' ? el.checked : f.kind === 'int' ? Number(el.value) : el.value;
        $('#stSave').disabled = false; $('#stReset').hidden = false;
        const m = $('#stMsg'); m.className = 'msg'; m.textContent = `改了 ${Object.keys(dirty).length} 项，还没保存`;
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
