"""Kiro provider：AWS CodeWhisperer 直连（多账号 + 双协议）。

## 协议映射

客户端三种协议都要能走，上游只有一套 ``generateAssistantResponse``：

| 客户端协议 | 入口 | 做法 |
|---|---|---|
| ``openai`` | ``/v1/chat/completions`` | chat 请求体直接翻译成 Kiro 的 conversationState |
| ``anthropic`` | ``/v1/messages`` | routes 层已把 anthropic 转成 chat（``body``），本通道照 chat 处理，**响应**再用 Anthropic 事件流输出 |
| ``responses`` | 不支持 | 回 501 并给出可操作提示 |

**响应**侧不需要回读 ``original``：Anthropic 的 SSE 由
:func:`.stream.to_anthropic_events` 直接生成（事件序列固定），不像 chat 那样
依赖请求形状。

## 工具调用

Kiro 的 ``userInputMessageContext`` 支持 tools，但**格式与 OpenAI 不同**
（``tools: [{toolSpecification: {...}}]``）。本轮先只做纯文本对话——工具调用
需要额外的双向转换与流式 tool_use 事件拼装，值得单独一轮。请求里带了 tools
时**如实降级**：不报错、不静默丢弃，而是打一条 warn 并只发文本部分。这样用户
能立刻看出「工具没生效」而不是收到莫名的行为差异。
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

from . import credentials as creds
from . import stream as kiro_stream
from .credentials import KIRO_UA_VERSION, KiroAccount
from ..providers.base import BaseProvider
from ..core.errors import describe_exception

log = logging.getLogger(__name__)

#: 上游推理端点（区域由账号决定）。
_CHAT_PATH = "/generateAssistantResponse"
_MODELS_PATH = "/ListAvailableModels"

#: 与 Kiro 客户端一致的 UA。**必须带 Kiro/<version>**：少了它上游会回
#: 403「Your subscription does not support this application」，极易被误判成
#: token 或订阅问题（见 credentials 模块头的踩坑记录）。
_USER_AGENT = f"aws-sdk-js/1.0.0 Kiro/{KIRO_UA_VERSION}"

#: EventStream 的 Accept。改成 text/event-stream 会被上游换成 SSE 吗？实测不会
#: ——它仍回二进制帧，所以这里如实声明。
_ACCEPT = "application/vnd.amazon.eventstream"

_TIMEOUT = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=15.0)

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "close"}


def _num(value: Any) -> float | None:
    """宽松取数：数字/数字字符串都收；bool/None/坏值返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _sse_json(payload: dict[str, Any]) -> str:
    """一帧 OpenAI SSE。"""
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def endpoint_base(region: str) -> str:
    """推理端点基址。Kiro 用区域化的 ``q.<region>.amazonaws.com``。"""
    return f"https://q.{region or creds.DEFAULT_REGION}.amazonaws.com"


def auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": _ACCEPT,
        "User-Agent": _USER_AGENT,
        "X-Amz-User-Agent": _USER_AGENT,
    }


def _messages_to_conversation(body: dict[str, Any]) -> tuple[str, str]:
    """把 chat 的 messages 压成 Kiro 的 conversationState。

    Kiro 的形状是「历史 + 当前消息」，没有独立的 system 字段——system 内容
    拼到**当前用户消息**前面（这是社区实现的通行做法，实测可用）。

    返回 ``(content, model_id)``。

    只取最后一条 user 作为 currentMessage，其余按序进 history。连续同角色
    的消息合并：Kiro 对角色交替没有严格校验，但合并能少掉「assistant 空串」
    这类上游会挑刺的形态。
    """
    messages = body.get("messages") or []
    system_parts: list[str] = []
    turns: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        content = msg.get("content")
        if isinstance(content, list):
            # 分块数组：只拼 text 块（图片需要额外的 image 字段，本轮不做）
            content = "".join(
                str(b.get("text") or "") for b in content
                if isinstance(b, dict) and b.get("type") == "text")
        text = str(content or "")
        if role in ("system", "developer"):
            if text.strip():
                system_parts.append(text)
            continue
        if role == "tool":
            # 工具结果在纯文本模式下当普通上下文
            text = f"[tool result] {text}"
            role = "user"
        if role not in ("user", "assistant"):
            continue
        if turns and turns[-1]["role"] == role and role == "user":
            turns[-1]["content"] += "\n\n" + text
        else:
            turns.append({"role": role, "content": text})
    model_id = str(body.get("model") or "").strip()
    current = turns.pop() if turns else {"role": "user", "content": "hi"}
    content = str(current.get("content") or "")
    if system_parts:
        prefix = "\n\n".join(system_parts)
        content = f"{prefix}\n\n{content}" if content else prefix
    return content, model_id


