"""Codex 上游纯函数：请求构造、额度解析、凭据时间解析。

不碰网络、不碰磁盘（额度查询在 :mod:`buddy_proxy.codex.provider` 里做），
方便直接单测。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

#: Codex 后端基址（与 Codex CLI / CLIProxyAPI 一致）。
DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"

#: 请求伪装：ChatGPT 后端会据 Originator / User-Agent 识别「是不是 Codex CLI」。
#: 缺这两个头很容易被 Cloudflare 拦成 1010 或直接 403。
ORIGINATOR = "codex-tui"
USER_AGENT = "codex-tui/0.154.0 (Mac OS 26.5.2; arm64) iTerm.app/3.6.11 (codex-tui; 0.154.0)"

#: 上游不认的 chat 化字段（透传会 400）。
_DROP_FIELDS = (
    "previous_response_id",
    "prompt_cache_retention",
    "safety_identifier",
    "generate",
    "stream_options",
    "stop",
)


def normalize_base_url(raw: str) -> str:
    """账号 JSON 里可能的 ``base_url`` → 可直接拼 ``/responses`` 的 base。

    幂等：已带 ``/responses`` 的去掉；空值回落到官方基址。
    """
    text = (raw or "").strip() or DEFAULT_BASE_URL
    text = text.rstrip("/")
    if text.endswith("/responses"):
        text = text[: -len("/responses")]
    return text


def build_headers(
    access_token: str,
    *,
    account_id: str = "",
    session_id: str = "",
    stream: bool = True,
) -> dict[str, str]:
    """构造上游请求头。

    ``Chatgpt-Account-Id`` 是多账号的关键：缺了会打到默认工作区，额度和
    模型权限都可能不是这个账号的。
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Originator": ORIGINATOR,
        "User-Agent": USER_AGENT,
        "Accept": "text/event-stream" if stream else "application/json",
    }
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    if session_id:
        headers["Session-Id"] = session_id
    return headers


def build_upstream_body(body: dict[str, Any], *, model: str, stream: bool) -> dict[str, Any]:
    """chat body → 上游 Responses body（在此收口的规整 + 字段剔除）。

    投影本身在 :func:`buddy_proxy.codex.projection.chat_to_responses_request`；
    这里只做「上游会 400 的字段剔除」与响应格式强制。
    """
    from .projection import chat_to_responses_request

    upstream = chat_to_responses_request(body, model=model, stream=stream)
    for key in _DROP_FIELDS:
        upstream.pop(key, None)
    # 上游对 instructions 的存在性敏感，缺失时补空串
    upstream.setdefault("instructions", "")
    upstream["stream"] = bool(stream)
    # ChatGPT 账号走 Codex 后端时**必须** store=false：带上（或让上游默认 true）
    # 会直接 400「Store must be set to false」。这是实测出来的硬约束，
    # 不是可选项——CLIProxyAPI 同样固定写入。
    upstream["store"] = False
    return upstream


def new_session_id() -> str:
    """上游 ``Session-Id`` 头用的随机串（每次请求一个，便于上游做会话粘滞）。"""
    return os.urandom(16).hex()


def cred_expired_epoch(cred: dict[str, Any]) -> float:
    """cred 的 ``expired``（ISO）→ epoch 秒；解析失败返回 0。

    兼容带 ``Z`` 后缀与无时区（按 UTC 处理）两种写法。
    """
    raw = str(cred.get("expired") or cred.get("expiry") or "").strip()
    if not raw:
        return 0.0
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ---------------------------------------------------------------------------
# 额度
# ---------------------------------------------------------------------------

#: 已知窗口的展示名与典型长度（秒）。上游给的是 ``window_minutes``，
#: 只在对不上已知档时才回落到原始分钟数。
_WINDOW_LABELS = {
    300: "5 小时窗口",
    1440: "1 天窗口",
    10080: "周窗口",
}


