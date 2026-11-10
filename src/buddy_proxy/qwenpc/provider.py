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

import asyncio
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

#: 协议 → 上游路径。两条都是**原生**支持，所以直接透传、不做转换。
_PROTOCOL_PATHS = {
    "openai": "/v1/chat/completions",
    "anthropic": "/v1/messages",
}

#: 额度查询的进程内缓存（秒）。
_QUOTA_TTL_S = 3.0

#: 额度快照缓存：``(ts, remaining)``。
_quota_cache: tuple[float, float | None] = (0.0, None)

#: 调用后**轮询等待稳定**的最长时间（秒）与间隔。
#:
#: 上游在调用结束后约 1 秒内会返回一个「比真实值低 20」的临时值（实测三轮
#: 完全一致）::
#:
#:     调用前        394.8859
#:     +0.0s        374.8858   ← 低 20（结算中）
#:     +0.4s        374.8858   ← 还在低
#:     +0.8s        394.8494   ← 稳定
#:
#: 所以这里不是「睡固定时间再查一次」，而是**轮询到连续两次读数一致**为止
#: ——固定 sleep 在网络慢时可能还没结算完，快时又白等。
_QUOTA_POLL_TIMEOUT_S = 10.0
_QUOTA_POLL_GAP_S = 0.4

#: 调用前采样的次数与间隔（取最大值，避开偶发抖动）。
_QUOTA_PRE_SAMPLES = 3
_QUOTA_SAMPLE_GAP_S = 0.35

#: 调用前采样的间隔（秒）。
_QUOTA_SAMPLE_GAP_S = 0.35

#: 单次调用消耗的合理上界。超过它一定是结算抖动/额度池切换，不是真消耗
#: （实测真实单次消耗 0.03~0.05）。
_MAX_SANE_CREDIT = 5.0

#: 抖动幅度：上游临时值 = 真实值 - 这个数。用来**识别**抖动读数。
_JITTER_DROP = 20.0


async def _quota_once() -> float | None:
    try:
        return await asyncio.to_thread(creds.remaining_quota)
    except Exception as exc:  # noqa: BLE001 - 查额度失败不影响转发
        log.debug("qwenpc 额度查询失败: %s", type(exc).__name__)
        return None


async def _quota_before() -> float | None:
    """调用**之前**取额度基线。

    注意：基线本身也可能落在抖动值上（实测 ``369.86`` vs 真实 ``389.88``，
    差整整 20）。所以不能简单取「几次采样的最大值」——如果整组都采到了抖动
    值，最大值也是错的。

    做法：采一组，若**组内出现两个相差 20 的值**，说明这一组混着抖动值，
    取大的那个（真实值只会比抖动值大）；若整组一致，也照用——但记下这个值，
    调用后如果发现差值是负数，说明基线取错了，由 ``_spent_credit`` 兜住。
    """
    values: list[float] = []
    for i in range(_QUOTA_PRE_SAMPLES):
        v = await _quota_once()
        if v is not None:
            values.append(v)
        if i < _QUOTA_PRE_SAMPLES - 1:
            await asyncio.sleep(_QUOTA_SAMPLE_GAP_S)
    if not values:
        return None
    return max(values)


def _is_jitter(value: float | None, baseline: float | None) -> bool:
    """这个读数是否是「比基线低 20」的结算抖动值。

    上游在调用后约 1 秒内会返回 ``真实值 - 20``（实测三次全部一致）。这个
    特征很明确，所以直接按差值判定，而不是靠「连续两次相同」——抖动值本身
    也常常连续相同，指望它自己变化会误判。
    """
    if value is None or baseline is None:
        return False
    return abs((baseline - value) - _JITTER_DROP) < 1.0


async def _quota_after(baseline: float | None) -> float | None:
    """调用**之后**取稳定额度。

    实测的完整过程（三轮完全一致）::

        轮1: 364.113 → 384.077 → 383.758(稳定)
        轮2: 363.758 → 383.725(稳定)
        轮3: 363.185 → 383.138(稳定)

    两个必须处理的特性：

    1. **首采样恒为抖动值**（比真实值低 20）。它出现一次就消失，所以只要
       跳过「比后续读数低约 20」的那个就能等到真实值。
    2. **真实值本身还会再降一点**（384.077 → 383.758），那是另一笔消耗或
       上游的二次结算。所以不能拿「第一次非抖动值」当最终值，要再观察一会。

    策略：持续采样到截止时间，**取所有非抖动采样里的最小值**（额度只会下降，
    最终稳定水位就是这一段里的最小非抖动值）。
    """
    end = time.monotonic() + _QUOTA_POLL_TIMEOUT_S
    samples: list[float] = []
    while time.monotonic() < end:
        cur = await _quota_once()
        if cur is not None:
            samples.append(cur)
            # 收够一定数量的样本且已稳定（最后三次完全一致）就提前结束
            if len(samples) >= 4 and len(set(f"{v:.4f}" for v in samples[-3:])) == 1:
                break
        await asyncio.sleep(_QUOTA_POLL_GAP_S)
    if not samples:
        return None
    # 非抖动值：不小于「整组最大值 - 20」的都算真实读数
    top = max(samples)
    real = [v for v in samples if v > top - _JITTER_DROP / 2]
    if not real:
        return None
    return min(real)


