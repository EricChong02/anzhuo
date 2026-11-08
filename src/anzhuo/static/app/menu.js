// 退出登录：退这台设备（这台的手机通知也停），或所有设备都退（手机丢了用）
// 登录的设备：看有几台登录着，能退出任意一台；这台自己退出时顺手停掉这台的通知
function ago2(t) { const m = Math.floor((Date.now() / 1000 - t) / 60); return m < 2 ? '刚刚' : m < 60 ? `${m} 分钟前` : m < 1440 ? `${Math.floor(m / 60)} 小时前` : `${Math.floor(m / 1440)} 天前`; }
async function logoutHere(all) {
  let endpoint = '';
  try { const reg = await navigator.serviceWorker.getRegistration('/_x/'); const sub = reg && await reg.pushManager.getSubscription();
        if (sub) { endpoint = sub.endpoint; await sub.unsubscribe(); } } catch (err) {}
  await fetch('/_x/api/logout', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({all, endpoint})});
  clearThreads();   // 学校电脑上退出，记在本机的对话一并清掉
  location.href = '/_x/login';
}
$('#moreBtn').onclick = () => {
  const admin = me.role !== 'user';
  const el = sheet((admin ? `<button data-a="s"><svg viewBox="0 0 24 24" style="width:18px;height:18px"><circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/></svg>定时任务</button>
    <button data-a="a"><svg viewBox="0 0 24 24" style="width:18px;height:18px"><path d="M5 20V11M12 20V5M19 20v-6"/></svg>管理</button>
    <div class="sep"></div><button data-a="h"><svg viewBox="0 0 24 24" style="width:18px;height:18px"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/></svg>历史记录</button>` : '') + `
    <button data-a="d"><svg viewBox="0 0 24 24" style="width:18px;height:18px"><rect x="2" y="5" width="13" height="10" rx="1.5"/><path d="M5 19h7"/><rect x="17" y="8" width="5" height="11" rx="1.2"/></svg>登录的设备 · 退出登录</button>`);
  if (document.documentElement.classList.contains('ipad')) {
    const b = document.createElement('button'); b.textContent = 'iPad 使用与键盘设置';
    el.querySelector('.box').prepend(b); b.onclick = () => { el.remove(); $('#workspaceHelp').click(); };
  }
  if (admin) {
    el.querySelector('[data-a=s]').onclick = () => { el.remove(); openSchedules(); };
    el.querySelector('[data-a=a]').onclick = () => { el.remove(); openAdmin('usage'); };
    el.querySelector('[data-a=h]').onclick = () => { location.href = '/_x/history'; };
}
  el.querySelector('[data-a=d]').onclick = () => { el.remove(); $('#logoutBtn').onclick(); };
};
$('#logoutBtn').onclick = async () => {
  const el = document.createElement('div'); el.className = 'sheet';
  document.body.appendChild(el);
  const draw = async () => {
    const {devices} = await api('devices');
    el.innerHTML = `<div class="box"><b>登录的设备 · ${devices.length} 台</b><p>不认识的设备点“退出”，它就得重新输密码。</p>
      <div class="devs">${devices.map(d => `<div class="dev"><div class="dn"><b>${esc(d.name)}${d.me ? '<span class="me">这台</span>' : ''}</b>
        <small>${d.me ? '正在用' : d.seen ? '最近用过：' + ago2(d.seen) : '这个功能上线前登录的，之后还没用过'}${d.ip ? ' · ' + esc(d.ip) : ''}${d.push ? ' · 开着通知' : ''}</small></div>
        ${d.me ? '' : `<button class="out" data-id="${d.id}">退出</button>`}</div>`).join('')}</div>
      <button class="danger" data-a="one">退出这台设备</button>${devices.length > 1 ? '<button data-a="all">所有设备都退出（手机丢了时用）</button>' : ''}<button data-a="no">关闭</button></div>`;
  };
  await draw();
  el.onclick = async e => {
    const t = e.target.closest('button');
    if (e.target === el || (t && t.dataset.a === 'no')) return el.remove();
    if (!t) return;
    if (t.dataset.id) { t.disabled = true; t.textContent = '退出中…'; await api('devices/logout', {id: t.dataset.id}).catch(() => {}); return draw(); }
    t.textContent = '正在退出…'; await logoutHere(t.dataset.a === 'all');
  };
};

addEventListener('popstate', e => { const lb = document.querySelectorAll('.lightbox'); if (lb.length) { lb.forEach(x => x.remove()); e.stopImmediatePropagation(); } });

addEventListener('popstate', e => { const n = e.state && e.state.s; if (n) openSession(n, true); else if (e.state && e.state.n) openNew(true); else if (e.state && e.state.a) openAdmin(e.state.a, true); else goList(); });

