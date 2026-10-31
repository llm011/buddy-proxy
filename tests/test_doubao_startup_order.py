"""豆包 CDP 启动顺序单测 —— 全离线，不碰真 App / 真端口。

锁定 ETHA-51 的失败模式（2026-10-31 本机实测）：

首次 doubao 请求时主 App CDP 没起来，旧实现直接起**裸 Helper**
（``--saman-from-chat=1``）。裸 Helper 和 App 共用
``~/Library/Application Support/DoubaoWork`` 这份 profile，会抢走 Chromium
单例锁（``SingletonLock -> <host>-<helper_pid>``）。之后用户自己点开「豆包
工作」，App 发现锁被一个没有 UI 的 Helper 占着就起不来 —— 用户看到的现象
就是「豆包工作一直打不开」。

所以契约是：

1. **先 App、后 Helper**：主 App 没开调试口时，先 ``open -a DoubaoWork``
   让 App 自己拉起内嵌 Chromium；只有它彻底起不来才回退独立 Helper。
2. **回退前先确认主 App 没在跑**：主 App 在跑（用户正在用）时绝不另起
   Helper 抢锁，直接报 502 让用户决策。
3. **回退前清掉 App 身份启动的残留**（否则单例锁仍被占着）；清理只匹配
   ``DoubaoWork.app/Contents/`` 下的进程，不误杀其他同名进程。
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

import pytest

from buddy_proxy.doubao import cdp_client


class _FakePopen:
    pid = 4242

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> None:
        return None


class _Harness:
    """把 CDPDoubaoClient 的外部副作用全部替换成可观测的假实现。"""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        cdp_after_open: bool,
        cdp_already_up: bool = False,
    ):
        self.calls: list[str] = []
        self.popen_cmds: list[list[str]] = []
        self._cdp_down = not cdp_already_up
        self.cdp_after_open = cdp_after_open

        client = cdp_client.CDPDoubaoClient(port=19223, helper_bin="/fake/Helper")

        def fake_probe() -> bool:
            # 端口只在 open -a 之后才活过来
            return not self._cdp_down

        def fake_run(cmd: list[str], *a: Any, **kw: Any):
            self.calls.append(cmd[0])
            if cmd[0] == "open" and self.cdp_after_open:
                self._cdp_down = False
            if cmd[0] == "pgrep":
                return subprocess.CompletedProcess(cmd, 0)  # 主 App 在跑
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        def fake_popen(cmd: list[str], *a: Any, **kw: Any):
            self.popen_cmds.append(cmd)
            return _FakePopen()

        async def fake_connect() -> None:
            self.calls.append("connect")

        async def fast_sleep(_s: float) -> None:
            return None

        monkeypatch.setattr(client, "_probe_cdp", fake_probe)
        monkeypatch.setattr(client, "_connect_chat", fake_connect)
        monkeypatch.setattr(cdp_client.subprocess, "run", fake_run)
        monkeypatch.setattr(cdp_client.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(cdp_client.asyncio, "sleep", fast_sleep)
        monkeypatch.setattr(cdp_client, "_main_app_installed", lambda: True)
        self.client = client

    def run_start(self) -> None:
        asyncio.run(self.client.start())


def test_reuses_existing_cdp_without_launching(monkeypatch: pytest.MonkeyPatch):
    """端口已有 CDP：先探一次有没有遗留裸 Helper（pgrep），再直接复用。"""
    h = _Harness(monkeypatch, cdp_after_open=False, cdp_already_up=True)
    h.run_start()
    assert h.calls == ["pgrep", "connect"]
    assert h.popen_cmds == []


def test_open_app_first_and_reuse_its_cdp(monkeypatch: pytest.MonkeyPatch):
    """主 App 起得来时：只走 open -a，绝不另起 Helper。"""
    h = _Harness(monkeypatch, cdp_after_open=True)
    h.run_start()
    assert h.calls == ["open", "connect"]
    assert h.popen_cmds == []


def test_helper_fallback_refused_when_main_app_still_running(
    monkeypatch: pytest.MonkeyPatch,
):
    """App 起不来但进程还在（用户在用）：报错，不抢单例锁。"""
    h = _Harness(monkeypatch, cdp_after_open=False)
    with pytest.raises(RuntimeError, match="未开启 CDP 调试端口"):
        h.run_start()
    assert h.popen_cmds == []
    assert h.calls == ["open", "pgrep"]


def test_helper_fallback_when_app_cannot_start(monkeypatch: pytest.MonkeyPatch):
    """App 起不来且已退出：先清理残留，再起独立 Helper。"""
    h = _Harness(monkeypatch, cdp_after_open=False)

    def fake_run(cmd: list[str], *a: Any, **kw: Any):
        h.calls.append(cmd[0])
        running = cmd[0] == "pgrep" and False
        return subprocess.CompletedProcess(cmd, 0 if running else 1, b"", b"")

    monkeypatch.setattr(cdp_client.subprocess, "run", fake_run)
    h.run_start()
    assert h.calls == ["open", "pgrep", "pkill", "connect"]
    assert len(h.popen_cmds) == 1
    assert h.popen_cmds[0][0] == "/fake/Helper"
    assert "--remote-debugging-port=19223" in h.popen_cmds[0]


def test_missing_app_reports_actionably(monkeypatch: pytest.MonkeyPatch):
    h = _Harness(monkeypatch, cdp_after_open=False)
    monkeypatch.setattr(cdp_client, "_main_app_installed", lambda: False)
    with pytest.raises(RuntimeError, match="DoubaoWork.app 不存在"):
        h.run_start()
    assert h.calls == []


def test_cleanup_pattern_only_matches_doubaowork_bundle(monkeypatch: pytest.MonkeyPatch):
    """清理正则只命中 ``DoubaoWork.app/Contents/``，不误伤 Doubao.app 等。"""
    import re

    pat = re.compile(cdp_client._APP_PROCESS_PATTERN)
    helper = (
        "/Applications/DoubaoWork.app/Contents/Helpers/DoubaoWork Browser.app/"
        "Contents/MacOS/DoubaoWork Browser --remote-debugging-port=9223"
    )
    main_app = "/Applications/DoubaoWork.app/Contents/MacOS/DoubaoWork"
    other = "/Applications/Doubao.app/Contents/PlugIns/finder-ext.appex/Contents/MacOS/finder-ext"
    assert pat.search(helper)
    assert pat.search(main_app)
    assert not pat.search(other)


# ----------------------------------------------------------------------
# 回归：proxy 重启后遗留的**裸 Helper** 占着单例锁
# ----------------------------------------------------------------------
# 现场（2026-10-31）：昨天那次裸 Helper 起在 proxy **之前/之外**，proxy 重启
# 后 ``self._proc`` 为空、``stop()`` 永远收不到它。它继续占着 9223 和
# SingletonLock，于是「复用现有 CDP」直接成功，但用户点开豆包工作仍然闪退。
# 契约：复用一个**裸 Helper** 的 CDP 之前，必须先收掉它并释放单例锁。


def _reclaim_harness(monkeypatch, *, bare: bool, app_running: bool):
    client = cdp_client.CDPDoubaoClient(port=19223, helper_bin="/fake/Helper")
    killed: list[str] = []
    cleared: list[str] = []

    def fake_run(cmd, *a, **kw):
        if cmd[0] == "pgrep" and cmd[1] == "-f":
            if cmd[2] == cdp_client._BARE_HELPER_PATTERN:
                out = b"7438\n" if bare else b""
                return subprocess.CompletedProcess(cmd, 0 if bare else 1, out, b"")
            return subprocess.CompletedProcess(cmd, 0 if app_running else 1, b"", b"")
        if cmd[0] == "kill":
            killed.append(cmd[2])
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(cdp_client.subprocess, "run", fake_run)
    monkeypatch.setattr(cdp_client.time, "sleep", lambda _s: None)
    monkeypatch.setattr(client, "_main_app_running", lambda: app_running)
    monkeypatch.setattr(
        client, "_clear_singleton_files", lambda: cleared.append("cleared")
    )
    return client, killed, cleared


def test_stale_bare_helper_is_reclaimed_before_reuse(monkeypatch):
    """主 App 没在跑、端口被遗留裸 Helper 占着 → 先杀它再释放锁。"""
    client, killed, cleared = _reclaim_harness(
        monkeypatch, bare=True, app_running=False
    )
    client._reclaim_from_stale_bare_helper()
    assert killed == ["7438"]
    assert cleared == ["cleared"]


def test_running_main_app_is_never_stripped(monkeypatch):
    """主 App 在跑（用户正在用）→ 端口属于 App，绝不清理。"""
    client, killed, cleared = _reclaim_harness(
        monkeypatch, bare=True, app_running=True
    )
    client._reclaim_from_stale_bare_helper()
    assert killed == []
    assert cleared == []


def test_no_bare_helper_is_a_noop(monkeypatch):
    """端口上是 App 自己的 CDP（无裸 Helper）→ 什么都不做。"""
    client, killed, cleared = _reclaim_harness(
        monkeypatch, bare=False, app_running=False
    )
    client._reclaim_from_stale_bare_helper()
    assert killed == []
    assert cleared == []


def test_bare_helper_pattern_excludes_app_owned_helpers():
    """只认 ``--saman-from-chat=1``：App 自己的 ``=<主AppPID>`` Helper 不能命中。"""
    import re

    pat = re.compile(cdp_client._BARE_HELPER_PATTERN)
    bare = "/Applications/DoubaoWork.app/Contents/MacOS/DoubaoWork Browser --saman-from-chat=1 --no-first-run"
    app_child = ("/Applications/DoubaoWork.app/Contents/MacOS/DoubaoWork Browser "
                 "--remote-debugging-port=9223 --saman-from-chat=17990")
    app_child_big = "... --saman-from-chat=18007"
    assert pat.search(bare)
    assert not pat.search(app_child)
    assert not pat.search(app_child_big)
