"""Kiro 上游响应：AWS EventStream 二进制解析 + 转成 OpenAI/Anthropic 事件流。

## 为什么不能当 SSE 处理

Kiro 的 ``generateAssistantResponse`` 回的是 **AWS EventStream**（vnd.amazon.eventstream），
一种带长度前缀和 CRC 的二进制分帧格式，**不是** ``data: ...`` 文本行。直接按 SSE
读会得到一堆乱码。

## 帧格式

每帧（大端整数）::

    [4B 总长][4B headers 长度][4B prelude CRC][headers][payload][4B 消息 CRC]

headers 里 ``:event-type`` 决定 payload 语义（``:message-type: event``）：

- ``assistantResponseEvent`` → ``{"content": "..."}``  正文增量
- ``contextUsageEvent``       → ``{"contextUsagePercentage": 2.05}``
- ``meteringEvent``           → ``{"unit":"credit","usage":0.0053}``  计费
- ``error``                   → 上游错误（应在此之前被 HTTP 状态码挡住）

## 输出

本模块把事件流转成两种协议的标准 SSE：

- :func:`to_openai_events` → ``chat.completion.chunk``
- :func:`to_anthropic_events`` → ``content_block_delta`` 等 Anthropic 事件序列

两种都是**增量**输出：拿到一帧就立刻产出，不等整轮结束——首字节体验与上游一致。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Iterator

#: EventStream 帧头固定长度（prelude）。
_PRELUDE_LEN = 12

#: 单帧合理上界（16 MiB）。超过它只能是把脏数据当成了长度前缀——真实事件
#: 都是几 KB（整轮对话通常 <1 MB），这个上界宽到不会误伤任何合法帧。
_MAX_FRAME = 16 * 1024 * 1024


class StreamError(RuntimeError):
    """上游事件流里的 ``error`` 帧（或流结构损坏）。"""


def _parse_headers(raw: bytes) -> dict[str, str]:
    """解析帧头。只取字符串型（type=7）的字段，其余跳过。

    帧头编码：``[1B 名字长][名字][1B 类型][值]``，字符串型的值前有 2B 长度。
    非字符串类型（bool/int/bytes）本通道用不到，直接不解析——但必须能正确
    **跳过**，否则后面所有 header 都会读歪。
    """
    out: dict[str, str] = {}
    i = 0
    total = len(raw)
    while i < total:
        name_len = raw[i]
        i += 1
        if i + name_len > total:
            break
        name = raw[i:i + name_len].decode("utf-8", "replace")
        i += name_len
        if i >= total:
            break
        vtype = raw[i]
        i += 1
        if vtype == 7:  # string
            if i + 2 > total:
                break
            vlen = int.from_bytes(raw[i:i + 2], "big")
            i += 2
            if i + vlen > total:
                break
            out[name] = raw[i:i + vlen].decode("utf-8", "replace")
            i += vlen
        elif vtype in (0, 1):  # bool true/false：无附加值
            out[name] = "true" if vtype == 0 else "false"
        elif vtype == 2:  # byte
            i += 1
        elif vtype in (4, 5, 8):  # int16/int32/int64
            i += {4: 2, 5: 4, 8: 8}[vtype]
        elif vtype in (6, 9):  # byte array / string array：2B 长度
            if i + 2 > total:
                break
            blen = int.from_bytes(raw[i:i + 2], "big")
            i += 2 + blen
        else:
            break  # 未知类型：停在这里比读歪更安全
    return out


def iter_frames(stream: Iterable[bytes]) -> Iterator[tuple[dict[str, str], bytes]]:
    """从字节流里切出完整帧，逐帧 ``(headers, payload)``。

    上层给的往往是任意切的 chunk（TCP 分包），所以这里必须自己累积缓冲、
    按总长切分：一个逻辑帧可能横跨多个 chunk，也可能一个 chunk 里有多帧。
    """
    buf = b""
    for chunk in stream:
        if not chunk:
            continue
        buf += chunk
        while len(buf) >= _PRELUDE_LEN:
            total = int.from_bytes(buf[0:4], "big")
            hlen = int.from_bytes(buf[4:8], "big")
            # 明显的脏数据：总长小于最小帧、超过合理上界、或 header 长度越界。
            # 丢掉首字节重新找帧边界，避免整条流卡死。
            #
            # ⚠ 上界那个判断是必需的：脏字节恰好是 0xFF 时，读出来的总长会是
            # 4294967295（≈4GB），只判「过小」会把它当成「还没收全」而永远等下去。
            if (total < _PRELUDE_LEN + 4 or total > _MAX_FRAME
                    or _PRELUDE_LEN + hlen + 4 > total):
                buf = buf[1:]
                continue
            if len(buf) < total:
                break  # 还没收全，等下一个 chunk
            headers = _parse_headers(buf[_PRELUDE_LEN:_PRELUDE_LEN + hlen])
            payload = buf[_PRELUDE_LEN + hlen:total - 4]
            buf = buf[total:]
            yield headers, payload
    # 收尾残留：不报错（上游正常结束时不应该有残留，但半帧不该让整轮炸掉）


def _payload_json(payload: bytes) -> dict[str, Any]:
    try:
        data = json.loads(payload.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def iter_events(stream: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    """把帧流成对地转成 ``{"type": ..., "text": ..., ...}`` 事件。

    统一成中间形状，两个协议的输出函数都只需消费这一种。
    """
    for headers, payload in iter_frames(stream):
        event_type = headers.get(":event-type", "")
        data = _payload_json(payload)
        if event_type == "assistantResponseEvent":
            text = data.get("content")
            if isinstance(text, str) and text:
                yield {"type": "text", "text": text}
        elif event_type == "meteringEvent":
            usage = data.get("usage")
            if isinstance(usage, (int, float)) and not isinstance(usage, bool):
                yield {"type": "credit", "credit": float(usage)}
        elif event_type == "contextUsageEvent":
            pct = data.get("contextUsagePercentage")
            if isinstance(pct, (int, float)) and not isinstance(pct, bool):
                yield {"type": "context", "percent": float(pct)}
        elif event_type == "toolUseEvent":
            parsed = _tool_use_event(data)
            if parsed is not None:
                yield parsed
        elif event_type in ("error", "exception"):
            message = data.get("message") or data.get("reason") or "upstream stream error"
            raise StreamError(str(message)[:300])
        # 其它事件（如 codeReferenceEvent）忽略：客户端用不到


# ---------------------------------------------------------------------------
# 转 OpenAI chat.completion.chunk
# ---------------------------------------------------------------------------

_THINK_OPEN = re.compile(r"<think>")


def to_openai_events(stream: Iterable[bytes], *, model: str, request_id: str,
                     created: int) -> Iterator[str]:
    """事件流 → OpenAI SSE（``data: {...}\\n\\n``）。

    与其它通道口径一致：正文直接放 ``delta.content``；上游若用 ``<think>``
    内联思考（Kiro 目前不用，但别假设），这里**不剥离**——剥离是协议转换层的
    事，塞在这里会让两处逻辑打架。
    """
    first = True
    for event in iter_events(stream):
        if event["type"] != "text":
            continue
        delta: dict[str, Any] = {"content": event["text"]}
        if first:
            delta["role"] = "assistant"
            first = False
        yield "data: " + json.dumps({
            "id": request_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }, ensure_ascii=False) + "\n\n"
    # 收尾：先给 finish_reason，再给 [DONE]（缺 finish 的客户端会当成截断）
    yield "data: " + json.dumps({
        "id": request_id, "object": "chat.completion.chunk",
        "created": created, "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }, ensure_ascii=False) + "\n\n"
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# 转 Anthropic messages 事件流
# ---------------------------------------------------------------------------


def to_anthropic_events(stream: Iterable[bytes], *, model: str,
                        message_id: str) -> Iterator[str]:
    """事件流 → Anthropic SSE。

    事件序列（Anthropic 规范要求按这个顺序，缺一段客户端就断开）：

    ``message_start`` → ``content_block_start`` → N× ``content_block_delta``
    → ``content_block_stop`` → ``message_delta`` → ``message_stop``
    """
    def ev(name: str, payload: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield ev("message_start", {"type": "message_start", "message": {
        "id": message_id, "type": "message", "role": "assistant",
        "model": model, "content": [], "stop_reason": None,
        "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0},
    }})
    yield ev("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}})
    output_chars = 0
    for event in iter_events(stream):
        if event["type"] != "text":
            continue
        output_chars += len(event["text"])
        yield ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "text_delta",
                                                   "text": event["text"]}})
    yield ev("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield ev("message_delta", {"type": "message_delta", "delta": {
        "stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": max(1, output_chars // 4)}})
    yield ev("message_stop", {"type": "message_stop"})


def take_frame(buf: bytes, *, final: bool = False
               ) -> tuple[int, dict[str, str], bytes] | None:
    """从缓冲区头部取出**一个完整帧**，返回 ``(消耗字节数, headers, payload)``。

    不够一帧返回 ``None``（调用方继续收数据）。这是给「增量喂 chunk」的场景
    用的：比 :func:`iter_frames` 多一个「缓冲区由调用方持有」的语义，避免整条
    流攒在内存里才能开始解析。

    ``final=True`` 时对脏数据更宽容：结构明显不对就丢弃一个字节重新对齐
    （避免流末尾的半帧卡住调用方）。
    """
    if len(buf) < _PRELUDE_LEN:
        return None
    total = int.from_bytes(buf[0:4], "big")
    hlen = int.from_bytes(buf[4:8], "big")
    if (total < _PRELUDE_LEN + 4 or total > _MAX_FRAME
            or _PRELUDE_LEN + hlen + 4 > total):
        # 脏数据：丢掉首字节重新找帧边界（上界见 _MAX_FRAME 的说明）
        return 1, {}, b""
    if len(buf) < total:
        return None
    headers = _parse_headers(buf[_PRELUDE_LEN:_PRELUDE_LEN + hlen])
    payload = buf[_PRELUDE_LEN + hlen:total - 4]
    return total, headers, payload


def _tool_use_event(data: dict[str, Any]) -> dict[str, Any] | None:
    """``toolUseEvent`` → 中间事件。

    上游把一次工具调用**拆成多帧**增量下发（实测）::

        {"name":"get_weather","toolUseId":"tooluse_XXX"}
        {"input":"","name":"get_weather","toolUseId":"tooluse_XXX"}
        {"input":"{\\"city\\"",...}
        {"input":": \\"北京\\"}",...}
        {"name":"get_weather","stop":true,"toolUseId":"tooluse_XXX"}

    首帧只给 name/id，中间帧按 ``input`` 拼参数（**是 JSON 字符串的碎片**，
    不能逐帧 json.loads），末帧 ``stop: true`` 表示结束。

    这里不拼装，只把碎片如实抛出——
    拼装状态（累计 input 字符串）由调用方持有，因为它还要跨事件跟踪多个
    并发工具调用。
    """
    tool_id = data.get("toolUseId")
    if not isinstance(tool_id, str) or not tool_id:
        return None
    name = data.get("name")
    event: dict[str, Any] = {"type": "tool", "tool_id": tool_id}
    if isinstance(name, str) and name:
        event["name"] = name
    chunk = data.get("input")
    if isinstance(chunk, str) and chunk:
        event["input"] = chunk
    if data.get("stop") is True:
        event["stop"] = True
    return event


def frame_to_event(headers: dict[str, str], payload: bytes) -> dict[str, Any] | None:
    """单帧 → 中间事件形状；与本模块 :func:`iter_events` 同语义。

    空 headers（``take_frame`` 判定为脏数据）返回 ``None``，由调用方跳过。
    """
    if not headers:
        return None
    event_type = headers.get(":event-type", "")
    data = _payload_json(payload)
    if event_type == "assistantResponseEvent":
        text = data.get("content")
        return {"type": "text", "text": text} if isinstance(text, str) and text else None
    if event_type == "meteringEvent":
        usage = data.get("usage")
        if isinstance(usage, (int, float)) and not isinstance(usage, bool):
            return {"type": "credit", "credit": float(usage)}
        return None
    if event_type == "contextUsageEvent":
        pct = data.get("contextUsagePercentage")
        if isinstance(pct, (int, float)) and not isinstance(pct, bool):
            return {"type": "context", "percent": float(pct)}
        return None
    if event_type == "toolUseEvent":
        return _tool_use_event(data)
    if event_type in ("error", "exception"):
        message = data.get("message") or data.get("reason") or "upstream stream error"
        raise StreamError(str(message)[:300])
    return None
