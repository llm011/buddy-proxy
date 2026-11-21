"""账号「临时下线」核心过滤（core/load_balance.py 的 reorder 入口）。

覆盖不变量：
1. 被禁用账号一律剔除，无论负载均衡/会话轮转开关开没开；
2. 全部禁用 → 空列表（通道据此报「无可用账号」）；单账号被禁用 → 空；
3. 无禁用表 / state 缺失 → 行为与历史逐字节一致；
4. 禁用过滤不改动入参列表，且与会话轮转/负载均衡叠加时仍生效。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from buddy_proxy.core import load_balance as lb


class _Acct:
    def __init__(self, id):
        self.id = id


@pytest.fixture(autouse=True)
def _clean():
    lb.clear_sessions()
    lb.clear_boards()
    yield
    lb.clear_sessions()
    lb.clear_boards()


def test_disabled_account_excluded_without_any_switch():
    st = SimpleNamespace(disabled_accounts={"antigravity:b@x.com"})
    accts = [_Acct("a@x.com"), _Acct("b@x.com"), _Acct("c@x.com")]
    out = lb.reorder(st, "antigravity", accts)
    assert [a.id for a in out] == ["a@x.com", "c@x.com"]
    # 入参不被修改
    assert [a.id for a in accts] == ["a@x.com", "b@x.com", "c@x.com"]


def test_all_disabled_returns_empty():
    st = SimpleNamespace(disabled_accounts={"p:a", "p:b"})
    assert lb.reorder(st, "p", [_Acct("a"), _Acct("b")]) == []


def test_single_account_disabled_returns_empty():
    st = SimpleNamespace(disabled_accounts={"p:a"})
    assert lb.reorder(st, "p", [_Acct("a")]) == []


def test_no_disabled_table_unchanged():
    accts = [_Acct("a"), _Acct("b")]
    assert [a.id for a in lb.reorder(SimpleNamespace(), "p", accts)] == ["a", "b"]
    assert [a.id for a in lb.reorder(None, "p", accts)] == ["a", "b"]


def test_disabled_filter_applies_with_load_balance_on():
    st = SimpleNamespace(disabled_accounts={"p:b"}, load_balance={"p"})
    lb.board("p").update("a", 1)
    lb.board("p").update("c", 10000)
    accts = [_Acct("a"), _Acct("b"), _Acct("c")]
    for _ in range(20):
        out = lb.reorder(st, "p", accts)
        assert "b" not in [a.id for a in out]
        assert sorted(a.id for a in out) == ["a", "c"]


def test_disabled_filter_applies_with_session_rotation_on():
    st = SimpleNamespace(disabled_accounts={"p:b"}, session_rotation={"p"})
    accts = [_Acct("a"), _Acct("b"), _Acct("c")]
    out = lb.reorder(st, "p", accts, session_key="s1")
    assert "b" not in [a.id for a in out]
    assert sorted(a.id for a in out) == ["a", "c"]


def test_is_account_disabled():
    st = SimpleNamespace(disabled_accounts={"antigravity:b@x.com"})
    assert lb.is_account_disabled(st, "antigravity", "b@x.com") is True
    assert lb.is_account_disabled(st, "antigravity", "a@x.com") is False
    # provider 段不同 → 不命中（键含 provider 前缀，跨通道不串）
    assert lb.is_account_disabled(st, "kimi", "b@x.com") is False
    assert lb.is_account_disabled(SimpleNamespace(), "p", "a") is False
    assert lb.is_account_disabled(None, "p", "a") is False


def test_disabled_key_is_provider_scoped():
    # 同名账号 id 在不同通道互不影响
    st = SimpleNamespace(disabled_accounts={"antigravity:shared"})
    assert [a.id for a in lb.reorder(st, "antigravity", [_Acct("shared"), _Acct("x")])] == ["x"]
    assert [a.id for a in lb.reorder(st, "kimi", [_Acct("shared"), _Acct("x")])] == ["shared", "x"]
