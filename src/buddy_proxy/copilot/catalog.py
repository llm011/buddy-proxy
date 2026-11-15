"""Copilot 模型目录：解析 ``GET /models``，决定每个模型走哪条上游端点。

目录是 Copilot 的**唯一事实源**——哪些模型对当前订阅开放、每个模型支持哪条
端点（``/chat/completions`` / ``/responses`` / ``/v1/messages``）、上下文与
输出上限，全在这里。代理不自己维护「模型 → 端点」映射：写死的映射会在
Copilot 上新模型/下线旧模型时静默错路由（请求打到不支持的端点 → 400
``no_available_model_endpoints``）。

实测要点（2026-11）：

- 类型在 ``capabilities.type``（``chat`` / ``completions`` / ``embeddings`` /
  ``image`` / ``decisions``），**顶层没有 type 字段**；
- ``policy.state == "disabled"`` 的模型在 picker 里可见但调不通（GitHub 要求
  用户先同意条款），目录照收、``models()`` 照列——调用时上游会明确拒绝，
  比我们从本地过滤掉更好排查；
- ``supported_endpoints`` 可能带 ``ws:`` 前缀（``ws:/responses``），是
  WebSocket 变体，对 HTTP 转发无意义，剥掉前缀后归并；
- ``supported_endpoints`` 为空（老的 gpt-4o 系列）时按 ``/chat/completions``
  兜底——它们全都只有这一条。

**对外模型名带通道前缀**（``copilot/gpt-5.6-terra``）：同一批模型 ID
（``gpt-4o``、``gpt-4.1``）在别的通道（codex、azure 系）也有，不带前缀会被
别的通道的 id 精确匹配抢走。带前缀只影响对外展示，上游请求体里的 ``model``
仍是裸 ID。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROVIDER_ID = "copilot"

#: 目录缓存有效期：新模型上线最多一小时被看到；登录/管理页强制刷新会跳过缓存。
CATALOG_TTL_S = 3600.0

#: 随包的静态兜底目录（``/models`` 拉不到时的最后手段）。
FALLBACK_PATH = Path(__file__).with_name("catalog_fallback.json")

#: 上游端点 ↔ 内部协议名。
ENDPOINT_TO_PROTOCOL = {
    "/chat/completions": "chat",
    "/responses": "responses",
    "/v1/messages": "anthropic",
}

#: 路由优先级：模型声明了多条端点时选哪条。
#:
#: ``/v1/messages`` 排第一是**前瞻性**的：个人订阅目前的目录里所有模型都只声明
#: chat 或 responses（实测 ``/v1/messages`` 对它们一律 400
#: ``no_available_model_endpoints``），但企业/组织订阅会解锁 Claude 系列，
#: 它们声明的是 Anthropic 原生端点——原生出口比经 chat 翻译更保真（思考块、
#: cache_control 都能原样过）。在它没出现之前这个分支不会被命中。
_ENDPOINT_PREFERENCE = ("/v1/messages", "/responses", "/chat/completions")


@dataclass(frozen=True)
class ModelEntry:
    """目录里的一个模型（只保留转发要用的字段）。"""

    id: str
    name: str
    vendor: str
    #: 上游端点（``/chat/completions`` / ``/responses`` / ``/v1/messages``）。
    endpoint: str
    #: 该模型声明的全部端点（已剥 ``ws:`` 前缀，保持目录原顺序）。
    endpoints: tuple[str, ...]
    context: int | None = None
    output: int | None = None
    picker: bool = False
    preview: bool = False
    policy_state: str = ""
    #: ``capabilities.supports`` 里的能力名（``tool_calls`` / ``vision`` …）。
    supports: tuple[str, ...] = ()
    #: ``capabilities.supports.reasoning_effort`` 的取值（字符串或数组）。
    reasoning_effort: tuple[str, ...] = ()

    @property
    def upstream(self) -> str:
        """内部协议名（``chat`` / ``responses`` / ``anthropic``）。"""
        return ENDPOINT_TO_PROTOCOL.get(self.endpoint, "chat")

    @property
    def disabled_by_policy(self) -> bool:
        return self.policy_state == "disabled"


def parse_catalog(payload: Any) -> list[ModelEntry]:
    """解析 ``/models`` 响应；只保留 ``capabilities.type == "chat"`` 的条目。

    ``completions``（FIM 补全）、``embeddings``、``image``、``decisions``
    都不是 chat 协议，代理不做这些接口，收进来只会在 ``/v1/models`` 里
    给用户几个永远 400 的名字。
    """
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    out: list[ModelEntry] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        model_id = str(raw.get("id") or "").strip()
        if not model_id:
            continue
        caps = raw.get("capabilities")
        if not isinstance(caps, dict):
            caps = {}
        if str(caps.get("type") or "chat") != "chat":
            continue
        endpoints = _normalize_endpoints(raw.get("supported_endpoints"))
        endpoint = _pick_endpoint(endpoints)
        limits = caps.get("limits") if isinstance(caps.get("limits"), dict) else {}
        supports = caps.get("supports") if isinstance(caps.get("supports"), dict) else {}
        efforts = supports.get("reasoning_effort")
        if isinstance(efforts, list):
            effort_values = tuple(str(e) for e in efforts if e)
        elif isinstance(efforts, str) and efforts:
            effort_values = (efforts,)
        else:
            effort_values = ()
        policy = raw.get("policy") if isinstance(raw.get("policy"), dict) else {}
        out.append(ModelEntry(
            id=model_id,
            name=str(raw.get("name") or model_id),
            vendor=str(raw.get("vendor") or ""),
            endpoint=endpoint,
            endpoints=tuple(endpoints),
            context=_int_or_none(limits.get("max_context_window_tokens")),
            output=_int_or_none(limits.get("max_output_tokens")),
            picker=bool(raw.get("model_picker_enabled")),
            preview=bool(raw.get("preview")),
            policy_state=str(policy.get("state") or ""),
            supports=tuple(sorted(str(k) for k, v in supports.items() if v and k != "reasoning_effort")),
            reasoning_effort=effort_values,
        ))
    return out


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _normalize_endpoints(raw: Any) -> list[str]:
    """``supported_endpoints`` → 去重后的 HTTP 端点列表（剥 ``ws:``、丢弃未知）。"""
    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text.startswith("ws:"):
            text = text[len("ws:"):]
        if text in ENDPOINT_TO_PROTOCOL and text not in seen:
            seen.append(text)
    return seen


def _pick_endpoint(endpoints: list[str]) -> str:
    """在多条端点里选一条；一条都没有时兜底 chat。"""
    for candidate in _ENDPOINT_PREFERENCE:
        if candidate in endpoints:
            return candidate
    return "/chat/completions"


def load_fallback_catalog(path: Path | None = None) -> list[ModelEntry]:
    """随包静态目录（``/models`` 不可用时的最后手段）。"""
    target = path or FALLBACK_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("copilot: 静态目录 %s 读取失败（%s）", target, exc)
        return []
    entries = parse_catalog(payload)
    if not entries:
        # 静态目录是精简格式（没有 capabilities 结构），单独解析一遍。
        entries = [
            ModelEntry(
                id=str(m.get("id") or ""),
                name=str(m.get("name") or m.get("id") or ""),
                vendor=str(m.get("vendor") or ""),
                endpoint=str(m.get("upstream_endpoint") or "/chat/completions"),
                endpoints=(str(m.get("upstream_endpoint") or "/chat/completions"),),
                context=_int_or_none(m.get("context")),
                output=_int_or_none(m.get("output")),
                picker=bool(m.get("picker")),
                preview=bool(m.get("preview")),
            )
            for m in (payload.get("models") or [])
            if isinstance(m, dict) and m.get("id")
        ]
    return entries


class ModelIndex:
    """当前生效的模型表：``model_id → ModelEntry`` + 对外名。

    线程安全靠「整表替换」而不是加锁读：刷新时构造新 dict 后一次性赋值，
    读侧拿到的永远是某个完整快照。
    """

    def __init__(self) -> None:
        self._entries: dict[str, ModelEntry] = {}
        #: 最近一次成功刷新的时刻（``0`` = 从未成功，只有静态兜底）。
        self.fetched_at: float = 0.0
        #: 目录来源：``upstream`` / ``fallback`` / ``empty``。
        self.source: str = "empty"

    def __bool__(self) -> bool:
        return bool(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, model_id: str) -> ModelEntry | None:
        return self._entries.get(model_id)

    def ids(self) -> list[str]:
        return list(self._entries)

    def entries(self) -> list[ModelEntry]:
        return list(self._entries.values())

    def replace(self, entries: list[ModelEntry], *, source: str) -> None:
        """整表替换；``entries`` 为空时**不**清空旧表（部分失败只增不删）。"""
        if not entries:
            log.warning("copilot: 目录刷新拿到空列表（source=%s），保留现有 %d 个模型",
                        source, len(self._entries))
            return
        self._entries = {e.id: e for e in entries}
        self.fetched_at = time.time()
        self.source = source

    def stale(self) -> bool:
        return not self._entries or (time.time() - self.fetched_at) > CATALOG_TTL_S

    def resolve(self, model: str) -> ModelEntry | None:
        """客户端传来的模型名 → 目录条目（接受裸 ID 与 ``copilot/<id>``）。

        ``copilot/`` 前缀只出现在对外名里；带前缀进来说明客户端是从
        ``/v1/models`` 抄的名字，剥掉即可。裸 ID 大小写不敏感——有的客户端
        会把模型名转小写。
        """
        text = str(model or "").strip()
        prefix = f"{PROVIDER_ID}/"
        if text.startswith(prefix):
            text = text[len(prefix):]
        if not text:
            return None
        entry = self._entries.get(text)
        if entry is not None:
            return entry
        lowered = text.lower()
        for model_id, candidate in self._entries.items():
            if model_id.lower() == lowered:
                return candidate
        return None
