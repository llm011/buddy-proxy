"""Antigravity OAuth 交互式登录：PKCE + 本地回调服务器。

与 gemini 通道的登录同构（同为 Google 安装型应用 + loopback 回调 +
``access_type=offline``/``prompt=consent`` + PKCE S256），差异只在参数：

- client 是 Antigravity 的 ``1071006060591-…``，scopes 多 cclog/experimentsandconfigs；
- 回调路径 ``/oauth-callback``（agy 同款；gemini 是 /oauth2callback）；
- 回调页直接渲染成功文案（agy 的「Authentication successful! You can close
  this window.」同款语义），不跳转外部落地页。

互通与 gemini 方向相反：agy 的登录态在系统 keyring（没有明文文件可回写），
所以只做**读取**（:func:`adopt_cli_login`，keyring 条目见 cli_bridge.py），
不做写回。
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass

from buddy_proxy.core.browser import open_external_url
from http.server import BaseHTTPRequestHandler, HTTPServer

from .credentials import (
    CLIENT_ID,
    CLIENT_SECRET,
    SCOPES,
    AuthError,
    _expiry_from,
    _token_request,
    account_cred_path,
    fetch_user_email,
    list_accounts,
    save_account_cred,
)
from .setup import SetupError, setup_code_assist

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"

#: 整个登录流程的预算（浏览器操作 + onboarding），qoder/trae/gemini 同级。
LOGIN_TIMEOUT_S = 300.0

_CALLBACK_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Antigravity</title></head><body style="font-family:sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh">
<p>Authentication successful! You can close this window.</p></body></html>"""


class LoginError(RuntimeError):
    """登录流程失败。"""


@dataclass
class _Callback:
    code: str = ""
    state: str = ""
    error: str = ""


