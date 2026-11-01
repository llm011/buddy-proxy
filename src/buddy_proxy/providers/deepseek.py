"""DeepSeek 官方 API provider —— ``deepseek`` 渠道。

上游是 DeepSeek 开放平台（``https://api.deepseek.com``）。与 zcode/glm 这类
「把某个 CLI 的登录态或第三方套餐接进来」的通道不同，本通道是**官方直连**：
凭据就是平台签发的 API key，模型目录与余额都能从官方端点查到。

三个协议**上游原生支持**，所以全部直通、零转换（2026-11-01 实测）：

- openai    → ``POST {base}/v1/chat/completions``
- responses → ``POST {base}/v1/responses``
- anthropic → ``POST {base}/anthropic/v1/messages``（注意 anthropic 挂在
  ``/anthropic`` 前缀下，不是 ``/v1/messages``）

直通的好处是客户端拿到的是上游原生的 SSE 事件序列/JSON 形状，不需要本地
转换器来回折——ark 那种「responses 优先、自动降级 chat、再翻译回 anthropic」
的复杂度在这里完全不需要。

凭据（两处，env 优先）：

1. 环境变量 ``DEEPSEEK_API_KEY``
2. 本项目 key 文件 ``~/.buddy-proxy/deepseek_api_key``（首行裸 key 或
   ``name=value``，0600）

基址可用 ``DEEPSEEK_BASE_URL`` 整体覆盖（走自建反代/网关时用）。

**模型名不与本地静态表抢名分**：官方 id 是 ``deepseek-flash`` /
``deepseek-v4-pro``，其中 ``deepseek-v4-pro`` 与 CodeBuddy 静态表里的同名条目
撞车。本通道声明 ``route_priority = 50``（低于 CodeBuddy 的 100），于是撞车的
裸名**让给兜底通道**，要显式走官方就写 ``deepseek/deepseek-v4-pro``——与 ark
通道同一套约定。原因是安全：加个 ``--deepseek`` 就把既有 ``deepseek-v4-pro``
流量静默改道到**计费的官方 API**上，属于会花钱的意外，必须由用户显式点名。

结算方式与 Credits 体系不同：官方是**预付费余额**（按 token 计价的美元/人民币
钱包），没有「积分倍率」概念，故 :meth:`DeepSeekProvider.quota` 报的是余额，
``estimate_credit`` 对本通道返回 None（请求日志的 Credit 列显示 ``—``）。
"""

from __future__ import annotations

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

#: 官方基址。``DEEPSEEK_BASE_URL`` 可整体覆盖（自建反代/网关）。
DEFAULT_BASE_URL = "https://api.deepseek.com"

#: 兜底模型表（``GET {base}/models`` 拉不到时用它）。
#:
#: 只放官方 ``/models`` 真实返回的 id（2026-11-01 实测）——上游**不再接受**
#: ``deepseek-v4.1-flash``（400：supported names are deepseek-flash,
#: deepseek-v4-pro），所以目录里不能出现它，否则用户选中一个必然 400 的名字。
DEFAULT_MODELS: dict[str, str] = {
    "deepseek-flash": "DeepSeek-V4.1-Flash (thinking, 1M ctx, vision)",
    "deepseek-v4-pro": "DeepSeek-V4-Pro (thinking, 1M ctx)",
}

#: 客户端可能照旧文档/别家目录写的名字 → 官方当前真名。
#:
#: ``deepseek-v4.1-flash`` 是 CodeBuddy 目录里的叫法（本仓库静态表用它），
#: 用户照着敲 ``deepseek/deepseek-v4.1-flash`` 是很自然的误写；官方对它是
#: 400 而不是自动归一，所以这里替上游兜一层，省掉一次「为什么 400」的排查。
#: ``deepseek-chat`` / ``deepseek-v4-flash`` 官方仍收（向后兼容名），归一后
#: 日志与 /v1/models 口径一致。
MODEL_ALIASES: dict[str, str] = {
    "deepseek-v4.1-flash": "deepseek-flash",
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-chat": "deepseek-flash",
}

#: 流式读超时对齐 ark/codex：thinking 模型首 token 之前会有静默段。
_TIMEOUT = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=15.0)

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "close"}

#: 三个协议各自的上游路径（anthropic 的 /anthropic 前缀由官方文档给定）。
_PROTOCOL_PATHS = {
    "openai": "/v1/chat/completions",
    "responses": "/v1/responses",
    "anthropic": "/anthropic/v1/messages",
}


