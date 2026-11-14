"""antigravity 多账号 failover 单元测试。

httpx.MockTransport 离线打桩（按 Bearer token 路由到「账号」），不触网。
冷却状态是模块级内存 dict，autouse fixture 每用例清零防串扰。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest


# ---------------------------------------------------------------------------
# 打桩基建
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    from buddy_proxy.antigravity import failover

    failover._cooldowns.clear()
    yield
    failover._cooldowns.clear()


def _gemini_ok(text: str = "pong") -> dict:
    return {
        "candidates": [{"content": {"parts": [{"text": text}], "role": "model"}}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2, "totalTokenCount": 3},
    }


def _sse(lines: list[str]) -> bytes:
    return "".join(f"data: {line}\n\n" for line in lines).encode()


_SSE_OK = _sse([
    json.dumps({"candidates": [{"content": {"parts": [{"text": "pong"}]}}]}),
    json.dumps({"usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2}}),
])
_SSE_429 = _sse([json.dumps({"error": {"code": 429, "message": "RESOURCE_EXHAUSTED: quota"}})])


class _ChunkedBody(httpx.AsyncByteStream):
    """真实网络流的替身：字节不可重放（httpx ``is_stream_consumed``）。

    MockTransport 的 ``content=`` 响应会把字节缓进 ``_content``、可重复
    迭代——恰好掩盖了「httpx 响应流只能消费一次」的 bug（#67 引入：闸门
    读走首事件后 ``_ReplayStream`` 二次消费抛 StreamConsumed，剩余流全丢）。
    凡是要回归「闸门读过后剩余流还能不能读」的测试都必须用这个流式 body。
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class _RaisingStream(httpx.AsyncByteStream):
    """迭代即抛超时的流：模拟「响应头已到、首事件前卡死」（闸门路径超时）。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aiter__(self):
        raise self._exc
        yield b""  # pragma: no cover - 仅为了成为 async generator


class _Upstream:
    """按 Bearer token（=账号）路由 canned 响应，记录调用顺序。"""

    def __init__(self):
        self.calls: list[str] = []  # 命中的 token 顺序
        self.plan: dict[str, tuple] = {}  # token -> (status, payload, headers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        tok = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.calls.append(tok)
        status, payload, headers = self.plan[tok]
        if isinstance(payload, Exception):
            raise payload  # 超时/网络错：client.send 或闸门迭代时抛出
        if isinstance(payload, httpx.AsyncByteStream):
            return httpx.Response(status, stream=payload, headers=headers)
        if isinstance(payload, (bytes, str)):
            return httpx.Response(status, content=payload, headers=headers)
        return httpx.Response(status, json=payload, headers=headers)


@pytest.fixture
def two_accounts():
    """真实写盘两个账号（u@x.com 主、v@y.com 备），返回 (provider, upstream)。"""
    from buddy_proxy.antigravity import credentials as creds
    from buddy_proxy.antigravity.provider import AntigravityProvider

    creds.save_account_cred({"access_token": "a1", "refresh_token": "r1",
                             "expiry": "2099-01-01T00:00:00+00:00",
                             "email": "u@x.com", "project_id": "p1"})
    creds.save_account_cred({"access_token": "a2", "refresh_token": "r2",
                             "expiry": "2099-01-01T00:00:00+00:00",
                             "email": "v@y.com", "project_id": "p2"})

    upstream = _Upstream()
    provider = AntigravityProvider()
    import buddy_proxy.antigravity.provider as prov

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream),
                                         timeout=prov._TIMEOUT)
    return provider, upstream


def _patch_token(monkeypatch, refreshes: list[str] | None = None):
    import buddy_proxy.antigravity.provider as prov

    def fake(account_id, *, force_refresh=False):
        if refreshes is not None and force_refresh:
            refreshes.append(account_id)
        return f"tok-{account_id}", {"project_id": f"proj-{account_id}"}

    monkeypatch.setattr(prov, "ensure_account_token", fake)


_BODY = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}


def _default_upstream_model() -> str:
    """_run 默认请求的模型经 apply_effort_suffix 后的上游真名。"""
    from buddy_proxy.antigravity.convert import apply_effort_suffix
    from buddy_proxy.antigravity.provider import MODELS

    return apply_effort_suffix(MODELS[0], None)


def _run(provider, stream=False):
    from buddy_proxy.antigravity.provider import MODELS

    body = {**_BODY, "model": f"antigravity/{MODELS[0]['id']}", "stream": stream}
    return asyncio.run(provider.forward(body, "openai"))


def _stream_text(sr) -> str:
    async def _collect():
        chunks = []
        async for chunk in sr.body_iterator:
            chunks.append(chunk.encode() if isinstance(chunk, str) else chunk)
        return b"".join(chunks).decode()

    return asyncio.run(_collect())


# ---------------------------------------------------------------------------
# 转发 failover
# ---------------------------------------------------------------------------

def test_429_switches_account_and_respects_retry_after(two_accounts, monkeypatch):
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (429, {"error": {"code": 429, "message": "quota"}}, {"Retry-After": "90"})
    up.plan["tok-v@y.com"] = (200, _gemini_ok(), {})
    _patch_token(monkeypatch)

    resp = _run(provider)
    assert resp.status_code == 200 and b"pong" in resp.body
    assert up.calls == ["tok-u@x.com", "tok-v@y.com"]  # 主账号失败才动备用
    # 429 按 (账号, 模型) 冷却：Retry-After 覆盖默认 300s。
    model = _default_upstream_model()
    left, kind = failover.model_cooldown_left("u@x.com", model)
    assert 0 < left <= 90 and kind == "quota"
    assert failover.model_cooldown_left("v@y.com", model)[0] == 0
    # 账号本身没被摘掉——换个模型仍能用主账号，这正是本次要防的「连坐」。
    assert failover.cooldown_left("u@x.com") == (0.0, "")


def test_bad_model_429_does_not_poison_other_models(two_accounts, monkeypatch):
    """线上事故回归：一个下架模型的伪装 429 不能连坐毒死整个通道。

    2026-11-14 实测：``gemini-3.6-flash`` 已不在上游目录，两个账号依次收到
    ``429 RESOURCE_EXHAUSTED``；旧逻辑按账号级冷却 5min，随后
    ``available_accounts()`` 为空，连正常的 ``gemini-3.8-flash`` 都 429。
    修复后 429 只冷却 (账号, 模型)，别的模型仍能用同一批账号。
    """
    import httpx
    from fastapi import HTTPException

    from buddy_proxy.antigravity import failover
    from buddy_proxy.antigravity.convert import apply_effort_suffix
    from buddy_proxy.antigravity.provider import MODELS

    by_id = {m["id"]: m for m in MODELS}
    bad = apply_effort_suffix(by_id["gemini-3.6-flash"], None)
    good = apply_effort_suffix(by_id["gemini-3.8-flash"], None)
    seen: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        model = body.get("model", "")
        seen.append(model)
        if model == bad:
            return httpx.Response(429, json={"error": {
                "code": 429, "message": "Resource has been exhausted (e.g. check quota)."}})
        return httpx.Response(200, json=_gemini_ok())

    provider, _up = two_accounts
    import buddy_proxy.antigravity.provider as prov

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(route),
                                         timeout=prov._TIMEOUT)
    _patch_token(monkeypatch)

    # 1) 坏模型：两个账号都 429，按模型冷却。
    with pytest.raises(HTTPException) as ei:
        asyncio.run(provider.forward(
            {**_BODY, "model": f"antigravity/gemini-3.6-flash", "stream": False},
            "openai"))
    assert ei.value.status_code == 429
    assert failover.model_cooldown_left("u@x.com", bad)[0] > 0
    assert failover.model_cooldown_left("v@y.com", bad)[0] > 0
    assert failover.cooldown_left("u@x.com") == (0.0, ""), "账号不该被整体摘掉"

    # 2) 紧跟着用好模型：必须 200，且仍从主账号开始，不被坏模型连坐。
    seen.clear()
    resp = asyncio.run(provider.forward(
        {**_BODY, "model": f"antigravity/gemini-3.8-flash", "stream": False},
        "openai"))
    assert resp.status_code == 200 and b"pong" in resp.body
    assert seen == [good], "好模型应一次成功，不该被前一轮的 429 挡在门外"


def test_403_switches_account_short_cooldown(two_accounts, monkeypatch):
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (403, {"error": {"code": 403, "message": "PERMISSION_DENIED"}}, {})
    up.plan["tok-v@y.com"] = (200, _gemini_ok(), {})
    _patch_token(monkeypatch)

    resp = _run(provider)
    assert resp.status_code == 200
    left, kind = failover.cooldown_left("u@x.com")
    assert 0 < left <= 60 and kind == "account"


def test_403_verify_account_blacklisted_long_cooldown(two_accounts, monkeypatch):
    """403「Verify your account to continue.」= Google 风控拉黑：6h 档，kind=blacklist。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (
        403, {"error": {"code": 403, "message": "Verify your account to continue."}}, {})
    up.plan["tok-v@y.com"] = (200, _gemini_ok(), {})
    _patch_token(monkeypatch)

    resp = _run(provider)
    assert resp.status_code == 200
    left, kind = failover.cooldown_left("u@x.com")
    assert kind == "blacklist"
    assert failover._ACCOUNT_COOLDOWN_S < left <= failover._BLACKLIST_COOLDOWN_S


