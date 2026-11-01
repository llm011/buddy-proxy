"""脱离 DuMate 桌面端独立运行本地代理（自己拉起 ``dumate-main-server``）。

背景（2026-11-01 用户报障）：关掉 DuMate.app 后，代理的 dumate 通道立刻 503
——因为转发依赖 App（Electron）拉起的 ``dumate-main-server`` 子进程。实测
（2026-11-01，DuMate 1.0.88）该 Go 二进制**可以完全脱离 Electron 独立启动**：

- 最小 env 只要 ``DUMATE_LOGIN_MODE`` + ``DUMATE_LOGIN_USER_ID`` +
  ``DUMATE_LOGIN_BCE_ACCOUNT_ID`` + 自设的 ``DUMATE_INAPP_KEY``，加
  ``--user-id``；不传 opencode/router 相关参数（Electron 才提供那些）时只是
  打几条 WARN（opencode 127.0.0.1:4096 连不上），**不影响转发**。
- 转发接口 ``/api/qianfanproxy/v1/chat/completions`` **压根不校验 inapp key**
  （错误 key 也 200 正常回包）；只有 ``/api/dumate/points/*``（额度/签到兜底）
  校验它（错 key 401）。所以 key 我们自生成一把即可，只在本机回环用。
- 与 App 并存不冲突：端口各用各的（我们 bind(0) 取空闲端口），设备指纹每进程
  现生成、无共享持久化。

于是本模块在 ``discovery.discover()`` 发现不到 App 的实例时，自己拉起一个：
登录标识从 ``auth.json`` 现读（复用 ``cookies._user_data_dir`` 的路径解析），
inapp key 用 ``secrets`` 现生成。用户不再需要为了跑代理而开着桌面端。

安全（与 cookies.py 同级约束）：读到的登录标识（bceUserId/bceAccountId）只用于
本机 spawn 子进程，**绝不写日志原文、绝不外发**；inapp key 自生成、只存本进程
内存与子进程 env，进程退出即失效。

生命周期：

- 默认**会话内**：随本进程退出而退出——``__main__`` 的关停钩子调
  :func:`shutdown_standalone`。**实现靠 stdin 连命符**：子进程自带「stdin
  读到 EOF 就自杀」的看门狗，我们给它一根父进程攥着写端的管道，父进程一死
  写端即关、子进程立刻干净退场（详见 :func:`spawn`）。父进程被 ``kill -9``
  也一样——写端随内核关 fd 而消失，不再产生孤儿（旧版给 ``DEVNULL`` 的老
  实现反而会让子进程 2s 后自杀，见 :func:`spawn` 的注释）。
- 可选**常驻**：``BUDDY_DUMATE_STANDALONE=persist`` 时不随会话退出清理；由
  用户/launchd 管理。子进程自己抱着那根管道的写端，我们死了它也不 EOF。想继续
  复用它，直接让 ``discovery`` 从 pgrep 里发现即可（与我们无关的独立实例，同样
  能命中）。
- **孤儿回收**：子进程用 ``start_new_session=True`` 起（前台进程组的 Ctrl-C
  不该连带杀掉正在转发的子进程），遗留实例仍按 owner pid 登记 ``state_dir()/
  dumate-standalone/<owner>.json``，下次 :func:`ensure_running` 前
  :func:`reap_orphans` 扫一遍：owner 已死且非 persist 的遗留实例回收掉。有了
  stdin 连命符后非 persist 的孤儿基本不再出现，但 persist 的遗留、或旧版本
  留下的登记仍可能命中，回收逻辑照旧保留。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 二进制与配置文件相对 App 资源根的位置（与 Electron ``app.config.js`` 一致）。
_BIN_REL = ("extra-resource", "backend", "bin", "dumate-main-server")
_CONFIG_REL = ("config", "desktop-main", "config.yml")

#: App 安装位置候选（**单一事实源**：discovery.is_app_installed 也复用这份，
#: 免得两处各写一份、加个安装位置只改一处就出现「installed=True 却拉不起来」）。
_APP_CANDIDATES = ("/Applications/DuMate.app", os.path.expanduser("~/Applications/DuMate.app"))

#: spawn 后等待 /health 就绪的上限（秒）。实测冷启 ~1s 内 ok。
_READY_TIMEOUT_S = 15.0

#: 本地鉴权 header 名（与 discovery.INAPP_HEADER 同口径，probe /health 要用）。
_INAPP_HEADER = "X-Dumate-Inapp-Key"

#: 会话内拉起的子进程句柄（退出时清理用）。保留最后一个。
_child: subprocess.Popen | None = None
_child_lock = threading.Lock()
#: 串行化「发现→拉起」这段临界区（与 _child_lock 分开，避免重入死锁）。
_spawn_lock = threading.Lock()

#: 本进程自设的 inapp key（每个代理进程一把，随进程存亡）。
_inapp_key: str | None = None

#: 会话内拉起时父进程攥着的 stdin 写端 fd——子进程的「连命符」：它一关，
#: 子进程的 stdin 就读到 EOF 并干净退场（见 ``spawn`` 里的长注释）。同一时刻
#: 至多一个会话内子进程（``spawn`` 由 ``_spawn_lock`` 串行），故单个全局够用。
_stdin_hold_w: int | None = None


def app_resources_root() -> Path | None:
    """返回 DuMate.app 的 Resources 目录；未安装返回 None。"""
    for cand in _APP_CANDIDATES:
        res = Path(cand) / "Contents" / "Resources"
        if res.joinpath(*_BIN_REL).is_file():
            return res
    return None


def standalone_supported() -> bool:
    """本机是否具备独立运行条件（App 已安装且二进制/配置齐全）。"""
    root = app_resources_root()
    return root is not None and root.joinpath(*_CONFIG_REL).is_file()


def _mode() -> str:
    return os.environ.get("BUDDY_DUMATE_STANDALONE", "").strip().lower()


def is_enabled() -> bool:
    """是否允许自动兜底拉起（默认开；``off/0/false/no`` 关）。"""
    return _mode() not in ("off", "0", "false", "no")


def is_persistent() -> bool:
    """是否启用「常驻」模式（不随代理会话退出清理）。"""
    return _mode() == "persist"


def read_login_context() -> dict[str, str]:
    """从 ``auth.json`` 现读 spawn 所需的登录标识。

    返回 ``{login_mode, user_id, account_id}``（缺字段为空串）。口径与 Electron
    ``app.config.js`` 的 ``runtimeLoginContext`` 对齐：

    - ``loginMode`` ← ``activeProviderType``（``bce``=个人 / ``enterprise``=企业，
      如飞书）；
    - ``userId`` / ``bceAccountId`` ← 当前 profile（``activeProfileId`` 命中者）
      的 ``bceUserId`` / ``bceAccountId``（本机实测二者相等，但按字段各取，别假设）。
    """
    from . import cookies as dumate_cookies

    auth_path = Path(dumate_cookies._user_data_dir()) / "auth.json"  # noqa: SLF001
    empty = {"login_mode": "", "user_id": "", "account_id": ""}
    try:
        with open(auth_path, encoding="utf-8") as f:
            auth = json.load(f)
    except (OSError, ValueError):
        return empty
    profiles = auth.get("accountProfiles") or []
    active = auth.get("activeProfileId")
    prof: dict[str, Any] = {}
    for p in profiles:
        if p.get("profileId") == active:
            prof = p
            break
    if not prof and profiles:
        prof = profiles[0]
    return {
        "login_mode": str(auth.get("activeProviderType") or prof.get("loginType") or ""),
        "user_id": str(prof.get("bceUserId") or ""),
        "account_id": str(prof.get("bceAccountId") or ""),
    }


def _pick_port() -> int:
    """取一个当前空闲的本机端口（bind(0) 让内核分配，立刻释放后交给子进程）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _probe_health(port: int, key: str, timeout: float = 1.0) -> bool:
    """GET /health，2xx 即视为就绪。

    **必须带 inapp key**：服务端开了 ``InappKeyAuth`` 后 ``/health`` 也要鉴权，
    不带 header 一律 401（实测踩坑——探活误判成「未就绪」，spawn 15s 超时）。
    """
    url = f"http://127.0.0.1:{port}/health"
    req = urllib.request.Request(url, headers={_INAPP_HEADER: key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 仅本机回环
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def spawn(app_res_root: Path | None = None, *,
          login: dict[str, str] | None = None) -> dict[str, Any]:
    """拉起一个独立的 ``dumate-main-server``，返回 ``{ok, port, pid, reason}``。

    只负责启动 + 探活。凭证缺失（本机未登录）时返回 ``ok=False`` + 可读 reason，
    不抛异常。
    """
    global _child, _inapp_key

    root = app_res_root or app_resources_root()
    if root is None:
        return {"ok": False, "reason": "未找到 DuMate.app 或其后端二进制"}
    bin_path = root.joinpath(*_BIN_REL)
    cfg_path = root.joinpath(*_CONFIG_REL)
    ctx = login if login is not None else read_login_context()
    if not (ctx.get("login_mode") and ctx.get("user_id") and ctx.get("account_id")):
        return {"ok": False, "reason": "本机无 DuMate 登录态（auth.json 缺 activeProviderType/bceUserId）"}

    port = _pick_port()
    key = _inapp_key or secrets.token_hex(32)
    env = {
        **os.environ,
        "DUMATE_LOGIN_MODE": ctx["login_mode"],
        "DUMATE_LOGIN_USER_ID": ctx["user_id"],
        "DUMATE_LOGIN_BCE_ACCOUNT_ID": ctx["account_id"],
        "DUMATE_USER_ID": ctx["user_id"],
        "DUMATE_ACCOUNT_ID": ctx["account_id"],
        "DUMATE_INAPP_KEY": key,
        "DUMATE_DEPLOYMENT_MODE": os.environ.get("DUMATE_DEPLOYMENT_MODE", "public"),
    }
    args = [
        str(bin_path), "-c", str(cfg_path),
        "--hostname=127.0.0.1", f"--port={port}",
        f"--user-id={ctx['user_id']}",
    ]
    # 子进程默认 logdir "./log/" 相对 cwd——给它一个一定可写的自有目录，别把
    # 日志写进 App 的只读资源树或仓库目录。
    log_cwd = Path(tempfile.gettempdir()) / "buddy-dumate-standalone"
    try:
        log_cwd.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_cwd = Path(tempfile.gettempdir())

    # 子进程自带「父进程 stdin 关了就自杀」的看门狗（二进制里的
    # ``main.watchParentStdin`` / ``parent-stdin:eof``）：stdin 读到 EOF 就
    # 主动 Shutdown——这是它作为 Electron 子进程时的正常退出路径。
    #
    # **不能给它 ``DEVNULL``**：DEVNULL 是 `/dev/null`，读它下一秒就 EOF，
    # 子进程 ~2s 后就自己退场（实测）。而 spawn 的就绪探活在 ~0.8s 就返回
    # 200——于是 ``spawn()`` 报 ok、请求转过去时进程已经死了，回一个
    # ``Server disconnected without sending a response``（502
    # ``dumate upstream error``）。2026-11-01 用户报障的根因就是这个竞态。
    #
    # 正解是给它一根**父进程还攥着写端的管道**：
    # - 会话内（默认）：写端留在父进程（``_stdin_hold_w``）。父进程活着 → 写端
    #   不关 → 读端不 EOF → 子进程一直活；父进程正常退出或被 kill -9 → 写端
    #   随进程关闭 → 子进程立刻拿到 EOF 干净退场。「随会话退出」语义不打折，
    #   还免掉了孤儿。
    # - 常驻（persist）：把写端也交给子进程自己（``pass_fds``），父进程两侧全
    #   关——它自己抱着写端，读端永远不 EOF，即便我们死了它也照常跑。要停它
    #   直接 SIGTERM（见 ``_terminate_pid``）。
    _r, _w = os.pipe()
    persistent = is_persistent()
    try:
        proc = subprocess.Popen(  # noqa: S603 - 本机固定二进制
            args, env=env, cwd=str(log_cwd),
            stdin=_r,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=(_w,) if persistent else (),
        )
    except OSError as exc:
        _close_fd(_r)
        _close_fd(_w)
        return {"ok": False, "reason": f"拉起 dumate-main-server 失败: {exc.__class__.__name__}"}
    # 读端交给子进程后父进程不再需要它。
    _close_fd(_r)
    if persistent:
        # 常驻：子进程自己拿着写端，父进程这条多余的关掉。
        _close_fd(_w)
    else:
        # 会话内：父进程攥着写端——它就是我们与子进程的「连命符」。
        global _stdin_hold_w
        if _stdin_hold_w is not None:
            # 上一个会话内实例已死但写端没来得及收：先关掉，别一路漏 fd。
            _close_fd(_stdin_hold_w)
        _stdin_hold_w = _w

    deadline = time.monotonic() + _READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _release_stdin_hold()
            return {"ok": False, "reason": f"dumate-main-server 启动即退出（exit={proc.returncode}）"}
        if _probe_health(port, key):
            with _child_lock:
                _child = proc
                _inapp_key = key
            _record_spawned(proc.pid, port)
            log.info("dumate standalone spawned pid=%d port=%d", proc.pid, port)
            return {"ok": True, "port": port, "pid": proc.pid}
        time.sleep(0.3)
    _release_stdin_hold()
    _terminate(proc)
    return {"ok": False, "reason": f"dumate-main-server {_READY_TIMEOUT_S:.0f}s 内未就绪"}


def ensure_running() -> dict[str, Any] | None:
    """发现不到实例时兜底拉起一个；返回 spawn 结果（已存在可复用实例时返回 None）。

    串行化「发现 → 拉起」：并发请求只拉一个（第二个进来时 discover 已能看到
    第一个的结果）。
    """
    from . import discovery

    if discovery.discover() is not None:
        return None
    with _spawn_lock:
        if discovery.discover() is not None:
            return None
        # 拉起前先回收上次遗留的孤儿（owner 已死且非 persist 的），避免每轮
        # 不可控退场都白留一个常驻 Go 进程。
        try:
            reap_orphans()
        except Exception:  # noqa: BLE001 - 回收失败不该挡住本次拉起
            log.debug("dumate standalone reap failed", exc_info=True)
        return spawn()


def shutdown_standalone() -> None:
    """退出清理：默认终止本进程拉起的子进程；persist 模式保留它（幂等、不抛）。

    persist 下**不杀子进程、也不清 pid 文件**——这正是「常驻」的语义：让独立
    实例活下去，下次代理启动时 ``discovery`` 会从 pgrep 里发现并复用它（inapp
    key 从子进程 env 读，与本进程内存里的那份无关）。
    """
    global _child, _inapp_key, _stdin_hold_w
    with _child_lock:
        proc = _child
        _child = None
        _inapp_key = None
        hold = _stdin_hold_w
        _stdin_hold_w = None
    if is_persistent():
        return
    # 先关「连命符」写端：子进程的 stdin 立刻 EOF，它会自己干净退场；下面
    # 的 _terminate 只是加速与兜底（万一它卡在关停流程里）。
    if hold is not None:
        _close_fd(hold)
    if proc is not None:
        _terminate(proc)
    _clear_own_pid()


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM → 短暂等待 → SIGKILL，静默吞掉「已退出」竞态。"""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    except (OSError, subprocess.SubprocessError):
        pass


def _close_fd(fd: int) -> None:
    """关一个裸 fd，吞掉「已关/非法」的竞态。"""
    try:
        os.close(fd)
    except OSError:
        pass


def _release_stdin_hold() -> None:
    """放弃当前会话内子进程的 stdin 写端（失败路径收尾，避免漏 fd）。"""
    global _stdin_hold_w
    hold = _stdin_hold_w
    _stdin_hold_w = None
    if hold is not None:
        _close_fd(hold)


# --- 拉起的实例的登记与回收 ------------------------------------------------
#
# 为什么需要：子进程用 ``start_new_session=True`` 起（否则前台进程组的 Ctrl-C
# 会把正在转发的子进程一起杀掉）。代价是父进程被 ``kill -9`` 时——launchd 超时
# 兜底 / proxy.sh 的 pattern_kill 都会走这条路——``shutdown_standalone`` 的
# finally 不执行，子进程被 reparent 到 PID 1 成孤儿（实测 PPID 变 1 存活）。
#
# 孤儿本身还能被下次 ``discovery.discover()`` 按 pgrep 命中复用，不是永久故障；
# 但它再也无人回收。于是每个拉起实例按 owner（本代理进程）pid 登记一个文件，
# 下次启动时 ``reap_orphans`` 扫一遍：owner 已死且非 persist 的遗留实例回收掉，
# 让生命周期重新变确定（谁拉起、谁负责清）。


def _pid_dir() -> Path:
    from ..core.paths import state_dir

    return state_dir() / "dumate-standalone"


def _record_spawned(pid: int, port: int) -> None:
    """登记本进程拉起的实例（文件名=owner pid，多 worktree 并存不互相覆盖）。"""
    try:
        d = _pid_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{os.getpid()}.json"
        path.write_text(json.dumps({
            "pid": pid, "port": port,
            "persistent": is_persistent(),
            "owner": os.getpid(),
        }), encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass


def _clear_own_pid() -> None:
    try:
        (_pid_dir() / f"{os.getpid()}.json").unlink(missing_ok=True)
    except OSError:
        pass


def _is_main_server(pid: int) -> bool:
    """确认 pid 仍在跑、且确实是 dumate-main-server（避免 pid 复用误杀）。"""
    from . import discovery

    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    cmd = (out.stdout or "").strip()
    return bool(cmd) and cmd.split(" ", 1)[0].endswith(discovery._MAIN_SERVER_BIN)  # noqa: SLF001


def reap_orphans() -> int:
    """回收上次遗留的独立实例（owner 已死且非 persist 的），返回回收个数。

    安全性：只处理登记文件里 owner 已死的条目；杀之前再用 ``_is_main_server``
    确认目标仍是 dumate-main-server（防 pid 复用误伤）；persist 条目一律跳过
    ——那是「常驻」语义，归用户/launchd 管。任何异常都吞掉，回收绝不该反过来
    让代理起不来。
    """
    d = _pid_dir()
    if not d.is_dir():
        return 0
    reaped = 0
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            child = int(data.get("pid") or 0)
            owner = int(data.get("owner") or 0)
            if data.get("persistent"):
                continue  # 常驻：不动
            if owner and _owner_alive(owner):
                continue  # owner 还活着（另一个并存的代理实例）：不动
            if child and _is_main_server(child):
                _terminate_pid(child)
                reaped += 1
        except (OSError, ValueError):
            pass
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
    if reaped:
        log.info("dumate standalone reaped %d orphaned instance(s)", reaped)
    return reaped


def _owner_alive(pid: int) -> bool:
    """owner 进程是否还在（signal 0 探测；无权限也视为活着，别乱杀）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _terminate_pid(pid: int) -> None:
    """SIGTERM → 短暂等待 → SIGKILL（不持有进程句柄时按 pid 杀）。"""
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(6):
        time.sleep(0.2)
        if not _is_main_server(pid):
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass