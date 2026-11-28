"""raccoon 多账号 failover：稳定主备 + 内存冷却。

核心冷却状态机收敛到 :class:`buddy_proxy.core.account_failover.CooldownTracker`
（antigravity/qoder/trae/kimi 共用）。本模块只保留 raccoon 自己的失败语义：

- **账号级冷却**（60s）：401/403——会话失效或普通上游拒绝。
- **额度冷却**（300s）：402 / 积分耗尽。
- **风控冷却**（30min）：``200108``——上游明确判定整号异常调用，不能
  和普通账号冷却混为一谈；管理页要据此显示「风控冷却」与剩余时长。
- **模型级冷却**（``账号\\x00模型``）：``429 user_model_rate_limited``——这是
  **账号×模型**的频率窗口，恢复中位数 25 分钟，只挡这一个模型，不该把整号
  摘掉（同账号其它模型仍可用）。
- **需重新登录**（长冷却）：会话被上游判死（``needs_relogin``）。有手机号+
  密码时优先自动重登（见 ``login.relogin_account``），没有才冷却并提示用户。

永远从优先级最高的可用账号开始（导入顺序即优先级），坏账号靠冷却被临时
摘出候选。

**冷却落盘（本通道是唯一开启的）**：``core/account_failover`` 默认不落盘——
60s / 5min 的短冷却重启清零无害，重探一次的代价远小于带着不信任启动。但 raccoon
的**风控冷却**（200108，30 分钟）不成立：重探不是免费的，重探本身就是「异常
调用」。2026-11-26 01:50:41 实测：5 个号全在冷却中 → 重启 → 冷却随进程消失
→ 新进程 +3 秒内全部重打上游 → 同一秒重新判风控、重新冷却 30 分钟，该区间
39 次请求 0 次成功。重启等于主动延长封禁。所以只对超过阈值的冷却落盘
（见 ``_new_tracker``），短冷却仍是纯内存。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from ..core.account_failover import CooldownTracker, diagnose_available
from . import rate_limit
from .credentials import (
    AccountRef,
    has_relogin_cred,
    list_accounts,
    load_account_cred,
    raccoon_state_dir,
    token_expiry_epoch,
)

log = logging.getLogger(__name__)

#: 模型级冷却键的分隔符（账号 id 不含 \x00）。
_MODEL_KEY_SEP = "\x00"

#: 冷却快照文件名（落在 raccoon 自己的状态目录，与凭据同目录）。
_COOLDOWN_FILE = "cooldowns.json"

#: 只有冷却时长不低于它才落盘。取值覆盖 raccoon 全部的长冷却档位：风控 30min
#: （_RISK_CONTROL_COOLDOWN_S）、需重登 1h（_RELOGIN_COOLDOWN_S）；而 60s 的
#: 账号级 / 模型级短冷却在阈值之下，保持纯内存——重启重探它们是免费的。
_PERSIST_MIN_SECONDS = 300.0


def _cooldown_path() -> Path:
    return raccoon_state_dir() / _COOLDOWN_FILE


def _new_tracker() -> CooldownTracker:
    """构造带持久化的 tracker，并对账掉已删账号的键。

    **对账**（持久化后才需要）：旧进程的快照里可能有用户后来删掉的账号。进程
    重启本来会把它清掉，落盘后它会跨重启一直活着；而 ``clear(账号id)`` 清不掉
    模型级复合键（键是 ``账号\\x00模型`` 拼出来的，不等于账号 id）。这里按当前
    真实账号列表过一遍：不是任何现存账号的键、也不是任何现存账号的复合键，剔除
    并回写。账号列表读不出来（None/异常）时**不动**快照——宁可多留一条幽灵冷却
    （最多让一个不存在的账号「被跳过」，无害），也不能把活账号的冷却误删。
    """
    # 传**函数**而不是路径：本模块的 tracker 在 import 时构造，那时把路径定死
    # 的话，之后环境变量（RACCOON_STATE_DIR）再变它也不跟——测试夹具想隔离
    # 状态目录隔不掉，冷却会写进真实目录。
    tracker = CooldownTracker(
        persist_path=_cooldown_path,
        persist=True,
        persist_min_seconds=_PERSIST_MIN_SECONDS,
    )
    try:
        alive = {str(a.id) for a in (list_accounts() or ())}
    except Exception:  # noqa: BLE001 - 见 docstring：读不到账号列表就不对账
        return tracker
    if not alive:
        return tracker
    stale = [k for k, (left, _kind) in tracker.snapshot().items()
             if left > 0 and not _key_belongs(k, alive)]
    if stale:
        for key in stale:
            tracker.clear(key)
    return tracker


def _key_belongs(key: str, alive: set[str]) -> bool:
    """冷却键是否属于现存账号：账号 id 本身，或 ``账号id\\x00模型`` 复合键。"""
    if key in alive:
        return True
    acct, sep, _model = key.partition(_MODEL_KEY_SEP)
    return bool(sep) and acct in alive


#: 模块级冷却状态机（账号级，收敛自各通道的 copy-paste 实现）。
_tracker = _new_tracker()

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


#: 账号级风控（``200108``）的冷却时长。上游提示「稍后重试」，实测短时间高频
#: 调用会触发；给 30 分钟——3 小时太长（正常使用等不了），30 分钟足够让
#: 风控窗口过去，也不会因为马上重试而把账号彻底打废。
_RISK_CONTROL_COOLDOWN_S = 1800.0


def is_account_risk_control(code: int, error_code: str = "",
                            message: str = "") -> bool:
    """是否是**账号级风控**（``200108 系统检测到当前账号存在异常调用``）。

    与模型级限流（``user_model_rate_limited``）必须分开：

    * 模型级只挡一个模型，其它模型还能用；
    * 账号级是**整个账号**被限制，必须整号长冷却——否则每个模型都去撞一次，
      等于持续给上游发「异常调用」信号，把风控窗口越拉越长
      （2026-11-23 实测：账号已冷却时并发重试，同一秒打出 4 条 429）。
    """
    if str(error_code) == "200108":
        return True
    text = (message or "")
    return "200108" in text or "存在异常调用" in text


def is_credit_exhausted(code: int, error_code: str = "", message: str = "") -> bool:
    """积分是否耗尽（整号停摆，不是单模型限流）。"""
    if code == 402:
        return True
    text = (message or "").lower()
    return (error_code in ("insufficient_credits", "credits_exhausted", "no_credit")
            or "积分不足" in text or "insufficient credit" in text)


def mark_cooldown(account_id: str, *, retry_after: str | None = None,
                  quota: bool = False, risk_control: bool = False,
                  reason: str = "") -> float:
    """把账号冷却一段时间。``retry_after``（Retry-After 头）优先于默认时长。

    ``risk_control`` 单独留档，避免面板把 200108 风控和 60s 网络/会话抖动
    都显示成同一种「账号冷却」；这个类别也会随长冷却一起落盘。
    """
    kind = "risk_control" if risk_control else "quota" if quota else "account"
    return _tracker.mark(account_id, retry_after=retry_after,
                         kind=kind, reason=reason, log=log)


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
    """清掉该账号的冷却标记（删除账号后调用，防内存残留）。

    必须连「账号\\x00模型」复合键一起清：``mark_model_cooldown`` 的键是拼接出来
    的，不等于账号 id，只 ``clear(账号id)`` 清不掉它。持久化前漏掉无所谓（进程
    重启清空）；落盘后复合键会一直活着——账号删了、它的模型级冷却还在盘上，
    重启后被恢复成谁也对不上的幽灵条目。删除账号当下就得清干净，不能等重启时
    的对账（那只是兜底）。
    """
    _tracker.clear(account_id)
    prefix = f"{account_id}{_MODEL_KEY_SEP}"
    for key in [k for k, (left, _kind) in _tracker.snapshot().items()
                if left > 0 and k.startswith(prefix)]:
        _tracker.clear(key)


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
    return _tracker.report(
        [a.id for a in list_accounts()],
        labels={"risk_control": "风控冷却"},
    )


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
    rate_win = rate_limit.window()
    items: list[dict[str, Any]] = []
    for i, a in enumerate(accounts):
        cred = load_account_cred(a.id) or {}
        exp = token_expiry_epoch(cred)
        hours_left = round((exp - time.time()) / 3600, 1) if exp > 0 else None
        cooling: list[dict[str, Any]] = []
        left, kind = cooldown_left(a.id)
        if left > 0:
            cooling.append({
                "kind": kind,
                "seconds_left": round(left, 1),
                "minutes_left": round(left / 60, 1),
            })
        # 滑动窗口到顶时转发会直接跳过该账号，但它不会写进 CooldownTracker。
        # 面板若只看 cooling，会把这种「暂时不可用」误报成可用，所以一并透出。
        rate_left = rate_win.recovery_in(a.id)
        if rate_left > 0:
            cooling.append({
                "kind": "rate",
                "seconds_left": round(rate_left, 1),
                "minutes_left": round(rate_left / 60, 1),
            })
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
            "available": not cooling,
            "cooling": cooling,
        })
    return {"enabled": bool(items), "accounts": items}


def _clear_all_cooldowns() -> None:
    """测试隔离用。"""
    _tracker.clear_all()
