"""Trae SSE 解析与 Anthropic 流包装。"""

from __future__ import annotations

import codecs
import json
import logging
from typing import Any, Iterator

from ..protocols.anthropic_adapter import AnthropicStreamConverter

log = logging.getLogger(__name__)

# ───────────────────────── SSE 解析 ─────────────────────────

# Trae 错误码 -> 友好中文文案（官方 docs.trae.ai/ide/error-codes）
_TRAE_ERROR_HINTS: dict[int, str] = {
    1005: "Trae plan 权益不足（当前账号/模型组合无权限，可检查会员套餐或换模型）",
    3003: "Trae 模型暂不可用（今日额度可能已用完，或当前模型无权限）",
    3004: "Trae 当前模型访问量过大，请稍后重试",
    4001: "Trae 服务端错误，请稍后重试",
    4007: "Trae 请求限流，请稍后重试",
    4008: "Trae 账号请求额度已超限",
    4010: "Trae 检测到风险账号，已自动登出，请重新登录",
    4011: "Trae AI 问答今日用量已达上限，请明日再试",
    4013: "Trae AI 服务在当前地区不可用",
    4015: "Trae 检测到账号/IP 风险，请求已被阻止",
    4021: "Trae 今日会话次数已达上限，请明日再试",
    4022: "Trae 请求失败，请尝试新建会话并重试",
    4023: "Trae 模型列表已更新，请确认后重试",
    4031: "Trae 今日请求额度已用完，请明日再试（每日 0 点重置）",
    4050: "Trae 请求超时，模型服务资源紧张，请稍后重试",
    4051: "Trae 请求超时，模型服务资源紧张，请稍后重试",
}

# 「换一个账号可能好转」的上游 SSE 错误码（work 多账号 failover 分类用）。
# 额度是**账号级**的（4008 quota exceeded / 4011 问答日限 / 4021 会话日限 /
# 4031 日额度包耗尽），A 号撞了 B 号可能还有；鉴权同理（1001 token 失效 /
# 4010 风险登出）。其余如 4001（服务端错）/ 4013（地区不支持）是通道级，
# 换号无意义。PAT 通道另有含 PAT 专属码的表（pat/config._FAILOVER_SSE_CODES），
# 两份各管各的账号体系。
_ACCOUNT_QUOTA_SSE_CODES = frozenset({4008, 4011, 4021, 4031})
_ACCOUNT_AUTH_SSE_CODES = frozenset({1001, 4010})
#: 1005「套餐权益不足」是**账号+模型**级：免费号不能用 glm-5.4，但 glm-5.3 照常
#: 可服务。换号有意义（付费号可能有权益），所以归 403 让 failover 换号；但它**不能**
#: 走 failover.mark_cooldown 的整账号冷却（会误伤该账号对其他模型的服务），forward
#: 循环据 ``trae_sse_code`` 识别后改记 (账号, 模型) 级跳过，见 provider._mark_entitlement_skip。
_ACCOUNT_ENTITLEMENT_SSE_CODES = frozenset({1005})
#: 4026「上下文超限」是**账号+模型**级：真实上限按账号浮动（2026-11-26 实测，
#: 同一 ~257k 请求 A 号 4026、B 号正常），换号有意义；但账号没坏，不能整号冷却。
#: 与 1005 同款处置：归 403 让 failover 换号，forward 据 ``trae_sse_code`` 改记
#: (账号, 模型) 级跳过，见 provider._mark_entitlement_skip。
_ACCOUNT_CONTEXT_SSE_CODES = frozenset({4026})


def _sse_error_status(code: Any) -> int:
    """上游 SSE 错误码 → 账号级 HTTP 状态码（failover 循环只认 401/403/429）。

    额度类归 429（冷却带 quota 标记）、鉴权类归 401、权益类（1005）与上下文
    超限（4026）归 403，其余 502 原样透传——502 不算账号级错误，failover 不
    换号。解析不出码也按 502。
    """
    try:
        c = int(code)
    except (TypeError, ValueError):
        return 502
    if c in _ACCOUNT_QUOTA_SSE_CODES:
        return 429
    if c in _ACCOUNT_AUTH_SSE_CODES:
        return 401
    if c in _ACCOUNT_ENTITLEMENT_SSE_CODES or c in _ACCOUNT_CONTEXT_SSE_CODES:
        return 403
    return 502


def _trae_error_text(data: dict[str, Any]) -> str:
    """把 Trae 错误事件转成友好中文文案（附错误码，便于定位）。"""
    code = data.get("code")
    msg = data.get("message") or data.get("error") or ""
    if isinstance(code, int) and code in _TRAE_ERROR_HINTS:
        hint = _TRAE_ERROR_HINTS[code]
        text = hint + (f"（{msg}）" if msg and msg not in hint else "")
    elif msg:
        text = f"Trae 错误: {msg}"
    else:
        text = "Trae 未知错误"
    if code is not None and str(code) not in text:
        text = f"{text} (code: {code})"
    return text


