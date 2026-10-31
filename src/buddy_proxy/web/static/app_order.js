let SCHEDULE_CTX = null;  // { provider, model }

function openScheduleModal(provider, model, windows) {
  SCHEDULE_CTX = { provider, model };
  document.getElementById('modal-title').textContent = `设置可用时段 · ${provider}/${model}`;
  const rows = (windows && windows.length ? windows : []).map(w => scheduleRow(w[0], w[1])).join('');
  document.getElementById('modal-body').innerHTML = `
    <div class="muted" style="font-size:13px;margin-bottom:10px">
      仅在下列时间窗内可调用，窗口外请求直接失败。<b>起 &gt; 止 视为跨天</b>（如 22:00→次日 08:00）。
      不添加任何时段 = 恢复全天可用。时区：Asia/Shanghai。
    </div>
    <div id="sched-rows">${rows}</div>
    <button class="ghost" style="margin-top:8px" onclick="addScheduleRow()">+ 添加时段</button>`;
  setModalFoot(`
    <button onclick="closeModal()">取消</button>
    <button class="primary" onclick="saveSchedule()">保存</button>`);
  document.getElementById('overlay').classList.add('show');
}

function scheduleRow(start, end) {
  return `<div class="sched-row" style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
    <input type="time" class="sched-start" value="${esc(start || '22:00')}" style="flex:1">
    <span class="muted">→</span>
    <input type="time" class="sched-end" value="${esc(end || '08:00')}" style="flex:1">
    <button class="ghost" title="删除此时段" onclick="this.closest('.sched-row').remove()">✕</button>
  </div>`;
}

function addScheduleRow() {
  document.getElementById('sched-rows').insertAdjacentHTML('beforeend', scheduleRow('', ''));
}

async function saveSchedule() {
  if (!SCHEDULE_CTX) return;
  const windows = [];
  document.querySelectorAll('#sched-rows .sched-row').forEach(row => {
    const s = row.querySelector('.sched-start').value;
    const e = row.querySelector('.sched-end').value;
    if (s && e && s !== e) windows.push([s, e]);
  });
  try {
    await api('/ui/api/model-schedule', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ...SCHEDULE_CTX, windows })});
    toast(windows.length
      ? `${SCHEDULE_CTX.provider}/${SCHEDULE_CTX.model} 限时可用：${windows.map(w => w.join('~')).join(', ')}`
      : `${SCHEDULE_CTX.provider}/${SCHEDULE_CTX.model} 已恢复全天可用`);
    closeModal();
    refreshAll();
  } catch (e) { toast('保存失败: ' + e.message, true); }
}

// ── 模型顺序页（独立页签：一眼看全所有已配顺序的模型，就地拖拽编辑）──
// 与模型页行内的「顺序」按钮改的是同一份数据（/ui/api/model-order）。这里存在的
// 意义是把入口从「藏在每行操作列的按钮里」提到一级页签，并按模型铺开。
// 展开态与草稿都存在 ORDER_PAGE 里，整页重绘（render() 由轮询/写操作触发）不丢编辑中
// 的内容——否则 30s 轮询一到，正在拖的顺序就被刷回旧值。

// 遍历 MODELS，收集「带 order 配置」的模型，形状 {p, m, targets[], marks[]}
// 通道目录里的 m["id"] 可能自带通道前缀（qoder 组里是 qoder/qwen3.8-max），
// 而 model_order 的键用的是**剥前缀的裸名**（转发侧按裸名路由，见 ui.py
// _bare_model_id）。不剥就会渲染出 qoder/qoder/qwen3.8-max 这种永远不生效的键。
function bareModelId(id, provider) {
  const p = String(provider || '');
  const s = String(id || '');
  return p && s.startsWith(p + '/') ? s.slice(p.length + 1) : s;
}

// 顺序页的卡片**就是 model_order 里的键**，一个键一张卡，多的一张都不画。
//
// 曾经的写法是「遍历通道目录，每个 通道+模型 组合画一张卡」——同一个模型名在
// 四个通道各画一份，页面上就成了四张一模一样的（用户原话：「配置什么就展示什么，
// 别搞这么复杂」）。现在渲染源只有一个：`/ui/api/model-order` 返回的键。
function orderRows() {
  const items = (ORDER && ORDER.items) || [];
  return items.filter(it => it && it.model);
}

