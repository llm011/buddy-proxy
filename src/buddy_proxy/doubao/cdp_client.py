"""豆包工作 CDP 客户端 —— 直连本地 DoubaoWork.app 的内置 Chromium。

与 ``BrowserClient``（Playwright，自己开 Chromium 连 doubao.com）不同，
本客户端**复用豆包工作 App 自带的 Helper（Chromium 147）**：

1. 优先**复用主 App**：探测 ``--remote-debugging-port`` 已开则直接连接；
   未开时用 ``open -a DoubaoWork --args --remote-debugging-port=<port>``
   拉起主 App（不杀用户进程，主 App 自带 ``--saman-from-chat=<主AppPID>``
   开 CDP），用户界面正常可用。
2. 主 App 不可用时才退回**独立 Helper**（``--saman-from-chat=1``），
   从磁盘 profile 自动加载完整登录态，无需扫码。
3. 纯 stdlib WebSocket 直连 CDP（避开 playwright 在沙箱被 SIGTERM 的问题）。
4. 在 ``chrome://doubaowork-chat/chat`` 页面的 JS 环境里 ``fetch``
   ``/chat/completion``，自动带 httpOnly cookie + a_bogus 签名。
5. 流式：JS 后台 fetch 逐块读 SSE push 到 window 队列，Python 轮询取回
   （该 Helper 不派发 ``Runtime.consoleAPICalled`` 事件，不能走 console 桥）。

关键坑（踩坑总结）：
- 独立 Helper 必须加 ``--disable-features=SpareRendererForSitePerProcess``，
  否则 10 秒后 spare renderer 崩溃连锁拖垮 network service。
- 只在**退回独立 Helper** 时才 ``pkill -KILL -f DoubaoWork``（单例锁拦截
  伪 pid 启动）；复用主 App 时绝不杀进程。
- CDP 客户端帧必须 masked（RFC6455 规定）。

接口与 ``BrowserClient`` 对齐，供 ``DoubaoProvider`` 无缝切换。
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import subprocess
import threading
import time
import urllib.request
import uuid
from typing import Any, AsyncGenerator, Optional

from .payloads import _build_agent_payload, _build_classic_payload
from .ws import _WS

log = logging.getLogger(__name__)

# 豆包工作 Helper 二进制路径
HELPER_BIN = (
    "/Applications/DoubaoWork.app/Contents/Helpers/"
    "DoubaoWork Browser.app/Contents/MacOS/DoubaoWork Browser"
)

DEFAULT_BOT_ID = "7338286299411103781"
DEFAULT_PORT = 9223

# 主 App 主可执行文件（open -a 拉起的那个「豆包工作」进程）
APP_BIN_PATTERN = "DoubaoWork.app/Contents/MacOS/DoubaoWork"

# 一次 App 身份启动里，等调试端口起来的秒数（App 冷启动要拉起内嵌 Chromium）
_MAIN_APP_CDP_WAIT_S = 30

# 「App 身份」进程：主 App + 它内嵌的 Helper。Helper 本身路径里也含
# ``DoubaoWork.app``，所以一条正则同时覆盖两者；pkill -f 是正则匹配。
_APP_PROCESS_PATTERN = "DoubaoWork\\.app/Contents/"


def _is_app_chat_url(url: str | None) -> bool:
    """是否 App 自己的聊天窗口（内部 WebUI 页）。

    /json/list 里 scheme 显示为 ``doubaowork://``（location.href 则是
    ``chrome://doubaowork-chat/chat/...``），所以按路径特征匹配而非 scheme。
    这是 App 同款请求上下文（自带 bdms/web_id/登录态）：2026-09-09 实测，
    纯 www.doubao.com 标签页发消息 LLM 回复正常但调不了工具（agent 任务
    卡死），App 聊天页才是工具可用的上下文。
    """
    return "doubaowork-chat/chat" in (url or "")


def _usable_chat_href(href: str | None) -> bool:
    """该页面的 JS 环境是否可直接发 /chat/completion（无需新开标签页）。"""
    if not href:
        return False
    return "doubao.com" in href or _is_app_chat_url(href)


def _main_app_installed() -> bool:
    """豆包工作主 App 是否已安装（常见位置）。"""
    for p in (
        pathlib.Path("/Applications/DoubaoWork.app"),
        pathlib.Path.home() / "Applications" / "DoubaoWork.app",
    ):
        if p.exists():
            return True
    return False

# 一方模型 -> use_deep_think（与 doubao_provider._DOUBAO_CHAT_MODELS 对齐）
# 这里只保留常量，模型表仍由 DoubaoProvider 维护。

_STABLE_FLAGS = [
    "--saman-from-chat=1",
    "--no-first-run",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-dev-shm-usage",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-extensions",
    "--disable-default-apps",
    "--disable-features=SpareRendererForSitePerProcess",
]


class CDPDoubaoClient:
    """直连豆包工作 Helper 的 CDP 客户端。"""

    def __init__(self, port: int = DEFAULT_PORT, helper_bin: str = HELPER_BIN):
        self.port = port
        self.helper_bin = helper_bin
        self._proc: subprocess.Popen | None = None
        self._ws: _WS | None = None
        self._ready = False
        self._web_id: str = ""
        self._consecutive_failures = 0
        self._last_error_code = 0
        # 所有 CDP 命令（含 to_thread 并发调用）都经此锁串行化，
        # 避免多请求同时读写同一 WebSocket 导致 id 串号 / 响应丢失 / 串流。
        self._ws_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def last_error_code(self) -> int:
        return self._last_error_code

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self, error_code: int = 0) -> None:
        self._consecutive_failures += 1
        self._last_error_code = error_code

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _probe_cdp(self) -> bool:
        """探测端口上是否已有可用的 CDP（主 App 已开调试端口）。"""
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/version", timeout=2
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _connect_chat(self) -> None:
        """连接 chat 页面 target，等 bdms 就绪并提取 web_id。"""
        chat = await self._wait_for_target()
        if not chat:
            raise RuntimeError("CDP target not found")
        ws_url = chat["webSocketDebuggerUrl"]
        path = ws_url.split(f":{self.port}", 1)[1]
        self._ws = _WS("127.0.0.1", self.port, path)

        # 连到的可能只是 Helper 的占位页（无 bdms/cookie，直接发请求会报
        # 710020202 invalid param）—— 此时新开独立标签页。绝不能 Page.navigate
        # 现有 target：background 页承载 agent 运行时（工具桥），导航走它会
        # 导致 App 端「工作环境准备中」卡死、工具全部失灵（2026-09-08 实测）。
        # App 自己的聊天窗口（doubaowork-chat/chat）与豆包域名页可直接用：
        # 在它们里面 fetch 就是 App 同款上下文（2026-09-09 实测工具可用）。
        href = await asyncio.to_thread(self._evaluate, "location.href")
        if not _usable_chat_href(href):
            log.info("CDPDoubaoClient: target is %s, opening dedicated tab for chat", href)
            # Target.createTarget 新开一个聊天标签（不动 App 自己的页面）。
            with self._ws_lock:
                r = self._ws.cmd(
                    "Target.createTarget",
                    {"url": "https://www.doubao.com/chat/", "background": False},
                )
            new_target_id = (r.get("result") or {}).get("targetId")
            if not new_target_id:
                # 不做 Page.navigate 兜底：能连上的非豆包页只剩 App 内部页，
                # 导航它们正是 269fe33 要修的劫持事故，宁可明确报错。
                raise RuntimeError(
                    f"Target.createTarget 失败（target={href}），无法安全地"
                    "开辟聊天页面；请手动打开一个豆包聊天窗口后重试"
                )
            # 重连到新开标签页的 target
            await asyncio.to_thread(self._switch_to_target_by_id, new_target_id)
            # 等导航完成（readyState=complete 且已到豆包域名）
            deadline = time.time() + 30
            while time.time() < deadline:
                href = await asyncio.to_thread(self._evaluate, "location.href")
                rs = await asyncio.to_thread(self._evaluate, "document.readyState")
                if "doubao.com" in (href or "") and rs == "complete":
                    break
                await asyncio.sleep(0.5)

        await self._wait_for_bdms()
        self._web_id = await self._extract_web_id() or ""
        self._ready = True
        log.info("CDPDoubaoClient: ready (web_id=%s)", self._web_id[:20])

    async def start(self) -> None:
        """确保 CDP 可用：优先复用主 App（共存，不杀用户进程），其次「App 身份
        启动 Helper」，最后才退回裸 Helper 直连。

        为什么不再「首次请求直接裸 Helper」：裸 Helper 会以同一份 profile 抢走
        Chromium 单例锁（``SingletonLock -> <host>-<pid>``）。之后用户自己点开
        豆包工作 App，App 发现锁被一个无 UI 的 Helper 占着就起不来，表现就是
        「豆包工作打不开」。所以只有「App 身份启动也拿不到 CDP」时才用裸 Helper。
        """
        # 1) 端口已有 CDP 直接复用（主 App 正在跑且开了调试端口）
        if await asyncio.to_thread(self._probe_cdp):
            log.info("CDPDoubaoClient: reuse existing CDP on port %d", self.port)
            await self._connect_chat()
            return

        # 2) 由 App 身份拉起（主 App 已在跑时 open -a 只激活，不杀进程）
        self._require_main_app_installed()
        launched = await asyncio.to_thread(self._launch_main_app)
        if launched and await self._wait_cdp_ready():
            log.info("CDPDoubaoClient: 主 App CDP ready on port %d", self.port)
            await self._connect_chat()
            return
        log.warning("CDPDoubaoClient: 主 App 未开 CDP，回退独立 Helper")

        # 3) 裸 Helper 直连。
        # 主 App 正在运行但没开调试口时（open -a 对已运行实例只激活、不传参），
        # 绝不能 pkill 用户正在用的豆包 App，也绝不另起 Helper 去抢它的单例锁
        # —— 明确报错让用户决策。
        if await asyncio.to_thread(self._main_app_running):
            raise RuntimeError(
                "豆包主 App 正在运行但未开启 CDP 调试端口（无法给已运行的实例"
                "追加启动参数）。请先完全退出豆包（Cmd+Q）后重试；代理不会强杀"
                "正在使用的豆包 App。"
            )

        # App 身份启动失败：先停掉残留进程，否则单例锁仍被占着。
        await asyncio.to_thread(self._kill_app_processes)
        await asyncio.sleep(2)
        cmd = [self.helper_bin, f"--remote-debugging-port={self.port}", *_STABLE_FLAGS]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        log.info("CDPDoubaoClient: Helper started pid=%s port=%d", self._proc.pid, self.port)
        await self._connect_chat()

    # ------------------------------------------------------------------
    # 内部：App 进程探测 / 清理
    # ------------------------------------------------------------------

    def _require_main_app_installed(self) -> None:
        """App 未安装时 Helper（在 App bundle 内）必然也缺失，提前给出可操作
        提示，而不是白等 30s 后抛难以理解的 FileNotFoundError。"""
        if not _main_app_installed():
            raise RuntimeError(
                "未检测到豆包工作 App（DoubaoWork.app 不存在）。"
                "请先安装豆包工作并完成登录后重试。"
            )

    def _launch_main_app(self) -> bool:
        """``open -a DoubaoWork --args --remote-debugging-port=<port>``。

        返回是否可能拉起成功；``open`` 自身报错（App 未注册等）时为 False。
        """
        try:
            r = subprocess.run(
                ["open", "-a", "DoubaoWork", "--args",
                 f"--remote-debugging-port={self.port}"],
                capture_output=True,
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001 - 任何拉起异常都走兜底
            log.warning("CDPDoubaoClient: 主 App 拉起异常（%s）", e)
            return False
        if r.returncode != 0:
            detail = (r.stderr or b"").decode("utf-8", "replace").strip()
            log.warning("CDPDoubaoClient: 主 App 拉起失败（%s）", detail or r.returncode)
            return False
        return True

    async def _wait_cdp_ready(self, timeout: float = _MAIN_APP_CDP_WAIT_S) -> bool:
        """等 App 把调试端口拉起来（App 冷启动要先拉起内嵌 Chromium）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(1)
            if await asyncio.to_thread(self._probe_cdp):
                return True
        return False

    @staticmethod
    def _main_app_running() -> bool:
        """豆包主 App 进程是否在运行（pgrep 不可用时按「在跑」处理，宁可不启动
        也不抢用户 App 的锁）。"""
        try:
            return subprocess.run(
                ["pgrep", "-f", APP_BIN_PATTERN],
                capture_output=True,
            ).returncode == 0
        except OSError:
            return True

    @staticmethod
    def _kill_app_processes() -> None:
        """清理 App 身份启动残留的 Helper / 主 App 进程（精确路径匹配）。"""
        try:
            subprocess.run(
                ["pkill", "-KILL", "-f", _APP_PROCESS_PATTERN], check=False
            )
        except OSError:
            pass

    async def stop(self) -> None:
        if self._ws:
            self._ws.close()
            self._ws = None
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        self._ready = False

    async def is_alive(self) -> bool:
        return self._ready and self._proc is not None and self._proc.poll() is None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    # ------------------------------------------------------------------
    # 内部：CDP 辅助
    # ------------------------------------------------------------------

    def _switch_to_target_by_id(self, target_id: str) -> None:
        """切换 WebSocket 连接到指定 target（Target.createTarget 后重连）。

        createTarget 返回的 targetId 不带 webSocketDebuggerUrl，需要从
        /json/list 里按 id 匹配再重连。
        """
        deadline = time.time() + 20
        while time.time() < deadline:
            for t in self._fetch_targets():
                if t.get("id") == target_id and t.get("webSocketDebuggerUrl"):
                    ws_url = t["webSocketDebuggerUrl"]
                    path = ws_url.split(f":{self.port}", 1)[1]
                    new_ws = _WS("127.0.0.1", self.port, path)
                    old = self._ws
                    self._ws = new_ws
                    if old:
                        try:
                            old.close()
                        except Exception:
                            pass
                    return
            time.sleep(0.3)
        raise RuntimeError(f"new tab target {target_id} not found in /json/list")

    def _evaluate(self, expr: str, timeout: float = 30.0, await_promise: bool = True) -> Any:
        """同步执行 Runtime.evaluate（返回 JS 值或错误标记）。

        经 ``_ws_lock`` 串行化：并发请求（to_thread 调用）不会交叉读写
        同一 WebSocket，避免 CDP 响应 id 串号。
        """
        if not self._ws:
            raise RuntimeError("CDP not connected")
        with self._ws_lock:
            r = self._ws.cmd(
                "Runtime.evaluate",
                {"expression": expr, "returnByValue": True, "awaitPromise": await_promise},
                timeout=timeout,
            )
        result = r.get("result", {})
        if "exceptionDetails" in result:
            desc = (
                result["exceptionDetails"].get("exception", {}).get("description")
                or result["exceptionDetails"].get("text")
            )
            return {"ERROR": str(desc)[:500]}
        return result.get("result", {}).get("value")

    def evaluate(self, expr: str, timeout: float = 20.0) -> Any:
        """公开的页面求值入口（签到等非对话用途用，同步，由调用方线程化）。

        与 ``_evaluate`` 同一实现、同一把 ``_ws_lock``：对话流与签到请求不会
        交叉读写同一条 WebSocket。
        """
        return self._evaluate(expr, timeout=timeout)

    def _fetch_targets(self) -> list[dict[str, Any]]:
        """同步拉取 /json/list（失败返回空列表）。"""
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/list", timeout=2
            ) as resp:
                return json.loads(resp.read())
        except Exception:
            return []

    async def _wait_for_target(self, timeout: float = 60.0) -> dict[str, Any] | None:
        """找可用的 chat target。

        优先级：
        1. App 自己的聊天窗口（doubaowork-chat/chat）—— App 同款上下文，
           bdms/web_id/登录态齐全，工具可用（2026-09-09 实测）；
        2. 豆包域名页面（主 App 复用场景下已存在）；
        3. 宽限 5s 后返回任意 type=page（独立 Helper 的占位页），由
           _connect_chat 新开独立标签页，绝不导航 App 内部页。
        """
        deadline = time.time() + timeout
        grace = time.time() + 5  # 最多等 5s 让聊天页出现
        fallback: dict[str, Any] | None = None
        while time.time() < deadline:
            web_page: dict[str, Any] | None = None
            for t in await asyncio.to_thread(self._fetch_targets):
                url = t.get("url", "")
                if not t.get("webSocketDebuggerUrl") or t.get("type") != "page":
                    # 只认真正的页面。iframe（drive-iframe 等）虽在豆包域名
                    # 下，但 localStorage 里没有 web_id、bdms 上下文也不对，
                    # 连上后请求会 710020202。
                    continue
                if _is_app_chat_url(url):
                    return t
                if web_page is None and "doubao.com" in url:
                    web_page = t
                if fallback is None:
                    fallback = t
            if web_page is not None:
                return web_page
            await asyncio.sleep(0.3)
            if fallback is not None and time.time() > grace:
                return fallback
        return fallback

    async def _wait_for_bdms(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = await asyncio.to_thread(self._evaluate, "typeof window.bdms")
            if r == "object":
                return
            await asyncio.sleep(0.5)
        log.warning("bdms not ready after %ss", timeout)

    async def _extract_web_id(self) -> str:
        expr = (
            "(() => { try { const v = JSON.parse(localStorage.getItem('samantha_web_web_id') || '{}');"
            " return v.web_id || ''; } catch(e) { return ''; } })()"
        )
        r = await asyncio.to_thread(self._evaluate, expr)
        return r if isinstance(r, str) else ""

    def _build_query_params(self) -> dict[str, str]:
        return {
            "aid": "1044603",
            "device_platform": "web",
            "doubao_device_platform": "desktop",
            "web_id": self._web_id or "",
            "version_code": "20800",
            "language": "zh",
            "real_aid": "1044603",
        }

    # ------------------------------------------------------------------
    # 登录辅助
    # ------------------------------------------------------------------

    async def wait_for_login(self, timeout: int = 120) -> bool:
        """检查服务端登录态；``timeout`` 只在未就绪时用于启动等待。

        **必须问服务端，不能看 ``localStorage.flow_web_has_login``**：那是前端
        缓存，服务端会话过期后它仍是 ``true``，于是签到面板一直「状态未知」、
        调用方以为已登录。问法是对 ``/api/chat/completion/chat`` 空 POST 看
        401（与 ``doubao/checkin.py`` 的 ``login_state`` 同一判据）。

        只在明确「非 401 且拿到状态码」时才 True；探针本身没打通（求值失败 /
        状态码 0）按未登录处理——宁可让调用方重试，也不把「探不到」冒充「已登录」。
        """
        if not self._ready:
            await self.start()
        url = f"https://www.doubao.com/api/chat/completion/chat?" + "&".join(
            f"{k}={v}" for k, v in sorted(self._build_query_params().items())
        )
        js = f"""
        (async () => {{
          try {{
            const r = await fetch({url!r}, {{
              method: 'POST', credentials: 'include',
              headers: {{'Content-Type': 'application/json'}}, body: '{{}}',
            }});
            return r.status;
          }} catch (e) {{ return 0; }}
        }})()
        """
        status = await asyncio.to_thread(self._evaluate, js, 20.0)
        if status == 401:
            log.warning("CDPDoubaoClient: 豆包服务端登录态无效（401），需要重新扫码登录")
            return False
        if not isinstance(status, int) or status == 0:
            log.warning("CDPDoubaoClient: 豆包登录态探针未打通，按未登录处理")
            return False
        return True

    def login_state_sync(self, timeout: float = 15.0) -> bool | None:
        """同步版 :meth:`wait_for_login`：``True`` 已登录 / ``False`` 明确未登录 /
        ``None`` 没探到（求值失败或页面回不了状态码）。

        签到在 ``asyncio.to_thread`` 的同步路径里跑，不能再套一层 ``asyncio.run``
        等登录；这里直接求值。
        """
        if not self._ready:
            return None
        url = f"https://www.doubao.com/api/chat/completion/chat?" + "&".join(
            f"{k}={v}" for k, v in sorted(self._build_query_params().items())
        )
        js = f"""
        (async () => {{
          try {{
            const r = await fetch({url!r}, {{
              method: 'POST', credentials: 'include',
              headers: {{'Content-Type': 'application/json'}}, body: '{{}}',
            }});
            return r.status;
          }} catch (e) {{ return 0; }}
        }})()
        """
        try:
            status = self._evaluate(js, timeout)
        except Exception:  # noqa: BLE001 - 探针失败按「没探到」处理
            return None
        if status == 401:
            log.warning("CDPDoubaoClient: 豆包服务端登录态无效（401），需要重新扫码登录")
            return False
        if not isinstance(status, int) or status == 0:
            return None
        return True

    # ------------------------------------------------------------------
    # 核心：chat_completion（对齐 BrowserClient 接口）
    # ------------------------------------------------------------------

    async def chat_completion(
        self,
        text: str,
        conversation_id: Optional[str] = None,
        bot_id: Optional[str] = None,
        use_deep_think: int = 0,
        model_spec: Optional[dict[str, Any]] = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """发送消息并 yield 解析后的 SSE 事件（与 BrowserClient 格式一致）。

        ``model_spec`` 非 None 时走 **agent 管线**（App 选模型时的同款协议：
        ``agent_mode=1`` + ``model_config.model_item_key`` + ``aggregate_params``
        + ``general_task_param``），可路由到 App 模型菜单里的具体模型
        （Turbo/Pro/Orange/Gemini/GPT）。经典管线（无 model_spec）会**忽略**
        模型字段，永远路由到服务端默认豆包模型（2026-09 实测）。

        model_spec 字段：
        - ``item_key``: 模型菜单的 model_item_key（str，如 "4"=Turbo）
        - ``extra``: model_extra_params（如 {"total_window_size": "256000"}）
        - ``provider``: provider_id（一方模型空串，Gemini/GPT 为 "cis"）
        - ``reasoning_effort``: 推理强度（3低/4中/5高/6极高/7最高，默认 5）
        """
        if not self._ready:
            raise RuntimeError("CDP client not ready")

        need_create = conversation_id is None or conversation_id == ""
        effective_bot_id = bot_id or DEFAULT_BOT_ID
        now_ms = int(time.time() * 1000)
        now_sec = int(time.time())

        if model_spec is not None:
            payload = _build_agent_payload(
                text, model_spec, need_create, effective_bot_id,
                conversation_id or "", now_ms, now_sec,
            )
        else:
            payload = _build_classic_payload(
                text, use_deep_think, need_create, effective_bot_id,
                conversation_id or "", now_ms, now_sec,
            )

        query = self._build_query_params()
        query_string = "&".join(f"{k}={v}" for k, v in sorted(query.items()))
        url = f"https://www.doubao.com/chat/completion?{query_string}"

        log.info("CDP POST /chat/completion (conv=%s, deep_think=%s, model=%s)",
                 conversation_id or "new", use_deep_think,
                 model_spec.get("item_key") if model_spec else "classic")

        # 流式：JS 读 SSE 逐块 console.log，Python 监听 consoleAPICalled
        async for event in self._stream_fetch(url, payload):
            yield event

    def _build_classic_payload(
        self,
        text: str,
        use_deep_think: int,
        need_create: bool,
        bot_id: str,
        conversation_id: str,
        now_ms: int,
        now_sec: int,
    ) -> dict[str, Any]:
        """委托 ``payloads._build_classic_payload``（2026-10 拆分，保留方法形状）。"""
        return _build_classic_payload(
            text, use_deep_think, need_create, bot_id, conversation_id, now_ms, now_sec)

    def _build_agent_payload(
        self,
        text: str,
        model_spec: dict[str, Any],
        need_create: bool,
        bot_id: str,
        conversation_id: str,
        now_ms: int,
        now_sec: int,
    ) -> dict[str, Any]:
        """委托 ``payloads._build_agent_payload``（2026-10 拆分，保留方法形状）。"""
        return _build_agent_payload(
            text, model_spec, need_create, bot_id, conversation_id, now_ms, now_sec)

    async def _stream_fetch(
        self, url: str, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any], None]:
        """在页面 JS 里 fetch 并流式读取 SSE，逐块回传。

        用「window 队列 + evaluate 轮询」作为桥：
        - JS 在后台 async IIFE 里 fetch，逐块解析 SSE，把事件 push 到
          ``window.__dwChunks`` 数组（元素是 JSON 字符串）。
        - Python 循环 ``Runtime.evaluate`` 读取 ``shift()`` 取走事件。

        不用 console.log 桥——实测该 Helper 不派发 consoleAPICalled 事件。
        关键：``awaitPromise=False`` 启动的 async IIFE 会在后台持续执行
        （已实测验证），fetch 不受 evaluate 返回影响。
        """
        if not self._ws:
            raise RuntimeError("CDP not connected")

        queue_name = f"__dwChunks_{uuid.uuid4().hex[:10]}"
        payload_json = json.dumps(payload, ensure_ascii=False)

        js = f"""
        (async () => {{
          const q = {json.dumps(queue_name)};
          window[q] = [];
          try {{
            const res = await fetch({json.dumps(url)}, {{
              method: 'POST',
              credentials: 'include',
              headers: {{'Content-Type': 'application/json'}},
              body: {json.dumps(payload_json)},
            }});
            window[q].push('__HTTP_STATUS__:' + res.status);
            if (!res.ok) {{
              const errBody = await res.text();
              window[q].push('__HTTP_ERROR__:' + res.status + ':' + errBody.slice(0, 500));
              window[q].push('__DONE__');
              return;
            }}
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let currentEvent = '';
            let buffer = '';
            while (true) {{
              const {{done, value}} = await reader.read();
              if (done) break;
              buffer += decoder.decode(value, {{stream: true}});
              const lines = buffer.split('\\n');
              buffer = lines.pop();
              for (const line of lines) {{
                const t = line.trim();
                if (!t) continue;
                if (t.startsWith('event: ')) {{ currentEvent = t.slice(7); continue; }}
                if (t.startsWith('id: ')) continue;
                if (!t.startsWith('data: ')) continue;
                const ds = t.slice(6);
                if (!ds || ds === '{{}}') continue;
                try {{
                  const obj = JSON.parse(ds);
                  obj._event = currentEvent;
                  window[q].push(JSON.stringify(obj));
                }} catch(e) {{}}
              }}
            }}
            if (buffer.trim()) {{
              const t = buffer.trim();
              if (t.startsWith('data: ')) {{
                const ds = t.slice(6);
                if (ds && ds !== '{{}}') {{
                  try {{
                    const obj = JSON.parse(ds);
                    obj._event = currentEvent;
                    window[q].push(JSON.stringify(obj));
                  }} catch(e) {{}}
                }}
              }}
            }}
            window[q].push('__DONE__');
          }} catch(e) {{
            window[q].push('__ERROR__:' + e.message);
          }}
        }})()
        """

        # 启动后台 JS（awaitPromise=False，后台持续跑；经 _ws_lock 串行化，
        # 不阻塞事件循环）
        await asyncio.to_thread(self._evaluate, js, 10.0, False)

        # 轮询队列：每次取走一批事件
        drain_js = (
            f"(() => {{ const q = window[{json.dumps(queue_name)}];"
            f" if (!q || q.length === 0) return '[]';"
            f" const batch = q.splice(0, q.length);"
            f" return JSON.stringify(batch); }})()"
        )
        # 空闲超时：以「最近一次收到数据」计，长回复（>180s 仍在出数据）不会被误杀
        idle_timeout = 180.0
        last_activity = time.time()
        while time.time() - last_activity < idle_timeout:
            await asyncio.sleep(0.15)
            r = await asyncio.to_thread(self._evaluate, drain_js, 10.0)
            if not isinstance(r, str):
                continue
            try:
                batch = json.loads(r)
            except json.JSONDecodeError:
                continue
            if batch:
                last_activity = time.time()
            for item in batch:
                if item == "__DONE__":
                    await self._cleanup_queue(queue_name)
                    return
                if item.startswith("__ERROR__:"):
                    yield {"error": True, "status": 0, "body": item[len("__ERROR__:"):]}
                    await self._cleanup_queue(queue_name)
                    return
                if item.startswith("__HTTP_ERROR__:"):
                    rest = item[len("__HTTP_ERROR__:"):]
                    status = int(rest.split(":", 1)[0])
                    body = rest.split(":", 1)[1] if ":" in rest else ""
                    yield {"error": True, "status": status, "body": body}
                    await self._cleanup_queue(queue_name)
                    return
                if item.startswith("__HTTP_STATUS__:"):
                    continue
                try:
                    obj = json.loads(item)
                    yield obj
                except json.JSONDecodeError:
                    continue
        # 超时
        await self._cleanup_queue(queue_name)
        yield {"error": True, "status": 0, "body": f"Stream idle timeout ({idle_timeout:.0f}s)"}

    async def _cleanup_queue(self, queue_name: str) -> None:
        """删除页面侧轮询队列，避免长会话下浏览器内存无界增长。"""
        try:
            await asyncio.to_thread(
                self._evaluate,
                f"delete window[{json.dumps(queue_name)}]",
                5.0,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # SSE 解析辅助（与 BrowserClient 对齐）
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(event: dict[str, Any]) -> str:
        """从 SSE 事件提取文本（复用 BrowserClient 逻辑）。"""
        event_type = event.get("_event", "")

        if event_type == "CHUNK_DELTA" and isinstance(event.get("text"), str):
            return event["text"]

        if "patch_op" in event:
            for op in event["patch_op"]:
                pv = op.get("patch_value", {})
                for block in pv.get("content_block", []):
                    tb = block.get("content", {}).get("text_block", {})
                    if tb.get("text"):
                        return tb["text"]
                if op.get("patch_object") == 102:
                    raw = pv.get("content", "")
                    if raw:
                        try:
                            parsed = json.loads(raw)
                            if parsed.get("text"):
                                return parsed["text"]
                        except (json.JSONDecodeError, TypeError):
                            pass

        if event_type == "STREAM_MSG_NOTIFY":
            content = event.get("content", {})
            if isinstance(content, dict):
                for block in content.get("content_block", []):
                    tb = block.get("content", {}).get("text_block", {})
                    if tb.get("text"):
                        return tb["text"]

        return ""

    @staticmethod
    def extract_conversation_id(event: dict[str, Any]) -> Optional[str]:
        ack = event.get("ack_client_meta", {})
        if ack.get("conversation_id"):
            return ack["conversation_id"]
        meta = event.get("meta", {})
        if meta.get("conversation_id"):
            return meta["conversation_id"]
        return None
