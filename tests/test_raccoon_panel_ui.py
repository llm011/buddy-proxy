"""raccoon 额度面板渲染。

2026-11-23 用户两次实报，各对应一条不变量：

1. 「数据不对，我都没用过一点积分，不可能 2100 里只剩下 300」
   → 不许编上限。上游只给剩余量，所以 ``total`` / ``used`` / ``percent``
   必须全为 ``None``（见 test_raccoon_channel 的同名断言）。
2. 「你要复用我多账号的逻辑呀，这里我目前只登录了一个账号，你要用现成的
   组件来展示」
   → 单账号时资源包必须画进 **.pat-pkg 账号卡**（复用 kimi/antigravity 的
   卡片组件），而不是全部横贯全宽摊成说明条。

第 2 条的根因很隐蔽：kimi/antigravity 用
``percent == null && used == null`` 判定「静态说明条」，但本通道刻意不编
上限，正常条目的 percent/used 天然都是 null —— 照抄那段判据会把**所有**
资源包误判成说明条，账号卡里一条明细都没有。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "buddy_proxy" / "web" / "static"

_STUB = """
const panels = new Map();
globalThis.document = {
  getElementById(id) {
    if (!panels.has(id)) {
      panels.set(id, {id, innerHTML: '', insertAdjacentHTML(){}, closest(){return null;}});
    }
    return panels.get(id);
  },
  querySelector(sel) {
    const p = panels.get('raccoon-panel');
    if (!p || !p.innerHTML) return null;
    const m = p.innerHTML.match(/<span class="pat-pkg-name"[^>]*>([^<]*)<\\/span>/);
    if (!m) return null;
    return {
      textContent: m[1],
      _html: p.innerHTML,
      hasAttribute(){ return false; },
      remove(){},
      insertAdjacentHTML(pos, html){ globalThis.__SUB = html; },
      get nextElementSibling(){
        return globalThis.__SUB ? {hasAttribute: () => true, remove(){ globalThis.__SUB = null; }} : null;
      },
    };
  },
  querySelectorAll(){ return []; },
};
globalThis.esc = s => String(s == null ? '' : s);
globalThis.pcolor = () => '#000';
globalThis.acctFetch = async (pid, fn) => fn();
globalThis.api = async () => ({enabled: true, accounts: []});
globalThis.syncQuotaFold = () => {};
globalThis.toast = () => {};
globalThis.openModal = () => {};
globalThis.closeModal = () => {};
globalThis.acctMoveButtons = () => '';
globalThis.acctRenameButton = () => '';
globalThis.acctDisableButton = () => '';
globalThis.quotaHeadSum = () => '';
globalThis.quotaAccountTotal = () => '';
globalThis.quotaProviderFold = () => ({badge:'', toggle:'', bodyStart:'', bodyEnd:''});
globalThis.providerLoadBalanceToggle = () => '';
globalThis.providerRefreshButton = () => '';
globalThis.ACCT_NO_RERENDER = false;
"""


def _run_js(body: str) -> str:
    script = (
        _STUB
        # 真实组件：ui_format（fmtNum 等）+ 额度组件（quotaItemHtml 等）。
        # 面板依赖它们，不能自己造 stub —— 那样测不到「是否真的复用了组件」。
        + (STATIC / "ui_format.js").read_text(encoding="utf-8")
        + (STATIC / "benefits_quota.js").read_text(encoding="utf-8")
        + (STATIC / "benefits_raccoon.js").read_text(encoding="utf-8")
        + "\n(async () => {\n" + body
        + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def _packs_json() -> str:
    """JS 字面量（None → null）。"""
    return json.dumps(_packs())


def _packs():
    """真实上游形状：只有剩余量，没有上限。"""
    return [
        {"label": "每日积分发放", "remaining": 300, "total": None,
         "used": None, "percent": None,
         "expire_ts": "2026-11-29T23:59:59+08:00"},
        {"label": "新人注册礼包", "remaining": 3000, "total": None,
         "used": None, "percent": None},
        {"label": "可用积分合计", "remaining": 3300, "total": None,
         "used": None, "percent": None},
    ]


def test_single_account_packs_render_inside_account_card():
    """单账号：资源包必须在 .pat-pkg 账号卡里，不能摊成全宽说明条。

    这条是用户第二次实报的回归。根因是照抄了 kimi 的
    ``percent == null && used == null → 说明条`` 判据，而本通道正常条目
    天然满足该条件。
    """
    out = _run_js(f"""
