"""豆包 CDP 断线后必须能**自愈**，而不是永远卡在「App closed or reloaded」。

2026-10-31 现场：豆包工作 App 重开后，网关每条请求都报
``doubao CDP connection lost (App closed or reloaded); reopen Doubao desktop
app and retry``，重开 App 毫无作用，只有重启网关才恢复。

三层根因，各自一条测试：
1. ``CDPDoubaoClient._ready`` 只在 ``start()`` 里置位，断线后没人复位 ——
   重连时必须先 :meth:`_drop_connection`，否则 ``start()`` 拿到一个
   "已经是 ready" 的假象就直接复用死连接；
2. ``DoubaoProvider._ensure_started`` 只认 ``self._started`` 这个一次性的
   布尔值，``is_ready`` 已回滚时也不再重连；
3. 断线发生在**流式首包之前**时可以安全地重连重试一次（非流式同理），
   这正是「测试豆包渠道」走的那条路。

运行：uv run python -m pytest tests/test_doubao_reconnect.py -v
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from buddy_proxy.doubao.provider import DoubaoProvider
from buddy_proxy.doubao.ws import WSClosed


# ----------------------------------------------------------------------
# 假客户端：CDP 连接可控「断/通」，restart() 只是把 ready 拉回来
# ----------------------------------------------------------------------
class _FakeClient:
    def __init__(self) -> None:
        self.is_ready = True
        self.starts = 0
        self.restarts = 0
        self.fail_first = 0          # 前 N 次 chat_completion 抛 WSClosed
        self.calls = 0
        self.consecutive_failures = 0  # _with_reconnect 的 401/409 判据读它

    async def start(self) -> None:
        self.starts += 1
        self.is_ready = True

    async def restart(self) -> None:
        self.restarts += 1
        self.is_ready = True     # 真实现里由 _drop_connection + start() 配合完成

    def record_failure(self, code: int = 0) -> None:
        pass

    def record_success(self) -> None:
        pass

    def extract_conversation_id(self, _event: Any) -> None:
        return None

    async def chat_completion(self, *_a, **_kw):
        self.calls += 1
        if self.calls <= self.fail_first:
            # 真机上断线时 WS 层就把连接标记为不可用了（ws.close → _ready=False）
            self.is_ready = False
            raise WSClosed("websocket send failed: [Errno 32] Broken pipe")
            yield  # pragma: no cover - 让它是异步生成器
        yield {"_event": "CHUNK_DELTA", "text": "ok"}


def _provider(client: _FakeClient) -> DoubaoProvider:
    """生产里 forward() 会先 _ensure_started；默认让 provider 视为「已连好」。"""
    p = DoubaoProvider()
    p._client = client
    p._started = client.is_ready
    return p


def test_reconnect_resets_ready_before_restart():
    """_reconnect 走 restart（内部先丢连接再 start），并把 ready 拉回。"""
    client = _FakeClient()
    client.is_ready = False
    p = _provider(client)
    p._started = True

    assert asyncio.run(p._reconnect()) is True
    assert client.restarts == 1
    assert client.is_ready is True


def test_ensure_started_reconnects_when_ready_rolled_back():
    """_started=True 但 is_ready=False 时必须重连，不能直接放过。"""
    client = _FakeClient()
    client.is_ready = False
    p = _provider(client)
    p._started = True

    asyncio.run(p._ensure_started())
    assert client.restarts == 1, "已启动但连接失效时必须 restart"
    assert client.is_ready is True


def test_ensure_started_noop_when_connected():
    client = _FakeClient()
    p = _provider(client)
    p._started = True

    asyncio.run(p._ensure_started())
    assert (client.restarts, client.starts) == (0, 0)


def test_stream_retries_once_after_reconnect():
    """流式首包前断线 → 重连 → 重试一次，用户看到的是正常回答。"""
    client = _FakeClient()
    client.fail_first = 1
    p = _provider(client)

    async def _drain() -> list[str]:
        return [c async for c in p._stream(
            "hi", 0, "doubao", None, None, retry_on_closed=True)]

    chunks = asyncio.run(_drain())
    assert client.restarts == 1
    assert any('"content": "ok"' in c for c in chunks), chunks
    assert chunks[-1] == "data: [DONE]\n\n"


def test_stream_does_not_retry_after_output_started():
    """已经开始吐内容后才断线：不能拼接第二次回答，只能如实报错。"""
    client = _FakeClient()
    p = _provider(client)

    async def _partial_then_die(*_a, **_kw):
        yield {"_event": "CHUNK_DELTA", "text": "前半"}
        raise WSClosed("websocket closed during CHAT")

    client.chat_completion = _partial_then_die  # type: ignore[assignment]

    async def _drain() -> list[str]:
        return [c async for c in p._stream(
            "hi", 0, "doubao", None, None, retry_on_closed=True)]

    chunks = asyncio.run(_drain())
    assert client.restarts == 0, "已经输出过就不能重试"
    assert any("前半" in c for c in chunks)
    assert any("[Error:" in c for c in chunks), chunks


def test_collect_retries_once_after_reconnect():
    """非流式（测试按钮走这条）断线 → 重连 → 重试一次，拿到完整回答。"""
    client = _FakeClient()
    client.fail_first = 1
    p = _provider(client)

    msg = asyncio.run(p._collect("hi", 0, None, None))
    assert client.restarts == 1
    assert msg["content"] == "ok"


def test_collect_drops_half_baked_conversation_id_on_retry():
    """断线前后半截会话 id 不能沿用：重跑没拿回 id 就不带 conversation_id。"""
    client = _FakeClient()
    p = _provider(client)
    calls = 0

    async def _cid_then_die(*_a, **_kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            p._client.is_ready = False  # 断线：连接随即不可用
            yield {"_cid": "half-baked"}
            raise WSClosed("websocket closed during CHAT")
        yield {"_event": "CHUNK_DELTA", "text": "ok"}

    client.chat_completion = _cid_then_die  # type: ignore[assignment]
    client.extract_conversation_id = (  # type: ignore[assignment]
        lambda ev: ev.get("_cid"))

    msg = asyncio.run(p._collect("hi", 0, None, None))
    assert client.restarts == 1
    assert msg["content"] == "ok"
    assert "conversation_id" not in msg, msg


def test_ensure_started_restarts_only_once():
    """_ensure_started 已重启过就别在 _with_reconnect 里再叠一次。"""
    client = _FakeClient()
    client.is_ready = False
    p = _provider(client)
    p._started = True
    client.restart = _count_and_ready(client)  # type: ignore[assignment]

    asyncio.run(p._with_reconnect())
    assert client.restarts == 1


def _count_and_ready(client: _FakeClient):
    async def _restart() -> None:
        client.restarts += 1
        client.is_ready = True
    return _restart


def test_restart_drops_dead_connection_before_reusing_port(
    monkeypatch: pytest.MonkeyPatch,
):
    """restart() 必须先复位 ready 再 start()。

    真机上 ``start()`` 看到端口已有 CDP 就直接 ``_connect_chat()``；若 restart
    不清 ``_ready``/``_ws``，重连后会握着旧 target 的死 socket 继续 ``Broken
    pipe`` —— 正是「重开 App 也没用，只能重启网关」的直接原因。
    """
    from buddy_proxy.doubao import cdp_client

    client = cdp_client.CDPDoubaoClient(port=19223, helper_bin="/fake/Helper")
    events: list[str] = []

    class _StaleWS:
        def close(self) -> None:
            events.append("close")

    client._ws = _StaleWS()  # type: ignore[assignment]
    client._ready = True
    client._web_id = "old"

    async def fake_start() -> None:
        # start() 之后仍要看到「不 ready」，证明 restart 清过状态
        events.append(f"start:ready={client.is_ready}:ws={client._ws}")
        client._ready = True

    monkeypatch.setattr(client, "start", fake_start)
    asyncio.run(client.restart())

    assert events == ["close", "start:ready=False:ws=None"]
    assert client.is_ready is True


def test_collect_still_409_when_reconnect_fails():
    """重连也连不上时才报 409，且不把模型打进冷却（409 不可换档）。"""
    from fastapi import HTTPException
    from buddy_proxy.codebuddy_provider.forward import _RETRYABLE_STATUS

    client = _FakeClient()
    client.fail_first = 99
    p = _provider(client)

    async def _restart_fail() -> None:
        client.restarts += 1
        client.is_ready = False

    client.restart = _restart_fail  # type: ignore[assignment]

    with pytest.raises(HTTPException) as ei:
        asyncio.run(p._collect("hi", 0, None, None))
    assert ei.value.status_code == 409
    assert client.restarts == 1, "只重连一次，别在请求里死循环"
    assert 409 not in _RETRYABLE_STATUS


# ----------------------------------------------------------------------
# 真客户端（CDPDoubaoClient）：死连接的活体判断与复位，
# 这是 2026-11-01 现场「App 还在、CDP 口还通、但聊天/签到全 WSClosed
# 且重启网关前不自愈」的直接缺口。
# ----------------------------------------------------------------------
def test_real_is_ready_tracks_ws_alive():
    """_ready=True 但底层 socket 已死时，is_ready 必须是 False。

    否则 ``_ensure_started``/``_reconnect`` 会把死连接当成「已连好」直接短路。
    """
    from buddy_proxy.doubao.cdp_client import CDPDoubaoClient
    from buddy_proxy.doubao.ws import _WS

    client = CDPDoubaoClient(port=19223, helper_bin="/fake/Helper")
    client._ready = True

    live = _WS.__new__(_WS)
    live._alive = True
    client._ws = live
    assert client.is_ready is True

    live._alive = False  # 断线：WS 层置灰
    assert client.is_ready is False, "死连接不能再被当成 ready"


def test_real_evaluate_resets_ready_on_ws_closed():
    """命令撞上死连接：``_evaluate`` 必须复位 ``_ready`` 让上层能重连。"""
    import threading

    from buddy_proxy.doubao.cdp_client import CDPDoubaoClient
    from buddy_proxy.doubao.ws import WSClosed, _WS

    client = CDPDoubaoClient(port=19223, helper_bin="/fake/Helper")
    client._ready = True

    class _Boom:
        alive = False  # 死连接：命令抛 WSClosed 时 _alive 已置 False

        def cmd(self, *_a, **_kw):
            raise WSClosed("websocket send failed: [Errno 32] Broken pipe")

    client._ws = _Boom()  # type: ignore[assignment]
    client._ws_lock = threading.Lock()

    with pytest.raises(WSClosed):
        client._evaluate("location.href")
    assert client._ready is False, "死连接上失败后 _ready 必须复位"

    # 复位后 is_ready 也随之为 False（即便换回一条不会抛的 ws，_ready 已清）
    assert client.is_ready is False


def test_real_reconnect_restarts_after_self_invalidation(monkeypatch):
    """自失效后 ``_reconnect`` 不能再短路，必须真的 restart 一次。"""
    from buddy_proxy.doubao.provider import DoubaoProvider
    from buddy_proxy.doubao.cdp_client import CDPDoubaoClient
    from buddy_proxy.doubao.ws import _WS

    client = CDPDoubaoClient(port=19223, helper_bin="/fake/Helper")
    client._ready = True
    dead = _WS.__new__(_WS)
    dead._alive = False
    client._ws = dead

    provider = DoubaoProvider()
    provider._client = client
    provider._started = True

    calls = {"n": 0}

    async def _restart():
        calls["n"] += 1
        alive = _WS.__new__(_WS)
        alive._alive = True
        client._ws = alive
        client._ready = True

    monkeypatch.setattr(client, "restart", _restart)
    assert asyncio.run(provider._reconnect()) is True
    assert calls["n"] == 1, "死连接必须触发一次 restart，而不是短路返回"


def test_real_evaluate_keeps_ready_on_timeout_ws_closed():
    """读超时（alive 仍 True）不算断线：不能复位 _ready 去触发无谓重连。"""
    import threading

    from buddy_proxy.doubao.cdp_client import CDPDoubaoClient
    from buddy_proxy.doubao.ws import WSClosed

    client = CDPDoubaoClient(port=19223, helper_bin="/fake/Helper")
    client._ready = True

    class _Slow:
        alive = True  # 只是这一帧慢，连接还活着

        def cmd(self, *_a, **_kw):
            raise WSClosed("websocket Runtime.evaluate timed out after 0.01s")

    client._ws = _Slow()  # type: ignore[assignment]
    client._ws_lock = threading.Lock()

    with pytest.raises(WSClosed):
        client._evaluate("location.href")
    assert client._ready is True, "读超时不该把好连接判死"
    assert client.is_ready is True


# ----------------------------------------------------------------------
# 401 vs 409：曾经连上、重连又失败 = 409「连接丢失」（不可换档），
# 只有从未连上才是 401「未登录」。否则 App 关了会被误报成「请扫码登录」。
# ----------------------------------------------------------------------
def _dead_fake() -> _FakeClient:
    """连接已失效、重连也拉不回来的客户端（模拟 App 关了）。"""
    c = _FakeClient()
    c.is_ready = False

    async def _restart_fail() -> None:
        c.restarts += 1
        c.is_ready = False

    c.restart = _restart_fail  # type: ignore[assignment]
    return c


def test_with_reconnect_reports_409_when_reconnect_fails():
    from fastapi import HTTPException
    from buddy_proxy.codebuddy_provider.forward import _RETRYABLE_STATUS

    client = _dead_fake()
    p = _provider(client)
    p._started = True   # 曾经连上 → App 关了，应报 409 而非 401

    with pytest.raises(HTTPException) as ei:
        asyncio.run(p._with_reconnect())
    assert ei.value.status_code == 409, "App 关了不能报成「未登录」"
    assert 409 not in _RETRYABLE_STATUS


def test_with_reconnect_reports_401_when_never_connected():
    from fastapi import HTTPException

    client = _FakeClient()
    client.is_ready = False

    async def _start_fail() -> None:
        client.starts += 1
        client.is_ready = False

    client.start = _start_fail  # type: ignore[assignment]
    p = _provider(client)
    p._started = False       # 从未连上 → 401

    with pytest.raises(HTTPException) as ei:
        asyncio.run(p._with_reconnect())
    assert ei.value.status_code == 401
