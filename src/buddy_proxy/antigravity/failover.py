"""antigravity 多账号 failover：稳定主备 + 内存冷却。

核心冷却状态机收敛到 :class:`buddy_proxy.core.account_failover.CooldownTracker`
（kimi/qoder/trae 共用）。antigravity 特有 ``blacklist`` 档（Google 风控拉黑
403「Verify your account」，6h）通过 ``extra_kinds`` 注入；本模块保留自己的
:func:`accounts_status`（字段口径）与拉黑指纹 :func:`is_blacklist_signal`。

选号策略与 trae PAT 同思路：永远从优先级最高的可用账号开始（登录顺序即
优先级），坏账号靠冷却被临时摘出候选——看起来像轮询，实质是主备降级。

冷却状态**只放内存**：只有 60s / 5min / 6h 级，进程重启清零的代价只是每账号
重探一次（本来也要探），换来零状态文件管理。全账号都冷却时
:func:`available_accounts` 为空，转发直接 429——通道级快速失败。

拉黑识别（2026-10-03）：403 文案「Verify your account to continue.」是
Google 风控把账号挡在 antigravity 外（能登录、凭据有效，但上游一律拒）。
这不上额度冷却的 60s 档——那只会每分钟白打一次上游；直接 6h 冷却 +
``blacklist`` 类别，面板副标题标注「疑似拉黑」引导删除（管理页可删账号）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..core.account_failover import CooldownTracker, diagnose_available
from .credentials import (
    AccountRef,
    _expiry_dt,
    list_accounts,
    load_account_cred,
)

log = logging.getLogger(__name__)

#: 模块级冷却状态机（含 antigravity 特有的 blacklist 档）。
_tracker = CooldownTracker(extra_kinds={"blacklist": 6 * 3600.0})

#: 活引用 tracker 内部 dict（同 ``_tracker._cooldowns``）：测试直接
#: ``failover._cooldowns.clear()`` / 赋值造过期条目，薄壳化后保留入口。
_cooldowns = _tracker._cooldowns

#: 兼容导出（原模块常量，外部可能引用）。
_ACCOUNT_COOLDOWN_S = 60.0
_QUOTA_COOLDOWN_S = 300.0
_BLACKLIST_COOLDOWN_S = 6 * 3600.0
_RETRY_AFTER_MIN_S = 1.0
_RETRY_AFTER_MAX_S = 7 * 86400.0

#: 上游 403 文案的拉黑指纹（不区分大小写匹配）。
_BLACKLIST_MSG = "verify your account"

#: 「账号 + 模型」复合冷却键的分隔符（同 codebuddy 的先例；账号 id 不含 \x00）。
_MODEL_KEY_SEP = "\x00"


def _model_key(account_id: str, model: str) -> str:
    return f"{account_id}{_MODEL_KEY_SEP}{model}"


def is_blacklist_signal(code: int, message: str) -> bool:
    """403 +「Verify your account to continue.」= Google 风控拉黑该账号。

    与普通 403（PERMISSION_DENIED 等）的区别：账号本身能登录、凭据有效，
    但 antigravity 上游一律拒——重试无意义，冷却按拉黑档走。
    """
    return code == 403 and _BLACKLIST_MSG in (message or "").lower()


def mark_cooldown(account_id: str, *, retry_after: str | None = None,
                  quota: bool = False, blacklist: bool = False,
                  reason: str = "") -> float:
    """把账号冷却一段时间。``retry_after``（Retry-After 头）优先于默认时长。

    返回实际冷却秒数。三档：``blacklist``（Google 风控拉黑，6h）>
    ``quota``（429 额度，默认 5min）> 其余账号级问题（403/401，60s）。
    Retry-After 只覆盖后两档——拉黑是风控决定，头部给不出恢复时间。
    """
    kind = "blacklist" if blacklist else ("quota" if quota else "account")
    # Retry-After 只覆盖非拉黑档（拉黑是风控决定，头部给不出恢复时间）。
    return _tracker.mark(account_id, retry_after=None if blacklist else retry_after,
                         kind=kind, reason=reason, log=log)


def mark_model_cooldown(account_id: str, model: str, *,
                        retry_after: str | None = None,
                        reason: str = "") -> float:
    """只冷却「该账号的这个模型」，账号本身仍可用于其它模型。

    2026-11-14 实测：``gemini-3.6-flash`` 已不在上游目录，请求会收到伪装的
    ``429 RESOURCE_EXHAUSTED``（quota 读数其实仍满额）。旧逻辑按账号级冷却
    5min，两个账号依次被标记后 ``available_accounts()`` 变空，连正常的
    ``gemini-3.8-flash`` 也 429——一个坏模型毒死整个通道。429 因此按
    (账号, 模型) 复合键冷却；403/拉黑/凭据问题仍是账号级（见 mark_cooldown）。
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