def test_in_band_403_verify_account_blacklisted(two_accounts, monkeypatch):
    """带内（HTTP 200 首事件）403 拉黑同样识别——闸门路径不漏。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    sse_403 = _sse([json.dumps(
        {"error": {"code": 403, "message": "Verify your account to continue."}})])
    up.plan["tok-u@x.com"] = (200, sse_403, {})
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    left, kind = failover.cooldown_left("u@x.com")
    assert kind == "blacklist" and left > 0
    assert up.calls == ["tok-u@x.com", "tok-v@y.com"]


def test_401_forces_refresh_then_switches(two_accounts, monkeypatch):
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (401, {"error": {"code": 401, "message": "Unauthorized"}}, {})
    up.plan["tok-v@y.com"] = (200, _gemini_ok(), {})
    refreshes: list[str] = []
    _patch_token(monkeypatch, refreshes)

    resp = _run(provider)
    assert resp.status_code == 200
    assert refreshes == ["u@x.com"]  # 401 先强刷同账号一次
    assert up.calls == ["tok-u@x.com", "tok-u@x.com", "tok-v@y.com"]  # 强刷重发后仍拒才换号
    assert failover.cooldown_left("u@x.com")[0] > 0


def test_all_accounts_failed_maps_last_status(two_accounts, monkeypatch):
    from fastapi import HTTPException

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (429, {"error": {"code": 429, "message": "quota a"}}, {})
    up.plan["tok-v@y.com"] = (429, {"error": {"code": 429, "message": "quota b"}}, {})
    _patch_token(monkeypatch)

    with pytest.raises(HTTPException) as ei:
        _run(provider)
    assert ei.value.status_code == 429
    msg = ei.value.detail["error"]["message"]
    assert "所有账号均不可用" in msg and "quota b" in msg  # 带上最后错误详情
    # 429 是模型级冷却：画像要点名「哪个账号的哪个模型」还剩多久，
    # 而不是查纯账号 id 的 cooldown_report（那样这里会是一片空白）。
    model = _default_upstream_model()
    assert f"u@x.com 的 {model} 额度冷却剩" in msg
    assert f"v@y.com 的 {model} 额度冷却剩" in msg


def test_business_4xx_passthrough_without_switching(two_accounts, monkeypatch):
    """业务 4xx 换号没意义：原样透传，不冷却也不打下一个账号。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (400, {"error": {"code": 400, "message": "bad request"}}, {})
    up.plan["tok-v@y.com"] = (200, _gemini_ok(), {})
    _patch_token(monkeypatch)

    resp = _run(provider)
    assert resp.status_code == 400
    assert up.calls == ["tok-u@x.com"]
    assert failover.cooldown_left("u@x.com") == (0.0, "")


