"""工具定义去重（``forward._dedupe_tools``）回归测试。

锁住一件事：上游 CodeBuddy 对**同名工具**直接拒
``400 / {"code":11152,"msg":"the tool name is invalid or duplicated"}``，
且 400 属确定性错误不换档，客户端只能看到一句 ``Upstream API error (HTTP 400)``，
真因完全不可见。触发源是客户端自己的工具合并策略（``toolUpdate: addition-only``
一类只追加不合并的配置），故在转发前兜底。

实测对照（2026-11-02）：同名 27 个 → 400/11152；不同名 27 个 → 200。

运行：
    .venv/bin/python -m pytest tests/test_tools_dedup.py -v
"""
from __future__ import annotations

from buddy_proxy.codebuddy_provider.forward import _dedupe_tools, _tool_name


def _openai_tool(name: str, desc: str = "d") -> dict:
    return {"type": "function",
            "function": {"name": name, "description": desc,
                         "parameters": {"type": "object", "properties": {}}}}


def _anthropic_tool(name: str) -> dict:
    return {"name": name, "description": "d",
            "input_schema": {"type": "object", "properties": {}}}


# ---------------------------------------------------------------------------
# _tool_name
# ---------------------------------------------------------------------------

def test_tool_name_openai_shape():
    assert _tool_name(_openai_tool("bash")) == "bash"


def test_tool_name_anthropic_shape():
    """Anthropic 的名字在顶层，不在 function 里。"""
    assert _tool_name(_anthropic_tool("read")) == "read"


def test_tool_name_missing_or_invalid():
    assert _tool_name({}) == ""
    assert _tool_name({"type": "function"}) == ""
    assert _tool_name({"function": {"name": None}}) == ""
    assert _tool_name("not-a-dict") == ""
    assert _tool_name(None) == ""


def test_tool_name_is_stripped():
    assert _tool_name(_openai_tool("  bash  ")) == "bash"


# ---------------------------------------------------------------------------
# _dedupe_tools — 去重
# ---------------------------------------------------------------------------

def test_duplicate_names_collapse_to_first():
    """27 个同名工具（addition-only 的典型后果）只剩 1 个。"""
    body = {"tools": [_openai_tool("bash", desc=f"d{i}") for i in range(27)]}
    out = _dedupe_tools(body)
    assert len(out["tools"]) == 1
    assert out["tools"][0]["function"]["description"] == "d0"  # 保留首次


def test_distinct_names_untouched():
    """不同名不触发任何改动，且返回同一对象（不制造无谓拷贝）。"""
    body = {"tools": [_openai_tool(f"t{i}") for i in range(27)]}
    assert _dedupe_tools(body) is body


def test_partial_duplicates_dropped_in_order():
    tools = [_openai_tool("a"), _openai_tool("b"), _openai_tool("a"),
             _openai_tool("c"), _openai_tool("b")]
    out = _dedupe_tools({"tools": tools})
    assert [t["function"]["name"] for t in out["tools"]] == ["a", "b", "c"]


def test_anthropic_shape_deduped():
    out = _dedupe_tools({"tools": [_anthropic_tool("x"), _anthropic_tool("x")]})
    assert len(out["tools"]) == 1


def test_nameless_tools_are_kept():
    """取不到名字的条目**不参与去重**：不能因为读不出名字就把工具丢掉。"""
    body = {"tools": [{}, {}, _openai_tool("a"), _openai_tool("a")]}
    out = _dedupe_tools(body)
    assert len(out["tools"]) == 3   # 两个无名 + 一个 a


# ---------------------------------------------------------------------------
# 边界：不能为「修重复」引入新的失败
# ---------------------------------------------------------------------------

def test_no_tools_key_is_noop():
    body = {"messages": []}
    assert _dedupe_tools(body) is body


def test_tools_not_a_list_is_noop():
    body = {"tools": {"oops": "not a list"}}
    assert _dedupe_tools(body) is body


def test_single_tool_is_noop():
    body = {"tools": [_openai_tool("only")]}
    assert _dedupe_tools(body) is body


def test_other_body_fields_preserved():
    body = {"model": "m", "stream": True, "messages": [{"role": "user"}],
            "tools": [_openai_tool("a"), _openai_tool("a")]}
    out = _dedupe_tools(body)
    assert out["model"] == "m" and out["stream"] is True
    assert out["messages"] == [{"role": "user"}]


def test_original_body_not_mutated():
    """返回新对象而非原地改：调用方可能还要用原 body 做摘要/日志。"""
    body = {"tools": [_openai_tool("a"), _openai_tool("a")]}
    _dedupe_tools(body)
    assert len(body["tools"]) == 2
