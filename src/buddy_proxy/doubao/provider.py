"""豆包（Doubao）provider —— 自研内联实现。

通过内联的 ``doubao.cdp_client.CDPDoubaoClient``（纯 stdlib CDP 直连豆包工作
App 的内置 Chromium，在页面 JS 环境里 fetch 自动注入 a_bogus 风控签名）直连
豆包，不再依赖外部 doubao2api HTTP 服务，也不依赖 Playwright。

本 provider 负责：
1. 持有并管理 CDPDoubaoClient 生命周期（登录态、健康检查）
2. 把 OpenAI 标准 chat 请求转成豆包文本提示词 + use_deep_think 模式
3. 把豆包原始 SSE 事件转回 OpenAI 标准格式（流式 / 非流式）

注意：豆包客户端模型不支持 function calling / tool use，仅适合对话、写作
场景，不能作为 coding agent 后端。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Sequence

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..providers.base import BaseProvider
from ..core.errors import describe_exception
from .ws import WSClosed

log = logging.getLogger(__name__)

# 豆包文本对话模型表。
#
# 两类条目（2026-09 对豆包工作 App 抓包实测，见 cdp_client._build_agent_payload）：
# - 经典管线（``deep_think``）：服务端忽略一切模型字段，固定路由到当前默认
#   豆包模型，仅 ``use_deep_think``（0=快速, 1=思考, 3=专家）有效；
# - agent 管线（``agent``）：与 App 模型菜单同款协议（``model_item_key`` 等），
#   实测可路由到菜单里的具体模型。item_key 来自 App 前端 modelItems 注册表。
_DOUBAO_CHAT_MODELS: dict[str, dict[str, Any]] = {
    # ---- 经典管线 ----
    "doubao": {"desc": "豆包（默认模型快速模式）", "deep_think": 0},
    # 旧别名：历史上并非真的 Pro 模型（经典管线固定默认模型），真 Pro 用
    # ``doubao-2.1-pro``。保留仅为兼容老客户端。
    "doubao-pro": {"desc": "豆包（旧别名，等同 doubao）", "deep_think": 0},
    "doubao-think": {"desc": "豆包深度思考", "deep_think": 1},
    "doubao-expert": {"desc": "豆包专家模式", "deep_think": 3},
    # ---- agent 管线（App 模型菜单同款）----
    "doubao-auto": {
        "desc": "豆包自动（App「自动」智能路由）",
        "agent": {"item_key": "9", "extra": {"total_window_size": "256000"}, "provider": ""},
    },
    "doubao-2.1-turbo": {
        "desc": "豆包 2.1 Turbo（App 同款）",
        "agent": {"item_key": "4", "extra": {"total_window_size": "256000"}, "provider": ""},
    },
    "doubao-2.1-pro": {
        "desc": "豆包 2.1 Pro（App 同款，额度消耗更快）",
        "agent": {"item_key": "5", "extra": {"total_window_size": "256000"}, "provider": ""},
    },
    "orange-5.0": {
        "desc": "Orange 5.0（App 同款，支持极高/最高推理强度）",
        "agent": {"item_key": "6", "extra": {"total_window_size": "256000"}, "provider": ""},
    },
    "gemini-3.7-flash": {
        "desc": "Gemini 3.7 Flash（豆包 App 内提供的第三方模型）",
        "agent": {
            "item_key": "1946880770",
            "extra": {"memory_profile": "256k", "provider_id": "cis", "total_window_size": "256000"},
            "provider": "cis",
        },
    },
    "gpt-5.6-sol": {
        "desc": "GPT-5.6 Sol（豆包 App 内提供的第三方模型）",
        "agent": {
            "item_key": "2123520258",
            "extra": {"memory_profile": "256k", "provider_id": "cis", "total_window_size": "256000"},
            "provider": "cis",
        },
    },
}

# agent 管线默认推理强度（3=低 4=中 5=高 6=极高 7=最高），可被请求体
# ``reasoning_effort`` 字段按模型覆盖
_DEFAULT_REASONING_EFFORT = 5

# 新会话引导消息：纯文本、不触发工具（见 DoubaoProvider.stream_agent_task）
_BOOTSTRAP_TASK = "（会话初始化）请直接回复两个字：就绪"


def _extract_prompt(messages: list[dict[str, Any]]) -> str:
    """从 OpenAI 标准 messages 提取文本提示词。

    豆包没有原生多轮 messages API，多轮时把历史拼成 ``[role]: content``
    文本（与 doubao2api 原实现一致）。
    """
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        else:
            text = ""
        if text:
            # user 消息保持自然语言（不带前缀）；非 user（system 等）必须带
            # role 前缀，否则单条 system 消息会被当成用户正文发给豆包
            if role == "user":
                parts.append(text)
            else:
                parts.append(f"[{role}]: {text}")
    return "\n".join(parts)


class DoubaoProvider(BaseProvider):
    id = "doubao"
    name = "豆包 (自研内联)"
    supports_checkin = True  # 每日签到（App 内置 Chromium 的 CDP 通道，见 checkin.py）

    def __init__(
        self,
        startup_timeout: float = 120.0,
    ):
        from .cdp_client import CDPDoubaoClient

        self._client = CDPDoubaoClient()
        self._startup_timeout = startup_timeout
        self._started = False
        # 防止并发首请求双重启动（双开 Helper / 重复连接 CDP）
        self._start_lock = asyncio.Lock()
        # 防止并发请求在断线后各自「重连 + 重试」（同一请求最多重试一次）
        self._reconnect_lock = asyncio.Lock()
        # /api/agent/doubao 任务接口的默认会话（缺省续聊最近一次会话）
        self._agent_session: str | None = None
        # 最近一次签到时的服务端登录态：False=明确未登录，None=没探到/没探过
        self._login_state: bool | None = None

    # ------------------------------------------------------------------
    # BaseProvider 接口
    # ------------------------------------------------------------------

    def models(self) -> Sequence[dict[str, Any]]:
        return [
            {
                "id": mid,
                "object": "model",
                "owned_by": self.id,
                "created": 0,
                "description": spec["desc"],
            }
            for mid, spec in _DOUBAO_CHAT_MODELS.items()
        ]

    def ensure_auth(self) -> None:
        """豆包认证是异步的（CDP 浏览器登录态），无法在同步方法里可靠
        检查。这里只做软检查（no-op），真正的启动 + 登录态检查由 ``forward``
        里的 ``_ensure_started`` 完成，未登录时由 ``forward`` 抛 401。
        """
        return

    async def _ensure_started(self) -> None:
        """异步确保 CDPDoubaoClient 已启动（惰性启动，加锁防并发双重启动）。

        ``_started`` 只在**启动过程**成功过就置 True，之后不复位——所以还必须
        看 ``client.is_ready``：认证/聊天页可以单独回滚（App 刷新、切换账号），
        那时 ``_started=True`` 但 ``is_ready=False``，只认前者会把通道永久判成
        「未登录」。
        """
        if self._is_connected():
            return
        async with self._start_lock:
            if self._is_connected():  # 等锁期间可能已被启动
                return
            if self._started:
                # 启动过一次但已失效：复用过的 CDP 会话/页面没了，重连
                await self._reconnect()
                return
            log.info("DoubaoProvider: starting CDP client")
            try:
                await self._client.start()
            except Exception as exc:
                log.error("DoubaoProvider: client start failed: %s", exc)
                raise HTTPException(
                    status_code=502,
                    detail=f"doubao client start failed: {exc}",
                ) from exc
            self._started = True

    def _is_connected(self) -> bool:
        return self._started and bool(self._client.is_ready)

    async def _reconnect(self) -> bool:
        """CDP 断线后重建连接；成功返回 True（并发请求只重连一次）。

        网关只在启动时连一次 CDP。豆包 App 刷新页面 / 被关掉再打开后，旧
        WebSocket 与 target 全部失效，而 ``is_ready`` 只在 ``start()`` 里置位，
        连接断了没人复位。于是每次请求都撞在死连接上——用户看到的就是
        「一直报 CDP connection lost，重开 App 也没用，只能重启网关」。

        ``restart()`` 会先 :meth:`_drop_connection`（关旧 socket、复位 ready）
        再 ``start()``：端口上已有新 App 的调试口就复用，没有就由 App 身份拉起。
        """
        async with self._reconnect_lock:
            if self._is_connected():
                return True  # 排队期间别的请求已经重连好了
            action = "restart" if self._started else "start"
            log.info("DoubaoProvider: %s CDP client", action)
            try:
                if self._started:
                    await self._client.restart()
                else:
                    await self._client.start()
            except Exception as exc:
                log.error("DoubaoProvider: CDP %s failed: %s", action,
                          describe_exception(exc))
                return False
            self._started = True
            return bool(self._client.is_ready)

    async def _with_reconnect(self) -> None:
        """请求入口用：先确保已连接，仍未就绪才抛 401。

        重连只在这里做一次：``_ensure_started`` 内部已经把「启动过但失效」
        的客户端 ``restart`` 过了，不再叠一次，免得白白关闭刚拉起来的 App。
        """
        await self._ensure_started()
        if not self._client.is_ready:
            raise HTTPException(
                status_code=401,
                detail="doubao not logged in - 请先完成豆包扫码登录",
            )

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        await self._with_reconnect()

        requested_model = body.get("model", "doubao")
        if requested_model not in _DOUBAO_CHAT_MODELS:
            raise HTTPException(
                status_code=400,
                detail=f"unknown doubao model '{requested_model}'",
            )
        spec = _DOUBAO_CHAT_MODELS[requested_model]
        model_spec: dict[str, Any] | None = None
        if "agent" in spec:
            # agent 管线：复制一份避免请求间共享可变默认值，
            # reasoning_effort 允许客户端按请求覆盖（3低/4中/5高/6极高/7最高）
            model_spec = dict(spec["agent"])
            effort = body.get("reasoning_effort")
            if isinstance(effort, int) and 3 <= effort <= 7:
                model_spec["reasoning_effort"] = effort
            else:
                model_spec.setdefault("reasoning_effort", _DEFAULT_REASONING_EFFORT)
        use_deep_think = spec.get("deep_think", 0)

        messages = body.get("messages", [])
        prompt = _extract_prompt(messages)
        if not prompt:
            raise HTTPException(status_code=400, detail="no text content")

        conversation_id = body.get("conversation_id")
        bot_id = body.get("bot_id")

        if body.get("stream"):
            include_usage = bool(
                (body.get("stream_options") or {}).get("include_usage"))
            gen = self._stream(prompt, use_deep_think, requested_model,
                               conversation_id, bot_id, include_usage,
                               model_spec=model_spec, retry_on_closed=True)
            if protocol == "anthropic":
                # Claude Code 等 /v1/messages 客户端：OpenAI chunk 流包一层
                # 转回 anthropic 事件序列（trae/mimo/dumate 同款）。
                gen = _to_anthropic_stream(gen, requested_model)
            return StreamingResponse(
                gen,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "close"},
            )

        message = await self._collect(prompt, use_deep_think, conversation_id,
                                      bot_id, model_spec=model_spec)
        resp_payload = self._build_nonstream_response(message, requested_model)
        if protocol == "anthropic":
            from ..protocols.anthropic_adapter import chat_completion_to_anthropic_message

            resp_payload = chat_completion_to_anthropic_message(resp_payload, original)
        return JSONResponse(content=resp_payload)

    def health(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "started": self._started,
            "ready": self._client.is_ready,
        }

    # ------------------------------------------------------------------
    # 转换逻辑
    # ------------------------------------------------------------------

    def _build_nonstream_response(
        self, message: dict[str, Any], model: str
    ) -> dict[str, Any]:
        content = message.get("content", "")
        reasoning = message.get("reasoning_content", "")
        resp: dict[str, Any] = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }
        if reasoning:
            resp["choices"][0]["message"]["reasoning_content"] = reasoning
        if message.get("conversation_id"):
            resp["conversation_id"] = message["conversation_id"]
        return resp

    async def _stream(
        self,
        prompt: str,
        use_deep_think: int,
        model: str,
        conversation_id: str | None,
        bot_id: str | None,
        include_usage: bool = False,
        model_spec: dict[str, Any] | None = None,
        retry_on_closed: bool = False,
    ) -> AsyncIterator[str]:
        """把豆包原始 SSE 事件转成 OpenAI chat.completion.chunk 流。

        ``retry_on_closed``：还没向客户端吐出任何内容时遇到 ``WSClosed``，
        先重连 CDP 再重跑一次（豆包 App 刷新后旧连接必然失效，不重连的话
        每次都要等用户重启网关）。已经吐过内容就只能如实报错，不能拼接。
        """
        request_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        thinking_count = 0
        in_thinking = False
        result_conversation_id = conversation_id

        def _chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
            return {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }

        def _iter_blocks(data: dict[str, Any]):
            for patch in data.get("patch_op", []):
                pv = patch.get("patch_value", {})
                yield from pv.get("content_block", [])
            dc = data.get("content", {})
            if isinstance(dc, dict):
                yield from dc.get("content_block", [])

        emitted = False  # 是否已向客户端吐出内容（决定断线后能否安全重试）
        attempt = 0
        while True:
            attempt += 1
            try:
                async for event in self._client.chat_completion(
                    prompt,
                    use_deep_think=use_deep_think,
                    conversation_id=conversation_id or None,
                    bot_id=bot_id or None,
                    model_spec=model_spec,
                ):
                    emitted = True
                    if event.get("error"):
                        status = event.get("status", 0)
                        # 上游错误 body 可能回显请求/账号信息，只记录状态码。
                        log.error("doubao stream error status=%s", status)
                        self._client.record_failure(status or 0)
                        yield f"data: {json.dumps(_chunk({'content': f'[Error {status}]'}), ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    event_type = event.get("_event", "")

                    if not result_conversation_id:
                        cid = self._client.extract_conversation_id(event)
                        if cid and cid != "0":
                            result_conversation_id = cid

                    # 风控 / 会话过期错误
                    if event_type == "STREAM_ERROR" or event.get("error_code"):
                        code = event.get("error_code", 0)
                        msg = event.get("error_msg", "unknown error")
                        self._client.record_failure(code)
                        yield f"data: {json.dumps(_chunk({'content': f'[Error code={code}: {msg}]'}), ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    # CHUNK_DELTA：紧凑 {"text": "..."}
                    if (
                        event_type == "CHUNK_DELTA"
                        and isinstance(event.get("text"), str)
                        and event["text"]
                    ):
                        t = event["text"]
                        if in_thinking:
                            yield f"data: {json.dumps(_chunk({'reasoning_content': t}), ensure_ascii=False)}\n\n"
                        else:
                            yield f"data: {json.dumps(_chunk({'role': 'assistant', 'content': t}), ensure_ascii=False)}\n\n"
                        continue

                    # content_block 数组（thinking 标记 / 正文）
                    for cb in _iter_blocks(event):
                        bt = cb.get("block_type", 0)
                        block_content = cb.get("content", {})

                        if bt == 10040:
                            thinking_count += 1
                            in_thinking = thinking_count == 1
                            continue

                        if bt == 10000:
                            tb = block_content.get("text_block", {})
                            if isinstance(tb, dict) and tb.get("text"):
                                t = tb["text"]
                                if in_thinking:
                                    yield f"data: {json.dumps(_chunk({'reasoning_content': t}), ensure_ascii=False)}\n\n"
                                else:
                                    yield f"data: {json.dumps(_chunk({'role': 'assistant', 'content': t}), ensure_ascii=False)}\n\n"

            except WSClosed as exc:
                # 豆包 App 刷新/重开：旧连接必然失效。先重连再重试一次，
                # 而不是把「连接断了」原样甩给用户（那样只能重启网关才好）。
                if retry_on_closed and not emitted and attempt == 1:
                    log.warning("doubao stream: CDP 断线（%s），重连后重试一次", exc)
                    if await self._reconnect():
                        continue
                detail = describe_exception(exc)
                log.error("doubao stream error: %s", detail)
                self._client.record_failure(0)
                yield f"data: {json.dumps(_chunk({'content': f'[Error: {detail}]'}), ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            except Exception as exc:
                # describe_exception：异常自身 str() 为空时（httpcore 映射的
                # httpx.ReadError 一类）仍能给出可读真因，日志与给客户端的文本都用它
                detail = describe_exception(exc)
                log.error("doubao stream error: %s", detail)
                self._client.record_failure(0)
                yield f"data: {json.dumps(_chunk({'content': f'[Error: {detail}]'}), ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return
            break

        self._client.record_success()

        final_delta: dict[str, Any] = {}
        if result_conversation_id:
            final_delta["conversation_id"] = result_conversation_id
        yield f"data: {json.dumps(_chunk(final_delta, 'stop'), ensure_ascii=False)}\n\n"
        if include_usage:
            # OpenAI 流式协议：stream_options.include_usage 时补一个 usage-only
            # 收尾 chunk（豆包 CDP 通道无 token 统计，返回零值），下游客户端靠
            # 它判定流的 is_final，缺失会导致 tool_calls 丢失
            payload = {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [],
                "usage": {
                    "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                },
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    async def _collect(
        self,
        prompt: str,
        use_deep_think: int,
        conversation_id: str | None,
        bot_id: str | None,
        model_spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """非流式：聚合完整响应，分离 reasoning_content 与 content。"""
        thinking_count = 0
        in_thinking = False
        thinking_parts: list[str] = []
        content_parts: list[str] = []
        result_conversation_id = conversation_id

        def _iter_blocks(data: dict[str, Any]):
            for patch in data.get("patch_op", []):
                pv = patch.get("patch_value", {})
                yield from pv.get("content_block", [])
            dc = data.get("content", {})
            if isinstance(dc, dict):
                yield from dc.get("content_block", [])

        attempt = 0
        while True:
            attempt += 1
            try:
                cid = await self._collect_once(
                    prompt, use_deep_think, conversation_id, bot_id, model_spec,
                    content_parts, thinking_parts,
                )
                if cid:
                    result_conversation_id = cid
            except WSClosed as exc:
                # 非流式还没向客户端吐过任何字节，重连后重跑是安全的。
                if attempt == 1 and await self._reconnect():
                    log.warning("doubao: CDP 断线（%s），重连后重试一次", exc)
                    content_parts.clear()
                    thinking_parts.clear()
                    # 新连接是新的会话上下文：断线前半截拿到的会话 id 别再沿用，
                    # 否则续聊会挂到一个没跑完的会话上（重跑没拿回 id 就不带）。
                    result_conversation_id = conversation_id
                    continue
                # 转成**不可换档**的 409：客户端看到的是豆包这一条失败，网关与
                # 其它渠道不受影响。
                #
                # 为什么不是 502：`_RETRYABLE_STATUS` 含 502，`_forward_with_order`
                # 会据此 `cooldown_mod.mark_failed(provider_id, model, status=502)`——
                # 普通档冷却 5 分钟、窗口内三次升到 1 小时；且 ≥500 还会进机器级突发
                # 窗口，跟另一通道凑两条就在同一模型上判「本机网络故障」。可 App 被关掉
                # 不是模型坏了，用户重开 App 后模型仍在冷却里，反而像个新故障。409 不在
                # 可换档集合内，换档逻辑原样上抛、不打标记（豆包没有同模型替代通道）。
                self._client.record_failure(0)
                log.error("doubao CDP connection lost: %s", exc)
                raise HTTPException(
                    status_code=409,
                    detail="doubao CDP connection lost (App closed or reloaded); "
                           "reopen Doubao desktop app and retry",
                ) from exc
            break

        self._client.record_success()

        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
        if thinking_parts:
            message["reasoning_content"] = "".join(thinking_parts)
        if result_conversation_id:
            message["conversation_id"] = result_conversation_id
        return message

    async def _collect_once(
        self,
        prompt: str,
        use_deep_think: int,
        conversation_id: str | None,
        bot_id: str | None,
        model_spec: dict[str, Any] | None,
        content_parts: list[str],
        thinking_parts: list[str],
    ) -> str | None:
        """跑一遍非流式聚合，把正文/思考片段追加到调用方给的列表里。

        ``WSClosed`` 原样上抛，由 :meth:`_collect` 决定「重连重试」还是
        转成渠道级 409（见那里的注释：为什么是 409 而不是 502）。
        """
        thinking_count = 0
        in_thinking = False
        result_cid = conversation_id or None

        def _iter_blocks(data: dict[str, Any]):
            for patch in data.get("patch_op", []):
                pv = patch.get("patch_value", {})
                yield from pv.get("content_block", [])
            dc = data.get("content", {})
            if isinstance(dc, dict):
                yield from dc.get("content_block", [])

        async for event in self._client.chat_completion(
            prompt,
            use_deep_think=use_deep_think,
            conversation_id=conversation_id or None,
            bot_id=bot_id or None,
            model_spec=model_spec,
        ):
            if event.get("error"):
                self._client.record_failure(event.get("status") or 0)
                raise RuntimeError(
                    f"API error {event.get('status')}: {event.get('body', '')[:200]}"
                )
            if event.get("error_code"):
                code = event.get("error_code", 0)
                msg = event.get("error_msg", "")
                self._client.record_failure(code)
                raise RuntimeError(f"Error code={code}: {msg}")

            if not result_cid:
                got = self._client.extract_conversation_id(event)
                if got and got != "0":
                    result_cid = got

            event_type = event.get("_event", "")

            if (
                event_type == "CHUNK_DELTA"
                and isinstance(event.get("text"), str)
                and event["text"]
            ):
                if in_thinking:
                    thinking_parts.append(event["text"])
                else:
                    content_parts.append(event["text"])
                continue

            for cb in _iter_blocks(event):
                bt = cb.get("block_type", 0)
                block_content = cb.get("content", {})

                if bt == 10040:
                    thinking_count += 1
                    in_thinking = thinking_count == 1
                elif bt == 10000:
                    tb = block_content.get("text_block", {})
                    if isinstance(tb, dict) and tb.get("text"):
                        if in_thinking:
                            thinking_parts.append(tb["text"])
                        else:
                            content_parts.append(tb["text"])

        return result_cid

    # ------------------------------------------------------------------
    # 签到（/ui 自动打卡循环消费；同步实现，调用方经 asyncio.to_thread 包装）
    # ------------------------------------------------------------------

    def _run_checkin(self, fn: Any) -> Any:
        """在独立事件循环里把 CDP 客户端拉到就绪，再执行同步的签到逻辑。

        ``checkin_status``/``checkin_claim`` 是被 ``benefits`` 用 to_thread
        调用的**同步**方法，而 CDP 客户端的启动/登录等待是异步的——这里用
        ``asyncio.run`` 起一个一次性循环（线程里没有运行中的循环，不会冲突；
        签到查询本身全程同步，不会和代理主循环抢事件循环）。
        """
        client = self._client
        # 服务端登录态三态（None=没探到）；只有明确未登录才在提示里说「未登录」。
        self._login_state: bool | None = None

        async def _ready() -> None:
            await self._ensure_started()
            # 每次签到都实时问服务端，不吃任何登录缓存：面板卡在「状态未知」
            # 时用户最想知道的正是「到底登没登」，缓存的 true 会让错误归因跑偏。
            probe = getattr(client, "login_state_sync", None)
            if callable(probe):
                self._login_state = await asyncio.to_thread(probe)  # 同步求值，别嵌套 asyncio.run
            else:
                self._login_state = await client.wait_for_login(timeout=15)

        try:
            asyncio.run(_ready())
        except HTTPException:
            return None
        except Exception as exc:  # noqa: BLE001 - 启动失败按「查询失败」处理
            log.debug("doubao checkin: client not ready: %s", type(exc).__name__)
            return None
        if not client.is_ready or self._login_state is False:
            return None
        return fn(client)

    def checkin_status(self) -> dict[str, Any] | None:
        """今日签到状态（CDP 内 fetch，见 ``doubao/checkin.py`` 的接口说明）。

        查不到（App 未登录 / 候选端点都没给出可解析响应）时**不返回裸
        ``None``**：这样面板会画成「状态未知」，不会退化成可点的「未签到」。
        """
        from . import checkin as doubao_checkin

        def _query(client: Any) -> dict[str, Any] | None:
            status, _path = doubao_checkin.fetch_checkin_status(client)
            return status

        status = self._run_checkin(_query)
        if isinstance(status, dict):
            return status
        return {
            "checked_in": False,
            "claimable": False,
            "inactive": False,
            "query_failed": True,
            "message": self._checkin_failure_message(),
        }

    def _checkin_failure_message(self) -> str:
        """签到查不到时的提示：区分「没登录」和「接口没认出」，别让用户瞎猜。"""
        if self._login_state is False:
            return "豆包未登录：请在豆包 App 里重新扫码登录后重试"
        if self._login_state is True:
            return "豆包签到接口未能识别（已登录，详见代理日志 doubao checkin）"
        return "豆包登录态未确认（探针未下结论），详见代理日志 doubao checkin"

    def checkin_claim(self) -> dict[str, Any] | None:
        from . import checkin as doubao_checkin

        def _claim(client: Any) -> dict[str, Any] | None:
            status, path = doubao_checkin.fetch_checkin_status(client)
            if status is None:
                raise RuntimeError("豆包签到状态查询失败，未发起领取")
            if not status.get("claimable"):
                if status.get("checked_in"):
                    return {**status, "message": "今日已签到（上游记录）"}
                raise RuntimeError("豆包今日暂无可领签到")
            claimed = doubao_checkin.claim_checkin(client, path)
            if claimed is None:
                raise RuntimeError("豆包签到领取失败（上游未确认）")
            return {**claimed, "message": "签到成功"}

        return self._run_checkin(_claim)

    # ------------------------------------------------------------------
    # 登录辅助（供外部触发扫码）
    # ------------------------------------------------------------------

    async def wait_for_login(self, timeout: float = 120.0) -> bool:
        """等待用户扫码登录（供管理端/CLI 调用）。"""
        await self._ensure_started()
        return await self._client.wait_for_login(timeout=timeout)

    # ------------------------------------------------------------------
    # 一次性 agent 任务接口（POST /api/agent/doubao）
    # ------------------------------------------------------------------

    async def ensure_ready(self) -> None:
        """惰性启动 + 就绪检查；失败抛 HTTPException（消息含可操作提示）。"""
        await self._ensure_started()
        if not self._client.is_ready:
            raise HTTPException(
                status_code=401,
                detail="doubao not logged in - 请先完成豆包扫码登录",
            )

    def resolve_agent_model(self, model: str | None) -> tuple[str, dict[str, Any]]:
        """校验任务接口的模型（只走 agent 管线），返回 (model, model_spec)。"""
        name = str(model or "doubao-auto").strip()
        spec = _DOUBAO_CHAT_MODELS.get(name)
        if spec is None or "agent" not in spec:
            raise HTTPException(
                status_code=400,
                detail="model '%s' 不是豆包 agent 管线模型，可选: %s" % (
                    name,
                    ", ".join(m for m, s in _DOUBAO_CHAT_MODELS.items() if "agent" in s),
                ),
            )
        model_spec = dict(spec["agent"])
        model_spec.setdefault("reasoning_effort", _DEFAULT_REASONING_EFFORT)
        return name, model_spec

    async def stream_agent_task(
        self,
        task: str,
        session_id: str | None,
        model: str,
        model_spec: dict[str, Any],
    ) -> AsyncIterator[str]:
        """一次性 agent 任务：任务文本发给豆包工作（agent 管线，不自定义
        system prompt，直接用豆包自己的工具），流式回传简易 SSE 事件。

        会话语义：session_id 显式指定时续聊该会话（"new"/空串 = 强制新建）；
        缺省续聊进程内默认会话（即最近一次任务用的会话，首次自动新建）。

        新会话引导（2026-09-09 实测）：runtime_type=2 的新会话要等 App 侧
        完成运行时握手（「工作环境准备中」），裸 fetch 不会做这一步，首条
        消息里一旦触发工具调用就会一直报错重试直到流卡死。因此新建会话时
        先发一条纯文本引导消息把会话建起来（纯文本回复不需要工具），拿到
        conversation_id 后以续聊（runtime_type=1）发正式任务，工具即可正常
        执行；引导消息的回复不透出给客户端。

        事件协议（每行 ``data: <JSON>``）：
        - ``{"type":"start","model":...,"session_id":...}``  首事件（session_id 为本次续聊的会话，新建时 null）
        - ``{"type":"session","session_id":"..."}``          从上游拿到 conversation_id 后发一次
        - ``{"type":"delta","text":"..."}``                  正文增量
        - ``{"type":"reasoning","text":"..."}``              思考链增量（可能不出现）
        - ``{"type":"error","message":"..."}``               出错（流终止）
        - ``{"type":"done","session_id":"..."}``             结束；session_id 供客户端下次续聊
        """
        def _evt(payload: dict[str, Any]) -> str:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        conv = self._agent_session
        if session_id is not None:
            s = str(session_id).strip()
            conv = None if s.lower() in ("", "new") else s
        result_conv = conv
        yield _evt({"type": "start", "model": model, "session_id": conv})

        if conv is None:
            # 新会话引导：见 docstring。引导失败则整个任务失败。
            boot_conv = ""
            async for event in self._client.chat_completion(
                _BOOTSTRAP_TASK, conversation_id=None, model_spec=model_spec,
            ):
                if event.get("error"):
                    status = event.get("status", 0)
                    log.error("doubao agent bootstrap upstream error status=%s", status)
                    self._client.record_failure(status or 0)
                    yield _evt({"type": "error", "message": f"上游 HTTP {status}"})
                    return
                if event.get("_event") == "STREAM_ERROR" or event.get("error_code"):
                    code = event.get("error_code", 0)
                    msg = event.get("error_msg", "unknown error")
                    self._client.record_failure(code)
                    yield _evt({"type": "error", "message": f"code={code}: {msg}"})
                    return
                if not boot_conv:
                    cid = self._client.extract_conversation_id(event)
                    if cid and cid != "0":
                        boot_conv = cid
            if not boot_conv:
                yield _evt({"type": "error", "message": "会话引导失败：未拿到 conversation_id"})
                return
            conv = boot_conv
            result_conv = conv
            yield _evt({"type": "session", "session_id": conv})

        thinking_count = 0
        in_thinking = False

        def _iter_blocks(data: dict[str, Any]):
            for patch in data.get("patch_op", []):
                pv = patch.get("patch_value", {})
                yield from pv.get("content_block", [])
            dc = data.get("content", {})
            if isinstance(dc, dict):
                yield from dc.get("content_block", [])

        try:
            async for event in self._client.chat_completion(
                task,
                conversation_id=conv,
                model_spec=model_spec,
            ):
                if event.get("error"):
                    status = event.get("status", 0)
                    log.error("doubao agent task upstream error status=%s", status)
                    self._client.record_failure(status or 0)
                    yield _evt({"type": "error", "message": f"上游 HTTP {status}"})
                    return

                if not result_conv:
                    cid = self._client.extract_conversation_id(event)
                    if cid and cid != "0":
                        result_conv = cid
                        yield _evt({"type": "session", "session_id": cid})

                event_type = event.get("_event", "")
                if event_type == "STREAM_ERROR" or event.get("error_code"):
                    code = event.get("error_code", 0)
                    msg = event.get("error_msg", "unknown error")
                    self._client.record_failure(code)
                    yield _evt({"type": "error", "message": f"code={code}: {msg}"})
                    return

                if (
                    event_type == "CHUNK_DELTA"
                    and isinstance(event.get("text"), str)
                    and event["text"]
                ):
                    yield _evt({"type": "reasoning" if in_thinking else "delta",
                                "text": event["text"]})
                    continue

                for cb in _iter_blocks(event):
                    bt = cb.get("block_type", 0)
                    if bt == 10040:
                        thinking_count += 1
                        in_thinking = thinking_count == 1
                    elif bt == 10000:
                        tb = cb.get("content", {}).get("text_block", {})
                        if isinstance(tb, dict) and tb.get("text"):
                            yield _evt({"type": "reasoning" if in_thinking else "delta",
                                        "text": tb["text"]})
        except Exception as exc:
            detail = describe_exception(exc)
            log.error("doubao agent task error: %s", detail)
            self._client.record_failure(0)
            yield _evt({"type": "error", "message": detail})
            return

        self._client.record_success()
        if result_conv:
            # 默认会话跟随最近一次任务用的会话，下次缺省 session_id 时续聊
            self._agent_session = result_conv
        yield _evt({"type": "done", "session_id": result_conv})

    @property
    def page_url(self) -> str:
        page = getattr(self._client, "page", None)
        return page.url if page else ""


async def _to_anthropic_stream(
    gen: AsyncIterator[str], model: str,
) -> AsyncIterator[str]:
    """豆包 OpenAI chunk 流 → Anthropic 事件流（/v1/messages 客户端形状）。

    复用 ``anthropic_adapter.AnthropicStreamConverter``（dumate/kimi/mimo 同款）。
    豆包上游 error 事件已转成 ``choices[0].delta.content = "[Error N]"`` chunk，
    转换器会当普通文本发出去——保持「能拿到内容比凭空断流好」的口径。
    """
    from ..protocols.anthropic_adapter import AnthropicStreamConverter

    converter = AnthropicStreamConverter(model)

    def _emit(event_name: str, payload: dict[str, Any]) -> str:
        return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _close_open() -> list[str]:
        try:
            return [_emit(n, p) for n, p in converter.close_open_blocks()]
        except Exception:  # noqa: BLE001
            return []

    def _abort(msg: str) -> list[str]:
        out = _close_open()
        out.append(_emit("error", {
            "type": "error",
            "error": {"type": "api_error", "message": msg},
        }))
        return out

    try:
        async for sse in gen:
            for line in sse.splitlines():
                line = line.strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                for name, payload in converter.feed_chunk(chunk):
                    yield _emit(name, payload)
        for name, payload in converter.finish():
            yield _emit(name, payload)
    except Exception as exc:  # noqa: BLE001 — 上游异常不该让客户端收到半截流
        log.warning("doubao anthropic stream aborted: %s: %s", type(exc).__name__, exc)
        for event in _abort(f"{type(exc).__name__}: {exc}"):
            yield event
        return
    yield "data: [DONE]\n\n"
