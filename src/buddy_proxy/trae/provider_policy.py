"""Shared native rejection pacing and account/model error policy."""

from __future__ import annotations

from typing import Any
from fastapi import HTTPException

# Bound once in the original function default, not dynamically read per call.
_MODEL_OVERFLOW_TTL_S = 60.0

def _note_reject(model: str) -> None:
    from . import provider as hooks
    hooks._last_reject_at[model] = hooks.time.monotonic()

def _pace_after_reject(model: str) -> None:
    """该模型最近 4001 后不满安静窗则等到窗外再发（读线程/to_thread 内睡）。"""
    from . import provider as hooks
    t = hooks._last_reject_at.get(model)
    if t is None:
        return
    wait = hooks._REJECT_QUIET_S - (hooks.time.monotonic() - t)
    if wait > 0:
        hooks.time.sleep(wait)

def _note_native_ok(model: str) -> None:
    """native 请求成功＝门开着：清掉节流时间戳，后续请求不必陪等安静窗。"""
    from . import provider as hooks
    hooks._last_reject_at.pop(model, None)

def _stream_rejected_recently(model: str) -> bool:
    """该模型的流式尝试近期被 4001 拒过（TTL 内）。单调钟，免疫墙钟跳变。"""
    from . import provider as hooks
    until = hooks._stream_reject_until.get(model)
    return until is not None and hooks.time.monotonic() < until

def _mark_stream_rejected(model: str) -> None:
    from . import provider as hooks
    hooks._stream_reject_until[model] = hooks.time.monotonic() + hooks._STREAM_REJECT_SKIP_S
    hooks._note_reject(model)

def _attach_sse_code(exc: HTTPException, code: Any) -> HTTPException:
    """把上游 SSE 原始码挂到异常上（forward 据 ``trae_sse_code`` 识别 1005 权益类）。"""
    from . import provider as hooks
    try:
        exc.trae_sse_code = int(code)  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        pass
    return exc

def _sse_error_exception(data: dict[str, Any]) -> HTTPException:
    """上游 SSE 错误事件 → HTTPException（状态按 ``_sse_error_status``，保留原始码）。

    ``detail`` 用 **OpenAI 的错误形状** ``{"error": {"message", "type"}}``，不能
    用裸字符串。

    裸字符串经 FastAPI 序列化成 ``{"detail": "Trae plan 权益不足…"}``——既不是
    OpenAI 口径也不是 Anthropic 口径，客户端只能靠 HTTP 状态码猜。Kimi 就把
    403 归成 ``provider.auth_error``，弹出「请重新登录」，而真因是「这个模型
    该账号没权益」。包成标准形状后，客户端能读到 ``type``/``message``。
    """
    from . import provider as hooks
    code = (data or {}).get("code")
    return hooks._attach_sse_code(
        hooks.HTTPException(
            status_code=hooks._sse_error_status(code),
            detail={"error": {
                "message": hooks._trae_error_text(data),
                "type": hooks._openai_error_type(hooks._sse_error_status(code)),
            }},
        ),
        code,
    )

def _is_entitlement_error(e: HTTPException) -> bool:
    """该异常是否为 1005 权益类（换号、但不整账号冷却）。"""
    from . import provider as hooks
    return getattr(e, "trae_sse_code", None) == hooks._ENTITLEMENT_SSE_CODE

def _is_context_overflow(e: HTTPException) -> bool:
    """该异常是否为 4026 上下文超限（换号、但不整账号冷却）。"""
    from . import provider as hooks
    return getattr(e, "trae_sse_code", None) == hooks._CONTEXT_OVERFLOW_SSE_CODE

def _mark_model_overflow(model: str, *, ttl: float = _MODEL_OVERFLOW_TTL_S) -> None:
    from . import provider as hooks
    key = str(model or "").strip().lower()
    if not key:
        return
    with hooks._model_overflow_lock:
        hooks._model_overflow_mark[key] = hooks.time.monotonic() + ttl

def _model_overflow_recently(model: str) -> bool:
    from . import provider as hooks
    key = str(model or "").strip().lower()
    if not key:
        return False
    with hooks._model_overflow_lock:
        until = hooks._model_overflow_mark.get(key)
        if until is None:
            return False
        if hooks.time.monotonic() >= until:
            hooks._model_overflow_mark.pop(key, None)
            return False
        return True