def test_no_available_accounts_fast_429(two_accounts):
    from fastapi import HTTPException

    from buddy_proxy.antigravity import failover

    provider, _up = two_accounts
    failover.mark_cooldown("u@x.com", quota=True)
    failover.mark_cooldown("v@y.com", quota=True)

    with pytest.raises(HTTPException) as ei:
        _run(provider)
    assert ei.value.status_code == 429
    assert "无可用账号" in ei.value.detail["error"]["message"]
    assert "u@x.com 额度冷却剩" in ei.value.detail["error"]["message"], \
        "空候选的报错要说明谁在冷却、还剩多久"


def test_account_meta_records_selected_account(two_accounts, monkeypatch):
    from buddy_proxy.core.metrics import ACCOUNT_META

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (429, {"error": {"code": 429, "message": "quota"}}, {})
    up.plan["tok-v@y.com"] = (200, _gemini_ok(), {})
    _patch_token(monkeypatch)

    meta: dict = {}
    token = ACCOUNT_META.set(meta)
    try:
        resp = _run(provider)
    finally:
        ACCOUNT_META.reset(token)
    assert resp.status_code == 200
    assert meta["account"] == "v@y.com"  # metrics 落库的是实际服务的账号


# ---------------------------------------------------------------------------
# 上游超时：换号不再被打断 + 尝试期预算（2026-10-03 实测 600s read 挂死 5 单）
# ---------------------------------------------------------------------------

