// 做课扩展的界面：侧栏顶上的“课”、课程库、搜索里的课、回复和标题栏的“试课”、新对话“接着做《某课》”。
// 只通过 window.XE 挂到核心界面上（见 static/app/core.js 里 XE 的说明），核心的小工具从 XE.lib 拿。
(() => {
  const {$, esc, ICON, sheet, openSession, openNew, agoDays, WHERE_ICON, FOLDER_SAY} = XE.lib;
  let drafts = [], courses = [];

  WHERE_ICON.course = '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/><path d="m10 8.5 4 2-4 2z"/></svg>';
  WHERE_ICON.plat = '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M7 20h10"/><path d="M7 8h6M7 12h10"/></svg>';
  FOLDER_SAY['课程根'] = ['folder', '做课'];
  FOLDER_SAY['平台'] = ['plat', 'CyberScope'];
  if ($('#q')) $('#q').placeholder = '搜对话和课：标题、说过的话';

  XE.on('sessions', d => {
    const x = (d.ext || {}).course || {};
    drafts = x.drafts || []; courses = x.courses || [];
  });

  // 侧栏顶上的“课”：最近动过的几门课；点卡片进对话（没有就新开一个接着做），右边 ▶ 直接试课
  // 没在改时说这门课走到哪了：草稿 / 上架了但学生看不到 / 已发布；发布后又改过就提醒还没发布
  const stageText = c => c.stage === 'draft' ? '草稿' : c.stage === 'hidden' ? '已上架 · 学生看不到' : c.dirty ? '有改动没发布' : c.stage === 'published' ? '已发布' : '';
  let folded = false;
  try { folded = localStorage.getItem('coursesFolded') === '1'; } catch (e) {}
  function box() {
    let el = $('#courses');
    if (!el && $('#extSide')) { el = document.createElement('div'); el.id = 'courses'; $('#extSide').appendChild(el); }
    return el;
  }
  function renderCourses(query) {
    const el = box();
    if (!el) return;
    const cs = courses.slice(0, query ? 0 : 2);
    el.innerHTML = cs.length ? `<button class="course-toggle" aria-expanded="${!folded}" aria-controls="courseCards"><span>课</span>${ICON.chev}</button><div id="courseCards" ${folded ? 'hidden' : ''}>` + cs.map(c =>
      `<div class="ccard ${c.status || 'none'} ${c.dirty && !['running', 'queued', 'awaiting'].includes(c.status) ? 'dirty' : ''}"><button class="cgo" data-n="${c.num || ''}" data-d="${esc(c.dir)}">
        <span class="tx"><div class="nm">${esc(c.title)}</div></span></button>
        <a class="cplay" href="${esc(c.url)}" target="_blank" title="试课" aria-label="试课">${ICON.play}</a></div>`).join('')
      + (drafts.length > cs.length ? `<button class="call" id="callBtn">${ICON.list}<span>课程库 · 全部 ${drafts.length} 节</span></button>` : '') + '</div>' : '';
    const toggle = el.querySelector('.course-toggle');
    if (toggle) toggle.onclick = () => { folded = !folded; try { localStorage.setItem('coursesFolded', folded ? '1' : '0'); } catch (e) {} renderCourses(query); };
    el.querySelectorAll('.cgo').forEach(b => b.onclick = () => b.dataset.n ? openSession(+b.dataset.n) : openNew(false, 'c:' + b.dataset.d));
    if ($('#callBtn')) $('#callBtn').onclick = courseLib;
  }
  XE.on('side', ({query}) => renderCourses(query));

  // 课程库里的任何一节：▶ 试课；点名字去改——有对话在改这节就进那个对话，没有就新开一个接着做
  const libRow = c => `<div class="ccard"><button class="cgo lib" data-d="${esc(c.dir)}" data-rel="${esc(c.rel)}">
      <span class="tx"><div class="nm">${esc(c.title)}</div><div class="st">${esc([stageText(c), agoDays(c.mtime).replace('动过', '改过')].filter(Boolean).join(' · '))}</div></span></button>
      <a class="cplay" href="${esc(c.url)}" target="_blank" title="试课" aria-label="试课">${ICON.play}</a></div>`;
  const libMatch = (c, q) => (c.title + ' ' + c.dir).toLowerCase().includes(q.trim().toLowerCase());
  async function goCourse(b, after) {
    let num = null;
    try { num = (await (await fetch('/_x/ext/course/api/course?course=' + encodeURIComponent(`/workspace/${b.dataset.rel}/course.json`))).json()).num; } catch (e) {}
    if (after) after();
    num ? openSession(num) : openNew(false, 'c:' + b.dataset.d);
  }
  function courseLib() {
    const el = sheet(`<div class="bhead"><b>课程库</b><button class="bx" id="bx" aria-label="关闭">✕</button></div>
      <label class="find lfind"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg><input id="lq" type="text" placeholder="找课：课名里的几个字" autocomplete="off" enterkeyhint="search"></label>
      <div class="blist" id="blist"></div>`);
    el.querySelector('.box').classList.add('browse');
    const draw = () => {
      const q = $('#lq').value, cs = q.trim() ? drafts.filter(c => libMatch(c, q)) : drafts;
      $('#blist').innerHTML = cs.map(libRow).join('') || `<div class="bempty">没有叫“${esc(q)}”的课</div>`;
      $('#blist').querySelectorAll('.cgo').forEach(b => b.onclick = () => goCourse(b, () => el.remove()));
    };
    $('#bx').onclick = () => el.remove();
    $('#lq').oninput = draw;
    draw();
  }

  // 搜索：课排在对话前面
  XE.on('search', q => {
    const cs = drafts.filter(c => libMatch(c, q));
    if (!cs.length) return null;
    return {html: `<div class="label">课（${cs.length}）</div>` + cs.slice(0, 5).map(libRow).join(''),
            wire: () => $('#list').querySelectorAll('.cgo.lib').forEach(b => b.onclick = () => goCourse(b))};
  });

  // 试课链接：标上是哪门课；这门课你在试课页反馈过、这条回复是反馈之后改的，就直接跳回你反馈的那一屏
  function previewLink(x, it, d) {
    const c = courses.find(y => y.dir === x.key) || drafts.find(y => y.dir === x.key);
    const title = c ? c.title : (x.title || x.key);
    const fb = ((d.ext || {}).course || {}).feedback;
    if (fb && fb.section && x.url.includes(encodeURIComponent(fb.course)) && (!it.ts || it.ts > fb.t)) {
      const u = x.url + `&xe_sec=${encodeURIComponent(fb.section)}` + (Number.isInteger(fb.screen) && fb.screen >= 0 ? `&xe_scr=${fb.screen}` : '');
      return {...x, url: u, title, label: `回到${(fb.where || '那一屏').replace(/「[^」]*」/, '').replace(/\s+/g, '')}`};
    }
    return {...x, title, label: '试课'};
  }
  XE.on('link', (x, it, d) => x.ext === 'course' ? previewLink(x, it, d) : x);

  // 标题栏右边：这个对话最新改到的那门课，一点就试课
  XE.on('thread', d => {
    const last = [...(d.items || [])].reverse().find(it => it.who === 'claude' && (it.links || []).some(x => x.ext === 'course'));
    if (!last || !$('#headActs')) return;
    const x = previewLink(last.links.find(y => y.ext === 'course'), last, d);
    $('#headActs').innerHTML = `<a class="pill" href="${esc(x.url)}" target="_blank" title="${esc(x.title)}">${ICON.play}<span>试课</span></a>`;
  });

  // 新对话：从侧栏点了某门课（preset = c:目录名）就临时加一项“接着做《课名》”，发送时带 course
  XE.on('where', preset => {
    const c = (preset || '').startsWith('c:') && drafts.find(x => 'c:' + x.dir === preset);
    return c ? [{v: preset, ic: 'course', t: `接着做《${c.title}》`, send: {course: c.dir}}] : null;
  });
})();
