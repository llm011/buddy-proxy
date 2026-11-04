"""权益到期告警：数据采集 + 后端筛选 + 前端横幅。

背景（2026-10-03 用户需求）：每个账号的套餐 / 资源包 / 签到积分 / 加油包等
各种权益都有到期时间，界面上看不到；用户要「套餐剩余不足 7 天时，在 tab
顶部挂横幅」，后来又加了一条「展示在横幅上的还要再过滤下量，比如大于 300
积分再提醒」。

三块要一起测：
1. **采集**：各通道把到期时刻送进 ``expire_ts``（不是 ``reset_ts``）——
   改错字段横幅要么漏报要么把「5 小时后重置」误报成「快到期」。
2. **筛选**：``benefits._expiring`` 的天数 + 量两条过滤。
3. **渲染**：横幅只在有告警时出现，且「到期」与「重置」文案不串。
"""

from __future__ import annotations

import time
from unittest import mock

import pytest

from buddy_proxy.benefits import (
    EXPIRY_URGENT_DAYS,
    EXPIRY_WARN_DAYS,
    EXPIRY_WARN_MIN_CREDITS,
    QUOTA_LOW_MIN_CREDITS,
    QUOTA_LOW_MIN_PERCENT,
    _expiring,
    _quota_low,
)

NOW = 1_800_000_000.0  # 固定「现在」，测试不跟真实时钟走
DAY = 86400.0


def _entry(pid: str, items: list[dict], name: str | None = None) -> dict:
    """造一个 provider 条目（形状同 snapshot() 的 provider_entries）。"""
    return {
        "id": pid,
        "name": name or pid,
        "checkin": {"supported": False},
        "quota": {"supported": True, "items": items},
    }


def _item(label: str, *, expire_days: float | None, remaining=1000.0,
          unit: str | None = "credit", reset_days: float | None = None) -> dict:
    return {
        "label": label, "used": 0, "total": 1000, "remaining": remaining,
        "percent": 0, "unit": unit,
        "expire_ts": int(NOW + expire_days * DAY) if expire_days is not None else None,
        "reset_ts": int(NOW + reset_days * DAY) if reset_days is not None else None,
    }


# ---------------------------------------------------------------------------
# _expiring：天数过滤
# ---------------------------------------------------------------------------

def test_expiring_picks_within_window_sorted_by_expiry():
    """7 天内到期的都该进来，按到期先后排序。"""
    out = _expiring([
        _entry("qoder", [
            _item("订阅额度", expire_days=3),
            _item("加油包", expire_days=1),
            _item("专属积分", expire_days=6.5),
        ]),
    ], now=NOW)
    assert [e["label"] for e in out] == ["加油包", "订阅额度", "专属积分"]
    assert out[0]["provider"] == "qoder"
    assert out[0]["days_left"] == 1.0


def test_expiring_ignores_beyond_window():
    """7 天及以后的不进横幅；没到期时间的条目直接跳过。"""
    out = _expiring([
        _entry("qoder", [
            _item("还早", expire_days=EXPIRY_WARN_DAYS),       # 恰好 7 天 → 不报
            _item("更早", expire_days=30),
            _item("无到期", expire_days=None),
        ]),
    ], now=NOW)
    assert out == []


def test_expiring_includes_already_expired():
    """已过期的也要报——「已经没了」比「快没了」更该提醒，由前端渲红色。"""
    out = _expiring([_entry("trae", [_item("签到奖励", expire_days=-2)])], now=NOW)
    assert len(out) == 1
    assert out[0]["days_left"] == -2.0
    # 过期项已经有「已过期 N 天」文案，不再挂「4 天内」：urgent 只给
    # 还没过期、但进入 4 天窗口的条目。
    assert out[0]["urgent"] is False


# ---------------------------------------------------------------------------
# _expiring：量过滤（用户要求「大于 300 积分再提醒」）
# ---------------------------------------------------------------------------

def test_expiring_skips_small_credit_remainders():
    """积分类剩余 ≤300 不提醒：每天签到送的 200 分小包会刷屏。

    用 5 天（4 天紧急窗口之外）来测这条——4 天内的小包另有专门规则，
    见 ``test_expiring_urgent_bypasses_credit_amount_filter``。
    """
    out = _expiring([
        _entry("trae", [
            _item("签到奖励", expire_days=5, remaining=200, unit="credit"),
            _item("会员 Pro 包", expire_days=5, remaining=4000, unit="credit"),
        ]),
    ], now=NOW)
    assert [e["label"] for e in out] == ["会员 Pro 包"]


