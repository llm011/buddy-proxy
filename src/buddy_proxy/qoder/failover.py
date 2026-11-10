"""qoder 多账号 failover：稳定主备 + 内存冷却。

核心冷却状态机收敛到 :class:`buddy_proxy.core.account_failover.CooldownTracker`
（antigravity/kimi/trae 共用）。本模块保留 qoder 自己的
:func:`accounts_status`（字段口径）与 :func:`available_accounts` 的 **region
过滤**——qoder 的 CN / 全球版账号不通用（连错域 401），请求级 failover 只在
同区账号间轮转。

永远从优先级最高的可用账号开始（登录顺序即优先级），坏账号靠冷却被临时摘出
候选。冷却状态只放内存（60s / 5min 级，进程重启清零的代价只是每账号重探一次）；
全账号都冷却时 :func:`available_accounts` 为空，转发直接 429——通道级快速失败。

qoder 的上游错误分两类：401（token 失效）与 403/429（额度/权益门——2026-10-03
起三方模型被收回就是带内 code 112 的 403）。403 里既可能是账号级（额度尽、
权益收回）也可能是请求级（模型不存在），按 code 与文案粗分：带 112/pricing 的
按账号冷却；其余 403 与 4xx 原样透传。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..core.account_failover import CooldownTracker, diagnose_available
from .catalog import unsupported_models_for
from .credentials import AccountRef, list_accounts, load_account_cred, cred_to_credential

log = logging.getLogger(__name__)

#: 模块级冷却状态机（账号级，收敛自原 copy-paste 实现）。
_tracker = CooldownTracker()

#: 活引用 tracker 内部 dict（同 ``_tracker._cooldowns``）：测试直接
#: ``failover._cooldowns.clear()`` / 赋值造过期条目，薄壳化后保留入口。
_cooldowns = _tracker._cooldowns

#: 兼容导出（原模块常量，外部可能引用）。
_ACCOUNT_COOLDOWN_S = 60.0
_QUOTA_COOLDOWN_S = 300.0


def mark_cooldown(account_id: str, *, retry_after: str | None = None,
                  quota: bool = False, reason: str = "") -> float:
    """把账号冷却一段时间。``retry_after``（Retry-After 头）优先于默认时长。"""
    return _tracker.mark(account_id, retry_after=retry_after,
                         kind="quota" if quota else "account",
                         reason=reason, log=log)


def clear_cooldown(account_id: str) -> None:
    """清掉该账号的冷却标记（删除账号后调用，防内存残留）。"""
    _tracker.clear(account_id)


def cooldown_left(account_id: str) -> tuple[float, str]:
    """剩余冷却秒数与类别（0, "" 表示没在冷却）。"""
    return _tracker.left(account_id)


def available_accounts(region: str | None = None) -> list[AccountRef]:
    """当前可用的账号（按 failover 顺位）。

    ``region`` 给出时只回该区账号——qoder 的 CN / 全球版账号不通用（连错域
    401），混域 failover 每轮必败一次，白白多付一次往返。
    """
    accts = list_accounts()
    out = [a for a in accts if cooldown_left(a.id)[0] <= 0]
    if region:
        out = [a for a in out if a.region == region]
    # 有账号被跳过时留一行诊断（冷却 / region 不匹配），见 diagnose_available。
    diagnose_available('qoder', _tracker, accts, out, region=region, log=log)
    return out


def cooldown_report() -> str:
    """全部账号冷却状态的一句话画像（通道耗尽报错用）。"""
    return _tracker.report([a.id for a in list_accounts()])


def accounts_status() -> dict[str, Any]:
    """UI 账号状态面板数据（纯本地，不触网、不含秘密）。"""
    accounts = list_accounts()
    items: list[dict[str, Any]] = []
    for i, a in enumerate(accounts):
        cred = load_account_cred(a.id) or {}
        # expires_at_ms 是**毫秒**，先除 1000 转 epoch 秒再和 time.time() 比——
        # 直接相减会算出 4.97 亿小时（真机 2026-10-05 用户实报「token 剩
        # 497759890.1h」）。其余通道的 expires 字段本就是秒，无此问题。
        exp = cred_to_credential(cred).expires_at_ms / 1000
        hours_left = round((exp - time.time()) / 3600, 1) if exp > 0 else None
        left, kind = cooldown_left(a.id)
        items.append({
            "id": a.id,
            "email": a.email,
            "name": a.alias or a.name or a.email or a.id,
            "alias": a.alias,
            "region": a.region,
            "index": i + 1,
            "uid": str(cred.get("uid") or ""),
            "token": "ok" if cred.get("refresh_token") else "missing",
            "hours_left": max(hours_left, 0.0) if hours_left is not None else None,
            "cooling": ([{"kind": kind, "minutes_left": round(left / 60, 1)}] if left > 0 else []),
            # per-account 模型限制（覆盖表反查）：该账号调不了的模型名，空 = 全支持
            "models_limited": unsupported_models_for(a.id),
        })
    return {"enabled": bool(items), "accounts": items}
