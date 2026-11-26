// ---- COPILOT 面板（codex/kimi 同款布局：每账号一块，标题=账号名、副标题=状态，
//      ▲▼ 顺位 / ✎ 改名 / ✕ 删除 / 导入账号；组件复用 benefits_accounts.js）----
// 与 codex 的差别：Copilot 凭据是**一把 GitHub token**（`gho_...`），没有刷新，
// 所以导入入口是「粘贴 token」+「从本机 gh 导入」两条，也提供了 device flow 的
// 等价路径（`buddy login copilot`）。额度单位是「次」（premium 交互数），不是
// 积分包，因此没有重置券那种特殊行。
let COPILOT_ACCTS = null;
let COPILOT_MOVING = false;

const _COPILOT_PREFIX_RE = /^Copilot #\d+(?: · |$)/;

function _copilot_acct_for(idx) {
  if (!COPILOT_ACCTS) return null;
  if (idx == null) return COPILOT_ACCTS.length === 1 ? COPILOT_ACCTS[0] : null;
  return COPILOT_ACCTS.find(a => a.index === idx) || null;
}

function _copilot_acct_by_title(who) {
  // 同 codex：**只按名字**认，位次由调用方显式匹配；宁可认不出单开一卡，
  // 也不拿旧编号把额度挂到别的账号上。
  if (!COPILOT_ACCTS || !COPILOT_ACCTS.length) return null;
  const want = String(who || '').replace(_COPILOT_PREFIX_RE, '').trim();
  if (!want) return null;
  return COPILOT_ACCTS.find(a => a.id === want || a.name === want || a.alias === want) || null;
}

function _copilot_sub_html(a) {
  if (!a) return '';
  const bits = [];
  for (const c of a.cooling || []) {
    const left = c.minutes_left >= 120 ? (c.minutes_left / 60).toFixed(1) + 'h' : c.minutes_left + 'min';
    bits.push(`${c.kind === 'quota' ? '额度' : '账号'}冷却 ${left}`);
  }
  if (a.source) bits.push(`来源 ${a.source}`);
  if (a.token === 'missing') bits.push('凭据缺失');
  return bits.length
    ? `<div class="muted" style="font-size:11px;margin:1px 0 6px" data-copilot-sub>${esc(bits.join(' · '))}</div>`
    : '';
}

function copilotConfirmDelete(id) {
  const a = (COPILOT_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.name || a.id)) || id;
  return confirmAccountDelete({
    title: '删除 Copilot 账号',
    name,
    extra: '该账号的 token 文件一并移除，转发不再使用它。token 被 GitHub 撤销' +
      '（副标题持续显示账号冷却且转发失败）的账号删掉后即不再白耗一轮 failover。',
  });
}

async function copilotMoveAccount(idx, delta, id, pin) {
  if (COPILOT_MOVING) return;
  COPILOT_MOVING = true;
  try {
    if (!COPILOT_ACCTS) {
      // 不走 acctFetch：在飞去重借到的可能是重排前那趟，旧顺序会挪错人
      const r0 = await api('/ui/api/copilot/accounts');
      COPILOT_ACCTS = r0.accounts || [];
    }
    const accts = COPILOT_ACCTS;
    if (id) {  // 按 id 校正到快照真实位次（面板重绘滞后时 idx 会过期）
      const at = accts.findIndex(a => a.id === id);
      if (at < 0) return;
      idx = at + 1;
    }
    const to = pin ? 1 : idx + delta;
    if (to < 1 || to > accts.length) return;
    const ids = accts.map(a => a.id);
    const [moved] = ids.splice(idx - 1, 1);
    ids.splice(to - 1, 0, moved);
    const r = await api('/ui/api/copilot/accounts/order', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids})});
    COPILOT_ACCTS = r.accounts || [];
    const movedName = (accts.find(a => a.id === moved) || {}).name || moved;
    toast(`${movedName} 已移到顺位 #${to}`);
    refreshAll();
  } catch (e) { toast('调整失败: ' + e.message, true); }
  finally { COPILOT_MOVING = false; }
}

async function copilotDeleteAccount(id) {
  if (COPILOT_MOVING) return;
  if (!(await copilotConfirmDelete(id))) return;
  const a = (COPILOT_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.name || a.id)) || id;
  COPILOT_MOVING = true;
  try {
    const r = await api('/ui/api/copilot/accounts/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})});
    COPILOT_ACCTS = r.accounts || [];
    toast(`${name} 已删除`);
    refreshAll();
  } catch (e) { toast('删除失败: ' + e.message, true); }
  finally {
    COPILOT_MOVING = false;
    ACCT_CONFIRM_OPEN = false;  // 确认时从 confirmAccountDelete 接手的锁，到这里才放
  }
}

