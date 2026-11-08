"""codebuddy 多账号签到聚合的「每日 +X」字段穿透。

``_checkin_status_from_payload`` 单账号解析里有 ``daily_credit``（上游
``today_credit``），但多账号聚合的明细行/顶层都把它丢了——签到卡上
codebuddy 的「每日 +X」chip 永远不出现，单账号通道却有（用户反馈的不一致，
与 qoder #120 同款病）。这里锁住聚合必须穿透。
"""

from __future__ import annotations

import pytest

from buddy_proxy.codebuddy_provider import credentials as cb_creds
from buddy_proxy.codebuddy_provider import failover
from buddy_proxy.codebuddy_provider.provider import CodeBuddyProvider


def _cred(account_id: str) -> dict:
    return {
        "account_id": account_id,
        "token": f"at-{account_id}",
        "refresh_token": f"rt-{account_id}",
        "expires_at_ms": 4_102_444_800_000,
        "uid": f"uid-{account_id}",
        "nickname": f"nick-{account_id}",
        "enterprise_id": "ent-1",
        "domain": "https://copilot.tencent.com",
        "machine_id": f"mach-{account_id}",
        "source": "state",
    }


def _payload(checked_in: bool) -> dict:
    return {"code": 0, "data": {
        "active": True, "today_checked_in": checked_in,
        "streak_days": 3, "today_credit": 100,
    }}


@pytest.fixture
def two_accounts(monkeypatch):
    a = cb_creds.save_account_cred(_cred("u1"))
    b = cb_creds.save_account_cred(_cred("u2"))
    monkeypatch.setattr(failover, "_cooldowns", {})

    calls = []

    def fake_post(account_id, path, *args, **kwargs):
        calls.append(account_id)
        return _payload(account_id == a.id)

    monkeypatch.setattr(cb_creds, "api_post_as", fake_post)
    return a, b, calls


def test_status_multi_account_forwards_daily_credit(two_accounts):
    a, b, _ = two_accounts
    st = CodeBuddyProvider().checkin_status()

    # 「每日 +X」是多账号**总和**（前端带「（N账号）」），逐行是各账号的
    assert st["daily_credit"] == 200
    assert len(st["accounts"]) == 2
    for row in st["accounts"]:
        assert row["daily_credit"] == 100


def test_status_single_account_keeps_direct_daily_credit(monkeypatch):
    cb_creds.save_account_cred(_cred("u1"))
    monkeypatch.setattr(failover, "_cooldowns", {})
    monkeypatch.setattr(cb_creds, "api_post_as", lambda *_a, **_k: _payload(True))
    st = CodeBuddyProvider().checkin_status()
    # 单账号直通分支原本就带 daily_credit，别被聚合改造弄丢
    assert st["daily_credit"] == 100


def test_status_all_zero_daily_credit_stays_absent(monkeypatch):
    cb_creds.save_account_cred(_cred("u1"))
    cb_creds.save_account_cred(_cred("u2"))
    monkeypatch.setattr(failover, "_cooldowns", {})
    monkeypatch.setattr(cb_creds, "api_post_as",
                        lambda *_a, **_k: {"code": 0, "data": {
                            "active": True, "today_checked_in": True}})
    st = CodeBuddyProvider().checkin_status()
    assert "daily_credit" not in st, "上游没给金额就别造键，前端按 >0 判定"


def test_claim_when_already_checked_in_still_reports_credits(monkeypatch):
    """今天已签过再点「立即打卡」：本次没有真正 claim，但金额不能留空。

    2026-11-08 用户实报：codebuddy 明明签了，日历里却是空的。根因是这条路径
    走 ``not claimable`` 分支，``extra_credits`` 直接给 None，而 benefits 层的
    取值链（extra_credits → credits → daily_credit）后两档在 claim 的返回里
    根本不存在——回退是死代码，于是一整天都记成「已签到但金额未知」。

    现在从未 claim 的账号的签到状态快照里取 ``daily_credit`` 兜底。
    """
    a = cb_creds.save_account_cred(_cred("u1"))
    b = cb_creds.save_account_cred(_cred("u2"))
    monkeypatch.setattr(failover, "_cooldowns", {})
    # 两个号今天都已签：状态接口如实报告，claim 接口不该被调用
    calls = []

    def fake_post(account_id, path, *args, **kwargs):
        calls.append(path)
        assert path == "/v2/billing/meter/checkin-activity-status", \
            "已签到的号不该再打 daily-checkin"
        return _payload(True)

    monkeypatch.setattr(cb_creds, "api_post_as", fake_post)
    res = CodeBuddyProvider().checkin_claim()

    assert res["checked_in"] is True
    # 两个号各 100，兜底之后不该是 None
    assert res["extra_credits"] == 200
