"""codebuddy 通道也必须丢弃被截断的半截工具参数。

用户 2026-11-28 报障的最后一段：raccoon 修好之后，``kimi-k3.1-pro`` 被换档到
codebuddy，而 ``collect_upstream`` 里**没有任何截断保护**——半截 arguments 原样
返回，客户端照旧报 ``invalid_tool_arguments: must have required property
'pattern'``。

实测（走 codebuddy，同一 prompt）：``max_tokens=45/50`` 返回
``finish_reason=length`` + 半截 arguments。
"""

from __future__ import annotations

import pytest

from buddy_proxy.codebuddy_provider.pipeline import _arguments_complete


@pytest.mark.parametrize("label,arguments,expected", [
    # 完整形态：都不能误杀
    ("标准 JSON", '{"pattern":"x"}', True),
    ("dict 形态", {"pattern": "x"}, True),
    ("零参空串", "", True),
    ("零参空对象", "{}", True),
    ("零参空白串", "   ", True),
    ("缺失", None, True),
    # 真半截：必须丢
    ("只有左括号", "{", False),
    ("截断键值", '{"path":"src', False),
    ("截断字符串", '{"pattern": "TO', False),
])
def test_arguments_complete_detection(label, arguments, expected):
    tc = {"function": {"name": "T", "arguments": arguments}}
    assert _arguments_complete(tc) is expected, label


def test_matches_raccoon_semantics():
    """codebuddy 与 raccoon 的判定口径必须一致。

    两边各写一套的话，修好一条通道另一条照样漏——这正是本次 bug 的形状
    （raccoon 修了、codebuddy 没修）。
    """
    from buddy_proxy.raccoon.provider import _tool_arguments_complete

    samples = [
        '{"pattern":"x"}', "{", '{"path":"src', "", "{}", "   ", None,
        {"pattern": "x"}, '{"a":1}',
    ]
    for raw in samples:
        a = _arguments_complete({"function": {"name": "T", "arguments": raw}})
        b = _tool_arguments_complete({"function": {"name": "T", "arguments": raw}})
        assert a is b, f"两条通道口径不一致：{raw!r} raccoon={b} codebuddy={a}"


def test_collect_upstream_drops_truncated_when_finish_length():
    """源码层面确认 collect_upstream 在 finish_reason=length 时过滤。

    真实路径要打上游，单测里用源码断言守住「这段保护没被删掉」。
    """
    import inspect

    from buddy_proxy.codebuddy_provider import pipeline

    src = inspect.getsource(pipeline.collect_upstream)
    assert '_arguments_complete(' in src, (
        "collect_upstream 没有走截断过滤，半截参数会从这里漏给客户端")
    assert 'finish_reason == "length"' in src


def test_dsml_fallback_does_not_mask_length():
    """DSML 兜底不能把 ``finish_reason=length`` 盖成 ``tool_calls``。

    DSML 是从正文里抠出来的工具调用，和「输出被 max_tokens 截断」是两件
    独立的事。盖成 tool_calls 会让客户端以为这轮输出完整、不去重试，截断的
    正文就静默丢了——用户看到的是「回答莫名其妙断掉，但没报错」。
    """
    import inspect

    from buddy_proxy.codebuddy_provider import pipeline

    src = inspect.getsource(pipeline.collect_upstream)
    # 覆盖条件里必须显式排除 length
    assert 'finish_reason != "length"' in src, (
        "DSML 兜底会盖掉 length，截断信息丢失")
