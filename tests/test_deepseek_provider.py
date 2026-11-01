"""DeepSeek 官方通道单测 —— 全离线，不访问上游。

锁定的契约（2026-11-01 接入时以真实 key 实测确认，见各测试注释）：

1. **官方只认两个模型名**：``deepseek-flash`` / ``deepseek-v4-pro``。CodeBuddy
   目录里那个 ``deepseek-v4.1-flash`` 打官方是 **400**（不是自动归一），所以
   本地必须替它兜一层别名，否则用户照着 /v1/models 敲必然吃 400。
2. **三协议原生直通**：openai/responses/anthropic 各有自己的上游路径，其中
   anthropic 挂在 ``/anthropic`` 前缀下。**转发必须用 original**（协议转换前
   的请求体）——拿 routes 转好的 chat 形态去打 responses/anthropic 上游会 400。
3. **裸名撞车让位**：``deepseek-v4-pro`` 与本地静态表同名，route_priority=50
   低于 CodeBuddy 的 100，加 ``--deepseek`` 不得把既有流量静默改道到计费官方
   API；要显式走官方就得写 ``deepseek/deepseek-v4-pro``。
4. **非流式响应必须先读**：``send(stream=True)`` 后直接 ``resp.json()`` 会抛
   ``ResponseNotRead``——这是接入时真踩到的 bug，用测试钉住。
5. **余额是预付费钱包**：只有 ``remaining``、没有分母，且要不是 0。

运行：
    .venv/bin/python -m pytest tests/test_deepseek_provider.py -v
"""
from __future__ import annotations

import asyncio
import json
import pathlib

import httpx
import pytest

from buddy_proxy.providers import deepseek as ds_mod
from buddy_proxy.providers.deepseek import (
    DeepSeekProvider,
    MODEL_ALIASES,
    resolve_api_key,
    secret_file_path,
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """隔离状态目录与 key 环境变量，绝不读真实用户 key。"""
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)


def _provider(handler, *, key: str = "sk-test") -> DeepSeekProvider:
    """构造 provider 并把它的 httpx client 换成 MockTransport。"""
    p = DeepSeekProvider(api_key=key, base_url="https://api.deepseek.test")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p


_MODELS_PAYLOAD = {
    "object": "list",
    "data": [
        {"id": "deepseek-flash", "object": "model", "owned_by": "deepseek",
         "name": "DeepSeek-V4.1-Flash", "context_window": 1048576,
         "max_output_tokens": 393216, "input_modalities": ["text", "image"],
         "output_modalities": ["text"],
         "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"}},
        {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek",
         "name": "DeepSeek-V4-Pro", "context_window": 1048576,
         "max_output_tokens": 393216, "input_modalities": ["text"],
         "output_modalities": ["text"], "effort": {"supported_levels": ["high"]}},
    ],
}

_BALANCE_PAYLOAD = {
    "is_available": True,
    "balance_infos": [{"currency": "CNY", "total_balance": "34.92",
                       "granted_balance": "0.00", "topped_up_balance": "34.92"}],
}

_CHAT_JSON = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "deepseek-flash",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}


# ---------------------------------------------------------------------------
# 凭据 / 基址
# ---------------------------------------------------------------------------

