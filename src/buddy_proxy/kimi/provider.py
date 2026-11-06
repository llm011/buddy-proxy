"""Kimi provider —— Kimi Code 订阅通道转发（OpenAI chat-completions 兼容直通）。

上游本身就是 OpenAI 形态（``{base_url}/v1/chat/completions``），所以请求/
响应以直通为骨架（mimo 同款）；差异：

- 多账号 failover：按登录/导入顺序主备降级（见 failover.py），429/403/凭据
  失效冷却当前账号换下一个；首个语义事件到达后绝不重放（防重复计费）。
- thinking：这批模型全是 thinking-only（关不掉），入参 ``reasoning_effort``
  经 ``map_thinking`` 折成 extra body ``{"thinking": {...}}``，不传则上游
  默认 max。
- ``reasoning_content`` 透传：openai/responses 客户端原样拿到（DeepSeek
  风格扩展）；anthropic 客户端由 ``AnthropicStreamConverter`` 转成 thinking
  块（流式）/ ``chat_completion_to_anthropic_message``（非流式）。
- quota：``/v1/usages`` 的 5h + 7d 双池，多账号并发查；未登录返回静态说明
  条（返回 None 会让面板整块隐藏，导入入口就没了）。

认证：OAuth Device Flow 或导入 kimi cli 的 token JSON（见 login.py），
access_token 15 分钟过期自动刷新。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..providers.base import BaseProvider
from ..core.account_store import quota_epoch_from
from ..core.metrics import ACCOUNT_META
from . import failover
from .credentials import (
    AuthError,
    ensure_account_token,
    has_cred,
    list_accounts,
    load_account_cred,
)
from .upstream import (
    build_upstream_body,
    device_headers,
    fetch_models,
    fetch_usages,
    normalize_base_url,
    usages_to_items,
)

log = logging.getLogger(__name__)

#: 流式：read 是「相邻两次读」的上限——首事件前（闸门在等）与流中卡死都按它断。
#: kimi 这批模型默认 max 思考力度，首 token 可能要等上一两分钟，比 antigravity
#: 的 90s 放宽到 240s。
_TIMEOUT_STREAM = httpx.Timeout(connect=15.0, read=240.0, write=60.0, pool=15.0)
#: 非流式：一次 read 拿到全部响应 = 总时长上限。
_TIMEOUT_NONSTREAM = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)
#: 兼容旧名（health/一次性 client 等处仍引用）。
_TIMEOUT = _TIMEOUT_NONSTREAM
#: failover 循环的**尝试期**总预算（从进循环到每次尝试开始前检查）：kimi 首
#: token 慢，预算放宽到 9 分钟（最坏两轮 240s 等待 + 刷新开销）。只挡「还没
#: 开始试」的尝试——已提交的流想跑多久跑多久（流中卡死由 read 超时管）。
_ATTEMPT_DEADLINE_S = 540.0
#: 流式首事件闸门的缓冲行上限（防异常上游无界攒内存，见 _gate_first_event）。
_GATE_BUFFER_MAX_LINES = 256

#: 模型表放 JSON（models.json，随包分发）：实测要增删模型改文件就行。
_MODELS_JSON = Path(__file__).with_name("models.json")

#: models.json 损坏时的内置兜底（保持通道可用，id 与上游一致）。
_FALLBACK_MODELS = [{"id": "kimi-for-coding", "description": "Kimi K2.8 Preview (fallback)"}]


def _load_models() -> list[dict[str, Any]]:
    try:
        data = json.loads(_MODELS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("kimi models.json 读取失败（%s），使用内置兜底表", exc)
        return [dict(m) for m in _FALLBACK_MODELS]
    return [m for m in data.get("models") or [] if isinstance(m, dict) and m.get("id")] or [
        dict(m) for m in _FALLBACK_MODELS
    ]


MODELS: list[dict[str, Any]] = _load_models()
DEFAULT_MODELS: dict[str, str] = {m["id"]: str(m.get("description") or m["id"]) for m in MODELS}

_QUOTA_NOTE = "Kimi Code 订阅：5 小时窗口 + 7 天池双额度（/v1/usages）"
_QUOTA_LOGIN_NOTE = "Kimi 未登录：跑 `buddy login kimi`，或在管理面板「导入账号」粘贴 kimi cli 导出的 token JSON"

#: 多账号额度并发查询：整轮 deadline + 常驻线程池（antigravity 同款，理由见
#: trae/pat/quota.py：每轮新建池会让慢轮线程后台累积；常驻池上限封顶自然排队）。
_QUOTA_ROUND_DEADLINE_S = 8.0
_QUOTA_WORKERS = 4
_quota_pool: "concurrent.futures.ThreadPoolExecutor | None" = None
_quota_pool_lock = threading.Lock()


def _quota_executor() -> "concurrent.futures.ThreadPoolExecutor":
    global _quota_pool
    with _quota_pool_lock:
        if _quota_pool is None:
            _quota_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=_QUOTA_WORKERS, thread_name_prefix="kimi-quota")
        return _quota_pool


def _quota_failure_notice(failed: list[str], total: int) -> dict[str, Any]:
    """部分账号额度**查不通**时的说明条（UI 警告色；benefits 认 query_failed 走短缓存）。

    ``failed`` 是失败账号的展示名（顺位标号或 nickname），列出来而不是只给
    个「N/M」——用户得知道是哪个号、去删还是去重登。措辞只说「取不到额度」，
    不写「网络或凭据问题」：这里分不清是 401 凭据失效还是真的断网，笼统断言
    反而把人支向错的方向；前面的分诊（401 强刷）已经试过了。
    """
    who = "、".join(failed)
    return {
        "label": "Kimi 额度查询失败",
        "used": None,
        "total": None,
        "remaining": f"{len(failed)}/{total} 个账号取不到额度（{who}）",
        "percent": None,
        "reset_ts": None,
        "query_failed": True,
    }


def _quota_empty_notice(multi: bool, names: list[str]) -> dict[str, Any]:
    """账号查通但 ``/usages`` 无分桶数据时的说明条（**不是失败**，走 info 色）。

    实测 Free 层（``user_level_name: "Free"``）就是这个形态：OAuth/me/usages
    全 200，但 usages 返回空 ``{}``——账号没坏，只是这个账号没有可展示的额度
    分桶。原先把这种情况直接吞掉（无条目、无提示），用户只能看到额度区空着，
    分不清是「账号好但没额度」还是「整个通道挂了」。
    """
    who = "、".join(names)
    return {
        "label": "Kimi 暂无额度数据",
        "used": None,
        "total": None,
        "remaining": (f"{who} 无得分桶额度（多为 Free 层账号：无 Kimi Code 权限，"
                      f"订阅后才有 5 小时/7 天池）" if multi else
                      "该账号无得分桶额度（多为 Free 层：无 Kimi Code 权限，订阅后才有）"),
        "percent": None,
        "reset_ts": None,
        "query_empty": True,
    }


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


def _upstream_error_response(resp: httpx.Response) -> JSONResponse:
    """上游错误透传（状态码与错误体原样，隐藏凭据痕迹）。"""
    try:
        payload = resp.json()
    except Exception:
        payload = {"error": {"message": resp.text[:500], "type": "upstream_error"}}
    return JSONResponse(status_code=resp.status_code, content=payload)


class KimiProvider(BaseProvider):
    #: /v1/models 里模型前缀即 ``kimi/kimi-for-coding``。
    id = "kimi"
    name = "Kimi (Kimi Code 订阅)"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # 每个 provider 实例自持运行时目录；模块级 MODELS 只作冷启动种子，刷新
        # 不能污染其它实例（尤其测试/多 app 场景）。
        self._models: list[dict[str, Any]] = [dict(m) for m in MODELS]

    # ---- BaseProvider 接口 ----

    def models(self) -> Sequence[dict[str, Any]]:
        return [
            {
                "id": m["id"],
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "description": str(
                    m.get("description") or m.get("display_name") or m.get("name") or m["id"]),
            }
            for m in self._models
        ]

    async def refresh_models(self, force: bool = False) -> list[dict[str, Any]]:
        """逐全量账号强拉官方 ``/v1/models``，成功结果取并集。

        ``force`` 为统一刷新接口保留；Kimi 当前没有 TTL 缓存，每次调用都请求
        上游。单账号失败只记日志，部分成功时只增不删，保留失败账号可能独有的
        上一版条目；只有全账号成功才允许确认下架。整轮没有一个成功账号则抛错且
        不改旧目录。本地已有条目按 id 合并，以保留 description/default/verified
        等 metadata；上游新 id 则采用 fetch_models 归一出的字段。
        """
        del force
        # 冷却只影响聊天选号，不影响目录发现；否则冷却账号独有模型会被静默下架。
        accounts = list_accounts()
        if not accounts:
            raise RuntimeError("Kimi 没有可用于刷新模型目录的账号")

        discovered: dict[str, dict[str, Any]] = {}
        succeeded = 0
        errors: list[str] = []
        for acct in accounts:
            try:
                token, cred = await asyncio.to_thread(ensure_account_token, acct.id)
                entries = await asyncio.to_thread(
                    fetch_models, str(cred.get("base_url") or ""), token, cred)
            except Exception as exc:  # noqa: BLE001 - 单账号失败不阻断并集刷新
                errors.append(f"{acct.id}: {exc}")
                log.warning("kimi 模型目录刷新跳过账号 %s: %s", acct.id, exc)
                continue
            succeeded += 1
            for entry in entries:
                model_id = str(entry.get("id") or "").strip()
                if model_id:
                    discovered.setdefault(model_id, entry)

        if not succeeded:
            detail = "; ".join(errors) or "无账号成功"
            raise RuntimeError(f"Kimi 模型目录刷新全部失败：{detail}")
        if not discovered:
            # fetch_models 已拒绝空目录；这里防测试替身或未来实现违反契约。
            raise RuntimeError("Kimi 模型目录刷新全部成功响应均为空")

        if succeeded < len(accounts):
            # 部分账号失败时无法证明旧 id 已从所有账号下架；本轮只允许新增/更新。
            for local in self._models:
                model_id = str(local.get("id") or "").strip()
                if model_id:
                    discovered.setdefault(model_id, local)

        local_by_id = {str(m.get("id")): m for m in self._models if m.get("id")}
        merged: list[dict[str, Any]] = []
        for model_id, remote in discovered.items():
            local = local_by_id.get(model_id)
            if local is None:
                merged.append(dict(remote))
                continue
            # 已知 id 以本地完整条目为底，只补上游新增字段；尤其不让归一阶段
            # 合成的 description 覆盖 models.json 里更丰富的人工 metadata。
            item = dict(local)
            for key, value in remote.items():
                if key not in item:
                    item[key] = value
            merged.append(item)
        self._models = merged
        return list(self.models())

    def ensure_auth(self) -> None:
        if not has_cred():
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": ("kimi 未登录：请先运行 `buddy login kimi`，"
                                    "或在管理面板「导入账号」粘贴 kimi cli 导出的 token JSON"),
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
        # 只路由当前实例目录明确命中的 id。刷新后新模型会立即进入此表；未知
        # 模型不能再静默改写成默认模型，否则请求看似成功却实际跑了别的模型。
        entry = next((m for m in self._models if str(m.get("id")) == model), None)
        if entry is None:
            raise HTTPException(
                status_code=400,
                detail={"error": {
                    "message": f"kimi 未知模型: {model}",
                    "type": "invalid_request_error",
                }},
            )
        upstream_model = str(entry["id"])

        # failover 循环：按顺位尝试可用账号（429/403/凭据失效 → 冷却换号）
        accounts = failover.available_accounts()
        if not accounts:
            raise HTTPException(
                status_code=429,
                detail={"error": {
                    "message": (f"kimi 所有账号均在冷却中：{failover.cooldown_report()}"
                                "；额度冷却到点自动恢复"),
                    "type": "rate_limit_error",
                }},
            )

        meta = ACCOUNT_META.get()  # metrics 账号归属（_instrument 放入的 dict）
        client = await self._get_client()
        started = time.monotonic()
        tried = 0
        last_status = 0
        last_detail = ""

        for acct in accounts:
            # 尝试期预算：前面账号的超时/失败把时间烧完后不再开新尝试（首个
            # 账号不受预算挡——预算是给「换号重试」踩刹车的）。
            if tried and time.monotonic() - started > _ATTEMPT_DEADLINE_S:
                last_status = last_status or 504
                last_detail = last_detail or "尝试预算用尽（上游持续无响应）"
                break
            tried += 1
            # token 刷新是同步 urllib；丢线程池避免卡事件循环。
            # token 与 cred 同源返回：base_url/device_id 从同一份 cred 取，多账号不串号。
            try:
                access_token, cred = await asyncio.to_thread(ensure_account_token, acct.id)
            except AuthError as exc:
                failover.mark_cooldown(acct.id, reason=f"凭据不可用: {exc}")
                continue
            base_url = normalize_base_url(str(cred.get("base_url") or ""))
            if not base_url:
                failover.mark_cooldown(acct.id, reason="cred 缺 base_url")
                continue
            if meta is not None:
                meta["account"] = acct.id

            upstream_body = build_upstream_body(body, model=upstream_model, stream=stream)
            headers = device_headers(cred, access_token=access_token)
            if stream:
                headers["Accept"] = "text/event-stream"
            try:
                req = client.build_request(
                    "POST", f"{base_url}/chat/completions",
                    json=upstream_body,
                    headers=headers,
                    timeout=_TIMEOUT_STREAM if stream else _TIMEOUT_NONSTREAM)
                resp = await client.send(req, stream=stream)
            except httpx.TimeoutException as exc:
                last_status, last_detail = 504, f"upstream timeout: {exc}"
                failover.mark_cooldown(acct.id, reason="请求超时")
                continue
            except httpx.HTTPError as exc:
                last_status, last_detail = 502, f"upstream error: {exc}"
                failover.mark_cooldown(acct.id, reason=f"网络错误: {exc}")
                continue

            # 401：token 被上游拒（refresh_token 被轮换/作废等）——强刷一次重试
            # 同账号；仍拒说明凭据层面失效，冷却换号
            if resp.status_code == 401:
                last_status, last_detail = 401, await _error_detail(resp, stream)
                await _drain_and_close(resp, stream)
                try:
                    access_token, cred = await asyncio.to_thread(
                        ensure_account_token, acct.id, force_refresh=True)
                except AuthError as exc:
                    failover.mark_cooldown(acct.id, reason=f"强刷失败: {exc}")
                    continue
                headers = device_headers(cred, access_token=access_token)
                if stream:
                    headers["Accept"] = "text/event-stream"
                try:
                    req = client.build_request(
                        "POST", f"{base_url}/chat/completions",
                        json=upstream_body,
                        headers=headers,
                        timeout=_TIMEOUT_STREAM if stream else _TIMEOUT_NONSTREAM)
                    resp = await client.send(req, stream=stream)
                except httpx.TimeoutException:
                    last_status, last_detail = 504, "upstream timeout（强刷重试）"
                    failover.mark_cooldown(acct.id, reason="强刷重试超时")
                    continue
                except httpx.HTTPError as exc:
                    last_status, last_detail = 502, f"upstream error: {exc}"
                    failover.mark_cooldown(acct.id, reason=f"网络错误: {exc}")
                    continue
                if resp.status_code == 401:
                    last_status, last_detail = 401, await _error_detail(resp, stream)
                    await _drain_and_close(resp, stream)
                    failover.mark_cooldown(acct.id, reason="401 强刷后仍被拒（refresh_token 可能已失效，请重新导入）")
                    continue

            # 403/429：账号级拒绝（额度/风控）——冷却该账号换下一个
            if resp.status_code in (403, 429):
                last_status = resp.status_code
                last_detail = await _error_detail(resp, stream)
                failover.mark_cooldown(
                    acct.id,
                    retry_after=resp.headers.get("Retry-After"),
                    quota=resp.status_code == 429,
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
            gate = await _gate_first_event(resp, stream)
            if gate.account_error:
                last_status, last_detail = gate.code, gate.message
                failover.mark_cooldown(
                    acct.id, quota=gate.code == 429,
                    reason=f"带内 error {gate.code}: {gate.message}")
                await _drain_and_close(gate.resp, stream, gate.lines)
                continue
            if gate.eof:
                # 语义事件之前断流/空流：没向客户端吐过字节，换下一个账号。
                # 读超时（首事件前卡死）额外短冷却——挂死账号别每轮都被首选；
                # 干净 EOF 不冷却（可能只是网络抖动）。
                if gate.timed_out:
                    last_status, last_detail = 504, "首事件前读超时"
                    failover.mark_cooldown(acct.id, reason="首事件前读超时")
                else:
                    # 不设的话会落到「最后错误 HTTP n/a」的 502——用户看不出是
                    # 上游断流还是网关坏了（token 过期这类可行动信息全被吞）。
                    last_status, last_detail = 502, "首事件前断流（上游空响应）"
                await _drain_and_close(gate.resp, stream, gate.lines)
                continue

            if stream:
                replay = _ReplayStream(gate.resp, gate.buffered, gate.lines)
                if protocol == "anthropic":
                    return StreamingResponse(
                        _to_anthropic_stream(replay, upstream_model),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "close"},
                    )
                # openai/responses 直通：闸门按行消费过流，经 _pass_through_lines
                # 续跑行迭代器补放缓冲行——绝不能重开 aiter_bytes()（#72 同坑）
                return StreamingResponse(
                    _pass_through_lines(replay),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "close"},
                )

            payload = gate.payload
            if payload is None:
                await gate.resp.aclose()
                raise HTTPException(
                    status_code=502,
                    detail={"error": {"message": "kimi upstream returned non-JSON",
                                      "type": "bad_gateway"}},
                )
            await gate.resp.aclose()
            if protocol == "anthropic":
                from ..protocols.anthropic_adapter import chat_completion_to_anthropic_message

                payload = chat_completion_to_anthropic_message(payload, original)
            return JSONResponse(content=payload)

        # 全部账号失败：报错里逐账号说明当前状态（谁在冷却还剩几分钟）
        report = failover.cooldown_report()
        tail = f"最后错误 HTTP {last_status or 'n/a'}{(': ' + last_detail) if last_detail else ''}"
        raise HTTPException(
            status_code=last_status if last_status in (401, 403, 429, 504) else 502,
            detail={"error": {
                "message": (f"kimi 所有账号均不可用（{report}；{tail}）" if report
                            else f"kimi 所有账号均不可用（{tail}）"),
                "type": ("timeout" if last_status == 504
                         else "rate_limit_error" if last_status in (403, 429)
                         else "bad_gateway"),
            }},
        )

    # ---- 内部 ----

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
            "nickname": str(cred.get("nickname") or ""),
            "user_id": str(cred.get("user_id") or ""),
            "base_url": str(cred.get("base_url") or ""),
            "models": list(DEFAULT_MODELS),
            "accounts": [
                {
                    "id": a.id,
                    "name": a.name or a.id,
                    "user_id": str((load_account_cred(a.id) or {}).get("user_id") or ""),
                }
                for a in accounts
            ],
        }

    def quota(self) -> dict[str, Any] | None:
        """额度：``/v1/usages`` 的 5 小时窗口 + 7 天池（可画真进度条）。

        多账号：并发查（常驻线程池 + 整轮 deadline 兜底），label 带
        ``Kimi #N · `` 前缀供前端分组；个别账号失败不拖垮整页，插
        ``query_failed`` 说明条（benefits 层认这个标记走短缓存）。**未登录
        也要返回说明条**——返回 None 会让面板把整个通道块隐藏，导入账号的
        入口就没了。
        """
        accounts = list_accounts()
        if not accounts:
            return {
                "items": [_static_notice("Kimi", _QUOTA_LOGIN_NOTE)],
                "level": None,
            }
        multi = len(accounts) > 1

        if multi:
            pool = _quota_executor()
            futures = [pool.submit(self._quota_one, a, i + 1, multi=True)
                       for i, a in enumerate(accounts)]
            deadline = time.monotonic() + _QUOTA_ROUND_DEADLINE_S
            items: list[dict[str, Any]] = []
            failed: list[str] = []
            empty: list[str] = []
            for acct, fut in zip(accounts, futures):  # 按 failover 顺位收集，UI 顺序稳定
                name = f"#{acct.priority + 1}"
                try:
                    its, ok, had_data = fut.result(
                        timeout=max(deadline - time.monotonic(), 0.05))
                except Exception:  # noqa: BLE001 - 超时/异常账号都算失败
                    its, ok, had_data = [], False, False
                if not ok:
                    failed.append(name)
                elif had_data:
                    items.extend(its)
                else:
                    empty.append(name)
            # 查不通（凭据/网络）才是告警；账号好但没分桶数据只是 info——
            # 两者分开计数，别让 Free 层健康号被折进「N/M 失败」里。
            if failed:
                items.insert(0, _quota_failure_notice(failed, len(accounts)))
            if empty:
                items.insert(0, _quota_empty_notice(multi=True, names=empty))
            if not items:
                items = [_static_notice("Kimi", _QUOTA_NOTE)]
        else:
            its, ok, had_data = self._quota_one(accounts[0], 1, multi=False)
            if ok and had_data:
                items = its
            elif ok:
                items = [_quota_empty_notice(multi=False, names=[])]
            else:
                items = [_static_notice("Kimi", _QUOTA_NOTE)]
        cred = (load_account_cred(accounts[0].id) if accounts else None) or {}
        level = str(cred.get("user_level_name") or "").strip()
        return {
            "items": items,
            "level": level or None,  # 别把 str(None)="None" 当 level 发出去
        }

    def quota_epoch(self) -> str:
        """quota 缓存代：账号列表一变（登录新号/删号/换顺位）旧快照就该作废。

        benefits 层的 quota 缓存键只有 provider id（TTL 300s）——加了新账号
        后，旧的单账号快照还会在缓存里顶满 5 分钟（antigravity #71 实测过的
        bug）。键里带上账号指纹后，账号列表一变键就变，旧缓存自然失效。
        """
        return quota_epoch_from(list_accounts)

    def _quota_one(
        self, acct: Any, index: int, *, multi: bool
    ) -> tuple[list[dict[str, Any]], bool, bool]:
        """单账号额度查询：``(items, ok, had_data)``。

        - ``ok=False``：**查不通**（ensure 失败 / 网络错误）——该账号告警；
        - ``ok=True, had_data=False``：查通了但 ``/usages`` 没分桶数据
          （实测 Free 层返回空 ``{}``）——账号是好的，只是没额度可展示，
          由调用方给 info 说明条，**不算失败**（真机踩过：健康号被标失败，
          和真死号一起显示 2/2）。

        同步 urllib 直接跑在常驻线程池里（不走 _run_sync——那是给 asyncio
        上游用的；这里 fetch_usages 本来就是同步调用）。
        """
        try:
            token, cred = ensure_account_token(acct.id)
            data = fetch_usages(str(cred.get("base_url") or ""), token)
        except Exception:  # noqa: BLE001 - quota 展示失败不影响主链路
            return [], False, False
        prefix = f"Kimi #{index} · " if multi else ""
        items = usages_to_items(data, prefix=prefix)
        return items, True, bool(items)


# ---------------------------------------------------------------------------
# 流式支撑：首事件闸门 / 重放流 / anthropic 转换
# ---------------------------------------------------------------------------

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


def _is_account_error(err: dict[str, Any]) -> bool:
    """带内 error 是否属于「账号级」——按 ``type`` 判定，不只看 code。

    真机实测：kimi 的订阅失效是
    ``{"error":{"message":"...access_terminated_error...","type":"access_terminated_error"}}``
    ——**没有 code 字段**（code 恒 0）。只看 code 的话这种带内 403 会被当成
    「已 committed」直接透传给客户端，既不冷却也不换号：每轮 failover 仍先
    选中这个死账号，白付一次完整往返。type 里的 account/subscription/
    permission/terminated 语义都是账号级，请求级错误（模型名不合法、tool
    schema 错）不在此列。
    """
    etype = str(err.get("type") or "").strip().lower()
    return any(k in etype for k in (
        "access_terminated", "account", "subscription", "permission", "quota"))


def _gate_semantic(payload: dict[str, Any]) -> str:
    """OpenAI SSE chunk 定性：``account_error`` / ``semantic`` / ``wait``。

    - 带 ``error`` 节点且 code ∈ {429, 403}：账号级错误（额度/风控）；
    - ``choices[0].delta`` 带 role/content/reasoning_content/tool_calls 或
      ``finish_reason`` 非空：语义已至；
    - 其余（仅 usage、空 choices）：继续等下一条。
    """
    err = payload.get("error")
    if isinstance(err, dict):
        try:
            code = int(err.get("code") or 0)
        except (TypeError, ValueError):
            code = 0
        if code in (429, 403) or _is_account_error(err):
            return "account_error"
        return "semantic"  # 其它带内错误：换号没意义，缓冲行随透传交给转换器
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        delta = first.get("delta")
        if isinstance(delta, dict) and any(
                k in delta for k in ("role", "content", "reasoning_content", "tool_calls")):
            return "semantic"
        if first.get("finish_reason"):
            return "semantic"
    return "wait"


async def _gate_first_event(resp: httpx.Response, stream: bool) -> _Gate:
    """压住第一个上游事件再决定透传还是换号。

    非流式：全量解析 JSON，``error`` 节点 code ∈ {429,403} 视为账号级错误
    （此时一个字节都没出网，换号绝对安全）。流式：缓冲原始 SSE 行直到
    第一条能定性的事件——语义已至即 committed（缓冲行随透传补放，客户端
    无损）；error 节点按 code 分类；只有 usage 则继续等；语义事件前断流/
    EOF 按 eof 处理（换号，防「200 空流假成功」）。
    """
    if not stream:
        try:
            payload = resp.json()
        except Exception:
            return _Gate(resp=resp, committed=True, payload=None)
        verdict = "wait"
        if isinstance(payload, dict):
            verdict = _gate_semantic(payload)
        if verdict == "account_error":
            err = payload.get("error") or {}
            try:
                code = int(err.get("code") or 0)
            except (TypeError, ValueError):
                code = 0
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
            if not data:
                continue
            if data == "[DONE]":
                # 没有任何语义事件就收尾：空流假成功，换号（缓冲行不再透传）
                return _Gate(resp=resp, eof=True, lines=lines)
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            verdict = _gate_semantic(payload)
            if verdict == "account_error":
                err = payload.get("error") or {}
                try:
                    code = int(err.get("code") or 0)
                except (TypeError, ValueError):
                    code = 0
                return _Gate(resp=resp, account_error=True, code=code,
                             message=str(err.get("message") or ""), lines=lines)
            if verdict == "semantic":
                return _Gate(resp=resp, committed=True, buffered=buffered, lines=lines)
            # 仅 usage 等非语义事件：继续等下一条
    except httpx.TimeoutException:
        # 首事件前读超时（上游排队/挂死）：与 EOF 同为「没出字节可换号」，
        # 但带 timed_out 标记让调用方短冷却——挂死账号别每轮都被首选。
        return _Gate(resp=resp, eof=True, timed_out=True, lines=lines)
    except httpx.HTTPError:
        return _Gate(resp=resp, eof=True, lines=lines)
    return _Gate(resp=resp, eof=True, lines=lines)  # 语义事件前 EOF：假成功，换号


class _ReplayStream:
    """闸门缓冲行 → 真实流的适配器（先补放缓冲，再接原流）。

    下游转换器只用 ``aiter_lines()``/``aclose()``，实现这两个就够。
    ``lines`` 为闸门的在途迭代器（见 _Gate）：续跑它而不是重开
    ``resp.aiter_lines()``，否则缓冲行往后的整条流会被 httpx 判为二次消费
    （antigravity #67 / kimi #72 同坑：claude 流式因此完全空流）。
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
        else:  # pragma: no cover - 闸门流式路径必然带 lines，正常不经过这里
            async for line in self._resp.aiter_lines():
                yield line

    async def aclose(self) -> None:
        await self._resp.aclose()


