// ---- TRAE WORK 面板（qoder 同款布局：每账号一块，标题=账号名、副标题=区域/冷却，
//      ▲▼ 顺位 / ✕ 删除 / ✎ 改名 / ↻ 刷新）----
// 数据两路：额度走 /ui/api/benefits 里 trae 条目（label 带「Trae #N · 」前缀），
// 账号状态走 /ui/api/trae/accounts（纯本地不触网，failover.accounts_status）。
// trae 与 qoder 的结构差异：总额度条是 head_only（各权益包的合计），明细列表
// **不能相加**（权益包是总额度的拆分，sum 会重复计算）——合计行改用 head_only
// 那条自己的数字（quotaHeadSum 非 sum_items 分支正好「取第一条有数的」）。
// 签到仍在上方「各通道签到」卡里（supports_checkin=True），本面板只管额度。
let TRAE_ACCTS = null;   // 最近一次 accounts 快照；render 先用它，避免每 30s 闪回「Trae #N」
let TRAE_MOVING = false; // 面板账号操作（重排/删除）在途：期间忽略新的点按

function _trae_acct_for(idx) {
  // idx=null（单账号组名无 Trae #N 前缀）只在恰有一个账号时能对上
  if (!TRAE_ACCTS) return null;
  if (idx == null) return TRAE_ACCTS.length === 1 ? TRAE_ACCTS[0] : null;
  return TRAE_ACCTS.find(a => a.index === idx) || null;
}

function _trae_sub_html(a) {
  // 副标题：区域 / 冷却（acctSubHtml 公共件；trae 无通道特有 bit）
  return acctSubHtml(a, null, 'data-trae-sub');
}

function _trae_move_btns(idx, n, id) {
  return acctMoveButtons('trae', idx, n, id);
}

function traeConfirmDelete(id) {
  const a = (TRAE_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.alias || a.nickname || a.id)) || id;
  return confirmAccountDelete({
    title: '删除 Trae 账号',
    name,
    extra: '该账号的凭据文件一并移除，转发不再使用它。不再使用或凭据失效' +
      '（转发持续 401）的账号删掉后即不再白耗一轮 failover。',
  });
}

async function traeMoveAccount(idx, delta, id) {
  if (TRAE_MOVING) return;
  TRAE_MOVING = true;
  try {
    if (!TRAE_ACCTS) {  // 按钮随额度数据先到、账号状态可能还没回：补一次快照
      // 这里**不走** acctFetch：加载器整个在飞去重，借到的可能是重排前那趟，
      // 拿旧顺序去算下一位次就挪错人了
      const r0 = await api('/ui/api/trae/accounts');
      TRAE_ACCTS = r0.accounts || [];
    }
    const accts = TRAE_ACCTS;
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
    const r = await api('/ui/api/trae/accounts/order', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids})});
    TRAE_ACCTS = r.accounts || [];
    const movedName = (accts.find(a => a.id === moved) || {}).nickname || moved;
    toast(`${movedName} 已移到顺位 #${to}`);
    refreshAll();
  } catch (e) { toast('调整失败: ' + e.message, true); }
  finally { TRAE_MOVING = false; }
}

async function traeDeleteAccount(id) {
  if (TRAE_MOVING) return;
  if (!(await traeConfirmDelete(id))) return;
  const a = (TRAE_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.alias || a.nickname || a.id)) || id;
  TRAE_MOVING = true;
  try {
    const r = await api('/ui/api/trae/accounts/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})});
    TRAE_ACCTS = r.accounts || [];
    toast(`${name} 已删除`);
    refreshAll();
  } catch (e) { toast('删除失败: ' + e.message, true); }
  finally {
    TRAE_MOVING = false;
    ACCT_CONFIRM_OPEN = false;
  }
}

