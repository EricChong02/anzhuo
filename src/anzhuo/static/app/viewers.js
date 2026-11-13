// 主屏幕打开的远窗（没有浏览器的返回键）点文件：不跳走，在页面里盖一层看；右上角“完成”回来，左上角“分享”交给 WPS、文件 App 等
const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
document.addEventListener('click', e => {
  const share = e.target.closest && e.target.closest('button.file-share');
  if (share) {
    e.preventDefault();
    viewFile(share.dataset.fileUrl, share.dataset.fileName, true);
    return;
  }
  const a = e.target.closest && e.target.closest('a.file');
  if (!a || !standalone) return;
  // 原始 PDF 在独立浏览页打开，交给浏览器的完整 PDF 阅读器。
  if (/\.pdf$/i.test((a.querySelector('.fn') || a).textContent.trim())) return;
  e.preventDefault(); viewFile(a.href, (a.querySelector('.fn') || a).textContent.trim());
});
// 取文件时报进度：大文件（几十 MB 的视频）经隧道要下好一阵，不能只显示一句“正在取文件…”
async function fetchFile(url, onp, signal) {
  const r = await fetch(url, {signal}); if (!r.ok) throw new Error(r.status);
  if (r.redirected && new URL(r.url, location.href).pathname.endsWith('/_x/login')) throw new Error('login');
  const total = +r.headers.get('content-length') || 0, type = r.headers.get('content-type') || '';
  if (!r.body || !r.body.getReader) return r.blob();
  const rd = r.body.getReader(), parts = []; let got = 0;
  for (;;) { const {done, value} = await rd.read(); if (done) break; parts.push(value); got += value.length; onp(got, total); }
  return new Blob(parts, {type});
}
const progressHTML = (got, total) => total
  ? `正在取文件… ${size(got)} / ${size(total)}（${Math.floor(got * 100 / total)}%）<div class="bar"><i style="width:${(got * 100 / total).toFixed(1)}%"></i></div>`
  : `正在取文件… 已取 ${size(got)}`;
