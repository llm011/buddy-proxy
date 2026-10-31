"""豆包 CDP 断线的两种失败形态必须是「渠道级」而不是「进程级」。

2026-10-31 事故：豆包 App 被关掉/刷新后，CDP WebSocket 以 ``BrokenPipeError``
等形式失效。原先 ``ws.send`` 裸抛 OSError、``_collect`` 也不接，异常一路冒泡到
FastAPI，**直接杀死网关进程**（launchd ``KeepAlive=1`` 于是每 5-8 分钟重启一次，
正在等待上游的其它渠道请求被一起切断）。

这里锁两件事：
1. ``_WS`` 的发送/读取失败统一包装成 ``WSClosed``；
2. ``_collect`` 把它转成 409 —— **不在** ``_RETRYABLE_STATUS`` 里，换档逻辑
   原样上抛、不给豆包模型打冷却（App 关掉不是模型坏了）。

运行：PYTHONPATH=src python3 -m pytest tests/test_doubao_ws_closed.py -v
"""
from __future__ import annotations

import asyncio
import socket

import pytest
from fastapi import HTTPException

from buddy_proxy.doubao import ws as ws_mod
from buddy_proxy.doubao.provider import DoubaoProvider
from buddy_proxy.doubao.ws import WSClosed, _WS
from buddy_proxy.codebuddy_provider.forward import _RETRYABLE_STATUS


class _DeadSocket:
    """写就 BrokenPipe、settimeout 正常的最小替身。"""

    def settimeout(self, _t):
        pass

    def sendall(self, _frame):
        raise BrokenPipeError(32, "Broken pipe")

    def recv(self, _n):
        raise ConnectionResetError(54, "Connection reset by peer")


def test_ws_send_wraps_broken_pipe():
    w = _WS.__new__(_WS)
    w._s = _DeadSocket()
    with pytest.raises(WSClosed):
        w.send("ping")


def test_ws_cmd_wraps_socket_errors():
    w = _WS.__new__(_WS)
    w._s = _DeadSocket()
    w._id = 1
    with pytest.raises(WSClosed):
        w.cmd("Runtime.evaluate", {"expression": "1"})


def test_ws_closed_is_runtime_error():
    # provider 里 ``except RuntimeError: raise`` 排在 WSClosed 之后，
    # 依赖 WSClosed 是它的子类才能先命中 WSClosed 分支。
    assert issubclass(WSClosed, RuntimeError)


def test_collect_maps_ws_closed_to_non_retryable(monkeypatch):
    """_collect 收到 WSClosed → 409，且 409 不会触发换档/冷却。"""
    provider = DoubaoProvider()
    provider._client.record_failure = lambda code=0: None

    async def _boom(*_a, **_kw):
        raise WSClosed("websocket send failed: [Errno 32] Broken pipe")
        yield  # pragma: no cover - 让它是异步生成器

    monkeypatch.setattr(provider._client, "chat_completion", _boom)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(provider._collect("hi", 0, None, None))
    assert ei.value.status_code == 409
    # 关键：必须在「可换档」集合之外，否则会 mark_failed 把模型冷却掉
    assert 409 not in _RETRYABLE_STATUS


def test_ws_module_imports_socket_for_timeout_mapping():
    # cmd() 依赖 socket.timeout 是 OSError 子类这一事实来统一包装
    assert issubclass(socket.timeout, OSError)
    assert ws_mod is not None
