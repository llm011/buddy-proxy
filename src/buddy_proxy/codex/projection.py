"""Chat Completions ⇄ OpenAI Responses 双向投影。

仓库里其它通道的上游都是 Chat Completions，唯独 Codex 后端是 **Responses**
协议（``POST https://chatgpt.com/backend-api/codex/responses``，SSE 事件流）。
而代理内部统一用 chat body 转发，因此本模块负责两个方向的翻译：

**请求方向**（:func:`chat_to_responses_request`）
    chat ``messages`` / ``tools`` / ``tool_choice`` / ``max_tokens``
    → Responses ``instructions`` / ``input`` / ``tools``

**响应方向**
    - 流式：:class:`ResponsesToChatStream` 把上游 SSE 事件折叠成 chat
      ``chat.completion.chunk``（``delta.content`` / ``delta.tool_calls``）
    - 非流式：:func:`responses_to_chat_completion` 把最终 response 对象折叠成
      ``chat.completion``

> 注意方向：``protocols/responses_adapter.py`` 是**反方向**（Responses→Chat 请求 /
> Chat SSE→Responses 事件），用于把 Codex 当客户端接别家后端。两者不通用，别混用。

Responses 事件流的形状（上游实际返回，取本次要消费的部分）::

    response.created
    response.in_progress
    response.output_item.added      {item: {type: "reasoning"|"message"|"function_call", ...}}
    response.reasoning_summary_text.delta   {delta}
    response.output_text.delta     {delta, item_id, output_index, content_index}
    response.function_call_arguments.delta  {delta, item_id, output_index}
    response.output_item.done      {item: {...完整 item}}
    response.completed             {response: {usage, output, ...}}
    response.failed / response.incomplete
    error                          {code, message}
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, AsyncIterator, Iterable, Iterator


def _rand_id(prefix: str = "") -> str:
    return prefix + os.urandom(12).hex()


def _now_s() -> int:
    return int(time.time())


# ---------------------------------------------------------------------------
# 请求方向：chat → Responses
# ---------------------------------------------------------------------------

#: chat 里表示「思考中」的字段名（各家用得不一样），统一映射到 reasoning summary。
_REASONING_MESSAGE_KEYS = ("reasoning_content", "reasoning", "thinking")


def _text_of(content: Any) -> str:
    """把 chat 的 content（str / parts 数组）压成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                out.append(str(part.get("text") or ""))
        return "".join(out)
    return ""


def _chat_images(content: Any) -> list[dict[str, Any]]:
    """提取 chat content 里的图片 parts，转成 Responses 的 input_image。"""
    images: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return images
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "image_url":
            continue
        image = part.get("image_url")
        url = image.get("url") if isinstance(image, dict) else image
        if isinstance(url, str) and url.strip():
            images.append({"type": "input_image", "image_url": url.strip()})
    return images


def _user_content(content: Any) -> list[dict[str, Any]] | str:
    """user 消息 content → Responses input_content 数组（纯文本时保持字符串）。"""
    images = _chat_images(content)
    if not images:
        return _text_of(content)
    parts: list[dict[str, Any]] = []
    text = _text_of(content)
    if text:
        parts.append({"type": "input_text", "text": text})
    parts.extend(images)
    return parts


def _assistant_content(content: Any) -> list[dict[str, Any]] | str:
    images = _chat_images(content)
    if not images:
        return _text_of(content)
    parts: list[dict[str, Any]] = []
    text = _text_of(content)
    if text:
        parts.append({"type": "output_text", "text": text})
    parts.extend(images)
    return parts


def _convert_tools(tools: Any) -> list[dict[str, Any]]:
    """chat tools（``{type:function, function:{...}}``）→ Responses tools（扁平）。"""
    out: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        kind = tool.get("type")
        if kind == "function":
            fn = tool.get("function")
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if not name:
                continue
            out.append({
                "type": "function",
                "name": name,
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                # Responses 用 strict 字段做 schema 严格校验；chat 一般不带，默认关
                "strict": bool(fn.get("strict", False)),
            })
        elif kind in ("web_search", "web_search_preview"):
            out.append({"type": "web_search"})
    return out


