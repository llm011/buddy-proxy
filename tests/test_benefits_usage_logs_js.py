"""官方消耗明细弹窗（benefits_usage_logs.js）的 node 桩测试。

三个通道（trae / raccoon / codebuddy）共用一个弹窗，数据来自
``/ui/api/<provider>/usage-logs``。这里盯住几件退化时会直接影响用户的事：

- **只读**：弹窗只发 GET 拉流水，绝不 POST/写回——明细粒度是会话/请求级，
  与我们日志的请求级对不上，回写会重复记账（见模块头注释）。
- **两种分页**：trae/codebuddy 是 offset（有 total、可跳页）；raccoon 是 cursor
  （上游只给 has_more + next_cursor，只能前后翻）。raccoon 误用 offset 分页会
  请求一个上游不认识的 ``page`` 参数、永远只看得到第一页。
- **转义**：preview / error 是上游文本，必须 esc；模型名、客户端名同理。
- **竞态**：用户在拉取中切到别的账号，旧响应回来不能覆盖新弹窗（seq 守卫）。
- **币种**：dollar 显示 ``$x.xxxx``，积分整数不带小数、小数 3 位。
- **入口**：index.html 必须引入本脚本，且在 benefits_accounts.js 之后
  （用到全局 esc/api；三个通道面板在渲染时引用 openUsageLogs）。

JS 由本机 ``node`` 执行；没有 node 时整文件跳过——CI 有。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

_STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
LOGS_JS = _STATIC / "benefits_usage_logs.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑弹窗 JS（CI 有，本机可选）"
)


def _run(script: str) -> dict:
    """在桩环境里执行 ``script``，返回它输出的结果对象。

    桩 + 被测 JS + 用例脚本**拼成一个文件**交给 node（与 test_benefits_accounts_js
    同款）。不嵌套 ``eval``：被测文件开头是 ``'use strict'``，在 eval 里会让
    ``const/let/function`` 变成 eval 的局部作用域，外面的用例读不到任何符号。
    """
    stub = r"""
// ---- 极简 DOM / 全局桩：只实现脚本真正碰到的那几个 ----
const els = {};
const _el = id => (els[id] ||= {innerHTML: '', textContent: '', classList: {add(){}, remove(){}}});
globalThis.document = {getElementById: _el};
globalThis.esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
globalThis.setModalFoot = () => {};
globalThis.closeModal = () => { if (typeof resetUsageLogs === 'function') resetUsageLogs(); };
globalThis.api = async (url, opts) => {
  (globalThis.__calls ||= []).push({url, opts: opts || null});
  if (typeof globalThis.__respond === 'function') return globalThis.__respond(url);
  return globalThis.__data;
};
"""
    full = stub + LOGS_JS.read_text(encoding="utf-8") + "\n(async () => {\n" + script + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    proc = subprocess.run(
        ["node", "-e", full], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 报错:\n{proc.stderr[-1500:]}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("@@")]
    assert lines, f"脚本没有输出结果对象:\nstdout={proc.stdout[-800:]}\nstderr={proc.stderr[-800:]}"
    return json.loads(lines[-1][2:])


# -- 入口与配置 ---------------------------------------------------------------


def test_index_html_loads_script_after_accounts_helpers():
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="/ui/([^"]+)"', html)
    assert "benefits_usage_logs.js" in scripts, "index.html 必须引入明细弹窗脚本"
    assert scripts.index("benefits_usage_logs.js") > scripts.index("benefits_accounts.js"), (
        "要用 benefits_accounts.js 的全局 esc/api，必须排在它之后")


def test_three_providers_configured_with_correct_paging():
    cfg = _run("""
console.log('@@' + JSON.stringify({
  trae: USAGE_LOGS_CFG.trae.paging, codebuddy: USAGE_LOGS_CFG.codebuddy.paging,
  raccoon: USAGE_LOGS_CFG.raccoon.paging,
  paths: ['trae','raccoon','codebuddy'].map(p => USAGE_LOGS_CFG[p].path)}));
""")
    assert cfg == {"trae": "offset", "codebuddy": "offset", "raccoon": "cursor",
                   "paths": ["/ui/api/trae/usage-logs", "/ui/api/raccoon/usage-logs",
                             "/ui/api/codebuddy/usage-logs"]}


# -- 只读：绝不回写 -----------------------------------------------------------


def test_only_gets_never_writes_back():
    calls = _run("""
