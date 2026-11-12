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

#: 轮询索引（每个 region 一个）：按登录顺序循环选择账号，避免单账号过度使用。
#: key = region or "global"
_rotation_index: dict[str, int] = {}

#: 账号权重（每个 region 一个）：成功 +1、失败 -1，用于智能轮询。
#: key = (region, account_id)
_weights: dict[tuple[str, str], float] = {}

#: 权重衰减率（每次请求后衰减 10%），避免单次失败导致长期低权重。
_WEIGHT_DECAY_RATE = 0.9

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
    """把账号冷却一段时间。``retry_after``（Retry-After 头）优先于默认时长。
    
    同时降低该账号权重（失败惩罚），让轮询更倾向其他账号。
    """
    # 降低权重（失败惩罚）
    for region in ["global", "cn", "global"]:  # 所有 region
        key = (region, account_id)
        _weights[key] = max(0.0, _weights.get(key, 0.0) - 1.0)
    
    return _tracker.mark(account_id, retry_after=retry_after,
                         kind="quota" if quota else "account",
                         reason=reason, log=log)


def mark_success(account_id: str) -> None:
    """账号请求成功，提升权重（成功奖励）。"""
    for region in ["global", "cn", "global"]:
        key = (region, account_id)
        _weights[key] = _weights.get(key, 0.0) + 1.0


def clear_cooldown(account_id: str) -> None:
    """清掉该账号的冷却标记（删除账号后调用，防内存残留）。
    
    同时恢复权重到初始值（0）。
    """
    for region in ["global", "cn", "global"]:
        key = (region, account_id)
        _weights[key] = 0.0
    _tracker.clear(account_id)


def cooldown_left(account_id: str) -> tuple[float, str]:
    """剩余冷却秒数与类别（0, "" 表示没在冷却）。"""
    return _tracker.left(account_id)


def available_accounts(region: str | None = None) -> list[AccountRef]:
    """当前可用的账号（按轮询 + 权重排序）。

    策略：
    1. 先过滤掉冷却中的账号与 region 不匹配的
    2. 按权重降序排列（成功的账号优先）
    3. 同权重的按登录顺序轮询（避免单账号过度使用）
    
    这样既避免单账号限流，又优先用成功率高的账号。
    """
    accts = list_accounts()
    out = [a for a in accts if cooldown_left(a.id)[0] <= 0]
    if region:
        out = [a for a in out if a.region == region]
    
    if not out:
        diagnose_available('qoder', _tracker, accts, [], region=region, log=log)
        return []
    
    # 按权重降序 + 轮询索引排序
    def sort_key(a):
        w = _weights.get((region or "global", a.id), 0.0)
        # 权重降序，同权重的按账号 ID 排序（确保稳定性）
        return (-w, a.id)
    
    out.sort(key=sort_key)
    
    # 轮询：**把排序后的列表按轮转起点旋转**，而不是只摘一个。
    #
    # 为什么要返回完整列表（不只是首选那个）：``forward`` 的 failover 循环靠
    # 遍历这个列表来「A 失败换 B」。只回一个的话，首选账号一失败就直接报
    # 「所有账号均不可用」——比原来的主备降级还脆弱（踩过：8 个 failover
    # 测试全红，生产上单个账号抖一下就整轮挂）。
    #
    # 旋转而不是重排：保持权重与相对次序不变，只换起点。这样连续请求会从
    # 不同账号开始（分摊负载），而每个请求内部仍能按权重顺序依次兜底。
    #
    # ⚠ ``rot_idx`` 必须对**当前**列表长度取模：索引按 region 累积，而账号会
    # 被冷却过滤掉、列表随时变短。直接用会 ``list index out of range``
    # （踩过：6 条连续 500、耗时 0ms，根本没发请求就崩了）。
    if out:
        rot_idx = _rotation_index.get(region or "global", 0) % len(out)
        out = out[rot_idx:] + out[:rot_idx]
        _rotation_index[region or "global"] = (rot_idx + 1) % len(out)
    
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