def _convert_tool_choice(choice: Any) -> Any:
    """chat tool_choice → Responses tool_choice。"""
    if isinstance(choice, str):
        return choice if choice in ("auto", "none", "required") else "auto"
    if isinstance(choice, dict):
        fn = choice.get("function")
        name = fn.get("name") if isinstance(fn, dict) else None
        if name:
            return {"type": "function", "name": name}
    return "auto"


def _convert_messages(messages: Any) -> tuple[str, list[dict[str, Any]]]:
    """chat messages → ``(instructions, input_items)``。

    - ``system`` / ``developer`` → 合并进 ``instructions``（Responses 没有 system 角色）
    - ``user`` → ``{type: message, role: user, content: [...]}``
    - ``assistant`` → ``{type: message, role: assistant, ...}`` + 各自的
      ``function_call`` items（tool_calls）
    - ``tool`` → ``{type: function_call_output, call_id, output}``
    """
    system_texts: list[str] = []
    items: list[dict[str, Any]] = []
    if not isinstance(messages, list):
        return "", items

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content")

        if role in ("system", "developer"):
            text = _text_of(content)
            if text:
                system_texts.append(text)
            continue

        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(msg.get("tool_call_id") or ""),
                "output": _text_of(content),
            })
            continue

        if role == "assistant":
            # 思考内容（reasoning_content 等）不往上游回灌：Responses 的
            # reasoning item 需要 encrypted_content 签名，凭空造的会被拒。
            text = _text_of(content)
            if text:
                items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": _assistant_content(content),
                })
            for call in msg.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                if not isinstance(fn, dict):
                    continue
                name = fn.get("name")
                if not name:
                    continue
                items.append({
                    "type": "function_call",
                    "call_id": str(call.get("id") or ""),
                    "name": name,
                    "arguments": fn.get("arguments") or "{}",
                })
            continue

        # user 及其它角色一律按 user 处理
        text = _text_of(content)
        extra_images = _chat_images(content)
        if not text and not extra_images:
            continue
        items.append({
            "type": "message",
            "role": "user",
            "content": _user_content(content),
        })

    return "\n\n".join(system_texts), items


def _reasoning_config(body: dict[str, Any]) -> dict[str, Any] | None:
    """chat 的 reasoning_effort / reasoning → Responses ``reasoning`` 对象。"""
    effort = body.get("reasoning_effort")
    if not effort:
        reasoning = body.get("reasoning")
        if isinstance(reasoning, dict):
            effort = reasoning.get("effort")
    if not isinstance(effort, str) or not effort.strip():
        return None
    return {"effort": effort.strip(), "summary": "auto"}


def chat_to_responses_request(
    body: dict[str, Any], *, model: str | None = None, stream: bool = True,
) -> dict[str, Any]:
    """chat 请求体 → Responses 请求体。

    ``model`` 传上游模型名（覆盖 body 里的对外名）。
    """
    instructions, items = _convert_messages(body.get("messages"))
    upstream: dict[str, Any] = {
        "model": model or str(body.get("model") or ""),
        "input": items,
        "stream": bool(stream),
    }
    # Claude Code / Codex CLI 进来时原本就带 instructions：它更精确，放前面；
    # 但 system 消息**不能丢**——例如 anthropic 入口把客户端 ``system`` 转成
    # system message，而同一份 body 又带着别人的 instructions 时，只取
    # instructions 会把系统提示整段吞掉。两者都在就按「instructions + 空行 +
    # system」拼接。
    explicit = body.get("instructions")
    parts: list[str] = []
    if isinstance(explicit, str) and explicit.strip():
        parts.append(explicit.strip())
    if instructions and instructions.strip() and instructions.strip() != (parts[0] if parts else ""):
        parts.append(instructions.strip())
    # Codex 后端对 instructions 字段的存在性敏感，缺失时补空串（CLIProxyAPI 同款）
    upstream["instructions"] = "\n\n".join(parts)

    tools = _convert_tools(body.get("tools"))
    if tools:
        upstream["tools"] = tools
        upstream["tool_choice"] = _convert_tool_choice(body.get("tool_choice"))
        if body.get("parallel_tool_calls") is not None:
            upstream["parallel_tool_calls"] = bool(body["parallel_tool_calls"])

    reasoning = _reasoning_config(body)
    if reasoning:
        upstream["reasoning"] = reasoning

    # chat 用 max_tokens，Responses 用 max_output_tokens
    max_tokens = body.get("max_completion_tokens") or body.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        upstream["max_output_tokens"] = max_tokens

    for key in ("temperature", "top_p", "user", "service_tier"):
        if body.get(key) is not None:
            upstream[key] = body[key]

    # chat 的 stop 是数组；Responses 不支持 stop，丢弃（强行塞会被 400）
    return upstream


