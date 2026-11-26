"""上游没内容时合成一条可读回复，而不是 502。

2026-11-21 用户第三次遇到 ``gemini-4-hydrogen`` 中断，看到的是
``502 候选上游全部不可用``。DSH TUI 对 502 的处理是 **turn error 直接中断、
不重试**；对空回复则归类 ``EMPTY_RESPONSE`` 自动重试。所以「合成一句带提示的
正常回复」既能不打断会话，又能告诉用户下一步该做什么。

不变量：

1. 提示语必须**用括号包起来**，让它在对话记录里明显是系统提示而非模型正文；
2. 只对「等一会儿可能好」的失败合成（冷却 / 空内容），**停用、风控、认证
   仍报 502**——那些要用户动手，掩盖真因比中断更糟；
3. 流式必须产出合法 SSE（一条 content delta + finish + ``[DONE]``），
   否则客户端解析失败，比 502 还糟；
4. 三条协议（openai / anthropic / responses）形状都要对。
"""

from __future__ import annotations

import json

import pytest

from buddy_proxy.codebuddy_provider.empty_reply import (
    EMPTY_REPLY_NOTICE,
    synthesize_reply,
)


def _read_stream(resp) -> str:
    """同步读取 StreamingResponse 的 body（测试里用 asyncio.run 驱动）。"""
    import asyncio

    async def collect() -> str:
        parts: list[str] = []
        async for chunk in resp.body_iterator:
            parts.append(chunk if isinstance(chunk, str) else chunk.decode())
        return "".join(parts)

    return asyncio.run(collect())


def _deltas(sse_text: str) -> list[dict]:
    out = []
    for line in sse_text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            continue
        out.append(json.loads(data))
    return out


# --- 文案 -------------------------------------------------------------------


def test_notice_is_wrapped_in_parentheses():
    """提示语整体用括号包起来（用户要求：明显是系统提示，不是模型正文）。"""
    assert EMPTY_REPLY_NOTICE.startswith("（")
    assert EMPTY_REPLY_NOTICE.endswith("）")
    assert "减小上下文" in EMPTY_REPLY_NOTICE


# --- 非流式 -----------------------------------------------------------------


def test_nonstream_openai_returns_normal_completion():
    resp = synthesize_reply("gemini-4-hydrogen", "openai",
                            reason="empty_content", body={"stream": False})
    payload = json.loads(resp.body)
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == EMPTY_REPLY_NOTICE
    assert payload["choices"][0]["finish_reason"] == "stop"
    # 合成回复没有真实消耗：token 必须是 0，不能编数污染面板统计
    assert payload["usage"]["prompt_tokens"] == 0


def test_nonstream_anthropic_returns_message_shape():
    resp = synthesize_reply("gemini-4-hydrogen", "anthropic",
                            reason="empty_content", body={"stream": False})
    payload = json.loads(resp.body)
    assert payload["type"] == "message"
    assert payload["role"] == "assistant"
    text = "".join(b.get("text", "") for b in payload["content"])
    assert text == EMPTY_REPLY_NOTICE


def test_cooldown_reason_gets_its_own_wording():
    """冷却类失败给「稍等再试」，与空内容区分开——用户能据此决定等还是换模型。"""
    resp = synthesize_reply("m", "openai", reason="cooldown", body={})
    content = json.loads(resp.body)["choices"][0]["message"]["content"]
    assert "冷却" in content
    assert content != EMPTY_REPLY_NOTICE


# --- 流式 -------------------------------------------------------------------


def test_stream_openai_is_valid_sse():
    resp = synthesize_reply("m", "openai", reason="empty_content",
                            body={"stream": True})
    body = _read_stream(resp)
    assert body.rstrip().endswith("data: [DONE]")
    deltas = _deltas(body)
    assert deltas, "至少要有一条 chunk"
    text = "".join(
        (d.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
        for d in deltas)
    assert text == EMPTY_REPLY_NOTICE
    assert deltas[-1]["choices"][0]["finish_reason"] == "stop"


def test_stream_anthropic_event_sequence():
    """Anthropic 流必须走全局转换器：事件序列与真实流一致，客户端才不报错。"""
    resp = synthesize_reply("m", "anthropic", reason="empty_content",
                            body={"stream": True})
    body = _read_stream(resp)
    events = [line[len("event: "):] for line in body.splitlines()
              if line.startswith("event: ")]
    assert events[0] == "message_start"
    assert "content_block_delta" in events
    assert events[-1] == "message_stop"
    # 正文必须真的进了 text_delta
    assert EMPTY_REPLY_NOTICE in body


def test_stream_flag_read_from_body_when_original_missing():
    """``original`` 为 None（model_order 路径）时仍要认出流式。

    回归：首版只看 ``original``，流式请求被错答成非流式 JSON，客户端解析失败。
    """
    resp = synthesize_reply("m", "openai", reason="cooldown",
                            original=None, body={"stream": True})
    assert hasattr(resp, "body_iterator"), "流式请求必须回 StreamingResponse"


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_stream_media_type(protocol):
    resp = synthesize_reply("m", protocol, reason="cooldown", body={"stream": True})
    assert resp.media_type == "text/event-stream"
