"""RunningHub OpenAI 兼容 Provider（生图 / 生视频）。"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, AsyncIterator

from fastapi.responses import JSONResponse, StreamingResponse

from buddy_proxy.runninghub.client import RunningHubError, query_task, submit_task
from buddy_proxy.runninghub.store import get_active_accounts

_TERMINAL_STATES = {"SUCCESS", "FAILED", "CANCEL", "CANCELLED"}
_VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".avi")

_MODEL_ALIASES = {
    "text-to-image": "rhart-image-n-pro/text-to-image",
    "text-to-video": "ai-app/2086711729156894722",
    "seedance-2.5": "ai-app/2086711729156894722",
}

_BUILTIN_MODELS = [
    {"id": "runninghub/text-to-image", "owned_by": "runninghub"},
    {"id": "runninghub/text-to-video", "owned_by": "runninghub"},
    {"id": "runninghub/seedance-2.5", "owned_by": "runninghub"},
]


def _extract_prompt_and_images(messages: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """从 OpenAI messages 提取最后一条 user 消息的 prompt 和参考图 URL。"""
    prompt_parts: list[str] = []
    images: list[str] = []
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                prompt_parts.append(content.strip())
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text" and isinstance(part.get("text"), str):
                    if part["text"].strip():
                        prompt_parts.append(part["text"].strip())
                elif ptype == "image_url":
                    img = part.get("image_url")
                    url = img.get("url") if isinstance(img, dict) else img
                    if isinstance(url, str) and url.strip():
                        images.append(url.strip())
        if prompt_parts or images:
            break
    return "\n".join(prompt_parts), images


def _resolve_endpoint(model: str) -> str:
    raw = (model or "").strip()
    if raw.startswith("runninghub/"):
        raw = raw[len("runninghub/"):]
    if not raw or raw == "default":
        return _MODEL_ALIASES["text-to-image"]
    return _MODEL_ALIASES.get(raw, raw)


def _extract_output_urls(task_data: dict[str, Any]) -> list[tuple[str, str]]:
    """从 query_task 返回中提取 [(url, media_type), ...]。"""
    items: list[Any] = []
    for key in ("results", "outputs", "data"):
        val = task_data.get(key)
        if isinstance(val, list):
            items.extend(val)
    if not items and isinstance(task_data.get("url"), str):
        items.append({"url": task_data["url"], "outputType": task_data.get("outputType", "")})

    urls: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("fileUrl")
        if not isinstance(url, str) or not url.strip():
            continue
        ftype = str(item.get("outputType") or item.get("fileType") or "").lower()
        is_video = ftype in ("video", "mp4", "webm", "mov") or url.lower().split("?")[0].endswith(_VIDEO_EXTS)
        urls.append((url.strip(), "video" if is_video else "image"))
    return urls


def _format_outputs_markdown(outputs: list[tuple[str, str]]) -> str:
    if not outputs:
        return "任务已完成，但未返回媒体文件。"
    lines: list[str] = []
    for url, kind in outputs:
        if kind == "video":
            lines.append(f"[生成视频]({url})")
        else:
            lines.append(f"![generated]({url})")
    return "\n\n".join(lines)


def _sse_chunk(chunk_id: str, model: str, delta: dict[str, Any], finish_reason: str | None = None) -> str:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class RunningHubProvider:
    """RunningHub 生图/生视频 Provider。"""

    id = "runninghub"
    name = "RunningHub"

    def models(self) -> list[dict[str, Any]]:
        return list(_BUILTIN_MODELS)

    def health(self) -> dict[str, Any]:
        accounts = get_active_accounts()
        return {
            "ok": bool(accounts),
            "accounts": len(accounts),
            "detail": f"{len(accounts)} 个账号" if accounts else "未配置账号",
        }

    def quota(self) -> dict[str, Any]:
        """**不声称**支持额度查询（``supported: False``）。

        RunningHub 不是「模型」通道：它跑的是工作流/任务，余额是钱包（$）和
        站内过渡币，账号卡上还带每日积分、任务队列、工作流配置——这些都在它
        自己的「🎨 RunningHub」页里（``benefits_runninghub.js``）。

        以前这里返回不带 ``supported`` 的 dict，而 ``benefits.py`` 的
        ``entry["quota"] = {"supported": True, **quota}`` 是「provider 实现了
        quota() 就当支持」——于是额度页末尾凭空多出一张「RunningHub / 无额度
        数据」的空卡，什么信息也没有（用户 2026-11-29 报障：它不是模型，不该
        出现在额度页）。

        显式给 ``supported: False``：``**quota`` 会覆盖掉外层那个 True。
        保留 ``accounts`` 是因为 ``auth/login.py`` 登录流程要用它读余额。
        """
        accounts = get_active_accounts()
        return {
            "supported": False,
            "provider": self.id,
            "accounts": len(accounts),
            "items": [],
        }

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str = "openai",
        **_kwargs: Any,
    ) -> StreamingResponse | JSONResponse:
        if protocol != "openai":
            return JSONResponse(
                {"error": {"message": "RunningHub 仅支持 OpenAI /v1/chat/completions 协议", "type": "unsupported_protocol"}},
                status_code=501,
            )

        accounts = get_active_accounts()
        if not accounts:
            return JSONResponse(
                {"error": {"message": "未配置 RunningHub 账号，请在管理页添加 API Token", "type": "auth_error"}},
                status_code=401,
            )

        prompt, images = _extract_prompt_and_images(body.get("messages") or [])
        if not prompt and not images:
            return JSONResponse(
                {"error": {"message": "messages 中未找到有效的 prompt 或图片", "type": "invalid_request_error"}},
                status_code=400,
            )

        model = str(body.get("model") or "runninghub/text-to-image")
        endpoint = _resolve_endpoint(model)
        payload: dict[str, Any] = {}
        if prompt:
            payload["prompt"] = prompt
        if images:
            payload["imageUrl"] = images[0] if len(images) == 1 else images

        # 透传常见生图/生视频参数
        for k in ("aspectRatio", "resolution", "duration", "negativePrompt", "seed", "nodeInfoList"):
            if k in body:
                payload[k] = body[k]

        api_key = accounts[0]["api_key"]
        try:
            created = await submit_task(api_key, endpoint, payload)
        except RunningHubError as exc:
            return JSONResponse(
                {"error": {"message": str(exc), "type": "upstream_error"}},
                status_code=exc.status_code,
            )

        task_id = created["taskId"]
        stream = bool(body.get("stream", False))
        poll_interval = float(os.environ.get("RUNNINGHUB_POLL_INTERVAL", "3.0"))
        max_wait = float(os.environ.get("RUNNINGHUB_MAX_WAIT", "600.0"))

        if stream:
            return StreamingResponse(
                self._stream_task(api_key, task_id, model, poll_interval, max_wait),
                media_type="text/event-stream",
            )

        return await self._wait_non_stream(api_key, task_id, model, poll_interval, max_wait)

    async def _poll_until_done(
        self,
        api_key: str,
        task_id: str,
        poll_interval: float,
        max_wait: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max_wait
        while True:
            res = await query_task(api_key, task_id)
            status = str(res.get("status") or res.get("taskStatus") or "").upper()
            if status in _TERMINAL_STATES:
                return res
            if time.monotonic() >= deadline:
                raise RunningHubError(f"RunningHub 任务超时 (taskId={task_id})", status_code=504)
            await asyncio.sleep(poll_interval)

    async def _wait_non_stream(
        self,
        api_key: str,
        task_id: str,
        model: str,
        poll_interval: float,
        max_wait: float,
    ) -> JSONResponse:
        try:
            res = await self._poll_until_done(api_key, task_id, poll_interval, max_wait)
        except RunningHubError as exc:
            return JSONResponse(
                {"error": {"message": str(exc), "type": "upstream_error"}},
                status_code=exc.status_code,
            )

        status = str(res.get("status") or res.get("taskStatus") or "").upper()
        if status != "SUCCESS":
            reason = res.get("errorMessage") or res.get("failedReason") or status
            return JSONResponse(
                {"error": {"message": f"RunningHub 任务失败: {reason}", "type": "upstream_error"}},
                status_code=502,
            )

        content = _format_outputs_markdown(_extract_output_urls(res))
        return JSONResponse(
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    async def _stream_task(
        self,
        api_key: str,
        task_id: str,
        model: str,
        poll_interval: float,
        max_wait: float,
    ) -> AsyncIterator[str]:
        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        yield _sse_chunk(chunk_id, model, {"role": "assistant", "content": ""})

        deadline = time.monotonic() + max_wait
        try:
            while True:
                res = await query_task(api_key, task_id)
                status = str(res.get("status") or res.get("taskStatus") or "").upper()
                if status == "SUCCESS":
                    content = _format_outputs_markdown(_extract_output_urls(res))
                    yield _sse_chunk(chunk_id, model, {"content": content})
                    yield _sse_chunk(chunk_id, model, {}, finish_reason="stop")
                    yield "data: [DONE]\n\n"
                    return
                if status in _TERMINAL_STATES:
                    reason = res.get("errorMessage") or res.get("failedReason") or status
                    yield _sse_chunk(chunk_id, model, {"content": f"\n[任务失败: {reason}]"})
                    yield _sse_chunk(chunk_id, model, {}, finish_reason="stop")
                    yield "data: [DONE]\n\n"
                    return
                if time.monotonic() >= deadline:
                    yield _sse_chunk(chunk_id, model, {"content": f"\n[任务超时: taskId={task_id}]"})
                    yield _sse_chunk(chunk_id, model, {}, finish_reason="stop")
                    yield "data: [DONE]\n\n"
                    return
                await asyncio.sleep(poll_interval)
        except RunningHubError as exc:
            yield _sse_chunk(chunk_id, model, {"content": f"\n[查询异常: {exc}]"})
            yield _sse_chunk(chunk_id, model, {}, finish_reason="stop")
            yield "data: [DONE]\n\n"
