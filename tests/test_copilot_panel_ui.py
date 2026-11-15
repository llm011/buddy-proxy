"""copilot 前端面板测试（node 桩真跑 benefits_copilot.js）。

与 test_codex_panel_ui.py 同构（copilot 面板照 codex 写，组件全部复用）：
把面板段抽出来用最小 DOM 桩真跑。盯这几件事：

- **分组渲染**：``Copilot #N · `` 前缀按账号分组，标题=账号名（快照回填），
  ▲▼ 顺位 + ✎ 改名 + ✕ 删除带账号 id
- **副标题**：冷却两档 + 凭据来源；未登录（无窗口）也要整卡渲染，导入入口常在
- **通道未注册**：providers 里没有 copilot（没加 --copilot）时整块不渲染
- **顺位/删除**：按快照 id 真实位次提交全量 id 列表；删除走确认弹窗
- **导入**：粘贴 token → POST /import；「从 gh 导入」→ GET /gh-accounts 出按钮、
  POST /import-gh；部分失败把原因留在弹窗里
- **刷新模型**：调 /models/refresh 并提示条数/错误

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
BENEFITS_JS_COPILOT = _STATIC / "benefits_copilot.js"
HELPERS_JS = _STATIC / "benefits_accounts.js"
SEG_COPILOT = re.compile(r"// ---- COPILOT 面板.*$", re.S)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑面板 JS（CI 有，本机可选）"
)

_STUB = """

let PANEL = {innerHTML: ''};        // #copilot-panel
globalThis.esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
globalThis.quotaItemHtml = it => '<div class="qitem">' + esc(it.label) +
  (typeof it.note === 'string' ? '<div class="muted">' + esc(it.note) + '</div>' : '') +
  '</div>';
globalThis.quotaItemsHtml = (items, key) =>
  '<div class="qbody" data-qfold="' + esc(key) + '">' +
  items.map(globalThis.quotaItemHtml).join('') + '</div>';
globalThis.quotaProviderFold = () => ({badge: '', toggle: '', bodyStart: '', bodyEnd: ''});
globalThis.providerLoadBalanceToggle = () => '';
globalThis.providerRefreshButton = () => '<button>R</button>';
globalThis.BENEFITS = {};
globalThis.__SYNC_CALLS = 0;
globalThis.syncQuotaFold = () => { globalThis.__SYNC_CALLS++; };
const MODAL = {};
globalThis.MODAL = MODAL;
globalThis.__FOOT = '';
globalThis.__CLOSED = 0;
globalThis.setModalFoot = html => { globalThis.__FOOT = html; };
globalThis.closeModal = () => { globalThis.__CLOSED++; };
const TEXTAREA = {value: '', focus() {}};       // #copilot-import-text
globalThis.TEXTAREA = TEXTAREA;
const GH_LIST = {innerHTML: ''};                // #copilot-gh-list
globalThis.GH_LIST = GH_LIST;
globalThis.document = {
  querySelector: sel => {
    if (sel === '#copilot-panel .pat-pkg-name') return PANEL.__names ? PANEL.__names[0] : null;
    const m = sel.match(new RegExp('^#copilot-panel [data-copilot-idx="(\\\\d+)"\\\\]$'));
    if (m && PANEL.__names) return PANEL.__names[Number(m[1]) - 1] || null;
    return null;
  },
  querySelectorAll: sel => [],
  getElementById: id => {
    if (id === 'copilot-panel') return PANEL;
    if (id === 'modal-title' || id === 'modal-body' || id === 'overlay') {
      if (!MODAL[id]) MODAL[id] = {textContent: '', innerHTML: '', className: '',
        classList: {_s: new Set(),
                    add(c) { this._s.add(c); },
                    remove(c) { this._s.delete(c); },
                    contains(c) { return this._s.has(c); }}};
      return MODAL[id];
    }
    if (id === 'copilot-import-text') return TEXTAREA;
    if (id === 'copilot-gh-list') return GH_LIST;
    return null;
  },
};
"""


def _run_js(body: str) -> str:
    text = BENEFITS_JS_COPILOT.read_text(encoding="utf-8")
    seg = SEG_COPILOT.search(text)
    assert seg, "找不到 COPILOT 面板段（边界注释被改了？）"
    script = (
        _STUB
        + HELPERS_JS.read_text(encoding="utf-8")
        + seg.group(0)
        + "\n(async () => {\n" + body
        + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def _provider(items):
    return {"id": "copilot", "name": "GitHub Copilot",
            "quota": {"supported": True, "items": items}}


def _item(n, note=""):
    """真实形态：``Copilot #N · Premium 交互``，unit=次，带 remaining/total。"""
    it = {"label": f"Copilot #{n} · Premium 交互", "used": 10, "total": 300,
          "remaining": 290, "percent": 3.3, "unit": "次",
          "reset_ts": None, "expire_ts": None}
    if note:
        it["note"] = note
    return it


