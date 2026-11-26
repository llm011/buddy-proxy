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


async def refresh_balance(acc: WebAccountSession) -> dict[str, Any]:
    """拉站内余额，并把 token 带回（调用方据此写回 store）。"""
    balance, _ = await acc.call(lambda t: websession.get_balance(t))
    return balance


async def run_workflow(
    acc: WebAccountSession,
    webapp_id: str,
    node_info_list: list[dict[str, Any]],
    *,
    estimate_only: bool = False,
    poll_interval: float = POLL_INTERVAL,
    max_wait: float = MAX_WAIT,
) -> WebRunResult:
    """站内运行一个 AI 应用：预估 → 提交 → 轮询 → 取结果。

    ``estimate_only=True`` 只做价格预估（免费、不扣任何东西），给 UI 的「先看看
    要花多少」用。
    """
    inputs = websession.normalize_inputs(node_info_list)
    estimate, _ = await acc.call(lambda t: websession.price_estimate(webapp_id, inputs, t))
    if estimate_only:
        return WebRunResult(task_id="", status="ESTIMATE", outputs=[], estimate=estimate, raw={})

    created, _ = await acc.call(lambda t: websession.create_task(webapp_id, inputs, t))
    task_id = created["taskId"]

    deadline = asyncio.get_running_loop().time() + max_wait
    status = created.get("status") or "RUNNING"
    last_raw: dict[str, Any] = created.get("raw") or {}
    while status in ("RUNNING", "QUEUED") and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(poll_interval)
        recs, _ = await acc.call(lambda t: websession.task_list(t, task_id=task_id))
        if recs:
            status = str(recs[0].get("taskStatus") or status).upper()
            last_raw = recs[0]

    if status in ("RUNNING", "QUEUED"):
        raise WebSessionError(f"任务超时 (taskId={task_id}, status={status})", kind="upstream")
    outputs: list[dict[str, str]] = []
    if status == "SUCCESS":
        hist, _ = await acc.call(lambda t: websession.output_history(t, webapp_id=webapp_id, task_id=task_id))
        outputs = _outputs_from_history(hist)
    else:
        raise WebSessionError(
            f"任务失败: {parse_task_failure(last_raw)} (taskId={task_id})", kind="upstream"
        )
    return WebRunResult(task_id=task_id, status=status, outputs=outputs, estimate=estimate, raw=last_raw)


__all__ = [
    "MAX_WAIT",
    "POLL_INTERVAL",
    "RunningHubError",
    "WebAccountSession",
    "WebRunResult",
    "refresh_balance",
    "run_workflow",
]
