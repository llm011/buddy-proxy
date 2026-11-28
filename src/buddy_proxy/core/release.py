"""版本发布与「有新版本」检测。

## 版本号从哪来

唯一事实来源是 ``pyproject.toml`` 的 ``version``。CI（``.github/workflows/release.yml``）
在每次 push 到 ``main`` 时自动把 patch 位 +1 并回提交，所以「一次合并 = 一个新版本」；
要发大版本时人工改 ``pyproject.toml``（或跑 ``uv version --bump minor``）即可覆盖默认节奏。

运行中的进程读的是**已安装**包的版本（``importlib.metadata``），而检测目标读的是
``origin/main`` 上的 ``pyproject.toml``——两者口径必须一致，否则会出现「本地明明已经
是最新代码，却提示有新版」。见 :func:`remote_version`。

## 为什么由服务端检测、而不是浏览器直连 GitHub

页面只允许访问本机（``/ui/api/*`` 本来就只对本机开放）。浏览器直连 GitHub 要跨墙，
国内网络下既慢又不可靠，还要处理 CORS 与凭据。改为服务端 ``git fetch``：

- 走的是本机已经配好的 SSH 凭据，无额外 token；
- 实测 ``git fetch origin main`` 3–5 秒（增量，无变化时也只是几秒），
  后台线程每 5 分钟跑一次，不占请求路径；
- 浏览器侧一次都不出网，只问 ``127.0.0.1``。

``fetch`` 失败（断网、墙、凭据过期）只是「本次检测不到」，不抛给页面——网络问题
不该让管理页报错，最多是提示延迟。
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import subprocess
import threading
import time
from typing import Any

from . import paths

_log = logging.getLogger(__name__)

#: 轮询间隔。用户明确要求「5 分钟一次」；也是 ``/ui/api/release`` 在无缓存时
#: 触发后台刷新的节流窗口——页面刷新再频繁也不会打穿到 git。
POLL_INTERVAL_S = 300.0

#: ``git`` 子命令超时。fetch 实测 3–5 秒，留足余量；超时按「本次失败」处理，
#: 不阻塞后续轮询（否则一次卡死的 fetch 会让检测永久停摆）。
_GIT_TIMEOUT_S = 30.0

#: 发布说明最多列几条。用户要求「一般写几点就行」——commit 多了页面会很长，
#: 超出部分折成「等 N 项改动」。
MAX_NOTES = 8

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _state_file() -> pathlib.Path:
    return paths.state_file("release_check.json")


def _run_git(*args: str) -> str:
    """跑一条只读 git 命令，返回 stdout；失败抛 ``RuntimeError``。

    ``check=False`` + 手动判断：``git`` 的失败原因在 stderr，要原样带出来才能
    区分「不是 git 仓库」和「远端不可达」。
    """
    try:
        proc = subprocess.run(  # noqa: S603 - 固定子命令，无用户输入拼接
            ["git", *args],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git {args[0]} 执行失败: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} 失败: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _read_version(text: str) -> str:
    """从 ``pyproject.toml`` 文本里取 ``version``（只认 ``[project]`` 段那个）。"""
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else ""


def installed_version() -> str:
    """当前**运行中**进程的版本（已安装包的元数据）。

    不能用 ``_REPO_ROOT/pyproject.toml``：服务可能跑在某个 worktree 里
    （``buddy-proxy-joycode`` 之类），那份 ``pyproject.toml`` 与真正 import 的
    包未必一致。``importlib.metadata`` 读的是实际安装进 venv 的版本，才代表
    「页面背后这个进程是什么版本」。
    """
    try:
        import importlib.metadata as md
        return md.version("buddy-proxy")
    except Exception:  # noqa: BLE001 - 开发态/未安装时元数据可能不存在
        return ""


def remote_version() -> str:
    """``origin/main`` 上的版本号（读远端 tree，不切分支、不动工作区）。

    用 ``git show origin/main:pyproject.toml`` 而不是「checkout 后读文件」：
    服务进程可能跑在带未提交改动的 worktree 里，任何 checkout 都可能毁掉
    用户正在改的东西。``git show`` 只读对象库，安全。
    """
    return _read_version(_run_git("show", "origin/main:pyproject.toml"))


def _version_tuple(v: str) -> tuple[int, ...]:
    """把 ``2.0.10`` 拆成 ``(2, 0, 10)`` 供比较。

    直接字符串比大小会得到 ``"2.0.10" < "2.0.9"``（字典序），patch 过 9 之后
    就再也检测不到新版本了。
    """
    out: list[int] = []
    for part in (v or "").split("."):
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out) or (0,)


def is_newer(candidate: str, base: str) -> bool:
    """``candidate`` 是否比 ``base`` 新（版本号比较，缺位补 0）。"""
    if not candidate:
        return False
    a, b = _version_tuple(candidate), _version_tuple(base)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def _since_ref(current: str) -> str:
    """算「我这一版之后有哪些改动」时的起点。

    优先用 ``v<当前版本>`` 标签：那才是「我装的这个版本」在主线上的位置。
    退而求其次用 ``HEAD``。

    为什么不能一律用 ``HEAD``：服务可能跑在某个 worktree 的功能分支上
    （``buddy-proxy-joycode`` 之类），那里的 ``HEAD`` 含着一堆还没合进 main 的
    提交，``HEAD..origin/main`` 算出来的差集既不是「新版本带来的改动」，也会把
    同一批 commit 重复列出来。用标签锚定主线才稳定。
    """
    if current:
        tag = f"v{current}"
        try:
            _run_git("rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}")
            return tag
        except RuntimeError:
            pass  # 没有这个标签（首次发版前/本地开发态），回退到 HEAD
    return "HEAD"


def _git_log_notes(current: str, limit: int = MAX_NOTES) -> list[str]:
    """从「当前版本之后的 origin/main 提交」里提取发布说明。

    取提交标题（squash 合并后一条 PR 就是一条提交，标题里已带 PR 号），
    去掉合并噪音与自动发版提交。git 默认倒序（最新在前），页面按序展示。
    """
    try:
        raw = _run_git(
            "log", f"--max-count={limit}", "--no-merges",
            "--pretty=format:%s", f"{_since_ref(current)}..origin/main",
        )
    except RuntimeError:
        return []
    notes = [
        line.strip() for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("chore(release):")
    ]
    return notes[:limit]


def _git_count_ahead(current: str) -> int:
    try:
        raw = _run_git("rev-list", "--count", f"{_since_ref(current)}..origin/main").strip()
        return int(raw or 0)
    except (RuntimeError, ValueError):
        return 0


def check(*, fetch: bool = True) -> dict[str, Any]:
    """做一次检测，返回给页面的完整结果（永不抛异常）。

    结果形状::

        {"current": "2.0.4", "latest": "2.0.5", "has_update": true,
         "behind": 3, "notes": ["feat: ...", ...],
         "checked_at": 1764..., "error": null, "fetch_ok": true}

    ``error`` 非空时 ``has_update`` 一律为 false——网络/凭据问题不等于「有新版本」，
    宁可不提示也不能误报。
    """
    result: dict[str, Any] = {
        "current": installed_version(),
        "latest": "",
        "has_update": False,
        "behind": 0,
        "notes": [],
        "checked_at": int(time.time()),
        "error": None,
        "fetch_ok": False,
    }
    if fetch:
        try:
            _run_git("fetch", "origin", "main", "--quiet")
            result["fetch_ok"] = True
        except RuntimeError as exc:
            result["error"] = str(exc)
    try:
        latest = remote_version()
    except RuntimeError as exc:
        result["error"] = result["error"] or str(exc)
        latest = ""
    result["latest"] = latest
    if latest and is_newer(latest, result["current"]):
        result["has_update"] = True
        result["behind"] = _git_count_ahead(result["current"])
        result["notes"] = _git_log_notes(result["current"])
    return result


# ---------------------------------------------------------------------------
# 后台轮询
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_cache: dict[str, Any] | None = None
_last_at: float = 0.0
_checking = False


def cached() -> dict[str, Any]:
    """最近一次检测结果（不触发检测）。从未检测过时返回占位结果。"""
    with _lock:
        if _cache is not None:
            return dict(_cache)
    return {
        "current": installed_version(), "latest": "", "has_update": False,
        "behind": 0, "notes": [], "checked_at": 0, "error": None,
        "fetch_ok": False, "pending": True,
    }


def _store(result: dict[str, Any]) -> None:
    global _cache, _last_at
    with _lock:
        _cache = dict(result)
        _last_at = time.time()
    try:
        _state_file().write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # 缓存落盘只是加速首屏，写不了不影响功能


def refresh(*, force: bool = False) -> dict[str, Any]:
    """同步跑一次检测并更新缓存。

    ``force=False`` 时若距上次检测不足 :data:`POLL_INTERVAL_S` 就直接返回缓存——
    这是 ``/ui/api/release`` 的节流：多个标签页同时打开也只打一次 git。
    """
    global _checking
    with _lock:
        if not force and _cache is not None and time.time() - _last_at < POLL_INTERVAL_S:
            return dict(_cache)
        if _checking:
            return dict(_cache) if _cache is not None else cached()
        _checking = True
    try:
        result = check()
        _store(result)
        return result
    finally:
        with _lock:
            _checking = False


def refresh_async() -> None:
    """在后台线程跑一次检测（供轮询循环与接口的非阻塞刷新用）。"""
    if _checking:
        return

    def _work() -> None:
        try:
            refresh(force=True)
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常抛进事件循环
            _log.warning("版本检测失败: %s: %s", type(exc).__name__, exc)

    threading.Thread(target=_work, name="release-check", daemon=True).start()


def _load_disk_cache() -> None:
    """启动时把上次结果读回内存，避免首屏显示「检测中」。

    只用于展示：真正的「是否有新版」仍以每次 fetch 的结果为准，磁盘缓存
    可能已经过时（用户重启前刚有人合了 PR）。
    """
    global _cache, _last_at
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict):
        with _lock:
            _cache = data
            _last_at = 0.0  # 视为过期，首次请求会重新检测


def poll_forever() -> None:
    """常驻轮询：每 :data:`POLL_INTERVAL_S` 秒检测一次，永不返回。

    由 ``__main__`` 在启动时 ``_spawn_bg``。异常一律吞掉并记日志——轮询线程
    死掉会让「有新版本」永久失效，比偶发一次检测失败糟糕得多。
    """
    _load_disk_cache()
    while True:
        try:
            refresh(force=True)
        except Exception as exc:  # noqa: BLE001
            _log.warning("版本轮询失败: %s: %s", type(exc).__name__, exc)
        time.sleep(POLL_INTERVAL_S)
