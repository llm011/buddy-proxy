"""火山方舟（Volcengine Ark）provider 单测 —— 全离线，不访问上游。

锁定的契约（2026-10-23 接入时实测确认）：

1. **模型与 key 不进代码**：模型表完全来自 ``~/.buddy-proxy/ark.json``，
   key 来自 ``ARK_API_KEY`` 或 ``~/.buddy-proxy/ark_api_key``。配置缺失/损坏
   时模型表为空而**不抛**，可操作提示留给 ``ensure_auth``。
   为什么必须这样：火山方舟按自定义推理接入点（``ep-...``）计费，接入点 id
   是账号私有资源；本账号用裸模型名会被上游拒为
   ``InvalidEndpointOrModel.ModelIDAccessDisabled``。
2. **三协议全支持**（用户明确要求）：openai / responses / anthropic 三种客户端
   形状都要正确输出，anthropic 与 openai 同等地位。
3. **上游 Responses 优先、遇不支持自动降级 chat**；只对「接入点干不了这件事」
   的 404 ``InvalidEndpointOrModel*`` 降级，其余 4xx 原样透传。
4. **首事件闸门**：上游 200 但带内报错必须还原成 HTTP 错误，不能变成 200 空流。
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import HTTPException

from buddy_proxy.providers import ark as ark_mod
from buddy_proxy.providers.ark import (
    ArkProvider,
    config_file_path,
    parse_models,
    resolve_api_key,
    secret_file_path,
)

# ---------------------------------------------------------------------------
# 上游假响应
# ---------------------------------------------------------------------------

_RESPONSES_SSE = (
    'event: response.created\n'
    'data: {"type":"response.created","response":{"id":"resp_1","object":"response"}}\n\n'
    'event: response.output_item.added\n'
    'data: {"type":"response.output_item.added","output_index":0,'
    '"item":{"id":"rs_1","type":"reasoning","summary":[]}}\n\n'
    'event: response.reasoning_summary_text.delta\n'
    'data: {"type":"response.reasoning_summary_text.delta","item_id":"rs_1",'
    '"delta":"thinking..."}\n\n'
    'event: response.output_text.delta\n'
    'data: {"type":"response.output_text.delta","item_id":"msg_1","delta":"po"}\n\n'
    'event: response.output_text.delta\n'
    'data: {"type":"response.output_text.delta","item_id":"msg_1","delta":"ng"}\n\n'
    'event: response.completed\n'
    'data: {"type":"response.completed","response":{"id":"resp_1","object":"response",'
    '"status":"completed","output":[{"id":"msg_1","type":"message","role":"assistant",'
    '"content":[{"type":"output_text","text":"pong"}]}],'
    '"usage":{"input_tokens":36,"output_tokens":7,"total_tokens":43,'
    '"input_tokens_details":{"cached_tokens":5}}}}\n\n'
    "data: [DONE]\n\n"
)

_CHAT_SSE = (
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\n\n'
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,'
    '"choices":[{"index":0,"delta":{"reasoning_content":"think"}}]}\n\n'
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,'
    '"choices":[{"index":0,"delta":{"content":"po"}}]}\n\n'
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,'
    '"choices":[{"index":0,"delta":{"content":"ng"}}]}\n\n'
    'data: {"id":"c1","object":"chat.completion.chunk","created":1,'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":36,"completion_tokens":7,"total_tokens":43,'
    '"prompt_tokens_details":{"cached_tokens":5}}}\n\n'
    "data: [DONE]\n\n"
)

_CHAT_TOOL_SSE = (
    'data: {"id":"c2","object":"chat.completion.chunk","created":1,'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\n\n'
    'data: {"id":"c2","object":"chat.completion.chunk","created":1,'
    '"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1",'
    '"type":"function","function":{"name":"get_weather","arguments":""}}]}}]}\n\n'
    'data: {"id":"c2","object":"chat.completion.chunk","created":1,'
    '"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
    '"function":{"arguments":"{\\"city\\": \\"Beijing\\"}"}}]}}]}\n\n'
    'data: {"id":"c2","object":"chat.completion.chunk","created":1,'
    '"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
    "data: [DONE]\n\n"
)


def _mock_transport(handler):
    """把一个「按 URL 分派」的 handler 包成 provider 内部的 AsyncClient。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# 夹具：隔离配置目录
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """隔离状态目录 + 干净的 ARK_* 环境变量。"""
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(tmp_path / "state"))
    for name in ("ARK_API_KEY", "ARK_BASE_URL", "ARK_UPSTREAM_PROTOCOL"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)

    def write(config: dict | None = None, key: str | None = None) -> None:
        if config is not None:
            config_file_path().write_text(json.dumps(config))
        if key is not None:
            secret_file_path().write_text(key + "\n")

    return write


