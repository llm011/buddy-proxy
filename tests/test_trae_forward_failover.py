"""trae work forward 多账号 failover 循环单测。

核心新行为：`_forward_once` 抛 401/429 时冷却当前账号、换下一个；非账号级错误
（502）直接透传；PAT 子类（_pat_variant=True）单账号直通不卷入 work 循环。
用 mock `_forward_once` 按调用序返回/抛错，断言冷却与换号顺序。conftest 已隔离
TRAE_WORK_STATE_DIR。异步 forward 经 asyncio.run 驱动（项目未配 pytest-asyncio）。
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from buddy_proxy.trae import failover
from buddy_proxy.trae.credentials import _CURRENT_WORK_ACCOUNT, save_account_cred
from buddy_proxy.trae.provider import TraeProvider


def _cred(uid: str, nickname: str = "T") -> dict:
    return {
        "uid": uid, "nickname": nickname,
        "access_token": f"at-{uid}", "refresh_token": f"rt-{uid}",
        "expires_at": int(time.time()) + 3600,
        "machine_id": "m", "device_id": "d", "api_host": "h",
        "enterprise_id": "",
    }


@pytest.fixture(autouse=True)
def _clear_cooldowns():
    failover._cooldowns.clear()
    yield
    failover._cooldowns.clear()


def _seed_accounts(*uids: str):
    return [save_account_cred(_cred(u, u.title())) for u in uids]


def _ok_response() -> JSONResponse:
    return JSONResponse(content={"choices": [], "object": "chat.completion"})


def _forward(p: TraeProvider, protocol: str = "openai"):
    return p.forward({"model": "m", "messages": [{"role": "user", "content": "hi"}]}, protocol)


# -- 换号逻辑 -----------------------------------------------------------------


def test_401_fails_over_to_next_account():
    accounts = _seed_accounts("u1", "u2")
    p = TraeProvider()
    calls: list[str] = []

    async def fake_forward_once(body, protocol, original, requested_model,
                                prompt, messages, tools, native, stream, agent_mode):
        acct = _CURRENT_WORK_ACCOUNT.get()
        calls.append(acct)
        if acct == accounts[0].id:
            raise HTTPException(status_code=401, detail="auth expired")
        return _ok_response()

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await _forward(p)
    resp = asyncio.run(run())

    assert resp.status_code == 200
    assert calls == [accounts[0].id, accounts[1].id]
    assert failover.cooldown_left(accounts[0].id)[0] > 0   # 401 → 冷却
    assert failover.cooldown_left(accounts[1].id)[0] == 0  # 成功的账号不冷却


def test_403_fails_over_to_next_account():
    """透传的上游 403（权益/风控）必须被闸门接住换号，不能穿透 failover。

    transport/native_tools 把 403 透传成真实状态码后，_is_account_error 只认
    (401, 429) 的话它既不换号也不冷却、直接冒给客户端（review 2026-10-12）。"""
    accounts = _seed_accounts("u1", "u2")
    p = TraeProvider()
    calls: list[str] = []

    async def fake_forward_once(body, protocol, original, requested_model,
                                prompt, messages, tools, native, stream, agent_mode):
        acct = _CURRENT_WORK_ACCOUNT.get()
        calls.append(acct)
        if acct == accounts[0].id:
            raise HTTPException(status_code=403, detail="forbidden")
        return _ok_response()

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await _forward(p)
    resp = asyncio.run(run())

    assert resp.status_code == 200
    assert calls == [accounts[0].id, accounts[1].id]
    assert failover.cooldown_left(accounts[0].id)[0] > 0   # 403 → 冷却
    assert failover.cooldown_left(accounts[1].id)[0] == 0


def test_429_marks_quota_cooldown():
    accounts = _seed_accounts("u1", "u2")
    p = TraeProvider()

    async def fake_forward_once(*a, **k):
        raise HTTPException(status_code=429, detail="quota")

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            with pytest.raises(HTTPException) as ei:
                await _forward(p)
            return ei.value
    exc = asyncio.run(run())
    assert exc.status_code == 429
    # u1 是 429（账号级）→ 冷却 quota 档；u2 是最后一个账号也**照常冷却**
    #（2026-10-06 行为变更：最后账号失败此前不落冷却，下一轮请求还会先打它、
    # 白吃一次同样的失败才轮到别人）
    _l1, k1 = failover.cooldown_left(accounts[0].id)
    _l2, k2 = failover.cooldown_left(accounts[1].id)
    assert k1 == "quota"
    assert k2 == "quota" and _l2 > 0.0


def test_last_account_error_reraised():
    accounts = _seed_accounts("u1", "u2")
    p = TraeProvider()
    calls: list[str] = []

    async def fake_forward_once(body, protocol, original, requested_model,
                                prompt, messages, tools, native, stream, agent_mode):
        acct = _CURRENT_WORK_ACCOUNT.get()
        calls.append(acct)
        if acct == accounts[0].id:
            raise HTTPException(status_code=401, detail="auth")
        raise HTTPException(status_code=429, detail="quota")

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            with pytest.raises(HTTPException) as ei:
                await _forward(p)
            return ei.value
    exc = asyncio.run(run())
    assert exc.status_code == 429  # 最后一个账号的错误
    assert calls == [accounts[0].id, accounts[1].id]


def test_non_account_error_passthrough_no_failover():
    accounts = _seed_accounts("u1", "u2")
    p = TraeProvider()
    calls: list[str] = []

    async def fake_forward_once(body, protocol, original, requested_model,
                                prompt, messages, tools, native, stream, agent_mode):
        calls.append(_CURRENT_WORK_ACCOUNT.get())
        raise HTTPException(status_code=502, detail="channel down")

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            with pytest.raises(HTTPException) as ei:
                await _forward(p)
            return ei.value
    exc = asyncio.run(run())
    assert exc.status_code == 502
    assert calls == [accounts[0].id]              # 没换号
    assert failover.cooldown_left(accounts[0].id)[0] == 0  # 没冷却


def test_anthropic_nonstream_error_fails_over():
    """关键回归：anthropic 非流式遇 401 必须换号，不能吞成 JSONResponse 返回。

    修复前 _forward_once 对 anthropic 非流式把 HTTPException return 成 JSONResponse，
    循环收不到异常 → 不换号不冷却。现在应冷却 #1 并试 #2。
    """
    accounts = _seed_accounts("u1", "u2")
    p = TraeProvider()
    calls: list[str] = []

    async def fake_forward_once(body, protocol, original, requested_model,
                                prompt, messages, tools, native, stream, agent_mode):
        acct = _CURRENT_WORK_ACCOUNT.get()
        calls.append(acct)
        if acct == accounts[0].id:
            raise HTTPException(status_code=401, detail="auth")
        return _ok_response()

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await _forward(p, protocol="anthropic")
    resp = asyncio.run(run())
    assert resp.status_code == 200
    assert calls == [accounts[0].id, accounts[1].id]
    assert failover.cooldown_left(accounts[0].id)[0] > 0


def test_anthropic_nonstream_final_error_has_anthropic_shape():
    _seed_accounts("u1")
    p = TraeProvider()

    async def fake_forward_once(*a, **k):
        raise HTTPException(status_code=429, detail="quota")

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await _forward(p, protocol="anthropic")
    resp = asyncio.run(run())
    assert resp.status_code == 429
    body = json.loads(resp.body.decode())
    assert body.get("type") == "error"
    assert "error" in body


# -- PAT 直通 -----------------------------------------------------------------


class _PatProvider(TraeProvider):
    _pat_variant = True


def test_pat_variant_single_shot_no_failover():
    """PAT 子类（_pat_variant=True）单次转发，不循环、不冷却任何 work 账号。"""
    _seed_accounts("u1", "u2")
    p = _PatProvider()
    calls = 0

    async def fake_forward_once(*a, **k):
        nonlocal calls
        calls += 1
        return _ok_response()

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await _forward(p)
    resp = asyncio.run(run())
    assert resp.status_code == 200
    assert calls == 1
    assert failover.cooldown_left("u1")[0] == 0
    assert failover.cooldown_left("u2")[0] == 0


def test_pat_variant_error_reraised_no_cooldown():
    _seed_accounts("u1", "u2")
    p = _PatProvider()

    async def fake_forward_once(*a, **k):
        raise HTTPException(status_code=429, detail="pat quota")

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            with pytest.raises(HTTPException) as ei:
                await _forward(p)
            return ei.value
    exc = asyncio.run(run())
    assert exc.status_code == 429
    assert failover.cooldown_left("u1")[0] == 0
    assert failover.cooldown_left("u2")[0] == 0


# -- 上游 SSE 额度码的账号级分类（2026-10-06：双账号一空一满仍报 4008） ----
#
# 根因：上游 error 帧的 code 是**上游码**（4008 等），不是 HTTP 状态码——
# 非流式 _collect 一律 raise 502、流式闸门只认 401/429 两种形态，4008 两条
# 路都进不了 failover 循环的账号级分支，A 号没额度 B 号有钱也直接透传报错。


def test_sse_error_status_classification():
    from buddy_proxy.trae.sse import _sse_error_status

    # 额度类（账号级，换号可能好转）→ 429
    for code in (4008, 4011, 4021, 4031):
        assert _sse_error_status(code) == 429, code
    # 鉴权类 → 401
    for code in (1001, 4010):
        assert _sse_error_status(code) == 401, code
    # 通道级/未知 → 502（不触发换号）
    for code in (4001, 4013, "x", None):
        assert _sse_error_status(code) == 502, code


def test_gate_reclaims_upstream_quota_code_frame():
    """流式闸门：error_chunk 带上游码（4008）也要还原成 HTTPException 换号。"""
    from buddy_proxy.trae.provider import _account_error_from_frame

    def frame(code):
        payload = json.dumps({"error": {"message": "boom", "type": "upstream_error",
                                        "code": code}}, ensure_ascii=False)
        return f"data: {payload}"

    # 上游额度码 → 429；鉴权码 → 401
    assert _account_error_from_frame(frame(4008)).status_code == 429
    assert _account_error_from_frame(frame(4031)).status_code == 429
    assert _account_error_from_frame(frame(1001)).status_code == 401
    # HTTP 形态（_stream 自己抛的 HTTPException）照旧
    assert _account_error_from_frame(frame(401)).status_code == 401
    assert _account_error_from_frame(frame(429)).status_code == 429
    # 通道级（4001/未知码/非 error 帧）→ None 放行
    assert _account_error_from_frame(frame(4001)) is None
    assert _account_error_from_frame(frame("x")) is None
    assert _account_error_from_frame('data: {"choices": []}') is None


def test_collect_maps_upstream_quota_frame_to_429(monkeypatch):
    """非流式：上游 error 帧 code=4008 → HTTPException 429（进 failover 循环）。"""
    raw = ('event: error\n'
           'data: {"code": 4008, "message": "Your requests have exceeded the quota."}\n\n')
    monkeypatch.setattr("buddy_proxy.trae.provider.send_trae_chat",
                        lambda *a, **k: raw)
    p = TraeProvider()
    with pytest.raises(HTTPException) as ei:
        p._collect([{"role": "user", "content": "hi"}], "glm-5.2")
    assert ei.value.status_code == 429
    assert "4008" in str(ei.value.detail)


def test_forward_4008_account_fails_over_to_funded_account():
    """端到端形状：#1 号额度尽（上游 4008 → 429）、#2 号有钱 → 自动换号成功。"""
    accounts = _seed_accounts("empty", "funded")
    p = TraeProvider()
    calls: list[str] = []

    async def fake_forward_once(body, protocol, original, requested_model,
                                prompt, messages, tools, native, stream, agent_mode):
        acct = _CURRENT_WORK_ACCOUNT.get()
        calls.append(acct)
        if acct == accounts[0].id:
            # _collect 把上游 4008 映射成 429 后抛出，detail 是友好中文文案
            raise HTTPException(status_code=429, detail="Trae 账号请求额度已超限 (code: 4008)")
        return _ok_response()

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await _forward(p)
    resp = asyncio.run(run())

    assert resp.status_code == 200
    assert calls == [accounts[0].id, accounts[1].id]
    _left, kind = failover.cooldown_left(accounts[0].id)
    assert kind == "quota" and _left > 0
    # 用尽的号进 quota 冷却后，后续请求直接从 funded 开始，不再先撞 4008
    assert failover.available_accounts()[0].id == accounts[1].id


