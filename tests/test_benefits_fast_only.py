"""``fast=1`` 首屏**绝不触网**的契约（2026-11-24 额度页卡死修复）。

背景：管理页首屏走 ``/ui/api/benefits?fast=1``，设计意图是「有缓存就回缓存，
没缓存就标 loading 让前端拆子请求」。旧实现只判了 ``key not in self._cache``，
于是另外三种情况仍然会**同步**打上游：

1. 缓存里是**失败结果**（``_is_failure``）→ ``_cached`` 走同步重查；
2. 缓存里的快照**已翻转**（``_state_flipped``，next_ts 已过）→ 同上；
3. 失败结果的短 TTL（30s）已过 → 同上。

单通道等满 ``QUERY_TIMEOUT_S``（12s），多家叠加就是用户报的「额度页卡死」。
极端案例：traepat 换 token 的两个端点都是内网地址（cloud.bytedance.net /
ide.byted.org），本机不可达时单账号换签实测 80s，10 个账号轮一遍远超页面
忍耐度——而国产模型走 public 网关本来是通的，全被这个前置步骤拖死。

这批测试钉的是「无论缓存处于哪种状态，fast_only 的耗时都必须 ≈0」。
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from buddy_proxy import benefits as bm
from buddy_proxy.benefits import BenefitsManager


class _SlowProvider:
    """quota 会睡 ``delay`` 秒的 provider；支持签到时可另注入签到状态。"""

    supports_checkin = False

    def __init__(self, pid="fake", delay=5.0, quota=None, checkin=None):
        self.id = pid
        self.name = pid
        self.delay = delay
        self._quota = quota if quota is not None else {"items": [], "level": None}
        self._checkin = checkin
        self.quota_calls = 0
        self.checkin_calls = 0

    def quota(self):
        self.quota_calls += 1
        time.sleep(self.delay)
        return self._quota

    def checkin_status(self):
        self.checkin_calls += 1
        time.sleep(self.delay)
        return self._checkin


def _manager(providers):
    return BenefitsManager("/dev/null", SimpleNamespace(providers=providers))


def _entry(snap, pid):
    return next(e for e in snap["providers"] if e["id"] == pid)


def test_fast_only_does_not_block_on_cold_cache():
    """冷缓存：标 loading 立刻返回，绝不等上游那 5 秒。"""

    async def run():
        p = _SlowProvider(delay=5.0)
        mgr = _manager({})
        mgr._providers = lambda: {"fake": p}
        t0 = time.monotonic()
        snap = await mgr.snapshot(fast_only=True)
        elapsed = time.monotonic() - t0
        e = _entry(snap, "fake")
        assert e.get("loading") is True, e
        assert elapsed < 0.5, f"冷缓存首屏不该等上游（耗时 {elapsed:.2f}s）"
        # 后台预热确实排上了（否则前端拆子请求也拿不到数据）
        assert mgr._refreshing, "应排后台预热任务"

    asyncio.run(run())


def test_fast_only_does_not_block_on_failure_cache():
    """**失败结果在缓存里**：这是旧实现的漏网之鱼——键存在，却仍同步重查。

    traepat 换 token 失败就是这条路径：缓存里躺着 ``{"error": ...}``，首屏
    每个这样的通道各等 12s。
    """

    async def run():
        p = _SlowProvider(delay=5.0)
        mgr = _manager({})
        mgr._providers = lambda: {"fake": p}
        # 预置一条**新鲜**的失败缓存（30s TTL 内，旧实现仍会同步重查）
        mgr._cache["quota:fake"] = (time.time(), {"error": "上游不可达"})
        t0 = time.monotonic()
        snap = await mgr.snapshot(fast_only=True)
        elapsed = time.monotonic() - t0
        e = _entry(snap, "fake")
        assert elapsed < 0.5, f"失败缓存不该触发同步重查（耗时 {elapsed:.2f}s）"
        # 旧值照旧下发：比空白卡片有信息量
        assert "上游不可达" in str(e["quota"].get("error")), e["quota"]
        assert not e.get("loading"), "有旧值可回就不该标 loading"

    asyncio.run(run())


def test_fast_only_does_not_block_on_flipped_snapshot():
    """**已翻转快照**：``next_ts`` 已过时 ``_cached`` 也会同步重查。"""

    async def run():
        p = _SlowProvider(delay=5.0)
        mgr = _manager({})
        mgr._providers = lambda: {"fake": p}
        now = time.time()
        # next_ts 刚过（在 FLIP_GRACE_S 窗口内）→ _state_flipped 判真
        mgr._cache["quota:fake"] = (now, {"items": [], "next_ts": now - 10})
        assert bm._state_flipped(mgr._cache["quota:fake"][1], now), "夹具应触发翻转"
        t0 = time.monotonic()
        snap = await mgr.snapshot(fast_only=True)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"已翻转快照不该同步重查（耗时 {elapsed:.2f}s）"
        e = _entry(snap, "fake")
        assert not e.get("loading"), "有旧值可回就不该标 loading"

    asyncio.run(run())


def test_fast_only_does_not_block_on_expired_failure_ttl():
    """失败结果过了短 TTL（30s）：旧实现同样同步重查。"""

    async def run():
        p = _SlowProvider(delay=5.0)
        mgr = _manager({})
        mgr._providers = lambda: {"fake": p}
        mgr._cache["quota:fake"] = (
            time.time() - bm.FAILURE_TTL_S - 1, {"error": "旧错误"})
        t0 = time.monotonic()
        snap = await mgr.snapshot(fast_only=True)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"过期失败缓存不该同步重查（耗时 {elapsed:.2f}s）"
        e = _entry(snap, "fake")
        assert "旧错误" in str(e["quota"].get("error")), e["quota"]

    asyncio.run(run())


def test_fast_only_does_not_block_on_slow_checkin():
    """签到侧同一条规矩：慢通道的 checkin_status 也不许拖住首屏。"""

    async def run():
        p = _SlowProvider(delay=5.0, checkin={"checked_in": False})
        p.supports_checkin = True
        mgr = _manager({})
        mgr._providers = lambda: {"fake": p}
        t0 = time.monotonic()
        snap = await mgr.snapshot(fast_only=True)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"签到查询不该拖住首屏（耗时 {elapsed:.2f}s）"
        e = _entry(snap, "fake")
        assert e.get("loading") is True, e

    asyncio.run(run())


def test_fast_only_serves_fresh_cache_without_upstream():
    """热缓存：直接回值，既不等上游也不排后台任务（不多打一次）。"""

    async def run():
        p = _SlowProvider(delay=5.0)
        mgr = _manager({})
        mgr._providers = lambda: {"fake": p}
        mgr._cache["quota:fake"] = (time.time(), {"items": ["v1"], "level": None})
        t0 = time.monotonic()
        snap = await mgr.snapshot(fast_only=True)
        elapsed = time.monotonic() - t0
        e = _entry(snap, "fake")
        assert e["quota"]["items"] == ["v1"], e["quota"]
        assert elapsed < 0.2, f"热缓存应瞬间返回（耗时 {elapsed:.2f}s）"
        assert not mgr._refreshing, "新鲜缓存不该触发后台刷新"
        assert p.quota_calls == 0, "不该打上游"

    asyncio.run(run())


def test_non_fast_path_still_blocks_and_refreshes():
    """反向钉子：**不带** fast 时仍走原语义（同步查、能拿到新值）。

    防止「为了让首屏快」把默认路径也改成只读缓存——那样强刷/首次访问会永远
    拿不到数据。
    """

    async def run():
        p = _SlowProvider(delay=0.05, quota={"items": ["fresh"], "level": None})
        mgr = _manager({})
        mgr._providers = lambda: {"fake": p}
        snap = await mgr.snapshot()  # fast_only=False
        e = _entry(snap, "fake")
        assert e["quota"]["items"] == ["fresh"], e["quota"]
        assert p.quota_calls == 1, "默认路径必须真查一次"

    asyncio.run(run())


def test_fast_only_fresh_failure_cache_does_not_hammer_upstream():
    """新鲜的失败缓存**不许**每轮都重查上游。

    ``_peek_or_kick`` 第一版无条件 ``_kick_refresh``，于是每次首屏轮询都会去打
    一遍已经挂掉的上游——失败通道越慢，轮询把它打得更慢（实测 3 轮 = 3 次上游
    请求）。失败结果有 ``FAILURE_TTL_S``（30s）短 TTL，窗口内应当只查一次，
    判据必须与 ``_cached`` 完全一致。
    """

    async def run():
        p = _SlowProvider(delay=0.0)
        p._quota = {"error": "上游不可达"}
        mgr = _manager({})
        mgr._providers = lambda: {"fake": p}
        mgr._cache["quota:fake"] = (time.time(), {"error": "上游不可达"})
        for _ in range(3):
            await mgr.snapshot(fast_only=True)
            await asyncio.gather(*mgr._refreshing.values(), return_exceptions=True)
        assert p.quota_calls == 0, (
            f"30s 短 TTL 内不该重查，实际打了 {p.quota_calls} 次上游")
        # 过期后才允许刷一次
        mgr._cache["quota:fake"] = (
            time.time() - bm.FAILURE_TTL_S - 1, {"error": "上游不可达"})
        await mgr.snapshot(fast_only=True)
        assert mgr._refreshing, "过期失败缓存应排后台刷新"

    asyncio.run(run())