def test_nonstream_timeout_switches_account_and_cools(two_accounts, monkeypatch):
    """非流式超时：过去直接 504 打断 failover（客户端干等满 read 超时），
    现在短冷却换号重试。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (0, httpx.ReadTimeout("read timed out"), {})
    up.plan["tok-v@y.com"] = (200, _gemini_ok(), {})
    _patch_token(monkeypatch)

    resp = _run(provider)
    assert resp.status_code == 200 and b"pong" in resp.body
    # u 在两个 endpoint（daily/prod）上各超时一次才判不可用，然后换 v
    assert up.calls == ["tok-u@x.com", "tok-u@x.com", "tok-v@y.com"]
    left, kind = failover.cooldown_left("u@x.com")
    assert 0 < left <= failover._ACCOUNT_COOLDOWN_S and kind == "account"


def test_timeout_on_all_accounts_maps_504_with_report(two_accounts, monkeypatch):
    """全部账号超时：504 + 逐账号画像（不再是打断循环的一只 504）。"""
    from fastapi import HTTPException

    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (0, httpx.ReadTimeout("slow"), {})
    up.plan["tok-v@y.com"] = (0, httpx.ReadTimeout("slow"), {})
    _patch_token(monkeypatch)

    with pytest.raises(HTTPException) as ei:
        _run(provider)
    assert ei.value.status_code == 504
    msg = ei.value.detail["error"]["message"]
    assert "所有账号均不可用" in msg and "504" in msg
    assert ei.value.detail["error"]["type"] == "timeout"
    assert failover.cooldown_left("u@x.com")[0] > 0  # 两个账号都进了短冷却
    assert failover.cooldown_left("v@y.com")[0] > 0


def test_stream_gate_read_timeout_switches_and_cools(two_accounts, monkeypatch):
    """流式首事件前卡死（头已到、读超时）：eof 换号 + timed_out 短冷却。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, _RaisingStream(httpx.ReadTimeout("stalled")), {})
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    assert "pong" in _stream_text(sr)
    assert up.calls == ["tok-u@x.com", "tok-v@y.com"]
    left, kind = failover.cooldown_left("u@x.com")
    assert 0 < left <= failover._ACCOUNT_COOLDOWN_S and kind == "account"


