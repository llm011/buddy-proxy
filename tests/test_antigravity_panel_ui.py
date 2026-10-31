"""Antigravity 面板「标题=邮箱、副标题=账号状态」的前端测试。

额度接口（/ui/api/benefits）的 label 只带「AG #N · 」前缀、不含邮箱，账号
状态接口（/ui/api/antigravity/accounts）才有 email——渲染时序上额度先出、
账号后到，标题/副标题的回填只能发生在 accounts 回来之后。这里把整段面板
代码抽出来用最小 DOM 桩真跑，盯四件事：

- **标题=邮箱**：多账号按 ``data-ag-idx`` 对应替换；单账号组名没有前缀，
  唯一标题直接替换——别退回显示「AG #1」/「Antigravity」这种内部代号
- **副标题=状态**：标题下一行 muted 小字（缺 project / 冷却），
  且数据变化时重写（冷却结束要消失）、不重复堆叠；「token 剩 Xh」已删
  （access token 几小时自动刷新，读了只会误导）
- **不闪内部代号**：有 AG_ACCTS 快照时 render 直接渲染邮箱，30s 轮询重建
  面板不再闪回「AG #N」
- **底部状态区已删**：状态全部上移为各账号副标题，不再有独立区块

JS 由本机 ``node`` 执行；没有 node 时整文件跳过（不是失败）——CI 有 node。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

_STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
# 2026-10 前端按职责拆分（benefits.js 超 700 行）：公共多账号 helper 抽到
# benefits_accounts.js（confirmAccountDelete 单例 + __acctDelYes），
# ANTIGRAVITY 面板段挪到 benefits_panels.js（下一段是 QODER 面板段头）。
BENEFITS_JS = _STATIC / "benefits_antigravity.js"
HELPERS_JS = _STATIC / "benefits_accounts.js"
SEG_RE = re.compile(r"// ---- ANTIGRAVITY 面板.*$", re.S)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑面板 JS（CI 有，本机可选）"
)

_STUB = """
let QS = {};                        // 选择器 -> 桩元素（没设的选择器返回 null）
let PANEL = {innerHTML: ''};        // #antigravity-panel
// 对账本路径：首份快照之后的就地回填现在直接整卡重渲（与渲染同一份快照，
// 不叠第二份按钮组）；渲染清空 PANEL 时报一声，用例自己挂的桩元素就能撤掉
// ——跟真 DOM 一样，面板 innerHTML 一换旧节点就不再挂着了
globalThis.__ON_RENDER = null;
let __PANEL_INNER = '';
Object.defineProperty(PANEL, 'innerHTML', {
  get() { return __PANEL_INNER; },
  set(v) {
    __PANEL_INNER = v;
    // 旧节点随 innerHTML 一起没了：桩选择器也撤掉，后续 fill 只能补到新节点上
    QS = {};
    if (globalThis.__ON_RENDER) globalThis.__ON_RENDER(v);
  },
});
globalThis.esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
globalThis.api = async () => globalThis.__RESPONSE;
globalThis.quotaItemHtml = it => '<div class="qitem">' + esc(it.label) + '</div>';
// 面板现在走 quotaItemsHtml（分组 + 折叠，见 test_quota_fold.py）；这里只关心
// 面板自身的布局与按钮，折叠那层照样打桩——不桩的话 syncQuotaFold 还要 DOM
globalThis.quotaItemsHtml = (items, key) =>
  '<div class="qbody" data-qfold="' + esc(key) + '">' +
  items.map(globalThis.quotaItemHtml).join('') + '</div>';
