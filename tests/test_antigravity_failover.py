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
    """按 Bearer token（=账号）路由 canned 响应，记录调用顺序。

    ``plan`` 给单个响应；``sequences`` 给按账号的响应列表（每次调用弹一个，
    弹空后沿用最后一个）——测「同账号重发」时用后者让第 2 次返回不同结果。
    """

    def __init__(self):
        self.calls: list[str] = []  # 命中的 token 顺序
        self.plan: dict[str, tuple] = {}  # token -> (status, payload, headers)
        self.sequences: dict[str, list] = {}  # token -> [tuple, ...]
        self._seq_idx: dict[str, int] = {}

    def _pick(self, tok: str):
        seq = self.sequences.get(tok)
        if seq is not None:
            i = self._seq_idx.get(tok, 0)
            self._seq_idx[tok] = i + 1
            return seq[min(i, len(seq) - 1)]
        return self.plan[tok]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        tok = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.calls.append(tok)
        status, payload, headers = self._pick(tok)
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
    # 空流重发走独立连接池（见 provider._fresh_client）：夹具必须一并注入，
    # 否则重发会打到真实网络。
    provider._retry_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), timeout=prov._TIMEOUT)
    # 收尾的配额水位也有独立长连接；不注入会在成功后直连 Google。
    # 与 upstream 分开，水位查询不能混进这些用例断言的转发调用序列。
    probe = httpx.MockTransport(lambda request: httpx.Response(200, json={"models": {}}))
    provider._probe_client = httpx.AsyncClient(transport=probe, timeout=prov._TIMEOUT)
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
    ``429 RESOURCE_EXHAUSTED``（该模型 2026-11-15 已从 models.json 删除，
    这里仍用「未知模型 id」直接构造请求验 429 隔离逻辑，不依赖表里有它）；旧逻辑按账号级冷却 5min，随后
    ``available_accounts()`` 为空，连正常的 ``gemini-3.8-flash`` 都 429。
    修复后 429 只冷却 (账号, 模型)，别的模型仍能用同一批账号。
    """
    import httpx
    from fastapi import HTTPException

    from buddy_proxy.antigravity import failover
    from buddy_proxy.antigravity.convert import apply_effort_suffix
    from buddy_proxy.antigravity.provider import MODELS

    by_id = {m["id"]: m for m in MODELS}
    model_id = "gemini-legacy-429"  # 模拟「已下架但仍在表里」的坏条目
    bad = apply_effort_suffix({"id": model_id, "efforts": ["medium"],
                               "default_effort": "medium"}, None)
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

    provider._model_by_id[model_id] = {"id": model_id, "efforts": ["medium"],
                                       "default_effort": "medium", "group": "gemini"}
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(route),
                                         timeout=prov._TIMEOUT)
    provider._retry_client = httpx.AsyncClient(
        transport=httpx.MockTransport(route), timeout=prov._TIMEOUT)
    _patch_token(monkeypatch)

    # 1) 坏模型：两个账号都 429，按模型冷却。
    with pytest.raises(HTTPException) as ei:
        asyncio.run(provider.forward(
            {**_BODY, "model": f"antigravity/{model_id}", "stream": False},
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


def test_stream_gate_thinking_then_text_single_loop(two_accounts, monkeypatch):
    """纯 thinking 不算 commit：闸门续读同一条流直到真文本（单 loop，同生产）。

    thinking 帧留在缓冲里，既不丢弃也不提前透传；真文本到达后连同它一起按
    committed 补放——即使上游用的是不可重放的 chunked body。
    """
    provider, up = two_accounts
    think = json.dumps({"candidates": [{"content": {"parts": [{"text": "think", "thought": True}]}}]})
    text = json.dumps({"candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}]})
    up.plan["tok-u@x.com"] = (
        200,
        _ChunkedBody([f"data: {think}\n\n".encode(), f"data: {text}\n\n".encode()]),
        {"content-type": "text/event-stream"},
    )
    _patch_token(monkeypatch)

    async def _forward_and_collect() -> str:
        from buddy_proxy.antigravity.provider import MODELS

        body = {**_BODY, "model": f"antigravity/{MODELS[0]['id']}", "stream": True}
        sr = await provider.forward(body, "openai")
        chunks = []
        async for chunk in sr.body_iterator:
            chunks.append(chunk.encode() if isinstance(chunk, str) else chunk)
        return b"".join(chunks).decode()

    text_out = asyncio.run(_forward_and_collect())
    assert "hello" in text_out and "think" in text_out  # thinking 随 commit 补放，不丢
    assert up.calls == ["tok-u@x.com"]


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
    """语义事件前干净 EOF（假成功）：先同账号重发一次，仍空才换号，且不冷却。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, b"", {})  # 两次都空 EOF
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    assert "pong" in _stream_text(sr)
    assert failover.cooldown_left("u@x.com") == (0.0, "")
    assert up.calls == ["tok-u@x.com", "tok-u@x.com", "tok-v@y.com"]


