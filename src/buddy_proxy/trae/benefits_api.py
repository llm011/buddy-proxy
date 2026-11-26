"""Trae 签到 / 积分 / 权益用量上游 API（UG 接口，按区域取址）。

两区端点差异（2026-10-05 无凭据实测，判据见 ``config.TraeRegion`` 注释）：
- 额度 ``ide_user_ent_usage``：CN 在 ``api.trae.cn`` 的 **v2**；海外在
  ``growsg-normal.trae.ai`` 的 **v1**（v2 在海外是 TLB 404）。同一接口靠响应
  里的 ``is_dollar_usage_billing`` flag 区分计费口径（CN 积分 / 海外美元）。
- 签到 ``checkin_credits/{status,claim}``：**只有 CN 有**。海外在 grow-normal /
  growsg-normal / api.trae.ai 三处全部回应用级 404，所以海外账号不发请求、
  直接返回「无签到」标记（provider 层 ``supports_checkin=False`` 已挡一层）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from .config import TraeRegion, resolve_trae_region
from .credentials import _auth
from .pat import net

log = logging.getLogger(__name__)

# ───────────────────────── 签到 / 积分 ─────────────────────────

# CN 默认 UG host（向后兼容的模块常量；运行时一律经 resolve_trae_region 取址）。
_UG_API_HOST = "https://api.trae.cn"
# 签到 API 是 device 维度的：device_id 从 JWT 里的稳定 userId 派生（trae2api-cn 方案）
_CHECKIN_DEVICE_IDS: dict[str, str] = {}


def _checkin_identity(token: str, account_id: str = "") -> str:
    """从 JWT 提取稳定 identity（不依赖可能刷新的 token 本身）。"""
    if token:
        try:
            parts = token.split(".")
            if len(parts) >= 2:
                import base64 as _b64

                encoded = parts[1] + "=" * (-len(parts[1]) % 4)
                payload = json.loads(_b64.urlsafe_b64decode(encoded.encode("ascii")))
                data = payload.get("data")
                if isinstance(data, dict) and data.get("id"):
                    return str(data["id"])
                for key in ("user_id", "userId", "sub"):
                    if payload.get(key):
                        return str(payload[key])
        except Exception:
            pass
    if account_id:
        return str(account_id)
    return token


def checkin_device_id(token: str, account_id: str = "") -> str:
    """返回账号绑定的 16 位稳定 device id（签到 API 需要）。"""
    identity = _checkin_identity(token, account_id)
    if not identity:
        return ""
    cache_key = f"checkin#{identity}"
    if cache_key in _CHECKIN_DEVICE_IDS:
        return _CHECKIN_DEVICE_IDS[cache_key]
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    did = str(int(digest, 16) % 10**16).zfill(16)
    _CHECKIN_DEVICE_IDS[cache_key] = did
    return did


# ───────────────────────── 设备指纹 ─────────────────────────
# 2026-10-06 实测（9074 排查）：trae2api-cn 原版写死的「ASUS TUF Gaming A15 +
# windows」指纹已被上游风控拉黑——该指纹下**新** device_id 首签一律
# 9074「当前参与用户太多」（把 brand/type 换成 MacBook 立即成功）；黑名单生效
# 前注册过的老 device（历史账号）继续放行。9074 不是活动热度：同刻 status 畅通、
# 9095「当前设备今日已经签到」证明 device 维度有注册表、错误码随 device 变化。
# 所以指纹必须：① 每账号**稳定**一台（换设备=风控画像极差）；② 不再全网撞车。
# 已验证的组合固化进 devices.json，其余按 identity 从真实机型池稳定派生。

#: 真实存在的常见机型池（避开被拉黑的原版 ASUS 串）。按 identity hash 稳定取，
#: 不随机——同一账号每次启动必须是同一台「设备」。
_DEVICE_POOL: tuple[tuple[str, str], ...] = (
    ("MacBookPro18,3", "macos"),
    ("Mac14,6", "macos"),
    ("ThinkPad X1 Carbon Gen 11", "windows"),
    ("Dell XPS 15 9530", "windows"),
    ("HP Spectre x360 14", "windows"),
    ("MateBook X Pro 2023", "windows"),
)

#: claim 撞 9074（指纹被风控拒）时依次尝试的备选序号：device_id 与机型**一起**
#: 换（上游按组合画像，只换 id 不换 brand 等于同台机器）。
_DEVICE_ALT_N = (1, 2, 3)


def _devices_path() -> Any:
    from .credentials import trae_state_dir

    return trae_state_dir() / "devices.json"


def _load_device_overrides() -> dict[str, dict[str, str]]:
    """已注册设备的固化表（``devices.json``；缺失/损坏 → 空表不影响服务）。"""
    try:
        data = json.loads(_devices_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    accts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accts, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, val in accts.items():
        if (isinstance(val, dict) and val.get("device_id")
                and val.get("brand") and val.get("type")):
            out[str(key)] = {"device_id": str(val["device_id"]),
                             "brand": str(val["brand"]), "type": str(val["type"])}
    return out


def _save_device_override(key: str, device: dict[str, str]) -> None:
    """首签成功后固化 (device_id, brand, type)——上游 device 维度一天一签，
    组合漂移等于「每天换设备」。写经 credentials 的 0600 原子写，目录 0700。"""
    import os as _os

    from .credentials import _atomic_write_json, trae_state_dir

    path = _devices_path()
    trae_state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        accounts = {}
    accounts[key] = device
    _atomic_write_json(path, {"version": 1, "accounts": accounts})
    _os.chmod(path, 0o600)


def _device_for(token: str, account_id: str = "", alt: int = 0) -> dict[str, str]:
    """账号绑定的签到设备。overrides 固化表优先，否则按 identity 稳定派生：
    device_id 沿用 ``checkin_device_id`` 的 sha256(identity) 口径（alt=0 时与
    老设备一致），机型从池里按 hash 取——同一账号每次都是同一台。``alt>0``
    是 9074 换机重试的备选序号。"""
    identity = _checkin_identity(token, account_id)
    key = str(account_id or identity)
    if not alt and key:
        fixed = _load_device_overrides().get(key)
        if fixed:
            return fixed
    fallback = {"device_id": "", "brand": _DEVICE_POOL[0][0], "type": _DEVICE_POOL[0][1]}
    if not identity:
        return fallback
    suffix = "" if not alt else f"/alt{alt}"
    digest = hashlib.sha256(f"{identity}{suffix}".encode("utf-8")).hexdigest()
    did = str(int(digest, 16) % 10**16).zfill(16)
    brand, dtype = _DEVICE_POOL[int(
        hashlib.sha256(f"{identity}/fp{suffix}".encode("utf-8")).hexdigest(), 16
    ) % len(_DEVICE_POOL)]
    return {"device_id": did, "brand": brand, "type": dtype}


def _build_checkin_headers(token: str, account_id: str = "",
                           region: str | None = None,
                           device: dict[str, str] | None = None) -> dict[str, str]:
    """UG 接口请求头。``package-type`` 按区域取（CN ``stable_cn`` / 海外
    ``stable_i18n``）——上游按它分流版本能力，填错区会被当异常客户端。"""
    reg = resolve_trae_region(region)
    dev = device or _device_for(token, account_id)
    headers = {
        "Authorization": f"Cloud-IDE-JWT {token}",
        "Content-Type": "application/json",
        "x-device-id": dev["device_id"],
        "x-device-brand": dev["brand"],
        "x-device-type": dev["type"],
        "package-type": reg.package_type,
    }
    return headers


def _post_ug(path: str, token: str = "", account_id: str = "",
             region: str | None = None,
             device: dict[str, str] | None = None) -> dict[str, Any]:
    """调用 Trae UG（user growth）签到/积分 API（按区域取 host）。"""
    if not token:
        token, _ = _auth()
    reg = resolve_trae_region(region)
    url = reg.ug_base + path
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers=_build_checkin_headers(token, account_id, region=reg.key, device=device),
        method="POST",
    )
    try:
        with net.open_direct(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Trae UG {path} [{e.code}]: {e.read().decode()[:300]}")
    return data


#: 海外账号的签到占位结果（无签到系统，不发请求）。provider 侧据此走
#: 「inactive / 不阻塞整页」分支，避免把海外的 404 当成签到失败刷进日历。
_NO_CHECKIN: dict[str, Any] = {
    "enable": False,
    "checked_in": True,
    "message": "海外版无每日签到",
    "no_checkin": True,
}


def fetch_checkin_status(token: str = "", account_id: str = "",
                         region: str | None = None) -> dict[str, Any]:
    """查询今日签到/积分状态。海外区无签到端点，直接返回占位（不发请求）。"""
    reg = resolve_trae_region(region)
    if not reg.has_checkin:
        return dict(_NO_CHECKIN)
    # _post_ug 自己拼 ug_base，这里只给路径（签到固定 v2，仅 CN 有）。
    return _post_ug("/trae/api/v2/ug/checkin_credits/status", token, account_id,
                    region=reg.key)


def claim_checkin_credits(token: str = "", account_id: str = "",
                          region: str | None = None) -> dict[str, Any]:
    """领取今日签到积分。海外区无签到端点，直接返回占位（不发请求）。

    9074「当前参与用户太多」实测是**设备指纹被风控拒**（2026-10-06：换
    brand/type 立即成功；活动热度说不通——同刻 status 畅通、错误码随 device
    变化），故依次换备选设备重试（``_DEVICE_ALT_N``），成功即把组合固化进
    ``devices.json``，之后每天同一台。9095「当前设备今日已经签到」按幂等
    成功返回（``code`` 置 0 + ``already_device_signed``，message 保留上游
    原话）——provider 层无需特判，历史/日历也能正确记为已签。
    """
    reg = resolve_trae_region(region)
    if not reg.has_checkin:
        return dict(_NO_CHECKIN)
    if not token:
        # legacy 单账号路径（cli/老登录）：先取 token 再提 identity——否则
        # key/换机循环全在空 token 上算，device_id 恒为空、固化表也查不到
        token, _ = _auth()
    key = str(account_id or _checkin_identity(token, account_id))
    overrides = _load_device_overrides()
    last: dict[str, Any] = {}
    for n in (None,) + _DEVICE_ALT_N:
        dev = overrides.get(key) if n is None else None
        if dev is None:
            dev = _device_for(token, account_id, alt=n or 0)
        last = _post_ug("/trae/api/v2/ug/checkin_credits/claim", token, account_id,
                        region=reg.key, device=dev)
        code = last.get("code")
        if code in (0, None):
            # 成功即固化（含 override 命中却仍成功的情况——无变化重复写无妨；
            # alt 换机成功说明原指纹已被拒，必须覆盖旧记录，否则明天还撞 9074）
            if not overrides.get(key) or n is not None:
                _save_device_override(key, dev)
            return last
        if code == 9095:
            return {"code": 0, "already_device_signed": True,
                    "checked_in": True, "extra_credits": None,
                    "message": f"设备今日已签：{last.get('message', '')}"}
        if code != 9074:
            return last  # 其他错误（token 失效等）原样上抛给上层分类
        time.sleep(1.0)  # 换机重试间隔，节奏像人
    # 池尽仍 9074：标记 device_rejected——provider 外层按最终失败处理，
    # 别再走 5/10s 限流退避（同样的 4 台设备再打 8 次救不回指纹黑名单）。
    return {**last, "device_rejected": True}


def fetch_ent_usage(token: str = "", account_id: str = "",
                    region: str | None = None) -> dict[str, Any]:
    """查询权益/额度用量（ide_user_ent_usage：总额度 + 权益包列表）。

    按区域选 ``ug_base`` 与接口版本（CN v2 / 海外 v1），并设 ``X-User-Region``
    头。计费口径分叉（积分 vs 美元）由**调用方**读响应里的
    ``is_dollar_usage_billing`` 决定，本函数只负责取回原始结构。
    """
    if not token:
        token, _ = _auth()
    reg = resolve_trae_region(region)
    headers = _build_checkin_headers(token, account_id, region=reg.key)
    headers["X-User-Region"] = "CN" if reg.key == "cn" else "GLOBAL"
    req = urllib.request.Request(
        reg.usage_url(),
        data=b"{}", headers=headers, method="POST",
    )
    try:
        with net.open_direct(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Trae usage [{e.code}]: {e.read().decode()[:300]}")


# ───────────────────────── 会话级消耗明细（官方 dashboard#usage） ─────────────────────────
#
# 官网 www.trae.cn/dashboard#usage 页面背后的接口，返回**会话级**真实消耗（不是
# 我们日志的请求级：一个会话含多次请求，无法 1:1 认领，所以这里只做展示、不回写
# metrics，避免重复记账）。
#
# 几个实测固化的约束（2026-11-25，CN 账号本地 token 打通）：
# - 头部必须走**浏览器 Web 路由**形态：浏览器 UA + Origin/Referer 指向官网域，
#   **不能**带 x-device-id / x-device-brand / package-type 这些 IDE 指纹头——
#   带上会触发 9004 参数校验失败（与签到/额度接口的头正好相反，别复用
#   _build_checkin_headers）。
# - 时间窗口最大 30 天，超了同样 9004。
# - page_size 对齐官方控制台上限 ~50；200 直接 9004。
# - usage_type=[7] = Cloud-IDE 会话积分消耗；1–8 实测只有 7 有数据。
# - 海外（growsg-normal / grow-normal）路由通但返回 total=0，api.trae.ai 是
#   TLB 404——海外是否真有会话明细待确认有海外消耗的账号验证，目前按「不支持」
#   处理，前端给明确提示而不是空列表假装没数据。

#: 会话明细接口允许的最大时间窗口（天）。超限上游 9004 拒绝。
_USAGE_LOGS_MAX_WINDOW_DAYS = 30
#: 单页最大条数。page_size 超限上游 9004（实测 200 被拒，50 通过）。
_USAGE_LOGS_MAX_PAGE_SIZE = 50


def _build_usage_logs_headers(token: str, region: TraeRegion) -> dict[str, str]:
    """会话明细接口的请求头：浏览器 Web 路由形态，**不带** IDE 指纹。"""
    host = region.auth_host  # www.trae.cn / www.trae.ai
    return {
        "Authorization": f"Cloud-IDE-JWT {token}",
        "Content-Type": "application/json",
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Origin": f"https://{host}",
        "Referer": f"https://{host}/",
        "sec-fetch-site": "same-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


def fetch_usage_logs(token: str = "", account_id: str = "",
                     region: str | None = None, *,
                     start_ts: int | None = None, end_ts: int | None = None,
                     page_size: int = 30, page_num: int = 1,
                     usage_type: list[int] | None = None) -> dict[str, Any]:
    """拉取 trae 官方会话级消耗明细（对应官网 dashboard#usage 页）。

    返回 ``{"total": N, "rows": [...], "page": n, "page_size": m, "window_days": d,
    "billing": "credits"|"dollar"}``。``rows`` 是归一化后的会话行，字段见
    :func:`_normalize_usage_log_row`。

    窗口默认最近 30 天（接口上限）；调用方也可传 start_ts/end_ts 收窄。任何
    上游错误抛 ``RuntimeError``，由 UI 层接住转成空态提示——别让一个账号的拉取
    失败拖垮整个面板。
    """
    if not token:
        token, _ = _auth()
    reg = resolve_trae_region(region)
    now = int(time.time())
    end = int(end_ts) if end_ts else now
    win_days = _USAGE_LOGS_MAX_WINDOW_DAYS
    start = int(start_ts) if start_ts else end - win_days * 86400
    # 兜底：窗口超上限就收窄到上限（按 end 对齐，保证「最近 N 天」语义）
    if end - start > _USAGE_LOGS_MAX_WINDOW_DAYS * 86400:
        start = end - _USAGE_LOGS_MAX_WINDOW_DAYS * 86400
    page_size = max(1, min(int(page_size), _USAGE_LOGS_MAX_PAGE_SIZE))
    page_num = max(1, int(page_num))

    body = json.dumps({
        "start_time": start, "end_time": end,
        "page_size": page_size, "page_num": page_num,
        "usage_type": usage_type if usage_type is not None else [7],
    }).encode("utf-8")
    req = urllib.request.Request(
        reg.usage_logs_url(), data=body,
        headers=_build_usage_logs_headers(token, reg), method="POST",
    )
    try:
        with net.open_direct(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Trae usage logs [{e.code}]: {e.read().decode()[:300]}")

    data = json.loads(raw)
    # 9004 是应用级错误码（HTTP 可能仍是 200）：参数校验失败
    code = data.get("code")
    if code not in (0, None):
        raise RuntimeError(f"Trae usage logs 上游拒绝 code={code}: {data.get('message', '')[:200]}")

    sessions = data.get("user_usage_group_by_sessions") or []
    return {
        "total": int(data.get("total") or 0),
        "page": page_num,
        "page_size": page_size,
        "window_days": win_days,
        "billing": reg.billing,
        "rows": [_normalize_usage_log_row(s) for s in sessions],
    }


def _normalize_usage_log_row(s: dict[str, Any]) -> dict[str, Any]:
    """把上游会话行摊平成前端直接能用的结构，顺手防御字段缺失。"""
    extra = s.get("extra_info") or {}
    cred = s.get("credits_float")
    if cred is None:
        cred = s.get("amount_float")
    # mode：上游 use_max_mode=true 时 mode 常为 "Max"；把两者合并成单一展示串
    mode = s.get("mode") or ""
    if s.get("use_max_mode") and mode != "Max":
        mode = mode or "Max"
    return {
        "session_id": s.get("session_id") or "",
        "ts": s.get("usage_time"),  # unix 秒，可能缺失
        "model": s.get("model_name") or "",
        "mode": mode,
        "credits": cred,
        "cost_money": s.get("cost_money_float"),
        "dollar": s.get("dollar_float"),
        "input_tokens": extra.get("input_token"),
        "output_tokens": extra.get("output_token"),
        "cache_read_tokens": extra.get("cache_read_token"),
        "cache_write_tokens": extra.get("cache_write_token"),
        "preview": s.get("user_input_preview") or "",
        "usage_source": s.get("usage_source"),
    }

