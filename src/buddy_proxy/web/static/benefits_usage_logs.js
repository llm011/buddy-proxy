// ---- 各通道「官方消耗明细」弹窗（trae / raccoon / codebuddy 共用）----
// 数据来自各通道的 /ui/api/<provider>/usage-logs（对应官网用量页背后的接口），
// 只做展示、不回写我们的请求日志（粒度对不上会重复记账）。
//
// 后端统一形状：
//   {rows:[{ts(unix秒|null), time_raw, credits, currency, model?, mode?, client?, preview?, event?}],
//    page?, page_size, total?, has_more?, next_cursor?, cursor?, currency, error?}
// 分页两种形态：
//   offset —— 有 total，可跳页（trae / codebuddy）
//   cursor —— 上游只给 has_more + next_cursor，只能前后翻（raccoon）
//
// 依赖全局：api / esc / setModalFoot / closeModal
'use strict';

let UL = null;  // 当前弹窗状态；null = 没打开

function resetUsageLogs() {
  // closeModal 是所有弹窗共用的：关闭后还有在途明细请求时，必须让响应失效，
  // 否则它会覆盖随后打开的 rename/test 弹窗（seq 只防 A 切 B，防不了关闭）。
  if (UL) UL.seq += 1;
  UL = null;
}

const USAGE_LOGS_CFG = {
  trae: {
    title: 'Trae 官方消耗明细',
    paging: 'offset',
    path: '/ui/api/trae/usage-logs',
    cols: [
      {label: '时间', cls: 'mono muted', tip: 'time', render: r => _ulTime(r.ts)},
      {label: '模型', cls: 'mono', render: r => esc(r.model || '') + (r.mode ? ` <span class="tag" style="opacity:.8">${esc(r.mode)}</span>` : '')},
      {label: '消耗', cls: 'num mono ok', render: r => _ulCredit(r)},
      {label: '对话预览', cls: 'trae-log-preview-wrap', render: r => _ulPreview(r.preview)},
    ],
  },
  codebuddy: {
    title: 'CodeBuddy 官方消耗明细',
    paging: 'offset',
    path: '/ui/api/codebuddy/usage-logs',
    cols: [
      {label: '时间', cls: 'mono muted', tip: 'time', render: r => _ulTime(r.ts)},
      {label: '模型', cls: 'mono', render: r => esc(r.model || '')},
      {label: '客户端', cls: 'muted', render: r => esc(r.client || '—')},
      {label: '消耗', cls: 'num mono ok', render: r => _ulCredit(r)},
      {label: '对话预览', cls: 'trae-log-preview-wrap', render: r => _ulPreview(r.preview)},
    ],
  },
  raccoon: {
    title: '小浣熊官方消耗明细',
    paging: 'cursor',
    path: '/ui/api/raccoon/usage-logs',
    // 上游流水不带模型名（见 provider「积分结算」注释），所以没有模型列；
    // 事件名（如「桌面端 LLM 对话消耗」）就是这一行最有信息量的东西。
    cols: [
      {label: '时间', cls: 'mono muted', tip: 'time', render: r => _ulTime(r.ts)},
      {label: '事件', cls: 'mono', render: r => esc(r.event || '—')},
      {label: '客户端', cls: 'muted', render: r => esc(r.client || '—')},
      {label: '消耗', cls: 'num mono ok', render: r => _ulCredit(r)},
    ],
  },
};

