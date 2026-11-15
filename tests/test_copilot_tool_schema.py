"""Copilot 上游 ``/v1/messages`` 的工具 schema 收敛。

规则全部来自实测：拿 Claude Code 的真实请求体（anthropic 协议、249 个工具）
打 ``copilot/gemini-4-hydrogen``，去掉 ``tools`` 就 200，带着就
``400 invalid request body``，逐条删到能过为止，归纳出下面这几条。
"""

from __future__ import annotations

import pytest

from buddy_proxy.copilot.catalog import ModelEntry
from buddy_proxy.copilot.provider import CopilotProvider
from buddy_proxy.copilot.schema import (
    sanitize_anthropic_tools,
    sanitize_chat_tools,
)


def _tool(name: str, schema: dict) -> dict:
    return {"name": name, "description": "", "input_schema": schema}


def _only(tools: list[dict]) -> dict:
    fixed, changed = sanitize_anthropic_tools(tools)
    assert changed == 1
    return fixed[0]["input_schema"]


def _unchanged(tools: list[dict]) -> None:
    fixed, changed = sanitize_anthropic_tools(tools)
    assert changed == 0
    assert fixed == tools


# ---------------------------------------------------------------------------
# 纯函数：收敛规则
# ---------------------------------------------------------------------------

def _prop(schema: dict) -> dict:
    """把 schema 放进某个属性里（规则在嵌套层验证，顶层一律钉 object）。"""
    return _only([_tool("t", {"type": "object", "properties": {"x": schema}})])[
        "properties"]["x"]


def test_union_type_with_null_collapses_to_single_type():
    assert _prop({"type": ["null", "string"]}) == {"type": "string"}


def test_union_type_prefers_object_over_other_scalars():
    got = _only([_tool("t", {
        "type": ["string", "object", "null"],
        "properties": {"a": {"type": "string"}},
    })])
    assert got == {"type": "object", "properties": {"a": {"type": "string"}}}


def test_union_type_with_array_gets_items():
    # 嵌套层：联合里带 array 又没 items，补 items 而不是把形状丢掉。
    got = _only([_tool("t", {"type": "object", "properties": {
        "xs": {"type": ["array", "null"]},
    }})])
    assert got["properties"]["xs"] == {"type": "array", "items": {}}


def test_union_with_properties_prefers_object_even_beside_array():
    got = _only([_tool("t", {
        "type": ["array", "string"],
        "properties": {"a": {"type": "string"}},
    })])
    assert got == {"type": "object", "properties": {"a": {"type": "string"}}}


def test_all_null_union_falls_back_to_object():
    # 什么都不剩：嵌套层删 type，顶层钉 object。
    assert _only([_tool("t", {"properties": {"a": {"type": ["null"]}}}),
                  ])["properties"]["a"] == {}


# ---------------------------------------------------------------------------
# 顶层必须是 object（实测 array / 空对象 / 无 properties 的 object 以外的形态全 400）
# ---------------------------------------------------------------------------

_EMPTY_OBJECT = {"type": "object", "properties": {}}


def test_top_level_array_is_forced_to_object():
    got = _only([_tool("t", {"type": "array", "items": {"type": "string"}})])
    assert got == _EMPTY_OBJECT


def test_top_level_union_with_array_is_forced_to_object():
    got = _only([_tool("t", {"type": ["array", "null"]})])
    assert got == _EMPTY_OBJECT


def test_empty_schema_is_forced_to_object_with_properties():
    # 光 {"type": "object"} 上游也拒，必须带 properties。
    assert _only([_tool("t", {})]) == _EMPTY_OBJECT
    assert _only([_tool("t", {"type": "object"})]) == _EMPTY_OBJECT


def test_top_level_items_is_dropped():
    assert _only([_tool("t", {"items": {"type": "string"}})]) == _EMPTY_OBJECT


def test_top_level_keeps_existing_properties():
    got = _only([_tool("t", {"type": "array",
                             "properties": {"a": {"type": "string"}}})])
    assert got == {"type": "object", "properties": {"a": {"type": "string"}}}