def available_accounts(model: str | None = None) -> list[AccountRef]:
    """当前可用的账号（按 failover 顺位）：剔除账号级冷却中的。

    ``model`` 给定时额外剔除「该模型正被 429 冷却」的账号。账号级冷却与
    模型级冷却是两套键，互不干扰：额度真耗尽的账号仍整体下线；模型名失效
    只影响这一个模型，其它模型照常可用。
    """
    accts = list_accounts()
    out = [a for a in accts
           if cooldown_left(a.id)[0] <= 0
           and (model is None or model_cooldown_left(a.id, model)[0] <= 0)]
    # 有账号被跳过时留一行诊断（见 core.account_failover.diagnose_available）：
    # 「本该用顺位第一、实际跳到了第三个号」这类现象，过去在日志里完全不可见。
    diagnose_available('antigravity', _tracker, accts, out, log=log)
    return out


def cooldown_report() -> str:
    """全部账号冷却状态的一句话画像（通道耗尽报错用），如：

    「u@x.com 额度冷却剩 2.1min、v@y.com 疑似拉黑剩 6.0h」

    只列冷却中的账号——报「全部不可用」时不在冷却的账号要么成功了（走不到
    报错）要么是无冷却失败（EOF 之类，由报错里的「最后错误」兜底说明）。
    """
    return _tracker.report([a.id for a in list_accounts()],
                           labels={"blacklist": "疑似拉黑"})


def model_cooldown_report(model: str) -> str:
    """某个模型在哪些账号上被 429 冷却（模型级键不进 cooldown_report）。

    ``CooldownTracker.report`` 只按纯账号 id 查，模型级复合键永远查不到——
    若所有账号都因同一个模型冷却，旧报错会写成「所有账号均在冷却中：」后面
    一片空白。这里补出「谁、因为哪个模型、还剩多久」。
    """
    parts = []
    for acct in list_accounts():
        left, _kind = model_cooldown_left(acct.id, model)
        if left > 0:
            parts.append(f"{acct.id} 的 {model} 额度冷却剩 {_tracker.fmt_left(left)}")
    return "、".join(parts)


def accounts_status() -> dict[str, Any]:
    """UI 账号状态面板数据（纯本地，不触网、不含秘密）。"""
    accounts = list_accounts()
    items: list[dict[str, Any]] = []
    for i, a in enumerate(accounts):
        cred = load_account_cred(a.id) or {}
        exp = _expiry_dt(cred)
        hours_left = round((exp.timestamp() - time.time()) / 3600, 1) if exp else None
        left, kind = cooldown_left(a.id)
        items.append({
            "id": a.id,
            "email": a.alias or a.email or a.id,
            "alias": a.alias,
            "index": i + 1,
            "project_id": cred.get("project_id") or "",
            "token": "ok" if cred.get("refresh_token") else "missing",
            "hours_left": max(hours_left, 0.0) if hours_left is not None else None,
            "cooling": ([{"kind": kind, "minutes_left": round(left / 60, 1)}] if left > 0 else []),
        })
    return {"enabled": bool(items), "accounts": items}
