"""Trae 上游请求的可对账遥测（仅安全元数据，不保存凭据或响应体）。"""

from __future__ import annotations

import time
import uuid
from typing import Any

from ..core.metrics import ACCOUNT_META

_MAX_ATTEMPTS = 8


def start_attempt(*, transport: str, request_id: str, session_id: str,
                  model: str, max_mode: bool, endpoint: str = "",
                  meta: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """记录一次实际 HTTP 发送；无 metrics holder 时保持转发路径完全不变。"""
    meta = meta if meta is not None else ACCOUNT_META.get()
    if meta is None:
        return None
    attempts = meta.setdefault("trae_attempts", [])
    if not isinstance(attempts, list) or len(attempts) >= _MAX_ATTEMPTS:
        return None
    meta.setdefault("trae_client_request_id", uuid.uuid4().hex)
    attempt = {
        "request_id": str(request_id)[:80],
        "session_id": str(session_id)[:80],
        "transport": str(transport)[:16],
        "model": str(model)[:80],
        "max_mode": bool(max_mode),
        "account": str(meta.get("account") or "")[:40],
        "endpoint": str(endpoint)[:80],
        "started": time.monotonic(),
        "outcome": "started",
    }
    attempts.append(attempt)
    return attempt


def finish_attempt(attempt: dict[str, Any] | None, *, outcome: str) -> None:
    """结束物理发送；这里绝不覆盖最终成功路由。"""
    if attempt is None or "duration_ms" in attempt:
        return
    attempt["outcome"] = str(outcome)[:40]
    started = attempt.pop("started", None)
    if isinstance(started, (int, float)):
        attempt["duration_ms"] = max(0, round((time.monotonic() - started) * 1000))


def mark_final_success(attempt: dict[str, Any] | None,
                       meta: dict[str, Any] | None = None) -> None:
    """仅由已接受的响应调用，保存最后可对账的 client-generated ID。"""
    if attempt is None:
        return
    meta = meta if meta is not None else ACCOUNT_META.get()
    if meta is None:
        return
    meta["trae_final_transport"] = attempt["transport"]
    meta["trae_request_id"] = attempt["request_id"]
    meta["trae_session_id"] = attempt["session_id"]


def response_outcome(raw: str) -> str:
    """将原文只用于内存判定，落盘采用稳定的无敏感类别。"""
    return "fallback" if '"code":4001' in raw.replace(" ", "") else "response"
