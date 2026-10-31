// ---- 多账号额度面板公共 helper（新通道接入时直接复用）----
// antigravity / kimi / qoder 的「多账号 · 自动切换」面板是同一模板：按「<通道名> #N」
// 分组额度条目、每组一块（标题=账号名、副标题=状态、↻/▲▼/✕ 按钮组）、账号快照
// 异步回填就地更新。那三处已稳定、各有密集 node 桩测试覆盖，故不动它们；本文件
// 把可复用件抽成参数化 helper，**新接入的多账号通道（如 dumate）用它一处组装**，
// 未来再接入照 dumatePanel 的样子写一个 CONFIG 即可，不必重抄这套逻辑。
//
// 复用件：confirmAccountDelete（删除确认弹窗）/ acctSubHtml（账号状态副标题）/
//         acctMoveButtons（▲▼ 顺位）/ acctDeleteButton + acctRowButtons（账号按钮组）/
//         groupAccountsByPrefix（按「<Prefix> #N」切分组）/ acctLoad（账号快照回填，
//         含首份快照整卡重渲 + 就地回填 + syncQuotaFold 校准）
//
// 依赖 app.js 全局：api / toast / esc / refreshAll / syncQuotaFold / closeModal / setModalFoot
// 以及 benefits.js 全局：BENEFITS / quotaItemsHtml / quotaItemHtml / refreshProviderQuota。
'use strict';

// 删除确认弹窗（各多账号通道共用）：复用 index.html 的 overlay/modal 骨架，不用系统
// confirm。Promise 化：「删除该账号」resolve(true)；取消/遮罩/Esc（汇入 closeModal）
// resolve(false)。closeModal 是 app.js 全局函数，临时替换拦下全部关闭路径、关完还原。
let ACCT_CONFIRM_OPEN = false;
function confirmAccountDelete(opts) {
  if (ACCT_CONFIRM_OPEN) return Promise.resolve(false);
  ACCT_CONFIRM_OPEN = true;
  return new Promise(resolve => {
    let done = false;
    const finish = v => {
      if (done) return;
      done = true;
      // 确认（v=true）时**不在这里清锁**：resolve 是微任务，调用方的在飞锁置位要等
      // 下一拍——这中间窗口里再点 ✕ 会叠开第二个确认框永远等不到表态（前端测试
      // test_delete_account_inflight_clicks_ignored 抓过）。锁交给确认方（delete 的
      // finally）在请求真正收尾后清；取消（v=false）没有后续请求，立即清。
      if (!v) ACCT_CONFIRM_OPEN = false;
      resolve(v);
    };
    document.getElementById('modal-title').textContent = opts.title;
    document.getElementById('modal-body').innerHTML =
      `<p style="margin:0 0 6px">确定删除 <b>${esc(opts.name)}</b>？</p>` +
      `<p class="muted" style="margin:0;font-size:12px">${opts.extra}</p>`;
    setModalFoot(
      `<button onclick="closeModal()">取消</button>` +
      `<button class="danger" onclick="globalThis.__acctDelYes()">${esc(opts.yes || '删除该账号')}</button>`);
    const prevClose = globalThis.closeModal;
    globalThis.closeModal = () => {
      globalThis.closeModal = prevClose;   // 先还原再走原关闭，别的弹窗不受污染
      finish(false);
      if (prevClose) prevClose();
    };
    globalThis.__acctDelYes = () => { finish(true); globalThis.closeModal(); };
    document.getElementById('overlay').classList.add('show');
  });
}

// 冷却时长格式化：≥2h 按小时（6h 拉黑档读着不像「360min」），否则按分钟。
function acctCoolLeft(min) {
  return min >= 120 ? (min / 60).toFixed(1) + 'h' : min + 'min';
}

/** 账号状态副标题（进度条上面那行 muted 小字）。
 *  通用 bits：region / 冷却（cooling[].kind+minutes_left）。
 *  kind='blacklist' 标「疑似拉黑」引导删除；kind='quota'→「额度冷却」，其余→「账号冷却」。
 *  通道特有 bit（如 dumate 的累计签到）经 subExtra(a, bits) 注入。
 *  不再显示「token 剩 Xh」：access token 几小时就自动刷新（qoder 甚至曾把毫秒当
 *  秒算出 4.97 亿小时），对「还能用多久」毫无参考价值——用户 2026-10-05 明确不要。 */