globalThis.BENEFITS = {};
// syncQuotaFold 定义在折叠区块（不在本测试切片里），打桩成计数器：
// 面板每次改完高度都该校准一次折叠，漏调就是「整块没裁、按钮不露」
globalThis.__SYNC_CALLS = 0;
globalThis.syncQuotaFold = () => { globalThis.__SYNC_CALLS++; };
// 删除确认弹窗是公共件的 confirmAccountDelete 单例：锁 ACCT_CONFIRM_OPEN、
// 确认按钮挂 __acctDelYes。panels 里旧的同名定义（AG_CONFIRM_OPEN/__agDelYes）
// 已删——经典脚本后加载覆盖前加载，两份同名函数在浏览器里是灾难（dumate 清
// ACCT_CONFIRM_OPEN、其余清 AG_CONFIRM_OPEN，确认过一次 ✕ 后锁永久卡死）。
globalThis.document = {
  querySelector: sel => (Object.prototype.hasOwnProperty.call(QS, sel) ? QS[sel] : null),
  querySelectorAll: () => [],
  getElementById: id => {
    if (id === 'antigravity-panel') return PANEL;
    // 删除确认弹窗（agConfirmDelete）用到的 overlay/modal 骨架元素
    if (id === 'modal-title' || id === 'modal-body' || id === 'overlay') {
      if (!globalThis.MODAL[id]) globalThis.MODAL[id] = {textContent: '', innerHTML: '', className: '',
        classList: {_s: new Set(),
                    add(c) { this._s.add(c); },
                    remove(c) { this._s.delete(c); },
                    contains(c) { return this._s.has(c); }}};
      return MODAL[id];
    }
    return null;
  },
};
// app.js 的弹窗全局（benefits.js 先加载，运行时才引用）——桩成可观察的
globalThis.MODAL = {};
globalThis.__FOOT = '';
globalThis.__CLOSED = 0;
globalThis.setModalFoot = html => { globalThis.__FOOT = html; };
globalThis.closeModal = () => { globalThis.__CLOSED++; };
function el(text) {
  return {
    textContent: text,
    nextElementSibling: null,
    inserted: [],
    insertAdjacentHTML(pos, html) { this.inserted.push(html); },
    closest() { return null; },
    querySelector() { return null; },
  };
}
"""


def _run_js(body: str) -> str:
    text = BENEFITS_JS.read_text(encoding="utf-8")
    match = SEG_RE.search(text)
    assert match, "benefits.js 里找不到 ANTIGRAVITY 面板段（边界注释被改了？）"
    script = (
        _STUB
        # 公共删除确认弹窗（单例 + ACCT_CONFIRM_OPEN），面板段闭包引用它
        + HELPERS_JS.read_text(encoding="utf-8")
        # ANTIGRAVITY 面板段（renderAntigravityPanel + loadAntigravityAccounts +
        # AG_ACCTS/_ag_* 辅助；confirmAccountDelete 已走公共 helper）
        + match.group(0)
        + "\n(async () => {\n" + body
        + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def test_accounts_fill_names_and_subtitles():
    """多账号：标题按 data-ag-idx 换成邮箱，副标题带状态且不含 email。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'AG #1 · Gemini 组', remaining: 900, total: 1000, percent: 10,
   used: 100, reset_ts: null, unit: 'permille'},
  {label: 'AG #2 · Gemini 组', remaining: 800, total: 1000, percent: 20,
   used: 200, reset_ts: null, unit: 'permille'},
]}}]};
globalThis.__RESPONSE = {enabled: true, accounts: [
  {index: 1, id: 'a@x.com', email: 'a@x.com', alias: '生产号', project_id: 'p', hours_left: 3.2, cooling: []},
  {index: 2, id: 'b@x.com', email: 'b@x.com', alias: '', project_id: '', hours_left: null,
   cooling: [{kind: 'quota', minutes_left: 4}]},
]};
// 快照已有旧值（回填路径；null 会走「首份快照整卡重渲」分支）
AG_ACCTS = [
  {index: 1, email: 'old@x.com', project_id: 'p', hours_left: 9, cooling: []},
  {index: 2, email: 'old2@x.com', project_id: '', hours_left: null, cooling: []},
];
await loadAntigravityAccounts();
// 首份快照之后的就地回填直接整卡重渲（同一份快照、不叠第二份按钮组），
// 名字/副标题从渲染出的 HTML 里看
const parts = PANEL.innerHTML.split('data-ag-idx="').slice(1);   // 每段=一个账号块
const nameOf = i => { const seg = parts.find(x => x.startsWith(i + '"')); return seg ? (seg.match(/^[^>]*>([^<]*)</) || [])[1] : ''; };
const subOf = i => { const seg = parts.find(x => x.startsWith(i + '"')); const m = seg && seg.match(/<[^]*?<div class="qbody"/); return m ? m[0].slice(7, -18) : ''; };
console.log(JSON.stringify({
  n1: nameOf(1), n2: nameOf(2),
  sub1: subOf(1), sub2: subOf(2),
  btns: (PANEL.innerHTML.match(/class="ag-move"/g) || []).length}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["n1"] == "生产号", "标题 1 优先显示别名（✎ 改的名）"
    assert data["n2"] == "b@x.com", "标题 2 没别名回落邮箱"
    assert "token 剩" not in data["sub1"], "「token 剩 Xh」已删"
    assert "额度冷却 4min" in data["sub2"], data["sub2"]
    assert "缺 project" in data["sub2"] and "额度冷却 4min" in data["sub2"], data["sub2"]
    assert "a@x.com" not in data["sub1"] and "b@x.com" not in data["sub2"], \
        "邮箱在标题上，副标题只放状态"
    assert data["btns"] == 2, "按钮组每组一份，不能叠成两份（绝对定位叠着看不出来）"


def test_single_account_name_and_subtitle_without_index():
    """单账号标题没有 AG #N 前缀（后端 label 不加）→ 唯一标题直接换。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'Gemini 组', remaining: 900, total: 1000, percent: 10,
   used: 100, reset_ts: null, unit: 'permille'}]}}]};
globalThis.__RESPONSE = {enabled: true, accounts: [
  {index: 1, id: 'solo@x.com', email: 'solo@x.com', project_id: 'p', hours_left: 0.7, cooling: []},
]};
AG_ACCTS = [{index: 1, email: 'stale@x.com', project_id: '', hours_left: null, cooling: []}];
await loadAntigravityAccounts();
const html = PANEL.innerHTML;
console.log(JSON.stringify({
  name: (html.match(/class="pat-pkg-name">([^<]*)</) || [])[1],
  sub: (html.match(/class="pat-pkg-name">[^<]*<\/span>([\s\S]*?)<div class="qbody"/) || [])[1] || ''}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["name"] == "solo@x.com", "单账号标题应替换成邮箱，而不是停在 'Antigravity'"
    assert "token 剩" not in data["sub"], "「token 剩 Xh」已删，副标题只剩冷却/缺 project 位"


def test_changed_data_rewrites_subtitle_without_duplicating():
    """数据变化：副标题**重写**（旧内容消失），而不是在旁边再插一份。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'AG #1 · Gemini 组', remaining: 900, total: 1000, percent: 10,
   used: 100, reset_ts: null, unit: 'permille'}]}}]};
