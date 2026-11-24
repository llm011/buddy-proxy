"""raccoon 每账号并发闸门（上游并发上限约 5，必须本地限流）。

## 为什么必须自己限流

上游的并发限制**不是**软性的排队，而是直接回
``429 user_model_rate_limited``；更糟的是这个 429 是「账号 × 模型」的
**频率窗口**，恢复中位数 **25 分钟**。也就是说：多发一个并发请求，代价是
这个账号在这个模型上停摆近半小时。

所以绝不能靠「打上去看 429 再退」——必须在本地就卡住。

## 阈值

开源实现（Coding2API）把 ``RACCOON_MAX_CONCURRENCY`` 默认设成 **2**，理由是
上游上限 5、留余量给同账号的其它客户端。本通道沿用这个保守值：

* 默认每账号 2 个在途（``RACCOON_MAX_CONCURRENCY`` 可覆盖，``0`` = 关闭）；
* 超限时**排队等待**（默认最长 90s），而不是立刻失败——agent 类客户端的
  工具调用链会短时打几个并发，直接拒绝会让整轮对话断掉；
* 排队超时才返回 429，并说清「等多久」。

## 实现选择

锁用 ``threading.Lock`` 而非 ``asyncio.Lock``：单测频繁用多次 ``asyncio.run()``
切换事件循环，跨 loop 复用的实例不能绑定单个 loop。锁内只做内存计数，
``asyncio.sleep`` 全在锁外。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: 默认每账号在途并发上限。上游硬上限约 5，留余量给同账号其它客户端。
DEFAULT_MAX_CONCURRENT = 2

#: 排队等待上限（秒）。agent 工具链短时并发靠它吸收，不让整轮对话断掉。
DEFAULT_QUEUE_TIMEOUT_S = 90.0

#: 并发占满时的轮询间隔。
_POLL_S = 0.2


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


@dataclass
class Lease:
    """一次在途占位。用完必须 :meth:`release`（幂等）。"""

    limiter: "AccountConcurrencyLimiter"
    account_id: str
    released: bool = False
    #: 占位处（便于排查「谁没还」）。只做诊断，不影响逻辑。
    origin: str = ""
    acquired_at: float = 0.0

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.limiter._release(self)


class AccountConcurrencyLimiter:
    """按账号的在途并发闸门（超限排队，可超时）。"""

    def __init__(self, *, max_concurrent: int | None = None,
                 queue_timeout_s: float | None = None) -> None:
        self._max = (DEFAULT_MAX_CONCURRENT if max_concurrent is None
                     else max_concurrent)
        self._queue_timeout_s = (DEFAULT_QUEUE_TIMEOUT_S if queue_timeout_s is None
                                 else queue_timeout_s)
        self._inflight: dict[str, int] = {}
        #: 未归还的 lease 对象（诊断用；release 时摘掉）。
        self._live: list[Lease] = []
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "AccountConcurrencyLimiter":
        return cls(
            max_concurrent=_env_int("RACCOON_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENT),
            queue_timeout_s=_env_float("RACCOON_QUEUE_TIMEOUT_S", DEFAULT_QUEUE_TIMEOUT_S),
        )

    @property
    def max_concurrent(self) -> int:
        return self._max

    def inflight(self, account_id: str) -> int:
        with self._lock:
            return self._inflight.get(account_id, 0)

    def available(self, account_id: str) -> bool:
        """该账号此刻是否还有空位（不占位，供选号用）。"""
        if self._max <= 0:
            return True
        with self._lock:
            return self._inflight.get(account_id, 0) < self._max

    async def acquire(self, account_id: str, *,
                      timeout_s: float | None = None) -> Lease | None:
        """占一个并发位；超时返回 ``None``（调用方据此换号或报 429）。

        ``max_concurrent <= 0`` 时关闭限流，直接返回一个空 lease。
        """
        if self._max <= 0:
            return Lease(self, account_id)
        limit = self._queue_timeout_s if timeout_s is None else max(0.0, timeout_s)
        deadline = time.monotonic() + limit
        while True:
            with self._lock:
                cur = self._inflight.get(account_id, 0)
                if cur < self._max:
                    self._inflight[account_id] = cur + 1
                    lease = Lease(self, account_id, acquired_at=time.monotonic(),
                                  origin=_caller_origin())
                    self._live.append(lease)
                    return lease
            if time.monotonic() >= deadline:
                log.warning(
                    "raccoon 账号 %s 并发已满（%d/%d），排队 %.0fs 仍无空位；"
                    "未归还的占位：%s",
                    account_id, self.inflight(account_id), self._max, limit,
                    self.leaks(min_age_s=1.0) or "无（可能是瞬时并发）",
                )
                return None
            await asyncio.sleep(_POLL_S)

    def _release(self, lease: Lease) -> None:
        with self._lock:
            try:
                self._live.remove(lease)
            except ValueError:
                pass
            account_id = lease.account_id
            cur = self._inflight.get(account_id, 0)
            if cur <= 1:
                self._inflight.pop(account_id, None)
            else:
                self._inflight[account_id] = cur - 1

    def snapshot(self) -> dict[str, Any]:
        """当前在途统计（诊断/测试用）。"""
        with self._lock:
            return {"max": self._max, "inflight": dict(self._inflight)}

    def leaks(self, min_age_s: float = 60.0) -> list[dict[str, Any]]:
        """占位超过 ``min_age_s`` 还没还的 lease（诊断用）。

        存在的理由：2026-11-23 用户实报「raccoon 一直 429」，症状是**上游完全
        正常**（直连 1.9s）而本地闸门恒满、每个请求排队 90s 后超时。也就是
        某个 lease 拿到手就没还回去，把每账号 2 个名额永久吃掉。没有这个
        视图只能靠猜，所以留一份「谁占着、占多久、从哪拿的」。
        """
        now = time.monotonic()
        with self._lock:
            out: list[dict[str, Any]] = []
            for lease in self._live:
                age = now - lease.acquired_at
                if age >= min_age_s:
                    out.append({
                        "account": lease.account_id,
                        "age_s": round(age, 1),
                        "origin": lease.origin,
                    })
            return out


def _caller_origin() -> str:
    """占位处的 ``文件:行``（只给诊断用，不进正常日志）。

    走 ``sys._getframe`` 而不是 ``traceback.extract_stack``：后者要格式化整条
    栈，在每次 acquire 上都做太贵；这里只要一帧。
    """
    try:
        frame = sys._getframe(2)
        return f"{frame.f_code.co_filename.rsplit('/', 1)[-1]}:{frame.f_lineno}"
    except (AttributeError, ValueError):
        return ""


#: 进程级单例（与 provider 同生命周期）。
_limiter = AccountConcurrencyLimiter.from_env()
def limiter() -> AccountConcurrencyLimiter:
    """取进程级限流器（测试可 monkeypatch 本函数或直接换 ``_limiter``）。"""
    return _limiter


# ---------------------------------------------------------------------------
# 滑动窗口频率闸门：到顶就换号，不打上游
# ---------------------------------------------------------------------------
#
# 与上面的并发闸门是两回事：并发闸门管「同一时刻几个在途」，这里是「一段时间
# 内发了几个」。200108 账号风控是后者触发的——2026-11-24 实测（见 git log）：
# 5 分钟窗口 32 次不触发、33 次触发；单分钟 18 次不触发。所以阈值设在 5 分钟
# 窗口上，单分钟只是防突发的兜底。
#
# 到顶时的动作是「换号」而不是「排队」：排队只是把请求推迟，上游窗口照样在
# 走；换号让每个号都留在阈值之下，才是真正把请求摊开。所有号都到顶才失败。

#: 5 分钟窗口上限。实测 33 触发 200108，留 ~15% 余量。
DEFAULT_RATE_PER_5MIN = 28

#: 1 分钟窗口上限（防突发）。实测 13/分钟与两次风控时刻重合，18/分钟单独
#: 不触发；取 12 偏保守——两个号合计 24/分钟，单个 agent 会话够用。
DEFAULT_RATE_PER_MIN = 12

#: 两个窗口的秒数（写死：阈值是按这两个口径标定的，改窗口等于重新标定）。
_WIN_5MIN_S = 300.0
_WIN_1MIN_S = 60.0


class AccountRateWindow:
    """按账号的滑动窗口频率计数（只计数不排队；到顶由调用方换号）。"""

    def __init__(self, *, per_minute: int | None = None,
                 per_5min: int | None = None) -> None:
        self._per_min = (DEFAULT_RATE_PER_MIN if per_minute is None
                         else max(0, int(per_minute)))
        self._per_5min = (DEFAULT_RATE_PER_5MIN if per_5min is None
                          else max(0, int(per_5min)))
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "AccountRateWindow":
        return cls(
            per_minute=_env_int("RACCOON_RATE_PER_MIN", DEFAULT_RATE_PER_MIN),
            per_5min=_env_int("RACCOON_RATE_PER_5MIN", DEFAULT_RATE_PER_5MIN),
        )

    @property
    def per_minute(self) -> int:
        return self._per_min

    @property
    def per_5min(self) -> int:
        return self._per_5min

    def _counts(self, dq: deque[float], now: float) -> tuple[int, int]:
        """修剪掉出窗的旧记录，返回 ``(1 分钟内条数, 5 分钟内条数)``。

        调用方须持有 ``self._lock``。先按 5 分钟剪（更长的窗口），再在剩下的
        里数 1 分钟的——1 分钟窗口是 5 分钟的子集，不会漏。
        """
        cutoff5 = now - _WIN_5MIN_S
        while dq and dq[0] <= cutoff5:
            dq.popleft()
        n5 = len(dq)
        cutoff1 = now - _WIN_1MIN_S
        n1 = sum(1 for t in dq if t > cutoff1)
        return n1, n5

    def has_capacity(self, account_id: str, *, now: float | None = None) -> bool:
        """该账号此刻两个窗口是否都还有余量（**不**计数，供选号用）。"""
        if self._per_min <= 0 and self._per_5min <= 0:
            return True
        now = time.monotonic() if now is None else now
        with self._lock:
            dq = self._events.get(account_id)
            if not dq:
                return True
            n1, n5 = self._counts(dq, now)
        if self._per_5min > 0 and n5 >= self._per_5min:
            return False
        if self._per_min > 0 and n1 >= self._per_min:
            return False
        return True

    def record(self, account_id: str, *, now: float | None = None) -> None:
        """登记一次「已发出」的请求。调用方在 :meth:`has_capacity` 通过后、
        真正 ``send`` 之前调用——放在 send 前而不是选号时，是为了不把
        「选了号却因 token 刷新失败没发出去」的请求算进去。"""
        if self._per_min <= 0 and self._per_5min <= 0:
            return
        now = time.monotonic() if now is None else now
        with self._lock:
            dq = self._events.setdefault(account_id, deque())
            self._counts(dq, now)
            dq.append(now)

    def recovery_in(self, account_id: str, *, now: float | None = None) -> float:
        """距离该账号重新有余量还要多少秒（0 = 现在就有）。

        两个窗口取最大：5 分钟窗口到顶要等最老一条出窗；1 分钟窗口到顶要等
        1 分钟内最老的一条出 1 分钟窗。
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            dq = self._events.get(account_id)
            if not dq:
                return 0.0
            n1, n5 = self._counts(dq, now)
            wait = 0.0
            if self._per_5min > 0 and n5 >= self._per_5min and dq:
                wait = max(wait, dq[0] + _WIN_5MIN_S - now)
            if self._per_min > 0 and n1 >= self._per_min:
                cutoff1 = now - _WIN_1MIN_S
                oldest1 = next((t for t in dq if t > cutoff1), None)
                if oldest1 is not None:
                    wait = max(wait, oldest1 + _WIN_1MIN_S - now)
        return max(0.0, wait)

    def counts(self, account_id: str, *, now: float | None = None) -> tuple[int, int]:
        """当前 ``(1 分钟内, 5 分钟内)`` 计数（诊断/测试用）。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            dq = self._events.get(account_id)
            if not dq:
                return 0, 0
            return self._counts(dq, now)

    def clear(self, account_id: str | None = None) -> None:
        """清掉计数（删账号 / 测试隔离用；``None`` 清全部）。"""
        with self._lock:
            if account_id is None:
                self._events.clear()
            else:
                self._events.pop(account_id, None)


#: 进程级单例（与 provider 同生命周期）。
_window = AccountRateWindow.from_env()
def window() -> AccountRateWindow:
    """取进程级滑动窗口（测试可 monkeypatch 本函数或直接换 ``_window``）。"""
    return _window
