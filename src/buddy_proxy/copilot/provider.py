"""GitHub Copilot 通道：把本地已登录的 Copilot 订阅模型代理成三种协议的 API。

**三条上游端点**（由目录 ``supported_endpoints`` 决定，见 ``catalog.py``）乘
**三种客户端协议**，出口矩阵全在这里：``/chat/completions``（chat）、
``/responses``（responses）、``/v1/messages``（anthropic）。

设计要点：

1. **上游恒以 stream 请求**。chat 端点不开 ``stream_options.include_usage``
   就一路回 ``usage: null``（实测），而 ``usage`` 是我们唯一能拿到积分
   （``copilot_usage.total_nano_aiu``）的地方；responses 非流式则会憋完整条
   推理才返回。所以非流式客户端也在本地聚合，两条路径行为一致。
2. **openai 出口永远走 chat chunk 管线**（即使是 responses 原生上游）：上游
   的 ``copilot_usage`` 只有经 :func:`~.upstream.normalize_billing_usage` 才能
   变成 metrics 认的 ``usage.credit``，直通会把积分丢掉。
3. **三个出口都把积分带上**：``usage.credit``（openai）/ ``message.usage.credit``
   （anthropic）/ ``response.usage.credit``（responses）。responses 出口复用
   ``ResponsesStreamConverter``，它把上游 ``usage`` 整个拷进 ``response.completed``
   的原样也顺带把 ``credit`` 带出去。
4. **首事件闸门**在向客户端吐第一个字节前完成：401/403/429/模型拒绝都还能换号，
   这正是「200 空流」的根源所在（其它通道踩过）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import time
from dataclasses import replace
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.account_store import quota_epoch_from
from ..core.errors import describe_exception
from ..core.metrics_protocol import ACCOUNT_META
from ..providers.base import BaseProvider
from . import failover
from .catalog import ModelEntry, ModelIndex, load_fallback_catalog, parse_catalog
from .credentials import AuthError, ensure_account_token, list_accounts
from .schema import sanitize_anthropic_tools, sanitize_chat_tools
from .upstream import (
    API_BASE,
    PATH_CHAT,
    anthropic_headers,
    body_has_image,
    classify_initiator,
    extract_error,
    fetch_models,
    fetch_user_usage,
    normalize_billing_usage,
    parse_user_usage,
    plan_label,
    upstream_headers,
)

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=15.0)

#: 非流式聚合的总时长上限（上游是流式的，断流时不能无限等）。
_AGGREGATE_DEADLINE_S = 600.0

#: 单条请求最多换几轮账号；两个账号互相冷却时别把预算烧光。
_MAX_ATTEMPTS = 8

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "close"}

#: 真正可能表示失败的事件名（``response.incomplete`` 是正常截断，不算）。
_ERROR_EVENT_NAMES = frozenset({"error", "response.failed"})

#: 额度面板：多账号在同一轮里并发查询 + 单轮总时长上限。
_QUOTA_ROUND_DEADLINE_S = 8.0
_QUOTA_WORKERS = 4
_quota_pool: concurrent.futures.ThreadPoolExecutor | None = None


def _quota_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _quota_pool
    if _quota_pool is None:
        _quota_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=_QUOTA_WORKERS, thread_name_prefix="copilot-quota")
    return _quota_pool


def _lb_state():
    """当前运行态（负载均衡开关读它）。未初始化时返回 None。

    走 ``core.state`` 的模块级属性而不是导入期绑定：测试普遍 monkeypatch
    ``state.proxy_state``，绑定早了会拿到旧对象。
    """
    from ..core import state as _state

    return getattr(_state, "proxy_state", None)


class CopilotProvider(BaseProvider):
    """GitHub Copilot（用本机已登录的 GitHub 账号 + 订阅）。"""

    id = "copilot"
    name = "GitHub Copilot（本地登录订阅）"

    #: 裸名撞车时让位：``gpt-4o`` / ``gpt-4.1`` 这类公共 ID 在 codex、azure 系
    #: 通道里也有，那些通道是用户**显式配置**的，应当优先认领。本通道自己的
    #: 对外名 ``copilot/<id>`` 不受影响。
    route_priority = 40

    def __init__(self) -> None:
        self._index = ModelIndex()
        # 随包静态目录先顶上：没有网络/还没登录时 ``/v1/models`` 也有东西可看，
        # 冷启动后的第一条请求不用等一次目录刷新。
        self._index.replace(load_fallback_catalog(), source="fallback")
        self._client: httpx.AsyncClient | None = None
        self._refresh_lock: asyncio.Lock | None = None
        self._refresh_loop: asyncio.AbstractEventLoop | None = None
        #: 目录刷新失败的原因（health 里如实展示，别让人以为「没模型」）。
        self._refresh_error = ""

    # ------------------------------------------------------------------
    # 模型
    # ------------------------------------------------------------------

    def models(self) -> Sequence[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for entry in self._index.entries():
            out.append({
                "id": f"{self.id}/{entry.id}",
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "name": entry.name,
                "vendor": entry.vendor,
                "description": f"Copilot · {entry.vendor or 'unknown'}"
                               f"{' · preview' if entry.preview else ''}",
                "preview": entry.preview,
                "endpoint": entry.upstream,
                "context": entry.context,
                "max_input": entry.context,
                "max_output": entry.output,
                "tool_call": not entry.supports or "tool_calls" in entry.supports,
                "vision": "vision" in entry.supports,
                "reasoning": bool(entry.reasoning_effort or "reasoning" in entry.supports),
                "reasoning_effort": list(entry.reasoning_effort),
                "streaming": True,
            })
        return out

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        """只认带前缀的对外名，或目录里确实存在的裸 ID。

        裸 ID 也认是为了「客户端直接写 ``gpt-5.6-terra``」能用，但见
        ``route_priority``：撞车时别的通道先认领。
        """
        return self._index.resolve(model) is not None

    def _entry_for(self, model: str) -> ModelEntry:
        entry = self._index.resolve(model)
        if entry is None:
            known = ", ".join(f"{self.id}/{m}" for m in self._index.ids()) or "（目录为空）"
            raise HTTPException(
                status_code=400,
                detail={"error": {
                    "message": f"copilot 未知模型: {model}；可用: {known}",
                    "type": "invalid_request_error",
                }},
            )
        return entry

    async def refresh_models(self, force: bool = False) -> int:
        """从 ``/models`` 拉最新目录（多账号取并集）；返回模型数。

        - TTL 内且非 ``force`` 直接返回，避免每条请求都打一次 GitHub；
        - 用 ``available_accounts()``（**不**按模型过滤）逐号尝试：目录发现与
          「某个模型被拒」是两件事，模型级冷却不该让目录刷不到新模型；
        - 单账号失败只收缩结果，**全失败不动旧表**——把能用的旧目录换成空表
          只会让所有请求 400「未知模型」。
        """
        if not force and not self._index.stale():
            return len(self._index)
        lock = self._lock()
        async with lock:
            # 等锁期间别的协程可能已经刷过了，再判一次。
            if not force and not self._index.stale():
                return len(self._index)
            accounts = failover.available_accounts()
            if not accounts:
                self._refresh_error = "没有可用的 copilot 账号"
                return len(self._index)
            merged: dict[str, ModelEntry] = {}
            errors: list[str] = []
            for acct in accounts:
                try:
                    token, _cred = await asyncio.to_thread(ensure_account_token, acct.id)
                    payload = await asyncio.to_thread(fetch_models, token)
                except Exception as exc:  # noqa: BLE001 - 单号失败不影响其它号
                    errors.append(f"{acct.id}: {exc}")
                    log.warning("copilot 目录拉取失败（%s）: %s", acct.id, exc)
                    continue
                for entry in parse_catalog(payload):
                    merged.setdefault(entry.id, entry)
            if not merged:
                self._refresh_error = "；".join(errors) or "上游返回空目录"
                log.warning("copilot 目录刷新全部失败: %s", self._refresh_error)
                return len(self._index)
            self._index.replace(list(merged.values()), source="upstream")
            self._refresh_error = "；".join(errors)
            log.info("copilot 目录已刷新：%d 个模型（%d 个账号，失败 %d）",
                     len(self._index), len(accounts), len(errors))
            return len(self._index)

    def _lock(self) -> asyncio.Lock:
        """惰性绑定当前事件循环的锁（``asyncio.Lock`` 不能跨 loop 复用）。"""
        loop = asyncio.get_running_loop()
        if self._refresh_lock is None or self._refresh_loop is not loop:
            self._refresh_lock = asyncio.Lock()
            self._refresh_loop = loop
        return self._refresh_lock

    async def ensure_models(self) -> None:
        """转发前确保目录不过期（失败不拦：旧表还能用，路由层会报准确错误）。"""
        try:
            await self.refresh_models()
        except Exception as exc:  # noqa: BLE001 - 目录刷新失败不该挡住转发
            log.warning("copilot 目录刷新异常: %s", describe_exception(exc))

    # ------------------------------------------------------------------
    # 认证 / 健康
    # ------------------------------------------------------------------

    def ensure_auth(self) -> None:
        if list_accounts():
            return
        raise HTTPException(status_code=401, detail={"error": {
            "message": (
                "copilot 未登录：请执行 `buddy login copilot` 或 "
                "`buddy login copilot --gh`（复用本机 gh CLI 已登录的账号），"
                "也可以在本页「账号」里粘贴 GitHub token 导入。"
                "Copilot 没有可自动刷新的 token——GitHub 登录态失效后只能重新登录"
            ),
            "type": "authentication_error",
        }})

    def health(self) -> dict[str, Any]:
        accounts = list_accounts()
        return {
            "id": self.id,
            "name": self.name,
            "configured": bool(accounts),
            "accounts": len(accounts),
            "logins": [a.id for a in accounts],
            "models": len(self._index),
            "catalog_source": self._index.source,
            "catalog_age_s": (round(time.time() - self._index.fetched_at)
                              if self._index.fetched_at else None),
            "catalog_error": self._refresh_error,
            "cooling": failover.cooldown_report(),
        }

    # ------------------------------------------------------------------
    # 转发
    # ------------------------------------------------------------------

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        self.ensure_auth()
        await self.ensure_models()

        entry = self._entry_for(str(body.get("model") or ""))
        upstream_model = entry.id
        client_model = f"{self.id}/{entry.id}"
        stream = bool(body.get("stream", False))

        chat_body = self._chat_body(body, original, protocol)
        initiator = classify_initiator(chat_body.get("messages"))
        vision = body_has_image(chat_body.get("messages"))

        accounts = failover.available_accounts(upstream_model)
        lb = _lb_state()
        if lb is not None:
            try:
                from ..core import load_balance as lb_mod

                accounts = lb_mod.reorder(lb, self.id, accounts, model=upstream_model)
            except Exception:  # noqa: BLE001 - 均衡开关坏掉不该挡住转发
                log.debug("copilot: 负载均衡重排失败", exc_info=True)

        client = await self._get_client()
        started = time.monotonic()
        last_status = 0
        last_detail = ""

        for acct in accounts[:_MAX_ATTEMPTS]:
            try:
                token, _cred = await asyncio.to_thread(ensure_account_token, acct.id)
            except AuthError as exc:
                last_status, last_detail = 401, str(exc)
                continue
            try:
                payload = self._payload_for(body, chat_body, original, entry,
                                            upstream_model=upstream_model,
                                            protocol=protocol, stream=True)
            except HTTPException:
                # 协议与端点不相容是**确定的**错误，换账号也救不回来。
                raise
            target = entry
            if payload.pop("_fallback_chat", False):
                target = _with_endpoint(entry, PATH_CHAT)
            headers = self._headers(target, token, initiator=initiator,
                                    vision=vision, stream=True)
            try:
                request = client.build_request(
                    "POST", f"{API_BASE}{target.endpoint}", json=payload,
                    headers=headers)
                resp = await client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                last_status, last_detail = 504, describe_exception(exc)
                failover.mark_cooldown(acct.id, reason=f"超时: {last_detail}")
                continue
            except httpx.HTTPError as exc:
                last_status, last_detail = 502, describe_exception(exc)
                failover.mark_cooldown(acct.id, reason=f"连接失败: {last_detail}")
                continue

            # 指标里把这一条记到具体账号头上（多号共用一个 provider id）。
            meta = ACCOUNT_META.get()
            if meta is not None:
                meta["account"] = acct.id

            if resp.status_code >= 400:
                detail = await _read_error(resp)
                await resp.aclose()
                code, message = extract_error(detail)
                last_status, last_detail = resp.status_code, message or code
                if failover.is_model_reject(resp.status_code, code, message):
                    failover.mark_model_cooldown(
                        acct.id, upstream_model,
                        reason=f"HTTP {resp.status_code} {code or message}")
                    continue
                if failover.is_quota_signal(resp.status_code, code):
                    failover.mark_cooldown(
                        acct.id, retry_after=resp.headers.get("retry-after"),
                        quota=True, reason=f"HTTP {resp.status_code} {code or message}")
                    continue
                if resp.status_code in (401, 403):
                    # 403 有两种来源：登录态没权限，或订阅不含该模型（个人号的
                    # SKU 差异常走 403 而不是 400）。后者只该冷却模型，否则一个
                    # 落后账号会把整号钉住、连带同号其它模型一起不可用。
                    # 403 的分支已由上面的 is_model_reject 先认领。
                    failover.mark_cooldown(
                        acct.id, retry_after=resp.headers.get("retry-after"),
                        reason=f"HTTP {resp.status_code} {code or message}")
                    last_detail = _auth_detail(resp.status_code, message, acct)
                    continue
                return _upstream_error_response(resp.status_code, detail)

            try:
                return await self._emit(resp, target, client_model, protocol,
                                        stream=stream, original=original)
            except _AccountFault as fault:
                # 上游把错误塞进 200 的流里（带内 error / 只回一段错误文字的
                # 400 体）：向客户端吐字节之前识别出来了，换号重来。
                last_status, last_detail = fault.status, fault.message
                continue

        report = failover.cooldown_report()
        model_report = failover.model_cooldown_report(upstream_model)
        parts = [p for p in (report, model_report) if p]
        tail = f"最后错误 HTTP {last_status or 'n/a'}{(': ' + last_detail) if last_detail else ''}"
        message = (f"copilot 所有账号均不可用（{'；'.join(parts)}；{tail}）" if parts
                   else f"copilot 所有账号均不可用（{tail}）")
        if not accounts:
            message = (f"copilot 没有可用于 {client_model} 的账号"
                       f"（{parts[0] if parts else '全部在冷却'}）")
        status = last_status if last_status in (401, 403, 429, 504) else 502
        if not accounts and last_status == 0:
            status = 429
        log.warning("copilot forward 失败（%s，耗时 %.1fs）: %s",
                    client_model, time.monotonic() - started, message)
        raise HTTPException(status_code=status, detail={"error": {
            "message": message, "type": _error_type(status)}})

    # ---- 内部 ----

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client

    def _chat_body(self, body: dict[str, Any], original: dict[str, Any] | None,
                   protocol: str) -> dict[str, Any]:
        """客户端请求体 → OpenAI chat 形态（上游 responses/chat 都从它派生）。

        ``web/routes.py`` 的三个入口都先把请求体转成 chat 再交给
        ``forward_chat``，所以 anthropic/responses 客户端拿到的 ``body`` 已经是
        chat 形态；上游 anthropic 端点需要原生形状时从 ``original`` 取。
        """
        if isinstance(body.get("messages"), list) and body["messages"]:
            return body
        if protocol == "anthropic" and isinstance(original, dict):
            from ..protocols.anthropic_adapter import anthropic_request_to_chat

            return anthropic_request_to_chat(original)
        return body

    def _payload_for(self, body: dict[str, Any], chat_body: dict[str, Any],
                     original: dict[str, Any] | None, entry: ModelEntry, *,
                     upstream_model: str, protocol: str, stream: bool) -> dict[str, Any]:
        """chat 形态 → 具体上游端点的请求体（恒 ``stream=True``）。

        上游 anthropic 端点**没有 chat→anthropic 的反向转换器**（仓里只有
        anthropic→chat 和 chat→anthropic 的*响应*转换）。所以只有客户端本来就是
        anthropic 协议时才能走原生端点（原样透传，思考块/cache_control 都保住）；
        OpenAI 客户端撞上 anthropic 端点的模型，只有它在目录里还声明了 chat
        端点时才退回 chat，否则明确拒绝——硬转出来的请求体会静默丢 thinking。
        """
        if entry.upstream == "responses":
            from ..codex.projection import chat_to_responses_request

            return chat_to_responses_request(chat_body, model=upstream_model,
                                             stream=stream)
        if entry.upstream == "anthropic":
            if protocol == "anthropic" and isinstance(original, dict):
                payload = {k: v for k, v in original.items() if not k.startswith("_")}
                payload["model"] = upstream_model
                payload["stream"] = stream
                # 上游 anthropic 端点对 tools[].input_schema 是严格校验的：MCP/Claude
                # Code 风格的宽松 schema（联合 type、anyOf + type 并存、游离 items）
                # 会让**整条**请求 400 invalid request body，连哪个工具错了都不说。
                # 同一份 schema 走 chat 端点没问题（实测 249 工具照样 200），所以
                # 只在这条透传路上收敛；没被改动的工具原样保留。
                tools, changed = sanitize_anthropic_tools(payload.get("tools"))
                if changed:
                    payload["tools"] = tools
                    log.debug("copilot %s: 收敛了 %d 个工具的 input_schema 以适配上"
                              "游 /v1/messages", upstream_model, changed)
                return payload
            if PATH_CHAT not in entry.endpoints:
                raise HTTPException(status_code=400, detail={"error": {
                    "message": (
                        f"copilot 模型 {upstream_model} 只提供 /v1/messages 上游端点，"
                        f"只能用 anthropic 客户端（/v1/messages）调用；"
                        f"当前是 OpenAI 协议请求，请改用 anthropic 客户端"
                    ),
                    "type": "invalid_request_error",
                }})
            return _chat_payload(chat_body, upstream_model, stream,
                                 fallback_chat=True)
        return _chat_payload(chat_body, upstream_model, stream)

    def _headers(self, entry: ModelEntry, token: str, *, initiator: str,
                 vision: bool, stream: bool) -> dict[str, str]:
        if entry.upstream == "anthropic":
            headers = anthropic_headers(token, initiator=initiator, stream=stream)
        else:
            headers = upstream_headers(token, initiator=initiator, vision=vision,
                                       stream=stream)
        headers["Content-Type"] = "application/json"
        return headers

    async def _emit(self, resp: httpx.Response, entry: ModelEntry, client_model: str,
                    protocol: str, *, stream: bool,
                    original: dict[str, Any] | None) -> StreamingResponse | JSONResponse:
        """200 之后的出口分派。``resp`` 的关闭由各出口自己负责（唯一主人）。"""
        lines = resp.aiter_lines()
        buffered, fault = await _gate_first_event(lines, entry.upstream)
        if fault is not None:
            await resp.aclose()
            raise fault

        upstream = entry.upstream
        source = _Lines(buffered, lines)
        if upstream == "anthropic":
            chunks = _chat_chunks_from_anthropic(source, client_model)
        else:
            chunks = _chat_chunks(source, upstream, client_model)

        if stream:
            if protocol == "anthropic" and upstream == "anthropic":
                # 上游本来就是 anthropic：不折成 chat 再折回去，原样泵。
                return StreamingResponse(
                    _passthrough_stream(source, resp),
                    media_type="text/event-stream", headers=_SSE_HEADERS)
            if protocol == "anthropic":
                return StreamingResponse(
                    _chat_chunks_to_anthropic(chunks, client_model),
                    media_type="text/event-stream", headers=_SSE_HEADERS)
            if protocol == "responses":
                return StreamingResponse(
                    _chat_chunks_to_responses(chunks, client_model, resp),
                    media_type="text/event-stream", headers=_SSE_HEADERS)
            return StreamingResponse(
                _chat_chunks_to_sse(chunks, resp),
                media_type="text/event-stream", headers=_SSE_HEADERS)

        try:
            completion = await _aggregate(chunks, client_model)
        finally:
            await resp.aclose()

        if protocol == "responses":
            return JSONResponse(content=_completion_to_response(completion, client_model))
        if protocol == "anthropic":
            from ..protocols.anthropic_adapter import (
                chat_completion_to_anthropic_message,
            )

            return JSONResponse(content=chat_completion_to_anthropic_message(
                completion, {**(original or {}), "model": client_model}))
        return JSONResponse(content=completion)

    # ------------------------------------------------------------------
    # 额度
    # ------------------------------------------------------------------

    def quota(self) -> dict[str, Any] | None:
        """套餐 + 配额面板。

        主路径是 ``/copilot_internal/user`` 的 ``quota_snapshots``（premium 交互
        次数等）；它拒了（老账号/接口口径变了/没有订阅）就给一条说明条，而不是
        ``None``——``None`` 在前端是「本通道没有额度概念」，会连卡片一起消失，
        用户就分不清「Copilot 没额度」和「查询失败」。
        """
        accounts = list_accounts()
        if not accounts:
            return {"items": [{
                "label": "Copilot 未登录",
                "used": None, "total": None,
                "remaining": "请执行 `buddy login copilot` 或 `buddy login copilot --gh`",
                "percent": None, "reset_ts": None, "expire_ts": None,
            }], "level": None}
        didx = failover.display_index()
        multi = len(accounts) > 1
        pool = _quota_executor()
        futures = [pool.submit(self._quota_one, a, didx.get(a.id, n + 1), multi=multi)
                   for n, a in enumerate(accounts)]
        deadline = time.monotonic() + _QUOTA_ROUND_DEADLINE_S
        items: list[dict[str, Any]] = []
        failed: list[str] = []
        level: str | None = None
        for acct, fut in zip(accounts, futures):
            name = f"#{didx.get(acct.id, 0) or '?'}"
            try:
                its, lv, ok = fut.result(
                    timeout=max(deadline - time.monotonic(), 0.05))
            except Exception:  # noqa: BLE001 - 超时/异常账号都算失败
                its, lv, ok = [], None, False
            if not ok:
                failed.append(name)
                continue
            items.extend(its)
            level = level or lv
        if failed:
            items.insert(0, {
                "label": "Copilot 额度查询失败",
                "used": None, "total": None,
                "remaining": f"{len(failed)}/{len(accounts)} 个账号取不到额度"
                             f"（{'、'.join(failed)}）",
                "percent": None, "reset_ts": None, "query_failed": True,
            })
        return {"items": items, "level": level}

    def _quota_one(self, acct: Any, index: int, *, multi: bool
                   ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """单账号额度：``(items, level, ok)``。同步跑在常驻线程池里。"""
        prefix = f"Copilot #{index} · " if multi else ""
        try:
            token, _cred = ensure_account_token(acct.id)
        except Exception as exc:  # noqa: BLE001 - 单账号失败不阻塞整页
            log.warning("copilot 额度查询失败（%s）: %s", acct.id, exc)
            return [], None, False
        try:
            data = fetch_user_usage(token)
        except AuthError as exc:
            # token 好使但额度接口拒了：说明条而不是失败条，别让人去重登。
            return [{
                "label": f"{prefix}Copilot 套餐额度",
                "used": None, "total": None,
                "remaining": "额度接口未返回数据（账号无订阅或接口口径已变）",
                "percent": None, "reset_ts": None, "expire_ts": None,
                "info": str(exc),
            }], None, True
        except Exception as exc:  # noqa: BLE001
            log.warning("copilot 额度查询失败（%s）: %s", acct.id, exc)
            return [], None, False
        items = parse_user_usage(data)
        level = plan_label(data) or None
        if not items:
            items = [{
                "label": f"{prefix}Copilot 套餐额度",
                "used": None, "total": None,
                "remaining": "该账号的配额不设上限（无剩余量可展示）",
                "percent": None, "reset_ts": None, "expire_ts": None,
            }]
        for item in items:
            item["label"] = f"{prefix}{item['label']}"
        return items, level, True

    def _quota_account_ids(self) -> dict[str, str]:
        """额度标签序号 → 账号 id（必须与 :meth:`quota` 的编号同源）。"""
        return {str(i): aid for aid, i in failover.display_index().items()}

    def quota_epoch(self) -> str:
        return quota_epoch_from(list_accounts)


# ---------------------------------------------------------------------------
# 首事件闸门：200 里的错误也能换号
# ---------------------------------------------------------------------------


class _AccountFault(Exception):
    """上游在 200 流里报的错：账号级问题，换号可能救得回来。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


