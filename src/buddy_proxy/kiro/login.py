"""Kiro 登录：浏览器授权（IdC / 社交登录）+ auth 文件导入。

## 两条路

1. **导入 auth 文件**（**:func:`import_auth_file`**，推荐）——Kiro IDE 已经在
   本机登录过的话，凭据就在 ``~/.aws/sso/cache/kiro-auth-token.json``。
   一键导入最省事，也不需要用户重新授权。

2. **设备授权码流程**（:func:`start_device_login` + :func:`poll_device_login`）——
   走 AWS SSO OIDC 的 device code，用户在浏览器里输入验证码完成授权。
   适合没有 Kiro IDE 的机器。

## 为什么导入优先

Kiro 的官方登录要过 AWS SSO / IAM Identity Center，device code 流程需要先知道
**企业 start URL**（``https://d-xxxxxxxx.awsapps.com/start``）和注册过的 client。
这些东西只有 Kiro 客户端自己知道。所以：

- 能从本机缓存拿到 client 注册信息 → 可以走 device code；
- 拿不到 → 只能让用户从 IDE 里登录一次再导入。

本模块把两条都实现，管理页默认推荐「导入」。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import credentials as creds
from ..core.account_store import read_json_dict

log = logging.getLogger(__name__)


class LoginError(RuntimeError):
    """登录流程中的可读错误（给管理页直接展示）。"""


# ---------------------------------------------------------------------------
# 导入本地 auth 文件
# ---------------------------------------------------------------------------


def detected_auth_files() -> list[dict[str, Any]]:
    """扫描本机可导入的 kiro auth 文件（供管理页列出候选）。

    返回 ``[{path, name, has_token, expires_at, provider, region}]``，按修改
    时间倒序。只做**只读**检查，不解密、不外发。
    """
    import pathlib

    out: list[dict[str, Any]] = []
    candidates: list[pathlib.Path] = [creds.IDE_TOKEN_PATH]
    try:
        candidates.extend(sorted(
            creds.SSO_CACHE_DIR.glob("kiro-auth-token*.json"),
            key=lambda p: p.stat().st_mtime, reverse=True))
    except OSError:
        pass
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        data = read_json_dict(path)
        if not data:
            continue
        has_token = bool(data.get("accessToken") or data.get("access_token")
                         or data.get("refreshToken") or data.get("refresh_token"))
        if not has_token:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append({
            "path": key,
            "name": path.name,
            "has_token": True,
            "expires_at": creds._parse_expiry(data.get("expiresAt")),  # noqa: SLF001
            "provider": str(data.get("provider") or ""),
            "region": str(data.get("region") or creds.DEFAULT_REGION),
            "mtime": mtime,
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _fetch_usage_limits(token: str, region: str) -> dict[str, Any]:
    """用已有 accessToken 调一次 ``getUsageLimits``（带 ``isEmailRequired=true``）。"""
    # isEmailRequired=true 才会带回 userInfo.email（false 时该字段恒 null）。
    # 账号显示名与多账号去重都要用 userInfo（email + userId）。
    url = (f"https://codewhisperer.{region}.amazonaws.com/getUsageLimits"
           "?origin=AI_EDITOR&isEmailRequired=true")
    req = urllib.request.Request(url, headers=_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        if exc.code == 403:
            raise LoginError(
                f"上游拒绝（403）：{body or '订阅不支持或凭据无效'}") from exc
        raise LoginError(f"探活失败 HTTP {exc.code}: {body}") from exc
    except Exception as exc:  # noqa: BLE001
        raise LoginError(f"探活失败: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise LoginError("探活失败: 上游响应不是 JSON 对象")
    return data


def import_auth_file(path: str | None = None, *, label: str = "") -> creds.KiroAccount:
    """导入一份 auth 文件（见 credentials.prepare_auth_file）。

    ⚠ **必须先在内存里刷新 + 探活拿到 ``userInfo.userId``，再落盘**：
    同一 AWS IAM Identity Center 企业目录下的多个账号共用相同的
    ``profileArn`` 和 ``clientIdHash``，如果先按旧逻辑直接落盘，第二个账号
    会在探活前就把第一个账号的凭据文件覆盖掉（真实踩坑）。
    """
    try:
        merged = creds.prepare_auth_file(path, label=label)
        cred = creds._normalize_cred(merged, account_id="pending")  # noqa: SLF001
        if creds.needs_refresh(cred):
            cred = creds.refresh_tokens(cred)
        data = _fetch_usage_limits(
            str(cred.get("access_token") or ""),
            str(cred.get("region") or creds.DEFAULT_REGION),
        )
    except LoginError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成可操作提示，且绝不污染磁盘
        raise LoginError(
            f"凭据不可用，未导入：{str(exc)[:200]}。"
            "请在 Kiro IDE 里重新登录后再导入") from exc

    info = data.get("userInfo") or {}
    user_id = str(info.get("userId") or "").strip()
    email = str(info.get("email") or "").strip()
    if user_id:
        cred["user_id"] = user_id
    if email:
        cred["email"] = email

    # 先找是否已有同 user_id（或同邮箱的旧格式账号），命中则原地更新，否则分配新 id
    account_id = creds.find_existing_account_id(user_id=user_id, email=email)
    if not account_id:
        account_id = creds._account_id(  # noqa: SLF001
            user_id=user_id,
            email=email,
            profile_arn=str(cred.get("profile_arn") or ""),
            client_id_hash=str(cred.get("client_id_hash") or ""),
            refresh_token=str(cred.get("refresh_token") or ""),
            access_token=str(cred.get("access_token") or ""),
        )
    cred["account_id"] = account_id
    if email and not label:
        existing_cred = creds.load_account_cred(account_id)
        if not existing_cred:
            cred["label"] = email
    account = creds.save_account_cred(cred)
    if email and not label:
        try:
            creds.set_label(account.id, email)
            account = next((a for a in creds.list_accounts() if a.id == account.id), account)
        except Exception:  # noqa: BLE001
            pass
    return account


# ---------------------------------------------------------------------------
# 凭据探活
# ---------------------------------------------------------------------------


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        # 这个头是必须的：少了它上游回 403 说订阅不支持（见 credentials 模块头）
        "User-Agent": f"aws-sdk-js/1.0.0 Kiro/{creds.KIRO_UA_VERSION}",
    }


def probe_account(account_id: str) -> dict[str, Any]:
    """用该账号真实拉一次 ``getUsageLimits``，确认凭据可用。

    返回 ``{"ok": True, "subscription": ..., "usage": ...}``；失败抛异常
    （含可读原因）。这是导入/登录后的**唯一可信验证**——文件存在不代表 token
    能刷新、能调通。
    """
    token, cred = creds.ensure_account_token(account_id)
    region = str(cred.get("region") or creds.DEFAULT_REGION)
    data = _fetch_usage_limits(token, region)
    info = data.get("userInfo") or {}
    email = str(info.get("email") or "").strip()
    user_id = str(info.get("userId") or "").strip()
    if (user_id and not cred.get("user_id")) or (email and not cred.get("email")):
        try:
            updated = dict(cred)
            if user_id:
                updated["user_id"] = user_id
            if email:
                updated["email"] = email
            creds.save_account_cred(updated)
        except Exception:  # noqa: BLE001 - 回填身份信息失败不影响探活结果
            pass
    if email:
        # 探活顺手把邮箱写进索引当显示名：导入时用户不一定填 label，
        # 不写的话列表里只剩一串 ``c-xxxxxxxx``，看不出是谁。
        try:
            creds.set_label(account_id, email)
        except Exception:  # noqa: BLE001 - 写名失败不影响探活结果
            pass
    return {
        "ok": True,
        "subscription": str((data.get("subscriptionInfo") or {}).get("subscriptionTitle") or ""),
        "email": email,
        "usage": data,
    }


# ---------------------------------------------------------------------------
# 设备授权码流程（AWS SSO OIDC Device Authorization）
# ---------------------------------------------------------------------------

_DEVICE_AUTH_URL = "https://oidc.{region}.amazonaws.com/device_authorization"
_DEVICE_TOKEN_URL = "https://oidc.{region}.amazonaws.com/token"

#: AWS SSO 的设备授权 scope。
_DEVICE_SCOPE = "codewhisperer:completions codewhisperer:analysis codewhisperer:conversations"

#: 登录会话（进程内）。同一时刻只保留最近一次，够用且避免状态泄漏。
_pending: dict[str, Any] = {}


def register_client(region: str = creds.DEFAULT_REGION,
                    client_name: str = "buddy-proxy") -> tuple[str, str]:
    """注册一个 OIDC public client，返回 ``(clientId, clientSecret)``。

    AWS SSO 的设备码流程要求先注册 client。注册是**幂等**的（同名会新建一个，
    但调用方每次登录只用一个），拿到后与 token 一起缓存到 sso cache 目录，
    以便后续 refresh 复用（credentials 模块按 clientIdHash 找它）。
    """
    url = f"https://oidc.{region}.amazonaws.com/client/register"
    payload = json.dumps({
        "clientName": client_name,
        "clientType": "public",
        "scopes": _DEVICE_SCOPE.split(),
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise LoginError(f"注册 client 失败 HTTP {exc.code}: {body}") from exc
    cid = str(data.get("clientId") or "").strip()
    secret = str(data.get("clientSecret") or "").strip()
    if not cid or not secret:
        raise LoginError("注册 client 的响应里缺少 clientId/clientSecret")
    return cid, secret


def start_device_login(region: str = creds.DEFAULT_REGION) -> dict[str, Any]:
    """发起设备授权，返回给用户看的``{url, code, expires_in}``。

    用户拿 ``code`` 去 ``url`` 里输入即可。之后前端轮询
    :func:`poll_device_login`。
    """
    cid, secret = register_client(region)
    body = urllib.parse.urlencode({
        "client_id": cid,
        "client_secret": secret,
        "scope": _DEVICE_SCOPE,
    }).encode("utf-8")
    req = urllib.request.Request(
        _DEVICE_AUTH_URL.format(region=region), data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise LoginError(
            f"设备授权失败 HTTP {exc.code}: {detail}。"
            "若提示 startUrl 相关错误，请改用「导入 auth 文件」——"
            "设备码流程需要 IAM Identity Center 的 start URL，只有 Kiro 客户端知道") from exc

    _pending.clear()
    _pending.update({
        "region": region,
        "client_id": cid,
        "client_secret": secret,
        "device_code": str(data.get("deviceCode") or ""),
        "user_code": str(data.get("userCode") or ""),
        "interval": int(data.get("interval") or 5),
        "expires_at": time.time() + float(data.get("expiresIn") or 600),
        "started_at": time.time(),
    })
    return {
        "verification_uri": (data.get("verificationUriComplete")
                             or data.get("verificationUri") or ""),
        "user_code": _pending["user_code"],
        "expires_in": int(float(data.get("expiresIn") or 600)),
        "interval": _pending["interval"],
    }


def poll_device_login() -> dict[str, Any]:
    """轮询一次设备授权状态。

    返回 ``{"status": "pending"|"slow_down"|"expired"|"ok", ...}``；成功时
    凭据已落盘并返回账号快照。
    """
    if not _pending:
        raise LoginError("没有进行中的登录（先调用 /login/start）")
    if time.time() > float(_pending.get("expires_at") or 0):
        _pending.clear()
        return {"status": "expired"}
    body = urllib.parse.urlencode({
        "client_id": _pending["client_id"],
        "client_secret": _pending["client_secret"],
        "grantType": "urn:ietf:params:oauth:grant-type:device_code",
        "deviceCode": _pending["device_code"],
    }).encode("utf-8")
    req = urllib.request.Request(
        _DEVICE_TOKEN_URL.format(region=_pending["region"]), data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        if "authorization_pending" in detail:
            return {"status": "pending"}
        if "slow_down" in detail:
            return {"status": "slow_down"}
        if "expired_token" in detail:
            _pending.clear()
            return {"status": "expired"}
        raise LoginError(f"轮询失败 HTTP {exc.code}: {detail[:200]}") from exc
    access = str(data.get("accessToken") or "").strip()
    if not access:
        return {"status": "pending"}
    # 落盘：把 client 注册信息写进 sso cache，供后续 refresh 找得到
    client_hash = _persist_client(_pending["client_id"], _pending["client_secret"],
                                  _pending["region"])
    cred_dict = {
        "accessToken": access,
        "refreshToken": str(data.get("refreshToken") or ""),
        "expiresAt": time.time() + float(data.get("expiresIn") or 3600),
        "clientIdHash": client_hash,
        "clientId": _pending["client_id"],
        "clientSecret": _pending["client_secret"],
        "authMethod": "IdC",
        "provider": "Enterprise",
        "region": _pending["region"],
    }
    try:
        usage = _fetch_usage_limits(access, _pending["region"])
        info = usage.get("userInfo") or {}
        uid = str(info.get("userId") or "").strip()
        mail = str(info.get("email") or "").strip()
        if uid:
            cred_dict["userId"] = uid
        if mail:
            cred_dict["email"] = mail
            cred_dict["label"] = mail
        existing_id = creds.find_existing_account_id(user_id=uid, email=mail)
        if existing_id:
            cred_dict["account_id"] = existing_id
    except Exception:  # noqa: BLE001 - 设备授权成功后若探活暂失败，仍保留凭据
        pass
    account = creds.save_account_cred(cred_dict)
    _pending.clear()
    return {"status": "ok", "account": {
        "id": account.id, "label": account.label, "region": account.region}}


def _persist_client(client_id: str, client_secret: str, region: str) -> str:
    """把 client 注册信息写进 sso cache，返回 clientIdHash。

    后续 refresh 要按这个 hash 找 ``clientId/clientSecret``（见
    credentials._load_client）。这里复用 Kiro 自己的目录，好处是导入/登录两条
    路产生的凭据形状完全一致。
    """
    import hashlib

    client_hash = hashlib.sha1(f"{client_id}:{region}".encode()).hexdigest()
    path = creds.SSO_CACHE_DIR / f"{client_hash}.json"
    try:
        creds.SSO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        from ..core.account_store import atomic_write_json

        atomic_write_json(path, {
            "clientId": client_id,
            "clientSecret": client_secret,
            "expiresAt": "2099-01-01T00:00:00.000Z",
        })
    except OSError as exc:
        raise LoginError(f"写入 client 注册信息失败: {exc}") from exc
    return client_hash