// 顺序页要渲染的卡片 = 配置里的键 + **还没保存、正在编辑中的**草稿键。
// 后者只有「+ 新增模型」选完到点保存之间才会出现（那时配置里还没有这个键）。
function pageRows() {
  const out = [];
  const known = new Set();
  orderRows().forEach(it => {
    known.add(it.model);
    out.push({ m: it.model, order: it });
  });
  Object.keys(ORDER_DRAFT).forEach(name => {
    if (known.has(name)) return;
    out.push({ m: name, order: { targets: [], marks: [] } });
  });
  return out;
}

// 某个模型名的配置（来自上面的接口；草稿另有 ORDER_DRAFT）
function orderRowOf(name) {
  return orderRows().find(it => it.model === name) || null;
}

// 可选模型全集（用于「+ 新增模型」的下拉），按通道分组
function allModelOptions() {
  const out = [];
  ((MODELS && MODELS.groups) || []).forEach(g => {
    (g.models || []).forEach(m => out.push({ p: g.id, m: bareModelId(m.id, g.id) }));
  });
  return out;
}

// 页面状态：卡片键 = **裸模型名**（`glm-5.3`），与 model_order 的键同口径——
// 用户配的就是这个名字，运行时裸键对所有通道生效，所以卡片也只按名字分。
// 展开集合是纯 UI 状态，不随数据刷新重置。
let ORDER_OPEN = new Set();     // 已展开的模型名
let ORDER_DRAFT = {};           // 模型名 → [{provider, model}] 编辑中的草稿（未保存）
let ORDER_PAGE_MARKS = {};      // 模型名 → {targetKey: 剩余秒数}

// 服务端下发的 targets（"p/m" 字符串数组）→ 行对象数组
function orderTargetsToItems(targets) {
  return (targets || []).map(t => {
    const i = String(t).indexOf('/');
    return i > 0 ? { provider: String(t).slice(0, i), model: String(t).slice(i + 1) }
                 : { provider: 'codebuddy', model: String(t) };
  });
}

// 只数填了模型名的档位：末尾那个刚点「+ 添加目标」还没填名的空行不算一档
function orderCount(items) { return (items || []).filter(i => i.model).length; }

// 「未保存」= 草稿与服务端值**内容**不同，而不是「草稿存在」。
// 草稿在展开时就会建立（renderOrderRows 的首次填充），若只看存在性，
// 每张展开过的卡片都会永远挂着「未保存」——撤销修改后也摘不掉。
function orderIsDirty(key) {
  const draft = ORDER_DRAFT[key];
  if (!draft) return false;
  const row = orderRowOf(key);
  const server = row ? row.targets : [];
  const cur = draft.filter(i => i.model).map(i => `${i.provider}/${i.model}`);
  return cur.length !== server.length || cur.some((v, i) => v !== server[i]);
}

// 顺序页的行渲染依赖通道下拉（ORDER_PROVIDER_LIST）与模型候选（ORDER_OPTIONS），
// 这两样原本只在打开弹窗时加载。这里幂等补齐：拉不到就退化成纯手输，不阻断页面。
async function ensureOrderOptions() {
  if (ORDER_PROVIDER_LIST.length) return;
  await loadOrderOptions().catch(() => { ORDER_OPTIONS = {}; });
  // 通道列表以选项接口为准（MODELS 只在模型页加载过才有值）
  ORDER_PROVIDER_LIST = Object.keys(ORDER_OPTIONS || {}).length
    ? Object.keys(ORDER_OPTIONS)
    : ((MODELS && MODELS.groups) || []).map(g => g.id);
}

