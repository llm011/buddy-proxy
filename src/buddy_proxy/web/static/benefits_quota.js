// 额度条目的标题汇总、单条 HTML 与折叠（PAT 面板与额度列表共用）。
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
  const percentOnly = it.unit === 'percent' && hasBar;
  let rem = it.remaining;
  if (rem == null && it.used != null && it.total != null) rem = it.total - it.used;
  // Codex /wham/usage 只给已用百分比，不给绝对 used/total；这种形状仍然有完整
  // 水位语义。缺 remaining 时可由 100-pct 推出，不能显示「已用未知」。
  if (rem == null && percentOnly) rem = 100 - pct;
  // 只有「分子分母都拿到」才算有绝对量：只见 total 时（例如额度网关只回包容量、
  // 还没回用量）下面的 ``percent`` 实际是 0，报出来就是「剩 300 / 300 · 已用
  // 0%」这种把未知当零的假象。百分比窗口是例外，它不需要绝对分母。
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
    ? (percentOnly
        ? `<span class="mono"><span style="color:var(--text)">剩 ${fmtNum(rem)}%</span> · 已用 ${pct}%</span>`
        : (hasVolume
            ? `<span class="mono"><span style="color:var(--text)">剩 ${fmtNum(rem)}</span> / ${fmtNum(it.total)} · 已用 ${pct}%</span>`
            : `<span class="mono"><span style="color:var(--text)">剩 ${fmtNum(rem)}</span> · 已用未知</span>`))
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
  const bar = (hasVolume || percentOnly) && hasBar ? `<div class="qbar"><div style="width:${pct > 0 ? Math.max(2, pct) : 0}%;background:${color}"></div></div>` : '';
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
