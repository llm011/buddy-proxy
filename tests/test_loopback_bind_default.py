"""锁住「默认只监听回环」这条安全约定。

`/v1/*` 模型代理端点**没有任何鉴权**（谁连上谁就能白嫖订阅额度），所以
`proxy.sh` / `buddy` 的默认 HOST 必须是 `127.0.0.1`。历史上这两个脚本默认
`0.0.0.0`，装成 launchd 服务后整条局域网都能用你的账号跑模型——用户
2026-11-23 实报「局域网现在可访问，不太安全」。

这条测试直接读脚本源码，锁的是「默认值字面量」：改成 0.0.0.0 会当场红。
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
PROXY_SH = REPO / "proxy.sh"
BUDDY = REPO / "buddy"

# `PROXY_HOST="${PROXY_HOST:-127.0.0.1}"`，允许引号/空格差异
_DEFAULT_HOST_RE = re.compile(
    r'^PROXY_HOST="\$\{PROXY_HOST:-([^}]+)\}"', re.MULTILINE
)


def _default_host(path: pathlib.Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = _DEFAULT_HOST_RE.search(text)
    assert m, f"{path.name} 里找不到 PROXY_HOST 默认值定义"
    return m.group(1).strip()


def test_proxy_sh_defaults_to_loopback():
    assert _default_host(PROXY_SH) == "127.0.0.1", (
        "proxy.sh 的默认 HOST 必须是 127.0.0.1：/v1/* 无鉴权，"
        "绑 0.0.0.0 等于把订阅额度开放给整个局域网"
    )


def test_buddy_defaults_to_loopback():
    assert _default_host(BUDDY) == "127.0.0.1", (
        "buddy 的默认 HOST 必须是 127.0.0.1（service install 会把它写进 plist）"
    )


def test_python_entrypoint_defaults_to_loopback():
    """`python -m buddy_proxy` 的 --host 默认也必须是回环。"""
    text = (REPO / "src" / "buddy_proxy" / "__main__.py").read_text(encoding="utf-8")
    m = re.search(r'add_argument\(\s*"--host"\s*,\s*default=[^)]*\)[^,]*', text)
    assert m, "__main__.py 里找不到 --host 参数定义"
    assert '"127.0.0.1"' in m.group(0), (
        "python -m buddy_proxy 的 --host 默认必须是 127.0.0.1"
    )


def test_service_plist_template_is_loopback():
    """deploy/ 里的 launchd 模板同样只能绑回环。"""
    text = (REPO / "deploy" / "com.buddy-proxy.plist").read_text(encoding="utf-8")
    m = re.search(r"<string>--host</string>\s*<string>([^<]+)</string>", text)
    assert m, "plist 模板里找不到 --host 参数"
    assert m.group(1).strip() == "127.0.0.1", "plist 模板的 --host 必须是 127.0.0.1"
