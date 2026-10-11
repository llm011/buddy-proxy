"""Qoder 每日活动权益（领取 Credits）单元测试。

断言锁的是**实测校准过的线缆形态**（2026-09，桌面端主进程 + 真实账号联调）：

- 活动面 ``/sash/**`` **不吃 COSY 签名**，裸 ``Bearer`` 即可
- 可领判据是逐条 ``CLAIM_BENEFIT`` + ``CLAIMABLE``，**不是**顶层 ``claimable``
- 领取幂等：已领过再 POST 返回 ``replayed: true``，那是重放不是新领取

fixture 直接取自真实抓包（``main.log`` 里的活动状态响应），回归时会立刻发现
判定逻辑被改坏。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from buddy_proxy.qoder.campaigns import (
    CLAIM_ACTION,
    Campaign,
    ClaimResult,
    campaign_headers,
)
from buddy_proxy.qoder.credentials import Credential
from buddy_proxy.qoder.provider import QoderProvider

# --- 真实抓包：CN 账号 2026-09-28 12:11 的活动状态响应 ------------------------
# 当天有一条 CLAIM_BENEFIT/CLAIMABLE（-556，100 credits），
# 另一条是 VIEW_DETAILS/CLAIMED（-922，无 benefit）。顶层 claimable=true。
LIVE_LISTING = {
    "uid": "00000000-0000-0000-0000-000000000001",
    "showCampaign": True,
    "claimable": True,
    "campaignUrl": "https://openapi.qoder.com.cn/growth-page/activity-iframe",
    "campaigns": [
        {
            "campaignId": "01a0cd42-bdc3-7416-809b-56b06c4382f3",
            "campaignKey": "act-20260923-556",
            "actionType": "CLAIM_BENEFIT",
            "startAt": 1790560800,
            "endAt": 1790647140,
            "claimStatus": "CLAIMABLE",
            "benefit": {
                "kind": "CREDITS",
                "amount": 100,
                "validity": {"mode": "RELATIVE_DAYS", "days": 30},
            },
        },
        {
            "campaignId": "01a05bbf-5668-7031-83d6-91545f97ec05",
            "campaignKey": "act-20260901-922",
            "actionType": "VIEW_DETAILS",
            "startAt": 1788243600,
            "endAt": 1790783940,
            "claimStatus": "CLAIMED",
        },
    ],
}

#: 同日领取**之后**的响应（-556 变 CLAIMED；顶层 claimable 归 false）。
LIVE_LISTING_AFTER = json.loads(json.dumps(LIVE_LISTING))
LIVE_LISTING_AFTER["claimable"] = False
LIVE_LISTING_AFTER["campaigns"][0]["claimStatus"] = "CLAIMED"

#: 真实的幂等重放响应（对已领活动再 POST）。
LIVE_REPLAY = {
    "grantId": "01a0ded1-3543-7203-bce8-6d333161f5e0",
    "status": "CLAIMED",
    "replayed": True,
    "campaignId": "01a05bbf-5668-7031-83d6-91545f97ec05",
    "campaignKey": "act-20260901-922",
    "campaignVersion": 6,
    "claimedAt": "2026-09-26T17:44:07.600812Z",
    "grantedAt": "2026-09-26T17:44:07.750363Z",
}

#: 真实的首次领取响应（replayed 缺省/假，claimedAt 是当下）。
LIVE_FRESH = {
    "grantId": "01a0ee11-2222-3333-4444-555566667777",
    "status": "CLAIMED",
    "campaignId": "01a0cd42-bdc3-7416-809b-56b06c4382f3",
    "campaignKey": "act-20260923-556",
    "claimedAt": "2026-09-28T12:32:24.000000Z",
}


# --- 解析 ------------------------------------------------------------------


def test_campaign_parse_live_entries():
    """真实条目的字段逐项落到 Campaign 上。"""
    c = Campaign.parse(LIVE_LISTING["campaigns"][0])
    assert c is not None
    assert c.id == "01a0cd42-bdc3-7416-809b-56b06c4382f3"
    assert c.key == "act-20260923-556"
    assert c.action_type == CLAIM_ACTION
    assert c.claim_status == "CLAIMABLE"
    assert c.amount == 100
    assert c.kind == "CREDITS"
    assert c.validity == {"mode": "RELATIVE_DAYS", "days": 30}
    # 窗口 = 当日 10:00 → 次日 09:59（UTC+8）
    assert c.start_at == 1790560800
    assert c.end_at == 1790647140
    assert c.is_claimable and not c.is_claimed


def test_campaign_parse_view_details_has_no_benefit():
    """VIEW_DETAILS 类没有 benefit，amount 为 None 且永远不可领。"""
    c = Campaign.parse(LIVE_LISTING["campaigns"][1])
    assert c is not None
    assert c.action_type == "VIEW_DETAILS"
    assert c.amount is None
    assert not c.is_claimable
    assert c.is_claimed


def test_campaign_parse_rejects_malformed():
    """缺 campaignId / 非 dict 的条目一律丢弃（不让坏数据污染判定）。"""
    assert Campaign.parse({"campaignKey": "x"}) is None
    assert Campaign.parse({"campaignId": "  "}) is None
    assert Campaign.parse(None) is None
    assert Campaign.parse("nope") is None


def test_campaign_parse_tolerates_bad_timestamps():
    """时间戳异常时归 0，不影响其余字段与判定。"""
    c = Campaign.parse(
        {"campaignId": "a", "actionType": CLAIM_ACTION, "claimStatus": "CLAIMABLE",
         "startAt": "not-a-number", "endAt": None}
    )
    assert c is not None and c.start_at == 0 and c.end_at == 0 and c.is_claimable


# --- 领取结果语义 ----------------------------------------------------------


def test_claim_result_fresh_claim_is_ok():
    """首次领取：ok，且 replayed 为假。"""
    r = ClaimResult.parse(LIVE_FRESH)
    assert r.ok and not r.replayed
    assert r.status == "CLAIMED"
    assert r.grant_id.startswith("01a0ee11")


def test_claim_result_replay_is_flagged():
    """幂等重放：ok（打卡算完成）但标记 replayed，调用方据此不给积分。"""
    r = ClaimResult.parse(LIVE_REPLAY)
    assert r.ok
    assert r.replayed is True
    assert r.claimed_at == "2026-09-26T17:44:07.600812Z"  # 过去那次的时间


def test_claim_result_error_carries_code():
    """业务失败带 errorCode（如 CAMPAIGN_NOT_FOUND），ok 为假。"""
    r = ClaimResult.parse(
        {"errorCode": "CAMPAIGN_NOT_FOUND", "errorMessage": "campaign was not found"}
    )
    assert not r.ok
    assert r.error_code == "CAMPAIGN_NOT_FOUND"
    assert r.message == "campaign was not found"


# --- 请求头：不吃 COSY 签名 ------------------------------------------------


def test_campaign_headers_use_plain_bearer():
    """``/sash`` 面只认裸 Bearer——不该出现 COSY 签名头/签名 key。"""
    cred = Credential(token="dt-abc", uid="u1", machine_id="m1")
    h = campaign_headers(cred)
    assert h["Authorization"] == "Bearer dt-abc"
    assert h["Cosy-ClientType"] == "10"
    assert h["Cosy-MachineId"] == h["Cosy-MachineToken"] == "m1"
    # 关键：没有签名器产出物
    assert "Cosy-Key" not in h
    assert "Cosy-Scene" not in h
    assert not any(k.startswith("X-Model") for k in h)


# --- provider 集成（假客户端） --------------------------------------------


class _FakeClient:
    """替身：喂真实抓包，记录是否真的发过 claim。"""

    def __init__(self, listing, claim_body=None):
        self._listing = listing
        self._claim_body = claim_body or LIVE_FRESH
        self.claimed: list[str] = []

    async def list(self):
        return [c for c in (Campaign.parse(x) for x in self._listing["campaigns"])
                if c is not None]

    async def claim(self, campaign_id):
        self.claimed.append(campaign_id)
        return ClaimResult.parse(self._claim_body)


def _provider_with(fake, listing) -> QoderProvider:
    p = QoderProvider()
    p._cred = Credential(token="dt-abc", uid="u1", machine_id="m1")

    async def _client():
        return fake

    p._campaigns = _client  # type: ignore[method-assign]
    return p


def test_status_distinguishes_non_claimable_campaign_from_no_activity():
    listing = {
        "campaigns": [{
            "campaignId": "view-1", "campaignKey": "act-view",
            "actionType": "VIEW_DETAILS", "claimStatus": "CLAIMED",
            "startAt": 1, "endAt": 9999999999,
        }]
    }
    p = _provider_with(_FakeClient(listing), listing)
    st = asyncio.run(p.checkin_status())
    assert st["inactive"] is False
    assert st["unavailable"] is True
    assert st["claimable"] is False
    assert "有活动" in st["message"]


def test_status_checks_later_account_when_first_has_no_checkin_campaign(monkeypatch):
    from types import SimpleNamespace
    from buddy_proxy.qoder import provider as qoder_provider

    first = SimpleNamespace(id="a1", priority=0, alias="", name="First", email="first@test", region="cn")
    second = SimpleNamespace(id="a2", priority=1, alias="Second", name="Second", email="second@test", region="cn")
    monkeypatch.setattr(qoder_provider.failover, "available_accounts", lambda *_: [first, second])
    monkeypatch.setattr(qoder_provider, "list_accounts", lambda: [first, second])
    empty_benefit = {"campaigns": [{
        "campaignId": "view-1", "campaignKey": "act-view",
        "actionType": "VIEW_DETAILS", "claimStatus": "CLAIMED",
    }]}
    has_benefit = json.loads(json.dumps(LIVE_LISTING))
    clients = {"a1": _FakeClient(empty_benefit), "a2": _FakeClient(LIVE_LISTING)}

    async def client_for(self, account=None):
        return clients[account.id]

    monkeypatch.setattr(QoderProvider, "_campaigns", client_for)
    st = asyncio.run(QoderProvider().checkin_status())
    assert st["claimable"] is True
    assert st["accounts"][0]["unavailable"] is True
    assert st["accounts"][1]["claimable"] is True
    assert st["accounts"][1]["name"] == "Second"


def test_status_aggregation_carries_daily_credit_top_and_per_account(monkeypatch):
    """多账号聚合不能丢活动信息——否则卡片上「每日 +100.00」时有时无。

    实测 bug：单账号通道（qoderintl）原样返回带 ``daily_credit``，多账号
    通道（qoder CN）聚合时只拷了状态五键，chips 因此忽有忽无。
    """
    from types import SimpleNamespace
    from buddy_proxy.qoder import provider as qoder_provider

    first = SimpleNamespace(id="a1", priority=0, alias="", name="First", email="first@test", region="cn")
    second = SimpleNamespace(id="a2", priority=1, alias="Second", name="Second", email="second@test", region="cn")
    monkeypatch.setattr(qoder_provider.failover, "available_accounts", lambda *_: [first, second])
    monkeypatch.setattr(qoder_provider, "list_accounts", lambda: [first, second])
    clients = {"a1": _FakeClient(LIVE_LISTING), "a2": _FakeClient(LIVE_LISTING)}

    async def client_for(self, account=None):
        return clients[account.id]

    monkeypatch.setattr(QoderProvider, "_campaigns", client_for)
    st = asyncio.run(QoderProvider().checkin_status())
    # 「每日 +X」是多账号**总和**（前端带「（N账号）」）
    assert st["daily_credit"] == 200
    assert st["benefit_kind"] == "CREDITS"
    assert st["activity_name"] == "act-20260923-556"
    # 「下次/截止」也要下发：单账号分支原样返回带着，聚合丢了卡片就没时间 chip
    assert st["next_ts"] == 1790647140
    assert st["next_ts_source"] == "upstream"
    for row in st["accounts"]:
        assert row["daily_credit"] == 100
        assert row["activity_name"] == "act-20260923-556"


def test_single_account_status_also_carries_account_rows(monkeypatch):
    """单账号（qoderintl 只有一个号）也要带 accounts 明细：卡片第二行显示
    账号，和多账号通道版式一致。"""
    from types import SimpleNamespace
    from buddy_proxy.qoder import provider as qoder_provider

    only = SimpleNamespace(id="a9", priority=0, alias="G1", name="G1", email="g@test", region="cn")
    monkeypatch.setattr(qoder_provider.failover, "available_accounts", lambda *_: [only])
    monkeypatch.setattr(qoder_provider, "list_accounts", lambda: [only])
    p = QoderProvider()

    async def client_for(self, account=None):
        return _FakeClient(LIVE_LISTING)

    monkeypatch.setattr(QoderProvider, "_campaigns", client_for)
    st = asyncio.run(p.checkin_status())
    assert st["daily_credit"] == 100
    assert st["accounts"] == [{
        "id": "a9", "index": 1, "name": "G1",
        "checked_in": False, "claimable": True, "inactive": False,
        "unavailable": False, "message": st["message"],
        "daily_credit": 100, "benefit_kind": "CREDITS",
        "activity_name": "act-20260923-556",
    }]


def test_status_claimable_today():
    """领取前：claimable=true，带每日金额与结束时间。"""
    fake = _FakeClient(LIVE_LISTING)
    p = _provider_with(fake, LIVE_LISTING)
    st = asyncio.run(p.checkin_status())
    assert st is not None
    assert st["claimable"] is True
    assert st["checked_in"] is False
    assert st["daily_credit"] == 100
    assert st["campaign_id"] == "01a0cd42-bdc3-7416-809b-56b06c4382f3"
    assert st["ends_at"] == 1790647140


def test_status_after_claim_marks_done():
    """领取后：checked_in=true、claimable=false，且带出金额供日历展示。"""
    fake = _FakeClient(LIVE_LISTING_AFTER)
    p = _provider_with(fake, LIVE_LISTING_AFTER)
    st = asyncio.run(p.checkin_status())
    assert st is not None
    assert st["checked_in"] is True
    assert st["claimable"] is False
    assert st["daily_credit"] == 100


def test_top_level_claimable_is_not_used():
    """顶层 claimable=true 但没有任何 CLAIM_BENEFIT 可领时，不得报可领。

    这是最容易踩的坑：``claimable`` 把 VIEW_DETAILS 也算进来，照它判断会让
    自动打卡对着一条领不出东西的活动反复打。
    """
    listing = json.loads(json.dumps(LIVE_LISTING))
    listing["claimable"] = True
    listing["campaigns"][0]["claimStatus"] = "CLAIMED"  # 唯一可领的已领掉
    p = _provider_with(_FakeClient(listing), listing)
    st = asyncio.run(p.checkin_status())
    assert st is not None
    assert st["claimable"] is False, "顶层 claimable 不该被当成判据"
    assert st["checked_in"] is True


def test_status_reports_claimed_checkin_on_later_account(monkeypatch):
    from types import SimpleNamespace
    from buddy_proxy.qoder import provider as qoder_provider

    first = SimpleNamespace(id="a1", priority=0, alias="", name="First", email="first@test", region="cn")
    second = SimpleNamespace(id="a2", priority=1, alias="Second", name="Second", email="second@test", region="cn")
    monkeypatch.setattr(qoder_provider.failover, "available_accounts", lambda *_: [first, second])
    monkeypatch.setattr(qoder_provider, "list_accounts", lambda: [first, second])
    no_checkin = {"campaigns": [{
        "campaignId": "view-1", "campaignKey": "act-view",
        "actionType": "VIEW_DETAILS", "claimStatus": "CLAIMED",
    }]}
    clients = {"a1": _FakeClient(no_checkin), "a2": _FakeClient(LIVE_LISTING_AFTER)}

    async def client_for(self, account=None):
        return clients[account.id]

    monkeypatch.setattr(QoderProvider, "_campaigns", client_for)
    st = asyncio.run(QoderProvider().checkin_status())
    assert st["checked_in"] is True and st["claimable"] is False
    assert st["accounts"][0]["unavailable"] is True
    assert st["accounts"][1]["checked_in"] is True


def test_claim_scans_later_account_for_claimable_campaign(monkeypatch):
    from types import SimpleNamespace
    from buddy_proxy.qoder import provider as qoder_provider

    first = SimpleNamespace(id="a1", priority=0, alias="", name="First", email="first@test", region="cn")
    second = SimpleNamespace(id="a2", priority=1, alias="Second", name="Second", email="second@test", region="cn")
    monkeypatch.setattr(qoder_provider.failover, "available_accounts", lambda *_: [first, second])
    monkeypatch.setattr(qoder_provider, "list_accounts", lambda: [first, second])
    no_checkin = {"campaigns": [{
        "campaignId": "view-1", "campaignKey": "act-view",
        "actionType": "VIEW_DETAILS", "claimStatus": "CLAIMED",
    }]}
    clients = {"a1": _FakeClient(no_checkin), "a2": _FakeClient(LIVE_LISTING)}

    async def client_for(self, account=None):
        return clients[account.id]

    monkeypatch.setattr(QoderProvider, "_campaigns", client_for)
    out = asyncio.run(QoderProvider().checkin_claim())
    assert clients["a1"].claimed == []
    assert clients["a2"].claimed == ["01a0cd42-bdc3-7416-809b-56b06c4382f3"]
    assert out["extra_credits"] == 100


def test_claim_posts_todays_campaign():
    """可领时：对**当天那条**发 POST，返回积分。"""
    fake = _FakeClient(LIVE_LISTING)
    p = _provider_with(fake, LIVE_LISTING)
    out = asyncio.run(p.checkin_claim())
    assert out is not None
    assert fake.claimed == ["01a0cd42-bdc3-7416-809b-56b06c4382f3"]
    assert out["extra_credits"] == 100
    assert out["checked_in"] is True
    assert out["replayed"] is False


def test_claim_when_nothing_claimable_does_not_post():
    """没可领的（今天已领）→ 不发 POST，返回已领说明。"""
    fake = _FakeClient(LIVE_LISTING_AFTER)
    p = _provider_with(fake, LIVE_LISTING_AFTER)
    out = asyncio.run(p.checkin_claim())
    assert out is not None
    assert fake.claimed == [], "已领的情况下不该再打上游 claim"
    assert out["checked_in"] is True
    assert "已领取" in out["message"]


def test_status_reports_error_if_every_campaign_probe_fails(monkeypatch):
    from types import SimpleNamespace
    from buddy_proxy.qoder import provider as qoder_provider

    account = SimpleNamespace(id="a1", priority=0, alias="", name="First", email="first@test", region="cn")
    monkeypatch.setattr(qoder_provider.failover, "available_accounts", lambda *_: [account])

    async def fail(self, account=None):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(QoderProvider, "_campaigns", fail)
    st = asyncio.run(QoderProvider().checkin_status())
    assert st["error"] == "network unavailable"
    assert "inactive" not in st


def test_claim_does_not_post_view_details_campaign():
    listing = {
        "campaigns": [{
            "campaignId": "view-1", "campaignKey": "act-view",
            "actionType": "VIEW_DETAILS", "claimStatus": "CLAIMED",
        }]
    }
    fake = _FakeClient(listing)
    p = _provider_with(fake, listing)
    out = asyncio.run(p.checkin_claim())
    assert fake.claimed == []
    assert out["unavailable"] is True
    assert "有活动" in out["message"]


def test_claim_replay_reports_no_credits():
    """重放响应不得报成「刚领到 100」——不给积分数字，消息据实说明。"""
    fake = _FakeClient(LIVE_LISTING, claim_body=LIVE_REPLAY)
    p = _provider_with(fake, LIVE_LISTING)
    out = asyncio.run(p.checkin_claim())
    assert out is not None
    assert out["extra_credits"] is None
    assert out["replayed"] is True
    assert "重放" in out["message"]


def test_status_keeps_partial_failure_from_reporting_fully_checked_in(monkeypatch):
    from types import SimpleNamespace
    from buddy_proxy.qoder import provider as qoder_provider

    failed = SimpleNamespace(id="a1", priority=0, alias="", name="First", email="first@test", region="cn")
    claimed = SimpleNamespace(id="a2", priority=1, alias="Second", name="Second", email="second@test", region="cn")
    monkeypatch.setattr(qoder_provider.failover, "available_accounts", lambda *_: [failed, claimed])
    monkeypatch.setattr(qoder_provider, "list_accounts", lambda: [failed, claimed])

    async def client_for(self, account=None):
        if account.id == "a1":
            raise RuntimeError("first account unavailable")
        return _FakeClient(LIVE_LISTING_AFTER)

    monkeypatch.setattr(QoderProvider, "_campaigns", client_for)
    status = asyncio.run(QoderProvider().checkin_status())

    assert status["checked_in"] is False
    assert status["error"] == "first account unavailable"
    assert status["accounts"][0]["error"] == "first account unavailable"
    assert status["accounts"][1]["checked_in"] is True


def test_claim_reports_partial_failure_instead_of_false_success(monkeypatch):
    from types import SimpleNamespace
    from buddy_proxy.qoder import provider as qoder_provider

    failed = SimpleNamespace(id="a1", priority=0, alias="", name="First", email="first@test", region="cn")
    claimed = SimpleNamespace(id="a2", priority=1, alias="Second", name="Second", email="second@test", region="cn")
    monkeypatch.setattr(qoder_provider.failover, "available_accounts", lambda *_: [failed, claimed])
    monkeypatch.setattr(qoder_provider, "list_accounts", lambda: [failed, claimed])

    async def client_for(self, account=None):
        if account.id == "a1":
            raise RuntimeError("first account unavailable")
        return _FakeClient(LIVE_LISTING_AFTER)

    monkeypatch.setattr(QoderProvider, "_campaigns", client_for)
    with pytest.raises(RuntimeError, match="first account unavailable"):
        asyncio.run(QoderProvider().checkin_claim())


def test_provider_declares_checkin_support():
    """provider 必须声明支持打卡，否则不进 /ui 打卡列表与自动循环。"""
    assert QoderProvider.supports_checkin is True