globalThis.__data = {rows: [], total: 0, currency: 'credits'};
await openUsageLogs('trae', 'acc-1', '账号一');
console.log('@@' + JSON.stringify(globalThis.__calls));
""")
    assert len(calls) == 1
    assert calls[0]["url"].startswith("/ui/api/trae/usage-logs?id=acc-1"), calls
    assert calls[0]["opts"] in (None, {"method": "GET"}), "明细只读：不能带写方法"


# -- offset 分页（trae / codebuddy）-------------------------------------------


def test_offset_paging_passes_page_number_and_renders_total():
    out = _run("""
globalThis.__respond = url => {
  const page = Number(new URL('http://x' + url).searchParams.get('page'));
  return {rows: [{ts: 1700000000, model: 'glm-5.4', credits: 1.5256, preview: 'p' + page}],
          total: 95, page, page_size: 30, currency: 'credits'};
};
await openUsageLogs('trae', 'acc-1');
await usageLogsGo(3);
const foot = document.getElementById('modal-foot').innerHTML;
console.log('@@' + JSON.stringify({urls: globalThis.__calls.map(c => c.url), foot}));
""")
    assert "page=1" in out["urls"][0] and "page=3" in out["urls"][1], out["urls"]
    assert "第 3/4 页" in out["foot"] and "共 95 条" in out["foot"], out["foot"]


def test_offset_rows_render_model_credit_and_escaped_preview():
    html = _run("""
globalThis.__data = {rows: [{ts: 1700000000, model: '<img src=x>', credits: 12,
                             preview: '<script>bad()</script>'}],
                     total: 1, page: 1, page_size: 30, currency: 'credits'};
await openUsageLogs('trae', 'acc-1');
console.log('@@' + JSON.stringify({body: document.getElementById('modal-body').innerHTML}));
""")["body"]
    assert "<script>bad()</script>" not in html, "上游文本必须转义"
    assert "&lt;script&gt;" in html
    assert "<img src=x>" not in html and "&lt;img" in html
    assert ">12<" in html, "积分整数不带小数"


# -- cursor 分页（raccoon）----------------------------------------------------


def test_cursor_paging_sends_cursor_not_page():
    out = _run("""
globalThis.__respond = url => {
  const sp = new URL('http://x' + url).searchParams;
  if (sp.get('cursor')) return {rows: [{ts: 2, event: '第二页', credits: 3}],
                                has_more: false, currency: 'credits'};
  return {rows: [{ts: 1, event: '第一页', credits: 1}], has_more: true,
          next_cursor: 'CUR+2', currency: 'credits'};
};
await openUsageLogs('raccoon', 'acc-1');
await usageLogsGo(1);            // 下一页
await usageLogsGo(-1);           // 回上一页
console.log('@@' + JSON.stringify(globalThis.__calls.map(c => decodeURIComponent(c.url))));
""")
    assert "page=" not in out[0], "raccoon 是 cursor 分页，不能发 page 参数"
    assert "cursor=CUR+2" in out[1], "下一页要带上游给的 next_cursor"
    assert "cursor=" not in out[2], "回到第一页不带 cursor"
    assert len(out) == 3


def test_cursor_next_disabled_when_no_more():
    out = _run("""
globalThis.__data = {rows: [{ts: 1, event: 'e', credits: 1}], has_more: false,
                     next_cursor: null, currency: 'credits'};
await openUsageLogs('raccoon', 'acc-1');
const before = globalThis.__calls.length;
await usageLogsGo(1);            // 没有下一页，不该再发请求
console.log('@@' + JSON.stringify({calls: globalThis.__calls.length - before,
                                    foot: document.getElementById('modal-foot').innerHTML}));
""")
    assert out["calls"] == 0, "has_more=false 时点下一页不该打上游"
    assert "下一页 ›</button>" in out["foot"] and "disabled" in out["foot"]


# -- 币种与空值 ---------------------------------------------------------------


def test_currency_format_dollar_vs_credits():
    out = _run("""
const r1 = _ulCredit({credits: 0.123456, currency: 'dollar'});
const r2 = _ulCredit({credits: 1, currency: 'credits'});
const r3 = _ulCredit({credits: 1.5256, currency: 'credits'});
const r4 = _ulCredit({credits: null, currency: 'credits'});
console.log('@@' + JSON.stringify({r1, r2, r3, r4}));
""")
    assert out == {"r1": "$0.1235", "r2": "1", "r3": "1.526", "r4": "—"}


def test_empty_rows_show_empty_state():
    out = _run("""
