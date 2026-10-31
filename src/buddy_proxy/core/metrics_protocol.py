"""请求指标的共享常量、usage 归一化与 SSE 协议提取。"""

from __future__ import annotations

import contextvars
import json
import time
from typing import Any, Optional

# 内存里保留的最近请求明细条数（「最近请求」表格用）
MAX_RECENT = 200
#: 模型页「平均耗时」取最近多少次实测（用户 2026-10-25 要求：近三次）。
RECENT_LATENCY_N = 3
# 启动时回读 metrics.jsonl 的尾部字节数（单条约 200B，约万条请求）
_TAIL_BYTES = 2_000_000
# 聚合只保留最近 N 天，更早的启动回读时丢弃
KEEP_DAYS = 30

# 请求级账号 holder（traepat 多账号 failover 用；其余 provider 单账号不写）。
# 请求开始时 _instrument 放入空 dict，pat.py 选定账号后在深处写入 account id。
# 之所以是「ContextVar 持有可变 dict」而非直接赋值：流式路径的写入发生在
# SSE 读线程/to_thread worker 里，ContextVar 赋值不跨线程传播，但同一 dict
# 对象的变更全局可见，_instrument 落库时从同一对象读回。
ACCOUNT_META: contextvars.ContextVar[Optional[dict[str, Any]]] = contextvars.ContextVar(
    "account_meta", default=None)


