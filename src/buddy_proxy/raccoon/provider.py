"""商汤小浣熊（SenseTime Raccoon）通道：多账号转发 + 积分面板。

## 协议

上游是 OpenAI chat-completions 形态，聊天走 **v2** 端点
（``/api/web/llm/v2/chat/completions``）。不能回切 v1：2026-11-27 实测 v1
虽返回 200 与正常 usage，但余额不动、bills 不生成消费流水；同账号同请求换
v2 后立即扣分并出账。v2 曾经的 200108 风控由设备绑定与客户端头解决。

当前发给上游的请求仍**一律非流式**（``build_chat_body(..., stream=False)``），
客户端要流式时拿完整 JSON 在本地合成 SSE（``_synthesize_openai_stream`` /
``_synthesize_anthropic_stream``）。这是保留稳定出口的实现取舍，代价是首字
延迟等于整条上游响应时间；修计费不同时切换原生 SSE。

认证使用 ``xhx_token`` / Authorization / Cookie，并带绑定设备的客户端头。

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
from ..core import net as core_net
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
    save_account_cred,
)
from .login import endpoint, fetch_user_info, session_healthy
from .upstream import (
    BALANCE_PATH,
    BILLS_PATH,
    BIND_DEVICE_PATH,
    CATALOG_PATH,
    CHAT_PATH,
    SETTING_INFO_PATH,
    USER_INFO_PATH,
    bind_device_body,
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

#: 当前上游统一走非流式，所以只有这一个超时。read=300s 是「整条响应」的
#: 上限——客户端要流式时首字延迟也受它约束，因为内容得等上游整条返回才能
#: 开始本地合成。
_TIMEOUT_NONSTREAM = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)
#: 兼容旧名。
_TIMEOUT = _TIMEOUT_NONSTREAM
#: failover 循环的尝试期总预算（只挡「还没开始试」的尝试）。
_ATTEMPT_DEADLINE_S = 240.0

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
            self._client = core_net.async_client(timeout=_TIMEOUT)
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
        # 滑动窗口到顶被跳过的账号：若最终一个都没真发出去，要报「频率上限」
        # 而不是「所有账号均不可用（最后错误 n/a）」——后者会让人误以为上游挂了。
        rate_limited: list[str] = []
        rate_win = rate_limit.window()

        for acct in accounts:
            # 滑动窗口：到顶就不打上游、直接换号。放在 tried += 1 之前——
            # 没消耗任何上游时间，不该占尝试预算。
            if not rate_win.has_capacity(acct.id):
                rate_limited.append(acct.id)
                continue
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

            # 请求前余额快照（结算用；读不到就是 None，不影响主链路）。
            # 放线程池里：这是同步 HTTP，不能卡事件循环。
            balance_before = await asyncio.to_thread(
                self._balance_now, access_token)

            # 确保设备已绑定（首次会打一次上游，之后走内存标记）。
            # 未绑定设备的会话调模型 = 200108 风控的触发形态，见 _ensure_device_bound。
            await asyncio.to_thread(_ensure_device_bound, acct.id, access_token)

            # 并发闸门：上游上限约 5，超了会停摆 25 分钟，必须本地卡住。
            lease = await rate_limit.limiter().acquire(acct.id)
            if lease is None:
                last_status, last_detail = 429, f"账号 {acct.id} 并发排队超时"
                continue

            # 拿到 lease 后**再查一次**窗口：循环开头那次只是快路径（省掉余额/
            # 设备绑定两次上游调用），acquire 可能排队最长 90s，期间同一账号
            # 的其它请求早就把窗口填满了。不复查的话，一瞬间 30 个并发全在窗口
            # 还空时通过开头检查、再挨个排队走到 record，实际打出去 30 > 28。
            # 复查后超发最多被 max_concurrent（2）兜住，28+2=30 仍在实测阈值 33 以下。
            if not rate_win.has_capacity(acct.id):
                lease.release()
                rate_limited.append(acct.id)
                continue

            # v2 已恢复计费；继续沿用稳定的非流式上游 + 本地合成，客户端要
            # 流式时拿到完整 JSON 后再合成，不在本次修复里同时切换原生 SSE。
            # 见 CHAT_PATH 的注释和 _synthesize_openai_stream 的说明。
            upstream_body = build_chat_body(body, model=upstream_model, stream=False)
            # 必须带设备头：绑定成功后上游按设备认会话，聊天却不声明自己属于
            # 那台设备 = 未绑定设备在调模型，正是 200108 风控抓的形态。
            headers = build_headers(access_token, _device_id_for(acct.id))
            url = f"{endpoint()}{CHAT_PATH}"
            # 登记进滑动窗口：放在 send 之前、所有可能 continue 的检查之后，
            # 只统计真正发出去的请求（见 AccountRateWindow.record 的说明）。
            rate_win.record(acct.id)
            try:
                req = client.build_request(
                    "POST", url, json=upstream_body, headers=headers,
                    # 非流式上游的超时用 _TIMEOUT_NONSTREAM（300s read），
                    # 即使是流式请求——因为上游就是非流式。
                    timeout=_TIMEOUT_NONSTREAM)
                # 永远 stream=False：上游就是非流式，不需要 httpx 的流式接收
                resp = await client.send(req, stream=False)
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
                if failover.is_account_risk_control(resp.status_code, code, detail):
                    # 账号级风控：整号长冷却，**不能**降级成模型级——否则
                    # 每个模型都去撞一次，持续给上游发「异常调用」信号。
                    failover.mark_cooldown(
                        acct.id, retry_after=str(int(failover._RISK_CONTROL_COOLDOWN_S)),
                        reason=f"账号风控: {detail}")
                elif failover.is_credit_exhausted(resp.status_code, code, detail):
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
            # 流式请求也用非流式闸门——上游已经被强制为非流式了，响应是完整 JSON。
            try:
                gate = await self._gate_first_event(resp, upstream_model)
            except BaseException:
                lease.release()
                await _drain(resp)
                raise

            if gate.error_code:
                lease.release()
                last_status, last_detail = gate.error_code, gate.error_message
                code = _extract_code(gate.error_message)
                if failover.is_account_risk_control(gate.error_code, code,
                                                    gate.error_message):
                    failover.mark_cooldown(
                        acct.id, retry_after=str(int(failover._RISK_CONTROL_COOLDOWN_S)),
                        reason=f"账号风控: {gate.error_message}")
                elif failover.is_credit_exhausted(gate.error_code, code,
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
                # 积分消耗在**响应收尾**结算（见 _settle_credit）：查一次真实
                # 余额，与请求前读数相减。这里只记下请求前的余额快照，
                # 不能凭倍率瞎算——见 _settle_credit 的说明。

            if stream:
                # 上游统一非流式，响应已经是完整 JSON。这里拆成 OpenAI SSE
                # 合成流，保持现有客户端出口不变；v2 只负责恢复真实计费。
                payload = gate.payload
                if payload is None:
                    await gate.resp.aclose()
                    lease.release()
                    raise HTTPException(
                        status_code=502,
                        detail={"error": {
                            "message": "raccoon upstream returned non-JSON",
                            "type": "bad_gateway"}},
                    )
                await gate.resp.aclose()
                if protocol == "anthropic":
                    return StreamingResponse(
                        _synthesize_anthropic_stream(
                            payload, upstream_model, lease=lease, provider=self,
                            access_token=access_token, balance_before=balance_before),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "close"},
                    )
                return StreamingResponse(
                    _synthesize_openai_stream(
                        payload, upstream_model, lease=lease, provider=self,
                        access_token=access_token, balance_before=balance_before),
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
                # 非流式在这里结算（流式在 _ReplayStream.aclose 里）
                after = await asyncio.to_thread(self._balance_now, access_token)
                self._settle_credit(ACCOUNT_META.get(), balance_before, after)
                return JSONResponse(content=payload)
            finally:
                lease.release()

        # 到这里 last_status 仍是 0 = 没有任何请求拿到过上游响应、也没 AuthError/
        # 超时——只可能是全部账号都被窗口跳过（开头快路径或 acquire 后复查）。
        # 不能用 tried == 0 判：复查失败的账号 tried 已经加过了。
        if last_status == 0 and rate_limited:
            # 全部账号都是「滑动窗口到顶」被跳过的：一个上游请求都没发。
            # 直接 429 并说清最快多久恢复——调用方（model_order）据此换通道。
            recover = min(rate_win.recovery_in(a) for a in rate_limited)
            n1, n5 = rate_win.counts(rate_limited[0])
            raise HTTPException(
                status_code=429,
                detail={"error": {
                    "message": (
                        f"raccoon 所有账号均达频率上限（滑动窗口 "
                        f"{n1}/{rate_win.per_minute}/min · "
                        f"{n5}/{rate_win.per_5min}/5min，账号 {len(rate_limited)} 个），"
                        f"最快 {recover:.0f}s 后恢复；未打上游"),
                    "type": "rate_limit_error",
                    "retry_after_s": round(recover, 1),
                }},
            )

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

    async def _gate_first_event(self, resp: httpx.Response,
                                upstream_model: str) -> "_Gate":
        """校验上游响应再决定采用还是换号。

        三件事：

        1. **识别带内错误**：上游偶发把错误塞进 200 的响应里（``{"error":...}``）。
        2. **校验 model 回显**：未知模型会静默回落默认模型，必须比对。
        3. **识别非 JSON**：拿不到 payload = 假成功，换号。

        只处理**非流式**响应。上游请求统一非流式，客户端要流式由
        ``_synthesize_openai_stream`` 在本地合成。以前这里还有一条读
        ``aiter_lines()`` 的 SSE 分支，改成
        全非流式之后不可达，已删除——留着会让后来者以为流式仍走上游。
        """
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
        # 已过期的包不渲染：它们已作废、不再计入可用余额（balance 已扣掉），
        # 显示出来用户会以为还有额度可用（2026-11-25 用户实报「看着过期的发放
        # 积分，不知道是不是能用」）。与上面算 granted_total 时的过期过滤同口径。
        #
        # 单包没有「剩余量」字段：上游 /points/v2/bills 给的是这笔**发放了多少**
        # （points），不是「还剩多少」。之前把发放量塞进 remaining，界面显示
        # 「剩 300 · 已用未知」——既不是真剩余，又因拿不到单包已用而恒显示
        # 「已用未知」，双重误导。现在 remaining=None，发放量写进 note，前端
        # 通用组件走「无数字」分支，只显示 label + 到期日 + note。
        #
        # ``expire_ts`` 必须是 **epoch 秒**：前端 ``fmtExpireMmdd`` 按
        # ``new Date(ts * 1000)`` 算，传 ISO 字符串会渲染成
        # 「NaN-NaN-NaN 到期」（2026-11-23 用户实报）。
        if bills:
            now = time.time()
            for bill in bills:
                expire_iso = bill.get("expire_at") or ""
                expire_epoch = _epoch_from_iso(expire_iso)
                if expire_epoch and expire_epoch < now:
                    continue  # 已过期作废，不展示
                points = round(float(bill.get("points") or 0), 1)
                items.append({
                    "label": f"{prefix}{bill.get('event') or '积分发放'}",
                    "used": None,
                    "total": None,
                    "remaining": None,
                    "percent": None,
                    "reset_ts": None,
                    "expire_ts": expire_epoch or None,
                    "unit": "credits",
                    # 发放量是上游给的真实数，写进 note 让用户知道「这份包有多大」；
                    # 但不冒充剩余量——单包已用无法从上游拿到（expense 流水不带包归属）。
                    "note": (f"发放 {points} 分，"
                             + ("无到期时间" if not expire_epoch
                                else "到期后这部分积分作废")),
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
            with core_net.client(timeout=_TIMEOUT) as c:
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

    # ---- 积分结算（读真实余额差）---------------------------------------
    #
    # 小浣熊**不提供**「本次请求扣了多少分」的字段。账单流水
    # （/points/v2/bills）里虽然有 ``points=-2`` 这类消费记录，但**不带模型名
    # 也不带请求 id**，无法归属到具体某条请求。
    #
    # 所以只有一条可靠口径：**请求前后各读一次余额，差值就是本条消耗**
    # （与 antigravity 的「池水位落差」同一个思路）。
    #
    # 曾经试过「1 × 模型倍率」——那是错的：实测同模型短请求扣 2 分、
    # 长请求扣 10~14 分，说明它是**按 token 量计费**的，倍率只是单价系数
    # （2026-11-23 用受控实验证伪）。

    def _balance_now(self, access_token: str) -> float | None:
        """读一次当前可用积分（结算用；失败返回 None）。"""
        data = self._fetch_json(BALANCE_PATH, access_token)
        parsed = parse_balance(data) if data else {}
        val = parsed.get("credits")
        return float(val) if isinstance(val, (int, float)) else None

    def _settle_credit(self, frame: dict[str, Any] | None,
                       before: float | None, after: float | None) -> None:
        """把「请求前后余额差」写进请求帧（observability 落库时读 ``ag_credit``）。

        口径说明：

        * 差值 ``<= 0``（余额没动/反而涨了）→ **不写**。面板记 —，不拿 0
          冒充「免费」——免费模型与「读不到」在面板上必须可区分。
        * 并发请求会让差值互相污染（两条同时在飞时，各自都可能看到对方扣的
          分）。所以只有「本条在飞期间余额单调下降」这个前提成立时才可信；
          我们的每账号并发闸门默认 2，偏差有界，且比编一个数字诚实。

        异常一律吞掉：结算只是旁路，不能把已成功的请求变成失败。
        """
        if not isinstance(frame, dict):
            return
        if before is None or after is None:
            return
        delta = round(before - after, 2)
        if delta <= 0:
            return
        frame["ag_credit"] = delta
        # **不标 ``ag_credit_exact``。** 上游没有任何接口返回「本条请求扣了几分」，
        # 这个数是两次余额读数相减推出来的（并发时还会互相污染，见上）。
        # 按约定「不是接口返回的，日志里的积分一律带 ≈」，面板据此显示「≈N」，
        # 不冒充上游实扣。（2026-11-24 用户定的规矩；此前这里写死 True，
        # 让 raccoon 的余额差与 kiro/qoder 的接口实扣在面板上长得一模一样。）

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

    def _checkin_for_account(self, acct: Any, *,
                             index: int) -> tuple[dict[str, Any] | None, str]:
        """单账号签到状态。返回 ``(status, error)``；失败时 status 为 None。

        判据是**流水**而不是接口返回码：上游对重复操作也回 200 但余额不动。
        """
        try:
            token, _cred = ensure_account_token(acct.id)
        except AuthError as exc:
            return None, str(exc)[:120]
        try:
            bills = self._bills(token)
        except Exception as exc:  # noqa: BLE001 — 单账号失败不该让整页 500
            log.warning("raccoon 签到状态查询失败（%s）: %s", acct.id, exc)
            return None, str(exc)[:120]
        done = has_daily_grant_today(bills)
        credits = _today_daily_grant_points(bills) if done else None
        return {
            "checked_in": done,
            "claimable": not done,
            "credits": credits,
            # 日历合并用（与 codebuddy/dumate 同字段名、同 YYYY-MM-DD 口径）：
            # 上游没有签到历史接口，靠流水里的 daily_grant 反推。
            "checkin_dates": _daily_grant_dates(bills),
            # 每天 00:00 +08:00 发新的一笔；上游没给这个字段，是规则推断
            "next_ts": next_daily_reset_epoch(),
            "next_ts_source": "inferred",
            "message": (f"今日 +{credits:g} 分已到账" if done and credits
                        else "今日每日积分已到账" if done
                        else "今日每日积分尚未到账（点「签到」触发页面启动）"),
        }, ""

    def checkin_status(self) -> dict[str, Any] | None:
        """今日签到状态：以**发放流水**为准，不看接口返回码。

        上游每日 300 分挂在「当天第一次页面启动」上，所以「今天有没有
        ``daily_grant``」就是唯一可信的签到判据。

        多账号**每个都查**（2026-11-23 用户实报：导入了两个号，签到卡只反映
        一个号）。聚合口径与 codebuddy 一致：任一可领→可领；全部已签→已签；
        个别账号失败只在明细里标、不阻塞整页。单账号直通、不带 ``accounts``
        明细——徽标本身就是它的状态，展开纯属噪音。
        """
        accounts = list_accounts()
        if not accounts:
            return {
                "checked_in": False,
                "claimable": False,
                "message": "未导入账号",
            }
        if len(accounts) == 1:
            st, err = self._checkin_for_account(accounts[0], index=1)
            if st is None:
                return {"checked_in": False, "claimable": False,
                        "message": err or "查询失败"}
            return st

        claimable_accts: list[str] = []
        signed_accts: list[str] = []
        failed = 0
        details: list[dict[str, Any]] = []
        total_credits = 0.0
        all_dates: set[str] = set()
        for pos, acct in enumerate(accounts, 1):
            name = acct.alias or acct.name or acct.id
            st, err = self._checkin_for_account(acct, index=pos)
            if st is None:
                failed += 1
                details.append({"index": pos, "id": acct.id, "name": name,
                                "error": err or "查询失败"})
                continue
            if st.get("claimable"):
                claimable_accts.append(f"#{pos}")
            elif st.get("checked_in"):
                signed_accts.append(f"#{pos}")
            if isinstance(st.get("credits"), (int, float)):
                total_credits += st["credits"]
            all_dates.update(st.get("checkin_dates") or [])
            details.append({
                "index": pos,
                "id": acct.id,
                "name": name,
                "checked_in": bool(st.get("checked_in")),
                "claimable": bool(st.get("claimable")),
                "daily_credit": st.get("credits"),
                "message": st.get("message"),
            })

        status: dict[str, Any] = {
            "checked_in": bool(signed_accts) and not claimable_accts,
            "claimable": bool(claimable_accts),
            "next_ts": next_daily_reset_epoch(),
            "next_ts_source": "inferred",
            "accounts": details,
            # 多账号取**并集**：日历画的是「这个通道这天有没有签」，任一账号
            # 签过就该显示（按账号分别画会需要改日历的数据结构，收益不大）。
            "checkin_dates": sorted(all_dates),
            "message": "",
        }
        if claimable_accts:
            status["message"] = f"账号 {'、'.join(claimable_accts)} 可领"
        elif signed_accts:
            status["message"] = f"账号 {'、'.join(signed_accts)} 已签"
        if failed:
            status["message"] = (status["message"] + " " if status["message"] else "") + \
                f"（{failed}/{len(accounts)} 个账号查询失败）"
        if total_credits:
            status["credits"] = total_credits
        return status

    def checkin_claim(self) -> dict[str, Any] | None:
        """触发今日每日积分发放：走一遍「页面启动」序列，然后核对流水。

        这是本通道唯一的「领取」动作——上游没有 claim 接口，补发挂在页面启动
        事件上。执行后**重新读流水**判定是否真到账，返回的状态与
        :meth:`checkin_status` 同构。

        全部请求尽力而为：单步失败不影响其它步（是否到账以流水为准）。

        多账号**每个都签**（2026-11-23 用户实报：两个号只处理了一个）。逐号
        执行页面启动、各自核对流水；单号失败只记明细，不阻断其它号。
        """
        accounts = list_accounts()
        if not accounts:
            return {"checked_in": False, "claimable": False,
                    "message": "未导入账号，无法签到"}
        if len(accounts) == 1:
            st = self._claim_for_account(accounts[0], index=1)
            return st

        details: list[dict[str, Any]] = []
        signed: list[str] = []
        failed = 0
        total_credits = 0.0
        all_dates: set[str] = set()
        for pos, acct in enumerate(accounts, 1):
            name = acct.alias or acct.name or acct.id
            st = self._claim_for_account(acct, index=pos)
            if st.get("error"):
                failed += 1
                details.append({"index": pos, "id": acct.id, "name": name,
                                "error": st["error"]})
                continue
            if st.get("checked_in"):
                signed.append(f"#{pos}")
            if isinstance(st.get("credits"), (int, float)):
                total_credits += st["credits"]
            all_dates.update(st.get("checkin_dates") or [])
            details.append({
                "index": pos,
                "id": acct.id,
                "name": name,
                "checked_in": bool(st.get("checked_in")),
                "claimable": bool(st.get("claimable")),
                "daily_credit": st.get("credits"),
                "message": st.get("message"),
            })

        status: dict[str, Any] = {
            "checked_in": bool(signed) and not failed,
            "claimable": False,
            "next_ts": next_daily_reset_epoch(),
            "next_ts_source": "inferred",
            "accounts": details,
            "checkin_dates": sorted(all_dates),
            "message": "",
        }
        if signed:
            status["message"] = f"账号 {'、'.join(signed)} 签到成功"
        if failed:
            status["message"] = (status["message"] + " " if status["message"] else "") + \
                f"（{failed}/{len(accounts)} 个账号签到失败）"
        if total_credits:
            status["credits"] = total_credits
        return status

    def _claim_for_account(self, acct: Any, *, index: int) -> dict[str, Any]:
        """单账号签到：跑一遍页面启动，再以流水核对是否真到账。

        返回结构同 :meth:`_checkin_for_account`；拿不到 token 时给 ``error``。
        """
        try:
            token, _cred = ensure_account_token(acct.id)
        except AuthError as exc:
            return {"checked_in": False, "claimable": False, "error": str(exc)[:120]}
        cred = load_account_cred(acct.id) or {}
        device_id = str(cred.get("device_id") or "") or _derive_device_id(acct.id)
        self._run_page_boot(token, device_id)
        # 给上游几秒入账时间：实测到账在页面加载后数秒内
        time.sleep(2.0)
        bills = self._bills(token)
        done = has_daily_grant_today(bills)
        # 今日实际到账金额：日历侧边栏要显示「签到加了多少」，取流水里
        # 今天那笔 daily_grant 的 points（不是硬编码 300——上游调整过面额）。
        credits = _today_daily_grant_points(bills) if done else None
        if done:
            log.info("raccoon 账号 %s 今日每日积分已到账（%s 分）", acct.id, credits)
        else:
            log.warning("raccoon 账号 %s 页面启动后仍未见到今日 daily_grant",
                        acct.id)
        return {
            "checked_in": done,
            "claimable": not done,
            "credits": credits,
            "checkin_dates": _daily_grant_dates(bills),
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
                with core_net.client(timeout=_TIMEOUT) as c:
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


def _daily_grant_dates(bills: list[dict[str, Any]]) -> list[str]:
    """从发放流水反推**历史签到日期**（``YYYY-MM-DD``，+08:00 口径）。

    为什么需要它：签到日历（``/ui`「打卡日历」）靠各通道返回的
    ``checkin_dates`` 合并历史，raccoon 原来**一个都不给**，于是日历里
    完全没有小浣熊——用户实报「打卡日历里也没有展示小浣熊的」（2026-11-23）。

    上游没有「签到历史」接口，但流水里每笔 ``daily_grant`` 都带 ``grant_at``，
    那就是当天签到到账的时刻，逐条取日期即可。``_bills`` 默认只拉 50 条
    income，覆盖最近几十天，够日历用（35 天窗口）。
    """
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    out: list[str] = []
    for bill in bills or []:
        if str(bill.get("biz_type") or "") != "daily_grant":
            continue
        try:
            dt = datetime.fromisoformat(str(bill.get("grant_at") or ""))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        day = dt.astimezone(tz).date().isoformat()
        if day not in out:
            out.append(day)
    return sorted(out)


def _derive_device_id(account_id: str) -> str:
    """从账号 id 派生稳定 device_id（≤64 字符，上游对超长会回 500）。

    上游对设备有长度限制（实测超 64 字符回 ``500 value too long for character
    varying(64)``），所以哈希后截断，不用原始 id。
    """
    import hashlib

    return hashlib.sha256(f"raccoon-device:{account_id}".encode()).hexdigest()[:32]


def _device_id_for(account_id: str) -> str:
    """取该账号的 device_id：优先用绑定成功后上游回的 ``dev...``。

    上游绑定接口会分配自己的设备 id（``dev660e...``），之后的请求应该带**它**
    而不是我们本地派生那个哈希值——带错的 id 与「没绑定」没区别，风控照样
    会盯上。绑定成功后由 :func:`_ensure_device_bound` 写回 cred。

    读不到 cred（如账号已被删除）时退回派生值，至少不比不带设备头更差。
    """
    cred = load_account_cred(account_id) or {}
    return str(cred.get("device_id") or "") or _derive_device_id(account_id)


#: 已确认绑定过的账号（内存标记，避免每个请求都查一次 user_info）。
_bound_ok: set[str] = set()

#: 绑定失败后的重试间隔（秒）：绑不上时别每个请求都撞一次上游。
_bind_retry_after: dict[str, float] = {}
_BIND_RETRY_S = 600.0


def _ensure_device_bound(account_id: str, access_token: str) -> None:
    """确保该账号的设备已绑定；未绑定就绑一次（失败静默，不阻断转发）。

    ## 为什么必须做

    2026-11-23 排查「新号用一阵就被风控（200108）」的根因：绑定请求一直
    400（字段名写成 ``device_id``，上游要 ``client_device_id``），设备从未
    绑上，``user_info`` 永远是 ``trusted: false / reason:
    session_without_bound_device``。所有模型调用都来自一个未绑定设备的会话，
    累积调用量后整号被风控——与并发/频率无关（实测触发时在途并发只有 2）。

    绑定成功后上游回 ``data.device_id``（``dev...``），必须写回 cred 并在
    后续请求头里带上，否则仍被视为陌生会话。

    失败不抛：绑定是「降低风控概率」的加固，不该把能用的转发搞挂。
    """
    if account_id in _bound_ok:
        return
    now = time.monotonic()
    if now < _bind_retry_after.get(account_id, 0.0):
        return
    device_id = _derive_device_id(account_id)
    try:
        base = endpoint()
        headers = build_headers(access_token, device_id)
        headers["Accept"] = "application/json"
        with core_net.client(timeout=_TIMEOUT) as c:
            # 先探活：已绑定就只刷心跳，省一次写操作
            info = c.get(f"{base}{USER_INFO_PATH}", headers=headers)
            trusted = False
            if info.status_code < 400:
                try:
                    td = (info.json().get("data") or {}).get("trusted_device") or {}
                    trusted = bool(td.get("trusted")) and not td.get("needs_bind")
                    upstream_dev = str(td.get("device_id") or "")
                except (ValueError, AttributeError):
                    upstream_dev = ""
            else:
                upstream_dev = ""
            if trusted:
                if upstream_dev:
                    _remember_device_id(account_id, upstream_dev)
                _bound_ok.add(account_id)
                return
            resp = c.post(f"{base}{BIND_DEVICE_PATH}", headers=headers,
                          json=bind_device_body(device_id))
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            upstream_dev = str((payload.get("data") or {}).get("device_id") or "")
            if upstream_dev:
                _remember_device_id(account_id, upstream_dev)
            _bound_ok.add(account_id)
            _bind_retry_after.pop(account_id, None)
            log.info("raccoon 账号 %s 设备已绑定（trusted），风控风险降低",
                     account_id)
    except Exception as exc:  # noqa: BLE001 - 加固动作，失败不阻断转发
        _bind_retry_after[account_id] = now + _BIND_RETRY_S
        log.warning("raccoon 账号 %s 设备绑定失败（%s），%.0fs 后重试",
                    account_id, exc, _BIND_RETRY_S)


def _remember_device_id(account_id: str, device_id: str) -> None:
    """把上游分配的设备 id 写回 cred（失败只记日志）。"""
    try:
        cred = load_account_cred(account_id) or {}
        if cred.get("device_id") == device_id:
            return
        cred["device_id"] = device_id
        save_account_cred(cred)
    except Exception as exc:  # noqa: BLE001 - 写盘失败不该影响转发
        log.debug("raccoon 账号 %s 设备 id 写回失败: %s", account_id, exc)


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


# ---------------------------------------------------------------------------
# 合成流：上游非流式响应 → OpenAI SSE / Anthropic SSE
#
# 所有请求都走非流式；客户端要流式时，拿到完整 JSON 后本地拆成 SSE 帧。
# v2 恢复计费后仍保留这条稳定出口，不把端点修复与原生 SSE 切换耦合。
# ---------------------------------------------------------------------------


async def _synthesize_openai_stream(
    payload: dict[str, Any],
    model: str,
    *,
    lease: rate_limit.Lease | None = None,
    provider: "RaccoonProvider | None" = None,
    access_token: str = "",
    balance_before: float | None = None,
) -> AsyncIterator[str]:
    """把非流式 OpenAI 响应拆成 SSE 帧。

    帧序列（与 v2 上游的真实流式行为对齐）：
    1. ``role`` 帧（``delta: {"role": "assistant"}``）
    2. ``reasoning_content`` 帧（如果有，放在 content 之前）
    3. ``content`` 帧（完整文本，一帧发完——非流式拿到的就是完整文本）
    4. ``tool_calls`` 帧（如果有，每个 tool_call 一帧）
    5. ``finish_reason`` 帧（``delta: {}, finish_reason: "stop"``）
    6. ``usage`` 帧（``choices: []`` + ``usage``，OpenAI 兼容口径）
    7. ``[DONE]``

    收尾时结算积分 + 释放 lease（与 _ReplayStream.aclose 同一条路径）。
    """
    try:
        choices = payload.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or "stop"
        chunk_id = payload.get("id") or ""
        created = payload.get("created") or 0
        model_name = payload.get("model") or model
        usage = payload.get("usage")

        def _chunk(delta: dict[str, Any], fr: str | None = None) -> str:
            c: dict[str, Any] = {
                "id": chunk_id, "object": "chat.completion.chunk",
                "created": created, "model": model_name,
                "choices": [{"index": 0, "delta": delta, "finish_reason": fr}],
            }
            return f"data: {json.dumps(c, ensure_ascii=False)}\n\n"

        # 1. role 帧
        yield _chunk({"role": "assistant"})

        # 2. reasoning_content（如果有）
        reasoning = message.get("reasoning_content")
        if reasoning:
            yield _chunk({"reasoning_content": reasoning})

        # 3. content
        content = message.get("content")
        if content:
            yield _chunk({"content": content})

        # 4. tool_calls
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for i, tc in enumerate(tool_calls):
                delta_tc = {"index": i, **tc}
                yield _chunk({"tool_calls": [delta_tc]})

        # 5. finish_reason
        yield _chunk({}, fr=finish_reason)

        # 6. usage（OpenAI 兼容：choices 为空 + usage 字段）
        if usage:
            usage_chunk = {
                "id": chunk_id, "object": "chat.completion.chunk",
                "created": created, "model": model_name,
                "choices": [], "usage": usage,
            }
            yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"

        # 7. [DONE]
        yield "data: [DONE]\n\n"

        # 结算积分 + 写 usage 到 meta
        if usage:
            meta = ACCOUNT_META.get()
            if isinstance(meta, dict):
                meta["usage"] = usage
        if provider and access_token:
            try:
                after = await asyncio.to_thread(provider._balance_now, access_token)
                provider._settle_credit(ACCOUNT_META.get(), balance_before, after)
            except Exception as exc:  # noqa: BLE001
                # 结算失败不影响已经发出去的内容，但**必须留痕**：静默 pass 会
                # 让「积分对不上」变成无从查起的问题（写这条测试时就踩过：
                # time 没导入导致 NameError 被吞，表现成「结算瞬间完成」）。
                log.warning("raccoon 积分结算失败（合成流）: %s: %s",
                            type(exc).__name__, exc)
    finally:
        if lease is not None:
            lease.release()


async def _synthesize_anthropic_stream(
    payload: dict[str, Any],
    model: str,
    *,
    lease: rate_limit.Lease | None = None,
    provider: "RaccoonProvider | None" = None,
    access_token: str = "",
    balance_before: float | None = None,
) -> AsyncIterator[str]:
    """把非流式 OpenAI 响应转成 Anthropic Messages 事件流。

    复用 ``AnthropicStreamConverter``：把非流式响应拆成 OpenAI chunk 喂给它，
    它负责转成 Anthropic 事件。与 ``_to_anthropic_openai_stream`` 同一条路径，
    只是输入来源不同（那里是上游 SSE 流，这里是本地合成的 chunk）。
    """
    from ..protocols.anthropic_adapter import AnthropicStreamConverter

    converter = AnthropicStreamConverter(model)

    def _emit(name: str, data: dict[str, Any]) -> str:
        return (f"event: {name}\ndata: "
                f"{json.dumps(data, ensure_ascii=False)}\n\n")

    try:
        choices = payload.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or "stop"
        chunk_id = payload.get("id") or ""
        created = payload.get("created") or 0
        model_name = payload.get("model") or model
        usage = payload.get("usage")

        def _chunk(delta: dict[str, Any], fr: str | None = None) -> dict[str, Any]:
            return {
                "id": chunk_id, "object": "chat.completion.chunk",
                "created": created, "model": model_name,
                "choices": [{"index": 0, "delta": delta, "finish_reason": fr}],
            }

        # 逐帧喂给转换器
        chunks = [
            _chunk({"role": "assistant"}),
        ]
        reasoning = message.get("reasoning_content")
        if reasoning:
            chunks.append(_chunk({"reasoning_content": reasoning}))
        content = message.get("content")
        if content:
            chunks.append(_chunk({"content": content}))
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for i, tc in enumerate(tool_calls):
                chunks.append(_chunk({"tool_calls": [{"index": i, **tc}]}))
        chunks.append(_chunk({}, fr=finish_reason))
        # usage 必须单独成帧喂进去：``AnthropicStreamConverter.feed_chunk`` 是从
        # ``chunk["usage"]`` 里取的（见 anthropic_adapter.py:535），不喂的话
        # message_delta 里的 input_tokens/output_tokens 恒为 0——面板和 metrics
        # 都会显示「没有 token」。这一帧按 OpenAI 口径是 choices=[] + usage。
        if isinstance(usage, dict):
            chunks.append({
                "id": chunk_id, "object": "chat.completion.chunk",
                "created": created, "model": model_name,
                "choices": [], "usage": usage,
            })

        for chunk in chunks:
            for name, data in converter.feed_chunk(chunk):
                yield _emit(name, data)
        for name, data in converter.finish():
            yield _emit(name, data)

        # 结算积分 + 写 usage 到 meta
        if usage:
            meta = ACCOUNT_META.get()
            if isinstance(meta, dict):
                meta["usage"] = usage
        if provider and access_token:
            try:
                after = await asyncio.to_thread(provider._balance_now, access_token)
                provider._settle_credit(ACCOUNT_META.get(), balance_before, after)
            except Exception as exc:  # noqa: BLE001
                # 结算失败不影响已经发出去的内容，但**必须留痕**：静默 pass 会
                # 让「积分对不上」变成无从查起的问题（写这条测试时就踩过：
                # time 没导入导致 NameError 被吞，表现成「结算瞬间完成」）。
                log.warning("raccoon 积分结算失败（合成流）: %s: %s",
                            type(exc).__name__, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("raccoon anthropic synthesize aborted: %s: %s",
                    type(exc).__name__, exc)
        for name, data in converter.close_open_blocks():
            yield _emit(name, data)
        yield _emit("error", {"type": "error",
                              "error": {"type": "api_error", "message": str(exc)}})
    finally:
        if lease is not None:
            lease.release()