function acctSubHtml(a, subExtra, dataAttr) {
  if (!a) return '';
  const bits = [];
  if (subExtra) subExtra(a, bits);
  if (a.region) bits.push(a.region === 'cn' ? 'CN 区' : 'Global 区');
  for (const c of a.cooling || []) {
    const left = acctCoolLeft(c.minutes_left);
    bits.push(c.kind === 'blacklist' ? `疑似拉黑 剩${left}`
      : `${c.kind === 'quota' ? '额度' : '账号'}冷却 ${left}`);
  }
  return bits.length
    ? `<div class="muted" style="font-size:11px;margin:1px 0 6px" ${dataAttr}>${esc(bits.join(' · '))}</div>`
    : '';
}

/** ✎ 改名按钮（要账号 id；快照没到时不渲染，同 ✕）。
 *  预填的当前显示名不内联进 onclick——alias 是用户自由文本（引号/反斜杠都可能有），
 *  字符串字面量拼参数会碎；prompt 里按 id 从各通道快照现查。 */
function acctRenameButton(pid, acct) {
  // 共用账号池的通道在按钮层就映射到实际 rename 端点（qoderintl 与 qoder
  // 同池同端点）——按钮 → 弹窗 → 提交整条链路只认端点 pid，不必每处特判。
  const ep = pid === 'qoderintl' ? 'qoder' : (pid === 'codebuddyintl' ? 'codebuddy' : pid);
  return acct
    ? `<button class="ghost" title="重命名该账号（只改显示名，凭据不动）" ` +
      `onclick="acctRenamePrompt('${ep}','${acct.id}')">✎</button>` : '';
}

/** 改名弹窗（四通道共用，复用 index.html 的 overlay/modal 骨架）。
 *  current 缺省时按 id 从快照数组现查；清空提交 = 恢复默认名（后端空串语义）。 */
function acctRenamePrompt(pid, id, current) {
  if (current == null) {
    // 预填从快照数组现查（typeof 守卫同上——数组可能声明在未加载的文件里）
    const snap = [].concat(
      (typeof AG_ACCTS !== 'undefined' ? AG_ACCTS : []) || [],
      (typeof QODER_ACCTS !== 'undefined' ? QODER_ACCTS : []) || [],
      (typeof KIMI_ACCTS !== 'undefined' ? KIMI_ACCTS : []) || [],
      (typeof CODEX_ACCTS !== 'undefined' ? CODEX_ACCTS : []) || [],
      (typeof TRAE_ACCTS !== 'undefined' ? TRAE_ACCTS : []) || [],
      (typeof CODEBUDDY_ACCTS !== 'undefined' ? CODEBUDDY_ACCTS : []) || [],
      (typeof CODEBUDDY_INTL_ACCTS !== 'undefined' ? CODEBUDDY_INTL_ACCTS : []) || [],
      (typeof DUMATE_STATE !== 'undefined' && DUMATE_STATE ? DUMATE_STATE.accts : []) || [],
      // 签到明细行（各通道 checkin.accounts[].name，后端按 alias 链下发）兜底——
      // 快照数组未到位（首屏）时从这里也能拿到显示名
      (typeof BENEFITS !== 'undefined' && BENEFITS.providers
        ? BENEFITS.providers.flatMap(p => (p.checkin && p.checkin.accounts) || []) : []));
    const a = snap.find(x => x && x.id === id);
    current = a ? (a.alias || a.name || a.email || a.nickname || a.id) : id;
  }
  document.getElementById('modal-title').textContent = '重命名账号';
  document.getElementById('modal-body').innerHTML =
    `<p style="margin:0 0 8px">给 <span class="mono">${esc(id)}</span> 起个显示名 ` +
    `（只改管理页显示，凭据与顺位不动）：</p>` +
    `<input id="acct-rename-input" type="text" maxlength="64" value="${esc(current || '')}" ` +
    `style="width:100%" placeholder="留空则恢复默认名">` +
    `<p class="muted" style="margin:8px 0 0;font-size:12px">清空并保存 = 恢复默认名（邮箱 / 官方昵称）。</p>`;
  setModalFoot('<button onclick="closeModal()">取消</button>' +
    `<button onclick="acctRenameSubmit('${pid}','${id}')">保存</button>`);
  document.getElementById('overlay').classList.add('show');
  const input = document.getElementById('acct-rename-input');
  if (input) { input.focus(); input.select(); }
}

