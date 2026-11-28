"""Loomy 积分：余额 / 账单 / 每日 pet-work 奖励。

## 为什么这个通道的「签到」不是签到

Loomy 客户端里**没有**传统的每日点击签到（所有积分端点都翻过：没有
``checkin`` / ``sign-in`` 之类）。替代品是 **pet-work（宠物打工）**：

- 客户端按你**当天真实使用量**算分：``sessionWeight 0.4`` + ``depthWeight 0.6``
- 达到档位后调 ``POST /api/v1/pet-work/rewards`` 领奖，每日上限 1000 分
- 档位：``tier_1=60`` / ``tier_2=100`` / ``tier_3=150``

所以本模块的 ``checkin_status`` 语义是「今天领到多少 / 下一档还差多少」，
``checkin_claim`` 是「把已达成的最高档位领掉」。

## 档位判定：为什么本地算

服务端快照只回 ``status.rewardedTierIds`` / ``earnedPointsToday``，**不回**
「当前达成哪一档」——那个由客户端按会话数与深度算。本实现照客户端的口径复刻：

::

    score = sessionWeight * min(sessionIndex, maxSessionIndex)
          + depthWeight   * min(depthIndex,   maxDepthIndex)

``sessionIndex`` / ``depthIndex`` 是我们能从快照里读到的最可靠的代理量：
``earnedPointsToday`` 已经包含历次领奖，直接拿它比档位会重复领（服务端会拒，
但会浪费一次请求并留下失败日志）。因此：

- **保守**：只用「今天还没领过的档位里，``earnedPointsToday`` 已经覆盖的」
  去领——也就是把 ``earnedPointsToday`` 当分数下界。
- 若下界覆盖不到任何未领档位，返回 ``claimable=False``，**不发请求**。

这样最坏情况是「该领没领」（下次使用量涨上去自然会领），而不是「乱领被拒」。

## 接口（实测）

::

    GET  {origin}/api/v1/points/balance
         -> {"code":"000000","data":{"balance":5000,"dailyBalance":4989,
                                      "availableBalance":9989}}
    GET  {origin}/api/v1/points/records?pageNo=1&pageSize=10
         -> data.list[] 含 modelName / pointsActual / balanceBefore / balanceAfter
    GET  {origin}/api/v1/pet-work
         -> data.config{tiers, dailyMaxPoints, sessionWeight, depthWeight, ...}
            data.status{earnedPointsToday, rewardedTierIds, dailyRewardCapped}
    POST {origin}/api/v1/pet-work/rewards
         body {"businessDate","configVersion","reachedTierId"}

**鉴权只认 ``token`` 头**（不是 Bearer）。这一点和 chat 端点不同——chat 两种
都收，积分端点只认 ``token``，用 Bearer 会回 ``100002 缺少 token``。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import credentials
from .credentials import AUTH_ERROR_CODE, OK_CODE, AuthError

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=20.0, pool=10.0)


@dataclass(frozen=True)
class PointsBalance:
    """积分余额。三个数是**不同口径**，别混："""

    #: 套餐/充值总额度（长期）
    balance: int = 0
    #: 每日赠送额度（每日轮换）
    daily_balance: int = 0
    #: 当前可用合计（上面两者之和，服务端算好给的）
    available: int = 0

    @property
    def total(self) -> int:
        """展示用的总量。优先用服务端给的 ``available``。"""
        return self.available or (self.balance + self.daily_balance)


@dataclass(frozen=True)
class PointsRecord:
    """一条积分流水。"""

    model: str = ""
    points: int = 0
    direction: str = ""
    description: str = ""
    created_at: int = 0
    balance_before: int = 0
    balance_after: int = 0
    source: str = ""


@dataclass(frozen=True)
class PetWorkTier:
    id: str
    score: int


@dataclass(frozen=True)
class PetWorkConfig:
    """pet-work 规则（服务端下发，**不要**在代码里写死档位）。"""

    version: str = ""
    daily_max_points: int = 0
    tiers: tuple[PetWorkTier, ...] = ()
    session_weight: float = 0.4
    depth_weight: float = 0.6
    max_session_index: int = 2
    max_depth_index: int = 2
    sample_window_days: int = 28
    minimum_active_days: int = 7


@dataclass(frozen=True)
class PetWorkStatus:
    """今日 pet-work 状态。

    **``earned_today`` 是积分，``score`` 才是档位分数**——两者量纲完全不同，
    混用会算错档位（见 :meth:`highest_unclaimed_tier`）。
    """

    business_date: str = ""
    earned_today: int = 0
    rewarded_tier_ids: tuple[str, ...] = ()
    capped: bool = False
    config: PetWorkConfig = field(default_factory=PetWorkConfig)
    #: 客户端本地算出的档位分数（0–``(sessionWeight+depthWeight)*maxIndex*100``）
    score: int = 0
    #: 分数来源：``local``（读到客户端本地状态）/ ``derived``（按公式推算）/ ``none``
    score_source: str = "none"

    def highest_unclaimed_tier(self) -> PetWorkTier | None:
        """已达成的**最高**未领档位。

        判据是 ``score``（档位分数），不是 ``earned_today``（积分）。档位分数
        由客户端按 ``sessionIndex``/``depthIndex`` 归一化算出，取值 0–200；
        而 ``tier_1=60`` / ``tier_2=100`` / ``tier_3=150`` 比的就是这个分数。

        分数未知（``score_source='none'``）时返回 ``None``——**不发请求**。
        宁可今天少领一次，也不要拿错的分数去撞服务端校验。
        """
        if self.score_source == "none":
            return None
        rewarded = set(self.rewarded_tier_ids)
        candidates = [
            tier
            for tier in self.config.tiers
            if tier.id not in rewarded and tier.score <= self.score
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda t: t.score)

    def next_tier(self) -> PetWorkTier | None:
        """还没达成的最低档位（UI 显示「还差多少」用）。"""
        if self.score_source == "none":
            return None
        rewarded = set(self.rewarded_tier_ids)
        pending = [
            tier
            for tier in self.config.tiers
            if tier.id not in rewarded and tier.score > self.score
        ]
        if not pending:
            return None
        return min(pending, key=lambda t: t.score)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _check_envelope(payload: Any, action: str) -> dict[str, Any]:
    """校验网关 envelope，成功返回 ``data``；登录失效抛 :class:`AuthError`。

    网关**恒回 HTTP 200**，成败只看 body 里的 ``code``——只看状态码会把
    ``100002 登录已失效`` 当成功。
    """
    if not isinstance(payload, dict):
        raise RuntimeError(f"Loomy {action}：响应不是 JSON 对象")
    code = str(payload.get("code") or "").strip()
    desc = str(payload.get("desc") or "").strip()
    # 100002 是复合码：「登录已失效」与「缺少 token」同码，必须看 desc 才能区分
    if credentials.is_auth_failure(code, desc):
        raise AuthError(credentials.describe_auth_code(code, desc))
    if code and code != OK_CODE:
        raise RuntimeError(
            credentials.describe_auth_code(code, desc, action) if desc
            else f"Loomy {action} 失败：未知错误（code={code}）"
        )
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _local_pet_work_daily(user_key: str = "") -> dict[str, Any]:
    """读 Loomy 桌面端本地的 pet-work 当日状态（``pet-work.json``）。

    为什么必须读它：档位分数 ``petWorkScore`` 是**客户端本地算**的
    （``electron/pet/pet-work-service.js`` 的 ``calculatePetWorkMetrics``）::

        sessionIndex = min(activeSessionCount / sessionBaseline, maxSessionIndex)
        depthIndex   = min(extraValidTurnCount / depthBaseline,   maxDepthIndex)
        score        = round((sessionIndex * sessionWeight
                            + depthIndex   * depthWeight) * 100)

    服务端 ``/pet-work`` 快照**只回积分**（``earnedPointsToday``），不回分数。
    所以想正确判定「够不够 tier_N」，只有两条路：读本地状态，或者自己维护
    会话/深度计数。前者简单且不会与服务端漂移，因此优先。

    读不到（文件不存在、结构变化、user 对不上）就返回 ``{}``，由调用方退回
    ``score_source='none'``——**不要瞎猜分数**。
    """
    path = _pet_work_state_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    root = raw.get("petWork")
    if not isinstance(root, dict):
        return {}
    users = root.get("users")
    if not isinstance(users, dict):
        return {}

    if user_key and isinstance(users.get(user_key), dict):
        entry = users[user_key]
    else:
        # 只有单账号时直接取；多账号又不知道是哪个 → 不猜
        if len(users) != 1:
            return {}
        entry = next(iter(users.values()))
    if not isinstance(entry, dict):
        return {}
    daily = entry.get("daily")
    return daily if isinstance(daily, dict) else {}


def _pet_work_state_file() -> Path:
    """``pet-work.json`` 路径（与 ``auth-session.json`` 同目录）。"""
    override = os.getenv("LOOMY_PET_WORK_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return _app_support_dir() / "pet-work.json"


def _app_support_dir() -> Path:
    """Loomy 用户数据目录（复用 credentials 的解析口径）。"""
    return credentials._app_support_dir()  # noqa: SLF001 - 同一模块族的路径解析


class PointsClient:
    """积分 / pet-work 的同步客户端（调用方用 ``asyncio.to_thread`` 包装）。"""

    def __init__(self, origin: str, token: str, *, version: str = "") -> None:
        self._origin = origin.rstrip("/")
        self._token = token
        self._version = version

    def _headers(self) -> dict[str, str]:
        # 积分端点**只认 token 头**（Bearer 会回「缺少 token」），与 chat 不同。
        headers = {"token": self._token, "Accept": "application/json"}
        if self._version:
            headers["loomy-version"] = self._version
        return headers

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._origin}{path}"
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(url, headers=self._headers(), params=params or {})
        return self._parse(resp, path)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._origin}{path}"
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(url, headers=self._headers(), json=body)
        return self._parse(resp, path)

    @staticmethod
    def _parse(resp: httpx.Response, path: str) -> dict[str, Any]:
        if resp.status_code >= 400:
            raise RuntimeError(f"Loomy {path}：HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(f"Loomy {path}：响应不是 JSON（{exc}）") from exc
        return _check_envelope(payload, path)

    # -- 余额 / 账单 ---------------------------------------------------

    def balance(self) -> PointsBalance:
        data = self._get("/api/v1/points/balance")
        return PointsBalance(
            balance=_to_int(data.get("balance")),
            daily_balance=_to_int(data.get("dailyBalance")),
            available=_to_int(data.get("availableBalance")),
        )

    def records(self, page: int = 1, size: int = 20) -> list[PointsRecord]:
        data = self._get(
            "/api/v1/points/records", {"pageNo": max(1, page), "pageSize": max(1, size)}
        )
        items = data.get("list")
        if not isinstance(items, list):
            return []
        out: list[PointsRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            out.append(
                PointsRecord(
                    model=str(item.get("modelName") or ""),
                    points=_to_int(item.get("pointsActual")),
                    direction=str(item.get("direction") or ""),
                    description=str(item.get("description") or ""),
                    created_at=_to_int(item.get("createdAt")),
                    balance_before=_to_int(item.get("balanceBefore")),
                    balance_after=_to_int(item.get("balanceAfter")),
                    source=str(item.get("consumeSource") or ""),
                )
            )
        return out

    # -- 明细（UI「📜 明细」弹窗用）------------------------------------

    def records_with_total(
        self, page: int = 1, size: int = 30
    ) -> tuple[list[dict[str, Any]], int]:
        """逐笔积分流水（``/api/v1/points/records``），返回 ``(行, 总数)``。

        每笔一次模型调用。字段见 :meth:`records`，这里转成 UI 的通用行形状
        （与 trae / codebuddy 的 usage-logs 同构，前端一套渲染逻辑复用）。

        ``direction`` 有 ``debit``（扣分）/ ``credit``（发放，如兑换码），两类
        都要——只显示扣分会让人以为「兑换的 5000 分凭空消失」。
        """
        data = self._get(
            "/api/v1/points/records",
            {"pageNo": max(1, page), "pageSize": max(1, min(size, 100))},
        )
        items = data.get("list")
        total = _to_int(data.get("total"))
        rows: list[dict[str, Any]] = []
        if not isinstance(items, list):
            return rows, total

        for item in items:
            if not isinstance(item, dict):
                continue
            ts = _to_int(item.get("createdAt"))
            points = _to_int(item.get("pointsActual"))
            direction = str(item.get("direction") or "")
            before = _to_int(item.get("balanceBefore"))
            after = _to_int(item.get("balanceAfter"))
            rows.append({
                # 发放是正向：UI 用正负号区分收支，比多一列「方向」直观
                "credits": points if direction != "credit" else points,
                "currency": "credits",
                "direction": direction,
                "model": str(item.get("modelName") or ""),
                "event": str(item.get("description") or ""),
                "ts": ts or None,
                "time_raw": "",
                "balance_before": before,
                "balance_after": after,
                "source": str(item.get("consumeSource") or ""),
                "cycle_date": str(item.get("dailyCycleDate") or ""),
                "ledger_id": str(item.get("ledgerId") or ""),
            })
        return rows, total

    def records_by_purpose(
        self, page: int = 1, size: int = 30
    ) -> tuple[list[dict[str, Any]], int]:
        """按功能分类的消耗汇总（``/api/v2/points/records`` + ``granularity=purpose``）。

        比逐笔流水更适合回答「钱花在哪类调用上」：``chat.message`` /
        ``image.generate`` 各一行，带 ``ledger_count``（这行聚合了多少笔）。

        注意上游 v2 的 ``total`` 是**聚合后的组数**，不是流水笔数。
        """
        data = self._get(
            "/api/v2/points/records",
            {
                "pageNo": max(1, page),
                "pageSize": max(1, min(size, 100)),
                "direction": "debit",
                "granularity": "purpose",
            },
        )
        items = data.get("list")
        total = _to_int(data.get("total"))
        rows: list[dict[str, Any]] = []
        if not isinstance(items, list):
            return rows, total

        for item in items:
            if not isinstance(item, dict):
                continue
            first = _to_int(item.get("firstCreatedAt"))
            last = _to_int(item.get("lastCreatedAt"))
            rows.append({
                "credits": _to_int(item.get("points")),
                "currency": "credits",
                "direction": str(item.get("direction") or "debit"),
                "event": str(item.get("purpose") or ""),
                "model": str(item.get("sourceEndpoint") or ""),
                "ts": last or first or None,
                "time_raw": "",
                "ledger_count": _to_int(item.get("ledger_count")),
                "first_ts": first or None,
                "last_ts": last or None,
            })
        return rows, total

    # -- pet-work ------------------------------------------------------

    def pet_work_status(self) -> PetWorkStatus:
        data = self._get("/api/v1/pet-work")
        config_raw = data.get("config") if isinstance(data.get("config"), dict) else {}
        status_raw = data.get("status") if isinstance(data.get("status"), dict) else {}

        tiers: list[PetWorkTier] = []
        raw_tiers = config_raw.get("tiers")
        if isinstance(raw_tiers, list):
            for item in raw_tiers:
                if not isinstance(item, dict):
                    continue
                tier_id = str(item.get("tierId") or "").strip()
                if not tier_id:
                    continue
                tiers.append(PetWorkTier(id=tier_id, score=_to_int(item.get("score"))))
        # 档位按分数升序，后面取「最高已达成」依赖这个顺序
        tiers.sort(key=lambda t: t.score)

        config = PetWorkConfig(
            version=str(config_raw.get("version") or "").strip(),
            daily_max_points=_to_int(config_raw.get("dailyMaxPoints")),
            tiers=tuple(tiers),
            session_weight=_to_float(config_raw.get("sessionWeight"), 0.4),
            depth_weight=_to_float(config_raw.get("depthWeight"), 0.6),
            max_session_index=_to_int(config_raw.get("maxSessionIndex"), 2),
            max_depth_index=_to_int(config_raw.get("maxDepthIndex"), 2),
            sample_window_days=_to_int(config_raw.get("sampleWindowDays"), 28),
            minimum_active_days=_to_int(config_raw.get("minimumActiveDays"), 7),
        )

        raw_rewarded = status_raw.get("rewardedTierIds")
        rewarded = tuple(
            str(x).strip() for x in (raw_rewarded or []) if str(x).strip()
        ) if isinstance(raw_rewarded, list) else ()

        business_date = str(data.get("businessDate") or "").strip()

        # 档位分数只能从客户端本地状态读（见 _local_pet_work_daily 的说明）。
        # 本地状态必须是**同一个业务日**的，否则是昨天的分数，不能拿来领今天的奖。
        local = _local_pet_work_daily()
        score = 0
        score_source = "none"
        local_date = str(local.get("businessDate") or "").strip()
        if local and (not business_date or local_date == business_date):
            if isinstance(local.get("petWorkScore"), (int, float)):
                score = int(local["petWorkScore"])
                score_source = "local"

        return PetWorkStatus(
            business_date=business_date,
            earned_today=_to_int(status_raw.get("earnedPointsToday")),
            rewarded_tier_ids=rewarded,
            capped=status_raw.get("dailyRewardCapped") is True,
            config=config,
            score=score,
            score_source=score_source,
        )

    def grant_pet_work_reward(
        self, *, business_date: str, config_version: str, tier_id: str
    ) -> dict[str, Any]:
        """申请一档奖励。**只发服务端定义的三个字段**（客户端同样如此）。"""
        return self._post(
            "/api/v1/pet-work/rewards",
            {
                "businessDate": business_date,
                "configVersion": config_version,
                "reachedTierId": tier_id,
            },
        )


def describe_points_usage(usage: dict[str, Any] | None) -> str:
    """把一次 chat 的 usage 描述成一行日志文本。

    ``points_consumed`` 是 Loomy 的非标准字段（服务端算的真实扣分），
    ``normalize_usage`` 读不到它，所以要在这里单独呈现。
    """
    if not isinstance(usage, dict):
        return ""
    points = usage.get("points_consumed")
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    details = usage.get("completion_tokens_details")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None

    parts: list[str] = []
    if prompt is not None:
        parts.append(f"in={prompt}")
    if completion is not None:
        parts.append(f"out={completion}")
    if reasoning:
        parts.append(f"reasoning={reasoning}")
    if points is not None:
        parts.append(f"points={points}")
    return " ".join(parts)


def now_ts() -> int:
    return int(time.time())
