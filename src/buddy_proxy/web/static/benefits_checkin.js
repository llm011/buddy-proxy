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
const CKREFRESH = {};  // pid -> 在飞标记：trae 强刷十几秒，30s 轮询会整页重建
                       // innerHTML 把按钮态刷掉（又能再点），照 QREFRESH 放模块级
async function refreshCheckin(pid, btn) {
  if (CKREFRESH[pid]) return;
  CKREFRESH[pid] = true;
  if (btn) { btn.disabled = true; btn.textContent = '刷新中…'; }
  const restore = () => { if (btn) { btn.disabled = false; btn.textContent = '强刷'; } };
  try {
    await api('/ui/api/benefits/refresh', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider: pid, checkin: true})});
    await loadData(['benefits'], true);
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



// ---- KIMI 面板（antigravity 同款布局：每账号一块，标题=账号名、副标题=状态，
//      ▲▼ 顺位 / ✕ 删除 / 导入账号。组件全部复用 antigravity 的）----
// 数据两路：额度走 /ui/api/benefits 里 kimi 条目（label 带「Kimi #N · 」前缀，
// 组名/副标题由账号数据回填），账号状态走 /ui/api/kimi/accounts（纯本地不触网）。
// 与 antigravity 的差别：未登录（quota 返回静态说明条）也要渲染整卡——
// 「导入账号」入口（粘贴 kimi cli 导出的 token JSON）就长在卡头上。
let KIMI_ACCTS = null;  // 最近一次 accounts 快照；render 先用它，避免每 30s 闪回「Kimi #N」
let KIMI_MOVING = false;  // 面板账号操作（重排/删除）在途：期间忽略新的点按

function _kimi_acct_for(idx) {
  // idx=null（单账号组名无 Kimi #N 前缀）只在恰有一个账号时能对上
  if (!KIMI_ACCTS) return null;
  if (idx == null) return KIMI_ACCTS.length === 1 ? KIMI_ACCTS[0] : null;
  return KIMI_ACCTS.find(a => a.index === idx) || null;
}

function _kimi_sub_html(a) {
  // 副标题（进度条上面那行 muted 小字）：冷却（kimi 只有
  // quota/account 两档冷却，没有 antigravity 的拉黑档）
  // （「token 剩 Xh」删了：access token 几小时自动刷新，读了只会误导）
  if (!a) return '';
  const bits = [];
  for (const c of a.cooling || []) {
    const left = c.minutes_left >= 120 ? (c.minutes_left / 60).toFixed(1) + 'h' : c.minutes_left + 'min';
    bits.push(`${c.kind === 'quota' ? '额度' : '账号'}冷却 ${left}`);
  }
  return bits.length
    ? `<div class="muted" style="font-size:11px;margin:1px 0 6px" data-kimi-sub>${esc(bits.join(' · '))}</div>`
    : '';
}

function _kimi_move_btns(idx, n, id) {
  // 上/下移按钮（与 _ag_move_btns 同构；顺位语义照 antigravity：按快照里
  // 该 id 的实际位次挪，不按按钮上的 idx——面板重绘滞后时 idx 会过期）
  const arg = id ? `,'${id}'` : '';
  return `<button class="ghost" title="上移（更优先使用）" ${idx <= 1 ? 'disabled' : ''} ` +
    `onclick="kimiMoveAccount(${idx},-1${arg})">▲</button>` +
    `<button class="ghost" title="下移" ${idx >= n ? 'disabled' : ''} ` +
    `onclick="kimiMoveAccount(${idx},1${arg})">▼</button>`;
}

function kimiConfirmDelete(id) {
  const a = (KIMI_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.name || a.id)) || id;
  return confirmAccountDelete({
    title: '删除 Kimi 账号',
    name,
    extra: '该账号的凭据文件一并移除，转发不再使用它。refresh_token 已作废' +
      '（副标题持续显示账号冷却且转发失败）的账号删掉后即不再白耗一轮 failover。',
  });
}

