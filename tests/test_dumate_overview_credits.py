"""总览按渠道/按天积分图：dumate 的**账号级**每日消耗要出现。

2026-11-01 用户报障：dumate 的 chat usage 不带单次积分（实测），逐条指标恒为
``None``，``credit_sum`` 一直是 0，被 ``chart_credits.js`` 整条过滤掉——总览的
积分图里看不到 dumate。

修法分两层：

- ``dumate.billing`` 按天查账号真实账单（``records/usage``）。**必须按天**：
  实测宽窗会吞掉早期独立日期（[now-24h] 与 [now-14d] 返回同一合计），逐日相加才
  是全量。缓存 + 后台线程刷新，调用方永不阻塞。
- ``queries._inject_dumate_credits`` 把它补成 ``model_daily`` 的合成桶。
"""
from __future__ import annotations

import time

import pytest

from buddy_proxy.dumate import billing
from buddy_proxy.web.ui import queries


@pytest.fixture(autouse=True)
def _reset_billing():
    billing.reset_cache()
    yield
    billing.reset_cache()


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


# --- billing：按天累计 + 定稿跳过 + 失败保留旧值 -----------------------------

def test_refresh_accumulates_one_entry_per_day(monkeypatch):
    """逐日扫一遍：每天一条，值来自当日账单（不是宽窗合计）。"""
    seen_days = []

    def fake(*, start_ts, end_ts, page=1, limit=20, timeout=15.0):
        seen_days.append(time.strftime("%Y-%m-%d", time.localtime(start_ts)))
        return {"total_count": 1, "consumed_points": 12.5, "records": []}

    monkeypatch.setattr(billing, "fetch_usage_records", fake)
    billing._refresh()
    got = billing.get_day_totals()
    assert len(got) == billing._DAYS
    assert all(v == pytest.approx(12.5) for v in got.values())
    assert _today() in got
    # 每天单独查（窗口=当日 0 点起算），不是一次宽窗
    assert len(set(seen_days)) == billing._DAYS


def test_refresh_skips_final_days_on_rescan(monkeypatch):
    """已结束的日期定稿后重扫不再打上游——只补还在变的「今天」。"""
    calls = []

    def fake(*, start_ts, end_ts, page=1, limit=20, timeout=15.0):
        calls.append(time.strftime("%Y-%m-%d", time.localtime(start_ts)))
        return {"total_count": 1, "consumed_points": 1.0, "records": []}

    monkeypatch.setattr(billing, "fetch_usage_records", fake)
    billing._refresh()
    first = len(calls)
    calls.clear()
    billing._refresh()   # 直接再扫一轮：已结束的日期都定稿了，只该补「今天」
    assert calls == [_today()], "重扫只该查未定稿的今天"
    assert first == billing._DAYS


def test_failed_day_keeps_previous_value(monkeypatch):
    """某天上游失败时保留旧值，不把已算出的数字抹成 0。"""
    monkeypatch.setattr(
        billing, "fetch_usage_records",
        lambda **k: {"total_count": 1, "consumed_points": 33.0, "records": []})
    billing._refresh()
    assert billing.get_day_totals()[_today()] == pytest.approx(33.0)
    # 今天还没定稿 → 下次会重查；模拟这次失败
    monkeypatch.setattr(billing, "fetch_usage_records", lambda **k: None)
    billing._refresh()
    assert billing.get_day_totals()[_today()] == pytest.approx(33.0)


def test_refresh_swallows_upstream_errors(monkeypatch):
    """上游抛异常也不该外抛——调用方（/ui/api/stats）不能被它拖挂。"""
    def boom(**k):
        raise RuntimeError("network down")
    monkeypatch.setattr(billing, "fetch_usage_records", boom)
    billing._refresh()           # 不抛
    assert billing._cache == {}  # 一个都没查到 → 空缓存、等退避后重试


def test_get_day_totals_returns_dict_without_blocking(monkeypatch):
    """get_day_totals 只读缓存、永不阻塞：首次（缓存空）直接返回 {}。"""
    monkeypatch.setattr(billing, "_maybe_refresh", lambda: None)
    assert billing.get_day_totals() == {}


