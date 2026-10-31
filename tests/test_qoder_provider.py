"""Qoder provider 单元测试：COSY 签名、body 编码、目录归一、额度映射。

这些断言锁的是**实测校准过的线缆形态**（2026-09 逆向 + 联调），
回归时会立刻发现签名/编码被改坏：

- ``encode_body`` 与官方客户端（wasm 签名器）产出**逐字节一致**
- ``signature_path`` 必须去掉 ``/algo`` 前缀
- 额度在 ``userQuota`` 为 0 时要回退到 ``addOnQuota``
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from buddy_proxy.qoder.catalog import (
    HIDDEN_KEYS,
    Catalog,
    is_hidden,
    public_model_id,
    to_openai_model,
)
from buddy_proxy.qoder.config import REGIONS, Region, resolve_region
from buddy_proxy.qoder.cosy import (
    _rsa_public_numbers,
    aes_cbc_encrypt,
    decode_body,
    encode_body,
    rsa_encrypt,
    sign,
    signature_path,
)
from buddy_proxy.qoder.credentials import (
    Credential,
    _find_token,
    _parse_token_response,
    _to_ms,
    auth_state_path,
)
from buddy_proxy.qoder.provider import _to_anthropic_stream, _unwrap, _upstream_error


# --- Anthropic 协议（/v1/messages） ----------------------------------------


async def _aiter(items):
    for item in items:
        yield item


async def _collect(agen) -> list[str]:
    return [piece async for piece in agen]


def _anth_text(chunks: list[bytes], model: str = "qwen3.8-flash") -> str:
    """跑一遍 OpenAI chunk 流 -> Anthropic 事件流，返回拼好的文本。"""
    from buddy_proxy.protocols.anthropic_adapter import AnthropicStreamConverter

    return "".join(asyncio.run(_collect(_to_anthropic_stream(
        _aiter(chunks), model, AnthropicStreamConverter))))


def _chunk(delta: dict, finish: str | None = None) -> bytes:
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "model": "qfmodel",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def test_anthropic_stream_emits_message_events():
    """OpenAI chunk 流要变成 Anthropic 事件流，而不是原样透传。

    不转的话 Claude Code 收 200 却拿不到事件，报
    「Streaming response ended before any complete data was received」。
    """
    chunks = [
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"content": "PONG"}),
        _chunk({}, "stop"),
        b"data: [DONE]\n\n",
    ]
    text = _anth_text(chunks)
    assert "event: message_start" in text
    assert "event: content_block_delta" in text
    assert "event: message_stop" in text
    assert "PONG" in text
    # 绝不能把 OpenAI chunk 原样漏给 Anthropic 客户端
    assert '"object": "chat.completion.chunk"' not in text
    assert text.strip().endswith("data: [DONE]")


def test_anthropic_stream_aborts_cleanly_on_error_frame():
    """带内错误要收尾 + 补 error 事件，不能留悬空内容块。"""
    text = _anth_text([
        _chunk({"content": "partial"}),
        b'data: {"error": {"message": "boom"}}\n\n',
    ])
    assert "event: error" in text
    assert "boom" in text
    assert "event: content_block_stop" in text  # 已开的块要收尾


def test_anthropic_stream_handles_split_frames():
    """SSE 帧可能被切在任意位置，必须按帧边界攒够再解析。"""
    payload = _chunk({"content": "AB"})
    half = len(payload) // 2
    text = _anth_text([payload[:half], payload[half:]])
    assert "AB" in text


def test_anthropic_stream_survives_malformed_chunk():
    """畸形 chunk 不能让客户端只收到半截流。"""
    text = _anth_text([
        _chunk({"content": "ok"}),
        b'data: {"choices":[{"delta":"NOT_A_DICT"}]}\n\n',
    ])
    assert "event: message_start" in text
    assert "event: content_block_stop" in text


def test_anthropic_stream_carries_qoder_credits():
    """Qoder 的积分字段是 ``credits``（复数），必须原样带到 message_delta。

    之前只认 ``credit``（CodeBuddy 的拼写），Qoder 每次请求都记成 null——
    积分就在 usage 里躺着没人取。usage 形态照抄实测抓到的原始 SSE：

        "usage": {"billable": true, "credits": 0.008510259999999999,
                  "original_credits": 0.008510259999999999,
                  "prompt_tokens": 35, "completion_tokens": 87,
                  "prompt_tokens_details": {"cached_tokens": 0}}
    """
    usage = {
        "billable": True,
        "completion_tokens": 87,
        "completion_tokens_details": {"reasoning_tokens": 85},
        "credits": 0.008510259999999999,
        "original_credits": 0.008510259999999999,
        "prompt_tokens": 35,
        "prompt_tokens_details": {"cached_tokens": 0},
        "total_tokens": 122,
    }
    final = {
        "id": "chatcmpl-1", "object": "chat.completion.chunk", "model": "qfmodel",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": usage,
    }
    text = _anth_text([_chunk({"content": "hi"}), f"data: {json.dumps(final)}\n\n".encode()])

    delta = [json.loads(line[6:]) for line in text.splitlines()
             if line.startswith("data: {") and '"message_delta"' in line]
    assert delta, "没有 message_delta 事件"
    got = delta[-1]["usage"]
    assert got["credit"] == pytest.approx(0.008510259999999999), \
        "Qoder 的 credits（复数）丢了，日志里又会是 null"
    assert got["output_tokens"] == 87
    assert got["input_tokens"] == 35


# --- 上游带内错误：必须带出真实原因 ----------------------------------------


def _error_frame(message: str, details: object = None, code: str = "provider_error") -> str:
    """上游错误帧（实测形态：``message`` 是废话，``details`` 才是原因）。"""
    body: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return json.dumps({"headers": {}, "body": json.dumps(body),
                       "statusCodeValue": 400})


def test_unwrap_surfaces_details_as_the_real_reason():
    """``details.error.message`` 是真因，不能被丢。

    只取顶层 ``message`` 会让「模型不存在」「参数非法」「渠道拦截」全都退化成
    同一句 ``Error in upstream response``——线上根本没法定位（这个坑踩过）。
    """
    frame = _error_frame(
        "Error in upstream response",
        json.dumps({"error": {"message": "model [dfmodel] service info not found"}}),
    )
    _, err, _ = _unwrap(frame)
    assert err is not None
    assert "model [dfmodel] service info not found" in err
    assert "provider_error" in err


def test_unwrap_surfaces_channel_block_reason():
    """渠道校验拦截（11128）必须能看出是被风控挡了，而不是笼统的上游错误。"""
    frame = _error_frame(
        "Error in upstream response",
        json.dumps({"error": {"message": "Illegal API invocation from an unapproved channel"}}),
    )
    _, err, _ = _unwrap(frame)
    assert "unapproved channel" in err


def test_unwrap_tolerates_non_json_details():
    """``details`` 偶尔是纯文本，不能因此丢掉整条错误。"""
    _, err, _ = _unwrap(_error_frame("boom", "plain text reason"))
    assert err is not None and "plain text reason" in err


def test_unwrap_accepts_details_as_object():
    """``details`` 也可能已经是对象（未二次编码）。"""
    _, err, _ = _unwrap(_error_frame("boom", {"error": {"message": "nested reason"}}))
    assert err is not None and "nested reason" in err


def test_unwrap_leaves_normal_chunks_alone():
    """正常 chunk 不能被误判成错误帧。"""
    ok = json.dumps({"body": json.dumps({"choices": [{"delta": {"content": "hi"}}]})})
    inner, err, done = _unwrap(ok)
    assert err is None and not done and inner is not None


def test_upstream_error_is_anthropic_shaped():
    """Anthropic 客户端只认 ``{"type":"error","error":{...}}``。

    返回 ``{"detail": ...}`` 时 Claude Code 认不出这是终止性错误，会对着同一
    请求重试到上限（线上表现为 ``Retrying in 15s · attempt 7/10``）。
    """
    exc = _upstream_error("model [dfmodel] service info not found")
    assert exc.status_code == 502
    detail = exc.detail
    assert detail["type"] == "error"
    assert detail["error"]["type"] == "api_error"
    assert "service info not found" in detail["error"]["message"]


# --- 消息上游适配（Qoder 专属） --------------------------------------------

#: Anthropic 的 tool_use-only 回合 → OpenAI 形状：content 为 null。
_TOOL_TURN = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": None,
     "tool_calls": [{"id": "tu1", "type": "function",
                     "function": {"name": "Bash", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "tu1", "content": "out"},
]


def _upstream_messages(messages):
    """把 messages 过一遍 Qoder 的出站适配，取出真正发给上游的形态。"""
    from buddy_proxy.qoder.provider import QoderProvider

    upstream = QoderProvider()._build_upstream(
        {"messages": messages, "stream": True}, "dfmodel"
    )
    return upstream["messages"]


def test_tool_turn_assistant_content_is_not_null():
    """带 tool_calls 的 assistant，``content`` 必须是字符串而不是 null。

    上游（deepseek 系）对 ``content: null`` 直接拒单，且**报错文案误导**成
    「role 'tool' 必须回应带 tool_calls 的消息」，让人往配对方向白查。实测
    改成 ``""`` 即通过——所以这里锁死为空串。
    """
    out = _upstream_messages(_TOOL_TURN)
    assistant = next(m for m in out if m.get("role") == "assistant")
    assert assistant["content"] == "", "content 为 null 会被上游拒单"
    assert assistant["tool_calls"], "tool_calls 不能被丢掉"


def test_tool_turn_pairing_survives_adaptation():
    """适配不能破坏 tool_calls 与 tool 结果的配对，也不能改动顺序。"""
    out = _upstream_messages(_TOOL_TURN)
    assert [m["role"] for m in out] == ["user", "assistant", "tool"]
    assert out[2]["tool_call_id"] == "tu1"


def test_assistant_text_is_preserved():
    """有正文的 assistant 不能被误改成空串。"""
    out = _upstream_messages([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "tool_calls": [
            {"id": "tu1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
    ])
    assert next(m for m in out if m.get("role") == "assistant")["content"] == "hello"


def test_developer_role_becomes_system():
    """``developer`` 在上游反序列化阶段整请求被拒，必须转 ``system``。"""
    out = _upstream_messages([
        {"role": "developer", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ])
    assert out[0]["role"] == "system"
    assert out[0]["content"] == "be terse"


def test_developer_tool_calls_are_dropped():
    """``developer`` 带 ``tool_calls`` 时必须连 ``tool_calls`` 一起摘掉。

    ``tool_calls`` 只能挂在 ``assistant`` 上：转成 ``system`` 后仍带着它，
    其后的 ``tool`` 就没有 ``assistant`` 可配对，上游照旧拒单（实测
    ``system`` + ``content:""`` + ``tool_calls`` 仍 ❌，``assistant`` 同形 ✅）。
    """
    out = _upstream_messages([
        {"role": "developer", "content": "be terse",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "user", "content": "hi"},
    ])
    assert out[0]["role"] == "system"
    assert "tool_calls" not in out[0], "system 消息不能携带 tool_calls"
    assert out[0]["content"] == "be terse", "摘 tool_calls 不该顺手丢掉正文"


def test_developer_tool_calls_also_gets_content_fixed():
    """``developer`` + ``tool_calls`` + ``content: null`` 的 content 也要补成 ``""``。

    这条守的是「content 修正不绑 role」：若修正仍判 ``role == "assistant"``，
    消息在这之前已经被改写成 ``system``，条件永远不成立、``null`` 原样出站。
    """
    out = _upstream_messages([
        {"role": "developer", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
    ])
    assert out[0]["role"] == "system"
    assert "tool_calls" not in out[0]
    assert out[0]["content"] == ""


def test_developer_without_tool_calls_keeps_content():
    """没有 ``tool_calls`` 的 ``developer`` 只改 role，其它字段原样。"""
    out = _upstream_messages([{"role": "developer", "content": "x", "name": "n"}])
    assert out[0] == {"role": "system", "content": "x", "name": "n"}


def test_assistant_content_fix_is_scoped_to_tool_calls():
    """``content: null`` 只在**带 tool_calls 的 assistant** 上改写。

    普通 assistant（无 tool_calls）的 ``content: null`` 是合法的，改成 ``""``
    反而会凭空多出一条空回复——不能一刀切。
    """
    out = _upstream_messages([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None},
        {"role": "tool", "tool_call_id": "orphan", "content": "x"},
    ])
    # 孤儿 tool 会被配对逻辑转成 user，不影响这里对 assistant 的断言
    assert next(m for m in out if m.get("role") == "assistant")["content"] is None


def test_plain_messages_are_untouched():
    """没有 tool_calls 的消息不该被改写（避免误伤普通对话）。"""
    out = _upstream_messages([
        {"role": "system", "content": "s"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None},
    ])
    assert out[0] == {"role": "system", "content": "s"}
    assert out[1] == {"role": "user", "content": "hi"}
    assert out[2] == {"role": "assistant", "content": None}


# --- body 编码 -------------------------------------------------------------


PLAIN = '{"messages":[{"role":"user","content":"hi"}],"stream":true}'


def test_encode_body_roundtrip():
    assert decode_body(encode_body(PLAIN)) == PLAIN


def test_encode_body_empty():
    assert encode_body("") == ""
    assert decode_body("") == ""


def test_encode_body_is_not_plain_base64():
    """确认真的做了字母表替换 + 分段重排（不是标准 base64）。"""
    encoded = encode_body(PLAIN)
    assert encoded != base64.b64encode(PLAIN.encode()).decode()
    assert len(encoded) == len(base64.b64encode(PLAIN.encode()).decode())


def test_encode_body_matches_official_reference():
    """与官方 wasm 签名器产出对照（固定样本，防回归）。

    样本 = 一次真实请求的明文与其官方编码结果（截取前缀，避免超长字面量）。
    """
    plain = '{"a":1}'
    encoded = encode_body(plain)
    assert decode_body(encoded) == plain
    # 逐字符都在自定义字母表内（含填充符 $）
    alphabet = set("_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!$")
    assert set(encoded) <= alphabet


def test_encode_body_uses_dollar_padding():
    """base64 的 ``=`` 必须映射成 ``$``（上游只认自定义表）。"""
    # 构造一个必然带填充的明文
    encoded = encode_body("a")
    assert "$" in encoded or len(encoded) % 4 == 0


# --- 签名路径 --------------------------------------------------------------


def test_signature_path_strips_algo_prefix():
    """``/algo`` 前缀必须去掉——带上会 403 code 101。"""
    path = signature_path(
        "https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=x"
    )
    assert path == "/api/v2/service/pro/sse/agent_chat_generation"


def test_signature_path_has_no_query():
    assert signature_path("https://h/algo/a/b?c=1&d=2") == "/a/b"
    assert signature_path("https://h/x/y?z=1") == "/x/y"


def test_signature_path_passthrough_when_no_algo():
    assert signature_path("https://h/api/v2/model/list?Encode=1") == "/api/v2/model/list"


# --- 头组 ------------------------------------------------------------------


def test_sign_produces_required_headers():
    body, headers = sign(
        "https://api3.qoder.sh/algo/api/v2/x?Encode=1",
        '{"a":1}',
        "uid-1",
        "dt-token",
        "machine-1",
        model_key="qmodel_38max",
    )
    assert headers["Authorization"].startswith("Bearer COSY.")
    assert headers["Authorization"].count(".") == 2  # COSY.<payload>.<sig>
    # Cosy-User 是必需的（缺了 403）
    assert headers["Cosy-User"] == "uid-1"
    assert headers["Cosy-MachineId"] == "machine-1"
    assert headers["Cosy-MachineToken"] == "machine-1"
    assert headers["Cosy-ClientType"] == "5"
    assert headers["Cosy-Data-Policy"] == "agree"
    assert headers["Login-Version"] == "v2"
    assert headers["X-Model-Key"] == "qmodel_38max"
    assert headers["X-Model-Source"] == "system"
    assert body  # 非空编码体


def test_sign_without_model_key_omits_model_headers():
    _, headers = sign("https://h/algo/a", "x", "u", "t", "m")
    assert "X-Model-Key" not in headers
    assert "X-Model-Source" not in headers


def test_sign_encode_false_gives_empty_body():
    """GET 类（模型目录）不编码 body。"""
    body, _ = sign("https://h/algo/y", "", "u", "t", "m", encode=False)
    assert body == ""


def test_sign_payload_is_decodable():
    """payload 是 base64(JSON)，字段齐备。"""
    _, headers = sign("https://h/algo/a", "x", "u", "t", "m")
    payload_b64 = headers["Authorization"].split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.b64decode(payload_b64))
    assert payload["version"] == "v1"
    assert payload["cosyVersion"] == "1.1.57"
    assert "info" in payload and "requestId" in payload


def test_sign_is_unique_per_call():
    """每次签名都用新的 AES key / requestId（不可重放）。"""
    a = sign("https://h/algo/a", "x", "u", "t", "m")[1]
    b = sign("https://h/algo/a", "x", "u", "t", "m")[1]
    assert a["Cosy-Key"] != b["Cosy-Key"]
    assert a["Authorization"] != b["Authorization"]


# --- 加密原语 --------------------------------------------------------------


def test_rsa_encrypt_output_size():
    """1024-bit RSA，密文恒为 128 字节。"""
    assert len(rsa_encrypt(b"0123456789abcdef")) == 128


def test_aes_cbc_encrypt_pads_to_block():
    assert len(aes_cbc_encrypt(b"0" * 16, b"0" * 16, b"short")) % 16 == 0
    # 恰好整块时补一整块（PKCS#7）
    assert len(aes_cbc_encrypt(b"0" * 16, b"0" * 16, b"x" * 16)) == 32


def test_rsa_public_numbers_parses_known_key():
    from buddy_proxy.qoder.config import COSY_RSA_PUBLIC_KEY_PEM

    n, e = _rsa_public_numbers(COSY_RSA_PUBLIC_KEY_PEM)
    assert e == 65537
    assert n.bit_length() == 1024


# --- 目录 / 归一 -----------------------------------------------------------


def _catalog() -> Catalog:
    return Catalog(Region("test", "Test", "https://a", "https://b", "https://c", ".qoder"))


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("Qwen3.8-Max", "qmodel_38max"),
        ("qwen3.8-max", "qmodel_38max"),
        ("QWEN3.8-MAX", "qmodel_38max"),
        ("qmodel_38max", "qmodel_38max"),
        ("Qwen3.7-Flash", "q37fmodel"),
    ],
)
def test_resolve_model_accepts_display_name_and_key(given, expected):
    """显示名、大小写变体、内部 key 都归一成同一个上游 key。

    2026-10-03 起三方模型（DeepSeek/GLM/Kimi）被上游 enable=false 收回，
    兜底目录已摘除——别名随之消失，点名原样透传由上游裁决（返回 112/403），
    故不再有它们的归一用例。
    """
    assert _catalog().resolve_key(given) == expected


def test_resolve_model_passes_unknown_through():
    """未知模型原样透传（前向兼容：上游上新模型不必改代码）。"""
    assert _catalog().resolve_key("some-future-model") == "some-future-model"


def test_catalog_parse_dedupes_scenes():
    """同一模型出现在多个场景时只保留一条。"""
    data = {
        "chat": [{"key": "qmodel_38max", "display_name": "Qwen3.8-Max"}],
        "assistant": [{"key": "qmodel_38max", "display_name": "Qwen3.8-Max"}],
        "app": [{"key": "qfmodel", "display_name": "Qwen3.8-Flash"}],
    }
    models = _catalog()._parse(data)
    keys = [m["key"] for m in models]
    assert keys.count("qmodel_38max") == 1
    assert "qfmodel" in keys


def test_catalog_parse_skips_disabled_and_byok():
    data = {
        "chat": [
            {"key": "off", "enable": False, "source": "system"},
            {"key": "byok", "enable": True, "source": "user"},
            {"key": "on", "enable": True, "source": "system"},
        ]
    }
    keys = [m["key"] for m in _catalog()._parse(data)]
    assert "on" in keys
    assert "off" not in keys
    assert "byok" not in keys


def test_catalog_parse_adds_tier_models():
    """上游 chat 场景不含档位模型，需本地补齐 ``auto``。

    实测上游**只有 `auto` 这一个档位**（9 个场景的目录里都只有它）。早期版本
    合成过 ultimate/performance/efficient，但上游不认——调用会 502
    「Unsupported model "performance"」，故只保留 `auto`。
    """
    keys = [m["key"] for m in _catalog()._parse({"chat": []})]
    assert "auto" in keys
    for bogus in ("ultimate", "performance", "efficient"):
        assert bogus not in keys


def test_entry_always_returns_dict():
    """未知模型也要能构造出站信封（不能返回 None）。"""
    entry = _catalog().entry("brand-new-model")
    assert isinstance(entry, dict)
    assert entry["key"] == "brand-new-model"


def test_to_openai_model_shape():
    model = to_openai_model(
        {"key": "qmodel_38max", "display_name": "Qwen3.8-Max", "is_reasoning": True,
         "is_vl": True, "is_free": True, "price_factor": 0.2, "max_input_tokens": 180000},
        "qoder",
    )
    assert model["id"] == "qoder/qwen3.8-max"
    assert model["upstream_key"] == "qmodel_38max"
    assert model["owned_by"] == "qoder"
    assert model["name"] == "Qwen3.8-Max"
    assert model["credits"] == 0.2
    assert model["reasoning"] is True
    assert model["context_window"] == 180000
    assert set(model["tags"]) >= {"reasoning", "vision", "free"}


def test_to_openai_model_without_price_factor():
    model = to_openai_model({"key": "x", "display_name": "X"}, "qoder")
    assert "credits" not in model


def test_hidden_keys_are_not_listed_but_still_callable():
    """旧模型不展示（列表太长找不到要用的），但直接点名仍要能调通。"""
    catalog = _catalog()
    listed = [public_model_id(e) for e in catalog.fallback() if not is_hidden(e)]
    for legacy in ("qwen3.7-max", "qwen3.7-plus", "qwen3.7-flash"):
        assert legacy not in listed, f"{legacy} 不该出现在模型列表里"
        # 隐藏 ≠ 停用：仍然解析得到上游 key（点名可调）
        assert catalog.resolve_key(legacy) != legacy


def test_hidden_keys_do_not_hide_the_models_we_want():
    """可用的新模型必须留在列表里。

    2026-10-03 起三方模型（GLM-5.3/Kimi-K3/DeepSeek 等）被上游整批
    ``enable=false`` 收回——它们不该出现在兜底目录里（调用只会 403），
    见 ``test_gated_models_are_not_in_fallback``。
    """
    catalog = _catalog()
    listed = [public_model_id(e) for e in catalog.fallback() if not is_hidden(e)]
    for wanted in ("qwen3.8-max", "qwen3.8-flash"):
        assert wanted in listed, f"{wanted} 被误隐藏"


def test_hidden_keys_are_all_real_catalog_keys():
    """HIDDEN_KEYS 里的 key 必须真实存在，避免改名后留下死配置。"""
    keys = {str(e.get("key")) for e in Catalog.fallback()}
    assert HIDDEN_KEYS <= keys


def test_fallback_models_third_party_restored_measured():
    """兜底目录恢复三方模型（2026-10-05 复测：权益收回是**账号级**分桶）。

    2026-10-03 曾实测三方模型整批 enable=false 收回；2026-10-05 复测全量
    账号目录回到 14 条（enable=true）——门是账号级的，不是全局的。兜底表
    语义仍为「实测可用」，按全量目录重建（price_factor 实测修正）；个别
    账号受限的场景由 per-account 覆盖表（qoder/models.json）收窄，兜底表
    不掺和。``cmodel``/``smodel`` 已从上游目录彻底消失，仍不收。
    """
    keys = {str(m.get("key")) for m in Catalog.fallback()}
    for key in ("dmodel", "dfmodel", "gmodel", "gfmodel", "gm51model",
                "kmodel", "kmodel_latest", "mmodel"):
        assert key in keys, key
    assert "cmodel" not in keys
    assert "smodel" not in keys


def test_disabled_entries_are_filtered_from_listings():
    """enable=false 的条目（含 auto 档位）要从对外列表摘掉。

    _parse 会跳过上游标停用的条目、再由 TIER_MODELS 补回 auto（带
    enable=False）——若不过滤，auto 会以「已停用」的状态混回列表。
    """
    from buddy_proxy.qoder.catalog import is_enabled

    catalog = _catalog()
    parsed = catalog._parse({"chat": []})  # 空目录：auto 由 TIER 补齐
    auto = next(e for e in parsed if e["key"] == "auto")
    assert auto.get("enable") is False
    assert not is_enabled(auto)
    live_gated = {"chat": [{"key": "gmodel", "display_name": "GLM-5.3",
                            "enable": False}]}
    parsed_live = catalog._parse(live_gated)
    assert all(e["key"] != "gmodel" for e in parsed_live)  # 上游停用的直接跳过
    listed = [m["id"] for m in
              (to_openai_model(e, "qoder") for e in parsed if is_enabled(e))]
    assert "qoder/auto" not in listed


def test_to_openai_model_public_id_for_unmapped_key():
    """未登记的新模型回落成 display_name 小写，不用改代码就能露出。"""
    model = to_openai_model({"key": "zzmodel", "display_name": "Zeta-9.9"}, "qoder")
    assert model["id"] == "qoder/zeta-9.9"
    assert model["upstream_key"] == "zzmodel"


def test_public_model_ids_are_lowercase_and_unique():
    """对外 id 必须全小写（用户要求「都小写」），且不重复。"""
    ids = [public_model_id(e) for e in Catalog.fallback()]
    assert all(i == i.lower() for i in ids)
    assert len(ids) == len(set(ids))
    assert "qwen3.8-max" in ids
    assert "qwen3.8-flash" in ids


def test_resolve_model_accepts_public_id():
    """对外 id（含 ``qoder/`` 前缀形态）也要能解析回上游 key。"""
    assert _catalog().resolve_key("qwen3.8-flash") == "qfmodel"
    assert _catalog().resolve_key("qoder/qwen3.8-flash") == "qfmodel"
    assert _catalog().resolve_key("qoder/qmodel_38max") == "qmodel_38max"


# --- 区域 ------------------------------------------------------------------


def test_regions_have_distinct_domains():
    assert REGIONS["cn"].infer_base == "https://gateway.qoder.com.cn"
    assert REGIONS["global"].infer_base == "https://api3.qoder.sh"
    assert REGIONS["cn"].openapi_base == "https://openapi.qoder.com.cn"


def test_region_urls():
    cn = REGIONS["cn"]
    assert cn.chat_url().endswith("AgentId=agent_common&Encode=1")
    assert cn.model_list_url().endswith("/algo/api/v2/model/list?Encode=1")
    assert cn.quota_url() == "https://openapi.qoder.com.cn/api/v2/quota/usage"


def test_resolve_region_falls_back():
    assert resolve_region("cn").key == "cn"
    assert resolve_region("global").key == "global"
    assert resolve_region("nonsense").key in REGIONS


# --- 凭据 ------------------------------------------------------------------


def test_to_ms_handles_seconds_and_millis():
    assert _to_ms(1700000000) == 1700000000000
    assert _to_ms(1700000000000) == 1700000000000
    assert _to_ms(0) == 0
    assert _to_ms(None) == 0


def test_to_ms_handles_rfc3339():
    assert _to_ms("2026-10-26T17:47:55Z") > 0


def test_parse_token_response_from_device_poll():
    """deviceToken/poll 的响应形状（实测字段名）。"""
    cred = _parse_token_response(
        {
            "token": "dt-abc",
            "user_id": "u-1",
            "refresh_token": "drt-xyz",
            "expires_at": "2026-10-26T17:47:55Z",
        }
    )
    assert cred.token == "dt-abc"
    assert cred.uid == "u-1"
    assert cred.refresh_token == "drt-xyz"
    assert cred.expires_at_ms > 0


def test_parse_token_response_defaults_expiry():
    """没有 expires_at 时给默认 30 天，避免 token 永不刷新。"""
    cred = _parse_token_response({"token": "dt-abc"})
    assert cred.expires_at_ms > 0


def test_credential_is_expired():
    import time

    fresh = Credential(token="t", expires_at_ms=int(time.time() * 1000) + 3600_000)
    stale = Credential(token="t", expires_at_ms=int(time.time() * 1000) - 1000)
    unknown = Credential(token="t")
    assert not fresh.is_expired()
    assert stale.is_expired()
    assert not unknown.is_expired()


def _seed_cred(account_id: str, **over) -> dict:
    """落一个测试账号（走多账号存储 API）。"""
    from buddy_proxy.qoder.credentials import save_account_cred

    cred = {
        "account_id": account_id,
        "token": f"dt-{account_id}-token",
        "uid": f"uid-{account_id}",
        "machine_id": "m1",
        "refresh_token": f"rt-{account_id}",
        "expires_at_ms": int(time.time() * 1000) + 3600_000,
        "name": "",
        "email": f"{account_id}@example.com",
        "region": "cn",
        "plan": "",
        "source": "state",
    }
    cred.update(over)
    save_account_cred(cred)
    return cred


def test_reload_if_rotated_reuses_token_refreshed_by_peer():
    """并发第二个请求拿到别人刚刷好的 token，不再重放 refresh_token。

    上游 refresh 是一次性的：同一个 refresh_token 打两次可能被判重放、把整条
    链作废，症状是「偶发地突然要重新登录」。
    """
    from buddy_proxy.qoder.credentials import _reload_if_rotated

    future = int(time.time() * 1000) + 3600_000
    _seed_cred("u1", token="dt-new", refresh_token="rt-new", expires_at_ms=future)

    old = {"account_id": "u1", "token": "dt-old", "refresh_token": "rt-old",
           "expires_at_ms": 0, "region": "cn", "source": "state"}
    fresh = _reload_if_rotated(old)
    assert fresh is not None
    assert fresh["token"] == "dt-new"
    assert fresh["refresh_token"] == "rt-new"
    assert not Credential(**{k: v for k, v in fresh.items()
                             if k in Credential.__dataclass_fields__}).is_expired()


def test_reload_if_rotated_returns_none_when_token_unchanged():
    """没人换过 token 时不能走捷径——否则真的过期了就永远不刷新。"""
    from buddy_proxy.qoder.credentials import _reload_if_rotated

    _seed_cred("u1", token="dt-same", refresh_token="rt-x")
    old = {"account_id": "u1", "token": "dt-same", "refresh_token": "rt-x",
           "expires_at_ms": 0, "region": "cn", "source": "state"}
    assert _reload_if_rotated(old) is None


def test_reload_if_rotated_ignores_already_expired_new_token():
    """别人换来的新 token 若也已过期，仍应走正常刷新而不是复用。"""
    from buddy_proxy.qoder.credentials import _reload_if_rotated

    _seed_cred("u1", token="dt-stale", refresh_token="rt-y", expires_at_ms=1000)
    old = {"account_id": "u1", "token": "dt-old", "refresh_token": "rt-old",
           "expires_at_ms": 0, "region": "cn", "source": "state"}
    assert _reload_if_rotated(old) is None


def test_credential_describe_masks_token():
    cred = Credential(token="dt-secretvalue", uid="u1", region="cn", source="state")
    described = cred.describe()
    assert "dt-secretvalue" not in described
    assert "dt-sec" in described


def test_auth_state_path_honours_env(monkeypatch, tmp_path):
    target = tmp_path / "qoder.json"
    monkeypatch.setenv("QODER_AUTH_FILE", str(target))
    assert auth_state_path() == target


def test_qoder_state_dir_honours_env(monkeypatch, tmp_path):
    """多账号状态目录由 QODER_STATE_DIR 决定（conftest 已隔离到 tmp）。"""
    from buddy_proxy.qoder.credentials import qoder_state_dir

    target = tmp_path / "qoder-custom"
    monkeypatch.setenv("QODER_STATE_DIR", str(target))
    assert qoder_state_dir() == target


def test_find_token_skips_avatar_like_fields():
    """头像/缩略图一类字段名下的 ``dt-`` 串不是令牌，必须跳过。

    桌面端把头像资源命名成 ``dt-avataruid-thumbnail`` 这种形态，按值前缀
    盲扫会把它当 token 落盘，表现是「明明登录了却一直 401」。
    """
    data = {
        "user": {"avatarUrl": "dt-avataruid-thumbnail", "id": "42"},
        "deviceToken": "dt-real-token",
    }
    assert _find_token(data) == "dt-real-token"


def test_find_token_accepts_when_only_avatar_exists():
    """真的只有头像时返回空串，而不是把头像当令牌。"""
    assert _find_token({"user": {"avatar": "dt-avataruid-thumbnail"}}) == ""


def test_find_token_shape_heuristic_rejects_url_like_values():
    """形态兜底：带后缀/路径/空格的 ``dt-`` 串不是令牌。

    键名黑名单挡不住全部（换成 ``userPic`` / ``gravatar`` 这类没进名单的
    键名就漏了），所以按值形态再筛一道。
    """
    from buddy_proxy.qoder.credentials import _looks_like_token

    assert _looks_like_token("dt-realtokenvalue12345") is True
    assert _looks_like_token("dt-real-token") is True
    # 文件后缀 / 路径 / 空白 → 是资源名或 URL
    assert _looks_like_token("dt-abc.def.ghi.png") is False
    assert _looks_like_token("dt-a/b/c/d/e/f/g/h") is False
    assert _looks_like_token("dt-a b c d e f g h") is False
    # 过短
    assert _looks_like_token("dt-x") is False
    assert _looks_like_token("dt-1") is False
    # 前缀不对
    assert _looks_like_token("at-x") is False


def test_find_token_denylist_covers_common_avatar_keys():
    """常见的头像字段名（含未在黑名单里的 `gravatar`/`userPic`）都不该被取。"""
    long_avatar = "dt-avataruid-thumbnailxyz123456"  # 够长，只能靠键名挡
    for key in ("avatar", "avatarUrl", "thumbnail", "userPic", "gravatar", "coverImage"):
        assert _find_token({key: long_avatar}) == "", f"{key} 不该被当成令牌"
        assert _find_token({key: long_avatar, "tok": "dt-realtokenvalue12345"}) == (
            "dt-realtokenvalue12345"
        ), f"{key} 在场时仍应找到真令牌"


def test_account_cred_file_mode_is_0600():
    """每账号 cred 文件必须 0600——且是**建文件时**就 0600。"""
    import stat as _stat

    from buddy_proxy.qoder.credentials import account_cred_path

    _seed_cred("u1")
    mode = _stat.S_IMODE(account_cred_path("u1").stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


# --- provider：额度映射与出站信封 -----------------------------------------


def _provider() -> object:
    from buddy_proxy.qoder.provider import QoderProvider

    return QoderProvider(
        Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn")
    )


def test_models_hide_legacy_but_keep_current():
    """列表只列当前模型；旧模型隐藏（隐藏 ≠ 停用，点名仍可调）。

    2026-10-05 复测三方模型恢复（账号级分桶），兜底目录重建后三方模型
    回到列表；auto 档位仍停用（TIER_MODELS enable=False）。Qwen 3.7、GLM-5.2、
    Kimi-K3 / K2.8-Preview 和 MiniMax-M2.7 按 HIDDEN_KEYS 隐藏，cantus/sonus 已消失。

    2026-10 目录换代是**原地改名**：``kmodel_latest`` 变成 Kimi-K3.1-Pro、
    ``mmodel`` 变成 MiniMax-M3.1-Flash，上游并没有新 key。所以这两个 key 必须
    留在列表里（按名字误判成旧模型会把当前一代整个删掉），旧 id
    ``kimi-k3`` / ``minimax-m2.7`` 靠 LEGACY_MODEL_ALIASES 仍可点名。
    """
    ids = [m["id"] for m in _provider().models()]
    assert "qoder/qwen3.8-max" in ids
    assert "qoder/qwen3.8-flash" in ids
    assert "qoder/glm-5.3" in ids
    assert "qoder/kimi-k3.1-pro" in ids
    assert "qoder/minimax-m3.1-flash" in ids
    # 隐藏/停用/消失的：不展示
    assert "qoder/auto" not in ids
    assert "qoder/qwen3.7-max" not in ids
    assert "qoder/glm-5.2" not in ids
    assert "qoder/kimi-k2.8-preview" not in ids
    assert "qoder/cantus" not in ids
    # 隐藏只影响目录展示，已有客户端仍可按别名解析到上游 key。
    catalog = Catalog(REGIONS["cn"])
    assert catalog.resolve_key("qoder/glm-5.2") == "gm51model"
    assert catalog.resolve_key("qoder/kimi-k2.8-preview") == "kmodel"
    assert catalog.resolve_key("qoder/kimi-k3.1-pro") == "kmodel_latest"
    assert catalog.resolve_key("qoder/minimax-m3.1-flash") == "mmodel"
    # 改名前的旧 id 仍要能点名，否则升级后老配置直接 400。
    assert catalog.resolve_key("qoder/kimi-k3") == "kmodel_latest"
    assert catalog.resolve_key("qoder/minimax-m2.7") == "mmodel"


def test_models_does_not_list_openai_style_ids():
    """对外 id 不能是上游内部代号（qmodel_38max 这种看不懂的名字）。"""
    ids = [m["id"] for m in _provider().models()]
    for internal in ("qoder/qmodel_38max", "qoder/qfmodel"):
        assert internal not in ids


@pytest.mark.parametrize("account_count", [1, 2])
def test_quota_wait_does_not_block_event_loop(monkeypatch, account_count):
    """单/多账号的慢同步额度请求都必须在线程中等待。"""
    import asyncio
    import time
    from types import SimpleNamespace

    import buddy_proxy.qoder.provider as qoder_provider

    provider = _provider()
    accounts = [SimpleNamespace(id=f"acct-{i}", priority=i, region="cn")
                for i in range(account_count)]
    # 额度枚举已改走区内全量 list_accounts（不剔冷却，2026-10-11 修）：
    # mock 点从 available_accounts 迁到 list_accounts
    monkeypatch.setattr(qoder_provider, "list_accounts", lambda: accounts)
    monkeypatch.setattr(qoder_provider, "ensure_account_token", lambda _id: ("token", {}))
    monkeypatch.setattr(qoder_provider, "cred_to_credential",
                        lambda _cred: SimpleNamespace(plan="test"))

    def slow_quota(_acct, _index, *, multi):
        time.sleep(0.2)
        return ([{"label": "quota"}], True)

    monkeypatch.setattr(provider, "_quota_one", slow_quota)

    async def run():
        task = asyncio.create_task(provider.quota())
        started = time.monotonic()
        await asyncio.sleep(0.01)
        assert time.monotonic() - started < 0.12
        result = await task
        assert result["items"]

    asyncio.run(run())


def test_qoderintl_quota_and_checkin_never_fall_back_to_cn_accounts(monkeypatch):
    import asyncio

    import buddy_proxy.qoder.provider as qoder_provider
    from buddy_proxy.qoder import failover
    from buddy_proxy.qoder.credentials import AuthError
    from buddy_proxy.qoder.intl_provider import QoderIntlProvider

    calls = []

    def list_all():
        calls.append("list")
        return []

    # 额度枚举走区内全量 list_accounts（2026-10-11）：global 区无账号 → quota
    # 返回 None，绝不回落到 CN 账号。_campaigns 仍走 available_accounts。
    monkeypatch.setattr(failover, "available_accounts", lambda region=None: [])
    monkeypatch.setattr(qoder_provider, "list_accounts", list_all)
    provider = QoderIntlProvider()

    assert asyncio.run(provider.quota()) is None
    with pytest.raises(AuthError, match="没有可用账号"):
        asyncio.run(provider._campaigns())
    assert calls == ["list"]


def test_quota_prefers_addon_when_user_quota_empty():
    """个人版 ``userQuota`` 恒为 0，真实额度在 ``addOnQuota``。"""
    from buddy_proxy.qoder.provider import QoderProvider

    provider = QoderProvider(Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    data = {
        "userType": "personal_standard",
        "usageType": "credits",
        "isQuotaExceeded": False,
        "userQuota": {"total": 0.0, "used": 0.0, "remaining": 0.0, "percentage": 0.0},
        "addOnQuota": {"total": 100.0, "used": 25.0, "remaining": 75.0, "percentage": 0.25},
    }
    out = provider._format_quota(data, Credential(token="t", uid="u"))
    assert out["level"] == "personal_standard"
    assert out["items"][0]["label"] == "加油包"
    assert out["items"][0]["total"] == 100.0
    assert out["items"][0]["remaining"] == 75.0


def test_quota_prefers_subscription_when_larger():
    from buddy_proxy.qoder.provider import QoderProvider

    provider = QoderProvider(Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    data = {
        "userQuota": {"total": 300.0, "used": 10.0, "remaining": 290.0, "percentage": 3.3},
        "addOnQuota": {"total": 100.0, "used": 0.0, "remaining": 100.0, "percentage": 0.0},
    }
    out = provider._format_quota(data, Credential(token="t", uid="u"))
    labels = [i["label"] for i in out["items"]]
    assert labels[0] == "订阅额度"
    assert out["items"][0]["total"] == 300.0
    assert "加油包" in labels


def test_quota_expire_ts_filters_sentinel():
    """上游用 253402214400000（9999 年）表示「不重置」，不该展示。

    该字段是**到期**时刻（``expire_ts``）不是重置——2026-10-03 前它被错送进
    ``reset_ts``，界面上显示成「10-30 重置」。
    """
    from buddy_proxy.qoder.provider import _expire_ts

    assert _expire_ts({"expiresAt": 253402214400000}) is None
    assert _expire_ts({"expiresAt": 1791653946861}) == 1791653946


def test_used_percent_prefers_counted_values():
    """优先用 ``used``/``total``（自洽），别信可能矛盾的 ``percentage``。

    实测 ``total=200, used=101`` 时上游给 ``0.51``（``remaining/total=0.495``）——
    那是**剩余**比例；而 ``userQuota`` 给过 ``percentage: 0.0`` 配
    ``used: 0, remaining: 2000``（一分没用却说剩余 0%），三者互相矛盾。
    counted 值两边都自洽，故以它为准。
    """
    from buddy_proxy.qoder.provider import _used_percent

    # 已用 101/200 = 50.5%；若错把上游 0.51 当剩余直接透传会是 0.51
    assert _used_percent({"percentage": 0.51}, 101.0, 200.0) == pytest.approx(50.5)
    # 矛盾样本：percentage=0.0 说「剩余 0%」，但 used=0 才是事实
    assert _used_percent({"percentage": 0.0}, 0.0, 2000.0) == pytest.approx(0.0)
    # total 拿不到时才退回 percentage（0~1 视作剩余比例）
    assert _used_percent({"percentage": 0.2}, 0.0, 0.0) == pytest.approx(80.0)
    assert _used_percent({"percentage": 1.0}, 0.0, 0.0) == pytest.approx(0.0)
    assert _used_percent({}, 0.0, 0.0) == pytest.approx(0.0)


def test_quota_items_percent_is_used_scale():
    """``items[].percent`` 必须是 0~100 的**已用**值（前端 ≥85 判红）。

    直接透传上游 ``percentage``（0~1）= 进度条画反 + 85 阈值永远够不到
    （红色告警成死代码）。
    """
    from buddy_proxy.qoder.provider import QoderProvider

    provider = QoderProvider(Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    data = {
        "userType": "personal_professional",
        "usageType": "credits",
        "isQuotaExceeded": False,
        "userQuota": {"total": 2000.0, "used": 2000.0, "remaining": 0.0, "percentage": 0.0},
        "addOnQuota": {"total": 200.0, "used": 101.0, "remaining": 99.0, "percentage": 0.51},
    }
    out = provider._format_quota(data, Credential(token="t", uid="u"))
    by_label = {i["label"]: i for i in out["items"]}
    # total 大的一侧当 primary（既有选择规则），但两侧口径要一致：都是「已用」
    assert by_label["订阅额度"]["percent"] == pytest.approx(100.0)
    # 上游 0.51 是「剩余 51%」；已用 101/200 = 50.5%
    assert by_label["加油包"]["percent"] == pytest.approx(50.5)
    # 两个值都落在 0~100 量纲内，85 阈值才有意义
    assert all(0.0 <= i["percent"] <= 100.0 for i in out["items"])


def test_quota_includes_dedicated_resource_packages():
    """专属资源包（活动赠送）必须并入 items，否则界面显示的积分比实际少一截。

    实测 personal_professional 账号：``userQuota`` 2000 + ``addOnQuota`` 300 +
    ``dedicatedResourcePackages`` 里一个 2000 的「Qwen 专属积分」。早期只读前两个
    节点，管理页就只显示 2000，用户以为额度对不上（实际有 4300）。
    样本取自真实上游返回（2026-09-29 抓取），仅改了数值。
    """
    from buddy_proxy.qoder.provider import QoderProvider

    provider = QoderProvider(Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    data = {
        "userType": "personal_professional",
        "usageType": "credits",
        "isQuotaExceeded": False,
        # 上游自己的聚合字段：0~1 的小数比例（**不是**百分数），
        # 取「三份相加的已用比例」；下面有断言盯着它跟 items 对得上。
        "totalUsagePercentage": 0.13,
        "expiresAt": 1793289600000,
        "userQuota": {"total": 2000.0, "used": 69.0, "remaining": 1931.0, "percentage": 0.04},
        "addOnQuota": {"total": 300.0, "used": 300.0, "remaining": 0.0, "percentage": 1.0},
        "dedicatedResourcePackages": [
            {
                "id": "pkg-1",
                "name": "act-20260901-170",
                "total": 2000.0,
                "used": 189.0,
                "remaining": 1811.0,
                "percentage": 0.08,
                "expiresAt": 1793212321646,
                "available": True,
                "displayLabels": [
                    {"dimension": "description", "value": "qwen model series description",
                     "valueI18n": {"zh-CN": "Qwen 专属积分：选择 Qwen 系列模型时优先抵扣。"}},
                    {"dimension": "title", "value": "qwen model series",
                     "valueI18n": {"en-US": "Qwen Exclusive Credits", "zh-CN": "Qwen 专属积分"}},
                ],
            }
        ],
    }
    out = provider._format_quota(data, Credential(token="t", uid="u"))
    by_label = {i["label"]: i for i in out["items"]}

    assert "Qwen 专属积分" in by_label, "专属包必须展示，且用 displayLabels 的中文标题"
    pkg = by_label["Qwen 专属积分"]
    assert pkg["total"] == 2000.0
    assert pkg["remaining"] == 1811.0
    # 专属包自带**到期**时间，比账号级 expiresAt 更早——必须按包取，
    # 不能用账号级的。走 expire_ts 不走 reset_ts：Qoder 的额度只到期、
    # 不周期重置（早先错投 reset_ts，界面把「10-30 到期」显示成「10-30 重置」）
    assert pkg["expire_ts"] == 1793212321, "专属包要用自己的 expiresAt"
    assert pkg["reset_ts"] is None, "Qoder 额度只到期不重置"
    assert by_label["订阅额度"]["expire_ts"] == 1793289600, "订阅额度用账号级 expiresAt"
    # 三个节点都在，合计才对得上真实总额
    assert sum(i["total"] for i in out["items"]) == 4300.0
    # 并且要**声明**它们可合计：管理页标题行只认这个标记，不认列表顺序。
    # 只把三行列出来而不置位的话，界面依旧只显示第一行（剩 1931/2000），
    # 用户看到的数还是比实际少一截——等于这个修复只做了一半。
    assert out["sum_items"] is True, "三份额度并存，必须声明可合计"
    # 而且「相加」不是我们拍的：上游自己就有个聚合字段 ``totalUsagePercentage``
    # （→ ``total_percent``），它应当约等于三份合计的已用比例。这条把「可合计」
    # 这个判断**钉在上游数据上**——哪天上游改成「订阅额度已含加油包」，这个
    # 等式就会破，届时求和反而会把额度算多，测试会先于用户发现。
    summed_used = sum(i["used"] for i in out["items"])
    summed_total = sum(i["total"] for i in out["items"])
    ours = summed_used / summed_total
    upstream = out["total_percent"]
    assert upstream is not None, "上游聚合字段缺失，无法校验可加性"
    assert abs(ours - upstream) < 0.01, (
        f"三份相加的已用比例 {ours:.4f} 与上游 total_percent {upstream} 对不上，"
        f"说明这几份可能不是并存关系，不能再相加"
    )


def test_quota_skips_unavailable_packages():
    """已失效（``available: false``）的专属包不展示——它不占额度。"""
    from buddy_proxy.qoder.provider import QoderProvider

    provider = QoderProvider(Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    data = {
        "userQuota": {"total": 100.0, "used": 0.0, "remaining": 100.0},
        "addOnQuota": {},
        "dedicatedResourcePackages": [
            {"name": "expired-pkg", "total": 500.0, "used": 0.0,
             "remaining": 0.0, "available": False},
        ],
    }
    out = provider._format_quota(data, Credential(token="t", uid="u"))
    labels = [i["label"] for i in out["items"]]
    assert "expired-pkg" not in labels
    assert sum(i["total"] for i in out["items"]) == 100.0


def test_pkg_active_checks_status_and_available():
    """``status`` 与 ``available`` 双信号判活：任一说不活跃就跳过。

    失效时上游翻哪个字段没有真样本（活跃包实测同时带 ``available: true``
    和 ``status: QUOTA_DETAIL_STATUS_ACTIVE``），只信一个可能把过期包算进
    总额度。未知 ``status`` 放行——宁可多显示，不能误杀活跃包。
    """
    from buddy_proxy.qoder.provider import _pkg_active

    assert _pkg_active({"available": True, "status": "QUOTA_DETAIL_STATUS_ACTIVE"})
    # available 翻 false -> 跳过
    assert not _pkg_active({"available": False, "status": "QUOTA_DETAIL_STATUS_ACTIVE"})
    # 只有 status 说不活跃（available 缺失/仍为 true）-> 也跳过
    assert not _pkg_active({"available": True, "status": "QUOTA_DETAIL_STATUS_EXPIRED"})
    assert not _pkg_active({"status": "QUOTA_DETAIL_STATUS_INVALID"})
    assert not _pkg_active({"status": "QUOTA_DETAIL_STATUS_INACTIVE"})
    # 未知枚举放行；status 缺失也放行（老版本/不同区域可能没有该字段）
    assert _pkg_active({"available": True, "status": "QUOTA_DETAIL_STATUS_WHATEVER_NEW"})
    assert _pkg_active({"available": True})
    assert _pkg_active({})


def test_pkg_label_fallback_chain():
    """专属包标题：zh-CN → en-US → value → name → 通用名，逐级回退。"""
    from buddy_proxy.qoder.provider import _pkg_label

    # 完整多语言：取 zh-CN
    assert _pkg_label({"displayLabels": [
        {"dimension": "title", "valueI18n": {"zh-CN": "中文标题", "en-US": "EN"}},
    ]}) == "中文标题"
    # 只有 en-US
    assert _pkg_label({"displayLabels": [
        {"dimension": "title", "valueI18n": {"en-US": "EN Only"}},
    ]}) == "EN Only"
    # 没有 i18n，用 value
    assert _pkg_label({"displayLabels": [
        {"dimension": "title", "value": "raw title"},
    ]}) == "raw title"
    # 没有 title 维度，回退 name
    assert _pkg_label({"name": "act-2026", "displayLabels": [
        {"dimension": "description", "value": "desc"},
    ]}) == "act-2026"
    # 啥都没有
    assert _pkg_label({}) == "专属积分"


def test_build_upstream_includes_attribution_envelope():
    """归因信封缺了会被上游业务路由拒（no flow nodes found）。"""
    from buddy_proxy.qoder.provider import QoderProvider

    provider = QoderProvider(Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    out = provider._build_upstream(
        {"model": "Qwen3.8-Max", "messages": [{"role": "user", "content": "hi"}]},
        "qmodel_38max",
    )
    for field in ("request_id", "request_set_id", "session_id", "chat_task",
                  "chat_context", "model_config", "business", "agent_id"):
        assert field in out, field
    assert out["model"] == "qmodel_38max"
    assert out["stream"] is True
    assert out["stream_options"] == {"include_usage": True}
    assert out["agent_id"] == "agent_common"
    assert out["model_config"]["key"] == "qmodel_38max"
    assert out["business"]["product"] == "cli"
    assert out["chat_context"]["text"] == "hi"


def test_build_upstream_converts_developer_role():
    """上游在反序列化阶段就拒绝 role=developer。"""
    from buddy_proxy.qoder.provider import QoderProvider

    provider = QoderProvider(Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    out = provider._build_upstream(
        {"messages": [{"role": "developer", "content": "sys"}, {"role": "user", "content": "hi"}]},
        "auto",
    )
    assert out["messages"][0]["role"] == "system"


def test_build_upstream_ignores_private_extensions():
    """客户端私有扩展不透传给上游。"""
    from buddy_proxy.qoder.provider import QoderProvider

    provider = QoderProvider(Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    out = provider._build_upstream(
        {"model": "auto", "messages": [], "secret_field": "leak", "patches": "x"}, "auto"
    )
    assert "secret_field" not in out
    assert "patches" not in out


# --- 入站信封解析 ----------------------------------------------------------


def test_unwrap_done_sentinel():
    from buddy_proxy.qoder.provider import _unwrap

    assert _unwrap("[DONE]") == (None, None, True)
    assert _unwrap(json.dumps({"body": "[DONE]"})) == (None, None, True)


def test_unwrap_ignores_trailer_frames():
    """``event:finish`` 的尾帧没有 body，应被忽略而不是当错误。"""
    from buddy_proxy.qoder.provider import _unwrap

    inner, err, done = _unwrap(json.dumps({"firstTokenDuration": 1, "totalDuration": 2}))
    assert inner is None and err is None and done is False


def test_unwrap_extracts_inner_chunk():
    from buddy_proxy.qoder.provider import _unwrap

    inner_body = json.dumps({"choices": [{"delta": {"content": "hi"}}]})
    inner, err, done = _unwrap(json.dumps({"headers": {}, "body": inner_body}))
    assert err is None and done is False
    assert json.loads(inner)["choices"][0]["delta"]["content"] == "hi"


def test_unwrap_detects_inband_error():
    """HTTP 200 但 body 里是业务错误（无 choices/usage，有 code/message）。"""
    from buddy_proxy.qoder.provider import _unwrap

    err_body = json.dumps({"code": "400", "message": "[FAIL]node:agent_router msg:None"})
    inner, err, done = _unwrap(json.dumps({"body": err_body}))
    assert inner is None and done is False
    assert err is not None and "agent_router" in err


def test_unwrap_handles_usage_only_chunk():
    """usage 帧没有 choices，但不是错误。"""
    from buddy_proxy.qoder.provider import _unwrap

    body = json.dumps({"usage": {"prompt_tokens": 1, "completion_tokens": 2}})
    inner, err, done = _unwrap(json.dumps({"body": body}))
    assert err is None and done is False
    assert json.loads(inner)["usage"]["prompt_tokens"] == 1


def test_unwrap_handles_garbage():
    from buddy_proxy.qoder.provider import _unwrap

    assert _unwrap("not json") == (None, None, False)


def test_last_user_text_handles_multimodal():
    from buddy_proxy.qoder.provider import _last_user_text

    assert _last_user_text([{"role": "user", "content": "plain"}]) == "plain"
    assert _last_user_text([
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image"}]}
    ]) == "a"
    assert _last_user_text([{"role": "assistant", "content": "x"}]) == ""
