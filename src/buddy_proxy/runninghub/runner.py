"""RunningHub 站内运行器：拿账号登录态，模拟网页「生成」按钮，轮询到结果。

和 ``provider.py``（走 OpenAPI，要 api_key + 钱包余额）的区别：

- 这里**花的是站内过渡币 / 积分**，只有登录态能花，所以账号必须是邮箱密码；
- 预检**不再**拦人：余额够不够让上游说，我们只负责把上游原文讲清楚；
- 登录态在 ``store`` 里持久化，401/412 自动续期/重登，续完写回。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from . import websession
from .client import RunningHubError
from .websession import WebSession, WebSessionError

#: 站内任务轮询间隔 / 最长等待（秒）。工作流平均 60–90 秒出图。
POLL_INTERVAL = float(os.environ.get("RUNNINGHUB_WEB_POLL_INTERVAL", "4.0"))
MAX_WAIT = float(os.environ.get("RUNNINGHUB_WEB_MAX_WAIT", "900.0"))

#: 「闭源模型」节点：RH 服务器**内部**再去调字节 / 可灵 / OpenAI 等官方 API，扣的是
#: **钱包 $**，站内过渡币也不认。2026-11-27 实测 Seedance 30s 应用（2086711729156894722）
#: 网页端提交 3 秒即 FAILED，exception_message 就是 605 NOT_ENOUGH_BALANCE。
#: 预估接口对这类工作流回 ``$0.00 / nodes: []``（预估阶段还没走到那一步），不能信。
_CLOSED_SOURCE_NODE_PREFIXES = (
    "RH_OpenAPI_", "RH_Bytedance", "RH_Seedance", "RH_Kling", "RH_Veo", "RH_Sora",
    "RH_Wan", "RH_Hailuo", "RH_Minimax", "RH_GPT", "RH_OpenAI", "RH_Claude", "RH_Gemini",
    "RH_Flux", "RH_Midjourney", "RH_Runway", "RH_Pika", "RH_Luma", "RH_Suno", "RH_ElevenLabs",
)


def closed_source_nodes(estimate: dict[str, Any]) -> list[str]:
    """从价格预估的 ``nodes[].classType`` 里挑出闭源节点；预估为空列表时返回 ``["?"]``。

    返回非空 = 这个工作流要扣钱包，过渡币跑不了。``["?"]`` 是「预估没给节点」——
    实测闭源应用就是这样，所以也按「大概率扣钱包」处理。
    """
    nodes = estimate.get("nodes") if isinstance(estimate, dict) else None
    if not isinstance(nodes, list) or not nodes:
        return ["?"]
    hits = []
    for n in nodes:
        ct = str((n or {}).get("classType") or "")
        if any(ct.startswith(p) or p in ct for p in _CLOSED_SOURCE_NODE_PREFIXES):
            hits.append(ct)
    return hits


def parse_task_failure(raw: dict[str, Any]) -> str:
    """把 ``taskResultDesc``（一段 JSON 的 ComfyUI 异常）拆成人能读的原因。

    之前 UI 只显示 ``任务失败: FAILED``，真正的 605 藏在 ``exception_message`` 里，
    用户得自己去翻历史——这里把它挖出来。
    """
    desc = (raw or {}).get("taskResultDesc") or (raw or {}).get("errorMessage") or ""
    if isinstance(desc, dict):
        return str(desc.get("exception_message") or desc.get("exception_type") or desc)
    try:
        d = json.loads(desc)
        if isinstance(d, dict):
            msg = str(d.get("exception_message") or "")
            node = str(d.get("node_name") or "")
            if msg:
                return f"{node}: {msg}" if node else msg
            return str(d.get("exception_type") or "FAILED")
    except (TypeError, ValueError):
        pass
    return str(desc)[:300] if desc else "FAILED"


@dataclass
class WebRunResult:
    task_id: str
    status: str
    outputs: list[dict[str, str]]
    estimate: dict[str, Any]
    raw: dict[str, Any]


def _outputs_from_history(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """``/api/output/v2/history`` 的记录 → ``[{url, type}]``。

    ``outputType`` 可能是扩展名（png/mp4）也可能是 MIME，统一映射成 image/video/audio。
    """
    out: list[dict[str, str]] = []
    for r in records:
        url = r.get("fileUrl") or r.get("filePreviewUrl")
        if not url:
            continue
        out.append({"url": str(url), "type": _media_type(r.get("outputType"))})
    return out


def _media_type(v: Any) -> str:
    s = str(v or "").lower()
    if any(k in s for k in ("png", "jpg", "jpeg", "webp", "gif", "image", "img")):
        return "image"
    if any(k in s for k in ("mp4", "webm", "mov", "video")):
        return "video"
    if any(k in s for k in ("mp3", "wav", "flac", "audio")):
        return "audio"
    return "file"


class WebAccountSession:
    """一个账号的「带持久化的会话」：每次续期/重登成功都写回 store。"""

    def __init__(self, account: dict[str, Any], *, persist=None):
        self.account_id = str(account.get("account_id") or "")
        self.email = str(account.get("email") or "")
        self.password = str(account.get("password") or "")
        self.session = WebSession.from_dict(account.get("web_session") or {})
        self._persist = persist
        if not (self.email and self.password):
            raise WebSessionError(
                f"账号 {account.get('label') or self.account_id} 没有邮箱密码，无法走站内运行",
                kind="login",
            )

    def _save(self) -> None:
        # 登录失败时 ensure 会返回一个空 token 的会话对象（只为了记冷却），不能把它写回 store
        if self._persist and self.session is not None and self.session.access_token:
            self._persist(self.account_id, self.session.to_dict())

    async def ensure(self, *, force: bool = False) -> str:
        # ensure_session 失败时会把「冷却」记在它返回/持有的那个对象上——所以要先把手里的
        # 引用交出去（没有就造一个空的），失败后 self.session 才带着 last_error_at，
        # 下一次 ensure 才会进冷却。否则每次请求都重新撞密码错。
        if self.session is None:
            self.session = WebSession(access_token="", refresh_token="", expires_at=0.0)
        self.session = await websession.ensure_session(
            self.session, self.email, self.password, force=force
        )
        self._save()
        return self.session.access_token

    async def call(self, fn, *, force_fresh: bool = False):
        """``fn(token)`` → ``(result, token)``；遇 401/412 自动续一次。"""
        if force_fresh:
            token = await self.ensure(force=True)
            return await fn(token), token
        token = await self.ensure()
        try:
            return await fn(token), token
        except WebSessionError as exc:
            if exc.kind != "refresh":
                raise
            # 只记文案，**不**记 last_error_at：冷却（in_cooldown）只由 login()/refresh()
            # 真正失败那一支触发。否则这里一记，下面 force 重登再失败时，下次请求
            # 看到的是「刚失败过…5 分钟内不重试」而不是真实的「密码错了」。
            if self.session is not None:
                self.session.last_error = f"token 失效: {exc}"
        token = await self.ensure(force=True)
        return await fn(token), token


async def refresh_balance(
    acc: WebAccountSession, *, include_welfare: bool = False, claim_gift: bool = False
) -> dict[str, Any]:
    """拉站内余额，并把 token 带回（调用方据此写回 store）。

    ``include_welfare=True`` 多拉一次免费次数（账号卡刷新时用）；submit 前不要，省一个往返。
    ``claim_gift`` 默认 False：领积分收口到 ``dailygift.DailyGiftManager``（它有状态文件），
    这里顺手领会让徽标显示「待领取」而积分其实已经领了。只有 manager 自己传 True。
    """
    balance, _ = await acc.call(lambda t: websession.get_balance(t, include_welfare=include_welfare, claim_gift=claim_gift))
    return balance


async def submit_task(
    acc: WebAccountSession,
    webapp_id: str,
    node_info_list: list[dict[str, Any]],
    *,
    estimate_only: bool = False,
) -> dict[str, Any]:
    """预估 + 提交，**不等待**。返回 ``{taskId, status, estimate, inputs}``。

    ``estimate_only=True`` 只做价格预估（免费、不扣任何东西），不提交。
    """
    inputs = websession.normalize_inputs(node_info_list)
    estimate, _ = await acc.call(lambda t: websession.price_estimate(webapp_id, inputs, t))
    if estimate_only:
        return {"taskId": "", "status": "ESTIMATE", "estimate": estimate, "inputs": inputs}
    created, _ = await acc.call(lambda t: websession.create_task(webapp_id, inputs, t))
    return {"taskId": created["taskId"], "status": created.get("status") or "RUNNING",
            "estimate": estimate, "inputs": inputs}


async def poll_task(
    acc: WebAccountSession,
    webapp_id: str,
    task_id: str,
    *,
    poll_interval: float | None = None,
    max_wait: float | None = None,
    should_cancel=None,
) -> WebRunResult:
    """轮询到终态并取结果。``should_cancel()`` 返回 True 时主动放弃（任务标 CANCELLED）。

    默认值在**调用时**才从模块常量读，不写在签名里——签名里的默认值是 import 时就
    定死的，测试 monkeypatch 和环境变量都改不动（踩过：测试改了 0.01 还是等 4s）。
    """
    poll_interval = POLL_INTERVAL if poll_interval is None else poll_interval
    max_wait = MAX_WAIT if max_wait is None else max_wait
    deadline = asyncio.get_running_loop().time() + max_wait
    status = "RUNNING"
    last_raw: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        if should_cancel is not None and should_cancel():
            raise WebSessionError(f"任务已取消 (taskId={task_id})", kind="cancel")
        recs, _ = await acc.call(lambda t: websession.task_list(t, task_id=task_id))
        if recs:
            status = str(recs[0].get("taskStatus") or status).upper()
            last_raw = recs[0]
            if status not in ("RUNNING", "QUEUED"):
                break
        await asyncio.sleep(poll_interval)

    if status in ("RUNNING", "QUEUED"):
        raise WebSessionError(f"任务超时 (taskId={task_id}, status={status})", kind="timeout")
    if status != "SUCCESS":
        raise WebSessionError(
            f"任务失败: {parse_task_failure(last_raw)} (taskId={task_id})", kind="upstream"
        )
    hist, _ = await acc.call(lambda t: websession.output_history(t, webapp_id=webapp_id, task_id=task_id))
    return WebRunResult(task_id=task_id, status=status, outputs=_outputs_from_history(hist),
                        estimate={}, raw=last_raw)


async def run_workflow(
    acc: WebAccountSession,
    webapp_id: str,
    node_info_list: list[dict[str, Any]],
    *,
    estimate_only: bool = False,
    poll_interval: float | None = None,
    max_wait: float | None = None,
) -> WebRunResult:
    """同步跑完（提交 → 轮询 → 取结果）。给脚本 / 测试 / 旧调用方用；UI 走 ``execute_task``。"""
    sub = await submit_task(acc, webapp_id, node_info_list, estimate_only=estimate_only)
    if estimate_only:
        return WebRunResult(task_id="", status="ESTIMATE", outputs=[], estimate=sub["estimate"], raw={})
    res = await poll_task(acc, webapp_id, sub["taskId"], poll_interval=poll_interval, max_wait=max_wait)
    res.estimate = sub["estimate"]
    return res


# ---------------------------------------------------------------------------
# 后台执行器：提交即落盘，状态一路写回 store
# ---------------------------------------------------------------------------

#: 进程内的取消标记：taskId -> True。取消只是「不再等它了」，站内任务本身照跑（RH 没有
# 取消接口给网页端用），所以记录里写清楚，别让用户以为币会退。
_cancel_flags: set[str] = set()
#: 进程内正在跑的执行器，防止重启后重复提交
_running: set[str] = set()


def is_cancelled(task_id: str) -> bool:
    return task_id in _cancel_flags


def reap_orphans() -> list[str]:
    """进程重启后，上一进程留下的 SUBMITTING / RUNNING 记录没人推进了——标成 INTERRUPTED。

    站内任务本身不受影响（可能早就跑完了），只是我们这边丢了跟踪；UI 会提示去
    站内历史看结果。返回被标记的 taskId 列表。服务启动时调一次。
    """
    from . import store as rh_store

    reaped: list[str] = []
    for t in rh_store.list_tasks():
        tid = str(t.get("taskId") or "")
        if t.get("status") in ("SUBMITTING", "RUNNING", "QUEUED") and tid not in _running:
            rh_store.update_task(
                tid, status="INTERRUPTED",
                error="服务重启，丢失跟踪；站内任务可能已跑完，请去站内「任务历史」查看",
                finishedAt=time.time(),
            )
            reaped.append(tid)
    return reaped


def request_cancel(task_id: str) -> bool:
    _cancel_flags.add(task_id)
    return True


#: 站内「算力 / 积分不够」：2026-11-27 实测 activityPoint 只剩 19、预估要 21 时
#: webapp/create 回 414 TASK_CREATE_FAILED_BY_NOT_ENOUGH_POWER_VALUE——**不是**钱包、
#: 不是闭源节点，是每日赠送的积分用完了（当天 16:00 UTC 清零重置）。
POWER_INSUFFICIENT_MARKERS = ("NOT_ENOUGH_POWER", "POWER_VALUE", "INSUFFICIENT_POWER")


def _finalize_hint(exc: WebSessionError) -> tuple[str, bool]:
    """把异常映射成 (status, 是否闭源 605)。"""
    msg = str(exc)
    closed = "605" in msg or "NOT_ENOUGH_BALANCE" in msg or "balance is insufficient" in msg.lower()
    if exc.kind == "cancel":
        return "CANCELLED", False
    if exc.kind == "timeout":
        return "TIMEOUT", False
    if exc.kind == "login":
        return "FAILED", False
    return "FAILED", closed


async def execute_task(
    account: dict[str, Any],
    task_id: str,
    *,
    persist_session=None,
    poll_interval: float | None = None,
    max_wait: float | None = None,
    balance_before: float | None = None,
) -> dict[str, Any] | None:
    """后台跑一个**已落盘**的任务：预估 → 提交 → 轮询 → 写结果。

    调用方（UI 端点）先 ``store.upsert_task`` 一条 ``status=SUBMITTING`` 的占位记录
    （``taskId`` 用本地生成的），再 ``asyncio.create_task(execute_task(...))``。
    这里拿到站内真 taskId 后把 ``upstreamTaskId`` 写回去，其余字段原样保留。

    任何一步失败都写进记录（``status=FAILED`` + ``error`` + ``errorCode``），**不抛**——
    后台任务抛出去没人接，只会变成一条 "Task exception was never retrieved" 的日志。
    """
    from . import store as rh_store

    if task_id in _running:
        return None
    _running.add(task_id)
    rec = rh_store.get_task(task_id) or {}
    webapp_id = str(rec.get("webappId") or "")
    inputs: list[dict[str, Any]] = list(rec.get("inputs") or [])
    try:
        # 提交前先看一眼取消：SUBMITTING 那一两秒点「取消」应该等于「没提交、没扣币」，
        # 不能等 poll_task 才发现——那时站内任务已经跑了
        if task_id in _cancel_flags:
            rh_store.update_task(task_id, status="CANCELLED", submitted=False,
                                 error="提交前已取消", finishedAt=time.time())
            return rh_store.get_task(task_id)
        acc = WebAccountSession(account, persist=persist_session)
        try:
            sub = await submit_task(acc, webapp_id, inputs)
        except WebSessionError as exc:
            status, closed = _finalize_hint(exc)
            rh_store.update_task(task_id, status=status, error=_humanize_submit_error(exc),
                                 errorCode=str(exc.raw_code or ""), closedSource=closed,
                                 upstreamTaskId=None, submitted=False, finishedAt=time.time())
            return rh_store.get_task(task_id)
        upstream_id = sub["taskId"]
        rh_store.update_task(task_id, status="RUNNING", upstreamTaskId=upstream_id, submitted=True,
                             estimate=_slim_estimate(sub["estimate"]),
                             closedSource=bool(closed_source_nodes(sub["estimate"]) != []))
        try:
            res = await poll_task(
                acc, webapp_id, upstream_id, poll_interval=poll_interval, max_wait=max_wait,
                should_cancel=lambda: task_id in _cancel_flags,
            )
        except WebSessionError as exc:
            status, closed = _finalize_hint(exc)
            rh_store.update_task(task_id, status=status, error=str(exc)[:400],
                                 errorCode=str(exc.raw_code or ""), closedSource=closed,
                                 submitted=True, finishedAt=time.time())
            return rh_store.get_task(task_id)
        rh_store.update_task(
            task_id, status="SUCCESS", outputs=res.outputs, finishedAt=time.time(),
            costTime=_cost_seconds(res.raw),
        )
        # 成功后刷一次余额算实扣（拉失败不阻塞，实扣就是 None）
        if persist_session is not None and balance_before is not None:
            try:
                bal = await refresh_balance(acc)
                after = bal.get("transitionCoins")
                if after is not None:
                    rh_store.update_task(task_id, coinsSpent=round(balance_before - float(after), 2),
                                         balanceAfter=bal)
            except WebSessionError:
                pass
        return rh_store.get_task(task_id)
    except WebSessionError as exc:
        # 账号级别的问题（没邮箱密码 / 登录冷却）——到这里说明连提交都没开始
        rh_store.update_task(task_id, status="FAILED", error=str(exc)[:400], finishedAt=time.time())
        return rh_store.get_task(task_id)
    finally:
        _running.discard(task_id)
        _cancel_flags.discard(task_id)


def _humanize_submit_error(exc: WebSessionError) -> str:
    """提交就被拒时把站内错误码翻成能看懂的话；其余原文透传。"""
    msg = str(exc)
    if str(exc.raw_code) == "414" or any(m in msg.upper() for m in POWER_INSUFFICIENT_MARKERS):
        return ("站内积分（每日赠送的 activityPoint）不够支付这次预估，16:00 UTC 清零后重置；"
                f"或换积分消耗更低的应用。上游原文：{exc}")
    return msg[:400]


def _slim_estimate(est: dict[str, Any]) -> dict[str, Any]:
    return {
        "totalPrice": est.get("totalPrice"), "currency": est.get("currency"),
        "priceText": est.get("priceText"), "estimatedPoint": est.get("estimatedPoint"),
        "estimatedTransitionCoin": est.get("estimatedTransitionCoin"),
        "nodes": [n.get("classType") for n in (est.get("nodes") or []) if isinstance(n, dict)],
    }


def _cost_seconds(raw: dict[str, Any]) -> float | None:
    v = (raw or {}).get("taskCostTime")
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "MAX_WAIT",
    "POLL_INTERVAL",
    "RunningHubError",
    "WebAccountSession",
    "WebRunResult",
    "execute_task",
    "is_cancelled",
    "poll_task",
    "reap_orphans",
    "refresh_balance",
    "request_cancel",
    "run_workflow",
    "submit_task",
]
