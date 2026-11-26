"""账号置顶（📌）的前端契约测试：按钮组 + 各通道真实移动函数。

置顶不是后端新接口，而是复用 ``MoveAccount`` 把目标账号挪到 #1。这里不只看
HTML，还真跑 1-based（ag/kimi/qoder/codebuddy/codex/copilot/trae）与 0-based
（raccoon/kiro）的实现，盯四件事：

- 通用 ``acctMoveButtons`` 与各通道专用按钮组都把目标写成顺位 #1；
- #1 的置顶按钮禁用（与 ▲ 同口径），且仍保留账号 id；
- ``pin`` 与相对位移分开传：按钮里的旧 idx 在重排后可能过期，只有显式 pin
  才能保证按 id 校正真实位次后仍落到 #1，而不是算成 no-op/错位；
- ``acctMove`` 分发时只给专用函数多传 pin，普通 ▲▼ 的调用形状不变。

JS 由本机 ``node`` 执行；没有 node 时整文件跳过（不是失败）——CI 有 node。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

_STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
_HELPERS = _STATIC / "benefits_accounts.js"
_MOVE_FILES = [
    _HELPERS,
    _STATIC / "benefits_antigravity.js",
    _STATIC / "benefits_kimi.js",
    _STATIC / "benefits_qoder.js",
    _STATIC / "benefits_codebuddy.js",
    _STATIC / "benefits_codex.js",
    _STATIC / "benefits_copilot.js",
    _STATIC / "benefits_trae.js",
    _STATIC / "benefits_raccoon.js",
    _STATIC / "benefits_kiro.js",
]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑面板 JS（CI 有，本机可选）"
)

_STUB = """
globalThis.esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
globalThis.renderRaccoonPanel = () => {};
globalThis.BENEFITS = {};
globalThis.document = {
  querySelector: () => null,
  querySelectorAll: () => [],
  getElementById: () => null,
};
"""


def _run_js(body: str, files=None) -> str:
    sources = "".join(path.read_text(encoding="utf-8") + "\n" for path in (files or _MOVE_FILES))
    script = (
        _STUB + sources
        + "\n(async () => {\n" + body
        + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    # 九个静态文件拼起来超过 Linux exec 的单参数上限；node -e 在 CI 会 E2BIG。
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as script_file:
        script_file.write(script)
        script_file.flush()
        proc = subprocess.run(
            ["node", script_file.name], capture_output=True, text=True, timeout=30,
        )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def _last_json(out: str):
    return json.loads(out.strip().splitlines()[-1])


def _pin_tag(html: str) -> str:
    match = re.search(r'<button[^>]*class="ghost acct-pin"[^>]*>', html)
    assert match, f"按钮组里没有置顶按钮: {html}"
    return match.group(0)


def test_generic_move_buttons_pin_to_first_and_keep_id():
    """通用按钮：置顶带 id + pin 标志；#1 禁用；无快照用 null 占住参数位。"""
    out = _run_js("""
const mid = acctMoveButtons('codex', 3, 4, 'c');
const first = acctMoveButtons('codex', 1, 4, 'a');
const noId = acctMoveButtons('codex', 3, 4);
console.log(JSON.stringify({
  mid, first, noId,
  up: acctMoveButtons('codex', 3, 4, 'c').match(/codexMoveAccount|acctMove\\([^)]*\\)/g),
}));
""", files=[_HELPERS])
    data = _last_json(out)
    assert "acctMove('codex',3,-2,'c',true)" in data["mid"], "通用置顶要带 id 和 pin"
    assert "acctMove('codex',3,1,'c')" in data["mid"], "原有 ▼ 参数形状不变"
    assert "disabled" in _pin_tag(data["first"]), "顺位 #1 的置顶必须禁用"
    assert "acctMove('codex',1,0,'a',true)" in data["first"], "禁用按钮仍保留目标/id（契约可读）"
    assert "acct-pin" not in data["noId"], "快照没有 id 时不出置顶，不能按旧 idx 挪错人"
    assert "acctMove('codex',3,-1)" in data["noId"], "原有 ▲▼ 不受置顶入口隐藏影响"