function canShareOriginal(file) {
  try { return !!navigator.share && (!navigator.canShare || navigator.canShare({files: [file]})); }
  catch (_) { return false; }
}
function bindOriginalShare(button, file) {
  button.hidden = false;
  button.textContent = '分享';
  button.onclick = async () => {
    if (button.disabled) return;
    button.disabled = true;
    try { await navigator.share({files: [file]}); }
    catch (err) { if (err.name !== 'AbortError') toast('分享面板没打开，请再点一次“分享”。'); }
    finally { button.disabled = false; }
  };
}
async function viewFile(url, name, shareOnly = false) {
  const el = document.createElement('div'); el.className = 'lightbox fv';
  el.innerHTML = `<span class="nm">${esc(name)}</span><button class="sh" hidden>分享</button><button class="x" aria-label="关闭"><b>×</b>完成</button><div class="tip">正在取文件…</div>`;
  document.body.appendChild(el); history.pushState({lb: 1}, '', location.hash || location.pathname);
  let u = '';
  const ac = new AbortController();
  el.querySelector('.x').onclick = () => history.back();
  new MutationObserver((_, ob) => { if (!el.isConnected) { ac.abort(); el.querySelectorAll('audio,video').forEach(a => { a.pause(); a.removeAttribute('src'); a.load(); }); if (u) URL.revokeObjectURL(u); ob.disconnect(); } }).observe(document.body, {childList: true});
  const sh = el.querySelector('.sh'), tipOf = () => el.querySelector('.tip');
  const ext = (name.split('.').pop() || '').toLowerCase();
  const pull = async () => {
    const blob = await fetchFile(url, (g, t) => { const tp = tipOf(); if (tp) tp.innerHTML = progressHTML(g, t); }, ac.signal);
    const type = blob.type && blob.type !== 'application/octet-stream' ? blob.type : ({doc: 'application/msword', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', xls: 'application/vnd.ms-excel', xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', ppt: 'application/vnd.ms-powerpoint', pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation', txt: 'text/plain', csv: 'text/csv', zip: 'application/zip', pdf: 'application/pdf', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', mp3: 'audio/mpeg', m4a: 'audio/mp4', wav: 'audio/wav', mp4: 'video/mp4', m4v: 'video/mp4', mov: 'video/quicktime', webm: 'video/webm'}[ext] || blob.type);
    return new File([blob], name, {type});
  };
  // 视频：不先整个下完，直接边下边播（服务端支持 Range）；要分享或保存再点“分享”下载，下载时有进度
  if (!shareOnly && /^(mp4|m4v|mov|webm)$/.test(ext)) {
    tipOf().outerHTML = `<video controls playsinline preload="metadata" src="${esc(url)}#t=0.1"></video><div class="tip">点 ▶ 边下边播，不用等整个下完。<br>要发到微信或保存，点左上角“分享”。</div>`;
    const v = el.querySelector('video');
    v.onplay = () => speechPlayer.pause();
    v.onerror = () => { const tp = tipOf(); if (tp) tp.textContent = '这个视频在这里放不了。点左上角“分享”，准备好后选微信、相册或文件 App。'; };
    sh.textContent = '分享'; sh.hidden = false;
    sh.onclick = async () => {
      sh.onclick = null; sh.textContent = '下载中…';
      try {
        const file = await pull(); if (!el.isConnected) return;
        // iPhone 的分享面板要“刚点过”才肯弹出，下完要再点一下
        sh.textContent = '分享';
        tipOf().textContent = '文件已准备好。点左上角“分享”，选择微信或保存到手机。';
        bindOriginalShare(sh, file);
        if (!canShareOriginal(file)) { sh.hidden = true; tipOf().textContent = '下完了，但这台设备不支持从这里存，回电脑上取吧。'; }
      } catch (err) { if (!el.isConnected) return; sh.textContent = '分享'; sh.onclick = null; tipOf().textContent = '没下完，点“完成”回去再试一次。'; }
    };
    return;
  }
  try {
    const file = await pull(); if (!el.isConnected) return;
    if (shareOnly) {
      if (!canShareOriginal(file)) {
        tipOf().textContent = '这个浏览器暂不支持分享此文件。请点“完成”返回，使用文件旁的“下载”。';
        return;
      }
      bindOriginalShare(sh, file);
      tipOf().textContent = '文件已准备好。点左上角“分享”，在系统面板中选择微信或其他应用。';
      // 快速取到文件时可沿用这次点击；大文件准备完后由用户再点，保留 iOS 所需的手势。
      if (navigator.userActivation && navigator.userActivation.isActive) sh.onclick();
      return;
    }
    const type = file.type;
    u = URL.createObjectURL(file);
    const tip = tipOf();
    if (/^image\//.test(type)) tip.outerHTML = `<img src="${u}">`;
    else if (/^audio\//.test(type)) tip.outerHTML = `<audio controls preload="metadata" src="${u}" style="width:calc(100% - 40px);margin:40px 20px"></audio>`;
    else if (type === 'application/pdf' || /^text\//.test(type)) tip.outerHTML = `<iframe src="${u}"></iframe>`;
    else tip.textContent = '这种文件在这里看不了预览。点左上角“分享”，发送到微信或用其他应用打开。';
    const previewAudio = el.querySelector('audio');
    if (previewAudio) previewAudio.onplay = () => speechPlayer.pause();
    if (canShareOriginal(file)) {
      bindOriginalShare(sh, file);
    } else if (!/^image\/|^audio\/|pdf|^text\//.test(type)) tip.textContent = '这种文件在这里看不了预览，回电脑上打开吧。';
  } catch (err) { const t = tipOf(); if (t) t.textContent = err.message === 'login' ? '登录已过期，请返回远窗重新登录后再分享。' : '文件没取到，点“完成”回去再试一次。'; }
}

// 看大图：在页面里盖一层（主屏幕 app 里没有浏览器的返回键，不能跳走）。两指捏合放大、双击放大/还原、放大后单指拖动；
// 没放大时点空白、右上角 ×、返回手势都能关。电脑上滚轮缩放、双击放大。
function viewImage(list, at = 0) {
  // list：同一条消息里的几张图；没放大时左右滑切换（电脑上左右方向键、两侧箭头），右上角“2 / 5”
  if (typeof list === 'string') list = [list];
  let i = Math.max(0, Math.min(at, list.length - 1));
  const many = list.length > 1;
  const el = document.createElement('div'); el.className = 'lightbox';
  el.innerHTML = `<img src="${esc(list[i])}" draggable="false"><button class="x" aria-label="关闭"><b>×</b>关闭</button>`
    + (many ? `<span class="cnt"></span><button class="nav prev" aria-label="上一张">‹</button><button class="nav next" aria-label="下一张">›</button>` : '');
  document.body.appendChild(el); history.pushState({lb: 1}, '', location.hash || location.pathname);
  const img = el.querySelector('img');
  let sc = 1, tx = 0, ty = 0, start = null, lastTap = 0, moved = false, closeTimer = 0, sliding = false;
  const W = () => innerWidth / 2, H = () => innerHeight / 2;
  const apply = (anim) => { img.style.transition = anim ? 'transform .22s ease-out' : 'none'; img.style.transform = `translate(${tx}px,${ty}px) scale(${sc})`; };
  const mark = () => { if (!many) return; el.querySelector('.cnt').textContent = `${i + 1} / ${list.length}`;
    el.querySelector('.prev').hidden = i === 0; el.querySelector('.next').hidden = i === list.length - 1; };
  mark();
  // 下一张从这边滑进来：先把当前这张滑出去，换图后从另一边滑回中间
  const go = d => {
    if (sliding || i + d < 0 || i + d >= list.length) { tx = ty = 0; sc = 1; return apply(true); }
    sliding = true; sc = 1; ty = 0; tx = -d * innerWidth; apply(true);
    setTimeout(() => {
      i += d; img.src = list[i]; mark();
      tx = d * innerWidth; apply(false);
      requestAnimationFrame(() => requestAnimationFrame(() => { tx = 0; apply(true); setTimeout(() => sliding = false, 230); }));
    }, 200);
  };
  [-1, 1].forEach(d => { const b = el.querySelector(d < 0 ? '.prev' : '.next'); if (b) b.onclick = e => { e.stopPropagation(); go(d); }; });
  const key = e => { if (e.key === 'ArrowRight') go(1); else if (e.key === 'ArrowLeft') go(-1); };
  addEventListener('keydown', key);
  new MutationObserver((_, ob) => { if (!el.isConnected) { removeEventListener('keydown', key); ob.disconnect(); } }).observe(document.body, {childList: true});
  const zoomAt = (ns, x, y) => {   // 以屏幕上 (x,y) 为中心缩放到 ns 倍
    ns = Math.min(6, Math.max(1, ns)); const cx = x - W(), cy = y - H();
    tx = cx - (cx - tx) * ns / sc; ty = cy - (cy - ty) * ns / sc; sc = ns; if (sc === 1) tx = ty = 0;
  };
  const dist = t => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  el.addEventListener('touchstart', e => {
    moved = false;
    if (e.touches.length === 2) start = {d: dist(e.touches), s: sc, x: (e.touches[0].clientX + e.touches[1].clientX) / 2, y: (e.touches[0].clientY + e.touches[1].clientY) / 2, tx, ty};
    else start = {px: e.touches[0].clientX, py: e.touches[0].clientY, tx, ty, swipe: sc === 1 && many};
  }, {passive: true});
  el.addEventListener('touchmove', e => {
    e.preventDefault(); moved = true; if (!start || sliding) return;
    if (e.touches.length === 2 && start.d) {
      start.swipe = false; sc = start.s; tx = start.tx; ty = start.ty; zoomAt(start.s * dist(e.touches) / start.d, start.x, start.y);
    } else if (e.touches.length === 1 && start.px != null && start.swipe) {
      // 没放大：图跟着手指左右走（到头了只走一点点，像拉不动）
      const dx = e.touches[0].clientX - start.px, edge = (dx > 0 && i === 0) || (dx < 0 && i === list.length - 1);
      tx = edge ? dx / 4 : dx; ty = 0;
    } else if (e.touches.length === 1 && sc > 1 && start.px != null) {
      tx = start.tx + e.touches[0].clientX - start.px; ty = start.ty + e.touches[0].clientY - start.py;
    }
    apply(false);
  }, {passive: false});
  el.addEventListener('touchend', e => {
    if (e.touches.length) { start = {px: e.touches[0].clientX, py: e.touches[0].clientY, tx, ty}; return; }
    const st = start; start = null;
    if (moved) {
      if (st && st.swipe && sc === 1) { const dx = e.changedTouches[0].clientX - st.px; if (Math.abs(dx) > 60) go(dx < 0 ? 1 : -1); else { tx = 0; apply(true); } }
      return;
    }
    const t = e.changedTouches[0], now = Date.now();
    if (now - lastTap < 300) { clearTimeout(closeTimer); zoomAt(sc > 1 ? 1 : 2.5, t.clientX, t.clientY); apply(true); lastTap = 0; e.preventDefault(); return; }
    lastTap = now;
    if (e.target.closest('.x')) { e.preventDefault(); return history.back(); }
    if (e.target.closest('.nav')) return;
    // 点在图片外面（没放大时）：等一下确认不是双击，就关掉
    if (sc === 1 && e.target !== img) { e.preventDefault(); clearTimeout(closeTimer); closeTimer = setTimeout(() => { if (sc === 1) history.back(); }, 300); }
  });
  el.addEventListener('wheel', e => { e.preventDefault(); zoomAt(sc * (e.deltaY < 0 ? 1.15 : 1 / 1.15), e.clientX, e.clientY); apply(false); }, {passive: false});
  el.addEventListener('dblclick', e => { if (e.target.closest('.nav')) return; clearTimeout(closeTimer); zoomAt(sc > 1 ? 1 : 2.5, e.clientX, e.clientY); apply(true); });
  el.onclick = e => {
    if (e.target.closest('.x')) return history.back();
    if (e.target.closest('.nav') || e.target === img || sc > 1) return;
    clearTimeout(closeTimer); closeTimer = setTimeout(() => { if (sc === 1) history.back(); }, 320);   // 等等看是不是双击
  };
}
document.addEventListener('click', e => {
  const a = e.target.closest('.imgs a'); if (!a) return;
  e.preventDefault();
  const all = [...a.closest('.imgs').querySelectorAll('a')];
  viewImage(all.map(x => x.getAttribute('href')), all.indexOf(a));
});
