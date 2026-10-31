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
      // 这里**不走** acctFetch：加载器整个在飞去重，借到的可能是重排前那趟，
      // 拿旧顺序去算下一位次就挪错人了
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

// fillKimiAccounts：账号快照的就地回填（首份快照后的回填、以及静默重绘路径都用它）。
// 组名换成账号名、组名后插副标题（antigravity 同款）。
// 多账号按 data-kimi-idx 对应，单账号组名没有前缀，直接替换。
function fillKimiAccounts(accts) {
  // 非静默：整卡重渲（与渲染同一份快照，按钮组不会叠第二份）；静默：只回填
  if (!ACCT_NO_RERENDER && typeof renderKimiPanel === 'function') { renderKimiPanel(); return; }
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
}

async function loadKimiAccounts() {
  try {
    // 同一通道在飞时复用（acctFetch）：30s 轮询 + 整页重绘叠加不再并排多发
    const r = await acctFetch('kimi', () => api('/ui/api/kimi/accounts'));
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
      fillKimiAccounts(accts);
      syncQuotaFold();
      return;
    }
    fillKimiAccounts(accts);
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
  }
}
