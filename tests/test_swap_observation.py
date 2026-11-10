"""换号观测：从真实流量学「这个模型的缓存是不是按账号隔离」。

负载均衡开关是通道维度的，但缓存隔离是**模型**维度的（2026-11-11 实测：
glm-5.3-flash / kimi-k3.1-pro / qwen3.8-max 换号仍 100% 命中，
deepseek-v4.1-flash 换号 0 命中）。对后者开均衡等于每个请求白丢一次缓存。

判断依据不能是静态猜测，也不能只看「同账号上一轮」的复用率——那个指标在
换号时必然取不到分母（键里带 account）。本文件钉住换号检测与观测链路：

- 检出「同一 (provider, model) 的上一条是别的账号」；
- 用**旧账号那一轮的 prompt** 当分母算复用率；
- 样本够且中位数低 → 判隔离 → reorder 不轮转该模型。
"""
from __future__ import annotations

from buddy_proxy.core import load_balance as lb
from buddy_proxy.core.metrics import MetricsCollector


def _rec(m, account, prompt, cached, model="deepseek-v4.1-flash"):
    m.record(provider="codebuddy", model=model, prompt_tokens=prompt,
             cached_tokens=cached, account=account)
    return list(m._recent)[-1]


def test_same_account_has_no_swap_observation():
    m = MetricsCollector(None)
    _rec(m, "A", 100000, 0)
    r = _rec(m, "A", 200000, 100000)
    assert r["swap_prev_prompt_tokens"] is None
    assert r["swap_prefix_reuse"] is None


def test_account_switch_uses_previous_accounts_prompt():
    """换号后的第一条：分母是**旧账号**那一轮的 prompt。"""
    m = MetricsCollector(None)
    _rec(m, "A", 100000, 0)
    r = _rec(m, "B", 100000, 0)          # B 没见过这段 → 0 命中
    assert r["swap_prev_prompt_tokens"] == 100000
    assert r["swap_prefix_reuse"] == 0.0


def test_shared_cache_model_shows_full_swap_reuse():
    """跨账号共享的模型：换号后仍应看到高复用率。"""
    m = MetricsCollector(None)
    _rec(m, "A", 100000, 0, model="glm-5.3-flash")
    r = _rec(m, "B", 100000, 100000, model="glm-5.3-flash")
    assert r["swap_prefix_reuse"] == 1.0


def test_observations_drive_account_scoped_verdict():
    """累积足够多的 0 命中后，该模型被判为「缓存按账号隔离」。"""
    lb.clear_swap_samples()
    try:
        m = MetricsCollector(None)
        for _ in range(lb.OBSERVE_MIN_SAMPLES + 2):
            _rec(m, "A", 100000, 0)
            _rec(m, "B", 100000, 0)      # 每次都换号且 0 命中
        assert lb.is_account_scoped("codebuddy", "deepseek-v4.1-flash") is True
    finally:
        lb.clear_swap_samples()


def test_shared_traffic_clears_scoped_verdict():
    """反过来：观测到持续命中，就要把静态表的结论推翻。"""
    lb.clear_swap_samples()
    try:
        m = MetricsCollector(None)
        for _ in range(lb.OBSERVE_MIN_SAMPLES + 2):
            _rec(m, "A", 100000, 0)
            _rec(m, "B", 100000, 100000)   # 换号仍完整命中
        assert lb.is_account_scoped("codebuddy", "deepseek-v4.1-flash") is False
    finally:
        lb.clear_swap_samples()


def test_restart_reload_restores_swap_tracking(tmp_path):
    """重启回读要重建「上一个账号是谁」，否则换号检测要等两次请求才重新生效。"""
    import json
    import time

    log = tmp_path / "metrics.jsonl"
    log.write_text(json.dumps({
        "ts": time.time(), "provider": "codebuddy", "model": "deepseek-v4.1-flash",
        "prompt_tokens": 100000, "cached_tokens": 0, "account": "A", "status": 200,
    }) + "\n", encoding="utf-8")

    m = MetricsCollector(log)
    r = _rec(m, "B", 100000, 0)
    assert r["swap_prev_prompt_tokens"] == 100000
    assert r["swap_prefix_reuse"] == 0.0


def test_empty_account_does_not_count_as_switch():
    """老记录/非多账号通道没有 account：不能每个请求都当成「换号」。"""
    m = MetricsCollector(None)
    _rec(m, "", 100000, 0)
    r = _rec(m, "", 200000, 100000)
    assert r["swap_prev_prompt_tokens"] is None