def test_combinator_conflicting_with_sibling_type_drops_type():
    # MCP 的 record_unknown / update_analysis_settings 就长这样。
    got = _prop({
        "type": "array",
        "anyOf": [{"type": "string"}],
        "items": {"type": "string"},
    })
    assert got == {"anyOf": [{"type": "string"}]}


def test_items_outside_array_type_is_dropped():
    assert _prop({"type": "object", "items": {}}) == {"type": "object"}


def test_bare_items_is_dropped():
    assert _prop({"items": {"type": "string"}}) == {}


def test_tuple_form_items_becomes_empty_object():
    got = _prop({"type": "array",
                 "items": [{"type": "string"}, {"type": "number"}]})
    assert got == {"type": "array", "items": {}}


def test_nested_rules_apply_inside_properties_and_combinators():
    schema = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "object",
                "properties": {
                    "dimensions": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "anyOf": [{"const": "overall"}],
                        },
                    },
                },
            },
        },
    }
    got = _only([_tool("t", schema)])
    item = got["properties"]["scope"]["properties"]["dimensions"]["items"]
    assert item == {"anyOf": [{"const": "overall"}]}


def test_clean_schema_is_returned_untouched():
    schema = {"type": "object", "properties": {"q": {"type": "string"}},
              "required": ["q"]}
    tools = [_tool("t", schema)]
    fixed, changed = sanitize_anthropic_tools(tools)
    assert changed == 0
    # 没改动的工具保持同一个对象，上层据此跳过重写。
    assert fixed[0] is tools[0]


def test_non_list_tools_passthrough():
    for value in (None, {}, "x"):
        tools, changed = sanitize_anthropic_tools(value)
        assert tools is value
        assert changed == 0


def test_tool_without_input_schema_is_left_alone():
    tools = [{"name": "t", "description": ""}]
    fixed, changed = sanitize_anthropic_tools(tools)
    assert changed == 0
    assert fixed[0] is tools[0]


# ---------------------------------------------------------------------------
# 接线：只在 anthropic 透传分支收敛
# ---------------------------------------------------------------------------

def _anthropic_entry(*endpoints: str) -> ModelEntry:
    return ModelEntry(
        id="gemini-4-hydrogen",
        name="Gemini 4 Hydrogen",
        vendor="google",
        endpoint="/v1/messages",
        endpoints=endpoints or ("/v1/messages", "/chat/completions"),
    )


def test_payload_for_anthropic_passthrough_sanitizes_tools():
    dirty = {"type": "object", "properties": {"x": {"type": ["null", "string"]}}}
    original = {
        "tools": [_tool("t", dirty)],
        "messages": [{"role": "user", "content": "hi"}],
    }
    payload = CopilotProvider()._payload_for(
        {"messages": []}, {"messages": []}, original, _anthropic_entry(),
        upstream_model="gemini-4-hydrogen", protocol="anthropic", stream=True,
    )
    assert payload["tools"][0]["input_schema"]["properties"]["x"] == {
        "type": "string"}
    # 客户端原始请求体不能被就地改坏。
    assert original["tools"][0]["input_schema"] is dirty
    assert dirty["properties"]["x"] == {"type": ["null", "string"]}


def test_payload_for_anthropic_passthrough_keeps_clean_tools():
    tool = _tool("t", {"type": "object", "properties": {}})
    original = {"tools": [tool], "messages": []}
    payload = CopilotProvider()._payload_for(
        {"messages": []}, {"messages": []}, original, _anthropic_entry(),
        upstream_model="gemini-4-hydrogen", protocol="anthropic", stream=True,
    )
    assert payload["tools"][0] is tool


def test_payload_for_openai_client_on_anthropic_model_sanitizes_chat_tools():
    # chat 端点与 /v1/messages 共用同一套严格校验（实测同样整条 400），也得收敛。
    dirty = {"type": ["null", "string"]}
    chat_body = {"messages": [{"role": "user", "content": "hi"}],
                 "tools": [_chat_tool("t", dirty)]}
    payload = CopilotProvider()._payload_for(
        chat_body, chat_body, None, _anthropic_entry(),
        upstream_model="gemini-4-hydrogen", protocol="openai", stream=True,
    )
    assert payload["tools"][0]["function"]["parameters"] == _EMPTY_OBJECT
    assert chat_body["tools"][0]["function"]["parameters"] is dirty


