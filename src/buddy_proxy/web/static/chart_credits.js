// ---- 积分消耗：按通道横向条 + 按天堆叠趋势（与请求趋势同款布局，对齐阅读）----
// 数据 = STATS.model_daily 里各桶累加的 credit_sum（上游实扣）/ credit_est_sum
// （本地估算，trae/zcode 等不给单次消耗的通道）。两档分开展示：混加会把真扣
// 和估的揉成一个数，看不出可信度。只报了倍率、没有单次消耗的通道（kimi 等）
// 不在图里——倍率×次数不是消耗，画了会误导。
let CREDITS_VIEW = [];
function creditsByProvider() {
  const per = new Map();  // provider -> {real, est}
  for (const b of (STATS && STATS.model_daily) || []) {
    if (!inRange(b.date)) continue;
    const real = b.credit_sum || 0, est = b.credit_est_sum || 0;
    if (!real && !est) continue;
    let e = per.get(b.provider);
    if (!e) per.set(b.provider, e = { real: 0, est: 0 });
    e.real += real; e.est += est;
  }
  return [...per.entries()].map(([provider, e]) => ({ provider, ...e }))
    .sort((a, b) => (b.real + b.est) - (a.real + a.est));
}
function renderCreditsBars() {
  const el = document.getElementById('chart-credits-providers');
  const rows = creditsByProvider();
  if (!rows.length) {
    el.innerHTML = '<div class="empty">范围内暂无积分消耗数据（仅统计上报了单次消耗的通道）</div>';
    return;
  }
  const max = Math.max(1e-9, ...rows.map(r => r.real + r.est));
  el.innerHTML = rows.map((r, i) => {
    const wReal = r.real / max * 100, wEst = r.est / max * 100;
    const total = r.real + r.est;
    const totalTxt = r.est && r.real ? `${fmtCredit(total)}（含估算）`
      : r.est ? `≈${fmtCredit(r.est)}` : fmtCredit(r.real);
    return `<div data-ci="${i}" style="display:flex;align-items:center;gap:10px;margin:7px 0;cursor:crosshair">
      <div class="mono" style="width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.provider)}">
        <i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${pcolor(r.provider)};margin-right:6px"></i>${esc(r.provider)}
      </div>
      <div class="bar-wrap" style="flex:1;display:flex">${
        wReal ? `<div class="bar" style="width:${Math.max(2, wReal)}%;background:${pcolor(r.provider)}"></div>` : ''}${
        wEst ? `<div class="bar" style="width:${Math.max(2, wEst)}%;background:${pcolor(r.provider)};opacity:.45" title="本地估算部分"></div>` : ''}
      </div>
      <div class="num muted" style="width:110px;font-size:12px;text-align:right">${totalTxt} 积分</div>
    </div>`;
  }).join('');
}

