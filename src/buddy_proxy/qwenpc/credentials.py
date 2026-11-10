"""千问办公（QwenWorkCN）MaaS 凭证：CDP 抓 JWT → mint token → 自刷新。

## 为什么是这个架构（三次试错的结果）

**不要读 Keychain。** 最初的做法是从 Keychain 取 safeStorage 主密钥自己解密
``engine-maas-key.dat``。问题是 ``security`` 每次都是新进程，而 Keychain 的 ACL
按「哪个程序」授权——**每次调用都可能弹系统授权框**，用户明确不接受。

**也不要用 App 的 safeStorage。** 让 App 自己解密（CDP 里调
``safeStorage.decryptString``）确实不弹框，但拿到的 ``accessKey`` 只在 App
**真正调用过模型**时才刷新（2 小时 TTL）。用户没在 App 里发过消息时读到的是
过期值，还得让他去发一条——体验很差。

**最终方案：抓 App 的 OAuth JWT，用它 mint。** 这条链完全绕开凭证文件：

::

    ① OAuth JWT（App 的登录态，有效期 ~6 天）
         │  CDP 注入 hook 抓 https.request 的 Authorization 头
         │  （App 启动时自己会请求 /api/sessions/unread，不用用户操作）
         ▼
    ② POST https://gateway.qwenwork.cn/api/desktop-sessions
       Authorization: Bearer <JWT>
       Body: {}
         ▼
       {"access_token":"sk-mm-v1.xxx", "base_url":"https://api9.qwenwork.cn",
        "ttl_ms":7200000}
         │
         ▼  ③ 直接调模型（**不需要再兑换**，实测直接可用）
            POST {base_url}/v1/chat/completions
            Authorization: Bearer sk-mm-v1.xxx

**token 还能自刷新**（实测）：把 ``sk-mm-v1.*`` 打到
``/v1/access-tokens/exchange`` 会换出一个新 token。所以：

- JWT 6 天有效 → 过期前重新抓一次即可
- 期间 token 2 小时过期 → 自刷新，**不碰 CDP、不碰 Keychain、不需要 App 在跑**

## App 重启交互（用户要求的）

正常双击启动的 App **没有调试端口**，CDP 连不上。所以需要：

::

    检测 App 在跑？
      ├─ 在跑但没 --inspect → 请它退出（App 会弹「确认退出」让用户看到）
      │                          → 带 --inspect=0 重启
      └─ 没在跑 → 直接带 --inspect=0 重启

**只有首次配置和 JWT 过期（6 天）时才需要这一步**；平时 token 自刷新。

## 模型名必须全小写（踩过）

UI 上显示 ``Qwen3.8-Max``，API 只认 ``qwen3.8-max``。大小写不对回
``invalid_request``——**不会**说「模型不存在」，极难排查。
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

APP_NAME = "QwenWorkCN"
APP_PATH = "/Applications/QwenWorkCN.app"
APP_BINARY = f"{APP_PATH}/Contents/MacOS/QwenWorkCN"

#: 带调试端口启动时把 stdout/stderr 写这里，从中解析 CDP 的 ws 地址。
DEBUG_LOG = "/tmp/buddy-qwenpc-app.log"

TOKEN_FILE_NAME = "qwenpc_token.json"

#: 上游 MaaS 网关（mint 接口挂在这里）。
GATEWAY = "https://gateway.qwenwork.cn"

#: mint 路径（源码里的 ``MAAS_MINT_PATH``）。
MINT_PATH = "/api/desktop-sessions"

#: 凭证里没给 base_url 时的兜底。
DEFAULT_API_BASE = "https://api9.qwenwork.cn"

#: 提前多久视为过期。
_EXPIRY_BUFFER_S = 120.0

#: 请求 token 时的 TTL（上游可能按自己策略缩短）。
_EXCHANGE_TTL_MS = 7_200_000

_lock = threading.Lock()


def token_path() -> pathlib.Path:
    override = os.environ.get("QWENPC_TOKEN_FILE")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".buddy-proxy" / TOKEN_FILE_NAME


class CredentialError(RuntimeError):
    """凭证不可用；消息直接面向用户（说明下一步做什么）。"""


# ---------------------------------------------------------------------------
# App 生命周期：确保有个带调试端口的实例
# ---------------------------------------------------------------------------


def app_pids() -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"{APP_PATH}/Contents/MacOS/{APP_NAME}"],
            capture_output=True, text=True, timeout=10).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    return [int(x) for x in out.split() if x.strip().isdigit()]


def _pid_has_inspect(pid: int) -> bool:
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=10).stdout
    except subprocess.SubprocessError:
        return False
    return "--inspect" in cmd


def quit_app(timeout_s: float = 25.0) -> bool:
    """请 App 正常退出。

    用 ``osascript quit`` 而非 kill：让 App 走自己的退出流程，**用户能看到并
    确认「是否退出」**——用户要求他知道 App 被关掉了。
    """
    try:
        subprocess.run(["osascript", "-e", f'quit app "{APP_NAME}"'],
                       capture_output=True, timeout=10)
    except (subprocess.SubprocessError, FileNotFoundError):
        log.debug("osascript quit 不可用，等待进程自行退出")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not app_pids():
            return True
        time.sleep(0.5)
    return not app_pids()


def _ws_from_log() -> str | None:
    """从启动日志里找 CDP 的 ws 地址（快路径）。"""
    pattern = re.compile(r"ws://127\.0\.0\.1:\d+/[0-9a-f-]{36}")
    try:
        text = pathlib.Path(DEBUG_LOG).read_text(errors="ignore")
    except OSError:
        return None
    hit = pattern.search(text)
    return hit.group(0) if hit else None


def _ws_from_process(pid: int) -> str | None:
    """从进程的监听端口探出 CDP ws 地址（不依赖日志文件）。

    为什么需要这条路径：日志可能被清理/覆盖（我们每次启动都会重写它），
    但**进程还在跑**。这时不该为了拿个地址去重启 App——用户已经确认过一次
    退出，没必要让他再确认一次。

    做法：列出该进程的本地监听端口，逐个问 ``/json``；哪个回的是
    ``electron/js2c/browser_init`` 就是主进程的 inspector。
    """
    try:
        out = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-p", str(pid)],
            capture_output=True, text=True, timeout=10).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    ports = set()
    for line in out.splitlines():
        m = re.search(r"127\.0\.0\.1:(\d+)\s*\(LISTEN\)", line)
        if m:
            ports.add(int(m.group(1)))
    for port in sorted(ports):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json", timeout=3) as resp:
                targets = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - 不是 inspector 就跳过
            continue
        if not isinstance(targets, list):
            continue
        for t in targets:
            if not isinstance(t, dict):
                continue
            ws_url = str(t.get("webSocketDebuggerUrl") or "")
            # 只要 Node 主进程那个（renderer 的 ws 调不了 safeStorage/require）
            if ws_url and t.get("type") == "node":
                return ws_url
        # 有些版本不给 type，只要 url 里有 uuid 也认
        for t in targets:
            if isinstance(t, dict) and t.get("webSocketDebuggerUrl"):
                return str(t["webSocketDebuggerUrl"])
    return None


def launch_with_debug() -> str:
    """带 ``--inspect=0`` 启动 App，返回 CDP ws 地址。"""
    pathlib.Path(DEBUG_LOG).parent.mkdir(parents=True, exist_ok=True)
    with open(DEBUG_LOG, "w") as logf:
        subprocess.Popen([APP_BINARY, "--inspect=0"],
                         stdout=logf, stderr=subprocess.STDOUT,
                         start_new_session=True)
    deadline = time.time() + 45.0
    while time.time() < deadline:
        ws = _ws_from_log()
        if ws:
            return ws
        time.sleep(0.5)
    raise CredentialError(
        "启动千问办公失败（没拿到调试端口）。请手动执行：\n"
        f"  {APP_BINARY} --inspect=0")


def ensure_debug_app() -> str:
    """确保有带调试端口的实例在跑，返回 CDP ws 地址。"""
    pids = app_pids()
    if pids:
        debug_pids = [p for p in pids if _pid_has_inspect(p)]
        if debug_pids:
            # 先看日志（快），再直接问进程（日志可能被清）——都不行才重启
            ws = _ws_from_log()
            if ws:
                return ws
            for pid in debug_pids:
                ws = _ws_from_process(pid)
                if ws:
                    log.info("从进程 %d 探测到 CDP 地址", pid)
                    return ws
            log.info("千问有调试端口但探测不到 ws 地址，重启一次")
        # 在跑但没有调试端口：请它退出
        log.info("千问在跑但无调试端口，请求退出后重启")
        if not quit_app():
            raise CredentialError(
                "千问办公没有退出（可能你没确认弹窗）。请手动退出后重试")
        time.sleep(1.0)
    return launch_with_debug()


# ---------------------------------------------------------------------------
# CDP
# ---------------------------------------------------------------------------


def _cdp(ws_url: str, expression: str, *, timeout: float = 30.0,
         await_promise: bool = False) -> str:
    """连 CDP 执行表达式，返回字符串结果。"""
    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CredentialError(
            "缺少 websocket-client 依赖（pip install websocket-client）") from exc
    ws = websocket.create_connection(ws_url, timeout=timeout)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": expression, "includeCommandLineAPI": True,
            "awaitPromise": await_promise}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") != 1:
                continue
            result = msg.get("result", {})
            if "exceptionDetails" in result:
                raise CredentialError(
                    f"CDP 执行失败：{str(result['exceptionDetails'])[:200]}")
            return str((result.get("result") or {}).get("value") or "")
    finally:
        ws.close()


#: 注入到 App 主进程的 hook：把发往 qwenwork 的 Authorization 头存到全局。
_HOOK_EXPR = r"""
(function(){
  if (globalThis.__BUDDY_JWT_HOOK__) return 'already';
  globalThis.__BUDDY_JWT_HOOK__ = true;
  const https = require('https');
  const orig = https.request;
  https.request = function(...args) {
    try {
      const opt = typeof args[0] === 'string' ? {path: args[0]} : (args[0] || {});
      const host = String(opt.hostname || opt.host || '');
      const hdrs = opt.headers || {};
      const auth = hdrs.Authorization || hdrs.authorization || '';
      if (/qwenwork/.test(host) && /^Bearer\s+eyJ/.test(String(auth))) {
        globalThis.__BUDDY_JWT__ = String(auth).replace(/^Bearer\s+/, '');
      }
    } catch (e) {}
    return orig.apply(this, args);
  };
  return 'hooked';
})()
"""


def _install_hook(ws_url: str) -> None:
    _cdp(ws_url, _HOOK_EXPR)


def _read_jwt(ws_url: str) -> str:
    return _cdp(ws_url, "String(globalThis.__BUDDY_JWT__ || '')")


# ---------------------------------------------------------------------------
# 抓 JWT
# ---------------------------------------------------------------------------


def capture_jwt(*, wait_s: float = 40.0) -> str:
    """确保 App 在跑（带调试端口）并抓一个 OAuth JWT。

    App 启动后会自己请求 ``/api/sessions/unread``（带 JWT），所以**不需要用户
    做任何操作**——注入 hook 后等它自己发请求即可。等不到就主动触发一次。
    """
    ws = ensure_debug_app()
    _install_hook(ws)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        jwt = _read_jwt(ws)
        if jwt:
            log.info("qwenpc 抓到 OAuth JWT（长度 %d）", len(jwt))
            return jwt
        time.sleep(1.0)
    raise CredentialError(
        "没能从千问办公拿到登录态（JWT）。请确认 App 已登录并保持运行，然后重试")


# ---------------------------------------------------------------------------
# mint / 刷新
# ---------------------------------------------------------------------------


def mint_token(jwt: str) -> dict[str, Any]:
    """用 OAuth JWT 换一个能直接调模型的 MaaS token。

    **这是本通道的关键洞察**：``/api/desktop-sessions`` 是 App 自己用来签发票据
    的接口，返回的 token 可以直接打 ``/v1/chat/completions``，不需要再走
    ``/v1/access-tokens/exchange``（实测）。
    """
    payload = json.dumps({}).encode()
    req = urllib.request.Request(
        f"{GATEWAY}{MINT_PATH}", data=payload, method="POST",
        headers={"Authorization": f"Bearer {jwt}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        if exc.code in (401, 403):
            raise CredentialError(
                "OAuth 登录态被拒绝（JWT 可能已过期）。"
                "请打开千问办公重新登录，然后重试") from exc
        raise CredentialError(f"mint 失败 HTTP {exc.code}: {body}") from exc
    except Exception as exc:  # noqa: BLE001
        raise CredentialError(f"mint 失败：{type(exc).__name__}") from exc

    token = str(data.get("access_token") or "").strip()
    if not token:
        raise CredentialError("mint 响应里没有 access_token")
    expires_ms = data.get("expires_at_ms")
    expires = (float(expires_ms) / 1000.0
               if isinstance(expires_ms, (int, float))
               and not isinstance(expires_ms, bool) and expires_ms > 0
               else time.time() + _EXCHANGE_TTL_MS / 1000.0)
    base = str(data.get("base_url") or DEFAULT_API_BASE).rstrip("/")
    return {"token": token, "expires_at": expires, "base": base}


def renew_token(token: str, base: str) -> dict[str, Any]:
    """用旧 token 换新 token（自刷新，不碰 CDP/Keychain）。"""
    root = base.rstrip("/")
    url = f"{root}/v1/access-tokens/exchange"
    payload = json.dumps({"ttl_ms": _EXCHANGE_TTL_MS}).encode()
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:160]
        except Exception:  # noqa: BLE001
            pass
        raise CredentialError(f"token 续期失败 HTTP {exc.code}: {body}") from exc
    except Exception as exc:  # noqa: BLE001
        raise CredentialError(f"token 续期失败：{type(exc).__name__}") from exc
    new_token = str(data.get("access_token") or "").strip()
    if not new_token:
        raise CredentialError("续期响应里没有 access_token")
    exp = data.get("expires_at")
    expires = (float(exp) / 1000.0 if isinstance(exp, (int, float))
               and not isinstance(exp, bool) and exp > 0
               else time.time() + _EXCHANGE_TTL_MS / 1000.0)
    return {"token": new_token, "expires_at": expires, "base": base}


# ---------------------------------------------------------------------------
# 本地缓存
# ---------------------------------------------------------------------------


def _load_cache() -> dict[str, Any]:
    try:
        data = json.loads(token_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(data: dict[str, Any]) -> None:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def _jwt_expiry(jwt: str) -> float:
    """从 JWT 的 payload 里读 exp（拿不到返回 0）。"""
    import base64
    parts = jwt.split(".")
    if len(parts) < 2:
        return 0.0
    try:
        pad = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:  # noqa: BLE001
        return 0.0
    exp = payload.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else 0.0


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def auth_token(*, force: bool = False) -> str:
    """取一个可用的 MaaS token。**优先用缓存和自刷新，尽量不碰 CDP。**

    顺序（每步失败才走下一步）：

    1. 缓存 token 未过期 → 直接用
    2. 用缓存 token 自刷新（不碰 App）
    3. 缓存的 JWT 未过期 → mint
    4. 经 CDP 抓 JWT → mint（可能触发 App 重启）
    """
    with _lock:
        cache = _load_cache()
        now = time.time()

        token = str(cache.get("token") or "")
        expires = cache.get("expires_at")
        base = str(cache.get("base") or DEFAULT_API_BASE)

        if token and isinstance(expires, (int, float)) and not force:
            if float(expires) - _EXPIRY_BUFFER_S > now:
                return token
            try:
                renewed = renew_token(token, base)
                cache.update(renewed)
                _save_cache(cache)
                log.info("qwenpc token 自刷新成功")
                return str(renewed["token"])
            except CredentialError as exc:
                log.warning("qwenpc token 自刷新失败（%s），尝试重新 mint", exc)

        jwt = str(cache.get("jwt") or "")
        jwt_exp = cache.get("jwt_expires_at")
        if not jwt or not isinstance(jwt_exp, (int, float)) \
                or float(jwt_exp) - _EXPIRY_BUFFER_S <= now:
            jwt = capture_jwt()
            jwt_exp = _jwt_expiry(jwt) or (now + 6 * 86400)

        minted = mint_token(jwt)
        cache.update(minted)
        cache["jwt"] = jwt
        cache["jwt_expires_at"] = jwt_exp
        cache["saved_at"] = now
        _save_cache(cache)
        log.info("qwenpc token mint 成功（base=%s）", minted["base"])
        return str(minted["token"])


def api_base() -> str:
    """当前 token 对应的 API base。"""
    cache = _load_cache()
    return str(cache.get("base") or DEFAULT_API_BASE)


#: 账号上下文端点（个人版拿额度的**唯一**入口）。
#:
#: 注意不是 ``/api/v2/quota/usage``——那个是**企业版**用的，个人版调用会返回
#: 一堆 null（``total``/``used``/``remaining`` 全空，``user_type=personal_standard``），
#: 看起来像"没有额度"。个人版的余额在 ``account-context`` 的 ``quota.remaining``
#: 里（实测 395.9554）。
ACCOUNT_CONTEXT_PATH = ("/api/v1/adapter/user/account-context"
                        "?include=user,plan,quota")


def account_context(jwt: str | None = None, *, timeout: float = 20.0
                    ) -> dict[str, Any]:
    """查账号上下文（用户 / 套餐 / 额度）。

    返回 ``{"user": {...}, "plan": {...}, "quota": {"remaining": float, ...}}``。

    ⚠ 必须用 **OAuth JWT**，不能用 MaaS token（``sk-mm-v1.*``）——后者是给
    模型调用用的，打这个网关会 401。
    """
    token = jwt or str(_load_cache().get("jwt") or "")
    if not token:
        raise CredentialError("没有可用的 OAuth JWT，无法查询额度")
    req = urllib.request.Request(
        f"{GATEWAY}{ACCOUNT_CONTEXT_PATH}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:160]
        except Exception:  # noqa: BLE001
            pass
        raise CredentialError(
            f"查询额度失败 HTTP {exc.code}: {body}") from exc
    except Exception as exc:  # noqa: BLE001
        raise CredentialError(
            f"查询额度失败：{type(exc).__name__}") from exc
    payload = data.get("data") if isinstance(data, dict) else None
    return payload if isinstance(payload, dict) else {}


def remaining_quota(jwt: str | None = None) -> float | None:
    """当前剩余额度（拿不到返回 None）。"""
    try:
        ctx = account_context(jwt)
    except CredentialError:
        return None
    quota = ctx.get("quota")
    if not isinstance(quota, dict):
        return None
    value = quota.get("remaining")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def clear_cache() -> None:
    with _lock:
        try:
            token_path().unlink()
        except OSError:
            pass
