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

// ---- 模型分组表 ----
// 分组折叠状态（localStorage 持久化；30s 自动刷新重渲染后仍保持）
let COLLAPSED = new Set();
try { COLLAPSED = new Set(JSON.parse(localStorage.getItem('bp-collapsed') || '[]')); } catch (e) {}
function persistCollapsed() {
  try { localStorage.setItem('bp-collapsed', JSON.stringify([...COLLAPSED])); } catch (e) {}
}
// 搜索只改变视图，不改 COLLAPSED（含 localStorage 中的偏好）。
let MODEL_QUERY = '';
function filteredModelGroups(groups, query) {
  const q = query.trim().toLowerCase();
  if (!q) return groups;
  const matches = value => String(value || '').toLowerCase().includes(q);
  return groups.flatMap(g => {
    if (matches(g.id) || matches(g.name)) return [g];
    const models = g.models.filter(m => matches(m.id) || matches(m.name));
    return models.length ? [{...g, models}] : [];
  });
}
function clearModelSearch() {
  const input = document.getElementById('model-search');
  input.value = '';
  MODEL_QUERY = '';
  const clearBtn = document.getElementById('model-search-clear');
  if (clearBtn) clearBtn.hidden = true;
  renderGroups();
  input.focus();
}
document.getElementById('model-search').addEventListener('input', e => {
  MODEL_QUERY = e.target.value.trim();
  const clearBtn = document.getElementById('model-search-clear');
  if (clearBtn) clearBtn.hidden = !e.target.value;
  renderGroups();
});
document.getElementById('model-search').addEventListener('keydown', e => {
  if (e.key === 'Escape') { e.preventDefault(); clearModelSearch(); }
});
document.getElementById('model-search-clear').addEventListener('click', clearModelSearch);
document.getElementById('groups').addEventListener('click', e => {
  if (e.target.closest('[data-clear-model-search]')) clearModelSearch();
});
function toggleGroup(gid) {
  if (MODEL_QUERY) return;
  if (COLLAPSED.has(gid)) COLLAPSED.delete(gid); else COLLAPSED.add(gid);
  persistCollapsed();
  const g = document.querySelector(`.group[data-gid="${CSS.escape(gid)}"]`);
  if (g) g.classList.toggle('collapsed', COLLAPSED.has(gid));
}
function setAllGroups(collapse) {
  if (MODEL_QUERY) return;
  COLLAPSED = collapse ? new Set((MODELS.groups || []).map(g => g.id)) : new Set();
  persistCollapsed();
  document.querySelectorAll('#groups .group').forEach(g =>
    g.classList.toggle('collapsed', COLLAPSED.has(g.dataset.gid)));
}
// 点击通道条或列头（表头）任意处 → 折叠/展开该组
document.getElementById('groups').addEventListener('click', e => {
  const g = e.target.closest('.group');
  if (!g) return;
  if (e.target.closest('.group-head') || e.target.closest('thead th')) {
    if (!e.target.closest('button')) toggleGroup(g.dataset.gid);
  }
});

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

