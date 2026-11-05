"""Trae 缓存命中估算（会话增量口径）的离线回归测试。

背景（2026-11-05 用户从官方「使用记录」截取 12 条 DeepSeek-V4.1-Flash 实测积分）：
那天是一段 agent 长会话，prompt 从 72,850 连续涨到 79,537。旧实现把整个 prompt
按全价算，12 条合计估 **23.58**，实际只扣 **4.46**——高估 5.3 倍（仅这 12 条）。

根因不是单价错（旧表 in 34.1 / out 139.0 积分/M，实测 39.9 / 158.7，只差 15~20%），
而是**上游确实按缓存计费、但不上报缓存 token 数**：

- ``token_usage`` 事件里明明有 ``cache_read_input_tokens`` /
  ``cache_creation_input_tokens`` 字段，实测在 80k prompt 连发、固定
  session_id、原生 chat_v3 通道下**恒为 0**；
- 账单侧却显示只收了「增量」的钱。

于是改口径：同账号同模型上一条 prompt 记作会话基线 P0，本次 P，
未命中 = max(P−P0, 0)、命中 = P − 未命中。三档单价联合最小二乘解出
miss=39.93 / hit=0.835 / out=158.73 积分每 M，回代 12 条合计 4.46 对 4.46。

这里守住四件事：
1. 12 条真实样本逐条复现（这是口径正确性的唯一硬证据）；
2. 会话首请求按全量计（基线不存在 = 无缓存）；
3. prompt 回落（新会话/换会话）同样按全量计，不会算出负数；
4. 上游真给了 ``cached_tokens`` 时优先用上游值（将来上游开始上报就自动生效）。

运行：
    .venv/bin/python -m pytest tests/test_trae_cache_credit.py -v
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from buddy_proxy.core.credit_estimate import (  # noqa: E402
    estimate_credit,
    remember_prompt,
    reset_baselines,
)

MODEL = "deepseek-v4.1-flash"
ACCOUNT = "2767937599176883"

#: (prompt_tokens, completion_tokens, 官方账单实扣积分)，按时间正序。
#: 截图 2026/11/05 01:37~01:38，与 logs/metrics.jsonl 同窗口逐条对应。
REAL_SAMPLES = [
    (72850, 131, 2.93),
    (73016, 121, 0.08),
    (73254, 245, 0.11),
    (73590, 315, 0.12),
    (74231, 172, 0.12),
    (74935, 1256, 0.29),
    (75542, 119, 0.11),
    (76079, 493, 0.16),
    (76613, 141, 0.11),
    (78402, 160, 0.16),
    (78939, 440, 0.16),
    (79537, 142, 0.11),
]


@pytest.fixture(autouse=True)
def _clean_baselines():
    """每条测试前后都清基线：模块级记忆会串到下一条测试。"""
    reset_baselines()
    yield
    reset_baselines()


def _estimate_seq(samples, account=ACCOUNT):
    """按 observability 的真实顺序跑一串请求：先估算、再记基线。"""
    out = []
    for prompt, completion, _actual in samples:
        out.append(estimate_credit("trae", MODEL, prompt, completion, 0, account=account))
        remember_prompt(account, MODEL, prompt)
    return out


def test_real_billing_samples_reproduce_offline_screenshot():
    """12 条真实样本逐条复现，且合计与账单一致。

    这是把「会话增量 = 缓存命中」这个假设钉死的地方。若哪天有人把公式改回
    「全量按全价」，合计会跳到 23.58，这条测试立刻红。
    """
    got = _estimate_seq(REAL_SAMPLES)
    want = [s[2] for s in REAL_SAMPLES]
    assert sum(got) == pytest.approx(sum(want), abs=0.01), f"合计不符: {got}"
    # 逐条对照（宽 0.02，等价于展示末位的四舍五入/截断差）
    mismatched = [(s, g) for s, g in zip(REAL_SAMPLES, got) if abs(g - s[2]) > 0.02]
    assert not mismatched, f"逐条偏差过大: {mismatched}"


def test_first_request_of_session_pays_full_price():
    """会话首请求没有基线，整段 prompt 按未命中全价计。

    实测首条 72,850 prompt 扣 2.93 —— 正是 miss 价 39.93/M 的结果。
    """
    first = estimate_credit("trae", MODEL, 72850, 131, 0, account=ACCOUNT)
    assert first == pytest.approx(2.93, abs=0.02)


def test_incremental_prompt_is_only_a_few_cents():
    """紧接上一轮、prompt 只涨了几百 token 时，只该收增量的钱（~0.1 分）。"""
    remember_prompt(ACCOUNT, MODEL, 72850)
    second = estimate_credit("trae", MODEL, 73016, 121, 0, account=ACCOUNT)
    assert second == pytest.approx(0.09, abs=0.02), second
    assert second < 0.2, "增量口径下续轮必须很便宜"


def test_prompt_shrink_treats_all_as_miss_not_negative():
    """prompt 回落（新会话/换了对话）时全部按未命中，绝不出现负数积分。

    基线 79,537 → 本次 500（全新短会话）：旧的「减基线」写法会得到负的
    miss，进而算出负积分。必须夹到 [0, prompt]。
    """
    remember_prompt(ACCOUNT, MODEL, 79537)
    v = estimate_credit("trae", MODEL, 500, 10, 0, account=ACCOUNT)
    assert v is not None and v >= 0, v
    # 500 token 全价 ≈ 0.02（1e-6 量级四舍五入到 0.02）
    assert v == pytest.approx(0.02, abs=0.02)


def test_baseline_is_per_account_and_model():
    """基线按 (账号, 模型) 隔离：别的账号/模型的请求不干扰本次增量。"""
    remember_prompt(ACCOUNT, MODEL, 72850)
    # 另一个账号没有基线 → 按全量算
    other = estimate_credit("trae", MODEL, 73016, 121, 0, account="another-account")
    assert other == pytest.approx(2.93, abs=0.05), other
    # 同账号但换了模型 → 也没有基线
    other_model = estimate_credit("trae", "glm-5.3-flashx", 73016, 121, 0, account=ACCOUNT)
    assert other_model is not None


def test_upstream_cached_tokens_take_precedence():
    """上游真给了 cached_tokens 就照它算，不再用会话增量近似。

    为将来上游开始上报缓存留的口子：一旦 ``cache_read_input_tokens`` 变成
    非 0，估算自动走精确口径，不必再改代码。
    """
    remember_prompt(ACCOUNT, MODEL, 72850)
    # 假设上游说 72000 命中、850 未命中
    v = estimate_credit("trae", MODEL, 72850, 131, 72000, account=ACCOUNT)
    assert v is not None
    # 未命中 850 全价 ≈ 0.034 + 命中 72000 ≈ 0.060 + 输出 131 ≈ 0.021 → 0.115
    assert v == pytest.approx(0.11, abs=0.02), v
    # 关键：与「忽略上游缓存数、只看基线」的结果不同（那条会给 0.09），
    # 说明确实走了 upstream_cached 分支而不是基线近似。
    assert v != estimate_credit("trae", MODEL, 72850, 131, 0, account=ACCOUNT)


def test_no_account_falls_back_to_full_price():
    """没有账号标识（测试/异常路径）时退回全价，不去猜缓存。"""
    v = estimate_credit("trae", MODEL, 72850, 131, 0, account="")
    assert v == pytest.approx(2.93, abs=0.02), v


def test_models_without_hit_rate_keep_old_behaviour():
    """表里没配命中价的模型（老三元组）行为不变：输入全按全价。

    glm-5.3-flashx 仍是三元组，不能被新的缓存分支误伤。
    """
    remember_prompt(ACCOUNT, "glm-5.3-flashx", 100000)
    full = estimate_credit("trae", "glm-5.3-flashx", 100000, 500, 0, account="")
    with_base = estimate_credit("trae", "glm-5.3-flashx", 100000, 500, 0, account=ACCOUNT)
    assert full == with_base, "三元组不应受会话基线影响"
