"""统一 HTTP 客户端配置与管理。

提供所有 Provider 共用的 httpx.AsyncClient 配置、连接池管理、超时控制等。
避免每个 Provider 重复配置相同的参数。
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)


class HttpClientConfig:
    """HTTP 客户端全局配置。"""
    
    # 默认超时设置（秒）
    DEFAULT_TIMEOUT: float = 60.0
    
    # 连接池配置
    DEFAULT_POOL_LIMIT: int = 100
    DEFAULT_POOL_MAX_SIZE: int = 100
    DEFAULT_KEEPALIVE_KEEPALIVE: int = 30
    
    # 重试配置
    DEFAULT_RETRIES: int = 3
    DEFAULT_BACKOFF_FACTOR: float = 0.5


def create_client(
    timeout: Optional[float] = None,
    pool_limit: Optional[int] = None,
    retries: Optional[int] = None,
    **kwargs,
) -> httpx.AsyncClient:
    """创建配置好的 AsyncClient。

    Args:
        timeout: 请求超时时间（秒），默认 HttpClientConfig.DEFAULT_TIMEOUT
        pool_limit: 连接池限制，默认 HttpClientConfig.DEFAULT_POOL_LIMIT
        retries: 重试次数，默认 HttpClientConfig.DEFAULT_RETRIES
        **kwargs: 额外传递给 httpx.AsyncClient 的参数

    Returns:
        配置好的 AsyncClient 实例
    """
    config = HttpClientConfig()
    
    return httpx.AsyncClient(
        timeout=timeout or config.DEFAULT_TIMEOUT,
        limits=httpx.Limits(
            max_connections=pool_limit or config.DEFAULT_POOL_LIMIT,
            max_keepalive_connections=config.DEFAULT_POOL_MAX_SIZE,
            keepalive_expiry=config.DEFAULT_KEEPALIVE_KEEPALIVE,
        ),
        retries=retries or config.DEFAULT_RETRIES,
        **kwargs,
    )


def create_retry_transport(
    max_retries: int = 3,
    backoff_factor: float = 0.5,
) -> httpx.AsyncHTTPTransport:
    """创建带重试的传输层。

    Args:
        max_retries: 最大重试次数
        backoff_factor: 退避因子

    Returns:
        配置好的 AsyncHTTPTransport 实例
    """
    retry_strategy = httpx.Retry(
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        allowed_methods=["GET", "POST", "PUT", "DELETE"],
        status_forcelist=[429, 500, 502, 503, 504],
    )
    return httpx.AsyncHTTPTransport(retry=retry_strategy)
