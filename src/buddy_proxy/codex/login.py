"""Codex（ChatGPT）交互登录 + ``~/.codex/auth.json`` 导入。

两条入口：

- **浏览器回环**：``buddy login codex`` → :func:`login_interactive`。起本地
  ``http://localhost:1455/auth/callback`` 回调服务，打印授权 URL 并尝试打开
  浏览器，收到 code 后换 token 落盘。回调端口被占用时自动退化为「打印 URL +
  粘贴回调 URL」。
- **导入现成登录态**：本机 Codex CLI 的 ``~/.codex/auth.json``（含
  ``tokens.access_token / refresh_token / id_token / account_id``）直接贴进
  管理面板或调 :func:`import_cred_payload`。

两路都先落盘再做补充信息；补充失败不白费授权（与 kimi / antigravity 同思路）。
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
from typing import Any

from buddy_proxy.core.browser import open_external_url

from . import oauth
from .callback_server import (
    CallbackResult,
    start_callback_server,
    stop_callback_server,
)
from .credentials import (
    AccountRef,
    _expiry_iso,
    account_cred_path,
    list_accounts,
    save_account_cred,
)

log = logging.getLogger(__name__)

#: 整个登录流程的预算（浏览器操作 + 等待回调），与其它通道同级。
LOGIN_TIMEOUT_S = 600.0

#: Codex CLI 的凭据文件（导入来源）。
CODEX_CLI_AUTH_PATH = "~/.codex/auth.json"


class LoginError(RuntimeError):
    """登录/导入失败。"""


def _apply_claims(cred: dict[str, Any]) -> None:
    """从 id_token 回填邮箱/套餐/account_id（解不出就跳过）。"""
    claims = oauth.decode_id_token_claims(str(cred.get("id_token") or ""))
    if not claims:
        return
    account_id = oauth.account_id_from_claims(claims)
    if account_id and not cred.get("chatgpt_account_id"):
        cred["chatgpt_account_id"] = account_id
    email = oauth.email_from_claims(claims)
    if email:
        cred["email"] = email
    user_id = oauth.user_id_from_claims(claims)
    if user_id:
        cred["user_id"] = user_id
    cred["plan_type"] = oauth.plan_type_from_claims(claims)


def cred_from_token_response(payload: oauth.TokenResponse) -> dict[str, Any]:
    """TokenResponse → 可落盘的 cred。"""
    cred: dict[str, Any] = {
        "type": "codex",
        "access_token": payload.access_token,
        "refresh_token": payload.refresh_token,
        "id_token": payload.id_token,
        "expired": _expiry_iso(payload.expires_in),
        "last_refresh": int(time.time()),
        "token_type": payload.token_type or "Bearer",
        "plan_type": "free",
    }
    if payload.scope:
        cred["scope"] = payload.scope
    _apply_claims(cred)
    return cred


def cred_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """``~/.codex/auth.json``（或同构 JSON）→ buddy cred。

    兼容两种形态：

    - Codex CLI 原生：``{"tokens": {"access_token", "refresh_token",
      "id_token", "account_id"}, "last_refresh": "..."}``
    - 扁平（面板手工粘贴 / 其它工具导出）：``{"access_token": ...}`` 直接在顶层

    必填 ``access_token`` 与 ``refresh_token``。``expired`` 缺失时留空，
    首次使用会自动刷新（refresh_token 才是长期凭据）。
    """
    if not isinstance(payload, dict):
        raise LoginError("导入内容不是 JSON 对象")

    tokens = payload.get("tokens")
    source = tokens if isinstance(tokens, dict) else payload

    access_token = str(source.get("access_token") or "").strip()
    refresh_token = str(source.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise LoginError("导入 JSON 缺 access_token 或 refresh_token（两者必填）")
    if payload.get("disabled") is True:
        raise LoginError("该账号在导出文件里标记了 disabled: true，拒绝导入")

    cred: dict[str, Any] = {
        "type": "codex",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": str(source.get("id_token") or "").strip(),
        "expired": str(source.get("expired") or source.get("expiry")
                       or source.get("expires_at") or "").strip(),
        "last_refresh": int(time.time()),
        "token_type": str(source.get("token_type") or "Bearer"),
        "plan_type": str(source.get("plan_type") or "free"),
    }
    account_id = str(source.get("account_id") or source.get("chatgpt_account_id") or "").strip()
    if account_id:
        cred["chatgpt_account_id"] = account_id
    email = str(source.get("email") or "").strip()
    if email:
        cred["email"] = email
    user_id = str(source.get("user_id") or "").strip()
    if user_id:
        cred["user_id"] = user_id
    if source.get("scope"):
        cred["scope"] = str(source["scope"])
    _apply_claims(cred)  # id_token 里的信息比导出文件的更权威
    return cred


def import_cred_payload(raw: str | dict[str, Any] | bytes) -> dict[str, Any]:
    """导入一份凭据（面板「导入账号」与 CLI 共用），返回落盘的 cred。"""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise LoginError("导入内容为空")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LoginError(f"导入内容不是合法 JSON: {exc}") from exc
    else:
        payload = raw
    cred = cred_from_payload(payload)
    ref = save_account_cred(cred)
    cred["account_id"] = ref.id
    return cred


def _read_codex_cli_auth() -> dict[str, Any] | None:
    """读本机 ``~/.codex/auth.json``；没有/损坏返回 None。"""
    path = pathlib.Path(CODEX_CLI_AUTH_PATH).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _prompt_manual_callback() -> tuple[str, str]:
    """让用户粘贴回调 URL（回调服务起不来时的退路）。"""
    print()
    print("[Codex] 无法启动本地回调服务（端口 1455 可能被占用）。")
    print("[Codex] 完成授权后浏览器会跳到 http://localhost:1455/auth/callback?code=...")
    print("        该页面打不开是正常的，请复制地址栏里的**完整 URL** 粘到下面。")
    try:
        raw = input("[Codex] 粘贴回调 URL（或直接粘 code）: ")
    except EOFError:  # 非交互（管道/CI/SSH 无 stdin）：明确失败而不是抛栈
        raise LoginError("标准输入不可用，无法粘贴回调 URL；请在终端里交互式运行") from None
    return oauth.parse_callback_input(raw)


def login_interactive(
    open_browser: bool = True, *, timeout: float = LOGIN_TIMEOUT_S,
) -> dict[str, Any]:
    """完整 OAuth 回环登录，返回落盘的凭据 dict。

    ``timeout`` 是等待回调整体的预算（测试可传小值）。
    """
    pkce = oauth.generate_pkce()
    state = oauth.generate_state()
    url = oauth.build_authorize_url(pkce, state)

    print()
    print("[Codex] 请在浏览器中打开下面的链接，用 ChatGPT 账号完成授权：")
    print()
    print(f"    {url}")
    print()
    # URL 必须**始终**回显：open_external_url 失败不抛只返回 False
    if open_browser:
        if open_external_url(url):
            print("[Codex] 已尝试自动打开浏览器…")
        else:
            print("[Codex] 自动打开浏览器失败，请手动复制上面的链接。")

    result = CallbackResult()
    server = start_callback_server(result)
    code = ""
    got_state = ""
    try:
        if server is not None:
            print(f"[Codex] 回调监听: http://localhost:1455/auth/callback"
                  f"（最多等待 {int(timeout)} 秒，Ctrl-C 可取消）…")
            if not result.wait(timeout):
                raise LoginError(f"等待授权超时（{int(timeout)} 秒内未收到回调）")
            if result.error:
                raise LoginError(f"授权失败: {result.error}")
            code, got_state = result.code, result.state
        else:
            code, got_state = _prompt_manual_callback()
    except KeyboardInterrupt:
        print("\n[Codex] 已取消。")
        raise SystemExit(1) from None
    finally:
        stop_callback_server(server)

    if got_state and got_state != state:
        # state 对不上说明回调来自别的授权请求：不拦，但明确告知
        print("[!] 回调的 state 与本次请求不一致，继续用收到的 code 换 token。")

    try:
        tokens = oauth.exchange_code(code, pkce)
    except oauth.OAuthError as exc:
        raise LoginError(f"换取 token 失败: {exc}") from exc

    cred = cred_from_token_response(tokens)
    ref = save_account_cred(cred)  # 先落盘：后续补充失败不白费授权
    _print_ready(cred, ref)
    return cred


def _print_ready(cred: dict[str, Any], ref: AccountRef) -> None:
    print()
    who = str(cred.get("email") or "") or ref.id
    plan = str(cred.get("plan_type") or "")
    print(f"[OK] Codex 登录成功: {who}" + (f"（{plan} 套餐）" if plan else ""))
    print(f"     凭据文件: {account_cred_path(ref.id)}")
    accounts = list_accounts()
    if len(accounts) > 1:
        mine = next((i for i, a in enumerate(accounts) if a.id == ref.id), None)
        if mine is not None:
            print(f"     账号顺位: #{mine + 1}（共 {len(accounts)} 个账号，额度耗尽自动切换下一个）")
    print("     启动代理时加 --codex（或 CODEX_ENABLED=1）即可启用该通道。")
    print("     若网关正在运行，需 `buddy restart` 才会加载新凭据。")
