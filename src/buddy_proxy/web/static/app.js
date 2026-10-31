// ---- 数据加载：按需拉取 ----
// 四个数据集各有归属页，惰性加载：
//   overview/benefits  轻量，首屏 + 轮询常驻（顶栏状态与页签角标要用）
//   stats              各页的统计区块与筛选候选，见 dataForTab()
//   models             模型页，首次进入时拉
//   logs               请求日志页，仅在日志页可见时拉
// 每个 loader 并发去重（同键进行中的 Promise 复用），避免重复请求。
const LOADED = { overview: false, benefits: false, stats: false, models: false, order: false };
const INFLIGHT = {};
// 每次 force 递增：标记出「force 之前发起」的请求。它们的响应可能早于本地写操作，
// 一律丢弃（见 markStale / ensureData），否则写操作后会把写前快照当最新数据渲染。
const GEN = { overview: 0, benefits: 0, stats: 0, models: 0, order: 0 };
// loadData 序号：并发/重叠的 loadData 只有最后发起的那次负责 render()，
// 避免「早发起、晚返回」的旧数据覆盖新统计。
let LOAD_SEQ = 0;
function markStale(key) {
  if (INFLIGHT[key]) INFLIGHT[key].stale = true;  // 在飞 → 作废其结果
  delete LOADED[key];  // 即使没有在飞请求（下次进入该页要重取），也别让旧缓存继续挡路
}
const LOADERS = {
  overview: () => api('/ui/api/overview').then(v => { OVERVIEW = v; LOADED.overview = true; }),
  benefits: () => api('/ui/api/benefits').then(v => { BENEFITS = v; LOADED.benefits = true; }),
  stats:    () => api('/ui/api/stats').then(v => { STATS = v; LOADED.stats = true; }),
  models:   () => api('/ui/api/models').then(v => { MODELS = v; LOADED.models = true; }),
  order:    () => api('/ui/api/model-order').then(v => { ORDER = v; LOADED.order = true; }),
};

function ensureData(key, force) {
  if (!force && LOADED[key]) return Promise.resolve();
  if (force) {
    GEN[key]++;      // 调用方都在 force 之前捕获了 gen，故本次强制的返回仍算数
    markStale(key);
  }
  if (INFLIGHT[key]) {
    // force 撞上在飞请求：在飞的那发是「force 之前」发起的，可能带着写操作
    // 之前的旧数据（写操作 → 刷新 → 旧在飞返回 → 被世代守卫作废 → 本次刷新
    // 白跑，界面停留旧值直到下轮轮询——顺位调整后面板不换位即此）。等它落定
    // 再补一发真正的重取，本次刷新的语义才算兑现。非 force 的去重不受影响。
    if (!force) return INFLIGHT[key];
    return INFLIGHT[key].catch(() => {}).then(() => ensureData(key, true));
  }
  const gen = GEN[key];
  const p = LOADERS[key]().finally(() => { delete INFLIGHT[key]; });
  // 结果已被更新的 force 取代 → 不渲染、不标记 LOADED，让后续按需重取
  if (key === 'overview') p.then(v => { if (gen !== GEN[key]) OVERVIEW = null; });
  if (key === 'benefits') p.then(v => { if (gen !== GEN[key]) BENEFITS = null; });
  if (key === 'stats') p.then(v => { if (gen !== GEN[key]) STATS = null; });
  if (key === 'models') p.then(v => { if (gen !== GEN[key]) MODELS = null; });
  if (key === 'order') p.then(v => { if (gen !== GEN[key]) ORDER = null; });
  p.then(() => { if (gen !== GEN[key]) delete LOADED[key]; });
  INFLIGHT[key] = p;
  return p;
}

// 强制重取时作废日志页的已渲染标记：新请求可能已产生新日志行，
// 否则 renderRecent() 会因 qs 相同而跳过，导致刷新后看不到最新记录。
function invalidateRecent() {
  RECENT_QS = null;
  MIX_QS = null;  // 头部饼图同理：强制刷新要看到新请求带来的变化
  // 只作废日志接口自己的去重条目；整表 clear() 会把 overview/benefits/models
  // 正在飞的条目一起丢掉，之后平白多打一轮请求。
  for (const k of [...GET_INFLIGHT.keys()])
    if (k.startsWith('/ui/api/logs') || k.startsWith('/ui/api/model-mix'))
      GET_INFLIGHT.delete(k);
}

