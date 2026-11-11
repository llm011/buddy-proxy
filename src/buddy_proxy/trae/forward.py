"""Trae 请求转发逻辑。

提供多账号 failover、协议转换、流式处理等通用转发功能。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi.responses import JSONResponse, StreamingResponse

from ..core.error_handler import is_account_error, create_user_friendly_error
from .credentials import set_current_work_account
from .text_protocol import _looks_like_agent_request, _extract_prompt
from .native_tools import _native_messages

log = logging.getLogger(__name__)


async def forward_with_failover(
    provider: Any,
    body: dict[str, Any],
    protocol: str,
    original: dict[str, Any] | None,
    requested_model: str,
    prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    native: dict[str, Any] | None,
    stream: bool,
    agent_mode: bool,
) -> StreamingResponse | JSONResponse:
    """带多账号 failover 的转发入口。

    Args:
        provider: TraeProvider 实例
        body: 原始请求体
        protocol: 客户端协议
        original: 原始请求（协议转换前）
        requested_model: 请求的模型
        prompt: 提取的 prompt
        messages: 消息列表
        tools: 工具定义
        native: 原生传输参数
        stream: 是否流式
        agent_mode: 是否为 agent 模式

    Returns:
        StreamingResponse 或 JSONResponse
    """
    # PAT 子类有独立账号体系，不走 work 多账号循环
    if provider._is_pat_variant():
        set_current_work_account(None)
        try:
            return await provider._forward_once(
                body, protocol, original, requested_model, prompt,
                messages, tools, native, stream, agent_mode,
            )
        finally:
            set_current_work_account(None)

    # 只在本区账号间 failover
    accounts = provider._work_accounts()
    if not accounts:
        raise ValueError("no available accounts")

    last_exc: Exception | None = None
    for idx, account in enumerate(accounts):
        try:
            set_current_work_account(account)
            return await provider._forward_once(
                body, protocol, original, requested_model, prompt,
                messages, tools, native, stream, agent_mode,
                account=account,
            )
        except Exception as exc:
            last_exc = exc
            if is_account_error(exc):
                log.info("trae account %s error, trying next: %s",
                        getattr(account, "id", idx), type(exc).__name__)
                continue
            # 非账号错误，直接抛出
            raise create_user_friendly_error(exc) from exc
        finally:
            if idx == len(accounts) - 1:
                set_current_work_account(None)

    # 所有账号都失败
    if last_exc:
        raise create_user_friendly_error(last_exc)
    raise RuntimeError("no accounts available")
