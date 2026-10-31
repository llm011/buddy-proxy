// ---- 请求日志页头部：各模型使用占比饼图（纯 SVG 圆环）----
//
// 数据来自 /ui/api/model-mix（MetricsCollector 的内存计数表，按天聚合，不扫
// 磁盘），所以它随日期范围联动，且与 /ui/api/stats 同价——30s 轮询不会额外读文件。
// 交互：扇区/图例行双向 hover 高亮，点击图例行 = 只看该模型（再点一次或点「全部」
// 回到全量），点击扇区则跳到对应模型。
let MIX = null;        // 最近一次 /ui/api/model-mix 响应
let MIX_QS = null;     // 该响应对应的查询串（相同则跳过，不重复请求）
let MIX_SEQ = 0;       // 并发防抖：只有最后发起的请求结果生效
let MIX_FOCUS = null;  // 当前聚焦的模型键 `provider/model`（null = 全部）

// 饼图配色：catmull 风格的柔和色板，与图表卡片的深色底相容。
// 通道色（pcolor）只用于请求趋势图——那里同一通道必须同色，而这里每个模型
// 各占一块，按模型取色才有区分度。
const MIX_COLORS = ['#5b8def', '#e5a54b', '#4fc48f', '#e06c9f', '#9a7be0',
                    '#4fb8d8', '#c9d05a', '#e07b5b', '#7b8ff0', '#d98bc4',
                    '#61c9a8', '#b0a06a'];
const MIX_OTHER_COLOR = '#4a5464';

function modelMixKey(m) { return m.model; }

// 扇区块列表：前 N 个模型各一块，剩下合并成「其他」一块（tooltip 里列明细）。
// 后端已按**模型名**跨通道合并，所以一块 = 一个模型（providers 里看各通道次数）。
// 没有对比时饼图没意义：一个满圆 + 一行 100%，或者一片空的「暂无请求」，
// 都纯占地方。用户在 2026-10-20 反馈里明确要求这两种情形直接隐藏整张卡片。
//
// 判定看**扇区数**而不是 MIX.models_total：
//   - models_total 是聚合前的模型数；top-N + 「其他」折叠后可能只剩两块，
//     但两块之间有对比，仍然值得看。
//   - 无数据（total=0）也隐藏：日志页紧接着就是「最近请求」，空态不会让人
//     以为功能坏了；此时副标题避开显示，免得留下一条孤零零的「0 次」。
// 所以：有数据、且真正只有一个扇区 → 隐藏；没有数据 → 隐藏。
function mixHasNothingToCompare() {
  const slices = mixSlices();
  const total = slices.reduce((n, s) => n + s.count, 0);
  return total === 0 || slices.length === 1;
}

// 卡片显隐：隐藏时整张 .mix-card 连同标题一起收起（不是只清空内容，
// 否则会在日志页顶部留一个空盒子）。用 hidden 属性而非 display:none，
// 便于测试与 a11y 工具识别。
function setMixVisible(visible) {
  const card = document.getElementById('log-mix');
  if (!card) return;
  if (visible) card.removeAttribute('hidden');
  else card.setAttribute('hidden', '');
}

function mixSlices() {
  if (!MIX) return [];
  const out = (MIX.models || []).map((m, i) => ({
    key: modelMixKey(m), label: m.model, provider: m.provider || '',
    count: m.count, errors: m.errors || 0, providers: m.providers || [],
    color: MIX_COLORS[i % MIX_COLORS.length],
  }));
  if (MIX.other && MIX.other.count) {
    out.push({ key: '__other__', label: '其他', provider: '',
               count: MIX.other.count, errors: MIX.other.errors || 0,
               color: MIX_OTHER_COLOR, members: MIX.other.models || [] });
  }
  return out;
}

