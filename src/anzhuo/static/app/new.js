// 新对话：和 Claude / ChatGPT 一样只有一个输入框；“在哪做”是输入框下面一个小按钮，默认远窗自动判断，要改才点开
function openNew(fromPop, preset) {
  setTimeout(connectEvents);
  sel = null; if (!fromPop) navTo({n: 1}, '#new'); $('#app').classList.add('in'); renderSide();
  // 菜单只留 自动 / 聊天 / FOLDER_SAY 里列的文件夹 / 电脑端对话 / 浏览文件夹…，不写解释的小字（2026-11-02、11-03定）；
  // 扩展给的（比如从侧栏点课）、或浏览选中的文件夹，只作为“这次选的”临时加一项，排在浏览前面
  const opts = [...(autoWhere ? [{v: 'auto', ic: 'auto', t: '自动'}] : []), {v: 'scratch', ic: 'chat', t: '聊天'},
    ...Object.keys(FOLDER_SAY).filter(f => folders.includes(f)).map(f => ({v: f, ic: FOLDER_SAY[f][0], t: FOLDER_SAY[f][1]}))];
  const choose = o => { const k = opts.findIndex(x => x.once); if (k >= 0) opts.splice(k, 1); opts.push({...o, once: true}); };
  XE.collect('where', preset).forEach(choose);
  let pick = preset && opts.some(o => o.v === preset) ? preset : opts[0].v, menu = false;
  $('#main').innerHTML = `<div class="head"><button class="icon-btn back" id="back">${ICON.back}</button><div class="ti"><b>新对话</b></div></div>
    <div class="newv"><h1>要做什么？</h1>${composerHTML('说说要做什么')}<div class="wbar" id="wbar"></div></div>`;
  $('#back').onclick = () => history.state && history.state.n ? history.back() : goList();
  const draw = () => {
    if (me.role === 'user') {   // 受限账号：只在自己的文件夹里做，不给选
      $('#wbar').innerHTML = `<span class="pill">${WHERE_ICON.folder}<span>在 ${esc(me.root || '你的文件夹')} 里做</span></span>`;
      return;
    }
    const cur = opts.find(o => o.v === pick);
    $('#wbar').innerHTML = `<button class="pill ${pick !== opts[0].v ? 'on' : ''}" id="wp">${WHERE_ICON[cur.ic]}<span>${esc(cur.v === 'auto' ? '在哪做：自动' : cur.t)}</span>${ICON.chev.replace('<svg', '<svg style="transform:rotate(90deg)"')}</button>`
      + (menu ? `<div class="wmenu">${opts.map(o => `<button class="wo ${o.v === pick ? 'on' : ''}" data-v="${esc(o.v)}">${WHERE_ICON[o.ic]}
          <span class="tx"><b>${esc(o.t)}</b></span></button>`).join('')}
          <button class="wo" id="wdesk">${WHERE_ICON.desk}<span class="tx"><b>电脑端对话</b></span></button>
          <button class="wo" id="wbrowse">${WHERE_ICON.folder}<span class="tx"><b>浏览文件夹…</b></span></button></div>` : '');
    if (menu) placeWhereMenu();
    $('#wp').onclick = e => { e.stopPropagation(); menu = !menu; draw(); };
    if ($('#wdesk')) $('#wdesk').onclick = e => { e.stopPropagation(); menu = false; draw(); deskSheet(); };
    if ($('#wbrowse')) $('#wbrowse').onclick = e => {
      e.stopPropagation(); menu = false; draw();
      browseSheet(pick, path => {
        if (!opts.some(o => o.v === path)) choose({v: path, ic: 'folder', t: path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || path});
        pick = path; draw(); $('#ta').focus();
      });
    };
    document.querySelectorAll('.wo[data-v]').forEach(c => c.onclick = e => { e.stopPropagation(); pick = c.dataset.v; menu = false; draw(); });
  };
  draw();
  $('.newv').onscroll = placeWhereMenu;
  $('#main').onclick = e => { if (menu && !e.target.closest('.wmenu')) { menu = false; draw(); } };
  const ms = {model: 'auto', effort: ''};   // 深度空 = 没动过，照分工表或默认
  renderModel(ms, () => {}, true);
  const extra = () => ({...(ms.model !== 'auto' ? {model: ms.model} : {}), ...(ms.effort ? {effort: ms.effort} : {})});
  wireComposer((text, onProg, attachments) => sendApi({...((opts.find(o => o.v === pick) || {}).send || {folder: pick}), ...extra(), text, ...attachments}, onProg),
    null, (text, imgs) => {
      // 一按发送就进“正在开”：你的话先摆出来，说清楚在等什么、能不能走开（开好了自动进去）
      const old = $('#opening'); if (old) old.remove(); if (text === null) return;
      const el = document.createElement('div'); el.id = 'opening'; el.className = 'opening';
      el.innerHTML = `<div class="col"><div class="you">${youImgs(imgs || [])}${esc(text)}</div>
        <div class="live"><div class="lh"><i class="spin"></i><span class="lt">正在开新对话</span></div>
        <div class="lsub">已送到</div></div></div>`;
      $('.newv').appendChild(el);
    });
  if (!coarsePhone() && !document.documentElement.classList.contains('ipad')) $('#ta').focus();
}
$('#newBtn').onclick = () => openNew();
$('#fab').onclick = () => openNew();