// 当前页需要哪些数据集。注意每个页签都带上 stats：
// 概览卡片与 renderRecent() 读的都是 STATS，缺了它 render() 会直接抛错。
function dataForTab(name) {
  if (name === 'models') return ['stats', 'models'];
  // 顺序页只吃 /ui/api/model-order（配置的键就是卡片）；models 只用来填「+ 新增模型」
  // 的下拉候选，二者互不干扰。
  if (name === 'order') return ['stats', 'order', 'models'];
  if (name === 'log') return ['stats'];
  if (name === 'benefits') return ['stats', 'benefits'];
  return ['stats'];  // overview：概览卡片与图表都来自 stats
}

// 拉取指定数据集后统一渲染一次。
// force（manual / poll）：重取而非吃缓存。重取会作废同键在飞请求，故这里统一
// 在发起前记下各键的世代；重取完成后若已被更晚的重取取代，就不 render——
// 否则「写操作 → 拿到写前快照 → 覆盖刚渲染好的新值」。
// 注意：只对真正发起/命中的键渲染，避免「全部已缓存」时白跑一次 render()
// —— render() 里的日志页分支会打最慢的 /ui/api/logs，重复 render 会放大成多次请求。
async function loadData(keys, manual, poll) {
  const want = [...new Set(keys)];
  const seq = ++LOAD_SEQ;  // 本次调用的序号：只有最后到达的那次负责渲染
  if (manual) invalidateRecent();  // 手动刷新/写操作后：日志视图需重取
  try {
    await Promise.all(want.map(k => ensureData(k, manual || poll)));
    if (seq !== LOAD_SEQ) return;  // 已有更晚的 loadData 在跑，由它渲染
    render(); if (manual) toast('已刷新');
  } catch (e) { toast('加载失败: ' + e.message, true); }
}

// 首屏 + 30s 轮询：overview/benefits 常驻，当前页所需数据集一并保持新鲜
// （总览页 → stats 图表与卡片；模型页 → models + stats）。
// 合并成一次 loadData，保证只 render 一次（否则日志页会被渲染多遍 → 重复请求）。
// poll=true（定时轮询）：走 force 真正重取——ensureData 对已 LOADED 的键会直接返回，
//   只清 RECENT_QS 的话轮询就是空转（顶栏 uptime、掉线状态、打卡角标全冻住）。
//   与 manual 的区别：不调 invalidateRecent()（日志表由下面的 RECENT_QS 控制重取）、
//   不动 GET 去重条目、不弹 toast。
async function loadAll(manual, poll) {
  const cur = currentTab();
  const keys = new Set(['overview', 'benefits', ...dataForTab(cur)]);
  if (poll) { RECENT_QS = null; MIX_QS = null; }  // 轮询：日志查询是活数据，需重取
  await loadData([...keys], manual, poll);
}

// 写操作（改默认模型/启停/时段/打卡/测试）后强制重取：数据已变，不能吃缓存。
// models/benefits 是写操作本身所属的页，其数据都要跟着刷新。
function refreshAll() {
  const cur = currentTab();
  const keys = ['overview', 'stats', ...dataForTab(cur)];
  if (cur === 'models') keys.push('benefits');
  return loadData([...new Set(keys)], true);
}

// 渲染当前激活页签（数据未就绪的页跳过，由 ensureData 拉回后再渲染）。
function currentTab() {
  const b = document.querySelector('.tab.active');
  return (b && b.dataset.tab) || 'overview';
}