def window_label(window_key: str, window_minutes: float) -> str:
    """把一个 rate-limit 窗口翻成人话。"""
    known = _WINDOW_LABELS.get(int(window_minutes))
    if known:
        return known
    if window_minutes and window_minutes % 1440 == 0:
        return f"{int(window_minutes // 1440)} 天窗口"
    if window_minutes and window_minutes % 60 == 0:
        return f"{int(window_minutes // 60)} 小时窗口"
    fallback = {"primary": "主窗口", "secondary": "次窗口"}.get(window_key, window_key)
    if window_minutes:
        return f"{fallback}（{int(window_minutes)} 分钟）"
    return fallback


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def parse_rate_limits(payload: Any) -> dict[str, Any]:
    """把上游 rate-limit 结构解析成 ``{"items": [...], "level": str|None}``。

    上游形状（CLIProxyAPI ``helps/codex_quota.go`` 同源）::

        {
          "rate_limits": {
            "primary":   {"used_percent": 12.5, "window_minutes": 300,
                           "reset_at": 1730000000},
            "secondary": {"used_percent": 40,   "window_minutes": 10080,
                           "reset_after_seconds": 3600}
          },
          "credits": {"has_credits": true, "unlimited": false, "balance": 12.5},
          "plan_type": "plus"
        }

    ``used_percent`` 在这里是**已用**百分比（与 MiMo 的「剩余」语义相反，
    见 ``providers/base.py`` 的 percent 契约）。``reset_ts`` 是周期性重置。
    """
    items: list[dict[str, Any]] = []
    level: str | None = None
    if not isinstance(payload, dict):
        return {"items": items, "level": level}

    # 上游有时把字段包在 rate_limits 下，有时直接平铺
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        limits = payload

    plan = payload.get("plan_type") or limits.get("plan_type")
    if isinstance(plan, str) and plan.strip():
        level = plan.strip()

    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        used = _num(window.get("used_percent"))
        if used is None:
            used = _num(window.get("usedPercent"))
        if used is None or used < 0:
            continue
        minutes = _num(window.get("window_minutes")) or _num(window.get("windowMinutes")) or 0.0
        reset_ts = _num(window.get("reset_at")) or _num(window.get("resetAt"))
        if not reset_ts:
            after = _num(window.get("reset_after_seconds"))
            if after is None:
                after = _num(window.get("resetAfterSeconds"))
            if after is not None and after >= 0:
                import time as _time
                reset_ts = _time.time() + after
        percent = max(0.0, min(100.0, used))
        item: dict[str, Any] = {
            "label": window_label(key, minutes),
            "percent": round(percent, 1),
            # used/total 只有百分比时无从得知，留 None 让前端只渲染百分比
            "used": None,
            "total": None,
            "remaining": round(100.0 - percent, 1),
            "unit": "percent",
        }
        if reset_ts:
            item["reset_ts"] = int(reset_ts)
        items.append(item)

    credits = payload.get("credits")
    if not isinstance(credits, dict):
        credits = limits.get("credits")
    if isinstance(credits, dict) and credits.get("has_credits"):
        balance = _num(credits.get("balance"))
        if credits.get("unlimited"):
            items.append({
                "label": "附加额度", "used": None, "total": None,
                "remaining": None, "percent": None, "unit": "credit",
            })
        elif balance is not None:
            items.append({
                "label": "附加额度", "used": None, "total": None,
                "remaining": round(balance, 2),
                "percent": None, "unit": "credit",
            })

    return {"items": items, "level": level}


def usage_url(base_url: str) -> str:
    """额度查询 URL：``<chatgpt base>/wham/usage``。

    Codex CLI 自己就是拿账号 token GET ``/backend-api/wham/usage`` 展示用量
    面板的（与 ``/backend-api/codex/usage`` 实测同源），所以不需要任何额外
    授权，也不需要抓响应头。``base_url`` 通常是 ``.../backend-api/codex``，
    这里把 ``/codex`` 剥掉再拼；既没 ``/backend-api`` 也不是官方基址的自
    定义域名按 ChatGPT 同构部署处理，补上 ``/backend-api``。
    """
    text = (base_url or "").strip().rstrip("/")
    codex_base = normalize_base_url(text)  # 空值 → 官方 .../backend-api/codex
    if codex_base.endswith("/backend-api/codex"):
        root = codex_base[: -len("/codex")]
    elif "/backend-api" not in codex_base:
        root = f"{codex_base}/backend-api"
    else:
        root = codex_base
    return f"{root}/wham/usage"


def _window_seconds(window: dict[str, Any]) -> float:
    """窗口长度（秒）：上游给 ``limit_window_seconds``，旧形状给 ``window_minutes``。"""
    seconds = _num(window.get("limit_window_seconds"))
    if seconds is None:
        seconds = _num(window.get("limitWindowSeconds"))
    if seconds is None:
        minutes = _num(window.get("window_minutes"))
        if minutes is None:
            minutes = _num(window.get("windowMinutes"))
        seconds = minutes * 60.0 if minutes else None
    return seconds or 0.0