function renderTraePanel() {
  const panel = document.getElementById('trae-panel');
  if (!panel) return;
  const trae = (BENEFITS.providers || []).find(p => p.id === 'trae' && !p.disabled);
  // 通道未注册（没加 --trae）或海外的 traeintl（独立 id）：整块不渲染
  if (!trae) { panel.innerHTML = ''; return; }

  // 按「Trae #N」分组（label 形如「Trae #1 · 总额度」）。qoder 同款切法，
  // 两点差异：
  // - head_only 条（「总额度」= 各权益包合计）不进明细列表，但**要**用作
  //   合计行数字——quotaItemsHtml 跳过它、quotaHeadSum 非 sum_items 分支取
  //   「第一条有数的」正好是它（items[0]，后端按总额度在前排列）。
  // - 无数字条目分两种：带组前缀的（如「免费」包，只有到期日没有额度数字）
  //   归进各账号子卡片当一行明细（横贯全宽反而像独立告警——用户 2026-10-06
  //   反馈）；连前缀都没有的孤条（未登录说明等）才进 notices 横贯全宽。
  const groups = new Map();
  const notices = [];
  for (const it of (trae.quota && trae.quota.supported ? trae.quota.items : []) || []) {
    if (it.query_failed ||
        (it.percent == null && it.used == null && it.label.indexOf(' · ') < 0)) {
      notices.push(it); continue;
    }
    if (it.head_only) {
      // head_only 条挂到「它前缀对应的组」的合计位；组还没出现就先攒着，
      // 循环结束后按 grp 补进去（后端顺序保证它在该组条目最前）
      const cut = it.label.indexOf(' · ');
      const grp = cut >= 0 ? it.label.slice(0, cut) : 'Trae';
      if (!groups.has(grp)) groups.set(grp, []);
      groups.get(grp).unshift(Object.assign({}, it, {
        label: cut >= 0 ? it.label.slice(cut + 3) : it.label}));
      continue;
    }
    const idx = it.label.indexOf(' · ');
    const grp = idx >= 0 ? it.label.slice(0, idx) : 'Trae';
    const sub = idx >= 0 ? it.label.slice(idx + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  const n = Math.max(...[...groups.keys()]
    .map(g => g.match(/^Trae #(\d+)$/)).filter(Boolean).map(mm => Number(mm[1])), 1);
  const quotaHtml = [...groups.entries()].map(([grp, its]) => {
    const m = grp.match(/^Trae #(\d+)$/);
    const idx = m ? Number(m[1]) : null;
    const acct = _trae_acct_for(idx);
    const moveBtns = (m && n > 1) ? _trae_move_btns(idx, n, acct && acct.id) : '';
    const delBtn = acct
      ? `<button class="ghost danger" title="删除该账号（不再使用/凭据失效时）" ` +
        `onclick="traeDeleteAccount('${acct.id}')">✕</button>` : '';
    const renameBtn = acctRenameButton('trae', acct);
    const rowBtns = `<span class="ag-move">${renameBtn}${moveBtns}${delBtn}</span>`;
    // 合计行：总额度那条（head_only，unshift 到组首）单独供数——各权益包是
    // 它的明细不能相加，quotaHeadSum 走非 sum_items 分支取第一条有数的。
    const headSum = quotaHeadSum({items: its});
    return `
    <div class="pat-pkg">
      <span class="pat-pkg-name"${m ? ` data-trae-idx="${idx}"` : ''}>${esc(acct ? (acct.alias || acct.nickname || acct.id) : grp)}</span>
      ${_trae_sub_html(acct)}
      ${headSum ? `<div style="display:flex;align-items:center;margin:0 0 6px">${headSum}</div>` : ''}
      ${quotaItemsHtml(its, 'trae:' + grp)}
      ${rowBtns}
    </div>`;
  }).join('');
  const noticeHtml = notices.map(quotaItemHtml).join('');
  const multi = n > 1;

  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">Trae</span>
        <span class="tag">${multi ? '多账号 · 自动切换' : 'Trae Work'}</span>
        <span class="grow"></span>
        <span class="muted" style="font-size:11px">${multi ? '401/额度尽自动冷却换号（按登录顺位）' : '401/额度尽自动切换下一个账号'}</span>
        ${providerRefreshButton('trae')}
      </div>
      ${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}
      ${groups.size ? `<div class="pat-quota-grid">${quotaHtml}</div>`
        : '<div class="empty" style="padding:12px 0">暂无账号额度数据（未登录：跑 <span class="mono">buddy login trae</span>）</div>'}
    </div>`;
  loadTraeAccounts();
}

// fillTraeAccounts：账号快照的就地回填（首份快照后的变化、以及静默重绘路径都用它）。
// 就地回填：组名换成账号名、组名后插副标题（qoder 同款）
function fillTraeAccounts(accts) {
  // 非静默：整卡重渲（与渲染同一份快照，按钮组不会叠第二份）；静默：只回填
  if (!ACCT_NO_RERENDER && typeof renderTraePanel === 'function') { renderTraePanel(); return; }
  for (const a of accts) {
    const el = (accts.length === 1)
      ? document.querySelector('#trae-panel .pat-pkg-name')
      : document.querySelector(`#trae-panel [data-trae-idx="${a.index}"]`);
    if (!el) continue;
    el.textContent = a.alias || a.nickname || a.id;
    if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-trae-sub')) {
      el.nextElementSibling.remove();
    }
    const sub = _trae_sub_html(a);
    if (sub) el.insertAdjacentHTML('afterend', sub);
  }
}

async function loadTraeAccounts() {
  try {
    // 同一通道在飞时复用（acctFetch）：30s 轮询 + 整页重绘叠加不再并排多发
    const r = await acctFetch('trae', () => api('/ui/api/trae/accounts'));
    if (!r.enabled) { TRAE_ACCTS = []; return; }
    const accts = r.accounts || [];
    // 数据没变就不动 DOM——renderTraePanel 已用同一份快照渲染过
    if (JSON.stringify(accts) === JSON.stringify(TRAE_ACCTS)) return;
    const snapshotMissing = !TRAE_ACCTS || !TRAE_ACCTS.length;
    TRAE_ACCTS = accts;
    if (!accts.length) return;
    if (snapshotMissing) {
      fillTraeAccounts(accts);
      syncQuotaFold();
      return;
    }
    fillTraeAccounts(accts);
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
  }
}


// ---- TRAE INTL 面板（海外额度独立展示；上游没有每日签到端点）----
function renderTraeIntlPanel() {
  const panel = document.getElementById('traeintl-panel');
  if (!panel) return;
  const provider = (BENEFITS.providers || []).find(p => p.id === 'traeintl' && !p.disabled);
  if (!provider) { panel.innerHTML = ''; return; }

  // 海外额度标签由 provider 明确使用「Trae 海外版 #N · …」。保持账号分组，
  // 不并回 CN 面板，避免两区额度与币种混在同一张卡里。
  const groups = new Map();
  const notices = [];
  for (const it of (provider.quota && provider.quota.supported ? provider.quota.items : []) || []) {
    const cut = it.label.indexOf(' · ');
    if (it.query_failed || (it.percent == null && it.used == null && cut < 0)) {
      notices.push(it); continue;
    }
    const grp = cut >= 0 ? it.label.slice(0, cut) : 'Trae 海外版';
    const sub = cut >= 0 ? it.label.slice(cut + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    const item = Object.assign({}, it, {label: sub});
    if (it.head_only) groups.get(grp).unshift(item);
    else groups.get(grp).push(item);
  }
  const quotaHtml = [...groups.entries()].map(([grp, its]) => {
    const headSum = quotaHeadSum({items: its});
    return `<div class="pat-pkg"><span class="pat-pkg-name">${esc(grp)}</span>` +
      `${headSum ? `<div style="display:flex;align-items:center;margin:0 0 6px">${headSum}</div>` : ''}` +
      `${quotaItemsHtml(its, 'traeintl:' + grp)}</div>`;
  }).join('');
  const noticeHtml = notices.map(quotaItemHtml).join('');

  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head"><span class="name">Trae 海外版</span>
        <span class="tag">Global · 海外额度</span><span class="grow"></span>
        <span class="muted" style="font-size:11px">海外版无每日签到</span>
        ${providerRefreshButton('traeintl')}
      </div>
      ${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}
      ${groups.size ? `<div class="pat-quota-grid">${quotaHtml}</div>`
        : '<div class="empty" style="padding:12px 0">暂无海外账号额度数据（<span class="mono">buddy login traeintl</span> 后重启 Buddy）</div>'}
    </div>`;
}
