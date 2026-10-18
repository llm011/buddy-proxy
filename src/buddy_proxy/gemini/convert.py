"""OpenAI chat ↔ Gemini（cloudcode-pa v1internal）请求/响应转换。

上游是 Gemini ``generateContent`` 的 v1internal 包装：

    {
      "model": "models/gemini-2.5-flash",
      "project": "genai-xxxx",
      "user_prompt_id": "<13位随机hex>",     # 真 CLI 每次对话随机生成
      "request": {                            # 标准 Gemini 请求体
        "contents": [...], "systemInstruction": {...},
        "tools": [...], "generationConfig": {...}, "session_id": "<uuid>"
      }
    }

这里只做最小而正确的转换（与真 CLI converter.js 对齐的字段）：
OpenAI messages/tools/tool_calls → Gemini contents/functionDeclarations/
functionCall/functionResponse；响应用 usageMetadata 映射回 OpenAI usage。
不支持的 OpenAI 字段（logit_bias、n>1 等）直接丢弃。

safetySettings **不注入**——真 CLI 全源码不发这个字段，注入反而是指纹。

thoughtSignature：Gemini 3 系在 functionCall part 上返回
``thoughtSignature``，多轮工具调用时上游强制校验它（缺 → 400）。回环通道
是 tool call id：响应方向把签名存进 :mod:`.thought_signature` 的进程内
LRU（OpenAI 路径同时透出 ``tool_calls[i].gemini_thought_signature`` 扩展
字段供直连客户端回传），请求方向凭 id 还原；Anthropic 协议丢非标字段，
靠它的 tool_use.id ↔ tool_result.tool_use_id 忠实回环。缺签名时 gemini-3
系注哨兵兜底。详见 :mod:`.thought_signature` 的实证矩阵。
"""

from __future__ import annotations

import json
import re
import secrets
import time
import uuid
from typing import Any

from .thought_signature import lookup, remember, resolve_signature

#: 真 CLI 的 user_prompt_id 形态：Math.random().toString(16).slice(2)，
#: 13 位左右的小写 hex。保持同样形态（长度随机 11-14 位）。
def new_user_prompt_id() -> str:
    return secrets.token_hex(7)[0:13].rstrip("0") or "a"


def new_session_id() -> str:
    return str(uuid.uuid4())


_SANITIZER = re.compile(r"[^a-zA-Z0-9_.:-]")


def sanitize_function_name(name: str) -> str:
    """Gemini 函数名只认 ``[a-zA-Z0-9_.:-]`` 且 ≤64 字符（插件同款规则）。"""
    out = _SANITIZER.sub("_", (name or "").strip()) or "_"
    if not (out[0].isalpha() or out[0] == "_"):
        out = "_" + out[:63]
    return out[:64]


# ---------------------------------------------------------------------------
# OpenAI → Gemini
# ---------------------------------------------------------------------------

_ROLE_MAP = {"assistant": "model", "tool": "user", "function": "user"}


def _content_to_parts(content: Any) -> list[dict[str, Any]]:
    """OpenAI message content → Gemini parts（文本/图片）。"""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}] if content else []
    parts: list[dict[str, Any]] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" or "text" in block:
                text = block.get("text")
                if text:
                    parts.append({"text": str(text)})
            elif btype == "image_url":
                url = (block.get("image_url") or {}).get("url") or ""
                data_url = url if url.startswith("data:") else ""
                if not data_url:
                    continue  # http URL 图片：Gemini 需要 fileData/上传，跳过
                try:
                    header, b64 = data_url.split(",", 1)
                except ValueError:
                    continue
                mime = header[5:].split(";", 1)[0] or "image/png"
                parts.append({"inlineData": {"mimeType": mime, "data": b64}})
    return parts


def _tool_call_to_function_call(
    tc: dict[str, Any], *, model: str, known_names: dict[str, str] | None = None
) -> dict[str, Any] | None:
    fn = tc.get("function") or {}
    name = sanitize_function_name(str(fn.get("name") or ""))
    if not name or name == "_":
        return None
    args: dict[str, Any] = {}
    raw = fn.get("arguments")
    if isinstance(raw, str) and raw.strip():
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            args = {"_raw": raw}
    elif isinstance(raw, dict):
        args = raw
    call_id = str(tc.get("id") or "")
    fc_part: dict[str, Any] = {"name": name, "args": args or {}}
    if call_id:
        # fc.id 与 fr.id 成对是 claude/gpt-oss/gemini-3 的硬要求（缺 → 400）
        fc_part["id"] = call_id
        if known_names is not None:
            known_names[call_id] = name
    call: dict[str, Any] = {"functionCall": fc_part}
    # 签名：客户端显式回传（OpenAI 协议的 gemini_thought_signature）> 缓存
    # 还原（Anthropic 协议丢字段，靠 id 回环）> gemini-3 系哨兵兜底。
    explicit = tc.get("gemini_thought_signature")
    sig = resolve_signature(
        call_id, explicit if isinstance(explicit, str) else "", model=model
    )
    if sig:
        call["thoughtSignature"] = sig
    return call


