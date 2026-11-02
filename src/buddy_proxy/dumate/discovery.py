"""DuMate 本地主进程的发现与凭证抽取。

百度搭子（DuMate.app）启动后会拉起一个本地 ``dumate-main-server`` 进程
（Go 二进制），监听 ``127.0.0.1:<port>``，对外暴露 OpenAI 兼容代理
``/api/qianfanproxy/v1``。本模块负责：

1. 找到正在运行的 ``dumate-main-server`` 进程（按可执行文件名匹配）；
2. 从其命令行与环境变量里抽出三样东西——监听端口（``--port``）、
   进程号（用于 ``ps eww`` 读环境）、以及本地鉴权 key（``DUMATE_INAPP_KEY``）。

为什么 key 要从**进程环境**读：``X-Dumate-Inapp-Key`` 是 Electron 主进程
``crypto.randomBytes(32)`` 每次启动现生成的（见 DuMate ``lib/crypto/inapp-key.js``），
通过 ``DUMATE_INAPP_KEY`` env 传给所有子进程。它不落盘、不进配置文件，所以
唯一稳定的取法就是读运行中进程的环境。key 每次 App 重启都会轮换。

安全：inapp key 只在本机回环上用，且我们**只从本机进程读、绝不用来访问外部
网络**；读取后只保留在内存，不进日志（打日志时一律脱敏成前 6 位 + 长度）。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: 主进程可执行文件名（不含目录）。匹配命令行里的 ``dumate-main-server``。
_MAIN_SERVER_BIN = "dumate-main-server"

#: 本地代理路由前缀（OpenAI 兼容 chat completions）。
PROXY_CHAT_PATH = "/api/qianfanproxy/v1/chat/completions"

#: 额度探测路由（返回 {"hasRemainingPoints": bool}）。
POINTS_REMAINING_PATH = "/api/dumate/points/remaining"

#: 本地鉴权 header 名。
INAPP_HEADER = "X-Dumate-Inapp-Key"

#: env 变量名（子进程经它共享 inapp key）。
_INAPP_ENV = "DUMATE_INAPP_KEY"


@dataclass
class DumateEndpoint:
    """一个已发现的 DuMate 本地端点。"""

    pid: int            #: dumate-main-server 进程号
    port: int           #: 本地监听端口（--port）
    inapp_key: str      #: X-Dumate-Inapp-Key 值（64 位 hex）
    app_version: str = ""  #: DUMATE_APP_VER（如 1.0.82.317），诊断用

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def chat_url(self) -> str:
        return f"{self.base_url}{PROXY_CHAT_PATH}"

    def points_url(self) -> str:
        return f"{self.base_url}{POINTS_REMAINING_PATH}"

    def headers(self) -> dict[str, str]:
        return {INAPP_HEADER: self.inapp_key}


def _parse_port(cmdline: str) -> int | None:
    """从命令行 ``--port=52414`` 或 ``--port 52414`` 抽监听端口。"""
    m = re.search(r"--port[= ](\d+)", cmdline)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _find_main_server_pid() -> tuple[int, str] | None:
    """按可执行文件名找运行中的 ``dumate-main-server``，返回 (pid, cmdline)。"""
    try:
        # -l 带 cmdline（macOS 的 pgrep 没有 -a，-f 单独用会被截断输出）。
        # 输出形如 "9007 /Applications/DuMate.app/.../dumate-main-server -c ..."。
        out = subprocess.run(
            ["pgrep", "-lf", _MAIN_SERVER_BIN],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("dumate pgrep failed: %s", exc)
        return None
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # pgrep -af 输出形如 "9007 /Applications/DuMate.app/.../dumate-main-server -c ..."
        parts = line.split(" ", 1)
        try:
            pid = int(parts[0])
        except (ValueError, IndexError):
            continue
        cmdline = parts[1] if len(parts) > 1 else ""
        # 排除 pgrep 自身（`pgrep -lf dumate-main-server` 的 cmdline 也含
        # 模式串，竞态时会先出现把自己匹配进去）/ 其它误匹配：要求命令是
        # 真实可执行文件路径里的 dumate-main-server，而不是参数里提到它。
        exe = cmdline.split(" ", 1)[0]
        if exe.endswith("/" + _MAIN_SERVER_BIN) or exe.endswith(_MAIN_SERVER_BIN):
            return pid, cmdline
    return None


def _read_inapp_key(pid: int) -> tuple[str, str]:
    """读 ``dumate-main-server`` 进程环境，返回 (inapp_key, app_version)。

    用 ``ps eww <pid>``（macOS/BSD 语法）拿完整环境串，再按空白切分。key 与版本
    只在本机内存用，绝不写日志原文。
    """
    try:
        out = subprocess.run(
            ["ps", "eww", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("dumate ps eww failed: %s", exc)
        return "", ""
    text = out.stdout or ""
    inapp, ver = "", ""
    for tok in text.split():
        if tok.startswith(_INAPP_ENV + "="):
            inapp = tok.split("=", 1)[1].strip()
        elif tok.startswith("DUMATE_APP_VER="):
            ver = tok.split("=", 1)[1].strip()
    return inapp, ver


def discover() -> DumateEndpoint | None:
    """发现运行中的 DuMate 本地端点；没找到 / 缺凭证时返回 None。

    这是**纯只读探测**：只跑 pgrep / ps，不发起任何网络请求，可在事件循环里
    安全调用（开销毫秒级）。真正发请求前的连通性由 :func:`probe` 确认。
    """
    found = _find_main_server_pid()
    if not found:
        return None
    pid, cmdline = found
    port = _parse_port(cmdline)
    if not port:
        log.debug("dumate-main-server pid=%d 缺少 --port，无法定位本地代理", pid)
        return None
    inapp_key, ver = _read_inapp_key(pid)
    if not inapp_key:
        log.debug("dumate-main-server pid=%d 读不到 %s", pid, _INAPP_ENV)
        return None
    return DumateEndpoint(pid=pid, port=port, inapp_key=inapp_key, app_version=ver)


def is_app_installed() -> bool:
    """本机是否装了 DuMate.app（用于「没运行」时给出可操作的提示）。

    安装位置候选复用 ``standalone._APP_CANDIDATES``——两处各写一份的话，将来
    加个安装位置只改一处就会出现「提示已安装、实际拉不起来」的不一致。
    """
    from . import standalone

    for cand in standalone._APP_CANDIDATES:  # noqa: SLF001 - 共享同一份候选常量
        if os.path.isdir(cand):
            return True
    return False


def can_auto_start() -> bool:
    """未运行状态下，本机是否具备「首次转发自动拉起独立实例」的条件。

    三个条件缺一不可：兜底开关没关（``standalone.is_enabled``）、App 包内
    二进制/配置齐全（``standalone_supported``）、``auth.json`` 里有登录标识。
    :func:`describe_state` 与 provider 的 ``ensure_auth`` / ``forward`` 共用这一处
    判据——早先各写一遍，面板与转发行为迟早会说法不一。
    """
    from . import standalone

    return (standalone.is_enabled() and standalone.standalone_supported()
            and bool(standalone.read_login_context().get("user_id")))


def describe_state() -> dict[str, Any]:
    """返回一个人类可读的发现状态摘要（供 /health、鉴权面板、日志用）。

    只读，不触网。``ready`` 为 True 表示现在就能转发（进程在跑 + 端口 + key 齐）。
    """
    ep = discover()
    if ep is not None:
        return {
            "ready": True,
            "installed": True,
            "running": True,
            "auto_start": False,  # 已经在跑，用不上兜底拉起
            "port": ep.port,
            "pid": ep.pid,
            "app_version": ep.app_version,
            "inapp_key_hint": f"{ep.inapp_key[:6]}…(len{len(ep.inapp_key)})",
        }
    installed = is_app_installed()
    auto = False
    # 装了 App 但没在跑：若本机具备独立拉起条件（App 包内二进制齐全 + 已登录），
    # 首次转发会自动 spawn 一个独立 dumate-main-server，**无需**用户手动开桌面端。
    # 提示必须如实反映这一点，否则面板让用户「先打开 App」，而实际请求根本不用开
    # （用户 2026-11-01：不该必须开着桌面端）。
    if installed:
        auto = can_auto_start()
        if auto:
            hint = "已安装 DuMate.app，当前未运行——首次转发时代理会自动拉起独立实例，无需手动打开。"
        else:
            hint = "已安装 DuMate.app，但当前未在运行——请先打开百度搭子桌面端。"
    else:
        hint = "未检测到 DuMate.app。请先安装并登录百度搭子（千帆桌面端）。"
    state: dict[str, Any] = {
        "ready": False,
        "installed": installed,
        "running": False,
        # 能自动拉起 ≠ 已就绪：这里仍是要等首次转发的「待命」态。前端据此把
        # 圆点画成琥珀而不是红色报警，并把话说明白（用户 2026-11-02 看到红点
        # 「App 未运行」以为通道坏了，其实转发根本不用开桌面端）。
        "auto_start": auto,
        "port": None,
        "pid": None,
        "app_version": "",
        "inapp_key_hint": "",
        "hint": hint,
    }
    # 进程没在跑 ≠ 不知道是谁。额度/签到走的是本机 bceConsole 登录态（auth.json
    # 里那套），与 dumate-main-server 在不在毫无关系——面板不认出账号，卡片标题
    # 就退化成「本机账号」、也不显示「已登录」（用户 2026-11-02 报障：没开 App
    # 时额度区整块是「App 未运行」，看着像什么都查不到）。
    login = _login_identity()
    if login:
        state["account_id"] = login.get("user_id", "")
        state["display_name"] = login.get("display_name", "")
    return state


def _login_identity() -> dict[str, str]:
    """从 ``auth.json`` 读面板要用的账号身份（只读、失败返回空 dict）。

    ``user_id`` 与独立拉起用的是同一个 ``bceUserId``（面板拿它当 dumate 单账号的
    固定标识）；``display_name`` 取自当前 profile。不读 cookie、不触网。
    """
    from . import standalone
    from . import cookies as dumate_cookies

    ctx = standalone.read_login_context()
    if not ctx.get("user_id"):
        return {}
    out = {"user_id": ctx["user_id"], "display_name": ""}
    try:
        import json
        from pathlib import Path

        auth = json.loads((Path(dumate_cookies._user_data_dir()) / "auth.json")  # noqa: SLF001
                          .read_text("utf-8"))
        active = auth.get("activeProfileId")
        profiles = auth.get("accountProfiles") or []
        prof = next((p for p in profiles if p.get("profileId") == active), None)
        if prof is None and profiles:
            prof = profiles[0]
        if isinstance(prof, dict):
            out["display_name"] = str(prof.get("displayName") or "")
    except (OSError, ValueError):
        pass
    return out
