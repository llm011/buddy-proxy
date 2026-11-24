"""PAT 凭证：bearer 两步交换、账号级缓存刷新与请求头构造。

交换异常消息永不包含 bearer/JWT/token；交换失败不切号（不是账号故障）。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from datetime import datetime
from typing import Any

from fastapi import HTTPException

# 接缝约定：函数体内对「测试可注入接缝」（monkeypatch 打在本包命名空间上的
# 名字，见包 __init__ 兼容约定）及包内共享状态经 _ns 调用期解析。
import buddy_proxy.trae.pat as _ns

from buddy_proxy.trae.credentials import _build_headers

from .config import (
    _AUTH_URL,
    PatCredentials,
    PatProfile,
    _EXCHANGE_TIMEOUT_S,
    _REFRESH_MARGIN_S,
    _TOKEN_URL,
    ensure_pat_config,
)
from .store import _account_state, _ensure_fingerprint, _mutate_account, _refresh_lock
from . import net

log = logging.getLogger(__name__)

def _parse_expired_at(text: str, fallback: float) -> float:
    try:
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return fallback


def _exchange(bearer: str) -> tuple[str, str, float]:
    """两步交换；异常消息永不包含 bearer/JWT/token。"""
    auth_url = os.environ.get(_AUTH_URL, "").strip()
    token_url = os.environ.get(_TOKEN_URL, "").strip()
    if not auth_url or not token_url:
        raise HTTPException(status_code=503, detail="PAT 换 token 缺少端点配置")
    request = urllib.request.Request(
        auth_url, data=b"", method="GET",
        headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json",
                 "User-Agent": "ByteDanceCLI/1.0"},
    )
    with net.open_direct(request, timeout=_EXCHANGE_TIMEOUT_S) as response:
        jwt = (response.headers.get("x-jwt-token") or "").strip()
    if not jwt:
        raise HTTPException(status_code=502, detail="PAT 第一步交换未返回令牌")
    request = urllib.request.Request(
        token_url, data=b"{}", method="POST",
        headers={"x-jwt-token": jwt, "Accept": "application/json",
                 "Content-Type": "application/json", "User-Agent": "ByteDanceCLI/1.0"},
    )
    with net.open_direct(request, timeout=_EXCHANGE_TIMEOUT_S) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    result = payload.get("Result") or payload.get("result") or {}
    token = result.get("Token") or ""
    uid = result.get("UserID") or result.get("userid") or ""
    # 收尾空白必须清掉：带尾随换行/空格的 token 直接进 Authorization: Bearer
    # 会构成非法头被上游拒（历史上一直 strip，重构时漏掉了）。
    if isinstance(token, str):
        token = token.strip()
    if isinstance(uid, str):
        uid = uid.strip()
    if not isinstance(token, str) or not token:
        raise HTTPException(status_code=502, detail="PAT 第二步交换未返回 token")
    if not isinstance(uid, str):
        uid = ""
    expires_at = _parse_expired_at(
        str(result.get("ExpiredAt") or ""), fallback=time.time() + 6.5 * 86400)
    return token, uid, expires_at


def _credentials_from_state(state: dict[str, Any]) -> PatCredentials | None:
    token = state.get("cloud_ide_token")
    if not isinstance(token, str) or not token:
        return None
    return PatCredentials(token, str(state.get("uid") or ""),
                          str(state.get("machine_id") or ""),
                          str(state.get("device_id") or ""))


def _get_profile_credentials(
    profile: PatProfile,
    *,
    force_refresh: bool = False,
    rejected_token: str | None = None,
) -> PatCredentials:
    """账号级取 token；锁内二次检查避免普通刷新和并发 401 强刷惊群。"""
    machine_id, device_id = _ensure_fingerprint(profile.cache_key)
    with _refresh_lock(profile.cache_key):
        state = _account_state(profile.cache_key)
        current = _credentials_from_state(state)
        try:
            expires_at = float(state.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        now = time.time()
        cache_is_fresh = current is not None and expires_at - now > _REFRESH_MARGIN_S
        if not force_refresh and cache_is_fresh:
            return current
        # 并发请求都拿旧 token 收到 401 时，仅首个线程交换；其余复用新 token。
        if force_refresh and rejected_token and current and current.token != rejected_token \
                and expires_at - now > 300:
            return current
        if not profile.bearer:
            # 直传 cloud_ide_token 模式（如桌面端导入）：无 bearer 可换签，
            # 未被 401 拒绝时直接落盘并返回；被 401 拒绝则直接报错换号。
            if force_refresh and rejected_token == profile.cloud_ide_token:
                raise HTTPException(status_code=401, detail="PAT direct cloud_ide_token expired or rejected")
            if current is not None and current.token == profile.cloud_ide_token:
                return current
            direct_exp = profile.expires_at or (now + 365 * 86400)

            def store_direct(account: dict[str, Any]) -> None:
                account.update({
                    "cloud_ide_token": profile.cloud_ide_token,
                    "uid": profile.uid,
                    "expires_at": direct_exp,
                    "refreshed_at": time.time(),
                    "machine_id": machine_id,
                    "device_id": device_id,
                })

            state = _mutate_account(profile.cache_key, store_direct)
            credentials = _credentials_from_state(state)
            assert credentials is not None
            return credentials
        try:
            token, uid, expires_at = _ns._exchange(profile.bearer)
        except Exception as exc:
            # 交换服务失败不是账号故障，绝不切号。普通临期刷新可沿用仍有效缓存；
            # 401 后的强刷若仍是被拒 token，则必须直接失败，不能把它当新凭据重放。
            may_reuse_current = (
                current is not None
                and expires_at - now > 300
                and (not force_refresh or current.token != rejected_token)
            )
            if may_reuse_current:
                log.warning("PAT token 刷新失败，账号序号=%d，沿用未过期缓存（%s）",
                            profile.index, type(exc).__name__)
                return current
            if isinstance(exc, HTTPException):
                raise
            log.warning("PAT token 刷新失败，账号序号=%d（%s）",
                        profile.index, type(exc).__name__)
            raise HTTPException(status_code=502, detail="PAT credential refresh unavailable") from None

        def store(account: dict[str, Any]) -> None:
            account.update({"cloud_ide_token": token, "uid": uid,
                            "expires_at": expires_at, "refreshed_at": time.time(),
                            "machine_id": machine_id, "device_id": device_id})

        state = _mutate_account(profile.cache_key, store)
        log.info("PAT token 已刷新，账号序号=%d", profile.index)
        credentials = _credentials_from_state(state)
        assert credentials is not None
        return credentials


def get_pat_credentials(
    force_refresh: bool = False,
    *,
    account_id: str | None = None,
    rejected_token: str | None = None,
) -> tuple[str, str]:
    """兼容旧接口，默认使用稳定首账号；可显式指定账号。"""
    profiles = ensure_pat_config()
    profile = next((item for item in profiles if item.id == account_id), None) if account_id else profiles[0]
    if profile is None:
        raise HTTPException(status_code=401, detail="PAT 账号不存在")
    credentials = _get_profile_credentials(
        profile, force_refresh=force_refresh, rejected_token=rejected_token)
    return credentials.token, credentials.uid


def _pat_headers(credentials: PatCredentials, accept: str) -> dict[str, str]:
    return {**_build_headers(credentials.token, credentials.uid,
                             machine_id=credentials.machine_id,
                             device_id=credentials.device_id),
            "Accept": accept}