// 设置文件告警条幅：settings.json 读不出来时贴顶展示。
// load_settings() 为容错会静默吞掉语法错误（返回 {}），代价是 model_order /
// 停用 / 时段 / 默认模型四项一起失效且毫无提示——用户只会看到「功能不见了」。
// 这里把「读不出来」显式化，并说清真正的风险：任何一次管理页保存都会用
// save_settings 的 {**previous, **update} 把整份文件覆盖成只剩那一项。
let ALERT_DISMISSED = false;   // 本次会话内用户手动关闭过：别在轮询里又弹回来
function renderAlert() {
  const el = document.getElementById('alertbar');
  if (!el) return;
  const s = (OVERVIEW && OVERVIEW.settings) || null;
  if (!s || s.ok || ALERT_DISMISSED) { el.classList.remove('show'); return; }
  el.innerHTML = `
    <span class="ab-ico">⚠️</span>
    <span class="ab-body">
      <b>设置文件无法解析，配置未生效</b>（候选顺序 / 停用 / 时段 / 默认模型
      会一起失效）。修改前请先在管理页停手：任何一次保存都会把整份文件
      覆盖成只剩该项。
      <div class="ab-detail">${esc(s.error || '未知错误')}
        ${s.mtime ? ` · 文件修改于 ${esc(s.mtime)}` : ''}
        ${s.size != null ? ` · ${s.size} 字节` : ''}</div>
      <div class="ab-detail">路径 <code>${esc(s.path)}</code>　修好后点右上角「↻ 刷新」重载</div>
    </span>
    <button class="ab-close" onclick="dismissAlert()">关闭</button>`;
  el.classList.add('show');
}
function dismissAlert() {
  ALERT_DISMISSED = true;
  document.getElementById('alertbar').classList.remove('show');
}

function render() {
  const tab = currentTab();
  renderAlert();

  // 范围选择器 + 当前范围下的模型聚合（Top10/耗时图/悬停共用）
  renderRangeQuick();
  RANGE_MODELS = modelsInRange();
  renderLogFilters();  // 筛选候选随范围数据刷新，选中态保留

  // 状态条（overview 数据未就绪时保留旧值，避免首屏闪 "undefined"）
  if (OVERVIEW) {
    const dot = document.getElementById('status-dot');
    const ok = OVERVIEW.authenticated !== false;
    dot.className = 'dot' + (ok ? '' : ' off');
    document.getElementById('status-text').textContent =
      `运行中 · 已运行 ${fmtUptime(OVERVIEW.uptime_seconds)}` + (ok ? '' : ' · codebuddy 未登录');
    document.getElementById('listen-hint').textContent =
      '默认模型: ' + (OVERVIEW.default_model.raw ? OVERVIEW.default_model.raw : '(未设置，按请求原样路由)');
  }

  // 概览卡片：请求量/平均耗时按所选范围合计（从 daily 过滤累加）
  // 守卫必须按真实数据源：这些卡片读的是 STATS.daily，只判 OVERVIEW 会在
  // 「overview 已就绪、stats 还没到」时直接抛 TypeError，把整个 render() 打断。
  if (OVERVIEW && STATS) {
    const dm = OVERVIEW.default_model;
    const rl = rangeLabel();
    const dRange = (STATS.daily || []).filter(d => inRange(d.date));
    const rTotal = dRange.reduce((n, d) => n + (d.total || 0), 0);
    const rErr = dRange.reduce((n, d) => n + (d.errors || 0), 0);
    const rDurSum = dRange.reduce((n, d) => n + (d.duration_ms_sum || 0), 0);
    const rAvg = rTotal ? Math.round(rDurSum / rTotal) : 0;
    document.getElementById('cards').innerHTML = `
    <div class="card" style="cursor:pointer" title="点击去模型页修改。仅在客户端请求未带 model 字段时生效；不影响已指定 model 的请求" onclick="switchTab('models')"><div class="k">默认启用模型 ↗</div>
      <div class="v">${dm.raw ? `<span class="chip">${esc(dm.provider || 'codebuddy')}</span>${esc(dm.model)}` :
        '<small>未设置（不影响正常使用）</small>'}</div></div>
    <div class="card"><div class="k">${esc(rl)} 请求</div><div class="v">${rTotal}
      <small>错误 ${rErr}</small></div></div>
    <div class="card"><div class="k">${esc(rl)} 平均耗时</div><div class="v">${rAvg ? fmtMs(rAvg) : '—'}</div></div>
    <div class="card"><div class="k">兜底通道</div><div class="v"><span class="chip">${esc(OVERVIEW.default_provider)}</span><small>未命中模型时</small></div></div>`;
  }

  // 页签角标：模型总数 / 已配顺序数 / 打卡状态（数据未加载时留空）
  if (MODELS)
    document.getElementById('cnt-models').textContent =
      (MODELS.groups || []).reduce((n, g) => n + g.models.length, 0) || '';
  // 角标 = 配置里有几条 model_order（与页面卡片数一致，一个键一张卡）
  if (ORDER)
    document.getElementById('cnt-order').textContent = orderRows().length || '';
  // 打卡角标挪进 benefits.js（refreshBenefitsBadge）：签到强刷走静默重绘、
  // 不再整页 render，得能单独刷新这一个角标
  if (BENEFITS && typeof refreshBenefitsBadge === 'function') refreshBenefitsBadge();

  // 只渲染当前页的区块：模型页/日志页的重活不再随总览页一起跑
  if (tab === 'overview') {
    renderDaily(); renderModelBars(); renderLatency();
    renderCreditsDaily(); renderCreditsBars();
    document.getElementById('credits-sub').textContent =
      `${rangeLabel()} · 上游实扣与本地估算分开显示；不含只报倍率的通道`;
  } else if (tab === 'models') {
    if (MODELS) renderGroups();
  } else if (tab === 'order') {
    renderOrderPage();
  } else if (tab === 'benefits') {
    if (BENEFITS) renderBenefits();
  } else if (tab === 'log') {
    if (STATS) renderRecent();
  }
}

