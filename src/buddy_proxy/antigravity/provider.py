"""Antigravity provider —— cloudcode-pa 免费通道转发。

上游是 Gemini 原生协议的 antigravity 包装（``/v1internal:streamGenerateContent
?alt=sse``，envelope 见 convert.py），请求/响应双向转换的骨架与 gemini 通道
同款；差异：

- 多账号 failover：按登录顺序主备降级（见 failover.py），429/403/凭据失效
  冷却当前账号换下一个；首个语义事件到达后绝不重放（防重复计费）。
- 端点 fallback：daily-cloudcode-pa 优先、cloudcode-pa 兜底，请求级网络错误
  /5xx/404 才换端点（4xx 业务错误换端点没意义）。
- reasoning_effort 自动映射成 gemini 3 系模型名的 effort 后缀。
- quota：上游有 ``fetchAvailableModels``（带各模型剩余比例与重置时间），
  可画真进度条；拿不到时退化为静态说明（与 gemini 同策略）。

认证：OAuth（buddy login antigravity，支持多账号），access_token 过期自动刷新。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..providers.base import BaseProvider
from ..core.metrics import ACCOUNT_META
from ..gemini.convert import gemini_response_to_chat
from . import failover
from .convert import apply_effort_suffix, chat_to_antigravity_request
from .credentials import (
    AuthError,
    ensure_account_token,
    has_cred,
    list_accounts,
    load_account_cred,
)
from .model_catalog import (
    DEFAULT_MODELS,
    MODELS,
    _EFFORTS,
    _MODELS_JSON,
    _MODEL_BY_ID,
    BLOCKED_PREFIXES,
    _load_models as _load_catalog_models,
    _default_effort,
    _group_for_model,
    _is_blocked_base,
    _is_public_upstream_model,
    _split_effort_suffix,
    _strip_effort_suffix,
)

log = logging.getLogger(__name__)

ENDPOINTS = (
    "https://daily-cloudcode-pa.googleapis.com",
    "https://cloudcode-pa.googleapis.com",
)
#: 流式：read 是「相邻两次读」的上限——首事件前（闸门在等）与流中卡死都按它断。
#: 实测（2026-10-03，266 条 gemini-3.8-flash）：首事件 p50 4.8s / p90 13s /
#: p99 57s（额度风暴窗口上游排队的怪胎，dur=240s 只吐 7 个 chunk），90s
#: 放过正常慢、掐掉排队怪胎换号重试。旧值 600s：一次挂死白等 10 分钟。
_TIMEOUT_STREAM = httpx.Timeout(connect=15.0, read=90.0, write=60.0, pool=15.0)
#: 非流式：一次 read 拿到全部响应 = 总时长上限。实测成功请求最长 16s，
#: 120s 已 7 倍余量；客户端（claude code 的小工具调用）挂死时 504 换号
#: 而不是干等 600s。
_TIMEOUT_NONSTREAM = httpx.Timeout(connect=15.0, read=120.0, write=60.0, pool=15.0)
#: 兼容旧名（health/一次性 client 等处仍引用）。
_TIMEOUT = _TIMEOUT_NONSTREAM
#: failover 循环的**尝试期**总预算（从进循环到每次尝试开始前检查）：超时
#: 换号后最坏 90s/账号，预算防 3 个账号串成 4 分半。只挡「还没开始试」的
#: 尝试——已提交的流想跑多久跑多久（流中卡死由 read 超时管）。
_ATTEMPT_DEADLINE_S = 180.0
#: 流式首事件闸门的缓冲行上限（防异常上游无界攒内存，见 _gate_first_event）。
_GATE_BUFFER_MAX_LINES = 256

def _load_models() -> list[dict[str, Any]]:
    """兼容旧导入路径；实际目录逻辑已拆到 ``model_catalog``。"""
    return _load_catalog_models(_MODELS_JSON)


_QUOTA_NOTE = ("两组模型各自共享 5 小时 + 每周两个额度池（Gemini 组 / Claude+GPT 组），"
               "按 token 成本比例消耗。上游只在用量逼近池上限时才下调读数——"
               "显示 100% 代表两组池都接近满额，不是「没有额度」")

#: 多账号额度并发查询：整轮 deadline + 常驻线程池。
#: 常驻（不是每轮新建）的理由见 trae/pat/quota.py：每轮新建 + shutdown(wait=False)
#: 会让慢轮线程留在后台累积；常驻池上限封顶，慢轮占名额、后续轮次自然排队。
_QUOTA_ROUND_DEADLINE_S = 8.0
_QUOTA_WORKERS = 4
_quota_pool: "concurrent.futures.ThreadPoolExecutor | None" = None
_quota_pool_lock = threading.Lock()


def _quota_executor() -> "concurrent.futures.ThreadPoolExecutor":
    global _quota_pool
    with _quota_pool_lock:
        if _quota_pool is None:
            _quota_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=_QUOTA_WORKERS, thread_name_prefix="ag-quota")
        return _quota_pool


def _quota_failure_notice(failed: int, total: int) -> dict[str, Any]:
    """部分账号额度查询失败时的说明条（UI 警告色展示；benefits 认 query_failed 走短缓存）。"""
    return {
        "label": "Antigravity 额度查询失败",
        "used": None,
        "total": None,
        "remaining": f"{failed}/{total} 个账号查询失败（网络或凭据问题；其余账号数据照常展示）",
        "percent": None,
        "reset_ts": None,
        "query_failed": True,
    }


def _run_sync(factory):
    """协程工厂 → 独立事件循环执行（/ui 线程安全，见 mimo/provider._run_sync）。"""
    def _runner():
        return asyncio.run(factory())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _runner()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_runner).result(timeout=30)


def _upstream_error_response(resp: httpx.Response) -> JSONResponse:
    try:
        payload = resp.json()
    except Exception:
        payload = {"error": {"message": resp.text[:500], "type": "upstream_error"}}
    return JSONResponse(status_code=resp.status_code, content=payload)


class _UpstreamUnavailable(RuntimeError):
    """两个 endpoint 都没给出 HTTP 响应（超时/网络错）。

    过去这里直接抛 HTTPException(504/502)，会**打断整个账号 failover 循环**
    ——客户端按 read 超时干等后收一只写死状态的错误，哪怕后面还有两个健康
    账号。改为抛本异常由 forward 循环捕获：短冷却当前账号、换下一个重试。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _iso_to_epoch(value: Any) -> float:
    """上游 ISO 时间（``2026-10-02T20:28:46Z``）→ epoch 秒；解析失败返回 0。"""
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class AntigravityProvider(BaseProvider):
    #: /v1/models 里模型前缀即 ``antigravity/gemini-3.1-pro``。
    id = "antigravity"
    name = "Antigravity (Gemini/Claude/GPT free tier)"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # 模块级 MODELS 只是冷启动种子。运行时目录归实例持有，刷新一个 provider
        # 不能污染其它实例（测试、多 app 同进程也不会串目录）。
        self._models: list[dict[str, Any]] = [dict(m) for m in MODELS]
        self._model_by_id: dict[str, dict[str, Any]] = {
            str(m["id"]): m for m in self._models
        }

    # ---- BaseProvider 接口 ----

    def models(self) -> Sequence[dict[str, Any]]:
        return [
            {
                "id": m["id"],
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "description": str(m.get("description") or m["id"]),
            }
            for m in self._models
        ]

    async def refresh_models(self, force: bool = False) -> list[dict[str, Any]]:
        """逐全量账号拉取上游目录，成功结果取并集并原子替换实例目录。

        ``force`` 为通用刷新接口保留；本 provider 没有 TTL 缓存，每次均强拉。
        单账号失败不阻断其余账号，但部分成功时只增不删，保留失败账号可能独有的
        上一版条目；只有全账号成功才允许确认下架。若整轮没有有效成功结果（包括
        成功响应均为空），则抛错且保留旧目录。静态表中已验证条目的路由规则与
        metadata 优先保留，新发现条目才按上游 effort 后缀推导配置。
        """
        del force
        # 冷却只影响聊天选号，不影响目录发现；否则冷却账号独有模型会被静默下架。
        accounts = list_accounts()
        if not accounts:
            raise RuntimeError("Antigravity 没有可用于刷新模型目录的账号")

        # remote base -> 按账号/响应顺序去重后的 (完整上游名, effort)
        discovered: dict[str, list[tuple[str, str | None]]] = {}
        succeeded = 0
        errors: list[str] = []
        for acct in accounts:
            try:
                data = await self._fetch_available_models(acct.id)
                upstream = data.get("models") if isinstance(data, dict) else None
                if not isinstance(upstream, dict):
                    raise ValueError("响应缺少 models dict")
            except Exception as exc:  # noqa: BLE001 - 单账号失败不阻断目录并集
                errors.append(f"{acct.id}: {exc}")
                log.warning("antigravity 模型目录刷新跳过账号 %s: %s", acct.id, exc)
                continue
            succeeded += 1
            for raw_name in upstream:
                name = str(raw_name or "").strip()
                if not _is_public_upstream_model(name):
                    continue
                base, effort = _split_effort_suffix(name)
                # 旧代/内部基名（gemini-2.5/3-flash/3.1-flash/3.5 等，见 models.json
                # 的 blocked_prefixes）：fetchAvailableModels 仍回传，不拦就会被
                # 「重新拉取」灌回 /v1/models。curated 的 verified 模型不在前缀表里。
                if _is_blocked_base(base):
                    continue
                variants = discovered.setdefault(base, [])
                if all(existing != name for existing, _ in variants):
                    variants.append((name, effort))

        if not succeeded:
            detail = "; ".join(errors) or "无账号成功"
            raise RuntimeError(f"Antigravity 模型目录刷新全部失败：{detail}")
        if not discovered:
            raise RuntimeError("Antigravity 模型目录刷新成功响应均无公开模型")

        # curated 的 verified 模型永不下架：fetchAvailableModels **不会**回传
        # gemini-3.1-pro / gemini-3.6-flash 这类 premium 条目（实测只吐旧代
        # flash 变体），全账号成功时若按上游目录下架，就会把人工校验过的好模型
        # 连同旧模型一起洗掉。故无论成功与否，先把 models.json 里 verified=true
        # 的条目按其 upstream base 注入 discovered，保证它们始终留在目录里。
        for local in MODELS:
            if not local.get("verified"):
                continue
            upstream_name = str(local.get("upstream") or local.get("id") or "").strip()
            if not upstream_name:
                continue
            discovered.setdefault(_strip_effort_suffix(upstream_name), [(upstream_name, None)])

        if succeeded < len(accounts):
            # 部分账号失败时无法证明旧 id 已从所有账号下架；本轮只允许新增/更新。
            for local in self._models:
                model_id = str(local.get("id") or "").strip()
                upstream_name = str(local.get("upstream") or model_id).strip()
                if model_id and upstream_name:
                    base = _strip_effort_suffix(upstream_name)
                    discovered.setdefault(base, [(upstream_name, None)])

        # 已验证本地条目按对外 id 和 upstream base 双索引；例如静态
        # gemini-3.8-flash -> gemini-3.8-flash-tiered 也能命中并保留固定路由。
        local_by_base: dict[str, dict[str, Any]] = {}
        # MODELS 永远在前：某个静态已验证条目若一轮未出现、下一轮重新出现，仍须
        # 恢复 models.json 中的人工 metadata/路由规则，而不是退化成动态推导条目。
        for local in (*MODELS, *self._models):
            model_id = str(local.get("id") or "").strip()
            upstream_name = str(local.get("upstream") or model_id).strip()
            if model_id:
                local_by_base.setdefault(model_id, local)
            if upstream_name:
                local_by_base.setdefault(_strip_effort_suffix(upstream_name), local)

        merged: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for base, variants in discovered.items():
            local = local_by_base.get(base)
            if local is not None:
                entry = dict(local)
            else:
                efforts = [effort for _, effort in variants if effort is not None]
                # 一个 base 的同档位可能来自多个账号；保持首次发现顺序并去重。
                efforts = list(dict.fromkeys(efforts))
                entry = {
                    "id": base,
                    "upstream": base,
                    "group": _group_for_model(base),
                    "description": base,
                }
                if efforts:
                    entry["efforts"] = efforts
                    entry["default_effort"] = _default_effort(efforts)
            model_id = str(entry["id"])
            if model_id not in seen_ids:
                seen_ids.add(model_id)
                merged.append(entry)

        if not merged:  # 防未来过滤规则变化把有效响应意外清空
            raise RuntimeError("Antigravity 模型目录刷新后无可用模型")
        self._models = merged
        self._model_by_id = {str(m["id"]): m for m in merged}
        return list(self.models())

    def ensure_auth(self) -> None:
        if not has_cred():
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": "antigravity 未登录：请先运行 `buddy login antigravity`",
                        "type": "authentication_error",
                    }
                },
            )

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        self.ensure_auth()
        stream = bool(body.get("stream", False))
        model = str(body.get("model") or self._models[0]["id"]).removeprefix(f"{self.id}/")
        # 模型表条目驱动：upstream 真名 + effort 后缀（上游只认列表变体名，
        # gemini 3 系裸名会被 429 RESOURCE_EXHAUSTED 伪装拒绝）。刷新后的新模型
        # 与 models() 共用实例索引，发现后即可路由。
        entry = self._model_by_id.get(model)
        if entry is None:
            raise HTTPException(
                status_code=400,
                detail={"error": {
                    "message": f"antigravity 未知模型: {model}",
                    "type": "invalid_request_error",
                }},
            )
        upstream_model = apply_effort_suffix(entry, body.get("reasoning_effort"))

        # failover 循环：按登录顺位尝试可用账号（429/403/凭据失效 → 冷却换号）
        accounts = failover.available_accounts()
        if not accounts:
            raise HTTPException(
                status_code=429,
                detail={"error": {
                    "message": (f"antigravity 所有账号均在冷却中：{failover.cooldown_report()}"
                                "；额度冷却到点自动恢复，疑似拉黑的账号请在管理页删除"),
                    "type": "rate_limit_error",
                }},
            )

        from .fingerprint import auth_headers

        meta = ACCOUNT_META.get()  # metrics 账号归属（_instrument 放入的 dict）
        client = await self._get_client()
        method = "streamGenerateContent" if stream else "generateContent"
        started = time.monotonic()
        tried = 0
        last_status = 0
        last_detail = ""

        for acct in accounts:
            # 尝试期预算：前面账号的超时/失败把时间烧完后不再开新尝试，
            # 直接带已有错误收场（防 N 个账号 × 90s 串成 分钟级等待）。
            # 首个账号不受预算挡（预算是给「换号重试」踩刹车的）。
            if tried and time.monotonic() - started > _ATTEMPT_DEADLINE_S:
                last_status = last_status or 504
                last_detail = last_detail or "尝试预算用尽（上游持续无响应）"
                break
            tried += 1
            # token 刷新是同步 urllib；丢线程池避免卡事件循环。
            # token 与 cred 同源返回：project_id 从同一份 cred 取，多账号不串号。
            try:
                access_token, cred = await asyncio.to_thread(ensure_account_token, acct.id)
            except AuthError as exc:
                failover.mark_cooldown(acct.id, reason=f"凭据不可用: {exc}")
                continue
            project_id = str(cred.get("project_id") or "")
            if not project_id:
                failover.mark_cooldown(acct.id, reason="缺 project_id（onboarding 未完成）")
                continue
            if meta is not None:
                meta["account"] = acct.id

            upstream_body = chat_to_antigravity_request(
                body, project_id=project_id, model=upstream_model
            )
            try:
                resp = await self._send_with_fallback(
                    client, method, upstream_body,
                    auth_headers(access_token, upstream_model, stream), stream,
                )
            except _UpstreamUnavailable as exc:
                # 两个 endpoint 都没给出 HTTP 响应（超时/网络错）：过去直接 504
                # 打断整个 failover 循环（客户端干等满 read 超时）——现在换号重试。
                # 短冷却防「挂死账号每轮都被首选」；机器级断网时全账号一起进
                # 60s 冷却 = 通道级快速失败，到点自动重探。
                last_status, last_detail = 504, exc.message
                failover.mark_cooldown(acct.id, reason=exc.message)
                continue

            # 401：token 被上游拒（比如文件被手工改过）——强刷一次重试同账号；
            # 仍拒说明凭据层面失效，冷却换号
            if resp.status_code == 401:
                await _drain_and_close(resp, stream)
                try:
                    access_token, _ = await asyncio.to_thread(
                        ensure_account_token, acct.id, force_refresh=True)
                except AuthError as exc:
                    failover.mark_cooldown(acct.id, reason=f"强刷失败: {exc}")
                    continue
                try:
                    resp = await self._send_with_fallback(
                        client, method, upstream_body,
                        auth_headers(access_token, upstream_model, stream), stream,
                    )
                except _UpstreamUnavailable as exc:
                    last_status, last_detail = 504, exc.message
                    failover.mark_cooldown(acct.id, reason=exc.message)
                    continue
                if resp.status_code == 401:
                    last_status, last_detail = 401, await _error_detail(resp, stream)
                    await _drain_and_close(resp, stream)
                    failover.mark_cooldown(acct.id, reason="401 强刷后仍被拒")
                    continue

            # 403/429：账号级拒绝（额度/风控）——冷却该账号换下一个。
            # 403 文案是「Verify your account to continue.」时按拉黑档（6h）
            # 冷却：账号能登录但被 Google 风控挡在门外，60s 一探只会白打。
            if resp.status_code in (403, 429):
                last_status = resp.status_code
                last_detail = await _error_detail(resp, stream)
                failover.mark_cooldown(
                    acct.id,
                    retry_after=resp.headers.get("Retry-After"),
                    quota=resp.status_code == 429,
                    blacklist=failover.is_blacklist_signal(resp.status_code, last_detail),
                    reason=f"HTTP {resp.status_code}{(': ' + last_detail) if last_detail else ''}",
                )
                await _drain_and_close(resp, stream)
                continue

            if resp.status_code >= 400:
                # 其余业务 4xx（模型名不合法等）：换号没意义，原样透传
                if stream:
                    await resp.aread()
                try:
                    return _upstream_error_response(resp)
                finally:
                    await resp.aclose()

            # 200：过首事件闸门——首个上游事件若是 429/403 error，还没向客户端
            # 吐过任何字节，可以安全冷却换号；见到语义事件后绝不重放（防重复计费）
            gate = await self._gate_first_event(resp, stream)
            if gate.account_error:
                last_status, last_detail = gate.code, gate.message
                failover.mark_cooldown(
                    acct.id, quota=gate.code == 429,
                    blacklist=failover.is_blacklist_signal(gate.code, gate.message),
                    reason=f"带内 error {gate.code}: {gate.message}")
                await _drain_and_close(gate.resp, stream, gate.lines)
                continue
            if gate.eof:
                # 语义事件之前断流/空流：没向客户端吐过字节，换下一个账号。
                # 读超时（首事件前卡死）额外短冷却——挂死账号别每轮都被首选；
                # 干净 EOF 不冷却（可能只是网络抖动，与 trae/model_order 同思路）。
                if gate.timed_out:
                    last_status, last_detail = 504, "首事件前读超时"
                    failover.mark_cooldown(acct.id, reason="首事件前读超时")
                await _drain_and_close(gate.resp, stream, gate.lines)
                continue

            if stream:
                from ..gemini.provider import _to_anthropic_stream, _to_openai_stream

                replay = _ReplayStream(gate.resp, gate.buffered, gate.lines)
                if protocol == "anthropic":
                    return StreamingResponse(
                        _to_anthropic_stream(replay, upstream_model),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "close"},
                    )
                return StreamingResponse(
                    _to_openai_stream(replay, upstream_model),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "close"},
                )

            payload = gate.payload
            if payload is None:
                await gate.resp.aclose()
                raise HTTPException(
                    status_code=502,
                    detail={"error": {"message": "antigravity upstream returned non-JSON",
                                      "type": "bad_gateway"}},
                )
            await gate.resp.aclose()
            chat = gemini_response_to_chat(payload, model=upstream_model)
            if protocol == "anthropic":
                from ..protocols.anthropic_adapter import chat_completion_to_anthropic_message

                chat = chat_completion_to_anthropic_message(chat, original)
            return JSONResponse(content=chat)

        # 全部账号失败：报错里逐账号说明当前状态（谁在额度冷却还剩几分钟、
        # 谁疑似被拉黑）——不然用户看到「两个账号明明能用」却报 403，没法自查。
        report = failover.cooldown_report()
        tail = f"最后错误 HTTP {last_status or 'n/a'}{(': ' + last_detail) if last_detail else ''}"
        raise HTTPException(
            status_code=last_status if last_status in (401, 403, 429, 504) else 502,
            detail={"error": {
                "message": (f"antigravity 所有账号均不可用（{report}；{tail}）" if report
                            else f"antigravity 所有账号均不可用（{tail}）"),
                "type": ("timeout" if last_status == 504
                         else "rate_limit_error" if last_status in (403, 429)
                         else "bad_gateway"),
            }},
        )

    async def _gate_first_event(self, resp: httpx.Response, stream: bool) -> "_Gate":
        """压住第一个上游事件再决定透传还是换号。

        非流式：全量解析 JSON，``error.code ∈ {429,403}`` 视为账号级错误
        （此时一个字节都没出网，换号绝对安全）。流式：缓冲原始 SSE 行直到
        第一条能定性的事件——见 candidates 即语义已至（committed，缓冲行随
        透传补放，客户端无损）；error 节点按 code 分类；只有 usageMetadata
        则继续等；语义事件前断流/EOF 按 eof 处理（换号）。
        """
        if not stream:
            try:
                payload = resp.json()
            except Exception:
                return _Gate(resp=resp, committed=True, payload=None)
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, dict):
                code = int(err.get("code") or 0)
                if code in (429, 403):
                    return _Gate(resp=resp, account_error=True, code=code,
                                 message=str(err.get("message") or ""))
            return _Gate(resp=resp, committed=True,
                         payload=payload if isinstance(payload, dict) else None)

        buffered: list[str] = []
        # httpx 响应流一次性消费：迭代器只创建这一次，所有 return 都把它
        # 带上（透传时 _ReplayStream 续跑、换号时 _drain_and_close 排空）。
        lines = resp.aiter_lines()
        try:
            async for line in lines:
                buffered.append(line)
                if len(buffered) > _GATE_BUFFER_MAX_LINES:
                    # 上游一直发非语义事件（心跳/注释刷屏）：按已到达透传放行，
                    # 别让每个请求无界攒内存；定性交给转换器
                    return _Gate(resp=resp, committed=True, buffered=buffered, lines=lines)
                stripped = line.strip()
                if not stripped.startswith("data:"):
                    continue
                data = stripped[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                inner = payload.get("response") if isinstance(payload.get("response"), dict) else payload
                if not isinstance(inner, dict):
                    continue
                err = inner.get("error")
                if isinstance(err, dict):
                    code = int(err.get("code") or 0)
                    if code in (429, 403):
                        return _Gate(resp=resp, account_error=True, code=code,
                                     message=str(err.get("message") or ""), lines=lines)
                    # 其它带内错误（400 等）：换号没意义，缓冲行随透传交给转换器
                    return _Gate(resp=resp, committed=True, buffered=buffered, lines=lines)
                if inner.get("candidates"):
                    return _Gate(resp=resp, committed=True, buffered=buffered, lines=lines)
                # 仅 usageMetadata 等非语义事件：继续等下一条
        except httpx.TimeoutException:
            # 首事件前读超时（上游排队/挂死）：与 EOF 同为「没出字节可换号」，
            # 但带 timed_out 标记让调用方短冷却——挂死账号别每轮都被首选。
            return _Gate(resp=resp, eof=True, timed_out=True, lines=lines)
        except httpx.HTTPError:
            return _Gate(resp=resp, eof=True, lines=lines)
        return _Gate(resp=resp, eof=True, lines=lines)  # 语义事件前 EOF：假成功，换号

    # ---- 内部 ----

    async def _send_with_fallback(
        self,
        client: httpx.AsyncClient,
        method: str,
        body: dict[str, Any],
        headers: dict[str, str],
        stream: bool,
    ) -> httpx.Response:
        """请求级 fallback：daily 失败（网络/超时/5xx/404）换 prod；业务 4xx 不换。

        超时按请求形态分级（``_TIMEOUT_STREAM``/``_TIMEOUT_NONSTREAM``），
        全部 endpoint 都拿不到 HTTP 响应时抛 :class:`_UpstreamUnavailable`
        让账号循环换号——不在这里直接对客户端收场。
        """
        last: httpx.Response | None = None
        timeout = _TIMEOUT_STREAM if stream else _TIMEOUT_NONSTREAM
        for i, base in enumerate(ENDPOINTS):
            url = f"{base}/v1internal:{method}"
            if stream:
                url += "?alt=sse"
            req = client.build_request("POST", url, json=body, headers=headers,
                                       timeout=timeout)
            try:
                resp = await client.send(req, stream=stream)
            except httpx.TimeoutException as exc:
                log.warning("antigravity upstream timeout (%s): %s", base, exc)
                if i + 1 < len(ENDPOINTS):
                    continue
                raise _UpstreamUnavailable(f"upstream timeout（{base.rsplit('//', 1)[-1].split('.')[0]}）") from exc
            except httpx.HTTPError as exc:
                log.warning("antigravity upstream error (%s): %s", base, exc)
                if i + 1 < len(ENDPOINTS):
                    continue
                raise _UpstreamUnavailable(f"upstream error: {exc}") from exc
            # 5xx/404：端点侧问题，试下一个；其余（401/429/4xx）直接返回
            if resp.status_code in (404, 500, 502, 503, 504) and i + 1 < len(ENDPOINTS):
                if stream:
                    await resp.aread()
                await resp.aclose()
                last = resp
                continue
            return resp
        return last  # pragma: no cover - ENDPOINTS 非空，上面必然 return/raise

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client

    def health(self) -> dict[str, Any]:
        """健康概览（纯本地不触网）：顶层字段保持主账号（#1）语义兼容旧前端，
        多账号概览放 ``accounts``（不含 token 等秘密）。"""
        accounts = list_accounts()
        cred = (load_account_cred(accounts[0].id) if accounts else None) or {}
        return {
            "id": self.id,
            "name": self.name,
            "configured": bool(accounts),
            "email": str(cred.get("email") or ""),
            "project_id": str(cred.get("project_id") or ""),
            "tier": str(cred.get("tier") or ""),
            "models": [str(m["id"]) for m in self._models],
            "accounts": [
                {
                    "id": a.id,
                    "email": a.email or a.id,
                    "project_id": str((load_account_cred(a.id) or {}).get("project_id") or ""),
                }
                for a in accounts
            ],
        }

    def quota(self) -> dict[str, Any] | None:
        """免费额度：fetchAvailableModels 带各模型剩余比例（可画真进度条）。

        同一组（gemini / claude-gpt）内模型共享 weekly + 5h 双池；上游把
        ``quotaInfo.remainingFraction``（0~1，两池取当前的合成水位）记在
        每个模型变体上（gemini 系按 effort 后缀分条）。这里按 upstream 名
        前缀归模型、按组聚合，组内取最小值代表该组水位。展示约定与前端
        ``quotaItemHtml`` 对齐：``percent`` 是**已用**百分比（进度条语义）、
        ``remaining/total`` 用千分制（0.9987 → 998.7/1000，小数看着直观）。

        多账号：并发查（常驻线程池 min(4,n) + 整轮 deadline 兜底），每账号
        两条（Gemini 组 / Claude+GPT 组），label 带 ``AG #N · `` 前缀供前端
        分组；个别账号失败不拖垮整页，插 ``query_failed`` 说明条（benefits
        层认这个标记走短缓存，网络恢复后尽快自愈）。拿不到退化为静态说明，
        不画假进度条。
        """
        accounts = list_accounts()
        if not accounts:
            return None
        multi = len(accounts) > 1

        if multi:
            pool = _quota_executor()
            futures = [pool.submit(self._quota_one, a, i + 1, multi=True)
                       for i, a in enumerate(accounts)]
            deadline = time.monotonic() + _QUOTA_ROUND_DEADLINE_S
            items: list[dict[str, Any]] = []
            failures = 0
            for fut in futures:  # 按 failover 顺位收集，UI 顺序稳定
                try:
                    its, ok = fut.result(timeout=max(deadline - time.monotonic(), 0.05))
                except Exception as exc:  # noqa: BLE001 - 超时/异常账号都算失败
                    # 不能静默：2026-10-03 重启后「2/2 个账号查询失败」30s 自愈，
                    # 日志里却一片空白（except 吞了），用户问起无从查起。
                    log.warning("antigravity quota 账号采集失败: %s: %s",
                                type(exc).__name__, exc)
                    its, ok = [], False
                if ok:
                    items.extend(its)
                else:
                    failures += 1
            if failures:
                items.insert(0, _quota_failure_notice(failures, len(accounts)))
        else:
            its, ok = self._quota_one(accounts[0], 1, multi=False)
            items = its if (ok and its) else [{
                "label": _QUOTA_NOTE,
                "used": None,
                "total": None,
                "remaining": None,
                "percent": None,
                "reset_ts": None,
                "expire_ts": None,
                "unit": None,
            }]
        cred = (load_account_cred(accounts[0].id) if accounts else None) or {}
        return {
            "items": items,
            "level": str(cred.get("tier_name") or cred.get("tier") or "free-tier"),
        }

    def quota_epoch(self) -> str:
        """quota 缓存代：账号列表一变（登录新号/删号/换顺位）旧快照就该作废。

        benefits 层的 quota 缓存键只有 provider id（TTL 300s）——加了新账号
        后，旧的单账号快照还会在缓存里顶满 5 分钟，前端看到的还是「一份额度」，
        看起来就像多账号被合并了（2026-10-03 实测）。键里带上账号指纹后，
        账号列表一变键就变，旧缓存自然失效（读的是本地 index，纯内存级开销）。
        """
        try:
            accts = list_accounts()
        except Exception:  # noqa: BLE001 - 拿不到就退回常量键，宁可多查不强撑旧值
            return "unknown"
        return ",".join(f"{a.id}#{a.priority}" for a in accts) or "empty"

    def _quota_one(
        self, acct: Any, index: int, *, multi: bool
    ) -> tuple[list[dict[str, Any]], bool]:
        """单账号额度查询：成功返回 (items, True)，失败 ([], False)。"""
        try:
            data = _run_sync(lambda: self._fetch_available_models(acct.id))
        except Exception as exc:  # noqa: BLE001 - quota 展示失败不影响主链路
            log.warning("antigravity quota 账号 %s（#%s）查询失败: %s: %s",
                        getattr(acct, "id", "?"), index, type(exc).__name__, exc)
            return [], False
        if not (isinstance(data, dict) and isinstance(data.get("models"), dict)):
            return [], False
        prefix = f"AG #{index} · " if multi else ""
        return self._quota_items_from(data, prefix), bool(data["models"])

    def _quota_items_from(self, data: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
        """fetchAvailableModels 响应 → 按组聚合的额度条目（prefix 拼在 label 前）。

        组内共享 5 小时 + 每周两个额度池，上游对组内每个模型变体只回**同一个**
        ``remainingFraction``（满额时恒为 1，且只在逼近池上限时才下调），没有分池
        字段、也没有任何接口能查到每周池的剩余（``retrieveUserQuota`` 返回同一份
        数据，``fetchUserStatus``/``fetchCredits``/``getUserQuota`` 均 404）。
        所以这里：

        * 组内取**最紧水位**（min）作为该组读数，并把「谁最紧」记进 note——
          满额时全组都是 1，此时不点名也无妨；
        * ``reset_ts`` 取组内任一**带 resetTime** 的模型的值（它只是下一个 5 小时
          窗口的滚动刷新点，不代表整组重置、更不代表每周池）——**不强制取最紧那个**：
          resetTime 在做标题行信息展示，因为「最紧的那个恰好没带 resetTime」就让
          整组不显示，是信息损失。
        * ``used`` 由 ``1 - frac`` 反推成千分制，与 ``remaining/total`` 同量纲：
          上游只给剩余，这是唯一诚实的「已用」口径（此前留 None，前端走「已用未知」
          分支，满额被渲染成空进度条，用户读成「额度是 0」）。
        """
        upstream = data["models"]
        # group -> [(frac, reset_epoch, upstream_model_name)]
        by_group: dict[str, list[tuple[float, float, str]]] = {}
        for m in self._models:
            base = str(m.get("upstream") or m["id"])
            fracs: list[tuple[float, float, str]] = []
            for key, q in upstream.items():
                if key != base and not key.startswith(f"{base}-"):
                    continue  # 变体名（-low/-medium/-high/-tiered）也算这个模型的
                if not isinstance(q, dict):
                    continue
                info = q.get("quotaInfo") or {}
                frac = info.get("remainingFraction")
                if not isinstance(frac, (int, float)):
                    continue
                fracs.append((float(frac), _iso_to_epoch(info.get("resetTime")), key))
            if fracs:
                by_group.setdefault(str(m.get("group") or "gemini"), []).extend(fracs)
        items: list[dict[str, Any]] = []
        for group, fracs in by_group.items():
            label = "Gemini 组" if group == "gemini" else "Claude/GPT 组"
            worst_frac, worst_reset, worst_model = min(fracs, key=lambda t: t[0])
            # worst_frac 钳到 [0,1]：上游理论上只发 0~1，但发过 >1（观察到的余量）
            # 会让 remaining>total、used 为负、percent 负值，前端渲染成「剩 1,500 /
            # 1,000 · 已用 -50%」。钳一下，让越界值退化成满额/用尽两端。
            frac = min(max(worst_frac, 0.0), 1.0)
            # 组内模型清单（upstream 变体名去 effort 后缀去重），供面板展示「这组管着谁」
            names = sorted({_strip_effort_suffix(n) for _, _, n in fracs})
            # 满额判定与展示口径**同源**：拿 round 后的 remaining 判，别拿原始 frac——
            # 否则 0.99995 会一边显示「剩 1000 / 1000 · 已用 0%」一边说「组内最紧的是 X」，
            # 同一行自相矛盾。比的是「看起来满没满」，所以按看起来的数判。
            remaining = round(frac * 1000, 1)
            full = remaining >= 1000
            tightness = ("满额（组内各模型均为 100%）" if full
                         else f"组内最紧的是 {worst_model}")
            # resetTime 取**该组任意一个带 resetTime 的模型**的值（resetTime 是
            # 「下一个 5 小时窗口刷新点」这类被 UI 当标题行信息展示的东西，用它做
            # 兜底比因为最紧那个恰好没带就让整组不显示更可靠）；再退一步取组内
            # 最紧模型的 resetTime，都没有才 None。
            reset = next((t for _, t, _ in fracs if t > 0), 0) or worst_reset
            items.append({
                # 解释性文字不塞 label（曾把整句塞进来，面板窄卡里 label 挤得日期
                # 换行、右侧「已用 0%」也断行——用户截图骂丑）。挪 note 字段给前端
                # 渲染成说明行 / title。
                "label": f"{prefix}{label}",
                "note": (f"组内包含：{'、'.join(names)}。共享 5 小时 + 每周双池，"
                         f"按 token 成本比例消耗；上游只在逼近池上限时才下调读数，"
                         f"此处取组内最紧水位，{tightness}"),
                "models_in_group": names,
                "used": round((1 - frac) * 1000, 1),
                "total": 1000,
                "remaining": remaining,
                "percent": round((1 - frac) * 100, 2),  # 前端 percent=已用
                # weekly/5h 是周期重置，不是权益到期：不给 expire_ts，
                # 否则「5 小时后重置」会被到期横幅误报成「5 小时后到期」
                "reset_ts": reset or None,
                # resetTime 只是下一个 5 小时窗口的滚动刷新点——前端把它拼进
                # reset 文案的 title，免得用户当成「整个池子到此重置」
                "reset_note": "下一个 5 小时窗口刷新点（滚动），不代表每周池重置",
                "expire_ts": None,
                "unit": "permille",
            })
        return items

    async def _fetch_available_models(self, account_id: str) -> dict[str, Any]:
        """POST /v1internal:fetchAvailableModels（带各模型配额剩余）。

        用一次性 client：本方法经 ``_run_sync`` 在临时事件循环里跑（/ui 线程），
        复用缓存的 ``self._client`` 会把连接池绑到这个短命 loop 上——loop 关闭
        后主循环的转发请求复用它就 ``RuntimeError: Event loop is closed``
        （未捕获 → internal error）。quota 是低频管理操作，新建开销可忽略。
        """
        from .fingerprint import auth_headers

        token, _cred = await asyncio.to_thread(ensure_account_token, account_id)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await self._send_with_fallback(
                client, "fetchAvailableModels", {}, auth_headers(token), stream=False
            )
            try:
                if resp.status_code >= 400:
                    return {}
                return resp.json()
            finally:
                await resp.aclose()


# ---------------------------------------------------------------------------
# failover 支撑：token 错误转 HTTP / 闸门类型 / 流适配
# ---------------------------------------------------------------------------

def _primary_token() -> str:
    """主账号 token（quota/health 这类管理面用，不走 failover）。"""
    accounts = list_accounts()
    if not accounts:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "antigravity 未登录", "type": "authentication_error"}},
        )
    try:
        return ensure_account_token(accounts[0].id)[0]
    except AuthError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": str(exc), "type": "authentication_error"}},
        ) from exc


