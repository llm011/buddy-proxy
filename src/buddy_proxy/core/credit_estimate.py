"""按 token 数粗估单次请求积分消耗（上游不给单次计费时的兜底口径）。

Trae 从 2026-09 起改为积分制按 token 计费，但 chat 流只回 token 数、不带
单次积分；IDE JWT 能打的 UG/pay 接口里也没有按请求粒度的消耗流水（候选
endpoint 全部 404，网页「使用记录」走另一套 web 会话体系）。因此按官方
「模型倍率」口径粗估，并用真实账单校准。

实测校准（2026-09-06，10 组代理 token 记录 ↔ Trae 网页「使用记录」真实
积分的分钟级对齐样本，glm-5.3-flash x0.06，输入 2.8 万~4.3 万 token）：

- 公式 ``(输入 + 输出) / 1M × 100 × 倍率``：稳态请求误差 ±0.04 积分；
- 会话首请求实测 0.45 vs 公式 0.17（约 2.7 倍）——疑似上游把隐藏注入
  （guard/教学 prompt）计入计费但未体现在 token_usage 里，代理侧无法
  识别，此类请求按公式值低估处理；
- 重复 ~32k 输入的请求积分不变（0.16~0.21 随输入线性微动），说明该通道
  缓存即便命中也按全价计（或根本未命中），故缓存 token 不做折扣加权。

zcode（GLM Coding Plan）的 /api/monitor/usage/quota/limit 计数器批量聚合
更新（0~90s 抖动）且无按请求流水——单次积分官方虽无 API 回传，但订阅文档
公布了完整抵扣公式与系数（见 providers/zcode.py CREDIT_COEFFS），按它算
是官方口径而非粗估；系数变化（新模型/调价/活动）时改那张表即可。
"""

from __future__ import annotations

import time
from typing import Any

# 积分 / 1M token / 倍率单位。校准：glm-5.3-flash（x0.06）实测 ≈6.0~6.2
# 积分/M，即基准 100×倍率；别家倍率体系（MODEL_CREDITS）同源换算。
CREDITS_PER_M_PER_MULT = 100.0


def _load_trae_tables() -> tuple[dict[str, str], dict[str, str]]:
    """惰性加载 Trae 的倍率表与别名映射（避免模块级循环导入）。"""
    try:
        from ..trae.config import MODEL_CREDITS, MODEL_MAP
        return MODEL_CREDITS, MODEL_MAP
    except Exception:
        return {}, {}


def _load_zcode_tables() -> tuple[dict[str, tuple[float, float, float]], Any]:
    """惰性加载 zcode 的抵扣系数表与时段折扣函数（避免模块级循环导入）。"""
    try:
        from ..providers.zcode import CREDIT_COEFFS, credit_discount_now
        return CREDIT_COEFFS, credit_discount_now
    except Exception:
        return {}, None


def _estimate_zcode(
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cached_tokens: int | None = 0,
    ts: float | None = None,
) -> float | None:
    """zcode（GLM Coding Plan）：按官方抵扣公式精确计算，非倍率粗估。

    消耗积分数 = (未命中输入×Input + 缓存命中×Cached + 输出×Output) / 10000，
    再乘以请求时刻的时段折扣（工作日 14–18 UTC+8 高峰 1×，其余 0.5×）。
    ``prompt_tokens`` 是 metrics 归一后的 OpenAI 口径（含缓存命中），先减掉
    cached 得未命中输入。缓存**写入** token 上游 anthropic 端点不单列
    （无 cache_creation_input_tokens），已并入 prompt 按 Input 系数计——
    与官方计费的差异未知，按保守（不加价）处理。
    """
    coeffs, discount_fn = _load_zcode_tables()
    rate = coeffs.get((model or "").strip().lower())
    if rate is None or discount_fn is None:
        return None
    ci, cc, co = rate
    prompt = max(0, int(prompt_tokens or 0))
    cached = min(prompt, max(0, int(cached_tokens or 0)))
    out = max(0, int(completion_tokens or 0))
    base = ((prompt - cached) * ci + cached * cc + out * co) / 10000.0
    if base <= 0:
        return None
    return round(base * discount_fn(ts), 2)


def _multiplier(provider: str, model: str) -> float | None:
    """解析模型倍率（"x0.06" → 0.06）；无表/无该模型返回 None。"""
    if provider != "trae":
        return None
    credits, model_map = _load_trae_tables()
    internal = model_map.get(model, model)
    raw = credits.get(internal) or credits.get(internal.lower())
    if not raw:
        return None
    try:
        return float(str(raw).lower().lstrip("x×"))
    except ValueError:
        return None


def _load_trae_measured_rates() -> dict[str, tuple[float, ...]]:
    """惰性加载 trae 实测计价表（避免模块级循环导入）。

    元组两种形状（按长度分派，见 ``trae.config.MEASURED_CREDIT_RATES``）：
    ``(输入价, 输出价, 校准倍率)`` 或
    ``(未命中输入价, 输出价, 校准倍率, 命中输入价)``。
    """
    try:
        from ..trae.config import MEASURED_CREDIT_RATES
        return MEASURED_CREDIT_RATES
    except Exception:
        return {}


