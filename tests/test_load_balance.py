"""负载均衡核心：按剩余额度加权的账号轮转（core/load_balance.py）。

覆盖三类不变量：
1. ``QuotaBoard`` 的读数语义（未知 / 过期 / 负数 / 真·耗尽要区分开）；
2. ``weighted_order`` 不丢账号、不改原列表、额度多的确实排得更前；
3. 退路：读数全无时仍然打散，不会退化成「永远同一个账号」。
"""
from __future__ import annotations

import random

import pytest

from buddy_proxy.core.load_balance import (
    MAX_QUOTA_AGE_S,
    MIN_WEIGHT,
    QuotaBoard,
    weighted_order,
)


# -- QuotaBoard 读数语义 ----------------------------------------------------


def test_unknown_account_gets_min_weight():
    board = QuotaBoard()
    assert board.weight_of("nope") == MIN_WEIGHT
    assert board.known("nope") is False
    assert board.remaining_of("nope") is None


def test_update_and_read_back():
    board = QuotaBoard()
    board.update("a", 1200)
    assert board.weight_of("a") == 1200
    assert board.known("a") is True
    assert board.remaining_of("a") == 1200


def test_zero_remaining_is_known_but_min_weight():
    # 「额度真的耗尽」与「查询失败」在权重上都拿下限，但 known 必须能分开：
    # 界面要显示「剩 0」而不是「未知」。
    board = QuotaBoard()
    board.update("a", 0)
    assert board.known("a") is True
    assert board.remaining_of("a") == 0
    assert board.weight_of("a") == MIN_WEIGHT


def test_negative_remaining_clamped():
    board = QuotaBoard()
    board.update("a", -5)
    assert board.weight_of("a") == MIN_WEIGHT


def test_update_none_clears_previous():
    # 查询失败时传 None，不能保留上一轮的旧数字——那会让权重撒谎。
    board = QuotaBoard()
    board.update("a", 500)
    board.update("a", None)
    assert board.known("a") is False
    assert board.weight_of("a") == MIN_WEIGHT


def test_stale_reading_expires():
    board = QuotaBoard()
    board.update("a", 9999)
    later = MAX_QUOTA_AGE_S + 1
    # 用显式 now 注入，不依赖真实时钟（否则测试要 sleep 15 分钟）
    import time as _t
    now = _t.time() + later
    assert board.weight_of("a", now=now) == MIN_WEIGHT
    assert board.known("a", now=now) is False


def test_set_zero_overrides():
    board = QuotaBoard()
    board.update("a", 5000)
    board.set_zero("a")
    assert board.remaining_of("a") == 0


def test_update_many():
    board = QuotaBoard()
    board.update_many({"a": 100, "b": 200})
    assert board.remaining_of("a") == 100
    assert board.remaining_of("b") == 200


def test_nan_rejected():
    board = QuotaBoard()
    board.update("a", float("nan"))
    assert board.known("a") is False


# -- weighted_order 行为 ----------------------------------------------------


class _Acct:
    def __init__(self, id):
        self.id = id

    def __repr__(self):
        return f"A({self.id})"


def test_empty_returns_empty():
    assert weighted_order([], lambda a: 1) == []


def test_single_returns_single():
    a = _Acct("a")
    assert weighted_order([a], lambda a: 1) == [a]


def test_never_drops_or_duplicates():
    accts = [_Acct(x) for x in "abcdef"]
    weights = {"a": 0, "b": 10, "c": 1, "d": 0.0, "e": -3, "f": 100}
    ordered = weighted_order(accts, lambda x: weights[x.id], rng=random.Random(7))
    assert sorted(x.id for x in ordered) == sorted(x.id for x in accts)


def test_does_not_mutate_input():
    accts = [_Acct(x) for x in "abc"]
    before = list(accts)
    weighted_order(accts, lambda a: 1, rng=random.Random(1))
    assert accts == before


