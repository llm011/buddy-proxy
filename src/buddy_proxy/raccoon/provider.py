"""商汤小浣熊（SenseTime Raccoon）通道：多账号转发 + 积分面板。

## 协议

上游本身就是 OpenAI chat-completions 形态（``/api/web/llm/v2/chat/completions``），
所以请求/响应以**直通**为骨架（kimi / mimo 同款），只做三处必要整形：

1. 模型名对齐目录真名；
2. ``stream`` 由本次转发路径决定；
3. 流式补 ``stream_options.include_usage``（上游默认不吐 usage）。

认证只有 ``xhx_token`` 一个头，无签名、无设备指纹。

## 多账号

按导入顺序主备降级（见 ``failover.py``）：会话失效 / 积分耗尽 / 模型限流
分别走账号级、额度级、模型级三档冷却。**模型级 429 只挡那一个模型**——
上游的 ``user_model_rate_limited`` 是「账号×模型」频率窗口（恢复中位数
25 分钟），把整号摘掉会让同账号其它模型白白不可用。

## 三个必须防的坑

1. **未知模型静默回落**：写错模型名，上游回 200 但换成默认模型。这里做
   两层拦截——请求前按目录校验（``_resolve_model``），回包首帧再比对
   ``model`` 回显（``_gate_first_event``）。只做一层仍会「问 A 答 B」。
2. **并发上限约 5**：第 6 个回 429 且停摆 25 分钟。用 ``rate_limit`` 的
   每账号闸门在本地卡住，不靠上游 429 反馈。
3. **额度是积分不是 token**：按模型倍率扣分（``0 = 限免``），面板要显示
   积分与倍率，不是 token 余额。

## 自动续期（「不用一个月登录一次」）

会话 JWT 约 30 天到期。有手机号+密码的账号在 ``ensure_account_token``
发现过期时自动重新登录（见 ``login.relogin_account``），无需人工介入；
没有密码的账号才需要重新导入，面板会标出来（``auto_renew: false``）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..providers.base import BaseProvider
from ..core.account_store import quota_epoch_from
from ..core import load_balance as lb_mod
from ..core.metrics import ACCOUNT_META
from . import failover, rate_limit
from .credentials import (
    AuthError,
    ensure_account_token,
    has_cred,
    has_relogin_cred,
    list_accounts,
    load_account_cred,
)
from .login import endpoint, fetch_user_info, session_healthy
from .upstream import (
    BALANCE_PATH,
    BILLS_PATH,
    CATALOG_PATH,
    CHAT_PATH,
    SETTING_INFO_PATH,
    has_daily_grant_today,
    next_daily_reset_epoch,
    page_boot_requests,
    parse_bills,
    build_chat_body,
    build_headers,
    model_echo_matches,
    normalize_model_id,
    parse_balance,
    parse_catalog,
    parse_setting_balance,
)

log = logging.getLogger(__name__)

#: 流式：read 是「相邻两次读」的上限。小浣熊实测 411K token 输入 14.2 秒返回，
#: 首字不算慢；给 120s 足够吸收大上下文的排队。
_TIMEOUT_STREAM = httpx.Timeout(connect=15.0, read=120.0, write=60.0, pool=15.0)
#: 非流式：一次 read 拿到全部响应 = 总时长上限。
_TIMEOUT_NONSTREAM = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)
#: 兼容旧名。
_TIMEOUT = _TIMEOUT_NONSTREAM
#: failover 循环的尝试期总预算（只挡「还没开始试」的尝试）。
_ATTEMPT_DEADLINE_S = 240.0
#: 流式首事件闸门的缓冲行上限（防异常上游无界攒内存）。
_GATE_BUFFER_MAX_LINES = 256

#: 模型表放 JSON（models.json，随包分发）：实测要增删模型改文件就行。
_MODELS_JSON = Path(__file__).with_name("models.json")

#: models.json 损坏时的内置兜底（保持通道可用）。实测最常用的两个：
#: 限免的 SenseNova Flash Lite 与一折的 GLM Flash。
_FALLBACK_MODELS = [
    {"id": "sn-sensenova-6-8-flash-lite", "upstream": "sn-sensenova-6-8-flash-lite",
     "multiplier": 0.0, "free": True,
     "description": "SenseNova-6.8-Flash-Lite（限免）"},
    {"id": "sn-glm-5-3-flash", "upstream": "sn-glm-5-3-flash",
     "multiplier": 0.2, "free": False, "description": "GLM-5-3-Flash"},
]

#: 额度并发查询线程数。
_QUOTA_WORKERS = 4
#: 整轮额度查询 deadline（防单账号挂死拖住整页）。
_QUOTA_ROUND_DEADLINE_S = 12.0

_quota_pool: "concurrent.futures.ThreadPoolExecutor | None" = None
_quota_pool_lock = threading.Lock()

#: 未登录时的静态说明条（返回 None 会让面板整块隐藏，导入入口就没了）。
_QUOTA_LOGIN_NOTE = (
    "未导入账号。点下方「导入」粘贴 xhx_token（或手机号+密码，可自动续期）"
)


def _quota_executor() -> "concurrent.futures.ThreadPoolExecutor":
    global _quota_pool
    with _quota_pool_lock:
        if _quota_pool is None:
            _quota_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=_QUOTA_WORKERS, thread_name_prefix="raccoon-quota")
        return _quota_pool


def _static_notice(label: str, remaining: str) -> dict[str, Any]:
    """静态说明条（不画进度条）：未登录兜底 / 查询失败降级。"""
    return {
        "label": label,
        "used": None,
        "total": None,
        "remaining": remaining,
        "percent": None,
        "reset_ts": None,
        "expire_ts": None,
        "unit": None,
    }


def _load_models() -> list[dict[str, Any]]:
    """读 models.json；损坏/缺失退回内置兜底。"""
    try:
        data = json.loads(_MODELS_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("raccoon models.json 读取失败，用内置兜底: %s", exc)
        return [dict(m) for m in _FALLBACK_MODELS]
    items = data.get("models") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        return [dict(m) for m in _FALLBACK_MODELS]
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        entry = dict(it)
        entry.setdefault("upstream", str(it["id"]))
        entry.setdefault("multiplier", 1.0)
        entry.setdefault("free", float(entry["multiplier"]) == 0.0)
        out.append(entry)
    return out or [dict(m) for m in _FALLBACK_MODELS]


def _lb_state():
    """当前运行态（负载均衡开关读它）。未初始化时返回 None。"""
    from ..core import state as _state
    return getattr(_state, "proxy_state", None)


class RaccoonProvider(BaseProvider):
    """商汤小浣熊通道（多账号 failover + 积分面板）。"""

    id = "raccoon"
    name = "商汤小浣熊 (SenseTime Raccoon)"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # 模块级表只是冷启动种子。运行时目录归实例持有，刷新一个 provider
        # 不能污染其它实例（测试、多 app 同进程也不会串目录）。
        self._models: list[dict[str, Any]] = _load_models()
        self._model_by_id: dict[str, dict[str, Any]] = {
            str(m["id"]): m for m in self._models
        }

    # ---- 基础契约 ----------------------------------------------------

    def models(self) -> list[dict[str, Any]]:
        return [dict(m) for m in self._models]

    def ensure_auth(self) -> None:
        """没有任何账号时给可操作提示（而不是裸异常）。"""
        if not has_cred():
            raise HTTPException(
                status_code=401,
                detail={"error": {
                    "message": ("raccoon 未导入账号：请在管理页导入 xhx_token，"
                                "或填手机号+密码（可自动续期）"),
                    "type": "authentication_error",
                }},
            )

    def health(self) -> dict[str, Any]:
        accounts = list_accounts()
        return {
            "id": self.id,
            "name": self.name,
            "configured": bool(accounts),
            "models": [str(m["id"]) for m in self._models],
            "catalog_error": getattr(self, "_catalog_error", ""),
            "accounts": [
                {
                    "id": a.id,
                    "name": a.alias or a.name or a.id,
                    "phone": str((load_account_cred(a.id) or {}).get("phone") or ""),
                }
                for a in accounts
            ],
        }

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client

    # ---- 模型解析 ----------------------------------------------------

    def _resolve_model(self, model: str) -> dict[str, Any]:
        """客户端模型名 → 目录条目；未知模型直接 400。

        **绝不能静默回落到默认模型**：上游对未知模型名回 200 但换成默认模型，
        客户端以为拿到了 A 的答案、实际是 B 答的。这里在发请求前就拦掉。
        """
        name = str(model or "").removeprefix(f"{self.id}/").strip()
        entry = self._model_by_id.get(name)
        if entry is None:
            # 容忍不带 sn- 前缀的写法（客户端手打时常见）
            entry = self._model_by_id.get(normalize_model_id(name))
        if entry is None:
            known = "、".join(sorted(self._model_by_id)[:12])
            raise HTTPException(
                status_code=400,
                detail={"error": {
                    "message": (f"raccoon 未知模型: {name}。已知模型：{known}"
                                "（上游对未知模型会静默回落默认模型，故在此拦截）"),
                    "type": "invalid_request_error",
                }},
            )
        return entry

    # ---- 转发主链路 --------------------------------------------------

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        self.ensure_auth()
        stream = bool(body.get("stream", False))
        entry = self._resolve_model(str(body.get("model") or ""))
        upstream_model = str(entry.get("upstream") or entry["id"])

        accounts = failover.available_accounts(upstream_model)
        # 负载均衡（额度页开关）：只改「先试谁」，不改「谁能试」。
        accounts = lb_mod.reorder(_lb_state(), self.id, accounts,
                                  model=upstream_model)
        if not accounts:
            report = failover.cooldown_report()
            model_report = failover.model_cooldown_report(upstream_model)
            states = "；".join(p for p in (report, model_report) if p)
            raise HTTPException(
                status_code=429,
                detail={"error": {
                    "message": (f"raccoon 所有账号均不可用"
                                + (f"（{states}）" if states else "")
                                + "；额度/限流冷却到点自动恢复"),
                    "type": "rate_limit_error",
                }},
            )

        meta = ACCOUNT_META.get()
        client = await self._get_client()
        started = time.monotonic()
        tried = 0
        last_status = 0
        last_detail = ""

        for acct in accounts:
            if tried and time.monotonic() - started > _ATTEMPT_DEADLINE_S:
                last_status = last_status or 504
                last_detail = last_detail or "尝试预算用尽（上游持续无响应）"
                break
            tried += 1

            # token 刷新/重新登录是同步 HTTP；丢线程池避免卡事件循环。
            try:
                access_token, cred = await asyncio.to_thread(
                    ensure_account_token, acct.id)
            except AuthError as exc:
                # 有密码却仍失败 = 登录被拒（密码错/要验证码），需要人工介入；
                # 没密码 = 会话过期，等用户重新导入。两者都长冷却。
                failover.mark_relogin_needed(acct.id, reason=str(exc))
                last_status, last_detail = 401, str(exc)
                continue
            if meta is not None:
                meta["account"] = acct.id

            # 并发闸门：上游上限约 5，超了会停摆 25 分钟，必须本地卡住。
            lease = await rate_limit.limiter().acquire(acct.id)
            if lease is None:
                last_status, last_detail = 429, f"账号 {acct.id} 并发排队超时"
                continue

            upstream_body = build_chat_body(body, model=upstream_model, stream=stream)
            headers = build_headers(access_token)
            url = f"{endpoint()}{CHAT_PATH}"
            try:
                req = client.build_request(
                    "POST", url, json=upstream_body, headers=headers,
                    timeout=_TIMEOUT_STREAM if stream else _TIMEOUT_NONSTREAM)
                resp = await client.send(req, stream=stream)
            except httpx.TimeoutException as exc:
                lease.release()
                last_status, last_detail = 504, f"upstream timeout: {exc}"
                failover.mark_cooldown(acct.id, reason="请求超时")
                continue
            except httpx.HTTPError as exc:
                lease.release()
                last_status, last_detail = 502, f"upstream error: {exc}"
                failover.mark_cooldown(acct.id, reason="连接失败")
                continue
            except BaseException:
                lease.release()
                raise

            # 非 2xx：按错误语义分档
            if resp.status_code >= 400:
                lease.release()
                detail = await _error_detail(resp)
                last_status = resp.status_code
                last_detail = detail
                code = _extract_code(detail)
                if resp.status_code == 401 or _is_session_dead(resp.status_code, detail):
                    # 会话被判死：先试自动重登（有密码时），否则长冷却等用户
                    cred_now = load_account_cred(acct.id) or {}
                    if has_relogin_cred(cred_now):
                        try:
                            await asyncio.to_thread(
                                ensure_account_token, acct.id, force_refresh=True)
                            last_detail = f"{detail}（已尝试自动重新登录）"
                        except AuthError as exc:
                            failover.mark_relogin_needed(acct.id, reason=str(exc))
                            last_detail = f"{detail}；自动重登失败: {exc}"
                    else:
                        failover.mark_relogin_needed(acct.id, reason="会话失效")
                    await _drain(resp)
                    continue
                if failover.is_credit_exhausted(resp.status_code, code, detail):
                    failover.mark_cooldown(acct.id, quota=True,
                                           reason=f"积分耗尽: {detail}")
                elif failover.is_model_rate_limited(resp.status_code, code, detail):
                    # 只挡这一个模型：同账号其它模型仍可用
                    failover.mark_model_cooldown(
                        acct.id, upstream_model,
                        retry_after=resp.headers.get("retry-after"),
                        reason=f"模型限流: {detail}")
                else:
                    failover.mark_cooldown(
                        acct.id, retry_after=resp.headers.get("retry-after"),
                        reason=f"HTTP {resp.status_code}: {detail}")
                await _drain(resp)
                continue

            # 200：过首事件闸门（校验 model 回显 + 识别带内错误）
            try:
                gate = await self._gate_first_event(resp, stream, upstream_model)
            except BaseException:
                lease.release()
                await _drain(resp)
                raise

            if gate.error_code:
                lease.release()
                last_status, last_detail = gate.error_code, gate.error_message
                code = _extract_code(gate.error_message)
                if failover.is_credit_exhausted(gate.error_code, code,
                                                gate.error_message):
                    failover.mark_cooldown(acct.id, quota=True,
                                           reason=f"积分耗尽: {gate.error_message}")
                elif failover.is_model_rate_limited(gate.error_code, code,
                                                    gate.error_message):
                    failover.mark_model_cooldown(
                        acct.id, upstream_model,
                        reason=f"模型限流: {gate.error_message}")
                else:
                    failover.mark_cooldown(
                        acct.id, reason=f"HTTP {gate.error_code}: {gate.error_message}")
                await _drain(gate.resp)
                continue
            if gate.model_mismatch:
                # 上游静默回落了别的模型：这是硬错误，不能把别人的答案当自己的
                lease.release()
                last_status = 502
                last_detail = (f"上游回落模型（请求 {upstream_model}，"
                               f"回显 {gate.echoed_model}）")
                failover.mark_model_cooldown(acct.id, upstream_model,
                                             reason="模型回显不一致")
                await _drain(gate.resp)
                continue
            if gate.eof:
                lease.release()
                last_status = 502
                last_detail = "上游空流（首事件前断流）"
                await _drain(gate.resp)
                continue

            frame = ACCOUNT_META.get()
            if isinstance(frame, dict):
                frame["account"] = acct.id

            if stream:
                replay = _ReplayStream(gate.resp, gate.buffered, gate.lines,
                                       lease=lease)
                if protocol == "anthropic":
                    # anthropic 出口才需要转换（上游是 OpenAI 形态）
                    from ..gemini.provider import _to_anthropic_stream

                    return StreamingResponse(
                        _to_anthropic_stream(replay, upstream_model),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "close"},
                    )
                # openai 出口**直通**：上游本身就是 OpenAI SSE。
                #
                # 以前这里错误地走了 gemini 的 ``_to_openai_stream``——它按
                # Gemini 事件形状解析（``candidates``/``parts``），而 raccoon
                # 上游发的是标准 ``choices``/``usage``，于是整条流被当成
                # 「没有语义事件」丢掉，客户端拿不到内容、metrics 里
                # ``prompt_tokens``/``completion_tokens`` 恒为 0
                # （用户实报「日志里都看不到 token 消耗信息」）。
                return StreamingResponse(
                    _passthrough_sse(replay),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "close"},
                )

            try:
                payload = gate.payload
                if payload is None:
                    await gate.resp.aclose()
                    raise HTTPException(
                        status_code=502,
                        detail={"error": {
                            "message": "raccoon upstream returned non-JSON",
                            "type": "bad_gateway"}},
                    )
                await gate.resp.aclose()
                if protocol == "anthropic":
                    from ..protocols.anthropic_adapter import (
                        chat_completion_to_anthropic_message,
                    )

                    payload = chat_completion_to_anthropic_message(payload, original)
                return JSONResponse(content=payload)
            finally:
                lease.release()

        raise HTTPException(
            status_code=last_status if last_status in (401, 402, 429, 504) else 502,
            detail={"error": {
                "message": (f"raccoon 所有账号均不可用"
                            f"（最后错误 HTTP {last_status or 'n/a'}"
                            f"{(': ' + last_detail) if last_detail else ''}）"),
                "type": ("rate_limit_error" if last_status in (402, 429)
                         else "timeout" if last_status == 504
                         else "bad_gateway"),
            }},
        )

    # ---- 首事件闸门 --------------------------------------------------

    async def _gate_first_event(self, resp: httpx.Response, stream: bool,
                                upstream_model: str) -> "_Gate":
        """压住第一个上游事件再决定透传还是换号。

        三件事：

        1. **识别带内错误**：上游偶发把错误塞进 200 的流里（``data: {"error":...}``）。
        2. **校验 model 回显**：未知模型会静默回落默认模型，必须比对首帧。
        3. **识别空流**：首事件前断流 = 假成功，换号。

        校验通过后缓冲行随透传补放，客户端无损。
        """
        if not stream:
            try:
                payload = resp.json()
            except ValueError:
                return _Gate(resp=resp, committed=True, payload=None)
            if isinstance(payload, dict):
                err = payload.get("error")
                if isinstance(err, dict):
                    return _Gate(resp=resp, error_code=int(err.get("code") or 502),
                                 error_message=str(err.get("message") or ""))
                if not model_echo_matches(payload, upstream_model):
                    return _Gate(resp=resp, model_mismatch=True,
                                 echoed_model=str(payload.get("model") or ""))
            return _Gate(resp=resp, committed=True,
                         payload=payload if isinstance(payload, dict) else None)

        buffered: list[str] = []
        lines = resp.aiter_lines()
        saw_any = False
        try:
            async for line in lines:
                buffered.append(line)
                if len(buffered) > _GATE_BUFFER_MAX_LINES:
                    # 上游一直发非语义事件：按已到达透传放行，别无界攒内存
                    return _Gate(resp=resp, committed=True, buffered=buffered,
                                 lines=lines)
                stripped = line.strip()
                if not stripped.startswith("data:"):
                    continue
                data = stripped[5:].strip()
                if not data or data == "[DONE]":
                    continue
                saw_any = True
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                err = chunk.get("error")
                if isinstance(err, dict):
                    return _Gate(resp=resp, error_code=int(err.get("code") or 502),
                                 error_message=str(err.get("message") or ""),
                                 lines=lines)
                if not model_echo_matches(chunk, upstream_model):
                    return _Gate(resp=resp, model_mismatch=True,
                                 echoed_model=str(chunk.get("model") or ""),
                                 lines=lines)
                # 见到任意带 choices 的帧即认为已提交（内容随透传补放）
                if chunk.get("choices") is not None:
                    return _Gate(resp=resp, committed=True, buffered=buffered,
                                 lines=lines)
                # 只有 usage / 心跳：继续等下一条
        except httpx.TimeoutException:
            return _Gate(resp=resp, eof=True, timed_out=True, lines=lines)
        except httpx.HTTPError as exc:
            log.warning("raccoon 上游流异常: %s: %s", type(exc).__name__, exc)
            return _Gate(resp=resp, eof=True, lines=lines)
        return _Gate(resp=resp, eof=True, saw_any=saw_any, lines=lines)

    # ---- 模型目录刷新 --------------------------------------------------

    async def refresh_models(self, force: bool = False) -> int:
        """从上游拉模型目录并覆盖内存表；返回条目数。

        目录里带 ``billing_effective_multiplier``，是「哪些模型限免」的唯一
        权威来源——所以刷新不只是补模型名，也是重新对齐积分倍率。

        失败时保留现有表并记 ``catalog_error``（面板可显示），不抛给调用方：
        刷新目录是管理操作，不该因为一次网络抖动把整页打成 500。
        """
        self._catalog_error = ""
        accounts = list_accounts()
        if not accounts:
            self._catalog_error = "未导入账号"
            return len(self._models)
        last_error = ""
        for acct in accounts:
            try:
                token, _cred = await asyncio.to_thread(ensure_account_token, acct.id)
            except AuthError as exc:
                last_error = str(exc)
                continue
            payload = await asyncio.to_thread(self._fetch_json, CATALOG_PATH, token)
            models = parse_catalog(payload) if payload else []
            if not models:
                last_error = "上游目录为空"
                continue
            self._models = models
            self._model_by_id = {str(m["id"]): m for m in models}
            log.info("raccoon 模型目录已刷新：%d 个模型（账号 %s）",
                     len(models), acct.id)
            return len(models)
        self._catalog_error = last_error or "刷新失败"
        log.warning("raccoon 模型目录刷新失败：%s", self._catalog_error)
        return len(self._models)

    # ---- 额度（积分）--------------------------------------------------

    def quota(self) -> dict[str, Any] | None:
        """积分余额（可画真进度条）。

        小浣熊的额度是**积分**不是 token：每笔请求按模型倍率扣分，每日池
        300 分（7 天有效，上限 2100）。面板按「已用 / 上限」展示每日池，
        因为它是唯一会作废的余额。

        多账号并发查；个别账号失败插 ``query_failed`` 说明条，不拖垮整页。
        **未导入也要返回说明条**——返回 None 会让面板整块隐藏，导入入口就没了。
        """
        accounts = list_accounts()
        if not accounts:
            return {
                "items": [_static_notice("小浣熊", _QUOTA_LOGIN_NOTE)],
                "level": None,
            }
        multi = len(accounts) > 1

        if multi:
            pool = _quota_executor()
            futures = [pool.submit(self._quota_one, a, i + 1, multi=True)
                       for i, a in enumerate(accounts)]
            deadline = time.monotonic() + _QUOTA_ROUND_DEADLINE_S
            items: list[dict[str, Any]] = []
            failures = 0
            for fut in futures:
                try:
                    its, ok = fut.result(
                        timeout=max(deadline - time.monotonic(), 0.05))
                except Exception as exc:  # noqa: BLE001 - 超时/异常都算失败
                    log.warning("raccoon quota 账号采集失败: %s: %s",
                                type(exc).__name__, exc)
                    its, ok = [], False
                if ok:
                    items.extend(its)
                else:
                    failures += 1
            if failures:
                items.insert(0, {
                    "label": f"小浣熊 · {failures}/{len(accounts)} 个账号查询失败",
                    "used": None, "total": None,
                    "remaining": "查询失败（可能是网络或会话过期）",
                    "percent": None, "reset_ts": None, "expire_ts": None,
                    "query_failed": True,
                })
            return {"items": items, "level": None}

        its, ok = self._quota_one(accounts[0], 1, multi=False)
        if not ok:
            return {"items": [_static_notice(
                "小浣熊", "额度查询失败（可能是网络或会话过期，稍后自动重试）")],
                "level": None}
        return {"items": its, "level": None}

    def _quota_one(self, acct: Any, index: int, *, multi: bool
                   ) -> tuple[list[dict[str, Any]], bool]:
        """单账号积分查询：成功返回 (items, True)，失败 ([], False)。"""
        prefix = f"小浣熊 #{index} · " if multi else ""
        try:
            token, _cred = ensure_account_token(acct.id)
        except AuthError as exc:
            log.warning("raccoon quota 账号 %s 取 token 失败: %s", acct.id, exc)
            return [], False
        data = self._fetch_json(BALANCE_PATH, token)
        parsed = parse_balance(data) if data else {}
        if not parsed:
            # 余额接口 403 时从 setting_info 兜底（读不到每日池，只给总积分）
            data = self._fetch_json(SETTING_INFO_PATH, token)
            parsed = parse_setting_balance(data) if data else {}
        if not parsed:
            return [], False
        # 资源包明细：只有 bills 接口能看出「有几个包、各多少、何时到期」。
        # balance 只给汇总剩余量，单靠它只能画一个总数，用户会问「我的包呢」。
        bills = parse_bills(self._fetch_json(BILLS_PATH, token, params={
            "paging.limit": 50, "type": "income"}))
        return self._items_from_balance(parsed, prefix, bills), True

    def _items_from_balance(self, parsed: dict[str, Any], prefix: str,
                            bills: list[dict[str, Any]] | None = None,
                            ) -> list[dict[str, Any]]:
        """积分数据 → 面板条目。

        ## 「已用」怎么来的（不是编的）

        上游 ``/points/v1/balance`` **只给各池剩余量**，没有 used/total 字段。
        但「已用」是可以**推出来的**：

            已用 = Σ(未过期发放流水) − 当前可用积分

        两个数都是上游真实给的：流水来自 ``/points/v2/bills?type=income``
        （每笔发放的金额与到期时间），可用积分来自 balance。差额就是消耗。

        实测佐证（用户账号）：两笔发放 300 + 3000 = 3300，balance 也是 3300
        → 已用 = 0，与该用户「我都没用过一点积分」的自述一致。

        ## 为什么不像上次那样编上限

        上次我按「每天 300 × 7 天」推了个 2100 当上限，于是「剩 300」被算成
        「已用 1800」——那是**规则推算**，没有数据支撑。这次的分子分母都来自
        上游实际记录，且差额能被用户自己的账单验证。

        拿不到流水时**不画进度条**（只显示剩余量）：没有分母就不该有百分比。
        """
        items: list[dict[str, Any]] = []
        credits = parsed.get("credits")
        available = float(credits) if isinstance(credits, (int, float)) else None

        # 未过期发放合计 = 进度条的分母（上游真实记录之和）
        granted_total: float | None = None
        if bills:
            granted_total = 0.0
            now = time.time()
            for bill in bills:
                points = bill.get("points") or 0
                expire = bill.get("expire_at") or ""
                if expire and _epoch_from_iso(expire) and _epoch_from_iso(expire) < now:
                    continue  # 已过期的包不计入分母
                granted_total += float(points)

        # 总额度条目：带进度条（已用 = 发放合计 − 剩余）
        if available is not None and granted_total and granted_total > 0:
            used = max(0.0, granted_total - available)
            items.append({
                "label": f"{prefix}积分总额",
                "used": round(used, 1),
                "total": round(granted_total, 1),
                "remaining": round(available, 1),
                "percent": round(used / granted_total * 100, 2),
                "reset_ts": None,
                "expire_ts": None,
                "unit": "credits",
                # 刻意**不设** head_only：head_only 的条目会被 quotaItemsHtml
                # 跳过、只由 quotaHeadSum 渲染成标题行的「剩 X / Y」文字，
                # **不带进度条**。用户明确要进度条，所以让它作为普通明细行，
                # 由现成的 quotaItemHtml 画出 qbar。
                "note": ("已用 = 未过期发放合计 − 当前可用积分（两者都来自上游流水）；"
                         "每笔请求按模型倍率扣分，倍率 0 = 限免"),
            })

        # 资源包明细：每笔发放一个包，独立到期（不给进度条——单包没有可信上限）
        #
        # ``expire_ts`` 必须是 **epoch 秒**：前端 ``fmtExpireMmdd`` 按
        # ``new Date(ts * 1000)`` 算，传 ISO 字符串会渲染成
        # 「NaN-NaN-NaN 到期」（2026-11-23 用户实报）。
        if bills:
            for bill in bills:
                points = bill.get("points") or 0
                expire_iso = bill.get("expire_at") or ""
                expire_epoch = _epoch_from_iso(expire_iso)
                items.append({
                    "label": f"{prefix}{bill.get('event') or '积分发放'}",
                    "used": None,
                    "total": None,
                    "remaining": round(float(points), 1),
                    "percent": None,
                    "reset_ts": None,
                    "expire_ts": expire_epoch or None,
                    "unit": "credits",
                    # note 只留一句人话；具体日期由组件按 expire_ts 渲染，
                    # 别在 note 里重复一遍 ISO（用户实报「又长又不好看」）
                    "note": ("无到期时间" if not expire_epoch
                             else "到期后这部分积分作废"),
                })

        # 分池剩余量（上游真实字段，不做减法）
        for key, label in (("daily_points", "每日积分剩余"),
                           ("reward_points", "奖励积分剩余"),
                           ("topup_points", "充值积分剩余"),
                           ("monthly_points", "月度积分剩余")):
            val = parsed.get(key)
            if isinstance(val, (int, float)) and float(val) > 0:
                items.append({
                    "label": f"{prefix}{label}",
                    "used": None, "total": None,
                    "remaining": round(float(val), 1),
                    "percent": None, "reset_ts": None, "expire_ts": None,
                    "unit": "credits",
                })

        # 兜底：既没流水也没分池时，至少把可用积分显示出来
        if not items and available is not None:
            items.append({
                "label": f"{prefix}可用积分",
                "used": None, "total": None,
                "remaining": round(available, 1),
                "percent": None, "reset_ts": None, "expire_ts": None,
                "unit": "credits",
                "note": "上游未返回发放流水，无法推算已用量",
            })
        if not items:
            items.append(_static_notice(f"{prefix}小浣熊", "上游未返回可用积分"))
        return items

    def _fetch_json(self, path: str, access_token: str,
                    params: dict[str, Any] | None = None) -> dict[str, Any]:
        """同步 GET 上游 JSON（额度查询走线程池，不占事件循环）。

        ``params`` 给查询串（如流水接口的 ``paging.limit`` / ``type``）。
        """
        headers = build_headers(access_token)
        headers["Accept"] = "application/json"
        try:
            with httpx.Client(timeout=_TIMEOUT) as c:
                resp = c.get(f"{endpoint()}{path}", headers=headers, params=params)
        except httpx.HTTPError as exc:
            log.warning("raccoon %s 请求失败: %s", path, exc)
            return {}
        if resp.status_code >= 400:
            log.debug("raccoon %s 返回 HTTP %s", path, resp.status_code)
            return {}
        try:
            payload = resp.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    # ---- 签到（每日 300 分）-------------------------------------------
    #
    # 小浣熊**没有传统签到接口**：每日 300 分是上游在「当天第一次页面启动」时
    # 补发的（2026-11-19 实测，只登录不打页面不给）。所以这里的签到动作 =
    # 把浏览器打开 /work/home 的那串请求走一遍，再**以流水为准**核对到账。
    #
    # 判定绝不看 HTTP 200：上游对重复操作也回 200 但余额不动（假成功）。
    # 只有 bills 里出现今天的 daily_grant 才算签上。

    supports_checkin = True

    def _bills(self, access_token: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """拉发放流水（只取 income，消费流水会把发放挤出窗口）。"""
        payload = self._fetch_json(BILLS_PATH, access_token,
                                   params={"paging.limit": limit, "type": "income"})
        return parse_bills(payload)

    def _first_account_token(self) -> tuple[str, str] | None:
        """取第一个可用账号的 (id, token)；没有账号返回 None。"""
        accounts = list_accounts()
        for acct in accounts:
            try:
                token, _cred = ensure_account_token(acct.id)
                return acct.id, token
            except AuthError as exc:
                log.debug("raccoon 签到跳过账号 %s: %s", acct.id, exc)
                continue
        return None

    def checkin_status(self) -> dict[str, Any] | None:
        """今日签到状态：以**发放流水**为准，不看接口返回码。

        上游每日 300 分挂在「当天第一次页面启动」上，所以「今天有没有
        ``daily_grant``」就是唯一可信的签到判据。
        """
        picked = self._first_account_token()
        if picked is None:
            return {
                "checked_in": False,
                "claimable": False,
                "message": "未导入账号",
            }
        _aid, token = picked
        bills = self._bills(token)
        done = has_daily_grant_today(bills)
        credits = _today_daily_grant_points(bills) if done else None
        return {
            "checked_in": done,
            "claimable": not done,
            "credits": credits,
            # 每天 00:00 +08:00 发新的一笔；上游没给这个字段，是规则推断
            "next_ts": next_daily_reset_epoch(),
            "next_ts_source": "inferred",
            "message": (f"今日 +{credits:g} 分已到账" if done and credits
                        else "今日每日积分已到账" if done
                        else "今日每日积分尚未到账（点「签到」触发页面启动）"),
        }

    def checkin_claim(self) -> dict[str, Any] | None:
        """触发今日每日积分发放：走一遍「页面启动」序列，然后核对流水。

        这是本通道唯一的「领取」动作——上游没有 claim 接口，补发挂在页面启动
        事件上。执行后**重新读流水**判定是否真到账，返回的状态与
        :meth:`checkin_status` 同构。

        全部请求尽力而为：单步失败不影响其它步（是否到账以流水为准）。
        """
        picked = self._first_account_token()
        if picked is None:
            return {"checked_in": False, "claimable": False,
                    "message": "未导入账号，无法签到"}
        aid, token = picked
        cred = load_account_cred(aid) or {}
        device_id = str(cred.get("device_id") or "") or _derive_device_id(aid)
        self._run_page_boot(token, device_id)
        # 给上游几秒入账时间：实测到账在页面加载后数秒内
        time.sleep(2.0)
        bills = self._bills(token)
        done = has_daily_grant_today(bills)
        # 今日实际到账金额：日历侧边栏要显示「签到加了多少」，取流水里
        # 今天那笔 daily_grant 的 points（不是硬编码 300——上游调整过面额）。
        credits = _today_daily_grant_points(bills) if done else None
        if done:
            log.info("raccoon 账号 %s 今日每日积分已到账（%s 分）", aid, credits)
        else:
            log.warning("raccoon 账号 %s 页面启动后仍未见到今日 daily_grant", aid)
        return {
            "checked_in": done,
            "claimable": not done,
            "credits": credits,
            "next_ts": next_daily_reset_epoch(),
            "next_ts_source": "inferred",
            "message": (f"签到成功，今日 +{credits:g} 分已到账" if done and credits
                        else "签到成功，今日积分已到账" if done
                        else "已触发页面启动，但流水里仍没有今天的发放"
                             "（可能今天已领过或上游延迟，稍后再查）"),
        }

    def _run_page_boot(self, access_token: str, device_id: str) -> None:
        """执行「页面启动」序列（尽力而为，逐步吞错）。"""
        base = endpoint()
        for method, url, headers, body in page_boot_requests(
                access_token, device_id, base):
            try:
                with httpx.Client(timeout=_TIMEOUT) as c:
                    if method == "GET":
                        c.get(url, headers=headers)
                    else:
                        c.post(url, headers=headers, json=body or {})
            except httpx.HTTPError as exc:
                log.debug("raccoon page_boot 步骤失败（忽略）%s %s: %s",
                          method, url.rsplit("/", 1)[-1], exc)

    def quota_epoch(self) -> str:
        """quota 缓存代：账号列表一变旧快照就该作废。"""
        return quota_epoch_from(list_accounts)


# ---------------------------------------------------------------------------
# 闸门类型 / 流适配 / 工具函数
# ---------------------------------------------------------------------------

class _Gate:
    """首事件闸门的判定结果。

    ``lines`` 是闸门持有的**在途行迭代器**：httpx 响应流一次性消费，闸门读过
    之后再调 ``resp.aiter_lines()`` 会抛 StreamConsumed。后续「透传剩余流」
    （_ReplayStream）与「换号前排空」都必须续跑这同一个迭代器。
    """

    __slots__ = ("resp", "committed", "buffered", "lines", "payload",
                 "eof", "timed_out", "saw_any", "error_code", "error_message",
                 "model_mismatch", "echoed_model")

    def __init__(self, *, resp: httpx.Response, committed: bool = False,
                 buffered: list[str] | None = None,
                 lines: AsyncIterator[str] | None = None,
                 payload: dict[str, Any] | None = None,
                 eof: bool = False, timed_out: bool = False,
                 saw_any: bool = False, error_code: int = 0,
                 error_message: str = "", model_mismatch: bool = False,
                 echoed_model: str = "") -> None:
        self.resp = resp
        self.committed = committed
        self.buffered = buffered or []
        self.lines = lines
        self.payload = payload
        self.eof = eof
        self.timed_out = timed_out
        self.saw_any = saw_any
        self.error_code = error_code
        self.error_message = error_message
        self.model_mismatch = model_mismatch
        self.echoed_model = echoed_model


def _today_daily_grant_points(bills: list[dict[str, Any]]) -> float | None:
    """今天那笔 ``daily_grant`` 的积分金额；没有返回 None。

    签到日历侧边栏要显示「今天签到加了多少」，数据源就是这笔流水的
    ``points``。**不硬编码 300**：上游调整过面额（CodeArts 的活动就从
    1000 降到过 100），写死会与实际到账不符。
    """
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).date()
    for bill in bills or []:
        if str(bill.get("biz_type") or "") != "daily_grant":
            continue
        try:
            dt = datetime.fromisoformat(str(bill.get("grant_at") or ""))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        if dt.astimezone(tz).date() == today:
            pts = bill.get("points")
            return float(pts) if isinstance(pts, (int, float)) else None
    return None


def _derive_device_id(account_id: str) -> str:
    """从账号 id 派生稳定 device_id（≤64 字符，上游对超长会回 500）。

    上游对设备有长度限制（实测超 64 字符回 ``500 value too long for character
    varying(64)``），所以哈希后截断，不用原始 id。
    """
    import hashlib

    return hashlib.sha256(f"raccoon-device:{account_id}".encode()).hexdigest()[:32]


def _epoch_from_iso(value: str) -> float:
    """ISO 时间 → epoch 秒；解析失败返回 0（当作「没有到期时间」）。

    上游的 ``expire_at`` 形如 ``2026-11-29T23:59:59+08:00``（带时区）。
    解析不了就不当过期——宁可多算进分母，也别把一个有效包当过期剔掉
    （那会让「已用」凭空变大）。
    """
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


class _ReplayStream:
    """闸门缓冲行 → 真实流的适配器（先补放缓冲，再接原流）。

    gemini 转换器只用 ``aiter_lines()``/``aclose()``，实现这两个就够了。
    ``lines`` 为闸门的在途迭代器——续跑它而不是重开 ``resp.aiter_lines()``。
    """

    def __init__(self, resp: httpx.Response, buffered: list[str],
                 lines: AsyncIterator[str] | None = None,
                 lease: rate_limit.Lease | None = None) -> None:
        self._resp = resp
        self._buffered = list(buffered)
        self._lines = lines
        self._lease = lease
        self._usage: dict[str, Any] | None = None

    def _note_usage(self, line: str) -> None:
        """从上游 SSE 行里挑 token 数（供指标落库）。

        为什么必须有这一步：流式请求的 usage 只在**最后几帧**里，而
        ``_instrument`` 读的是 ``ACCOUNT_META`` 里那份 meta。以前这个适配器
        只做透传、不解析 usage，于是流式请求在 metrics 里恒为
        ``prompt_tokens=0 / completion_tokens=0``——用户实报「日志里都看不到
        token 消耗信息，只有耗时信息」正是这个原因。

        上游在 ``stream_options.include_usage`` 时会于收尾前发一帧
        ``choices: []`` + ``usage``（OpenAI 兼容口径）；取最后见到的为准。
        """
        if not line.startswith("data:"):
            return
        data = line[5:].strip()
        if not data or data == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except (ValueError, TypeError):
            return
        if not isinstance(obj, dict):
            return
        usage = obj.get("usage")
        if isinstance(usage, dict):
            self._usage = usage

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._buffered:
            self._note_usage(line)
            yield line
        if self._lines is not None:
            async for line in self._lines:
                self._note_usage(line)
                yield line

    async def aclose(self) -> None:
        # 把本流解析到的 usage 写进请求级 meta：``_instrument`` 在流结束时
        # 读 ``ACCOUNT_META`` 那份 dict 落库（与 antigravity 的 _ReplayStream
        # 同一条路径）。没有 usage 时不写，让指标保持 0 而不是编数。
        if self._usage:
            meta = ACCOUNT_META.get()
            if isinstance(meta, dict):
                meta["usage"] = self._usage
        try:
            await self._resp.aclose()
        except Exception:  # noqa: BLE001 - 收尾失败不影响已完成的流
            pass
        finally:
            if self._lease is not None:
                self._lease.release()


async def _passthrough_sse(stream: "_ReplayStream") -> AsyncIterator[str]:
    """把上游 OpenAI SSE 原样转给客户端（不解析、不改写）。

    只做两件事：逐行产出、结束时关流（``aclose`` 会释放并发 lease）。
    行分隔符保持 ``\n\n``——SSE 规范允许 ``\n`` 或 ``\r\n``，客户端都认，
    但保持与上游一致最省事。
    """
    try:
        async for line in stream.aiter_lines():
            yield line + "\n"
    finally:
        await stream.aclose()


async def _drain(resp: httpx.Response, lines: AsyncIterator[str] | None = None) -> None:
    """排空并关闭上游响应（释放连接回池，避免半读连接卡住 pool）。"""
    try:
        if lines is not None:
            async for _ in lines:
                pass
        elif not resp.is_closed:
            await resp.aread()
    except Exception:  # noqa: BLE001 - 排空失败也要关
        pass
    finally:
        try:
            await resp.aclose()
        except Exception:  # noqa: BLE001
            pass


async def _error_detail(resp: httpx.Response) -> str:
    """读上游错误体（截断），读不到退回状态码。"""
    try:
        raw = await resp.aread()
        text = raw.decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001
        return f"HTTP {resp.status_code}"
    if not text:
        return f"HTTP {resp.status_code}"
    return text[:300]


def _extract_code(detail: str) -> str:
    """从错误体里抠业务码（字符串形式，便于集合比较）。"""
    try:
        payload = json.loads(detail)
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("code", "error_code", "error"):
        val = payload.get(key)
        if isinstance(val, (str, int)) and not isinstance(val, bool):
            return str(val)
    return ""


def _is_session_dead(status: int, detail: str) -> bool:
    """会话是否被判死（需要重新登录，而不是简单重试）。

    上游用几个业务码表达这件事：``device_bind_required``（200811）/
    ``needs_relogin`` / ``trusted=false``。401 已单独处理，这里认 403 + 业务码。
    """
    if status not in (401, 403):
        return False
    text = (detail or "").lower()
    return ("device_bind" in text or "needs_relogin" in text
            or "relogin" in text or "session" in text and "expired" in text)
