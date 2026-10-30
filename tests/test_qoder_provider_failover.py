"""qoder 转发主链路 + 多账号 failover 测试（离线，httpx.MockTransport）。

模式照 test_kimi_failover.py：
- 假上游按 ``Cosy-User``（uid）路由 canned 响应，uid 由打桩的
  ``ensure_account_token`` 生成（``uid-<account_id>``），顺带断言账号归属；
- qoder 上游是 COSY 信封：成功帧 ``data:{"headers":…,"body":"<内层 json>"}``，
  带内错误帧 ``data:{"code":"112","message":"…"}``；
- 闸门类场景（流式）必须在**单个 event loop** 里跑完 forward + 消费：
  ``_ReplayStream`` 续跑的行迭代器跨 asyncio.run 边界会炸。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from buddy_proxy.core.metrics import ACCOUNT_META
from buddy_proxy.qoder import failover
from buddy_proxy.qoder import provider as qoder_provider
from buddy_proxy.qoder.credentials import save_account_cred
from buddy_proxy.qoder.provider import QoderProvider


# ---------------------------------------------------------------------------
# 基建
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    failover._cooldowns.clear()
    yield
    failover._cooldowns.clear()


def _write_account(acct_id: str, *, region: str = "cn") -> None:
    save_account_cred({
        "account_id": acct_id,
        "token": f"dt-{acct_id}",
        "uid": f"uid-{acct_id}",
        "machine_id": f"m-{acct_id}",
        "refresh_token": f"rt-{acct_id}",
        "expires_at_ms": 9999999999000,
        "name": "",
        "email": f"{acct_id}@qoder.example.com",
        "region": region,
        "plan": "",
        "source": "state",
    })


class _ChunkedBody(httpx.AsyncByteStream):
    """真实一次性流：分块交付，闸门消费过就不许再开。"""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class _Upstream:
    """按 ``Cosy-User``（uid）路由 canned 响应；记录每次命中的 uid（= 账号归属）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.routes: dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        uid = request.headers.get("Cosy-User", "").strip()
        self.calls.append(uid)
        route = self.routes.get(uid)
        if route is None:
            return httpx.Response(500, json={"error": {"message": f"no route {uid}"}})
        return route(request)


def _patch_token(monkeypatch) -> list[tuple[str, bool]]:
    """打桩 ensure_account_token（provider 命名空间）：token 与 cred 同源返回。

    返回调用记录 ``[(account_id, force_refresh)]``。
    """
    calls: list[tuple[str, bool]] = []

    def fake(account_id, *, force_refresh=False):
        calls.append((account_id, force_refresh))
        return (f"dt-{account_id}", {
            "account_id": account_id,
            "token": f"dt-{account_id}",
            "uid": f"uid-{account_id}",
            "machine_id": f"m-{account_id}",
            "refresh_token": f"rt-{account_id}",
            "expires_at_ms": 9999999999000,
            "email": f"{account_id}@qoder.example.com",
            "region": "cn",
            "plan": "",
            "source": "state",
        })

    monkeypatch.setattr("buddy_proxy.qoder.provider.ensure_account_token", fake)
    return calls


def _cosy_frame(inner: dict) -> bytes:
    """COSY 信封成功帧（内层是 OpenAI chunk）。"""
    envelope = {"headers": {}, "body": json.dumps(inner)}
    return f"data: {json.dumps(envelope)}\n\n".encode()


def _cosy_error(code: str, message: str) -> bytes:
    """COSY 带内错误帧（HTTP 200，body 里是 code/message）。"""
    envelope = {"code": code, "message": message}
    return f"data: {json.dumps(envelope)}\n\n".encode()


def _text_stream(text: str) -> httpx.Response:
    """成功流：一个带 content 的 chunk + finish/usage + [DONE]。"""
    chunks = [
        _cosy_frame({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}),
        _cosy_frame({"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}),
        _cosy_frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}),
        b"data: [DONE]\n\n",
    ]
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          stream=_ChunkedBody(chunks))


