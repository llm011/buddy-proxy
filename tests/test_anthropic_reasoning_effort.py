"""Anthropic ``thinking`` → chat ``reasoning_effort`` 映射测试（完全离线）。

背景：``/v1/messages`` 进来的 ``thinking`` 以前整段被丢掉——客户端设了推理强度、
日志里也看不出来，只留下「怎么不思考了」。2026-11-04 实测 openai 路径同一模型能拿到
reasoning_tokens=282，anthropic 路径恒为 0。

覆盖：
- budget 分档边界（low / medium / high）
- type=disabled → none
- client 直接发 reasoning_effort 时优先用它
- 两个字段都没有时不写入，保持原状
- 端到端：``/v1/messages`` 请求经路由后，转发给上游的 chat body 带上了 effort

运行：
    .venv/bin/python -m pytest tests/test_anthropic_reasoning_effort.py -v
"""
from __future__ import annotations

from typing import Any

import pytest

from buddy_proxy.protocols.anthropic_adapter import (
    anthropic_request_to_chat,
    effort_from_thinking,
)


def _body(**extra: Any) -> dict:
    return {"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "hi"}], **extra}


# ---- effort_from_thinking：分档 ----


@pytest.mark.parametrize(
    ("budget", "expected"),
    [
        (1024, "low"),
        (2048, "low"),     # 边界：上限含在 low
        (2049, "medium"),
        (8192, "medium"),  # 边界：上限含在 medium
        (8193, "high"),
        (32768, "high"),
    ],
)
def test_budget_buckets(budget: int, expected: str):
    assert effort_from_thinking({"type": "enabled", "budget_tokens": budget}) == expected


def test_enabled_without_budget_falls_back_to_medium():
    """type=enabled 但没给预算，语义仍是「要思考」，不能退化成不思考。"""
    assert effort_from_thinking({"type": "enabled"}) == "medium"
    assert effort_from_thinking({"type": "enabled", "budget_tokens": 0}) == "medium"
    assert effort_from_thinking({"type": "enabled", "budget_tokens": "8192"}) == "medium"


def test_disabled_maps_to_none():
    assert effort_from_thinking({"type": "disabled"}) == "none"


def test_unknown_or_missing_returns_none():
    assert effort_from_thinking(None) is None
    assert effort_from_thinking({}) is None
    assert effort_from_thinking({"type": "weird"}) is None
    assert effort_from_thinking("enabled") is None  # 非 dict 不猜


# ---- anthropic_request_to_chat：落进 chat body ----


def test_thinking_becomes_reasoning_effort():
    chat = anthropic_request_to_chat(
        _body(thinking={"type": "enabled", "budget_tokens": 16000}))
    assert chat["reasoning_effort"] == "high"
    assert "thinking" not in chat


def test_explicit_reasoning_effort_wins_over_thinking():
    """客户端两套字段混着发时，显式 reasoning_effort 优先。"""
    chat = anthropic_request_to_chat(
        _body(reasoning_effort="HIGH",
              thinking={"type": "enabled", "budget_tokens": 1024}))
    assert chat["reasoning_effort"] == "high"


def test_reasoning_effort_alone_is_forwarded():
    chat = anthropic_request_to_chat(_body(reasoning_effort="low"))
    assert chat["reasoning_effort"] == "low"


def test_no_thinking_no_effort_adds_nothing():
    chat = anthropic_request_to_chat(_body())
    assert "reasoning_effort" not in chat


def test_blank_reasoning_effort_falls_through_to_thinking():
    chat = anthropic_request_to_chat(
        _body(reasoning_effort="   ",
              thinking={"type": "enabled", "budget_tokens": 2048}))
    assert chat["reasoning_effort"] == "low"


def test_disabled_thinking_sends_none():
    chat = anthropic_request_to_chat(_body(thinking={"type": "disabled"}))
    assert chat["reasoning_effort"] == "none"


# ---- 回归：既有透传参数不受影响 ----


def test_existing_passthrough_still_works():
    chat = anthropic_request_to_chat(
        _body(max_tokens=100, temperature=0.3, top_p=0.9, stop=["x"], top_k=5,
              thinking={"type": "enabled", "budget_tokens": 4096}))
    for key in ("max_tokens", "temperature", "top_p", "stop", "top_k"):
        assert key in chat
    assert chat["reasoning_effort"] == "medium"


# ---- 链路级：qoder 的出站白名单不能把 reasoning_effort 过滤掉 ----


def test_qoder_upstream_body_keeps_reasoning_effort():
    """``_PASSTHROUGH_FIELDS`` 是白名单——漏了字段就是「根本没生效」。

    anthropic 路由把 thinking 转成 chat 的 reasoning_effort 之后，qoder 这条
    model_order 档位（deepseek-v4.1-flash 的首选通道之一）必须真的发出去。
    """
    from buddy_proxy.qoder.config import Region
    from buddy_proxy.qoder.provider import QoderProvider

    provider = QoderProvider(
        Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    out = provider._build_upstream(
        {"model": "deepseek-v4.1-flash",
         "messages": [{"role": "user", "content": "hi"}],
         "reasoning_effort": "high"},
        "dfmodel",
    )
    assert out["reasoning_effort"] == "high"

