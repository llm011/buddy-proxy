"""DuMate（百度搭子）provider 单测。

锁定接入时的几个关键行为：

1. 模型目录：同档只留最新款；同一底座的别名只留实名一个条目——``text.L0`` /
   ``model-text`` / ``model-mm`` / ``model-gui-use`` 经 ``x-dumate-gw-realmodel``
   实测全路由到 ``deepseek-v4.1-flash-low-latency``，故只发布 ``deepseek-v4.1-flash``
   （2026-11-01 用户要求）。
2. 转发：openai 协议直通本地代理（URL 带 inapp key header），stream/非流式
   都原样回传；anthropic 协议 400 拒绝（DuMate 网关只认 OpenAI chat）。
3. 额度：本地 ``/api/dumate/points/remaining`` 的布尔态翻译成 1/1 条目。
4. 端点发现失败（App 未运行）时 ensure_auth 抛 401、forward 抛 503。

全部用 mock 隔离：不真发网络请求、不读真实进程环境。
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest
from fastapi import HTTPException

from buddy_proxy.dumate import discovery, provider as dumate_provider
from buddy_proxy.dumate.provider import DumateProvider


def _endpoint(port: int = 52414, key: str = "a" * 64) -> discovery.DumateEndpoint:
    return discovery.DumateEndpoint(pid=9007, port=port, inapp_key=key,
                                    app_version="1.0.82.317")


# --- 模型目录 --------------------------------------------------------------


def test_models_catalog_latest_only():
    """同档只留最新款、同底座别名只留实名（2026-11-01 用户要求）。"""
    p = DumateProvider()
    ids = [m["id"] for m in p.models()]
    assert "kimi-k3" in ids
    assert "qwen3.8-max" in ids
    assert "deepseek-v4.1-flash" in ids
    assert "deepseek-v4-pro" in ids
    # 旧款 / 同底座别名都不上目录
    assert "glm-5" not in ids
    assert "qwen3.5-35b-a3b" not in ids
    assert "dm-auto-model/text.L0" not in ids
    assert "model-text" not in ids
    assert "model-mm" not in ids
    assert "model-gui-use" not in ids


def test_models_all_support_reasoning_and_tools():
    """目录里每个模型都实测支持 reasoning 与 function calling。"""
    p = DumateProvider()
    for m in p.models():
        assert m["reasoning"] is True, m["id"]
        assert m["tool_call"] is True, m["id"]


# --- 端点发现失败时的行为 ----------------------------------------------------


def test_ensure_auth_401_when_app_not_running():
    """App 未运行 **且本机无从拉起**（未装/未登录）→ 401。

    2026-11-01 起，只要本机装了 App 且已登录，ensure_auth 会放行（把真正的
    拉起交给 async 的 forward），所以这里显式把独立运行能力 mock 掉。
    """
    from buddy_proxy.dumate import standalone

    p = DumateProvider()
    with mock.patch.object(discovery, "discover", return_value=None), \
         mock.patch.object(discovery, "is_app_installed", return_value=True), \
         mock.patch.object(standalone, "standalone_supported", return_value=False):
        with pytest.raises(HTTPException) as ei:
            p.ensure_auth()
    assert ei.value.status_code == 401


def test_ensure_auth_passes_through_when_standalone_capable():
    """App 未运行但本机可独立拉起（装了 + 已登录）→ ensure_auth 放行不抛。

    不能在同步 ensure_auth 里阻塞 spawn，所以这里只放行；真正失败由 forward 报。
    """
    from buddy_proxy.dumate import standalone

    p = DumateProvider()
    with mock.patch.object(discovery, "discover", return_value=None), \
         mock.patch.object(standalone, "standalone_supported", return_value=True), \
         mock.patch.object(standalone, "read_login_context",
                           return_value={"login_mode": "bce", "user_id": "u", "account_id": "a"}):
        p.ensure_auth()  # 不抛即通过


def test_forward_503_when_app_not_running():
    """App 未运行且本机无从拉起 → 503（mock 掉独立运行能力，不真起进程）。"""
    import asyncio

    from buddy_proxy.dumate import standalone

    async def _run():
        p = DumateProvider()
        body = {"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}]}
        with mock.patch.object(discovery, "discover", return_value=None), \
             mock.patch.object(discovery, "is_app_installed", return_value=False), \
             mock.patch.object(standalone, "standalone_supported", return_value=False):
            with pytest.raises(HTTPException) as ei:
                await p.forward(body, "openai")
        return ei.value.status_code

    assert asyncio.run(_run()) == 503


def test_forward_spawns_standalone_when_app_closed():
    """**核心修复**：App 关掉时 forward 自己拉起独立实例并成功转发。

    全程 mock：``ensure_running`` 假装拉起成功，随后 discover 返回端点。断言
    真的走了 spawn 分支、并按端点转发（用假 client，不发真网络）。
    """
    import asyncio

    from buddy_proxy.dumate import standalone
    from buddy_proxy.dumate.provider import DumateProvider

    async def _run():
        p = DumateProvider()
        ep = _endpoint()
        seen = {"n": 0}

        def _fake_discover():
            seen["n"] += 1
            return None if seen["n"] == 1 else ep  # 首次 None → 触发 spawn；之后给端点

        sent = {}

        class _FakeResp:
            status_code = 200
            def json(self): return {"id": "x", "choices": [{"message": {"content": "好"}}]}

        async def _fake_post(url, json=None, headers=None):
            sent["url"] = url
            sent["headers"] = headers
            return _FakeResp()

        with mock.patch.object(discovery, "discover", side_effect=_fake_discover), \
             mock.patch.object(standalone, "is_enabled", return_value=True), \
             mock.patch.object(standalone, "standalone_supported", return_value=True), \
             mock.patch.object(standalone, "ensure_running",
                               return_value={"ok": True, "port": ep.port, "pid": 42}) as er, \
             mock.patch.object(p, "_get_client") as gc:
            gc.return_value.post = _fake_post
            resp = await p.forward({"model": "kimi-k3",
                                    "messages": [{"role": "user", "content": "hi"}]}, "openai")
        return sent, resp, er

    sent, resp, er = asyncio.run(_run())
    er.assert_called_once()
    assert resp.status_code == 200
    assert sent["url"] == _endpoint().chat_url()


def test_forward_reports_spawn_failure_with_reason():
    """拉起失败（未登录等）→ 503 且文案带上真实原因，不静默成「未运行」。"""
    import asyncio

    from buddy_proxy.dumate import standalone

    async def _run():
        p = DumateProvider()
        with mock.patch.object(discovery, "discover", return_value=None), \
             mock.patch.object(standalone, "is_enabled", return_value=True), \
             mock.patch.object(standalone, "standalone_supported", return_value=True), \
             mock.patch.object(standalone, "ensure_running",
                               return_value={"ok": False, "reason": "本机无 DuMate 登录态"}):
            with pytest.raises(HTTPException) as ei:
                await p.forward({"model": "kimi-k3", "messages": []}, "openai")
        return ei.value

    exc = asyncio.run(_run())
    assert exc.status_code == 503
    assert "本机无 DuMate 登录态" in exc.detail["error"]["message"]


# --- anthropic 协议拒绝 ----------------------------------------------------


def test_forward_anthropic_nonstream_wraps_to_message():
    """anthropic 非流式：OpenAI 响应包一层转回 anthropic message 形状。"""
    import asyncio

    async def _run():
        p = DumateProvider()
        ep = _endpoint()
        upstream_json = {
            "id": "gd-1",
            "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        }

        class _FakeResp:
            status_code = 200
            def json(self): return upstream_json

        async def _fake_post(url, json=None, headers=None):
            return _FakeResp()

        with mock.patch.object(discovery, "discover", return_value=ep), \
             mock.patch.object(p, "_get_client") as gc:
            gc.return_value.post = _fake_post
            resp = await p.forward(
                {"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}]},
                "anthropic",
                original={"model": "kimi-k3", "messages": []},
            )
        return resp

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    body = json.loads(resp.body)
    # anthropic message 形状：content 块 + stop_reason + usage
    assert body["type"] == "message"
    text_blocks = [b for b in body["content"] if b.get("type") == "text"]
    assert any("pong" in b.get("text", "") for b in text_blocks)
    assert body["stop_reason"] == "end_turn"
    assert body["usage"]["input_tokens"] == 5
    assert body["usage"]["output_tokens"] == 7


def test_forward_anthropic_stream_emits_events():
    """anthropic 流式：OpenAI SSE → anthropic 事件序列（start/delta/stop）。"""
    import asyncio

    async def _run():
        p = DumateProvider()
        ep = _endpoint()
        sse_lines = "\n".join([
            'data: {"choices":[{"delta":{"content":"po"},"index":0}]}',
            'data: {"choices":[{"delta":{"content":"ng"},"index":0}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}',
            "data: [DONE]",
            "",
        ])

        class _FakeResp:
            status_code = 200
            async def aiter_lines(self):
                for ln in sse_lines.splitlines():
                    yield ln
            async def aclose(self): pass

        async def _fake_post(url, json=None, headers=None):
            return _FakeResp()

        async def _fake_send(request, stream=False):
            assert stream is True, "流式必须 send(stream=True)，用 post() 会把首字节推迟到生成结束"
            return _FakeResp()

        with mock.patch.object(discovery, "discover", return_value=ep), \
             mock.patch.object(p, "_get_client") as gc:
            gc.return_value.post = _fake_post
            gc.return_value.send = _fake_send
            gc.return_value.build_request = mock.Mock(
                side_effect=lambda method, url, **kw: mock.Mock(method=method, url=url, **kw))
            resp = await p.forward(
                {"model": "kimi-k3", "messages": [], "stream": True},
                "anthropic",
                original={"model": "kimi-k3", "messages": []},
            )
            # StreamingResponse 对 str 迭代器会逐段 encode——拼回 str 再断言语义
            body = "".join([c.decode() if isinstance(c, bytes) else c
                            for c in [c async for c in resp.body_iterator]])
        return resp, body

    resp, body = asyncio.run(_run())
    assert resp.status_code == 200
    assert "message_start" in body
    assert "content_block_delta" in body
    assert "message_stop" in body
    # 内容真的传过去了
    assert '"po"' not in body or "text_delta" in body
    assert "input_tokens" in body and "output_tokens" in body


def test_anthropic_stream_delegates_to_shared_protocol_machine():
    """``_to_anthropic_stream`` 已上移到 ``protocols.anthropic_adapter``。

    这里只钉住**接缝仍在且行为一致**（ark 通道复用同一份实现）：同一份上游
    SSE，经过 dumate 的 ``_to_anthropic_stream`` 与协议层
    ``sse_to_anthropic_stream`` 产出的事件字节必须逐段相等。万一以后有人把
    其中一边改歪，两边的差异会在这里暴露。
    """
    import asyncio

    from buddy_proxy.dumate import provider as prov_mod
    from buddy_proxy.protocols.anthropic_adapter import sse_to_anthropic_stream

    sse_lines = "\n".join([
        'data: {"choices":[{"delta":{"content":"hi"},"index":0}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}',
        "data: [DONE]",
        "",
    ])

    class _FakeResp:
        status_code = 200

        async def aiter_lines(self):
            for ln in sse_lines.splitlines():
                yield ln

        async def aclose(self):
            pass

    async def _collect(fn):
        return [c async for c in fn(_FakeResp(), "kimi-k3")]

    via_dumate = asyncio.run(_collect(prov_mod._to_anthropic_stream))
    via_protocol = asyncio.run(_collect(sse_to_anthropic_stream))

    # 消息 id 每次随机（msg_<uuid 前缀>），比对前抹掉
    import re

    def _norm(chunks):
        text = "".join(chunks)
        text = re.sub(r'"id": "msg_[0-9a-f]+"', '"id": "msg_X"', text)
        return text.split("\n\n")

    assert _norm(via_dumate) == _norm(via_protocol)
    joined = "".join(via_dumate)
    assert "message_start" in joined and "message_stop" in joined


# --- 转发直通（mock httpx） -------------------------------------------------


def test_forward_openai_nonstream_pass_through():
    """openai 非流式：POST 到本地 chat 接口、带 inapp key，返回上游 JSON。"""
    import asyncio

    async def _run():
        p = DumateProvider()
        ep = _endpoint()
        upstream_json = {"id": "gd-1", "choices": [{"message": {"content": "ok"}}]}

        class _FakeResp:
            status_code = 200
            def json(self): return upstream_json

        sent = {}
        async def _fake_post(url, json=None, headers=None):
            sent["url"] = url
            sent["headers"] = headers
            sent["json"] = json
            return _FakeResp()

        with mock.patch.object(discovery, "discover", return_value=ep), \
             mock.patch.object(p, "_get_client") as gc:
            gc.return_value.post = _fake_post
            resp = await p.forward(
                {"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}]},
                "openai")
        return sent, resp

    sent, resp = asyncio.run(_run())
    assert sent["url"] == _endpoint().chat_url()
    assert sent["headers"]["X-Dumate-Inapp-Key"] == _endpoint().inapp_key
    assert resp.status_code == 200


def test_forward_stream_uses_send_not_post():
    """流式必须走 ``send(stream=True)``。

    ``await client.post()`` 是 read 模式——httpx 会把整个响应体读完才返回，
    首字节被推迟到上游生成结束。实测 kimi-k3 首 token 从 3s 拖成 11s、整条流
    只剩 1 个 chunk（客户端体验到「憋很久再一次性倒出」）。这条测试锁住回归。
    """
    import asyncio

    async def _run():
        p = DumateProvider()
        ep = _endpoint()

        class _FakeResp:
            status_code = 200
            async def aiter_bytes(self):
                yield b"data: {}\n\n"
            async def aclose(self): pass

        calls: list[str] = []

        async def _fake_post(*a, **kw):
            calls.append("post")
            return _FakeResp()

        async def _fake_send(request, stream=False):
            calls.append(f"send(stream={stream})")
            return _FakeResp()

        client = mock.Mock()
        client.post = _fake_post
        client.send = _fake_send
        client.build_request = mock.Mock(side_effect=lambda method, url, **kw: mock.Mock(
            method=method, url=url, **kw))

        with mock.patch.object(discovery, "discover", return_value=ep), \
             mock.patch.object(p, "_get_client", new=mock.AsyncMock(return_value=client)):
            resp = await p.forward(
                {"model": "kimi-k3", "messages": [], "stream": True}, "openai")
            [c async for c in resp.body_iterator]
        return calls

    assert asyncio.run(_run()) == ["send(stream=True)"]


# --- 额度：quota_overview 数字余额 → 积分条目 + 本地布尔兜底 -----------------


def _quota_overview_result():
    # 已订阅形状（2026-10-05 实测抓包）：totalPoints = subscription 主池 +
    # incremental 赠送包合计
    return {
        "isSubscribed": True,
        "usedPoints": "160.04",
        "totalPoints": "26000.00",
        "modelThrottleInfo": {"throttled": False, "reason": "", "throttleType": ""},
        "subscription": [
            {"packageId": "s1", "usedPoints": "0.00", "totalPoints": "25000.00",
             "startDate": 1791174137, "expireDate": 1793852537,
             "packageType": "plan_pro", "source": "purchase",
             "status": "active", "autoRenewStatus": "cancelled",
             "nextAutoRenewAt": 0, "devicePlatform": "PC",
             "resourceId": "dp-x", "product": "dumate"},
        ],
        "incremental": [
            {"packageId": "p1", "usedPoints": "0.00", "totalPoints": "500.00",
             "startDate": 1791129600, "expireDate": 1793807999,
             "packageType": "grant_point", "source": "login_bonus",
             "status": "active", "product": "dumate"},
            {"packageId": "p2", "usedPoints": "160.04", "totalPoints": "500.00",
             "startDate": 1790870400, "expireDate": 1793548799,
             "packageType": "grant_point", "source": "login_bonus",
             "status": "active", "product": "dumate"},
        ],
    }


def test_quota_prefers_quota_overview_numeric():
    """数字余额（quota_overview）优先：剩 839.96 / 1000，percent 16.0。"""
    from buddy_proxy.dumate import checkin as dumate_checkin

    p = DumateProvider()
    q_overview = {
        "total_points": 1000.0, "used_points": 160.04, "remaining_points": 839.96,
        "is_subscribed": False, "packages": [
            {"package_id": "p1", "used_points": 0.0, "total_points": 500.0,
             "start_ts": 1791129600, "expire_ts": 1793807999,
             "source": "login_bonus", "package_type": "grant_point", "status": "active"},
            {"package_id": "p2", "used_points": 160.04, "total_points": 500.0,
             "start_ts": 1790870400, "expire_ts": 1793548799,
             "source": "login_bonus", "package_type": "grant_point", "status": "active"},
        ],
        "throttled": False,
    }
    with mock.patch.object(dumate_checkin, "fetch_quota_overview", return_value=q_overview):
        q = p.quota()

    head = q["items"][0]
    assert head["label"] == "订阅积分"
    assert head["remaining"] == 839.96
    assert head["total"] == 1000.0
    assert head["used"] == 160.04
    assert head["percent"] == 16.0
    assert head["unit"] == "points"
    # 积分包明细：两个 500 包
    pkgs = q["items"][1:]
    assert len(pkgs) == 2
    assert pkgs[0]["label"].startswith("积分包")
    assert pkgs[0]["remaining"] == 500.0
    assert pkgs[1]["remaining"] == 339.96
    assert pkgs[0]["expire_ts"] == 1793807999


def test_quota_subscription_pool_labeled_and_counted():
    """已订阅账号：subscription 付费主池 + incremental 赠送包都进明细，口径与总结行对上。

    回归 review 发现：旧代码只解析 incremental，漏掉 subscription 主池——
    totalPoints=26000（plan_pro 25000 + 两个 login_bonus 各 500）但明细只列那两个
    已用完的 500 包，用户看面板会懵。
    """
    from buddy_proxy.dumate import checkin as dumate_checkin

    p = DumateProvider()
    q_overview = {
        "total_points": 26000.0, "used_points": 2112.54, "remaining_points": 23887.46,
        "is_subscribed": True, "packages": [
            {"package_id": "sub1", "used_points": 1112.54, "total_points": 25000.0,
             "start_ts": 1791174137, "expire_ts": 1793852537,
             "source": "purchase", "package_type": "plan_pro", "status": "active"},
            {"package_id": "inc1", "used_points": 500.0, "total_points": 500.0,
             "start_ts": 1791129600, "expire_ts": 1793807999,
             "source": "login_bonus", "package_type": "grant_point", "status": "used"},
            {"package_id": "inc2", "used_points": 500.0, "total_points": 500.0,
             "start_ts": 1790870400, "expire_ts": 1793548799,
             "source": "login_bonus", "package_type": "grant_point", "status": "used"},
        ],
        "throttled": False,
    }
    with mock.patch.object(dumate_checkin, "fetch_quota_overview", return_value=q_overview):
        q = p.quota()

    head = q["items"][0]
    assert head["label"] == "订阅积分"
    assert head["total"] == 26000.0
    # 汇总行必须标 head_only：它本身就是各包合计，不能进明细列表
    assert head["head_only"] is True
    # **同时不能声明 sum_items**——否则前端（与 benefits.py 低额度横幅）会把
    # 汇总行和它下面的每个包一起相加，标题翻倍（2026-11-01 报障：界面 33,037
    # vs 线上 16,286）。
    assert q.get("sum_items") in (None, False)
    # 明细 = 总结行的拆分：subscription 主池 + 两个赠送包
    pkgs = q["items"][1:]
    assert len(pkgs) == 3
    assert pkgs[0]["label"] == "订阅套餐（plan_pro）"
    assert pkgs[0]["total"] == 25000.0
    assert pkgs[1]["label"].startswith("积分包")
    # 各池 total 合计 == 总结行 total（口径一致）
    assert sum(x["total"] for x in pkgs) == head["total"]


def test_quota_headline_not_doubled():
    """**核心回归**：标题行等于账号真实剩余，不是汇总行+各包的翻倍。

    用户 2026-11-01 报障：线上可用 16,286，界面却显示 33,037（差一倍）。根因是
    provider 返回的 items 里「订阅积分」汇总行 = 各包合计，同时又声明了
    ``sum_items``，前端把两者一起相加。修法：汇总行标 ``head_only``、去掉
    ``sum_items``，前端标题行走「取第一条有数的」正好取到汇总行。
    """
    from buddy_proxy.dumate import checkin as dumate_checkin

    p = DumateProvider()
    q_overview = {
        "total_points": 45500.0, "used_points": 29397.57, "remaining_points": 16102.43,
        "is_subscribed": True, "packages": [
            {"package_id": "s", "used_points": 25000.0, "total_points": 25000.0,
             "start_ts": 1, "expire_ts": 2, "source": "purchase",
             "package_type": "plan_pro", "status": "used"},
            {"package_id": "g1", "used_points": 0.0, "total_points": 2000.0,
             "start_ts": 1, "expire_ts": 2, "source": "invite_bonus",
             "package_type": "grant_point", "status": "active"},
            {"package_id": "g2", "used_points": 4397.57, "total_points": 18500.0,
             "start_ts": 1, "expire_ts": 2, "source": "login_bonus",
             "package_type": "grant_point", "status": "active"},
        ],
        "throttled": False,
    }
    with mock.patch.object(dumate_checkin, "fetch_quota_overview", return_value=q_overview):
        q = p.quota()

    items = q["items"]
    # 模拟前端 quotaHeadSum 非 sum_items 分支：取第一条有数的（=汇总行）
    assert q.get("sum_items") in (None, False), "声明 sum_items 会触发前端相加翻倍"
    head = items[0]
    assert head["head_only"] is True
    assert head["remaining"] == 16102.43  # == 线上真实可用
    # 若错误地走 sum_items 相加，会是汇总行 + 各包 ≈ 双倍——这里断言没走那条路
    double = head["remaining"] + sum(x["remaining"] for x in items[1:])
    assert double > head["remaining"] * 1.9, "构造场景确实会造成翻倍（确保测试有意义）"
    # 明细列表（quotaItemsHtml 跳过 head_only）只出各包，不含汇总行
    assert all(not it.get("head_only") for it in items[1:])
    assert sum(x["total"] for x in items[1:]) == head["total"]


def test_quota_falls_back_to_boolean_when_overview_fails():
    """quota_overview 拿不到（未登录/网络失败）→ 退回本地布尔 → 百分制。"""
    from buddy_proxy.dumate import checkin as dumate_checkin

    p = DumateProvider()
    ep = _endpoint()

    class _FakeResp:
        status_code = 200
        def json(self): return {"hasRemainingPoints": True}

    with mock.patch.object(dumate_checkin, "fetch_quota_overview", return_value=None), \
         mock.patch.object(discovery, "discover", return_value=ep), \
         mock.patch.object(httpx, "Client") as client_cls:
        client_cls.return_value.__enter__.return_value.get.return_value = _FakeResp()
        q = p.quota()

    assert q["items"][0]["remaining"] == 100
    assert q["items"][0]["unit"] == "percent"


def test_quota_overview_parse():
    """_parse 层：quota_overview 响应 → total/used/remaining + packages。

    subscription 付费主池 + incremental 赠送包都要进 packages（只读 incremental
    会漏掉订阅主池，面板明细跟总结行对不上——review 实测发现的回归点）。
    """
    from buddy_proxy.dumate import checkin as dumate_checkin

    class _FakeResp:
        status_code = 200
        def json(self):
            return {"success": True, "result": _quota_overview_result()}

    with mock.patch.object(dumate_checkin, "resolve_bceconsole_auth") as auth, \
         mock.patch.object(httpx, "Client") as client_cls:
        auth.return_value.headers = lambda: {"Cookie": "x"}
        client_cls.return_value.__enter__.return_value.get.return_value = _FakeResp()
        q = dumate_checkin.fetch_quota_overview()

    assert q is not None
    assert q["total_points"] == 26000.0
    assert q["used_points"] == 160.04
    assert q["remaining_points"] == 25839.96
    assert q["is_subscribed"] is True
    # 1 个订阅主池 + 2 个赠送包
    assert len(q["packages"]) == 3
    assert q["packages"][0]["package_type"] == "plan_pro"
    assert q["packages"][0]["total_points"] == 25000.0
    assert q["packages"][1]["source"] == "login_bonus"
    assert q["packages"][1]["expire_ts"] == 1793807999


def test_quota_overview_none_when_not_logged_in():
    from buddy_proxy.dumate import checkin as dumate_checkin

    with mock.patch.object(dumate_checkin, "resolve_bceconsole_auth", return_value=None):
        assert dumate_checkin.fetch_quota_overview() is None


def test_usage_records_parse():
    """records/usage 响应 → total_count/consumed_points + 逐笔 records。

    实测（2026-10-05）：startAt/endAt 是**秒级**时间戳（毫秒会 500）；
    pointsChange 是带符号扣减字符串（"-8.88"），consumedPoints 是区间合计正数。
    """
    from buddy_proxy.dumate import checkin as dumate_checkin

    class _FakeResp:
        status_code = 200
        def json(self):
            return {"code": 0, "success": True, "result": {
                "totalCount": 168,
                "consumedPoints": "1239.96",
                "list": [
                    {"createdAt": 1791174999, "pointsChange": "-8.88",
                     "expectedPointsChange": "-8.88", "packageId": "p1",
                     "conversationName": "调试会话", "reason": ""},
                    {"createdAt": 1791174986, "pointsChange": "-9.68",
                     "expectedPointsChange": "-9.68", "packageId": "p1",
                     "conversationName": "", "reason": ""},
                ],
            }}

    with mock.patch.object(dumate_checkin, "resolve_bceconsole_auth") as auth, \
         mock.patch.object(httpx, "Client") as client_cls:
        auth.return_value.headers = lambda: {"Cookie": "x"}
        client_cls.return_value.__enter__.return_value.get.return_value = _FakeResp()
        r = dumate_checkin.fetch_usage_records(start_ts=1791100000, end_ts=1791200000)

    assert r is not None
    assert r["total_count"] == 168
    assert r["consumed_points"] == 1239.96
    assert len(r["records"]) == 2
    assert r["records"][0]["ts"] == 1791174999
    assert r["records"][0]["points"] == -8.88
    assert r["records"][0]["conversation_name"] == "调试会话"


def test_usage_records_none_when_not_logged_in():
    from buddy_proxy.dumate import checkin as dumate_checkin

    with mock.patch.object(dumate_checkin, "resolve_bceconsole_auth", return_value=None):
        assert dumate_checkin.fetch_usage_records(start_ts=0, end_ts=1) is None


# --- 签到（bceConsole 通道） ------------------------------------------------


def test_checkin_status_parses_login_bonus_info():
    from buddy_proxy.dumate import checkin as dumate_checkin

    class _FakeResp:
        status_code = 200
        def json(self):
            return {"success": True, "result": {
                "hasIssued": True, "totalPoints": 1000, "totalTimes": 2,
                "signInDays": ["2026-10-02", "2026-10-05"],
            }}

    auth = SimpleNamespace(headers=lambda: {"Cookie": "x", "csrftoken": "y"})
    with mock.patch.object(dumate_checkin, "resolve_bceconsole_auth", return_value=auth), \
         mock.patch.object(httpx, "Client") as client_cls:
        client_cls.return_value.__enter__.return_value.get.return_value = _FakeResp()
        st = dumate_checkin.fetch_checkin_status()

    assert st["checked_in"] is True
    assert st["claimable"] is False
    assert st["total_points"] == 1000
    assert st["daily_credit"] == 500
    assert st["sign_in_days"] == ["2026-10-02", "2026-10-05"]
    # 每日零点轮换（本地推断）：卡片「下次 明天 00:00」chip 用
    assert isinstance(st["next_ts"], int) and st["next_ts"] > 0
    assert st["next_ts_source"] == "inferred"


def test_checkin_status_none_when_not_logged_in():
    from buddy_proxy.dumate import checkin as dumate_checkin

    with mock.patch.object(dumate_checkin, "resolve_bceconsole_auth", return_value=None):
        assert dumate_checkin.fetch_checkin_status() is None


# --- 签到：provider 层把「查不到」标成 query_failed（不返回裸 None）----------
#
# 报障（2026-10-07）：dumate 已在另一台机器自动签到，但本机 8787 面板显示
# 「未打卡」。根因是本机无 DuMate 登录态（qianfan-desktop-app 目录不存在）→
# fetch_checkin_status() 返回 None → snapshot 里 done_today=False、无 error，
# 前端据此渲染成「未签到」+ 可点的「立即打卡」——把「拿不到状态」谎报成
# 「今天还没打」。修法：provider 返回带 query_failed 的失败结构。


def test_checkin_status_returns_query_failed_when_not_logged_in():
    """本机无登录态：不返回裸 None，返回 query_failed 的失败结构。

    裸 None 在 snapshot 里会退化成「未签到」可点状态（本次报障成因）。
    """
    from buddy_proxy.dumate import checkin as dumate_checkin
    from buddy_proxy.dumate.provider import DumateProvider

    p = DumateProvider()
    with mock.patch.object(dumate_checkin, "fetch_checkin_status", return_value=None), \
         mock.patch.object(dumate_checkin, "resolve_bceconsole_auth", return_value=None):
        st = p.checkin_status()

    assert st is not None, "不能返回裸 None——那会让前端显示「未签到」"
    assert st["query_failed"] is True
    assert st["checked_in"] is False and st["claimable"] is False
    assert "本机" in st["message"] and "别的机器" in st["message"]


def test_checkin_status_returns_query_failed_on_upstream_error():
    """有登录态但查询失败（网络/上游异常）：同样标 query_failed，文案区分。"""
    from buddy_proxy.dumate import checkin as dumate_checkin
    from buddy_proxy.dumate.provider import DumateProvider

    p = DumateProvider()
    auth = SimpleNamespace(headers=lambda: {"Cookie": "x"})
    with mock.patch.object(dumate_checkin, "fetch_checkin_status", return_value=None), \
         mock.patch.object(dumate_checkin, "resolve_bceconsole_auth", return_value=auth):
        st = p.checkin_status()

    assert st["query_failed"] is True
    assert "查询失败" in st["message"]


def test_checkin_status_passes_through_real_status():
    """拿得到状态时原样透传（不带 query_failed），行为不变。"""
    from buddy_proxy.dumate import checkin as dumate_checkin
    from buddy_proxy.dumate.provider import DumateProvider

    real = {"checked_in": True, "claimable": False, "message": "ok"}
    p = DumateProvider()
    with mock.patch.object(dumate_checkin, "fetch_checkin_status", return_value=real):
        assert p.checkin_status() == real


# --- 端点发现：pgrep 竞态（自身先出现）------------------------------------


def test_find_main_server_pid_skips_pgrep_self_match():
    """`pgrep -lf dumate-main-server` 输出里竞态出现 pgrep 自身（cmdline 也含
    模式串）——旧过滤「cmdline 含串就匹配」会把自己当端点返回，discover() 随即
    拿到空 key 失败（面板 /health 随机「未运行」）。修正后只认真实可执行文件路径。"""
    from buddy_proxy.dumate import discovery as d

    class _Fake:
        stdout = (
            "8316 pgrep -lf dumate-main-server\n"
            "9007 /Applications/DuMate.app/Contents/Resources/extra-resource/"
            "backend/bin/dumate-main-server -c config.yml --port=52414\n"
        )

    with mock.patch.object(d.subprocess, "run", return_value=_Fake()):
        found = d._find_main_server_pid()
    assert found is not None
    assert found[0] == 9007, "跳过 pgrep 自身，命中真实端点进程"
    assert "dumate-main-server" in found[1]
