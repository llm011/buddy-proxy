"""``core/net`` 的出站直连封装。

## 这些断言防的是什么退化

2026-11-24 实测：本机 ``getproxies()`` 返回 ``127.0.0.1:7890``，而 ``httpx``
的 ``trust_env`` 默认是 True，于是 raccoon 每个请求都被塞进那个代理，对
``xiaohuanxiong.com`` 固定多出 8.6 秒（直连 0.38s vs 走代理 8.62s）。

所以这里锁两件事：

1. **默认必须直连**。哪天有人把 ``trust_env`` 的默认值改回 True，或者调用点
   绕过 ``core_net.client`` 直接用 ``httpx.Client``，延迟会静默涨 20 倍——
   而症状只是「慢」，没有报错，很容易被当成上游问题查很久。
2. **逃生口必须可用**。直连在某些网络环境下会让上游不可达，那时要能一键
   切回代理，而不是去改代码。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from buddy_proxy.core import net

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SRC = _REPO / "src" / "buddy_proxy"


def test_trust_env_defaults_to_false(monkeypatch):
    """默认直连：没有环境变量时必须 trust_env=False。"""
    monkeypatch.delenv(net.PROXY_ENV, raising=False)
    assert net.trust_env() is False
    assert net.client_kwargs()["trust_env"] is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "anything"])
def test_escape_hatch_turns_proxy_on(monkeypatch, value):
    """设了非空且非假值 → 恢复走环境代理。"""
    monkeypatch.setenv(net.PROXY_ENV, value)
    assert net.trust_env() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "  ", "False"])
def test_falsey_values_keep_direct(monkeypatch, value):
    """``0``/``false``/``no``/空串都算「关」，别让 ``"0"`` 意外打开代理。"""
    monkeypatch.setenv(net.PROXY_ENV, value)
    assert net.trust_env() is False


def test_caller_can_override_trust_env():
    """调用方显式传的值优先——封装只补默认值，不强行覆盖。"""
    assert net.client_kwargs(trust_env=True)["trust_env"] is True


def test_client_and_async_client_are_direct_by_default(monkeypatch):
    """真造两个客户端，确认 trust_env 真的落到了 httpx 对象上。

    只测 ``client_kwargs`` 不够：工厂函数可能忘了把 kwargs 传下去。
    """
    monkeypatch.delenv(net.PROXY_ENV, raising=False)
    with net.client(timeout=5) as c:
        assert c.trust_env is False
    import asyncio

    async def _check():
        c = net.async_client(timeout=5)
        try:
            assert c.trust_env is False
        finally:
            await c.aclose()

    asyncio.run(_check())


#: 允许直接用 httpx 客户端的文件（白名单）。加进来要有理由，别图省事。
#: 目前为空：raccoon 那 5 处已全部改走 core_net。
_HTTPX_CLIENT_ALLOWLIST: frozenset[str] = frozenset()


def test_raccoon_constructs_no_bare_httpx_client():
    """**raccoon 通道**不许再出现裸 ``httpx.Client(...)`` / ``AsyncClient(...)``。

    范围就是 raccoon，不是全仓：其它通道（qoder 6、mimo 5、antigravity 3……）
    还有 20 处裸客户端，它们走不走代理取决于各自上游，本 PR 没有评估，所以
    这里也不替它们下结论。名字带 ``raccoon`` 就是为了别让人误以为全局已覆盖。

    这条比「测某个函数返回值」更能拦住退化：只要有人新写一处裸客户端，
    那处就绕过了直连默认值，延迟静默涨回去。用 AST 找调用而不是正则——
    正则会把注释和字符串里的 ``httpx.Client`` 也算进去。
    """
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel in _HTTPX_CLIENT_ALLOWLIST:
            continue
        # 只扫 raccoon：其它通道的上游大多需要代理，本轮不扩大范围。
        if not rel.startswith("raccoon/"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 语法错的文件轮不到这里管
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if not isinstance(func.value, ast.Name):
                continue
            if func.value.id != "httpx":
                continue
            if func.attr in ("Client", "AsyncClient"):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "raccoon 里有裸 httpx 客户端，会绕过直连默认值（每个请求多 8.6s）：\n"
        + "\n".join(offenders)
        + "\n改成 core_net.client(...) / core_net.async_client(...)"
    )


def test_raccoon_imports_core_net():
    """两个出站文件都真的 import 了封装——防止有人只改了一处。"""
    for name in ("raccoon/provider.py", "raccoon/login.py"):
        text = (_SRC / name).read_text(encoding="utf-8")
        assert "from ..core import net as core_net" in text, f"{name} 没 import core_net"


def test_cached_client_does_not_follow_env_change(monkeypatch):
    """已造好的客户端不跟随环境变量变化——这是 httpx 的语义，不是 bug。

    钉住它是因为容易误会：``open_direct`` 那种「每次新建 opener」的封装确实
    改完立刻生效，但 ``trust_env`` 是构造时固定到对象上的。``raccoon`` 的
    ``_get_client`` 缓存实例，所以改开关要重启进程。

    这条测试同时是文档：哪天有人想「让缓存客户端也跟随开关」，会先看到这里
    说明为什么不能，而不是直接踩坑。
    """
    monkeypatch.delenv(net.PROXY_ENV, raising=False)
    c = net.client(timeout=5)
    try:
        assert c.trust_env is False
        monkeypatch.setenv(net.PROXY_ENV, "1")
        # 同一个对象不会变——httpx 把 trust_env 固化在构造期
        assert c.trust_env is False
        # 但新造的一定用当前值
        with net.client(timeout=5) as fresh:
            assert fresh.trust_env is True
    finally:
        c.close()