# -- metrics 账号归属 ----------------------------------------------------------

def test_forward_reports_used_account_to_metrics():
    """work 循环选定账号后写 ACCOUNT_META（/ui 请求日志「通道」列的账号后缀）。

    qoder/kimi/antigravity 同款：选号即写 ``meta["account"]``，failover 后被
    覆盖为最终账号。值统一是稳定 id——名字由 /ui/api/logs 展示端按当前
    alias 链解析（改名后历史日志行跟着变），落库写死名字会冻结旧称。
    """
    from buddy_proxy.core.metrics import ACCOUNT_META

    accounts = _seed_accounts("u1", "u2")
    p = TraeProvider()

    async def fake_forward_once(body, protocol, original, requested_model,
                                prompt, messages, tools, native, stream, agent_mode):
        return _ok_response()

    meta: dict = {}
    holder = ACCOUNT_META.set(meta)
    try:
        async def run():
            with mock.patch.object(TraeProvider, "_forward_once",
                                   side_effect=fake_forward_once):
                return await _forward(p)
        resp = asyncio.run(run())
    finally:
        ACCOUNT_META.reset(holder)

    assert resp.status_code == 200
    assert meta["account"] == "u1"  # 稳定 id（展示端解析成 alias，不落名字）


def test_failover_reports_final_account_to_metrics():
    """首个账号 401 换号后，meta 里是**最终服务成功**的那个账号。"""
    from buddy_proxy.core.metrics import ACCOUNT_META

    accounts = _seed_accounts("u1", "u2")
    p = TraeProvider()

    async def fake_forward_once(body, protocol, original, requested_model,
                                prompt, messages, tools, native, stream, agent_mode):
        acct = _CURRENT_WORK_ACCOUNT.get()
        if acct == accounts[0].id:
            raise HTTPException(status_code=401, detail="auth expired")
        return _ok_response()

    meta: dict = {}
    holder = ACCOUNT_META.set(meta)
    try:
        async def run():
            with mock.patch.object(TraeProvider, "_forward_once",
                                   side_effect=fake_forward_once):
                return await _forward(p)
        resp = asyncio.run(run())
    finally:
        ACCOUNT_META.reset(holder)

    assert resp.status_code == 200
    assert meta["account"] == "u2"


def test_forward_without_account_meta_holder_still_works():
    """离线/单测等未经 _instrument 的场景 holder 为 None，不能炸。"""
    _seed_accounts("u1")
    p = TraeProvider()

    async def fake_forward_once(body, protocol, original, requested_model,
                                prompt, messages, tools, native, stream, agent_mode):
        return _ok_response()

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once",
                               side_effect=fake_forward_once):
            return await _forward(p)
    assert asyncio.run(run()).status_code == 200
