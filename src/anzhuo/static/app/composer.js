// 文字草稿持久化；照片和 File 原件保留在本页内存，切换对话不会丢。
const composerDrafts = new Map();
let composerView = null;
function composerDraft(key) {
  if (!composerDrafts.has(key)) {
    let text = '';
    if (key !== 'new') try { text = JSON.parse(localStorage.getItem('composerDrafts') || '{}')[key] || ''; } catch (_) {}
    composerDrafts.set(key, {text, images: [], files: [], sending: false, reading: 0, error: '', progress: ''});
  }
  return composerDrafts.get(key);
}
function saveComposerText(key, text) {
  try {
    const saved = JSON.parse(localStorage.getItem('composerDrafts') || '{}');
    // 新对话不跨次保留：每次打开都是空输入框
    if (text && key !== 'new') saved[key] = text; else delete saved[key];
    localStorage.setItem('composerDrafts', JSON.stringify(saved));
  } catch (_) {}
}
// 敲字时草稿停手 0.3 秒才写盘，切走、关页、发送时立即写
let draftTimer = 0, draftWait = null;
function saveComposerSoon(key, text) { draftWait = [key, text]; clearTimeout(draftTimer); draftTimer = setTimeout(flushComposerText, 300); }
function flushComposerText() { clearTimeout(draftTimer); if (draftWait) { const [k, t] = draftWait; draftWait = null; saveComposerText(k, t); } }
addEventListener('pagehide', flushComposerText);
document.addEventListener('visibilitychange', () => { if (document.hidden) flushComposerText(); });
function refreshComposer(key) { if (composerView?.key === key) composerView.paint(); }

