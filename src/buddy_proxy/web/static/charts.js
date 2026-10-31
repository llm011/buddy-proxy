// ---- 请求趋势柱状图（纯 SVG）。图例可点选：只显示某通道，或“全部”堆叠 ----
// DAILY_PROV = null → 堆叠展示全部通道；否则只展示该通道单色柱
let DAILY_PROV = null;
// 当前范围过滤后的 daily（供柱状图悬停 tooltip 读取，索引与图内一致）
let DAILY_VIEW = [];
function renderDaily() {
  const daily = (STATS.daily || []).filter(d => inRange(d.date));
  DAILY_VIEW = daily;
  const W = 520, H = 190, padL = 34, padB = 24, padT = 15;
  const providers = [...new Set(daily.flatMap(d => Object.keys(d.by_provider)))];
  const filt0 = DAILY_PROV;
  // 若所选通道已不在当前范围窗口内（数据/范围刷新后），自动退回“全部”
  if (filt0 && !providers.includes(filt0)) DAILY_PROV = null;
  const filt = DAILY_PROV; // null = 全部(堆叠)
  // 当前口径下每天的值：堆叠取 total，过滤取该通道数
  const valOf = d => filt ? (d.by_provider[filt] || 0) : (d.total || 0);
  const max = Math.max(1, ...daily.map(valOf));
  const base = H - padB;
  const slot = daily.length ? (W - padL - 8) / daily.length : 1;
  const bw = Math.min(30, slot - 8);
  const scale = (base - padT) / max; // 每单位像素高度

  let svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">`;
  // 横格线
  for (let i = 0; i <= 4; i++) {
    const y = base - (base - padT) * i / 4;
    const v = max * i / 4;
    const lbl = (Number.isInteger(v) && v < 10) || v >= 10 ? Math.round(v) : v.toFixed(1);
    svg += `<line x1="${padL}" y1="${y}" x2="${W-4}" y2="${y}" stroke="#262f3c" stroke-width="1"/>` +
           `<text x="${padL-6}" y="${y+4}" fill="#8b96a5" font-size="10" text-anchor="end">${lbl}</text>`;
  }
  daily.forEach((d, i) => {
    const x = padL + i * slot + (slot - bw) / 2;
    if (filt) {
      const n = valOf(d);
      if (n) {
        const h = n * scale;
        const y = base - h;
        svg += `<rect x="${x}" y="${y}" width="${bw}" height="${h}" fill="${pcolor(filt)}" rx="2"/>` +
               `<text x="${x + bw/2}" y="${Math.max(padT + 7, y - 3)}" fill="#dbe4ee" font-size="9.5" text-anchor="middle">${n}</text>`;
      }
    } else {
      // 堆叠：自下而上逐通道画段
      let y = base;
      for (const p of providers) {
        const n = d.by_provider[p] || 0; if (!n) continue;
        const h = n * scale;
        y -= h;
        svg += `<rect x="${x}" y="${y}" width="${bw}" height="${h}" fill="${pcolor(p)}" rx="2"/>`;
      }
      if (d.total) {
        svg += `<text x="${x + bw/2}" y="${Math.max(padT + 7, y - 3)}" fill="#dbe4ee" font-size="9.5" text-anchor="middle">${d.total}</text>`;
      }
    }
    if (valOf(d)) svg += `<text x="${x + bw/2}" y="${H - 6}" fill="#8b96a5" font-size="9.5" text-anchor="middle">${d.date.slice(5)}</text>`;
  });
  // 整列悬停热区：移入即显示当日明细（CSS hover 加高亮底色）
  daily.forEach((d, i) => {
    svg += `<rect class="hitzone" data-i="${i}" x="${padL + i * slot}" y="${padT}" width="${slot}" height="${base - padT}" fill="transparent"/>`;
  });
  svg += '</svg>';
  document.getElementById('chart-daily').innerHTML = svg;

  // 图例（可点选）：全部 + 各通道
  const items = [`<span class="legend-item${filt ? '' : ' active'}" data-prov="" title="点击回到堆叠视图">全部</span>`]
    .concat(providers.map(p =>
      `<span class="legend-item${filt === p ? ' active' : ''}" data-prov="${esc(p)}" title="只显示 ${esc(p)}">
         <i style="background:${pcolor(p)}"></i>${esc(p)}</span>`));
  document.getElementById('legend-daily').innerHTML = items.join('');

  // 副标题随过滤态更新
  document.getElementById('trend-sub').textContent = filt
    ? `${rangeLabel()} · 仅 ${filt}`
    : `${rangeLabel()} · 按通道堆叠（点下方图例可只看某通道）`;
}
// 图例点选：选中某通道只展示其消耗；再点一次或点「全部」回到堆叠视图
document.getElementById('legend-daily').addEventListener('click', e => {
  const item = e.target.closest('.legend-item');
  if (!item) return;
  const p = item.dataset.prov; // '' = 全部
  const next = (p === '') ? null : p;
  DAILY_PROV = (DAILY_PROV === next && next !== null) ? null : next; // 再点同通道 = 取消过滤
  renderDaily();
});

// ---- 模型请求 Top10 横向条 ----
function renderModelBars() {
  const models = RANGE_MODELS.slice(0, 10);
  const el = document.getElementById('chart-models');
  if (!models.length) { el.innerHTML = '<div class="empty">范围内暂无请求数据</div>'; return; }
  const max = Math.max(1, ...models.map(m => m.count));
  el.innerHTML = models.map((m, mi) => `
    <div data-mi="${mi}" style="display:flex;align-items:center;gap:10px;margin:7px 0;cursor:crosshair">
      <div class="mono" style="width:190px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(m.provider + '/' + m.model)}">
        <i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${pcolor(m.provider)};margin-right:6px"></i>${esc(m.model)}
      </div>
      <div class="bar-wrap" style="flex:1"><div class="bar" style="width:${Math.max(3, m.count/max*100)}%;background:${pcolor(m.provider)}"></div></div>
      <div class="num muted" style="width:120px;font-size:12px">${m.count} 次 · ${m.avg_ms ? fmtMs(m.avg_ms) : '—'}</div>
    </div>`).join('');
}