def _messages_to_contents(
    messages: list[dict[str, Any]], *, model: str
) -> list[dict[str, Any]]:
    """OpenAI messages → Gemini contents。

    关键点：tool 消息 → ``functionResponse`` part（role=user），并且**按上游
    习惯紧跟在带 functionCall 的 model turn 之后**（OpenAI 序列本来就是
    assistant(tool_calls) → tool → tool…，顺序天然满足）。连续 tool 消息合并
    进同一个 user content 的多个 parts（Gemini 一次 functionCall 组对应一个
    user turn，多 response 拆多个 turn 某些模型会 400）。

    ``functionCall.id`` 与 ``functionResponse.id`` 成对回传（claude/gpt-oss
    的硬要求，缺 → 400；gemini-3 同样接受）。函数名的还原顺序：tool 消息自带
    name > 本请求里 tool_call 的 name > 签名缓存的 name > id 兜底——Anthropic
    的 tool_result 不带函数名，过去只能拿 id 顶替，现在能还原真名。
    """
    contents: list[dict[str, Any]] = []
    known_names: dict[str, str] = {}
    for msg in messages:
        role = str(msg.get("role") or "user")
        role = _ROLE_MAP.get(role, role)
        if role not in ("user", "model"):
            role = "user"

        if role == "user" and msg.get("role") == "tool":
            tid = str(msg.get("tool_call_id") or "")
            name = str(msg.get("name") or "")
            if not name and tid:
                name = known_names.get(tid, "") or lookup(tid)[1]
            name = sanitize_function_name(name or tid or "tool")
            resp_body = msg.get("content")
            if isinstance(resp_body, str):
                try:
                    parsed = json.loads(resp_body)
                    resp_body = parsed if isinstance(parsed, dict) else {"result": resp_body}
                except json.JSONDecodeError:
                    resp_body = {"result": resp_body}
            elif not isinstance(resp_body, dict):
                resp_body = {"result": "" if resp_body is None else str(resp_body)}
            fr: dict[str, Any] = {"name": name}
            if tid:
                fr["id"] = tid
            fr["response"] = {"content": resp_body}
            part = {"functionResponse": fr}
            # 与上一个 user turn（若也是纯 functionResponse）合并
            if contents and contents[-1].get("role") == "user" and all(
                "functionResponse" in p for p in contents[-1].get("parts", [])
            ):
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
            continue

        parts: list[dict[str, Any]] = []
        tool_calls = msg.get("tool_calls") or []
        if role == "model" and tool_calls:
            text_parts = _content_to_parts(msg.get("content"))
            parts.extend(text_parts)
            for tc in tool_calls:
                if isinstance(tc, dict):
                    call = _tool_call_to_function_call(
                        tc, model=model, known_names=known_names
                    )
                    if call:
                        parts.append(call)
        else:
            parts = _content_to_parts(msg.get("content"))
        if not parts:
            continue
        contents.append({"role": role, "parts": parts})

    # 相邻同角色合并（OpenAI 允许连续 assistant；Gemini 交替校验较松但合并更稳）
    merged: list[dict[str, Any]] = []
    for c in contents:
        if merged and merged[-1]["role"] == c["role"]:
            merged[-1]["parts"].extend(c["parts"])
        else:
            merged.append(c)
    return merged