def _openai_error_type(status: int) -> str:
    """HTTP 状态码 → OpenAI 口径的 ``error.type``。

    错误体必须是 ``{"error": {"message", "type"}}``：客户端（Kimi / DSH /
    OpenAI SDK）靠 ``type`` 分类，靠状态码只能猜。裸字符串 detail 会被序列化
    成 ``{"detail": "..."}``，Kimi 直接归成 ``provider.auth_error`` 弹重新登录
    ——而真因往往是「这个模型该账号没权益」这种和登录无关的事。
    """
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 429:
        return "rate_limit_error"
    if status == 402:
        return "insufficient_quota"
    if status in (400, 404, 422):
        return "invalid_request_error"
    if status == 504:
        return "timeout"
    return "api_error"


class _SSEDecoder:
    """增量解码 SSE，允许 UTF-8、行和事件跨任意传输块边界。"""

    def __init__(self) -> None:
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        self._text = ""
        self._event = ""
        self._data: list[str] = []

    def _flush_event(self) -> tuple[str, dict[str, Any]] | None:
        if not self._data:
            self._event = ""
            return None
        raw = "\n".join(self._data)
        self._data.clear()
        event = self._event
        self._event = ""
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw}
        return event, parsed

    def _line(self, line: str) -> tuple[str, dict[str, Any]] | None:
        # SSE 只移除协议允许的单个前导空格；正文尾部空格必须保留。
        if line.endswith("\r"):
            line = line[:-1]
        if not line:
            return self._flush_event()
        if line.startswith(":"):
            return None
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        return None

    def feed(self, chunk: bytes | str) -> list[tuple[str, dict[str, Any]]]:
        if isinstance(chunk, bytes):
            self._text += self._utf8.decode(chunk)
        else:
            self._text += chunk
        lines = self._text.split("\n")
        self._text = lines.pop()
        events: list[tuple[str, dict[str, Any]]] = []
        for line in lines:
            event = self._line(line)
            if event is not None:
                events.append(event)
        return events

    def finish(self) -> list[tuple[str, dict[str, Any]]]:
        self._text += self._utf8.decode(b"", final=True)
        events: list[tuple[str, dict[str, Any]]] = []
        if self._text:
            event = self._line(self._text)
            self._text = ""
            if event is not None:
                events.append(event)
        event = self._flush_event()
        if event is not None:
            events.append(event)
        return events


def _parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """解析完整 Trae SSE 文本；规则与流式增量 decoder 完全一致。"""
    decoder = _SSEDecoder()
    return decoder.feed(text) + decoder.finish()


# ───────────────────────── Provider 实现 ─────────────────────────

def _anthropic_sse(event_name: str, payload: dict[str, Any]) -> str:
    """构造一条 Anthropic SSE 事件（event + data）。"""
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _wrap_anthropic_stream(
    openai_stream: Iterator[str], model: str
) -> Iterator[str]:
    """把 TraeProvider._stream 的 OpenAI chat chunk SSE 流包成 Anthropic 事件流。

    /v1/messages（Claude Code 等 Anthropic 客户端）经 anthropic_to_chat 转成
    chat 请求后路由到 Trae；本包装器把 OpenAI chunk 喂给
    AnthropicStreamConverter，输出 message_start / content_block_delta /
    message_stop 等标准事件（含 reasoning_content → thinking 块、
    tool_calls → tool_use 块）。_stream 是同步生成器，此处保持同步迭代。
    """
    converter = AnthropicStreamConverter(model)
    for piece in openai_stream:
        for line in piece.splitlines():
            line = line.strip()
            if line.startswith(":"):
                # SSE 注释行（上游用 ": heartbeat" 续命）：原样透传，喂饱
                # 下游 Anthropic 客户端（Claude Code）的 per-chunk 读超时计时器。
                # 不透传的话长时思考期（首字可迟至 TRAE_SEMANTIC_TIMEOUT）下游
                # 收不到任何字节会误判断流而中止——正是心跳要防的失败。
                yield line + "\n\n"
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(chunk, dict) and chunk.get("error"):
                # 错误 chunk → Anthropic error 事件（而非塞进正文文本）
                err = chunk["error"]
                msg = str(err.get("message", err))
                code = err.get("code")
                if code is not None and str(code) not in msg:
                    msg = f"{msg} (code: {code})"
                # 若已产出内容（有块处于打开态），先补 content_block_stop 收尾，
                # 否则严格客户端会因块悬空而卡死；再发 error 并结束。
                for event_name, payload in converter.close_open_blocks():
                    yield _anthropic_sse(event_name, payload)
                yield _anthropic_sse("error", {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": msg,
                    },
                })
                return
            for event_name, payload in converter.feed_chunk(chunk):
                yield _anthropic_sse(event_name, payload)
    for event_name, payload in converter.finish():
        yield _anthropic_sse(event_name, payload)
    # 与 CodeBuddy 通道的 anthropic 流保持一致：message_stop 后补 [DONE]
    # 确保 SSE 客户端立即结束等待
    yield "data: [DONE]\n\n"

