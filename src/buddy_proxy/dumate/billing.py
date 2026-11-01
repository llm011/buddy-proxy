"""DuMate 账号级「每日积分消耗」——喂总览的按渠道 / 按天积分图。

背景（2026-11-01 用户报障）：总览的积分图（``chart_credits.js`` 读
``STATS.model_daily`` 的 ``credit_sum``）看不到 dumate。原因是 dumate 的 chat
响应 ``usage`` 只有 token、不带单次积分（实测），于是逐条指标 ``credit`` 一直是
``None``、按 provider 聚合恒为 0，图把它整条过滤掉了。

账号侧有一份真实账单（``records/usage``）。但**不能拿一个宽窗口一次拉完**：
实测（2026-11-01）该端点对宽窗的合计**不含中间的独立日期**，且随窗口长度跳变——
1~10 天窗口返回的合计完全一样（都只含最近那天 27800.8）；14~25 天又都停在同一个
更大的数（今天 + 10-19）；只有拉到 30 天才把 10-06、10-05 凑回来，却仍漏掉
10-02——总之与逐日相加对不上。**按天单独查**（[当日 0 点, 次日 0 点]）则每一天
都是准的（10-05=6966.7、10-06=6417.8…，逐日相加 = 全量 42252.1）。所以这里
按天逐条查，不赌宽窗。

口径：这是**账号级真实扣减**——用户经本代理转发的、以及直接在 App 里用的都算
在内，不等于「本代理转发了多少」。dumate 单账号，就是这一个账号。

``get_day_totals`` 不阻塞调用方：命中缓存直接返回，缓存陈旧就在后台线程里
重扫。只在上游不可达 / 未登录时返回空——调用方静默跳过，图里 dumate 一栏
缺省，而不是错误地画成 0。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import httpx

from .checkin import fetch_usage_records

log = logging.getLogger(__name__)

#: 扫描窗口（天）——与总览 model_daily 的 30 天窗口对齐。
_DAYS = 30
#: 重扫间隔（秒）：已结束的日期扫过一次即定稿、重扫直接跳过，所以一次重扫
#: 只打一次上游（今天那天），可以把间隔压短、让「今日」跟得上。
_SCAN_TTL_S = 60.0
#: 单次上游查询超时（按天，取小值快速失败，别拖垮后台线程）。
_TIMEOUT_S = 6.0
#: 整轮一条都没查到（未登录 / 上游全挂）时的退避：这么久内不再重扫，别空转。
_FAIL_BACKOFF_S = 180.0
#: 首轮扫描的并发度：30 天串行要 ~16s（每天一次 TLS 握手，实测），并发把
#: 首轮压到 ~2s，让第一次打开总览就能看到 dumate。重扫只剩「今天」1 天，
#: 并发度无关紧要。
_SCAN_WORKERS = 8

_lock = threading.Lock()
#: date -> (points, final)。final=True 表示该天已结束、数值定稿，不再重查。
_cache: dict[str, tuple[float, bool]] = {}
_last_scan = 0.0
_scan_day = ""        # 上次扫描时的「今天」，跨日则立即重扫（把昨天定稿）
_scanning = False


def _local_date(ts: float) -> str:
    """本地自然日的 ``YYYY-MM-DD``（与 metrics._date_str 同口径）。"""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _day_window(d: str) -> tuple[int, int]:
    """本地日 ``YYYY-MM-DD`` → ``[当日 0 点, 次日 0 点)`` 的秒级时间戳。"""
    dt = datetime.strptime(d, "%Y-%m-%d")
    start = int(dt.timestamp())  # naive datetime 按本地时区解释
    return start, start + 86400


def _fetch_day(d: str, client: httpx.Client, auth: Any) -> float | None:
    """查某一天（本地日）的消耗合计；未登录 / 失败返回 None。

    ``client`` / ``auth`` 由 :func:`_refresh` 传入：一轮扫描共用一个连接、一份
    登录态，别 30 天各新开一次 TLS、重复解 cookie。
    """
    start, end = _day_window(d)
    rec = fetch_usage_records(
        start_ts=start, end_ts=end, page=1, limit=1, timeout=_TIMEOUT_S,
        client=client, auth=auth)
    if rec is None:
        return None
    return float(rec.get("consumed_points") or 0.0)


def _refresh() -> None:
    """后台全量重扫：只补还在变的日期，已定稿的跳过。"""
    global _last_scan, _scan_day, _scanning
    ok_any = False
    try:
        now = time.time()
        today = _local_date(now)
        # 待查日期：未定稿的（首轮=全部 30 天，重扫=只剩还没结束的今天）
        todo = []
        for i in range(_DAYS):
            d = _local_date(now - i * 86400)
            with _lock:
                ent = _cache.get(d)
            if not (ent and ent[1]):
                todo.append(d)
        if todo:
            from .cookies import resolve_bceconsole_auth

            auth = resolve_bceconsole_auth()
            if auth is None:
                return  # 未登录：整轮跳过（finally 里按「空手」退避）
            # 一轮共用一个连接池：30 天走同一条 TCP/TLS，别各开各的。
            with httpx.Client(timeout=httpx.Timeout(_TIMEOUT_S)) as client:
                with ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as pool:
                    for d, pts in zip(
                            todo, pool.map(lambda x: _fetch_day(x, client, auth), todo)):
                        if pts is None:
                            continue  # 上游失败：保留旧值（如有），本轮不覆盖
                        ok_any = True
                        with _lock:
                            _cache[d] = (pts, d < today)
        # 清掉滑出窗口的旧日期，别让缓存无限长
        cutoff = _local_date(now - (_DAYS - 1) * 86400)
        with _lock:
            for d in list(_cache):
                if d < cutoff:
                    _cache.pop(d, None)
    except Exception:  # noqa: BLE001 - 后台刷新绝不外抛
        log.debug("dumate billing refresh failed", exc_info=True)
    finally:
        with _lock:
            # 整轮全成功 → 距下次重扫 TTL；整轮空手（未登录/上游挂）→ 退避，
            # 别每个 30s 轮询都去踹一个登不上的上游。
            _last_scan = time.time() + (_SCAN_TTL_S if ok_any else _FAIL_BACKOFF_S)
            _scan_day = _local_date(time.time())
            _scanning = False


def _maybe_refresh() -> None:
    global _scanning
    now = time.time()
    with _lock:
        stale = now >= _last_scan or _scan_day != _local_date(now)
        if not stale or _scanning:
            return
        _scanning = True
    threading.Thread(target=_refresh, name="dumate-billing", daemon=True).start()


def get_day_totals() -> dict[str, float]:
    """返回已知的 ``{YYYY-MM-DD: 消耗积分}``（可能为空），顺带触发后台刷新。

    永不阻塞调用方，永不抛。首次调用（缓存空）返回 ``{}`` 并启动扫描；扫描
    完成后（约几秒）下一次调用就有数据。
    """
    try:
        _maybe_refresh()
    except Exception:  # noqa: BLE001
        pass
    with _lock:
        return {d: p for d, (p, _final) in _cache.items() if p}


def reset_cache() -> None:
    """清空缓存（测试用）。"""
    global _last_scan, _scan_day, _scanning
    with _lock:
        _cache.clear()
        _last_scan = 0.0
        _scan_day = ""
        _scanning = False