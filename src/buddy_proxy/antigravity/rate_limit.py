"""Antigravity 按账号并发与滑窗频控保护（超限自动排队）。

根据 2026-11-14 ~ 2026-11-21 实测日志设计：
- 上游并非单纯按「固定极低 QPS」拒单，但单账号在短时高并发（>=3 在途）或高 RPM
  （>10~13 req/min）叠加长上下文时，极易触发 daily-cloudcode-pa 90s 卡死并回退
  cloudcode-pa 吃 ``429 Resource has been exhausted`` 或「纯 thinking 空流 502」。
- 因此在**发起上游请求前**对每个账号做两层保护：
  1. **并发上限**（默认每账号最多 2 个在途请求，流式直到流关闭才释放）；
  2. **双滑窗节流**（默认每账号 10s 最多 3 次突发、60s 最多 10 次）。
- 多账号时优先挑「此刻无需排队」的账号（不改变顺位内既有主备/均衡顺序），
  全部账号都满载时按最短预计等待时间异步排队（可配置超时），不直接把并发峰值砸向
  上游换 429。

锁选 ``threading.Lock`` 而非 ``asyncio.Lock``：单测频繁通过多次 ``asyncio.run()``
切换事件循环，跨 loop 复用的实例不能绑定单个 loop；锁内只做纳秒级内存计数，
``asyncio.sleep`` 全在锁外执行。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Sequence, TypeVar

log = logging.getLogger(__name__)

_T = TypeVar("_T")

DEFAULT_MAX_CONCURRENT_PER_ACCOUNT = 2
DEFAULT_RPM_PER_ACCOUNT = 10
DEFAULT_BURST_10S_PER_ACCOUNT = 3
DEFAULT_MIN_INTERVAL_S = 0.0
DEFAULT_QUEUE_TIMEOUT_S = 90.0

_CONCURRENCY_POLL_S = 0.2


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
class RateLimitConfig:
    """单账号频控与并发参数（0 表示不限制对应维度）。"""

    max_concurrent: int = DEFAULT_MAX_CONCURRENT_PER_ACCOUNT
    rpm: int = DEFAULT_RPM_PER_ACCOUNT
    burst_10s: int = DEFAULT_BURST_10S_PER_ACCOUNT
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S
    queue_timeout_s: float = DEFAULT_QUEUE_TIMEOUT_S

    @classmethod
    def from_env(cls) -> "RateLimitConfig":
        return cls(
            max_concurrent=_env_int(
                "ANTIGRAVITY_MAX_CONCURRENT_PER_ACCOUNT",
                DEFAULT_MAX_CONCURRENT_PER_ACCOUNT,
            ),
            rpm=_env_int("ANTIGRAVITY_RPM_PER_ACCOUNT", DEFAULT_RPM_PER_ACCOUNT),
            burst_10s=_env_int(
                "ANTIGRAVITY_BURST_10S_PER_ACCOUNT",
                DEFAULT_BURST_10S_PER_ACCOUNT,
            ),
            min_interval_s=_env_float(
                "ANTIGRAVITY_MIN_INTERVAL_S", DEFAULT_MIN_INTERVAL_S
            ),
            queue_timeout_s=_env_float(
                "ANTIGRAVITY_QUEUE_TIMEOUT_S", DEFAULT_QUEUE_TIMEOUT_S
            ),
        )


class AccountLease:
    """单次账号执行租约；释放幂等，流式路径交给 ``_ReplayStream`` 持有到收尾。"""

    def __init__(self, release_fn: Callable[[], None]) -> None:
        self._release_fn = release_fn
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._release_fn()


class AccountRateLimiter:
    """按账号维度的并发 + 双滑窗（10s/60s）异步排队器。"""

    def __init__(
        self,
        config: RateLimitConfig | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._in_flight: dict[str, int] = {}
        self._timestamps: dict[str, deque[float]] = {}

    @property
    def config(self) -> RateLimitConfig:
        return self._config if self._config is not None else RateLimitConfig.from_env()

    def _prune(self, stamps: deque[float], now: float) -> None:
        cutoff = now - 60.0
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()

    def _compute_delay_locked(
        self, account_id: str, now: float, cfg: RateLimitConfig
    ) -> tuple[float, str]:
        """在锁内计算该账号还需等待的秒数与原因；(0.0, '') 表示可立即放行。"""
        if cfg.max_concurrent > 0 and self._in_flight.get(account_id, 0) >= cfg.max_concurrent:
            return _CONCURRENCY_POLL_S, f"并发上限 {cfg.max_concurrent}"

        stamps = self._timestamps.get(account_id)
        if not stamps:
            return 0.0, ""
        self._prune(stamps, now)
        if not stamps:
            return 0.0, ""

        wait = 0.0
        reason = ""
        if cfg.min_interval_s > 0:
            gap = stamps[-1] + cfg.min_interval_s - now
            if gap > wait:
                wait = gap
                reason = f"最小间隔 {cfg.min_interval_s:.1f}s"

        if cfg.burst_10s > 0:
            cutoff_10 = now - 10.0
            in_10 = [t for t in stamps if t > cutoff_10]
            if len(in_10) >= cfg.burst_10s:
                gap = in_10[0] + 10.0 - now
                if gap > wait:
                    wait = gap
                    reason = f"10s 突发上限 {cfg.burst_10s}"

        if cfg.rpm > 0 and len(stamps) >= cfg.rpm:
            gap = stamps[0] + 60.0 - now
            if gap > wait:
                wait = gap
                reason = f"60s 频控上限 {cfg.rpm}/min"

        return max(0.0, wait), reason

    def estimate_wait(self, account_id: str) -> float:
        """估算该账号此刻距离可放行的等待秒数（0.0 表示立即可用）。"""
        cfg = self.config
        now = self._clock()
        with self._lock:
            wait, _ = self._compute_delay_locked(account_id, now, cfg)
            return wait

    def pick_account_order(self, accounts: Sequence[_T]) -> list[_T]:
        """多账号选号排序：立即可用的保持原顺位排前，需排队的按最短等待排后。"""
        if len(accounts) <= 1:
            return list(accounts)
        cfg = self.config
        now = self._clock()
        ready: list[_T] = []
        busy: list[tuple[float, int, _T]] = []
        with self._lock:
            for idx, acct in enumerate(accounts):
                aid = str(getattr(acct, "id", acct))
                wait, _ = self._compute_delay_locked(aid, now, cfg)
                if wait <= 0.0:
                    ready.append(acct)
                else:
                    busy.append((wait, idx, acct))
        busy.sort(key=lambda item: (item[0], item[1]))
        return ready + [item[2] for item in busy]

    def _try_reserve_locked(
        self, account_id: str, now: float, cfg: RateLimitConfig
    ) -> tuple[AccountLease | None, float, str]:
        wait, reason = self._compute_delay_locked(account_id, now, cfg)
        if wait > 0.0:
            return None, wait, reason
        self._in_flight[account_id] = self._in_flight.get(account_id, 0) + 1
        stamps = self._timestamps.setdefault(account_id, deque())
        stamps.append(now)

        def _release() -> None:
            with self._lock:
                cur = self._in_flight.get(account_id, 0)
                if cur <= 1:
                    self._in_flight.pop(account_id, None)
                else:
                    self._in_flight[account_id] = cur - 1

        return AccountLease(_release), 0.0, ""

    async def acquire(
        self, account_id: str, *, timeout_s: float | None = None
    ) -> AccountLease | None:
        """申请账号执行租约；若达并发/频控上限则异步排队等待直到超时。"""
        cfg = self.config
        budget = cfg.queue_timeout_s if timeout_s is None else max(0.0, float(timeout_s))
        deadline = self._clock() + budget
        logged_wait = False

        while True:
            now = self._clock()
            with self._lock:
                lease, wait, reason = self._try_reserve_locked(account_id, now, cfg)
            if lease is not None:
                return lease
            remaining = deadline - now
            if remaining <= 0.0:
                return None
            sleep_s = min(wait, remaining)
            if not logged_wait:
                logged_wait = True
                log.info(
                    "antigravity 账号 %s 触发保护，排队等待（%s，本轮预计 %.1fs）",
                    account_id,
                    reason or "限流",
                    sleep_s,
                )
            await asyncio.sleep(sleep_s)