def _provider(monkeypatch, transport, config=None, key="test-key") -> ArkProvider:
    """构造 provider 并把它的 httpx client 换成 MockTransport。"""
    if config is not None:
        config_file_path().write_text(json.dumps(config))
    if key is not None:
        secret_file_path().write_text(key + "\n")
    p = ArkProvider()
    p._client = _mock_transport(transport)
    return p


_BASE_CFG = {"models": [{"id": "ds-flash", "endpoint": "ep-test"}]}


# ---------------------------------------------------------------------------
# 1. 本地配置：代码里不出现模型与 key
# ---------------------------------------------------------------------------


def test_no_hardcoded_model_or_key_in_source():
    """源码里不许出现接入点或默认模型表——这是本通道的硬约束。"""
    src = ark_mod.__file__
    text = open(src, encoding="utf-8").read()
    # 真实接入点的形状是 ep-<14 位日期>-<随机>；源码里只能出现文档占位符
    import re

    real_like = re.findall(r"ep-\d{8,}", text)
    assert not real_like, f"源码里出现了疑似真实接入点: {real_like}"
    # 没有 DEFAULT_MODELS 这类内置模型表
    assert not hasattr(ark_mod, "DEFAULT_MODELS")


def test_config_missing_yields_empty_models(cfg):
    """配置文件不存在时模型表为空，且不抛异常。"""
    p = ArkProvider()
    assert list(p.models()) == []


def test_config_broken_json_does_not_raise(cfg):
    """ark.json 损坏 → 空表 + 不抛（一份可选配置不该炸掉整个代理）。"""
    config_file_path().write_text("{not json")
    assert list(ArkProvider().models()) == []


def test_parse_models_rejects_invalid_entries():
    """缺 id / 缺上游名 / id 重复的条目逐个跳过，其它条目照常保留。"""
    entries = parse_models({"models": [
        {"id": "ok", "endpoint": "ep-1"},
        {"id": "", "endpoint": "ep-2"},          # 缺 id
        {"id": "no-upstream"},                    # 缺 endpoint/model
        {"id": "ok", "endpoint": "ep-3"},         # 重复 id
        {"id": "bare", "model": "some-model"},    # endpoint 的等价写法
    ]})
    assert [e["id"] for e in entries] == ["ok", "bare"]
    assert entries[0]["upstream"] == "ep-1"
    assert entries[1]["upstream"] == "some-model"


def test_models_include_display_fields(cfg):
    """name/description/上限等透传到 /v1/models。"""
    p = _provider(None, None, config={"models": [{
        "id": "ds-flash", "endpoint": "ep-test", "name": "DeepSeek Flash",
        "description": "desc", "max_input": 1000, "tool_call": True,
    }]})
    item = p.models()[0]
    assert item["id"] == "ds-flash"
    assert item["name"] == "DeepSeek Flash"
    assert item["owned_by"] == "ark"
    assert item["max_input"] == 1000
    assert item["tool_call"] is True
    # 接入点不能泄漏进对外模型表
    assert "ep-test" not in json.dumps(item)


def test_api_key_priority_env_over_file(cfg, monkeypatch):
    cfg(key="from-file")
    assert resolve_api_key() == "from-file"
    monkeypatch.setenv("ARK_API_KEY", "from-env")
    assert resolve_api_key() == "from-env"