function renderOrderPage() {
  const box = document.getElementById('order-list');
  if (!box) return;
  // 本页的数据源是 ORDER（配置的键）。MODELS 只给「+ 新增模型」的下拉用，
  // 不能拿它当加载门槛：那会让配置早就到了、却因为模型目录慢或拉失败而一直
  // 停在「加载中…」。ORDER 非空即当前世代的数据（ensureData 会在代次失配时
  // 把它置回 null），所以这里不必再额外判「加载完没有」。
  if (!ORDER) { box.innerHTML = '<div class="empty">加载中…</div>'; return; }
  // 选项还没到位时先拉一次再重绘（只触发一轮：拉到后 ORDER_PROVIDER_LIST 非空）
  if (!ORDER_PROVIDER_LIST.length) {
    ensureOrderOptions().then(() => { if (currentTab() === 'order') renderOrderPage(); });
  }

  const rows = pageRows();
  const sum = document.getElementById('order-summary');
  if (sum) sum.textContent = rows.length ? `${rows.length} 个模型已配顺序` : '';

  if (!rows.length) {
    box.innerHTML = '<div class="empty">还没有配置任何模型顺序 —— 点右上角「+ 新增模型」开始。'
      + '<br><span class="muted" style="font-size:12px">未配置的模型按模型 id 自动匹配通道（历史行为）。</span></div>';
    return;
  }

  box.innerHTML = rows.map(r => orderPageCard(r)).join('');
  // 拖拽/编辑只绑一次：整块容器用事件委托，避免每张卡片重复绑定
  bindOrderPage(box);
}

function orderPageCard(r) {
  const key = r.m;   // 卡片键 = model_order 的键本身
  const open = ORDER_OPEN.has(key);
  // 草稿优先：用户正在编辑时不要被新拉到的数据覆盖
  const items = ORDER_DRAFT[key] || orderTargetsToItems(r.order.targets);
  const marks = ORDER_PAGE_MARKS[key] || {};
  const markCount = Object.keys(marks).length;
  // 标题里展示**通道的顺序**（`qoder → codebuddy`），这是这张卡片真正要回答的
  // 问题：请求这个模型时，依次找哪些上游。用草稿优先的 items 而不是服务端
  // targets，编辑中就能立刻看到改动后的样子。
  const chain = items.filter(i => i.model).map(i => i.provider).join(' → ');

  return `<div class="card order-card" data-key="${esc(key)}">
    <div class="order-head">
      <div class="order-head-main" onclick="orderToggle('${esc(key)}')">
        <span class="chev" style="transform:rotate(${open ? 0 : -90}deg)">▾</span>
        <span class="mono order-title">${esc(r.m)}</span>
        <button class="ghost copy-model" data-label="复制键名"
                title="复制 ${esc(key)}（本卡片的键；要带通道前缀的全名请用下面某一档的「复制」）"
                onclick="event.stopPropagation();copyModelName(this,${jsq(key)})">复制键名</button>
        <span class="tag order-count">${orderCount(items)} 档</span>
        ${markCount ? `<span class="tag bad" style="cursor:pointer" title="有目标处于冷却中，点击清除全部冷却，立即重试" onclick="event.stopPropagation();clearOrderMarks('${esc(key)}','${esc(key)}')">⏸ ${markCount}</span>` : ''}
        <span class="tag bad order-dirty" title="有未保存的修改" style="${orderIsDirty(key) ? '' : 'display:none'}">未保存</span>
        <span class="order-chain mono">${esc(chain)}</span>
      </div>
      <button class="primary order-test" onclick="runTest('','${esc(key)}')">测试</button>
    </div>
    ${open ? `<div class="order-body">
      <div class="order-rows" data-key="${esc(key)}"></div>
      <div class="order-actions">
        <button class="ghost" onclick="orderPageAdd('${esc(key)}')">+ 添加目标</button>
        <span class="spacer"></span>
        <button class="ghost" onclick="orderPageReset('${esc(key)}')">撤销修改</button>
        <button class="primary" onclick="orderPageSave('${esc(key)}')">保存</button>
        <button class="ghost danger" title="清空全部目标 = 恢复按模型 id 自动匹配的历史路由"
                onclick="orderPageClear('${esc(key)}')">清空</button>
      </div>
    </div>` : ''}
  </div>`;
}

// 展开/收起：只影响这一张卡片的 DOM，不整体重绘（避免打断其它卡片的编辑）
function orderToggle(key) {
  if (ORDER_OPEN.has(key)) ORDER_OPEN.delete(key); else ORDER_OPEN.add(key);
  renderOrderPage();
}

// 给每个 .order-rows 容器绑一次事件，再填行内容。
// 绑定（bindOrderRowEvents）与渲染（renderOrderRows）分开，是为了避免
// renderOrderRows → bindOrderPage → renderOrderRows 的无限递归。
function bindOrderPage(box) {
  box.querySelectorAll('.order-rows').forEach(el => renderOrderRows(el.dataset.key));
}