def _tools_to_gemini(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI tools → Gemini functionDeclarations（含 schema 清洗）。"""
    decls: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") not in (None, "function"):
            continue
        fn = tool.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        decl: dict[str, Any] = {
            "name": sanitize_function_name(name),
        }
        if fn.get("description"):
            decl["description"] = str(fn["description"])
        params = fn.get("parameters")
        decl["parametersJsonSchema"] = clean_json_schema_for_gemini(
            params if isinstance(params, dict) else {"type": "object", "properties": {}}
        )
        decls.append(decl)
    return [{"function_declarations": decls}] if decls else []


def _clean_schema_node(node: Any) -> Any:
    """递归清洗 JSON Schema 为 Gemini 接受的形态（插件 schema.go 的最小集）。

    - ``type: [x, null]`` → ``x``（nullable 语义丢失，Gemini 无此概念；
      插件的做法相同，只是把类型数组拍平）
    - 删除 Gemini 不认识的关键字（$schema/$defs/const/additionalProperties/
      pattern/format/default/examples/minLength/…）
    - ``const`` → 单值 ``enum``
    - ``required`` 只保留 properties 里真实存在的键
    """
    if isinstance(node, list):
        return [_clean_schema_node(n) for n in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    t = node.get("type")
    if isinstance(t, list):
        types = [x for x in t if x != "null"]
        t = types[0] if types else "string"
    if t:
        out["type"] = t
    if "const" in node:
        out.setdefault("enum", [node["const"]])
    if isinstance(node.get("enum"), list):
        out["enum"] = [str(x) if not isinstance(x, (int, float, bool)) else x for x in node["enum"]]
    if node.get("description"):
        out["description"] = node["description"]
    if isinstance(node.get("properties"), dict):
        out["properties"] = {
            k: _clean_schema_node(v) for k, v in node["properties"].items()
        }
        req = node.get("required")
        if isinstance(req, list):
            valid = [k for k in req if k in out["properties"]]
            if valid:
                out["required"] = valid
    if isinstance(node.get("items"), (dict, list)):
        out["items"] = _clean_schema_node(node["items"])
    # anyOf/oneOf：取第一个分支（Gemini 某些版本支持 anyOf，但拍平最稳）
    for comb in ("anyOf", "oneOf"):
        if isinstance(node.get(comb), list) and node[comb]:
            merged = _clean_schema_node(node[comb][0])
            if isinstance(merged, dict):
                for k, v in merged.items():
                    out.setdefault(k, v)
            break
    return out or {"type": "string"}


def clean_json_schema_for_gemini(schema: dict[str, Any]) -> dict[str, Any]:
    cleaned = _clean_schema_node(schema)
    return cleaned if isinstance(cleaned, dict) and cleaned else {"type": "object", "properties": {}}


def _generation_config(body: dict[str, Any]) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if body.get("temperature") is not None:
        cfg["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        cfg["topP"] = body["top_p"]
    if body.get("max_tokens") is not None:
        cfg["maxOutputTokens"] = body["max_tokens"]
    stops = body.get("stop")
    if isinstance(stops, str):
        cfg["stopSequences"] = [stops]
    elif isinstance(stops, list) and stops:
        cfg["stopSequences"] = [str(s) for s in stops]
    # reasoning_effort → thinkingConfig（Gemini 2.5+；低配额下 low 最实用）
    effort = str(body.get("reasoning_effort") or "").lower()
    if effort in ("low", "medium", "high"):
        cfg["thinkingConfig"] = {
            "thinkingBudget": {"low": 1024, "medium": 8192, "high": 24576}[effort]
        }
    elif effort == "none" or effort == "disable":
        cfg["thinkingConfig"] = {"thinkingBudget": 0}
    return cfg


def chat_to_gemini_request(
    body: dict[str, Any],
    *,
    project_id: str,
    model: str,
    stream: bool,
) -> dict[str, Any]:
    """OpenAI chat 请求 → v1internal 包装体。"""
    messages = body.get("messages") or []
    system_texts: list[str] = []
    chat_messages: list[dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            c = msg.get("content")
            if isinstance(c, str) and c:
                system_texts.append(c)
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("text"):
                        system_texts.append(str(b["text"]))
        else:
            chat_messages.append(msg)

    request: dict[str, Any] = {
        "contents": _messages_to_contents(chat_messages, model=model),
    }
    if system_texts:
        request["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_texts)}]}
    tools = body.get("tools") or []
    if tools:
        request["tools"] = _tools_to_gemini(tools)
    tool_choice = body.get("tool_choice")
    if isinstance(tool_choice, dict) and tool_choice.get("function", {}).get("name"):
        request["toolConfig"] = {
            "functionCallingConfig": {
                "mode": "ANY",
                "allowedFunctionNames": [
                    sanitize_function_name(tool_choice["function"]["name"])
                ],
            }
        }
    elif tool_choice == "required":
        request["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}
    elif tool_choice == "none":
        request["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
    gc = _generation_config(body)
    if gc:
        request["generationConfig"] = gc
    request["session_id"] = new_session_id()

    return {
        "model": f"models/{model}" if not model.startswith("models/") else model,
        "project": project_id,
        "user_prompt_id": new_user_prompt_id(),
        "request": request,
    }


# ---------------------------------------------------------------------------
# Gemini → OpenAI
# ---------------------------------------------------------------------------

def new_tool_call(fc: dict[str, Any], signature: str = "") -> dict[str, Any]:
    """上游 functionCall → OpenAI tool_call（非流式/流式共用）。

    自造唯一 id（上游短计数器 id 会撞车），签名与函数名记进 LRU 供请求
    方向凭 id 还原；OpenAI 路径同时透出 ``gemini_thought_signature``
    扩展字段（直连 OpenAI 客户端原样回传即天然命中）。
    """
    name = str(fc.get("name") or "")
    call_id = f"call_{secrets.token_hex(12)}"
    sig = signature or ""
    remember(call_id, signature=sig, name=name)
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False),
        },
        "gemini_thought_signature": sig,
    }


def inline_image_part(part: dict[str, Any]) -> dict[str, Any] | None:
    """Gemini ``inlineData`` part → OpenAI ``image_url`` block（生图模型用）。

    生图模型（如 gemini-3.1-flash-image）把图片放在 ``candidates[].content.
    parts[].inlineData``：返回 ``{"type": "image_url", "image_url": {"url":
    "data:<mime>;base64,..."}}``，流式/非流式共用。缺数据时返回 None。
    """
    payload = part.get("inlineData") or part.get("inline_data") or {}
    if not isinstance(payload, dict):
        return None
    data = str(payload.get("data") or "")
    if not data:
        return None
    mime = str(payload.get("mimeType") or payload.get("mime_type") or "image/png")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def _candidate_parts(candidate: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str, list[dict[str, str]]]:
    """candidate → (text, tool_calls, reasoning_text, images)。

    images 是生图模型（如 gemini-3.1-flash-image）返回的 ``inlineData`` 图片，
    转成 OpenAI chat 的 ``{"type": "image_url", "image_url": {"url": data URL}}``，
    聊天客户端（含 ethan 的 image-generate 技能）靠 ``message.images[]`` 取图。
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: list[dict[str, Any]] = []
    images: list[dict[str, str]] = []
    for part in candidate.get("content", {}).get("parts") or []:
        if not isinstance(part, dict):
            continue
        if "functionCall" in part:
            calls.append(new_tool_call(
                part["functionCall"] or {},
                signature=str(part.get("thoughtSignature") or ""),
            ))
        elif "inlineData" in part or "inline_data" in part:
            image = inline_image_part(part)
            if image:
                images.append(image)
        elif part.get("thought") is True and part.get("text"):
            reasoning_parts.append(str(part["text"]))
        elif "text" in part:
            text_parts.append(str(part["text"]))
    return "".join(text_parts), calls, "".join(reasoning_parts), images


def _candidate_text_and_calls(candidate: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str]:
    """candidate → (text, tool_calls, reasoning_text)。"""
    text, calls, reasoning, _ = _candidate_parts(candidate)
    return text, calls, reasoning


def _finish_reason(candidate: dict[str, Any]) -> str:
    raw = str(candidate.get("finishReason") or "")
    return {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
        "MALFORMED_FUNCTION_CALL": "tool_calls",
    }.get(raw, "tool_calls" if raw == "STOP" else "stop") or "stop"


def usage_from_metadata(usage: dict[str, Any]) -> dict[str, Any]:
    """Gemini usageMetadata → OpenAI usage（流式/非流式共用一份映射）。

    缓存命中（cachedContentTokenCount）进 prompt_tokens_details.cached_tokens：
    OpenAI 口径 prompt_tokens 已含缓存命中，这里不扣减；Anthropic 出口
    （protocols/anthropic_adapter）会读这个字段转成 cache_read_input_tokens
    并从 input_tokens 里扣除。
    """
    return {
        "prompt_tokens": int(usage.get("promptTokenCount") or 0),
        "completion_tokens": int(usage.get("candidatesTokenCount") or 0),
        "total_tokens": int(usage.get("totalTokenCount") or 0),
        "prompt_tokens_details": {
            "cached_tokens": int(usage.get("cachedContentTokenCount") or 0),
        },
    }


def gemini_response_to_chat(
    payload: dict[str, Any],
    *,
    model: str,
) -> dict[str, Any]:
    """非流式：v1internal 响应（``response`` 包一层）→ OpenAI chat.completion。"""
    inner = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    candidates = inner.get("candidates") or []
    cand = candidates[0] if candidates else {}
    text, calls, reasoning, images = _candidate_parts(cand)
    usage = inner.get("usageMetadata") or {}
    now = int(time.time())

    message: dict[str, Any] = {}
    if reasoning:
        message["reasoning_content"] = reasoning
    message["role"] = "assistant"
    message["content"] = text or None
    if calls:
        message["tool_calls"] = calls
    if images:
        message["images"] = images
    return {
        "id": f"chatcmpl-{secrets.token_hex(12)}",
        "object": "chat.completion",
        "created": now,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _finish_reason(cand),
            }
        ],
        "usage": usage_from_metadata(usage),
    }


def iter_gemini_sse_payloads(raw_stream):
    """把上游 SSE（``data: {...}`` 行）解成 dict；生成器透传异常。"""
    for raw in raw_stream:
        if isinstance(raw, bytes):
            line = raw.decode("utf-8", "replace")
        else:
            line = raw
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload
