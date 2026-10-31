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
      <button class="ghost copy-model" data-label="复制" title="复制这一档的「通道/模型」全名（可直接填客户端 model 字段）"
        onclick="copyOrderTarget(this)">复制</button>
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

// 「顺序」页某一档的复制：读**当前界面**的通道按钮文字与模型输入框，拼成
// `通道/模型`。不能拿草稿或服务端 targets 拼——用户刚改了通道/模型还没保存时，
// 复制到的必须是屏幕上看到的那个名字，否则贴进客户端一定 404。
function copyOrderTarget(btn) {
  const row = btn && btn.closest && btn.closest('.order-row');
  if (!row) return;
  const provider = orderProviderOf(row);
  const model = ((row.querySelector('.order-model') || {}).value || '').trim();
  if (!provider || !model) { toast('这一档还没选好通道和模型', true); return; }
  copyModelName(btn, provider + '/' + model);
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
