"""火山方舟（火山引擎 Ark）provider —— ``ark`` 渠道。

上游是火山方舟的模型接入点，两个端点**同时可用**（2026-10-23 实测）：

- ``POST {base}/responses``（OpenAI Responses 协议，SSE）
- ``POST {base}/chat/completions``（OpenAI Chat 协议，SSE）

默认打 ``/responses``：它的 ``response.completed.response.usage`` 带
``cached_tokens`` / ``reasoning_tokens`` 明细，且事件流里 reasoning summary
是独立事件，折叠回 chat 后信息更全。若某个接入点不支持 responses，``auto``
模式会自动降级到 ``/chat/completions``（见 :meth:`ArkProvider._pick_upstream`）。

**模型名与 key 一律不进代码**——火山方舟按「自定义推理接入点」（``ep-...``）
计费，接入点 id 是账号私有资源；裸模型名在这种账号上反而被拒：

    {"error": {"code": "InvalidEndpointOrModel.ModelIDAccessDisabled",
               "message": "Accessing the model via Model ID is not allowed for
                           your account. Please use a custom endpoint ID instead."}}

所以本通道的模型表**完全来自本地配置**，代码里没有任何默认模型。

配置（两个文件都在 ``~/.buddy-proxy/``，随 ``BUDDY_PROXY_STATE_DIR`` 迁移）：

1. ``ark_api_key`` —— 首行裸 key（或 ``ARK_API_KEY=...``），文件 0600；
   环境变量 ``ARK_API_KEY`` 优先。
2. ``ark.json`` —— 模型表::

       {
         "base_url": "https://ark.cn-beijing.volces.com/api/v3",
         "models": [
           {"id": "deepseek-v4.1-flash", "endpoint": "ep-xxxxxxxx",
            "name": "DeepSeek V4.1 Flash"}
         ]
       }

   - ``id``：**对外开放的模型名**（必填、唯一），客户端用 ``ark/<id>`` 或裸名；
   - ``endpoint``：上游接入点 ``ep-...``（与 ``model`` 二选一）；
   - ``model``：账号允许直调裸模型名时用这个字段（``endpoint`` 的等价写法）；
   - ``base_url``：可省，缺省北京区；环境变量 ``ARK_BASE_URL`` 优先；
   - 其余键（``name`` / ``description`` / ``max_input`` / ``max_output`` /
     ``context`` / ``tool_call`` / ``reasoning``）原样进 ``/v1/models``。

   文件缺失或 JSON 损坏时模型表为空而**不抛**（与 ``codex/catalog`` 同口径）：
   启动阶段不该因为一份可选配置文件炸掉整个代理；真正的可操作提示在
   需要时由 :meth:`ArkProvider.ensure_auth` 给出。

协议路由（三种客户端协议全支持，规则与 dumate/kimi 一致）：

- ``openai``（``/v1/chat/completions``）→ 两个上游都归一到 chat chunk；
- ``responses``（``/v1/responses``）→ 上游本来就是 responses 时**原样透传**；
  降级到 chat 时用 ``protocols.responses_adapter.ResponsesStreamConverter`` 转回；
- ``anthropic``（``/v1/messages``）→ 归一到 chat 后交给
  ``protocols.anthropic_adapter.sse_to_anthropic_stream``（与 dumate 共用同一份
  事件机），``reasoning_content`` → thinking 块、``tool_calls`` → tool_use 块，
  Claude Code 等客户端可直接使用。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Iterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .base import BaseProvider
from .zcode import _load_secret_file_impl
from ..core.errors import describe_exception
from ..core.paths import state_file

log = logging.getLogger(__name__)

#: 火山方舟默认基址（北京区）。``ARK_BASE_URL`` 或 ``ark.json`` 的 ``base_url``
#: 可整体覆盖（如新加坡区 ``ark.ap-southeast-1.volces.com``）。
DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

#: 上游协议选择：``auto``（默认，responses 优先、遇不支持自动降级）/ ``responses``
#: / ``chat``。强制指定时不做降级——把配置写死的人通常是在绕某个上游缺陷，
#: 悄悄换协议会让「为什么我的参数没生效」变成悬案。
PROTOCOL_ENV = "ARK_UPSTREAM_PROTOCOL"

#: 流式读超时：推理模型首 token 可能很慢（实测 reasoning summary 前有数秒静默），
#: 与 codex 取同档。
_TIMEOUT = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=15.0)
#: 非流式聚合的总时长上限（上游持续慢速吐帧时兜底）。
_AGGREGATE_DEADLINE_S = 600.0

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "close"}


# ---------------------------------------------------------------------------
# 本地配置
# ---------------------------------------------------------------------------


def secret_file_path() -> Path:
    """本渠道的 key 文件：``~/.buddy-proxy/ark_api_key``。"""
    return state_file("ark_api_key")


def config_file_path() -> Path:
    """本渠道的模型表：``~/.buddy-proxy/ark.json``。"""
    return state_file("ark.json")


def resolve_api_key() -> str:
    """解析 API key：环境变量 ``ARK_API_KEY`` > ``ark_api_key`` 文件。"""
    key = os.environ.get("ARK_API_KEY", "").strip()
    if key:
        return key
    return _load_secret_file_impl(secret_file_path())


def resolve_base_url(configured: str = "") -> str:
    """基址优先级：``ARK_BASE_URL`` > ``ark.json`` 的 ``base_url`` > 北京区默认值。"""
    for raw in (os.environ.get("ARK_BASE_URL", ""), configured):
        text = (raw or "").strip().rstrip("/")
        if text:
            return text
    return DEFAULT_BASE_URL


def _upstream_protocol() -> str:
    """``ARK_UPSTREAM_PROTOCOL`` 归一：只认三个值，其余按 ``auto`` 处理。"""
    value = os.environ.get(PROTOCOL_ENV, "").strip().lower()
    return value if value in ("responses", "chat") else "auto"


def _entry_upstream_name(entry: dict[str, Any]) -> str:
    """配置条目 → 上游真名（``ep-...`` 或裸模型名）。``endpoint`` 优先。"""
    return str(entry.get("endpoint") or entry.get("model") or "").strip()


def load_config() -> dict[str, Any]:
    """读 ``ark.json`` 原始内容；缺失/损坏都返回空 dict，绝不抛。

    容错口径与 ``codex/catalog.load_models_file`` 一致：一份**可选**的本地配置
    不该在 import 期或启动期把整个代理带下线。坏文件会打一条 warning，让
    「模型列表是空的」有迹可循。
    """
    path = config_file_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("ark: 读取 %s 失败: %s", path, exc)
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("ark: %s 不是合法 JSON（%s），模型表为空", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def parse_models(config: dict[str, Any]) -> list[dict[str, Any]]:
    """``ark.json`` → 开源模型条目列表（已校验、已去重）。

    无效条目逐个跳过而非整体失败——一条写错不该让其它可用模型一起消失。
    ``id`` 缺失/非字符串/重复、``endpoint`` 与 ``model`` 都缺，都算无效。
    """
    raw_models = config.get("models")
    if not isinstance(raw_models, list):
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        model_id = str(raw.get("id") or "").strip()
        upstream = _entry_upstream_name(raw)
        if not model_id or not upstream or model_id in seen:
            continue
        seen.add(model_id)
        entry: dict[str, Any] = {
            "id": model_id,
            "upstream": upstream,
            "name": str(raw.get("name") or model_id),
            "description": str(raw.get("description") or ""),
        }
        for key in ("max_input", "max_output", "context"):
            value = raw.get(key)
            if isinstance(value, int) and value > 0:
                entry[key] = value
        for key in ("tool_call", "reasoning", "images"):
            if isinstance(raw.get(key), bool):
                entry[key] = raw[key]
        out.append(entry)
    return out


def _harden_config_permissions() -> None:
    """``ark.json`` 若存在则收成 0600。

    它本身不含 key，但会把账号的接入点 id 暴露出来——接入点能反查出模型与
    计费主体。只在文件存在时调整，不创建（用户可能压根没配）。
    """
    path = config_file_path()
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class ArkProvider(BaseProvider):
    """火山方舟（火火山引擎 Ark）模型接入点通道。"""

    id = "ark"
    name = "火山方舟 (Volcengine Ark)"

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self._api_key = api_key if api_key is not None else resolve_api_key()
        config = load_config()
        self._base = resolve_base_url(str(config.get("base_url") or ""))
        if base_url:
            self._base = base_url.rstrip("/")
        self._entries = parse_models(config)
        self._client: httpx.AsyncClient | None = None
        #: 降级记忆：``auto`` 模式下探测到 responses 端点不可用后置位，
        #: 后续请求直接走 chat，不再每条请求都白撞一次 404。
        self._responses_unsupported = False
        #: 上游事件里学到的底层模型真名（接入点后面托管的是哪个平台模型，
        #: 配置里写不出来）。只做 model 字段改写，不参与路由。
        self._observed_upstream_models: set[str] = set()
        _harden_config_permissions()

    # ---- BaseProvider 接口 ----

    def models(self) -> Sequence[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for entry in self._entries:
            item: dict[str, Any] = {
                "id": entry["id"],
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "name": entry["name"],
                "description": entry["description"] or f"火山方舟接入点 {entry['upstream']}",
            }
            for key in ("max_input", "max_output", "context",
                        "tool_call", "reasoning", "images"):
                if key in entry:
                    item[key] = entry[key]
            out.append(item)
        return out

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        want = (model or "").strip().removeprefix(f"{self.id}/")
        return any(entry["id"] == want for entry in self._entries)

    def resolve_model(self, model: str) -> str | None:
        want = (model or "").strip().removeprefix(f"{self.id}/")
        found = next((e for e in self._entries if e["id"] == want), None)
        return found["id"] if found else None

    def _upstream_names(self) -> list[str]:
        """本通道配过的全部上游名；只用于识别「这个 model 字段是我自己的」。"""
        return [e["upstream"] for e in self._entries]

    def ensure_auth(self) -> None:
        """启动/转发前校验：key 与模型表都得有，缺哪个就报哪个。"""
        if not self._api_key:
            self._api_key = resolve_api_key()
        if not self._api_key:
            raise HTTPException(
                status_code=401,
                detail={"error": {
                    "message": (
                        "ark 未配置认证：请把火山方舟 API key 写入 "
                        f"{secret_file_path()}（首行裸 key，0600），"
                        "或设置环境变量 ARK_API_KEY"
                    ),
                    "type": "authentication_error",
                }},
            )
        if not self._entries:
            raise HTTPException(
                status_code=401,
                detail={"error": {
                    "message": (
                        "ark 未配置模型：请在 "
                        f"{config_file_path()} 里填写模型表，形如 "
                        '{"models": [{"id": "deepseek-v4.1-flash", '
                        '"endpoint": "ep-xxxxxxxx"}]}。'
                        "火山方舟的自定义推理接入点 id（ep-...）属于账号私有资源，"
                        "不能写死在代码里"
                    ),
                    "type": "configuration_error",
                }},
            )

    def health(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self._base,
            "configured": bool(self._api_key) and bool(self._entries),
            "models": len(self._entries),
            "upstream_protocol": self._protocol_label(),
        }

    # ---- 内部 ----

    def _protocol_label(self) -> str:
        """health 展示用的实际上游协议。"""
        requested = _upstream_protocol()
        if requested != "auto":
            return requested
        return "chat" if self._responses_unsupported else "responses"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client

    def _entry_for(self, model: str) -> dict[str, Any]:
        want = (model or "").strip().removeprefix(f"{self.id}/")
        entry = next((e for e in self._entries if e["id"] == want), None)
        if entry is None:
            known = ", ".join(e["id"] for e in self._entries) or "（模型表为空）"
            raise HTTPException(
                status_code=400,
                detail={"error": {
                    "message": f"ark 未知模型: {model}；已配置: {known}",
                    "type": "invalid_request_error",
                }},
            )
        return entry

    def _headers(self, stream: bool) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }

    def _upstream_body(self, body: dict[str, Any], *, upstream: str,
                       protocol: str) -> dict[str, Any]:
        """chat body → 上游请求体（两种上游形态各自规整）。

        上游**恒以 stream=True 请求**：responses 非流式会把整条推理憋完才返回，
        首字节延迟不可控；chat 端点则需要我们自行聚合非流式。两条路径都在本地
        做非流式还原，行为一致。
        """
        if protocol == "responses":
            from ..codex.projection import chat_to_responses_request

            return chat_to_responses_request(body, model=upstream, stream=True)
        payload = {k: v for k, v in body.items() if not k.startswith("_")}
        payload["model"] = upstream
        payload["stream"] = True
        # 流式 usage 只在该端点显式要求时才回传（实测不开就一路 usage:null）；
        # 客户端自己带的值不覆盖——有人会特意关掉。
        options = payload.get("stream_options")
        if not isinstance(options, dict):
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _endpoint_url(self, protocol: str) -> str:
        suffix = "responses" if protocol == "responses" else "chat/completions"
        return f"{self._base}/{suffix}"

    # ---- 转发 ----

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        self.ensure_auth()
        entry = self._entry_for(str(body.get("model") or ""))
        upstream_model = entry["upstream"]
        # 回给客户端的模型名永远是**对外名**：anthropic 的 message_start.model、
        # responses 的 response.model 都会原样透出，回 ep-... 会让客户端以为
        # 自己连的是另一个模型。别名集合按本通道全部上游名算——接入点后面
        # 托管的是哪个底层模型，只有上游事件自己说，配置里写不出来。
        client_model = f"{self.id}/{entry['id']}"
        own_aliases = _model_aliases(upstream_model)
        stream = bool(body.get("stream", False))

        upstream_protocol = await self._pick_upstream(body, upstream_model, stream)
        client = await self._get_client()
        url = self._endpoint_url(upstream_protocol)
        payload = self._upstream_body(body, upstream=upstream_model,
                                      protocol=upstream_protocol)

        try:
            request = client.build_request(
                "POST", url, json=payload, headers=self._headers(stream=True))
            resp = await client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            log.warning("ark upstream timeout: %s", describe_exception(exc))
            raise HTTPException(status_code=504, detail={
                "error": {"message": "ark upstream timeout", "type": "timeout"}
            }) from exc
        except httpx.HTTPError as exc:
            log.warning("ark upstream error: %s", describe_exception(exc))
            raise HTTPException(status_code=502, detail={
                "error": {"message": "ark upstream error", "type": "bad_gateway"}
            }) from exc

        if resp.status_code >= 400:
            # 错误体先读完再关：aclose() 会丢弃未读 body，之后拿不到上游真实
            # 错误（429 配额 / 401 key 无效都会变成空错误体）——zcode 踩过。
            await resp.aread()
            try:
                return _upstream_error_response(resp)
            finally:
                await resp.aclose()

        events = _iter_upstream_events(resp, upstream_protocol)

        # anthropic：统一走 chat 事件机再翻译（与 dumate 共用一份实现）。
        # 非流式必须走下面「本地聚合」那段——`/v1/messages` 非流式要的是一整个
        # message JSON，回 SSE 流客户端只会拿到一串事件（实测 Claude Code 直接
        # 报 JSON 解析失败）。
        if protocol == "anthropic" and stream:
            if upstream_protocol == "chat":
                return StreamingResponse(
                    _to_anthropic_from_chat(resp, client_model),
                    media_type="text/event-stream", headers=_SSE_HEADERS)
            chat_chunks = _responses_to_chat_stream(events, client_model)
            return StreamingResponse(
                _chat_chunks_to_anthropic(chat_chunks, client_model),
                media_type="text/event-stream", headers=_SSE_HEADERS)

        if stream:
            if upstream_protocol == "responses":
                if protocol == "responses":
                    # 上游原生 responses：只改写 model 字段，其余零转换。
                    return StreamingResponse(
                        _pass_through_stream(
                            resp, "responses",
                            own_aliases=own_aliases, client_model=client_model,
                            extra_model_names=self._observed_upstream_models),
                        media_type="text/event-stream", headers=_SSE_HEADERS)
                chunks = _responses_to_chat_stream(events, client_model, close=False)
                return StreamingResponse(
                    _chat_chunks_to_sse(chunks, resp),
                    media_type="text/event-stream", headers=_SSE_HEADERS)
            # 上游 chat
            if protocol == "responses":
                return StreamingResponse(
                    _chat_sse_to_responses(resp, client_model),
                    media_type="text/event-stream", headers=_SSE_HEADERS)
            return StreamingResponse(
                _pass_through_stream(resp, "chat"),
                media_type="text/event-stream", headers=_SSE_HEADERS)

        # ---- 非流式：本地聚合 ----
        try:
            if upstream_protocol == "responses":
                final = await _collect_responses(resp, events)
                from ..codex.projection import responses_to_chat_completion

                completion = responses_to_chat_completion(final, model=client_model)
            else:
                completion = await _collect_chat(resp, client_model)
        finally:
            await resp.aclose()

        if protocol == "responses":
            return JSONResponse(content=_chat_completion_to_response(
                completion, client_model, original))
        if protocol == "anthropic":
            from ..protocols.anthropic_adapter import (
                chat_completion_to_anthropic_message,
            )

            return JSONResponse(content=chat_completion_to_anthropic_message(
                completion, {**(original or {}), "model": client_model}))
        return JSONResponse(content=completion)

    async def _pick_upstream(self, body: dict[str, Any], upstream_model: str,
                             stream: bool) -> str:
        """决定打哪个上游端点，并在 ``auto`` 下完成一次性降级探测。

        responses 端点对**不支持的接入点**回的是明确的 ``404`` +
        ``InvalidEndpointOrModel*``（实测裸模型名 / 未开通的模型都是这个码）。
        只对这个组合降级：400/401/403/429 是「请求本身有问题」或「账号/额度
        问题」，换成 chat 端点只是把同一个错误再犯一遍——还会把真错误洗成
        另一种形状，让用户更难理解。
        """
        requested = _upstream_protocol()
        if requested == "chat":
            return "chat"
        if requested == "responses":
            return "responses"
        if self._responses_unsupported:
            return "chat"

        client = await self._get_client()
        payload = self._upstream_body(body, upstream=upstream_model,
                                      protocol="responses")
        try:
            request = client.build_request(
                "POST", self._endpoint_url("responses"),
                json=payload, headers=self._headers(stream=True))
            resp = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            # 探测失败不改变协议选择，让下面的正式请求去报准确错误。
            log.warning("ark responses 探测失败（沿用 responses）: %s",
                        describe_exception(exc))
            return "responses"

        is_endpoint_missing = (
            resp.status_code == 404
            and "InvalidEndpointOrModel" in await _peek_body(resp)
        )
        await resp.aclose()
        if not is_endpoint_missing:
            return "responses"
        self._responses_unsupported = True
        log.info("ark: 接入点 %s 不支持 /responses，本进程后续改走 /chat/completions",
                 upstream_model)
        return "chat"

    # ---- 额度 / 签到 ----

    def quota(self) -> dict[str, Any] | None:
        """火山方舟没有可匿名查询余额/用量的公开接口 —— 不出额度卡。

        返回 ``None`` 而不是编一条：编出来的数字会误导用户以为额度被追踪了。
        """
        return None


# ---------------------------------------------------------------------------
# 上游响应 → 三种客户端形状
# ---------------------------------------------------------------------------


async def _peek_body(resp: httpx.Response, limit: int = 2048) -> str:
    """读一小段错误体用于判别错误码，读后连接由调用方关闭。"""
    try:
        raw = await resp.aread()
    except Exception:  # noqa: BLE001
        return ""
    return raw[:limit].decode("utf-8", errors="replace")


def _upstream_error_response(resp: httpx.Response) -> JSONResponse:
    """透传上游状态码与错误体（上游本身就是 OpenAI 错误形状）。"""
    try:
        payload = resp.json()
    except Exception:
        payload = {"error": {"message": resp.text[:500], "type": "upstream_error"}}
    return JSONResponse(status_code=resp.status_code, content=payload)


async def _gate_inband_error(
    lines: AsyncIterator[str],
    upstream_protocol: str,
    own_aliases: frozenset[str] | None = None,
    client_model: str | None = None,
    extra_model_names: set[str] | None = None,
) -> AsyncIterator[str]:
    """首事件闸门的**字节侧**版本：原样透传行，但首帧是带内报错时抛异常。

    透传路径（openai 客户端 + chat 上游、responses 客户端 + responses 上游）
    不解析事件，所以 :func:`_iter_upstream_events` 的闸门在那儿根本不生效——
    上游 200 带 ``error`` 帧会被原样泵给客户端，正是「200 空流/假成功」那个坑。
    这里只嗅探错误帧，其余行一字不动地往下传。

    给了 ``own_aliases`` / ``client_model`` 时顺带把 ``model`` 字段改写成
    对外名：responses 透传会把上游真名（``ep-...``，或接入点背后那个平台
    托管模型的真名）原样吐给客户端，等于把本地映射白做了。
    """
    saw_event = False
    async for line in lines:
        # 空行是**帧分隔符**，必须先跳过：`"".strip().startswith("data:")` 为假
        # 没关系，但后面 `payload` 若被误置成 `{}` 会让改写逻辑认错对象。
        text = line.strip()
        if text.startswith("data:"):
            raw = text[len("data:"):].strip()
            payload: Any = None
            if raw and raw != "[DONE]":
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = None
            name = ""
            out = line
            if isinstance(payload, dict):
                name = str(payload.get("type") or "")
                # chat 上游的错误帧长这样：{"error": {...}}（没有 type=error）
                err = payload.get("error")
                if err:
                    message = (str(err.get("message") or err)
                               if isinstance(err, dict) else str(err))
                    raise HTTPException(status_code=502, detail={"error": {
                        "message": f"ark 上游错误: {message}",
                        "type": "upstream_error",
                    }})
            if (client_model and own_aliases and upstream_protocol == "responses"
                    and isinstance(payload, dict)):
                # 只在 responses 事件上改写：chat chunk 的 ``model`` 已经是上游
                # 配置名，客户端也不认，没必要动。改写与学习同一次完成——接入点
                # 背后托管的平台真名只可能出现在 ``model`` 叶子，学它安全；学完
                # 同一行即生效，客户端不会先看到一次真名。
                # 按路径白名单改写：tool call 的 arguments 字符串、工具结果回显里
                # 的同名字段一律不碰（逐行正则不认层级，会误伤）。
                out = line
                if _rewrite_model_payload(
                    payload, client_model, own_aliases,
                    _known_extra(extra_model_names), extra_model_names,
                ):
                    out = "data: " + json.dumps(payload, ensure_ascii=False)
            if not saw_event and upstream_protocol == "responses":
                status, message = _inband_error(name, payload)
                if status or message:
                    raise HTTPException(status_code=status or 502, detail={"error": {
                        "message": f"ark 上游错误: {message}" if message
                        else "ark 上游返回错误事件",
                        "type": "upstream_error",
                    }})
            saw_event = True
            line = out
        # iter_sse_lines 剥掉行尾，这里补回 "\n"：SSE 以空行分帧，丢换行
        # 客户端会读到一整块没有分隔符的垃圾（老版本按字节原样泵没这问题）。
        yield line + "\n"


#: ``is_error_event`` 真正认识的错误事件名。事件名对不上时它仍会去看
#: ``response.error``——而方舟的正常帧带的是 ``"error": null``，``None.get()``
#: 直接抛 AttributeError，把一条好流做成 502。所以先按名字过滤再调用。
_ERROR_EVENT_NAMES = frozenset({"error", "response.failed", "response.incomplete"})


def _inband_error(name: str, payload: Any) -> tuple[int, str]:
    """只对错误事件调用 ``is_error_event``，其余事件直接放过。"""
    from ..codex.projection import is_error_event

    if name not in _ERROR_EVENT_NAMES or not isinstance(payload, dict):
        return 0, ""
    return is_error_event(name, payload)


def _model_aliases(upstream_model: str) -> frozenset[str]:
    """上游真名 + 它的**归一形态**。

    接入点 ``ep-...`` 上游会原样回；写成裸模型 ID 时上游回的是自己规范化后的
    名字——实测 ``deepseek-v4.1-flash`` 回来是 ``deepseek-v4-1-flash-261017``
    （点换连字符、补日期后缀）。只按精确相等比对会一个都改不到。
    """
    aliases = {upstream_model}
    normalized = re.sub(r"[^0-9a-z]+", "-", upstream_model.lower()).strip("-")
    if normalized:
        aliases.add(normalized)
    return frozenset(aliases)


def _is_own_upstream_model(value: Any, aliases: frozenset[str]) -> bool:
    """``value`` 是否就是本次请求的那个上游模型（精确名或归一后**完全**相等）。

    不做前缀匹配：配置里写 ``deepseek-v4`` 这种短名时，前缀匹配会把
    ``deepseek-v4-1-flash`` 也认成自己，改掉本来就该如实透出的别的模型名。
    上游归一形态（``deepseek-v4.1-flash`` → ``deepseek-v4-1-flash-261017``）
    靠 :func:`_remember_upstream_model` 从首帧里学，不需要模糊匹配。
    """
    if not isinstance(value, str) or not value:
        return False
    if value in aliases:
        return True
    normalized = re.sub(r"[^0-9a-z]+", "-", value.lower()).strip("-")
    return normalized in aliases


def _known_extra(observed: set[str] | None) -> frozenset[str]:
    """已见过的底层真名快照（复制一份，避免边迭代边被并发写）。"""
    return frozenset(observed) if observed else frozenset()


#: 一条 SSE 行里的 ``"model": "..."`` 叶子。替换时只动捕获组，其余字节不动。
_MODEL_FIELD_RE = re.compile(r'("model"\s*:\s*)"((?:[^"\\]|\\.)*)"')

#: 允许改写 / 学习的键路径。只有这些位置上的 ``model`` 才是「本次请求用的模型」，
#: 其余（tool call 的 ``arguments`` 字符串、工具结果回显、别的对象上的同名字段）
#: 一律不碰——逐行正则不认层级，只靠白名单路径兜住。
_RESPONSES_PATHS = frozenset({
    ("model",),                      # response.output_text.delta 等事件的顶层 model
    ("response", "model"),           # response.created / in_progress / completed
})


def _walk_payload(payload: Any) -> Iterator[tuple[str, dict[str, Any], str]]:
    """``(键路径, 父对象, 键名)``：遍历出所有可做白名单判定的 ``"model"`` 位置。

    只降入 dict / list；字符串值里的同名字段（JSON-in-string）永远走不到。
    """
    stack: list[tuple[tuple[str, ...], Any]] = [((), payload)]
    while stack:
        path, node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                child = (*path, str(key))
                if key == "model":
                    yield child, node, key
                stack.append((child, value))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                stack.append(((*path, str(index)), item))


def _rewrite_model_payload(
    payload: Any, client_model: str, own: frozenset[str], extra: frozenset[str],
    learned: set[str] | None = None,
) -> bool:
    """按路径白名单改写 ``payload`` 里的模型名；顺带学习底层真名。

    返回 ``True`` 表示有字段被改写（调用方据此重新序列化）。
    """
    changed = False
    for path, holder, key in _walk_payload(payload):
        if path not in _RESPONSES_PATHS:
            continue
        value = holder.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        if _is_own_upstream_model(value, own) or value.strip() in extra:
            holder[key] = client_model
            changed = True
        elif learned is not None and "/" not in value:
            # 不是本次请求的上游名 → 先当接入点背后托管的平台真名学下来
            # （首帧就出现在 response.model 里，改不动它就漏给客户端了）。
            learned.add(value.strip())
            # 首帧才学到的名字，本次即可改写：它落在已知的 model 叶子上，
            # 且不是任何「对外名」形状（带 / 的都在前面被排除了）。
            holder[key] = client_model
            changed = True
    return changed


async def _iter_upstream_events(
    resp: httpx.Response, upstream_protocol: str,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """统一的事件流：两种上游都产出 ``(event_name, payload)``。

    - responses：``(type, payload)``，``event:`` 行缺省时回退 ``payload.type``；
    - chat：``("chunk", chunk)``，``[DONE]`` 结束。

    首事件闸门在这一层落地：上游 200 但带内报错（``event: error`` /
    ``response.failed``）时，必须在**向客户端吐第一个字节前**还原成异常——
    codebuddy/trae/codex 都因为漏了这步把失败做成了「200 空流」。
    """
    from ..codex.projection import aiter_sse_events
    from ..codex.sse import iter_sse_lines

    lines = iter_sse_lines(resp)
    if upstream_protocol == "responses":
        async for name, payload in aiter_sse_events(lines):
            status, message = _inband_error(name, payload)
            if status or message:
                raise HTTPException(
                    status_code=status or 502,
                    detail={"error": {
                        "message": f"ark 上游错误: {message}" if message
                        else "ark 上游返回错误事件",
                        "type": "upstream_error",
                    }},
                )
            yield name, payload
        return

    async for line in lines:
        text = line.strip()
        if not text or text.startswith(":"):
            continue
        if not text.startswith("data:"):
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
            err = chunk["error"]
            message = (str(err.get("message") or err)
                       if isinstance(err, dict) else str(err))
            raise HTTPException(status_code=502, detail={"error": {
                "message": f"ark 上游错误: {message}",
                "type": "upstream_error",
            }})
        yield "chunk", chunk


async def _responses_to_chat_stream(
    events: AsyncIterator[tuple[str, dict[str, Any]]],
    model: str,
    *,
    close: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """上游 responses 事件 → chat chunk（复用 codex 的折叠状态机）。"""
    from ..codex.projection import ResponsesToChatStream

    converter = ResponsesToChatStream(model)
    try:
        async for name, payload in events:
            for chunk in converter.feed(name, payload):
                yield chunk
        if close:
            for chunk in converter.finish():
                yield chunk
        elif not converter.finished:
            # 透传路径不补 end 事件（上游没发 completed 说明是断流，补一个假的
            # finish_reason 反而会把「假成功」写进指标）；但要冲掉已缓存的思考。
            for chunk in converter.finish():
                yield chunk
    finally:
        # events 已绑定的上游响应由下游出口的 finally 关闭（_chat_chunks_to_sse /
        # _iter_upstream_events 的调用方）；这里只负责让事件迭代器及时收摊，
        # 否则客户端中途取消时它会被搁置到下个 GC 周期。
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            await aclose()


async def _chat_chunks_to_sse(
    chunks: AsyncIterator[dict[str, Any]], resp: httpx.Response,
) -> AsyncIterator[str]:
    """chat chunk → ``data: {...}`` SSE 文本，结束后补 ``[DONE]`` 并释放上游。

    ``finally`` 是这条路径上 ``resp`` 的**唯一**主人：客户端中途取消时
    Starlette 会在 yield 处抛 ``GeneratorExit``，靠「走到最后一个 yield」释放
    等于每次取消都漏一条上游连接（方舟那头还在继续生成、继续计费）。
    """
    try:
        async for chunk in chunks:
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        await resp.aclose()


async def _pass_through_stream(
    resp: httpx.Response,
    upstream_protocol: str = "chat",
    own_aliases: frozenset[str] | None = None,
    client_model: str | None = None,
    extra_model_names: set[str] | None = None,
) -> AsyncIterator[str]:
    """把上游 SSE 原样泵给客户端（只过错误嗅探 + 模型名改写）；结束后释放连接。"""
    from ..codex.sse import iter_sse_lines

    try:
        async for line in _gate_inband_error(
            iter_sse_lines(resp), upstream_protocol,
            own_aliases=own_aliases, client_model=client_model,
            extra_model_names=extra_model_names,
        ):
            yield line
    finally:
        await resp.aclose()


async def _to_anthropic_from_chat(
    resp: httpx.Response, model: str,
) -> AsyncIterator[str]:
    """上游 chat SSE → Anthropic 事件流（dumate 同款，已上移到协议层）。"""
    from ..protocols.anthropic_adapter import sse_to_anthropic_stream

    async for event in sse_to_anthropic_stream(resp, model):
        yield event


async def _chat_chunks_to_anthropic(
    chunks: AsyncIterator[dict[str, Any]], model: str,
) -> AsyncIterator[str]:
    """chat chunk 流 → Anthropic 事件流（上游 responses 时的 anthropic 出口）。

    直接驱动 ``AnthropicStreamConverter``，不再套一层假 SSE 文本再解析回来。
    """
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
    except Exception as exc:  # noqa: BLE001 — 上游畸形数据不该只给客户端半截流
        log.warning("ark anthropic stream aborted: %s: %s", type(exc).__name__, exc)
        for event in _close_open():
            yield event
        yield _emit("error", {"type": "error", "error": {
            "type": "api_error", "message": f"{type(exc).__name__}: {exc}"}})
        return
    yield "data: [DONE]\n\n"


async def _chat_sse_to_responses(
    resp: httpx.Response, model: str,
) -> AsyncIterator[str]:
    """上游 chat SSE → Responses 事件流（上游被降级成 chat 时的 responses 出口）。"""
    from ..codex.sse import iter_sse_lines
    from ..protocols.responses_adapter import ResponsesStreamConverter

    converter = ResponsesStreamConverter(model)

    def _emit(name: str, payload: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    try:
        async for line in iter_sse_lines(resp):
            text = line.strip()
            if not text or text.startswith(":"):
                continue
            if not text.startswith("data:"):
                continue
            data = text[len("data:"):].strip()
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
    finally:
        await resp.aclose()
    yield "data: [DONE]\n\n"


async def _collect_responses(
    resp: httpx.Response, events: AsyncIterator[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """非流式：把 responses 事件流聚合成最终 response 对象。"""
    final: dict[str, Any] | None = None
    async for name, payload in events:
        if name == "response.completed":
            response = payload.get("response")
            if isinstance(response, dict):
                final = response
        elif name in ("response.failed", "response.incomplete"):
            response = payload.get("response") or {}
            err = response.get("error") if isinstance(response, dict) else None
            raise HTTPException(status_code=502, detail={"error": {
                "message": f"ark 上游未正常完成: {err or name}",
                "type": "upstream_error",
            }})
    if final is None:
        raise HTTPException(status_code=502, detail={"error": {
            "message": "ark 上游未返回完整的 response 对象（断流）",
            "type": "bad_gateway",
        }})
    return final


async def _collect_chat(resp: httpx.Response, model: str) -> dict[str, Any]:
    """非流式：把 chat chunk 流聚合成 ``chat.completion``。

    上游恒以 stream 请求，所以非流式客户端也得在这里拼。工具调用的
    ``arguments`` 按 ``index`` 增量拼接（OpenAI 流式契约）。
    """
    text: list[str] = []
    reasoning: list[str] = []
    tools: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    chunk_id = ""
    created = int(time.time())

    async def _drive() -> None:
        nonlocal finish_reason, usage, chunk_id, created
        async for _name, chunk in _iter_upstream_events(resp, "chat"):
            chunk_id = str(chunk.get("id") or chunk_id)
            if isinstance(chunk.get("created"), int):
                created = chunk["created"]
            if chunk.get("usage"):
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

    import asyncio

    try:
        await asyncio.wait_for(_drive(), timeout=_AGGREGATE_DEADLINE_S)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail={
            "error": {"message": "ark 非流式聚合超过总时长上限", "type": "timeout"}
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


def _chat_completion_to_response(
    completion: dict[str, Any], model: str, original: dict[str, Any] | None,
) -> dict[str, Any]:
    """chat.completion → Responses ``response`` 对象（非流式 responses 出口）。"""
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
    return {
        "id": f"resp_{os.urandom(12).hex()}",
        "object": "response",
        "created_at": completion.get("created") or int(time.time()),
        "model": model,
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
    }


# ---------------------------------------------------------------------------
# 冒烟自测：python -m buddy_proxy.providers.ark
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    key = resolve_api_key()
    if not key:
        print(f"no api key found (env ARK_API_KEY / {secret_file_path()})")
        raise SystemExit(1)
    entries = parse_models(load_config())
    if not entries:
        print(f"no models configured in {config_file_path()}")
        raise SystemExit(1)
    print(f"key: {key[:6]}***{key[-4:]}  base: {resolve_base_url()}")
    print(f"models: {', '.join(e['id'] + ' -> ' + e['upstream'] for e in entries)}")
    with httpx.Client(timeout=60) as client:
        for entry in entries:
            r = client.post(
                f"{resolve_base_url()}/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": entry["upstream"], "max_tokens": 16,
                      "messages": [{"role": "user", "content": "只回复两个字：pong"}]},
            )
            print(f"{entry['id']}: status {r.status_code}  {r.text[:200]}")