// 导入账号：粘贴 GitHub token；或者点「从本机 gh 导入」。
// **不读 keychain**：token 只来自用户粘贴、`gh auth token`、或 device flow。
function openCopilotImport() {
  document.getElementById('modal-title').textContent = '导入 Copilot 账号';
  document.getElementById('modal-body').innerHTML =
    `<p style="margin:0 0 8px">粘贴一把 GitHub token（<span class="mono">gho_...</span>），` +
    `或点下方按钮从本机 <span class="mono">gh</span> 已登录的账号直接导入。</p>` +
    `<p class="muted" style="margin:0 0 8px;font-size:12px">token 只用于调用 Copilot，` +
    `身份（login）会先经 GitHub <span class="mono">/user</span> 探测再以 0600 落盘。` +
    `没有 gh 也可以：终端跑 <span class="mono">buddy login copilot</span> 走 GitHub device flow。</p>` +
    `<textarea id="copilot-import-text" class="mono" style="width:100%;height:90px;resize:vertical" ` +
    `placeholder="gho_xxxxxxxxxxxxxxxxxxxx"></textarea>` +
    `<div id="copilot-gh-list" style="margin-top:8px"></div>`;
  setModalFoot('<button onclick="closeModal()">取消</button>' +
    '<button onclick="copilotImportSubmit()">导入</button>');
  document.getElementById('overlay').classList.add('show');
  const ta = document.getElementById('copilot-import-text');
  if (ta) ta.focus();
  copilotLoadGhAccounts();
}

async function copilotLoadGhAccounts() {
  const box = document.getElementById('copilot-gh-list');
  if (!box) return;
  box.innerHTML = '<span class="muted" style="font-size:12px">读取本机 gh 登录态…</span>';
  try {
    const r = await api('/ui/api/copilot/gh-accounts');
    const users = r.accounts || [];
    if (!users.length) {
      box.innerHTML = '<span class="muted" style="font-size:12px">本机 gh 没有已登录的账号' +
        '（或未安装 gh）：可以先 <span class="mono">gh auth login</span>，或直接粘贴 token。</span>';
      return;
    }
    box.innerHTML = '<div class="muted" style="font-size:12px;margin-bottom:4px">本机 gh 已登录：</div>' +
      users.map(u => `<button class="ghost" style="margin:0 6px 4px 0" ` +
        `onclick="copilotImportGh('${esc(u).replace(/'/g, "\\'")}')">${esc(u)}</button>`).join('') +
      (users.length > 1
        ? '<button class="ghost" style="margin:0 6px 4px 0" onclick="copilotImportGh(null)">全部导入</button>'
        : '');
  } catch (e) {
    box.innerHTML = `<span class="muted" style="font-size:12px">读取 gh 登录态失败：${esc(e.message)}</span>`;
  }
}

async function copilotImportGh(user) {
  const btns = document.querySelectorAll('#modal-foot button, #copilot-gh-list button');
  btns.forEach(b => { b.disabled = true; });
  try {
    const r = await api('/ui/api/copilot/accounts/import-gh', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(user ? {user} : {})});
    COPILOT_ACCTS = r.accounts || [];
    const errs = r.errors || [];
    if (errs.length) {
      // 部分成功：列表已刷新，把失败原因留在弹窗里（关掉就看不到了）
      const box = document.getElementById('copilot-gh-list');
      if (box) box.innerHTML = errs.map(e => `<div class="muted" style="font-size:12px">${esc(e)}</div>`).join('');
      toast(`部分导入失败：${errs.length} 个`, true);
      refreshAll();
      return;
    }
    closeModal();
    toast(user ? `已从 gh 导入 ${user}` : '已从 gh 导入全部账号');
    refreshAll();
  } catch (e) { toast('导入失败: ' + e.message, true); }
  finally { btns.forEach(b => { b.disabled = false; }); }
}

async function copilotImportSubmit() {
  const ta = document.getElementById('copilot-import-text');
  const token = ((ta && ta.value) || '').trim();
  if (!token) { toast('先粘贴 GitHub token 再导入', true); return; }
  const btns = document.querySelectorAll('#modal-foot button');
  btns.forEach(b => { b.disabled = true; });
  try {
    const r = await api('/ui/api/copilot/accounts/import', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token})});
    COPILOT_ACCTS = r.accounts || [];
    if (ta) ta.value = '';  // 别把 token 留在页面里
    closeModal();
    toast('Copilot 账号已导入');
    refreshAll();
  } catch (e) { toast('导入失败: ' + e.message, true); }
  finally { btns.forEach(b => { b.disabled = false; }); }
}

// 模型目录刷新：不涉及额度，单独一个按钮（与 ↻ 额度刷新分开，
// 免得用户以为「刷新模型」会顺带重查配额）。
async function copilotRefreshModels(btn) {
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const r = await api('/ui/api/copilot/models/refresh', {method: 'POST'});
    if (r.error) toast(`刷新模型部分失败：${r.error}`, true);
    else toast(`模型目录已刷新：${r.count} 个`);
  } catch (e) { toast('刷新模型失败: ' + e.message, true); }
  finally { if (btn) { btn.disabled = false; btn.textContent = old; } }
}

