"""Codex provider —— ChatGPT 订阅通道转发（上游 Responses 协议）。

与其它通道最大的不同：**上游不是 Chat Completions**。Codex 后端是
``POST https://chatgpt.com/backend-api/codex/responses`` 的 Responses 协议
SSE，所以本通道在 :mod:`buddy_proxy.codex.projection` 里自带双向投影：

- 请求：chat body → Responses body（:func:`chat_to_responses_request`）
- 响应流式：Responses SSE → chat chunk（:class:`ResponsesToChatStream`）
- 响应非流式：response 对象 → chat.completion（:func:`responses_to_chat_completion`）

三种客户端协议的分派（``forward`` 的 ``protocol`` 参数）：

- ``"openai"``：投影成 chat chunk/chat.completion
- ``"responses"``：上游本来就是 Responses，**原样透传**（最省转换、最贴近原生）
- ``"anthropic"``：先投影成 chat，再交给 ``anthropic_adapter`` 转 Anthropic

多账号 failover 与首事件闸门照 kimi 的结构：429/403 冷却换号，401 强刷一次
仍失败则换号，首个语义事件到达后绝不重放（防重复计费）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.metrics import ACCOUNT_META
from ..providers.base import BaseProvider
from . import failover
from .catalog import (
    CatalogError,
    build_model_entries,
    fetch_catalog,
    write_catalog,
)
from .credentials import (
    AuthError,
    codex_state_dir,
    ensure_account_token,
    has_cred,
    list_accounts,
    load_account_cred,
)
from .images import (
    ImageUpstreamError,
    build_image_body,
    collect_image,
    extract_prompt,
    to_chat_completion as image_to_chat_completion,
)
from .projection import (
    ResponsesToChatStream,
    aiter_sse_events,
    is_error_event,
    responses_to_chat_completion,
)
from .upstream import (
    build_headers,
    build_upstream_body,
    load_models_file,
    new_session_id,
    normalize_base_url,
    notice_item,
)

log = logging.getLogger(__name__)

#: 流式超时：Responses 上游首 token 可能需要较长推理时间（尤其高 reasoning
#: effort），read 放到 300s。
_TIMEOUT_STREAM = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=15.0)
#: 非流式：一次 read 拿到全部响应 = 总时长上限。
_TIMEOUT_NONSTREAM = httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=15.0)
_TIMEOUT = _TIMEOUT_NONSTREAM
#: failover 循环的尝试期总预算（只挡「还没开始试」的尝试）。
_ATTEMPT_DEADLINE_S = 600.0
#: 闸门缓冲行上限（防异常上游无界攒内存）。
_GATE_BUFFER_MAX_LINES = 256

#: 上游心跳事件（不携带业务数据）。生图与高 reasoning 模型的首帧常是它，
#: 不能被当成「语义已至」。
_HEARTBEAT_EVENTS = frozenset({"keepalive", "ping", "heartbeat"})

#: 出厂目录（随包分发）：首次启动与兜底用。
_MODELS_JSON = Path(__file__).with_name("models.json")
_IMAGES_JSON = Path(__file__).with_name("image_models.json")

#: models.json 损坏时的内置兜底（保持通道可用）。
_FALLBACK_MODELS = [{
    "id": "gpt-6.1-sol",
    "description": "GPT-6.1 Sol (fallback)",
}]


def _runtime_models_path() -> Path:
    """运行期目录快照（「↻ 重新拉取」写这里，不污染仓库工作区）。

    与凭据同目录（``CODEX_STATE_DIR``），删掉即回落到随包出的厂目录。
    """
    return codex_state_dir() / "models.json"


def _load_models() -> list[dict[str, Any]]:
    for path in (_runtime_models_path(), _MODELS_JSON):
        models = load_models_file(str(path))
        if models:
            return models
    log.warning("codex: models.json 缺失或损坏，使用内置兜底模型表")
    return [dict(m) for m in _FALLBACK_MODELS]


def _load_images() -> list[dict[str, Any]]:
    """图片模型目录（生图走 ``/images/generations``，不混进对话目录文件）。"""
    models = load_models_file(str(_IMAGES_JSON))
    return [{**m, "type": "image"} for m in models]


class CodexProvider(BaseProvider):
    """ChatGPT / Codex 订阅通道。"""

    id = "codex"
    name = "Codex (ChatGPT)"

    def __init__(self) -> None:
        self._models: list[dict[str, Any]] = _load_models()
        self._images = _load_images()
        self._client: httpx.AsyncClient | None = None
        self._refresh_lock = asyncio.Lock()

    # -- 目录 --

    def models(self) -> Sequence[dict[str, Any]]:
        return [dict(m) for m in (*self._models, *self._images)]

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        want = (model or "").strip().removeprefix(f"{self.id}/")
        return any(str(m.get("id")) == want for m in (*self._models, *self._images))

    def _is_image_model(self, model: str) -> bool:
        return any(str(m.get("id")) == model for m in self._images)

    async def refresh_models(self, force: bool = False) -> list[dict[str, Any]]:
        """从上游目录重新拉取对话模型，并写回 ``models.json``。

        模型页「↻ 重新拉取」按钮的数据源（``GET /ui/api/models/refresh``）。
        上游目录**按账号套餐**给，所以逐账号尝试、第一个成功即用；单账号
        失败不冷却（目录失败不代表账号不能转发），全部失败则保留旧快照并
        抛错，由接口层转成 502——绝不把空表写进文件。
        """
        del force  # 目录没有缓存期：按钮本来就是「强制重拉」
        async with self._refresh_lock:
            accounts = list_accounts()
            if not accounts:
                raise CatalogError("未登录：先跑 `buddy login codex`")
            client = await self._get_client()
            errors: list[str] = []
            upstream: list[dict[str, Any]] | None = None
            for acct in accounts:
                try:
                    access_token, cred = await asyncio.to_thread(
                        ensure_account_token, acct.id)
                except AuthError as exc:
                    errors.append(f"{acct.id}: {exc}")
                    continue
                try:
                    upstream = await fetch_catalog(
                        client,
                        base_url=str(cred.get("base_url") or ""),
                        access_token=access_token,
                        account_id=str(cred.get("chatgpt_account_id") or ""),
                    )
                    break
                except CatalogError as exc:
                    errors.append(f"{acct.id}: {exc}")
            if upstream is None:
                raise CatalogError("所有账号均拉取失败：" + "；".join(errors))

            entries = build_model_entries(
                upstream, image_ids=[str(m["id"]) for m in self._images])
            chat_entries = [e for e in entries if e.get("type") != "image"]
            path = _runtime_models_path()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                write_catalog(path, chat_entries)
            except OSError as exc:
                raise CatalogError(f"写回 {path} 失败: {exc}") from exc
            self._models = chat_entries
            log.info("codex 模型目录已刷新（%d 个对话模型 + %d 个图片模型）",
                     len(chat_entries), len(self._images))
            return self.models()

    def resolve_model(self, model: str) -> str | None:
        want = (model or "").strip().removeprefix(f"{self.id}/")
        return want if self.accepts_model(want) else None

    # -- 认证 --

    def ensure_auth(self) -> None:
        if not has_cred():
            raise HTTPException(
                status_code=401,
                detail={"error": {
                    "message": ("codex 通道未登录：先跑 `buddy login codex`"
                                "（无需安装 Codex CLI；已装并登录过的话，"
                                "也可在管理面板导入 ~/.codex/auth.json）"),
                    "type": "invalid_request_error",
                }},
            )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client

    # -- 转发 --

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        self.ensure_auth()
        stream = bool(body.get("stream", False))
        default_model = self._models[0]["id"] if self._models else _FALLBACK_MODELS[0]["id"]
        model = str(body.get("model") or default_model).removeprefix(f"{self.id}/")
        entry = next((m for m in self._models if str(m.get("id")) == model), None)
        if entry is None:
            if self._is_image_model(model):
                return await self._forward_image(
                    body, model, protocol, original, stream)
            raise HTTPException(
                status_code=400,
                detail={"error": {
                    "message": f"codex 未知模型: {model}",
                    "type": "invalid_request_error",
                }},
            )
        upstream_model = str(entry["id"])

        accounts = failover.available_accounts()
        if not accounts:
            raise HTTPException(
                status_code=429,
                detail={"error": {
                    "message": (f"codex 所有账号均在冷却中：{failover.cooldown_report()}"
                                "；额度冷却到点自动恢复"),
                    "type": "rate_limit_error",
                }},
            )

        meta = ACCOUNT_META.get()
        client = await self._get_client()
        started = time.monotonic()
        tried = 0
        last_status = 0
        last_detail = ""

        for acct in accounts:
            if tried and time.monotonic() - started > _ATTEMPT_DEADLINE_S:
                last_status = last_status or 504
                last_detail = last_detail or "尝试预算用尽（上游持续无响应）"
                break
            tried += 1

            try:
                access_token, cred = await asyncio.to_thread(ensure_account_token, acct.id)
            except AuthError as exc:
                failover.mark_cooldown(acct.id, reason=f"凭据不可用: {exc}")
                continue

            base_url = normalize_base_url(str(cred.get("base_url") or ""))
            account_id = str(cred.get("chatgpt_account_id") or "")
            if meta is not None:
                meta["account"] = acct.id

            # 上游**只**接受 SSE：非流式请求（chat / anthropic 不带 stream）也必须
            # 以 stream=true 打过去，再把 response.completed 里的终态对象聚合成
            # 非流式响应。带 stream=false 会被直接 400「Stream must be set to true」。
            upstream_body = build_upstream_body(body, model=upstream_model, stream=True)

            resp = await self._post(
                client, base_url, upstream_body, access_token, account_id, stream)
            if isinstance(resp, str):  # 网络错误：已冷却
                last_status, last_detail = 502, resp
                failover.mark_cooldown(acct.id, reason=resp)
                continue

            # 401：token 被上游拒（refresh_token 被轮换/作废等）——强刷一次
            if resp.status_code == 401:
                last_status, last_detail = 401, await _error_detail(resp, stream)
                await _drain_and_close(resp, stream)
                try:
                    access_token, cred = await asyncio.to_thread(
                        ensure_account_token, acct.id, force_refresh=True)
                except AuthError as exc:
                    failover.mark_cooldown(acct.id, reason=f"强刷失败: {exc}")
                    continue
                account_id = str(cred.get("chatgpt_account_id") or "")
                resp = await self._post(
                    client, base_url, upstream_body, access_token, account_id, stream)
                if isinstance(resp, str):
                    last_status, last_detail = 502, resp
                    failover.mark_cooldown(acct.id, reason=resp)
                    continue
                if resp.status_code == 401:
                    last_status, last_detail = 401, await _error_detail(resp, stream)
                    await _drain_and_close(resp, stream)
                    failover.mark_cooldown(
                        acct.id,
                        reason="401 强刷后仍被拒（refresh_token 可能已失效，请重新登录）")
                    continue

            # 403/429：账号级拒绝（额度/风控/订阅不含该模型）——冷却换号
            if resp.status_code in (403, 429):
                last_status = resp.status_code
                last_detail = await _error_detail(resp, stream)
                failover.mark_cooldown(
                    acct.id,
                    retry_after=resp.headers.get("Retry-After"),
                    quota=resp.status_code == 429,
                    reason=f"HTTP {resp.status_code}{(': ' + last_detail) if last_detail else ''}",
                )
                await _drain_and_close(resp, stream)
                continue

            if resp.status_code >= 400:
                # 其余业务 4xx（模型名不合法等）：换号没意义，原样透传
                if stream:
                    await resp.aread()
                try:
                    return _upstream_error_response(resp)
                finally:
                    await resp.aclose()

            # 200：过首事件闸门（上游恒为 SSE；非流式客户端也要先读完流）
            gate = await _gate_first_event(resp, True)
            if gate.account_error:
                last_status, last_detail = gate.code, gate.message
                failover.mark_cooldown(
                    acct.id, quota=gate.code == 429,
                    reason=f"带内 error {gate.code}: {gate.message}")
                await _drain_and_close(gate.resp, stream, gate.lines)
                continue
            if gate.eof:
                if gate.timed_out:
                    last_status, last_detail = 504, "首事件前读超时"
                    failover.mark_cooldown(acct.id, reason="首事件前读超时")
                else:
                    last_status, last_detail = 502, "首事件前断流（上游空响应）"
                await _drain_and_close(gate.resp, stream, gate.lines)
                continue

            if stream:
                replay = _ReplayStream(gate.resp, gate.buffered, gate.lines)
                if protocol == "responses":
                    # 上游原生就是 Responses：原样透传，零转换
                    return StreamingResponse(
                        _pass_through_lines(replay),
                        media_type="text/event-stream",
                        headers=_SSE_HEADERS,
                    )
                if protocol == "anthropic":
                    return StreamingResponse(
                        _to_anthropic_stream(replay, upstream_model),
                        media_type="text/event-stream",
                        headers=_SSE_HEADERS,
                    )
                return StreamingResponse(
                    _to_chat_stream(replay, upstream_model, stream=True),
                    media_type="text/event-stream",
                    headers=_SSE_HEADERS,
                )

            # 非流式客户端：上游仍是一条 SSE（见上面注释），这里把最后一个
            # response 对象聚合出来，再按 protocol 还原成非流式响应。
            payload = await _collect_response(gate.resp, _replay_lines(gate))
            await gate.resp.aclose()
            if payload is None:
                raise HTTPException(
                    status_code=502,
                    detail={"error": {"message": "codex upstream returned no response object",
                                      "type": "bad_gateway"}},
                )
            if protocol == "responses":
                # 上游 response 对象直接回给 Responses 客户端
                return JSONResponse(content=payload)
            chat = responses_to_chat_completion(payload, model=upstream_model)
            if protocol == "anthropic":
                from ..protocols.anthropic_adapter import (
                    chat_completion_to_anthropic_message,
                )

                chat = chat_completion_to_anthropic_message(chat, original)
            return JSONResponse(content=chat)

        report = failover.cooldown_report()
        tail = f"最后错误 HTTP {last_status or 'n/a'}{(': ' + last_detail) if last_detail else ''}"
        raise HTTPException(
            status_code=last_status if last_status in (401, 403, 429, 504) else 502,
            detail={"error": {
                "message": (f"codex 所有账号均不可用（{report}；{tail}）" if report
                            else f"codex 所有账号均不可用（{tail}）"),
                "type": ("timeout" if last_status == 504
                         else "rate_limit_error" if last_status in (403, 429)
                         else "bad_gateway"),
            }},
        )

    async def _post(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        body: dict[str, Any],
        access_token: str,
        account_id: str,
        stream: bool,
    ) -> httpx.Response | str:
        """发一次上游请求；网络层失败时返回错误文案字符串（调用方冷却换号）。"""
        headers = build_headers(
            access_token, account_id=account_id,
            session_id=new_session_id(), stream=stream)
        try:
            req = client.build_request(
                "POST", f"{base_url}/responses", json=body, headers=headers,
                timeout=_TIMEOUT_STREAM if stream else _TIMEOUT_NONSTREAM)
            return await client.send(req, stream=stream)
        except httpx.TimeoutException as exc:
            return f"upstream timeout: {exc}"
        except httpx.HTTPError as exc:
            return f"upstream error: {exc}"

    # -- 生图 --

    async def _forward_image(
        self,
        body: dict[str, Any],
        model: str,
        protocol: str,
        original: dict[str, Any] | None,
        stream: bool,
    ) -> StreamingResponse | JSONResponse:
        """``gpt-image-*``：走 ``/images/generations``，按协议包成 chat/Anthropic。

        图片是一次性产物：上游 SSE 收完才有图，所以**客户端选流式也只是
        最后一帧转发**（先发 role 帧再发整图，避免客户端以为卡死）。生成
        失败按对话同款策略换号（401/403/429 冷却，其余透传最后一次错误）。
        """
        prompt = extract_prompt(body)
        if not prompt:
            raise HTTPException(
                status_code=400,
                detail={"error": {
                    "message": "codex 生图缺少 prompt（放在 messages 的用户文本里）",
                    "type": "invalid_request_error",
                }},
            )
        accounts = failover.available_accounts()
        if not accounts:
            raise HTTPException(
                status_code=429,
                detail={"error": {
                    "message": f"codex 所有账号均在冷却中：{failover.cooldown_report()}",
                    "type": "rate_limit_error",
                }},
            )
        image_body = build_image_body(model, prompt, **{
            k: body.get(k) for k in ("size", "quality", "background")
        })
        meta = ACCOUNT_META.get()
        client = await self._get_client()
        last_status, last_detail = 0, ""
        for acct in accounts:
            try:
                access_token, cred = await asyncio.to_thread(ensure_account_token, acct.id)
            except AuthError as exc:
                failover.mark_cooldown(acct.id, reason=f"凭据不可用: {exc}")
                continue
            if meta is not None:
                meta["account"] = acct.id
            try:
                result = await collect_image(
                    client,
                    base_url=str(cred.get("base_url") or ""),
                    body=image_body,
                    access_token=access_token,
                    account_id=str(cred.get("chatgpt_account_id") or ""),
                )
            except ImageUpstreamError as exc:
                last_status, last_detail = exc.status, str(exc)
                if exc.status in (401, 403, 429, 502):
                    failover.mark_cooldown(
                        acct.id, quota=exc.status == 429,
                        reason=f"生图 HTTP {exc.status}: {exc}")
                continue

            chat = image_to_chat_completion(result, model=model, prompt=prompt)
            if protocol == "anthropic":
                from ..protocols.anthropic_adapter import (
                    chat_completion_to_anthropic_message,
                )

                return JSONResponse(
                    content=chat_completion_to_anthropic_message(chat, original))
            if protocol == "responses":
                return JSONResponse(content=_image_as_response(chat, model))
            if stream:
                return StreamingResponse(
                    _image_chat_stream(chat), media_type="text/event-stream",
                    headers=_SSE_HEADERS)
            return JSONResponse(content=chat)

        raise HTTPException(
            status_code=last_status if last_status in (401, 403, 429) else 502,
            detail={"error": {
                "message": f"codex 生图失败（{failover.cooldown_report() or '无可用账号'}；"
                           f"HTTP {last_status or 'n/a'}{(': ' + last_detail) if last_detail else ''}）",
                "type": "bad_gateway",
            }},
        )

    # -- 健康 / 额度 --

    def health(self) -> dict[str, Any]:
        accounts = list_accounts()
        first = accounts[0] if accounts else None
        cred = load_account_cred(first.id) if first else None
        return {
            "id": self.id,
            "name": self.name,
            "accounts": len(accounts),
            "email": str((cred or {}).get("email") or ""),
            "plan_type": str((cred or {}).get("plan_type") or ""),
        }

    def quota(self) -> dict[str, Any] | None:
        """套餐额度。

        ChatGPT 后端没有公开的「查额度」端点（Codex CLI 自己也是从响应头与
        本地用量推的），因此这里只做**本地可确定**的部分：

        - 未登录：说明条（返回 None 会让整块额度卡从面板消失）
        - 登录了：按账号列出套餐档位与凭据到期时刻

        真实 `used_percent` 需要上游 ``rate_limits`` 数据，接入点见
        :func:`buddy_proxy.codex.upstream.parse_rate_limits`——等拿到稳定
        数据源（响应头观测）再接，不在这里编数字。
        """
        accounts = list_accounts()
        if not accounts:
            return {"items": [notice_item(
                "Codex 额度", "未登录：先跑 `buddy login codex`（无需安装 Codex CLI）")],
                "level": None}
        multi = len(accounts) > 1
        items: list[dict[str, Any]] = []
        level: str | None = None
        for i, acct in enumerate(accounts, start=1):
            cred = load_account_cred(acct.id) or {}
            plan = str(cred.get("plan_type") or "free")
            if level is None:
                level = plan
            prefix = f"Codex #{i} · " if multi else ""
            who = acct.alias or str(cred.get("email") or "") or acct.id
            # 条目必须走**进度条形态**而不是 notice 形态：管理页面板按
            # 「percent == null && used == null」把说明条判成 notice、横贯全宽，
            # 账号分组（▲▼/✎/✕ 按钮）就永远不出现（kimi 靠真实百分比天然满足）。
            # 上游没有百分比，所以 total/used **留 None**——前端文案显示
            # 「剩 — · 已用未知」，不画假进度条、也不编「100%」。
            items.append({
                "label": f"{prefix}{who}",
                "used": None, "total": None, "remaining": None,
                "percent": 0, "unit": "percent",
                "reset_ts": None, "expire_ts": None,
                # 别写「无公开端点」：上游 ``rate_limits`` 解析已就绪，缺的只是
                # 稳定数据源（接入点见 comments 与 upstream.parse_rate_limits）
                "note": f"{plan} 套餐 · 额度随请求实时扣减（上游未提供百分比，暂不展示）",
            })
        return {"items": items, "level": level}

    def quota_epoch(self) -> str:
        """账号集合指纹：账号变化时让额度缓存失效。"""
        return ",".join(f"{a.id}:{a.priority}" for a in list_accounts())


# ---------------------------------------------------------------------------
# 下游协议适配
# ---------------------------------------------------------------------------

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "close"}


def _upstream_error_response(resp: httpx.Response) -> JSONResponse:
    """把上游错误原样包成 JSONResponse（保留状态码与 body）。"""
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        payload = {"error": {"message": resp.text or f"HTTP {resp.status_code}",
                             "type": "upstream_error"}}
    return JSONResponse(content=payload, status_code=resp.status_code)


async def _error_detail(resp: httpx.Response, stream: bool) -> str:
    """从错误响应体抽 ``error.message``（只用于日志/客户端报错文案）。"""
    try:
        if stream:
            await resp.aread()
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return ""
    err = payload.get("error") if isinstance(payload, dict) else None
    return str(err.get("message") or "") if isinstance(err, dict) else ""


async def _to_chat_stream(
    replay: "_ReplayStream", model: str, *, stream: bool = True,
) -> AsyncIterator[bytes]:
    """上游 Responses SSE → chat.completion.chunk SSE。"""
    conv = ResponsesToChatStream(model)

    def _emit(chunk: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

    try:
        async for name, payload in aiter_sse_events(replay.aiter_lines()):
            if not name:
                continue
            for chunk in conv.feed(name, payload):
                yield _emit(chunk)
        for chunk in conv.finish():
            yield _emit(chunk)
    except Exception as exc:  # noqa: BLE001 - 中途炸掉也要给客户端一个结构完整的结尾
        log.warning("codex chat stream aborted: %s: %s", type(exc).__name__, exc)
        err = {"error": {"message": f"{type(exc).__name__}: {exc}",
                         "type": "upstream_error"}}
        yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode()
    finally:
        yield b"data: [DONE]\n\n"
        await replay.aclose()


async def _to_anthropic_stream(replay: "_ReplayStream", model: str) -> AsyncIterator[str]:
    """上游 Responses SSE → Anthropic 事件流（``/v1/messages`` 客户端要的形状）。

    先把 Responses 投影成 chat chunk，再复用 ``AnthropicStreamConverter``
    （mimo/trae/kimi 同款），这样 ``reasoning_content`` → thinking 块、
    ``tool_calls`` → tool_use 块的行为与其它通道完全一致。
    """
    from ..protocols.anthropic_adapter import AnthropicStreamConverter

    converter = AnthropicStreamConverter(model)
    conv = ResponsesToChatStream(model)

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
        async for name, payload in aiter_sse_events(replay.aiter_lines()):
            if not name:
                continue
            for chunk in conv.feed(name, payload):
                for event_name, event_payload in converter.feed_chunk(chunk):
                    yield _emit(event_name, event_payload)
        for chunk in conv.finish():
            for event_name, event_payload in converter.feed_chunk(chunk):
                yield _emit(event_name, event_payload)
        for event_name, event_payload in converter.finish():
            yield _emit(event_name, event_payload)
    except Exception as exc:  # noqa: BLE001 — 上游畸形数据不该让客户端只收到半截流
        log.warning("codex anthropic stream aborted: %s: %s", type(exc).__name__, exc)
        for event in _abort(f"{type(exc).__name__}: {exc}"):
            yield event
        return
    finally:
        await replay.aclose()
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# 流式支撑：首事件闸门 / 重放流
# ---------------------------------------------------------------------------

@dataclass
class _Gate:
    """首事件闸门的判定结果（结构同 kimi，``lines`` 必须被后续消费者续跑）。"""

    resp: httpx.Response
    committed: bool = False
    buffered: list[str] = field(default_factory=list)
    lines: AsyncIterator[str] | None = None
    payload: dict[str, Any] | None = None
    account_error: bool = False
    code: int = 0
    message: str = ""
    eof: bool = False
    timed_out: bool = False


async def _gate_first_event(resp: httpx.Response, stream: bool) -> _Gate:
    """压住第一个上游事件再决定透传还是换号。

    ``stream`` 参数只表示**上游**是不是流式；Codex 后端对 ``/responses``
    只接受 SSE，所以调用方恒传 ``True``（非流式客户端也是先收完整条流再
    聚合）。非流式 JSON 分支保留给可能的兼容基址。

    流式：缓冲原始 SSE 行直到第一条能定性的事件——语义已至即 committed
    （缓冲行随透传补放，客户端无损）；``error``/``response.failed`` 判账号级
    则换号；语义事件前断流按 eof 处理（换号，防「200 空流假成功」）。
    **心跳（``keepalive``）不算语义事件**：生图/高推理模型首帧往往是心跳，
    把它当 committed 会让换号失效。
    """
    if not stream:
        try:
            payload = resp.json()
        except Exception:
            return _Gate(resp=resp, committed=True, payload=None)
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            err = payload["error"]
            code = _code_from(err)
            if code in (401, 403, 429):
                return _Gate(resp=resp, account_error=True, code=code,
                             message=str(err.get("message") or ""))
        return _Gate(resp=resp, committed=True,
                     payload=payload if isinstance(payload, dict) else None)

    buffered: list[str] = []
    lines = resp.aiter_lines()
    try:
        async for line in lines:
            buffered.append(line)
            if len(buffered) > _GATE_BUFFER_MAX_LINES:
                return _Gate(resp=resp, committed=True, buffered=buffered, lines=lines)
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            data = stripped[5:].strip()
            if not data:
                continue
            if data == "[DONE]":
                return _Gate(resp=resp, eof=True, lines=lines)
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            name = str(payload.get("type") or "")
            if name in _HEARTBEAT_EVENTS:
                # 心跳只证明连接活着，不证明这次请求会成功：继续等真正的
                # 语义事件（生图首帧实测要等 16s+，全是 keepalive）。
                # 心跳帧不必重放给下游（它不含业务数据）
                buffered.clear()
                continue
            code, message = is_error_event(name, payload)
            if code in (401, 403, 429):
                return _Gate(resp=resp, account_error=True, code=code,
                             message=message, lines=lines)
            if code:
                # 其它错误（500 等）：已 committed，交给下游转换器透出
                return _Gate(resp=resp, committed=True, buffered=buffered, lines=lines)
            # 任何非错误的 Responses 事件都算语义已至：
            # response.created 一到就可以安全透传（此时不再可能换号）
            return _Gate(resp=resp, committed=True, buffered=buffered, lines=lines)
    except httpx.TimeoutException:
        return _Gate(resp=resp, eof=True, timed_out=True, lines=lines)
    except httpx.HTTPError:
        return _Gate(resp=resp, eof=True, lines=lines)
    return _Gate(resp=resp, eof=True, lines=lines)


def _code_from(err: dict[str, Any]) -> int:
    """错误对象里的状态码（先看数值 code，再看语义串）。"""
    raw = err.get("code") or err.get("status")
    if isinstance(raw, int) and 400 <= raw <= 599:
        return raw
    text = str(raw or "").lower()
    if any(k in text for k in ("rate_limit", "quota", "usage_limit")):
        return 429
    if any(k in text for k in ("unauthorized", "invalid_api_key", "auth")):
        return 401
    if "forbidden" in text or "permission" in text:
        return 403
    return 0


class _ReplayStream:
    """闸门缓冲行 → 真实流的适配器（先补放缓冲，再接原流）。

    下游只用 ``aiter_lines()``/``aclose()``。``lines`` 是闸门的在途迭代器：
    续跑它而不是重开 ``resp.aiter_lines()``，否则 httpx 会判二次消费。
    """

    def __init__(self, resp: httpx.Response, buffered: list[str],
                 lines: AsyncIterator[str] | None = None) -> None:
        self._resp = resp
        self._buffered = list(buffered)
        self._lines = lines

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._buffered:
            yield line
        if self._lines is not None:
            async for line in self._lines:
                yield line
        else:  # pragma: no cover - 闸门流式路径必然带 lines
            async for line in self._resp.aiter_lines():
                yield line

    async def aclose(self) -> None:
        await self._resp.aclose()


def _image_as_response(chat: dict[str, Any], model: str) -> dict[str, Any]:
    """生图 chat.completion → Responses 对象（``/v1/responses`` 客户端）。

    图片是 assistant message 的 ``output_image`` content part；文本放
    ``output_text``。usage 字段名用 Responses 口径。
    """
    message = (chat.get("choices") or [{}])[0].get("message") or {}
    content: list[dict[str, Any]] = []
    if message.get("content"):
        content.append({"type": "output_text", "text": str(message["content"])})
    for item in message.get("images") or []:
        url = ((item or {}).get("image_url") or {}).get("url")
        if url:
            content.append({"type": "output_image", "image_url": url})
    usage = chat.get("usage") or {}
    return {
        "id": chat.get("id") or f"resp_img_{chat.get('created')}",
        "object": "response",
        "created_at": chat.get("created"),
        "status": "completed",
        "model": model,
        "output": [{"type": "message", "role": "assistant", "id": "msg_img",
                    "content": content}],
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


async def _image_chat_stream(chat: dict[str, Any]) -> AsyncIterator[bytes]:
    """生图 chat.completion → 两帧 chat chunk（role 帧 + 整图帧）。"""
    from .images import sse_chunk

    message = (chat.get("choices") or [{}])[0].get("message") or {}
    base = {"id": chat.get("id"), "object": "chat.completion.chunk",
            "created": chat.get("created"), "model": chat.get("model")}
    yield sse_chunk({**base, "choices": [{
        "index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}).encode()
    delta = {k: message[k] for k in ("content", "images") if message.get(k)}
    yield sse_chunk({**base, "choices": [{
        "index": 0, "delta": delta, "finish_reason": "stop"}]}).encode()
    yield b"data: [DONE]\n\n"


async def _replay_lines(gate: "_Gate") -> AsyncIterator[str]:
    """闸门缓冲行 → 在途迭代器（非流式聚合时也要先补放缓冲）。"""
    for line in gate.buffered:
        yield line
    if gate.lines is not None:
        async for line in gate.lines:
            yield line


async def _collect_response(
    resp: httpx.Response, lines: AsyncIterator[str] | None = None,
) -> dict[str, Any] | None:
    """把整条 Responses SSE 收拢成最终 response 对象（非流式客户端用）。

    终态事件是 ``response.completed``（或 ``response.incomplete`` 截断）；
    客户端只要最终结果，中间 delta 全部丢弃。``lines`` 是闸门的在途迭代器
    （已缓冲的心跳被清过，续跑它而不是重开 ``aiter_lines``）。
    """
    source = lines if lines is not None else resp.aiter_lines()
    final: dict[str, Any] | None = None
    async for name, payload in aiter_sse_events(source):
        kind = name or str(payload.get("type") or "")
        if kind in ("response.completed", "response.incomplete"):
            body = payload.get("response")
            if isinstance(body, dict):
                final = body
        elif kind == "response.failed":
            # 走到这里说明闸门已经放行了（例如非账号级错误），把错误体当结果
            body = payload.get("response")
            if isinstance(body, dict):
                return body
    return final


async def _pass_through_lines(replay: _ReplayStream) -> AsyncIterator[bytes]:
    """responses 直通：行迭代器 → 字节流（``line + "\\n"`` 与原字节等价）。"""
    try:
        async for line in replay.aiter_lines():
            yield (line + "\n").encode()
    finally:
        await replay.aclose()


async def _drain_and_close(resp: httpx.Response, stream: bool,
                           lines: AsyncIterator[str] | None = None) -> None:
    """读完丢弃响应体并关闭（换号前必须回收连接）。"""
    try:
        if stream:
            try:
                if lines is not None:
                    async for _ in lines:
                        pass
                elif not resp.is_stream_consumed:
                    await resp.aread()
            except (httpx.HTTPError, httpx.StreamError):
                pass
    finally:
        await resp.aclose()


# ---------------------------------------------------------------------------
# 冒烟自测：python -m buddy_proxy.codex.provider
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    p = CodexProvider()
    print("health:", json.dumps(p.health(), ensure_ascii=False))
    if not has_cred():
        print("未登录：先跑 buddy login codex（无需安装 Codex CLI）")
        raise SystemExit(1)
    resp = asyncio.run(p.forward(
        {
            "model": _load_models()[0]["id"],
            "messages": [{"role": "user", "content": "只回复两个字：pong"}],
            "stream": False,
        },
        "openai",
    ))
    print("status:", resp.status_code)
    print(str(resp.body)[:400])
