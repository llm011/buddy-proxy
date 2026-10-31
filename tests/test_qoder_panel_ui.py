"""Qoder 面板前端测试（node 桩真跑 benefits_panels.js 的 QODER 段）。

与 test_kimi_panel_ui / test_antigravity_panel_ui 同构：面板段 + 公共件抽出来
用最小 DOM 桩真跑。这卡此前零覆盖（kimi/ag 各有面板测试），而 ✎ 改名和合计行
都是新加的——renderQoderPanel 直接调 quotaHeadSum（定义在 benefits_quota.js，不在
面板段里），加载顺序一变整卡 render 当场炸。所以 quotaHeadSum/fmtNum 从源文件
里抽**真函数**进桩，不另造替身。盯三件事：

- **冒烟**：render 不炸、标题=alias 优先、按钮组 ✎ 在最前、✕ 带 id
- **合计行**：sum_items 求和出「剩 X / Y · 已用 z%」；没可合计条目时**不留空行**
- **副标题**：不带「token 剩 Xh」（2026-10-05 删的）

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
BENEFITS_JS = _STATIC / "benefits_panels.js"
HELPERS_JS = _STATIC / "benefits_accounts.js"   # 公共件（confirmAccountDelete/acctRename*）
BENEFITS_CORE = _STATIC / "benefits_quota.js"   # quotaHeadSum 定义在这
APP_JS = _STATIC / "app.js"                     # fmtNum
SEG_QODER = re.compile(r"// ---- QODER 面板.*$", re.S)
SEG_HEAD_SUM = re.compile(r"function quotaHeadSum\(q\) \{.*?\n\}", re.S)
SEG_FMT_NUM = re.compile(r"function fmtNum\(n\) \{.*?\n\}", re.S)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑面板 JS（CI 有，本机可选）"
)

_STUB = """
let PANEL = {innerHTML: ''};        // #qoder-panel
let INTL_PANEL = {innerHTML: ''};   // #qoderintl-panel
globalThis.esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
globalThis.quotaItemHtml = it => '<div class="qitem">' + esc(it.label) + '</div>';
globalThis.quotaItemsHtml = (items, key) =>
  '<div class="qbody" data-qfold="' + esc(key) + '">' +
  items.map(globalThis.quotaItemHtml).join('') + '</div>';
globalThis.BENEFITS = {};
globalThis.__SYNC_CALLS = 0;
globalThis.syncQuotaFold = () => { globalThis.__SYNC_CALLS++; };
globalThis.refreshAll = () => {};
globalThis.toast = () => {};
globalThis.api = async () => ({enabled: true, accounts: globalThis.__ACCTS || []});
const MODAL = {};
globalThis.MODAL = MODAL;
globalThis.__FOOT = '';
globalThis.__CLOSED = 0;
globalThis.setModalFoot = html => { globalThis.__FOOT = html; };
globalThis.closeModal = () => { globalThis.__CLOSED++; };
globalThis.document = {
  querySelector: () => null,
  querySelectorAll: sel => (sel === '#modal-foot button' ? [] : []),
  getElementById: id => {
    if (id === 'qoder-panel') return PANEL;
    if (id === 'qoderintl-panel') return INTL_PANEL;
    if (id === 'modal-title' || id === 'modal-body' || id === 'overlay') {
      if (!MODAL[id]) MODAL[id] = {textContent: '', innerHTML: '', className: '',
        classList: {_s: new Set(),
                    add(c) { this._s.add(c); },
                    remove(c) { this._s.delete(c); },
                    contains(c) { return this._s.has(c); }}};
      return MODAL[id];
    }
    return null;
  },
};
"""


def _run_js(body: str) -> str:
    panels = BENEFITS_JS.read_text(encoding="utf-8")
    m = SEG_QODER.search(panels)
    assert m, "benefits_panels.js 里找不到 QODER 面板段（边界注释被改了？）"
    head = SEG_HEAD_SUM.search(BENEFITS_CORE.read_text(encoding="utf-8"))
    fmt = SEG_FMT_NUM.search(APP_JS.read_text(encoding="utf-8"))
    assert head and fmt, "抽不到 quotaHeadSum / fmtNum（定义被改了？）"
    script = (
        _STUB
        + fmt.group(0) + "\n"                 # quotaHeadSum 依赖
        + head.group(0) + "\n"                # 真函数：面板段的调用点一起被验
        + HELPERS_JS.read_text(encoding="utf-8")
        + m.group(0)
        + "\n(async () => {\n" + body
        + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def test_qoder_render_smoke_rename_first_and_head_sum():
    """冒烟：标题=alias 优先、✎ 在按钮组最前、合计行真求和、✕ 带 id。"""
    out = _run_js("""