def test_api_key_env_wins_over_file(tmp_path, monkeypatch):
    """env 优先于 key 文件（与 glm/ark 同约定）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    secret_file_path().parent.mkdir(parents=True, exist_ok=True)
    secret_file_path().write_text("from-file")
    assert resolve_api_key() == "from-env"


def test_api_key_reads_file_with_name_value(tmp_path):
    """key 文件支持 ``name=value`` 与裸值两种写法（复用 zcode 的读法）。"""
    secret_file_path().parent.mkdir(parents=True, exist_ok=True)
    secret_file_path().write_text("DEEPSEEK_API_KEY=sk-from-file\n")
    assert resolve_api_key() == "sk-from-file"
    secret_file_path().write_text("sk-bare\n")
    assert resolve_api_key() == "sk-bare"


def test_base_url_override(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://my-gateway.test/")
    assert ds_mod.resolve_base_url() == "https://my-gateway.test"
    monkeypatch.delenv("DEEPSEEK_BASE_URL")
    assert ds_mod.resolve_base_url() == ds_mod.DEFAULT_BASE_URL


def test_missing_key_raises_actionable_401():
    p = DeepSeekProvider(api_key="", base_url="https://api.deepseek.test")
    with pytest.raises(Exception) as ei:
        p.ensure_auth()
    detail = ei.value.detail["error"]
    assert ei.value.status_code == 401
    # 提示必须同时给出 env 与文件两条路，否则用户不知道 key 往哪放。
    assert "DEEPSEEK_API_KEY" in detail["message"]
    assert "deepseek_api_key" in detail["message"]


# ---------------------------------------------------------------------------
# 模型目录
# ---------------------------------------------------------------------------

def test_default_models_only_official_names():
    """兜底表里不许出现 ``deepseek-v4.1-flash``——官方对它回 400。"""
    p = DeepSeekProvider(api_key="k", base_url="https://api.deepseek.test")
    ids = {m["id"] for m in p.models()}
    assert ids == {"deepseek-flash", "deepseek-v4-pro"}
    assert "deepseek-v4.1-flash" not in ids


def test_refresh_models_keeps_capability_metadata():
    """刷新要带回 context_window / images —— /v1/models 的图片能力靠它。"""
    p = _provider(lambda r: httpx.Response(200, json=_MODELS_PAYLOAD))
    models = asyncio.run(p.refresh_models())
    by_id = {m["id"]: m for m in models}
    assert set(by_id) == {"deepseek-flash", "deepseek-v4-pro"}
    assert by_id["deepseek-flash"]["context_window"] == 1048576
    assert by_id["deepseek-flash"]["images"] is True
    assert by_id["deepseek-flash"]["input_modalities"] == ["text", "image"]
    # v4-pro 官方不给图片，别跟着 flash 一起报 images
    assert by_id["deepseek-v4-pro"]["images"] is False
    assert by_id["deepseek-v4-pro"]["input_modalities"] == ["text"]


def test_refresh_models_failure_keeps_old_catalog():
    """上游挂了必须保留旧目录，不能把表刷成空的。"""
    def handler(r):
        return httpx.Response(500, text="boom")

    p = _provider(handler)
    before = [m["id"] for m in p.models()]
    with pytest.raises(RuntimeError):
        asyncio.run(p.refresh_models())
    assert [m["id"] for m in p.models()] == before


# ---------------------------------------------------------------------------
# 别名
# ---------------------------------------------------------------------------

def test_alias_maps_legacy_names_to_official():
    """旧的公开写法必须归一到官方真名，而不是原样打过去吃 400。"""
    assert MODEL_ALIASES["deepseek-v4.1-flash"] == "deepseek-flash"
    assert MODEL_ALIASES["deepseek-chat"] == "deepseek-flash"
    p = DeepSeekProvider(api_key="k", base_url="https://api.deepseek.test")
    assert p.resolve_model("deepseek-v4.1-flash") == "deepseek-flash"
    assert p.resolve_model("deepseek-flash") == "deepseek-flash"


# ---------------------------------------------------------------------------
# 转发：三协议路径 + 模型名归一 + 非流式必须先读
# ---------------------------------------------------------------------------

def _recording_provider(monkeypatch, *, stream: bool = False):
    """记录发往上游的 (path, body, headers)，返回一个普通 JSON 响应。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_CHAT_JSON)

    p = _provider(handler)
    return p, seen


def test_openai_forward_uses_body_and_official_path(monkeypatch):
    p, seen = _recording_provider(monkeypatch)
    resp = asyncio.run(p.forward(
        {"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "hi"}],
         "stream": False},
        "openai"))
    assert resp.status_code == 200
    assert seen["path"] == "/v1/chat/completions"
    # 别名在发上游前必须归一：官方不认 deepseek-v4.1-flash
    assert seen["body"]["model"] == "deepseek-flash"
    assert seen["auth"] == "Bearer sk-test"


def test_responses_forward_uses_original_body(monkeypatch):
    """responses 必须发 original（Responses 形状），不是转换后的 chat。"""
    p, seen = _recording_provider(monkeypatch)
    original = {"model": "deepseek-flash", "input": "hi", "max_output_tokens": 16,
                "stream": False}
    asyncio.run(p.forward({"model": "deepseek-flash", "messages": [], "stream": False},
                          "responses", original=original))
    assert seen["path"] == "/v1/responses"
    assert "input" in seen["body"], "发出去的应是 Responses 形状（input）"
    assert "messages" not in seen["body"]


