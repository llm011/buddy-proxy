// ---- KIRO 面板 ----
//
// 复用通用多账号额度面板布局与组件（.pat-head / .pat-quota-grid / .pat-pkg /
// .ag-move / quotaAccountTotal / quotaProviderFold / acctRenameButton /
// acctMoveButtons / acctDeleteButton / confirmAccountDelete）：
//   - 两栏网格（.pat-quota-grid），多账号并排各占半宽，与 kimi/codex/copilot 一致
//   - 卡头带跨账号合计、负载均衡开关、刷新、折叠、以及 kiro 特有入口：
//     1. 「导入 auth 文件」——从本机 ~/.aws/sso/cache/ 导入
//     2. 「浏览器授权」——设备码流程兜底

let KIRO_ACCTS = null;
let KIRO_MOVING = false;
let KIRO_LOGIN_TIMER = null;

function _kiro_acct_by_id(id) {
  return (KIRO_ACCTS || []).find(a => a.id === id) || null;
}

function _kiro_acct_for(idx) {
  if (!KIRO_ACCTS) return null;
  if (idx == null) return KIRO_ACCTS.length === 1 ? KIRO_ACCTS[0] : null;
  return KIRO_ACCTS.find(a => a.index === idx) || null;
}

function _kiro_acct_by_title(title) {
  if (!KIRO_ACCTS || !title) return null;
  const clean = String(title).replace(/^Kiro(?:\s+#\d+)?\s+·\s+/, '').trim();
  return KIRO_ACCTS.find(a => a.label === clean || a.id === clean) || null;
}

function _kiro_sub_html(a) {
  if (!a) return '';
  const bits = [];
  if (a.provider) bits.push(a.provider);
  if (a.region) bits.push(a.region);
  if (a.needs_refresh) bits.push('token 待刷新');
  if (!a.has_refresh_token) bits.push('缺 refreshToken');
  return bits.length
    ? `<div class="muted" style="font-size:11px;margin:1px 0 6px" data-kiro-sub>${esc(bits.join(' · '))}</div>`
    : '';
}

function renderKiroPanel() {
  const panel = document.getElementById('kiro-panel');
  if (!panel) return;
  const kiro = (BENEFITS.providers || []).find(p => p.id === 'kiro' && !p.disabled);
  if (!kiro) { panel.innerHTML = ''; return; }   // 未启用（没加 --kiro）：整块不渲染

  const items = (kiro.quota && kiro.quota.supported ? kiro.quota.items : []) || [];
  // 后端 label 形如「Kiro #1 · addbackteam1@addbacktech.com · Credit」（多账号）
  // 或「Kiro · addbackteam1@addbacktech.com · Credit」（单账号）。
  // 这里按「第 1 段 + #N」拆出账号组名，并把末尾明细名留给 quotaItemsHtml。
  const groups = new Map();
  const notices = [];
  for (const it of items) {
    if (it.query_failed || (it.percent == null && it.used == null)) { notices.push(it); continue; }
    const parts = String(it.label || '').split(' · ');
    let grp = 'Kiro';
    let sub = it.label;
    if (parts.length >= 3 && /^Kiro(?:\s+#\d+)?$/.test(parts[0])) {
      grp = `${parts[0]} · ${parts[1]}`;
      sub = parts.slice(2).join(' · ');
    } else if (parts.length === 2) {
      grp = parts[0];
      sub = parts[1];
    }
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  const accts = KIRO_ACCTS || [];
  // 查不到额度的账号（如新导入还没刷新 benefits、或单账号暂未拉到）也要出卡片，
  // 这样改名/删号/顺位按钮总有地方挂。
  if (!(accts.length === 1 && groups.size === 1)) {
    for (const a of accts) {
      const hasMine = [...groups.keys()].some(g => {
        const m = g.match(/^Kiro #(\d+)(?: · |$)/);
        if (m) return Number(m[1]) === a.index;
        return g === `Kiro · ${a.label || a.id}`;
      });
      if (!hasMine) {
        const key = accts.length > 1
          ? `Kiro #${a.index} · ${a.label || a.id}`
          : `Kiro · ${a.label || a.id}`;
        groups.set(key, []);
      }
    }
  }
  const idxOf = grp => {
    const m = grp.match(/^Kiro #(\d+)(?: · |$)/);
    return m ? Number(m[1]) : null;
  };
  const ordered = [...groups.keys()].sort((a, b) => {
    const ia = idxOf(a), ib = idxOf(b);
    return ia == null || ib == null ? (ia == null) - (ib == null) : ia - ib;
  });
  const n = Math.max(...ordered.map(g => idxOf(g) || 0), accts.length, 1);
  const quotaHtml = ordered.map(grp => {
    const its = groups.get(grp) || [];
    const idx = idxOf(grp);
    const m = idx != null;
    const acct = _kiro_acct_by_title(grp) || _kiro_acct_for(idx);
    const moveBtns = (m && n > 1) ? acctMoveButtons('kiro', idx, n, acct ? acct.id : '') : '';
    const delBtn = acctDeleteButton('kiro', acct, 'token 失效/不再使用时');
    const renameBtn = acctRenameButton('kiro', acct);
    const rowBtns = `<span class="ag-move">${renameBtn}${moveBtns}${acctDisableButton('kiro', acct)}${delBtn}</span>`;
    const shown = acct ? (acct.label || acct.id)
      : grp.replace(/^Kiro(?:\s+#\d+)?\s+·\s+/, '');
    return `
    <div class="pat-pkg">
      <span class="pat-pkg-name"${m ? ` data-kiro-idx="${idx}"` : ''}>${esc(shown)}</span>
      ${_kiro_sub_html(acct)}
      ${its.length ? quotaItemsHtml(its, 'kiro:' + grp)
        : '<div class="muted" style="font-size:11px;padding:4px 0">暂无额度数据</div>'}
      ${rowBtns}
    </div>`;
  }).join('');
  const noticeHtml = notices.map(quotaItemHtml).join('');
  const multi = accts.length > 1 || n > 1;
  const levelTag = (kiro.quota && kiro.quota.level) ? String(kiro.quota.level) : 'AWS CodeWhisperer';
  const fold = quotaProviderFold(ordered.length ? items : 'empty', 'kiro');

  panel.innerHTML = `<div class="chart-card" style="margin-top:14px" data-kiro-panel>
    <div class="pat-head">
      <span class="name">Kiro</span>
      <span class="tag">${esc(multi ? '多账号 · 自动切换' : levelTag)}</span>${fold.badge}
      <span class="pat-head-mid">${quotaAccountTotal(groups, !!(kiro.quota && kiro.quota.sum_items))}
        <span class="muted" style="font-size:11px">${multi ? '402/429/403 自动切换（按导入顺位）' : `${accts.length} 个账号`}</span>
      </span>
      <span class="pat-head-actions">${fold.toggle}${providerLoadBalanceToggle('kiro', multi)}${providerRefreshButton('kiro')}
      <button class="ghost" onclick="kiroOpenImport()">导入 auth 文件</button>
      <button class="ghost" onclick="kiroStartLogin()">浏览器授权</button></span>
    </div>
    ${fold.bodyStart}${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}${ordered.length ? `<div class="pat-quota-grid">${quotaHtml}</div>`
      : '<div class="empty" style="padding:10px 0">还没有账号</div>'}
    <div id="kiro-login-box" style="margin-top:10px"></div>${fold.bodyEnd}
  </div>`;
  loadKiroAccounts();
}

// ---- 账号加载/顺位/删除 ----------------------------------------------------

function fillKiroAccounts(accts) {
  if (!ACCT_NO_RERENDER && typeof renderKiroPanel === 'function') { renderKiroPanel(); return; }
  for (const a of accts) {
    const el = (accts.length === 1)
      ? document.querySelector('#kiro-panel .pat-pkg-name')
      : document.querySelector(`#kiro-panel [data-kiro-idx="${a.index}"]`);
    if (!el) continue;
    el.textContent = a.label || a.id;
    if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-kiro-sub')) {
      el.nextElementSibling.remove();
    }
    const sub = _kiro_sub_html(a);
    if (sub) el.insertAdjacentHTML('afterend', sub);
  }
}

function loadKiroAccounts() {
  return acctFetch('kiro', () => (async function() {
    try {
      const r = await api('/ui/api/kiro/accounts');
      if (!r.enabled) { KIRO_ACCTS = []; return; }
      const list = r.accounts || [];
      if (JSON.stringify(list) === JSON.stringify(KIRO_ACCTS)) return;  // 没变不动 DOM
      KIRO_ACCTS = list;
      fillKiroAccounts(list);
      syncQuotaFold();
    } catch (e) {
      // 静默：账号接口抖动不清面板，下轮 30s 自动重试
    }
  })());
}

async function kiroMoveAccount(idx, delta, id) {
  if (KIRO_MOVING) return;
  KIRO_MOVING = true;
  try {
    if (!KIRO_ACCTS) {
      const r0 = await api('/ui/api/kiro/accounts');
      KIRO_ACCTS = r0.accounts || [];
    }
    const ids = KIRO_ACCTS.map(a => a.id);
    const from = ids.indexOf(id);
    if (from < 0) return;
    const to = from + delta;
    if (to < 0 || to >= ids.length) return;
    ids.splice(to, 0, ids.splice(from, 1)[0]);
    const r = await api('/ui/api/kiro/accounts/order', {
      method: 'POST', body: JSON.stringify({ids}),
    });
    if (r && r.ok) {
      KIRO_ACCTS = (r.accounts || []).map((a, i) => ({...a, index: i + 1}));
      toast('顺位已更新');
      refreshAll();
    }
  } catch (e) {
    toast('调整失败: ' + e.message, true);
  } finally {
    KIRO_MOVING = false;
  }
}

function kiroConfirmDelete(id) {
  const a = _kiro_acct_by_id(id);
  const name = (a && a.label) || id;
  return confirmAccountDelete({
    title: '删除 Kiro 账号',
    name,
    extra: '该账号的本地凭据一并移除，转发不再使用它。' +
      '如果只是 token 过期，重新在 Kiro IDE 登录后「导入 auth 文件」即可恢复，不必删号。',
  });
}

async function kiroDeleteAccount(id) {
  if (KIRO_MOVING) return;
  if (!(await kiroConfirmDelete(id))) return;
  const a = _kiro_acct_by_id(id);
  const name = (a && a.label) || id;
  KIRO_MOVING = true;
  try {
    await api(`/ui/api/kiro/accounts/${encodeURIComponent(id)}`, {method: 'DELETE'});
    KIRO_ACCTS = (KIRO_ACCTS || []).filter(x => x.id !== id);
    toast(`${name} 已删除`);
    refreshAll();
  } catch (e) {
    toast('删除失败: ' + e.message, true);
  } finally {
    KIRO_MOVING = false;
    ACCT_CONFIRM_OPEN = false;
  }
}

// ---- 导入 auth 文件 --------------------------------------------------------

async function kiroOpenImport() {
  let files = [];
  try {
    const r = await api('/ui/api/kiro/auth-files');
    files = r.files || [];
  } catch (e) {
    toast('读取候选文件失败: ' + e.message, true);
    return;
  }
  const rows = files.length
    ? files.map((f, i) => `<label style="display:flex;gap:8px;align-items:flex-start;margin:6px 0;
        padding:8px;border:1px solid var(--line);border-radius:8px;cursor:pointer">
        <input type="radio" name="kiro-auth-file" value="${esc(f.path)}" ${i === 0 ? 'checked' : ''}>
        <span style="min-width:0">
          <span class="mono" style="font-size:12px">${esc(f.name)}</span>
          <span class="muted" style="font-size:11px"> · ${esc(f.region)} · ${esc(f.provider || '未知')}</span>
          <div class="muted" style="font-size:11px;overflow-wrap:anywhere">${esc(f.path)}</div>
        </span>
      </label>`).join('')
    : `<p class="empty" style="padding:12px 0">本机没有找到可导入的 kiro auth 文件。</p>
       <p class="muted" style="font-size:12px">请先在 Kiro IDE 里登录一次，
       凭据会写到 <span class="mono">~/.aws/sso/cache/kiro-auth-token.json</span>，
       再回来点这里。也可以直接用「浏览器授权」。</p>`;

  document.getElementById('modal-title').textContent = '导入 Kiro auth 文件';
  document.getElementById('modal-body').innerHTML = rows +
    `<div style="margin-top:10px">
      <label class="muted" style="font-size:12px">显示名（可选）</label>
      <input id="kiro-import-label" type="text" placeholder="如：主号 / 工作号" style="width:100%">
    </div>`;
  setModalFoot(`<button onclick="closeModal()">取消</button>` +
    (files.length ? `<button class="primary" onclick="kiroDoImport()">导入</button>` : ''));
  document.getElementById('overlay').classList.add('show');
}

async function kiroDoImport() {
  const picked = document.querySelector('input[name="kiro-auth-file"]:checked');
  const labelEl = document.getElementById('kiro-import-label');
  const path = picked ? picked.value : '';
  const label = labelEl ? labelEl.value.trim() : '';
  if (!path) { toast('请选择一个文件', true); return; }
  try {
    const r = await api('/ui/api/kiro/import', {
      method: 'POST', body: JSON.stringify({path, label}),
    });
    closeModal();
    toast(`✓ 已导入 ${(r.account && r.account.label) || '账号'}`);
    await loadKiroAccounts();
    await loadData(['benefits'], true);   // 额度卡要跟着出现
  } catch (e) {
    toast('导入失败: ' + e.message, true);
  }
}

// ---- 浏览器授权（设备码） --------------------------------------------------

async function kiroStartLogin() {
  const box = document.getElementById('kiro-login-box');
  try {
    const r = await api('/ui/api/kiro/login/start', {
      method: 'POST', body: JSON.stringify({}),
    });
    if (!r || !r.verification_uri) throw new Error('没有拿到验证地址');
    if (box) {
      box.innerHTML = `<div style="padding:10px;border:1px solid var(--line);border-radius:8px">
        <div style="font-size:12px;margin-bottom:6px">在浏览器打开下面的地址，输入验证码完成授权：</div>
        <div><a href="${esc(r.verification_uri)}" target="_blank" rel="noopener">${esc(r.verification_uri)}</a></div>
        <div style="margin:8px 0"><span class="muted" style="font-size:12px">验证码</span>
          <b class="mono" style="font-size:18px;letter-spacing:2px">${esc(r.user_code)}</b></div>
        <div class="muted" style="font-size:11px" id="kiro-login-status">等待授权…（${Math.round((r.expires_in || 600) / 60)} 分钟内有效）</div>
      </div>`;
    }
    const interval = Math.max(3, (r.interval || 5)) * 1000;
    clearInterval(KIRO_LOGIN_TIMER);
    KIRO_LOGIN_TIMER = setInterval(async () => {
      try {
        const st = await api('/ui/api/kiro/login/poll', {method: 'POST', body: '{}'});
        const el = document.getElementById('kiro-login-status');
        if (st.status === 'ok') {
          clearInterval(KIRO_LOGIN_TIMER); KIRO_LOGIN_TIMER = null;
          if (el) el.textContent = '授权成功，正在刷新账号…';
          toast('✓ 登录成功');
          await loadKiroAccounts();
          await loadData(['benefits'], true);
        } else if (st.status === 'expired') {
          clearInterval(KIRO_LOGIN_TIMER); KIRO_LOGIN_TIMER = null;
          if (el) el.textContent = '验证码已过期，请重新点击「浏览器授权」。';
        } else if (el) {
          el.textContent = `等待授权…${st.status === 'slow_down' ? '（请稍候）' : ''}`;
        }
      } catch (e) {
        clearInterval(KIRO_LOGIN_TIMER); KIRO_LOGIN_TIMER = null;
        const el = document.getElementById('kiro-login-status');
        if (el) el.textContent = '轮询失败：' + e.message;
      }
    }, interval);
  } catch (e) {
    if (box) box.innerHTML = `<div class="empty" style="padding:8px 0">发起授权失败：${esc(e.message)}</div>`;
  }
}
