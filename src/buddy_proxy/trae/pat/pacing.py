"""PAT 频控：按（账号, 模型）的滑动窗口节流 + 撞限频码后的短冷却。

上游对部分扩展模型有**按账号、按模型**的请求频率限制（撞限会回队列满/
访问量过大类 SSE 错误码，见 ``config._RATE_LIMIT_SSE_CODES``）。这与额度
耗尽是两回事：几十秒后自愈，换账号或稍等即可。因此：

- 模型可在 models_config.json 声明 ``rate_limit: {"requests": N, "window_s": W}``，
  发送前按窗口预留名额，满了就换账号，所有账号都满时**有界等待**（流式
  路径等待期间读线程外层照常发心跳）；
- 撞限频码时给该（账号, 模型）打短冷却并换号，不影响该账号的其它模型；
  所有账号都处于限频冷却时不等待，直接 429 交给 model_order 换档；
- 等待超过 ``TRAE_PAT_PACE_MAX_WAIT_S`` 仍无名额 → 由调用方抛 429，让
  model_order 换到备选通道。

纯内存状态：重启后从零计数，代价至多一次撞码。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Sequence

import buddy_proxy.trae.pat as _ns

from .config import PatProfile

log = logging.getLogger(__name__)

#: 撞限频码后（账号, 模型）的冷却秒数（上游未给 Retry-After）。
_RATE_LIMIT_COOLDOWN_S = 30
#: 所有账号都无名额时的最长等待秒数（超过即放弃，由调用方抛 429）。
_DEFAULT_MAX_WAIT_S = 30.0

_lock = threading.Lock()
#: (cache_key, model) → 窗口内已预留的请求时间戳（monotonic）。
_slots: dict[tuple[str, str], deque[float]] = {}
#: (cache_key, model) → 限频冷却截止（monotonic）。
_rate_cooldowns: dict[tuple[str, str], float] = {}


def _max_wait_s() -> float:
    try:
        return max(0.0, float(os.environ.get("TRAE_PAT_PACE_MAX_WAIT_S", _DEFAULT_MAX_WAIT_S)))
    except ValueError:
        return _DEFAULT_MAX_WAIT_S


def _model_limit(model: str) -> tuple[int, float] | None:
    """模型声明的窗口限额 (requests, window_s)；未声明返回 None（不节流）。"""
    _ns._reload_pat_models()  # mtime 未变时为空操作
    return _ns.PAT_MODEL_RATE_LIMITS.get(model)


def _wait_for(key: tuple[str, str], limit: tuple[int, float] | None, now: float) -> float:
    """该（账号, 模型）距可用还需等待的秒数；0 = 立即可用。调用方持锁。"""
    wait = max(0.0, _rate_cooldowns.get(key, 0.0) - now)
    if limit is not None:
        requests, window = limit
        slots = _slots.get(key)
        if slots is not None:
            while slots and now - slots[0] >= window:
                slots.popleft()
            if len(slots) >= requests:
                wait = max(wait, slots[0] + window - now)
    return wait


def try_reserve(profile: PatProfile, model: str) -> bool:
    """原子地为本次请求预留名额；不可用（冷却中或窗口已满）返回 False。"""
    limit = _model_limit(model)
    key = (profile.cache_key, model)
    now = time.monotonic()
    with _lock:
        if _wait_for(key, limit, now) > 0:
            return False
        if limit is not None:
            _slots.setdefault(key, deque()).append(now)
    return True


def wait_for_slot(profiles: Sequence[PatProfile], model: str,
                  stop: threading.Event | None = None) -> bool:
    """有界等待直到任一账号可用；超时/被中止返回 False（不预留名额）。"""
    limit = _model_limit(model)
    deadline = time.monotonic() + _max_wait_s()
    logged = False
    while True:
        now = time.monotonic()
        with _lock:
            waits = [_wait_for((p.cache_key, model), limit, now) for p in profiles]
            # 上游已明确限频（全部账号在限频冷却中）：不原地干等，立刻让调用方
            # 报 429 换下一个顺序目标。只有主动节流的窗口空档才值得等。
            all_rejected = bool(profiles) and all(
                _rate_cooldowns.get((p.cache_key, model), 0.0) > now for p in profiles)
        wait = min(waits) if waits else 0.0
        if wait <= 0:
            return True
        if all_rejected:
            return False
        if now + wait > deadline:
            return False
        if not logged:
            log.info("PAT %s 各账号均在限频窗口内，等待 %.1fs", model, wait)
            logged = True
        # 分段睡眠以便及时响应客户端断开
        if stop is not None:
            if stop.wait(min(wait, 1.0)):
                return False
        else:
            time.sleep(min(wait, 1.0))


def mark_rate_limited(profile: PatProfile, model: str, code: int | None = None) -> None:
    """撞限频码：该（账号, 模型）短冷却，其它模型不受影响。"""
    key = (profile.cache_key, model)
    with _lock:
        _rate_cooldowns[key] = max(_rate_cooldowns.get(key, 0.0),
                                   time.monotonic() + _RATE_LIMIT_COOLDOWN_S)
    log.warning("PAT 账号序号=%d 模型 %s 撞限频码 %s，冷却 %ds 并换号",
                profile.index, model, code if code is not None else "unknown",
                _RATE_LIMIT_COOLDOWN_S)


def _reset_for_tests() -> None:
    with _lock:
        _slots.clear()
        _rate_cooldowns.clear()