def test_api_key_file_accepts_name_value_form(cfg):
    """key 文件允许 ``ARK_API_KEY=xxx`` 形式（复用 zcode 的读取器）。"""
    secret_file_path().write_text("ARK_API_KEY=abc123\n")
    assert resolve_api_key() == "abc123"


def test_ensure_auth_missing_key_points_at_file(cfg):
    cfg({"models": [{"id": "x", "endpoint": "ep-1"}]})
    with pytest.raises(HTTPException) as exc:
        ArkProvider().ensure_auth()
    assert exc.value.status_code == 401
    assert "ark_api_key" in exc.value.detail["error"]["message"]


def test_ensure_auth_missing_models_points_at_config(cfg):
    cfg(key="k")
    with pytest.raises(HTTPException) as exc:
        ArkProvider().ensure_auth()
    assert exc.value.status_code == 401
    assert "ark.json" in exc.value.detail["error"]["message"]


def test_accepts_model_scoped_to_configured_ids(cfg):
    p = ArkProvider()
    cfg(_BASE_CFG)
    p = ArkProvider()
    assert p.accepts_model("ds-flash")
    assert p.accepts_model("ark/ds-flash")
    assert not p.accepts_model("ep-test")  # 上游真名不是对外模型名


def test_unknown_model_returns_400(cfg, monkeypatch):
    p = _provider(monkeypatch, lambda r: httpx.Response(200), config=_BASE_CFG)
    p.ensure_auth()
    with pytest.raises(HTTPException) as exc:
        p._entry_for("nope")
    assert exc.value.status_code == 400
    assert "ds-flash" in exc.value.detail["error"]["message"]


def test_quota_is_none(cfg):
    """火山方舟没有可查的余额接口 —— 不出额度卡，也不编数字。"""
    cfg(_BASE_CFG, key="k")
    assert ArkProvider().quota() is None


# ---------------------------------------------------------------------------
# 2. 三协议出口
# ---------------------------------------------------------------------------


async def _drain(response) -> str:
    out = []
    async for chunk in response.body_iterator:
        out.append(chunk if isinstance(chunk, str) else chunk.decode())
    return "".join(out)


def _run(coro_fn):
    """同步跑一个协程（仓库没装 pytest-asyncio，既有测试都用 asyncio.run）。

    ⚠️ 发请求与消费 ``body_iterator`` 必须在**同一个** ``asyncio.run`` 里：
    分两次 run 会在第一次结束时触发 ``shutdown_asyncgens``，把仍挂起的上游
    生成器关掉，第二次读到的就是空流（见 tests/test_openai_stream_first_chunk
    的历史坑）。
    """
    import asyncio

    return asyncio.run(coro_fn())


async def _forward_and_drain(provider, body, protocol):
    """转发并消费完整响应体，返回 ``(resp, text)``。

    ``JSONResponse``（非流式/错误体）没有 ``body_iterator``，其 ``body`` 已是
    完整字节，直接解码即可。
    """
    resp = await provider.forward(body, protocol)
    if not hasattr(resp, "body_iterator"):
        return resp, resp.body.decode()
    return resp, await _drain(resp)


def _run_forward(provider, body, protocol):
    """转发并消费，返回 ``(resp, text)``；异常直接抛给调用方。"""
    return _run(lambda: _forward_and_drain(provider, body, protocol))


def _run_forward_capture(provider, body, protocol):
    """转发并消费，返回 ``(resp, text, exc)``。

    首帧闸门的异常发生在**消费** ``body_iterator`` 阶段而不是 ``forward()``
    返回时，所以必须把两段包在同一个事件循环里捕获。
    """

    async def _go():
        resp = await provider.forward(body, protocol)
        try:
            return resp, await _forward_and_drain_text(resp), None
        except Exception as exc:  # noqa: BLE001 - 断言用，原样交给调用方
            return resp, None, exc

    return _run(_go)


