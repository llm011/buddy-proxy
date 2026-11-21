"""上游没给出内容时，合成一条**可读的兜底回复**而不是 502。

## 为什么要合成

客户端（DSH TUI 等）对两类失败的处理完全不同：

* **空回复**（HTTP 200 但内容为空）：dsh 归类 ``EMPTY_RESPONSE`` 并**自动重试**，
  重试几次后才中止整轮；
* **502**：直接 ``turn error``，**不重试**，用户看到的就是中断。

而我们的现实是：``gemini-4-hydrogen`` 的候选上游偶发空流（``finishReason=n/a``）、
或者两个候选同时进冷却。此时返回 502 让整轮对话被打断；返回一句「没拿到回复、
可以减小上下文再试」反而让用户**知道发生了什么、下一步该做什么**，而且不打断
会话（下一轮用户可以直接继续，或按提示开新会话）。

## 只对「等一会儿可能好」的失败合成

不能对所有 502 都合成——那会把需要用户处理的问题掩盖掉：

* **合成**：候选全部处于冷却（等冷却结束可重试）、上游空内容（偶发抖动）；
* **照实报错**：被停用/不在可用时段（要用户去 /ui 改）、通道被风控拦截
  （要换通道）、认证失败（要重新登录）、编程错误。

判据是「这条错误是不是用户等一等就会自己好」。是 → 合成提示；不是 → 保留
原始 502，让真因可见。

## 为什么用括号

提示语整体用中文全角括号包起来（``（...）``），让它在对话记录里明显是
**系统提示**而不是模型正文——用户回看历史时不会误以为模型真的这么说过。
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from fastapi.responses import JSONResponse, StreamingResponse

#: 合成回复的固定文案。刻意写明「可减小上下文窗口再重试」：这类空流在超大
#: 上下文（实测 68 万 token）下更容易触发，压缩会话是最有效的自救手段。
EMPTY_REPLY_NOTICE = (
    "（模型问题未拿到回复，可以减小上下文窗口再重试）"
)

#: 冷却类失败额外提示还剩多久——用户据此判断「等一下」还是「现在换模型」。
_COOLDOWN_NOTE = "（候选上游均在冷却中，可稍等几十秒后重试）"


def _notice_text(reason: str) -> str:
    """按失败类型给一句可操作的话。未知类型回落到通用文案。"""
    if reason == "cooldown":
        return _COOLDOWN_NOTE
    return EMPTY_REPLY_NOTICE


def _openai_chunk(model: str, *, delta: dict[str, Any] | None = None,
                  finish: str | None = None) -> dict[str, Any]:
    choice: dict[str, Any] = {"index": 0, "delta": delta or {}}
    if finish:
        choice["finish_reason"] = finish
    return {
        "id": f"chatcmpl-{secrets.token_hex(12)}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
    }


def _usage(model: str) -> dict[str, Any]:
    """合成的回复没有真实 token 消耗，但客户端普遍要求 usage 字段存在。

    填 0 而不是编一个数：编数会污染面板的 token 统计和积分估算。
    """
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def synthesize_reply(
    model: str,
    protocol: str,
    *,
    reason: str,
    original: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> StreamingResponse | JSONResponse:
    """合成一条含提示语的「正常回复」，替代 502。

    ``reason`` 目前认 ``"cooldown"``（候选全在冷却）与其它（空内容等），
    只影响提示文案，不影响协议形状。

    ``body`` 是本次请求体，用来兜底判断流式：``original`` 在部分调用路径上是
    None（如 model_order 路径），只看它会把流式请求错答成非流式 JSON。
    """
    text = _notice_text(reason)
    stream = bool((original or {}).get("stream") or (body or {}).get("stream"))
    if not stream:
        body: dict[str, Any] = {
            "id": f"chatcmpl-{secrets.token_hex(12)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": _usage(model),
        }
        if protocol == "anthropic":
            from ..protocols.anthropic_adapter import chat_completion_to_anthropic_message

            return JSONResponse(content=chat_completion_to_anthropic_message(body, original))
        return JSONResponse(content=body)

    if protocol == "anthropic":
        return StreamingResponse(
            _anthropic_stream(model, text),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "close"},
        )
    return StreamingResponse(
        _openai_stream(model, text),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "close"},
    )


async def _openai_stream(model: str, text: str):
    """OpenAI chat.completion.chunk 流：一条 content delta + 收尾 + [DONE]。"""
    yield _sse(_openai_chunk(model, delta={"role": "assistant", "content": text}))
    yield _sse(_openai_chunk(model, finish="stop"))
    yield "data: [DONE]\n\n"


async def _anthropic_stream(model: str, text: str):
    """Anthropic Messages 事件流：走全局转换器，保证事件序列与真实流一致。"""
    from ..protocols.anthropic_adapter import AnthropicStreamConverter

    converter = AnthropicStreamConverter(model)
    for name, payload in converter.feed_chunk(
            _openai_chunk(model, delta={"role": "assistant", "content": text})):
        yield _sse_event(name, payload)
    for name, payload in converter.feed_chunk(_openai_chunk(model, finish="stop")):
        yield _sse_event(name, payload)
    for name, payload in converter.finish():
        yield _sse_event(name, payload)


def _sse(chunk: dict[str, Any]) -> str:
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _sse_event(name: str, payload: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
