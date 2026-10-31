// ---- DUMATE 面板（底部整宽卡片，复用 benefits_accounts.js 的公共 helper）----
// DuMate（百度搭子）复用本机 App 登录态，经本地代理转发。卡片布局与 antigravity
// 同款：外层 chart-card 头部（通道名 + tag + 就绪态圆点/版本/已登录），内层
// .pat-quota-grid > .pat-pkg 一块装账号（标题=账号名 .pat-pkg-name、副标题=
// 今日消耗/累计签到、右上 .ag-move 按钮组=↻+✕）。额度明细走 quotaItemsHtml：
// 首条「订阅积分」即总结行（「剩 X / Y · 已用 Z%」+ 进度条，quotaItemHtml
// 渲染），后面跟各积分包。单账号无 failover 顺位——▲▼ 不渲染，delete 是
// no-op（仅前端卡片刷新）。账号名 + 副标题走 /ui/api/dumate/accounts 经通用
// acctLoad 回填（就地回填按「.pat-pkg-name 紧后兄弟 data-dumate-sub」定位，
// 渲染侧用 acctSubHtml 同款输出，保证两条路径结构一致）。
const DUMATE_STATE = { accts: null };

function _dumate_ready() {
  return !!(DUMATE_STATE.status && DUMATE_STATE.status.ready);
}

// 账号副标题 bits（渲染与 acctLoad 就地回填共用同一份逻辑）：
// 今日消耗走 bceConsole records/usage 的真实流水合计（consumedPoints，正数），
// 不是估算；拿不到（未登录/超时）就不显示这一位。
function _dumate_sub_extra(a, bits) {
  if (a.today_consumed != null) {
    const n = Number(a.today_consumed);
    if (Number.isFinite(n) && n > 0) {
      bits.push(`今日消耗 ${fmtNum(n)} 分${a.today_calls ? `（${a.today_calls} 次）` : ''}`);
    }
  }
  if (a.checkin_total_points != null) {
    bits.push(`累计签到 ${a.checkin_total_points} 分（${a.checkin_total_times || 0} 次）`);
  }
  // 今日签到状态：后端只有真查到上游才下发 checkin_status；缺省＝查询失败
  // （未登录/网络），此时不显示这一位，免得把「不知道」画成「没签」。
  const ck = a.checkin_status;
  if (ck) {
    bits.push(ck.checked_in ? '今日已签到' : '今日未签到');
    if (ck.streak_days >= 2) bits.push(`连续 ${ck.streak_days} 天`);
  }
}