async def _gate_first_event(lines: AsyncIterator[str],
                            upstream: str) -> tuple[list[str], _AccountFault | None]:
    """**向客户端吐第一个字节前**判定首帧是不是错误，缓冲行供重放。

    两种「假 200」都要拦（其它通道踩过，表现为 200 空流 / 假成功）：

    - SSE 带内错误：responses 的 ``event: error`` / ``response.failed``；
      chat 的 ``{"error": {...}}`` 帧；
    - chat 端点回 200 但整段是错误 JSON（不是 SSE）。

    带内错误一律还原成 :class:`_AccountFault`（调用方据此换号），只有读流本身
    失败才是 502。返回 ``(buffered, fault)``：``fault`` 非空时 ``buffered`` 无用。
    """
    from ..codex.projection import aiter_sse_events, is_error_event

    buffered: list[str] = []
    if upstream != "responses":
        try:
            first = await lines.__anext__()
        except StopAsyncIteration:
            return buffered, None
        except Exception as exc:  # noqa: BLE001
            return buffered, _AccountFault(
                502, f"上游流读取失败: {describe_exception(exc)}")
        buffered.append(first)
        text = first.strip()
        if text.startswith("{") and "data:" not in text and "event:" not in text:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and payload.get("error"):
                return buffered, _fault_from_payload(payload)
        return buffered, None

    # responses：把已读行与后续行合成一股，闸门读数后整段保留下来重放。
    consumed = 0
    probe: list[str] = []

    async def _feed() -> AsyncIterator[str]:
        for line in buffered:
            probe.append(line)
            yield line
        async for line in lines:
            probe.append(line)
            yield line

    events = aiter_sse_events(_feed())
    try:
        async for name, payload in events:
            consumed = len(probe)
            if name in _ERROR_EVENT_NAMES:
                status, message = is_error_event(name, payload)
                return [], _AccountFault(status or 502, message or "上游返回错误事件")
            break
    except Exception as exc:  # noqa: BLE001
        return [], _AccountFault(502, f"上游流读取失败: {describe_exception(exc)}")
    finally:
        await events.aclose()
    if not consumed:
        # 一个事件都没解析出来（只有心跳/空流）：原样放行，让下游按断流处理。
        return list(probe), None
    # 闸门读到第 consumed 行；它之前的行原样重放。``probe`` 里第一行之前不
    # 存在「已读但没进 probe」的行，所以直接切片即可。
    return probe[:consumed], None


