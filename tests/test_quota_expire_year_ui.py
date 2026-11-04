"""额度卡到期日「跨年要补年份」的前端测试。

额度条目原先这样渲染到期时间（``benefits_quota.js``）：:

    new Date(it.expire_ts * 1000).toLocaleString('zh-CN',
        {month: '2-digit', day: '2-digit'})

只给月日、**不给年份**。到期是绝对时点，省掉年份就会与往年/明年的同月日撞形：

用户 2026-11-05 反馈，CodeBuddy 资源包在额度页显示「资源包 · 10/27 到期」，
他看着觉得早就过了。实测该账号（杰森 #1）的裂变包里确实有一条
``2026-10-27 19:00:19 → 2027-10-27 19:00:18``、额度 428 分的**一年期**条目：
真实到期是 2027-10-27，但渲染出来与真·2026-10-27 那批完全同形（都是 ``10/27``），
用户无从分辨。

修法：``fmtExpireMmdd`` 对**不在今年**的日期补上年份（``2027-10-27``），
今年的仍只显示 ``11-16``，保持窄卡不挤。重置时间（``fmtReset``）不动——
它是按周期滚动的相对时点，补年份只是噪声。

运行：
    .venv/bin/python -m pytest tests/test_quota_expire_year_ui.py -v
"""
from __future__ import annotations

import json
import re
import pathlib
import shutil
import subprocess

import pytest

STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
QUOTA_JS = STATIC / "benefits_quota.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑内联 JS（CI 有，本机可选）"
)

#: 真实数据点（杰森 #1 裂变包里那条一年期条目，上游 get-user-resource 实测）。
FAR_FUTURE_TS = 1824634818  # 2027-10-27 19:00:18 本地时间


#: 页面里这些由 ui_format.js 提供，单独 eval 本文件时要给桩（同
#: test_quota_headline_ui.py 的做法）。
_STUB = """
globalThis.esc = s => String(s);
globalThis.fmtNum = n => (Math.round(n * 100) / 100).toLocaleString('en-US');
"""


def _run_js(body: str) -> str:
    text = QUOTA_JS.read_text(encoding="utf-8")
    proc = subprocess.run(
        ["node", "-e", _STUB + text + "\n" + body],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def test_quota_item_is_loadable_on_its_own():
    """整个文件要能独立 eval：本文件先于 benefits.js 加载，不能引用它的符号。

    早先直接调 ``benefits.js`` 的 ``fmtExpireDate`` 就会在真实页面上炸
    （``index.html`` 里 benefits_quota.js 在 benefits.js 之前），所以这里
    连「加载」本身都要守住。
    """
    proc = subprocess.run(
        ["node", "--check", str(QUOTA_JS)], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"语法错误:\n{proc.stderr}"
    # 只认**调用**，注释里提到这个名字是说明来由，不算依赖。
    code = re.sub(r"//[^\n]*", "", QUOTA_JS.read_text(encoding="utf-8"))
    assert "fmtExpireDate(" not in code, \
        "不应跨文件调用 benefits.js 的 fmtExpireDate（本文件先于它加载）"


def test_next_year_expiry_keeps_the_year():
    """明年的到期日必须带年份，否则与今年同月日无法区分。"""
    out = _run_js(f"console.log(fmtExpireMmdd({FAR_FUTURE_TS}));")
    assert out.strip() == "2027-10-27", f"应显示 2027-10-27，实际: {out!r}"


def test_same_year_expiry_stays_short():
    """今年的到期日仍只显示 MM-DD（窄卡不挤）。"""
    import time
    ts = int(time.mktime(time.strptime(f"{time.localtime().tm_year}-11-16 14:49:11",
                                       "%Y-%m-%d %H:%M:%S")))
    out = _run_js(f"console.log(fmtExpireMmdd({ts}));")
    assert out.strip() == "11-16", f"今年应只显示 11-16，实际: {out!r}"


def test_empty_expiry_renders_nothing():
    """没有 expire_ts 就不出「到期」字样（条目照常显示数字）。"""
    out = _run_js(
        "console.log(JSON.stringify([fmtExpireMmdd(null), fmtExpireMmdd(undefined), "
        "fmtExpireMmdd(0)]));"
    )
    assert json.loads(out.strip()) == ["", "", ""], out


def test_quota_item_shows_year_for_far_future_package():
    """端到端：quotaItemHtml 渲染出的资源包行要带 2027，而不是裸的 10-27。"""
    out = _run_js(
        "console.log(quotaItemHtml({label:'资源包', remaining: 428, total: 428, "
        f"percent: 0, expire_ts: {FAR_FUTURE_TS}}}));"
    )
    assert "2027-10-27" in out, f"应带年份: {out!r}"
    assert "到期" in out, out
