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
from .credentials import (
    AuthError,
    ensure_account_token,
    has_cred,
    list_accounts,
    load_account_cred,
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

_MODELS_JSON = Path(__file__).with_name("models.json")

#: models.json 损坏时的内置兜底（保持通道可用）。
_FALLBACK_MODELS = [{
    "id": "gpt-5.5",
    "description": "GPT-5.5 (fallback)",
}]


def _load_models() -> list[dict[str, Any]]:
    models = load_models_file(str(_MODELS_JSON))
    if not models:
        log.warning("codex: models.json 缺失或损坏，使用内置兜底模型表")
        return [dict(m) for m in _FALLBACK_MODELS]
    return models


class CodexProvider(BaseProvider):
    """ChatGPT / Codex 订阅通道。"""

    id = "codex"
    name = "Codex (ChatGPT)"

    def __init__(self) -> None:
        self._models: list[dict[str, Any]] = _load_models()
        self._client: httpx.AsyncClient | None = None

    # -- 目录 --

    def models(self) -> Sequence[dict[str, Any]]:
        return [dict(m) for m in self._models]

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        want = (model or "").strip().removeprefix(f"{self.id}/")
        return any(str(m.get("id")) == want for m in self._models)

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
        model = str(body.get("model") or self._models[0]["id"]).removeprefix(f"{self.id}/")
        entry = next((m for m in self._models if str(m.get("id")) == model), None)
        if entry is None:
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

            upstream_body = build_upstream_body(body, model=upstream_model, stream=stream)

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

            # 200：过首事件闸门
            gate = await _gate_first_event(resp, stream)
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

            payload = gate.payload
            if payload is None:
                await gate.resp.aclose()
                raise HTTPException(
                    status_code=502,
                    detail={"error": {"message": "codex upstream returned non-JSON",
                                      "type": "bad_gateway"}},
                )
            await gate.resp.aclose()
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
                "note": f"{plan} 套餐 · 额度随请求实时扣减（无公开查询端点）",
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

    非流式：全量解析 JSON，``error`` 节点按码分类。流式：缓冲原始 SSE 行
    直到第一条能定性的事件——语义已至即 committed（缓冲行随透传补放，客户端
    无损）；``error``/``response.failed`` 判账号级则换号；语义事件前断流按
    eof 处理（换号，防「200 空流假成功」）。
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
