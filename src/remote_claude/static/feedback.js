// 试课页上的“反馈这一屏”：小E 转发试课预览时插进页面（不改 CyberScope 本身）。
// 引擎自己记着翻到第几节第几屏（__CS_CURRENT_SECTION / __CS_SCREEN_IDX），点一下写一句话（可附 iPad 截屏后圈画的图），直接交给做这门课的对话。
(() => {
  if (window.__xiaoeFeedback) return;
  window.__xiaoeFeedback = true;
  const courseUrl = new URLSearchParams(location.search).get('course') || '';
  let manifest = null;
  const loadManifest = async () => {
    if (manifest || !courseUrl) return manifest;
    try { manifest = await (await fetch(courseUrl)).json(); } catch (e) { manifest = {}; }
    return manifest;
  };
  const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const css = `
.xe-fb{position:fixed;left:calc(14px + env(safe-area-inset-left));bottom:calc(14px + env(safe-area-inset-bottom));z-index:2147483000;
  height:40px;padding:0 14px;border-radius:20px;border:1px solid rgba(0,0,0,.1);background:rgba(255,255,255,.82);
  -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);box-shadow:0 6px 20px rgba(0,0,0,.12);
  font:600 15px -apple-system,"PingFang SC",sans-serif;color:#1c1c1e;display:flex;align-items:center;gap:6px;opacity:.9}
.xe-fb:active{transform:scale(.97)}
.xe-sh{position:fixed;inset:0;z-index:2147483001;background:rgba(0,0,0,.35);display:flex;align-items:flex-end;justify-content:center;
  font:16px/1.5 -apple-system,"PingFang SC",sans-serif;color:#1c1c1e}
.xe-box{width:min(560px,100%);background:#fff;border-radius:20px 20px 0 0;padding:18px 18px calc(18px + env(safe-area-inset-bottom));box-shadow:0 -6px 30px rgba(0,0,0,.15)}
@media (min-width:700px){.xe-sh{align-items:center}.xe-box{border-radius:20px}}
.xe-box b{display:block;font-size:17px}.xe-where{color:#6b6b70;font-size:15px;margin:2px 0 12px}
.xe-box textarea{width:100%;box-sizing:border-box;min-height:96px;border:1px solid rgba(0,0,0,.12);border-radius:12px;padding:10px 12px;font-family:inherit;font-size:16px;line-height:1.5;resize:vertical;outline:none}
.xe-row{display:flex;gap:8px;margin-top:10px;align-items:center}
.xe-row label,.xe-row button{height:44px;border-radius:12px;padding:0 14px;font-family:inherit;font-size:16px;border:0;background:#f2f2ef;color:#1c1c1e;display:flex;align-items:center}
.xe-row .xe-send{margin-left:auto;background:#2f6fdf;color:#fff;font-weight:600}
.xe-row .xe-send:disabled{opacity:.5}
.xe-thumb{height:44px;border-radius:8px;border:1px solid rgba(0,0,0,.1)}
.xe-done{font-size:16px;margin:8px 0 4px}.xe-done a{color:#2f6fdf}`;
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

  async function open() {
    const m = await loadManifest() || {};
    const section = (window.__CS_CURRENT_SECTION && window.__CS_CURRENT_SECTION.section_id) || '';
    const secs = m.sections || [];
    const i = secs.findIndex(x => x.id === section);
    const screen = typeof window.__CS_SCREEN_IDX === 'number' ? window.__CS_SCREEN_IDX : -1;
    const where = [i >= 0 ? `第 ${i + 1} 节「${secs[i].title || secs[i].id}」` : '', screen >= 0 ? `第 ${screen + 1} 屏` : ''].filter(Boolean).join(' · ') || '当前这一屏';
    const sh = document.createElement('div'); sh.className = 'xe-sh';
    sh.innerHTML = `<div class="xe-box"><b>反馈这一屏</b><div class="xe-where">《${esc(m.title || '这门课')}》${esc(where)}</div>
      <textarea placeholder="哪里要改？比如：标题太小、这句话太长、图挡住了按钮"></textarea>
      <div class="xe-row"><label>加截图<input type="file" accept="image/*" hidden></label><span class="xe-th"></span>
      <button class="xe-cancel">取消</button><button class="xe-send" disabled>交给做课的对话</button></div></div>`;
    document.body.appendChild(sh);
    const ta = sh.querySelector('textarea'), send = sh.querySelector('.xe-send');
    let image = '';
    const sync = () => { send.disabled = !ta.value.trim() && !image; };
    ta.oninput = sync;
    sh.querySelector('input').onchange = async e => {
      if (!e.target.files[0]) return;
      image = await shrink(e.target.files[0]);
      sh.querySelector('.xe-th').innerHTML = `<img class="xe-thumb" src="${image}">`; sync();
    };
    sh.querySelector('.xe-cancel').onclick = () => sh.remove();
    sh.onclick = e => { if (e.target === sh) sh.remove(); };
    send.onclick = async () => {
      send.disabled = true; send.textContent = '正在交…';
      try {
        const r = await fetch('/_x/api/feedback', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({course: courseUrl, section, section_no: i + 1, section_title: i >= 0 ? (secs[i].title || '') : '',
                                screen, text: ta.value.trim(), images: image ? [image] : []})});
        const d = await r.json();
        if (!d.ok) throw new Error(d.error || '没交出去');
        sh.querySelector('.xe-box').innerHTML = `<b>交给「${esc(d.name)}」了</b>
          <div class="xe-done">它改完会通知你，回来刷新这页就能看到。<a href="/_x/#s${d.num}">去看对话</a></div>
          <div class="xe-row"><button class="xe-send">继续试课</button></div>`;
        sh.querySelector('.xe-send').onclick = () => sh.remove();
      } catch (e) {
        send.disabled = false; send.textContent = '没交出去，再试一次';
      }
    };
    setTimeout(() => ta.focus(), 50);
  }

  const add = () => {
    if (document.querySelector('.xe-fb')) return;
    const b = document.createElement('button'); b.className = 'xe-fb'; b.textContent = '反馈这一屏';
    b.onclick = e => { e.stopPropagation(); open(); };
    document.body.appendChild(b);
  };
  if (document.body) add(); else document.addEventListener('DOMContentLoaded', add);
})();