// 把 DOM 行回写成草稿（DOM 是编辑期真源，草稿是保存时读的真源）
function orderPageSync(key) {
  const el = document.querySelector(`.order-rows[data-key="${CSS.escape(key)}"]`);
  if (!el) return;
  ORDER_DRAFT[key] = Array.from(el.querySelectorAll('.order-row')).map(r => ({
    provider: orderProviderOf(r),
    model: (r.querySelector('.order-model').value || '').trim(),
  }));
}

function renderOrderRows(key) {
  const el = document.querySelector(`.order-rows[data-key="${CSS.escape(key)}"]`);
  if (!el) return;
  let items = ORDER_DRAFT[key];
  if (!items) {
    // 首次：从服务端拉到的 targets 建草稿
    const row = orderRowOf(key);
    if (!row) { el.innerHTML = ''; return; }
    items = ORDER_DRAFT[key] = orderTargetsToItems(row.targets);
    const marks = {};
    (row.marks || []).forEach(k => {
      const i = String(k.target).indexOf('/');
      if (i > 0) marks[orderKey(k.target.slice(0, i), k.target.slice(i + 1))] = k.cooldown_s;
    });
    ORDER_PAGE_MARKS[key] = marks;
  }
  el.innerHTML = items.length
    ? items.map((it, i) => orderRow(it, i, ORDER_PAGE_MARKS[key] || {}, key)).join('')
    : '<div class="muted" style="font-size:13px;padding:6px 0">暂无目标 —— 保存后恢复历史路由（按模型 id 自动匹配）。</div>';
  // 只补绑新产生的容器，绝不回头调 bindOrderPage（那会再触发本函数 → 递归）
  bindOrderRowEvents(el);
}

// 只给单个容器挂事件（不渲染），供 renderOrderRows / bindOrderPage 共用
function bindOrderRowEvents(el) {
  if (!el || el.dataset.bound) return;
  el.dataset.bound = '1';
  el.addEventListener('dragstart', ev => orderPageDrag(ev, 'start'));
  el.addEventListener('dragover',  ev => orderPageDrag(ev, 'over'));
  el.addEventListener('dragend',   ev => orderPageDrag(ev, 'end'));
  el.addEventListener('drop',      ev => ev.preventDefault());
  el.addEventListener('dragleave', ev => {
    if (!el.contains(ev.relatedTarget)) orderPageDrag(ev, 'end');
  });
}

function orderPageAdd(key) {
  orderPageSync(key);
  const list = ORDER_DRAFT[key] || [];
  const prev = list.length ? list[list.length - 1].provider : '';
  list.push({ provider: prev || (ORDER_PROVIDER_LIST[0] || ''), model: '' });
  ORDER_DRAFT[key] = list;
  renderOrderRows(key);
  const el = document.querySelector(`.order-rows[data-key="${CSS.escape(key)}"]`);
  const last = el && el.querySelector('.order-row:last-child .order-model');
  if (last) last.focus();
}

function orderPageReset(key) {
  delete ORDER_DRAFT[key];
  renderOrderRows(key);
  renderOrderPageHead(key);
}

// 拖拽：与弹窗版同一套语义，只是作用域换成单张卡片的 .order-rows
function orderPageDrag(ev, phase) {
  const box = ev.currentTarget;
  const row = ev.target.closest && ev.target.closest('.order-row');
  if (phase === 'start') {
    if (!row) return;
    ORDER_DRAG_FROM = Number(row.dataset.idx);
    if (ev.dataTransfer) {
      ev.dataTransfer.effectAllowed = 'move';
      try { ev.dataTransfer.setData('text/plain', String(ORDER_DRAG_FROM)); } catch (e) {}
    }
    row.style.opacity = '0.4';
    return;
  }
  if (phase === 'over') {
    if (ORDER_DRAG_FROM === null) return;
    ev.preventDefault();
    if (!row || Number(row.dataset.idx) === ORDER_DRAG_FROM) return;
    const dragged = box.querySelector(`.order-row[data-idx="${ORDER_DRAG_FROM}"]`);
    if (!dragged) return;
    const rect = row.getBoundingClientRect();
    const after = (ev.clientY - rect.top) > rect.height / 2;
    box.insertBefore(dragged, after ? row.nextElementSibling : row);
    Array.from(box.querySelectorAll('.order-row')).forEach((r, i) => { r.dataset.idx = i; });
    ORDER_DRAG_FROM = Number(dragged.dataset.idx);
    return;
  }
  // end：回写
  if (ORDER_DRAG_FROM !== null) {
    const key = box.dataset.key;
    ORDER_DRAFT[key] = Array.from(box.querySelectorAll('.order-row')).map(r => ({
      provider: orderProviderOf(r),
      model: (r.querySelector('.order-model').value || '').trim(),
    }));
  }
  ORDER_DRAG_FROM = null;
  const key = box.dataset.key;
  if (key) { renderOrderRows(key); renderOrderPageHead(key); }
}

