"""codex 模型目录刷新 + 生图 + 上游流式收口（全部离线）。

三块盯的东西：

1. **目录刷新**（``/ui/api/models/refresh`` → ``CodexProvider.refresh_models``）：
   上游目录按 ``client_version`` 分代、只收 ``visibility=list``、空表不得覆盖
   旧快照、写回 ``models.json`` 且保留图片模型。
2. **生图**（``gpt-image-*`` → ``/images/generations``）：SSE 收成
   ``b64_json``，按协议包成 chat / Responses / Anthropic；失败换号冷却。
3. **心跳不进闸门**：生图与高推理模型首帧是 ``keepalive``，不能被当成
   「语义已至」而断掉换号能力。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from buddy_proxy.codex import catalog, failover
from buddy_proxy.codex.credentials import save_account_cred
from buddy_proxy.codex.provider import CodexProvider
from buddy_proxy.web.ui import channels

from test_codex_failover import (  # noqa: F401 - fixtures 复用
    _ChunkedBody,
    _final_response,
    _patch_token,
    _sse_events,
    _write_account,
    two_accounts,
)


@pytest.fixture(autouse=True)
def _isolated_models(monkeypatch, tmp_path):
    """刷新会写状态目录快照；优先读快照会让本文件的目录改写泄漏到别的测试。"""
    state = tmp_path / "state"
    # 写快照的路径与 provider 读快照的路径都要指到隔离目录
    monkeypatch.setattr("buddy_proxy.codex.provider._runtime_models_path",
                        lambda: state / "models.json")
    monkeypatch.setattr("buddy_proxy.codex.provider.codex_state_dir",
                        lambda: state)


@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    failover._cooldowns.clear()
    yield
    failover._cooldowns.clear()


# ---------------------------------------------------------------------------
# 1. 目录刷新
# ---------------------------------------------------------------------------

_CATALOG = {"models": [
    {"slug": "old-model", "visibility": "list", "supported_in_api": True,
     "display_name": "Old", "description": "old one", "context_window": 1000},
    {"slug": "gpt-6.1-sol", "visibility": "list", "supported_in_api": True,
     "display_name": "GPT-6.1-Sol", "description": "workhorse",
     "context_window": 500000, "input_modalities": ["text", "image"]},
    {"slug": "gpt-reserve", "visibility": "hide", "supported_in_api": True},
    {"slug": "disabled-model", "visibility": "list", "supported_in_api": False},
]}


def test_parse_catalog_filters_hidden_and_unsupported():
    out = catalog.parse_catalog(_CATALOG)
    assert [m["slug"] for m in out] == ["old-model", "gpt-6.1-sol"]


def test_parse_catalog_tolerates_garbage():
    assert catalog.parse_catalog(None) == []
    assert catalog.parse_catalog({"models": "nope"}) == []
    assert catalog.parse_catalog({"models": [1, {"nope": 1}, None]}) == []


def test_build_model_entries_default_first_and_keeps_context():
    entries = catalog.build_model_entries(
        catalog.parse_catalog(_CATALOG), image_ids=["gpt-image-2.5"])
    ids = [e["id"] for e in entries]
    assert ids[0] == "gpt-6.1-sol", "默认模型必须置顶"
    assert entries[0]["context_window"] == 500000
    assert entries[0]["input_modalities"] == ["text", "image"]
    # 本地没有中文说明的新 slug 回退上游描述
    assert entries[1]["description"].startswith("Old")
    # 图片模型原样追加、带 type 标记
    assert entries[-1] == {"id": "gpt-image-2.5",
                           "description": "GPT Image 2.5（生图，默认）",
                           "type": "image"}


def test_catalog_url_uses_client_version():
    assert catalog.catalog_url("https://chatgpt.com/backend-api/codex") == (
        "https://chatgpt.com/backend-api/codex/models?client_version=0.159.0")


def _state_models(target: Path | None = None) -> Path:
    """刷新写的是状态目录（不入 git 工作区），conftest 已把 CODEX_STATE_DIR 指到 tmp。"""
    from buddy_proxy.codex.provider import _runtime_models_path

    return target or _runtime_models_path()


def test_refresh_writes_catalog_and_models(monkeypatch, two_accounts, tmp_path):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    target = _state_models()
    upstream.routes["tok-acct-a"] = lambda req: httpx.Response(200, json=_CATALOG)

    models = asyncio.run(prov.refresh_models())

    req = upstream.requests[0]
    assert "/backend-api/codex/models?client_version=" in str(req.url)
    assert req.headers["Chatgpt-Account-Id"] == "acc-acct-a"
    written = json.loads(target.read_text())["models"]
    assert [m["id"] for m in written] == ["gpt-6.1-sol", "old-model"]
    # 下一次启动读的就是这份快照（优先于随包的 models.json）
    assert [m["id"] for m in CodexProvider().models()][:2] == [
        "gpt-6.1-sol", "old-model"]
    ids = [m["id"] for m in models]
    assert ids[:2] == ["gpt-6.1-sol", "old-model"]
    assert "gpt-image-2.5" in ids, "图片模型刷新后仍在"
    assert "gpt-reserve" not in ids


def test_refresh_empty_catalog_keeps_snapshot(monkeypatch, two_accounts, tmp_path):
    """空表是风控抽风的典型表现：绝不能把它写进文件、清空目录。"""
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    target = _state_models()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"models": [{"id": "keep-me"}]}')
    upstream.routes["tok-acct-a"] = lambda req: httpx.Response(200, json={"models": []})
    upstream.routes["tok-acct-b"] = lambda req: httpx.Response(200, json={"models": []})

    with pytest.raises(catalog.CatalogError) as exc:
        asyncio.run(prov.refresh_models())
    assert "空" in str(exc.value)
    assert "keep-me" in target.read_text()


def test_refresh_falls_back_to_next_account(monkeypatch, two_accounts, tmp_path):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = lambda req: httpx.Response(403, json={})
    upstream.routes["tok-acct-b"] = lambda req: httpx.Response(200, json=_CATALOG)
    asyncio.run(prov.refresh_models())
    assert upstream.calls == ["tok-acct-a", "tok-acct-b"]
    # 目录失败不等于账号不能转发，不该冷却
    assert failover.cooldown_left("acct-a")[0] == 0.0


def test_refresh_without_accounts_raises():
    with pytest.raises(catalog.CatalogError):
        asyncio.run(CodexProvider().refresh_models())


def test_ui_refresh_endpoint_reports_added(monkeypatch, tmp_path):
    """模型页「↻ 重新拉取」走 /ui/api/models/refresh，codex 必须可刷新。"""
    from buddy_proxy.core import state as state_mod

    _write_account("acct-a")
    _patch_token(monkeypatch)
    upstream = httpx.MockTransport(lambda req: httpx.Response(200, json=_CATALOG))

    prov = CodexProvider()
    prov._client = httpx.AsyncClient(transport=upstream)

    class _State:
        providers = {"codex": prov}
        hidden_models: set[str] = set()

    monkeypatch.setattr(state_mod, "get_state", lambda: _State())
    # routes 模块里的 get_state 是 from-import 的引用，两个都要打
    monkeypatch.setattr("buddy_proxy.web.ui.models_api.get_state", lambda: _State())

    client = TestClient(channels.app)
    r = client.post("/ui/api/models/refresh", json={"provider": "codex"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == len(prov.models())
    assert "old-model" in [m for m in body["added"]]


# ---------------------------------------------------------------------------
# 2. 生图
# ---------------------------------------------------------------------------

def _image_sse(fmt: str = "png") -> bytes:
    # 真实上游先发若干 keepalive（首帧要等十几秒），最后才给图
    return _sse_events(
        ("keepalive", {"type": "keepalive"}),
        ("keepalive", {"type": "keepalive"}),
        ("keepalive", {"type": "keepalive"}),
        ("image_generation.completed", {
            "type": "image_generation.completed", "b64_json": "QUJD",
            "output_format": fmt, "created_at": 1760000000,
            "generation_id": "gen_1", "size": "1024x1024",
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        }),
    )


def _image_ok(fmt: str = "png"):
    def _route(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_ChunkedBody([_image_sse(fmt)]))
    return _route


def _image_body(**over) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "codex/gpt-image-2.5",
        "messages": [{"role": "user", "content": "一只雪地里的红狐，水彩"}],
        "stream": False,
    }
    body.update(over)
    return body


def test_image_models_listed():
    prov = CodexProvider()
    assert prov.accepts_model("codex/gpt-image-2.5")
    assert prov._is_image_model("gpt-image-2.5-sunburst")


def test_image_openai_protocol(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = _image_ok()

    resp = asyncio.run(prov.forward(_image_body(), "openai"))
    payload = json.loads(resp.body)
    assert payload["object"] == "chat.completion"
    message = payload["choices"][0]["message"]
    assert message["content"] == "图片已生成。"
    url = message["images"][0]["image_url"]["url"]
    assert url == "data:image/png;base64,QUJD"
    assert payload["usage"]["total_tokens"] == 3

    req = upstream.requests[0]
    assert str(req.url) == "https://chatgpt.com/backend-api/codex/images/generations"
    sent = json.loads(req.content)
    assert sent == {"model": "gpt-image-2.5", "prompt": "一只雪地里的红狐，水彩",
                    "store": False, "stream": True}


def test_image_prompt_from_multimodal_and_direct_field(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = _image_ok()
    asyncio.run(prov.forward(_image_body(messages=[{
        "role": "user",
        "content": [{"type": "text", "text": "画个"} , {"type": "text", "text": "苹果"}],
    }]), "openai"))
    assert json.loads(upstream.requests[0].content)["prompt"] == "画个苹果"

    asyncio.run(prov.forward(_image_body(prompt="直接给 prompt"), "openai"))
    assert json.loads(upstream.requests[-1].content)["prompt"] == "直接给 prompt"


def test_image_requires_prompt(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    with pytest.raises(Exception) as exc:
        asyncio.run(prov.forward(_image_body(messages=[]), "openai"))
    assert getattr(exc.value, "status_code", None) == 400
    assert upstream.calls == []


def test_image_responses_and_anthropic_protocols(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = _image_ok("jpeg")

    resp = asyncio.run(prov.forward(_image_body(), "responses"))
    out = json.loads(resp.body)
    assert out["object"] == "response"
    parts = out["output"][0]["content"]
    assert parts[0]["type"] == "output_text"
    assert parts[1] == {"type": "output_image",
                        "image_url": "data:image/jpeg;base64,QUJD"}

    resp = asyncio.run(prov.forward(_image_body(), "anthropic"))
    msg = json.loads(resp.body)
    assert msg["type"] == "message"
    image = next(b for b in msg["content"] if b["type"] == "image")
    assert image["source"]["media_type"] == "image/jpeg"
    assert image["source"]["data"] == "QUJD"


def test_image_stream_protocol_emits_single_frame(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = _image_ok()

    async def _go():
        resp = await prov.forward(_image_body(stream=True), "openai")
        out = []
        async for raw in resp.body_iterator:
            out.append(raw.decode() if isinstance(raw, bytes) else raw)
        return "".join(out)

    text = asyncio.run(_go())
    chunks = [json.loads(line[6:]) for line in text.splitlines()
              if line.startswith("data: ") and line[6:] != "[DONE]"]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["delta"]["images"][0]["image_url"]["url"].startswith(
        "data:image/png")


def test_image_failure_cools_and_fails_over(monkeypatch, two_accounts):
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = lambda req: httpx.Response(
        429, json={"error": {"message": "slow down"}})
    upstream.routes["tok-acct-b"] = _image_ok()
    resp = asyncio.run(prov.forward(_image_body(), "openai"))
    assert resp.status_code == 200
    assert upstream.calls[:2] == ["tok-acct-a", "tok-acct-b"]
    left, kind = failover.cooldown_left("acct-a")
    assert left > 0 and kind == "quota"


def test_image_missing_b64_raises():
    from buddy_proxy.codex.images import ImageUpstreamError, to_data_url

    with pytest.raises(ImageUpstreamError):
        to_data_url({"output_format": "png"})


# ---------------------------------------------------------------------------
# 3. 上游流式收口
# ---------------------------------------------------------------------------

def test_keepalive_is_not_a_semantic_event(monkeypatch, two_accounts):
    """首帧 keepalive 后断流：闸门必须判 eof 并换号，而不是把心跳当成功。"""
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    ka = _sse_events(("keepalive", {"type": "keepalive"}))
    upstream.routes["tok-acct-a"] = lambda req: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        stream=_ChunkedBody([ka]))
    upstream.routes["tok-acct-b"] = lambda req: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        stream=_ChunkedBody([_sse_events((
            "response.completed",
            {"type": "response.completed", "response": _final_response("from-b")},
        ))]))

    resp = asyncio.run(prov.forward(
        {"model": "codex/gpt-5.6-terra", "messages": [{"role": "user", "content": "hi"}],
         "stream": False}, "openai"))
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == "from-b"
    assert upstream.calls == ["tok-acct-a", "tok-acct-b"]


def test_nonstream_always_requests_upstream_stream(monkeypatch, two_accounts):
    """非流式客户端也必须以 stream=true 打上游（否则 400 Stream must be set to true）。"""
    prov, upstream = two_accounts
    _patch_token(monkeypatch)
    upstream.routes["tok-acct-a"] = lambda req: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        stream=_ChunkedBody([_sse_events((
            "response.completed",
            {"type": "response.completed", "response": _final_response("pong")},
        ))]))
    asyncio.run(prov.forward(
        {"model": "codex/gpt-5.6-terra", "messages": [{"role": "user", "content": "hi"}],
         "stream": False}, "openai"))
    sent = json.loads(upstream.requests[0].content)
    assert sent["stream"] is True
    assert sent["store"] is False


def test_aiter_sse_events_resets_name_between_frames():
    """event 名必须在帧间清空：否则第二帧会顶着第一帧的 event 名。"""
    from buddy_proxy.codex.projection import aiter_sse_events

    lines = [
        "event: response.created", 'data: {"type": "x"}', "",
        'data: {"type": "response.completed"}', "",
    ]

    async def _lines():
        for line in lines:
            yield line

    async def _go():
        return [name async for name, _ in aiter_sse_events(_lines())]

    assert asyncio.run(_go()) == ["response.created", "response.completed"]
