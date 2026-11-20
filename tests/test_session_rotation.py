"""会话轮转核心与入口路由测试（core/load_balance.py + web/routes.py）。

覆盖：
1. 第一级显式会话标识提取（请求头、prompt_cache_key、metadata.user_id），以及无标识时返回空串；
2. 严格轮转 + 会话粘性：会话 1→账号 1、会话 2→账号 2、同会话粘在同一账号；
3. 缓存隔离模型（如 deepseek-v4.1-flash）在会话轮转下照常参与；
4. 绑定账号进入冷却/不可用后自动切到下一个可用账号并刷新绑定；
5. TTL 过期与容量上限淘汰；
6. /v1/chat/completions、/v1/responses、/v1/messages 三条 HTTP 路由正确注入与清理 SESSION_KEY。
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from buddy_proxy.core import load_balance as lb
from buddy_proxy.core import state as st
from buddy_proxy.web import routes as routes_mod


class _Acct:
    def __init__(self, id: str):
        self.id = id

    def __repr__(self):
        return f"Acct({self.id})"


@pytest.fixture(autouse=True)
def _clean_sessions():
    lb.clear_sessions()
    lb.clear_swap_samples()
    yield
    lb.clear_sessions()
    lb.clear_swap_samples()


# -- 1. 显式会话标识提取 ----------------------------------------------------


def test_extract_session_key_headers_priority():
    assert lb.extract_session_key({"X-Session-Id": "sess-1", "Session-Id": "sess-2"}) == "sess-1"
    assert lb.extract_session_key({"Session-Id": "sess-2"}) == "sess-2"
    assert lb.extract_session_key({"X-Conversation-Id": "conv-3"}) == "conv-3"
    assert lb.extract_session_key({"conversation-id": "conv-4"}) == "conv-4"


def test_extract_session_key_body_fields():
    assert lb.extract_session_key({}, {"prompt_cache_key": "pk-1"}) == "pk-1"
    assert lb.extract_session_key({}, {"session_id": "bs-1"}) == "bs-1"
    assert lb.extract_session_key({}, {"conversation_id": "bc-1"}) == "bc-1"


def test_extract_session_key_claude_metadata():
    # Claude Code 格式：user_<hash>_account_<uuid>_session_<uuid>
    uid = "user_abc123_account_1111-2222_session_9999-aaaa"
    assert lb.extract_session_key({}, {"metadata": {"user_id": uid}}) == "9999-aaaa"

    # JSON 字符串格式
    juid = json.dumps({"device_id": "d1", "session_id": "json-sess-1"})
    assert lb.extract_session_key({}, {"metadata": {"user_id": juid}}) == "json-sess-1"

    # 普通 user_id（不含 session 段）不能误判成同一个会话
    assert lb.extract_session_key({}, {"metadata": {"user_id": "user_plain_123"}}) == ""


def test_extract_session_key_absent_returns_empty():
    assert lb.extract_session_key({}, {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]}) == ""
    assert lb.extract_session_key(None, None) == ""


def test_extract_session_key_long_id_is_hashed():
    long_id = "s" * 500
    got = lb.extract_session_key({"x-session-id": long_id})
    assert got.startswith("sha256:")
    assert len(got) < 64
    assert lb.extract_session_key({"x-session-id": long_id}) == got


# -- 2. 轮转与会话粘性 ------------------------------------------------------


def test_session_rotation_assigns_sequentially_and_sticks():
    st_obj = SimpleNamespace(session_rotation={"codebuddy"}, load_balance=set())
    accts = [_Acct("a1"), _Acct("a2"), _Acct("a3")]

    # 会话 1 首轮 → a1
    assert [a.id for a in lb.reorder(st_obj, "codebuddy", accts, session_key="s1")] == ["a1", "a2", "a3"]
    # 会话 1 续轮 → 仍粘在 a1，不推动游标
    assert [a.id for a in lb.reorder(st_obj, "codebuddy", accts, session_key="s1")] == ["a1", "a2", "a3"]

    # 会话 2 首轮 → a2
    assert [a.id for a in lb.reorder(st_obj, "codebuddy", accts, session_key="s2")] == ["a2", "a3", "a1"]
    # 会话 2 续轮 → 仍粘在 a2
    assert [a.id for a in lb.reorder(st_obj, "codebuddy", accts, session_key="s2")] == ["a2", "a3", "a1"]

    # 会话 3 首轮 → a3
    assert [a.id for a in lb.reorder(st_obj, "codebuddy", accts, session_key="s3")] == ["a3", "a1", "a2"]
    # 会话 4 回到 → a1
    assert [a.id for a in lb.reorder(st_obj, "codebuddy", accts, session_key="s4")] == ["a1", "a2", "a3"]
    # 会话 1 依然粘在 a1
    assert [a.id for a in lb.reorder(st_obj, "codebuddy", accts, session_key="s1")] == ["a1", "a2", "a3"]


def test_session_rotation_without_session_key_preserves_original_order():
    st_obj = SimpleNamespace(session_rotation={"codebuddy"}, load_balance=set())
    accts = [_Acct("a1"), _Acct("a2"), _Acct("a3")]
    for _ in range(10):
        got = lb.reorder(st_obj, "codebuddy", accts, model="glm-5.3-flash")
        assert [a.id for a in got] == ["a1", "a2", "a3"]


def test_session_rotation_includes_account_scoped_models():
    """缓存隔离模型在会话轮转下也应正常轮转（同一会话不换号，不会掉缓存）。"""
    st_obj = SimpleNamespace(session_rotation={"codebuddy"}, load_balance=set())
    accts = [_Acct("a1"), _Acct("a2")]

    got1 = lb.reorder(st_obj, "codebuddy", accts, model="deepseek-v4.1-flash", session_key="s1")
    got2 = lb.reorder(st_obj, "codebuddy", accts, model="deepseek-v4.1-flash", session_key="s2")
    got1_again = lb.reorder(st_obj, "codebuddy", accts, model="deepseek-v4.1-flash", session_key="s1")

    assert got1[0].id == "a1"
    assert got2[0].id == "a2"
    assert got1_again[0].id == "a1"


def test_session_rotation_rebinds_when_bound_account_unavailable():
    st_obj = SimpleNamespace(session_rotation={"codebuddy"}, load_balance=set())
    a1, a2, a3 = _Acct("a1"), _Acct("a2"), _Acct("a3")

    assert lb.reorder(st_obj, "codebuddy", [a1, a2, a3], session_key="s1")[0].id == "a1"
    # a1 进冷却，候选池只剩 [a2, a3]
    next_order = lb.reorder(st_obj, "codebuddy", [a2, a3], session_key="s1")
    rebound_id = next_order[0].id
    assert rebound_id in ("a2", "a3")
    # 即使 a1 随后恢复，s1 也继续粘在新绑定的账号上（避免来回抖动掉缓存）
    assert lb.reorder(st_obj, "codebuddy", [a1, a2, a3], session_key="s1")[0].id == rebound_id


def test_session_router_ttl_expiry():
    router = lb.SessionRouter()
    accts = [_Acct("a1"), _Acct("a2")]
    t0 = 1000.0
    assert router.order("p", "s1", accts, now=t0)[0].id == "a1"
    # 过期后重新分配，按游标轮到 a2
    assert router.order("p", "s1", accts, now=t0 + lb.SESSION_TTL_S + 1)[0].id == "a2"


def test_session_router_evicts_oldest_over_capacity():
    router = lb.SessionRouter()
    accts = [_Acct("a1"), _Acct("a2")]
    t0 = 1000.0
    for i in range(lb.MAX_SESSIONS + 5):
        router.order("p", f"s-{i}", accts, now=t0 + i)
    assert len(router._bindings) <= lb.MAX_SESSIONS


# -- 3. 路由层 ContextVar 集成 ----------------------------------------------


def _make_route_client():
    fake_state = SimpleNamespace(
        mock_dir=None,
        client=SimpleNamespace(session={}),
        providers={},
        default_provider="codebuddy",
        default_model=None,
        disabled_models=set(),
        hidden_models=set(),
        disabled_providers=set(),
        load_balance=set(),
        session_rotation={"codebuddy"},
        model_schedules={},
        model_order={},
        metrics=None,
        benefits=None,
        enable_desensitize=False,
        enable_optimize_context=False,
        verbose_llm=False,
        logger=None,
        json_logger=None,
        started_at=time.time(),
        write_log=lambda *a, **kw: None,
    )
    return fake_state


def test_routes_propagate_and_reset_session_key():
    seen: list[str] = []

    async def fake_forward_chat(body, protocol, original=None):
        seen.append(lb.current_session_key())
        return JSONResponse(status_code=200, content={"ok": True})

    fake_state = _make_route_client()
    client = TestClient(st.app)

    with (
        mock.patch.object(routes_mod, "get_state", return_value=fake_state),
        mock.patch.object(routes_mod, "log_client_request"),
        mock.patch.object(routes_mod, "diagnostic"),
        mock.patch.object(routes_mod, "forward_chat", side_effect=fake_forward_chat),
    ):
        # 1) /v1/chat/completions 带 x-session-id
        r1 = client.post(
            "/v1/chat/completions",
            headers={"x-session-id": "ethan-sess-1"},
            json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r1.status_code == 200
        assert lb.current_session_key() == ""

        # 2) /v1/messages 带 Claude Code metadata.user_id（anthropic_to_chat 会剥掉 metadata，必须在转换前提取）
        r2 = client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 64,
                "metadata": {"user_id": "user_h_account_a_session_cc-sess-2"},
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert r2.status_code == 200
        assert lb.current_session_key() == ""

        # 3) /v1/responses 带 prompt_cache_key
        r3 = client.post(
            "/v1/responses",
            json={"model": "gpt-5", "input": "hi", "prompt_cache_key": "resp-sess-3"},
        )
        assert r3.status_code == 200
        assert lb.current_session_key() == ""

        # 4) 不带任何会话标识
        r4 = client.post(
            "/v1/chat/completions",
            json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r4.status_code == 200

    assert seen == ["ethan-sess-1", "cc-sess-2", "resp-sess-3", ""]
