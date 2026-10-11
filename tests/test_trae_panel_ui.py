"""Trae work 额度面板前端测试（node 桩真跑 benefits_panels.js 的 TRAE 段）。

与 test_qoder_panel_ui / test_kimi_panel_ui 同构：面板段 + 公共件抽出来用最小
DOM 桩真跑。盯四件事：

- **冒烟**：render 不炸、标题=alias/nickname 优先、按钮组 ✎ 在最前、✕ 带 id
- **head_only 合计**：「总额度」条不进明细列表、只出合计行（各权益包是它的
  明细**不能相加**——qoder 的 sum_items 合计模式对 trae 会重复计算）
- **未登录**：quota.items 全是说明条 → 显示「暂无账号额度数据」引导登录
- **分发**：acctMove/acctDelete 按 pid='trae' 路由到 traeMoveAccount/traeDeleteAccount

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
HELPERS_JS = _STATIC / "benefits_accounts.js"   # 公共件（acctSubHtml/acctMoveButtons/…）
BENEFITS_CORE = _STATIC / "benefits.js"         # quotaHeadSum 定义在这
APP_JS = _STATIC / "app.js"                     # fmtNum
SEG_TRAE = re.compile(r"// ---- TRAE WORK 面板.*$", re.S)
SEG_HEAD_SUM = re.compile(r"function quotaHeadSum\(q\) \{.*?\n\}", re.S)
SEG_FMT_NUM = re.compile(r"function fmtNum\(n\) \{.*?\n\}", re.S)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑面板 JS（CI 有，本机可选）"
)

_STUB = """
let PANEL = {innerHTML: ''};        // #trae-panel
globalThis.esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
globalThis.quotaItemHtml = it => '<div class="qitem">' + esc(it.label) +
  (it.expire_ts ? ' · 到期' : '') + '</div>';   // 与真实现同款：带 expire_ts 拼到期
