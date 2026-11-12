// 屏幕键盘（或妙控键盘的悬浮快捷条）挡住底部时：页面保持满屏，只把输入条往上挪被挡住的那么多；
// 整页不滚，Safari 为露出输入框滚走的那段拉回来（不然会卡在半截、下面一片空白）
if (window.visualViewport) {
  const vv = visualViewport;
  const fit = () => {
    if (scrollX || scrollY) scrollTo(0, 0);
    // 用布局高度（clientHeight）算：iPhone 的 Safari 里 innerHeight 会跟着键盘一起变矮，拿它算会得 0
    const full = document.documentElement.clientHeight || innerHeight;
    const kb = Math.max(0, Math.round(full - vv.height - vv.offsetTop));
    document.documentElement.style.setProperty('--kb', kb + 'px');
    document.documentElement.style.setProperty('--visible-height', vv.height + 'px');
    document.documentElement.classList.toggle('keyboard-open', kb > 100);
    const t = $('#ta'); if (t && t.oninput) t.oninput();   // 键盘弹起后看得见的区域变小，输入框高度跟着收
  };
  vv.addEventListener('resize', fit); vv.addEventListener('scroll', fit); fit();
  addEventListener('focusout', () => setTimeout(fit, 60));
}

Object.assign(XE.lib, {$, esc, ICON, api, sheet, toast, openSession, openNew, ago, agoDays, WHERE_ICON, FOLDER_SAY});
// 从快捷指令唤醒主屏幕 App 时，页面可能没有重载；新建指令也要在恢复前台时识别。
let appReady = false;
function openShortcutNew() {
  if (!appReady || location.hash !== '#new' || document.querySelector('#main .newv')) return false;
  openNew();
  return true;
}
addEventListener('hashchange', openShortcutNew);
addEventListener('pageshow', openShortcutNew);

// 等扩展的脚本（排在这个脚本后面）都跑完再开始，它们才订阅得上第一次拿列表
const boot = async () => {
  await loadSessions();
  appReady = true;
  const m = location.hash.match(/^#s(\d+)/);
  if (await openFromNotice()) {} else if (m) { history.replaceState(null, '', location.pathname); openSession(+m[1]); } else if (location.hash === '#new') { history.replaceState(null, '', location.pathname); openNew(); }
  else if (location.hash === '#admin') { history.replaceState(null, '', location.pathname); openAdmin(); }
  // 实时推送连着时只偶尔兜底刷新；断了（换网络、锁屏回来）退回每几秒问一次
  connectEvents();
  setInterval(() => { if (!document.hidden && (!esOk || Date.now() - lastPoll > 20000)) { lastPoll = Date.now(); loadSessions(); } }, 4000);
  setInterval(() => { if (!document.hidden && !esOk) loadThread(); }, 3000);
  setInterval(() => { if (!document.hidden && $('#adm')) loadAdmin(); }, 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) lastAct = Date.now(); presence(!document.hidden); if (!document.hidden) { connectEvents(); loadSessions(); if (!openShortcutNew()) { loadThread(); openFromNotice(); } } });
  // 键盘：⌘K 搜对话，⌘⇧O / ⌘N 新对话，⌘↑ ⌘↓ 上一个 / 下一个对话，Esc 回列表（手机上没键盘就用不到）
  addEventListener('keydown', e => {
    if (!e.metaKey && !e.ctrlKey && !e.altKey && !e.isComposing && !e.defaultPrevented && e.key.length === 1 && !coarsePhone()) {   // 焦点不在输入框时直接打字：落进输入框
      const ta = $('#ta'), t = e.target;
      if (ta && t !== ta && !(t.closest && t.closest('input,textarea,select,[contenteditable],button,a,.sheet,.amenu,.lightbox,.modal,[role=dialog]')) && !document.querySelector('.sheet,.amenu,.lightbox,.modal,[role=dialog]')) ta.focus({preventScroll: true});
      return;
    }
    if (!(e.metaKey || e.ctrlKey) || e.altKey || e.isComposing || e.defaultPrevented || document.querySelector('.sheet,.amenu,.lightbox,[role=dialog]')) return;
    const k = e.key.toLowerCase();
    if (k === 'k') { e.preventDefault(); if ($('#app').classList.contains('in') && (matchMedia('(max-width:820px)').matches || (document.documentElement.classList.contains('ipad') && innerWidth < 1000))) goList(); $('#q').focus(); $('#q').select(); }
    else if (k === 'n' || (k === 'o' && e.shiftKey)) { e.preventDefault(); openNew(); }
    else if (k === 'arrowup' || k === 'arrowdown') {
      if (e.target.closest('input,textarea,select,[contenteditable]')) return;
      const nums = [...document.querySelectorAll('#list .item[data-n]')].map(b => +b.dataset.n); if (!nums.length) return;
      e.preventDefault(); const i = nums.indexOf(sel), j = i < 0 ? 0 : Math.max(0, Math.min(nums.length - 1, i + (k === 'arrowup' ? -1 : 1)));
      if (nums[j] !== sel) openSession(nums[j]);
    }
  });
};
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();