async def _forward_and_drain_text(resp) -> str:
    if not hasattr(resp, "body_iterator"):
        return resp.body.decode()
    return await _drain(resp)


def _collect_events(text: str) -> list[dict]:
    """把 SSE 文本拆成 ``[{"event": ..., "data": {...}}]``。"""
    events, name = [], ""
    for line in text.splitlines():
        if line.startswith("event:"):
            name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            raw = line[len("data:"):].strip()
            events.append({"event": name,
                           "data": None if raw == "[DONE]" else json.loads(raw)})
    return events


def test_openai_protocol_from_responses_upstream(cfg, monkeypatch):
    """openai 客户端 + responses 上游 → chat chunk（思考与正文都在）。"""
    p = _provider(monkeypatch, lambda r: httpx.Response(200, text=_RESPONSES_SSE),
                  config=_BASE_CFG)
    _resp, body = _run(lambda: _forward_and_drain(
        p, {"model": "ds-flash", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}, "openai"))
    chunks = [e["data"] for e in _collect_events(body) if e["data"]]
    assert any(c["choices"][0]["delta"].get("content") for c in chunks)
    assert "".join(c["choices"][0]["delta"].get("content") or ""
                   for c in chunks) == "pong"
    # 对外模型名，不是接入点
    assert {c["model"] for c in chunks} == {"ark/ds-flash"}
    # usage 从 response.completed 里取到
    assert any(c.get("usage") for c in chunks)


def test_responses_protocol_passthrough(cfg, monkeypatch):
    """responses 客户端 + responses 上游 → 原样透传（零转换）。"""
    p = _provider(monkeypatch, lambda r: httpx.Response(200, text=_RESPONSES_SSE),
                  config=_BASE_CFG)
    _resp, body = _run(lambda: _forward_and_drain(
        p, {"model": "ds-flash", "stream": True,
            "input": [{"role": "user", "content": "hi"}]}, "responses"))
    assert body == _RESPONSES_SSE


def test_anthropic_protocol_event_sequence_complete(cfg, monkeypatch):
    """anthropic 客户端：事件序列完整且以 message_stop 收尾（不能是残流）。"""
    p = _provider(monkeypatch, lambda r: httpx.Response(200, text=_RESPONSES_SSE),
                  config=_BASE_CFG)
    _resp, body = _run(lambda: _forward_and_drain(
        p, {"model": "ds-flash", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}, "anthropic"))
    events = _collect_events(body)
    names = [e["event"] for e in events if e["data"]]

    assert names[0] == "message_start"
    assert "message_stop" in names
    assert names.index("message_stop") == len(names) - 1

    start = events[names.index("message_start")]["data"]["message"]
    assert start["model"] == "ark/ds-flash"          # 对外名，不是 ep-test
    assert start["role"] == "assistant"

    # 思考 → thinking 块；正文 → text 块
    block_types = {}
    for e in events:
        d = e["data"]
        if e["event"] == "content_block_start":
            block_types[d["index"]] = d["content_block"]["type"]
    assert "thinking" in block_types.values()
    assert "text" in block_types.values()

    text = "".join(
        e["data"]["delta"]["text"] for e in events
        if e["event"] == "content_block_delta"
        and e["data"]["delta"].get("type") == "text_delta")
    assert text == "pong"

    # 每个开过的块都关上了
    opened = {e["data"]["index"] for e in events if e["event"] == "content_block_start"}
    closed = {e["data"]["index"] for e in events if e["event"] == "content_block_stop"}
    assert opened == closed


def test_anthropic_protocol_from_chat_upstream(cfg, monkeypatch):
    """chat 上游 + anthropic 客户端（dumate 同款事件机）。"""
    monkeypatch.setenv("ARK_UPSTREAM_PROTOCOL", "chat")
    p = _provider(monkeypatch, lambda r: httpx.Response(200, text=_CHAT_SSE),
                  config=_BASE_CFG)
    _resp, body = _run(lambda: _forward_and_drain(
        p, {"model": "ds-flash", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}, "anthropic"))
    events = _collect_events(body)
    names = [e["event"] for e in events if e["data"]]
    assert names[0] == "message_start"
    assert names[-1] == "message_stop"
    usage = [e["data"]["usage"] for e in events if e["event"] == "message_delta"][0]
    # input_tokens 要扣掉缓存命中（36 - 5），否则 Claude Code 会双倍计
    assert usage["input_tokens"] == 31
    assert usage["output_tokens"] == 7


