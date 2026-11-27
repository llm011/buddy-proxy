"""Antigravity onboarding：loadCodeAssist → （必要时）onboardUser → 项目 ID。

与 gemini 通道的 Code Assist onboarding 同族（同一 API 家族
``cloudcode-pa.googleapis.com/v1internal:*``），三处不同：

1. **metadata 是数字枚举**（gemini 是字符串）：``ideType=9``（ANTIGRAVITY）、
   ``platform``（1= darwin-amd64 … 5= win32-amd64）、``pluginType=2``（GEMINI）。
2. 端点带 fallback：daily-cloudcode-pa 优先、cloudcode-pa 兜底（参考实现与
   omp 客户端均为 daily 优先；agy 二进制里两者皆有）。
3. onboardUser 的 LRO 轮询沿用参考实现语义：响应 ``done=false`` 时隔几秒
   **重发同一请求**（幂等）直到 done；响应带 ``name`` 时也可 GET operation
   （gemini CLI 语义），两者都支持，谁先给 done 用谁。
"""

from __future__ import annotations

import json
import logging
import platform
import time
import urllib.error
import urllib.request
from typing import Any

from ..core import net as core_net

log = logging.getLogger(__name__)

#: 通道 id，也是 ``settings.json`` 里 ``provider_proxy`` 的键。
_PROVIDER_ID = "antigravity"

#: 端点 fallback（daily 优先，参考实现同序）。
ENDPOINTS = (
    "https://daily-cloudcode-pa.googleapis.com",
    "https://cloudcode-pa.googleapis.com",
)
API_VERSION = "v1internal"

IDE_TYPE_ANTIGRAVITY = 9
PLUGIN_TYPE_GEMINI = 2
_PLATFORM_ENUM = {
    ("darwin", "amd64"): 1,
    ("darwin", "arm64"): 2,
    ("linux", "amd64"): 3,
    ("linux", "arm64"): 4,
    ("windows", "amd64"): 5,
}

TIER_FREE = "free-tier"
TIER_LEGACY = "legacy-tier"

#: LRO 轮询节奏：参考实现 5 秒一次、预算 10 轮；放宽到 60 秒总预算。
_POLL_INTERVAL_S = 5.0
_POLL_BUDGET_S = 60.0


class SetupError(RuntimeError):
    """onboarding 失败（loadCodeAssist/onboardUser 被拒、拿不到项目）。"""


def _platform_enum() -> int:
    arch = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64"}.get(
        platform.machine().lower(), "amd64"
    )
    system = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(
        platform.system().lower(), "linux"
    )
    return _PLATFORM_ENUM.get((system, arch), 1)


def core_metadata() -> dict[str, int]:
    return {
        "ideType": IDE_TYPE_ANTIGRAVITY,
        "platform": _platform_enum(),
        "pluginType": PLUGIN_TYPE_GEMINI,
    }