function _ulTime(ts) {
  if (!ts) return '—';
  const pad = n => String(n).padStart(2, '0');
  const d = new Date(ts * 1000);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function _ulTimeTip(r) {
  // hover 看完整到秒：优先原始串（带上游时区偏移，最可信），没有就按 ts 格式化
  if (r.time_raw) return r.time_raw;
  if (!r.ts) return '';
  const pad = n => String(n).padStart(2, '0');
  const d = new Date(r.ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function _ulCredit(r) {
  if (r.credits == null) return '—';
  const n = Number(r.credits);
  if (!isFinite(n)) return '—';
  if (r.currency === 'dollar') return `$${n.toFixed(4)}`;
  // 积分：整数不带小数（raccoon 扣 1 分就是 "1"），小数保留 3 位（trae 1.5256）
  return Number.isInteger(n) ? String(n) : n.toFixed(3);
}

function _ulPreview(pv) {
  if (!pv) return '<span class="muted">—</span>';
  const s = String(pv).slice(0, 200);
  return `<span class="muted trae-log-preview" title="${esc(s)}">${esc(s)}</span>`;
}

function _ulRender(rows) {
  const cfg = USAGE_LOGS_CFG[UL.provider];
  const body = document.getElementById('modal-body');
  if (!rows.length) {
    body.innerHTML = '<div class="empty" style="padding:24px 0">最近 30 天没有消耗记录</div>';
    return;
  }
  const head = cfg.cols.map(c => `<th${c.cls && c.cls.indexOf('num') >= 0 ? ' class="num"' : ''}>${c.label}</th>`).join('');
  const trs = rows.map(row => {
    const tip = _ulTimeTip(row);
    return `<tr>${cfg.cols.map(c => {
      const cls = c.cls || '';
      const tipAttr = (c.tip === 'time' && tip) ? ` title="${esc(tip)}"` : '';
      return `<td class="${cls}"${tipAttr}>${c.render(row)}</td>`;
    }).join('')}</tr>`;
  }).join('');
  body.innerHTML = `<table class="trae-logs"><thead><tr>${head}</tr></thead><tbody>${trs}</tbody></table>`;
}

function _ulPager() {
  const foot = document.getElementById('modal-foot');
  const cfg = USAGE_LOGS_CFG[UL.provider];
  const close = '<button onclick="closeModal()">关闭</button>';
  if (cfg.paging === 'cursor') {
    // cursor 分页：page 只是我们自己数的，没有总数
    const nav = (label, fn, disabled) =>
      `<button class="ghost" ${disabled ? 'disabled' : ''} onclick="${fn}">${label}</button>`;
    foot.innerHTML = nav('‹ 上一页', 'usageLogsGo(-1)', UL.page <= 1) +
      `<span class="muted" style="margin:0 6px">第 ${UL.page} 页</span>` +
      nav('下一页 ›', 'usageLogsGo(1)', !UL.hasMore) + close;
    return;
  }
  const pages = Math.max(1, Math.ceil((UL.total || 0) / UL.pageSize));
  if (!UL.total) { foot.innerHTML = close; return; }
  const p = UL.page;
  const nav = (label, page, disabled) =>
    `<button class="ghost" ${disabled ? 'disabled' : ''} onclick="usageLogsGo(${page})">${label}</button>`;
  const nums = [];
  const span = (s) => { if (s >= 1 && s <= pages && nums.indexOf(s) < 0) nums.push(s); };
  [1, 2, pages - 1, pages, p - 1, p, p + 1].forEach(span);
  nums.sort((a, b) => a - b);
  const pageHtml = nums.map((n, i) =>
    (i && nums[i] - nums[i - 1] > 1 ? '<span class="muted">…</span>' : '') +
    `<button class="${n === p ? 'primary' : 'ghost'}" onclick="usageLogsGo(${n})">${n}</button>`
  ).join('');
  foot.innerHTML = nav('‹ 上一页', p - 1, p <= 1) +
    `<span class="muted" style="margin:0 6px">${pageHtml}</span>` +
    nav('下一页 ›', p + 1, p >= pages) +
    `<span class="muted" style="margin-left:auto">共 ${UL.total} 条 · 第 ${p}/${pages} 页</span>` + close;
}

async function openUsageLogs(provider, id, name) {
  const cfg = USAGE_LOGS_CFG[provider];
  if (!cfg) return;
  UL = {provider, id, name: name || id, page: 1, pageSize: 30, total: 0, hasMore: false,
        nextCursor: null, cursorStack: [], currency: 'credits', fetching: false, seq: (UL ? UL.seq : 0) + 1};
  document.getElementById('modal-title').textContent = `${cfg.title} · ${UL.name}`;
  document.getElementById('modal-body').innerHTML = '<span class="spin"></span>拉取最近 30 天的官方消耗记录…';
  setModalFoot('');
  document.getElementById('overlay').classList.add('show');
  await usageLogsFetch();
}

// offset：page 是目标页码；cursor：delta = ±1
async function usageLogsGo(pageOrDelta) {
  if (!UL || UL.fetching) return;
  const cfg = USAGE_LOGS_CFG[UL.provider];
  if (cfg.paging === 'cursor') {
    const d = pageOrDelta;
    if (d > 0) {
      if (!UL.hasMore || !UL.nextCursor) return;
      UL.cursorStack.push(UL.nextCursor);
      UL.page += 1;
    } else {
      if (UL.page <= 1) return;
      UL.cursorStack.pop();
      UL.page -= 1;
    }
  } else {
    if (pageOrDelta === UL.page) return;
    UL.page = pageOrDelta;
  }
  await usageLogsFetch();
}

async function usageLogsFetch() {
  if (!UL || UL.fetching) return;
  UL.fetching = true;
  const seq = UL.seq;
  const cfg = USAGE_LOGS_CFG[UL.provider];
  const body = document.getElementById('modal-body');
  try {
    body.innerHTML = '<span class="spin"></span>拉取中…';
    let qs = `id=${encodeURIComponent(UL.id)}&page_size=${UL.pageSize}`;
    if (cfg.paging === 'cursor') {
      const cur = UL.cursorStack[UL.cursorStack.length - 1];
      if (cur) qs += `&cursor=${encodeURIComponent(cur)}`;
    } else {
      qs += `&page=${UL.page}`;
    }
    const r = await api(cfg.path + '?' + qs);
    if (!UL || seq !== UL.seq) return;  // 用户已切到别的账号/通道
    if (r.error) {
      body.innerHTML = `<pre class="view" style="color:#ffb3b3">拉取失败：${esc(r.error)}</pre>`;
      document.getElementById('modal-foot').innerHTML = '<button onclick="closeModal()">关闭</button>';
      return;
    }
    UL.total = r.total || 0;
    UL.hasMore = !!r.has_more;
    UL.nextCursor = r.next_cursor || null;
    UL.currency = r.currency || 'credits';
    // 把 currency 挂到每行（渲染函数按行读）
    const rows = (r.rows || []).map(x => Object.assign({currency: UL.currency}, x));
    _ulRender(rows);
    _ulPager();
  } catch (e) {
    if (UL && seq === UL.seq) {
      body.innerHTML = `<pre class="view" style="color:#ffb3b3">${esc(e.message || String(e))}</pre>`;
      document.getElementById('modal-foot').innerHTML = '<button onclick="closeModal()">关闭</button>';
    }
  } finally {
    if (UL && seq === UL.seq) UL.fetching = false;
  }
}