// 悬浮框内容：模型 + 次数/占比 + 错误 + 各通道明细
function mixTipHtml(s) {
  const total = mixSlices().reduce((n, x) => n + x.count, 0) || 1;
  let html = `<div class="t-date">${esc(s.label)}</div>`
           + `<div class="t-p"><i style="background:${s.color}"></i>请求 <b style="margin-left:auto;padding-left:12px">${s.count.toLocaleString()} 次</b></div>`
           + `<div class="t-p">占比 <b style="margin-left:auto;padding-left:12px">${(s.count / total * 100).toFixed(1)}%</b></div>`;
  if (s.errors) html += `<div class="t-p">错误 <b style="margin-left:auto;padding-left:12px;color:var(--err)">${s.errors}</b></div>`;
  // 各通道明细：同模型跨通道时才知道流量走的是哪个上游
  if (s.providers && s.providers.length > 1) {
    html += '<div class="t-sep"></div>' + s.providers.map(p =>
      `<div class="t-p muted" style="font-size:11.5px">${esc(p.provider)}<b style="margin-left:auto;padding-left:12px">${p.count.toLocaleString()}</b></div>`).join('');
  }
  if (s.members && s.members.length) {
    html += s.members.slice(0, 8).map(m =>
      `<div class="t-p muted" style="font-size:11.5px">${esc(m.model)}<b style="margin-left:auto;padding-left:12px">${m.count.toLocaleString()}</b></div>`).join('');
    if (s.members.length > 8)
      html += `<div class="t-p muted" style="font-size:11.5px">… 另 ${s.members.length - 8} 个</div>`;
  }
  return html;
}

// 极坐标 → 笛卡尔坐标（12 点方向起画，顺时针；与图例行顺序一致，读起来更顺）
function polarXY(cx, cy, r, angle) {
  return [cx + r * Math.sin(angle), cy - r * Math.cos(angle)];
}

function donutArcPath(cx, cy, rOuter, rInner, from, to) {
  const [x0, y0] = polarXY(cx, cy, rOuter, from);
  const [x1, y1] = polarXY(cx, cy, rOuter, to);
  const [x2, y2] = polarXY(cx, cy, rInner, to);
  const [x3, y3] = polarXY(cx, cy, rInner, from);
  const large = (to - from) > Math.PI ? 1 : 0;
  return `M${x0} ${y0}A${rOuter} ${rOuter} 0 ${large} 1 ${x1} ${y1}`
       + `L${x2} ${y2}A${rInner} ${rInner} 0 ${large} 0 ${x3} ${y3}Z`;
}

