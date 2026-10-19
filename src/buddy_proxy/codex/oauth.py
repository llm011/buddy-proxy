"""OpenAI Codex (ChatGPT) OAuth 2.0 + PKCE 协议层。

纯 HTTP 协议，不碰磁盘、不管多账号——落盘见 :mod:`buddy_proxy.codex.credentials`，
交互流程见 :mod:`buddy_proxy.codex.login`。

端点与参数取自 Codex CLI 官方实现（``codex-rs/login``），CLIProxyAPI 的
``internal/auth/codex/openai_auth.go`` 用的是同一套：

- authorize ``https://auth.openai.com/oauth/authorize``
- token     ``https://auth.openai.com/oauth/token``
- client_id ``app_EMoamEEZ73f0CkXaXp7hrann``（Codex CLI 公开 client，非机密）
- redirect  ``http://localhost:1455/auth/callback``（**必须逐字一致**，
  OpenAI 侧对 redirect_uri 做白名单校验，端口换了会直接拒绝）

PKCE 用 S256：verifier 是 96 字节随机数的 base64url（无 padding）——比 RFC 7636
下界 43 字符宽松得多，照抄官方实现（CLIProxyAPI ``pkce.go`` 同款）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"

#: 授权请求的 scope。``offline_access`` 是拿到 refresh_token 的前提。
SCOPE = "openid email profile offline_access"

#: 授权 URL 上的固定附加参数，缺一个都可能换不到 code：
#: - ``prompt=login`` 强制走登录页（否则已登录会话直接弹授权，多账号切换不了）
#: - ``id_token_add_organizations`` 让 ID token 带上 org/account 声明
#: - ``codex_cli_simplified_flow`` 是 Codex CLI 专用简化流程开关
#: 授权页的额外参数（与 Codex CLI 一致）。``prompt`` 不在这里——它由
#: :func:`build_authorize_url` 的 ``force_reauth`` 控制（重复出现会打架）。
_EXTRA_AUTH_PARAMS = {
    "id_token_add_organizations": "true",
    "codex_cli_simplified_flow": "true",
}


class OAuthError(RuntimeError):
    """授权/换 token/刷新失败。"""


@dataclass(frozen=True)
class PKCECodes:
    """一次授权流程用的 PKCE 对。"""

    verifier: str
    challenge: str


@dataclass(frozen=True)
class TokenResponse:
    """``/oauth/token`` 的响应（字段按官方返回命名）。"""

    access_token: str
    refresh_token: str
    id_token: str
    expires_in: float
    token_type: str = "Bearer"
    scope: str = ""

    def expires_at(self) -> float:
        """绝对过期时刻（epoch 秒）。"""
        return time.time() + self.expires_in


def generate_pkce() -> PKCECodes:
    """生成 PKCE 对（S256）。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(96)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return PKCECodes(verifier=verifier, challenge=challenge)


def generate_state() -> str:
    """防 CSRF 的 state 随机串。"""
    return secrets.token_urlsafe(24)


def build_authorize_url(
    pkce: PKCECodes, state: str, *, force_reauth: bool = True,
) -> str:
    """拼授权 URL（浏览器打开的就是这个）。

    ``force_reauth``：追加 ``prompt=login``（与 Codex CLI 每次登录都强制重新
    输密码一致——否则浏览器里已有会话会被静默复用，想「换个账号登」根本换不掉）。
    **用 dict 合并而不是拼 query**：URL 里本来可能已有 ``prompt=consent``，
    再拼一个就成了重复参数，服务端取哪个不确定。
    """
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
    }
    params.update(_EXTRA_AUTH_PARAMS)
    if force_reauth:
        params["prompt"] = "login"
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def parse_callback_input(raw: str) -> tuple[str, str]:
    """从用户粘贴的内容里抠出 ``(code, state)``。

    用户粘贴的形态不固定，三种都要认：

    - 完整回调 URL ``http://localhost:1455/auth/callback?code=...&state=...``
    - 只有 query ``code=...&state=...``
    - 裸 code 串

    裸 code 时 state 返回空串（此时不做 state 校验，见 login 的说明）。
    """
    text = (raw or "").strip()
    if not text:
        raise OAuthError("粘贴内容为空")
    # 只要看着像回调（带 ``?``、带 ``=`` 参数、或是 http(s) URL），就按参数表解析。
    # 只认 "code=" 出现与否会把 ``?state=xyz``、``error=access_denied`` 这类
    # 失败回调当裸 code 送上去换 token，白跑一轮网络请求且报错不知所云。
    looks_like_callback = ("=" in text) or ("://" in text)
    if looks_like_callback:
        query = text.split("?", 1)[1] if "?" in text else text
        # 用户可能把整行控制台输出一起粘进来，截断到空白前
        query = query.split()[0]
        parsed = urllib.parse.parse_qs(query)
        code = (parsed.get("code") or [""])[0]
        state = (parsed.get("state") or [""])[0]
        if not code:
            err = (parsed.get("error") or [""])[0]
            desc = (parsed.get("error_description") or [""])[0]
            if err:
                raise OAuthError(f"授权失败: {err}{(': ' + desc) if desc else ''}")
            raise OAuthError("回调 URL 里没有 code 参数")
        return code, state
    return text, ""