globalThis.BENEFITS = {{providers: [{{
  id: 'raccoon', name: '商汤小浣熊', disabled: false,
  quota: {{supported: true, items: {_packs_json()}}},
}}]}};
renderRaccoonPanel();
const html = document.getElementById('raccoon-panel').innerHTML;
console.log(JSON.stringify({{
  html,
  hasCard: html.includes('class="pat-pkg"'),
  hasGrid: html.includes('pat-quota-grid'),
  // 明细必须在 qbody（卡片内的折叠体）里
  packsInBody: html.includes('data-qfold="raccoon:小浣熊"'),
  // 不能出现在 notice 区（那是不带卡片的全宽说明条）
  leakedAsNotice: html.includes('class="qnotice"'),
  showsPack1: html.includes('每日积分发放'),
  showsPack2: html.includes('新人注册礼包'),
}}));
""")
    data = json.loads(out)
    assert data["hasCard"], "单账号也必须渲染账号卡（复用 .pat-pkg 组件）"
    assert data["hasGrid"], "账号卡应放进 .pat-quota-grid（与其它通道一致）"
    assert data["packsInBody"], "资源包明细必须在账号卡的 qbody 里"
    assert not data["leakedAsNotice"], (
        "资源包被误判成静态说明条了——说明 notices 判据写错了"
        "（本通道不编上限，percent/used 天然为 null，不能用它判说明条）")
    assert data["showsPack1"] and data["showsPack2"], "两个资源包都要显示"


def test_account_name_backfilled_after_snapshot():
    """账号快照到达后，卡片标题要换成手机号并显示续期状态。"""
    out = _run_js(f"""
globalThis.BENEFITS = {{providers: [{{
  id: 'raccoon', name: '商汤小浣熊', disabled: false,
  quota: {{supported: true, items: {_packs_json()}}},
}}]}};
globalThis.api = async () => ({{enabled: true, accounts: [
  {{id:'u1', name:'18576630731', alias:'', index:1, phone:'18576630731',
    auto_renew:true, cooling:[]}},
]}});
renderRaccoonPanel();
await new Promise(r => setTimeout(r, 80));
const html = document.getElementById('raccoon-panel').innerHTML;
console.log(JSON.stringify({{
  html,
  phone: html.includes('18576630731'),
  autoRenew: html.includes('自动续期'),
  needsManual: html.includes('需手动重导'),
}}));
""")
    data = json.loads(out)
    assert data["phone"], "账号快照到达后卡片标题应是手机号"
    assert data["autoRenew"], "有密码的账号要标「自动续期」"
    assert not data["needsManual"], "有密码时不该显示「需手动重导」"


def test_query_failed_renders_as_notice_not_card():
    """查询失败才是真正的说明条（横贯全宽），不能塞进账号卡。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{
  id: 'raccoon', name: '商汤小浣熊', disabled: false,
  quota: {supported: true, items: [
    {label: '小浣熊', remaining: '查询失败', query_failed: true,
     used: null, total: null, percent: null},
  ]},
}]};
renderRaccoonPanel();
const html = document.getElementById('raccoon-panel').innerHTML;
console.log(JSON.stringify({
  hasNotice: html.includes('查询失败'),
  hasCard: html.includes('class="pat-pkg"'),
}));
""")
    data = json.loads(out)
    # 真实组件把说明条也渲染成 .qitem（remaining 是字符串文案），
    # 所以判据是「不进账号卡」而不是某个 class 名。
    assert data["hasNotice"], "query_failed 的文案要显示出来"
    assert not data["hasCard"], "查询失败时不该画空账号卡"

def test_progress_bar_rendered_by_shared_component():
    """进度条必须由现成的 ``quotaItemHtml`` 画出来（别自己写一套）。

    用户两次追问「进度条呢？你没用现成的组件吧？」。这条断言渲染结果里出现
    **组件产出的** ``class="qbar"``。

    组件契约（读 benefits_quota.js 得出）：``qbar`` 只在
    ``used + total 同时有值`` 时才画；``head_only`` 的条目会被
    ``quotaItemsHtml`` 跳过，只由 ``quotaHeadSum`` 渲染成标题行文字、
    **没有进度条**。所以「积分总额」不能标 head_only。
    """
    out = _run_js("""
