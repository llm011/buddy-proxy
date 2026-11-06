"""Responses API（流式 + 非流式）：输出 item 的内容块必须是 ``output_text``。

回归背景（2026-11-06）：Codex CLI 0.166.0 起，``response.output_item.done``
里的 message item 如果内容块 type 还是 Chat 风格的 ``text``，客户端会静默丢弃：

    DEBUG codex_api::sse::responses: failed to parse ResponseItem from output_item.done

表现是 ``codex exec --profile deepseek`` 有 token 消耗、却一个字都不打印。
Responses 规范里 assistant 输出块叫 ``output_text``（带 ``annotations``），
所以转换器必须发 ``output_text``，不能用 chat 的 ``text``。
"""
from buddy_proxy.codebuddy_provider.pipeline import convert_nonstream
from buddy_proxy.protocols.responses_adapter import ResponsesStreamConverter


def _run(text: str = "收到") -> list[tuple[str, dict]]:
    conv = ResponsesStreamConverter("test-model")
    events: list[tuple[str, dict]] = []
    events += conv.feed_chunk({
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                     "finish_reason": None}],
    })
    events += conv.feed_chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 3, "completion_tokens": 1,
                                   "total_tokens": 4}})
    events += conv.finish()
    return events


def _named(events, name):
    return [payload for event_name, payload in events if event_name == name]


def test_message_item_content_uses_output_text():
    events = _run()
    done = _named(events, "response.output_item.done")
    message_items = [e["item"] for e in done if e["item"].get("type") == "message"]
    assert message_items, "应当发出 message 类型的 output_item.done"
    for item in message_items:
        assert item["content"], "内容块不应为空"
        for part in item["content"]:
            assert part["type"] == "output_text", (
                f"内容块 type 必须是 output_text（chat 的 text 会被 Codex CLI 丢弃），实际 {part['type']!r}")
            assert part["text"] == "收到"
            assert "annotations" in part


def test_content_part_events_use_output_text():
    events = _run()
    for name in ("response.content_part.added", "response.content_part.done"):
        for payload in _named(events, name):
            assert payload["part"]["type"] == "output_text", name


def test_completed_output_items_use_output_text():
    events = _run()
    for payload in _named(events, "response.completed"):
        for item in payload["response"]["output"]:
            if item.get("type") != "message":
                continue
            for part in item["content"]:
                assert part["type"] == "output_text"
                assert part["text"] == "收到"


def test_empty_text_keeps_content_list_empty():
    """没有文本时不要凭空造一个空 output_text 块。"""
    conv = ResponsesStreamConverter("test-model")
    conv.feed_chunk({"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                            "finish_reason": None}]})
    events = conv.finish()
    for payload in _named(events, "response.output_item.done"):
        assert payload["item"]["content"] == []


# ---------------------------------------------------------------------------
# 非流式路径（codebuddy_provider.pipeline.convert_nonstream）必须同形
# ---------------------------------------------------------------------------


def _collected(content: str = "收到", tool_calls: list | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{
        "index": 0,
        "message": message,
        "finish_reason": "tool_calls" if tool_calls else "stop",
    }]}


def test_nonstream_message_uses_output_text_item():
    data = convert_nonstream(_collected(), "responses", None)
    items = data["output"]
    assert [i["type"] for i in items] == ["message"]
    item = items[0]
    assert item.get("id"), "message item 需要 id，否则客户端无法把增量对回 item"
    assert item["status"] == "completed"
    assert item["content"] == [
        {"type": "output_text", "text": "收到", "annotations": []}
    ]


def test_nonstream_tool_calls_are_top_level_items():
    """``function_call`` 是顶层 output item，不能塞进 message 的 content。"""
    data = convert_nonstream(
        _collected("hello", [{
            "id": "call_1",
            "function": {"name": "f", "arguments": '{"a": 1}'},
        }]),
        "responses",
        None,
    )
    assert [i["type"] for i in data["output"]] == ["message", "function_call"]
    call = data["output"][1]
    assert call["id"] == "call_1"
    assert call["call_id"] == "call_1"
    assert call["name"] == "f"
    # 规范要求 arguments 是 JSON 字符串（与流式路径的增量拼接结果一致）
    assert call["arguments"] == '{"a": 1}'
    for item in data["output"]:
        if item["type"] == "message":
            assert all(p["type"] == "output_text" for p in item["content"])


def test_nonstream_tool_only_does_not_emit_empty_message_item():
    data = convert_nonstream(
        _collected("", [{"id": "call_1", "function": {"name": "f", "arguments": "{}"}}]),
        "responses",
        None,
    )
    assert [i["type"] for i in data["output"]] == ["function_call"]
