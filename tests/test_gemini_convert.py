"""gemini 通道单元测试：转换器、指纹、凭证状态机、模型表加载。

不发真实网络请求；上游交互（onboarding/生成）靠登录后的冒烟命令覆盖
（python -m buddy_proxy.gemini.provider）。
"""

from __future__ import annotations

import json


from buddy_proxy.gemini.convert import (
    chat_to_gemini_request,
    clean_json_schema_for_gemini,
    gemini_response_to_chat,
    sanitize_function_name,
)
from buddy_proxy.gemini.fingerprint import auth_headers, user_agent


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------

def test_user_agent_matches_real_cli_shape():
    ua = user_agent("gemini-2.5-flash")
    # 真 CLI 模板 + auth 库追加后缀
    assert ua.startswith("GeminiCLI/0.33.1/gemini-2.5-flash (")
    assert ua.endswith("google-api-nodejs-client/9.15.1")
    # 真 CLI 没有 "; terminal"（插件加的，指纹错配）
    assert "terminal" not in ua


def test_auth_headers_no_accept_on_non_stream():
    headers = auth_headers("tok", "gemini-2.5-flash", stream=False)
    assert "Accept" not in headers  # 真 CLI undici 默认，不带
    assert headers["x-goog-api-client"].startswith("gl-node/")
    assert "google-genai-sdk" not in headers["x-goog-api-client"]  # OAuth 路径不带 SDK 头


def test_auth_headers_stream_accept():
    headers = auth_headers("tok", "m", stream=True)
    assert headers["Accept"] == "text/event-stream"


# ---------------------------------------------------------------------------
# 请求转换
# ---------------------------------------------------------------------------

def _basic_body(**over):
    body = {
        "model": "gemini-2.5-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(over)
    return body


def test_request_wrapper_shape():
    req = chat_to_gemini_request(_basic_body(), project_id="p1",
                                 model="gemini-2.5-flash", stream=False)
    assert req["model"] == "models/gemini-2.5-flash"
    assert req["project"] == "p1"
    assert req["user_prompt_id"]  # 真 CLI 每次随机
    assert req["request"]["session_id"]  # 真 CLI 会话 UUID
    # 真 CLI 不发 safetySettings（插件注入是指纹错配）
    assert "safetySettings" not in req["request"]


def test_system_message_to_system_instruction():
    body = _basic_body(messages=[
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "hi"},
    ])
    req = chat_to_gemini_request(body, project_id="p", model="m", stream=False)
    assert req["request"]["systemInstruction"] == {"parts": [{"text": "be nice"}]}
    assert len(req["request"]["contents"]) == 1


def test_tool_round_trip_shape():
    body = _basic_body(messages=[
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "get_weather", "arguments": '{"city": "SF"}'}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "name": "get_weather",
         "content": '{"temp": 20}'},
    ], tools=[{"type": "function", "function": {
        "name": "get_weather", "description": "d",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}],
    )
    req = chat_to_gemini_request(body, project_id="p", model="m", stream=False)
    contents = req["request"]["contents"]
    assert contents[1]["parts"][0]["functionCall"]["name"] == "get_weather"
    # fc.id 与 fr.id 成对回传（claude/gpt-oss 硬要求，缺 → 400；gemini 同收）
    assert contents[1]["parts"][0]["functionCall"]["id"] == "c1"
    resp_content = contents[2]
    assert resp_content["role"] == "user"
    fr = resp_content["parts"][0]["functionResponse"]
    assert fr["name"] == "get_weather"
    assert fr["id"] == "c1"
    assert fr["response"]["content"] == {"temp": 20}
    decls = req["request"]["tools"][0]["function_declarations"]
    assert decls[0]["name"] == "get_weather"


def test_schema_cleaning():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": ["string", "null"], "minLength": 1, "default": "x"},
            "b": {"const": 5},
        },
        "required": ["a", "b", "ghost"],
        "additionalProperties": False,
    }
    out = clean_json_schema_for_gemini(schema)
    assert out["properties"]["a"]["type"] == "string"
    assert "minLength" not in out["properties"]["a"]
    assert "default" not in out["properties"]["a"]
    assert out["properties"]["b"]["enum"] == [5]
    assert set(out["required"]) == {"a", "b"}  # ghost 不在 properties 里，剔掉
    assert "additionalProperties" not in out


