"""CodeBuddy 上游「system prompt 渠道校验」中和（``provider._neutralize_leading_system``）。

锁住 11128 的一个真实根因（2026-11-02 实测）：

    turn error · 400 {"detail":{"error":{"message":"Upstream API error (HTTP 400)"…}}}   # 客户端只看到这句

上游真身是::

    {"code":11128,"msg":"Illegal API invocation from an unapproved channel",
     "retry_policy":{"retry":false}, …}

触发条件是 system prompt **首段**命中了别的 Agent 客户端的开场白
（``You are an AI agent powered by DeepSeek Harness.`` —— dsh 的 harness:identity
段；``You are Claude Code, Anthropic's official CLI for Claude.``）。400 属确定性
错误，model_order 不换档，客户端只看到一句无信息的 upstream_error。

下面每条断言都对应一次真实上游往返，不是猜的（矩阵见 ``_SYSTEM_PROMPT_SIGNATURES``
旁注释）。改匹配语义前请先复测，不要凭「看起来更宽松」调整。

运行：
    .venv/bin/python -m pytest tests/test_codebuddy_system_signature.py -v
"""
from __future__ import annotations

import pytest

from buddy_proxy.codebuddy_provider.provider import (
    _neutralize_leading_system,
    _neutralize_system_signature,
)

DSH = "You are an AI agent powered by DeepSeek Harness."
CLAUDE = "You are Claude Code, Anthropic's official CLI for Claude."
MASK = "[redacted: client identity]"


def _body(content: str, *, role: str = "system") -> dict:
    return {"model": "m", "stream": True,
            "messages": [{"role": role, "content": content},
                         {"role": "user", "content": "hi"}]}


# ---------------------------------------------------------------------------
# 命中的两个签名本身
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sig", [DSH, CLAUDE])
def test_bare_signature_is_masked(sig):
    """实测 400 的两种写法，都必须被中和。"""
    out = _neutralize_system_signature(sig)
    assert out == MASK
    assert sig not in out


@pytest.mark.parametrize("prefix", [" ", "  ", "   "])
def test_leading_spaces_still_masked(prefix):
    """前导空格不影响上游判定（实测 400），所以也必须中和。"""
    assert _neutralize_system_signature(prefix + DSH) == prefix + MASK


def test_signature_with_trailing_text_masked():
    """实测 ``Harness. + 后文`` 仍是 400：签名命中就整单拒，与后文无关。"""
    src = DSH + "\n\nYou are a coding agent."
    assert DSH not in _neutralize_system_signature(src)


def test_signature_with_trailing_tab_indent_masked():
    """首段里签名后换行、下一行前导是制表符——实测仍 400。"""
    src = DSH + "\n\tYou are a coding agent."
    assert DSH not in _neutralize_system_signature(src)


# ---------------------------------------------------------------------------
# 不该动的位置：上游根本不看，改了就是篡改用户 prompt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", ["\n", "\n\n", "\t", "\r\n"])
def test_newline_or_tab_prefix_is_exempt(prefix):
    """实测放行：首段之前先有换行/制表符，签名就不再触发校验。"""
    src = prefix + DSH
    assert _neutralize_system_signature(src) is src


@pytest.mark.parametrize("prefix", [
    "abc.\n\n",                 # 有前言段
    "# Role\n",                 # Markdown 标题段
    "DeepSeek Harness\n\n",     # 别处先提过这个名字
    "Follow these instructions carefully.\n\n",
])
def test_later_paragraph_is_exempt(prefix):
    """签名出现在后面的段落里：实测放行。"""
    src = prefix + DSH + "\n\nUse tools wisely."
    assert _neutralize_system_signature(src) is src


def test_signature_in_second_system_block_is_masked():
    """messages 里**后面**的 system 块同样会被上游拦（2026-11-08 复测）。

    2026-11-02 那版结论是「后面的 system 块放行」，实测已不再成立：上游把
    扫描面扩到了所有 system / assistant 消息。旧实现只查第一条，导致屏蔽
    完全失效、codebuddy 通道被反复判死（日志里「已中性化」一次都没出现过）。
    """
    body = {"messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "system", "content": DSH},
        {"role": "user", "content": "hi"},
    ]}
    out = _neutralize_leading_system(body)
    assert out is not body
    assert out["messages"][1]["content"] == MASK
    # 没命中的那条 system 不能动
    assert out["messages"][0]["content"] == "You are a helpful assistant."


def test_signature_in_assistant_message_is_masked():
    """assistant 消息同样被扫（实测 400），必须中和。"""
    body = {"messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": DSH},
    ]}
    out = _neutralize_leading_system(body)
    assert out["messages"][2]["content"] == MASK