def test_expiring_credit_threshold_is_exclusive():
    """恰好 300 不报（要求是「大于 300」）。边界值单拎出来盯住。

    用 5 天（4 天紧急窗口之外）；4 天内是另一条规则，不受 300 门槛约束。
    """
    assert _expiring([
        _entry("trae", [_item("刚好 300", expire_days=5,
                              remaining=EXPIRY_WARN_MIN_CREDITS, unit="credit")]),
    ], now=NOW) == []
    assert len(_expiring([
        _entry("trae", [_item("300 出头", expire_days=5,
                              remaining=EXPIRY_WARN_MIN_CREDITS + 0.01, unit="credit")]),
    ], now=NOW)) == 1


def test_expiring_non_credit_units_skip_the_amount_filter():
    """非积分类只看天数：mimo 的 remaining 是「还剩几天」（19.5），
    antigravity 是千分制（361.8）——拿它们跟 300 比大小毫无意义。"""
    out = _expiring([
        _entry("mimo", [_item("套餐有效期（天）", expire_days=3,
                              remaining=3.0, unit="day")]),
        _entry("antigravity", [_item("Gemini 组", expire_days=3,
                                     remaining=100.0, unit="permille")]),
        _entry("zcode", [_item("5 小时窗口", expire_days=3,
                               remaining=50, unit="count")]),
    ], now=NOW)
    assert {e["provider"] for e in out} == {"mimo", "antigravity", "zcode"}


def test_expiring_unknown_unit_fails_open():
    """``unit`` 缺失/None 时不做量过滤：新通道忘了填 unit 宁可多提醒一个，
    也别漏掉真到期。"""
    out = _expiring([
        _entry("newchan", [_item("新通道权益", expire_days=2,
                                 remaining=1.0, unit=None)]),
    ], now=NOW)
    assert len(out) == 1


def test_expiring_credit_without_remaining_is_skipped():
    """积分类但拿不到余量（None）→ 不报：无法判断值不值得提醒。"""
    out = _expiring([
        _entry("trae", [_item("权益包", expire_days=2,
                              remaining=None, unit="credit")]),
    ], now=NOW)
    assert out == []


def test_expiring_urgent_bypasses_credit_amount_filter():
    """4 天内到期的小额资源包也要报（用户 2026-11-04 要求）。

    300 门槛是给「还早着呢」的小包降噪的；4 天内作废时，值不值得提醒
    不该再由余量决定——过期就彻底没了。恰好超过 4 天的同额条目仍被门槛
    挡掉，两条规则的分界线要钉死。
    """
    out = _expiring([
        _entry("trae", [
            _item("4 天内小包", expire_days=EXPIRY_URGENT_DAYS - 0.1,
                  remaining=50, unit="credit"),
            _item("刚好超过 4 天", expire_days=EXPIRY_URGENT_DAYS + 0.1,
                  remaining=50, unit="credit"),
            _item("4 天内空包", expire_days=1, remaining=0, unit="credit"),
        ]),
    ], now=NOW)
    assert [e["label"] for e in out] == ["4 天内空包", "4 天内小包"]
    assert all(e["urgent"] for e in out)


def test_expiring_splits_account_and_item_label_for_two_level_list():
    """多账号 label 拆成 ``account`` + ``item_label``，供前端两级展开。

    ``label`` 保留原文（兼容旧调用方），前端优先用 ``item_label``
    和 ``account``；没有账号前缀的通道 account 为空串。
    """
    out = _expiring([
        _entry("codebuddy", [
            _item("CodeBuddy #2 · 资源包", expire_days=1, remaining=0,
                  unit="credit"),
            _item("CodeBuddy #2 · 资源包 #2", expire_days=2, remaining=500,
                  unit="credit"),
        ]),
        _entry("dumate", [
            _item("积分包（login_bonus）", expire_days=1, remaining=0,
                  unit="points"),
        ]),
    ], now=NOW)
    cb = [e for e in out if e["provider"] == "codebuddy"]
    assert cb[0]["account"] == "#2"
    assert cb[0]["item_label"] == "资源包"
    assert cb[0]["label"] == "CodeBuddy #2 · 资源包", "label 保留原文"
    # 名称末尾自带的「#2」不是账号前缀，不能被剥掉
    assert cb[1]["item_label"] == "资源包 #2"
    dm = next(e for e in out if e["provider"] == "dumate")
    assert dm["account"] == ""
    assert dm["item_label"] == "积分包（login_bonus）"