# --- _inject_dumate_credits：合成桶插进 model_daily --------------------------

def _snap(model_daily=None):
    return {"model_daily": model_daily or []}


def test_inject_adds_synthetic_dumate_buckets(monkeypatch):
    monkeypatch.setattr(billing, "get_day_totals",
                        lambda: {"2026-11-01": 100.0, "2026-10-31": 250.0})
    snap = _snap([{"date": "2026-11-01", "provider": "dumate", "model": "m",
                   "count": 5, "credit_sum": 0.0, "credit_est_sum": 0.0}])
    queries._inject_dumate_credits(snap)
    md = snap["model_daily"]
    synth = [b for b in md if b["provider"] == "dumate" and b["model"] == ""]
    by_day = {b["date"]: b for b in synth}
    assert by_day["2026-11-01"]["credit_sum"] == pytest.approx(100.0)
    assert by_day["2026-10-31"]["credit_sum"] == pytest.approx(250.0)
    assert by_day["2026-10-31"]["count"] == 0
    # 原逐模型桶还在（count=5），只是同一天多了合成桶
    orig = [b for b in md if b["provider"] == "dumate" and b["model"] == "m"]
    assert len(orig) == 1 and orig[0]["count"] == 5


def test_inject_skips_day_already_covered_by_usage(monkeypatch):
    """某天指标里已经带了 dumate 的积分（上游开始在 usage 回传）→ 不叠加成双倍。"""
    monkeypatch.setattr(billing, "get_day_totals", lambda: {"2026-11-01": 100.0})
    snap = _snap([{"date": "2026-11-01", "provider": "dumate", "model": "m",
                   "count": 5, "credit_sum": 7.0, "credit_est_sum": 0.0}])
    queries._inject_dumate_credits(snap)
    dumate = [b for b in snap["model_daily"] if b["provider"] == "dumate"]
    assert len(dumate) == 1 and dumate[0]["credit_sum"] == pytest.approx(7.0)


def test_inject_noop_when_upstream_empty(monkeypatch):
    monkeypatch.setattr(billing, "get_day_totals", lambda: {})
    snap = _snap([{"date": "2026-11-01", "provider": "trae", "model": "a",
                   "count": 1, "credit_sum": 1.0, "credit_est_sum": 0.0}])
    before = [dict(b) for b in snap["model_daily"]]
    queries._inject_dumate_credits(snap)
    assert snap["model_daily"] == before   # 没 dumate 数据，什么都不补


def test_inject_does_not_touch_other_providers(monkeypatch):
    monkeypatch.setattr(billing, "get_day_totals", lambda: {"2026-11-01": 50.0})
    snap = _snap([{"date": "2026-10-30", "provider": "codebuddy", "model": "a",
                   "count": 2, "credit_sum": 3.0, "credit_est_sum": 0.0}])
    queries._inject_dumate_credits(snap)
    cb = [b for b in snap["model_daily"] if b["provider"] == "codebuddy"]
    assert len(cb) == 1 and cb[0]["credit_sum"] == pytest.approx(3.0)


def test_inject_survives_billing_exception(monkeypatch):
    def boom():
        raise RuntimeError("upstream gone")
    monkeypatch.setattr(billing, "get_day_totals", boom)
    snap = _snap([{"date": "2026-11-01", "provider": "dumate", "model": "m",
                   "count": 1, "credit_sum": 0.0, "credit_est_sum": 0.0}])
    queries._inject_dumate_credits(snap)   # 不抛
    assert len(snap["model_daily"]) == 1

def test_refresh_is_clean_noop_without_login():
    """隔离目录里没有 auth.json/.cookie-key → 逐日查询一律返回 None，不触网、缓存空。

    回归本 PR 暴露出的隐患：/ui/api/stats 现在会触发账号级账单扫描，若 dumate
    的 App 用户目录没被隔离，本机装了 App 时测试就会读真实登录态、打上游。
    """
    billing.reset_cache()
    billing._refresh()          # 用真 fetch_usage_records；隔离目录无凭据 → 全 None
    assert billing.get_day_totals() == {}
