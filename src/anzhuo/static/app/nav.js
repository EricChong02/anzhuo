// 历史只留一层：列表 → 某个对话。已经在对话（或新对话页）里再切到别的，就替换当前这一层，返回一次就回到列表
function navTo(state, hash) {
  const st = history.state;
  if (st && (st.s || st.n || st.a)) history.replaceState(state, '', hash); else history.pushState(state, '', hash);
}
function followRelay(num) {
  const feed = $('#feed'), ta = $('#ta');
  const view = feed ? {top: feed.scrollTop, stick, html: $('#col').innerHTML, draft: ta?.value || ''} : null;
  flushComposerText();
  const draft = composerDraft(String(sel));
  if (draft.text || draft.images.length || draft.files.length) {
    composerDrafts.set(String(num), draft);
    saveComposerText(String(num), draft.text);
  }
  relayWait = 0;
  openSession(num, false, view);
}
function openSession(num, fromPop, view) {
  partialText = ''; setTimeout(connectEvents);
  if (sel && sel !== num) presence(false);
  sel = num; lastKey = ''; presence(true);
  if (!fromPop) navTo({s: num}, '#s' + num);
  $('#app').classList.add('in');
  const s = sessions.find(x => x.num === num) || {name: '#' + num, cwd: ''};
  $('#main').innerHTML = `<div class="head"><button class="icon-btn back" id="back">${ICON.back}</button>
      <div class="ti"><b class="wt"><span class="tt">${esc(s.name)}</span></b><span id="hst"></span></div><span id="usageBox"></span><span id="headActs"></span></div>
    <div class="feed" id="feed"><div class="col" id="col"><div class="note">读取中…</div></div></div>
    ${composerHTML('说点什么…')}`;
  $('#back').onclick = () => history.state && history.state.s ? history.back() : goList();
  const feed = $('#feed');
  // 刚点进来：一直贴着最下面（图片、在做卡片后加载撑高也跟着到底），直到你自己往上翻
  stick = view ? view.stick : true;
  if (view) { $('#col').innerHTML = view.html; feed.scrollTop = view.top; }
  const toEnd = () => { if (stick) feed.scrollTop = feed.scrollHeight; };
  new ResizeObserver(toEnd).observe($('#col'));
  feed.addEventListener('load', toEnd, true);   // 图片加载完
  const unstick = () => { stick = false; };
  feed.addEventListener('touchmove', unstick, {passive: true}); feed.addEventListener('wheel', e => { if (e.deltaY < 0) unstick(); }, {passive: true});
  const resumeAtEnd = () => { if (feed.querySelector('.plist[data-reading="1"]')) return; if (feed.scrollHeight - feed.scrollTop - feed.clientHeight < 4) stick = true; };
  feed.addEventListener('touchend', () => setTimeout(resumeAtEnd, 0), {passive: true});
  feed.addEventListener('pointerdown', e => { if (e.target === feed) unstick(); });
  feed.addEventListener('pointerup', () => setTimeout(resumeAtEnd, 0));
  feed.addEventListener('keyup', resumeAtEnd);
  feed.addEventListener('wheel', e => { if (e.deltaY > 0) resumeAtEnd(); }, {passive: true});
  feed.addEventListener('keydown', e => { if (['ArrowUp', 'PageUp', 'Home'].includes(e.key)) unstick(); });
  feed.onscroll = () => { const gap = feed.scrollHeight - feed.scrollTop - feed.clientHeight; $('#jump').classList.toggle('on', gap > 40); };
  $('#jump').onclick = () => { document.querySelectorAll('.live .plist').forEach(x => { delete x.dataset.reading; x.scrollTop = x.scrollHeight; }); stick = true; feed.scrollTo({top: feed.scrollHeight, behavior: 'smooth'}); };
  wireComposer((text, onProg, attachments) => sendApi({num, text, ...attachments}, onProg), (text, imgs) => { const p = {text, imgs, t: Date.now(), base: saidBase(num, text)}; (pending[num] ||= []).push(p); return p; });
  if (view && view.draft && $('#ta')) { $('#ta').value = view.draft; $('#ta').oninput(); }
  renderSide(); headStatus(); if (!view) showCached(num); loadThread();
}

