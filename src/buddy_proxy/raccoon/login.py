"""小浣熊登录：手机号密码登录、会话导入、桌面客户端登录态复用。

## 为什么要有这一层（「一个月登录一次」的解法）

小浣熊网页端是手机号+密码登录，会话 JWT 存在 ``xhx_token`` cookie 里，约 30
天到期。开源实现（Coding2API / magpie 插件）都只做「手工从 DevTools 复制
cookie 再粘进来」——一次性、到期必须人工重取，多账号管理极痛。

本模块给出**三条**导入路径，其中第 1 条能自动续期：

1. :func:`login_with_password` —— 手机号 + 密码直登上游，拿到新会话。
   密码用 AES-CFB 加密落盘（与上游网页端同款算法），因此账号可以**无人值守
   自动续期**，不必一个月人工换一次 token。多账号批量导入后各自独立续期。
2. :func:`import_session` —— 粘 ``xhx_token``（也接受裸 JWT / 整段 cookie /
   ``{"access_token": "..."}``）。没有密码时的兜底，30 天后需重取。
3. :func:`read_desktop_auth` —— 只读本机小浣熊桌面客户端的 ``auth.json``，
   不动它的 refresh token。

## 密码加密格式

上游网页端：``AES-CFB``，key ``senseraccoon2023``，16 字节随机 IV **前置**，
整体 Base64。本模块按同款实现，所以落盘的密文与浏览器发出的形状一致。

**这是明文等价物**：能解出密码，安全性完全依赖 ``~/.buddy-proxy`` 的
0600/0700 权限边界——与其它通道存 refresh_token 是同一信任级别。因此在
管理页不回显、导出时也不带（见 web 层）。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import pathlib
import time
from typing import Any

import httpx

from ..core.aes_pure import aes_cbc_encrypt
from . import credentials as creds
from .credentials import AuthError, save_account_cred

log = logging.getLogger(__name__)

#: 上游站点（``RACCOON_API_ENDPOINT`` 可覆盖，测试/代理用）。
DEFAULT_ENDPOINT = "https://xiaohuanxiong.com"

#: 网页端登录接口。
_LOGIN_PATH = "/api/web/auth/v1/login_with_password"
_USER_INFO_PATH = "/api/web/auth/v1/user_info"

#: 上游密码加密参数（与网页端同款，实测口径）。
_PASSWORD_AES_KEY = b"senseraccoon2023"

#: 密码最短长度（上游要求 ≥8，短了直接本地拒绝，省一次网络往返）。
MIN_PASSWORD_LEN = 8

#: 登录/查询超时。
_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=15.0)


def endpoint() -> str:
    """上游站点根地址。"""
    return (os.environ.get("RACCOON_API_ENDPOINT", "").strip()
            or DEFAULT_ENDPOINT).rstrip("/")


# ---------------------------------------------------------------------------
# 密码加解密
# ---------------------------------------------------------------------------

def encrypt_password(password: str) -> str:
    """按网页端口径加密密码：AES-CFB(key, IV 前置) → Base64。

    返回的字符串可直接放进登录请求体的 ``password`` 字段。
    """
    iv = os.urandom(16)
    # CFB 用 CBC 的块函数逐块异或实现：CFB-128 的加密与 CBC 单块加密同源，
    # 区别只在反馈的是密文块。这里直接复用 aes_cbc_encrypt 会引入填充，
    # 所以用「零填充 + 只取前 n 字节」的方式构造 CFB 流。
    plain = password.encode("utf-8")
    out = b""
    prev = iv
    for i in range(0, len(plain), 16):
        chunk = plain[i:i + 16]
        ks = aes_cbc_encrypt(_PASSWORD_AES_KEY, b"\x00" * 16, prev)[:len(chunk)]
        enc = bytes(chunk[j] ^ ks[j] for j in range(len(chunk)))
        out += enc
        # CFB 反馈的是**密文**块（不足 16 字节时补零继续，与网页端一致）
        prev = (enc + b"\x00" * 16)[:16]
    return base64.b64encode(iv + out).decode("ascii")


def decrypt_password(blob: str) -> str:
    """解出落盘的密码密文（仅用于调试/校验；转发路径不解密）。

    对称实现见 :func:`encrypt_password`。
    """
    raw = base64.b64decode(blob)
    iv, data = raw[:16], raw[16:]
    out = b""
    prev = iv
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        ks = aes_cbc_encrypt(_PASSWORD_AES_KEY, b"\x00" * 16, prev)[:len(chunk)]
        dec = bytes(chunk[j] ^ ks[j] for j in range(len(chunk)))
        out += dec
        prev = (chunk + b"\x00" * 16)[:16]
    return out.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 上游交互
# ---------------------------------------------------------------------------

def _client() -> httpx.Client:
    return httpx.Client(timeout=_TIMEOUT, follow_redirects=True)


def _base_headers() -> dict[str, str]:
    """网页端请求的公共头。

    上游会看 UA / Origin / Referer；缺 UA 会被当异常客户端（与 trae 的
    4011 同类问题）。这些头是**识别成本最低**的一部分，不值得省。
    """
    return {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": endpoint(),
        "Referer": f"{endpoint()}/",
    }


def _extract_token(payload: Any) -> str:
    """从登录响应里挖出会话 JWT。

    上游把 token 放在哪一层不稳定（见过 ``data.access_token`` /
    ``data.token`` / 顶层 ``access_token``），这里按候选键逐个找，找不到
    再扫一遍嵌套 dict 里形如 JWT 的字符串。
    """
    if not isinstance(payload, dict):
        return ""
    for holder in (payload, payload.get("data"), payload.get("result")):
        if not isinstance(holder, dict):
            continue
        for key in ("access_token", "accessToken", "token", "xhx_token"):
            val = holder.get(key)
            if isinstance(val, str) and val.count(".") == 2 and len(val) > 40:
                return val
    # 兜底：任意嵌套层里的 JWT 形状字符串
    stack = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for v in cur.values():
                if isinstance(v, str) and v.count(".") == 2 and len(v) > 40:
                    return v
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return ""


def _raise_for_login(payload: Any, status: int) -> None:
    """把上游的登录失败翻成人话。"""
    code = ""
    msg = ""
    if isinstance(payload, dict):
        code = str(payload.get("code") or payload.get("error") or "")
        msg = str(payload.get("msg") or payload.get("message")
                  or payload.get("error_description") or "")
    hint = ""
    if "phone_or_password" in msg or "200004" in code:
        hint = "（手机号或密码不正确）"
    elif "captcha" in msg.lower() or "verify" in msg.lower():
        hint = "（上游要求验证码/人机校验，请改用「粘 xhx_token」方式导入）"
    elif status == 429:
        hint = "（登录过于频繁，稍后再试）"
    raise AuthError(f"小浣熊登录失败 HTTP {status} {code} {msg}{hint}".strip())


def login_with_password(phone: str, password: str) -> dict[str, Any]:
    """手机号 + 密码登录，返回上游原始响应（含会话 JWT）。

    密码先按网页端口径加密再发（明文不上网）。失败抛 :class:`AuthError`，
    消息里带上游的业务码，便于区分「密码错」与「要求验证码」。
    """
    phone = str(phone or "").strip()
    if not phone:
        raise AuthError("缺少手机号")
    if len(str(password or "")) < MIN_PASSWORD_LEN:
        raise AuthError(f"密码至少 {MIN_PASSWORD_LEN} 位（上游要求）")
    body = {"phone": phone, "password": encrypt_password(password)}
    url = f"{endpoint()}{_LOGIN_PATH}"
    try:
        with _client() as c:
            resp = c.post(url, json=body, headers=_base_headers())
    except httpx.HTTPError as exc:
        raise AuthError(f"登录请求失败：{exc}") from exc
    try:
        payload = resp.json()
    except ValueError:
        payload = {"raw": resp.text[:200]}
    if resp.status_code >= 400:
        _raise_for_login(payload, resp.status_code)
    token = _extract_token(payload)
    if not token:
        _raise_for_login(payload, resp.status_code or 200)
    return {"access_token": token, "raw": payload}


def fetch_user_info(access_token: str) -> dict[str, Any]:
    """用会话 JWT 查账号信息（昵称/手机号/信任状态）。

    这个接口同时是**会话健康度探针**：``trusted_device=false`` /
    ``needs_relogin=true`` 表示会话已失效，需要重新登录。失败返回空 dict
    而不是抛错——查信息失败不该阻断导入。
    """
    headers = _base_headers()
    headers["xhx_token"] = access_token
    try:
        with _client() as c:
            resp = c.get(f"{endpoint()}{_USER_INFO_PATH}", headers=headers)
    except httpx.HTTPError as exc:
        log.warning("raccoon user_info 请求失败: %s", exc)
        return {}
    if resp.status_code >= 400:
        return {}
    try:
        payload = resp.json()
    except ValueError:
        return {}
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else (payload if isinstance(payload, dict) else {})


def session_healthy(info: dict[str, Any]) -> bool:
    """会话是否仍然健康（``user_info`` 的信任状态直说）。

    上游对失效会话给的是 ``trusted_device=false`` / ``needs_relogin=true`` /
    ``device_bind_state=suspicious``——这些字段比 HTTP 200 更早暴露问题。
    """
    if not info:
        return True  # 查不到不代表失效，交给真实请求判
    if info.get("needs_relogin") is True:
        return False
    if info.get("trusted_device") is False:
        return False
    state = str(info.get("device_bind_state") or "").lower()
    return state not in ("suspicious", "required")


# ---------------------------------------------------------------------------
# 导入 / 重新登录
# ---------------------------------------------------------------------------

def _build_cred(token: str, *, phone: str = "", password: str = "",
                nickname: str = "", uid: str = "") -> dict[str, Any]:
    """把会话 JWT 组成落盘 cred（补齐 uid/到期时间/显示名）。"""
    uid = uid or creds.jwt_uid(token)
    exp = creds.jwt_expiry(token)
    cred: dict[str, Any] = {
        "access_token": token,
        "uid": uid,
        "phone": str(phone or "").strip(),
        "nickname": str(nickname or "").strip(),
        "expires_at": exp,
        "region": "cn",
    }
    if password:
        # 密码加密落盘：能解出明文（自动续期需要），安全性靠目录 0600/0700。
        cred["password"] = encrypt_password(password)
        cred["password_enc"] = "aes-cfb-b64"
    return cred


def import_session(raw: str, *, phone: str = "", password: str = "",
                   nickname: str = "") -> Any:
    """导入一个会话：接受裸 JWT / ``{"access_token": ...}`` / 整段 cookie。

    解析失败抛 :class:`AuthError`。成功返回 ``AccountRef``。

    带 ``password`` 时会一并落盘（加密），之后可自动续期；不带就只能等
    30 天后重新导入。
    """
    text = str(raw or "").strip()
    if not text:
        raise AuthError("没有内容可导入")
    token = _parse_session_blob(text)
    if not token:
        raise AuthError(
            "无法从内容里识别出会话 JWT。请粘贴 xhx_token 的值（形如 xxx.yyy.zzz），"
            "或 {\"access_token\": \"...\"}，或整段 cookie"
        )
    cred = _build_cred(token, phone=phone, password=password, nickname=nickname)
    if not cred["nickname"] and cred["phone"]:
        cred["nickname"] = cred["phone"]
    return save_account_cred(cred)


def _parse_session_blob(text: str) -> str:
    """从各种粘贴形态里挖出会话 JWT。"""
    # 1) JSON（含 access_token / token / 小浣熊导出格式）
    if text.startswith("{") or text.startswith("["):
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if payload is not None:
            token = _extract_token(payload)
            if token:
                return token
            if isinstance(payload, list) and payload:
                token = _extract_token(payload[0])
                if token:
                    return token
    # 2) 整段 cookie 串：找 xhx_token=...
    for part in text.replace(";", "\n").splitlines():
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition("=")
        if name.strip().lower() in ("xhx_token", "xhxtoken", "access_token"):
            value = value.strip().strip('"')
            if value.count(".") == 2 and len(value) > 40:
                return value
    # 3) 裸 JWT（可能带 Bearer 前缀）
    candidate = text.removeprefix("Bearer ").strip().strip('"')
    if candidate.count(".") == 2 and len(candidate) > 40 and " " not in candidate:
        return candidate
    # 4) 兜底：文本里任意 JWT 形状的片段
    for chunk in text.replace('"', " ").replace("'", " ").split():
        if chunk.count(".") == 2 and len(chunk) > 40:
            return chunk
    return ""


def relogin_account(account_id: str) -> dict[str, Any]:
    """用落盘的手机号+密码重新登录，更新该账号的会话并返回新 cred。

    这是「不用一个月人工换一次」的核心：调用方（``ensure_account_token``）
    发现 token 过期时自动走这里。失败抛 :class:`AuthError`，由 failover
    换号并在管理页提示重新导入。
    """
    cred = creds.load_account_cred(account_id)
    if cred is None:
        raise AuthError(f"raccoon 账号 {account_id} 的凭据不存在或已损坏")
    phone = str(cred.get("phone") or "").strip()
    enc = str(cred.get("password") or "").strip()
    if not phone or not enc:
        raise AuthError(
            f"raccoon 账号 {account_id} 没有手机号+密码，无法自动续期；"
            "请重新导入（建议同时填手机号与密码）"
        )
    password = decrypt_password(enc)
    result = login_with_password(phone, password)
    token = result["access_token"]
    cred["access_token"] = token
    cred["uid"] = cred.get("uid") or creds.jwt_uid(token)
    cred["expires_at"] = creds.jwt_expiry(token)
    cred["last_login"] = int(time.time())
    if not cred.get("nickname"):
        info = fetch_user_info(token)
        nick = str(info.get("nickname") or info.get("screen_name") or "").strip()
        if nick:
            cred["nickname"] = nick
    save_account_cred(cred)
    log.info("raccoon 账号 %s 已自动重新登录（会话续期成功）", account_id)
    return cred


# ---------------------------------------------------------------------------
# 桌面客户端登录态（只读）
# ---------------------------------------------------------------------------

def desktop_auth_path() -> pathlib.Path | None:
    """本机小浣熊桌面客户端的 ``auth.json`` 路径；找不到返回 None。

    新版在客户端配置目录下的 ``agent/config/auth.json``，旧版在
    ``~/.box-agent/config/auth.json``。**只读**：不写、不动它的 refresh token。
    """
    candidates: list[pathlib.Path] = []
    home = pathlib.Path.home()
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or ""
        if appdata:
            candidates.append(pathlib.Path(appdata) / "office-raccoon"
                              / "agent" / "config" / "auth.json")
    elif os.uname().sysname == "Darwin":
        candidates.append(home / "Library" / "Application Support"
                          / "office-raccoon" / "agent" / "config" / "auth.json")
    else:
        candidates.append(home / ".config" / "office-raccoon"
                          / "agent" / "config" / "auth.json")
    candidates.append(home / ".box-agent" / "config" / "auth.json")
    for path in candidates:
        if path.is_file():
            return path
    return None


def read_desktop_auth() -> dict[str, Any] | None:
    """读桌面客户端的登录态（只读）；没有/解析失败返回 None。"""
    path = desktop_auth_path()
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("raccoon 桌面登录态读取失败（%s）: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None
