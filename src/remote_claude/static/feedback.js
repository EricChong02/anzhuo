// 试课页上的“反馈这一屏”：小E 转发试课预览时插进页面（不改 CyberScope 本身）。
// 引擎自己记着翻到第几节第几屏（__CS_CURRENT_SECTION / __CS_SCREEN_IDX），点一下写一句话（可附 iPad 截屏后圈画的图），直接交给做这门课的对话。
// 还管三件事：
//  1. 地址里带 xe_sec / xe_scr（案头回复里的“回到第 N 节第 M 屏”）就直接翻到那一屏；
//  2. 右下角一个小条，告诉你这门课是哪个对话在改、改到哪了；改完了变成“改好了 · 刷新到这一屏”；
//  3. “先记下”：一屏一屏看下来，几处问题攒着，最后一起发（它一次改完，不用一处处等）。
(() => {
  if (window.__xiaoeFeedback) return;
  window.__xiaoeFeedback = true;
  const params = new URLSearchParams(location.search);
  const courseUrl = params.get('course') || '';
  const KEY = 'xe-fb:' + courseUrl, SENT = 'xe-sent:' + courseUrl;
  let manifest = null;
  const loadManifest = async () => {
    if (manifest || !courseUrl) return manifest;
    try { manifest = await (await fetch(courseUrl)).json(); } catch (e) { manifest = {}; }
    return manifest;
  };
  const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const load = k => { try { return JSON.parse(localStorage.getItem(k) || 'null'); } catch (e) { return null; } };
  const save = (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} };
  const until = async (ok, ms) => { const t = Date.now(); while (Date.now() - t < ms) { try { if (ok()) return true; } catch (e) {} await new Promise(r => setTimeout(r, 150)); } return false; };

  const css = `
.xe-bar{position:fixed;left:calc(14px + env(safe-area-inset-left));bottom:calc(14px + env(safe-area-inset-bottom));z-index:2147483000;display:flex;gap:8px;align-items:center;
  font:600 15px -apple-system,"PingFang SC",sans-serif}
.xe-fb,.xe-st{height:40px;padding:0 14px;border-radius:999px;border:1px solid rgba(0,0,0,.1);background:rgba(255,255,255,.86);
  -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);box-shadow:0 6px 20px rgba(0,0,0,.12);color:#1c1c1e;display:flex;align-items:center;gap:6px;font:inherit}
.xe-fb:active,.xe-st:active{transform:scale(.97)} .xe-fb em{font-style:normal;background:#2c4a74;color:#fff;border-radius:999px;padding:0 7px;font-size:13px;line-height:20px}
.xe-st{font-weight:500;color:#4a4a4f;max-width:46vw} .xe-st span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.xe-st i{width:7px;height:7px;border-radius:50%;background:#2c4a74;flex:none;animation:xe-p 1.6s ease-in-out infinite} .xe-st.wait i{background:#b07a06;animation:none}
.xe-st.done{background:#2c4a74;color:#fff;border-color:transparent;font-weight:600} .xe-st.done i{display:none}
@keyframes xe-p{50%{opacity:.35}}
@media (prefers-reduced-motion:reduce){.xe-st i{animation:none}}
.xe-sh{position:fixed;inset:0;z-index:2147483001;background:rgba(0,0,0,.35);display:flex;align-items:flex-end;justify-content:center;
  font:16px/1.5 -apple-system,"PingFang SC",sans-serif;color:#1c1c1e}
.xe-box{width:min(560px,100%);background:#fff;border-radius:20px 20px 0 0;padding:18px 18px calc(18px + env(safe-area-inset-bottom));box-shadow:0 -6px 30px rgba(0,0,0,.15);max-height:86vh;overflow:auto}
@media (min-width:700px){.xe-sh{align-items:center}.xe-box{border-radius:20px}}
.xe-box b{display:block;font-size:17px}.xe-where{color:#6b6b70;font-size:15px;margin:2px 0 12px;display:flex;align-items:center;gap:10px}
.xe-where span{flex:1;min-width:0}.xe-ask{flex:none;height:32px;border-radius:999px;padding:0 12px;border:1px solid #e4e4e0;background:#fff;color:#2c4a74;font:inherit;font-size:14px}
.xe-box textarea{width:100%;box-sizing:border-box;min-height:96px;border:1px solid rgba(0,0,0,.12);border-radius:14px;padding:10px 12px;font-family:inherit;font-size:16px;line-height:1.5;resize:vertical;outline:none}
.xe-box textarea:focus{border-color:#2c4a74}
.xe-row{display:flex;gap:8px;margin-top:10px;align-items:center;flex-wrap:wrap}
.xe-row label,.xe-row button{height:44px;border-radius:999px;padding:0 16px;font-family:inherit;font-size:16px;border:0;background:#f2f2ef;color:#1c1c1e;display:flex;align-items:center}
.xe-row .xe-send{margin-left:auto;background:#2c4a74;color:#fff;font-weight:600}
.xe-row .xe-send:disabled,.xe-row .xe-later:disabled{opacity:.45}
.xe-thumb{height:44px;border-radius:8px;border:1px solid rgba(0,0,0,.1)}
.xe-q{margin:0 0 12px;padding:10px 12px;border-radius:14px;background:#f6f6f3;font-size:15px}
.xe-q div{display:flex;gap:8px;padding:3px 0} .xe-q div span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.xe-q small{color:#6b6b70} .xe-q button{border:0;background:none;color:#6b6b70;font-size:18px;line-height:1;padding:0 4px}
.xe-done{font-size:16px;margin:8px 0 4px;color:#4a4a4f}.xe-done a{color:#2c4a74}`;
  const st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  const shrink = file => new Promise(res => {
    const img = new Image(), url = URL.createObjectURL(file);
    img.onload = () => {
      const k = Math.min(1, 1800 / Math.max(img.width, img.height));
      const c = document.createElement('canvas'); c.width = img.width * k; c.height = img.height * k;
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height); URL.revokeObjectURL(url);
      res(c.toDataURL('image/jpeg', .88));
    };
    img.src = url;
  });

  // 现在在哪一节哪一屏
  async function here() {
    const m = await loadManifest() || {};
    const section = (window.__CS_CURRENT_SECTION && window.__CS_CURRENT_SECTION.section_id) || '';
    const secs = m.sections || [];
    const i = secs.findIndex(x => x.id === section);
    const screen = typeof window.__CS_SCREEN_IDX === 'number' ? window.__CS_SCREEN_IDX : -1;
    const where = [i >= 0 ? `第 ${i + 1} 节「${secs[i].title || secs[i].id}」` : '', screen >= 0 ? `第 ${screen + 1} 屏` : ''].filter(Boolean).join(' · ') || '当前这一屏';
    return {m, section, section_no: i + 1, section_title: i >= 0 ? (secs[i].title || '') : '', screen, where};
  }
  const queue = () => load(KEY) || [];

  async function open() {
    const h = await here(), q = queue();
    const sh = document.createElement('div'); sh.className = 'xe-sh';
    sh.innerHTML = `<div class="xe-box"><b>反馈这一屏</b><div class="xe-where"><span>${h.m.title ? `《${esc(h.m.title)}》` : ''}${esc(h.where)}</span><button class="xe-ask" title="只问不改：它对照教案说说这一屏想做什么">问问这屏</button></div>
      ${q.length ? `<div class="xe-q"><small>已记下 ${q.length} 处，会和这一处一起发：</small>${q.map((x, k) =>
        `<div><span>${esc(x.where)}：${esc(x.text || '见截图')}</span><button data-k="${k}" aria-label="删掉">×</button></div>`).join('')}</div>` : ''}
      <textarea placeholder="哪里要改？"></textarea>
      <div class="xe-row"><label>加截图<input type="file" accept="image/*" hidden></label><span class="xe-th"></span>
      <button class="xe-cancel">取消</button><button class="xe-later" disabled>先记下</button>
      <button class="xe-send" ${q.length ? '' : 'disabled'}>${q.length ? `一起交（${q.length + 1} 处）` : '交给做课的对话'}</button></div></div>`;
    document.body.appendChild(sh);
    const ta = sh.querySelector('textarea'), send = sh.querySelector('.xe-send'), later = sh.querySelector('.xe-later');
    let image = '';
    const sync = () => { const has = !!(ta.value.trim() || image); later.disabled = !has; send.disabled = !has && !queue().length;
      send.textContent = queue().length ? `一起交（${queue().length + (has ? 1 : 0)} 处）` : '交给做课的对话'; };
    ta.oninput = sync;
    sh.querySelectorAll('.xe-q button').forEach(b => b.onclick = () => { const l = queue(); l.splice(+b.dataset.k, 1); save(KEY, l.length ? l : null); sh.remove(); refreshBtn(); open(); });
    sh.querySelector('input').onchange = async e => {
      if (!e.target.files[0]) return;
      image = await shrink(e.target.files[0]);
      sh.querySelector('.xe-th').innerHTML = `<img class="xe-thumb" src="${image}">`; sync();
    };
    const cur = () => ({section: h.section, section_no: h.section_no, section_title: h.section_title, screen: h.screen, where: h.where,
                        text: ta.value.trim(), images: image ? [image] : []});
    sh.querySelector('.xe-ask').onclick = async e => {
      const b = e.currentTarget; b.disabled = true; b.textContent = '正在问…';
      try {
        const r = await fetch('/_x/api/feedback', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({course: courseUrl, ask: true, items: [cur()]})});
        const d = await r.json(); if (!d.ok) throw new Error(d.error || '');
        sh.querySelector('.xe-box').innerHTML = `<b>问「${esc(d.name)}」了</b>
          <div class="xe-done"><a href="/_x/#s${d.num}">去看对话</a></div>
          <div class="xe-row"><button class="xe-send">接着试课</button></div>`;
        sh.querySelector('.xe-send').onclick = () => sh.remove();
      } catch (err) { b.disabled = false; b.textContent = '没问出去，再点一次'; }
    };
    sh.querySelector('.xe-cancel').onclick = () => sh.remove();
    sh.onclick = e => { if (e.target === sh) sh.remove(); };
    later.onclick = () => {
      // 截图存在本机浏览器里，太大存不下就只记文字
      const l = queue(); l.push(cur());
      try { localStorage.setItem(KEY, JSON.stringify(l)); } catch (e) { l[l.length - 1].images = []; save(KEY, l); }
      sh.remove(); refreshBtn();
    };
    send.onclick = async () => {
      send.disabled = true; later.disabled = true; send.textContent = '正在交…';
      const items = [...queue(), ...((ta.value.trim() || image) ? [cur()] : [])];
      try {
        const r = await fetch('/_x/api/feedback', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({course: courseUrl, items})});
        const d = await r.json();
        if (!d.ok) throw new Error(d.error || '没交出去');
        save(KEY, null); refreshBtn();
        const last = items[items.length - 1];
        save(SENT, {t: Date.now() / 1000, num: d.num, section: last.section, screen: last.screen, where: last.where});
        sh.querySelector('.xe-box').innerHTML = `<b>交给「${esc(d.name)}」了${items.length > 1 ? `（${items.length} 处）` : ''}</b>
          <div class="xe-done"><a href="/_x/#s${d.num}">去看对话</a></div>
          <div class="xe-row"><button class="xe-send">接着试课</button></div>`;
        sh.querySelector('.xe-send').onclick = () => sh.remove();
        poll();
      } catch (e) {
        send.disabled = false; later.disabled = false; send.textContent = '没交出去，再试一次';
      }
    };
    setTimeout(() => ta.focus(), 50);
  }

  let bar, fb, stEl;
  const refreshBtn = () => { if (!fb) return; const n = queue().length; fb.innerHTML = n ? `反馈这一屏 <em>${n}</em>` : '反馈这一屏'; };
  // 右下角的小条：这门课是哪个对话在改。交过反馈后它做完了，就变成“改好了 · 刷新到这一屏”
  let timer = 0;
  async function poll() {
    clearTimeout(timer);
    if (!courseUrl || !stEl) return;
    if (document.hidden) { timer = setTimeout(poll, 8000); return; }
    let d = null;
    try { d = await (await fetch('/_x/api/course?course=' + encodeURIComponent(courseUrl))).json(); } catch (e) {}
    const sent = load(SENT);
    stEl.className = 'xe-st'; stEl.hidden = true; stEl.onclick = null;
    if (d && d.num) {
      const go = () => { location.href = '/_x/#s' + d.num; };
      if (d.state === 'running') { stEl.hidden = false; stEl.innerHTML = `<i></i><span>#${d.num} 在改${d.step ? ' · ' + esc(d.step) : ''}${d.minutes ? ' · ' + d.minutes + ' 分钟' : ''}</span>`; stEl.onclick = go; }
      else if (d.state === 'awaiting') { stEl.hidden = false; stEl.classList.add('wait'); stEl.innerHTML = `<i></i><span>#${d.num} 等你定</span>`; stEl.onclick = go; }
      else if (sent && d.updated > sent.t) {
        stEl.hidden = false; stEl.classList.add('done'); stEl.innerHTML = `<span>改好了 · 刷新到${esc((sent.where || '这一屏').replace(/「[^」]*」/, '').replace(/\s+/g, ''))}</span>`;
        stEl.onclick = () => {
          save(SENT, null);
          const u = new URL(location.href);
          if (sent.section) u.searchParams.set('xe_sec', sent.section);
          if (Number.isInteger(sent.screen) && sent.screen >= 0) u.searchParams.set('xe_scr', sent.screen);
          location.href = u.toString();
        };
      }
    }
    timer = setTimeout(poll, d && d.state === 'running' ? 5000 : 15000);
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

  // 打开时就翻到要看的那一屏（案头回复里的“回到第 N 节第 M 屏”）
  async function jump() {
    const sec = params.get('xe_sec'), scr = params.get('xe_scr');
    if (!sec) return;
    if (!await until(() => typeof window.showSection === 'function' && document.querySelector('.cs-canvas-board'), 15000)) return;
    if (window.__CS_CURRENT_SECTION?.section_id !== sec) {
      window.showSection(sec);
      await until(() => window.__CS_CURRENT_SECTION?.section_id === sec, 5000);
    }
    const idx = scr === null ? NaN : Number(scr);
    if (Number.isInteger(idx) && idx >= 0 && Number(window.__CS_SCREEN_IDX) !== idx) {
      await until(() => { window.__cs_pinScreen?.(idx); return Number(window.__CS_SCREEN_IDX) === idx; }, 4000);
      window.__cs_pinScreen?.(null);
    }
    const u = new URL(location.href); u.searchParams.delete('xe_sec'); u.searchParams.delete('xe_scr');
    history.replaceState(history.state, '', u.toString());   // 再刷新就是普通的试课页
  }

  const add = () => {
    if (document.querySelector('.xe-bar')) return;
    bar = document.createElement('div'); bar.className = 'xe-bar';
    fb = document.createElement('button'); fb.className = 'xe-fb';
    fb.onclick = e => { e.stopPropagation(); open(); };
    stEl = document.createElement('button'); stEl.className = 'xe-st'; stEl.hidden = true;
    bar.append(fb, stEl); document.body.appendChild(bar);
    refreshBtn(); poll(); jump();
  };
  if (document.body) add(); else document.addEventListener('DOMContentLoaded', add);
})();
