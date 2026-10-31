// force=false：读缓存（GET，不触网），页面默认展示；force=true：强制查询（POST）。
// silent=true：后台自动刷新用，不清空面板、不占用按钮（避免每轮闪烁）。
async function loadTraepatStatus(btn, force, silent) {
  const box = document.getElementById('traepat-status');
  if (!box) return;
  if (btn) { btn.disabled = true; btn.textContent = '更新中…'; }
  if (force && !silent) box.innerHTML = '<div class="empty" style="padding:8px 0">正在查询模型负载…</div>';
  const restore = () => { if (btn) { btn.disabled = false; btn.textContent = '更新'; } };
  try {
    const r = force
      ? await api('/ui/api/traepat/model-status', {method: 'POST'})
      : await api('/ui/api/traepat/model-status');
    // 排序：有负载的在前（按负载降序，越繁忙越靠上），查不到负载（workload==null）
    // 的沉底。原序（config 顺序）用作同档内的稳定次序（Array.prototype.sort 在
    // 现代引擎稳定）。只排展示，不改后端数据。
    const sorted = (r.models || []).slice().sort((a, b) => {
      const wa = a.workload, wb = b.workload;
      if (wa == null && wb == null) return 0;
      if (wa == null) return 1;
      if (wb == null) return -1;
      return Number(wb) - Number(wa);
    });
    const rows = sorted.map(m => {
      const w = m.workload;
      const hasW = w != null;
      const pct = hasW ? Math.max(0, Math.min(100, Number(w))) : 0;
      const color = pct >= 80 ? 'var(--err)' : pct >= 50 ? 'var(--warn)' : 'var(--ok)';
      const label = hasW ? `${Math.round(pct)}%` : '—';
      const bar = hasW ? `<div class="qbar"><div style="width:${pct > 0 ? Math.max(2, pct) : 0}%;background:${color}"></div></div>` : '';
      const extra = [m.credits ? `积分 ${esc(m.credits)}` : '', m.max_input ? `输入上限 ${fmtNum(m.max_input)}` : ''].filter(Boolean).join(' · ');
      return `<div class="qitem"><div class="qhead"><span>${esc(m.name || m.id)}${extra ? ` <span class="muted">${extra}</span>` : ''}</span><span class="mono">${label}</span></div>${bar}</div>`;
    }).join('');
    const ts = r.fetched_at ? new Date(r.fetched_at * 1000).toLocaleTimeString('zh-CN') : '';
    const auto = PAT_STATUS_TIMER ? ' · 自动每分钟' : '';
    const hint = !r.fetched_at
      ? '尚无缓存，点「更新」查询'
      : `数值越高越繁忙 · ${ts} ${r.cached ? '缓存' : '更新'}${auto}`;
    box.innerHTML = `<div class="muted pat-load-hint">${hint}</div><div class="pat-load-grid">` +
      (rows || '<div class="empty" style="padding:8px 0">无负载数据</div>') + '</div>';
  } catch (e) {
    // 后台静默刷新失败不覆盖已有数据，仅手动/首屏才提示
    if (!silent) box.innerHTML = `<div class="empty" style="padding:8px 0">查询失败：${esc(e.message)}</div>`;
  }
  restore();
}

// 模型负载自动刷新：仅当「打卡 & 额度」页可见时每 60s 强制查一次；
// 切走或标签页隐藏即停，避免后台持续打上游。
let PAT_STATUS_TIMER = null;
const PAT_STATUS_INTERVAL = 60000;
function patStatusAutoActive() {
  const page = document.getElementById('page-benefits');
  return !!page && page.classList.contains('active')
    && document.visibilityState === 'visible'
    && !!document.getElementById('traepat-status');
}
function syncPatStatusAuto() {
  if (patStatusAutoActive()) {
    if (!PAT_STATUS_TIMER) {
      PAT_STATUS_TIMER = setInterval(() => {
        if (patStatusAutoActive()) loadTraepatStatus(null, true, true);
        else syncPatStatusAuto();  // 定时器还在但已不该跑：清掉
      }, PAT_STATUS_INTERVAL);
    }
  } else if (PAT_STATUS_TIMER) {
    clearInterval(PAT_STATUS_TIMER);
    PAT_STATUS_TIMER = null;
  }
}
document.addEventListener('visibilitychange', syncPatStatusAuto);

