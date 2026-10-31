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
  // who 形如 `Codex #N · 名字` 或单账号的裸名字。这里**只按名字**认（去掉前缀
  // 后比 id/name/alias），不按位次：位次匹配由调用方显式做，免得快照顺位变了
  // 之后拿旧编号把券挂到别的账号上——宁可认不出单开一卡，也不乱挂。
  if (!CODEX_ACCTS || !CODEX_ACCTS.length) return null;
  const want = String(who || '').replace(/^Codex #\d+ · /, '').trim();
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
      // 这里**不走** acctFetch：加载器整个在飞去重，借到的可能是重排前那趟，
      // 拿旧顺序去算下一位次就挪错人了
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
  // 券行顺序跟随 who 里自带的位次（多账号 `Codex #N · X`）；单账号或无位次时
  // 用原始插入顺序，正好落在对应账号卡之后。
  const resetOrder = who => {
    const m = who.match(/^Codex #(\d+)/);
    return m ? Number(m[1]) : 0;
  };
  const resetGrp = new Map();
  const resetSrc = new Map();
  for (const [who, it] of resetByGrp) {
    const grp = grpKey(who);
    if (!resetGrp.has(grp)) {
      resetGrp.set(grp, it);
      resetSrc.set(grp, resetOrder(who));
    }
  }
  const allGrps = new Set([...groups.keys(), ...resetGrp.keys()]);
  // 只有券的账号没有「Codex #N」标题可解析，从 who 里取位次（与卡片编号一致）
  // 才能拿到账号快照、挂上 ▲▼/✎/✕ 并按真实顺位排序。原分组名优先，其次券
  // 行自带的位次。
  const idxOf = grp => {
    const m = grp.match(/^Codex #(\d+)(?: · |$)/);
    if (m) return Number(m[1]);
    return resetSrc.has(grp) ? resetSrc.get(grp) || null : null;
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
    const acct = _codex_acct_for(idx) || _codex_acct_by_title(resetGrp.get(grp)?.who);
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

// fillCodexAccounts：账号快照的就地回填（首份快照后的回填、以及静默重绘路径都用它）。
// 多账号按 data-codex-idx 对应，单账号组名没有前缀，直接替换。
function fillCodexAccounts(accts) {
  // 非静默：整卡重渲（与渲染同一份快照，按钮组不会叠第二份）；静默：只回填
  if (!ACCT_NO_RERENDER && typeof renderCodexPanel === 'function') { renderCodexPanel(); return; }
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
}

async function loadCodexAccounts() {
  try {
    // 同一通道在飞时复用（acctFetch）：30s 轮询 + 整页重绘叠加不再并排多发
    const r = await acctFetch('codex', () => api('/ui/api/codex/accounts'));
    if (!r.enabled) { CODEX_ACCTS = []; return; }
    const accts = r.accounts || [];
    if (JSON.stringify(accts) === JSON.stringify(CODEX_ACCTS)) return;
    const snapshotMissing = !CODEX_ACCTS || !CODEX_ACCTS.length;
    CODEX_ACCTS = accts;
    if (!accts.length) return;
    if (snapshotMissing) {
      fillCodexAccounts(accts);
      syncQuotaFold();
      return;
    }
    fillCodexAccounts(accts);
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
  }
}