def _chat_payload(chat_body: dict[str, Any], model: str, stream: bool, *,
                  fallback_chat: bool = False) -> dict[str, Any]:
    """chat 请求体 → 上游 chat 端点请求体。

    ``include_usage`` 不显式要，上游一路回 ``usage: null``——积分就没了。
    """
    payload = {k: v for k, v in chat_body.items() if not k.startswith("_")}
    payload["model"] = model
    payload["stream"] = stream
    # chat 端点与 /v1/messages 共用同一套严格 schema 校验（逐条实测：顶层 array、
    # 空 schema、anyOf+type、多类型联合、缺 items 的 array……同样整条 400）。
    # 随包静态目录里绝大多数模型只声明 /chat/completions，这条路不收敛的话，
    # 「copilot 大工具集必 400」在那些模型上照样复现。
    tools, changed = sanitize_chat_tools(payload.get("tools"))
    if changed:
        payload["tools"] = tools
        log.debug("copilot %s: 收敛了 %d 个工具的 parameters 以适配 chat 端点",
                  model, changed)
    options = payload.get("stream_options")
    if not isinstance(options, dict):
        options = {}
    options["include_usage"] = True
    payload["stream_options"] = options
    if fallback_chat:
        # 消费方据它把请求改投 chat 端点（见 ``_payload_for`` 的 anthropic 分支）。
        payload["_fallback_chat"] = True
    return payload