globalThis.BENEFITS = {providers: [{
  id: 'raccoon', name: '商汤小浣熊', disabled: false,
  quota: {supported: true, items: [
    {label: '积分总额', remaining: 3300, total: 3300, used: 0, percent: 0,
     unit: 'credits'},
    {label: '新人注册礼包', remaining: 3000, total: null, used: null,
     percent: null, unit: 'credits'},
  ]},
}]};
renderRaccoonPanel();
const html = document.getElementById('raccoon-panel').innerHTML;
console.log(JSON.stringify({
  hasBar: html.includes('class="qbar"'),
  hasQitem: html.includes('class="qitem"'),
  hasQhead: html.includes('class="qhead"'),
  htmlLen: html.length,
}));
""")
    data = json.loads(out)
    assert data["hasQitem"], "没走组件的 qitem 结构（quotaItemsHtml 没生效）"
    assert data["hasQhead"], "没走组件的 qhead 结构"
    assert data["hasBar"], (
        "进度条没渲染出来——组件要求 used+total 同时有值才画 qbar")


def test_cooling_account_gets_blocked_name_and_remaining_time():
    """风控号名字前加 ❌，副标题明确写风控与剩余时长；健康号仍标可用。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{
  id: 'raccoon', name: '商汤小浣熊', disabled: false,
  quota: {supported: true, items: [
    {label: '小浣熊 #1 · 积分总额', remaining: 100, total: 100, used: 0, percent: 0},
    {label: '小浣熊 #2 · 积分总额', remaining: 200, total: 200, used: 0, percent: 0},
  ]},
}]};
globalThis.api = async () => ({enabled: true, accounts: [
  {id:'u1', name:'一号', alias:'', index:1, auto_renew:false, available:false,
   cooling:[{kind:'risk_control', seconds_left:125, minutes_left:2.1}]},
  {id:'u2', name:'二号', alias:'', index:2, auto_renew:true, available:true, cooling:[]},
]});
renderRaccoonPanel();
await new Promise(r => setTimeout(r, 80));
const html = document.getElementById('raccoon-panel').innerHTML;
console.log(JSON.stringify({
  blockedName: html.includes('❌ 一号'),
  healthyName: html.includes('二号') && !html.includes('❌ 二号'),
  riskText: html.includes('风控冷却 · 剩余 2.1 分钟'),
  availableText: html.includes('✓ 可用'),
  summary: html.includes('可用 1/2'),
  blockedCard: html.includes('data-raccoon-available="false"'),
}));
""")
    data = json.loads(out)
    assert data["blockedName"], "不可用账号必须在名字前加 ❌"
    assert data["healthyName"], "可用账号不能被误标成 ❌"
    assert data["riskText"], "要明确显示风控状态和剩余时长"
    assert data["availableText"] and data["summary"], "可用账号与汇总都要一眼可见"
    assert data["blockedCard"], "卡片状态属性要与后端 available 一致"


def test_cooling_account_without_quota_still_renders_card():
    """额度查询失败时也不能把正在风控的账号从面板里弄丢。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{
  id: 'raccoon', name: '商汤小浣熊', disabled: false,
  quota: {supported: true, items: [
    {label: '小浣熊 · 查询失败', remaining: '查询失败', query_failed: true,
     used: null, total: null, percent: null},
  ]},
}]};
globalThis.api = async () => ({enabled: true, accounts: [
  {id:'u1', name:'风控号', alias:'', index:1, auto_renew:true, available:false,
   cooling:[{kind:'risk_control', seconds_left:1799, minutes_left:30}]},
]});
renderRaccoonPanel();
await new Promise(r => setTimeout(r, 80));
const html = document.getElementById('raccoon-panel').innerHTML;
console.log(JSON.stringify({
  hasCard: html.includes('class="pat-pkg"'),
  blockedName: html.includes('❌ 风控号'),
  riskText: html.includes('风控冷却'),
  notice: html.includes('查询失败'),
}));
""")
    data = json.loads(out)
    assert data["hasCard"] and data["blockedName"], "风控账号没有额度数据也要渲染账号卡"
    assert data["riskText"], "账号卡必须显示风控状态"
    assert data["notice"], "额度查询失败说明仍要保留"


def test_manually_disabled_account_is_not_counted_available():
    """手动下线不参与转发，面板不能仍把「✓ 可用」和可用汇总送给它。"""
    out = _run_js("""
globalThis.DISABLED_ACCOUNTS = ['raccoon:u1'];
globalThis.BENEFITS = {providers: [{
  id: 'raccoon', name: '商汤小浣熊', disabled: false,
  quota: {supported: true, items: [
    {label: '小浣熊 · 积分总额', remaining: 100, total: 100, used: 0, percent: 0},
  ]},
}]};
globalThis.api = async () => ({enabled: true, accounts: [
  {id:'u1', name:'下线号', alias:'', index:1, auto_renew:true, available:true, cooling:[]},
]});
renderRaccoonPanel();
await new Promise(r => setTimeout(r, 80));
const html = document.getElementById('raccoon-panel').innerHTML;
console.log(JSON.stringify({
  blockedName: html.includes('❌ 下线号'),
  disabledText: html.includes('已手动下线'),
  noAvailableBadge: !html.includes('✓ 可用'),
  summary: html.includes('可用 0/1'),
}));
""")
    data = json.loads(out)
    assert data["blockedName"] and data["disabledText"], "手动下线账号要明确标成不可用"
    assert data["noAvailableBadge"] and data["summary"], "可用标记与汇总必须和转发过滤一致"
