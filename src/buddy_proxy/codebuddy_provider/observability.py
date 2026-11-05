"""可观测性：指标埋点、客户端来源标签与请求/响应日志。

- ``_instrument`` / ``_metrics_stream``：provider.forward 的统一指标埋点
  （/ui 图表数据源；metrics 未初始化时直通，不改变转发行为）
- ``CLIENT_TAG`` / ``resolve_client_tag``：客户端来源识别（routes 层设置）
- ``body_summary`` / ``log_client_request`` / ``log_upstream_request`` /
  ``log_upstream_response``：verbose 分级的请求/响应日志

不依赖本包其它子模块（依赖方向：observability <- pipeline <- provider <- forward）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
import time
from contextvars import ContextVar
from typing import Any, Optional

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from buddy_proxy.core.credit_estimate import estimate_credit, remember_prompt
from buddy_proxy.core.metrics import ACCOUNT_META, SSEErrorExtractor, SSEUsageExtractor, normalize_usage
from buddy_proxy.core.state import (
    diagnostic,
    get_state,
    is_policy_blocked,
)

log = logging.getLogger(__name__)

def _elapsed_ms(started: float) -> int:
    return round((time.time() - started) * 1000)


def _exc_text(detail: Any) -> str:
    """HTTPException.detail → 短文本（dict 形式的结构化错误取 message）。"""
    if isinstance(detail, dict):
        detail = (detail.get("error") or {}).get("message") or detail
    return str(detail)[:300]


def _credit_or_estimate(provider_id: str, model_id: str, norm: dict[str, Any],
                        account: str = "") -> tuple[Optional[float], bool]:
    """上游 usage 带实扣积分就用实扣；否则按 token 估算
    （trae 按实测单价表算，zcode 按 GLM Coding Plan 官方抵扣系数精算）。

    ``account`` 参与 Trae 的缓存命中估算：上游不上报缓存数时，用「同账号同模型
    上一条 prompt」作会话基线算增量（见 ``credit_estimate._LAST_PROMPT``）。
    没有账号信息时退回全价，不猜。

    返回 (credit, credit_estimated)；估算失败/无 token 数据时为 (None, False)。
    """
    if norm.get("credit") is not None:
        return norm["credit"], False
    est = estimate_credit(provider_id, model_id,
                          norm.get("prompt_tokens"), norm.get("completion_tokens"),
                          norm.get("cached_tokens"), account=account)
    return (est, True) if est is not None else (None, False)


def _remember_baseline(provider_id: str, model_id: str, norm: dict[str, Any],
                       account: str) -> None:
    """把本次 prompt 记作该 (账号, 模型) 的会话基线，供下一条算缓存增量。

    只在 Trae 生效（其余通道不需要，白占记忆表）；没有账号或 token 就不记。
    放在 ``_credit_or_estimate`` **之后**调用：估算必须先读到「上一条」，再覆盖。
    """
    if provider_id != "trae" or not account:
        return
    prompt = int(norm.get("prompt_tokens") or 0)
    if prompt <= 0:
        return
    remember_prompt(account, model_id, prompt)


# 客户端来源标签（由 routes 层在每个请求开始时设置，_instrument 落 metrics 时读取）。
# 用 ContextVar 而非函数参数穿透：避免改动所有 provider.forward 签名。
CLIENT_TAG: ContextVar[str] = ContextVar("client_tag", default="")


def _client_names_path() -> pathlib.Path:
    configured = os.environ.get("BUDDY_CLIENT_NAMES_FILE", "")
    if configured:
        return pathlib.Path(configured)
    from buddy_proxy.core.paths import state_file
    return state_file("buddy_client_names.json", legacy="buddy_client_names.json")


_CLIENT_NAMES_FILE = _client_names_path()
_client_names_cache: tuple[float, dict[str, dict[str, str]]] | None = None


def _client_names() -> dict[str, dict[str, str]]:
    """客户端名称映射表（本地文件，热加载，无文件/解析失败时空表）。

    格式见 .token.md：``{"by_key": {"<key>": "名称"}, "by_ua": {"<ua片段>": "名称"}}``。
    """
    global _client_names_cache
    try:
        p = pathlib.Path(_CLIENT_NAMES_FILE)
        if not p.exists():
            return {"by_key": {}, "by_ua": {}}
        mtime = p.stat().st_mtime
        if _client_names_cache and _client_names_cache[0] == mtime:
            return _client_names_cache[1]
        raw = json.loads(p.read_text("utf-8"))
        mapping = {
            "by_key": {str(k): str(v) for k, v in (raw.get("by_key") or {}).items()},
            "by_ua": {str(k).lower(): str(v) for k, v in (raw.get("by_ua") or {}).items()},
        }
        _client_names_cache = (mtime, mapping)
        return mapping
    except Exception as e:
        log.warning("客户端名称映射表加载失败: %s", e)
        return {"by_key": {}, "by_ua": {}}


def resolve_client_tag(user_agent: str, client_name: str, api_key: str = "") -> str:
    """合成用于展示/落库的客户端短标签，优先级：

    1. 客户端自声明 ``X-Client-Name``（最可靠）
    2. 本地映射表按 API key 精确匹配（``~/.buddy-proxy/buddy_client_names.json``）
    3. 本地映射表按 UA 片段匹配
    4. UA 推断兜底：claude-cli → claude-code，其余取可辨识片段
    """
    if client_name:
        return re.sub(r"[^\w./@-]", "", client_name)[:40]
    names = _client_names()
    if api_key and api_key in names["by_key"]:
        return names["by_key"][api_key][:40]
    ua = (user_agent or "").strip()
    ua_lower = ua.lower()
    for frag, name in names["by_ua"].items():
        if frag and frag in ua_lower:
            return name[:40]
    if ua.startswith("claude-cli"):
        return "claude-code"
    for prefix in ("python-httpx", "OpenAI/Python", "OpenAI/JS", "node-fetch", "undici", "curl"):
        if ua.startswith(prefix):
            return prefix.lower().replace("/", "-")
    return ua.split(" ")[0][:40]


async def _instrument(
    state: Any,
    coro,
    *,
    provider_id: str,
    model_id: str,
    protocol: str,
    stream: bool,
):
    """统一指标埋点：包装 provider.forward 的结果/异常写入 MetricsCollector。

    - 非流式：响应返回时立即记一条（顺带从 JSON body 提取 usage token 数）；
    - 流式：包装 body_iterator，流结束（或中途断开）时补记，附 chunk 数。
    metrics 未初始化（如离线冒烟测试）时直通，不改变任何转发行为。
    """
    metrics = getattr(state, "metrics", None)
    if metrics is None:
        return await coro
    client = CLIENT_TAG.get()
    # 请求级账号 holder：traepat 在 SSE 读线程/to_thread worker 里选定账号后
    # 写入（见 metrics.ACCOUNT_META 注释），流式落库发生在流结束，经此 dict 传回。
    # 不做 reset：流式 body 在本函数返回后才迭代，_stream 届时才复制上下文派生
    # 读线程；请求任务结束后上下文随之丢弃，不会泄漏到其它请求。
    account_meta: dict[str, str] = {}
    ACCOUNT_META.set(account_meta)
    started = time.time()
    try:
        resp = await coro
    except HTTPException as exc:
        metrics.record(provider=provider_id, model=model_id, protocol=protocol,
                       status=exc.status_code, duration_ms=_elapsed_ms(started),
                       error=_exc_text(exc.detail), client=client,
                       account=account_meta.get("account", ""))
        raise
    except Exception as exc:
        metrics.record(provider=provider_id, model=model_id, protocol=protocol,
                       status=500, duration_ms=_elapsed_ms(started), error=str(exc),
                       client=client, account=account_meta.get("account", ""))
        raise

    if isinstance(resp, StreamingResponse):
        resp.body_iterator = _metrics_stream(
            resp.body_iterator, metrics,
            provider_id=provider_id, model_id=model_id, protocol=protocol,
            started=started, client=client, account_meta=account_meta,
        )
        return resp

    usage: dict[str, Any] = {}
    error = ""
    try:
        payload = json.loads(resp.body)
        usage = payload.get("usage") or {}
        if resp.status_code >= 400:
            error = str((payload.get("error") or {}).get("message") or f"HTTP {resp.status_code}")
    except Exception:
        pass
    norm = normalize_usage(usage) if usage else {
        "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "credit": None}
    credit, credit_estimated = _credit_or_estimate(
        provider_id, model_id, norm, account=account_meta.get("account", ""))
    _remember_baseline(provider_id, model_id, norm, account_meta.get("account", ""))
    metrics.record(
        provider=provider_id, model=model_id, protocol=protocol,
        status=resp.status_code, duration_ms=_elapsed_ms(started),
        prompt_tokens=norm["prompt_tokens"],
        completion_tokens=norm["completion_tokens"],
        cached_tokens=norm["cached_tokens"],
        credit=credit,
        credit_estimated=credit_estimated,
        error=error,
        client=client,
        account=account_meta.get("account", ""),
    )
    return resp


async def _metrics_stream(inner, metrics, *, provider_id: str, model_id: str,
                          protocol: str, started: float, client: str = "",
                          account_meta: dict[str, str] | None = None):
    """流式响应的计数包装：透传所有 chunk，结束时补记（含 TTFT 与流式 usage）。

    带内错误（provider 用正常 SSE 事件报错、生成器正常结束）也记成失败——
    否则就是假绿灯：客户端一个字节没收到，指标却是 200。
    """
    chunk_count = 0
    error = ""
    first_ts: float | None = None
    extractor = SSEUsageExtractor()
    err_extractor = SSEErrorExtractor()
    try:
        async for chunk in inner:
            if first_ts is None:
                first_ts = time.time()
            chunk_count += 1
            extractor.feed(chunk)
            err_extractor.feed(chunk)
            yield chunk
    except Exception as exc:
        error = f"stream error: {exc}"
        raise
    finally:
        u = extractor.usage
        if not error and err_extractor.message:
            error = f"in-band error: {err_extractor.message}"
        credit, credit_estimated = _credit_or_estimate(
            provider_id, model_id, u, account=(account_meta or {}).get("account", ""))
        _remember_baseline(provider_id, model_id, u, (account_meta or {}).get("account", ""))
        metrics.record(provider=provider_id, model=model_id, protocol=protocol,
                       status=500 if error else 200, duration_ms=_elapsed_ms(started),
                       stream=True, chunk_count=chunk_count, error=error,
                       ttft_ms=round((first_ts - started) * 1000) if first_ts else None,
                       prompt_tokens=u.get("prompt_tokens", 0),
                       completion_tokens=u.get("completion_tokens", 0),
                       cached_tokens=u.get("cached_tokens", 0),
                       credit=credit,
                       credit_estimated=credit_estimated,
                       client=client,
                       account=(account_meta or {}).get("account", ""))


# ============================================================================
# 日志辅助函数（body_summary 等，供 routes 与转发逻辑共用）
# ============================================================================

def body_summary(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages") or []
    message_summary = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        content = item.get("content", "")
        if isinstance(content, str):
            content_length = len(content)
            content_type = "text"
        elif isinstance(content, list):
            content_length = sum(
                len(str(part.get("text", ""))) for part in content if isinstance(part, dict)
            )
            content_type = "parts"
        else:
            content_length = 0
            content_type = type(content).__name__
        message_summary.append({
            "role": item.get("role"),
            "content_type": content_type,
            "content_length": content_length,
        })
    return {
        "model": body.get("model"),
        "stream": bool(body.get("stream")),
        "message_count": len(messages),
        "messages": message_summary,
        "tool_count": len(body.get("tools") or []),
    }


def log_client_request(method: str, path: str, body: dict[str, Any] | None, **client_info) -> None:
    """记录客户端请求的安全摘要，不持久化请求原文。

    ``verbose_llm`` 保持兼容，但只表示输出更多运行诊断，不能突破日志
    红线：token、UID、body 和工具参数均不写入日志。
    """
    state = get_state()
    if body:
        summary = body_summary(body)
        state.write_log("client_request_summary", method=method, path=path,
                        **client_info, **summary)
    else:
        state.write_log("client_request_summary", method=method, path=path, **client_info)


def log_upstream_request(protocol: str, body: dict[str, Any]) -> None:
    """记录上游请求安全摘要，不持久化请求原文。"""
    state = get_state()
    messages = body.get("messages", [])
    total_chars = sum(
        len(str(m.get("content", "")))
        for m in messages
        if isinstance(m, dict)
    )
    summary = {
        "model": body.get("model"),
        "message_count": len(messages),
        "tool_count": len(body.get("tools", [])),
        "stream": bool(body.get("stream")),
        "total_chars": total_chars,
    }
    state.write_log("upstream_request_summary", protocol=protocol, **summary)
    diagnostic("upstream_request_summary", protocol=protocol, **summary)


def log_upstream_response(protocol: str, text: str, **stats) -> None:
    """记录上游响应的长度、短哈希和状态，不记录正文。"""
    state = get_state()
    common = {
        "protocol": protocol,
        "content_length": len(text),
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "safety_message_detected": is_policy_blocked(text),
        **stats,
    }
    diagnostic("response", **common)
    state.write_log("stream_completed" if stats.get("stream") else "response", **common)
