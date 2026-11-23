"""ZCode Start Plan (Trust Build) provider —— Anthropic 兼容端点直通转发。

上游为智谱 Z.ai 的 Trust Build / Start Plan 通道（与 ZCode CLI 的
bigmodel-start-plan 配置同款）：

- Base: https://zcode.z.ai/api/v1/zcode-plan/anthropic
- Endpoint: {base}/v1/messages（Anthropic 协议）
- Quota: {base}/api/v1/zcode-plan/billing/current（HTTP GET, Bearer JWT）

关键发现（风控绕过）：
- 请求体必须包含 ``metadata.user_id``，值为字符串化 JSON：
  ``{"device_id":"<deviceMid>","account_uuid":"","session_id":"<sessionId>"}``
- deviceMid 来自 ~/.zcode/v2/telemetry-state.json
- 认证形态：**双头**同时发送 ``Authorization: Bearer <JWT>`` 和 ``x-api-key: <JWT>``
- 额外 header：anthropic-beta: mid-conversation-system-2026-04-07、x-device-mid

套餐权限（2026-10-05 实测）：
- 模型：仅支持 glm-5.3-flash
- 额度：每日刷新（ends_at 当天 24:00），100,000,000 token
- one_time 类型，priority 110

凭证来源优先级：
1. 环境变量 ``ZCODE_START_API_KEY``
2. 本项目 key 文件 ``~/.buddy-proxy/zcode_start_api_key``
3. 本机 ZCode CLI 配置 ``~/.zcode/cli/config.json`` 中 ``builtin:bigmodel-start-plan``
   的 apiKey（与解密后的 zcodejwttoken 相同）

安全：API key/JWT 只在服务端使用，绝不明文进日志；状态目录 0700、文件 0600。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .base import BaseProvider
from ..core.errors import describe_exception
from ..core.paths import state_file

log = logging.getLogger(__name__)

# 加载系统提示模板（风控必需：完整系统提示才能过 3012）
_SYSTEM_BLOCKS_PATH = Path(__file__).parent / "zcode_start_system_blocks.json"
try:
    _SYSTEM_BLOCKS = json.loads(_SYSTEM_BLOCKS_PATH.read_text())
except Exception:
    _SYSTEM_BLOCKS = []
    log.warning("zcode-start: system blocks template not found at %s", _SYSTEM_BLOCKS_PATH)

_SYSTEM_PROMPT = "\n\n".join(_SYSTEM_BLOCKS) if _SYSTEM_BLOCKS else ""

# ---------------------------------------------------------------------------
# 常量与默认模型表
# ---------------------------------------------------------------------------

ZCODE_START_ANTHROPIC_BASE = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"

#: Start Plan（Trust Build）**只有 glm-5.3-flash**，别跟着 ``providers/zcode.py``
#: 一起加 GLM-5.4。
#:
#: 2026-11-23 接 5.4 时专门确认过（当时有人问「是不是漏改了这边」）：
#:
#: * 这是**另一个套餐**，走 ``zcode-plan/anthropic`` 端点，与 coding-plan 的
#:   ``open.bigmodel.cn/api/anthropic`` 不是一条线；
#: * 本机 ZCode CLI 自己的配置（``~/.zcode/cli/config.json``）里，
#:   ``bigmodel-start-plan`` 的默认模型仍是 ``glm-5.3-flash``——官方客户端
#:   都没把 5.4 挂到这个套餐上；
#: * 本机没配 ``~/.buddy-proxy/zcode_start_api_key``，无法直接实测。
#:
#: 所以这里是**确认过的边界**，不是遗漏。等 Start Plan 放开 5.4（或拿到 key
#: 实测通过）再加，加的时候三张表一起补。
DEFAULT_MODELS: dict[str, str] = {
    "glm-5.3-flash": "GLM-5.3-Flash (Start Plan)",
}

MODEL_NAME_CANONICAL: dict[str, str] = {
    "glm-5.3-flash": "glm-5.3-flash",
}

_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0)

# ---------------------------------------------------------------------------
# 凭证解析
# ---------------------------------------------------------------------------


def secret_file_path() -> Path:
    """本项目自己的 key 文件：``~/.buddy-proxy/zcode_start_api_key``。"""
    return state_file("zcode_start_api_key")


def _load_secret_file() -> str:
    """读 key 文件，支持 ``name=value`` 或裸 value。"""
    try:
        for raw in secret_file_path().read_text().splitlines():
            line = raw.strip()
            if not line:
                continue
            return line.split("=", 1)[1].strip() if "=" in line else line
    except Exception:
        pass
    return ""


def _load_zcode_start_config_key() -> tuple[str, str]:
    """从本机 ZCode CLI 配置读取 bigmodel-start-plan 的 apiKey 与 baseURL。"""
    try:
        cfg = json.loads((Path.home() / ".zcode" / "cli" / "config.json").read_text())
    except Exception:
        return "", ""
    model = cfg.get("model", "")
    providers = cfg.get("provider") or {}
    # 优先找 bigmodel-start-plan
    if "bigmodel-start-plan" in providers:
        p = providers["bigmodel-start-plan"]
        if isinstance(p, dict) and p.get("kind") == "anthropic":
            opts = p.get("options") or {}
            key = opts.get("apiKey") or ""
            base = opts.get("baseURL") or ""
            if key:
                return key, base.rstrip("/")
    # fallback: 遍历找 enabled=True 的 anthropic provider
    ordered = sorted(
        providers.items(),
        key=lambda kv: bool((kv[1] or {}).get("enabled")),
        reverse=True,
    )
    for _pid, p in ordered:
        if not isinstance(p, dict) or p.get("kind") != "anthropic":
            continue
        opts = p.get("options") or {}
        key = opts.get("apiKey") or ""
        base = opts.get("baseURL") or ""
        if key:
            return key, base
    return "", ""


def resolve_credentials() -> tuple[str, str]:
    """解析 (api_key, anthropic_base_url)。来源优先级见模块 docstring。"""
    key = os.environ.get("ZCODE_START_API_KEY", "").strip()
    if not key:
        key = _load_secret_file()
    base = ""
    if not key:
        key, base = _load_zcode_start_config_key()
    if not base:
        base = ZCODE_START_ANTHROPIC_BASE
    return key, base.rstrip("/")


def _load_device_mid() -> str | None:
    """从 ~/.zcode/v2/telemetry-state.json 读取 deviceMid。"""
    try:
        data = json.loads((Path.home() / ".zcode" / "v2" / "telemetry-state.json").read_text())
        mid = data.get("deviceMid", "")
        return mid if isinstance(mid, str) and mid else None
    except Exception:
        return None


def _build_metadata_user_id(session_id: str) -> str:
    """构建 metadata.user_id（字符串化 JSON）。"""
    mid = _load_device_mid()
    if not mid:
        log.warning("zcode-start: deviceMid not found, request may be blocked by risk control")
        return "{}"
    return json.dumps({
        "device_id": mid,
        "account_uuid": "",
        "session_id": session_id,
    }, ensure_ascii=False)


def _required_system_blocks() -> list[dict[str, Any]]:
    """ZCode 风控要求的系统块：模板的每块各带一个 cache_control 断点。"""
    return [
        {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}
        for text in _SYSTEM_BLOCKS
    ]


def _client_system_blocks(system: Any) -> list[dict[str, Any]]:
    """客户端自带 system → 合并用的纯文本块（不带客户端的 cache_control）。

    断点要保持「前置 ZCode 块各一个」的形状：客户端块再带 cache_control 既会把
    断点数撑过 Anthropic 上限，也会让风控指纹漂移。
    """
    out: list[dict[str, Any]] = []
    if isinstance(system, str):
        if system:
            out.append({"type": "text", "text": system})
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict):
                text = block.get("text")
                if text:
                    out.append({"type": "text", "text": text})
            elif isinstance(block, str) and block:
                out.append({"type": "text", "text": block})
    return out


def _merged_system(client_system: Any) -> list[dict[str, Any]]:
    """最终 system：ZCode 块永远在最前，客户端 system 合并在后。

    2026-10-06 实测：anthropic 路径原先只在**没有** system 时才注入 ZCode 块，
    而 Claude Code 的请求一律自带 system（``/model`` 探测、正常对话都是），
    注入被跳过后整条通道撞 3012「unusual activity」（HTTP 405）。openai→anthropic
    转换路径（``_openai_to_anthropic_request``）一直是合并口径且实测可过，
    两边对齐。"""
    return _required_system_blocks() + _client_system_blocks(client_system)


def _openai_to_anthropic_request(body: dict[str, Any]) -> dict[str, Any]:
    """将 OpenAI chat completions 请求体转换为 Anthropic Messages 请求体。

    关键映射：
        messages 中的 system → 顶层 system 参数
        messages 中的 user/assistant → messages（展开 content blocks）
        max_tokens → max_tokens
        model → model
        stream → stream

    注入完整的 ZCode 系统提示（风控必需：过 3012 检测）。
    """
    messages = body.get("messages") or []
    system_blocks: list[dict[str, Any]] = []
    anthropic_messages: list[dict[str, Any]] = []

    # 先注入 ZCode 系统提示（风控必需）
    # 风控要求：3 个 system 块，每个带 cache_control
    for block_text in _SYSTEM_BLOCKS:
        system_blocks.append({"type": "text", "text": block_text, "cache_control": {"type": "ephemeral"}})

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            # system message → anthropic system parameter
            if isinstance(content, str):
                system_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        system_blocks.append({"type": "text", "text": block.get("text", "")})
        else:
            # user / assistant → anthropic messages
            anthropic_msg: dict[str, Any] = {"role": role}
            if isinstance(content, str):
                anthropic_msg["content"] = content
            elif isinstance(content, list):
                anthropic_msg["content"] = content
            else:
                anthropic_msg["content"] = str(content)

            # tool_calls (openai) → tool_use blocks (anthropic)
            if "tool_calls" in msg and msg["tool_calls"]:
                content_blocks = []
                if isinstance(anthropic_msg.get("content"), str):
                    content_blocks.append({"type": "text", "text": anthropic_msg["content"]})
                elif isinstance(anthropic_msg.get("content"), list):
                    content_blocks.extend(anthropic_msg["content"])

                for tc in msg["tool_calls"]:
                    fn = tc.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        args = fn.get("arguments", "")
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    })
                anthropic_msg["content"] = content_blocks

            # tool role (openai) → user role with tool_result (anthropic)
            if role == "tool":
                anthropic_msg["role"] = "user"
                anthropic_msg["content"] = [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": str(content),
                }]

            anthropic_messages.append(anthropic_msg)

    result: dict[str, Any] = {
        "model": body.get("model", ""),
        "max_tokens": body.get("max_tokens", 4096),
        "messages": anthropic_messages,
    }
    if system_blocks:
        result["system"] = system_blocks
    if body.get("stream"):
        result["stream"] = True
    if body.get("temperature") is not None:
        result["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        result["top_p"] = body["top_p"]
    if body.get("stop") is not None:
        stop = body["stop"]
        result["stop_sequences"] = [stop] if isinstance(stop, str) else stop

    # tools conversion (openai format → anthropic format)
    if body.get("tools"):
        anthropic_tools = []
        for tool in body["tools"]:
            if tool.get("type") == "function":
                fn = tool.get("function") or {}
                anthropic_tools.append({
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {}),
                })
        if anthropic_tools:
            result["tools"] = anthropic_tools

    return result


def _auth_headers(api_key: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """上游是 Anthropic 兼容端点，用双头认证（CLI 实证形态）。"""
    headers = {
        "authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "mid-conversation-system-2026-04-07",
        "Content-Type": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


# ---------------------------------------------------------------------------
# 上游响应处理（在提交 200 前校验成功响应，避免错误体伪装成功）
# ---------------------------------------------------------------------------


_FIRST_EVENT_LIMIT = 64 * 1024


def _is_message(payload: Any) -> bool:
    return (isinstance(payload, dict) and payload.get("type") == "message"
            and payload.get("role") == "assistant"
            and isinstance(payload.get("content"), list))


def _invalid_success(payload: Any) -> JSONResponse:
    """HTTP 200 错误体不能让客户端误认为 Anthropic Message。"""
    node = payload.get("error") if isinstance(payload, dict) else None
    error = node if isinstance(node, dict) else payload if isinstance(payload, dict) else {}
    code = error.get("code") or (payload.get("code") if isinstance(payload, dict) else None)
    message = (error.get("message") or error.get("msg")
               or (node if isinstance(node, str) else None)
               or "zcode-start upstream returned invalid message")
    message = str(message)[:500]
    # Start Plan 的额度错误可能以 HTTP 200 返回；让模型顺序在响应提交前换档。
    quota = str(code) in {"1308", "429"} or any(word in message.lower() for word in (
        "quota", "rate limit", "usage limit", "额度", "限额", "余额不足",
    ))
    status = 429 if quota else 502
    return JSONResponse(status_code=status, content={
        "type": "error",
        "error": {"type": "rate_limit_error" if quota else "upstream_error",
                  "message": message, **({"code": code} if code is not None else {})},
    })


async def _checked_stream(response: httpx.Response) -> StreamingResponse | JSONResponse:
    """读取首个有效 SSE message_start 后才提交流；错误帧先变成 HTTP 错误。"""
    iterator = response.aiter_bytes()
    prefix = bytearray()
    handed_off = False
    try:
        # JSON 响应（即使 HTTP 200）不应标成 text/event-stream。
        if "text/event-stream" not in response.headers.get("content-type", "").lower():
            async for chunk in iterator:
                prefix.extend(chunk)
                if len(prefix) > _FIRST_EVENT_LIMIT:
                    return _invalid_success(None)
            try:
                return _invalid_success(json.loads(prefix))
            except (ValueError, TypeError):
                return _invalid_success(None)

        # SSE 逐行扫描，原始字节保留给直通客户端；只规范化用于解析的完整行。
        # 与整个缓冲区 replace(CRLF) 不同，这也能处理 CR/LF 分落两个网络块。
        line_buffer = bytearray()
        data_lines: list[bytes] = []
        event_name = ""
        original_prefix: list[bytes] = []
        total = 0
        async for chunk in iterator:
            total += len(chunk)
            if total > _FIRST_EVENT_LIMIT:
                return _invalid_success(None)
            original_prefix.append(chunk)
            for byte in chunk:
                if byte != 10:  # LF；结尾 CR 在读到 LF 时清除
                    line_buffer.append(byte)
                    continue
                line = bytes(line_buffer).removesuffix(b"\r")
                line_buffer.clear()
                if line:
                    if line.startswith(b"event:"):
                        event_name = line[6:].strip().decode(errors="replace")
                    elif line.startswith(b"data:"):
                        data_lines.append(line[5:].strip())
                    continue
                if not data_lines:
                    event_name = ""  # 空心跳帧
                    continue
                try:
                    event = json.loads(b"\n".join(data_lines))
                except ValueError:
                    return _invalid_success(None)
                data_lines = []
                if not isinstance(event, dict):
                    return _invalid_success(None)
                etype = event.get("type") or event_name
                event_name = ""
                if etype == "error":
                    return _invalid_success(event)
                if etype == "message_start" and _is_message(event.get("message")):
                    async def _rest() -> AsyncIterator[bytes]:
                        try:
                            for buffered in original_prefix:
                                yield buffered
                            async for remaining in iterator:
                                if remaining:
                                    yield remaining
                        finally:
                            await response.aclose()
                    handed_off = True
                    return StreamingResponse(
                        _rest(), media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "close"},
                    )
                return _invalid_success(None)
        return _invalid_success(None)
    finally:
        # 成功时连接所有权交给 _rest；失败时尚未提交响应，必须直接释放。
        if not handed_off:
            await response.aclose()


def _upstream_error_response(resp: httpx.Response) -> JSONResponse:
    """把上游错误转成客户端错误响应（透传状态码与错误体，隐藏 key 痕迹）。"""
    try:
        payload = resp.json()
    except Exception:
        payload = {"error": {"message": resp.text[:500], "type": "upstream_error"}}
    return JSONResponse(status_code=resp.status_code, content=payload)


# ---------------------------------------------------------------------------
# anthropic → openai 响应转换（openai 协议客户端直连时用）
# ---------------------------------------------------------------------------


def _anthropic_message_to_chat(payload: dict[str, Any], requested_model: str) -> dict[str, Any]:
    """非流式：Anthropic Messages 响应 → OpenAI chat.completion 响应体。"""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text") or ""))
        elif block.get("type") == "thinking":
            reasoning_parts.append(str(block.get("thinking") or ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": block.get("name") or "",
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                },
            })

    stop_reason = payload.get("stop_reason")
    finish = "tool_calls" if tool_calls else {
        "max_tokens": "length",
        "stop_sequence": "stop",
        "refusal": "content_filter",
    }.get(stop_reason, "stop")

    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage_node = payload.get("usage") or {}
    return {
        "id": payload.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish,
        }],
        "usage": {
            "prompt_tokens": int(usage_node.get("input_tokens") or 0),
            "completion_tokens": int(usage_node.get("output_tokens") or 0),
        },
    }


async def _anthropic_sse_to_openai_stream(
    response: httpx.Response,
    requested_model: str,
) -> AsyncIterator[bytes]:
    """流式：上游 Anthropic SSE → OpenAI chat.completion.chunk SSE。

    解析 message_start / content_block_delta / message_delta / message_stop，
    逐段重组为 OpenAI delta 协议；thinking 块映射为 reasoning_content。
    """
    def _dump(chunk: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

    def _chunk(delta: dict[str, Any] | None = None,
               finish: str | None = None,
               usage: dict[str, Any] | None = None) -> dict[str, Any]:
        choice: dict[str, Any] = {"index": 0, "delta": delta or {}}
        if finish:
            choice["finish_reason"] = finish
        out = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": requested_model,
            "choices": [choice],
        }
        if usage:
            out["usage"] = usage
        return out

    # tool_use 按内容块 index 聚合（input_json_delta 分片拼接 arguments）
    tool_buffers: dict[int, dict[str, Any]] = {}
    usage_final: dict[str, Any] | None = None
    finish: str | None = None
    opened_role = False

    try:
        buf = b""
        async for raw in response.aiter_bytes():
            buf += raw
            # 上游合法 SSE 可用 CRLF 分隔，按完整行寻找帧界，不依赖 \n\n 子串。
            while True:
                normalized = buf.replace(b"\r\n", b"\n")
                end = normalized.find(b"\n\n")
                if end < 0:
                    break
                frame, buf = normalized[:end], normalized[end + 2:]
                event_name = ""
                data_lines: list[bytes] = []
                for line in frame.split(b"\n"):
                    if line.startswith(b"event:"):
                        event_name = line[6:].strip().decode(errors="replace")
                    elif line.startswith(b"data:"):
                        data_lines.append(line[5:].strip())
                if not data_lines:
                    continue
                try:
                    data = json.loads(b"\n".join(data_lines))
                except json.JSONDecodeError:
                    continue
                etype = data.get("type") or event_name

                if etype == "message_start":
                    if not opened_role:
                        opened_role = True
                        yield _dump(_chunk(delta={"role": "assistant", "content": ""}))
                elif etype == "content_block_start":
                    block = data.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        tool_buffers[data.get("index", 0)] = {
                            "id": block.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                            "name": block.get("name") or "",
                            "args": "",
                        }
                elif etype == "content_block_delta":
                    delta = data.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta" and delta.get("text"):
                        yield _dump(_chunk(delta={"content": delta["text"]}))
                    elif dtype == "thinking_delta" and delta.get("thinking"):
                        yield _dump(_chunk(delta={"reasoning_content": delta["thinking"]}))
                    elif dtype == "input_json_delta":
                        tb = tool_buffers.get(data.get("index", 0))
                        if tb is not None:
                            tb["args"] += str(delta.get("partial_json") or "")
                elif etype == "message_delta":
                    delta = data.get("delta") or {}
                    if delta.get("stop_reason"):
                        finish = {
                            "max_tokens": "length",
                            "stop_sequence": "stop",
                            "refusal": "content_filter",
                        }.get(delta["stop_reason"], "stop")
                    if data.get("usage"):
                        u = data["usage"]
                        usage_final = {
                            "prompt_tokens": int(u.get("input_tokens") or 0),
                            "completion_tokens": int(u.get("output_tokens") or 0),
                        }
                elif etype == "message_stop":
                    break
        if tool_buffers:
            # anthropic 流里 tool_use 在文本之后才结束，OpenAI 协议要求
            # tool_calls delta 按序吐出；聚合完一次性发（上游本来就一次给全）。
            for i, tb in enumerate(tool_buffers.values()):
                yield _dump(_chunk(delta={"tool_calls": [{
                    "index": i,
                    "id": tb["id"],
                    "type": "function",
                    "function": {"name": tb["name"], "arguments": tb["args"] or "{}"},
                }]}))
            if finish == "stop":
                finish = "tool_calls"
        yield _dump(_chunk(delta={}, finish=finish or "stop", usage=usage_final))
        yield b"data: [DONE]\n\n"
    finally:
        await response.aclose()


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class ZCodeStartPlanProvider(BaseProvider):
    id = "zcode-start"
    name = "ZCode Start Plan"

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        if api_key or base_url:
            self._api_key = api_key or ""
            self._base = (base_url or ZCODE_START_ANTHROPIC_BASE).rstrip("/")
        else:
            key, base = resolve_credentials()
            self._api_key = key
            self._base = base
        self._client: httpx.AsyncClient | None = None

    # ---- BaseProvider 接口 ----

    def models(self) -> Sequence[dict[str, Any]]:
        return [
            {
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "description": desc,
            }
            for model_id, desc in DEFAULT_MODELS.items()
        ]

    def ensure_auth(self) -> None:
        if not self._api_key:
            self._api_key, self._base = resolve_credentials()
        if not self._api_key:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": (
                            "zcode-start 未配置认证：请设置 ZCODE_START_API_KEY / "
                            "~/.buddy-proxy/zcode_start_api_key，或在本机 ZCode CLI "
                            "登录 start plan（凭据存于 ~/.zcode/cli/config.json，"
                            "本 provider 会自动读取）"
                        ),
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
        session_id = str(uuid.uuid4())
        requested_model = str(body.get("model") or "")

        if protocol == "anthropic":
            source = original if isinstance(original, dict) and original else body
            upstream_body = {k: v for k, v in source.items() if not k.startswith("_")}
            if isinstance(original, dict) and original and body.get("model"):
                upstream_body["model"] = MODEL_NAME_CANONICAL.get(body["model"], body["model"])

            # 风控必需：ZCode 系统块永远在 system 最前，客户端 system 合并在后
            # （只在「没有 system」时补不够——自带 system 的请求会整条撞 3012）
            if _SYSTEM_BLOCKS:
                upstream_body["system"] = _merged_system(upstream_body.get("system"))

            # 注入 metadata.user_id（风控必需）
            upstream_body["metadata"] = upstream_body.get("metadata") or {}
            if isinstance(upstream_body["metadata"], dict):
                upstream_body["metadata"]["user_id"] = _build_metadata_user_id(session_id)

            url = f"{self._base}/v1/messages"
            headers = _auth_headers(self._api_key, {
                "Accept": "text/event-stream",
                "x-session-id": session_id,
                "x-request-id": str(uuid.uuid4()),
                "x-query-id": str(uuid.uuid4()),
                "x-zcode-trace-id": str(uuid.uuid4()),
                "x-title": "Z Code@cli",
                "x-zcode-app-version": "3.11.2",
                "x-zcode-agent": "glm",
                "x-zcode-session-type": "main",
                "x-release-channel": "production",
                "x-os-category": "macos",
                "x-platform": "darwin-arm64",
                "x-client-language": "en-US",
                "x-client-timezone": "Asia/Shanghai",
                "accept": "*/*",
                "accept-language": "*",
                "accept-encoding": "gzip, deflate",
                "sec-fetch-mode": "cors",
            })
            # x-device-mid 单独加（_auth_headers 不包它）
            device_mid = _load_device_mid()
            if device_mid:
                headers["x-device-mid"] = device_mid
        else:
            # openai / responses：转成 anthropic 格式再发
            # Start Plan 只有 anthropic 端点，没有 openai 兼容路径
            upstream_body = _openai_to_anthropic_request(body)
            # 注入 metadata.user_id（风控必需）
            upstream_body["metadata"] = upstream_body.get("metadata") or {}
            if isinstance(upstream_body["metadata"], dict):
                upstream_body["metadata"]["user_id"] = _build_metadata_user_id(session_id)
            url = f"{self._base}/v1/messages"
            headers = _auth_headers(self._api_key, {
                "Accept": "text/event-stream" if stream else "application/json",
                "x-session-id": session_id,
                "x-request-id": str(uuid.uuid4()),
                "x-query-id": str(uuid.uuid4()),
                "x-zcode-trace-id": str(uuid.uuid4()),
                "x-title": "Z Code@cli",
                "x-zcode-app-version": "3.11.2",
                "x-zcode-agent": "glm",
                "x-zcode-session-type": "main",
                "x-release-channel": "production",
                "x-os-category": "macos",
                "x-platform": "darwin-arm64",
                "x-client-language": "en-US",
                "x-client-timezone": "Asia/Shanghai",
                "accept": "*/*",
                "accept-language": "*",
                "accept-encoding": "gzip, deflate",
                "sec-fetch-mode": "cors",
            })
            device_mid = _load_device_mid()
            if device_mid:
                headers["x-device-mid"] = device_mid

        client = await self._get_client()

        async def _send():
            req = client.build_request("POST", url, json=upstream_body, headers=headers)
            return await client.send(req, stream=stream)

        try:
            try:
                resp = await _send()
            except httpx.RemoteProtocolError as exc:
                log.warning("zcode-start upstream disconnected before response, retrying once: %s", exc)
                try:
                    resp = await _send()
                except httpx.HTTPError as exc2:
                    raise HTTPException(status_code=502, detail={
                        "error": {"message": "zcode-start upstream error", "type": "bad_gateway"}}
                    ) from exc2
        except httpx.TimeoutException as exc:
            log.warning("zcode-start upstream timeout: %s", exc)
            raise HTTPException(status_code=504, detail={
                "error": {"message": "zcode-start upstream timeout", "type": "timeout"}}
            ) from exc
        except httpx.HTTPError as exc:
            log.warning("zcode-start upstream error: %s", describe_exception(exc))
            raise HTTPException(status_code=502, detail={
                "error": {"message": "zcode-start upstream error", "type": "bad_gateway"}}
            ) from exc

        if resp.status_code >= 400:
            if stream:
                await resp.aread()
            try:
                return _upstream_error_response(resp)
            finally:
                await resp.aclose()

        # anthropic 协议：直通（客户端就是 Anthropic 形态）。
        # openai 协议：上游只有 anthropic 出参，需转回 chat.completion 形态。
        if protocol == "anthropic":
            if stream:
                return await _checked_stream(resp)
            try:
                payload = resp.json()
                if not _is_message(payload):
                    return _invalid_success(payload)
                return JSONResponse(content=payload)
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=502, detail={
                    "error": {"message": "zcode-start upstream returned non-JSON", "type": "bad_gateway"}}
                ) from exc
            finally:
                await resp.aclose()

        if stream:
            # OpenAI 输出转换也必须先在提交 200 前验上游首帧；复用闸门，
            # 已读取的前缀从迭代器返回，再交给转换器解析。
            checked = await _checked_stream(resp)
            if isinstance(checked, JSONResponse):
                return checked
            class _CheckedResponse:
                async def aiter_bytes(self):
                    async for chunk in checked.body_iterator:
                        yield chunk
                async def aclose(self):
                    await resp.aclose()
            return StreamingResponse(
                _anthropic_sse_to_openai_stream(_CheckedResponse(), requested_model),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "close"},
            )
        try:
            payload = resp.json()
            if not _is_message(payload):
                return _invalid_success(payload)
            return JSONResponse(content=_anthropic_message_to_chat(payload, requested_model))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=502, detail={
                "error": {"message": "zcode-start upstream returned non-JSON", "type": "bad_gateway"}}
            ) from exc
        finally:
            await resp.aclose()

    # ---- 内部 ----

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client

    def health(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "configured": bool(self._api_key),
            "base_url": self._base,
            "models": list(DEFAULT_MODELS),
        }

    # ---- 额度查询（/ui 管理页消费；同步 httpx，调用方经 asyncio.to_thread 包装） ----

    def quota(self) -> dict[str, Any] | None:
        """查询 Start Plan 用量（billing/current）。

        返回格式：
        {
          "plans": [{
            "entitlements": [{
              "grant_units": 100000000,
              "one_time": True,
              "ends_at": "2026-10-05T16:00:00Z",
              "priority": 110,
              ...
            }]
          }]
        }

        注意：额度用完时 plans 为空数组 []，这是正常状态（今日额度耗尽，
        等待每日重置）。
        """
        key = self._api_key or resolve_credentials()[0]
        if not key:
            raise RuntimeError("zcode-start 未配置 API key，无法查询额度")
        origin = self._base.split("/api/")[0]
        url = f"{origin}/api/v1/zcode-plan/billing/current"
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            })
        if resp.status_code != 200:
            raise RuntimeError(f"quota HTTP {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        data = payload.get("data") or {}
        plans = data.get("plans") or []
        items = []
        for plan in plans:
            ents = plan.get("entitlements") or []
            for ent in ents:
                grant = ent.get("grant_units") or 0
                ends = ent.get("ends_at")
                priority = ent.get("priority")
                one_time = ent.get("one_time", True)
                # 归一化为管理页条目格式
                items.append({
                    "label": f"Start Plan ({ent.get('model', 'GLM-5.3-Flash')})",
                    "used": 0,  # upstream doesn't report usage per entitlement
                    "total": grant,
                    "remaining": grant,  # one_time = daily refresh, remaining = total until ends_at
                    "percent": 0 if grant > 0 else 100,
                    "reset_ts": None,
                    "expire_ts": None,  # parse ends_at if needed
                    "unit": "token",
                    "priority": priority,
                    "one_time": one_time,
                })
        # 如果 plans 为空（额度耗尽），显示一个空状态
        if not items:
            items.append({
                "label": "Start Plan (GLM-5.3-Flash)",
                "used": 0,
                "total": 0,
                "remaining": 0,
                "percent": 100,
                "reset_ts": None,
                "expire_ts": None,
                "unit": "token",
                "note": "今日额度已用完，等待每日重置",
            })
        return {"items": items, "level": "start_plan"}


# ---------------------------------------------------------------------------
# 冒烟自测
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    key, base = resolve_credentials()
    if not key:
        print("no api key found (env ZCODE_START_API_KEY / secrets / ~/.zcode/cli/config.json)")
        raise SystemExit(1)
    print(f"key: {key[:6]}***{key[-4:]}  base: {base}")
    with httpx.Client(timeout=60) as client:
        r = client.post(
            f"{base}/v1/messages",
            headers={
                "authorization": f"Bearer {key}",
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "glm-5.3-flash",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "只回复两个字：pong"}],
                "metadata": {"user_id": '{"device_id":"test","account_uuid":"","session_id":"test"}'},
            },
        )
        print(f"status: {r.status_code}")
        print(r.text[:600])
