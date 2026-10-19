"""Chat ⇄ Responses 双向投影测试（纯函数，离线）。"""

from __future__ import annotations

import json

from buddy_proxy.codex import projection


# ---------------------------------------------------------------------------
# 请求方向
# ---------------------------------------------------------------------------

def _chat(**over):
    body = {
        "model": "codex/gpt-6.1-sol",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }
    body.update(over)
    return body


def test_request_basic_shape():
    out = projection.chat_to_responses_request(_chat(), model="gpt-6.1-sol")
    assert out["model"] == "gpt-6.1-sol"
    assert out["stream"] is True
    assert out["input"] == [
        {"type": "message", "role": "user", "content": "hello"}]
    # instructions 必须存在（Codex 后端对字段存在性敏感）
    assert out["instructions"] == ""


def test_request_system_becomes_instructions():
    out = projection.chat_to_responses_request(_chat(messages=[
        {"role": "system", "content": "you are helpful"},
        {"role": "developer", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]))
    assert out["instructions"] == "you are helpful\n\nbe brief"
    # system/developer 不进 input
    assert [i["role"] for i in out["input"]] == ["user"]


def test_request_explicit_instructions_wins():
    out = projection.chat_to_responses_request(_chat(
        instructions="explicit prompt",
        messages=[{"role": "system", "content": "from system"},
                  {"role": "user", "content": "hi"}]))
    # explicit 在前（更精确），但 system 不能被吞：anthropic 入口同时带
    # 客户端 system message 与别人的 instructions 时，丢掉 system 等于
    # 把系统提示整段删掉。
    assert out["instructions"] == "explicit prompt\n\nfrom system"


def test_request_explicit_instructions_alone_verbatim():
    """只有 instructions、没有 system message 时原样使用（不多空行）。"""
    out = projection.chat_to_responses_request(_chat(instructions="only prompt"))
    assert out["instructions"] == "only prompt"


def test_request_instructions_dedup_identical():
    """instructions 与 system 文本完全一致时不重复拼接。"""
    out = projection.chat_to_responses_request(_chat(
        instructions="same",
        messages=[{"role": "system", "content": "same"}]))
    assert out["instructions"] == "same"


def test_request_assistant_tool_calls_become_function_call_items():
    out = projection.chat_to_responses_request(_chat(messages=[
        {"role": "user", "content": "read file"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "read", "arguments": '{"p":"a"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "file body"},
    ]))
    items = out["input"]
    assert items[0]["role"] == "user"
    assert items[1] == {
        "type": "function_call", "call_id": "call_1",
        "name": "read", "arguments": '{"p":"a"}'}
    assert items[2] == {
        "type": "function_call_output", "call_id": "call_1", "output": "file body"}


def test_request_drops_reasoning_from_assistant_history():
    """思考内容不能回灌：Responses 的 reasoning item 需要签名，凭空造的会被拒。"""
    out = projection.chat_to_responses_request(_chat(messages=[
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a", "reasoning_content": "secret thinking"},
    ]))
    blob = json.dumps(out["input"])
    assert "secret thinking" not in blob
    assert '"a"' in blob


def test_request_tools_flattened():
    out = projection.chat_to_responses_request(_chat(tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }], tool_choice={"type": "function", "function": {"name": "get_weather"}}))
    assert out["tools"] == [{
        "type": "function",
        "name": "get_weather",
        "description": "weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        "strict": False,
    }]
    assert out["tool_choice"] == {"type": "function", "name": "get_weather"}


def test_request_tool_choice_string_passthrough():
    out = projection.chat_to_responses_request(_chat(tools=[{
        "type": "function", "function": {"name": "f"}}], tool_choice="none"))
    assert out["tool_choice"] == "none"


def test_request_max_tokens_and_reasoning():
    out = projection.chat_to_responses_request(_chat(
        max_tokens=1234, reasoning_effort="high", temperature=0.5))
    assert out["max_output_tokens"] == 1234
    assert out["reasoning"] == {"effort": "high", "summary": "auto"}
    assert out["temperature"] == 0.5


def test_request_max_completion_tokens_preferred():
    out = projection.chat_to_responses_request(_chat(
        max_tokens=100, max_completion_tokens=200))
    assert out["max_output_tokens"] == 200


def test_request_drops_stop():
    """Responses 不支持 stop，塞进去会 400。"""
    out = projection.chat_to_responses_request(_chat(stop=["END"]))
    assert "stop" not in out