// 桌面工作区偏好只保存在当前浏览器。
(() => {
  const root = document.documentElement, app = $('#app');
  const tablet = root.classList.contains('ipad');
  const prefPrefix = tablet ? 'ipad.' : 'desktop.';
  const desktop = () => tablet || matchMedia('(min-width:821px) and (pointer:fine)').matches && !root.classList.contains('ipad');
  const read = k => { try { return localStorage.getItem(prefPrefix + k); } catch (_) { return null; } };
  const save = (k, v) => { try { localStorage.setItem(prefPrefix + k, v); } catch (_) {} };
  root.classList.toggle('command-send', tablet ? read('commandSend') !== 'false' : read('commandSend') === 'true');
  if (tablet) root.style.setProperty('--sidebar-width', '280px');
  const width = Number(read('sidebar'));
  if (width >= 240 && width <= 420) root.style.setProperty('--sidebar-width', width + 'px');
  const bar = document.createElement('div'); bar.className = 'desktop-tools';
  bar.innerHTML = '<button id="workspaceToggle" title="收起侧栏（⌘⇧L）" aria-label="收起侧栏" aria-expanded="true">☰</button><button id="workspaceHelp" title="桌面使用设置与快捷键" aria-label="桌面使用设置与快捷键">⌘</button>';
  app.appendChild(bar);
  const toggle = () => {
    if (tablet && innerWidth < 1000) { if ($('#app').classList.contains('in')) $('#back')?.click(); return; }
    const hidden = app.classList.toggle('sidebar-hidden'), b = $('#workspaceToggle');
    b.setAttribute('aria-expanded', String(!hidden));
    b.title = (hidden ? '展开侧栏' : '收起侧栏') + '（⌘⇧L）'; b.setAttribute('aria-label', b.title);
  };
  $('#workspaceToggle').onclick = toggle;
  const help = () => {
    if (document.querySelector('#desktopDialog')) return;
    const previous = document.activeElement;
    const el = document.createElement('div'); el.className = 'sheet desktop-dialog'; el.id = 'desktopDialog';
    el.setAttribute('role', 'dialog'); el.setAttribute('aria-modal', 'true'); el.setAttribute('aria-labelledby', 'desktopTitle');
    el.innerHTML = `<div class="box"><h2 id="desktopTitle">${tablet ? 'iPad 使用' : '桌面使用'}</h2><p>拖动侧栏右边缘调整宽度，双击恢复。文件和截图可拖进对话，截图也可直接粘贴。</p><label class="send-preference"><input type="checkbox" ${root.classList.contains('command-send') ? 'checked' : ''}>用 ⌘Enter 发送，Enter 换行</label><p>${tablet ? 'iPad 默认 Enter 换行，⌘Enter 发送；可在这里切换。窄窗口返回列表可保留草稿。' : '默认 Enter 发送，Shift Enter 换行。'}此设置只保存在当前浏览器。</p><dl><dt>搜索对话</dt><dd>⌘K</dd><dt>新建对话</dt><dd>⌘⇧O</dd><dt>收起 / 展开侧栏</dt><dd>⌘⇧L</dd><dt>上 / 下一个对话</dt><dd>⌘↑ / ⌘↓（输入框外）</dd><dt>回到输入框</dt><dd>⌘⇧I</dd><dt>桌面使用帮助</dt><dd>⌘/</dd></dl><button>关闭</button></div>`;
    document.body.appendChild(el);
    const close = () => { el.remove(); if (previous?.isConnected) previous.focus(); };
    el.querySelector('input').onchange = e => { root.classList.toggle('command-send', e.target.checked); save('commandSend', e.target.checked); };
    el.querySelector('button').onclick = close;
    el.onclick = e => { if (e.target === el) close(); };
    el.onkeydown = e => {
      if (e.key === 'Escape') { e.preventDefault(); close(); }
      if (e.key === 'Tab') {
        const first = el.querySelector('input'), last = el.querySelector('button');
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    el.querySelector('input').focus();
  };
  $('#workspaceHelp').onclick = help;
  if (tablet) { $('#workspaceHelp').title = 'iPad 使用与键盘设置'; $('#workspaceHelp').setAttribute('aria-label', 'iPad 使用与键盘设置'); }
  const grip = document.createElement('div'); grip.className = 'sidebar-grip'; grip.tabIndex = 0;
  grip.setAttribute('role', 'separator'); grip.setAttribute('aria-label', '调整侧栏宽度'); grip.setAttribute('aria-orientation', 'vertical');
  grip.setAttribute('aria-valuemin', '240'); grip.setAttribute('aria-valuemax', '420'); app.appendChild(grip);
  const resize = v => { const w = Math.max(240, Math.min(420, v)); root.style.setProperty('--sidebar-width', w + 'px'); grip.setAttribute('aria-valuenow', w); save('sidebar', w); };
  grip.setAttribute('aria-valuenow', width >= 240 && width <= 420 ? width : (tablet ? 280 : 320));
  grip.onpointerdown = e => { if (e.button !== 0) return; e.preventDefault(); grip.setPointerCapture(e.pointerId); app.classList.add('sidebar-resizing'); };
  grip.onpointermove = e => { if (grip.hasPointerCapture(e.pointerId)) resize(e.clientX); };
  const stop = () => app.classList.remove('sidebar-resizing');
  grip.onpointerup = stop; grip.onlostpointercapture = stop; grip.ondblclick = () => resize(tablet ? 280 : 320);
  grip.onkeydown = e => { if (['ArrowLeft', 'ArrowRight'].includes(e.key)) { e.preventDefault(); resize(parseInt(getComputedStyle(root).getPropertyValue('--sidebar-width') || '320') + (e.key === 'ArrowLeft' ? -16 : 16)); } };
  addEventListener('keydown', e => {
    if (!desktop() || e.isComposing || !(e.metaKey || e.ctrlKey) || e.altKey || document.querySelector('.sheet,.amenu,.lightbox,[role=dialog]')) return;
    const k = e.key.toLowerCase();
    if (k === '/' || (e.shiftKey && ['l', 'i'].includes(k))) {
      e.preventDefault(); if (k === '/') help(); else if (k === 'l') toggle(); else $('#ta')?.focus();
    } else if (k === 'k' && app.classList.contains('sidebar-hidden')) toggle();
  }, true);
  // 输入条变高后仍能把末条回复滚到它的上方。
  let observed = null;
  const ro = new ResizeObserver(entries => {
    const feed = $('#feed');
    if (feed) { feed.style.paddingBottom = Math.ceil($('#main').getBoundingClientRect().bottom - entries[0].target.getBoundingClientRect().top + 24) + 'px'; if (stick) feed.scrollTop = feed.scrollHeight; }
  });
  const observe = () => {
    const dock = $('#main .dock'); if (observed === dock) return;
    ro.disconnect(); observed = dock; if (dock) ro.observe(dock);
    const feed = $('#feed'); if (feed) { feed.tabIndex = 0; feed.setAttribute('aria-label', '对话内容'); }
  };
  new MutationObserver(observe).observe($('#main'), {childList: true}); observe();
  const refit = () => { if (observed) { ro.unobserve(observed); ro.observe(observed); } };
  addEventListener('resize', refit);
  window.visualViewport?.addEventListener('resize', refit);
})();

// 切换 WiFi / 蜂窝与从系统返回时，立即重建实时连接；不自动重发消息。
function resumeConnection() {
  if (document.hidden) return;
  if (es) es.close(); es = null; esOk = false;
  connectEvents(); loadSessions(); loadThread();
}
addEventListener('online', resumeConnection);
addEventListener('offline', () => setOffline(true));
addEventListener('pageshow', e => { if (e.persisted) resumeConnection(); });