def test_stream_clean_eof_does_not_cool(two_accounts, monkeypatch):
    """干净 EOF（假成功）换号但**不**冷却——只有读超时才冷却（既有语义保持）。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, b"", {})
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    assert failover.cooldown_left("u@x.com") == (0.0, "")


def test_attempt_deadline_caps_chain(two_accounts, monkeypatch):
    """尝试期预算：前面的账号把时间烧完后不再开新尝试。"""
    import buddy_proxy.antigravity.provider as prov

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (0, httpx.ReadTimeout("slow"), {})
    up.plan["tok-v@y.com"] = (200, _gemini_ok(), {})
    _patch_token(monkeypatch)
    monkeypatch.setattr(prov, "_ATTEMPT_DEADLINE_S", -1.0)  # 立刻超预算

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        _run(provider)
    assert ei.value.status_code == 504
    # 首个账号不受预算挡（打满两个 endpoint），第二个账号被预算拦下
    assert up.calls == ["tok-u@x.com", "tok-u@x.com"], "预算用尽后不得再开第二个账号的尝试"


# ---------------------------------------------------------------------------
# 流式首事件闸门
# ---------------------------------------------------------------------------

def test_stream_gate_one_shot_body_not_double_consumed(two_accounts, monkeypatch):
    """闸门读过首事件后，剩余流必须还能继续读（#67 预存 bug）。

    真实网络的 httpx 响应流只能消费一次；闸门用 ``resp.aiter_lines()``
    读走首事件后，``_ReplayStream`` 再调一次即抛 StreamConsumed，缓冲行
    之后的整条流静默丢失（claude 流式因此完全空流）。首事件带文本 + 次事件
    带 usage，复现「尾部事件被吞」的最直观形态。

    **必须单 loop 跑完 forward + 消费**：闸门持有的在途迭代器是 async
    generator，跨 ``asyncio.run`` 边界时前一个 loop 收尾会 aclose 它，
    只剩缓冲行可读——那种形态既不复现 StreamConsumed，也掩盖真实回归
    （生产是 uvicorn 单 loop，与本节一致）。``_run``/``_stream_text``
    的两段式只适合断言缓冲行内内容的用例。
    """
    provider, up = two_accounts
    first = json.dumps({"candidates": [{"content": {"parts": [{"text": "hello"}]}}]})
    second = json.dumps({"usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}})
    up.plan["tok-u@x.com"] = (
        200,
        _ChunkedBody([f"data: {first}\n\n".encode(), f"data: {second}\n\n".encode()]),
        {"content-type": "text/event-stream"},
    )
    _patch_token(monkeypatch)

    async def _forward_and_collect() -> str:
        from buddy_proxy.antigravity.provider import MODELS

        body = {**_BODY, "model": f"antigravity/{MODELS[0]['id']}", "stream": True}
        sr = await provider.forward(body, "openai")
        assert sr.status_code == 200
        chunks = []
        async for chunk in sr.body_iterator:
            chunks.append(chunk.encode() if isinstance(chunk, str) else chunk)
        return b"".join(chunks).decode()

    text = asyncio.run(_forward_and_collect())
    assert "hello" in text
    assert '"usage"' in text and '"prompt_tokens": 1' in text  # 第二事件（闸门缓冲行之后）没有丢


def test_stream_gate_switches_on_in_band_429(two_accounts, monkeypatch):
    """HTTP 200 但首条 SSE 事件是 429 error：未出字节，冷却换号。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, _SSE_429, {})
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    text = _stream_text(sr)
    assert "RESOURCE_EXHAUSTED" not in text  # 客户端没吃到失败账号的错误
    # 带内 429 与 HTTP 429 走同一套模型级冷却，闸门路径不漏。
    assert failover.model_cooldown_left(
        "u@x.com", _default_upstream_model())[0] > 0
    assert failover.cooldown_left("u@x.com") == (0.0, "")
    assert up.calls == ["tok-u@x.com", "tok-v@y.com"]


def test_stream_gate_committed_after_candidates(two_accounts, monkeypatch):
    """首条事件已是 candidates：语义已至，缓冲行补放、绝不换号。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, _SSE_OK, {})
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    text = _stream_text(sr)
    assert "pong" in text and "data:" in text
    assert failover.cooldown_left("u@x.com") == (0.0, "")
    assert up.calls == ["tok-u@x.com"]


def test_stream_gate_eof_switches_without_cooldown(two_accounts, monkeypatch):
    """语义事件前 EOF（假成功）：换号但不冷却（可能只是网络抖动）。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, b"", {})
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    assert "pong" in _stream_text(sr)
    assert failover.cooldown_left("u@x.com") == (0.0, "")
    assert up.calls == ["tok-u@x.com", "tok-v@y.com"]


