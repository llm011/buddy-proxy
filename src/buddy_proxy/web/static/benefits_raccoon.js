// 商汤小浣熊（SenseTime Raccoon）额度/账号面板。
//
// 与其它多账号通道的区别（也是本面板存在的理由）：
//
// 1. **额度是积分不是 token**：每笔请求按模型倍率扣分（倍率 0 = 限免），
//    与 token 量无关。所以这里显示「每日积分（7 天有效）」与「可用积分」，
//    不显示 token 余额。
// 2. **登录方式不止「粘 token」**：小浣熊没有 OAuth 设备码流程，网页端只有
//    手机号+密码。所以导入弹窗给三条路——手机号+密码（可自动续期）、粘
//    xhx_token、复用本机桌面客户端登录态。只做粘 token 的话，多账号每月
//    都要人工换一遍。
// 3. **auto_renew 要显式标出来**：存了密码的账号 token 过期会自动重登，
//    没存的到期要人工重导。这个差别必须让用户一眼看到。

let RACCOON_ACCTS = null;   // 最近一次 accounts 快照
let RACCOON_IMPORTING = false;

/** 单个账号在快照里的位置（按 index 对齐「小浣熊 #N」分组名）。
 *
 *  单账号时后端 label 不带「#N」前缀，组名就是「小浣熊」——此时只有恰好
 *  一个账号才认它（否则会把唯一那个号的名字安到别的组上）。
 */
function _raccoon_acct_for(idx) {
  if (!RACCOON_ACCTS || !RACCOON_ACCTS.length) return null;
  if (idx == null) return RACCOON_ACCTS.length === 1 ? RACCOON_ACCTS[0] : null;
  return RACCOON_ACCTS.find(a => a.index === idx) || null;
}

function _raccoon_unavailable(a) {
  return !!(a && (a.available === false || (a.cooling || []).length));
}

function _raccoon_acct_name(a, fallback) {
  const name = a ? (a.alias || a.name || a.id) : fallback;
  return `${_raccoon_unavailable(a) ? '❌ ' : ''}${name || fallback || ''}`;
}

function _raccoon_cooling_text(c) {
  const labels = {
    risk_control: '风控冷却',
    quota: '额度耗尽',
    rate: '频率窗口',
    account: '账号冷却',
  };
  const seconds = c.seconds_left != null
    ? Number(c.seconds_left) : Number(c.minutes_left) * 60;
  let left;
  if (!Number.isFinite(seconds) || seconds <= 0) left = '剩余时间待刷新';
  else if (seconds >= 3600) left = `剩余 ${(seconds / 3600).toFixed(1)} 小时`;
  else if (seconds >= 60) left = `剩余 ${(seconds / 60).toFixed(1)} 分钟`;
  else left = `剩余 ${Math.ceil(seconds)} 秒`;
  return `${labels[c.kind] || (c.kind ? `${c.kind} 冷却` : '冷却')} · ${left}`;
}

/** 账号副标题：手机号 / 自动续期 / 当前可用性。 */
function _raccoon_sub_html(a) {
  if (!a) return '';
  const bits = [];
  if (a.phone) bits.push(`<span class="mono">${esc(a.phone)}</span>`);
  if (a.auto_renew) {
    bits.push('<span style="color:var(--ok)">自动续期</span>');
  } else {
    bits.push('<span style="color:var(--warn)">需手动重导（无密码）</span>');
  }
  const cooling = (a.cooling || []).map(_raccoon_cooling_text);
  if (cooling.length) {
    bits.push(`<span style="color:var(--err);font-weight:600">${esc(cooling.join(' · '))}</span>`);
  } else if (a.available === false) {
    bits.push('<span style="color:var(--err);font-weight:600">暂不可用</span>');
  } else {
    bits.push('<span style="color:var(--ok);font-weight:600">✓ 可用</span>');
  }
  return `<div class="muted" style="font-size:11px;margin:1px 0 6px" data-raccoon-sub>` +
    bits.join(' · ') + `</div>`;
}

function _raccoon_availability_html() {
  const accts = RACCOON_ACCTS || [];
  if (!accts.length) return '';
  const blocked = accts.filter(_raccoon_unavailable).length;
  const cls = !blocked ? '' : blocked === accts.length ? ' bad' : ' warn';
  return `<span class="tag${cls}">可用 ${accts.length - blocked}/${accts.length}</span>`;
}

function _raccoon_move_btns(idx, n, id) {
  return acctMoveButtons('raccoon', idx, n, id);
}

// ---- 账号操作 ---------------------------------------------------------------

