"""上下文窗口守卫：转发前按目标通道真实窗口截断历史，防上游 4026。

2026-11-25 实测：聚合模型 glm-5.4 对外声明 1M（来自 zcode），但 failover 落到
trae 时 trae 只有 116k，客户端按 1M 管理上下文不及时 compact，几百 k 的请求
直接撞上游 4026 ``context length has exceeded``。守卫在派发前按**当前候选
通道**的真实窗口预检截断——zcode 给全量，落到 trae 才截。
"""

from __future__ import annotations

import asyncio

import pytest

from buddy_proxy.core import context_guard as cg


@pytest.fixture(autouse=True)
def _clean_cache():
    cg.reset_cache()
    yield
    cg.reset_cache()


# ---------------------------------------------------------------------------
# 估算
# ---------------------------------------------------------------------------

def test_estimate_grows_with_content():
    """内容越多估算越大（单调性是截断决策成立的前提）。"""
    small = {"messages": [{"role": "user", "content": "hi"}]}
    big = {"messages": [{"role": "user", "content": "x" * 200000}]}
    assert cg.estimate_body_tokens(big) > cg.estimate_body_tokens(small) > 0


def test_estimate_handles_missing_messages():
    assert cg.estimate_body_tokens({}) == 0
    assert cg.estimate_body_tokens({"messages": None}) == 0


# ---------------------------------------------------------------------------
# 截断
# ---------------------------------------------------------------------------

def _msgs(n: int, size: int = 30000) -> list[dict]:
    """造 n 条大消息（每条 ~10k tokens 估算），用于触发截断。"""
    return [{"role": "user" if i % 2 == 0 else "assistant",
             "content": "x" * size} for i in range(n)]


def test_truncate_keeps_system_and_last():
    """截断只能丢中间历史：system 全保留、最后一条保留。"""
    body = {"messages": [
        {"role": "system", "content": "SYS-KEEP"},
        *_msgs(6, size=60000),   # 6 条 ~20k tokens 估算，合计 > 116k×0.88
        {"role": "user", "content": "LAST-KEEP"},
    ]}
    new, dropped = cg.truncate_body_for_window(body, 116000)
    assert dropped >= 1, "116k 窗口下 7 条大消息必须被截"
    roles = [m["role"] for m in new["messages"]]
    assert roles[0] == "system" and new["messages"][0]["content"] == "SYS-KEEP"
    assert new["messages"][-1]["content"] == "LAST-KEEP"
    assert cg.estimate_body_tokens(new) <= int(116000 * cg._SAFETY_RATIO) + 1


def test_truncate_does_not_mutate_original():
    """原 body 不能被改——failover 下一个更大窗口的通道要拿全量。"""
    body = {"messages": _msgs(8)}
    original_msgs = body["messages"]
    original_contents = [m["content"] for m in original_msgs]
    new, dropped = cg.truncate_body_for_window(body, 50000)
    assert dropped >= 1
    assert body["messages"] is original_msgs, "原 messages 列表对象不能被替换"
    assert [m["content"] for m in body["messages"]] == original_contents, \
        "原 messages 内容不能变"
    assert len(new["messages"]) < len(original_msgs)


def test_truncate_under_budget_is_noop():
    """没超预算时原样返回（同一对象引用，零开销）。"""
    body = {"messages": [{"role": "user", "content": "hi"}]}
    new, dropped = cg.truncate_body_for_window(body, 1_000_000)
    assert dropped == 0
    assert new is body, "未超预算应返回原 body 引用"


def test_truncate_single_message_is_noop():
    """只剩一条消息时不截（没法安全丢）。"""
    body = {"messages": [{"role": "user", "content": "x" * 500000}]}
    new, dropped = cg.truncate_body_for_window(body, 1000)
    assert dropped == 0
    assert new is body


def test_truncate_all_system_keeps_everything():
    """全是 system 时不截（宁可放行让上游判，也不丢系统提示）。"""
    body = {"messages": [{"role": "system", "content": "x" * 500000}]}
    new, dropped = cg.truncate_body_for_window(body, 1000)
    assert dropped == 0
    assert new is body


# ---------------------------------------------------------------------------
# 窗口获取（缓存 + 按需 refresh + 失败放行）
# ---------------------------------------------------------------------------

def test_get_window_caches_and_avoids_refresh_when_present():
    """models() 已带窗口时，不触发 refresh、直接命中缓存。"""
    class P:
        id = "trae"
        def models(self):
            return [{"id": "glm-5.4", "context_window": 116000}]
        def refresh_models(self):
            raise AssertionError("已有窗口不该触发 refresh")

    w1 = asyncio.run(cg.get_window(P(), "glm-5.4"))
    w2 = asyncio.run(cg.get_window(P(), "glm-5.4"))
    assert w1 == w2 == 116000


def test_get_window_refreshes_when_absent():
    """models() 没窗口时触发 refresh，refresh 后能取到。"""
    state = {"refreshed": False}

    class P:
        id = "trae"
        def models(self):
            if state["refreshed"]:
                return [{"id": "glm-5.4", "context_window": 116000}]
            return [{"id": "glm-5.4"}]
        def refresh_models(self):
            state["refreshed"] = True

    w = asyncio.run(cg.get_window(P(), "glm-5.4"))
    assert w == 116000
    assert state["refreshed"]


def test_get_window_none_skips_guard_no_failure():
    """拿不到窗口返回 None（放行），且记进 _NO_WINDOW 不再反复 refresh。"""
    calls = {"n": 0}

    class P:
        id = "ghost"
        def models(self):
            calls["n"] += 1
            return []
        def refresh_models(self):
            return []

    w1 = asyncio.run(cg.get_window(P(), "m"))
    w2 = asyncio.run(cg.get_window(P(), "m"))
    assert w1 is None and w2 is None
    # 第二次走 _NO_WINDOW 缓存，不应再调 models()/refresh
    assert ("ghost", "m") in cg._NO_WINDOW


def test_get_window_refresh_failure_does_not_block():
    """refresh 抛错时守卫放行，不把请求搞挂。"""
    class P:
        id = "boom"
        def models(self):
            return []
        def refresh_models(self):
            raise RuntimeError("upstream down")

    w = asyncio.run(cg.get_window(P(), "m"))
    assert w is None


def test_window_lookup_is_case_insensitive_on_model():
    class P:
        id = "trae"
        def models(self):
            return [{"id": "GLM-5.4", "context_window": 116000}]

    assert asyncio.run(cg.get_window(P(), "glm-5.4")) == 116000
