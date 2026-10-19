const PCOLORS = { codebuddy: '#4f8cff', trae: '#a78bfa', zcode: '#3ecf8e', doubao: '#f0b429', kimi: '#4dd0e7' };
function pcolor(p) {
  if (PCOLORS[p]) return PCOLORS[p];
  let h = 0; for (const c of p) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h}, 62%, 62%)`;
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
// 安全嵌进单引号 HTML 属性里的 JS 字符串字面量。模型 id 来自上游目录，
// 不能假定不含引号；先 JSON 编码，再做 HTML entity 编码。
function jsq(s) { return esc(JSON.stringify(String(s ?? ''))); }
function fmtMs(ms) { return ms >= 1000 ? (ms/1000).toFixed(ms >= 10000 ? 0 : 1) + ' s' : ms + ' ms'; }
function fmtUptime(s) {
  if (s < 90) return s + ' 秒';
  if (s < 5400) return Math.round(s/60) + ' 分钟';
  if (s < 172800) return (s/3600).toFixed(1) + ' 小时';
  return Math.round(s/86400) + ' 天';
}
function fmtNum(n) {
  if (n == null) return '—';
  const x = Number(n);
  if (!isFinite(x)) return String(n);
  return x >= 100 ? Math.round(x).toLocaleString() : String(Math.round(x * 10) / 10);
}
// 积分只留 2 位小数展示：上游给的是完整浮点（0.008510259999999999 这种），
// 直接铺在表格里又长又晃眼。Qoder 官网自己也是 2 位（0.12 / 0.07），对齐它。
// 只截展示，JSONL 里存的仍是原值——精度丢了就没法再算总账。
function fmtCredit(v) {
  if (v == null) return null;
  const x = Number(v);
  return isFinite(x) ? x.toFixed(2) : String(v);
}
function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function toast(msg, isErr) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = isErr ? 'err' : ''; el.style.display = 'block';
  clearTimeout(el._t); el._t = setTimeout(() => el.style.display = 'none', 2600);
}
// GET 去重：同一 URL 在飞时复用 Promise。页面里多处 render 会重复触发同一接口
// （切页签 + 轮询 + 各区块渲染），重复请求会白读磁盘（/ui/api/logs 最重）。
// 仅对 GET 生效；带 opts（POST 等写操作）一律直通，避免误合并。
const GET_INFLIGHT = new Map();

// 从错误响应体里提取可读消息。
// FastAPI 的 `HTTPException(detail={...})` 会把 detail 原样包成 `{"detail": {...}}`，
// 而后端统一约定 detail 是 Anthropic 风格的嵌套对象 `{"error": {"message": ...}}`
// （全项目 60+ 处）。若只取 `body.detail` 会拿到**对象**，`new Error(对象)` 转成
// 字符串就是 `[object Object]`——用户看到「保存失败: [object Object]」看不出原因。
// 这里逐层剥：顶层 error.message → detail 里的 error.message → detail 是字符串 →
// detail.message → 兜底状态码。
function errMessage(body, status) {
  const pick = o => (o && typeof o === 'object' && typeof o.message === 'string') ? o.message : '';
  return pick(body)
    || (body && pick(body.error))
    || (body && body.detail && (pick(body.detail) || pick(body.detail.error)))
    || (typeof (body && body.detail) === 'string' ? body.detail : '')
    || (status + ' error');
}

async function api(path, opts) {
  const isGet = !opts || (!opts.method || opts.method.toUpperCase() === 'GET');
  if (isGet && GET_INFLIGHT.has(path)) return GET_INFLIGHT.get(path);
  const p = (async () => {
    const r = await fetch(path, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(errMessage(body, r.status));
    return body;
  })();
  if (isGet) p.finally(() => GET_INFLIGHT.delete(path)).catch(() => {});
  return p;
}

let OVERVIEW = null, MODELS = null, STATS = null, BENEFITS = null;
// 顺序页的唯一数据源：`/ui/api/model-order` 原样返回的 model_order（键 = 卡片）。
let ORDER = null;

// ---- 日期范围选择器 ----
// RANGE.q 记录当前快捷键（如 'd14'）；手改起止日期后 q 置空、退化为自定义区间。
// 数据保留 30 天，故起止限制在 [今天-29, 今天]。
function ymd(d) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function dayOffsetStr(off) {
  const d = new Date(); d.setHours(12, 0, 0, 0); d.setDate(d.getDate() + off);
  return ymd(d);
}
const QUICK_RANGES = [
  { key: 'today', label: '今天', from: 0, to: 0 },
  { key: 'yest', label: '昨天', from: -1, to: -1 },
  { key: 'd3', label: '3 天', from: -2, to: 0 },
  { key: 'd7', label: '7 天', from: -6, to: 0 },
  { key: 'd14', label: '14 天', from: -13, to: 0 },
  { key: 'd30', label: '30 天', from: -29, to: 0 },
];
function rangeFromQuick(q) {
  const r = QUICK_RANGES.find(x => x.key === q);
  return r ? { start: dayOffsetStr(r.from), end: dayOffsetStr(r.to), q } : null;
}
function validDateParam(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return false;
  const d = new Date(value + 'T12:00:00');
  return !Number.isNaN(d.getTime()) && ymd(d) === value;
}
function rangeFromUrl(params) {
  const quick = params.get('range');
  const quickRange = quick && rangeFromQuick(quick);
  if (quickRange) return quickRange;
  const start = params.get('start'), end = params.get('end');
  if (!validDateParam(start) || !validDateParam(end) || start > end) return null;
  return { start, end };
}
const INITIAL_URL_PARAMS = new URLSearchParams(location.search);
const URL_RANGE = rangeFromUrl(INITIAL_URL_PARAMS);
let RANGE = URL_RANGE || rangeFromQuick('d14');
if (!URL_RANGE) {
  try {
    const s = JSON.parse(localStorage.getItem('bp-range') || 'null');
    if (s && s.q) { const r = rangeFromQuick(s.q); if (r) RANGE = r; }
    else if (s && validDateParam(s.start) && validDateParam(s.end) && s.start <= s.end)
      RANGE = { start: s.start, end: s.end };
  } catch (e) {}
}
function persistRange() {
  try {
    localStorage.setItem('bp-range',
      JSON.stringify(RANGE.q ? { q: RANGE.q } : { start: RANGE.start, end: RANGE.end }));
  } catch (e) {}
}
function inRange(dateStr) { return dateStr >= RANGE.start && dateStr <= RANGE.end; }
function tsInRange(ts) { const d = ymd(new Date(ts * 1000)); return d >= RANGE.start && d <= RANGE.end; }
function rangeLabel() {
  const q = QUICK_RANGES.find(x => x.key === RANGE.q);
  return q ? q.label : `${RANGE.start} → ${RANGE.end}`;
}
function renderRangeQuick() {
  document.getElementById('rb-quick').innerHTML = QUICK_RANGES.map(r =>
    `<button class="rb-btn${RANGE.q === r.key ? ' active' : ''}" data-q="${r.key}">${r.label}</button>`).join('');
  const s = document.getElementById('rb-start'), e = document.getElementById('rb-end');
  s.min = e.min = dayOffsetStr(-29); s.max = e.max = dayOffsetStr(0);
  s.value = RANGE.start; e.value = RANGE.end;
}
function rerenderRange() { if (STATS) render(); }
document.getElementById('rb-quick').addEventListener('click', ev => {
  const b = ev.target.closest('.rb-btn'); if (!b) return;
  RANGE = rangeFromQuick(b.dataset.q); persistRange(); syncUrlFromState();
  RECENT_PAGE = 1; renderRangeQuick(); rerenderRange();
});
function onDateInput() {
  let s = document.getElementById('rb-start').value, e = document.getElementById('rb-end').value;
  if (!s || !e) return;
  if (s > e) { const t = s; s = e; e = t; }
  RANGE = { start: s, end: e }; // 手改起止 → 脱离快捷选项
  persistRange(); syncUrlFromState();
  RECENT_PAGE = 1; renderRangeQuick(); rerenderRange();
}
document.getElementById('rb-start').addEventListener('change', onDateInput);
document.getElementById('rb-end').addEventListener('change', onDateInput);

// ---- 请求日志快速筛选（通道 / 模型 / 客户端）----
// 点一次选中、再点取消；组内多选 = OR，组与组之间 = AND；全不选 = 不筛。
// 通道/模型候选从当前日期范围有流量的数据动态生成（RANGE_MODELS）；
// 客户端候选来自日志接口返回的 RANGE_CLIENTS（该维度统计里没有，只能从日志行收集）。
const LOG_FILTERS = {
  provider: new Set((INITIAL_URL_PARAMS.get('provider') || '').split(',').filter(Boolean)),
  model: new Set((INITIAL_URL_PARAMS.get('model') || '').split(',').filter(Boolean)),
  client: new Set((INITIAL_URL_PARAMS.get('client') || '').split(',').filter(Boolean)),
};

// 日期范围与请求日志筛选写进 query，刷新/复制链接后仍能恢复当前视图。
// 保留其它 query 参数和 hash（tab 仍由现有 hash 机制管理）。
function syncUrlFromState() {
  const url = new URL(location.href);
  for (const key of ['range', 'start', 'end']) url.searchParams.delete(key);
  if (RANGE.q) url.searchParams.set('range', RANGE.q);
  else {
    url.searchParams.set('start', RANGE.start);
    url.searchParams.set('end', RANGE.end);
  }
  for (const key of ['provider', 'model', 'client']) {
    url.searchParams.delete(key);
    if (LOG_FILTERS[key].size)
      url.searchParams.set(key, [...LOG_FILTERS[key]].sort().join(','));
  }
  history.replaceState(null, '', url.pathname + url.search + url.hash);
}

function renderLogFilters() {
  const el = document.getElementById('log-filters');
  if (!el) return;
  const provs = [...new Set(RANGE_MODELS.map(m => m.provider))];
  const models = [...new Set(RANGE_MODELS.map(m => m.model))];
  const clients = RANGE_CLIENTS;
  const btn = (kind, val, inner) =>
    `<button class="rb-btn${LOG_FILTERS[kind].has(val) ? ' active' : ''}" data-kind="${kind}" data-val="${esc(val)}">${inner}</button>`;
  // 三组各占一行（.lf-row）：组名徽章 + 该组按钮；空组整行不渲染
  const row = (label, buttons) =>
    buttons.length ? `<div class="lf-row"><span class="lf-label">${label}</span>${buttons.join('')}</div>` : '';
  el.innerHTML =
    row('通道', provs.map(p => btn('provider', p,
      `<i class="lf-dot" style="background:${pcolor(p)}"></i>${esc(p)}`))) +
    row('模型', models.map(m => btn('model', m, esc(m)))) +
    row('客户端', clients.map(c => btn('client', c, esc(c))));
}
document.getElementById('log-filters').addEventListener('click', ev => {
  const b = ev.target.closest('.rb-btn'); if (!b) return;
  const set = LOG_FILTERS[b.dataset.kind];
  const v = b.dataset.val;
  if (set.has(v)) set.delete(v); else set.add(v);
  syncUrlFromState();
  RECENT_PAGE = 1;
  renderLogFilters();
  renderRecent();
});

// 由 STATS.model_daily 按所选范围过滤 + 按（通道/模型）聚合，
// 结构与后端 STATS.models 一致（供 Top10、平均耗时图、悬停共用）。
let RANGE_MODELS = [];
// 客户端筛选候选（按出现次数降序），由 /ui/api/logs 响应带回（见 renderRecent）。
let RANGE_CLIENTS = [];
function modelsInRange() {
  const md = (STATS && STATS.model_daily) || [];
  const per = new Map();
  for (const b of md) {
    if (!inRange(b.date)) continue;
    const k = b.provider + '/' + b.model;
    let e = per.get(k);
    if (!e) {
      e = { provider: b.provider, model: b.model, count: 0, errors: 0, _sum: 0,
            duration_ms_max: 0, prompt_tokens: 0, completion_tokens: 0, last_ts: 0 };
      per.set(k, e);
    }
    e.count += b.count; e.errors += b.errors; e._sum += b.duration_ms_sum;
    e.duration_ms_max = Math.max(e.duration_ms_max, b.duration_ms_max);
    e.prompt_tokens += b.prompt_tokens; e.completion_tokens += b.completion_tokens;
    e.last_ts = Math.max(e.last_ts, b.last_ts || 0);
  }
  const out = [...per.values()].map(e => {
    e.avg_ms = e.count ? Math.round(e._sum / e.count) : 0; delete e._sum; return e;
  });
  out.sort((a, b) => b.count - a.count);
  return out;
}

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
  if (BENEFITS) {
    const ck = (BENEFITS.providers || []).filter(p => p.checkin.supported);
    const cb = document.getElementById('cnt-benefits');
    if (ck.length) {
      const pending = ck.filter(p => !p.checkin.done_today && !p.checkin.inactive && !p.checkin.error);
      if (!pending.length) { cb.textContent = '✓ 已签'; cb.className = 'cnt ok'; }
      else { cb.textContent = `未签 ${pending.length}`; cb.className = 'cnt warn'; }
    } else { cb.textContent = ''; }
  }

  // 只渲染当前页的区块：模型页/日志页的重活不再随总览页一起跑
  if (tab === 'overview') {
    renderDaily(); renderModelBars(); renderLatency();
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
async function setDefault(provider, model) {
  try {
    await api('/ui/api/settings', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ default_model: provider + '/' + model })});
    toast(`默认模型已设为 ${provider}/${model}`);
    refreshAll();
  } catch (e) { toast('设置失败: ' + e.message, true); }
}

async function toggleModel(provider, model, disabled) {
  try {
    await api('/ui/api/model-toggle', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider, model, disabled })});
    toast(`${provider}/${model} 已${disabled ? '停用（调用将直接失败）' : '启用'}`);
    refreshAll();
  } catch (e) { toast('操作失败: ' + e.message, true); }
}

async function toggleProvider(provider, enabled) {
  try {
    await api('/ui/api/provider-toggle', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider, disabled: !enabled })});
    toast(`${provider} 已${enabled ? '启用' : '停用（调用直接失败，额度页不再展示）'}`);
    refreshAll();
  } catch (e) {
    toast('操作失败: ' + e.message, true);
    if (MODELS) renderGroups();  // 失败回滚 checkbox 视觉：按服务端状态重画
  }
}

async function refreshProviderModels(provider, button) {
  if (button) button.disabled = true;
  const oldText = button ? button.textContent : '';
  if (button) button.innerHTML = '<span class="spin"></span> 拉取中';
  try {
    const r = await api('/ui/api/models/refresh', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider })});
    const changes = [];
    if ((r.added || []).length) changes.push(`新增 ${r.added.length}`);
    if ((r.removed || []).length) changes.push(`下架 ${r.removed.length}`);
    toast(`${provider} 模型目录已更新：${r.count} 个${changes.length ? '（' + changes.join('，') + '）' : ''}`);
    // 强制重取：刷新本身只改 provider 内存目录，当前 MODELS 是旧快照。
    await loadData(['models'], true);
  } catch (e) {
    toast('模型目录刷新失败: ' + e.message, true);
  } finally {
    if (button && button.isConnected) { button.disabled = false; button.textContent = oldText; }
  }
}

async function hideModel(provider, model) {
  if (!confirm(`隐藏 ${provider}/${model}？\n\n它会从模型列表和选择器消失，但直接点名仍可调用；这不是“停用”。`)) return;
  try {
    await api('/ui/api/model-hidden', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider, model, hidden: true })});
    toast(`${provider}/${model} 已隐藏（直接点名仍可调用）`);
    await loadData(['models'], true);
  } catch (e) { toast('隐藏失败: ' + e.message, true); }
}

async function restoreHiddenModel(provider, model) {
  try {
    await api('/ui/api/model-hidden', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider, model, hidden: false })});
    toast(`${provider}/${model} 已恢复显示`);
    closeModal();
    await loadData(['models'], true);
  } catch (e) { toast('恢复失败: ' + e.message, true); }
}

function openHiddenModelsModal() {
  const hidden = (MODELS && MODELS.hidden_models) || [];
  document.getElementById('modal-title').textContent = `已隐藏模型 · ${hidden.length}`;
  document.getElementById('modal-body').innerHTML = hidden.length ? `
    <div class="muted" style="font-size:13px;margin-bottom:10px">隐藏只影响列表曝光，直接点名仍可调用；要禁止调用请使用模型行里的“停用”。</div>
    <div class="kv">${hidden.map(m => `
      <span class="k"><span class="tag">${esc(m.provider)}</span></span>
      <span><span class="mono">${esc(m.model)}</span>${m.name && m.name !== m.model ? `<small class="muted" style="margin-left:8px">${esc(m.name)}</small>` : ''}${m.missing ? '<span class="tag bad" style="margin-left:8px">上游已下架</span>' : ''}</span>
      <span></span><span><button class="primary" onclick='restoreHiddenModel(${jsq(m.provider)},${jsq(m.model)})'>恢复显示</button></span>`).join('')}</div>`
    : '<div class="empty">没有隐藏的模型</div>';
  setModalFoot('');
  document.getElementById('overlay').classList.add('show');
}

// ── 限时可用时段编辑 ──
let SCHEDULE_CTX = null;  // { provider, model }

function openScheduleModal(provider, model, windows) {
  SCHEDULE_CTX = { provider, model };
  document.getElementById('modal-title').textContent = `设置可用时段 · ${provider}/${model}`;
  const rows = (windows && windows.length ? windows : []).map(w => scheduleRow(w[0], w[1])).join('');
  document.getElementById('modal-body').innerHTML = `
    <div class="muted" style="font-size:13px;margin-bottom:10px">
      仅在下列时间窗内可调用，窗口外请求直接失败。<b>起 &gt; 止 视为跨天</b>（如 22:00→次日 08:00）。
      不添加任何时段 = 恢复全天可用。时区：Asia/Shanghai。
    </div>
    <div id="sched-rows">${rows}</div>
    <button class="ghost" style="margin-top:8px" onclick="addScheduleRow()">+ 添加时段</button>`;
  setModalFoot(`
    <button onclick="closeModal()">取消</button>
    <button class="primary" onclick="saveSchedule()">保存</button>`);
  document.getElementById('overlay').classList.add('show');
}

function scheduleRow(start, end) {
  return `<div class="sched-row" style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
    <input type="time" class="sched-start" value="${esc(start || '22:00')}" style="flex:1">
    <span class="muted">→</span>
    <input type="time" class="sched-end" value="${esc(end || '08:00')}" style="flex:1">
    <button class="ghost" title="删除此时段" onclick="this.closest('.sched-row').remove()">✕</button>
  </div>`;
}

function addScheduleRow() {
  document.getElementById('sched-rows').insertAdjacentHTML('beforeend', scheduleRow('', ''));
}

async function saveSchedule() {
  if (!SCHEDULE_CTX) return;
  const windows = [];
  document.querySelectorAll('#sched-rows .sched-row').forEach(row => {
    const s = row.querySelector('.sched-start').value;
    const e = row.querySelector('.sched-end').value;
    if (s && e && s !== e) windows.push([s, e]);
  });
  try {
    await api('/ui/api/model-schedule', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ...SCHEDULE_CTX, windows })});
    toast(windows.length
      ? `${SCHEDULE_CTX.provider}/${SCHEDULE_CTX.model} 限时可用：${windows.map(w => w.join('~')).join(', ')}`
      : `${SCHEDULE_CTX.provider}/${SCHEDULE_CTX.model} 已恢复全天可用`);
    closeModal();
    refreshAll();
  } catch (e) { toast('保存失败: ' + e.message, true); }
}

// ── 模型顺序页（独立页签：一眼看全所有已配顺序的模型，就地拖拽编辑）──
// 与模型页行内的「顺序」按钮改的是同一份数据（/ui/api/model-order）。这里存在的
// 意义是把入口从「藏在每行操作列的按钮里」提到一级页签，并按模型铺开。
// 展开态与草稿都存在 ORDER_PAGE 里，整页重绘（render() 由轮询/写操作触发）不丢编辑中
// 的内容——否则 30s 轮询一到，正在拖的顺序就被刷回旧值。

// 遍历 MODELS，收集「带 order 配置」的模型，形状 {p, m, targets[], marks[]}
// 通道目录里的 m["id"] 可能自带通道前缀（qoder 组里是 qoder/qwen3.8-max），
// 而 model_order 的键用的是**剥前缀的裸名**（转发侧按裸名路由，见 ui.py
// _bare_model_id）。不剥就会渲染出 qoder/qoder/qwen3.8-max 这种永远不生效的键。
function bareModelId(id, provider) {
  const p = String(provider || '');
  const s = String(id || '');
  return p && s.startsWith(p + '/') ? s.slice(p.length + 1) : s;
}

// 顺序页的卡片**就是 model_order 里的键**，一个键一张卡，多的一张都不画。
//
// 曾经的写法是「遍历通道目录，每个 通道+模型 组合画一张卡」——同一个模型名在
// 四个通道各画一份，页面上就成了四张一模一样的（用户原话：「配置什么就展示什么，
// 别搞这么复杂」）。现在渲染源只有一个：`/ui/api/model-order` 返回的键。
function orderRows() {
  const items = (ORDER && ORDER.items) || [];
  return items.filter(it => it && it.model);
}

// 顺序页要渲染的卡片 = 配置里的键 + **还没保存、正在编辑中的**草稿键。
// 后者只有「+ 新增模型」选完到点保存之间才会出现（那时配置里还没有这个键）。
function pageRows() {
  const out = [];
  const known = new Set();
  orderRows().forEach(it => {
    known.add(it.model);
    out.push({ m: it.model, order: it });
  });
  Object.keys(ORDER_DRAFT).forEach(name => {
    if (known.has(name)) return;
    out.push({ m: name, order: { targets: [], marks: [] } });
  });
  return out;
}

// 某个模型名的配置（来自上面的接口；草稿另有 ORDER_DRAFT）
function orderRowOf(name) {
  return orderRows().find(it => it.model === name) || null;
}

// 可选模型全集（用于「+ 新增模型」的下拉），按通道分组
function allModelOptions() {
  const out = [];
  ((MODELS && MODELS.groups) || []).forEach(g => {
    (g.models || []).forEach(m => out.push({ p: g.id, m: bareModelId(m.id, g.id) }));
  });
  return out;
}

// 页面状态：卡片键 = **裸模型名**（`glm-5.3`），与 model_order 的键同口径——
// 用户配的就是这个名字，运行时裸键对所有通道生效，所以卡片也只按名字分。
// 展开集合是纯 UI 状态，不随数据刷新重置。
let ORDER_OPEN = new Set();     // 已展开的模型名
let ORDER_DRAFT = {};           // 模型名 → [{provider, model}] 编辑中的草稿（未保存）
let ORDER_PAGE_MARKS = {};      // 模型名 → {targetKey: 剩余秒数}

// 服务端下发的 targets（"p/m" 字符串数组）→ 行对象数组
function orderTargetsToItems(targets) {
  return (targets || []).map(t => {
    const i = String(t).indexOf('/');
    return i > 0 ? { provider: String(t).slice(0, i), model: String(t).slice(i + 1) }
                 : { provider: 'codebuddy', model: String(t) };
  });
}

// 只数填了模型名的档位：末尾那个刚点「+ 添加目标」还没填名的空行不算一档
function orderCount(items) { return (items || []).filter(i => i.model).length; }

// 「未保存」= 草稿与服务端值**内容**不同，而不是「草稿存在」。
// 草稿在展开时就会建立（renderOrderRows 的首次填充），若只看存在性，
// 每张展开过的卡片都会永远挂着「未保存」——撤销修改后也摘不掉。
function orderIsDirty(key) {
  const draft = ORDER_DRAFT[key];
  if (!draft) return false;
  const row = orderRowOf(key);
  const server = row ? row.targets : [];
  const cur = draft.filter(i => i.model).map(i => `${i.provider}/${i.model}`);
  return cur.length !== server.length || cur.some((v, i) => v !== server[i]);
}

// 顺序页的行渲染依赖通道下拉（ORDER_PROVIDER_LIST）与模型候选（ORDER_OPTIONS），
// 这两样原本只在打开弹窗时加载。这里幂等补齐：拉不到就退化成纯手输，不阻断页面。
async function ensureOrderOptions() {
  if (ORDER_PROVIDER_LIST.length) return;
  await loadOrderOptions().catch(() => { ORDER_OPTIONS = {}; });
  // 通道列表以选项接口为准（MODELS 只在模型页加载过才有值）
  ORDER_PROVIDER_LIST = Object.keys(ORDER_OPTIONS || {}).length
    ? Object.keys(ORDER_OPTIONS)
    : ((MODELS && MODELS.groups) || []).map(g => g.id);
}

function renderOrderPage() {
  const box = document.getElementById('order-list');
  if (!box) return;
  // 本页的数据源是 ORDER（配置的键）。MODELS 只给「+ 新增模型」的下拉用，
  // 不能拿它当加载门槛：那会让配置早就到了、却因为模型目录慢或拉失败而一直
  // 停在「加载中…」。ORDER 非空即当前世代的数据（ensureData 会在代次失配时
  // 把它置回 null），所以这里不必再额外判「加载完没有」。
  if (!ORDER) { box.innerHTML = '<div class="empty">加载中…</div>'; return; }
  // 选项还没到位时先拉一次再重绘（只触发一轮：拉到后 ORDER_PROVIDER_LIST 非空）
  if (!ORDER_PROVIDER_LIST.length) {
    ensureOrderOptions().then(() => { if (currentTab() === 'order') renderOrderPage(); });
  }

  const rows = pageRows();
  const sum = document.getElementById('order-summary');
  if (sum) sum.textContent = rows.length ? `${rows.length} 个模型已配顺序` : '';

  if (!rows.length) {
    box.innerHTML = '<div class="empty">还没有配置任何模型顺序 —— 点右上角「+ 新增模型」开始。'
      + '<br><span class="muted" style="font-size:12px">未配置的模型按模型 id 自动匹配通道（历史行为）。</span></div>';
    return;
  }

  box.innerHTML = rows.map(r => orderPageCard(r)).join('');
  // 拖拽/编辑只绑一次：整块容器用事件委托，避免每张卡片重复绑定
  bindOrderPage(box);
}

function orderPageCard(r) {
  const key = r.m;   // 卡片键 = model_order 的键本身
  const open = ORDER_OPEN.has(key);
  // 草稿优先：用户正在编辑时不要被新拉到的数据覆盖
  const items = ORDER_DRAFT[key] || orderTargetsToItems(r.order.targets);
  const marks = ORDER_PAGE_MARKS[key] || {};
  const markCount = Object.keys(marks).length;
  // 标题里展示**通道的顺序**（`qoder → codebuddy`），这是这张卡片真正要回答的
  // 问题：请求这个模型时，依次找哪些上游。用草稿优先的 items 而不是服务端
  // targets，编辑中就能立刻看到改动后的样子。
  const chain = items.filter(i => i.model).map(i => i.provider).join(' → ');

  return `<div class="card order-card" data-key="${esc(key)}">
    <div class="order-head">
      <div class="order-head-main" onclick="orderToggle('${esc(key)}')">
        <span class="chev" style="transform:rotate(${open ? 0 : -90}deg)">▾</span>
        <span class="mono order-title">${esc(r.m)}</span>
        <span class="tag order-count">${orderCount(items)} 档</span>
        ${markCount ? `<span class="tag bad" style="cursor:pointer" title="有目标处于冷却中，点击清除全部冷却，立即重试" onclick="event.stopPropagation();clearOrderMarks('${esc(key)}','${esc(key)}')">⏸ ${markCount}</span>` : ''}
        <span class="tag bad order-dirty" title="有未保存的修改" style="${orderIsDirty(key) ? '' : 'display:none'}">未保存</span>
        <span class="order-chain mono">${esc(chain)}</span>
      </div>
      <button class="primary order-test" onclick="runTest('','${esc(key)}')">测试</button>
    </div>
    ${open ? `<div class="order-body">
      <div class="order-rows" data-key="${esc(key)}"></div>
      <div class="order-actions">
        <button class="ghost" onclick="orderPageAdd('${esc(key)}')">+ 添加目标</button>
        <span class="spacer"></span>
        <button class="ghost" onclick="orderPageReset('${esc(key)}')">撤销修改</button>
        <button class="primary" onclick="orderPageSave('${esc(key)}')">保存</button>
        <button class="ghost danger" title="清空全部目标 = 恢复按模型 id 自动匹配的历史路由"
                onclick="orderPageClear('${esc(key)}')">清空</button>
      </div>
    </div>` : ''}
  </div>`;
}

// 展开/收起：只影响这一张卡片的 DOM，不整体重绘（避免打断其它卡片的编辑）
function orderToggle(key) {
  if (ORDER_OPEN.has(key)) ORDER_OPEN.delete(key); else ORDER_OPEN.add(key);
  renderOrderPage();
}

// 给每个 .order-rows 容器绑一次事件，再填行内容。
// 绑定（bindOrderRowEvents）与渲染（renderOrderRows）分开，是为了避免
// renderOrderRows → bindOrderPage → renderOrderRows 的无限递归。
function bindOrderPage(box) {
  box.querySelectorAll('.order-rows').forEach(el => renderOrderRows(el.dataset.key));
}

// 把 DOM 行回写成草稿（DOM 是编辑期真源，草稿是保存时读的真源）
function orderPageSync(key) {
  const el = document.querySelector(`.order-rows[data-key="${CSS.escape(key)}"]`);
  if (!el) return;
  ORDER_DRAFT[key] = Array.from(el.querySelectorAll('.order-row')).map(r => ({
    provider: orderProviderOf(r),
    model: (r.querySelector('.order-model').value || '').trim(),
  }));
}

function renderOrderRows(key) {
  const el = document.querySelector(`.order-rows[data-key="${CSS.escape(key)}"]`);
  if (!el) return;
  let items = ORDER_DRAFT[key];
  if (!items) {
    // 首次：从服务端拉到的 targets 建草稿
    const row = orderRowOf(key);
    if (!row) { el.innerHTML = ''; return; }
    items = ORDER_DRAFT[key] = orderTargetsToItems(row.targets);
    const marks = {};
    (row.marks || []).forEach(k => {
      const i = String(k.target).indexOf('/');
      if (i > 0) marks[orderKey(k.target.slice(0, i), k.target.slice(i + 1))] = k.cooldown_s;
    });
    ORDER_PAGE_MARKS[key] = marks;
  }
  el.innerHTML = items.length
    ? items.map((it, i) => orderRow(it, i, ORDER_PAGE_MARKS[key] || {}, key)).join('')
    : '<div class="muted" style="font-size:13px;padding:6px 0">暂无目标 —— 保存后恢复历史路由（按模型 id 自动匹配）。</div>';
  // 只补绑新产生的容器，绝不回头调 bindOrderPage（那会再触发本函数 → 递归）
  bindOrderRowEvents(el);
}

// 只给单个容器挂事件（不渲染），供 renderOrderRows / bindOrderPage 共用
function bindOrderRowEvents(el) {
  if (!el || el.dataset.bound) return;
  el.dataset.bound = '1';
  el.addEventListener('dragstart', ev => orderPageDrag(ev, 'start'));
  el.addEventListener('dragover',  ev => orderPageDrag(ev, 'over'));
  el.addEventListener('dragend',   ev => orderPageDrag(ev, 'end'));
  el.addEventListener('drop',      ev => ev.preventDefault());
  el.addEventListener('dragleave', ev => {
    if (!el.contains(ev.relatedTarget)) orderPageDrag(ev, 'end');
  });
}

function orderPageAdd(key) {
  orderPageSync(key);
  const list = ORDER_DRAFT[key] || [];
  const prev = list.length ? list[list.length - 1].provider : '';
  list.push({ provider: prev || (ORDER_PROVIDER_LIST[0] || ''), model: '' });
  ORDER_DRAFT[key] = list;
  renderOrderRows(key);
  const el = document.querySelector(`.order-rows[data-key="${CSS.escape(key)}"]`);
  const last = el && el.querySelector('.order-row:last-child .order-model');
  if (last) last.focus();
}

function orderPageReset(key) {
  delete ORDER_DRAFT[key];
  renderOrderRows(key);
  renderOrderPageHead(key);
}

// 拖拽：与弹窗版同一套语义，只是作用域换成单张卡片的 .order-rows
function orderPageDrag(ev, phase) {
  const box = ev.currentTarget;
  const row = ev.target.closest && ev.target.closest('.order-row');
  if (phase === 'start') {
    if (!row) return;
    ORDER_DRAG_FROM = Number(row.dataset.idx);
    if (ev.dataTransfer) {
      ev.dataTransfer.effectAllowed = 'move';
      try { ev.dataTransfer.setData('text/plain', String(ORDER_DRAG_FROM)); } catch (e) {}
    }
    row.style.opacity = '0.4';
    return;
  }
  if (phase === 'over') {
    if (ORDER_DRAG_FROM === null) return;
    ev.preventDefault();
    if (!row || Number(row.dataset.idx) === ORDER_DRAG_FROM) return;
    const dragged = box.querySelector(`.order-row[data-idx="${ORDER_DRAG_FROM}"]`);
    if (!dragged) return;
    const rect = row.getBoundingClientRect();
    const after = (ev.clientY - rect.top) > rect.height / 2;
    box.insertBefore(dragged, after ? row.nextElementSibling : row);
    Array.from(box.querySelectorAll('.order-row')).forEach((r, i) => { r.dataset.idx = i; });
    ORDER_DRAG_FROM = Number(dragged.dataset.idx);
    return;
  }
  // end：回写
  if (ORDER_DRAG_FROM !== null) {
    const key = box.dataset.key;
    ORDER_DRAFT[key] = Array.from(box.querySelectorAll('.order-row')).map(r => ({
      provider: orderProviderOf(r),
      model: (r.querySelector('.order-model').value || '').trim(),
    }));
  }
  ORDER_DRAG_FROM = null;
  const key = box.dataset.key;
  if (key) { renderOrderRows(key); renderOrderPageHead(key); }
}

// 只更新卡片头部（档位数/未保存标记/链路），不整页重绘
function renderOrderPageHead(key) {
  const card = document.querySelector(`.order-card[data-key="${CSS.escape(key)}"]`);
  if (!card) return;
  const head = card.querySelector('.order-head');
  const items = ORDER_DRAFT[key] || [];
  const cnt = head.querySelector('.order-count');
  if (cnt) cnt.textContent = `${orderCount(items)} 档`;
  const dirty = head.querySelector('.order-dirty');
  if (dirty) dirty.style.display = orderIsDirty(key) ? '' : 'none';
  // 标题里的通道顺序随编辑实时更新（拖拽/换 provider 后立刻反映）。
  const chain = items.filter(i => i.model).map(i => i.provider).join(' → ');
  const tail = head.querySelector('.order-chain');
  if (tail) tail.textContent = chain;
}

// 保存时读的「真源」。卡片**展开**时 DOM 是编辑真源（输入框里的字可能还没回写草稿）；
// **折叠**时没有输入框，能代表这轮意图的只有 `ORDER_DRAFT[key]`。
//
// 这里返回 null 表示「拿不到可信的界面状态」——两种情况：
//   1. 既没展开、也没有草稿（比如刚刷新完页面，orderPage() 的 not-open 分支把
//      `ORDER_DRAFT[key]` 重置成了 `[]`）。此时 `[]` 只是占位，不代表用户想清空。
//   2. 展开着但行数对不上（渲染竞态）。宁可就地提示重来，也不要把空数组当成
//      用户输入发出去，把服务端配置**静默抹平**——这正是「折叠态保存一下配置就没了」
//      的成因。返回 [] 才是用户真的把行都删光了，那是合法的「恢复历史路由」。
function orderPageIntendedItems(key) {
  const el = document.querySelector(`.order-rows[data-key="${CSS.escape(key)}"]`);
  if (!el) {
    // 折叠态：没有 DOM 可读。只有草稿存在（说明这轮编辑过）才认它；
    // 连草稿都没有就是「没展开过」，不可信。
    return ORDER_DRAFT[key] ? ORDER_DRAFT[key].filter(it => it.model) : null;
  }
  const rows = Array.from(el.querySelectorAll('.order-row'));
  const items = rows.map(r => ({
    provider: orderProviderOf(r),
    model: (r.querySelector('.order-model').value || '').trim(),
  })).filter(it => it.model);
  // 输入框里已经填了名的行数，和草稿里的行数对不上 → 渲染竞态，不可信
  const draft = (ORDER_DRAFT[key] || []).filter(it => it.model);
  if (rows.length && items.length !== draft.length && draft.length) return null;
  return items;
}

async function orderPageSave(key) {
  orderPageSync(key);
  const items = orderPageIntendedItems(key);
  if (items === null) {
    toast('界面还没同步好，请重新展开这张卡片再保存', true);
    return;
  }
  const asked = items.map(t => `${t.provider}/${t.model}`);
  // 按键就是裸模型名，provider 留空 —— 后端会用「裸名」形态落键，运行时对
  // 所有发布该模型名的通道都生效（这正是用户要的「写一遍就够」）。
  const model = key;
  try {
    const r = await api('/ui/api/model-order', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider: '', model, targets: items })});
    const saved = r.targets || [];
    // 按下标一对一比是够的：服务端回的是**已经归一化过**的列表（去重/别名/截断都在
    // 写盘时做完），所以 `saved` 与 `asked` 逐项不同就代表真的被规范化过。实测别名
    // 撞车（草稿里加 workbuddy/glm-5.3）时它会正确地报出来——那不是误报。
    const changed = asked.some((a, i) => saved[i] !== a);
    toast(items.length
      ? `${key} 候选顺序：${saved.join(' → ')}` + (changed ? '（已按通道目录规范化）' : '')
      : `${key} 已恢复历史路由`);
    delete ORDER_DRAFT[key];
    delete ORDER_PAGE_MARKS[key];
    await refreshAll();
    renderOrderPage();
  } catch (e) { toast('保存失败: ' + e.message, true); }
}

// 「清空」= 明确表达「恢复历史路由」的意图，绕开 orderPageSync（它靠 DOM 反写草稿，
// 折叠态没容器时会把那时的 `[]` 原样当成用户输入）。这是不可逆操作，先问一句。
async function orderPageClear(key) {
  if (!confirm(`清空 ${key} 的全部候选，恢复按模型 id 自动匹配的历史路由？`)) return;
  ORDER_DRAFT[key] = [];
  const el = document.querySelector(`.order-rows[data-key="${CSS.escape(key)}"]`);
  if (el) renderOrderRows(key);   // 展开态让界面立刻反映清空，再走保存
  await orderPageSave(key);
}

// 「+ 新增模型」：选**模型名**，选中即以空顺序展开（保存后生效）。
// 列表只显示裸模型名、不显示通道：用户配的是「这个模型名走什么顺序」，通道是
// 运行时解析归属用的实现细节。同名模型被多通道发布时也只列一条（一个名字一张卡、
// 一份配置）。
function orderAddModel() {
  const existing = new Set(orderRows().map(it => it.model.toLowerCase()));
  const names = [];
  const seenName = new Set();
  allModelOptions().forEach(o => {
    // 大小写不敏感去重：同一条目在目录里常同时有 `DeepSeek-V4-Flash` 和
    // `deepseek-v4-flash` 两个写法，全列出来只是噪声（6 组这样的重复）。
    const k = o.m.toLowerCase();
    if (seenName.has(k)) return;
    seenName.add(k);
    names.push(o.m);
  });
  names.sort((a, b) => a.localeCompare(b));
  document.getElementById('modal-title').textContent = '新增模型顺序';
  document.getElementById('modal-body').innerHTML = `
    <div class="muted" style="font-size:13px;margin-bottom:10px">
      选一个模型名即可 —— 之后就按这张卡片里的顺序依次尝试，默认用第一个。
    </div>
    <input id="order-new-search" placeholder="搜索模型名…" style="width:100%;margin-bottom:8px">
    <div id="order-new-list" class="order-new-list"></div>`;
  setModalFoot('<button onclick="closeModal()">取消</button>');
  const draw = (q) => {
    const kw = (q || '').trim().toLowerCase();
    const hit = names.filter(n => !kw || n.toLowerCase().includes(kw));
    document.getElementById('order-new-list').innerHTML = hit.length
      ? hit.map(n => {
          const has = existing.has(n.toLowerCase());
          return `<div class="order-new-item">
            <span class="mono on-m">${esc(n)}</span>
            ${has ? '<span class="tag">已配置</span>' : ''}
            <span class="spacer"></span>
            <button class="ghost" onclick="orderAddModelPick('','${esc(n)}')">${has ? '编辑' : '添加'}</button>
          </div>`;
        }).join('')
      : '<div class="muted">没有匹配的模型</div>';
  };
  draw('');
  const s = document.getElementById('order-new-search');
  s.addEventListener('input', () => draw(s.value));
  s.focus();
  document.getElementById('overlay').classList.add('show');
}

// 选一个模型名 → 直接以该名字建/展开卡片。**不再解析「归属通道」**：键就是裸名，
// 运行时对每个发布它的通道都生效，所以没有「这份顺序归哪个通道」这个问题。
// provider 参数仅为兼容调用方保留（一律为空）。
function orderAddModelPick(provider, model) {
  const key = model;
  ORDER_OPEN.add(key);
  if (!ORDER_DRAFT[key]) {
    const row = orderRowOf(key);
    // 已配置的沿用其 targets；未配置的从空开始
    ORDER_DRAFT[key] = row ? orderTargetsToItems(row.targets) : [];
  }
  closeModal();
  switchTab('order');
  renderOrderPage();
  setTimeout(() => {
    const card = document.querySelector(`.order-card[data-key="${CSS.escape(key)}"]`);
    if (card) card.scrollIntoView({block: 'center', behavior: 'smooth'});
  }, 60);
}

// ── 候选上游顺序编辑（按序尝试、未提交即失败换下一档）──
// 编辑界面只在「模型顺序」页签；模型表那一列只留徽标，不做行内入口。
let ORDER_OPTIONS = null;     // /ui/api/model-order/options 的缓存：{provider: [{id,label}]}
let ORDER_PROVIDER_LIST = []; // 可选通道 id 列表
let ORDER_DRAG_FROM = null;   // 拖拽中的行下标

// 规范化比较：大小写/空白不敏感，避免「Glm-5.3」被判成不存在的模型
function orderKey(provider, model) {
  return `${(provider || '').toLowerCase()}/${(model || '').trim().toLowerCase()}`;
}

async function loadOrderOptions() {
  if (ORDER_OPTIONS) return ORDER_OPTIONS;
  const r = await api('/ui/api/model-order/options');
  const map = {};
  (r.groups || []).forEach(g => { map[g.provider] = g.models || []; });
  ORDER_OPTIONS = map;
  return map;
}

// 渲染一行候选目标。marks 是该模型的冷却表（"provider/model" → 剩余秒数），
// scope 是数据源作用域，写进行的 data-scope（模型键，读写 ORDER_DRAFT[key]）。
// 行内处理器只带 this，作用域由 orderRowCtx 从**事件元素自身的行**上读——
// 不做全局 querySelector 反查：页面上可能同时存在多张展开的卡片，各自都有
// data-idx="0" 的行，全局查会写错数据源。
// 模型页开关停用的通道集合（MODELS.groups[].disabled 由后端下发）
function disabledProviderSet() {
  return new Set(((MODELS && MODELS.groups) || []).filter(g => g.disabled).map(g => g.id));
}

function orderRow(item, idx, marks, scope) {
  const id = `order-p-${String(scope).replace(/[^a-zA-Z0-9]/g, '_')}-${idx}`;
  const marksMap = marks || {};
  const provOff = disabledProviderSet().has(item.provider);
  const models = (ORDER_OPTIONS && ORDER_OPTIONS[item.provider]) || [];
  // 自由文本 + <datalist>：既能点选（用户要的「可以选」），又保留手输
  // （目录里的名字可能与上游实际接受的 id 不同，锁死成下拉会把人卡住）
  const list = models.map(m =>
    `<option value="${esc(m.id)}"${m.label ? ` label="${esc(m.label)}"` : ''}></option>`).join('');
  const left = marksMap[orderKey(item.provider, item.model)];
  const known = !models.length || models.some(m => m.id === item.model);
  // 该通道/模型的积分倍率（与请求日志页同源：/ui/api/stats 的 credits_map），
  // 展开后逐项展示方便横向对比谁便宜谁贵；改通道/模型时随重绘即时刷新。
  // 目录里的值带「 credits」后缀（如 "x1.62 credits"），徽章里剥掉、原文进悬浮提示
  const mult = ((STATS || {}).credits_map || {})[orderKey(item.provider, item.model)];
  const multTxt = mult ? String(mult).replace(/\s*credits\s*$/i, '') : '';
  // per-account 模型支持画像（qoder 特有）：该模型只在部分账号上可用
  const sup = (models.find(m => m.id === item.model) || {}).support;
  return `<div class="order-row" draggable="true" data-idx="${idx}" data-scope="${esc(scope)}"${provOff ? ' style="opacity:.45"' : ''}>
    <span class="order-grip" title="拖拽调整顺序">⠿</span>
    <span class="order-no muted mono">${idx + 1}</span>
    <button type="button" class="order-provider" title="候选通道（点击选择）"
      onclick="orderProviderMenu(this)">${esc(item.provider || '选择通道…')}</button>
    <input class="order-model" type="text" list="${id}"
      placeholder="模型名（可下拉选择或手输，如 glm-5.3）"
      value="${esc(item.model || '')}"
      oninput="orderSetModel(this)">
    <datalist id="${id}">${list}</datalist>
    <span class="order-flags">
      ${multTxt ? `<span class="tag mult" title="积分倍率：${esc(mult)}（估算参考，非实际扣费）">${esc(multTxt)}</span>` : ''}
      ${provOff ? '<span class="tag bad" title="该通道已在模型页停用：这一档转发时会被跳过">通道已停用</span>' : ''}
      ${sup ? `<span class="tag" title="受账号限制，仅支持：${esc((sup.accounts || []).join('、'))}">部分账号</span>` : ''}
      ${left ? `<span class="tag bad" style="cursor:pointer" title="冷却中，剩余 ${left}s；点击清除冷却，立即重试" onclick="clearOneMark('${esc(item.provider)}/${esc(item.model)}')">⏸ ${left}s</span>` : ''}
      ${known ? '' : '<span class="tag bad" title="该通道目录里没有这个名字；保存时后端会报错">?</span>'}
    </span>
    <span class="order-btns">
      <button class="ghost" title="上移" onclick="orderMove(this,-1)">▲</button>
      <button class="ghost" title="下移" onclick="orderMove(this,1)">▼</button>
      <button class="ghost danger" title="删除此目标" onclick="orderRemove(this)">✕</button>
    </span>
  </div>`;
}

// 行的当前 provider：自定义下拉后触发器是按钮，值在 textContent 里；
// 未选时的占位文案不是合法通道名，归一成空串
function orderProviderOf(rowEl) {
  const t = (rowEl.querySelector('.order-provider') || {}).textContent || '';
  const v = t.trim();
  return ORDER_PROVIDER_LIST.includes(v) ? v : '';
}

// 从事件元素定位它所属的行：作用域 + 下标 + 对应的可变数组
function orderRowCtx(el) {
  const row = el.closest('.order-row');
  if (!row) return null;
  const scope = row.dataset.scope || '';
  return { row, scope, idx: Number(row.dataset.idx), items: orderItemsOf(scope) };
}

// 按作用域取数据源：作用域就是模型键，对应该卡片的草稿
function orderItemsOf(scope) {
  return (ORDER_DRAFT[scope] = ORDER_DRAFT[scope] || []);
}

function orderRepaint(scope) {
  renderOrderRows(scope);
  renderOrderPageHead(scope);
}

// 重绘会把整行 innerHTML 换掉，输入框随之失焦。手输模型名时这等于「打第二个
// 字符就没法继续打」，所以重绘后把光标送回原处。
function orderRefocus(scope, idx, field, pos) {
  const row = document.querySelector(
    `.order-row[data-scope="${CSS.escape(scope)}"][data-idx="${idx}"]`);
  const el = row && row.querySelector(field);
  if (!el) return;
  el.focus();
  try { el.setSelectionRange(pos, pos); } catch (e) {}
}

let _opickEl = null;

function orderProviderMenuClose() {
  if (_opickEl) { _opickEl.remove(); _opickEl = null; }
  document.removeEventListener('mousedown', orderProviderMenuOutside, true);
  document.removeEventListener('keydown', orderProviderMenuEsc, true);
}

function orderProviderMenuOutside(ev) {
  if (_opickEl && !_opickEl.contains(ev.target)) orderProviderMenuClose();
}

function orderProviderMenuEsc(ev) {
  if (ev.key === 'Escape') orderProviderMenuClose();
}

function orderProviderMenu(btn) {
  const c = orderRowCtx(btn);
  if (!c) return;
  const cur = c.items[c.idx].provider || '';
  orderProviderMenuClose();
  const el = document.createElement('div');
  el.className = 'opick';
  el.id = 'opick-root';
  const draw = (showAll) => {
    const model = c.items[c.idx].model || '';
    const nk = orderKey('', model).replace(/^\//, '');
    const matches = (pid) => ((ORDER_OPTIONS && ORDER_OPTIONS[pid]) || [])
      .some(m => orderKey('', m.id).replace(/^\//, '') === nk);
    const primary = [], rest = [];
    ORDER_PROVIDER_LIST.forEach(p => {
      // 当前已选通道永远进主列表：不然切到别处后想切回来，得先开「更多选项」
      (p === cur || (!nk || matches(p)) ? primary : rest).push(p);
    });
    const item = (p) => `<div class="opick-item${p === cur ? ' on' : ''}" data-p="${esc(p)}">${esc(p)}${p === cur ? ' <span class="opick-cur">当前</span>' : ''}</div>`;
    const more = rest.length
      ? (showAll ? rest.map(item).join('')
        : `<div class="opick-more" onclick="orderProviderMenuMore()">更多选项（${rest.length} 个不带「${esc(model || '该模型')}」的通道）</div>`)
      : '';
    el.innerHTML = primary.map(item).join('') + more;
  };
  el.addEventListener('click', ev => {
    const it = ev.target.closest('.opick-item');
    if (it) orderProviderPick(it.dataset.p, btn);
  });
  el._draw = draw;
  draw(false);
  document.body.appendChild(el);
  // 定位：按钮正下方，右缘对齐按钮（行右端空间小）；超视口再翻转到上方
  const b = btn.getBoundingClientRect();
  el.style.visibility = 'hidden'; el.style.display = 'block';
  const ew = el.offsetWidth, eh = el.offsetHeight;
  let x = Math.min(b.right - ew, window.innerWidth - ew - 8);
  let y = b.bottom + 4;
  if (y + eh > window.innerHeight - 8) y = b.top - eh - 4;
  el.style.left = `${Math.max(8, x)}px`; el.style.top = `${Math.max(8, y)}px`;
  el.style.visibility = '';
  _opickEl = el;
  setTimeout(() => {   // 跳过打开这次 mousedown，否则立刻被 outside 关掉
    document.addEventListener('mousedown', orderProviderMenuOutside, true);
    document.addEventListener('keydown', orderProviderMenuEsc, true);
  }, 0);
}

function orderProviderMenuMore() {
  if (_opickEl && _opickEl._draw) _opickEl._draw(true);
}

function orderProviderPick(pid, btn) {
  orderProviderMenuClose();
  const c = orderRowCtx(btn);
  if (!c || !c.items[c.idx]) return;
  if (c.items[c.idx].provider === pid) return;   // 原样点击只收浮层
  c.items[c.idx].provider = pid;
  orderRepaint(c.scope);
}

function orderSetModel(el) {
  const c = orderRowCtx(el);
  if (!c || !c.items[c.idx]) return;
  c.items[c.idx].model = el.value;
  // 只对「已完整输入」的名做存在性提示，避免每敲一个字符就重绘闪烁
  if (el.value.trim().length < 2) return;
  const pos = el.selectionStart;
  orderRepaint(c.scope);
  orderRefocus(c.scope, c.idx, '.order-model', pos);
}

function orderMove(el, delta) {
  const c = orderRowCtx(el);
  if (!c) return;
  const to = c.idx + delta;
  if (to < 0 || to >= c.items.length) return;
  const [it] = c.items.splice(c.idx, 1);
  c.items.splice(to, 0, it);
  orderRepaint(c.scope);
}

function orderRemove(el) {
  const c = orderRowCtx(el);
  if (!c || !c.items[c.idx]) return;
  c.items.splice(c.idx, 1);
  orderRepaint(c.scope);
}


async function clearOrderMarks(provider, model) {
  try {
    const r = await api('/ui/api/model-order/mark-clear', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider, model })});
    toast(`已清除 ${r.marks_cleared || 0} 个冷却标记`);
    refreshAll();
  } catch (e) { toast('清除失败: ' + e.message, true); }
}

// 清除单个目标的冷却标记（orderRow 里点⏸ 徽标触发；target 形如 "provider/model"）
async function clearOneMark(target) {
  try {
    const r = await api('/ui/api/model-order/mark-clear', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ target })});
    toast(`已清除 ${target} 的冷却`);
    refreshAll();
  } catch (e) { toast('清除失败: ' + e.message, true); }
}

async function runTest(provider, model) {
  const overlay = document.getElementById('overlay');
  document.getElementById('modal-title').textContent = `测试 ${provider ? provider + '/' : ''}${model}`;
  document.getElementById('modal-body').innerHTML = '<span class="spin"></span>发送 "hi" 到上游，等待回复…（最长 120s）';
  setModalFoot('');  // 这个弹窗只有固定的「关闭」；不清空会残留上一个弹窗的按钮
  overlay.classList.add('show');
  try {
    const r = await api('/ui/api/test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider, model })});
    const u = r.usage || {};
    const replyProvider = String(r.provider || provider || '');
    const replyModel = String(r.model || model || '');
    const replyTarget = replyProvider && !replyModel.startsWith(replyProvider + '/')
      ? `${replyProvider}/${replyModel}` : replyModel;
    document.getElementById('modal-body').innerHTML = r.ok ? `
      <div class="kv">
        <span class="k">结果</span><span style="color:var(--ok)">✓ 成功</span>
        <span class="k">耗时</span><span class="mono">${fmtMs(r.latency_ms)}</span>
        <span class="k">回复模型</span><span class="mono">${esc(replyTarget)}</span>
        <span class="k">Tokens</span><span class="mono">${u.prompt_tokens ?? '—'} + ${u.completion_tokens ?? '—'}</span>
        <span class="k">finish</span><span class="mono">${esc(r.finish_reason || '—')}</span>
      </div>
      <pre class="view">${esc(r.content)}</pre>` : `
      <div class="kv">
        <span class="k">结果</span><span style="color:var(--err)">✗ 失败</span>
        <span class="k">耗时</span><span class="mono">${fmtMs(r.latency_ms)}</span>
        <span class="k">状态</span><span class="mono">${r.status || '—'}</span>
      </div>
      <pre class="view" style="color:#ffb3b3">${esc(r.error || '未知错误')}</pre>`;
    refreshAll();
  } catch (e) {
    document.getElementById('modal-body').innerHTML =
      `<pre class="view" style="color:#ffb3b3">${esc(e.message)}</pre>`;
  }
}

// 弹窗底部按钮槽。各弹窗自己带「取消/保存」，写进这里而不是 body 末尾，
// 否则会和骨架里固定的「关闭」并排出现两个含义重叠的按钮。
function setModalFoot(html) {
  const foot = document.getElementById('modal-foot');
  if (foot) foot.innerHTML = html || '<button onclick="closeModal()">关闭</button>';
}

function closeModal() {
  document.getElementById('overlay').classList.remove('show');
  setModalFoot('');  // 复位：下次直接打开「测试」弹窗时不会看到上个弹窗的按钮
}
document.getElementById('overlay').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeModal();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

let initTab = location.hash.replace('#', '');
try { initTab = initTab || localStorage.getItem('bp-tab') || ''; } catch (e) {}
switchTab(initTab);
loadAll();
setInterval(() => loadAll(false, true), 30000);