function wireComposer(sendFn, onSent, onOpening) {
  const ta = $('#ta'), btn = $('#send'), thumbs = $('#thumbs'), warning = $('#warn');
  const key = String(sel ?? 'new'), state = composerDraft(key), num0 = sel;
  const active = () => ta.isConnected && $('#ta') === ta && String(sel ?? 'new') === key && $('#app').classList.contains('in');
  let fitting = false, thumbSig = '';
  const paint = () => {
    if (!active()) return;
    if (ta.value !== state.text) ta.value = state.text;
    if (!fitting) { fitting = true; requestAnimationFrame(() => { fitting = false; if (!active()) return; ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, Math.max(96, (window.visualViewport ? visualViewport.height : innerHeight) * .3)) + 'px'; }); }
    btn.disabled = state.sending || state.reading > 0 || (!state.text.trim() && !state.images.length && !state.files.length);
    btn.innerHTML = state.sending ? '<i class="spin"></i>' : ICON.up;
    warning.hidden = !(state.error || state.progress);
    warning.textContent = state.error || state.progress;
    warning.classList.toggle('prog', !!state.progress && !state.error);
    const sig = state.images.join('|') + '#' + state.files.map(f => f.name).join('|');
    if (sig === thumbSig) return;
    thumbSig = sig;
    thumbs.hidden = !state.images.length && !state.files.length;
    thumbs.innerHTML = state.images.map((u, i) => `<span class="thumb"><img src="${u}"><button data-i="${i}">×</button></span>`).join('')
      + state.files.map((f, i) => `<span class="thumb chipf">${ICON.doc}<span>${esc(f.name)}</span><button data-f="${i}">×</button></span>`).join('');
    thumbs.querySelectorAll('button[data-i]').forEach(b => b.onclick = () => { state.images.splice(+b.dataset.i, 1); paint(); });
    thumbs.querySelectorAll('button[data-f]').forEach(b => b.onclick = () => { state.files.splice(+b.dataset.f, 1); paint(); });
  };
  composerView = {key, paint};
  // 发送后输入法可能补发一次 input，把旧文字写回草稿；只认当前正在显示的输入框
  const sync = () => { if (!active()) return; state.text = ta.value; saveComposerSoon(key, state.text); paint(); };
  const add = async files => {
    state.reading++; refreshComposer(key);
    try {
      for (const f of [...files]) {
        if (/^image\/(png|jpe?g|gif|webp|heic|heif)$/.test(f.type)) state.images.push(await shrink(f));
        else state.files.push(f);
      }
    } catch (_) { state.error = '照片读取失败，请重新选择该照片'; }
    finally { state.reading--; refreshComposer(key); }
  };
  ta.oninput = sync;
  $('#phoneList').onclick = () => { flushComposerText(); ta.blur(); $('#back')?.click(); };
  $('#phoneKeyboard').onpointerdown = e => e.preventDefault();
  $('#phoneKeyboard').onclick = () => ta.blur();
  ta.setAttribute('aria-label', '消息');
  $('#file').onchange = e => { const files = [...e.target.files]; e.target.value = ''; add(files); };
  ta.onpaste = e => { const fs = [...e.clipboardData.items].filter(i => i.kind === 'file').map(i => i.getAsFile()).filter(Boolean); if (fs.length) { e.preventDefault(); add(fs); } };
  const box = ta.closest('.composer'); let depth = 0;
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
  document.ondragenter = e => { if (!hasFiles(e)) return; e.preventDefault(); depth++; box.classList.add('drop'); };
  document.ondragover = e => { if (hasFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } };
  document.ondragleave = e => { if (hasFiles(e) && --depth <= 0) { depth = 0; box.classList.remove('drop'); } };
  document.ondrop = e => { if (!hasFiles(e)) return; e.preventDefault(); depth = 0; box.classList.remove('drop'); if (e.dataTransfer.files.length) add([...e.dataTransfer.files]); };
  const phone = coarsePhone();
  ta.setAttribute('enterkeyhint', phone || document.documentElement.classList.contains('ipad') ? 'enter' : 'send');
  ta.onkeydown = e => {
    if (e.key === 'ArrowUp' && !ta.value && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey && !e.isComposing && lastYou[num0]) {   // 输入框空着按 ↑：调出上一句，改一下重发
      e.preventDefault(); ta.value = lastYou[num0]; sync(); ta.setSelectionRange(ta.value.length, ta.value.length); return;
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.altKey && !e.isComposing && e.keyCode !== 229 && (!phone || e.metaKey || e.ctrlKey) && (!document.documentElement.classList.contains('command-send') || e.metaKey || e.ctrlKey)) { e.preventDefault(); go(); }
  };
  async function go() {
    const originalText = state.text, text = originalText.trim();
    if ((!text && !state.images.length && !state.files.length) || state.sending || state.reading) return;
    const snapshot = {images: state.images.slice(), files: state.files.slice()};
    state.sending = true; state.error = ''; state.progress = ''; paint();
    const shown = text || (snapshot.files.length ? '[附件]' : '[图片]');
    const item = onSent ? onSent(shown, snapshot.images) : null;
    if (onSent) { lastKey = ''; loadThread(); } else if (onOpening && active()) onOpening(shown, snapshot.images);
    const undo = () => {
      if (num0 && pending[num0]) pending[num0] = pending[num0].filter(p => p !== item);
      if (onOpening && active()) onOpening(null);
      lastKey = ''; loadThread();
    };
    try {
      const mb = n => (n / 1048576).toFixed(1);
      const r = await sendFn(text, (a, b) => {
        state.progress = a >= b ? '传完了，正在交给远窗…' : `正在上传 ${mb(a)} / ${mb(b)} MB · ${Math.floor(a / b * 100)}%`;
        refreshComposer(key);
        if (active()) { const op = document.querySelector('#opening .lsub'); if (op) op.textContent = a >= b ? '已送到' : state.progress; }
      }, snapshot);
      if (!r.ok) { undo(); state.error = r.error || '没发出去，再试一次'; }
      else {
        if (r.scheduled) { undo(); toast('定时任务已安排 · ' + scheduleDate(r.scheduled.next_at)); }
        if (item) item.acked = true;
        // 只清除这次发送的内容；发送中输入的新草稿、另一对话的内容都保留。
        if (state.text === originalText) state.text = '';
        state.images = state.images.filter(x => !snapshot.images.includes(x));
        state.files = state.files.filter(x => !snapshot.files.includes(x));
        draftWait = null; saveComposerText(key, state.text);
        if (active()) ta.value = state.text;
        if (r.num) {
          if (key === 'new' && !state.text && !state.images.length && !state.files.length) composerDrafts.delete(key);
          state.sending = false; state.progress = ''; refreshComposer(key);
          await loadSessions();
          if (active()) openSession(r.num); else toast('新对话已创建', '查看', () => openSession(r.num));
          return;
        }
      }
    } catch (e) {
      undo();
      if (e.message !== 'login') state.error = e.message === 'big' ? '服务器拒绝了这次上传，请稍后再试' : e.message === 'offline' ? '连接中断，附件已保留，请重试' : e.message || '没发出去，再试一次';
    } finally {
      state.sending = false; state.progress = ''; refreshComposer(key);
      if (active() && !coarsePhone() && !document.documentElement.classList.contains('ipad')) ta.focus();
      lastKey = ''; loadThread(); loadSessions();
    }
  }
  btn.onclick = go; paint();
  if (active() && !coarsePhone() && !document.documentElement.classList.contains('ipad')) ta.focus({preventScroll: true});
}

