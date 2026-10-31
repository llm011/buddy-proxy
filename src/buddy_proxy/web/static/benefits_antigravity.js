// ---- ANTIGRAVITY 面板（trae PAT 同款布局：每账号一块，标题=邮箱、副标题=状态，
//      下面是它自己的各组进度条）----
// 数据两路：额度走 /ui/api/benefits 里 antigravity 条目（label 带「AG #N · 」前缀，
// 组名/副标题由账号数据回填），账号状态走 /ui/api/antigravity/accounts（纯本地不触
// 网）。未登录时整个面板隐藏。
let AG_ACCTS = null;  // 最近一次 accounts 快照；render 先用它，避免每 30s 闪回「AG #N」
let AG_MOVING = false;  // 面板账号操作（重排/删除）在途：期间忽略新的点按

function _ag_acct_for(idx) {
  // idx=null（单账号组名无 AG #N 前缀）只在恰有一个账号时能对上
  if (!AG_ACCTS) return null;
  if (idx == null) return AG_ACCTS.length === 1 ? AG_ACCTS[0] : null;
  return AG_ACCTS.find(a => a.index === idx) || null;
}

function _ag_sub_html(a) {
  // 副标题（进度条上面那行 muted 小字）：缺 project / 冷却。
  // blacklist（Google 风控拉黑）不是「冷却」语义——标注成疑似拉黑引导删除；
  // 时长 2h 以上按小时显示（6h 拉黑档读着不像「360min」）。
  // 「token 剩 Xh」删掉了：access token 几小时自动刷新，读了只会误导
  //（qoder 那边还曾把毫秒当秒算出 4.97 亿小时）——用户 2026-10-05 确认三通道一并删。
  if (!a) return '';
  const bits = [];
  if (!a.project_id) bits.push('缺 project');
  for (const c of a.cooling || []) {
    const left = c.minutes_left >= 120 ? (c.minutes_left / 60).toFixed(1) + 'h' : c.minutes_left + 'min';
    bits.push(c.kind === 'blacklist' ? `疑似拉黑 剩${left}`
      : `${c.kind === 'quota' ? '额度' : '账号'}冷却 ${left}`);
  }
  return bits.length
    ? `<div class="muted" style="font-size:11px;margin:1px 0 6px" data-ag-sub>${esc(bits.join(' · '))}</div>`
    : '';
}

function _ag_move_btns(idx, n, id) {
  // 上/下移按钮（idx 是渲染时的 1-based 顺位）；首尾各自禁用对应的那个。
  // 有账号 id 就内联带上：重排响应回来后、面板重绘前（要先重取一次额度），
  // DOM 里还挂着旧按钮，其 idx 按旧顺序标号——点按时按「快照里该 id 的实际
  // 位次」挪才不会动到别的账号（只凭 idx 挪实测会把刚调好的顺序点回去）。
  // id 字符集由后端 _ID_RE 约束（字母数字 ._@-），内联进 onclick 安全。
  // 只出裸按钮——外壳 <span class="ag-move"> 由调用方统一搭（✕ 删除同排）。
  const arg = id ? `,'${id}'` : '';
  return `<button class="ghost" title="上移（更优先使用）" ${idx <= 1 ? 'disabled' : ''} ` +
    `onclick="agMoveAccount(${idx},-1${arg})">▲</button>` +
    `<button class="ghost" title="下移" ${idx >= n ? 'disabled' : ''} ` +
    `onclick="agMoveAccount(${idx},1${arg})">▼</button>`;
}

