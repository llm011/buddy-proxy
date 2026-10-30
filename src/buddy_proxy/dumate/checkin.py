"""DuMate 签到（bceConsole 通道）。

接口（2026-10-05 实测）：

- 查询：``GET  https://console.bce.baidu.com/api/dumate/points/loginBonusInfo``
  → ``{"result": {"totalTimes": N, "totalPoints": N, "hasIssued": bool,
       "signInDays": ["YYYY-MM-DD", ...]}}``
- 领取：``POST https://console.bce.baidu.com/api/dumate/points/loginBonus``
  （body ``{}``，需 ``csrftoken`` = ``bce-user-info`` cookie 值）
  → ``{"result": true}``

认证走 :mod:`buddy_proxy.dumate.cookies` 解出的百度云 cookie（App 登录态），
**不是** inapp key 通道。App 未登录 / cookie 过期时返回 None，由调用方兜底。

``totalPoints`` 是**累计签到所得**（不是当前余额——余额面板走 quotaOverview，
无公开接口）。签到积分与模型消耗积分是两个独立池，别拿 totalPoints 当余额。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from ..core.checkin import SOURCE_INFERRED, next_daily_reset
from .cookies import resolve_bceconsole_auth

log = logging.getLogger(__name__)

_BCE_CONSOLE = "https://console.bce.baidu.com"
_BONUS_INFO = f"{_BCE_CONSOLE}/api/dumate/points/loginBonusInfo"
_BONUS_CLAIM = f"{_BCE_CONSOLE}/api/dumate/points/loginBonus"
_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)


def fetch_checkin_status(timeout: float = 20.0) -> dict[str, Any] | None:
    """查询签到状态；未登录 / 失败返回 None。

    返回 ``{"checked_in": bool, "claimable": bool, "total_points": int,
    "total_times": int}``（total_points/total_times 拿不到时为 0）。

    ``timeout`` 默认给足（签到调度器调用）；面板状态接口这类高频只读场景
    传小值（如 3s）快速失败，别拖慢页面首渲。
    """
    auth = resolve_bceconsole_auth()
    if auth is None:
        return None
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
            resp = client.get(_BONUS_INFO, headers=auth.headers())
    except httpx.HTTPError as exc:
        log.debug("dumate checkin status fetch failed: %s", type(exc).__name__)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    result = data.get("result") or {}
    if not data.get("success", True) and not result:
        return None
    return _parse_status_payload(data)


def _parse_day_set(days: object) -> set[date]:
    """``signInDays`` → 干净的 ``date`` 集合（去重、剔坏值、与顺序无关）。

    「连续 N 天」与下发给日历的 ``sign_in_days`` 都从这一份解析来——上游哪天
    改口径（带时间戳、换分隔符）只需改这里，两处不会各自漂。
    """
    if not isinstance(days, (list, tuple)):
        return set()
    stamp: set[date] = set()
    for raw in days:
        if not isinstance(raw, str):
            continue
        try:
            stamp.add(datetime.strptime(raw.strip()[:10], "%Y-%m-%d").date())
        except ValueError:
            continue
    return stamp


def _sign_in_streak(days: object, now: float | None = None) -> int:
    """``signInDays``（``YYYY-MM-DD`` 列表）→ 截至今天/昨天的连续签到天数。

    上游给的是**完整签到日期列表**（实测 27 次 = 27 个日期，跨月也照给），
    面板上的「连续 N 天」由此直接算，不需要自己攒历史。今天还没签时从昨天
    往前数——否则每天零点一过「连续」就归零，用户刚打开页面看到 0 天会
    以为漏签了。

    日期按本地时区解析（与 ``signInDays`` 的自然日口径一致，见
    ``core.checkin.next_daily_reset`` 的同类说明）。列表里混进坏值就跳过，
    不为一个脏日期把整条状态打挂。
    """
    stamp = _parse_day_set(days)
    if not stamp:
        return 0
    moment = datetime.fromtimestamp(time.time() if now is None else now).date()
    cursor = moment if moment in stamp else moment - timedelta(days=1)
    streak = 0
    while cursor in stamp:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _normalize_sign_in_days(days: object) -> list[str]:
    """``signInDays`` → 干净的 ``YYYY-MM-DD`` 列表（去重、升序、剔坏值）。

    上游给的是日期列表，打卡日历与「连续 N 天」都从这里来；原样透传会把
    重复日期和脏值一路带到响应里，日历合并按字面串比对，脏值等于静默少一天。
    """
    return [d.isoformat() for d in sorted(_parse_day_set(days))]


def _parse_status_payload(data: dict[str, Any]) -> dict[str, Any]:
    """loginBonusInfo 响应 → 统一状态 dict（签领与查询共用解析）。"""
    result = data.get("result") or {}
    return {
        "checked_in": bool(result.get("hasIssued")),
        "claimable": not bool(result.get("hasIssued")),
        "total_points": int(result.get("totalPoints") or 0),
        "total_times": int(result.get("totalTimes") or 0),
        # 每日签到所得：totalPoints/totalTimes（已签次数），单次拿不到就 0。
        # 仅用于界面「每日 +X」展示，不是精确口径。
        "daily_credit": round(int(result.get("totalPoints") or 0)
                              / max(int(result.get("totalTimes") or 1), 1)),
        # signInDays 是真实签到日期列表：既交给 BenefitsManager 记日历
        # （``checkin_dates``，与 codebuddy 同字段名、同 ``YYYY-MM-DD`` 口径），
        # 也直接算出「连续 N 天」——上游给了日期列表，不用再攒本地历史。
        "sign_in_days": _normalize_sign_in_days(result.get("signInDays")),
        "checkin_dates": _normalize_sign_in_days(result.get("signInDays")),
        "streak_days": _sign_in_streak(result.get("signInDays")),
        # 每日零点轮换（百度签到按自然日），上游没给时间字段——同 trae/codebuddy
        # 按本地零点推断，标 inferred，界面注明。
        "next_ts": next_daily_reset(),
        "next_ts_source": SOURCE_INFERRED,
    }


def _bce_get(path: str, *, timeout: float = 20.0) -> dict[str, Any] | None:
    """带 cookie 的 bceConsole GET；未登录 / 失败返回 None。"""
    auth = resolve_bceconsole_auth()
    if auth is None:
        return None
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
            resp = client.get(f"{_BCE_CONSOLE}{path}", headers=auth.headers())
    except httpx.HTTPError as exc:
        log.debug("dumate bce GET %s failed: %s", path, type(exc).__name__)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not data.get("success", True):
        return None
    return data


def fetch_quota_overview(timeout: float = 20.0) -> dict[str, Any] | None:
    """查积分余额（quota_overview，bceConsole 通道，数字面板同口径）。

    实测（2026-10-05）桌面端抓包：App 的「积分」面板走
    ``GET /api/dumate/points/quota_overview?timezone=...&clientType=...&ignoreLoginBonus=true``
    （**下划线**版；camelCase ``quotaOverview`` 是另一个端点、怎么调都是 500）。
    返回 dict 含 ``total_points`` / ``used_points`` / ``remaining_points`` /
    ``is_subscribed`` / ``packages``（subscription 付费主池 + incremental 赠送包，
    ``total_points`` 就是两个池的合计）等；未登录 / 失败 None。
    """
    data = _bce_get(
        "/api/dumate/points/quota_overview"
        "?timezone=Asia%2FShanghai&clientType=mac&ignoreLoginBonus=true",
        timeout=timeout,
    )
    if data is None:
        return None
    result = data.get("result") or {}
    if not result:
        return None
    try:
        total = float(result.get("totalPoints") or 0)
        used = float(result.get("usedPoints") or 0)
    except (TypeError, ValueError):
        return None
    packages = []
    # 两个池：subscription（付费订阅主池，plan_pro 等）+ incremental（login_bonus
    # 赠送包）。totalPoints = 两者合计；只读 incremental 会漏掉订阅主池，面板明细
    # 跟总结行对不上（实测：totalPoints=26000 = subscription 25000 + incremental
    # 两个 500 包，但旧代码只列那两个包）。
    for p in (result.get("subscription") or []) + (result.get("incremental") or []):
        try:
            packages.append({
                "package_id": p.get("packageId") or "",
                "used_points": float(p.get("usedPoints") or 0),
                "total_points": float(p.get("totalPoints") or 0),
                "start_ts": p.get("startDate"),
                "expire_ts": p.get("expireDate"),
                "source": p.get("source") or "",
                "package_type": p.get("packageType") or "",
                "status": p.get("status") or "",
            })
        except (TypeError, ValueError):
            continue
    return {
        "total_points": total,
        "used_points": used,
        "remaining_points": max(total - used, 0.0),
        "is_subscribed": bool(result.get("isSubscribed")),
        "packages": packages,
        "throttled": bool((result.get("modelThrottleInfo") or {}).get("throttled")),
    }


def fetch_usage_records(
    *, start_ts: int, end_ts: int, page: int = 1, limit: int = 20, timeout: float = 15.0,
) -> dict[str, Any] | None:
    """查积分消耗流水（真实逐笔数据，bceConsole 通道）。

    实测（2026-10-05）App 抓包：``GET /api/dumate/points/records/usage``，
    query 参数 ``page`` / ``limit`` / ``startAt`` / ``endAt``，**秒级时间戳**
    （毫秒会报 500「系统出现错误」；缺 startAt 报「参数错误:StartAt」）。

    响应 ``{"code": 0, "success": true, "result": {"totalCount": N,
    "consumedPoints": "1239.96", "list": [{"createdAt": 秒, "pointsChange": "-8.88",
    "expectedPointsChange": "-8.88", "packageId": "...", "reason": ""}]}}``。
    ``consumedPoints`` 是该区间合计消耗（正数字符串），``pointsChange`` 是单笔
    带符号扣减。未登录 / 失败返回 None。
    """
    data = _bce_get(
        f"/api/dumate/points/records/usage?page={page}&limit={limit}"
        f"&startAt={int(start_ts)}&endAt={int(end_ts)}",
        timeout=timeout,
    )
    if data is None:
        return None
    result = data.get("result") or {}
    records: list[dict[str, Any]] = []
    for it in result.get("list") or []:
        try:
            records.append({
                "ts": int(it.get("createdAt") or 0),
                "points": float(it.get("pointsChange") or 0),
                "expected_points": float(it.get("expectedPointsChange") or 0),
                "package_id": it.get("packageId") or "",
                "conversation_name": it.get("conversationName") or "",
                "reason": it.get("reason") or "",
            })
        except (TypeError, ValueError):
            continue
    try:
        consumed = float(result.get("consumedPoints") or 0)
    except (TypeError, ValueError):
        consumed = 0.0
    return {
        "total_count": int(result.get("totalCount") or 0),
        "consumed_points": consumed,
        "records": records,
    }


def claim_checkin() -> dict[str, Any] | None:
    """领取今日签到；成功返回状态 dict，失败 / 未登录返回 None。"""
    auth = resolve_bceconsole_auth()
    if auth is None:
        return None
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(
                _BONUS_CLAIM, headers=auth.headers(), json={},
            )
    except httpx.HTTPError as exc:
        log.debug("dumate checkin claim failed: %s", type(exc).__name__)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not data.get("success") or not data.get("result"):
        return None
    return fetch_checkin_status() or {"checked_in": True, "claimable": False}