BENEFITS.providers = [{
  id: 'qoder', name: 'Qoder', quota: {supported: true, items: [
    {label: 'Qoder #1 · 订阅额度', remaining: 800, total: 1000, percent: 80, used: 200},
    {label: 'Qoder #1 · 加油包', remaining: 40, total: 100, percent: 40, used: 60},
    {label: 'Qoder #2 · 订阅额度', remaining: 50, total: 100, percent: 50, used: 50},
  ]},
}];
QODER_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com', name: 'a@x.com', alias: '主力号', cooling: []},
  {index: 2, id: 'b@x.com', email: 'b@x.com', cooling: []},
];
globalThis.__ACCTS = QODER_ACCTS;
renderQoderPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    html = data["html"]

    # 标题：alias 优先，没 alias 的走 email
    assert "主力号" in html, "账号标题应显示 alias"
    assert "b@x.com" in html, "无 alias 的账号走 email"

    # 按钮组最前是 ✎（不插名字与副标题中间——就地回填靠这个相邻关系）
    m = re.search(r'<span class="ag-move">(.*?)</span>', html, re.S)
    assert m, "缺 .ag-move 按钮组"
    row = m.group(1)
    assert row.startswith('<button class="ghost" title="重命名'), f"✎ 不在按钮组最前: {row[:80]}"
    assert "acctRenamePrompt('qoder','a@x.com')" in html

    # 合计行：#1 两份额度求和 840/1100，#2 不求和（单条也走 sum_items）
    assert "2 项合计" in html, "Qoder #1 两份额度应合计"
    assert "剩 840" in html, f"合计剩 840，实际: {html[html.find('剩'):html.find('剩') + 40]}"
    assert "已用 24%" in html
    assert "qoderDeleteAccount('a@x.com')" in html

    # 「token 剩 Xh」已删
    assert "token 剩" not in html


def test_qoderintl_panel_renders_global_quota_without_cn_management_api():
    out = _run_js("""
BENEFITS.providers = [{id: 'qoderintl', name: 'Qoder 海外版', quota: {supported: true, items: [
  {label: 'Qoder #1 · Subscription', remaining: 75, total: 100, percent: 25, used: 25},
  {label: 'Qoder #2 · Add-on', remaining: 30, total: 50, percent: 40, used: 20},
]}}];
renderQoderIntlPanel();
console.log(JSON.stringify({html: INTL_PANEL.innerHTML}));
""")
    html = json.loads(out.strip().splitlines()[-1])["html"]
    assert "Qoder 海外版" in html
    assert "Global #1" in html and "Global #2" in html
    assert "Subscription" in html and "Add-on" in html
    assert "refreshProviderQuota('qoderintl'" in html
    assert "/ui/api/qoder/accounts" not in html