@dataclass
class _Gate:
    """首事件闸门的判定结果。

    ``lines`` 是闸门持有的**在途行迭代器**（流式判定时创建的那个
    ``resp.aiter_lines()``）。httpx 响应流是一次性消费的：闸门读过之后
    再调 ``resp.aiter_lines()`` 会抛 StreamConsumed，剩余流全丢——所以
    后续「透传剩余流」（_ReplayStream）与「换号前排空」（_drain_and_close）
    都必须续跑这同一个迭代器，不能重新打开。
    """
    resp: httpx.Response
    committed: bool = False  # 已见语义事件：缓冲行必须透传，绝不重放
    buffered: list[str] = field(default_factory=list)  # 闸门期间缓冲的原始 SSE 行
    lines: AsyncIterator[str] | None = None  # 在途行迭代器（流式时非 None）
    payload: dict[str, Any] | None = None  # 非流式：解析好的 JSON
    account_error: bool = False  # 429/403 账号级错误：冷却换号
    code: int = 0
    message: str = ""
    eof: bool = False  # 语义事件前断流/EOF：假成功，换号
    timed_out: bool = False  # eof 的细分：读超时（区别于干净 EOF，调用方要短冷却）


class _ReplayStream:
    """闸门缓冲行 → 真实流的适配器（先补放缓冲，再接原流）。

    gemini 转换器只用 ``aiter_lines()``/``aclose()``，实现这两个就够了，
    转换器零改动。``lines`` 为闸门的在途迭代器（见 _Gate）：续跑它而不是
    重开 ``resp.aiter_lines()``，否则缓冲行往后的整条流会被 httpx 判为
    二次消费（#67 预存 bug：claude 流式因此完全空流）。
    """

    def __init__(self, resp: httpx.Response, buffered: list[str],
                 lines: AsyncIterator[str] | None = None) -> None:
        self._resp = resp
        self._buffered = list(buffered)
        self._lines = lines

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._buffered:
            yield line
        if self._lines is not None:
            async for line in self._lines:
                yield line
        # 兜底分支仅在未传 lines 时走到——此时响应流还没被消费过，直接
        # 重开是安全的（闸门流式路径必然带 lines，正常不经过这里）。
        else:  # pragma: no cover
            async for line in self._resp.aiter_lines():
                yield line

    async def aclose(self) -> None:
        await self._resp.aclose()


