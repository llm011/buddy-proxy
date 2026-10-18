"""
anthropic_adapter.py — Anthropic Messages API ↔ Chat Completions API 适配层。

Claude Code / CC Switch 使用 Anthropic Messages API，而 CodeBuddy 后端只支持
Chat Completions 协议。本模块做双向转换：
    请求：Anthropic system/messages/tools → Chat messages/tools
    响应：Chat SSE delta → Anthropic SSE 事件流（message_start / content_block_delta / …）
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from buddy_proxy.core.metrics import _credit_field


def _rand_id(prefix: str = "msg_") -> str:
    """生成随机ID"""
    return prefix + os.urandom(12).hex()


def _now_s() -> int:
    """当前时间戳（秒）"""
    return int(time.time())


def _extract_text(value: Any) -> str:
    """从content中提取文本"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts) if parts else ""
    return "" if value is None else str(value)


def _anthropic_image_to_chat(block: dict[str, Any]) -> dict[str, Any] | None:
    """Anthropic image block → OpenAI/Trae ``image_url`` block。

    Claude Code 发图使用 ``{type:image, source:{type:base64, media_type,
    data}}``；Trae ``llm_utils_chat`` 接受的是 OpenAI 风格 image_url（data
    URL）。同时兼容 Anthropic URL source。字段不完整时丢弃该块，避免把
    半成品图片继续发给上游触发 4001 param invalid。
    """
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    source_type = source.get("type")
    if source_type == "base64":
        media_type = str(source.get("media_type") or "").strip()
        data = source.get("data")
        if not media_type.startswith("image/") or not isinstance(data, str) or not data:
            return None
        url = f"data:{media_type};base64,{data}"
    elif source_type == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            return None
    else:
        return None
    return {"type": "image_url", "image_url": {"url": url}}