globalThis.__RESPONSE = {enabled: true, accounts: [
  {index: 1, id: 'a@x.com', email: 'a@x.com', project_id: 'p', hours_left: 2, cooling: []},
]};
AG_ACCTS = [];   // 首份快照：整卡重渲，旧副标题随 innerHTML 一起没了
await loadAntigravityAccounts();
const html = PANEL.innerHTML;
console.log(JSON.stringify({
  subs: (html.match(/data-ag-sub/g) || []).length,
  sub: (html.match(/class="pat-pkg-name">[^<]*<\/span>([\s\S]*?)<div class="qbody"/) || [])[1] || ''}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["subs"] == 0, "冷却结束后副标题整行消失（token 剩已删，无别的 bit）"
    assert data["sub"] == "", data["sub"]


def test_unchanged_data_leaves_dom_alone():
    """accounts 没变就不动 DOM——render 已用同一份快照渲染过，别把邮箱打回代号。"""
    accts = "[{index: 1, email: 'a@x.com', project_id: 'p', hours_left: 1, cooling: []}]"
    out = _run_js(f"""
AG_ACCTS = {accts};
globalThis.__RESPONSE = {{enabled: true, accounts: {accts}}};
QS['#antigravity-panel [data-ag-idx="1"]'] = el('a@x.com');   // render 渲染好的样子
await loadAntigravityAccounts();
console.log(JSON.stringify({{
  name: QS['#antigravity-panel [data-ag-idx="1"]'].textContent,
  touched: QS['#antigravity-panel [data-ag-idx="1"]'].inserted.length}}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["name"] == "a@x.com", "没变就不该重写（重写会闪一下内部代号）"
    assert data["touched"] == 0


def test_render_uses_snapshot_and_drops_status_block():
    """有快照时 render 直接出邮箱+副标题；底部「账号状态」区不再渲染。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'AG #1 · Gemini 组（组内共享 weekly + 5h 双池）',
   remaining: 999, total: 1000, percent: 0.1, used: null, reset_ts: null},
]}}]};
AG_ACCTS = [{index: 1, id: 'a@x.com', email: 'a@x.com', project_id: 'p', hours_left: 1.5, cooling: []}];
globalThis.__RESPONSE = {enabled: false};   // render 尾部会再拉一次，别让它改 DOM
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    html = data["html"]
    assert "a@x.com" in html, "有快照就该直接渲染邮箱（不闪 AG #1）"
    assert ">AG #1<" not in html, "内部代号不该露出来"
    # 账号一切正常（project 在、无冷却）时副标题整行不渲染——「token 剩」删了，
    # 这个账号没有可说的 bit；有 bit（缺 project/冷却）时才出 data-ag-sub 行
    assert "data-ag-sub" not in html and "token 剩" not in html, \
        "正常账号无副标题行（token 剩已删，无可说的 bit）"
    assert "acctRenamePrompt('antigravity','a@x.com')" in html, "标题行有 ✎ 改名入口"
    assert 'id="antigravity-accounts"' not in html and "账号状态" not in html, \
        "底部独立状态区已删，状态全部上移为副标题"


def test_render_without_snapshot_keeps_placeholder():
    """没快照（首屏）：先按 AG #N 占位渲染，等 accounts 回来再回填。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'AG #2 · Claude/GPT 组', remaining: 500, total: 1000, percent: 50,
   used: null, reset_ts: null},
]}}]};
globalThis.__RESPONSE = {enabled: false};
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert 'data-ag-idx="2"' in data["html"], "占位要挂序号供回填定位"
    assert ">AG #2<" in data["html"]


def test_disabled_accounts_clear_snapshot():
    """enabled=false（未登录）→ 快照清空；面板本来就隐藏，不该留旧账号残影。"""
    out = _run_js("""
