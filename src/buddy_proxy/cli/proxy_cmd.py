"""``buddy proxy``：查看/修改「通道 → 代理」映射（``settings.json`` 的 ``provider_proxy``）。

为什么需要它：某些通道的上游在墙外（antigravity 打
``oauth2.googleapis.com`` / ``cloudcode-pa.googleapis.com``），本机没起全局
代理时连 TLS 握手都过不去，症状是「所有账号均不可用」——但真正的错误埋在
token 刷新里，从 UI 上看不出来。手工编辑 settings.json 也能改，可这个文件
是 0600 + 原子写 + 有 .bak 的，绕过 ``save_settings`` 直接改容易把别的键写坏，
所以给一条正式入口。

用法::

    buddy proxy                          # 列出当前映射（未配置的通道也列出来）
    buddy proxy antigravity 127.0.0.1:7890
    buddy proxy antigravity direct       # 显式直连（覆盖通道默认）
    buddy proxy antigravity env          # 走环境变量里的代理
    buddy proxy antigravity --unset      # 删掉该通道的配置

改完需要 ``buddy restart`` 才生效：httpx 客户端在构造时就把代理钉死了
（见 ``core/net.py`` 的 ``_PROVIDER_PROXY_CACHE`` 注释）。
"""

from __future__ import annotations

import sys
from typing import Any, Optional

from ..core import net as core_net
from ..core import settings as settings_mod


def _known_providers() -> list[str]:
    """设置里出现过的通道 + 项目认得的通道，去重保序。

    前者让「已经配过但不在 KNOWN_PROVIDER_IDS 里」的通道（比如自建通道）也能
    被看到；后者让用户知道还有哪些名字可用，不用去翻源码。
    """
    seen: dict[str, None] = {}
    for provider in core_net.provider_proxy_map():
        seen.setdefault(provider, None)
    for provider in ("antigravity", "gemini", "kiro", "qwenpc", "raccoon"):
        seen.setdefault(provider, None)
    for provider in sorted(settings_mod.KNOWN_PROVIDER_IDS):
        seen.setdefault(provider, None)
    return list(seen)


def _describe(value: str) -> str:
    if not value:
        return "direct（直连）"
    if value == "env":
        return "env（走环境变量代理）"
    return value


def _print_table() -> None:
    table = core_net.provider_proxy_map()
    print(f"设置文件: {settings_mod.settings_path()}")
    print(f"{'通道':<16} {'代理':<28} 生效")
    for provider in _known_providers():
        configured = table.get(provider, "")
        effective = core_net.provider_proxy(provider)
        mark = "*" if configured else " "
        print(f"{mark}{provider:<15} {_describe(configured):<28} {_describe(effective)}")
    print("\n* = 显式配置过；其余按 BUDDY_UPSTREAM_USE_PROXY 环境开关决定（默认直连）")
    print("改完请 `buddy restart` 生效。")


def run(args: Any) -> int:
    provider = (getattr(args, "provider", "") or "").strip()
    value = getattr(args, "value", None)
    unset = bool(getattr(args, "unset", False))
    as_json = bool(getattr(args, "json", False))

    if not provider:
        if as_json:
            import json

            print(json.dumps(
                {p: core_net.provider_proxy(p) for p in _known_providers()},
                ensure_ascii=False, indent=2,
            ))
            return 0
        _print_table()
        return 0

    if unset:
        current = core_net.provider_proxy_map()
        if provider not in current:
            print(f"{provider} 没有显式代理配置，无需删除", file=sys.stderr)
            return 0
        current.pop(provider, None)
        settings_mod.save_settings({core_net.PROVIDER_PROXY_KEY: current})
        print(f"已删除 {provider} 的代理配置（现在: {_describe(core_net.provider_proxy(provider))}）")
        print("重启后生效: buddy restart")
        return 0

    if value is None:
        print(f"{provider}: {_describe(core_net.provider_proxy(provider))}")
        return 0

    normalized = core_net.normalize_proxy_url(value)
    # ``normalize_proxy_url`` 把 direct/none/off 都归一成空串，而「显式直连」
    # 与「没配」在语义上不同：前者要能覆盖环境开关。所以这里按原始输入判断。
    raw = str(value).strip().lower()
    if raw in ("direct", "none", "off", "0", "false", "no"):
        stored = "direct"
    elif raw in ("env", "environment"):
        stored = "env"
    elif not normalized:
        print(f"无法识别的代理地址: {value!r}（示例: 127.0.0.1:7890 / http://127.0.0.1:7890 / "
              "direct / env）", file=sys.stderr)
        return 2
    else:
        stored = normalized

    table = dict(core_net.provider_proxy_map())
    table[provider] = stored
    settings_mod.save_settings({core_net.PROVIDER_PROXY_KEY: table})
    print(f"已设置 {provider} → {_describe(core_net.normalize_proxy_url(stored) or stored)}")
    print("重启后生效: buddy restart")
    return 0


def build_parser(sub: Any) -> None:
    p = sub.add_parser(
        "proxy",
        help="查看/设置按通道的出站代理（settings.json 的 provider_proxy）",
    )
    p.add_argument("provider", nargs="?", default="",
                   help="通道 id（如 antigravity）；省略则列出全部")
    p.add_argument("value", nargs="?", default=None,
                   help="代理地址（127.0.0.1:7890）、direct（直连）或 env（环境变量）")
    p.add_argument("--unset", action="store_true", help="删除该通道的代理配置")
    p.add_argument("--json", action="store_true", help="输出 JSON")
