"""raccoon 多账号 failover：稳定主备 + 内存冷却。

核心冷却状态机收敛到 :class:`buddy_proxy.core.account_failover.CooldownTracker`
（antigravity/qoder/trae/kimi 共用）。本模块只保留 raccoon 自己的失败语义：

- **账号级冷却**（60s）：401/403——会话失效或被风控。
- **额度冷却**（300s）：402 / 积分耗尽。
- **模型级冷却**（``账号\\x00模型``）：``429 user_model_rate_limited``——这是
  **账号×模型**的频率窗口，恢复中位数 25 分钟，只挡这一个模型，不该把整号
  摘掉（同账号其它模型仍可用）。
- **需重新登录**（长冷却）：会话被上游判死（``needs_relogin``）。有手机号+
  密码时优先自动重登（见 ``login.relogin_account``），没有才冷却并提示用户。

永远从优先级最高的可用账号开始（导入顺序即优先级），坏账号靠冷却被临时
摘出候选。冷却只放内存，进程重启清零的代价只是每账号重探一次。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..core.account_failover import CooldownTracker, diagnose_available
from .credentials import (
    AccountRef,
    has_relogin_cred,
    list_accounts,
    load_account_cred,
    token_expiry_epoch,
)

log = logging.getLogger(__name__)

#: 「账号 × 模型」复合冷却键的分隔符（账号 id 不含 \x00）。
_MODEL_KEY_SEP = "\x00"

#: 模块级冷却状态机（账号级，收敛自各通道的 copy-paste 实现）。
_tracker = CooldownTracker()

#: 活引用 tracker 内部 dict（测试直接清空用）。
_cooldowns = _tracker._cooldowns

#: 上游「该账号 × 该模型」频率窗口的冷却首枪时长。实测恢复中位数 25 分钟，
#: 但首枪给 60s 就够——真有持续限流会在下一次尝试时翻倍。
_MODEL_RATE_COOLDOWN_S = 60.0

#: 会话被判死后的冷却（没有密码可自动续期时）。
_RELOGIN_COOLDOWN_S = 3600.0


def _model_key(account_id: str, model: str) -> str:
    return f"{account_id}{_MODEL_KEY_SEP}{model}"


def is_model_rate_limited(code: int, error_code: str = "", message: str = "") -> bool:
    """是不是「账号 × 模型」的频率窗口（429 ``user_model_rate_limited``）。

    与账号级 429（积分耗尽）区分：前者只挡这一个模型，后者整号停摆。
    """
    if error_code == "user_model_rate_limited":
        return True
    text = (message or "").lower()
    if "user_model_rate_limited" in text or "rate_limited" in text:
        return True
    return code == 429 and "model" in text


def is_credit_exhausted(code: int, error_code: str = "", message: str = "") -> bool:
    """积分是否耗尽（整号停摆，不是单模型限流）。"""
    if code == 402:
        return True
    text = (message or "").lower()
    return (error_code in ("insufficient_credits", "credits_exhausted", "no_credit")
            or "积分不足" in text or "insufficient credit" in text)


def mark_cooldown(account_id: str, *, retry_after: str | None = None,
                  quota: bool = False, reason: str = "") -> float:
    """把账号冷却一段时间。``retry_after``（Retry-After 头）优先于默认时长。"""
    return _tracker.mark(account_id, retry_after=retry_after,
                         kind="quota" if quota else "account",
                         reason=reason, log=log)


def mark_model_cooldown(account_id: str, model: str, *,
                        retry_after: str | None = None, reason: str = "") -> float:
    """只冷却「账号 × 模型」这一个组合（429 频率窗口）。"""
    return _tracker.mark(_model_key(account_id, model),
                         retry_after=retry_after, kind="quota",
                         reason=reason, log=log)


def model_cooldown_left(account_id: str, model: str) -> tuple[float, str]:
    return _tracker.left(_model_key(account_id, model))


def mark_relogin_needed(account_id: str, reason: str = "") -> float:
    """会话被判死且无法自动续期：长冷却，等用户重新导入。"""
    return _tracker.mark(account_id, retry_after=str(int(_RELOGIN_COOLDOWN_S)),
                         kind="account",
                         reason=reason or "会话失效，需重新登录", log=log)


def clear_cooldown(account_id: str) -> None:
    """清掉该账号的冷却标记（删除账号后调用，防内存残留）。"""
    _tracker.clear(account_id)


def cooldown_left(account_id: str) -> tuple[float, str]:
    """剩余冷却秒数与类别（0, "" 表示没在冷却）。"""
    return _tracker.left(account_id)


def available_accounts(model: str = "") -> list[AccountRef]:
    """当前可用的账号（按 failover 顺位）：剔除冷却中的。

    ``model`` 给定时同时剔除「该账号 × 该模型」被限流的组合——这是关键：
    模型级 429 不该把整号摘掉，同账号其它模型仍要能用。
    """
    accts = list_accounts()
    out: list[AccountRef] = []
    for a in accts:
        if cooldown_left(a.id)[0] > 0:
            continue
        if model and model_cooldown_left(a.id, model)[0] > 0:
            continue
        out.append(a)
    diagnose_available('raccoon', _tracker, accts, out, log=log)
    return out


def cooldown_report() -> str:
    """全部账号冷却状态的一句话画像（通道耗尽报错用）。"""
    return _tracker.report([a.id for a in list_accounts()])


def model_cooldown_report(model: str) -> str:
    """该模型上被限流的账号画像（429 时补充说明用）。"""
    parts: list[str] = []
    for a in list_accounts():
        left, _kind = model_cooldown_left(a.id, model)
        if left > 0:
            parts.append(f"{a.id} 该模型限流剩 {left / 60:.0f}min")
    return "；".join(parts)


def accounts_status() -> dict[str, Any]:
    """UI 账号状态面板数据（纯本地，不触网、不含秘密）。"""
    accounts = list_accounts()
    items: list[dict[str, Any]] = []
    for i, a in enumerate(accounts):
        cred = load_account_cred(a.id) or {}
        exp = token_expiry_epoch(cred)
        hours_left = round((exp - time.time()) / 3600, 1) if exp > 0 else None
        left, kind = cooldown_left(a.id)
        items.append({
            "id": a.id,
            "name": a.alias or a.name or a.id,
            "alias": a.alias,
            "index": i + 1,
            "phone": str(cred.get("phone") or ""),
            "uid": str(cred.get("uid") or ""),
            "token": "ok" if cred.get("access_token") else "missing",
            # 有密码 = 能自动续期，面板据此显示「自动续期」而不是「需重新导入」
            "auto_renew": has_relogin_cred(cred),
            "hours_left": max(hours_left, 0.0) if hours_left is not None else None,
            "cooling": ([{"kind": kind, "minutes_left": round(left / 60, 1)}]
                        if left > 0 else []),
        })
    return {"enabled": bool(items), "accounts": items}


def _clear_all_cooldowns() -> None:
    """测试隔离用。"""
    _tracker.clear_all()
