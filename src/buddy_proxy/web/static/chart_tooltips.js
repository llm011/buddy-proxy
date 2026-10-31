// 打卡日历悬停 tooltip（自绘，即时显示：日期 + 星期 + 各通道当日领取）
const calTip = document.getElementById('cal-tip');
document.getElementById('cal').addEventListener('mousemove', e => {
  const cell = e.target.closest('.day');
  const tip = cell && !cell.classList.contains('blank')
    && window._calTips && window._calTips[cell.dataset.date];
  if (!tip) { calTip.style.display = 'none'; return; }
  calTip.innerHTML = tip;
  calTip.style.display = 'block';
  const r = cell.getBoundingClientRect();
  let x = r.left + r.width / 2 - calTip.offsetWidth / 2;
  x = Math.max(8, Math.min(x, window.innerWidth - calTip.offsetWidth - 8));
  let y = r.top - calTip.offsetHeight - 8;
  if (y < 8) y = r.bottom + 8;
  calTip.style.left = x + 'px';
  calTip.style.top = y + 'px';
});
document.getElementById('cal').addEventListener('mouseleave', () => {
  calTip.style.display = 'none';
});

// 请求趋势图表悬停 tooltip（与日历同款即时悬浮框）
const chartTip = document.getElementById('chart-tip');
function showChartTip(html, rect) {
  chartTip.innerHTML = html;
  chartTip.style.display = 'block';
  let x = rect.left + rect.width / 2 - chartTip.offsetWidth / 2;
  x = Math.max(8, Math.min(x, window.innerWidth - chartTip.offsetWidth - 8));
  let y = rect.top - chartTip.offsetHeight - 8;
  if (y < 8) y = rect.bottom + 8;
  chartTip.style.left = x + 'px';
  chartTip.style.top = y + 'px';
}
function hideChartTip() { chartTip.style.display = 'none'; }

document.getElementById('chart-daily').addEventListener('mousemove', e => {
  const hz = e.target.closest('rect.hitzone');
  const d = hz && DAILY_VIEW[+hz.dataset.i];
  if (!d) { hideChartTip(); return; }
  const lines = Object.entries(d.by_provider).sort((a, b) => b[1] - a[1])
    .map(([p, n]) => `<div class="t-p"><i style="background:${pcolor(p)}"></i>${esc(p)}<b style="margin-left:auto;padding-left:12px">${n}</b></div>`).join('');
  showChartTip(
    `<div class="t-date">${esc(d.date)} · 合计 ${d.total} 次${d.errors ? ` · 错误 ${d.errors}` : ''}</div>` +
    (lines || '<div class="t-p muted">无请求</div>'),
    hz.getBoundingClientRect());
});
document.getElementById('chart-daily').addEventListener('mouseleave', hideChartTip);

document.getElementById('chart-models').addEventListener('mousemove', e => {
  const row = e.target.closest('[data-mi]');
  const m = row && RANGE_MODELS[+row.dataset.mi];
  if (!m) { hideChartTip(); return; }
  showChartTip(
    `<div class="t-date"><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${pcolor(m.provider)};margin-right:6px"></i>${esc(m.provider)} / ${esc(m.model)}</div>` +
    `<div class="t-p">请求 <b style="margin-left:auto">${m.count}</b> 次 · 错误 ${m.errors}</div>` +
    `<div class="t-p">平均 ${fmtMs(m.avg_ms)} · 最长 ${fmtMs(m.duration_ms_max)}</div>` +
    `<div class="t-p">Tokens <b style="margin-left:auto">↑${fmtNum(m.prompt_tokens)} ↓${fmtNum(m.completion_tokens)}</b></div>` +
    `<div class="t-p muted">↑ 为全部输入（含缓存命中部分）</div>` +
    (m.last_ts ? `<div class="t-p muted">最近 ${fmtTime(m.last_ts)}</div>` : ''),
    row.getBoundingClientRect());
});
document.getElementById('chart-models').addEventListener('mouseleave', hideChartTip);

// 模型平均耗时图：悬停某通道列 → 该通道下各模型耗时明细
document.getElementById('chart-latency').addEventListener('mousemove', e => {
  const hz = e.target.closest('rect.hitzone');
  const p = hz && hz.dataset.p;
  if (!p || !LATENCY_DATA) { hideChartTip(); return; }
  const { topNames, byModel, mcolor } = LATENCY_DATA;
  const shown = LATENCY_MODEL ? [LATENCY_MODEL] : topNames;
  const rows = shown.map(n => ({ n, m: byModel.get(n).provs[p] }))
    .filter(r => r.m).sort((a, b) => b.m.avg_ms - a.m.avg_ms);
  if (!rows.length) { hideChartTip(); return; }
  const total = rows.reduce((s, r) => s + r.m.count, 0);
  const lines = rows.map(r =>
    `<div class="t-p"><i style="background:${mcolor.get(r.n)}"></i>${esc(r.n)}` +
    `<b style="margin-left:auto;padding-left:12px">${r.m.count} 次 · ${fmtMs(r.m.avg_ms)}</b></div>`).join('');
  showChartTip(
    `<div class="t-date">${esc(p)} · 合计 ${total} 次 · 平均耗时降序</div>` + lines,
    hz.getBoundingClientRect());
});
document.getElementById('chart-latency').addEventListener('mouseleave', hideChartTip);