def test_anthropic_forward_uses_anthropic_prefix(monkeypatch):
    """anthropic 的上游路径带 /anthropic 前缀，且请求体用 original。"""
    p, seen = _recording_provider(monkeypatch)
    original = {"model": "deepseek-flash", "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}], "stream": False}
    asyncio.run(p.forward({"model": "deepseek-flash", "messages": [], "stream": False},
                          "anthropic", original=original))
    assert seen["path"] == "/anthropic/v1/messages"
    assert seen["body"]["max_tokens"] == 16


def test_nonstream_response_is_read_before_json(monkeypatch):
    """非流式必须显式 aread()，否则 httpx 抛 ResponseNotRead（接入时真踩到）。

    这一条不是「顺手加的」：``client.send(stream=True)`` 之后 body 是未读的，
    直接 ``resp.json()`` 会被 httpx 拒，整个非流式路径全 502。
    """
    p, _ = _recording_provider(monkeypatch)
    resp = asyncio.run(p.forward(
        {"model": "deepseek-flash", "messages": [{"role": "user", "content": "hi"}],
         "stream": False}, "openai"))
    assert resp.status_code == 200
    assert json.loads(resp.body)["model"] == "deepseek-flash"


def test_streaming_passes_through_sse(monkeypatch):
    sse = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
           b'data: [DONE]\n\n')

    def handler(r):
        return httpx.Response(200, content=sse,
                              headers={"content-type": "text/event-stream"})

    p = _provider(handler)

    async def run():
        resp = await p.forward(
            {"model": "deepseek-flash", "messages": [{"role": "user", "content": "hi"}],
             "stream": True}, "openai")
        chunks = [c async for c in resp.body_iterator]
        return resp, b"".join(chunks)

    resp, body = asyncio.run(run())
    assert resp.media_type == "text/event-stream"
    assert body == sse, "SSE 必须原样透传，不做本地转换"


def test_upstream_error_is_passed_through(monkeypatch):
    """上游 4xx 原样透传（含官方 OpenAI 形状错误体），不吞成 502。"""
    err = {"error": {"message": "Authentication Fails", "type": "authentication_error",
                     "param": None, "code": "invalid_request_error"}}

    def handler(r):
        return httpx.Response(401, json=err)

    p = _provider(handler)
    resp = asyncio.run(p.forward(
        {"model": "deepseek-flash", "messages": [{"role": "user", "content": "hi"}],
         "stream": False}, "openai"))
    assert resp.status_code == 401
    assert json.loads(resp.body)["error"]["type"] == "authentication_error"


def test_transport_error_becomes_502(monkeypatch):
    def handler(r):
        raise httpx.ConnectError("boom")

    p = _provider(handler)
    with pytest.raises(Exception) as ei:
        asyncio.run(p.forward(
            {"model": "deepseek-flash", "messages": [{"role": "user", "content": "hi"}],
             "stream": False}, "openai"))
    assert ei.value.status_code == 502


# ---------------------------------------------------------------------------
# 余额
# ---------------------------------------------------------------------------

def test_quota_reports_balance_without_fake_total():
    """余额只报 ``remaining``、不编 ``total``（编了「已用 N%」就是假的）。"""
    q = ds_mod.parse_balance(_BALANCE_PAYLOAD)
    items = q["items"]
    assert len(items) == 1
    item = items[0]
    assert item["remaining"] == 34.92
    assert "total" not in item, "预付费钱包没有总量分母，不该编一个出来"
    assert item["unit"] == "currency", "currency 量纲让占比告警不会误报"
    assert item["label"] == "账户余额 (CNY)"
    assert q["level"] is None


def test_quota_flags_unavailable_balance():
    payload = {"is_available": False,
               "balance_infos": [{"currency": "CNY", "total_balance": "0.00"}]}
    q = ds_mod.parse_balance(payload)
    assert q["level"] == "余额不足"
    assert q["items"][0]["remaining"] == 0.0


def test_quota_handles_missing_balance_numbers():
    """上游没给数字时必须给说明条，不能把未知当零。"""
    q = ds_mod.parse_balance({"is_available": True,
                              "balance_infos": [{"currency": "USD"}]})
    assert q["items"][0]["remaining"] == "余额未知（USD）"
    assert "total" not in q["items"][0]


def test_quota_handles_empty_balance_infos():
    q = ds_mod.parse_balance({"is_available": True, "balance_infos": []})
    assert q["items"][0]["remaining"] == "上游未返回余额信息"


