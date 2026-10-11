"""Gemini CLI OAuth 交互式登录：PKCE + 本地回调服务器。

与 gemini CLI 的 ``authWithWeb``（code_assist/oauth2.js:346）同构：

1. 随机端口起 ``http://127.0.0.1:<port>/oauth2callback``；
2. 构造授权 URL（``access_type=offline`` + ``prompt=consent`` + PKCE S256；
   gemini CLI 用 google-auth-library 默认也开了 PKCE）；
3. 打开浏览器 → 用户选择 Google 账号授权 → 回调带 ``code``/``state``；
4. 校验 state → code 换 token（带 code_verifier）→ 拉 email → onboarding
   （loadCodeAssist/onboardUser，拿托管项目 ID）→ 落盘。

桌面浏览器场景回调必然能到达本机（127.0.0.1），不需要 Trae 那种手动
粘贴兜底；远程 SSH 场景用户可用 ``--no-browser`` 拿链接在自己电脑打开后，
把回调完整 URL 粘回终端（保留与 qoder 一致的手动通道）。
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
    fetch_user_email,
    save_cred,
)
from .setup import SetupError, setup_code_assist

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
#: 登录成功/失败后浏览器跳的落地页（与 gemini CLI 相同的两个地址）。
SIGN_IN_SUCCESS_URL = "https://developers.google.com/gemini-code-assist/auth_success_gemini"
SIGN_IN_FAILURE_URL = "https://developers.google.com/gemini-code-assist/auth_failure_gemini"

#: 整个登录流程的预算（浏览器操作 + onboarding），qoder/trae 同级。
LOGIN_TIMEOUT_S = 300.0


class LoginError(RuntimeError):
    """登录流程失败。"""


@dataclass
class _Callback:
    code: str = ""
    state: str = ""
    error: str = ""


def _pkce_pair() -> tuple[str, str]:
    """(verifier, challenge)；S256，与 gemini CLI（43 字节随机）同级强度。"""
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
    server_ready = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/oauth2callback":
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
            land = SIGN_IN_SUCCESS_URL if (cb.code and not cb.error) else SIGN_IN_FAILURE_URL
            self.send_response(302)
            self.send_header("Location", land)
            self.end_headers()

        def log_message(self, *args):  # noqa: N802 - 静默默认日志
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/oauth2callback"
    auth_url = _build_auth_url(redirect_uri, state, challenge)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server_ready.set()
    try:
        print()
        print("[Gemini] 请在浏览器中打开下面的链接，用 Google 账号完成授权：")
        print()
        print(f"    {auth_url}")
        print()
        if open_browser:
            # webbrowser 静默失败（返回 False）由 open_external_url 兜底
            if open_external_url(auth_url):
                print("[Gemini] 已尝试自动打开浏览器…")
            else:
                print("[Gemini] 自动打开浏览器失败，请手动复制上面的链接。")
        if not open_browser:
            print(f"[Gemini] 等待授权回调（127.0.0.1:{port}）…")
            print(
                "[Gemini] 若浏览器无法回跳本机（远程 SSH 等），完成授权后把"
                "地址栏完整 URL 粘贴回这里（直接回车继续等回调）："
            )
            # 简单做法：起一个输入线程，粘贴/回调谁先到用谁。
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
            print(f"[Gemini] 等待授权回调（127.0.0.1:{port}，最长 "
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
        print(f"[Gemini] 登录成功: {cred.get('email') or '(未知邮箱)'}")
        print(f"     项目: {cred.get('project_id')}  tier: {cred.get('tier')}")
        print(f"     凭据文件: {cred_path_display()}")
        print("     启动代理时加 --gemini（或 GEMINI_ENABLED=1）即可启用该通道。")
        print("     若网关正在运行，需 `buddy restart` 才会加载新凭据。")
        return cred
    finally:
        server.shutdown()
        server.server_close()


def cred_path_display() -> str:
    from .credentials import cred_path

    return str(cred_path())


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

    # 先把 token 落盘再做 onboarding：onboarding 失败（如地区不符）时
    # 授权不该白费——重试只需再跑 onboarding，不用重新点浏览器授权。
    save_cred(cred)

    try:
        info = setup_code_assist(cred["access_token"])
    except SetupError as exc:
        # onboarding 卡住也先把 CLI 喂饱：用户可直接跑 `gemini` 验证账号本身
        _sync_cli_best_effort(cred)
        raise LoginError(
            f"Code Assist onboarding 失败: {exc}\n"
            f"    token 已保存（{cred_path_display()}），并已同步到本机 Gemini CLI——"
            f"可直接运行 `gemini` 验证账号本身是否可用；修好上面问题后重跑 "
            f"`buddy login gemini` 会自动续跑（不会重新打开浏览器）；"
            f"要换账号请先删除该文件。"
        ) from exc
    cred["project_id"] = info["project_id"]
    cred["tier"] = info["tier"]
    cred["tier_name"] = info["tier_name"]

    save_cred(cred)
    _sync_cli_best_effort(cred)
    return cred


def _sync_cli_best_effort(cred: dict) -> None:
    """把凭证同步给本机 Gemini CLI（写 ~/.gemini）；失败不影响我们的登录。"""
    from .cli_bridge import sync_to_cli

    try:
        notes = sync_to_cli(cred)
    except Exception as exc:  # noqa: BLE001 - 互通是锦上添花，任何异常都不能拦登录
        print(f"[Gemini] 同步到本机 Gemini CLI 失败: {exc}")
        return
    for note in notes:
        print(f"[Gemini] {note}")


def adopt_cli_login() -> dict:
    """直接采用本机 Gemini CLI 的登录态（不重开浏览器）。

    与交互登录共用后半段（token 先落盘、onboarding 带诊断、最后回写
    CLI），区别只在 token 来源是 ``~/.gemini/oauth_creds.json`` 而不是
    浏览器授权。access token 过期时用同一 client 直接刷新。
    """
    from .cli_bridge import cli_cached_email, cli_creds_path, cli_creds_usable, load_cli_creds, to_buddy_format
    from .credentials import access_token_valid, refresh_cred

    cli = load_cli_creds()
    if cli is None:
        raise LoginError(f"未找到 Gemini CLI 登录态（{cli_creds_path()} 不存在或为空）")
    usable, note = cli_creds_usable(cli)
    if not usable:
        raise LoginError(f"Gemini CLI 登录态不可用（{note}），请重新登录 CLI 或改走浏览器授权")

    cred = to_buddy_format(cli)
    if not cred["refresh_token"] and not access_token_valid(cred):
        raise LoginError("CLI 登录态既无 refresh_token 也已过期，无法采用")
    if cred["refresh_token"] and not access_token_valid(cred):
        try:
            refresh_cred(cred)
        except AuthError as exc:
            raise LoginError(f"CLI 的 refresh_token 已失效（{exc}），请重新登录") from exc

    email = cli_cached_email() or fetch_user_email(cred["access_token"])
    if email:
        cred["email"] = email

    # 与 _exchange 同序：token 先落盘，onboarding 失败授权不白费
    save_cred(cred)
    try:
        info = setup_code_assist(cred["access_token"])
    except SetupError as exc:
        _sync_cli_best_effort(cred)
        raise LoginError(
            f"Code Assist onboarding 失败: {exc}\n"
            f"    token 已保存（{cred_path_display()}），修好上面问题后重跑 "
            f"`buddy login gemini` 会自动续跑。"
        ) from exc
    cred["project_id"] = info["project_id"]
    cred["tier"] = info["tier"]
    cred["tier_name"] = info["tier_name"]
    save_cred(cred)
    _sync_cli_best_effort(cred)
    return cred


def resume_onboarding() -> dict:
    """免浏览器重试 onboarding：用已保存的 token 再走一遍 setup。

    针对「OAuth 已成功、onboarding 失败」的中间态（如资格问题解决后重试、
    或首次失败后上游状态已就绪）。token 失效会自动刷新。
    """
    from .credentials import AuthError, ensure_access_token, load_cred

    cred = load_cred()
    if cred is None:
        raise LoginError("没有已保存的登录态，请先运行 `buddy login gemini`")
    try:
        token = ensure_access_token(cred)
    except AuthError as exc:
        raise LoginError(f"刷新 token 失败（可能需要重新登录）: {exc}") from exc
    try:
        info = setup_code_assist(token)
    except SetupError as exc:
        raise LoginError(f"Code Assist onboarding 仍失败: {exc}") from exc
    cred["project_id"] = info["project_id"]
    cred["tier"] = info["tier"]
    cred["tier_name"] = info["tier_name"]
    cred["access_token"] = token
    save_cred(cred)
    _sync_cli_best_effort(cred)
    return cred
