"""PAT 配置：多账号 profiles 校验、通道开关与状态文件路径。

``TRAE_PAT_BEARER_PROFILES`` 是严格 JSON 数组，每项必须包含 ``bearer``，
可选 ``id``、``priority``，不接受其他字段。配置存在时不会回退旧的
``TRAE_PAT_BEARER``；旧变量只在 profiles 完全未设置时兼容。
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import logging
import os
import pathlib
import re
from typing import Any

from fastapi import HTTPException

log = logging.getLogger(__name__)

_BEARER = "TRAE_PAT_BEARER"
_BEARER_PROFILES = "TRAE_PAT_BEARER_PROFILES"
_AUTH_URL = "TRAE_PAT_AUTH_URL"
_TOKEN_URL = "TRAE_PAT_TOKEN_URL"
_PLUS_GATEWAY = "TRAE_PAT_PLUS_GATEWAY"
_TOKEN_FILE = "TRAE_PAT_TOKEN_FILE"

_MAX_PROFILES = 16
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_REFRESH_MARGIN_S = 2 * 3600
_EXCHANGE_TIMEOUT_S = 20
_CHAT_TIMEOUT_S = 180
# 仅连接建立前可安全重放的瞬时错误；总尝试次数 = 首次 + 3 次重试。
_CONNECT_RETRY_DELAYS_S = (0.3, 1.0, 2.0)
_CONNECT_RETRY_ERRNOS = frozenset({errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH})
# 账号级认证/限流的短冷却；429 可由 Retry-After 覆盖。
_ACCOUNT_COOLDOWN_S = 60
_FAILOVER_SSE_CODES = frozenset({4008, 4009, 4031, 4220, 4221, 4222, 4223, 4224, 4227})
# 上游频控码（请求队列满 / 访问量过大）：按账号×模型计数、几十秒自愈，不是
# 额度耗尽。首个语义事件前撞到即换号（见 pacing），全部账号都限频时报 429
# 让 model_order 换备选通道，而不是当 502 透传成带内错误。
_RATE_LIMIT_SSE_CODES = frozenset({3004, 4050, 4051})


@dataclass(frozen=True, slots=True)
class PatProfile:
    id: str
    bearer: str
    priority: int
    index: int
    cloud_ide_token: str = ""
    uid: str = ""
    expires_at: float = 0.0

    @property
    def cache_key(self) -> str:
        # id 可调整展示或排序；凭据归属必须绑定 bearer（或直传 token），避免配置重排后串用。
        secret = self.bearer if self.bearer else f"direct:{self.cloud_ide_token}"
        digest = hashlib.sha256(secret.encode()).hexdigest()[:20]
        return f"profile-{digest}"


@dataclass(frozen=True, slots=True)
class PatCredentials:
    token: str
    uid: str
    machine_id: str
    device_id: str


class _PatConfigError(ValueError):
    """配置错误；消息必须保持脱敏。"""



def _profiles_configured() -> bool:
    return _BEARER_PROFILES in os.environ


def _load_profiles() -> tuple[PatProfile, ...]:
    """读取并严格校验账号配置，结果按 ``priority/index`` 稳定排序。"""
    if _profiles_configured():
        raw = os.environ.get(_BEARER_PROFILES, "")
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            # 带上「值有多长、从哪来」，否则用户看到「不是合法 JSON」只会去翻
            # .env——而 2026-11-25 那次真正的问题在 shell 里残留的坏值把 .env
            # 挡住了。长度 0/1 基本就是空值或半截括号，能直接指路。
            raise _PatConfigError(
                f"TRAE_PAT_BEARER_PROFILES 不是合法 JSON（当前值长度 {len(raw or '')}；"
                "若 .env 里配置正常，检查 shell/服务环境里是否残留同名变量把它挡住了）"
            ) from exc
        if not isinstance(parsed, list) or not parsed:
            raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 必须是非空 JSON 数组")
        if len(parsed) > _MAX_PROFILES:
            raise _PatConfigError(f"TRAE_PAT_BEARER_PROFILES 最多允许 {_MAX_PROFILES} 个账号")
        profiles: list[PatProfile] = []
        ids: set[str] = set()
        secrets: set[str] = set()
        allowed = {"bearer", "cloud_ide_token", "uid", "expires_at", "id", "priority"}
        for index, item in enumerate(parsed):
            if not isinstance(item, dict) or not set(item) <= allowed:
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 项字段无效")
            has_bearer = "bearer" in item and bool(item.get("bearer"))
            has_direct = "cloud_ide_token" in item and bool(item.get("cloud_ide_token"))
            if not has_bearer and not has_direct:
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 项字段无效")
            account_id = item.get("id", f"profile-{index}")
            bearer = item.get("bearer", "") or ""
            direct_token = item.get("cloud_ide_token", "") or ""
            uid = str(item.get("uid") or "")
            raw_exp = item.get("expires_at", 0.0)
            try:
                expires_at = float(raw_exp or 0.0)
            except (TypeError, ValueError):
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES expires_at 无效") from None
            priority = item.get("priority", index)
            if not isinstance(account_id, str) or not _PROFILE_ID_RE.fullmatch(account_id):
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 含非法 id")
            if has_bearer and (not isinstance(bearer, str) or bearer != bearer.strip()
                               or len(bearer) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in bearer)):
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 含非法 bearer")
            if has_direct and (not isinstance(direct_token, str) or direct_token != direct_token.strip()
                               or len(direct_token) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in direct_token)):
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 含非法 cloud_ide_token")
            if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 1000:
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES priority 必须是 0..1000 的整数")
            secret_key = f"b:{bearer}" if has_bearer else f"d:{direct_token}"
            if account_id in ids or secret_key in secrets:
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 含重复账号")
            ids.add(account_id)
            secrets.add(secret_key)
            profiles.append(PatProfile(
                account_id, bearer, priority, index,
                cloud_ide_token=direct_token, uid=uid, expires_at=expires_at,
            ))
        return tuple(sorted(profiles, key=lambda profile: (profile.priority, profile.index)))

    bearer = os.environ.get(_BEARER, "").strip()
    if not bearer:
        return ()
    return (PatProfile("legacy", bearer, 0, 0),)


def _configuration_error(exc: Exception) -> HTTPException:
    # 不拼接原始 JSON、bearer 或底层异常，防止秘密进入 HTTP 响应和日志。
    return HTTPException(status_code=503, detail=f"PAT 多账号配置无效：{exc}")


def ensure_pat_config() -> tuple[PatProfile, ...]:
    """仅校验本地配置，不读网络，供 provider.ensure_auth 使用。"""
    try:
        profiles = _load_profiles()
    except _PatConfigError as exc:
        raise _configuration_error(exc) from None
    if not profiles:
        raise HTTPException(status_code=401, detail="PAT 通道未配置服务账号密钥")
    return profiles


def pat_enabled() -> bool:
    """是否存在 PAT 配置意图；配置错误留给请求期校验明确报告。

    profiles 变量一旦存在便不回退旧 bearer。即使其内容无效，也注册 provider，
    避免请求静默落入其他通道；真正转发前由 ``ensure_pat_config`` fail-closed。
    """
    if _profiles_configured():
        return True
    return bool(os.environ.get(_BEARER, "").strip())


def _token_file() -> pathlib.Path:
    configured = os.environ.get(_TOKEN_FILE, "")
    if configured:
        return pathlib.Path(configured)
    # 状态统一收敛到 ~/.buddy-proxy/；首次访问自动从 ~/.ethan/ 迁移。
    from buddy_proxy.core.paths import state_file
    return state_file("trae_pat_token.json", legacy="trae_pat_token.json")


def _parse_expires_at(raw: Any) -> float:
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    if isinstance(raw, str) and raw.strip():
        from datetime import datetime

        text = raw.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _update_env_file(env_path: pathlib.Path, serialized: str) -> None:
    if not env_path.exists():
        return
    content = env_path.read_text(encoding="utf-8")
    line = f"{_BEARER_PROFILES}='{serialized}'"
    pattern = re.compile(
        r"^" + re.escape(_BEARER_PROFILES) + r"=(?:'[^']*'|\"[^\"]*\"|[^\n]*)",
        re.MULTILINE,
    )
    if pattern.search(content):
        new_content = pattern.sub(lambda _m: line, content, count=1)
    else:
        sep = "" if (not content or content.endswith("\n")) else "\n"
        new_content = f"{content}{sep}{line}\n"
    env_path.write_text(new_content, encoding="utf-8")


def import_from_ide(
    edition: str | None = None,
    env_path: pathlib.Path | None = None,
) -> dict[str, Any]:
    """从本机 Trae IDE 登录态导入 Cloud-IDE-JWT 到 TRAE_PAT_BEARER_PROFILES。"""
    from buddy_proxy.trae.auth_storage import _extract_auth_fields, find_auth_data

    editions = (edition,) if edition else ("cn", "global")
    picked_ed = ""
    fields: dict[str, Any] = {}
    for ed in editions:
        data = find_auth_data(ed)
        if not data:
            continue
        extracted = _extract_auth_fields(data)
        if str(extracted.get("token") or "").strip():
            picked_ed = ed
            fields = extracted
            break
    if not picked_ed:
        raise _PatConfigError("未找到本机 Trae IDE 登录态，请先在 Trae 客户端登录")

    token = str(fields.get("token") or "").strip()
    uid = str(fields.get("userId") or "").strip()
    expires_at = _parse_expires_at(fields.get("expiredAt"))
    profile_id = f"desktop-{picked_ed}"

    raw = os.environ.get(_BEARER_PROFILES, "").strip()
    if raw:
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _PatConfigError(
                f"TRAE_PAT_BEARER_PROFILES 不是合法 JSON（当前值长度 {len(raw)}；"
                "若 .env 里配置正常，检查 shell/服务环境里是否残留同名变量把它挡住了）"
            ) from exc
        if not isinstance(items, list):
            raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 必须是非空 JSON 数组")
    else:
        items = []

    new_entry: dict[str, Any] = {
        "id": profile_id,
        "cloud_ide_token": token,
    }
    if uid:
        new_entry["uid"] = uid
    if expires_at > 0:
        new_entry["expires_at"] = expires_at

    updated = False
    for i, item in enumerate(items):
        if isinstance(item, dict) and str(item.get("id") or "").strip() == profile_id:
            items[i] = new_entry
            updated = True
            break
    if not updated:
        if len(items) >= _MAX_PROFILES:
            raise _PatConfigError(f"TRAE_PAT_BEARER_PROFILES 最多允许 {_MAX_PROFILES} 个账号")
        items.append(new_entry)

    serialized = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    os.environ[_BEARER_PROFILES] = serialized
    _update_env_file(env_path if env_path is not None else pathlib.Path(".env"), serialized)
    return {
        "id": profile_id,
        "edition": picked_ed,
        "uid": uid,
        "expires_at": expires_at,
        "updated": updated,
        "total": len(items),
    }
