"""模块边界（分层）的机器检查。

``docs/architecture.md`` 把仓库写成三段式：``core`` 是共享底座，``protocols`` 是
客户端协议层，``trae`` / ``qoder`` / ``gemini`` … 是互不相关的通道适配器，``web``
是入口。这些边界以前只写在文档和 review 习惯里，破坏它们的代价很具体：

- ``core`` 一旦 import 通道包，就会撞上 ``codebuddy_provider → observability →
  core.state`` 的循环导入——历史上正是靠 ``if TYPE_CHECKING:`` 补丁绕过去的；
- 通道之间互相 import 会把两个独立逆向通道钉在一起：一个通道改内部实现，另一个
  跟着坏，而它俩的上游毫无关系（``qoder`` 曾经直接从 ``trae.aes_pure`` 取 AES，
  现该模块已下沉到 ``core/aes_pure.py``）；
- ``web`` 被下层 import 会让「入口层可以依赖一切」的假设反过来。

本测试用 AST 静态扫描把边界固化成可执行规则，作用等价于 import-linter，但**不引入
新依赖**：这个项目的依赖刻意保持极简（运行时四个，CI 只有 ruff + pytest），
而这里需要的规则表达力（按文件、按 import 位置、带理由的豁免表）比 import-linter
的配置更直接。

判定口径：
- **module** 级 = import 语句不在任何函数体内（含模块级 ``if TYPE_CHECKING``）；
- **lazy** = import 在函数体内。两者分开记：module 级会参与包初始化顺序，是循环
  导入的真正来源；函数内 delay 的耦合只是「用到时才引入」，风险低得多。
- 通道之间的耦合不是绝对禁止，而是**必须登记**：``_DECLARED_COUPLING`` 里逐条写清
  谁依赖谁、是 module 还是 lazy、为什么。集合是精确相等的——新增耦合会让测试红，
  删除耦合也会（免得留下过期的理由）。等价于 import-linter 的
  ``ignore_imports`` 白名单，只是每条都强制带一句人能读的理由。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

#: 源码根。测试只读 AST，不 import 被测模块（避免测试自身触发副作用）。
_SRC = Path(__file__).resolve().parents[1] / "src" / "buddy_proxy"

#: 通道子包：彼此平行、上游互不相关，除登记豁免外不得互相 import。
_CHANNELS = frozenset({
    "antigravity", "codebuddy_provider", "codex", "doubao", "dumate",
    "gemini", "kimi", "mimo", "providers", "qoder", "trae",
})

#: 共享层的顶层包名：所有通道都允许依赖它们，它们本身受更严的规则约束。
_SHARED_LAYERS = frozenset({"core", "protocols"})

#: 根级模块（``__main__.py`` / ``asyncutil.py`` …）的 importer_pkg 取值。
_ROOT = "buddy_proxy"

#: **叶子**模块：只允许 import 标准库。它们被 core 与各通道从任意位置 import，
#: 一旦反向依赖任何 ``buddy_proxy.*``，循环导入就从这里开始。列表里的路径必须真实
#: 存在（见 ``test_leaf_modules_exist``），避免改名后规则悄悄失效。
_LEAF_MODULES = frozenset({
    "buddy_proxy.core.account_failover",  # 账号级冷却状态机（多通道共用）
    "buddy_proxy.core.account_store",     # 多账号索引 + 0600 凭据文件底座
    "buddy_proxy.core.aes_pure",          # 纯 Python AES（trae/qoder 共用）
    "buddy_proxy.core.browser",           # 拉起浏览器
    "buddy_proxy.core.cooldown",          # 模型级冷却标记
    "buddy_proxy.core.desensitize",       # 请求脱敏
    "buddy_proxy.core.errors",            # 异常分类（DNS 判定等）
    "buddy_proxy.core.logging_setup",     # 日志装配
    "buddy_proxy.core.metrics_protocol",  # 指标记录协议（contextvar）
    "buddy_proxy.core.paths",             # 状态目录解析
    "buddy_proxy.asyncutil",              # 事件循环桥（跨线程调度）
})

#: ``providers.base`` 是 ``BaseProvider`` 契约本身，语义属于共享层：每个通道都要
#: 实现它，实现方 import 契约不算「通道互相依赖」。它在 ``providers/`` 包里只是历史
#: 位置（该包剩下的都是薄官方 API 通道），所以单独点名而不把它算进通道耦合。
_SHARED_CONTRACT_MODULES = frozenset({"buddy_proxy.providers.base"})

#: 已登记的跨层耦合：``(importer, target, scope) -> 理由``。
#: **集合精确相等**（见 ``test_declared_coupling_matches_reality``）：新增要登记，
#: 删掉也要删登记，别让理由表腐烂成一份"以前发生过什么"的旧地图。
_DECLARED_COUPLING: dict[tuple[str, str, str], str] = {
    # -- 通道 ↔ 通道 --------------------------------------------------------
    ("antigravity/convert.py", "buddy_proxy.gemini.convert", "module"):
        "Antigravity 上游走的就是 Gemini 线缆，请求/响应转换直接复用 gemini.convert；"
        "同族协议复用而非平行实现，改 Gemini 线缆必须同时想一遍 Antigravity。",
    ("antigravity/provider.py", "buddy_proxy.gemini.convert", "module"):
        "同上：非流式响应还原复用 gemini.convert。",
    ("antigravity/provider.py", "buddy_proxy.gemini.provider", "lazy"):
        "流式适配器（_to_openai_stream/_to_anthropic_stream）直接复用 Gemini 通道实现；"
        "放函数内是因为它还会反向 import 本包，模块级会互锁。",
    ("codebuddy_provider/forward.py", "buddy_proxy.trae.pat", "lazy"):
        "路由层判断 traepat（服务账号）前缀模型归属只需 pat_gateway_is_plus 一个布尔；"
        "函数内 import 避免为了一个前缀判断拉起整个 trae 包初始化。",
    ("providers/ark.py", "buddy_proxy.codex.projection", "lazy"):
        "Ark 的 Responses 协议与 Codex 同构，复用其事件投影（chat↔responses）。",
    ("providers/ark.py", "buddy_proxy.codex.sse", "lazy"):
        "同上：复用 Codex 的 SSE 事件解析。",
    # -- core → 通道（仅限函数内 + 有兜底） ---------------------------------
    ("core/credit_estimate.py", "buddy_proxy.trae.config", "lazy"):
        "积分/倍率换算要读 Trae 的倍率表与别名；带 try/except 兜底，"
        "通道缺席时退回基准倍率，因此不能变成 core 的模块级硬依赖。",
    ("core/credit_estimate.py", "buddy_proxy.providers.zcode", "lazy"):
        "同上：读 zcode 的抵扣系数与时段折扣函数，缺失时按无折扣处理。",
}


@dataclass(frozen=True)
class Edge:
    """一条指向 ``buddy_proxy.*`` 的 import。"""

    importer: str      # 相对 src/buddy_proxy 的 posix 路径，如 "core/state.py"
    importer_pkg: str  # 顶层包名；根级模块为 "buddy_proxy"
    target: str        # 解析后的绝对模块名（相对 import 已按包路径还原）
    target_pkg: str    # target 的顶层包名
    scope: str         # "module"（不在函数体内）或 "lazy"（函数内）
    line: int


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(_SRC).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join([_ROOT, *parts]) if parts else _ROOT


def _package_of(path: Path) -> str:
    """该文件所在的包（``__init__.py`` 自己就是包）。"""
    if path.name == "__init__.py":
        return _module_name(path)
    return _module_name(path).rsplit(".", 1)[0]


def _resolve_relative(path: Path, level: int, module: str | None) -> str:
    """把相对 import 还原成绝对模块名（``level`` 是 Python 的点数，1 = 当前包）。"""
    if level == 0:
        return module or ""
    parts = _package_of(path).split(".")
    base = parts[: len(parts) - (level - 1)]
    if module:
        base.extend(module.split("."))
    return ".".join(base)


def _function_scope_ids(tree: ast.AST) -> set[int]:
    """函数体内的节点 id 集合（含嵌套函数与 lambda 体）。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for sub in ast.walk(node):
                ids.add(id(sub))
    return ids


