"""PAT 额度查询：按账号拉取 entitlement 包并归一为 UI 额度项。"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import socket
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

from fastapi import HTTPException

# 接缝约定：函数体内对「测试可注入接缝」（monkeypatch 打在本包命名空间上的
# 名字，见包 __init__ 兼容约定）及包内共享状态经 _ns 调用期解析。
import buddy_proxy.trae.pat as _ns

from .config import _PLUS_GATEWAY, PatProfile, ensure_pat_config
from .credentials import _pat_headers
from .store import _account_state, _mutate_account

log = logging.getLogger(__name__)

# ───────────────────────── 额度查询 ─────────────────────────
#
# 本函数服务于 /ui 额度页，**必须在秒级返回**：页面 30s 轮询一次，而
# benefits 快照是常驻数据集，慢在这里＝整页白屏。
#
# 2026-09-24 复盘：网关 DNS 不可达时曾经慢到 245s（10 账号 × 20s 超时 +
# 每账号一次 ~5s 的 http.client 连接重试退避），前端表现为「额度页一直不
# 出来、点刷新也没用」，而纯本地的 overview/stats/models 毫秒级正常。
# 因此这里两道闸：进循环前探测网关可达性（不可达直接走缓存），以及每账号
# 请求用短超时；并发跑账号，最坏耗时不再随账号数线性增长。

# 单账号额度请求超时：展示用途，6s 足够（实测正常响应亚秒级）。
_QUOTA_TIMEOUT_S = 6.0
# 并发账号数上限：够快，又不至于一次打太多连接。
_QUOTA_MAX_WORKERS = 6
# 整轮查询上限：超过就先拿缓存渲染（未回的请求后台跑完写缓存，下轮生效）。
# 单账号超时 6s × ceil(账号数/workers) 会随账号数线性涨（10 账号 12s、20 账号
# 24s），这里给整轮封顶，让页面延迟不随账号数失控。
_QUOTA_ROUND_DEADLINE_S = 8.0
# 网关可达性探测总预算（DNS + 全部 TCP 尝试，不含 TLS）。
# 注意是**总预算**而非每地址超时：解析出多地址时逐个尝试，若各自跑满超时，
# 探测本身就会变成新的慢点（实测 2 地址 × 1.5s = 3s）。这里按整体 deadline 收口。
_QUOTA_PROBE_BUDGET_S = 1.2
# 可达性探测结果缓存秒数：避免 30s 一轮的轮询每次都做探测。
_GATEWAY_PROBE_TTL_S = 30.0
# 探测结论缓存 {"key": gateway_url, "checked_at": float, "reachable": bool}
_gateway_probe_cache: dict[str, Any] = {}
# 常驻查询线程池：按并发上限惰性建一次，避免慢轮残留线程被反复新建
_quota_pool_obj: concurrent.futures.ThreadPoolExecutor | None = None
_quota_pool_lock = threading.Lock()


def _quota_pool() -> concurrent.futures.ThreadPoolExecutor:
    """取常驻查询线程池（惰性创建，只建一次）。"""
    global _quota_pool_obj
    with _quota_pool_lock:
        if _quota_pool_obj is None:
            _quota_pool_obj = concurrent.futures.ThreadPoolExecutor(
                max_workers=_QUOTA_MAX_WORKERS, thread_name_prefix="pat-quota")
        return _quota_pool_obj


def _gateway_reachable(plus: str, budget: float = _QUOTA_PROBE_BUDGET_S) -> bool:
    """探测 plus 网关 DNS+TCP 是否可达。

    ``urllib`` 对连接类失败会带一次隐式重试且退避约 5s（实测），串行 10 个
    账号就是近 1 分钟起步——这类「网络整体不可达」必须提前短路，而不是让
    每个账号各自去撞一遍。与 ``keeper._exchange_env_ready`` 同思路：**只做
    DNS + TCP 预检，不建 TLS、不发请求**，探测失败只意味着「本轮跳过主动
    查询」，已缓存的额度照常展示。

    **这只是粗筛，不保证后续请求一定通**：DNS 正常、TCP 也能连上、但接口
    层不响应（中继挂了/被限流）时探测照样通过。那种情况由整轮上限
    （``_QUOTA_ROUND_DEADLINE_S``）兜底，别指望预检能全覆盖。

    ``budget`` 是整轮探测的总预算（不是每地址超时）：地址逐个试，但剩余
    时间不够就立刻放弃，避免「多地址 × 每地址超时」把探测本身变成慢点。
    """
    host, port = _gateway_host_port(plus)
    if not host:
        return True  # 解析不出主机名：交给真正的请求去报错，不在这里下结论
    deadline = time.monotonic() + budget
    for info in _resolve(host, port):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            with socket.create_connection(info[4][:2], timeout=remaining):
                return True
        except OSError:
            continue
    return False


def _gateway_host_port(gateway: str) -> tuple[str, int]:
    try:
        parts = urllib.parse.urlsplit(gateway)
    except ValueError:
        return "", 0
    return parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)


def _resolve(host: str, port: int) -> list[Any]:
    try:
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return []


def _gateway_ok(plus: str) -> bool:
    """带短 TTL 缓存的网关可达性判断（一次轮询/刷新只探测一次）。"""
    now = time.time()
    cached = _gateway_probe_cache
    if cached.get("key") == plus and now - float(cached.get("checked_at") or 0) < _GATEWAY_PROBE_TTL_S:
        return bool(cached.get("reachable"))
    # 经 _ns 调用期解析：与包内其它接缝一致，测试可在本包命名空间打桩
    reachable = _ns._gateway_reachable(plus)
    _gateway_probe_cache.update({"key": plus, "checked_at": now, "reachable": reachable})
    return reachable


def _cached_quota(profile: PatProfile, quota_class: str) -> list[dict[str, Any]] | None:
    state = _account_state(profile.cache_key)
    quotas = state.get("quota") if isinstance(state.get("quota"), dict) else {}
    value = quotas.get(quota_class) if isinstance(quotas, dict) else None
    items = value.get("items") if isinstance(value, dict) else None
    return items if isinstance(items, list) else None


def _save_quota(profile: PatProfile, quota_class: str, items: list[dict[str, Any]]) -> None:
    def store(state: dict[str, Any]) -> None:
        quotas = state.setdefault("quota", {})
        quotas[quota_class] = {"items": items, "fetched_at": time.time()}
    _mutate_account(profile.cache_key, store)


def _quota_items(data: dict[str, Any], profile: PatProfile, multi: bool) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for pack in data.get("user_entitlement_pack_list") or []:
        base = pack.get("entitlement_base_info") or {}
        quota = base.get("quota") or {}
        usage = pack.get("usage") or {}
        limit = quota.get("basic_usage_limit")
        used_raw = usage.get("basic_usage_amount")
        if not isinstance(limit, (int, float)) or limit <= 0:
            continue
        entitlement_id = str(base.get("entitlement_id") or "pack")
        if "weekly" in entitlement_id:
            # 上游 entitlement_id 是 free_weekly_*（周期重置的通用池），但这池实际
            # 对应 openrouter/es1_orange_* 那条上游线，用户侧按「Orange 包」认知，
            # 故 UI 直呼其名而非笼统的「周包/通用额度」。
            label = "PAT Orange 包"
        elif "daily" in entitlement_id:
            # 同一账号会同时返回多个日包（例如 GPT-5.6 与 GPT-6），不能都叫
            # “高级模型共享”，否则 UI 看起来像重复额度。只从 entitlement_id 的
            # 已知后缀提取公开模型系列，不展示账号标识或其它原始字段。
            family = ""
            if entitlement_id.endswith("_gpt_56_sol"):
                family = "GPT-5.6 Sol"
            elif entitlement_id.endswith("_gpt_6_1"):
                family = "GPT-6.1"
            elif entitlement_id.endswith("_gpt_6"):
                family = "GPT-6"
            label = f"PAT 日包（{family or '高级模型'}）"
        else:
            label = "PAT 额度包"
        if multi:
            label = f"PAT #{profile.index + 1} · {label[4:]}"
        end_time = base.get("end_time") or 0
        # ``used`` 缺失时**不能**按 0 算：那是「未知」，不是「没花」。
        # 只回了包容量、还没回用量时（网关偶发只给半拉数据），按 0 算会渲染成
        # 「剩 300 / 300 · 已用 0%」——把未知说成满血，比不显示更误导。
        # 此时只给 remaining（total），percent 留 None，UI 会显示「已用未知」。
        if isinstance(used_raw, (int, float)):
            used: float | None = round(float(used_raw), 2)
            remaining: float | None = round(limit - used, 2)
            percent: int | None = round(used / limit * 100)
        else:
            used = None
            remaining = limit
            percent = None
        items.append({"label": label, "used": used, "total": limit,
                      "remaining": remaining,
                      "percent": percent,
                      # PAT 的 weekly / daily 包是**周期重置的池**，``end_time``
                      # 是本周期结束、到点额度回满（cooldown.py 实测：「日包按
                      # 00:00 重置」、4031 的 extra 分 daily/weekly 两池）。
                      # 所以走 reset_ts 不走 expire_ts——后者会让到期横幅每天
                      # 喊一次「PAT 日包要到期了」（其实只是又要刷新了）。
                      "reset_ts": int(end_time) if end_time else None,
                      "expire_ts": None,
                      "unit": "credit"})
    return items


def _failure_notice(failed: int, total: int, gateway_ok: bool) -> dict[str, Any]:
    """额度查询失败时给 UI 的说明条目。

    区分「网关整体不可达」和「网关可达但账号查询失败」：前者是网络问题（常见于
    离开内网），用户无需反复点刷新；后者才需要看具体账号。绝不把上游地址、
    token 或账号标识带进 UI。
    """
    if not gateway_ok:
        return {
            "label": "PAT 额度网关不可达",
            "used": None, "total": None, "percent": None, "reset_ts": None,
            "remaining": (f"{failed}/{total} 个账号本轮未取到新数据（网络不可达）；"
                          f"下方为上次成功查询的缓存，稍后会自动重试"),
            "unreachable": True, "query_failed": True,
        }
    return {
        "label": "PAT 额度查询失败",
        "used": None, "total": None, "percent": None, "reset_ts": None,
        "remaining": f"{failed}/{total} 个账号查询失败（下方可能为缓存数据）",
        "unreachable": False, "query_failed": True,
    }


def _fetch_one_account(
    profile: PatProfile, plus: str, multi: bool, gateway_ok: bool
) -> tuple[list[dict[str, Any]], bool]:
    """单账号额度：成功返回 (items, True)，失败回退该账号缓存并返回 (items, False)。

    ``gateway_ok=False`` 时跳过请求直接走缓存：网络整体不可达时不必让每个账号
    各自去撞一遍超时（那正是额度页卡死的来源）。
    """
    if not gateway_ok:
        cached = _cached_quota(profile, "advanced")
        return (
            [dict(item, label=f"{item['label']}·缓存") for item in cached] if cached else []
        ), False
    try:
        credentials = _ns._get_profile_credentials(profile)
        request = urllib.request.Request(
            f"{plus}/trae/api/v1/pay/ide_user_ent_usage", data=b"{}", method="POST",
            headers=_pat_headers(credentials, "application/json"))
        with urllib.request.urlopen(request, timeout=_QUOTA_TIMEOUT_S) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        items = _quota_items(data, profile, multi)
        _save_quota(profile, "advanced", items)
        return items, True
    except Exception as exc:
        # 只记异常类型：额度查询失败多为网络层问题，具体消息可能带上游地址，
        # 不进日志也不进 UI（UI 用 _failure_notice 给出脱敏文案）。
        cached = _cached_quota(profile, "advanced")
        log.warning("PAT 额度查询失败，账号序号=%d（%s）", profile.index, type(exc).__name__)
        return (
            [dict(item, label=f"{item['label']}·缓存") for item in cached] if cached else []
        ), False


def fetch_pat_ent_usage() -> list[dict[str, Any]]:
    """按账号**并发**查询 advanced 额度；失败只使用该账号自己的缓存。

    网关不可达时整轮跳过主动查询、直接用缓存（见模块头注释的 245s 复盘）。
    各账号互不影响：单账号请求/缓存/日志都在 ``_fetch_one_account`` 内，
    结果按 ``profiles`` 原顺序拼接，保证 UI 展示顺序稳定。

    末尾附带被动采集的 standard 池数据（若有）：standard 走外网中继、无主动
    查询接口，唯一信号源是撞 4031 时错误体 ``extra`` 携带的 used/quota，由
    ``_record_standard_pool_4031`` 在撞码时写入**撞码账号自己**的缓存（4031
    实测为账号级分桶，非租户共享）。
    """
    plus = os.environ.get(_PLUS_GATEWAY, "").strip().rstrip("/")
    if not plus:
        raise HTTPException(status_code=503, detail="PAT 通道未配置 TRAE_PAT_PLUS_GATEWAY")
    profiles = list(ensure_pat_config())
    if not profiles:
        return list(_ns._standard_pool_items())
    multi = len(profiles) > 1

    gateway_ok = _gateway_ok(plus)
    if not gateway_ok:
        log.warning("PAT 额度网关不可达，本轮跳过主动查询，仅展示各账号缓存")

    def run(profile: PatProfile) -> tuple[list[dict[str, Any]], bool]:
        return _fetch_one_account(profile, plus, multi, gateway_ok)

    if len(profiles) == 1:
        results = [run(profiles[0])]
    else:
        # 复用一个常驻线程池（不是每轮新建）：每轮新建 + shutdown(wait=False)
        # 会让「撞上整轮上限」的慢轮把线程留在后台继续跑，下一轮再新建一批，
        # 线程数随轮次线性累积（实测 5 轮 7→27）。常驻池的上限天然封顶，
        # 慢轮残留的线程占用名额，后续轮次自然排队而不会无限膨胀。
        #
        # 注意不能用 with / shutdown(wait=True)：那会 join 所有线程，把整轮
        # 上限架空（实测慢请求下仍要等满 60s）。
        pool = _quota_pool()
        futures = [pool.submit(run, profile) for profile in profiles]
        results = _collect_with_deadline(futures, profiles)

    all_items: list[dict[str, Any]] = []
    failures = 0
    for items, ok in results:
        if not ok:
            failures += 1
        all_items.extend(items)
    standard_pool = _ns._standard_pool_items()
    if standard_pool:
        all_items.extend(standard_pool)
    if failures:
        # 有账号失败：说明条与被查到的条目并存，页面照常渲染可用数据，顶部能
        # 直接看到「几个账号没取到」。这里刻意**不抛 502**——额度页是常驻数据集，
        # 抛错会让整页（含其它 provider 的额度）一起失败，而失败原因往往只是
        # 本机网络不通，缓存里的数据依然有意义。说明条已带 unreachable/
        # query_failed 标记，前端用警告色区分。
        all_items.insert(0, _failure_notice(failures, len(profiles), gateway_ok))
    return all_items


def _collect_with_deadline(
    futures: list[concurrent.futures.Future], profiles: list[PatProfile]
) -> list[tuple[list[dict[str, Any]], bool]]:
    """收并发结果，整体不超过 ``_QUOTA_ROUND_DEADLINE_S``。

    没有整轮上限时，最坏耗时是 ``ceil(账号数 / workers) × 单账号超时``：本机
    10 账号 = 12s，20 账号就 24s——「网关 TCP 能连上、但接口层不响应」时（预检
    挡不住这种）页面就得干等这么久。这里给整轮兜一个上限，超时未回的账号直接
    用它的缓存顶上，先让页面出来；那些请求仍在后台跑完并写缓存，下一轮就能拿到。

    结果顺序与 ``profiles`` 对齐（未回的用缓存），保证 UI 展示顺序稳定。
    """
    deadline = time.monotonic() + _QUOTA_ROUND_DEADLINE_S
    results: list[tuple[list[dict[str, Any]], bool]] = []
    for profile, future in zip(profiles, futures):
        remaining = deadline - time.monotonic()
        try:
            results.append(future.result(timeout=max(0.0, remaining)))
        except concurrent.futures.TimeoutError:
            cached = _cached_quota(profile, "advanced")
            log.warning("PAT 额度查询超过整轮上限，账号序号=%d 本轮沿用缓存", profile.index)
            results.append((
                [dict(item, label=f"{item['label']}·缓存") for item in cached] if cached else [],
                False,
            ))
        except Exception as exc:  # 单账号内部已兜错，这里只兜线程池自身异常
            log.warning("PAT 额度查询线程异常，账号序号=%d（%s）",
                        profile.index, type(exc).__name__)
            results.append(([], False))
    return results
