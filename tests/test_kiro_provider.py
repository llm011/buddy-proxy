"""kiro 通道：EventStream 解析、请求体翻译、凭证处理、双协议输出。

## 为什么值得单独锁

kiro 上游的两个「反直觉」约束都是实测踩出来的，很容易被后来者改回去：

1. ``User-Agent`` 必须带 ``Kiro/<version>``，否则回 403「订阅不支持」——
   看起来像 token 失效或没订阅，实际只是客户端身份没对上；
2. 请求体**不能带** ``editorState``，带了回 400 ``REQUEST_BODY_INVALID``。

以及响应是 **AWS EventStream 二进制帧**，不是 SSE——按文本行解析会得到乱码。

本文件只做离线测试（合成帧），真实往返由开发时的手测覆盖。
"""

from __future__ import annotations

import json
import struct

import pytest

from buddy_proxy.kiro import credentials as creds
from buddy_proxy.kiro import stream as kiro_stream
from buddy_proxy.kiro.provider import (
    _USER_AGENT,
    _chat_to_kiro,
    _messages_to_conversation,
    auth_headers,
)


# ---------------------------------------------------------------------------
# 合成 EventStream 帧
# ---------------------------------------------------------------------------


def _header_bytes(name: str, value: str) -> bytes:
    """字符串型 header：``[1B 名字长][名字][1B 类型=7][2B 值长][值]``。"""
    n = name.encode()
    v = value.encode()
    return bytes([len(n)]) + n + bytes([7]) + struct.pack(">H", len(v)) + v


def make_frame(event_type: str, payload: dict) -> bytes:
    headers = _header_bytes(":event-type", event_type)
    headers += _header_bytes(":content-type", "application/json")
    headers += _header_bytes(":message-type", "event")
    body = json.dumps(payload).encode()
    total = 12 + len(headers) + len(body) + 4
    return (struct.pack(">I", total) + struct.pack(">I", len(headers))
            + b"\x00" * 4  # prelude CRC（解析器不校验，占位即可）
            + headers + body + b"\x00" * 4)  # message CRC


def _stream_of(*frames: bytes) -> list[bytes]:
    return list(frames)


# ---------------------------------------------------------------------------
# EventStream 解析
# ---------------------------------------------------------------------------


def test_parses_text_event():
    raw = make_frame("assistantResponseEvent", {"content": "Hello"})
    events = list(kiro_stream.iter_events([raw]))
    assert events == [{"type": "text", "text": "Hello"}]


def test_parses_metering_event_as_credit():
    raw = make_frame("meteringEvent", {"unit": "credit", "usage": 0.0054})
    events = list(kiro_stream.iter_events([raw]))
    assert events == [{"type": "credit", "credit": 0.0054}]


def test_parses_context_usage_event():
    raw = make_frame("contextUsageEvent", {"contextUsagePercentage": 2.05})
    events = list(kiro_stream.iter_events([raw]))
    assert events[0]["type"] == "context"
    assert events[0]["percent"] == 2.05


def test_handles_arbitrary_chunk_boundaries():
    """TCP 分包会任意切：一帧横跨多个 chunk、一个 chunk 含多帧，都要能解。"""
    raw = (make_frame("assistantResponseEvent", {"content": "AB"})
           + make_frame("assistantResponseEvent", {"content": "CD"}))
    for size in (1, 3, 7, 13, 29):
        chunks = [raw[i:i + size] for i in range(0, len(raw), size)]
        texts = [e["text"] for e in kiro_stream.iter_events(chunks) if e["type"] == "text"]
        assert texts == ["AB", "CD"], f"chunk size {size} 解出 {texts}"


def test_unknown_event_types_are_ignored():
    """上游会加新事件类型（如 codeReferenceEvent），不能因此报错。"""
    raw = make_frame("codeReferenceEvent", {"foo": "bar"})
    assert list(kiro_stream.iter_events([raw])) == []


def test_empty_content_is_skipped():
    raw = make_frame("assistantResponseEvent", {"content": ""})
    assert list(kiro_stream.iter_events([raw])) == []


def test_error_frame_raises():
    raw = make_frame("error", {"message": "upstream boom"})
    with pytest.raises(kiro_stream.StreamError, match="upstream boom"):
        list(kiro_stream.iter_events([raw]))