def _with_endpoint(entry: ModelEntry, endpoint: str) -> ModelEntry:
    """同一模型换一条上游端点（目录条目是 frozen 的，只能换壳）。"""
    return replace(entry, endpoint=endpoint)


def _fault_from_payload(payload: dict[str, Any]) -> _AccountFault:
    """带内错误帧 → ``_AccountFault``（按错误码判定该冷却账号还是模型）。"""
    from ..codex.projection import _status_from_code

    code, message = extract_error(payload)
    if not code and isinstance(payload.get("error"), dict):
        code = str(payload["error"].get("type") or "")
    detail = message or code or "上游返回错误"
    if (
        code
        and code != "model_not_supported"
        and failover.is_model_reject(400, code, message)
    ):
        # 带内说的「不支持」没有 400 状态码佐证：可能是账号没权限，也可能是模型
        # 真的没有——按账号级 403 处理，让别的账号也试一轮，别被一条消息钉死。
        return _AccountFault(403, detail)
    if failover.is_quota_signal(400, code):
        return _AccountFault(429, detail)
    # 映射不到的码（500）按 502 走：这不是账号的错，换号也未必好，别白烧一轮。
    status = _status_from_code(code)
    if status not in (400, 401, 403, 429):
        status = 502
    return _AccountFault(status, detail)


