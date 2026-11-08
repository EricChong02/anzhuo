function composerHTML(ph) {
  return `<div class="dock"><div style="text-align:center"><button class="jump" id="jump">${ICON.down}最新</button></div><div class="quick" id="quick" hidden></div><div class="composer"><div class="relay" id="relay" hidden></div><div class="hint" id="hint" hidden></div><div class="cwarn" id="warn" hidden></div><div class="thumbs" id="thumbs" hidden></div>
    <textarea id="ta" rows="1" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="${esc(ph)}"></textarea>
    <div class="row"><button class="icon-btn phone-list" id="phoneList" title="返回对话列表" aria-label="返回对话列表">${ICON.back}</button><label class="icon-btn" title="图片或文件" aria-label="添加图片或文件">${ICON.clip}<input type="file" multiple id="file" hidden></label>
    <button class="icon-btn phone-keyboard" id="phoneKeyboard" title="收起键盘" aria-label="收起键盘"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="12" rx="2"/><path d="M7 7h1m3 0h1m3 0h1M7 11h10m-8 8 3 3 3-3"/></svg></button><span class="gap"></span><span class="mpw" id="mpw"></span><button class="send" id="send" disabled title="发送">${ICON.up}</button></div></div></div>`;
}

// 选模型和思考深度：和 Claude 一样放在输入框里、发送键左边。新对话多一个“自动”：按管理 · 设置里的分工表挑（定方案、开发、简单任务各交给谁）
let modelOpts = [['opus', 'Claude Opus'], ['sonnet', 'Claude Sonnet'], ['haiku', 'Claude Haiku'], ['fable', 'Claude Fable'], ['gpt', 'GPT']];
const EFFORT_OPTS = [['low', '低'], ['medium', '中'], ['high', '高'], ['xhigh', '很高'], ['max', '最高']];
let mpOpen = false, mpRedraw = null;
// 模型按钮只写型号和深度（“Opus 5.5 · 高”）；菜单按 Claude / GPT 分组，哪家额度快用完了就在组名旁标出来。
// 新对话：自动 + 两家都能选。对话里：只能在同一家里换（Claude 开的不能换 GPT，反之亦然），换了从下一句起生效。
function renderModel(st, onPick, isNew) {
  const el = $('#mpw'); if (!el) return;
  mpRedraw = () => renderModel(st, onPick, isNew);
  const opt = k => (modelOpts.find(m => m[0] === k) || [, ''])[1];
  const label = k => k === 'auto' ? '自动' : (k === st.model && st.label ? st.label : opt(k) || k || '默认').replace(/^Claude /, '');
  const hot = ls => { const h = ls.filter(l => l.pct >= 80).sort((a, b) => b.pct - a.pct)[0]; return h ? `<span class="mh">${esc(h.name)}已用 ${h.pct}%</span>` : ''; };
  const ok = '<svg class="ok" viewBox="0 0 24 24"><path d="m5 12 5 5 9-10" stroke="currentColor" fill="none" stroke-width="2.2"/></svg>';
  const row = k => `<button class="mo ${k === st.model ? 'on' : ''}" data-m="${esc(k)}"><span class="tx"><b>${esc(k === 'auto' ? '自动 · 按分工表' : label(k))}</b></span>${ok}</button>`;
  // 别家助手（走 ACP 的 Kimi 等）：每家一组、一行；开对话时选，对话里不换
  const alt = modelOpts.map(m => m[0]).filter(k => !['opus', 'sonnet', 'haiku', 'fable', 'gpt'].includes(k));
  const altRows = ks => ks.length ? `<div class="lb">其他助手</div>${ks.map(row).join('')}` : '';
  const claude = modelOpts.map(m => m[0]).filter(k => k !== 'gpt' && !alt.includes(k));
  if (!isNew && st.model && st.model !== 'gpt' && !alt.includes(st.model) && !claude.includes(st.model)) claude.push(st.model);
  const gptChat = !isNew && st.model === 'gpt', altChat = !isNew && alt.includes(st.model);
  const list = isNew ? row('auto') + `<div class="lb">Claude${hot(limits)}</div>${claude.map(row).join('')}<div class="lb">GPT${hot(limitsGpt)}</div>${row('gpt')}` + altRows(alt)
    : gptChat ? `<div class="lb">GPT${hot(limitsGpt)}</div>${row('gpt')}`
    : altChat ? altRows([st.model])
    : `<div class="lb">Claude${hot(limits)}</div>${claude.map(row).join('')}`;
  const ename = (EFFORT_OPTS.find(e => e[0] === st.effort) || [, st.effort === '' ? '默认' : '中'])[1];
  // 新对话“自动”又没动深度：模型和深度都照分工表，按钮上只写“自动”
  const head = st.model === 'auto' && !st.effort ? '自动' : `${label(st.model)} · ${ename}`;
  el.innerHTML = `<button class="mp ${mpOpen ? 'open' : ''}" id="mp" title="模型和思考深度">${esc(head)}${ICON.chev}</button>`
    + (mpOpen ? `<div class="mmenu"><button class="phone-menu-close" id="modelDone" aria-label="收起模型菜单">收起</button>${list}<div class="lb">思考深度</div><div class="seg">${EFFORT_OPTS.map(([k, t]) => `<button class="${k === st.effort ? 'on' : ''}" data-e="${k}">${t}</button>`).join('')}</div></div>` : '');
  fitModelMenu(el.querySelector('.mmenu'));
  if ($('#modelDone')) $('#modelDone').onclick = e => { e.stopPropagation(); mpOpen = false; renderModel(st, onPick, isNew); };
  $('#mp').onclick = e => { e.stopPropagation(); mpOpen = !mpOpen; renderModel(st, onPick, isNew); };
  el.querySelectorAll('[data-m],[data-e]').forEach(b => b.onclick = e => {
    e.stopPropagation();
    const was = st.model + st.effort;
    if (b.dataset.m) { st.model = b.dataset.m; delete st.label; mpOpen = false; } else st.effort = b.dataset.e;
    onPick(st); renderModel(st, onPick, isNew);
    if (!isNew && was !== st.model + st.effort) toast(`下一句起用 ${label(st.model)} · ${(EFFORT_OPTS.find(x => x[0] === st.effort) || [, ''])[1]}`);
  });
}
// 菜单跟着按钮弹出时，朝空间大的一侧展开，高度不超过那一侧的可见空间（窗口矮、新对话页按钮靠下都不再超框）
function fitModelMenu(m) {
  if (!m || getComputedStyle(m).position !== 'absolute') return;
  const r = m.parentElement.getBoundingClientRect(), vh = window.visualViewport ? visualViewport.height : innerHeight;
  const below = vh - r.bottom - 16, above = r.top - 16, need = m.scrollHeight, down = below >= need || below > above;
  Object.assign(m.style, down ? {top: 'calc(100% + 10px)', bottom: 'auto'} : {top: 'auto', bottom: 'calc(100% + 12px)'});
  m.style.maxHeight = Math.max(160, (down ? below : above) - 10) + 'px';
}
document.addEventListener('click', e => { if (mpOpen && !e.target.closest('.mpw')) { mpOpen = false; mpRedraw && mpRedraw(); } });

function headStatus() {
  const s = sessions.find(x => x.num === sel), el = $('#hst');
  if (!s || !el) return;
  el.className = s.status; el.textContent = s.status === 'idle' ? s.cwd.split('/').filter(Boolean).pop() || s.cwd : statusText(s);
}