async function loadRaccoonAccounts() {
  try {
    // 同一通道在飞时复用（acctFetch）：30s 轮询 + 整页重绘叠加不再并排多发
    const r = await acctFetch('raccoon', () => api('/ui/api/raccoon/accounts'));
    if (!r.enabled) { RACCOON_ACCTS = []; return; }
    const accts = r.accounts || [];
    RACCOON_DESKTOP_OK = !!r.desktop_available;
    if (JSON.stringify(accts) === JSON.stringify(RACCOON_ACCTS)) return;
    const snapshotMissing = !RACCOON_ACCTS || !RACCOON_ACCTS.length;
    RACCOON_ACCTS = accts;
    if (!accts.length) return;
    if (snapshotMissing && !ACCT_NO_RERENDER && typeof renderRaccoonPanel === 'function') {
      // 首份快照到位：整卡重渲（账号名直出、按钮带上 id），再拉取走早退
      renderRaccoonPanel();
      syncQuotaFold();
      return;
    }
    fillRaccoonAccounts(accts);
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
  }
}

/** 账号快照就地回填（首份快照后的回填路径）。 */
function fillRaccoonAccounts(accts) {
  if (!ACCT_NO_RERENDER && typeof renderRaccoonPanel === 'function') {
    renderRaccoonPanel();
    return;
  }
  for (const a of accts) {
    const el = (accts.length === 1)
      ? document.querySelector('#raccoon-panel .pat-pkg-name')
      : document.querySelector(`#raccoon-panel [data-raccoon-idx="${a.index}"]`);
    if (!el) continue;
    el.textContent = _raccoon_acct_name(a, a.id);
    if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-raccoon-sub')) {
      el.nextElementSibling.remove();
    }
    const sub = _raccoon_sub_html(a);
    if (sub) el.insertAdjacentHTML('afterend', sub);
  }
}

let RACCOON_DESKTOP_OK = false;

async function raccoonMoveAccount(idx, delta, id, pin) {
  if (!RACCOON_ACCTS) await loadRaccoonAccounts();
  const list = (RACCOON_ACCTS || []).map(a => a.id);
  const cur = list.indexOf(id);
  const next = pin ? 0 : cur + delta;
  if (cur < 0 || next < 0 || next >= list.length) return;
  list.splice(cur, 1);
  list.splice(next, 0, id);
  try {
    const r = await api('/ui/api/raccoon/accounts/order', {
      method: 'POST', body: JSON.stringify({ ids: list }),
    });
    RACCOON_ACCTS = r.accounts || [];
    renderRaccoonPanel();
    toast('顺位已调整');
  } catch (e) {
    toast('调整失败：' + e.message, 'err');
  }
}

async function raccoonDeleteAccount(id) {
  const a = (RACCOON_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.alias || a.name || a.id)) || id;
  if (!confirm(`删除账号「${name}」？\n\n这会清掉本地凭据（含已存的手机号密码）。`)) return;
  try {
    const r = await api('/ui/api/raccoon/accounts/delete', {
      method: 'POST', body: JSON.stringify({ id }),
    });
    RACCOON_ACCTS = r.accounts || [];
    renderRaccoonPanel();
    toast('账号已删除');
  } catch (e) {
    toast('删除失败：' + e.message, 'err');
  }
}

/** 手动强制重新登录（用落盘的手机号+密码）。 */
async function raccoonRelogin(id) {
  toast('正在重新登录…');
  try {
    const r = await api('/ui/api/raccoon/accounts/relogin', {
      method: 'POST', body: JSON.stringify({ id }),
    });
    RACCOON_ACCTS = r.accounts || [];
    renderRaccoonPanel();
    toast('会话已刷新');
  } catch (e) {
    toast('重新登录失败：' + e.message, 'err');
  }
}

// ---- 导入弹窗 ---------------------------------------------------------------