def _run(monkeypatch, upstream_routes: dict[str, Any], body: dict,
         protocol: str = "openai"):
    """起 provider + MockTransport，跑 forward。"""
    up = _Upstream()
    up.routes.update(upstream_routes)
    token_calls = _patch_token(monkeypatch)

    prov = QoderProvider()
    meta: dict[str, Any] = {}
    holder = ACCOUNT_META.set(meta)
    try:
        async def _go():
            # forward 内部每次尝试都新建 AsyncClient——换成 MockTransport 需要
            # 包一层 transport 工厂；这里改用 patch httpx.AsyncClient。
            return await prov.forward(body, protocol)
        # patch：让 forward 里的 httpx.AsyncClient 走 MockTransport
        real_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(up)
            kwargs.pop("timeout", None)  # MockTransport 不吃 timeout 组合
            return real_client(*args, **kwargs)

        monkeypatch.setattr("buddy_proxy.qoder.provider.httpx.AsyncClient", client_factory)
        resp = asyncio.run(_go())
        return resp, up.calls, token_calls, meta
    finally:
        ACCOUNT_META.reset(holder)


def _run_stream(monkeypatch, upstream_routes: dict[str, Any], body: dict,
                protocol: str = "openai") -> tuple[bytes, list[str], list, dict]:
    """流式：单 loop 跑完 forward + 消费（见模块 docstring）。"""
    up = _Upstream()
    up.routes.update(upstream_routes)
    token_calls = _patch_token(monkeypatch)

    prov = QoderProvider()
    meta: dict[str, Any] = {}
    holder = ACCOUNT_META.set(meta)

    async def _go():
        resp = await prov.forward(body, protocol)
        chunks = []
        async for piece in resp.body_iterator:
            chunks.append(piece if isinstance(piece, bytes) else piece.encode())
        return b"".join(chunks)

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(up)
        kwargs.pop("timeout", None)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("buddy_proxy.qoder.provider.httpx.AsyncClient", client_factory)
    try:
        body_bytes = asyncio.run(_go())
    finally:
        ACCOUNT_META.reset(holder)
    return body_bytes, up.calls, token_calls, meta


# ---------------------------------------------------------------------------
# failover 编排
# ---------------------------------------------------------------------------

def test_single_account_success_nonstream(monkeypatch):
    _write_account("acct-a")
    routes = {"uid-acct-a": lambda req: _text_stream("pong")}
    resp, calls, _, meta = _run(
        monkeypatch, routes,
        {"model": "qwen3.8-flash", "messages": [{"role": "user", "content": "hi"}],
         "stream": False})
    assert resp.status_code == 200
    payload = json.loads(resp.body)
    assert payload["choices"][0]["message"]["content"] == "pong"
    assert calls == ["uid-acct-a"]
    assert meta.get("account") == "acct-a", "ACCOUNT_META 打标实际服务账号"


def test_failover_on_inband_112_error(monkeypatch):
    """带内 112（权益门/额度尽）：冷却换号，第二个账号出字。"""
    _write_account("acct-a")
    _write_account("acct-b")
    routes = {
        "uid-acct-a": lambda req: httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=_ChunkedBody([_cosy_error("112", '{"pricingUrl":"https://qoder.com.cn/pricing"}')])),
        "uid-acct-b": lambda req: _text_stream("from-b"),
    }
    body_bytes, calls, _, meta = _run_stream(
        monkeypatch, routes,
        {"model": "qwen3.8-flash", "messages": [{"role": "user", "content": "hi"}],
         "stream": True})
    assert b"from-b" in body_bytes
    assert calls == ["uid-acct-a", "uid-acct-b"], "112 带内错误触发换号"
    assert meta.get("account") == "acct-b"
    # 账号 a 被冷却
    left, kind = failover.cooldown_left("acct-a")
    assert left > 0 and kind == "quota"