async def _pass_through_lines(replay: _ReplayStream) -> AsyncIterator[bytes]:
    """openai/responses 直通：行迭代器 → 字节流。

    闸门按行消费过响应，原始字节流拿不回来了；标准 SSE 行语义下
    「line + "\\n"」与原始字节等价（事件以单换行结尾、空行分隔事件），
    客户端无感。**不能**重开 ``resp.aiter_bytes()``——流已被判为消费过，
    会抛 StreamConsumed 丢整条流。
    """
    try:
        async for line in replay.aiter_lines():
            yield (line + "\n").encode()
    finally:
        await replay.aclose()


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


async def _iter_openai_chunks(response: Any) -> AsyncIterator[dict[str, Any]]:
    """把上游 OpenAI SSE 解成一个个 chunk dict（供转 Anthropic 事件流）。

    只认 ``data:`` 行；``[DONE]`` 结束；解析不了的片段跳过（上游偶发半包/
    心跳注释行，跳过比整体报错好）。异常/结束时确保释放连接。
    """
    try:
        async for line in response.aiter_lines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(chunk, dict):
                yield chunk
    finally:
        await response.aclose()


async def _to_anthropic_stream(response: Any, model: str) -> AsyncIterator[str]:
    """上游 OpenAI SSE → Anthropic 事件流（``/v1/messages`` 客户端要的形状）。

    kimi 上游没有 Anthropic 原生端点，必须自己把 OpenAI chunk 转成
    ``message_start`` / ``content_block_delta`` / ``message_stop``。转换器
    直接复用 ``anthropic_adapter.AnthropicStreamConverter``（mimo/trae 同款），
    ``reasoning_content`` → thinking 块、``tool_calls`` → tool_use 块的行为
    与它们一致。转换途中任何异常都要**先收尾再抛**（``feed_chunk`` 对畸形
    chunk 会抛），不兜的话客户端拿到的是「内容块悬空、没有 message_stop」
    的残流，比直接报错更难排查。
    """
    from ..protocols.anthropic_adapter import AnthropicStreamConverter

    converter = AnthropicStreamConverter(model)

    def _emit(event_name: str, payload: dict[str, Any]) -> str:
        return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _close_open() -> list[str]:
        """把已开出的内容块收尾（best-effort：收尾本身再炸也不能盖掉原错误）。"""
        try:
            return [_emit(n, p) for n, p in converter.close_open_blocks()]
        except Exception:  # noqa: BLE001
            return []

    def _abort(msg: str) -> list[str]:
        """收尾 + 补一个 error 事件，让客户端拿到结构完整的结束。"""
        out = _close_open()
        out.append(_emit("error", {
            "type": "error",
            "error": {"type": "api_error", "message": msg},
        }))
        return out

    try:
        async for chunk in _iter_openai_chunks(response):
            if chunk.get("error"):
                err = chunk["error"]
                msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
                for event in _abort(msg):
                    yield event
                return
            for name, payload in converter.feed_chunk(chunk):
                yield _emit(name, payload)
        for name, payload in converter.finish():
            yield _emit(name, payload)
    except Exception as exc:  # noqa: BLE001 — 上游畸形数据不该让客户端只收到半截流
        log.warning("kimi anthropic stream aborted: %s: %s", type(exc).__name__, exc)
        for event in _abort(f"{type(exc).__name__}: {exc}"):
            yield event
        return
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# 冒烟自测：python -m buddy_proxy.kimi.provider
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    p = KimiProvider()
    print("health:", json.dumps(p.health(), ensure_ascii=False))
    if not has_cred():
        print("未登录：先跑 buddy login kimi（或导入 kimi cli 的 token JSON）")
        raise SystemExit(1)
    resp = asyncio.run(p.forward(
        {
            "model": "kimi-for-coding",
            "messages": [{"role": "user", "content": "只回复两个字：pong"}],
            "stream": False,
            "max_tokens": 32,
        },
        "openai",
    ))
    print("status:", resp.status_code)
    print(str(resp.body)[:400])
