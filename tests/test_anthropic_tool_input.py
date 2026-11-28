"""Anthropic 协议下 tool_use 的 ``input`` 必须是**对象**。

起因（2026-11-28 用户报障）：Kimi 侧调用 raccoon 时报

    Retrying (2/10) · invalid_tool_arguments
    Tool call "Grep" was not executed because its arguments were invalid:
    must have required property 'pattern'

看着像代理把参数弄丢了，实际是上游被 ``max_tokens`` 截断，参数是半截 JSON
（实测 sn-kimi-k3-1-pro：``max_tokens=40``/``50`` 都返回 ``'{"path":"src'``）。
原实现解析失败时把**原始字符串**塞进 ``input``，而 Anthropic 协议的
``input`` 必须是对象——客户端拿到字符串后取不到任何字段，于是报「缺必填参数」。
"""

from __future__ import annotations

import json

from buddy_proxy.protocols.anthropic_adapter import (
    chat_completion_to_anthropic_message,
)


def _convert(arguments: str, finish_reason: str = "tool_calls") -> dict:
    data = {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "Grep", "arguments": arguments},
                }],
            },
            "finish_reason": finish_reason,
        }],
        "usage": {},
    }
    return chat_completion_to_anthropic_message(data, {"model": "m"})


def test_valid_arguments_become_object_input():
    out = _convert('{"pattern":"foo","path":"src"}')
    block = out["content"][0]
    assert block["type"] == "tool_use"
    assert block["input"] == {"pattern": "foo", "path": "src"}
    assert out["stop_reason"] == "tool_use"


def test_truncated_arguments_yield_empty_object_not_string():
    """半截 JSON → ``input`` 给 ``{}``，绝不能是字符串。

    字符串 input 会让客户端报「must have required property 'pattern'」，
    把「输出被截断」误报成「工具参数无效」——排查方向直接跑偏。
    """
    out = _convert('{"path":"src', finish_reason="length")
    block = out["content"][0]
    assert isinstance(block["input"], dict), (
        "Anthropic 的 tool_use.input 必须是对象；字符串会让客户端解析失败")
    assert block["input"] == {}


def test_truncated_tool_call_keeps_max_tokens_stop_reason():
    """截断语义不能被 tool_use 盖掉：客户端该按长度截断重试，而非执行半截参数。"""
    out = _convert('{"path":"src', finish_reason="length")
    assert out["stop_reason"] == "max_tokens"


def test_empty_arguments_string_becomes_empty_object():
    """``arguments`` 为空串时同样不能塞字符串。"""
    out = _convert("")
    assert out["content"][0]["input"] == {}


def test_multiple_tool_calls_each_get_own_input():
    data = {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [
                    {"index": 0, "id": "a", "type": "function",
                     "function": {"name": "Grep",
                                  "arguments": '{"pattern":"foo"}'}},
                    {"index": 1, "id": "b", "type": "function",
                     "function": {"name": "Glob",
                                  "arguments": '{"pattern":"*.py"}'}},
                ],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {},
    }
    out = chat_completion_to_anthropic_message(data, {"model": "m"})
    blocks = [b for b in out["content"] if b["type"] == "tool_use"]
    assert [b["name"] for b in blocks] == ["Grep", "Glob"]
    assert blocks[0]["input"] == {"pattern": "foo"}
    assert blocks[1]["input"] == {"pattern": "*.py"}


def test_arguments_json_roundtrip_is_not_stringified():
    """防止有人为了「保留原始文本」把 input 改成 json.dumps 后的字符串。"""
    out = _convert('{"pattern":"foo"}')
    block = out["content"][0]
    assert not isinstance(block["input"], str)
    assert json.dumps(block["input"], sort_keys=True) == '{"pattern": "foo"}'
