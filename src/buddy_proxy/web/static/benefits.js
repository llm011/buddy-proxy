// ---- 打卡「下次时间」----
// 后端给 next_ts（epoch 秒）+ next_ts_source（upstream=上游时间窗 /
// inferred=上游没给、按实测零点轮换推断）。推断的要标出来：那是我们从打卡
// 记录反推的，不是上游契约，说成准点会误导。
function fmtNextClock(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const hhmm = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return `今天 ${hhmm}`;
  const tmr = new Date(now); tmr.setDate(now.getDate() + 1);
  if (d.toDateString() === tmr.toDateString()) return `明天 ${hhmm}`;
  return `${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} ${hhmm}`;
}
function nextTimeHtml(c) {
  if (!c || c.next_ts == null) return '';
  const inferred = c.next_ts_source === 'inferred';
  // 未签到时 next_ts 是本轮**截止**（qoder 的窗口此刻过期即失效），措辞要区分：
  // 「可打卡中，X 后截止」比「下次 X」更贴合用户当下该做的事。
  const verb = (c.claimable && !c.done_today) ? '截止' : '下次';
  const tip = inferred
    ? '上游未提供每日轮换时刻，按打卡记录推断为本地零点'
    : '时刻来自上游返回的时间窗';
  // 推断值用虚线框 + 斜体表示「不确定」，替掉原先贴在「分后」后面的「≈」：
  // 那个位置读着像错字，而且它只区分了来源，没说清「哪里不确定」。
  // 倒计时尾巴（「21 小时 33 分后」）按用户反馈撤了——第一行太挤，绝对时刻
  // 已够用；连带的 fmtCountdown / .cd 样式 / cd 更新分支一并清掉。
  return `<span class="br-next${inferred ? ' inferred' : ''}" title="${esc(tip)}" data-next-ts="${c.next_ts}">` +
    `<span class="lbl">${verb}</span>` +
    `<b class="at" data-next-clock>${esc(fmtNextClock(c.next_ts))}</b></span>`;
}
// 时钟每秒对一次表（纯本地计算，不打上游）；只在打卡页可见且真有元素时跑。
let NEXT_TIME_TIMER = null;
function nextTimeWanted() {
  const page = document.getElementById('page-benefits');
  return !!page && page.classList.contains('active')
    && document.visibilityState === 'visible'
    && !!document.querySelector('[data-next-ts]');
}
function renderNextTimes() {
  document.querySelectorAll('[data-next-ts]').forEach(el => {
    const clock = el.querySelector('[data-next-clock]');
    // 过点后本地时钟文案也要跟上（数据要等下一轮刷新才换）
    if (clock) clock.textContent = fmtNextClock(Number(el.dataset.nextTs));
  });
}
function syncNextTimeAuto() {
  if (nextTimeWanted()) {
    renderNextTimes();                       // 立刻算一次，别等第一秒
    if (!NEXT_TIME_TIMER) NEXT_TIME_TIMER = setInterval(renderNextTimes, 1000);
  } else if (NEXT_TIME_TIMER) {
    clearInterval(NEXT_TIME_TIMER);
    NEXT_TIME_TIMER = null;
  }
}
document.addEventListener('visibilitychange', syncNextTimeAuto);

