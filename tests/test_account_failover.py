"""公共冷却状态机 CooldownTracker 单测。

antigravity / kimi / qoder / trae 的 failover.py 都收敛到这一个模块。这里直接
测公共行为（档位、Retry-After 钳制、聚合视图），不依赖任何通道的 AccountRef。
"""
from __future__ import annotations

import time

import pytest

from buddy_proxy.core.account_failover import (
    CooldownTracker,
    DEFAULT_ACCOUNT_COOLDOWN_S,
    DEFAULT_QUOTA_COOLDOWN_S,
    RETRY_AFTER_MAX_S,
    RETRY_AFTER_MIN_S,
)


@pytest.fixture()
def tracker():
    return CooldownTracker()


# -- mark / left 基本行为 ----------------------------------------------------


def test_account_default_cooldown(tracker):
    got = tracker.mark("a")
    assert got == DEFAULT_ACCOUNT_COOLDOWN_S
    left, kind = tracker.left("a")
    assert kind == "account"
    assert DEFAULT_ACCOUNT_COOLDOWN_S - 1 < left <= DEFAULT_ACCOUNT_COOLDOWN_S


def test_quota_kind_uses_quota_duration(tracker):
    got = tracker.mark("a", kind="quota")
    assert got == DEFAULT_QUOTA_COOLDOWN_S
    _left, kind = tracker.left("a")
    assert kind == "quota"


def test_extra_kinds_duration():
    tr = CooldownTracker(extra_kinds={"blacklist": 6 * 3600.0})
    got = tr.mark("a", kind="blacklist")
    assert got == 6 * 3600.0
    _left, kind = tr.left("a")
    assert kind == "blacklist"


def test_unknown_kind_falls_back_to_account(tracker):
    # kind 未注册档位时按 account 档算，不应 KeyError
    got = tracker.mark("a", kind="whatever")
    assert got == DEFAULT_ACCOUNT_COOLDOWN_S


def test_no_cooling_returns_zero(tracker):
    assert tracker.left("nope") == (0.0, "")
    assert tracker.is_cooling("nope") is False


def test_clear_removes(tracker):
    tracker.mark("a")
    assert tracker.is_cooling("a")
    tracker.clear("a")
    assert tracker.left("a") == (0.0, "")


def test_clear_all(tracker):
    tracker.mark("a")
    tracker.mark("b", kind="quota")
    tracker.clear_all()
    assert tracker.snapshot() == {}


def test_expired_entry_treated_as_clear(tracker):
    tracker.mark("a")
    # 直接改内部 until 到过去，模拟时间流逝（不真 sleep）
    until, kind = tracker._cooldowns["a"]
    tracker._cooldowns["a"] = (until - 10_000, kind)
    assert tracker.left("a") == (0.0, "")
    assert tracker.is_cooling("a") is False
    assert "a" not in tracker.snapshot()


# -- Retry-After 钳制 ---------------------------------------------------------


def test_retry_after_overrides_default(tracker):
    got = tracker.mark("a", kind="quota", retry_after="120")
    assert got == 120.0


def test_retry_after_clamped_to_min(tracker):
    got = tracker.mark("a", retry_after="0.0001")
    assert got == RETRY_AFTER_MIN_S


def test_retry_after_clamped_to_max(tracker):
    got = tracker.mark("a", retry_after=str(10 * 86400))
    assert got == RETRY_AFTER_MAX_S


def test_retry_after_invalid_falls_back_to_default(tracker):
    got = tracker.mark("a", kind="quota", retry_after="not-a-number")
    assert got == DEFAULT_QUOTA_COOLDOWN_S


# -- report / snapshot 聚合视图 ----------------------------------------------


def test_report_only_cooling_accounts(tracker):
    tracker.mark("a", kind="quota")
    tracker.mark("b")  # account 档
    # c 不冷却：不应出现在 report
    got = tracker.report(["a", "b", "c"])
    assert "a" in got and "额度冷却" in got
    assert "b" in got and "账号冷却" in got
    assert "c" not in got


def test_report_custom_labels(tracker):
    tr = CooldownTracker(extra_kinds={"blacklist": 3600.0})
    tr.mark("a", kind="blacklist")
    got = tr.report(["a"], labels={"blacklist": "疑似拉黑"})
    assert "疑似拉黑" in got


def test_snapshot_only_active(tracker):
    tracker.mark("a", kind="quota")
    tracker.mark("b")
    snap = tracker.snapshot()
    assert set(snap) == {"a", "b"}
    for left, kind in snap.values():
        assert left > 0
        assert kind in {"quota", "account"}


# -- fmt_left 静态方法 --------------------------------------------------------


def test_fmt_left_minutes():
    assert CooldownTracker.fmt_left(300) == "5.0min"
    assert CooldownTracker.fmt_left(89 * 60) == "89.0min"