// 调 POST /ui/api/antigravity/accounts/order 提交完整顺序；后端重写
// priority 后 quota 缓存键（quota_epoch 带 priority）随之失效，所以这里
// 拿到响应后直接 refreshAll() 走一遍 benefits 重取，进度条组顺序即更新。
//
// id 是按钮渲染时所在账号（可选）：按钮可能比面板旧一拍（重排响应已回、
// 额度还没重取完），按快照里该 id 的**当前**位次挪，而不是按按钮上的 idx
// ——否则连点两下 = 连提交两次同方向重排（第二次拿旧 idx 在新顺序里挪到了
// 别的账号，整轮被点回去）。在飞期间 AG_MOVING 挡住后续点按。
async function agMoveAccount(idx, delta, id) {
  if (AG_MOVING) return;
  AG_MOVING = true;
  try {
    // 按钮随额度数据先到、账号状态可能还没回：按需补一次快照再算全量 id
    if (!AG_ACCTS) {
      // 这里**不走** acctFetch：加载器整个在飞去重，借到的可能是重排前那趟，
      // 拿旧顺序去算下一位次就挪错人了
      const r0 = await api('/ui/api/antigravity/accounts');
      AG_ACCTS = r0.accounts || [];
    }
    const accts = AG_ACCTS;
    if (id) {  // 按 id 校正到快照里的真实位次（面板重绘滞后于快照时 idx 会过期）
      const at = accts.findIndex(a => a.id === id);
      if (at < 0) return;  // 该账号已不在快照里（刚被删？）：宁可不动，别按过期 idx 挪错人
      idx = at + 1;
    }
    const to = idx + delta;  // idx 是 1-based 顺位，to 是目标顺位
    if (to < 1 || to > accts.length) return;
    const ids = accts.map(a => a.id);
    const [moved] = ids.splice(idx - 1, 1);
    ids.splice(to - 1, 0, moved);
    const r = await api('/ui/api/antigravity/accounts/order', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids})});
    AG_ACCTS = r.accounts || [];
    const movedEmail = (accts.find(a => a.id === moved) || {}).email || moved;
    toast(`${movedEmail} 已移到顺位 #${to}`);
    refreshAll();
  } catch (e) { toast('调整失败: ' + e.message, true); }
  finally { AG_MOVING = false; }
}

// 删除确认弹窗已抽到 benefits_accounts.js 的 confirmAccountDelete（公共件）。
// 曾在本文件里还有一份同名旧定义（锁是 AG_CONFIRM_OPEN、按钮挂 __agDelYes），
// 经典脚本后加载覆盖前加载，浏览器里实际生效的是它——而 dumate 的 finally 清的是
// 公共件的 ACCT_CONFIRM_OPEN，确认过一次 DuMate ✕ 后 AG_CONFIRM_OPEN 永久卡死、
// 后续所有通道删除确认被当成「取消」。删除旧定义、全链统一走公共件的锁。
function agConfirmDelete(id) {
  const a = (AG_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.email || a.id)) || id;
  return confirmAccountDelete({
    title: '删除 Antigravity 账号',
    name,
    extra: '该账号的凭据文件一并移除，转发不再使用它。' +
      '被 Google 拉黑的账号（副标题标「疑似拉黑」）删掉后即不再白耗一轮 failover。',
  });
}

// 删除账号（POST /ui/api/antigravity/accounts/delete）：被 Google 拉黑
// （403 Verify your account，副标题会标「疑似拉黑」）或不再使用的账号从
// 轮换里摘掉——留着只会每轮 failover 白打一次上游。确认弹窗（agConfirmDelete）
// 后提交，与顺位调整共用 AG_MOVING 在飞锁（确认后的连点不再发第二个请求）。
async function agDeleteAccount(id) {
  if (AG_MOVING) return;
  if (!(await agConfirmDelete(id))) return;
  const a = (AG_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.email || a.id)) || id;
  AG_MOVING = true;
  try {
    const r = await api('/ui/api/antigravity/accounts/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})});
    AG_ACCTS = r.accounts || [];
    toast(`${name} 已删除`);
    refreshAll();
  } catch (e) { toast('删除失败: ' + e.message, true); }
  finally {
    AG_MOVING = false;
    ACCT_CONFIRM_OPEN = false;  // 确认时从 confirmAccountDelete 接手的锁，到这里才放
  }
}

// 渠道标题「↻ 刷新」：一次作废并重查该 provider 的全部账号额度，其余通道不打扰。
// POST 已返回完整 snapshot 时直接更新全局并重渲；只为兼容旧响应才 force GET。
const QREFRESH = {};  // pid -> 在飞标记：转圈期间忽略再点（整页 30s 重建 innerHTML，
                      // 按钮态挂 DOM 会被刷掉，和 QUOTA_FOLD 同理放模块级）
let QREFRESH_SEQ = 0;
let QREFRESH_APPLIED_SEQ = 0;
async function refreshProviderQuota(pid, btn) {
  if (QREFRESH[pid]) return;
  QREFRESH[pid] = true;
  const requestSeq = ++QREFRESH_SEQ;
  const prev = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⟳'; btn.classList.add('spinning'); }
  try {
    const snapshot = await api('/ui/api/benefits/refresh', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider: pid})});
    // refresh 接口已经返回作废缓存后的完整快照，直接采用可少一次 GET，也避免
    // 写后读取竞态。兼容旧服务未返回 providers 的响应时再走强制拉取。
    if (snapshot && Array.isArray(snapshot.providers)) {
      // 不同 provider 可以并行刷；较早请求若较晚返回，快照里可能还带着另一个
      // provider 刷新前的数据。只允许最后发起且尚未被采用的完整快照覆盖全局。
      if (requestSeq > QREFRESH_APPLIED_SEQ) {
        QREFRESH_APPLIED_SEQ = requestSeq;
        BENEFITS = snapshot;
        renderBenefits();
      }
    } else {
      await loadData(['benefits'], true);
    }
  } catch (e) { toast('刷新失败: ' + e.message, true); }
  finally {
    QREFRESH[pid] = false;
    if (btn) { btn.disabled = false; btn.innerHTML = prev; btn.classList.remove('spinning'); }
  }
}

