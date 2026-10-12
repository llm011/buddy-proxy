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

    @property
    def cache_key(self) -> str:
        # id 可调整展示或排序；凭据归属必须绑定 bearer，避免配置重排后串用 token。
        digest = hashlib.sha256(self.bearer.encode()).hexdigest()[:20]
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
            raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 不是合法 JSON") from exc
        if not isinstance(parsed, list) or not parsed:
            raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 必须是非空 JSON 数组")
        if len(parsed) > _MAX_PROFILES:
            raise _PatConfigError(f"TRAE_PAT_BEARER_PROFILES 最多允许 {_MAX_PROFILES} 个账号")
        profiles: list[PatProfile] = []
        ids: set[str] = set()
        bearers: set[str] = set()
        allowed = {"bearer", "id", "priority"}
        for index, item in enumerate(parsed):
            if not isinstance(item, dict) or "bearer" not in item or not set(item) <= allowed:
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 项字段无效")
            account_id = item.get("id", f"profile-{index}")
            bearer = item.get("bearer")
            priority = item.get("priority", index)
            if not isinstance(account_id, str) or not _PROFILE_ID_RE.fullmatch(account_id):
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 含非法 id")
            if (not isinstance(bearer, str) or not bearer or bearer != bearer.strip()
                    or len(bearer) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in bearer)):
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 含非法 bearer")
            if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 1000:
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES priority 必须是 0..1000 的整数")
            if account_id in ids or bearer in bearers:
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 含重复账号")
            ids.add(account_id)
            bearers.add(bearer)
            profiles.append(PatProfile(account_id, bearer, priority, index))
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