function renderGroups() {
  const el = document.getElementById('groups');
  // 搜索时基于原始 MODELS.groups 生成过滤视图（浅拷贝组、过滤模型行），
  // 不改数据源；COLLAPSED 完全不参与搜索态的折叠判断。
  const searching = MODEL_QUERY.trim() !== '';
  const all = MODELS.groups || [];
  const groups = searching ? filteredModelGroups(all, MODEL_QUERY) : all;
  const shownModels = groups.reduce((n, g) => n + g.models.length, 0);
  const dm = MODELS.default_model || '';
  document.getElementById('model-summary').textContent = searching
    ? `命中 ${groups.length} 个通道 · ${shownModels} 个模型 · 搜索「${MODEL_QUERY.trim()}」`
    : `${all.length} 个通道 · ${shownModels} 个模型` + (dm ? ` · 默认 ${dm}` : '');
  const hidden = (MODELS.hidden_models || []).length;
  const hiddenBtn = document.getElementById('hidden-models-btn');
  if (hiddenBtn) hiddenBtn.textContent = `已隐藏${hidden ? ' ' + hidden : ''}`;
  // 搜索态下视图强制全展开、且不允许写折叠偏好，全部展开/折叠按钮禁用
  ['models-expand', 'models-collapse'].forEach(id => {
    const b = document.getElementById(id);
    if (b) b.disabled = searching;
  });
  if (!groups.length) {
    el.innerHTML = searching
      ? `<div class="empty">没有匹配「${esc(MODEL_QUERY.trim())}」的通道或模型 <button class="ghost" data-clear-model-search>清空搜索</button></div>`
      : '<div class="empty">没有可用通道</div>';
    return;
  }
  el.innerHTML = groups.map(g => {
    const h = g.health || {};
    const okFlag = (g.id === 'codebuddy') ? (h.authenticated && h.token_valid !== false)
                 : ('configured' in h ? h.configured !== false : (h.ready !== false && h.started !== false));
    const healthTxt = g.id === 'codebuddy' ? (okFlag ? '已登录' : '未登录')
                    : h.base_url ? esc(h.base_url.replace(/^https?:\/\//, '')) : (okFlag ? '正常' : '未就绪');
    const rows = g.models.map(m => {
      const st = m.stats || {};
      const dis = !!m.disabled;
      const sch = m.schedule || null;
      const schWins = sch ? sch.windows.map(w => w[0] + '~' + w[1]).join(', ') : '';
      const schTag = sch
        ? `<span class="tag ${sch.open ? 'ok' : 'bad'}" title="限时可用：${esc(schWins)}${sch.open ? '（当前开放）' : '（当前关闭，窗口外调用被拒）'}">⏰ ${sch.open ? '开放中' : '限时'}</span>`
        : '';
      const schWinsAttr = sch ? esc(JSON.stringify(sch.windows)) : '[]';
      const dim = dis ? ' style="opacity:.5"' : '';
      const ord = m.order || null;
      const ordMarks = ord ? ord.marks : [];
      const ordTitle = ord
        ? '候选上游顺序：' + ord.targets.join(' → ')
          + (ordMarks.length ? '；冷却中：' + ordMarks.map(k => k.target).join(', ') : '')
        : '';
      const ordTag = ord
        ? `<span class="tag ${ordMarks.length ? 'bad' : 'ok'}" title="${esc(ordTitle)}">⇄ ${ord.targets.length}${ordMarks.length ? ` <span style="cursor:pointer" title="冷却中，点击清除" onclick='clearOrderMarks(${jsq(g.id)},${jsq(m.id)})'>⏸${ordMarks.length}</span>` : ''}</span>`
        : '';
      return `<tr>
        <td${dim}><span class="mono">${esc(m.id)}</span>
            ${m.is_default ? '<span class="badge-default">✦ 默认</span>' : ''}
            ${dis ? '<span class="tag bad">已停用</span>' : ''}
            ${schTag}
            ${ordTag}</td>
        <td class="muted"${dim}>${esc(m.name || '')}</td>
        <td${dim}>${m.credits != null ? `<span class="tag">${esc(m.credits)}</span>` : ''}${m.support ? `<span class="tag" title="受账号限制，仅支持：${esc((m.support.accounts || []).join('、'))}（转发时自动跳过不支持的账号）">部分账号</span>` : ''}${m.tier ? `<span class="tag">${esc(m.tier)}</span>` : ''}${m.reasoning ? '<span class="tag">reasoning</span>' : ''}</td>
        <td class="num mono muted"${dim}>${st.count || '—'}</td>
        <td class="num mono muted"${dim}>${st.count ? fmtMs(st.avg_ms) : '—'}</td>
        <td class="num" style="white-space:nowrap">
            ${(m.is_default || dis) ? '' : `<button title="客户端请求未带 model 字段时，自动改用此模型（不影响已指定 model 的请求）" onclick='setDefault(${jsq(g.id)},${jsq(m.id)})'>设为默认</button>`}
            <button class="primary" onclick='runTest(${jsq(g.id)},${jsq(m.id)})'>测试</button>
            <button class="ghost" title="设置限时可用时段：仅在窗口内可调用，窗口外直接失败（支持跨天）" onclick='openScheduleModal(${jsq(g.id)},${jsq(m.id)},${schWinsAttr})'>${sch ? '时段·' + sch.windows.length : '时段'}</button>
            <!-- 候选顺序的编辑入口只在「模型顺序」页签：这张表行多列窄，塞进去
                 既挤又难找（用户当初就是在这一列里找不到它）。行内只留一个
                 徽标，说明「这个模型已配顺序、有几档、几档在冷却」。 -->
            ${ordMarks.length ? `<button class="ghost" title="清除冷却标记，立刻重新尝试这些目标" onclick='clearOrderMarks(${jsq(g.id)},${jsq(m.id)})'>清冷却</button>` : ''}
            <button class="${dis ? 'primary' : 'ghost'}" title="${dis ? '重新启用后可正常调用' : '停用后调用此模型的请求直接失败'}" onclick='toggleModel(${jsq(g.id)},${jsq(m.id)},${dis ? 'false' : 'true'})'>${dis ? '启用' : '停用'}</button>
            <button class="ghost" title="只从模型列表和选择器隐藏；直接点名仍可调用" onclick='hideModel(${jsq(g.id)},${jsq(m.id)})'>隐藏</button>
        </td></tr>`;
    }).join('');
    const gdis = !!g.disabled;
    const isDefaultProv = g.id === 'codebuddy';
    const swTitle = isDefaultProv ? '默认兜底通道，不能停用'
      : (gdis ? '重新启用该通道' : '停用该通道：调用直接失败，额度页不再展示，顺序页模型置灰');
    return `<div class="group${!searching && COLLAPSED.has(g.id) ? ' collapsed' : ''}" data-gid="${esc(g.id)}"${gdis ? ' style="opacity:.55"' : ''}>
      <div class="group-head" title="点击折叠/展开">
        <span class="dot" style="background:${gdis ? 'var(--muted)' : (okFlag ? 'var(--ok)' : 'var(--err)')}"></span>
        <span class="name">${esc(g.name)}</span>
        <span class="tag">${g.id}</span>
        ${gdis ? '<span class="tag bad">已停用</span>' : ''}
        <span class="tag ${okFlag ? 'ok' : 'bad'}">${healthTxt}</span>
        <label style="display:flex;align-items:center;gap:4px;font-size:12px;margin-left:6px;cursor:${isDefaultProv ? 'not-allowed' : 'pointer'}" title="${swTitle}" onclick="event.stopPropagation()">
          <input type="checkbox" ${gdis ? '' : 'checked'} ${isDefaultProv ? 'disabled' : ''} onchange='toggleProvider(${jsq(g.id)}, this.checked)'> 启用
        </label>
        <span class="spacer"></span>
        ${g.refreshable ? `<button class="ghost" title="从上游官方/逆向目录强制重新拉取，发现新增模型" onclick='event.stopPropagation();refreshProviderModels(${jsq(g.id)},this)'>↻ 重新拉取</button>` : ''}
        <span class="muted" style="font-size:12px">${g.models.length} 个模型</span>
        <span class="chev">▾</span>
      </div>
      <table>
        <thead><tr><th>模型 ID</th><th>名称</th><th>标签</th>
          <th class="num" title="本代理转发的、近 14 天的成功+失败请求数。按实际转发的通道和模型名统计（不是按客户端原始请求里的名字），最久保留 30 天。">近 14 天请求</th>
          <th class="num" title="仅统计有耗时的请求">平均耗时</th><th class="num">操作</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;
  }).join('');
  refreshStickyVars();
}

let RECENT_PAGE = 1, RECENT_SIZE = 20;
let RECENT_SEQ = 0;  // 并发防抖：仅最后一次请求的结果生效（范围/翻页快速切换时）
let RECENT_QS = null;  // 已渲染结果对应的查询串；相同则跳过（多次 render 不重复打请求）

function recentGo(p) { RECENT_PAGE = p; renderRecent(); }
function recentSize(v) { RECENT_SIZE = +v; RECENT_PAGE = 1; renderRecent(); }

// 页码序列：首尾常驻，当前页 ±1，间隔以 '…' 占位
function recentPages(cur, total) {
  const wanted = new Set([1, total, cur - 1, cur, cur + 1]);
  const pages = [...wanted].filter(p => p >= 1 && p <= total).sort((a, b) => a - b);
  const out = [];
  let prev = 0;
  for (const p of pages) {
    if (p - prev > 1) out.push('…');
    out.push(p);
    prev = p;
  }
  return out;
}

// 服务端分页：直接读 logs/metrics.jsonl + 30 天归档，突破进程内 200 条上限。
// 该接口最慢（要全量扫归档），故对「相同查询」直接跳过（RECENT_QS），
// 并在飞请求由 api() 的 GET 去重兜住。
async function renderRecent() {
  // 日志页不可见时直接跳过：翻页/筛选/范围变化都可能调到这里，别在其它页签白跑。
  if (currentTab() !== 'log') return;
  const el = document.getElementById('recent');
  const pager = document.getElementById('recent-pager');
  document.getElementById('recent-sub').textContent =
    `${rangeLabel()} · 读磁盘日志（最多 30 天归档），服务端分页；TTFT 为首 token 延迟（流式）`;
  let qs = `start=${encodeURIComponent(RANGE.start)}&end=${encodeURIComponent(RANGE.end)}` +
           `&page=${RECENT_PAGE}&page_size=${RECENT_SIZE}`;
  if (LOG_FILTERS.provider.size)
    qs += `&provider=${encodeURIComponent([...LOG_FILTERS.provider].join(','))}`;
  if (LOG_FILTERS.model.size)
    qs += `&model=${encodeURIComponent([...LOG_FILTERS.model].join(','))}`;
  if (LOG_FILTERS.client.size)
    qs += `&client=${encodeURIComponent([...LOG_FILTERS.client].join(','))}`;

  // 同一查询已渲染过 → 直接跳过。首屏/切页签/轮询会多次 render(),
  // 该接口要全量扫归档，重复请求代价最高。
  if (qs === RECENT_QS) return;

  const seq = ++RECENT_SEQ;
  let resp;
  try {
    resp = await api('/ui/api/logs?' + qs);
  } catch (e) {
    if (seq !== RECENT_SEQ) return;  // 已被更晚的请求取代
    el.innerHTML = `<tr><td colspan="9" class="empty">加载失败：${esc(e.message)}</td></tr>`;
    pager.innerHTML = '';
    return;
    // 失败路径不记 RECENT_QS：下次渲染（轮询/切回页签）应当重试，而不是拿着旧视图空转
  }
  if (seq !== RECENT_SEQ) return;  // 已被更晚的请求取代
  RECENT_QS = qs;  // 记录已渲染的查询。所有 return 分支都在此行之后，
                   // 故「空结果 / 无行」也照样记，不会切走再回来白重跑一次全量扫描
  const rows = resp.rows || [];
  const total = resp.total || 0;
  const pages = resp.pages || 1;
  RECENT_PAGE = resp.page || 1;  // 后端已收敛到合法区间
  // 客户端筛选候选随日志数据刷新（按出现次数降序，不随已选筛选收缩）
  RANGE_CLIENTS = resp.clients || [];
  renderLogFilters();
  if (!rows.length) {
    const filtered = LOG_FILTERS.provider.size || LOG_FILTERS.model.size || LOG_FILTERS.client.size;
    el.innerHTML = `<tr><td colspan="9" class="empty">${
      filtered ? '当前筛选条件下没有请求记录' : (total ? '所选范围内没有请求记录' : '还没有请求记录')}</td></tr>`;
    pager.innerHTML = '';
    return;
  }
  const cmap = (STATS || {}).credits_map || {};
  el.innerHTML = rows.map(r => {
    const bad = (r.status >= 400 || r.error);
    const ttft = r.ttft_ms != null ? fmtMs(r.ttft_ms) : '—';
    const hasTok = (r.prompt_tokens || r.completion_tokens || r.cached_tokens);
    let tok = '—', tokTitle = '';
    if (hasTok) {
      const cacheTxt = r.cached_tokens ? ` · 其中缓存命中 ${fmtNum(r.cached_tokens)}` : '';
      tok = `↑${fmtNum(r.prompt_tokens)} ↓${fmtNum(r.completion_tokens)}`;
      tokTitle = `输入 ${r.prompt_tokens}（↑ 含缓存命中）· 输出 ${r.completion_tokens}${cacheTxt}`;
    }
    // 实扣积分留 2 位展示；credits_map 那档本来就是 "×1.62" 这种字符串，原样带过
    const creditTxt = r.credit != null ? fmtCredit(r.credit)
                    : cmap[r.provider + '/' + r.model] || null;
    // 多账号通道：括号内展示账号名。日志落库的是稳定 id，后端在 /ui/api/logs
    // 按各通道当前 alias 链解析成 account_name（改名后历史行跟着变）。解析不到
    // （trae 旧行存的当时 alias、或已删号）原样显示：UUID 形态截前 6 位、其余
    // 截 12 位，hover 看完整原始值。
    const shown = r.account_name || r.account;
    const acct = r.account
      ? ` <span class="muted acct-name" title="${r.account_name ? '账号 id：' : '账号：'}${esc(r.account)}">(${esc(shown)})</span>`
      : '';
    return `<tr>
      <td class="mono muted">${fmtTime(r.ts)}</td>
      <td><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${pcolor(r.provider)};margin-right:6px"></i><span class="mono muted">${esc(r.provider)}</span>${acct}</td>
      <td class="mono">${esc(r.model)}</td>
      <td class="mono muted" title="客户端来源：X-Client-Name 自声明 > UA 推断（旧记录无此字段）">${r.client ? esc(r.client) : '<span class="muted">—</span>'}</td>
      <td class="num mono">${r.ttft_ms != null ? ttft : '—'}</td>
      <td class="num mono">${r.duration_ms ? fmtMs(r.duration_ms) : '—'}</td>
      <td class="num mono" title="${esc(tokTitle)}">${tok}${r.cached_tokens ? ` <span class="tag">缓 ${fmtNum(r.cached_tokens)}</span>` : ''}</td>
      <td class="num mono">${r.credit != null ? (r.credit_estimated
          ? `<b style="color:var(--ok)" title="本地估算，非上游实扣：trae 已实测模型按官方账单校准的输入/输出单价×token（随倍率面板调价自动缩放；会话首请求实测偏高约 2~3 倍），未实测模型按倍率粗估；zcode 按 GLM Coding Plan 官方抵扣系数（含时段折扣）">≈${creditTxt}</b>`
          : `<b style="color:var(--ok)" title="上游实扣积分（原始值 ${esc(r.credit)}）">${creditTxt}</b>`)
        : (creditTxt ? `<span class="tag" title="仅倍率参考（×基准单价），非本次实际消耗；${esc(r.provider)} 不提供单次消耗">${esc(creditTxt)}</span>` : '<span class="muted" title="该上游未提供单次积分消耗">—</span>')}</td>
      <td>${bad ? `<span class="tag bad" title="${esc(r.error || '')}">${r.status || 'ERR'}</span>` : '<span class="tag ok">OK</span>'}</td>
    </tr>`;
  }).join('');
  const nav = (label, page, disabled) =>
    `<button class="ghost" ${disabled ? 'disabled' : ''} onclick="recentGo(${page})">${label}</button>`;
  pager.innerHTML =
    `<span class="muted">共 ${total} 条</span>` +
    `<select title="每页条数" onchange="recentSize(this.value)">` +
      [20, 50, 100].map(n => `<option value="${n}" ${n === RECENT_SIZE ? 'selected' : ''}>${n} 条/页</option>`).join('') +
    `</select>` +
    `<span class="spacer"></span>` +
    nav('«', 1, RECENT_PAGE === 1) +
    nav('‹', RECENT_PAGE - 1, RECENT_PAGE === 1) +
    recentPages(RECENT_PAGE, pages).map(p => p === '…'
      ? '<span class="muted">…</span>'
      : `<button class="${p === RECENT_PAGE ? 'primary' : 'ghost'}" onclick="recentGo(${p})">${p}</button>`).join('') +
    nav('›', RECENT_PAGE + 1, RECENT_PAGE === pages) +
    nav('»', pages, RECENT_PAGE === pages);
}