def test_high_quota_tends_to_come_first():
    # 统计口径：大额度账号排第一的次数应显著多于小额度账号。
    # 固定 seed 保证结果可复现（避免 flaky）。
    accts = [_Acct("rich"), _Acct("poor")]
    weights = {"rich": 1000, "poor": 1}
    rng = random.Random(20261110)
    first = {"rich": 0, "poor": 0}
    for _ in range(400):
        ordered = weighted_order(accts, lambda a: weights[a.id], rng=rng)
        first[ordered[0].id] += 1
    # 理论概率 rich 约 1000/1001 ≈ 99.9%；留足够宽容度只要求明显占优
    assert first["rich"] > first["poor"] * 10


def test_all_zero_still_rotates():
    # 全部读数为 0（例如额度查询全失败）时不能退化成永远用第一个：
    # 那样「额度全是 0」的通道会被钉死在同一个账号上。
    accts = [_Acct(x) for x in "abc"]
    rng = random.Random(3)
    seen_first = set()
    for _ in range(60):
        ordered = weighted_order(accts, lambda a: 0, rng=rng)
        seen_first.add(ordered[0].id)
    assert seen_first == {"a", "b", "c"}


def test_bad_weight_values_do_not_crash():
    accts = [_Acct(x) for x in "abc"]
    bad = {"a": None, "b": "oops", "c": float("nan")}

    def w(a):
        v = bad[a.id]
        return v  # type: ignore[return-value]

    ordered = weighted_order(accts, w, rng=random.Random(5))
    assert len(ordered) == 3


def test_deterministic_with_same_rng():
    accts = [_Acct(x) for x in "abc"]
    w = {"a": 10, "b": 20, "c": 30}
    a1 = weighted_order(accts, lambda a: w[a.id], rng=random.Random(42))
    a2 = weighted_order(accts, lambda a: w[a.id], rng=random.Random(42))
    assert [x.id for x in a1] == [x.id for x in a2]


# -- provider 维度：board / enabled / reorder --------------------------------


def test_board_is_per_provider_and_cached():
    from buddy_proxy.core import load_balance as lb

    lb.clear_boards()
    a = lb.board("codebuddy")
    b = lb.board("qoder")
    assert a is not b
    assert lb.board("codebuddy") is a, "同一通道必须复用同一个表"


def test_enabled_reads_state_switch():
    from types import SimpleNamespace

    from buddy_proxy.core import load_balance as lb

    assert lb.enabled(SimpleNamespace(load_balance={"codebuddy"}), "codebuddy") is True
    assert lb.enabled(SimpleNamespace(load_balance={"qoder"}), "codebuddy") is False
    assert lb.enabled(SimpleNamespace(load_balance=set()), "codebuddy") is False
    # state 缺失 / 没这个属性：一律当没开（行为与历史一致）
    assert lb.enabled(None, "codebuddy") is False
    assert lb.enabled(SimpleNamespace(), "codebuddy") is False


def test_reorder_is_noop_when_disabled():
    from types import SimpleNamespace

    from buddy_proxy.core import load_balance as lb

    accts = [_Acct(x) for x in "abc"]
    got = lb.reorder(SimpleNamespace(load_balance=set()), "codebuddy", accts)
    assert [x.id for x in got] == ["a", "b", "c"]
    assert got is not accts, "必须返回副本，否则调用方的列表会被就地改掉"


def test_reorder_is_noop_with_single_account():
    from types import SimpleNamespace

    from buddy_proxy.core import load_balance as lb

    lb.board("codebuddy").clear()
    lb.board("codebuddy").update("a", 1)
    got = lb.reorder(SimpleNamespace(load_balance={"codebuddy"}), "codebuddy", [_Acct("a")])
    assert [x.id for x in got] == ["a"]


