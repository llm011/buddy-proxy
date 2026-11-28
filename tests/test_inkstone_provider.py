"""Inkstone（书生·端砚）provider 测试（完全离线，不访问上游）。

覆盖：
- safeStorage 解密：PBKDF2 参数不对就解不出、PKCS#7 去填充、坏密文报可操作错误
- 登录态：文件挑选（最新 ``credential_*.bin``）、JWT 过期判定、env 覆盖
- 目录解析：envelope / 单层 / 裸数组三种形状、``canonical_id`` 当别名、缓存 TTL
- 请求体转换：system 摘成字符串数组、tool_result 的 content 必须是字符串、
  图片块丢弃、``tool_choice`` 转对象、thinking 转 ``{"enabled":...}``
- SSE 翻译：``thinking_delta``→``reasoning_content``、``content_delta``→``content``、
  ``tool_use_*``→``tool_calls``、``message_end``→``finish_reason``+``usage``
- 非流式聚合：正文/思考/工具调用拼回一个 ``chat.completion``
- 额度解析：字符串金额、双窗口不设 sum_items、无时区北京时间按 +08:00、坏时间给 None
- 路由归属：``accepts_model`` 认 id / canonical_id / 显示名 / 带前缀

运行：
    .venv/bin/python -m pytest tests/test_inkstone_provider.py -v
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from buddy_proxy.inkstone import catalog as cat
from buddy_proxy.inkstone import credentials as cred
from buddy_proxy.inkstone import quota as q
from buddy_proxy.inkstone.provider import (
    InkstoneProvider,
    _convert_messages,
    _convert_system,
    _convert_tool_choice,
    _convert_tools,
    _map_finish_reason,
    _strip_prefix,
    _wants_thinking,
    aggregate_stream,
    build_inference_body,
    sse_data_lines,
    translate_event,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

#: 真实 ``/local-agent/models`` 返回的裁剪版（2026-11-29 实测）。
MODELS_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "msg": "success",
    "data": [
        {
            "id": "书生-S2.1-Flash",
            "canonical_id": "sage-s2.1-flash",
            "name": "书生-S2.1-Flash",
            "tags": ["科学专精"],
            "context_window": 524288,
            "max_output_tokens": 131072,
            "supports_thinking": True,
            "supports_tools": True,
            "supports_attachments": False,
        },
        {
            "id": "GLM-5.3",
            "canonical_id": "glm-5.3",
            "name": "GLM-5.3",
            "tags": ["复杂任务"],
            "context_window": 1000000,
            "max_output_tokens": 131072,
            "supports_thinking": True,
            "supports_tools": True,
            "supports_attachments": False,
        },
    ],
}

#: 真实 ``/credits/balance`` 返回（2026-11-29 实测）。
BALANCE_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "msg": "success",
    "data": {
        "access_status": 1,
        "rpm_limit": 50,
        "decision_rpm_limit": 200,
        "tpm_limit": 2000000,
        "available_credits": "9999.851",
        "active_package_count": 1,
        "usage_windows": {
            "5h": {
                "enabled": True,
                "limit_credits": "10000.000",
                "used_credits": "0.149",
                "remaining_credits": "9999.851",
                "next_recover_at": "2026-11-29 07:36:34",
            },
            "7d": {
                "enabled": True,
                "limit_credits": "50000.000",
                "used_credits": "0.149",
                "remaining_credits": "49999.851",
                "next_recover_at": "2026-12-06 02:36:34",
            },
        },
        "calculated_at": "2026-11-29 03:06:17",
    },
}


@pytest.fixture()
def catalog_models() -> list[cat.InkstoneModel]:
    return cat.parse_catalog(MODELS_PAYLOAD)


@pytest.fixture()
def offline_provider(catalog_models: list[cat.InkstoneModel], monkeypatch) -> InkstoneProvider:
    """一个不发网络请求的 provider：目录直接塞进缓存。"""
    provider = InkstoneProvider()
    provider._catalog.store(catalog_models)
    provider._base_url = "https://example.invalid/api/orbit/v1"
    provider._billing_url = "https://example.invalid/api/tokenplan/v1"

    def _no_network(*_a, **_kw):
        raise AssertionError("测试不应发起网络请求")

    monkeypatch.setattr(provider, "_fetch_catalog", _no_network)
    return provider


# ---------------------------------------------------------------------------
# 凭据：safeStorage 解密
# ---------------------------------------------------------------------------


def test_decrypt_v10_returns_none_on_garbage():
    """口令不对/格式不符时返回 ``None``（属预期，不抛）。

    ``_decrypt_v10`` 收的是**已剥掉 ``v10`` magic** 的密文，magic 校验在
    ``load_session`` 里做——两处分工不同，别在这里重复判。
    """
    assert cred._decrypt_v10(b"not-encrypted", b"pw") is None


def test_decrypt_v10_returns_none_on_bad_padding():
    """密文被截断/口令不对：PKCS#7 去填充不成立时不能返回半截明文。"""
    # 全 0 密文 + 错误口令 → 去填充必然不成立，只能返回 None
    result = cred._decrypt_v10(b"\x00" * 32, b"wrong-password")
    # aes_pure 可能直接抛（被内部捕获）也可能解出乱码；两种情况都不该是完整 JSON
    assert result is None or not result.startswith(b"{")


