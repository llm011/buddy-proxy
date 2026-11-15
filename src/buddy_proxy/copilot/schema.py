"""把 MCP / Claude Code 风格的宽松 JSON Schema 收敛成 Copilot ``/v1/messages`` 能接受的形状。

**为什么必须做这件事**：Copilot 的 Anthropic 兼容端点对 ``tools[].input_schema``
做严格校验，只要有一个 schema 命中下面任意一条，整条请求就回
``400 invalid request body``（``code: invalid_request_body``），连错误定位都不给：

1. **组合关键字与同级 ``type`` 并存**：``{"anyOf": […], "type": "string"}``。
   JSON Schema 规范允许，Copilot 拒绝。
2. **``type`` 是联合数组**：``{"type": ["null", "string"]}`` 能过，但
   ``{"type": ["array", "null"]}`` / 六类型的联合会被拒——只要联合里含
   ``"array"`` 又没配套的 ``items``，或联合过长（六个类型全写）。
3. **``items`` 不在 ``type: "array"`` 下面**：``{"type": ["null","string"], "items": {}}``、
   裸 ``{"items": {}}`` 都会被拒。``items`` 写成数组（旧版 tuple 校验）同样被拒。
4. **顶层必须是「带 properties 的 object」**：``{"type":"array","items":{}}``、``{}``、
   ``{"type":"object"}`` 都是 ``400``，`{"type":"object","properties":{…}}` 才是 200
   （实测，同一工具名对照）。所以顶层 ``type`` 一律钉成 ``object`` 并保证有
   ``properties``；嵌套层的 ``array`` + ``items`` 是合法的（实测 200）。

MCP 服务器（本机就有几十个）和 Claude Code 的工具集正好大量命中这几条，
于是 ``/model copilot/gemini-4-hydrogen`` 这种请求**一律** 400——不是模型不行，
是历史会话里随便哪个工具的 schema 都足以把整条请求打死。

**同一份 schema 走 chat 端点 ``/chat/completions`` 是可以通过的**（实测 249 个
工具全带上照样 200），所以只在 anthropic 透传这条路上收敛，别的出口保持原样：
客户端拿到的工具定义不能被我们悄悄改窄，只有上游不接受时才降级。

收敛是**只删不改语义**的：联合里的 ``null`` 去掉（可选性已经由 ``required``
表达）、多类型联合优先保留 ``object``/``array`` 的形状信息、被拒的组合关键字
形状原样保留（``anyOf`` 本身能过，冲突的只是同级的 ``type``）。
"""

from __future__ import annotations

from typing import Any

#: 与同级 ``type`` 冲突的组合关键字。三个都不能和 ``type`` 出现在同一层。
_COMBINATORS = frozenset({"anyOf", "oneOf", "allOf"})


def sanitize_anthropic_tools(tools: Any) -> tuple[Any, int]:
    """收敛一份 Anthropic ``tools`` 列表；返回（新列表, 被改动的工具数）。

    ``tools`` 不是列表时原样返回（``0``）。只复制改动过的条目，未命中的工具
    保持同一个对象，方便上层做「要不要重写请求体」的判断。
    """
    if not isinstance(tools, list):
        return tools, 0
    out: list[Any] = []
    changed = 0
    for tool in tools:
        if not isinstance(tool, dict):
            out.append(tool)
            continue
        schema = tool.get("input_schema")
        if not isinstance(schema, dict):
            out.append(tool)
            continue
        fixed = _sanitize_node(schema, root=True)
        if fixed == schema:
            out.append(tool)
            continue
        out.append({**tool, "input_schema": fixed})
        changed += 1
    return out, changed


def _sanitize_node(node: Any, *, root: bool = False) -> Any:
    """递归收敛一个 JSON Schema 子树（``dict`` / ``list`` 以外的值原样返回）。

    ``root`` 表示这是工具的顶层 ``input_schema``：上游只接受 object 形状的顶层
    （``{"type":"array","items":{}}`` 和空 ``{}`` 都是 ``400``，实测），所以顶层
    的 ``type`` 一律钉成 object，``items`` 在顶层没有意义，直接丢掉。
    """
    if isinstance(node, list):
        return [_sanitize_node(item) for item in node]
    if not isinstance(node, dict):
        return node

    out = {key: _sanitize_node(value) for key, value in node.items()}

    # 1) 组合关键字与同级 type 不能并存：type 是多余的描述，删掉。
    if _COMBINATORS & out.keys():
        out.pop("type", None)

    # 2) type 写成联合数组时收敛成单一类型。
    declared = out.get("type")
    if isinstance(declared, list):
        rest = [item for item in declared if item != "null"]
        # 带 properties 的联合一定是 object：别把形状信息选丢。
        if "object" in rest or "properties" in out:
            out["type"] = "object"
        elif len(rest) == 1:
            out["type"] = rest[0]
        elif "array" in rest:
            out["type"] = "array"
        elif rest:
            out["type"] = rest[0]
        else:
            out.pop("type", None)

    if root:
        out["type"] = "object"
        out.pop("items", None)
        # 光有 type 没有 properties 也过不去（实测 400）；补个空对象。
        if not isinstance(out.get("properties"), dict):
            out["properties"] = {}
        return out

    # 3) items 只在 type == "array" 下合法；array 又必须带一个合法的 items。
    if out.get("type") != "array":
        out.pop("items", None)
    else:
        items = out.get("items")
        if items is None or isinstance(items, list):
            out["items"] = {}
    return out
