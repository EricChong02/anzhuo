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
    const t = $('#ta'); if (t && t.oninput) t.oninput();   // 键盘弹起后看得见的区域变小，输入框高度跟着收
  };
  vv.addEventListener('resize', fit); vv.addEventListener('scroll', fit); fit();
  addEventListener('focusout', () => setTimeout(fit, 60));
}

Object.assign(XE.lib, {$, esc, ICON, api, sheet, toast, openSession, openNew, ago, agoDays, WHERE_ICON, FOLDER_SAY});
// 等扩展的脚本（排在这个脚本后面）都跑完再开始，它们才订阅得上第一次拿列表
const boot = async () => {
  await loadSessions();
  const m = location.hash.match(/^#s(\d+)/);
  if (await openFromNotice()) {} else if (m) { history.replaceState(null, '', location.pathname); openSession(+m[1]); } else if (location.hash === '#new') { history.replaceState(null, '', location.pathname); openNew(); }
  else if (location.hash === '#admin') { history.replaceState(null, '', location.pathname); openAdmin(); }
  // 实时推送连着时只偶尔兜底刷新；断了（换网络、锁屏回来）退回每几秒问一次
  connectEvents();
  setInterval(() => { if (!document.hidden && (!esOk || Date.now() - lastPoll > 20000)) { lastPoll = Date.now(); loadSessions(); } }, 4000);
  setInterval(() => { if (!document.hidden && !esOk) loadThread(); }, 3000);
  setInterval(() => { if (!document.hidden && $('#adm')) loadAdmin(); }, 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) lastAct = Date.now(); presence(!document.hidden); if (!document.hidden) { connectEvents(); loadSessions(); loadThread(); openFromNotice(); } });
  // 键盘：⌘K 搜对话，⌘⇧O / ⌘N 新对话，⌘↑ ⌘↓ 上一个 / 下一个对话，Esc 回列表（手机上没键盘就用不到）
  addEventListener('keydown', e => {
    if (!e.metaKey && !e.ctrlKey && !e.altKey && !e.isComposing && e.key.length === 1 && !coarsePhone()) {   // 焦点不在输入框时直接打字：落进输入框
      const ta = $('#ta'), t = e.target;
      if (ta && t !== ta && !(t.closest && t.closest('input,textarea,select,[contenteditable],button,a,.modal,[role=dialog]')) && !document.querySelector('.modal,[role=dialog]')) ta.focus({preventScroll: true});
      return;
    }
    if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === 'k') { e.preventDefault(); if ($('#app').classList.contains('in') && matchMedia('(max-width:820px)').matches) goList(); $('#q').focus(); $('#q').select(); }
    else if (k === 'n' || (k === 'o' && e.shiftKey)) { e.preventDefault(); openNew(); }
    else if (k === 'arrowup' || k === 'arrowdown') {
      const nums = [...document.querySelectorAll('#list .item[data-n]')].map(b => +b.dataset.n); if (!nums.length) return;
      e.preventDefault(); const i = nums.indexOf(sel), j = i < 0 ? 0 : Math.max(0, Math.min(nums.length - 1, i + (k === 'arrowup' ? -1 : 1)));
      if (nums[j] !== sel) openSession(nums[j]);
    }
  });
};
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
