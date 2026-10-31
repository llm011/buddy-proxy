// ---- QODER 面板（kimi 同款布局：每账号一块，标题=账号邮箱、副标题=状态，
//      ▲▼ 顺位 / ✕ 删除。组件全部复用 kimi/antigravity 的）----
// 数据两路：额度走 /ui/api/benefits 里 qoder 条目（label 带「Qoder #N · 」前缀，
// 组名/副标题由账号数据回填），账号状态走 /ui/api/qoder/accounts（纯本地不触网）。
let QODER_ACCTS = null;  // 最近一次 accounts 快照；render 先用它，避免每 30s 闪回「Qoder #N」
let QODER_MOVING = false;  // 面板账号操作（重排/删除）在途：期间忽略新的点按

function _qoder_acct_for(idx, region) {
  // 额度标签的「#N」是**区内**顺位（quota 按区域过滤后编号），快照的 index 是
  // 全列表顺位——按区域过滤后取第 N 个才对得上。idx=null（单账号组名无前缀）
  // 只在区内恰有一个账号时能对上。
  if (!QODER_ACCTS) return null;
  const inRegion = QODER_ACCTS.filter(a => (a.region || 'cn') === (region || 'cn'));
  if (idx == null) return inRegion.length === 1 ? inRegion[0] : null;
  return inRegion[idx - 1] || null;
}

function _qoder_sub_html(a, withRegion) {
  // 副标题（进度条上面那行 muted 小字）：区域 / 冷却
  // （「token 剩 Xh」删了：access token 几小时自动刷新，读了只会误导）
  // 整块包一层 data-qoder-sub 容器：就地回填靠这个属性删旧插新——模型受限行
  // 以前不带标记，回填时删不掉，状态每变一次就多积一行（真机实报的重复）。
  if (!a) return '';
  const rows = [];
  const bits = [];
  if (withRegion !== false && a.region) bits.push(a.region === 'cn' ? 'CN 区' : 'Global 区');
  for (const c of a.cooling || []) {
    const left = c.minutes_left >= 120 ? (c.minutes_left / 60).toFixed(1) + 'h' : c.minutes_left + 'min';
    bits.push(`${c.kind === 'quota' ? '额度' : '账号'}冷却 ${left}`);
  }
  if (bits.length) {
    rows.push(`<div class="muted" style="font-size:11px;margin:1px 0 6px">${esc(bits.join(' · '))}</div>`);
  }
  // 模型受限小字（覆盖表反查的 models_limited）：转发会自动跳过受限模型，
  // 提醒这个账号调不了哪些。完整名单塞 title 悬浮——十来个模型名平铺会把卡撑高
  const lim = a.models_limited || [];
  if (lim.length) {
    rows.push(`<div class="muted" style="font-size:11px;margin:1px 0 6px" title="${esc(lim.join('、'))}">` +
      `模型受限（仅 Qwen3.8 两档可用；其余 ${lim.length} 个调不了）</div>`);
  }
  return rows.length ? `<div data-qoder-sub>${rows.join('')}</div>` : '';
}

function _qoder_move_btns(idx, n, id) {
  // 上/下移按钮（与 _kimi_move_btns 同构；顺位语义按快照里该 id 的实际位次挪）
  const arg = id ? `,'${id}'` : '';
  return `<button class="ghost" title="上移（更优先使用）" ${idx <= 1 ? 'disabled' : ''} ` +
    `onclick="qoderMoveAccount(${idx},-1${arg})">▲</button>` +
    `<button class="ghost" title="下移" ${idx >= n ? 'disabled' : ''} ` +
    `onclick="qoderMoveAccount(${idx},1${arg})">▼</button>`;
}

function qoderConfirmDelete(id) {
  const a = (QODER_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.name || a.email || a.id)) || id;
  return confirmAccountDelete({
    title: '删除 Qoder 账号',
    name,
    extra: '该账号的凭据文件一并移除，转发不再使用它。不再使用或凭据失效' +
      '（转发持续 401/403）的账号删掉后即不再白耗一轮 failover。',
  });
}

