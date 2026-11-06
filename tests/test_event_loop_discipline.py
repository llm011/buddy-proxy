"""事件循环纪律的可执行检查：``async def`` 里不许直接调阻塞 API。

代理是**单进程、单事件循环**的：uvicorn 的 HTTP 请求、SSE 流式转发、管理台轮询
共用同一个 asyncio 循环。任何一个 ``async def`` 里直接调同步 httpx / urllib /
``time.sleep`` / 子进程，都不会报错——表现是"代理活着但全站卡死"，而且要等上游
超时才恢复。这类 bug 在 review 里几乎看不出来：代码在同步函数里完全正确，只有
被 async 调用链碰到才致命。

``docs/architecture.md`` 的结论是「必须 ``to_thread`` / ``wrap_future``」，但纪律
靠人守就一定会漏。本测试把纪律变成 CI 可检查项：

- 扫描 ``src/buddy_proxy/**/*.py`` 的 AST，找**最近的外层函数是 ``async def``**
  的调用点。``async def`` 无论词法上嵌在哪（包括嵌在同步 ``def`` 里、随后被
  ``asyncio.run`` 或注册成 ``on_startup`` 的），协程体都跑在主循环上，一样扫；
  只把**同步 ``def`` 的函数体**排除掉——它是否被 async 直接调用静态看不出来；
- 调用包在 ``to_thread`` / ``run_in_executor`` / ``run_sync`` / ``wrap_future`` 的
  实参里（含里面的 lambda）→ 合法，正是推荐写法；
- 确实需要在 async 里阻塞的少数场景，在该行写 ``# async-ok: <理由>``。理由必填：
  这条注释是给下一个人看的「为什么这里可以卡住循环」。

已知盲区（写在这里，免得把绿灯当成全覆盖）：

- **实例方法**（``client.get(...)``）静态认不出来属于哪个对象，只有模块级函数和
  明确的同步 API 在名单里。httpx 的用法约定是「``AsyncClient`` + ``await``」，
  同步 ``httpx.Client`` 出现在 async 里仍要靠 review；
- **间接调用**：async 里调用一个同步 helper，helper 内部再阻塞，跨函数分析不在
  本检查范围内；
- **线程池耗尽**：``to_thread`` 用得过多会把默认 executor 占满，那是容量问题，
  不是纪律问题。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "buddy_proxy"

#: 阻塞调用 → 为什么它会卡住事件循环（同时也是报错信息的一部分）。
_BLOCKED: dict[str, str] = {
    "time.sleep": "睡的是整个事件循环，所有在途请求一起停摆",
    "requests.get": "requests 是同步 HTTP，等 TCP/TLS 期间循环被占住",
    "requests.post": "同上",
    "requests.put": "同上",
    "requests.patch": "同上",
    "requests.delete": "同上",
    "requests.head": "同上",
    "requests.request": "同上",
    "httpx.get": "同步便捷 API，等价于 httpx.Client(...).get(...)",
    "httpx.post": "同上",
    "httpx.put": "同上",
    "httpx.patch": "同上",
    "httpx.delete": "同上",
    "httpx.request": "同上",
    "urllib.request.urlopen": "同步 socket 读；代理解密 IDE 登录态的路径也走它",
    "socket.create_connection": "同步建连，DNS/TCP 超时会直接冻结循环",
    "subprocess.run": "同步等子进程退出（拉起浏览器、DuMate 桌面端都用它）",
    "subprocess.call": "同上",
    "subprocess.check_call": "同上",
    "subprocess.check_output": "同上",
    "subprocess.Popen": "构造本身要 fork/exec，在 macOS 上可能等几十毫秒",
    "os.system": "同步 shell",
    "os.popen": "同步 shell",
}

#: 把同步调用挪出事件循环的包装器（按调用的最后一个属性名判定，兼容
#: ``asyncio.get_event_loop().run_in_executor`` 和 ``anyio.to_thread.run_sync``）。
_WRAPPERS = frozenset({"to_thread", "run_in_executor", "run_sync", "wrap_future"})

#: 同一行内声明「我知道这是阻塞调用，理由如下」。
_EXEMPT_MARKER = "# async-ok:"


def _has_inline_exemption(line: str) -> bool:
    """当前行是否带了**带理由**的 ``# async-ok:`` 豁免。"""
    _, marker, reason = line.partition(_EXEMPT_MARKER)
    return bool(marker) and bool(reason.strip())


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    call: str
    reason: str


def _call_name(node: ast.Call) -> str:
    """``a.b.c(...)`` → ``"a.b.c"``；无法静态确定时返回空串。"""
    parts: list[str] = []
    func = node.func
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    else:
        return ""
    return ".".join(reversed(parts))


def _exempt_call_ids(tree: ast.AST) -> set[int]:
    """被 ``to_thread`` / ``run_in_executor`` 等包装的调用节点 id。

    只看实参：包装器**参数里的**调用会被挪到别的线程，而包装器本身的调用不受影响。
    """
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name.rsplit(".", 1)[-1] not in _WRAPPERS:
            continue
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Call):
                    exempt.add(id(sub))
    return exempt


