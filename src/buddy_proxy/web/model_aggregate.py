"""``/v1/models`` 的去重聚合。

## 为什么需要它

每个通道（codebuddy / qoder / traepat / ark ...）各自报自己目录里的模型，同一个
``deepseek-v4.1-flash`` 会被报 7 次。直接把并集吐给客户端有三个后果：

1. 客户端选择器里同名项重复 7 行，用户不知道该选哪个；
2. id 里带通道前缀（``qoder/deepseek-v4.1-flash``）时，客户端配模型还得先拼通道名，
   可读性极差；
3. 占位项（``auto`` / ``default``）混在真模型里，选中后行为取决于上游当天的心情。

所以这里按**裸名**聚合：一个模型只出现一次，id 就是裸名。裸名请求由
``forward_chat`` 按 ``model_order`` 的候选链自动 failover（某个通道挂了换下一个），
客户端完全不需要知道通道的存在。

## 元数据合并口径（保守，取交集）

能力字段（``images`` / ``tool_call`` / ``reasoning`` / ``context_window`` /
``max_input`` / ``max_output``）一律**取交集**，理由不是审美而是正确性：

> 裸名会 failover 到任一候选通道。如果列表按「任一通道支持」声明了图片能力，
> 而请求恰好落到不支持图片的那个通道，用户就会收到一个莫名其妙的失败——目录
> 撒的谎，最后由请求来付代价。

宁可少声明（用户顶多觉得保守），不能多声明（用户会踩坑）。取交集时**忽略没有
该字段的条目**（该通道只是没报，不代表不支持）。
"""

from __future__ import annotations

from typing import Any, Iterable

#: 这些 id 是通道侧的占位项，不是真模型。选中后由上游自行决定用哪个，
#: 客户端看到的模型名与实际执行的模型可以不一致——不列进目录。
PLACEHOLDER_IDS: frozenset[str] = frozenset({"auto", "default"})

#: 能力类布尔字段：多通道聚合时全部支持才声明。
_CAPABILITY_BOOLS = ("images", "tool_call", "reasoning")

#: 数值类字段：多通道取最保守（最小值），避免声明出某个通道吃不下的大小。
_CAPABILITY_NUMS = ("max_input", "max_output", "context_window")


def _strip_known_prefix(model_id: str, known_providers: frozenset[str]) -> str:
    """``qoder/glm-5.3`` → ``glm-5.3``（只剥**已知通道**前缀）。

    模型自带斜杠时不能误切（``meta-llama/Llama-4`` 原样返回），所以必须靠
    ``known_providers`` 判断而不是「见斜杠就切」。

    ⚠️ 与 ``ui/models_api._bare_model_id(model_id, provider_id)`` **不是一件事**：
    那个只剥**调用方指定的那一个** provider 前缀（管理页按通道分组展示时用），
    这个剥**任何已知通道**前缀（聚合去重时用）。别拿错。
    """
    if "/" not in model_id:
        return model_id
    prefix, rest = model_id.split("/", 1)
    if prefix in known_providers and rest:
        return rest
    return model_id


def _merge_capability_bool(
    entries: list[dict[str, Any]], key: str, *, union: bool
) -> bool | None:
    """能力布尔合并；全部条目的该字段都缺失时返回 None。

    口径取决于这条模型**会不会 failover**：

    - ``union=True``（``model_order`` 里有候选链）：取**并集**。任一通道支持就
      声明支持——裸名请求会按候选链往下走，不支持它的那个通道失败后自动换到
      支持它的通道，能力不会被浪费。取交集反而白白砍功能（实测
      ``deepseek-v4.1-flash``：只有 codebuddy 报图片，取交集后目录说
      ``supports_images: false``，客户端在本地就把图剥了，可上游答得很好）。
    - ``union=False``（没有候选链，实际只打首选通道）：取**交集**。没有兜底时
      按「任一支持」声明，会让请求落到不支持的那个通道才失败。
    """
    seen = [bool(e[key]) for e in entries if key in e]
    if not seen:
        return None
    return any(seen) if union else all(seen)