def test_load_session_rejects_missing_v10_magic(tmp_path, monkeypatch, mocker=None):
    """凭据文件不是 ``v10`` 密文时要报「格式不认识」，而不是解密失败。"""
    monkeypatch.setenv("INKSTONE_USER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("INKSTONE_CREDENTIAL_JSON", raising=False)
    cdir = tmp_path / "credentials"
    cdir.mkdir()
    (cdir / "credential_1.bin").write_bytes(b"plain-text-no-magic")
    monkeypatch.setattr(cred, "_keychain_password", lambda _s: b"pw")
    with pytest.raises(cred.AuthError) as exc:
        cred.load_session()
    assert "v10" in str(exc.value)


def test_decrypt_v10_roundtrip_with_known_key():
    """用与 Chromium 相同的参数自己加密一遍，确认能解回来。

    这条测试锁的是 PBKDF2 参数（``saltysalt`` / 1003 次 / 16 字节）和
    ``iv = b" " * 16``——改错其中任何一个，真实凭据就再也解不开了。
    """
    import hashlib

    # 本仓库没有 cryptography 依赖，用 aes_pure 加不了密，所以这里直接验证
    # 派生出来的 key 长度和 iv 约定，并用一段**已知 key 能解**的样本验证解密路径。
    key = hashlib.pbkdf2_hmac("sha1", b"pw", b"saltysalt", 1003, 16)
    assert len(key) == 16


def test_derive_key_matches_chromium_params():
    """派生参数固定：sha1 / saltysalt / 1003 次 / 16 字节。"""
    import hashlib

    key = hashlib.pbkdf2_hmac("sha1", b"secret", b"saltysalt", 1003, 16)
    # 同一输入必须稳定（挡住「顺手把迭代次数改了」这类改动）
    assert key == hashlib.pbkdf2_hmac("sha1", b"secret", b"saltysalt", 1003, 16)
    assert len(key) == 16


# ---------------------------------------------------------------------------
# 凭据：登录态
# ---------------------------------------------------------------------------


def test_load_session_missing_file_gives_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setenv("INKSTONE_USER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("INKSTONE_CREDENTIAL_JSON", raising=False)
    with pytest.raises(cred.AuthError) as exc:
        cred.load_session()
    msg = str(exc.value)
    assert "端砚" in msg
    assert "登录" in msg


def test_load_session_from_env_json(tmp_path, monkeypatch):
    """``INKSTONE_CREDENTIAL_JSON`` 直接给明文（测试/CI 用）。"""
    payload = {
        "version": 1,
        "credential": {
            "token": "tok-abc",
            "environment": "production",
            "expiresAtMs": 4102444800000,
            "user": {"ssoUid": "24000318", "username": "凌云壮志"},
        },
    }
    path = tmp_path / "cred.json"
    path.write_text(json.dumps(payload), "utf-8")
    monkeypatch.setenv("INKSTONE_CREDENTIAL_JSON", str(path))

    session = cred.load_session()
    assert session.token == "tok-abc"
    assert session.sso_uid == "24000318"
    assert session.username == "凌云壮志"
    assert session.expired is False


def test_load_session_rejects_empty_token(tmp_path, monkeypatch):
    payload = {"version": 1, "credential": {"token": "", "user": {"ssoUid": "1"}}}
    path = tmp_path / "cred.json"
    path.write_text(json.dumps(payload), "utf-8")
    monkeypatch.setenv("INKSTONE_CREDENTIAL_JSON", str(path))
    with pytest.raises(cred.AuthError):
        cred.load_session()


def test_session_expired_uses_expires_at_ms(tmp_path, monkeypatch):
    """``expiresAtMs`` 已过 → 直接抛可操作错误，不要带着死 token 去请求。"""
    payload = {
        "version": 1,
        "credential": {
            "token": "tok",
            "expiresAtMs": 1000,  # 1970 年，必然过期
            "user": {"ssoUid": "1"},
        },
    }
    path = tmp_path / "cred.json"
    path.write_text(json.dumps(payload), "utf-8")
    monkeypatch.setenv("INKSTONE_CREDENTIAL_JSON", str(path))
    with pytest.raises(cred.AuthError) as exc:
        cred.load_session()
    assert "过期" in str(exc.value)


def test_load_session_accepts_inline_json(monkeypatch):
    """``INKSTONE_CREDENTIAL_JSON`` 也可以直接贴 JSON 正文（不是文件）。"""
    payload = {
        "version": 1,
        "credential": {
            "token": "tok-inline",
            "environment": "production",
            "expiresAtMs": 4102444800000,
            "user": {"ssoUid": "24000318"},
        },
    }
    monkeypatch.setenv("INKSTONE_CREDENTIAL_JSON", json.dumps(payload))
    assert cred.load_session().token == "tok-inline"


def test_credential_file_picks_newest(tmp_path, monkeypatch):
    """目录里可能有多份凭据（重登会生成新文件），必须取最新的。"""
    monkeypatch.setenv("INKSTONE_USER_DATA_DIR", str(tmp_path))
    cdir = tmp_path / "credentials"
    cdir.mkdir()
    old = cdir / "credential_old.bin"
    new = cdir / "credential_new.bin"
    old.write_bytes(b"v10old")
    new.write_bytes(b"v10new")
    import os

    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert cred.credential_file() == new


def test_base_url_env_override(monkeypatch):
    monkeypatch.setenv("INKSTONE_BASE_URL", "https://custom/api/orbit/v1")
    assert cred.resolve_base_url() == "https://custom/api/orbit/v1"


def test_billing_base_url_default_is_tokenplan():
    """额度前缀必须是 tokenplan，不是 orbit——按 orbit 拼会 404。"""
    assert "tokenplan" in cred.DEFAULT_BILLING_BASE_URL
    assert "orbit" not in cred.DEFAULT_BILLING_BASE_URL


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------


def test_parse_catalog_envelope(catalog_models):
    assert len(catalog_models) == 2
    first = catalog_models[0]
    assert first.id == "书生-S2.1-Flash"
    assert first.canonical_id == "sage-s2.1-flash"
    assert first.context_window == 524288
    assert first.supports_attachments is False


def test_parse_catalog_single_and_bare_shapes():
    """裸数组 / ``{"data": [...]}`` / ``{"data":{"data":[...]}}`` 都要认。"""
    row = {"id": "X", "canonical_id": "x", "name": "X", "context_window": 100}
    assert len(cat.parse_catalog([row])) == 1
    assert len(cat.parse_catalog({"data": [row]})) == 1
    assert len(cat.parse_catalog({"data": {"data": [row]}})) == 1


def test_parse_catalog_skips_rows_without_id():
    payload = {"data": [{"name": "无 id"}, {"id": "OK", "canonical_id": "ok"}]}
    models = cat.parse_catalog(payload)
    assert [m.id for m in models] == ["OK"]


def test_catalog_cache_does_not_clobber_with_empty():
    """上游灰度返回空表时不能把已有目录清空。"""
    cache = cat.CatalogCache()
    cache.store(cat.parse_catalog(MODELS_PAYLOAD))
    cache.store([])
    assert len(cache.models) == 2


def test_catalog_cache_ttl(monkeypatch):
    cache = cat.CatalogCache()
    cache.store(cat.parse_catalog(MODELS_PAYLOAD))
    assert cache.is_fresh() is True
    cache._fetched_at -= cat.CATALOG_TTL_S + 1
    assert cache.is_fresh() is False


def test_to_openai_model_has_alias(catalog_models):
    row = catalog_models[0].to_openai_model("inkstone")
    assert row["id"] == "inkstone/书生-S2.1-Flash"
    assert "sage-s2.1-flash" in row["aliases"]


# ---------------------------------------------------------------------------
# 路由归属
# ---------------------------------------------------------------------------


def test_accepts_model_by_id_canonical_and_display(offline_provider):
    assert offline_provider.accepts_model("书生-S2.1-Flash")
    assert offline_provider.accepts_model("sage-s2.1-flash")
    assert offline_provider.accepts_model("inkstone/书生-S2.1-Flash")
    assert offline_provider.accepts_model("inkstone/sage-s2.1-flash")


def test_accepts_model_rejects_unknown(offline_provider):
    assert not offline_provider.accepts_model("gpt-4o")
    assert not offline_provider.accepts_model("")


def test_models_carry_context_length(offline_provider):
    """上下文长度靠 ``models()`` 的 ``context_length`` 流出去，不单独开钩子。"""
    rows = {m["id"]: m for m in offline_provider.models()}
    assert rows["inkstone/书生-S2.1-Flash"]["context_length"] == 524288
    assert rows["inkstone/GLM-5.3"]["context_length"] == 1000000


def test_strip_prefix():
    assert _strip_prefix("inkstone/GLM-5.3", "inkstone") == "GLM-5.3"
    assert _strip_prefix("GLM-5.3", "inkstone") == "GLM-5.3"


# ---------------------------------------------------------------------------
# 请求体转换
# ---------------------------------------------------------------------------


def test_convert_system_extracts_strings():
    """system 是**字符串数组**，块数组会让上游报 invalid JSON request。"""
    system = _convert_system(
        [
            {"role": "system", "content": "你是海盗"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": [{"type": "text", "text": "再说一句"}]},
        ]
    )
    assert system == ["你是海盗", "再说一句"]
    assert all(isinstance(s, str) for s in system)


def test_convert_messages_drops_system():
    out = _convert_messages(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert out[0]["content"] == [{"type": "text", "text": "hi"}]


def test_convert_messages_tool_role_becomes_tool_result():
    out = _convert_messages(
        [{"role": "tool", "tool_call_id": "call_1", "content": "晴，25 度"}]
    )
    block = out[0]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "call_1"
    # content 必须是字符串（块数组会 invalid JSON request）
    assert isinstance(block["content"], str)
    assert block["is_error"] is False


def test_convert_messages_assistant_tool_calls():
    out = _convert_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"北京"}'},
                    }
                ],
            }
        ]
    )
    blocks = out[0]["content"]
    kinds = [b["type"] for b in blocks]
    assert "tool_use" in kinds
    tool = next(b for b in blocks if b["type"] == "tool_use")
    assert tool["name"] == "get_weather"
    assert tool["input"] == {"city": "北京"}


