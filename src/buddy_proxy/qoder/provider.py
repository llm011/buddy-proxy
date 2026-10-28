"""Qoder provider：COSY 面聊天转发（多账号 failover）+ 额度查询。

Qoder 的聊天面（``/algo/.../agent_chat_generation``）与 OpenAI 线缆的差别
只在**外壳**：

- 出站：COSY 签名头 + 自定义编码 body；body 明文里除标准 OpenAI 字段外，
  还要带官方客户端的**用量归因信封**（``request_id`` / ``session_id`` /
  ``chat_context`` / ``model_config`` / ``business``）。实测裸 body 会被
  业务路由拒（``[FAIL]node:agent_router msg:None flow nodes found``），
  带上信封即正常出字。
- 入站：SSE 信封 ``data:{"headers":…,"body":"<内层 JSON 字符串>"}``，
  内层才是标准 OpenAI ``chat.completion.chunk``。另有 ``event:finish``
  尾帧（无 body）与带内错误帧（HTTP 200 但 body 里是 ``{code,message}``）。

多账号 failover 照抄 kimi/antigravity：按登录顺序主备降级，401 强刷、
403（权益门/额度）/429 冷却换号；首个语义事件到达后绝不重放（防重复计费）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from buddy_proxy.core.checkin import SOURCE_UPSTREAM, next_from_window
from buddy_proxy.core.metrics import ACCOUNT_META
from buddy_proxy.providers.base import BaseProvider

from . import failover
from .campaigns import CLAIM_ACTION, CampaignClient
from .catalog import (
    Catalog,
    account_supports,
    is_enabled,
    is_hidden,
    override_accounts,
    override_missing_entries,
    public_model_id,
    to_openai_model,
)
from .config import COSY_VERSION, REGIONS, Region, resolve_region, with_cached_endpoints
from .convert import _normalize_message, _to_anthropic_stream, _unwrap
from .cosy import sign
from .credentials import (
    AccountRef,
    AuthError,
    Credential,
    backfill_identity,
    cred_to_credential,
    ensure_account_token,
    list_accounts,
    load_account_cred,
)
from .errors import _last_user_text, _sse_error, _upstream_error
from .quota import _expire_ts, _pkg_active, _pkg_label, _used_percent
from .umid import machine_identity

log = logging.getLogger(__name__)


#: 归 CodeBuddy 的档位式名字：Qoder 目录里也挂着同名档位（``auto`` 是
#: ``TIER_MODELS`` 合成的档位模型），但这个名字在 CodeBuddy 那边是**默认模型**，
#: 语义上归 CodeBuddy。这些名字两个轮次都让开，只在显式 ``qoder/`` 前缀下
#: 才由本通道服务。
#:
#: 为什么不能全靠 ``_is_codebuddy_model`` 推出来：静态表里还有
#: ``qwen3.8-max`` / ``kimi-k3`` / ``deepseek-v4.1-flash`` / ``glm-5.3``
#: —— 它们同时也是本通道真实发布的模型（同一批权重，两边都有资格），
#: 让开会让这些模型在目录里"消失"。能自动区分的只有「**档位**模型」这一条：
#: ``TIER_MODELS`` 是本地合成的、不是上游目录里的真实模型。
CODEBUDDY_OWNED_IDS: frozenset[str] = frozenset({"auto"})


def _is_codebuddy_owned(name: str) -> bool:
    """该名字是否归 CodeBuddy（本通道让开）。

    只对档位模型（:data:`CODEBUDDY_OWNED_IDS`）成立。大小写不敏感——``auto``
    是档位名、没有大小写语义，``Auto`` / ``AUTO`` 也该让开；而别名轮那道
    ``_is_codebuddy_model`` 守卫刻意做精确比对（否则 ``Qwen3.8-Max`` 这类
    官方显示名会被误挡），漏得过去。
    """
    return name.strip().lower() in CODEBUDDY_OWNED_IDS

#: 出站透传给上游的 OpenAI 字段白名单（其余私有扩展不透传）。
_PASSTHROUGH_FIELDS = (
    "messages",
    "tools",
    "tool_choice",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "reasoning_effort",
    "presence_penalty",
    "frequency_penalty",
    "response_format",
    "seed",
    "user",
    "parallel_tool_calls",
    # 代理自身会写入 messages，但没有谁规定 system 只能在 messages 里。
    # 上游有多个取 system 文本的位置，这里一并透传，避免出现「客户端设了
    # 系统提示词、上游却看不到」的那种「根本没生效」。
    "system",
)

#: 流式：read 是「相邻两次读」的上限——首字节前（边缘收下不回应）与流中卡死都按它断。
_TIMEOUT_STREAM = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0)
#: 整轮流式上限（防止挂死连接长期占用）。
STREAM_TIMEOUT_S = 600.0
#: failover 循环的**尝试期**总预算（从进循环到每次尝试开始前检查）：只挡
#: 「还没开始试」的尝试——已提交的流想跑多久跑多久（流中卡死由 read 超时管）。
_ATTEMPT_DEADLINE_S = 300.0
#: 流式首事件闸门的缓冲行上限（防异常上游无界攒内存）。
_GATE_BUFFER_MAX_LINES = 256

#: 10605（RESOURCE_LIMIT）是上游按 (账号, 模型) 的**周期性排队**，不是额度耗尽：
#: 载荷自带 ``retryAfterSeconds``（实测 2s / 30s 两档）与 ``serviceAvailable``，
#: 同账号等过这段时间就能出流（实测 15s、60s 后复现可用）。所以按它给的时长
#: 原地等待后重放首事件闸门，**不冷却、不换号**——账号是好的，换号也躲不开
#: （实测两个 CN 号同时被挡）。
_QUEUE_WAIT_MAX_ROUNDS = 2
#: 单次等待封顶（防上游给个离谱的大数把请求挂死）。
_QUEUE_WAIT_MAX_S = 60.0

#: 多账号额度并发查询：整轮 deadline + 常驻线程池（kimi 同款，理由见
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
                max_workers=_QUOTA_WORKERS, thread_name_prefix="qoder-quota")
        return _quota_pool


class QoderProvider(BaseProvider):
    """Qoder（Qwen3.8 / DeepSeek / GLM / Kimi …）通道。"""

    id = "qoder"
    name = "Qoder"
    # 每日活动权益（「每天领 100 Credits」）走 /sash 面，见 qoder/campaigns.py。
    supports_checkin = True

    def __init__(self, region: Region | None = None) -> None:
        # 主区域（顶层 health / 无账号时的兜底语义）；实际转发/额度按每账号
        # 自己的 region 取 catalog。
        self._region = with_cached_endpoints(region or resolve_region())
        self._catalogs: dict[str, Catalog] = {}
        self._catalog_lock = threading.Lock()
        # 跨账号目录并集（upstream key -> entry），refresh_models 时重建。
        # 目录是账号级分桶：受限账号只剩 Qwen 两档，只看单账号会把全量账号
        # 的三方模型从列表里抹掉；并集 = 「至少一个账号支持」。
        self._union: dict[str, dict[str, Any]] = {}

    # -- catalog ------------------------------------------------------------

    def _catalog_for(self, region_key: str) -> Catalog:
        """按 region key 取（惰性建）模型目录。"""
        with self._catalog_lock:
            cat = self._catalogs.get(region_key)
            if cat is None:
                reg = REGIONS.get(region_key) or self._region
                cat = Catalog(with_cached_endpoints(reg))
                self._catalogs[region_key] = cat
            return cat

    # -- 认证 ---------------------------------------------------------------

    def region(self) -> Region:
        """主区域（含从桌面端缓存覆盖的端点）。"""
        return self._region

    def ensure_auth(self) -> None:
        """启动时校验至少有一个账号；缺失抛 401。"""
        if not list_accounts():
            raise HTTPException(
                status_code=401,
                detail=("qoder 未登录：请先跑 `buddy login qoder`（浏览器授权）"))

    # -- 模型 ---------------------------------------------------------------

    def _entries(self) -> list[dict[str, Any]]:
        """当前展示/路由用的目录条目：跨账号并集 > 主区域 catalog > 兜底表。"""
        if self._union:
            return list(self._union.values())
        return self._catalog_for(self._region.key)._models or Catalog.fallback()

    def models(self) -> Sequence[dict[str, Any]]:
        """同步返回模型列表（用主区域兜底目录；异步刷新见 ``refresh_models``）。

        两类条目不展示：旧模型（:data:`catalog.HIDDEN_KEYS`）和上游已停用的
        （``enable=false``，:func:`catalog.is_enabled`）。隐藏 ≠ 停用：两类
        直接点名仍可调用（透传上游，由上游裁决），只是不列出来——列表里
        摆着调不通的模型只会让客户端白白选到它（2026-10-03 三方模型整批
        被收回后，/v1/models 仍虚报 7 个 403 模型的教训）。
        """
        return [to_openai_model(e, self.id)
                for e in self._entries() if not is_hidden(e) and is_enabled(e)]

    async def refresh_models(self, force: bool = False) -> list[dict[str, Any]]:
        """逐账号刷新目录取并集（供管理页「刷新模型」与启动预热用）。

        并集而不是 ``accounts[0]`` 的单份：目录是**账号级**分桶，受限账号只剩
        Qwen 两档，拿它当唯一视角会把全量账号的三方模型从列表里抹掉（线上
        「qoder 只剩 2 个模型」的根因）。同区账号共享 Catalog 实例、TTL 缓存
        会把第二份吞掉，故逐账号 ``force=True`` 拉取；单账号失败只收缩并集，
        不拖垮整体。``force`` 形参保留兼容旧调用（内部恒强拉）。
        """
        del force  # 逐账号并集语义下恒强拉（见 docstring）
        accounts = failover.available_accounts(self._region.key)
        union: dict[str, dict[str, Any]] = {}
        for acct in accounts:
            try:
                _, cred_dict = await asyncio.to_thread(ensure_account_token, acct.id)
                cred = cred_to_credential(cred_dict)
                entries = await self._catalog_for(cred.region).fetch(cred, force=True)
            except Exception as exc:  # noqa: BLE001 - 单账号失败不阻断整体刷新
                log.warning("qoder 目录刷新跳过账号 %s: %s", acct.id, exc)
                continue
            for e in entries:
                union.setdefault(str(e.get("key") or ""), e)
        if union:
            # 覆盖表登记、目录暂时缺失的模型补最小条目（保证可显示/可路由）。
            for e in override_missing_entries(list(union.values())):
                union.setdefault(str(e.get("key") or ""), e)
            self._union = union
        entries = list(self._union.values()) if self._union else Catalog.fallback()
        return [to_openai_model(e, self.id)
                for e in entries if not is_hidden(e) and is_enabled(e)]

    def resolve_model(self, model: str) -> str:
        """把显示名/大小写变体归一成上游 key。"""
        return self._catalog_for(self._region.key).resolve_key(model)

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        """目录别名（显示名/大小写变体）也要能被自动路由命中。

        Qoder 的目录 key 是内部代号（``qfmodel``），客户端常直接发显示名
        （``Qwen3.8-Flash``）——只比 id 会漏配，请求掉进兜底通道后被上游拒成
        「模型不存在」。

        ``aliases=False``（路由第一轮）**只认本通道自己发布的 id**（裸名形态，
        即 :data:`catalog.MODEL_IDS` 的值）：``qwen3.8-flash`` / ``glm-5.3`` 等。

        为什么第一轮不能直接调基类 ``accepts_model``：基类把「剥前缀裸名与请求
        名相等」也算精确命中，而 Qoder 的裸名里有 ``auto``、``glm-5.3``、
        ``glm-5.3-flash`` —— 前一个是 CodeBuddy 静态表的默认模型，后两个是 zcode
        按 id 发布的模型名。它们对 Qoder 而言**只是别名**（上游叫 ``gmodel`` /
        ``gfmodel``），让它们在精确轮命中就会把别人的模型抢走：实测 ``auto`` 被
        路由到 qoder 并真的走通了（CodeBuddy 的默认模型静默改道）。所以精确轮
        只比对**本通道 id 集合**，其余一律留给别名轮兜底。
        """
        want = (model or "").strip()
        if not want:
            return False
        # 名字若同时属于 CodeBuddy 静态表（如 ``auto``），**两个轮次都让开**。
        if _is_codebuddy_owned(want):
            return False
        entries = self._entries()
        # 隐藏模型（旧模型）也放进精确集合：它们只是不出现在列表里，点名仍可调。
        exact_ids = {(public_model_id(e) or "").strip() for e in entries}
        exact_ids.discard("")
        if want in exact_ids:
            return True
        if not aliases:
            return False
        # ``resolve_key`` 对「已经是上游 key」与「认不出」两种输入都原样返回，
        # 单看 ``key == want`` 无法区分——只能直接查目录：归一结果确实存在，
        # 才认领；否则（真正的未知模型）放行给别人。
        key = self._catalog_for(self._region.key).resolve_key(want)
        return any(str(m.get("key") or "") == key for m in entries)

    def _account_supports(self, model_key: str, account_id: str) -> bool:
        """该账号是否支持该模型（per-account 覆盖表；未登记 = 全支持）。

        目录里没有的模型（未知名字透传上游裁决的形态）不做账号收窄，一律放行。
        """
        entry = next((e for e in self._entries()
                      if str(e.get("key") or "") == model_key), None)
        if entry is None:
            return True
        return account_supports(entry, account_id)

    def model_support_map(self) -> dict[str, dict[str, Any]]:
        """对外 id -> per-account 支持画像（UI「部分账号」标注用；纯本地不触网）。

        只收窄过的模型才进表：``{"limited": True, "accounts": [支持账号的
        显示名, ...]}``；未登记的模型不出现（= 全账号支持，前端不画 badge）。
        """
        names = {a["id"]: (a.get("alias") or a.get("name") or a["id"])
                 for a in (failover.accounts_status().get("accounts") or [])}
        out: dict[str, dict[str, Any]] = {}
        for e in self._entries():
            allowed = override_accounts(e)
            if allowed is None:
                continue
            public = public_model_id(e) or str(e.get("key") or "")
            out[public] = {
                "limited": True,
                "accounts": sorted(names.get(a, a) for a in allowed),
            }
        return out

    # -- 每日活动权益（打卡） ------------------------------------------------

    async def _campaigns(self, account: AccountRef | None = None) -> CampaignClient:
        """构造指定区域账号的活动面客户端。

        Qoder 签到按账号独立发放；多账号状态需要逐号查询，领取时也不能只看
        顺位第一号，否则第一号没有活动会掩盖后续账号的可领活动。
        """
        accounts = failover.available_accounts(self._region.key)
        if account is None:
            if not accounts:
                raise AuthError("qoder 没有可用账号（未登录或全部冷却中）")
            account = accounts[0]
        elif account.region != self._region.key:
            raise AuthError(f"qoder 活动账号区域不匹配: {account.region}")
        _, cred_dict = await asyncio.to_thread(ensure_account_token, account.id)
        cred = cred_to_credential(cred_dict)
        reg = with_cached_endpoints(resolve_region(cred.region))
        # 全球区活动面按机器指纹定向发放签到活动；指纹在事件循环外生成。
        identity = await asyncio.to_thread(
            machine_identity, reg.key, cred.uid or account.id)
        return CampaignClient(reg, cred, identity)

    async def _campaign_status(self, campaigns: list[Any]) -> dict[str, Any]:
        """把一个账号的活动列表映射为签到状态。"""
        claimable = [c for c in campaigns if c.is_claimable]
        claimed = [c for c in campaigns if c.action_type == CLAIM_ACTION and c.is_claimed]
        today = claimable[0] if claimable else (claimed[0] if claimed else None)
        status: dict[str, Any] = {
            "checked_in": bool(claimed) and not claimable,
            "claimable": bool(claimable),
            "inactive": not campaigns,
            "unavailable": bool(campaigns) and not claimable and not claimed,
            "streak_days": 0,
            "message": "",
        }
        if today is not None:
            status.update({
                "daily_credit": today.amount,
                "benefit_kind": today.kind,
                "activity_key": today.key,
                "activity_name": today.key,
                "campaign_id": today.id,
                "ends_at": today.end_at or None,
            })
            if claimable and today.end_at > 0:
                status["next_ts"] = int(today.end_at)
                status["next_ts_source"] = SOURCE_UPSTREAM
            elif not claimable:
                window_next = next_from_window(today.start_at, today.end_at)
                if window_next is not None:
                    status["next_ts"] = window_next
                    status["next_ts_source"] = SOURCE_UPSTREAM
        if claimable:
            status["message"] = f"今日可领 {claimable[0].amount or ''} Credits".strip()
        elif claimed:
            status["message"] = "今日已领取"
        elif campaigns:
            # VIEW_DETAILS 等条目证明活动存在，但不代表有可领取的签到奖励。
            status["message"] = "有活动，但当前没有可领取的签到奖励"
        return status

    async def checkin_status(self) -> dict[str, Any] | None:
        """查同区域所有账号的活动权益领取状态。每天重新拉列表，不跨天缓存。

        只把逐条 ``CLAIM_BENEFIT`` + ``CLAIMABLE`` 当成可领，不采信顶层
        ``claimable``；账号的活动权益彼此独立，逐号查询并聚合，避免顺位首号
        没有签到奖励时掩盖后续账号的活动。
        """
        # 签到与额度冷却无关（冷却挡的是模型转发）：全量枚举本区账号，
        # 否则在冷却的账号会整条从签到卡里消失（2026-10-07 codebuddy 同病实报）。
        region = self._region.key
        accounts = [a for a in list_accounts() if getattr(a, "region", None) == region]
        if not accounts:
            if list_accounts():
                return {"checked_in": False, "claimable": False, "error":
                        "qoder 本区没有已登录账号"}
            try:
                client = await self._campaigns()
                return await self._campaign_status(await client.list())
            except Exception as exc:  # noqa: BLE001 - 状态查询失败不该让整页 500
                log.warning("qoder 活动状态查询失败: %s", exc)
                return {"checked_in": False, "claimable": False,
                        "error": str(exc)[:200], "message": str(exc)[:200]}

        didx = {a.id: a.priority + 1 for a in list_accounts()}
        # 无名账号（登录时 deviceToken 回包不带身份）懒回填 name/email：
        # 本区一旦出现无名号就查一次 userinfo（进程内每号只成功一次），
        # 下轮快照起明细行/额度面板都显示真名而不是 UUID。
        nameless = [a.id for a in accounts if not (a.alias or a.name or a.email)]
        if nameless:
            await asyncio.to_thread(
                lambda: [backfill_identity(aid) for aid in nameless])
            accounts = [a for a in list_accounts()
                        if getattr(a, "region", None) == region]
            didx = {a.id: a.priority + 1 for a in list_accounts()}

        async def _status_one(acct: AccountRef) -> tuple[AccountRef, dict[str, Any]]:
            try:
                client = await self._campaigns(acct)
                status = await self._campaign_status(await client.list())
                return acct, status
            except Exception as exc:  # noqa: BLE001 - 单账号失败不阻断其他账号
                log.warning("qoder 活动状态查询失败（%s）: %s", acct.id, exc)
                return acct, {"error": str(exc)[:120]}

        # 多账号查询并发，避免每个账号的上游超时串行累加成几十秒的管理页等待。
        results = await asyncio.gather(*(_status_one(acct) for acct in accounts))

        def _detail_rows() -> list[dict[str, Any]]:
            """逐账号明细行（前端第二行起，一行一个账号）。

            活动信息（``daily_credit``/``activity_name`` 等）也要跟着进行——
            之前只拷了状态五键，顶层聚合又没回填，多账号通道的卡片永远缺
            「每日 +100.00」，单账号通道却有，用户看到的就是这种不一致。
            """
            rows = []
            for acct, status in results:
                i = didx.get(acct.id, acct.priority + 1)
                row: dict[str, Any] = {"id": acct.id, "index": i,
                                       "name": acct.alias or acct.name or acct.email or acct.id}
                if status.get("error"):
                    row["error"] = status["error"]
                else:
                    row.update({k: status.get(k) for k in
                                ("checked_in", "claimable", "inactive", "unavailable",
                                 "message", "daily_credit", "benefit_kind",
                                 "activity_name")})
                rows.append(row)
            return rows

        if len(results) == 1:
            # 单账号也要带 accounts 明细：qoderintl 只有一个号，之前走这条
            # 原样返回，卡片上没有第二行账号，和多账号通道版式对不上。
            status = results[0][1]
            if not status.get("error"):
                status["accounts"] = _detail_rows()
            return status

        valid = [status for _, status in results if not status.get("error")]
        claimable = [s for s in valid if s.get("claimable")]
        claimed = [s for s in valid if s.get("checked_in")]
        any_activity = any(not s.get("inactive") for s in valid)
        details = _detail_rows()
        failures = [s["error"] for _, s in results if s.get("error")]
        # 顶层也回填活动信息（chips 用）。「每日 +X」是多账号**总和**（用户
        # 2026-10-07 要求，前端带「（N账号）」后缀）；活动 key 同区域一般相同，
        # 取第一个带 activity_name 的；没有就保持缺省，不硬造。
        daily_sum = sum(s["daily_credit"] for s in valid
                        if isinstance(s.get("daily_credit"), (int, float)))
        activity = next((s for s in valid if s.get("activity_name")), None)
        # 「下次/截止」也要聚合下发，否则多账号通道的卡片没有时间 chip（单账号
        # 分支原样返回带着，又是一种不一致）。有可领账号时各号截止不同，取
        # **最早**的——那是用户此刻该关心的时间点。
        nexts = [(s["next_ts"], s.get("next_ts_source")) for s in valid
                 if isinstance(s.get("next_ts"), (int, float))]
        next_ts_out: dict[str, Any] = {}
        if nexts:
            ts, src = min(nexts)
            next_ts_out = {"next_ts": int(ts), "next_ts_source": src}
        return {
            "checked_in": bool(claimed) and not claimable and not failures,
            "claimable": bool(claimable),
            "inactive": not any_activity and not failures,
            "unavailable": any(s.get("unavailable") for s in valid) and not claimable and not claimed,
            "error": ("；".join(failures)[:200]
                      if failures and not claimable else ""),
            "accounts": details,
            "message": ("有账号可领取签到奖励" if claimable else
                        "部分账号查询失败，签到状态不完整" if failures else
                        "今日已领取" if claimed else
                        "有活动，但当前没有可领取的签到奖励" if any_activity else ""),
            **({"daily_credit": daily_sum} if daily_sum else {}),
            **({"benefit_kind": activity["benefit_kind"],
                "activity_name": activity["activity_name"]}
               if activity else {}),
            **next_ts_out,
        }

    async def checkin_claim(self) -> dict[str, Any] | None:
        """领取首个有可领签到奖励的同区域账号（``POST .../{campaignId}/claim``）。

        账号各自有独立活动列表。按优先级扫描，跳过没有可领取签到奖励的账号，
        但每次点击仍只领取一个账号的奖励；对已领过的活动再 POST 会重放，故
        ``replayed`` 仍用于区分是否真的新发 Credits。
        """
        # 与 checkin_status 同口径：不剔除冷却账号（冷却挡转发不挡签到）
        region = self._region.key
        accounts = [a for a in list_accounts() if getattr(a, "region", None) == region]
        if not accounts:
            accounts = [None]
        found_claimable = None
        claimed = []
        has_activity = False
        errors = []
        for acct in accounts:
            try:
                client = await (self._campaigns() if acct is None else self._campaigns(acct))
                campaigns = await client.list()
            except Exception as exc:  # noqa: BLE001 - 继续查后续账号
                errors.append(str(exc)[:120])
                continue
            has_activity = has_activity or bool(campaigns)
            target = next((c for c in campaigns if c.is_claimable), None)
            if target is not None:
                found_claimable = (client, target)
                break
            claimed.extend(c for c in campaigns
                           if c.action_type == CLAIM_ACTION and c.is_claimed)
        if found_claimable is None:
            if errors:
                raise RuntimeError("；".join(errors))
            if claimed:
                return {
                    "checked_in": True,
                    "claimable": False,
                    "message": "今日已领取（无需重复领取）",
                    "activity_key": claimed[0].key,
                }
            return {
                "checked_in": False,
                "claimable": False,
                "inactive": not has_activity,
                "unavailable": has_activity,
                "message": ("有活动，但当前没有可领取的签到奖励" if has_activity
                            else "当前没有可领取的活动"),
            }

        client, target = found_claimable
        result = await client.claim(target.id)
        if not result.ok:
            raise RuntimeError(
                result.message or result.error_code or f"领取失败（status={result.status}）"
            )
        return {
            "checked_in": True,
            "claimable": False,
            "replayed": result.replayed,
            # 重放时并没有新发奖，不给积分数字，免得上游打卡历史记成「今天领了」。
            "extra_credits": None if result.replayed else target.amount,
            "activity_key": target.key,
            "grant_id": result.grant_id,
            "claimed_at": result.claimed_at,
            "message": (
                "该活动此前已领取（重放），本次未新发 Credits"
                if result.replayed
                else f"已领取 {target.amount or ''} Credits".strip()
            ),
        }

    # -- 额度 ---------------------------------------------------------------

    async def quota(self) -> dict[str, Any] | None:
        """查额度（``/api/v2/quota/usage``），多账号并发。

        Qoder 的 ``userQuota`` 在部分账号（个人版）恒为 0，真实余额在
        ``addOnQuota``——因此取「total 更大的一侧」作为展示口径。多账号时
        各账号条目带 ``Qoder #N · `` 前缀供前端分组；个别账号失败插
        ``query_failed`` 说明条（benefits 层认这个标记走短缓存）。
        """
        # 遍历**区内全量**账号（与 checkin_status 同口径），不剔除冷却中的：
        # 冷却挡的是模型转发，与额度查询的 billing 面无关——available_accounts
        # 会把冷却账号整条从额度面板藏掉（2026-10-11 trae 同病实报：3 号只显
        # 1 个）。enumerate 位次对全量列表天然与账号快照一致，不引入错位。
        region = self._region.key
        accounts = [a for a in list_accounts()
                    if getattr(a, "region", None) == region]
        if not accounts:
            return None
        multi = len(accounts) > 1

        if multi:
            pool = _quota_executor()
            futures = [pool.submit(self._quota_one, a, i + 1, multi=True)
                       for i, a in enumerate(accounts)]
            deadline = time.monotonic() + _QUOTA_ROUND_DEADLINE_S
            items: list[dict[str, Any]] = []
            failed: list[str] = []
            for acct, fut in zip(accounts, futures):  # 按 failover 顺位收集，UI 顺序稳定
                name = f"#{acct.priority + 1}"
                try:
                    wrapped = asyncio.wrap_future(fut)
                    its, ok = await asyncio.wait_for(
                        wrapped, timeout=max(deadline - time.monotonic(), 0.05))
                except Exception:  # noqa: BLE001 - 超时/异常账号都算失败
                    its, ok = [], False
                if not ok:
                    failed.append(name)
                else:
                    items.extend(its)
            if failed:
                items.insert(0, {
                    "label": "Qoder 额度查询失败",
                    "used": None, "total": None,
                    "remaining": f"{len(failed)}/{len(accounts)} 个账号取不到额度"
                                 f"（{'、'.join(failed)}）",
                    "percent": None, "reset_ts": None,
                    "query_failed": True,
                })
        else:
            its, ok = await asyncio.to_thread(self._quota_one, accounts[0], 1, multi=False)
            items = its if ok else []

        # level / account 取首个可用账号的（标题行展示用）。
        _, first_cred = await asyncio.to_thread(ensure_account_token, accounts[0].id)
        first = cred_to_credential(first_cred)
        return {
            "items": items,
            "level": first.plan or None,
        }

    def quota_epoch(self) -> str:
        """quota 缓存代：账号列表一变（登录新号/删号/换顺位）旧快照就该作废。"""
        try:
            accts = list_accounts()
        except Exception:  # noqa: BLE001 - 拿不到就退回常量键
            return "unknown"
        return ",".join(f"{a.id}#{a.priority}" for a in accts) or "empty"

    def _quota_one(self, acct: AccountRef, index: int, *, multi: bool
                   ) -> tuple[list[dict[str, Any]], bool]:
        """单账号额度查询：``(items, ok)``。

        同步 httpx 直接跑在常驻线程池里（kimi 同款：fetch 本来就是同步调用）。
        """
        # 无名账号懒回填身份（与 checkin_status 同款；每号进程内只成功一次），
        # 否则额度面板组标题一直是「Qoder #N」对不上真名。
        if not (acct.alias or acct.name or acct.email):
            backfill_identity(acct.id)
        try:
            token, cred_dict = ensure_account_token(acct.id)
        except AuthError:
            return [], False
        cred = cred_to_credential(cred_dict)
        reg = with_cached_endpoints(resolve_region(cred.region))
        try:
            resp = httpx.get(reg.quota_url(),
                             headers={"Authorization": f"Bearer {token}",
                                      "Accept": "application/json"},
                             timeout=20)
        except httpx.HTTPError as exc:
            log.warning("qoder 额度查询失败（%s）: %s", acct.id, exc)
            return [], False
        if resp.status_code != 200:
            log.warning("qoder 额度查询 HTTP %s（%s）: %s",
                        resp.status_code, acct.id, resp.text[:160])
            return [], False
        try:
            data = resp.json()
        except ValueError:
            return [], False
        prefix = f"Qoder #{index} · " if multi else ""
        out = self._format_quota(data, cred, label_prefix=prefix)
        # 账号套餐名来自额度接口（``userType``），顺手回填到 cred 文件，让
        # /health、鉴权面板不必再单独查一次。**必须读最新盘再改 plan**：
        # cred_dict 是 ensure_account_token 返回时的快照，若期间有并发请求
        # 触发刷新（RT 滚动、token 已更新），拿旧快照整份覆盖会把新 token
        # 回滚回去——下一个请求拿着被回滚的旧 token 就 401 了。
        tier = str(data.get("userType") or "")
        if tier and cred.plan != tier:
            try:
                from .credentials import (
                    _atomic_write_json,
                    _index_file_lock,
                    _index_lock,
                    account_cred_path,
                    load_account_cred,
                )

                with _index_lock, _index_file_lock():
                    current = load_account_cred(acct.id)
                    if current is not None and current.get("plan") != tier:
                        current["plan"] = tier
                        _atomic_write_json(account_cred_path(acct.id), current)
            except Exception as exc:  # noqa: BLE001 - 回填失败不影响额度展示
                log.debug("qoder 套餐名回填失败: %s", exc)
        return out["items"], True

    def _account_info(self, cred: Credential) -> dict[str, Any]:
        return {
            "uid": cred.uid,
            "name": cred.name,
            "email": cred.email,
            "region": cred.region,
            "plan": cred.plan,
            "source": cred.source,
            "expires_at_ms": cred.expires_at_ms,
        }

    def _format_quota(self, data: dict[str, Any], cred: Credential,
                      label_prefix: str = "") -> dict[str, Any]:
        """上游额度 -> 管理页统一结构。"""
        user_q = data.get("userQuota") or {}
        addon_q = data.get("addOnQuota") or {}

        def _total(node: dict) -> float:
            try:
                return float(node.get("total") or 0)
            except (TypeError, ValueError):
                return 0.0

        # 个人版 userQuota 常为 0，真实额度在 addOnQuota；取有额度的那一侧。
        primary, label = (addon_q, "加油包") if _total(addon_q) >= _total(user_q) else (user_q, "订阅额度")
        total = _total(primary)
        try:
            used = float(primary.get("used") or 0)
        except (TypeError, ValueError):
            used = 0.0
        try:
            remaining = float(primary.get("remaining") or 0)
        except (TypeError, ValueError):
            remaining = max(total - used, 0.0)
        percent = _used_percent(primary, used, total)

        def _item(node: dict, name: str, expire_ts: int | None = None) -> dict[str, Any]:
            total_v = _total(node)
            try:
                used_v = float(node.get("used") or 0)
            except (TypeError, ValueError):
                used_v = 0.0
            try:
                remain_v = float(node.get("remaining") or 0)
            except (TypeError, ValueError):
                remain_v = max(total_v - used_v, 0.0)
            return {
                "label": label_prefix + name,
                "used": round(used_v, 4),
                "total": round(total_v, 4),
                "remaining": round(remain_v, 4),
                "percent": round(_used_percent(node, used_v, total_v), 4),
                # Qoder 的额度不会「周期性重置」，只会到期作废，故 reset_ts 恒
                # None（前端据此不拼「重置」后缀）；到期时刻走 expire_ts。
                # 专属包自带过期时间（比账号级的更早），不传则用账号级 expiresAt。
                "reset_ts": None,
                "expire_ts": expire_ts if expire_ts is not None else _expire_ts(data),
                "unit": "credit",
            }

        # 有额度的一侧排前面（个人版 userQuota 常为 0，主力额度在 addOnQuota），
        # 但另一侧只要非零就也展示——订阅额度与加油包可以并存。
        items = [_item(primary, label)]
        other_node, other_label = (user_q, "订阅额度") if label == "加油包" else (addon_q, "加油包")
        if _total(other_node) > 0:
            items.append(_item(other_node, other_label))

        # 专属资源包（活动赠送，如「Qwen 专属积分」）：与订阅额度/加油包**并存**，
        # 是账号总额度的一部分。漏掉它会让管理页显示的积分比实际少一截
        # （实测 personal_professional 账号：userQuota 2000 + 专属包 2000，
        # 只读前两个节点就只显示 2000，用户以为额度对不上）。
        for pkg in data.get("dedicatedResourcePackages") or []:
            if not isinstance(pkg, dict):
                continue
            if not _pkg_active(pkg):
                continue  # 已失效/过期的包不占额度，不展示
            items.append(_item(
                pkg, _pkg_label(pkg), expire_ts=_expire_ts(pkg),
            ))

        return {
            "level": data.get("userType") or cred.plan or None,
            "usage_type": data.get("usageType") or "credits",
            "quota_exceeded": bool(data.get("isQuotaExceeded")),
            "total_percent": data.get("totalUsagePercentage"),
            "upgrade_url": data.get("upgradeUrl"),
            "region": cred.region,
            "items": items,
            # 上面三项（订阅额度 / 加油包 / 专属积分，各自又是列表里的一条）是
            # **并存的份额**，加起来才是账号剩余总量——管理页标题行据此求和，
            # 而不是只显示第一条（否则「剩 1621/2000」会漏掉加油包与专属积分，
            # 用户看到的就是比实际少的数）。上游自己也这么算：实测三项合计
            # 已用 37.5%，上游 totalUsagePercentage 正好是 0.38。
            "sum_items": True,
            "account": self._account_info(cred),
        }

    # -- 健康 ---------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        accounts = list_accounts()
        info: dict[str, Any] = {
            "provider": self.id,
            "region": self._region.key,
            "region_label": self._region.label,
            "base_url": self._region.infer_base,
            "endpoint_type": "cosy",
            "protocol": "openai-envelope",
            "cosy_version": COSY_VERSION,
            "models": len(self._entries()),
            "authenticated": bool(accounts),
            "accounts": [
                {
                    "id": a.id,
                    "email": a.email,
                    "name": a.name or a.email or a.id,
                    "region": a.region,
                }
                for a in accounts
            ],
        }
        # 顶层字段保持主账号（#1）语义兼容旧前端。
        if accounts:
            first = load_account_cred(accounts[0].id) or {}
            first_cred = cred_to_credential(first)
            info.update(
                {
                    "account": first_cred.uid,
                    "plan": first_cred.plan or None,
                    "auth_source": first_cred.source,
                    "expires_at_ms": first_cred.expires_at_ms or None,
                }
            )
        return info

    # -- 转发 ---------------------------------------------------------------

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        """把 OpenAI chat 请求转发到 Qoder COSY 面（多账号 failover）。"""
        stream = bool(body.get("stream", True))
        model = self.resolve_model(str(body.get("model") or "auto"))

        accounts = failover.available_accounts(self._region.key)
        if not accounts:
            other = failover.available_accounts()
            if other:
                raise HTTPException(
                    status_code=401,
                    detail={"error": {
                        "message": (f"qoder 无 {self._region.label} 区域的可用账号"
                                    f"（已有 {len(other)} 个账号属于其它区域，"
                                    f"跨区账号间不自动切换）"),
                        "type": "authentication_error"}},
                )
            raise HTTPException(
                status_code=429,
                detail={"error": {
                    "message": (f"qoder 所有账号均在冷却中：{failover.cooldown_report()}"
                                "；额度冷却到点自动恢复"),
                    "type": "rate_limit_error"}},
            )

        meta = ACCOUNT_META.get()  # metrics 账号归属

        # per-account 模型支持收窄（覆盖表白名单）：不支持该模型的账号直接
        # 跳过——不冷却、不算失败（账号没坏，只是没这个权益）；全不支持时
        # 快速失败，不白打上游（实测受限号调三方模型是 ReadTimeout/带内 400，
        # 白付一次慢超时）。
        accounts = [a for a in accounts if self._account_supports(model, a.id)]
        if not accounts:
            raw_name = str(body.get("model") or "").strip()
            raise HTTPException(
                status_code=404,
                detail={"error": {
                    "message": (f"qoder 没有任何可用账号支持模型 "
                                f"{raw_name or model}（上游名 {model}）"
                                "——per-account 模型限制，见 qoder/models.json"),
                    "type": "invalid_request_error"}},
            )

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
            try:
                token, cred_dict = await asyncio.to_thread(ensure_account_token, acct.id)
            except AuthError as exc:
                failover.mark_cooldown(acct.id, reason=f"凭据不可用: {exc}")
                continue
            cred = cred_to_credential(cred_dict)
            if meta is not None:
                meta["account"] = acct.id

            queue_waited = 0.0
            gate: _Gate | None = None

            reg = with_cached_endpoints(resolve_region(cred.region))
            catalog = self._catalog_for(reg.key)
            upstream = self._build_upstream(body, model, catalog)
            body_json = json.dumps(upstream, ensure_ascii=False, separators=(",", ":"))
            url = reg.chat_url()

            # 10605 排队重放在**同账号内**循环：等上游给的时长后重发同一请求。
            # 不冷却、不换号——账号是好的，且实测多个账号会同时被挡。
            for _queue_round in range(_QUEUE_WAIT_MAX_ROUNDS + 1):
                enc_body, headers = sign(
                    url, body_json,
                    cred.uid, token, cred.machine_id,
                    name=cred.name, email=cred.email,
                    model_key=model,
                )
                headers["Accept"] = "text/event-stream"

                client = httpx.AsyncClient(timeout=_TIMEOUT_STREAM)
                try:
                    req = client.build_request("POST", url, headers=headers,
                                               content=enc_body.encode())
                    resp = await client.send(req, stream=True)
                except httpx.HTTPError as exc:
                    await client.aclose()
                    last_status, last_detail = 502, f"上游连接失败: {exc}"
                    failover.mark_cooldown(acct.id, reason=f"网络错误: {exc}")
                    continue

                if resp.status_code != 200:
                    break  # 非 200 交给下方原有分支处理
                gate = await _gate_first_event(resp, stream)
                if gate.queue_wait_s <= 0:
                    break  # 正常提交 / 账号级错误 / EOF，交给下方原有分支处理
                # 10605：上游明确要求等一会儿再试
                await _drain_and_close(gate.resp, client, gate.lines)
                if _queue_round >= _QUEUE_WAIT_MAX_ROUNDS:
                    last_status, last_detail = 403, (
                        f"上游排队已等待 {queue_waited:.0f}s 仍未放行（code 10605）")
                    gate = None  # 别让下方把这条已关闭的响应当提交结果
                    break
                wait = gate.queue_wait_s
                log.info("qoder %s 上游排队（10605），%.0fs 后重试（%s）",
                         model, wait, acct.id)
                await asyncio.sleep(wait)
                queue_waited += wait
                continue
            else:  # pragma: no cover - range 保证不会走到
                continue

            if resp.status_code != 200:
                text = (await resp.aread()).decode("utf-8", "replace")[:300]
                await resp.aclose()
                if resp.status_code == 401:
                    # token 被上游拒（refresh_token 被轮换/作废等）——强刷一次重试同
                    # 账号。401 分支**不关 client**：强刷重试复用同连接。
                    last_status, last_detail = 401, text
                    try:
                        token, cred_dict = await asyncio.to_thread(
                            ensure_account_token, acct.id, force_refresh=True)
                    except AuthError as exc:
                        await client.aclose()
                        failover.mark_cooldown(acct.id, reason=f"强刷失败: {exc}")
                        continue
                    cred = cred_to_credential(cred_dict)
                    enc_body, headers = sign(
                        url, body_json,
                        cred.uid, token, cred.machine_id,
                        name=cred.name, email=cred.email,
                        model_key=model,
                    )
                    headers["Accept"] = "text/event-stream"
                    try:
                        req = client.build_request("POST", url, headers=headers,
                                                   content=enc_body.encode())
                        resp = await client.send(req, stream=True)
                    except httpx.HTTPError as exc:
                        await client.aclose()
                        last_status, last_detail = 502, f"上游连接失败（强刷重试）: {exc}"
                        failover.mark_cooldown(acct.id, reason="强刷重试网络错误")
                        continue
                    if resp.status_code == 401:
                        text = (await resp.aread()).decode("utf-8", "replace")[:300]
                        await resp.aclose()
                        await client.aclose()
                        last_status, last_detail = 401, text
                        failover.mark_cooldown(acct.id, reason="401 强刷后仍被拒")
                        continue
                    if resp.status_code != 200:
                        # 强刷后变成别的错误码（401 已在上方 continue）：按新状态码处理。
                        text = (await resp.aread()).decode("utf-8", "replace")[:300]
                        await resp.aclose()
                        await client.aclose()
                        if resp.status_code == 429:
                            failover.mark_cooldown(acct.id, quota=True,
                                                   retry_after=resp.headers.get("Retry-After"),
                                                   reason=f"HTTP 429（强刷后）: {text[:120]}")
                            continue
                        if resp.status_code == 403 and ("112" in text or "pricing" in text.lower()):
                            failover.mark_cooldown(acct.id, quota=True,
                                                   reason=f"HTTP 403 权益门（强刷后）: {text[:120]}")
                            continue
                        raise HTTPException(status_code=resp.status_code,
                                            detail=f"qoder 上游 HTTP {resp.status_code}: {text}")
                    # 强刷后 200：这条路径绕过了上面的排队循环，闸门还没过。
                    gate = await _gate_first_event(resp, stream)
                else:
                    await client.aclose()
                if resp.status_code == 429:
                    last_status, last_detail = 429, text
                    failover.mark_cooldown(acct.id, quota=True,
                                           retry_after=resp.headers.get("Retry-After"),
                                           reason=f"HTTP 429: {text}")
                    continue
                if resp.status_code == 403:
                    # 403 可能是账号级（额度尽、权益收回 code 112）也可能是请求级
                    # （模型不存在）。带 112/pricing 的按账号冷却换号；其余透传。
                    if "112" in text or "pricing" in text.lower():
                        last_status, last_detail = 403, text
                        failover.mark_cooldown(acct.id, quota=True,
                                               reason=f"HTTP 403 权益门: {text[:120]}")
                        continue
                    await client.aclose()
                    raise HTTPException(status_code=403, detail=f"qoder 上游 HTTP 403: {text}")
                # 其余业务 4xx（模型名不合法等）：换号没意义，原样透传
                await client.aclose()
                raise HTTPException(status_code=resp.status_code,
                                    detail=f"qoder 上游 HTTP {resp.status_code}: {text}")

            # 200：闸门已在上面的排队循环里过完（gate 由循环带出）——首个内层
            # chunk / 带内错误帧到达前没向客户端吐过任何字节，可以安全冷却换号；
            # 见到语义事件后绝不重放（防重复计费）。
            if gate is None:
                # 排队等待用尽（响应已在循环里排空关闭）：不是账号故障——不冷却，
                # 直接试下一个账号（多个账号同时被挡时等待也多半白等）。
                continue
            if gate.account_error:
                last_status, last_detail = gate.code, gate.message
                failover.mark_cooldown(
                    acct.id, quota=gate.code in (112, 429),
                    reason=f"带内 error {gate.code}: {gate.message[:120]}")
                await _drain_and_close(gate.resp, client, gate.lines)
                continue
            if gate.eof:
                last_status, last_detail = 502, "首事件前断流（上游空响应）"
                await _drain_and_close(gate.resp, client, gate.lines)
                continue

            if stream:
                inner = _ReplayStream(gate.resp, gate.buffered, gate.lines, client)
                if protocol == "anthropic":
                    from ..protocols.anthropic_adapter import AnthropicStreamConverter

                    return StreamingResponse(
                        _to_anthropic_stream(
                            _stream_inner(inner, model), model, AnthropicStreamConverter),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    )
                return StreamingResponse(
                    _stream_inner(inner, model),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            payload = await _collect_inner(gate.resp, client, gate.lines, gate.buffered, model)
            if payload is None:
                raise HTTPException(
                    status_code=502,
                    detail={"error": {"message": "qoder upstream returned non-JSON",
                                      "type": "bad_gateway"}},
                )
            if protocol == "anthropic":
                from ..protocols.anthropic_adapter import chat_completion_to_anthropic_message
                return JSONResponse(chat_completion_to_anthropic_message(payload, original))
            return JSONResponse(payload)

        # 全部账号失败
        report = failover.cooldown_report()
        tail = f"最后错误 HTTP {last_status or 'n/a'}{(': ' + last_detail) if last_detail else ''}"
        raise HTTPException(
            status_code=last_status if last_status in (401, 403, 429, 504) else 502,
            detail={"error": {
                "message": (f"qoder 所有账号均不可用（{report}；{tail}）" if report
                            else f"qoder 所有账号均不可用（{tail}）"),
                "type": ("timeout" if last_status == 504
                         else "rate_limit_error" if last_status in (403, 429)
                         else "bad_gateway"),
            }},
        )

    # -- 出站体构造 ---------------------------------------------------------

    def _build_upstream(self, body: dict[str, Any], model: str,
                        catalog: Catalog | None = None) -> dict[str, Any]:
        """组装带归因信封的上游 body（明文）。

        ``catalog`` 缺省用主区域目录（测试与单区域调用方常见形态）。
        """
        upstream: dict[str, Any] = {
            "model": model,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        for field in _PASSTHROUGH_FIELDS:
            value = body.get(field)
            if value is not None:
                upstream[field] = value

        # developer -> system：上游在反序列化阶段整请求拒绝该 role。
        messages = upstream.get("messages")
        if isinstance(messages, list):
            upstream["messages"] = [_normalize_message(m) for m in messages]

        now_ms = int(time.time() * 1000)
        request_id = str(uuid.uuid4())
        request_set_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        prompt_text = _last_user_text(upstream.get("messages") or [])
        if catalog is None:
            catalog = self._catalog_for(self._region.key)
        entry = catalog.entry(model)

        upstream.update(
            {
                "request_id": request_id,
                "request_set_id": request_set_id,
                "chat_record_id": request_id,
                "session_id": session_id,
                "chat_task": "FREE_INPUT",
                "chat_context": {
                    "text": prompt_text,
                    "features": [],
                    "extra": {
                        "context": [],
                        "modelConfig": {
                            "key": model,
                            "is_reasoning": bool(entry.get("is_reasoning")),
                        },
                        "originalContent": prompt_text,
                    },
                    "chatPrompt": "",
                    "imageUrls": None,
                },
                "is_reply": True,
                "is_retry": False,
                "source": 1,
                "version": "3",
                "agent_id": "agent_common",
                "task_id": "common",
                "session_type": "qoderclicn",
                "aliyun_user_type": "",
                "model_config": {
                    "key": model,
                    "display_name": str(entry.get("display_name") or model),
                    "model": "",
                    "format": "openai",
                    "is_vl": bool(entry.get("is_vl")),
                    "is_reasoning": bool(entry.get("is_reasoning")),
                    "api_key": "",
                    "url": "",
                    "source": "system",
                    "max_input_tokens": entry.get("max_input_tokens") or 128000,
                },
                "business": {
                    "product": "cli",
                    "version": COSY_VERSION,
                    "type": "agent",
                    "id": request_set_id,
                    "name": prompt_text[:10],
                    "begin_at": now_ms,
                    "stage": "processing",
                },
            }
        )
        return upstream


# ---------------------------------------------------------------------------
# 首事件闸门 / 重放流 / 内层流拆解
# ---------------------------------------------------------------------------

@dataclass
class _Gate:
    """首事件闸门的判定结果（qoder 上游是 SSE 信封，闸门按**信封帧**消费）。"""
    resp: httpx.Response
    committed: bool = False
    buffered: list[str] = field(default_factory=list)  # 闸门期间缓冲的原始 SSE 行
    lines: Any = None  # 在途行迭代器（续跑/排空都用它，不重开 aiter_lines）
    account_error: bool = False
    code: int = 0
    message: str = ""
    eof: bool = False
    #: 10605 排队：上游要求的等待秒数（>0 表示这不是真错误，等一下重试即可）。
    queue_wait_s: float = 0.0


def _queue_wait_seconds(error: str) -> float | None:
    """从 10605 错误链里挖出上游要求的等待秒数；非 10605 返回 ``None``。

    上游把真正的载荷埋了三层（信封 ``body`` → ``{"code":"10605","message":...}``
    → 内层 JSON 才有 ``retryAfterSeconds``），而且 ``_unwrap`` 已把最外层拍平成
    一行文本，所以这里顺着 ``message`` 逐层 ``json.loads`` 往下钻。挖不到就退
    到 :data:`_QUEUE_WAIT_MAX_S`——既然认出了 10605，等一会儿总比立刻失败好。
    """
    if "10605" not in error:
        return None
    payload: Any = error
    for _ in range(4):  # 层数有限，防环
        if isinstance(payload, dict):
            secs = payload.get("retryAfterSeconds")
            if isinstance(secs, (int, float)):
                return min(float(secs), _QUEUE_WAIT_MAX_S)
            # 10605 内层给了 serviceAvailable=false 时上游确实在排空，
            # 但仍按 retryAfterSeconds 走；这里只做兜底取值。
            payload = payload.get("message")
            continue
        if not isinstance(payload, str):
            break
        text = payload.strip()
        start = text.find("{")
        if start < 0:
            break
        try:
            payload = json.loads(text[start:])
        except ValueError:
            # 错误串后面常挂 ``| code=403`` 之类的尾巴（``_describe_upstream_error``
            # 拼的），整体 json.loads 必失败——退到 raw_decode 只吃第一个对象。
            try:
                payload = json.JSONDecoder().raw_decode(text, start)[0]
            except ValueError:
                break
    return _QUEUE_WAIT_MAX_S


def _classify_frame(payload: str) -> str:
    """单个上游信封帧定性：``account_error`` / ``queue_wait`` / ``semantic`` / ``wait``。

    - 带内错误（:func:`_unwrap` 给出 error——含 envelope 顶层带 code 的权益门
      帧）→ 挖 ``code``：112/429/401 是账号级（权益门/额度/token 失效）冷却换号；
      **10605 是排队**，返回 ``queue_wait:<秒数>`` 交给闸门原地等待重放；
      其余请求级错误原样透传。
    - 内层 chunk 带 ``choices``/``usage`` → 语义已至（committed）。
    - 尾帧/心跳（无 body 无 code）→ 继续等。
    """
    inner, error, _done = _unwrap(payload)
    if error is not None:
        wait = _queue_wait_seconds(error)
        if wait is not None:
            return f"queue_wait:{wait}"
        code = 0
        try:
            frame = json.loads(payload)
            code = int((frame or {}).get("code") or 0)
        except (ValueError, TypeError):
            pass
        if code in (112, 429, 401):
            return f"account_error:{code}:{error}"
        # 请求级错误：交给后续拆解当语义透传（转换器会发 error 事件收尾）
        return "semantic"
    if inner is not None:
        return "semantic"
    return "wait"


async def _gate_first_event(resp: httpx.Response, stream: bool) -> _Gate:
    """压住第一个上游事件再决定透传还是换号。

    流式：缓冲原始 SSE 行直到第一条能定性的信封帧——内层 chunk（语义已至）
    即 committed（缓冲行随透传补放）；带内错误帧按 code 分类；尾帧/心跳继续等；
    语义事件前断流/EOF 按 eof 处理（换号，防「200 空流假成功」）。非流式：COSY
    面恒为 SSE 信封，``stream=False`` 也走同一套行缓冲（由调用方聚合）。
    """
    buffered: list[str] = []
    lines = resp.aiter_lines()
    try:
        async for line in lines:
            buffered.append(line)
            if len(buffered) > _GATE_BUFFER_MAX_LINES:
                return _Gate(resp=resp, committed=True, buffered=buffered, lines=lines)
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            payload = stripped[5:].strip()
            if not payload:
                continue
            verdict = _classify_frame(payload)
            if verdict.startswith("queue_wait:"):
                _, _, secs = verdict.partition("queue_wait:")
                try:
                    wait = float(secs)
                except ValueError:
                    wait = _QUEUE_WAIT_MAX_S
                return _Gate(resp=resp, code=10605, queue_wait_s=wait, lines=lines)
            if verdict.startswith("account_error:"):
                _, _, rest = verdict.partition("account_error:")
                code_s, _, message = rest.partition(":")
                try:
                    code = int(code_s)
                except ValueError:
                    code = 0
                return _Gate(resp=resp, account_error=True, code=code,
                             message=message, lines=lines)
            if verdict == "semantic":
                return _Gate(resp=resp, committed=True, buffered=buffered, lines=lines)
            # 尾帧/心跳：继续等下一条
    except httpx.TimeoutException:
        return _Gate(resp=resp, eof=True, lines=lines)
    except httpx.HTTPError:
        return _Gate(resp=resp, eof=True, lines=lines)
    return _Gate(resp=resp, eof=True, lines=lines)


class _ReplayStream:
    """闸门缓冲行 → 真实流的适配器（先补放缓冲，再接原流）。

    下游只用 ``aiter_lines()``/``aclose()``。``lines`` 为闸门的在途迭代器：
    续跑它而不是重开 ``resp.aiter_lines()``，否则会被 httpx 判为二次消费
    （kimi #72 同坑：claude 流式因此完全空流）。
    """

    def __init__(self, resp: httpx.Response, buffered: list[str], lines: Any,
                 client: httpx.AsyncClient) -> None:
        self._resp = resp
        self._buffered = list(buffered)
        self._lines = lines
        self._client = client

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._buffered:
            yield line
        if self._lines is not None:
            async for line in self._lines:
                yield line
        else:  # pragma: no cover - 闸门路径必然带 lines
            async for line in self._resp.aiter_lines():
                yield line

    async def aclose(self) -> None:
        await self._resp.aclose()
        await self._client.aclose()


async def _drain_and_close(resp: httpx.Response, client: httpx.AsyncClient,
                           lines: Any = None) -> None:
    """读完丢弃响应体并关闭（换号前必须回收连接，别挂着半开流）。"""
    try:
        if lines is not None:
            try:
                async for _ in lines:
                    pass
            except (httpx.HTTPError, httpx.StreamError):
                pass
        elif not resp.is_stream_consumed:
            await resp.aread()
    finally:
        await resp.aclose()
        await client.aclose()


async def _stream_inner(replay: _ReplayStream, model: str) -> AsyncIterator[bytes]:
    """拆 COSY 信封，把内层 OpenAI chunk 原样下游（带内错误转 OpenAI error 帧）。"""
    started = time.time()
    try:
        async for line in replay.aiter_lines():
            if time.time() - started > STREAM_TIMEOUT_S:
                log.warning("qoder 流超时（%.0fs），中止", STREAM_TIMEOUT_S)
                break
            raw = line.strip()
            if not raw or raw.startswith("event:"):
                continue
            if not raw.startswith("data:"):
                continue
            payload = raw[5:].strip()
            inner, error, done = _unwrap(payload)
            if error is not None:
                log.warning("qoder 上游带内错误 (%s): %s", model, error)
                yield _sse_error(error)
                yield b"data: [DONE]\n\n"
                return
            if done:
                yield b"data: [DONE]\n\n"
                return
            if inner is not None:
                yield f"data: {inner}\n\n".encode()
    except httpx.HTTPError as exc:
        log.warning("qoder 流中断: %s", exc)
        yield _sse_error(f"上游流中断: {exc}")
        yield b"data: [DONE]\n\n"
    finally:
        await replay.aclose()


async def _collect_inner(resp: httpx.Response, client: httpx.AsyncClient,
                         lines: Any, buffered: list[str],
                         model: str) -> dict[str, Any] | None:
    """非流式：聚合内层 chunk 成一个 ``chat.completion``。"""
    created = int(time.time())
    content: list[str] = []
    reasoning: list[str] = []
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    tool_calls: dict[int, dict[str, Any]] = {}
    error: str | None = None

    async def _lines() -> AsyncIterator[str]:
        for line in buffered:
            yield line
        if lines is not None:
            async for line in lines:
                yield line
        else:  # pragma: no cover
            async for line in resp.aiter_lines():
                yield line

    try:
        async for line in _lines():
            raw = line.strip()
            if not raw.startswith("data:"):
                continue
            inner, err, done = _unwrap(raw[5:].strip())
            if err is not None:
                error = err
                break
            if done:
                break
            if inner is None:
                continue
            try:
                chunk = json.loads(inner)
            except ValueError:
                continue
            choices = chunk.get("choices") or []
            if choices:
                choice = choices[0] or {}
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content.append(str(delta["content"]))
                if delta.get("reasoning_content"):
                    reasoning.append(str(delta["reasoning_content"]))
                for call in delta.get("tool_calls") or []:
                    idx = int(call.get("index") or 0)
                    slot = tool_calls.setdefault(
                        idx, {"id": "", "type": "function",
                              "function": {"name": "", "arguments": ""}}
                    )
                    if call.get("id"):
                        slot["id"] = call["id"]
                    fn = call.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += str(fn["arguments"])
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
            if chunk.get("usage"):
                usage = chunk["usage"]
    except httpx.HTTPError as exc:
        error = f"上游流中断: {exc}"
    finally:
        await resp.aclose()
        await client.aclose()

    if error is not None:
        raise _upstream_error(error)

    message: dict[str, Any] = {"role": "assistant", "content": "".join(content) or None}
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason or "stop",
            }
        ],
        "usage": usage
        or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ---------------------------------------------------------------------------
# 冒烟自测：python -m buddy_proxy.qoder.provider
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    from .config import REGIONS

    p = QoderProvider()
    print("health:", json.dumps(p.health(), ensure_ascii=False))
    accounts = list_accounts()
    if not accounts:
        print("未登录：先跑 buddy login qoder（浏览器授权）")
        raise SystemExit(1)
    resp = asyncio.run(p.forward(
        {
            "model": "qwen3.8-flash",
            "messages": [{"role": "user", "content": "只回复两个字：pong"}],
            "stream": False,
        },
        "openai",
    ))
    print("status:", resp.status_code)
    print(str(resp.body)[:400])
