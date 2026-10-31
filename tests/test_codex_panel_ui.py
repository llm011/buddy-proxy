"""Codex 面板前端测试（node 桩真跑 benefits_checkin.js 的 CODEX 段）。

与 test_kimi_panel_ui.py 同构（codex 面板照 kimi 写，组件全部复用）：
把整段面板代码抽出来用最小 DOM 桩真跑。盯六件事：

- **分组渲染**：``Codex #N · `` 前缀按账号分组，标题=账号名（快照回填），
  ▲▼ 顺位 + ✎ 改名 + ✕ 删除按钮带上账号 id
- **副标题**：冷却 + ChatGPT 套餐档位（`套餐 plus`），没有「token 剩 Xh」
- **未登录可见**：quota 只有静态说明条也要渲染整卡——「导入账号」入口常在
- **通道未注册**：providers 里没有 codex（没加 --codex）时整块不渲染
- **顺位/删除**：按快照里 id 的真实位次提交全量 id 列表；删除走确认弹窗
- **导入弹窗**：openCodexImport 填 textarea → codexImportSubmit POST payload，
  文案要说明「无需安装 Codex CLI」

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
# CODEX 面板段在 benefits_checkin.js 末尾（KIMI 段之后）；组件复用 KIMI 段与
# ANTIGRAVITY 段（confirmAccountDelete / ACCT_CONFIRM_OPEN），三段拼接执行。
BENEFITS_JS_AG = _STATIC / "benefits_antigravity.js"
BENEFITS_JS_KIMI = _STATIC / "benefits_kimi.js"
BENEFITS_JS_CODEX = _STATIC / "benefits_codex.js"
HELPERS_JS = _STATIC / "benefits_accounts.js"
SEG_AG = re.compile(r"// ---- ANTIGRAVITY 面板.*$", re.S)
SEG_KIMI = re.compile(r"// ---- KIMI 面板.*$", re.S)
SEG_CODEX = re.compile(r"// ---- CODEX 面板.*$", re.S)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑面板 JS（CI 有，本机可选）"
)

_STUB = """

let PANEL = {innerHTML: ''};        // #codex-panel
globalThis.esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
globalThis.quotaItemHtml = it => '<div class="qitem">' + esc(it.label) +
  (typeof it.note === 'string' ? '<div class="muted">' + esc(it.note) + '</div>' : '') +
  '</div>';
globalThis.quotaItemsHtml = (items, key) =>
  '<div class="qbody" data-qfold="' + esc(key) + '">' +
  items.map(globalThis.quotaItemHtml).join('') + '</div>';
globalThis.BENEFITS = {};
globalThis.__SYNC_CALLS = 0;
globalThis.syncQuotaFold = () => { globalThis.__SYNC_CALLS++; };
// 删除确认弹窗 / 导入弹窗用到的 overlay/modal 骨架——桩成可观察的
const MODAL = {};
globalThis.MODAL = MODAL;
globalThis.__FOOT = '';
globalThis.__CLOSED = 0;
globalThis.setModalFoot = html => { globalThis.__FOOT = html; };
globalThis.closeModal = () => { globalThis.__CLOSED++; };
const TEXTAREA = {value: '', focus() {}};       // #codex-import-text（openCodexImport 后赋值）
globalThis.TEXTAREA = TEXTAREA;
globalThis.document = {
  querySelector: sel => {
    if (sel === '#codex-panel .pat-pkg-name') return PANEL.__names ? PANEL.__names[0] : null;
    const m = sel.match(new RegExp('^#codex-panel [data-codex-idx="(\\d+)"\\]$'));
    if (m && PANEL.__names) return PANEL.__names[Number(m[1]) - 1] || null;
    return null;
  },
  querySelectorAll: sel => (sel === '#modal-foot button' ? [] : []),
  getElementById: id => {
    if (id === 'codex-panel') return PANEL;
    if (id === 'modal-title' || id === 'modal-body' || id === 'overlay') {
      if (!MODAL[id]) MODAL[id] = {textContent: '', innerHTML: '', className: '',
        classList: {_s: new Set(),
                    add(c) { this._s.add(c); },
                    remove(c) { this._s.delete(c); },
                    contains(c) { return this._s.has(c); }}};
      return MODAL[id];
    }
    if (id === 'codex-import-text') return TEXTAREA;
    return null;
  },
};
"""


def _run_js(body: str) -> str:
    text_ag = BENEFITS_JS_AG.read_text(encoding="utf-8")
    text_kimi = BENEFITS_JS_KIMI.read_text(encoding="utf-8")
    text_codex = BENEFITS_JS_CODEX.read_text(encoding="utf-8")
    ag = SEG_AG.search(text_ag)
    kimi = SEG_KIMI.search(text_kimi)
    codex = SEG_CODEX.search(text_codex)
    assert ag and kimi and codex, "找不到 ANTIGRAVITY/KIMI/CODEX 面板段（边界注释被改了？）"
    script = (
        _STUB
        + HELPERS_JS.read_text(encoding="utf-8")
        + ag.group(0) + "\n" + kimi.group(0) + "\n" + codex.group(0)
        + "\n(async () => {\n" + body
        + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def _provider(items):
    return {"id": "codex", "name": "Codex (ChatGPT)", "quota": {"supported": True, "items": items}}


def _item(n, who, note):
    """codex 额度条目：真实形态——percent 有值（进账号分组）、总量/已用留 None。"""
    return {"label": f"Codex #{n} · {who}", "used": None, "total": None,
            "remaining": None, "percent": 0, "unit": "percent",
            "reset_ts": None, "expire_ts": None, "note": note}


def test_codex_panel_renders_groups_and_buttons():
    """分组渲染：标题=账号名（快照回填）、▲▼ 顺位、✎ 改名、✕ 删除、导入按钮。"""
    items = [
        _item(1, "a@x.y", "plus 套餐 · 额度随请求实时扣减"),
        _item(2, "b@x.y", "pro 套餐 · 额度随请求实时扣减"),
    ]
    out = _run_js("""