// 只更新卡片头部（档位数/未保存标记/链路），不整页重绘
function renderOrderPageHead(key) {
  const card = document.querySelector(`.order-card[data-key="${CSS.escape(key)}"]`);
  if (!card) return;
  const head = card.querySelector('.order-head');
  const items = ORDER_DRAFT[key] || [];
  const cnt = head.querySelector('.order-count');
  if (cnt) cnt.textContent = `${orderCount(items)} 档`;
  const dirty = head.querySelector('.order-dirty');
  if (dirty) dirty.style.display = orderIsDirty(key) ? '' : 'none';
  // 标题里的通道顺序随编辑实时更新（拖拽/换 provider 后立刻反映）。
  const chain = items.filter(i => i.model).map(i => i.provider).join(' → ');
  const tail = head.querySelector('.order-chain');
  if (tail) tail.textContent = chain;
}

// 保存时读的「真源」。卡片**展开**时 DOM 是编辑真源（输入框里的字可能还没回写草稿）；
// **折叠**时没有输入框，能代表这轮意图的只有 `ORDER_DRAFT[key]`。
//
// 这里返回 null 表示「拿不到可信的界面状态」——两种情况：
//   1. 既没展开、也没有草稿（比如刚刷新完页面，orderPage() 的 not-open 分支把
//      `ORDER_DRAFT[key]` 重置成了 `[]`）。此时 `[]` 只是占位，不代表用户想清空。
//   2. 展开着但行数对不上（渲染竞态）。宁可就地提示重来，也不要把空数组当成
//      用户输入发出去，把服务端配置**静默抹平**——这正是「折叠态保存一下配置就没了」
//      的成因。返回 [] 才是用户真的把行都删光了，那是合法的「恢复历史路由」。
function orderPageIntendedItems(key) {
  const el = document.querySelector(`.order-rows[data-key="${CSS.escape(key)}"]`);
  if (!el) {
    // 折叠态：没有 DOM 可读。只有草稿存在（说明这轮编辑过）才认它；
    // 连草稿都没有就是「没展开过」，不可信。
    return ORDER_DRAFT[key] ? ORDER_DRAFT[key].filter(it => it.model) : null;
  }
  const rows = Array.from(el.querySelectorAll('.order-row'));
  const items = rows.map(r => ({
    provider: orderProviderOf(r),
    model: (r.querySelector('.order-model').value || '').trim(),
  })).filter(it => it.model);
  // 输入框里已经填了名的行数，和草稿里的行数对不上 → 渲染竞态，不可信
  const draft = (ORDER_DRAFT[key] || []).filter(it => it.model);
  if (rows.length && items.length !== draft.length && draft.length) return null;
  return items;
}

async function orderPageSave(key) {
  orderPageSync(key);
  const items = orderPageIntendedItems(key);
  if (items === null) {
    toast('界面还没同步好，请重新展开这张卡片再保存', true);
    return;
  }
  const asked = items.map(t => `${t.provider}/${t.model}`);
  // 按键就是裸模型名，provider 留空 —— 后端会用「裸名」形态落键，运行时对
  // 所有发布该模型名的通道都生效（这正是用户要的「写一遍就够」）。
  const model = key;
  try {
    const r = await api('/ui/api/model-order', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider: '', model, targets: items })});
    const saved = r.targets || [];
    // 按下标一对一比是够的：服务端回的是**已经归一化过**的列表（去重/别名/截断都在
    // 写盘时做完），所以 `saved` 与 `asked` 逐项不同就代表真的被规范化过。实测别名
    // 撞车（草稿里加 workbuddy/glm-5.3）时它会正确地报出来——那不是误报。
    const changed = asked.some((a, i) => saved[i] !== a);
    toast(items.length
      ? `${key} 候选顺序：${saved.join(' → ')}` + (changed ? '（已按通道目录规范化）' : '')
      : `${key} 已恢复历史路由`);
    delete ORDER_DRAFT[key];
    delete ORDER_PAGE_MARKS[key];
    await refreshAll();
    renderOrderPage();
  } catch (e) { toast('保存失败: ' + e.message, true); }
}