function renderDumatePanel() {
  const panel = document.getElementById('dumate-panel');
  if (!panel) return;
  const dm = (BENEFITS.providers || []).find(p => p.id === 'dumate' && !p.disabled && p.quota && p.quota.supported);
  if (!dm) { panel.innerHTML = ''; return; }
  loadDumateStatus();  // 每次渲染顺手拉本地状态（秒回），就绪态圆点/版本总能反映当前

  const st = DUMATE_STATE.status;
  const ready = _dumate_ready();
  const dotColor = ready ? 'var(--ok)' : (st && st.installed ? 'var(--warn)' : 'var(--err)');
  const stateText = ready ? '已就绪' : (st ? (st.installed ? 'App 未运行' : '未安装') : '检测中…');
  const ver = st && st.app_version ? ` · v${st.app_version}` : '';
  const acct = acctFor(DUMATE_STATE, 1);
  const delBtn = acctDeleteButton('dumate', acct, '清除本地累计签到缓存');
  // 单账号没有 ▲▼ 顺位（moveBtns 传空串），↻ + ✕ 两个。
  const rowBtns = acctRowButtons('dumate', '', delBtn);
  // 未就绪说明（未安装/未运行/检测中）：替换积分明细位置，整块 muted 提示。
  const notReadyHint = st && st.installed === false
    ? '未检测到 DuMate.app。请先安装并登录百度搭子（千帆桌面端）。'
    : st && st.installed ? '已安装 DuMate.app，但当前未在运行——请先打开百度搭子桌面端。'
    : '正在检测本地代理…';

  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">百度搭子 (DuMate)</span>
        <span class="tag">本地代理 · 复用 App 登录态</span>
        <span class="grow"></span>
        <span class="muted" style="font-size:11px;display:flex;align-items:center;gap:5px">
          <i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${dotColor}"></i>
          ${esc(stateText)}${esc(ver)}${ready && st && st.bceconsole_authenticated ? ' · 已登录' : ''}
        </span>
        ${providerRefreshButton('dumate')}
      </div>
      <div class="pat-quota-grid">
        <div class="pat-pkg">
          <span class="pat-pkg-name">${esc(acct ? (acct.display_name || acct.id) : '本机账号')}</span>
          ${acctSubHtml(acct, _dumate_sub_extra, 'data-dumate-sub')}
          ${ready
            ? quotaItemsHtml(dm.quota.items || [], 'dumate')
            : `<div class="muted" style="font-size:11px;line-height:1.5;padding:8px 0">${esc(notReadyHint)}</div>`}
          ${rowBtns}
        </div>
      </div>
    </div>`;
  loadDumateAccounts().catch(() => {});  // 状态获取失败留给下一轮轮询
}

async function loadDumateStatus() {
  try {
    const r = await api('/ui/api/dumate/status');
    const prev = DUMATE_STATE.status;
    DUMATE_STATE.status = r;
    // 就绪态是异步拉回来的：状态**变化**时刷新一次面板让圆点/版本/已登录反映
    // 新状态（否则首渲的「检测中…」要等下个 30s 轮询才消失）。只在变化时重渲
    // ——renderDumatePanel 尾部会调本函数，无条件重渲会自己套自己死循环。
    if (r && r.ready && JSON.stringify(r) !== JSON.stringify(prev)) {
      renderDumatePanel();
    }
  } catch (e) {
    DUMATE_STATE.status = null;  // 静默：下轮 30s 自动重试
  }
}

// dumate 的就地回填（acctLoad 的第二拍与静默重绘路径的 cfg.fill）：账号名 +
// 副标题按「.pat-pkg-name 紧后兄弟 data-dumate-sub」定位——renderDumatePanel
// 用同一套输出，两条路径结构一致。
function fillDumateAccounts() {
  const list = DUMATE_STATE.accts || [];
  for (const a of list) {
    const el = (list.length === 1)
      ? document.querySelector('#dumate-panel .pat-pkg-name')
      : document.querySelector(`#dumate-panel [data-dumate-idx="${a.index}"]`);
    if (!el) continue;
    el.textContent = a.display_name || a.id;
    if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-dumate-sub')) {
      el.nextElementSibling.remove();
    }
    const sub = acctSubHtml(a, _dumate_sub_extra, 'data-dumate-sub');
    if (sub) el.insertAdjacentHTML('afterend', sub);
  }
}

function loadDumateAccounts() {
  return acctLoad(
    {
      pid: 'dumate',
      nameOf: a => a.display_name || a.id,
      subExtra: _dumate_sub_extra,
      render: renderDumatePanel,
      fill: fillDumateAccounts,
    },
    DUMATE_STATE,
  );
}

// 登记表放到 Dumate 惰性加载器声明之后：加载器必须是可重复调用的函数，
// 不能在模块初始化时把 acctLoad 的 Promise 存成加载器。
const ACCT_LOADERS = {
  antigravity: () => loadAntigravityAccounts(),
  qoder: () => loadQoderAccounts(),
  qoderintl: () => loadQoderIntlAccounts(),
  trae: () => loadTraeAccounts(),
  codebuddy: () => loadCodebuddyAccounts(),
  codebuddyintl: () => loadCodebuddyIntlAccounts(),
  kimi: () => loadKimiAccounts(),
  codex: () => loadCodexAccounts(),
  dumate: () => loadDumateAccounts(),
};