class _Lines:
    """缓冲行 + 上游行合成一条迭代流（``aclose`` 不关上游，由出口负责）。"""

    def __init__(self, buffered: list[str], lines: AsyncIterator[str]) -> None:
        self._buffered = list(buffered)
        self._lines = lines

    async def aclose(self) -> None:
        """不关上游：上游连接的唯一主人是出口协程，闸门抛错时由 ``_emit`` 关。"""
        self._buffered = []

    async def __aiter__(self) -> AsyncIterator[str]:
        for line in self._buffered:
            yield line
        self._buffered = []
        async for line in self._lines:
            yield line


# ---------------------------------------------------------------------------
# chat chunk 管线（三种上游都先折叠成 chat chunk）
# ---------------------------------------------------------------------------


class _ChordResponsesToChat:
    """``ResponsesToChatStream`` 的补丁版：``response.completed`` 的 usage 补上积分。

    父类把 ``usage`` 整个拷进 chat usage，但 Copilot 的 ``copilot_usage`` 得先经
    ``normalize_billing_usage`` 才会变成 metrics 认的 ``credit``。
    """

    @staticmethod
    def build(model: str):
        from ..codex.projection import ResponsesToChatStream

        class _Stream(ResponsesToChatStream):
            def feed(self, event_name: str, event: dict[str, Any]):
                if event_name in ("response.completed", "response.incomplete"):
                    response = event.get("response") or event
                    if isinstance(response, dict):
                        usage = response.get("usage")
                        enriched = normalize_billing_usage(usage)
                        if isinstance(enriched, dict):
                            response = {**response, "usage": enriched}
                            event = {**event, "response": response}
                return super().feed(event_name, event)

        return _Stream(model)


