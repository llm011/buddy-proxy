"""DeepSeek Harness system 模板的渠道校验拦截回归测试。

背景（2026-11-04 实测）：CodeBuddy 对 system prompt 里出现 ``deepseek harness``
（不分大小写）的请求整单拦截，返回 HTTP 400 / ``code 11128 Illegal API
invocation from an unapproved channel``。dsh（deepseek-harness TUI）的默认系统
模板固定带这句，所以它在 codebuddy 通道上**任何请求都失败**；而 400 不在
``_RETRYABLE_STATUS`` 里，不会 failover 到下一个候选上游，整个模型直接不可用。

``desensitize_body(..., compact_harness=True)`` 本来就会把 Claude Code / Codex
的 harness 模板压成短摘要以躲开同类审核，但没有覆盖 DeepSeek Harness。这里锁定
该补丁：命中时压成摘要，且摘要本身不得再含触发词。
"""

from __future__ import annotations

import pytest

from buddy_proxy.core.desensitize import desensitize_body

#: dsh 真实模板的开头（请求体里 system 的第一段），足以触发上游校验。
_DSH_SYSTEM = (
    "You are an AI agent powered by DeepSeek Harness.\n\n"
    "You are a coding agent powered by the deepseek-v4.1-flash model.\n\n"
    "[状态栏] 你有一个状态栏展示给用户。\n\n"
    "Check the [exit code: N] marker on every bash result; "
    "investigate failures before moving on.\n\n"
    "Use the read tool — not shell commands like cat — to inspect text files.\n"
)


def _body(system: str) -> dict:
    return {
        "model": "deepseek-v4.1-flash",
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "hi"},
        ],
    }


def _system_text(body: dict) -> str:
    for msg in body["messages"]:
        if msg.get("role") == "system":
            return msg["content"]
    raise AssertionError("system message missing")


def test_deepseek_harness_system_is_compacted():
    """命中 dsh 模板时必须替换成摘要，且摘要不含触发词。"""
    out = desensitize_body(_body(_DSH_SYSTEM), compact_harness=True)
    text = _system_text(out)

    assert text != _DSH_SYSTEM, "dsh 模板应被压缩，不能原样转发"
    assert "deepseek harness" not in text.lower(), "摘要本身不得再含触发词"
    assert "codebuddy" not in text.lower()


def test_deepseek_harness_mixed_case_also_compacted():
    """大小写变体同样会被上游拦，规则必须不分大小写。"""
    out = desensitize_body(
        _body("You are an AI agent powered by DEEPSEEK HARNESS."),
        compact_harness=True,
    )
    text = _system_text(out)
    assert "deepseek harness" not in text.lower()


def test_plain_system_prompt_untouched():
    """普通 system prompt 不能被误伤（上游只在 harness 字样上拦）。"""
    plain = "You are a helpful assistant. Answer concisely."
    out = desensitize_body(_body(plain), compact_harness=True)
    # 允许脱敏插入零宽空格，但不该被压成摘要
    assert "helpful assistant" in _system_text(out)


def test_deepseek_without_harness_untouched():
    """只出现 DeepSeek（没有 Harness）时上游放行，不该被改写。"""
    plain = "You are running on DeepSeek. Answer concisely."
    out = desensitize_body(_body(plain), compact_harness=True)
    assert "Answer concisely" in _system_text(out)


def test_benign_mention_of_harness_is_not_wiped():
    """用户自己写的 prompt 里提到 Harness，不能被整段替换掉。

    上游拦的是 dsh 那条固定身份声明，不是「提到 Harness」；这种正常业务
    prompt 实测返回 200，替换了反而丢掉用户语义。
    """
    benign = "Help me compare the DeepSeek Harness benchmark numbers."
    out = desensitize_body(_body(benign), compact_harness=True)
    text = _system_text(out)
    assert "benchmark" in text, "正常业务 prompt 不该被整段替换"


def test_long_prompt_mentioning_harness_is_not_wiped():
    """即使很长，只要不是 dsh 身份声明句式，也不该被替换。"""
    long_prompt = "DeepSeek Harness runtime.\n\n" + ("Follow these rules carefully. " * 200)
    out = desensitize_body(_body(long_prompt), compact_harness=True)
    text = _system_text(out)
    assert "Follow these rules" in text, "长但非 dsh 句式不该被替换"


@pytest.mark.parametrize(
    "prompt",
    [
        # 都不是 dsh 的身份声明句式，上游实测全部放行
        "You are a coding agent powered by DeepSeek Harness.",
        "You are an AI assistant powered by DeepSeek Harness.",
        "You are an AI agent powered by DeepSeek.",
        "You are an AI agent. DeepSeek Harness.",
        "Powered by DeepSeek Harness, you are an AI agent.",
    ],
)
def test_similar_phrasings_are_not_compacted(prompt):
    """邻近句式都不拦，不该被误伤。"""
    out = desensitize_body(_body(prompt), compact_harness=True)
    assert "Help with software engineering tasks" not in _system_text(out)


def test_exact_declaration_is_compacted():
    """dsh 的原句（就是它会被拦的那条）必须被替换。"""
    out = desensitize_body(
        _body("You are an AI agent powered by DeepSeek Harness."),
        compact_harness=True,
    )
    assert "Help with software engineering tasks" in _system_text(out)


def test_compaction_is_opt_in():
    """未开 compact_harness 时保持历史行为：只做零宽空格脱敏，不压成摘要。

    注意零宽空格版本的 ``DeepSeek H\\u200barness`` **绕不过** codebuddy 的渠道
    校验（2026-11-04 实测仍返回 11128），所以这条路径只是「历史行为不变」的
    兼容断言，不是修复手段——真正的修复在 ``compact_harness=True`` 这条链上。
    """
    out = desensitize_body(_body(_DSH_SYSTEM), compact_harness=False)
    text = _system_text(out)
    assert len(text) >= len(_DSH_SYSTEM), "未开 compact 时不该被压成短摘要"
    assert "⏵" in text or "状态栏" in text, "原模板正文应保留"