const WHERE_ICON = {
  auto: '<svg viewBox="0 0 24 24"><path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  chat: '<svg viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.4A8 8 0 1 1 21 12z"/></svg>',
  desk: '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
};
const FOLDER_SAY = {};  // 设置里的文件夹短名 → [图标, 说人话]：列了的才放进“在哪做”菜单（扩展往里加）
function agoDays(t) {
  const d = Math.floor((Date.now() / 1000 - t) / 86400);
  return d < 1 ? '今天动过' : d < 2 ? '昨天动过' : `${d} 天前动过`;
}
// 菜单按可见区域定位，避开标题栏和键盘；固定定位避免被新对话的滚动容器裁切。
function placeWhereMenu() {
  const menu = document.querySelector('.wmenu'), button = $('#wp');
  if (!menu || !button) return;
  const vv = window.visualViewport, main = $('#main').getBoundingClientRect();
  const head = document.querySelector('#main .head').getBoundingClientRect();
  const top = Math.max(head.bottom, vv ? vv.offsetTop : 0) + 8;
  const bottom = Math.min(main.bottom, vv ? vv.offsetTop + vv.height : innerHeight) - 8;
  const left = Math.max(main.left, vv ? vv.offsetLeft : 0) + 8;
  const right = Math.min(main.right, vv ? vv.offsetLeft + vv.width : innerWidth) - 8;
  const anchor = button.getBoundingClientRect();
  const above = Math.max(0, anchor.top - 6 - top), below = Math.max(0, bottom - anchor.bottom - 6);
  const down = below >= Math.min(menu.scrollHeight, 420) || below > above;
  const room = down ? below : above;
  const width = Math.max(0, Math.min(340, right - left));
  menu.style.width = width + 'px';
  menu.style.maxHeight = Math.max(0, Math.min(420, room)) + 'px';
  menu.style.left = Math.max(left, Math.min(anchor.left, right - width)) + 'px';
  menu.style.top = (down ? Math.max(top, anchor.bottom + 6) : Math.max(top, anchor.top - 6 - menu.getBoundingClientRect().height)) + 'px';
}
addEventListener('resize', placeWhereMenu);
if (window.visualViewport) {
  visualViewport.addEventListener('resize', placeWhereMenu);
  visualViewport.addEventListener('scroll', placeWhereMenu);
}