def _pkce_pair() -> tuple[str, str]:
    """(verifier, challenge)；S256，与 gemini 通道同级强度。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _build_auth_url(redirect_uri: str, state: str, challenge: str) -> str:
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def login_interactive(open_browser: bool = True) -> dict:
    """完整登录流程，返回落盘的凭据 dict。"""
    verifier, challenge = _pkce_pair()
    state = secrets.token_hex(16)

    received: list[_Callback] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/oauth-callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            cb = _Callback(
                code=(qs.get("code") or [""])[0],
                state=(qs.get("state") or [""])[0],
                error=(qs.get("error") or qs.get("error_description") or [""])[0],
            )
            received.append(cb)
            ok = bool(cb.code) and not cb.error
            body = _CALLBACK_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: N802 - 静默默认日志
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/oauth-callback"
    auth_url = _build_auth_url(redirect_uri, state, challenge)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        print()
        print("[Antigravity] 请在浏览器中打开下面的链接，用 Google 账号完成授权：")
        print()
        print(f"    {auth_url}")
        print()
        if open_browser:
            # webbrowser 静默失败（返回 False）由 open_external_url 兜底
            if open_external_url(auth_url):
                print("[Antigravity] 已尝试自动打开浏览器…")
            else:
                print("[Antigravity] 自动打开浏览器失败，请手动复制上面的链接。")
        if not open_browser:
            print(f"[Antigravity] 等待授权回调（127.0.0.1:{port}）…")
            print(
                "[Antigravity] 若浏览器无法回跳本机（远程 SSH 等），完成授权后把"
                "地址栏完整 URL 粘贴回这里（直接回车继续等回调）："
            )
            import sys as _sys

            def _read_input():
                try:
                    pasted = _sys.stdin.readline().strip()
                except Exception:  # noqa: BLE001
                    return
                if pasted:
                    parsed = urllib.parse.urlparse(
                        pasted if "://" in pasted else "http://x" + pasted
                    )
                    qs = urllib.parse.parse_qs(parsed.query)
                    received.append(_Callback(
                        code=(qs.get("code") or [""])[0],
                        state=(qs.get("state") or [""])[0],
                        error=(qs.get("error") or qs.get("error_description") or [""])[0],
                    ))

            threading.Thread(target=_read_input, daemon=True).start()
        else:
            print(f"[Antigravity] 等待授权回调（127.0.0.1:{port}，最长 "
                  f"{int(LOGIN_TIMEOUT_S)} 秒，Ctrl-C 取消）…")

        deadline = time.monotonic() + LOGIN_TIMEOUT_S
        while not received:
            if time.monotonic() > deadline:
                raise LoginError("登录超时：未收到 Google 回调")
            time.sleep(0.3)
        cb = received[0]
        if cb.error:
            raise LoginError(f"Google 授权失败: {cb.error}")
        if cb.state != state:
            raise LoginError("OAuth state 校验失败（CSRF 或回调错乱），请重试")
        if not cb.code:
            raise LoginError("回调缺少 code")

        cred = _exchange(cb.code, redirect_uri, verifier)
        print(f"[Antigravity] 登录成功: {cred.get('email') or '(未知邮箱)'}")
        print(f"     项目: {cred.get('project_id')}  tier: {cred.get('tier')}")
        print(f"     凭据文件: {account_cred_path(str(cred.get('account_id') or ''))}")
        accounts = list_accounts()
        if len(accounts) > 1:
            mine = next((i for i, a in enumerate(accounts) if a.id == cred.get("account_id")), None)
            if mine is not None:
                print(f"     账号顺位: #{mine + 1}（共 {len(accounts)} 个账号，额度耗尽自动切换下一个）")
        print("     启动代理时加 --antigravity（或 ANTIGRAVITY_ENABLED=1）即可启用该通道。")
        print("     若网关正在运行，需 `buddy restart` 才会加载新凭据。")
        return cred
    finally:
        server.shutdown()
        server.server_close()


def resume_onboarding(account_id: str | None = None) -> dict:
    """免浏览器重试 onboarding：用已保存的 token 再走一遍 setup。

    针对「OAuth 已成功、onboarding 失败」的中间态。token 失效会自动刷新。
    多账号下 ``account_id`` 缺省时定位「有 token 缺 project」的账号（按
    failover 顺位取第一个）——完整登录中断后重跑自动续上那个账号。
    """
    from .credentials import ensure_account_token, list_accounts, load_account_cred

    if account_id:
        if load_account_cred(account_id) is None:
            raise LoginError(f"账号 {account_id} 不存在")
    else:
        account_id = next(
            (a.id for a in list_accounts()
             if not (load_account_cred(a.id) or {}).get("project_id")),
            None)
        if account_id is None:
            raise LoginError(
                "没有待续跑的账号（已登录账号都完成了 onboarding）；"
                "换号/新增账号请直接 `buddy login antigravity`")
    try:
        token, cred = ensure_account_token(account_id)
    except AuthError as exc:
        raise LoginError(f"刷新 token 失败（可能需要重新登录）: {exc}") from exc
    try:
        info = setup_code_assist(token)
    except SetupError as exc:
        raise LoginError(f"Antigravity onboarding 仍失败: {exc}") from exc
    cred["project_id"] = info["project_id"]
    cred["tier"] = info["tier"]
    cred["tier_name"] = info["tier_name"]
    save_account_cred(cred)
    return cred


def adopt_cli_login() -> dict:
    """直接采用本机 agy（Antigravity CLI）的 keyring 登录态（不重开浏览器）。

    与交互登录共用后半段（token 先落盘、onboarding 失败不白费授权），
    区别只在 token 来源是系统 keyring 里 agy 的条目（见 cli_bridge.py）。
    access token 过期时用同一 client 直接刷新。
    """
    from .cli_bridge import cli_cached_email, cli_creds_usable, cli_token, load_cli_creds, to_buddy_format
    from .credentials import access_token_valid, refresh_account_cred

    payload = load_cli_creds()
    if payload is None:
        raise LoginError(
            "未找到 Antigravity CLI 的登录态（keyring 里没有可用条目，"
            "或当前平台不支持读取）。请先运行 `agy` 登录，或改走浏览器授权"
        )
    usable, note = cli_creds_usable(payload)
    if not usable:
        raise LoginError(f"Antigravity CLI 登录态不可用（{note}）")

    cred = to_buddy_format(cli_token(payload))
    if cred["refresh_token"] and not access_token_valid(cred):
        try:
            refresh_account_cred(cred)
        except AuthError as exc:
            raise LoginError(f"agy 的 refresh_token 已失效（{exc}），请在 agy 里重新登录") from exc

    email = cli_cached_email(payload) or fetch_user_email(cred["access_token"])
    if email:
        cred["email"] = email

    # 与 _exchange 同序：token 先落盘，onboarding 失败授权不白费
    save_account_cred(cred)
    try:
        info = setup_code_assist(cred["access_token"])
    except SetupError as exc:
        raise LoginError(
            f"Antigravity onboarding 失败: {exc}\n"
            f"    token 已保存（{account_cred_path(str(cred.get('account_id') or ''))}），"
            f"修好上面问题后重跑 `buddy login antigravity` 会自动续跑。"
        ) from exc
    cred["project_id"] = info["project_id"]
    cred["tier"] = info["tier"]
    cred["tier_name"] = info["tier_name"]
    save_account_cred(cred)
    return cred


def _exchange(code: str, redirect_uri: str, verifier: str) -> dict:
    """code 换 token → email → onboarding → 落盘。"""
    payload = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code_verifier": verifier,
        }
    )
    cred = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token") or "",
        "expiry": _expiry_from(payload.get("expires_in")),
        "token_type": payload.get("token_type") or "Bearer",
        "scope": payload.get("scope") or "",
    }
    if not cred["refresh_token"]:
        raise LoginError("Google 未返回 refresh_token（授权时缺 offline consent），请重试")

    email = fetch_user_email(cred["access_token"])
    if email:
        cred["email"] = email

    # 先把 token 落盘再做 onboarding：onboarding 失败时授权不白费——
    # 重试只需再跑 onboarding，不用重新点浏览器授权。
    # （save_account_cred 是幂等 upsert：同邮箱更新原账号，新邮箱追加。）
    save_account_cred(cred)

    try:
        info = setup_code_assist(cred["access_token"])
    except SetupError as exc:
        raise LoginError(
            f"Antigravity onboarding 失败: {exc}\n"
            f"    token 已保存（{account_cred_path(str(cred.get('account_id') or ''))}），"
            f"修好上面问题后重跑 `buddy login antigravity` 会自动续跑"
            f"（不会重新打开浏览器）。"
        ) from exc
    cred["project_id"] = info["project_id"]
    cred["tier"] = info["tier"]
    cred["tier_name"] = info["tier_name"]

    save_account_cred(cred)
    return cred