# ---------------------------------------------------------------------------
# _expiring：结构性前提
# ---------------------------------------------------------------------------

def test_expiring_ignores_reset_only_items():
    """只有 ``reset_ts``（周期重置）的条目不算到期——ZCode 的 5 小时窗口、
    antigravity 的 weekly 池天天「快重置」，报成到期就是天天误报。"""
    out = _expiring([
        _entry("zcode", [_item("5 小时窗口", expire_days=None,
                               unit="count", reset_days=0.2)]),
    ], now=NOW)
    assert out == []


def test_expiring_skips_unsupported_and_notice_items():
    """不支持额度的通道、以及混在 items 里的失败说明条都不该进横幅。"""
    out = _expiring([
        {"id": "gemini-cli", "name": "gemini", "quota": {"supported": False}},
        _entry("traepat", [
            # 说明条：remaining 是文案不是数字
            {"label": "PAT 额度查询失败", "used": None, "total": None,
             "remaining": "2/9 个账号查询失败", "percent": None,
             "reset_ts": None},
        ]),
    ], now=NOW)
    assert out == []


def test_expiring_bad_expire_ts_is_skipped_not_crashed():
    """布尔 / 零 / 负数不吃；数字字符串照收（上游偶发给 ``"1793289600"``）。"""
    items = [
        _item("布尔", expire_days=1) | {"expire_ts": True},
        _item("零", expire_days=None) | {"expire_ts": 0},
        _item("负数", expire_days=None) | {"expire_ts": -1},
        _item("非数字串", expire_days=1) | {"expire_ts": "不是时间"},
        _item("空串", expire_days=1) | {"expire_ts": ""},
        # 数字字符串要收：上游给 zcode 的 unit/number 就发过字符串
        _item("数字串", expire_days=1) | {"expire_ts": str(int(NOW + DAY))},
    ]
    out = _expiring([_entry("weird", items)], now=NOW)
    assert [e["label"] for e in out] == ["数字串"]


def test_expiring_garbage_input_does_not_crash():
    """形状完全不对的输入不能炸（函数是纯的、又长在聚合出口上）。

    正常路径由 snapshot() 保证 entry 是 dict，但这里宽松取数是几行代码的事，
    而崩在聚合出口会连带整页 500——代价不对等。
    """
    out = _expiring([
        None, "x", 42,
        {"id": "a"},                       # 没有 quota
        {"id": "b", "quota": None},
        {"id": "c", "quota": "怪值"},
        {"id": "d", "quota": {"supported": True, "items": None}},
        {"id": "e", "quota": {"supported": True, "items": [None, "x", 7]}},
    ], now=NOW)
    assert out == []


def test_expiring_rejects_bool_remaining():
    """``remaining`` 是布尔时不算数：``True`` 是 1 会变成「只剩 1 分」的假数据。"""
    out = _expiring([
        _entry("x", [_item("真", expire_days=1, remaining=True, unit="credit")]),
    ], now=NOW)
    assert out == []


def test_expiring_accepts_numeric_string_remaining():
    """数字字符串的余量照收——上游偶发把数额发成字符串。

    用 5 天避开 4 天紧急窗口，否则 "200" 也会被紧急规则收进来。
    """
    assert len(_expiring([
        _entry("x", [_item("够多", expire_days=5, remaining="500", unit="credit")]),
    ], now=NOW)) == 1
    assert _expiring([
        _entry("x", [_item("太少", expire_days=5, remaining="200", unit="credit")]),
    ], now=NOW) == []


# ---------------------------------------------------------------------------
# 采集端：到期时刻必须进 expire_ts，不能留在 reset_ts
# ---------------------------------------------------------------------------

