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


#: 受「环境代理固定多 8.6s」影响的目录 → 它们打的上游域名。
#:
#: 2026-11-24 把仓库里 32 个上游域名逐个实测（直连 vs 走本机代理），**只有
#: 两个域名**走代理会固定慢 8.6s：``copilot.tencent.com`` / ``www.codebuddy.ai``
#: （codebuddy）与 ``xiaohuanxiong.com``（raccoon）。其余 30 个（google /
#: openai / github / qoder / kimi / trae …）走代理都正常，所以**不**在这里——
#: 改它们没有收益，只有回归风险。
#:
#: 新通道接进来前先测，别凭感觉加：探测时 ``api3.qoder.sh`` 直连报过一次
#: ConnectError，看着像「必须走代理」，复测 2/2 通，是并发探测的瞬时抖动。
_SLOW_VIA_PROXY_DIRS: dict[str, str] = {
    "raccoon/": "xiaohuanxiong.com",
    "codebuddy_provider/": "copilot.tencent.com / www.codebuddy.ai",
}

#: ``httpx.<attr>`` 里会自建客户端的快捷函数（``trust_env`` 默认 True）。
_HTTPX_CLIENT_CTORS = {"Client", "AsyncClient"}
_HTTPX_SHORTCUTS = {"post", "get", "put", "delete", "patch", "head", "options",
                    "request", "stream"}


def _bare_outbound_calls(rel: str, tree: ast.AST) -> list[str]:
    """找出 ``rel`` 里绕过 ``core/net`` 的裸出站调用（文件:行 形式）。

    用 AST 而不是正则：正则会把注释和字符串里的 ``httpx.Client`` 也算进去。
    只认 ``httpx.xxx(...)`` / ``urllib.request.urlopen(...)`` /
    ``urlopen(...)`` 三种**直接**调用形态——经变量转手的（``c = httpx.Client``）
    这里不管，那种写法本仓库没有，真出现了 review 时该拦。
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            if f.value.id == "httpx" and f.attr in (_HTTPX_CLIENT_CTORS | _HTTPX_SHORTCUTS):
                found.append(f"{rel}:{node.lineno} httpx.{f.attr}")
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute) \
                and isinstance(f.value.value, ast.Name):
            if f.value.value.id == "urllib" and f.value.attr == "request" and f.attr == "urlopen":
                found.append(f"{rel}:{node.lineno} urllib.request.urlopen")
        elif isinstance(f, ast.Name) and f.id == "urlopen":
            found.append(f"{rel}:{node.lineno} urlopen")
    return found


@pytest.mark.parametrize("dirname", sorted(_SLOW_VIA_PROXY_DIRS))
def test_no_bare_outbound_call_in_slow_host_dirs(dirname: str):
    """打了「走代理会慢 8.6s」域名的目录，出站必须全部经过 ``core/net``。

    这条比「测某个函数返回值」更能拦住退化：只要有人新写一处裸客户端，那处
    就绕过了直连默认值，延迟静默涨回去——而且**没有任何单元测试会红**
    （codebuddy 的测试都 patch 在更上层的入口，``pipeline.py`` /
    ``client.py`` 里的出站调用本来就没有直接覆盖）。

    为什么同时管 urllib：仓库里出站一半 httpx、一半 urllib（扫描 90 处裸
    调用，40 处是 urllib）。只收口 httpx，另一半照样把请求塞进代理。
    """
    offenders: list[str] = []
    for path in sorted((_SRC / dirname.rstrip("/")).rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel in _HTTPX_CLIENT_ALLOWLIST:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        offenders.extend(_bare_outbound_calls(rel, tree))
    assert not offenders, (
        f"{dirname} 里有绕过 core_net 的裸出站调用（上游 {_SLOW_VIA_PROXY_DIRS[dirname]} "
        "走环境代理每发固定多 8.6s）：\n" + "\n".join(offenders)
        + "\n改成 core_net.client / async_client / post / get / urlopen"
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



# ---- urllib 这条路 ----------------------------------------------------------

def test_urlopen_uses_direct_opener_by_default(monkeypatch):
    """``core_net.urlopen`` 默认走无代理 opener，不碰 ``urllib.request.urlopen``。

    守卫（test_no_bare_outbound_call_in_slow_host_dirs）只管「有没有绕过
    core_net」，不管 core_net 自己有没有真的直连。这条补上：opener 被换掉
    的话，codebuddy 的 ``client.py``（模型转发 + 登录）会静默回到走代理。
    """
    import urllib.request

    monkeypatch.delenv(net.PROXY_ENV, raising=False)
    seen: list[object] = []

    def _direct_open(request, timeout=None):
        seen.append(("direct", request, timeout))
        return "DIRECT"

    def _plain_urlopen(request, timeout=None):
        seen.append(("plain", request, timeout))
        return "PLAIN"

    monkeypatch.setattr(net._DIRECT_OPENER, "open", _direct_open)
    monkeypatch.setattr(urllib.request, "urlopen", _plain_urlopen)

    req = urllib.request.Request("https://example.invalid/", method="GET")
    assert net.urlopen(req, timeout=7) == "DIRECT"
    assert seen == [("direct", req, 7)], "默认必须走直连 opener，并把 timeout 原样传下去"


def test_urlopen_escape_hatch_falls_back_to_plain_urlopen(monkeypatch):
    """设了 ``BUDDY_UPSTREAM_USE_PROXY`` → 回到读环境代理的 ``urlopen``。"""
    import urllib.request

    monkeypatch.setenv(net.PROXY_ENV, "1")
    seen: list[str] = []

    def _direct_open(request, timeout=None):
        seen.append("direct")
        return "DIRECT"

    def _plain_urlopen(request, timeout=None):
        seen.append("plain")
        return "PLAIN"

    monkeypatch.setattr(net._DIRECT_OPENER, "open", _direct_open)
    monkeypatch.setattr(urllib.request, "urlopen", _plain_urlopen)

    assert net.urlopen(urllib.request.Request("https://example.invalid/")) == "PLAIN"
    assert seen == ["plain"]


def test_urlopen_reads_env_every_call(monkeypatch):
    """urllib 这条路每次调用都读环境变量——和 httpx 那条路不同。

    httpx 的 ``trust_env`` 构造时固定到实例上（见
    test_cached_client_does_not_follow_env_change）；opener 是无状态的，
    所以这里**能**做到改完立刻生效。两条路语义不同是刻意的，钉住免得有人
    为了「统一」把它们改成一样。
    """
    import urllib.request

    monkeypatch.setattr(net._DIRECT_OPENER, "open", lambda r, timeout=None: "DIRECT")
    monkeypatch.setattr(urllib.request, "urlopen", lambda r, timeout=None: "PLAIN")
    req = urllib.request.Request("https://example.invalid/")

    monkeypatch.setenv(net.PROXY_ENV, "")
    assert net.urlopen(req) == "DIRECT"
    monkeypatch.setenv(net.PROXY_ENV, "1")
    assert net.urlopen(req) == "PLAIN", "同一个 opener 对象，改完环境变量下一发就该变"