// 文件夹浏览：从 ~/Projects 开始一层层点进去，选定的文件夹加进“在哪做”（来自开源版 GitHub #1）
// 盘的再上一层是“此电脑”（Windows 所有盘符，没有共同的文件系统父级）
function browseSheet(start, done) {
  let at = /^([A-Za-z]:[\\/]|\/|~)/.test(start || '') ? start : '', up = '', show = '';
  const el = sheet(`<div class="bhead"><b>选择文件夹</b><button class="bx" id="bx" aria-label="关闭">✕</button></div>
    <div class="bpath" id="bpath"></div><div class="blist" id="blist"><div class="bempty">读取中…</div></div>
    <div class="bbtns"><button class="pill" id="bup">← 上级</button><button class="pill on" id="bok">用这个文件夹</button></div>`);
  el.querySelector('.box').classList.add('browse');
  const draw = async () => {
    try {
      const d = await api('browse' + (at ? '?path=' + encodeURIComponent(at) : ''));
      if (!d.dirs) throw new Error(d.error);
      at = d.raw; up = d.parent || (d.drive_root ? '__drives__' : ''); show = d.show;
      $('#bpath').textContent = d.show;
      $('#bup').style.visibility = up ? 'visible' : 'hidden';
      $('#bok').style.display = d.drive_level ? 'none' : '';
      $('#blist').innerHTML = d.dirs.map(n => `<button class="bo" data-n="${esc(n)}" ${d.absolute ? 'data-abs="1"' : ''}>${WHERE_ICON.folder}<span class="tx"><b>${esc(n)}</b></span></button>`).join('')
        || '<div class="bempty">这里没有子文件夹了</div>';
      document.querySelectorAll('#blist .bo').forEach(b => b.onclick = () => {
        at = b.dataset.abs ? b.dataset.n : /[\\/]$/.test(at) ? at + b.dataset.n : at + (at.includes('\\') ? '\\' : '/') + b.dataset.n;
        draw();
      });
    } catch (e) { $('#blist').innerHTML = '<div class="bempty">读不到这个文件夹，换个地方试试</div>'; }
  };
  $('#bx').onclick = () => el.remove();
  $('#bup').onclick = () => { if (up) { at = up; draw(); } };
  $('#bok').onclick = () => { el.remove(); done(at, show); };
  draw();
}

// 电脑端对话：你在电脑上（终端 / IDE / 桌面端）开的 Claude Code 对话，不分文件夹按时间列，点一下接进来接着聊；接过的回到原来那个
// 只写文件夹名；Claude 桌面端自己的临时工作区（一长串编号）写成“Claude 桌面端”
const deskDir = p => /[\\/]Claude[\\/]scratch-workspaces[\\/]/.test(p || '') ? 'Claude 桌面端' : (p || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop() || p;
function deskSheet() {
  const el = sheet(`<div class="bhead"><b>电脑端对话</b><button class="bx" id="bx" aria-label="关闭">✕</button></div>
    <div class="blist" id="blist"><div class="bempty">读取中…</div></div>`);
  el.querySelector('.box').classList.add('browse');
  $('#bx').onclick = () => el.remove();
  const load = async () => {
    $('#blist').innerHTML = '<div class="bempty">读取中…</div>';
    try {
      const d = await api('cc-sessions');
      if (d.error) throw new Error(d.error);
      if (!d.sessions.length) { $('#blist').innerHTML = '<div class="bempty">电脑上还没有 Claude Code 对话</div>'; return; }
      $('#blist').innerHTML = d.sessions.map(s => `<button class="bo" data-sid="${esc(s.id)}" data-cwd="${esc(s.cwd)}" data-name="${esc(s.title.slice(0, 16))}">${WHERE_ICON.desk}<span class="tx"><b>${esc(s.title)}</b><small>${s.num ? '已在远窗 · ' : ''}${esc(deskDir(s.cwd))} · ${esc(ago(s.at / 1000))}</small></span></button>`).join('');
      document.querySelectorAll('#blist .bo[data-sid]').forEach(b => b.onclick = () => take(b.dataset, false));
    } catch (e) { $('#blist').innerHTML = '<div class="bempty">读不到对话记录</div>'; }
  };
  // 记录几分钟内还在被写：电脑上那个窗口可能还开着，两边接着用会各说各的，先问一句
  const take = async (x, force) => {
    const r = await api('resume-cc', {path: x.cwd, session_id: x.sid, name: x.name, force}).catch(() => ({error: '没接上，再试一次'}));
    if (r.warn) {
      $('#blist').innerHTML = `<div class="dwarn"><b>电脑上可能还开着</b><p>这段对话${r.ago < 60 ? '刚刚' : ` ${Math.floor(r.ago / 60)} 分钟前`}还在电脑上写过。两边同时用会各说各的，先把电脑上那个窗口关掉再接。</p>
        <div class="bbtns"><button class="pill" id="dno">返回</button><button class="pill on" id="dyes">已关掉，接上</button></div></div>`;
      $('#dno').onclick = () => load(); $('#dyes').onclick = () => take(x, true); return;
    }
    if (r.error || !r.num) { toast(r.error || '没接上，再试一次'); return; }
    el.remove(); openSession(r.num); loadSessions();
  };
  load();
}