async function acctRenameSubmit(pid, id) {
  const input = document.getElementById('acct-rename-input');
  const alias = input ? input.value.trim() : '';
  try {
    const r = await api(`/ui/api/${pid}/accounts/rename`, {
      method: 'POST', body: JSON.stringify({id, alias}),
    });
    // 就地更新快照（各面板的全局数组）+ 立即重渲，不等 30s 轮询。
    // 快照数组都在别的文件里用 let 声明，这里 typeof 守卫着取——本文件是
    // 'use strict'，直接引用未声明标识符会 ReferenceError（被 catch 吞成
    // 「重命名失败」，改名看似发了请求实际没生效）。
    // trae 不在 AG/QODER/KIMI/DUMATE 之列（它只有签到明细行、无快照数组），
    // 名字由后端 name 链（alias 优先）下发，renderBenefits 重渲即生效。
    const newAlias = (r && r.accounts && (r.accounts.find(x => x.id === id) || {}).alias) ?? alias;
    const snaps = [
      (typeof AG_ACCTS !== 'undefined' ? AG_ACCTS : null),
      (typeof QODER_ACCTS !== 'undefined' ? QODER_ACCTS : null),
      (typeof INTL_ACCTS !== 'undefined' ? INTL_ACCTS : null),
      (typeof KIMI_ACCTS !== 'undefined' ? KIMI_ACCTS : null),
      (typeof CODEX_ACCTS !== 'undefined' ? CODEX_ACCTS : null),
      (typeof TRAE_ACCTS !== 'undefined' ? TRAE_ACCTS : null),
      (typeof CODEBUDDY_ACCTS !== 'undefined' ? CODEBUDDY_ACCTS : null),
      (typeof CODEBUDDY_INTL_ACCTS !== 'undefined' ? CODEBUDDY_INTL_ACCTS : null),
      (typeof DUMATE_STATE !== 'undefined' && DUMATE_STATE ? DUMATE_STATE.accts : null),
    ];
    for (const arr of snaps) {
      const a = (arr || []).find(x => x.id === id);
      if (a) a.alias = newAlias;
    }
    // Trae/Qoder 签到明细行的名字来自后端，不在明细里复制 alias——照 rename
    // 响应就地改 name，免得要等下轮 benefits 轮询才看到新名字。
    if ((pid === 'trae' || pid === 'qoder') &&
        typeof BENEFITS !== 'undefined' && BENEFITS.providers) {
      const st = (r.accounts || []).find(x => x.id === id) || {};
      const displayName = pid === 'trae'
        ? st.nickname
        : (st.alias || st.name || st.email || st.id);
      for (const p of BENEFITS.providers) {
        const row = (p.checkin && p.checkin.accounts || []).find(x => x.id === id);
        if (row && displayName != null) row.name = displayName;
      }
    }
    if (typeof renderAntigravityPanel === 'function') renderAntigravityPanel();
    if (typeof renderKimiPanel === 'function') renderKimiPanel();
    if (typeof renderCodexPanel === 'function') renderCodexPanel();
    if (typeof renderQoderPanel === 'function') renderQoderPanel();
    if (typeof renderQoderIntlPanel === 'function') renderQoderIntlPanel();
    if (typeof renderTraePanel === 'function') renderTraePanel();
    if (typeof renderCodebuddyPanel === 'function') renderCodebuddyPanel();
    if (typeof renderCodebuddyIntlPanel === 'function') renderCodebuddyIntlPanel();
    if (typeof renderBenefits === 'function') renderBenefits();
    closeModal();
    toast(alias ? `已重命名为「${alias}」` : '已恢复默认名');
  } catch (e) {
    toast(`重命名失败：${e && e.message ? e.message : e}`, true);
  }
}

/** ▲▼ 顺位按钮（idx 渲染时 1-based，首尾禁用对应那个）。带 id 内联：重排响应回来后、
 *  面板重绘前 DOM 还挂旧按钮（idx 是旧顺序）——点按时按快照里该 id 的实际位次挪才
 *  不动错人。id 字符集由后端 _ID_RE 约束（字母数字 ._@-），内联进 onclick 安全。 */