#: 额度查询 payload 里被当成「一个窗口」的对象名（有序：先主后次）。
_WINDOW_KEYS = ("primary_window", "secondary_window", "primary", "secondary")


def _quotas_from_window_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """``GET /wham/usage`` 的形状 → ``{"items", "level"}``。

    payload 形如 (CodexBar / codex-rs 抓包)::

        {"plan_type": "team",
         "rate_limit": {"allowed": true, "limit_reached": false,
                        "primary_window":  {"used_percent": 0,
                                            "limit_window_seconds": 18000,
                                            "reset_at": 1792477637},
                        "secondary_window": {"used_percent": 17,
                                            "limit_window_seconds": 604800,
                                            "reset_at": 1792940980}},
         "credits": {"has_credits": false, "unlimited": false, "balance": null}}

    窗口身份**按长度认**（5h / 1 天 / 周），不按 primary/secondary 认——
    上游把滚动窗口和周窗口在这两个槽之间挪过。
    """
    items: list[dict[str, Any]] = []
    level = payload.get("plan_type")
    level = level.strip() if isinstance(level, str) and level.strip() else None

    limit = payload.get("rate_limit")
    if not isinstance(limit, dict):
        limit = payload.get("rateLimit")
    if not isinstance(limit, dict):
        return {"items": items, "level": level}

    seen: set[str] = set()
    for key in _WINDOW_KEYS:
        window = limit.get(key)
        if not isinstance(window, dict):
            continue
        used = _num(window.get("used_percent"))
        if used is None:
            used = _num(window.get("usedPercent"))
        if used is None:
            continue
        seconds = _window_seconds(window)
        minutes = round(seconds / 60.0)
        label = window_label("", minutes)
        if label in seen:
            continue
        seen.add(label)
        percent = max(0.0, min(100.0, used))
        item: dict[str, Any] = {
            "label": label,
            "percent": round(percent, 1),
            "used": None,
            "total": None,
            "remaining": round(100.0 - percent, 1),
            "unit": "percent",
        }
        reset_ts = _num(window.get("reset_at")) or _num(window.get("resetAt"))
        if not reset_ts:
            after = _num(window.get("reset_after_seconds"))
            if after is not None and after >= 0:
                import time as _time
                reset_ts = _time.time() + after
        if reset_ts:
            item["reset_ts"] = int(reset_ts)
        items.append(item)

    credits = payload.get("credits")
    if isinstance(credits, dict) and credits.get("has_credits"):
        if credits.get("unlimited"):
            items.append({
                "label": "附加额度", "used": None, "total": None,
                "remaining": None, "percent": None, "unit": "credit",
                "display": "不限额",
            })
        else:
            balance = _num(credits.get("balance"))
            if balance is not None:
                items.append({
                    "label": "附加额度", "used": None, "total": None,
                    "remaining": round(balance, 2),
                    "percent": None, "unit": "credit",
                })

    return {"items": items, "level": level}


def parse_usage(payload: Any) -> dict[str, Any]:
    """/wham/usage 完整响应 → ``{"items", "level"}``（两种已知形状都认）。

    - 新版（Codex CLI 当前形状）：``rate_limit.primary_window/secondary_window``，
      窗口长度在 ``limit_window_seconds``——见 :func:`_quotas_from_window_payload`
    - 旧版（CLIProxyAPI ``helps/codex_quota.go`` 同源，事件/响应头形态）：
      ``rate_limits.primary/secondary``，长度在 ``window_minutes``——复用
      :func:`parse_rate_limits`
    """
    if not isinstance(payload, dict):
        return {"items": [], "level": None}
    if isinstance(payload.get("rate_limit"), dict) or isinstance(
            payload.get("rateLimit"), dict):
        return _quotas_from_window_payload(payload)
    return parse_rate_limits(payload)


def notice_item(label: str, text: str) -> dict[str, Any]:
    """一条纯说明性的「额度行」（无数字，前端原样显示）。

    未登录 / 查询失败时用它占位——返回 ``None`` 会让整块额度卡消失，
    用户反而不知道为什么没有。
    """
    return {
        "label": label,
        "display": text,
        "used": None, "total": None, "remaining": None,
        "percent": None, "unit": None,
    }


def load_models_file(path: str) -> list[dict[str, Any]]:
    """读 models.json（容错：损坏返回空表，由调用方兜底）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    return [m for m in models if isinstance(m, dict) and m.get("id")]
