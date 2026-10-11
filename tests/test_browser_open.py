"""open_external_url：webbrowser 静默失败必须兜底，不能吞掉授权链接。

历史缺陷：登录流程直接 ``webbrowser.open(url)``,不看返回值。webbrowser
失败时**不抛异常、只返回 False**（macOS 的 osascript ``open location``
在 SSH/无 GUI 会话必失败），于是授权 URL 凭空消失，用户对着「浏览器将
打开授权页」的横幅干等到超时（buddy login trae 实测踩坑）。

本文件不碰真实浏览器、不联网：webbrowser/subprocess 全部 monkeypatch。
"""
from __future__ import annotations

import subprocess

import pytest

from buddy_proxy.core import browser as browser_module
from buddy_proxy.core.browser import open_external_url

URL = "https://auth.example.invalid/login?state=xyz"


def test_webbrowser_ok_skips_open(monkeypatch):
    """webbrowser 成功就不该再跑 open（避免重复开页）。"""
    monkeypatch.setattr("webbrowser.open", lambda u: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(browser_module.subprocess, "run",
                        lambda *a, **kw: calls.append(list(a[0])))
    assert open_external_url(URL) is True
    assert calls == []


def test_webbrowser_false_falls_back_to_open_cmd(monkeypatch):
    """webbrowser 静默返回 False 时，macOS 退 ``open`` 命令再救一次。"""
    monkeypatch.setattr("webbrowser.open", lambda u: False)
    monkeypatch.setattr("sys.platform", "darwin")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        browser_module.subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    assert open_external_url(URL) is True
    assert calls == [["open", URL]]


@pytest.mark.parametrize("raise_exc", [False, True], ids=["ret_false", "raise"])
def test_all_paths_fail_returns_false(monkeypatch, raise_exc):
    """两条路都死时必须返回 False（不抛），让调用方把链接打出来。"""
    def _boom(u):
        if raise_exc:
            raise RuntimeError("no gui session")
        return False

    monkeypatch.setattr("webbrowser.open", _boom)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(
        browser_module.subprocess, "run",
        lambda cmd, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(1, cmd)),
    )
    assert open_external_url(URL) is False


def test_non_darwin_no_open_fallback(monkeypatch):
    """非 macOS 没有 open 命令可退，直接 False（Linux 的 xdg-open 暂不涉及）。"""
    monkeypatch.setattr("webbrowser.open", lambda u: False)
    monkeypatch.setattr("sys.platform", "linux")
    calls: list[list[str]] = []
    monkeypatch.setattr(browser_module.subprocess, "run",
                        lambda *a, **kw: calls.append(list(a[0])))
    assert open_external_url(URL) is False
    assert calls == []