def test_request_images():
    out = projection.chat_to_responses_request(_chat(messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ],
    }]))
    content = out["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "what is this"}
    assert content[1] == {"type": "input_image", "image_url": "data:image/png;base64,AAA"}


def test_request_stable_keys_removed():
    """上游不认的 chat 化字段要被剔除。"""
    out = projection.chat_to_responses_request(_chat(
        previous_response_id="resp_1", prompt_cache_retention="24h",
        safety_identifier="x", response_format={"type": "json_object"}))
    for key in ("previous_response_id", "prompt_cache_retention",
                "safety_identifier", "response_format"):
        assert key not in out


# ---------------------------------------------------------------------------
# 非流式响应方向
# ---------------------------------------------------------------------------

def _response(**over):
    base = {
        "id": "resp_abc",
        "object": "response",
        "status": "completed",
        "output": [{
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "hello world"}],
        }],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }
    base.update(over)
    return base


def test_response_text_and_usage():
    out = projection.responses_to_chat_completion(_response(), model="gpt-6.1-sol")
    assert out["object"] == "chat.completion"
    assert out["model"] == "gpt-6.1-sol"
    choice = out["choices"][0]
    assert choice["message"]["content"] == "hello world"
    assert choice["finish_reason"] == "stop"
    assert out["usage"] == {
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_response_tool_calls():
    out = projection.responses_to_chat_completion(_response(output=[
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "checking"}]},
        {"type": "function_call", "call_id": "call_9", "name": "read",
         "arguments": '{"p":"a"}'},
    ]), model="m")
    calls = out["choices"][0]["message"]["tool_calls"]
    assert calls == [{
        "id": "call_9", "type": "function",
        "function": {"name": "read", "arguments": '{"p":"a"}'}}]
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_response_reasoning_surfaced():
    out = projection.responses_to_chat_completion(_response(output=[
        {"type": "reasoning",
         "summary": [{"type": "summary_text", "text": "thinking..."}]},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "done"}]},
    ]), model="m")
    msg = out["choices"][0]["message"]
    assert msg["content"] == "done"
    assert msg["reasoning_content"] == "thinking..."


def test_response_reasoning_only_goes_to_content():
    """只有思考没有正文时，别让客户端收到空消息。"""
    out = projection.responses_to_chat_completion(_response(output=[
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "only think"}]},
    ]), model="m")
    msg = out["choices"][0]["message"]
    assert msg["content"] == "only think"
    assert "reasoning_content" not in msg


def test_response_incomplete_maps_length():
    out = projection.responses_to_chat_completion(_response(
        status="incomplete", incomplete_details={"reason": "max_output_tokens"},
    ), model="m")
    assert out["choices"][0]["finish_reason"] == "length"


# ---------------------------------------------------------------------------
# 流式响应方向
# ---------------------------------------------------------------------------

def _drain(conv, events):
    out = []
    for name, payload in events:
        out.extend(conv.feed(name, payload))
    out.extend(conv.finish())
    return out


def test_stream_text_sequence():
    conv = projection.ResponsesToChatStream("m")
    chunks = _drain(conv, [
        ("response.created", {"type": "response.created"}),
        ("response.output_item.added", {"item": {"type": "message", "id": "msg_1"}}),
        ("response.output_text.delta", {"delta": "he"}),
        ("response.output_text.delta", {"delta": "llo"}),
        ("response.completed", {"response": {
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}}}),
    ])
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert text == "hello"
    finish = [c["choices"][0]["finish_reason"] for c in chunks]
    assert finish[-1] == "stop" or finish[-2] == "stop"
    assert any(c.get("usage", {}).get("total_tokens") == 5 for c in chunks)