def test_trae_pack_remaining_uses_credits_amount_as_used(monkeypatch):
    """Trae 权益包补全 remaining——``credits_amount`` 是**已用**不是剩余。

    2026-10-03 实测交叉校验（账号总额度当标尺）：Σlimit=9500.0、
    Σamount=6257.1772，``Σlimit - Σamount`` 与接口自报 remaining（3242.82）
    差 0.00；而「amount=剩余」的假设差 3014.36。这条断言把方向钉死：
    算反的话下面 remaining 会变成 6257.18，横幅就会把「已花光」的包
    报成「还剩一大笔」，正好反了。
    """
    from buddy_proxy.trae import provider as trae_provider

    monkeypatch.setattr(trae_provider, "fetch_ent_usage", lambda region="": {
        "usage_summary": {"total_amount": 9500.0, "consumed_amount": 6257.18,
                          "consumption_ratio": 0.66},
        "user_entitlement_pack_list": [
            {   # 有 usage：已用 4000 → 剩 0（会员包已花光）
                "display_desc": "会员 Pro 连续包月",
                "entitlement_base_info": {
                    "entitlement_id": "pro", "end_time": int(NOW + 1 * DAY),
                    "quota": {"credits_limit": 4000},
                },
                "usage": {"credits_amount": 4000.0},
            },
            {   # usage 缺失 = 真·未消费（不是「未知」），按 0 已用算 → 剩 500
                "display_desc": "每月登录赠送",
                "entitlement_base_info": {
                    "entitlement_id": "monthly_bonus", "end_time": int(NOW + 3 * DAY),
                    "quota": {"credits_limit": 500},
                },
                "usage": {},
            },
        ],
    })
    monkeypatch.setattr(trae_provider.TraeProvider, "_client", None, raising=False)

    out = trae_provider.TraeProvider().quota()
    packs = [i for i in out["items"] if i["label"] != "总额度"]
    by_label = {i["label"]: i for i in packs}

    pro = by_label["会员 Pro 连续包月"]
    assert pro["used"] == 4000.0
    assert pro["remaining"] == 0.0, "已花光的包不能报成还剩 4000"
    bonus = by_label["每月登录赠送"]
    assert bonus["used"] == 0.0
    assert bonus["remaining"] == 500.0
    for it in packs:
        assert it["expire_ts"] is not None, "权益包必须给结构化到期时间"
        assert it["reset_ts"] is None, "到期不是重置"
        assert it["unit"] == "credit"


def test_trae_pack_dedup_keeps_same_named_packs(monkeypatch):
    """同名「签到奖励」不能只留一条：25 条里有 12 条是独立额度。

    早先按名字去重 + 只留前 3 条，界面上剩余积分比实际少一大截。
    """
    from buddy_proxy.trae import provider as trae_provider

    packs = []
    for i, used in enumerate((200.0, 200.0, None, None)):
        packs.append({
            "display_desc": "签到奖励",
            "entitlement_base_info": {
                "entitlement_id": f"checkin_{i}", "end_time": int(NOW + (i + 1) * DAY),
                "quota": {"credits_limit": 200},
            },
            "usage": ({"credits_amount": used} if used is not None else {}),
        })
    monkeypatch.setattr(trae_provider, "fetch_ent_usage", lambda region="": {
        "usage_summary": {"total_amount": 800.0, "consumed_amount": 400.0},
        "user_entitlement_pack_list": packs,
    })

    out = trae_provider.TraeProvider().quota()
    named = [i for i in out["items"] if i["label"] == "签到奖励"]
    assert len(named) == 4, "四条签到奖励是四份额度，不能合并"
    assert [i["remaining"] for i in named] == [0.0, 0.0, 200.0, 200.0]
    # 到期早的排前面
    assert [i["expire_ts"] for i in named] == sorted(i["expire_ts"] for i in named)


def test_trae_pack_duplicate_rows_still_deduped(monkeypatch):
    """上游重复返回同一行（同名字 + 同 id + 同到期日）仍要合并。"""
    from buddy_proxy.trae import provider as trae_provider

    row = {
        "display_desc": "会员 Pro 连续包月",
        "entitlement_base_info": {
            "entitlement_id": "pro", "end_time": int(NOW + 1 * DAY),
            "quota": {"credits_limit": 4000},
        },
        "usage": {"credits_amount": 100.0},
    }
    monkeypatch.setattr(trae_provider, "fetch_ent_usage", lambda region="": {
        "usage_summary": {"total_amount": 4000.0, "consumed_amount": 100.0},
        "user_entitlement_pack_list": [row, dict(row)],
    })
    out = trae_provider.TraeProvider().quota()
    assert len([i for i in out["items"] if i["label"] == "会员 Pro 连续包月"]) == 1