def test_reorder_uses_per_provider_board():
    """两个通道的权重互不串味：同一个账号 id 在不同通道有不同的剩余。"""
    from types import SimpleNamespace

    from buddy_proxy.core import load_balance as lb

    lb.clear_boards()
    lb.board("codebuddy").update("a", 1)
    lb.board("codebuddy").update("b", 10000)
    lb.board("qoder").update("a", 10000)
    lb.board("qoder").update("b", 1)
    st = SimpleNamespace(load_balance={"codebuddy", "qoder"})

    accts = [_Acct("a"), _Acct("b")]
    cb_firsts = {lb.reorder(st, "codebuddy", accts)[0].id for _ in range(40)}
    qd_firsts = {lb.reorder(st, "qoder", accts)[0].id for _ in range(40)}
    assert cb_firsts == {"b"}, cb_firsts
    assert qd_firsts == {"a"}, qd_firsts


def test_weights_from_items_skips_none():
    from buddy_proxy.core import load_balance as lb

    lb.clear_boards()
    lb.weights_from_items("x", {"a": 100, "b": None})
    assert lb.board("x").remaining_of("a") == 100
    assert lb.board("x").known("b") is False


# -- remaining_by_account：从额度条目反推每账号剩余 -------------------------


def test_remaining_by_account_groups_by_index():
    from buddy_proxy.core.load_balance import remaining_by_account

    items = [
        {"label": "AG #1 · Gemini 组", "remaining": 900},
        {"label": "AG #1 · Claude+GPT 组", "remaining": 100},   # 同账号另一条要相加
        {"label": "AG #2 · Gemini 组", "remaining": 50},
    ]
    assert remaining_by_account(items) == {"1": 1000.0, "2": 50.0}


def test_remaining_by_account_single_account_has_empty_key():
    from buddy_proxy.core.load_balance import remaining_by_account

    assert remaining_by_account([{"label": "总额度", "remaining": 7}]) == {"": 7.0}


def test_remaining_by_account_skips_notices_and_non_numeric():
    from buddy_proxy.core.load_balance import remaining_by_account

    items = [
        {"label": "CodeBuddy 额度查询失败", "remaining": "2/6 个账号取不到额度",
         "query_failed": True},
        {"label": "Kiro #1 · PRO", "remaining": None},
        {"label": "Kiro #1 · Credit", "remaining": 12},
    ]
    assert remaining_by_account(items) == {"1": 12.0}


def test_remaining_by_account_handles_cn_label():
    """带空格/中文的通道名（「CodeBuddy 海外版 #1」）也要能切出序号。"""
    from buddy_proxy.core.load_balance import remaining_by_account

    items = [{"label": "CodeBuddy 海外版 #2 · 订阅套餐", "remaining": 300}]
    assert remaining_by_account(items) == {"2": 300.0}


# -- 缓存隔离名单：模型维度 ------------------------------------------------


def test_known_account_scoped_model_from_static_table():
    """deepseek-v4.1-flash 实测换号 0 命中 → 静态表判为隔离。"""
    from buddy_proxy.core import load_balance as lb

    lb.clear_swap_samples()
    assert lb.is_account_scoped("codebuddy", "deepseek-v4.1-flash") is True
    # 实测换号 100% 命中的模型不在表里 → 共享
    assert lb.is_account_scoped("codebuddy", "glm-5.3-flash") is False
    assert lb.is_account_scoped("codebuddy", "kimi-k3.1-pro") is False
    assert lb.is_account_scoped("qoder", "qwen3.8-max") is False


def test_observation_overrides_static_table():
    """线上观测优先于静态表：上游改了行为，几十个请求内就能纠正。"""
    from buddy_proxy.core import load_balance as lb

    lb.clear_swap_samples()
    try:
        # 观测说 glm 换号后全 miss（与静态表相反）
        for _ in range(lb.OBSERVE_MIN_SAMPLES):
            lb.record_swap_reuse("codebuddy", "glm-5.3-flash", 0.0)
        assert lb.is_account_scoped("codebuddy", "glm-5.3-flash") is True
        # 反向：deepseek 观测到共享
        for _ in range(lb.OBSERVE_MIN_SAMPLES):
            lb.record_swap_reuse("codebuddy", "deepseek-v4.1-flash", 1.0)
        assert lb.is_account_scoped("codebuddy", "deepseek-v4.1-flash") is False
    finally:
        lb.clear_swap_samples()


