// ---- 模型平均耗时对比：同一模型在各通道的分组柱状图（纯 SVG）----
// 模型平均耗时图：x 轴为通道，柱为各模型；LATENCY_MODEL = null → 各模型并列，否则只看该模型
let LATENCY_MODEL = null;
// renderLatency 产出的视图数据（供 mousemove tooltip 读取）
let LATENCY_DATA = null;
function renderLatency() {
  const models = RANGE_MODELS;
  const el = document.getElementById('chart-latency');
  if (!models.length) {
    el.innerHTML = '<div class="empty">范围内暂无请求数据</div>';
    document.getElementById('legend-latency').innerHTML = '';
    LATENCY_DATA = null;
    return;
  }
  const providers = [...new Set(models.map(m => m.provider))];
  // 按模型聚合跨通道数据，取请求量前 8（与模型 Top10 口径一致）
  const byModel = new Map();
  for (const m of models) {
    const e = byModel.get(m.model) || { total: 0, provs: {} };
    e.total += m.count;
    e.provs[m.provider] = m;
    byModel.set(m.model, e);
  }
  const top = [...byModel.entries()].sort((a, b) => b[1].total - a[1].total).slice(0, 8);
  const topNames = top.map(([n]) => n);
  // 数据刷新后所选模型可能已无请求，自动退回“全部”
  if (LATENCY_MODEL && !topNames.includes(LATENCY_MODEL)) LATENCY_MODEL = null;
  const filt = LATENCY_MODEL;
  const shown = filt ? [filt] : topNames;
  // 模型固定 8 色板（柱色区分模型；通道色仍用于请求趋势图）
  const MCOLORS = ['#5b8def', '#e5a54b', '#4fc48f', '#e06c9f', '#9a7be0', '#4fb8d8', '#c9d05a', '#e07b5b'];
  const mcolor = new Map(topNames.map((n, i) => [n, MCOLORS[i % MCOLORS.length]]));
  LATENCY_DATA = { providers, topNames, byModel, mcolor };
  const W = 1100, H = 250, padL = 46, padB = 24, padT = 18; // 全宽卡片：viewBox≈实际显示宽度，避免整体被拉伸放大
  const base = H - padB;
  const max = Math.max(1, ...providers.flatMap(p =>
    shown.map(n => (byModel.get(n).provs[p] || {}).avg_ms || 0)));
  const slot = providers.length ? (W - padL - 8) / providers.length : 1;
  const scale = (base - padT) / max;

  let svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">`;
  for (let i = 0; i <= 4; i++) {
    const y = base - (base - padT) * i / 4;
    svg += `<line x1="${padL}" y1="${y}" x2="${W-4}" y2="${y}" stroke="#262f3c" stroke-width="1"/>` +
           `<text x="${padL-6}" y="${y+4}" fill="#8b96a5" font-size="11" text-anchor="end">${fmtMs(Math.round(max * i / 4))}</text>`;
  }
  providers.forEach((p, i) => {
    const x0 = padL + i * slot;
    const n = shown.length;
    const inner = slot - 14;
    const bw = Math.min(38, Math.max(5, (inner - (n - 1) * 3) / n));
    const xStart = x0 + (slot - (n * bw + (n - 1) * 3)) / 2;
    // 组内最慢的模型标数值；单模型视图每根都标
    let slowest = null;
    for (const name of shown) {
      const m = byModel.get(name).provs[p];
      if (m && (!slowest || m.avg_ms > slowest.avg_ms)) slowest = m;
    }
    shown.forEach((name, k) => {
      const m = byModel.get(name).provs[p];
      if (!m) return;
      const h = Math.max(0, m.avg_ms * scale);
      const x = xStart + k * (bw + 3);
      const y = base - h;
      svg += `<rect x="${x}" y="${y}" width="${bw}" height="${h}" fill="${mcolor.get(name)}" rx="2"/>`;
      if (n === 1 || m === slowest)
        svg += `<text x="${x + bw/2}" y="${Math.max(padT + 8, y - 3)}" fill="#dbe4ee" font-size="10.5" text-anchor="middle">${fmtMs(m.avg_ms)}</text>`;
    });
    svg += `<text x="${x0 + slot/2}" y="${H - 6}" fill="#8b96a5" font-size="11.5" text-anchor="middle">${esc(p)}</text>`;
    // 整列悬停热区：移入即显示该通道下各模型耗时明细
    svg += `<rect class="hitzone" data-p="${esc(p)}" x="${x0}" y="${padT}" width="${slot}" height="${base - padT}" fill="transparent"/>`;
  });
  svg += '</svg>';
  el.innerHTML = svg;

  // 图例（可点选）：全部 + 请求量前 8 的模型（点模型只看它）
  const items = [`<span class="legend-item${filt ? '' : ' active'}" data-model="" title="点击显示所有模型">全部</span>`]
    .concat(topNames.map(n =>
      `<span class="legend-item${filt === n ? ' active' : ''}" data-model="${esc(n)}" title="只显示 ${esc(n)}">
         <i style="background:${mcolor.get(n)}"></i>${esc(n)}</span>`));
  document.getElementById('legend-latency').innerHTML = items.join('');
  document.getElementById('latency-sub').textContent = filt
    ? `仅 ${filt} · ${rangeLabel()} 平均耗时`
    : `${rangeLabel()} · 各通道下模型并列对比（点下方图例可只看某模型）`;
}
// 图例点选：点某模型只看它；再点一次或点「全部」回到并列视图
 document.getElementById('legend-latency').addEventListener('click', e => {
  const item = e.target.closest('.legend-item');
  if (!item) return;
  const n = item.dataset.model; // '' = 全部
  const next = (n === '') ? null : n;
  LATENCY_MODEL = (LATENCY_MODEL === next && next !== null) ? null : next;
  renderLatency();
});
