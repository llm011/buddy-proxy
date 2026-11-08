"""code 6004（单模型限流）只冷却「账号 + 模型」，不冷却账号本身。

## 背景

2026-11-08 用户实报：一个 codebuddy 账号用 `kimi-k3.1-pro` 返回

    {"code":6004,"msg":"您的使用量已超出频率限制，将在 2026-11-09 00:37:39
     重置，您也可以切换其他模型继续使用。"}

当时的处理把 429 一律当**账号级**失败，于是 6 个账号全部被冷却 5 分钟——
用户挑的只是一个限流的模型，结果连 deepseek / glm 都用不了，看起来像
「所有账号都坏了」。

## 上游语义（实测）

| 错误码 | 含义 | 正确处置 |
|---|---|---|
| `6004` | **单模型**频率限制（提示明说「可切换其他模型」） | 只冷却 (账号, 模型)，换号；账号仍可用 |
| `14018` | 账号额度耗尽（「请购买加量包」） | 冷却整个账号 |

实测佐证：同账号限制 kimi 时，`deepseek-v4.1-flash` / `glm-5.3` 仍返回 200。

## 实现

专用键：``account_id + "\\x00" + model``。账号级冷却仍用纯 id，两套互不干扰，
``accounts_status`` 等按纯 id 查询的视图不受影响。
"""

from __future__ import annotations

import pytest

from buddy_proxy.codebuddy_provider import failover
from buddy_proxy.codebuddy_provider.provider import (
    _MODEL_SCOPED_429_CODES,
    _is_account_error,
    _is_model_scoped_429,
)


@pytest.fixture(autouse=True)
def _clean():
    failover._tracker.clear_all()
    yield
    failover._tracker.clear_all()


def _exc(status: int, detail: str):
    from fastapi import HTTPException
    return HTTPException(status_code=status, detail=detail)


# --- 错误码识别 -------------------------------------------------------------


def test_6004_is_model_scoped():
    e = _exc(429, '{"code":6004,"msg":"您的使用量已超出频率限制"}')
    assert _is_model_scoped_429(e) is True
    # 仍然算「账号级错误」→ 会换号（别的号可能没被限）
    assert _is_account_error(e) is True


def test_14018_is_account_scoped():
    """额度耗尽才是账号级：不该被当成单模型限流。"""
    e = _exc(429, '{"error":{"data":{"code":14018,"msg":"额度已用尽"}}}')
    assert _is_model_scoped_429(e) is False
    assert _is_account_error(e) is True


def test_non_429_never_model_scoped():
    for status in (400, 401, 500):
        assert _is_model_scoped_429(_exc(status, '{"code":6004}')) is False


def test_model_scoped_codes_contains_6004():
    assert 6004 in _MODEL_SCOPED_429_CODES


# --- 复合键冷却 -------------------------------------------------------------


def test_model_cooldown_does_not_cool_account():
    """核心断言：冷却 (账号,模型) 后，账号本身仍可用。"""
    acct, model = "acct-1", "kimi-k3.1-pro"
    failover.mark_model_cooldown(acct, model, reason="test")

    assert failover.model_cooldown_left(acct, model)[0] > 0
    # 账号级冷却必须是 0——否则会连带封掉其它模型
    assert failover.cooldown_left(acct)[0] == 0


def test_model_cooldown_is_per_model():
    """同账号的其它模型不受影响。"""
    acct = "acct-1"
    failover.mark_model_cooldown(acct, "kimi-k3.1-pro")
    assert failover.model_cooldown_left(acct, "kimi-k3.1-pro")[0] > 0
    assert failover.model_cooldown_left(acct, "deepseek-v4.1-flash")[0] == 0
    assert failover.model_cooldown_left(acct, "glm-5.3")[0] == 0


def test_model_cooldown_is_per_account():
    """同模型换个账号不受影响。"""
    failover.mark_model_cooldown("acct-1", "kimi-k3.1-pro")
    assert failover.model_cooldown_left("acct-2", "kimi-k3.1-pro")[0] == 0


def test_available_accounts_skips_model_limited_account(monkeypatch):
    """选号时该模型的受限账号要被跳过，但它在别的模型上仍可选中。"""
    from buddy_proxy.codebuddy_provider.credentials import AccountRef

    accts = [AccountRef(id="a1", uid="u1", nickname="n1", priority=0, added_at=1),
             AccountRef(id="a2", uid="u2", nickname="n2", priority=1, added_at=2)]
    monkeypatch.setattr(failover, "list_accounts", lambda: accts)
    failover.mark_model_cooldown("a1", "kimi-k3.1-pro")

    got = [a.id for a in failover.available_accounts("cn", model="kimi-k3.1-pro")]
    assert got == ["a2"], "受限账号该被跳过"

    # 换模型：两个都可用
    got = [a.id for a in failover.available_accounts("cn", model="glm-5.3")]
    assert got == ["a1", "a2"]

    # 不传 model（签到/额度等按账号维度的调用方）：行为不变
    got = [a.id for a in failover.available_accounts("cn")]
    assert got == ["a1", "a2"]


def test_account_cooldown_still_blocks_all_models(monkeypatch):
    """账号级冷却（14018 那种）仍然对所有模型生效。"""
    from buddy_proxy.codebuddy_provider.credentials import AccountRef

    accts = [AccountRef(id="a1", uid="u1", nickname="n1", priority=0, added_at=1)]
    monkeypatch.setattr(failover, "list_accounts", lambda: accts)
    failover.mark_cooldown("a1", quota=True, reason="额度耗尽")

    assert failover.available_accounts("cn", model="kimi-k3.1-pro") == []
    assert failover.available_accounts("cn", model="glm-5.3") == []
    assert failover.available_accounts("cn") == []


def test_clear_cooldown_drops_model_keys_too():
    """删号时复合键要一起清掉，否则内存里留下孤儿键。"""
    failover.mark_model_cooldown("a1", "kimi-k3.1-pro")
    failover.mark_model_cooldown("a1", "glm-5.3")
    failover.mark_model_cooldown("a2", "kimi-k3.1-pro")
    assert failover.model_cooldown_left("a1", "kimi-k3.1-pro")[0] > 0

    failover.clear_cooldown("a1")

    assert failover.model_cooldown_left("a1", "kimi-k3.1-pro")[0] == 0
    assert failover.model_cooldown_left("a1", "glm-5.3")[0] == 0
    # 别的账号不受影响
    assert failover.model_cooldown_left("a2", "kimi-k3.1-pro")[0] > 0