// 告诉 Mac“我正开着这个对话看”：做完时不再弹通知、不发钉钉（切走、锁屏、5 分钟没碰就不算在看）
// Mac 屏幕灭了 Safari 仍算“可见”，只能看人有没有碰：点、按键、滚轮、移鼠标（不算自动滚动）
const LOOK_IDLE = 300000;
let lastAct = Date.now(), looked = false;
const looking = () => !!sel && !document.hidden && document.hasFocus() && Date.now() - lastAct < LOOK_IDLE;
for (const ev of ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart', 'focus'])
  addEventListener(ev, () => { const back = !looked; lastAct = Date.now(); if (back && looking()) { looked = true; presence(true); } }, {passive: true, capture: true});
function presence(on) {
  const body = JSON.stringify({num: sel || 0, visible: !!(on && looking()), v: 2});
  if (!on && navigator.sendBeacon) navigator.sendBeacon('/_x/api/presence', new Blob([body], {type: 'application/json'}));
  else fetch('/_x/api/presence', {method: 'POST', headers: {'Content-Type': 'application/json'}, body, keepalive: true}).catch(() => {});
}
setInterval(() => { const on = looking(); if (sel && (on || looked)) presence(on); looked = on; }, 15000);
addEventListener('pagehide', () => presence(false));
function goList() { presence(false); sel = null; setTimeout(connectEvents); if (location.hash) history.replaceState(null, '', location.pathname); $('#app').classList.remove('in'); renderSide(); }
// 实时推送：Mac 那边一有变化就推过来（列表 s、这个对话 t、正在写的回复 p），不再每几秒问一次
let es = null, esNum = -1, esOk = false, lastPoll = 0, partialText = '', partialOpen = false;
const pushOn = () => 'Notification' in window && Notification.permission === 'granted';
function connectEvents() {
  const n = sel || 0;
  if (es && esNum === n && es.readyState !== 2) return;
  if (es) es.close();
  esNum = n; esOk = false;
  es = new EventSource('/_x/api/events?num=' + n);
  es.onopen = () => { esOk = true; setOffline(false); };
  es.onerror = () => { esOk = false; setConn('wait'); };          // 浏览器会自己重连；期间退回定时问
  es.addEventListener('s', () => loadSessions());
  es.addEventListener('t', () => loadThread());
  es.addEventListener('p', e => { partialText = JSON.parse(e.data).text || ''; showPartial(); });
}
function showPartial() {
  const live = document.querySelector('.live'), feed = $('#feed');
  let el = document.querySelector('.live .partial');
  if (!live || !partialText) { if (el) el.remove(); return; }
  const near = feed && stick;
  if (!el) { el = document.createElement('div'); el.className = 'partial'; live.appendChild(el); el.onclick = () => { partialOpen = !partialOpen; showPartial(); }; }
  // 正在写的这段：默认只露最后一句（多半是“我先看看…”这类过程话），点开看全文；写完的回复会整段出现在下面
  const lastLine = partialText.trim().split('\n').filter(Boolean).pop() || '';
  el.classList.toggle('open', partialOpen);
  el.innerHTML = partialOpen ? md(partialText) + '<i class="caret"></i>' : `<span class="pl">正在写：</span>${esc(lastLine.replace(/[*`#>]/g, '').slice(-80))}<i class="caret"></i>`;
  if (near) feed.scrollTop = feed.scrollHeight;
}

// 点手机通知进来：打开通知说的那个对话。编号可能在地址里（?s=4），也可能在后台脚本记下的缓存里，或者直接发消息过来
async function openFromNotice() {
  let n = +new URLSearchParams(location.search).get('s') || 0;
  try {
    const c = await caches.open('anzhuo'), r = await c.match('/_x/__open');
    if (r) { n = n || +(await r.text()); await c.delete('/_x/__open'); }
  } catch (e) {}
  if (!n) return false;
  history.replaceState(null, '', location.pathname); openSession(n); return true;
}
if ('serviceWorker' in navigator) navigator.serviceWorker.addEventListener('message', e => {
  if (e.data && e.data.open) { caches.open('anzhuo').then(c => c.delete('/_x/__open')).catch(() => {}); openSession(+e.data.open); }
});

// 手机通知：只有加到主屏幕后打开（全屏模式）iPhone 才允许；第一次要点一下“开启通知”，之后每次打开悄悄续上订阅
async function setupPush() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
  const reg = await navigator.serviceWorker.register('/_x/sw.js', {scope: '/_x/'});
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  const subscribe = async () => {
    const {key} = await api('push/key');
    const raw = Uint8Array.from(atob(key.replace(/-/g, '+').replace(/_/g, '/') + '=='.slice(0, (4 - key.length % 4) % 4)), c => c.charCodeAt(0));
    const sub = (await reg.pushManager.getSubscription()) || await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: raw});
    await api('push', sub.toJSON());
  };
  if (Notification.permission === 'granted') return subscribe().catch(() => {});
  if (!standalone || Notification.permission === 'denied') return;
  const b = $('#pushBtn'); b.hidden = false;
  b.onclick = async () => {
    b.querySelector('.nm').textContent = '正在开启…';
    if (await Notification.requestPermission() === 'granted') { await subscribe().catch(() => {}); b.hidden = true; }
    else b.querySelector('.nm').textContent = '没允许通知，可到 设置 → 通知 → 远窗 里打开';
  };
}
setupPush().catch(() => {});

