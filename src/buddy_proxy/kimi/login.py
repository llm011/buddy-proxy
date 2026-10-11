"""Kimi Code 交互登录（Device Flow）+ kimi cli 导出 JSON 导入。

两条入口：

- **浏览器授权**：``buddy login kimi`` → :func:`login_interactive`。打印
  user_code 并自动打开 ``verification_uri_complete``（链接里已带好授权码，
  浏览器确认即可），CLI 按上游给的 interval 轮询换 token。
- **导入现成登录态**：kimi cli 导出的 token JSON（``kimi-<ts>.json``，含
  access_token/refresh_token/base_url/device_id）直接贴进管理面板或调
  :func:`import_cred_payload`——同构字段近乎透传，见 :func:`cred_from_payload`。

两路都先落盘再做补充信息（/v1/me 的 user_id/nickname）：补充失败不白费
授权/导入（与 antigravity「token 先落盘、onboarding 失败不白费」同思路）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from buddy_proxy.core.browser import open_external_url

from . import oauth
from .credentials import (
    AccountRef,
    _expiry_iso,
    account_cred_path,
    list_accounts,
    new_device_id,
    refresh_account_cred,
    save_account_cred,
)
from .credentials import AuthError
from .upstream import device_headers, fetch_me

log = logging.getLogger(__name__)

#: 整个登录流程的预算（浏览器操作 + 轮询），qoder/antigravity 同级。
LOGIN_TIMEOUT_S = 600.0

#: base_url 缺省（mainland-cn）；global 区由 oauth_host 域名推导。
_DEFAULT_BASE_URL = "https://api.kimi.com/coding"
_GLOBAL_BASE_URL = "https://api.kimi.ai/coding"


class LoginError(RuntimeError):
    """登录/导入失败。"""


def _base_url_for_host(oauth_host: str) -> str:
    """OAuth host → 同区 managed API base（auth.kimi.ai ↔ api.kimi.ai/coding）。"""
    host = (oauth_host or "").rstrip("/")
    if "kimi.ai" in host:
        return _GLOBAL_BASE_URL
    return _DEFAULT_BASE_URL


def _derive_oauth_host(base_url: str) -> str:
    """导入 JSON 的 base_url → 同区 OAuth host（按账号存，混用不串）。"""
    url = (base_url or "").lower()
    if "kimi.ai" in url:
        return "https://auth.kimi.ai"
    return oauth.DEFAULT_OAUTH_HOST


def _default_base_url() -> str:
    import os

    return os.environ.get("KIMI_BASE_URL", "").strip().rstrip("/") or _DEFAULT_BASE_URL


def _default_oauth_host() -> str:
    import os

    return os.environ.get("KIMI_OAUTH_HOST", "").strip().rstrip("/") or oauth.DEFAULT_OAUTH_HOST


def cred_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """kimi cli 导出 JSON → buddy cred（必填校验 + 字段归一）。

    - ``expired`` 三键兼容（``expired`` / ``expiry`` / ``expires_at``），
      缺失时留空——首次使用会自动刷新（refresh_token 才是长期凭据）；
    - ``base_url`` 原样保留（kimi cli 形态，不带 /v1；调用时 normalize），
      缺失按 oauth_host 区推导；
    - ``device_id`` 缺失现场生成（每账号从此固定）；
    - ``disabled: true`` 拒绝导入（kimi cli 自己都标了禁用的号没有救的价值）；
    - ``type != "kimi"`` 只 warning 不拦（防御上游改字段，别把能用的号挡外面）。
    """
    if not isinstance(payload, dict):
        raise LoginError("导入内容不是 JSON 对象")
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise LoginError("导入 JSON 缺 access_token 或 refresh_token（两者必填）")
    if payload.get("disabled") is True:
        raise LoginError("该账号在导出文件里标记了 disabled: true，拒绝导入")

    raw_type = str(payload.get("type") or "")
    if raw_type and raw_type != "kimi":
        print(f"[!] 导入 JSON 的 type={raw_type!r}（预期 \"kimi\"），继续导入。")
    cred: dict[str, Any] = dict(payload)
    cred["type"] = "kimi"
    cred["access_token"] = access_token
    cred["refresh_token"] = refresh_token
    cred["expired"] = str(
        payload.get("expired") or payload.get("expiry") or payload.get("expires_at") or "")
    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        base_url = _base_url_for_host(str(payload.get("oauth_host") or ""))
    cred["base_url"] = base_url
    oauth_host = str(payload.get("oauth_host") or "").strip() or _derive_oauth_host(base_url)
    cred["oauth_host"] = oauth_host
    if not str(cred.get("device_id") or "").strip():
        cred["device_id"] = new_device_id()
    cred["last_refresh"] = int(time.time())
    return cred


def _enrich_cred(cred: dict[str, Any]) -> None:
    """尽力补 user_id / nickname（/v1/me；access token 失效就先刷新再试）。

    任何失败都只 debug 日志、不抛——导入/登录的价值在 refresh_token，
    展示字段拿不到不影响转发。
    """
    token = str(cred.get("access_token") or "")
    base_url = str(cred.get("base_url") or "")
    try:
        info = fetch_me(base_url, token)
    except OSError:
        try:
            refresh_account_cred(cred)
        except AuthError as exc:
            log.debug("kimi: 导入后补 /v1/me 失败（刷新也不行）: %s", exc)
            return
        try:
            info = fetch_me(base_url, str(cred["access_token"]))
        except OSError as exc:
            log.debug("kimi: 刷新后 /v1/me 仍失败: %s", exc)
            return
    if info.get("user_id"):
        cred["user_id"] = str(info["user_id"])
    if info.get("nickname"):
        cred["nickname"] = str(info["nickname"])
    if info.get("avatar"):
        cred["avatar"] = str(info["avatar"])
    if info.get("user_level_name"):
        cred["user_level_name"] = str(info["user_level_name"])


def import_cred_payload(raw: str | dict[str, Any]) -> dict[str, Any]:
    """导入一份 kimi cli 导出 JSON（str 或已解析 dict），落盘并返回 cred。

    同 refresh_token 更新原账号（顺位不变），新的追加为备用号。先落盘再补
    user_id/nickname：/v1/me 失败（token 过期又刷新不了）导入照样成立，
    转发路径会自己再试刷新。
    """
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            raise LoginError("导入内容为空")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LoginError(f"导入内容不是合法 JSON: {exc}") from exc
    else:
        payload = raw
    cred = cred_from_payload(payload)
    ref = save_account_cred(cred)  # 先落盘：补充信息失败不白费导入
    _enrich_cred(cred)
    save_account_cred(cred)  # 幂等 upsert：同 refresh_token 必命中原账号
    cred["account_id"] = ref.id
    return cred


def login_interactive(open_browser: bool = True) -> dict[str, Any]:
    """完整 Device Flow 登录，返回落盘的凭据 dict。"""
    oauth_host = _default_oauth_host()
    device_id = new_device_id()  # 登录即固定；随 cred 落盘，此后每账号不变

    flow = oauth.start_device_authorization(oauth_host, device_id=device_id)
    print()
    print("[Kimi] 请在浏览器中打开下面的链接，并用 Kimi 账号完成授权：")
    print()
    print(f"    {flow.verification_uri_complete or flow.verification_uri}")
    if flow.verification_uri and flow.verification_uri_complete:
        print(f"    （打不开就访问 {flow.verification_uri} 并输入授权码: {flow.user_code}）")
    print()
    if open_browser:
        # webbrowser 静默失败（返回 False）由 open_external_url 兜底
        if open_external_url(flow.verification_uri_complete or flow.verification_uri):
            print("[Kimi] 已尝试自动打开浏览器…")
        else:
            print("[Kimi] 自动打开浏览器失败，请手动复制上面的链接。")
    print(f"[Kimi] 等待授权中（最多 {int(min(flow.expires_in, LOGIN_TIMEOUT_S))} 秒，Ctrl-C 可取消）…")

    ticks = {"n": 0}

    def _tick(wait: float) -> None:
        ticks["n"] += int(wait)
        if ticks["n"] % 30 < int(wait):
            print(f"    …仍在等待授权（已等待约 {ticks['n']} 秒）")

    try:
        payload = oauth.poll_token(
            oauth_host, flow.device_code,
            device_id=device_id, interval=flow.interval,
            expires_in=min(flow.expires_in, LOGIN_TIMEOUT_S), on_wait=_tick)
    except KeyboardInterrupt:
        print("\n[Kimi] 已取消。")
        raise SystemExit(1) from None
    except oauth.OAuthError as exc:
        raise LoginError(str(exc)) from exc

    cred: dict[str, Any] = {
        "type": "kimi",
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expired": _expiry_iso(payload.get("expires_in")),
        "last_refresh": int(time.time()),
        "token_type": payload.get("token_type") or "Bearer",
        "scope": payload.get("scope") or "kimi-code",
        "base_url": _default_base_url(),
        "oauth_host": oauth_host,
        "device_id": device_id,
    }
    ref = save_account_cred(cred)  # 先落盘：补信息失败授权不白费
    _enrich_cred(cred)
    ref = save_account_cred(cred)
    _print_ready(cred, ref)
    return cred


def _print_ready(cred: dict[str, Any], ref: AccountRef) -> None:
    print()
    who = str(cred.get("nickname") or "") or ref.id
    level = str(cred.get("user_level_name") or "")
    print(f"[OK] Kimi 登录成功: {who}" + (f"（{level}）" if level else ""))
    print(f"     凭据文件: {account_cred_path(ref.id)}")
    accounts = list_accounts()
    if len(accounts) > 1:
        mine = next((i for i, a in enumerate(accounts) if a.id == ref.id), None)
        if mine is not None:
            print(f"     账号顺位: #{mine + 1}（共 {len(accounts)} 个账号，额度耗尽自动切换下一个）")
    print("     启动代理时加 --kimi（或 KIMI_ENABLED=1）即可启用该通道。")
    print("     若网关正在运行，需 `buddy restart` 才会加载新凭据。")
