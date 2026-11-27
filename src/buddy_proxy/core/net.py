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

## 范围：默认直连与按通道覆盖

这个模块的 ``client`` / ``urlopen`` 仍是原有的全局默认直连入口；按通道代理
则通过 ``provider_client`` / ``provider_async_client`` / ``provider_urlopen``
接入。raccoon 保持默认直连，antigravity 的 OAuth、onboarding、转发和配额请求
已接入后者。其它通道未统一改接线，因为「该不该走代理」要看各自上游；需要时
先把该通道的出站调用收口，再配置 ``provider_proxy``。

## 逃生口

如果哪天确实需要代理才能访问上游（换到墙外线路、公司强制 egress 代理），
设 ``BUDDY_UPSTREAM_USE_PROXY=1`` 就恢复 ``trust_env=True`` 的行为。
命名与仓库里其它开关一致，且与 ``trae/pat/net.py`` 的
``BUDDY_PAT_USE_PROXY`` 分开——PAT 那条链路有它自己的历史和默认值。

## 按通道指定代理（2026-11-28）

上面的默认值解决的是「本机有个全局代理，但某些上游直连更快」。反过来的情形
同样存在：**上游在墙外，必须走代理**。antigravity 就是——它打
``oauth2.googleapis.com`` 与 ``cloudcode-pa.googleapis.com``，本机没起代理时
连 TLS 握手都过不去，日志里表现为 token 刷新超时、三个账号一起冷却。

所以这里加了一张**按 provider 的代理表**，落在 ``settings.json`` 的
``provider_proxy``：

    {"provider_proxy": {"antigravity": "http://127.0.0.1:7890"}}

优先级（高 → 低）：

1. ``provider_proxy[<provider>]`` 显式配置 —— 走它指定的代理；
2. ``BUDDY_UPSTREAM_USE_PROXY`` 打开 —— 走环境代理（旧行为）；
3. 都没有 —— 直连。

值支持三种写法：``http://host:port``（``socks5://`` 仅支持 httpx 路径）、
``"direct"`` / ``""``（显式直连，用来覆盖「通道默认想走代理」的历史行为）、
以及 ``"env"``（走环境代理）。表里没有该通道时按第 2/3 步处理。

