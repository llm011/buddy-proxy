// ---- LOOMY 官方积分明细弹窗 ----
//
// Loomy 走**通用额度卡**（没有专属面板，见 benefits.js 的 _PANEL_PROVIDERS
// 注释：只有真做了专属面板才加进排除表），明细按钮由 loomyLogsButton() 内联
// 挂到卡头，和 zcode 的重置券按钮同一个位置。
//
// 数据来自 /ui/api/loomy/usage-logs，两种视图：
//   - ledger  逐笔流水（/api/v1/points/records）：每笔一次模型调用，带
//             modelName / pointsActual / balanceBefore→After
//   - purpose 按功能汇总（/api/v2/points/records + granularity=purpose）：
//             chat.message / image.generate 各一行，带聚合笔数
//
// 分页是 offset（上游有 total），可跳页。复用 benefits_usage_logs.js 的
// 弹窗骨架（#modal-body / #modal-foot / closeModal / api / esc），但状态独立
// （那套按 USAGE_LOGS_CFG 表驱动，塞进来会把 loomy 混进「官方消耗明细」通用表）。

let LOOMY_USAGE_VIEW = 'ledger';   // 'ledger' 逐笔 / 'purpose' 按功能汇总
let LUL = null;                    // 当前弹窗状态；null = 没打开

function resetLoomyUsageLogs() {
  // 与 resetUsageLogs 同理：关闭后仍有在途请求时必须让响应失效，
  // 否则它会覆盖随后打开的其它弹窗。
  if (LUL) LUL.seq += 1;
  LUL = null;
}

// 视图切换放在**正文顶部**而不是底栏：620px 弹窗的底栏还要放翻页、总数和
// 关闭，塞进去会溢出——实测「逐笔流水」被裁成「流水」。
function _loomyViewTabs() {
  const mk = (v, label) => {
    const on = LOOMY_USAGE_VIEW === v;
    return `<button class="${on ? 'primary' : 'ghost'}" ` +
      `onclick="loomyUsageSwitch('${v}')">${label}</button>`;
  };
  return `<div style="display:flex;gap:8px;margin-bottom:10px">` +
    mk('ledger', '逐笔流水') + mk('purpose', '按功能汇总') + `</div>`;
}

async function openLoomyUsageLogs() {
  LUL = {page: 1, pageSize: 30, total: 0, fetching: false, seq: (LUL ? LUL.seq : 0) + 1};
  document.getElementById('modal-title').textContent = 'Loomy 官方积分明细';
  document.getElementById('modal-body').innerHTML =
    '<span class="spin"></span>拉取 Loomy 官方积分记录…';
  setModalFoot('');
  document.getElementById('overlay').classList.add('show');
  await loomyUsageFetch();
}

function loomyUsageSwitch(view) {
  if (!LUL || view === LOOMY_USAGE_VIEW) return;
  LOOMY_USAGE_VIEW = view;
  LUL.page = 1;
  LUL.total = 0;
  LUL.seq += 1;
  loomyUsageFetch();
}

async function loomyUsageGo(page) {
  if (!LUL || LUL.fetching || page === LUL.page) return;
  LUL.page = page;
  await loomyUsageFetch();
}

async function loomyUsageFetch() {
  if (!LUL || LUL.fetching) return;
  LUL.fetching = true;
  const seq = LUL.seq;
  const body = document.getElementById('modal-body');
  const foot = document.getElementById('modal-foot');
  try {
    body.innerHTML = '<span class="spin"></span>拉取中…';
    const r = await api(`/ui/api/loomy/usage-logs?view=${encodeURIComponent(LOOMY_USAGE_VIEW)}` +
      `&page=${LUL.page}&page_size=${LUL.pageSize}`);
    if (!LUL || seq !== LUL.seq) return;   // 已切视图 / 已关闭
    if (r.error) {
      body.innerHTML = _loomyViewTabs() +
        `<pre class="view" style="color:#ffb3b3">拉取失败：${esc(r.error)}</pre>`;
      foot.innerHTML = '<button onclick="closeModal()">关闭</button>';
      return;
    }
    LUL.total = r.total || 0;
    _loomyRenderRows(r.rows || []);
    _loomyPager();
  } catch (e) {
    if (LUL && seq === LUL.seq) {
      body.innerHTML = _loomyViewTabs() +
        `<pre class="view" style="color:#ffb3b3">${esc(e.message || String(e))}</pre>`;
      foot.innerHTML = '<button onclick="closeModal()">关闭</button>';
    }
  } finally {
    if (LUL && seq === LUL.seq) LUL.fetching = false;
  }
}