function acctMoveButtons(pid, idx, n, id) {
  const arg = id ? `,'${id}'` : '';
  return `<button class="ghost" title="上移（更优先使用）" ${idx <= 1 ? 'disabled' : ''} ` +
    `onclick="acctMove('${pid}',${idx},-1${arg})">▲</button>` +
    `<button class="ghost" title="下移" ${idx >= n ? 'disabled' : ''} ` +
    `onclick="acctMove('${pid}',${idx},1${arg})">▼</button>`;
}

/** ✕ 删除按钮（要账号 id；快照没到（首屏）时先不渲染，等下轮回填）。 */
function acctDeleteButton(pid, acct, hint) {
  return acct
    ? `<button class="ghost danger" title="删除该账号（${esc(hint)}）" ` +
      `onclick="acctDelete('${pid}','${acct.id}')">✕</button>` : '';
}

/** 渠道标题统一使用的整体额度刷新按钮。 */
function providerRefreshButton(pid) {
  return `<button class="ghost" title="刷新本通道全部账号额度（绕过缓存重查）" ` +
    `onclick="refreshProviderQuota('${pid}', this)">↻</button>`;
}

/** ▲▼ + ✕ 合成右上角账号按钮组。渠道级 ↻ 统一放面板标题；这里仍放块尾、
 *  绝对定位——load 的就地回填靠「名字元素的下一个兄弟是副标题」定位，
 *  中间插任何元素会乱。 */
function acctRowButtons(pid, moveBtns, delBtn) {
  return `<span class="ag-move">${moveBtns}${delBtn}</span>`;
}

/** 按「<Prefix> #N」切分组。额度 label 形如「<Prefix> #N · <条目>」，单账号无前缀。
 *  query_failed 说明条 / 无进度条语义的静态条（percent==null && used==null）不进分组，
 *  横贯全宽展示。返回 {groups, notices}。 */