/** 导入账号弹窗：三条路（手机号密码 / 粘 token / 桌面端复用）。 */
function openRaccoonImport() {
  const desktopBtn = RACCOON_DESKTOP_OK
    ? `<div style="margin:10px 0;padding:10px;border:1px solid var(--line);border-radius:6px">
         <div style="font-weight:600;margin-bottom:6px">方式三：复用本机桌面客户端登录态</div>
         <div class="muted" style="font-size:11px;margin-bottom:8px">
           只读客户端保存的会话，不写入、不动它的 refresh token。</div>
         <button class="ghost" onclick="raccoonImportDesktop()">从桌面端导入</button>
       </div>` : '';
  openModal('导入小浣熊账号', `
    <div style="padding:4px 0">
      <div style="margin-bottom:12px;padding:10px;border:1px solid var(--line);border-radius:6px">
        <div style="font-weight:600;margin-bottom:6px">方式一：手机号 + 密码（推荐）</div>
        <div class="muted" style="font-size:11px;margin-bottom:8px">
          密码加密保存在本机（0600），token 过期会自动重新登录，不用一个月换一次。
          多账号就重复导入几次，每个号各自续期。</div>
        <div style="display:flex;gap:6px;margin-bottom:6px">
          <input id="raccoon-phone" placeholder="手机号" style="flex:1" />
          <input id="raccoon-pass" type="password" placeholder="密码（至少 8 位）" style="flex:1" />
        </div>
        <button class="ghost" onclick="raccoonImportPassword()">登录并保存</button>
      </div>

      <div style="margin-bottom:12px;padding:10px;border:1px solid var(--line);border-radius:6px">
        <div style="font-weight:600;margin-bottom:6px">方式二：粘贴会话 token</div>
        <div class="muted" style="font-size:11px;margin-bottom:8px">
          浏览器打开 xiaohuanxiong.com 登录 → DevTools → Application → Cookies →
          复制 <span class="mono">xhx_token</span> 的值。约 30 天到期，
          没有密码时到期需重新粘贴。</div>
        <textarea id="raccoon-session" rows="4" placeholder="xhx_token 的值（也接受整段 cookie 或 {&quot;access_token&quot;: &quot;...&quot;}）"
          style="width:100%;box-sizing:border-box;font-family:monospace;font-size:11px"></textarea>
        <button class="ghost" style="margin-top:6px" onclick="raccoonImportSession()">导入</button>
      </div>
      ${desktopBtn}
    </div>`);
}

function _raccoonImportDone(r) {
  RACCOON_ACCTS = r.accounts || [];
  const who = (r.imported && (r.imported.name || r.imported.id)) || '账号';
  closeModal();
  renderRaccoonPanel();
  toast(`已导入：${who}`);
}

async function raccoonImportPassword() {
  const phone = (document.getElementById('raccoon-phone').value || '').trim();
  const password = document.getElementById('raccoon-pass').value || '';
  if (!phone || !password) { toast('请填手机号与密码', 'err'); return; }
  if (password.length < 8) { toast('密码至少 8 位（上游要求）', 'err'); return; }
  try {
    const r = await api('/ui/api/raccoon/accounts/import', {
      method: 'POST', body: JSON.stringify({ phone, password }),
    });
    _raccoonImportDone(r);
  } catch (e) {
    toast('登录失败：' + e.message, 'err');
  }
}

async function raccoonImportSession() {
  const session = (document.getElementById('raccoon-session').value || '').trim();
  if (!session) { toast('请粘贴 token', 'err'); return; }
  try {
    const r = await api('/ui/api/raccoon/accounts/import', {
      method: 'POST', body: JSON.stringify({ session }),
    });
    _raccoonImportDone(r);
  } catch (e) {
    toast('导入失败：' + e.message, 'err');
  }
}

async function raccoonImportDesktop() {
  try {
    const r = await api('/ui/api/raccoon/accounts/import', {
      method: 'POST', body: JSON.stringify({ from_desktop: true }),
    });
    _raccoonImportDone(r);
  } catch (e) {
    toast('导入失败：' + e.message, 'err');
  }
}

// ---- 面板渲染 ---------------------------------------------------------------

