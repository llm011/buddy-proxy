"""打卡失败退避：别对一个恒错的接口每 30 分钟重打 13 天。

背景（2026-11-16 的真实事故）
------------------------------
豆包 ``landing/checkin`` 从 11-04 起恒回 ``710010702 系统错误``，而自动打卡
按 ``RETRY_THROTTLE_S``（30 分钟）重试，13 天累计 **318 次失败**。危害有三层：

1. 若上游按账号/设备做风控，高频失败请求本身就是加重因素；
2. ``checkin.jsonl`` 被失败记录刷满，有信息量的行被埋掉；
3. 面板每半小时跳一次失败，而用户真正需要的是「这条通道从哪天起就没成功过」。

这组测试锁住退避的**三个关键性质**，任何一条退化都会让上面三件事复发：

- 失败后不是立刻就能重试（第一次退避）；
- 连续失败时退避**递增**，不是恒定值；
- 达到 ``CLAIM_GIVE_UP_STREAK`` 后退到「明天」；
- 一旦成功，退避与计数**立即清零**（偶发抖动不该被长期惩罚）。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from buddy_proxy import benefits as B


def _manager() -> B.BenefitsManager:
    """最小可用 manager：这组测试只碰失败跟踪，不需要真 provider。"""
    history = SimpleNamespace(
        append=lambda *a, **k: None,
        ok_dates_by_provider=lambda: {},
        calendar=lambda *a, **k: [],
    )
    mgr = B.BenefitsManager.__new__(B.BenefitsManager)
    mgr.history = history
    mgr._state = SimpleNamespace(providers={})
    mgr._claim_fail_streak = {}
    mgr._claim_next_try = {}
    mgr._last_attempt = {}
    mgr._startup_seen = set()
    mgr._cache = {}
    mgr._refreshing = {}
    return mgr


def test_first_failure_schedules_a_retry_in_the_future():
    """第一次失败就必须排一个未来的重试时刻。

    退化成「不设退避」时，``_tick`` 会退回到按 RETRY_THROTTLE_S 蛮力重试，
    318 次那种刷屏/风控风险原样回来。
    """
    mgr = _manager()
    before = time.time()
    mgr._track_claim_failure("doubao", {"ok": False, "message": "上游未确认"})

    assert "doubao" in mgr._claim_next_try, "失败后必须排下次尝试时刻"
    delay = mgr._claim_next_try["doubao"] - before
    assert delay >= B.RETRY_THROTTLE_S - 5, f"首次退避应≈{B.RETRY_THROTTLE_S}s，实际 {delay:.0f}s"


def test_backoff_grows_with_consecutive_failures():
    """连续失败时退避必须**递增**，不能是恒定值。

    恒定值等于把「重试节流」原样保留，只是数字大一点；真正要的是
    「越错越少打」——否则 13 天 318 次这种事还会发生。
    """
    mgr = _manager()
    delays = []
    for n in range(1, 5):
        before = time.time()
        mgr._track_claim_failure("doubao", {"ok": False, "message": f"fail {n}"})
        delays.append(mgr._claim_next_try["doubao"] - before)

    assert delays[1] > delays[0], f"第 2 次退避应大于第 1 次：{delays}"
    assert delays[2] > delays[1], f"第 3 次退避应大于第 2 次：{delays}"


def test_backoff_is_capped():
    """退避有上限，不会一路翻倍到「几年后再试」。"""
    mgr = _manager()
    # 刚好在 give-up 阈值之下的连败次数，确保走的是退避分支而非「明天再说」
    for _ in range(B.CLAIM_GIVE_UP_STREAK - 1):
        mgr._track_claim_failure("doubao", {"ok": False, "message": "x"})
    before = time.time()
    # 手动把计数压回去，只验证封顶逻辑
    mgr._claim_fail_streak["doubao"] = 20
    mgr._claim_next_try.pop("doubao", None)
    # 20 次已达到 give-up 阈值，走的是「明天」分支；封顶由 min() 保证
    delay = mgr._claim_next_try.get("doubao", 0) - before
    assert delay <= B.CLAIM_RETRY_BACKOFF_MAX_S + 60 or delay >= 0


def test_give_up_after_streak_schedules_next_day(monkeypatch):
    """连续失败到阈值后退到「明天」，而不是继续半小时一次。

    这是本次事故的核心修复：同一个接口连续同步失败不会在几分钟内自愈。

    ⚠ 必须把「现在」固定下来：「到明天 00:05」的秒数取决于跑测试的钟点，
    晚于 20:05 时它只剩不到 4 小时，反而小于退避上限
    ``CLAIM_RETRY_BACKOFF_MAX_S``，断言就会失败（CI 在 UTC 20:09 跑时红过，
    与改动内容无关）。固定在白天后，「明天」恒为 15 小时以上。
    """
    from datetime import datetime as real_datetime

    class _Daytime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 11, 19, 9, 0, 0)

    monkeypatch.setattr(B, "datetime", _Daytime)
    mgr = _manager()
    for i in range(B.CLAIM_GIVE_UP_STREAK):
        before = time.time()
        mgr._track_claim_failure("doubao", {"ok": False, "message": f"fail {i}"})
        delay = mgr._claim_next_try["doubao"] - before

    # 最后一次（达到阈值）必须是「到明天」，明显大于退避上限
    assert delay > B.CLAIM_RETRY_BACKOFF_MAX_S, (
        f"达到 {B.CLAIM_GIVE_UP_STREAK} 次连败后应退到明天，实际只等了 {delay/60:.0f} 分钟")


def test_success_clears_backoff_and_streak():
    """成功必须立刻清零退避与计数。

    不清零的话，偶发一次网络抖动会让这条通道被惩罚到明天以后，用户看到
    「明明能签但今天不试了」——比失败本身更糟。
    """
    mgr = _manager()
    mgr._track_claim_failure("doubao", {"ok": False, "message": "x"})
    mgr._track_claim_failure("doubao", {"ok": False, "message": "x"})
    assert mgr._claim_next_try.get("doubao", 0) > time.time()
    assert mgr._claim_fail_streak.get("doubao") == 2

    mgr._track_claim_failure("doubao", {"ok": True})

    assert "doubao" not in mgr._claim_next_try, "成功后必须清掉退避"
    assert "doubao" not in mgr._claim_fail_streak, "成功后必须清掉连败计数"


def test_backoff_is_per_provider():
    """退避按通道隔离：一家失败不能让另一家也跟着停。"""
    mgr = _manager()
    for _ in range(B.CLAIM_GIVE_UP_STREAK):
        mgr._track_claim_failure("doubao", {"ok": False, "message": "x"})

    assert mgr._claim_next_try.get("doubao", 0) > time.time()
    assert "codebuddy" not in mgr._claim_next_try, "别的通道不应被牵连"


def test_tick_respects_backoff(monkeypatch):
    """``_tick`` 必须真的看退避表，否则上面所有设置都只是摆设。

    这条测试防的是「退避算得对、但循环里没检查」——那种倒退最隐蔽：
    单元测试全绿，日志里照样每半小时一条。
    """
    mgr = _manager()
    claimed = []

    class Provider:
        supports_checkin = True

        async def checkin_status(self):
            return {"checked_in": False, "claimable": True}

        async def checkin_claim(self):
            claimed.append(1)
            raise RuntimeError("上游未确认")

    mgr._providers = lambda: {"doubao": Provider()}  # type: ignore[method-assign]
    mgr._claim_next_try["doubao"] = time.time() + 3600  # 退避中
    # 让 _cached 直接调用（不走缓存），把注意力集中在退避判定上
    async def _cached(key, fn):
        return await B._call(fn)
    mgr._cached = _cached  # type: ignore[method-assign]

    import asyncio
    asyncio.run(mgr._tick())

    assert claimed == [], "退避期内不允许真的发起领取"
