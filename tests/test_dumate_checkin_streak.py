"""DuMate 签到的「连续 N 天」与日历日期来源。

上游 ``loginBonusInfo`` 已经给了完整签到日期列表 ``signInDays``，面板的
「连续 N 天」与打卡日历都应该直接从它来，而不是自己攒本地历史——顶栏和
日历两处口径分开算，迟早会一个说连了 22 天、另一个日历上缺两格。

实测（2026-10-30）本机数据：27 次签到、累计 13500 分、日期列表 27 个
（10-02 与 10-05 之间断过三天，所以连续天数是 26 而不是 27）。
"""

from __future__ import annotations

import datetime

from buddy_proxy.dumate import checkin as dumate_checkin


def _ts(y, m, d, hh=12):
    return datetime.datetime(y, m, d, hh, 0, 0).timestamp()


def _payload(days, *, issued=True, points=13500, times=27):
    return {"success": True, "result": {
        "hasIssued": issued,
        "totalPoints": points,
        "totalTimes": times,
        "signInDays": list(days),
    }}


# --- 连续天数 ---------------------------------------------------------------


def test_streak_counts_back_from_today():
    """今天已签：从今天往前数连续日期。"""
    days = ["2026-07-01", "2026-07-29", "2026-07-30", "2026-07-31"]
    assert dumate_checkin._sign_in_streak(days, now=_ts(2026, 7, 31)) == 3


def test_streak_survives_before_todays_claim():
    """今天还没签：从昨天往前数，不能一过零点就把连续清零。

    这是展示口径上的关键一分：自动打卡设在 09:30，用户早上打开面板时今天
    还没签，若严格要求「今天在列表里」就显示 0 天，看着像昨天漏签了。
    """
    days = ["2026-07-29", "2026-07-30", "2026-07-31"]
    assert dumate_checkin._sign_in_streak(days, now=_ts(2026, 8, 1, hh=8)) == 3


def test_streak_is_zero_when_yesterday_and_today_missing():
    """断签超过一天：连续归零（只剩今天的补数才重新开始）。"""
    days = ["2026-07-29", "2026-07-30"]
    assert dumate_checkin._sign_in_streak(days, now=_ts(2026, 8, 2)) == 0


def test_streak_breaks_on_gap():
    """实测口径：10-02 与 10-05 之间断了三天 → 从 10-30 往前只数到 10-05。"""
    days = (["2026-10-02"]
            + [f"2026-10-{d:02d}" for d in range(5, 31)])
    assert len(days) == 27
    assert dumate_checkin._sign_in_streak(days, now=_ts(2026, 10, 30)) == 26


def test_streak_ignores_junk_and_empty():
    """脏日期 / 非列表 / 空列表都只影响自己，不抛异常。"""
    assert dumate_checkin._sign_in_streak(None) == 0
    assert dumate_checkin._sign_in_streak([]) == 0
    assert dumate_checkin._sign_in_streak("2026-07-31") == 0
    days = ["bad", 123, "2026-07-31"]
    assert dumate_checkin._sign_in_streak(days, now=_ts(2026, 7, 31)) == 1


def test_streak_uses_local_date_for_timestamp():
    """``now`` 是 epoch 秒，按本地时区换算日期（与日历同口径）。"""
    moment = datetime.datetime(2026, 7, 31, 23, 30).timestamp()
    assert dumate_checkin._sign_in_streak(["2026-07-31"], now=moment) == 1


# --- 状态解析 ---------------------------------------------------------------


def test_parse_status_exposes_checkin_dates_and_streak():
    """``checkin_dates`` 交给 BenefitsManager 记日历；``sign_in_days`` 原样保留。"""
    days = ["2026-07-30", "2026-07-31"]
    status = dumate_checkin._parse_status_payload(_payload(days))
    assert status["checked_in"] is True
    assert status["claimable"] is False
    assert status["checkin_dates"] == days
    assert status["sign_in_days"] == days
    assert status["total_times"] == 27
    assert status["daily_credit"] == 500
    # next_ts 仍是本地零点（百度自然日轮换），标 inferred
    assert status["next_ts_source"] == "inferred"


def test_parse_status_streak_zero_without_days():
    """上游没给 signInDays（旧版本 / 字段缺失）→ 0，界面不显示 chip 即可。"""
    payload = {"success": True, "result": {"hasIssued": False, "totalPoints": 0,
                                           "totalTimes": 0}}
    status = dumate_checkin._parse_status_payload(payload)
    assert status["checkin_dates"] == []
    assert status["streak_days"] == 0


def test_sign_in_days_are_normalized_for_calendar():
    """重复日期 / 脏值 / 乱序：下发前收干净，日历按字面串比对才不会静默少一天。"""
    payload = _payload(["2026-07-31", "2026-07-30", "2026-07-31",
                        "bad", 123, "2026-13-01"])
    status = dumate_checkin._parse_status_payload(payload)
    assert status["sign_in_days"] == ["2026-07-30", "2026-07-31"]
    assert status["checkin_dates"] == ["2026-07-30", "2026-07-31"]