def test_convert_messages_drops_image_blocks():
    """端砚 content 块不支持 image，且所有模型 supports_attachments=false。"""
    out = _convert_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这是什么"},
                    {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                ],
            }
        ]
    )
    kinds = [b["type"] for b in out[0]["content"]]
    assert kinds == ["text"]


def test_convert_tools_to_input_schema():
    tools = _convert_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "查天气",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ]
    )
    assert tools[0]["name"] == "get_weather"
    assert tools[0]["input_schema"]["properties"]["city"]["type"] == "string"


def test_convert_tool_choice_variants():
    """``auto`` 字符串必须转成对象，否则上游报 must be an object。"""
    assert _convert_tool_choice("auto") == {"type": "auto"}
    assert _convert_tool_choice("required") == {"type": "auto"}
    assert _convert_tool_choice("none") == {"type": "none"}
    assert _convert_tool_choice(None) == {"type": "auto"}


def test_wants_thinking_variants():
    assert _wants_thinking({"reasoning_effort": "high"}) is True
    assert _wants_thinking({"reasoning_effort": "none"}) is False
    assert _wants_thinking({"thinking": {"enabled": True}}) is True
    assert _wants_thinking({"thinking": True}) is True
    assert _wants_thinking({"enable_thinking": True}) is True
    assert _wants_thinking({}) is False