async def _chat_chunks(lines: AsyncIterator[str], upstream: str,
                       model: str) -> AsyncIterator[dict[str, Any]]:
    """上游行流 → chat chunk 流（两种上游都折叠成同一种形状）。"""
    from ..codex.projection import aiter_sse_events

    if upstream == "responses":
        converter = _ChordResponsesToChat.build(model)
        try:
            async for _name, payload in aiter_sse_events(lines):
                for chunk in converter.feed(_name, payload):
                    yield chunk
            for chunk in converter.finish():
                yield chunk
        finally:
            aclose = getattr(lines, "aclose", None)
            if aclose is not None:
                await aclose()
        return

    async for chunk in _chat_chunks_from_openai(lines):
        yield chunk


async def _chat_chunks_from_openai(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """OpenAI chat SSE 行 → chunk；每条带 ``usage`` 的 chunk 补上 ``credit``。"""
    async for line in lines:
        text = line.strip()
        if not text or text.startswith(":") or not text.startswith("data:"):
            continue
        data = text[len("data:"):].strip()
        if data == "[DONE]":
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        if chunk.get("error"):
            code, message = extract_error(chunk)
            raise _AccountFault(502, message or code or "上游返回错误")
        if isinstance(chunk.get("usage"), dict):
            enriched = normalize_billing_usage(chunk["usage"])
            if isinstance(enriched, dict):
                chunk = {**chunk, "usage": enriched}
        yield chunk


async def _passthrough_stream(lines: AsyncIterator[str], resp: httpx.Response
                              ) -> AsyncIterator[str]:
    """上游 anthropic SSE 原样泵给 anthropic 客户端（``finally`` 关上游）。"""
    try:
        async for line in lines:
            yield line + "\n"
    finally:
        await resp.aclose()


async def _chat_chunks_from_anthropic(lines: AsyncIterator[str], model: str
                                      ) -> AsyncIterator[dict[str, Any]]:
    """上游 anthropic 事件流 → chat chunk 流（anthropic 上游的另两种出口）。

    ``web/routes.py`` 的三个入口都会把请求体先转成 chat，所以「anthropic 端点 +
    OpenAI 客户端」只会走到这里：把 message_start/content_block_delta/message_delta
    翻成 chat 增量。工具调用的 ``input_json_delta`` 拼回去就是 chat 那套
    ``function.arguments`` 增量，两边的分片语义一致，不需要额外状态。
    """
    from ..codex.projection import aiter_sse_events

    usage: dict[str, Any] = {}
    tool_index: dict[int, int] = {}
    started = False
    finish_reason = "stop"
    created = int(time.time())
    msg_id = ""

    def _chunk(delta: dict[str, Any] | None = None, *,
               finish: str | None = None,
               extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": msg_id or f"chatcmpl-{os.urandom(8).hex()}",
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": delta or {},
                "finish_reason": finish,
            }],
        }
        if extra:
            payload.update(extra)
        return payload

    try:
        async for name, event in aiter_sse_events(lines):
            if name == "message_start":
                message = event.get("message") or {}
                msg_id = str(message.get("id") or msg_id)
                if isinstance(message.get("usage"), dict):
                    usage.update(message["usage"])
                if not started:
                    started = True
                    yield _chunk({"role": "assistant", "content": ""})
                continue
            if name == "content_block_start":
                block = event.get("content_block") or {}
                kind = block.get("type")
                if kind == "tool_use":
                    index = int(event.get("index", 0))
                    slot = tool_index.setdefault(index, len(tool_index))
                    yield _chunk({"tool_calls": [{
                        "index": slot,
                        "id": str(block.get("id") or ""),
                        "type": "function",
                        "function": {"name": str(block.get("name") or ""),
                                     "arguments": ""},
                    }]})
                elif kind == "thinking":
                    text = str(block.get("thinking") or "")
                    if text:
                        yield _chunk({"reasoning_content": text})
                continue
            if name == "content_block_delta":
                delta = event.get("delta") or {}
                kind = delta.get("type")
                if kind == "text_delta":
                    yield _chunk({"content": str(delta.get("text") or "")})
                elif kind == "thinking_delta":
                    yield _chunk({"reasoning_content": str(delta.get("thinking") or "")})
                elif kind == "input_json_delta":
                    index = int(event.get("index", 0))
                    slot = tool_index.setdefault(index, len(tool_index))
                    yield _chunk({"tool_calls": [{
                        "index": slot,
                        "function": {"arguments": str(delta.get("partial_json") or "")},
                    }]})
                continue
            if name == "message_delta":
                stop = (event.get("delta") or {}).get("stop_reason")
                finish_reason = {
                    "max_tokens": "length",
                    "tool_use": "tool_calls",
                }.get(str(stop or ""), "stop")
                if isinstance(event.get("usage"), dict):
                    usage.update(event["usage"])
                continue
            if name == "error":
                raise _AccountFault(502, str(event.get("error") or event))
    finally:
        aclose = getattr(lines, "aclose", None)
        if aclose is not None:
            await aclose()

    if not started:
        yield _chunk({"role": "assistant", "content": ""})
    chat_usage: dict[str, Any] | None = None
    if usage:
        prompt = int(usage.get("input_tokens") or 0)
        cached = int(usage.get("cache_read_input_tokens") or 0)
        chat_usage = {
            "prompt_tokens": prompt + cached,
            "completion_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": prompt + cached + int(usage.get("output_tokens") or 0),
        }
        if cached:
            chat_usage["prompt_tokens_details"] = {"cached_tokens": cached}
        if usage.get("cache_creation_input_tokens"):
            chat_usage["cache_creation_input_tokens"] = usage["cache_creation_input_tokens"]
        for key in ("credit", "credits"):
            if isinstance(usage.get(key), (int, float)):
                chat_usage[key] = usage[key]
    yield _chunk({}, finish=finish_reason,
                 extra={"usage": chat_usage} if chat_usage else None)


