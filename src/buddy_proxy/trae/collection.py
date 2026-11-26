"""Trae non-stream SSE collection; streaming lives in provider_stream."""

from __future__ import annotations

import time
import uuid
from typing import Any

from .usage import normalize_token_usage


def collect_response(
    self, messages: list[dict[str, Any]], model: str, sanitize: bool = True,
    tools: list[dict[str, Any]] | None = None,
    native: dict[str, Any] | None = None,
    max_inject: bool = False,
) -> dict[str, Any]:
    """非流式：聚合 Trae SSE 成完整响应。

    ``max_inject`` 同 :meth:`_stream`：本次是否注入 ``is_max_mode``，forward
    决策后下传，贯穿 native 重试与文本协议兜底。
    """
    # Resolve facade hooks at call time so existing monkeypatches and PAT overrides survive.
    from . import provider as hooks

    reasoning_parts: list[str] = []
    content_parts: list[str] = []
    usage_real: dict[str, Any] | None = None

    if native is not None:
        if not self._keeps_native_error(model):
            # 同 _stream：最近有过 4001 就先安静等到窗外，别在惩罚窗内开火
            hooks._pace_after_reject(model)
        raw = self._send_native_request(
            native["messages"], model, stream=False, tools=native["tools"],
            max_mode=max_inject)
        used_native = True
        if hooks._native_rejected(raw) and not self._keeps_native_error(model):
            hooks.log.warning(
                "trae native tools rejected (4001), "
                "paced retry nonstream: model=%s", model)
            hooks._debug_dump("trae_native_fallback", model=model, phase="collect")
            # 同 _stream：先安静等到窗外原路重试一次，仍拒才落文本协议
            hooks._note_reject(model)
            hooks._pace_after_reject(model)
            raw = self._send_native_request(
                native["messages"], model, stream=False, tools=native["tools"],
                max_mode=max_inject)
        if hooks._native_rejected(raw) and not self._keeps_native_error(model):
            hooks.log.warning(
                "trae native nonstream rejected twice (4001), "
                "fallback to text protocol: model=%s", model)
            hooks._note_reject(model)
            hooks._pace_after_reject(model)
            raw = hooks.send_trae_chat(messages, model, stream=False,
                                 base_url=self._base_url, max_mode=max_inject)
            used_native = False
        if used_native:
            hooks._note_native_ok(model)
    else:
        raw = hooks.send_trae_chat(messages, model, stream=False,
                             base_url=self._base_url, max_mode=max_inject)
        used_native = False
    acc = hooks._NativeToolAccumulator() if used_native else None
    for event, data in hooks._parse_sse(raw):
        if event == "error":
            # 状态码按**上游码分类**（额度 4008/4011/4021/4031 → 429、
            # 鉴权 1001/4010 → 401、权益 1005 → 403），不能一律 502——forward
            # 的 failover 循环只认 401/403/429 为账号级错误，额度是账号级的，
            # A 号撞 4008 时 B 号可能还有，一律 502 会让 failover 直接透传不
            # 换号（2026-10-06 实测：双账号一空一满，测试按钮直接报 4008）。
            # 原始码挂到异常上：1005 要按「账号+模型」跳过而非整账号冷却。
            raise hooks._sse_error_exception(data)
        if event == "token_usage":
            u = data or {}
            usage_real = normalize_token_usage(u)
        if event == "output":
            if data.get("reasoning_content"):
                reasoning_parts.append(data["reasoning_content"])
            if data.get("response"):
                content_parts.append(data["response"])
            if acc is not None and data.get("tool_calls"):
                acc.feed(data["tool_calls"])

    reasoning = "".join(reasoning_parts)
    content = "".join(content_parts)
    # 无 tools 请求也必须有初始值——下方 hooks._debug_dump 无条件引用
    # （实测缺失时非流式无 tools 请求直接 UnboundLocalError -> internal error）
    tool_calls: list[dict[str, Any]] = []
    if used_native:
        # 原生通道：tool_calls 来自结构化事件；无 agent 预设，正文没有
        # 可泄漏的协议语法，不做解析/清洗
        tool_calls = acc.finish()
    else:
        # 顺序关键：带 tools 的请求必须先解析、后清洗——hooks._sanitize_agent_leak
        # 会把 <tool_call>/</arg_value> 标签全剥掉，先清洗再解析会让解析器
        # 拿到被拆掉结构的残骸（实测 glm-5.3 函数调用表达式因此整段漏进正文）
        if tools:
            content, tool_calls = hooks._parse_tool_calls(content, hooks._tool_names(tools))
        if sanitize:
            content = hooks._sanitize_agent_leak(content)
    hooks._debug_dump("debug_trae_response", model=model, stream=False, content=content,
                tool_calls=len(tool_calls))
    # 只有 tool_calls 没有正文也是合法响应（agent 直接发起调用），
    # 不注入空响应兜底文案——否则会混进 Anthropic tool_use 消息正文
    if not content and not reasoning and not tool_calls:
        content = "(trae upstream 返回了空响应，未产生任何内容)"
    # Keep reasoning on the wire, not in display text: tool continuations need
    # message.reasoning_content verbatim, including reasoning-only responses.

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
                **({"reasoning_content": reasoning} if reasoning else {}),
                **({"tool_calls": tool_calls} if tool_calls else {}),
            },
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        # 原生通道带真实 token_usage；文本协议上游不吐 usage，沿用旧启发式
        "usage": usage_real or {
            "prompt_tokens": 0,
            "completion_tokens": max(1, len(content.encode("utf-8")) // 4),
            "total_tokens": 0,
        },
    }
