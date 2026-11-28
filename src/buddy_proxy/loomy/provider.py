"""Loomy（科大讯飞桌面 Agent）provider —— OpenAI 兼容网关直通。

## 上游形状（实测 2026-11-29，Loomy 0.9.39 / 生产网关）

Loomy 桌面端内核是 OpenCode，模型统一走一个 OpenAI 兼容网关::

    baseURL = https://loomyad.xunfei.cn/api/v1

    GET  /models              -> 模型目录（14 个：chat + image）
    POST /chat/completions    -> OpenAI chat（流式 SSE / 非流式 JSON）
    POST /images/generations  -> 生图（本通道暂不暴露，见下）

客户端按 ``/models`` 里每个模型的 ``protocol`` 字段分两个 provider
（``imodel`` = ``@ai-sdk/openai-compatible``，``imodel-anthropic`` =
``@ai-sdk/anthropic``），但**两者 baseURL 完全相同**——也就是说同一个网关
同时提供两种协议端点，走哪条由模型决定。实测当前目录里所有模型都是
``openai_chat``，所以本 provider 只实现 OpenAI 一条路，Anthropic 客户端
（``/v1/messages``）由路由层转成 chat 后再回来转成 Anthropic 事件流，
与 mimo / qoder 同一套做法。

## 鉴权：两个端点认的头不一样（踩过）

::

    POST /chat/completions   ->  token  ✅   Authorization: Bearer ✅
    GET  /models             ->  token  ✅   Authorization: Bearer ❌ (回「缺少 token」)

所以 :meth:`LoomyProvider._auth_headers` 对 chat 两个都带（客户端自己也是
两个都带），目录/积分请求只用 ``token``。统一都带 ``token`` 是最省心的写法。

**登录态失效是 HTTP 200 + 业务码** ``{"code":"100002"}``。只看状态码会把
「登录过期」当成功，然后把一个空 JSON 交给客户端——所以 :func:`_raise_for_envelope`
必须检查 body。

## 积分：每次请求的真实扣分在 ``usage.points_consumed``

Loomy 在标准 OpenAI ``usage`` 里多塞了三个字段：

::

    {"prompt_tokens":17,"completion_tokens":20,"total_tokens":37,
     "prompt_tokens_details":{},
     "completion_tokens_details":{"reasoning_tokens":20},
     "points_consumed":1}

``points_consumed`` 是服务端算的真实扣分，比按 token 估算可信。倍率只在
模型显示名里（``GLM 5.3 Flash(x0.8)``），见 ``catalog.parse_multiplier``。
本 provider 把两者都往日志里落：真实值给账，倍率给解释。

## reasoning 的两个坑

1. **思考链吃 ``max_tokens``**。``max_tokens=20`` 时 GLM-5.3-Flash 把 20 个
   token 全用在 ``reasoning_content`` 上，``content`` 是空串、
   ``finish_reason=length``。上游默认 ``default_reasoning_effort=low``，客户端
   调用方必须留足预算（本 provider 只在调用方**没给** max_tokens 时兜底）。
2. **``reasoning_effort: none`` 不保证关掉思考**。实测传了之后
   ``reasoning_tokens`` 仍有 75。所以不要指望它省 token，它只是让上游少想一点。

流式响应里 ``delta.reasoning_content`` 逐帧下发，与 ``delta.content`` 分开。
OpenAI 协议没有这个字段，客户端（Codex/Claude Code）不认，所以：
**流式 OpenAI 直通时保留原样**（不认识的字段客户端会忽略），转 Anthropic 时
由 ``AnthropicStreamConverter`` 映射成 ``thinking_delta``。
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

from ..providers.base import BaseProvider
from ..core.errors import describe_exception
from ..protocols.anthropic_adapter import (
    AnthropicStreamConverter,
    chat_completion_to_anthropic_message,
)
from . import catalog as catalog_mod
from . import credentials as creds
from . import points as points_mod
from .catalog import CatalogCache, LoomyModel
from .credentials import AUTH_ERROR_CODE, AuthError, LoomySession
from .points import PointsClient, PointsBalance, PetWorkStatus

log = logging.getLogger(__name__)

#: chat 读超时给到 600s：reasoning 模型首 token 可能很慢，且长上下文请求
#: 上传体本身就不小。write 给 180s 与 kiro 同口径（MB 级请求体实测需要）。
_TIMEOUT = httpx.Timeout(connect=20.0, read=600.0, write=180.0, pool=15.0)

#: 默认 max_tokens 兜底。**必须足够大**：思考链会吃掉预算，给小了就是空
#: content（见模块说明的坑 1）。上游 spark-x2.6 支持 131072，这里取一个
#: 对普通对话足够、又不至于让上游排队太久的中间值。
_DEFAULT_MAX_TOKENS = 8192


class LoomyProvider(BaseProvider):
    """Loomy 通道。"""

    id = "loomy"
    name = "Loomy (iFlytek)"

    #: 声明支持打卡：pet-work 每日奖励见 ``points.PointsClient``。
    supports_checkin = True

    def __init__(self) -> None:
        self._catalog = CatalogCache()
        self._session: LoomySession | None = None
        self._base_url: str = ""
        self._origin: str = ""
        self._last_catalog_error: str = ""

    # ------------------------------------------------------------------
    # 认证 / 目录
    # ------------------------------------------------------------------

    def ensure_auth(self) -> None:
        """校验登录态并预热模型目录；失败抛 401（带可操作提示）。"""
        try:
            session = creds.load_session()
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        self._session = session
        self._base_url = creds.resolve_base_url()
        self._origin = creds.resolve_origin(self._base_url)

        # 预热目录：失败**不阻断启动**（用户可能只是刚登录、网关抖一下），
        # 但要记下来，转发时再报更具体的错。
        try:
            models = self._fetch_catalog()
            self._catalog.store(models)
            self._last_catalog_error = ""
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 启动期目录失败不该阻断
            self._last_catalog_error = describe_exception(exc)
            log.warning("Loomy 模型目录预热失败（不影响启动）：%s", self._last_catalog_error)

    def _require_session(self) -> LoomySession:
        """取登录态；缓存失效时重读（用户可能刚在客户端里重登）。"""
        if self._session is None:
            try:
                self._session = creds.load_session()
            except AuthError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
        if not self._base_url:
            self._base_url = creds.resolve_base_url()
        if not self._origin:
            self._origin = creds.resolve_origin(self._base_url)
        return self._session

    def _refresh_session(self) -> LoomySession:
        """丢掉缓存的登录态重读一次（401 后重试用）。"""
        self._session = None
        self._catalog.invalidate()
        return self._require_session()

    def _auth_headers(self, *, bearer: bool = True) -> dict[str, str]:
        """鉴权头。

        ``token`` 恒带（目录/积分只认它）；chat 额外带 ``Authorization``
        （客户端自己也两个都带，兼容网关两种入参）。
        """
        session = self._require_session()
        headers = {"token": session.token}
        if bearer:
            headers["Authorization"] = f"Bearer {session.token}"
        return headers

    def _client_headers(self, *, bearer: bool = True, accept: str = "application/json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "Content-Type": "application/json",
            "loomy-version": creds.client_version(),
            **self._auth_headers(bearer=bearer),
        }
        return headers

    def _fetch_catalog(self) -> list[LoomyModel]:
        url = f"{self._base_url.rstrip('/')}/models"
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(url, headers=self._client_headers(bearer=False))
        _raise_for_envelope(resp, "/models")
        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"Loomy /models 响应不是 JSON：{exc}") from exc
        models = catalog_mod.parse_catalog(payload)
        if not models:
            raise RuntimeError("Loomy /models 未返回任何模型")
        return models

    def _ensure_catalog(self) -> list[LoomyModel]:
        """按需刷新目录。失败时**回落到旧缓存**（有的话）。"""
        if self._catalog.is_fresh():
            return self._catalog.models
        try:
            models = self._fetch_catalog()
            self._catalog.store(models)
            self._last_catalog_error = ""
            return models
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - 刷新失败用旧缓存
            self._last_catalog_error = describe_exception(exc)
            log.warning("Loomy 模型目录刷新失败，沿用缓存：%s", self._last_catalog_error)
            cached = self._catalog.models
            if cached:
                return cached
            raise

    def models(self) -> Sequence[dict[str, Any]]:
        """``/v1/models`` 的元素列表。

        目录拉不到时返回**空列表**而不是抛错：``/v1/models`` 是纯读接口，
        让它在网关抖动时整体 500 会把所有通道的模型列表一起拖垮。
        """
        try:
            models = self._ensure_catalog()
        except Exception as exc:  # noqa: BLE001
            log.warning("Loomy 模型列表不可用：%s", describe_exception(exc))
            return []
        return [m.to_openai_model(self.id) for m in models]

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        """只认目录内的 id（含 ``loomy/`` 前缀与显示名）。

        显示名当别名：Loomy 的 ``name`` 带倍率后缀（``GLM 5.3 Flash(x0.8)``），
        用户从截图里抄名字时很可能带上，所以 ``aliases=True`` 时也认显示名与
        去掉倍率后的干净名。
        """
        want = (model or "").strip()
        if not want:
            return False
        prefix = f"{self.id}/"
        bare = want[len(prefix):] if want.startswith(prefix) else want

        try:
            catalog = self._ensure_catalog()
        except Exception:  # noqa: BLE001 - 目录不可用时退化成前缀判断
            return False

        for m in catalog:
            if bare == m.id:
                return True
            if aliases and bare in (m.name, m.display_name):
                return True
        return False

    def context_window(self, model: str) -> int | None:
        """模型上下文窗口（``context_guard`` 截断用）。"""
        bare = _strip_prefix(model, self.id)
        try:
            for m in self._ensure_catalog():
                if m.id == bare:
                    return m.context_length
        except Exception:  # noqa: BLE001
            return None
        return None

    def model_multiplier(self, model: str) -> float | None:
        """模型倍率（从目录显示名里抽）；未知返回 ``None``。

        倍率只是**解释用**：真实扣分看 ``usage.points_consumed``。
        """
        bare = _strip_prefix(model, self.id)
        try:
            for m in self._ensure_catalog():
                if m.id == bare:
                    return m.multiplier
        except Exception:  # noqa: BLE001
            return None
        return None

    def health(self) -> dict[str, Any]:
        session = self._session
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self._base_url or creds.resolve_base_url(),
            "account": session.display if session else "",
            "models": len(self._catalog.models),
            "catalog_error": self._last_catalog_error,
        }

    # ------------------------------------------------------------------
    # 积分 / 打卡
    # ------------------------------------------------------------------

    def _points_client(self) -> PointsClient:
        self._require_session()
        return PointsClient(
            self._origin, self._require_session().token, version=creds.client_version()
        )

    def quota(self) -> dict[str, Any] | None:
        """积分余额 → 管理页额度卡。

        三个数是不同口径（见 ``points.PointsBalance``）：总额度 / 每日额度 /
        可用合计。管理页标题行取 ``available``（服务端算好的可用合计），
        明细里把三者都列出来。
        """
        try:
            balance: PointsBalance = self._points_client().balance()
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - 额度查询失败不该影响转发
            log.warning("Loomy 积分余额查询失败：%s", describe_exception(exc))
            return None

        items: list[dict[str, Any]] = [
            {
                "label": "可用积分",
                "used": 0,
                "total": balance.total,
                "remaining": balance.available,
                "percent": None,
                "unit": "credit",
            }
        ]
        if balance.balance:
            items.append(
                {
                    "label": "套餐额度",
                    "used": 0,
                    "total": balance.balance,
                    "remaining": balance.balance,
                    "percent": None,
                    "unit": "credit",
                }
            )
        if balance.daily_balance:
            items.append(
                {
                    "label": "每日额度",
                    "used": 0,
                    "total": balance.daily_balance,
                    "remaining": balance.daily_balance,
                    "percent": None,
                    "unit": "credit",
                }
            )
        # 三项是**并存的口径**（套餐 + 每日 = 可用），相加会重复计算 → 不置 sum_items
        return {"items": items, "level": None}

    def checkin_status(self) -> dict[str, Any] | None:
        """pet-work 今日进度。

        Loomy 没有点击签到（见 ``points`` 模块说明），所以这里的语义是
        「今日已领多少 / 是否还有可领档位」。
        """
        try:
            status: PetWorkStatus = self._points_client().pet_work_status()
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Loomy pet-work 状态查询失败：%s", describe_exception(exc))
            return None

        claimable_tier = status.highest_unclaimed_tier()
        next_tier = status.next_tier()
        if status.capped:
            message = f"今日已达上限，已领 {status.earned_today} 积分"
        elif claimable_tier is not None:
            message = f"可领 {claimable_tier.id}（档位分 {status.score}）"
        elif status.score_source == "none":
            message = "无法读取档位分数（Loomy 桌面端未运行或状态过期）"
        elif next_tier is not None:
            message = (
                f"今日档位分 {status.score}，距 {next_tier.id}"
                f"（{next_tier.score}）还差 {next_tier.score - status.score}"
            )
        else:
            message = f"今日档位分 {status.score}，全部档位已领（{status.earned_today} 积分）"

        return {
            "checked_in": bool(status.rewarded_tier_ids),
            "claimable": claimable_tier is not None and not status.capped,
            "inactive": not status.config.tiers,
            "streak_days": 0,
            "message": message,
            "earned_today": status.earned_today,
            "score": status.score,
            "score_source": status.score_source,
            "rewarded_tiers": list(status.rewarded_tier_ids),
            "daily_max_points": status.config.daily_max_points,
            "next_tier": next_tier.id if next_tier else "",
            "next_tier_score": next_tier.score if next_tier else 0,
        }

    def checkin_claim(self) -> dict[str, Any] | None:
        """领取 pet-work 已达成的最高档位。

        档位判定口径见 ``points`` 模块说明（保守：用 ``earnedPointsToday``
        当分数下界）。没有可领档位时**不发请求**，直接返回当前状态。
        """
        try:
            client = self._points_client()
            status = client.pet_work_status()
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Loomy pet-work 状态查询失败：%s", describe_exception(exc))
            return None

        tier = status.highest_unclaimed_tier()
        if tier is None or status.capped or not status.business_date or not status.config.version:
            # 无可领：返回状态即可（与其它通道「今天已打过」的语义对齐）
            return self.checkin_status()

        try:
            client.grant_pet_work_reward(
                business_date=status.business_date,
                config_version=status.config.version,
                tier_id=tier.id,
            )
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - 领奖失败不该让面板报错
            log.warning(
                "Loomy pet-work 领奖失败（tier=%s）：%s", tier.id, describe_exception(exc)
            )
            return self.checkin_status()

        log.info("Loomy pet-work 已领取 %s（+%s 分）", tier.id, tier.score)
        return self.checkin_status()

    def points_records(self, page: int = 1, size: int = 20) -> list[points_mod.PointsRecord]:
        """积分流水（管理页「最近扣分」用）。"""
        try:
            return self._points_client().records(page=page, size=size)
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Loomy 积分流水查询失败：%s", describe_exception(exc))
            return []

    # ------------------------------------------------------------------
    # 生图 / 视频
    # ------------------------------------------------------------------

    def image_models(self) -> list[str]:
        """目录里的生图/视频模型 id（``type == "image"``）。"""
        try:
            return [m.id for m in self._ensure_catalog() if m.is_image]
        except Exception:  # noqa: BLE001
            return []

    async def generate_image(
        self,
        prompt: str,
        *,
        model: str = "",
        size: str = "2304x1728",
        n: int = 1,
        response_format: str = "url",
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        """调上游 ``/images/generations``（文生图 / 图生图）。

        实测（2026-11-29）:

        - **鉴权只认 ``Authorization: Bearer``**。带 ``token`` 头会回
          ``100002 缺少 token``——与 chat（两个都收）和 ``/models``（只认
          token）都不同。三个端点三种脾气，别想当然。
        - **固定 110 积分/张**，与 ``size``（1024x1024 / 2304x1728）无关，
          与 ``n`` 也无关（传 ``n=2`` 仍只回 1 张、只扣一次）。
        - 响应是 ``{"created":..., "data":[{"url":"..."}], "points_consumed":110}``，
          **积分不在 usage 里**，是响应体顶层字段。
        - ``response_format`` 传 ``url`` 时给的是腾讯云 COS 预签名链接
          （带 ``q-sign-time``，会过期），不是长期地址。
        - **模型名不决定后端**：请求 ``doubao-seedream-5-lite`` /
          ``qwen-image-3.0-pro`` / ``doubao-seedream-5.5`` 三个不同模型，
          返回的 URL 里都是 ``hy_image_v3_5_preview``，账单也都记成
          ``Hy-Image-3.5-preview``。也就是当前网关把生图请求统一路由到同一个
          后端，模型名只是别名。所以**不要**按模型名给用户承诺不同的画质。
        """
        if not prompt.strip():
            raise HTTPException(status_code=400, detail="缺少 prompt")
        self._require_session()

        payload: dict[str, Any] = {
            "prompt": prompt,
            "n": max(1, int(n)),
            "size": size,
            "response_format": response_format,
        }
        if model:
            payload["model"] = _strip_prefix(model, self.id)
        if images:
            payload["images"] = images

        url = f"{self._base_url.rstrip('/')}/images/generations"
        # 生图端点的鉴权头：只认 Bearer（见 docstring）
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "loomy-version": creds.client_version(),
            "Authorization": f"Bearer {self._require_session().token}",
        }
        # 生图很慢（实测单张 20~40s，客户端给自己留了 5 分钟），超时单独放宽
        timeout = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code >= 400:
                detail = await _read_error(response)
                raise HTTPException(status_code=response.status_code, detail=detail)
            try:
                data = response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise HTTPException(
                    status_code=502, detail=f"Loomy 生图响应不是 JSON：{exc}"
                ) from exc

        _raise_for_body(data)
        points = data.get("points_consumed")
        if points is not None:
            # 生图的积分在响应体顶层，不在 usage 里；单独记一条，便于和 chat 区分
            log.info("Loomy 生图 model=%s size=%s n=%s points=%s",
                     payload.get("model", "-"), size, payload["n"], points)
        return data

    # ------------------------------------------------------------------
    # 转发
    # ------------------------------------------------------------------

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        """转发 chat 请求。

        - ``openai`` / ``responses``：上游就是 OpenAI 形状，直通（流式/非流式）
        - ``anthropic``：路由层已把 body 转成 chat，响应要**反向**转回
          Anthropic（流式用 ``AnthropicStreamConverter``，非流式用
          ``chat_completion_to_anthropic_message``）——漏转 Claude Code 会报
          「0 stream events received」
        """
        session = self._require_session()
        model = _strip_prefix(str(body.get("model") or ""), self.id)
        if not model:
            raise HTTPException(status_code=400, detail="缺少 model 字段")

        # 生图/视频模型（type=image）走 ``/images/generations``，再包成 chat 响应。
        # 项目惯例是**不开** ``/v1/images`` 端点（见 codex/images.py 模块说明）：
        # 客户端统一用 chat 协议点名模型，网关负责分流。这样 dsh / Claude Code
        # 这类只会 chat 的客户端也能生图。
        if self._is_image_model(model):
            return await self._forward_image(body, model, protocol, original)

        # 思考链会吃 max_tokens（见模块说明的坑 1）：调用方没给就兜底给足，
        # 给了就尊重——不要偷偷放大用户的预算，那会改变计费预期。
        if body.get("max_tokens") is None and body.get("max_completion_tokens") is None:
            body = {**body, "max_tokens": _DEFAULT_MAX_TOKENS}
        body = {**body, "model": model}

        stream = bool(body.get("stream"))
        url = f"{self._base_url.rstrip('/')}/chat/completions"

        if stream:
            return await self._forward_stream(url, body, protocol, session)
        return await self._forward_once(url, body, protocol, session)

    def _is_image_model(self, model: str) -> bool:
        """模型是否属于生图/视频类（目录里 ``type=image``）。

        目录拉不到时返回 ``False``——退化成按 chat 转发，让上游自己去报错，
        比在这里拦下来给一个「不知道」的错误更有用。
        """
        try:
            for m in self._ensure_catalog():
                if m.id == model:
                    return m.is_image
        except Exception:  # noqa: BLE001
            return False
        return False

    async def _forward_image(
        self,
        body: dict[str, Any],
        model: str,
        protocol: str,
        original: dict[str, Any] | None,
    ) -> StreamingResponse | JSONResponse:
        """生图模型：调 ``/images/generations``，把结果包成 chat / Anthropic 响应。

        为什么包成 chat：客户端（dsh / Claude Code / Codex）只会说 chat 协议。
        图片是一次性产物，流式与否都只在**最后一帧**给完整 markdown 图片链接，
        避免客户端以为卡死（与 codex 通道同口径）。
        """
        prompt = _extract_prompt(body)
        if not prompt:
            raise HTTPException(
                status_code=400,
                detail="生图请求缺少 prompt（放在 messages 的用户文本里）",
            )

        result = await self.generate_image(
            prompt,
            model=model,
            size=str(body.get("size") or "2304x1728"),
            n=int(body.get("n") or 1),
            response_format="url",
        )
        images = result.get("data") or []
        urls = [
            str(item.get("url") or "").strip()
            for item in images
            if isinstance(item, dict) and str(item.get("url") or "").strip()
        ]
        if not urls:
            raise HTTPException(status_code=502, detail="Loomy 生图未返回任何图片")

        # markdown 图片语法：终端/桌面客户端都能直接渲染
        text = "\n\n".join(f"![generated]({url})" for url in urls)
        points = result.get("points_consumed")
        if points is not None:
            text += f"\n\n（本次消耗 {points} 积分）"

        payload = {
            "id": f"loomy-img-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                # 生图的积分在响应体顶层，塞进 usage 才能被 normalize_usage 读到
                "points_consumed": points if points is not None else 0,
            },
        }

        if protocol == "anthropic":
            return JSONResponse(chat_completion_to_anthropic_message(payload, original))

        if bool(body.get("stream")):
            # 流式：先发 role 帧再发整图，客户端不会以为卡死
            async def frames() -> AsyncIterator[bytes]:
                first = {
                    "id": payload["id"],
                    "object": "chat.completion.chunk",
                    "created": payload["created"],
                    "model": model,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n".encode()
                last = {
                    "id": payload["id"],
                    "object": "chat.completion.chunk",
                    "created": payload["created"],
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}],
                    "usage": payload["usage"],
                }
                yield f"data: {json.dumps(last, ensure_ascii=False)}\n\n".encode()
                yield b"data: [DONE]\n\n"

            return StreamingResponse(
                frames(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        return JSONResponse(payload)

    async def _post(
        self,
        url: str,
        body: dict[str, Any],
        session: LoomySession,
        *,
        stream: bool,
    ) -> httpx.Response:
        """发请求并按 envelope 校验（401 时重读登录态重试一次）。"""
        headers = self._client_headers(
            accept="text/event-stream" if stream else "application/json"
        )
        client = httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            request = client.build_request("POST", url, headers=headers, json=body)
            response = await client.send(request, stream=stream)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(
                status_code=502, detail=f"Loomy 上游连接失败：{describe_exception(exc)}"
            ) from exc
        # 把 client 挂在 response 上，流式结束后由消费方关闭
        response._loomy_client = client  # type: ignore[attr-defined]

        if response.status_code == 401:
            await _close(response)
            # 登录态可能刚在客户端里续过：重读一次再试
            self._refresh_session()
            headers = self._client_headers(
                accept="text/event-stream" if stream else "application/json"
            )
            client = httpx.AsyncClient(timeout=_TIMEOUT)
            try:
                request = client.build_request("POST", url, headers=headers, json=body)
                response = await client.send(request, stream=stream)
            except httpx.HTTPError as exc:
                await client.aclose()
                raise HTTPException(
                    status_code=502,
                    detail=f"Loomy 上游连接失败：{describe_exception(exc)}",
                ) from exc
            response._loomy_client = client  # type: ignore[attr-defined]

        if response.status_code >= 400:
            detail = await _read_error(response)
            await _close(response)
            raise HTTPException(status_code=response.status_code, detail=detail)
        return response

    async def _forward_once(
        self, url: str, body: dict[str, Any], protocol: str, session: LoomySession
    ) -> JSONResponse:
        response = await self._post(url, body, session, stream=False)
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            await _close(response)
            raise HTTPException(
                status_code=502, detail=f"Loomy 响应不是 JSON：{exc}"
            ) from exc
        finally:
            await _close(response)

        _raise_for_body(payload)
        _log_points_usage(payload, body.get("model", ""), self.model_multiplier(body.get("model", "")))

        if protocol == "anthropic":
            original = body
            converted = chat_completion_to_anthropic_message(payload, original)
            return JSONResponse(converted)
        return JSONResponse(payload)

    async def _forward_stream(
        self, url: str, body: dict[str, Any], protocol: str, session: LoomySession
    ) -> StreamingResponse:
        response = await self._post(url, body, session, stream=True)

        if protocol == "anthropic":
            converter = AnthropicStreamConverter(body.get("model", ""))

            async def anthropic_events() -> AsyncIterator[str]:
                try:
                    async for chunk in _iter_chunks(response):
                        for event in converter.feed(chunk):
                            yield event
                    for event in converter.finish():
                        yield event
                finally:
                    await _close(response)

            return StreamingResponse(
                anthropic_events(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        async def passthrough() -> AsyncIterator[bytes]:
            """原样泵给客户端，顺手把末帧的 usage 落日志（积分/倍率）。"""
            tail = b""
            try:
                async for raw in response.aiter_bytes():
                    if not raw:
                        continue
                    tail = (tail + raw)[-8192:]
                    yield raw
            finally:
                await _close(response)
                _log_points_from_sse(tail, body.get("model", ""), self.model_multiplier(body.get("model", "")))

        return StreamingResponse(
            passthrough(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------


def _extract_prompt(body: dict[str, Any]) -> str:
    """从 chat / responses 请求体里抽出要画的 prompt。

    - 不含对话消息时认顶层 ``prompt`` 字段（OpenAI images 语义）
    - 否则取最后一条 user 消息的文本（多模态 content 只拼 text 部分）

    与 ``codex/images.extract_prompt`` 同口径，但这里不 import 它——两个通道
    互不依赖，生图分流逻辑各自独立演化，复制这 20 行比制造耦合划算。
    """
    if not body.get("messages") and not body.get("input"):
        direct = body.get("prompt")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if not isinstance(msg, dict) or str(msg.get("role")) != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                if content.strip():
                    return content.strip()
            elif isinstance(content, list):
                parts = [
                    str(p.get("text") or "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") in ("text", "input_text")
                ]
                text = "".join(parts).strip()
                if text:
                    return text
    return ""


def _strip_prefix(model: str, provider_id: str) -> str:
    """``loomy/GLM-5.3-Flash`` -> ``GLM-5.3-Flash``。"""
    prefix = f"{provider_id}/"
    return model[len(prefix):] if model.startswith(prefix) else model


def _raise_for_envelope(resp: httpx.Response, action: str) -> None:
    """检查网关 envelope（**HTTP 200 + code=100002 也算失效**）。"""
    if resp.status_code >= 400:
        raise RuntimeError(f"Loomy {action}：HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError):
        return  # 非 JSON 交给调用方处理
    if isinstance(payload, dict):
        code = str(payload.get("code") or "").strip()
        if code == AUTH_ERROR_CODE:
            raise AuthError(
                "Loomy 登录态已失效（上游 code=100002）。"
                "请重新登录 Loomy 桌面端，然后重启 buddy。"
            )
        if code and code != points_mod.OK_CODE:
            desc = str(payload.get("desc") or "").strip() or "未知错误"
            raise RuntimeError(f"Loomy {action} 失败：{desc}（code={code}）")


def _raise_for_body(payload: Any) -> None:
    """非流式响应体里的业务错误码检查。"""
    if not isinstance(payload, dict):
        return
    code = str(payload.get("code") or "").strip()
    if code == AUTH_ERROR_CODE:
        raise HTTPException(
            status_code=401,
            detail=(
                "Loomy 登录态已失效（上游 code=100002）。"
                "请重新登录 Loomy 桌面端，然后重启 buddy。"
            ),
        )
    if code and code != points_mod.OK_CODE and "choices" not in payload:
        desc = str(payload.get("desc") or "").strip() or "未知错误"
        raise HTTPException(status_code=502, detail=f"Loomy 上游错误：{desc}（code={code}）")


def _log_points_usage(payload: dict[str, Any], model: str, multiplier: float | None = None) -> None:
    """把一次非流式请求的 token / 积分 / 倍率写进日志。

    ``multiplier`` 由调用方从目录里查好传进来——倍率只存在于模型的**显示名**
    （``GLM 5.3 Flash(x0.8)``），模型 id 里没有，所以这里不能自己从 ``model``
    解析。
    """
    usage = payload.get("usage")
    summary = points_mod.describe_points_usage(usage if isinstance(usage, dict) else None)
    if not summary:
        return
    suffix = f" x{multiplier:g}" if multiplier is not None else ""
    log.info("Loomy 用量 model=%s%s %s", model, suffix, summary)


def _log_points_from_sse(tail: bytes, model: str, multiplier: float | None = None) -> None:
    """从流式响应的尾部字节里抠最后一帧 usage，落日志。

    流式时 usage 只在**末帧**出现（且需要客户端请求里带
    ``stream_options.include_usage``，部分上游无条件下发）。尾部取 8KB 足够
    覆盖末帧 + ``[DONE]``；抠不到就静默跳过，不能因为日志失败影响转发。
    """
    if not tail:
        return
    try:
        text = tail.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return
    best: dict[str, Any] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(chunk, dict) and isinstance(chunk.get("usage"), dict):
            best = chunk
    if best is not None:
        _log_points_usage(best, model, multiplier)


async def _iter_chunks(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """把上游 SSE 解成 chunk dict（供转 Anthropic 事件流）。"""
    async for line in response.aiter_lines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            return
        try:
            chunk = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(chunk, dict):
            yield chunk


async def _read_error(response: httpx.Response) -> str:
    try:
        raw = await response.aread()
    except Exception:  # noqa: BLE001
        return f"Loomy 上游 HTTP {response.status_code}"
    text = raw.decode("utf-8", errors="ignore").strip()
    if not text:
        return f"Loomy 上游 HTTP {response.status_code}"
    # 网关错误体是 {"code":"...","desc":"..."}；抽 desc 给用户看，原文只留截断
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return f"Loomy 上游 HTTP {response.status_code}: {text[:300]}"
    if isinstance(payload, dict):
        desc = str(payload.get("desc") or "").strip()
        code = str(payload.get("code") or "").strip()
        if desc:
            return f"Loomy 上游错误：{desc}（code={code or response.status_code}）"
    return f"Loomy 上游 HTTP {response.status_code}: {text[:300]}"


async def _close(response: httpx.Response) -> None:
    """关闭响应及其背后的 client。"""
    client = getattr(response, "_loomy_client", None)
    try:
        await response.aclose()
    except Exception:  # noqa: BLE001
        pass
    if client is not None:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