def test_fmt_left_hours():
    assert CooldownTracker.fmt_left(6 * 3600) == "6.0h"
    assert CooldownTracker.fmt_left(90 * 60) == "1.5h"


# -- 线程安全冒烟：并发 mark 不崩、left 自洽 -----------------------------------


def test_concurrent_marks(tracker):
    import threading

    def _worker(i: int) -> None:
        tracker.mark(f"acct-{i}", kind="quota" if i % 2 else "account")

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(tracker.snapshot()) == 50


# ---------------------------------------------------------------------------
# 选号诊断：为什么某个账号被跳过
# ---------------------------------------------------------------------------
#
# 动机（2026-11-11 用户实报）：顺位第一的账号本该被用，实际跳到了第三个号，
# 而日志里完全看不到原因——只能靠反推时间线（对比前后 prompt 大小、数各账号
# 出现次数）才敢断定是冷却。静默跳过（`continue` 之后什么也不留）是根因。


def test_explain_skip_reports_reasons():
    from buddy_proxy.core.account_failover import explain_skip

    info = explain_skip("codebuddy", ["a", "b", "c"], ["b"],
                        reason_for=lambda aid: f"{aid} 冷却" if aid == "a" else "")
    assert [s["id"] for s in info["skipped"]] == ["a", "c"]
    assert info["skipped"][0]["reason"] == "a 冷却"
    # 没给理由时不能空着——空串会被读成「没有原因」，而这里确实跳过了
    assert info["skipped"][1]["reason"] == "unknown"
    assert info["available"] == 1 and info["total"] == 3


def test_explain_skip_empty_when_nothing_skipped():
    from buddy_proxy.core.account_failover import explain_skip

    info = explain_skip("codebuddy", ["a", "b"], ["a", "b"])
    assert info["skipped"] == []


def test_explain_skip_survives_broken_reason_for():
    """reason_for 抛异常不能让诊断把转发搞挂。"""
    from buddy_proxy.core.account_failover import explain_skip

    def boom(_aid):
        raise RuntimeError("boom")

    info = explain_skip("x", ["a"], [], reason_for=boom)
    assert info["skipped"][0]["reason"] == "unknown"


def test_cooldown_reason_formats_kind_and_time():
    from buddy_proxy.core.account_failover import CooldownTracker, cooldown_reason

    t = CooldownTracker()
    t.mark("a", kind="quota", retry_after="300")
    got = cooldown_reason(t, "a")
    assert "额度冷却" in got and "min" in got
    # 没在冷却 → 空串（调用方据此换别的说法，如 region 不匹配）
    assert cooldown_reason(t, "nope") == ""
    # 自定义类别名（antigravity 的 blacklist）
    t.mark("b", kind="blacklist")
    assert "疑似拉黑" in cooldown_reason(t, "b", {"blacklist": "疑似拉黑"})


def test_available_accounts_emits_diagnostic_on_skip():
    """真正接线的那一层：有跳过就打日志，没跳过就不打（否则正常路径一行一条）。"""
    from types import SimpleNamespace
    from unittest import mock

    from buddy_proxy.codebuddy_provider import failover

    accts = [SimpleNamespace(id="a1", region="cn"), SimpleNamespace(id="a2", region="cn")]
    failover._cooldowns.clear()
    failover.mark_cooldown("a1", quota=True, retry_after="120")
    lines = []
    with mock.patch.object(failover, "list_accounts", lambda: accts), \
         mock.patch.object(failover.log, "info", lambda *a, **k: lines.append((a, k))):
        assert [a.id for a in failover.available_accounts("cn")] == ["a2"]
        assert lines, "跳过账号时必须留下诊断行"
        # log.info("account_skip: %s", payload)：事件名在格式串里，payload 是 JSON
        fmt, payload = lines[0][0][0], lines[0][0][1]
        assert "account_skip" in fmt
        assert "a1" in payload and "额度冷却" in payload

        # 无人被跳过 → 不产生噪声
        lines.clear()
        failover._cooldowns.clear()
        assert len(failover.available_accounts("cn")) == 2
        assert lines == []


def test_available_accounts_region_filter_is_explained():
    """region 不匹配也要能看出来（qoder/trae 跨区时最容易困惑）。"""
    from types import SimpleNamespace
    from unittest import mock

    from buddy_proxy.qoder import failover

    accts = [SimpleNamespace(id="cn1", region="cn"),
             SimpleNamespace(id="gl1", region="global")]
    failover._cooldowns.clear()
    lines = []
    with mock.patch.object(failover, "list_accounts", lambda: accts), \
         mock.patch.object(failover.log, "info", lambda *a, **k: lines.append((a, k))):
        assert [a.id for a in failover.available_accounts("cn")] == ["cn1"]
        assert lines
        assert "region" in lines[0][0][1]
