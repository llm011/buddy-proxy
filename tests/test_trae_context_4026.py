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
    yield
    failover._cooldowns.clear()
    with prov._entitlement_skip_lock:
        prov._entitlement_skip.clear()


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