def _post(access_token: str, method: str, body: dict[str, Any], timeout: float = 30.0) -> tuple[int, dict[str, Any] | str]:
    """POST /v1internal:<method>，返回 (status, json或文本)。不打 fallback。"""
    from .fingerprint import metadata_headers

    last_err = ""
    for base in ENDPOINTS:
        url = f"{base}/{API_VERSION}:{method}"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            # 真 token 放后面：metadata_headers() 带的是占位空 Authorization，
            # 顺序反了会被 "Bearer "（空）覆盖 → 上游 401 CREDENTIALS_MISSING
            headers={**metadata_headers(), "Authorization": f"Bearer {access_token}"},
        )
        try:
            with core_net.provider_urlopen(_PROVIDER_ID, req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()[:500]
            except Exception:  # noqa: BLE001
                pass
            last_err = f"{method} HTTP {exc.code}: {detail}"
            log.warning("%s 在 %s 失败，尝试下一个端点: %s", method, base, last_err)
        except urllib.error.URLError as exc:
            last_err = f"{method} 网络失败: {exc.reason}"
            log.warning("%s 在 %s 失败: %s", method, base, last_err)
    raise SetupError(last_err or f"{method} 所有端点均失败")


def _get_operation(access_token: str, name: str, timeout: float = 30.0) -> dict[str, Any]:
    """GET /v1internal/{name}（LRO 轮询，gemini CLI 语义；单端点足够）。"""
    from .fingerprint import metadata_headers

    url = f"{ENDPOINTS[0]}/{API_VERSION}/{name}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={**metadata_headers(), "Authorization": f"Bearer {access_token}"},
    )
    try:
        with core_net.provider_urlopen(_PROVIDER_ID, req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise SetupError(f"getOperation 失败: {exc}") from exc


def _default_tier(load_res: dict[str, Any]) -> str:
    """allowedTiers 里 isDefault 的 tier id；没有取第一个；再没有 legacy。"""
    tiers = [t for t in load_res.get("allowedTiers") or [] if isinstance(t, dict) and t.get("id")]
    for tier in tiers:
        if tier.get("isDefault"):
            return str(tier["id"])
    return str(tiers[0]["id"]) if tiers else TIER_LEGACY


def _project_from_value(value: Any) -> str:
    """从 cloudaicompanionProject 字段提取项目 ID（字符串/多层 dict 兼容）。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("id", "projectId", "project"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
            if isinstance(inner, dict):
                nested = inner.get("id")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return ""


def _ineligible_reasons(load_res: dict[str, Any]) -> str:
    parts: list[str] = []
    for tier in load_res.get("ineligibleTiers") or []:
        if not isinstance(tier, dict):
            continue
        parts.append(
            "{}: {}: {}".format(
                tier.get("tierId") or tier.get("id") or "?",
                tier.get("reasonCode") or "?",
                str(tier.get("reasonMessage") or "").strip(),
            ).strip(": ")
        )
    return "；".join(parts)


def _missing_project_error(load_res: dict[str, Any], base: str) -> SetupError:
    reasons = _ineligible_reasons(load_res)
    msg = f"账号不符合 Antigravity 资格：{reasons}" if reasons else base
    cur = load_res.get("currentTier")
    if isinstance(cur, dict) and cur.get("id"):
        msg += f"\n    currentTier={cur['id']}"
    return SetupError(msg)


def _wait_lro(access_token: str, op: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """onboardUser LRO 等待：有 name 用 GET（gemini 语义），否则重 POST（参考实现语义）。"""
    deadline = time.monotonic() + _POLL_BUDGET_S
    while not op.get("done"):
        if time.monotonic() > deadline:
            raise SetupError("onboardUser 超时（LRO 未在预算内完成）")
        time.sleep(_POLL_INTERVAL_S)
        if op.get("name"):
            op = _get_operation(access_token, op["name"])
        else:
            op = _post(access_token, "onboardUser", body)[1]
    return op


def setup_code_assist(access_token: str, project_id: str = "") -> dict[str, Any]:
    """执行 onboarding，返回 ``{"project_id", "tier", "tier_name"}``。

    失败抛 :class:`SetupError` / :class:`AuthError`。
    """
    metadata = core_metadata()
    load_body: dict[str, Any] = {"metadata": metadata}
    if project_id:
        load_body["cloudaicompanionProject"] = project_id
        load_body["metadata"] = {**metadata, "duetProject": project_id}

    _, load_res = _post(access_token, "loadCodeAssist", load_body)
    if not isinstance(load_res, dict):
        raise SetupError("loadCodeAssist 返回异常")

    # 已开通：直接取项目（free 层 loadCodeAssist 通常已带托管项目）
    if load_res.get("currentTier"):
        tier = load_res.get("paidTier") or load_res.get("currentTier") or {}
        proj = _project_from_value(load_res.get("cloudaicompanionProject"))
        if not proj and project_id:
            proj = project_id
        if not proj:
            raise _missing_project_error(
                load_res,
                base="账号已开通但未返回 cloudaicompanionProject，请重试",
            )
        return {
            "project_id": proj,
            "tier": str(tier.get("id") or TIER_LEGACY),
            "tier_name": str(tier.get("name") or ""),
        }

    # 未开通：onboarding（tierId + metadata，与参考实现同款最小请求体）
    tier_id = _default_tier(load_res)
    onboard_body: dict[str, Any] = {"tierId": tier_id, "metadata": metadata}

    lro = _post(access_token, "onboardUser", onboard_body)[1]
    lro = _wait_lro(access_token, lro, onboard_body)

    lro_err = lro.get("error")
    if isinstance(lro_err, dict) and lro_err:
        raise SetupError(
            "onboardUser 失败: [{}] {}".format(
                lro_err.get("code") or "?",
                str(lro_err.get("message") or "").strip()
                or json.dumps(lro_err, ensure_ascii=False)[:400],
            )
        )

    resp = lro.get("response") if isinstance(lro.get("response"), dict) else {}
    proj = _project_from_value(resp.get("cloudaicompanionProject"))
    if not proj:
        proj = _project_from_value(lro.get("cloudaicompanionProject"))
    if not proj and project_id:
        proj = project_id
    if not proj:
        raise _missing_project_error(load_res, "onboarding 完成但未返回项目 ID")
    return {"project_id": proj, "tier": tier_id, "tier_name": ""}
