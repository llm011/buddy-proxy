"""codex 转发主链路 + 多账号 failover 测试（离线，httpx.MockTransport）。

模式照 test_kimi_failover.py：

- 假上游按 ``Authorization: Bearer <token>`` 路由 canned 响应，token 由打桩的
  ``ensure_account_token`` 生成（``tok-<account_id>``），顺带断言账号归属；
- 上游是 **Responses** 协议（SSE 事件 ``response.output_text.delta`` 等），
  不是 chat completions；
- 流式响应用 ``_ChunkedBody`` 真实一次性流；
- 闸门类场景（流式）必须在**单个 event loop** 里跑完 forward + 消费，
  否则 ``_ReplayStream`` 续跑的行迭代器会炸。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from buddy_proxy.codex import failover
from buddy_proxy.codex.credentials import save_account_cred
from buddy_proxy.codex.provider import CodexProvider
from buddy_proxy.core.metrics import ACCOUNT_META


@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    failover._cooldowns.clear()
    yield
    failover._cooldowns.clear()


def _write_account(acct_id: str, *, email: str = "", plan: str = "plus") -> None:
    save_account_cred({
        "account_id": acct_id,
        "type": "codex",
        "access_token": f"stale-{acct_id}",
        "refresh_token": f"rt-{acct_id}",
        "expired": "2099-01-01T00:00:00Z",
        "chatgpt_account_id": f"acc-{acct_id}",
        "email": email or f"{acct_id}@example.com",
        "plan_type": plan,
    })


class _ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class _RaisingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise httpx.TimeoutException("read timed out")
        yield b""  # pragma: no cover


class _Upstream:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        self.calls.append(token)
        self.requests.append(request)
        route = self.routes.get(token)
        if route is None:
            return httpx.Response(500, json={"error": {"message": f"no route {token}"}})
        return route(request)


def _patch_token(monkeypatch) -> list[tuple[str, bool]]:
    calls: list[tuple[str, bool]] = []

    def fake(account_id, *, force_refresh=False):
        calls.append((account_id, force_refresh))
        return (f"tok-{account_id}", {
            "chatgpt_account_id": f"acc-{account_id}",
        })

    monkeypatch.setattr("buddy_proxy.codex.provider.ensure_account_token", fake)
    return calls


def _sse_events(*events: tuple[str, dict]) -> bytes:
    out = []
    for name, payload in events:
        out.append(f"event: {name}\ndata: {json.dumps(payload)}\n\n")
    return "".join(out).encode()


def _ok_stream(text: str = "pong") -> httpx.Response:
    body = _sse_events(
        ("response.created", {"type": "response.created", "response": {"id": "r1"}}),
        ("response.output_item.added", {"item": {"type": "message", "id": "m1"}}),
        ("response.output_text.delta", {"delta": text}),
        ("response.completed", {"response": {"id": "r1", "usage": {
            "input_tokens": 3, "output_tokens": 2, "total_tokens": 5}}}),
    )
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          stream=_ChunkedBody([body]))


def _ok_json(text: str = "pong") -> httpx.Response:
    return httpx.Response(200, json={
        "id": "resp_1", "object": "response", "status": "completed",
        "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    })


def _error(status: int, retry_after: str | None = None):
    headers = {"Retry-After": retry_after} if retry_after else None
    return lambda req: httpx.Response(
        status, json={"error": {"message": f"HTTP {status}"}}, headers=headers)


@pytest.fixture
def two_accounts():
    _write_account("acct-a", email="a@x.y")   # priority 0（主号）
    _write_account("acct-b", email="b@x.y")   # priority 1（备号）
    upstream = _Upstream()
    prov = CodexProvider()
    from buddy_proxy.codex import provider as codex_provider
    prov._client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
        timeout=codex_provider._TIMEOUT_NONSTREAM)
    return prov, upstream


def _body(**over) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "codex/gpt-5.4",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    body.update(over)
    return body


def _run(prov, body, protocol: str = "openai"):
    return asyncio.run(prov.forward(body, protocol))


async def _consume(resp) -> str:
    """把 StreamingResponse 的 body 读干净（流式断言用）。"""
    out = []
    async for raw in resp.body_iterator:
        out.append(raw.decode() if isinstance(raw, bytes) else raw)
    return "".join(out)


def _run_stream_text(prov, body, protocol: str = "openai") -> str:
    """跑一次流式请求并把整条流读成文本（forward + 消费同一 event loop）。"""

    async def _go():
        resp = await prov.forward(body, protocol)
        return await _consume(resp)

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------

def test_models_catalog():
    prov = CodexProvider()
    ids = [m["id"] for m in prov.models()]
    assert ids[0] == "gpt-5.5", "目录首条 = 默认模型"
    assert prov.accepts_model("gpt-5.5")
    assert prov.accepts_model("codex/gpt-5.5")
    assert not prov.accepts_model("kimi-for-coding")
    assert prov.resolve_model("codex/gpt-5.4") == "gpt-5.4"
    assert prov.resolve_model("nope") is None


def test_ensure_auth_without_accounts():
    prov = CodexProvider()
    with pytest.raises(HTTPException) as exc:
        prov.ensure_auth()
    assert exc.value.status_code == 401


def test_unknown_model_rejected(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _run(prov, _body(model="codex/nonexistent"))
    assert exc.value.status_code == 400
    assert upstream.calls == []


def test_upstream_request_shape(monkeypatch, two_accounts):
    """上游请求必须打到 /responses，带伪装头与 Chatgpt-Account-Id。"""
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = lambda req: _ok_json("pong")
    resp = _run(prov, _body())
    assert resp.status_code == 200

    req = upstream.requests[0]
    assert str(req.url) == "https://chatgpt.com/backend-api/codex/responses"
    assert req.headers["Chatgpt-Account-Id"] == "acc-acct-a"
    assert req.headers["Originator"] == "codex-tui"
    assert "codex-tui" in req.headers["User-Agent"]
    sent = json.loads(req.content)
    assert sent["model"] == "gpt-5.4"
    assert sent["input"][0]["role"] == "user"
    assert "instructions" in sent


def test_nonstream_openai_protocol(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = lambda req: _ok_json("pong")
    resp = _run(prov, _body(), "openai")
    payload = json.loads(resp.body)
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "pong"
    assert payload["usage"]["total_tokens"] == 5


def test_nonstream_responses_protocol_passthrough(monkeypatch, two_accounts):
    """responses 客户端拿到的是上游 response 对象，不经 chat 投影。"""
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = lambda req: _ok_json("pong")
    resp = _run(prov, _body(), "responses")
    payload = json.loads(resp.body)
    assert payload["object"] == "response"
    assert payload["output"][0]["type"] == "message"


def test_nonstream_anthropic_protocol(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = lambda req: _ok_json("pong")
    resp = _run(prov, _body(), "anthropic")
    payload = json.loads(resp.body)
    assert payload["type"] == "message"
    assert payload["content"][0]["text"] == "pong"


def test_stream_openai_protocol(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = lambda req: _ok_stream("pong")

    async def _go():
        resp = await prov.forward(_body(stream=True), "openai")
        chunks = []
        async for raw in resp.body_iterator:
            text = raw.decode() if isinstance(raw, bytes) else raw
            for line in text.splitlines():
                if line.startswith("data: ") and line[6:] != "[DONE]":
                    chunks.append(json.loads(line[6:]))
        return chunks

    chunks = asyncio.run(_go())
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert text == "pong"


def test_stream_responses_protocol_passthrough(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = lambda req: _ok_stream("pong")

    async def _go():
        resp = await prov.forward(_body(stream=True), "responses")
        out = []
        async for raw in resp.body_iterator:
            out.append(raw.decode() if isinstance(raw, bytes) else raw)
        return "".join(out)

    text = asyncio.run(_go())
    assert "response.output_text.delta" in text
    assert "pong" in text


def test_stream_anthropic_protocol(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = lambda req: _ok_stream("pong")

    async def _go():
        resp = await prov.forward(_body(stream=True), "anthropic")
        out = []
        async for raw in resp.body_iterator:
            out.append(raw.decode() if isinstance(raw, bytes) else raw)
        return "".join(out)

    text = asyncio.run(_go())
    assert "message_start" in text
    assert "content_block_delta" in text
    assert "pong" in text


# ---------------------------------------------------------------------------
# failover
# ---------------------------------------------------------------------------

def test_429_fails_over_and_cools_down(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = _error(429, retry_after="120")
    upstream.routes["tok-acct-b"] = lambda req: _ok_json("from-b")
    resp = _run(prov, _body())
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == "from-b"
    assert upstream.calls == ["tok-acct-a", "tok-acct-b"]
    left, kind = failover.cooldown_left("acct-a")
    assert left > 0
    assert kind == "quota"


def test_403_fails_over(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = _error(403)
    upstream.routes["tok-acct-b"] = lambda req: _ok_json("from-b")
    resp = _run(prov, _body())
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == "from-b"
    assert failover.cooldown_left("acct-a")[0] > 0


def test_401_forces_refresh_then_fails_over(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    token_calls = _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = _error(401)   # 强刷后仍是同一个 tok → 仍 401
    upstream.routes["tok-acct-b"] = lambda req: _ok_json("from-b")
    resp = _run(prov, _body())
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == "from-b"
    # acct-a 试两次（原始 + 强刷），然后换 acct-b
    assert token_calls == [("acct-a", False), ("acct-a", True), ("acct-b", False)]
    assert failover.cooldown_left("acct-a")[0] > 0


def test_business_4xx_passthrough_without_failover(monkeypatch, two_accounts):
    """400（模型参数不合法）换号没意义，原样透传。"""
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = _error(400)
    resp = _run(prov, _body())
    assert resp.status_code == 400
    assert upstream.calls == ["tok-acct-a"]


def test_all_accounts_cooling_returns_429(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    failover.mark_cooldown("acct-a", quota=True)
    failover.mark_cooldown("acct-b", quota=True)
    with pytest.raises(HTTPException) as exc:
        _run(prov, _body())
    assert exc.value.status_code == 429
    assert upstream.calls == []


def test_all_accounts_fail_returns_502(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = _error(500)
    upstream.routes["tok-acct-b"] = _error(500)
    resp = _run(prov, _body())
    # 5xx 不换号（透传），只有 acct-a 被打过
    assert resp.status_code == 500
    assert upstream.calls == ["tok-acct-a"]


def test_account_meta_marked(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = lambda req: _ok_json()
    seen: dict[str, Any] = {}

    async def _go():
        meta = {"x": 1}
        token = ACCOUNT_META.set(meta)
        try:
            await prov.forward(_body(), "openai")
        finally:
            ACCOUNT_META.reset(token)
        seen.update(meta)

    asyncio.run(_go())
    assert seen["account"] == "acct-a"


def test_network_error_fails_over(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)

    def _boom(req):
        raise httpx.ConnectError("connection refused")

    upstream.routes["tok-acct-a"] = _boom
    upstream.routes["tok-acct-b"] = lambda req: _ok_json("from-b")
    resp = _run(prov, _body())
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == "from-b"
    assert failover.cooldown_left("acct-a")[0] > 0


# ---------------------------------------------------------------------------
# 首事件闸门
# ---------------------------------------------------------------------------

def test_inband_429_before_semantic_event_fails_over(monkeypatch, two_accounts):
    """HTTP 200 但首个事件是 rate_limit error：闸门必须识别并换号。"""
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    err_body = _sse_events(("error", {
        "type": "error", "code": "rate_limit_exceeded", "message": "slow down"}))
    upstream.routes["tok-acct-a"] = lambda req: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        stream=_ChunkedBody([err_body]))
    upstream.routes["tok-acct-b"] = lambda req: _ok_stream("from-b")

    text = _run_stream_text(prov, _body(stream=True))
    assert "from-b" in text
    assert upstream.calls == ["tok-acct-a", "tok-acct-b"]
    assert failover.cooldown_left("acct-a")[0] > 0


def test_empty_stream_before_semantic_event_fails_over(monkeypatch, two_accounts):
    """200 空流（假成功）必须换号，不能返回空内容。"""
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = lambda req: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        stream=_ChunkedBody([b"data: [DONE]\n\n"]))
    upstream.routes["tok-acct-b"] = lambda req: _ok_stream("from-b")
    text = _run_stream_text(prov, _body(stream=True))
    assert "from-b" in text
    assert upstream.calls == ["tok-acct-a", "tok-acct-b"]


def test_first_event_timeout_fails_over(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = lambda req: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        stream=_RaisingStream())
    upstream.routes["tok-acct-b"] = lambda req: _ok_stream("from-b")
    text = _run_stream_text(prov, _body(stream=True))
    assert "from-b" in text


def test_stream_gate_does_not_lose_buffered_lines(monkeypatch, two_accounts):
    """闸门按行消费过流，缓冲行必须随透传补放，不能丢首段内容。"""
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = lambda req: _ok_stream("hello")

    async def _go():
        resp = await prov.forward(_body(stream=True), "openai")
        out = []
        async for raw in resp.body_iterator:
            out.append(raw.decode() if isinstance(raw, bytes) else raw)
        return "".join(out)

    text = asyncio.run(_go())
    assert "hello" in text


# ---------------------------------------------------------------------------
# 额度 / 健康
# ---------------------------------------------------------------------------

def test_quota_not_logged_in():
    prov = CodexProvider()
    q = prov.quota()
    assert q is not None
    assert q["items"][0]["display"].startswith("未登录")


def test_quota_lists_accounts(two_accounts):
    prov, _ = two_accounts
    q = prov.quota()
    labels = [i["label"] for i in q["items"]]
    assert len(labels) == 2
    assert any("a@x.y" in label for label in labels)
    assert q["level"] == "plus"


def test_quota_items_enter_account_groups(two_accounts):
    """条目必须是进度条形态（percent 有值），否则面板按 notice 横贯全宽渲染，
    账号分组与 ▲▼/✎/✕ 按钮全消失；同时不许编总量（total/used 留 None）。"""
    prov, _ = two_accounts
    for it in prov.quota()["items"]:
        assert it["percent"] is not None, "percent 为 None 会被面板判成 notice"
        assert it["total"] is None and it["used"] is None, "上游没有总量，不能编"
        assert "plus" in it["note"], "套餐档位写在 note 里"


def test_quota_epoch_changes_with_accounts(two_accounts):
    prov, _ = two_accounts
    before = prov.quota_epoch()
    _write_account("acct-c", email="c@x.y")
    assert prov.quota_epoch() != before


def test_health_shape(two_accounts):
    prov, _ = two_accounts
    h = prov.health()
    assert h["id"] == "codex"
    assert h["accounts"] == 2
    assert h["plan_type"] == "plus"
