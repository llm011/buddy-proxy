"""Kimi 面板前端测试（node 桩真跑 benefits.js 的 ANTIGRAVITY+KIMI 段）。

与 test_antigravity_panel_ui.py 同构：把整段面板代码抽出来用最小 DOM 桩
真跑。KIMI 段复用 ANTIGRAVITY 段的组件（confirmAccountDelete /
ACCT_CONFIRM_OPEN / .ag-move 样式），所以切片从 ANTIGRAVITY 段头一直取到
文件尾。盯六件事：

- **分组渲染**：`Kimi #N · ` 前缀按账号分组，标题=账号名（快照回填），
  ▲▼ 顺位 + ✕ 删除按钮带上账号 id（复用 antigravity 同款交互）；
  ✎ 改名入口在按钮组最前（alias 优先显示）
- **未登录可见**：quota 只有静态说明条也要渲染整卡——「导入账号」入口
  （粘贴 kimi cli 导出 JSON）就长在卡头上（quota 返 None 会被整块隐藏）
- **通道未注册**：providers 里没有 kimi（没加 --kimi）时整块不渲染
- **顺位/删除**：按快照里 id 的真实位次提交全量 id 列表；删除走确认弹窗
- **导入弹窗**：openKimiImport 填 textarea → kimiImportSubmit POST payload
- **跨通道确认锁**：共享 ACCT_CONFIRM_OPEN，同一时刻只有一个确认框

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
# 2026-10 前端按职责拆分（benefits.js 超 700 行）：ANTIGRAVITY 面板段挪到
# benefits_panels.js，KIMI 面板段挪到 benefits_checkin.js。这里分两个文件提取拼接。
BENEFITS_JS_AG = _STATIC / "benefits_antigravity.js"  # ANTIGRAVITY 段
BENEFITS_JS_KIMI = _STATIC / "benefits_kimi.js"       # KIMI 段（含导入）
HELPERS_JS = _STATIC / "benefits_accounts.js"       # 公共件（✎ 改名等，面板段引用）
# KIMI 段复用 ANTIGRAVITY 段的组件（confirmAccountDelete / ACCT_CONFIRM_OPEN），
# 但两段在文件里不相邻：traepat 段挪到 benefits_panels.js 开头，KIMI 段挪到
# benefits_checkin.js 末尾，中间隔着 QODER 段——分两段提取拼接。
# ANTIGRAVITY 段尾：后随 QODER 面板段头。
SEG_AG = re.compile(r"// ---- ANTIGRAVITY 面板.*$", re.S)
SEG_KIMI = re.compile(r"// ---- KIMI 面板.*$", re.S)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑面板 JS（CI 有，本机可选）"
)

_STUB = """
let PANEL = {innerHTML: ''};        // #kimi-panel
globalThis.esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
globalThis.quotaItemHtml = it => '<div class="qitem">' + esc(it.label) +
  (typeof it.remaining === 'string' ? '<span class="qnotice">' + esc(it.remaining) + '</span>' : '') +
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
const TEXTAREA = {value: '', focus() {}};       // #kimi-import-text（openKimiImport 后赋值）
globalThis.TEXTAREA = TEXTAREA;
globalThis.document = {
  querySelector: sel => {
    if (sel === '#kimi-panel .pat-pkg-name') return PANEL.__names ? PANEL.__names[0] : null;
    const m = sel.match(/^#kimi-panel \\[data-kimi-idx="(\\d+)"\\]$/);
    if (m && PANEL.__names) return PANEL.__names[Number(m[1]) - 1] || null;
    return null;
  },
  querySelectorAll: sel => (sel === '#modal-foot button' ? [] : []),
  getElementById: id => {
    if (id === 'kimi-panel') return PANEL;
    if (id === 'modal-title' || id === 'modal-body' || id === 'overlay') {
      if (!MODAL[id]) MODAL[id] = {textContent: '', innerHTML: '', className: '',
        classList: {_s: new Set(),
                    add(c) { this._s.add(c); },
                    remove(c) { this._s.delete(c); },
                    contains(c) { return this._s.has(c); }}};
      return MODAL[id];
    }
    if (id === 'kimi-import-text') return TEXTAREA;
    return null;
  },
};
"""


def _run_js(body: str) -> str:
    text_ag = BENEFITS_JS_AG.read_text(encoding="utf-8")
    text_kimi = BENEFITS_JS_KIMI.read_text(encoding="utf-8")
    ag = SEG_AG.search(text_ag)
    kimi = SEG_KIMI.search(text_kimi)
    assert ag and kimi, "找不到 ANTIGRAVITY/KIMI 面板段（边界注释被改了？）"
    script = (
        _STUB
        # 公共多账号 helper（acctRenameButton/Prompt/Submit 等）：面板段和
        # rename 测试都引用它，必须先于面板段加载
        + HELPERS_JS.read_text(encoding="utf-8")
        + ag.group(0) + "\n" + kimi.group(0)
        + "\n(async () => {\n" + body
        + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def _kimi_provider(items):
    return {"id": "kimi", "name": "Kimi (Kimi Code 订阅)", "quota": {"supported": True, "items": items}}


def _item(n, label, pct):
    return {"label": f"Kimi #{n} · {label}", "used": pct, "total": 100,
            "remaining": 100 - pct, "percent": pct, "unit": "percent",
            "reset_ts": None, "expire_ts": None}


def test_kimi_panel_renders_groups_and_buttons():
    """分组渲染：标题=账号名（快照回填）、▲▼ 顺位、✕ 删除、导入按钮。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [%s]};
