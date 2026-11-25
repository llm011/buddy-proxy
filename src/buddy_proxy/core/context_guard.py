"""上下文窗口守卫：转发前按**目标通道的真实窗口**预检，超了就本地截断历史。

## 为什么需要

聚合模型对外（``/v1/models``）声明的 ``context_window`` 取的是 failover 链里
**最大**的那个通道（见 ``model_aggregate`` 对窗口不取最小值的设计——避免小通道
把大通道的长上下文能力压没）。但请求实际落到哪个通道是运行时决定的：zcode 的
``glm-5.4`` 是 1M，trae 的 ``glm-5.4`` 只有 116k。客户端（DSH 等）按声明的 1M
管理上下文、决定何时 compact，当 zcode 被 429 限流、failover 切到 trae 时，
几百 k 的上下文 >> 116k，上游直接报 ``4026 context length has exceeded``——
请求硬失败，compact 也救不回来。

2026-11-25 实测：17 次 4026 全是 ``glm-5.4`` 走 trae，失败时上游连 usage 都不回
（``prompt_tokens=0``）。trae 成功请求里最大才 4080 token。

## 做什么

在跨通道派发层（``codebuddy_provider.forward._dispatch_once``）派发**之前**：

1. 查这个 ``(provider, model)`` 的真实上下文窗口（``provider.models()`` 里的
   ``context_window`` / ``max_input``）；
2. 估算当前请求体的 token 量；
3. 若估算 > 窗口 × 安全系数（0.88），**截断消息历史**（保留 system + 最新一条，
   从最旧的中间消息整条丢），构造一个**新 body 副本**发出去——原 body 不动，
   下一个候选通道（更大窗口）仍能拿全量。

不引入新失败：拿不到窗口、估算失败、或 body 没有可截断的 messages，就**原样放
行**——最差也只是退回到今天的行为（上游可能 4026），不会因为 guard 本身把一个
本来能成的请求搞挂。

## 窗口数据的获取

各 provider 把窗口填进 ``models()`` 的时机不一：trae 只在 ``refresh_models()``
后才有（静态底表不带），而它没有启动 warmup，刚启动时 ``models()`` 是空的。
所以本模块**按需触发一次 refresh**（带超时、provider 级锁防并发重复），成功后
缓存进 ``_WINDOW_CACHE``，之后零开销。refresh 失败/无窗口的 ``(provider,model)``
进 ``_NO_WINDOW``，避免每个请求都重试 refresh。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

#: 估算超过窗口的这个比例就截断。留 12% 给 system 提示、工具定义、以及估算误差
#: （无 tiktoken，按 json 字符粗估；宁多截一点，也不要让上游报 4026）。
_SAFETY_RATIO = 0.88

#: 估算用的「字符/token」换算：json 序列化后的字符数 ÷ 3。中文 UTF-8 多字节，
#: 英文 4 字符/token，÷3 对两者都偏保守（高估），符合「宁多勿少」。
_CHARS_PER_TOKEN = 3.0

#: ``(provider_id, model_id_lower) -> window`` 正向缓存。
_WINDOW_CACHE: dict[tuple[str, str], int] = {}
#: 确认取不到窗口的键，避免每个请求反复触发 refresh。
_NO_WINDOW: set[tuple[str, str]] = set()
#: provider 级 refresh 进行中的标记，防并发重复打上游。
_refreshing: set[str] = set()
_refresh_lock = asyncio.Lock()
#: 单次 refresh 最长等待（秒）。trae/qoder 拉目录偶尔慢，超时就放行不拦请求。
_REFRESH_TIMEOUT_S = 8.0


def estimate_body_tokens(body: dict[str, Any]) -> int:
    """保守估算请求体的 token 量（高估优先）。

    只算会进模型上下文的部分：``messages`` / ``tools`` / ``system`` /
    ``prompt`` / ``input`` 等。序列化成 JSON 后按字符数 ÷ 3 估。没有精确
    tokenizer 的代价是误差，但用于「该不该截断」的二值决策足够——只要稳定
    高估，就不会漏截导致上游 4026。
    """
    parts: list[str] = []
    for key in ("messages", "tools", "system", "prompt", "input",
                "response_format", "tool_choice"):
        val = body.get(key)
        if val is not None:
            try:
                parts.append(json.dumps(val, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                parts.append(str(val))
    chars = sum(len(p) for p in parts)
    if chars <= 0:
        return 0
    return max(1, int(chars / _CHARS_PER_TOKEN) + 1)


def _window_from_models(provider: Any, model_id: str) -> int | None:
    """从 ``provider.models()`` 同步取该模型的窗口；取不到返回 None。"""
    if provider is None:
        return None
    models_fn = getattr(provider, "models", None)
    if not callable(models_fn):
        return None
    try:
        models = models_fn() or []
    except Exception as exc:  # noqa: BLE001 - models() 不该抛，抛了当没窗口
        log.warning("context_guard: %s.models() 抛错: %s",
                    getattr(provider, "id", "?"), exc)
        return None
    target = model_id.lower()
    for m in models:
        if not isinstance(m, dict):
            continue
        if str(m.get("id") or "").lower() != target:
            continue
        for key in ("context_window", "max_input", "maxAllowedSize"):
            val = m.get(key)
            if isinstance(val, int) and not isinstance(val, bool) and val > 0:
                return val
            if isinstance(val, str) and val.isdigit() and int(val) > 0:
                return int(val)
    return None


async def get_window(provider: Any, model_id: str) -> int | None:
    """取 ``(provider, model)`` 的真实上下文窗口（token），带缓存与按需 refresh。

    返回 None 表示拿不到（该 provider 不声明窗口 / refresh 失败 / 模型不在目录）。
    调用方应在 None 时**跳过截断、原样放行**，不能因此拒掉请求。
    """
    if not model_id:
        return None
    pid = getattr(provider, "id", "?")
    key = (pid, model_id.lower())
    if key in _WINDOW_CACHE:
        return _WINDOW_CACHE[key]
    if key in _NO_WINDOW:
        return None

    # 1) 同步先取一次（已有目录快照的 provider 这一步就够了）
    win = _window_from_models(provider, model_id)
    if win:
        _WINDOW_CACHE[key] = win
        return win

    # 2) 没有 → 按需触发 refresh（只对有该方法的 provider；带超时 + 并发锁）
    refresh = getattr(provider, "refresh_models", None)
    if callable(refresh) and pid not in _refreshing:
        async with _refresh_lock:
            if pid in _refreshing:
                pass  # 被并发请求触发了，等它完再取
            else:
                _refreshing.add(pid)
                try:
                    result = refresh()
                    if asyncio.iscoroutine(result):
                        await asyncio.wait_for(result, timeout=_REFRESH_TIMEOUT_S)
                except Exception as exc:  # noqa: BLE001 - 任何失败都不拦主链路
                    log.info("context_guard: %s.refresh_models 失败（跳过窗口守卫）: %s",
                             pid, exc)
                finally:
                    _refreshing.discard(pid)
        # 再取一次（refresh 可能填充了 _discovered / 模型目录）
        win = _window_from_models(provider, model_id)
        if win:
            _WINDOW_CACHE[key] = win
            return win

    _NO_WINDOW.add(key)
    return None


def truncate_body_for_window(
    body: dict[str, Any], window: int,
) -> tuple[dict[str, Any], int]:
    """若 ``body`` 估算超 ``window × SAFETY_RATIO``，截断消息历史返回新 body。

    返回 ``(new_body, dropped_count)``。未超或无可截断内容时返回 ``(body, 0)``
    （**原对象引用**，调用方据此判断是否真的截了）。

    截断策略（保守、只丢中间历史）：
    - 保留所有 ``role == "system"`` 消息（系统提示/工具教学，丢了模型就失能）；
    - 保留**最后一条**消息（最新用户输入或模型回复，是当前 turn 的核心）；
    - 从最旧的非 system、非末尾消息**整条**丢弃，直到估算 ≤ 预算或无可丢。
    只在该候选的 body 副本上改，原 body 不动——failover 下一个更大窗口的通道
    仍能拿到完整上下文。
    """
    if not isinstance(window, int) or window <= 0:
        return body, 0
    budget = int(window * _SAFETY_RATIO)
    if budget <= 0:
        return body, 0
    if estimate_body_tokens(body) <= budget:
        return body, 0

    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) <= 1:
        return body, 0  # 只剩一条或不是列表，没法安全截

    # 标记「必须保留」：全部 system（丢了模型失能）+ 最后一条（当前 turn 核心）。
    keep = [str(m.get("role") or "").lower() == "system"
            if isinstance(m, dict) else False
            for m in messages]
    keep[-1] = True

    dropped = 0
    # 从最旧往新扫：每轮找第一个非 keep 的消息整条丢掉，重算，直到达标或无可丢。
    while estimate_body_tokens({**body, "messages": messages}) > budget:
        idx = next((i for i in range(len(messages) - 1) if not keep[i]), -1)
        if idx < 0:
            break  # 只剩 keep 的，不能再丢（宁可放行让上游判，也不丢 system/末尾）
        messages = messages[:idx] + messages[idx + 1:]
        keep = keep[:idx] + keep[idx + 1:]
        dropped += 1
        if len(messages) <= 1:
            break
    if dropped == 0:
        return body, 0
    return {**body, "messages": messages}, dropped


# ---------------------------------------------------------------------------
# 测试 / 运行时的缓存重置
# ---------------------------------------------------------------------------

def reset_cache() -> None:
    """清空窗口缓存（测试隔离用；运行时不应调用）。"""
    _WINDOW_CACHE.clear()
    _NO_WINDOW.clear()
    _refreshing.clear()