def test_garbage_bytes_do_not_hang():
    """脏数据要能跳过并继续，不能卡死整条流。"""
    raw = b"\xff\xff\xff\xff" + make_frame("assistantResponseEvent", {"content": "OK"})
    events = list(kiro_stream.iter_events([raw]))
    assert any(e.get("text") == "OK" for e in events)


# ---------------------------------------------------------------------------
# 请求体翻译
# ---------------------------------------------------------------------------


def test_chat_to_kiro_shape():
    body = {"model": "claude-haiku-4.5",
            "messages": [{"role": "user", "content": "hi"}]}
    out = _chat_to_kiro(body)
    state = out["conversationState"]
    assert state["chatTriggerType"] == "MANUAL"
    assert state["currentMessage"]["userInputMessage"]["modelId"] == "claude-haiku-4.5"
    assert state["currentMessage"]["userInputMessage"]["origin"] == "AI_EDITOR"


def test_chat_to_kiro_never_sends_editor_state():
    """踩过的坑：带 editorState 上游回 400 REQUEST_BODY_INVALID。"""
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    dumped = json.dumps(_chat_to_kiro(body))
    assert "editorState" not in dumped
    assert "editor_state" not in dumped


def test_system_prompt_is_prepended_to_user_message():
    """Kiro 没有独立 system 字段：system 要拼到当前用户消息前面。"""
    body = {"model": "m", "messages": [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "hi"},
    ]}
    content, _, _hist = _messages_to_conversation(body)
    assert "You are terse." in content
    assert content.index("You are terse.") < content.index("hi")


def test_developer_role_treated_as_system():
    """dsh 0.2.2 起用 developer 角色；Kiro 侧没有这个概念，按 system 处理。"""
    body = {"model": "m", "messages": [
        {"role": "developer", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]}
    content, _, _hist = _messages_to_conversation(body)
    assert "be brief" in content


def test_block_array_content_flattened_to_text():
    body = {"model": "m", "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "part1 "},
            {"type": "image_url", "image_url": {"url": "x"}},
            {"type": "text", "text": "part2"},
        ]},
    ]}
    content, _, _hist = _messages_to_conversation(body)
    assert content == "part1 part2", "只拼 text 块，图片本通道暂不支持"


def test_assistant_history_preserved():
    body = {"model": "m", "messages": [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]}
    content, _, _hist = _messages_to_conversation(body)
    # currentMessage 是最后一条 user
    assert content == "q2"


def test_tools_are_not_silently_dropped():
    """本轮不支持工具：如实告警（日志），但请求本身不能炸。"""
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f"}}]}
    out = _chat_to_kiro(body)  # 不抛异常
    assert out["conversationState"]["currentMessage"]["userInputMessage"]["content"] == "hi"


# ---------------------------------------------------------------------------
# 请求头
# ---------------------------------------------------------------------------


def test_auth_headers_carry_kiro_user_agent():
    """这个头是必须的：少了它上游 403「订阅不支持」。"""
    headers = auth_headers("tok")
    assert "Kiro/" in headers["User-Agent"]
    assert "Kiro/" in headers["X-Amz-User-Agent"]
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Accept"] == "application/vnd.amazon.eventstream"


def test_user_agent_version_constant_present():
    assert "Kiro/" in _USER_AGENT


# ---------------------------------------------------------------------------
# 双协议输出
# ---------------------------------------------------------------------------


def test_openai_events_shape():
    raw = [make_frame("assistantResponseEvent", {"content": "Hi"})]
    out = list(kiro_stream.to_openai_events(
        raw, model="m", request_id="r1", created=0))
    assert out[0].startswith("data: ")
    payload = json.loads(out[0][len("data: "):].strip())
    assert payload["choices"][0]["delta"]["content"] == "Hi"
    assert payload["choices"][0]["delta"]["role"] == "assistant"
    # 收尾：finish_reason 与 [DONE] 都不能少
    assert '"finish_reason": "stop"' in out[-2]
    assert out[-1].strip() == "data: [DONE]"


def test_anthropic_events_sequence():
    """Anthropic 事件顺序是硬要求，缺一段客户端就断开。"""
    raw = [make_frame("assistantResponseEvent", {"content": "Hi"})]
    out = list(kiro_stream.to_anthropic_events(raw, model="m", message_id="msg1"))
    names = [line.split("\n")[0].removeprefix("event: ") for line in out]
    assert names == ["message_start", "content_block_start", "content_block_delta",
                     "content_block_stop", "message_delta", "message_stop"]