// 「清空」= 明确表达「恢复历史路由」的意图，绕开 orderPageSync（它靠 DOM 反写草稿，
// 折叠态没容器时会把那时的 `[]` 原样当成用户输入）。这是不可逆操作，先问一句。
async function orderPageClear(key) {
  if (!confirm(`清空 ${key} 的全部候选，恢复按模型 id 自动匹配的历史路由？`)) return;
  ORDER_DRAFT[key] = [];
  const el = document.querySelector(`.order-rows[data-key="${CSS.escape(key)}"]`);
  if (el) renderOrderRows(key);   // 展开态让界面立刻反映清空，再走保存
  await orderPageSave(key);
}

// 「+ 新增模型」：选**模型名**，选中即以空顺序展开（保存后生效）。
// 列表只显示裸模型名、不显示通道：用户配的是「这个模型名走什么顺序」，通道是
// 运行时解析归属用的实现细节。同名模型被多通道发布时也只列一条（一个名字一张卡、
// 一份配置）。
function orderAddModel() {
  const existing = new Set(orderRows().map(it => it.model.toLowerCase()));
  const names = [];
  const seenName = new Set();
  allModelOptions().forEach(o => {
    // 大小写不敏感去重：同一条目在目录里常同时有 `DeepSeek-V4-Flash` 和
    // `deepseek-v4-flash` 两个写法，全列出来只是噪声（6 组这样的重复）。
    const k = o.m.toLowerCase();
    if (seenName.has(k)) return;
    seenName.add(k);
    names.push(o.m);
  });
  names.sort((a, b) => a.localeCompare(b));
  document.getElementById('modal-title').textContent = '新增模型顺序';
  document.getElementById('modal-body').innerHTML = `
    <div class="muted" style="font-size:13px;margin-bottom:10px">
      选一个模型名即可 —— 之后就按这张卡片里的顺序依次尝试，默认用第一个。
    </div>
    <input id="order-new-search" placeholder="搜索模型名…" style="width:100%;margin-bottom:8px">
    <div id="order-new-list" class="order-new-list"></div>`;
  setModalFoot('<button onclick="closeModal()">取消</button>');
  const draw = (q) => {
    const kw = (q || '').trim().toLowerCase();
    const hit = names.filter(n => !kw || n.toLowerCase().includes(kw));
    document.getElementById('order-new-list').innerHTML = hit.length
      ? hit.map(n => {
          const has = existing.has(n.toLowerCase());
          return `<div class="order-new-item">
            <span class="mono on-m">${esc(n)}</span>
            ${has ? '<span class="tag">已配置</span>' : ''}
            <span class="spacer"></span>
            <button class="ghost" onclick="orderAddModelPick('','${esc(n)}')">${has ? '编辑' : '添加'}</button>
          </div>`;
        }).join('')
      : '<div class="muted">没有匹配的模型</div>';
  };
  draw('');
  const s = document.getElementById('order-new-search');
  s.addEventListener('input', () => draw(s.value));
  s.focus();
  document.getElementById('overlay').classList.add('show');
}

// 选一个模型名 → 直接以该名字建/展开卡片。**不再解析「归属通道」**：键就是裸名，
// 运行时对每个发布它的通道都生效，所以没有「这份顺序归哪个通道」这个问题。
// provider 参数仅为兼容调用方保留（一律为空）。
function orderAddModelPick(provider, model) {
  const key = model;
  ORDER_OPEN.add(key);
  if (!ORDER_DRAFT[key]) {
    const row = orderRowOf(key);
    // 已配置的沿用其 targets；未配置的从空开始
    ORDER_DRAFT[key] = row ? orderTargetsToItems(row.targets) : [];
  }
  closeModal();
  switchTab('order');
  renderOrderPage();
  setTimeout(() => {
    const card = document.querySelector(`.order-card[data-key="${CSS.escape(key)}"]`);
    if (card) card.scrollIntoView({block: 'center', behavior: 'smooth'});
  }, 60);
}
