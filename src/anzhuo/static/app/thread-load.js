document.addEventListener('click', e => {
  const b = e.target.closest('.cpy'); if (!b) return;
  e.preventDefault(); e.stopPropagation();
  const pre = b.closest('.cw, details.code').querySelector('pre');
  copyBtn(b, pre.textContent);
}, true);
function copyBtn(b, text) {
  navigator.clipboard.writeText(text).then(() => {
    b.innerHTML = IB.ok; b.dataset.tip = '已复制'; b.classList.add('done');
    setTimeout(() => { b.innerHTML = IB.copy; b.dataset.tip = '复制'; b.classList.remove('done'); }, 1500);
  }, () => toast('没复制成'));
}
async function forkAt(d, to, text) {
  toast('正在分叉…');
  const r = await api('fork/' + d.num, {to}).catch(() => null);
  if (!r || !r.ok) return toast((r && r.error) || '没分成，再试一次');
  await loadSessions(); openSession(r.num);
  const ta = $('#ta'); if (ta && text) { ta.value = text; ta.oninput(); ta.focus(); }
  toast('已分叉');
}
// 好一会儿没新动静：3 分钟起轻轻提一句，10 分钟起提醒可能卡住了，给个“问问它”
function tickSilence() {
  const el = document.querySelector('.silence'); if (!el) return;
  const m = Math.floor((Date.now() / 1000 - +el.dataset.t0) / 60);
  const html = m >= 10 ? `<span>${m} 分钟没动静</span><button class="pill" id="askBtn">问问它</button>`
    : m >= 3 ? `<span>${m} 分钟没动静</span>` : '';
  if (el.dataset.h !== html) { el.dataset.h = html; el.innerHTML = html; el.className = 'silence' + (m >= 10 ? ' stuck' : ''); el.dataset.t0 = el.dataset.t0;
    const b = $('#askBtn'); if (b) b.onclick = async () => { b.disabled = true; b.textContent = '问了';
      await api('send', {num: sel, text: '现在做到哪了？是不是卡住了？卡在哪一步说一下。'}).catch(() => {}); loadThread(); }; }
}

// 看过的对话记在本机：再点开先显示上次的内容，最新的随后补上（外网服务器出口常只有几 Mbps，整段对话要传一两秒）
const threadCache = new Map();
try { for (const [n, v] of JSON.parse(localStorage.getItem('threads') || '[]')) threadCache.set(n, v); } catch (e) {}
function cacheThread(d) {
  threadCache.delete(d.num);
  threadCache.set(d.num, JSON.stringify({...d, live: null, decision: null}));   // 进行中、等你定这些只认新鲜的
  while (threadCache.size > 8) threadCache.delete(threadCache.keys().next().value);
  clearTimeout(cacheThread.t);
  cacheThread.t = setTimeout(saveThreads, 1500);
}
function clearThreads() { threadCache.clear(); clearTimeout(cacheThread.t); cacheThread.t = 0; try { localStorage.removeItem('threads'); } catch (e) {} }
function saveThreads() { try { localStorage.setItem('threads', JSON.stringify([...threadCache])); } catch (e) {} }
addEventListener('pagehide', () => { if (cacheThread.t) saveThreads(); });
function showCached(num) {
  const v = threadCache.get(num);
  if (v) try { renderThread(JSON.parse(v)); } catch (e) {}
}

async function loadThread() {
  // 后台通知仍更新列表；只有实际打开对话时才读取，避免请求顺带清掉未读。
  if (!sel || document.hidden || (matchMedia('(max-width:820px)').matches && !$('#app').classList.contains('in'))) return;
  try {
    const d = await api('thread/' + sel);
    if (d.num !== sel) return;
    if (d.redirect) { await loadSessions(); if (sel === d.num) followRelay(d.redirect); return; }
    cacheThread(d);
    const key = JSON.stringify(d) + JSON.stringify(pending[d.num] || []);
    const selText = getSelection && getSelection(); const reading = selText && !selText.isCollapsed && $('#feed') && $('#feed').contains(selText.anchorNode);
    if (key !== lastKey && !reading) {
      lastKey = key; renderThread(d);
      // 服务端已在读取完成回复时标记已读，让新完成及时归回原任务分类。
      if (!d.live && sessions.some(s => s.num === d.num && s.unread)) loadSessions();
    }
  } catch (e) { if (e.message !== 'login') console.warn(e); }
}

function shrink(file) {
  return new Promise((res, reject) => {
    const img = new Image(), url = URL.createObjectURL(file);
    img.onload = () => {
      const k = Math.min(1, 1600 / Math.max(img.width, img.height));
      const c = document.createElement('canvas'); c.width = img.width * k; c.height = img.height * k;
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height); URL.revokeObjectURL(url);
      res(c.toDataURL('image/jpeg', .86));
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('image')); };
    img.src = url;
  });
}