def test_anthropic_tool_calls_become_tool_use(cfg, monkeypatch):
    """工具调用 → tool_use 块（Claude Code 依赖这条链路）。"""
    monkeypatch.setenv("ARK_UPSTREAM_PROTOCOL", "chat")
    p = _provider(monkeypatch, lambda r: httpx.Response(200, text=_CHAT_TOOL_SSE),
                  config=_BASE_CFG)
    _resp, body = _run(lambda: _forward_and_drain(
        p, {"model": "ds-flash", "stream": True,
            "messages": [{"role": "user", "content": "weather?"}]}, "anthropic"))
    events = _collect_events(body)
    tool_starts = [e["data"] for e in events if e["event"] == "content_block_start"
                   and e["data"]["content_block"]["type"] == "tool_use"]
    assert tool_starts and tool_starts[0]["content_block"]["name"] == "get_weather"
    args = "".join(
        e["data"]["delta"]["partial_json"] for e in events
        if e["event"] == "content_block_delta"
        and e["data"]["delta"].get("type") == "input_json_delta")
    assert json.loads(args) == {"city": "Beijing"}
    stop = [e["data"] for e in events if e["event"] == "message_delta"][0]
    assert stop["delta"]["stop_reason"] == "tool_use"


def test_anthropic_nonstream(cfg, monkeypatch):
    """非流式 anthropic：message 形状 + usage 扣缓存。"""
    p = _provider(monkeypatch, lambda r: httpx.Response(200, text=_RESPONSES_SSE),
                  config=_BASE_CFG)
    _resp, text = _run(lambda: _forward_and_drain(
        p, {"model": "ds-flash", "stream": False,
            "messages": [{"role": "user", "content": "hi"}]}, "anthropic"))
    payload = json.loads(text)
    assert payload["type"] == "message"
    assert payload["model"] == "ark/ds-flash"
    assert payload["content"][0]["text"] == "pong"
    assert payload["usage"]["input_tokens"] == 31
    assert payload["usage"]["cache_read_input_tokens"] == 5


def test_openai_nonstream_aggregates_usage(cfg, monkeypatch):
    p = _provider(monkeypatch, lambda r: httpx.Response(200, text=_RESPONSES_SSE),
                  config=_BASE_CFG)
    resp = _run(lambda: p.forward(
        {"model": "ds-flash", "stream": False,
         "messages": [{"role": "user", "content": "hi"}]}, "openai"))
    payload = json.loads(resp.body)
    assert payload["object"] == "chat.completion"
    assert payload["model"] == "ark/ds-flash"
    assert payload["choices"][0]["message"]["content"] == "pong"
    assert payload["usage"]["total_tokens"] == 43


def test_responses_nonstream_shape(cfg, monkeypatch):
    """responses 非流式：返回 response 对象（不是 chat.completion）。"""
    monkeypatch.setenv("ARK_UPSTREAM_PROTOCOL", "chat")
    p = _provider(monkeypatch, lambda r: httpx.Response(200, text=_CHAT_SSE),
                  config=_BASE_CFG)
    resp = _run(lambda: p.forward(
        {"model": "ds-flash", "stream": False,
         "input": [{"role": "user", "content": "hi"}]}, "responses"))
    payload = json.loads(resp.body)
    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    assert payload["model"] == "ark/ds-flash"
    texts = [c["text"] for item in payload["output"]
             for c in item.get("content", [])]
    assert texts == ["pong"]


# ---------------------------------------------------------------------------
# 3. 上游协议选择与降级
# ---------------------------------------------------------------------------