def test_signature_in_block_array_is_masked():
    """分块数组的每个 text 块都要查（实测第 2 块命中同样 400）。"""
    body = {"messages": [
        {"role": "system", "content": [
            {"type": "text", "text": "clean intro"},
            {"type": "text", "text": DSH},
        ]},
        {"role": "user", "content": "hi"},
    ]}
    out = _neutralize_leading_system(body)
    blocks = out["messages"][0]["content"]
    assert blocks[0]["text"] == "clean intro"
    assert blocks[1]["text"] == MASK


def test_signature_in_late_system_block_is_masked():
    """对话中途再插一条 system（实测 400）也要中和。"""
    body = {"messages": [
        {"role": "system", "content": "clean"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "q"},
        {"role": "system", "content": DSH},
    ]}
    out = _neutralize_leading_system(body)
    assert out["messages"][4]["content"] == MASK


def test_signature_in_tool_message_untouched():
    """tool 消息不被扫（实测 200），不能篡改工具结果。"""
    body = {"messages": [
        {"role": "system", "content": "clean"},
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "c1", "content": DSH},
    ]}
    assert _neutralize_leading_system(body) is body


def test_non_text_blocks_preserved():
    """分块数组里的非 text 块（图片等）原样保留。"""
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}
    body = {"messages": [
        {"role": "system", "content": [image, {"type": "text", "text": DSH}]},
        {"role": "user", "content": "hi"},
    ]}
    out = _neutralize_leading_system(body)
    assert out["messages"][0]["content"][0] == image
    assert out["messages"][0]["content"][1]["text"] == MASK


# ---------------------------------------------------------------------------
# 近邻串：不命中就不能改
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "You are an AI agent powered by Qwen Harness.",     # 换厂商名：实测 200
    "You are an AI agent powered by DeepSeek.",         # 少 Harness：实测 200
    "You are an AI agent powered by DeepSeek Harnes.",  # 拼错：实测 200
    "You are an AI agent\npowered by DeepSeek Harness.",# 中间断行：实测 200
    "You are an AI agent, powered by DeepSeek Harness.",# 多了逗号：实测 200
    "You are an AI agent that is powered by DeepSeek Harness.",
    "You are an AI coding agent powered by DeepSeek Harness.",
    "You are Claude Code.",                             # 只到前半句：实测 200
    "You are Codex, a coding agent based on GPT-5.",    # 实测 200
    "You are Trae.",
    "You are CodeBuddy.",
    "You are Cline, an AI coding agent.",
    "You are Cursor.",
    "",
    "You are a helpful assistant.",
])
def test_near_misses_untouched(text):
    """这些**全部实测 200**：改了它们只会平白破坏用户 prompt。"""
    assert _neutralize_system_signature(text) is text


def test_case_sensitive_deepseek_variant_untouched():
    """全大写 ``DEEPSEEK HARNESS`` 是未实测的变体，不做猜测性匹配。

    匹配按 ``_SYSTEM_PROMPT_SIGNATURES`` 逐字（区分大小写）：这份测试只锁
    「已知不命中的形态不被误改」，不锁未验证的变体。
    """
    text = "You are an AI agent powered by DEEPSEEK HARNESS."
    assert _neutralize_system_signature(text) is text


# ---------------------------------------------------------------------------
# body 级：形状、边界、不变性
# ---------------------------------------------------------------------------

def test_body_content_masked_and_other_fields_preserved():
    body = {**_body(DSH + "\n\nmore"), "max_completion_tokens": 32}
    out = _neutralize_leading_system(body)
    assert out is not body
    assert out["messages"][0]["content"].startswith(MASK)
    assert out["max_completion_tokens"] == 32
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_signature_in_user_message_untouched():
    """上游只查 system（实测 user 里出现签名放行）。"""
    body = _body(DSH, role="user")
    assert _neutralize_leading_system(body) is body


def test_no_messages_key_is_noop():
    body = {"model": "m"}
    assert _neutralize_leading_system(body) is body


@pytest.mark.parametrize("messages", [
    [],                              # 空
    "not-a-list",
    [{"role": "user", "content": DSH}],                 # 首条是 user：上游不扫
    [{"role": "system", "content": None}],
    [{"role": "system"}],
    ["not-a-dict"],
])
def test_odd_shapes_are_noop(messages):
    """没法安全处理的一律原样返回——绝不把能通的请求改成 400。"""
    body = {"model": "m", "messages": messages}
    assert _neutralize_leading_system(body) is body


def test_block_array_with_signature_is_now_patched():
    """分块数组**不再**是 noop：实测这种形态同样 400（见上面的专门用例）。"""
    body = {"model": "m", "messages": [
        {"role": "system", "content": [{"type": "text", "text": DSH}]}]}
    out = _neutralize_leading_system(body)
    assert out is not body
    assert out["messages"][0]["content"][0]["text"] == MASK


def test_original_body_not_mutated():
    body = _body(DSH)
    _neutralize_leading_system(body)
    assert body["messages"][0]["content"] == DSH


def test_repeated_calls_are_idempotent():
    once = _neutralize_leading_system(_body(DSH))
    twice = _neutralize_leading_system(once)
    assert twice == once
