"""签到卡「状态未知」契约（dumate 报障 2026-10-07，静态断言）。

报障：dumate 已在另一台机器自动签到，但本机 8787 面板显示「未打卡」。根因是
本机无 DuMate 登录态 → 后端拿到 ``None`` → 面板把它画成「未签到」+ 可点的
「立即打卡」，把「拿不到状态」谎报成「今天还没打」。

后端修法见 ``tests/test_dumate_provider.py`` / ``tests/test_checkin_rotation.py``
（返回 ``query_failed`` 失败结构）。这里锁前端：``error`` 与 ``query_failed``
都要走「状态未知」徽标，并且此时「立即打卡」按钮禁用——绝不能是可点状态。

渲染函数整体依赖 BENEFITS/DOM，离网 JS harness 的桩成本与这几行不成比例，
故对 benefits.js 做静态断言（与 ``test_checkin_card_declutter.py`` 同思路）。
改成结构性重构时这里要跟着挪。
"""

from __future__ import annotations

import pathlib

BENEFITS_JS = (pathlib.Path(__file__).resolve().parents[1]
               / "src/buddy_proxy/web/static/benefits.js")


def test_query_failed_renders_unknown_badge():
    """``query_failed`` 与 ``error`` 同样进「状态未知」分支。"""
    text = BENEFITS_JS.read_text(encoding="utf-8")
    assert "c.error || c.query_failed" in text, \
        "query_failed 必须与 error 同判——否则会退化成「未签到」"
    assert "状态未知" in text


def test_unknown_state_disables_claim_button():
    """状态未知时「立即打卡」按钮禁用（不能显示成可领）。"""
    text = BENEFITS_JS.read_text(encoding="utf-8")
    assert "unknown || c.done_today || c.inactive || c.unavailable" in text, \
        "unknown 要并进 disabled 条件，否则状态未知时按钮仍可点"


def test_unknown_badge_not_pending():
    """兜底：pending「未签到」只在不 unknown 时才出现。"""
    text = BENEFITS_JS.read_text(encoding="utf-8")
    # 徽标三元的最后一档才是 pending；它前面必须有 unknown 分支拦住
    assert "unknown ?" in text
    assert "pending\">未签到" in text
