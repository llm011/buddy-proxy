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

// 筛选区折叠状态（默认收起超过一行的部分）。存模块变量而非 DOM：整页每 30s
// 重建 innerHTML + 每次点按钮都重渲本函数，挂 DOM 上的状态会被冲掉。
let LOG_FILTERS_OPEN = false;

function renderLogFilters() {
  const el = document.getElementById('log-filters');
  if (!el) return;
  // 过滤空串：后端会给 dumate 补「账号级每日消耗」的合成桶（model 留空、
  // count=0，见 queries._inject_dumate_credits），空模型名不是合法筛选项——
  // 放进去就是列表里一个空白按钮。
  const provs = [...new Set(RANGE_MODELS.map(m => m.provider).filter(Boolean))];
  const models = [...new Set(RANGE_MODELS.map(m => m.model).filter(Boolean))];
  const clients = RANGE_CLIENTS;
  const btn = (kind, val, inner) =>
    `<button class="rb-btn${LOG_FILTERS[kind].has(val) ? ' active' : ''}" data-kind="${kind}" data-val="${esc(val)}">${inner}</button>`;
  // 三组各占一行（.lf-row）：组名徽章 + 该组按钮；空组整行不渲染
  const row = (label, buttons) =>
    buttons.length ? `<div class="lf-row"><span class="lf-label">${label}</span>${buttons.join('')}</div>` : '';
  // 内容放进 .lf-main，外面包一层 .lf-clip（限高）与展开按钮。折叠与否由
  // syncLogFilterFold 按**实际高度**判定——模型多寡决定要不要按钮，不能按
  // 条数硬算（窄屏 / 长模型名都会让同一批按钮高低不同）。
  el.innerHTML =
    `<div class="lf-clip${LOG_FILTERS_OPEN ? ' open' : ''}" data-lffold="log">` +
      `<div class="lf-main">` +
        row('通道', provs.map(p => btn('provider', p,
          `<i class="lf-dot" style="background:${pcolor(p)}"></i>${esc(p)}`))) +
        row('模型', models.map(m => btn('model', m, esc(m)))) +
        row('客户端', clients.map(c => btn('client', c, esc(c)))) +
      `</div>` +
    `</div>` +
    `<button type="button" class="lf-more" data-lffold="log" onclick="toggleLogFilterFold()" hidden></button>`;
  syncLogFilterFold();
}

// 按实际高度决定裁不裁、按钮露不露。上限来自 CSS 变量 --lffold-max（一行高），
// 两边共用一个数——改了 CSS 忘了 JS 就会出现「已经裁了但按钮不出现」的死角。
function syncLogFilterFold() {
  const box = document.querySelector('.lf-clip[data-lffold="log"]');
  if (!box) return;
  const open = LOG_FILTERS_OPEN;
  const limit = parseFloat(getComputedStyle(box).getPropertyValue('--lffold-max')) || 34;
  const clipped = !open && box.scrollHeight > limit + 1;
  box.classList.toggle('open', open);
  box.classList.toggle('clipped', clipped);
  const btn = document.querySelector('.lf-more[data-lffold="log"]');
  if (btn) {
    const show = open || clipped;
    btn.hidden = !show;
    btn.textContent = open ? '收起 ▴' : '更多筛选 ▾';
  }
}

function toggleLogFilterFold() {
  LOG_FILTERS_OPEN = !LOG_FILTERS_OPEN;
  syncLogFilterFold();
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
// 只统计**真实有请求**的桶：count=0 的桶是「该渠道当天的账号级积分」，不是某个
// 模型的请求——前端为补上 dumate 的积分图，后端会往 model_daily 里塞
// {provider:'dumate', model:'', count:0} 的合成桶（见 queries._inject_dumate_credits）。
// 让它们进 RANGE_MODELS 会污染所有按流量口径的视图：耗时图/型号占比/Top10 里凭空
// 多出一个空模型名行、筛选区多一个空白按钮（2026-11-01 回归）。积分图直接读原始
// model_daily，不受这里影响。
function modelsInRange() {
  const md = (STATS && STATS.model_daily) || [];
  const per = new Map();
  for (const b of md) {
    if (!inRange(b.date) || !b.count) continue;
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
