"""trae 4026「上下文超限」必须换号，不能当通道级错误直接冒给客户端。

2026-11-26 实测：trae glm-5.4 的真实上限按**账号**浮动，同一请求 A 号 4026、
B 号正常。所以 4026 换号有意义，归账号级（403）；但它不整账号冷却——账号没坏，
只是这一次上下文太长。
"""

from __future__ import annotations

from buddy_proxy.trae.sse import _sse_error_status


def test_4026_maps_to_account_level_so_failover_rotates():
    assert _sse_error_status(4026) == 403


def test_other_classifications_unchanged():
    assert _sse_error_status(1005) == 403
    assert _sse_error_status(4008) == 429
    assert _sse_error_status(1001) == 401
    assert _sse_error_status(4001) == 502


# -- forward 循环：4026 换号，且不整账号冷却 --------------------------------------

import asyncio  # noqa: E402
import time  # noqa: E402
from unittest import mock  # noqa: E402

from fastapi import HTTPException  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from buddy_proxy.trae import failover  # noqa: E402
from buddy_proxy.trae import provider as prov  # noqa: E402
from buddy_proxy.trae.credentials import _CURRENT_WORK_ACCOUNT, save_account_cred  # noqa: E402
from buddy_proxy.trae.provider import TraeProvider  # noqa: E402

import pytest  # noqa: E402


def _cred(uid: str) -> dict:
    return {
        "uid": uid, "nickname": uid.title(),
        "access_token": f"at-{uid}", "refresh_token": f"rt-{uid}",
        "expires_at": int(time.time()) + 3600,
        "machine_id": "m", "device_id": "d", "api_host": "h",
        "enterprise_id": "",
    }


def _ctx_exc() -> HTTPException:
    """模拟闸门/非流式路径还原出的 4026 异常：403 + 挂着上游原始码。"""
    return prov._attach_sse_code(HTTPException(status_code=403, detail="ctx too long"), 4026)


@pytest.fixture
def _clear_state():
    failover._cooldowns.clear()
    with prov._entitlement_skip_lock:
        prov._entitlement_skip.clear()
    with prov._model_overflow_lock:
        prov._model_overflow_mark.clear()
    yield
    failover._cooldowns.clear()
    with prov._entitlement_skip_lock:
        prov._entitlement_skip.clear()
    with prov._model_overflow_lock:
        prov._model_overflow_mark.clear()


def test_4026_rotates_without_account_cooldown(_clear_state):
    """核心：4026 → 换下一个账号；当前账号不进冷却，只记本模型跳过。"""
    accounts = [save_account_cred(_cred(u)) for u in ("u1", "u2")]
    p = TraeProvider()
    calls: list[str] = []

    async def fake_forward_once(*a, **k):
        acct = _CURRENT_WORK_ACCOUNT.get()
        calls.append(acct)
        if acct == accounts[0].id:
            raise _ctx_exc()
        return JSONResponse(content={"choices": []})

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await p.forward(
                {"model": "glm-5.4", "messages": [{"role": "user", "content": "hi"}]},
                "openai")

    resp = asyncio.run(run())
    assert resp.status_code == 200
    assert calls == [accounts[0].id, accounts[1].id], "必须换到下一个账号"
    assert failover.cooldown_left(accounts[0].id)[0] == 0, "4026 不能整账号冷却"
    assert prov._entitlement_skipped(accounts[0].id, "glm-5.4") is True


# -- 对外声明的窗口不能超过实测上限 ---------------------------------------------

from buddy_proxy.trae import max_mode as max_mode_mod  # noqa: E402


def test_declared_window_is_capped_at_measured_ceiling():
    """目录/静态表写 1M，但实测没有账号能过 ~385k：对外声明必须封顶。

    声明虚高有两个下游代价：DSH 按它决定何时压缩（压晚了 → 4026），
    context_guard 按它预检（永不触发 → 失去保护）。2026-11-26 实测 glm-5.4
    在 ~230k 就 4026（部分账号），deepseek-v4.1-flash 实测到 385k。
    """
    win, inject = max_mode_mod.effective_window("cn", "glm-5.4", enabled=True)
    assert win == max_mode_mod.MEASURED_MAX_WINDOW
    assert win < 1_000_000, "不能再把 1M 的目录值原样声明出去"
    assert inject is True, "封顶只改声明值，is_max_mode 仍照常注入"


def test_dev_window_is_not_lowered_by_cap():
    """dev 档（116k）本来就低于封顶值，不受影响。"""
    win, inject = max_mode_mod.effective_window("cn", "glm-5.4", enabled=False)
    assert win == 116_000
    assert inject is False


def test_discovered_max_window_is_capped_too():
    """目录刷新抄回的 max_window=1M 同样要封顶（它才是运行时真值来源）。"""
    discovered = {"max_mode": True, "context_window": 116_000, "max_window": 1_000_000}
    win, _ = max_mode_mod.effective_window(
        "cn", "glm-5.4", discovered=discovered, enabled=True)
    assert win == max_mode_mod.MEASURED_MAX_WINDOW


