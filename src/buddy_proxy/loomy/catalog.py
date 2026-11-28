"""Loomy 模型目录：``GET /models`` 解析 + 倍率提取。

## 上游返回形状（实测 2026-11-29，生产网关）

::

    {"data": {"data": [ <model>, ... ]}}

注意是**两层 data**（网关统一 envelope 套 OpenAI 的 ``data`` 数组）。只按
OpenAI 标准读 ``payload["data"]`` 会拿到一个 dict，`.map` 不出来——所以解析
同时兼容三种形状（双层 / 单层 / 裸数组）。

单个 model::

    {"id": "spark-x2.6",
     "name": "Spark-X2.6 正式版(x2.5)",     # <- 倍率只在这里
     "type": "chat",                        # chat / image
     "protocol": "openai_chat",             # 实测全部 openai_chat
     "context_length": 1048576,
     "max_output_tokens": 131072,
     "is_default_model": true,
     "reasoning_efforts": ["none","low","medium","high","xhigh"],
     "default_reasoning_effort": "low",
     "capabilities": {"reasoning": true, "vision": false,
                      "function_calling": true, "streaming": true,
                      "input_modalities": ["text"], ...}}

## 倍率为什么只能从 ``name`` 里正则抽

上游**没有**独立的倍率字段（所有 key 都验过了：没有 ``multiplier`` /
``ratio`` / ``credit_rate``）。倍率只以 ``(x2.5)`` / ``(x0.8)`` / ``（x0.8）``
这种中英括号混排的形式嵌在显示名末尾。所以这里用正则抽，抽不到就是 ``None``
（表示「上游没标」），**不要**默认成 1.0——那会让「未知倍率」和「倍率恰好
是 1」在 UI 上无法区分。

倍率**只是展示用**：真实扣分以 ``usage.points_consumed`` 为准（服务端算）。
两者不一致时以服务端为准，倍率仅用于「为什么这个模型贵」的解释。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

#: 从显示名里抽倍率：``(x2.5)`` / ``（x0.8）`` / ``(X2.0)``，允许前后空格。
#:
#: 用 ``[（(]`` 同时兼容全角半角括号——实测上游两种混用（``qwen 3.8
#: flash（x0.8）`` 是全角，``Hy4 Flash(x0.5)`` 是半角）。
_MULTIPLIER_RE = re.compile(r"[（(]\s*[xX×]\s*([0-9]+(?:\.[0-9]+)?)\s*[)）]")

#: 目录缓存时长（秒）。模型表变化不频繁，但登录后首次拉取必须成功。
_CATALOG_TTL_S = 600


def parse_multiplier(display_name: str) -> float | None:
    """从显示名抽倍率；抽不到返回 ``None``（**不是** 1.0，见模块说明）。"""
    match = _MULTIPLIER_RE.search(display_name or "")
    if not match:
        return None
    try:
        value = float(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def strip_multiplier(display_name: str) -> str:
    """去掉显示名末尾的倍率后缀，得到干净的名字（UI 上展示用）。"""
    cleaned = _MULTIPLIER_RE.sub("", display_name or "").strip()
    return cleaned or (display_name or "").strip()


@dataclass(frozen=True)
class LoomyModel:
    """目录里的一个模型。"""

    id: str
    name: str
    kind: str = "chat"          # chat / image
    protocol: str = "openai_chat"
    context_length: int | None = None
    max_output_tokens: int | None = None
    reasoning: bool = False
    vision: bool = False
    function_calling: bool = False
    input_modalities: tuple[str, ...] = ("text",)
    default_reasoning_effort: str = ""
    is_default: bool = False
    is_default_small: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def multiplier(self) -> float | None:
        """倍率（从 ``name`` 抽）。``None`` 表示上游没标。"""
        return parse_multiplier(self.name)

    @property
    def display_name(self) -> str:
        """去掉倍率后缀的显示名。"""
        return strip_multiplier(self.name)

    @property
    def is_image(self) -> bool:
        return self.kind == "image"

    def to_openai_model(self, provider_id: str = "loomy") -> dict[str, Any]:
        """转成 ``/v1/models`` 的元素。

        字段名对齐项目既有约定（``max_input`` / ``max_output`` /
        ``context_window`` / ``credits``），这样 ``web/model_aggregate`` 的
        能力合并逻辑能直接读到，不会因为用了上游原名（``context_length`` /
        ``max_output_tokens``）而在聚合层被静默丢掉。
        """
        out: dict[str, Any] = {
            "id": self.id,
            "object": "model",
            "created": int(self.raw.get("created") or 0),
            "owned_by": provider_id,
            "name": self.name,
            "type": self.kind,
            "protocol": self.protocol,
        }
        if self.context_length:
            out["context_window"] = self.context_length
            out["max_input"] = self.context_length
        if self.max_output_tokens:
            # 两个键名都放：聚合层读 max_output，原始 OpenAI 客户端读
            # max_output_tokens。少一个就有半边读不到。
            out["max_output"] = self.max_output_tokens
            out["max_output_tokens"] = self.max_output_tokens
        multiplier = self.multiplier
        if multiplier is not None:
            # ``credits`` 是项目里「这条通道每 token 的计价倍率」的既有字段，
            # 管理页会把它当价格展示。Loomy 的 xN 正是这个语义。
            out["credits"] = multiplier
            out["multiplier"] = multiplier
        if self.is_default:
            out["is_default_model"] = True
        if self.is_default_small:
            out["is_default_small_model"] = True
        # 能力字段名与 web/model_aggregate 的 _CAPABILITY_BOOLS 对齐
        # （images / tool_call / reasoning），否则聚合层读不到，客户端会以为
        # 这些模型不支持工具调用——agent 类客户端（dsh/Claude Code/Codex）
        # 全靠它，丢了的表现是「跑一次就停」而不是报错。
        if self.function_calling:
            out["tool_call"] = True
        if self.reasoning:
            out["reasoning"] = True
        if self.vision:
            out["images"] = True
        return out


def _iter_model_items(payload: Any) -> list[dict[str, Any]]:
    """从三种可能的 envelope 里挖出模型数组。"""
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    if not isinstance(payload, dict):
        return []
    inner = payload.get("data")
    if isinstance(inner, list):
        return [m for m in inner if isinstance(m, dict)]
    if isinstance(inner, dict):
        # 双层 data：{"data": {"data": [...]}}
        deeper = inner.get("data")
        if isinstance(deeper, list):
            return [m for m in deeper if isinstance(m, dict)]
        # {"data": {"models": [...]}}
        models = inner.get("models")
        if isinstance(models, list):
            return [m for m in models if isinstance(m, dict)]
    models = payload.get("models")
    if isinstance(models, list):
        return [m for m in models if isinstance(m, dict)]
    return []


def _to_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_catalog(payload: Any) -> list[LoomyModel]:
    """把 ``/models`` 的响应体解析成模型列表（顺序保持上游给的）。"""
    models: list[LoomyModel] = []
    seen: set[str] = set()
    for item in _iter_model_items(payload):
        model_id = str(item.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)

        capabilities = item.get("capabilities")
        caps = capabilities if isinstance(capabilities, dict) else {}
        raw_modalities = caps.get("input_modalities")
        modalities: tuple[str, ...] = ("text",)
        if isinstance(raw_modalities, list):
            cleaned = tuple(
                str(m).strip().lower() for m in raw_modalities if str(m).strip()
            )
            if cleaned:
                modalities = cleaned

        models.append(
            LoomyModel(
                id=model_id,
                name=str(item.get("name") or model_id),
                kind=str(item.get("type") or "chat").strip().lower() or "chat",
                protocol=str(item.get("protocol") or "openai_chat").strip() or "openai_chat",
                context_length=_to_int(item.get("context_length")),
                max_output_tokens=_to_int(item.get("max_output_tokens")),
                reasoning=caps.get("reasoning") is True,
                vision=caps.get("vision") is True,
                function_calling=caps.get("function_calling") is True,
                input_modalities=modalities,
                default_reasoning_effort=str(
                    item.get("default_reasoning_effort") or ""
                ).strip(),
                is_default=item.get("is_default_model") is True,
                is_default_small=item.get("is_default_small_model") is True,
                raw=item,
            )
        )
    return models


class CatalogCache:
    """``/models`` 的内存缓存（TTL + 失败不覆盖旧值）。"""

    def __init__(self, ttl_s: float = _CATALOG_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._models: list[LoomyModel] | None = None
        self._fetched_at: float = 0.0

    @property
    def models(self) -> list[LoomyModel]:
        return list(self._models or [])

    def is_fresh(self, now: float | None = None) -> bool:
        if self._models is None:
            return False
        current = time.monotonic() if now is None else now
        return (current - self._fetched_at) < self._ttl_s

    def store(self, models: list[LoomyModel], now: float | None = None) -> None:
        """写入缓存。**空列表不写入**——上游偶发返回空表（灰度/限流）时，
        用空表覆盖会让目录整个消失，直到下次刷新；保留旧值更安全。
        """
        if not models:
            return
        self._models = list(models)
        self._fetched_at = time.monotonic() if now is None else now

    def invalidate(self) -> None:
        self._models = None
        self._fetched_at = 0.0

    def dump_json(self) -> str:
        return json.dumps(
            [m.to_openai_model() for m in self.models], ensure_ascii=False
        )