#: 「上一次 prompt 大小」的记忆：key 为 ``(account, model)``，值是 (prompt_tokens, ts)。
#: Trae 的 chat 流不回缓存命中数（``cache_read_input_tokens`` 实测恒 0，见
#: ``MEASURED_CREDIT_RATES`` 注释），但**账单侧确实按缓存计费**——于是用「同账号
#: 同模型上一条 prompt」近似上下文基线：本次比它多出来的部分算未命中、其余算命中。
#:
#: 用模块级 dict 而非请求上下文：流式落库发生在读线程/收尾 finally，没有可穿透
#: 的请求作用域，且基线本身就是跨请求状态（「上一条」天然是全局最近一条）。
_LAST_PROMPT: dict[tuple[str, str], tuple[int, float]] = {}
#: 基线有效期（秒）。Trae 的缓存池按会话长期驻留（实测隔十几小时仍命中），但
#: 过久之前的 prompt 与当前会话大概率无关，留着只会把命中算多（低估积分）。
_BASELINE_TTL_S = 6 * 3600
#: 记忆表上限：多账号 × 多模型，正常几个条目；异常增长时按时间裁掉最旧的。
_LAST_PROMPT_MAX = 256


def _baseline_prompt(account: str, model: str) -> int | None:
    """取该 (账号, 模型) 的上一条 prompt 大小；过期或没有返回 None。"""
    entry = _LAST_PROMPT.get((account, model))
    if entry is None:
        return None
    prompt, ts = entry
    if time.time() - ts > _BASELINE_TTL_S:
        _LAST_PROMPT.pop((account, model), None)
        return None
    return prompt


def remember_prompt(account: str, model: str, prompt_tokens: int) -> None:
    """记下本次 prompt 大小，供下一条算增量；超上限时裁掉最旧的。"""
    _LAST_PROMPT[(account, model)] = (prompt_tokens, time.time())
    if len(_LAST_PROMPT) > _LAST_PROMPT_MAX:
        oldest = min(_LAST_PROMPT, key=lambda k: _LAST_PROMPT[k][1])
        _LAST_PROMPT.pop(oldest, None)


def reset_baselines() -> None:
    """清空会话基线（测试隔离用；生产不调用）。"""
    _LAST_PROMPT.clear()


def estimate_credit(
    provider: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cached_tokens: int | None = 0,
    account: str = "",
) -> float | None:
    """按 token 估算单次积分消耗；无系数/倍率表或无 token 数据时返回 None。

    zcode 走官方抵扣公式（见 :func:`_estimate_zcode`）；trae 优先用实测计价表
    （``MEASURED_CREDIT_RATES``，输入/输出分开计价、与官方账单对账校准——
    「100×倍率/1M 总 tokens」公式对已实测模型偏差约 9 倍），未实测的退回倍率
    粗估。实测单价按「当前倍率/校准倍率」等比缩放：官方调倍率时照常更新
    ``MODEL_CREDITS`` 即可自动跟价，不必重跑对账。返回值统一 round 到 2 位
    （与 Trae 网页使用记录的展示粒度一致）。

    缓存命中口径：上游给了 ``cached_tokens`` 就用它；没给（Trae 实测恒 0）则
    退化为「本账号本模型上一条 prompt」作基线算增量（见 ``_LAST_PROMPT``）。
    没配命中价的模型（老三元组）忽略该分支，行为与改动前一致。
    """
    if provider in ("zcode", "glm"):
        # glm 是 zcode 的子类通道（同上游同套餐计价），官方系数原样复用；
        # 漏掉它 glm 请求的 Credit 列会掉进倍率粗估并因无倍率表返回 None。
        return _estimate_zcode(model, prompt_tokens, completion_tokens, cached_tokens)
    rates = _load_trae_measured_rates().get(model) if provider == "trae" else None
    if rates is not None:
        # 元组形状容错：旧格式（仅两项）或手工补录漏写校准倍率时退化到不缩放，
        # 不让 ValueError 冒到调用方——本函数跑在流式收尾的 finally 里，
        # 抛出会把上游已成功的请求变成客户端 500。
        in_rate, out_rate = rates[0], rates[1]
        calib_mult = rates[2] if len(rates) > 2 else 0.0
        # 命中价只在四元组里出现；没有就全部输入按全价（旧行为）。
        hit_rate = rates[3] if len(rates) > 3 else None
        mult = _multiplier(provider, model)
        # 拿不到当前倍率（表里无此模型、被删/改名）时不缩放：退回校准绝对值。
        # mult 为 0（x0.00 免费档，如促销期）是**有效倍率**，应正常清零估算。
        # 校准倍率为 0 属配置错误，同样不缩放。
        if mult is None or not calib_mult:
            scale = 1.0
        else:
            scale = mult / calib_mult
        prompt = max(0, int(prompt_tokens or 0))
        completion = max(0, int(completion_tokens or 0))
        if prompt == 0 and completion == 0:
            return None  # 无 token 数据（错误响应/解析失败）不记估算
        if hit_rate is None:
            total = (in_rate * prompt + out_rate * completion) * scale
            return round(max(0.0, total), 2)
        # ---- 缓存感知：拆出「未命中输入」与「命中输入」 ----
        upstream_cached = max(0, int(cached_tokens or 0))
        if upstream_cached > 0:
            # 上游真给了缓存数（OpenAI 口径：prompt 含缓存），直接照用。
            cached = min(prompt, upstream_cached)
            miss = prompt - cached
        elif account:
            # Trae 实测不上报：按同账号同模型的上一条 prompt 近似会话基线。
            base = _baseline_prompt(account, model)
            miss = prompt if base is None else max(prompt - base, 0)
            miss = min(miss, prompt)
            cached = prompt - miss
        else:
            # 没有账号标识（离线/测试/异常路径）就退回全价，不去猜。
            cached = 0
            miss = prompt
        total = (in_rate * miss + hit_rate * cached + out_rate * completion) * scale
        return round(max(0.0, total), 2)
    mult = _multiplier(provider, model)
    if mult is None:
        return None
    total = max(0, int(prompt_tokens or 0)) + max(0, int(completion_tokens or 0))
    if total <= 0:
        return None
    return round(total / 1e6 * CREDITS_PER_M_PER_MULT * mult, 2)