def _date_str(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _credit_field(u: dict[str, Any]) -> Any:
    """取单次请求的积分消耗，兼容两种拼写。

    **CodeBuddy 给 ``credit``（单数），Qoder 给 ``credits``（复数）**——只读一个
    就会把另一家的数据整条丢掉（Qoder 的积分此前一直记成 null 就是这个原因，
    连 ``original_credits``/``billable`` 一起摆在 usage 里没人取）。
    两个都在时以 ``credit`` 为准（CodeBuddy 的实际扣费口径）。
    """
    for key in ("credit", "credits", "original_credits"):
        value = u.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def normalize_usage(u: dict[str, Any]) -> dict[str, Any]:
    """把 OpenAI / CodeBuddy / Anthropic 三种 usage 形态归一化。

    - prompt/completion：Anthropic 叫 input/output_tokens
    - 缓存：OpenAI 在 prompt_tokens_details.cached_tokens（prompt 含缓存），
      Anthropic 是 cache_read_input_tokens（input 不含缓存，单列）——
      统一输出 OpenAI 口径：prompt_tokens = input + cache_read + cache_creation
    - credit：单次积分消耗。上游命名不统一（见 :func:`_credit_field`），
      都没有 → None（由上层决定是否估算）
    """
    details = (u.get("prompt_tokens_details")
               or u.get("input_tokens_details") or {})
    credit = _credit_field(u)
    prompt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    cached = int(u.get("cached_tokens") or details.get("cached_tokens")
                 or u.get("cache_read_input_tokens") or 0)
    cache_write = int(u.get("cache_creation_input_tokens") or 0)
    if u.get("cache_read_input_tokens") is not None or cache_write:
        # Anthropic 官方口径：input_tokens 不含缓存命中/创建，缓存单列——
        # 统一成 OpenAI 口径（prompt 含 cached），UI 各通道才能横向对比。
        # OpenAI/Responses 形态没有这两个顶层字段，prompt 本就含缓存，勿重复累加
        prompt += cached + cache_write
    return {
        "prompt_tokens": prompt,
        "completion_tokens": int(u.get("completion_tokens") or u.get("output_tokens") or 0),
        "cached_tokens": cached,
        "credit": float(credit) if credit is not None else None,
    }


class SSEUsageExtractor:
    """从上游 SSE 字节流里轻量提取 token usage 与 credit。

    兼容 OpenAI（usage 在末尾 chunk 一次性给）与 Anthropic
    （message_start 给 input、message_delta 累计 output）两种形态；
    跨 chunk 断行由内部缓冲处理；只对包含 "usage" 的行做 JSON 解析，
    高吞吐流的开销可忽略。
    """

    def __init__(self) -> None:
        self._buf = b""
        self.usage: dict[str, Any] = {}

    def feed(self, chunk: bytes | str) -> None:
        # 各 provider 产出类型不一：codebuddy/zcode 流是 bytes，trae 流是 str
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        if b"usage" not in chunk and not self._buf:
            return
        self._buf += chunk
        lines = self._buf.split(b"\n")
        self._buf = lines.pop()  # 最后一段可能是残行，留给下一个 chunk
        for line in lines:
            line = line.strip()
            if b'"usage"' not in line:
                continue
            if line.startswith(b"data:"):
                line = line[5:].strip()
            try:
                payload = json.loads(line)
            except Exception:
                continue
            u = payload.get("usage")
            if not isinstance(u, dict):
                u = (payload.get("message") or {}).get("usage")
            if not isinstance(u, dict):
                # Responses 协议：response.completed 事件的 usage 在
                # payload["response"]["usage"]
                u = (payload.get("response") or {}).get("usage")
            if not isinstance(u, dict):
                continue
            for k, v in normalize_usage(u).items():
                # Anthropic 的 output_tokens 逐事件累计，取 max 即最终值
                cur = self.usage.get(k)
                self.usage[k] = v if cur is None else max(cur, v or 0)


class SSEErrorExtractor:
    """从上游 SSE 字节流里提取**带内错误**（provider 用正常 SSE 事件报的错）。

    **Why:** codebuddy 的 ``pipeline.stream_upstream`` 把上游错误做成了带内事件
    （``{"error": {...}}`` / ``event: error``）而不是抛异常——流因此**正常结束**，
    ``_metrics_stream`` 看不到任何异常、按 200 记账。2026-10-02 实测就是这样：
    一条 codebuddy 请求实际只发了 54 字节错误体、客户端什么都没收到，
    指标里却是 ``status=200, chunk_count=3, error=""`` 的漂亮记录（假绿灯）。
    UI 的失败率、模型可用性判断全建立在这份数据上，假绿灯会让排查彻底跑偏。

    只认**明确的错误信号**，不猜语义：Anthropic/OpenAI 的 ``error`` 事件、
    Responses 的 ``type: error``、doubao 的 ``{"type": "error"}``。正常内容里
    出现 "error" 这个词不算。
    """

    def __init__(self) -> None:
        self._buf = b""
        self.message: str = ""

    def feed(self, chunk: bytes | str) -> None:
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        # 必须先无条件缓冲再判断：「error」一词可能被 chunk 边界从中间劈开
        # （b'data: {"err' + b'or":...），先判后缓冲会让两半都漏掉。
        # 缓冲区只留最后一个残行，大小有界（与 SSEUsageExtractor 同一套纪律）。
        self._buf += chunk
        if b"error" not in self._buf:
            # 无错误的流残行也留不住太多字节：有换行就清掉已扫过的部分
            lines = self._buf.rsplit(b"\n", 1)
            self._buf = lines[-1] if len(lines) == 2 else self._buf
            return
        lines = self._buf.split(b"\n")
        self._buf = lines.pop()  # 残行留给下一个 chunk
        for line in lines:
            line = line.strip()
            # 行级过滤与缓冲区判断保持**同样区分大小写**：上游错误键按协议都是
            # 小写（"error" / event: error），大小写混判只会造成「缓冲区漏过、
            # 行级才认得」的静默不一致。
            if not line or b"error" not in line:
                continue
            if line.startswith(b"data:"):
                line = line[5:].strip()
            elif line.startswith(b"event:"):
                # Anthropic 的 `event: error` 只是**声明**下面 data 行是错误事件，
                # 真正的原因在 data 里。这里只做兜底标记（data 行解析出消息时会
                # 覆盖它），让「有 event 行但 data 解析失败」的情况也不至于漏报。
                if b"error" in line and not self.message:
                    self.message = "in-band error event"
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            err = payload.get("error")
            if err is None and payload.get("type") == "error":
                err = payload.get("message") or payload
            if err is None:
                continue
            if isinstance(err, dict):
                err = err.get("message") or err.get("type") or "in-band error"
            # 第一个错误即真因，后续不覆盖；但兜底占位文案要让位给真实消息。
            # **不能**在这里提前 return：feed() 会被逐 chunk 反复调用以重组断行，
            # 中途跳出会丢字节。
            text = str(err)[:300]
            if not self.message or self.message == "in-band error event":
                self.message = text