AG_ACCTS = [{index: 1, email: 'a@x.com', project_id: 'p', hours_left: 1, cooling: []}];
globalThis.__RESPONSE = {enabled: false};
await loadAntigravityAccounts();
console.log(JSON.stringify({snapshot: AG_ACCTS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["snapshot"] == []


def test_move_buttons_multi_account_with_boundary_disables():
    """多账号：每块有 ▲▼，首位 ▲ 禁用、末位 ▼ 禁用；单账号不渲染按钮。"""
    multi = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'AG #1 · Gemini 组', remaining: 900, total: 1000, percent: 10, used: null, reset_ts: null},
  {label: 'AG #2 · Gemini 组', remaining: 500, total: 1000, percent: 50, used: null, reset_ts: null},
]}}]};
globalThis.__RESPONSE = {enabled: false};
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    html = json.loads(multi.strip().splitlines()[-1])["html"]
    assert html.count("agMoveAccount(") == 4, "两个账号各 ▲▼ 共 4 个按钮"
    assert "agMoveAccount(1,-1)" in html and "agMoveAccount(1,1)" in html
    assert "agMoveAccount(2,-1)" in html and "agMoveAccount(2,1)" in html
    # 首位 ▲、末位 ▼ 禁用（看按钮前后的 disabled）
    first_up = html.split("agMoveAccount(1,-1)")[0].rsplit("<button", 1)[-1]
    last_down = html.split("agMoveAccount(2,1)")[0].rsplit("<button", 1)[-1]
    assert "disabled" in first_up and "disabled" in last_down

    single = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'Gemini 组', remaining: 900, total: 1000, percent: 10, used: null, reset_ts: null},
]}}]};
globalThis.__RESPONSE = {enabled: false};
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    assert "agMoveAccount(" not in json.loads(single.strip().splitlines()[-1])["html"], \
        "单账号没有顺位可调，不应渲染按钮"


def test_move_account_posts_full_order_and_refreshes():
    """点 ▼（把 #1 下移）：POST 完整的重排后 id 列表，拿到响应后触发刷新。"""
    out = _run_js("""
AG_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com', project_id: 'p', hours_left: 1, cooling: []},
  {index: 2, id: 'b@x.com', email: 'b@x.com', project_id: 'p', hours_left: 1, cooling: []},
  {index: 3, id: 'c@x.com', email: 'c@x.com', project_id: 'p', hours_left: 1, cooling: []},
];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  if (path.endsWith('/order')) {
    return {enabled: true, accounts: [
      {index: 1, id: 'b@x.com', email: 'b@x.com'}, {index: 2, id: 'a@x.com', email: 'a@x.com'},
      {index: 3, id: 'c@x.com', email: 'c@x.com'}]};
  }
  return {enabled: true, accounts: []};
};
let TOASTS = [], REFRESHED = 0;
globalThis.toast = (m) => TOASTS.push(m);
globalThis.refreshAll = () => REFRESHED++;
await agMoveAccount(1, 1);   // #1 下移 → 期望 b,a,c
console.log(JSON.stringify({
  calls: CALLS, toasts: TOASTS, refreshed: REFRESHED,
  snapshot: AG_ACCTS.map(a => a.id)}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    order_call = [c for c in data["calls"] if c["path"].endswith("/order")]
    assert len(order_call) == 1
    assert order_call[0]["body"]["ids"] == ["b@x.com", "a@x.com", "c@x.com"], \
        "提交的是完整顺序（后端校验完整性）"
    assert data["refreshed"] == 1, "重排后要刷新额度（quota_epoch 变了，重查换新顺位）"
    assert data["snapshot"] == ["b@x.com", "a@x.com", "c@x.com"], "本地快照同步成响应"
    assert any("a@x.com" in t and "#2" in t for t in data["toasts"]), \
        f"toast 报被移动的账号与其新顺位: {data['toasts']}"


def test_move_buttons_carry_account_id_when_snapshot_present():
    """有快照时按钮内联账号 id：面板重绘滞后于快照时靠它定位（见 agMoveAccount）。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'AG #1 · Gemini 组', remaining: 900, total: 1000, percent: 10, used: null, reset_ts: null},
  {label: 'AG #2 · Gemini 组', remaining: 500, total: 1000, percent: 50, used: null, reset_ts: null},
]}}]};
AG_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com', cooling: []},
  {index: 2, id: 'b@x.com', email: 'b@x.com', cooling: []},
];
globalThis.__RESPONSE = {enabled: false};
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    html = json.loads(out.strip().splitlines()[-1])["html"]
    assert "agMoveAccount(1,-1,'a@x.com')" in html and "agMoveAccount(1,1,'a@x.com')" in html
    assert "agMoveAccount(2,1,'b@x.com')" in html, "每个按钮都带自己账号的 id"


