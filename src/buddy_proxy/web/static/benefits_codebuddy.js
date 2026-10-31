// ---- CODEBUDDY 面板（qoder/trae 同款布局：每账号一块，标题=账号名、副标题=冷却，
//      ▲▼ 顺位 / ✕ 删除 / ✎ 改名 / ↻ 刷新）----
// 数据两路：额度走 /ui/api/benefits 里 codebuddy 条目（label 带「CodeBuddy #N · 」
// 前缀），账号状态走 /ui/api/codebuddy/accounts（纯本地不触网，failover.accounts_status）。
// codebuddy 无 region（全部账号同域 copilot.tencent.com），副标题只有冷却位。
// 各资源包是并存份额（后端 sum_items: true），quotaHeadSum 走 sum_items 分支合计。
// 签到仍在上方「各通道签到」卡里（supports_checkin=True），本面板只管额度。
let CODEBUDDY_ACCTS = null;  // 最近一次 accounts 快照；render 先用它，避免每 30s 闪回「CodeBuddy #N」
let CODEBUDDY_MOVING = false;  // 面板账号操作（重排/删除）在途：期间忽略新的点按

function _codebuddy_acct_for(idx) {
  // idx=null（单账号组名无 CodeBuddy #N 前缀）只在恰有一个账号时能对上
  if (!CODEBUDDY_ACCTS) return null;
  if (idx == null) return CODEBUDDY_ACCTS.length === 1 ? CODEBUDDY_ACCTS[0] : null;
  return CODEBUDDY_ACCTS.find(a => a.index === idx) || null;
}

function _codebuddy_sub_html(a) {
  // 副标题（进度条上面那行 muted 小字）：冷却（无 region——codebuddy 全账号同域）
  if (!a) return '';
  const bits = [];
  for (const c of a.cooling || []) {
    const left = c.minutes_left >= 120 ? (c.minutes_left / 60).toFixed(1) + 'h' : c.minutes_left + 'min';
    bits.push(`${c.kind === 'quota' ? '额度' : '账号'}冷却 ${left}`);
  }
  return bits.length
    ? `<div class="muted" style="font-size:11px;margin:1px 0 6px" data-codebuddy-sub>${esc(bits.join(' · '))}</div>`
    : '';
}

function _codebuddy_move_btns(idx, n, id) {
  // 上/下移按钮（与 _qoder_move_btns 同构；顺位语义按快照里该 id 的实际位次挪）
  const arg = id ? `,'${id}'` : '';
  return `<button class="ghost" title="上移（更优先使用）" ${idx <= 1 ? 'disabled' : ''} ` +
    `onclick="codebuddyMoveAccount(${idx},-1${arg})">▲</button>` +
    `<button class="ghost" title="下移" ${idx >= n ? 'disabled' : ''} ` +
    `onclick="codebuddyMoveAccount(${idx},1${arg})">▼</button>`;
}

function codebuddyConfirmDelete(id) {
  const a = (CODEBUDDY_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.alias || a.nickname || a.id)) || id;
  return confirmAccountDelete({
    title: '删除 CodeBuddy 账号',
    name,
    extra: '该账号的凭据文件一并移除，转发不再使用它。不再使用或凭据失效' +
      '（转发持续 401/429 额度尽）的账号删掉后即不再白耗一轮 failover。',
  });
}

async function codebuddyMoveAccount(idx, delta, id) {
  if (CODEBUDDY_MOVING) return;
  CODEBUDDY_MOVING = true;
  try {
    if (!CODEBUDDY_ACCTS) {  // 按钮随额度数据先到、账号状态可能还没回：补一次快照
      // 这里**不走** acctFetch：加载器整个在飞去重，借到的可能是重排前那趟，
      // 拿旧顺序去算下一位次就挪错人了
      const r0 = await api('/ui/api/codebuddy/accounts');
      CODEBUDDY_ACCTS = r0.accounts || [];
    }
    const accts = CODEBUDDY_ACCTS;
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
    const r = await api('/ui/api/codebuddy/accounts/order', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      // region=cn：主卡快照只含 CN 子集，须走后端「区内重排」契约——
      // 不带的话 region=None 按全量校验，有海外账号时必然 400。
      body: JSON.stringify({ids, region: 'cn'})});
    CODEBUDDY_ACCTS = r.accounts || [];
    const a0 = accts.find(a => a.id === moved) || {};
    const movedName = a0.alias || a0.nickname || moved;
    toast(`${movedName} 已移到顺位 #${to}`);
    refreshAll();
  } catch (e) { toast('调整失败: ' + e.message, true); }
  finally { CODEBUDDY_MOVING = false; }
}

