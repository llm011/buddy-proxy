"""千问办公（QwenWorkCN）provider：MaaS 直连，双协议原生透传。

## 上游能力（全部实测）

| 能力 | 状态 | 说明 |
|---|---|---|
| OpenAI ``/v1/chat/completions`` | ✅ | 非流式 + 流式 |
| Anthropic ``/v1/messages`` | ✅ | **原生支持**，连 ``thinking`` 块都给 |
| 工具调用 | ✅ | OpenAI ``tool_calls`` / Anthropic ``tool_use`` |
| ``usage`` | ✅ | 含 ``prompt_tokens_details.cached_tokens`` |
| 模型目录 | ❌ | 上游没有 ``/v1/models`` |

因为是**原生双协议**，本 provider 不做任何协议转换——拿到请求体直接转发，
把响应原样回给客户端。这比 kiro 那条通道简单得多（那边要自己拼
``conversationState``、解析二进制 EventStream）。

## 模型目录从哪来

上游没给 models 接口，模型目录有两处来源：

1. **本地静态表**（:data:`FALLBACK_MODELS`）——保证开箱可用
2. App 的加密目录缓存 ``~/.qwenworkcn/.models/*/catalog-*``——**编码方式不是
   safeStorage**（``UU1D`` 开头的自定义序列化），解不开，所以不用

UI 上显示 ``Qwen3.8-Max``，但 API 只认全小写 ``qwen3.8-max``（踩过：大小写
不对回 ``invalid_request``，且不提示"模型不存在"）。对外 id 统一小写，
同时接受大小写混写的别名。

## 鉴权

见 :mod:`.credentials` —— OAuth JWT → mint token → 自刷新，全程不碰 Keychain。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from . import credentials as creds
from ..core.errors import describe_exception
from ..providers.base import BaseProvider

log = logging.getLogger(__name__)

#: 已知模型（上游无目录接口，这里是兜底表）。
#:
#: ``credits`` 是相对倍率（``qwen3.8-max`` = 1.0 为基准），仅供 UI 参考——
#: 上游不提供这个数据，是从界面里观察到的档位（Max > Plus > Flash）。
FALLBACK_MODELS: tuple[dict[str, Any], ...] = (
    # 每个系列**只留最新版**（用户 2026-11-11 要求）：有 glm-5.3 就不要 glm-5.2。
    # 上游确实同时提供旧版，但列出来只会让模型选择器变长、还容易选错。
    #
    # ---- Qwen 系 ----
    {"id": "qwen3.8-max", "name": "Qwen3.8-Max", "credits": 1.0, "images": True},
    {"id": "qwen3.8-flash", "name": "Qwen3.8-Flash", "credits": 0.1, "images": True},
    # ---- GLM 系（智谱）----
    {"id": "glm-5.3", "name": "GLM-5.3", "credits": 0.6},
    {"id": "glm-5.3-flash", "name": "GLM-5.3-Flash", "credits": 0.1},
    # ---- DeepSeek 系 ----
    {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro", "credits": 0.8},
    {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash", "credits": 0.1},
    # ---- Kimi 系（月之暗面）----
    {"id": "kimi-k3.1-pro", "name": "Kimi K3.1 Pro", "credits": 1.0},
    {"id": "kimi-k3.1-pro-highspeed", "name": "Kimi K3.1 Pro (Highspeed)", "credits": 1.2},
)

_TIMEOUT = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=15.0)

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "close"}

#: 协议 → 上游路径。两条都是原生支持，直接透传。
_PROTOCOL_PATHS = {
    "openai": "/v1/chat/completions",
    "anthropic": "/v1/messages",
}


def provider_models() -> list[dict[str, Any]]:
    """对外模型列表（OpenAI /v1/models 元素形状）。"""
    out = []
    for item in FALLBACK_MODELS:
        model = {
            "id": item["id"],
            "object": "model",
            "created": 0,
            "owned_by": "qwenpc",
            "name": item.get("name") or item["id"],
        }
        if "images" in item:
            model["images"] = item["images"]
        if "credits" in item:
            model["credits"] = item["credits"]
        out.append(model)
    return out


def resolve_model(model: str) -> str:
    """客户端给的模型名 → 上游名。

    上游**只认全小写**（``qwen3.8-max``）。UI 显示的是 ``Qwen3.8-Max``，用户很
    可能直接抄过来——所以这里统一 ``lower()``，而不是报错。带 ``qwenpc/``
    前缀也剥掉。
    """
    want = (model or "").strip()
    if "/" in want:
        prefix, _, rest = want.partition("/")
        if prefix == "qwenpc" and rest:
            want = rest
    return want.lower()


class QwenpcProvider(BaseProvider):
    """千问办公（QwenWorkCN）桌面上游的 MaaS 通道。"""

    id = "qwenpc"
    name = "QwenWork (千问办公)"

    #: 与其它聚合/网关型通道同档：裸名撞车时让位给原生通道。
    route_priority = 50

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    # -- 目录 ---------------------------------------------------------------

    def models(self) -> Sequence[dict[str, Any]]:
        return provider_models()

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        want = (model or "").strip()
        if not want:
            return False
        bare = want.removeprefix(f"{self.id}/").lower()
        return any(str(m["id"]) == bare for m in self.models())

    # -- 认证 ---------------------------------------------------------------

    def ensure_auth(self) -> None:
        """不主动连 App——只检查有没有缓存/凭证来源。

        真正的取 token 放在 ``forward`` 里按需做：``ensure_auth`` 在启动阶段
        被调用，那时去抓 JWT 会**在启动时拉起千问 App**，这个副作用太重。
        """
        return None

    def health(self) -> dict[str, Any]:
        cached = creds._load_cache()  # noqa: SLF001 - 同包内直接用
        expires = cached.get("expires_at")
        return {
            "id": self.id,
            "name": self.name,
            "models": [str(m["id"]) for m in provider_models()],
            "cached_token": bool(cached.get("token")),
            "token_expires_in_s": (
                int(float(expires) - time.time())
                if isinstance(expires, (int, float)) else None),
        }

    # -- 转发 ---------------------------------------------------------------

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        path = _PROTOCOL_PATHS.get(protocol)
        if path is None:
            raise HTTPException(status_code=501, detail={"error": {
                "message": (
                    f"qwenpc 不支持 {protocol} 协议。"
                    "请改用 openai（/v1/chat/completions）或 "
                    "anthropic（/v1/messages）"),
                "type": "unsupported_protocol"}})

        stream = bool(body.get("stream", False))

        # anthropic 必须用 original：routes 层已把 anthropic 转成 chat 形态，
        # 拿转过的 body 去打 /v1/messages 上游会 400（与 yuntoken 同一个坑）。
        if protocol == "anthropic":
            source = original if isinstance(original, dict) and original else body
        else:
            source = body
        upstream_body = {k: v for k, v in source.items() if not k.startswith("_")}
        # model 一律以 body 为准：original 里是客户端原话（可能带 qwenpc/ 前缀
        # 或大小写混写），上游只认小写裸名。
        upstream_body["model"] = resolve_model(str(body.get("model") or ""))
        upstream_body["stream"] = stream

        try:
            token = creds.auth_token()
        except creds.CredentialError as exc:
            raise HTTPException(status_code=401, detail={"error": {
                "message": str(exc), "type": "authentication_error"}}) from exc

        base = creds.api_base()
        url = f"{base.rstrip('/')}{path}"
        client = await self._get_client()
        try:
            request = client.build_request(
                "POST", url, json=upstream_body,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json",
                         "Accept": ("text/event-stream" if stream
                                    else "application/json")})
            resp = await client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            log.warning("qwenpc upstream timeout: %s", describe_exception(exc))
            raise HTTPException(status_code=504, detail={"error": {
                "message": "qwenpc upstream timeout", "type": "timeout"}}) from exc
        except httpx.HTTPError as exc:
            log.warning("qwenpc upstream error: %s", describe_exception(exc))
            raise HTTPException(status_code=502, detail={"error": {
                "message": "qwenpc upstream error",
                "type": "bad_gateway"}}) from exc

        if resp.status_code >= 400:
            # 错误体先读完再关：aclose() 会丢弃未读 body
            await resp.aread()
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                payload = {"error": {"message": f"HTTP {resp.status_code}"}}
            finally:
                await resp.aclose()
            # token 失效（401）：清缓存，下次请求会重新 mint。
            if resp.status_code == 401:
                log.warning("qwenpc token 被拒，清除缓存以便下次重新 mint")
                creds.clear_cache()
            raise HTTPException(status_code=resp.status_code, detail=payload)

        if stream:
            return StreamingResponse(
                self._passthrough(client, resp),
                media_type="text/event-stream", headers=_SSE_HEADERS)

        await resp.aread()
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail={"error": {
                "message": "qwenpc upstream returned non-JSON",
                "type": "bad_gateway"}}) from exc
        finally:
            await resp.aclose()
        return JSONResponse(content=payload)

    async def _passthrough(self, client: httpx.AsyncClient,
                           resp: httpx.Response) -> AsyncIterator[bytes]:
        """流式原样透传（上游已经是标准 SSE，不需要重组）。"""
        try:
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await resp.aclose()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client