def test_stream_gate_buffer_cap_committed(two_accounts, monkeypatch):
    """闸门缓冲超过上限：按 committed 透传放行，不无界攒内存也不冷却。"""
    import buddy_proxy.antigravity.provider as prov
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    keepalive = b": keepalive\n\n" * (prov._GATE_BUFFER_MAX_LINES + 10)
    up.plan["tok-u@x.com"] = (200, keepalive + _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    assert failover.cooldown_left("u@x.com") == (0.0, "")  # 放行不冷却
    assert up.calls == ["tok-u@x.com"]  # 不换号


# ---------------------------------------------------------------------------
# failover 模块本身
# ---------------------------------------------------------------------------

def test_mark_cooldown_retry_after_clamp():
    from buddy_proxy.antigravity import failover

    failover.mark_cooldown("a", retry_after="2", quota=True)
    assert 1 < failover.cooldown_left("a")[0] <= 2
    failover.mark_cooldown("b", retry_after="99999999", quota=True)  # 离谱值 → 钳 7d
    assert failover.cooldown_left("b")[0] <= 7 * 86400
    failover.mark_cooldown("c", retry_after="not-a-number", quota=True)  # 解析失败 → 默认
    assert failover.cooldown_left("c")[0] <= failover._QUOTA_COOLDOWN_S
    failover.mark_cooldown("d")  # 非 quota → 60s 档
    left, kind = failover.cooldown_left("d")
    assert kind == "account" and left <= failover._ACCOUNT_COOLDOWN_S


def test_available_accounts_filters_and_orders():
    from buddy_proxy.antigravity import credentials as creds, failover

    creds.save_account_cred({"access_token": "a", "refresh_token": "r", "expiry": "2099-01-01T00:00:00+00:00", "email": "u@x.com"})
    creds.save_account_cred({"access_token": "b", "refresh_token": "r2", "expiry": "2099-01-01T00:00:00+00:00", "email": "v@y.com"})
    creds.save_account_cred({"access_token": "c", "refresh_token": "r3", "expiry": "2099-01-01T00:00:00+00:00", "email": "w@z.com"})

    failover.mark_cooldown("v@y.com", quota=True)
    ids = [a.id for a in failover.available_accounts()]
    assert ids == ["u@x.com", "w@z.com"]  # 冷却的中间账号被剔除，顺位不变


def test_accounts_status_shape():
    from buddy_proxy.antigravity import credentials as creds, failover

    creds.save_account_cred({"access_token": "a", "refresh_token": "r",
                             "expiry": "2099-01-01T00:00:00+00:00",
                             "email": "u@x.com", "project_id": "p1"})
    failover.mark_cooldown("u@x.com", quota=True)

    status = failover.accounts_status()
    assert status["enabled"] is True
    acct = status["accounts"][0]
    assert acct["id"] == "u@x.com" and acct["index"] == 1
    assert acct["project_id"] == "p1" and acct["token"] == "ok"
    assert acct["hours_left"] is not None  # token 有效期还早
    assert acct["cooling"] and acct["cooling"][0]["kind"] == "quota"

    creds.account_cred_path("u@x.com").unlink()
    assert failover.accounts_status()["enabled"] is False  # 自愈后空列表


def test_ui_endpoint_antigravity_accounts():
    """/ui/api/antigravity/accounts 冒烟：端点直连 failover.accounts_status。"""
    import asyncio
    import types

    from buddy_proxy.antigravity import credentials as creds
    from buddy_proxy.web.ui import channels as web_ui  # 2026-10-03 拆包后端点在 channels

    creds.save_account_cred({"access_token": "a", "refresh_token": "r",
                             "expiry": "2099-01-01T00:00:00+00:00",
                             "email": "u@x.com", "project_id": "p1"})
    request = types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"))
    out = asyncio.run(web_ui.ui_antigravity_accounts(request))
    assert out["enabled"] is True
    assert out["accounts"][0]["email"] == "u@x.com"


# ---------------------------------------------------------------------------
# 账号顺位调整（POST /ui/api/antigravity/accounts/order）
# ---------------------------------------------------------------------------

def _order_request(ids):
    import types

    class _Req:
        client = types.SimpleNamespace(host="127.0.0.1")

        async def json(self):
            return {"ids": ids}

    return _Req()


def test_reorder_endpoint_switches_failover_priority(two_accounts, monkeypatch):
    """端点重排 → 转发先打新首位账号；available_accounts 顺序随之变。"""
    import asyncio

    from buddy_proxy.antigravity import failover
    from buddy_proxy.web.ui import channels as web_ui

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, _gemini_ok("u"), {})
    up.plan["tok-v@y.com"] = (200, _gemini_ok("v"), {})
    _patch_token(monkeypatch)

    out = asyncio.run(web_ui.ui_antigravity_accounts_order(
        _order_request(["v@y.com", "u@x.com"])))
    assert [a["id"] for a in out["accounts"]] == ["v@y.com", "u@x.com"]
    assert out["accounts"][0]["index"] == 1  # UI 顺位即 failover 顺位
    assert [a.id for a in failover.available_accounts()] == ["v@y.com", "u@x.com"]

    resp = _run(provider)
    assert resp.status_code == 200
    assert up.calls == ["tok-v@y.com"]  # 重排后首选是 v，u 根本没被打


