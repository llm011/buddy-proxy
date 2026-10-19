"""本地 OAuth 回调服务（``http://localhost:1455/auth/callback``）。

OpenAI 对 ``redirect_uri`` 做白名单校验，**端口 1455 与 path 都不能换**，
所以这里不提供端口配置项；被占用时的处理见 :func:`start_callback_server`
返回 ``None`` 的分支（登录流程退化为「手动粘贴回调 URL」）。

设计取舍：不用 ``trae_work_login_server`` 那种独立进程 + 常驻服务，而是
在登录进程内起一个后台 ``ThreadingHTTPServer`` 线程，收到 code 就置事件、
主线程立刻继续——登录是**一次性**流程，进程退出服务就没了，不需要
生命周期管理。
"""

from __future__ import annotations

import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .oauth import CALLBACK_PATH, CALLBACK_PORT

log = logging.getLogger(__name__)

#: 成功/失败页（极简 HTML：用户只看一眼就会关掉）。
_SUCCESS_HTML = """<!doctype html><meta charset="utf-8">
<title>登录成功</title>
<body style="font-family:system-ui;text-align:center;padding-top:15vh">
<h1>✅ 授权完成</h1>
<p>可以关闭此页面回到终端。</p></body>"""

_FAIL_HTML = """<!doctype html><meta charset="utf-8">
<title>登录失败</title>
<body style="font-family:system-ui;text-align:center;padding-top:15vh">
<h1>⚠️ 授权失败</h1><p>{detail}</p>
<p>请回到终端查看错误信息。</p></body>"""


class CallbackResult:
    """回调结果（线程 → 主线程传递）。"""

    def __init__(self) -> None:
        self.code = ""
        self.state = ""
        self.error = ""
        self.event = threading.Event()

    def set_code(self, code: str, state: str) -> None:
        self.code = code
        self.state = state
        self.event.set()

    def set_error(self, error: str) -> None:
        self.error = error
        self.event.set()

    def wait(self, timeout: float) -> bool:
        return self.event.wait(timeout)


def make_handler(result: CallbackResult):
    """构造处理回调请求的 Handler 类（绑定到给定的 result）。"""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.rstrip("/") not in (CALLBACK_PATH.rstrip("/"), "/success"):
                self.send_response(404)
                self.end_headers()
                return
            query = urllib.parse.parse_qs(parsed.query)
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            error = (query.get("error") or [""])[0]
            desc = (query.get("error_description") or [""])[0]

            if parsed.path.rstrip("/") == "/success":
                self._html(200, _SUCCESS_HTML)
                return

            if code:
                result.set_code(code, state)
                self._html(200, _SUCCESS_HTML)
                return

            detail = f"{error}{(': ' + desc) if desc else ''}" or "回调里没有 code 参数"
            result.set_error(detail)
            self._html(400, _FAIL_HTML.format(detail=detail))

        def _html(self, status: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: Any) -> None:
            """留痕但脱敏：不打完整 query（含 code）。"""
            try:
                path = urllib.parse.urlparse(self.path).path
                print(f"[codex-srv] {self.command} {path}", flush=True)
            except Exception:  # noqa: BLE001
                pass

    return _Handler


def start_callback_server(
    result: CallbackResult, *, host: str = "127.0.0.1", port: int = CALLBACK_PORT,
) -> ThreadingHTTPServer | None:
    """在后台线程起回调服务；端口被占用等失败返回 ``None``。

    返回 ``None`` 时调用方要退化成「打印 URL + 让用户粘贴回调 URL」，
    不能直接报错退出——1455 被别的 Codex CLI 会话占着是常见情况。
    """
    try:
        server = ThreadingHTTPServer((host, port), make_handler(result))
    except OSError as exc:
        # 静默返回 None 会让用户以为是「端口被占用」；把真实原因（权限 /
        # 地址已占用 / 绑定失败）打出来，否则登录卡在粘贴提示里无从排查
        print(f"[Codex] 无法监听 {host}:{port}（{exc}），退化为手动粘贴回调 URL。",
              flush=True)
        return None
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="codex-oauth-callback")
    thread.start()
    return server


def stop_callback_server(server: ThreadingHTTPServer | None) -> None:
    """关掉回调服务（幂等）。"""
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:  # noqa: BLE001 - 关闭失败不该影响登录结果
        pass
    try:
        server.server_close()
    except Exception:  # noqa: BLE001
        pass
