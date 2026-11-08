"""yuntoken 网关 provider 的单元测试。

只测本仓库自己的逻辑（配置解析、协议分派、错误透传、目录归一），不触网：
httpx 的 ``MockTransport`` 替换 ``AsyncClient``，配置目录用
``BUDDY_PROXY_STATE_DIR`` 隔离到 tmp_path——临时测试不设这个环境变量会把
假 key 写进真实凭据目录（见 tests/conftest.py 的既有约定）。
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest
from fastapi import HTTPException

from buddy_proxy.providers import yuntoken as yt


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """每个用例独立的 state 目录，且清掉可能存在的真实环境变量。"""
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(tmp_path))
    for name in ("YUNTOKEN_API_KEY", "YUNTOKEN_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _provider(handler, *, base="http://gw.test", key="sk-test") -> yt.YuntokenProvider:
    p = yt.YuntokenProvider(base_url=base, api_key=key)
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------

def test_missing_config_raises_actionable_401(_isolated_state):
    """基址与 key 都缺时 401 里要分别点出两件事，而不是笼统一句「未配置」。"""
    p = yt.YuntokenProvider(base_url="", api_key="")
    with pytest.raises(HTTPException) as exc:
        p.ensure_auth()
    assert exc.value.status_code == 401
    message = exc.value.detail["error"]["message"]
    assert "网关基址" in message
    assert "API key" in message
    assert "YUNTOKEN_BASE_URL" in message


def test_env_overrides_files(_isolated_state, monkeypatch):
    """环境变量优先于文件。"""
    (_isolated_state / "yuntoken_api_key").write_text("file-key\n")
    (_isolated_state / "yuntoken.json").write_text(json.dumps({"base_url": "http://file.test"}))
    assert yt.resolve_api_key() == "file-key"
    assert yt.resolve_base_url() == "http://file.test"

    monkeypatch.setenv("YUNTOKEN_API_KEY", "env-key")
    monkeypatch.setenv("YUNTOKEN_BASE_URL", "http://env.test/")
    assert yt.resolve_api_key() == "env-key"
    # 尾部斜杠要剥掉，否则拼出来是 //v1/models
    assert yt.resolve_base_url() == "http://env.test"


def test_corrupt_config_file_is_not_fatal(_isolated_state):
    """可选配置文件损坏时返回空值而不抛——不该让整个代理起不来。"""
    (_isolated_state / "yuntoken.json").write_text("{not json")
    assert yt.resolve_base_url() == ""


def test_no_builtin_default_base_url():
    """基址刻意没有硬编码默认值（开源仓库不钉别人的基础设施）。"""
    assert "" not in yt._PROTOCOL_PATHS  # 只防手滑，真正的断言在下面
    p = yt.YuntokenProvider(base_url="", api_key="k")
    assert p.health()["base_url"] == ""


# ---------------------------------------------------------------------------
# 路由与协议
# ---------------------------------------------------------------------------

def test_route_priority_below_codebuddy():
    """裸名撞车时让位：低于 CodeBuddy 的 100。"""
    assert yt.YuntokenProvider.route_priority < 100


def test_accepts_bare_and_prefixed():
    p = _provider(lambda r: httpx.Response(200))
    p._models = [{"id": "claude-opus-4.6"}]
    assert p.accepts_model("claude-opus-4.6")
    assert p.accepts_model("yuntoken/claude-opus-4.6")
    assert not p.accepts_model("claude-haiku-4.5")


def test_responses_protocol_returns_501(_isolated_state):
    """上游没有 /v1/responses：回 501 + 换协议提示，而不是打一个必 404 的请求。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    p = _provider(handler)

    async def _run():
        with pytest.raises(HTTPException) as exc:
            await p.forward({"model": "m", "stream": True}, "responses")
        return exc.value

    exc = asyncio.run(_run())
    assert exc.status_code == 501
    assert "openai" in exc.detail["error"]["message"]
    assert seen == []  # 一个请求都没发出去