async def _chat_chunks_to_sse(chunks: AsyncIterator[dict[str, Any]],
                              resp: httpx.Response) -> AsyncIterator[str]:
    """chat chunk → OpenAI SSE；``finally`` 是 ``resp`` 的唯一主人。"""
    try:
        async for chunk in chunks:
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        await resp.aclose()


async def _chat_chunks_to_anthropic(chunks: AsyncIterator[dict[str, Any]],
                                    model: str) -> AsyncIterator[str]:
    """chat chunk → Anthropic 事件流（``AnthropicStreamConverter``，已带 credit）。"""
    from ..protocols.anthropic_adapter import AnthropicStreamConverter

    converter = AnthropicStreamConverter(model)

    def _emit(name: str, payload: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _close_open() -> list[str]:
        try:
            return [_emit(n, p) for n, p in converter.close_open_blocks()]
        except Exception:  # noqa: BLE001
            return []

    try:
        async for chunk in chunks:
            for name, payload in converter.feed_chunk(chunk):
                yield _emit(name, payload)
        for name, payload in converter.finish():
            yield _emit(name, payload)
    except Exception as exc:  # noqa: BLE001 - 上游畸形数据不该只给半截流
        log.warning("copilot anthropic stream aborted: %s: %s", type(exc).__name__, exc)
        for event in _close_open():
            yield event
        yield _emit("error", {"type": "error", "error": {
            "type": "api_error", "message": f"{type(exc).__name__}: {exc}"}})
        return
    yield "data: [DONE]\n\n"


async def _chat_chunks_to_responses(chunks: AsyncIterator[dict[str, Any]], model: str,
                                    resp: httpx.Response) -> AsyncIterator[str]:
    """chat chunk → Responses 事件流（``finally`` 关上游）。"""
    from ..protocols.responses_adapter import ResponsesStreamConverter

    converter = ResponsesStreamConverter(model)

    def _emit(name: str, payload: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    try:
        async for chunk in chunks:
            for name, payload in converter.feed_chunk(chunk):
                yield _emit(name, payload)
        for name, payload in converter.finish():
            yield _emit(name, payload)
    finally:
        await resp.aclose()
    yield "data: [DONE]\n\n"


async def _aggregate(chunks: AsyncIterator[dict[str, Any]], model: str) -> dict[str, Any]:
    """chat chunk 流 → ``chat.completion``（非流式出口；工具参数按 index 拼）。"""
    text: list[str] = []
    reasoning: list[str] = []
    tools: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    chunk_id = ""
    created = int(time.time())

    async def _drive() -> None:
        nonlocal finish_reason, usage, chunk_id, created
        async for chunk in chunks:
            chunk_id = str(chunk.get("id") or chunk_id)
            if isinstance(chunk.get("created"), int):
                created = chunk["created"]
            if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}
                if isinstance(delta.get("content"), str) and delta["content"]:
                    text.append(delta["content"])
                if isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"]:
                    reasoning.append(delta["reasoning_content"])
                for call in delta.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    index = int(call.get("index", 0))
                    slot = tools.setdefault(index, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if call.get("id"):
                        slot["id"] = str(call["id"])
                    fn = call.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] += str(fn["name"])
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += str(fn["arguments"])

    try:
        await asyncio.wait_for(_drive(), timeout=_AGGREGATE_DEADLINE_S)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail={
            "error": {"message": "copilot 非流式聚合超过总时长上限", "type": "timeout"}
        }) from exc

    message: dict[str, Any] = {"role": "assistant", "content": "".join(text)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tools:
        message["tool_calls"] = [tools[i] for i in sorted(tools)]
        finish_reason = finish_reason or "tool_calls"

    payload: dict[str, Any] = {
        "id": chunk_id or f"chatcmpl-{os.urandom(8).hex()}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason or "stop",
        }],
    }
    if usage:
        payload["usage"] = usage
    return payload