def test_failover_on_http_429(monkeypatch):
    """HTTP 429：冷却换号。"""
    _write_account("acct-a")
    _write_account("acct-b")
    routes = {
        "uid-acct-a": lambda req: httpx.Response(429, text="rate limited"),
        "uid-acct-b": lambda req: _text_stream("ok"),
    }
    body_bytes, calls, _, _ = _run_stream(
        monkeypatch, routes,
        {"model": "qwen3.8-flash", "messages": [{"role": "user", "content": "hi"}],
         "stream": True})
    assert b"ok" in body_bytes
    assert calls == ["uid-acct-a", "uid-acct-b"]
    left, kind = failover.cooldown_left("acct-a")
    assert left > 0 and kind == "quota"


def test_401_force_refresh_then_failover(monkeypatch):
    """401 先强刷同账号重试，仍 401 冷却换号。"""
    _write_account("acct-a")
    _write_account("acct-b")
    routes = {
        "uid-acct-a": lambda req: httpx.Response(401, text="unauthorized"),
        "uid-acct-b": lambda req: _text_stream("ok"),
    }
    _, calls, token_calls, _ = _run_stream(
        monkeypatch, routes,
        {"model": "qwen3.8-flash", "messages": [{"role": "user", "content": "hi"}],
         "stream": True})
    # acct-a 被试两次（含 force_refresh=True 的强刷重试）
    a_calls = [c for c in token_calls if c[0] == "acct-a"]
    assert len(a_calls) == 2 and a_calls[1][1] is True, "401 强刷重试同账号"


def test_all_accounts_cooldown_fast_fail(monkeypatch):
    """全部冷却：429 快速失败，不触网。"""
    _write_account("acct-a")
    _write_account("acct-b")
    failover.mark_cooldown("acct-a", quota=True, reason="测试")
    failover.mark_cooldown("acct-b", quota=True, reason="测试")
    with pytest.raises(HTTPException) as exc_info:
        _run(monkeypatch, {}, {"model": "qwen3.8-flash",
                               "messages": [{"role": "user", "content": "hi"}],
              "stream": False})
    assert exc_info.value.status_code == 429
    assert "冷却" in str(exc_info.value.detail)


def test_no_account_in_region_gives_401(monkeypatch):
    """主区域无可用账号（账号都在其它区域）：401 提示跨区不自动切换。"""
    _write_account("gl-a", region="global")
    with pytest.raises(HTTPException) as exc_info:
        _run(monkeypatch, {}, {"model": "qwen3.8-flash",
                               "messages": [{"role": "user", "content": "hi"}],
              "stream": False})
    assert exc_info.value.status_code == 401
    assert "跨区" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# 10605 排队（RESOURCE_LIMIT）：同账号等过上游给的秒数后重放，不冷却不换号
# ---------------------------------------------------------------------------

#: 线上实报的三层嵌套错误串（外层信封 body 内的 code/message，尾随 ``| code=403``）。
_10605_REAL = (
    '{"code":"10605","message":"{\\"isQueued\\":true,\\"modelKey\\":\\"dfmodel\\",'
    '\\"queueCount\\":0,\\"queueType\\":\\"p3\\",\\"retryAfterSeconds\\":30,'
    '\\"serviceAvailable\\":false,\\"subCode\\":\\"RESOURCE_LIMIT\\",\\"waitTime\\":30}",'
    '"subCode":"RESOURCE_LIMIT"} | code=403'
)


def test_queue_wait_seconds_parses_real_payload():
    """三层嵌套 + 尾随 ``| code=403`` 的 10605 串必须挖出 retryAfterSeconds。"""
    assert qoder_provider._queue_wait_seconds(_10605_REAL) == 30.0
    # 非 10605 一律 None（不能把其它 403 也当排队）
    assert qoder_provider._queue_wait_seconds('{"code":"112"}') is None
    # 认出 10605 但挖不到秒数：退回封顶值（等一会儿总比立刻失败好）
    assert qoder_provider._queue_wait_seconds(
        '{"code":"10605","message":"busy"}') == qoder_provider._QUEUE_WAIT_MAX_S
    # 上游给离谱大数时封顶
    big = '{"code":"10605","message":"{\\"retryAfterSeconds\\":99999}"}'
    assert qoder_provider._queue_wait_seconds(big) == qoder_provider._QUEUE_WAIT_MAX_S