def test_openai_stream_passthrough(_isolated_state):
    chunks = [b"data: {\"a\":1}\n\n", b"data: [DONE]\n\n"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://gw.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b"".join(chunks))

    p = _provider(handler)

    async def _run() -> bytes:
        resp = await p.forward({"model": "m", "stream": True, "messages": []}, "openai")
        body = b""
        # 转发返回的流必须在**同一个事件循环**里消费完：跨 asyncio.run
        # 去读会让 shutdown_asyncgens 先关掉挂起的 raw_gen，帧凭空消失。
        async for part in resp.body_iterator:
            body += part if isinstance(part, bytes) else part.encode()
        return body

    assert asyncio.run(_run()) == b"".join(chunks)


def test_anthropic_uses_original_body(_isolated_state):
    """anthropic 必须发 original（协议转换前的形状），不能用已转成 chat 的 body。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"type": "message", "content": []})

    p = _provider(handler)
    original = {"model": "claude-opus-4.6", "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}]}
    chat_body = {"model": "claude-opus-4.6", "stream": False,
                 "messages": [{"role": "user", "content": "hi"}]}
    asyncio.run(p.forward(chat_body, "anthropic", original))
    assert captured["url"] == "http://gw.test/v1/messages"
    assert captured["body"]["messages"] == original["messages"]
    assert captured["body"]["model"] == "claude-opus-4.6"


def test_upstream_error_passthrough(_isolated_state):
    """上游 401 的错误体原样回给客户端（网关用的是 {"detail": ...} 形状）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Invalid or missing API Key"})

    p = _provider(handler)
    resp = asyncio.run(p.forward({"model": "m", "stream": False}, "openai"))
    assert resp.status_code == 401
    assert json.loads(resp.body)["detail"] == "Invalid or missing API Key"


# ---------------------------------------------------------------------------
# 模型目录
# ---------------------------------------------------------------------------

def test_refresh_models_keeps_capabilities_unset(_isolated_state):
    """上游没有能力元数据：目录条目不得凭空填 images/tool_call 等字段。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://gw.test/v1/models"
        return httpx.Response(200, json={"object": "list", "data": [
            {"id": "claude-opus-4.6", "object": "model", "owned_by": "anthropic",
             "description": "Claude model via Kiro API"},
            {"id": "", "object": "model"},
        ]})

    p = _provider(handler)
    models = asyncio.run(p.refresh_models())
    assert [m["id"] for m in models] == ["claude-opus-4.6"]
    entry = models[0]
    for field in ("images", "input_modalities", "tool_call", "context_window"):
        assert field not in entry, f"{field} 不该被凭空填出来"
    assert entry["description"] == "Claude model via Kiro API"


def test_refresh_failure_keeps_old_catalog(_isolated_state):
    """刷新失败保留旧目录：清空成空表等于所有模型 404。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    p = _provider(handler)
    p._models = [{"id": "kept", "object": "model"}]
    with pytest.raises(RuntimeError):
        asyncio.run(p.refresh_models())
    assert [m["id"] for m in p.models()] == ["kept"]
    assert "HTTP 500" in p.health()["models_error"]


def test_no_hardcoded_models():
    """代码里不能有默认模型表：网关挂哪些模型取决于它的账号池。"""
    p = yt.YuntokenProvider(base_url="http://gw.test", api_key="k")
    assert p.models() == []


def test_module_has_no_embedded_secret():
    """回归护栏：源码里不得出现真实凭据或私有地址。

    刻意**不写真实值**（写成 ``"sk-<真前缀>" not in source`` 等于把前缀本身
    留在开源仓库里），只按形状判：任何 ``sk-`` 开头的字面量 key、任何
    硬编码的私网/公网 IP 都属于泄漏。
    """
    source = open(yt.__file__, encoding="utf-8").read()
    assert not re.search(r"sk-[A-Za-z0-9]{8,}", source), "模块里出现了硬编码 API key"
    assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", source), "模块里出现了硬编码 IP 地址"
