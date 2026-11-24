"""出站请求默认**直连**，不吃环境里的 HTTP(S)_PROXY。

## 为什么需要这个（2026-11-24 实测）

本机 ``urllib.request.getproxies()`` 返回 ``127.0.0.1:7890``（Clash 一类本地
代理），而 ``httpx`` 的 ``trust_env`` 默认是 **True**——于是每个出站请求都被
交给那个代理。

代价不是「偶尔慢一点」，而是**某些域名固定多出 8.6 秒**：

===========  ==========  ==========
路径          耗时        说明
===========  ==========  ==========
直连          0.38s       正常
走代理        8.62s       慢 20 倍
===========  ==========  ==========

关键证据是「同一台机器、同一个域名、同样一个空 GET」：

* ``httpx.Client(trust_env=True)``  → 8.9s
* ``httpx.Client(trust_env=False)`` → 0.14s
* ``curl`` 走同一个代理             → 0.47s

也就是说代理**本身**转发是快的，慢在 httpx 经代理时对特定域名的解析/建连。
受影响的是 ``xiaohuanxiong.com``（raccoon 通道）——它的 CNAME 指向火山 WAF，
TTL 只有 60 秒，缓存几乎不命中，每次都要重新走那条慢路径。

排查时踩过的坑，记下来免得下次重走：

1. **别拿随机域名做对照**。随机域名 NXDOMAIN 本身就慢（9s 左右），会让人误
   以为「所有冷域名都慢」。真实存在且未缓存的 ``example.com`` / ``www.qq.com``
   都只要 0.1–0.6s。
2. **``lsof`` 看不到配置**。``efanapp`` 这类客户端的配置是加密二进制
   （``Core/.config.dat``），``~/.config/clash/*.yaml`` 可能是早年遗留、根本
   没被读取。改那种文件不会有任何效果。
3. **``curl`` 通不代表 httpx 通**。两者走代理的实现不同，必须用同一个客户端
   库对照。

## 为什么是「默认直连」而不是「加例外域名」

服务到上游这条链路不需要代理：这些上游在国内可直连，让它们走代理只会多一个
失败点。代理是给人浏览外网用的，不该插在机器到机器的调用里。

## 范围：目前只接入了 raccoon

这个模块是通用封装，但**本 PR 只把 raccoon 接了过来**。其它通道还有约 20 处
裸 ``httpx.Client(timeout=...)`` / ``AsyncClient(timeout=...)``（qoder 6、
mimo 5、antigravity 3，codex/copilot/kimi/kiro/qwenpc/gemini 各 1），它们同样
是 ``trust_env`` 默认 True。

没有一起改，是因为「该不该走代理」要看各自上游：raccoon 的
``xiaohuanxiong.com`` 实测直连更快且可达，而有的通道上游在墙外、必须走代理。
逐个验证过再改，比统一改一遍更稳。所以别看到这个模块就以为全局已经直连了。

## 逃生口

如果哪天确实需要代理才能访问上游（换到墙外线路、公司强制 egress 代理），
设 ``BUDDY_UPSTREAM_USE_PROXY=1`` 就恢复 ``trust_env=True`` 的行为。
命名与仓库里其它开关一致，且与 ``trae/pat/net.py`` 的
``BUDDY_PAT_USE_PROXY`` 分开——PAT 那条链路有它自己的历史和默认值。
"""

from __future__ import annotations

import os
import urllib.request
from typing import Any

import httpx

#: 逃生口的环境变量名。设成非空且非 ``0``/``false``/``no`` 时恢复走环境代理。
PROXY_ENV = "BUDDY_UPSTREAM_USE_PROXY"


def env_wants_proxy() -> bool:
    """读开关。

    每次调用都读环境（一次 ``os.environ.get`` 很便宜），所以**新造**的客户端
    一定用当前值。但要注意：``trust_env`` 是在构造时固定到 httpx 对象上的，
    已经造好的客户端**不会**因为改了环境变量而切换——``raccoon`` 的
    ``_get_client`` 会缓存实例，所以那里改开关仍需重启进程才生效。

    （``trae/pat/net.py`` 的 ``open_direct`` 能做到「改完立刻生效」，是因为它
    每次调用都新建 opener，没有缓存这一层。两者别混为一谈。）
    """
    value = os.environ.get(PROXY_ENV, "").strip().lower()
    return value not in ("", "0", "false", "no")


def trust_env() -> bool:
    """``httpx`` 的 ``trust_env`` 取值：默认 ``False``（直连）。"""
    return env_wants_proxy()


def client_kwargs(**kwargs: Any) -> dict[str, Any]:
    """给 ``httpx.Client`` / ``AsyncClient`` 的默认参数（调用方可覆盖）。

    用 kwargs 而不是直接造客户端：调用点各有各的 ``timeout``/``follow_redirects``，
    包一层工厂反而要把这些参数再抄一遍。这里只补 ``trust_env`` 这一个默认值。
    """
    kwargs.setdefault("trust_env", trust_env())
    return kwargs


def client(**kwargs: Any) -> httpx.Client:
    """同步客户端，默认直连。"""
    return httpx.Client(**client_kwargs(**kwargs))


def async_client(**kwargs: Any) -> httpx.AsyncClient:
    """异步客户端，默认直连。"""
    return httpx.AsyncClient(**client_kwargs(**kwargs))


def post(url: str, **kwargs: Any) -> httpx.Response:
    """``httpx.post`` 的直连版：一次性请求，用完即关。

    给那些「打一发就走」的管理类调用用（刷新 token、查额度）。每次新建连接
    比复用客户端慢一点点（多一次 TLS 握手，实测 ~0.1s），但换来「不用管
    连接池生命周期」——调用点不用记得 close，也不会因为忘了关而漏连接。
    高频的转发主链路请用 :func:`client` / :func:`async_client` 复用。
    """
    with client(**kwargs.pop("_client_kwargs", {})) as c:
        return c.post(url, **kwargs)


def get(url: str, **kwargs: Any) -> httpx.Response:
    """``httpx.get`` 的直连版，语义同 :func:`post`。"""
    with client(**kwargs.pop("_client_kwargs", {})) as c:
        return c.get(url, **kwargs)


# ---- urllib 这条路 ----------------------------------------------------------

#: 不走任何代理的 opener。``ProxyHandler({})`` 是「代理表为空」，不是「禁用
#: 代理机制」——urllib 见到它就是直连。
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def urlopen(request: urllib.request.Request, timeout: float | None = None):
    """``urllib.request.urlopen`` 的直连版，签名与返回值一致。

    为什么要有这个：仓库里出站请求一半走 httpx、一半走 urllib（2026-11-24
    扫描：90 处裸调用里 40 处是 urllib）。只收口 httpx 的话，另一半照样会把
    请求塞进环境代理——同一个坑，换了个库而已。两条路都得有直连入口。

    与 :func:`client` 不同，这里**每次调用都读环境变量**、不做任何缓存，所以
    改 ``BUDDY_UPSTREAM_USE_PROXY`` 对它立刻生效——opener 是无状态的，新建
    不要钱。
    """
    if env_wants_proxy():
        return urllib.request.urlopen(request, timeout=timeout)
    return _DIRECT_OPENER.open(request, timeout=timeout)