async function qoderMoveAccount(idx, delta, id) {
  if (QODER_MOVING) return;
  QODER_MOVING = true;
  try {
    if (!QODER_ACCTS) {  // 按钮随额度数据先到、账号状态可能还没回：补一次快照
      // 这里**不走** acctFetch：加载器整个在飞去重，借到的可能是重排前那趟，
      // 拿旧顺序去算下一位次就挪错人了
      const r0 = await api('/ui/api/qoder/accounts');
      QODER_ACCTS = r0.accounts || [];
    }
    const accts = QODER_ACCTS;
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
    const r = await api('/ui/api/qoder/accounts/order', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids})});
    QODER_ACCTS = r.accounts || [];
    const movedName = (accts.find(a => a.id === moved) || {}).name || moved;
    toast(`${movedName} 已移到顺位 #${to}`);
    refreshAll();
  } catch (e) { toast('调整失败: ' + e.message, true); }
  finally { QODER_MOVING = false; }
}

async function qoderDeleteAccount(id) {
  if (QODER_MOVING) return;
  if (!(await qoderConfirmDelete(id))) return;
  const a = (QODER_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.name || a.email || a.id)) || id;
  QODER_MOVING = true;
  try {
    const r = await api('/ui/api/qoder/accounts/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})});
    QODER_ACCTS = r.accounts || [];
    toast(`${name} 已删除`);
    refreshAll();
  } catch (e) { toast('删除失败: ' + e.message, true); }
  finally {
    QODER_MOVING = false;
    ACCT_CONFIRM_OPEN = false;
  }
}