def test_sanitize_function_name():
    assert sanitize_function_name("my tool#1") == "my_tool_1"
    assert sanitize_function_name("9lead") == "_9lead"
    assert len(sanitize_function_name("x" * 100)) == 64


def test_generation_config_and_effort():
    body = _basic_body(temperature=0.5, max_tokens=99, reasoning_effort="low")
    req = chat_to_gemini_request(body, project_id="p", model="m", stream=False)
    gc = req["request"]["generationConfig"]
    assert gc["temperature"] == 0.5
    assert gc["maxOutputTokens"] == 99
    assert gc["thinkingConfig"] == {"thinkingBudget": 1024}


# ---------------------------------------------------------------------------
# 响应转换
# ---------------------------------------------------------------------------

def test_response_text():
    payload = {"response": {"candidates": [{
        "content": {"parts": [{"text": "pong"}]},
        "finishReason": "STOP",
    }], "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1,
                          "totalTokenCount": 4}}}
    chat = gemini_response_to_chat(payload, model="m")
    choice = chat["choices"][0]
    assert choice["message"]["content"] == "pong"
    assert choice["finish_reason"] == "stop"
    assert chat["usage"] == {"prompt_tokens": 3, "completion_tokens": 1,
                             "total_tokens": 4,
                             "prompt_tokens_details": {"cached_tokens": 0}}


def test_usage_from_metadata_carries_cache():
    from buddy_proxy.gemini.convert import usage_from_metadata

    # promptTokenCount 已含缓存命中（OpenAI 口径），cached 只进 details 不扣减
    usage = usage_from_metadata({"promptTokenCount": 100,
                                 "candidatesTokenCount": 7,
                                 "totalTokenCount": 107,
                                 "cachedContentTokenCount": 96})
    assert usage == {"prompt_tokens": 100, "completion_tokens": 7,
                     "total_tokens": 107,
                     "prompt_tokens_details": {"cached_tokens": 96}}
    # 缺缓存字段时补 0（与 OpenAI 真实响应形状一致）
    assert usage_from_metadata({})["prompt_tokens_details"] == {"cached_tokens": 0}


def test_response_cache_reaches_anthropic_exit():
    # 非流式 Anthropic 出口：prompt_tokens_details.cached_tokens →
    # cache_read_input_tokens + input_tokens 扣减（口径对齐，防双计）
    from buddy_proxy.protocols.anthropic_adapter import (
        chat_completion_to_anthropic_message,
    )

    payload = {"response": {"candidates": [{
        "content": {"parts": [{"text": "pong"}]},
        "finishReason": "STOP",
    }], "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 7,
                          "totalTokenCount": 107,
                          "cachedContentTokenCount": 96}}}
    chat = gemini_response_to_chat(payload, model="m")
    msg = chat_completion_to_anthropic_message(chat)
    assert msg["usage"] == {"input_tokens": 4, "output_tokens": 7,
                            "cache_read_input_tokens": 96}


def test_stream_final_chunk_carries_cache():
    import asyncio

    import httpx

    from buddy_proxy.gemini.provider import _to_openai_stream

    inner = {"candidates": [{
        "content": {"parts": [{"text": "pong"}]},
        "finishReason": "STOP",
    }], "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 7,
                          "totalTokenCount": 107,
                          "cachedContentTokenCount": 96}}
    line = "data: " + json.dumps({"response": inner}) + "\n\n"
    resp = httpx.Response(
        200, content=line.encode(),
        headers={"content-type": "text/event-stream"},
        request=httpx.Request("POST", "https://x/"),
    )

    async def _go():
        out = []
        async for piece in _to_openai_stream(resp, "m"):
            out.append(piece)
        return "".join(out)

    text = asyncio.run(_go())
    usage_lines = [l for l in text.splitlines()
                   if l.startswith("data:") and '"usage"' in l]
    assert usage_lines, text
    usage = json.loads(usage_lines[-1][5:])["usage"]
    assert usage["prompt_tokens"] == 100
    assert usage["prompt_tokens_details"] == {"cached_tokens": 96}