# ---------------------------------------------------------------------------
# 凭证：坐标与刷新判定
# ---------------------------------------------------------------------------


def test_account_id_prefers_profile_arn():
    """profileArn 稳定跨登录（clientIdHash 每次登录都变），优先用它。"""
    aid = creds._account_id(profile_arn="arn:aws:codewhisperer:us-east-1:1:profile/KGVMAMAMGGDK",
                            client_id_hash="abc123", access_token="t")
    assert aid == "p-kgvmamamggdk"


def test_account_id_falls_back_to_client_hash():
    aid = creds._account_id(profile_arn="", client_id_hash="d5c7abe37885",
                            access_token="t")
    assert aid.startswith("c-") and "d5c7abe37885" in aid


def test_needs_refresh_boundaries():
    now = 1_000_000.0
    assert creds.needs_refresh({"expires_at": now + 100}, now=now) is True
    assert creds.needs_refresh({"expires_at": now + 3600}, now=now) is False
    # 缺字段/坏值一律视为该刷新（宁可多刷一次，别等过期）
    assert creds.needs_refresh({}, now=now) is True
    assert creds.needs_refresh({"expires_at": "x"}, now=now) is True


def test_parse_expiry_supports_both_shapes():
    """上游给过 ``...Z`` 与 ``...+00:00`` 两种。"""
    a = creds._parse_expiry("2026-11-08T19:57:49.063Z")
    b = creds._parse_expiry("2026-11-08T19:57:49.063+00:00")
    assert a == b and a > 0


def test_parse_expiry_rejects_garbage():
    for bad in (None, "", "not-a-date", 0, False):
        assert creds._parse_expiry(bad) == 0.0