// 吸顶偏移：应用顶栏 + 通道条高度（动态测量，写进 CSS 变量供 sticky 使用）
function refreshStickyVars() {
  const tb = document.querySelector('.topbar');
  if (tb) document.documentElement.style.setProperty('--top-h', tb.offsetHeight + 'px');
  const gh = document.querySelector('#page-models .group-head');
  // 模型页未激活时 display:none 测不出高度，保持上次值即可（切页签时会再刷新）
  if (gh && gh.offsetHeight > 0)
    document.documentElement.style.setProperty('--grp-h', gh.offsetHeight + 'px');
}
window.addEventListener('resize', refreshStickyVars);

// ---- 页签 ----
const TABS = ['overview', 'log', 'models', 'order', 'benefits'];
// 范围选择器仅在总览/请求日志页有意义
const RANGE_TABS = new Set(['overview', 'log']);
function switchTab(name) {
  if (!TABS.includes(name)) name = 'overview';
  for (const b of document.querySelectorAll('.tab'))
    b.classList.toggle('active', b.dataset.tab === name);
  for (const s of document.querySelectorAll('.page'))
    s.classList.toggle('active', s.id === 'page-' + name);
  // 单一范围组件：移动到当前页顶部的挂载点；非范围页则隐藏
  const rb = document.getElementById('rangebar');
  const slot = RANGE_TABS.has(name)
    && document.querySelector('#page-' + name + ' .range-slot');
  if (slot) { slot.appendChild(rb); rb.classList.add('show'); }
  else { rb.classList.remove('show'); }
  const url = new URL(location.href);
  url.hash = name;
  history.replaceState(null, '', url.pathname + url.search + url.hash);
  try { localStorage.setItem('bp-tab', name); } catch (e) {}
  refreshStickyVars();
  if (typeof syncPatStatusAuto === 'function') syncPatStatusAuto();  // 进/离额度页 → 起停模型负载自动刷新
  if (typeof syncNextTimeAuto === 'function') syncNextTimeAuto();   // 同上：起停打卡倒计时（离开即停，不空转）

  // 懒加载：进入页签时补齐该页所需数据，拉回后 render() 会渲染该页
  loadData(dataForTab(name)).catch(() => {});
}
document.querySelectorAll('.tab').forEach(
  b => b.addEventListener('click', () => switchTab(b.dataset.tab)));

// ---- 操作 ----
