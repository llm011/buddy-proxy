"""按通道的代理配置（``core/net.py`` 的 ``provider_proxy`` 一族）。

## 这些断言防的是什么退化

2026-11-28：antigravity 打 ``oauth2.googleapis.com`` / ``cloudcode-pa``，本机
代理没起来、环境变量也没了，于是三个账号的 token 刷新同时 SSL EOF / 超时，
日志里只剩「所有账号均不可用」。所以这里钉三件事：

1. **配了就必须真的走代理**——不是「读到了配置」就算数，要看到 httpx 对象上
   的 ``proxy`` 属性、以及 urllib 的 opener 真的挂了 ``ProxyHandler``；
2. **没配时保持直连**——默认行为不能被这次改动悄悄改掉；
3. **antigravity 的出站全部经过封装**——漏一处，那一处就退回直连，
   症状是「有时能通、有时超时」，最难查。
"""

from __future__ import annotations

import ast
import json
import pathlib
import urllib.request

import pytest

from buddy_proxy.core import net

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SRC = _REPO / "src" / "buddy_proxy"


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """把 settings.json 指到临时目录，并清掉按 mtime 的缓存。"""
    path = tmp_path / "settings.json"
    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(path))
    monkeypatch.setattr(net, "_PROVIDER_PROXY_CACHE", None)
    return path


def _write(path: pathlib.Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")


# ---- 归一化 ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("127.0.0.1:7890", "http://127.0.0.1:7890"),
    ("http://127.0.0.1:7890", "http://127.0.0.1:7890"),
    ("socks5://127.0.0.1:7891", "socks5://127.0.0.1:7891"),
    ("env", "env"),
    ("direct", ""),
    ("none", ""),
    ("", ""),
    ("   ", ""),
    ("not a proxy", ""),
    ("127.0.0.1", ""),          # 缺端口：当非法，别静默连到 80
    ("127.0.0.1:not-a-port", ""),
    ("127.0.0.1:99999", ""),
])
def test_normalize_proxy_url(raw, expected):
    assert net.normalize_proxy_url(raw) == expected


# ---- settings 读取 --------------------------------------------------------

def test_provider_proxy_map_reads_settings(settings_file):
    _write(settings_file, '{"provider_proxy": {"antigravity": "127.0.0.1:7890"}}')
    assert net.provider_proxy_map() == {"antigravity": "http://127.0.0.1:7890"}
    assert net.provider_proxy("antigravity") == "http://127.0.0.1:7890"


def test_provider_proxy_map_tolerates_garbage(settings_file):
    """配置写坏时退化成直连，不能让整个进程起不来。"""
    _write(settings_file, '{"provider_proxy": "oops"}')
    assert net.provider_proxy_map() == {}
    assert net.provider_proxy("antigravity") == ""


def test_provider_proxy_map_missing_file(settings_file):
    assert net.provider_proxy_map() == {}


def test_provider_proxy_follows_file_change(settings_file):
    """改完 settings.json，下一次读取就该生效（按 mtime 失效）。"""
    _write(settings_file, '{"provider_proxy": {"antigravity": "127.0.0.1:7890"}}')
    assert net.provider_proxy("antigravity").endswith("7890")
    _write(settings_file, '{"provider_proxy": {"antigravity": "127.0.0.1:7892"}}')
    import os
    st = settings_file.stat()
    os.utime(settings_file, (st.st_atime, st.st_mtime + 5))
    assert net.provider_proxy("antigravity").endswith("7892")


# ---- 优先级 --------------------------------------------------------------

def test_unconfigured_provider_stays_direct(settings_file, monkeypatch):
    monkeypatch.delenv(net.PROXY_ENV, raising=False)
    _write(settings_file, '{"provider_proxy": {"codebuddy": "127.0.0.1:7890"}}')
    assert net.provider_proxy("antigravity") == ""
    assert net.provider_client_kwargs("antigravity")["trust_env"] is False


def test_explicit_config_beats_env_switch(settings_file, monkeypatch):
    """显式配置优先于 BUDDY_UPSTREAM_USE_PROXY。"""
    monkeypatch.setenv(net.PROXY_ENV, "1")
    _write(settings_file, '{"provider_proxy": {"antigravity": "127.0.0.1:7890"}}')
    assert net.provider_proxy("antigravity") == "http://127.0.0.1:7890"


def test_env_switch_applies_when_unconfigured(settings_file, monkeypatch):
    monkeypatch.setenv(net.PROXY_ENV, "1")
    assert net.provider_proxy("antigravity") == "env"
    assert net.provider_client_kwargs("antigravity")["trust_env"] is True


def test_explicit_direct_beats_env_switch(settings_file, monkeypatch):
    monkeypatch.setenv(net.PROXY_ENV, "1")
    _write(settings_file, '{"provider_proxy": {"antigravity": "direct"}}')
    assert net.provider_proxy_map() == {"antigravity": "direct"}
    assert net.provider_proxy("antigravity") == ""
    assert net.provider_client_kwargs("antigravity")["trust_env"] is False


