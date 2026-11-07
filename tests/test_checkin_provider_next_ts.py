"""三个打卡通道各自怎么算 ``next_ts``（provider 层接入）。

轮换语义三家不同，且**只有 qoder 的上游给时间窗**：

- codebuddy：上游只给整个档期（``start_time``/``end_time``），每日轮换按实测
  零点推断 → ``inferred``；档期未开时给开打时刻 → ``upstream``
- trae：上游返回里连档期都没有 → 只能推断零点 ``inferred``
- qoder：活动自带 ``startAt``/``endAt`` → ``upstream``；未领时给本轮**截止**，
  已领时给下一轮开始

fixture 的形态取自 2026-09-30 的真实抓包（时间戳改成相对当下的未来时刻，
否则「下次」必然算不出来，测不到东西）。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from types import SimpleNamespace

from buddy_proxy import codebuddy_provider as cbp
from buddy_proxy.core.checkin import SOURCE_INFERRED, SOURCE_UPSTREAM
from buddy_proxy.trae import provider as tp

FUTURE = int(time.time()) + 3600
PAST = int(time.time()) - 3600


def _local_str(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# --- codebuddy：上游给档期，轮换时刻推断 ------------------------------------


def _cb_status(monkeypatch, data: dict) -> dict:
    # checkin_status 现走多账号 store（failover.available_accounts +
    # creds.api_post_as），mock 两个入口即可，不碰真实凭据目录
    envelope = {"code": 0, "msg": "OK", "data": data}
    acct = cbp.creds.AccountRef(id="uid-1", uid="uid-1", nickname="tester",
                                priority=0, added_at=1000)
    monkeypatch.setattr(cbp.creds, "list_accounts", lambda: [acct])
    monkeypatch.setattr(cbp.creds, "api_post_as",
                        lambda aid, path, body=None, **kw: dict(envelope))
    return cbp.CodeBuddyProvider().checkin_status()


def test_codebuddy_next_ts_is_inferred_midnight_within_season(monkeypatch):
    """档期进行中：给「下一个本地零点」，并标 inferred（不是上游契约）。

    实测 ``start_time = 2026-09-30 00:00:00`` / ``end_time = 2026-10-15
    23:59:59``——上游给的是**整个活动档期**，没有任何每日轮换字段，所以
    零点这个时刻是我们从打卡记录反推的，必须标出来。
    """
    season_end = int(time.time()) + 86400 * 5
    st = _cb_status(monkeypatch, {
        "active": True, "today_checked_in": True, "streak_days": 14,
        "daily_credit": 100, "checkin_dates": [],
        "start_time": _local_str(int(time.time()) - 86400),
        "end_time": _local_str(season_end),
    })
    assert st["next_ts_source"] == SOURCE_INFERRED
    # 必须是明天零点：比「现在」晚、且不超一天
    assert st["next_ts"] > time.time()
    assert st["next_ts"] - time.time() <= 86400
    assert datetime.fromtimestamp(st["next_ts"]).strftime("%H:%M") == "00:00"


def test_codebuddy_no_next_ts_when_season_already_over(monkeypatch):
    """档期已过：不给字段。界面就不显示，比显示一个过期时刻诚实。"""
    st = _cb_status(monkeypatch, {
        "active": True, "today_checked_in": True,
        "start_time": _local_str(int(time.time()) - 86400 * 20),
        "end_time": _local_str(int(time.time()) - 3600),   # 一小时前就结束了
    })
    assert "next_ts" not in st
    assert "next_ts_source" not in st


def test_codebuddy_inactive_season_gives_upstream_open_time(monkeypatch):
    """档期还没开：「下次」= 开打时刻，这个是上游给的（标 upstream）。"""
    opens = int(time.time()) + 86400 * 3
    st = _cb_status(monkeypatch, {
        "active": False, "today_checked_in": False,
        "start_time": _local_str(opens),
        "end_time": _local_str(opens + 86400 * 15),
    })
    assert st["inactive"] is True
    assert st["next_ts_source"] == SOURCE_UPSTREAM
    # 按本地时区字符串往返，允许 1 秒取整误差
    assert abs(st["next_ts"] - opens) <= 1


def test_codebuddy_inactive_with_past_start_gives_nothing(monkeypatch):
    """档期未开且 ``start_time`` 在过去（上一季残留）：算不出下次，不给。"""
    st = _cb_status(monkeypatch, {
        "active": False, "today_checked_in": False,
        "start_time": _local_str(PAST - 86400),
        "end_time": _local_str(PAST),
    })
    assert "next_ts" not in st


def test_codebuddy_missing_dates_do_not_crash(monkeypatch):
    """上游没给 start_time/end_time（或给了垃圾）也不炸——打卡面板不能因此 500。"""
    for data in (
        {"active": True, "today_checked_in": False},
        {"active": True, "today_checked_in": False, "end_time": "nonsense"},
        {"active": True, "today_checked_in": False, "end_time": 12345},
    ):
        st = _cb_status(monkeypatch, data)
        # end_time 缺失/垃圾 → 退回纯零点轮换，仍应给出（trae 同理）
        assert st["next_ts"] > time.time()
        assert st["next_ts_source"] == SOURCE_INFERRED


# --- trae：上游零时间字段，只能推断 ----------------------------------------


def _trae_status(monkeypatch, payload: dict) -> dict:
    monkeypatch.setattr(tp, "fetch_checkin_status", lambda *a, **k: payload)
    return tp.TraeProvider().checkin_status()


def test_trae_next_ts_is_inferred_midnight(monkeypatch):
    """实测返回只有 checked_in/enable/credits/message，一个时间字段都没有。

    轮换只能按打卡记录反推的零点算，故标 inferred。
    """
    st = _trae_status(monkeypatch, {
        "checked_in": True, "code": 0, "credits": 200,
        "did_checked_in": True, "enable": True,
        "extra_credits": 100, "message": "success",
    })
    assert st["next_ts_source"] == SOURCE_INFERRED
    assert st["next_ts"] > time.time()
    assert datetime.fromtimestamp(st["next_ts"]).strftime("%H:%M") == "00:00"


def test_trae_not_checked_in_still_shows_next(monkeypatch):
    """没打也要显示「下次」（此刻等于本轮截止），别只在已签到时给。"""
    st = _trae_status(monkeypatch, {"checked_in": False, "enable": True, "message": "ok"})
    assert st["claimable"] is True
    assert st["next_ts"] > time.time()
    assert st["next_ts_source"] == SOURCE_INFERRED


def test_trae_inactive_campaign_gives_no_next(monkeypatch):
    """活动未开（enable=false）：连有没有下一轮都不知道，不给时刻。"""
    st = _trae_status(monkeypatch, {"checked_in": False, "enable": False, "message": "off"})
    assert st["inactive"] is True
    assert "next_ts" not in st
    assert "next_ts_source" not in st


# --- qoder：上游给窗口，两种状态翻转点不同 ---------------------------------


def _qoder(listing: dict):
    from buddy_proxy.qoder.credentials import Credential
    from buddy_proxy.qoder.provider import QoderProvider

    p = QoderProvider()
    p._cred = Credential(token="dt-abc", uid="u1", machine_id="m1")

    class _Fake:
        cred = p._cred

        async def list(self):
            from buddy_proxy.qoder.campaigns import Campaign
            return [c for c in (Campaign.parse(x) for x in listing.get("campaigns") or []) if c]

    async def _client():
        return _Fake()

    p._campaigns = _client  # type: ignore[method-assign]
    return p


def _qoder_listing(status: str, start: int, end: int) -> dict:
    return {"campaigns": [{
        "campaignId": "01a0cd42-bdc3-7416-809b-56b06c4382f3",
        "campaignKey": "act-20260923-556",
        "actionType": "CLAIM_BENEFIT",
        "startAt": start, "endAt": end, "claimStatus": status,
        "benefit": {"kind": "CREDITS", "amount": 100},
    }]}


def test_qoder_claimed_next_is_window_start_plus_a_day():
    """已领取：下一轮开始 = ``endAt + 60``（实测窗口 10:00 → 次日 09:59）。

    上游不返回未来那条活动，只能从当前窗口推；但绝对时刻来自上游的 endAt，
    所以仍标 upstream。
    """
    # 窗口仍开着（真实形态：昨天 10:00 起、今天 09:59 止，当前在窗口内且已领）
    start = int(time.time()) - 3600
    end = start + 86340
    assert end > time.time(), "fixture 的窗口必须还没到期，否则测不到下一轮推算"
    st = asyncio.run(_qoder(_qoder_listing("CLAIMED", start, end)).checkin_status())
    assert st["checked_in"] is True
    assert st["next_ts_source"] == SOURCE_UPSTREAM
    assert st["next_ts"] == end + 60
    assert st["next_ts"] > time.time()


def test_qoder_claimable_next_is_this_rounds_deadline():
    """还没领：给本轮**截止**，不是下一轮开始。

    此刻用户该去点「立即打卡」，显示截止时间才有意义（错过就没了）；
    显示下一轮开始反而误导成「不用急」。
    """
    start = int(time.time()) - 3600
    end = int(time.time()) + 86340
    st = asyncio.run(_qoder(_qoder_listing("CLAIMABLE", start, end)).checkin_status())
    assert st["claimable"] is True
    assert st["next_ts"] == end
    assert st["next_ts_source"] == SOURCE_UPSTREAM


def test_qoder_fully_expired_window_gives_no_next():
    """窗口整条过期（缓存的旧列表）：两个候选都在过去，就不给字段。

    真实窗口长度 86340 下这是唯一可能的过期形态：``end+60 <= now`` 时
    ``start+86400`` 必然也已过去（差 60 秒都凑不齐）。显示过去的时刻比
    不显示更糟——界面会钉着一个永远「即将刷新」的倒计时。
    """
    old_start = int(time.time()) - 86400 * 2
    old_end = old_start + 86340
    st = asyncio.run(_qoder(_qoder_listing("CLAIMED", old_start, old_end)).checkin_status())
    assert "next_ts" not in st, "两个候选都已过期时不该显示过去的时刻"


def test_qoder_short_window_falls_back_to_start_plus_day():
    """``endAt+60`` 已过但 ``startAt+24h`` 还在未来 → 走 fallback。

    只有窗口比一天短很多时才可达（上游改了轮换规则、或时钟漂移），所以
    单独构造：fallback 不能因为「正常情况下走不到」就烂在那里没人测。
    """
    from buddy_proxy.core.checkin import next_from_window

    now = int(time.time())
    start = now - 43200                 # 12 小时前开始
    end = now - 600                     # 10 分钟前就结束（半日窗口）
    assert next_from_window(start, end, now) == start + 86400
    st = asyncio.run(_qoder(_qoder_listing("CLAIMED", start, end)).checkin_status())
    assert st["next_ts"] == start + 86400
    assert st["next_ts_source"] == SOURCE_UPSTREAM


def test_qoder_inactive_has_no_next():
    """今天没有活动（inactive）：没有「下次」可言。"""
    p = _qoder({"campaigns": []})
    st = asyncio.run(p.checkin_status())
    assert st["inactive"] is True
    assert "next_ts" not in st


def test_qoder_missing_window_fields_do_not_crash():
    """上游漏给 startAt/endAt（0）也不炸。"""
    st = asyncio.run(_qoder(_qoder_listing("CLAIMED", 0, 0)).checkin_status())
    assert "next_ts" not in st


# --- 契约：字段能穿过 snapshot 到前端 --------------------------------------


def test_next_ts_survives_benefits_snapshot(tmp_path, monkeypatch):
    """``next_ts`` 必须原样穿过 BenefitsManager.snapshot 到 /ui/api/benefits。

    前端只认 ``p.checkin.next_ts``；若 snapshot 那层做了字段白名单过滤，
    后端算了也到不了界面——这是「根本没生效」那类坑。
    """
    from buddy_proxy.benefits import BenefitsManager

    class _P:
        id = "fakeck"
        name = "Fake"
        supports_checkin = True

        def checkin_status(self):
            return {"checked_in": True, "claimable": False, "inactive": False,
                    "message": "ok", "next_ts": FUTURE,
                    "next_ts_source": SOURCE_INFERRED}

        def quota(self):
            return None

    # _providers() 恒把默认 codebuddy 通道排在首位（未初始化时它会兜错进
    # snapshot），所以按 id 找自己那条，不要假设下标。
    state = SimpleNamespace(providers={"fakeck": _P()})
    mgr = BenefitsManager(tmp_path / "checkin.jsonl", state)
    snap = asyncio.run(mgr.snapshot())
    ck = next(p["checkin"] for p in snap["providers"] if p["id"] == "fakeck")
    assert ck["next_ts"] == FUTURE
    assert ck["next_ts_source"] == SOURCE_INFERRED
    assert ck["done_today"] is True


def test_codebuddy_claim_throttles_between_accounts(monkeypatch):
    """多账号领取串行节流（照 trae 同款）：第 2 个**可领**账号起必须先 sleep
    再打（上游活动接口对短窗口连打有频控）；单账号直通不 sleep。

    2026-11-07 起「可领」是先查状态得出的：两个号都可领 → 都打、中间睡一拍。
    """
    accts = [cbp.creds.AccountRef(id=f"uid-{n}", uid=f"uid-{n}", nickname=n,
                                  priority=i, added_at=1000 + i)
             for i, n in enumerate(("u1", "u2"))]
    monkeypatch.setattr(cbp.creds, "list_accounts", lambda: accts)
    calls: list[str] = []
    # 状态查询与领取都走 api_post_as，按 path 分流：状态一律「可领」
    monkeypatch.setattr(
        cbp.creds, "api_post_as",
        lambda aid, path, body=None, **kw:
        (calls.append(f"status:{aid}") if path.endswith("checkin-activity-status")
         else calls.append(aid)) or
        ({"code": 0, "data": {"active": True, "today_checked_in": False}}
         if path.endswith("checkin-activity-status")
         else {"code": 0, "msg": "OK", "data": {"credit": 5}}))
    sleeps: list[float] = []
    monkeypatch.setattr(cbp.provider.time, "sleep",
                        lambda s: sleeps.append(s))
    st = cbp.CodeBuddyProvider().checkin_claim()
    claims = [c for c in calls if not c.startswith("status:")]
    assert claims == ["uid-u1", "uid-u2"], "按 failover 顺位串行打"
    assert sleeps == [cbp.provider._CHECKIN_THROTTLE_S], "#1 前不等，#2 前等一次"
    assert st["accounts"][1]["ok"] is True


def test_codebuddy_claim_skips_already_signed_accounts(monkeypatch):
    """已签到的号不参与 claim，也不占节流间隔（用户 2026-11-07 实报一分钟）。

    4 个号只有 1 个可领时，应该只发 1 次 claim、0 次 sleep——而不是对 4 个号
    各打一次（3 个吃 400）再等 3 拍。
    """
    accts = [cbp.creds.AccountRef(id=f"uid-{n}", uid=f"uid-{n}", nickname=n,
                                  priority=i, added_at=1000 + i)
             for i, n in enumerate(("u1", "u2", "u3", "u4"))]
    monkeypatch.setattr(cbp.creds, "list_accounts", lambda: accts)
    claims: list[str] = []
    status_calls: list[str] = []

    def fake_post(aid, path, body=None, **kw):
        if path.endswith("checkin-activity-status"):
            status_calls.append(aid)
            return {"code": 0, "data": {
                "active": True,
                "today_checked_in": aid != "uid-u3",  # 只有 u3 没签
            }}
        claims.append(aid)
        return {"code": 0, "msg": "OK", "data": {"credit": 100}}

    monkeypatch.setattr(cbp.creds, "api_post_as", fake_post)
    sleeps: list[float] = []
    monkeypatch.setattr(cbp.provider.time, "sleep",
                        lambda s: sleeps.append(s))

    st = cbp.CodeBuddyProvider().checkin_claim()

    assert claims == ["uid-u3"], "只有可领的那个号才该打 claim"
    assert sleeps == [], "只有一个号要打，不该有任何节流等待"
    assert sorted(status_calls) == ["uid-u1", "uid-u2", "uid-u3", "uid-u4"]
    # 已签的号仍要在明细里出现（前端逐号渲染，不能凭空消失）
    rows = {r["id"]: r for r in st["accounts"]}
    assert set(rows) == {"uid-u1", "uid-u2", "uid-u3", "uid-u4"}
    assert rows["uid-u3"]["ok"] is True and not rows["uid-u3"].get("skipped")
    assert rows["uid-u1"]["skipped"] is True
    assert st["extra_credits"] == 100


def test_codebuddy_claim_treats_unknown_status_as_claimable(monkeypatch):
    """状态查不通的号按可领处理：宁可多打一次，也不能因为网络抖动漏签。"""
    accts = [cbp.creds.AccountRef(id=f"uid-{n}", uid=f"uid-{n}", nickname=n,
                                  priority=i, added_at=1000 + i)
             for i, n in enumerate(("u1", "u2"))]
    monkeypatch.setattr(cbp.creds, "list_accounts", lambda: accts)
    claims: list[str] = []

    def fake_post(aid, path, body=None, **kw):
        if path.endswith("checkin-activity-status"):
            raise RuntimeError("network down")
        claims.append(aid)
        return {"code": 0, "msg": "OK", "data": {"credit": 5}}

    monkeypatch.setattr(cbp.creds, "api_post_as", fake_post)
    monkeypatch.setattr(cbp.provider.time, "sleep", lambda s: None)

    cbp.CodeBuddyProvider().checkin_claim()
    assert claims == ["uid-u1", "uid-u2"]