def _chat_image_to_anthropic(block: dict[str, Any]) -> dict[str, Any] | None:
    """OpenAI ``image_url`` block → Anthropic image block（生图模型出口）。

    只转换 data URL；http(s) URL 转成 Anthropic ``url`` source。字段不全
    直接丢弃，与入站方向保持对称。
    """
    url = (block.get("image_url") or {}).get("url")
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        if not data:
            return None
        media_type = header[5:].split(";", 1)[0] or "image/png"
        return {"type": "image", "source": {
            "type": "base64", "media_type": media_type, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _anthropic_mixed_content(blocks: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    """转换 text/image blocks，返回 (OpenAI blocks, 是否包含图片)。"""
    parts: list[dict[str, Any]] = []
    has_image = False
    for block in blocks:
        if isinstance(block, str):
            if block:
                parts.append({"type": "text", "text": block})
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            parts.append({"type": "text", "text": block.get("text", "")})
        elif block.get("type") == "image":
            image = _anthropic_image_to_chat(block)
            if image:
                parts.append(image)
                has_image = True
    return parts, has_image


def _chat_content(parts: list[dict[str, Any]], has_image: bool) -> Any:
    """纯文本保持历史 string 形态；含图时保留有序 block 列表。"""
    if has_image:
        return parts
    return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")


def _convert_anthropic_message(msg: dict) -> list[dict]:
    """将单条 Anthropic 消息转换为 Chat 消息（可能返回多条）。

    支持 text / image / tool_use / tool_result；图片转换成 Trae 可识别的
    OpenAI ``image_url`` block，tool_result 内嵌图片也保留。
    """
    role = msg.get("role", "user")
    content = msg.get("content", "")
    
    # 简单字符串content
    if isinstance(content, str):
        if not content:
            return []
        return [{"role": role, "content": content}]
    
    # 空content
    if not content:
        return []
    
    # 复杂content blocks
    if not isinstance(content, list):
        content = [content]
    
    messages = []
    
    # assistant消息处理
    if role == "assistant":
        text_parts = []
        tool_calls = []
        
        for block in content:
            if not isinstance(block, dict):
                continue
            
            block_type = block.get("type")
            
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            
            elif block_type == "tool_use":
                tool_calls.append({
                    "id": block.get("id", _rand_id("call_")),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
        
        # 构造assistant消息
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": "\n".join(text_parts) if text_parts else None,
        }
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)
    
    # user消息处理
    elif role == "user":
        user_parts, has_user_image = _anthropic_mixed_content(content)
        tool_results = []

        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            # tool_result 块 → 独立 tool 消息；其 content 内也可能包含截图。
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                result_parts, has_result_image = _anthropic_mixed_content(result_content)
                converted_result = _chat_content(result_parts, has_result_image)
            else:
                converted_result = str(result_content)
            tool_results.append({
                "role": "tool",
                "tool_call_id": block.get("tool_use_id", ""),
                "content": converted_result,
            })

        # 先添加 tool 结果，再添加 user 内容，保持严格 tool 配对顺序。
        messages.extend(tool_results)
        if user_parts:
            messages.append({
                "role": "user",
                "content": _chat_content(user_parts, has_user_image),
            })
    
    # 其他角色（system等）
    else:
        text = _extract_text(content)
        if text:
            messages.append({"role": role, "content": text})
    
    return messages


def repair_tool_sequence(messages: list[dict]) -> list[dict]:
    """修复 chat messages 中 tool_calls 与 tool 结果的配对，保证上游校验通过。

    DeepSeek 系上游（CodeBuddy 11148 / tool_call_sequence_broken）要求：
        - assistant 的每个 tool_call 必须有且仅有一条同 id 的 tool 消息回答；
        - tool 消息必须紧跟在带 tool_calls 的 assistant 消息之后；
        - 不能出现没有对应 tool_call 的 tool 消息。

    Claude Code / CC 兼容客户端的历史并不保证这些（中断后不回结果、上下文
    裁剪掉 tool_use 但保留结果、tool_use_id 为空等），逐条修复：

        1. assistant 带 tool_calls → 其后吸收连续的 tool 消息，按 id 配对；
           缺结果的 call 补一条占位 tool 消息（标注中断），保证 call 全部被回答；
        2. 重复 id 的结果只保留第一条（重复 id 同样触发 11148）；
        3. id 对不上任何 call 的孤儿结果 → 转成 user 消息保留内容（不静默丢信息）；
        4. 无前置 tool_calls 的 tool 消息 → 同样转成 user 消息；
        5. 结果丢失 id（空 tool_use_id）→ 按顺序认领第一个未回答的 call；
        6. 纯 thinking 等产生的空 assistant 消息（无 text、无 tool_calls）→ 丢弃，
           content 为 null 的 assistant 是很多校验器的硬伤；
        7. tool_call 缺 id → 兜底生成，保证配对键存在。
    """
    result: list[dict] = []
    i, n = 0, len(messages)
    while i < n:
        msg = messages[i]
        role = msg.get("role")

        if role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                # 空 assistant 消息（content 为 None/""）直接丢弃
                if msg.get("content"):
                    result.append(msg)
                i += 1
                continue

            # id 缺失的 call 兜底生成（原地补，msg 是转换器新建的 dict）
            for tc in tool_calls:
                if not tc.get("id"):
                    tc["id"] = _rand_id("call_")
            result.append(msg)
            expected = [tc["id"] for tc in tool_calls]
            answered: set[str] = set()

            i += 1
            while i < n and messages[i].get("role") == "tool":
                tid = messages[i].get("tool_call_id")
                if not tid:
                    # 结果丢了 id（客户端回传空 tool_use_id）：按顺序认领
                    # 第一个未回答的 call（Anthropic 语义里结果与 call 同序）
                    unmatched = [t for t in expected if t not in answered]
                    if unmatched:
                        messages[i] = {**messages[i], "tool_call_id": unmatched[0]}
                        answered.add(unmatched[0])
                        result.append(messages[i])
                    # 无可认领（call 都已回答）→ 丢弃
                elif tid in expected and tid not in answered:
                    answered.add(tid)
                    result.append(messages[i])
                elif tid not in expected:
                    # 孤儿结果：找不到发起方，转 user 保留内容
                    result.append({
                        "role": "user",
                        "content": (f"[tool result without matching tool call "
                                    f"(id={tid!r})]: {messages[i].get('content', '')}"),
                    })
                # 其余情况（重复 id）丢弃
                i += 1

            # 未被回答的 call 补占位结果，避免 tool_call_sequence_broken
            for tid in expected:
                if tid not in answered:
                    result.append({
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": "[tool result missing: the call was interrupted "
                                   "or its output was trimmed from context]",
                    })
            continue

        if role == "tool":
            # 前面没有带 tool_calls 的 assistant → 转 user 保留内容
            result.append({
                "role": "user",
                "content": (f"[tool result without matching tool call "
                            f"(id={msg.get('tool_call_id')!r})]: {msg.get('content', '')}"),
            })
            i += 1
            continue

        result.append(msg)
        i += 1
    return result


def anthropic_request_to_chat(body: dict) -> dict:
    """将Anthropic Messages API请求体转换为Chat Completions请求体
    
    关键映射：
        system → system message（置顶）
        messages → messages（展开content blocks）
        tools → Chat格式tools
        tool_choice → Chat格式
    """
    messages: list[dict] = []
    
    # system参数 → system message
    system = body.get("system")
    if system:
        system_text = _extract_text(system)
        if system_text:
            messages.append({"role": "system", "content": system_text})
    
    # messages转换
    for msg in body.get("messages", []):
        messages.extend(_convert_anthropic_message(msg))

    # 兜底修复 tool_calls/tool 配对（中断、裁剪、混排的历史都会破坏配对，
    # DeepSeek 系上游会以 11148 tool_call_sequence_broken 拒单）
    messages = repair_tool_sequence(messages)
    
    # 构造Chat body
    chat: dict[str, Any] = {
        "messages": messages,
        # Anthropic Messages defaults to a normal JSON response.  Preserve
        # the client's choice here; forward_chat() will still request a
        # stream from CodeBuddy internally and aggregate it when needed.
        "stream": bool(body.get("stream", False)),
    }
    
    # model
    if "model" in body:
        chat["model"] = body["model"]
    
    # tools转换
    tools = body.get("tools")
    if tools:
        chat["tools"] = _convert_tools_for_chat(tools)
    
    # tool_choice转换
    tool_choice = body.get("tool_choice")
    if tool_choice is not None:
        chat["tool_choice"] = _convert_tool_choice(tool_choice)
    
    # 透传参数
    for key in ("max_tokens", "temperature", "top_p", "stop", "top_k"):
        if key in body:
            chat[key] = body[key]
    
    return chat


def _convert_tools_for_chat(tools: list[dict]) -> list[dict]:
    """将Anthropic工具格式转换为Chat格式
    
    Anthropic: {name, description, input_schema}
    Chat: {type: "function", function: {name, description, parameters}}
    """
    result = []
    for tool in tools:
        # 已经是Chat格式
        if tool.get("type") == "function" and tool.get("function"):
            result.append(tool)
        # Anthropic格式
        elif tool.get("name"):
            result.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            })
        else:
            result.append(tool)
    return result


def _convert_tool_choice(tool_choice: Any) -> Any:
    """转换tool_choice格式
    
    Anthropic支持：
        - "auto" / "any" / "required" (字符串)
        - {"type": "tool", "name": "..."} (指定工具)
    
    Chat支持：
        - "auto" / "none" / "required" (字符串)
        - {"type": "function", "function": {"name": "..."}}
    """
    if isinstance(tool_choice, str):
        # Anthropic的"any" → Chat的"required"
        if tool_choice == "any":
            return "required"
        # "auto" / "none" 直接映射
        return tool_choice
    
    if isinstance(tool_choice, dict):
        # {"type": "tool", "name": "..."} → {"type": "function", "function": {"name": "..."}}
        if tool_choice.get("type") == "tool" and tool_choice.get("name"):
            return {
                "type": "function",
                "function": {"name": tool_choice["name"]},
            }
        # 已经是Chat格式
        return tool_choice
    
    return tool_choice


# ---------------------------------------------------------------------------
# 响应转换：Chat SSE → Anthropic SSE事件流
# ---------------------------------------------------------------------------

class AnthropicStreamConverter:
    """将Chat SSE流转换为Anthropic Messages API事件流
    
    事件序列：
        1. message_start
        2. content_block_start (thinking，仅推理模型有 reasoning_content 时)
        3. content_block_delta (thinking_delta，多次)
        4. content_block_start (text)
        5. content_block_delta (text_delta，多次)
        6. content_block_stop
        7. content_block_start (tool_use)
        8. content_block_delta (input_json_delta，多次)
        9. content_block_stop
        10. message_delta (stop_reason + usage)
        11. message_stop
    """
    
    def __init__(self, model: str):
        self.message_id = _rand_id("msg_")
        self.model = model

        # 状态跟踪
        self.started = False
        self.thinking_block_index: int | None = None  # 当前thinking块的index
        self.thinking = ""
        self.text_block_index: int | None = None  # 当前text块的index
        self.text = ""
        self.tool_blocks: dict[int, dict] = {}  # Chat index -> {Anthropic index, id, name, arguments}
        self.next_anthropic_index = 0  # Anthropic content_block的index计数器
        self.finish_reason: str | None = None
        self.usage: dict | None = None
        self.open_blocks: set[int] = set()  # 已打开但未关闭的块index
    
    def feed_chunk(self, chunk: dict) -> list[tuple[str, dict]]:
        """处理一个Chat SSE chunk，返回Anthropic事件列表"""
        events: list[tuple[str, dict]] = []
        
        # 首次chunk：发出message_start
        if not self.started:
            self.started = True
            events.append(("message_start", {
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": self.model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }))
        
        # 提取usage
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        
        # 处理choices
        for choice in chunk.get("choices", []):
            self.finish_reason = choice.get("finish_reason") or self.finish_reason
            delta = choice.get("delta", {})
            
            # 思维链（reasoning_content，DeepSeek/glm 等推理模型的 OpenAI 风格扩展）
            # → Anthropic thinking 块。thinking 块必须排在 text 之前，
            # 推理模型总是先出 reasoning 再出正文，天然满足。
            if delta.get("reasoning_content"):
                think_delta = str(delta["reasoning_content"])
                self.thinking += think_delta
                
                # 首次思维链：打开thinking块（signature 留空，兼容 Claude Code）
                if self.thinking_block_index is None:
                    self.thinking_block_index = self.next_anthropic_index
                    self.next_anthropic_index += 1
                    self.open_blocks.add(self.thinking_block_index)
                    events.append(("content_block_start", {
                        "type": "content_block_start",
                        "index": self.thinking_block_index,
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                    }))
                
                events.append(("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self.thinking_block_index,
                    "delta": {"type": "thinking_delta", "thinking": think_delta},
                }))
            
            # 文本内容
            if delta.get("content"):
                text_delta = str(delta["content"])
                self.text += text_delta
                
                # 首次文本：打开text块
                if self.text_block_index is None:
                    self.text_block_index = self.next_anthropic_index
                    self.next_anthropic_index += 1
                    self.open_blocks.add(self.text_block_index)
                    events.append(("content_block_start", {
                        "type": "content_block_start",
                        "index": self.text_block_index,
                        "content_block": {"type": "text", "text": ""},
                    }))
                
                # 发出text_delta
                events.append(("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self.text_block_index,
                    "delta": {"type": "text_delta", "text": text_delta},
                }))
            
            # 工具调用。用 `or []` 而非默认值：上游常在**每个** chunk 里显式带
            # "tool_calls": null（MiMo 就是这样），默认值只在「缺字段」时生效，
            # 对显式 null 会迭代 None → TypeError 把整条流打断。
            for call in delta.get("tool_calls") or []:
                chat_index = int(call.get("index", 0))
                
                # 初始化工具块
                if chat_index not in self.tool_blocks:
                    anthropic_index = self.next_anthropic_index
                    self.next_anthropic_index += 1
                    self.tool_blocks[chat_index] = {
                        "anthropic_index": anthropic_index,
                        "id": None,
                        "name": None,
                        "arguments": "",
                        "started": False,
                        "pending_args": "",
                    }

                slot = self.tool_blocks[chat_index]
                anthropic_index = slot["anthropic_index"]

                call_id = call.get("id")
                if call_id and not slot["id"]:
                    slot["id"] = call_id

                fn = call.get("function", {})
                fn_name = fn.get("name")
                fn_args = fn.get("arguments")

                # 首次见到工具名：打开tool_use块
                if fn_name and not slot["name"]:
                    slot["name"] = fn_name
                    slot["started"] = True
                    self.open_blocks.add(anthropic_index)
                    events.append(("content_block_start", {
                        "type": "content_block_start",
                        "index": anthropic_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": slot["id"] or f"toolu_{self.message_id}_{chat_index}",
                            "name": fn_name,
                            "input": {},
                        },
                    }))
                    # 块打开前若已到过 arguments 分片，先冲刷缓冲，避免丢参数。
                    if slot["pending_args"]:
                        events.append(("content_block_delta", {
                            "type": "content_block_delta",
                            "index": anthropic_index,
                            "delta": {"type": "input_json_delta",
                                      "partial_json": slot["pending_args"]},
                        }))
                        slot["pending_args"] = ""

                # arguments增量：块已打开才发 delta；否则先缓冲，等 content_block_start
                # 之后再冲刷——绝不在未 start 的 index 上发 delta（会产出非法事件流）。
                if fn_args:
                    slot["arguments"] += fn_args
                    if slot["started"]:
                        events.append(("content_block_delta", {
                            "type": "content_block_delta",
                            "index": anthropic_index,
                            "delta": {"type": "input_json_delta", "partial_json": fn_args},
                        }))
                    else:
                        slot["pending_args"] += fn_args
        
        return events
    
    def close_open_blocks(self) -> list[tuple[str, dict]]:
        """关闭所有已打开但未收尾的 content block（供中途出错的调用方收尾用）。

        流中途冒出错误事件时，已 content_block_start 的块必须补
        content_block_stop，否则严格的 Anthropic 客户端会因为块悬空而卡死/报错。
        幂等：关一次后 open_blocks 清空，finish() 不会重复关。
        """
        events: list[tuple[str, dict]] = []
        for index in sorted(self.open_blocks):
            events.append(("content_block_stop", {
                "type": "content_block_stop",
                "index": index,
            }))
        self.open_blocks.clear()
        return events

    def finish(self) -> list[tuple[str, dict]]:
        """流结束，发出stop事件"""
        events: list[tuple[str, dict]] = []

        # 关闭所有打开的块
        events.extend(self.close_open_blocks())

        # 映射finish_reason
        stop_reason_map = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
        }
        stop_reason = stop_reason_map.get(self.finish_reason or "stop", "end_turn")
        # 发出 tool_use 块且正常收尾（end_turn）时提升为 tool_use：
        # 上游最终 chunk 的 finish_reason=stop 会覆盖中途注入的 tool_calls
        # （非流式路径 chat_completion_to_anthropic_message 也是按 tool_calls
        # 有无来定的，两条路径口径一致）。
        # 但 finish_reason=length → max_tokens 不能被盖掉：截断语义比
        # tool_use 优先（上游没发完 tool call，客户端该按长度截断处理）
        if self.tool_blocks and stop_reason == "end_turn":
            stop_reason = "tool_use"
        
        # 映射usage
        usage_delta = {"input_tokens": 0, "output_tokens": 0}
        if self.usage:
            # Chat使用completion_tokens/prompt_tokens，Anthropic使用output_tokens/input_tokens
            usage_delta["output_tokens"] = self.usage.get("completion_tokens", 0)
            # 缓存命中与积分透传：cache_read_input_tokens 是 Anthropic 标准字段
            # （Claude Code 靠它显示缓存），credit 是 CodeBuddy 扩展——两者都要
            # 出现在流里，metrics 层（SSEUsageExtractor）才记录得到
            details = self.usage.get("prompt_tokens_details") or {}
            cached = (details.get("cached_tokens")
                      or self.usage.get("cached_tokens") or 0)
            # 口径对齐：Chat 的 prompt_tokens 已含缓存命中（OpenAI 口径），
            # Anthropic 的 input_tokens 不含缓存（与 cache_read 是加法关系）。
            # 不扣的话客户端（Claude Code 等）会把两者加总成双倍输入
            usage_delta["input_tokens"] = max(0, self.usage.get("prompt_tokens", 0) - cached)
            if cached:
                usage_delta["cache_read_input_tokens"] = cached
            # 用共享的取数器：CodeBuddy 给 credit、Qoder 给 credits，只认一个
            # 就会把另一家的积分整条丢掉（详见 core/metrics._credit_field）
            credit = _credit_field(self.usage)
            if credit is not None:
                usage_delta["credit"] = credit
        
        # 发出message_delta
        events.append(("message_delta", {
            "type": "message_delta",
            "delta": {
                "stop_reason": stop_reason,
                "stop_sequence": None,
            },
            "usage": usage_delta,
        }))
        
        # 发出message_stop
        events.append(("message_stop", {"type": "message_stop"}))
        
        return events
    


# ---------------------------------------------------------------------------
# 响应转换：Chat Completion（非流式聚合结果）→ Anthropic Message
# ---------------------------------------------------------------------------

def _split_leading_think(content: str) -> tuple[str, str]:
    """拆出正文开头内联的 <think>...</think> 思维链，返回 (thinking, text)。

    部分上游（如 Trae provider 的 _collect）会把 reasoning 合并成
    ``<think>\\n...\\n</think>\\n\\n正文`` 内联进 content；Anthropic 协议
    里思维链应是独立的 thinking 块，此处拆开。仅认开头位置，避免误伤
    正文中举例的 think 标签。
    """
    if not content.startswith("<think>"):
        return "", content
    end = content.find("</think>")
    if end == -1:
        return "", content
    thinking = content[len("<think>"):end].strip("\n")
    text = content[end + len("</think>"):].lstrip("\n")
    return thinking, text


def chat_completion_to_anthropic_message(
    data: dict, original: dict | None = None
) -> dict:
    """将聚合的 OpenAI chat.completion 转换为 Anthropic Messages 响应。

    - 文本 → text 块；开头内联的 <think>...</think> → thinking 块
    - ``message.reasoning_content``（DeepSeek 风格思考字段，kimi/qoder 等
      上游在非流式响应里带）→ thinking 块，排在 text 前
    - tool_calls → tool_use 块（arguments 反序列化为 input 对象）
    - finish_reason 映射 stop_reason；usage 映射 token 字段
    """
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""

    thinking, text = _split_leading_think(content)
    reasoning = str(message.get("reasoning_content") or "").strip()
    if reasoning:
        # 显式思考字段优先于内联 <think> 拆解（上游既然单独给了字段，
        # 正文里一般不会再内联；两者都有时拼在一起也不丢内容）
        thinking = f"{reasoning}\n{thinking}".strip() if thinking else reasoning

    content_blocks = []
    if thinking:
        content_blocks.append({"type": "thinking", "thinking": thinking, "signature": ""})
    if text:
        content_blocks.append({"type": "text", "text": text})
    # 生图模型（gemini-*-image）返回的图片，走 OpenAI 扩展字段 images[]；
    # 放在 tool_use 之前——tool_use 之后只允许 tool_result（在下一个 user turn）。
    for image in message.get("images") or []:
        block = _chat_image_to_anthropic(image) if isinstance(image, dict) else None
        if block:
            content_blocks.append(block)
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            arguments = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            arguments = fn.get("arguments", "")
        content_blocks.append({
            "type": "tool_use",
            "id": call.get("id", ""),
            "name": fn.get("name", ""),
            "input": arguments,
        })
    usage = data.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") or usage.get("cached_tokens") or 0
    usage_out = {
        # 口径对齐：prompt_tokens（OpenAI 口径）已含缓存命中，Anthropic 的
        # input_tokens 不含——须扣除后再输出，否则与 cache_read 加总重复计数
        "input_tokens": max(0, usage.get("prompt_tokens", 0) - cached),
        "output_tokens": usage.get("completion_tokens", 0),
    }
    if cached:
        usage_out["cache_read_input_tokens"] = cached
    credit = _credit_field(usage)
    if credit is not None:
        usage_out["credit"] = credit
    return {
        "id": _rand_id("msg_"),
        "type": "message",
        "role": "assistant",
        "model": (original or {}).get("model", data.get("model", "default")),
        "content": content_blocks,
        "stop_reason": "tool_use" if message.get("tool_calls") else "end_turn",
        "stop_sequence": None,
        "usage": usage_out,
    }


# 向后兼容别名
anthropic_to_chat = anthropic_request_to_chat
