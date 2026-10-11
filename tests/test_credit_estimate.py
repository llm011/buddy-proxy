"""积分估算（credit_estimate）测试：纯离线，用真实账单校准样本回归。

校准样本来源（2026-09-06 15:40~15:42）：代理 metrics.jsonl 的 trae token
记录与 Trae 网页「使用记录」真实积分按分钟对齐（10 组，glm-5.3-flash）。
会话首请求（样本 1）实测约为公式值 2.7 倍（隐藏注入计费所致），单独断言
该已知偏差，不参与稳态精度断言。

运行：
    .venv/bin/python -m pytest tests/test_credit_estimate.py -v
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from buddy_proxy.core.credit_estimate import (  # noqa: E402
    _estimate_zcode,
    estimate_credit,
)
from buddy_proxy.providers.zcode import credit_discount_now  # noqa: E402
from buddy_proxy.trae import config as trae_config  # noqa: E402

# (输入 token, 输出 token, 网页真实积分)；顺序即代理记录时间序
CALIBRATION_SAMPLES = [
    (27863, 147, 0.45),  # 会话首请求：已知偏差样本
    (32495, 111, 0.21),
    (32671, 44, 0.21),
    (32516, 96, 0.21),
    (32181, 25, 0.20),
    (32214, 52, 0.16),
    (33134, 101, 0.17),
    (33111, 408, 0.19),
    (33533, 60, 0.17),
    (43225, 300, 0.34),
]


def test_steady_state_samples_within_tolerance():
    """稳态请求（样本 2~10）：估算与真实积分误差 ≤ 0.09。

    多数样本误差 ≤0.05；43225-in 那条偏差 +0.08，与首请求同类
    （部分隐藏注入计费，token_usage 未体现），属「粗估」口径的预期散布。
    """
    for prompt, completion, real in CALIBRATION_SAMPLES[1:]:
        est = estimate_credit("trae", "glm-5.3-flash", prompt, completion)
        assert est is not None
        assert abs(est - real) <= 0.09, (prompt, completion, est, real)


def test_session_first_request_known_deviation():
    """会话首请求：真实值显著高于公式值（隐藏注入计费），估算必然低估。"""
    prompt, completion, real = CALIBRATION_SAMPLES[0]
    est = estimate_credit("trae", "glm-5.3-flash", prompt, completion)
    assert est is not None
    assert est < real * 0.6  # 0.17 vs 0.45


def test_multiplier_scales_across_models():
    """倍率换算：glm-5.3（x0.39）估算 ≈ 同 token 下 flash（x0.06）的 39/6 倍。"""
    flash = estimate_credit("trae", "glm-5.3-flash", 30000, 100)
    big = estimate_credit("trae", "glm-5.3", 30000, 100)
    assert flash is not None and big is not None
    assert abs(big / flash - 0.39 / 0.06) < 0.01


def test_alias_model_resolves_multiplier():
    """外部别名（MODEL_MAP 映射）也能命中倍率表。"""
    direct = estimate_credit("trae", "glm-5.3-flash", 30000, 100)
    assert direct is not None


def test_non_trae_and_unknown_model_return_none():
    """无系数/倍率表的通道与模型返回 None（不显示估算）。"""
    assert estimate_credit("codebuddy", "glm-5.3", 30000, 100) is None
    assert estimate_credit("trae", "no-such-model", 30000, 100) is None
    assert estimate_credit("trae", "glm-5.3-flash", 0, 0) is None


# ---- trae 实测计价：倍率调整自动等比缩放 ----

def test_measured_rates_scale_with_current_multiplier(monkeypatch):
    """实测单价随 MODEL_CREDITS 倍率等比缩放（官方调价只改倍率表即可）。

    校准倍率与当前倍率一致时，估算 == 原绝对值口径；把倍率翻倍后应线性
    翻倍——不需要动 MEASURED_CREDIT_RATES。
    """
    base = estimate_credit("trae", "glm-5.3-flashx", 30000, 100)
    assert base == 0.71  # 30000×2.351e-5 + 100×7.242e-5，校准倍率 x0.31 即现状
    monkeypatch.setitem(trae_config.MODEL_CREDITS, "glm-5.3-flashx", "x0.62")
    assert estimate_credit("trae", "glm-5.3-flashx", 30000, 100) == 1.43


def test_measured_rates_fall_back_when_multiplier_missing(monkeypatch):
    """拿不到当前倍率时不缩放（用校准绝对值），而不是报错或归零。"""
    monkeypatch.delitem(trae_config.MODEL_CREDITS, "glm-5.3-flashx", raising=False)
    assert estimate_credit("trae", "glm-5.3-flashx", 30000, 100) == 0.71


def test_measured_rates_zero_multiplier_means_free(monkeypatch):
    """x0.00 是有效倍率（限时免费促销档），估算应归零而不是当成「查不到」。"""
    monkeypatch.setitem(trae_config.MODEL_CREDITS, "glm-5.3-flashx", "x0.00")
    assert estimate_credit("trae", "glm-5.3-flashx", 30000, 100) == 0.0


def test_measured_rates_tolerate_legacy_two_tuple(monkeypatch):
    """遗留两项格式（无校准倍率）不抛异常，退化到不缩放。

    该函数跑在流式收尾的 finally 里，抛 ValueError 会把上游已成功的请求
    变成客户端 500——配置形状问题绝不能炸请求路径。
    """
    monkeypatch.setitem(trae_config.MEASURED_CREDIT_RATES, "glm-5.3-flashx",
                        (2.351e-5, 7.242e-5))
    assert estimate_credit("trae", "glm-5.3-flashx", 30000, 100) == 0.71


# ---- zcode：GLM Coding Plan 官方抵扣公式 ----
# 公式：(未命中输入×Input + 缓存命中×Cached + 输出×Output) / 10000 × 时段折扣
# glm-5.3 系数 (6.9, 1.7, 24)；flash/turbo (2.3, 0.56, 8)。
def _bj_ts(y, m, d, hh, mm=0):
    """构造 UTC+8 指定时刻的 epoch 秒（折扣函数按北京时间判断高峰）。"""
    import datetime
    return datetime.datetime(
        y, m, d, hh, mm, tzinfo=datetime.timezone(datetime.timedelta(hours=8))
    ).timestamp()


PEAK_TS = _bj_ts(2026, 10, 14, 15, 0)     # 周三 15:00（活动期外）→ 1×
OFFPEAK_TS = _bj_ts(2026, 10, 14, 12, 0)  # 周三 12:00 → 0.5×
WEEKEND_TS = _bj_ts(2026, 10, 17, 15, 0)  # 周六 15:00 → 0.5×
PROMO_TS = _bj_ts(2026, 10, 1, 15, 0)     # 活动期内的工作日高峰 → 仍 0.5×


def test_zcode_discount_windows():
    """高峰（工作日 14–18 UTC+8）1×，夜间/周末 0.5×，活动期全天 0.5×。"""
    assert credit_discount_now(PEAK_TS) == 1.0
    assert credit_discount_now(OFFPEAK_TS) == 0.5
    assert credit_discount_now(WEEKEND_TS) == 0.5
    assert credit_discount_now(PROMO_TS) == 0.5


def test_zcode_official_formula():
    """官方系数公式（glm-5.3）：(2000×6.9 + 8000×1.7 + 1000×24)/10000 = 5.14。"""
    # prompt 为 OpenAI 口径（含缓存命中）：10000 = 未命中 2000 + 缓存 8000
    assert _estimate_zcode("glm-5.3", 10000, 1000, 8000, ts=PEAK_TS) == 5.14
    assert _estimate_zcode("glm-5.3", 10000, 1000, 8000, ts=OFFPEAK_TS) == 2.57


def test_zcode_estimate_via_public_api():
    """公开入口 dispatch：zcode + 已知模型必出值（折扣随时钟，但恒非 None）。"""
    est = estimate_credit("zcode", "glm-5.3", 10000, 1000, cached_tokens=8000)
    assert est is not None


def test_zcode_flash_and_turbo_share_coeffs():
    """GLM-5-Turbo 上游自动切 Flash：两者同 token 估算值相同。"""
    flash = _estimate_zcode("glm-5.3-flash", 10000, 1000, 8000, ts=PEAK_TS)
    turbo = _estimate_zcode("glm-5-turbo", 10000, 1000, 8000, ts=PEAK_TS)
    # (2000×2.3 + 8000×0.56 + 1000×8)/10000 = 1.708
    assert flash == turbo == 1.71


def test_zcode_cache_hit_costs_less():
    """缓存命中按 Cached 系数（约 0.24×Input）计：全命中比全未命中便宜得多。"""
    all_uncached = _estimate_zcode("glm-5.3", 10000, 0, 0, ts=PEAK_TS)
    all_cached = _estimate_zcode("glm-5.3", 10000, 0, 10000, ts=PEAK_TS)
    assert all_uncached == round(10000 * 6.9 / 10000, 2)      # 6.9
    assert all_cached == round(10000 * 1.7 / 10000, 2)        # 1.7


def test_zcode_unknown_model_and_zero_tokens_return_none():
    """flashx 套餐未开放（无系数）→ None；零 token → None。"""
    assert _estimate_zcode("glm-5.3-flashx", 10000, 10, 0, ts=PEAK_TS) is None
    assert _estimate_zcode("glm-5.3", 0, 0, 0, ts=PEAK_TS) is None
    assert estimate_credit("zcode", "glm-5.3-flashx", 30000, 100) is None


def test_zcode_cached_clamped_to_prompt():
    """异常记录（cached > prompt）时 cached 收敛到 prompt，不出现负的未命中输入。"""
    # prompt=100, cached=200 → clamp 100：(0×6.9 + 100×1.7 + 10×24)/10000×1 = 0.04
    est = _estimate_zcode("glm-5.3", 100, 10, 200, ts=PEAK_TS)
    assert est == round((0 * 6.9 + 100 * 1.7 + 10 * 24) / 10000, 2)
