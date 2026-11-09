// ---- KIRO 面板 ----
//
// 与 kimi/codex 同款布局（每账号一块：名字 / 状态 / ▲▼ / ✎ / ✕），但多出两个
// **kiro 特有的**入口：
//
//   1. 「导入 auth 文件」——从本机 Kiro IDE 的 sso cache 导入。这是主路径：
//      Kiro 的官方登录要过 AWS SSO/IAM Identity Center，设备码流程需要企业
//      start URL（只有 Kiro 客户端自己知道），所以「IDE 里登录过 → 一键导入」
//      远比「让用户去查 start URL」现实。
//   2. 「浏览器授权」——设备码流程兜底。打开地址输码即可，无需本机装 IDE。
//
// 额度区不需要本文件参与：kiro 走**通用额度卡**（见 benefits.js 的
// _PANEL_PROVIDERS 说明），条目 label 形如 `Kiro #1 · 主号 · Credit`。
// 本面板只管账号（谁在、什么状态、怎么加/删/改）。

let KIRO_ACCTS = null;
let KIRO_MOVING = false;
let KIRO_LOGIN_TIMER = null;

function _kiro_acct_by_id(id) {
  return (KIRO_ACCTS || []).find(a => a.id === id) || null;
}

function _kiro_sub_html(a) {
  if (!a) return '';
  const bits = [];
  if (a.provider) bits.push(a.provider);
  if (a.region) bits.push(a.region);
  if (a.needs_refresh) bits.push('token 待刷新');
  else if (a.expires_at) {
    const left = Math.round(a.expires_at - Date.now() / 1000);
    if (left > 0) bits.push(`token ${left > 3600 ? (left / 3600).toFixed(1) + 'h' : Math.max(1, Math.round(left / 60)) + 'min'} 有效`);
    else bits.push('token 已过期');
  }
  if (!a.has_refresh_token) bits.push('缺 refreshToken');
  return bits.length
    ? `<div class="muted" style="font-size:11px;margin:1px 0 6px">${esc(bits.join(' · '))}</div>`
    : '';
}

/** 单个账号的额度明细：复用通用卡的 ``quotaItemHtml``（进度条 + 剩余/已用 + 重置）。

 * 额度条目的 label 形如 ``Kiro #1 · 主号 · Credit``（多账号）或
 * ``Kiro · 主号 · Credit``（单账号），本函数按**账号名**把条目分回各自的卡。
 * 认不出归属的一律不显示——错挂到别的账号比少显示一条更糟。
 */
function _kiro_quota_html(kiro, acct) {
  const q = kiro.quota || {};
  if (!q.supported) return '';
  const items = (q.items || []).filter(it => it && it.label
    && !it.query_failed && it.percent != null || (it.used != null && it.total));
  const mine = items.filter(it => {
    const parts = String(it.label).split(' · ');
    // 形如 [Kiro, #N, 名字, 明细...] 或 [Kiro, 名字, 明细...]
    const name = parts.find((x, i) => i > 0 && !/^#\d+$/.test(x) && x !== 'Kiro') || '';
    return name === (acct.label || acct.id) || parts.includes(acct.label || acct.id);
  });
  if (!mine.length) return '';
  return `<div class="kiro-quota">` + mine.map(it => {
    // 去掉通道/账号前缀，只留包名（进度条自己会显示数字）
    const idx = String(it.label).lastIndexOf(' · ');
    const short = Object.assign({}, it, {label: idx >= 0 ? String(it.label).slice(idx + 3) : it.label});
    return quotaItemHtml(short);
  }).join('') + '</div>';
}

function renderKiroPanel() {
  const panel = document.getElementById('kiro-panel');
  if (!panel) return;
  const kiro = (BENEFITS.providers || []).find(p => p.id === 'kiro' && !p.disabled);
  if (!kiro) { panel.innerHTML = ''; return; }   // 未启用（没加 --kiro）：整块不渲染

  const accts = KIRO_ACCTS || [];
  const rows = accts.map(a => {
    const name = a.label || a.id;
    const moveUp = accts.length > 1
      ? `<button class="ghost" onclick="kiroMoveAccount(${a.index},-1,'${esc(a.id)}')" title="上移">▲</button>` +
        `<button class="ghost" onclick="kiroMoveAccount(${a.index},1,'${esc(a.id)}')" title="下移">▼</button>`
      : '';
    return `<div class="pat-pkg" style="margin-bottom:8px">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span class="pat-pkg-name" style="font-weight:600">${esc(name)}</span>
        ${moveUp}
        ${acctRenameButton('kiro', a)}
        <span style="flex:1"></span>
        <button class="ghost" onclick="kiroConfirmDelete('${esc(a.id)}')" title="删除该账号">✕</button>
      </div>
      ${_kiro_sub_html(a)}
      ${_kiro_quota_html(kiro, a)}
      <div class="muted" style="font-size:11px" title="账号 id（排障用）">${esc(a.id)}</div>
    </div>`;
  }).join('');

  panel.innerHTML = `<div class="chart-card" data-kiro-panel>
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
      <span style="font-weight:600">Kiro（AWS CodeWhisperer）</span>
      <span class="muted" style="font-size:12px">${accts.length} 个账号</span>
      <span style="flex:1"></span>
      ${providerRefreshButton('kiro')}
    </div>
    ${rows || '<div class="empty" style="padding:10px 0">还没有账号</div>'}
    <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
      <button onclick="kiroOpenImport()">导入 auth 文件</button>
      <button class="ghost" onclick="kiroStartLogin()">浏览器授权</button>
    </div>
    <div id="kiro-login-box" style="margin-top:10px"></div>
  </div>`;
  if (!accts.length) loadKiroAccounts();
}

// ---- 账号加载/顺位/删除 ----------------------------------------------------

function loadKiroAccounts() {
  return acctFetch('kiro', () => (async function() {
    try {
      const r = await api('/ui/api/kiro/accounts');
      if (!r.enabled) { KIRO_ACCTS = []; return; }
      const list = r.accounts || [];
      if (JSON.stringify(list) === JSON.stringify(KIRO_ACCTS)) return;  // 没变不动 DOM
      KIRO_ACCTS = list;
      renderKiroPanel();
      syncQuotaFold();
    } catch (e) {
      const panel = document.getElementById('kiro-panel');
      if (panel && !KIRO_ACCTS) {
        panel.innerHTML = `<div class="chart-card"><div class="empty">账号加载失败：${esc(e.message)}</div></div>`;
      }
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
      renderKiroPanel();
      toast('顺位已更新');
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
  confirmAccountDelete({
    title: '删除 Kiro 账号',
    name,
    extra: '该账号的本地凭据一并移除，转发不再使用它。' +
      '如果只是 token 过期，重新在 Kiro IDE 登录后「导入 auth 文件」即可恢复，不必删号。',
  }).then(ok => { if (ok) kiroDeleteAccount(id); });
}

async function kiroDeleteAccount(id) {
  try {
    await api(`/ui/api/kiro/accounts/${encodeURIComponent(id)}`, {method: 'DELETE'});
    KIRO_ACCTS = (KIRO_ACCTS || []).filter(a => a.id !== id);
    renderKiroPanel();
    toast('已删除');
  } catch (e) {
    toast('删除失败: ' + e.message, true);
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
