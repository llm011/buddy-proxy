"""TraeProvider compatibility facade and native transport override hooks.

Responsibilities live in provider_catalog/accounts/forward/stream/policy/gate.
The original module globals stay here deliberately: moved implementations resolve
this facade at call time so replacement monkeypatches (not just object mutations)
and existing PAT/Intl subclasses keep their original dispatch behavior.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from typing import Any, AsyncIterator, Sequence

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.account_store import quota_epoch_from
from ..core.checkin import SOURCE_INFERRED, next_daily_reset
from ..core import load_balance as lb_mod
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
    map_model_for,
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
from . import max_mode as max_mode_mod
from .native_tools import (
    _NativeStreamRejected,
    _NativeToolAccumulator,
    _native_messages,
    _native_rejected,
    _send_native_chat,
    _stream_native_chat_events,
)
from .sse import (_parse_sse, _trae_error_text, _wrap_anthropic_stream,
                  _sse_error_status)
from .text_protocol import _extract_prompt, _looks_like_agent_request
from .transport import _send_trae_work_chat
from .text_toolcall import _StreamToolCallSplitter, _parse_tool_calls, _tool_names
from .transport import send_trae_chat
from .usage import normalize_token_usage

from .provider_catalog import CatalogMixin
from .provider_accounts import AccountsMixin, _claim_rate_limited, _first_num
from .provider_forward import ForwardMixin
from .provider_stream import StreamMixin
from .provider_catalog import (
    _parse_generation,
    _is_old_generation,
)
from .provider_policy import (
    _note_reject,
    _pace_after_reject,
    _note_native_ok,
    _stream_rejected_recently,
    _mark_stream_rejected,
    _attach_sse_code,
    _sse_error_exception,
    _is_entitlement_error,
    _is_context_overflow,
    _mark_model_overflow,
    _model_overflow_recently,
    _internal_model_key,
    _mark_entitlement_skip,
    _entitlement_skipped,
    _account_error_from_frame,
    _lb_state,
)
from .provider_gate import (
    _next_or_end,
    _sync_to_async_iter,
    _gate_first_event_async,
    _gate_first_event,
    _replay_prefixed,
)
from .provider_gate import _GatedStream

log = logging.getLogger(__name__)

#: 「家族名 + 数字版本」解析（glm-5.2 → ("glm", 5.2)；gpt-5.6-sol →
#: ("gpt", 5.6)——代号尾巴不影响代际比较）。解析不出的（如
#: doubao-seed-2.1-pro）返回 None，不参与代际比较。
_GENERATION_RE = re.compile(
    r"^(?P<family>[a-z]+(?:\d+)?)[-_]?v?(?P<major>\d+)(?:\.(?P<minor>\d+))?"
    r"(?:[-_].*)?$")
# 代号系（minimax-m3 / kimi-k3）：家族名与版本号之间只隔一个字母代号，
# 没有纯数字版本（``minimax-m3.1-flash`` → 家族 minimax-m，版本 3.1）。
# 不认它们的话 m3 / m3.1 之间永远比不出代际，只能靠黑名单手工维护。
_GENERATION_CODENAME_RE = re.compile(
    r"^(?P<family>[a-z]+)-(?P<code>[a-z])(?P<major>\d+)"
    r"(?:\.(?P<minor>\d+))?(?:[-_].*)?$")

#: 家族 → **保留底线**（该代际及以上永不因代际收敛被隐藏）。
#:
#: 默认行为是「有新的就藏旧的」：用户 2026-10-25 点名 glm-5.1 / glm-5.2 这批
#: 没用，收敛掉是对的。但 2026-11-24 用户要求 glm-5.4 上线后**不要顶掉 5.3**
#: ——两个都保留。
#:
#: 为什么不整族豁免：收敛是按家族名判的，豁免整个 glm 家族会让 glm-5.1 /
#: glm-5.2 也重新冒出来（用户已明确说过没用）。设底线更精准——5.3 及以上
#: 留着，5.2 及以下照旧被 5.3 顶掉。
#:
#: 为什么 5.3 值得保：`glm-5.3-flash` / `-flashx` 是本机实测能出流的廉价快档，
#: 日常小请求用它比 5.4 划算。整族藏掉等于拿一个新模型换掉三个能用的。
_GENERATION_FLOOR: dict[str, float] = {"glm": 5.3}

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

# 兼容旧调用点保留（原「全表逐模型探测」的可用性缓存）。2026-10-25 起刷新
# 改为「拉上游目录 + 抄倍率 + 只验证新名字」，不再往这里写；留变量只为外部
# 脚本/旧测试引用时不炸。
_PROBE_CACHE: dict[str, dict[str, bool]] = {}

class TraeProvider(CatalogMixin, AccountsMixin, ForwardMixin, StreamMixin, BaseProvider):
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

    def ensure_auth(self) -> None:
        _auth()

    def _send_native_request(
        self,
        native_msgs: list[dict[str, Any]],
        model: str,
        stream: bool,
        tools: list[dict[str, Any]] | None,
        *, max_mode: bool = False,
    ) -> str:
        """Provider 级发送钩子；PAT 子类覆盖后不会因重叠模型串到个人账号。

        ``max_mode`` 由 forward/collect 决策后下传（与 models() 声明窗口一致，
        见 max_mode.py）；PAT 覆盖忽略它（服务账号通道独立计费口径）。
        """
        return _send_native_chat(native_msgs, model, stream, tools, max_mode=max_mode)

    def _keeps_native_error(self, model: str) -> bool:
        """PAT 子类覆写为 True，避免把其真实错误回落到个人文本通道。"""
        return False

    def _uses_native_mode(self) -> bool:
        """是否使用原生传输；PAT 子类始终启用以隔离账号体系。"""
        return _NATIVE_TOOLS_ENABLED

    def _max_inject(self, model: str) -> bool:
        """本次请求是否应向上游注入 ``is_max_mode``。

        与 ``models()`` 声明窗口共用 :func:`max_mode.effective_window` 同一决策
        点（见 max_mode.py 模块 docstring 的强耦合不变量）：开关注入 max 档、
        关闭或不支持则不注入。别名先按 MODEL_MAP 解析到内部名。
        """
        internal = _internal_model_key(self._region.key, model)
        _window, inject = max_mode_mod.effective_window(
            self._region.key, internal,
            discovered=self._discovered.get(internal),
        )
        return inject

    def _stream_native_events(
        self,
        native_msgs: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None,
        stop: threading.Event,
        *, max_mode: bool = False,
    ):
        """原生通道的增量事件源（真流式）。

        历史上这里恒返回 None，个人 Work/IDE 通道因此走整段兼容路径：读线程
        用 ``_send_native_chat`` 把整个响应读完，再交给主线程切块「重放」。
        上游本身是真流式（2026-11-23 实测首块 0.87s / 178 chunk / 6.93s），
        整段缓冲让客户端 TTFT 等于总耗时——metrics 里 ``TTFT/总耗时`` 中位数
        trae 0.998、codebuddy 0.308。改成增量后首字能立刻下发。

        PAT 子类仍覆盖本方法走自己的账号体系（``pat.stream_pat_native``），
        忽略 ``max_mode``。
        """
        return _stream_native_chat_events(native_msgs, model, tools, stop=stop,
                                          max_mode=max_mode)

    def _collect(
        self, messages: list[dict[str, Any]], model: str, sanitize: bool = True,
        tools: list[dict[str, Any]] | None = None,
        native: dict[str, Any] | None = None,
        max_inject: bool = False,
    ) -> dict[str, Any]:
        """Delegate non-stream aggregation; retain this override/patch entry point."""
        from .collection import collect_response

        return collect_response(self, messages, model, sanitize, tools, native, max_inject)

# Policy state remains facade-owned for old imports and replacement patches.

#: 错误 chunk 里判定「换下一个账号可能好转」的 HTTP 状态码（与
#: ``TraeProvider._is_account_error`` 一致）。
_ACCOUNT_ERROR_CODES = (401, 403, 429)

#: 1005「套餐权益不足」是**账号+模型**级：免费号不能用 glm-5.4，但 glm-5.3 照常可
#: 服务。换号有意义（付费号可能有权益），所以 ``_sse_error_status`` 把它归 403 让
#: failover 换号；但它**不能**走 ``failover.mark_cooldown`` 的整账号冷却——那会把该
#: 账号对其他模型的服务一起误伤。这里按 (account_id, 模型) 记跳过，TTL 到期重试
#: （套餐可能升级）。键用 MODEL_MAP 解析后的内部名，别名请求也能命中同一条跳过。
_ENTITLEMENT_SSE_CODE = 1005
_ENTITLEMENT_SKIP_TTL_S = 600.0
_entitlement_skip: dict[tuple[str, str], float] = {}
_entitlement_skip_lock = threading.Lock()

#: 4026「上下文超限」与 1005 同款处置（换号、不整号冷却），但跳过 TTL 更短：
#: 它反映的是「这次请求太长」而非账号权益，DSH 压缩后几十秒内就会重试短请求。
_CONTEXT_OVERFLOW_SSE_CODE = 4026
_CONTEXT_OVERFLOW_SKIP_TTL_S = 120.0

#: 「本模型近期全员 4026」的标记：模型内部键 → 单调钟到期时刻。
#:
#: 上下文太大时**每个**账号都会 4026（4026 最常见的场景）。此时跳过表全满，
#: 选号的「全被跳过则保留全部」兜底会让每个请求都把 N 个账号挨个重打一遍
#: （review 实测：两轮请求 18 次上游，9 账号 × 2），每次都注定失败。全员
#: 都拒之后记一个模型级短标记，TTL 内直接抛 4026 让客户端去压缩。
#: 用单调钟（monotonic）：免疫墙钟跳变，且这里只关心「多久之前」。
_MODEL_OVERFLOW_TTL_S = 60.0
_model_overflow_mark: dict[str, float] = {}
_model_overflow_lock = threading.Lock()