def test_move_account_uses_id_position_when_dom_is_stale():
    """按钮 idx 过期（重排响应已回、面板未重绘）：按 id 校正，挪的是账号自己。"""
    out = _run_js("""
AG_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com'},
  {index: 2, id: 'b@x.com', email: 'b@x.com'},
  {index: 3, id: 'c@x.com', email: 'c@x.com'},
];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push(opts ? JSON.parse(opts.body).ids : null);
  if (path.endsWith('/order')) {
    const ids = JSON.parse(opts.body).ids;
    return {enabled: true, accounts: ids.map((id, i) => ({index: i+1, id, email: id}))};
  }
  return {enabled: true, accounts: []};
};
let TOASTS = [];
globalThis.toast = m => TOASTS.push(m);
globalThis.refreshAll = () => {};
await agMoveAccount(1, 1, 'a@x.com');   // [a,b,c] → [b,a,c]
// 面板还没重绘，屏幕上的按钮仍是 idx=1；但 a 的实际位次已是 #2。
// 不按 id 校正的话这次会挪到 b（提交 [a,b,c] 把整轮点回去）。
await agMoveAccount(1, 1, 'a@x.com');
console.log(JSON.stringify({calls: CALLS, final: AG_ACCTS.map(a => a.id), toasts: TOASTS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"][1] == ["b@x.com", "c@x.com", "a@x.com"], \
        f"第二次点按应按 id 定位到 a 的当前位次: {data['calls']}"
    assert data["final"] == ["b@x.com", "c@x.com", "a@x.com"]
    assert any("a@x.com" in t and "#3" in t for t in data["toasts"])


def test_move_account_fetches_snapshot_when_missing():
    """按钮随额度先到、快照还没回：点按时先补拉 accounts 再提交。"""
    out = _run_js("""
AG_ACCTS = null;
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  if (path.endsWith('/order')) return {enabled: true, accounts: []};
  return {enabled: true, accounts: [
    {index: 1, id: 'a@x.com', email: 'a@x.com'},
    {index: 2, id: 'b@x.com', email: 'b@x.com'}]};
};
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
await agMoveAccount(2, -1);  // #2 上移 → 期望 b,a
console.log(JSON.stringify({calls: CALLS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert len(data["calls"]) == 2, "先 GET 补快照，再 POST order"
    assert data["calls"][0]["path"] == "/ui/api/antigravity/accounts"
    assert data["calls"][1]["body"]["ids"] == ["b@x.com", "a@x.com"]


def test_move_account_inflight_clicks_ignored():
    """重排在途（POST + 额度重取还没完）：第二次点按直接忽略，不重复提交。"""
    out = _run_js("""
AG_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com'},
  {index: 2, id: 'b@x.com', email: 'b@x.com'},
  {index: 3, id: 'c@x.com', email: 'c@x.com'},
];
let CALLS = [], TOASTS = [];
globalThis.api = async (path, opts) => {
  CALLS.push(opts ? JSON.parse(opts.body).ids : null);
  if (path.endsWith('/order')) {
    const ids = JSON.parse(opts.body).ids;
    return {enabled: true, accounts: ids.map((id, i) => ({index: i+1, id, email: id}))};
  }
  return {enabled: true, accounts: []};
};
globalThis.toast = m => TOASTS.push(m);
globalThis.refreshAll = () => {};
const p1 = agMoveAccount(1, 1, 'a@x.com');
const p2 = agMoveAccount(1, 1, 'a@x.com');   // 在飞，应被忽略
await Promise.all([p1, p2]);
console.log(JSON.stringify({calls: CALLS, final: AG_ACCTS.map(a => a.id), toasts: TOASTS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"] == [["b@x.com", "a@x.com", "c@x.com"]], \
        f"在途点按不应产生第二次提交: {data['calls']}"
    assert data["final"] == ["b@x.com", "a@x.com", "c@x.com"]
    assert len(data["toasts"]) == 1, "只报一次"


def test_move_account_missing_id_is_noop():
    """按钮带的 id 不在快照里（账号刚被删）：宁可不动，也不按过期 idx 挪别人。"""
    out = _run_js("""
AG_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com'},
  {index: 2, id: 'b@x.com', email: 'b@x.com'},
];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push(path);
  return {enabled: true, accounts: []};
};
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
await agMoveAccount(1, 1, 'gone@x.com');   // 不在快照里的 id
console.log(JSON.stringify({calls: CALLS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"] == [], f"不应发任何请求: {data['calls']}"


# ---------------------------------------------------------------------------
# 删除账号（✕ 按钮 + agDeleteAccount）
# ---------------------------------------------------------------------------

def test_delete_button_renders_even_for_single_account():
    """✕ 不依赖多账号（单个被拉黑的账号恰恰最需要删）；要等快照给出 id。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'Gemini 组', remaining: 900, total: 1000, percent: 10, used: null, reset_ts: null},
]}}]};
AG_ACCTS = [{index: 1, id: 'a@x.com', email: 'a@x.com', cooling: []}];
globalThis.__RESPONSE = {enabled: false};
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    html = json.loads(out.strip().splitlines()[-1])["html"]
    assert "agDeleteAccount('a@x.com')" in html, "单账号也要有删除入口"
    assert "agMoveAccount(" not in html, "单账号没有顺位可调（与既有行为一致）"
    assert 'class="ghost danger"' in html, "删除 ✕ 用危险样式"


def test_delete_button_sits_next_to_move_buttons():
    """✕ 与 ▲▼ 同排（.ag-move 按钮组内）= 账号名那一行的行尾，不再孤零零挂块尾。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'AG #1 · Gemini 组', remaining: 900, total: 1000, percent: 10, used: null, reset_ts: null},
  {label: 'AG #2 · Gemini 组', remaining: 800, total: 1000, percent: 20, used: null, reset_ts: null},
]}}]};
AG_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com', cooling: []},
  {index: 2, id: 'b@x.com', email: 'b@x.com', cooling: []},
];
globalThis.__RESPONSE = {enabled: false};
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    html = json.loads(out.strip().splitlines()[-1])["html"]
    groups = re.findall(r'<span class="ag-move">(.*?)</span>', html, re.S)
    assert len(groups) == 2, f"每个账号一个按钮组: {html}"
    for g in groups:
        assert "agMoveAccount(" in g and "agDeleteAccount(" in g, \
            f"▲▼ 与 ✕ 要同排: {g}"
        assert 'class="ghost danger"' in g, f"✕ 用危险样式: {g}"

    nosnap = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'Gemini 组', remaining: 900, total: 1000, percent: 10, used: null, reset_ts: null},
]}}]};
AG_ACCTS = null;
globalThis.__RESPONSE = {enabled: false};
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    assert "agDeleteAccount(" not in json.loads(nosnap.strip().splitlines()[-1])["html"], \
        "快照没到（拿不到 id）时先不渲染删除按钮"