// 强刷单通道签到状态：绕过 5 分钟快照缓存真打上游（多账号通道逐号重查，
// 最坏十几秒——和 claimNow 同理必须给反馈，否则像「点了没反应」）。
// 拿到响应后**只**用返回的快照静默重绘签到区：以前这里走 loadData(['benefits'], true)，
// 会连带强刷 stats（概览卡片/图表）、重建全部通道面板再各拉一次 accounts、
// traepat 模型负载、dumate 本地状态——点一下签到打出十来个请求（用户 2026-11-01
// 实报）。用户点的是「刷这个通道的签到」，其余数据交给 30s 轮询自然更新。
const CKREFRESH = {};  // pid -> 在飞标记：trae 强刷十几秒，30s 轮询会整页重建
                       // innerHTML 把按钮态刷掉（又能再点），照 QREFRESH 放模块级
async function refreshCheckin(pid, btn) {
  if (CKREFRESH[pid]) return;
  CKREFRESH[pid] = true;
  if (btn) { btn.disabled = true; btn.textContent = '刷新中…'; }
  const restore = () => { if (btn) { btn.disabled = false; btn.textContent = '强刷'; } };
  try {
    const snapshot = await api('/ui/api/benefits/refresh', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider: pid, checkin: true})});
    // refresh 接口已返回作废缓存后的完整快照，直接采用可少一整轮 GET；
    // 旧服务不回 providers 时才回退——也只强刷 benefits 一个键，不带 stats。
    if (snapshot && Array.isArray(snapshot.providers)) {
      BENEFITS = snapshot;
      renderBenefits({quiet: true});
    } else {
      await loadData(['benefits'], true);
    }
    toast(`✓ ${pid} 签到状态已强刷`);
  } catch (e) { toast('刷新失败: ' + e.message, true); }
  finally {
    CKREFRESH[pid] = false;
    restore();
  }
}

async function claimNow(pid, btn) {
  // 多账号通道（trae）的 claim 是串行节流的（账号间 sleep + 限流退避），
  // 一次点击最坏 20s+ 才返回——期间必须给反馈，否则看起来像「点了没反应」。
  if (btn) { btn.disabled = true; btn.textContent = '打卡中…'; }
  const restore = () => { if (btn) { btn.disabled = false; btn.textContent = '立即打卡'; } };
  try {
    const r = await api('/ui/api/checkin', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider: pid})});
    if (!r.ok) {
      toast(`打卡失败: ${r.message || r.error || '未知原因'}`, true);
    } else if (Array.isArray(r.accounts) && r.accounts.length > 1) {
      // 多账号通道（trae）：逐账号报结果——「成功」两个字盖不住个别账号的失败
      const bad = r.accounts.filter(a => a.ok === false);
      const got = r.accounts.filter(a => a.ok && a.credits != null)
        .map(a => `${a.name || '#' + a.index} +${fmtCredit(a.credits)}`).join('、');
      if (bad.length) {
        toast(`✓ ${pid} 部分失败：${got ? got + '；' : ''}${bad.map(a => `${a.name || '#' + a.index} ${a.message || '失败'}`).join('、')}`, true);
      } else {
        toast(`✓ ${pid} 全部签到成功${got ? '：' + got : ''}`);
      }
    } else {
      toast(`✓ ${pid} 打卡成功`);
    }
    refreshAll();
  } catch (e) { toast('打卡失败: ' + e.message, true); }
  finally { restore(); }
}

async function saveCheckinSettings() {
  try {
    await api('/ui/api/settings', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        auto_checkin: document.getElementById('auto-checkin').checked,
        checkin_time: document.getElementById('checkin-time').value})});
    toast('自动打卡设置已保存');
    refreshAll();
  } catch (e) { toast('保存失败: ' + e.message, true); }
}
