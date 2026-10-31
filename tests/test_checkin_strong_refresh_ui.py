"""「各通道签到」强刷按钮的副作用：只该刷新签到状态，不该连带整页重查。

2026-11-01 用户实测：点豆包那行的「强刷」，DevTools 里连着发出
``refresh → benefits → accounts ×N → model-status → status → overview``。
强刷按钮本身只打一个 ``/ui/api/benefits/refresh``（后端已同步签到状态），
后面那一串全是它事后调 ``loadData(['benefits'], true)`` 引起的：

- ``loadData(manual)`` 会 ``invalidateRecent()``，进入签到页时
  ``dataForTab('benefits')`` = ``['stats', 'benefits']``，于是顺带把
  ``/ui/api/stats``（overview 卡片 + 图表）也强刷一次；
- ``render()`` 重建面板，``renderBenefits()`` 又跑一遍每个通道的
  ``loadXxxAccounts()``，这些函数没有各自的在飞去重，30s 轮询 + 强刷
  叠在一起就成了 ``accounts`` 刷屏。

这里锁两件事（用 node 真跑抽出来的函数，不打假桩逻辑）：

1. 强刷只走「用响应快照原地重绘」那条路，**不再**调 ``loadData``；
   没有快照可用的旧服务才回退强制 GET ``benefits``（且只这一个键）；
2. 同一通道的账号加载在飞时复用，不重复发请求。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import tempfile

import pytest

_STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
CHECKIN_JS = _STATIC / "benefits_checkin.js"
ACCOUNTS_JS = _STATIC / "benefits_accounts.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑签到强刷 JS（CI 有，本机可选）"
)


def _fn(src: str, name: str, end: str) -> str:
    start = src.index(name)
    stop = src.index(end, start)
    return src[start:stop]


_STUB = """
// ---- 最小 DOM / 全局桩：只记录请求，不模拟渲染细节 ----
globalThis.__CALLS = [];
globalThis.__SNAPSHOT = undefined;   // refresh 返回体；undefined = 旧服务（无快照）
globalThis.__ACCOUNT_DELAY = 0;      // 账号接口的人为延迟（测在飞去重）
globalThis.api = async (url, opts) => {
  globalThis.__CALLS.push(String(url));
  if (String(url).endsWith('/ui/api/benefits/refresh')) {
    return globalThis.__SNAPSHOT;
  }
  if (globalThis.__ACCOUNT_DELAY && /\\/accounts$/.test(String(url))) {
    await new Promise(r => setTimeout(r, globalThis.__ACCOUNT_DELAY));
  }
  return {};
};
globalThis.toast = () => {};
globalThis.esc = s => String(s);
globalThis.BENEFITS = {providers: []};
globalThis.__RENDERED = 0;
globalThis.__QUIET = null;
globalThis.renderBenefits = (opts) => {
  globalThis.__RENDERED++;
  globalThis.__QUIET = opts && opts.quiet === true;
};
globalThis.__LOADS = [];
globalThis.loadData = async (keys, manual) => { globalThis.__LOADS.push([keys, !!manual]); };
globalThis.__RERENDERS = 0;
globalThis.refreshAll = () => { globalThis.__RERENDERS++; };
// 各面板里的账号加载器：在本测试里都退化成一个可观察的 GET
globalThis.loadTraepatAccounts = () => globalThis.api('/ui/api/traepat/accounts');
"""


def _run(js: str) -> dict:
    # 用 CommonJS 跑脚本文件（node -e 是 ESM/strict 求值，顶层 const/let 不进全局）
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / "case.js"
        path.write_text(_STUB + "\n" + js + "\n", encoding="utf-8")
        out = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_strong_refresh_redraws_from_snapshot_without_refetching_stats():
    """快照可用时：只发 refresh 一发，用快照原地重绘，不碰 stats/benefits。"""
    fn = _fn(CHECKIN_JS.read_text(encoding="utf-8"),
             "const CKREFRESH", "async function claimNow")
    js = fn + """
(async () => {
  const snapshot = {providers: [{id: 'doubao'}]};
  globalThis.__SNAPSHOT = snapshot;
  globalThis.BENEFITS = {providers: []};
  await refreshCheckin('doubao', null);
  console.log(JSON.stringify({
    calls: globalThis.__CALLS,
    loads: globalThis.__LOADS,
    rendered: globalThis.__RENDERED,
    quiet: globalThis.__QUIET,
    adopted: globalThis.BENEFITS === snapshot,
  }));
})();
"""
    r = _run(js)
    assert r["calls"] == ["/ui/api/benefits/refresh"], r
    assert r["loads"] == [], "强刷后不该再 loadData（会连带 stats/overview）"
    assert r["rendered"] == 1 and r["adopted"] is True, "快照必须直接采用并重绘"
    assert r["quiet"] is True, "必须走静默重绘：不重建各通道面板（否则又刷一串 accounts）"


def test_strong_refresh_falls_back_to_benefits_only_on_old_server():
    """旧服务不回快照：回退只强刷 benefits 一个键，不拉 stats。"""
    fn = _fn(CHECKIN_JS.read_text(encoding="utf-8"),
             "const CKREFRESH", "async function claimNow")
    js = fn + """
