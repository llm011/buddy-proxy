"""ZCode Coding Plan 额度重置券客户端。

负责从本机 ZCode 配置/环境变量解析重置券所需的双重 JWT 凭据，并调用
``https://zcode.z.ai/api/v1/coding-plan/reset/*`` 查询可用重置券与核销使用。

为什么需要双重凭据（与 ``providers/zcode.py`` 的 API key 不同）：
ZCode 服务端重置接口要求 ``Authorization: Bearer <zcode_jwt>`` 与
``X-Bigmodel-Authorization: <oauth:bigmodel:access_token>``（直传，不带 Bearer），
以及 ``Bigmodel-Target-Type: PERSONAL``。单靠 Coding Plan API Key 无法调用。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

ZCODE_RESET_BASE = "https://zcode.z.ai/api/v1/coding-plan/reset"
_USER_AGENT = "ZCode/3.14.3"
_VALID_RESET_TYPES = ("FIVE_HOUR", "WEEK")


def _read_json_safe(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve_zcode_reset_credentials() -> tuple[str, str]:
    """解析 ``(zcode_jwt, bigmodel_access_token)``。

    ``zcode_jwt`` 读取顺序：
    1. 环境变量 ``ZCODE_JWT_TOKEN``
    2. ``~/.zcode/cli/config.json`` 中 ``provider.bigmodel-start-plan.options.apiKey``
       （ZCode CLI 持续刷新该 JWT；``v2/credentials.json`` 里的旧 token 可能已过期）
    3. ``~/.zcode/v2/credentials.json`` 中的 ``zcodejwttoken``

    ``bigmodel_access_token`` 读取顺序：
    1. 环境变量 ``ZCODE_BIGMODEL_ACCESS_TOKEN``
    2. ``~/.zcode/v2/credentials.json`` 中的 ``oauth:bigmodel:access_token``
    """
    zcode_jwt = os.environ.get("ZCODE_JWT_TOKEN", "").strip()
    if not zcode_jwt:
        cli_cfg = _read_json_safe(Path.home() / ".zcode" / "cli" / "config.json")
        providers = cli_cfg.get("provider") or {}
        if isinstance(providers, dict):
            bm_start = providers.get("bigmodel-start-plan")
            if isinstance(bm_start, dict):
                zcode_jwt = str((bm_start.get("options") or {}).get("apiKey") or "").strip()
            if not zcode_jwt:
                for p in providers.values():
                    if isinstance(p, dict) and p.get("kind") == "anthropic":
                        cand = str((p.get("options") or {}).get("apiKey") or "").strip()
                        if cand:
                            zcode_jwt = cand
                            break

    v2_cred = _read_json_safe(Path.home() / ".zcode" / "v2" / "credentials.json")
    if not zcode_jwt:
        zcode_jwt = str(v2_cred.get("zcodejwttoken") or "").strip()

    bigmodel_token = os.environ.get("ZCODE_BIGMODEL_ACCESS_TOKEN", "").strip()
    if not bigmodel_token:
        bigmodel_token = str(v2_cred.get("oauth:bigmodel:access_token") or "").strip()

    return zcode_jwt, bigmodel_token


def _reset_headers(zcode_jwt: str, bigmodel_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {zcode_jwt}",
        "X-Bigmodel-Authorization": bigmodel_token,
        "Bigmodel-Target-Type": "PERSONAL",
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json",
    }


def parse_reset_status_payload(
    payload: dict[str, Any],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """将服务端 ``/reset/status`` 响应归一化为前端可直接消费的摘要结构。"""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    now = now_ms if now_ms is not None else int(time.time() * 1000)

    def _valid_items(raw: Any) -> list[int]:
        if not isinstance(raw, list):
            return []
        out: list[int] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            exp = item.get("expire_at")
            if isinstance(exp, (int, float)) and exp > now:
                out.append(int(exp))
        return sorted(out)

    five_hour = _valid_items(data.get("available_five_hour_resets"))
    week = _valid_items(data.get("available_week_resets"))

    fh_hist = data.get("latest_five_hour_reset_history")
    wk_hist = data.get("latest_week_reset_history")

    return {
        "supported": True,
        "five_hour_count": len(five_hour),
        "five_hour_earliest_expire_at": five_hour[0] if five_hour else None,
        "week_count": len(week),
        "week_earliest_expire_at": week[0] if week else None,
        "total_count": len(five_hour) + len(week),
        "latest_five_hour_used_at": fh_hist.get("used_at") if isinstance(fh_hist, dict) else None,
        "latest_week_used_at": wk_hist.get("used_at") if isinstance(wk_hist, dict) else None,
        "has_unread_history": bool(data.get("has_unread_history")),
        "server_time": data.get("server_time"),
    }


async def fetch_reset_status(
    *,
    zcode_jwt: str | None = None,
    bigmodel_token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """拉取当前 ZCode 账号的可用重置券状态。

    凭据缺失或上游异常时返回 ``{"supported": False, ...}``，不抛出异常以免阻塞主额度查询。
    """
    if zcode_jwt is None or bigmodel_token is None:
        resolved_jwt, resolved_bm = resolve_zcode_reset_credentials()
        zcode_jwt = resolved_jwt if zcode_jwt is None else zcode_jwt
        bigmodel_token = resolved_bm if bigmodel_token is None else bigmodel_token

    if not zcode_jwt or not bigmodel_token:
        return {"supported": False, "reason": "missing_credentials", "total_count": 0}

    url = f"{ZCODE_RESET_BASE}/status"
    headers = _reset_headers(zcode_jwt, bigmodel_token)
    try:
        if client is not None:
            resp = await client.get(url, headers=headers, timeout=10.0)
        else:
            async with httpx.AsyncClient(timeout=10.0) as tmp:
                resp = await tmp.get(url, headers=headers)
        if resp.status_code != 200:
            log.warning("zcode reset/status failed: HTTP %s", resp.status_code)
            return {
                "supported": False,
                "reason": f"http_{resp.status_code}",
                "total_count": 0,
            }
        body = resp.json()
        if not isinstance(body, dict) or body.get("code") != 0:
            code = body.get("code") if isinstance(body, dict) else "invalid"
            log.warning("zcode reset/status business error: code=%s", code)
            return {
                "supported": False,
                "reason": f"api_code_{code}",
                "total_count": 0,
            }
        return parse_reset_status_payload(body)
    except Exception as exc:
        log.warning("zcode reset/status request error: %s", exc)
        return {"supported": False, "reason": "request_error", "total_count": 0}


def fetch_reset_status_sync(
    *,
    zcode_jwt: str | None = None,
    bigmodel_token: str | None = None,
) -> dict[str, Any]:
    """同步版 ``fetch_reset_status``，供 ``ZcodeProvider.quota()`` 直接调用。"""
    if zcode_jwt is None or bigmodel_token is None:
        resolved_jwt, resolved_bm = resolve_zcode_reset_credentials()
        zcode_jwt = resolved_jwt if zcode_jwt is None else zcode_jwt
        bigmodel_token = resolved_bm if bigmodel_token is None else bigmodel_token

    if not zcode_jwt or not bigmodel_token:
        return {"supported": False, "reason": "missing_credentials", "total_count": 0}

    url = f"{ZCODE_RESET_BASE}/status"
    headers = _reset_headers(zcode_jwt, bigmodel_token)
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            log.warning("zcode reset/status failed: HTTP %s", resp.status_code)
            return {"supported": False, "reason": f"http_{resp.status_code}", "total_count": 0}
        body = resp.json()
        if not isinstance(body, dict) or body.get("code") != 0:
            code = body.get("code") if isinstance(body, dict) else "invalid"
            log.warning("zcode reset/status business error: code=%s", code)
            return {"supported": False, "reason": f"api_code_{code}", "total_count": 0}
        return parse_reset_status_payload(body)
    except Exception as exc:
        log.warning("zcode reset/status request error: %s", exc)
        return {"supported": False, "reason": "request_error", "total_count": 0}


async def use_reset_ticket(
    reset_type: str,
    *,
    idempotency_key: str | None = None,
    zcode_jwt: str | None = None,
    bigmodel_token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """核销使用一张 ZCode 重置券（``FIVE_HOUR`` 或 ``WEEK``）。"""
    rtype = (reset_type or "").strip().upper()
    if rtype not in _VALID_RESET_TYPES:
        raise ValueError(f"不支持的重置券类型: {reset_type!r}，仅支持 FIVE_HOUR 或 WEEK")

    if zcode_jwt is None or bigmodel_token is None:
        resolved_jwt, resolved_bm = resolve_zcode_reset_credentials()
        zcode_jwt = resolved_jwt if zcode_jwt is None else zcode_jwt
        bigmodel_token = resolved_bm if bigmodel_token is None else bigmodel_token

    if not zcode_jwt or not bigmodel_token:
        raise RuntimeError("未找到 ZCode 重置券凭据（需登录本机 ZCode CLI）")

    idem = (idempotency_key or str(uuid.uuid4())).strip()[:64]
    url = f"{ZCODE_RESET_BASE}/use"
    headers = _reset_headers(zcode_jwt, bigmodel_token)
    payload = {"idempotency_key": idem, "reset_type": rtype}

    if client is not None:
        resp = await client.post(url, headers=headers, json=payload, timeout=15.0)
    else:
        async with httpx.AsyncClient(timeout=15.0) as tmp:
            resp = await tmp.post(url, headers=headers, json=payload)

    try:
        body = resp.json()
    except Exception:
        body = None

    if resp.status_code != 200:
        code = body.get("code") if isinstance(body, dict) else None
        msg = body.get("msg") if isinstance(body, dict) else resp.text[:200]
        raise RuntimeError(f"使用重置券失败（HTTP {resp.status_code}" + (f", code={code}" if code is not None else "") + f"）: {msg}")

    if not isinstance(body, dict) or body.get("code") != 0:
        code = body.get("code") if isinstance(body, dict) else "invalid"
        msg = body.get("msg") if isinstance(body, dict) else "未知响应"
        raise RuntimeError(f"使用重置券失败（code={code}）: {msg}")

    return {
        "ok": True,
        "reset_type": rtype,
        "idempotency_key": idem,
        "data": body.get("data") or {},
    }