def test_response_tool_call_with_signature():
    payload = {"response": {"candidates": [{
        "content": {"parts": [{"functionCall": {"name": "f", "args": {"x": 1}},
                               "thoughtSignature": "sig123"}]},
        "finishReason": "STOP",
    }]}}
    chat = gemini_response_to_chat(payload, model="m")
    tc = chat["choices"][0]["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "f"
    assert json.loads(tc["function"]["arguments"]) == {"x": 1}
    assert tc["gemini_thought_signature"] == "sig123"


def test_response_thought_split():
    payload = {"response": {"candidates": [{
        "content": {"parts": [
            {"text": "thinking...", "thought": True},
            {"text": "answer"},
        ]},
        "finishReason": "STOP",
    }]}}
    chat = gemini_response_to_chat(payload, model="m")
    msg = chat["choices"][0]["message"]
    assert msg["content"] == "answer"
    assert msg["reasoning_content"] == "thinking..."


def test_response_inline_image():
    # 生图模型（gemini-3.1-flash-image）返回 inlineData，需转成 message.images[]
    payload = {"response": {"candidates": [{
        "content": {"parts": [
            {"text": "Here is the image."},
            {"inlineData": {"mimeType": "image/png", "data": "QUJD"}},
        ]},
        "finishReason": "STOP",
    }]}}
    msg = gemini_response_to_chat(payload, model="m")["choices"][0]["message"]
    assert msg["content"] == "Here is the image."
    assert msg["images"] == [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}
    ]


def test_stream_carries_inline_image():
    import asyncio

    import httpx

    from buddy_proxy.gemini.provider import _to_openai_stream

    inner = {"candidates": [{
        "content": {"parts": [
            {"text": "here"},
            {"inlineData": {"mimeType": "image/webp", "data": "QUJD"}},
        ]},
        "finishReason": "STOP",
    }]}
    line = "data: " + json.dumps({"response": inner}) + "\n\n"
    resp = httpx.Response(
        200, content=line.encode(),
        headers={"content-type": "text/event-stream"},
        request=httpx.Request("POST", "https://x/"),
    )

    async def _go():
        out = []
        async for piece in _to_openai_stream(resp, "m"):
            out.append(piece)
        return "".join(out)

    text = asyncio.run(_go())
    deltas = [json.loads(l[5:])["choices"][0]["delta"]
              for l in text.splitlines()
              if l.startswith("data:") and l != "data: [DONE]"]
    images = [img for d in deltas for img in (d.get("images") or [])]
    assert images == [{"type": "image_url",
                       "image_url": {"url": "data:image/webp;base64,QUJD"}}]


def test_inline_image_rejects_malformed_base64():
    from buddy_proxy.gemini.convert import inline_image_part

    ok = {"inlineData": {"mimeType": "image/png", "data": "QUJD"}}
    assert inline_image_part(ok)
    # 长度非 4 的倍数（分片）/ 非法字符：宁缺勿坏
    assert inline_image_part({"inlineData": {"data": "QUJ"}}) is None
    assert inline_image_part({"inlineData": {"data": "QU JD"}}) is None
    assert inline_image_part({"inlineData": {"data": ""}}) is None
    assert inline_image_part({}) is None


def test_response_without_image_has_no_images_key():
    payload = {"response": {"candidates": [{
        "content": {"parts": [{"text": "pong"}]},
        "finishReason": "STOP",
    }]}}
    assert "images" not in gemini_response_to_chat(payload, model="m")["choices"][0]["message"]


# ---------------------------------------------------------------------------
# 模型表 / 凭证
# ---------------------------------------------------------------------------

def test_models_load_from_json():
    from buddy_proxy.gemini.provider import DEFAULT_MODELS

    assert "gemini-2.5-flash" in DEFAULT_MODELS
    assert all(isinstance(v, str) for v in DEFAULT_MODELS.values())


def test_cred_roundtrip(tmp_path, monkeypatch):
    from buddy_proxy.gemini import credentials as creds

    monkeypatch.setenv("GEMINI_OAUTH_JSON", str(tmp_path / "g.json"))
    assert creds.load_cred() is None
    assert not creds.has_cred()

    cred = {"access_token": "a", "refresh_token": "r",
            "expiry": "2099-01-01T00:00:00+00:00"}
    creds.save_cred(cred)
    loaded = creds.load_cred()
    assert loaded["refresh_token"] == "r"
    assert creds.access_token_valid(loaded)  # 未到期
    assert creds.ensure_access_token(loaded) == "a"  # 不触发刷新

    import os

    assert (os.stat(tmp_path / "g.json").st_mode & 0o777) == 0o600