# 上游偶发「只想了没答」：output 0 token、finish=STOP、parts 里只有 thought。
# 旧闸门一见 candidates 就 commit，客户端拿到「成功但没有内容」（dsh →
# EMPTY_RESPONSE 重试 5 次后中止整轮）；现在按空内容换号。
_SSE_THOUGHT_ONLY = _sse([
    json.dumps({"candidates": [{"content": {"parts": [{"text": "plan...", "thought": True}],
                                           "role": "model"},
                                "finishReason": "STOP"}]}),
    json.dumps({"usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 0}}),
])
# 空 parts 且没有 finishReason 的纯 thinking 帧，最后靠 EOF 收尾
_SSE_THOUGHT_THEN_EOF = _sse([
    json.dumps({"candidates": [{"content": {"parts": [{"text": "hmm", "thought": True}]}}]}),
])


@pytest.mark.parametrize("empty_body", [_SSE_THOUGHT_ONLY, _SSE_THOUGHT_THEN_EOF])
def test_stream_gate_empty_retries_same_account_first(two_accounts, monkeypatch, empty_body):
    """上游只给 thinking/空内容：先同账号重发一次，救回来就不换号。

    20:11/20:33 两次中断的根因就是偶发空流；直接换号会撞上兜底通道 402。
    """
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    # 主账号第 1 次空、第 2 次正常 → 重发救回，不动备用账号
    up.sequences["tok-u@x.com"] = [(200, empty_body, {}), (200, _SSE_OK, {})]
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    text = _stream_text(sr)
    assert "pong" in text
    assert "plan..." not in text and "hmm" not in text  # 首次空流的 thought 不得透传
    assert failover.cooldown_left("u@x.com") == (0.0, "")
    assert up.calls == ["tok-u@x.com", "tok-u@x.com"]  # 同账号重发，未换号


@pytest.mark.parametrize("empty_body", [_SSE_THOUGHT_ONLY, _SSE_THOUGHT_THEN_EOF])
def test_stream_gate_empty_retry_exhausted_switches_account(two_accounts, monkeypatch, empty_body):
    """同账号重发仍空：才换下一个账号，且不冷却主账号。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, empty_body, {})  # 两次都空
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    assert "pong" in _stream_text(sr)
    assert failover.cooldown_left("u@x.com") == (0.0, "")
    assert up.calls == ["tok-u@x.com", "tok-u@x.com", "tok-v@y.com"]


def test_stream_gate_empty_all_accounts_raises(two_accounts, monkeypatch):
    """所有账号重发后仍只给 thinking：明确 502 no content。"""
    from fastapi import HTTPException

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, _SSE_THOUGHT_ONLY, {})
    up.plan["tok-v@y.com"] = (200, _SSE_THOUGHT_ONLY, {})
    _patch_token(monkeypatch)

    with pytest.raises(HTTPException) as ei:
        _run(provider, stream=True)
    assert ei.value.status_code == 502
    assert "returned no content" in json.dumps(ei.value.detail, ensure_ascii=False)
    # 每账号各 2 次（首判 + 重发）
    assert up.calls == ["tok-u@x.com", "tok-u@x.com", "tok-v@y.com", "tok-v@y.com"]
    # 空内容必须是独立错误类型：换档层靠它区分「上游没产出」与「通道真故障」，
    # 前者只走软退避、不打 5 分钟冷却（见 core.cooldown.mark_empty_content）。
    assert ei.value.detail["error"]["type"] == "upstream_empty_content"


def test_prompt_block_reason_is_deterministic_and_not_retried(two_accounts, monkeypatch):
    """promptFeedback.blockReason（内容拦截）：不重发、不换号、不冷却账号。

    上游明确表示「这次请求不产出内容」，重发只会再被拒一次、白烧上游配额；
    把它当空流重试正是「重发 1/1 之后还是空」的成因之一。
    """
    from fastapi import HTTPException

    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    blocked = _sse([json.dumps({"promptFeedback": {"blockReason": "SAFETY"}})])
    up.plan["tok-u@x.com"] = (200, blocked, {})
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    with pytest.raises(HTTPException) as ei:
        _run(provider, stream=True)
    assert ei.value.status_code == 400
    assert "SAFETY" in json.dumps(ei.value.detail, ensure_ascii=False)
    # 只打了一次：没有重发，也没换到 v 账号
    assert up.calls == ["tok-u@x.com"]
    assert failover.cooldown_left("u@x.com") == (0.0, "")


def test_empty_retry_uses_fresh_client(two_accounts, monkeypatch):
    """空流重发必须走独立连接池（排除复用坏 keep-alive 连接这个成因）。"""
    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, b"", {})
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    used: list[httpx.AsyncClient] = []
    orig_send = provider._send_with_fallback

    async def spy(client, *a, **kw):
        used.append(client)
        return await orig_send(client, *a, **kw)

    monkeypatch.setattr(provider, "_send_with_fallback", spy)
    sr = _run(provider, stream=True)
    assert "pong" in _stream_text(sr)
    # 第 1 次用共享 client，重发用 _retry_client
    assert used[0] is provider._client
    assert used[1] is provider._retry_client
    assert used[0] is not used[1]


def test_stream_gate_thinking_then_text_commits(two_accounts, monkeypatch):
    """先 thinking 后真文本：仍在闸门内，见到 text 才 commit，切回同一账号。"""
    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, _sse([
        json.dumps({"candidates": [{"content": {"parts": [{"text": "think", "thought": True}]}}]}),
        json.dumps({"candidates": [{"content": {"parts": [{"text": "pong"}]},
                                    "finishReason": "STOP"}]}),
    ]), {})
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    text = _stream_text(_run(provider, stream=True))
    assert "pong" in text
    assert up.calls == ["tok-u@x.com"]


def test_nonstream_empty_retries_same_account_first(two_accounts, monkeypatch):
    """非流式同理：首次空内容先同账号重发一次，救回就不换号。"""
    provider, up = two_accounts
    up.sequences["tok-u@x.com"] = [
        (200, {"candidates": [{
            "content": {"parts": [{"text": "plan...", "thought": True}], "role": "model"},
            "finishReason": "STOP"}]}, {}),
        (200, _gemini_ok(), {}),
    ]
    up.plan["tok-v@y.com"] = (200, _gemini_ok(), {})
    _patch_token(monkeypatch)

    resp = _run(provider)
    assert resp.status_code == 200 and b"pong" in resp.body
    assert up.calls == ["tok-u@x.com", "tok-u@x.com"]  # 同账号重发


# 20:11/20:33 两次中断的真实形状：流里连 candidates 都没出现（只有 usage /
# 心跳），随后干净 EOF —— _gate_first_event 判 eof=True, empty=False,
# finishReason=n/a。这正是必须重发的那类（只判 empty 会漏）。
_SSE_BARE_EOF = _sse([
    json.dumps({"usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 0}}),
])


def test_stream_gate_bare_eof_retries_same_account(two_accounts, monkeypatch):
    """finishReason=n/a 的干净断流：同账号重发一次救回，不换号不冷却。

    退化护栏：这是 20:11、20:33 两次真实中断的形状；若重发条件只判
    gate.empty，这类 empty=False 的断流会直接换号 → 撞兜底 402 → 双冷却。
    """
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.sequences["tok-u@x.com"] = [(200, _SSE_BARE_EOF, {}), (200, _SSE_OK, {})]
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    assert "pong" in _stream_text(sr)
    assert failover.cooldown_left("u@x.com") == (0.0, "")
    assert up.calls == ["tok-u@x.com", "tok-u@x.com"]  # 同账号重发


def test_empty_retry_second_429_cooldowns_and_switches(two_accounts, monkeypatch):
    """重发拿到 429：折算成带内错误，主流程按 (账号,模型) 冷却并换号，不崩。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.sequences["tok-u@x.com"] = [
        (200, _SSE_THOUGHT_THEN_EOF, {}),  # 首次空
        (429, {"error": {"code": 429, "message": "quota"}}, {}),  # 重发 429
    ]
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    text = _stream_text(_run(provider, stream=True))
    assert "pong" in text
    model = _default_upstream_model()
    assert failover.model_cooldown_left("u@x.com", model)[0] > 0  # 429 冷却了模型
    assert up.calls[-1] == "tok-v@y.com"  # 换到备用号


def test_empty_retry_second_transport_error_cooldowns(two_accounts, monkeypatch):
    """重发遇网络错：折算成 504 冷却换号，不抛裸异常。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.sequences["tok-u@x.com"] = [
        (200, _SSE_THOUGHT_THEN_EOF, {}),
        (0, httpx.ConnectError("boom"), {}),  # 重发网络错 → _UpstreamUnavailable
    ]
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    text = _stream_text(_run(provider, stream=True))
    assert "pong" in text
    assert failover.cooldown_left("u@x.com")[0] > 0  # 504 冷却


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


# ---------------------------------------------------------------------------
# 5xx 换号 + 重置倒计时解析 + 账号频控排队保护
# ---------------------------------------------------------------------------

def test_http_5xx_switches_account_and_cools(two_accounts, monkeypatch):
    """HTTP 502/503（两个 endpoint 都 5xx）必须冷却当前账号并切到下一个，不直接炸给客户端。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (503, {"error": {"code": 503, "message": "overloaded"}}, {})
    up.plan["tok-v@y.com"] = (200, _gemini_ok("recovered"), {})
    _patch_token(monkeypatch)

    resp = _run(provider, stream=False)
    assert resp.status_code == 200
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == "recovered"
    # u@x.com 打满两个 endpoint（daily + prod），随后切到 v@y.com 成功
    assert up.calls == ["tok-u@x.com", "tok-u@x.com", "tok-v@y.com"]
    left, kind = failover.cooldown_left("u@x.com")
    assert left > 0 and kind == "account"


def test_in_band_5xx_switches_account_and_cools(two_accounts, monkeypatch):
    """HTTP 200 流首事件为 500/503 error：闸门未出字节，必须冷却并换号。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    sse_500 = _sse([json.dumps({"error": {"code": 500, "message": "internal error"}})])
    up.plan["tok-u@x.com"] = (200, sse_500, {})
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    assert "pong" in _stream_text(sr)
    assert up.calls == ["tok-u@x.com", "tok-v@y.com"]
    left, kind = failover.cooldown_left("u@x.com")
    assert left > 0 and kind == "account"


def test_429_resets_in_message_sets_cooldown_duration(two_accounts, monkeypatch):
    """无 Retry-After 头时，从 'Resets in 4h29m15s.' 解析冷却时长而非死守 5min。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (
        429,
        {"error": {"code": 429, "message": "Individual quota reached. Resets in 4h29m15s."}},
        {},
    )
    up.plan["tok-v@y.com"] = (200, _gemini_ok(), {})
    _patch_token(monkeypatch)

    resp = _run(provider, stream=False)
    assert resp.status_code == 200
    left, kind = failover.model_cooldown_left("u@x.com", _default_upstream_model())
    expected = 4 * 3600 + 29 * 60 + 15
    assert kind == "quota" and expected - 5 <= left <= expected + 1


def test_rate_limiter_queues_when_burst_exceeded(two_accounts, monkeypatch):
    """单账号超出突发频率时排队等待后放行，而不是直接 429 或打爆上游。"""
    from buddy_proxy.antigravity import credentials as creds
    from buddy_proxy.antigravity.provider import AntigravityProvider, MODELS
    from buddy_proxy.antigravity.rate_limit import AccountRateLimiter, RateLimitConfig

    # 仅保留单个账号，验证超频后确实发生异步排队并最终成功
    creds.delete_account("v@y.com")
    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, _gemini_ok("queued-ok"), {})
    _patch_token(monkeypatch)

    fake_now = [100.0]

    async def fake_sleep(dt: float) -> None:
        fake_now[0] += dt

    monkeypatch.setattr("buddy_proxy.antigravity.rate_limit.asyncio.sleep", fake_sleep)
    provider._rate_limiter = AccountRateLimiter(
        RateLimitConfig(max_concurrent=2, rpm=10, burst_10s=1, min_interval_s=0.0, queue_timeout_s=30.0),
        clock=lambda: fake_now[0],
    )

    body = {**_BODY, "model": f"antigravity/{MODELS[0]['id']}", "stream": False}
    r1 = asyncio.run(provider.forward(body, "openai"))
    assert r1.status_code == 200
    assert fake_now[0] == 100.0

    # 第 2 次在同一秒内到来，命中 burst_10s=1，应当推进时钟等待 10s 后成功
    r2 = asyncio.run(provider.forward(body, "openai"))
    assert r2.status_code == 200
    assert fake_now[0] >= 110.0
    assert up.calls == ["tok-u@x.com", "tok-u@x.com"]


def test_rate_limiter_prefers_ready_account_without_waiting(two_accounts, monkeypatch):
    """账号 #1 正在受频控保护排队时，多账号请求优先走无需等待的账号 #2。"""
    from buddy_proxy.antigravity.provider import MODELS
    from buddy_proxy.antigravity.rate_limit import AccountRateLimiter, RateLimitConfig

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, _gemini_ok("from-u"), {})
    up.plan["tok-v@y.com"] = (200, _gemini_ok("from-v"), {})
    _patch_token(monkeypatch)

    fake_now = [200.0]

    async def fake_sleep(dt: float) -> None:
        fake_now[0] += dt

    monkeypatch.setattr("buddy_proxy.antigravity.rate_limit.asyncio.sleep", fake_sleep)
    provider._rate_limiter = AccountRateLimiter(
        RateLimitConfig(max_concurrent=2, rpm=10, burst_10s=1, min_interval_s=0.0, queue_timeout_s=30.0),
        clock=lambda: fake_now[0],
    )

    body = {**_BODY, "model": f"antigravity/{MODELS[0]['id']}", "stream": False}
    r1 = asyncio.run(provider.forward(body, "openai"))
    r2 = asyncio.run(provider.forward(body, "openai"))
    assert r1.status_code == 200 and r2.status_code == 200
    assert up.calls == ["tok-u@x.com", "tok-v@y.com"]
    assert fake_now[0] == 200.0, "账号 #2 空闲时应立即切换，不应傻等账号 #1"


