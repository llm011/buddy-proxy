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
