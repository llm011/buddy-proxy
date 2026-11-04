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

// 「本机具备独立拉起条件」——未运行时也照样能转发（首次请求时代理自起实例）。
// 后端 discovery.can_auto_start() 的统一判据经 /ui/api/dumate/status 下发。
function _dumate_auto() {
  return !!(DUMATE_STATE.status && DUMATE_STATE.status.auto_start);
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
  const auto = _dumate_auto();
  // 圆点三态：绿=进程已起、琥珀=未起但能自动拉起（转发时自起，不是故障）、
  // 红=未装/无从拉起。早先「未运行」一律画琥珀，用户看到「App 未运行」就以为
  // 通道坏了（2026-11-02 报障），其实点下去照样能用。
  const dotColor = ready ? 'var(--ok)' : (auto || (st && st.installed) ? 'var(--warn)' : 'var(--err)');
  const stateText = ready ? '已就绪'
    : (st ? (auto ? '待命 · 首次转发自动拉起' : (st.installed ? 'App 未运行' : '未安装')) : '检测中…');
  const ver = st && st.app_version ? ` · v${st.app_version}` : '';
  const acct = acctFor(DUMATE_STATE, 1);
  const delBtn = acctDeleteButton('dumate', acct, '清除本地累计签到缓存');
  // 单账号没有 ▲▼ 顺位（moveBtns 传空串），↻ + ✕ 两个。
  const rowBtns = acctRowButtons('dumate', '', delBtn);
  // 合计行：取 items[0]「订阅积分」——它本身已是各积分包的合计（head_only）。
  // **不能传 sum_items**：那样会把汇总行和它下面的每个包一起相加，标题翻倍
  // （用户 2026-11-01 报障 33,037 vs 线上 16,286）。传非合计模式，quotaHeadSum
  // 走「取第一条有数的」正好取到汇总行。
  const items = dm.quota.items || [];
  const headSum = quotaHeadSum({items});
  // 未就绪说明（未安装/检测中）：替换积分明细位置，整块 muted 提示。
  // 优先用后端 status 的 hint（单一事实源）——它区分「装了但没跑（会自动拉起，
  // 无需手动开）」与「没装」，前端硬编码会把自动拉起说成「请先打开桌面端」。
  const notReadyHint = st && st.installed === false
    ? '未检测到 DuMate.app。请先安装并登录百度搭子（千帆桌面端）。'
    : st && st.installed ? (st.hint || '已安装 DuMate.app，但当前未在运行。')
    : '正在检测本地代理…';
  // 额度明细该不该铺：**不再只看进程在不在**。额度走 bceConsole cookie（公网
  // HTTPS），与本地 dumate-main-server 毫无关系；进程没起时后端也会顺手拉起独立
  // 实例再兜底。早先这里按 ready 一刀切，桌面端一关整块额度就变成一句
  // 「App 未运行」（用户 2026-11-02 报障）。只有「未装 / 拉起也免谈」时才退回提示。
  const showQuota = ready || auto || (st && st.installed !== false && items.length > 0);
  // 待命态（进程没起、也拉不起来）只剩下方那句提示，收起时别把它藏掉：
  // 提示本身是有用的引导。有明细才按「耗尽/没有」判折叠。
  const fold = quotaProviderFold(items.length ? items : (showQuota ? 'empty' : null), 'dumate');
  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">百度搭子 (DuMate)</span>
        <span class="tag">本地代理 · 复用 App 登录态</span>${fold.badge}
        <span class="grow"></span>
        <span class="muted" style="font-size:11px;display:flex;align-items:center;gap:5px">
          <i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${dotColor}"></i>
          ${esc(stateText)}${esc(ver)}${st && st.bceconsole_authenticated ? ' · 已登录' : ''}
        </span>
        ${fold.toggle}${providerRefreshButton('dumate')}
      </div>
      ${fold.bodyStart}<div class="pat-quota-grid">
        <div class="pat-pkg">
          <span class="pat-pkg-name">${esc(acct ? (acct.display_name || acct.id) : '本机账号')}</span>
          ${acctSubHtml(acct, _dumate_sub_extra, 'data-dumate-sub')}
          ${showQuota
            ? `${headSum ? `<div style="display:flex;align-items:center;margin:0 0 6px">${headSum}</div>` : ''}` +
              quotaItemsHtml(items, 'dumate')
            : `<div class="muted" style="font-size:11px;line-height:1.5;padding:8px 0">${esc(notReadyHint)}</div>`}
          ${rowBtns}
        </div>
      </div>${fold.bodyEnd}
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
    //
    // 判据用「状态变了」而不是「变成 ready」：auto_start / installed /
    // bceconsole_authenticated 同样决定圆点与额度区怎么画，只盯 ready 的话
    // 非就绪态的变化（刚检出「可自动拉起」、刚解密出登录态）永远刷不出来。
    // 重渲尾部又调本函数 → 同一份响应回来后比对相等、自然不再重渲（不会自套）。
    if (r && JSON.stringify(r) !== JSON.stringify(prev)) {
      renderDumatePanel();
      // 重渲换了整块 DOM（.qbody 高度随明细条数变），校准必须跟着走一次：
      // renderBenefits 末尾那次 syncQuotaFold 跑在本函数 await 之前，看不到
      // 重渲后的高度。不补这一步，这条路径出来的 .qbody 保持模板里「未裁切 +
      // 按钮 hidden」的原样——Dumate 明细 31 条全铺开、也没有「收起」按钮
      // （用户 2026-11-01 报障）。别的面板的异步重渲都包在 acctLoad 里、尾部带
      // 校准，只有这条 status 驱动的自重建漏了。（antigravity/kimi 同款）
      syncQuotaFold();
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