function groupAccountsByPrefix(items, prefix, defaultName) {
  const groups = new Map();
  const notices = [];
  for (const it of items) {
    if (it.query_failed || (it.percent == null && it.used == null)) { notices.push(it); continue; }
    const idx = it.label.indexOf(' · ');
    const grp = idx >= 0 ? it.label.slice(0, idx) : defaultName;
    const sub = idx >= 0 ? it.label.slice(idx + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  return { groups, notices };
}

/** 界标 n = 全部「<Prefix> #N」序号最大值（自包含，不依赖快照到达时序，首屏首渲就有按钮）。 */
function accountGroupBound(groups, prefix) {
  const re = new RegExp('^' + prefix + ' #(\\d+)$');
  return Math.max(...[...groups.keys()]
    .map(g => g.match(re)).filter(Boolean).map(mm => Number(mm[1])), 1);
}

// ---- 账号加载去重 + 静默开关（强刷签到只该刷签到，别刮一串 accounts）----
// 同一通道的账号加载在飞时复用同一发请求：30s 轮询 + 整页重绘叠加，原来每个通道
// 会并排发好几发一模一样的 GET。键是通道 pid，单位是「整条加载器」（不只 GET），
// 这样借到的 promise 落定时快照数组、面板回填也都已经做完。
const ACCT_INFLIGHT = new Map();  // pid -> Promise（整条加载器）
// 静默重绘期间为 true（强刷签到用快照重绘）：各加载器不许整卡重渲，只就地回填，
// 否则会把刚画好的签到区/别处 DOM 冲掉。
let ACCT_NO_RERENDER = false;
function acctNoRerender(pid) { return ACCT_NO_RERENDER; }

function acctFetch(pid, fn) {
  const inflight = ACCT_INFLIGHT.get(pid);
  if (inflight) return inflight;
  const p = Promise.resolve().then(fn)
    .finally(() => { if (ACCT_INFLIGHT.get(pid) === p) ACCT_INFLIGHT.delete(pid); });
  ACCT_INFLIGHT.set(pid, p);
  return p;
}

/** 账号快照回填（通用 load）。行为：enabled=false 清快照；数据没变不动 DOM（避免 30s
 *  轮询闪回内部代号）；首份快照整卡重渲（直出账号名 + 带 id 按钮）并 syncQuotaFold；
 *  之后就地回填（换组名 + 重写副标题）并 syncQuotaFold。acctFor(idx) 按快照取账号。
 *  走 acctFetch 去重；返回的 promise 在本拍加载**开始**时就拿到（数据已同步的通道
 *  会早早落定，接着调用的加载器这时自己再发起）——别把 acctFetch 包在 async
 *  里再 await，那样返回的 promise 要等下一个微任务才存在，复用不了。
 *  静默重绘期间首份快照也只就地回填。 */
function acctLoad(cfg, state) {
  return acctFetch(cfg.pid, () => (async function() {
    try {
      const r = await api(`/ui/api/${cfg.pid}/accounts`);
      if (!r.enabled) { state.accts = []; return; }
      const list = r.accounts || [];
      if (JSON.stringify(list) === JSON.stringify(state.accts)) return;  // 没变不动 DOM
      const snapshotMissing = !state.accts || !state.accts.length;
      state.accts = list;
      if (!list.length) return;
      if (snapshotMissing) {
        // 静默重绘期间（强刷签到只换签到区）别整卡重渲——会冲掉别处 DOM，
        // 改就地回填
        if (acctNoRerender(cfg.pid)) cfg.fill();
        else cfg.render();
        syncQuotaFold();
        return;
      }
      cfg.fill();
      syncQuotaFold();
    } catch (e) {
      // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
    }
  })());
}

function acctFor(state, idx) {
  const accts = state.accts;
  if (!accts) return null;
  if (idx == null) return accts.length === 1 ? accts[0] : null;
  return accts.find(a => a.index === idx) || null;
}

// ---- 全局分发：acctMove / acctDelete ----
// 模板里统一写 acctMove/acctDelete（不同通道同一入口），这里按 pid 路由到对应
// 通道的处理。ag/kimi/qoder 用各自的 agMoveAccount/agDeleteAccount 等（不动它们）；
// trae 用 traeMoveAccount/traeDeleteAccount；dumate 用下面的 dumateMove/dumateDelete。
// 新增多账号通道时在这里登记即可。
function acctMove(pid, idx, delta, id) {
  if (pid === 'dumate') return dumateMove(idx, delta, id);
  const fn = globalThis[({antigravity: 'ag', kimi: 'kimi', codex: 'codex', qoder: 'qoder', trae: 'trae', codebuddy: 'codebuddy'})[pid] + 'MoveAccount'];
  if (fn) fn(idx, delta, id);
}
function acctDelete(pid, id) {
  if (pid === 'dumate') return dumateDelete(id);
  const fn = globalThis[({antigravity: 'ag', kimi: 'kimi', codex: 'codex', qoder: 'qoder', trae: 'trae', codebuddy: 'codebuddy'})[pid] + 'DeleteAccount'];
  if (fn) fn(id);
}

// dumate 单账号：重排无意义（no-op）。
async function dumateMove() {}

// dumate 单账号：删除即「清除本地累计签到缓存」——确认后调后端 no-op 接口刷新
// 卡片（账号本身是 App 登录态，不能也不该真删，所以后端 /accounts/delete 是 no-op）。
let DUMATE_DEL_MOVING = false;
async function dumateDelete(id) {
  if (DUMATE_DEL_MOVING) return;
  const a = acctFor(DUMATE_STATE, 1);
  const name = a ? (a.displayName || a.id) : id;
  if (!(await confirmAccountDelete({
      title: '清除 DuMate 本地缓存',
      name,
      extra: 'DuMate 复用本机 App 登录态、无多账号 failover，删除仅清除本地累计签到缓存的展示，不影响登录态与转发。',
      yes: '清除缓存'}))) return;
  DUMATE_DEL_MOVING = true;
  try {
    const r = await api('/ui/api/dumate/accounts/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})});
    DUMATE_STATE.accts = r.accounts || [];
    toast('已清除 DuMate 本地缓存');
    refreshAll();
  } catch (e) { toast('清除失败: ' + e.message, true); }
  finally {
    DUMATE_DEL_MOVING = false;
    ACCT_CONFIRM_OPEN = false;  // 确认时从 confirmAccountDelete 接手的锁，到这里才放
  }
}