function renderRaccoonPanel() {
  const panel = document.getElementById('raccoon-panel');
  if (!panel) return;
  const provider = (BENEFITS.providers || []).find(p => p.id === 'raccoon' && !p.disabled);
  if (!provider) { panel.innerHTML = ''; return; }

  // 额度条目按「小浣熊 #N」分组（多账号带前缀，单账号不带 → 归到「小浣熊」）。
  //
  // 判据只看 ``query_failed``，**不能**照抄 kimi/antigravity 的
  // ``percent == null && used == null``：本通道刻意不编上限（上游只给剩余量），
  // 所以正常条目的 percent/used 天然都是 null。照抄会把所有资源包误判成
  // 「静态说明条」，于是账号卡里一条明细都没有、全部横贯全宽地摊在外面
  // ——2026-11-23 用户实报「还是不对呀，你要复用我多账号的逻辑」。
  const groups = new Map();
  const notices = [];
  // 账号快照先铺空组：额度查询失败/正在风控的账号也必须留下卡片，否则它们
  // 恰好没有 quota item 时会从面板消失，用户也就看不到名字前的 ❌ 与剩余时长。
  const acctCount = (RACCOON_ACCTS || []).length;
  for (const a of RACCOON_ACCTS || []) {
    const grp = acctCount > 1 ? `小浣熊 #${a.index}` : '小浣熊';
    if (!groups.has(grp)) groups.set(grp, []);
  }
  for (const it of (provider.quota && provider.quota.supported ? provider.quota.items : []) || []) {
    if (it.query_failed) { notices.push(it); continue; }
    const cut = it.label.indexOf(' · ');
    const grp = cut >= 0 ? it.label.slice(0, cut) : '小浣熊';
    const sub = cut >= 0 ? it.label.slice(cut + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, { label: sub }));
  }

  const n = Math.max(...[...groups.keys()]
    .map(g => g.match(/^小浣熊 #(\d+)$/)).filter(Boolean).map(mm => Number(mm[1])), 1);

  const quotaHtml = [...groups.entries()].map(([grp, its]) => {
    const m = grp.match(/^小浣熊 #(\d+)$/);
    const idx = m ? Number(m[1]) : null;
    const acct = _raccoon_acct_for(idx);
    const moveBtns = (m && n > 1) ? _raccoon_move_btns(idx, n, acct && acct.id) : '';
    const delBtn = acct
      ? `<button class="ghost danger" title="删除该账号（清掉本地凭据）" ` +
        `onclick="raccoonDeleteAccount('${acct.id}')">✕</button>` : '';
    const renameBtn = acctRenameButton('raccoon', acct);
    // 手动续期按钮：只在「有密码」时给——没密码时点了也只会失败
    const renewBtn = (acct && acct.auto_renew)
      ? `<button class="ghost" title="用已保存的手机号密码重新登录，刷新会话" ` +
        `onclick="raccoonRelogin('${acct.id}')">↻登录</button>` : '';
    // 官方消耗明细：/points/v2/bills?type=expense，流水不带模型名，看事件/客户端
    const logsBtn = acct
      ? `<button class="ghost" title="查看该账号的官方积分消耗流水（最近 30 天）" ` +
        `onclick="openUsageLogs('raccoon','${acct.id}')">📜 明细</button>` : '';
    const rowBtns = `<span class="ag-move">${renameBtn}${renewBtn}${moveBtns}` +
      `${acctDisableButton('raccoon', acct)}${logsBtn}${delBtn}</span>`;
    const headSum = quotaHeadSum({ items: its });
    return `
    <div class="pat-pkg"${acct ? ` data-raccoon-available="${!_raccoon_unavailable(acct)}"` : ''}>
      <span class="pat-pkg-name"${m ? ` data-raccoon-idx="${idx}"` : ''}>${esc(_raccoon_acct_name(acct, grp))}</span>
      ${_raccoon_sub_html(acct)}
      ${headSum ? `<div style="display:flex;align-items:center;margin:0 0 6px">${headSum}</div>` : ''}
      ${quotaItemsHtml(its, 'raccoon:' + grp)}
      ${rowBtns}
    </div>`;
  }).join('');

  const noticeHtml = notices.map(quotaItemHtml).join('');
  const multi = n > 1;
  // 有账号卡时强制默认展开：全部额度查询失败会被公共折叠器判成「无剩余额度」，
  // 但此时用户最需要看到的恰恰是哪些号被风控、还要等多久。
  const fold = quotaProviderFold(groups.size ? null
    : (notices.length ? provider.quota.items || [] : 'empty'), 'raccoon');

  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">商汤小浣熊</span>
        <span class="tag">${multi ? '多账号 · 自动切换' : 'SenseTime Raccoon'}</span>${_raccoon_availability_html()}${fold.badge}
        <span class="pat-head-mid">${quotaAccountTotal(groups)}
          <span class="muted" style="font-size:11px">积分制：按模型倍率扣分，倍率 0 = 限免</span>
        </span>
        <span class="pat-head-actions">${fold.toggle}${providerLoadBalanceToggle('raccoon', multi)}${providerRefreshButton('raccoon')}
        <button class="ghost" title="导入账号：手机号+密码（可自动续期）/ 粘 token / 桌面端复用"
          onclick="openRaccoonImport()">导入账号</button></span>
      </div>
      ${fold.bodyStart}${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}${groups.size ? `<div class="pat-quota-grid">${quotaHtml}</div>`
        : '<div class="empty" style="padding:12px 0">暂无账号额度数据</div>'}${fold.bodyEnd}
    </div>`;
  loadRaccoonAccounts();
}