def secret_file_path() -> Path:
    """本渠道自己的 key 文件：``~/.buddy-proxy/deepseek_api_key``。"""
    return state_file("deepseek_api_key")


def resolve_api_key() -> str:
    """解析 API key：环境变量 ``DEEPSEEK_API_KEY`` > ``deepseek_api_key`` 文件。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    return _load_secret_file_impl(secret_file_path())


def resolve_base_url() -> str:
    """基址：``DEEPSEEK_BASE_URL`` 优先，缺省官方域名（去掉尾部斜杠）。"""
    return (os.environ.get("DEEPSEEK_BASE_URL", "") or DEFAULT_BASE_URL).strip().rstrip("/")


def auth_headers(api_key: str, *, stream: bool = False) -> dict[str, str]:
    """Bearer 认证头；``Accept`` 按流式/非流式分开（部分网关据它决定是否 SSE）。"""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }


def upstream_error_response(resp: httpx.Response) -> JSONResponse:
    """把上游错误转成客户端错误响应（透传状态码与错误体）。

    官方错误体本身就是 OpenAI 兼容形状（``error.message`` / ``error.type``），
    原样回给客户端即可——客户端能按自己熟悉的那套解析出真因。
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


class DeepSeekProvider(BaseProvider):
    id = "deepseek"
    name = "DeepSeek 官方 API"

    #: 低于 CodeBuddy 的 100：裸名撞车时让位（见模块 docstring）。
    route_priority = 50

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self._api_key = api_key if api_key is not None else resolve_api_key()
        self._base = (base_url or resolve_base_url()).rstrip("/")
        self._client: httpx.AsyncClient | None = None
        # 目录从静态兜底表起步；refresh_models() 拉到官方元数据后就地替换。
        self._models: list[dict[str, Any]] = [
            {
                "id": mid,
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "name": desc,
                "description": desc,
            }
            for mid, desc in DEFAULT_MODELS.items()
        ]

    # ---- BaseProvider 接口 ----

    def models(self) -> Sequence[dict[str, Any]]:
        return list(self._models)

    def resolve_model(self, model: str) -> str:
        """别名归一（停用/时段键与转发口径共用，见 forward_chat 的说明）。"""
        return MODEL_ALIASES.get(model, model)

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        """认 ``deepseek-flash`` 与 ``deepseek/deepseek-flash`` 两种写法。

        ``models()`` 回的是裸 id（与 zcode/kimi 同口径，不带 ``provider/``
        前缀），所以基类那套「id 自带前缀」的匹配在这里落空——不覆写的话
        ``deepseek/<名>`` 只能靠 forward_chat 的前缀分支兜住，而
        ``_resolve_auto`` 的 model_order 归属解析会认不出它。ark 出于同一原因
        也覆写了本方法（见 ark.DeepSeekProvider 同名实现）。
        """
        want = (model or "").strip()
        if not want:
            return False
        bare = want.removeprefix(f"{self.id}/")
        return any(str(m.get("id") or "") == bare for m in self._models)

    def ensure_auth(self) -> None:
        if not self._api_key:
            self._api_key = resolve_api_key()
        if not self._api_key:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": (
                            "deepseek 未配置认证：请设置 DEEPSEEK_API_KEY 或写 "
                            "~/.buddy-proxy/deepseek_api_key（DeepSeek 开放平台 → "
                            "API Keys 签发，platform.deepseek.com）"
                        ),
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
        """按客户端协议直通官方端点；三个协议上游都原生支持，不做本地转换。

        ``original``（routes 层传来的、协议转换前的请求体）是 anthropic /
        responses 的权威形状——routes 传进来的 ``body`` 一律是**已转成 chat**
        的形态，拿它去打 ``/v1/responses`` 或 ``/anthropic/v1/messages`` 上游
        会直接 400。openai 协议则反过来：``body`` 才是要发的那份。
        """
        self.ensure_auth()
        stream = bool(body.get("stream", False))
        # 路由层剥过 "deepseek/" 前缀、也可能把别名归一过；上游只见真名。
        upstream_model = MODEL_ALIASES.get(str(body.get("model") or ""), str(body.get("model") or ""))

        if protocol == "openai":
            upstream_body = {k: v for k, v in body.items() if not k.startswith("_")}
        else:
            source = original if isinstance(original, dict) and original else body
            upstream_body = {k: v for k, v in source.items() if not k.startswith("_")}
        upstream_body["model"] = upstream_model

        # 非流式也保留 stream 字段的原值：上游据此决定回 JSON 还是 SSE，路由层
        # 已把它归一成 bool（客户端没写时按 provider 约定处理）。
        upstream_body["stream"] = stream

        url = f"{self._base}{_PROTOCOL_PATHS.get(protocol, _PROTOCOL_PATHS['openai'])}"
        client = await self._get_client()
        try:
            request = client.build_request(
                "POST", url, json=upstream_body,
                headers=auth_headers(self._api_key, stream=stream),
            )
            resp = await client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            log.warning("deepseek upstream timeout: %s", describe_exception(exc))
            raise HTTPException(status_code=504, detail={
                "error": {"message": "deepseek upstream timeout", "type": "timeout"}}
            ) from exc
        except httpx.HTTPError as exc:
            # describe_exception 而非裸 %s：httpcore 会把底层异常映射成自身
            # str() 为空的 httpx 对象，裸打只剩一行空话（zcode 踩过）。
            log.warning("deepseek upstream error: %s", describe_exception(exc))
            raise HTTPException(status_code=502, detail={
                "error": {"message": "deepseek upstream error", "type": "bad_gateway"}}
            ) from exc

        if resp.status_code >= 400:
            # 错误体先读完再关：aclose() 会丢弃未读 body，之后拿不到上游真实
            # 错误（401 key 无效 / 402 余额不足都会变成空错误体）——zcode 踩过。
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
        # send(stream=True) 之后 body 是**未读**的：直接 resp.json() 会抛
        # ResponseNotRead（httpx 的流式响应必须显式读完才能取内容）。非流式
        # 在上游本来就回一个普通 JSON，一次 aread() 即可，不是「聚合流」。
        await resp.aread()
        try:
            payload = resp.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail={
                "error": {"message": "deepseek upstream returned non-JSON",
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
            "configured": bool(self._api_key),
            "base_url": self._base,
            "models": [str(m.get("id")) for m in self._models],
        }

    async def refresh_models(self, force: bool = False) -> list[dict[str, Any]]:
        """从官方 ``GET {base}/models`` 强拉模型目录与元数据。

        ``force`` 只为统一刷新接口保留：本通道没有 TTL 缓存，每次调用都请求上游。

        上游给的是**能力元数据**（context_window / max_output_tokens /
        input_modalities / effort），比静态表丰富得多，而 /v1/models 的上下文
        窗口与图片能力又直接由这些字段驱动（漏传会让支持读图的 deepseek-flash
        被报成纯文本）——所以拿到就覆盖。拉取失败抛错、保留旧目录，由管理页
        转成可读 502，不会把目录清空成空表。
        """
        del force
        self.ensure_auth()
        client = await self._get_client()
        try:
            resp = await client.get(f"{self._base}/models",
                                    headers=auth_headers(self._api_key))
        except httpx.HTTPError as exc:
            raise RuntimeError(f"deepseek 模型目录请求失败: {describe_exception(exc)}") from exc
        if resp.status_code >= 400:
            await resp.aread()
            raise RuntimeError(f"deepseek 模型目录 HTTP {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or not entries:
            raise RuntimeError("deepseek 模型目录响应为空")
        models = [m for m in (_normalize_entry(e) for e in entries) if m]
        if not models:
            raise RuntimeError("deepseek 模型目录里没有可用条目")
        self._models = models
        return list(models)

    # ---- 余额查询（/ui 管理页消费；同步 httpx，调用方经 asyncio.to_thread 包装） ----

    def quota(self) -> dict[str, Any] | None:
        """查询账户余额（``GET {base}/user/balance``）。

        与其它通道的「额度/套餐」不是一回事：官方是**预付费钱包**，报的是还能
        花多少钱，没有周期重置、也没有总量分母。所以每个币种给一条
        ``remaining``（有余额有钱数、无分母），**刻意不填 total**——
        填个 total 就得凭空造一个「总量」出来，「已用 N%」会是编的。
        不填 total 还有个附带的好处：``_quota_low`` 的占比告警不会误报
        （它要求分子分母都有），余额是否告急由用户自己看数字判断。
        """
        key = self._api_key or resolve_api_key()
        if not key:
            raise RuntimeError("deepseek 未配置 API key，无法查询余额")
        payload = fetch_balance(self._base, key)
        return parse_balance(payload)


def fetch_balance(base: str, api_key: str) -> dict[str, Any]:
    """同步拉官方余额（``GET {base}/user/balance``）。

    拆成独立函数是为了让它可被测试用 transport 替换——``quota()`` 是
    BenefitsManager 经 ``asyncio.to_thread`` 调用的同步方法，签名里塞不进
    mock client，而 httpx 的 MockTransport 只在自建 Client 上生效。
    """
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(f"{base}/user/balance", headers=auth_headers(api_key))
    if resp.status_code != 200:
        hint = "402 表示余额不足" if resp.status_code == 402 else ""
        raise RuntimeError(
            f"balance HTTP {resp.status_code}: {resp.text[:200]} {hint}".strip())
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("balance 响应不是 JSON 对象")
    return payload


def parse_balance(payload: dict[str, Any]) -> dict[str, Any]:
    """官方余额响应 → 本仓库的 quota 结构。

    每个币种一条，只填 ``remaining``、**刻意不填 total**：官方是预付费钱包，
    没有「总量分母」，编一个出来会让界面显示「已用 N%」这种假水位；不填还有个
    附带好处——``_quota_low`` 的占比告警要求分子分母都有，余额不会触发它。
    """
    infos = payload.get("balance_infos") or []
    items: list[dict[str, Any]] = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        currency = str(info.get("currency") or "").strip() or "?"
        total = _as_float(info.get("total_balance"))
        granted = _as_float(info.get("granted_balance"))
        topped = _as_float(info.get("topped_up_balance"))
        note_parts = []
        if granted is not None:
            note_parts.append(f"赠送 {granted:.2f}")
        if topped is not None:
            note_parts.append(f"充值 {topped:.2f}")
        item: dict[str, Any] = {
            "label": f"账户余额 ({currency})",
            # unit=currency：既不是 credit 也不是百分比口径，_quota_low 的
            # 两套阈值对它都不适用（没分母），横幅不会拿它报警。
            "unit": "currency",
        }
        if total is not None:
            item["remaining"] = round(total, 2)
        else:
            # 上游没给数：给一条说明文案（前端按字符串渲染成说明条），
            # 别让卡片显示成「剩 0」把未知当零。
            item["remaining"] = f"余额未知（{currency}）"
        if note_parts:
            item["note"] = " · ".join(note_parts)
        items.append(item)
    level = None if payload.get("is_available") else "余额不足"
    return {"items": items or [{"label": "账户余额", "remaining": "上游未返回余额信息",
                                "unit": "currency"}],
            "level": level}


def _as_float(value: Any) -> float | None:
    """宽松取数：数字照收、数字字符串也收（上游余额是字符串），布尔不收。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _normalize_entry(entry: Any) -> dict[str, Any] | None:
    """官方 ``/models`` 条目 → 本仓库的模型条目（带能力元数据）。

    只认官方目录里真实存在的 id；上游元数据里有的字段才带上——缺字段时留空
    比编一个默认值诚实（``/v1/models`` 的图片能力判断依赖 ``images``）。
    """
    if not isinstance(entry, dict):
        return None
    model_id = str(entry.get("id") or "").strip()
    if not model_id:
        return None
    name = str(entry.get("name") or model_id)
    modalities = entry.get("input_modalities")
    images = isinstance(modalities, list) and "image" in modalities
    model: dict[str, Any] = {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "deepseek",
        "name": name,
        "description": name,
        "images": images,
        "input_modalities": ["text", "image"] if images else ["text"],
        # 三个协议上游都支持工具调用与思考模式（官方 Models & Pricing 表）。
        "tool_call": True,
        "reasoning": bool(entry.get("effort")),
    }
    context_window = entry.get("context_window")
    if isinstance(context_window, int):
        model["context_window"] = context_window
        model["max_input"] = context_window
    max_output = entry.get("max_output_tokens")
    if isinstance(max_output, int):
        model["max_output"] = max_output
    effort = entry.get("effort")
    if isinstance(effort, dict) and effort.get("supported_levels"):
        model["reasoning_levels"] = list(effort["supported_levels"])
    return model


# ---------------------------------------------------------------------------
# 冒烟自测：python -m buddy_proxy.providers.deepseek
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    key = resolve_api_key()
    if not key:
        print("no api key found (env DEEPSEEK_API_KEY / ~/.buddy-proxy/deepseek_api_key)")
        raise SystemExit(1)
    base = resolve_base_url()
    print(f"key: {key[:6]}***{key[-4:]}  base: {base}")
    with httpx.Client(timeout=60) as client:
        r = client.get(f"{base}/models", headers=auth_headers(key))
        print(f"models: {r.status_code} {r.text[:300]}")
        b = client.get(f"{base}/user/balance", headers=auth_headers(key))
        print(f"balance: {b.status_code} {b.text[:300]}")