function renderCreditsDaily() {
  const md = (STATS && STATS.model_daily) || [];
  const days = [...new Set(md.filter(b => inRange(b.date) && (b.credit_sum || b.credit_est_sum))
    .map(b => b.date))].sort();
  const byDay = new Map(days.map(d => [d, new Map()]));  // date -> provider -> {real, est}
  const providers = [];
  for (const b of md) {
    if (!inRange(b.date)) continue;
    const real = b.credit_sum || 0, est = b.credit_est_sum || 0;
    if (!real && !est) continue;
    const dayMap = byDay.get(b.date);
    if (!dayMap) continue;
    let e = dayMap.get(b.provider);
    if (!e) dayMap.set(b.provider, e = { real: 0, est: 0 });
    e.real += real; e.est += est;
    if (!providers.includes(b.provider)) providers.push(b.provider);
  }
  const el = document.getElementById('chart-credits-daily');
  if (!days.length) { el.innerHTML = '<div class="empty">范围内暂无积分消耗数据</div>'; return; }
  const W = 520, H = 170, padL = 44, padB = 24, padT = 15;
  const base = H - padB;
  const slot = (W - padL - 8) / days.length;
  const bw = Math.min(30, slot - 8);
  const valOf = d => [...byDay.get(d).values()].reduce((s, e) => s + e.real + e.est, 0);
  const max = Math.max(1e-9, ...days.map(valOf));
  const scale = (base - padT) / max;
  CREDITS_VIEW = days.map(d => ({ date: d, by_provider: byDay.get(d), total: valOf(d) }));

  let svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">`;
  for (let i = 0; i <= 4; i++) {
    const y = base - (base - padT) * i / 4;
    const v = max * i / 4;
    svg += `<line x1="${padL}" y1="${y}" x2="${W-4}" y2="${y}" stroke="#262f3c" stroke-width="1"/>` +
           `<text x="${padL-6}" y="${y+4}" fill="#8b96a5" font-size="10" text-anchor="end">${v < 10 ? v.toFixed(1) : Math.round(v)}</text>`;
  }
  days.forEach((d, i) => {
    const x = padL + i * slot + (slot - bw) / 2;
    let y = base;
    for (const p of providers) {
      const e = byDay.get(d).get(p);
      if (!e) continue;
      // 实扣段在下（实色），估算段在上（半透明），与通道条的两段式一致
      for (const [v, op] of [[e.real, 1], [e.est, 0.45]]) {
        if (!v) continue;
        const h = v * scale;
        y -= h;
        svg += `<rect x="${x}" y="${y}" width="${bw}" height="${h}" fill="${pcolor(p)}" opacity="${op}" rx="2"/>`;
      }
    }
    const total = valOf(d);
    if (total) svg += `<text x="${x + bw/2}" y="${Math.max(padT + 7, y - 3)}" fill="#dbe4ee" font-size="9.5" text-anchor="middle">${fmtCredit(total)}</text>`;
    svg += `<text x="${x + bw/2}" y="${H - 6}" fill="#8b96a5" font-size="9.5" text-anchor="middle">${d.slice(5)}</text>`;
  });
  days.forEach((d, i) => {
    svg += `<rect class="hitzone" data-i="${i}" x="${padL + i * slot}" y="${padT}" width="${slot}" height="${base - padT}" fill="transparent"/>`;
  });
  svg += '</svg>';
  el.innerHTML = svg;
}

// 积分图 tooltip：通道条 → 实扣/估算分明细；按天柱 → 当日各通道分明细
document.getElementById('chart-credits-providers').addEventListener('mousemove', e => {
  const row = e.target.closest('[data-ci]');
  if (!row) { hideChartTip(); return; }
  const r = creditsByProvider()[+row.dataset.ci];
  if (!r) { hideChartTip(); return; }
  const lines = [`<div class="t-date"><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${pcolor(r.provider)};margin-right:6px"></i>${esc(r.provider)} · ${rangeLabel()}</div>`];
  if (r.real) lines.push(`<div class="t-p">上游实扣<b style="margin-left:auto;padding-left:12px">${fmtCredit(r.real)} 积分</b></div>`);
  if (r.est) lines.push(`<div class="t-p">本地估算<b style="margin-left:auto;padding-left:12px">≈${fmtCredit(r.est)} 积分</b></div>`);
  lines.push(`<div class="t-p muted">不含只报倍率、无单次消耗的通道</div>`);
  showChartTip(lines.join(''), row.getBoundingClientRect());
});
document.getElementById('chart-credits-providers').addEventListener('mouseleave', hideChartTip);

document.getElementById('chart-credits-daily').addEventListener('mousemove', e => {
  const hz = e.target.closest('rect.hitzone');
  const d = hz && CREDITS_VIEW[+hz.dataset.i];
  if (!d) { hideChartTip(); return; }
  const lines = [...d.by_provider.entries()]
    .sort((a, b) => (b[1].real + b[1].est) - (a[1].real + a[1].est))
    .flatMap(([p, v]) => {
      const rows = [];
      const label = `<i style="background:${pcolor(p)}"></i>${esc(p)}`;
      if (v.real) rows.push(`<div class="t-p">${label} · 上游实扣<b style="margin-left:auto;padding-left:12px">${fmtCredit(v.real)} 积分</b></div>`);
      if (v.est) rows.push(`<div class="t-p">${label} · 本地估算<b style="margin-left:auto;padding-left:12px">≈${fmtCredit(v.est)} 积分</b></div>`);
      return rows;
    }).join('');
  showChartTip(
    `<div class="t-date">${esc(d.date)} · 上游实扣与本地估算分项</div>` +
    (lines || '<div class="t-p muted">无消耗</div>'),
    hz.getBoundingClientRect());
});
document.getElementById('chart-credits-daily').addEventListener('mouseleave', hideChartTip);