@pytest.mark.parametrize(
    ("builder", "pid"),
    [
        ("_ag_move_btns", "antigravity"),
        ("_kimi_move_btns", "kimi"),
        ("_qoder_move_btns", "qoder"),
        ("_codebuddy_move_btns", "codebuddy"),
        ("_trae_move_btns", "trae"),
        ("_raccoon_move_btns", "raccoon"),
    ],
)
def test_specialized_button_builders_pin_to_first(builder, pid):
    """各专用按钮组：📌 走通用分发，id 保留，首账号禁用，▲▼ 原地不动。"""
    out = _run_js(f"""
const mid = {builder}(3, 4, 'c');
const first = {builder}(1, 4, 'a');
const noId = {builder}(3, 4);
console.log(JSON.stringify({{mid, first, noId}}));
""")
    data = _last_json(out)
    assert f"acctMove('{pid}',3,-2,'c',true)" in data["mid"]
    assert "disabled" in _pin_tag(data["first"])
    assert f"acctMove('{pid}',1,0,'a',true)" in data["first"]
    assert "acct-pin" not in data["noId"], "无 id 快照不出置顶，等回填后再开放"
    assert "3,-1)" in data["noId"] and "3,1)" in data["noId"]
    assert data["mid"].count("acct-pin") == 1, "每组只能有一个置顶按钮"
    assert "3,-1,'c')" in data["mid"], "▲ 仍是相对位移"
    assert "3,1,'c')" in data["mid"], "▼ 仍是相对位移"


_MOVERS = [
    ("antigravity", "AG_ACCTS", "agMoveAccount", {"ids": ["c", "a", "b", "d"]}),
    ("kimi", "KIMI_ACCTS", "kimiMoveAccount", {"ids": ["c", "a", "b", "d"]}),
    ("qoder", "QODER_ACCTS", "qoderMoveAccount", {"ids": ["c", "a", "b", "d"]}),
    ("codebuddy", "CODEBUDDY_ACCTS", "codebuddyMoveAccount",
     {"ids": ["c", "a", "b", "d"], "region": "cn"}),
    ("codex", "CODEX_ACCTS", "codexMoveAccount", {"ids": ["c", "a", "b", "d"]}),
    ("copilot", "COPILOT_ACCTS", "copilotMoveAccount", {"ids": ["c", "a", "b", "d"]}),
    ("trae", "TRAE_ACCTS", "traeMoveAccount", {"ids": ["c", "a", "b", "d"]}),
    ("raccoon", "RACCOON_ACCTS", "raccoonMoveAccount", {"ids": ["c", "a", "b", "d"]}),
    ("kiro", "KIRO_ACCTS", "kiroMoveAccount", {"ids": ["c", "a", "b", "d"]}),
]


@pytest.mark.parametrize(("provider", "snapshot", "func", "expected_body"), _MOVERS)
def test_every_move_function_honors_pin_with_stale_index(
    provider, snapshot, func, expected_body,
):
    """旧按钮 idx=4，但 c 的真实位次已是 #3：pin 仍必须把它挪到 #1。

    这是只传 ``delta = 1 - idx`` 的实现抓不到的回归：函数先按 id 把 idx 校正
    成 3，再算 ``3 + (-3) = 0`` 直接 no-op，用户点置顶毫无反应。
    """
    out = _run_js(f"""
let CALLS = [];
globalThis.api = async (path, opts) => {{
  CALLS.push({{path, body: opts && opts.body ? JSON.parse(opts.body) : null}});
  return {{ok: true, enabled: true, accounts: []}};
}};
{snapshot} = ['a', 'b', 'c', 'd'].map(
  (id, i) => ({{index: i + 1, id, name: id, alias: id, nickname: id}}));
await {func}(4, -3, 'c', true);
console.log(JSON.stringify(CALLS));
""")
    calls = _last_json(out)
    assert len(calls) == 1, f"{provider}: 应只提交一次重排: {calls}"
    assert calls[0]["path"] == f"/ui/api/{provider}/accounts/order"
    assert calls[0]["body"] == expected_body, f"{provider}: 置顶未落到 #1"


def test_acct_move_dispatches_pin_without_changing_normal_arity():
    """通用分发：置顶多传第五参；普通 ▲▼ 仍只传三参，旧处理器无需改签名。"""
    out = _run_js("""
const CALLS = [];
globalThis.codexMoveAccount = (...args) => CALLS.push(args);
acctMove('codex', 3, -2, 'c', true);
acctMove('codex', 3, 1, 'c');
acctMove('unknown', 1, 1);
console.log(JSON.stringify(CALLS));
""", files=[_HELPERS])
    calls = _last_json(out)
    assert calls == [[3, -2, "c", True], [3, 1, "c"]]
