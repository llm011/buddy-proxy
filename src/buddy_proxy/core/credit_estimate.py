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

antigravity（Google Antigravity 免费额度）既无积分字段也无公开抵扣公式，
唯一可观测的计费信号是 ``fetchAvailableModels.quotaInfo.remainingFraction``
——池级水位（同一 quota 组内的模型共享）。每条请求收尾读一次水位、与上次读数
相减结算本条，见 :func:`record_quota_mark`：

- 读数**必须来自固定连接**：水位按连接/接入节点缓存，新建连接会读到另一份
  快照（数值跳动、还会回升）。固定连接上逐条单调下降，落差就是本条请求的消耗；
- 水位精度只有 4 位有效数字（0.7551521 这类），一条中等请求落差约 0.0001，
  换算约 0.1 积分；实测落差还能跳一大截（同样规模的一条读到 6.41）——**单次
  估算是粗的**，看量级即可；
- 上游会**回升**（空转 30 s 实测 +0.0014，比一次大请求的消耗还多）：回升那条记
  ``—`` 并把基线跟到新读数，否则下一次的落差会虚增；
- 水位**只在接近上限时才降**（轻用时长期读 1.0），所以估出来的 0 不等于真免费；
- 读不到水位 / 水位没动时返回 None（记 ``—``，不瞎猜）。
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


#: kiro 的模型倍率：``ListAvailableModels`` 的 ``rateMultiplier`` 字段。
#: 由 provider 在拉目录时登记（``{"claude-opus-5.5": 2.0, ...}``）。
#: 用模块级 dict 而不是 import kiro 包：本模块被 metrics 在导入期依赖，
#: 反向 import provider 会形成环（且估算路径不该触发网络/凭证逻辑）。
_KIRO_RATES: dict[str, float] = {}


def register_kiro_rates(rates: dict[str, float]) -> None:
    """登记 kiro 模型倍率（由 kiro provider 拉完目录后调用）。

    kiro 上游**不返回 token 数**，所以这里的倍率只能配合「按字符估 token」用，
    精度远不如它自己的 meteringEvent 实扣——实扣拿得到时优先用它，
    本表只是拿不到时的兜底。
    """
    if isinstance(rates, dict):
        _KIRO_RATES.clear()
        _KIRO_RATES.update({str(k): float(v) for k, v in rates.items()
                            if isinstance(v, (int, float)) and not isinstance(v, bool)})


def _multiplier(provider: str, model: str) -> float | None:
    """解析模型倍率（"x0.06" → 0.06）；无表/无该模型返回 None。"""
    if provider == "kiro":
        # kiro 的倍率来自上游 rateMultiplier（单位是「倍」），与 trae 的
        # x0.06 是同一套语义：数值本身就是乘数。
        return _KIRO_RATES.get(model)
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


#: antigravity 的「上次观测水位」：key 为 ``(account, quota_key)``。
#: 按账号分记，多账号互不污染。
_QUOTA_MARKS: dict[tuple[str, str], tuple[float, float]] = {}
#: antigravity 水位落差 → 积分的标定系数。水位是 4 位有效数字，一条中等请求
#: 的落差约 0.0001~0.0003；放大到 1000 让主流差落在 0.1~0.3 的可读量级，
#: 而不是被 round(2) 抹成 0。**标定值，不是官方口径。**
_ANTIGRAVITY_CREDITS_PER_WATERMARK = 1000.0


def record_quota_mark(account: str, quota_key: str,
                      remaining: float | None,
                      tokens: int = 0) -> tuple[float | None, float | None]:
    """把一次水位观测并入账本，返回 ``(本条请求的积分, 上次观测时刻)``。

    antigravity provider 在每条请求**收尾时**调用一次（``_credit_probe``），
    口径是「上次读数 − 本次读数」，也就是这条请求消耗的水位。

    这个口径成立的前提是**读数来自同一条连接**：2026-11-16 实测（打 daily
    端点、同一账号同一模型）

    * 每次新建连接读：``0.7551521 / 0.7562798 / 0.7592398`` 随机跳动，还
      会「回升」——水位按连接/接入节点缓存，换连接就换了一份快照；
    * 复用同一条连接连读 8 次：恒为同一值；
    * 在固定连接上连发 4 条请求，逐条收尾读数 ``0.7562798 → 0.75617707 →
      0.7560655 → 0.7559592 → 0.7558411``，每条落差与该条请求体量对得上。

    所以 provider 侧必须用固定连接（``antigravity.provider._fetch_watermark``）；
    本函数只管相减，不做跨请求分摊。

    **看不见的消耗记 None，不记 0**：

    * ``remaining`` 读不到（上游省略 ``quotaInfo`` / 请求失败）→ None，且**不动**
      上一条读数，下次观测仍是「与最后一条真实读数的落差」；
    * 水位没动或**回升**（上游重算/回填；实测空转 30 s 回升 0.0014）→ None，
      并把基线更新到本次读数——回升后若继续拿旧基线比，下一次的落差会虚增。
    * 落差小到 round(2) 归零 → None（4 位精度下短区间很常见，记 0 等于断言
      「没花钱」，那不真）。

    ``tokens`` 现在只用于诊断，不参与结算（保留参数是为了日志/后续口径调整）。
    """
    key = (str(account or ""), str(quota_key or ""))
    if not account or not quota_key:
        return None, None
    if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
        prev = _QUOTA_MARKS.get(key)
        return None, (prev[1] if prev else None)
    observed = float(remaining)
    prev = _QUOTA_MARKS.get(key)
    _QUOTA_MARKS[key] = (observed, time.time())
    if prev is None:
        return None, None  # 首次观测只铺基线
    drop = prev[0] - observed
    if drop <= 1e-9:
        return None, prev[1]
    credits = round(drop * _ANTIGRAVITY_CREDITS_PER_WATERMARK, 2)
    return (credits if credits > 0 else None), prev[1]


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

    antigravity 不经过这里：它的上游没有 token 口径的积分公式，消耗只体现在
    池水位落差上，所以由 provider 在请求收尾时用固定连接查一次水位、与上一次
    读数相减结算本条并写进 metrics（见 ``antigravity.provider._credit_probe``
    与 :func:`record_quota_mark`）；本函数对它恒返回 None。
    """
    if provider == "antigravity":
        return None  # 消耗按池水位落差结算，见 record_quota_mark
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
