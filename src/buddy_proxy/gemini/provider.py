"""Gemini CLI provider —— cloudcode-pa 免费通道转发。

上游是 Gemini 原生协议（v1internal 包装），不是 OpenAI/Anthropic 形态，
所以与 mimo/zcode 的「直通」模式不同：请求和响应都要双向转换。

- openai / responses：请求 chat_to_gemini_request，非流式响应
  gemini_response_to_chat；流式把上游 SSE 的每个 ``response`` 转成 OpenAI
  chunk（含 tool_calls 增量、reasoning、usage）。
- anthropic：路由层传入的 body 已是 chat 格式，复用 openai 链路出 chunk，
  再经 ``AnthropicStreamConverter`` 包成 Anthropic 事件流（与 mimo 同款）；
  非流式走 ``chat_completion_to_anthropic_message``。

认证：OAuth（buddy login gemini），access_token 过期自动刷新（401 重试一次）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..providers.base import BaseProvider
from .convert import (
    chat_to_gemini_request,
    gemini_response_to_chat,
    inline_image_part,
    new_tool_call,
    usage_from_metadata,
)
from .credentials import AuthError, ensure_access_token, has_cred, load_cred

log = logging.getLogger(__name__)

_CODE_ASSIST_BASE = "https://cloudcode-pa.googleapis.com"
_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0)

#: 模型表放 JSON（models.json，随包分发）：实测要增删模型（preview 免费层
#: 可用性等）改文件就行，不动代码。字段：id/description/context/max_output。
_MODELS_JSON = Path(__file__).with_name("models.json")


def _load_models() -> dict[str, str]:
    try:
        data = json.loads(_MODELS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("gemini models.json 读取失败（%s），使用内置兜底表", exc)
        return {"gemini-2.5-flash": "Gemini 2.5 Flash (fallback)"}
    out: dict[str, str] = {}
    for item in data.get("models") or []:
        if isinstance(item, dict) and item.get("id"):
            out[str(item["id"])] = str(item.get("description") or item["id"])
    return out or {"gemini-2.5-flash": "Gemini 2.5 Flash (fallback)"}


DEFAULT_MODELS: dict[str, str] = _load_models()

#: 免费 tier 已知配额（社区实测，Google 官方文档未固化）：每分钟请求数 /
#: 每日请求数。仅用于管理页展示，不做本地限流。
_FREE_TIER_QUOTA_NOTE = "free: 2.5-pro 100 req/day, flash 系 250 req/day"


def _run_sync(factory):
    """协程工厂 → 独立事件循环执行（/ui 线程安全，见 mimo/provider._run_sync）。"""
    import concurrent.futures

    def _runner():
        return asyncio.run(factory())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _runner()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_runner).result(timeout=30)


def _upstream_error_response(resp: httpx.Response) -> JSONResponse:
    try:
        payload = resp.json()
    except Exception:
        payload = {"error": {"message": resp.text[:500], "type": "upstream_error"}}
    return JSONResponse(status_code=resp.status_code, content=payload)


async def _pass_through_stream(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk
    finally:
        await response.aclose()


class GeminiProvider(BaseProvider):
    #: 通道标识带 -cli 后缀：与「gemini 原生 API key / vertex」类通道区分开，
    #: /v1/models 里模型前缀即 ``gemini-cli/gemini-2.5-flash``。
    id = "gemini-cli"
    name = "Gemini CLI (Code Assist free tier)"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    # ---- BaseProvider 接口 ----

    def models(self) -> Sequence[dict[str, Any]]:
        return [
            {
                "id": mid,
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "description": desc,
            }
            for mid, desc in DEFAULT_MODELS.items()
        ]

    def ensure_auth(self) -> None:
        if not has_cred():
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": "gemini 未登录：请先运行 `buddy login gemini`",
                        "type": "authentication_error",
                    }
                },
            )

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        self.ensure_auth()
        stream = bool(body.get("stream", False))
        model = str(body.get("model") or "gemini-2.5-flash").removeprefix(f"{self.id}/")
        upstream_model = model if model in DEFAULT_MODELS else "gemini-2.5-flash"

        # token 刷新是同步 urllib；丢线程池避免卡事件循环
        access_token = await asyncio.to_thread(_token_or_raise)

        cred = load_cred() or {}
        project_id = str(cred.get("project_id") or "")
        if not project_id:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": "gemini 凭据缺 project_id，请重新 `buddy login gemini`",
                        "type": "authentication_error",
                    }
                },
            )

        upstream_body = chat_to_gemini_request(
            body, project_id=project_id, model=upstream_model, stream=stream
        )
        method = "streamGenerateContent" if stream else "generateContent"
        url = f"{_CODE_ASSIST_BASE}/v1internal:{method}"
        if stream:
            url += "?alt=sse"

        from .fingerprint import auth_headers

        headers = auth_headers(access_token, upstream_model, stream)
        client = await self._get_client()
        req = client.build_request(
            "POST", url, json=upstream_body, headers=headers
        )
        try:
            resp = await client.send(req, stream=stream)
        except httpx.TimeoutException as exc:
            log.warning("gemini upstream timeout: %s", exc)
            raise HTTPException(
                status_code=504,
                detail={"error": {"message": "gemini upstream timeout", "type": "timeout"}},
            ) from exc
        except httpx.HTTPError as exc:
            log.warning("gemini upstream error: %s", exc)
            raise HTTPException(
                status_code=502,
                detail={"error": {"message": "gemini upstream error", "type": "bad_gateway"}},
            ) from exc

        # 401：access_token 失效（比如文件被手工改过）——刷新重试一次
        if resp.status_code == 401:
            if stream:
                await resp.aread()
            await resp.aclose()
            access_token = await asyncio.to_thread(_refresh_or_raise)
            headers = auth_headers(access_token, upstream_model, stream)
            req = client.build_request("POST", url, json=upstream_body, headers=headers)
            try:
                resp = await client.send(req, stream=stream)
            except httpx.HTTPError as exc:
                log.warning("gemini upstream error after refresh: %s", exc)
                raise HTTPException(
                    status_code=502,
                    detail={"error": {"message": "gemini upstream error", "type": "bad_gateway"}},
                ) from exc

        if resp.status_code >= 400:
            if stream:
                await resp.aread()
            try:
                return _upstream_error_response(resp)
            finally:
                await resp.aclose()

        if stream:
            if protocol == "anthropic":
                return StreamingResponse(
                    _to_anthropic_stream(resp, upstream_model),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "close"},
                )
            return StreamingResponse(
                _to_openai_stream(resp, upstream_model),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "close"},
            )

        try:
            payload = resp.json()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"error": {"message": "gemini upstream returned non-JSON", "type": "bad_gateway"}},
            ) from exc
        chat = gemini_response_to_chat(payload, model=upstream_model)
        if protocol == "anthropic":
            from ..protocols.anthropic_adapter import chat_completion_to_anthropic_message

            chat = chat_completion_to_anthropic_message(chat, original)
        return JSONResponse(content=chat)

    # ---- 内部 ----

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client

    def health(self) -> dict[str, Any]:
        cred = load_cred() or {}
        return {
            "id": self.id,
            "name": self.name,
            "configured": has_cred(),
            "email": cred.get("email") or "",
            "project_id": cred.get("project_id") or "",
            "tier": cred.get("tier") or "",
            "models": list(DEFAULT_MODELS),
        }

    def quota(self) -> dict[str, Any] | None:
        """免费额度：上游没有用量查询接口，展示静态说明条目。

        retrieveUserQuota 在真 CLI 里只对标准/企业 tier 有意义；free tier
        的每/日计数服务端不回查接口，只能看 429。这里给管理页一条提示信息
        （量纲：条数），不画假进度条。
        """
        if not has_cred():
            return None
        cred = load_cred() or {}
        return {
            "items": [
                {
                    "label": _FREE_TIER_QUOTA_NOTE,
                    "used": None,
                    "total": None,
                    "remaining": None,
                    "percent": None,
                    "reset_ts": None,   # 免费层按天/分钟限流，与权益到期无关
                    "expire_ts": None,
                    "unit": None,
                }
            ],
            "level": str(cred.get("tier_name") or cred.get("tier") or "free-tier"),
        }


# ---------------------------------------------------------------------------
# SSE → OpenAI / Anthropic 流
# ---------------------------------------------------------------------------

def _token_or_raise() -> str:
    try:
        return ensure_access_token()
    except AuthError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": str(exc), "type": "authentication_error"}},
        ) from exc


def _refresh_or_raise() -> str:
    from .credentials import refresh_cred

    cred = load_cred()
    if cred is None:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "gemini 未登录", "type": "authentication_error"}},
        )
    try:
        cred = refresh_cred(cred)
    except AuthError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": f"刷新凭据失败: {exc}", "type": "authentication_error"}},
        ) from exc
    return cred["access_token"]


def _sse_event(name: str, payload: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _openai_chunk(
    model: str,
    *,
    delta: dict[str, Any] | None = None,
    finish: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    choice: dict[str, Any] = {"index": 0, "delta": delta or {}}
    if finish:
        choice["finish_reason"] = finish
    chunk = {
        "id": f"chatcmpl-{secrets.token_hex(12)}",
        "object": "chat.completion.chunk",
        "created": int(_now()),
        "model": model,
        "choices": [choice],
    }
    if usage:
        chunk["usage"] = usage
    return chunk


def _now() -> float:
    return time.time()


class _ToolCallBuffer:
    """跨 SSE chunk 聚合 functionCall（上游一次给全量，这里仍按增量协议发）。"""

    def __init__(self) -> None:
        self.emitted = 0

    def feed(self, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deltas: list[dict[str, Any]] = []
        for i, call in enumerate(calls):
            idx = self.emitted + i
            fn = call["function"]
            deltas.append(
                {
                    "index": idx,
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": fn["name"], "arguments": fn["arguments"]},
                    "gemini_thought_signature": call.get("gemini_thought_signature") or "",
                }
            )
        self.emitted += len(calls)
        return deltas


async def _iter_upstream_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """上游 SSE → 内部事件（response dict 流）。"""
    async for payload in _sse_lines(response):
        inner = payload.get("response") if isinstance(payload.get("response"), dict) else payload
        if inner:
            yield inner


async def _sse_lines(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    try:
        async for line in response.aiter_lines():
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
    finally:
        await response.aclose()


async def _to_openai_stream(response: httpx.Response, model: str) -> AsyncIterator[str]:
    """上游 Gemini SSE → OpenAI chat.completion.chunk SSE。"""

    def _dump(chunk: dict[str, Any]) -> str:
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    yield _dump(_openai_chunk(model, delta={"role": "assistant", "content": ""}))
    buf = _ToolCallBuffer()
    usage: dict[str, Any] | None = None
    finish: str | None = None
    try:
        async for inner in _iter_upstream_events(response):
            usage_node = inner.get("usageMetadata")
            if isinstance(usage_node, dict):
                usage = usage_from_metadata(usage_node)
            candidates = inner.get("candidates") or []
            cand = candidates[0] if candidates else {}
            parts = (cand.get("content") or {}).get("parts") or []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if "functionCall" in part:
                    call = new_tool_call(
                        part["functionCall"] or {},
                        signature=str(part.get("thoughtSignature") or ""),
                    )
                    for delta in buf.feed([call]):
                        yield _dump(_openai_chunk(model, delta={"tool_calls": [delta]}))
                elif "inlineData" in part or "inline_data" in part:
                    # 生图模型流式返回的图片（与 gemini_response_to_chat 同形）
                    image = inline_image_part(part)
                    if image:
                        yield _dump(_openai_chunk(model, delta={"images": [image]}))
                elif part.get("thought") is True and part.get("text"):
                    yield _dump(_openai_chunk(model, delta={"reasoning_content": part["text"]}))
                elif "text" in part and part.get("text"):
                    yield _dump(_openai_chunk(model, delta={"content": part["text"]}))
            raw_finish = str(cand.get("finishReason") or "")
            if raw_finish:
                finish = {
                    "STOP": "stop",
                    "MAX_TOKENS": "length",
                    "SAFETY": "content_filter",
                    "RECITATION": "content_filter",
                }.get(raw_finish, "stop")
                if buf.emitted and finish == "stop":
                    finish = "tool_calls"
    except Exception as exc:  # noqa: BLE001 - 半截流不如补一个错误再收尾
        log.warning("gemini openai stream aborted: %s: %s", type(exc).__name__, exc)
        yield _dump(_openai_chunk(model, delta={}, finish="stop"))
        yield "data: [DONE]\n\n"
        return
    yield _dump(_openai_chunk(model, delta={}, finish=finish or "stop", usage=usage))
    yield "data: [DONE]\n\n"


async def _to_anthropic_stream(response: httpx.Response, model: str) -> AsyncIterator[str]:
    """上游 Gemini SSE → Anthropic 事件流（复用全局转换器，与 mimo 一致）。"""
    from ..protocols.anthropic_adapter import AnthropicStreamConverter

    converter = AnthropicStreamConverter(model)

    def _emit(name: str, payload: dict[str, Any]) -> str:
        return _sse_event(name, payload)

    def _abort(msg: str) -> list[str]:
        out: list[str] = []
        try:
            out = [_emit(n, p) for n, p in converter.close_open_blocks()]
        except Exception:  # noqa: BLE001
            pass
        out.append(_emit("error", {
            "type": "error",
            "error": {"type": "api_error", "message": msg},
        }))
        return out

    # 复用 openai 流逻辑生成 chunk，再喂给 AnthropicStreamConverter
    async def _chat_chunks() -> AsyncIterator[dict[str, Any]]:
        async for line in _to_openai_stream(response, model):
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
            if isinstance(chunk, dict):
                yield chunk

    try:
        async for chunk in _chat_chunks():
            if chunk.get("error"):
                err = chunk["error"]
                msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
                for event in _abort(msg):
                    yield event
                return
            for name, payload in converter.feed_chunk(chunk):
                yield _emit(name, payload)
        for name, payload in converter.finish():
            yield _emit(name, payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("gemini anthropic stream aborted: %s: %s", type(exc).__name__, exc)
        for event in _abort(f"{type(exc).__name__}: {exc}"):
            yield event
        return
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# 冒烟自测：python -m buddy_proxy.gemini.provider
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    p = GeminiProvider()
    print("health:", json.dumps(p.health(), ensure_ascii=False))
    if not has_cred():
        print("未登录：先跑 buddy login gemini")
        raise SystemExit(1)
    resp = asyncio.run(p.forward(
        {
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "只回复两个字：pong"}],
            "stream": False,
            "max_tokens": 32,
        },
        "openai",
    ))
    print("status:", resp.status_code)
    print(str(resp.body)[:400])