function renderAntigravityPanel() {
  const panel = document.getElementById('antigravity-panel');
  if (!panel) return;
  const ag = (BENEFITS.providers || []).find(p => p.id === 'antigravity' && !p.disabled && p.quota && p.quota.supported);
  if (!ag) { panel.innerHTML = ''; return; }

  // 按「AG #N」分组（label 形如「AG #1 · Gemini 组（…）」），与 trae 的 PAT #N 同款切法；
  // 单账号时 label 无前缀，整体落进一个组。query_failed 说明条不进分组，横贯全宽展示。
  const groups = new Map();
  const notices = [];
  for (const it of ag.quota.items || []) {
    if (it.query_failed) { notices.push(it); continue; }
    const idx = it.label.indexOf(' · ');
    const grp = idx >= 0 ? it.label.slice(0, idx) : 'Antigravity';
    const sub = idx >= 0 ? it.label.slice(idx + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  // 组名=邮箱、副标题=账号状态：额度接口（benefits）不含邮箱，先挂 data-ag-idx
  // 占位；已有 AG_ACCTS 快照则同步直接渲染（30s 轮询重建面板不闪内部代号），
  // 没有就等 loadAntigravityAccounts 回填。
  // 顺位按钮的界标 n：全部「AG #N」序号的最大值（首屏首渲就有按钮，
  // 不依赖 accounts 快照时序）。
  const n = Math.max(...[...groups.keys()]
    .map(g => g.match(/^AG #(\d+)$/)).filter(Boolean).map(mm => Number(mm[1])), 1);
  const quotaHtml = [...groups.entries()].map(([grp, its]) => {
    const m = grp.match(/^AG #(\d+)$/);
    const idx = m ? Number(m[1]) : null;
    const acct = _ag_acct_for(idx);
    // 顺位按钮：多账号才有意义。界标 n 直接取分组序号的最大值（自包含，
    // 不依赖 AG_ACCTS 快照的到达时序——首屏首渲就有按钮，快照只是点按时
    // 提交全量 id 列表的数据源）。正常情况所有账号都有额度分组，n=账号数；
    // 个别账号额度查询失败时它没有卡片，按「可见卡片」定界正好。
    // acct.id 内联进按钮：点按时按快照里的真实位次挪（面板重绘延迟见 agMoveAccount）。
    const moveBtns = (m && n > 1) ? _ag_move_btns(idx, n, acct && acct.id) : '';
    // 删除按钮：要账号 id 才能删，快照没到（首屏首渲）时先不渲染，等下轮。
    // id 字符集由后端 _ID_RE 约束（字母数字 ._@-），内联进 onclick 安全。
    // ghost danger：与 app.js「删除此目标」同款危险样式（悬停变红），且
    // .ag-move button.danger 常红——小尺寸 ✕ 里红色是唯一的危险提示。
    const delBtn = acct
      ? `<button class="ghost danger" title="删除该账号（被 Google 拉黑/不再使用时）" ` +
        `onclick="agDeleteAccount('${acct.id}')">✕</button>` : '';
    // ✎ 改名：同样要 id（同 ✕ 快照没到先不渲）。放按钮组最前=标题行行尾，
    // 别插在名字和副标题之间——就地回填靠「名字元素的下一个兄弟是副标题」定位。
    const renameBtn = acctRenameButton('antigravity', acct);
    // 账号级按钮只保留改名/排序/删除；渠道级 ↻ 统一放面板标题。
    const rowBtns = `<span class="ag-move">${renameBtn}${moveBtns}${delBtn}</span>`;
    return `
    <div class="pat-pkg">
      <span class="pat-pkg-name"${m ? ` data-ag-idx="${idx}"` : ''}>${esc(acct ? (acct.alias || acct.email) : grp)}</span>
      ${_ag_sub_html(acct)}
      ${quotaItemsHtml(its, 'ag:' + grp)}
      ${rowBtns}
    </div>`;
  }).join('') || '<div class="empty" style="padding:12px 0">无额度数据</div>';
  const noticeHtml = notices.map(quotaItemHtml).join('');

  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">Antigravity</span>
        <span class="tag">多账号 · 自动切换</span>
        <span class="grow"></span>
        <span class="muted" style="font-size:11px">429/403 自动冷却换号（按登录顺位）</span>
        ${providerRefreshButton('antigravity')}
      </div>
      ${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}
      <div class="pat-quota-grid">${quotaHtml}</div>
    </div>`;
  loadAntigravityAccounts();
}

// fillAntigravityAccounts：账号快照的就地回填（首份快照后的变化、以及静默重绘路径都用它）。
// 就地回填：组名换成邮箱、组名后插副标题。多账号按 data-ag-idx 对应；
// 单账号组名没有前缀（后端 label 不加），唯一 .pat-pkg-name 直接替换。
// 名字旁边的按钮组（✎/▲▼/✕）也按账号重建：✕/✎ 要账号 id、▲▼ 把 id 内联在
// onclick 里——快照（id、顺位）一变，旧按钮就指错人。
function fillAntigravityAccounts(accts) {
  // 非静默：整卡重渲（与渲染同一份快照，按钮组不会叠第二份）；静默：只回填
  if (!ACCT_NO_RERENDER && typeof renderAntigravityPanel === 'function') {
    renderAntigravityPanel();
    return;
  }
  return _fillAntigravityAccounts(accts);
}

function _fillAntigravityAccounts(accts) {
  // 界标 n 与 renderAntigravityPanel 同口径：全部「AG #N」序号的最大值
  const n = Math.max(...accts.map(a => Number(a.index) || 0), accts.length, 1);
  for (const a of accts) {
    const el = (accts.length === 1)
      ? document.querySelector('#antigravity-panel .pat-pkg-name')
      : document.querySelector(`#antigravity-panel [data-ag-idx="${a.index}"]`);
    if (!el) continue;
    el.textContent = a.alias || a.email;
    // 副标题总是重写：状态变了（冷却结束消失）要跟上，不是只防重复插
    if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-ag-sub')) {
      el.nextElementSibling.remove();
    }
    const sub = _ag_sub_html(a);
    if (sub) el.insertAdjacentHTML('afterend', sub);
    const pkg = el.closest('.pat-pkg');
    const old = pkg && pkg.querySelector(':scope > .ag-move');
    if (old) old.remove();
    const moveBtns = (n > 1) ? _ag_move_btns(a.index, n, a.id) : '';
    const delBtn = `<button class="ghost danger" title="删除该账号（被 Google 拉黑/不再使用时）" ` +
      `onclick="agDeleteAccount('${a.id}')">✕</button>`;
    el.insertAdjacentHTML('beforeend', acctRowButtons('antigravity',
      acctRenameButton('antigravity', a) + moveBtns, delBtn));
  }
}

async function loadAntigravityAccounts() {
  try {
    // 同一通道在飞时复用（acctFetch）：30s 轮询 + 整页重绘叠加不再并排多发
    const r = await acctFetch('antigravity', () => api('/ui/api/antigravity/accounts'));
    if (!r.enabled) { AG_ACCTS = []; return; }
    const accts = r.accounts || [];
    // 数据没变就不动 DOM——renderAntigravityPanel 已用同一份快照渲染过
    if (JSON.stringify(accts) === JSON.stringify(AG_ACCTS)) return;
    const snapshotMissing = !AG_ACCTS || !AG_ACCTS.length;
    AG_ACCTS = accts;
    if (!accts.length) return;
    if (snapshotMissing) {
      // 首份快照到位（首屏刚开/刚登录）：带着快照整卡重渲——邮箱直出、
      // ▲▼/✕ 按钮带上账号 id。重渲尾部的再拉取数据已同、走上面的早退，
      // 不会循环。
      // 静默重绘期间（强刷签到只换签到区）别整卡重渲——会冲掉别处 DOM，
      // 改就地回填；按钮由 fill 一并补上
      fillAntigravityAccounts(accts);
      // 重渲换了整块 DOM（邮箱、副标题都变了高度），校准要跟着走一次，
      // 否则首屏这次校准要拖到下个 30s 轮询：renderBenefits 末尾那次
      // syncQuotaFold 跑在本函数 await 之前，看不到重渲后的高度。
      syncQuotaFold();
      return;
    }
    fillAntigravityAccounts(accts);
    // 就地回填会插入/移除副标题行（高度变了），重新校准一次折叠：
    // 不补这一步，卡在阈值附近的分组会出现「已经裁掉了但按钮不露」。
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
  }
}