function renderCopilotPanel() {
  const panel = document.getElementById('copilot-panel');
  if (!panel) return;
  const cp = (BENEFITS.providers || []).find(p => p.id === 'copilot' && !p.disabled);
  if (!cp) { panel.innerHTML = ''; return; }  // 通道未注册（没加 --copilot）：整块不渲染

  const items = (cp.quota && cp.quota.supported ? cp.quota.items : []) || [];
  const { groups, notices } = groupAccountsByPrefix(items, 'Copilot', 'Copilot');
  // 只有说明条/没窗口的账号也要出卡片：按账号快照补空组，按钮才有地方挂。
  // 单账号时后端不拼 `Copilot #1 · ` 前缀，条目归进 `'Copilot'` 组；此时卡已在，
  // 不能再补 `'Copilot #1'` 空卡，否则同一个号会出两张卡（一有额度一空卡）。
  if (!((COPILOT_ACCTS || []).length === 1 && groups.has('Copilot'))) {
    for (const a of COPILOT_ACCTS || []) {
      const g = `Copilot #${a.index}`;
      if (!groups.has(g)) groups.set(g, []);
    }
  }
  const idxOf = grp => {
    const m = grp.match(/^Copilot #(\d+)(?: · |$)/);
    return m ? Number(m[1]) : null;
  };
  const ordered = [...groups.keys()].sort((a, b) => {
    const ia = idxOf(a), ib = idxOf(b);
    return ia == null || ib == null ? (ia == null) - (ib == null) : ia - ib;
  });
  const n = Math.max(...ordered.map(g => idxOf(g) || 0), 1);
  const quotaHtml = ordered.map(grp => {
    const its = groups.get(grp) || [];
    const idx = idxOf(grp);
    const m = idx != null;
    // 组名自带 `Copilot #N · 名字` 时按名字反查；否则按位次取快照
    const acct = _copilot_acct_by_title(grp) || _copilot_acct_for(idx);
    const moveBtns = (m && n > 1) ? acctMoveButtons('copilot', idx, n, acct ? acct.id : '') : '';
    const delBtn = acctDeleteButton('copilot', acct, 'token 撤销/不再使用时');
    const renameBtn = acctRenameButton('copilot', acct);
    const rowBtns = `<span class="ag-move">${renameBtn}${moveBtns}${acctDisableButton('copilot', acct)}${delBtn}</span>`;
    const shown = acct ? (acct.alias || acct.name || acct.id) : grp.replace(/^Copilot #\d+ · /, '');
    return `
    <div class="pat-pkg">
      <span class="pat-pkg-name"${m ? ` data-copilot-idx="${idx}"` : ''}>${esc(shown)}</span>
      ${_copilot_sub_html(acct)}
      ${quotaItemsHtml(its, 'copilot:' + grp)}
      ${rowBtns}
    </div>`;
  }).join('');
  const noticeHtml = notices.map(quotaItemHtml).join('');
  const multi = n > 1;

  const fold = quotaProviderFold(ordered.length ? items : 'empty', 'copilot');
  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">Copilot</span>
        <span class="tag">${multi ? '多账号 · 自动切换' : 'GitHub Copilot 订阅 · 自动切换'}</span>${fold.badge}
        <span class="pat-head-mid">
          <span class="muted" style="font-size:11px">${multi ? '429/401 自动冷却换号（按账号顺位）' : '额度耗尽自动切换下一个账号'}</span>
        </span>
        <span class="pat-head-actions">${fold.toggle}${providerLoadBalanceToggle('copilot', multi)}${providerRefreshButton('copilot')}
        <button class="ghost" title="从上游重新拉取模型目录（新模型上线时用）" onclick="copilotRefreshModels(this)">刷新模型</button>
        <button class="ghost" title="粘贴 GitHub token，或从本机 gh 导入（无需安装 Copilot 客户端）" onclick="openCopilotImport()">导入账号</button></span>
      </div>
      ${fold.bodyStart}${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}${ordered.length ? `<div class="pat-quota-grid">${quotaHtml}</div>`
        : '<div class="empty" style="padding:12px 0">暂无账号额度数据</div>'}${fold.bodyEnd}
    </div>`;
  loadCopilotAccounts();
}

// 账号快照就地回填（首份快照后的回填、静默重绘路径都用它）。
function fillCopilotAccounts(accts) {
  if (!ACCT_NO_RERENDER && typeof renderCopilotPanel === 'function') { renderCopilotPanel(); return; }
  for (const a of accts) {
    const el = (accts.length === 1)
      ? document.querySelector('#copilot-panel .pat-pkg-name')
      : document.querySelector(`#copilot-panel [data-copilot-idx="${a.index}"]`);
    if (!el) continue;
    el.textContent = a.alias || a.name || a.id;
    if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-copilot-sub')) {
      el.nextElementSibling.remove();
    }
    const sub = _copilot_sub_html(a);
    if (sub) el.insertAdjacentHTML('afterend', sub);
  }
}

async function loadCopilotAccounts() {
  try {
    const r = await acctFetch('copilot', () => api('/ui/api/copilot/accounts'));
    if (!r.enabled) { COPILOT_ACCTS = []; return; }
    const accts = r.accounts || [];
    if (JSON.stringify(accts) === JSON.stringify(COPILOT_ACCTS)) return;
    COPILOT_ACCTS = accts;
    if (!accts.length) return;
    fillCopilotAccounts(accts);
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
  }
}
