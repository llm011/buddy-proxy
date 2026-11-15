"""copilot 多账号 failover：稳定主备 + 账号级/模型级双档冷却。

冷却状态机收敛在 :class:`buddy_proxy.core.account_failover.CooldownTracker`；
本模块只放 copilot 自己的失败语义：

- **账号级**冷却（``account`` 档，60s）：401/403——token 被撤销、订阅无权限。
  401 的账号**不自动摘除**：token 可能只是被用户临时换掉，重启或重新登录
  即恢复，自动删号会让用户下次登录时莫名多一个「新」顺位。
- **额度**冷却（``quota`` 档，300s）：429。只记账号级——Copilot 的 429 是
  账号维度的配额/频率限制，本账号所有模型一起停，换模型救不了。
- **模型级**冷却（复合键 ``账号\\x00模型``，``quota`` 档）：400
  ``model_not_supported`` / 400 ``no_available_model_endpoints``——这是
  **SKU 不含该模型**的确定性拒绝（不是请求写错），账号本身完全健康。只冷却
  这一个模型，否则一个不在套餐里的模型会把整个账号从所有模型上摘掉。

选号策略与全仓一致：永远从优先级最高的可用账号开始，坏账号靠冷却临时摘出
候选。冷却只放内存，进程重启清零。
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.account_failover import CooldownTracker, diagnose_available
from .credentials import AccountRef, list_accounts, load_account_cred

log = logging.getLogger(__name__)

_tracker = CooldownTracker()

#: 活引用 tracker 内部 dict（同 ``_tracker._cooldowns``）：测试直接清空用。
_cooldowns = _tracker._cooldowns

#: 「账号 + 模型」复合冷却键的分隔符（账号 id 不含 \x00）。
_MODEL_KEY_SEP = "\x00"

#: 上游错误里表示「该 SKU 不含这个模型」的确定性拒绝码。
_MODEL_REJECT_CODES = frozenset({"model_not_supported", "no_available_model_endpoints"})

#: 上游错误里表示「额度/频率」的码（429 之外还有带内的）。
_QUOTA_CODES = frozenset({
    "rate_limit_exceeded", "quota_exceeded", "usage_limit_exceeded",
})


def _model_key(account_id: str, model: str) -> str:
    return f"{account_id}{_MODEL_KEY_SEP}{model}"


def is_model_reject(code: int, error_code: str, message: str = "") -> bool:
    """这是不是「该模型对这个账号不可用」的确定性拒绝。

    Copilot 对 SKU 不含的模型回 **400** 而不是 403/429（实测：
    ``{"error":{"message":"model not supported ...","code":"model_not_supported"}}``），
    ``/v1/messages`` 不支持某模型时回 400，码是
    ``no_available_model_endpoints``、文案是 ``no model endpoints available``
    （码和文案都对不上，两条都要认）。两者都属于「换账号/换时间都不会好，
    但同账号其它模型没事」——只冷却模型，不冷却账号。

    403 也认：个人号的订阅不含某模型时常回 403 而非 400。
    """
    if code not in (400, 403):
        return False
    if error_code in _MODEL_REJECT_CODES:
        return True
    text = (message or "").lower()
    return ("model not supported" in text
            or "no model endpoints available" in text
            or "no_available_model_endpoints" in text)


def is_quota_signal(code: int, error_code: str = "") -> bool:
    return code == 429 or error_code in _QUOTA_CODES


def mark_cooldown(account_id: str, *, retry_after: str | None = None,
                  quota: bool = False, reason: str = "") -> float:
    """把账号冷却一段时间（``retry_after`` 优先），返回实际冷却秒数。"""
    return _tracker.mark(account_id, retry_after=retry_after,
                         kind="quota" if quota else "account",
                         reason=reason, log=log)


def mark_model_cooldown(account_id: str, model: str, *,
                        retry_after: str | None = None,
                        reason: str = "") -> float:
    """只冷却「该账号的这个模型」（账号本身仍可用于其它模型）。"""
    return _tracker.mark(_model_key(account_id, model), retry_after=retry_after,
                         kind="quota", reason=reason, log=log)


def model_cooldown_left(account_id: str, model: str) -> tuple[float, str]:
    return _tracker.left(_model_key(account_id, model))


def clear_cooldown(account_id: str) -> None:
    """清掉该账号的冷却标记（删除账号后调用），连带清模型级复合键。"""
    _tracker.clear(account_id)
    prefix = f"{account_id}{_MODEL_KEY_SEP}"
    with _tracker._lock:  # noqa: SLF001 - 同模块内的状态清理
        for key in [k for k in _tracker._cooldowns if k.startswith(prefix)]:
            _tracker._cooldowns.pop(key, None)


def clear_all() -> None:
    _tracker.clear_all()


def cooldown_left(account_id: str) -> tuple[float, str]:
    return _tracker.left(account_id)


def available_accounts(model: str | None = None) -> list[AccountRef]:
    """当前可用账号（按 failover 顺位），剔除账号级与该模型级的冷却。"""
    accts = list_accounts()
    out = [a for a in accts
           if cooldown_left(a.id)[0] <= 0
           and (model is None or model_cooldown_left(a.id, model)[0] <= 0)]
    diagnose_available("copilot", _tracker, accts, out, log=log)
    return out


def display_index() -> dict[str, int]:
    """账号 id → 展示序号（全量位次，从 1 开始）。

    ``quota()`` 的 ``Copilot #N`` 标签与 UI 账号快照共用这一份编号：两处各自
    ``enumerate`` 一次也能对上，但删号/重排后一旦有一处用了别的过滤（比如
    只取可用账号），✕ 删除就会落到别人的 id 上——那一步不可逆。
    """
    return {a.id: i for i, a in enumerate(list_accounts(), start=1)}


def cooldown_report() -> str:
    return _tracker.report([a.id for a in list_accounts()])


def model_cooldown_report(model: str) -> str:
    """某个模型在哪些账号上被冷却（模型级复合键不进 ``cooldown_report``）。"""
    parts = []
    for acct in list_accounts():
        left, _kind = model_cooldown_left(acct.id, model)
        if left > 0:
            parts.append(f"{acct.id} 的 {model} 冷却剩 {_tracker.fmt_left(left)}")
    return "、".join(parts)


def accounts_status() -> dict[str, Any]:
    """UI 账号面板数据（纯本地，不触网、不含秘密）。"""
    accounts = list_accounts()
    items: list[dict[str, Any]] = []
    for i, a in enumerate(accounts):
        cred = load_account_cred(a.id) or {}
        left, kind = cooldown_left(a.id)
        items.append({
            "id": a.id,
            "login": a.name,
            "name": a.alias or a.name or a.id,
            "alias": a.alias,
            "index": i + 1,
            "source": str(cred.get("source") or ""),
            "token": "ok" if cred.get("access_token") else "missing",
            "cooling": ([{"kind": kind, "minutes_left": round(left / 60, 1)}] if left > 0 else []),
        })
    return {"enabled": bool(items), "accounts": items}