def test_delete_account_confirms_posts_and_updates_snapshot():
    """确认弹窗点「删除」后 POST /delete（带 id），快照同步、toast 报邮箱、触发刷新。"""
    out = _run_js("""
AG_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com', cooling: []},
  {index: 2, id: 'b@x.com', email: 'b@x.com', cooling: []},
];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  return {enabled: true, accounts: [{index: 1, id: 'b@x.com', email: 'b@x.com'}]};
};
let TOASTS = [], REFRESHED = 0;
globalThis.toast = m => TOASTS.push(m);
globalThis.refreshAll = () => REFRESHED++;
const p = agDeleteAccount('a@x.com');          // 打开确认弹窗，等用户表态
await new Promise(r => setTimeout(r, 5));
const shown = {
  title: globalThis.MODAL['modal-title'].textContent,
  body: globalThis.MODAL['modal-body'].innerHTML,
  foot: globalThis.__FOOT,
  overlayShown: globalThis.MODAL['overlay'].classList.contains('show'),
};
globalThis.__acctDelYes();                      // 点「删除该账号」
await p;
console.log(JSON.stringify({
  shown, calls: CALLS, toasts: TOASTS, refreshed: REFRESHED,
  snapshot: AG_ACCTS.map(a => a.id), closed: globalThis.__CLOSED}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["shown"]["title"] == "删除 Antigravity 账号"
    assert "a@x.com" in data["shown"]["body"], "弹窗正文带账号邮箱"
    assert 'class="danger"' in data["shown"]["foot"], "删除键用危险样式"
    assert data["shown"]["overlayShown"], "弹窗要真的展开"
    assert len(data["calls"]) == 1 and data["calls"][0]["path"].endswith("/delete")
    assert data["calls"][0]["body"] == {"id": "a@x.com"}
    assert data["snapshot"] == ["b@x.com"], "快照同步成删除后响应"
    assert data["refreshed"] == 1, "删除改变轮换组成，要刷新额度面板"
    assert any("a@x.com" in t for t in data["toasts"])
    assert data["closed"] >= 1, "确认后弹窗要关掉"


def test_first_snapshot_rerenders_panel_with_buttons():
    """首份快照到位（首屏 AG_ACCTS=null）：整卡重渲，✕ 不干等 30s 才出现。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'AG #1 · Gemini 组', remaining: 900, total: 1000, percent: 10, used: null, reset_ts: null},
]}}]};
AG_ACCTS = null;
globalThis.__RESPONSE = {enabled: true, accounts: [
  {index: 1, id: 'a@x.com', email: 'a@x.com', project_id: 'p', hours_left: 1, cooling: []},
]};
renderAntigravityPanel();   // 首渲（无快照占位）尾部拉账号 → 快照到位触发重渲
await new Promise(r => setTimeout(r, 10));   // render 内部 load 是 fire-and-forget，等它落地
console.log(JSON.stringify({html: PANEL.innerHTML, syncs: globalThis.__SYNC_CALLS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    html = data["html"]
    assert "agDeleteAccount('a@x.com')" in html, "首份快照就该带出 ✕，不等下一轮 30s"
    assert "a@x.com" in html and ">AG #1<" not in html, "重渲走快照直出，不闪内部代号"
    # 重渲换了整块 DOM（占位组名 → 邮箱、多一行副标题），高度变了要跟着校准；
    # 这条分支原先直接 return，校准得拖到下个 30s 轮询
    assert data["syncs"] >= 1, "首份快照重渲后要同步一次折叠校准"


def test_delete_account_cancelled_sends_nothing():
    """确认弹窗取消（closeModal 路径=取消按钮/遮罩/Esc）：不发请求、快照不动；
    取消后确认锁释放，再点 ✕ 还能重新弹。"""
    out = _run_js("""
AG_ACCTS = [{index: 1, id: 'a@x.com', email: 'a@x.com', cooling: []}];
let CALLS = [];
globalThis.api = async (path) => { CALLS.push(path); return {enabled: true, accounts: []}; };
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
const p = agDeleteAccount('a@x.com');
await new Promise(r => setTimeout(r, 5));      // 弹窗已渲染
const footShown = globalThis.__FOOT;
globalThis.closeModal();                        // 走被 agConfirmDelete 接管的关闭入口
const yes = await p;
// 取消后再点 ✕：确认锁已释放，应能重新弹出（行为断言——let 声明不进 globalThis）
const p2 = agDeleteAccount('a@x.com');
await new Promise(r => setTimeout(r, 5));
const reShown = globalThis.__FOOT;
globalThis.closeModal();
await p2;
console.log(JSON.stringify({canceled: !yes, calls: CALLS, snapshot: AG_ACCTS.map(a => a.id),
                            footShown, reShown}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["canceled"], "取消路径不得当作已确认"
    assert data["calls"] == []
    assert data["snapshot"] == ["a@x.com"]
    assert data["footShown"] and data["reShown"], "取消后确认锁要释放，下次 ✕ 还能弹"


def test_delete_account_inflight_clicks_ignored():
    """与顺位调整共用 AG_MOVING 在飞锁：确认框关掉后的连点只发一个请求。"""
    out = _run_js("""
AG_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com', cooling: []},
  {index: 2, id: 'b@x.com', email: 'b@x.com', cooling: []},
];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push(path);
  await new Promise(r => setTimeout(r, 20));   // 拉长在飞窗口
  return {enabled: true, accounts: [{index: 1, id: 'b@x.com', email: 'b@x.com'}]};
};
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
const p1 = agDeleteAccount('a@x.com');          // 开确认弹窗
await new Promise(r => setTimeout(r, 5));
const p2 = agDeleteAccount('a@x.com');   // 弹窗已开：第二个 ✕ 不叠层（resolve false）
globalThis.__acctDelYes();                      // 确认；resolve 是微任务，AG_MOVING
await new Promise(r => setTimeout(r, 5));       // 随后置位——等它落地再模拟下一次点击
const p3 = agDeleteAccount('a@x.com');   // POST 在飞，应被 AG_MOVING 挡掉
await Promise.all([p1, p2, p3]);
console.log(JSON.stringify({calls: CALLS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"].count("/ui/api/antigravity/accounts/delete") == 1, \
        f"在飞点按不应产生第二次删除请求: {data['calls']}"


def test_blacklist_subtitle_label_and_hours():
    """拉黑冷却的副标题标注「疑似拉黑」；2h 以上按小时显示（不是 360min）。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'AG #1 · Gemini 组', remaining: 900, total: 1000, percent: 10,
   used: 100, reset_ts: null, unit: 'permille'},
  {label: 'AG #2 · Gemini 组', remaining: 800, total: 1000, percent: 20,
   used: 200, reset_ts: null, unit: 'permille'},
]}}]};
globalThis.__RESPONSE = {enabled: true, accounts: [
  {index: 1, id: 'a@x.com', email: 'a@x.com', project_id: 'p', hours_left: 1,
   cooling: [{kind: 'blacklist', minutes_left: 359.5}]},
  {index: 2, id: 'b@x.com', email: 'b@x.com', project_id: 'p', hours_left: 1, cooling: []},
]};
// 快照已有旧值，走就地回填分支（空快照会触发整卡重渲，但那也一样要紧）
AG_ACCTS = [
  {index: 1, email: 'a@x.com', project_id: 'p', hours_left: 1, cooling: []},
  {index: 2, email: 'b@x.com', project_id: 'p', hours_left: 1, cooling: []},
];
await loadAntigravityAccounts();
const one = (PANEL.innerHTML.match(/data-ag-idx="1"[^]*?<div class="qbody"/) || [''])[0];
console.log(JSON.stringify({
  sub: (one.lastIndexOf('</span>') >= 0 ? one.slice(one.lastIndexOf('</span>') + 7) : '')}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "疑似拉黑" in data["sub"], data["sub"]
    assert "6.0h" in data["sub"], f"6h 档要按小时展示: {data['sub']}"
    assert "360min" not in data["sub"]


# ---------------------------------------------------------------------------
# 渠道标题「↻ 刷新」：一次重查本通道全部账号额度
# ---------------------------------------------------------------------------

def test_panel_renders_refresh_button_even_single_account():
    """渠道标题始终有 ↻；单账号同样可绕过额度缓存重查。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', checkin: {supported: false},
  quota: {supported: true, level: 'free', items: [
    {label: 'Gemini 组', remaining: 900, total: 1000, percent: 10,
     used: 100, reset_ts: null, unit: 'permille'}]}}]};
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "refreshProviderQuota('antigravity', this)" in data["html"], \
        f"卡上没有刷新按钮: {data['html'][:400]}"


def test_refresh_provider_quota_posts_and_force_reload():
    """旧服务没返回快照时 POST 后 force 重拉；在飞期间再点被忽略。"""
    out = _run_js("""
let POSTS = [], REFRESHES = 0, LOADS = [];
globalThis.loadData = async (keys, manual) => { LOADS.push({keys, manual}); };
globalThis.api = async (path, opts) => {
  if (path === '/ui/api/benefits/refresh') {
    REFRESHES++;
    if (REFRESHES === 1)   // 第一发挂起：模拟上游 8s 重查
      return new Promise(res => { globalThis.__REL = () => res({ok: true}); });
    POSTS.push(JSON.parse((opts || {}).body || '{}'));
    return {ok: true};
  }
  return globalThis.__RESPONSE;
};
const btn = {innerHTML: '↻', disabled: false, classList: {_s: new Set(),
  add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
  contains(c) { return this._s.has(c); }}};
const p1 = refreshProviderQuota('antigravity', btn);
await new Promise(r => setTimeout(r, 5));
const spinning = btn.classList.contains('spinning') && btn.disabled;
await refreshProviderQuota('antigravity', btn);   // 在飞：应立即返回、不发第二发
globalThis.__REL();
await p1;
await new Promise(r => setTimeout(r, 5));
console.log(JSON.stringify({
  refreshes: REFRESHES,
  spinning: spinning,
  restored: !btn.disabled,
  loads: LOADS,
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["refreshes"] == 1, f"在飞期间再点不应发第二发: {data}"
    assert data["spinning"], "等待期间按钮该转圈+禁用"
    assert data["restored"], "完成后按钮该恢复"
    assert data["loads"] == [{"keys": ["benefits"], "manual": True}], \
        f"旧响应缺 snapshot 时才 force 重拉 benefits: {data['loads']}"


def test_refresh_provider_quota_uses_returned_snapshot_directly():
    """POST 已返回完整 snapshot 时直接更新全局并重渲，不再补 GET。"""
    out = _run_js("""
let LOADS = [], RENDERS = 0;
globalThis.loadData = async (keys, manual) => { LOADS.push({keys, manual}); };
globalThis.renderBenefits = () => { RENDERS++; };
globalThis.api = async () => ({providers: [{id: 'fresh'}]});
await refreshProviderQuota('antigravity', null);
console.log(JSON.stringify({loads: LOADS, renders: RENDERS,
  provider: BENEFITS.providers[0].id}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data == {"loads": [], "renders": 1, "provider": "fresh"}


def test_refresh_provider_quota_lock_is_per_provider():
    """锁按 provider 分桶：同通道防连点，不同通道可以并行刷新。"""
    out = _run_js("""
let BODIES = [], RELEASE = {};
globalThis.api = async (path, opts) => {
  const pid = JSON.parse(opts.body).provider;
  BODIES.push(pid);
  return new Promise(res => { RELEASE[pid] = () => res({providers: []}); });
};
globalThis.renderBenefits = () => {};
const p1 = refreshProviderQuota('antigravity', null);
const duplicate = refreshProviderQuota('antigravity', null);
const p2 = refreshProviderQuota('kimi', null);
await new Promise(r => setTimeout(r, 5));
RELEASE.antigravity(); RELEASE.kimi();
await Promise.all([p1, duplicate, p2]);
console.log(JSON.stringify(BODIES));
""")
    assert json.loads(out.strip().splitlines()[-1]) == ["antigravity", "kimi"]


def test_parallel_refresh_does_not_apply_late_stale_snapshot():
    """不同通道并行刷新时，先发后到的旧快照不能覆盖后发请求的新结果。"""
    out = _run_js("""
let RELEASE = {}, RENDERS = [];
globalThis.api = async (path, opts) => {
  const pid = JSON.parse(opts.body).provider;
  return new Promise(res => { RELEASE[pid] = res; });
};
globalThis.renderBenefits = () => { RENDERS.push(BENEFITS.providers[0].id); };
const older = refreshProviderQuota('antigravity', null);
const newer = refreshProviderQuota('kimi', null);
await new Promise(r => setTimeout(r, 5));
RELEASE.kimi({providers: [{id: 'newer'}]});
await newer;
RELEASE.antigravity({providers: [{id: 'older-stale'}]});
await older;
console.log(JSON.stringify({renders: RENDERS, provider: BENEFITS.providers[0].id}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data == {"renders": ["newer"], "provider": "newer"}


def test_refresh_provider_quota_failure_keeps_old_snapshot():
    """POST 失败保留旧 BENEFITS，并恢复按钮状态。"""
    out = _run_js("""
BENEFITS = {providers: [{id: 'old'}]};
let TOASTS = [];
globalThis.toast = (m, bad) => TOASTS.push([m, bad]);
globalThis.api = async () => { throw new Error('boom'); };
const btn = {innerHTML: '↻', disabled: false, classList: {
  add() {}, remove() {}, contains() { return false; }}};
await refreshProviderQuota('antigravity', btn);
console.log(JSON.stringify({provider: BENEFITS.providers[0].id,
  toast: TOASTS[0], restored: !btn.disabled && btn.innerHTML === '↻'}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["provider"] == "old"
    assert data["toast"] == ["刷新失败: boom", True]
    assert data["restored"]