async def _drain_and_close(resp: httpx.Response, stream: bool,
                           lines: AsyncIterator[str] | None = None) -> None:
    """读完丢弃响应体并关闭（换号前必须回收连接，别挂着半开流）。

    ``lines`` 给出时（闸门已消费过响应）续跑迭代器排空；否则才 ``aread()``
    ——对已消费的响应调 aread 会抛 StreamConsumed（异常在 finally 里 aclose
    之前炸出，换号路径被它带崩）。
    """
    try:
        if stream:
            try:
                if lines is not None:
                    async for _ in lines:
                        pass
                elif not resp.is_stream_consumed:
                    await resp.aread()
            except (httpx.HTTPError, httpx.StreamError):
                pass  # 反正是丢弃：排空途中断网不该把换号路径带崩
    finally:
        await resp.aclose()


async def _error_detail(resp: httpx.Response, stream: bool) -> str:
    """从错误响应体抽 ``error.message``（只用于日志/客户端报错文案）。"""
    try:
        if stream:
            await resp.aread()
        payload = resp.json()
    except Exception:  # noqa: BLE001 - 文案而已，拿不到就空着
        return ""
    err = payload.get("error") if isinstance(payload, dict) else None
    return str(err.get("message") or "") if isinstance(err, dict) else ""


# ---------------------------------------------------------------------------
# 冒烟自测：python -m buddy_proxy.antigravity.provider
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    p = AntigravityProvider()
    print("health:", json.dumps(p.health(), ensure_ascii=False))
    if not has_cred():
        print("未登录：先跑 buddy login antigravity")
        raise SystemExit(1)
    resp = asyncio.run(p.forward(
        {
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "只回复两个字：pong"}],
            "stream": False,
            "max_tokens": 32,
        },
        "openai",
    ))
    print("status:", resp.status_code)
    print(str(resp.body)[:400])
