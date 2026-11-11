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
//
// quotaSumOf / quotaHeadSumHtml 拆成纯函数（不碰 DOM）：「单个资源包合计」与
// 「跨账号合计」（quotaAccountTotal）共用同一套「哪些条目算数」的判断——量纲
// 口径只准有一处定义，否则两处迟早分叉，标题行的数就跟子卡对不上。
function quotaHeadSum(q) {
  return quotaHeadSumHtml(quotaSumOf(q.items || [], !!q.sum_items));
}

// 到期日「MM-DD」，**不在今年的补上年份**（``2027-10-27``）。
//
// 到期是绝对时点，省掉年份就会与同月日的往年/明年撞形：用户看到资源包写
// 「10/27 到期」时今天是 11-05，会直接当成已过期，而它实际是明年才到期
// （CodeBuddy 的裂变包里有有效期一年的条目，正是这么撞上的）。
//
// 重置时间（``fmtReset``）**保持只显示月日**：它是按周期滚动的相对时点，
// 同一个「10-30 重置」月月复用，补年份反而噪声。
//
// 不直接复用 ``benefits.js`` 的 ``fmtExpireDate``：本文件先于它加载
// （index.html 里 benefits_quota.js 在 benefits.js 之前），运行时函数虽然
// 存在、但跨文件依赖加载顺序太脆，两处各留一份小函数更稳。
function fmtExpireMmdd(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const md = `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  return d.getFullYear() === new Date().getFullYear() ? md : `${d.getFullYear()}-${md}`;
}

// 一组条目 → 「本组合计」的原始数值。抽出来是因为跨账号合计要的是**数**而
// 不是 HTML，但「怎么取」必须与标题行完全一致。
//
// - sumItems 为真：所有有数的条目相加，返回 ``{rem, total, n}``（n=参与条数）
// - 为假：只取第一条有数的条目（Trae 的「总额度」就是这样拿到手的）
// - 一条可用的都没有：返回 ``null``，调用方据此不渲染（否则「剩 0 / 0」）
//
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
function quotaSumOf(items, sumItems) {
  const num = v => v !== null && v !== '' && typeof v !== 'boolean'
                && Number.isFinite(Number(v));
  // 重置券（unit=reset_credit）是**张数说明**不是额度水位：后端给的
  // reset_credits 张数会被 num() 当成数字收进来（title 也明说「仅显示、不计入
  // 合计」）。谁第一个撞上就能整组算错——Codex 多账号时，只有券的账号会顶掉
  // 真正那张窗口的合计。
  const usable = items.filter(it =>
    !(it.unit === 'reset_credit' || it.reset_credit === true)
    && num(it.remaining) && num(it.total));
  if (!usable.length) return null;
  if (sumItems) {
    return {
      rem: usable.reduce((a, it) => a + Number(it.remaining), 0),
      total: usable.reduce((a, it) => a + Number(it.total), 0),
      n: usable.length,
    };
  }
  return {rem: Number(usable[0].remaining), total: Number(usable[0].total), n: 1};
}

// quotaSumOf 的结果 → 标题行「剩 X / Y · 已用 N%」chip。
// 输出与拆分之前逐字节一致（tests/test_quota_headline_ui.py 在盯）。
function quotaHeadSumHtml(sum) {
  if (!sum) return '';
  if (sum.n > 1) {
    const label = `${sum.n} 项合计`;
    const used = sum.total > 0 ? ` · 已用 ${Math.round((1 - sum.rem / sum.total) * 100)}%` : '';
    return `<span class="mono" style="margin-left:auto" title="${esc(label)}">
      <span style="color:var(--ok);font-weight:600">剩 ${fmtNum(sum.rem)}</span>
      <span class="muted">/ ${fmtNum(sum.total)}${used}</span>
      <span class="muted" style="font-size:11px"> · ${esc(label)}</span></span>`;
  }
  // 单条：**列表顺序就是语义**——各家自己决定先展示哪一档（ZCode 是「窗口由
  // 小到大」，5 小时档在前；Trae 是总额度在前），前端不再自作主张挑「最紧的」。
  // 「已用 N%」放在总量后面（用户 2026-10-03：参考明细行的写法）——这是单条
  // 自己的比例，永远成立。
  const hUsed = sum.total > 0 ? ` · 已用 ${Math.round((1 - sum.rem / sum.total) * 100)}%` : '';
  return `<span class="mono" style="margin-left:auto">
      <span style="color:var(--ok);font-weight:600">剩 ${fmtNum(sum.rem)}</span>
      <span class="muted">/ ${fmtNum(sum.total)}${hUsed}</span></span>`;
}

// 「剩 N」的统一渲染：数字用**成功色**高亮加粗。
//
// 用户 2026-11-11 要求：所有通道的剩余量都要像 zcode 那样绿色高亮。之前这里
// 用 `color:var(--text)`（普通正文色），和「/ 总量 · 已用 N%」的次要信息混在
// 一起，一眼看不出哪个是重点。
//
// 颜色按水位分档，与进度条同语义：>15% 绿、≤15% 黄、≤0 红。没有分母的通道
// （DeepSeek 钱包、qwenpc 个人版）拿不到占比，一律按绿色处理——它们只有绝对
// 量，没水位可言，不该因为「无法判断」就退回灰字。
function remainSpan(text, pct) {
  let color = 'var(--ok)';
  if (typeof pct === 'number' && Number.isFinite(pct)) {
    color = pct >= 85 ? 'var(--err)' : pct >= 60 ? 'var(--warn)' : 'var(--ok)';
  }
  return `<span style="color:${color};font-weight:600">剩 ${text}</span>`;
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
  // 预付费余额（unit=currency，如 DeepSeek 官方）：上游给的是「还能花多少钱」，
  // 没有总量分母，所以既不是百分比水位、也没有「已用 N%」可算。单独一条分支，
  // 免得掉进下面「剩 X · 已用未知」——那句话在这种形态下是误导（用户会以为
  // 少了一个分母，实际是本来就没有）。
  //
  // 必须要求**真是数字**：余额取不到时后端发的是字符串说明文案（「余额未知」），
  // 而 hasNums 只判 ``rem != null``，字符串会照过——不卡这一道，说明条会被
  // 塞进数字位、渲染成大字「余额未知（USD） 可用」（与 quotaHeadSum 剔除
  // 字符串说明条是同一条来由）。
  const currencyOnly = it.unit === 'currency' && hasNums
    && typeof rem !== 'boolean' && rem !== '' && Number.isFinite(Number(rem));
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
  const fmtExpire = fmtExpireMmdd(it.expire_ts);
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
  const nums = currencyOnly
    ? `<span class="mono"><span style="color:var(--ok);font-weight:600">${fmtNum(rem)}</span> <span class="muted">可用</span></span>`
    : (hasNums
    ? (percentOnly
        ? `<span class="mono">${remainSpan(`${fmtNum(rem)}%`, pct)} · 已用 ${pct}%</span>`
        : (hasVolume
            ? `<span class="mono">${remainSpan(fmtNum(rem), pct)} / ${fmtNum(it.total)} · 已用 ${pct}%</span>`
            : `<span class="mono">${remainSpan(fmtNum(rem), hasVolume ? pct : null)} · 已用未知</span>`))
    : (pending
        ? `<span class="mono muted" title="standard 池无主动查询接口，用量仅在账号撞 4031 时被动采集；重置后成功请求不带账单事件，故用量待下次撞码确认">已于 ${fmtReset || '—'} 重置 · 用量待确认</span>`
        : (notice
            ? `<span class="mono" style="color:${noticeColor}">${esc(noticeText)}</span>`
            : (it.total != null
                // 总额已知、用量未知（如海外 Trae 的次数制：上游不给已用
                // 次数）——明说「剩 — / N · 已用未知」，别兜底成空 span
                //（那行只剩 label + 到期日，看不出是没用量还是没渲染出来）
                ? `<span class="mono"><span style="color:var(--muted)">剩 —</span> / ${fmtNum(it.total)} · 已用未知</span>`
                : '<span class="mono"></span>'))));
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
  const r = quotaRemaining(it);
  return r != null && r <= 0;
}

// 数值余量的唯一判读口径：null / 说明文字 / 空串都不是 0；百分比窗口可由
// 100-percent 推出。非法 total/used 也不允许经 Number(null) 伪装成有数据。
function quotaRemaining(it) {
  const numeric = v => v != null && v !== '' && typeof v !== 'boolean' && Number.isFinite(Number(v));
  let r = it.remaining;
  if (('used' in it && !numeric(it.used)) || ('total' in it && !numeric(it.total))) return null;
  if (r == null && numeric(it.used) && numeric(it.total)) r = Number(it.total) - Number(it.used);
  if (r == null && it.unit === 'percent' && it.percent != null
      && Number.isFinite(Number(it.percent))) r = 100 - Number(it.percent);
  if (r == null || r === '' || typeof r === 'boolean' || !Number.isFinite(Number(r))) return null;
  return Number(r);
}

// ---- provider 级折叠：没额度的通道都收起来 ----
// 与条目层 QUOTA_FOLD 完全独立。收起的两种情形：
//
// 1. **明确全部耗尽**：每条可量化的剩余都 ≤ 0（重置券不计）
// 2. **压根没有额度明细**：items 为空、或每组只有说明条（未登录 / 查询失败 /
//    Free 的 query_empty），一张带可量化剩余的行都画不出来。这种卡展开只剩一句
//    「暂无账号额度数据」，占满一屏却没内容（用户 2026-11-02 报障 Codex 账号
//    登录着、卡里什么额度都没有还大敞着）。
//
// **一组有余额 + 另一组全失败**时照旧展开：失败那组要给用户看见。所以面板传进来
// 的 items 由「渲染出来的东西」决定（见各 renderXxxPanel），不是原样透传。
// 重置券既不算耗尽、也不算「有额度」——只有券的通道同样收起。
// 收起后标题行留徽标与展开开关，点一下就能看回去。
const PROVIDER_QUOTA_FOLD = { open: {} };

function quotaProviderExhausted(items) {
  for (const it of items || []) {
    if (it.unit === 'reset_credit' || it.reset_credit === true) continue;
    const remaining = quotaRemaining(it);
    if (remaining == null) continue;   // 说明条 / 未知：不参与耗尽判定
    if (remaining > 0) return false;
  }
  // 没有任何一条可量化的剩余 > 0：全部耗尽，或压根没有额度明细
  return true;
}

// 普通卡和专属卡共用的外层折叠。有余额时永远展开，不让旧的「手动收起」
// 覆盖值把新到账的额度藏住；只有耗尽卡才显示开关并记住用户选择。
// items 传 'empty'（或 null）表示「面板压根画不出带额度的行」：没有可量化的
// 剩余就收起。徽标写「无额度数据」而不是「无剩余额度」——后者是查到了、且确实
// 是 0，前者是查不到。别的字符串（非数组）按「无从判断」处理，保持展开。
function quotaProviderFold(items, key) {
  const exhausted = items === 'empty' || quotaProviderExhausted(items);
  const empty = items === 'empty';
  const overridden = Object.prototype.hasOwnProperty.call(PROVIDER_QUOTA_FOLD.open, key);
  const open = !exhausted || (overridden ? PROVIDER_QUOTA_FOLD.open[key] : false);
  const id = 'provider-quota-' + key.replace(/[^A-Za-z0-9_-]/g, (m, i) =>
    '-' + key.charCodeAt(i).toString(16) + '-');
  return {
    exhausted,
    badge: exhausted ? '<span class="tag ' + (empty ? '' : 'bad') + '">'
      + (empty ? '无额度数据' : '无剩余额度') + '</span>' : '',
    toggle: exhausted ? `<button class="ghost provider-quota-toggle" data-provider-fold="${esc(key)}" ` +
      `aria-expanded="${open}" aria-controls="${esc(id)}" title="${open ? '收起' : '展开'}额度明细" ` +
      `onclick="quotaProviderToggle(this)">${open ? '收起额度 ▴' : '展开额度 ▾'}</button>` : '',
    bodyStart: `<div class="provider-quota-body" id="${esc(id)}"${open ? '' : ' hidden'}>`,
    bodyEnd: '</div>',
  };
}

function quotaProviderToggle(btn) {
  const key = btn.dataset.providerFold;
  PROVIDER_QUOTA_FOLD.open[key] = btn.getAttribute('aria-expanded') !== 'true';
  const body = document.getElementById(btn.getAttribute('aria-controls'));
  const open = PROVIDER_QUOTA_FOLD.open[key];
  if (body) body.hidden = !open;
  btn.setAttribute('aria-expanded', String(open));
  btn.title = (open ? '收起' : '展开') + '额度明细';
  btn.textContent = open ? '收起额度 ▴' : '展开额度 ▾';
}

// 展开/收起按钮。**必须**紧跟 .qbody 渲染（syncQuotaFold 靠
// ``box.nextElementSibling`` 找它）——早先它排在 .qbody 后面、但前面又插了
// 一块 .qspent「已用完 N 项」，于是 syncQuotaFold 把 .qspent 当成了按钮，
// 按钮的 hidden 永远摘不掉：展开后既没有「收起」可点，也再没有「展开」
// （用户 2026-11-01 报障：「全部积分包展开后收不回去」）。
//
// **一枚就够，不做第二枚浮钮。** 早先展开态里还在视口底沿浮了一枚同样的
// 「收起」（绝对定位），想接住被 ``overflow-y:auto`` 推出滚动视口的主钮。代价
// 是它和主钮同文案、同状态，一屏里出现两枚一模一样的按钮，用户三次报障都
// 出在这条上（收起态双按钮 → 铺成整页通栏 → 展开态双按钮）。主钮本身就在
// 明细末尾，滚一下就能点到；那点便利不值这一堆坑。彻底去掉浮钮后只剩主钮
// 一枚，展开/收起各一个，状态由 syncQuotaFold 单独驱动。
function quotaMoreButton(key, cls, attrs) {
  return `<button class="${cls}" data-qfold="${esc(key)}" ${attrs}` +
    ` onclick="quotaFoldToggle(this)" hidden></button>`;
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
  // 已用完区块放进 .qbody **里面**：它随展开态一起显示，且不会再横在
  // .qbody 和按钮中间（那段历史正是上面那个 bug 的成因）
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
    quotaMoreButton(
      key, 'qmore',
      `data-act="${act.length}" data-spent="${spent.length}" data-total="${shown.length}" `,
    );
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
    // 按钮只认「紧跟着 .qbody 的 .qmore」——不能拿 ``nextElementSibling`` 直接
    // 当按钮用：那处在别的通道里可能是普通内容块，会把「找不到按钮」误判成
    // 「按钮已处理」。
    const first = box.nextElementSibling;
    const main = first && first.classList && first.classList.contains('qmore') ? first : null;
    // 展开后总是留个「收起」入口；收起时只有真被裁了、或底下还压着已用完的
    // 条目，才值得给按钮——否则是个点了没有任何变化的假按钮
    const show = open || clipped || !!spent;
    const label = open ? '收起 ▴'
      : (!Number(main && main.dataset.act) ? `展开 ${main ? main.dataset.spent : 0} 项已用完 ▾`
        : `展开全部 ${main.dataset.total} 项 ▾`);
    if (main) { main.hidden = !show; main.textContent = label; }
  });
}

function quotaFoldToggle(btn) {
  const key = btn.dataset.qfold;
  QUOTA_FOLD.open[key] = !QUOTA_FOLD.open[key];
  syncQuotaFold();
}

// ---- 多账号通道：面板标题行的「跨账号合计」----
//
// 子卡上的合计行（quotaHeadSum）只算**一个账号**里的几个资源包。多账号通道里
// 用户真正想知道的是「这个通道总共还剩多少」，原先只能自己把几张卡加起来
// （用户 2026-11-02 提出）。
//
// 三条口径，缺一条就会显示成假数字：
//
// 1. **按区分别累加。** CN 与 Global 是不同面板 / 不同 provider（额度标签就不同：
//    ``CodeBuddy`` vs ``CodeBuddy 海外版``；Trae 的 CN 记 credits、海外记 dollars），
//    各自算各自的。合起来是个既不可兑付、又没有量纲的数字——所以跨区**绝不**由
//    这里合并；哪怕哪天两个区挤进同一个面板，下面按 unit 分桶也会拆成两个 chip。
//
// 2. **取数口径跟子卡一致**：走 ``quotaSumOf``。``opts.sumItems`` 由面板按通道
//    声明——Qoder / CodeBuddy 系列的条目是并存份额要相加；Trae / traepat /
//    Antigravity 传假，按「取第一条有数的」处理（Trae 的「总额度」本就是各权益
//    包的合计，再加一遍就是重复计算）。
//
// 3. **不可加的就明确说没加。** 上游只给比例（``percent``，没有绝对积分）的账号
//    直接跳过，被跳过的个数写进 chip（「2 账号合计 · 1 个未计入」），账号名进
//    悬停提示。宁可显示一个标注过的不完整总数，也不许拿百分比反推绝对量拼出
//    一个看起来完整的假数。
//
// 返回 chip HTML；不该显示时返回 ``''``：分组不足两个（单账号——子卡上已经有
// 一行了，标题再写一遍没有信息量），或一个可加的账号都没有。
function quotaAccountTotal(groups, opts) {
  const sumItems = !!(opts && opts.sumItems);
  if (!(groups instanceof Map) || groups.size <= 1) return '';
  // 千分制 / 美元不是积分：Antigravity 的 total 恒为 1000，加出来的「剩 1,700」
  // 会被当成积分数。这类量纲在 chip 里点名，免得和 credits 混为一谈。
  const UNIT_LABEL = {permille: '千分制水位', dollar: '美元'};
  // unit → {rem, total, counted, skipped: [组名]}
  const buckets = new Map();
  const bucket = unit => {
    if (!buckets.has(unit)) buckets.set(unit, {unit, rem: 0, total: 0, counted: 0, skipped: []});
    return buckets.get(unit);
  };
  for (const [name, items] of groups) {
    const list = items || [];
    // 没有额度数字的分组（未登录、查询失败）不算「跳过」：它压根没数据可比，
    // 标进「N 个未计入」反而让合计看起来比实际情况更可疑。
    const noticeOnly = list.length > 0 && list.every(
      it => it.unreachable === true || it.query_failed === true
        || (it.unit !== 'percent' && !quotaSumOf([it], false)));
    if (noticeOnly) continue;
    // 整组都只有比例 → 没有绝对量可加（**先判 percent**：它的 total 是 100
    // 这种占位分母，``quotaSumOf`` 照样能得出数，但那不是积分）。
    // 券行不进量纲集合：它没有单位含义，混进来会把整组打成 credit。
    const units = [...new Set(list
      .filter(it => !(it.unit === 'reset_credit' || it.reset_credit === true))
      .map(it => it.unit || 'credit'))];
    if (!units.length) continue;   // 只剩券行：没额度可加，也不算「跳过」
    const unit = units.every(u => u === 'percent') ? 'percent'
      : (units.length === 1 ? units[0] : 'credit');
    const sum = unit === 'percent' ? null : quotaSumOf(list, sumItems);
    if (!sum) { bucket(unit).skipped.push(name); continue; }
    const b = bucket(unit);
    b.rem += sum.rem; b.total += sum.total; b.counted += 1;
  }
  const chips = [];
  // 整组都不可加的账号（如只给百分比）：挂到「算了的」那份上一起标出来。
  // 不标的话它们就悄悄消失在合计里——用户看到的总数少了几个账号还不知道。
  // 顺序有讲究：先扫一遍收齐 dropped，再一次性拼 chip，中途不去碰 dropped。
  let dropped = 0, droppedNames = [];
  for (const b of buckets.values()) {
    if (b.counted) continue;
    dropped += b.skipped.length;
    droppedNames = droppedNames.concat(b.skipped);
  }
  for (const b of buckets.values()) {
    if (!b.counted) continue;
    const parts = [`${b.counted} 账号合计`];
    const skipped = b.skipped.length + dropped;
    if (skipped) parts.push(`${skipped} 个未计入`);
    const used = b.total > 0 ? ` · 已用 ${Math.round((1 - b.rem / b.total) * 100)}%` : '';
    const label = `${parts.join(' · ')}${UNIT_LABEL[b.unit] ? ` · ${UNIT_LABEL[b.unit]}` : ''}`;
    const tip = `跨账号合计：各账号子卡合计相加`
      + (skipped ? `；未计入 ${skipped} 个：${b.skipped.concat(droppedNames).join('、')}` : '');
    chips.push(`<span class="mono" title="${esc(tip)}">
        <span style="color:var(--ok);font-weight:600">剩 ${fmtNum(b.rem)}</span>
        <span class="muted">/ ${fmtNum(b.total)}${used}</span>
        <span class="muted" style="font-size:11px"> · ${esc(label)}</span></span>`);
  }
  return chips.join('');
}