def test_refresh_requires_client_registration(monkeypatch, tmp_path):
    """缺 client 注册信息时给出「去登录一次」的可操作提示，而不是裸异常。"""
    monkeypatch.setattr(creds, "SSO_CACHE_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="client 凭据"):
        creds.refresh_tokens({"refresh_token": "rt", "client_id_hash": "nope",
                              "region": "us-east-1"})


def test_refresh_without_refresh_token_fails_clearly():
    with pytest.raises(RuntimeError, match="refreshToken"):
        creds.refresh_tokens({"region": "us-east-1"})


# ---------------------------------------------------------------------------
# 积分回传：日志的「积分」列靠这个
# ---------------------------------------------------------------------------
#
# 上游 meteringEvent 给的是**真实计费**（精确到 1e-6）。要让日志显示它，
# 必须把 credit 放进 **usage** 里——metrics 的非流式路径读 `payload["usage"]`
# 再走 normalize_usage，顶层字段它看不到（踩过一次：响应体有 credit，
# 日志却记成估算 0.0）。

def test_usage_payload_carries_credit_for_metrics():
    from buddy_proxy.kiro.provider import _usage_payload
    from buddy_proxy.core.metrics_protocol import normalize_usage

    usage = _usage_payload(40, 0.0054)
    assert usage["credit"] == 0.0054
    norm = normalize_usage(usage)
    assert norm["credit"] == 0.0054, "metrics 必须能从 usage 里读到 credit"
    assert norm["completion_tokens"] > 0


def test_usage_payload_anthropic_shape():
    from buddy_proxy.kiro.provider import _usage_payload
    from buddy_proxy.core.metrics_protocol import normalize_usage

    usage = _usage_payload(40, 0.0054, anthropic=True)
    assert usage["input_tokens"] == 0
    assert normalize_usage(usage)["credit"] == 0.0054


def test_usage_payload_without_credit_is_none():
    """上游没给 meteringEvent 时不能伪造 0——0 会被当成「真的没花钱」。"""
    from buddy_proxy.kiro.provider import _usage_payload
    from buddy_proxy.core.metrics_protocol import normalize_usage

    usage = _usage_payload(40, None)
    assert "credit" not in usage
    assert normalize_usage(usage)["credit"] is None


def test_openai_stream_emits_usage_chunk_with_credit():
    """流式末尾必须有 usage chunk，否则日志的积分列永远是空的。

    测的是 provider 实际用的 ``_stream_openai``（``stream.to_openai_events``
    是早期同步版，已不在转发链路上；留它是为了单测解析，别拿它当实现）。
    """
    import asyncio
    from buddy_proxy.kiro.provider import KiroProvider

    raw = [make_frame("assistantResponseEvent", {"content": "Hi there"}),
           make_frame("meteringEvent", {"unit": "credit", "usage": 0.007})]

    async def source():
        for chunk in raw:
            yield chunk

    async def collect():
        provider = KiroProvider()
        out = []
        async for line in provider._stream_openai(
                source(), "m", "r", 0):
            out.append(line)
        return out

    out = asyncio.run(collect())
    usage_lines = [json.loads(l[len("data: "):]) for l in out
                   if l.startswith("data: {") and '"usage"' in l]
    assert usage_lines, "缺少 usage chunk"
    assert usage_lines[0]["usage"]["credit"] == 0.007
    assert usage_lines[0]["choices"] == [], "OpenAI 规范：usage chunk 的 choices 为空"


def test_kiro_rates_registered_for_estimation():
    """拿不到实扣时（异常路径）按倍率粗估，不能整列为空。"""
    from buddy_proxy.core.credit_estimate import _multiplier, register_kiro_rates

    register_kiro_rates({"claude-haiku-4.5": 0.4})
    assert _multiplier("kiro", "claude-haiku-4.5") == 0.4
    register_kiro_rates({})  # 清空，避免污染其它用例


# ---------------------------------------------------------------------------
# 对外 id 用短横线（与 yuntoken / Anthropic 官方命名一致）
# ---------------------------------------------------------------------------
#
# 用户 2026-11-10 要求：kiro 也统一成 `claude-haiku-5-5` 这种写法（yuntoken 与
# Anthropic 官方都是这么写的）。上游 kiro 自己写点号（`claude-haiku-5.5`），
# 所以对外 id 与转发用的上游名不是同一串，必须双向映射：
#   - 拉目录：点号 → 短横线，原串存 upstream_key
#   - 转发：还原成上游串（也接受点号写法当别名）

def test_public_id_rewrites_claude_dots():
    from buddy_proxy.kiro.provider import _public_model_id

    assert _public_model_id("claude-opus-5.5") == "claude-opus-5-5"
    assert _public_model_id("claude-haiku-4.5") == "claude-haiku-4-5"
    assert _public_model_id("claude-fable-5.1") == "claude-fable-5-1"


def test_public_id_leaves_other_vendors_alone():
    """非 claude 系保持点号——它们的厂商命名本来就用点。"""
    from buddy_proxy.kiro.provider import _public_model_id

    assert _public_model_id("gpt-5.6-sol") == "gpt-5.6-sol"
    assert _public_model_id("minimax-m2.5") == "minimax-m2.5"
    assert _public_model_id("deepseek-3.2") == "deepseek-3.2"
    assert _public_model_id("kimi-k3") == "kimi-k3"


def test_public_id_idempotent():
    """已经是短横线的名字不再动（避免 refresh 两次变成怪名）。"""
    from buddy_proxy.kiro.provider import _public_model_id

    assert _public_model_id("claude-opus-5-5") == "claude-opus-5-5"


def test_resolve_upstream_model_accepts_both_forms():
    """转发时三种写法都要还原成上游真名。"""
    from buddy_proxy.kiro.provider import _resolve_upstream_model

    id_map = {"claude-opus-5-5": "claude-opus-5.5"}
    assert _resolve_upstream_model("claude-opus-5-5", id_map) == "claude-opus-5.5"
    # 点号写法本身就是上游名
    assert _resolve_upstream_model("claude-opus-5.5", id_map) == "claude-opus-5.5"
    # 带通道前缀
    assert _resolve_upstream_model("kiro/claude-opus-5-5", id_map) == "claude-opus-5.5"


def test_resolve_upstream_model_passes_unknown_through():
    """认不出就原样透传——让上游报错，别猜一个名字。"""
    from buddy_proxy.kiro.provider import _resolve_upstream_model

    assert _resolve_upstream_model("some-new-model", {}) == "some-new-model"


def test_chat_to_kiro_uses_upstream_name():
    """核心断言：对外 id 进、上游 id 出。"""
    from buddy_proxy.kiro.provider import _chat_to_kiro

    body = {"model": "claude-opus-5-5",
            "messages": [{"role": "user", "content": "hi"}]}
    out = _chat_to_kiro(body, {"claude-opus-5-5": "claude-opus-5.5"})
    assert out["conversationState"]["currentMessage"]["userInputMessage"]["modelId"] \
        == "claude-opus-5.5"


# ---------------------------------------------------------------------------
# 账号显示名：取上游邮箱（用户 2026-11-10 要求）
# ---------------------------------------------------------------------------

def test_usage_url_requests_email():
    """``isEmailRequired=true`` 才有 ``userInfo.email``。

    ``false`` 时那个字段恒 null，账号列表里只剩 ``c-xxxxxxxx`` 这种 id，
    用户看不出是哪个号。这是实测出来的差异，不是猜的。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "buddy_proxy" / "kiro"
           / "provider.py").read_text(encoding="utf-8")
    assert "isEmailRequired=true" in src
    assert "isEmailRequired=false" not in src


def test_set_label_only_overwrites_auto_generated():
    """程序写名字时不能冲掉用户手动设的。"""
    import re as _re

    # 自动生成的形态：空串 / 等于 id / c-xxx / p-xxx
    def auto_like(current, aid):
        return (not current) or current == aid \
            or _re.fullmatch(r"[cp]-[a-z0-9]{8,}", current) is not None

    assert auto_like("", "c-abc12345678") is True
    assert auto_like("c-abc12345678", "c-abc12345678") is True
    assert auto_like("p-kgvmamamggdk", "x") is True
    # 用户手动改过的名字不受影响
    assert auto_like("主号", "x") is False
    assert auto_like("addbackteam9@addbacktech.com", "x") is False


# ---------------------------------------------------------------------------
# token 反推：从 contextUsagePercentage 估算 input
# ---------------------------------------------------------------------------
#
# kiro 的 EventStream 只给两样：``contextUsagePercentage``（整段对话占窗口的
# 百分比）与 ``credit``（真实计费）。**没有 inputTokens / outputTokens / 缓存
# 命中数**，响应头里也没有任何缓存字段（逐项查过）。
#
# 但 ctx% 可以反推 input：实测输入行数 10/100/300/600 对应 2.227%/3.220%/
# 5.416%/8.697%，严格线性。反推时要**扣掉 Kiro 自己注入的系统提示**——
# 四点标定出来是 4214~4243 token，波动 <1%，所以取 4233 当常量。

def test_estimate_tokens_subtracts_system_overhead():
    """不扣系统开销会把一句 16 字符的提问算成 4457 token（高估约 4200）。"""
    from buddy_proxy.kiro.provider import _estimate_tokens, _KIRO_SYSTEM_OVERHEAD_TOKENS

    window = 200_000
    # ctx% 刚好对应「系统开销 + 1000 token 输入」
    pct = (_KIRO_SYSTEM_OVERHEAD_TOKENS + 1000) / window * 100
    inp, _out, src = _estimate_tokens(pct, window, input_chars=3000, output_chars=100)
    assert src == "context"
    assert 900 <= inp <= 1100, f"应约 1000，实际 {inp}"


def test_estimate_tokens_falls_back_to_chars():
    """拿不到 ctx% 时按字符估（4 字符/token），并如实标记来源。"""
    from buddy_proxy.kiro.provider import _estimate_tokens

    inp, out, src = _estimate_tokens(None, 200_000, input_chars=400, output_chars=80)
    assert src == "chars"
    assert inp == 100 and out == 20


def test_estimate_tokens_never_negative():
    """ctx% 极小时扣完可能为负——必须兜住，不能吐负 token。"""
    from buddy_proxy.kiro.provider import _estimate_tokens

    inp, _out, src = _estimate_tokens(0.01, 200_000, input_chars=100, output_chars=10)
    assert inp >= 1, f"token 不能为负/零，实际 {inp}"


def test_usage_payload_has_input_and_no_fake_cache():
    """input 要有数；**缓存字段不能编**——上游不提供就别填。"""
    from buddy_proxy.kiro.provider import _usage_payload

    usage = _usage_payload(400, 0.01, ctx_percent=5.0, input_chars=1600, window=200_000)
    assert usage["prompt_tokens"] > 0, "input 应该有数了"
    assert usage["completion_tokens"] == 100
    assert "cached_tokens" not in usage, "上游不给缓存数，填 0 会被误读成「查了没命中」"


def test_system_overhead_constant_is_documented():
    """标定值别被人随手改：改之前必须重跑四点标定。"""
    from buddy_proxy.kiro.provider import _KIRO_SYSTEM_OVERHEAD_TOKENS

    assert 3_500 <= _KIRO_SYSTEM_OVERHEAD_TOKENS <= 5_000, \
        "标定值在 4214~4243 之间；超范围说明窗口假设变了，要重新标定"


# ---------------------------------------------------------------------------
# 多轮对话：history 必须真的发给上游
# ---------------------------------------------------------------------------
#
# 踩过的坑（很严重）：``_messages_to_conversation`` 一开始只返回
# ``(content, model_id)``，把算好的 turns 直接丢掉，于是**每次请求都只有最后
# 一条 user 消息**。表现是「在 dsh TUI 里聊了两句，第三句就忘了前面说过什么」，
# 换别的通道却正常——极易误判成上游或客户端问题。
#
# 上游的 history 形状（实测可用，问「我叫什么」能正确答出「小明」）：
#   {"userInputMessage": {"content":..., "modelId":..., "origin": "AI_EDITOR"}}
#   {"assistantResponseMessage": {"content": ...}}

def test_history_is_included_in_payload():
    """核心回归：多轮消息必须出现在 payload 的 history 里。"""
    from buddy_proxy.kiro.provider import _chat_to_kiro

    body = {"model": "claude-haiku-4-5", "messages": [
        {"role": "user", "content": "我叫小明"},
        {"role": "assistant", "content": "好的，记住了"},
        {"role": "user", "content": "我叫什么？"},
    ]}
    out = _chat_to_kiro(body, {"claude-haiku-4-5": "claude-haiku-4.5"})
    state = out["conversationState"]
    assert "history" in state, "缺 history = 模型失忆"
    assert len(state["history"]) == 2
    assert state["history"][0]["userInputMessage"]["content"] == "我叫小明"
    assert state["history"][1]["assistantResponseMessage"]["content"] == "好的，记住了"
    # currentMessage 只是最后那条
    assert state["currentMessage"]["userInputMessage"]["content"] == "我叫什么？"


def test_history_entries_carry_model_and_origin():
    """history 里的 user 条目也要带 modelId/origin，否则上游可能拒。"""
    from buddy_proxy.kiro.provider import _chat_to_kiro

    body = {"model": "m", "messages": [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]}
    hist = _chat_to_kiro(body, {}).get("conversationState", {}).get("history") or []
    assert hist and hist[0]["userInputMessage"]["modelId"] == "m"
    assert hist[0]["userInputMessage"]["origin"] == "AI_EDITOR"


def test_no_history_key_when_single_turn():
    """单轮不带空 history：上游用不着，少发字段更干净。"""
    from buddy_proxy.kiro.provider import _chat_to_kiro

    out = _chat_to_kiro({"model": "m", "messages": [
        {"role": "user", "content": "只问一句"}]}, {})
    assert "history" not in out["conversationState"]


def test_empty_history_entries_skipped():
    """空内容的轮次不发——没信息量，上游对空串也不友好。"""
    from buddy_proxy.kiro.provider import _chat_to_kiro

    body = {"model": "m", "messages": [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "q2"},
    ]}
    hist = _chat_to_kiro(body, {}).get("conversationState", {}).get("history") or []
    assert all(
        (h.get("assistantResponseMessage") or {}).get("content")
        or (h.get("userInputMessage") or {}).get("content")
        for h in hist
    ), f"出现了空条目: {hist}"


def test_messages_to_conversation_returns_history():
    """函数签名必须是三元组（改回两元组就会重演失忆 bug）。"""
    from buddy_proxy.kiro.provider import _messages_to_conversation

    out = _messages_to_conversation({"model": "m", "messages": [
        {"role": "user", "content": "a"}, {"role": "user", "content": "b"}]})
    assert len(out) == 3, "必须返回 (content, model_id, history)"


# ---------------------------------------------------------------------------
# 工具调用（agent 类客户端必需）
# ---------------------------------------------------------------------------
#
# 用户实测：dsh TUI 接 kiro 后「跑一次就停」。日志给出根因——
# `kiro 暂不支持工具调用（请求带 28 个 tools），本次按纯文本转发`。
# 模型看不到工具就只回文本，agent 拿不到 tool_call，一轮就结束。
#
# 上游实测确认的格式（三处都踩过）：
#   1. tools 放每一条 userInputMessage.userInputMessageContext，不是顶层 toolConfig
#   2. toolUseEvent 是多帧增量：name/id 首帧、input 碎片中间、stop 末帧
#   3. tool 消息的 result 不能被后续 user 合并掉，否则 TOOL_USE_RESULT_MISMATCH

def test_tools_sent_to_upstream():
    """tools 必须真的下发——之前是静默丢弃，导致 agent 跑一次就停。"""
    from buddy_proxy.kiro.provider import _chat_to_kiro

    body = {"model": "m", "messages": [{"role": "user", "content": "查天气"}],
            "tools": [{"type": "function", "function": {
                "name": "get_weather", "description": "查天气",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            }}]}
    out = _chat_to_kiro(body, {})
    ctx = out["conversationState"]["currentMessage"]["userInputMessage"]["userInputMessageContext"]
    specs = ctx["tools"]
    assert len(specs) == 1
    assert specs[0]["toolSpecification"]["name"] == "get_weather"
    # inputSchema 外面必须套 json（Bedrock union 标签）
    assert "json" in specs[0]["toolSpecification"]["inputSchema"]


def test_max_tokens_does_not_clobber_tools():
    """回归：给 context 整体赋值 maxTokens 会抹掉 tools（踩过）。"""
    from buddy_proxy.kiro.provider import _chat_to_kiro

    body = {"model": "m", "max_tokens": 100,
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {"name": "f"}}]}
    ctx = _chat_to_kiro(body, {})["conversationState"]["currentMessage"][
        "userInputMessage"]["userInputMessageContext"]
    assert "tools" in ctx and ctx.get("maxTokens") == 100


def test_tool_role_becomes_tool_results_and_is_not_merged():
    """tool 消息要独立成一个 user 轮次，不能被后续 user 合并掉。

    合并了的话它会被 pop 成 currentMessage，tool_result 丢失 → 上游报
    TOOL_USE_RESULT_MISMATCH。
    """
    from buddy_proxy.kiro.provider import _chat_to_kiro

    body = {"model": "m", "messages": [
        {"role": "user", "content": "查天气"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "get_weather", "arguments": "{\"city\":\"北京\"}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "晴 25度"},
        {"role": "user", "content": "所以呢？"},
    ]}
    hist = _chat_to_kiro(body, {})["conversationState"]["history"]
    kinds = [list(h)[0] for h in hist]
    assert kinds == ["userInputMessage", "assistantResponseMessage", "userInputMessage"]
    # 第三条是 tool_result 容器
    results = hist[2]["userInputMessage"]["userInputMessageContext"]["toolResults"]
    assert results[0]["toolUseId"] == "t1"
    assert results[0]["status"] == "success"


def test_history_model_id_uses_upstream_name():
    """history 里的 modelId 也要剥掉 kiro/ 前缀（构造早于解析，踩过）。"""
    from buddy_proxy.kiro.provider import _chat_to_kiro

    body = {"model": "kiro/claude-opus-5-5", "messages": [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]}
    hist = _chat_to_kiro(body, {"claude-opus-5-5": "claude-opus-5.5"})[
        "conversationState"]["history"]
    assert hist[0]["userInputMessage"]["modelId"] == "claude-opus-5.5"


def test_tool_use_event_parsing():
    """toolUseEvent 是多帧增量，解析器要如实吐出碎片。"""
    from buddy_proxy.kiro.stream import _tool_use_event

    assert _tool_use_event({"name": "f", "toolUseId": "t1"}) == {
        "type": "tool", "tool_id": "t1", "name": "f"}
    assert _tool_use_event({"input": "{\"a\"", "toolUseId": "t1"}) == {
        "type": "tool", "tool_id": "t1", "input": "{\"a\""}
    assert _tool_use_event({"stop": True, "toolUseId": "t1"}) == {
        "type": "tool", "tool_id": "t1", "stop": True}
    # 缺 toolUseId 的帧丢弃
    assert _tool_use_event({"name": "f"}) is None


def test_openai_stream_emits_tool_calls():
    """流式必须产出 tool_calls + finish_reason=tool_calls。"""
    import asyncio
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "buddy_proxy" / "kiro"
           / "provider.py").read_text(encoding="utf-8")
    assert '"tool_calls"' in src and 'finish = "tool_calls"' in src
    assert 'stop_reason": "tool_use"' in src or '"tool_use"' in src


# ---------------------------------------------------------------------------
# tool_use / tool_result 配对修复
# ---------------------------------------------------------------------------
#
# 真实事故（用户 dsh 会话反复中断，2026-11-11）：
#
#     messages.710: `tool_use` ids were found without `tool_result` blocks
#     immediately after: toolu_bdrk_01YFVaiNmbRBq7kv5yZde3r8
#     reason: TOOL_USE_RESULT_MISMATCH
#
# 上游的约束是硬性的：每个 toolUses 后面**必须**紧跟配对的 toolResults，
# 一个都不能缺，否则整条请求 400、整轮对话中断。
#
# 而客户端历史不保证满足——用户可能工具跑完就不再说话、也可能点「跳过」。

def _pairing_problems(msgs):
    """把 chat messages 转成 payload，检查每个 toolUses 是否都有配对结果。"""
    from buddy_proxy.kiro.provider import _chat_to_kiro

    out = _chat_to_kiro({"model": "m", "messages": msgs}, {})
    hist = out["conversationState"]["history"]
    cur = out["conversationState"]["currentMessage"]["userInputMessage"]
    problems = []
    for i, e in enumerate(hist):
        uses = (e.get("assistantResponseMessage") or {}).get("toolUses") or []
        if not uses:
            continue
        nxt = hist[i + 1] if i + 1 < len(hist) else None
        if nxt and "userInputMessage" in nxt:
            ctx = nxt["userInputMessage"].get("userInputMessageContext") or {}
        else:
            ctx = cur.get("userInputMessageContext") or {}
        have = {r.get("toolUseId") for r in (ctx.get("toolResults") or [])}
        want = {u.get("toolUseId") for u in uses}
        if want - have:
            problems.append((i, want - have))
    return problems


def _t(i):
    return {"id": i, "type": "function",
            "function": {"name": "f", "arguments": "{}"}}


def _r(i):
    return {"role": "tool", "tool_call_id": i, "content": "ok"}


def test_pairing_ok_for_normal_single_tool():
    msgs = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [_t("t1")]},
            _r("t1"), {"role": "user", "content": "next"}]
    assert _pairing_problems(msgs) == []


def test_pairing_ok_for_multiple_tools():
    msgs = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [_t("t1"), _t("t2")]},
            _r("t1"), _r("t2"), {"role": "user", "content": "next"}]
    assert _pairing_problems(msgs) == []


def test_pairing_fixed_when_conversation_ends_right_after_tool():
    """工具跑完用户就没再说话——这是最容易踩的一种（对话自然结束）。"""
    msgs = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [_t("t1")]},
            _r("t1")]
    assert _pairing_problems(msgs) == [], "末尾工具调用必须补上结果"


def test_pairing_fixed_when_result_missing_entirely():
    """客户端点「跳过」——根本没有 tool 消息。"""
    msgs = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [_t("t1")]},
            {"role": "user", "content": "next"}]
    assert _pairing_problems(msgs) == [], "缺失的结果要补占位"


def test_pairing_fixed_when_result_comes_after_next_user():
    """客户端把 result 写在后续 user 之后（顺序错乱）。"""
    msgs = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [_t("t1")]},
            {"role": "user", "content": "跳过工具"},
            _r("t1")]
    assert _pairing_problems(msgs) == []


def test_pairing_fixed_for_consecutive_tool_rounds():
    msgs = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [_t("t1")]},
            _r("t1"),
            {"role": "assistant", "content": None, "tool_calls": [_t("t2")]},
            _r("t2"), {"role": "user", "content": "next"}]
    assert _pairing_problems(msgs) == []


def test_missing_result_placeholder_is_marked_error():
    """补的占位要如实标 error，不能伪装成成功。

    模型据 status 判断工具是否产出过数据；标 success 会让它以为拿到了
    结果，进而基于不存在的数据继续推理。
    """
    from buddy_proxy.kiro.provider import MISSING_TOOL_RESULT_TEXT, _fix_tool_pairing

    hist = _fix_tool_pairing([
        {"assistantResponseMessage": {"content": "", "toolUses": [
            {"toolUseId": "tX", "name": "f", "input": {}}]}},
    ])
    assert len(hist) == 2
    ctx = hist[1]["userInputMessage"]["userInputMessageContext"]
    result = ctx["toolResults"][0]
    assert result["toolUseId"] == "tX"
    assert result["status"] == "error"
    assert MISSING_TOOL_RESULT_TEXT in result["content"][0]["text"]
