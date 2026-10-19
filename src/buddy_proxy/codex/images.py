"""Codex 生图：``gpt-image-*`` 走 ``/backend-api/codex/images/generations``。

上游行为（2026-10 实测）：

- ``POST {base}/images/generations``，body ``{model, prompt, store:false, stream:true}``
- 返回 SSE：先若干 ``keepalive``，最后 ``image_generation.completed``，
  图片在 ``b64_json``（约 1254×1254 PNG，2~4 MB base64）
- 首帧要等 **十几秒**（上游先发 keepalive 占住连接），所以这条路径不能
  套用对话的首事件闸门（它会把 keepalive 当「已提交」→ 换号失效）
- ``store`` 与 ``stream`` 两个字段和对话一样是硬约束

buddy 侧不做独立的 ``/v1/images`` 端点：客户端统一用 chat 协议点名
``codex/gpt-image-*``，provider 把 prompt 送去生图上游，再把图包成
「assistant 消息 + data URL 图片」——与 antigravity 生图模型
（图片在 ``message.images[]``，见 anthropic_adapter 的
``_chat_image_to_anthropic``）同一套出口约定。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .projection import aiter_sse_events
from .sse import iter_sse_lines
from .upstream import build_headers, normalize_base_url, new_session_id

#: 图片默认模型（请求里的 model 若只是 ``image`` 之类占位时用）。
DEFAULT_IMAGE_MODEL = "gpt-image-2.5"

#: 生图要等十几秒到几十秒，非流式调用是「一次 read 拿到全部」。
IMAGE_TIMEOUT = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=15.0)

#: OpenAI images API 里可以原样带给上游的字段（上游不认的字段直接丢）。
_PASS_FIELDS = ("size", "quality", "background", "output_format", "moderation")


def image_url(base_url: str) -> str:
    return f"{normalize_base_url(base_url)}/images/generations"


def build_image_body(model: str, prompt: str, **extra: Any) -> dict[str, Any]:
    """生图请求体：``store``/``stream`` 是硬约束，其余字段按白名单透传。"""
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "store": False,
        "stream": True,
    }
    for key in _PASS_FIELDS:
        value = extra.get(key)
        if value not in (None, ""):
            body[key] = value
    return body


#: 顶层 ``prompt`` 只在**不含 messages**（即 /v1/images 那种一次性素材请求）
#: 时才认；chat 体里 ``prompt`` 往往是 SDK 顺手透传的额外字段，拿它当提示词
#: 会盖掉用户真正说的那句话。
def extract_prompt(body: dict[str, Any]) -> str:
    """从 chat / responses 请求体里抽出要画的 prompt。

    - 不含对话消息时认顶层 ``prompt`` 字段（OpenAI images 语义）
    - 否则取最后一条 user 消息的文本（多模态 content 只拼 text 部分）
    """
    if not body.get("messages") and not body.get("input"):
        direct = body.get("prompt")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if not isinstance(msg, dict) or str(msg.get("role")) != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                if content.strip():
                    return content.strip()
            elif isinstance(content, list):
                parts = [
                    str(p.get("text") or "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") in ("text", "input_text")
                ]
                text = "".join(parts).strip()
                if text:
                    return text
    # /v1/responses 入口经投射后也是 chat messages，上面已覆盖；这里兜 input
    items = body.get("input")
    if isinstance(items, list):
        for item in reversed(items):
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, dict) and str(item.get("role")) == "user":
                content = item.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
    return ""


async def collect_image(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    body: dict[str, Any],
    access_token: str,
    account_id: str,
    timeout: httpx.Timeout | None = None,
) -> dict[str, Any]:
    """发一次生图请求并把 SSE 收成最终 payload（``image_generation.completed``）。

    返回 ``{"b64_json", "output_format", "size", "created_at", "usage"}``。
    非 200 抛 :class:`ImageUpstreamError`（带状态码，供 failover 决策）。
    """
    headers = build_headers(
        access_token, account_id=account_id,
        session_id=new_session_id(), stream=True)
    payload: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    try:
        async with client.stream(
            "POST", image_url(base_url), json=body, headers=headers,
            timeout=timeout or IMAGE_TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                raw = (await resp.aread())[:400]
                raise ImageUpstreamError(resp.status_code,
                                         raw.decode("utf-8", "replace"))
            async for name, event in aiter_sse_events(iter_sse_lines(resp)):
                kind = name or str(event.get("type") or "")
                if kind == "image_generation.completed":
                    payload = event
                    break
                if kind in ("error", "image_generation.failed"):
                    err = event.get("error")
                    error = err if isinstance(err, dict) else event
                    break
    except httpx.HTTPError as exc:
        raise ImageUpstreamError(502, f"上游网络错误: {exc}") from exc
    if payload is not None:
        return payload
    if error is not None:
        raise ImageUpstreamError(_error_status(error),
                                 str(error.get("message") or error))
    raise ImageUpstreamError(502, "上游在返回图片前断流")


class ImageUpstreamError(RuntimeError):
    """生图上游失败；``status`` 用于触发与对话一致的冷却/换号策略。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