function renderModelMix() {
  if (currentTab() !== 'log') return;   // 其它页签不可见，别空转
  const donutEl = document.getElementById('mix-donut');
  const legendEl = document.getElementById('mix-legend');
  const subEl = document.getElementById('mix-sub');
  if (!donutEl || !legendEl) return;
  const slices = mixSlices();
  const total = slices.reduce((n, s) => n + s.count, 0);

  // 只有一种模型 / 没有数据 → 整张卡隐藏（含标题），别留满圆或空盒子。
  if (mixHasNothingToCompare()) {
    MIX_FOCUS = null;
    donutEl.innerHTML = '';
    legendEl.innerHTML = '';
    if (subEl) subEl.textContent = '';
    setMixVisible(false);
    return;
  }
  setMixVisible(true);   // 切范围/筛选后模型变多了，要能重新露出来
  const focus = slices.some(s => s.key === MIX_FOCUS) ? MIX_FOCUS : null;
  MIX_FOCUS = focus;

  // 几何：viewBox 固定 220x220，卡片按实际宽度缩放（等比）
  const S = 220, cx = S / 2, cy = S / 2, rOuter = 96, rInner = 62;
  const active = focus ? slices.filter(s => s.key === focus) : slices;
  const activeTotal = active.reduce((n, s) => n + s.count, 0) || 1;
  let angle = 0;
  let svg = `<svg viewBox="0 0 ${S} ${S}" width="100%" height="100%" role="img"
      aria-label="各模型使用占比">`;
  for (const s of slices) {
    const sweep = (s.count / total) * Math.PI * 2;
    const from = angle, to = angle + sweep;
    angle = to;
    const dim = focus && s.key !== focus;
    // 只剩一块时画整环（画圆弧到 2π 两端点重合，A 指令退化画不出来）
    const d = sweep >= Math.PI * 2 - 1e-6
      ? donutArcPath(cx, cy, rOuter, rInner, 0, Math.PI * 2 - 1e-4) + ''
      : donutArcPath(cx, cy, rOuter, rInner, from, to);
    const grow = focus === s.key ? 3 : 0;   // 聚焦扇区往外顶一点
    svg += `<path class="mix-slice${dim ? ' dim' : ''}" data-key="${esc(s.key)}"
      d="${d}" fill="${s.color}"
      ${grow ? `transform="translate(${(grow * Math.sin((from + to) / 2)).toFixed(2)} ${(-grow * Math.cos((from + to) / 2)).toFixed(2)})"` : ''}
    />`;
  }
  // 圆心：全部时显示合计，聚焦时显示该模型占比
  const focused = focus ? slices.find(s => s.key === focus) : null;
  const big = focused ? (focused.count / total * 100) : total;
  const small = focused ? `${focused.count} 次` : '次请求';
  svg += `<text class="mix-center-v" x="${cx}" y="${cy - 1}" text-anchor="middle">`
       + `${focused ? big.toFixed(1) + '%' : big.toLocaleString()}</text>`
       + `<text class="mix-center-k" x="${cx}" y="${cy + 16}" text-anchor="middle">${small}</text>`;
  // 扇区热区：直用 path 的 hit 区域（圆环本身），hover 高亮
  svg += '</svg>';
  donutEl.innerHTML = svg;

  // 图例 = 明细表：色块 / 模型名 / 占比 / 次数 / 迷你占比条
  const maxShare = Math.max(...slices.map(s => s.count), 1);
  legendEl.innerHTML = slices.map(s => {
    const share = s.count / total * 100;
    const isFocus = focus === s.key;
    return `<button type="button" class="mix-row${isFocus ? ' on' : ''}${focus && !isFocus ? ' dim' : ''}"
        data-key="${esc(s.key)}" title="${esc(s.providers && s.providers.length > 1
          ? s.providers.map(p => p.provider + ' ' + p.count + ' 次').join(' · ')
          : (s.provider || '其余模型'))}">
      <i class="mix-dot" style="background:${s.color}"></i>
      <span class="mix-name">${esc(s.label)}</span>
      <span class="mix-prov muted">${esc(s.provider || '')}${(s.providers && s.providers.length > 1) ? ' +' + (s.providers.length - 1) : ''}</span>
      <span class="mix-bar"><i style="width:${Math.max(2, s.count / maxShare * 100)}%;background:${s.color}"></i></span>
      <span class="mix-pct mono">${share.toFixed(share >= 9.95 ? 0 : 1)}%</span>
      <span class="mix-count mono muted">${s.count.toLocaleString()}</span>
    </button>`;
  }).join('');

  // 副标题：范围 + 覆盖度（内存计数表只能覆盖启动回读的部分历史）
  // 筛选生效时明确写出来：否则和下面筛选前的「共 N 条」对不上会被当成 bug
  const flt = [...LOG_FILTERS.provider].concat([...LOG_FILTERS.model]);
  const bits = [rangeLabel(), `共 ${total.toLocaleString()} 次 · ${(MIX && MIX.models_total) || slices.length} 个模型`];
  if (flt.length) bits.push(`已按 ${flt.join(' / ')} 筛选`);
  if (MIX && MIX.partial) bits.push('≈ 更早的归档未计入');
  if (subEl) subEl.textContent = bits.join(' · ') + ' · 点图例行可只看该模型';
}

// 拉取 + 渲染（只在日志页可见时发请求）
// 接口挂了（典型：进程还没重启，路由表里没有 /ui/api/model-mix）时画一个有指引的
// 错误态，而不是把后端原话「Not Found」直接益给用户——那句话既不说哪里坏了，
// 也不说该怎么办。用户 2026-10-19 报的就是这个。
// 400/404 这类「路由都不存在」的失败重试也没用（每次轮询都会再打一遍、再登一次），
// 所以标上 MIX_BROKEN 后本轮不再重试，只留一个「重试」按钮；5xx/网络错误则允许
// 下轮轮询自动重试。
let MIX_BROKEN = false;

function mixErrorHtml(msg, status) {
  // 没拿到状态码时（fetch 本身失败）归为网络类，允许自动重试
  const gone = status === 401 || status === 403 || status === 404 || status === 405;
  const hint = gone
    ? '后端没有这个接口——改完代码后<b>需要重启 proxy 进程</b>才会加载新路由（静态 JS/CSS 每次现读，所以前端已经是新的、后端还是旧的）。'
    : '下一轮自动刷新会重试；也可点「重试」立即重试。';
  return `<div class="mix-error">
      <div class="me-title">占比图暂时不可用</div>
      <div class="me-msg">${esc(msg)}${status ? `（HTTP ${status}）` : ''}</div>
      <div class="me-hint">${hint}</div>
      <button class="ghost me-retry" onclick="loadModelMix(true)">重试</button>
    </div>`;
}

