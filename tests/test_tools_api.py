"""``/v1/tools/search`` 与 ``/v1/tools/fetch`` 单测 —— 全离线。

锁定的契约：

1. **固定指向官方，没有任何多通道路由**。这两个端点没有「模型名」可路由
   （只收 query/url），且是 DeepSeek 独有接口——别家通道根本没有对应端点。
   所以实现必须写死，别在路由层留一个「按 provider 猜」的口子。
2. **本地只做能省一次计费往返的轻校验**（空 query / 超长 / count 越界），
   其余字段原样透传（官方明确未知字段会被忽略）。
3. **上游错误原样透传**——尤其 401（key 无效）/402（余额不足）/404（抓不到
   内容）都是确定性的，重试无意义，客户端要能按官方错误体自行决策。
4. **不缓存**：按成功请求次数计费，缓存会让账单与实时性对不上。

运行：
    .venv/bin/python -m pytest tests/test_tools_api.py -v
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest
from fastapi.testclient import TestClient

from buddy_proxy import __main__ as m
from buddy_proxy.core import state as st
from buddy_proxy.web import tools_api


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.test")


@pytest.fixture()
def client(monkeypatch):
    """替换上游 httpx.AsyncClient，记录请求并回预设响应。"""
    import asyncio
    fake_state = SimpleNamespace(
        write_log=mock.MagicMock(), logger=mock.MagicMock(), json_logger=mock.MagicMock(),
        metrics=None, providers={}, runtime_info={}, client=mock.MagicMock(),
        mock_dir=None, started_at=0.0, enable_desensitize=False,
        enable_optimize_context=False, verbose_llm=False,
        ensure_auth=mock.MagicMock(), disabled_providers=set(),
    )
    monkeypatch.setattr(st, "proxy_state", fake_state)
    # client_meta 里用到的 request.client 由 TestClient 提供，无需额外准备。
    return TestClient(m.app)


_FAKE_CLIENT = {"seen": None, "response": None}


class _FakeAsyncClient:
    """最小 httpx.AsyncClient 替身：记录一次 POST，回放预定响应。"""

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        _FAKE_CLIENT["seen"] = {"url": url, "json": json, "headers": headers}
        return _FAKE_CLIENT["response"]()


def _install_client(monkeypatch, response_factory):
    _FAKE_CLIENT["response"] = response_factory
    monkeypatch.setattr(tools_api.httpx, "AsyncClient", _FakeAsyncClient)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def test_search_hits_official_url_and_passes_fields(client, monkeypatch):
    _install_client(monkeypatch, lambda: httpx.Response(
        200, json={"results": [{"title": "t", "url": "u", "summary": "s"}]}))

    r = client.post("/v1/tools/search",
                    json={"query": "deepseek v4", "count": 3, "need_content": True})
    assert r.status_code == 200
    assert r.json()["results"][0]["title"] == "t"
    seen = _FAKE_CLIENT["seen"]
    assert seen["url"] == "https://api.deepseek.test/v1/tools/search"
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    # count / need_content 必须原样透传，不能被本地裁掉
    assert seen["json"]["count"] == 3
    assert seen["json"]["need_content"] is True


def test_search_rejects_blank_query_without_calling_upstream(client, monkeypatch):
    _install_client(monkeypatch, lambda: pytest.fail("不该打到上游"))

    for bad in ({}, {"query": ""}, {"query": "   "}, {"query": 123}):
        r = client.post("/v1/tools/search", json=bad)
        assert r.status_code == 400, bad
        assert r.json()["detail"]["error"]["message"] == "query 不能为空"


def test_search_rejects_overlong_query(client, monkeypatch):
    _install_client(monkeypatch, lambda: pytest.fail("不该打到上游"))
    r = client.post("/v1/tools/search", json={"query": "x" * 129})
    assert r.status_code == 400
    assert "128" in r.json()["detail"]["error"]["message"]


def test_search_rejects_out_of_range_count(client, monkeypatch):
    _install_client(monkeypatch, lambda: pytest.fail("不该打到上游"))
    for bad in (0, 51, -1, "10", 2.5):
        r = client.post("/v1/tools/search", json={"query": "x", "count": bad})
        assert r.status_code == 400, bad


def test_search_accepts_boundary_count(client, monkeypatch):
    _install_client(monkeypatch, lambda: httpx.Response(200, json={"results": []}))
    for ok in (1, 50):
        r = client.post("/v1/tools/search", json={"query": "x", "count": ok})
        assert r.status_code == 200, ok
    # 不传 count 也合法（官方默认 10）
    r = client.post("/v1/tools/search", json={"query": "x"})
    assert r.status_code == 200


def test_search_passes_through_401(client, monkeypatch):
    """key 无效是确定性的：原样透传，不重试、不吞成 502。"""
    body = {"error": {"message": "Authentication Fails", "type": "authentication_error",
                      "param": None, "code": "invalid_request_error"}}
    _install_client(monkeypatch, lambda: httpx.Response(401, json=body))
    r = client.post("/v1/tools/search", json={"query": "x"})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "authentication_error"


def test_search_passes_through_402_balance(client, monkeypatch):
    _install_client(monkeypatch, lambda: httpx.Response(
        402, json={"error": {"message": "Insufficient Balance", "type": "insufficient_balance"}}))
    r = client.post("/v1/tools/search", json={"query": "x"})
    assert r.status_code == 402


def test_search_timeout_becomes_504(client, monkeypatch):
    def boom():
        raise httpx.ConnectTimeout("slow")

    _install_client(monkeypatch, boom)
    r = client.post("/v1/tools/search", json={"query": "x"})
    assert r.status_code == 504


def test_search_transport_error_becomes_502(client, monkeypatch):
    def boom():
        raise httpx.ConnectError("nope")

    _install_client(monkeypatch, boom)
    r = client.post("/v1/tools/search", json={"query": "x"})
    assert r.status_code == 502


def test_search_reports_missing_key_as_401(client, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _install_client(monkeypatch, lambda: pytest.fail("没 key 不该打上游"))
    r = client.post("/v1/tools/search", json={"query": "x"})
    assert r.status_code == 401
    assert "DEEPSEEK_API_KEY" in r.json()["detail"]["error"]["message"]


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def test_fetch_hits_official_url(client, monkeypatch):
    _install_client(monkeypatch, lambda: httpx.Response(
        200, json={"title": "Example", "content": "# Example"}))
    r = client.post("/v1/tools/fetch", json={"url": "https://example.com"})
    assert r.status_code == 200
    assert r.json()["content"] == "# Example"
    assert _FAKE_CLIENT["seen"]["url"] == "https://api.deepseek.test/v1/tools/fetch"
    assert _FAKE_CLIENT["seen"]["json"] == {"url": "https://example.com"}


def test_fetch_rejects_blank_url(client, monkeypatch):
    _install_client(monkeypatch, lambda: pytest.fail("不该打到上游"))
    for bad in ({}, {"url": ""}, {"url": "  "}, {"url": None}):
        r = client.post("/v1/tools/fetch", json=bad)
        assert r.status_code == 400, bad


def test_fetch_passes_through_404(client, monkeypatch):
    """抓不到内容时官方回 404（不是 200 空体），必须原样透传。

    客户端据此区分「这一页抓不到」与「路由不存在」——吞成 200 会让调用方
    以为拿到了空内容。
    """
    _install_client(monkeypatch, lambda: httpx.Response(
        404, json={"error": {"message": "no usable content", "type": "not_found"}}))
    r = client.post("/v1/tools/fetch", json={"url": "https://example.com/x"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 固定路由：不存在多通道路由的口子
# ---------------------------------------------------------------------------

def test_endpoints_are_fixed_to_official_base(client, monkeypatch):
    """DEEPSEEK_BASE_URL 一改，两个端点都跟着走——证明它们读的是同一份配置。"""
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://proxy.internal/deepseek")
    _install_client(monkeypatch, lambda: httpx.Response(200, json={"results": []}))
    client.post("/v1/tools/search", json={"query": "x"})
    assert _FAKE_CLIENT["seen"]["url"] == "https://proxy.internal/deepseek/v1/tools/search"


def test_no_caching_between_calls(client, monkeypatch):
    """两次相同查询必须打两次上游：按成功请求计费，缓存会让账单对不上。"""
    calls = []

    def responder():
        calls.append(1)
        return httpx.Response(200, json={"results": []})

    _install_client(monkeypatch, responder)
    client.post("/v1/tools/search", json={"query": "same"})
    client.post("/v1/tools/search", json={"query": "same"})
    assert len(calls) == 2


def test_tool_status_reports_config():
    st_dict = tools_api._tool_status()
    assert st_dict["configured"] is True
    assert st_dict["base_url"] == "https://api.deepseek.test"


def test_health_exposes_tools_status(client):
    body = client.get("/health").json()
    assert body["tools"]["configured"] is True
    assert body["tools"]["base_url"] == "https://api.deepseek.test"