def test_quota_raises_without_key():
    p = DeepSeekProvider(api_key="", base_url="https://api.deepseek.test")
    with pytest.raises(RuntimeError):
        p.quota()


# ---------------------------------------------------------------------------
# 路由名分：裸名撞车让位（写死的安全边界，别静默花钱）
# ---------------------------------------------------------------------------

def test_route_priority_lower_than_codebuddy():
    """route_priority 必须低于 CodeBuddy 的 100。

    否则加 ``--deepseek`` 会把既有的 ``deepseek-v4-pro`` 请求静默改道到**计费
    的官方 API** 上——用户没点名，钱却花了。要显式走官方就写
    ``deepseek/deepseek-v4-pro``。
    """
    from buddy_proxy.codebuddy_provider.forward import _CODEBUDDY_ROUTE_PRIORITY
    assert DeepSeekProvider.route_priority < _CODEBUDDY_ROUTE_PRIORITY


def test_bare_name_collides_and_yields_to_static_table():
    """``deepseek-v4-pro`` 在本地静态表里，裸名必须仍归 CodeBuddy。"""
    from buddy_proxy.codebuddy_provider.forward import (
        _CODEBUDDY_ROUTE_PRIORITY, _codebuddy_static_ids,
    )
    assert "deepseek-v4-pro" in _codebuddy_static_ids()
    assert DeepSeekProvider.route_priority < _CODEBUDDY_ROUTE_PRIORITY


def test_accepts_model_exact_and_prefixed():
    p = DeepSeekProvider(api_key="k", base_url="https://api.deepseek.test")
    assert p.accepts_model("deepseek-flash")
    assert p.accepts_model("deepseek/deepseek-flash")
    assert not p.accepts_model("glm-5.3")
    assert not p.accepts_model("")


def test_health_reports_models_and_base():
    p = DeepSeekProvider(api_key="k", base_url="https://api.deepseek.test")
    h = p.health()
    assert h["configured"] is True
    assert h["base_url"] == "https://api.deepseek.test"
    assert "deepseek-flash" in h["models"]


def test_credit_estimation_returns_none_for_deepseek():
    """官方按预付费余额计费，没有「积分倍率」概念 → 估算必须是 None。

    返回一个编出来的数字会让请求日志的 Credit 列显示假账。
    """
    from buddy_proxy.core.credit_estimate import estimate_credit
    assert estimate_credit("deepseek", "deepseek-flash", 1000, 500) is None

def test_fallback_catalog_carries_capability_metadata():
    """兜底目录必须在**首次刷新之前**就报对图片能力。

    这是前端能力判断的数据源：``deepseek-flash`` 原生支持读图，若兜底条目
    只有 id/name，``/v1/models`` 会把它报成纯文本，客户端据此把用户发的
    图片整张剥掉（model_list 注释里写过这个坑的多次教训）。refresh_models()
    不是每次启动都会跑，所以兜底表本身就得带全字段。
    """
    p = DeepSeekProvider(api_key="k", base_url="https://api.deepseek.test")
    by_id = {m["id"]: m for m in p.models()}
    assert by_id["deepseek-flash"]["images"] is True
    assert by_id["deepseek-flash"]["input_modalities"] == ["text", "image"]
    assert by_id["deepseek-v4-pro"]["images"] is False
    assert by_id["deepseek-v4-pro"]["input_modalities"] == ["text"]
    for mid in by_id:
        assert by_id[mid]["context_window"] == 1048576
        assert by_id[mid]["tool_call"] is True


def test_fallback_and_refreshed_catalog_agree_on_metadata():
    """兜底表与线上刷新结果不能对同一个模型给出不同的能力画像。

    两边不一致时，用户会看到「重启后 /v1/models 变了」这种无从解释的漂移。
    """
    p = DeepSeekProvider(api_key="k", base_url="https://api.deepseek.test")
    fallback = {m["id"]: m for m in p.models()}

    async def run():
        q = _provider(lambda r: httpx.Response(200, json=_MODELS_PAYLOAD))
        return {m["id"]: m for m in await q.refresh_models()}

    refreshed = asyncio.run(run())
    assert set(fallback) == set(refreshed)
    for mid in fallback:
        for field in ("images", "input_modalities", "context_window", "tool_call"):
            assert fallback[mid][field] == refreshed[mid][field], (mid, field)