function _loomyTime(ts) {
  if (!ts) return '—';
  const pad = n => String(n).padStart(2, '0');
  const d = new Date(ts * 1000);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function _loomyTimeTip(ts) {
  if (!ts) return '';
  const pad = n => String(n).padStart(2, '0');
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// 收支用正负号 + 颜色区分：只显示数字的话，兑换进来的 5000 分和扣掉的 1 分
// 长得一样，用户会以为「怎么还涨了」。
function _loomyCredit(r) {
  const n = Number(r.credits);
  if (!isFinite(n)) return '—';
  const isCredit = r.direction === 'credit';
  return `<span class="${isCredit ? 'ok' : ''}">${isCredit ? '+' : '-'}${Math.abs(n)}</span>`;
}

function _loomyRenderRows(rows) {
  const body = document.getElementById('modal-body');
  if (!rows.length) {
    body.innerHTML = _loomyViewTabs() +
      '<div class="empty" style="padding:24px 0">没有积分记录</div>';
    return;
  }
  let head, trs;
  if (LOOMY_USAGE_VIEW === 'purpose') {
    head = '<th>功能</th><th>端点</th><th class="num">笔数</th>' +
      '<th class="num">消耗</th><th class="mono muted">最近一次</th>';
    trs = rows.map(r => {
      const ts = r.last_ts || r.ts;
      const tip = _loomyTimeTip(ts);
      return `<tr>
        <td class="mono">${esc(r.event || '—')}</td>
        <td class="muted">${esc(r.model || '—')}</td>
        <td class="num mono">${r.ledger_count != null ? r.ledger_count : '—'}</td>
        <td class="num mono">${_loomyCredit(r)}</td>
        <td class="mono muted"${tip ? ` title="${esc(tip)}"` : ''}>${_loomyTime(ts)}</td>
      </tr>`;
    }).join('');
  } else {
    head = '<th class="mono muted">时间</th><th class="mono">模型</th><th>事件</th>' +
      '<th class="num">消耗</th><th class="num">余额</th>';
    trs = rows.map(r => {
      const tip = _loomyTimeTip(r.ts);
      const bal = (r.balance_before != null && r.balance_after != null)
        ? `<span class="muted">${r.balance_before} → ${r.balance_after}</span>` : '—';
      return `<tr>
        <td class="mono muted"${tip ? ` title="${esc(tip)}"` : ''}>${_loomyTime(r.ts)}</td>
        <td class="mono">${esc(r.model || '—')}</td>
        <td class="muted">${esc(r.event || '—')}${r.source ? ` <span class="tag" style="opacity:.8">${esc(r.source)}</span>` : ''}</td>
        <td class="num mono">${_loomyCredit(r)}</td>
        <td class="num mono">${bal}</td>
      </tr>`;
    }).join('');
  }
  body.innerHTML = _loomyViewTabs() +
    `<table class="trae-logs"><thead><tr>${head}</tr></thead><tbody>${trs}</tbody></table>`;
}

function _loomyPager() {
  const foot = document.getElementById('modal-foot');
  const close = '<button onclick="closeModal()">关闭</button>';
  const pages = Math.max(1, Math.ceil((LUL.total || 0) / LUL.pageSize));
  if (!LUL.total) { foot.innerHTML = close; return; }
  const p = LUL.page;
  const nav = (label, page, disabled) =>
    `<button class="ghost" ${disabled ? 'disabled' : ''} onclick="loomyUsageGo(${page})">${label}</button>`;
  const nums = [];
  const span = s => { if (s >= 1 && s <= pages && nums.indexOf(s) < 0) nums.push(s); };
  [1, 2, pages - 1, pages, p - 1, p, p + 1].forEach(span);
  nums.sort((a, b) => a - b);
  const pageHtml = nums.map((n, i) =>
    (i && nums[i] - nums[i - 1] > 1 ? '<span class="muted">…</span>' : '') +
    `<button class="${n === p ? 'primary' : 'ghost'}" onclick="loomyUsageGo(${n})">${n}</button>`
  ).join('');
  foot.innerHTML = nav('‹ 上一页', p - 1, p <= 1) +
    `<span class="muted" style="margin:0 6px">${pageHtml}</span>` +
    nav('下一页 ›', p + 1, p >= pages) +
    `<span class="muted" style="margin-left:auto">共 ${LUL.total} 条 · 第 ${p}/${pages} 页</span>` +
    close;
}