def _completion_to_response(completion: dict[str, Any], model: str) -> dict[str, Any]:
    """chat.completion → Responses ``response`` 对象（非流式 responses 出口）。

    ``usage`` 原样带走（含 ``credit``），客户端与 metrics 都能读到。
    """
    message = (completion.get("choices") or [{}])[0].get("message") or {}
    output: list[dict[str, Any]] = []
    reasoning = str(message.get("reasoning_content") or "").strip()
    if reasoning:
        output.append({
            "id": f"rs_{os.urandom(12).hex()}",
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": reasoning}],
            "status": "completed",
        })
    text = str(message.get("content") or "")
    if text or not message.get("tool_calls"):
        output.append({
            "id": f"msg_{os.urandom(12).hex()}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
            "status": "completed",
        })
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        output.append({
            "id": f"fc_{os.urandom(12).hex()}",
            "type": "function_call",
            "call_id": str(call.get("id") or ""),
            "name": str(fn.get("name") or ""),
            "arguments": str(fn.get("arguments") or "{}"),
            "status": "completed",
        })
    usage = completion.get("usage") or {}
    out_usage: dict[str, Any] = {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }
    if "credit" in usage:
        out_usage["credit"] = usage["credit"]
    return {
        "id": f"resp_{os.urandom(12).hex()}",
        "object": "response",
        "created_at": completion.get("created") or int(time.time()),
        "model": model,
        "status": "completed",
        "output": output,
        "usage": out_usage,
    }


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


async def _read_error(resp: httpx.Response) -> Any:
    """读完错误体再关（``aclose`` 会丢弃未读 body，之后拿不到真实错误）。"""
    try:
        raw = await resp.aread()
    except Exception:  # noqa: BLE001
        return {}
    text = raw[:4000].decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": {"message": text[:500], "type": "upstream_error"}}


def _upstream_error_response(status: int, detail: Any) -> JSONResponse:
    """透传上游错误（形状已经是 OpenAI/anthropic 错误体，不改写）。"""
    if not isinstance(detail, dict) or not detail:
        detail = {"error": {"message": f"copilot 上游返回 HTTP {status}",
                            "type": "upstream_error"}}
    return JSONResponse(status_code=status, content=detail)


def _auth_detail(status: int, message: str, account) -> str:
    """401/403 的可操作提示：账号 + 下一步动作 + 上游原话。

    优先报 GitHub login（``gh auth login --user`` 要的是 login）；``id`` 在
    手工粘贴 token、拿不到官方登录名时会是哈希派生的 ``acct-xxxx``，照抄给
    用户只会得到「未知用户」。
    """
    login = getattr(account, "name", "") or getattr(account, "id", "")
    hint = (
        f"账号 {login} 的 GitHub 登录态被拒（HTTP {status}），"
        f"请 `gh auth login --user {login}` 或在管理页重新导入 token"
    )
    return f"{hint}；上游: {message}" if message else hint


def _error_type(status: int) -> str:
    return {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        429: "rate_limit_error",
        504: "timeout",
    }.get(status, "bad_gateway")