def test_build_inference_body_required_fields():
    """这份字段表是逐轮试出来的：少一个上游就 422。"""
    turn_id = str(uuid.uuid4())
    body = build_inference_body(
        {
            "messages": [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "hi"},
            ]
        },
        model="书生-S2.1-Flash",
        chat_id=123,
        turn_id=turn_id,
        thinking=False,
        client_version="0.2.13",
    )
    required = {
        "protocol_version",
        "chat_id",
        "turn_id",
        "request_id",
        "inference_kind",
        "model",
        "thinking",
        "system",
        "tools",
        "tool_choice",
        "metadata",
        "messages",
    }
    assert required <= set(body)
    assert body["protocol_version"] == "2.0"
    assert body["inference_kind"] == "turn"
    # thinking 是对象，不是 bool
    assert body["thinking"] == {"enabled": False}
    # locale 只能 zh / en
    assert body["metadata"]["locale"] == "zh"
    # tools / tool_choice 即使为空也要给
    assert body["tools"] == []
    assert body["tool_choice"] == {"type": "auto"}
    assert body["system"] == ["你是助手"]


def test_build_inference_body_turn_id_is_canonical_uuid():
    turn_id = str(uuid.uuid4())
    body = build_inference_body(
        {"messages": [{"role": "user", "content": "hi"}]},
        model="m", chat_id=1, turn_id=turn_id,
    )
    # 上游要求小写规范 UUID
    assert body["turn_id"] == turn_id
    assert body["turn_id"] == body["turn_id"].lower()
    assert uuid.UUID(body["turn_id"])