def _dispatching(seen: list[str], *, responses: httpx.Response,
                 chat: httpx.Response = None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/responses"):
            return responses
        return chat if chat is not None else httpx.Response(200, text=_CHAT_SSE)

    return handler


_NOT_SUPPORTED = httpx.Response(
    404, json={"error": {"code": "InvalidEndpointOrModel.NotFound",
                         "message": "The model or endpoint does not exist"}})


def test_auto_prefers_responses(cfg, monkeypatch):
    seen: list[str] = []
    p = _provider(monkeypatch,
                  _dispatching(seen, responses=httpx.Response(200, text=_RESPONSES_SSE)),
                  config=_BASE_CFG)
    _run_forward(p, {"model": "ds-flash", "stream": True, "messages": []}, "openai")
    # 探测一次 + 正式请求一次，都是 responses，且从没打过 chat
    assert seen == ["/api/v3/responses", "/api/v3/responses"]


def test_auto_downgrades_when_responses_unsupported(cfg, monkeypatch):
    """接入点不支持 responses → 改走 chat，且后续请求不再白撞 responses。"""
    seen: list[str] = []
    p = _provider(monkeypatch,
                  _dispatching(seen, responses=_NOT_SUPPORTED),
                  config=_BASE_CFG)
    _run_forward(p, {"model": "ds-flash", "stream": True, "messages": []}, "openai")
    assert p._responses_unsupported is True
    assert seen == ["/api/v3/responses", "/api/v3/chat/completions"]

    seen.clear()
    _run_forward(p, {"model": "ds-flash", "stream": True, "messages": []}, "openai")
    assert seen == ["/api/v3/chat/completions"]  # 不再探测 responses


@pytest.mark.parametrize("status,code", [
    (400, "InvalidParameter"),
    (401, "AuthenticationError"),
    (429, "RateLimitExceeded"),
])
def test_do_not_downgrade_on_other_statuses(cfg, monkeypatch, status, code):
    """400/401/429 不降级：那是请求或账号问题，换端点只会把真错误洗掉。"""
    # 探测与正式请求各打一次：探测的响应体被 _peek_body 读掉，httpx.Response
    # 不能重复读，所以按次新建。
    seen: list[str] = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(status,
                              json={"error": {"code": code, "message": "nope"}})

    p = _provider(monkeypatch, handler, config=_BASE_CFG)
    resp, body = _run_forward(p, {"model": "ds-flash", "stream": True, "messages": []},
                              "openai")
    assert resp.status_code == status
    assert json.loads(body)["error"]["code"] == code
    assert p._responses_unsupported is False
    # 探测 + 正式请求，两次都是 responses，一次都没落到 chat
    assert seen == ["/api/v3/responses", "/api/v3/responses"]


def test_forced_protocol_env_is_respected(cfg, monkeypatch):
    """ARK_UPSTREAM_PROTOCOL=chat 时完全不碰 responses。"""
    monkeypatch.setenv("ARK_UPSTREAM_PROTOCOL", "chat")
    seen: list[str] = []
    p = _provider(monkeypatch, _dispatching(seen, responses=_NOT_SUPPORTED),
                  config=_BASE_CFG)
    _run_forward(p, {"model": "ds-flash", "stream": True, "messages": []}, "openai")
    assert seen == ["/api/v3/chat/completions"]


def test_base_url_priority(cfg, monkeypatch):
    cfg({"base_url": "https://ark.ap-southeast-1.volces.com/api/v3",
         "models": [{"id": "x", "endpoint": "ep-1"}]}, key="k")
    assert ArkProvider()._base.endswith("ap-southeast-1.volces.com/api/v3")
    monkeypatch.setenv("ARK_BASE_URL", "https://custom.example/api/v3/")
    assert ArkProvider()._base == "https://custom.example/api/v3"


# ---------------------------------------------------------------------------
# 4. 首事件闸门与上游错误
# ---------------------------------------------------------------------------


def test_inband_error_is_converted_to_http_error(cfg, monkeypatch):
    """上游 200 + 带内 error 帧 → 抛 HTTPException，不是 200 空流。

    走的是 openai 客户端 + responses 上游——即 ``_responses_to_chat_stream``
    解析路径上的闸门。
    """
    sse = ('event: error\n'
           'data: {"type":"error","code":429,"message":"quota exceeded"}\n\n')
    p = _provider(monkeypatch, lambda r: httpx.Response(200, text=sse),
                  config=_BASE_CFG)
    _, _, exc = _run_forward_capture(
        p, {"model": "ds-flash", "stream": True, "messages": []}, "openai")
    assert isinstance(exc, HTTPException)
    assert exc.status_code == 429
    assert "quota exceeded" in exc.detail["error"]["message"]


def test_chat_upstream_inband_error(cfg, monkeypatch):
    monkeypatch.setenv("ARK_UPSTREAM_PROTOCOL", "chat")
    sse = 'data: {"error":{"message":"bad things","type":"internal"}}\n\n'
    p = _provider(monkeypatch, lambda r: httpx.Response(200, text=sse),
                  config=_BASE_CFG)
    _, _, exc = _run_forward_capture(
        p, {"model": "ds-flash", "stream": True, "messages": []}, "openai")
    assert isinstance(exc, HTTPException)
    assert "bad things" in exc.detail["error"]["message"]


def test_passthrough_path_still_has_first_event_gate(cfg, monkeypatch):
    """原样透传路径同样是「200 + 带内报错」的重灾区，必须过闸。

    - openai 客户端 + chat 上游、responses 客户端 + responses 上游都是**不解析**
      的透传路径（最早实现只在这两条上漏了闸门，错误帧被原样泵给客户端）；
    - 顺带锁住「行尾换行不能丢」：``iter_sse_lines`` 剥的是行尾，透传时必须补回，
      否则 SSE 帧之间没有空行分隔。
    """
    chat_err = 'data: {"error":{"message":"bad things","type":"internal"}}\n\n'
    monkeypatch.setenv("ARK_UPSTREAM_PROTOCOL", "chat")
    p = _provider(monkeypatch, lambda r: httpx.Response(200, text=chat_err),
                  config=_BASE_CFG)
    _, _, exc = _run_forward_capture(
        p, {"model": "ds-flash", "stream": True, "messages": []}, "openai")
    assert isinstance(exc, HTTPException), "chat 透传路径漏了首帧闸门"
    assert "bad things" in exc.detail["error"]["message"]

    monkeypatch.delenv("ARK_UPSTREAM_PROTOCOL")
    resp_err = ('event: response.failed\n'
                'data: {"type":"response.failed","response":{"status":"failed",'
                '"error":{"code":"ServerOverloaded","message":"overloaded"}}}\n\n')
    p = _provider(monkeypatch, lambda r: httpx.Response(200, text=resp_err),
                  config=_BASE_CFG)
    _, _, exc = _run_forward_capture(
        p, {"model": "ds-flash", "stream": True,
            "input": [{"role": "user", "content": "hi"}]}, "responses")
    assert isinstance(exc, HTTPException), "responses 透传路径漏了首帧闸门"


def test_responses_stream_rewrites_upstream_model_to_public_name(cfg, monkeypatch):
    """responses 透传必须把上游回的模型名改回对外名。

    上游在 ``response.model`` 里回它自己的名字（``ep-...``，或裸模型 ID 被上游
    归一后的形态，如 ``deepseek-v4-1-flash-261017``）。原样透传等于把本地映射
    白做了——客户端会看到一个自己不认识的模型名。每条带 ``model`` 的事件都要
    改写；不属于本次请求的模型名保持原样（排障线索）。
    """
    monkeypatch.delenv("ARK_UPSTREAM_PROTOCOL", raising=False)
    sse = (
        'event: response.created\n'
        'data: {"type":"response.created","response":{"id":"r1","object":"response",'
        '"model":"ep-test","output":[]}}\n\n'
        'event: response.output_text.delta\n'
        'data: {"type":"response.output_text.delta","model":"ep-test","delta":"hi"}\n\n'
        'event: response.completed\n'
        'data: {"type":"response.completed","response":{"id":"r1","model":"ep-test",'
        '"output":[{"type":"message","role":"assistant","content":[{"type":"output_text",'
        '"text":"hi"}]}]}}\n\n'
        "data: [DONE]\n\n"
    )
    p = _provider(monkeypatch, lambda r: httpx.Response(200, text=sse),
                  config=_BASE_CFG)
    _, body = _run_forward(
        p, {"model": "ds-flash", "stream": True,
            "input": [{"role": "user", "content": "hi"}]}, "responses")
    assert "ep-test" not in body, "上游模型名泄漏给了客户端"
    # 三帧都带 model（created.response / delta / completed.response），逐个改写
    assert body.count('"model": "ark/ds-flash"') == 3
    assert "response.completed" in body


def test_passthrough_preserves_sse_frame_boundaries(cfg, monkeypatch):
    """透传时行尾换行要保留，客户端才能按空行切帧。"""
    monkeypatch.setenv("ARK_UPSTREAM_PROTOCOL", "chat")
    p = _provider(monkeypatch, lambda r: httpx.Response(200, text=_CHAT_SSE),
                  config=_BASE_CFG)
    _, body = _run_forward(p, {"model": "ds-flash", "stream": True, "messages": []},
                           "openai")
    assert "\n\n" in body            # 帧分隔在
    assert "[DONE]" in body
    assert body.count("data: ") == _CHAT_SSE.count("data: ")


def test_upstream_http_error_passthrough(cfg, monkeypatch):
    """上游 4xx 原样透传状态码与错误体（key 无效一类）。"""
    err = httpx.Response(401, json={"error": {"code": "AuthenticationError",
                                              "message": "bad key"}})
    p = _provider(monkeypatch, lambda r: err, config=_BASE_CFG)
    resp, _ = _run_forward(p, {"model": "ds-flash", "stream": True, "messages": []},
                           "openai")
    assert resp.status_code == 401
    assert json.loads(resp.body)["error"]["code"] == "AuthenticationError"


def test_network_error_becomes_502(cfg, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("boom")

    p = _provider(monkeypatch, handler, config=_BASE_CFG)
    with pytest.raises(HTTPException) as exc:
        _run_forward(p, {"model": "ds-flash", "stream": True, "messages": []}, "openai")
    assert exc.value.status_code == 502


# ---------------------------------------------------------------------------
# 5. 请求构造
# ---------------------------------------------------------------------------


def test_upstream_always_streaming_and_usage_requested(cfg, monkeypatch):
    """发往 chat 上游恒为 stream + include_usage（否则拿不到 usage）。"""
    cfg(_BASE_CFG, key="k")
    monkeypatch.setenv("ARK_UPSTREAM_PROTOCOL", "chat")
    p = ArkProvider()
    body = p._upstream_body({"model": "ds-flash", "stream": False, "messages": [],
                             "_internal": 1}, upstream="ep-test", protocol="chat")
    assert body["stream"] is True
    assert body["model"] == "ep-test"      # 上游真名
    assert body["stream_options"] == {"include_usage": True}
    assert "_internal" not in body


def test_responses_body_uses_projection(cfg, monkeypatch):
    """responses 上游：chat messages 投影成 responses input。"""
    cfg(_BASE_CFG, key="k")
    p = ArkProvider()
    body = p._upstream_body(
        {"model": "ds-flash", "messages": [{"role": "user", "content": "hi"}]},
        upstream="ep-test", protocol="responses")
    assert body["stream"] is True
    assert body["model"] == "ep-test"
    assert isinstance(body["input"], list)


# ---------------------------------------------------------------------------
# 6. 注册
# ---------------------------------------------------------------------------


def test_registered_in_known_provider_ids():
    from buddy_proxy.core.settings import KNOWN_PROVIDER_IDS, PROVIDER_ENABLE_HINTS

    assert "ark" in KNOWN_PROVIDER_IDS
    assert "--ark" in PROVIDER_ENABLE_HINTS["ark"]