let API_CALLS = [];
globalThis.api = async (path) => {
  API_CALLS.push(path);
  return {enabled: true, accounts: [
    {id: 'u1', name: '一号', alias: '主力号', index: 1, hours_left: 11.5, cooling: []},
    {id: 'u2', name: '二号', index: 2, hours_left: null,
     cooling: [{kind: 'quota', minutes_left: 4.2}]},
  ]};
};
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
renderKimiPanel();
await new Promise(r => setTimeout(r, 5));   // loadKimiAccounts 快照回填（首渲→重渲）
const html = PANEL.innerHTML;
console.log(JSON.stringify({
  head: html.includes('Kimi'),
  tag: html.includes('多账号 · 自动切换'),
  name1: html.includes('主力号'), name2: html.includes('二号'),
  noTokenLeft: !html.includes('token 剩'),
  sub2: html.includes('额度冷却 4.2min'),
  moves: (html.match(/kimiMoveAccount\\(/g) || []).length,
  ren1: html.includes("acctRenamePrompt('kimi','u1')"),
  ren2: html.includes("acctRenamePrompt('kimi','u2')"),
  del1: html.includes("kimiDeleteAccount('u1')"),
  del2: html.includes("kimiDeleteAccount('u2')"),
  importBtn: html.includes('openKimiImport()'),
  accountsApi: API_CALLS,
}));
""" % json.dumps(_kimi_provider([_item(1, "5 小时窗口", 25), _item(1, "7 天池", 50),
                                 _item(2, "5 小时窗口", 10), _item(2, "7 天池", 20)])))
    data = json.loads(out.strip().splitlines()[-1])
    assert data["head"] and data["tag"]
    assert data["name1"] and data["name2"], "标题=别名/账号名（快照回填），不是 Kimi #N 代号"
    assert data["noTokenLeft"], "副标题不再有「token 剩 Xh」（已删）"
    assert data["sub2"], "副标题=冷却状态"
    assert data["moves"] == 4, "两个账号各一对 ▲▼（首尾对应的按钮 disabled 但仍在）"
    assert data["ren1"] and data["ren2"], "✎ 改名按钮带账号 id"
    assert data["del1"] and data["del2"], "✕ 带账号 id"
    assert data["importBtn"], "「导入账号」按钮常在"
    # 首渲拉一次；首份快照整卡重渲又拉一次（数据已同走早退，不会循环）
    assert data["accountsApi"] == ["/ui/api/kimi/accounts"] * 2


def test_kimi_panel_visible_when_not_logged_in():
    """未登录：quota 只有静态说明条也要渲染整卡（导入入口不能消失）。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [%s]};
