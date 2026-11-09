"""Kiro provider：AWS CodeWhisperer 直连（多账号 + 双协议）。

## 协议映射

客户端三种协议都要能走，上游只有一套 ``generateAssistantResponse``：

| 客户端协议 | 入口 | 做法 |
|---|---|---|
| ``openai`` | ``/v1/chat/completions`` | chat 请求体直接翻译成 Kiro 的 conversationState |
| ``anthropic`` | ``/v1/messages`` | routes 层已把 anthropic 转成 chat（``body``），本通道照 chat 处理，**响应**再用 Anthropic 事件流输出 |
| ``responses`` | 不支持 | 回 501 并给出可操作提示 |

**响应**侧不需要回读 ``original``：Anthropic 的 SSE 由
:func:`.stream.to_anthropic_events` 直接生成（事件序列固定），不像 chat 那样
依赖请求形状。

## 工具调用

Kiro 的 ``userInputMessageContext`` 支持 tools，但**格式与 OpenAI 不同**
（``tools: [{toolSpecification: {...}}]``）。本轮先只做纯文本对话——工具调用
需要额外的双向转换与流式 tool_use 事件拼装，值得单独一轮。请求里带了 tools
时**如实降级**：不报错、不静默丢弃，而是打一条 warn 并只发文本部分。这样用户
能立刻看出「工具没生效」而不是收到莫名的行为差异。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from . import credentials as creds
from . import stream as kiro_stream
from .credentials import KIRO_UA_VERSION, KiroAccount
from ..providers.base import BaseProvider
from ..core.errors import describe_exception

log = logging.getLogger(__name__)

#: 上游推理端点（区域由账号决定）。
_CHAT_PATH = "/generateAssistantResponse"
_MODELS_PATH = "/ListAvailableModels"

#: 与 Kiro 客户端一致的 UA。**必须带 Kiro/<version>**：少了它上游会回
#: 403「Your subscription does not support this application」，极易被误判成
#: token 或订阅问题（见 credentials 模块头的踩坑记录）。
_USER_AGENT = f"aws-sdk-js/1.0.0 Kiro/{KIRO_UA_VERSION}"

#: EventStream 的 Accept。改成 text/event-stream 会被上游换成 SSE 吗？实测不会
#: ——它仍回二进制帧，所以这里如实声明。
_ACCEPT = "application/vnd.amazon.eventstream"

_TIMEOUT = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=15.0)

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "close"}


def _num(value: Any) -> float | None:
    """宽松取数：数字/数字字符串都收；bool/None/坏值返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


#: kiro 的 ``contextUsagePercentage`` 是「**整段对话**占模型上下文窗口的比例」。
#: 各模型窗口取 ``ListAvailableModels`` 的 ``maxInputTokens``（拿不到时用这个兜底：
#: Claude 系普遍 200K）。
#:
#: 为什么能这样反推：实测同一段输入连发，``contextUsagePercentage`` 稳定不变
#: （±0.001），而输入长度翻倍时它按比例上涨（66 字符/行 → 2.227% / 3.220% /
#: 5.416% / 8.697% 对应 10/100/300/600 行，严格线性）。所以
#: ``input_tokens ≈ ctx% × window`` 是从上游给的百分比推出来的**估算值**，
#: 不是瞎猜的常数。
_DEFAULT_CONTEXT_WINDOW = 200_000

#: Kiro 在**每次请求**里都会注入自己的系统提示与工具定义，这部分也计入
#: ``contextUsagePercentage``。实测标定（窗口按 200K）：
#:
#: ====== ============ ============ ============
#: 输入行 ctx%         总 token     扣掉输入后
#: ====== ============ ============ ============
#: 10     2.227%       4454         4234
#: 100    3.220%       6440         4243
#: 300    5.416%       10832        4242
#: 600    8.697%       17394        4214
#: ====== ============ ============ ============
#:
#: 四个点算出来 4214~4243，波动 <1%——说明它确实是个**固定的系统开销**，不是
#: 估算噪声。不扣掉它的话，一句 16 字符的提问会被算成 4457 token（高估约 4200）。
_KIRO_SYSTEM_OVERHEAD_TOKENS = 4_233

#: 模型 → 上下文窗口。由 :meth:`KiroProvider.refresh_models` 从
#: ``ListAvailableModels`` 的 ``maxInputTokens`` 填进来（上游没给就留空，
#: 由 :func:`_window_for` 兜底）。
_MODEL_WINDOWS: dict[str, int] = {}