def _iter_edges() -> list[Edge]:
    edges: list[Edge] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lazy_ids = _function_scope_ids(tree)
        rel = path.relative_to(_SRC).as_posix()
        importer_pkg = rel.split("/", 1)[0] if "/" in rel else _ROOT
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom):
                base = _resolve_relative(path, node.level, node.module)
                targets.append(base)
                # ``from . import x`` 里的 x 是子模块（``from .x import y`` 的 y 不是）。
                if node.module is None:
                    targets.extend(f"{base}.{a.name}" for a in node.names if a.name != "*")
            elif isinstance(node, ast.Import):
                targets.extend(a.name for a in node.names)
            else:
                continue
            scope = "lazy" if id(node) in lazy_ids else "module"
            for target in targets:
                if not target.startswith(f"{_ROOT}."):
                    continue
                edges.append(Edge(
                    importer=rel,
                    importer_pkg=importer_pkg,
                    target=target,
                    target_pkg=target.split(".")[1],
                    scope=scope,
                    line=node.lineno,
                ))
    return edges


# 扫描一次，多个测试共用（源码树在测试进程内不会变）。
_EDGES = _iter_edges()


def _fmt(edges: list[Edge]) -> str:
    return "\n".join(
        f"  {e.importer}:{e.line} ({e.scope}) {e.importer_pkg} -> {e.target}" for e in edges
    )


