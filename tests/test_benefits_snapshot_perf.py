"""snapshot 的三个性能/鲁棒性语义（2026-10-11 benefits 卡顿修复）：

1. **通道间并发**：串行时整页 = 各通道之和（实测 15-27s），并发后 = 最慢一家。
2. **stale-while-revalidate**：缓存过期先回旧值、后台刷新，页面不再整页等上游。
3. **单查询硬超时**：provider 自己的超时失灵（实测单键挂过 930s）时，
   ``QUERY_TIMEOUT_S`` 兜底把这次查询按失败短缓存，页面照常出。

运行：
    .venv/bin/python -m pytest tests/test_benefits_snapshot_perf.py -v
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from buddy_proxy import benefits as bm
from buddy_proxy.benefits import BenefitsManager


class _FakeProvider:
    """最小 provider：quota 可注入延迟/返回值/异常；不支持签到。"""

    id = "fake"
    name = "Fake"
    supports_checkin = False

    def __init__(self, pid="fake", delay=0.0, results=None):
        self.id = pid
        self.name = pid
        self.delay = delay
        self.results = list(results or [])
        self.calls = 0

    def quota(self):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.results:
            item = self.results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return {"items": [], "level": None}


def _manager(providers):
    return BenefitsManager("/dev/null", SimpleNamespace(providers=providers))


def test_snapshot_queries_providers_concurrently():
    """并发：两家各睡 0.4s，整页应 ≈0.4s（串行至少 0.8s）。"""

    async def run():
        fakes = {"slow1": _FakeProvider("slow1", delay=0.4),
                 "slow2": _FakeProvider("slow2", delay=0.4)}
        mgr = _manager({})
        # 摘掉默认 codebuddy 通道，计时不受真实网络行为影响
        mgr._providers = lambda: dict(fakes)
        t0 = time.monotonic()
        snap = await mgr.snapshot()
        return time.monotonic() - t0, snap

    elapsed, snap = asyncio.run(run())
    ids = [p["id"] for p in snap["providers"]]
    assert set(ids) == {"slow1", "slow2"}
    assert elapsed < 0.7, f"通道查询没有并发（耗时 {elapsed:.2f}s）"


def test_snapshot_returns_stale_and_refreshes_in_background():
    """SWR：过期缓存先回旧值（不等上游），后台任务刷完下一轮见新值。"""

    async def run():
        p = _FakeProvider(results=[{"items": ["v1"], "level": None},
                                   {"items": ["v2"], "level": None}])
        mgr = _manager({"fake": p})
        snap1 = await mgr.snapshot()
        got1 = next(e for e in snap1["providers"] if e["id"] == "fake")
        assert got1["quota"]["items"] == ["v1"]  # 冷缓存同步查 → v1

        # 人为把缓存推过期（比等 300s 快）
        ts, data = mgr._cache["quota:fake"]
        mgr._cache["quota:fake"] = (ts - bm.SNAPSHOT_TTL_S - 1, data)

        t0 = time.monotonic()
        snap2 = await mgr.snapshot()
        elapsed = time.monotonic() - t0
        got2 = next(e for e in snap2["providers"] if e["id"] == "fake")
        # 过期缓存：立刻拿旧值，不阻塞
        assert got2["quota"]["items"] == ["v1"], "过期缓存应先回旧值"
        assert elapsed < 0.1, f"过期缓存不该同步等上游（耗时 {elapsed:.2f}s）"

        # 后台刷完 → 新值进缓存，再取就是 v2
        await asyncio.gather(*mgr._refreshing.values())
        snap3 = await mgr.snapshot()
        got3 = next(e for e in snap3["providers"] if e["id"] == "fake")
        assert got3["quota"]["items"] == ["v2"]
        assert p.calls == 2, "后台刷新应再打一次上游（共 2 次）"

    asyncio.run(run())


def test_query_timeout_bounds_hang_and_short_caches_failure(monkeypatch):
    """超时兜底：上游挂死时按失败返回，不拖整页；失败短缓存 30s。"""

    async def run():
        monkeypatch.setattr(bm, "QUERY_TIMEOUT_S", 0.2)
        p = _FakeProvider(delay=5.0)  # 远超 0.2s 兜底线的「挂起」
        mgr = _manager({"fake": p})
        t0 = time.monotonic()
        snap = await mgr.snapshot()
        elapsed = time.monotonic() - t0
        got = next(e for e in snap["providers"] if e["id"] == "fake")
        assert elapsed < 2.0, f"挂起的通道该被 {0.2}s 兜底截断（耗时 {elapsed:.2f}s）"
        assert "超时" in str(got["quota"].get("error")), got["quota"]
        # 失败短缓存：窗口期内再查不打上游（底层线程仍在跑，结果丢弃）
        before = p.calls
        snap2 = await mgr.snapshot()
        assert p.calls == before
        ts, data = mgr._cache["quota:fake"]
        assert bm._is_failure(data)

    asyncio.run(run())


def test_kick_refresh_dedupes_inflight_tasks():
    """同键在飞任务去重：连续两次触发只查一次。"""

    async def run():
        mgr = _manager({})
        calls: list[str] = []

        async def fake_query(key, fn=None, *args):
            calls.append(key)
            await asyncio.sleep(0.1)
            return {"new": key}

        # 打桩 _query：_kick_refresh 调 self._query(key, fn)，实例属性可遮蔽
        mgr._query = fake_query

        mgr._kick_refresh("quota:a", lambda: None)
        mgr._kick_refresh("quota:a", lambda: None)
        mgr._kick_refresh("quota:b", lambda: None)
        assert sorted(mgr._refreshing) == ["quota:a", "quota:b"], "同键应去重"
        await asyncio.gather(*mgr._refreshing.values())
        assert sorted(calls) == ["quota:a", "quota:b"], "同键只查一次"
        assert mgr._refreshing == {}, "完成后应从在飞表摘除"

    asyncio.run(run())


def test_stale_while_revalidate_skips_failure_results(monkeypatch):
    """失败结果过期不走 SWR：同步重查（「30s 后自愈」语义，与
    test_trae_pat_failover 的 short_ttl 测试同一条钉子；先回旧错误再后台刷
    的话，short-TTL 自愈承诺就落空了——CI 在 3.10 上抓到过这个 race）。"""

    async def run():
        calls: list[int] = []

        def boom():
            calls.append(1)
            raise RuntimeError("offline")

        mgr = _manager({})
        first = await mgr._cached("k", boom)
        assert first["error"] == "offline"
        assert len(calls) == 1
        # 把失败缓存推过 FAILURE_TTL_S → 必须同步重查（而不是回旧错误+后台）
        ts, data = mgr._cache["k"]
        mgr._cache["k"] = (ts - bm.FAILURE_TTL_S - 1, data)
        second = await mgr._cached("k", boom)
        assert second["error"] == "offline"
        assert len(calls) == 2, "失败过期要同步重查，短 TTL 才能兑现自愈"
        assert not mgr._refreshing, "失败路径不该留后台任务"

    asyncio.run(run())