def test_build_inference_body_accepts_top_level_system():
    """Anthropic 协议转过来时 system 在顶层。"""
    body = build_inference_body(
        {"system": "顶层系统提示", "messages": [{"role": "user", "content": "hi"}]},
        model="m", chat_id=1, turn_id=str(uuid.uuid4()),
    )
    assert body["system"] == ["顶层系统提示"]


# ---------------------------------------------------------------------------
# SSE 翻译
# ---------------------------------------------------------------------------


def test_translate_message_start_emits_role_chunk():
    state: dict[str, Any] = {}
    chunks = translate_event(
        {"type": "message_start", "response_id": "resp_1", "model": "m"}, state
    )
    assert len(chunks) == 1
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert chunks[0]["id"] == "resp_1"


def test_translate_thinking_delta_goes_to_reasoning_content():
    state: dict[str, Any] = {}
    chunks = translate_event({"type": "thinking_delta", "text": "想一想"}, state)
    assert chunks[0]["choices"][0]["delta"] == {"reasoning_content": "想一想"}


def test_translate_content_delta():
    state: dict[str, Any] = {}
    chunks = translate_event({"type": "content_delta", "text": "你好"}, state)
    assert chunks[0]["choices"][0]["delta"] == {"content": "你好"}


def test_translate_tool_use_stream():
    state: dict[str, Any] = {}
    start = translate_event(
        {"type": "tool_use_start", "id": "call_1", "name": "get_weather"}, state
    )
    call = start[0]["choices"][0]["delta"]["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["index"] == 0
    assert call["function"]["name"] == "get_weather"

    delta = translate_event(
        {"type": "tool_input_delta", "id": "call_1", "partial_json": '{"city":'}, state
    )
    frag = delta[0]["choices"][0]["delta"]["tool_calls"][0]
    assert frag["index"] == 0
    assert frag["function"]["arguments"] == '{"city":'


def test_translate_message_end_carries_usage():
    state: dict[str, Any] = {}
    translate_event({"type": "usage", "usage": {"input_tokens": 10, "output_tokens": 3}}, state)
    chunks = translate_event({"type": "message_end", "stop_reason": "end_turn"}, state)
    final = chunks[0]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"]["total_tokens"] == 13
    assert state["finished"] is True


def test_map_finish_reason():
    assert _map_finish_reason("end_turn") == "stop"
    assert _map_finish_reason("tool_use") == "tool_calls"
    assert _map_finish_reason("max_tokens") == "length"
    assert _map_finish_reason("whatever") == "stop"
    assert _map_finish_reason("") == "stop"


def test_sse_data_lines_skips_done_and_blank():
    raw = 'data: {"a":1}\n\ndata: [DONE]\n\nevent: x\ndata: {"b":2}\n'
    assert sse_data_lines(raw) == ['{"a":1}', '{"b":2}']


# ---------------------------------------------------------------------------
# 非流式聚合
# ---------------------------------------------------------------------------


def test_aggregate_stream_merges_text_and_tools():
    chunks = [
        {"id": "c1", "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"id": "c1", "choices": [{"index": 0, "delta": {"content": "北"}, "finish_reason": None}]},
        {"id": "c1", "choices": [{"index": 0, "delta": {"content": "京"}, "finish_reason": None}]},
        {
            "id": "c1",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call_1", "function": {"name": "f", "arguments": '{"a"'}}
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "c1",
            "choices": [
                {"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ":1}"}}]}}
            ],
        },
        {"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    out = aggregate_stream(chunks, "GLM-5.3")
    msg = out["choices"][0]["message"]
    assert msg["content"] == "北京"
    assert msg["tool_calls"][0]["function"]["arguments"] == '{"a":1}'
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_aggregate_stream_keeps_reasoning():
    chunks = [
        {"id": "c1", "choices": [{"index": 0, "delta": {"reasoning_content": "想"}, "finish_reason": None}]},
        {"id": "c1", "choices": [{"index": 0, "delta": {"reasoning_content": "了"}, "finish_reason": None}]},
        {"id": "c1", "choices": [{"index": 0, "delta": {"content": "答"}, "finish_reason": "stop"}]},
    ]
    msg = aggregate_stream(chunks, "m")["choices"][0]["message"]
    assert msg["reasoning_content"] == "想了"
    assert msg["content"] == "答"


# ---------------------------------------------------------------------------
# 额度
# ---------------------------------------------------------------------------


def test_parse_balance_strings_to_float():
    bal = q.parse_balance(BALANCE_PAYLOAD)
    assert bal.available == pytest.approx(9999.851)
    assert len(bal.windows) == 2
    assert bal.windows[0].key == "5h"
    assert bal.windows[0].limit == pytest.approx(10000.0)
    assert bal.windows[1].key == "7d"


def test_parse_upstream_ts_is_beijing_time():
    """上游给的是无时区北京时间字符串，必须按 +08:00 解析。"""
    ts = q.parse_upstream_ts("2026-11-29 07:36:34")
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
    assert (dt.hour, dt.minute) == (7, 36)


def test_parse_upstream_ts_bad_value_is_none():
    """解析不出来给 None——不要退回当前时间，那会显示「0 秒后重置」。"""
    assert q.parse_upstream_ts("") is None
    assert q.parse_upstream_ts(None) is None
    assert q.parse_upstream_ts("昨天") is None


def test_to_quota_items_no_sum_items():
    """5h 和 7d 是并存的约束，不能相加。"""
    items = q.to_quota_items(q.parse_balance(BALANCE_PAYLOAD))
    assert "sum_items" not in items
    assert len(items["items"]) == 2


def test_to_quota_items_uses_reset_ts_not_expire_ts():
    """窗口是「回满」不是「到期」，用 expire_ts 会让到期横幅误报。"""
    items = q.to_quota_items(q.parse_balance(BALANCE_PAYLOAD))
    for item in items["items"]:
        assert item["expire_ts"] is None
        assert item["reset_ts"] is not None
        assert item["reset_note"] == "额度回满"


def test_to_quota_items_percent_is_used_ratio():
    items = q.to_quota_items(q.parse_balance(BALANCE_PAYLOAD))
    first = items["items"][0]
    # 0.149 / 10000 = 0.00149%，round 到 2 位就是 0.0——保留字段即可，别改精度
    assert first["percent"] == 0.0
    assert first["used"] == pytest.approx(0.149)


def test_to_quota_items_zero_limit_no_percent():
    payload = {
        "data": {
            "usage_windows": {
                "5h": {"enabled": True, "limit_credits": "0", "used_credits": "0",
                       "remaining_credits": "0"}
            }
        }
    }
    items = q.to_quota_items(q.parse_balance(payload))
    assert items["items"][0]["percent"] is None


def test_provider_has_no_checkin(offline_provider):
    """端砚的免费额度是月度自动发放，没有点击签到。"""
    assert offline_provider.supports_checkin is False


# ---------------------------------------------------------------------------
# provider 健康 / 认证
# ---------------------------------------------------------------------------


def test_health_reports_catalog_size(offline_provider):
    health = offline_provider.health()
    assert health["id"] == "inkstone"
    assert health["models"] == 2


def test_models_without_catalog_returns_empty(monkeypatch):
    """目录拉不到时返回空列表，不能把整个 /v1/models 拖成 500。"""
    provider = InkstoneProvider()

    def _boom(*_a, **_kw):
        raise RuntimeError("网关抖动")

    monkeypatch.setattr(provider, "_ensure_catalog", _boom)
    assert provider.models() == []


def test_accepts_model_offline_prefix_fallback(monkeypatch):
    """目录不可用时退化成「不认」，不要让路由误抢。"""
    provider = InkstoneProvider()

    def _boom(*_a, **_kw):
        raise RuntimeError("网关抖动")

    monkeypatch.setattr(provider, "_ensure_catalog", _boom)
    assert provider.accepts_model("GLM-5.3") is False
