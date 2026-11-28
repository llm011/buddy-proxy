"""书生·端砚（inkstone）登录态：解 Electron safeStorage 里的 SSO 凭据。

## 凭据从哪来（实测 2026-11-29，书生·端砚 0.2.13）

端砚是 Electron 应用（bundle id ``cn.internai.inkstone``），登录态**不像 Loomy
那样明文落盘**，而是走 Electron 的 ``safeStorage``：::

    ~/Library/Application Support/inkstone-desktop/credentials/credential_<uuid>.bin
    %APPDATA%/inkstone-desktop/credentials/credential_<uuid>.bin

文件是 macOS 的 ``v10`` 格式：3 字节 magic + AES-128-CBC 密文。解出来是::

    {"version":1,
     "credential":{
        "token":"eyJ0eXBlIjoiSldUIiwiYWxnIjoiSFM1MTIifQ....",   # JWT，14 天
        "environment":"production",
        "expiresAtMs":1797100526000,
        "user":{"ssoUid":"24000318","username":"...","nickname":"..."}}}

``token`` 是 JWT（``iss=OpenXLab``，``rol=ROLE_REGISTER``，``jti`` 就是
``ssoUid``），**有效期 14 天**（``exp - iat == 1209600``）。

## 口令在哪：macOS Keychain

Electron safeStorage 的加密口令存在系统钥匙串里，条目名是
``<应用名> Safe Storage``（这里是 ``书生·端砚 Safe Storage``），用
``security find-generic-password -s <name> -w`` 取。派生方式与 Chromium 一致::

    key = PBKDF2-HMAC-SHA1(password, b"saltysalt", 1003, 16)
    plain = AES-128-CBC(key, iv=b" " * 16).decrypt(blob)

**不要把口令写进代码或日志**——它跟用户钥匙串绑定，换机就失效。取不到就报
一个可操作的错，让用户在端砚里重新登录（登录会刷新凭据文件）。

## 为什么不做自动登录

端砚登录走 ``sso.openxlab.org.cn``（微信扫码 / 手机号），要复刻 OpenXLab SSO
的 OAuth 流程 + 客户端签名，而 token 14 天续一次——用户重开端砚一次的成本远
低于维护一套登录链路。**登录态本就该由用户在官方客户端里授权**，这与 Loomy
通道的口径一致。

## 与 Loomy 的关键差异：协议不是 OpenAI 兼容

Loomy 的上游是 OpenAI 兼容网关，``body`` 可以直通。端砚**没有** OpenAI 兼容
端点（``/v1/chat/completions`` 全是 405/404），它自己的协议是两段式：

1. ``POST /api/orbit/v1/chat/new`` 建会话 → ``data.id``
2. ``POST /api/orbit/v1/chat/{id}/local-turns`` 注册 turn（见 provider.py）
3. ``POST /api/orbit/v1/local-agent/messages`` 发推理（SSE）

所以 ``provider.py`` 里要做完整的请求体构造与 SSE 帧解析，不像 Loomy 那样直通。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 端砚生产网关（``MAIN_VITE_CORE_API_BASE_URL``）。模型目录、会话、推理都挂在
#: ``/api/orbit/v1`` 下。
DEFAULT_BASE_URL = "https://discovery.intern-ai.org.cn/api/orbit/v1"

#: 套餐额度接口挂在 ``/api/tokenplan/v1``（**不在** ``/api/orbit/v1`` 下）。
#:
#: 这是实测踩出来的：按 orbit 前缀拼 ``/tokenplan/v1/credits/balance`` 一律
#: 404 ``page not found``（nginx 层就没这个路由），去掉 orbit 才对。
DEFAULT_BILLING_BASE_URL = "https://discovery.intern-ai.org.cn/api/tokenplan/v1"

#: 端砚客户端版本，随推理请求的 ``metadata.client_version`` 发。
#:
#: 实测缺这个字段直接 422 ``metadata is missing required field client_version``
#: ——**必填**，不是可选。
DEFAULT_CLIENT_VERSION = "0.2.13"

#: macOS 钥匙串里 safeStorage 口令的条目名（``<应用名> Safe Storage``）。
SAFESTORAGE_SERVICE = "书生·端砚 Safe Storage"

#: 推理协议的版本号。上游对 ``protocol_version != "2.0"`` 直接拒。
PROTOCOL_VERSION = "2.0"

#: 业务码：成功。
OK_CODE = 0


class AuthError(Exception):
    """登录态缺失 / 失效。消息直接给用户看，要可操作。"""


@dataclass(frozen=True)
class InkstoneSession:
    """一份解析好的登录态。"""

    token: str
    sso_uid: str = ""
    username: str = ""
    environment: str = "production"
    expires_at_ms: int = 0

    @property
    def display(self) -> str:
        """日志里用的脱敏标识（**绝不打印 token 原文**）。"""
        if self.username:
            return self.username
        if self.sso_uid:
            return f"uid {self.sso_uid}"
        return "unknown"

    @property
    def expired(self) -> bool:
        """是否已过期（``expires_at_ms`` 缺失时按「未知」当作未过期）。"""
        if not self.expires_at_ms:
            return False
        import time

        return self.expires_at_ms / 1000.0 <= time.time()


def _app_support_dir() -> Path:
    """端砚的用户数据目录（Electron ``app.getPath('userData')``）。"""
    override = os.getenv("INKSTONE_USER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "inkstone-desktop"
    if sys.platform.startswith("win"):
        appdata = os.getenv("APPDATA", "").strip()
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "inkstone-desktop"
    return Path.home() / ".config" / "inkstone-desktop"


def credentials_dir() -> Path:
    """``credentials/`` 目录（一个账号一个 ``credential_<uuid>.bin``）。"""
    return _app_support_dir() / "credentials"


def credential_file() -> Path | None:
    """最新的凭据文件；没有则 ``None``。

    端砚按账号存多个 ``credential_<uuid>.bin``，取 **mtime 最新**的那个——用户
    换号登录后会新建一个，最新的就是当前在用的。
    """
    directory = credentials_dir()
    try:
        candidates = [p for p in directory.glob("credential_*.bin") if p.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _keychain_password(service: str) -> bytes | None:
    """从 macOS 钥匙串取 safeStorage 口令；取不到返回 ``None``。

    非 macOS 返回 ``None``（Linux 用 libsecret、Windows 用 DPAPI，各自算法不同，
    本实现只覆盖 macOS——这个应用本身也只发了 macOS 版）。
    """
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip().encode("utf-8") or None


def _decrypt_v10(blob: bytes, password: bytes) -> bytes | None:
    """解 ``v10`` 密文（PBKDF2-HMAC-SHA1 → AES-128-CBC）。

    返回**已去填充**的明文；失败返回 ``None``（口令不对 / 格式不符都算失败，
    属预期，不要抛）。
    """
    try:
        from buddy_proxy.core.aes_pure import aes_cbc_decrypt
    except ImportError:  # pragma: no cover - 环境异常
        return None
    try:
        key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
        plain = aes_cbc_decrypt(key, b" " * 16, blob)
    except Exception:  # noqa: BLE001 - 解密失败属预期
        return None
    if not plain:
        return None
    # PKCS#7 去填充
    pad = plain[-1]
    if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
        plain = plain[:-pad]
    return plain


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    """读 JWT 的 payload（**不验签**——我们只用它取 exp / jti）。"""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}


def load_session() -> InkstoneSession:
    """读并解密登录态；失败抛 :class:`AuthError`（带可操作提示）。

    ``INKSTONE_CREDENTIAL_JSON`` 可以给**明文 JSON 文本**，也可以给**文件路径**
    ——测试里写文件更直观，排查时贴一行 JSON 更快，两种都收。
    """
    override = os.getenv("INKSTONE_CREDENTIAL_JSON", "").strip()
    plain: bytes | None = None

    if override:
        candidate = Path(override)
        if candidate.is_file():
            try:
                plain = candidate.read_bytes()
            except OSError as exc:
                raise AuthError(f"读取端砚登录态失败（{candidate}）：{exc}") from exc
            path = candidate
        else:
            # 不是已存在的文件 → 当成明文 JSON 正文
            plain = override.encode("utf-8")
            path = Path("<INKSTONE_CREDENTIAL_JSON>")
    else:
        path = credential_file()
        if path is None:
            raise AuthError(
                f"未找到端砚登录态（{credentials_dir()}）。"
                "请先启动书生·端砚并完成登录，再重启 buddy。"
            )

    if plain is None:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise AuthError(f"读取端砚登录态失败（{path}）：{exc}") from exc
    else:
        raw = plain

    if plain is None:
        password = _keychain_password(
            os.getenv("INKSTONE_SAFESTORAGE_SERVICE", "").strip() or SAFESTORAGE_SERVICE
        )
        if password is None:
            raise AuthError(
                "取不到端砚的 safeStorage 口令（钥匙串条目 "
                f"「{SAFESTORAGE_SERVICE}」）。"
                "请在端砚里重新登录一次（会重建该钥匙串条目），再重启 buddy。"
            )
        if not raw.startswith(b"v10"):
            raise AuthError(f"端砚登录态格式不认识（{path}）：缺少 v10 magic")
        plain = _decrypt_v10(raw[3:], password)
        if plain is None:
            raise AuthError(
                f"端砚登录态解密失败（{path}）。"
                "口令可能已轮换：请在端砚里重新登录后再试。"
            )

    try:
        data = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthError(f"端砚登录态不是合法 JSON（{path}）：{exc}") from exc
    if not isinstance(data, dict):
        raise AuthError(f"端砚登录态格式异常（{path}）：期望对象")

    credential = data.get("credential")
    if not isinstance(credential, dict):
        raise AuthError(
            f"端砚登录态里没有 credential（{path}）。请在端砚里重新登录后再试。"
        )
    token = str(credential.get("token") or "").strip()
    if not token:
        raise AuthError(
            f"端砚登录态里没有 token（{path}）。请在端砚里重新登录后再试。"
        )

    user = credential.get("user")
    user = user if isinstance(user, dict) else {}
    claims = _decode_jwt_claims(token)
    expires_at_ms = int(credential.get("expiresAtMs") or 0)
    if not expires_at_ms and claims.get("exp"):
        expires_at_ms = int(claims["exp"]) * 1000

    session = InkstoneSession(
        token=token,
        sso_uid=str(user.get("ssoUid") or claims.get("jti") or "").strip(),
        username=str(user.get("nickname") or user.get("username") or "").strip(),
        environment=str(credential.get("environment") or "production").strip(),
        expires_at_ms=expires_at_ms,
    )
    if session.expired:
        raise AuthError(
            "端砚登录态已过期。请在端砚里重新登录（token 有效期 14 天），再重启 buddy。"
        )
    return session


def resolve_base_url() -> str:
    """网关 baseURL（``/api/orbit/v1``）。"""
    explicit = os.getenv("INKSTONE_BASE_URL", "").strip()
    return (explicit or DEFAULT_BASE_URL).rstrip("/")


def resolve_billing_base_url() -> str:
    """套餐额度 baseURL（``/api/tokenplan/v1``）。"""
    explicit = os.getenv("INKSTONE_BILLING_BASE_URL", "").strip()
    return (explicit or DEFAULT_BILLING_BASE_URL).rstrip("/")


def client_version() -> str:
    """客户端版本号（推理请求 ``metadata.client_version`` 必填）。"""
    return os.getenv("INKSTONE_CLIENT_VERSION", "").strip() or DEFAULT_CLIENT_VERSION
