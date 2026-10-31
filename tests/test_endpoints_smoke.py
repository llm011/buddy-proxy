"""关键接口冒烟测试（完全离线，不访问远程 CodeBuddy 后端）。

覆盖：/health、/v1/models、/v1/chat/completions、/v1/responses、/v1/messages。
所有依赖远程 / 全局状态的调用都被 monkeypatch 掉，保证测试可离线、可重复运行。

运行：
    .venv/bin/python -m pytest tests/test_endpoints_smoke.py -v
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from buddy_proxy import __main__ as m
from buddy_proxy.core import state as st
from buddy_proxy import codebuddy_provider as cbp


# ---------------------------------------------------------------------------
# fixture：构造一个假的 ProxyState，并替换模块级 get_state()
# （拆分后 get_state/collect_upstream/stream_upstream 分别位于 state 与
#  codebuddy_provider 模块，monkeypatch 目标随之调整。）
# ---------------------------------------------------------------------------
def _make_state(client_mock=None):
    """构造一个最小可用的假 ProxyState（SimpleNamespace）。"""
    client = client_mock
    if client is None:
        client = mock.MagicMock()
        client.endpoint = "https://fake.endpoint.invalid"
        client.auth_headers.return_value = {"X-IDE-Fake": "1"}
        client.session = {"auth": {"accessToken": "test-token", "expiresAt": int((time.time() + 3600) * 1000)}}

    return SimpleNamespace(
        client=client,
        providers={},
        mock_dir=None,
        started_at=time.time(),
        enable_desensitize=False,
        enable_optimize_context=False,
        verbose_llm=False,
        write_log=mock.MagicMock(),
        ensure_auth=mock.MagicMock(),
        logger=mock.MagicMock(),
        json_logger=mock.MagicMock(),
        runtime_info={"app_version": "test", "system_version": "test", "python_version": "test", "machine": "test"},
    )


@pytest.fixture()
def proxy_state(monkeypatch):
    """为测试提供替换 get_state 的假 state。

    拆分后 get_state() 在 state 模块内读取模块级 proxy_state 变量，
    故直接替换 st.proxy_state 即可让所有模块（routes/codebuddy_provider
    等）拿到假 state。

    多账号改造后 forward 不再走 state.client 认证，而是读多账号 store
    （failover.available_accounts + creds.ensure_account_token）——测试环境
    CODEBUDDY_STATE_DIR 已被 conftest 隔离成空目录，直接 429「所有账号均在
    冷却中」。这里塞一个假账号（token 也 mock 掉，不碰真实凭据文件），并把
    冷却表清空——前一个测试触发的 429 冷却（模块级 tracker 跨测试存活）不能
    泄漏到下一个测试里。
    """
    state = _make_state()
    monkeypatch.setattr(st, "proxy_state", state)
    acct = cbp.creds.AccountRef(id="uid-test", uid="uid-test", nickname="tester",
                                priority=0, added_at=1)
    monkeypatch.setattr(cbp.failover, "list_accounts", lambda: [acct])
    cbp.failover._cooldowns.clear()
    monkeypatch.setattr(
        cbp.creds, "ensure_account_token",
        lambda aid, **kw: ("test-token", {
            "account_id": aid, "token": "test-token", "refresh_token": "rt",
            "uid": "uid-test", "nickname": "tester", "machine_id": "machine-test",
            "expires_at_ms": int((time.time() + 3600) * 1000), "source": "state",
        }))
    return state


@pytest.fixture()
def client(proxy_state):
    """FastAPI TestClient，指向真实的 app。"""
    return TestClient(m.app)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["authenticated"] is True
    assert body["token_valid"] is True
    assert isinstance(body["uptime_seconds"], int)


def test_health_unauthenticated(monkeypatch):
    """无 token 时 authenticated 应为 False。"""
    state = _make_state()
    state.client.session = {}  # 无 auth
    monkeypatch.setattr(st, "proxy_state", state)
    c = TestClient(m.app)
    body = c.get("/health").json()
    assert body["authenticated"] is False
    # expires 为 0 时按「不过期」处理，故 token_valid 为 True
    assert body["token_valid"] is True


# ---------------------------------------------------------------------------
# /v1/models（真实静态加载，不 mock 数据）
# ---------------------------------------------------------------------------
def test_models_shape_and_count(client):
    r = client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert len(body["data"]) >= 10
    assert len(body["models"]) == len(body["data"])


def test_models_has_required_ids(client):
    """用户要求的模型 id 必须都在静态配置里。"""
    required = {
        "glm-5.3", "glm-5.3-flash", "hy3", "hy4-preview",
        "minimax-m3", "kimi-k3", "kimi-k2.7",
        "kimi-k3.1-pro",
        "deepseek-v4-flash", "deepseek-v4-pro",
    }
    body = client.get("/v1/models").json()
    ids = {x["id"] for x in body["data"]}
    assert required <= ids, f"缺少模型: {required - ids}"


def test_models_credits_and_name(client):
    """data 和 models 都必须透出 name 与 credits，且与 config 一致。"""
    body = client.get("/v1/models").json()
    by_id = {(x["id"], x.get("owned_by")): x for x in body["data"]}

    # 同一 id 可能被多个 provider 声明（trae/traepat/zcode...），按 (id, owned_by) 取
    checks = {
        ("glm-5.3", "zhipu"): ("x0.69 credits", "GLM-5.3"),
        ("glm-5.3-flash", "zhipu"): ("x0.08 credits", "GLM-5.3-Flash"),
        ("hy4-preview", "tencent"): ("x0.29 credits", "Hy4 preview"),
        ("minimax-m3", "f"): ("x0.25 credits", "MiniMax-M3"),
        ("deepseek-v4-flash", "f"): ("x0.17 credits", "Deepseek-V4-Flash"),
        ("deepseek-v4-pro", "f"): ("x0.51 credits", "Deepseek-V4-Pro"),
        ("kimi-k2.7", "f"): ("x0.57 credits", "Kimi-K2.7-Code"),
        # Kimi-K3.1-Pro 两档倍率里的订阅优先档（1.56）；
        # 0.97 的 -lite 档已移除——上游无此模型，转发必 11102
        ("kimi-k3.1-pro", "moonshot"): ("x1.56 credits", "Kimi-K3.1-Pro"),
    }
    for key, (credit, name) in checks.items():
        entry = by_id.get(key)
        assert entry is not None, f"{key} 缺失"
        assert entry["name"] == name, f"{key} name={entry.get('name')!r}"
        assert entry["credits"] == credit, f"{key} credits={entry.get('credits')!r}"


def test_models_codex_metadata(client):
    """codex models 要带 context_window / max_context_window / credits。"""
    body = client.get("/v1/models").json()
    sample = next(x for x in body["models"] if x["id"] == "deepseek-v4-pro")
    assert sample.get("max_context_window") is not None
    assert sample.get("context_window") == sample.get("max_context_window")
    assert sample["credits"] == "x0.51 credits"


def test_models_advertise_image_capability(client):
    """标准 OpenAI `data` 数组必须透出图片能力，且与 codex 扩展数组同口径。

    回归背景：/v1/models 只把能力挂在 Codex 扩展 `models` 数组里，第三方
    客户端（agent/IDE）按标准字段解析时读不到，只能按模型名猜（关键词
    匹配 vision/gpt-4o/claude…），支持读图的模型会被判为纯文本、图片在
    客户端侧就被剥掉。这里锁死标准字段与 codex 字段一致。
    """
    body = client.get("/v1/models").json()
    by_id = {x["id"]: x for x in body["data"]}
    codex_by_id = {x["id"]: x for x in body["models"]}

    # 标准字段必须存在（哪怕是 False），否则客户端无法区分「不支持」与「没这信息」
    for mid in ("deepseek-v4-flash", "deepseek-v4.1-flash", "glm-5.3"):
        entry = by_id.get(mid)
        if entry is None:
            continue  # 配置里没这个模型就跳过，不把测试绑死在目录组成上
        assert "supports_images" in entry, f"{mid} 缺 supports_images"
        assert entry["input_modalities"] in (["text"], ["text", "image"]), f"{mid} modalities={entry['input_modalities']}"
        assert entry["input_modalities"] == (["text", "image"] if entry["supports_images"] else ["text"])

    # 两个数组口径一致：data 的 supports_images 必须与 codex 的 input_modalities 一致
    for mid, entry in by_id.items():
        codex = codex_by_id.get(mid)
        if codex is None or "input_modalities" not in codex:
            continue
        assert entry["input_modalities"] == codex["input_modalities"], f"{mid} 两个数组口径不一致"

    # 至少有一个模型声明了图片能力，且至少有一个没有——防止能力字段被整体写死
    flags = {e["supports_images"] for e in by_id.values()}
    assert flags == {True, False}, f"图片能力字段疑似写死: {flags}"


# ---------------------------------------------------------------------------
# /v1/chat/completions（非流式，monkeypatch collect_upstream）
# ---------------------------------------------------------------------------
def _fake_collected():
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1720872952,
        "model": "glm-5.3",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "你好，我是测试回复"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def test_chat_completions_nonstream(client, monkeypatch):
    async def fake_collect(*args, **kwargs):
        return _fake_collected()
    monkeypatch.setattr(cbp, "collect_upstream", fake_collect)

    r = client.post("/v1/chat/completions", json={
        "model": "glm-5.3",
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "你好，我是测试回复"
    assert body["model"] == "glm-5.3"


# ---------------------------------------------------------------------------
# /v1/responses（注意：该端点返回 SSE 事件流，即使非流式请求也如此）
# ---------------------------------------------------------------------------
def test_responses_nonstream(client, monkeypatch):
    """/v1/responses 走流式转发，返回 SSE 事件流。patch stream_upstream 模拟上游。"""
    async def fake_stream(url, headers, body, protocol, original):
        # 模拟上游已完成的响应，产生 responses 协议的 response.completed 事件
        yield (
            "event: response.completed\ndata: "
            + json.dumps({"type": "response.completed", "response": {"id": "resp_test", "status": "completed"}}, ensure_ascii=False)
            + "\n\n"
        ).encode()
        yield b"data: [DONE]\n\n"
    monkeypatch.setattr(cbp, "stream_upstream", fake_stream)

    r = client.post("/v1/responses", json={
        "model": "glm-5.3",
        "input": "hello",
    })
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    text = r.text
    # SSE 事件流被透传给客户端
    assert "response.completed" in text
    assert "[DONE]" in text


# ---------------------------------------------------------------------------
# /v1/messages（Anthropic /v1/messages）
# ---------------------------------------------------------------------------
def test_messages_nonstream(client, monkeypatch):
    async def fake_collect(*args, **kwargs):
        return _fake_collected()
    monkeypatch.setattr(cbp, "collect_upstream", fake_collect)

    r = client.post("/v1/messages", json={
        "model": "claude-4.0-sonnet",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 256,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["content"]  # 至少一个 content block


# ---------------------------------------------------------------------------
# 请求体非 UTF-8（GBK）也能解析
# ---------------------------------------------------------------------------
def test_safe_log_fields_redacts_request_data():
    """日志最后一道防线也不能让调用方意外写入敏感正文。"""
    secret = "bearer-secret-value"
    safe = st.safe_log_fields({"body": {"token": secret}, "status": 502})
    assert secret not in repr(safe)
    assert "body" not in safe
    assert safe["body_bytes"] > 0
    assert safe["status"] == 502
    assert st.safe_log_fields({
        "content_length": 12, "content_sha256": "abc", "message_count": 2,
    }) == {
        "content_length": 12,
        "content_sha256": "abc",
        "message_count": 2,
    }


def test_client_request_log_never_contains_api_key(client, proxy_state, monkeypatch):
    """客户端日志只标记是否携带 key，绝不记录 key 或其片段。"""
    async def fake_collect(*args, **kwargs):
        return _fake_collected()
    monkeypatch.setattr(cbp, "collect_upstream", fake_collect)
    secret = "super-secret-api-key-value"
    r = client.post("/v1/chat/completions", json={
        "model": "glm-5.3", "messages": [{"role": "user", "content": "hello"}],
    }, headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 200
    calls = proxy_state.write_log.call_args_list
    assert calls
    rendered = repr(calls)
    assert secret not in rendered
    assert secret[:4] not in rendered
    assert any(call.args[0] == "client_request_summary" and call.kwargs["auth_present"]
               for call in calls)
    # auth_present 字段名必须绕开 safe_log_fields 的敏感词表——否则布尔值会被
    # 当正文哈希成 *_bytes/*_sha256，丢失「是否带鉴权」的可观测性（PR #37 评论）。
    safe = st.safe_log_fields({"auth_present": True})
    assert safe == {"auth_present": True}


def test_tool_choice_never_reaches_upstream_as_object(client, monkeypatch):
    """tool_choice 必须归一化成 string 再发上游，任何 object 形态都会 400。

    上游 CodeBuddy 后端是 Go 的 ``Request.tool_choice string``，收到 object 会回
    ``cannot unmarshal object into ... of type string``（错误码 11101）。实测
    11101 在 #35 之后仍在复现——当时只覆盖了 OpenAI 的
    ``{"type":"function","function":{"name":...}}``，而 Anthropic 协议的
    ``{"type":"any"|"auto"|"none"}`` 会原样漏过去（Claude Code 发
    ``{"type":"any"}``）。这里对每种形态断言发上游的都是字符串。
    """
    captured = {}

    async def fake_collect(_url, _headers, body, _protocol):
        captured.update(body)
        return _fake_collected()

    monkeypatch.setattr(cbp, "collect_upstream", fake_collect)

    cases = [
        {"type": "any"},                                   # Anthropic：必须调工具
        {"type": "auto"},                                  # Anthropic：自动
        {"type": "none"},
        {"type": "tool", "name": "Bash"},                  # Anthropic 原生指定
        {"type": "function", "function": {"name": "Bash"}},  # OpenAI 指定
        {"type": "function"},                              # OpenAI 缺 name
        "any",
        "auto",
    ]
    for tc in cases:
        captured.clear()
        r = client.post("/v1/chat/completions", json={
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hello"}],
            "tool_choice": tc,
        })
        assert r.status_code == 200, (tc, r.text)
        sent = captured.get("tool_choice")
        assert not isinstance(sent, dict), f"tool_choice={tc!r} 仍以 object 发往上游: {sent!r}"
        assert sent in ("auto", "none", "required", "Bash"), (tc, sent)


def test_tool_choice_any_maps_to_required(client, monkeypatch):
    """语义映射：Anthropic 的 any（必须调用工具）→ 上游 required。"""
    captured = {}

    async def fake_collect(_url, _headers, body, _protocol):
        captured.update(body)
        return _fake_collected()

    monkeypatch.setattr(cbp, "collect_upstream", fake_collect)
    r = client.post("/v1/chat/completions", json={
        "model": "glm-5.3",
        "messages": [{"role": "user", "content": "hello"}],
        "tool_choice": {"type": "any"},
    })
    assert r.status_code == 200
    assert captured["tool_choice"] == "required"

    # 指定具体工具时传函数名本身
    captured.clear()
    r = client.post("/v1/chat/completions", json={
        "model": "glm-5.3",
        "messages": [{"role": "user", "content": "hello"}],
        "tool_choice": {"type": "tool", "name": "Bash"},
    })
    assert r.status_code == 200
    assert captured["tool_choice"] == "Bash"


def test_gbk_request_body(client, monkeypatch):
    """parse_request_body 要能处理 GBK 编码的请求体，不应 500。"""
    async def fake_collect(*args, **kwargs):
        return _fake_collected()
    monkeypatch.setattr(cbp, "collect_upstream", fake_collect)

    gbk_payload = (
        '{"model":"glm-5.3","messages":[{"role":"user","content":"\u4f60\u597d"}]}'
    ).encode("gbk")

    r = client.post(
        "/v1/chat/completions",
        content=gbk_payload,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200
