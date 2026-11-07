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
// 时钟每秒对一次表（纯本地计算，不打上游）；只在签到页可见且真有元素时跑。
let NEXT_TIME_TIMER = null;
function nextTimeWanted() {
  // 签到已独立成页（2026-11-07）：判 page-checkin，不是 page-benefits。
  // 判错页的后果是「进了签到页倒计时不刷新、停在页面加载那一刻」。
  const page = document.getElementById('page-checkin');
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
// 后端算好 expiring（谁快到期、还剩几天）与 low_quota（哪个通道快烧干），
// 这里只渲染不判规则——阈值与「积分类才做量过滤」的逻辑都在 benefits
// 一处，免得两边各写一份慢慢走偏。
//
// 形态是**两级列表**（用户 2026-11-04 要求）：
//   第 1 级 = 通道。每个通道一行，summary 里一眼看到「哪个通道、余额告急
//            多少、几项临期、最近哪天」；默认收起，点开才看明细。
//   第 2 级 = 该通道的明细行。多账号通道按「账号 #N」逐行铺开，每行是
//            「账号 / 资源包名 / 到期日 / 剩余天数」，4 天内到期的打
//            「N 天内」标签。
// 这样同一个通道无论有 1 个还是 30 个资源包，外层都只占一行（用户
// 2026-11-03 的旧版聚合会直接合并成一条、丢掉账号归属；现在改成折叠，
// 信息还在，但默认不刷屏）。
//
// 余额告急与到期**不再互相顶掉**：同一通道两边都命中时，仍然只占一行，
// 展开后两种明细都在。旧版「同通道与余额告急去重」会把 4 天内到期的资源包
// 藏掉，正是这次要修的（用户 2026-11-04）。
//
// 空壳包（余量 0）在渲染前收掉，但 4 天内的例外放行：这条原本是 2026-11-03
// 「同类合并」里的一半（另一半——同类包并成一行——已经被两级折叠取代：通道行
// 本来就只占一条，再合并会把「点开看每个包、哪个账号」的信息也合掉）。
//
// 展开状态存在 EXPIRY_FOLD 而不是 DOM 上：整页每 30 秒重建一次 innerHTML，
// 挂 DOM 里会被下一轮刷掉（用户刚点开、30 秒后自己收回去）。
const EXPIRY_FOLD = { open: {} };

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
// 余量只对积分类有意义（天数/次数/千分制/dollar 的 remaining 是各自的
// 量纲，铺出来只会让人困惑）。points 与 credit 都是积分（DuMate 发的是
// points），漏掉就会把「还剩多少」藏起来。
function _expiryAmountOf(e) {
  if ((e.unit !== 'credit' && e.unit !== 'points') || e.remaining == null) return null;
  const n = Number(e.remaining);
  return Number.isFinite(n) ? n : null;
}
// 后端拆出的包名（已剥掉「通道 #N ·」前缀）；老响应没有 item_label 时
// 退回原 label，保证前端先于后端更新也能渲染。
function _expiryItemName(e) {
  return e.item_label != null && e.item_label !== '' ? e.item_label : e.label;
}
// 余额告急的单行文案：credit 通道铺绝对值（那是它的告警依据），其余量纲
// 铺占比。label 由后端剥过通道名前缀，形如「#1 · 总额度」，多账号下能看出
// 告的是哪个号。
function _expiryLowText(p) {
  const isCredit = p.unit === 'credit';
  const amt = isCredit
    ? `剩 ${fmtNum(p.remaining)} credits`
    : (p.percent_left != null ? `剩 ${fmtNum(p.percent_left)}%` : '余额不足');
  return `余额告急${p.label ? ' · ' + p.label : ''} · ${amt}`;
}
// 账号排序：#1、#2 …；没有账号前缀的（单账号通道）排在最后，但仍算一组。
function _acctNum(a) {
  const m = String(a || '').match(/#(\d+)/);
  return m ? Number(m[1]) : 9999;
}
// 把 expiring + low_quota 合成「每通道一组」。同一通道的两种告警合并成
// 一行，正是用户要的「同一个通道只写一条」；后端 low_quota 目前本就是
// 一通道一条，这里再兜一道重复（降级/旧响应）也不会多铺行。
function buildAlertGroups(expiring, lowQuota) {
  const groups = new Map();
  const get = (pid, name) => {
    const key = String(pid == null ? '' : pid);
    let g = groups.get(key);
    if (!g) {
      g = {provider: key, provider_name: name || key, lows: [], items: []};
      groups.set(key, g);
    } else if (!g.provider_name && name) {
      g.provider_name = name;
    }
    return g;
  };
  for (const e of (expiring || [])) {
    if (!e || typeof e !== 'object') continue;
    get(e.provider, e.provider_name).items.push(e);
  }
  for (const p of (lowQuota || [])) {
    if (!p || typeof p !== 'object') continue;
    get(p.provider, p.provider_name).lows.push(p);
  }
  const out = [...groups.values()];
  for (const g of out) {
    const byAcct = new Map();
    for (const e of g.items) {
      const acct = String(e.account || '');
      if (!byAcct.has(acct)) byAcct.set(acct, []);
      byAcct.get(acct).push(e);
    }
    // 每个账号内按到期先后（后端已升序，这里再稳一次，旧响应也不乱）
    for (const items of byAcct.values()) {
      items.sort((a, b) => (Number(a.expire_ts) || 0) - (Number(b.expire_ts) || 0));
    }
    g.accounts = [...byAcct.entries()].sort((a, b) => _acctNum(a[0]) - _acctNum(b[0]));
    g.earliest = g.items.reduce((a, e) => {
      if (!a) return e;
      return (Number(e.expire_ts) || 0) < (Number(a.expire_ts) || 0) ? e : a;
    }, null);
    g.hasOver = g.items.some(e => Number(e.days_left) < 0);
    g.urgentCount = g.items.filter(e => e.urgent).length;
    // 同一通道若有多条余额告急（理论上不会），取最紧的一条渲染 summary；
    // 明细里仍全部列出，不丢信息。
    g.low = g.lows.reduce((a, p) => {
      if (!a) return p;
      const av = Number(a.percent_left), pv = Number(p.percent_left);
      if (!Number.isFinite(av)) return Number.isFinite(pv) ? p : a;
      if (!Number.isFinite(pv)) return a;
      return pv < av ? p : a;
    }, null);
  }
  // 余额告急最急（现在就没得用），其次已过期，再按最近到期排
  out.sort((a, b) => {
    if (a.lows.length !== b.lows.length) return b.lows.length - a.lows.length;
    if (a.hasOver !== b.hasOver) return a.hasOver ? -1 : 1;
    const at = a.earliest ? Number(a.earliest.expire_ts) || 0 : Infinity;
    const bt = b.earliest ? Number(b.earliest.expire_ts) || 0 : Infinity;
    if (at !== bt) return at < bt ? -1 : 1;
    return String(a.provider).localeCompare(String(b.provider));
  });
  return out;
}
// 第 2 级：通道展开后的明细。先铺余额告急（如果有），再逐账号铺资源包。
function renderAlertGroupBody(g, urgentDays) {
  let html = '';
  for (const p of g.lows) {
    const isCredit = p.unit === 'credit';
    const amt = isCredit
      ? `剩 ${fmtNum(p.remaining)} credits`
      : (p.percent_left != null ? `剩 ${fmtNum(p.percent_left)}%` : '余额不足');
    html += `<div class="eb-subrow eb-lowrow">` +
      `<span class="eb-acct">余额</span>` +
      `<span class="eb-label">${esc(p.label || '余额告急')} · ${esc(amt)}</span>` +
      `<span class="eb-date">—</span>` +
      `<span class="eb-days over">${p.percent_left != null ? esc(fmtNum(p.percent_left)) + '%' : '告急'}</span>` +
      `</div>`;
  }
  for (const [acct, items] of g.accounts) {
    for (const e of items) {
      const over = Number(e.days_left) < 0;
      const amt = _expiryAmountOf(e) != null && e.remaining != null
        ? `<span class="eb-hint">· 剩 ${fmtNum(e.remaining)}</span>` : '';
      const urgent = e.urgent
        ? `<span class="eb-urgent">${esc(String(urgentDays))} 天内</span>` : '';
      html += `<div class="eb-subrow">` +
        `<span class="eb-acct">${esc(acct || '')}</span>` +
        `<span class="eb-label">${esc(_expiryItemName(e))}${amt}</span>` +
        `<span class="eb-date">${esc(fmtExpireDate(e.expire_ts))}</span>` +
        `<span class="eb-days${over ? ' over' : ''}">${esc(fmtDaysLeft(e.days_left))}${urgent}</span>` +
        `</div>`;
    }
  }
  return html || '<div class="eb-subrow"><span class="eb-label eb-hint">无明细</span></div>';
}
// 第 1 级：一个通道一行 <details>。summary 里把「余额告急」「N 项临期」
// 压成徽标，右侧给最近到期日与剩余天数；展开才显示第 2 级明细。
function renderAlertGroup(g, urgentDays) {
  const open = !!EXPIRY_FOLD.open[g.provider];
  const badges = [];
  if (g.low) {
    badges.push(`<span class="eb-badge low">${esc(_expiryLowText(g.low))}</span>`);
  }
  if (g.items.length) {
    const n = g.items.length;
    const u = g.urgentCount;
    badges.push(`<span class="eb-badge${u ? ' urgent' : ''}">` +
      `${esc(String(n))} 项临期${u ? `（${esc(String(u))} 项 ${esc(String(urgentDays))} 天内）` : ''}</span>`);
  }
  const first = g.earliest;
  const dateHtml = first
    ? `<span class="eb-date">${esc(fmtExpireDate(first.expire_ts))}</span>`
    : `<span class="eb-date">—</span>`;
  let daysHtml;
  if (first) {
    daysHtml = `<span class="eb-days${Number(first.days_left) < 0 ? ' over' : ''}">${esc(fmtDaysLeft(first.days_left))}</span>`;
  } else {
    const p = g.low;
    daysHtml = `<span class="eb-days over">${p && p.percent_left != null ? esc(fmtNum(p.percent_left)) + '%' : '告急'}</span>`;
  }
  return `<details class="eb-group" data-eb-group="${esc(g.provider)}"${open ? ' open' : ''}>` +
    `<summary>` +
    `<span class="eb-prov">${esc(g.provider_name)}</span>` +
    `<span class="eb-label">${badges.join(' ')}</span>` +
    `${dateHtml}${daysHtml}</summary>` +
    `<div class="eb-sub">${renderAlertGroupBody(g, urgentDays)}</div>` +
    `</details>`;
}
// 嵌套 <details> 的展开状态回写。外层 #expirybar 是常驻元素，监听器只挂
// 一次；内层每次重建，靠冒泡/toggle 捕获统一收。
function bindExpiryFold(el) {
  if (!el || el.__ebBound || !el.addEventListener) return;
  el.__ebBound = true;
  el.addEventListener('toggle', ev => {
    const d = ev.target;
    if (!d || !d.dataset || !d.dataset.ebGroup) return;
    EXPIRY_FOLD.open[d.dataset.ebGroup] = !!d.open;
  }, true);
}
function renderExpiryBanner() {
  const el = document.getElementById('expirybar');
  if (!el) return;
  // 窗口天数以后端下发为准；老响应缺字段时退回 7（与后端默认一致）。
  const warnDays = Number(BENEFITS && BENEFITS.expiry_warn_days) || 7;
  // 「4 天内」紧急窗口同理，前端不自己存常量。
  const urgentDays = Number(BENEFITS && BENEFITS.expiry_urgent_days) || 4;
  // 空壳包（余量为 0）一律不铺出来：到期也带不走任何额度，只会把真告警淹了
  // （用户 2026-11-03 报障：DuMate 每天一个 login_bonus 小包，全是 0；
  // 2026-11-06 又反馈 CodeBuddy 三个剩 0 的包因为 4 天内到期被顶成临期告警，
  // 「太傻瓜了」）。所以这条**不看 days_left / urgent**——后端 4 天例外只豁免
  // 「余量小」，不豁免「余量为空」，这里守住同一条口径。
  // 判不出余量的（`_expiryAmountOf` 给 null：量纲不是 credit/points、字段
  // 缺失、或 NaN）照常放行，别把「不知道」当成「是 0」。
  //
  // 阈值是 0.05 而不是 0：下面渲染用 `fmtNum`（`Math.round(x * 10) / 10`），
  // 不足 0.05 一律显示成「剩 0」。只滤 `> 0` 的话，`remaining = 0.004` 这种尾差
  // 会被放行，用户看到的还是「剩 0 · 2 天后到期」——和这次要修的现象一样。
  // 与后端 `EXPIRY_EMPTY_REMAINING` 保持同一个数。
  //
  // 老后端没有 `urgent` 字段（静态资源先更新、服务还没重启）时按同一条规则
  // 自己算，徽标和「N 天内」标签才有得用。
  const isUrgent = e => (e && e.urgent != null)
    ? !!e.urgent
    : (Number(e && e.days_left) >= 0 && Number(e && e.days_left) <= urgentDays);
  // 判空阈值 0.05 与后端 `benefits.EXPIRY_EMPTY_REMAINING` 是同一个数，两边
  // 要一起改。写成字面量是因为这里的函数会被测试按 `function name(...)`
  // 逐个抽出来单独跑，顶层 const 带不进去。
  const list = ((BENEFITS && BENEFITS.expiring) || []).filter(e => {
    const amt = _expiryAmountOf(e);
    return amt == null || amt >= 0.05;
  }).map(e => (e && e.urgent == null)
    // 老响应：把推导出来的紧急标记补回条目，徽标和「N 天内」标签才有得用
    ? Object.assign({}, e, {urgent: isUrgent(e)}) : e);
  const low = (BENEFITS && BENEFITS.low_quota) || [];
  if (!list.length && !low.length) {
    el.className = 'expirybar';
    el.innerHTML = '';
    el.open = false;
    return;
  }
  bindExpiryFold(el);
  const hasOver = list.some(e => Number(e.days_left) < 0);
  el.className = 'expirybar show' + (hasOver ? ' expired' : '');
  const groups = buildAlertGroups(list, low);
  // 两种告警的汇总各说各的段，都命中就用「 · 」接上。计数按条目/通道：
  // 通道数只算一遍（同一通道两边都命中不重复数）。
  const expHead = list.length
    ? (hasOver ? `有 ${list.length} 项权益已过期或即将到期`
               : `有 ${list.length} 项权益将在 ${warnDays} 天内到期`)
    : '';
  const lowChannels = new Set(low.map(p => p && p.provider).filter(Boolean)).size;
  const lowHead = lowChannels ? `${lowChannels} 个通道余额告急` : '';
  const head = [expHead, lowHead].filter(Boolean).join(' · ');
  // 不依赖「后端已排序」这个隐含前提：旧响应/测试桩乱序时，最近一项
  // 仍要取真正最早的那条，否则 summary 会指错日子。
  const soonest = list.reduce((a, e) => {
    if (!a) return e;
    return (Number(e.expire_ts) || 0) < (Number(a.expire_ts) || 0) ? e : a;
  }, null);
  const soonHint = soonest
    ? `<span class="eb-hint"> · 最近一项 ${esc(fmtExpireDate(soonest.expire_ts))}（${esc(fmtDaysLeft(soonest.days_left))}）</span>`
    : '';
  el.innerHTML =
    `<summary><span class="eb-count"><b>⏳ ${esc(head)}</b>${soonHint}</span>` +
    `<span class="eb-hint">展开通道 ▾</span></summary>` +
    `<div class="eb-list">${groups.map(g => renderAlertGroup(g, urgentDays)).join('')}</div>`;
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
const CHECKIN_DETAIL_FOLD = { open: {} };

function checkinAccountState(a) {
  const cls = a.error ? 'bad'
    : a.unavailable || a.inactive ? ''
    : a.ok === false ? 'bad'
    : a.claimable ? 'pending' : 'ok';
  const label = a.error ? '查询失败'
    : a.unavailable ? '暂无可领奖励'
    : a.inactive ? '无活动'
    : a.ok === false ? '失败'
    : a.claimable ? '可领' : '已签到';
  return {cls, label};
}

function checkinAccountDetails(c) {
  return (c.accounts || []).map(a => {
    const st = checkinAccountState(a);
    const name = a.name || ('#' + a.index);
    const message = a.error || a.message;
    const credit = a.daily_credit > 0 ? `<span class="muted">每日 +${esc(fmtCredit(a.daily_credit))}</span>` : '';
    return `<div class="ck-account-card ${st.cls}">
      <div class="ck-account-head"><strong>${esc(name)}</strong><span class="br-state ${st.cls}">${esc(st.label)}</span></div>
      ${credit}${message ? `<div class="ck-account-message">${esc(message)}</div>` : ''}
    </div>`;
  }).join('');
}

function checkinDetailToggle(btn) {
  const pid = btn.dataset.checkinFold;
  CHECKIN_DETAIL_FOLD.open[pid] = !CHECKIN_DETAIL_FOLD.open[pid];
  const open = CHECKIN_DETAIL_FOLD.open[pid];
  const body = document.getElementById('checkin-details-' + pid);
  if (body) body.hidden = !open;
  if (btn) {
    btn.setAttribute('aria-expanded', String(open));
    btn.textContent = open ? '收起账号 ▴' : '账号明细 ▾';
  }
}

// ---- 签到页：今日概览 + 通道卡片 + 日历 ----
// 2026-11-07 从「打卡 & 额度」拆出来独立成页。原先签到被压在十来张额度卡上面，
// 滚两屏就看不见；而签到是每天都要看一眼的动作，额度只是偶尔查。拆开后这一页
// 只回答三件事：今天签了没、谁还没签、点一下全领。
//
// 数据仍来自同一份 /ui/api/benefits 快照——拆页是展示层的事，不多打一次上游。

/** 单通道今日状态归一：卡片配色、概览计数、页签角标共用这一套口径。 */
function checkinChannelState(p) {
  const c = p.checkin || {};
  if (c.error || c.query_failed) return 'unknown';
  if (c.inactive) return 'inactive';
  if (c.unavailable) return 'unavailable';
  const accounts = c.accounts || [];
  if (accounts.length) {
    const pending = accounts.filter(a => a.claimable && !a.checked_in && a.ok !== false);
    if (pending.length) return 'pending';
    const failed = accounts.filter(a => a.ok === false || a.error);
    if (failed.length === accounts.length) return 'failed';
    return 'done';
  }
  if (c.done_today) return 'done';
  if (c.claimable) return 'pending';
  return 'unknown';
}

const CK_STATE_LABEL = {
  done: '已签到', pending: '待签到', unknown: '状态未知',
  inactive: '无活动', unavailable: '无可领奖励', failed: '领取失败',
};

/** 今日仍可领取的账号数（多账号通道按号算，而不是按通道算）。 */
function checkinPendingAccounts(p) {
  const c = p.checkin || {};
  const accounts = c.accounts || [];
  if (accounts.length) {
    return accounts.filter(a => a.claimable && !a.checked_in && a.ok !== false).length;
  }
  return (!c.done_today && c.claimable) ? 1 : 0;
}

// 打卡日历右侧明细当前选中的日期（YYYY-MM-DD）。放模块级而不是 DOM/闭包里：
// renderCheckin() 每 30s 整块重建，选中态要跨重绘存活。
// 声明放在 renderCheckin 之前：它在 renderCheckin 内部被读写，写在函数后面虽然
// 靠「调用发生在文件执行完之后」也能跑（不会踩 TDZ），但读起来像引用了未声明变量。
let CAL_SELECTED = null;

function renderCheckin(opts) {
  if (!BENEFITS) return;
  const quiet = !!(opts && opts.quiet);
  const page = document.getElementById('page-checkin');
  if (!page) return;

  // 自动打卡设置（元素在签到页，渲染前先同步值）
  const autoEl = document.getElementById('auto-checkin');
  if (autoEl) autoEl.checked = !!BENEFITS.auto_checkin;
  const timeEl = document.getElementById('checkin-time');
  if (timeEl) timeEl.value = BENEFITS.checkin_time || '09:30';
  const enabled = BENEFITS.checkin_enabled_providers || [];
  const hintEl = document.getElementById('checkin-hint');
  if (hintEl) {
    hintEl.textContent = enabled.length
      ? '每天自动为: ' + enabled.join(', ') : '暂无支持打卡的通道';
  }

  // 只列「支持签到且当前有活动」的通道（traeintl 无端点、codebuddyintl 档期
  // 未开时整行隐藏，理由见原实现）。
  const rows = (BENEFITS.providers || []).filter(p =>
    p.checkin.supported && !p.checkin.inactive);

  // ---- 今日概览 ----
  let pendingChannels = 0, pendingAccounts = 0, doneChannels = 0, dailyTotal = 0;
  rows.forEach(p => {
    const st = checkinChannelState(p);
    const n = checkinPendingAccounts(p);
    if (n > 0) { pendingChannels++; pendingAccounts += n; }
    else if (st === 'done') doneChannels++;
    if (p.checkin.daily_credit > 0 && !p.checkin.inactive) {
      dailyTotal += Number(p.checkin.daily_credit) || 0;
    }
  });
  const valueEl = document.getElementById('ck-hero-value');
  const subEl = document.getElementById('ck-hero-sub');
  if (valueEl) {
    valueEl.textContent = pendingAccounts > 0
      ? `${pendingAccounts} 个账号待领` : '今日已全部签到';
  }
  if (subEl) {
    subEl.textContent = pendingAccounts > 0
      ? `${pendingChannels} 个通道还有可领奖励，点右侧「一键领取」`
      : `共 ${rows.length} 个通道有签到活动${dailyTotal ? ` · 每日合计 +${fmtCredit(dailyTotal)}` : ''}`;
  }
  const kpis = document.getElementById('ck-kpis');
  if (kpis) {
    kpis.innerHTML = [
      ['通道', `${doneChannels}/${rows.length}`, '已完成'],
      ['待领', String(pendingAccounts), '账号'],
      ['每日', dailyTotal ? `+${fmtCredit(dailyTotal)}` : '—', '合计'],
    ].map(([k, v, u]) => `<div class="ck-kpi"><span class="ck-kpi-k">${esc(k)}</span>` +
      `<span class="ck-kpi-v">${esc(v)}</span><span class="ck-kpi-u">${esc(u)}</span></div>`).join('');
  }
  const allBtn = document.getElementById('ck-claim-all');
  if (allBtn && !allBtn.dataset.busy) {
    allBtn.disabled = pendingAccounts === 0;
    allBtn.textContent = pendingAccounts === 0 ? '今日已完成' : '一键领取';
  }

  // ---- 通道卡片网格 ----
  const grid = document.getElementById('checkin-rows');
  if (grid) {
    grid.innerHTML = rows.length ? rows.map(p => checkinCardHtml(p)).join('')
      : '<div class="empty" style="grid-column:1/-1;padding:14px 0">当前没有支持打卡的通道</div>';
  }
  syncNextTimeAuto();

  // ---- 打卡日历 ----
  const cal = document.getElementById('cal');
  if (cal) {
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
    // ---- 右侧「当天明细」：默认选中今天，点格子换日期 ----
    // 整页每 30s 重建 innerHTML，选中的日期存在 CAL_SELECTED 里（不是在 DOM
    // 上），重绘后仍在同一天上，不会把用户选好的日子弹回今天。
    window._calDays = days;
    window._calDowOf = dowOf;
    window._calToday = todayStr;
    const hasSel = days.some(d => d.date === CAL_SELECTED);
    if (!hasSel) CAL_SELECTED = todayStr;
    renderCalDetail();
    cal.querySelectorAll('.day[data-date]').forEach(el => {
      el.classList.toggle('sel', el.dataset.date === CAL_SELECTED);
    });
  }

/** 右侧「当天明细」：选中日签了哪些通道、各领多少积分、合计多少。 */
function renderCalDetail() {
  const box = document.getElementById('cal-detail');
  if (!box) return;
  const days = window._calDays || [];
  const todayStr = window._calToday || '';
  const dowOf = window._calDowOf || (() => '');
  const date = CAL_SELECTED;
  const d = days.find(x => x.date === date);
  const head = `<div class="ck-cal-d-head">
      <span class="ck-cal-d-date mono">${esc(date || '—')}</span>
      <span class="muted">${esc(date ? dowOf(date) : '')}${date === todayStr ? ' · 今天' : ''}</span>
    </div>`;
  if (!d) {
    box.innerHTML = head + `<div class="empty" style="padding:16px 0">选一天看当天的签到明细</div>`;
    return;
  }
  const providers = d.providers || [];
  const credits = d.credits || {};
  // 合计只累加**真实存在的**金额。`credits` 为空 / 缺某个通道时不能当 0 算：
  // 上游签到历史合并进来的日子根本没有本地领取记录，显示「+0」会让人以为当天
  // 真的只领到 0（与上面那些 `—` 行自相矛盾）。一个金额都没有时合计也显示 `—`。
  let total = 0;
  let hasAmount = false;
  for (const pid of providers) {
    const raw = credits[pid];
    if (raw == null) continue;
    const v = Number(raw);
    if (!Number.isFinite(v)) continue;
    total += v;
    hasAmount = true;
  }
  if (!providers.length) {
    box.innerHTML = head +
      `<div class="empty" style="padding:16px 0">当天未打卡${date === todayStr ? '，点上方「一键领取」' : ''}</div>`;
    return;
  }
  const rows = providers.map(pid => {
    const credit = fmtCredit(credits[pid]);
    return `<div class="ck-cal-d-row">
      <span class="ck-cal-d-name"><i style="background:${pcolor(pid)}"></i>${esc(pid)}</span>
      <span class="mono ${credit != null ? '' : 'muted'}"
            ${credit != null ? '' : 'title="上游签到历史合并进来的日子没有本地领取金额记录"'}>${credit != null ? '+' + esc(credit) : '—'}</span>
    </div>`;
  }).join('');
  const totalTxt = hasAmount
    ? `<b class="mono">+${esc(fmtCredit(total))}</b>`
    : `<b class="mono muted" title="当天有签到记录，但没有本地领取金额（上游历史合并进来的日子）">—</b>`;
  box.innerHTML = head + `<div class="ck-cal-d-list">${rows}</div>` +
    `<div class="ck-cal-d-foot"><span class="muted">${providers.length} 个通道</span>` +
    `<span class="grow"></span><span>当日合计 ${totalTxt}</span></div>`;
}

/** 点日历格子：锁定选中日期并刷新右侧明细（tooltip 仍可悬停速览）。 */
function selectCalDay(date) {
  if (!date) return;
  CAL_SELECTED = date;
  const cal = document.getElementById('cal');
  if (cal) {
    cal.querySelectorAll('.day[data-date]').forEach(el => {
      el.classList.toggle('sel', el.dataset.date === date);
    });
  }
  renderCalDetail();
}

// 到期/余额告警横幅挂在额度页，这里不渲染（避免两页各挂一份）
  if (typeof refreshCheckinBadge === 'function') refreshCheckinBadge();
  if (quiet) return;
}

/** 单通道卡片：状态一眼可辨，账号胶囊在中间，操作沉底。 */
function checkinCardHtml(p) {
  const c = p.checkin || {};
  const state = checkinChannelState(p);
  const label = CK_STATE_LABEL[state] || '状态未知';
  const accounts = c.accounts || [];
  const pending = checkinPendingAccounts(p);

  // 账号胶囊：状态由配色 + 字形表达（✓ 已签 / ＋ 可领 / ✕ 失败）
  const pills = accounts.map(a => {
    const st = checkinAccountState(a);
    const name = a.name || ('#' + a.index);
    const tip = [name, st.label, a.error || a.message].filter(Boolean).join(' · ');
    return `<span class="ck-pill${st.cls ? ' ' + st.cls : ''}" title="${esc(tip)}">` +
      `<span class="sr-only">${esc(st.label)}</span>` +
      `<span class="ck-pill-name">${esc(name)}</span>` +
      `${a.id ? acctRenameButton(p.id, a) : ''}</span>`;
  }).join('');

  const reward = (c.daily_credit > 0 && !c.inactive)
    ? `每日 +${fmtCredit(c.daily_credit)}${accounts.length > 1 ? `（${accounts.length} 账号）` : ''}`
    : '';
  const streak = c.streak_days >= 2 ? `连续 ${c.streak_days} 天` : '';

  // 明细折叠状态存 CHECKIN_DETAIL_FOLD：整页 30s 重建，挂 DOM 会被刷回去
  const multi = accounts.length > 1;
  const open = multi && !!CHECKIN_DETAIL_FOLD.open[p.id];
  const toggle = multi
    ? `<button class="ghost ck-detail-toggle" data-checkin-fold="${esc(p.id)}" ` +
      `aria-expanded="${open}" aria-controls="checkin-details-${esc(p.id)}" ` +
      `onclick="checkinDetailToggle(this)">${open ? '收起账号 ▴' : '账号明细 ▾'}</button>` : '';
  const details = multi
    ? `<div class="ck-account-grid" id="checkin-details-${esc(p.id)}"${open ? '' : ' hidden'}>` +
      checkinAccountDetails(c) + '</div>' : '';

  const unknown = state === 'unknown';
  const disabled = unknown || pending === 0;

  return `<article class="ck-card ck-${state}">
    <div class="ck-card-top">
      <span class="ck-badge" style="--c:${pcolor(p.id)}">${esc((p.name || p.id).slice(0, 1).toUpperCase())}</span>
      <div class="ck-card-title">
        <h3>${esc(p.name || p.id)}</h3>
        <div class="ck-card-meta">${reward ? `<span>${esc(reward)}</span>` : ''}${streak ? `<span>${esc(streak)}</span>` : ''}</div>
      </div>
      <span class="ck-status ck-${state}"${(c.error || c.message) ? ` title="${esc(c.error || c.message)}"` : ''}>${esc(label)}</span>
    </div>
    ${pills ? `<div class="ck-card-accounts">${pills}</div>` : ''}
    <div class="ck-card-foot">
      ${nextTimeHtml(c) || '<span class="ck-next-empty">—</span>'}
      <div class="ck-actions">
        ${toggle}
        <button class="ghost" onclick="refreshCheckin('${esc(p.id)}', this)"
                title="绕过缓存，真打上游重查该通道签到状态">刷新</button>
        <button class="primary" ${disabled ? 'disabled' : ''}
                onclick="claimNow('${esc(p.id)}', this)"
                ${unknown ? 'title="状态未知，先点「刷新」重查"' : ''}>
          ${pending > 0 ? `打卡${pending > 1 ? ` (${pending})` : ''}` : '已签'}</button>
      </div>
    </div>
    ${details}
  </article>`;
}

function renderBenefits(opts) {
  if (!BENEFITS) return;
  const quiet = !!(opts && opts.quiet);
  renderExpiryBanner();

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
    const fold = quotaProviderFold(q.items || [], p.id);
    return `<div class="chart-card" style="margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
        <span style="font-weight:600">${esc(p.name)}</span>
        ${q.level ? `<span class="tag">${esc(q.level)}</span>` : ''}${fold.badge}
        ${headSum}<span style="flex:1"></span>${fold.toggle}${providerRefreshButton(p.id)}
      </div>${fold.bodyStart}${items || '<div class="empty" style="padding:12px 0">无额度数据</div>'}${fold.bodyEnd}</div>`;
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

// 签到页签角标（「✓ 已签」/「待签 N」）。按**账号**算，不是按通道算：
// 多账号通道里 4 个号只差 1 个时，按通道算会显示「未签 1」，用户点进去却看到
// 一整行账号、不知道该领谁；按账号算与「一键领取」实际会打的数量一致。
function refreshCheckinBadge() {
  if (!BENEFITS) return;
  const el = document.getElementById('cnt-checkin');
  if (!el) return;
  const ck = (BENEFITS.providers || []).filter(p => p.checkin.supported);
  if (!ck.length) { el.textContent = ''; el.className = 'cnt'; return; }
  const pending = ck.filter(p => !p.checkin.inactive)
    .reduce((n, p) => n + checkinPendingAccounts(p), 0);
  if (!pending) { el.textContent = '✓ 已签'; el.className = 'cnt ok'; }
  else { el.textContent = `待签 ${pending}`; el.className = 'cnt warn'; }
}

// 额度页签角标：只关心「有没有通道有额度」，不再兼职签到角标（拆页后各算各的）
function refreshBenefitsBadge() {
  if (!BENEFITS) return;
  const el = document.getElementById('cnt-benefits');
  if (!el) return;
  const q = (BENEFITS.providers || []).filter(p => p.quota && p.quota.supported);
  el.textContent = q.length ? String(q.length) : '';
  el.className = 'cnt';
}

// 「一键领取」：把所有可领账号的通道依次打完。
// 后端 claim 已按通道聚合（多账号通道内部会先探状态、只领该领的），所以这里
// 按通道串行调用即可；单个通道失败不影响后续通道，最后统一报结果。
// 与逐卡按钮共用 claimNow 的后端路径，不另开接口。
async function claimAllCheckin(btn) {
  const rows = (BENEFITS && BENEFITS.providers || []).filter(p =>
    p.checkin && p.checkin.supported && !p.checkin.inactive
    && checkinPendingAccounts(p) > 0);
  if (!rows.length) { toast('今天已经没有可领的签到了'); return; }
  if (btn) { btn.dataset.busy = '1'; btn.disabled = true; btn.textContent = '领取中…'; }
  const done = [], failed = [];
  try {
    for (const p of rows) {
      try {
        const r = await api('/ui/api/checkin', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({provider: p.id})});
        if (r && r.ok) done.push(p.id);
        else failed.push(`${p.id} ${(r && (r.message || r.error)) || '失败'}`);
      } catch (e) {
        failed.push(`${p.id} ${e.message}`);
      }
    }
  } finally {
    if (btn) { delete btn.dataset.busy; btn.disabled = false; btn.textContent = '一键领取'; }
  }
  if (failed.length) toast(`✓ ${done.length} 个通道已领；失败：${failed.join('、')}`, true);
  else toast(`✓ 已领取 ${done.length} 个通道`);
  // 领完只重拉快照并重绘签到页，不整页重建（额度面板/账号面板都不必跟着动）
  await refreshCheckinOnly();
}

/** 重新拉 /ui/api/benefits 并只重绘签到页（打卡/刷新后的统一收尾）。 */
async function refreshCheckinOnly() {
  try {
    const snap = await api('/ui/api/benefits');
    if (snap && Array.isArray(snap.providers)) {
      BENEFITS = snap;
      if (typeof renderCheckin === 'function') renderCheckin({quiet: true});
      if (typeof refreshCheckinBadge === 'function') refreshCheckinBadge();
    }
  } catch (e) { /* 静默：下一轮轮询会补上 */ }
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