def test_qoder_head_sum_hidden_without_usable_items():
    """没可合计的数字条目时合计行整行不渲染——不留空白占位。"""
    out = _run_js("""
BENEFITS.providers = [{
  id: 'qoder', name: 'Qoder', quota: {supported: true, items: [
    {label: 'Qoder #1 · 订阅额度', remaining: '—', total: '—', percent: 50, used: 5},
  ]},
}];
QODER_ACCTS = [{index: 1, id: 'a@x.com', email: 'a@x.com', cooling: []}];
globalThis.__ACCTS = QODER_ACCTS;
renderQoderPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    html = json.loads(out.strip().splitlines()[-1])["html"]
    assert "margin:0 0 6px" not in html, "无可用数字时合计行不该渲染空 div"
    assert "剩 " not in html, "不该渲染「剩 NaN / NaN」"


def test_qoder_head_sum_renders_real_numbers():
    """quotaHeadSum 走 sum_items 分支时面板真把合计行放进去（调用点没被改坏）。"""
    out = _run_js("""
BENEFITS.providers = [{
  id: 'qoder', name: 'Qoder', quota: {supported: true, items: [
    {label: 'Qoder #1 · 订阅额度', remaining: 25, total: 100, percent: 25, used: 75},
  ]},
}];
QODER_ACCTS = [{index: 1, id: 'a@x.com', email: 'a@x.com', cooling: []}];
globalThis.__ACCTS = QODER_ACCTS;
renderQoderPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    html = json.loads(out.strip().splitlines()[-1])["html"]
    assert "剩 25" in html and "/ 100" in html
    assert "已用 75%" in html
    # 单条不出「N 项合计」角标
    assert "项合计" not in html


def test_qoder_sub_wraps_all_rows_in_single_data_qoder_sub():
    """副标题整体（区域/冷却/模型受限）包在唯一一个 data-qoder-sub 容器里。

    就地回填按这个属性「删旧插新」——模型受限行以前不带标记，回填时删不掉，
    状态每变一次就积一行（真机实报「模型受限」重复两遍）。锁：整个副标题块
    只出现一次 data-qoder-sub，且容器里含模型受限行。
    """
    out = _run_js("""
const a = {region: 'cn', cooling: [], models_limited: ['glm-5.3', 'kimi-k3']};
const withLim = _qoder_sub_html(a);
const withoutLim = _qoder_sub_html({region: 'cn', cooling: []});
console.log(JSON.stringify({withLim, withoutLim}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["withLim"].count("data-qoder-sub") == 1, "副标题块必须只有一层容器"
    assert 'data-qoder-sub><div' in data["withLim"], "容器应包住全部小字行"
    assert "模型受限" in data["withLim"]
    assert data["withoutLim"].count("data-qoder-sub") == 1


def test_qoder_acct_matching_is_region_scoped():
    """「Qoder #N」是区内顺位：全球账号排在前面也不能把 CN 顺位对错人。"""
    out = _run_js("""
QODER_ACCTS = [
  {index: 1, id: 'g1', alias: '海外号', region: 'global', cooling: []},
  {index: 2, id: 'c1', alias: 'CN 一号', region: 'cn', cooling: []},
];
const cn1 = _qoder_acct_for(1, 'cn');
const g1 = _qoder_acct_for(1, 'global');
console.log(JSON.stringify({cn1: cn1 && cn1.id, g1: g1 && g1.id}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["cn1"] == "c1", "区内 #1 应匹配 CN 账号，而不是全列表 index=1 的全球账号"
    assert data["g1"] == "g1"


def test_qoder_intl_panel_titles_accounts_and_drops_region_row():
    """海外卡标题=账号名（alias 优先）；副标题不重复「Global 区」；区里顺位对上。"""
    out = _run_js("""
BENEFITS.providers = [{id: 'qoderintl', name: 'Qoder 海外版', quota: {supported: true, items: [
  {label: 'Qoder #1 · Subscription', remaining: 75, total: 100, percent: 25, used: 25},
]}}];
globalThis.__ACCTS = [
  {index: 3, id: 'g1', alias: '海外主力', region: 'global',
   cooling: [{kind: 'quota', minutes_left: 3.2}], models_limited: []},
];
INTL_ACCTS = globalThis.__ACCTS.filter(a => a.region === 'global');
renderQoderIntlPanel();
console.log(JSON.stringify({html: INTL_PANEL.innerHTML}));
""")
    html = json.loads(out.strip().splitlines()[-1])["html"]
    assert "海外主力" in html, "海外卡标题应是账号名"
    assert "额度冷却" in html, "冷却状态应进海外卡副标题"
    assert "Global 区" not in html, "面板头已有 Global 标识，副标题不重复区域"
    assert "模型受限" not in html, "无受限模型的账号不该出现受限行"


def test_qoder_intl_panel_falls_back_to_global_label_without_accounts():
    out = _run_js("""
BENEFITS.providers = [{id: 'qoderintl', name: 'Qoder 海外版', quota: {supported: true, items: [
  {label: 'Qoder #1 · Subscription', remaining: 75, total: 100, percent: 25, used: 25},
]}}];
globalThis.__ACCTS = [];
renderQoderIntlPanel();
console.log(JSON.stringify({html: INTL_PANEL.innerHTML}));
""")
    html = json.loads(out.strip().splitlines()[-1])["html"]
    assert "Global #1" in html, "账号数据没到时回落组名，标题不能空"
