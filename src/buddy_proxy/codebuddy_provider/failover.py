"""codebuddy 多账号 failover：稳定主备 + 内存冷却。

核心冷却状态机收敛到 :class:`buddy_proxy.core.account_failover.CooldownTracker`
（antigravity/kimi/qoder/trae 共用）。本模块保留 codebuddy 自己的
:func:`accounts_status`（字段口径：uid/nickname，过期时间毫秒）并持有一个
模块级实例。

永远从优先级最高的可用账号开始（登录顺序即优先级），坏账号靠冷却被临时摘出
候选。冷却状态只放内存（60s / 5min 级，进程重启清零的代价只是每账号重探一次）；
全账号都冷却时 :func:`available_accounts` 为空，转发直接 429——通道级快速失败。

codebuddy 的上游错误：401（token 失效）与 429（单账号额度耗尽，如 code 14018）。

region 维度（2026-10 海外版上线）：CN（copilot.tencent.com）与海外版
（www.codebuddy.ai）账号同存一个 index.json，但**绝不互捞**——海外通道只轮转
region=global 的账号（协议同构、账号体系独立，拿 CN 账号打海外端点必 401）。
``available_accounts`` / ``display_index`` / ``accounts_status`` 都带 region
参数，缺省 None = 全量（历史调用方行为不变）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..core.account_failover import CooldownTracker, diagnose_available
from .credentials import AccountRef, list_accounts, load_account_cred

log = logging.getLogger(__name__)

#: 模块级冷却状态机。
_tracker = CooldownTracker()

#: 活引用 tracker 内部 dict（同 ``_tracker._cooldowns``）：测试直接
#: ``failover._cooldowns.clear()`` / 赋值造过期条目，保留入口。
_cooldowns = _tracker._cooldowns

#: 兼容导出（原模块常量，外部可能引用）。
_ACCOUNT_COOLDOWN_S = 60.0
_QUOTA_COOLDOWN_S = 300.0


def mark_cooldown(account_id: str, *, retry_after: str | None = None,
                  quota: bool = False, reason: str = "") -> float:
    """把**账号**冷却一段时间。``retry_after``（Retry-After 头）优先于默认时长。

    警告：这只该在「账号整体不可用」时调用（401 token 失效 / 14018 额度耗尽）。
    单模型限流（code 6004）请用 :func:`mark_model_cooldown`，否则会把一个模型的
    限制放大成整个账号不可用——实测 6 个账号只因 kimi 限流被全部冷却 5 分钟，
    期间连 deepseek/glm 都调不了。
    """
    return _tracker.mark(account_id, retry_after=retry_after,
                         kind="quota" if quota else "account",
                         reason=reason, log=log)


#: 「账号 + 模型」复合冷却键的分隔符。账号 id 是 UUID/短 hex，不含 ``\x00``。
_MODEL_KEY_SEP = "\x00"


def _model_key(account_id: str, model: str) -> str:
    return f"{account_id}{_MODEL_KEY_SEP}{model}"


def mark_model_cooldown(account_id: str, model: str, *,
                        retry_after: str | None = None,
                        reason: str = "") -> float:
    """只冷却「该账号的这个模型」，账号本身仍然可用。

    用于上游 code 6004：``您的使用量已超出频率限制 … 您也可以切换其他模型继续
    使用``——这是**单模型**限流，不是账号坏了。同账号的其它模型实测照常 200。

    实现复用 :class:`CooldownTracker`，只是键换成 ``account\\x00model`` 复合键；
    账号级冷却仍用纯 id。两套键互不干扰，也不影响 ``accounts_status`` 等按纯 id
    查询的既有视图（它们查的是纯 id，不会命中复合键）。
    """
    return _tracker.mark(_model_key(account_id, model), retry_after=retry_after,
                         kind="quota", reason=reason, log=log)


def model_cooldown_left(account_id: str, model: str) -> tuple[float, str]:
    """该账号的这个模型还剩多少冷却秒数（0, "" 表示没受限）。"""
    return _tracker.left(_model_key(account_id, model))


def clear_cooldown(account_id: str) -> None:
    """清掉该账号的冷却标记（删除账号后调用，防内存残留）。

    同时清掉以它开头的模型级键，否则删号后复合键会一直挂在内存里。
    """
    _tracker.clear(account_id)
    prefix = f"{account_id}{_MODEL_KEY_SEP}"
    with _tracker._lock:  # noqa: SLF001 - 同模块内的状态清理
        for key in [k for k in _tracker._cooldowns if k.startswith(prefix)]:
            _tracker._cooldowns.pop(key, None)


def cooldown_left(account_id: str) -> tuple[float, str]:
    """剩余冷却秒数与类别（0, "" 表示没在冷却）。"""
    return _tracker.left(account_id)


def available_accounts(region: str | None = None,
                       model: str | None = None) -> list[AccountRef]:
    """当前可用的账号（按 failover 顺位）：剔除冷却中的。

    ``region`` 给定时只返回该区账号（cn/global，海外版通道与 CN 互不混用）；
    ``None`` = 全量（历史行为）。

    ``model`` 给定时额外剔除「该模型被限流」的账号（code 6004 单模型限制）。
    这是**按模型**跳过，不影响账号本身：同账号换个模型照常可用，也不会因此
    把账号标记成整体冷却。不传 ``model`` 时行为与以前完全一致（签到/额度等
    按账号维度的调用方不受影响）。
    """
    out = []
    all_ids: list[str] = []
    for acct in list_accounts():
        all_ids.append(acct.id)
        if cooldown_left(acct.id)[0] > 0:
            continue
        if region is not None and acct.region != region:
            continue
        if model is not None and model_cooldown_left(acct.id, model)[0] > 0:
            continue
        out.append(acct)
    # 有账号被跳过时留一行诊断（见 core.account_failover.diagnose_available）。
    # 本通道额外可能因「单模型限流」跳过，所以 reason_for 要覆盖那一层。
    diagnose_available("codebuddy", _tracker, list_accounts(), out,
                       region=region, log=log)
    return out



def cooldown_report() -> str:
    """全部账号冷却状态的一句话画像（通道耗尽报错用）。"""
    return _tracker.report([a.id for a in list_accounts()])


def display_index(region: str | None = None) -> dict[str, int]:
    """账号 id → 管理页展示序号（1-based）。

    ``region=None`` 是全量列表位次（历史行为）；``region`` 给定时是该区**区内**
    位次——CN 与海外版的额度标签（``CodeBuddy #N``）各自编号，与各自面板的
    账号快照（也按区过滤）对得上。

    这是「序号」的唯一权威：``accounts_status()`` 的 ``index`` 与 provider 的
    额度/签到标签（``CodeBuddy #N · ``）都必须由它产生。前端按序号把额度块对上
    账号快照（账号名、▲▼ 顺位、✕ 删除都拿它定位），两边各数各的就会错位——
    额度侧遍历的是「未冷却」子集，快照侧是全量列表，于是 ``#1`` 可能指向另一个
    账号，✕ 就把**别人的**凭据文件删了（不可逆）。

    用位次而不用 ``priority``：``delete_account`` 不重排 priority，删号后会留下
    空洞（0,2,3），``priority+1`` 与快照位次对不上；位次是排序后的实际位置，
    与 ``accounts_status`` 的 ``enumerate`` 天然同源。
    """
    accts = list_accounts()
    if region is not None:
        accts = [a for a in accts if a.region == region]
    return {a.id: i + 1 for i, a in enumerate(accts)}


def accounts_status(region: str | None = None) -> dict[str, Any]:
    """UI 账号状态面板数据（纯本地，不触网、不含秘密）。

    ``region`` 给定时只下发该区账号，``index`` 也按区内位次计（与
    ``display_index(region)`` 同源）——CN 面板与海外版面板各看各的。
    """
    accounts = list_accounts()
    if region is not None:
        accounts = [a for a in accounts if a.region == region]
    items: list[dict[str, Any]] = []
    for i, a in enumerate(accounts):
        cred = load_account_cred(a.id) or {}
        # expires_at_ms 是毫秒（上游原样）；trae 那边是秒，别抄错单位
        exp = int(cred.get("expires_at_ms") or 0)
        hours_left = round((exp - time.time() * 1000) / 3_600_000, 1) if exp > 0 else None
        left, kind = cooldown_left(a.id)
        items.append({
            "id": a.id,
            "uid": a.uid,
            "nickname": a.alias or a.nickname or a.uid or a.id,
            "alias": a.alias,
            "region": a.region,
            # 序号口径见 display_index()：额度标签必须用同一套，否则面板会把
            # 额度块对到别的账号上（✕ 删错人）。
            "index": i + 1,
            "token": "ok" if cred.get("refresh_token") else "missing",
            "hours_left": max(hours_left, 0.0) if hours_left is not None else None,
            "cooling": ([{"kind": kind, "minutes_left": round(left / 60, 1)}] if left > 0 else []),
        })
    return {"enabled": bool(items), "accounts": items}