def test_payload_for_anthropic_only_model_still_rejects_openai_client():
    entry = _anthropic_entry("/v1/messages")
    with pytest.raises(Exception) as excinfo:
        CopilotProvider()._payload_for(
            {"messages": []}, {"messages": []}, None, entry,
            upstream_model="gemini-4-hydrogen", protocol="openai", stream=True,
        )
    assert "只提供 /v1/messages 上游端点" in str(excinfo.value)


# ---------------------------------------------------------------------------
# chat 形态（``tools[].function.parameters``）：copilot 的 `/chat/completions`
# 与 `/v1/messages` 共用同一套严格校验，静态目录里绝大多数模型只声明 chat 端点，
# 这条路不收敛的话「大工具集必 400」照样复现。
# ---------------------------------------------------------------------------

def _chat_tool(name: str, schema: dict) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": schema}}


def _clean_chat_tools(tools: list[dict]) -> list[dict]:
    fixed, _ = sanitize_chat_tools(tools)
    return fixed


def test_chat_tools_top_level_array_becomes_object_with_properties():
    tools = [_chat_tool("t", {"type": "array", "items": {}})]
    assert _clean_chat_tools(tools)[0]["function"]["parameters"] == _EMPTY_OBJECT


def test_chat_tools_nested_rules_apply_to_parameters():
    dirty = {"type": "object", "properties": {
        "x": {"type": ["null", "string"]},
        "y": {"type": "array"},
        "z": {"type": "string", "items": {}},
    }}
    fixed = _clean_chat_tools([_chat_tool("t", dirty)])[0]["function"]["parameters"]
    assert fixed["properties"]["x"] == {"type": "string"}
    assert fixed["properties"]["y"] == {"type": "array", "items": {}}
    assert fixed["properties"]["z"] == {"type": "string"}


def test_chat_tools_flat_shape_is_sanitized_too():
    tools = [{"name": "t", "parameters": {"type": "array", "items": {}}}]
    assert _clean_chat_tools(tools)[0]["parameters"] == _EMPTY_OBJECT


def test_chat_tools_do_not_mutate_input():
    dirty = {"type": ["null", "string"]}
    tools = [_chat_tool("t", dirty)]
    _clean_chat_tools(tools)
    assert tools[0]["function"]["parameters"] is dirty
    assert dirty == {"type": ["null", "string"]}


def test_chat_tools_clean_tool_keeps_same_object():
    tool = _chat_tool("t", {"type": "object", "properties": {}})
    fixed, changed = sanitize_chat_tools([tool])
    assert changed == 0
    assert fixed[0] is tool


def test_chat_tools_without_parameters_left_alone():
    tool = {"type": "function", "function": {"name": "t"}}
    fixed, changed = sanitize_chat_tools([tool])
    assert changed == 0
    assert fixed[0] is tool


def test_chat_tools_non_list_passthrough():
    fixed, changed = sanitize_chat_tools(None)
    assert fixed is None and changed == 0


def test_payload_for_chat_endpoint_sanitizes_tools():
    # 静态目录里的模型（如 gemini-4-hydrogen）只声明 /chat/completions。
    entry = ModelEntry(id="gemini-4-hydrogen", name="Gemini 4 Hydrogen",
                       vendor="google", endpoint="/chat/completions",
                       endpoints=("/chat/completions",))
    chat_body = {"messages": [{"role": "user", "content": "hi"}],
                 "tools": [_chat_tool("t", {"type": "array", "items": {}})]}
    payload = CopilotProvider()._payload_for(
        chat_body, chat_body, None, entry,
        upstream_model="gemini-4-hydrogen", protocol="openai", stream=True,
    )
    assert payload["tools"][0]["function"]["parameters"] == _EMPTY_OBJECT
    # 客户端请求体不能被就地改坏。
    assert chat_body["tools"][0]["function"]["parameters"] == {
        "type": "array", "items": {}}