// 调 POST /ui/api/kimi/accounts/order 提交完整顺序（agMoveAccount 同构）。
// 后端重写 priority 后 quota 缓存键（quota_epoch 带 priority）随之失效，
// 这里拿到响应后 refreshAll() 重取 benefits，进度条组顺序即更新。
async function kimiMoveAccount(idx, delta, id) {
  if (KIMI_MOVING) return;
  KIMI_MOVING = true;
  try {
    if (!KIMI_ACCTS) {  // 按钮随额度数据先到、账号状态可能还没回：补一次快照
      const r0 = await api('/ui/api/kimi/accounts');
      KIMI_ACCTS = r0.accounts || [];
    }
    const accts = KIMI_ACCTS;
    if (id) {  // 按 id 校正到快照里的真实位次
      const at = accts.findIndex(a => a.id === id);
      if (at < 0) return;
      idx = at + 1;
    }
    const to = idx + delta;
    if (to < 1 || to > accts.length) return;
    const ids = accts.map(a => a.id);
    const [moved] = ids.splice(idx - 1, 1);
    ids.splice(to - 1, 0, moved);
    const r = await api('/ui/api/kimi/accounts/order', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids})});
    KIMI_ACCTS = r.accounts || [];
    const movedName = (accts.find(a => a.id === moved) || {}).name || moved;
    toast(`${movedName} 已移到顺位 #${to}`);
    refreshAll();
  } catch (e) { toast('调整失败: ' + e.message, true); }
  finally { KIMI_MOVING = false; }
}

async function kimiDeleteAccount(id) {
  if (KIMI_MOVING) return;
  if (!(await kimiConfirmDelete(id))) return;
  const a = (KIMI_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.name || a.id)) || id;
  KIMI_MOVING = true;
  try {
    const r = await api('/ui/api/kimi/accounts/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})});
    KIMI_ACCTS = r.accounts || [];
    toast(`${name} 已删除`);
    refreshAll();
  } catch (e) { toast('删除失败: ' + e.message, true); }
  finally {
    KIMI_MOVING = false;
    ACCT_CONFIRM_OPEN = false;  // 确认时从 confirmAccountDelete 接手的锁，到这里才放
  }
}

// 导入账号：粘贴 kimi cli 导出的 token JSON（buddy login kimi 的等价入口，
// 适合「token 在别的机器上导出、这边只是接进来」的场景）。
function openKimiImport() {
  document.getElementById('modal-title').textContent = '导入 Kimi 账号';
  document.getElementById('modal-body').innerHTML =
    `<p style="margin:0 0 8px">粘贴 kimi cli 导出的 token JSON（<span class="mono">kimi-&lt;时间戳&gt;.json</span> 文件内容，` +
    `含 <span class="mono">access_token</span> / <span class="mono">refresh_token</span> / <span class="mono">base_url</span>）。</p>` +
    `<textarea id="kimi-import-text" class="mono" style="width:100%;height:180px;resize:vertical" ` +
    `placeholder='{"access_token": "...", "refresh_token": "...", "base_url": "https://api.kimi.com/coding", ...}'></textarea>`;
  setModalFoot('<button onclick="closeModal()">取消</button>' +
    '<button onclick="kimiImportSubmit()">导入</button>');
  document.getElementById('overlay').classList.add('show');
  const ta = document.getElementById('kimi-import-text');
  if (ta) ta.focus();
}

async function kimiImportSubmit() {
  const ta = document.getElementById('kimi-import-text');
  const text = ((ta && ta.value) || '').trim();
  if (!text) { toast('先粘贴 JSON 再导入', true); return; }
  const btns = document.querySelectorAll('#modal-foot button');
  btns.forEach(b => { b.disabled = true; });
  try {
    const r = await api('/ui/api/kimi/accounts/import', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({payload: text})});
    KIMI_ACCTS = r.accounts || [];
    closeModal();
    toast('Kimi 账号已导入');
    refreshAll();
  } catch (e) { toast('导入失败: ' + e.message, true); }
  finally { btns.forEach(b => { b.disabled = false; }); }
}