(async () => {
  globalThis.__SNAPSHOT = undefined;
  await refreshCheckin('doubao', null);
  console.log(JSON.stringify({loads: globalThis.__LOADS}));
})();
"""
    r = _run(js)
    assert r["loads"] == [[["benefits"], True]], r


def test_account_loader_dedupes_inflight_requests():
    """同一通道的账号加载在飞时复用：30s 轮询 + 整页重绘叠加不再并排多发。"""
    src = ACCOUNTS_JS.read_text(encoding="utf-8")
    # 拼：声明块（含静默开关）+ acctFetch 函数
    decl = src[src.index("const ACCT_INFLIGHT"):src.index("function acctFetch")]
    # acctLoad 紧随其后且会发请求，不能拼进这个无 DOM 的测试桩
    fetch_fn = _fn(src, "function acctFetch", "\n\n/** 账号快照回填")
    js = decl + fetch_fn + """
globalThis.__ACCOUNT_DELAY = 40;
(async () => {
  const call = () => acctFetch('qoder', () => globalThis.api('/ui/api/qoder/accounts'));
  await Promise.all([call(), call(), call()]);   // 三个同时到：只该发一发
  await call();                                   // 落定后再来：新的一发
  console.log(JSON.stringify({calls: globalThis.__CALLS, pending: ACCT_INFLIGHT.size}));
})();
"""
    r = _run(js)
    assert r["calls"] == ["/ui/api/qoder/accounts"] * 2, r
    assert r["pending"] == 0, "落定后必须清掉在飞记录，否则下次永远不再发"


def test_dumate_panel_and_quiet_refresh_use_callable_loader():
    """Dumate 直调与静默重绘的登记表都必须拿到函数，不是加载 Promise。"""
    text = (_STATIC / "benefits_dumate.js").read_text(encoding="utf-8")
    core = (_STATIC / "benefits.js").read_text(encoding="utf-8")
    loader_registry = text[text.index("const ACCT_LOADERS"):]
    quiet_load = core[core.index("function loadAllAccounts"):]
    script = ACCOUNTS_JS.read_text(encoding="utf-8") + text + quiet_load + """
globalThis.fmtNum = n => String(n);
globalThis.syncQuotaFold = () => {};
const panel = {innerHTML: '已渲染'};
globalThis.document = {getElementById: id => id === 'dumate-panel' ? panel : null};
globalThis.BENEFITS = {providers: [{id: 'dumate', quota: {supported: true, items: []}}]};
globalThis.api = async path => {
  globalThis.__CALLS.push(String(path));
  return String(path).endsWith('/accounts') ? {enabled: false, accounts: []} : {ready: false};
};
(async () => {
  document.getElementById('dumate-panel').innerHTML = '已渲染';
  renderDumatePanel();                 // 面板尾部直接调加载器
  await new Promise(r => setTimeout(r, 0));
  await loadAllAccounts();            // 强刷签到后的静默账号对账
  console.log(JSON.stringify({calls: globalThis.__CALLS, quiet: ACCT_NO_RERENDER}));
})();
"""
    result = _run(script)
    assert result["calls"] == [
        "/ui/api/dumate/status", "/ui/api/dumate/accounts", "/ui/api/dumate/accounts",
    ], result
    assert result["quiet"] is False, result
    assert "loadDumateAccounts" in loader_registry, "登记表要覆盖 Dumate"


def test_every_account_loader_goes_through_acct_fetch():
    """各面板自己的 accounts 加载器都经 acctFetch 去重（漏一个就还是刷屏）。"""
    panels = (_STATIC / "benefits_panels.js").read_text(encoding="utf-8")
    checkin = CHECKIN_JS.read_text(encoding="utf-8")
    accounts = ACCOUNTS_JS.read_text(encoding="utf-8")
    for pid in ("traepat", "antigravity", "qoder", "trae", "codebuddy",
                "codebuddyintl"):
        assert f"acctFetch('{pid}'" in panels, pid
    for pid in ("kimi", "codex"):
        assert f"acctFetch('{pid}'" in checkin, pid
    # dumate 走通用 acctLoad，内部同样经 acctFetch 且受静默开关控制
    assert "acctFetch(cfg.pid" in accounts
    assert "acctNoRerender(cfg.pid)" in accounts


def test_refresh_checkin_button_reports_failure_and_unlocks():
    """失败也要解锁按钮、清在飞标记，否则一次失败后强刷按钮永久失效。"""
    fn = _fn(CHECKIN_JS.read_text(encoding="utf-8"),
             "const CKREFRESH", "async function claimNow")
    js = fn + """
(async () => {
  const btn = {disabled: false, textContent: '强刷'};
  globalThis.api = async () => { throw new Error('boom'); };
  await refreshCheckin('doubao', btn);
  const first = {disabled: btn.disabled, text: btn.textContent,
                 inflight: !!CKREFRESH['doubao']};
  let second = null;
  globalThis.api = async () => { second = 'retried'; };
  await refreshCheckin('doubao', btn);
  console.log(JSON.stringify({first, second}));
})();
"""
    r = _run(js)
    assert r["first"] == {"disabled": False, "text": "强刷", "inflight": False}, r
    assert r["second"] == "retried", "失败后必须还能再点"