def test_classify_frame_routes_10605_to_queue_wait():
    frame = json.dumps({"code": "10605", "message": "queued",
                        "subCode": "RESOURCE_LIMIT"})
    assert qoder_provider._classify_frame(frame).startswith("queue_wait:")


def _queue_then_ok(monkeypatch, waits: list[float]) -> None:
    """把 asyncio.sleep 换成记录秒数（真 sleep 30s 测试挂死）。"""
    async def fake_sleep(secs):
        waits.append(float(secs))

    monkeypatch.setattr(qoder_provider.asyncio, "sleep", fake_sleep)


def test_10605_waits_same_account_then_replays(monkeypatch):
    """10605 → 按 retryAfterSeconds 原地等 → 重发同一账号；不冷却、不换号。"""
    _write_account("acct-a")
    _write_account("acct-b")
    hits = {"a": 0}

    def route_a(req):
        hits["a"] += 1
        if hits["a"] == 1:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                stream=_ChunkedBody([_cosy_error(
                    "10605", '{"retryAfterSeconds":2,"subCode":"RESOURCE_LIMIT"}')]))
        return _text_stream("after-wait")

    routes = {"uid-acct-a": route_a,
              "uid-acct-b": lambda req: _text_stream("from-b")}
    waits: list[float] = []
    _queue_then_ok(monkeypatch, waits)
    body_bytes, calls, _, meta = _run_stream(
        monkeypatch, routes,
        {"model": "deepseek-v4.1-flash",
         "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert b"after-wait" in body_bytes
    assert calls == ["uid-acct-a", "uid-acct-a"], "排队必须同账号重放，不该换号"
    assert waits == [2.0], "等上游给的 retryAfterSeconds"
    assert failover.cooldown_left("acct-a")[0] <= 0, "排队不是账号故障，不冷却"
    assert meta.get("account") == "acct-a"


def test_10605_exhausted_fails_over_without_cooldown(monkeypatch):
    """一直 10605：等满轮数后降级，换号且不冷却原账号。"""
    _write_account("acct-a")
    _write_account("acct-b")

    def always_queued(req):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=_ChunkedBody([_cosy_error(
                "10605", '{"retryAfterSeconds":1}')]))

    routes = {"uid-acct-a": always_queued,
              "uid-acct-b": lambda req: _text_stream("from-b")}
    waits: list[float] = []
    _queue_then_ok(monkeypatch, waits)
    body_bytes, calls, _, meta = _run_stream(
        monkeypatch, routes,
        {"model": "deepseek-v4.1-flash",
         "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert b"from-b" in body_bytes
    rounds = qoder_provider._QUEUE_WAIT_MAX_ROUNDS + 1
    assert calls == ["uid-acct-a"] * rounds + ["uid-acct-b"]
    assert len(waits) == qoder_provider._QUEUE_WAIT_MAX_ROUNDS
    assert failover.cooldown_left("acct-a")[0] <= 0
    assert meta.get("account") == "acct-b"


def test_empty_stream_before_first_event_fails_over(monkeypatch):
    """首个语义事件前 EOF（假成功）：换号重试。"""
    _write_account("acct-a")
    _write_account("acct-b")
    routes = {
        # acct-a：200 但流里只有尾帧（无 body）→ eof
        "uid-acct-a": lambda req: httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=_ChunkedBody([b"event:finish\n\n"])),
        "uid-acct-b": lambda req: _text_stream("real"),
    }
    body_bytes, calls, _, _ = _run_stream(
        monkeypatch, routes,
        {"model": "qwen3.8-flash", "messages": [{"role": "user", "content": "hi"}],
         "stream": True})
    assert b"real" in body_bytes
    assert calls == ["uid-acct-a", "uid-acct-b"], "空流假成功触发换号"