def test_reorder_endpoint_rejects_incomplete_ids(two_accounts):
    import asyncio

    from fastapi import HTTPException

    from buddy_proxy.web.ui import channels as web_ui

    with pytest.raises(HTTPException) as ei:
        asyncio.run(web_ui.ui_antigravity_accounts_order(
            _order_request(["v@y.com"])))  # 少一个账号
    assert ei.value.status_code == 400
    assert "完整的账号" in ei.value.detail["error"]["message"]


def test_reorder_endpoint_rejects_bad_shape(two_accounts):
    import asyncio

    from fastapi import HTTPException

    from buddy_proxy.web.ui import channels as web_ui

    for bad in ([1, 2], "u@x.com", None):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(web_ui.ui_antigravity_accounts_order(_order_request(bad)))
        assert ei.value.status_code == 400


# ---------------------------------------------------------------------------
# 删除账号（POST /ui/api/antigravity/accounts/delete）
# ---------------------------------------------------------------------------

def _delete_request(aid):
    import types

    class _Req:
        client = types.SimpleNamespace(host="127.0.0.1")

        async def json(self):
            return {"id": aid}

    return _Req()


def test_delete_endpoint_removes_account_and_cooldown(two_accounts):
    """删除账号：索引+cred 文件移除、冷却清掉、状态返回剩余账号；再删 404。"""
    import asyncio

    from fastapi import HTTPException

    from buddy_proxy.antigravity import credentials as creds, failover
    from buddy_proxy.web.ui import channels as web_ui

    failover.mark_cooldown("v@y.com", quota=True)  # 冷却残留应随删除一起清掉
    out = asyncio.run(web_ui.ui_antigravity_accounts_delete(_delete_request("v@y.com")))
    assert [a["id"] for a in out["accounts"]] == ["u@x.com"]
    assert creds.load_account_cred("v@y.com") is None
    assert not creds.account_cred_path("v@y.com").exists()
    assert failover.cooldown_left("v@y.com") == (0.0, "")

    with pytest.raises(HTTPException) as ei:
        asyncio.run(web_ui.ui_antigravity_accounts_delete(_delete_request("v@y.com")))
    assert ei.value.status_code == 404


def test_delete_endpoint_rejects_bad_shape(two_accounts):
    import asyncio

    from fastapi import HTTPException

    from buddy_proxy.web.ui import channels as web_ui

    for bad in (None, 123, "", "  "):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(web_ui.ui_antigravity_accounts_delete(_delete_request(bad)))
        assert ei.value.status_code == 400


def test_blacklist_cooldown_report_and_status_shape():
    """blacklist 类别进 accounts_status 透传、cooldown_report 里标「疑似拉黑」。"""
    from buddy_proxy.antigravity import credentials as creds, failover

    creds.save_account_cred({"access_token": "a", "refresh_token": "r",
                             "expiry": "2099-01-01T00:00:00+00:00",
                             "email": "u@x.com", "project_id": "p1"})
    failover.mark_cooldown("u@x.com", blacklist=True)

    acct = failover.accounts_status()["accounts"][0]
    assert acct["cooling"] == [{"kind": "blacklist", "minutes_left": pytest.approx(360, rel=0.01)}]
    report = failover.cooldown_report()
    assert "u@x.com 疑似拉黑剩" in report and report.endswith("h"), "6h 档按小时展示"
