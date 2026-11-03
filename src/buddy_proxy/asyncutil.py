"""跨事件循环安全的异步原语。

``asyncio.Lock`` 自 3.10 起是**绑定循环**的（``_LoopBoundMixin._get_loop``：
第一次用就记住那个 loop，之后在别的 loop 上用直接 ``RuntimeError: ... is
bound to a different event loop``）。而 provider 实例是进程级单例，却会被两个
不同的循环碰到：

- 主循环：uvicorn 处理 HTTP（``/v1/chat/completions``、UI 的「测试」按钮）；
- 一次性循环：``benefits`` 用 ``asyncio.to_thread`` 调同步的签到方法，里头再
  ``asyncio.run(...)``（豆包 ``_run_checkin``）。

谁先用到锁谁就把它钉死在该循环上，另一个循环下次进来就报错——用户 2026-11-03
在豆包通道点「测试」看到的正是这个。

所以只要锁可能在两个循环之间流转，就必须用 :class:`LoopSafeLock`：内部按当前
运行循环各备一把 ``asyncio.Lock``，同一循环内照旧串行，跨循环互不等待（它们本来
也等不了彼此——不同循环上的任务不可能互相唤醒）。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any


class _LoopSafeGuard:
    """``async with lock.guard():`` 的上下文管理器。"""

    __slots__ = ("_lock", "_key", "_inner")

    def __init__(self, lock: "LoopSafeLock", key: int, inner: asyncio.Lock) -> None:
        self._lock = lock
        self._key = key
        self._inner = inner

    async def __aenter__(self) -> None:
        try:
            await self._inner.acquire()
        except BaseException:
            # 等锁时被取消：``async with`` 不会替我们跑 __aexit__，引用计数
            # 必须自己退，否则这把死锁永远留在字典里占着这个循环的键。
            self._lock._release(self._key, self._inner)
            raise
        return None

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._inner.release()
        self._lock._release(self._key, self._inner)


class LoopSafeLock:
    """按事件循环分身的互斥锁，语义同 ``asyncio.Lock``。

    用法只是把 ``async with self._lock:`` 换成 ``async with self._lock.guard():``。

    - 同一循环内：完全等价于一把普通 ``asyncio.Lock``（公平 FIFO、防并发双启动）。
    - 不同循环之间：各自拿到自己那把，**不互相阻塞**。这是刻意的取舍——两个
      循环上的任务本来就不可能被对方唤醒，硬等只会死锁；而锁在这里保护的是
      「同一循环内别并发做同一件事」，跨循环那点重叠由调用方的状态判据（如
      ``_is_connected()`` 或 ``threading.Lock``）兜住。

    ``threading.Lock`` 串行化簿记：锁被不同线程的循环同时摸到时不会给同一个
    循环建出两把。每把锁按「有几个 guard 正持有/正等它」计数，归零才从字典
    摘掉——一次性循环（``asyncio.run``）用完即走，不能攒着，否则 ``id(loop)``
    被回收复用时会错配到新循环上。
    """

    __slots__ = ("_name", "_locks", "_refs", "_tlock")

    def __init__(self, name: str = "") -> None:
        self._name = name
        # id(loop) → asyncio.Lock（同一循环是同一个对象，且其 _loop 就是它）
        self._locks: dict[int, asyncio.Lock] = {}
        self._refs: dict[int, int] = {}
        self._tlock = threading.Lock()

    def _slot(self) -> tuple[int, asyncio.Lock]:
        loop = asyncio.get_running_loop()
        key = id(loop)
        with self._tlock:
            lock = self._locks.get(key)
            # ``lock._loop is not loop``：这把是别的循环用过的（asyncio.Lock 只在
            # 真正等待时才 ``_get_loop`` 钉死；快路径抢锁根本不碰它，所以不能靠
            # 在创建时给 ``_loop`` 赋值来标记归属）。对不上就换一把新的。
            if lock is None or lock._loop not in (None, loop):
                lock = asyncio.Lock()
                self._locks[key] = lock
                self._refs[key] = 0
            self._refs[key] += 1
            return key, lock

    def _release(self, key: int, lock: asyncio.Lock) -> None:
        with self._tlock:
            if self._locks.get(key) is not lock:
                return
            self._refs[key] -= 1
            if self._refs[key] <= 0:
                self._locks.pop(key, None)
                self._refs.pop(key, None)

    def guard(self) -> _LoopSafeGuard:
        key, inner = self._slot()
        return _LoopSafeGuard(self, key, inner)

    def locked(self) -> bool:
        """当前循环里的那把是否被占——纯诊断用，别拿它做业务判据。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        with self._tlock:
            lock = self._locks.get(id(loop))
            return bool(lock and lock._loop is loop and lock.locked())

    def __repr__(self) -> str:
        return f"<LoopSafeLock {self._name!r} loops={len(self._locks)}>"
