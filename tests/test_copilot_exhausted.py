"""copilot 402「月度配额耗尽」：标记下线 + 额度查询确认恢复。

不变量：
1. 402 被识别为 exhausted（与 5 分钟级 429 限流区分）；
2. mark_exhausted 后账号从 available_accounts 剔除、is_exhausted=True；
3. 只有额度查询确认 has_quota=True 才清标记（查询失败/全 0 保持）；
4. usage_has_quota 各种数据形状判定正确；
5. clear_cooldown（删号）连带清掉耗尽标记。
"""
from __future__ import annotations

import pytest

from buddy_proxy.copilot import failover
from buddy_proxy.copilot.upstream import usage_has_quota
from buddy_proxy.copilot.credentials import AccountRef


@pytest.fixture(autouse=True)
def _fresh():
    failover._cooldowns.clear()
    failover.clear_all()
    yield
    failover._cooldowns.clear()
    failover.clear_all()


def _acct(login="jsongo"):
    return AccountRef(id=login, name=login, priority=0, added_at=0)


def test_is_exhausted_signal():
    assert failover.is_exhausted_signal(402, "quota_exceeded",
                                        "You have exceeded your monthly quota") is True
    assert failover.is_exhausted_signal(402, "", "") is True
    assert failover.is_exhausted_signal(429, "rate_limit_exceeded", "slow down") is False
    assert failover.is_exhausted_signal(400, "model_not_supported", "") is False
    assert failover.is_exhausted_signal(500, "", "internal error") is False


def test_mark_exhausted_removes_account_from_available(monkeypatch):
    acct = _acct()
    monkeypatch.setattr(failover, "list_accounts", lambda: [acct])
    assert [a.id for a in failover.available_accounts()] == ["jsongo"]

    failover.mark_exhausted("jsongo", reason="HTTP 402 quota_exceeded")
    assert failover.is_exhausted("jsongo") is True
    assert failover.available_accounts() == []
    # 没有定时冷却：cooldown_left 仍是 0，剔除完全靠标记而非计时
    assert failover.cooldown_left("jsongo") == (0.0, "")


def test_clear_exhausted_only_after_quota_confirmed():
    failover.mark_exhausted("jsongo", reason="402")
    # 查询失败/没额度 → 保持标记
    assert failover.clear_exhausted_if_recovered("jsongo", has_quota=False) is False
    assert failover.is_exhausted("jsongo") is True
    # 确认有额度 → 清除
    assert failover.clear_exhausted_if_recovered("jsongo", has_quota=True) is True
    assert failover.is_exhausted("jsongo") is False
    # 再清一次幂等
    assert failover.clear_exhausted_if_recovered("jsongo", has_quota=True) is False


def test_usage_has_quota():
    assert usage_has_quota({"quota_snapshots": {"premium_interactions": {"remaining": 5}}}) is True
    assert usage_has_quota({"quota_snapshots": {"chat": {"unlimited": True}}}) is True
    assert usage_has_quota({"quota_snapshots": {"premium_interactions": {"remaining": 0}}}) is False
    assert usage_has_quota({"quota_snapshots": {"premium_interactions": {"quota_remaining": 3}}}) is True
    assert usage_has_quota({"quota_snapshots": {"premium_interactions": {"remaining": None}}}) is False
    assert usage_has_quota({}) is False
    assert usage_has_quota({"quota_snapshots": None}) is False
    # 布尔不能当数字（_number 排除 bool）
    assert usage_has_quota({"quota_snapshots": {"x": {"remaining": True}}}) is False


def test_clear_cooldown_also_clears_exhausted():
    failover.mark_exhausted("jsongo", reason="402")
    assert failover.is_exhausted("jsongo") is True
    failover.clear_cooldown("jsongo")
    assert failover.is_exhausted("jsongo") is False


def test_clear_all_clears_exhausted():
    failover.mark_exhausted("a", reason="402")
    failover.mark_exhausted("b", reason="402")
    failover.clear_all()
    assert failover.is_exhausted("a") is False
    assert failover.is_exhausted("b") is False


def test_quota_signal_still_uses_short_cooldown():
    """429 限流仍走 5 分钟 quota 档，不受 exhausted 标记影响。"""
    failover.mark_cooldown("jsongo", quota=True, reason="429")
    left, kind = failover.cooldown_left("jsongo")
    assert 0 < left <= 300 and kind == "quota"
    assert failover.is_exhausted("jsongo") is False  # 429 不记耗尽