# --- 下发到界面的字段（2026-10-30 报障「打卡区域没有豆包」）-------------------


class _CheckinProvider:
    """最小签到 provider：checkin_status 原样回灌给定状态，quota 不查。"""

    id = "dumate"
    name = "百度搭子"
    supports_checkin = True

    def __init__(self, status):
        self._status = status

    def checkin_status(self):
        return self._status

    def quota(self):
        return None


def _entry_for(status):
    """跑一遍 snapshot 的单通道条目，拿前端真正收到的 ``checkin`` 字段。"""
    import asyncio
    from types import SimpleNamespace

    from buddy_proxy.benefits import BenefitsManager

    mgr = BenefitsManager("/dev/null", SimpleNamespace(providers={}))
    provider = _CheckinProvider(status)
    return asyncio.run(
        mgr._provider_entry("dumate", provider, set(), {}, "2026-10-30"))["checkin"]


def test_entry_keeps_streak_and_daily_credit():
    """「连续 N 天」「每日 +X」必须活到响应里——只算出来不给前端等于没算。

    报障现场：``/ui/api/benefits`` 里 dumate 的 streak_days 是 null，前端
    ``c.streak_days >= 2`` 恒假，打卡卡上除了一行「已签到」什么都没有，
    用户以为豆包压根没接进打卡区域。
    """
    days = [f"2026-10-{d:02d}" for d in range(5, 31)]
    entry = _entry_for(dumate_checkin._parse_status_payload(_payload(days)))
    assert entry["supported"] is True
    assert entry["streak_days"] == 26
    assert entry["daily_credit"] == 500
    assert entry["done_today"] is True
    assert entry["sign_in_days"] == days


def test_entry_drops_calendar_only_field_but_keeps_supported():
    """``checkin_dates`` 只给日历合并用，不下发；``supported`` 不许被盖掉。"""
    days = ["2026-10-29", "2026-10-30"]
    status = dumate_checkin._parse_status_payload(_payload(days))
    # provider 状态里带了同名字段（qoder 就有 supported），也不能顶掉能力声明
    status["supported"] = False
    entry = _entry_for(status)
    assert entry["supported"] is True
    assert "checkin_dates" not in entry
    assert entry["sign_in_days"] == days


# --- 账号卡（/ui/api/dumate/accounts）的签到列 ---------------------------------
#
# 2026-10-30 review 抓到：提交说明声称这里补了 checkin_status，实际那版 diff
# 只动了 checkin.py / benefits.py，端点仍只回 total_points/total_times，
# benefits_accounts.js 的签到列拿不到数据源——「算了但没下发」的老毛病又犯一次。
# 这两条把字段钉死在端点上。


def test_accounts_endpoint_ships_checkin_status(monkeypatch):
    """账号卡必须带 checkin_status（今日状态 + 连续天数），否则签到列无数据源。"""
    import asyncio
    from types import SimpleNamespace

    from buddy_proxy.dumate import discovery
    from buddy_proxy.web.ui import channels

    days = [f"2026-10-{d:02d}" for d in range(5, 31)]
    status = dumate_checkin._parse_status_payload(_payload(days))

    monkeypatch.setattr(discovery, "discover", lambda: object())
    monkeypatch.setattr(
        channels, "_dumate_account_info_cached",
        lambda: {
            "bceconsole_authenticated": True,
            "account_id": "acct-1",
            "display_name": "小豆",
            "checkin_total_points": 13500,
            "checkin_total_times": 27,
            "checkin_status": {
                "checked_in": status["checked_in"],
                "claimable": status["claimable"],
                "streak_days": status["streak_days"],
                "daily_credit": status["daily_credit"],
            },
        },
    )
    req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    out = asyncio.run(channels.ui_dumate_accounts(req))
    acct = out["accounts"][0]
    assert acct["checkin_status"]["checked_in"] is True
    assert acct["checkin_status"]["streak_days"] == 26
    assert acct["checkin_status"]["daily_credit"] == 500


def test_account_info_builds_checkin_status_from_single_query(monkeypatch):
    """``_dumate_account_info`` 复用同一次签到查询，不多打一次上游。"""
    from buddy_proxy.dumate import cookies as dumate_cookies
    from buddy_proxy.web.ui import channels

    calls = []

    def _fetch(timeout=20.0):
        calls.append(timeout)
        return dumate_checkin._parse_status_payload(_payload(["2026-10-29", "2026-10-30"]))

    monkeypatch.setattr(dumate_cookies, "resolve_bceconsole_auth", lambda: {"token": "x"})
    monkeypatch.setattr(dumate_checkin, "fetch_checkin_status", _fetch)
    monkeypatch.setattr(dumate_checkin, "fetch_usage_records",
                        lambda **kw: {"consumed_points": 0.0, "total_count": 0})

    info = channels._dumate_account_info()
    assert len(calls) == 1, "签到查询只该打一次，账号卡状态从中派生"
    assert info["checkin_status"]["checked_in"] is True
    assert info["checkin_status"]["streak_days"] == 2