def test_stream_tool_call_sequence():
    conv = projection.ResponsesToChatStream("m")
    chunks = _drain(conv, [
        ("response.output_item.added", {"item": {
            "type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read"}}),
        ("response.function_call_arguments.delta", {"item_id": "fc_1", "delta": '{"p":'}),
        ("response.function_call_arguments.delta", {"item_id": "fc_1", "delta": '"a"}'}),
        ("response.output_item.done", {"item": {
            "type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read"}}),
        ("response.completed", {"response": {"usage": {}}}),
    ])
    tool_deltas = [c for c in chunks
                   if c["choices"][0]["delta"].get("tool_calls")]
    first = tool_deltas[0]["choices"][0]["delta"]["tool_calls"][0]
    assert first["index"] == 0
    assert first["id"] == "call_1"
    assert first["function"]["name"] == "read"
    args = "".join(d["choices"][0]["delta"]["tool_calls"][0]["function"].get("arguments") or ""
                   for d in tool_deltas)
    assert args == '{"p":"a"}'
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_stream_reasoning_buffered_until_complete():
    conv = projection.ResponsesToChatStream("m")
    chunks = _drain(conv, [
        ("response.reasoning_summary_text.delta", {"delta": "think"}),
        ("response.reasoning_summary_text.delta", {"delta": "ing"}),
        ("response.output_text.delta", {"delta": "answer"}),
        ("response.completed", {"response": {"usage": {}}}),
    ])
    reasoning = [c["choices"][0]["delta"].get("reasoning_content") for c in chunks]
    assert [r for r in reasoning if r] == ["thinking"]
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert text == "answer"


def test_stream_finish_is_idempotent():
    conv = projection.ResponsesToChatStream("m")
    list(conv.feed("response.completed", {"response": {"usage": {}}}))
    assert conv.finish() == []


def test_stream_truncated_upstream_still_finishes():
    """上游没发 response.completed 就断流，finish() 要兜底补齐，别让客户端挂住。"""
    conv = projection.ResponsesToChatStream("m")
    conv.feed("response.output_text.delta", {"delta": "partial"})
    tail = conv.finish()
    assert tail[-1]["choices"][0]["finish_reason"] == "stop"


# ---------------------------------------------------------------------------
# 错误事件识别（闸门用）
# ---------------------------------------------------------------------------

def test_is_error_event_error_frame():
    status, msg = projection.is_error_event("error", {
        "code": "rate_limit_exceeded", "message": "slow down"})
    assert status == 429
    assert "slow down" in msg


def test_is_error_event_response_failed():
    status, msg = projection.is_error_event("response.failed", {"response": {
        "error": {"code": "invalid_api_key", "message": "bad key"}}})
    assert status == 401


def test_is_error_event_incomplete_max_tokens_is_not_error():
    assert projection.is_error_event("response.incomplete", {"response": {
        "incomplete_details": {"reason": "max_output_tokens"}}}) == (0, "")


def test_is_error_event_normal_event():
    assert projection.is_error_event("response.output_text.delta", {"delta": "x"}) == (0, "")


def test_status_from_code_numeric_passthrough():
    assert projection._status_from_code(429) == 429
    assert projection._status_from_code(503) == 503
    assert projection._status_from_code("weird") == 500


# ---------------------------------------------------------------------------
# SSE 解析
# ---------------------------------------------------------------------------

def test_iter_sse_events_parses_frames():
    lines = [
        "event: response.output_text.delta",
        'data: {"delta": "hi"}',
        "",
        "event: response.completed",
        'data: {"response": {"usage": {"total_tokens": 1}}}',
        "",
        "data: [DONE]",
        "",
    ]
    events = list(projection.iter_sse_events(iter(lines)))
    assert events[0] == ("response.output_text.delta", {"delta": "hi"})
    assert events[1][0] == "response.completed"


def test_iter_sse_events_falls_back_to_type_field():
    lines = ['data: {"type": "response.created", "response": {}}', ""]
    events = list(projection.iter_sse_events(iter(lines)))
    assert events == [("response.created", {"type": "response.created", "response": {}})]


def test_iter_sse_events_skips_comments_and_bad_frames():
    lines = [
        ": keep-alive",
        "data: not-json",
        "",
        "event: response.output_text.delta",
        'data: {"delta": "ok"}',
        "",
    ]
    events = list(projection.iter_sse_events(iter(lines)))
    assert events == [("response.output_text.delta", {"delta": "ok"})]


# ---------------------------------------------------------------------------
# 上游 body 收口（build_upstream_body）
# ---------------------------------------------------------------------------

def test_upstream_body_forces_store_false():
    """ChatGPT 账号走 Codex 后端时必须 store=false，否则直接 400。"""
    from buddy_proxy.codex import upstream as codex_upstream

    out = codex_upstream.build_upstream_body(
        _chat(store=True), model="gpt-6-sol", stream=True)
    assert out["store"] is False
    assert codex_upstream.build_upstream_body(
        _chat(), model="gpt-6-sol", stream=False)["store"] is False