globalThis.__data = {rows: [], total: 0, currency: 'credits'};
await openUsageLogs('trae', 'acc-1');
console.log('@@' + JSON.stringify({body: document.getElementById('modal-body').innerHTML}));
""")["body"]
    assert "没有消耗记录" in out


# -- 错误与竞态 ---------------------------------------------------------------


def test_upstream_error_is_escaped_and_not_thrown():
    out = _run("""
globalThis.__data = {error: '<b>账号不存在</b>', rows: [], total: 0};
await openUsageLogs('trae', 'acc-1');
console.log('@@' + JSON.stringify({body: document.getElementById('modal-body').innerHTML}));
""")["body"]
    assert "拉取失败" in out
    assert "<b>" not in out and "&lt;b&gt;" in out, "错误原话要转义"


def test_stale_response_does_not_overwrite_newer_modal():
    """拉取中切到别的账号：旧响应回来要被丢弃（否则显示的是 A 的数据挂在 B 的标题下）。"""
    out = _run("""
let resolveA;
globalThis.__respond = url => {
  if (url.includes('id=acc-a')) return new Promise(r => { resolveA = r; });
  return Promise.resolve({rows: [{ts: 1, model: 'B-model', credits: 2}], total: 1, currency: 'credits'});
};
const pa = openUsageLogs('trae', 'acc-a', 'A');   // 挂起
await openUsageLogs('trae', 'acc-b', 'B');          // 先完成
resolveA({rows: [{ts: 1, model: 'A-model', credits: 9}], total: 1, currency: 'credits'});
await pa;
console.log('@@' + JSON.stringify({body: document.getElementById('modal-body').innerHTML}));
""")["body"]
    assert "B-model" in out and "A-model" not in out, "旧响应不能覆盖新弹窗"


def test_stale_response_after_close_does_not_overwrite_shared_modal():
    """关闭后打开 rename/test：在途明细响应不能再覆盖新弹窗及保存按钮。"""
    out = _run("""
let resolveA;
globalThis.__respond = () => new Promise(r => { resolveA = r; });
const pending = openUsageLogs('trae', 'acc-a', 'A');
closeModal();
document.getElementById('modal-title').textContent = '重命名账号';
document.getElementById('modal-body').innerHTML = '<input id="rename-input">';
document.getElementById('modal-foot').innerHTML = '<button id="save">保存</button>';
resolveA({rows: [{ts: 1, model: 'A-model', credits: 9}], total: 1, currency: 'credits'});
await pending;
console.log('@@' + JSON.stringify({
  title: document.getElementById('modal-title').textContent,
  body: document.getElementById('modal-body').innerHTML,
  foot: document.getElementById('modal-foot').innerHTML,
  state: UL,
}));
""")
    assert out["title"] == "重命名账号"
    assert out["body"] == '<input id="rename-input">'
    assert out["foot"] == '<button id="save">保存</button>'
    assert out["state"] is None


def test_real_close_modal_invalidates_usage_logs():
    """node 桩之外，再钉住生产 closeModal 真的调用了作废钩子。"""
    src = (_STATIC / "app_run_test.js").read_text(encoding="utf-8")
    match = re.search(r"function closeModal\(\) \{(?P<body>.*?)\n\}", src, re.S)
    assert match and "resetUsageLogs()" in match.group("body")


# -- 三通道面板都有入口 -------------------------------------------------------


@pytest.mark.parametrize("jsfile,provider", [
    ("benefits_trae.js", "trae"),
    ("benefits_raccoon.js", "raccoon"),
    ("benefits_codebuddy.js", "codebuddy"),
])
def test_provider_panel_has_logs_entry(jsfile, provider):
    src = (_STATIC / jsfile).read_text(encoding="utf-8")
    assert f"openUsageLogs('{provider}'" in src, f"{jsfile} 缺少「📜 明细」入口"


def test_trae_logs_entry_hidden_for_global_region():
    """海外账号实测 total=0，先不给入口（点了全是空态误导）。"""
    src = (_STATIC / "benefits_trae.js").read_text(encoding="utf-8")
    m = re.search(r"const logsBtn = \(acct && ([^)]+)\)\s*\n", src)
    assert m and "region" in m.group(1) and "global" in m.group(1), (
        "trae 明细入口必须按 region 过滤掉海外账号")


def test_css_has_table_styles():
    css = (_STATIC / "style.css").read_text(encoding="utf-8")
    assert ".trae-logs" in css and ".trae-log-preview" in css
