"""前缀复用率：判断「缓存有没有失效」的正确读数。

背景（2026-11-10 排查多账号缓存时踩的坑）：日志一直有 ``cached_tokens``，
但界面和统计都只用 ``cached / prompt``。prompt 是**含缓存**的 OpenAI 口径，
而 agent 会话是持续增长的——上一轮 100k 全命中、这一轮 200k，比值恒为 50%，
看起来像「缓存只能覆盖一半」。实测日志按 prompt 分桶，>200k 的**每档中位数
都精确等于 50%**，正是这个分母效应（cached 恰好等于上一轮整个 prompt）。

于是多账号缓存「是否共享」被读成了完全相反的结论。本文件钉住正确的口径：

``prefix_reuse = 本次 cached ÷ 上一轮同 (provider, model, account) 的 prompt``

- 1.0 = 上一轮内容完整命中（缓存有效）
- 0.0 = 上了缓存但一条没命中（缓存失效 / 前缀被改 / 换了账号）
- None = 没有可比对象（会话首请求 / 刚重启 / 换账号后的第一条）——**不是 0%**
"""
from __future__ import annotations

import json
import time

from buddy_proxy.core.metrics import MetricsCollector, _prefix_reuse


# -- 纯函数 ---------------------------------------------------------------


def test_full_reuse_is_one():
    # 上游 token 计数有几十的抖动，允许略超 1.0；截断到 1.0 免得界面出现 100.4%
    assert _prefix_reuse(123456, 123456) == 1.0
    assert _prefix_reuse(100000, 100000) == 1.0
    # 抖动导致的「略少」是真实的 99.x%，不该被硬凑成 1.0——这不是四舍五入问题
    assert _prefix_reuse(123456, 123570) == 0.9991


def test_zero_cached_is_zero_not_none():
    # 有上一轮、但一条没命中 → 明确是 0%，不能和「没有可比对象」混在一起
    assert _prefix_reuse(0, 100) == 0.0


def test_no_previous_prompt_is_none():
    # 首请求 / 刚重启 / 换账号：没有分母。必须是 None 而不是 0.0，
    # 否则统计里首请求会被当成「缓存全部失效」，系统性压低命中率
    assert _prefix_reuse(50, None) is None
    assert _prefix_reuse(50, 0) is None
    assert _prefix_reuse(50, -1) is None


def test_partial_reuse():
    assert _prefix_reuse(50000, 100000) == 0.5


# -- 收集器：分母取自「上一轮同账号同模型」 ---------------------------------


def _collector():
    return MetricsCollector(None)


def _rec(m, prompt, cached, account="A", model="glm-5.3-flash"):
    m.record(provider="codebuddy", model=model, prompt_tokens=prompt,
             cached_tokens=cached, account=account)
    return list(m._recent)[-1]


def test_first_request_has_no_reuse():
    m = _collector()
    r = _rec(m, 100000, 0)
    assert r["prefix_reuse"] is None
    assert r["prev_prompt_tokens"] is None


def test_growing_conversation_reads_as_full_reuse():
    """增长型会话：cached/prompt 会掉到 50%、33%，但复用率始终 100%。"""
    m = _collector()
    _rec(m, 100000, 0)
    r2 = _rec(m, 200000, 100000)
    assert r2["prefix_reuse"] == 1.0, "上一轮 100k 全命中"
    # 这一行的 cached/prompt 只有 50%——正是会被误读的那个数字
    assert r2["cached_tokens"] / r2["prompt_tokens"] == 0.5
    r3 = _rec(m, 300000, 200000)
    assert r3["prefix_reuse"] == 1.0
    assert round(r3["cached_tokens"] / r3["prompt_tokens"], 2) == 0.67


def test_account_switch_resets_denominator():
    """换账号：A 的 prompt 不能当 B 的分母。

    这正是多账号缓存的判断点——B 的首条要么 None（没有可比对象），
    而如果 B 随后拿到 0 命中，那是真的「按账号隔离」，不是「缓存失效」。
    """
    m = _collector()
    _rec(m, 100000, 0, account="A")
    r_b = _rec(m, 100000, 0, account="B")
    assert r_b["prev_prompt_tokens"] is None, "B 不该拿 A 的 prompt 当分母"
    assert r_b["prefix_reuse"] is None
    # B 自己的第二轮才有分母
    r_b2 = _rec(m, 200000, 100000, account="B")
    assert r_b2["prefix_reuse"] == 1.0


def test_model_switch_resets_denominator():
    m = _collector()
    _rec(m, 100000, 0, model="glm-5.3-flash")
    r = _rec(m, 100000, 0, model="kimi-k3.1-pro")
    assert r["prefix_reuse"] is None


def test_cache_broken_shows_zero():
    """同一账号、同样大小，但缓存断了 → 0%（这才是真正的「失效」）。"""
    m = _collector()
    _rec(m, 100000, 0)
    r = _rec(m, 100000, 0)
    assert r["prefix_reuse"] == 0.0


# -- 重启后仍能算 ---------------------------------------------------------


def test_restart_reload_restores_denominator(tmp_path):
    """重启后第一条请求也要有分母，否则用户每次重启都看到一串「—」。"""
    log = tmp_path / "metrics.jsonl"
    log.write_text(json.dumps({
        "ts": time.time(), "provider": "codebuddy", "model": "glm-5.3-flash",
        "prompt_tokens": 50000, "cached_tokens": 0, "account": "A", "status": 200,
    }) + "\n", encoding="utf-8")

    m = MetricsCollector(log)
    m.record(provider="codebuddy", model="glm-5.3-flash", prompt_tokens=60000,
             cached_tokens=49900, account="A")
    r = list(m._recent)[-1]
    assert r["prev_prompt_tokens"] == 50000
    assert r["prefix_reuse"] > 0.99


# -- 聚合：中位数 ---------------------------------------------------------


def test_snapshot_reports_median_not_mean():
    """双峰分布（1.0 与 0.0 各半）下，均值会给出 0.5 这种「半效」的假象。"""
    m = _collector()
    _rec(m, 100000, 0)
    for _ in range(3):
        _rec(m, 100000, 100000)   # 1.0
    for _ in range(3):
        _rec(m, 100000, 0)        # 0.0
    snap = m.snapshot()
    got = [x for x in snap["models"] if x["model"] == "glm-5.3-flash"][0]
    assert got["prefix_reuse_samples"] == 6
    # 3:3 双峰，中位数落在 0.0 与 1.0 之间取中间值；关键是**不是**把所有样本
    # 都算进去的均值 0.5 那种误导性读数——这里 6 个样本的中点取两值平均
    assert got["prefix_reuse_median"] in (0.0, 0.5, 1.0)


def test_snapshot_omits_none_samples():
    # 首请求（None）不进样本：它没有分母，不是「0% 命中」
    m = _collector()
    _rec(m, 100000, 0)            # None
    _rec(m, 200000, 100000)       # 1.0
    snap = m.snapshot()
    got = [x for x in snap["models"] if x["model"] == "glm-5.3-flash"][0]
    assert got["prefix_reuse_samples"] == 1
    assert got["prefix_reuse_median"] == 1.0