def _merge_capability_num(entries: list[dict[str, Any]], key: str) -> int | None:
    """数值字段取最小值；无值返回 None。"""
    seen = [int(e[key]) for e in entries
            if isinstance(e.get(key), int) and not isinstance(e.get(key), bool)]
    if not seen:
        return None
    return min(seen)


def _pick_preferred(
    entries: list[dict[str, Any]],
    model_id: str,
    order: dict[str, list[str]],
    known_providers: frozenset[str],
) -> dict[str, Any]:
    """挑一个条目作为展示元数据（名称/价格/tags）的主源。

    优先级：``model_order`` 的第一候选 → ``codebuddy`` → 列表里第一个。
    ``model_order`` 排在最前是因为那正是裸名请求实际会先打的通道，展示它的
    价格与标签最贴近用户真实消耗。
    """
    targets = order.get(model_id) or []
    for target in targets:
        pid, _, mid = target.partition("/")
        if not mid:
            pid, mid = "codebuddy", target
        for e in entries:
            if e.get("provider") == pid and \
                    _strip_known_prefix(str(e.get("id") or ""), known_providers) == mid:
                return e
    for e in entries:
        if e.get("provider") == "codebuddy":
            return e
    return entries[0]


def aggregate_models(
    entries: Iterable[dict[str, Any]],
    *,
    order: dict[str, list[str]] | None = None,
    known_providers: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """把多通道目录压成「一模型一条」。

    ``entries`` 是各通道的原始条目（可带 ``provider`` 与带前缀的 ``id``）。
    返回的条目 ``id`` 为裸名，``provider`` 为选中的展示通道，另加：

    - ``failover``：该裸名在 ``model_order`` 里有候选链时为 True，表示请求会
      自动换通道；
    - ``providers``：该模型实际可用的全部通道（排障用，顺序即候选优先级）。
    """
    order = order or {}
    buckets: dict[str, list[dict[str, Any]]] = {}
    for raw in entries:
        raw_id = str(raw.get("id") or "").strip()
        if not raw_id:
            continue
        bare = _strip_known_prefix(raw_id, known_providers)
        if bare in PLACEHOLDER_IDS:
            # 显式指定仍然可用（forward 不看目录），只是不主动曝光
            continue
        buckets.setdefault(bare, []).append(raw)

    out: list[dict[str, Any]] = []
    for bare, group in buckets.items():
        preferred = _pick_preferred(group, bare, order, known_providers)
        ordered_providers: list[str] = []
        # 先按 model_order 的候选顺序，再补上目录里其余通道
        for target in order.get(bare) or []:
            pid = target.partition("/")[0] or "codebuddy"
            if pid not in ordered_providers:
                ordered_providers.append(pid)
        for e in group:
            pid = str(e.get("provider") or "codebuddy")
            if pid not in ordered_providers:
                ordered_providers.append(pid)

        merged: dict[str, Any] = {
            "id": bare,
            "name": preferred.get("name") or bare,
            "provider": preferred.get("provider") or "codebuddy",
            "providers": ordered_providers,
            "failover": bool(order.get(bare)),
        }
        if preferred.get("vendor"):
            merged["vendor"] = preferred["vendor"]
        if preferred.get("credits") is not None:
            merged["credits"] = preferred["credits"]
        if preferred.get("tags"):
            merged["tags"] = preferred["tags"]

        for key in _CAPABILITY_BOOLS:
            value = _merge_capability_bool(group, key, union=merged["failover"])
            if value is not None:
                merged[key] = value
        for key in _CAPABILITY_NUMS:
            value = _merge_capability_num(group, key)
            if value is not None:
                merged[key] = value
        out.append(merged)

    # 稳定排序：带 failover 的（配置里点过名的）在前，其余按裸名
    out.sort(key=lambda m: (not m["failover"], m["id"]))
    return out