# ---------------------------------------------------------------------------
# 非流式：Responses response → chat.completion
# ---------------------------------------------------------------------------

def _output_text(item: dict[str, Any]) -> str:
    """从一个 message item 里拼出文本。"""
    content = item.get("content")
    if isinstance(content, str):
        return content
    out: list[str] = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind in ("output_text", "text", "input_text"):
                out.append(str(part.get("text") or ""))
            elif kind == "refusal":
                out.append(str(part.get("refusal") or ""))
    return "".join(out)


def _reasoning_text(item: dict[str, Any]) -> str:
    """从 reasoning item 里拼出思考摘要。"""
    out: list[str] = []
    summary = item.get("summary")
    if isinstance(summary, list):
        for part in summary:
            if isinstance(part, dict) and part.get("type") in ("summary_text", "text"):
                out.append(str(part.get("text") or ""))
    content = item.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("reasoning_text", "text"):
                out.append(str(part.get("text") or ""))
    return "".join(out)


def _map_usage(usage: Any) -> dict[str, Any]:
    """Responses usage → chat usage。"""
    if not isinstance(usage, dict):
        return {}
    prompt = usage.get("input_tokens")
    completion = usage.get("output_tokens")
    total = usage.get("total_tokens")
    if total is None and isinstance(prompt, int) and isinstance(completion, int):
        total = prompt + completion
    mapped: dict[str, Any] = {}
    if isinstance(prompt, int):
        mapped["prompt_tokens"] = prompt
        details = usage.get("input_tokens_details")
        if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
            mapped["prompt_tokens_details"] = {"cached_tokens": details["cached_tokens"]}
    if isinstance(completion, int):
        mapped["completion_tokens"] = completion
        details = usage.get("output_tokens_details")
        if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
            mapped["completion_tokens_details"] = {
                "reasoning_tokens": details["reasoning_tokens"]}
    if isinstance(total, int):
        mapped["total_tokens"] = total
    return mapped


def responses_to_chat_completion(
    response: dict[str, Any], *, model: str, created: int | None = None,
) -> dict[str, Any]:
    """最终 response 对象 → chat.completion（非流式）。"""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "message":
                text_parts.append(_output_text(item))
            elif kind == "reasoning":
                reasoning_parts.append(_reasoning_text(item))
            elif kind == "function_call":
                name = item.get("name") or ""
                tool_calls.append({
                    "id": str(item.get("call_id") or item.get("id") or _rand_id("call_")),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": str(item.get("arguments") or "{}"),
                    },
                })

    text = "".join(text_parts)
    reasoning = "".join(reasoning_parts)

    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if reasoning and not text:
        # 只有思考没有正文：把思考塞进 content，别让客户端看到空消息
        message["content"] = reasoning
        reasoning = ""
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = tool_calls

    finish_reason = "tool_calls" if tool_calls else "stop"
    status = response.get("status")
    if status == "incomplete":
        reason = (response.get("incomplete_details") or {}).get("reason")
        finish_reason = "length" if reason == "max_output_tokens" else "stop"

    completion: dict[str, Any] = {
        "id": str(response.get("id") or _rand_id("chatcmpl-")),
        "object": "chat.completion",
        "created": created if created is not None else _now_s(),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
    }
    usage = _map_usage(response.get("usage"))
    if usage:
        completion["usage"] = usage
    return completion


# ---------------------------------------------------------------------------
# 流式：Responses SSE 事件 → chat.completion.chunk
# ---------------------------------------------------------------------------