#: 上游带内 error 里表示「这个账号的额度/用量用完了」的文案特征。实测空 prompt
#: 是 ``invalid_request_error``（与账号无关），用量上限则以 ``usage_limit_reached``
#: 一类码或 ``rate limit`` 文案出现——这类才是换号能救的。
_ACCOUNT_LIMIT_WORDS = (
    "usage_limit", "rate_limit", "rate limit", "quota", "too many requests",
    "insufficient", "credits", "limit reached", "resets_in",
)


def _error_status(err: dict[str, Any]) -> int:
    """带内 error → 供 failover 决策的 HTTP 状态码。

    顺序很重要：先认文案（用量/额度 → 429）再认 ``code``，因为上游把
    ``usage_limit_reached`` 塞在 ``error.code`` 而 ``error.type`` 仍是
    ``invalid_request_error``——只看 type 会把账号额度误判成请求错误，
    永远不换号。
    """
    raw = err.get("code") or err.get("status")
    if isinstance(raw, int) and 400 <= raw <= 599:
        return raw
    text = " ".join(str(err.get(k) or "") for k in ("code", "type", "message")).lower()
    if any(k in text for k in _ACCOUNT_LIMIT_WORDS):
        return 429
    if "unauthorized" in text or "authentication" in text:
        return 401
    if "forbidden" in text or "permission" in text:
        return 403
    # 参数/请求错误（空 prompt、不支持的尺寸等）：请求本身有问题，换号也没用
    if "invalid" in text or "unsupported" in text:
        return 400
    return 502


# ---------------------------------------------------------------------------
# 出口：图 → chat / responses / anthropic
# ---------------------------------------------------------------------------

def to_data_url(payload: dict[str, Any]) -> str:
    fmt = str(payload.get("output_format") or "png").lstrip(".")
    b64 = str(payload.get("b64_json") or "")
    if not b64:
        raise ImageUpstreamError(502, "上游返回的图片缺少 b64_json")
    return f"data:image/{fmt};base64,{b64}"


def to_chat_completion(
    payload: dict[str, Any], *, model: str, prompt: str = "",
) -> dict[str, Any]:
    """生图结果 → chat.completion。

    正文放一句人话（客户端至少能显示「图已生成」），图片按 antigravity
    同款约定挂在 ``message.images[]``；usage 用上游给的值兜底成 0。
    """
    url = to_data_url(payload)
    created = int(payload.get("created_at") or time.time())
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "图片已生成。",
        "images": [{"type": "image_url", "image_url": {"url": url}}],
    }
    if prompt:
        message["revised_prompt"] = prompt
    return {
        "id": f"img-{payload.get('generation_id') or created}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": int(usage.get("input_tokens") or 0),
            "completion_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
    }


def chat_images_to_responses_images(message: dict[str, Any]) -> list[dict[str, Any]]:
    """chat 里的 ``images[]`` → Responses ``message.content[]`` 的 output_image。

    仓库的 responses 出口（``protocols/responses_adapter``）原本只认文本；
    Codex 生图需要把图带回去，这里给出可复用的小转换。
    """
    out: list[dict[str, Any]] = []
    for item in message.get("images") or []:
        if not isinstance(item, dict):
            continue
        url = (item.get("image_url") or {}).get("url") if isinstance(
            item.get("image_url"), dict) else item.get("url")
        if url:
            out.append({"type": "output_image", "image_url": url})
    return out


def sse_chunk(payload: dict[str, Any]) -> str:
    """``data: {...}\\n\\n``（生图流式出口只有一两帧）。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