globalThis.BENEFITS = {providers: [%s]};
let API_CALLS = [];
globalThis.api = async (path) => {
  API_CALLS.push(path);
  return {enabled: true, accounts: [
    {id: 'u1', name: 'a@x.y', alias: '主力号', index: 1, plan_type: 'plus', cooling: []},
    {id: 'u2', name: 'b@x.y', index: 2, plan_type: 'pro',
     cooling: [{kind: 'quota', minutes_left: 4.2}]},
  ]};
};
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
renderCodexPanel();
await new Promise(r => setTimeout(r, 5));
const html = PANEL.innerHTML;
console.log(JSON.stringify({
  head: html.includes('Codex'),
  tag: html.includes('多账号 · 自动切换') || html.includes('ChatGPT 订阅'),
  name1: html.includes('主力号'), name2: html.includes('b@x.y'),
  noTokenLeft: !html.includes('token 剩'),
  note2: html.includes('pro 套餐'),
  sub2: html.includes('额度冷却 4.2min'),
  moves: (html.split("acctMove\\('codex'").length - 1),
  ren1: html.includes("acctRenamePrompt('codex','u1')"),
  del1: html.includes("acctDelete('codex','u1')"),
  importBtn: html.includes('openCodexImport()'),
  accountsApi: API_CALLS,
  html,
}));
""" % json.dumps(_provider(items)))
    data = json.loads(out.strip().splitlines()[-1])
    assert data["head"] and data["tag"]
    assert data["name1"] and data["name2"], "标题=别名/账号名（快照回填），不是 Codex #N 代号"
    assert data["noTokenLeft"]
    assert data["note2"], "套餐档位写在条目 note 里（qitem 正文小字）"
    assert data["sub2"], "副标题=冷却"
    assert "%" not in data["html"], "没有总量时不出现任何百分比（不编假数字）"
    assert data["moves"] == 4, "两个账号各一对 ▲▼"
    assert data["ren1"] and data["del1"], "✎/✕ 带账号 id"
    assert data["importBtn"], "「导入账号」按钮常在"
    # 首渲拉一次；首份快照到位后整卡重渲又拉一次（数据已同走早退，不会循环）
    assert data["accountsApi"] == ["/ui/api/codex/accounts"] * 2


def test_codex_panel_visible_when_not_logged_in():
    """未登录：quota 只有静态说明条也要渲染整卡（导入入口不能消失）。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [%s]};
globalThis.api = async () => ({enabled: false, accounts: []});
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
renderCodexPanel();
const html = PANEL.innerHTML;
console.log(JSON.stringify({
  head: html.includes('Codex'),
  notice: html.includes('buddy login codex'),
  importBtn: html.includes('openCodexImport()'),
  noGroups: !html.includes("acctDelete('codex'"),
}));
""" % json.dumps(_provider([{
    "label": "Codex 额度",
    "note": "未登录：先跑 `buddy login codex`（无需安装 Codex CLI）",
    # 未登录走 notice_item：剩余是说明文案、没有百分比
    "used": None, "total": None, "remaining": "未登录", "percent": None,
    "reset_ts": None, "expire_ts": None, "unit": None}])))
    data = json.loads(out.strip().splitlines()[-1])
    assert data["head"] and data["notice"], "说明条横贯展示"
    assert data["importBtn"], "未登录也要有「导入账号」"
    assert data["noGroups"], "没有账号就没有 ▲▼/✕"


def test_codex_panel_hidden_when_channel_absent():
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'trae', quota: {supported: true, items: []}}]};
globalThis.api = async () => { throw new Error('不该调接口'); };
renderCodexPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["html"] == ""


def test_codex_move_account_posts_full_ids():
    """顺位：按快照里 id 的真实位次提交全量 id 列表。"""
    out = _run_js("""