def _internal_model_key(region_key: str, requested: Any) -> str:
    """外部模型名 → 内部键（MODEL_MAP 解析后小写），供选号/Max 决策统一使用。

    ``requested`` 可能为 None：``forward`` 里是 ``body.get("model", "auto")``，``.get``
    的默认值只在**键缺失**时生效，客户端显式发 ``"model": null`` 就会拿到 None；
    而 ``map_model_for`` 对 None 原样透传，直接 ``.lower()`` 会 AttributeError。
    这里统一 ``str(x or "")`` 兜底，所有需要「内部模型键」的地方都走它，别各写一份。
    """
    from . import provider as hooks
    return hooks.map_model_for(region_key, str(requested or "")).lower()

def _mark_entitlement_skip(account_id: str, model: str,
                           ttl: float | None = None) -> None:
    """记「该账号对本模型不可用」，TTL 内选号时跳过它。

    ``ttl`` 缺省用权益类的 600s；4026 传更短的 TTL（上下文超限只说明这次
    请求太长，DSH 压缩后很快就会重试短请求，不该让账号白等 10 分钟）。
    """
    from . import provider as hooks
    key = (str(account_id), str(model or "").strip().lower())
    with hooks._entitlement_skip_lock:
        hooks._entitlement_skip[key] = hooks.time.time() + (ttl or hooks._ENTITLEMENT_SKIP_TTL_S)

def _entitlement_skipped(account_id: str, model: str) -> bool:
    """该账号是否因 1005 被记为本模型不可用（过期条目顺手清掉）。"""
    from . import provider as hooks
    key = (str(account_id), str(model or "").strip().lower())
    with hooks._entitlement_skip_lock:
        until = hooks._entitlement_skip.get(key)
        if until is None:
            return False
        if hooks.time.time() >= until:
            hooks._entitlement_skip.pop(key, None)
            return False
        return True

def _account_error_from_frame(stripped_frame: str) -> "HTTPException | None":
    """从单个 SSE 帧里识别账号级错误，还原成 HTTPException（None = 放行）。

    ``_stream`` 的 ``except HTTPException`` 会把账号级错误 yield 成
    ``error_chunk``（payload 是 ``{"error": {"message", "type", "code"}}``）
    再 ``return``——那是假成功。code 有两种形态：HTTP 形态（``_stream``
    抛出的 HTTPException 的 ``status_code``，401/429）与上游 SSE 码形态
    （``event:error`` 帧原样透传的 4008/4011 等，经 ``_sse_error_status``
    映射）。本函数在闸门里把这类帧还原成 HTTPException，让 forward 的
    failover 循环冷却换号；非账号级错误帧返回 None（放行，由下游/包装层
    按原样处理）。
    """
    from . import provider as hooks
    if not stripped_frame.startswith("data:"):
        return None
    payload = stripped_frame[5:].strip()
    try:
        data = hooks.json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if not isinstance(err, dict):
        return None
    try:
        code = int(err.get("code"))
    except (TypeError, ValueError):
        return None
    detail = str(err.get("message") or "trae upstream error")
    # 两种 code 形态都认：HTTP 形态（``_stream`` 把自己的 HTTPException
    # yield 成 error chunk 时传的是 e.status_code）与上游 SSE 码形态
    # （``event:error`` 帧原样透传的 4008/4011 等）。后者此前不认——额度
    # 错误放行成假成功，闸门形同虚设（2026-10-06 双账号实测撞上）。
    if code in hooks._ACCOUNT_ERROR_CODES:
        return hooks.HTTPException(status_code=code, detail=detail)
    status = hooks._sse_error_status(code)
    if status == 502:
        return None
    # 保留上游原始码：1005 权益类在 forward 里按「账号+模型」跳过，不整账号冷却。
    return hooks._attach_sse_code(hooks.HTTPException(status_code=status, detail=detail), code)

def _lb_state():
    """当前运行态（负载均衡开关读它）。未初始化时返回 None。"""
    from . import provider as hooks
    from ..core import state as _state
    return getattr(_state, "proxy_state", None)