为什么存在 settings 而不是环境变量：这个开关是**按通道**的，环境变量只能全局
一个值；而且改完重启即生效，不用重新拼 ``buddy start`` 的命令行。
"""

from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import httpx

#: 逃生口的环境变量名。设成非空且非 ``0``/``false``/``no`` 时恢复走环境代理。
PROXY_ENV = "BUDDY_UPSTREAM_USE_PROXY"

#: ``settings.json`` 里「通道 → 代理」映射的键名。
PROVIDER_PROXY_KEY = "provider_proxy"

#: 代理值的三个特殊标记：显式直连 / 走环境代理。
_PROXY_DIRECT = "direct"
_PROXY_ENV_MARK = "env"


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


# ---- 按通道的代理 -----------------------------------------------------------

#: ``settings.json`` 的解析缓存：``(mtime, size) -> {provider: value}``。
#: 每个转发请求都读一次盘太浪费；但也不能永久缓存——用户改完设置重启进程才
#: 生效是可以接受的，可 ``buddy`` 里「改完立刻试一发」的期望也得满足，所以按
#: mtime 失效（文件没变就是一个 stat，比 json.loads 便宜得多）。
_PROVIDER_PROXY_CACHE: tuple[tuple[float, int], dict[str, str]] | None = None


def normalize_proxy_url(raw: Any) -> str:
    """归一化一个代理配置值；非法/空 → ``""``（视为直连）。

    ``127.0.0.1:7890`` 这种缺 scheme 的写法补 ``http://``——用户从别处抄来的
    代理地址十有八九是裸 ``host:port``，要求他补 scheme 只会多一条踩坑记录。
    ``socks5://`` 原样保留（httpx 支持，urllib 不支持，见 :func:`provider_urlopen`）。
    """
    value = str(raw or "").strip()
    if not value:
        return ""
    low = value.lower()
    if low in (_PROXY_DIRECT, "none", "off", "0", "false", "no"):
        return ""
    if low in (_PROXY_ENV_MARK, "environment"):
        return _PROXY_ENV_MARK
    if "://" not in value:
        value = f"http://{value}"
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = bool(parsed.hostname and parsed.port)
    except ValueError:
        return ""
    return value if valid else ""


def provider_proxy_map() -> dict[str, str]:
    """读 ``settings.json`` 的 ``provider_proxy``，返回 ``{provider: 归一后的值}``。

    读失败/文件缺失一律返回空表（直连），**绝不抛异常**：网络配置坏掉时应该
    退化成「和以前一样直连」，而不是让整个进程起不来。
    """
    global _PROVIDER_PROXY_CACHE
    try:
        from buddy_proxy.core import settings as settings_mod

        path = settings_mod.settings_path()
        stat = path.stat()
        key = (stat.st_mtime, stat.st_size)
        if _PROVIDER_PROXY_CACHE is not None and _PROVIDER_PROXY_CACHE[0] == key:
            return _PROVIDER_PROXY_CACHE[1]
        raw = settings_mod.load_settings().get(PROVIDER_PROXY_KEY)
    except Exception:  # noqa: BLE001 - 配置读不出来就当没配，见 docstring
        return {}
    table: dict[str, str] = {}
    if isinstance(raw, dict):
        for provider, value in raw.items():
            normalized = normalize_proxy_url(value)
            if normalized:
                table[str(provider).strip()] = normalized
            elif str(value or "").strip().lower() in (
                    "", _PROXY_DIRECT, "none", "off", "0", "false", "no"):
                # 显式直连要留在表里，才能覆盖全局环境代理；没配置才回退。
                table[str(provider).strip()] = _PROXY_DIRECT
    _PROVIDER_PROXY_CACHE = (key, table)
    return table


def provider_proxy(provider: str) -> str:
    """该通道生效的代理：``""`` = 直连，``"env"`` = 走环境代理，否则是代理 URL。

    优先级见模块 docstring：显式配置 > 环境开关 > 直连。
    """
    configured = provider_proxy_map().get((provider or "").strip())
    if configured == _PROXY_DIRECT:
        return ""
    if configured:
        return configured
    return _PROXY_ENV_MARK if env_wants_proxy() else ""


def provider_client_kwargs(provider: str, **kwargs: Any) -> dict[str, Any]:
    """给 ``httpx`` 的默认参数：按通道决定直连 / 指定代理 / 环境代理。

    ``proxy=`` 与 ``trust_env=True`` 是互斥的两种来源，所以这里显式把
    ``trust_env`` 钉成 False——不钉的话 httpx 会把环境代理**叠加**在显式
    ``proxy`` 之上（``NO_PROXY`` 之类的例外表也会参与判断），行为难以预测。
    """
    target = provider_proxy(provider)
    if target == _PROXY_ENV_MARK:
        kwargs.setdefault("trust_env", True)
    elif target:
        kwargs.setdefault("proxy", target)
        kwargs.setdefault("trust_env", False)
    else:
        kwargs.setdefault("trust_env", False)
    return kwargs


def provider_client(provider: str, **kwargs: Any) -> httpx.Client:
    """同步客户端，按通道决定是否走代理。"""
    return httpx.Client(**provider_client_kwargs(provider, **kwargs))


def provider_async_client(provider: str, **kwargs: Any) -> httpx.AsyncClient:
    """异步客户端，按通道决定是否走代理。"""
    return httpx.AsyncClient(**provider_client_kwargs(provider, **kwargs))


def _opener_for(proxy_url: str) -> urllib.request.OpenerDirector:
    """``proxy_url`` 对应的 urllib opener；空串 = 直连。

    每次调用新建（与 :data:`_DIRECT_OPENER` 共享一个对象不同）：代理配置可能在
    运行期被改，缓存 opener 会让「改完不重启」失效。建 opener 不联网，成本可忽略。
    """
    if not proxy_url:
        return _DIRECT_OPENER
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )


def provider_urlopen(provider: str, request: urllib.request.Request,
                     timeout: float | None = None):
    """``urllib`` 这条路的按通道代理版本，签名与 :func:`urlopen` 一致。

    ``socks5://`` 在这里明确报错：``urllib`` 的 ``ProxyHandler`` 只认 http
    代理。配置了代理就不能静默退回直连，否则必须走代理的上游仍会失败。
    需要 socks 的通道请走 :func:`provider_async_client`（httpx 支持）。
    """
    target = provider_proxy(provider)
    if target == _PROXY_ENV_MARK:
        return urllib.request.urlopen(request, timeout=timeout)
    if target.lower().startswith("socks"):
        raise urllib.error.URLError(
            f"provider {provider!r} 配置了 SOCKS 代理，但 urllib 路径不支持；"
            "请改用 http:// 代理或 httpx 路径"
        )
    return _opener_for(target).open(request, timeout=timeout)


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
