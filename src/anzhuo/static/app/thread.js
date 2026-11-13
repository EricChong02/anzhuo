// 进行中用了多久：按秒走，每秒自己跳（不用等服务器）
const dur = sec => { sec = Math.max(0, Math.floor(sec)); return sec < 60 ? `${sec} 秒` : `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`; };
setInterval(() => { document.querySelectorAll('.tick').forEach(t => { t.textContent = dur(Date.now() / 1000 - +t.dataset.t0); }); tickSilence(); }, 1000);
// 你发的图：气泡里一排小图，点开看大图
const youImgs = us => us.length ? `<div class="imgs yimgs">${us.map(u => `<a href="${esc(u)}"><img src="${esc(u)}"></a>`).join('')}</div>` : '';
function renderThread(d) {
  const feed = $('#feed'); if (!feed) return;
  const near = stick, savedTop = feed.scrollTop;
  const saidList = d.items.filter(i => i.who === 'you' && !i.pending).map(i => i.text.trim().replace(/^\[(?:\d+ 张)?图片\]\s*/, ''));
  const relayTail = x => x.startsWith('（这是接力的新对话') && x.includes('用户这次说：\n\n') ? x.split('用户这次说：\n\n').pop() : x;   // 接力对话第一句：记录里带着交接说明
  // 记录里可能多了“（附件已存到…）”，按开头比；只发图或文件时网页摆的是“[图片]”，记录里是空的或“（见图）”，对不上字——就按“记录里你说的话多了一条”算
  const sayCount = t => { const k = /^\[(?:图片|附件)\]$/.test(t) ? '' : t.slice(0, 40); return saidList.filter(x => x.startsWith(k) || relayTail(x).startsWith(k)).length; };
  lastSaid[d.num] = sayCount;
  lastYou[d.num] = saidList.filter(Boolean).slice(-1)[0] || '';
  d.items.forEach(i => { if (i.pending) i.cls = 'pending'; });
  (pending[d.num] || []).forEach(p => { if ((p.imgs || []).length) sentImgs.set(p.text.trim(), p.imgs); });  // 刚发的图记着（服务器那边的待发只有字）
  const localImgs = sentImgs;
  // 服务器那边也报了这条“待发”（插话、排队）时，网页自己先摆的那条就不再重复显示
  const srvPend = d.items.filter(i => i.who === 'you' && i.pending).map(i => i.text.trim());
  const inSrv = t => srvPend.some(x => x.startsWith(t.slice(0, 40)) || t.startsWith(x.slice(0, 40)));
  pending[d.num] = (pending[d.num] || []).filter(p => !(sayCount(p.text.trim()) > (p.base || 0)));   // 比发出时多出一条才算进了记录，同一句话再发不会被旧的那条顶替
  const localShow = pending[d.num].filter(p => !inSrv(p.text.trim()));
  d.items.forEach(i => { if (i.pending && !(i.imgs || []).length && localImgs.has(i.text.trim())) i.local = localImgs.get(i.text.trim()); });
  d.items = d.items.flatMap(saSplit);
  const replies = d.items.filter(i => i.who === 'claude');
  const keep = new Set(replies.slice(-2));
  // 新回复来了、你正跟着看（在底部或刚才它在干活）：停在新回复开头，从头读；后面的图再加载也不会把你挤走
  const prevN = replyCount[d.num], fresh = prevN !== undefined && replies.length > prevN && (near || wasLive[d.num]);
  replyCount[d.num] = replies.length; wasLive[d.num] = !!d.live;
  const queued = new Set(d.queue || []);
  const shownQ = new Set(d.items.filter(i => i.pending).map(i => i.text.slice(0, 80)));
  // 你某句话之前的那条回复：回退、分叉都从那里接着。前面没有回复 = ''（第一句）；那条在压缩之前 = null（回不去）
  const prevEnd = k => { for (let j = k - 1; j >= 0; j--) if (d.items[j].historical || d.items[j].handoff !== undefined) return null; else if (d.items[j].who === 'claude') return d.items[j].end || null; return ''; };
  let h = d.items.map((it, idx) => {
    // 总管收到的回报、退回、提问、重启提示（super_agent）：不是你说的话，折成一张卡，展开按 Markdown 显示；总管接着会说
    const tag = it.who === 'you' && SA_TAGS.find(t => it.text.startsWith(t));
    if (tag) {
      const [head, ...rest] = it.text.slice(tag.length).split('\n\n');
      return `<details class="note rpt"><summary>${tag.slice(1, -1)} · ${esc(head)}</summary><div class="md">${md(rest.join('\n\n') || '（没有内容）')}</div></details>`;
    }
    if (it.who === 'note' && it.sa) return `<details class="note rpt"><summary>${esc(it.text)}</summary><div class="md">${md(it.sa)}</div></details>`;
    if (it.who === 'you') {
      const isQ = it.pending && queued.has(it.text.slice(0, 80));
      // 你发出去的话下面写清楚它现在怎么样了：排着、插进去了、还是已经在做——不用猜要不要守着
      const req = d.live && (d.live.request || '').replace(/…$/, '');
      const tag = !it.pending ? '' : isQ ? '排队中' : d.live ? (req && it.text.startsWith(req) ? '' : '插话') : (it.age > 30 ? '没开工' : '已送到');
      const before = prevEnd(idx);
      const btns = ib('copy', '复制') + (it.historical ? ib('bring', '带给当前会话') : '') + (!it.historical && (isQ || (!it.pending && before)) ? ib('back', '回退') : '') + (!it.historical && !it.pending && before !== null ? ib('fork', '分叉') : '');
      return `<div class="yw" data-y="${idx}" data-q="${isQ ? 1 : ''}"><div class="you ${it.pending ? 'pending' : ''}">${youImgs(it.local || (it.imgs || []).map(r => `/_x/api/uimg/${it.source_num || d.num}/${r}`))}${esc(it.text)}</div>
        <div class="ymeta"><span class="ibs">${btns}</span>${tag ? `<span class="ys">${tag}</span>` : ''}</div></div>`;
    }
    if (it.who === 'note' && it.handoff !== undefined) return `<details class="note"><summary>${esc(it.text)}</summary><div class="md">${md(it.handoff || '无交接说明')}</div></details>`;
    if (it.who === 'note' && it.relay) return `<div class="note rw">${esc(it.text)}<button class="relaygo" data-n="${it.relay}">过去</button></div>`;
    if (it.who === 'note') return it.rewind
      ? `<div class="note rw">${esc(it.text)}${it.fork ? '' : '<button class="rwundo">撤销</button>'}</div>` : `<div class="note">${esc(it.text)}</div>`;
    const acts = [it.tid && !(it.process && it.process.length) ? `<a class="pill" href="/_x/t/${it.tid}" target="_blank">${ICON.list}过程</a>` : '',
      ...(it.links || []).map(x => linkPill(x, it, d))].join('');
    const id = d.num + ':' + idx, fold = !keep.has(it) && it.text.length > 160 && !opened.has(id);
    const imgs = (it.images || []).length ? `<div class="imgs">${it.images.map(u => `<a href="${u}" target="_blank"><img src="${u}" loading="lazy"></a>`).join('')}</div>` : '';
    const files = (it.files || []).map(f => `<div class="file-row"><a class="file" href="${esc(f.url)}${/\.pdf$/i.test(f.name) ? '?view=1' : ''}" target="_blank" rel="noopener">${ICON.doc}<span class="fn">${esc(f.name)}</span><span class="fs">${size(f.size)}</span></a><button class="file-download file-share" data-file-url="${esc(f.url)}" data-file-name="${esc(f.name)}" aria-label="分享 ${esc(f.name)}"${navigator.share ? '' : ' hidden'}>分享</button><a class="file-download" href="${esc(f.url)}?download=1" download="${esc(f.name)}" aria-label="下载 ${esc(f.name)}">下载</a></div>`).join('');
    const elapsed = Number.isFinite(it.elapsed) && it.elapsed >= 0 ? `<span class="process-time">耗时 ${dur(it.elapsed)}</span>` : '';
    const proc = it.process && it.process.length ? (() => {
      const pid = d.num + ':p' + idx, op = openProc.has(pid), thinks = it.process.filter(e => e.k === 'think').length;
      return `<div class="process-summary"><button class="ptog ${op ? 'open' : ''}" data-p="${pid}">${ICON.chev}<span class="process-title">工作过程 · ${it.steps} 步${thinks ? ' · 含思考' : ''}</span></button>${elapsed}</div>${op ? procHTML(it.process, pid, it.tid) : ''}`;
    })() : elapsed ? `<div class="process-summary time-only">${elapsed}</div>` : '';
    // 回复底部只保留模型与交付信息；耗时放在工作过程右侧
    const facts = [it.model_label ? it.model_label.replace(/^Claude /, '') : '', it.nfiles ? `改 ${it.nfiles} 个文件` : '', (it.images || []).length ? `${it.images.length} 张图` : '',
      (it.files || []).length ? `${it.files.length} 个文件给你` : ''].filter(Boolean);
    const btns = `<span class="ibs">${ib('copy', '复制')}${it.historical ? ib('bring', '带给当前会话') : it.end ? ib('fork', '分叉') : ''}${it.tid || it.end ? ib('listen', '朗读') : ''}</span>`;
    return `<div class="cl ${fold ? 'fold' : ''}" data-i="${idx}">${proc}${imgs}<div class="md">${md(it.text)}</div>${fold ? `<button class="more" data-id="${id}">展开全部</button>` : ''}${files}
      <div class="acts">${btns}${facts.length ? `<span class="facts">${facts.join(' · ')}</span>` : ''}${acts}</div></div>`;
  }).join('');
  h += localShow.map(p => `<div class="you pending">${youImgs(p.imgs || [])}${esc(p.text === '[图片]' && (p.imgs || []).length ? '' : p.text)}</div>`
    + `<div class="ymeta"><span class="${p.acked ? '' : 'sending'}">${p.acked ? (Date.now() - p.t > 30000 && !d.live ? '没开工' : '已送到') : p.quick ? '3 秒后发出' : '发送中…'}</span></div>`).join('');
  if (!d.items.length && !d.live && !localShow.length) h = '<div class="note">这个对话还没有内容</div>';
  if (d.live) {
    const L = d.live, first = L.total - L.steps.length + 1;
    if (L.compact) h += `<div class="live"><div class="lh"><i class="spin"></i><span>正在压缩上下文 · 大约一分钟</span></div></div>`;
    else {
      // 一大行说清：第几步、在做什么、做了多久；有步骤清单按清单算，还能估个大概还要多久
      const plan = L.plan || [], done = plan.filter(x => x.status === 'done').length, doing = plan.find(x => x.status === 'doing');
      const t0 = Date.now() / 1000 - (L.elapsed ?? L.minutes * 60);
      const head = plan.length ? esc((doing || plan.find(x => x.status !== 'done') || plan[plan.length - 1]).text)
        : L.total ? esc(L.steps[L.steps.length - 1] || '') : L.elapsed < 20 ? '收到了，正在开工' : '在想…';
      const at = plan.length ? `第 ${Math.min(plan.length, done + 1)}/${plan.length} 步 · ` : L.total ? `第 ${L.total} 步 · ` : '';
      const popen = openProc.has('plan');
      const seg = plan.length > 1 ? `<button class="pbar" data-p="plan" aria-label="${popen ? '收起步骤' : '看全部步骤'}">${plan.map(x => `<i class="${x.status}"></i>`).join('')}</button>` : '';
      const left = plan.length - done, eta = plan.length && done >= 2 && left > 0 ? Math.max(1, Math.round((L.elapsed / done) * left / 60)) : 0;
      const pop = openProc.has('live');
      h += `<div class="live"><div class="lh"><i class="spin"></i><span class="lt">${head}</span><button class="stop" id="stop">${ICON.stop}停止</button></div>
      <div class="lsub">${at}<b class="tick" data-t0="${t0}">${dur(L.elapsed ?? L.minutes * 60)}</b>${eta ? ` · 预计还要约 ${eta} 分钟` : ''}${L.injected ? ` · 插话 ${L.injected} 次` : ''}</div>
      ${seg}${popen && plan.length > 1 ? planHTML(plan) : ''}
      <div class="silence" data-t0="${Date.now() / 1000 - (L.idle || 0)}"></div>${agentsHTML(L.agents || [])}
      ${(L.process || []).length ? `<button class="ptog ${pop ? 'open' : ''}" data-p="live">${ICON.chev}看过程 · ${L.process.length} 条</button>${pop ? procHTML(L.process, 'live') : ''}` : ''}</div>`;
    }
  }
  const restQ = (d.queue || []).filter(q => !shownQ.has(q));
  if (restQ.length) h += `<div class="queue">接下来：${restQ.map(esc).join('；')}</div>`;
  if (d.live && d.live.confirm) h += `<div class="confirm"><div class="q">等你确认：${esc(d.live.confirm.what)}</div>
      <div class="cw"><button class="ib cpy" data-tip="复制" aria-label="复制"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M15 9V6.5A2.5 2.5 0 0 0 12.5 4h-6A2.5 2.5 0 0 0 4 6.5v6A2.5 2.5 0 0 0 6.5 15H9"/></svg></button><pre>${esc(d.live.confirm.cmd)}</pre></div><div class="cb"><button class="ok" data-allow="1">允许</button><button data-allow="0">不允许</button></div>
</div>`;
  if (d.limit) h += `<div class="limit"><div class="q">额度用完了 · ${esc(d.limit.at)} 恢复${d.limit.auto ? '后自动接着做' : ''}</div>
      ${(d.relay_targets || []).length ? `<div class="lw">不想等：换一家接着做，远窗把这边的记录整理成交接材料带过去。</div>` : ''}
      <div class="lb">${(d.relay_targets || []).map(a => relayAlt(a, `换 ${a.label} 接着做`, 'pill on')).join('')}<button class="pill" id="autoRes">${d.limit.auto ? '别自动接' : '恢复后自动接着做'}</button></div></div>`;
  if (d.decision) h += `<div class="ask">${d.decision.options.map(([k, v]) =>
      `<button class="opt ${k === d.decision.recommend ? 'rec' : ''}" data-k="${k}" data-v="${esc(v)}"><span class="k">${k}</span><span class="v">${esc(v)}</span>${k === d.decision.recommend ? '<span class="tag">推荐</span>' : ''}</button>`).join('')}</div>`;
  setTimeout(showPartial);
  const pos = {};
  document.querySelectorAll('.plist[data-pid]').forEach(x => { pos[x.dataset.pid] = {top: x.scrollTop, end: x.dataset.reading !== '1' && x.scrollHeight - x.scrollTop - x.clientHeight < 4, reading: x.dataset.reading}; });
  $('#col').innerHTML = h;
  document.querySelectorAll('.plist[data-pid]').forEach(x => { const p = pos[x.dataset.pid]; if (p && !p.end) x.scrollTop = p.top; else x.scrollTop = x.scrollHeight;
    if (p && p.reading) x.dataset.reading = p.reading;
    const pause = () => { x.dataset.reading = '1'; stick = false; };
    const resume = () => { if (x.scrollHeight - x.scrollTop - x.clientHeight < 4) delete x.dataset.reading; };
    x.addEventListener('wheel', e => { if (e.deltaY < 0) pause(); else resume(); }, {passive: true});
    x.addEventListener('touchmove', pause, {passive: true});
    x.addEventListener('touchend', () => setTimeout(resume, 0), {passive: true});
    x.addEventListener('pointerdown', pause);
    x.addEventListener('pointerup', () => setTimeout(resume, 0));
    x.addEventListener('keyup', resume);
    x.addEventListener('scroll', () => { const gap = x.scrollHeight - x.scrollTop - x.clientHeight; if (gap > 40) { x.dataset.reading = '1'; stick = false; } else if (gap < 4) delete x.dataset.reading; }, {passive: true});
    const jb = x.querySelector('.pjump');
    if (jb) jb.onclick = e => { e.stopPropagation(); delete x.dataset.reading; x.scrollTop = x.scrollHeight; };
    x.addEventListener('keydown', e => { if (['ArrowUp', 'PageUp', 'Home'].includes(e.key)) pause(); });
  });
  document.querySelectorAll('.confirm [data-allow]').forEach(b => b.onclick = async () => {
    const allow = b.dataset.allow === '1'; b.parentElement.querySelectorAll('button').forEach(x => x.disabled = true);
    b.innerHTML = '<i class="spin"></i>' + (allow ? '允许' : '不允许');
    await api('confirm/' + sel, {allow}).catch(() => {}); lastKey = ''; loadThread();
  });
  document.querySelectorAll('.more').forEach(b => b.onclick = () => { opened.add(b.dataset.id); lastKey = ''; renderThread(d); });
  document.querySelectorAll('.ptog, .plan[data-p], .pbar').forEach(b => b.onclick = () => { const k = b.dataset.p; openProc.has(k) ? openProc.delete(k) : openProc.add(k);
    anchor = {sel: `.${b.classList[0]}[data-p="${CSS.escape(k)}"]`, top: b.getBoundingClientRect().top}; stick = false; lastKey = ''; renderThread(d); });
  document.querySelectorAll('.pt[data-c], .pd[data-c]').forEach(c => c.onclick = () => {
    const k = c.dataset.c; c.classList.toggle('clamp') ? unclamp.delete(k) : unclamp.add(k); });
  // 你说的话：有鼠标时悬停出图标，手机上点一下气泡出图标
  document.querySelectorAll('.yw .you').forEach(b => b.onclick = e => {
    if (e.target.closest('img, a') || String(getSelection() || '')) return;
    b.parentElement.classList.toggle('show');
  });
  document.querySelectorAll('.ib[data-act]').forEach(b => b.onclick = e => {
    e.stopPropagation();
    const y = b.closest('.yw'), c = b.closest('.cl'), act = b.dataset.act;
    if (act === 'bring') {
      const it = d.items[+(y ? y.dataset.y : c.dataset.i)], ta = $('#ta');
      ta.value += (ta.value ? '\n\n' : '') + '请参考之前这段聊天：\n' + it.text;
      ta.oninput(); ta.focus(); toast('已放入输入框，发送后才带入上下文'); return;
    }
    if (y) {
      const i = +y.dataset.y, text = d.items[i].text.replace(/\n\n（附件已存到[\s\S]*$/, '');
      if (act === 'copy') copyBtn(b, text);
      else if (act === 'back') y.dataset.q ? unqueue(d.num) : rewindSheet(d, prevEnd(i), true);
      else forkAt(d, prevEnd(i) || '', text);
    } else if (c) {
      const it = d.items[+c.dataset.i];
      if (act === 'copy') copyBtn(b, it.text); else if (act === 'listen') speechPlayer.start({...d, num: it.source_num || d.num}, it); else forkAt(d, it.end, '');
    }
  });
  document.querySelectorAll('.relaygo').forEach(b => b.onclick = () => openSession(+b.dataset.n));
  document.querySelectorAll('.rwundo').forEach(b => b.onclick = async () => { b.disabled = true; const r = await api('rewind/' + d.num, {cancel: true}).catch(() => null); if (!r || !r.ok) toast(r?.error || '没能撤销，请重试'); lastKey = ''; loadThread(); loadSessions(); });
  if ($('.limit')) bindRelayAlts($('.limit'), d.num, true);
  if ($('#autoRes')) $('#autoRes').onclick = async () => { await manage(d.num, 'autoresume', !d.limit.auto); lastKey = ''; loadThread(); };
  const q = $('#quick'), lastReply = replies.length ? replies[replies.length - 1].text : '';
  const me = sessions.find(x => x.num === d.num) || {};
  const approve = (lastReply.match(/批准本会话编辑：[^\n`]+/g) || []);
  const yesNo = /(吗|么|是否|要不要|对不对|行不行|可不可以|好不好|对吧)[？?]\s*$/.test((d.awaiting || '').trim());
  // 按它最后说的给几个最可能的回答：批准编辑、是 / 不是、好的 / 按推荐、做完顺口的建议、停了或出错了就“继续”
  const quick = d.live || d.decision || d.limit ? []
    : approve.length ? [[`批准（${approve.length} 处）`, approve.join('\n')], ['先不改', '先不改']]
    : d.awaiting_kind === 'suggest' ? [['好，做吧', '好，做吧'], ['不用了', '不用了']]
    : d.awaiting && yesNo ? [['是', '是'], ['不是', '不是'], ['先不做', '先不做']]
    : d.awaiting ? [['好的', '好的'], ['就按你推荐的', '就按你推荐的'], ['先不做', '先不做']]
    : me.status === 'error' || /\[Request interrupted|已停下|停下了/.test(lastReply.slice(-80)) ? [['继续', '继续']] : [];
  const canDismiss = !d.live && !d.limit && (d.awaiting || me.status === 'error');
  q.hidden = !quick.length && !canDismiss;
  q.innerHTML = quick.map(([t], i) => `<button data-i="${i}">${esc(t)}</button>`).join('') + (canDismiss ? '<button data-dismiss="1">不用回答了</button>' : '');
  const dm = q.querySelector('[data-dismiss]');
  if (dm) dm.onclick = async () => { q.hidden = true; if (await manage(d.num, 'dismiss')) { lastKey = ''; loadThread(); } };
  q.querySelectorAll('button[data-i]').forEach(b => {
    const text = quick[+b.dataset.i][1];
    let timer = 0, longed = false;
    const fill = () => { longed = true; const ta = $('#ta'); ta.value = text; ta.oninput(); ta.focus(); };
    b.addEventListener('touchstart', () => { longed = false; timer = setTimeout(fill, 450); }, {passive: true});
    b.addEventListener('touchend', () => clearTimeout(timer)); b.addEventListener('touchmove', () => clearTimeout(timer), {passive: true});
    b.oncontextmenu = e => { e.preventDefault(); fill(); };
    b.onclick = () => { if (longed) { longed = false; return; } q.hidden = true; quickSend(d.num, text, b.textContent); };
  });
  renderRelay(d);
  const hint = $('#hint'), fh = d.five_hour, risky = fh && fh.pct >= 90 && d.usage && (d.usage.mkey || 'opus') === 'opus' && !d.live && !d.limit;
  if (hint) { hint.hidden = !risky; hint.textContent = risky ? `5 小时额度已用 ${fh.pct}%${fh.reset ? ` · ${fh.reset} 重置` : ''}` : ''; }
  renderUsage(d.usage);
  if (!mpOpen && d.usage) renderModel({model: d.usage.mkey || 'opus', label: d.usage.model === '默认模型' ? '' : d.usage.model, effort: d.usage.ekey || 'medium'}, st => api('settings/' + sel, st), false);
  $('#headActs').innerHTML = '';
  XE.emit('thread', d);   // 标题栏右边留给扩展
  $('#ta').placeholder = d.live && d.live.compact ? '说点什么…' : d.live ? '插话…' : d.awaiting && d.awaiting_kind !== 'suggest' ? '回答它的问题…' : '说点什么…';
  if ($('#stop')) $('#stop').onclick = async e => { e.currentTarget.innerHTML = '<i class="spin"></i>停止中'; e.currentTarget.disabled = true; await api('stop/' + d.num, {}); loadThread(); };
  document.querySelectorAll('.opt').forEach(b => b.onclick = async () => {
    document.querySelectorAll('.opt').forEach(x => x.disabled = true);
    b.querySelector('.k').innerHTML = '<i class="spin"></i>';
    const text = `选 ${b.dataset.k}：${b.dataset.v}`;
    (pending[d.num] ||= []).push({text, t: Date.now(), base: saidBase(d.num, text)});
    await api('send', {num: d.num, text}); lastKey = ''; loadThread(); loadSessions();
  });
  tickSilence();   // 先填好“没动静”那行，再定位置
  const cls = document.querySelectorAll('#col .cl'), lastCl = cls[cls.length - 1];
  const an = anchor && document.querySelector('#col ' + anchor.sel);
  if (an) feed.scrollTop += an.getBoundingClientRect().top - anchor.top, anchor = null;
  else if (near && fresh && lastCl) stick = false, feed.scrollTop += lastCl.getBoundingClientRect().top - feed.getBoundingClientRect().top - 12;
  else if (near) feed.scrollTop = feed.scrollHeight;
  else feed.scrollTop = savedTop;
}
const replyCount = {}, wasLive = {}, sentImgs = new Map();

// 回复底下的链接按钮（扩展附的）：扩展可以先改一改地址和字
function linkPill(x0, it, d) {
  const x = XE.pipe('link', {...x0}, it, d);
  return `<a class="pill" href="${esc(x.url)}" target="_blank" title="${esc(x.title || '')}">${ICON[x.icon] || ICON.list}<span>${esc(x.label || '打开')}</span>${x.title ? `<small>${esc(x.title)}</small>` : ''}</a>`;
}
// 快捷回答：先显示出去，3 秒内能撤回（点错了不至于真发出去）
function quickSend(num, text, label) {
  const item = {text, t: Date.now(), quick: true, base: saidBase(num, text)};
  (pending[num] ||= []).push(item); lastKey = ''; loadThread();
  const timer = setTimeout(async () => { item.quick = false; lastKey = ''; loadThread();
    const r = await api('send', {num, text}).catch(() => null); if (r && r.ok) item.acked = true; else { pending[num] = (pending[num] || []).filter(p => p !== item); toast('没发出去，再点一次'); }
    lastKey = ''; loadThread(); loadSessions(); }, 3000);
  toast(`发出“${label.length > 12 ? label.slice(0, 12) + '…' : label}”`, '撤回', () => {
    clearTimeout(timer); pending[num] = (pending[num] || []).filter(p => p !== item); lastKey = ''; loadThread();
  }, 3000);
}
// 撤回还排着没开始的那句：放回输入框
async function unqueue(num) {
  const r = await api('unqueue/' + num, {}).catch(() => null);
  if (!r || !r.ok) return toast((r && r.error) || '没撤回，再试一次');
  pending[num] = []; const ta = $('#ta'); if (ta) { ta.value = r.text; ta.oninput(); ta.focus(); }
  toast('已撤回'); lastKey = ''; loadThread();
}
// 回到某条回复：之后的收起，下一句从这里接着说；可选把之后改过的文件也退回去
function rewindSheet(d, to, undoLast) {
  const k = d.items.findIndex(i => i.who === 'claude' && i.end === to); if (k < 0) return toast('回不去了（在压缩之前）');
  const n = d.items.slice(k + 1).filter(i => i.who === 'you').length;
  const el = sheet(`<h2 id="rewindTitle">${undoLast ? '回退到这句之前？' : '回到这条回复？'}</h2>
    <p class="rewind-summary">${undoLast ? `收起这句${n > 1 ? `及之后 ${n - 1} 轮对话` : ''}，原话放回输入框。` : `收起之后 ${n} 轮对话，从这里继续。`}${d.live ? '正在进行的任务会先停止。' : ''}</p>
    <label class="rewind-option"><span>同时恢复文件</span><input type="checkbox" id="rwf"></label>
    <div class="rewind-error" id="rewindError" role="alert" hidden></div>
    <div class="rewind-actions"><button id="rwno">取消</button><button class="on" id="rwok">确认回退</button></div>`);
  el.classList.add('rewind-dialog');
  const panel = el.querySelector('.box'); panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-modal', 'true'); panel.setAttribute('aria-labelledby', 'rewindTitle');
  const escape = e => { if (e.key === 'Escape' && !el.querySelector('#rwok').disabled) el.remove(); };
  el.addEventListener('keydown', escape); el.querySelector('#rwno').focus();
  el.querySelector('#rwno').onclick = () => el.remove();
  el.querySelector('#rwok').onclick = async () => {
    const b = el.querySelector('#rwok'); b.disabled = true; el.querySelector('#rwno').disabled = true; el.onclick = e => e.stopPropagation(); b.innerHTML = '<i class="spin"></i>' + (d.live ? '先停下再回去…' : '正在回去…');
    const r = await api('rewind/' + d.num, {to, files: !!el.querySelector('#rwf')?.checked}).catch(() => null);
    if (!r || !r.ok) { const err = el.querySelector('#rewindError'); err.hidden = false; err.textContent = (r && r.error) || '没回去，再试一次'; b.disabled = false; el.querySelector('#rwno').disabled = false; b.textContent = '确认回退'; return; }
    el.remove();
    pending[d.num] = [];
    if (undoLast && r.text) { const ta = $('#ta'); if (ta) { ta.value = r.text.replace(/\n\n（附件已存到[\s\S]*$/, ''); ta.oninput(); } }
    lastKey = ''; loadThread(); loadSessions();
  };
}
// 复制、回退、分叉：和 Claude 一样是一排小图标，悬停出名字
const IB = {
  bring: '<svg viewBox="0 0 24 24"><path d="M4 5h12v9H9l-5 4V5zM14 19h6M17 16v6"/></svg>',
  copy: '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M15 9V6.5A2.5 2.5 0 0 0 12.5 4h-6A2.5 2.5 0 0 0 4 6.5v6A2.5 2.5 0 0 0 6.5 15H9"/></svg>',
  back: '<svg viewBox="0 0 24 24"><path d="M4 12a8 8 0 1 0 2.35-5.65L4 8.7"/><path d="M4 4v4.7h4.7"/></svg>',
  fork: '<svg viewBox="0 0 24 24"><path d="M3 12h6.5l8.5-7"/><path d="M13.5 5H18v4.5"/><path d="M9.5 12l8.5 7M13.5 19H18v-4.5"/></svg>',
  listen: '<svg viewBox="0 0 24 24"><path d="m11 5-6 4H2v6h3l6 4zM15 8a6 6 0 0 1 0 8M18 5a10 10 0 0 1 0 14"/></svg>',
  ok: '<svg viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>'};
const ib = (a, t) => `<button class="ib" data-act="${a}" data-tip="${t}" aria-label="${t}">${IB[a]}</button>`;
