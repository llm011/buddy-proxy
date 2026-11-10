"""账号级 failover 冷却状态机：稳定主备 + 内存冷却（多通道共用）。

antigravity / kimi / qoder / trae 的「坏账号冷却、按顺位主备降级」是同一套逻辑：
内存 TTL 字典 + ``threading.Lock``，到期惰性恢复。本模块把这套状态机参数化成
:class:`CooldownTracker`，各通道的 ``failover.py`` 持有一个实例并保留自己的
``accounts_status``（字段不同）与 ``available_accounts``（qoder 带 region）。

**为什么收拢**：此前 antigravity/kimi/qoder 各 copy-paste 一份（~400 行），核心
``mark_cooldown / clear_cooldown / cooldown_left / cooldown_report`` 逐字一致，
只有冷却档位（blacklist 仅 antigravity）、Retry-After 区间等常量差异。收敛后修
一处（如 Retry-After 钳制区间）不必三个通道同步改。

与 ``core/cooldown.py``（**模型级**换档冷却）区分：本模块管**账号**健康状态，
那个管 **provider/model 目标**的换档失败。

刻意**不落盘**：同 ``core/cooldown.py`` 的理由——冷却只是「此刻的健康状态」，
重启后重探一次的代价远小于带着不信任启动。

本模块是**叶子**：只 import 标准库，任何地方 import 都不会循环导入。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Sequence

#: 403 / 401 强刷后仍被拒：账号级问题，短冷却快速重探。
DEFAULT_ACCOUNT_COOLDOWN_S = 60.0
#: 429（额度耗尽）默认冷却：对齐各通道首档 5min；Retry-After 可覆盖。
DEFAULT_QUOTA_COOLDOWN_S = 300.0
#: Retry-After 的合理区间（钳到 1s~7d 防御离谱值）。
RETRY_AFTER_MIN_S = 1.0
RETRY_AFTER_MAX_S = 7 * 86400.0


class CooldownTracker:
    """一组账号的冷却状态。

    每个多账号通道持有一个实例（``failover.py`` 模块级单例）。冷却键是
    account_id（不耦合具体通道的 AccountRef 类型——各通道字段不同）。
    """

    def __init__(
        self,
        *,
        account_cooldown_s: float = DEFAULT_ACCOUNT_COOLDOWN_S,
        quota_cooldown_s: float = DEFAULT_QUOTA_COOLDOWN_S,
        extra_kinds: dict[str, float] | None = None,
    ) -> None:
        """``extra_kinds``：通道特有档位（如 antigravity 的 blacklist→6h），
        键是类别名，值是默认时长（秒）。``mark_cooldown`` 的 ``kind=`` 可用。"""
        self._durations: dict[str, float] = {
            "account": account_cooldown_s,
            "quota": quota_cooldown_s,
        }
        if extra_kinds:
            self._durations.update(extra_kinds)
        self._cooldowns: dict[str, tuple[float, str]] = {}  # id -> (until, kind)
        self._lock = threading.Lock()

    # -- 基本操作 -----------------------------------------------------------

    def mark(
        self,
        account_id: str,
        *,
        retry_after: str | None = None,
        kind: str = "account",
        reason: str = "",
        log: object = None,
    ) -> float:
        """把账号冷却一段时间，返回实际冷却秒数。

        ``retry_after``（Retry-After 头）优先于该类别默认时长，钳到
        ``[RETRY_AFTER_MIN_S, RETRY_AFTER_MAX_S]`` 防御离谱值。
        """
        default = self._durations.get(kind, self._durations["account"])
        seconds = default
        if retry_after:
            try:
                seconds = max(RETRY_AFTER_MIN_S,
                              min(float(retry_after), RETRY_AFTER_MAX_S))
            except (TypeError, ValueError):
                pass
        with self._lock:
            self._cooldowns[account_id] = (time.time() + seconds, kind)
        if reason and log is not None:
            log.warning("账号 %s 冷却 %s（%s）", account_id, self.fmt_left(seconds), reason)
        return seconds

    def clear(self, account_id: str) -> None:
        """清掉该账号的冷却标记（删除账号后调用，防内存残留）。"""
        with self._lock:
            self._cooldowns.pop(account_id, None)

    def clear_all(self) -> None:
        """清掉全部冷却标记（测试隔离用）。"""
        with self._lock:
            self._cooldowns.clear()

    def left(self, account_id: str) -> tuple[float, str]:
        """剩余冷却秒数与类别（0, "" 表示没在冷却）。"""
        with self._lock:
            entry = self._cooldowns.get(account_id)
        if not entry:
            return 0.0, ""
        until, kind = entry
        left = until - time.time()
        if left <= 0:
            return 0.0, ""
        return left, kind

    def is_cooling(self, account_id: str) -> bool:
        return self.left(account_id)[0] > 0

    # -- 聚合视图 -----------------------------------------------------------

    def report(self, ids: list[str], labels: dict[str, str] | None = None) -> str:
        """给定账号 id 列表的冷却状态一句话画像（通道耗尽报错用）。

        ``labels``：类别 → 展示名（如 antigravity 的 ``blacklist→疑似拉黑``）；
        缺省 ``quota→额度冷却``、其余 ``账号冷却``。
        """
        default_labels = {"quota": "额度冷却"}
        if labels:
            default_labels.update(labels)
        parts = []
        for aid in ids:
            left, kind = self.left(aid)
            if left <= 0:
                continue
            label = default_labels.get(kind, "账号冷却")
            parts.append(f"{aid} {label}剩 {self.fmt_left(left)}")
        return "、".join(parts)

    def snapshot(self) -> dict[str, tuple[float, str]]:
        """当前生效中的冷却 ``{id: (剩余秒, 类别)}``（未过期的）。"""
        now = time.time()
        out: dict[str, tuple[float, str]] = {}
        with self._lock:
            items = list(self._cooldowns.items())
        for aid, (until, kind) in items:
            left = until - now
            if left > 0:
                out[aid] = (left, kind)
        return out

    @staticmethod
    def fmt_left(seconds: float) -> str:
        if seconds >= 90 * 60:
            return f"{seconds / 3600:.1f}h"
        return f"{seconds / 60:.1f}min"


# ---------------------------------------------------------------------------
# 选号诊断：为什么某个账号这次没被用上
# ---------------------------------------------------------------------------
#
# 动机（2026-11-11 实报）：用户看到「顺位第一的账号本该被用，却跳到了 #3」，
# 而日志里**完全看不到原因**。那次排查只能靠反推时间线（对比前后 prompt 大小、
# 数账号出现次数）才断定是冷却，而不是负载均衡或代码 bug。
#
# 这类事件的特征是「静默跳过」：过滤发生在 available_accounts 里，`continue`
# 之后什么也不留。加日志的价值不在于事后能查，而在于**当时就能看出是哪一环**：
# 冷却（还要能看出剩多久、哪一类）/ 被停用 / region 不匹配 / 单模型限流。


def explain_skip(
    provider_id: str,
    total_accounts: Sequence[str],
    available: Sequence[str],
    *,
    reason_for: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """对比「全部账号」与「本次可用的账号」，给出被跳过者及原因。

    参数都是纯数据（id 列表），不碰任何通道的 AccountRef 类型——各通道字段
    不同，传对象会把这层耦合进通道。

    ``reason_for`` 由调用方提供（它才知道自己有哪些过滤条件：region、
    单模型限流、额度耗尽……）。缺省只区分「在冷却」与「其它」。

    返回 ``{"skipped": [{id, reason}], "available": n, "total": n}``。
    **没有跳过时返回空 skipped**，调用方据此决定要不要打日志——正常路径不该
    产生噪声，否则日志里每请求一行，真正要看的那条就埋了。
    """
    avail = {str(a) for a in available or ()}
    skipped: list[dict[str, str]] = []
    for aid in total_accounts or ():
        key = str(aid)
        if key in avail:
            continue
        reason = ""
        if reason_for is not None:
            try:
                reason = str(reason_for(key) or "")
            except Exception:  # noqa: BLE001 - 诊断失败不能影响转发
                reason = ""
        skipped.append({"id": key, "reason": reason or "unknown"})
    return {
        "skipped": skipped,
        "available": len(avail),
        "total": len(list(total_accounts or ())),
    }


def cooldown_reason(tracker: Any, account_id: str, labels: dict[str, str] | None = None
                    ) -> str:
    """某账号当前的冷却画像（供 ``explain_skip`` 的 ``reason_for`` 用）。

    形如 ``额度冷却剩 4.2min``；没在冷却返回空串（让调用方决定要不要换别的
    说法，比如「region 不匹配」）。
    """
    try:
        left, kind = tracker.left(account_id)
    except Exception:  # noqa: BLE001
        return ""
    if left <= 0:
        return ""
    names = {"quota": "额度冷却", "account": "账号冷却"}
    if labels:
        names.update(labels)
    human = f"{left / 60:.1f}min" if left >= 60 else f"{left:.0f}s"
    return f"{names.get(kind, kind or '冷却')}剩 {human}"


def diagnose_available(
    provider_id: str,
    tracker: "CooldownTracker",
    all_accounts: Sequence[Any],
    available: Sequence[Any],
    *,
    region: str | None = None,
    labels: dict[str, str] | None = None,
    log: Any = None,
) -> None:
    """各通道 ``available_accounts`` 共用的收尾：有跳过就打一行诊断。

    抽成公共函数而不是每个通道抄一份：漏抄一个通道，那个通道就继续静默跳过
    （而「静默跳过」正是这次要消灭的东西）。五个通道的过滤条件只差 region，
    在这里用参数表达即可。

    **只在有跳过时输出**：正常路径（全部可用）打日志会让每请求一行，
    真正要看的那条就被埋了。

    异常全吞：诊断是旁路，绝不能因为取不到冷却状态把转发搞挂。
    """
    try:
        avail_ids = {str(getattr(a, "id", "") or "") for a in available or ()}
        all_ids = [str(getattr(a, "id", "") or "") for a in all_accounts or ()]
        if len(avail_ids) >= len(all_ids):
            return
        def _reason(aid: str) -> str:
            got = cooldown_reason(tracker, aid, labels)
            if got:
                return got
            if region is not None:
                for a in all_accounts or ():
                    if str(getattr(a, "id", "") or "") == aid:
                        if getattr(a, "region", None) not in (None, region):
                            return f"region 不匹配（本通道 {region}）"
                        break
            return "未纳入可用集合"

        info = explain_skip(provider_id, all_ids, [str(x) for x in avail_ids],
                            reason_for=_reason)
        if log is not None:
            log.info("account_skip: %s", _json_dumps(provider_id, info))
    except Exception:  # noqa: BLE001 - 见 docstring
        pass


def _json_dumps(provider_id: str, info: dict[str, Any]) -> str:
    """诊断行 payload（延迟 import json：本模块其余部分不需要它）。"""
    import json

    return json.dumps({"provider": provider_id, **info}, ensure_ascii=False)
