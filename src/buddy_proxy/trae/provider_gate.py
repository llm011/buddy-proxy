"""First-semantic-event commitment and isolated sync iterator adaptation."""

from __future__ import annotations

from typing import Any



def _next_or_end(it: Any) -> tuple[bool, Any]:
    """在线程里驱动同步生成器；用标记返回值避免 StopIteration 穿过 Future。"""
    from . import provider as hooks
    try:
        return True, next(it)
    except StopIteration:
        return False, None


async def _sync_to_async_iter(it: Any) -> Any:
    """把同步 iterator 包装成 async iterator（喂 StreamingResponse）。

    ``_stream`` / ``_wrap_anthropic_stream`` 在拿上游事件时会阻塞等待 queue；
    每次 ``next()`` 都移出事件循环，否则首帧之后的慢事件仍会卡住全站。
    """
    from . import provider as hooks
    it = iter(it)
    loop = hooks.asyncio.get_running_loop()
    while True:
        has_item, item = await loop.run_in_executor(
            hooks._stream_iter_pool, hooks._next_or_end, it)
        if not has_item:
            return
        yield item


async def _gate_first_event_async(gen: Any) -> "_GatedStream | BaseException":
    """用专属线程池预驱动首事件，并把请求 ContextVar 带入工作线程。

    ``run_in_executor`` 不像 ``asyncio.to_thread`` 会自动传播 contextvars；PAT 的
    请求级账号 holder 就在 ``ACCOUNT_META`` 里。若首个 ``next(gen)`` 在空上下文
    中执行，``_stream`` 随后复制给 SSE 读线程的也是空上下文，最终请求虽成功，
    metrics 的 account 却永远为空（Claude Code 流式请求正好稳定命中这条路径）。
    """
    from . import provider as hooks
    loop = hooks.asyncio.get_running_loop()
    request_ctx = hooks.contextvars.copy_context()
    return await loop.run_in_executor(
        hooks._stream_gate_pool, request_ctx.run, hooks._gate_first_event, gen)


def _gate_first_event(gen: Any) -> "_GatedStream | BaseException":
    """预驱动 ``_stream``（**同步**生成器）到第一个语义事件。

    ``_stream`` 是读线程 + queue 轮询的同步生成器（非 async）——下游
    :func:`_wrap_anthropic_stream` 也按同步 ``Iterator[str]`` 消费，本闸门
    同步迭代即可；StreamingResponse 那边由 starlette 对同步 iterator 做
    线程池适配（与改造前一致）。

    返回 :class:`_GatedStream`（成功，可放行）或捕获到的异常（换号/透传）。
    心跳帧（``: heartbeat``）与空帧跳过不缓冲。第一个含 ``data:`` 的语义帧
    默认即 committed；**但若该帧是账号级错误**（``_stream`` 的 ``except
    HTTPException`` 把 401/429 yield 成 ``{"error": {...}}`` 错误 chunk 再
    return，是假成功）则**不缓冲、直接返回对应 HTTPException** 让 failover
    换号——此时客户端未收字节，换号安全。真正的异常在第一个语义帧**之前**
    抛出也都安全换号（客户端未收字节）。
    """
    from . import provider as hooks
    buffered: list[str] = []
    try:
        for piece in gen:
            text = piece if isinstance(piece, str) else piece.decode("utf-8", "replace")
            stripped = text.strip()
            # 心跳/注释帧：保活用，不是内容，丢掉
            if not stripped or stripped.startswith(":"):
                continue
            # 账号级错误帧（_stream 把 HTTPException yield 成 error chunk）：
            # 不缓冲、直接当异常换号，别让它假成功混过闸门。
            err = hooks._account_error_from_frame(stripped)
            if err is not None:
                return err
            buffered.append(text)
            return hooks._GatedStream(buffered, gen)
        # 没有任何语义事件就结束：空流假成功，当错误交调用方换号
        return hooks.HTTPException(status_code=502, detail="trae stream returned no content")
    except hooks.HTTPException as e:
        return e
    except Exception as e:  # noqa: BLE001 — 读线程透传的原始异常
        return e


def _replay_prefixed(buffered: list[str], gen: Any) -> Any:
    """先补放闸门缓冲的语义帧，再续跑在途生成器（不重开，防二次消费）。

    同步生成器（与 ``_stream`` 同型）；补放后直接 ``for`` 续跑在途迭代器，
    不重开（重开会二次消费上游 / 触发二次计费）。
    """
    from . import provider as hooks
    for piece in buffered:
        yield piece
    for piece in gen:
        yield piece


class _GatedStream:
    """闸门结果：``buffered`` 是预驱动期间攒下的**语义**事件（心跳已丢），
    ``gen`` 是在途生成器（committed 后续跑它，不重开）。"""

    def __init__(self, buffered: list[str], gen: Any) -> None:
        self.buffered = buffered
        self.gen = gen