def test_insufficient_samples_fall_back_to_static():
    """样本不足不采信：一两次偶发 miss 可能是上游抖动，不能推翻结论。"""
    from buddy_proxy.core import load_balance as lb

    lb.clear_swap_samples()
    try:
        for _ in range(lb.OBSERVE_MIN_SAMPLES - 1):
            lb.record_swap_reuse("codebuddy", "glm-5.3-flash", 0.0)
        assert lb.observed_verdict("codebuddy", "glm-5.3-flash") is None
        assert lb.is_account_scoped("codebuddy", "glm-5.3-flash") is False  # 静态表
    finally:
        lb.clear_swap_samples()


def test_observation_uses_median_not_mean():
    """双峰样本下中位数才代表真实情形（与日志指标同一个坑）。"""
    from buddy_proxy.core import load_balance as lb

    lb.clear_swap_samples()
    try:
        for _ in range(lb.OBSERVE_MIN_SAMPLES - 1):
            lb.record_swap_reuse("x", "m", 1.0)
        lb.record_swap_reuse("x", "m", 0.0)
        # 均值 ≈0.875，中位数 1.0 → 判共享
        assert lb.observed_verdict("x", "m") is False
    finally:
        lb.clear_swap_samples()


def test_sample_buffer_is_bounded():
    from buddy_proxy.core import load_balance as lb

    lb.clear_swap_samples()
    try:
        for _ in range(lb.OBSERVE_MIN_SAMPLES * 10):
            lb.record_swap_reuse("x", "m", 0.5)
        assert len(lb._SWAP_SAMPLES[("x", "m")]) <= lb.OBSERVE_MIN_SAMPLES * 4
    finally:
        lb.clear_swap_samples()


def test_reorder_skips_account_scoped_model():
    """核心行为：缓存按账号隔离的模型，即使开关打开也不重排。"""
    from types import SimpleNamespace

    from buddy_proxy.core import load_balance as lb

    lb.clear_boards()
    lb.clear_swap_samples()
    try:
        lb.board("codebuddy").update("a", 1)
        lb.board("codebuddy").update("b", 10000)
        st = SimpleNamespace(load_balance={"codebuddy"})
        accts = [_Acct("a"), _Acct("b")]

        # deepseek：隔离 → 永远原顺位
        for _ in range(30):
            got = lb.reorder(st, "codebuddy", accts, model="deepseek-v4.1-flash")
            assert [x.id for x in got] == ["a", "b"], "隔离模型不该被轮转"

        # glm：共享 → 权重生效（额度大的 b 几乎总在第一）
        firsts = {lb.reorder(st, "codebuddy", accts, model="glm-5.3-flash")[0].id
                  for _ in range(30)}
        assert firsts == {"b"}
    finally:
        lb.clear_swap_samples()


def test_reorder_without_model_still_balances():
    """不传 model（老调用方/新通道漏传）时照常均衡，不能静默失效。"""
    from types import SimpleNamespace

    from buddy_proxy.core import load_balance as lb

    lb.clear_boards()
    lb.board("codebuddy").update("a", 1)
    lb.board("codebuddy").update("b", 10000)
    st = SimpleNamespace(load_balance={"codebuddy"})
    firsts = {lb.reorder(st, "codebuddy", [_Acct("a"), _Acct("b")])[0].id
              for _ in range(30)}
    assert firsts == {"b"}


def test_account_scoped_models_lists_for_ui():
    from buddy_proxy.core import load_balance as lb

    lb.clear_swap_samples()
    assert "deepseek-v4.1-flash" in lb.account_scoped_models("codebuddy")
    assert "glm-5.3-flash" not in lb.account_scoped_models("codebuddy")
