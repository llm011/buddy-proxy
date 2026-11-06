"""上游转发管线：SSE 流式/聚合转发与协议转换（OpenAI/Anthropic/Responses）。

- 顶层：可选高级功能模块（脱敏 / 投影 / responses / anthropic 适配器）的
  try-import 与降级实现
- ``stream_upstream``：流式转发（DSML 清洗、原生 tool_calls 修复、协议事件转换、
  空闲/总时长双重超时）
- ``collect_upstream``：聚合上游流式响应为单个 OpenAI JSON（非流式场景）
- ``convert_nonstream``：聚合结果转目标协议格式

依赖 observability（响应日志）；被 provider（forward 分派）依赖。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import httpx
from fastapi import HTTPException

from buddy_proxy.protocols.dsml_parser import DSMLStreamBuffer
from buddy_proxy.core.logging_setup import now_s
from buddy_proxy.core.state import (
    diagnostic,
    get_state,
)

from .observability import log_upstream_response

# 可选高级功能模块（脱敏 / 投影 / 协议转换器）
try:
    from buddy_proxy.core.desensitize import desensitize_body
    HAS_DESENSITIZE = True
except ImportError:
    HAS_DESENSITIZE = False

    def desensitize_body(body, **kwargs):
        return body


try:
    from buddy_proxy.protocols.responses_projection import project_responses_chat_body
    HAS_PROJECTION = True
except ImportError:
    HAS_PROJECTION = False

    def project_responses_chat_body(body):
        return body, {}


try:
    from buddy_proxy.protocols.responses_adapter import responses_request_to_chat, ResponsesStreamConverter
    HAS_RESPONSES_ADAPTER = True
except ImportError:
    HAS_RESPONSES_ADAPTER = False

    def responses_request_to_chat(body):
        raise RuntimeError("responses_adapter not available - cannot convert /v1/responses requests")

    ResponsesStreamConverter = None


try:
    from buddy_proxy.protocols.anthropic_adapter import (
        AnthropicStreamConverter,
        anthropic_to_chat,
        chat_completion_to_anthropic_message,
    )
    HAS_ANTHROPIC_ADAPTER = True
except ImportError:
    HAS_ANTHROPIC_ADAPTER = False

    def anthropic_to_chat(body):
        raise RuntimeError("anthropic_adapter not available - cannot convert /v1/messages requests")

    def chat_completion_to_anthropic_message(data, original=None):
        raise RuntimeError("anthropic_adapter not available - cannot convert to anthropic format")

    AnthropicStreamConverter = None



def _build_openai_flush_chunk(
    residual: str, last_chunk_id: str, last_chunk_created: int, last_chunk_model: str
) -> dict:
    """构造流结束 flush 时发出的 OpenAI ChatCompletionChunk。

    必须补全顶层 id/object/created/model 字段，否则严格解析的客户端
    （如 grok）会报 ``serialization error: missing field id``。
    """
    return {
        "id": last_chunk_id,
        "object": "chat.completion.chunk",
        "created": last_chunk_created,
        "model": last_chunk_model,
        "choices": [{
            "index": 0,
            "delta": {"content": residual},
        }],
    }


def _build_dsml_tool_call_deltas(
    chunk_tool_calls: list[dict], start_index: int
) -> tuple[list[dict], int]:
    """DSML 兜底注入的流式 tool_calls 分片，index 跨 chunk 自增。

    每 chunk 从 0 重排会让 AnthropicStreamConverter 把两次调用
    合并进同一个 tool_use 槽位（它按 index 开槽），arguments 互相污染。
    """
    deltas = [
        {
            "index": start_index + i,
            "id": call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": call["function"]["name"],
                "arguments": call["function"]["arguments"],
            },
        }
        for i, call in enumerate(chunk_tool_calls)
    ]
    return deltas, start_index + len(chunk_tool_calls)


#: 上游读超时（秒）。``httpx.Timeout(read=...)`` 是**两次读之间**的最大间隔，
#: 不是总时长上限；总时长另有 ``MAX_TOTAL_DURATION``（默认 30 分钟）兜住。
#:
#: 2026-11-07 实测：dsh TUI 一条长上下文请求（28 个工具定义 + 几十条消息）从
#: 发出到上游吐出**首个**字节可超过 300 秒，而这里读超时是 300 秒 → ``ReadTimeout``
#: 被当成传输层失败，错误直接冒到客户端（形态与渠道拦截的 400 相似，但根因不同）。
#: 放宽到与总时长上限同口径：慢启动交给总时长管，读超时只防「卡死不动」。
#:
#: 环境变量 ``BUDDY_UPSTREAM_READ_TIMEOUT`` 可覆盖（秒，>0），用于按需调回。
def _env_read_timeout() -> float:
    """读 ``BUDDY_UPSTREAM_READ_TIMEOUT``；缺省/非法/非正数一律回落到 1800 秒。"""
    try:
        value = float(os.environ.get("BUDDY_UPSTREAM_READ_TIMEOUT") or 1800)
    except ValueError:
        return 1800.0
    return value if value > 0 else 1800.0


UPSTREAM_READ_TIMEOUT_S = _env_read_timeout()
#: 建连超时，保持不变（连不上是另一类故障，尽快暴露更能提示换档）。
UPSTREAM_CONNECT_TIMEOUT_S = 30.0


def _upstream_timeout() -> httpx.Timeout:
    """上游 httpx 超时配置（流式与聚合共用，见 ``UPSTREAM_READ_TIMEOUT_S``）。"""
    return httpx.Timeout(UPSTREAM_CONNECT_TIMEOUT_S, read=UPSTREAM_READ_TIMEOUT_S)


async def stream_upstream(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    protocol: str,
    original: dict[str, Any] | None,
):
    """异步流式转发上游响应到客户端。

    关键改进：
    1. 使用 httpx.AsyncClient 异步请求
    2. aiter_lines() 自动处理行分割和超时
    3. 记录流开始/进度/完成日志
    """
    state = get_state()
    stream_start_time = time.time()

    # 【日志】流开始
    state.write_log("stream_started", protocol=protocol, timestamp=stream_start_time)
    diagnostic("stream_started", protocol=protocol)

    response_id = "resp_" + uuid.uuid4().hex
    anthropic_state = AnthropicStreamConverter(
        (original or {}).get("model", "default")
    ) if protocol == "anthropic" and AnthropicStreamConverter else None

    # Responses 协议转换器（使用传入的 body 参数）
    responses_state = ResponsesStreamConverter(
        model=body.get("model", "auto")
    ) if protocol == "responses" and ResponsesStreamConverter else None

    # DSML 缓冲区（用于处理可能的文本标记格式工具调用）
    dsml_buffer = DSMLStreamBuffer()

    # 【修复 B1】原生流式 tool_calls name 缓存
    # 上游首 chunk 带完整 name，后续 chunk name 为空但有 arguments 分片
    # 维护 name 缓存防止空值覆盖
    native_tool_name_by_index: dict[int, str] = {}

    emitted_response_created = False
    response_text = ""
    response_text_started = False
    chunk_count = 0
    done_seen = False
    last_progress_log = stream_start_time
    detected_tool_calls = []
    # DSML 注入的 tool_calls index（跨 chunk 自增，见 _build_dsml_tool_call_deltas）
    dsml_tool_index = 0
    # 【修复 C3】记录最后一个上游 chunk 的元数据，供流结束 flush 时复用，
    # 保证 final_chunk 补全 OpenAI ChatCompletionChunk 必需的顶层字段
    last_chunk_id: str = ""
    last_chunk_created: int = 0
    last_chunk_model: str = ""
    try:
        # 异步HTTP客户端：timeout=None 依赖TCP超时
        timeout_config = _upstream_timeout()
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    error_text = error_body.decode("utf-8", "replace")

                    # 【诊断】记录 400 错误时的工具定义
                    if resp.status_code == 400:
                        tools = body.get("tools", [])
                        diagnostic("upstream_400_error",
                                   status=resp.status_code,
                                   error_preview=error_text[:200],
                                   tool_count=len(tools),
                                   sample_tools=tools[:2] if tools else [])
                    diagnostic("upstream_error", protocol=protocol,
                        status=resp.status_code,
                        detail=error_text[:500])

                    # 返回结构化错误（包含详细信息）
                    if protocol == "anthropic":
                        # Anthropic error format
                        error_event = {
                            "type": "error",
                            "error": {
                                "type": "api_error",
                                "message": f"Upstream API error (HTTP {resp.status_code}): {error_text[:200]}"
                            }
                        }
                        yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n".encode()
                    else:
                        # OpenAI error format
                        error_chunk = {
                            "error": {
                                "message": f"Upstream API error (HTTP {resp.status_code})",
                                "type": "upstream_error",
                                "code": resp.status_code,
                                "details": error_text[:500]
                            }
                        }
                        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode()

                    return

                diagnostic("upstream_response", protocol=protocol, status=resp.status_code)

                # 【配置】超时策略 - 双重超时机制
                # 1. 空闲超时（idle timeout）：连续N秒没有新chunk → 超时
                # 2. 总时长上限（total duration）：绝对时长限制，防止无限期占用
                client_timeout = body.get("max_stream_duration")
                client_idle_timeout = body.get("max_idle_duration")

                # 空闲超时：默认60秒，客户端可配置（10-300秒）
                if client_idle_timeout is not None:
                    MAX_IDLE_DURATION = min(max(int(client_idle_timeout), 10), 300)
                else:
                    MAX_IDLE_DURATION = 60  # 60秒没有新数据则超时

                # 总时长上限：默认30分钟，客户端可配置（最大2小时）
                if client_timeout is not None:
                    MAX_TOTAL_DURATION = min(max(int(client_timeout), 60), 7200)
                else:
                    MAX_TOTAL_DURATION = 1800  # 30分钟绝对上限

                last_chunk_time = time.time()  # 记录最后一次收到chunk的时间

                # 异步迭代行（自动处理超时和分块）
                async for line in resp.aiter_lines():
                    current_time = time.time()

                    # 【保护1】检查空闲超时：距离上次chunk超过N秒
                    idle_time = current_time - last_chunk_time
                    if idle_time > MAX_IDLE_DURATION:
                        diagnostic("stream_idle_timeout", protocol=protocol,
                                  chunks=chunk_count,
                                  idle_time=round(idle_time, 2),
                                  max_idle=MAX_IDLE_DURATION)
                        state.write_log("stream_idle_timeout", protocol=protocol,
                                       chunks=chunk_count, idle_time=round(idle_time, 2))
                        break  # 空闲超时，结束流

                    # 【保护2】检查总时长上限：防止无限期运行
                    total_elapsed = current_time - stream_start_time
                    if total_elapsed > MAX_TOTAL_DURATION:
                        diagnostic("stream_total_duration_exceeded", protocol=protocol,
                                  chunks=chunk_count,
                                  elapsed=round(total_elapsed, 2),
                                  max_duration=MAX_TOTAL_DURATION)
                        state.write_log("stream_total_duration_exceeded", protocol=protocol,
                                       chunks=chunk_count, elapsed=round(total_elapsed, 2))
                        break  # 总时长超限，结束流

                    # 更新最后chunk时间
                    last_chunk_time = current_time

                    # 【日志】进度记录（每10个chunk且间隔5秒）
                    if chunk_count > 0 and chunk_count % 10 == 0:
                        now = time.time()
                        if now - last_progress_log >= 5:
                            diagnostic("stream_progress", protocol=protocol,
                                chunks=chunk_count,
                                elapsed=round(now - stream_start_time, 2))
                            last_progress_log = now
                    line = line.strip()

                    if not line.startswith("data:"):
                        continue

                    data = line[5:].strip()
                    if data == "[DONE]":
                        done_seen = True
                        break

                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    chunk_count += 1

                    # 【修复 C3】记录最后一个上游 chunk 的元数据，供流结束 flush 复用
                    if chunk.get("id"):
                        last_chunk_id = chunk["id"]
                    if chunk.get("created"):
                        last_chunk_created = chunk["created"]
                    if chunk.get("model"):
                        last_chunk_model = chunk["model"]

                    # 提取当前 chunk 的原生 tool_calls（三种协议共享）。
                    # 注意：必须在协议分支之前定义，responses/anthropic 分支
                    # 的 B2 覆盖条件也会引用它（原生 tool_calls 存在时不覆盖）。
                    native_tool_calls = (
                        ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("tool_calls")
                    )

                    # 根据协议转换事件
                    if protocol == "openai":
                        # 【修复 Bug B3】删除空 finish_reason 字段
                        # 上游可能返回 "finish_reason": "" 或 null，导致客户端序列化失败
                        # 完全删除该字段，只保留真实的 stop/tool_calls/length/content_filter
                        if "choices" in chunk:
                            for choice in chunk["choices"]:
                                if "finish_reason" in choice and (choice["finish_reason"] == "" or choice["finish_reason"] is None):
                                    del choice["finish_reason"]

                                # DSML 兜底：中途注入的 tool_calls 会被上游最终
                                # finish_reason=stop 盖掉（客户端以最后一个为准），
                                # 检测到文本工具调用时改写为 tool_calls
                                if (choice.get("finish_reason") == "stop"
                                        and dsml_buffer.should_emit_tool_calls()):
                                    choice["finish_reason"] = "tool_calls"

                        # 【修复 Bug 2】原生流式 tool_calls name 缓存
                        # 首 chunk 带完整 name，后续 chunk name 为空但带 arguments 分片，
                        # 这里按 index 缓存 name 并回填（native_tool_calls 已在协议分支前提取）
                        if native_tool_calls:
                            for tc in native_tool_calls:
                                idx = tc.get("index", 0)
                                fn = tc.get("function") or {}
                                nm = fn.get("name") or ""

                                # 首次出现非空 name：记录到缓存
                                if nm:
                                    native_tool_name_by_index[idx] = nm
                                # 后续空 name：从缓存回填
                                elif idx in native_tool_name_by_index:
                                    if "function" not in tc:
                                        tc["function"] = {}
                                    tc["function"]["name"] = native_tool_name_by_index[idx]

                        # 提取 content 并通过 DSML 缓冲区处理
                        chunk_content = str(
                            ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                        )

                        # 每轮先置空：首 chunk 可能只有 role 没有 content，
                        # 下面注入块在 if chunk_content: 外引用它，不初始化会 UnboundLocalError
                        chunk_tool_calls = []

                        if chunk_content:
                            # 使用 DSML 缓冲区处理（清理标记，检测工具调用）
                            cleaned_content, chunk_tool_calls = dsml_buffer.add_chunk(chunk_content)

                            # 累积清理后的文本
                            if cleaned_content:
                                response_text += cleaned_content

                            # 记录检测到的工具调用
                            if chunk_tool_calls:
                                detected_tool_calls.extend(chunk_tool_calls)

                            # 修改 chunk 中的 content 为清理后的内容
                            if "choices" in chunk and len(chunk["choices"]) > 0:
                                if "delta" not in chunk["choices"][0]:
                                    chunk["choices"][0]["delta"] = {}
                                chunk["choices"][0]["delta"]["content"] = cleaned_content

                        # 【修复 Bug B2】仅当 chunk 不含原生 tool_calls 时，才使用 DSML 解析的工具调用
                        # DSML 用于兜底：处理上游以文本标记返回工具调用的场景
                        # 如果 chunk 已有原生 delta.tool_calls，原样透传，绝不覆盖
                        if chunk_tool_calls and dsml_buffer.should_emit_tool_calls() and not native_tool_calls:
                            if "choices" in chunk and len(chunk["choices"]) > 0:
                                chunk["choices"][0]["finish_reason"] = "tool_calls"
                                # 解析器产出 {id, type, function:{name, arguments}} 格式，
                                # 直接映射为 OpenAI 流式 tool_calls 分片（index 跨 chunk 自增）
                                deltas, dsml_tool_index = _build_dsml_tool_call_deltas(
                                    chunk_tool_calls, dsml_tool_index
                                )
                                chunk["choices"][0]["delta"]["tool_calls"] = deltas

                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

                    elif protocol == "responses" and responses_state:
                        # 【修复】提取 content 并通过 DSML 缓冲区处理
                        chunk_content = str(
                            ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                        )

                        if chunk_content:
                            # 使用 DSML 缓冲区处理（清理标记，检测工具调用）
                            cleaned_content, chunk_tool_calls = dsml_buffer.add_chunk(chunk_content)

                            # 累积清理后的文本
                            if cleaned_content:
                                response_text += cleaned_content

                            # 记录检测到的工具调用
                            if chunk_tool_calls:
                                detected_tool_calls.extend(chunk_tool_calls)

                            # 修改 chunk 中的 content 为清理后的内容
                            if "choices" in chunk and len(chunk["choices"]) > 0:
                                if "delta" not in chunk["choices"][0]:
                                    chunk["choices"][0]["delta"] = {}
                                chunk["choices"][0]["delta"]["content"] = cleaned_content

                            # 【修复 Bug B2】仅当 chunk 不含原生 tool_calls 时，才使用 DSML 解析的工具调用
                            if chunk_tool_calls and dsml_buffer.should_emit_tool_calls() and not native_tool_calls:
                                if "choices" in chunk and len(chunk["choices"]) > 0:
                                    chunk["choices"][0]["finish_reason"] = "tool_calls"
                                    # 解析器产出 function 格式（非 {name, input}），按实际结构取值
                                    deltas, dsml_tool_index = _build_dsml_tool_call_deltas(
                                        chunk_tool_calls, dsml_tool_index
                                    )
                                    chunk["choices"][0]["delta"]["tool_calls"] = deltas

                        # 使用 ResponsesStreamConverter 转换事件（此时 chunk 已经被清理）
                        events = responses_state.feed_chunk(chunk)
                        for event_name, event_data in events:
                            yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
                    elif protocol == "anthropic" and anthropic_state:
                        # 提取 content 并通过 DSML 缓冲区处理
                        chunk_content = str(
                            ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                        )

                        if chunk_content:
                            # 使用 DSML 缓冲区处理（清理标记，检测工具调用）
                            cleaned_content, chunk_tool_calls = dsml_buffer.add_chunk(chunk_content)

                            # 累积清理后的文本
                            if cleaned_content:
                                response_text += cleaned_content

                            # 记录检测到的工具调用
                            if chunk_tool_calls:
                                detected_tool_calls.extend(chunk_tool_calls)

                            # ✅ 关键修复：在传递给 AnthropicStreamConverter 之前，先修改 chunk
                            if "choices" in chunk and len(chunk["choices"]) > 0:
                                if "delta" not in chunk["choices"][0]:
                                    chunk["choices"][0]["delta"] = {}
                                # 使用清理后的内容替换原始内容
                                chunk["choices"][0]["delta"]["content"] = cleaned_content

                            # 【修复 Bug B2】仅当 chunk 不含原生 tool_calls 时，才使用 DSML 解析的工具调用
                            if chunk_tool_calls and dsml_buffer.should_emit_tool_calls() and not native_tool_calls:
                                if "choices" in chunk and len(chunk["choices"]) > 0:
                                    chunk["choices"][0]["finish_reason"] = "tool_calls"
                                    deltas, dsml_tool_index = _build_dsml_tool_call_deltas(
                                        chunk_tool_calls, dsml_tool_index
                                    )
                                    chunk["choices"][0]["delta"]["tool_calls"] = deltas

                        # 转换为 Anthropic 事件（此时 chunk 已经被清理）
                        events = anthropic_state.feed_chunk(chunk)
                        for event_name, event_data in events:
                            yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()

                # 发送结束事件
                if protocol == "responses" and responses_state:
                    # 【修复】流结束前强制刷新 DSML 缓冲区残留内容
                    residual = dsml_buffer.flush()
                    if residual:
                        for event_name, event_data in responses_state.feed_chunk({
                            "choices": [{"index": 0, "delta": {"content": residual}}]
                        }):
                            yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
                    # 使用 ResponsesStreamConverter 的 finish() 方法发出完整事件序列
                    for event_name, event_data in responses_state.finish():
                        yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()

                    # 【修复】发送SSE流结束标记，防止客户端持续等待
                    yield b"data: [DONE]\n\n"

                elif protocol == "anthropic" and anthropic_state:
                    # 【修复】流结束前强制刷新 DSML 缓冲区残留内容
                    residual = dsml_buffer.flush()
                    if residual:
                        for event_name, event_data in anthropic_state.feed_chunk({
                            "choices": [{"index": 0, "delta": {"content": residual}}]
                        }):
                            yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
                    for event_name, event_data in anthropic_state.finish():
                        yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()

                    # 【修复】发送SSE流结束标记，防止客户端持续等待（与Responses协议保持一致）
                    # 虽然Anthropic有message_stop事件，但明确的[DONE]标记能确保客户端立即处理最后的内容
                    yield b"data: [DONE]\n\n"

                elif protocol == "openai":
                    # 【修复】流结束前强制刷新 DSML 缓冲区残留内容，避免含 '<' 的
                    # 普通文本（如文档中举例的工具调用标签）被永久扣留而截断输出
                    residual = dsml_buffer.flush()
                    if residual:
                        response_text += residual
                        final_chunk = _build_openai_flush_chunk(
                            residual, last_chunk_id, last_chunk_created, last_chunk_model
                        )
                        yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode()
                    yield b"data: [DONE]\n\n"

    except httpx.TimeoutException as exc:
        # 【日志】超时
        diagnostic("stream_timeout", protocol=protocol, chunks=chunk_count,
            elapsed=round(time.time() - stream_start_time, 2), error=str(exc))
        state.write_log("stream_timeout", protocol=protocol, chunks=chunk_count, error=str(exc))

        # 【防御性编程】确保发出完整的事件序列
        if protocol == "responses" and responses_state:
            for event_name, event_data in responses_state.finish():
                yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
        elif protocol == "anthropic" and anthropic_state:
            for event_name, event_data in anthropic_state.finish():
                yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()

        # 发送错误事件
        error_chunk = {
            "error": {
                "message": f"stream timeout after {chunk_count} chunks",
                "type": "timeout_error"
            }
        }
        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode()

    except Exception as exc:
        # 【日志】其他错误
        diagnostic("stream_error", protocol=protocol, chunks=chunk_count,
            elapsed=round(time.time() - stream_start_time, 2), error=str(exc))
        state.write_log("stream_error", protocol=protocol, chunks=chunk_count, error=str(exc))

        # 【防御性编程】确保发出完整的事件序列
        if protocol == "responses" and responses_state:
            for event_name, event_data in responses_state.finish():
                yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
        elif protocol == "anthropic" and anthropic_state:
            for event_name, event_data in anthropic_state.finish():
                yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()

        # 发送错误事件
        error_chunk = {
            "error": {
                "message": f"stream error: {exc}",
                "type": "internal_error"
            }
        }
        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode()

    finally:
        # 不收集或落盘原始 SSE 正文；仅在下方记录安全摘要。
        logged_text = (
            anthropic_state.text if anthropic_state
            else responses_state.text if responses_state
            else response_text
        )
        stream_duration = round(time.time() - stream_start_time, 2)

        log_upstream_response(protocol, logged_text, stream=True,
                            chunk_count=chunk_count, duration=stream_duration,
                            upstream_done=done_seen)


async def collect_upstream(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    protocol: str,
) -> dict[str, Any]:
    """聚合上游流式响应为单个 JSON 对象（非流式场景）。"""
    state = get_state()

    usage = None
    finish_reason = None
    content = ""
    tool_calls_dict: dict[int, dict] = {}  # 使用 dict 按 index 累加
    # DSML 缓冲区
    dsml_buffer = DSMLStreamBuffer()

    try:
        timeout_config = _upstream_timeout()
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    raise HTTPException(
                        status_code=resp.status_code,
                        detail={"error": {"message": error_body.decode("utf-8", "replace")[:500], "type": "upstream_error"}}
                    )

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue

                    data = line[5:].strip()
                    if data == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    usage = chunk.get("usage") or usage

                    for choice in chunk.get("choices") or []:
                        finish_reason = choice.get("finish_reason") or finish_reason
                        delta = choice.get("delta") or {}

                        # 处理 content（可能包含 DSML）
                        if delta.get("content"):
                            chunk_content = delta["content"]

                            # 使用 DSML 缓冲区处理
                            cleaned_content, detected_tool_calls = dsml_buffer.add_chunk(chunk_content)

                            # 调试日志：记录 DSML 解析结果
                            if detected_tool_calls:
                                diagnostic("dsml_detected",
                                          tool_count=len(detected_tool_calls),
                                          tools=[tc["function"]["name"] for tc in detected_tool_calls])

                            # 累积清理后的 content
                            if cleaned_content:
                                content += cleaned_content

                            # 如果检测到 tool_calls，添加到 dict 中
                            if detected_tool_calls:
                                for detected_call in detected_tool_calls:
                                    # 找到下一个可用的 index
                                    next_idx = len(tool_calls_dict)
                                    tool_calls_dict[next_idx] = detected_call

                        # 处理原生 tool_calls（使用 dict 累加，避免预填充）
                        if delta.get("tool_calls"):
                            for tc in delta["tool_calls"]:
                                idx = tc.get("index", 0)
                                if idx not in tool_calls_dict:
                                    tool_calls_dict[idx] = {
                                        "id": "",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""}
                                    }

                                if tc.get("id"):
                                    tool_calls_dict[idx]["id"] = tc["id"]
                                if tc.get("function", {}).get("name"):
                                    tool_calls_dict[idx]["function"]["name"] = tc["function"]["name"]
                                if tc.get("function", {}).get("arguments"):
                                    tool_calls_dict[idx]["function"]["arguments"] += tc["function"]["arguments"]

    except httpx.HTTPError as exc:
        diagnostic("upstream_error", protocol=protocol, error=str(exc))
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {exc}", "type": "upstream_error"}})

    # 转换为 list 并过滤掉无效的 tool_calls（name 为空的）
    tool_calls = [
        v for k, v in sorted(tool_calls_dict.items())
        if v["function"]["name"]
    ]

    # 如果检测到 DSML tool_calls，修改 finish_reason
    if tool_calls and dsml_buffer.should_emit_tool_calls():
        finish_reason = "tool_calls"

    # 【日志】收集完成
    if state.verbose_llm:
        # collect_upstream 没有保存原始响应，只记录聚合后的内容
        pass

    log_upstream_response(protocol, content, stream=False)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": now_s(),
        "model": body.get("model", "auto"),
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls if tool_calls else None
            },
            "finish_reason": finish_reason or "stop"
        }],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    }


def convert_nonstream(data: dict[str, Any], protocol: str, original: dict[str, Any] | None) -> dict[str, Any]:
    """将聚合的 OpenAI 格式转换为目标协议格式。"""
    if protocol == "openai":
        return data

    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content", "")

    if protocol == "anthropic":
        return chat_completion_to_anthropic_message(data, original)

    elif protocol == "responses":
        # Responses 的 output 是**顶层 item 列表**：文本进 message item 的
        # ``output_text`` 内容块，工具调用是独立的 ``function_call`` item。
        # 此前把 function_call 塞进 message 的 content 里，既不是合法 content
        # part 类型，也和流式路径（``ResponsesStreamConverter.finish``）的形状
        # 对不上——严格客户端会整条 item 解析失败，和 output_text 那次回归同类。
        response_id = "resp_" + uuid.uuid4().hex
        output: list[dict[str, Any]] = []

        if content:
            output.append({
                "id": response_id + "_msg",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": content, "annotations": []}],
            })

        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            # 规范里 arguments 是 JSON **字符串**；聚合阶段可能已经是 dict，
            # 这里统一序列化，与流式路径发出的增量字符串保持一致。
            arguments = fn.get("arguments", "")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            item_id = str(call.get("id") or "") or f"fc_{uuid.uuid4().hex[:12]}"
            output.append({
                "type": "function_call",
                "id": item_id,
                "call_id": item_id,
                "name": fn.get("name", ""),
                "arguments": arguments,
            })

        return {
            "id": response_id,
            "object": "response",
            "created_at": now_s(),
            "status": "completed",
            "output": output,
        }

    return data
