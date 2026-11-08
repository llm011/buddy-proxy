"""yuntoken 网关 provider —— ``yuntoken`` 渠道。

上游是一个**自建的 OpenAI 兼容聚合网关**（多账号池 + 负载均衡），由它自己
负责账号轮转、配额与故障切换。对本仓库来说它就是一个「Bearer key + 模型
目录」的普通上游，与 ``deepseek`` 同属**直通型通道**（不做本地协议转换）。

协议支持（2026-11-07 实测）：

- ``openai``    → ``POST {base}/v1/chat/completions``（流式/非流式都原生支持）
- ``anthropic`` → ``POST {base}/v1/messages``（上游直接回 Anthropic 原生形状，
  含 ``thinking`` 块），所以同样直通，不需要事件机来回折；
- ``responses`` → **上游没有这个端点**（实测 404）。本通道对该协议回 501 并
  给出「换 openai/anthropic 协议」的可操作提示，而不是把 chat 请求打到
  ``/v1/responses`` 换来一个语焉不详的 404。

配置（三处，环境变量优先）：

1. ``YUNTOKEN_API_KEY``          —— 网关签发的 Bearer key；
2. ``YUNTOKEN_BASE_URL``         —— 网关基址，**没有默认值**；
3. ``~/.buddy-proxy/yuntoken_api_key`` 与 ``~/.buddy-proxy/yuntoken.json``
   （``{"base_url": "..."}``，0600），供不想塞环境变量时使用。

**基址刻意没有硬编码默认值。** 网关地址与 key 都是部署方的私有信息，本仓库
是开源的——写死一个域名等于把某人的基础设施钉进公开代码里，而且换部署还得
改代码。没配基址时 :meth:`YuntokenProvider.ensure_auth` 给一条带具体操作步骤
的 401，而不是拼一个 ``http://localhost`` 之类的假地址去打。同理，模型目录
完全来自上游 ``GET /v1/models``，代码里**没有任何默认模型表**：网关挂哪些模型
取决于它背后的账号池，编一张静态表只会在刷新前误导用户。

``route_priority = 50``：网关里会出现 ``glm-5`` / ``deepseek-3.2`` 这类**公共
模型名**，与本仓库其它通道的静态表撞车。声明低名分后，裸名撞车时仍归原本
声明它的通道（通常是 CodeBuddy 静态表），要显式走网关就写
``yuntoken/<模型>``——与 ark / deepseek 同一套约定。作用是安全：加一个
``--yuntoken`` 不该把既有的裸名流量静默改道到另一条计费链路上。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .base import BaseProvider
from .zcode import _load_secret_file_impl
from ..core.errors import describe_exception
from ..core.paths import state_file

log = logging.getLogger(__name__)

#: 三个协议各自的上游路径。``responses`` 为 None —— 上游没有该端点。
_PROTOCOL_PATHS: dict[str, str | None] = {
    "openai": "/v1/chat/completions",
    "anthropic": "/v1/messages",
    "responses": None,
}

#: 与 ark/codex 对齐：thinking 模型首 token 之前会有较长静默段。
_TIMEOUT = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=15.0)

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "close"}

#: 配置提示里用的固定文案（多处复用，避免措辞漂移）。
_CONFIG_HINT = (
    "请设置 YUNTOKEN_API_KEY 与 YUNTOKEN_BASE_URL（或写 "
    "~/.buddy-proxy/yuntoken_api_key 与 ~/.buddy-proxy/yuntoken.json 的 "
    "base_url 字段）"
)


def secret_file_path() -> Path:
    """本渠道的 key 文件：``~/.buddy-proxy/yuntoken_api_key``。"""
    return state_file("yuntoken_api_key")


def config_file_path() -> Path:
    """本渠道的配置文件：``~/.buddy-proxy/yuntoken.json``（目前只放 base_url）。"""
    return state_file("yuntoken.json")


def resolve_api_key() -> str:
    """解析 API key：环境变量 ``YUNTOKEN_API_KEY`` > ``yuntoken_api_key`` 文件。"""
    key = os.environ.get("YUNTOKEN_API_KEY", "").strip()
    if key:
        return key
    return _load_secret_file_impl(secret_file_path())


def _load_config_impl(path: Path) -> dict[str, Any]:
    """读 JSON 配置；缺失/损坏时返回空 dict 而不抛。

    与 ``ark.json`` 同口径：可选配置文件不该让整个代理在启动阶段炸掉，真正的
    可操作提示由 :meth:`YuntokenProvider.ensure_auth` 在需要时给出。
    """
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve_base_url() -> str:
    """基址：``YUNTOKEN_BASE_URL`` > ``yuntoken.json`` 的 ``base_url``。

    **没有内置默认值**（见模块 docstring）；都没配时返回空串，由
    ``ensure_auth`` 转成可操作的 401。
    """
    base = os.environ.get("YUNTOKEN_BASE_URL", "").strip()
    if not base:
        cfg_base = _load_config_impl(config_file_path()).get("base_url")
        base = str(cfg_base).strip() if isinstance(cfg_base, str) else ""
    return base.rstrip("/")


def auth_headers(api_key: str, *, stream: bool = False) -> dict[str, str]:
    """Bearer 认证头；``Accept`` 按流式/非流式分开。"""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }


def upstream_error_response(resp: httpx.Response) -> JSONResponse:
    """把上游错误转成客户端错误响应（透传状态码与错误体）。

    上游错误体有两种形态，都得原样回：

    - OpenAI 形状 ``{"error": {"message": ...}}``（chat 端点）；
    - FastAPI 形状 ``{"detail": "..."}``（鉴权/路由层，如 ``401
      {"detail": "Invalid or missing API Key"}``）。

    不做归一化——客户端本来就按 OpenAI 那套解析，多包一层反而丢掉真因。
    """
    try:
        payload = resp.json()
    except Exception:
        payload = {"error": {"message": resp.text[:500], "type": "upstream_error"}}
    return JSONResponse(status_code=resp.status_code, content=payload)


async def _pass_through_stream(
    client: httpx.AsyncClient,
    response: httpx.Response,
) -> AsyncIterator[bytes]:
    """把上游 SSE/字节流原样泵给客户端；结束后确保连接释放。"""
    try:
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk
    finally:
        await response.aclose()


class YuntokenProvider(BaseProvider):
    id = "yuntoken"
    name = "yuntoken 网关"

    #: 低于 CodeBuddy 的 100：裸名（glm-5 / deepseek-3.2 等公共名）撞车时让位
    #: 给原本声明该名字的通道，显式走本通道用 ``yuntoken/<模型>``。
    route_priority = 50

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self._api_key = api_key if api_key is not None else resolve_api_key()
        self._base = (base_url if base_url is not None else resolve_base_url()).rstrip("/")
        self._client: httpx.AsyncClient | None = None
        # 刻意从空表起步，没有任何默认模型（见模块 docstring）。
        self._models: list[dict[str, Any]] = []
        #: 上一次目录刷新失败的原因（health/管理页可见），成功后清空。
        self._models_error: str = ""

    # ---- BaseProvider 接口 ----

    def models(self) -> Sequence[dict[str, Any]]:
        return list(self._models)

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        """认 ``claude-opus-4.6`` 与 ``yuntoken/claude-opus-4.6`` 两种写法。

        ``models()`` 回的是裸 id（与 deepseek/ark 同口径），基类那套「id 自带
        ``provider/`` 前缀」的匹配在这里落空——不覆写的话 ``_resolve_auto`` 的
        model_order 归属解析会认不出本通道。
        """
        want = (model or "").strip()
        if not want:
            return False
        bare = want.removeprefix(f"{self.id}/")
        return any(str(m.get("id") or "") == bare for m in self._models)

    def ensure_auth(self) -> None:
        """校验 key 与基址都在。

        基址缺失与 key 缺失归到同一条 401 里分别说清：这两件事的修法不同
        （一个是申请 key，一个是填网关地址），混成一句「未配置认证」会让
        用户只去改一半。
        """
        if not self._api_key:
            self._api_key = resolve_api_key()
        if not self._base:
            self._base = resolve_base_url()
        missing: list[str] = []
        if not self._base:
            missing.append("网关基址（YUNTOKEN_BASE_URL / yuntoken.json 的 base_url）")
        if not self._api_key:
            missing.append("API key（YUNTOKEN_API_KEY / yuntoken_api_key）")
        if missing:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": f"yuntoken 未配置：缺 {' 与 '.join(missing)}。" + _CONFIG_HINT,
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
        """按客户端协议直通对应端点；不做本地协议转换。

        ``original`` 是协议转换前的请求体：``anthropic`` 必须用它——routes 层
        传进来的 ``body`` 已转成 chat 形态，拿它去打 ``/v1/messages`` 上游会
        直接 400。``openai`` 则相反，``body`` 才是要发的那份。

        但 ``original`` 里**没被剥前缀**：``yuntoken/<模型>`` 的剥前缀只改了
        ``body``（见 ``forward_chat`` 的「显式 provider 前缀」分支）。直接拿
        ``original`` 发包，上游会收到 ``yuntoken/deepseek-3.2`` 整串，回
        「分组 default 下模型 … 无可用渠道」。所以 ``model`` 一律以 ``body``
        为准回填——与 deepseek 同一处同一种写法（``body`` 的 model 才是路由
        解析后的真名）。
        """
        self.ensure_auth()
        path = _PROTOCOL_PATHS.get(protocol, _PROTOCOL_PATHS["openai"])
        if path is None:
            raise HTTPException(
                status_code=501,
                detail={
                    "error": {
                        "message": (
                            f"yuntoken 网关不支持 {protocol} 协议（上游没有该端点）。"
                            "请改用 openai（/v1/chat/completions）或 "
                            "anthropic（/v1/messages）协议访问本通道。"
                        ),
                        "type": "unsupported_protocol",
                    }
                },
            )

        stream = bool(body.get("stream", False))
        if protocol == "openai":
            upstream_body = {k: v for k, v in body.items() if not k.startswith("_")}
        else:
            source = original if isinstance(original, dict) and original else body
            upstream_body = {k: v for k, v in source.items() if not k.startswith("_")}
        # model 一律对齐到 body（已剥 yuntoken/ 前缀）。original 里是客户端原话，
        # 带前缀时整串漏给上游会报「无可用渠道」——见 forward 的说明。
        upstream_model = str(body.get("model") or "").strip()
        if upstream_model:
            upstream_body["model"] = upstream_model
        upstream_body["stream"] = stream

        url = f"{self._base}{path}"
        client = await self._get_client()
        try:
            request = client.build_request(
                "POST", url, json=upstream_body,
                headers=auth_headers(self._api_key, stream=stream),
            )
            resp = await client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            log.warning("yuntoken upstream timeout: %s", describe_exception(exc))
            raise HTTPException(status_code=504, detail={
                "error": {"message": "yuntoken upstream timeout", "type": "timeout"}}
            ) from exc
        except httpx.HTTPError as exc:
            # describe_exception 而非裸 %s：httpcore 会把底层异常映射成自身
            # str() 为空的 httpx 对象，裸打只剩一行空话。
            log.warning("yuntoken upstream error: %s", describe_exception(exc))
            raise HTTPException(status_code=502, detail={
                "error": {"message": "yuntoken upstream error", "type": "bad_gateway"}}
            ) from exc

        if resp.status_code >= 400:
            # 错误体先读完再关：aclose() 会丢弃未读 body，之后拿不到上游真实
            # 错误（401 key 无效会变成空错误体）。
            await resp.aread()
            try:
                return upstream_error_response(resp)
            finally:
                await resp.aclose()

        if stream:
            return StreamingResponse(
                _pass_through_stream(client, resp),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )
        # send(stream=True) 之后 body 未读：直接 resp.json() 会抛
        # ResponseNotRead，一次 aread() 即可。
        await resp.aread()
        try:
            payload = resp.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail={
                "error": {"message": "yuntoken upstream returned non-JSON",
                          "type": "bad_gateway"}}
            ) from exc
        finally:
            await resp.aclose()
        return JSONResponse(content=payload)

    # ---- 内部 ----

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client

    def health(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "configured": bool(self._api_key and self._base),
            "base_url": self._base,
            "models": [str(m.get("id")) for m in self._models],
            **({"models_error": self._models_error} if self._models_error else {}),
        }

    async def refresh_models(self, force: bool = False) -> list[dict[str, Any]]:
        """从网关 ``GET /v1/models`` 拉模型目录。

        ``force`` 只为统一刷新接口保留：本通道没有 TTL 缓存，每次调用都请求上游。

        上游条目只有 ``id`` / ``description`` / ``owned_by``，**没有能力元数据**
        （上下文窗口、是否支持工具、是否支持读图）。这些字段一律不猜、不填：
        ``/v1/models`` 的图片能力判断读 ``images``，填 ``True`` 会让客户端把图片
        发给一个可能不认图的上游，填 ``False`` 会把支持读图的模型当纯文本——
        两种都错，缺字段才是诚实（由客户端按自己的默认值处理）。

        拉取失败抛错、**保留旧目录**，由管理页转成可读 502，不会把目录清空成
        空表（空表等于所有模型 404，比一句报错难查得多）。
        """
        del force
        self.ensure_auth()
        client = await self._get_client()
        try:
            resp = await client.get(f"{self._base}/v1/models",
                                    headers=auth_headers(self._api_key))
        except httpx.HTTPError as exc:
            self._models_error = describe_exception(exc)
            raise RuntimeError(f"yuntoken 模型目录请求失败: {self._models_error}") from exc
        if resp.status_code >= 400:
            await resp.aread()
            self._models_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            raise RuntimeError(f"yuntoken 模型目录 {self._models_error}")
        payload = resp.json()
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or not entries:
            self._models_error = "响应为空"
            raise RuntimeError("yuntoken 模型目录响应为空")
        models = [m for m in (_normalize_entry(e) for e in entries) if m]
        if not models:
            self._models_error = "没有可用条目"
            raise RuntimeError("yuntoken 模型目录里没有可用条目")
        self._models = models
        self._models_error = ""
        return list(models)


def _normalize_entry(entry: Any) -> dict[str, Any] | None:
    """上游 ``/v1/models`` 条目 → 本仓库的模型条目。

    只搬上游真给了的字段：``id`` 必需，``name`` 缺失时退回 id，能力字段
    （``images`` / ``tool_call`` / ``context_window`` 等）**一律不填**——见
    :meth:`YuntokenProvider.refresh_models` 的说明。
    """
    if not isinstance(entry, dict):
        return None
    model_id = str(entry.get("id") or "").strip()
    if not model_id:
        return None
    description = entry.get("description")
    model: dict[str, Any] = {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "yuntoken",
        "name": model_id,
    }
    if isinstance(description, str) and description.strip():
        model["description"] = description.strip()
    return model


# ---------------------------------------------------------------------------
# 冒烟自测：python -m buddy_proxy.providers.yuntoken
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    key = resolve_api_key()
    base = resolve_base_url()
    if not key or not base:
        print("missing config (env YUNTOKEN_API_KEY / YUNTOKEN_BASE_URL, "
              "or ~/.buddy-proxy/yuntoken_api_key + yuntoken.json)")
        raise SystemExit(1)
    print(f"key: {key[:6]}***{key[-4:]}  base: {base}")
    with httpx.Client(timeout=60) as client:
        r = client.get(f"{base}/v1/models", headers=auth_headers(key))
        print(f"models: {r.status_code} {r.text[:300]}")