def _chat_to_kiro(body: dict[str, Any]) -> dict[str, Any]:
    """chat 请求体 → Kiro ``generateAssistantResponse`` 请求体。

    ⚠ **不要带 ``editorState``**：实测带它回 ``400 REQUEST_BODY_INVALID``，
    去掉就 200。``profileArn`` 有就带、没有也能通（两种都验过）。
    """
    content, model_id = _messages_to_conversation(body)
    user_message: dict[str, Any] = {
        "content": content,
        "modelId": model_id,
        "origin": "AI_EDITOR",
    }
    payload: dict[str, Any] = {
        "conversationState": {
            "chatTriggerType": "MANUAL",
            "conversationId": str(uuid.uuid4()),
            "currentMessage": {"userInputMessage": user_message},
        },
    }
    if body.get("tools"):
        # 见模块头：本轮只做纯文本，如实告警而不是静默丢
        log.warning("kiro 暂不支持工具调用（请求带 %d 个 tools），本次按纯文本转发",
                    len(body["tools"]))
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        # 上游字段名就叫 maxTokens，放在 conversationState 下
        payload["conversationState"]["currentMessage"]["userInputMessage"][
            "userInputMessageContext"] = {"maxTokens": max_tokens}
    return payload


class KiroProvider(BaseProvider):
    """Kiro（AWS CodeWhisperer）通道。"""

    id = "kiro"
    name = "Kiro (CodeWhisperer)"

    supports_checkin = False

    #: 与 yuntoken 同档：网关/聚合型通道在裸名撞车时让位给原生通道。
    route_priority = 50

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._models_cache: list[dict[str, Any]] = []
        self._models_error = ""

    # -- 模型目录 -----------------------------------------------------------

    def models(self) -> Sequence[dict[str, Any]]:
        return list(self._models_cache)

    async def refresh_models(self, force: bool = False) -> list[dict[str, Any]]:
        """逐账号拉 ``ListAvailableModels`` 取并集。

        目录是**账号级**的（不同订阅可见模型不同），所以并集而不是只看第一个
        账号——与 qoder 同一个理由。单账号失败只收缩并集。
        """
        del force
        accounts = creds.list_accounts()
        if not accounts:
            self._models_error = "没有已登录的 kiro 账号"
            return list(self._models_cache)
        union: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for acct in accounts:
            try:
                entries = await self._fetch_models(acct)
            except Exception as exc:  # noqa: BLE001 - 单账号失败不阻断整体
                errors.append(f"{acct.label}: {str(exc)[:60]}")
                log.warning("kiro 目录刷新跳过账号 %s: %s", acct.label, exc)
                continue
            for entry in entries:
                union.setdefault(str(entry.get("id") or ""), entry)
        if union:
            self._models_cache = list(union.values())
            self._models_error = ""
        elif errors:
            self._models_error = "; ".join(errors)
        return list(self._models_cache)

    async def _fetch_models(self, acct: KiroAccount) -> list[dict[str, Any]]:
        token, cred = creds.ensure_account_token(acct.id)
        region = str(cred.get("region") or acct.region)
        url = f"{endpoint_base(region)}{_MODELS_PATH}"
        client = await self._get_client()
        resp = await client.get(url, headers=auth_headers(token),
                                params={"origin": "AI_EDITOR", "maxResults": 50})
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
        payload = resp.json()
        entries = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise RuntimeError("响应里没有 models")
        out = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("modelId") or "").strip()
            if not mid:
                continue
            model: dict[str, Any] = {
                "id": mid,
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "name": str(item.get("modelName") or mid),
            }
            # 上游给了输入模态就用它（TEXT/IMAGE）；没给就不猜
            types = item.get("supportedInputTypes")
            if isinstance(types, list):
                model["images"] = any(str(t).upper() == "IMAGE" for t in types)
            rate = item.get("rateMultiplier")
            if isinstance(rate, (int, float)) and not isinstance(rate, bool):
                model["credits"] = round(float(rate), 2)
            out.append(model)
        return out

    # -- 额度（管理页「额度」卡） -------------------------------------------

    def quota(self) -> dict[str, Any] | None:
        """逐账号查 ``getUsageLimits``，聚合成管理页的额度条目。

        上游结构（实测）::

            {"subscriptionInfo": {"subscriptionTitle": "KIRO PRO"},
             "usageBreakdownList": [{
                "displayName": "Credit", "unit": "INVOCATIONS",
                "usageLimit": 1000, "currentUsageWithPrecision": 0.99,
                "nextDateReset": 1796083200.0, ...}],
             "nextDateReset": 1796083200.0}

        映射口径：

        - ``used`` = ``currentUsageWithPrecision``（没有时退 ``currentUsage``）
        - ``total`` = ``usageLimitWithPrecision`` / ``usageLimit``
        - ``reset_ts`` = 该条目的 ``nextDateReset``（**周期性**重置，符合语义）
        - ``unit`` = ``"credit"``（界面的到期告警按这个做量过滤）

        多账号时条目标签带 ``Kiro #N · 订阅名``，与其它多账号通道一致。单账号
        查询失败只跳过它，不拖垮整卡。
        """
        accounts = creds.list_accounts()
        if not accounts:
            return None
        multi = len(accounts) > 1
        items: list[dict[str, Any]] = []
        failed: list[str] = []
        level: str | None = None
        for index, acct in enumerate(accounts, 1):
            try:
                usage = self._usage_one(acct.id)
            except Exception as exc:  # noqa: BLE001 - 单账号失败不阻塞整页
                log.warning("kiro 额度查询失败（%s）: %s", acct.label, exc)
                failed.append(f"#{index}")
                continue
            title = str((usage.get("subscriptionInfo") or {}).get("subscriptionTitle") or "")
            if level is None and title:
                level = title
            prefix = f"Kiro #{index} · " if multi else ""
            for pack in usage.get("usageBreakdownList") or []:
                if not isinstance(pack, dict):
                    continue
                total = _num(pack.get("usageLimitWithPrecision") or pack.get("usageLimit"))
                used = _num(pack.get("currentUsageWithPrecision") or pack.get("currentUsage"))
                if total is None:
                    continue
                if used is None:
                    used = 0.0
                name = str(pack.get("displayName") or "Credit")
                items.append({
                    "label": f"{prefix}{name}",
                    "used": used,
                    "total": total,
                    "remaining": max(0.0, round(total - used, 2)),
                    "percent": round(used / total * 100) if total else 0,
                    # nextDateReset 是**周期重置**（额度回满），不是权益到期
                    "reset_ts": _num(pack.get("nextDateReset")),
                    "expire_ts": None,
                    "unit": "credit",
                })
        if failed:
            items.insert(0, {
                "label": "Kiro 额度查询失败",
                "used": None, "total": None,
                "remaining": f"{len(failed)}/{len(accounts)} 个账号取不到额度"
                             f"（{'、'.join(failed)}）",
                "percent": None, "reset_ts": None,
                "query_failed": True,
            })
        if not items:
            return None
        return {"items": items, "sum_items": True, "level": level}

    def _usage_one(self, account_id: str) -> dict[str, Any]:
        """单账号 ``getUsageLimits``（同步，benefits 层走 to_thread）。"""
        import urllib.request

        token, cred = creds.ensure_account_token(account_id)
        region = str(cred.get("region") or creds.DEFAULT_REGION)
        url = (f"https://codewhisperer.{region}.amazonaws.com/getUsageLimits"
               "?origin=AI_EDITOR&isEmailRequired=false")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": f"aws-sdk-js/1.0.0 Kiro/{KIRO_UA_VERSION}",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if not isinstance(data, dict):
            raise RuntimeError("响应不是 JSON 对象")
        return data

    # -- 认证 ---------------------------------------------------------------

    def ensure_auth(self) -> None:
        if not creds.list_accounts():
            raise HTTPException(status_code=401, detail={"error": {
                "message": ("kiro 未登录：请运行 `buddy login kiro`（浏览器授权），"
                            "或在管理页用「导入 auth 文件」带入 "
                            "~/.aws/sso/cache/kiro-auth-token.json"),
                "type": "authentication_error"}})

    def health(self) -> dict[str, Any]:
        accounts = creds.list_accounts()
        return {
            "id": self.id,
            "name": self.name,
            "accounts": len(accounts),
            "models": [str(m.get("id")) for m in self._models_cache],
            **({"models_error": self._models_error} if self._models_error else {}),
        }

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        want = (model or "").strip()
        if not want:
            return False
        bare = want.removeprefix(f"{self.id}/")
        return any(str(m.get("id") or "") == bare for m in self._models_cache)

    # -- 转发 ---------------------------------------------------------------

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        if protocol == "responses":
            raise HTTPException(status_code=501, detail={"error": {
                "message": ("kiro 不支持 responses 协议（上游没有该端点）。"
                            "请改用 openai（/v1/chat/completions）或 "
                            "anthropic（/v1/messages）"),
                "type": "unsupported_protocol"}})

        accounts = creds.list_accounts()
        if not accounts:
            self.ensure_auth()
        stream = bool(body.get("stream", False))
        payload = _chat_to_kiro(body)
        model_id = str(body.get("model") or "").strip()

        last_exc: Exception | None = None
        for acct in accounts:
            try:
                token, cred = await self._token_for(acct)
                region = str(cred.get("region") or acct.region)
                payload["profileArn"] = cred.get("profile_arn") or None
                if not payload["profileArn"]:
                    payload.pop("profileArn", None)
                resp = await self._post_chat(token, region, payload)
                if resp.status_code == 403 and "subscription does not support" in (
                        await resp.aread()).decode("utf-8", "replace"):
                    # 这个特定 403 = 账号没被订阅覆盖，换号有意义
                    last_exc = RuntimeError(f"{acct.label}: 订阅不支持")
                    await resp.aclose()
                    continue
                if resp.status_code >= 400:
                    body_text = (await resp.aread()).decode("utf-8", "replace")
                    last_exc = RuntimeError(f"HTTP {resp.status_code}: {body_text[:200]}")
                    await resp.aclose()
                    # 429/5xx 换号；其它（400 参数错）换号也没用，直接抛
                    if resp.status_code in (429, 500, 502, 503, 504):
                        continue
                    raise HTTPException(status_code=resp.status_code, detail={"error": {
                        "message": f"kiro 上游错误: {body_text[:300]}",
                        "type": "upstream_error"}})
                return await self._respond(resp, protocol, model_id, stream)
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001 - 单账号失败换下一个
                last_exc = exc
                log.warning("kiro 账号 %s 失败: %s", acct.label, describe_exception(exc))
                continue
        detail = describe_exception(last_exc) if last_exc else "无可用账号"
        raise HTTPException(status_code=502, detail={"error": {
            "message": f"kiro 所有账号均失败：{detail}", "type": "bad_gateway"}})

    async def _token_for(self, acct: KiroAccount) -> tuple[str, dict[str, Any]]:
        import asyncio

        return await asyncio.to_thread(creds.ensure_account_token, acct.id)

    async def _post_chat(self, token: str, region: str,
                         payload: dict[str, Any]) -> httpx.Response:
        client = await self._get_client()
        url = f"{endpoint_base(region)}{_CHAT_PATH}"
        headers = {**auth_headers(token),
                   "x-amz-target": ("AmazonCodeWhispererStreamingService"
                                    ".GenerateAssistantResponse")}
        request = client.build_request("POST", url, json=payload, headers=headers)
        return await client.send(request, stream=True)

    async def _respond(self, resp: httpx.Response, protocol: str,
                       model: str, stream: bool) -> StreamingResponse | JSONResponse:
        """把上游 EventStream 转成客户端要的协议。"""
        request_id = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        message_id = "msg_" + uuid.uuid4().hex[:24]

        async def byte_iter() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk
            finally:
                await resp.aclose()

        # 转换器是**同步**的纯字节解析（写成同步更好读，也便于单测），
        # 但数据源是异步的。不搞线程桥接（那会踩「httpx client 绑在另一个
        # event loop」的坑——实测过一次），而是把两个协议的输出都做成
        # **异步生成器**：内部用 async for 收字节、同步解析、立即 yield 出去。
        if stream:
            gen = (self._stream_anthropic(byte_iter(), model, message_id)
                   if protocol == "anthropic"
                   else self._stream_openai(byte_iter(), model, request_id, created))
            return StreamingResponse(gen, media_type="text/event-stream",
                                     headers=_SSE_HEADERS)

        # 非流式：把事件流聚合完再回一个整体 JSON
        text_parts: list[str] = []
        credit: float | None = None
        async for event in self._events(byte_iter()):
            if event["type"] == "text":
                text_parts.append(event["text"])
            elif event["type"] == "credit":
                credit = event["credit"]
        text = "".join(text_parts)
        if protocol == "anthropic":
            usage = {"input_tokens": 0, "output_tokens": max(1, len(text) // 4)}
            return JSONResponse(content={
                "id": message_id, "type": "message", "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": text}] if text else [],
                "stop_reason": "end_turn", "stop_sequence": None, "usage": usage,
            })
        chat: dict[str, Any] = {
            "id": request_id, "object": "chat.completion", "created": created,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": max(1, len(text) // 4),
                      "total_tokens": max(1, len(text) // 4)},
        }
        if credit is not None:
            # 上游 meteringEvent 给的真实计费：放进扩展字段（metrics 会认）
            chat["credit"] = credit
        return JSONResponse(content=chat)

    # -- 异步桥接：同步解析器 + 异步字节源 ---------------------------------

    async def _events(self, source: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any]]:
        """把异步字节流跑过同步的 EventStream 解析器。

        解析器是纯 CPU 的字节拼接（见 :mod:`.stream`），不需要异步；但字节源
        是异步的。这里按 chunk 喂进一个增量缓冲，解析出一条事件就 yield 一条
        ——不做整轮回填，首字节体验与上游一致。
        """
        import queue

        sentinel = object()
        buf = b""

        async def drain(final: bool) -> AsyncIterator[dict[str, Any]]:
            nonlocal buf
            while True:
                frame = kiro_stream.take_frame(buf, final=final)
                if frame is None:
                    return
                consumed, headers, payload = frame
                buf = buf[consumed:]
                event = kiro_stream.frame_to_event(headers, payload)
                if event is not None:
                    yield event

        async for chunk in source:
            buf += chunk
            async for event in drain(False):
                yield event
        async for event in drain(True):
            yield event

    async def _stream_openai(self, source: AsyncIterator[bytes], model: str,
                             request_id: str, created: int) -> AsyncIterator[str]:
        first = True
        async for event in self._events(source):
            if event["type"] != "text":
                continue
            delta: dict[str, Any] = {"content": event["text"]}
            if first:
                delta["role"] = "assistant"
                first = False
            yield _sse_json({
                "id": request_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            })
        yield _sse_json({
            "id": request_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        })
        yield "data: [DONE]\n\n"

    async def _stream_anthropic(self, source: AsyncIterator[bytes], model: str,
                                message_id: str) -> AsyncIterator[str]:
        def ev(name: str, payload: dict[str, Any]) -> str:
            return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        yield ev("message_start", {"type": "message_start", "message": {
            "id": message_id, "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0},
        }})
        yield ev("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "text", "text": ""}})
        chars = 0
        async for event in self._events(source):
            if event["type"] != "text":
                continue
            chars += len(event["text"])
            yield ev("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": event["text"]}})
        yield ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield ev("message_delta", {"type": "message_delta", "delta": {
            "stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": max(1, chars // 4)}})
        yield ev("message_stop", {"type": "message_stop"})

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client