def test_all_accounts_overflowed_tries_each_once_and_reraises(_clear_state):
    """上下文太大时**所有**账号都会 4026——这是 4026 最常见的场景。

    选号的「全被跳过则至少留一个」兜底只保证不误报「无可用账号」，不能变成
    每轮把 N 个账号挨个重打：这里锁定每轮每个账号**只试一次**，并如实抛出
    4026 让 DSH 去压缩。本机真实账号会混进 list_accounts，这里隔离掉。
    """
    accounts = [save_account_cred(_cred(u)) for u in ("t1", "t2", "t3")]
    ids = {a.id for a in accounts}
    isolated = [a for a in prov.failover.list_accounts() if a.id in ids]
    p = TraeProvider()

    def make():
        calls: list[str] = []

        async def fake_forward_once(*a, **k):
            calls.append(_CURRENT_WORK_ACCOUNT.get())
            raise _ctx_exc()

        async def run():
            with mock.patch.object(prov.failover, "list_accounts", return_value=isolated), \
                 mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
                return await p.forward(
                    {"model": "glm-5.4", "messages": [{"role": "user", "content": "hi"}]},
                    "openai")
        return run, calls

    # 第一轮：每个账号试一次（全员 4026），如实抛出
    run, calls = make()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(run())
    assert prov._is_context_overflow(ei.value) is True, "全部 4026 时要如实抛出"
    assert sorted(calls) == sorted(ids), f"每个账号只试一次，实际 {calls}"
    assert all(prov._entitlement_skipped(a, "glm-5.4") for a in ids)


def test_overflow_skip_expires_quickly(_clear_state):
    """4026 的跳过 TTL 是 120s（短于 1005 的 600s）：DSH 压缩后很快重试短请求。"""
    accounts = [save_account_cred(_cred(u)) for u in ("u1",)]
    prov._mark_entitlement_skip(accounts[0].id, "glm-5.4",
                                ttl=prov._CONTEXT_OVERFLOW_SKIP_TTL_S)
    left = prov._entitlement_skip[(accounts[0].id, "glm-5.4")] - time.time()
    assert 100 < left <= prov._CONTEXT_OVERFLOW_SKIP_TTL_S
    assert prov._CONTEXT_OVERFLOW_SKIP_TTL_S < prov._ENTITLEMENT_SKIP_TTL_S


def test_stream_gate_turns_real_4026_frame_into_rotation(_clear_state):
    """流式路径：上游 ``event:error`` 帧 → _stream 的 error_chunk → 闸门识别换号。

    这是 DSH 实际走的路径（stream=true）。帧形态取自 2026-11-26 实抓。
    """
    frame = ('data: {"error": {"message": "Trae 错误: We\'re sorry, your context '
             'length has exceeded the maximum limit. (code: 4026)", '
             '"type": "upstream_error", "code": 4026}}')
    exc = prov._account_error_from_frame(frame)
    assert exc is not None, "闸门必须把它当账号级错误，不能放行成假成功"
    assert exc.status_code == 403
    assert prov._is_context_overflow(exc) is True


def test_stream_4026_rotates_to_next_account(_clear_state):
    accounts = [save_account_cred(_cred(u)) for u in ("s1", "s2")]
    p = TraeProvider()
    calls: list[str] = []

    def fake_stream(*a, **k):
        acct = _CURRENT_WORK_ACCOUNT.get()
        calls.append(acct)
        frame = ('data: {"error": {"message": "ctx too long", '
                 '"type": "upstream_error", "code": 4026}}')
        if acct == accounts[0].id:
            return iter([frame])
        return iter(['data: {"choices": [{"delta": {"content": "ok"}}]}\n\n',
                     "data: [DONE]\n\n"])

    async def run():
        with mock.patch.object(TraeProvider, "_stream", side_effect=fake_stream):
            return await p.forward(
                {"model": "glm-5.4", "stream": True,
                 "messages": [{"role": "user", "content": "hi"}]}, "openai")

    resp = asyncio.run(run())
    assert calls == [accounts[0].id, accounts[1].id], "流式 4026 也必须换号"
    assert resp.status_code == 200
    assert failover.cooldown_left(accounts[0].id)[0] == 0, "不整账号冷却"


def test_model_wide_overflow_short_circuits_further_fanout(_clear_state):
    """全员 4026 后，本模型进短冷却：后续请求不再把账号挨个打一遍。

    review 实测：跳过表全满后选号回退全量，两轮请求打 18 次上游（9 账号×2）。
    上下文太大时每个账号都注定 4026，重复 fan-out 只是白付往返；模型级标记
    TTL 内直接抛 4026，让客户端（DSH）去压缩。
    """
    accounts = [save_account_cred(_cred(u)) for u in ("m1", "m2")]
    ids = {a.id for a in accounts}
    isolated = [a for a in prov.failover.list_accounts() if a.id in ids]
    p = TraeProvider()

    def make():
        calls: list[str] = []

        async def fake_forward_once(*a, **k):
            calls.append(_CURRENT_WORK_ACCOUNT.get())
            raise _ctx_exc()

        async def run():
            with mock.patch.object(prov.failover, "list_accounts", return_value=isolated), \
                 mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
                return await p.forward(
                    {"model": "glm-5.4", "messages": [{"role": "user", "content": "hi"}]},
                    "openai")
        return run, calls

    run, calls = make()
    with pytest.raises(HTTPException):
        asyncio.run(run())
    assert len(calls) == 2, "第一轮：每个账号试一次"

    run, calls = make()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(run())
    assert len(calls) == 0, f"全员 4026 后不该再打上游，实际打了 {calls}"
    assert prov._is_context_overflow(ei.value) is True, "如实抛 4026 让客户端压缩"


def test_model_wide_overflow_mark_is_model_scoped_and_expires(_clear_state):
    prov._mark_model_overflow("glm-5.4", ttl=60.0)
    assert prov._model_overflow_recently("glm-5.4") is True
    assert prov._model_overflow_recently("glm-5.3") is False, "别的模型不受影响"
    prov._model_overflow_mark.clear()
    prov._mark_model_overflow("glm-5.4", ttl=-1.0)
    assert prov._model_overflow_recently("glm-5.4") is False, "过期即失效"
