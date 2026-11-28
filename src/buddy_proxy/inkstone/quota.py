"""书生·端砚 额度（Token Plan）：``/api/tokenplan/v1/credits/balance``。

## 接口在哪个前缀下（踩过）

端砚的推理/会话接口在 ``/api/orbit/v1``，但**额度接口不在那儿**：:

    GET https://discovery.intern-ai.org.cn/api/tokenplan/v1/credits/balance   ✅
    GET https://discovery.intern-ai.org.cn/api/orbit/v1/tokenplan/...         ❌ 404

按 orbit 前缀拼一律 ``404 page not found``——那是 nginx 层就没有这个路由，不是
路径写错，光看 404 容易以为是接口改名了。

## 返回结构（实测 2026-11-29）

::

    {"code":0,"msg":"success","data":{
        "access_status":1,
        "rpm_limit":50,
        "decision_rpm_limit":200,
        "tpm_limit":2000000,
        "available_credits":"9999.851",     # 字符串！不是数字
        "active_package_count":1,
        "usage_windows":{
            "5h":{"enabled":true,
                  "limit_credits":"10000.000",
                  "used_credits":"0.149",
                  "remaining_credits":"9999.851",
                  "next_recover_at":"2026-11-29 07:36:34"},
            "7d":{"enabled":true,
                  "limit_credits":"50000.000",
                  "used_credits":"0.149",
                  "remaining_credits":"49999.851",
                  "next_recover_at":"2026-12-06 02:36:34"}},
        "calculated_at":"2026-11-29 03:06:17"}}

两个窗口是**并存的约束**（5 小时滚动窗 + 7 天窗），不是「套餐 + 每日」那种相加
关系——所以不能设 ``sum_items``，否则界面会把两个窗口的剩余量加起来显示成一个
根本不存在的总额。

## 时间字段：上游给的是北京时间字符串

``next_recover_at`` / ``calculated_at`` 形如 ``2026-11-29 07:36:34``，**没有时区
标记**，实测是东八区。转 epoch 时按 ``+08:00`` 解析；解析不出来就填 ``None``，
让界面不显示倒计时——比显示一个错的时刻诚实。

这两个时间是 ``reset_ts``（周期性回满），**不是** ``expire_ts``（权益作废）。
混用会让到期横幅误报「5 小时后到期」——那其实是「5 小时后回满」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

#: 额度查询超时（秒）。
_TIMEOUT = httpx.Timeout(15.0)

#: 上游时间字符串所在时区（东八区）。
_UPSTREAM_TZ = timezone(timedelta(hours=8))

#: 窗口展示顺序与中文名。``5h`` 在前——用户更关心「现在还能不能用」。
_WINDOW_LABELS = {
    "5h": ("5 小时窗口", "rolling_5h"),
    "7d": ("7 天窗口", "weekly"),
}


@dataclass(frozen=True)
class UsageWindow:
    """一个用量窗口（5 小时 / 7 天）。"""

    key: str
    limit: float = 0.0
    used: float = 0.0
    remaining: float = 0.0
    enabled: bool = True
    next_recover_at: int | None = None


@dataclass(frozen=True)
class TokenPlanBalance:
    """一份额度快照。"""

    available: float = 0.0
    windows: tuple[UsageWindow, ...] = ()
    rpm_limit: int = 0
    tpm_limit: int = 0
    access_status: int = 0
    package_count: int = 0
    calculated_at: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


def _to_float(value: Any) -> float:
    """上游金额是字符串（``"9999.851"``）；转不出来按 0。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_upstream_ts(value: Any) -> int | None:
    """``"2026-11-29 07:36:34"`` → epoch 秒（按东八区）。

    解析不出来返回 ``None``——**不要**退回当前时间，那会让界面显示「0 秒后
    重置」这种假倒计时。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=_UPSTREAM_TZ)
        except ValueError:
            continue
        return int(dt.timestamp())
    return None


def parse_balance(payload: Any) -> TokenPlanBalance:
    """解析 ``/credits/balance`` 响应。"""
    data = payload
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict):
            data = inner
    if not isinstance(data, dict):
        return TokenPlanBalance()

    windows: list[UsageWindow] = []
    raw_windows = data.get("usage_windows")
    if isinstance(raw_windows, dict):
        # 先按固定顺序（5h 再 7d），再补上游多给的其它窗口
        order = list(_WINDOW_LABELS) + [
            k for k in raw_windows if k not in _WINDOW_LABELS
        ]
        for key in order:
            row = raw_windows.get(key)
            if not isinstance(row, dict):
                continue
            windows.append(
                UsageWindow(
                    key=key,
                    limit=_to_float(row.get("limit_credits")),
                    used=_to_float(row.get("used_credits")),
                    remaining=_to_float(row.get("remaining_credits")),
                    enabled=bool(row.get("enabled", True)),
                    next_recover_at=parse_upstream_ts(row.get("next_recover_at")),
                )
            )

    return TokenPlanBalance(
        available=_to_float(data.get("available_credits")),
        windows=tuple(windows),
        rpm_limit=_to_int(data.get("rpm_limit")),
        tpm_limit=_to_int(data.get("tpm_limit")),
        access_status=_to_int(data.get("access_status")),
        package_count=_to_int(data.get("active_package_count")),
        calculated_at=parse_upstream_ts(data.get("calculated_at")),
        raw=data,
    )


def fetch_balance(base_url: str, token: str, sso_uid: str = "") -> TokenPlanBalance:
    """拉一次额度快照。

    鉴权头与推理接口一致（``Authorization: Bearer`` + ``id``）。
    """
    url = f"{base_url.rstrip('/')}/credits/balance"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if sso_uid:
        headers["id"] = sso_uid
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(url, headers=headers)
    resp.raise_for_status()
    return parse_balance(resp.json())


def to_quota_items(balance: TokenPlanBalance) -> dict[str, Any]:
    """额度快照 → 管理页额度卡的 ``{"items": [...], "level": ...}``。

    每个窗口一条。``percent`` 前端当**已用**百分比，所以按 ``used / limit`` 算；
    ``limit`` 为 0 时给 ``None``（不显示百分比，别除零）。
    """
    items: list[dict[str, Any]] = []
    for window in balance.windows:
        label, kind = _WINDOW_LABELS.get(window.key, (f"{window.key} 窗口", "unknown"))
        percent = None
        if window.limit > 0:
            percent = round(window.used / window.limit * 100, 2)
        items.append(
            {
                "label": label,
                # 解释性文字进 note，不塞 label——窄卡里会把 label 挤到换行
                "note": f"上限 {window.limit:g} 积分，已用 {window.used:g}",
                "used": round(window.used, 3),
                "total": round(window.limit, 3),
                "remaining": round(window.remaining, 3),
                "percent": percent,
                # 周期回满，不是权益到期：不给 expire_ts，否则到期横幅会误报
                "reset_ts": window.next_recover_at,
                "reset_note": "额度回满",
                "window": kind,
                "expire_ts": None,
                "unit": "credit",
            }
        )

    # 两个窗口是并存的约束，不是相加关系 → 不设 sum_items
    return {"items": items, "level": None}