async function codebuddyDeleteAccount(id) {
  if (CODEBUDDY_MOVING) return;
  if (!(await codebuddyConfirmDelete(id))) return;
  const a = (CODEBUDDY_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.alias || a.nickname || a.id)) || id;
  CODEBUDDY_MOVING = true;
  try {
    const r = await api('/ui/api/codebuddy/accounts/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})});
    CODEBUDDY_ACCTS = r.accounts || [];
    toast(`${name} 已删除`);
    refreshAll();
  } catch (e) { toast('删除失败: ' + e.message, true); }
  finally {
    CODEBUDDY_MOVING = false;
    ACCT_CONFIRM_OPEN = false;
  }
}

function renderCodebuddyPanel() {
  const panel = document.getElementById('codebuddy-panel');
  if (!panel) return;
  const cb = (BENEFITS.providers || []).find(p => p.id === 'codebuddy');
  if (!cb) { panel.innerHTML = ''; return; }  // 通道未注册：整块不渲染

  // 按「CodeBuddy #N」分组（label 形如「CodeBuddy #1 · 订阅套餐」），qoder 同款切法。
  const groups = new Map();
  const notices = [];
  for (const it of (cb.quota && cb.quota.supported ? cb.quota.items : []) || []) {
    if (it.query_failed || (it.percent == null && it.used == null)) { notices.push(it); continue; }
    const idx = it.label.indexOf(' · ');
    const grp = idx >= 0 ? it.label.slice(0, idx) : 'CodeBuddy';
    const sub = idx >= 0 ? it.label.slice(idx + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  const n = Math.max(...[...groups.keys()]
    .map(g => g.match(/^CodeBuddy #(\d+)$/)).filter(Boolean).map(mm => Number(mm[1])), 1);
  const quotaHtml = [...groups.entries()].map(([grp, its]) => {
    const m = grp.match(/^CodeBuddy #(\d+)$/);
    const idx = m ? Number(m[1]) : null;
    const acct = _codebuddy_acct_for(idx);
    const moveBtns = (m && n > 1) ? _codebuddy_move_btns(idx, n, acct && acct.id) : '';
    const delBtn = acct
      ? `<button class="ghost danger" title="删除该账号（不再使用/凭据失效时）" ` +
        `onclick="codebuddyDeleteAccount('${acct.id}')">✕</button>` : '';
    const renameBtn = acctRenameButton('codebuddy', acct);
    const rowBtns = `<span class="ag-move">${renameBtn}${moveBtns}${delBtn}</span>`;
    // 合计行：codebuddy 的订阅套餐/资源包是并存的份额，加起来才是账号总量
    // （后端 sum_items 声明可合计）。
    const headSum = quotaHeadSum({items: its, sum_items: true});
    return `
    <div class="pat-pkg">
      <span class="pat-pkg-name"${m ? ` data-codebuddy-idx="${idx}"` : ''}>${esc(acct ? (acct.alias || acct.nickname || acct.id) : grp)}</span>
      ${_codebuddy_sub_html(acct)}
      ${headSum ? `<div style="display:flex;align-items:center;margin:0 0 6px">${headSum}</div>` : ''}
      ${quotaItemsHtml(its, 'codebuddy:' + grp)}
      ${rowBtns}
    </div>`;
  }).join('');
  const noticeHtml = notices.map(quotaItemHtml).join('');
  const multi = n > 1;

  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">CodeBuddy</span>
        <span class="tag">${multi ? '多账号 · 自动切换' : 'CodeBuddy 订阅'}</span>
        <span class="grow"></span>
        <span class="muted" style="font-size:11px">${multi ? '401/额度尽自动冷却换号（按登录顺位）' : '401/额度尽自动切换下一个账号'}</span>
        ${providerRefreshButton('codebuddy')}
      </div>
      ${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}
      ${groups.size ? `<div class="pat-quota-grid">${quotaHtml}</div>`
        : '<div class="empty" style="padding:12px 0">暂无账号额度数据（未登录：跑 <span class="mono">buddy login codebuddy</span>）</div>'}
    </div>`;
  loadCodebuddyAccounts();
}

// fillCodebuddyAccounts：账号快照的就地回填（首份快照后的变化、以及静默重绘路径都用它）。
// 就地回填：组名换成账号名、组名后插副标题（qoder 同款）
function fillCodebuddyAccounts(accts) {
  // 非静默：整卡重渲（与渲染同一份快照，按钮组不会叠第二份）；静默：只回填
  if (!ACCT_NO_RERENDER && typeof renderCodebuddyPanel === 'function') { renderCodebuddyPanel(); return; }
  for (const a of accts) {
    const el = (accts.length === 1)
      ? document.querySelector('#codebuddy-panel .pat-pkg-name')
      : document.querySelector(`#codebuddy-panel [data-codebuddy-idx="${a.index}"]`);
    if (!el) continue;
    el.textContent = a.alias || a.nickname || a.id;
    if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-codebuddy-sub')) {
      el.nextElementSibling.remove();
    }
    const sub = _codebuddy_sub_html(a);
    if (sub) el.insertAdjacentHTML('afterend', sub);
  }
}

async function loadCodebuddyAccounts() {
  try {
    // 同一通道在飞时复用（acctFetch）：30s 轮询 + 整页重绘叠加不再并排多发
    const r = await acctFetch('codebuddy', () => api('/ui/api/codebuddy/accounts'));
    if (!r.enabled) { CODEBUDDY_ACCTS = []; return; }
    const accts = r.accounts || [];
    // 数据没变就不动 DOM——renderCodebuddyPanel 已用同一份快照渲染过
    if (JSON.stringify(accts) === JSON.stringify(CODEBUDDY_ACCTS)) return;
    const snapshotMissing = !CODEBUDDY_ACCTS || !CODEBUDDY_ACCTS.length;
    CODEBUDDY_ACCTS = accts;
    if (!accts.length) return;
    if (snapshotMissing) {
      fillCodebuddyAccounts(accts);
      syncQuotaFold();
      return;
    }
    fillCodebuddyAccounts(accts);
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
  }
}

// ---- CODEBUDDY INTL 面板（额度 + ✎ 改名 + ↻ 刷新；▲▼/✕ 仍在 CodeBuddy 主卡
//      ——主卡只管 CN 账号，海外卡照 qoderintl 先例只做展示与改名；
//      rename 走 codebuddy 的共享端点，见 acctRenameSubmit 的 pid 映射）----
let CODEBUDDY_INTL_ACCTS = null;  // 最近一次 accounts 快照（global 区）；render 先用，防 30s 闪回

function renderCodebuddyIntlPanel() {
  const panel = document.getElementById('codebuddyintl-panel');
  if (!panel) return;
  const provider = (BENEFITS.providers || []).find(p => p.id === 'codebuddyintl' && !p.disabled);
  if (!provider) { panel.innerHTML = ''; return; }

  // 按「CodeBuddy 海外版 #N」分组（label 形如「CodeBuddy 海外版 #1 · 订阅套餐」），
  // CN 卡同款切法；组名兜底（单账号无前缀）用「CodeBuddy 海外版」。
  const groups = new Map();
  const notices = [];
  for (const it of (provider.quota && provider.quota.supported ? provider.quota.items : []) || []) {
    if (it.query_failed || (it.percent == null && it.used == null)) { notices.push(it); continue; }
    const idx = it.label.indexOf(' · ');
    const grp = idx >= 0 ? it.label.slice(0, idx) : 'CodeBuddy 海外版';
    const sub = idx >= 0 ? it.label.slice(idx + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  const quotaHtml = [...groups.entries()].map(([grp, its]) => {
    const m = grp.match(/^CodeBuddy 海外版 #(\d+)$/);
    const inRegion = (CODEBUDDY_INTL_ACCTS || []);
    const acct = m ? inRegion.find(a => a && a.index === Number(m[1])) || null
      : (inRegion.length === 1 ? inRegion[0] : null);
    const title = acct ? (acct.alias || acct.nickname || acct.id) : grp;
    const headSum = quotaHeadSum({items: its, sum_items: true});
    return `<div class="pat-pkg"><span class="pat-pkg-name"${m ? ` data-codebuddyintl-idx="${Number(m[1])}"` : ''}>${esc(title)}</span>` +
      `${acctSubHtml(acct, null, 'data-codebuddyintl-sub')}` +
      `${headSum ? `<div style="display:flex;align-items:center;margin:0 0 6px">${headSum}</div>` : ''}` +
      `${quotaItemsHtml(its, 'codebuddyintl:' + grp)}` +
      `${acct ? `<span class="ag-move">${acctRenameButton('codebuddyintl', acct)}</span>` : ''}</div>`;
  }).join('');
  const noticeHtml = notices.map(quotaItemHtml).join('');
  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head"><span class="name">CodeBuddy 海外版</span>
        <span class="tag">Global · 额度与签到</span><span class="grow"></span>
        ${providerRefreshButton('codebuddyintl')}
      </div>
      ${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}
      ${groups.size ? `<div class="pat-quota-grid">${quotaHtml}</div>`
        : '<div class="empty" style="padding:12px 0">暂无海外账号额度数据（<span class="mono">buddy login codebuddy --region global</span> 后重启 Buddy）</div>'}
    </div>`;
  loadCodebuddyIntlAccounts();
}

// fillCodebuddyIntlAccounts：账号快照的就地回填（首份快照后的变化、以及静默重绘路径都用它）。
// 就地回填：组名换成账号名、组名后插副标题（CN 卡同款）
function fillCodebuddyIntlAccounts(accts) {
  // 非静默：整卡重渲（与渲染同一份快照，按钮组不会叠第二份）；静默：只回填
  if (!ACCT_NO_RERENDER && typeof renderCodebuddyIntlPanel === 'function') { renderCodebuddyIntlPanel(); return; }
  // 非静默：整卡重渲（与渲染同一份快照，按钮组不会叠第二份）；静默：只回填
  if (!ACCT_NO_RERENDER && typeof renderCodebuddyIntlPanel === 'function') { renderCodebuddyIntlPanel(); return; }
  for (const a of accts) {
    const el = (accts.length === 1)
      ? document.querySelector('#codebuddyintl-panel .pat-pkg-name')
      : document.querySelector(`#codebuddyintl-panel [data-codebuddyintl-idx="${a.index}"]`);
    if (!el) continue;
    el.textContent = a.alias || a.nickname || a.id;
    if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-codebuddyintl-sub')) {
      el.nextElementSibling.remove();
    }
    const sub = acctSubHtml(a, null, 'data-codebuddyintl-sub');
    if (sub) el.insertAdjacentHTML('afterend', sub);
  }
}

async function loadCodebuddyIntlAccounts() {
  try {
    // 同一通道在飞时复用（acctFetch）：30s 轮询 + 整页重绘叠加不再并排多发
    const r = await acctFetch('codebuddyintl', () => api('/ui/api/codebuddyintl/accounts'));
    if (!r.enabled) { CODEBUDDY_INTL_ACCTS = []; return; }
    const accts = r.accounts || [];
    // 数据没变就不动 DOM——renderCodebuddyIntlPanel 已用同一份快照渲染过
    if (JSON.stringify(accts) === JSON.stringify(CODEBUDDY_INTL_ACCTS)) return;
    const snapshotMissing = !CODEBUDDY_INTL_ACCTS || !CODEBUDDY_INTL_ACCTS.length;
    CODEBUDDY_INTL_ACCTS = accts;
    if (!accts.length) return;
    if (snapshotMissing) {
      fillCodebuddyIntlAccounts(accts);
      syncQuotaFold();
      return;
    }
    fillCodebuddyIntlAccounts(accts);
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
  }
}