# ---- httpx 这条路 --------------------------------------------------------

def test_provider_client_really_uses_proxy(settings_file):
    """不能只看 kwargs：httpx 对象上的 proxy 必须真的挂上了。"""
    _write(settings_file, '{"provider_proxy": {"antigravity": "127.0.0.1:7890"}}')
    with net.provider_client("antigravity", timeout=5) as c:
        assert c._mounts, "httpx 没有为显式 proxy 装 transport"
        assert c.trust_env is False


def test_provider_async_client_direct_by_default(settings_file):
    import asyncio

    async def _check():
        c = net.provider_async_client("antigravity", timeout=5)
        try:
            assert c.trust_env is False
        finally:
            await c.aclose()

    asyncio.run(_check())


# ---- urllib 这条路 -------------------------------------------------------

def test_provider_urlopen_uses_proxy_handler(settings_file, monkeypatch):
    _write(settings_file, '{"provider_proxy": {"antigravity": "127.0.0.1:7890"}}')
    seen: list[object] = []

    def _fake_build_opener(*handlers):
        seen.extend(handlers)
        opener = urllib.request.OpenerDirector()

        class _R:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        opener.open = lambda req, timeout=None: _R()
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", _fake_build_opener)
    req = urllib.request.Request("https://oauth2.googleapis.com/token")
    net.provider_urlopen("antigravity", req, timeout=3)
    assert seen, "没有挂 ProxyHandler"
    handler = seen[0]
    assert isinstance(handler, urllib.request.ProxyHandler)
    assert handler.proxies.get("https") == "http://127.0.0.1:7890"


def test_provider_urlopen_direct_when_unconfigured(settings_file, monkeypatch):
    monkeypatch.delenv(net.PROXY_ENV, raising=False)
    called: list[str] = []
    monkeypatch.setattr(net._DIRECT_OPENER, "open",
                        lambda r, timeout=None: called.append("direct") or "D")
    req = urllib.request.Request("https://example.invalid/")
    assert net.provider_urlopen("antigravity", req) == "D"
    assert called == ["direct"]


def test_provider_urlopen_socks_degrades_to_direct(settings_file, monkeypatch):
    """urllib 不认 socks：别抛 unknown url type，退化成直连并在文档里说明。"""
    _write(settings_file, '{"provider_proxy": {"antigravity": "socks5://127.0.0.1:7891"}}')
    monkeypatch.setattr(net._DIRECT_OPENER, "open", lambda r, timeout=None: "DIRECT")
    req = urllib.request.Request("https://example.invalid/")
    assert net.provider_urlopen("antigravity", req) == "DIRECT"


# ---- CLI -----------------------------------------------------------------

def test_cli_lists_antigravity_and_preserves_direct(settings_file, monkeypatch, capsys):
    from buddy_proxy.cli.__main__ import main

    assert main(["proxy", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert "antigravity" in listed

    monkeypatch.setenv(net.PROXY_ENV, "1")
    assert main(["proxy", "antigravity", "direct"]) == 0
    capsys.readouterr()
    assert json.loads(settings_file.read_text())["provider_proxy"]["antigravity"] == "direct"
    assert net.provider_proxy("antigravity") == ""

    assert main(["proxy", "antigravity", "--unset"]) == 0
    capsys.readouterr()
    assert json.loads(settings_file.read_text())["provider_proxy"] == {}
    assert net.provider_proxy("antigravity") == "env"


# ---- antigravity 接线守卫 -------------------------------------------------

#: 这条通道的上游在墙外，任何一处裸出站都会退回直连并超时。
_ANTIGRAVITY_DIR = "antigravity"


def _bare_outbound_calls(rel: str, tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            if f.value.id == "httpx" and f.attr in {"Client", "AsyncClient", "get", "post"}:
                found.append(f"{rel}:{node.lineno} httpx.{f.attr}")
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute) \
                and isinstance(f.value.value, ast.Name):
            if f.value.value.id == "urllib" and f.value.attr == "request" and f.attr == "urlopen":
                found.append(f"{rel}:{node.lineno} urllib.request.urlopen")
    return found


def test_antigravity_has_no_bare_outbound_calls():
    """antigravity 的出站必须全部经过 ``core_net.provider_*``。

    漏一处的后果不是报错，而是**那一处静默退回直连**：token 刷新走代理、
    转发不走（或反过来），表现成「有时能通有时超时」，比全挂更难查。
    """
    offenders: list[str] = []
    for path in sorted((_SRC / _ANTIGRAVITY_DIR).rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        offenders.extend(_bare_outbound_calls(rel, tree))
    assert not offenders, (
        "antigravity 里有绕过 core_net 的裸出站调用（上游在墙外，直连必然超时）：\n"
        + "\n".join(offenders)
        + "\n改成 core_net.provider_async_client / provider_urlopen"
    )
