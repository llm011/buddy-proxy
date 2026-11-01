"""DuMate 额度标题行不重复计算（前端接线回归）。

用户 2026-11-01 报障：线上可用 ~16,286，界面却显示 ~33,037（正好翻倍）。
根因：``dumate/provider.py`` 的 items 里，「订阅积分」汇总行**本身已是各积分包
的合计**，却又声明了 ``sum_items: True`` 让前端把汇总行 + 每个包一起相加。
Trae 同款坑（总额度 vs 权益包明细），修法一致：汇总行标 ``head_only``、去掉
``sum_items``，标题行走「取第一条有数的」正好取到汇总行。

本测试钉住**前端接线**：面板不得再硬编码 ``sum_items: true``，且对 dumate 形状
（head_only 汇总行 + 各包）的 ``quotaHeadSum`` 输出必须等于汇总行、不是双倍。
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
QUOTA_JS = STATIC / "benefits_quota.js"
DUMATE_JS = STATIC / "benefits_dumate.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑内联 JS（CI 有，本机可选）"
)

_FUNC_RE = re.compile(r"function quotaHeadSum\(q\) \{(.*?)\n\}\n", re.S)


def _run_js(body: str) -> str:
    text = QUOTA_JS.read_text(encoding="utf-8")
    m = _FUNC_RE.search(text)
    assert m, "benefits_quota.js 里找不到 quotaHeadSum"
    stub = """
globalThis.esc = s => String(s);
globalThis.fmtNum = n => String(n);
"""
    proc = subprocess.run(["node", "-e", stub + m.group(0) + "\n" + body],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


#: 真实形状（2026-11-01 抓取）：汇总行 16102.43/45500 = 下面各包之和。
DUMATE = """{
  items: [
    {label: '订阅积分', remaining: 16102.43, total: 45500, head_only: true},
    {label: '订阅套餐（plan_pro）', remaining: 0, total: 25000},
    {label: '积分池（grant_point）', remaining: 2000, total: 2000},
    {label: '积分包（login_bonus）', remaining: 14102.43, total: 18500},
  ],
}"""


def test_dumate_headline_is_summary_row_not_double():
    """不传 sum_items → 取第一条有数的（汇总行）→ 剩 16102.43，不是双倍。"""
    out = _run_js(f"console.log(quotaHeadSum({DUMATE}));")
    assert "16102.43" in out, f"应显示汇总行的 16102.43，实际: {out}"
    # 双倍特征值：16102.43 + (0+2000+14102.43) = 32204.86
    assert "32204.86" not in out, "重复相加了汇总行与各包（翻倍）"


def test_dumate_sum_items_false_would_double():
    """对照：若强行置 sum_items（旧 bug 的调用）就会翻倍——确保修法方向正确。"""
    doubled = DUMATE.replace("items:", "sum_items: true, items:")
    out = _run_js(f"console.log(quotaHeadSum({doubled}));")
    # sum_items 分支只显示合计：汇总行 16102.43 + (0+2000+14102.43) = 32204.86
    assert "32204.86" in out, f"sum_items 分支应把汇总行与各包相加成 32204.86，实际: {out}"


def test_dumate_panel_does_not_hardcode_sum_items():
    """面板调用 quotaHeadSum 时不得再硬编码 sum_items: true。

    后端已经不给这个标记；面板若还自己塞一个，后端改对也会被前端再翻倍一遍。
    """
    src = DUMATE_JS.read_text(encoding="utf-8")
    # 找到 dumate 面板里所有 quotaHeadSum(...) 调用，逐个检查实参
    calls = re.findall(r"quotaHeadSum\(\{[^}]*\}\)", src)
    assert calls, "没找到 quotaHeadSum 调用（面板被改名/挪走？）"
    for call in calls:
        assert "sum_items" not in call, f"面板仍硬编码 sum_items: {call}"