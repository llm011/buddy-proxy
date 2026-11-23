"""CodeBuddy 上游「渠道校验」中和（``provider._neutralize_leading_system``）。

锁住 11128 的两个真实根因（都表现为「这个账号的模型全废了」，实际账号没事）：

**根因 A：客户端签名出现在被扫描的位置**（2026-11-02 起，2026-11-08 扩大）

    400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}

上游会扫 system / assistant / developer 消息的**每个 text 块首段**，命中
别的 Agent 客户端开场白就整单拒。实测矩阵见 ``_SYSTEM_PROMPT_SIGNATURES``
与文件上方「踩坑档案」。

**根因 B：``role: "developer"`` 上游根本不认**（2026-11-08，dsh 0.2.2 起）

dsh 0.2.2 把 system prompt 发成 ``developer``（OpenAI 新角色名），而上游只认
经典角色——**哪怕 content 是干净的 "hi" 也 400**。必须先降级成 ``system``，
再走签名中和；两个修法的顺序不能调换（见
``test_developer_with_signature_needs_both_fixes``）。

⚠ 改动匹配语义或扫描范围前，**请先对真实上游重跑矩阵**，不要凭「看起来更
宽松/更严格」推理——历史上两次都是「只覆盖了当时实测到的那一种形态」，
第二次因此让保护静默失效了一整天。

运行：
    .venv/bin/python -m pytest tests/test_codebuddy_system_signature.py -v
"""
from __future__ import annotations

import pytest

from buddy_proxy.codebuddy_provider.provider import (
    _NEUTRALIZED_ROLES,
    _demote_developer_role,
    _neutralize_leading_system,
    _neutralize_system_signature,
)

DSH = "You are an AI agent powered by DeepSeek Harness."
CLAUDE = "You are Claude Code, Anthropic's official CLI for Claude."
MASK = "[redacted: client identity]"

#: Claude Code 2.1.278 二进制里同时存在、且都以「首段」形态发出的另一种 persona：
#: 官方 CLI 那句后面接 ``running within the Claude Agent SDK``。它只比 ``CLAUDE``
#: 多一个逗号，所以 ``CLAUDE`` 若带句号就前缀匹配不到——2026-11-23 实测正是
#: 这条变体在打 11128。
CLAUDE_AGENT_SDK_VARIANT = (
    "You are Claude Code, Anthropic's official CLI for Claude, "
    "running within the Claude Agent SDK."
)

#: Agent SDK 的**独立** persona（不是上面那条的尾巴）。2026-11-23 实测上游
#: 返回 200，所以刻意**不**进 ``_SYSTEM_PROMPT_SIGNATURES``。
CLAUDE_AGENT_SDK_STANDALONE = (
    "You are a Claude agent, built on Anthropic's Claude Agent SDK."
)


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


# ---------------------------------------------------------------------------
# Claude Code 2.x 的逗号变体（2026-11-23 实测新增）
# ---------------------------------------------------------------------------

def test_claude_comma_variant_is_masked():
    """``...for Claude, running within the Claude Agent SDK.`` 实测 400，必须中和。

    这是「只覆盖了当时实测到的那一种形态」的第三次翻车：签名表里那条带句号，
    ``startswith`` 匹配不到逗号变体，于是变体原样发出去、稳定打 11128。
    去掉句号后靠前缀命中；断言「变体里不再残留原句」比断言具体输出更抗重构。
    """
    out = _neutralize_system_signature(CLAUDE_AGENT_SDK_VARIANT)
    assert out != CLAUDE_AGENT_SDK_VARIANT
    assert "You are Claude Code" not in out
    # 逗号变体只是「同一句 + 尾巴」，尾巴该原样保留（掩码只吃紧邻的一个标点）。
    assert out == MASK + " running within the Claude Agent SDK."


def test_claude_comma_variant_inside_body_is_masked():
    """真实 body 里也要走通（防止只在裸字符串上正确）。"""
    out = _neutralize_leading_system(_body(CLAUDE_AGENT_SDK_VARIANT))
    assert out is not None
    content = out["messages"][0]["content"]
    assert "You are Claude Code" not in content


def test_signature_without_trailing_period_masked():
    """不带句号的写法（上游同样 400）也要命中。"""
    bare = "You are Claude Code, Anthropic's official CLI for Claude"
    assert _neutralize_system_signature(bare) == MASK


