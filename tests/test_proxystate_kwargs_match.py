"""``__main__`` 传给 ``ProxyState`` 的关键字必须都在它的签名里。

历史缺陷（2026-11-10，用户报「buddy restart 没用」）：#225 在 ``__main__`` 里读了
settings 的 ``load_balance`` 并传进 ``ProxyState(load_balance=...)``，但
``core/state.py`` 的 ``__init__`` 当时没有这个形参。于是进程把所有
``[Provider] Enabled`` 都打完、在构造 state 时必崩：

    TypeError: ProxyState.__init__() got an unexpected keyword argument 'load_balance'

8787 上没有监听，``buddy restart`` 怎么敲都没用。而那会儿全量测试是绿的——没有
任何一条把这两处对起来看：构造 ``ProxyState`` 的测试都只传自己关心的那几个参数，
没人按 ``__main__`` 的口径传全套。

本测试只读 AST，不 import ``buddy_proxy.__main__``（它模块级有副作用、还会拉起
通道初始化）。``ProxyState.__init__`` 的签名则用 inspect 取，因为 state 模块
import 干净。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from buddy_proxy.core.state import ProxyState

_MAIN = Path(__file__).resolve().parents[1] / "src" / "buddy_proxy" / "__main__.py"


def _proxystate_call_keywords() -> set[str]:
    """``__main__`` 里 ``ProxyState(...)`` 那次调用的关键字名。

    找不到调用就直接失败——否则这条护栏会在 ``__main__`` 改写法后静默失效。
    """
    tree = ast.parse(_MAIN.read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ProxyState"
    ]
    assert calls, f"{_MAIN} 里找不到 ProxyState(...) 调用，这条护栏需要跟着改"
    return {kw.arg for call in calls for kw in call.keywords if kw.arg is not None}


def test_main_passes_only_accepted_keywords():
    """``__main__`` 不许传 ``ProxyState`` 不认的关键字（启动即 TypeError）。"""
    accepted = set(inspect.signature(ProxyState.__init__).parameters) - {"self"}
    unknown = sorted(_proxystate_call_keywords() - accepted)
    assert not unknown, (
        f"__main__ 传了 ProxyState.__init__ 不认的关键字: {unknown}；"
        "要么在 core/state.py 补上形参，要么把 __main__ 里那几行去掉"
    )