def test_cred_expired_needs_refresh(tmp_path, monkeypatch):
    from buddy_proxy.gemini import credentials as creds

    monkeypatch.setenv("GEMINI_OAUTH_JSON", str(tmp_path / "g.json"))
    cred = {"access_token": "a", "refresh_token": "r",
            "expiry": "2000-01-01T00:00:00+00:00"}
    creds.save_cred(cred)
    loaded = creds.load_cred()
    assert not creds.access_token_valid(loaded)

    called = {}
    monkeypatch.setattr(creds, "_token_request", lambda data, timeout=30.0: (
        called.update(grant=data["grant_type"]) or
        {"access_token": "new", "expires_in": 3600}
    ))
    assert creds.ensure_access_token(loaded) == "new"
    assert called["grant"] == "refresh_token"
    assert loaded["access_token"] == "new"  # 就地更新


# ---------------------------------------------------------------------------
# onboarding 诊断（真实失败：onboardUser done 但项目字段空/缺失）
# ---------------------------------------------------------------------------

def test_project_from_value_shapes():
    from buddy_proxy.gemini.setup import _project_from_value

    assert _project_from_value("genai-abc") == "genai-abc"
    assert _project_from_value({"id": "genai-abc"}) == "genai-abc"
    assert _project_from_value({"projectId": "p2"}) == "p2"
    assert _project_from_value({"id": {"id": "nested"}}) == "nested"
    assert _project_from_value({}) == ""
    assert _project_from_value(None) == ""


def test_missing_project_error_reports_ineligible_reason():
    from buddy_proxy.gemini.setup import SetupError, _missing_project_error

    load_res = {
        "allowedTiers": [{"id": "free-tier", "isDefault": True}],
        "ineligibleTiers": [
            {"tierId": "free-tier", "reasonCode": "UNSUPPORTED_LOCATION",
             "reasonMessage": "not available in your location"}
        ],
    }
    err = _missing_project_error(load_res, {"done": True, "response": {}})
    assert isinstance(err, SetupError)
    text = str(err)
    assert "UNSUPPORTED_LOCATION" in text   # 真实原因带出来
    assert "not available in your location" in text
    assert "allowedTiers=free-tier" in text  # 响应摘要


def test_missing_project_error_without_reasons_keeps_base():
    from buddy_proxy.gemini.setup import _missing_project_error

    err = _missing_project_error({"allowedTiers": []}, {"done": True, "response": {}})
    assert "onboarding 完成但未返回项目 ID" in str(err)


def test_onboard_lro_error_is_raised(monkeypatch):
    """LRO done=true + error 时要把上游错误抛出，不落入缺项目的兜底。"""
    from buddy_proxy.gemini import setup as st

    monkeypatch.setattr(st, "_code_assist_post", lambda *a, **k: {
        "name": "operations/x", "done": True,
        "error": {"code": 7, "message": "PERMISSION_DENIED: tier not available"},
    })
    try:
        st.setup_code_assist("fake-token")
    except st.SetupError as exc:
        assert "PERMISSION_DENIED" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("预期 SetupError")


def test_resume_onboarding_uses_saved_token(tmp_path, monkeypatch):
    """OAuth 已成功、onboarding 失败的中间态：重试不该重走浏览器。"""
    from buddy_proxy.gemini import credentials as creds
    from buddy_proxy.gemini import login as gm_login

    monkeypatch.setenv("GEMINI_OAUTH_JSON", str(tmp_path / "g.json"))
    # 这条测试曾是真实污染源：resume_onboarding 会把凭证回写 ~/.gemini，
    # 没有 GEMINI_CLI_HOME 隔离时写的是真目录（假邮箱 a@b.c 进了
    # google_accounts.json）。conftest 现已全局隔离，这里显式再写一道
    # 是给读者留痕——任何绕过 conftest 直接跑本文件的方式也安全。
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "cli-home"))
    creds.save_cred({"access_token": "tok", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00", "email": "a@b.c"})

    monkeypatch.setattr(gm_login, "setup_code_assist",
                        lambda token, project_id="": {"project_id": "genai-x",
                                                      "tier": "free-tier", "tier_name": "Free"})
    cred = gm_login.resume_onboarding()
    assert cred["project_id"] == "genai-x"
    assert creds.load_cred()["project_id"] == "genai-x"  # 已落盘
