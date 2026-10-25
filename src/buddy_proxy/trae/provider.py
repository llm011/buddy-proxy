"""TraeProvider：OpenAI/Anthropic 兼容入口，编排原生通道与文本协议兜底。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
import logging
import os
import queue
import threading
import time
import uuid
from typing import Any, AsyncIterator, Sequence

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.checkin import SOURCE_INFERRED, next_daily_reset
from ..core.metrics import ACCOUNT_META
from ..protocols.anthropic_adapter import chat_completion_to_anthropic_message
from ..providers.base import BaseProvider
from .benefits_api import claim_checkin_credits, fetch_checkin_status, fetch_ent_usage
from .config import (
    TRAE_HEARTBEAT_INTERVAL,
    TRAE_SEMANTIC_TIMEOUT,
    _NATIVE_TOOLS_ENABLED,
    _debug_dump,
    credit_multiplier,
    model_tables,
    resolve_trae_region,
)
from . import failover
from .credentials import (
    _auth,
    _load_work_cred,
    ensure_account_token,
    list_accounts,
    load_account_cred,
    set_current_work_account,
)
from .leak_guard import _StreamLeakCleaner, _sanitize_agent_leak
from .native_tools import (
    _NativeToolAccumulator,
    _native_messages,
    _native_rejected,
    _send_native_chat,
)
from .sse import (_parse_sse, _trae_error_text, _wrap_anthropic_stream,
                  _sse_error_status)
from .text_protocol import _extract_prompt, _looks_like_agent_request
from .transport import _send_trae_work_chat
from .text_toolcall import _StreamToolCallSplitter, _parse_tool_calls, _tool_names
from .transport import send_trae_chat

log = logging.getLogger(__name__)

#: 4001 后的安静节流窗口：上游对**刚失败**的账号有快速重试惩罚（2026-10-07
#: 实测：流式探测 4001 会把该模型「毒」~30-60s，期间**连非流式也一并 4001**，
#: 且窗口随连续失败次数拉长——密集失败后实测可到数分钟才衰减）。策略不是
#: 固定退避而是**节流**：记住该模型最近一次 4001 时刻，此后的上游尝试先安静
#: 等到窗外再发——等待本身不产生失败，窗口不会被越喂越长。默认 25s 的依据：
#: 同日对照实验中 +15s 的重试 3/3 仍落在窗内、+31s 起 2/2 成功，25s 让第一跳
#: 直接跳过必败的 +15s 区。
# 下界 5s：设 0/负数等于关掉节流，退回窗内连环开火的老路
_REJECT_QUIET_S = max(5.0, float(os.environ.get("WB_TRAE_REJECT_QUIET_S", "25")))
_last_reject_at: dict[str, float] = {}


def _note_reject(model: str) -> None:
    _last_reject_at[model] = time.monotonic()


def _pace_after_reject(model: str) -> None:
    """该模型最近 4001 后不满安静窗则等到窗外再发（读线程/to_thread 内睡）。"""
    t = _last_reject_at.get(model)
    if t is None:
        return
    wait = _REJECT_QUIET_S - (time.monotonic() - t)
    if wait > 0:
        time.sleep(wait)


def _note_native_ok(model: str) -> None:
    """native 请求成功＝门开着：清掉节流时间戳，后续请求不必陪等安静窗。"""
    _last_reject_at.pop(model, None)

#: 「该模型 chat_v3 流式被上游整体拒绝」的标记 TTL。流式 4001 后短 TTL 内直接
#: 跳过必败的流式尝试、straight to 非流式缓冲——既省一次注定失败的往返，也避免
#: 每个流式请求都喂养一次惩罚窗口（实测惩罚窗会随连续失败拉长，02:31 时 ~1s、
#: 02:42 已漂到 ~5s+）。TTL 过后放一次流式探测，上游恢复流式即自动回到直通。
#: 探测本身会毒化安静窗（见上），TTL 别设太短——一次探测税 = 探测失败 + 兜底链
#: 的 3-4 次节流重试，窗内该模型请求全部变慢甚至报错，实测累积失败后毒窗可到
#: 分钟级。默认 30 分钟：流式门控若真要恢复，最多晚 30 分钟发现，换来的稳态是
#: 探测税不再周期性打断正常流量。下界 60s：设得更小等于频繁放探测、反复交税。
_STREAM_REJECT_SKIP_S = max(60.0, float(os.environ.get("WB_TRAE_STREAM_REJECT_TTL_S", "1800")))
_stream_reject_until: dict[str, float] = {}


def _stream_rejected_recently(model: str) -> bool:
    """该模型的流式尝试近期被 4001 拒过（TTL 内）。单调钟，免疫墙钟跳变。"""
    until = _stream_reject_until.get(model)
    return until is not None and time.monotonic() < until


def _mark_stream_rejected(model: str) -> None:
    _stream_reject_until[model] = time.monotonic() + _STREAM_REJECT_SKIP_S
    _note_reject(model)

#: 多账号额度并发查询：整轮 deadline + 常驻线程池（与 antigravity/qoder 同口径）。
#: 常驻（不是每轮新建）的理由见 trae/pat/quota.py：每轮新建 + shutdown(wait=False)
#: 会让慢轮线程留在后台累积；常驻池上限封顶，慢轮占名额、后续轮次自然排队。
_QUOTA_ROUND_DEADLINE_S = 8.0
_QUOTA_WORKERS = 4
_quota_pool: "concurrent.futures.ThreadPoolExecutor | None" = None
_quota_pool_lock = threading.Lock()

#: 流式生成器可能长时间阻塞，不能和额度/管理请求共享 asyncio 默认线程池。
#: 首帧闸门与已提交后的迭代分池，避免慢闸门挤占正在传输的流。
_STREAM_GATE_WORKERS = 16
_STREAM_ITER_WORKERS = 32
_stream_gate_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=_STREAM_GATE_WORKERS, thread_name_prefix="trae-stream-gate")
_stream_iter_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=_STREAM_ITER_WORKERS, thread_name_prefix="trae-stream-iter")


def _quota_executor() -> "concurrent.futures.ThreadPoolExecutor":
    global _quota_pool
    with _quota_pool_lock:
        if _quota_pool is None:
            _quota_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=_QUOTA_WORKERS, thread_name_prefix="trae-quota")
        return _quota_pool


#: 权益包额度/已用的候选字段名，按计费口径各一套（见 ``_quota_items`` 注释）。
#: CN 侧是 2026-10-03 实测的确切字段名；海外（dollar）尚未拿到真账号快照，故
#: 把美元专用名放前面、CN 名放后面兜底——上游若沿用同名 credits_* 承载美元
#: 数值也读得对，等实测拿到海外响应后再收敛成单一字段名。
_LIMIT_KEYS_CREDITS = ("credits_limit",)
_AMOUNT_KEYS_CREDITS = ("credits_amount",)
_LIMIT_KEYS_DOLLAR = ("dollar_limit", "usage_limit", "credits_limit", "limit")
_AMOUNT_KEYS_DOLLAR = ("dollar_amount", "usage_amount", "credits_amount", "amount")


# ---- 签到节流 / 限流退避 ----
# 上游签到接口有短窗口频控：多账号背靠背连打时，第二个起的请求命中
# 「当前参与用户太多，请稍后再试」（2026-10-06 多账号实测复现）。
# 对策两段：账号间强制间隔，让每个 claim 看起来都是独立用户行为；
# 仍命中限流时退避重试（间隔递增），活动高峰期第一发也可能被挤掉。
_CHECKIN_THROTTLE_S = 4.0
_CHECKIN_RETRY_DELAYS = (5.0, 10.0)

#: 限流文案特征（上游 message 原样匹配，出现在 data["message"] 或 RuntimeError
#: 文案里都算）。宁可误判（多等几秒重试）不可漏判——漏判就是白丢一天积分。
_CHECKIN_RATE_LIMIT_MARKERS = ("参与用户太多", "稍后再试", "稍后重试")


def _claim_rate_limited(data: dict[str, Any] | None, err: str = "") -> bool:
    """该次 claim 响应是否命中上游频控（按文案判断——上游无独立错误码）。"""
    text = err + str((data or {}).get("message") or "")
    return any(m in text for m in _CHECKIN_RATE_LIMIT_MARKERS)


def _first_num(obj: dict[str, Any], keys: Sequence[str]) -> float | None:
    """按候选名依次取第一个数值字段；全不命中/非数值返回 None。"""
    for k in keys:
        v = obj.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


# 兼容旧调用点保留（原「全表逐模型探测」的可用性缓存）。2026-10-25 起刷新
# 改为「拉上游目录 + 抄倍率 + 只验证新名字」，不再往这里写；留变量只为外部
# 脚本/旧测试引用时不炸。
_PROBE_CACHE: dict[str, dict[str, bool]] = {}


class TraeProvider(BaseProvider):
    id = "trae"
    # 显示名保持干净的「Trae」：它会进告警横幅（「Trae · 余额告急 · …」）、
    # 卡片标题等用户可见处——早先的「(本地解密直连)」实现备注（用户 2026-10-06
    # 反馈）放在显示名里只有山寨感，实现细节看代码注释就够了。
    name = "Trae"
    # 打卡/积分 API 只有 Trae 上游提供（/ui 自动打卡据此识别）。
    # 海外版上游没有签到端点，TraeIntlProvider 会把它覆写成 False。
    supports_checkin = True
    # 额度/签到分组标签的通道名（``Trae #N · ``）。TraeIntlProvider 覆写成
    # 「Trae 海外版」：两个通道并行在线时各自额度卡的行标如果都叫 ``Trae #N``，
    # 用户分不清哪张是 CN 积分哪张是海外美元（账号定位本身不受影响——序号
    # 都出自 display_index 的全量位次，✕/✎ 不会错绑，这里纯是可读性）。
    _quota_tag = "Trae"

    def __init__(self, base_url: str | None = None, region: str | None = None):
        # region 决定：chat 网关、模型目录、failover 选账号、额度/签到口径。
        # base_url 仍可显式传入（测试注入点/自定义网关），缺省按区域取。
        self._region = resolve_trae_region(region)
        self._base_url = base_url or self._region.chat_base
        # 目录发现快照（上游 config_name -> {display_name, context_window,
        # multimodal}）：「↻ 重新拉取」成功后原子替换，models() 里与静态底表
        # 合并展示。内存态、重启即回底表——与 traeintl / qoder 同款生命周期。
        self._discovered: dict[str, dict[str, Any]] = {}

    def models(self) -> Sequence[dict[str, Any]]:
        # 按区域取模型表：CN 与海外是两套几乎不重叠的目录（GLM/Doubao/Qwen vs
        # Claude/GPT/Gemini 系），必须各用各的——见 config.model_tables。
        model_map, model_ids, _, supports_images = model_tables(self._region.key)
        result = []
        seen = set()

        def _entry(mid: str, **extra: Any) -> dict[str, Any]:
            info = self._discovered.get(mid) or {}
            out = {
                "id": mid,
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                # 倍率走 credit_multiplier：刷新抄回的 consumption_rate 优先，
                # 其次静态底表（两者分工见 config.DISCOVERED_CREDITS 注释）
                "credits": credit_multiplier(self._region.key, mid),
                # 图片能力：与 CodeBuddy 通道同口径（供 /v1/models 的
                # input_modalities 判定），漏报会让客户端误剥图片
                "images": mid in supports_images or bool(info.get("multimodal")),
                "description": "Trae 模型",
            }
            # 展示名只在**真的不同**时给（比 id 时忽略大小写：上游
            # ``MiniMax-M3.1-Flash`` 只是官方排版，别让 /v1/models 多一个
            # 与 id 仅大小写不同的冗余字段）
            display = info.get("display_name")
            if display and display.lower() != mid.lower():
                out["name"] = display
            if info.get("context_window") is not None:
                out["context_window"] = info["context_window"]
            out.update(extra)
            return out

        for m in model_ids:
            if m in seen:
                continue
            seen.add(m)
            result.append(_entry(m))
        # 加别名（外部名映射）——倍率与图片能力均跟随映射到的内部模型
        for external, internal in model_map.items():
            if external not in seen:
                seen.add(external)
                result.append(_entry(external, maps_to=internal,
                                     description=f"Trae 别名 -> {internal}"))
        # 目录发现的新名字排在底表之后（同 traeintl）：底表已有同名条目时
        # 上面的 _entry 已经带上了它的展示名/上下文/读图能力
        for name in sorted(self._discovered):
            if name not in seen:
                seen.add(name)
                result.append(_entry(name, description="Trae 模型（目录发现）"))
        return result

    def ensure_auth(self) -> None:
        _auth()

    async def refresh_models(self, force: bool = False) -> list[dict[str, Any]]:
        """目录「重新拉取」：从上游目录端点拉全量候选 + 抄倍率，再剔失效。

        2026-10-25 改造（用户要求「拉取倍率」）：此前是纯探测——没有可拉的
        目录端点、只能逐模型 ping 然后剔 4001。实测 CN 网关的
        ``get_detail_param`` 返回完整目录，每条带 ``consumption_rate``（**就是
        客户端「模型倍率」面板显示的值**）与展示名/上下文/读图能力，于是：

        1. **拉目录**：CN 走 ``cn_catalog.fetch_cn_catalog``（IN 账号全量并集，
           不剔冷却——目录发现与冷却无关）；海外走继承的 ``intl_catalog``
           路径（``TraeIntlProvider.refresh_models`` 覆写，本方法到那里不执行）。
           单账号失败只 log 跳过；全失败抛错，旧快照保留（models_api 转 502）。
        2. **抄倍率**：写进 ``config.DISCOVERED_CREDITS``（运行时合成，不落盘。
           静态底表是人工核过的值，刷新不覆盖它，重启即回底表）。
        3. **验证可用性**：只对底表/发现里**没有**记录的**新名字**发一条真实
           请求（repo 铁律：上游收下名字 ≠ 能服务），4001/1005 拒收的不并入。
           已有模型不再逐个 ping——旧实现每点一次按钮就全表打一遍，既慢又会
           白付积分。
        4. **合并**：新名字进 ``self._discovered``（展示名/上下文/读图能力）。
        ``force`` 保留兼容通用刷新接口（本实现恒强拉）。"""
        from .config import DISCOVERED_CREDITS
        from .credentials import ensure_account_token

        accounts = [a for a in list_accounts() if (a.region or "cn") == self._region.key]
        if not accounts:
            raise RuntimeError(f"trae({self._region.key}) 没有账号可用于刷新模型目录")

        from . import cn_catalog

        union: dict[str, dict[str, Any]] = {}
        succeeded = 0
        for acct in accounts:
            try:
                await asyncio.to_thread(ensure_account_token, acct.id)
                cred = await asyncio.to_thread(load_account_cred, acct.id)
                if not cred:
                    raise RuntimeError("凭据缺失或无 access_token")
                entries = await asyncio.to_thread(cn_catalog.fetch_cn_catalog, cred)
            except Exception as exc:  # noqa: BLE001 - 单账号失败不阻断并集
                log.warning("trae(%s) 目录刷新跳过账号 %s: %s",
                            self._region.key, acct.id, exc)
                continue
            succeeded += 1
            for e in entries:
                # 按**上游 config_name** 去重（跨账号同一条），元数据与倍率
                # 都是账号无关的，先到先得即可
                union.setdefault(e["name"], e)
        if not succeeded:
            raise RuntimeError(f"trae({self._region.key}) 目录刷新全部账号均未成功")

        _, model_ids, static_credits, _ = model_tables(self._region.key)
        # 对外 id 一律小写（cn_catalog 已归一），与底表 / model_order 同口径
        known = {m.lower() for m in model_ids} | set(self._discovered)
        # 上游真名 -> 对外 id（转发时由 map_model_for 还原大小写，见 MODEL_MAP）
        upstream_by_id = {e["id"]: e["name"] for e in union.values() if e.get("id")}
        verified = await self._verify_candidates(
            [e["id"] for e in union.values() if e.get("id") not in known])

        discovered: dict[str, dict[str, Any]] = {}
        for mid in verified + [e["id"] for e in union.values() if e.get("id") in known]:
            e = next((x for x in union.values() if x.get("id") == mid), None)
            if e is None:
                continue
            discovered[mid] = {
                "display_name": e.get("display_name"),
                "context_window": e.get("context_window"),
                "multimodal": e.get("multimodal"),
                "upstream": upstream_by_id.get(mid),
            }
            if e.get("rate") is None:
                continue
            # 静态底表有的模型不抄倍率（人工核过的活动折扣口径），只补新名字
            if mid not in {m.lower() for m in static_credits}:
                DISCOVERED_CREDITS[mid] = f"x{float(e['rate']):.2f}"
        # 快照只替换「底表里没有的新名字」，底表的元数据补充合并进去（不因
        # 某次刷新少了一条就丢展示名/上下文）
        self._discovered = {**self._discovered, **discovered}
        log.info("trae(%s) 目录刷新：上游 %d 条，新收录 %d 个，倍率抄回 %d 条",
                 self._region.key, len(union), len(verified), len(DISCOVERED_CREDITS))
        return list(self.models())

    async def _verify_candidates(self, names: list[str]) -> list[str]:
        """新名字逐个发一条最小真实请求，只留上游真能服务的。

        判定口径：``_probe_model`` 返回 False（4001 「参数非法」——名字/客户端
        版本不被接受）即不收；**能解析出错误码的其它拒绝**（1005 会员档位不足、
        4011 日额度等）说明名字本身有效、只是当前账号没权限，照收——它们与
        已收录模型同属「按账号诚实报错」的一类。网络/凭据瞬态按可用保留。
        """
        ok_names: list[str] = []
        for name in names:
            try:
                ok = await asyncio.to_thread(self._probe_model, name)
            except Exception:  # noqa: BLE001 - 网络/凭据瞬态：按可用保留
                ok = True
            if ok:
                ok_names.append(name)
            else:
                log.info("trae(%s) 新模型 %s 被上游拒绝（4001），不收录",
                         self._region.key, name)
        return ok_names

    def _probe_model(self, model: str) -> bool:
        """单模型最小真实请求。返回 True=可用；False=上游明确拒绝（4001）。
        其它异常向上抛（调用方按瞬态处理，不剔除）。"""
        work = _load_work_cred()
        try:
            if work and work.get("access_token"):
                raw = _send_trae_work_chat(
                    [{"role": "user", "content": [{"type": "text", "text": "ping"}]}],
                    model, False, work)
            else:
                raw = _send_native_chat(
                    _native_messages([{"role": "user", "content": "ping"}]),
                    model, False, None)
        except HTTPException as e:
            # 4001 只在 200+SSE error / 400 壳里出现；401/403/502 是凭据或
            # 网关问题，与模型无关——按可用保留（不剔除）
            if e.status_code == 400:
                return False
            raise
        # 200 + SSE：看首个事件是不是 error:4001（solo_work_lite 下被拒的形态）
        return not _native_rejected(raw)

    def _send_native_request(
        self,
        native_msgs: list[dict[str, Any]],
        model: str,
        stream: bool,
        tools: list[dict[str, Any]] | None,
    ) -> str:
        """Provider 级发送钩子；PAT 子类覆盖后不会因重叠模型串到个人账号。"""
        return _send_native_chat(native_msgs, model, stream, tools)

    def _keeps_native_error(self, model: str) -> bool:
        """PAT 子类覆写为 True，避免把其真实错误回落到个人文本通道。"""
        return False

    def _uses_native_mode(self) -> bool:
        """是否使用原生传输；PAT 子类始终启用以隔离账号体系。"""
        return _NATIVE_TOOLS_ENABLED

    def _stream_native_events(
        self,
        native_msgs: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None,
        stop: threading.Event,
    ):
        """PAT 子类覆盖为增量事件源；个人通道继续沿用整段兼容路径。"""
        return None

    # ---- 打卡 / 额度（/ui 管理页消费，均经 asyncio.to_thread 调用） ----

    def _work_accounts(self) -> list[Any]:
        """遍历用的 work 账号列表（按 failover 顺位，剔除冷却中的）。

        PAT 子类（``_pat_variant``）不经过 work 多账号体系——签到/额度是
        个人账号专属，PAT 的 ``supports_checkin=False`` 本就挡住 UI 调度，
        这里再兜一层：PAT 返回空列表，调用方各自退化为单账号（拿首个可用
        work 账号）或返回 None（与改造前 PAT 行为一致）。
        """
        if self._is_pat_variant():
            return []
        # 只遍历本区账号——CN / 海外账号不通用（连错域 401），额度与签到请求
        # 打错区就是白付一次失败往返，还会把海外的美元 Usage 混进 CN 的积分卡。
        # PAT 子类的 region 恒为 cn（见 pat/chat.py 注释），过滤对它无影响。
        return failover.available_accounts(self._region.key)

    def _checkin_accounts(self) -> list[Any]:
        """签到遍历的账号列表：与 :meth:`_work_accounts` 唯一差别是**不剔除冷却**。

        冷却挡的是模型转发（quota/限流退避），签到的 billing 面与它无关；
        按冷却过滤会把在冷却的账号整条从签到卡里藏掉（2026-10-07 用户实报
        codebuddy 同病：双号只显示一个、「每日」只算一半）。同区过滤保留
        （CN/海外账号不通用）；PAT 子类照旧走各自的 checkin 覆写，不经这里。
        """
        if self._is_pat_variant():
            return []
        region = self._region.key
        return [a for a in list_accounts() if getattr(a, "region", None) == region]

    def checkin_status(self) -> dict[str, Any] | None:
        """查今日签到状态。多账号**都查**，聚合：任一账号可领→可领；
        全部已签→已签；个别账号失败只在日志记、不阻塞整页（与 qoder 一致）。"""
        accounts = self._checkin_accounts()
        if not accounts:
            # 单账号（legacy 迁移前 / PAT 兜底）：走首个可用账号
            try:
                data = fetch_checkin_status(region=self._region.key)
            except Exception as e:  # noqa: BLE001 — 状态查询失败不该让整页 500
                log.warning("trae 签到状态查询失败: %s", e)
                return {"checked_in": False, "claimable": False, "message": str(e)[:200]}
            return self._checkin_status_one(data, label="", multi=False)

        if len(accounts) == 1:
            st, _err = self._checkin_status_for_account(accounts[0], index=1, multi=False)
            return st

        # 多账号：并查，聚合任一可领 / 全部已签；per-account 明细放 ``accounts``
        # （管理页签到卡逐账号渲染「#N 已签 / 可领 / 查询失败」）。
        claimable_accts: list[str] = []
        signed_accts: list[str] = []
        failed_accts: list[str] = []
        acct_details: list[dict[str, Any]] = []
        enabled_any = False
        total_daily = 0.0  # 「每日 +X」chip 用：多账号显示总和（前端带「（N账号）」）
        # 展示序号一律取 failover.display_index()（与 /ui 账号快照同源）。
        # 不能用 enumerate 的位置：accounts 是「本区 + 未冷却」子集，位次与
        # 快照对不上，前端会把签到行对到别的账号上（✎ 改名改错人）。
        didx = failover.display_index()
        for pos, acct in enumerate(accounts, 1):
            i = didx.get(acct.id, pos)
            # 显示名 alias 优先（管理页 ✎ 改的名，签到卡与额度面板保持一致）；
            # id 一并下发——签到明细行的 ✎ 改名按钮要拿它定位账号。
            name = acct.alias or acct.nickname or acct.uid or acct.id
            st, err = self._checkin_status_for_account(acct, index=i, multi=True)
            if st is None:
                failed_accts.append(f"#{i}")
                acct_details.append({"index": i, "id": acct.id, "name": name,
                                     "error": err or "查询失败"})
                continue
            if not st.get("inactive"):
                enabled_any = True
            if isinstance(st.get("daily_credit"), (int, float)):
                total_daily += st["daily_credit"]
            if st.get("claimable"):
                claimable_accts.append(f"#{i}")
            elif st.get("checked_in"):
                signed_accts.append(f"#{i}")
            acct_details.append({
                "index": i,
                "id": acct.id,
                "name": name,
                "checked_in": bool(st.get("checked_in")),
                "claimable": bool(st.get("claimable")),
                "inactive": bool(st.get("inactive")),
                "daily_credit": st.get("daily_credit"),
            })
        status: dict[str, Any] = {
            "checked_in": bool(signed_accts) and not claimable_accts,
            "claimable": bool(claimable_accts),
            "inactive": not enabled_any and not claimable_accts and not signed_accts,
            "message": "",
            "accounts": acct_details,
        }
        if claimable_accts:
            status["message"] = f"账号 {'、'.join(claimable_accts)} 可领"
        elif signed_accts:
            status["message"] = f"账号 {'、'.join(signed_accts)} 已签"
        if failed_accts:
            status["message"] = (status["message"] + " " if status["message"] else "") + \
                f"（{len(failed_accts)}/{len(accounts)} 个账号查询失败）"
        if total_daily:
            status["daily_credit"] = total_daily
        if enabled_any:
            status["next_ts"] = next_daily_reset()
            status["next_ts_source"] = SOURCE_INFERRED
        return status

    def _checkin_status_for_account(
            self, acct: Any, *, index: int, multi: bool) -> tuple[dict[str, Any] | None, str]:
        """单账号签到状态查询。返回 ``(status, error)``：查询失败时 status 为
        None、error 带截断后的原因（进 per-account 明细的 title，光「查询失败」
        说不清是 token 过期还是网络问题）。"""
        try:
            token, _cred = ensure_account_token(acct.id)
            data = fetch_checkin_status(token=token, account_id=acct.id,
                                       region=self._region.key)
        except Exception as e:  # noqa: BLE001 — 单账号失败不该让整页 500
            log.warning("trae 签到状态查询失败（%s）: %s", acct.id, e)
            return None, str(e)[:120]
        label = f"{self._quota_tag} #{index} · " if multi else ""
        return self._checkin_status_one(data, label=label, multi=multi), ""

    def _checkin_status_one(
            self, data: dict[str, Any], *, label: str, multi: bool) -> dict[str, Any]:
        """把上游 ``/ug/checkin_credits/status`` 原始响应收拢成统一签到状态。"""
        checked_in = bool(data.get("checked_in"))
        enabled = bool(data.get("enable", True))
        status: dict[str, Any] = {
            "checked_in": checked_in,
            "claimable": enabled and not checked_in,
            "inactive": not enabled,
            "message": data.get("message", ""),
            # 每日可得 = credits + extra_credits（语义实测 2026-10-07：双账号
            # 卡用户期望日入 400 → 每账号 200 = 两字段各 100 的和；单看哪个
            # 都只有一半）。若哪天 chip 数字对不上账，先怀疑上游改了字段口径。
            "daily_credit": (data.get("credits") or 0) + (data.get("extra_credits") or 0),
        }
        # trae 的 ``/ug/checkin_credits/status`` 返回里**没有任何时间字段**
        # （实测只有 checked_in / enable / credits / message），连档期都不给。
        # 轮换时刻只能按 logs/checkin.jsonl 反推的本地零点算（09-27 01:56、
        # 02:20 这类凌晨领取也被记为新一天 → 零点轮换），故标 inferred。
        # 活动未开（enable=false）时不给：那会儿连有没有下一轮都不知道。
        if enabled:
            status["next_ts"] = next_daily_reset()
            status["next_ts_source"] = SOURCE_INFERRED
        return status

    def checkin_claim(self) -> dict[str, Any] | None:
        """领取今日签到积分。多账号**每个都领**（用户决策），逐个尝试、
        各自容错：单个账号失败（网络/token/已领过）不阻塞其它账号。汇总
        ``extra_credits`` 为各账号实领之和，``message`` 据实说明每个账号结果。

        多账号**串行 + 节流**（用户 2026-10-06 要求）：实测双账号背靠背连打，
        第二个必中「当前参与用户太多，请稍后再试」——上游把短窗口内的连续
        claim 识别为刷量限流。账号间强制间隔 ``_CHECKIN_THROTTLE_S``，命中
        限流文案再按 ``_CHECKIN_RETRY_DELAYS`` 退避重试。本方法经 benefits 层
        ``asyncio.to_thread`` 跑（``_call``），``time.sleep`` 不阻塞事件循环；
        手动点击的 HTTP 响应最坏多等约 15s（两次重试），前端 fetch 无超时，可接受。
        """
        accounts = self._checkin_accounts()
        if not accounts:
            # 单账号 legacy 兜底（PAT 由 supports_checkin=False 挡住 UI 调度，
            # 走不到这里；即便走到也退回原单账号语义）
            data = claim_checkin_credits(region=self._region.key)
            if data.get("code") not in (0, None):
                raise RuntimeError(data.get("message") or json.dumps(data, ensure_ascii=False)[:200])
            return {
                "checked_in": True,
                "extra_credits": data.get("credits_granted", data.get("extra_credits")),
                "message": data.get("message", ""),
            }
        if len(accounts) == 1:
            # 单账号直通：不带 accounts 明细（与 checkin_status 的单账号分支对称，
            # 徽标本身就是它的状态，逐账号展开纯属噪音）
            acct = accounts[0]
            try:
                token, _cred = ensure_account_token(acct.id)
                data = claim_checkin_credits(token=token, account_id=acct.id,
                                            region=self._region.key)
            except Exception as e:  # noqa: BLE001 — 与多账号循环同款容错
                log.warning("trae 签到领取失败（%s）: %s", acct.id, e)
                raise RuntimeError(str(e)[:200]) from e
            if data.get("code") not in (0, None):
                raise RuntimeError(data.get("message") or json.dumps(data, ensure_ascii=False)[:200])
            granted = data.get("credits_granted", data.get("extra_credits"))
            return {
                "checked_in": True,
                "extra_credits": granted,
                "message": data.get("message", ""),
            }

        total_credits: float = 0.0
        any_claimed = False
        any_granted = False  # replay（已领过）拿不到金额：别把「未知」记成 0
        messages: list[str] = []
        acct_details: list[dict[str, Any]] = []
        # 展示序号取 failover.display_index()（与 /ui 账号快照同源，理由同
        # checkin_status）；**节流**用的是本轮循环位置 pos，两者必须分开：
        # 展示序号是「这账号在管理页排第几」（跨区、含冷却账号都占位），
        # 节流要的是「是不是本轮第一个打的」——拿展示序号判会把「快照里的
        # 第 3 个、但本轮第 1 个」也睡一拍（无谓等待），更糟的是若某轮只领
        # 快照第 2、3 号，`i > 1` 对两者都成立，第 1 个也白睡。
        didx = failover.display_index()
        for pos, acct in enumerate(accounts, 1):
            i = didx.get(acct.id, pos)
            # 显示名 alias 优先（同 checkin_status）；id 下发供明细行 ✎ 定位
            name = acct.alias or acct.nickname or acct.uid or acct.id
            tag = f"#{i}"
            # 串行节流：从第 2 个账号起先等一段再打（实测零间隔必中频控——
            # 见 _CHECKIN_THROTTLE_S 注释）。异常路径同样等：哪怕上一个账号
            # 是网络失败，下一个也照常歇一拍，节奏一致才像「人在操作」。
            if pos > 1:
                time.sleep(_CHECKIN_THROTTLE_S)
            msg = ""
            ok = False
            granted: float | None = None
            last_rl = False
            device_rejected = False  # 本账号最终是不是指纹池尽（见失败分支）
            for attempt in range(len(_CHECKIN_RETRY_DELAYS) + 1):
                last_rl = False
                try:
                    token, _cred = ensure_account_token(acct.id)
                    data = claim_checkin_credits(token=token, account_id=acct.id,
                                                region=self._region.key)
                except Exception as e:  # noqa: BLE001 — 单账号失败不阻塞其它账号
                    log.warning("trae 签到领取失败（%s）: %s", acct.id, e)
                    msg = str(e)[:60]
                    last_rl = _claim_rate_limited(None, msg)
                    if last_rl and attempt < len(_CHECKIN_RETRY_DELAYS):
                        time.sleep(_CHECKIN_RETRY_DELAYS[attempt])
                        continue
                    break
                if data.get("code") in (0, None):
                    ok = True
                    g = data.get("credits_granted", data.get("extra_credits"))
                    if isinstance(g, (int, float)):
                        granted = float(g)
                    msg = str(data.get("message") or "")[:80]
                    break
                msg = str(data.get("message"))[:60]
                # 9074 池尽（device_rejected）= 指纹黑名单，换机都没救回来，
                # 5/10s 退避后再来也是同样的 4 台设备——按最终失败处理
                device_rejected = bool(data.get("device_rejected"))
                last_rl = (not device_rejected) and _claim_rate_limited(data)
                if last_rl and attempt < len(_CHECKIN_RETRY_DELAYS):
                    log.info("trae 签到限流（%s），%.0fs 后重试 %d/%d",
                             acct.id, _CHECKIN_RETRY_DELAYS[attempt],
                             attempt + 1, len(_CHECKIN_RETRY_DELAYS))
                    time.sleep(_CHECKIN_RETRY_DELAYS[attempt])
                    continue
                break
            if ok:
                any_claimed = True
                if granted is not None:
                    total_credits += granted
                    any_granted = True
                messages.append(f"{tag} 已领 {granted or ''}".strip())
                acct_details.append({
                    "index": i, "id": acct.id, "name": name, "ok": True,
                    "credits": granted,
                    "message": msg,
                })
            else:
                # 重试耗尽仍限流：说明「稍后再试」——别写死「失败」，
                # message 保持上游原话，用户稍后手动再点一次即可补上。
                # 指纹池尽（device_rejected）则必须翻译：上游原话是「当前参与
                # 用户太多」的烟幕，照搬会把人带去查限流——2026-10-06 实锤真因
                # 是设备指纹黑名单（换满 4 台备选机都救不回来），告诉用户真实
                # 原因和自愈方式（稍后自动重试/查 devices.json）才对。
                if device_rejected:
                    msg = ("设备指纹被上游拒（已自动换 4 台设备仍失败），"
                           "稍后自动重试；持续失败删除 ~/.buddy-proxy/trae/"
                           "devices.json 对应条目重派")
                messages.append(f"{tag} 失败：{msg}")
                acct_details.append({"index": i, "id": acct.id, "name": name,
                                     "ok": False, "message": msg})
        return {
            "checked_in": any_claimed,
            "extra_credits": total_credits if any_granted else None,
            "message": "；".join(messages) or "没有可领取的账号",
            "accounts": acct_details,
        }

    def quota(self) -> dict[str, Any] | None:
        """查额度（``/ug/usage``），多账号并发。多账号时各账号条目带
        ``Trae #N · `` 前缀供前端分组；个别账号失败插 ``query_failed``
        说明条（benefits 层认这个标记走短缓存）。"""
        # 遍历**本区全量**账号（与签到同口径），不剔除冷却中的：冷却挡的是
        # 模型转发（quota/限流退避），与额度查询的 billing 面无关。走
        # available_accounts 会把在冷却的账号整条从额度面板藏掉——2026-10-11
        # 用户实报：3 个 cn 号、2 个挂 quota 冷却，面板只剩 1 张卡。#130 修
        # 签到时漏了这条路径（当时注释「额度/转发路径不动」，转发该不动、
        # 额度不该）。display_index 的编号对全量位次天然一致，不引入错位。
        accounts = self._checkin_accounts()
        if not accounts:
            # 单账号 legacy 兜底（迁移前 / PAT）：保持原行为
            try:
                data = fetch_ent_usage(region=self._region.key)
            except Exception as e:  # noqa: BLE001 — 额度查询失败不阻塞整页
                log.warning("trae 额度查询失败: %s", e)
                return None
            items = self._quota_items(data, label_prefix="")
            return {"items": items, "level": None}

        multi = len(accounts) > 1
        # 展示序号取 failover.display_index()（与 /ui 账号快照同源）。这里曾经
        # 是 enumerate 的位置，失败说明条里还另用过 ``priority + 1``——三套编号
        # 各说各话：额度块按快照序号对上账号后，✕ 删除 / ▲▼ 顺位 / ✎ 改名拿到的
        # 是**别的账号**的 id，删号是不可逆的（凭据文件一并 unlink）。
        # display_index 用全量列表位次，快照与标签必然同源。
        didx = failover.display_index()
        if multi:
            pool = _quota_executor()
            futures = [pool.submit(self._quota_one, a, didx.get(a.id, n + 1), multi=True)
                       for n, a in enumerate(accounts)]
            deadline = time.monotonic() + _QUOTA_ROUND_DEADLINE_S
            items: list[dict[str, Any]] = []
            failed: list[str] = []
            for acct, fut in zip(accounts, futures):  # 按 failover 顺位收集，UI 顺序稳定
                name = f"#{didx.get(acct.id, 0) or '?'}"
                try:
                    its, ok = fut.result(timeout=max(deadline - time.monotonic(), 0.05))
                except Exception:  # noqa: BLE001 - 超时/异常账号都算失败
                    its, ok = [], False
                if not ok:
                    failed.append(name)
                else:
                    items.extend(its)
            if failed:
                items.insert(0, {
                    "label": "Trae 额度查询失败",
                    "used": None, "total": None,
                    "remaining": f"{len(failed)}/{len(accounts)} 个账号取不到额度"
                                 f"（{'、'.join(failed)}）",
                    "percent": None, "reset_ts": None,
                    "query_failed": True,
                })
        else:
            its, ok = self._quota_one(accounts[0], didx.get(accounts[0].id, 1), multi=False)
            items = its if ok else []

        return {"items": items, "level": None}

    def quota_epoch(self) -> str:
        """quota 缓存代：账号列表一变（登录新号/删号/换顺位）旧快照就该作废。"""
        try:
            accts = list_accounts()
        except Exception:  # noqa: BLE001 - 拿不到就退回常量键
            return "unknown"
        return ",".join(f"{a.id}#{a.priority}" for a in accts) or "empty"

    def _quota_one(self, acct: Any, index: int, *, multi: bool
                   ) -> tuple[list[dict[str, Any]], bool]:
        """单账号额度查询：``(items, ok)``。同步跑在常驻线程池里。"""
        try:
            token, _cred = ensure_account_token(acct.id)
            data = fetch_ent_usage(token=token, account_id=acct.id,
                                  region=self._region.key)
        except Exception as e:  # noqa: BLE001 — 单账号失败不阻塞整页
            log.warning("trae 额度查询失败（%s）: %s", acct.id, e)
            return [], False
        prefix = f"{self._quota_tag} #{index} · " if multi else ""
        return self._quota_items(data, label_prefix=prefix), True

    def _quota_items(self, data: dict[str, Any], *, label_prefix: str) -> list[dict[str, Any]]:
        """把上游 ``/ug/usage`` 原始响应收拢成统一额度条目列表。

        **计费口径分叉**：CN 是积分制（``unit="credit"``），海外是美元 Usage
        余额制（``unit="dollar"``）。判据用**响应自带的** ``is_dollar_usage_billing``
        flag，而不是 ``self._region.billing``——两区共用同一个
        ``ide_user_ent_usage`` 接口，flag 才是上游对「这份额度按什么计价」的
        权威声明；区域只作 flag 缺失时的兜底（老快照/字段改名都不至于把美元
        报成积分）。数字字段两侧同名（``total_amount``/``consumed_amount``/
        ``credits_limit``/``credits_amount``），所以只换量纲标签与展示单位。

        量纲标签不能沿用 ``credit``：``benefits._quota_low`` 对 credit 通道用
        **绝对值** 300 门槛判「余额告急」，$20 的海外套餐会被天天误报；改成
        ``dollar`` 后它走非 credit 分支，按剩余占比判定（与 day/count/permille
        同款），才是对的口径。
        """
        dollar = bool(data.get("is_dollar_usage_billing")) or (
            not data.get("is_credits_billing") and self._region.billing == "dollar")
        unit = "dollar" if dollar else "credit"
        us = data.get("usage_summary", {})
        items: list[dict[str, Any]] = []
        total, consumed = us.get("total_amount"), us.get("consumed_amount")
        if total is not None:
            ratio = us.get("consumption_ratio")
            percent = round(ratio * 100) if isinstance(ratio, (int, float)) else None
            remaining = None
            if isinstance(total, (int, float)) and isinstance(consumed, (int, float)):
                remaining = round(total - consumed, 2)
            items.append({"label": f"{label_prefix}总额度", "used": consumed, "total": total,
                          "remaining": remaining, "percent": percent,
                          # 总额度是所有包的合计，没有单一到期日——到期告警
                          # 由下面各权益包自己承担，合计行不参与
                          "reset_ts": None, "expire_ts": None, "unit": unit,
                          # head_only：只在标题行「剩 X / Y」用它的数字，明细
                          # 列表不单列这一条（它是下面各权益包的合计，再铺一条
                          # 带进度条的明细行是重复——用户 2026-10-04 反馈）。
                          "head_only": True})
        # 权益包：每条都有额度与到期时间，全部展示（各条**不能相加**——它们
        # 是上面「总额度」的明细，加了就重复计算，故本通道不给 sum_items）。
        packs: list[dict[str, Any]] = []
        seen: set[tuple] = set()
        for p in data.get("user_entitlement_pack_list", []):
            eb = p.get("entitlement_base_info") or {}
            end_time = eb.get("end_time")
            if not end_time:
                continue
            desc = p.get("display_desc") or "权益包"
            quota = eb.get("quota") or {}
            # 去重按「名字 + 权益 id + 到期日」三元组，只防上游返回重复行。
            # **不能**只按名字去重：那样 25 条「签到奖励」会被合并成 1 条，
            # 12 条尚未消费的额度直接从界面上消失（2026-10-03 实测：升级前
            # 只显示前 3 条，其余 24 个包全被 `desc in seen` 加 `len>=3` 截掉）。
            key = (desc, str(eb.get("entitlement_id") or ""), int(end_time))
            if key in seen:
                continue
            seen.add(key)
            used: float | None = None
            limit_v: float | None = None
            rows: list[tuple[str, float | None, float | None, str]] = []
            if dollar and ("premium_model_fast_request_limit" in quota
                           or "basic_usage_limit" in quota):
                if p.get("is_hide"):
                    # 海外用 is_hide 标记「UI 不展示」的包（实测：Promo Code
                    # 包全 0 配额仍会出现在列表里）。只在海外结构下过滤——
                    # CN 快照没有这个字段，别让新过滤改变 CN 行为。
                    continue
                # 海外实测结构（2026-10-06，Pro plan 快照）：额度字段与 CN
                # 完全不同名，且**一个包里混两种量纲**——Premium 快速请求是
                # 次数（``premium_model_fast_request_limit: 600``），Basic 是
                # 美元（``basic_usage_limit: 20`` + 已用
                # ``usage.basic_usage_amount``）。原通用键表
                # （``_LIMIT_KEYS_DOLLAR`` 命中 credits_limit=0）在这里只能
                # 解析出 0/0 的空条目，这就是「海外版查不到额度」的根因。
                # 两条量纲分开成两行，不能相加也不能共用一个 percent。
                fast = _first_num(quota, ("premium_model_fast_request_limit",))
                if fast is not None and fast > 0:
                    # 已用次数上游不给（usage 只有 basic/bonus/credits_amount
                    # 三个键）——used=None 显示「—」，好过拿美元已用配次数
                    # 上限算出一个错百分比对用户撒谎。
                    rows.append(("Premium 快速请求", None, float(fast), "count"))
                slow = _first_num(quota, ("premium_model_slow_request_limit",))
                basic = _first_num(quota, ("basic_usage_limit",))
                if basic is not None and basic > 0:
                    amt = _first_num(p.get("usage") or {}, ("basic_usage_amount",))
                    rows.append(("Basic 用量", round(float(amt), 4) if amt is not None else None,
                                 float(basic), "dollar"))
                # slow / advanced / auto_completion 的 -1 是「无限」，不占
                # 条目——无限没有进度可画，铺出来只是噪声。
            limit = None if rows else _first_num(
                quota, _LIMIT_KEYS_DOLLAR if dollar else _LIMIT_KEYS_CREDITS)
            if not rows and limit is not None and limit > 0:
                limit_v = float(limit)
                # ``*_amount`` 是**已用**不是剩余——2026-10-03 实测交叉
                # 校验：Σlimit=9500.0、Σamount=6257.1772，Σlimit-Σamount 与
                # 接口自报 remaining（3242.82）差 0.00，而「amount=剩余」的
                # 假设差 3014.36。算反会把「剩 3242」显示成「剩 6257」。
                #
                # ``usage`` 缺失按 0 已用算（= 没花），**不是**「未知」：有
                # 记录的包已用合计 6257.1772 恰好等于 usage_summary 的
                # consumed_amount，且消耗严格按到期日 FIFO（先扣 4000 的会员
                # 包、再扣最早到期的签到包），未出现的包都是到期更晚、还没轮到
                # 的——真·满额。这与 ``trae/pat/quota.py`` 的相反先例不是一回
                # 事：那里是网关偶发只回半拉数据（包容量回来了、用量没回来），
                # 按 0 算会把未知说成满血；这里是接口语义，值为 0 就是没消费。
                amount = _first_num(p.get("usage") or {},
                                    _AMOUNT_KEYS_DOLLAR if dollar else _AMOUNT_KEYS_CREDITS)
                used = round(float(amount), 2) if amount is not None else 0.0
            if not rows:
                rows.append(("", used, limit_v, unit))
            for suffix, row_used, row_total, row_unit in rows:
                packs.append({
                    "label": f"{label_prefix}{desc}{(' · ' + suffix) if suffix else ''}",
                    "used": row_used,
                    "total": round(row_total, 2) if row_total else None,
                    "percent": (round(row_used / row_total * 100)
                                if row_used is not None and row_total else None),
                    "remaining": (round(row_total - row_used, 2)
                                  if row_used is not None and row_total else None),
                    # 权益包只有到期、没有周期性重置，故 reset_ts 恒 None
                    "reset_ts": None,
                    "expire_ts": int(end_time),
                    "unit": row_unit,
                })
        packs.sort(key=lambda it: it["expire_ts"] or 0)  # 先到期的排前面
        items.extend(packs)
        return items

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        requested_model = body.get("model", "auto")
        messages = body.get("messages", [])
        stream = bool(body.get("stream", False))
        # coding agent 请求：不注入 guard、不清洗——下游自己解析工具调用语法
        agent_mode = _looks_like_agent_request(messages, body)
        _debug_dump(
            "debug_trae_route",
            model=requested_model,
            agent_mode=agent_mode,
            tools_count=len(body.get("tools") or []),
            stream=stream,
            message_count=len(messages),
        )

        tools = body.get("tools") or []
        # 原生通道：全部请求（含纯聊天）默认走 chat_v3 直通——该通道无 agent
        # 预设，纯聊天不再需要 guard 注入与泄漏清洗。上游 4001 拒绝时自动回落
        # 文本协议（prompt 始终照算，它就是兜底路径的输入：纯聊天带 guard、
        # 工具请求带教学）。
        native_mode = self._uses_native_mode()
        prompt = _extract_prompt(
            messages, guard=not agent_mode, tools=tools if agent_mode else None
        )
        if not prompt:
            raise HTTPException(status_code=400, detail="no text content")
        native = (
            {"messages": _native_messages(messages), "tools": tools}
            if native_mode else None
        )

        # ---- 多账号 failover：按顺位试可用 work 账号，账号级错误冷却换号 ----
        # PAT 子类（TraePatProvider）覆写了发送路径且有自己的账号体系，不该被卷
        # 进 work 多账号循环——直接单次转发（保持它原有的单账号语义）。
        if self._is_pat_variant():
            set_current_work_account(None)
            try:
                return await self._forward_once(
                    body, protocol, original, requested_model, prompt,
                    messages, tools, native, stream, agent_mode,
                )
            finally:
                set_current_work_account(None)

        # 只在本区账号间 failover——CN / 海外账号不通用（连错域 401）。
        # **不做** ``or available_accounts()`` 全量兜底：那会让 CN 通道在只有海外
        # 账号时捞到海外账号（反之亦然），每轮白付一次 401。现有账号的 region
        # 已由 list_accounts 归一成 "cn"（credentials.py），过滤本身就覆盖到它们。
        # 空列表时区分「本区无账号但别区有」（401，配置问题）与「全在冷却」（429）。
        accounts = failover.available_accounts(self._region.key)
        if not accounts:
            other = failover.available_accounts()
            if other:
                raise HTTPException(
                    status_code=401,
                    detail={"error": {
                        "message": (f"trae 无 {self._region.label} 区域的可用账号"
                                    f"（已有 {len(other)} 个账号属于其它区域，"
                                    f"跨区账号间不自动切换）"),
                        "type": "authentication_error"}})
            raise HTTPException(
                status_code=429,
                detail={"error": {
                    "message": (f"trae work 所有账号均在冷却中：{failover.cooldown_report()}"
                                "；额度冷却到点自动恢复"),
                    "type": "rate_limit_error"}})
        last_exc: BaseException | None = None
        try:
            for i, acct in enumerate(accounts):
                set_current_work_account(acct.id)
                # metrics 账号归属（/ui 请求日志「通道」列的账号后缀）：qoder/kimi/
                # antigravity 同款——选号即写，failover 后被覆盖为最终账号。holder
                # 是 codebuddy observability._instrument 放入的 dict，离线/测试等
                # 未经 _instrument 的场景为 None，直接跳过。PAT 子类不走这里
                # （pat/chat.py 自己写 meta["account"]）。
                # 落库统一写稳定 id，名字由展示端按当前 alias 链解析
                # （web/ui/queries._account_name_map）——写死名字的话改名后
                # 历史日志行跟不上。
                meta = ACCOUNT_META.get()
                if meta is not None:
                    meta["account"] = acct.id
                try:
                    return await self._forward_once(
                        body, protocol, original, requested_model, prompt,
                        messages, tools, native, stream, agent_mode,
                    )
                except HTTPException as e:
                    last_exc = e
                    # 账号级错误（401 凭据失效 / 429 额度——含上游额度码映射
                    # 而来的 429，如 4008/4011/4021/4031）：**先冷却再决定换不
                    # 换**。此前只在前头还有账号时才冷却，最后一个账号失败不落
                    # 冷却——下一轮请求还会先打它、再白吃一次同样的失败才轮到
                    # 别人。其余错误（502 通道级/业务 4xx）换号无意义，直接透传。
                    if self._is_account_error(e):
                        failover.mark_cooldown(
                            acct.id, quota=e.status_code == 429,
                            reason=f"HTTP {e.status_code}: {str(e.detail)[:120]}")
                        if i < len(accounts) - 1:
                            continue
                    raise
                finally:
                    set_current_work_account(None)
        except HTTPException as e:
            # 走到这是「最后一个账号也失败」或「非账号级错误透传」。anthropic 协议
            # 的错误体用标准形状（Claude Code 等 SDK 靠它渲染错误）；openai 协议
            # 维持「抛 HTTPException → 外层 exception handler 转 JSON」不变。
            if protocol == "anthropic":
                return JSONResponse(
                    status_code=e.status_code,
                    content={
                        "type": "error",
                        "error": {"type": "api_error", "message": str(e.detail)},
                    },
                )
            raise
        # 全部账号失败（循环正常跑完不该到这，兜底）
        raise last_exc or HTTPException(status_code=429, detail=failover.cooldown_report())

    def _is_pat_variant(self) -> bool:
        """PAT 子类标记：TraePatProvider 覆写 ``_pat_variant=True``（见
        pat_provider.py）。work 多账号 failover 只服务个人 work 通道。"""
        return bool(getattr(self, "_pat_variant", False))

    @staticmethod
    def _is_account_error(e: HTTPException) -> bool:
        """该 HTTPException 是否「换下一个账号可能好转」。

        401 = 凭据失效（换号有意义）；403/429 = 额度/权益/限流（换号有意义
        ——transport/native_tools 透传上游 403 后这里必须接住，否则它穿透
        failover 直接冒给客户端且坏账号永不被冷却；与 failover.py 模块注释
        的「401 与 403/429」口径一致）。502/504 是通道级（换号无意义，所有
        账号同网关）。其余业务 4xx 换号也无意义。
        """
        return e.status_code in (401, 403, 429)

    async def _forward_once(
        self, body: dict[str, Any], protocol: str, original: dict[str, Any] | None,
        requested_model: str, prompt: Any, messages: list[dict[str, Any]],
        tools: list[dict[str, Any]], native: dict[str, Any] | None,
        stream: bool, agent_mode: bool,
    ) -> StreamingResponse | JSONResponse:
        """单账号实际转发（forward 的 failover 循环体）。"""
        if stream:
            include_usage = bool(
                (body.get("stream_options") or {}).get("include_usage"))
            # anthropic 协议没有 stream_options 字段，但 message_delta 需要
            # usage（Claude Code 靠它统计 token），这里强制向 _stream 索取
            if protocol == "anthropic":
                include_usage = True
            raw_gen = self._stream(prompt, requested_model,
                                   sanitize=not agent_mode, tools=tools or None,
                                   include_usage=include_usage, native=native)
            # 首事件闸门：预驱动**原始** _stream 到第一个非心跳事件再决定放行
            # 还是换号。trae 的 _stream 在等上游时会先吐「: heartbeat」保活——
            # 那是给下游超时看的，此刻 StreamingResponse 还没建、客户端一个字节
            # 都没收到，丢掉心跳无影响；第一个语义事件前的账号级错误（读线程透
            # 传）此刻抛出可安全换号（还没向客户端吐过内容，防重复计费）。见到
            # 语义事件即 committed，绝不重放。anthropic 包装在闸门之后（包住
            # 「缓冲 + 续跑」的拼接流）。
            # 闸门会同步调用 next(_stream)，而 _stream 首事件前可能等上游很久。
            # 不能在唯一的 asyncio loop 上预驱动，否则一次 Trae 慢响应就拖住
            # 管理页和其它通道的所有请求。
            gated = await _gate_first_event_async(raw_gen)
            if isinstance(gated, BaseException):
                raise gated
            replay = _replay_prefixed(gated.buffered, gated.gen)
            gen = _wrap_anthropic_stream(replay, requested_model) \
                if protocol == "anthropic" else replay
            return StreamingResponse(
                _sync_to_async_iter(gen),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "close"},
            )
        # 非流式聚合内部是同步 urllib 调用（最长 180s），放线程池执行，
        # 避免阻塞事件循环拖垮所有并发请求。
        # 注意：这里的 HTTPException **必须 raise**（不能 return JSONResponse）——
        # forward 的 failover 循环靠捕获它来判断账号级错误（401/429）冷却换号；
        # 若在这里吞成返回值，撞错的账号不会被冷却、也不会换下一个账号。
        collected = await asyncio.to_thread(
            self._collect, prompt, requested_model, not agent_mode, tools or None,
            native,
        )
        if protocol == "anthropic":
            collected = chat_completion_to_anthropic_message(collected, original)
        return JSONResponse(content=collected)

    def _stream(
        self, messages: list[dict[str, Any]], model: str, sanitize: bool = True,
        tools: list[dict[str, Any]] | None = None,
        include_usage: bool = False,
        native: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """把 Trae SSE 转成 OpenAI chat.completion.chunk 流。"""
        request_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        usage: dict[str, Any] | None = None
        stop_signal: threading.Event | None = None
        last_semantic_at = time.monotonic()

        def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
            payload = {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        def usage_chunk() -> str:
            # OpenAI 流式协议：stream_options.include_usage 时，[DONE] 前须有一个
            # choices 为空、只带 usage 的收尾 chunk。下游客户端（如 ethan）靠它
            # 判定 is_final——缺失会导致 final chunk 永远不到、tool_calls 整体丢失
            payload = {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [],
                "usage": usage or {
                    "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                },
            }
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        def error_chunk(msg: str, code: Any = None) -> str:
            # 结构化错误 chunk（与 CodeBuddy 通道 stream_upstream 的错误格式一致）。
            # 不能塞进 content 文本——anthropic 客户端（Claude Code）会把
            # 伪正文当模型输出继续循环；包装层据此转成 event: error
            err: dict[str, Any] = {"message": msg, "type": "upstream_error"}
            if code is not None:
                err["code"] = code
            return f"data: {json.dumps({'error': err}, ensure_ascii=False)}\n\n"

        try:
            # 上游调用放独立读线程：send_trae_chat 是同步整段缓冲读（最长受
            # 上游 socket 180s 超时约束），原实现直接在生成器线程里阻塞读——
            # 生成期间客户端收不到任何字节，长生成（深度 review 大报告等）会
            # 触发下游单 chunk 超时（实测 Ethan _CHUNK_TIMEOUT=120s）→ 回合
            # 中止、落库空回复。拆成读线程 + 带 timeout 的队列等待后，等待
            # 间隙按 TRAE_HEARTBEAT_INTERVAL 发 reasoning 心跳：喂饱下游超时
            # 计时器（续命），也让下游 UI 知道中转还在等上游。
            _ev_q: queue.Queue = queue.Queue()
            _stop = threading.Event()
            stop_signal = _stop
            incremental = False

            def _read_upstream() -> None:
                try:
                    if native is not None:
                        event_source = self._stream_native_events(
                            native["messages"], model, native["tools"], _stop)
                        if event_source is not None:
                            for upstream_event in event_source:
                                if _stop.is_set():
                                    return
                                _ev_q.put(("event", upstream_event))
                            _ev_q.put(("eof", None))
                            return
                        skip_stream = (not self._keeps_native_error(model)
                                       and _stream_rejected_recently(model))
                        if skip_stream:
                            # TTL 内：跳过必败的流式尝试，直接非流式缓冲（省一次
                            # 注定失败的往返，也不再喂养惩罚窗口）；若最近刚有过
                            # 4001，先安静等到窗外
                            log.info(
                                "trae native stream rejected recently, "
                                "skip to nonstream: model=%s", model)
                            _pace_after_reject(model)
                            raw_text = self._send_native_request(
                                native["messages"], model, stream=False,
                                tools=native["tools"])
                        else:
                            raw_text = self._send_native_request(
                                native["messages"], model, stream=True,
                                tools=native["tools"])
                        used = True
                        if _native_rejected(raw_text) and not self._keeps_native_error(model):
                            # 上游会整体拒绝 chat_v3 流式请求（2026-10-07 实测：同
                            # body 非流式 200、流式 4001，且文本协议兜底对 GPT-6 系
                            # 的 solo_work_lite 也 4001）。先降级 native 非流式重试
                            # ——非流式响应本就是 SSE 帧形态，整段缓冲后走同一解析
                            # 路径。
                            # 注意每一跳前必须**节流**：上游对刚失败（4001）的账号
                            # 有快速重试惩罚，且窗口随连续失败拉长（~0.2s 必拒，
                            # 累积失败后 ~15-20s 才恢复）。固定短退避会在窗内连环
                            # 开火、越喂越长；改为记住最近 4001 时刻，安静等到窗外
                            # 再发——等待不产生失败，窗口只衰减不增长。读线程里睡，
                            # 心跳照常喂下游，客户端无感。
                            if not skip_stream:
                                log.warning(
                                    "trae native stream rejected (4001), "
                                    "retry nonstream: model=%s", model)
                                _debug_dump("trae_native_fallback", model=model,
                                            phase="stream")
                                _mark_stream_rejected(model)
                                _pace_after_reject(model)
                                raw_text = self._send_native_request(
                                    native["messages"], model, stream=False,
                                    tools=native["tools"])
                            if _native_rejected(raw_text):
                                # 非流式也被拒（落在惩罚窗内）：安静等到窗外原路
                                # 再试——文本协议对 GPT-6 系已死，能用非流式救回就
                                # 别落文本多喂一次必败失败
                                log.warning(
                                    "trae native nonstream rejected (4001), "
                                    "paced retry: model=%s", model)
                                _note_reject(model)
                                _pace_after_reject(model)
                                raw_text = self._send_native_request(
                                    native["messages"], model, stream=False,
                                    tools=native["tools"])
                            if _native_rejected(raw_text):
                                log.warning(
                                    "trae native nonstream rejected twice (4001), "
                                    "fallback to text protocol: model=%s", model)
                                _note_reject(model)
                                _pace_after_reject(model)
                                raw_text = send_trae_chat(
                                    messages, model, stream=False,
                                    base_url=self._base_url)
                                used = False
                            else:
                                used = True
                        if used:
                            _note_native_ok(model)
                        _ev_q.put(("raw", (raw_text, used)))
                    else:
                        _ev_q.put(("raw", (send_trae_chat(
                            messages, model, stream=True,
                            base_url=self._base_url), False)))
                except BaseException as e:  # noqa: BLE001 — 原样转主线程抛出
                    _ev_q.put(("error", e))

            # 读线程以请求上下文的副本运行：ContextVar 赋值不跨线程，PAT 子类的
            # 发送钩子靠这份副本读到请求级账号 holder（ACCOUNT_META）并上报账号
            _request_ctx = contextvars.copy_context()
            threading.Thread(target=_request_ctx.run, args=(_read_upstream,),
                             daemon=True, name="trae-sse-reader").start()
            raw: str | None = None
            used_native = native is not None
            _waited = 0
            pending_event: tuple[str, dict[str, Any]] | None = None
            while raw is None and pending_event is None:
                semantic_left = TRAE_SEMANTIC_TIMEOUT - (time.monotonic() - last_semantic_at)
                timeout = (min(TRAE_HEARTBEAT_INTERVAL, semantic_left)
                           if TRAE_HEARTBEAT_INTERVAL > 0 else semantic_left)
                try:
                    kind, payload = _ev_q.get(timeout=max(0.001, timeout))
                except queue.Empty:
                    idle_for = time.monotonic() - last_semantic_at
                    _waited += max(0, timeout)
                    if idle_for >= TRAE_SEMANTIC_TIMEOUT:
                        _stop.set()
                        raise HTTPException(
                            status_code=504,
                            detail=f"trae stream produced no content for {round(idle_for)}s")
                    _debug_dump("trae_heartbeat", waited=round(_waited))
                    if TRAE_HEARTBEAT_INTERVAL > 0:
                        yield ": heartbeat\n\n"
                    continue
                if kind == "error":
                    raise payload
                if kind == "eof":
                    raise HTTPException(status_code=502, detail="trae stream returned no content")
                if kind == "event":
                    incremental = True
                    pending_event = payload
                else:
                    raw, used_native = payload
            acc = _NativeToolAccumulator() if used_native else None
            cleaner = (
                _StreamLeakCleaner() if sanitize and not used_native else None
            )
            splitter = (
                _StreamToolCallSplitter(_tool_names(tools))
                if tools and not used_native else None
            )
            dbg_parts: list[str] = []
            semantic_seen = False
            done_seen = False
            event_iter = iter((pending_event,)) if incremental else iter(_parse_sse(raw or ""))
            while True:
                try:
                    item = next(event_iter)
                except StopIteration:
                    if not incremental:
                        break
                    semantic_left = TRAE_SEMANTIC_TIMEOUT - (time.monotonic() - last_semantic_at)
                    timeout = (min(TRAE_HEARTBEAT_INTERVAL, semantic_left)
                               if TRAE_HEARTBEAT_INTERVAL > 0 else semantic_left)
                    try:
                        kind, payload = _ev_q.get(timeout=max(0.001, timeout))
                    except queue.Empty:
                        idle_for = time.monotonic() - last_semantic_at
                        _waited += max(0, timeout)
                        if idle_for >= TRAE_SEMANTIC_TIMEOUT:
                            _stop.set()
                            raise HTTPException(
                                status_code=504,
                                detail=f"trae stream produced no content for {round(idle_for)}s")
                        _debug_dump("trae_heartbeat", waited=round(_waited))
                        if TRAE_HEARTBEAT_INTERVAL > 0:
                            yield ": heartbeat\n\n"
                        continue
                    if kind == "error":
                        raise payload
                    if kind == "eof":
                        if not semantic_seen:
                            raise HTTPException(status_code=502, detail="trae stream returned no content")
                        if not done_seen:
                            raise HTTPException(status_code=502, detail="trae stream ended before completion")
                        break
                    if kind != "event":
                        raise RuntimeError("unexpected trae stream message")
                    item = payload
                event, data = item
                if event == "output" and (
                    data.get("reasoning_content") or data.get("response") or data.get("tool_calls")
                ):
                    semantic_seen = True
                    last_semantic_at = time.monotonic()
                    _waited = 0
                if event == "done":
                    done_seen = True
                if event == "error":
                    yield error_chunk(_trae_error_text(data), (data or {}).get("code"))
                    yield "data: [DONE]\n\n"
                    return
                if event == "output":
                    if data.get("reasoning_content"):
                        yield chunk({"reasoning_content": data["reasoning_content"]})
                    if acc is not None:
                        if data.get("tool_calls"):
                            acc.feed(data["tool_calls"])
                        text = data.get("response") or ""
                        if text:
                            dbg_parts.append(text)
                            yield chunk({"role": "assistant", "content": text})
                    elif data.get("response"):
                        text = data["response"]
                        if cleaner is not None:
                            text = cleaner.feed(text)
                        elif splitter is not None:
                            text = splitter.feed(text)
                        if text:
                            dbg_parts.append(text)
                            yield chunk({"role": "assistant", "content": text})
                elif event == "token_usage":
                    u = data or {}
                    try:
                        usage = {
                            "prompt_tokens": int(u.get("prompt_tokens") or 0),
                            "completion_tokens": int(u.get("completion_tokens") or 0),
                            "total_tokens": int(u.get("total_tokens") or 0),
                        }
                    except (TypeError, ValueError):
                        usage = None
                elif event == "done":
                    done_seen = True
                    break
            if not semantic_seen:
                raise HTTPException(status_code=502, detail="trae stream returned no content")
            if incremental and not done_seen:
                raise HTTPException(status_code=502, detail="trae stream ended before completion")
        except GeneratorExit:
            if stop_signal is not None:
                stop_signal.set()
            raise
        except HTTPException as e:
            if stop_signal is not None:
                stop_signal.set()
            yield error_chunk(f"trae error {e.status_code}: {e.detail}", e.status_code)
            yield "data: [DONE]\n\n"
            return
        except Exception as e:
            if stop_signal is not None:
                stop_signal.set()
            log.error("Trae stream error: %s", e)
            yield error_chunk(f"trae stream error: {e}")
            return

        if stop_signal is not None:
            stop_signal.set()
        finish = "stop"
        dbg_calls: list[dict[str, Any]] = []
        if used_native:
            calls = acc.finish()
            if calls:
                finish = "tool_calls"
                dbg_calls = calls
                # OpenAI 流式协议：tool_call 必须带 index（客户端靠它合并分片），
                # 首个 delta 带 role；不带 index 会被部分解析器直接丢弃
                yield chunk({"role": "assistant", "tool_calls": calls})
        elif cleaner is not None:
            tail = cleaner.flush()
            if tail:
                dbg_parts.append(tail)
                yield chunk({"role": "assistant", "content": tail})
        elif splitter is not None:
            tail, calls = splitter.flush()
            if tail:
                dbg_parts.append(tail)
                yield chunk({"role": "assistant", "content": tail})
            if calls:
                finish = "tool_calls"
                dbg_calls = calls
                # OpenAI 流式协议：tool_call 必须带 index（客户端靠它合并分片），
                # 首个 delta 带 role；不带 index 会被部分解析器直接丢弃
                yield chunk({"role": "assistant", "tool_calls": [
                    {"index": i, "id": c["id"], "type": "function",
                     "function": c["function"]}
                    for i, c in enumerate(calls)
                ]})
        _debug_dump("debug_trae_response", model=model, stream=True,
                    content="".join(dbg_parts),
                    tool_calls=[
                        {"name": c["function"]["name"],
                         "arguments": c["function"]["arguments"]}
                        for c in dbg_calls
                    ])
        yield chunk({}, finish)
        if include_usage:
            yield usage_chunk()
        yield "data: [DONE]\n\n"

    def _collect(
        self, messages: list[dict[str, Any]], model: str, sanitize: bool = True,
        tools: list[dict[str, Any]] | None = None,
        native: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """非流式：聚合 Trae SSE 成完整响应。"""
        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        usage_real: dict[str, Any] | None = None

        if native is not None:
            if not self._keeps_native_error(model):
                # 同 _stream：最近有过 4001 就先安静等到窗外，别在惩罚窗内开火
                _pace_after_reject(model)
            raw = self._send_native_request(
                native["messages"], model, stream=False, tools=native["tools"])
            used_native = True
            if _native_rejected(raw) and not self._keeps_native_error(model):
                log.warning(
                    "trae native tools rejected (4001), "
                    "paced retry nonstream: model=%s", model)
                _debug_dump("trae_native_fallback", model=model, phase="collect")
                # 同 _stream：先安静等到窗外原路重试一次，仍拒才落文本协议
                _note_reject(model)
                _pace_after_reject(model)
                raw = self._send_native_request(
                    native["messages"], model, stream=False, tools=native["tools"])
            if _native_rejected(raw) and not self._keeps_native_error(model):
                log.warning(
                    "trae native nonstream rejected twice (4001), "
                    "fallback to text protocol: model=%s", model)
                _note_reject(model)
                _pace_after_reject(model)
                raw = send_trae_chat(messages, model, stream=False,
                                     base_url=self._base_url)
                used_native = False
            if used_native:
                _note_native_ok(model)
        else:
            raw = send_trae_chat(messages, model, stream=False,
                                 base_url=self._base_url)
            used_native = False
        acc = _NativeToolAccumulator() if used_native else None
        for event, data in _parse_sse(raw):
            if event == "error":
                # 状态码按**上游码分类**（额度 4008/4011/4021/4031 → 429、
                # 鉴权 1001/4010 → 401），不能一律 502——forward 的 failover
                # 循环只认 401/429 为账号级错误，额度是账号级的，A 号撞 4008
                # 时 B 号可能还有，一律 502 会让 failover 直接透传不换号
                # （2026-10-06 实测：双账号一空一满，测试按钮直接报 4008）。
                raise HTTPException(
                    status_code=_sse_error_status((data or {}).get("code")),
                    detail=_trae_error_text(data),
                )
            if event == "token_usage":
                u = data or {}
                try:
                    usage_real = {
                        "prompt_tokens": int(u.get("prompt_tokens") or 0),
                        "completion_tokens": int(u.get("completion_tokens") or 0),
                        "total_tokens": int(u.get("total_tokens") or 0),
                    }
                except (TypeError, ValueError):
                    usage_real = None
            if event == "output":
                if data.get("reasoning_content"):
                    reasoning_parts.append(data["reasoning_content"])
                if data.get("response"):
                    content_parts.append(data["response"])
                if acc is not None and data.get("tool_calls"):
                    acc.feed(data["tool_calls"])

        reasoning = "".join(reasoning_parts)
        content = "".join(content_parts)
        # 无 tools 请求也必须有初始值——下方 _debug_dump 无条件引用
        # （实测缺失时非流式无 tools 请求直接 UnboundLocalError -> internal error）
        tool_calls: list[dict[str, Any]] = []
        if used_native:
            # 原生通道：tool_calls 来自结构化事件；无 agent 预设，正文没有
            # 可泄漏的协议语法，不做解析/清洗
            tool_calls = acc.finish()
        else:
            # 顺序关键：带 tools 的请求必须先解析、后清洗——_sanitize_agent_leak
            # 会把 <tool_call>/</arg_value> 标签全剥掉，先清洗再解析会让解析器
            # 拿到被拆掉结构的残骸（实测 glm-5.3 函数调用表达式因此整段漏进正文）
            if tools:
                content, tool_calls = _parse_tool_calls(content, _tool_names(tools))
            if sanitize:
                content = _sanitize_agent_leak(content)
        _debug_dump("debug_trae_response", model=model, stream=False, content=content,
                    tool_calls=len(tool_calls))
        # 只有 tool_calls 没有正文也是合法响应（agent 直接发起调用），
        # 不注入空响应兜底文案——否则会混进 Anthropic tool_use 消息正文
        if not content and not reasoning and not tool_calls:
            content = "(trae upstream 返回了空响应，未产生任何内容)"
        if reasoning:
            if content:
                content = f"<think>\n{reasoning}\n</think>\n\n{content}"
            else:
                # 只有思维链没有正文时，别把 reasoning 整个丢掉
                content = f"<think>\n{reasoning}\n</think>"

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    **({"tool_calls": tool_calls} if tool_calls else {}),
                },
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }],
            # 原生通道带真实 token_usage；文本协议上游不吐 usage，沿用旧启发式
            "usage": usage_real or {
                "prompt_tokens": 0,
                "completion_tokens": max(1, len(content.encode("utf-8")) // 4),
                "total_tokens": 0,
            },
        }



# ---------------------------------------------------------------------------
# 流式首事件闸门（多账号 failover 用）
# ---------------------------------------------------------------------------

class _GatedStream:
    """闸门结果：``buffered`` 是预驱动期间攒下的**语义**事件（心跳已丢），
    ``gen`` 是在途生成器（committed 后续跑它，不重开）。"""

    def __init__(self, buffered: list[str], gen: Any) -> None:
        self.buffered = buffered
        self.gen = gen


def _next_or_end(it: Any) -> tuple[bool, Any]:
    """在线程里驱动同步生成器；用标记返回值避免 StopIteration 穿过 Future。"""
    try:
        return True, next(it)
    except StopIteration:
        return False, None


async def _sync_to_async_iter(it: Any) -> Any:
    """把同步 iterator 包装成 async iterator（喂 StreamingResponse）。

    ``_stream`` / ``_wrap_anthropic_stream`` 在拿上游事件时会阻塞等待 queue；
    每次 ``next()`` 都移出事件循环，否则首帧之后的慢事件仍会卡住全站。
    """
    it = iter(it)
    loop = asyncio.get_running_loop()
    while True:
        has_item, item = await loop.run_in_executor(
            _stream_iter_pool, _next_or_end, it)
        if not has_item:
            return
        yield item


async def _gate_first_event_async(gen: Any) -> "_GatedStream | BaseException":
    """用专属线程池预驱动首事件，并把请求 ContextVar 带入工作线程。

    ``run_in_executor`` 不像 ``asyncio.to_thread`` 会自动传播 contextvars；PAT 的
    请求级账号 holder 就在 ``ACCOUNT_META`` 里。若首个 ``next(gen)`` 在空上下文
    中执行，``_stream`` 随后复制给 SSE 读线程的也是空上下文，最终请求虽成功，
    metrics 的 account 却永远为空（Claude Code 流式请求正好稳定命中这条路径）。
    """
    loop = asyncio.get_running_loop()
    request_ctx = contextvars.copy_context()
    return await loop.run_in_executor(
        _stream_gate_pool, request_ctx.run, _gate_first_event, gen)


def _gate_first_event(gen: Any) -> "_GatedStream | BaseException":
    """预驱动 ``_stream``（**同步**生成器）到第一个语义事件。

    ``_stream`` 是读线程 + queue 轮询的同步生成器（非 async）——下游
    :func:`_wrap_anthropic_stream` 也按同步 ``Iterator[str]`` 消费，本闸门
    同步迭代即可；StreamingResponse 那边由 starlette 对同步 iterator 做
    线程池适配（与改造前一致）。

    返回 :class:`_GatedStream`（成功，可放行）或捕获到的异常（换号/透传）。
    心跳帧（``: heartbeat``）与空帧跳过不缓冲。第一个含 ``data:`` 的语义帧
    默认即 committed；**但若该帧是账号级错误**（``_stream`` 的 ``except
    HTTPException`` 把 401/429 yield 成 ``{"error": {...}}`` 错误 chunk 再
    return，是假成功）则**不缓冲、直接返回对应 HTTPException** 让 failover
    换号——此时客户端未收字节，换号安全。真正的异常在第一个语义帧**之前**
    抛出也都安全换号（客户端未收字节）。
    """
    buffered: list[str] = []
    try:
        for piece in gen:
            text = piece if isinstance(piece, str) else piece.decode("utf-8", "replace")
            stripped = text.strip()
            # 心跳/注释帧：保活用，不是内容，丢掉
            if not stripped or stripped.startswith(":"):
                continue
            # 账号级错误帧（_stream 把 HTTPException yield 成 error chunk）：
            # 不缓冲、直接当异常换号，别让它假成功混过闸门。
            err = _account_error_from_frame(stripped)
            if err is not None:
                return err
            buffered.append(text)
            return _GatedStream(buffered, gen)
        # 没有任何语义事件就结束：空流假成功，当错误交调用方换号
        return HTTPException(status_code=502, detail="trae stream returned no content")
    except HTTPException as e:
        return e
    except Exception as e:  # noqa: BLE001 — 读线程透传的原始异常
        return e


#: 错误 chunk 里判定「换下一个账号可能好转」的 HTTP 状态码（与
#: ``TraeProvider._is_account_error`` 一致）。
_ACCOUNT_ERROR_CODES = (401, 403, 429)


def _account_error_from_frame(stripped_frame: str) -> "HTTPException | None":
    """从单个 SSE 帧里识别账号级错误，还原成 HTTPException（None = 放行）。

    ``_stream`` 的 ``except HTTPException`` 会把账号级错误 yield 成
    ``error_chunk``（payload 是 ``{"error": {"message", "type", "code"}}``）
    再 ``return``——那是假成功。code 有两种形态：HTTP 形态（``_stream``
    抛出的 HTTPException 的 ``status_code``，401/429）与上游 SSE 码形态
    （``event:error`` 帧原样透传的 4008/4011 等，经 ``_sse_error_status``
    映射）。本函数在闸门里把这类帧还原成 HTTPException，让 forward 的
    failover 循环冷却换号；非账号级错误帧返回 None（放行，由下游/包装层
    按原样处理）。
    """
    if not stripped_frame.startswith("data:"):
        return None
    payload = stripped_frame[5:].strip()
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if not isinstance(err, dict):
        return None
    try:
        code = int(err.get("code"))
    except (TypeError, ValueError):
        return None
    # 两种 code 形态都认：HTTP 形态（``_stream`` 把自己的 HTTPException
    # yield 成 error chunk 时传的是 e.status_code）与上游 SSE 码形态
    # （``event:error`` 帧原样透传的 4008/4011 等）。后者此前不认——额度
    # 错误放行成假成功，闸门形同虚设（2026-10-06 双账号实测撞上）。
    if code in _ACCOUNT_ERROR_CODES:
        return HTTPException(status_code=code, detail=str(err.get("message") or "trae upstream error"))
    status = _sse_error_status(code)
    if status == 502:
        return None
    return HTTPException(status_code=status, detail=str(err.get("message") or "trae upstream error"))


def _replay_prefixed(buffered: list[str], gen: Any) -> Any:
    """先补放闸门缓冲的语义帧，再续跑在途生成器（不重开，防二次消费）。

    同步生成器（与 ``_stream`` 同型）；补放后直接 ``for`` 续跑在途迭代器，
    不重开（重开会二次消费上游 / 触发二次计费）。
    """
    for piece in buffered:
        yield piece
    for piece in gen:
        yield piece
