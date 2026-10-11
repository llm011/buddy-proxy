"""小米账号交互式登录：浏览器登录 → 长轮询接住结果 → 落盘凭据。

为什么是**轮询**而不是像 Trae 那样起本地回调服务：小米的 ``callback`` 参数
是服务端带签名生成的（``/sts?sign=...&followup=...``），只认它自己白名单内
的域。自建 ``http://127.0.0.1:xxxx/cb`` 会被直接拒：

    {"code":10025,"result":"error","desc":"Callback连接不合法"}

拿它自己生成的 ``https://account.xiaomi.com/sts`` 去试同样被拒——签名每次
由服务端现算，外部无法伪造。所以走官方给第三方留的 ``longPolling/loginUrl``
（**不传 callback**，用 ticket 机制），跟 ``buddy login qoder`` 的 device
flow 同构：打印并打开链接 → 用户登录 → 命令行轮询到结果自动继续。

登录结果的形状（asar 里 ``z()`` 解析函数 + 2026-09 实测跑通）：长轮询返回
``&&&START&&&`` 前缀包装的 JSON，``data`` 或顶层直接带 ``passToken`` / ``userId``。
拿到这两个就够用了——现有的 :mod:`buddy_proxy.mimo.sso` Phase 1 只需要它俩换
serviceToken（``nonce`` / ``ssecurity`` 是换票过程中自己产生的）。

另外两个**实测**（不是从 asar 推的，踩过才知道）：

1. ``loginUrl`` 是**给程序调的 API 端点**，浏览器直接打开只会看到
   ``{"code":70016,"desc":"登录验证失败"}``。真正的登录页在同一个响应的
   ``location`` 字段里，且**必须用浏览器 UA** 请求才拿得到那个响应体
   （客户端 UA 会直接 302）。见 :func:`_browser_url`。
2. ticket 只有 **300 秒**（Qoder 的 device flow 是 10 分钟）。而且**过期后长轮询
   依然挂住、不返回任何错误**——除了自己按 ``expires_in`` 掐表，没有别的信号可用。

凭据落到 ``~/.buddy-proxy/mimo_account.json``（0600）。读取顺序见
:func:`load_saved_account`：**文件优先于桌面 cookie 库**，这样新机器上不装
MiMo 桌面也能用。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from buddy_proxy.core.browser import open_external_url
from pathlib import Path
from typing import Any, Callable

import httpx

from buddy_proxy.core.paths import state_file

from .config import SSO_SID, SSO_UA
from .sso import AccountCookies

#: 生成登录链接 / 长轮询的入口（**不能**带 callback，见模块 docstring）
LOGIN_URL_API = "https://account.xiaomi.com/longPolling/loginUrl"
#: 浏览器 UA。**必须**用浏览器 UA：`loginUrl` 对 ``MiClaw/1.0`` 之类的客户端 UA
#: 直接 302，跟完之后拿到的才是带 ``location`` 的登录结果。
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
#: 小米登录结果的前缀包装（与 serviceLogin 一致）
_RESULT_PREFIX = "&&&START&&&"
#: 单次长轮询挂多久。官方给的 ``timeout`` 是 300s，这里留点余量自己断开重连，
#: 免得卡在一个连接上等满 5 分钟才发现该刷 ticket。
_POLL_TIMEOUT_S = 60.0
#: 总预算兜底（秒）。正常情况下用链接自带的 ``expires_in``（300s）；这个常量只在
#: 上游没给 ``timeout`` 字段时兜底。
_DEFAULT_BUDGET_S = 300.0
#: 链接失效前留出的余量：到点就停下报错，别让用户对着一个已经死了的 ticket 干等。
#: 实测 ticket 过期后长轮询**依然挂住**（不返回任何错误），所以只能自己掐表。
_EXPIRY_GRACE_S = 5.0


class LoginError(RuntimeError):
    """登录流程失败（生成链接失败 / 被拒 / 超时）。"""


@dataclass
class LoginSession:
    """一次登录尝试：给用户打开的链接 + 用来等待结果的轮询地址。"""

    login_url: str
    poll_url: str
    expires_in: int = 300


def _strip_prefix(text: str) -> str:
    body = text.strip()
    if body.startswith(_RESULT_PREFIX):
        body = body[len(_RESULT_PREFIX) :]
    return body


def account_path() -> Path:
    """登录凭据落盘位置；``MIMO_ACCOUNT_JSON`` 可覆盖。"""
    env = os.environ.get("MIMO_ACCOUNT_JSON", "").strip()
    if env:
        return Path(env).expanduser()
    return state_file("mimo_account.json")


def save_account(account: AccountCookies) -> Path:
    """写凭据（0600，含 passToken）。

    先按 0600 建文件再写内容：``write_text`` 会以默认 0644 创建，token 在
    ``chmod`` 之前就已落盘，同机其它用户在那个窗口里读得到。
    """
    path = account_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pass_token": account.pass_token,
        "user_id": account.user_id,
        "c_user_id": account.c_user_id,
        "saved_at": int(time.time()),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def load_saved_account() -> AccountCookies | None:
    """读登录落盘的凭据；没有或不全时返回 ``None``。"""
    try:
        data = json.loads(account_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    pass_token = str(data.get("pass_token") or "").strip()
    user_id = str(data.get("user_id") or "").strip()
    if not pass_token or not user_id:
        return None
    return AccountCookies(
        pass_token=pass_token,
        user_id=user_id,
        c_user_id=str(data.get("c_user_id") or "").strip(),
    )


def _browser_url(api_url: str, http: httpx.Client) -> str:
    """把 ``loginUrl``（API 端点）换成**给浏览器打开的登录页地址**。

    直接开 ``loginUrl`` 只会看到一段 JSON：那个地址是给程序调的，浏览器访问会
    拿到 ``code:70016 登录验证失败``。真正的登录页藏在同一个响应的 ``location``
    字段里（``account.xiaomi.com/fe/service/login?...``，一个 SPA，二维码由 JS
    渲染）。所以这里必须先跟一次跳转、把 ``location`` 取出来。

    取不到时退回 ``api_url``：至少不是死路，用户还能看到那段 JSON 自行判断。
    """
    try:
        resp = http.get(api_url, headers={"User-Agent": _BROWSER_UA}, follow_redirects=True)
        data = json.loads(_strip_prefix(resp.text))
    except (httpx.HTTPError, json.JSONDecodeError):
        return api_url
    location = str(data.get("location") or "")
    return location or api_url


def start_login(sid: str = SSO_SID, client: httpx.Client | None = None) -> LoginSession:
    """取一个登录链接 + 对应的轮询地址（``callback`` 必须留空）。"""
    owns = client is None
    http = client or httpx.Client(timeout=20.0)
    try:
        resp = http.get(
            LOGIN_URL_API,
            params={
                "_qrsize": "480",
                "qs": f"?sid={sid}",
                "sid": sid,
                "callback": "",  # 非空一律 10025「Callback连接不合法」
                "_json": "true",
            },
            headers={"User-Agent": _BROWSER_UA},
        )
        data = json.loads(_strip_prefix(resp.text))
        if data.get("code") != 0:
            raise LoginError(
                f"取登录链接被拒: {data.get('desc') or data.get('description') or data}"
            )
        poll_url = str(data.get("lp") or "")
        api_url = str(data.get("loginUrl") or "")
        if not api_url or not poll_url:
            raise LoginError(f"登录链接响应缺字段: {sorted(data)}")
        # loginUrl 是 API 端点，浏览器要开 location 里那个（见 _browser_url）
        login_url = _browser_url(api_url, http)
    except httpx.HTTPError as exc:
        raise LoginError(f"取登录链接失败: {exc}") from exc
    finally:
        if owns:
            http.close()

    return LoginSession(
        login_url=login_url,
        poll_url=poll_url,
        expires_in=int(data.get("timeout") or 300),
    )


def _extract_account(node: Any) -> AccountCookies | None:
    """从结果体里挖 ``passToken``/``userId``。

    asar ``z()`` 的做法：先看 ``data`` 子节点，再退回顶层——两种形态上游都用过。
    """
    for candidate in (node.get("data"), node) if isinstance(node, dict) else ():
        if not isinstance(candidate, dict):
            continue
        pass_token = str(candidate.get("passToken") or candidate.get("pass_token") or "").strip()
        user_id = str(candidate.get("userId") or candidate.get("user_id") or "").strip()
        if pass_token and user_id:
            return AccountCookies(
                pass_token=pass_token,
                user_id=user_id,
                c_user_id=str(candidate.get("cUserId") or candidate.get("c_user_id") or "").strip(),
            )
    return None


def poll_login(
    session: LoginSession,
    on_tick: Callable[[], None] | None = None,
    client: httpx.Client | None = None,
) -> AccountCookies:
    """等用户登录，返回凭据。

    长轮询在**未登录时会把连接挂住**（这正是它的语义）。所以这里的循环是：
    挂住 → 客户端超时断开 → 重连再挂，直到拿到结果或链接失效。

    截止时间是**链接自带的 ``expires_in``**，不是拍脑袋的常量：ticket 一旦过期，
    上游仍会把连接挂着不返回任何错误（实测），只能自己掐表，否则用户会对着一个
    已经死掉的链接白等。
    """
    owns = client is None
    http = client or httpx.Client(timeout=_POLL_TIMEOUT_S)
    budget = float(session.expires_in or 0) or _DEFAULT_BUDGET_S
    deadline = time.time() + max(budget - _EXPIRY_GRACE_S, 0.0)
    try:
        while time.time() < deadline:
            if on_tick is not None:
                on_tick()
            try:
                resp = http.get(session.poll_url, headers={"User-Agent": SSO_UA})
            except httpx.ReadTimeout:
                continue  # 没登录时的正常路径：挂满就重连
            except httpx.HTTPError as exc:
                raise LoginError(f"轮询失败: {exc}") from exc

            body = _strip_prefix(resp.text)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                # 非 JSON 响应（HTML 错误页等）不当成结果，继续等
                continue
            if not isinstance(data, dict):
                continue

            account = _extract_account(data)
            if account is not None:
                return account

            code = data.get("code")
            # 未登录/待扫码时历史上出现过 code 非 0 但属于「还没好」的态；
            # 只有在响应**带了明确终态描述**且确实没凭据时才放弃。
            if code not in (0, "0", None) and data.get("result") == "error":
                desc = str(data.get("desc") or data.get("description") or "")
                if "过期" in desc or "expire" in desc.lower() or "invalid" in desc.lower():
                    raise LoginError(f"登录链接已失效（{desc}），请重新发起登录")
        raise LoginError(
            f"登录链接已失效（{int(budget)} 秒内未完成登录），请重新发起登录"
        )
    finally:
        if owns:
            http.close()


def login_interactive(
    open_browser: bool = True, sid: str = SSO_SID
) -> tuple[AccountCookies, Path]:
    """完整交互流程：取链接 → 打开 → 等结果 → 落盘。

    浏览器侧与 Qoder 一致：用户只需在页面上完成登录，不用回终端做任何事。
    返回 ``(凭据, 落盘路径)``，调用方负责打印成功信息。
    """
    session = start_login(sid=sid)
    print()
    print("[Mimo] 请在浏览器中打开下面的链接，用小米账号完成登录：")
    print()
    print(f"    {session.login_url}")
    print()
    if open_browser:
        # webbrowser 静默失败（返回 False）由 open_external_url 兜底
        if open_external_url(session.login_url):
            print("[Mimo] 已尝试自动打开浏览器…")
        else:
            print("[Mimo] 自动打开浏览器失败，请手动复制上面的链接。")
    print(f"[Mimo] 等待登录中（链接 {session.expires_in} 秒内有效，Ctrl-C 可取消）…")

    ticks = {"n": 0}

    def _tick() -> None:
        ticks["n"] += 1
        if ticks["n"] % 2 == 0:
            print(f"    …仍在等待登录（已等待约 {int(ticks['n'] * _POLL_TIMEOUT_S)} 秒）")

    account = poll_login(session, on_tick=_tick)
    path = save_account(account)
    # 换号后旧的 serviceToken 必须作废：它只对上一个账号有效，而网关可能
    # 已经把它缓存下来了（见 sso.load_cached_token 的 user_id 校验）。
    # 这里主动清一次，省得等下次请求 401 才发现用的是别人的票。
    try:
        from .sso import invalidate_cache

        invalidate_cache()
    except Exception:  # noqa: BLE001 - 清缓存失败不该让登录报错
        pass
    print()
    return account, path