globalThis.api = async () => ({enabled: false, accounts: []});
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
renderKimiPanel();
const html = PANEL.innerHTML;
console.log(JSON.stringify({
  head: html.includes('Kimi'),
  notice: html.includes('buddy login kimi'),
  importBtn: html.includes('openKimiImport()'),
  noGroups: !html.includes('kimiDeleteAccount'),
}));
""" % json.dumps(_kimi_provider([{"label": "Kimi", "used": None, "total": None,
                                  "remaining": "Kimi 未登录：跑 `buddy login kimi`，或在管理面板「导入账号」粘贴 kimi cli 导出的 token JSON",
                                  "percent": None, "reset_ts": None, "expire_ts": None, "unit": None}])))
    data = json.loads(out.strip().splitlines()[-1])
    assert data["head"] and data["notice"], "说明条横贯展示"
    assert data["importBtn"], "未登录也要有「导入账号」"
    assert data["noGroups"], "没有账号就没有 ▲▼/✕"


def test_kimi_panel_hidden_when_channel_absent():
    """通道未注册（没加 --kimi）：整块不渲染。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'trae', quota: {supported: true, items: []}}]};
globalThis.api = async () => { throw new Error('不该调接口'); };
renderKimiPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["html"] == ""


def test_kimi_move_account_posts_full_ids():
    """顺位：按快照里 id 的真实位次提交全量 id 列表（agMoveAccount 同语义）。"""
    out = _run_js("""
KIMI_ACCTS = [
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
await kimiMoveAccount(2, -1);   // #2 上移；快照已在，无需补拉
console.log(JSON.stringify({calls: CALLS, toasts: TOASTS, refreshed: REFRESHED}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert len(data["calls"]) == 1
    assert data["calls"][0]["path"] == "/ui/api/kimi/accounts/order"
    assert data["calls"][0]["body"] == {"ids": ["u2", "u1"]}
    assert any("二号" in t and "#1" in t for t in data["toasts"])
    assert data["refreshed"] == 1, "重排改变 quota_epoch，要刷新额度面板"


def test_kimi_delete_account_modal_flow():
    """删除：确认弹窗（danger 键）→ 确认 → POST /delete → 快照/toast/刷新。"""
    out = _run_js("""
KIMI_ACCTS = [{id: 'u1', name: '一号', index: 1, cooling: []}];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  return {enabled: true, accounts: []};
};
let TOASTS = [], REFRESHED = 0;
globalThis.toast = m => TOASTS.push(m);
globalThis.refreshAll = () => REFRESHED++;
const p = kimiDeleteAccount('u1');          // 打开确认弹窗
await new Promise(r => setTimeout(r, 5));
const shown = {
  title: globalThis.MODAL['modal-title'].textContent,
  body: globalThis.MODAL['modal-body'].innerHTML,
  foot: globalThis.__FOOT,
  overlayShown: globalThis.MODAL['overlay'].classList.contains('show'),
};
globalThis.__acctDelYes();                  // 点「删除该账号」
await p;
console.log(JSON.stringify({
  shown, calls: CALLS, toasts: TOASTS, refreshed: REFRESHED, closed: globalThis.__CLOSED,
  confirmFree: !ACCT_CONFIRM_OPEN,
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["shown"]["title"] == "删除 Kimi 账号"
    assert "一号" in data["shown"]["body"], "弹窗正文带账号名"
    assert 'class="danger"' in data["shown"]["foot"], "删除键用危险样式"
    assert data["shown"]["overlayShown"], "弹窗要真的展开"
    assert len(data["calls"]) == 1
    assert data["calls"][0]["path"] == "/ui/api/kimi/accounts/delete"
    assert data["calls"][0]["body"] == {"id": "u1"}
    assert data["refreshed"] == 1
    assert any("一号" in t for t in data["toasts"])
    assert data["closed"] >= 1 and data["confirmFree"], "确认后弹窗关掉、锁释放"


def test_kimi_delete_cancelled_sends_nothing():
    """确认弹窗取消（closeModal 路径）：不发请求、锁释放。"""
    out = _run_js("""
KIMI_ACCTS = [{id: 'u1', name: '一号', index: 1, cooling: []}];
globalThis.api = async (path) => { throw new Error('取消路径不该发请求'); };
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
const p = kimiDeleteAccount('u1');
await new Promise(r => setTimeout(r, 5));
globalThis.closeModal();                    // 走被 confirmAccountDelete 接管的关闭入口
const yes = await p;
console.log(JSON.stringify({canceled: !yes, confirmFree: !ACCT_CONFIRM_OPEN}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["canceled"] and data["confirmFree"]


def test_kimi_import_modal_flow():
    """导入弹窗：textarea 粘贴 JSON → POST payload → 关窗/刷新。"""
    out = _run_js("""
KIMI_ACCTS = [];
globalThis.BENEFITS = {providers: []};
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  return {enabled: true, accounts: [{id: 'u9', name: '新号', index: 1, cooling: []}]};
};
let TOASTS = [], REFRESHED = 0;
globalThis.toast = m => TOASTS.push(m);
globalThis.refreshAll = () => REFRESHED++;
openKimiImport();
await new Promise(r => setTimeout(r, 5));
const shown = {
  title: globalThis.MODAL['modal-title'].textContent,
  body: globalThis.MODAL['modal-body'].innerHTML,
  foot: globalThis.__FOOT,
};
TEXTAREA.value = '{"access_token": "at", "refresh_token": "rt"}';
await kimiImportSubmit();
console.log(JSON.stringify({
  shown, calls: CALLS, toasts: TOASTS, refreshed: REFRESHED, closed: globalThis.__CLOSED,
  accts: KIMI_ACCTS.map(a => a.id),
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["shown"]["title"] == "导入 Kimi 账号"
    assert "access_token" in data["shown"]["body"]
    assert "kimiImportSubmit" in data["shown"]["foot"]
    assert len(data["calls"]) == 1
    assert data["calls"][0]["path"] == "/ui/api/kimi/accounts/import"
    assert data["calls"][0]["body"]["payload"].startswith("{")
    assert data["accts"] == ["u9"]
    assert data["refreshed"] == 1 and data["closed"] >= 1
    assert any("已导入" in t for t in data["toasts"])


def test_kimi_import_empty_text_rejected():
    """空 textarea 点导入：不请求、toast 提示。"""
    out = _run_js("""
let CALLS = [];
globalThis.api = async (path) => { CALLS.push(path); return {enabled: true, accounts: []}; };
let TOASTS = [];
globalThis.toast = (m, isErr) => TOASTS.push({m, isErr});
globalThis.refreshAll = () => {};
TEXTAREA.value = '   ';
await kimiImportSubmit();
console.log(JSON.stringify({calls: CALLS, toasts: TOASTS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"] == []
    assert data["toasts"] and data["toasts"][0]["isErr"], "提示是警告色"


def test_kimi_cross_channel_confirm_lock():
    """确认框共享一把锁：kimi 的确认框开着时 antigravity 的 ✕ 不叠层、不发请求。"""
    out = _run_js("""
AG_ACCTS = [{index: 1, id: 'a@x.com', email: 'a@x.com', cooling: []}];
KIMI_ACCTS = [{id: 'u1', name: '一号', index: 1, cooling: []}];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  return {enabled: true, accounts: []};
};
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
const pk = kimiDeleteAccount('u1');         // kimi 先开确认框
await new Promise(r => setTimeout(r, 5));
const pa = agDeleteAccount('a@x.com');      // antigravity 的 ✕：锁被占，不叠层直接取消
await new Promise(r => setTimeout(r, 5));
globalThis.__acctDelYes();                  // 只确认 kimi 那个
await pk;
await pa;
console.log(JSON.stringify({
  calls: CALLS, confirmFree: !ACCT_CONFIRM_OPEN,
  closed: globalThis.__CLOSED,
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"] == [
        {"path": "/ui/api/kimi/accounts/delete", "body": {"id": "u1"}},
    ], "只有 kimi 的删除真正提交；antigravity 的 ✕ 被锁挡掉没发请求"
    assert data["confirmFree"], "确认流程收尾后锁释放"
    assert data["closed"] >= 1, "请求收尾后弹窗关闭"


def test_kimi_rename_modal_flow():
    """✎ 改名：弹窗预填当前显示名（alias 优先）→ POST /rename → 快照就地更新 +
    立即重渲（不等 30s 轮询）→ toast。清空提交 = 恢复默认名。"""
    out = _run_js("""
KIMI_ACCTS = [{id: 'u1', name: '一号', alias: '旧名', index: 1, cooling: []}];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  return {enabled: true, accounts: [{id: 'u1', name: '一号', alias: '新名', index: 1, cooling: []}]};
};
let TOASTS = [], RENDERS = 0;
globalThis.toast = m => TOASTS.push(m);
globalThis.refreshAll = () => {};
globalThis.renderKimiPanel = () => { RENDERS++; };   // 面板重渲打桩成计数
acctRenamePrompt('kimi', 'u1');                       // 打开弹窗（预填从快照查）
const shown = {
  title: globalThis.MODAL['modal-title'].textContent,
  body: globalThis.MODAL['modal-body'].innerHTML,
  foot: globalThis.__FOOT,
};
// 桩输入框：value 由弹窗预填（旧名）
const INPUT = {value: '新名'};
document.getElementById = id => {
  if (id === 'acct-rename-input') return INPUT;
  if (globalThis.MODAL[id]) return globalThis.MODAL[id];
  if (id === 'kimi-panel') return PANEL;
  return null;
};
await acctRenameSubmit('kimi', 'u1');
console.log(JSON.stringify({shown, calls: CALLS, toasts: TOASTS, renders: RENDERS,
  closed: globalThis.__CLOSED, alias: KIMI_ACCTS[0].alias}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["shown"]["title"] == "重命名账号"
    assert "留空则恢复默认名" in data["shown"]["body"]
    assert "acctRenameSubmit('kimi','u1')" in data["shown"]["foot"]
    assert len(data["calls"]) == 1
    assert data["calls"][0]["path"] == "/ui/api/kimi/accounts/rename"
    assert data["calls"][0]["body"] == {"id": "u1", "alias": "新名"}
    assert data["alias"] == "新名", "快照就地更新（响应的 alias 为准）"
    assert data["renders"] >= 1, "立即重渲，不等 30s 轮询"
    assert any("已重命名" in t for t in data["toasts"])
    assert data["closed"] >= 1, "弹窗关闭"


def test_kimi_rename_prompt_prefills_alias_from_snapshot():
    """弹窗预填链 alias → name → id（当前显示名一致，改起来不突兀）。"""
    out = _run_js("""
KIMI_ACCTS = [{id: 'u1', name: '一号', alias: '', index: 1, cooling: []}];
acctRenamePrompt('kimi', 'u1');
const html1 = globalThis.MODAL['modal-body'].innerHTML;
KIMI_ACCTS = [{id: 'u2', name: '二号', alias: '别名二', index: 2, cooling: []}];
acctRenamePrompt('kimi', 'u2');
const html2 = globalThis.MODAL['modal-body'].innerHTML;
console.log(JSON.stringify({html1, html2}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert 'value="一号"' in data["html1"], "无别名预填 name"
    assert 'value="别名二"' in data["html2"], "有别名预填 alias"