class ResponsesToChatStream:
    """把 Responses 事件流折叠成 chat 流式 chunk。

    用法::

        conv = ResponsesToChatStream(model="gpt-6.1-sol")
        for event in sse_events:
            for chunk in conv.feed(event):
                yield chunk
        for chunk in conv.finish():
            yield chunk

    设计取舍：思考摘要（``reasoning_summary_text.delta``）累积到
    ``delta.reasoning_content`` 一次性推 —— 与仓库其它通道对
    ``reasoning_content`` 的处理口径一致，前端不必为此加分叉。
    """

    def __init__(self, model: str, *, created: int | None = None):
        self.id = _rand_id("chatcmpl-")
        self.model = model
        self.created = created if created is not None else _now_s()
        self.started = False
        self.finished = False
        self._reasoning_buf: list[str] = []
        self._reasoning_sent = False
        self._tool_index: dict[str, int] = {}
        self._tool_names: dict[str, str] = {}
        self._next_tool_index = 0
        self._tool_buf: dict[str, list[str]] = {}
        self._usage: dict[str, Any] = {}
        self._finish_reason: str = "stop"

    # -- 内部 --

    def _chunk(self, delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def _start(self) -> list[dict[str, Any]]:
        if self.started:
            return []
        self.started = True
        return [self._chunk({"role": "assistant", "content": ""})]

    # -- 公开 --

    def feed(self, event_name: str, event: dict[str, Any]) -> list[dict[str, Any]]:
        """消费一个上游事件，返回要下发的 chat chunk 列表。"""
        if self.finished:
            return []
        out: list[dict[str, Any]] = []

        if event_name == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                out.extend(self._start())
                out.append(self._chunk({"content": delta}))
            return out

        if event_name == "response.reasoning_summary_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                self._reasoning_buf.append(delta)
            return out

        if event_name == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                out.extend(self._start())
                out.extend(self._open_tool(item))
            return out

        if event_name == "response.function_call_arguments.delta":
            item_id = str(event.get("item_id") or "")
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                self._tool_buf.setdefault(item_id, []).append(delta)
            return out

        if event_name == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                out.extend(self._close_tool(item))
            return out

        if event_name == "response.completed":
            response = event.get("response") or event
            if isinstance(response, dict):
                self._usage = _map_usage(response.get("usage"))
            self._finish_reason = "tool_calls" if self._tool_index else "stop"
            self.finished = True
            out.extend(self._start())
            out.extend(self._flush_reasoning())
            out.append(self._chunk({}, self._finish_reason))
            if self._usage:
                final = self._chunk({})
                final["usage"] = self._usage
                out.append(final)
            return out

        if event_name in ("response.failed", "response.incomplete"):
            self.finished = True
            self._finish_reason = "stop"
            out.extend(self._start())
            out.append(self._chunk({}, self._finish_reason))
            return out

        return out

    def _open_tool(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        item_id = str(item.get("id") or item.get("call_id") or _rand_id("fc_"))
        index = self._next_tool_index
        self._next_tool_index += 1
        self._tool_index[item_id] = index
        name = str(item.get("name") or "")
        self._tool_names[item_id] = name
        return [self._chunk({"tool_calls": [{
            "index": index,
            "id": str(item.get("call_id") or item_id),
            "type": "function",
            "function": {"name": name, "arguments": ""},
        }]})]

    def _close_tool(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        item_id = str(item.get("id") or item.get("call_id") or "")
        index = self._tool_index.get(item_id)
        if index is None:
            return []
        args = "".join(self._tool_buf.pop(item_id, []))
        if not args:
            args = str(item.get("arguments") or "")
        if not args:
            return []
        return [self._chunk({"tool_calls": [{
            "index": index,
            "function": {"arguments": args},
        }]})]

    def _flush_reasoning(self) -> list[dict[str, Any]]:
        if self._reasoning_sent or not self._reasoning_buf:
            return []
        self._reasoning_sent = True
        return [self._chunk({"reasoning_content": "".join(self._reasoning_buf)})]

    def finish(self) -> list[dict[str, Any]]:
        """上游流结束后兜底收尾（上游没发 response.completed 时用）。"""
        if self.finished:
            return []
        self.finished = True
        out = self._start()
        out.extend(self._flush_reasoning())
        out.append(self._chunk({}, self._finish_reason))
        if self._usage:
            final = self._chunk({})
            final["usage"] = self._usage
            out.append(final)
        return out


def is_error_event(event_name: str, event: dict[str, Any]) -> tuple[int, str]:
    """判断上游事件是否是错误，返回 ``(http_status, message)``；不是错误返回 ``(0, "")``。

    上游把限流/鉴权失败塞在 SSE 里（HTTP 仍是 200），必须在向客户端吐字节前
    识别出来，否则闸门形同虚设——这正是 codebuddy/trae 都踩过的坑。
    """
    if event_name == "error":
        code = event.get("code") or event.get("status")
        message = str(event.get("message") or event.get("detail") or "")
        return _status_from_code(code), message

    if event_name in ("response.failed", "response.incomplete"):
        response = event.get("response") or {}
        if not isinstance(response, dict):
            return 500, str(event)
        err = response.get("error")
        if isinstance(err, dict):
            code = err.get("code") or err.get("status")
            message = str(err.get("message") or "")
            return _status_from_code(code), message
        details = response.get("incomplete_details")
        if event_name == "response.incomplete" and isinstance(details, dict):
            reason = details.get("reason")
            if reason == "max_output_tokens":
                return 0, ""  # 正常截断，不是错误
            return 500, f"incomplete: {reason}"
    return 0, ""


def _status_from_code(code: Any) -> int:
    """上游错误码 → HTTP 状态码（映射不到一律 500）。"""
    if isinstance(code, int) and 400 <= code <= 599:
        return code
    text = str(code or "").lower()
    if any(k in text for k in ("rate_limit", "rate limit", "quota", "usage_limit")):
        return 429
    if any(k in text for k in ("unauthorized", "invalid_api_key", "auth")):
        return 401
    if "forbidden" in text or "permission" in text:
        return 403
    return 500


# ---------------------------------------------------------------------------
# SSE 解析
# ---------------------------------------------------------------------------

def iter_sse_events(
    lines: Iterable[str] | AsyncIterator[str],
) -> Iterator[tuple[str, dict[str, Any]]]:
    """同步版 SSE 行流解析（行已经是完整的 list / 同步迭代器时用）。

    async 行迭代器（httpx 的 ``aiter_lines()``）请用 :func:`aiter_sse_events`。
    """
    name = ""
    data_lines: list[str] = []

    def _flush() -> tuple[str, dict[str, Any]] | None:
        if not data_lines:
            return None
        raw = "\n".join(data_lines)
        if raw.strip() == "[DONE]":
            return None
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001 - 坏帧跳过，别炸整条流
            return None
        if not isinstance(payload, dict):
            return None
        return (name or str(payload.get("type") or "")), payload

    for line in lines:  # type: ignore[union-attr]
        if line == "" or line == "\r":
            flushed = _flush()
            if flushed is not None:
                yield flushed
            name = ""
            data_lines = []
            continue
        if line.startswith(":"):
            continue  # SSE 注释/心跳
        if line.startswith("event:"):
            name = line[len("event:"):].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())

    flushed = _flush()
    if flushed is not None:
        yield flushed


async def aiter_sse_events(
    lines: AsyncIterator[str],
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """异步版 SSE 行流解析——转发路径用这个。

    上游每帧形如::

        event: response.output_text.delta
        data: {"type": "response.output_text.delta", ...}

    ``event:`` 行缺省时回退到 payload 里的 ``type`` 字段；``[DONE]`` 结束。
    无法解析的帧跳过（上游偶尔会夹心跳/注释行，不该因此中断整条流）。
    """
    # 状态用 dict 而不是局部变量：``_flush`` 是闭包，若在 ``yield`` 之后
    # 重新绑定局部变量，闭包仍会持有旧对象，重置不生效（第二帧会顶着上一帧
    # 的 event 名）。dict 原地改字节的是同一个对象。
    state: dict[str, Any] = {"name": "", "data": []}

    def _flush() -> tuple[str, dict[str, Any]] | None:
        if not state["data"]:
            return None
        raw = "\n".join(state["data"])
        if raw.strip() == "[DONE]":
            return None
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(payload, dict):
            return None
        return (state["name"] or str(payload.get("type") or "")), payload

    async for line in lines:
        if line == "" or line == "\r":
            flushed = _flush()
            if flushed is not None:
                yield flushed
            state["name"] = ""
            state["data"] = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            state["name"] = line[len("event:"):].strip()
            continue
        if line.startswith("data:"):
            state["data"].append(line[len("data:"):].lstrip())

    flushed = _flush()
    if flushed is not None:
        yield flushed