def test_trailing_period_is_absorbed_by_mask():
    """标准形态掩码后**精确等于** MASK，不残留原客户端的句号。

    签名不带句号是为了兼容逗号变体，代价是 ``...for Claude.`` 会剩一个孤零零
    的 ``.``。掩码吃标点后两种形态收敛到同一个输出——这条锁住那个吸收动作，
    删掉 ``_SYSTEM_PROMPT_MASK_TAIL_PUNCT`` 会立刻红。
    """
    assert _neutralize_system_signature(CLAUDE) == MASK
    assert _neutralize_system_signature(CLAUDE.rstrip(".")) == MASK


def test_standalone_agent_sdk_persona_untouched():
    """Agent SDK 的**独立** persona 实测 200，不能顺手加进签名表。

    这条是防「过度修复」的：它跟 ``CLAUDE`` 只共享 ``Claude`` 这个词，上游
    明确放行。加了就等于平白篡改用户 prompt，正是本文件反复强调的反面。
    """
    assert _neutralize_system_signature(CLAUDE_AGENT_SDK_STANDALONE) is \
        CLAUDE_AGENT_SDK_STANDALONE


def test_standalone_agent_sdk_persona_with_long_template_untouched():
    """真实形态：独立 persona 后面跟着整段 Claude Code 模板，实测仍 200。"""
    src = CLAUDE_AGENT_SDK_STANDALONE + "\n\n# Harness\nUse the tools available."
    assert _neutralize_system_signature(src) is src


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


# ---------------------------------------------------------------------------
# developer 角色：上游根本不认（2026-11-08，dsh 0.2.2 起踩的坑）
# ---------------------------------------------------------------------------
#
# 现象：codebuddy 通道全线 400/11128，看起来像「账号的模型全废了」。
# 真因：dsh 0.2.2 把 system prompt 发成 role="developer"（OpenAI 新角色名），
# 而上游只认经典角色——**哪怕 content 是干净的 "hi" 也 400**。
#
# 实测矩阵（CN 账号，真实上游）：
#   developer + 干净内容 → 400   ← 角色本身被拒
#   system    + 干净内容 → 200
#   developer + 签名     → 400
#   降级成 system + 签名 → 400（所以还得再走签名中和）
#   降级成 system + 屏蔽 → 200
#
# ⚠ 两个修法的顺序不能调换：先降级、再扫签名。反过来写的话，降级前它不在
# 扫描名单里、降级后又没人扫，签名照样漏过去。

def test_developer_role_is_demoted_to_system():
    """developer → system（语义等价，OpenAI 文档：developer 是 system 的新名）。"""
    body = {"messages": [
        {"role": "developer", "content": "clean"},
        {"role": "user", "content": "hi"},
    ]}
    out = _demote_developer_role(body)
    assert out is not body
    assert out["messages"][0]["role"] == "system"
    # 其余字段与消息原样保留
    assert out["messages"][0]["content"] == "clean"
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_developer_demotion_leaves_other_roles_alone():
    body = {"messages": [
        {"role": "system", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": "c"},
        {"role": "tool", "content": "d", "tool_call_id": "x"},
    ]}
    assert _demote_developer_role(body) is body


def test_developer_demotion_is_noop_without_developer():
    for messages in ([], [{"role": "system", "content": "x"}], "not-a-list"):
        body = {"messages": messages}
        assert _demote_developer_role(body) is body


def test_developer_with_signature_needs_both_fixes():
    """只降级不够：降级后的 system 里签名还在，必须再扫一遍。

    这条钉住调用顺序——先 _demote_developer_role 再 _neutralize_leading_system。
    """
    body = {"messages": [
        {"role": "developer", "content": DSH},
        {"role": "user", "content": "hi"},
    ]}
    demoted = _demote_developer_role(body)
    assert demoted["messages"][0]["role"] == "system"
    assert demoted["messages"][0]["content"] == DSH, "降级本身不改内容"

    neutralized = _neutralize_leading_system(demoted)
    assert neutralized["messages"][0]["content"] == MASK


def test_developer_is_in_neutralized_roles():
    """developer 也要在扫描名单里：万一哪天降级被摘掉，这层还能兜住。"""
    assert "developer" in _NEUTRALIZED_ROLES