def test_copilot_panel_renders_groups_and_buttons():
    items = [_item(1), _item(2)]
    out = _run_js("""
globalThis.BENEFITS = {providers: [%s]};
let API_CALLS = [];
globalThis.api = async (path) => {
  API_CALLS.push(path);
  return {enabled: true, accounts: [
    {id: 'jsongo', name: 'jsongo', alias: '主力号', index: 1, source: 'gh:jsongo', token: 'ok', cooling: []},
    {id: 'other', name: 'other', index: 2, source: 'panel', token: 'ok',
     cooling: [{kind: 'quota', minutes_left: 4.2}]},
  ]};
};
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
renderCopilotPanel();
await new Promise(r => setTimeout(r, 5));
const html = PANEL.innerHTML;
console.log(JSON.stringify({
  head: html.includes('Copilot'),
  multi: html.includes('多账号 · 自动切换'),
  name1: html.includes('主力号'), name2: html.includes('>other<'),
  sub2: html.includes('额度冷却 4.2min') && html.includes('来源 panel'),
  moves: (html.split("acctMove\\('copilot'").length - 1),
  ren1: html.includes("acctRenamePrompt('copilot','jsongo')"),
  del1: html.includes("acctDelete('copilot','jsongo')"),
  importBtn: html.includes('openCopilotImport()'),
  refreshModels: html.includes('copilotRefreshModels(this)'),
  accountsApi: API_CALLS,
}));
""" % json.dumps(_provider(items)))
    data = json.loads(out.strip().splitlines()[-1])
    assert data["head"] and data["multi"]
    assert data["name1"] and data["name2"], "标题=别名/账号名（快照回填）"
    assert data["sub2"], "副标题=冷却 + 凭据来源"
    assert data["moves"] == 4, "两个账号各一对 ▲▼"
    assert data["ren1"] and data["del1"], "✎/✕ 带账号 id"
    assert data["importBtn"] and data["refreshModels"]
    assert data["accountsApi"] == ["/ui/api/copilot/accounts"] * 2


def test_copilot_panel_visible_when_not_logged_in():
    out = _run_js("""
globalThis.BENEFITS = {providers: [%s]};
globalThis.api = async () => ({enabled: false, accounts: []});
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
renderCopilotPanel();
const html = PANEL.innerHTML;
console.log(JSON.stringify({
  head: html.includes('Copilot'),
  notice: html.includes('buddy login copilot'),
  importBtn: html.includes('openCopilotImport()'),
  noGroups: !html.includes("acctDelete('copilot'"),
}));
""" % json.dumps(_provider([{
    "label": "Copilot 额度",
    "note": "未登录：先 `buddy login copilot`",
    "used": None, "total": None, "remaining": "未登录", "percent": None,
    "reset_ts": None, "expire_ts": None, "unit": None}])))
    data = json.loads(out.strip().splitlines()[-1])
    assert data["head"] and data["notice"]
    assert data["importBtn"], "未登录也要有「导入账号」"
    assert data["noGroups"]


def test_copilot_panel_hidden_when_channel_absent():
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'trae', quota: {supported: true, items: []}}]};
globalThis.api = async () => { throw new Error('不该调接口'); };
renderCopilotPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["html"] == ""


def test_copilot_move_account_posts_full_ids():
    out = _run_js("""
COPILOT_ACCTS = [
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
await copilotMoveAccount(2, -1);
// 面板重绘滞后：按钮 idx 过期，按 id 在快照里的真实位次挪
COPILOT_ACCTS = [{id: 'u1', name: '一号', index: 1, cooling: []},
                 {id: 'u2', name: '二号', index: 2, cooling: []}];
await copilotMoveAccount(99, -1, 'u2');
console.log(JSON.stringify({calls: CALLS, toasts: TOASTS, refreshed: REFRESHED}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"][0] == {"path": "/ui/api/copilot/accounts/order",
                                "body": {"ids": ["u2", "u1"]}}
    assert data["calls"][1]["body"] == {"ids": ["u2", "u1"]}
    assert any("二号" in t and "#1" in t for t in data["toasts"])
    assert data["refreshed"] == 2


def test_copilot_delete_account_modal_flow():
    out = _run_js("""
COPILOT_ACCTS = [{id: 'u1', name: '一号', index: 1, cooling: []}];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  return {enabled: true, accounts: []};
};
let TOASTS = [], REFRESHED = 0;
globalThis.toast = m => TOASTS.push(m);
globalThis.refreshAll = () => REFRESHED++;
const p = copilotDeleteAccount('u1');
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
    assert data["shown"]["title"] == "删除 Copilot 账号"
    assert "一号" in data["shown"]["body"]
    assert 'class="danger"' in data["shown"]["foot"]
    assert data["calls"] == [{"path": "/ui/api/copilot/accounts/delete", "body": {"id": "u1"}}]
    assert data["refreshed"] == 1
    assert any("一号" in t for t in data["toasts"])
    assert data["closed"] >= 1 and data["confirmFree"]