def test_rate_limiter_stream_releases_concurrency_on_close(two_accounts, monkeypatch):
    """流式响应占用的并发槽位必须到流关闭时才释放，防止并发流堆积打爆上游。"""
    from buddy_proxy.antigravity import credentials as creds
    from buddy_proxy.antigravity.provider import MODELS
    from buddy_proxy.antigravity.rate_limit import AccountRateLimiter, RateLimitConfig

    creds.delete_account("v@y.com")
    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    provider._rate_limiter = AccountRateLimiter(
        RateLimitConfig(max_concurrent=1, rpm=10, burst_10s=5, min_interval_s=0.0, queue_timeout_s=0.05),
    )

    async def _go():
        body = {**_BODY, "model": f"antigravity/{MODELS[0]['id']}", "stream": True}
        sr1 = await provider.forward(body, "openai")
        # 第一条流尚未消费/关闭，并发上限 1 下第二条在短超时内应排队失败（429 排队超时）
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            await provider.forward(body, "openai")
        assert ei.value.status_code == 429

        # 消费并关闭第一条流后，并发槽位释放，下一条请求立即成功
        chunks = []
        async for piece in sr1.body_iterator:
            chunks.append(piece if isinstance(piece, bytes) else piece.encode())
        assert b"pong" in b"".join(chunks)

        sr2 = await provider.forward(body, "openai")
        assert sr2.status_code == 200
        async for _ in sr2.body_iterator:
            pass

    asyncio.run(_go())
    assert up.calls == ["tok-u@x.com", "tok-u@x.com"]


def test_default_rate_limiter_does_not_throttle_normal_agent_burst(monkeypatch):
    """默认阈值必须放行正常高频 agent 工具循环（如 10s 内 6 次、并发 4 路），不得误伤。"""
    from buddy_proxy.antigravity.rate_limit import AccountRateLimiter

    fake_now = [500.0]

    async def fail_if_slept(_dt: float) -> None:
        raise AssertionError("正常突发不应触发排队 sleep")

    monkeypatch.setattr("buddy_proxy.antigravity.rate_limit.asyncio.sleep", fail_if_slept)
    limiter = AccountRateLimiter(clock=lambda: fake_now[0])

    async def _go():
        leases = []
        # 同一秒连续 5 路并发（覆盖 p99 并发与 10s 突发）应全部零等待拿到租约
        for _ in range(5):
            lease = await limiter.acquire("u@x.com")
            assert lease is not None
            leases.append(lease)
        for lease in leases:
            lease.release()

    asyncio.run(_go())