globalThis.quotaItemsHtml = (items, key) => {
  const shown = (items || []).filter(it => !it.head_only);   // 与 benefits.js 真实现同款
  return '<div class="qbody" data-qfold="' + esc(key) + '">' +
    shown.map(globalThis.quotaItemHtml).join('') + '</div>';
};
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
  querySelectorAll: sel => [],
  getElementById: id => {
    if (id === 'trae-panel') return PANEL;
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
    m = SEG_TRAE.search(panels)
    assert m, "benefits_panels.js 里找不到 TRAE WORK 面板段（边界注释被改了？）"
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


def test_trae_render_smoke_head_only_summary_and_buttons():
    """冒烟 + head_only 合计：总额度不进明细、只出合计行；标题 alias 优先；
    ✎ 在按钮组最前；✕ 带 id；明细条目照铺。"""
    out = _run_js("""
BENEFITS.providers = [{
  id: 'trae', name: 'Trae', quota: {supported: true, items: [
    {label: 'Trae #1 · 总额度', remaining: 87.17, total: 5300, percent: 98,
     used: 5212.83, head_only: true},
    {label: 'Trae #1 · 会员包', remaining: 1000, total: 4000, percent: 75,
     used: 3000, expire_ts: 1790000000},
    {label: 'Trae #1 · 签到奖励', remaining: 0, total: 200, percent: 100,
     used: 200},
    {label: 'Trae #2 · 总额度', remaining: 900, total: 1000, percent: 10,
     used: 100, head_only: true},
    {label: 'Trae #2 · 会员包', remaining: 900, total: 1000, percent: 10,
     used: 100},
  ]},
}];
TRAE_ACCTS = [
  {index: 1, id: 'u1', nickname: 'main', alias: '主力号', region: 'cn', cooling: []},
  {index: 2, id: 'u2', nickname: 'backup0', alias: '', region: 'cn', cooling: []},
];
globalThis.__ACCTS = TRAE_ACCTS;
renderTraePanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    html = data["html"]

    # 标题：alias 优先，无 alias 回落 nickname
    assert "主力号" in html, "账号标题应显示 alias"
    assert "backup0" in html, "无 alias 的账号回落 nickname"

    # head_only「总额度」不进明细（quotaItemsHtml 的 qbody 里没有它）。
    # qbody 到下一个组卡的边界用「Trae #2」锚定（div 嵌套，</div> 截太早）。
    assert 'data-qfold="trae:Trae #1"' in html
    qbody = html.split('data-qfold="trae:Trae #1"')[1].split("Trae #2")[0]
    assert "总额度" not in qbody, "head_only 条不进明细列表"
    assert "会员包" in qbody and "签到奖励" in qbody, "明细条目照铺"

    # 合计行 = 总额度那条自己的数字（不能把明细加起来——那是重复计算）
    assert "剩 87.2" in html, f"合计行取总额度剩余，实际: {html[html.find('剩'):html.find('剩')+40]}"
    assert "/ 5,300" in html or "/ 5300" in html

    # 按钮组最前是 ✎，✕ 带 id
    m = re.search(r'<span class="ag-move">(.*?)</span>', html, re.S)
    assert m, "缺 .ag-move 按钮组"
    assert m.group(1).startswith('<button class="ghost" title="重命名'), "✎ 不在按钮组最前"
    assert "traeDeleteAccount('u1')" in html


def test_trae_free_pack_into_account_card_not_notice():
    """无数字但有组前缀的包（如「免费」包，只有到期日）归进各账号子卡片；
    连前缀都没有的孤条（未登录说明）才横贯全宽（用户 2026-10-06 反馈：
    「Trae #1 · 免费 · 10/31 到期」单独挂在外面像条独立告警）。"""
    out = _run_js("""
BENEFITS.providers = [{
  id: 'trae', name: 'Trae', quota: {supported: true, items: [
    {label: 'Trae #1 · 总额度', remaining: 87.17, total: 5300, percent: 98,
     used: 5212.83, head_only: true},
    {label: 'Trae #1 · 免费', expire_ts: 1791302400},
    {label: '请先登录 trae', remaining: '跑 buddy login trae'},
  ]},
}];
TRAE_ACCTS = [
  {index: 1, id: 'u1', nickname: 'ethan', alias: '主力号', region: 'cn', cooling: []},
];
globalThis.__ACCTS = TRAE_ACCTS;
renderTraePanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    html = json.loads(out.strip().splitlines()[-1])["html"]
    grid_at = html.find('data-qfold="trae:Trae #1"')
    assert grid_at > 0, "组卡没渲染出来"
    head, card = html[:grid_at], html[grid_at:]

    assert "免费" not in head, "「免费」包不该横贯全宽（应归进账号子卡片）"
    assert "免费" in card, "「免费」包应作为明细行出现在子卡片里"
    assert "到期" in card, "到期日跟着包名一起进子卡片"
    assert "请先登录" in head, "无前缀孤条仍进全宽说明区"


def test_trae_panel_hidden_when_provider_missing():
    """trae 未注册（没加 --trae）：整块不渲染，不留空卡。"""
    out = _run_js("""
BENEFITS.providers = [{id: 'qoder', name: 'Qoder', quota: {supported: true, items: []}}];
renderTraePanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    assert json.loads(out.strip().splitlines()[-1])["html"] == ""


def test_trae_empty_items_shows_login_hint():
    """额度条目为空（未登录）：显示登录引导而不是空白卡。"""
    out = _run_js("""
BENEFITS.providers = [{id: 'trae', name: 'Trae', quota: {supported: true, items: []}}];
renderTraePanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    html = json.loads(out.strip().splitlines()[-1])["html"]
    assert "buddy login trae" in html, "未登录要给登录引导"


def test_trae_move_delete_dispatch_by_pid():
    """acctMove/acctDelete 按 pid='trae' 路由到 traeMoveAccount/traeDeleteAccount。"""
    out = _run_js("""
const calls = [];
globalThis.traeMoveAccount = (...a) => calls.push(['move', ...a]);
globalThis.traeDeleteAccount = (...a) => calls.push(['del', ...a]);
acctMove('trae', 2, -1, 'u2');
acctDelete('trae', 'u2');
console.log(JSON.stringify(calls));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert ["move", 2, -1, "u2"] in data
    assert ["del", "u2"] in data
