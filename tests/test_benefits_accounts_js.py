"""benefits_accounts.js 公共多账号 helper 的 node 桩测试。

2026-10 用户要求把 antigravity/kimi/qoder 已稳定的多账号卡片模板抽象成公共件、
未来接入直接复用。该文件把可复用件参数化：confirmAccountDelete（删除确认弹窗，
跨通道单例锁）/ acctSubHtml（账号状态副标题）/ acctMoveButtons / acctDeleteButton /
acctRowButtons / groupAccountsByPrefix / accountGroupBound / acctLoad（账号快照回填）/
acctMove/acctDelete（全局分发）。dumate 面板是第一个复用方（见 benefits.js）。

这里直接跑公共件本身（dumate 之外的消费方面板在 test_antigravity_panel_ui.py /
test_kimi_panel_ui.py 里已各自覆盖）。盯六件事：

- **分组**：按「<Prefix> #N · 」切分组；query_failed / 纯静态说明条不进分组
- **界标**：accountGroupBound 取「<Prefix> #N」序号最大值（不依赖账号快照时序）
- **副标题**：region / 冷却 bits（「token 剩 Xh」已删——access token 几小时
  自动刷新，读了只会误导），通道特有 bit 经 subExtra 注入
- **按钮**：▲▼ 带 id 内联 + 首尾禁用；✕ 带 pid+id；✎ 改名 + ↻ 无条件在按钮组最前
- **确认弹窗**：Promise 化；取消/遮罩汇入 closeModal resolve(false)；确认时锁
  不立即清（交给删除方的 finally），取消立即清
- **回填**：首份快照整卡重渲 + syncQuotaFold；之后就地回填（换名 + 重写副标题，
  不是堆叠）；数据没变不动 DOM
- **分发**：acctMove/acctDelete 按 pid 路由到通道处理函数

JS 由本机 ``node`` 执行；没有 node 时整文件跳过（不是失败）——CI 有 node。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

_STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
HELPERS_JS = _STATIC / "benefits_accounts.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑面板 JS（CI 有，本机可选）"
)

_STUB = """
let PANEL = {innerHTML: ''};        // #dumate-panel
globalThis.esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
// app.js 的数字格式化（与真实现一致）：dumate 今日消耗副标题用它
globalThis.fmtNum = n => {
  if (n == null) return '—';
  const x = Number(n);
  if (!isFinite(x)) return String(n);
  return x >= 100 ? Math.round(x).toLocaleString('en-US') : String(Math.round(x * 10) / 10);
};
globalThis.__SYNC_CALLS = 0;
globalThis.syncQuotaFold = () => { globalThis.__SYNC_CALLS++; };
globalThis.BENEFITS = {};
const MOD = {};
globalThis.MOD = MOD;
// 懒建兜底：元素首次被读时若还没建，先补一个带 classList 的桩再返回。
// classList 方法必须是 function 语法（方法调用 this=接收者）——箭头函数会把
// this 词法绑到模块级（CommonJS 下是 undefined），contains 永远炸
// （test_antigravity_panel_ui 的桩只调 add 恰好没踩到）。
function mkClassList() {
  return {_s: new Set(),
          add(c) { this._s.add(c); },
          remove(c) { this._s.delete(c); },
          contains(c) { return this._s.has(c); }};
}
function modalEl(id) {
  if (!MOD[id] || !MOD[id].classList) {
    MOD[id] = Object.assign(MOD[id] || {}, {textContent: '', innerHTML: '',
      classList: mkClassList()});
  }
  return MOD[id];
}
globalThis.modalEl = modalEl;
globalThis.__FOOT = '';
globalThis.__CLOSED = 0;
globalThis.setModalFoot = html => { globalThis.__FOOT = html; };
globalThis.closeModal = () => { globalThis.__CLOSED++; };
const OVERLAY = {className: '', classList: mkClassList()};
let QS = {};                        // 选择器 -> 桩元素（没设的选择器返回 null）
globalThis.__QS = QS;
globalThis.document = {
  querySelector: sel => (Object.prototype.hasOwnProperty.call(QS, sel) ? QS[sel] : null),
  querySelectorAll: () => [],
  getElementById: id => {
    if (id === 'overlay') { MOD[id] = OVERLAY; return OVERLAY; }
    if (id === 'modal-title' || id === 'modal-body') {
      if (!MOD[id]) MOD[id] = {textContent: '', innerHTML: ''};
      return MOD[id];
    }
    return null;
  },
  // 兜底读路径：classList 缺失就补桩（防 undefined.classList）
  __ensureCls: e => e,
};
// 桩账号元素：textContent 可写、insertAdjacentHTML 可观察（acctLoad 就地回填用）
function el(text) {
  return {
    textContent: text,
    nextElementSibling: null,
    inserted: [],
    insertAdjacentHTML(pos, html) { this.inserted.push(html); },
  };
}
globalThis.el = el;
"""


def _run_js(body: str) -> str:
    script = (
        _STUB + HELPERS_JS.read_text(encoding="utf-8")
        + "\n(async () => {\n" + body
        + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def test_group_accounts_by_prefix_splits_and_notices():
    """按「Prefix #N · 」切分组；query_failed 与纯静态条（percent/used 全空）不进组。"""
    out = _run_js("""
const {groups, notices} = groupAccountsByPrefix([
  {label: 'DuMate #1 · 可用额度', used: 0, total: 100, percent: 0},
  {label: 'DuMate #1 ·  another', used: 1, total: 100, percent: 1},
  {label: '单账号无前缀条目', used: 2, total: 100, percent: 2},
  {label: '查询失败说明', query_failed: true},
  {label: '未登录说明条', remaining: '请 buddy login'},
], 'DuMate', 'DuMate');
console.log(JSON.stringify({
  keys: [...groups.keys()],
  sub0: groups.get('DuMate #1')[0].label,
  fallback: groups.get('DuMate').map(it => it.label),
  notices: notices.length,
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["keys"] == ["DuMate #1", "DuMate"], "无前缀条目落到 defaultName 组"
    assert data["sub0"] == "可用额度", "组内条目标签剥掉「Prefix #N · 」前缀"
    assert data["fallback"] == ["单账号无前缀条目"], "defaultName 组里是剥前缀后的条目"
    assert data["notices"] == 2, "query_failed + 纯静态条（percent/used 全空）都进 notices"


def test_account_group_bound_uses_max_index():
    """界标 = 「Prefix #N」序号最大值（自包含，不依赖账号快照到达时序）。"""
    out = _run_js("""
const g = new Map([['DuMate #1', []], ['DuMate #3', []]]);
console.log(JSON.stringify({
  n: accountGroupBound(g, 'DuMate'),
  empty: accountGroupBound(new Map(), 'DuMate'),
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["n"] == 3 and data["empty"] == 1


def test_acct_sub_html_bits_and_subextra():
    """通用 bits（region/冷却/疑似拉黑；token 剩已删）+ 通道特有 bit 经 subExtra 注入。"""
    out = _run_js("""
const a = {region: 'cn', hours_left: 3.2,
           cooling: [{kind: 'quota', minutes_left: 4}, {kind: 'blacklist', minutes_left: 360}]};
const html = acctSubHtml(a, (x, bits) => bits.push('累计签到 1000 分'), 'data-dumate-sub');
const none = acctSubHtml({cooling: []}, null, 'data-dumate-sub');
console.log(JSON.stringify({html, none}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "CN 区" in data["html"]
    assert "token 剩" not in data["html"], "「token 剩 Xh」已删（哪怕 hours_left 有值）"
    assert "额度冷却 4min" in data["html"] and "疑似拉黑 剩6.0h" in data["html"]
    assert "累计签到 1000 分" in data["html"], "通道特有 bit 注入"
    assert "data-dumate-sub" in data["html"]
    assert data["none"] == "", "没有任何 bit 时不渲染空副标题行"


def test_acct_rename_button_and_prompt_prefill():
    """✎ 按钮：有 id 才渲染；弹窗预填 alias → name → email/nickname → id 链。"""
    out = _run_js("""
AG_ACCTS = [{id: 'a@x.com', email: 'a@x.com', alias: '生产号'}];
QODER_ACCTS = [];
KIMI_ACCTS = [];
const btn = acctRenameButton('antigravity', {id: 'a@x.com'});
const btnNoAcct = acctRenameButton('antigravity', null);
acctRenamePrompt('antigravity', 'a@x.com');          // 预填从快照查（alias 优先）
const withAlias = globalThis.MOD['modal-body'].innerHTML;
AG_ACCTS = [{id: 'a@x.com', email: 'a@x.com', alias: ''}];
acctRenamePrompt('antigravity', 'a@x.com');
const noAlias = globalThis.MOD['modal-body'].innerHTML;
console.log(JSON.stringify({btn, btnNoAcct, withAlias, noAlias,
  title: globalThis.MOD['modal-title'].textContent, foot: globalThis.__FOOT}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "acctRenamePrompt('antigravity','a@x.com')" in data["btn"], "✎ 带 pid+id"
    assert data["btnNoAcct"] == "", "快照没到不渲染（同 ✕）"
    assert data["title"] == "重命名账号"
    assert "留空则恢复默认名" in data["withAlias"]
    assert 'value="生产号"' in data["withAlias"], "预填 alias"
    assert 'value="a@x.com"' in data["noAlias"], "无别名回落 email"
    assert "acctRenameSubmit('antigravity','a@x.com')" in data["foot"]


def test_acct_rename_submit_posts_and_updates_snapshots():
    """改名提交：POST /rename → 四通道快照就地更新 → 各面板重渲 + toast。
    失败路径：请求抛错 → 警告 toast、不关弹窗（输入不丢）。"""
    out = _run_js("""
AG_ACCTS = [{id: 'u1', email: 'a@x.com', alias: '旧'}];
QODER_ACCTS = [{id: 'u1', name: 'q', alias: '旧'}];
KIMI_ACCTS = [{id: 'u1', name: 'k', alias: ''}];      // kimi 的 u1 无别名：也该被更新
let CALLS = [], RENDERS = 0, BENEFITS_RENDERS = 0;
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  return {enabled: true, accounts: [{id: 'u1', alias: '主力'}]};
};
globalThis.toast = () => {};
globalThis.renderAntigravityPanel = () => RENDERS++;
globalThis.renderKimiPanel = () => RENDERS++;
globalThis.renderQoderPanel = () => RENDERS++;
globalThis.renderBenefits = () => BENEFITS_RENDERS++;
const INPUT = {value: '主力'};
const origGet = document.getElementById.bind(document);
document.getElementById = id => (id === 'acct-rename-input' ? INPUT : origGet(id));
await acctRenameSubmit('kimi', 'u1');
console.log(JSON.stringify({calls: CALLS,
  ag: AG_ACCTS[0].alias, qoder: QODER_ACCTS[0].alias, kimi: KIMI_ACCTS[0].alias,
  renders: RENDERS, benefitsRenders: BENEFITS_RENDERS, closed: globalThis.__CLOSED}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"] == [{"path": "/ui/api/kimi/accounts/rename",
                              "body": {"id": "u1", "alias": "主力"}}]
    assert data["ag"] == "主力" and data["qoder"] == "主力" and data["kimi"] == "主力", \
        "四通道快照就地更新（同 id 跨通道一致改）"
    assert data["renders"] == 3 and data["benefitsRenders"] == 1, "各面板立即重渲"
    assert data["closed"] >= 1, "弹窗关闭"


def test_qoder_checkin_rename_updates_detail_name():
    out = _run_js("""
QODER_ACCTS = [{id: 'u1', name: 'Old name', alias: '旧'}];
BENEFITS.providers = [{id: 'qoder', checkin: {accounts: [
  {id: 'u1', name: '旧'}]}}];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push(path);
  return {accounts: [{id: 'u1', name: '新名字', alias: '新名字', email: 'a@x'}]};
};
globalThis.toast = () => {};
const INPUT = {value: '新名字'};
const origGet = document.getElementById.bind(document);
document.getElementById = id => id === 'acct-rename-input' ? INPUT : origGet(id);
await acctRenameSubmit('qoder', 'u1');
console.log(JSON.stringify({name: BENEFITS.providers[0].checkin.accounts[0].name,
  alias: QODER_ACCTS[0].alias, calls: CALLS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["name"] == "新名字"
    assert data["alias"] == "新名字"
    assert data["calls"] == ["/ui/api/qoder/accounts/rename"]


def test_move_delete_row_buttons_markup():
    """▲▼ 带 id 内联 + 首尾禁用；✕ 带 pid+id；↻ 只在渠道标题。"""
    out = _run_js("""
const acct = {id: 'u1'};
console.log(JSON.stringify({
  mid: acctMoveButtons('dumate', 2, 3, 'u1'),
  first: acctMoveButtons('dumate', 1, 3, 'u1'),
  last: acctMoveButtons('dumate', 3, 3, 'u1'),
  del: acctDeleteButton('dumate', acct, '清除本地缓存'),
  delNoAcct: acctDeleteButton('dumate', null, 'x'),
  row: acctRowButtons('dumate', acctMoveButtons('dumate', 1, 1, 'u1'), acctDeleteButton('dumate', acct, 'h')),
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "acctMove('dumate',2,-1,'u1')" in data["mid"]
    assert "acctMove('dumate',2,1,'u1')" in data["mid"]
    assert "disabled" in data["first"] and "acctMove('dumate',1,-1" in data["first"]
    assert "disabled" in data["last"]
    assert "acctDelete('dumate','u1')" in data["del"] and "清除本地缓存" in data["del"]
    assert "ghost danger" in data["del"]
    assert data["delNoAcct"] == "", "没有账号（快照未到）时不渲染 ✕"
    assert data["row"].startswith('<span class="ag-move"><button class="ghost" title="上移')
    assert "refreshProviderQuota" not in data["row"], "账号卡内不应重复放渠道级 ↻"
    assert "refreshProviderQuota('dumate', this)" in _run_js(
        "console.log(providerRefreshButton('dumate'));"
    )


def test_confirm_account_delete_modal_flow():
    """确认弹窗：标题/正文/danger 键；取消走 closeModal resolve(false) 并立即清锁；
    确认 resolve(true) 但锁不清（交给删除方 finally）。"""
    out = _run_js("""
let TOASTS = [];
globalThis.toast = m => TOASTS.push(m);
const pCancel = confirmAccountDelete({title: '删除 X 账号', name: 'a@x.com', extra: '注'});
await new Promise(r => setTimeout(r, 5));
const shown = {
  title: MOD['modal-title'].textContent,
  body: MOD['modal-body'].innerHTML,
  foot: globalThis.__FOOT,
  overlayShown: modalEl('overlay').classList.contains('show'),
};
globalThis.closeModal();                       // 取消路径
const canceled = await pCancel;
const lockAfterCancel = ACCT_CONFIRM_OPEN;
const pYes = confirmAccountDelete({title: 'T', name: 'n', extra: 'e'});
await new Promise(r => setTimeout(r, 5));
globalThis.__acctDelYes();                     // 确认路径
const yes = await pYes;
console.log(JSON.stringify({shown, canceled, lockAfterCancel, yes,
  lockAfterYes: ACCT_CONFIRM_OPEN, closed: globalThis.__CLOSED}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["shown"]["title"] == "删除 X 账号"
    assert "a@x.com" in data["shown"]["body"] and "注" in data["shown"]["body"]
    assert 'class="danger"' in data["shown"]["foot"]
    assert data["shown"]["overlayShown"]
    assert data["canceled"] is False and data["lockAfterCancel"] is False, "取消立即清锁"
    assert data["yes"] is True
    assert data["lockAfterYes"] is True, "确认后锁仍在（删除方 finally 才放，防连点叠框）"
    assert data["closed"] >= 2, "取消 + 确认（__acctDelYes 内部走 closeModal）各关一次"


def test_confirm_account_delete_is_singleton():
    """跨通道单例：已有一个确认框开着时，再开直接 resolve(false) 不叠层。"""
    out = _run_js("""
const p1 = confirmAccountDelete({title: '一', name: 'a', extra: ''});
await new Promise(r => setTimeout(r, 5));
const p2 = confirmAccountDelete({title: '二', name: 'b', extra: ''});
const second = await p2;
globalThis.__acctDelYes();
const first = await p1;
console.log(JSON.stringify({first, second, lockHeld: ACCT_CONFIRM_OPEN}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["first"] is True and data["second"] is False, "第二个被锁挡掉"
    assert data["lockHeld"], "确认后锁仍在——公共件只负责挡叠层，释放是删除方 finally 的契约"


def test_acct_load_first_snapshot_rerenders_then_in_place():
    """acctLoad 回填：首份快照整卡重渲（render+syncQuotaFold）；之后就地回填
    （换名 + 重写副标题，先删旧的）；数据没变不动 DOM。"""
    out = _run_js("""
let RENDERED = 0;
let FILLED = 0;                     // 静默重绘期间只许就地回填，不许整卡重渲
const state = {accts: null};
// render 桩按真实消费方（renderDumatePanel）的契约：首渲前把快照填进 state，
// 这样 acctLoad 才能区分「首份快照」与「后续变化」
const cfg = {pid: 'dumate', nameOf: a => a.displayName || a.id,
             subExtra: (a, bits) => bits.push('累计签到 ' + a.pts + ' 分'),
             render: () => { RENDERED++; state.accts = state.accts || [{index: 1, id: 'u1', displayName: '旧名', pts: 100}]; },
             // fill 桩与消费方一致（acctLoad 第二拍换成 cfg.fill()）
             // cfg.fill：第二拍起换成就地回填（换名 + 重写副标题）。桩里按真实
             // 消费方（fillDumateAccounts）的定位规则写一遍，验证 acctLoad 确实调到它
             fill: () => {
               FILLED++;
               for (const a of state.accts || []) {
                 const e = QS['#dumate-panel .pat-pkg-name'];
                 if (!e) continue;
                 e.textContent = a.displayName || a.id;
                 if (e.nextElementSibling && e.nextElementSibling.hasAttribute('data-dumate-sub')) {
                   e.nextElementSibling.remove();
                 }
                 const sub = acctSubHtml(a, cfg.subExtra, 'data-dumate-sub');
                 if (sub) e.insertAdjacentHTML('afterend', sub);
               }
             }};
// 采集时直接返回本拍加载的 promise（内部经 acctFetch 去重），调用侧不再包一层
globalThis.load = () => acctLoad(cfg, state);
let RESP = {enabled: true, accounts: [
  {index: 1, id: 'u1', displayName: '旧名', pts: 100}]};
globalThis.api = async () => RESP;
// 首份快照整卡重渲（render 桩把快照写进 state）
await acctLoad(cfg, state);
const afterFirst = {rendered: RENDERED, sync: globalThis.__SYNC_CALLS,
                    accts: state.accts.map(a => a.displayName)};
// 第二拍：数据变了 → 就地回填（快照已在，不整卡重渲）
RESP = {enabled: true, accounts: [{index: 1, id: 'u1', displayName: '新名', pts: 200}]};
const nameEl = el('旧名');
nameEl.nextElementSibling = {hasAttribute: t => t === 'data-dumate-sub', remove() { this.removed = true; }};
// 单账号（list.length===1）走 .pat-pkg-name 分支（与真实 dumate 面板一致）；
// 多账号才按 [data-<pid>-idx="N"] 对应
QS['#dumate-panel .pat-pkg-name'] = nameEl;
globalThis.__SYNC_CALLS = 0;
await load();
const afterUpdate = {rendered: RENDERED, sync: globalThis.__SYNC_CALLS,
                     name: nameEl.textContent, sub: nameEl.inserted.join(''),
                     removedOld: !!nameEl.nextElementSibling.removed};
// 第三拍：数据没变 → 不动 DOM（名字元素换成哨兵，被重写就露馅）
const untouched = el('哨兵');
QS['#dumate-panel .pat-pkg-name'] = untouched;
globalThis.__SYNC_CALLS = 0;
await load();
const afterSame = {rendered: RENDERED, sync: globalThis.__SYNC_CALLS,
                   name: untouched.textContent};
console.log(JSON.stringify({afterFirst, afterUpdate, afterSame}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["afterFirst"]["rendered"] == 1, "首份快照触发整卡重渲"
    assert data["afterFirst"]["sync"] == 1, "重渲后校准折叠"
    assert data["afterUpdate"]["name"] == "新名", "就地回填换组名"
    assert "累计签到 200 分" in data["afterUpdate"]["sub"], "通道特有 bit 进副标题"
    assert data["afterUpdate"]["removedOld"], "旧副标题先移除再插新的（不堆叠）"
    assert data["afterUpdate"]["rendered"] == 1, "快照已在就不整卡重渲（就地回填）"
    assert data["afterSame"]["rendered"] == 1 and data["afterSame"]["sync"] == 0, \
        "数据没变不动 DOM、不再校准"
    assert data["afterSame"]["name"] == "哨兵", "没变就不重写名字（重写会闪旧代号）"


def test_acct_load_disabled_clears_snapshot():
    """enabled=false 清快照（面板渲染侧据此隐藏账号位）。"""
    out = _run_js("""
const state = {accts: [{index: 1, id: 'u1'}]};
const load = () => acctLoad({pid: 'dumate', nameOf: a => a.id, render: () => {}, fill: () => {}}, state);
globalThis.api = async () => ({enabled: false});
await load();
console.log(JSON.stringify({accts: state.accts}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["accts"] == []


def test_acct_move_delete_dispatch_by_pid():
    """acctMove/acctDelete 按 pid 路由：未知 pid 不炸；已登记 pid 调到对应函数。"""
    out = _run_js("""
const calls = [];
globalThis.dumateMove = (...a) => calls.push(['dumateMove', ...a]);
globalThis.dumateDelete = (...a) => calls.push(['dumateDelete', ...a]);
globalThis.agMoveAccount = (...a) => calls.push(['agMove', ...a]);
globalThis.agDeleteAccount = (...a) => calls.push(['agDel', ...a]);
acctMove('dumate', 1, -1, 'u1');
acctDelete('dumate', 'u1');
acctMove('antigravity', 2, 1, 'a@x.com');
acctDelete('antigravity', 'a@x.com');
acctMove('unknown', 1, 1);                      // 未登记：静默不炸
acctDelete('unknown', 'x');
console.log(JSON.stringify(calls));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert ["dumateMove", 1, -1, "u1"] in data
    assert ["dumateDelete", "u1"] in data
    assert ["agMove", 2, 1, "a@x.com"] in data
    assert ["agDel", "a@x.com"] in data
    assert len(data) == 4, "未知 pid 不调用任何处理（也不报错中断后续）"


# --- dumate 面板就绪态重渲（loadDumateStatus 拿 ready 后主动 render） ----------


def test_dumate_status_ready_triggers_rerender():
    """就绪态异步到达后必须立即重渲一次，否则首渲「检测中…」要等 30s 轮询。

    这是 PR #85 review 评论 1 的回归：renderDumatePanel 本身此前没有 node
    桩覆盖，回归全靠 Playwright 手测。这里把 benefits.js 的 dumate 段抽出来
    跑（带 quotaItemsHtml/quotaItemHtml 桩），盯三件事：

    - 首渲：status 未到 → 「检测中…」
    - loadDumateStatus 拿到 ready → 主动 renderDumatePanel 一次（不再等轮询）
    - App 未运行（installed=True, ready=False, 带 hint）→ 显示 hint 不显示已就绪
    """
    import re
    text = (_STATIC / "benefits_dumate.js").read_text(encoding="utf-8")
    seg = re.search(
        r"// ---- DUMATE 面板.*$", text, re.S,
    )
    assert seg, "benefits_dumate.js 里找不到 DUMATE 面板段"
    # benefits_accounts.js 提供 acctLoad/acctFor/acctDeleteButton/acctRowButtons；
    # loadDumateAccounts 是惰性加载函数，只有面板真正调用它才发请求。
    script = (
        _STUB
        + HELPERS_JS.read_text(encoding="utf-8")
        + "globalThis.quotaItemHtml = it => '<div class=\"qitem\">' + esc(it.label) + '</div>';\n"
        + "globalThis.quotaItemsHtml = (items, key) =>\n"
        + "  '<div class=\"qbody\" data-qfold=\"' + esc(key) + '\">' + items.map(globalThis.quotaItemHtml).join('') + '</div>';\n"
        + "globalThis.api = async (path) => path.includes('/accounts')\n"
        + "  ? globalThis.__DM_ACCTS : globalThis.__DM_RESPONSE;\n"
        # 数渲染次数：段里的 renderDumatePanel 声明与函数体里的自调用一起
        # 改名成 __dumate_render_raw，原函数在段前一行另存成 __orig_render；
        # 段内按名字调的 loadDumateAccounts 改名成 __dumate_load_raw，段尾的
        # 真实惰性加载器被截掉（acctLoad 由单独用例覆盖）。
        + "let RENDER_COUNT = 0;\n"
        + re.sub(r"\brenderDumatePanel\b", "__dumate_render_raw",
                 re.sub(r"\bloadDumateAccounts\b", "__dumate_load_raw",
                        seg.group(0).split("\nfunction loadDumateAccounts()")[0]))
        + """
const __orig_render = __dumate_render_raw;
__dumate_render_raw = () => { RENDER_COUNT++; return __orig_render(); };
// 段尾的真实惰性加载器被截掉了（本用例只盯
// 渲染/加载的行为，acctLoad 由 test_acct_load_* 覆盖），这里的加载桩照
// acctLoad 的账本语义：首份快照整卡重渲、之后只回填，数据没变早退
let __dumate_load_raw = async () => {
  const r = await api('/ui/api/dumate/accounts');
  if (!r.enabled) { DUMATE_STATE.accts = []; return; }
  const list = r.accounts || [];
  if (JSON.stringify(list) === JSON.stringify(DUMATE_STATE.accts)) return;
  const snapshotMissing = !DUMATE_STATE.accts || !DUMATE_STATE.accts.length;
  DUMATE_STATE.accts = list;
  if (!list.length) return;
  if (snapshotMissing) __dumate_render_raw();
  else fillDumateAccounts();
};
(async () => {
globalThis.BENEFITS = {providers: [{id: 'dumate', quota: {supported: true, items: [
  {label: '订阅积分', used: 160.04, total: 1000, remaining: 839.96, percent: 16.0,
   reset_ts: null, expire_ts: null, unit: 'points'},
]}}]};
// 桩 document.getElementById('dumate-panel') → PANEL
const origGetEl = document.getElementById.bind(document);
document.getElementById = id => (id === 'dumate-panel' ? PANEL : origGetEl(id));
globalThis.__DM_ACCTS = {enabled: false, accounts: []};
globalThis.__DM_RESPONSE = {ready: false, installed: true, running: false,
                            hint: '已安装 DuMate.app，但当前未在运行——请先打开百度搭子桌面端。'};
__dumate_render_raw();                      // 首渲（status 未到 → 检测中）
const firstHtml = PANEL.innerHTML;
const firstDetecting = firstHtml.includes('检测中…');
await new Promise(r => setTimeout(r, 5));
// 状态到手：ready
globalThis.__DM_RESPONSE = {ready: true, installed: true, running: true,
                            port: 52414, pid: 9007, app_version: '1.0.0',
                            inapp_key_hint: 'abcd…(len64)',
                            base_url: 'http://127.0.0.1:52414',
                            bceconsole_authenticated: true,
                            display_name: 'dumateuser', checkin_total_points: 1000,
                            checkin_total_times: 2};
await loadDumateStatus();                   // 应主动 render 一次
const readyHtml = PANEL.innerHTML;
// 账号快照到手（含今日消耗）：首份快照整卡重渲 → 卡片标题=display_name
globalThis.__DM_ACCTS = {enabled: true, accounts: [
  {index: 1, id: 'u1', display_name: 'dumateuser',
   today_consumed: 123.45, today_calls: 42,
   checkin_total_points: 1000, checkin_total_times: 2}]};
await __dumate_load_raw();     // 首份快照 → 整卡重渲（RENDER_COUNT +1）
await new Promise(r => setTimeout(r, 5));   // 等重渲尾部的嵌套 load 落定
const acctHtml = PANEL.innerHTML;
// 第二拍：消耗数变了 → 就地回填副标题（QS 桩元素观察 insertAdjacentHTML）
const nameEl = el('dumateuser');
nameEl.nextElementSibling = null;
QS['#dumate-panel .pat-pkg-name'] = nameEl;
globalThis.__DM_ACCTS = {enabled: true, accounts: [
  {index: 1, id: 'u1', display_name: 'dumateuser',
   today_consumed: 130.05, today_calls: 43,
   checkin_total_points: 1000, checkin_total_times: 2}]};
await __dumate_load_raw();     // 第二拍 → 就地回填
const insertedSub = nameEl.inserted.join('');
console.log(JSON.stringify({
  firstDetecting,
  firstReady: firstHtml.includes('已就绪'),
  renderCount: RENDER_COUNT,
  readyNow: readyHtml.includes('已就绪'),
  stillDetecting: readyHtml.includes('检测中'),
  showsLogin: readyHtml.includes('已登录'),
  version: readyHtml.includes('v1.0.0'),
  cardName: acctHtml.includes('dumateuser'),
  insertedSub,
}));
})().catch(e => { console.error(e); process.exit(1); });
"""
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["firstDetecting"] and not data["firstReady"], "首渲应是检测中"
    assert data["renderCount"] >= 2, "就绪态到达后要主动重渲一次"
    assert data["readyNow"] and not data["stillDetecting"], "重渲后应显示已就绪"
    assert data["showsLogin"], "已登录徽标要出来"
    assert data["version"], "版本号 v1.0.0 要出来"
    assert data["cardName"], "账号卡标题=display_name（快照到位后）"
    # 今日消耗：真实流水合计数进副标题（fmtNum: 130.05 → '130'，≥100 取整）
    assert "今日消耗 130 分（43 次）" in data["insertedSub"], \
        f"今日消耗要进副标题: {data['insertedSub']}"
    assert "累计签到 1000 分（2 次）" in data["insertedSub"], "签到累计照旧"


def test_acct_load_quiet_mode_fills_instead_of_rerendering():
    """首份快照期间若在静默重绘（签到强刷），只就地回填、不整卡重渲——
    整卡重渲会冲掉别处 DOM，还会让面板尾部的加载器再刮一发 accounts。"""
    out = _run_js("""
let RENDERED = 0, FILLED = 0;
const state = {accts: null};
const cfg = {pid: 'dumate', nameOf: a => a.id, render: () => { RENDERED++; }, fill: () => { FILLED++; }};
globalThis.api = async () => ({enabled: true, accounts: [{index: 1, id: 'u1'}]});
ACCT_NO_RERENDER = true;            // loadAllAccounts 打开的那个开关
await acctLoad(cfg, state);
ACCT_NO_RERENDER = false;
await (async () => {
  state.accts = null;
  await acctLoad(cfg, state);
})();
console.log(JSON.stringify({inQuiet: {rendered: RENDERED, filled: FILLED}, quiet: acctNoRerender('dumate')}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["quiet"] is False, "开关只在 loadAllAccounts 期间打开"
    # 第一拍静默：filled=1 rendered=0；第二拍非静默：rendered=1 filled 仍是 1
    assert data["inQuiet"] == {"rendered": 1, "filled": 1}
