"""PAT 出站请求统一走**直连**，不吃环境里的 HTTP(S)_PROXY。

为什么需要这个（2026-11-24 实测）：本机 ``urllib.request.getproxies()`` 返回
``http/https/socks -> 127.0.0.1:7890``（Clash 一类本地代理）。而
``urllib.request.urlopen`` **默认就会读这些变量**并把请求交给代理。

表现很有欺骗性：``socket``/TLS 直连 0.12s 通、``curl`` 也通，但 urllib 的请求
一律 30s 超时——因为代理连上了却转发不过去。额度页因此一直显示「PAT 额度
网关不可达」，看起来像上游挂了，实际是本地代理挡的。

为什么是「一律直连」而不是「加例外域名」：PAT 的三个上游
（``copilot-cn.bytedance.net``、``trae-api-cn.mchost.guru``、``api.trae.ai``）
都不需要代理就能访问；反过来，让它们走代理只会引入一个额外的失败点和一层
不确定性。代理是给人浏览外网用的，不该插在服务到上游的这条链路里。

代价：如果哪天真的需要代理才能访问上游（比如换到墙外线路），得在这里显式
加回 ProxyHandler——这是**故意**的，宁可要一个能看懂的断点，也不要一个
「有时候连得上有时候连不上」的隐性依赖。
"""

from __future__ import annotations

import urllib.request

#: 不走任何代理的 opener。``ProxyHandler({})`` 是「代理表为空」，不是「禁用
#: 代理机制」——urllib 见到它就是直连。
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def open_direct(request: urllib.request.Request, timeout: float):
    """取代 ``urllib.request.urlopen``，语义相同但不经代理。

    单列成函数是为了让测试可以 monkeypatch 本模块的这个名字换假响应，不必去
    动全局的 urllib（本仓已有测试在 monkeypatch ``urlopen``，那是更粗的做法）。
    """
    return _DIRECT_OPENER.open(request, timeout=timeout)