function renderQoderPanel() {
  const panel = document.getElementById('qoder-panel');
  if (!panel) return;
  const qoder = (BENEFITS.providers || []).find(p => p.id === 'qoder' && !p.disabled);
  if (!qoder) { panel.innerHTML = ''; return; }  // 通道未注册（没加 --qoder）：整块不渲染

  // 按「Qoder #N」分组（label 形如「Qoder #1 · 订阅额度」），kimi 同款切法。
  const groups = new Map();
  const notices = [];
  for (const it of (qoder.quota && qoder.quota.supported ? qoder.quota.items : []) || []) {
    if (it.query_failed || (it.percent == null && it.used == null)) { notices.push(it); continue; }
    const idx = it.label.indexOf(' · ');
    const grp = idx >= 0 ? it.label.slice(0, idx) : 'Qoder';
    const sub = idx >= 0 ? it.label.slice(idx + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  const n = Math.max(...[...groups.keys()]
    .map(g => g.match(/^Qoder #(\d+)$/)).filter(Boolean).map(mm => Number(mm[1])), 1);
  const quotaHtml = [...groups.entries()].map(([grp, its]) => {
    const m = grp.match(/^Qoder #(\d+)$/);
    const idx = m ? Number(m[1]) : null;
    const acct = _qoder_acct_for(idx);
    const moveBtns = (m && n > 1) ? _qoder_move_btns(idx, n, acct && acct.id) : '';
    const delBtn = acct
      ? `<button class="ghost danger" title="删除该账号（不再使用/凭据失效时）" ` +
        `onclick="qoderDeleteAccount('${acct.id}')">✕</button>` : '';
    const renameBtn = acctRenameButton('qoder', acct);
    const rowBtns = `<span class="ag-move">${renameBtn}${moveBtns}${delBtn}</span>`;
    // 合计行：qoder 的订阅额度/加油包/专属积分是并存的份额，加起来才是账号
    // 总量（后端 sum_items 声明可合计）。quotaHeadSum 原本服务「列表头部」，
    // 借用它产出同款「剩 X / Y」chip，放进账号卡标题行下。
    const headSum = quotaHeadSum({items: its, sum_items: true});
    return `
    <div class="pat-pkg">
      <span class="pat-pkg-name"${m ? ` data-qoder-idx="${idx}"` : ''}>${esc(acct ? (acct.alias || acct.name || acct.email || acct.id) : grp)}</span>
      ${_qoder_sub_html(acct)}
      ${headSum ? `<div style="display:flex;align-items:center;margin:0 0 6px">${headSum}</div>` : ''}
      ${quotaItemsHtml(its, 'qoder:' + grp)}
      ${rowBtns}
    </div>`;
  }).join('');
  const noticeHtml = notices.map(quotaItemHtml).join('');
  const multi = n > 1;

  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">Qoder</span>
        <span class="tag">${multi ? '多账号 · 自动切换' : 'Qoder 订阅'}</span>
        <span class="grow"></span>
        <span class="muted" style="font-size:11px">${multi ? '403/额度尽自动冷却换号（按登录顺位）' : '403/额度尽自动切换下一个账号'}</span>
        ${providerRefreshButton('qoder')}
      </div>
      ${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}
      ${groups.size ? `<div class="pat-quota-grid">${quotaHtml}</div>`
        : '<div class="empty" style="padding:12px 0">暂无账号额度数据（未登录：跑 <span class="mono">buddy login qoder</span>）</div>'}
    </div>`;
  loadQoderAccounts();
}

// fillQoderAccounts：账号快照的就地回填（首份快照后的变化、以及静默重绘路径都用它）。
// 就地回填：组名换成账号名、组名后插副标题（kimi 同款）。
// 只回填 CN 账号（面板只显示 CN 额度），位次按区内顺位——与 quota 标签的
// 「Qoder #N」编号同口径，全球账号排在前面也不串位。
function fillQoderAccounts(accts) {
  // 非静默：整卡重渲（与渲染同一份快照，按钮组不会叠第二份）；静默：只回填
  if (!ACCT_NO_RERENDER && typeof renderQoderPanel === 'function') { renderQoderPanel(); return; }
  // 非静默：整卡重渲（与渲染同一份快照，按钮组不会叠第二份）；静默：只回填
  if (!ACCT_NO_RERENDER && typeof renderQoderPanel === 'function') { renderQoderPanel(); return; }
  const cnAccts = accts.filter(a => (a.region || 'cn') === 'cn');
  cnAccts.forEach((a, i) => {
    const pos = i + 1;
    const el = (cnAccts.length === 1)
      ? document.querySelector('#qoder-panel .pat-pkg-name')
      : document.querySelector(`#qoder-panel [data-qoder-idx="${pos}"]`);
    if (!el) return;
    el.textContent = a.alias || a.name || a.email || a.id;
    if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-qoder-sub')) {
      el.nextElementSibling.remove();
    }
    const sub = _qoder_sub_html(a);
    if (sub) el.insertAdjacentHTML('afterend', sub);
  });
}

async function loadQoderAccounts() {
  try {
    // 同一通道在飞时复用（acctFetch）：30s 轮询 + 整页重绘叠加不再并排多发
    const r = await acctFetch('qoder', () => api('/ui/api/qoder/accounts'));
    if (!r.enabled) { QODER_ACCTS = []; return; }
    const accts = r.accounts || [];
    // 数据没变就不动 DOM——renderQoderPanel 已用同一份快照渲染过
    if (JSON.stringify(accts) === JSON.stringify(QODER_ACCTS)) return;
    const snapshotMissing = !QODER_ACCTS || !QODER_ACCTS.length;
    QODER_ACCTS = accts;
    if (!accts.length) return;
    if (snapshotMissing) {
      fillQoderAccounts(accts);
      syncQuotaFold();
      return;
    }
    // 就地回填（只回填 CN 账号、按区内顺位，见 fillQoderAccounts）
    fillQoderAccounts(accts);
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
  }
}


// ---- QODER INTL 面板（额度 + ✎ 改名；▲▼/✕ 仍在 Qoder 主卡——主卡只管 CN 账号，
//      海外号此前无任何改名入口、显示链只能落到 UUID，故 ✎ 落在本卡；
//      rename 走 qoder 的共享端点，见 acctRenameSubmit 的 pid 映射）----
let INTL_ACCTS = null;  // 最近一次 accounts 快照的 global 区子集；render 先用，防 30s 闪回「Global」

function renderQoderIntlPanel() {
  const panel = document.getElementById('qoderintl-panel');
  if (!panel) return;
  const provider = (BENEFITS.providers || []).find(p => p.id === 'qoderintl' && !p.disabled);
  if (!provider) { panel.innerHTML = ''; return; }

  const groups = new Map();
  const notices = [];
  for (const it of (provider.quota && provider.quota.supported ? provider.quota.items : []) || []) {
    if (it.query_failed || (it.percent == null && it.used == null)) { notices.push(it); continue; }
    const idx = it.label.indexOf(' · ');
    const raw = idx >= 0 ? it.label.slice(0, idx) : 'Global';
    const grp = raw.startsWith('Qoder #') ? raw.replace('Qoder #', 'Global #') : raw;
    const sub = idx >= 0 ? it.label.slice(idx + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  const quotaHtml = [...groups.entries()].map(([grp, its]) => {
    // 组标题=账号名（alias 优先）：「Global」和「Global #N」是区内顺位的兜底名，
    // 多账号时分不清谁是谁；账号数据没到/没对上时回落组名。
    const m = grp.match(/^Global #(\d+)$/);
    const inRegion = (INTL_ACCTS || []);
    const acct = m ? (inRegion[Number(m[1]) - 1] || null)
      : (inRegion.length === 1 ? inRegion[0] : null);
    const title = acct ? (acct.alias || acct.name || acct.email || acct.id) : grp;
    const headSum = quotaHeadSum({items: its, sum_items: true});
    return `<div class="pat-pkg"><span class="pat-pkg-name"${m ? ` data-qoderintl-idx="${Number(m[1])}"` : ''}>${esc(title)}</span>` +
      `${_qoder_sub_html(acct, false)}` +
      `${headSum ? `<div style="display:flex;align-items:center;margin:0 0 6px">${headSum}</div>` : ''}` +
      `${quotaItemsHtml(its, 'qoderintl:' + grp)}` +
      `${acct ? `<span class="ag-move">${acctRenameButton('qoderintl', acct)}</span>` : ''}</div>`;
  }).join('');
  const noticeHtml = notices.map(quotaItemHtml).join('');
  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head"><span class="name">Qoder 海外版</span>
        <span class="tag">Global · 额度与签到</span><span class="grow"></span>
        ${providerRefreshButton('qoderintl')}
      </div>
      ${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}
      ${groups.size ? `<div class="pat-quota-grid">${quotaHtml}</div>`
        : '<div class="empty" style="padding:12px 0">暂无海外账号额度数据（登录后重启 Buddy 注册通道）</div>'}
    </div>`;
  loadQoderIntlAccounts();
}

// fillQoderIntlAccounts：账号快照的就地回填（首份快照后的变化、以及静默重绘路径都用它）。
// 就地回填：组名换成账号名、组名后插副标题（CN 卡同款）
function fillQoderIntlAccounts(accts) {
  // 非静默：整卡重渲（与渲染同一份快照，按钮组不会叠第二份）；静默：只回填
  if (!ACCT_NO_RERENDER && typeof renderQoderIntlPanel === 'function') { renderQoderIntlPanel(); return; }
  // 非静默：整卡重渲（与渲染同一份快照，按钮组不会叠第二份）；静默：只回填
  if (!ACCT_NO_RERENDER && typeof renderQoderIntlPanel === 'function') { renderQoderIntlPanel(); return; }
  accts.forEach((a, i) => {
    const pos = i + 1;
    const el = (accts.length === 1)
      ? document.querySelector('#qoderintl-panel .pat-pkg-name')
      : document.querySelector(`#qoderintl-panel [data-qoderintl-idx="${pos}"]`);
    if (!el) return;
    el.textContent = a.alias || a.name || a.email || a.id;
    if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-qoder-sub')) {
      el.nextElementSibling.remove();
    }
    const sub = _qoder_sub_html(a, false);
    if (sub) el.insertAdjacentHTML('afterend', sub);
  });
}

async function loadQoderIntlAccounts() {
  try {
    // 同一通道在飞时复用（acctFetch）：30s 轮询 + 整页重绘叠加不再并排多发
    const r = await acctFetch('qoderintl', () => api('/ui/api/qoder/accounts'));
    if (!r.enabled) { INTL_ACCTS = []; return; }
    const accts = (r.accounts || []).filter(a => a.region === 'global');
    // 数据没变就不动 DOM——renderQoderIntlPanel 已用同一份快照渲染过
    if (JSON.stringify(accts) === JSON.stringify(INTL_ACCTS)) return;
    const snapshotMissing = !INTL_ACCTS || !INTL_ACCTS.length;
    INTL_ACCTS = accts;
    if (!accts.length) return;
    if (snapshotMissing) {
      fillQoderIntlAccounts(accts);
      syncQuotaFold();
      return;
    }
    fillQoderIntlAccounts(accts);
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
  }
}