def test_mimo_period_item_uses_expire_ts_not_label():
    """MiMo 的套餐到期走结构化字段，不再拼进 label。

    拼在 label 里（「套餐有效期至 10-23（天）」）前端拿不到值，做不了告警；
    现在横幅列明细时还会和 expire_ts 渲染的日期重复。
    """
    from buddy_proxy.mimo.provider import _period_item

    # _period_item 用真实时钟算已用/剩余，这里也按真实时钟造时间窗：
    # 30 天套餐走到第 10.5 天 → 剩 19.5 天
    end = time.time() + 19.5 * DAY
    items = _period_item({"startTime": end - 30 * DAY, "endTime": end})
    assert len(items) == 1
    it = items[0]
    assert it["expire_ts"] == int(end)
    assert it["unit"] == "day"
    assert it["reset_ts"] is None
    assert "有效期至" not in it["label"], "日期不该再拼进 label（会与横幅重复）"
    assert it["remaining"] == pytest.approx(19.5, abs=0.1)


def test_qoder_quota_items_carry_expire_ts_and_credit_unit():
    """Qoder 的额度只到期不重置：expire_ts 有值、reset_ts 恒 None。"""
    from buddy_proxy.qoder.provider import QoderProvider
    from buddy_proxy.qoder.credentials import Credential
    from buddy_proxy.qoder.config import Region

    provider = QoderProvider(Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    out = provider._format_quota({
        "userType": "personal_professional",
        "expiresAt": int((NOW + 4 * DAY) * 1000),
        "userQuota": {"total": 2000.0, "used": 100.0, "remaining": 1900.0},
        "addOnQuota": {"total": 0.0, "used": 0.0, "remaining": 0.0},
        "dedicatedResourcePackages": [],
    }, Credential(token="t", uid="u"))
    assert out["items"], "至少要有一条额度"
    for it in out["items"]:
        assert it["expire_ts"] == int(NOW + 4 * DAY)
        assert it["reset_ts"] is None
        assert it["unit"] == "credit"


# ---------------------------------------------------------------------------
# _quota_low：余额告急（2026-10-03 用户需求，同日澄清口径）
#
# 「不足 300 credits（或不足 8%）」按量纲分流：credits 渠道（CodeBuddy/Qoder/
# Trae）只看绝对值剩多少；不按 credits 计费的渠道（ZCode/MiMo/antigravity）
# 没有 credits 概念，才用 8% 占比。早先把「或 8%」对 credits 渠道同时生效，
# CodeBuddy 剩 950（占比 7.2%）被报了警，用户指正——绝对值还多，告的什么急。
# ---------------------------------------------------------------------------

def _sum_entry(pid: str, rows: list[tuple[float, float]], name: str | None = None,
               unit: str = "credit") -> dict:
    """sum_items 通道（如 Qoder）：若干 (remaining, total) 的并存额度。"""
    items = [
        _item(f"份{i}", expire_days=None, remaining=rem, unit=unit) | {"total": tot}
        for i, (rem, tot) in enumerate(rows)
    ]
    return {"id": pid, "name": name or pid, "checkin": {"supported": False},
            "quota": {"supported": True, "sum_items": True, "items": items}}


def test_quota_low_sum_items_aggregates_before_judging():
    """sum_items 通道先加总再判定：三份各剩 150/100/30，加总 280 < 300 才是
    该报的口径（Qoder 的并存额度，单看哪份都不是账号总量）。"""
    out = _quota_low([_sum_entry("qoder", [(150, 2000), (100, 400), (30, 2000)])])
    assert len(out) == 1
    assert out[0]["provider"] == "qoder"
    assert out[0]["remaining"] == 280.0
    assert out[0]["unit"] == "credit"


def test_quota_low_healthy_credit_not_reported():
    """credits 渠道余量充足（绝对值过线）不报。"""
    out = _quota_low([
        _sum_entry("qoder", [(1900, 2000), (300, 400), (1800, 2000)]),
        _entry("codebuddy", [_item("合计", expire_days=None,
                                   remaining=5200, unit="credit")]),
    ])
    assert out == []


def test_quota_low_credit_ignores_percent():
    """credits 渠道不做占比判定——防回归锚点：剩 950/13200（7.2%）占比很低
    但绝对值还多，报「余额告急」就是误报（2026-10-03 用户指正的原始场景）。"""
    out = _quota_low([
        _entry("codebuddy", [
            _item("合计", expire_days=None, remaining=950, unit="credit")
            | {"total": 13200},
        ]),
        _entry("x", [_item("大包", expire_days=None, remaining=700,
                           unit="credit") | {"total": 10000}]),
    ])
    assert out == []


def test_quota_low_without_sum_items_takes_first_item_only():
    """Trae 这类「总额度 + 明细包」通道只看第一条：明细是总额度的拆解，
    相加就把同一份额度算两遍（与 quotaHeadSum 同一套取舍）。"""
    entry = _entry("trae", [
        _item("总额度", expire_days=None, remaining=200, unit="credit"),
        _item("会员 Pro 连续包月", expire_days=None, remaining=4000, unit="credit"),
        _item("签到奖励", expire_days=None, remaining=600, unit="credit"),
    ])
    out = _quota_low([entry])
    assert len(out) == 1, "总额度 200/1000 该报"
    assert out[0]["remaining"] == 200.0, "不能把明细包加进来双算"


def test_quota_low_strips_provider_prefix_from_label():
    """带通道名前缀的 label（「Trae #1 · 总额度」）在出口剥掉前缀：横幅拼的是
    「{provider_name} · 余额告急 · {label}」，不剥就出「Trae · … · Trae #1」
    两遍（用户 2026-10-06 反馈：多账号告警看不出是哪个号，补 label 时暴露）。
    顺带锚 credit 通道也带 label——早先只有占比通道拼。"""
    entry = _entry("trae", [
        _item("Trae #1 · 总额度", expire_days=None, remaining=200, unit="credit")
        | {"total": 1000},
    ], name="Trae")
    out = _quota_low([entry])
    assert len(out) == 1
    assert out[0]["label"] == "#1 · 总额度"
    assert out[0]["provider_name"] == "Trae"


def test_quota_low_non_credit_uses_percent():
    """非 credits 渠道按占比判定（它们没有 credits 概念）：antigravity 千分制
    剩 3.05%、ZCode 窗口剩 5% 都该报；MiMo 剩 15% 不报。"""
    out = _quota_low([
        _entry("antigravity", [_item("Gemini 组", expire_days=None,
                                     remaining=30.5, unit="permille")
                               | {"total": 1000}]),
        _entry("zcode", [_item("5 小时窗口", expire_days=None,
                               remaining=5, unit="count") | {"total": 100}]),
        _entry("mimo", [_item("本周额度", expire_days=None,
                              remaining=15, unit="percent") | {"total": 100}]),
    ])
    assert {e["provider"] for e in out} == {"antigravity", "zcode"}, \
        f"MiMo 15% 不该报: {out}"
    zc = next(e for e in out if e["provider"] == "zcode")
    assert zc["label"] == "5 小时窗口", "非 credits 条目要带 label（哪个窗口告急）"
    assert zc["percent_left"] == 5.0


def test_quota_low_credit_boundary_is_exclusive():
    """credits 恰好 300 不算「不足」不报，差一点才报。"""
    assert _quota_low([_sum_entry("x", [(QUOTA_LOW_MIN_CREDITS, 1000)])]) == []
    assert len(_quota_low([_sum_entry("y", [(QUOTA_LOW_MIN_CREDITS - 1, 1000)])])) == 1


def test_quota_low_percent_boundary_is_exclusive():
    """非 credits 恰好 8% 不报（占比分支用 permille 大 total，纯看占比）。"""
    assert _quota_low([_entry("x", [_item("恰 8%", expire_days=None, remaining=80,
                                          unit="permille") | {"total": 1000}])]) == []
    assert len(_quota_low([_entry("y", [_item("7.9%", expire_days=None, remaining=79,
                                              unit="permille") | {"total": 1000}])])) == 1


def test_quota_low_items_without_numbers_are_skipped():
    """remaining/total 缺一个就不参与判定；通道内没有可用条目就不报——
    口径与 quotaHeadSum 的 usable 一致，无法判断的余额不告警。"""
    out = _quota_low([
        _entry("a", [
            _item("只给 total", expire_days=None, remaining=None, unit="credit")
            | {"total": 300},
            _item("total 缺失", expire_days=None, remaining=100, unit="credit")
            | {"total": None},
        ]),
        _entry("b", [_item("全缺", expire_days=None, remaining=None,
                           unit="credit") | {"total": None}]),
    ])
    assert out == []


def test_quota_low_skips_unsupported_and_garbage_input():
    """不支持额度的通道、以及形状不对的输入不能炸（函数长在聚合出口上，
    崩了会连带整页 500）。"""
    out = _quota_low([
        None, "x", 42,
        {"id": "a"},
        {"id": "b", "quota": None},
        {"id": "c", "quota": {"supported": False,
                              "items": [_item("剩 0", expire_days=None,
                                              remaining=0, unit="credit")]}},
        {"id": "d", "quota": {"supported": True, "items": None}},
        {"id": "e", "quota": {"supported": True, "items": [None, "x", 7]}},
        {"id": "f", "quota": {"supported": True, "items": [
            _item("布尔", expire_days=None, remaining=True, unit="credit"),
        ]}},
    ])
    assert out == []


def test_quota_low_sorted_tightest_first():
    """剩得最少的排最前，横幅第一行就是最烧干的通道（credits 按占比、
    非 credits 按占比，统一占比升序）。"""
    out = _quota_low([
        _sum_entry("wide", [(250, 10000)]),    # 2.5%，靠 <300 触发
        _sum_entry("dry", [(50, 1000)]),       # 5% credits，触发 <300
        _entry("ag", [_item("Gemini 组", expire_days=None, remaining=30,
                            unit="permille") | {"total": 1000}]),  # 3%
    ])
    assert [e["provider"] for e in out] == ["wide", "ag", "dry"]


def test_quota_low_total_zero():
    """total=0：credits 通道剩 0 仍是告急（绝对值判定成立），占比拿不到置
    None；非 credits 通道占比是唯一依据，拿不到就不报——不能拿 0/0 报警。"""
    out = _quota_low([_sum_entry("weird", [(0, 0)])])
    assert len(out) == 1
    assert out[0]["remaining"] == 0.0
    assert out[0]["percent_left"] is None
    assert _quota_low([_sum_entry("weird2", [(0, 0)], unit="permille")]) == []


def test_quota_low_bool_remaining_rejected_numeric_string_accepted():
    """布尔不算数（``True`` 是 1 会变「只剩 1 分」的假数据）；数字字符串照收
    （上游偶发把数额发成字符串）。"""
    out = _quota_low([
        _sum_entry("bools", [(True, 1000)]),
        _sum_entry("strs", [("250", "1000")]),
    ])
    assert [e["provider"] for e in out] == ["strs"]
    assert out[0]["remaining"] == 250.0


# ---------------------------------------------------------------------------
# 卡片级刷新后端：invalidate_quota + POST /ui/api/benefits/refresh
# ---------------------------------------------------------------------------

def test_invalidate_quota_deletes_provider_prefix_only():
    """按前缀删（键带 epoch 的通道键名不固定），不波及别的通道和 checkin 状态。"""
    from buddy_proxy.benefits import BenefitsManager

    m = BenefitsManager.__new__(BenefitsManager)  # 跳过 init：只测缓存字典操作
    m._cache = {
        "quota:antigravity:a#0": (1.0, {}),
        "quota:antigravity:a#0,b#1": (2.0, {}),
        "quota:antigravitory-x": (2.5, {}),   # 前缀相似但不是同一通道
        "quota:qoder": (3.0, {}),
        "checkin:antigravity": (4.0, {}),
    }
    assert m.invalidate_quota("antigravity") == 2
    assert "quota:antigravitory-x" in m._cache, "前缀相似的其他通道不能误伤"
    assert "quota:qoder" in m._cache
    assert "checkin:antigravity" in m._cache, "签到状态有自己的翻转判定，不动"
    assert m.invalidate_quota("never-cached") == 0


def test_refresh_api_invalidates_and_returns_snapshot(monkeypatch):
    """POST /ui/api/benefits/refresh：作废目标通道缓存并返回新快照。"""
    import os
    from types import SimpleNamespace

    from buddy_proxy import __main__ as m
    from buddy_proxy.core import state as st
    from fastapi.testclient import TestClient

    calls: list[str] = []

    class FakeManager:
        def provider_ids(self):
            return {"antigravity"}

        def invalidate_quota(self, pid):
            calls.append(pid)
            return 1

        async def snapshot(self, disabled_providers=None):
            calls.append(f"disabled={sorted(disabled_providers or set())}")
            return {"providers": [], "refreshed": True}

    monkeypatch.setattr(st, "proxy_state", SimpleNamespace(benefits=FakeManager()))
    monkeypatch.setenv("BUDDY_PROXY_ADMIN_OPEN", "1")  # TestClient 来源不是 127.0.0.1
    r = TestClient(m.app).post("/ui/api/benefits/refresh", json={"provider": "antigravity"})
    assert r.status_code == 200
    assert r.json()["refreshed"] is True
    assert calls == ["antigravity", "disabled=[]"]


def test_refresh_api_rejects_unknown_provider(monkeypatch):
    """未知通道一律 404，不制造无效缓存键，也不触发快照查询。"""
    from types import SimpleNamespace

    from buddy_proxy import __main__ as m
    from buddy_proxy.core import state as st
    from fastapi.testclient import TestClient

    boom = mock.MagicMock(side_effect=AssertionError("不该触发作废或快照"))
    manager = SimpleNamespace(
        provider_ids=lambda: {"trae", "qoder"},
        invalidate_quota=boom,
        invalidate_checkin=boom,
        snapshot=boom,
    )
    monkeypatch.setattr(st, "proxy_state", SimpleNamespace(benefits=manager))
    monkeypatch.setenv("BUDDY_PROXY_ADMIN_OPEN", "1")
    r = TestClient(m.app).post(
        "/ui/api/benefits/refresh", json={"provider": "not-real"})
    assert r.status_code == 404
    assert "未知 provider" in r.text
    boom.assert_not_called()


def test_refresh_api_requires_provider(monkeypatch):
    """缺 provider 一律 400，不打上游。"""
    from types import SimpleNamespace

    from buddy_proxy import __main__ as m
    from buddy_proxy.core import state as st
    from fastapi.testclient import TestClient

    boom = mock.MagicMock(side_effect=AssertionError("不该触发作废"))
    monkeypatch.setattr(st, "proxy_state",
                        SimpleNamespace(benefits=SimpleNamespace(invalidate_quota=boom)))
    monkeypatch.setenv("BUDDY_PROXY_ADMIN_OPEN", "1")
    r = TestClient(m.app).post("/ui/api/benefits/refresh", json={})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# snapshot(disabled_providers)：整通道停用的呈现（2026-10-06 模型页开关）
# ---------------------------------------------------------------------------
def test_snapshot_marks_disabled_providers_and_filters_alerts(tmp_path, monkeypatch):
    """停用通道：entry 带 disabled 标记、不再上告警横幅；额度数据仍照带——
    前端只是隐藏展示，开关一开原样回来，不用重查上游。"""
    import asyncio
    from types import SimpleNamespace

    from buddy_proxy.benefits import BenefitsManager

    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(tmp_path / "settings.json"))

    class P:
        id = "p1"
        name = "P1"
        supports_checkin = False

        def quota(self):
            return {"supported": True, "items": [
                {"label": "P1 · 总额度", "used": 800, "total": 1000, "remaining": 200,
                 "percent": 80, "unit": "credit"}]}

    mgr = BenefitsManager(tmp_path / "checkin.jsonl",
                          SimpleNamespace(providers={"p1": P()}))
    snap = asyncio.run(mgr.snapshot({"p1"}))
    # _providers() 会把 codebuddy 塞在首位，按 id 取目标条目
    entry = next(e for e in snap["providers"] if e["id"] == "p1")
    assert entry["disabled"] is True
    assert entry["quota"]["supported"] is True, "数据照带"
    assert snap["low_quota"] == [] and snap["expiring"] == [], "停用通道不上告警"

    snap2 = asyncio.run(mgr.snapshot(set()))
    entry2 = next(e for e in snap2["providers"] if e["id"] == "p1")
    assert entry2["disabled"] is False
    assert len(snap2["low_quota"]) == 1, "未停用时 200 < 300 照常告警"
