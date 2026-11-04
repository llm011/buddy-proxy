"""豆包通道「测试」按钮的跨事件循环锁事故（2026-11-03 用户报障）。

现象：豆包通道点模型「测试」，返回
``<asyncio.locks.Lock object at 0x... [unlocked, waiters:1]> is bound to a
different event loop``。

成因：``DoubaoProvider`` 是进程级单例，锁在 ``__init__`` 里建一次；而它会被两个
循环用到——

- 签到：``benefits`` 用 ``asyncio.to_thread`` 调同步的 ``checkin_status``，里头
  ``_run_checkin`` 再 ``asyncio.run(...)``，拿到的是**一次性循环**；
- 转发 / 「测试」：主循环（uvicorn）。

``asyncio.Lock`` 第一次被用就记住那个循环（``_LoopBoundMixin._get_loop``），之后
另一个循环进来直接 RuntimeError。谁先跑谁赢——签到先跑过，窗口签到的下一次
「测试」就必炸。这里锁死「签到循环 → 主循环」这条真实路径 + ``LoopSafeLock``
本身的两条语义。
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from buddy_proxy.asyncutil import LoopSafeLock
from buddy_proxy.doubao.provider import DoubaoProvider


def _run_in_worker(fn) -> None:
    """在 worker 线程里起一次性循环跑（复刻 to_thread + asyncio.run 的形状）。"""
    box: dict = {}

    def _target():
        try:
            asyncio.run(fn())
        except BaseException as exc:  # noqa: BLE001 - 原样带回主线程断言
            box["exc"] = exc

    t = threading.Thread(target=_target)
    t.start()
    t.join()
    if "exc" in box:
        raise box["exc"]


def _run_in_new_loop(fn) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(fn())
    finally:
        loop.close()


def _run_in_main_like_loop(fn) -> None:
    """起一个独立线程 + 新循环跑（``asyncio.run`` 在非主线程也能跑；主线程如果
    再 ``asyncio.run`` 会和 pytest 的循环打架，所以这里统一放线程里）。"""
    _run_in_worker(fn)


def _contended(provider, n: int = 5):
    """并发进同一把锁——这是跨循环错误真正被触发的前提。

    ``asyncio.Lock`` 只在**等锁**时才 ``_get_loop()`` 把循环钉死；串行进锁走的是
    「直接置 locked」快路径，根本不碰 ``_loop``，所以串行调用在两个循环之间
    来回跑也不会出错（最初按串行写的复现就是这样漏掉的）。
    """

    async def main():
        await asyncio.gather(*(provider._ensure_started() for _ in range(n)))

    return main


def test_contended_lock_across_loops_without_guard_raises():
    """反向证明：裸 asyncio.Lock 在「主循环先并发抢 → 换循环再抢」时就炸（即用户
    2026-11-03 报的错）。这条不依赖 provider，只是把事故条件钉在测试里，免得哪天
    有人把 LoopSafeLock 换回去还以为没事。"""

    class _Bare:
        def __init__(self):
            self._lock = asyncio.Lock()

        async def _ensure_started(self):
            async with self._lock:
                await asyncio.sleep(0.01)

    bare = _Bare()

    async def main():
        await asyncio.gather(*(bare._ensure_started() for _ in range(5)))

    _run_in_main_like_loop(main)
    with pytest.raises(RuntimeError, match="bound to a different event loop"):
        _run_in_main_like_loop(main)


# --- provider 的真实路径 -----------------------------------------------------


def test_ensure_started_survives_checkin_loop_then_main_loop(monkeypatch):
    """签到的一次性循环先拿锁，主循环再来（点「测试」）不能报 bound to 别的循环。"""
    p = DoubaoProvider()
    p._started = False
    p._client._ready = False
    started = []

    async def fake_start():
        started.append(asyncio.get_running_loop())
        p._client._ready = True   # 启动完成 → 后续调用走 _is_connected 快路径

    async def fake_restart():
        await fake_start()

    monkeypatch.setattr(p._client, "start", fake_start)
    monkeypatch.setattr(p._client, "restart", fake_restart)

    # 1) 主循环：并发请求一起抢 _start_lock（这时锁被钉在主循环上）
    _run_in_main_like_loop(_contended(p))
    # 2) App 刷新页面 / 换账号：连接失效，但 _started 仍为 True → 走 _reconnect
    p._client._ready = False
    # 3) 签到：worker 线程 + 一次性循环 + asyncio.run（原来就在这里炸）
    _run_in_worker(_contended(p))

    assert p._started is True
    assert len(started) >= 1  # 两条路径都跑完了，没有 RuntimeError 逃出去


def test_ensure_started_checkin_path_after_forward(monkeypatch):
    """反过来也一样：主循环先拿锁，签到的一次性循环进来不能炸。"""
    p = DoubaoProvider()
    p._started = False
    p._client._ready = False

    async def fake_start():
        p._client._ready = True

    async def fake_restart():
        await fake_start()

    monkeypatch.setattr(p._client, "start", fake_start)
    monkeypatch.setattr(p._client, "restart", fake_restart)

    _run_in_worker(_contended(p))
    p._client._ready = False
    _run_in_main_like_loop(_contended(p))
    assert p._started is True


def test_provider_locks_are_loop_safe():
    """回归闸门：provider 里不准再出现裸 asyncio.Lock（它会被钉死在首个循环上）。"""
    p = DoubaoProvider()
    assert isinstance(p._start_lock, LoopSafeLock)
    assert isinstance(p._reconnect_lock, LoopSafeLock)


# --- LoopSafeLock 本身 -------------------------------------------------------


def test_loop_safe_lock_serializes_within_one_loop():
    """同一循环内仍是互斥锁（防并发双重启动的本职不能被修跨循环时弄丢）。"""
    lock = LoopSafeLock("t")
    state = {"n": 0}

    async def work():
        async with lock.guard():
            v = state["n"]
            await asyncio.sleep(0)   # 强制让出，并发若没被挡住这里就会交错
            state["n"] = v + 1

    async def many():
        await asyncio.gather(*(work() for _ in range(50)))

    _run_in_new_loop(many)
    assert state["n"] == 50


def test_loop_safe_lock_does_not_block_across_loops():
    """跨循环不互等（两个循环的任务本就唤不醒对方，等就是死锁）。"""
    lock = LoopSafeLock("t")
    order: list[str] = []
    entered = threading.Event()

    async def slow():
        async with lock.guard():
            order.append("slow-enter")
            await asyncio.sleep(0.2)

    def _slow_thread():
        asyncio.run(slow())
        entered.set()

    t = threading.Thread(target=_slow_thread)
    t.start()
    # 等慢的确实占了锁（同循环内 locked() 才看得到；这里靠时间窗，足够稳）
    import time

    for _ in range(50):
        if lock.locked():
            break
        time.sleep(0.01)

    async def fast():
        async with lock.guard():
            order.append("fast-enter")

    _run_in_new_loop(fast)
    t.join(timeout=5)
    entered.wait(timeout=5)

    # 快的没被慢的挡住：顺序是先 slow-enter 再 fast-enter（而不是 fast 卡 0.2s）
    assert order == ["slow-enter", "fast-enter"]


def test_loop_safe_lock_drops_dead_loop_entries():
    """一次性循环用完即走：字典里不该攒着已完成循环的锁（否则 id() 复用会错配）。"""
    lock = LoopSafeLock("t")

    async def once():
        async with lock.guard():
            pass

    for _ in range(3):
        _run_in_new_loop(once)
    assert lock._locks == {}, lock


@pytest.mark.parametrize("rounds", [1, 2])
def test_loop_safe_lock_reentrant_safe_after_many_loops(rounds):
    """反复换循环仍能正常进锁（不被上一轮残留的状态挡住）。"""
    lock = LoopSafeLock("t")
    seen = []

    async def work():
        async with lock.guard():
            seen.append(1)

    for _ in range(rounds):
        _run_in_new_loop(work)
    assert len(seen) == rounds