async function loadModelMix(force) {
  if (currentTab() !== 'log') return;
  // 已确认是「接口不存在」：别每轮白打一次（force = 用户点「重试」，要放行）
  if (MIX_BROKEN && !force) return;
  // 与日志表同一套查询串：日期范围 + 通道/模型筛选（客户端维度计数表里没有）。
  // 只传日期的话，勾了「只看某通道」下面表格缩了、饼图却还是全量。
  let qs = `start=${encodeURIComponent(RANGE.start)}&end=${encodeURIComponent(RANGE.end)}`;
  if (LOG_FILTERS.provider.size)
    qs += `&providers=${encodeURIComponent([...LOG_FILTERS.provider].join(','))}`;
  if (LOG_FILTERS.model.size)
    qs += `&models=${encodeURIComponent([...LOG_FILTERS.model].join(','))}`;
  if (!force && qs === MIX_QS) return;   // 同查询已渲染：不重复请求
  const seq = ++MIX_SEQ;
  try {
    const resp = await api('/ui/api/model-mix?' + qs);
    if (seq !== MIX_SEQ) return;         // 已被更晚的请求取代
    MIX = resp; MIX_QS = qs;
    MIX_BROKEN = false;
    renderModelMix();
  } catch (e) {
    if (seq !== MIX_SEQ) return;
    const el = document.getElementById('mix-donut');
    // api() 抛的 Error 只带 message，丢了状态码；这里从 message 里识别常见的
    // 「404 Not Found」形态即可（errMessage 对 FastAPI 的 {"detail":"Not Found"}
    // 就是原样透传这句）。识别不到就按可重试处理。
    const gone = /not found|404/i.test(e.message || '');
    MIX_BROKEN = gone;
    // 出错时要**强制重新露出卡片**：如果上一次是「只有一种模型」而隐藏了，
    // 现在接口挂了却把错误提示也一并隐藏，用户只会看到空白，比报错还困惑。
    setMixVisible(true);
    if (el) el.innerHTML = mixErrorHtml(e.message || '请求失败', gone ? 404 : null);
    // 副标题也要收尾：否则会一直挂着「加载中…」，看着像还在跑
    const subEl = document.getElementById('mix-sub');
    if (subEl) subEl.textContent = `${rangeLabel()} · 占比图不可用`;
    const lg = document.getElementById('mix-legend');
    if (lg) lg.innerHTML = '';
    // 关键：**成功才记 MIX_QS**。失败也记的话，下一轮轮询会在
    // `qs === MIX_QS` 处直接 return，网络抖一下就永久停在错误态
    // （用户只能手动点「重试」）——这正是本函数要避免的。
    // 404 那种「接口根本不存在」已经由 MIX_BROKEN 拦住了，不会因此每轮白打。
    MIX_QS = null;
  }
}

// 扇区 hover：显示该模型明细（不改变筛选，只做浮框 + 扇区提亮）
document.getElementById('mix-donut').addEventListener('mousemove', e => {
  const path = e.target.closest('.mix-slice');
  if (!path) { hideChartTip(); return; }
  const s = mixSlices().find(x => x.key === path.dataset.key);
  if (!s) { hideChartTip(); return; }
  showChartTip(mixTipHtml(s), path.getBoundingClientRect());
});
document.getElementById('mix-donut').addEventListener('mouseleave', hideChartTip);

// 点图例行：聚焦/取消聚焦（纯前端筛选，不重新拉数据）
document.getElementById('mix-legend').addEventListener('click', e => {
  const row = e.target.closest('.mix-row');
  if (!row) return;
  const key = row.dataset.key;
  MIX_FOCUS = (MIX_FOCUS === key) ? null : key;
  renderModelMix();
});
document.getElementById('mix-legend').addEventListener('mousemove', e => {
  const row = e.target.closest('.mix-row');
  if (!row) { hideChartTip(); return; }
  const s = mixSlices().find(x => x.key === row.dataset.key);
  if (!s) { hideChartTip(); return; }
  showChartTip(mixTipHtml(s)
    + `<div class="t-p muted" style="font-size:11.5px">点击只看它，再点一次恢复</div>`,
    row.getBoundingClientRect());
});
document.getElementById('mix-legend').addEventListener('mouseleave', hideChartTip);