function renderKimiPanel() {
  const panel = document.getElementById('kimi-panel');
  if (!panel) return;
  const kimi = (BENEFITS.providers || []).find(p => p.id === 'kimi' && !p.disabled);
  if (!kimi) { panel.innerHTML = ''; return; }  // 通道未注册（没加 --kimi）：整块不渲染

  // 按「Kimi #N」分组（label 形如「Kimi #1 · 5 小时窗口」），antigravity 同款切法。
  // 静态说明条（未登录引导/查询失败）没有进度条语义，不进账号分组，横贯全宽。
  const groups = new Map();
  const notices = [];
  for (const it of (kimi.quota && kimi.quota.supported ? kimi.quota.items : []) || []) {
    if (it.query_failed || (it.percent == null && it.used == null)) { notices.push(it); continue; }
    const idx = it.label.indexOf(' · ');
    const grp = idx >= 0 ? it.label.slice(0, idx) : 'Kimi';
    const sub = idx >= 0 ? it.label.slice(idx + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  // 界标 n = 全部「Kimi #N」序号的最大值（自包含，不依赖快照到达时序）
  const n = Math.max(...[...groups.keys()]
    .map(g => g.match(/^Kimi #(\d+)$/)).filter(Boolean).map(mm => Number(mm[1])), 1);
  const quotaHtml = [...groups.entries()].map(([grp, its]) => {
    const m = grp.match(/^Kimi #(\d+)$/);
    const idx = m ? Number(m[1]) : null;
    const acct = _kimi_acct_for(idx);
    const moveBtns = (m && n > 1) ? _kimi_move_btns(idx, n, acct && acct.id) : '';
    // 删除按钮要账号 id，快照没到（首屏首渲）时先不渲染，等下轮（同 antigravity）
    const delBtn = acct
      ? `<button class="ghost danger" title="删除该账号（refresh_token 作废/不再使用时）" ` +
        `onclick="kimiDeleteAccount('${acct.id}')">✕</button>` : '';
    const renameBtn = acctRenameButton('kimi', acct);
    const rowBtns = `<span class="ag-move">${renameBtn}${moveBtns}${delBtn}</span>`;
    return `
    <div class="pat-pkg">
      <span class="pat-pkg-name"${m ? ` data-kimi-idx="${idx}"` : ''}>${esc(acct ? (acct.alias || acct.name || acct.id) : grp)}</span>
      ${_kimi_sub_html(acct)}
      ${quotaItemsHtml(its, 'kimi:' + grp)}
      ${rowBtns}
    </div>`;
  }).join('');
  const noticeHtml = notices.map(quotaItemHtml).join('');
  const multi = n > 1;

  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">Kimi</span>
        <span class="tag">${multi ? '多账号 · 自动切换' : 'Kimi Code 订阅'}</span>
        <span class="grow"></span>
        <span class="muted" style="font-size:11px">${multi ? '429/403 自动冷却换号（按导入顺位）' : '额度耗尽自动切换下一个账号'}</span>
        ${providerRefreshButton('kimi')}
        <button class="ghost" title="粘贴 kimi cli 导出的 token JSON" onclick="openKimiImport()">导入账号</button>
      </div>
      ${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}
      ${groups.size ? `<div class="pat-quota-grid">${quotaHtml}</div>`
        : '<div class="empty" style="padding:12px 0">暂无账号额度数据</div>'}
    </div>`;
  loadKimiAccounts();
}

async function loadKimiAccounts() {
  try {
    const r = await api('/ui/api/kimi/accounts');
    if (!r.enabled) { KIMI_ACCTS = []; return; }
    const accts = r.accounts || [];
    // 数据没变就不动 DOM——renderKimiPanel 已用同一份快照渲染过
    if (JSON.stringify(accts) === JSON.stringify(KIMI_ACCTS)) return;
    const snapshotMissing = !KIMI_ACCTS || !KIMI_ACCTS.length;
    KIMI_ACCTS = accts;
    if (!accts.length) return;
    if (snapshotMissing) {
      // 首份快照到位：带快照整卡重渲（账号名直出、▲▼/✕ 按钮带上 id），
      // 再拉取数据已同、走早退，不会循环（antigravity 同款）
      renderKimiPanel();
      syncQuotaFold();
      return;
    }
    // 就地回填：组名换成账号名、组名后插副标题（antigravity 同款）
    for (const a of accts) {
      const el = (accts.length === 1)
        ? document.querySelector('#kimi-panel .pat-pkg-name')
        : document.querySelector(`#kimi-panel [data-kimi-idx="${a.index}"]`);
      if (!el) continue;
      el.textContent = a.alias || a.name || a.id;
      if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-kimi-sub')) {
        el.nextElementSibling.remove();
      }
      const sub = _kimi_sub_html(a);
      if (sub) el.insertAdjacentHTML('afterend', sub);
    }
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
  }
}



// ---- CODEX 面板（kimi 同款布局：每账号一块，标题=账号名、副标题=状态，
//      ▲▼ 顺位 / ✎ 改名 / ✕ 删除 / 导入账号；组件复用 kimi 的）----
// 与 kimi 的唯一差别在「导入」提示：Codex 凭据可来自 `buddy login codex` 的
// 本机授权、粘贴 ~/.codex/auth.json，或别的机器上导出的同构 JSON——**都不需要
// 本机装 Codex CLI**。
let CODEX_ACCTS = null;
let CODEX_MOVING = false;

function _codex_acct_for(idx) {
  if (!CODEX_ACCTS) return null;
  if (idx == null) return CODEX_ACCTS.length === 1 ? CODEX_ACCTS[0] : null;
  return CODEX_ACCTS.find(a => a.index === idx) || null;
}

function _codex_acct_by_title(who) {
  // who 形如 `Codex #N · 名字` 或单账号的裸名字；逐项比对快照里的 id/name/alias，
  // 取最后一个（等价于「最多匹配一次」的兜底：宁可不匹配也不乱挂）。
  if (!CODEX_ACCTS || !CODEX_ACCTS.length) return null;
  const want = String(who || '').replace(/^Codex #\d+ · /, '').trim();
  const num = (String(who || '').match(/^Codex #(\d+)/) || [])[1];
  if (num) {
    const byIndex = CODEX_ACCTS.find(a => a.index === Number(num));
    if (byIndex) return byIndex;
  }
  if (!want) return null;
  return CODEX_ACCTS.find(a => a.id === want || a.name === want || a.alias === want) || null;
}

function _codex_sub_html(a) {
  // 副标题：冷却（quota/account 两档）+ ChatGPT 套餐档位
  if (!a) return '';
  const bits = [];
  for (const c of a.cooling || []) {
    const left = c.minutes_left >= 120 ? (c.minutes_left / 60).toFixed(1) + 'h' : c.minutes_left + 'min';
    bits.push(`${c.kind === 'quota' ? '额度' : '账号'}冷却 ${left}`);
  }
  if (a.plan_type) bits.push(`套餐 ${a.plan_type}`);
  return bits.length
    ? `<div class="muted" style="font-size:11px;margin:1px 0 6px" data-codex-sub>${esc(bits.join(' · '))}</div>`
    : '';
}

function codexConfirmDelete(id) {
  const a = (CODEX_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.name || a.id)) || id;
  return confirmAccountDelete({
    title: '删除 Codex 账号',
    name,
    extra: '该账号的凭据文件一并移除，转发不再使用它。refresh_token 已被 OpenAI 轮换作废' +
      '（副标题持续显示账号冷却且转发失败）的账号删掉后即不再白耗一轮 failover。',
  });
}

async function codexMoveAccount(idx, delta, id) {
  if (CODEX_MOVING) return;
  CODEX_MOVING = true;
  try {
    if (!CODEX_ACCTS) {
      const r0 = await api('/ui/api/codex/accounts');
      CODEX_ACCTS = r0.accounts || [];
    }
    const accts = CODEX_ACCTS;
    if (id) {  // 按 id 校正到快照里的真实位次（面板重绘滞后时 idx 会过期）
      const at = accts.findIndex(a => a.id === id);
      if (at < 0) return;
      idx = at + 1;
    }
    const to = idx + delta;
    if (to < 1 || to > accts.length) return;
    const ids = accts.map(a => a.id);
    const [moved] = ids.splice(idx - 1, 1);
    ids.splice(to - 1, 0, moved);
    const r = await api('/ui/api/codex/accounts/order', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids})});
    CODEX_ACCTS = r.accounts || [];
    const movedName = (accts.find(a => a.id === moved) || {}).name || moved;
    toast(`${movedName} 已移到顺位 #${to}`);
    refreshAll();
  } catch (e) { toast('调整失败: ' + e.message, true); }
  finally { CODEX_MOVING = false; }
}

async function codexDeleteAccount(id) {
  if (CODEX_MOVING) return;
  if (!(await codexConfirmDelete(id))) return;
  const a = (CODEX_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.name || a.id)) || id;
  CODEX_MOVING = true;
  try {
    const r = await api('/ui/api/codex/accounts/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})});
    CODEX_ACCTS = r.accounts || [];
    toast(`${name} 已删除`);
    refreshAll();
  } catch (e) { toast('删除失败: ' + e.message, true); }
  finally {
    CODEX_MOVING = false;
    ACCT_CONFIRM_OPEN = false;  // 确认时从 confirmAccountDelete 接手的锁，到这里才放
  }
}

// 导入账号：粘贴凭据 JSON。认 ~/.codex/auth.json（{"tokens": {...}}）与扁平形态；
// 没有 Codex CLI 也行——`buddy login codex` 走的是同一套 OpenAI OAuth。
function openCodexImport() {
  document.getElementById('modal-title').textContent = '导入 Codex 账号';
  document.getElementById('modal-body').innerHTML =
    `<p style="margin:0 0 8px">粘贴 <span class="mono">~/.codex/auth.json</span> 的内容，或任何含 ` +
    `<span class="mono">access_token</span> / <span class="mono">refresh_token</span> 的 JSON。</p>` +
    `<p class="muted" style="margin:0 0 8px;font-size:12px">不需要安装 Codex CLI：终端跑 ` +
    `<span class="mono">buddy login codex</span> 直接走 OpenAI 授权页登录，成功后自动落到这里。</p>` +
    `<textarea id="codex-import-text" class="mono" style="width:100%;height:180px;resize:vertical" ` +
    `placeholder='{"tokens": {"access_token": "...", "refresh_token": "...", "id_token": "..."}}'></textarea>`;
  setModalFoot('<button onclick="closeModal()">取消</button>' +
    '<button onclick="codexImportSubmit()">导入</button>');
  document.getElementById('overlay').classList.add('show');
  const ta = document.getElementById('codex-import-text');
  if (ta) ta.focus();
}

async function codexImportSubmit() {
  const ta = document.getElementById('codex-import-text');
  const text = ((ta && ta.value) || '').trim();
  if (!text) { toast('先粘贴 JSON 再导入', true); return; }
  const btns = document.querySelectorAll('#modal-foot button');
  btns.forEach(b => { b.disabled = true; });
  try {
    const r = await api('/ui/api/codex/accounts/import', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({payload: text})});
    CODEX_ACCTS = r.accounts || [];
    closeModal();
    toast('Codex 账号已导入');
    refreshAll();
  } catch (e) { toast('导入失败: ' + e.message, true); }
  finally { btns.forEach(b => { b.disabled = false; }); }
}

// 重置券行（后端 unit=reset_credit）：它是**张数说明**不是额度水位，没有
// percent/remaining，所以不能交给 quotaItemHtml——那会被当成 notice 横贯整卡，
// 或渲染出空的数字行。归属靠后端的 who（账号标题）而不是解析 label：额度
// label 会因单/多账号、窗口数量变化而改写，who 才是稳定的账号键。
function _codex_reset_line(it) {
  const rc = it && it.reset_credits;
  const okCount = v => Number.isInteger(v) && v >= 0;
  if (!rc || typeof rc !== 'object' || !okCount(rc.available)) return '';
  let text = `重置券 ${rc.available} 张`;
  if (okCount(rc.applicable)) text += ` · 当前可用 ${rc.applicable} 张`;
  return `<div class="muted codex-reset-credit" data-reset-credit ` +
    `style="font-size:11px;line-height:1.45;margin:2px 0 0">${esc(text)}</div>`;
}

function renderCodexPanel() {
  const panel = document.getElementById('codex-panel');
  if (!panel) return;
  const codex = (BENEFITS.providers || []).find(p => p.id === 'codex' && !p.disabled);
  if (!codex) { panel.innerHTML = ''; return; }  // 通道未注册（没加 --codex）：整块不渲染

  const groups = new Map();
  const notices = [];
  const resetByGrp = new Map();
  for (const it of (codex.quota && codex.quota.supported ? codex.quota.items : []) || []) {
    // 重置券必须在 notice 判断之前分流：它同样没有 percent/used，否则会被
    // 当成横贯全宽的说明条，跑到所有账号卡片外面去。
    if (it.unit === 'reset_credit' || it.reset_credit === true) {
      const who = String(it.who || '').trim();
      // 没有 who 就不显示：错挂到别的账号比少显示一条更糟
      if (who) resetByGrp.set(who, it);
      continue;
    }
    if (it.query_failed || (it.percent == null && it.used == null)) { notices.push(it); continue; }
    const idx = it.label.indexOf(' · ');
    const grp = idx >= 0 ? it.label.slice(0, idx) : 'Codex';
    const sub = idx >= 0 ? it.label.slice(idx + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  // 只有券、没有窗口的账号也要出卡片（空组照常渲染按钮，quotaItemsHtml([])
  // 返回空串）。同一账号的券行要并进它的卡片：券的 who 常带 `Codex #N · `
  // 前缀（多账号），也可能被别名改写或单账号无前缀——先按完整 who 认，再按
  // 位次认（`Codex #N` 与 `Codex #N · X` 实为同一张卡），最后按账号名认；
  // 都认不出才单开一卡。
  const grpByWho = new Map();
  for (const grp of groups.keys()) {
    grpByWho.set(grp, grp);
    const m = grp.match(/^Codex #\d+ · (.*)$/);
    if (m && !grpByWho.has(m[1])) grpByWho.set(m[1], grp);
  }
  const grpKey = who => {
    if (grpByWho.has(who)) return grpByWho.get(who);
    const m = who.match(/^Codex #(\d+)(?: · |$)/);
    if (m && groups.has(`Codex #${m[1]}`)) return `Codex #${m[1]}`;
    const acct = _codex_acct_by_title(who);
    if (acct && acct.index != null && groups.has(`Codex #${acct.index}`)) {
      return `Codex #${acct.index}`;
    }
    return who;
  };
  const resetGrp = new Map();
  for (const [who, it] of resetByGrp) {
    const grp = grpKey(who);
    if (!resetGrp.has(grp)) resetGrp.set(grp, it);
  }
  const allGrps = new Set([...groups.keys(), ...resetGrp.keys()]);
  // 只有券的账号没有「Codex #N」标题可解析，从 who 里取位次（与卡片编号一致）
  // 才能拿到账号快照、挂上 ▲▼/✎/✕ 并按真实顺位排序。
  const idxOf = grp => {
    const m = grp.match(/^Codex #(\d+)(?: · |$)/);
    return m ? Number(m[1]) : null;
  };
  const ordered = [...allGrps].sort((a, b) => {
    const ia = idxOf(a), ib = idxOf(b);
    return ia == null || ib == null ? (ia == null) - (ib == null) : ia - ib;
  });
  const n = Math.max(...ordered.map(g => idxOf(g) || 0), 1);
  const quotaHtml = ordered.map(grp => {
    const its = groups.get(grp) || [];
    const idx = idxOf(grp);
    const m = idx != null;
    // 券没有窗口行可借，就用 who 在快照里按 id/名字/别名反查账号，
    // 否则「只有券」的账号拿不到 id，按钮和标题都会退化。
    const acct = _codex_acct_for(idx) || _codex_acct_by_title(grp);
    const moveBtns = (m && n > 1) ? acctMoveButtons('codex', idx, n, acct && acct.id) : '';
    const delBtn = acctDeleteButton('codex', acct, 'refresh_token 作废/不再使用时');
    const renameBtn = acctRenameButton('codex', acct);
    const rowBtns = `<span class="ag-move">${renameBtn}${moveBtns}${delBtn}</span>`;
    // 卡片标题用账号名（快照回填）；后缀只是分组键自带的前缀，不要露出来
    const shown = acct ? (acct.alias || acct.name || acct.id) : grp.replace(/^Codex #\d+ · /, '');
    return `
    <div class="pat-pkg">
      <span class="pat-pkg-name"${m ? ` data-codex-idx="${idx}"` : ''}>${esc(shown)}</span>
      ${_codex_sub_html(acct)}
      ${quotaItemsHtml(its, 'codex:' + grp)}
      ${resetGrp.has(grp) ? _codex_reset_line(resetGrp.get(grp)) : ''}
      ${rowBtns}
    </div>`;
  }).join('');
  const noticeHtml = notices.map(quotaItemHtml).join('');
  const multi = n > 1;

  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">Codex</span>
        <span class="tag">${multi ? '多账号 · 自动切换' : 'ChatGPT 订阅 · 自动切换'}</span>
        <span class="grow"></span>
        <span class="muted" style="font-size:11px">${multi ? '429/403 自动冷却换号（按账号顺位）' : '额度耗尽自动切换下一个账号'}</span>
        ${providerRefreshButton('codex')}
        <button class="ghost" title="粘贴 ~/.codex/auth.json 或同构 JSON（无需安装 Codex CLI）" onclick="openCodexImport()">导入账号</button>
      </div>
      ${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}
      ${allGrps.size ? `<div class="pat-quota-grid">${quotaHtml}</div>`
        : '<div class="empty" style="padding:12px 0">暂无账号额度数据</div>'}
    </div>`;
  loadCodexAccounts();
}

async function loadCodexAccounts() {
  try {
    const r = await api('/ui/api/codex/accounts');
    if (!r.enabled) { CODEX_ACCTS = []; return; }
    const accts = r.accounts || [];
    if (JSON.stringify(accts) === JSON.stringify(CODEX_ACCTS)) return;
    const snapshotMissing = !CODEX_ACCTS || !CODEX_ACCTS.length;
    CODEX_ACCTS = accts;
    if (!accts.length) return;
    if (snapshotMissing) {
      renderCodexPanel();
      syncQuotaFold();
      return;
    }
    for (const a of accts) {
      const el = (accts.length === 1)
        ? document.querySelector('#codex-panel .pat-pkg-name')
        : document.querySelector(`#codex-panel [data-codex-idx="${a.index}"]`);
      if (!el) continue;
      el.textContent = a.alias || a.name || a.id;
      if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-codex-sub')) {
        el.nextElementSibling.remove();
      }
      const sub = _codex_sub_html(a);
      if (sub) el.insertAdjacentHTML('afterend', sub);
    }
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
  }
}