def _post_form(data: dict[str, str], *, timeout: float) -> dict[str, Any]:
    """向 token 端点发 form 请求，返回 JSON。

    OpenAI 的错误响应也是 JSON（``{"error": "invalid_grant", "error_description": ...}``），
    非 2xx 时优先把它拼进异常，比裸 HTTP 状态码可诊断得多。
    """
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
            parsed = json.loads(raw)
            detail = (parsed.get("error_description") or parsed.get("error")
                      or parsed.get("message") or raw)
        except Exception:  # noqa: BLE001 - 解析失败就退回空串
            detail = ""
        raise OAuthError(
            f"HTTP {exc.code}{(': ' + str(detail)) if detail else ''}") from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"网络错误: {exc.reason}") from exc
    except TimeoutError as exc:
        raise OAuthError("请求超时") from exc

    try:
        parsed = json.loads(payload.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise OAuthError(f"响应不是合法 JSON: {payload[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise OAuthError(f"响应不是 JSON 对象: {payload[:200]!r}")
    return parsed


def _to_token_response(parsed: dict[str, Any]) -> TokenResponse:
    access = str(parsed.get("access_token") or "")
    if not access:
        raise OAuthError(f"响应缺 access_token: {parsed}")
    try:
        expires_in = float(parsed.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0.0
    return TokenResponse(
        access_token=access,
        refresh_token=str(parsed.get("refresh_token") or ""),
        id_token=str(parsed.get("id_token") or ""),
        expires_in=expires_in,
        token_type=str(parsed.get("token_type") or "Bearer"),
        scope=str(parsed.get("scope") or ""),
    )


def exchange_code(code: str, pkce: PKCECodes, *, timeout: float = 30.0) -> TokenResponse:
    """用授权码换 token。"""
    if not code.strip():
        raise OAuthError("授权码为空")
    parsed = _post_form({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code.strip(),
        "redirect_uri": REDIRECT_URI,
        "code_verifier": pkce.verifier,
    }, timeout=timeout)
    return _to_token_response(parsed)


def refresh(refresh_token: str, *, timeout: float = 30.0) -> TokenResponse:
    """用 refresh_token 换新 access_token。

    OpenAI 会**轮换** refresh_token：新值必须回写，否则旧值作废、下次刷新必失败。
    """
    if not (refresh_token or "").strip():
        raise OAuthError("refresh_token 为空")
    parsed = _post_form({
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token.strip(),
        "scope": "openid profile email",
    }, timeout=timeout)
    return _to_token_response(parsed)


# ---------------------------------------------------------------------------
# ID token 解析
# ---------------------------------------------------------------------------

def decode_id_token_claims(id_token: str) -> dict[str, Any]:
    """解出 ID token 的 payload（**不验签**）。

    只用来读展示信息（邮箱、套餐、account_id），信任边界在 TLS + 我们自己
    走 OAuth 拿到的那份 token，不做额外校验（与 Codex CLI / CLIProxyAPI 一致）。
    """
    token = (id_token or "").strip()
    if not token:
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - 解不开就当作没有
        return {}
    return claims if isinstance(claims, dict) else {}


def _nested(claims: dict[str, Any], key: str) -> Any:
    """先看顶层，再看 ``https://api.openai.com/auth`` 命名空间（官方两种都可能出现）。"""
    if key in claims:
        return claims[key]
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict) and key in auth:
        return auth[key]
    return None


def account_id_from_claims(claims: dict[str, Any]) -> str:
    """ChatGPT account id（请求头 ``Chatgpt-Account-Id`` 用）。"""
    for key in ("chatgpt_account_id", "chatgpt_account_id".replace("_", "")):
        value = _nested(claims, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        org = auth.get("organization_id")
        if isinstance(org, str) and org.strip():
            return org.strip()
    # 退回 token 里的 sub（Codex CLI 在缺 chatgpt_account_id 时也这么干）
    sub = claims.get("sub")
    return sub.strip() if isinstance(sub, str) else ""


def email_from_claims(claims: dict[str, Any]) -> str:
    for key in ("email", "preferred_username"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def plan_type_from_claims(claims: dict[str, Any]) -> str:
    value = _nested(claims, "chatgpt_plan_type")
    return value.strip() if isinstance(value, str) and value.strip() else "free"


def user_id_from_claims(claims: dict[str, Any]) -> str:
    value = _nested(claims, "chatgpt_user_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    sub = claims.get("sub")
    return sub.strip() if isinstance(sub, str) else ""
