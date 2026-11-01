"""请求日志页给 dumate 行补「真实消耗积分」。

dumate 的 chat 响应 usage 里**没有**单次积分（实测确认），但账号的
``records/usage`` 有真实逐笔扣减（秒级时间戳）。两者按时间戳最近邻 1:1
认领，容差内才写——认错一笔就是给别人安了个数字，宁可留空。
"""
from __future__ import annotations

import asyncio

import pytest

from buddy_proxy.dumate import checkin as dumate_checkin
from buddy_proxy.web.ui import queries


@pytest.fixture(autouse=True)
def _clear_usage_cache():
    dumate_checkin._USAGE_CACHE.clear()
    yield
    dumate_checkin._USAGE_CACHE.clear()


# --- fetch_usage_records_cached：分页 / 截窗 / 缓存 --------------------------

def test_cached_fetches_and_sorts(monkeypatch):
    calls = []

    def fake(*, start_ts, end_ts, page=1, limit=20, timeout=15.0):
        calls.append((start_ts, end_ts, page))
        return {"total_count": 2, "consumed_points": 0.0,
                "records": [{"ts": 200, "points": -3.0}, {"ts": 100, "points": -1.0}]}

    monkeypatch.setattr(dumate_checkin, "fetch_usage_records", fake)
    out = dumate_checkin.fetch_usage_records_cached(50, 250)
    assert out == [(100, -1.0), (200, -3.0)]  # 排好序
    assert len(calls) == 1


def test_cached_paginates_until_total_count(monkeypatch):
    # 一页 2000 条、totalCount 3000 → 翻两页；上游 limit 截到 2000
    def fake(*, start_ts, end_ts, page=1, limit=20, timeout=15.0):
        if page == 1:
            return {"total_count": 3000, "consumed_points": 0.0,
                    "records": [{"ts": i, "points": -1.0} for i in range(2000)]}
        return {"total_count": 3000, "consumed_points": 0.0,
                "records": [{"ts": 5000 + i, "points": -1.0} for i in range(1000)]}

    monkeypatch.setattr(dumate_checkin, "fetch_usage_records", fake)
    out = dumate_checkin.fetch_usage_records_cached(0, 10)
    assert len(out) == 3000


def test_cached_clamps_window_to_24h(monkeypatch):
    seen = {}

    def fake(*, start_ts, end_ts, page=1, limit=20, timeout=15.0):
        seen["start"], seen["end"] = start_ts, end_ts
        return {"total_count": 0, "consumed_points": 0.0, "records": []}

    monkeypatch.setattr(dumate_checkin, "fetch_usage_records", fake)
    dumate_checkin.fetch_usage_records_cached(0, 10 * 86400)
    assert seen["end"] - seen["start"] == dumate_checkin._USAGE_MAX_SPAN_S


def test_cached_returns_none_when_upstream_down(monkeypatch):
    monkeypatch.setattr(dumate_checkin, "fetch_usage_records", lambda **k: None)
    assert dumate_checkin.fetch_usage_records_cached(0, 100) is None
    # 失败不入缓存：下次仍会重试
    assert dumate_checkin._USAGE_CACHE == {}


def test_cached_reuses_superset_window(monkeypatch):
    """同起点、缓存窗口更晚结束的旧结果仍覆盖当前区间（流水只增不改）。"""
    n = [0]

    def fake(*, start_ts, end_ts, page=1, limit=20, timeout=15.0):
        n[0] += 1
        return {"total_count": 1, "consumed_points": 0.0,
                "records": [{"ts": 100, "points": -5.0}]}

    monkeypatch.setattr(dumate_checkin, "fetch_usage_records", fake)
    dumate_checkin.fetch_usage_records_cached(50, 1000)
    assert n[0] == 1
    out = dumate_checkin.fetch_usage_records_cached(50, 300)  # 更窄，应复用
    assert n[0] == 1 and out == [(100, -5.0)]


# --- _claim_dumate_credits：最近邻 1:1 + 容差 + 取绝对值 ----------------------

def test_claim_matches_nearest_within_tolerance():
    rows = [{"ts": 100.0}, {"ts": 202.0}, {"ts": 900.0}]
    recs = [(101.0, -4.5), (204.0, -8.0), (905.0, -1.0)]
    queries._claim_dumate_credits(rows, recs)
    assert rows[0]["credit"] == pytest.approx(4.5)   # 取绝对值
    assert rows[1]["credit"] == pytest.approx(8.0)
    assert rows[2]["credit"] == pytest.approx(1.0)
    assert all(r["credit_source"] == "dumate_usage_records" for r in rows)


def test_claim_leaves_too_far_unmatched():
    rows = [{"ts": 100.0}]
    queries._claim_dumate_credits(rows, [(130.0, -9.0)])   # 差 30s > 5s 容差
    assert "credit" not in rows[0]


def test_claim_is_one_to_one_no_reuse():
    """两条日志行抢同一笔流水时，只有最近的那条拿到，另一条留空。"""
    rows = [{"ts": 100.0}, {"ts": 103.0}]
    recs = [(100.5, -7.0)]
    queries._claim_dumate_credits(rows, recs)
    got = [r for r in rows if r.get("credit") is not None]
    assert len(got) == 1 and got[0]["ts"] == 100.0


def test_claim_keeps_existing_credit():
    """已经有实扣值的行不再被流水覆盖（CodeBuddy 那类 usage 直接带积分的通道）。"""
    rows = [{"ts": 100.0, "credit": 2.0}]
    queries._claim_dumate_credits(rows, [(100.0, -9.0)])
    assert rows[0]["credit"] == 2.0


# --- _attach_dumate_credits：只认 dumate、失败静默 --------------------------

def test_attach_only_touches_dumate_rows(monkeypatch):
    rows = [{"provider": "dumate", "ts": 100.0},
            {"provider": "codebuddy", "ts": 100.0, "credit": 1.5}]
    monkeypatch.setattr(
        dumate_checkin, "fetch_usage_records",
        lambda **k: {"total_count": 1, "consumed_points": 0.0,
                     "records": [{"ts": 100.0, "points": -6.0}]})
    asyncio.run(queries._attach_dumate_credits(rows))
    assert rows[0]["credit"] == pytest.approx(6.0)
    assert rows[1]["credit"] == 1.5   # 非 dumate 行没动


def test_attach_noop_when_no_dumate_rows(monkeypatch):
    def boom(**k):
        raise AssertionError("没有 dumate 行不该打上游")
    monkeypatch.setattr(dumate_checkin, "fetch_usage_records", boom)
    asyncio.run(queries._attach_dumate_credits([{"provider": "trae", "ts": 1.0}]))


def test_attach_survives_upstream_failure(monkeypatch):
    monkeypatch.setattr(dumate_checkin, "fetch_usage_records", lambda **k: None)
    rows = [{"provider": "dumate", "ts": 100.0}]
    asyncio.run(queries._attach_dumate_credits(rows))   # 不抛
    assert "credit" not in rows[0]