def _spent_credit(before: float | None, after: float | None) -> float | None:
    """由前后额度算本次消耗；不可信时返回 None。

    ``after`` 已由 :func:`_quota_after` 过滤掉抖动值。这里还要处理
    **基线本身踩到抖动值**的情况（实测会发生）：那时差值是 ``-20 + 真实消耗``，
    用固定的抖动幅度把它修正回来。
    """
    if before is None or after is None:
        return None
    spent = before - after
    # 基线偏小 20（踩了抖动值）→ 差值约 -20，加回来
    if spent < 0 and abs(spent + _JITTER_DROP) < 1.0:
        spent = spent + _JITTER_DROP
    if spent <= 0 or spent > _MAX_SANE_CREDIT:
        log.debug("qwenpc 额度差 %.4f 不可信（before=%s after=%s），跳过",
                  spent, before, after)
        return None
    return round(spent, 6)


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

    # -- 额度（管理页「额度」卡） -------------------------------------------

    def quota(self) -> dict[str, Any] | None:
        """查额度卡数据，返回 ``{"items": [...], "level": ...}``。

        上游的个人版**没有配额分母**：``account-context`` 的 ``quota`` 只给
        ``remaining``，``total``/``used`` 都是 ``null``。所以这里只报剩余量，
        不编一个假分母（``percent`` 留 ``None``，界面不会画进度条）。
        """
        try:
            ctx = creds.account_context()
        except Exception as exc:  # noqa: BLE001 - 查额度失败不阻塞整页
            log.warning("qwenpc 额度查询失败: %s", type(exc).__name__)
            return None
        quota = ctx.get("quota")
        if not isinstance(quota, dict):
            return None
        remaining = quota.get("remaining")
        if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
            return None
        plan = ctx.get("plan") if isinstance(ctx.get("plan"), dict) else {}
        user = ctx.get("user") if isinstance(ctx.get("user"), dict) else {}
        label = str(user.get("name") or user.get("username") or "qwenpc")
        return {
            "items": [{
                "label": f"{label} · Credits",
                # 上游不给 total/used，只有 remaining —— 别编分母
                "used": None,
                "total": None,
                "remaining": round(float(remaining), 2),
                "percent": None,
                "reset_ts": None,
                "expire_ts": None,
                "unit": "credit",
            }],
            "level": str(plan.get("name") or "") or None,
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
            # ⚠ 基线必须在**发请求之前**取：请求一发出去上游就开始结算，
            # 之后再读已经是扣费后的值，差值会算成 0（非流式因此恒为 None，
            # 踩过）。注意它要放在 build_request 之前、紧邻 send。
            before = await _quota_before()
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
                self._passthrough(client, resp, before=before),
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
        # 非流式：响应体已完整，直接查额度差并塞进 usage。
        #
        # 为什么放 usage 而不是顶层：metrics 的非流式路径读 payload["usage"] 再走
        # normalize_usage，顶层字段它看不到（kiro 那边踩过同样的坑）。
        await self._fill_credit(payload, before)
        return JSONResponse(content=payload)

    async def _fill_credit(self, payload: dict[str, Any],
                           before: float | None) -> None:
        """查一次额度、算差值、写进 ``payload["usage"]["credit"]``。

        实测精度（``qwen3.8-max``，113 token）：扣 0.0434。上游**不提供**单次
        计费字段，只能靠前后额度差反推——比按倍率估算准得多。
        """
        if before is None:
            return
        after = await _quota_after(before)
        spent = _spent_credit(before, after)
        if spent is None:
            return
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = {}
            payload["usage"] = usage
        usage["credit"] = round(spent, 6)

    async def _passthrough(self, client: httpx.AsyncClient,
                           resp: httpx.Response, *,
                           before: float | None = None) -> AsyncIterator[bytes]:
        """流式原样透传 + 收尾补一个带 credit 的 usage 帧。

        上游的 SSE 已经在末尾给了 usage（含 cached_tokens），但**没有单次计费**。
        这里在流结束时查一次额度差，再补发一帧 ``choices: []`` 的 usage chunk
        ——``SSEUsageExtractor`` 只认带 ``usage`` 的行，最后一帧会覆盖前面的，
        所以客户端与 metrics 都能读到完整数字。

        为什么必须在**这里**（而不是异步后台任务）：metrics 的
        ``_metrics_stream`` 是在本生成器**结束之后**才写日志的。异步任务查回来时
        日志已经落库，插不回去了——所以查询必须发生在 yield 完最后一帧之前。
        查询本身只有 ~100ms，且只在流末尾发生一次。
        """
        usage_seen = False
        try:
            async for chunk in resp.aiter_bytes():
                if not chunk:
                    continue
                if b'"usage"' in chunk:
                    usage_seen = True
                yield chunk
        finally:
            await resp.aclose()
        # 流结束：查额度差，补一帧 usage
        if before is not None and usage_seen:
            after = await _quota_after(before)
            spent = _spent_credit(before, after)
            if spent is not None:
                yield _credit_chunk(spent)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client


def _credit_chunk(credit: float) -> bytes:
    """补发的 usage 帧（OpenAI 流式规范：usage chunk 的 choices 为空）。"""
    payload = {"object": "chat.completion.chunk", "choices": [],
               "usage": {"credit": credit}}
    return ('data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n').encode()