def _scan(source: str, filename: str) -> list[Violation]:
    """扫描一段源码，返回 ``async def`` 里的阻塞调用。

    ``filename`` 只用于报错信息；``__init__.py`` 之外的路径按行号可定位。
    """
    tree = ast.parse(source, filename=filename)
    lines = source.splitlines()
    exempt = _exempt_call_ids(tree)
    found: list[Violation] = []

    def walk(node: ast.AST, stack: tuple[bool, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                # async def 嵌在同步 def 里时也照样被 ``asyncio.run`` / 注册到
                # ``on_startup`` 之类的地方跑在主循环上（``__main__`` 的
                # ``_install_qoder_model_warmup``、doubao/mimo 的 ``_ready`` /
                # ``_fetch`` 都是这个形状）。协程体在哪个循环里执行与词法嵌套
                # 无关，所以这里新起一段全 true 的上下文，而不是继承外层。
                walk(child, (True,))
            elif isinstance(child, ast.FunctionDef):
                # 同步函数：它是否被 async 直接调用静态看不出来，不在这里判。
                walk(child, stack + (False,))
            elif isinstance(child, ast.Lambda):
                # lambda 不引入新的执行上下文（它常被塞进 to_thread 的实参）。
                walk(child, stack)
            else:
                if (
                    isinstance(child, ast.Call)
                    and stack
                    and all(stack)
                    and id(child) not in exempt
                ):
                    name = _call_name(child)
                    reason = _BLOCKED.get(name)
                    if reason is not None:
                        lineno = child.lineno
                        line = lines[lineno - 1] if lineno <= len(lines) else ""
                        if not _has_inline_exemption(line):
                            found.append(Violation(filename, lineno, name, reason))
                walk(child, stack)

    walk(tree, ())
    return found


def _scan_tree() -> list[Violation]:
    violations: list[Violation] = []
    for path in sorted(_SRC.rglob("*.py")):
        violations.extend(_scan(path.read_text(encoding="utf-8"), str(path)))
    return violations


def test_no_blocking_calls_in_async_functions():
    """``src`` 里没有「``async def`` 直接调阻塞 API」的调用点。"""
    violations = _scan_tree()
    detail = "\n".join(
        f"  {v.path}:{v.line} {v.call}() —— {v.reason}" for v in violations
    )
    assert not violations, (
        "async 函数里直接调了同步阻塞 API；改成 `await asyncio.to_thread(...)` / "
        "`await loop.run_in_executor(...)`，确实必须阻塞时在同行写 "
        f"`{_EXEMPT_MARKER} <理由>`：\n{detail}"
    )


def test_checker_catches_blocking_call_in_async_function():
    """检查器本身有效：async 里的同步调用会被抓出来。"""
    bad = """
import time

async def handler():
    time.sleep(1)
    return await fetch()
"""
    assert [v.call for v in _scan(bad, "sample.py")] == ["time.sleep"]


def test_checker_catches_blocking_call_inside_async_comprehension():
    """嵌套在 async 函数里的推导式/with 也在同一执行上下文内。"""
    bad = """
import urllib.request

async def handler(urls):
    with urllib.request.urlopen("https://example.com") as resp:
        return [line for line in resp]
"""
    assert [v.call for v in _scan(bad, "sample.py")] == ["urllib.request.urlopen"]


def test_checker_ignores_properly_wrapped_calls():
    """``to_thread`` / ``run_in_executor`` 包住的同步调用是推荐写法，必须放过。"""
    good = """
import asyncio
import time

async def handler():
    await asyncio.to_thread(time.sleep, 1)
    await asyncio.to_thread(lambda: time.sleep(1))
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, time.sleep, 1)
"""
    assert _scan(good, "sample.py") == []


def test_checker_ignores_synchronous_functions():
    """普通同步函数里的阻塞调用不是事件循环问题（例如启动前的探测）。"""
    good = """
import time

def wait():
    time.sleep(1)
"""
    assert _scan(good, "sample.py") == []


def test_checker_catches_blocking_call_in_async_nested_in_sync_function():
    """嵌在同步函数里的 ``async def`` 一样跑在主循环上（``asyncio.run`` /
    ``on_startup``），不能因为词法嵌套就漏掉。"""
    bad = """
import time

def make_handler():
    async def handler():
        time.sleep(1)
    return handler
"""
    assert [v.call for v in _scan(bad, "sample.py")] == ["time.sleep"]


def test_checker_requires_reason_for_inline_exemption():
    """``# async-ok:`` 豁免可以写，但必须真的在**同一行**、带上理由。"""
    marked = """
import time

async def handler():
    time.sleep(0.01)  # async-ok: 只睡 10ms，构造客户端前的退避，代价小于线程切换
"""
    assert _scan(marked, "sample.py") == []

    wrong_line = """
import time

async def handler():
    # async-ok: 理由写在上一行不算
    time.sleep(1)
"""
    assert [v.call for v in _scan(wrong_line, "sample.py")] == ["time.sleep"]

    empty_reason = """
import time

async def handler():
    time.sleep(1)  # async-ok:
"""
    assert [v.call for v in _scan(empty_reason, "sample.py")] == ["time.sleep"]