def test_copilot_import_modal_and_submit():
    """导入：弹窗说明 token 来源 → 粘贴 → POST payload → 清空输入框/关闭。"""
    out = _run_js("""
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  return {enabled: true, accounts: [{id: 'n1', name: '新号', index: 1, cooling: []}]};
};
let TOASTS = [];
globalThis.toast = (m, bad) => TOASTS.push({m, bad});
globalThis.refreshAll = () => {};
openCopilotImport();
const body = globalThis.MODAL['modal-body'].innerHTML;
await new Promise(r => setTimeout(r, 5));
CALLS = [];                   // 丢掉弹窗自己那发 gh-accounts
TEXTAREA.value = 'gho_abcdefghij';
await copilotImportSubmit();
console.log(JSON.stringify({body, calls: CALLS, toasts: TOASTS, closed: globalThis.__CLOSED,
  accts: COPILOT_ACCTS, cleared: TEXTAREA.value}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "gho_" in data["body"] and "buddy login copilot" in data["body"]
    assert data["calls"][0] == {"path": "/ui/api/copilot/accounts/import",
                                "body": {"token": "gho_abcdefghij"}}
    assert any("已导入" in t["m"] for t in data["toasts"])
    assert data["closed"] >= 1
    assert data["cleared"] == "", "token 不能留在页面里"


def test_copilot_import_empty_shows_error():
    out = _run_js("""
globalThis.api = async () => { throw new Error('空内容不该发请求'); };
let TOASTS = [];
globalThis.toast = (m, bad) => TOASTS.push({m, bad});
globalThis.refreshAll = () => {};
openCopilotImport();
await new Promise(r => setTimeout(r, 5));
TEXTAREA.value = '   ';
await copilotImportSubmit();
console.log(JSON.stringify({toasts: TOASTS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["toasts"] and data["toasts"][0]["bad"] is True


def test_copilot_import_gh_lists_accounts_and_posts():
    out = _run_js("""
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  if (path === '/ui/api/copilot/gh-accounts') return {accounts: ['jsongo', 'work']};
  return {enabled: true, accounts: [{id: 'jsongo', name: 'jsongo', index: 1, cooling: []}]};
};
let TOASTS = [];
globalThis.toast = (m, bad) => TOASTS.push({m, bad});
globalThis.refreshAll = () => {};
openCopilotImport();
await new Promise(r => setTimeout(r, 10));
const list = GH_LIST.innerHTML;
await copilotImportGh('jsongo');
console.log(JSON.stringify({list, calls: CALLS, toasts: TOASTS, closed: globalThis.__CLOSED}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "jsongo" in data["list"] and "全部导入" in data["list"]
    assert data["calls"][0] == {"path": "/ui/api/copilot/gh-accounts", "body": None}
    assert data["calls"][1] == {"path": "/ui/api/copilot/accounts/import-gh",
                                "body": {"user": "jsongo"}}
    assert any("已从 gh 导入" in t["m"] for t in data["toasts"])
    assert data["closed"] >= 1


def test_copilot_import_gh_partial_failure_keeps_modal():
    out = _run_js("""
globalThis.api = async (path) => {
  if (path === '/ui/api/copilot/gh-accounts') return {accounts: ['a', 'b']};
  return {enabled: true, accounts: [], errors: ['b: Copilot 目录接口拒绝了该 token']};
};
let TOASTS = [];
globalThis.toast = (m, bad) => TOASTS.push({m, bad});
globalThis.refreshAll = () => {};
openCopilotImport();
await new Promise(r => setTimeout(r, 10));
await copilotImportGh(null);
console.log(JSON.stringify({list: GH_LIST.innerHTML, toasts: TOASTS,
  closed: globalThis.__CLOSED}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "目录接口拒绝" in data["list"], "失败原因留在弹窗里（关掉就看不到了）"
    assert any(t["bad"] for t in data["toasts"])
    assert data["closed"] == 0


def test_copilot_refresh_models_toasts():
    out = _run_js("""
let CALLS = [];
globalThis.api = async (path) => {
  CALLS.push(path);
  return {ok: true, count: 12, models: [], error: null};
};
let TOASTS = [];
globalThis.toast = (m, bad) => TOASTS.push({m, bad});
const btn = {disabled: false, textContent: '刷新模型'};
await copilotRefreshModels(btn);
console.log(JSON.stringify({calls: CALLS, toasts: TOASTS, disabled: btn.disabled}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"] == ["/ui/api/copilot/models/refresh"]
    assert any("12" in t["m"] for t in data["toasts"])
    assert data["disabled"] is False