def test_leaf_modules_exist():
    """规则清单里的叶子模块必须真实存在，否则改名后规则会静默失效。"""
    existing = {_module_name(p) for p in _SRC.rglob("*.py")}
    missing = sorted(_LEAF_MODULES - existing)
    assert not missing, f"叶子模块清单里有不存在的模块（改名后请同步本测试）：{missing}"


def test_leaf_modules_import_only_stdlib():
    """叶子模块只 import 标准库。

    这些模块是 ``core`` 与各通道的公共底座（冷却状态机、AES、事件循环桥…），
    任何反向依赖都会把「谁都能 import 它」变成循环导入的起点。
    """
    bad = [
        e for e in _EDGES
        if _module_name(_SRC / e.importer) in _LEAF_MODULES
    ]
    assert not bad, (
        "叶子模块不得 import 任何 buddy_proxy.*（只能标准库）：\n" + _fmt(bad)
    )


def test_core_does_not_import_channels_at_module_level():
    """``core`` 不得在模块级 import 通道包。

    这是 ``core.state`` 曾经用 ``if TYPE_CHECKING:`` 绕开的那条边：模块级 import
    通道包会触发 ``codebuddy_provider.__init__ → observability → core.state`` 的
    循环。需要读通道常量时用函数内 import 并兜底（见 ``_DECLARED_COUPLING``）。
    """
    bad = [
        e for e in _EDGES
        if e.importer_pkg == "core"
        and e.target_pkg in _CHANNELS
        and e.scope == "module"
        and e.target not in _SHARED_CONTRACT_MODULES
    ]
    assert not bad, (
        "core 不能在模块级依赖通道包（改用函数内 import + 兜底，或把共用件下沉到 core）：\n"
        + _fmt(bad)
    )


def test_protocols_depend_only_on_shared_layers():
    """``protocols`` 只依赖 ``core`` / ``protocols``。

    协议层要能被任何通道复用，也必须能在没有 pyinstaller 式打包的情况下单独理解：
    它一旦 import 某个通道，那个通道的初始化（读本机 IDE 登录态等）就会被协议层带着走。
    """
    allowed = _SHARED_LAYERS | {_ROOT}
    bad = [
        e for e in _EDGES
        if e.importer_pkg == "protocols" and e.target_pkg not in allowed
    ]
    assert not bad, (
        "protocols 只能依赖 core / protocols（不得 import 通道包、web 或根级编排模块）：\n"
        + _fmt(bad)
    )


def test_web_is_imported_only_by_entrypoints():
    """``web`` 是入口层：除自身与 ``__main__`` 外，任何模块不得 import 它。

    反过来的依赖（web 依赖通道）是设计允许的——管理台要展示全部通道。正方向一旦
    出现，core/通道就会被 HTTP 路由的 import 副作用（注册路由、读全局 app）绑住，
    单测和复用都会开始出问题。
    """
    bad = [
        e for e in _EDGES
        if e.target_pkg == "web" and e.importer_pkg not in {"web", _ROOT}
    ]
    # 根级模块里只有 __main__.py 是入口；其余根级模块（benefits/asyncutil…）不许。
    bad += [
        e for e in _EDGES
        if e.target_pkg == "web" and e.importer_pkg == _ROOT
        and e.importer not in {"__main__.py"}
    ]
    assert not bad, "web 只能被入口（__main__.py）import：\n" + _fmt(bad)


def test_declared_coupling_matches_reality():
    """跨通道 / core→通道的耦合必须与登记表**完全一致**。

    新增一条耦合（尤其 module 级）会让这条红——不是为了禁止，而是逼一次显式决定：
    这份依赖是「同族协议复用」还是「抄近路」，理由补进 ``_DECLARED_COUPLING``。
    删除耦合也会红：过期理由留着，下一个人会以为它还在。
    """
    actual: dict[tuple[str, str, str], list[int]] = {}
    for e in _EDGES:
        cross_channel = (
            e.importer_pkg in _CHANNELS
            and e.target_pkg in _CHANNELS
            and e.importer_pkg != e.target_pkg
            and e.target not in _SHARED_CONTRACT_MODULES
        )
        core_to_channel = (
            e.importer_pkg == "core"
            and e.target_pkg in _CHANNELS
            and e.target not in _SHARED_CONTRACT_MODULES
        )
        if not (cross_channel or core_to_channel):
            continue
        actual.setdefault((e.importer, e.target, e.scope), []).append(e.line)

    undeclared = sorted(set(actual) - set(_DECLARED_COUPLING))
    stale = sorted(set(_DECLARED_COUPLING) - set(actual))
    assert not undeclared, (
        "发现未登记的跨层依赖（确认是有意复用后，在 _DECLARED_COUPLING 补一条理由）：\n"
        + "\n".join(f"  {imp} -> {tgt} ({scope}) 行 {actual[(imp, tgt, scope)]}"
                    for imp, tgt, scope in undeclared)
    )
    assert not stale, (
        "登记表里有已经不存在的耦合（依赖已删除就去掉登记，别留下过期理由）："
        f"{stale}"
    )