def _estimate_tokens(ctx_percent: float | None, window: int,
                     input_chars: int, output_chars: int
                     ) -> tuple[int | None, int, str]:
    """由 ``contextUsagePercentage`` 反推 input token；output 按字符估。

    返回 ``(input_tokens, output_tokens, source)``。``source`` 说明可信度：

    - ``"context"``：input 来自上游的 ctx%（较可信，误差主要在窗口大小）
    - ``"chars"``：拿不到 ctx%，退回按字符估（4 字符/token，粗糙）
    - ``"none"``：连字符数都没有

    **输出侧上游不给任何数据**，只能按字符估。这一点在 UI 上必须能看出来
    （日志的 token 列带「≈」），否则用户会拿它做精确统计。
    """
    output = max(1, output_chars // 4) if output_chars else 0
    if isinstance(ctx_percent, (int, float)) and not isinstance(ctx_percent, bool) \
            and ctx_percent > 0 and window > 0:
        total = ctx_percent / 100.0 * window
        # 扣掉 Kiro 自己的系统提示开销（见 _KIRO_SYSTEM_OVERHEAD_TOKENS 的标定表）
        est = int(round(total - _KIRO_SYSTEM_OVERHEAD_TOKENS))
        # 下限用字符粗估兜住：上游可能给极小的百分比，扣完会变成负数
        floor = max(1, input_chars // 6)
        if est <= 0:
            return floor, output, "chars"
        return max(est, 1), output, "context"
    if input_chars:
        return max(1, input_chars // 4), output, "chars"
    return None, output, "none"


def _input_chars(payload: dict[str, Any]) -> int:
    """上游请求体里「用户输入」的字符数（只用于 token 估算的兜底）。"""
    try:
        cur = payload["conversationState"]["currentMessage"]["userInputMessage"]
        return len(str(cur.get("content") or ""))
    except (KeyError, TypeError):
        return 0


def _window_for(model: str) -> int:
    """模型上下文窗口（token）。取不到就退回 :data:`_DEFAULT_CONTEXT_WINDOW`。"""
    entry = _MODEL_WINDOWS.get(model)
    if isinstance(entry, int) and entry > 0:
        return entry
    return _DEFAULT_CONTEXT_WINDOW


def _usage_payload(chars: int, credit: float | None, *,
                   anthropic: bool = False,
                   ctx_percent: float | None = None,
                   input_chars: int = 0,
                   window: int = _DEFAULT_CONTEXT_WINDOW) -> dict[str, Any]:
    """构造 usage 对象（两种协议共用）。

    kiro 的 EventStream **只给两样东西**：``contextUsagePercentage``（整段对话
    占窗口的百分比）与 ``credit``（真实计费）。没有 inputTokens / outputTokens /
    缓存命中数，响应头里也没有缓存字段（逐项查过，只有 ``Cache-Control:
    no-cache``）。

    所以这里：

    - **input**：由 ``ctx% × 窗口`` 反推（见 :func:`_estimate_tokens`）。比原来
      「恒为 0」有用得多——日志的 ``↑`` 列终于有数字了。
    - **output**：只能按字符估（上游不给）。
    - **credit**：``meteringEvent`` 的真实值，原样透传。
    - **缓存**：上游不提供，**不填**。填 0 会被误读成「查了但没命中」；留空
      才是「上游没报」。（实测确认过：credit 随输入长度线性上涨，说明输入
      确实计费；但缓存命中与否没有任何可观测字段。）

    这个字段组合是给本仓库的 metrics 用的：``normalize_usage`` 会按
    ``prompt_tokens`` / ``completion_tokens`` / ``credit`` 取值。
    """
    input_tokens, output_tokens, _src = _estimate_tokens(
        ctx_percent, window, input_chars, chars)
    total = (input_tokens or 0) + output_tokens
    base: dict[str, Any] = {
        "output_tokens": output_tokens, "completion_tokens": output_tokens,
        "total_tokens": total,
    }
    if anthropic:
        base["input_tokens"] = input_tokens or 0
    else:
        base["prompt_tokens"] = input_tokens or 0
    if credit is not None:
        base["credit"] = credit
    return base


def _sse_json(payload: dict[str, Any]) -> str:
    """一帧 OpenAI SSE。"""
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


#: Claude 系模型对外用**短横线**版本号（``claude-opus-5-5``），与 Anthropic
#: 官方命名（``claude-3-5-sonnet``）和 yuntoken 目录一致。
#:
#: 上游（kiro）自己写的是点号（``claude-opus-5.5``），所以对外 id 与转发时
#: 用的上游名**不是同一串**，必须双向映射：
#:
#: - 拉目录时 ``claude-opus-5.5`` → 对外 id ``claude-opus-5-5``，原串存进
#:   ``upstream_key``（排障时能对回上游）；
#: - 转发时把客户端给的 id 还原成上游串（也接受点号写法，做别名）。
#:
#: 只对 ``claude-`` 前缀生效。``gpt-5.6-sol`` / ``minimax-m2.5`` 这些**保持点号**
#: —— 它们的厂商命名本来就用点，改了反而对不上别处的目录。
_CLAUDE_PREFIX = "claude-"


def _public_model_id(upstream_id: str) -> str:
    """上游 id → 对外 id（只对 claude 系把点号换成短横线）。"""
    if upstream_id.startswith(_CLAUDE_PREFIX) and "." in upstream_id:
        return upstream_id.replace(".", "-")
    return upstream_id


def endpoint_base(region: str) -> str:
    """推理端点基址。Kiro 用区域化的 ``q.<region>.amazonaws.com``。"""
    return f"https://q.{region or creds.DEFAULT_REGION}.amazonaws.com"


def auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": _ACCEPT,
        "User-Agent": _USER_AGENT,
        "X-Amz-User-Agent": _USER_AGENT,
    }


def _messages_to_conversation(body: dict[str, Any]
                              ) -> tuple[str, str, list[dict[str, Any]]]:
    """把 chat 的 messages 压成 Kiro 的 conversationState。

    Kiro 的形状是「历史 + 当前消息」，没有独立的 system 字段——system 内容
    拼到**当前用户消息**前面（这是社区实现的通行做法，实测可用）。

    返回 ``(content, model_id, history)``。

    ⚠ **``history`` 必须真的发给上游**，否则模型完全失忆。踩过的坑：
    这个函数一开始只返回 ``(content, model_id)``，把算好的 ``turns`` 直接丢掉，
    于是每次请求都只有最后一条 user 消息——表现是「在 dsh 里聊了两句，第三句
    就忘记前面说过什么」，而换别的通道（如 deepseek）正常，极易误判成上游问题。

    ``history`` 的元素是 Kiro 的形状（实测可用，问它「我叫什么」能正确答出）::

        {"userInputMessage": {"content": ..., "modelId": ..., "origin": "AI_EDITOR"}}
        {"assistantResponseMessage": {"content": ...}}

    只取最后一条 user 作为 currentMessage，其余按序进 history。连续同角色
    的消息合并：Kiro 对角色交替没有严格校验，但合并能少掉「assistant 空串」
    这类上游会挑刺的形态。
    """
    messages = body.get("messages") or []
    system_parts: list[str] = []
    turns: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        content = msg.get("content")
        if isinstance(content, list):
            # 分块数组：只拼 text 块（图片需要额外的 image 字段，本轮不做）
            content = "".join(
                str(b.get("text") or "") for b in content
                if isinstance(b, dict) and b.get("type") == "text")
        text = str(content or "")
        if role in ("system", "developer"):
            if text.strip():
                system_parts.append(text)
            continue
        if role == "tool":
            # 工具结果在纯文本模式下当普通上下文
            text = f"[tool result] {text}"
            role = "user"
        if role not in ("user", "assistant"):
            continue
        if turns and turns[-1]["role"] == role and role == "user":
            turns[-1]["content"] += "\n\n" + text
        else:
            turns.append({"role": role, "content": text})
    model_id = str(body.get("model") or "").strip()
    current = turns.pop() if turns else {"role": "user", "content": "hi"}
    content = str(current.get("content") or "")
    if system_parts:
        prefix = "\n\n".join(system_parts)
        content = f"{prefix}\n\n{content}" if content else prefix
    # 转成 Kiro 的 history 形状。空内容的历史条目直接跳过——上游对空串不友好，
    # 而且没有信息量。
    history: list[dict[str, Any]] = []
    for turn in turns:
        text = str(turn.get("content") or "")
        if not text.strip():
            continue
        if turn.get("role") == "assistant":
            history.append({"assistantResponseMessage": {"content": text}})
        else:
            history.append({"userInputMessage": {
                "content": text, "modelId": model_id, "origin": "AI_EDITOR"}})
    return content, model_id, history


def _resolve_upstream_model(model: str, id_map: dict[str, str]) -> str:
    """客户端给的模型名 → 上游真名。

    认三种写法：对外 id（``claude-opus-5-5``）、上游原名（``claude-opus-5.5``）、
    以及带 ``kiro/`` 前缀的形式。都不命中就原样透传——上游会报错，比我们猜
    一个名字然后「模型不存在」更好查。
    """
    want = (model or "").strip()
    if "/" in want:
        prefix, _, rest = want.partition("/")
        if prefix == "kiro" and rest:
            want = rest
    return id_map.get(want, want)


def _chat_to_kiro(body: dict[str, Any],
                  id_map: dict[str, str] | None = None) -> dict[str, Any]:
    """chat 请求体 → Kiro ``generateAssistantResponse`` 请求体。

    ⚠ **不要带 ``editorState``**：实测带它回 ``400 REQUEST_BODY_INVALID``，
    去掉就 200。``profileArn`` 有就带、没有也能通（两种都验过）。
    """
    content, model_id, history = _messages_to_conversation(body)
    model_id = _resolve_upstream_model(model_id, id_map or {})
    user_message: dict[str, Any] = {
        "content": content,
        "modelId": model_id,
        "origin": "AI_EDITOR",
    }
    payload: dict[str, Any] = {
        "conversationState": {
            "chatTriggerType": "MANUAL",
            "conversationId": str(uuid.uuid4()),
            # 历史一定要带上（见 _messages_to_conversation 的 ⚠ 说明）
            **({"history": history} if history else {}),
            "currentMessage": {"userInputMessage": user_message},
        },
    }
    if body.get("tools"):
        # 见模块头：本轮只做纯文本，如实告警而不是静默丢
        log.warning("kiro 暂不支持工具调用（请求带 %d 个 tools），本次按纯文本转发",
                    len(body["tools"]))
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        # 上游字段名就叫 maxTokens，放在 conversationState 下
        payload["conversationState"]["currentMessage"]["userInputMessage"][
            "userInputMessageContext"] = {"maxTokens": max_tokens}
    return payload


class KiroProvider(BaseProvider):
    """Kiro（AWS CodeWhisperer）通道。"""

    id = "kiro"
    name = "Kiro (CodeWhisperer)"

    supports_checkin = False

    #: 与 yuntoken 同档：网关/聚合型通道在裸名撞车时让位给原生通道。
    route_priority = 50

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._models_cache: list[dict[str, Any]] = []
        self._models_error = ""
        #: 对外 id → 上游 id（同值时也存，方便统一查）。见 _public_model_id。
        self._id_map: dict[str, str] = {}

    # -- 模型目录 -----------------------------------------------------------

    def models(self) -> Sequence[dict[str, Any]]:
        return list(self._models_cache)

    async def refresh_models(self, force: bool = False) -> list[dict[str, Any]]:
        """逐账号拉 ``ListAvailableModels`` 取并集。

        目录是**账号级**的（不同订阅可见模型不同），所以并集而不是只看第一个
        账号——与 qoder 同一个理由。单账号失败只收缩并集。
        """
        del force
        accounts = creds.list_accounts()
        if not accounts:
            self._models_error = "没有已登录的 kiro 账号"
            return list(self._models_cache)
        union: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for acct in accounts:
            try:
                entries = await self._fetch_models(acct)
            except Exception as exc:  # noqa: BLE001 - 单账号失败不阻断整体
                errors.append(f"{acct.label}: {str(exc)[:60]}")
                log.warning("kiro 目录刷新跳过账号 %s: %s", acct.label, exc)
                continue
            for entry in entries:
                union.setdefault(str(entry.get("id") or ""), entry)
        if union:
            self._models_cache = list(union.values())
            self._models_error = ""
            # 登记倍率：拿不到 meteringEvent 实扣时（非流式以外/异常路径），
            # credit_estimate 用这个按 token 粗估，日志才不会整列为空。
            try:
                from ..core.credit_estimate import register_kiro_rates

                register_kiro_rates({
                    str(m.get("id")): float(m["credits"])
                    for m in union.values()
                    if isinstance(m.get("credits"), (int, float))
                })
            except Exception as exc:  # noqa: BLE001 - 估算登记失败不影响目录
                log.debug("kiro 倍率登记失败: %s", type(exc).__name__)
        elif errors:
            self._models_error = "; ".join(errors)
        return list(self._models_cache)

    async def _fetch_models(self, acct: KiroAccount) -> list[dict[str, Any]]:
        token, cred = creds.ensure_account_token(acct.id)
        region = str(cred.get("region") or acct.region)
        url = f"{endpoint_base(region)}{_MODELS_PATH}"
        client = await self._get_client()
        resp = await client.get(url, headers=auth_headers(token),
                                params={"origin": "AI_EDITOR", "maxResults": 50})
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
        payload = resp.json()
        entries = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise RuntimeError("响应里没有 models")
        out = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("modelId") or "").strip()
            if not mid:
                continue
            public = _public_model_id(mid)
            # 登记双向映射：转发时要还原成上游串，别名解析要能认两种写法
            self._id_map[public] = mid
            if public != mid:
                self._id_map.setdefault(mid, mid)   # 点号写法也当别名收下
            model: dict[str, Any] = {
                "id": public,
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "name": str(item.get("modelName") or mid),
                # 上游真名：对外 id 变了以后，排障/对账要靠它
                **({"upstream_key": mid} if public != mid else {}),
            }
            # 上游给了输入模态就用它（TEXT/IMAGE）；没给就不猜
            types = item.get("supportedInputTypes")
            if isinstance(types, list):
                model["images"] = any(str(t).upper() == "IMAGE" for t in types)
            rate = item.get("rateMultiplier")
            if isinstance(rate, (int, float)) and not isinstance(rate, bool):
                model["credits"] = round(float(rate), 2)
            # 上下文窗口：token 反推要用（ctx% × 窗口 ≈ input token）。
            # 上游给了 maxInputTokens 就用它，没给留空交给兜底常量。
            win = item.get("maxInputTokens") or item.get("contextWindow")
            if isinstance(win, int) and not isinstance(win, bool) and win > 0:
                model["context_window"] = win
                _MODEL_WINDOWS[public] = win
            out.append(model)
        return out

    # -- 额度（管理页「额度」卡） -------------------------------------------

    def quota(self) -> dict[str, Any] | None:
        """逐账号查 ``getUsageLimits``，聚合成管理页的额度条目。

        上游结构（实测）::

            {"subscriptionInfo": {"subscriptionTitle": "KIRO PRO"},
             "usageBreakdownList": [{
                "displayName": "Credit", "unit": "INVOCATIONS",
                "usageLimit": 1000, "currentUsageWithPrecision": 0.99,
                "nextDateReset": 1796083200.0, ...}],
             "nextDateReset": 1796083200.0}

        映射口径：

        - ``used`` = ``currentUsageWithPrecision``（没有时退 ``currentUsage``）
        - ``total`` = ``usageLimitWithPrecision`` / ``usageLimit``
        - ``reset_ts`` = 该条目的 ``nextDateReset``（**周期性**重置，符合语义）
        - ``unit`` = ``"credit"``（界面的到期告警按这个做量过滤）

        多账号时条目标签带 ``Kiro #N · 订阅名``，与其它多账号通道一致。单账号
        查询失败只跳过它，不拖垮整卡。
        """
        accounts = creds.list_accounts()
        if not accounts:
            return None
        multi = len(accounts) > 1
        items: list[dict[str, Any]] = []
        failed: list[str] = []
        level: str | None = None
        for index, acct in enumerate(accounts, 1):
            try:
                usage = self._usage_one(acct.id)
            except Exception as exc:  # noqa: BLE001 - 单账号失败不阻塞整页
                log.warning("kiro 额度查询失败（%s）: %s", acct.label, exc)
                failed.append(f"#{index}")
                continue
            title = str((usage.get("subscriptionInfo") or {}).get("subscriptionTitle") or "")
            if level is None and title:
                level = title
            # 标签里**始终带账号名**（不只在多账号时加序号）。
            #
            # 用户 2026-11-10 反馈：「看得到额度，但看不到是哪个账号」。单账号
            # 时不带名字，用户没法确认「这 1000 额度属于哪个号」——尤其
            # kiro 会切号（凭据文件被覆盖），过几天再看就分不清了。
            # 名字用 alias（用户可改），没有就用 profileArn 尾部/账号 id 短哈希。
            account_label = acct.label or acct.id
            prefix = (f"Kiro #{index} · {account_label} · " if multi
                      else f"Kiro · {account_label} · ")
            for pack in usage.get("usageBreakdownList") or []:
                if not isinstance(pack, dict):
                    continue
                total = _num(pack.get("usageLimitWithPrecision") or pack.get("usageLimit"))
                used = _num(pack.get("currentUsageWithPrecision") or pack.get("currentUsage"))
                if total is None:
                    continue
                if used is None:
                    used = 0.0
                name = str(pack.get("displayName") or "Credit")
                items.append({
                    "label": f"{prefix}{name}",
                    "used": used,
                    "total": total,
                    "remaining": max(0.0, round(total - used, 2)),
                    "percent": round(used / total * 100) if total else 0,
                    # nextDateReset 是**周期重置**（额度回满），不是权益到期
                    "reset_ts": _num(pack.get("nextDateReset")),
                    "expire_ts": None,
                    "unit": "credit",
                })
        if failed:
            items.insert(0, {
                "label": "Kiro 额度查询失败",
                "used": None, "total": None,
                "remaining": f"{len(failed)}/{len(accounts)} 个账号取不到额度"
                             f"（{'、'.join(failed)}）",
                "percent": None, "reset_ts": None,
                "query_failed": True,
            })
        if not items:
            return None
        return {"items": items, "sum_items": True, "level": level}

    def _usage_one(self, account_id: str) -> dict[str, Any]:
        """单账号 ``getUsageLimits``（同步，benefits 层走 to_thread）。"""
        import urllib.request

        token, cred = creds.ensure_account_token(account_id)
        region = str(cred.get("region") or creds.DEFAULT_REGION)
        # ``isEmailRequired=true`` 才会有 ``userInfo.email``——false 时那个字段是
        # null。用户要求「额度里要看到账号名」，而 profileArn 尾部（KGVMAMAMGGDK）
        # 和本地 id（c-d5c7...）都不是人能认的，邮箱才是。多一个参数不影响用量数字。
        url = (f"https://codewhisperer.{region}.amazonaws.com/getUsageLimits"
               "?origin=AI_EDITOR&isEmailRequired=true")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": f"aws-sdk-js/1.0.0 Kiro/{KIRO_UA_VERSION}",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if not isinstance(data, dict):
            raise RuntimeError("响应不是 JSON 对象")
        return data

    # -- 认证 ---------------------------------------------------------------

    def ensure_auth(self) -> None:
        if not creds.list_accounts():
            raise HTTPException(status_code=401, detail={"error": {
                "message": ("kiro 未登录：请运行 `buddy login kiro`（浏览器授权），"
                            "或在管理页用「导入 auth 文件」带入 "
                            "~/.aws/sso/cache/kiro-auth-token.json"),
                "type": "authentication_error"}})

    def health(self) -> dict[str, Any]:
        accounts = creds.list_accounts()
        return {
            "id": self.id,
            "name": self.name,
            "accounts": len(accounts),
            "models": [str(m.get("id")) for m in self._models_cache],
            **({"models_error": self._models_error} if self._models_error else {}),
        }

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        want = (model or "").strip()
        if not want:
            return False
        bare = want.removeprefix(f"{self.id}/")
        # 两条都认：对外 id（短横线）与上游原名（点号）——用户可能从上游
        # 文档里直接抄了 ``claude-opus-5.5`` 过来。
        return (any(str(m.get("id") or "") == bare for m in self._models_cache)
                or bare in self._id_map)

    # -- 转发 ---------------------------------------------------------------

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        if protocol == "responses":
            raise HTTPException(status_code=501, detail={"error": {
                "message": ("kiro 不支持 responses 协议（上游没有该端点）。"
                            "请改用 openai（/v1/chat/completions）或 "
                            "anthropic（/v1/messages）"),
                "type": "unsupported_protocol"}})

        accounts = creds.list_accounts()
        if not accounts:
            self.ensure_auth()
        stream = bool(body.get("stream", False))
        payload = _chat_to_kiro(body, self._id_map)
        model_id = str(body.get("model") or "").strip()

        last_exc: Exception | None = None
        for acct in accounts:
            try:
                token, cred = await self._token_for(acct)
                region = str(cred.get("region") or acct.region)
                payload["profileArn"] = cred.get("profile_arn") or None
                if not payload["profileArn"]:
                    payload.pop("profileArn", None)
                resp = await self._post_chat(token, region, payload)
                if resp.status_code == 403 and "subscription does not support" in (
                        await resp.aread()).decode("utf-8", "replace"):
                    # 这个特定 403 = 账号没被订阅覆盖，换号有意义
                    last_exc = RuntimeError(f"{acct.label}: 订阅不支持")
                    await resp.aclose()
                    continue
                if resp.status_code >= 400:
                    body_text = (await resp.aread()).decode("utf-8", "replace")
                    last_exc = RuntimeError(f"HTTP {resp.status_code}: {body_text[:200]}")
                    await resp.aclose()
                    # 429/5xx 换号；其它（400 参数错）换号也没用，直接抛
                    if resp.status_code in (429, 500, 502, 503, 504):
                        continue
                    raise HTTPException(status_code=resp.status_code, detail={"error": {
                        "message": f"kiro 上游错误: {body_text[:300]}",
                        "type": "upstream_error"}})
                return await self._respond(resp, protocol, model_id, stream,
                                           input_chars=_input_chars(payload))
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001 - 单账号失败换下一个
                last_exc = exc
                log.warning("kiro 账号 %s 失败: %s", acct.label, describe_exception(exc))
                continue
        detail = describe_exception(last_exc) if last_exc else "无可用账号"
        raise HTTPException(status_code=502, detail={"error": {
            "message": f"kiro 所有账号均失败：{detail}", "type": "bad_gateway"}})

    async def _token_for(self, acct: KiroAccount) -> tuple[str, dict[str, Any]]:
        import asyncio

        return await asyncio.to_thread(creds.ensure_account_token, acct.id)

    async def _post_chat(self, token: str, region: str,
                         payload: dict[str, Any]) -> httpx.Response:
        client = await self._get_client()
        url = f"{endpoint_base(region)}{_CHAT_PATH}"
        headers = {**auth_headers(token),
                   "x-amz-target": ("AmazonCodeWhispererStreamingService"
                                    ".GenerateAssistantResponse")}
        request = client.build_request("POST", url, json=payload, headers=headers)
        return await client.send(request, stream=True)

    async def _respond(self, resp: httpx.Response, protocol: str,
                       model: str, stream: bool, *,
                       input_chars: int = 0) -> StreamingResponse | JSONResponse:
        """把上游 EventStream 转成客户端要的协议。"""
        request_id = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        message_id = "msg_" + uuid.uuid4().hex[:24]

        async def byte_iter() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk
            finally:
                await resp.aclose()

        # 转换器是**同步**的纯字节解析（写成同步更好读，也便于单测），
        # 但数据源是异步的。不搞线程桥接（那会踩「httpx client 绑在另一个
        # event loop」的坑——实测过一次），而是把两个协议的输出都做成
        # **异步生成器**：内部用 async for 收字节、同步解析、立即 yield 出去。
        if stream:
            gen = (self._stream_anthropic(byte_iter(), model, message_id,
                                          input_chars=input_chars)
                   if protocol == "anthropic"
                   else self._stream_openai(byte_iter(), model, request_id, created,
                                            input_chars=input_chars))
            return StreamingResponse(gen, media_type="text/event-stream",
                                     headers=_SSE_HEADERS)

        # 非流式：把事件流聚合完再回一个整体 JSON
        text_parts: list[str] = []
        credit: float | None = None
        ctx_percent: float | None = None
        async for event in self._events(byte_iter()):
            if event["type"] == "text":
                text_parts.append(event["text"])
            elif event["type"] == "credit":
                credit = event["credit"]
            elif event["type"] == "context":
                ctx_percent = event["percent"]
        text = "".join(text_parts)
        chars = len(text)
        # ⚠ ``credit`` 必须放进 **usage 里面**，不能放响应顶层。
        # metrics 的非流式路径读的是 ``payload["usage"]`` 再走 normalize_usage，
        # 顶层字段它根本看不到——踩过一次：响应体里明明有 credit，
        # 日志却记成 0.0/估算。（流式那条路走的是 usage chunk，同样的口径。）
        if protocol == "anthropic":
            return JSONResponse(content={
                "id": message_id, "type": "message", "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": text}] if text else [],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": _usage_payload(chars, credit, anthropic=True,
                                        ctx_percent=ctx_percent,
                                        input_chars=input_chars,
                                        window=_window_for(model)),
            })
        return JSONResponse(content={
            "id": request_id, "object": "chat.completion", "created": created,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": _usage_payload(chars, credit, ctx_percent=ctx_percent,
                                    input_chars=input_chars,
                                    window=_window_for(model)),
        })

    # -- 异步桥接：同步解析器 + 异步字节源 ---------------------------------

    async def _events(self, source: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any]]:
        """把异步字节流跑过同步的 EventStream 解析器。

        解析器是纯 CPU 的字节拼接（见 :mod:`.stream`），不需要异步；但字节源
        是异步的。这里按 chunk 喂进一个增量缓冲，解析出一条事件就 yield 一条
        ——不做整轮回填，首字节体验与上游一致。
        """
        import queue

        sentinel = object()
        buf = b""

        async def drain(final: bool) -> AsyncIterator[dict[str, Any]]:
            nonlocal buf
            while True:
                frame = kiro_stream.take_frame(buf, final=final)
                if frame is None:
                    return
                consumed, headers, payload = frame
                buf = buf[consumed:]
                event = kiro_stream.frame_to_event(headers, payload)
                if event is not None:
                    yield event

        async for chunk in source:
            buf += chunk
            async for event in drain(False):
                yield event
        async for event in drain(True):
            yield event

    async def _stream_openai(self, source: AsyncIterator[bytes], model: str,
                             request_id: str, created: int, *,
                             input_chars: int = 0) -> AsyncIterator[str]:
        first = True
        chars = 0
        credit: float | None = None
        ctx_percent: float | None = None
        async for event in self._events(source):
            if event["type"] == "credit":
                credit = event["credit"]
                continue
            if event["type"] == "context":
                ctx_percent = event["percent"]
                continue
            if event["type"] != "text":
                continue
            chars += len(event["text"])
            delta: dict[str, Any] = {"content": event["text"]}
            if first:
                delta["role"] = "assistant"
                first = False
            yield _sse_json({
                "id": request_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            })
        yield _sse_json({
            "id": request_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        })
        # usage 收尾 chunk：**日志从这里取本次消耗**。
        #
        # ``SSEUsageExtractor``（metrics_protocol）只认带 ``usage`` 字段的行，
        # 而 kiro 的真实计费在 ``meteringEvent`` 里、跟正文是分开的事件。不补
        # 这一帧的话日志的积分列永远是空的——上游明明给了精确到 1e-6 的数字。
        # ``choices`` 必须为空数组（OpenAI 规范里 usage chunk 就是这样）。
        if credit is not None or chars:
            yield _sse_json({
                "id": request_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [],
                "usage": _usage_payload(chars, credit,
                                        ctx_percent=ctx_percent,
                                        input_chars=input_chars,
                                        window=_window_for(model)),
            })
        yield "data: [DONE]\n\n"

    async def _stream_anthropic(self, source: AsyncIterator[bytes], model: str,
                                message_id: str, *,
                                input_chars: int = 0) -> AsyncIterator[str]:
        def ev(name: str, payload: dict[str, Any]) -> str:
            return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        yield ev("message_start", {"type": "message_start", "message": {
            "id": message_id, "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0},
        }})
        yield ev("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "text", "text": ""}})
        chars = 0
        credit: float | None = None
        ctx_percent: float | None = None
        async for event in self._events(source):
            if event["type"] == "credit":
                credit = event["credit"]
                continue
            if event["type"] == "context":
                ctx_percent = event["percent"]
                continue
            if event["type"] != "text":
                continue
            chars += len(event["text"])
            yield ev("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": event["text"]}})
        yield ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        # usage 里带 ``credit``：Anthropic 官方没有这个字段，但本仓库
        # ``normalize_usage`` 会从 usage 里取它（见 metrics_protocol._credit_field），
        # 日志因此能显示 kiro 的真实计费。客户端不认识这个键会忽略，无害。
        yield ev("message_delta", {"type": "message_delta", "delta": {
            "stop_reason": "end_turn", "stop_sequence": None},
            "usage": _usage_payload(chars, credit, anthropic=True,
                                    ctx_percent=ctx_percent,
                                    input_chars=input_chars,
                                    window=_window_for(model))})
        yield ev("message_stop", {"type": "message_stop"})

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client
