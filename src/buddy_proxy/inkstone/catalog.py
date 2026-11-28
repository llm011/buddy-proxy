"""书生·端砚 模型目录：``/local-agent/models`` 解析。

## 上游返回形状（实测 2026-11-29，端砚 0.2.13）

::

    GET {base}/local-agent/models?supports_output_resource_limits=true
    -> {"code":0,"msg":"success","trace_id":"...",
        "data":{"supports_output_resource_limits":true,
                "protocol_version":"2.0",
                "models":[ <model>, ... ]}}

注意这是**网关 envelope**（``code`` / ``data``），不是 OpenAI 的 ``{"data":[...]}``
——按 OpenAI 读会拿到一个 dict，``.map`` 不出来。

单个 model::

    {"id":"书生-S2.1-Flash",              # 客户端填进请求体的名字
     "canonical_id":"sage-s2.1-flash",    # 内部 id，也能直接当 model 用
     "display_name":"书生-S2.1-Flash",
     "local_icon":"https://.../x.svg",
     "tags":["科学专精"],
     "pro":false,
     "context_window":524288,
     "max_output_tokens":131072,
     "supports_thinking":true,
     "supports_tools":true,
     "supports_attachments":false,
     "answer_transport":"tool_arguments"}

``answer_transport`` 是端砚协议里的**答案回传方式**，三个取值：

- ``tool_arguments``：最终答案从工具调用参数里取（多数模型）
- ``native_terminal_content``：走原生 content 块（书生-Agents-A1 / A1.5）
- ``strict_eof``：以流结束为准（书生-S2）

我们的适配层统一把 ``content_delta`` 拼成 OpenAI 的 ``delta.content``，所以这个
字段只做**展示与排查**用，不改变转发行为——上游在 SSE 里已经把三种都归一成
``content_delta`` 了（实测三个模型都是这个事件名）。

## ``Auto`` 是个特殊模型

``id="Auto"``、``canonical_id="sage-s2.1-flash"``、``display_name="Default"``：
它是客户端的「自动选型」入口，``canonical_id`` 与「书生-S2.1-Flash」相同。两个
都保留——用户从客户端截图里抄 ``Auto`` 时能命中，按 ``sage-s2.1-flash`` 调也
能命中。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: 目录缓存时长（秒）。模型表变化不频繁，但登录后首次拉取必须成功。
CATALOG_TTL_S = 600


@dataclass(frozen=True)
class InkstoneModel:
    """目录里的一个模型。"""

    id: str
    canonical_id: str = ""
    display_name: str = ""
    tags: tuple[str, ...] = ()
    pro: bool = False
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_thinking: bool = False
    supports_tools: bool = False
    supports_attachments: bool = False
    answer_transport: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_openai_model(self, owner: str) -> dict[str, Any]:
        """转成 ``/v1/models`` 的一个元素。

        ``id`` 用**客户端名**（``书生-S2.1-Flash``）——那是端砚界面里显示的名字，
        用户从截图里抄的就是它；``canonical_id`` 放进 ``aliases``，路由侧
        ``accepts_model`` 也会认。
        """
        return {
            "id": f"{owner}/{self.id}" if owner else self.id,
            "object": "model",
            "created": 0,
            "owned_by": owner,
            "context_length": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "aliases": [self.canonical_id] if self.canonical_id and self.canonical_id != self.id else [],
            "capabilities": {
                "reasoning": self.supports_thinking,
                "vision": self.supports_attachments,
                "function_calling": self.supports_tools,
                "streaming": True,
            },
        }


def _as_int(value: Any) -> int | None:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _as_str_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(x) for x in value if isinstance(x, str) and x.strip())


def parse_catalog(payload: Any) -> list[InkstoneModel]:
    """解析 ``/local-agent/models`` 响应。

    兼容三种形状（envelope / 单层 / 裸数组）——上游历史上换过包法，多兼容一层
    比在报错里猜「是网关变了还是我读错了」便宜。
    """
    rows: Any = payload
    if isinstance(rows, dict):
        data = rows.get("data")
        if isinstance(data, dict):
            # ``{"data":{"models":[...]}}`` 或再套一层 ``{"data":{"data":[...]}}``
            rows = data.get("models")
            if not isinstance(rows, list):
                rows = data.get("data")
        elif isinstance(data, list):
            rows = data
    if not isinstance(rows, list):
        return []

    models: list[InkstoneModel] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        models.append(
            InkstoneModel(
                id=mid,
                canonical_id=str(row.get("canonical_id") or "").strip(),
                display_name=str(row.get("display_name") or "").strip(),
                tags=_as_str_list(row.get("tags")),
                pro=bool(row.get("pro")),
                context_window=_as_int(row.get("context_window")),
                max_output_tokens=_as_int(row.get("max_output_tokens")),
                supports_thinking=bool(row.get("supports_thinking")),
                supports_tools=bool(row.get("supports_tools")),
                supports_attachments=bool(row.get("supports_attachments")),
                answer_transport=str(row.get("answer_transport") or "").strip(),
                raw=row,
            )
        )
    return models


class CatalogCache:
    """模型目录的进程内缓存（带 TTL）。"""

    def __init__(self, ttl: float = CATALOG_TTL_S) -> None:
        self._ttl = ttl
        self._models: list[InkstoneModel] = []
        self._fetched_at = 0.0

    @property
    def models(self) -> list[InkstoneModel]:
        return list(self._models)

    def is_fresh(self) -> bool:
        if not self._models:
            return False
        return (time.time() - self._fetched_at) < self._ttl

    def store(self, models: list[InkstoneModel]) -> None:
        if not models:
            return
        self._models = list(models)
        self._fetched_at = time.time()

    def invalidate(self) -> None:
        self._models = []
        self._fetched_at = 0.0
