"""Trae 流式响应处理。

提供 SSE 流解析、事件收集、超时控制等通用功能。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, AsyncGenerator, Optional

log = logging.getLogger(__name__)


class GatedStream:
    """带门控的流式响应生成器。

    用于缓冲初始数据，直到满足某些条件才开始输出。
    """

    def __init__(self, buffered: list[str], gen: AsyncGenerator[str, None]):
        self.buffered = buffered
        self.gen = gen
        self._started = False

    async def __aiter__(self) -> AsyncGenerator[str, None]:
        # 先输出缓冲的数据
        for item in self.buffered:
            self._started = True
            yield item
        
        # 然后输出流式数据
        async for item in self.gen:
            self._started = True
            yield item


async def stream_with_timeout(
    generator: AsyncGenerator[dict[str, Any], None],
    timeout_s: float,
    idle_timeout_s: float = 180.0,
) -> AsyncGenerator[dict[str, Any], None]:
    """带超时控制的流式生成器包装器。

    Args:
        generator: 原始流式生成器
        timeout_s: 总超时时间（秒）
        idle_timeout_s: 空闲超时（秒），超过无数据则断开

    Yields:
        流式事件字典
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_activity = asyncio.get_event_loop().time()
    
    async for event in generator:
        # 检查总超时
        if asyncio.get_event_loop().time() > deadline:
            log.warning("stream total timeout reached")
            break
        
        # 检查空闲超时
        if asyncio.get_event_loop().time() - last_activity > idle_timeout_s:
            log.warning("stream idle timeout reached")
            break
        
        last_activity = asyncio.get_event_loop().time()
        yield event
    
    # 清理资源
    try:
        await generator.aclose()
    except Exception:
        pass


def collect_stream_events(
    generator: AsyncGenerator[dict[str, Any], None],
    max_events: int = 1000,
) -> list[dict[str, Any]]:
    """收集流式事件为列表。

    Args:
        generator: 原始流式生成器
        max_events: 最大事件数

    Returns:
        事件列表
    """
    events: list[dict[str, Any]] = []
    
    try:
        import asyncio
        
        async def _collect():
            async for event in generator:
                events.append(event)
                if len(events) >= max_events:
                    break
            
            if events:
                yield events
        
        # 同步调用异步协程
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return list(loop.run_until_complete(_collect()))
        finally:
            loop.close()
    except Exception as exc:
        log.debug("collect_stream_events error: %s", type(exc).__name__)
        return events


def create_stop_event() -> threading.Event:
    """创建流式停止事件。

    Returns:
        线程安全的 Event 对象
    """
    return threading.Event()
