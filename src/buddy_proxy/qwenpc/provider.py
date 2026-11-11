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
import re
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
#: ``credits`` 是相对倍率（``qwen3.8-max`` = 1.0 为基准）。
#:
#: **来自实测标定，不是估的**（2026-11-12）。上游没有任何「模型单价」接口，
#: 只能靠「调用前后额度差」反推，而反推有两个坑，标定时必须控住：
#:
#: 1. **缓存命中会大幅打折**。同一段 prompt 第二次调用（``cached_tokens`` 命中
#:    87%）扣费只有第一次的 1/3.7。所以标定必须用**每次全新随机内容**保证
#:    ``cached_tokens == 0``，否则测出来的数字偏低且不稳定。
#: 2. **结算有抖动窗口**。调用刚结束时上游返回的是临时值（低 5 或低 20），
#:    要等结算完成（约 1.7s）再读数。
#:
#: 标定方法：固定 1600 字随机输入、``max_tokens=1``（把输出压到最小）、
#: 每个模型测 2 次取均值、只保留 ``cached_tokens == 0`` 的样本。
#:
#: 结果（每千 token 单价 → 相对倍率）::
#:
#:     qwen3.8-max              0.2172  →  1.00
#:     qwen3.8-flash            0.0264  →  0.12
#:     glm-5.3                  0.1122  →  0.52
#:     glm-5.3-flash            0.0080  →  0.04
#:     deepseek-v4-pro          0.0902  →  0.42
#:     deepseek-v4-flash        0.0100  →  0.05
#:     kimi-k3.1-pro            0.5013  →  2.31
#:     kimi-k3.1-pro-highspeed  1.0041  →  4.63
#:
#: ⚠ 这组数字**只反映输入侧（冷缓存）**的计价比。实际扣费还受缓存命中率影响，
#: 同一段对话的第二轮往往只花第一轮的 1/4——所以倍率适合横向比较模型贵贱，
#: 不适合估算具体一次调用花多少（那要看日志里的真实 ``credit``）。
FALLBACK_MODELS: tuple[dict[str, Any], ...] = (
    # 每个系列**只留最新版**（用户 2026-11-11 要求）：有 glm-5.3 就不要 glm-5.2。
    # 上游确实同时提供旧版，但列出来只会让模型选择器变长、还容易选错。
    #
    # ---- Qwen 系 ----
    {"id": "qwen3.8-max", "name": "Qwen3.8-Max", "credits": 1.0, "images": True},
    {"id": "qwen3.8-flash", "name": "Qwen3.8-Flash", "credits": 0.12, "images": True},
    # ---- GLM 系（智谱）----
    {"id": "glm-5.3", "name": "GLM-5.3", "credits": 0.52},
    {"id": "glm-5.3-flash", "name": "GLM-5.3-Flash", "credits": 0.04},
    # ---- DeepSeek 系 ----
    {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro", "credits": 0.42},
    {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash", "credits": 0.05},
    # ---- Kimi 系（月之暗面）----
    {"id": "kimi-k3.1-pro", "name": "Kimi K3.1 Pro", "credits": 2.31},
    {"id": "kimi-k3.1-pro-highspeed", "name": "Kimi K3.1 Pro (Highspeed)", "credits": 4.63},
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
#: 采样次数。结算通常在 1.7s 内完成（实测 20 秒连续观察），但**偶尔会拖到
#: 5 秒以上**（并发/网络慢时）。0.7s × 8 次 ≈ 5.6s，覆盖最坏情况的绝大多数；
#: 再长就是在拿用户等待时间换一个参考数字，不值。
_QUOTA_POLL_SAMPLES = 8

_QUOTA_POLL_TIMEOUT_S = 10.0
_QUOTA_POLL_GAP_S = 0.7

#: 调用前采样的次数与间隔（取最大值，避开偶发抖动）。
_QUOTA_PRE_SAMPLES = 3
_QUOTA_SAMPLE_GAP_S = 0.35

#: 单次消耗的**兜底上界**（无 token 数据时用）。
#:
#: 真实消耗**不是固定量级**——它随请求规模线性增长（实测同一模型）：
#:
#: ======= ========== ========
#: 输入 token   扣费     每千 token
#: ======= ========== ========
#: 2 088     0.118    0.057
#: 10 088    1.353    0.134
#: 40 089   15.929    0.397
#: ======= ========== ========
#:
#: 所以**不能用一个固定阈值判断「是否异常」**——旧版写死 5.0，直接导致
#: 4 万 token 的请求（真实扣 15.9）被当成抖动丢掉，日志里只剩 None。
#: 现在按本次请求的 token 数动态算上界（见 :func:`_max_sane_credit`）。
_MAX_SANE_CREDIT = 5.0

#: 每千 token 的扣费上界（实测最贵 0.397/千，留 3 倍余量）。
_CREDIT_PER_1K_TOKENS_CAP = 1.2

#: 抖动的**已知幅度**（实测见过 20 和 5 两档）。
#:
#: 上游结算窗口内会先返回一个「真实值 - N」的中间值，N 不是固定的::
#:
#:     轮1: 365.3232 → 370.3055   （-5）
#:     轮2: 370.3055 → 370.2884   （无抖动，正常消耗）
#:     轮3: 365.2890 → 370.2713   （-5）
#:
#: 更早还观测到过 -20 的档位。所以**不能只按某个固定幅度过滤**——这里只用来
#: 做「事后修正」的候选，真正的主判据是下方 ``_quota_after`` 的「取最高值」。
#:
#: ⚠ 档位最小值取 2.0：真实单次消耗在 0.0x 量级，绝不会到 2；而把 1.0 这类
#: 小档位也列进来，会让「正常消耗 0.0171」被误判成抖动。
_JITTER_DROPS = (20.0, 5.0, 2.0)

#: 单次调用消耗的合理上界（实测真实值 0.03~0.05）。
_MAX_SANE_CREDIT = 1.0

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
    """该读数是否「比基线低一个已知抖动幅度」。

    只用于**日志/兜底判断**；主判据是 ``_quota_after`` 的取最高值策略
    （见那里的说明）——因为抖动的幅度不固定，靠特征匹配总有漏网的。
    """
    if value is None or baseline is None:
        return False
    drop = baseline - value
    return any(abs(drop - d) < 1.0 for d in _JITTER_DROPS)


async def _quota_after(baseline: float | None) -> float | None:
    """调用**之后**取真实水位。

    实测的确定性规律（20 秒连续观察）：::

        before      370.0116
        +0.1s       365.0122   ← 抖动（-5）
        +1.7s       369.9945   ← 真实值，之后一直不动
        ...
        +19.4s      369.9945

    **结算在 1.7 秒内完成**，之后读数完全稳定。抖动只会「先低后高」，所以
    **这一段采样里的最大值就是真实水位**——不必等固定时长，也不必按固定
    幅度去猜（实测见过 -20 和 -5 两档）。

    早先版本「连续两次一致就收」会踩到两个坑：抖动值自己连续一致（提前收，
    拿到偏低值）；以及结算还没开始时读数一直等于基线（差值为 0，积分丢失）。
    取最大值同时解决这两件事。
    """
    end = time.monotonic() + _QUOTA_POLL_TIMEOUT_S
    samples: list[float] = []
    while time.monotonic() < end:
        cur = await _quota_once()
        if cur is not None:
            samples.append(cur)
            # 采样窗口覆盖过结算点（> _SETTLE_CONFIRM_S）且已有稳定读数就收
            if len(samples) >= _QUOTA_POLL_SAMPLES:
                break
        await asyncio.sleep(_QUOTA_POLL_GAP_S)
    if not samples:
        return None
    top = max(samples)
    if baseline is not None and abs(top - baseline) < 1e-9:
        # 整段都等于基线：结算尚未落地，当次不记（宁缺毋错）
        return None
    return top


def _max_sane_credit(tokens: int | None) -> float:
    """按本次请求规模算「合理消耗」上界。

    有 token 数就按 :data:`_CREDIT_PER_1K_TOKENS_CAP` 算（+1 的固定余量给
    小请求），否则退回 :data:`_MAX_SANE_CREDIT`。
    """
    if isinstance(tokens, int) and tokens > 0:
        return max(_MAX_SANE_CREDIT, tokens / 1000.0 * _CREDIT_PER_1K_TOKENS_CAP + 1.0)
    return _MAX_SANE_CREDIT


def _spent_credit(before: float | None, after: float | None,
                  *, tokens: int | None = None) -> float | None:
    """由前后额度算本次消耗；不可信时返回 None。

    ``after`` 已由 :func:`_quota_after` 过滤掉抖动值。这里还要处理
    **基线本身踩到抖动值**的情况（实测会发生）：那时差值是 ``-20 + 真实消耗``，
    用固定的抖动幅度把它修正回来。
    """
    if before is None or after is None:
        return None
    spent = before - after
    # 基线踩了抖动值（偏小一个已知幅度）→ 差值约为负的那个幅度，加回来
    if spent < 0:
        for drop in _JITTER_DROPS:
            if abs(spent + drop) < 1.0:
                spent = spent + drop
                break
    if spent <= 0 or spent > _max_sane_credit(tokens):
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
        # ⚠ **不要**写 ``"used": None, "total": None``。
        #
        # 前端 ``quotaRemaining()`` 有一道判定：``used``/``total`` 这两个键
        # **存在但不是数字**时直接返回 null —— 于是整条被当成「查不到额度」，
        # 卡片顶部挂「无剩余额度」红标、且画不出进度条（用户 2026-11-11 报障：
        # 「剩 386 却说无剩余额度」）。
        #
        # 正确做法是**干脆不写这两个键**：语义是「上游没有这个概念」，而不是
        # 「有这个字段但值是空」。只留 ``remaining`` 就能被正常识别。
        item: dict[str, Any] = {
            "label": f"{label} · Credits",
            "remaining": round(float(remaining), 2),
            "unit": "credit",
        }
        return {
            "items": [item],
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
        # 用本次请求的 token 数算「合理消耗」上界：扣费随输入规模线性增长，
        # 固定阈值会把大请求误判成抖动（旧版写死 5.0，4 万 token 的请求被
        # 直接丢掉，日志里只剩 None）。
        _u = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        _n = (_u.get("prompt_tokens") or 0) + (_u.get("completion_tokens") or 0)
        spent = _spent_credit(before, after, tokens=_n or None)
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
        total_tokens: int | None = None
        try:
            async for chunk in resp.aiter_bytes():
                if not chunk:
                    continue
                if b'"usage"' in chunk:
                    usage_seen = True
                    m = _USAGE_TOKENS_RE.search(chunk)
                    if m:
                        try:
                            total_tokens = int(m.group(1))
                        except ValueError:
                            pass
                yield chunk
        finally:
            await resp.aclose()
        # 流结束：查额度差，补一帧 usage
        if before is not None and usage_seen:
            after = await _quota_after(before)
            spent = _spent_credit(before, after, tokens=total_tokens)
            if spent is not None:
                yield _credit_chunk(spent)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client


#: 从流式 chunk 里抠 ``total_tokens``（用于算合理消耗上界）。
_USAGE_TOKENS_RE = re.compile(r'"total_tokens"\s*:\s*(\d+)')


def _credit_chunk(credit: float) -> bytes:
    """补发的 usage 帧（OpenAI 流式规范：usage chunk 的 choices 为空）。"""
    payload = {"object": "chat.completion.chunk", "choices": [],
               "usage": {"credit": credit}}
    return ('data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n').encode()
