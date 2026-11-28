"""书生·端砚（inkstone）通道：把端砚的私有推理协议适配成 OpenAI chat。

## 为什么这个通道比 Loomy 复杂

Loomy 上游就是 OpenAI 兼容网关，请求体直通、SSE 直通。端砚**没有** OpenAI 兼容
端点（``/v1/chat/completions`` 实测全是 405/404），它的协议是端砚自己的两段式：

::

    POST {base}/chat/new                       建会话 -> data.id
    POST {base}/chat/{id}/local-turns          注册 turn（每次推理一个）
    POST {base}/local-agent/messages           发推理（**只支持 SSE**）

所以本模块要做完整的**双向转换**：

- 请求：OpenAI chat body → 端砚 ``messages`` / ``system`` / ``tools``
- 响应：端砚 SSE 事件 → OpenAI ``chat.completion.chunk``

转成 OpenAI 形状之后，``anthropic`` / ``responses`` 协议复用项目现有的转换器
（``AnthropicStreamConverter`` 等），不用为每种协议各写一套。

## 端砚协议实测细节（2026-11-29，0.2.13）

推理请求体是**严格 schema**，少一个字段就 422，且错误信息只报**第一个**缺的
字段——所以下面这份字段表是逐轮试出来的，不是抄文档：

::

    {"protocol_version":"2.0",       # 必填，字面量 "2.0"
     "chat_id":2754185,              # 必填，/chat/new 返回的 id
     "turn_id":"<uuid4>",            # 必填，**小写规范 UUID**
     "request_id":"<uuid4>",         # 必填
     "inference_kind":"turn",        # 必填，turn | detached
     "model":"书生-S2.1-Flash",      # 必填，目录里的 id 或 canonical_id
     "thinking":{"enabled":false},   # 必填，**对象**（不是 bool）
     "system":["你是一个助手"],       # 必填，**字符串数组**（不是 content 块）
     "tools":[],                     # 必填，空数组也要给
     "tool_choice":{"type":"auto"},  # 必填，**对象**
     "metadata":{"locale":"zh",      # 必填；只能 zh 或 en（不是 zh-CN！）
                 "client_version":"0.2.13"},  # 必填
     "messages":[{"role":"user",
                  "content":[{"type":"text","text":"hi"}]}]}

几个反直觉的点，都是实测撞出来的：

1. ``thinking`` 是对象 ``{"enabled":bool}``，给 ``false`` 报
   ``thinking must be an object``；给 ``{"type":"disabled"}`` 报缺 ``enabled``。
2. ``system`` 是**字符串数组**。给 content 块数组（``[{"type":"text",...}]``）
   报 ``invalid JSON request``——注意这个错和「字段缺失」不同，是**形状错了**，
   排查时容易被误导成「我的 JSON 拼坏了」。
3. ``metadata.locale`` 只能是 ``zh`` / ``en``。给 ``zh-CN`` 报
   ``must be zh or en``。
4. ``tool_choice`` 是对象 ``{"type":"auto"}``，不是字符串 ``"auto"``。
5. ``messages[].content`` 必须是**块数组**，字符串不行。
6. ``tool_result`` 块的 ``content`` 是**字符串**（不是块数组），且
   ``is_error`` 必填——给块数组会回 ``invalid JSON request``。

## 只支持流式

``Accept`` 必须是 ``text/event-stream``，给 ``application/json`` 回 406
``Accept header must be exactly text/event-stream``。所以**非流式请求也在内部
走流式**，把帧聚合成一个完整 chat.completion 再返回——这是本模块
:func:`aggregate_stream` 存在的原因。

## SSE 事件（实测）

::

    {"type":"message_start","response_id":"resp_...","model":"..."}
    {"type":"thinking_delta","text":"..."}          # 思考链，可能没有
    {"type":"content_delta","text":"..."}           # 正文
    {"type":"tool_use_start","id":"call_...","name":"get_weather"}
    {"type":"tool_input_delta","id":"call_...","partial_json":"{\"city\":"}
    {"type":"tool_use_end","id":"call_..."}
    {"type":"usage","usage":{"input_tokens":161,"output_tokens":50}}
    {"type":"message_end","stop_reason":"end_turn"|"tool_use"}

``stop_reason`` 只有 ``end_turn`` / ``tool_use`` / ``max_tokens`` 等，映射到
OpenAI 的 ``finish_reason`` 见 :func:`_map_finish_reason`。

## 会话复用

``chat_id`` 可以复用（实测同一 chat 连发 3 个 turn 都正常），但**每次推理都要
新注册一个 turn**。这里按「一个 chat 最多复用 N 次」做池化，避免每个请求都
``/chat/new`` 一次把用户会话列表刷满——端砚的会话列表是用户可见的。

## 额度

额度不在 ``/api/orbit/v1`` 下，而在 ``/api/tokenplan/v1``：按 orbit 前缀拼会
404（nginx 层就没这路由）。结构见 ``quota.py``。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.errors import describe_exception
from ..providers.base import BaseProvider
from ..protocols.anthropic_adapter import (
    AnthropicStreamConverter,
    chat_completion_to_anthropic_message,
)
from . import catalog as catalog_mod
from . import credentials as creds
from . import quota as quota_mod
from .catalog import CatalogCache, InkstoneModel
from .credentials import AuthError, InkstoneSession

log = logging.getLogger(__name__)

#: 上游超时（秒）。推理是流式的，首帧可能慢（思考模型尤其），给足。
_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)

#: 管理端点（建会话 / 注册 turn）的超时。
_ADMIN_TIMEOUT = httpx.Timeout(30.0)

#: 单个 chat 最多复用几次。端砚会话列表用户可见，别刷太满。
_MAX_TURNS_PER_CHAT = 4

#: 每个 chat 复用计数（chat_id -> 已用次数）。
_MAX_CHATS_CACHED = 64

#: 上游 ``stop_reason`` → OpenAI ``finish_reason``。
_FINISH_REASONS = {
    "end_turn": "stop",
    "stop": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "max_output_tokens": "length",
    "content_filter": "content_filter",
}


def _map_finish_reason(reason: str) -> str:
    """端砚 ``stop_reason`` → OpenAI ``finish_reason``（未知按 stop）。"""
    return _FINISH_REASONS.get((reason or "").strip(), "stop")


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _strip_prefix(model: str, provider_id: str) -> str:
    """剥掉 ``inkstone/`` 前缀。"""
    want = (model or "").strip()
    prefix = f"{provider_id}/"
    return want[len(prefix):] if want.startswith(prefix) else want


# ----------------------------------------------------------------------
# OpenAI → 端砚 请求体
# ----------------------------------------------------------------------


def _text_of(content: Any) -> str:
    """从 OpenAI 的 content（字符串或块数组）里抽纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("text", "input_text") and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def _convert_messages(messages: Any) -> list[dict[str, Any]]:
    """OpenAI ``messages`` → 端砚 ``messages``。

    端砚的 ``system`` 是**独立字段**（字符串数组），所以 system 消息要摘出去，
    不能留在 messages 里。``tool`` 角色转成 ``tool_result`` 块，``assistant`` 的
    ``tool_calls`` 转成 ``tool_use`` 块。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(messages, list):
        return out

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip()
        if role == "system":
            # system 由 _convert_system 单独处理
            continue

        if role == "tool":
            # OpenAI: {"role":"tool","tool_call_id":...,"content":"..."}
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(msg.get("tool_call_id") or ""),
                            "is_error": False,
                            # 端砚要字符串，给块数组会 invalid JSON request
                            "content": _text_of(msg.get("content")),
                        }
                    ],
                }
            )
            continue

        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = _text_of(msg.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for call in msg.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                raw_args = fn.get("arguments")
                try:
                    parsed = json.loads(raw_args) if isinstance(raw_args, str) and raw_args else {}
                except (ValueError, json.JSONDecodeError):
                    parsed = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id") or ""),
                        "name": str(fn.get("name") or ""),
                        "input": parsed if isinstance(parsed, dict) else {},
                    }
                )
            if not blocks:
                blocks.append({"type": "text", "text": ""})
            out.append({"role": "assistant", "content": blocks})
            continue

        # user（及未知角色按 user 处理）
        content = msg.get("content")
        if isinstance(content, list):
            blocks = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in ("text", "input_text"):
                    blocks.append({"type": "text", "text": str(block.get("text") or "")})
                # 图片：端砚的 content 块**不支持** image（实测各种字段名都
                # 422），且目录里所有模型 supports_attachments=false，所以这里
                # 直接丢掉图片块，只留文本——比整条请求 422 好。
            if not blocks:
                blocks.append({"type": "text", "text": ""})
            out.append({"role": "user", "content": blocks})
        else:
            out.append(
                {"role": "user", "content": [{"type": "text", "text": _text_of(content)}]}
            )
    return out


def _convert_system(messages: Any) -> list[str]:
    """把 OpenAI 的 system 消息抽成端砚要的**字符串数组**。"""
    out: list[str] = []
    if not isinstance(messages, list):
        return out
    for msg in messages:
        if not isinstance(msg, dict) or str(msg.get("role") or "") != "system":
            continue
        text = _text_of(msg.get("content"))
        if text:
            out.append(text)
    return out


def _convert_tools(tools: Any) -> list[dict[str, Any]]:
    """OpenAI ``tools`` → 端砚 ``tools``。

    OpenAI: ``{"type":"function","function":{"name","description","parameters"}}``
    端砚:   ``{"name","description","input_schema"}``
    """
    out: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        schema = fn.get("parameters")
        out.append(
            {
                "name": name,
                "description": str(fn.get("description") or ""),
                "input_schema": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
            }
        )
    return out


def _convert_tool_choice(choice: Any) -> dict[str, Any]:
    """OpenAI ``tool_choice`` → 端砚 ``{"type": ...}``。"""
    if isinstance(choice, dict):
        # 已经是对象形状（``{"type":"function","function":{"name":...}}``）
        kind = str(choice.get("type") or "").strip()
        if kind == "function":
            fn = choice.get("function") if isinstance(choice.get("function"), dict) else {}
            return {"type": "tool", "name": str(fn.get("name") or "")}
        if kind in ("auto", "any", "none", "required"):
            return {"type": "auto" if kind == "required" else kind}
    if isinstance(choice, str) and choice.strip():
        kind = choice.strip()
        return {"type": "auto" if kind == "required" else kind}
    return {"type": "auto"}


def build_inference_body(
    body: dict[str, Any],
    *,
    model: str,
    chat_id: int,
    turn_id: str,
    thinking: bool = False,
    client_version: str = "",
) -> dict[str, Any]:
    """OpenAI chat body → 端砚 ``/local-agent/messages`` 请求体。

    字段顺序不影响上游，但**少一个就 422**，见模块说明的字段表。
    """
    messages = body.get("messages")
    system = _convert_system(messages)
    # 也支持顶层 ``system``（Anthropic 协议转过来时会落在这里）
    extra_system = body.get("system")
    if isinstance(extra_system, str) and extra_system.strip():
        system.append(extra_system.strip())
    elif isinstance(extra_system, list):
        text = _text_of(extra_system)
        if text:
            system.append(text)

    return {
        "protocol_version": creds.PROTOCOL_VERSION,
        "chat_id": chat_id,
        "turn_id": turn_id,
        "request_id": _new_uuid(),
        "inference_kind": "turn",
        "model": model,
        "thinking": {"enabled": bool(thinking)},
        "system": system,
        "tools": _convert_tools(body.get("tools")),
        "tool_choice": _convert_tool_choice(body.get("tool_choice")),
        "metadata": {
            # 只能 zh / en——给 zh-CN 会被拒
            "locale": "zh",
            "client_version": client_version or creds.client_version(),
        },
        "messages": _convert_messages(messages),
    }


# ----------------------------------------------------------------------
# 端砚 SSE → OpenAI chunk
# ----------------------------------------------------------------------


def translate_event(event: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    """把一个端砚 SSE 事件转成 0..n 个 OpenAI ``chat.completion.chunk``。

    ``state`` 在多次调用间共享，用来记住 ``response_id`` / 工具调用序号 /
    是否已经发过 role 帧。
    """
    kind = str(event.get("type") or "")
    out: list[dict[str, Any]] = []

    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "id": state.get("id") or "chatcmpl-inkstone",
            "object": "chat.completion.chunk",
            "created": state.get("created") or int(time.time()),
            "model": state.get("model") or "",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    if kind == "message_start":
        state["id"] = str(event.get("response_id") or state.get("id") or "chatcmpl-inkstone")
        state["model"] = str(event.get("model") or state.get("model") or "")
        # 首个 chunk 带 role，OpenAI 客户端靠它建消息
        out.append(chunk({"role": "assistant"}))
        return out

    if kind == "thinking_delta":
        text = event.get("text")
        if isinstance(text, str) and text:
            # 思考链放 reasoning_content（DeepSeek/GLM 系客户端认这个字段）
            out.append(chunk({"reasoning_content": text}))
        return out

    if kind == "content_delta":
        text = event.get("text")
        if isinstance(text, str) and text:
            out.append(chunk({"content": text}))
        return out

    if kind == "tool_use_start":
        index = state.get("tool_index", 0)
        state["tool_index"] = index + 1
        state.setdefault("tools", {})[str(event.get("id") or "")] = index
        out.append(
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": str(event.get("id") or ""),
                            "type": "function",
                            "function": {"name": str(event.get("name") or ""), "arguments": ""},
                        }
                    ]
                }
            )
        )
        return out

    if kind == "tool_input_delta":
        tool_id = str(event.get("id") or "")
        index = (state.get("tools") or {}).get(tool_id, 0)
        partial = event.get("partial_json")
        if isinstance(partial, str) and partial:
            out.append(
                chunk(
                    {
                        "tool_calls": [
                            {"index": index, "function": {"arguments": partial}}
                        ]
                    }
                )
            )
        return out

    if kind == "usage":
        usage = event.get("usage")
        if isinstance(usage, dict):
            state["usage"] = usage
        return out

    if kind == "message_end":
        finish = _map_finish_reason(str(event.get("stop_reason") or ""))
        final = chunk({}, finish)
        usage = state.get("usage")
        if isinstance(usage, dict):
            final["usage"] = {
                "prompt_tokens": int(usage.get("input_tokens") or 0),
                "completion_tokens": int(usage.get("output_tokens") or 0),
                "total_tokens": int(usage.get("input_tokens") or 0)
                + int(usage.get("output_tokens") or 0),
            }
        out.append(final)
        state["finished"] = True
        return out

    # 其它事件（workflow_* / error）暂不透传
    if kind == "error":
        message = str(event.get("message") or event.get("code") or "端砚上游返回错误")
        out.append(
            {
                "id": state.get("id") or "chatcmpl-inkstone",
                "object": "chat.completion.chunk",
                "created": state.get("created") or int(time.time()),
                "model": state.get("model") or "",
                "choices": [{"index": 0, "delta": {"content": f"\n[error] {message}"}, "finish_reason": "stop"}],
            }
        )
        state["finished"] = True
    return out


def sse_data_lines(raw: str) -> list[str]:
    """从 SSE 原始文本里抽出所有 ``data:`` 行（一帧一行 JSON）。"""
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload and payload != "[DONE]":
                out.append(payload)
    return out


def aggregate_stream(chunks: list[dict[str, Any]], model: str) -> dict[str, Any]:
    """把 OpenAI chunk 列表聚合成一个完整 ``chat.completion``。

    端砚只给流式，但客户端可能要非流式响应，所以要在内部聚合。
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason = "stop"
    usage: dict[str, Any] = {}
    cid = "chatcmpl-inkstone"
    created = int(time.time())

    for chunk in chunks:
        cid = chunk.get("id") or cid
        created = chunk.get("created") or created
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content_parts.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning_parts.append(delta["reasoning_content"])
            for call in delta.get("tool_calls") or []:
                idx = int(call.get("index") or 0)
                slot = tool_calls.setdefault(
                    idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]

    out: dict[str, Any] = {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage:
        out["usage"] = usage
    return out


# ----------------------------------------------------------------------
# Provider
# ----------------------------------------------------------------------


class InkstoneProvider(BaseProvider):
    """书生·端砚通道。"""

    id = "inkstone"
    name = "书生·端砚 (Intern AI)"

    #: 端砚没有点击签到（免费额度是月度自动发），见 ``quota.py``。
    supports_checkin = False

    def __init__(self) -> None:
        self._catalog = CatalogCache()
        self._session: InkstoneSession | None = None
        self._base_url = ""
        self._billing_url = ""
        self._last_catalog_error = ""
        # chat 复用计数：chat_id -> 已用 turn 数
        self._chat_uses: dict[int, int] = {}

    # ------------------------------------------------------------------
    # 认证 / 目录
    # ------------------------------------------------------------------

    def ensure_auth(self) -> None:
        """校验登录态并预热模型目录；失败抛 401（带可操作提示）。"""
        try:
            session = creds.load_session()
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        self._session = session
        self._base_url = creds.resolve_base_url()
        self._billing_url = creds.resolve_billing_base_url()

        try:
            models = self._fetch_catalog()
            self._catalog.store(models)
            self._last_catalog_error = ""
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 启动期目录失败不该阻断
            self._last_catalog_error = describe_exception(exc)
            log.warning("端砚模型目录预热失败（不影响启动）：%s", self._last_catalog_error)

    def _require_session(self) -> InkstoneSession:
        """取登录态；缓存为空时重读（用户可能刚在客户端里重登）。"""
        if self._session is None:
            try:
                self._session = creds.load_session()
            except AuthError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
        if not self._base_url:
            self._base_url = creds.resolve_base_url()
        if not self._billing_url:
            self._billing_url = creds.resolve_billing_base_url()
        return self._session

    def _refresh_session(self) -> InkstoneSession:
        """丢掉缓存的登录态重读一次（401 后重试用）。"""
        self._session = None
        self._catalog.invalidate()
        self._chat_uses.clear()
        return self._require_session()

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        """鉴权头。

        ``Authorization: Bearer <jwt>`` + ``id: <ssoUid>``——两个都要，客户端
        自己也这么发。缺 ``id`` 上游回 401（不是 400）。
        """
        session = self._require_session()
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {session.token}",
        }
        if session.sso_uid:
            headers["id"] = session.sso_uid
        if accept == "application/json":
            headers["Content-Type"] = "application/json"
        return headers

    def _fetch_catalog(self) -> list[InkstoneModel]:
        url = f"{self._base_url.rstrip('/')}/local-agent/models"
        with httpx.Client(timeout=_ADMIN_TIMEOUT) as client:
            resp = client.get(
                url,
                params={"supports_output_resource_limits": "true"},
                headers=self._headers(),
            )
        _raise_for_envelope(resp, "/local-agent/models")
        models = catalog_mod.parse_catalog(resp.json())
        if not models:
            raise RuntimeError("端砚 /local-agent/models 未返回任何模型")
        return models

    def _ensure_catalog(self) -> list[InkstoneModel]:
        """按需刷新目录。失败时**回落到旧缓存**（有的话）。"""
        if self._catalog.is_fresh():
            return self._catalog.models
        try:
            models = self._fetch_catalog()
            self._catalog.store(models)
            self._last_catalog_error = ""
            return models
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - 刷新失败用旧缓存
            self._last_catalog_error = describe_exception(exc)
            log.warning("端砚模型目录刷新失败，沿用缓存：%s", self._last_catalog_error)
            cached = self._catalog.models
            if cached:
                return cached
            raise

    def models(self) -> Sequence[dict[str, Any]]:
        """``/v1/models`` 的元素列表。

        目录拉不到时返回**空列表**而不是抛错：这是纯读接口，让它在网关抖动时
        整体 500 会把所有通道的模型列表一起拖垮。
        """
        try:
            models = self._ensure_catalog()
        except Exception as exc:  # noqa: BLE001
            log.warning("端砚模型列表不可用：%s", describe_exception(exc))
            return []
        return [m.to_openai_model(self.id) for m in models]

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        """只认目录内的 id（含 ``inkstone/`` 前缀、canonical_id、显示名）。

        ``canonical_id`` 当别名：``sage-s2.1-flash`` 与 ``书生-S2.1-Flash`` 是
        同一个模型的两个名字，用户两种写法都该能用。
        """
        want = (model or "").strip()
        if not want:
            return False
        bare = _strip_prefix(want, self.id)

        try:
            catalog = self._ensure_catalog()
        except Exception:  # noqa: BLE001 - 目录不可用时退化成前缀判断
            return False

        for m in catalog:
            if bare == m.id:
                return True
            if aliases and bare in (m.canonical_id, m.display_name):
                return True
        return False

    def health(self) -> dict[str, Any]:
        session = self._session
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self._base_url or creds.resolve_base_url(),
            "account": session.display if session else "",
            "models": len(self._catalog.models),
            "catalog_error": self._last_catalog_error,
        }

    # ------------------------------------------------------------------
    # 额度
    # ------------------------------------------------------------------

    def quota(self) -> dict[str, Any] | None:
        """套餐额度 → 管理页额度卡。

        端砚的额度是 **Token Plan**（5 小时窗 + 7 天窗双窗口），不是积分余额。
        两个窗口都要展示：5h 窗短、7d 窗长，用户关心的是「现在还能不能用」和
        「这周还剩多少」。
        """
        try:
            # 只取一次 session：``_require_session()`` 在缓存为空时会读盘 + 解密，
            # 调两次等于把凭据解两遍（还要两次 Keychain）。
            session = self._require_session()
            balance = quota_mod.fetch_balance(
                self._billing_url, session.token, session.sso_uid
            )
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - 额度查询失败不该影响转发
            log.warning("端砚额度查询失败：%s", describe_exception(exc))
            return None
        return quota_mod.to_quota_items(balance)

    # ------------------------------------------------------------------
    # 转发
    # ------------------------------------------------------------------

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        """转发 chat 请求（端砚私有协议 ↔ OpenAI）。

        端砚**只支持流式**，所以：

        - ``stream=True``：边转边发，SSE 直通
        - ``stream=False``：内部走流式、聚合后返回一个完整 ``chat.completion``
        """
        session = self._require_session()
        model = _strip_prefix(str(body.get("model") or ""), self.id)
        if not model:
            raise HTTPException(status_code=400, detail="缺少 model 字段")

        # canonical_id → 客户端名（上游两个都收，但用目录里的 id 更稳）
        model = self._resolve_model_name(model)
        thinking = _wants_thinking(body)
        stream = bool(body.get("stream"))

        chat_id = await self._acquire_chat(session)
        turn_id = _new_uuid()
        await self._register_turn(session, chat_id, turn_id, model, thinking, body)

        upstream_body = build_inference_body(
            body,
            model=model,
            chat_id=chat_id,
            turn_id=turn_id,
            thinking=thinking,
        )

        if stream:
            return await self._forward_stream(upstream_body, model, protocol, session)
        return await self._forward_aggregated(
            upstream_body, model, protocol, session, original=body
        )

    def _resolve_model_name(self, model: str) -> str:
        """把 canonical_id / 显示名归一成目录里的 ``id``。

        找不到就原样返回，让上游自己报错——比在这里拦下来给一个「不知道」的
        错误更有用。
        """
        try:
            for m in self._ensure_catalog():
                if model in (m.id, m.canonical_id, m.display_name):
                    return m.id
        except Exception:  # noqa: BLE001
            return model
        return model

    async def _acquire_chat(self, session: InkstoneSession) -> int:
        """取一个可复用的 chat_id；用满 :data:`_MAX_TURNS_PER_CHAT` 次就换新的。

        端砚的会话列表用户可见，所以不能每个请求都 ``/chat/new``。
        """
        for chat_id, used in list(self._chat_uses.items()):
            if used < _MAX_TURNS_PER_CHAT:
                self._chat_uses[chat_id] = used + 1
                return chat_id
        chat_id = await self._create_chat(session)
        # 简单上限：超过就清掉最旧的（dict 保序）
        while len(self._chat_uses) >= _MAX_CHATS_CACHED:
            self._chat_uses.pop(next(iter(self._chat_uses)))
        self._chat_uses[chat_id] = 1
        return chat_id

    async def _create_chat(self, session: InkstoneSession) -> int:
        """``POST /chat/new`` 建会话。"""
        url = f"{self._base_url.rstrip('/')}/chat/new"
        payload = {"scenario": 2, "type": 0, "provider": 0, "agent": "local_agent"}
        async with httpx.AsyncClient(timeout=_ADMIN_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
        _raise_for_envelope(resp, "/chat/new")
        data = (resp.json() or {}).get("data")
        if not isinstance(data, dict):
            raise HTTPException(status_code=502, detail="端砚 /chat/new 响应缺少 data")
        try:
            chat_id = int(data.get("id"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=502, detail="端砚 /chat/new 返回了无效的会话 id"
            ) from exc
        if chat_id <= 0:
            raise HTTPException(status_code=502, detail="端砚 /chat/new 返回了无效的会话 id")
        return chat_id

    async def _register_turn(
        self,
        session: InkstoneSession,
        chat_id: int,
        turn_id: str,
        model: str,
        thinking: bool,
        body: dict[str, Any],
    ) -> None:
        """``POST /chat/{id}/local-turns`` 注册 turn。

        每次推理都要注册一个——不注册直接打 ``/local-agent/messages`` 会回
        ``404002 turn not found``。
        """
        url = f"{self._base_url.rstrip('/')}/chat/{chat_id}/local-turns"
        # user_prompt 是必填的「这一轮用户说了什么」，取最后一条 user 消息
        prompt = _last_user_text(body.get("messages"))
        payload = {
            "protocol_version": creds.PROTOCOL_VERSION,
            "turn_id": turn_id,
            "execution_mode": "instant",
            "model": model,
            "thinking_enabled": bool(thinking),
            "user_prompt": prompt or " ",
        }
        async with httpx.AsyncClient(timeout=_ADMIN_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
        _raise_for_envelope(resp, "/chat/{id}/local-turns")

    async def _forward_stream(
        self,
        upstream_body: dict[str, Any],
        model: str,
        protocol: str,
        session: InkstoneSession,
    ) -> StreamingResponse:
        """流式：端砚 SSE → OpenAI chunk SSE（或 Anthropic 事件流）。"""
        response = await self._open_inference(upstream_body, session)

        if protocol == "anthropic":
            converter = AnthropicStreamConverter(model)

            async def anthropic_events() -> AsyncIterator[str]:
                state: dict[str, Any] = {"model": model, "created": int(time.time())}
                try:
                    async for event in _iter_events(response):
                        for chunk in translate_event(event, state):
                            for name, payload in converter.feed_chunk(chunk):
                                yield f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    for name, payload in converter.finish():
                        yield f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                finally:
                    await _close(response)

            return StreamingResponse(
                anthropic_events(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        async def openai_events() -> AsyncIterator[str]:
            state: dict[str, Any] = {"model": model, "created": int(time.time())}
            try:
                async for event in _iter_events(response):
                    for chunk in translate_event(event, state):
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    if state.get("finished"):
                        break
                yield "data: [DONE]\n\n"
            finally:
                await _close(response)

        return StreamingResponse(
            openai_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _forward_aggregated(
        self,
        upstream_body: dict[str, Any],
        model: str,
        protocol: str,
        session: InkstoneSession,
        original: dict[str, Any] | None = None,
    ) -> JSONResponse:
        """非流式：内部走流式、聚合后返回完整响应。

        端砚给 ``Accept: application/json`` 会回 406，所以没有真正的非流式路径。
        """
        response = await self._open_inference(upstream_body, session)
        state: dict[str, Any] = {"model": model, "created": int(time.time())}
        chunks: list[dict[str, Any]] = []
        try:
            async for event in _iter_events(response):
                chunks.extend(translate_event(event, state))
                if state.get("finished"):
                    break
        finally:
            await _close(response)

        payload = aggregate_stream(chunks, model)
        if protocol == "anthropic":
            # ``original`` 只用来回填响应的 ``model`` 字段：给**客户端原始请求**
            # 才回得出它自己写的名字（可能是 ``inkstone/书生-S2.1-Flash``），
            # 给端砚请求体就只能回剥掉前缀的内部名。
            return JSONResponse(
                chat_completion_to_anthropic_message(payload, original or upstream_body)
            )
        return JSONResponse(payload)

    async def _open_inference(
        self, upstream_body: dict[str, Any], session: InkstoneSession
    ) -> httpx.Response:
        """发推理请求（流式），返回未消费的 response。

        ``Accept`` 必须是 ``text/event-stream``（给 json 回 406），见模块说明。
        """
        url = f"{self._base_url.rstrip('/')}/local-agent/messages"
        headers = self._headers(accept="text/event-stream")
        headers["Content-Type"] = "application/json"

        client = httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            request = client.build_request("POST", url, headers=headers, json=upstream_body)
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(
                status_code=502, detail=f"端砚上游连接失败：{describe_exception(exc)}"
            ) from exc

        if response.status_code == 401:
            await _close(response)
            await client.aclose()
            # 登录态可能刚在客户端里续过：重读一次再试
            self._refresh_session()
            headers = self._headers(accept="text/event-stream")
            headers["Content-Type"] = "application/json"
            client = httpx.AsyncClient(timeout=_TIMEOUT)
            try:
                request = client.build_request("POST", url, headers=headers, json=upstream_body)
                response = await client.send(request, stream=True)
            except httpx.HTTPError as exc:
                await client.aclose()
                raise HTTPException(
                    status_code=502,
                    detail=f"端砚上游连接失败：{describe_exception(exc)}",
                ) from exc

        if response.status_code >= 400:
            detail = await _read_error(response)
            await _close(response)
            await client.aclose()
            raise HTTPException(status_code=response.status_code, detail=detail)

        # 把 client 挂在 response 上，流结束后由 _close 一起关
        response._inkstone_client = client  # type: ignore[attr-defined]
        return response


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------


def _wants_thinking(body: dict[str, Any]) -> bool:
    """请求是否要开思考链。

    客户端可能用 ``reasoning_effort`` / ``thinking`` / ``enable_thinking`` 表达，
    端砚只有一个布尔开关。任一显式开启就开。
    """
    effort = body.get("reasoning_effort")
    if isinstance(effort, str) and effort.strip() and effort.strip() != "none":
        return True
    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        return bool(thinking.get("enabled"))
    if isinstance(thinking, bool):
        return thinking
    if body.get("enable_thinking") is True:
        return True
    return False


def _last_user_text(messages: Any) -> str:
    """最后一条 user 消息的纯文本（``local-turns`` 的 ``user_prompt``）。"""
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if isinstance(msg, dict) and str(msg.get("role") or "") == "user":
            return _text_of(msg.get("content")).strip()
    return ""


async def _iter_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """把端砚 SSE 流拆成一个个事件 dict。

    上游是 ``data: {json}`` 一行一帧，没有 event 名。用 ``aiter_lines`` 逐行读
    ——``aiter_bytes`` 自己切帧会在多字节中文被拆开时炸掉。
    """
    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(event, dict):
            yield event


async def _read_error(response: httpx.Response) -> str:
    """读上游错误正文，拼成给人看的 detail。"""
    try:
        raw = await response.aread()
    except Exception:  # noqa: BLE001
        return f"端砚上游返回 {response.status_code}"
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return f"端砚上游返回 {response.status_code}"
    try:
        payload = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return text[:500]
    if isinstance(payload, dict):
        msg = payload.get("msg") or payload.get("message")
        code = payload.get("code")
        if msg:
            return f"端砚上游错误 {code}：{msg}" if code is not None else str(msg)
    return text[:500]


async def _close(response: httpx.Response) -> None:
    """关掉流式响应和它背后的 client。"""
    client = getattr(response, "_inkstone_client", None)
    try:
        await response.aclose()
    except Exception:  # noqa: BLE001
        pass
    if client is not None:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass


def _raise_for_envelope(resp: httpx.Response, action: str) -> None:
    """校验网关 envelope（``code != 0`` 也算失败，HTTP 200 也可能是错）。"""
    if resp.status_code == 401:
        raise HTTPException(
            status_code=401,
            detail="端砚登录态失效。请在端砚里重新登录，再重启 buddy。",
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"端砚 {action} 返回 {resp.status_code}：{resp.text[:300]}",
        )
    try:
        payload = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"端砚 {action} 响应不是 JSON：{exc}") from exc
    if isinstance(payload, dict):
        code = payload.get("code")
        if code not in (None, creds.OK_CODE, "0"):
            msg = payload.get("msg") or payload.get("message") or ""
            raise HTTPException(
                status_code=502,
                detail=f"端砚 {action} 失败（code={code}）：{msg}",
            )
