"""统一错误处理工具。

提供 HTTP 异常分类、错误信息提取、降级策略等通用功能。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import HTTPException

log = logging.getLogger(__name__)


class ErrorCategory:
    """错误分类常量。"""
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    QUOTA_EXHAUSTED = "quota_exhausted"
    UPSTREAM_ERROR = "upstream_error"
    NETWORK_ERROR = "network_error"
    CLIENT_ERROR = "client_error"


def categorize_http_exception(exc: HTTPException) -> str:
    """将 HTTPException 分类为预定义类型。

    Args:
        exc: HTTP 异常实例

    Returns:
        错误分类字符串
    """
    status_code = exc.status_code
    
    if status_code in (401, 403):
        return ErrorCategory.AUTH
    if status_code == 429:
        return ErrorCategory.RATE_LIMIT
    if status_code >= 500:
        return ErrorCategory.UPSTREAM_ERROR
    if status_code >= 400:
        return ErrorCategory.CLIENT_ERROR
    
    return ErrorCategory.UPSTREAM_ERROR


def is_account_error(exc: HTTPException) -> bool:
    """判断是否为账号相关错误（需要切换账号）。

    Args:
        exc: HTTP 异常实例

    Returns:
        True=需要切换账号，False=不需要
    """
    category = categorize_http_exception(exc)
    return category in (ErrorCategory.AUTH, ErrorCategory.QUOTA_EXHAUSTED, ErrorCategory.RATE_LIMIT)


def extract_error_message(exc: HTTPException, default: str = "Unknown error") -> str:
    """从 HTTPException 中提取用户友好的错误消息。

    Args:
        exc: HTTP 异常实例
        default: 默认消息

    Returns:
        格式化后的错误消息
    """
    # 优先使用 detail 字段
    if exc.detail:
        detail_str = str(exc.detail)
        # 如果是 JSON 字符串，尝试解析
        if detail_str.startswith("{"):
            try:
                import json
                data = json.loads(detail_str)
                return data.get("message", data.get("msg", default))
            except Exception:
                pass
        return detail_str[:200]  # 截断过长的消息
    
    return default


def create_user_friendly_error(
    exc: HTTPException,
    fallback: str = "请求失败，请稍后重试",
) -> HTTPException:
    """创建用户友好的 HTTP 异常。

    Args:
        exc: 原始异常
        fallback: 回退消息

    Returns:
        新的 HTTPException 实例
    """
    message = extract_error_message(exc, fallback)
    
    # 保持原状态码
    return HTTPException(
        status_code=exc.status_code,
        detail=message,
    )


async def safe_call(
    coro: Any,
    fallback: Any = None,
    catch_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Any:
    """安全调用协程，捕获异常并返回 fallback。

    Args:
        coro: 协程对象
        fallback: 异常时的返回值
        catch_exceptions: 要捕获的异常类型元组

    Returns:
        协程结果或 fallback
    """
    try:
        return await coro
    except catch_exceptions as exc:
        log.debug("safe_call 失败：%s", type(exc).__name__)
        return fallback
