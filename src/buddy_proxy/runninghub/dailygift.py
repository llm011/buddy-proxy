"""RunningHub 每日赠送积分自动领取。

为什么要这一层：站内「每日赠送 30 积分」是**手动领**的——网页端登录会自动触发
（``loginCoinTriggered``），脚本登录不会。不领的话新号 ``activityPoint=0``，跑应用
直接 414 NOT_ENOUGH_POWER_VALUE。之前只在「有人点刷新 / 提交」时顺手领（见
``websession.get_balance(claim_gift=True)``），没人开面板就永远不领。

这里的循环和 ``benefits.BenefitsManager`` 同一套骨架：``tick`` 定期跑、按「站点今天
是否已领」判断、单号失败退避、手动触发不受退避限制。

「站点今天」怎么算：积分在**站点时间 00:00**（= UTC 16:00）清零重置，所以
``site_day_key`` 用 UTC 16:00 之后的日期。清零后 10 分钟内本循环就会给每个号领一次。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import store, websession
from .runner import WebAccountSession, refresh_balance
from .websession import DAILY_GIFT_RESET_HOUR_UTC, WebSessionError

log = logging.getLogger(__name__)

#: 多久巡一轮（秒）。清零后最多这么久才领到；和 benefits 的 600s 对齐。
CHECK_INTERVAL_S = float(os.environ.get("RUNNINGHUB_GIFT_CHECK_INTERVAL_S", "600"))
#: 同一个号两次尝试之间至少隔多久（秒），避免网络抖动时反复打。
RETRY_THROTTLE_S = 300
#: 连续失败退避上限（秒）：15min → 30min → 1h → 2h → 4h。
BACKOFF_BASE_S = 900
BACKOFF_MAX_S = 4 * 3600
#: 领完顺手刷一次余额，让账号卡立刻显示 30 分
REFRESH_AFTER_CLAIM = True


def _state_path() -> Path:
    env = os.environ.get("RUNNINGHUB_GIFT_STATE_PATH", "").strip()
    return Path(env) if env else (Path.home() / ".buddy-proxy" / "runninghub_gift.json")


def site_day_key(now: float | None = None) -> str:
    """站点「日」的 key：UTC 16:00 清零，所以 16:00 之后算第二天。

    例：2026-11-27 15:59Z → "2026-11-26"；16:00Z → "2026-11-27"。
    """
    t = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    shifted = t - timedelta(hours=DAILY_GIFT_RESET_HOUR_UTC)
    return shifted.strftime("%Y-%m-%d")


def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


class DailyGiftManager:
    """给所有邮箱账号自动领每日积分。随应用启停（uvicorn 生命周期）。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._state = _read_state()
        self._last_attempt: dict[str, float] = {}
        self._next_try: dict[str, float] = {}
        self._fails: dict[str, int] = {}

    # ---- 状态查询（给 UI / 测试）----
    def claimed_day(self, account_id: str) -> str | None:
        return (self._state.get(account_id) or {}).get("day")

    def is_claimed_today(self, account_id: str, now: float | None = None) -> bool:
        return self.claimed_day(account_id) == site_day_key(now)

    def status(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "interval_s": CHECK_INTERVAL_S,
            "day": site_day_key(),
            "accounts": {
                aid: {
                    "day": v.get("day"),
                    "claimed_at": v.get("at"),
                    "error": v.get("error"),
                    "next_try_in": max(0, int(self._next_try.get(aid, 0) - time.time())) or None,
                }
                for aid, v in self._state.items()
                if isinstance(v, dict)
            },
        }

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 自动领取异常不能影响代理转发
                log.exception("runninghub 每日积分自动领取循环异常")
            await asyncio.sleep(CHECK_INTERVAL_S)

    # ---- 核心 ----
    def _accounts(self) -> list[dict[str, Any]]:
        return [a for a in store.get_active_accounts() if a.get("email") and a.get("password")]

    async def tick(self, *, force: bool = False) -> list[str]:
        """巡一轮：给「站点今天还没领」的号领。``force=True`` 跳过退避和「已领」判断。

        返回这一轮**成功领到**的 account_id。单号失败只记状态不影响其他号。
        """
        # 先决定要打哪些号（同步、不耗 IO），再并行打——和 accounts/refresh 对齐
        targets: list[dict[str, Any]] = []
        now = time.time()
        for acc in self._accounts():
            aid = str(acc.get("account_id") or "")
            if not aid:
                continue
            # 已经领过站点今天的 → 跳。状态是落盘的，启动后首轮靠这个就能判断，
            # 不需要额外的「首轮补领」逻辑
            if not force and self.is_claimed_today(aid, now):
                continue
            # 节流 / 退避
            if not force:
                if now - self._last_attempt.get(aid, 0.0) < RETRY_THROTTLE_S:
                    continue
                if now < self._next_try.get(aid, 0.0):
                    continue
            self._last_attempt[aid] = now
            targets.append(acc)
        results = await asyncio.gather(*(self._claim_guarded(a) for a in targets))
        return [aid for (aid, ok) in results if ok]

    async def _claim_guarded(self, acc: dict[str, Any]) -> tuple[str, bool]:
        """单个号的领取 + 失败记账。永不抛，让 gather 里的兄弟不受影响。"""
        aid = str(acc.get("account_id") or "")
        now = time.time()
        try:
            ok = await self._claim_one(acc)
        except WebSessionError as exc:
            self._fails[aid] = self._fails.get(aid, 0) + 1
            backoff = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** (self._fails[aid] - 1)))
            self._next_try[aid] = now + backoff
            self._state[aid] = {"day": self.claimed_day(aid), "error": str(exc)[:200],
                                "error_at": now, "backoff_s": backoff}
            _write_state(self._state)
            log.warning("runninghub 自动领积分失败 %s: %s（%d 分钟后重试）", acc.get("label") or aid, exc, backoff // 60)
            return aid, False
        self._fails[aid] = 0
        self._next_try.pop(aid, None)
        return aid, ok

    async def _claim_one(self, acc: dict[str, Any]) -> bool:
        """领一个号并落状态。返回「这次是否领到」。"""
        aid = str(acc.get("account_id") or "")
        ws = WebAccountSession(acc, persist=store.update_session)
        ok, _ = await ws.call(lambda t: websession.claim_daily_gift(t))
        if ok is None:
            # claim_daily_gift 把上游业务错误 / 网络错误吞成 None 了（为了不炸 get_balance）；
            # 这里必须当成失败走退避，别记成「已领」
            raise WebSessionError("领取接口没给明确答复（网络 / 上游错误）", kind="upstream")
        now = time.time()
        if ok:
            self._state[aid] = {"day": site_day_key(now), "at": now, "error": None}
            log.info("runninghub 自动领积分成功 %s", acc.get("label") or aid)
        else:
            # 上游回 false = 今天已经领过（网页上领了 / 之前刷余额时顺手领了）。
            # 也记成「今天已领」，否则每轮都来问一次。
            self._state[aid] = {"day": site_day_key(now), "at": now, "error": None, "already": True}
        _write_state(self._state)
        if REFRESH_AFTER_CLAIM:
            try:
                # include_welfare=True 是有意的：领完账号卡要同时显示「今日积分 ✓」和免费次数，
                # 这里一天只跑一次，多一个往返无所谓。claim_gift=False：就是本函数刚领过
                bal = await refresh_balance(ws, include_welfare=True, claim_gift=False)
                store.update_account_info(aid, bal)
            except WebSessionError:
                pass  # 余额刷不出来不影响「已领」的事实
        return ok

    async def claim_now(self, account_id: str | None = None) -> dict[str, Any]:
        """手动触发（UI 按钮）：不走退避，已经领过也照问一次上游。并行打所有目标号。"""
        targets = [a for a in self._accounts() if not account_id or a.get("account_id") == account_id]

        async def _one(acc: dict[str, Any]) -> tuple[str, bool, str | None]:
            aid = str(acc.get("account_id") or "")
            try:
                return aid, await self._claim_one(acc), None
            except WebSessionError as exc:
                return aid, False, str(exc)[:200]

        out: dict[str, Any] = {"claimed": [], "already": [], "failed": {}}
        for aid, ok, err in await asyncio.gather(*(_one(a) for a in targets)):
            if err:
                out["failed"][aid] = err
            else:
                (out["claimed"] if ok else out["already"]).append(aid)
        return out


_manager: DailyGiftManager | None = None


def get_manager() -> DailyGiftManager:
    global _manager
    if _manager is None:
        _manager = DailyGiftManager()
    return _manager
