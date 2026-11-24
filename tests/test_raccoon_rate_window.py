"""raccoon 滑动窗口频率闸门：到顶换号、全顶直拒、不打上游。

## 为什么是这两个阈值（2026-11-24/25 实测，见 rate_limit.py 顶部注释）

5 分钟窗口 **32 次不触发 / 33 次触发** 200108；单分钟 18 次单独不触发。
所以主阈值在 5 分钟窗口（默认 28，留 ~15% 余量），单分钟 12 只是防突发。

## 这组测试锁什么

* **阈值边界**：28 放行、29 拦；12 放行、13 拦——改常量必须同步这里。
* **滑动**：老记录出窗后余量回来，不是「到了 N 点清零」的固定窗口。
* **全顶直拒**：所有号都到顶时 ``forward()`` 返回 429，且**一次上游都没发**
  （``client.send`` 零调用）——这是用户明确要的「都到极限就别再试了」。
* **到顶前换号**：号1 到顶、号2 有余量 → 请求打号2，号1 零上游调用。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from buddy_proxy.raccoon import rate_limit
from buddy_proxy.raccoon.rate_limit import (
    DEFAULT_RATE_PER_5MIN,
    DEFAULT_RATE_PER_MIN,
    AccountRateWindow,
)


# ---------------------------------------------------------------------------
# 单元：AccountRateWindow
# ---------------------------------------------------------------------------

def test_5min_window_boundary():
    """5 分钟窗口：28 放行，第 29 个拦。常量改了这里必须跟着改。"""
    assert DEFAULT_RATE_PER_5MIN == 28
    w = AccountRateWindow(per_minute=0, per_5min=28)  # 关 1 分钟，单测 5 分钟
    now = 1000.0
    for _ in range(28):
        assert w.has_capacity("a", now=now)
        w.record("a", now=now)
    assert w.has_capacity("a", now=now) is False, "第 29 个必须拦（实测 33 触发风控）"
    # counts() 是诊断口径，报真实计数、不随开关变：28 条都在 1 分钟内
    assert w.counts("a", now=now) == (28, 28)


def test_1min_window_boundary():
    """1 分钟窗口：12 放行，第 13 个拦（即使 5 分钟窗口还很空）。"""
    assert DEFAULT_RATE_PER_MIN == 12
    w = AccountRateWindow(per_minute=12, per_5min=0)
    now = 1000.0
    for _ in range(12):
        assert w.has_capacity("a", now=now)
        w.record("a", now=now)
    assert w.has_capacity("a", now=now) is False
    assert w.counts("a", now=now) == (12, 12)


def test_window_slides_not_fixed_bucket():
    """是滑动窗口不是固定桶：老记录出窗后余量回来，不需要等整点。

    固定桶会在 N 分钟边界处「瞬间清零」，允许边界前后各打满一次 = 实际翻倍；
    滑动窗口任意时刻回看都 ≤ 上限。
    """
    w = AccountRateWindow(per_minute=0, per_5min=3)
    t0 = 2000.0
    for i in range(3):
        w.record("a", now=t0 + i)
    # 三个都在窗内 → 满
    assert w.has_capacity("a", now=t0 + 10) is False
    # 第一条（t0）在 t0+300 出窗（含边界：<=cutoff 即出）
    assert w.has_capacity("a", now=t0 + 300) is True
    assert w.counts("a", now=t0 + 300) == (0, 2)
    # 第二条 t0+1 还在
    assert w.has_capacity("a", now=t0 + 300.5) is True
    assert w.counts("a", now=t0 + 300.5) == (0, 2)


def test_per_account_isolation():
    """窗口按账号隔离：号1 打满不影响号2。"""
    w = AccountRateWindow(per_minute=0, per_5min=2)
    now = 3000.0
    w.record("a1", now=now)
    w.record("a1", now=now)
    assert w.has_capacity("a1", now=now) is False
    assert w.has_capacity("a2", now=now) is True


def test_zero_disables_each_window():
    """0 = 关闭该窗口（与并发闸门口径一致）。两个都 0 等于不限。"""
    w = AccountRateWindow(per_minute=0, per_5min=0)
    now = 4000.0
    for _ in range(100):
        assert w.has_capacity("a", now=now)
        w.record("a", now=now)
    assert w.has_capacity("a", now=now) is True
    # record 在全关时连记都不记（省内存）
    assert w.counts("a", now=now) == (0, 0)


def test_recovery_in_waits_for_oldest_to_expire():
    """恢复时间 = 最老一条出窗的时刻，两个窗口取最大。"""
    w = AccountRateWindow(per_minute=0, per_5min=2)
    t0 = 5000.0
    w.record("a", now=t0)
    w.record("a", now=t0 + 30)
    # 满了；t0 在 t0+300 出窗
    left = w.recovery_in("a", now=t0 + 100)
    assert left == pytest.approx(200.0)
    # 还有余量时为 0
    assert w.recovery_in("b", now=t0 + 100) == 0.0


def test_recovery_in_1min_window_dominates_when_5min_has_room():
    """5 分钟没满、1 分钟满了 → 等 1 分钟内最老那条出 1 分钟窗。"""
    w = AccountRateWindow(per_minute=2, per_5min=100)
    t0 = 6000.0
    w.record("a", now=t0)
    w.record("a", now=t0 + 10)
    # 1 分钟满（2 条），5 分钟远未满
    assert w.has_capacity("a", now=t0 + 20) is False
    left = w.recovery_in("a", now=t0 + 20)
    assert left == pytest.approx(40.0)   # t0 在 t0+60 出 1 分钟窗


def test_clear_and_monotonic_now():
    """clear 能清；不传 now 时用 monotonic（不抛错且计数自洽）。"""
    w = AccountRateWindow(per_minute=5, per_5min=5)
    w.record("a")
    assert w.has_capacity("a") is True
    w.clear("a")
    assert w.counts("a") == (0, 0)
    w.record("b")
    w.clear()
    assert w.counts("b") == (0, 0)


# ---------------------------------------------------------------------------
# 集成：forward() 的选号行为
# ---------------------------------------------------------------------------

def _acct(aid: str):
    return SimpleNamespace(id=aid, alias=None, name=aid)


def _patch_forward_env(monkeypatch, provider, accts, *, window, client):
    """把 forward() 依赖的外部全部换成假的：不发任何网络。"""
    from buddy_proxy.raccoon import failover, provider as prov_mod

    monkeypatch.setattr(failover, "available_accounts", lambda model="": list(accts))
    monkeypatch.setattr(failover, "mark_cooldown", lambda *a, **k: 0.0)
    monkeypatch.setattr(failover, "mark_model_cooldown", lambda *a, **k: 0.0)
    monkeypatch.setattr(failover, "mark_relogin_needed", lambda *a, **k: 0.0)
    monkeypatch.setattr(failover, "cooldown_report", lambda: "")
    monkeypatch.setattr(failover, "model_cooldown_report", lambda m: "")
    monkeypatch.setattr(prov_mod, "ensure_account_token",
                        lambda aid, **k: (f"tok-{aid}", {}))
    monkeypatch.setattr(prov_mod, "_ensure_device_bound", lambda *a, **k: None)
    monkeypatch.setattr(rate_limit, "window", lambda: window)
    monkeypatch.setattr(rate_limit, "limiter",
                        lambda: rate_limit.AccountConcurrencyLimiter(max_concurrent=0))
    provider.ensure_auth = lambda: None  # type: ignore[method-assign]
    provider._get_client = _async_return(client)  # type: ignore[method-assign]
    provider._balance_now = lambda tok: 1.0  # type: ignore[method-assign]


def _async_return(value):
    async def _f(*a, **k):
        return value
    return _f


class _SpyClient:
    """记录 send 调用；按 token 返回一个能过首事件闸门的 200 SSE。"""

    def __init__(self):
        self.sent_tokens: list[str] = []

    def build_request(self, method, url, *, json=None, headers=None, timeout=None):
        return {"headers": headers or {}}

    async def send(self, req, *, stream=False):
        tok = (req.get("headers") or {}).get("xhx_token") or \
              str((req.get("headers") or {}).get("Authorization", ""))
        self.sent_tokens.append(tok)
        raise RuntimeError("stop-here")  # 到 send 就够了，不往下走闸门


def _drain_noop(*a, **k):
    pass


def test_forward_all_accounts_at_window_limit_returns_429_without_upstream(monkeypatch):
    """所有号都到顶 → 429「频率上限」，且**零上游调用**。

    这是用户明确要的行为：「如果所有号都到极限，都不要再试了，直接返回异常」。
    断言 send 零调用是关键——哪怕返回了 429，只要打过一次上游就算失败
    （那一次就是给风控窗口续命的信号）。
    """
    from buddy_proxy.raccoon.provider import RaccoonProvider

    w = AccountRateWindow(per_minute=0, per_5min=2)
    now = time.monotonic()
    for aid in ("a1", "a2"):
        w.record(aid, now=now)
        w.record(aid, now=now)

    provider = RaccoonProvider()
    spy = _SpyClient()
    _patch_forward_env(monkeypatch, provider, [_acct("a1"), _acct("a2")],
                       window=w, client=spy)
    monkeypatch.setattr("buddy_proxy.raccoon.provider._drain", _drain_noop)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        asyncio.run(provider.forward(
            {"model": "sn-glm-5-3", "stream": True, "messages": []}, "openai"))
    assert ei.value.status_code == 429
    msg = ei.value.detail["error"]["message"]
    assert "频率上限" in msg
    assert "未打上游" in msg
    assert spy.sent_tokens == [], "全部到顶时不该碰上游"
    # 恢复时间字段给调用方换通道用
    assert ei.value.detail["error"]["retry_after_s"] > 0


def test_forward_skips_account_at_limit_and_uses_next(monkeypatch):
    """号1 到顶、号2 有余量 → 打号2；号1 一次上游都不碰。

    「在达到阈值前切号」的核心：到顶的号不是打上去看 429 再退，而是选号时
    就跳过。
    """
    from buddy_proxy.raccoon.provider import RaccoonProvider

    w = AccountRateWindow(per_minute=0, per_5min=2)
    now = time.monotonic()
    w.record("a1", now=now)
    w.record("a1", now=now)

    provider = RaccoonProvider()
    spy = _SpyClient()
    _patch_forward_env(monkeypatch, provider, [_acct("a1"), _acct("a2")],
                       window=w, client=spy)

    try:
        asyncio.run(provider.forward(
            {"model": "sn-glm-5-3", "stream": True, "messages": []}, "openai"))
    except Exception:  # noqa: BLE001 - spy 故意在 send 抛错，只看谁被 send
        pass

    assert spy.sent_tokens and all("a2" in t for t in spy.sent_tokens), \
        f"只该打号2，实际 send 的 token：{spy.sent_tokens}"
    # 号2 的窗口被记了 1 条（真正发出去才记），号1 仍是预填的 2 条
    assert w.counts("a2")[1] == 1
    assert w.counts("a1")[1] == 2


def test_forward_records_only_when_actually_sent(monkeypatch):
    """窗口只记真正 send 的请求：选了号却在 send 前 continue 的不算。

    否则 token 刷新失败这种「没发出去」的也会吃掉窗口额度，白白提前换号。
    """
    from buddy_proxy.raccoon import provider as prov_mod
    from buddy_proxy.raccoon.provider import RaccoonProvider

    w = AccountRateWindow(per_minute=0, per_5min=5)
    provider = RaccoonProvider()
    spy = _SpyClient()
    _patch_forward_env(monkeypatch, provider, [_acct("a1")], window=w, client=spy)
    # token 刷新失败 → 走 AuthError 分支 continue，没到 send
    def _boom(aid, **k):
        raise prov_mod.AuthError("refresh failed")
    monkeypatch.setattr(prov_mod, "ensure_account_token", _boom)

    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        asyncio.run(provider.forward(
            {"model": "sn-glm-5-3", "stream": True, "messages": []}, "openai"))
    assert spy.sent_tokens == []
    assert w.counts("a1") == (0, 0), "没发出去不该占窗口额度"
