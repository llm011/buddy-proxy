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
function renderBenefits() {
  if (!BENEFITS) return;
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

  const rows = (BENEFITS.providers || []).filter(p =>
    p.checkin.supported || (!p.disabled && p.id === 'traeintl'));
  document.getElementById('checkin-rows').innerHTML = rows.length ? rows.map(p => {
    const c = p.checkin;
    // Trae 海外版上游没有每日签到端点：展示原因但不伪装成支持，更不能渲染
    // 「立即打卡」按钮。额度仍由下方通用渠道卡正常展示与刷新。
    if (p.id === 'traeintl' && !c.supported) {
      return `<div class="pat-pkg ck-pkg"><div class="benefit-row">` +
        `<div class="br-id"><i class="br-dot" style="background:${pcolor(p.id)}"></i>` +
        `<span class="br-name">${esc(p.id)}</span></div>` +
        `<div class="br-meta"><span class="br-state">海外版无每日签到</span></div>` +
        `<div class="br-act"></div></div></div>`;
    }
    // 签到态做成带状态点的徽标；「连续 N 天 / 每日 +X」降级成旁边的小 chip，
    // 别和状态挤在同一个 tag 里（那行本来就长，挤一起更难扫）
    const st = c.error ? `<span class="br-state bad" title="${esc(c.error)}">状态未知</span>`
      : c.unavailable ? `<span class="br-state" title="${esc(c.message || '')}">有活动，暂无可领签到奖励</span>`
      : c.inactive ? '<span class="br-state">今日无签到活动</span>'
      : c.done_today ? '<span class="br-state ok">已签到</span>'
      : '<span class="br-state pending">未签到</span>';
    // 有逐账号明细行时第一行不再放状态徽标：第二行一号一个状态，顶上再放
    // 一个聚合态既重复又挤（用户反馈）。整体查询失败（状态未知）仍保留——
    // 那种情况明细行也全是「查询失败」，徽标的 error title 是唯一线索。
    const badge = (c.accounts && c.accounts.length && !c.error) ? '' : st;
    const chips = [];
    // daily_credit 是多账号总和（后端聚合），带（N账号）说明口径，免得和
    // 单账号通道并排时看着数字莫名翻倍
    if (c.daily_credit > 0) {
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
          <button class="primary" ${(c.done_today || c.inactive || c.unavailable) ? 'disabled' : ''} onclick="claimNow('${esc(p.id)}', this)">立即打卡</button>
        </div>
      </div>
      ${acctsHtml ? `<div class="ck-accts">${acctsHtml}</div>` : ''}
    </div>`;
  }).join('') : '<div class="empty" style="grid-column:1/-1;padding:14px 0">当前没有支持打卡的通道</div>';
  // 数据换了新的一批 data-next-ts，重算一次并决定要不要起 ticker
  syncNextTimeAuto();

  // traepat 的日包/周包挪到底部 PAT 面板内展示，这里排除，避免重复且缩短页面
  // traepat/antigravity/kimi/qoder/qoderintl/trae/traeintl/codebuddy/dumate 的额度
  // 挪到底部专属面板内展示，这里排除，避免重复且缩短页面。
  // 停用的通道整卡不展示（模型页 provider 开关，用户 2026-10-06 需求）
  const qps = (BENEFITS.providers || []).filter(p => !p.disabled && p.quota.supported && p.id !== 'traepat' && p.id !== 'antigravity' && p.id !== 'kimi' && p.id !== 'qoder' && p.id !== 'qoderintl' && p.id !== 'trae' && p.id !== 'traeintl' && p.id !== 'codebuddy' && p.id !== 'codebuddyintl' && p.id !== 'dumate');
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
  renderCodebuddyPanel();
  renderCodebuddyIntlPanel();
  renderTraePanel();
  renderTraeIntlPanel();
  renderTraepatPanel();
  renderKimiPanel();
  renderQoderPanel();
  renderQoderIntlPanel();
  renderAntigravityPanel();
  renderDumatePanel();
  syncQuotaFold();  // 样式与布局就位后按实际高度校准（各面板都已重建完）
  renderBenefitsNav();
}

// ---- 额度页侧边导航 ----
// 条目 = 当前实际渲染出来的各通道卡（含打卡日历与各通道签到）：通道开关 /
// 登录状态一变，导航得跟着变，所以在 renderBenefits 尾部（所有面板重建后）重建。
// 只把「有内容」的面板列进去——空面板（未注册/停用通道）滚过去只有空白，没
// 意义；PAT 卡有自己完整的空态清空（renderTraepatPanel），逻辑上无特权。
// 高亮用 IntersectionObserver：视口里最靠上的已渲染卡片对应的条目亮着，
// 比纯 scroll 监听省事且滚动中实时跟随。观察器随条目一起重建（面板 DOM 换了
// 旧观察全失效，挂着只会白跑回调）。
function renderBenefitsNav() {
  const nav = document.getElementById('benefits-sidenav');
  if (!nav) return;
  const page = document.getElementById('page-benefits');
  const labels = [];
  const cards = [];
  // 打卡日历：页首第一张卡（grid2.top 里左栏那张）。选择器只匹配得到它
  // ——后面的面板卡都是动态 innerHTML 填充、不带 .chart-card 属性节点。
  const cal = page && page.querySelector('.grid2.top > .chart-card');
  if (cal) { labels.push(['打卡日历', 'cal']); cards.push(cal); }
  // 「各通道签到」全宽卡（DOM 位置在日历 grid 与通道面板之间）：也是页面
  // 主区块之一，滚过去要能一键回来看签到状态
  const ck = page && page.querySelector('.rows-card');
  if (ck && ck.querySelector('#checkin-rows')) {
    labels.push(['各通道签到', 'checkin']); cards.push(ck);
  }
  page.querySelectorAll('[id$="-panel"]').forEach(p => {
    if (!p.innerHTML.trim()) return;      // 未注册/停用的通道面板：整块空
    const pid = p.id.replace(/-panel$/, '');
    const shown = (BENEFITS.providers || []).find(x => x.id === pid && !x.disabled);
    if (!shown) return;                    // 停用通道：面板有历史 DOM 也不列
    labels.push([shown.name || pid, pid]);
    cards.push(p);
  });
  if (!labels.length) { nav.innerHTML = ''; _navDisconnect(); return; }
  // rail 标签剥掉通道名里的括注（「Antigravity (Gemini/Claude/GPT free tier)」→
  // 「Antigravity」）：导航宽度钉死 132px（style.css .sidenav），长名只会被
  // 省略号截断；完整名挂 title，悬停可见。循环剥到不动为止——单趟正则
  // 穿不透嵌套括号（「A (B (C))」会剩个悬空括号，review 指出）。
  const shortLabel = (l) => {
    let s = String(l), prev;
    do { prev = s; s = s.replace(/[（(][^()（）]*[)）]/g, ""); } while (s !== prev);
    return s.trim() || l;
  };
  nav.innerHTML = labels.map(([label, pid], i) =>
    `<a data-i="${i}" title="${esc(label)}"><span class="sn-dot" style="background:${pcolor(pid)}"></span><span class="sn-label">${esc(shortLabel(label))}</span></a>`).join('');
  // 点击不用 hash 跳转（会改地址栏且默认硬跳）。不用 scrollIntoView：
  // block:'start' 会把卡片顶到视口 0，被 sticky 的 .topbar（tab 头）盖住
  // 一截（2026-10-12 用户实报）——手动滚，目标位置扣掉 topbar 实际高度。
  // 滚动动画期间（~800ms）把高亮锁到目标卡——中途经过的卡片会轮流进
  // IntersectionObserver 的视口，不锁的话高亮会从上到下逐个闪过去。
  nav.querySelectorAll('a').forEach(a => {
    a.addEventListener('click', e => {
      e.preventDefault();
      const card = cards[+a.dataset.i];
      nav.querySelectorAll('a').forEach(x =>
        x.classList.toggle('active', x === a));
      const bar = document.querySelector('.topbar');
      const top = bar ? bar.getBoundingClientRect().bottom : 0;
      const y = card.getBoundingClientRect().top + window.scrollY - top - 6;
      _navLockUntil = Date.now() + 800;
      window.scrollTo({ top: Math.max(0, y), behavior: 'smooth' });
    });
  });
  // 滚动高亮：每张卡进入/离开视口时回调，取「离视口顶最近且已进入」的那张
  const active = new Map();  // card -> isIntersecting
  const io = new IntersectionObserver(items => {
    items.forEach(it => active.set(it.target, it.isIntersecting));
    if (Date.now() < _navLockUntil) return;  // 点击后的平滑滚动中：高亮已锁
    let best = null, bestTop = Infinity;
    active.forEach((on, card) => {
      if (!on) return;
      const top = card.getBoundingClientRect().top;
      if (top < bestTop) { bestTop = top; best = card; }
    });
    nav.querySelectorAll('a').forEach(a => {
      a.classList.toggle('active', cards[+a.dataset.i] === best);
    });
  }, { root: null, threshold: 0 });
  cards.forEach(c => io.observe(c));
  _navDisconnect();
  renderBenefitsNav._io = io;
}

let _navLockUntil = 0;

function _navDisconnect() {
  renderBenefitsNav._io && renderBenefitsNav._io.disconnect();
  renderBenefitsNav._io = null;
}

// 单条额度条目 → HTML（周包/日包/积分窗口通用；PAT 面板与额度列表共用）
// 标题行的「剩 X / Y」汇总。
//
// 不能一律把 items 加起来——各家 items 的语义不同：
// - Qoder：订阅额度 + 加油包 + 专属积分是**并存的三份额度**，加起来才是账号
//   总量（上游自己的 totalUsagePercentage 就是这么算的）。只取第一条会显示
//   剩 1621/2000，而实际还有 100 + 1030 没算进去。
// - Trae：``总额度`` 已经是账号总量，下面的权益包只是它的**明细**（且
//   used/total 为空），加起来就是重复计算。
// - ZCode / MiMo：各条是不同口径（5 小时窗口 vs 月窗口；百分比 vs 天数），
//   量纲都不同，相加没有意义。
//
// 所以由后端用 ``sum_items: true`` 显式声明「这些条目可以合计」，前端只认
// 这个标记，不靠猜——靠猜的话，下一个新通道就会被默默算错。
function quotaHeadSum(q) {
  const items = q.items || [];
  // 只认「能当数字用」的值。两道剔除各有来由：
  // - null / '' / 布尔：``Number(null)`` 和 ``Number('')`` 都是 0，不收的话
  //   会把「没有数据」当 0 求进合计；``Number(true)`` 是 1，更不能要。
  // - 收不进数字的字符串：那是**说明条**不是额度——``trae.pat.quota.
  //   _failure_notice`` 的 remaining 发的是「2/9 个账号本轮未取到新数据」
  //   这类文案，``quotaItemHtml`` 也正是按 ``typeof === 'string'`` 认它的。
  //   不加这道判断，说明条会被当数字求和，标题行渲染成「剩 NaN / NaN」，
  //   比不显示还糟。
  // 注意**数字字符串（"2000"）要照收**：上游给 zcode 的 unit/number 就发过
  // 字符串，额度字段同样可能这么来，直接拿 Number.isFinite 会把它误杀。
  const num = v => v !== null && v !== '' && typeof v !== 'boolean'
                && Number.isFinite(Number(v));
  const usable = items.filter(it => num(it.remaining) && num(it.total));
  if (q.sum_items) {
    // 一条都没有就别显示（否则会渲染成「剩 0 / 0」）
    if (!usable.length) return '';
    const rem = usable.reduce((a, it) => a + Number(it.remaining), 0);
    const total = usable.reduce((a, it) => a + Number(it.total), 0);
    const label = usable.length > 1 ? `${usable.length} 项合计` : '';
    const used = total > 0 ? ` · 已用 ${Math.round((1 - rem / total) * 100)}%` : '';
    return `<span class="mono" style="margin-left:auto" title="${esc(label || '剩余 / 总额')}">
      <span style="color:var(--ok);font-weight:600">剩 ${fmtNum(rem)}</span>
      <span class="muted">/ ${fmtNum(total)}${used}</span>
      ${label ? `<span class="muted" style="font-size:11px"> · ${esc(label)}</span>` : ''}</span>`;
  }
  // 未声明可合计：取第一条有数的条目。此时**列表顺序就是语义**——各家自己
  // 决定先展示哪一档（ZCode 是「窗口由小到大」，5 小时档在前；Trae 是总额度
  // 在前），前端不再自作主张挑「最紧的」。
  const head = usable[0];
  if (!head) return '';
  // 「已用 N%」放在总量后面（用户 2026-10-03：参考明细行的写法）——各条
  // 量纲能否相加无所谓，这是单条自己的比例，永远成立
  const hRem = Number(head.remaining), hTot = Number(head.total);
  const hUsed = hTot > 0 ? ` · 已用 ${Math.round((1 - hRem / hTot) * 100)}%` : '';
  return `<span class="mono" style="margin-left:auto">
      <span style="color:var(--ok);font-weight:600">剩 ${fmtNum(head.remaining)}</span>
      <span class="muted">/ ${fmtNum(head.total)}${hUsed}</span></span>`;
}

function quotaItemHtml(it) {
  let pct = it.percent;
  if (pct == null && it.used != null && it.total) pct = Math.round(it.used / it.total * 100);
  const hasBar = pct != null;
  pct = Math.max(0, Math.min(100, Number(pct) || 0));
  const color = pct >= 85 ? 'var(--err)' : pct >= 60 ? 'var(--warn)' : 'var(--ok)';
  let rem = it.remaining;
  if (rem == null && it.used != null && it.total != null) rem = it.total - it.used;
  // 只有「分子分母都拿到」才算有数：只见 total 时（例如额度网关只回包容量、
  // 还没回用量）下面的 ``percent`` 实际是 0，报出来就是「剩 300 / 300 · 已用
  // 0%」这种把未知当零的假象。有 rem 走正常文案，没 rem 就说「已用未知」。
  const hasNums = rem != null;
  const hasVolume = hasNums && it.total != null;
  const fmtReset = it.reset_ts ? new Date(it.reset_ts * 1000).toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'}) : '';
  // 已过重置时刻的 standard 池：后端不再给用量（那份快照是撞码时采集的，必然接近
  // 满额，池重置后不会有新数据来覆盖），这里明确说明「待下一次撞码确认」，
  // 免得看起来像池没被重置或卡在 100%。
  const pending = !hasNums && it.reset_pending;
  // reset_note：说明「这个时间点到底是什么」——如 antigravity 的 resetTime 只是
  // 下一个 5 小时窗口的滚动刷新点，不代表整组/每周池重置。拼进 title 而不是
  // 正文，免得窄卡更挤。
  const resetNote = typeof it.reset_note === 'string' && it.reset_note ? `（${esc(it.reset_note)}）` : '';
  const reset = fmtReset && !pending
    ? (resetNote ? `<span title="${resetNote}"> · ${fmtReset} 重置</span>`
                 : ` · ${fmtReset} 重置`)
    : '';
  // 到期时间（expire_ts）与重置时间是两回事：到期是这份权益作废、不再回来，
  // 重置是周期回满。早先 Qoder 的 expiresAt 被塞进 reset_ts，界面上显示成
  // 「10-30 重置」——用户以为到期日没被记录。这里分开渲染，措辞也分开。
  const fmtExpire = it.expire_ts
    ? new Date(it.expire_ts * 1000).toLocaleString('zh-CN', {month: '2-digit', day: '2-digit'})
    : '';
  const expire = fmtExpire ? ` · ${fmtExpire} 到期` : '';
  // 查询失败说明条：后端给出 remaining 文案 + unreachable/query_failed 标记，
  // 用警告色区分于正常额度，并说明「是网络问题、已缓存、无需反复刷新」——
  // 以前这种情况页面只是转圈没有任何解释，用户不知道卡在哪。
  const noticeText = (!hasNums && !pending && typeof it.remaining === 'string') ? it.remaining : '';
  // 三类说明条分开配色：unreachable=红（网关不可达）、query_failed=黄（账号
  // 取不到额度，要动手查/删号）、query_empty=灰（账号是好的，只是 Free 层没
  // 分桶额度——不该当告警，否则用户会以为通道坏了）
  const notice = it.unreachable === true || it.query_failed === true || it.query_empty === true;
  const noticeColor = it.unreachable ? 'var(--err)'
    : (it.query_failed ? 'var(--warn)' : 'var(--muted)');
  const nums = hasNums
    ? (hasVolume
        ? `<span class="mono"><span style="color:var(--text)">剩 ${fmtNum(rem)}</span> / ${fmtNum(it.total)} · 已用 ${pct}%</span>`
        : `<span class="mono"><span style="color:var(--text)">剩 ${fmtNum(rem)}</span> · 已用未知</span>`)
    : (pending
        ? `<span class="mono muted" title="standard 池无主动查询接口，用量仅在账号撞 4031 时被动采集；重置后成功请求不带账单事件，故用量待下次撞码确认">已于 ${fmtReset || '—'} 重置 · 用量待确认</span>`
        : (notice
            ? `<span class="mono" style="color:${noticeColor}">${esc(noticeText)}</span>`
            : (it.total != null
                // 总额已知、用量未知（如海外 Trae 的次数制：上游不给已用
                // 次数）——明说「剩 — / N · 已用未知」，别兜底成空 span
                //（那行只剩 label + 到期日，看不出是没用量还是没渲染出来）
                ? `<span class="mono"><span style="color:var(--text)">剩 —</span> / ${fmtNum(it.total)} · 已用未知</span>`
                : '<span class="mono"></span>')));
  // 用量为 0 时留空条（不画那撮绿点，避免「0% 却有进度」的观感）；
  // >0 时至少给 2% 让细条可见
  const bar = hasVolume && hasBar ? `<div class="qbar"><div style="width:${pct > 0 ? Math.max(2, pct) : 0}%;background:${color}"></div></div>` : '';
  // note：后端的解释性附注（如 antigravity 的「组内包含哪些模型 / 满额含义」）。
  // 以前只挂 label 的 title，但面板是窄卡、悬停提示基本看不到——用户实际反馈
  // 「额度都是 0、看不明白」就是没看到这层解释。改为正文一行小号 muted 展示：
  // 塞进 label 会把日期和数字挤折行，单独一行则不影响标题行布局。
  const note = typeof it.note === 'string' && it.note ? String(it.note) : '';
  const noteHtml = note
    ? `<div class="muted" style="font-size:11px;line-height:1.45;margin:2px 0 0">${esc(note)}</div>`
    : '';
  return `<div class="qitem"><div class="qhead">` +
    `<span class="qlabel">${esc(it.label)}${expire}${reset}</span>` +
    `${nums}</div>${bar}${noteHtml}</div>`;
}

// ---- 额度条目分组：只铺没消耗完的 + 限高折叠 ----
//
// 为什么必须收：Trae 一次返回 28 条，其中 12 条签到奖励各自独立、且大多已
// 花光，全铺出来一屏都是「200/200 · 已用 0%」的重复行，真正还有余额的几条
// 反而被淹掉。所以默认只显示还有剩余的，已用完的收进底部那一块；条数再多，
// 整块超过最大高度就裁一刀（点按钮展开）。三处渲染（主列表 / PAT / antigravity）
// 共用这一套，免得只有 trae 变好看、别的通道还是老样子。
//
// 展开状态存在 QUOTA_FOLD 而不是 DOM 上：整页每 30s 重建一次 innerHTML，
// 状态挂 DOM 里会被下一轮刷掉——用户刚点开，30 秒后自己收回去。
const QUOTA_FOLD = { open: {} };

// 条目是否「已用完」。只认余量明确 ≤0 的（含 "0" 这类数字字符串，上游发字符串
// 是常事）。拿不到余量的一律不算：null / 说明条文案（"2/9 个账号查询失败"）/
// reset_pending 的「用量待确认」都是**未知**，不是「没花」——按用完收起来，
// 正好会把最该看见的那条藏掉。
function quotaSpent(it) {
  let r = it.remaining;
  // 与 quotaItemHtml 同一条回填规则：只见 total/used 时也能算出余量
  if (r == null && it.used != null && it.total != null) r = Number(it.total) - Number(it.used);
  if (r == null || r === '' || typeof r === 'boolean') return false;
  const n = Number(r);
  return Number.isFinite(n) && n <= 0;
}

// 一组额度条目 → HTML。key 是这组的稳定标识（通道 id / PAT #N / AG #N），
// 用来记住展开状态；同 key 在两次渲染之间保持展开。
function quotaItemsHtml(items, key) {
  if (!items.length) return '';
  // head_only：这条只为标题行「剩 X / Y」提供数字（Trae 的「总额度」——它是
  // 下面各权益包的合计，再单列一条带进度条的明细就是重复）。明细列表跳过它，
  // 标题行（quotaHeadSum）与余额告警仍照取 items[0]。
  const shown = items.filter(it => !it.head_only);
  if (!shown.length) return '';
  const act = [], spent = [];
  for (const it of shown) (quotaSpent(it) ? spent : act).push(it);
  const open = !!QUOTA_FOLD.open[key];
  const spentHtml = spent.length
    ? `<div class="qspent"${open ? '' : ' hidden'}>` +
      `<div class="qspent-head">已用完 ${spent.length} 项</div>` +
      spent.map(quotaItemHtml).join('') + '</div>'
    : '';
  // 一条都没剩时给个占位：否则收起状态下这组整个是空的，看着像加载失败
  const empty = !act.length && spent.length
    ? '<div class="empty" style="padding:10px 0">权益已全部用完</div>' : '';
  return `<div class="qbody${open ? ' open' : ''}" data-qfold="${esc(key)}">` +
      act.map(quotaItemHtml).join('') + empty + spentHtml + '</div>' +
    `<button class="qmore" data-qfold="${esc(key)}" data-act="${act.length}" ` +
      `data-spent="${spent.length}" data-total="${shown.length}" ` +
      `onclick="quotaFoldToggle(this)" hidden></button>`;
}

// 渲染后校准：按**实际高度**决定裁不裁、按钮露不露。
// 为什么不用「超过 N 条就收」：同一屏里窄卡片（两栏）和整宽卡片的可用高度
// 不同，按条数定界在窄卡片上会裁错。CSS 只管裁剪，判断在这儿做。
//
// 高度上限只写在 CSS 变量 --qfold-max 一处，这里读它——两边各写一个 208，
// 改了 CSS 忘了 JS 就会出现「已经裁了但按钮不出现」的死角。
function syncQuotaFold() {
  document.querySelectorAll('.qbody[data-qfold]').forEach(box => {
    const open = !!QUOTA_FOLD.open[box.dataset.qfold];
    // 先定已用完区块的显隐，再量高度：收起时量的正是「没消耗完的那部分」
    const spent = box.querySelector('.qspent');
    if (spent) spent.hidden = !open;
    // scrollHeight 报的是内容自然高度，不被 max-height 夹住（headless Chrome
    // 实测：12 条时 465，而 clientHeight 被夹到 208），所以不必先摘掉 clipped
    // 类再量。加不加类量到的**不完全相等**（实测差 18px：overflow:hidden 让
    // 盒子自成 BFC，首尾两条 .qitem 的 9px 外边距不再塌陷出去），所以这里
    // 只拿它跟上限比大小、不拿它当精确像素用。
    const limit = parseFloat(getComputedStyle(box).getPropertyValue('--qfold-max')) || 208;
    const clipped = !open && box.scrollHeight > limit + 1;
    box.classList.toggle('open', open);
    box.classList.toggle('clipped', clipped);
    const btn = box.nextElementSibling;
    if (!btn || !btn.classList || !btn.classList.contains('qmore')) return;
    // 展开后总是留个「收起」入口；收起时只有真被裁了、或底下还压着已用完的
    // 条目，才值得给按钮——否则是个点了没有任何变化的假按钮
    btn.hidden = !open && !clipped && !spent;
    btn.textContent = open ? '收起 ▴'
      : (!Number(btn.dataset.act) ? `展开 ${btn.dataset.spent} 项已用完 ▾`
        : `展开全部 ${btn.dataset.total} 项 ▾`);
  });
}

function quotaFoldToggle(btn) {
  const key = btn.dataset.qfold;
  QUOTA_FOLD.open[key] = !QUOTA_FOLD.open[key];
  syncQuotaFold();
}

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
  loadDumateAccounts();
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

const loadDumateAccounts = acctLoad(
  {
    pid: 'dumate',
    nameOf: a => a.display_name || a.id,
    subExtra: _dumate_sub_extra,
    render: renderDumatePanel,
  },
  DUMATE_STATE,
);