// ---- 权益到期告警横幅 ----
// 后端算好 expiring（谁快到期、还剩几天），这里只渲染不判规则——阈值与
// 「积分类才做量过滤」的逻辑都在 benefits._expiring 一处，免得两边各写一份
// 慢慢走偏（比如前端漏掉 unit 判断，就会给 mimo 的「还剩 3 天」也套 300 门槛）。
//
// 汇总文案要写「将在 N 天内到期」，这个 N 由后端随 expiring 一起下发
// （``expiry_warn_days``），前端不自己存一份——两处各写一个常量，改了一处
// 就会出现「文案写着 7 天、实际窗口 3 天」，比不写更让人困惑。
//
// 形态用 <details>：默认收起，汇总行说明有几项、最早是哪天；展开才列明细
// （通道 / 名称 / 到期日 / 剩余天数）。已过期项用红色单独标——那是最该看的。
function fmtExpireDate(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const mmdd = `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  // 不在今年的补上年份，免得「01-05」看不出是明年
  return d.getFullYear() === now.getFullYear() ? mmdd : `${d.getFullYear()}-${mmdd}`;
}
function fmtDaysLeft(days) {
  if (days < 0) return `已过期 ${Math.abs(Math.round(days))} 天`;
  if (days < 1) return '今天到期';
  return `剩 ${Math.round(days)} 天`;
}
function renderExpiryBanner() {
  const el = document.getElementById('expirybar');
  if (!el) return;
  const list = (BENEFITS && BENEFITS.expiring) || [];
  const low = (BENEFITS && BENEFITS.low_quota) || [];
  if (!list.length && !low.length) {
    el.className = 'expirybar';
    el.innerHTML = '';
    el.open = false;
    return;
  }
  const hasOver = list.some(e => e.days_left < 0);
  el.className = 'expirybar show' + (hasOver ? ' expired' : '');
  // 窗口天数以后端下发为准；老响应缺字段时退回 7（与后端默认一致）
  const warnDays = Number(BENEFITS && BENEFITS.expiry_warn_days) || 7;
  // 两种告警各说各的段，都命中就用「 · 」接上
  const expHead = list.length
    ? (hasOver ? `有 ${list.length} 项权益已过期或即将到期`
               : `有 ${list.length} 项权益将在 ${warnDays} 天内到期`)
    : '';
  const lowHead = low.length ? `${low.length} 个通道余额告急` : '';
  const head = [expHead, lowHead].filter(Boolean).join(' · ');
  const expRows = list.map(e => {
    const over = e.days_left < 0;
    // 余量只对积分类有意义（天数/次数/千分制的 remaining 是各自的量纲，
    // 铺出来只会让人困惑），故只给 credit 带余量
    const amt = e.unit === 'credit' && e.remaining != null
      ? `<span class="eb-hint">· 剩 ${fmtNum(e.remaining)}</span>` : '';
    return `<span class="eb-prov">${esc(e.provider_name || e.provider)}</span>` +
      `<span class="eb-label">${esc(e.label)}${amt}</span>` +
      `<span class="eb-date">${esc(fmtExpireDate(e.expire_ts))}</span>` +
      `<span class="eb-days${over ? ' over' : ''}">${esc(fmtDaysLeft(e.days_left))}</span>`;
  });
  // 余额告急没有日期可填（eb-date 用 — 占位）；判定与面板标题行同口径，
  // 后端算好剩余与占比，这里照搬，不自己重算。credit 通道铺绝对值
  // （「剩 263 credits」——绝对值才是它们的告警依据），其余量纲铺占比。
  const lowRows = low.map(p => {
    const isCredit = p.unit === 'credit';
    const amt = isCredit
      ? `剩 ${fmtNum(p.remaining)} credits`
      : (p.percent_left != null ? `剩 ${fmtNum(p.percent_left)}%` : '余额不足');
    // label 一律拼上（后端已剥掉通道名前缀，形如「#1 · 总额度」）：多账号下
    // 只有「Trae · 余额告急」看不出告的是哪个号（用户 2026-10-06 反馈）
    const lbl = p.label ? ` · ${esc(p.label)}` : '';
    return `<span class="eb-prov">${esc(p.provider_name || p.provider)}</span>` +
      `<span class="eb-label">余额告急${lbl} · ${amt}</span>` +
      `<span class="eb-date">—</span>` +
      `<span class="eb-days over">${p.percent_left != null ? esc(fmtNum(p.percent_left)) + '%' : '告急'}</span>`;
  });
  const soonest = list[0];  // 后端按 expire_ts 升序，第一条就是最紧的
  const soonHint = soonest
    ? `<span class="eb-hint"> · 最近一项 ${esc(fmtExpireDate(soonest.expire_ts))}（${esc(fmtDaysLeft(soonest.days_left))}）</span>`
    : '';
  el.innerHTML =
    `<summary><span class="eb-count"><b>⏳ ${esc(head)}</b>${soonHint}</span>` +
    `<span class="eb-hint">展开明细 ▾</span></summary>` +
    `<div class="eb-list">${expRows.concat(lowRows).join('')}</div>`;
}

// ---- 打卡日历 + 自动打卡 + 各通道额度 ----
// opts.quiet：静默重绘（签到强刷用 POST 返回的快照重画时走这条）。只换签到区
// ——签到行 / 日历 / 到期横幅 / 下方通用额度卡 / 角标——不重建各通道的
// 「多账号」面板和侧边导航：整块重写会让正在点的按钮失效，而且每个面板重渲
// 尾部都会再拉一发 accounts，一次强刷就刮出一串请求（2026-11-01 用户实报）。
// 静默期间账号加载器只就地回填（acctNoRerender），已渲染的面板补一次加载器，
// 让快照里新出现的账号也能把名字/按钮补上——没变就走加载器的早退，不发请求。
// 非静默路径下 fill<X>Accounts 会整卡重渲（与渲染同一份快照，按钮组不会叠第二份）；
// 静默才只回填。
function renderBenefits(opts) {
  if (!BENEFITS) return;
  const quiet = !!(opts && opts.quiet);
  renderExpiryBanner();
  document.getElementById('auto-checkin').checked = !!BENEFITS.auto_checkin;
  document.getElementById('checkin-time').value = BENEFITS.checkin_time || '09:30';
  const enabled = BENEFITS.checkin_enabled_providers || [];
  document.getElementById('checkin-hint').textContent =
    enabled.length ? '每天自动为: ' + enabled.join(', ') : '暂无支持打卡的通道';

  const cal = document.getElementById('cal');
  const days = BENEFITS.calendar || [];
  window._calTips = {};
  const DOW = ['一', '二', '三', '四', '五', '六', '日'];
  const DOW_FULL = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
  const now = new Date();
  const pad2 = n => String(n).padStart(2, '0');
  const todayStr = `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`;
  // 起点补齐到周一，让列与星期对齐
  const first = new Date(now); first.setDate(now.getDate() - (days.length - 1 || 34));
  const padN = (first.getDay() + 6) % 7;
  const cellCount = Math.ceil((days.length + padN) / 7) * 7;
  let html = DOW.map(w => `<div class="dow">${w}</div>`).join('');
  for (let i = 0; i < cellCount; i++) {
    if (i < padN) { html += '<div class="day blank"></div>'; continue; }
    const d = days[i - padN];
    if (!d) { html += '<div class="day blank"></div>'; continue; }
    const hit = (d.providers || []).length > 0;
    html += `<div class="day${hit ? ' hit' : ''}${d.date === todayStr ? ' today' : ''}" data-date="${d.date}">${Number(d.date.slice(8))}</div>`;
  }
  cal.innerHTML = html;
  // 悬停 tooltip 数据：日期 + 星期 + 各通道当日领取
  const dowOf = ds => DOW_FULL[(new Date(ds + 'T12:00:00').getDay() + 6) % 7];
  days.forEach(d => {
    const lines = (d.providers || []).map(pid => {
      const credit = fmtCredit((d.credits || {})[pid]);
      return `<div class="t-p"><i style="background:${pcolor(pid)}"></i>${esc(pid)}${credit != null ? ` <b style="color:var(--ok)">+${esc(credit)}</b>` : ''}</div>`;
    });
    window._calTips[d.date] =
      `<div class="t-date">${d.date} ${dowOf(d.date)}${d.date === todayStr ? ' · 今天' : ''}</div>` +
      (lines.length ? lines.join('') : '<div class="t-p muted">当天未打卡</div>');
  });

  // 只列「支持签到且当前有活动」的通道，两类整行隐藏：
  // - traeintl：上游无每日签到端点（supports_checkin=False），列出来只会
  //   永远挂着一行「无签到」，奇怪（2026-10-12 用户反馈）；
  // - codebuddyintl：渠道存在但海外站当前没开签到活动（inactive，上游
  //   status 报 active:false、claim 直接拒「签到活动未开启或已过期」，2026-10-12
  //   实测），挂着一行「暂无签到活动」+ 名义上的「每日 +300」纯属误导。
  // 按状态而非通道 id 判：哪天海外开了活动 inactive 翻 false，行自动回来，
  // 不用改代码。它们的额度仍由下方通用渠道卡正常展示。
  const rows = (BENEFITS.providers || []).filter(p =>
    p.checkin.supported && !p.checkin.inactive);
  document.getElementById('checkin-rows').innerHTML = rows.length ? rows.map(p => {
    const c = p.checkin;
    // 签到态做成带状态点的徽标；「连续 N 天 / 每日 +X」降级成旁边的小 chip，
    // 别和状态挤在同一个 tag 里（那行本来就长，挤一起更难扫）
    // 查询失败（error / query_failed）→「状态未知」，绝不画成「未签到」：
    // 声明支持打卡却拿不到状态时，显示可点的「立即打卡」会让用户以为今天还没打
    // （dumate 在无本机登录态时就是这条路径——别处已打过卡，本机显示未打卡）。
    const unknown = c.error || c.query_failed;
    const st = unknown ? `<span class="br-state bad" title="${esc(c.error || c.message || '')}">状态未知</span>`
      : c.unavailable ? `<span class="br-state" title="${esc(c.message || '')}">有活动，暂无可领签到奖励</span>`
      : c.inactive ? `<span class="br-state"${c.message && c.message !== 'OK' ? ` title="${esc(c.message)}"` : ''}>${c.activity_name ? '活动未开始' : '暂无签到活动'}</span>`
      : c.done_today ? '<span class="br-state ok">已签到</span>'
      : '<span class="br-state pending">未签到</span>';
    // 有逐账号明细行时第一行不再放状态徽标：第二行一号一个状态，顶上再放
    // 一个聚合态既重复又挤（用户反馈）。整体查询失败（状态未知）仍保留——
    // 那种情况明细行也全是「查询失败」，徽标的 error title 是唯一线索。
    const badge = (c.accounts && c.accounts.length && !c.error) ? '' : st;
    const chips = [];
    // daily_credit 是多账号总和（后端聚合），带（N账号）说明口径，免得和
    // 单账号通道并排时看着数字莫名翻倍
    // inactive 时 daily_credit 是上游照报的名义奖励额（活动没开也带 100/账号），
    // 显示「每日 +N」会让人以为能领——虽然 inactive 行已被上面过滤，这里
    // 再拦一道（行过滤逻辑将来改动时这层兜底别跟着丢）
    if (c.daily_credit > 0 && !c.inactive) {
      const n = (c.accounts || []).length;
      chips.push(`每日 +${fmtCredit(c.daily_credit)}${n > 1 ? `（${n}账号）` : ''}`);
    }
    if (c.streak_days >= 2) chips.push(`连续 ${c.streak_days} 天`);
    // activity_name（上游 campaign key，如 act-20260930-551）不再展示：对用户
    // 纯粹是噪音，用户反馈「看得更迷糊」；字段后端仍下发，排查日志时有用。
    const meta = chips.map(t => `<span class="br-chip">${esc(t)}</span>`).join('');
    // 多账号通道的 per-account 明细（Trae/Qoder 下发的 accounts 列表）：一行一账号，
    // 徽标 + 失败原因。整体聚合态说不清「哪个号没签上」，用户反馈过这个盲点。
    const acctsHtml = (c.accounts || []).map(a => {
      const s = a.error ? `<span class="br-state bad" title="${esc(a.error)}">查询失败</span>`
        : a.unavailable ? `<span class="br-state" title="${esc(a.message || '')}">暂无可领奖励</span>`
        : a.inactive ? '<span class="br-state">无活动</span>'
        : a.ok === false ? '<span class="br-state bad">失败</span>'
        : (a.claimable ? '<span class="br-state pending">可领</span>'
          : '<span class="br-state ok">已签到</span>');
      const extra = a.message ? `<span class="muted" style="font-size:11px">${esc(a.message)}</span>` : '';
      return `<div class="ck-acct">${s}<span class="ck-acct-name">${esc(a.name || ('#' + a.index))}</span>` +
        `${a.id ? acctRenameButton(p.id, a) : ''}${extra}</div>`;
    }).join('');
    // 照 antigravity 面板的卡中卡（pat-pkg）包一层：两列平摊时裸行 + 底线
    // 会把左右两列糊成一片，子卡（亮底 + 描边 + 圆角）分隔一眼能看出来
    return `<div class="pat-pkg ck-pkg">
      <div class="benefit-row">
        <div class="br-id">
          <i class="br-dot" style="background:${pcolor(p.id)}"></i>
          <span class="br-name">${esc(p.id)}</span>
        </div>
        <div class="br-meta">${badge}${meta}${nextTimeHtml(c)}</div>
        <div class="br-act">
          <button onclick="refreshCheckin('${esc(p.id)}', this)" title="绕过缓存，真打上游重查该通道签到状态">强刷</button>
          <button class="primary" ${(unknown || c.done_today || c.inactive || c.unavailable) ? 'disabled' : ''} onclick="claimNow('${esc(p.id)}', this)" ${unknown ? 'title="状态未知，先点「强刷」重查"' : ''}>立即打卡</button>
        </div>
      </div>
      ${acctsHtml ? `<div class="ck-accts">${acctsHtml}</div>` : ''}
    </div>`;
  }).join('') : '<div class="empty" style="grid-column:1/-1;padding:14px 0">当前没有支持打卡的通道</div>';
  // 数据换了新的一批 data-next-ts，重算一次并决定要不要起 ticker
  syncNextTimeAuto();

  // traepat 的日包/周包挪到底部 PAT 面板内展示，这里排除，避免重复且缩短页面
  // traepat/antigravity/kimi/codex/qoder/qoderintl/trae/traeintl/codebuddy/dumate 的额度
  // 挪到底部专属面板内展示，这里排除，避免重复且缩短页面。
  // 停用的通道整卡不展示（模型页 provider 开关，用户 2026-10-06 需求）
  const qps = (BENEFITS.providers || []).filter(p => !p.disabled && p.quota.supported && p.id !== 'traepat' && p.id !== 'antigravity' && p.id !== 'kimi' && p.id !== 'codex' && p.id !== 'qoder' && p.id !== 'qoderintl' && p.id !== 'trae' && p.id !== 'traeintl' && p.id !== 'codebuddy' && p.id !== 'codebuddyintl' && p.id !== 'dumate');
  // zcode / zcode-start 是同一家产品的两档套餐，卡片排一起好对照——providers
  // 默认按通道注册顺序排，zcode-start 落在队尾、和 zcode 中间隔着 glm/mimo 的卡
  const zcIdx = qps.findIndex(p => p.id === 'zcode');
  const zcsIdx = qps.findIndex(p => p.id === 'zcode-start');
  if (zcIdx >= 0 && zcsIdx > zcIdx + 1) {
    const [zcs] = qps.splice(zcsIdx, 1);
    qps.splice(zcIdx + 1, 0, zcs);
  }
  document.getElementById('quota-list').innerHTML = qps.length ? qps.map(p => {
    const q = p.quota;
    const items = quotaItemsHtml(q.items || [], p.id);
    const headSum = quotaHeadSum(q);
    return `<div class="chart-card" style="margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
        <span style="font-weight:600">${esc(p.name)}</span>
        ${q.level ? `<span class="tag">${esc(q.level)}</span>` : ''}
        ${headSum}<span style="flex:1"></span>${providerRefreshButton(p.id)}
      </div>${items || '<div class="empty" style="padding:12px 0">无额度数据</div>'}</div>`;
  }).join('') : '<div class="chart-card"><div class="empty" style="padding:14px 0">当前通道均不支持额度查询</div></div>';
  if (quiet) {
    // 上面已把签到区和通用额度卡换成新快照；各通道面板原样留着，只补角标和
    // 账号回填，然后收工
    syncQuotaFold();
    refreshBenefitsBadge();
    loadAllAccounts();
    return;
  }
  renderCodebuddyPanel();
  renderCodebuddyIntlPanel();
  renderTraePanel();
  renderTraeIntlPanel();
  renderTraepatPanel();
  renderKimiPanel();
  renderCodexPanel();
  renderQoderPanel();
  renderQoderIntlPanel();
  renderAntigravityPanel();
  renderDumatePanel();
  syncQuotaFold();  // 样式与布局就位后按实际高度校准（各面板都已重建完）
  renderBenefitsNav();
}

// 打卡页签角标（「✓ 已签」/「未签 N」）。原先内联在 app.js 的 render() 里，
// 静默重绘不整页 render 时也得跟着快照更新，挪过来单独一个函数。
function refreshBenefitsBadge() {
  if (!BENEFITS) return;
  const el = document.getElementById('cnt-benefits');
  if (!el) return;
  const ck = (BENEFITS.providers || []).filter(p => p.checkin.supported);
  if (!ck.length) { el.textContent = ''; return; }
  const pending = ck.filter(p => !p.checkin.done_today && !p.checkin.inactive && !p.checkin.error);
  if (!pending.length) { el.textContent = '✓ 已签'; el.className = 'cnt ok'; }
  else { el.textContent = `未签 ${pending.length}`; el.className = 'cnt warn'; }
}

// 各通道账号快照加载器登记表（键 = 通道 pid）。loadAllAccounts 只对「当前
// 已经渲染出来」的面板补一发——静默重绘后快照里出现的账号能就地补上名字和
// 按钮；数据没变的话加载器自己早退，不发请求。表本体在 benefits_dumate.js
// 尾部，声明在惰性加载器之后；此处在它之后执行，渲染和刷新由 app.js 后续触发。
/** 静默地把已渲染面板的账号快照对一遍（打开静默开关：期间加载器只就地回填，
 *  不许整卡重渲）。各加载器自带 catch，这里 Promise.all 只防漏网。 */
function loadAllAccounts() {
  ACCT_NO_RERENDER = true;
  const jobs = [];
  for (const pid of Object.keys(ACCT_LOADERS)) {
    const panel = document.getElementById(`${pid}-panel`);
    if (!panel || !panel.innerHTML.trim()) continue;
    jobs.push(Promise.resolve().then(ACCT_LOADERS[pid]).catch(() => {}));
  }
  return Promise.all(jobs).finally(() => { ACCT_NO_RERENDER = false; });
}