CODEX_ACCTS = [
  {id: 'u1', name: '一号', index: 1, cooling: []},
  {id: 'u2', name: '二号', index: 2, cooling: []},
];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  return {enabled: true, accounts: [
    {id: 'u2', name: '二号', index: 1, cooling: []},
    {id: 'u1', name: '一号', index: 2, cooling: []}]};
};
let TOASTS = [], REFRESHED = 0;
globalThis.toast = m => TOASTS.push(m);
globalThis.refreshAll = () => REFRESHED++;
await codexMoveAccount(2, -1);
console.log(JSON.stringify({calls: CALLS, toasts: TOASTS, refreshed: REFRESHED}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"] == [{"path": "/ui/api/codex/accounts/order",
                              "body": {"ids": ["u2", "u1"]}}]
    assert any("二号" in t and "#1" in t for t in data["toasts"])
    assert data["refreshed"] == 1


def test_codex_move_uses_snapshot_position_not_stale_idx():
    """面板重绘滞后：按钮上的 idx 过期，按 id 在快照里的真实位次挪。"""
    out = _run_js("""
CODEX_ACCTS = [
  {id: 'u1', name: '一号', index: 1, cooling: []},
  {id: 'u2', name: '二号', index: 2, cooling: []},
];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  return {enabled: true, accounts: CODEX_ACCTS};
};
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
await codexMoveAccount(99, -1, 'u2');   // 按钮 idx 过期，但 id=u2 真实位次是 2
console.log(JSON.stringify({calls: CALLS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"][0]["body"] == {"ids": ["u2", "u1"]}


def test_codex_delete_account_modal_flow():
    """删除：确认弹窗 → 确认 → POST /delete → 快照/toast/刷新，锁释放。"""
    out = _run_js("""
CODEX_ACCTS = [{id: 'u1', name: '一号', index: 1, cooling: []}];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  return {enabled: true, accounts: []};
};
let TOASTS = [], REFRESHED = 0;
globalThis.toast = m => TOASTS.push(m);
globalThis.refreshAll = () => REFRESHED++;
const p = codexDeleteAccount('u1');
await new Promise(r => setTimeout(r, 5));
const shown = {
  title: globalThis.MODAL['modal-title'].textContent,
  body: globalThis.MODAL['modal-body'].innerHTML,
  foot: globalThis.__FOOT,
};
globalThis.__acctDelYes();
await p;
console.log(JSON.stringify({shown, calls: CALLS, toasts: TOASTS, refreshed: REFRESHED,
  closed: globalThis.__CLOSED, confirmFree: !ACCT_CONFIRM_OPEN}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["shown"]["title"] == "删除 Codex 账号"
    assert "一号" in data["shown"]["body"]
    assert 'class="danger"' in data["shown"]["foot"]
    assert data["calls"] == [{"path": "/ui/api/codex/accounts/delete", "body": {"id": "u1"}}]
    assert data["refreshed"] == 1
    assert any("一号" in t for t in data["toasts"])
    assert data["closed"] >= 1 and data["confirmFree"]


def test_codex_import_modal_and_submit():
    """导入：弹窗文案说明无需 CLI → 粘贴 → POST payload → 快照/toast/关闭。"""
    out = _run_js("""
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  return {enabled: true, accounts: [{id: 'n1', name: '新号', index: 1, cooling: []}]};
};
let TOASTS = [];
globalThis.toast = (m, bad) => TOASTS.push({m, bad});
globalThis.refreshAll = () => {};
openCodexImport();
const body = globalThis.MODAL['modal-body'].innerHTML;
TEXTAREA.value = '{"access_token":"at","refresh_token":"rt"}';
await codexImportSubmit();
console.log(JSON.stringify({body, calls: CALLS, toasts: TOASTS,
  closed: globalThis.__CLOSED, accts: CODEX_ACCTS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "不需要" in data["body"] and "Codex CLI" in data["body"], "文案说明不装 CLI 也能登"
    assert data["calls"] == [{"path": "/ui/api/codex/accounts/import",
                              "body": {"payload": '{"access_token":"at","refresh_token":"rt"}'}}]
    assert any("已导入" in t["m"] for t in data["toasts"])
    assert data["closed"] >= 1


def test_codex_import_empty_shows_error():
    out = _run_js("""
globalThis.api = async () => { throw new Error('空内容不该发请求'); };
let TOASTS = [];
globalThis.toast = (m, bad) => TOASTS.push({m, bad});
globalThis.refreshAll = () => {};
openCodexImport();
TEXTAREA.value = '   ';
await codexImportSubmit();
console.log(JSON.stringify({toasts: TOASTS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["toasts"] and data["toasts"][0]["bad"] is True


def test_codex_rename_modal_flow():
    out = _run_js("""
CODEX_ACCTS = [{id: 'u1', name: '一号', alias: '旧名', index: 1, cooling: []}];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  return {enabled: true, accounts: [{id: 'u1', name: '一号', alias: '新名', index: 1, cooling: []}]};
};
let TOASTS = [], RENDERS = 0;
globalThis.toast = m => TOASTS.push(m);
globalThis.refreshAll = () => {};
globalThis.renderCodexPanel = () => { RENDERS++; };
acctRenamePrompt('codex', 'u1');
const INPUT = {value: '新名'};
document.getElementById = id => {
  if (id === 'acct-rename-input') return INPUT;
  if (globalThis.MODAL[id]) return globalThis.MODAL[id];
  if (id === 'codex-panel') return PANEL;
  return null;
};
await acctRenameSubmit('codex', 'u1');
console.log(JSON.stringify({calls: CALLS, alias: CODEX_ACCTS[0].alias, renders: RENDERS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"] == [{"path": "/ui/api/codex/accounts/rename",
                              "body": {"id": "u1", "alias": "新名"}}]
    assert data["alias"] == "新名"
    assert data["renders"] >= 1


def _reset_item(who, available, applicable=None):
    """重置券条目：真实形态——独立标记行，无 percent/used，只靠 who 归属。"""
    counts = {"available": available}
    if applicable is not None:
        counts["applicable"] = applicable
    return {"label": "重置券", "unit": "reset_credit", "reset_credit": True,
            "reset_credits": counts, "used": None, "total": None,
            "remaining": None, "percent": None, "who": who}


_ACCTS_TWO = """[{id: 'u1', name: 'a@x.y', alias: '主力号', index: 1, plan_type: 'plus', cooling: []},
  {id: 'u2', name: 'b@x.y', index: 2, plan_type: 'pro', cooling: []}]"""


def test_codex_reset_credits_rendered_inside_each_account_card():
    """券行落在各自账号卡片内：有 applicable 才显示「当前可用」，不进横贯 notice 区。"""
    items = [
        _item(1, "a@x.y", "plus 套餐"),
        _item(2, "b@x.y", "pro 套餐"),
        _reset_item("Codex #1 · a@x.y", 3, 0),
        _reset_item("Codex #2 · b@x.y", 1),
    ]
    out = _run_js("""
globalThis.BENEFITS = {providers: [%s]};
CODEX_ACCTS = %s;
globalThis.api = async () => ({enabled: true, accounts: CODEX_ACCTS});
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
renderCodexPanel();
const html = PANEL.innerHTML;
const cards = html.split('pat-pkg-name').slice(1);
const seg = (n) => {
  const part = cards[n];
  const end = part.indexOf('</div>', part.indexOf('<span class="ag-move"'));
  return part.slice(0, end > 0 ? end : part.length);
};
console.log(JSON.stringify({
  card1: seg(0), card2: seg(1), html,
  lines: html.split('data-reset-credit').length - 1,
}));
""" % (json.dumps(_provider(items)), _ACCTS_TWO))
    data = json.loads(out.strip().splitlines()[-1])
    assert data["lines"] == 2, "两个账号各一条券行（不合并、不重复）"
    assert "重置券 3 张 · 当前可用 0 张" in data["card1"]
    assert "重置券 1 张" in data["card2"]
    assert "当前可用" not in data["card2"], "没有 applicable 时不拼「当前可用」"
    # 不在横贯 notice 区：券行只出现在 pat-pkg 卡片里
    head, _, cards_html = data["html"].partition('pat-quota-grid')
    assert "重置券" not in head, "券行不能被当成 notice 横贯全宽"


def test_codex_reset_credits_only_account_still_renders_card():
    """只有券没有窗口（额度耗尽/上游没给窗口）的账号也要出带账号名的卡。"""
    items = [_item(1, "a@x.y", "plus 套餐"), _reset_item("Codex #2 · b@x.y", 2, 1)]
    out = _run_js("""
globalThis.BENEFITS = {providers: [%s]};
CODEX_ACCTS = %s;
globalThis.api = async () => ({enabled: true, accounts: CODEX_ACCTS});
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
renderCodexPanel();
const html = PANEL.innerHTML;
console.log(JSON.stringify({
  grid: html.includes('pat-quota-grid'),
  card2: html.includes('data-codex-idx="2"'),
  line: html.includes('重置券 2 张 · 当前可用 1 张'),
  del2: html.includes("acctDelete('codex','u2')"),
  empty: html.includes('暂无账号额度数据'),
}));
""" % (json.dumps(_provider(items)), _ACCTS_TWO))
    data = json.loads(out.strip().splitlines()[-1])
    assert data["grid"] and data["card2"], "只有券的账号仍出自己的卡片"
    assert data["line"] and data["del2"]
    assert not data["empty"]


@pytest.mark.parametrize("bad", [None, "3", 1.5, -1, True])
def test_codex_reset_credits_invalid_available_not_rendered(bad):
    """available 非法/缺失 → 整行不渲染（不编数字），也不退化成空 notice。"""
    item = _reset_item("Codex #1 · a@x.y", 3, 0)
    if bad is None:
        item["reset_credits"] = {}
    else:
        item["reset_credits"] = {"available": bad, "applicable": 0}
    items = [_item(1, "a@x.y", "plus 套餐"), item]
    out = _run_js("""
globalThis.BENEFITS = {providers: [%s]};
CODEX_ACCTS = %s;
globalThis.api = async () => ({enabled: true, accounts: CODEX_ACCTS});
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
renderCodexPanel();
const html = PANEL.innerHTML;
const grid = html.includes('pat-quota-grid');
const outside = html.split('pat-quota-grid')[0];
console.log(JSON.stringify({line: html.includes('重置券'),
                            notice: outside.includes('class="qitem"')}));
""" % (json.dumps(_provider(items)), _ACCTS_TWO))
    data = json.loads(out.strip().splitlines()[-1])
    assert not data["line"]
    assert not data["notice"], "非法券行不能落到横贯 notice 区"


def test_codex_reset_credits_without_who_not_rendered():
    """缺 who 的券行直接丢弃：错挂到别的账号比少显示一条更糟。"""
    items = [_item(1, "a@x.y", "plus 套餐"), _reset_item("", 3, 0)]
    out = _run_js("""
globalThis.BENEFITS = {providers: [%s]};
CODEX_ACCTS = %s;
globalThis.api = async () => ({enabled: true, accounts: CODEX_ACCTS});
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
renderCodexPanel();
const html = PANEL.innerHTML;
console.log(JSON.stringify({line: html.includes('重置券'),
                            cards: html.split('pat-pkg-name').length - 1}));
""" % (json.dumps(_provider(items)), _ACCTS_TWO))
    data = json.loads(out.strip().splitlines()[-1])
    assert not data["line"]
    assert data["cards"] == 1, "不能为无归属券行凭空多出一张卡"


def test_codex_reset_credits_follow_name_when_snapshot_reordered():
    """快照顺位变了：券按名字并进自己的卡片，不按旧 `Codex #N` 挂到别人卡上。"""
    items = [
        _item(1, "a@x.y", "plus 套餐"),
        _item(2, "b@x.y", "pro 套餐"),
        _reset_item("Codex #2 · b@x.y", 4, 1),
    ]
    # 快照里 b 已经被移到第一位，a 成了第二位
    accts = """[{id: 'u1', name: 'a@x.y', index: 2, plan_type: 'plus', cooling: []},
  {id: 'u2', name: 'b@x.y', index: 1, plan_type: 'pro', cooling: []}]"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [%s]};
CODEX_ACCTS = %s;
globalThis.api = async () => ({enabled: true, accounts: CODEX_ACCTS});
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
renderCodexPanel();
const html = PANEL.innerHTML;
const cards = html.split('pat-pkg-name').slice(1);
console.log(JSON.stringify({cards, line: html.includes('重置券 4 张 · 当前可用 1 张')}));
""" % (json.dumps(_provider(items)), accts))
    data = json.loads(out.strip().splitlines()[-1])
    assert data["line"]
    with_line = [c for c in data["cards"] if "重置券" in c]
    assert len(with_line) == 1, "券只挂一张卡"
    assert "data-codex-idx=\"2\"" in with_line[0], "券跟着 b@x.y（快照位次 2）走"
