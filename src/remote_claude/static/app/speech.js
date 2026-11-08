// 同一个音频元素跨对话保留；流式播放无需锁屏后用 JS 接下一段。
const speechPlayer = (() => {
  const audio = new Audio(); audio.preload = 'none';
  const bar = document.createElement('section'); bar.className = 'speech-player'; bar.hidden = true;
  bar.setAttribute('aria-label', '回复朗读');
  bar.innerHTML = `<div class="sp-main"><button class="sp-play" aria-label="播放"></button><button class="sp-back" aria-label="后退15秒"><svg viewBox="0 0 24 24"><path d="M4 9a8 8 0 1 1 0 6M4 4v5h5"/><text x="8" y="16" stroke="none" fill="currentColor" font-size="9">15</text></svg></button><div class="sp-title"><b></b><span role="status" aria-live="polite"></span></div><button class="sp-next" aria-label="前进15秒"><svg viewBox="0 0 24 24"><path d="M20 9a8 8 0 1 0 0 6M20 4v5h-5"/><text x="7" y="16" stroke="none" fill="currentColor" font-size="9">15</text></svg></button><button class="sp-speed" aria-label="播放速度">1×</button><a class="sp-download" target="_blank" rel="noopener" aria-label="下载音频">${ICON.down}</a><button class="sp-close" aria-label="关闭朗读">×</button></div><div class="sp-progress"><span class="sp-pos">0:00</span><input type="range" min="0" max="1" step="0.1" value="0" aria-label="播放进度"><span class="sp-total">生成中</span></div>`;
  const q = s => bar.querySelector(s), slider = q('input');
  const pauseIcon = '<svg viewBox="0 0 24 24"><path d="M8 5v14M16 5v14" stroke-width="3"/></svg>';
  let listener = '', waiting = '', mediaVersion = 0, buffering = false;
  let source = null, key = '', ref = '', num = 0, ready = false, attached = false, mode = '', desired = false, loading = false;
  let restoreAt = 0, total = 0, token = 0, poll = null, error = '', failed = false, lastSave = 0, lastTouch = 0, dragging = false;
  const timecode = t => { t = Math.max(0, Math.floor(t || 0)); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`; };
  function mount() {
    if (bar.hidden) return;
    const mobileList = matchMedia('(max-width:820px)').matches && !$('#app').classList.contains('in');
    const host = mobileList ? document.querySelector('.side') : $('#main');
    const head = host?.querySelector(mobileList ? '.brand' : '.head');
    const before = head ? head.nextSibling : host?.firstChild;
    if (host && (bar.parentNode !== host || before !== bar)) host.insertBefore(bar, before);
  }
  function positions() {
    try { return JSON.parse(localStorage.getItem('speechPositions') || '{}'); } catch (_) { return {}; }
  }
  function save(force = false) {
    if (!key || !attached || audio.readyState < 1 || (!force && Date.now() - lastSave < 3000)) return;
    lastSave = Date.now();
    const p = positions(); p[key] = {at: audio.ended ? 0 : audio.currentTime || 0, touched: Date.now()};
    const list = Object.entries(p).filter(([,v]) => Date.now() - v.touched < 7 * 86400000).sort((a,b) => b[1].touched-a[1].touched).slice(0,50);
    try { localStorage.setItem('speechPositions', JSON.stringify(Object.fromEntries(list))); } catch (_) {}
  }
  function touch(playing) {
    if (!key) return;
    lastTouch = Date.now(); api('audio/' + key, {playing, listener}).catch(() => {});
  }
  function paint() {
    const active = attached && !audio.paused && !audio.ended && !buffering;
    q('.sp-play').innerHTML = desired && (loading || buffering) ? '<i class="spin"></i>' : active ? pauseIcon : ICON.play;
    q('.sp-play').setAttribute('aria-label', active || desired && (loading || buffering) ? '暂停' : '播放');
    q('.sp-title span').textContent = error || (!desired ? (audio.ended ? '已播完' : '已暂停') : waiting || (buffering ? '正在缓冲…' : !attached ? '点播放开始' : audio.paused ? '已暂停' : !ready ? '播放中 · 正在生成' : ''));
    q('.sp-pos').textContent = timecode(attached ? audio.currentTime : restoreAt);
    q('.sp-total').textContent = ready ? timecode(total) : waiting.startsWith('排队中') ? '排队中' : waiting.startsWith('正在准备') ? '准备中' : '生成中';
    slider.disabled = !ready;
    slider.max = Math.max(total, 1);
    if (!dragging) slider.value = Math.min(total || 0, audio.currentTime || 0);
    q('.sp-back').disabled = q('.sp-next').disabled = !ready;
    q('.sp-download').hidden = !ready;
    if (ready) q('.sp-download').href = '/_x/audio/' + key + '.mp3?download=1';
    if ('mediaSession' in navigator) {
      navigator.mediaSession.playbackState = active ? 'playing' : 'paused';
      if (ready && total > 0 && Number.isFinite(audio.currentTime)) {
        try { navigator.mediaSession.setPositionState({duration: total, playbackRate: audio.playbackRate, position: Math.min(total, audio.currentTime)}); } catch (_) {}
      }
    }
  }
  async function play() {
    const version = mediaVersion; desired = true; error = '';
    try { await audio.play(); } catch (_) { if (version === mediaVersion) { desired = false; error = '点播放开始收听'; } }
    if (version === mediaVersion) paint();
  }
  function attach(full, at = 0) {
    const wasPlaying = desired; ++mediaVersion; buffering = true;
    attached = false; mode = full ? 'full' : 'stream';
    audio.src = '/_x/audio/' + key + '.mp3' + (full ? '?complete=1' : '?stream=1');
    audio.onloadedmetadata = () => {
      attached = true;
      if (full && at > 0) audio.currentTime = Math.min(at, Math.max(0, total - .1));
      paint();
    };
    audio.load(); attached = true;
    if (wasPlaying) play(); else paint();
  }
  async function refresh(myToken) {
    if (myToken !== token || !key) return;
    try {
      const d = await api('audio/' + key + '?listener=' + encodeURIComponent(listener));
      if (myToken !== token) return;
      if (!d.ok) { failed = true; error = '音频已清理，请点播放重新生成'; desired = false; loading = false; audio.pause(); paint(); return; }
      if (d.state === 'error') { failed = true; loading = false; error = d.error || '生成失败，请关闭后重新点朗读'; desired = false; audio.pause(); paint(); return; }
      total = d.duration || 0; ready = d.state === 'ready'; loading = !d.parts && !ready;
      waiting = d.state === 'queued' ? (d.queue_ahead ? `排队中，前面还有 ${d.queue_ahead} 段` : '正在准备语音…') : loading ? `正在生成，已等 ${d.wait_seconds || 0} 秒` : '';
      if (!attached && (d.parts || ready) && (!restoreAt || ready)) {
        attach(ready, ready ? restoreAt : 0);
      } else if (ready && mode === 'stream' && document.visibilityState === 'visible') {
        attach(true, audio.currentTime || 0);
      }
      paint();
    } catch (e) { if (myToken !== token) return; error = e.message === 'offline' ? '连接中…' : e.message; paint(); }
    if (myToken === token && !ready) poll = setTimeout(() => refresh(myToken), 1000);
  }
  function release(id) {
    if (id) fetch('/_x/api/speech/release', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({listener: id}), keepalive: true}).catch(() => {});
  }
  function close() {
    const oldListener = listener; ++mediaVersion; buffering = false;
    ++token; clearTimeout(poll); save(true); desired = false; attached = false; audio.onloadedmetadata = null; audio.pause(); touch(false);
    audio.removeAttribute('src'); audio.load(); attached = false; key = ''; ref = ''; source = null; listener = ''; waiting = ''; bar.hidden = true; bar.remove();
    release(oldListener);
    if ('mediaSession' in navigator) { navigator.mediaSession.playbackState = 'none'; navigator.mediaSession.metadata = null; }
  }
  async function start(d, it) {
    const nextRef = d.num + ':' + (it.tid || it.end);
    if (ref === nextRef && key) { toggle(); return; }
    close(); const myToken = ++token, myListener = crypto.randomUUID ? crypto.randomUUID() : Date.now() + '-' + Math.random();
    listener = myListener; waiting = '正在准备语音…';
    source = {d, it}; num = d.num; ref = nextRef; ready = false; total = 0; loading = true; desired = true; error = ''; failed = false; restoreAt = 0;
    q('.sp-title b').textContent = d.name || '远窗朗读'; bar.hidden = false; mount(); paint();
    // 用户点击时先激活同一媒体元素，后续生成完成仍使用它；若系统拒绝自动续播，提供明确的播放按钮。
    audio.src = 'data:audio/wav;base64,UklGRnQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YVAAAACAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgA==';
    audio.play().catch(() => {});
    try {
      const r = await api('speech/' + d.num, {tid: it.tid || '', end: it.end || '', listener: myListener});
      if (myToken !== token) { release(myListener); return; }
      if (!r.ok) throw new Error(r.error || '没有生成成功，请重试');
      key = r.key; restoreAt = positions()[key]?.at || 0;
      if ('mediaSession' in navigator && typeof MediaMetadata !== 'undefined') navigator.mediaSession.metadata = new MediaMetadata({title: d.name || '远窗朗读', artist: '远窗'});
      touch(true); await refresh(myToken);
    } catch (e) { if (myToken === token) { failed = true; loading = false; desired = false; error = e.message; paint(); } }
  }
  function toggle() {
    if (failed && source) { const prev = source; close(); start(prev.d, prev.it); return; }
    if (!attached) { desired = !desired; paint(); return; }
    if (!audio.paused || loading && desired) { desired = false; audio.pause(); } else { desired = true; if (audio.error) attach(ready, audio.currentTime || restoreAt); else play(); }
    touch(desired); paint();
  }
  function seek(to) { if (ready && attached) { audio.currentTime = Math.max(0, Math.min(total, to)); save(true); paint(); } }
  q('.sp-play').onclick = toggle;
  q('.sp-back').onclick = () => seek(audio.currentTime - 15);
  q('.sp-next').onclick = () => seek(audio.currentTime + 15);
  q('.sp-close').onclick = close;
  q('.sp-speed').onclick = () => {
    const rates = [1, 1.25, 1.5, 1.75, 2, .75]; audio.playbackRate = rates[(rates.indexOf(audio.playbackRate) + 1) % rates.length];
    q('.sp-speed').textContent = audio.playbackRate + '×'; paint();
  };
  slider.oninput = () => { dragging = true; q('.sp-pos').textContent = timecode(+slider.value); };
  slider.onchange = () => { seek(+slider.value); dragging = false; paint(); };
  slider.onblur = () => { dragging = false; paint(); };
  audio.ontimeupdate = () => { if (!attached) return; save(); if (!audio.paused && Date.now() - lastTouch > 30000) touch(true); paint(); };
  audio.onwaiting = () => { if (attached) { buffering = true; paint(); } };
  audio.onplaying = () => { if (attached) { buffering = false; error = ''; paint(); } };
  audio.onplay = () => { if (attached) { desired = true; paint(); } };
  audio.onpause = () => { if (attached) { save(true); paint(); } };
  audio.onended = () => { if (attached) { desired = false; save(true); touch(false); paint(); } };
  audio.onerror = () => { if (attached) { desired = false; error = '播放中断，点播放重试'; paint(); } };
  addEventListener('pagehide', () => { save(true); release(listener); });
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && key) { clearTimeout(poll); refresh(token); } });
  new MutationObserver(mount).observe($('#app'), {childList: true, subtree: true, attributes: true, attributeFilter: ['class']});
  addEventListener('resize', mount);
  if ('mediaSession' in navigator) {
    const actions = {play, pause: () => { desired = false; audio.pause(); touch(false); paint(); }, seekbackward: e => seek(audio.currentTime - (e.seekOffset || 15)), seekforward: e => seek(audio.currentTime + (e.seekOffset || 15)), seekto: e => seek(e.seekTime), stop: close};
    for (const [name, handler] of Object.entries(actions)) { try { navigator.mediaSession.setActionHandler(name, handler); } catch (_) {} }
  }
  return {start, close, pause: () => { desired = false; audio.pause(); touch(false); paint(); }, deleted: n => { if (num === n) close(); }};
})();
