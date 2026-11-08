"""额度耗尽的账号自动调到顺位末尾（用户 2026-11-08 要求）。

## 需求

某个 codebuddy 账号额度用完后，在账号顺位里把它自动挪到最后——这样 failover
不会再优先打一个注定失败的号。

## 范围

**同一个 provider 内部**的账号之间调序（codebuddy 的 index.json priority），
不跨通道。`reorder_accounts(ids, region)` 只动本区账号，不影响 qoder / trae。

## 判定

某账号**所有包的剩余合计** <= ``_EXHAUSTED_REMAINING``（0.05）即算耗尽。
阈值与界面展示精度对齐：界面保留一位小数，剩 0.04 会渲染成「剩 0」，
若判空只认 ``<= 0``，用户会看到「剩 0」的账号还排在第一位。

## 必须守住的边界

- **查询失败的账号不动**：拿不到额度 ≠ 额度为 0。把网络抖动变成路由变化是
  最糟的失败模式。
- 其余账号保持相对顺序；多个耗尽账号按原顺序追加到末尾。
- 顺序没变就不写盘（额度查询是高频动作，无谓写盘会一直刷新 mtime）。
- 写盘失败只记日志，不影响额度返回。
"""

from __future__ import annotations

import pytest

from buddy_proxy.codebuddy_provider import credentials as creds
from buddy_proxy.codebuddy_provider import failover
from buddy_proxy.codebuddy_provider.provider import (
    _EXHAUSTED_REMAINING,
    CodeBuddyProvider,
)


def _cred(account_id: str, priority: int) -> dict:
    return {
        "account_id": account_id,
        "token": f"at-{account_id}",
        "refresh_token": f"rt-{account_id}",
        "expires_at_ms": 4_102_444_800_000,
        "uid": f"uid-{account_id}",
        "nickname": account_id,
        "enterprise_id": "ent-1",
        "domain": "https://copilot.tencent.com",
        "machine_id": f"mach-{account_id}",
        "source": "state",
        "priority": priority,
        "region": "cn",
    }


@pytest.fixture
def three_accounts(monkeypatch):
    """三个账号，顺位 a(0) / b(1) / c(2)。"""
    for i, aid in enumerate(("a", "b", "c")):
        creds.save_account_cred(_cred(aid, i))
    monkeypatch.setattr(failover, "_cooldowns", {})
    return ["a", "b", "c"]


def _order():
    return [a.id for a in creds.list_accounts() if a.region == "cn"]


def _fake_quota(monkeypatch, remaining: dict[str, float | None]):
    """让 _quota_one_account 按 id 返回额度；None = 查询失败（抛异常）。"""

    def fake(self, acct):
        value = remaining.get(acct.id)
        if value is None:
            raise RuntimeError("query failed")
        return ([{"label": "资源包", "used": 1.0, "total": 100.0,
                  "remaining": value, "percent": 1,
                  "reset_ts": None, "expire_ts": None, "unit": "credit"}], "pro")

    monkeypatch.setattr(CodeBuddyProvider, "_quota_one_account", fake)


def test_exhausted_account_moves_to_end(three_accounts, monkeypatch):
    """a 额度耗尽 → 排到最后，其余相对顺序不变。"""
    _fake_quota(monkeypatch, {"a": 0.0, "b": 500.0, "c": 300.0})
    CodeBuddyProvider().quota()
    assert _order() == ["b", "c", "a"]


def test_multiple_exhausted_keep_relative_order(three_accounts, monkeypatch):
    """a、c 都耗尽 → 按原顺序追加到末尾（a 在 c 前）。"""
    _fake_quota(monkeypatch, {"a": 0.0, "b": 500.0, "c": 0.0})
    CodeBuddyProvider().quota()
    assert _order() == ["b", "a", "c"]


def test_tiny_remaining_counts_as_exhausted(three_accounts, monkeypatch):
    """剩 0.04 会被界面显示成「剩 0」，必须同判为空。"""
    assert _EXHAUSTED_REMAINING >= 0.05
    _fake_quota(monkeypatch, {"a": 0.04, "b": 500.0, "c": 300.0})
    CodeBuddyProvider().quota()
    assert _order() == ["b", "c", "a"]


def test_failed_query_never_demotes(three_accounts, monkeypatch):
    """拿不到额度 ≠ 没有额度：查询失败的账号位置不变。"""
    _fake_quota(monkeypatch, {"a": None, "b": 500.0, "c": 300.0})
    CodeBuddyProvider().quota()
    assert _order() == ["a", "b", "c"], "a 查询失败不该被贬到末尾"


def test_no_change_means_no_write(three_accounts, monkeypatch):
    """顺序没变就不该重写 index.json（额度查询是高频动作）。"""
    _fake_quota(monkeypatch, {"a": 500.0, "b": 400.0, "c": 300.0})
    calls = []
    monkeypatch.setattr(creds, "reorder_accounts",
                        lambda *a, **k: calls.append((a, k)))
    CodeBuddyProvider().quota()
    assert calls == [], "顺序未变时不该写盘"


def test_already_demoted_is_idempotent(three_accounts, monkeypatch):
    """已经排好序时重复查询不再动它。"""
    _fake_quota(monkeypatch, {"a": 0.0, "b": 500.0, "c": 300.0})
    CodeBuddyProvider().quota()
    assert _order() == ["b", "c", "a"]

    calls = []
    monkeypatch.setattr(creds, "reorder_accounts",
                        lambda *a, **k: calls.append((a, k)))
    CodeBuddyProvider().quota()
    assert calls == [], "已是目标顺序，第二次不该再写"


def test_write_failure_does_not_break_quota(three_accounts, monkeypatch):
    """调序失败只记日志，额度结果照常返回。"""
    _fake_quota(monkeypatch, {"a": 0.0, "b": 500.0, "c": 300.0})

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(creds, "reorder_accounts", boom)
    out = CodeBuddyProvider().quota()
    assert out is not None and out["items"], "额度照常返回"
    assert _order() == ["a", "b", "c"], "写盘失败时顺序不变"


def test_single_account_no_reorder(monkeypatch):
    """单账号没有「调到最后」的概念，不该报错也不该写盘。"""
    creds.save_account_cred(_cred("only", 0))
    _fake_quota(monkeypatch, {"only": 0.0})
    calls = []
    monkeypatch.setattr(creds, "reorder_accounts",
                        lambda *a, **k: calls.append((a, k)))
    CodeBuddyProvider().quota()
    assert calls == []


def test_reorder_only_touches_codebuddy_accounts(three_accounts, monkeypatch):
    """调序范围是本 provider 的账号，不碰其它通道的账号库。"""
    _fake_quota(monkeypatch, {"a": 0.0, "b": 500.0, "c": 300.0})
    CodeBuddyProvider().quota()
    # codebuddy 的 index.json 里仍然只有这三个账号
    assert sorted(_order()) == ["a", "b", "c"]
