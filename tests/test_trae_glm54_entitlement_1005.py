"""trae 1005「套餐权益不足」的 failover 行为：换号，但按「账号+模型」跳过，不整账号冷却。

## 为什么 1005 特殊

1005 是**账号+模型**级：免费号不能用 glm-5.4，但 glm-5.3 照常可服务。所以：
- 换号有意义（付费号可能有权益）→ 必须让 ``_is_account_error`` 认它（归 403）；
- 但**不能**走 ``failover.mark_cooldown`` 的整账号冷却——那会把该账号对其他模型的
  服务一起误伤 → 改记 ``(account_id, model)`` 跳过，TTL 到期重试。

此前 1005 → 502 → 不换号直接冒给客户端；而为了让免费号「能用」而省略 config_name，
代价是所有账号都被静默换成 Seed（见 ``test_trae_glm54_config_name.py``）。
"""

from __future__ import annotations

import asyncio
import time
from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from buddy_proxy.trae import failover
from buddy_proxy.trae import provider as prov
from buddy_proxy.trae.credentials import _CURRENT_WORK_ACCOUNT, save_account_cred
from buddy_proxy.trae.provider import TraeProvider
from buddy_proxy.trae.sse import _sse_error_status


def _cred(uid: str) -> dict:
    return {
        "uid": uid, "nickname": uid.title(),
        "access_token": f"at-{uid}", "refresh_token": f"rt-{uid}",
        "expires_at": int(time.time()) + 3600,
        "machine_id": "m", "device_id": "d", "api_host": "h",
        "enterprise_id": "",
    }


def _seed(*uids: str):
    return [save_account_cred(_cred(u)) for u in uids]


def _ok() -> JSONResponse:
    return JSONResponse(content={"choices": [], "object": "chat.completion"})


def _forward(p: TraeProvider, model: str = "glm-5.4"):
    return p.forward({"model": model, "messages": [{"role": "user", "content": "hi"}]}, "openai")


def _entitlement_exc() -> HTTPException:
    """模拟闸门/非流式路径还原出的 1005 异常：403 + 挂着原始码。"""
    return prov._attach_sse_code(
        HTTPException(status_code=403, detail="Trae plan 权益不足"), 1005)


@pytest.fixture(autouse=True)
def _clear_state():
    failover._cooldowns.clear()
    with prov._entitlement_skip_lock:
        prov._entitlement_skip.clear()
    yield
    failover._cooldowns.clear()
    with prov._entitlement_skip_lock:
        prov._entitlement_skip.clear()


# -- 状态码映射 / 异常构造 --------------------------------------------------------


def test_1005_maps_to_403_so_failover_rotates():
    """1005 必须归 403（账号级），否则 failover 不换号、直接冒给客户端。"""
    assert _sse_error_status(1005) == 403
    # 既有分类不受影响
    assert _sse_error_status(4008) == 429
    assert _sse_error_status(1001) == 401
    assert _sse_error_status(4001) == 502
    assert _sse_error_status("not-a-code") == 502


def test_sse_error_exception_attaches_original_code():
    exc = prov._sse_error_exception({"code": 1005, "message": "plan"})
    assert exc.status_code == 403
    assert getattr(exc, "trae_sse_code", None) == 1005
    assert prov._is_entitlement_error(exc) is True


def test_attach_code_ignores_garbage():
    exc = prov._attach_sse_code(HTTPException(status_code=502, detail="x"), None)
    assert getattr(exc, "trae_sse_code", None) is None
    assert prov._is_entitlement_error(exc) is False


# -- 闸门 _account_error_from_frame ---------------------------------------------


def test_gate_turns_1005_frame_into_entitlement_exception():
    frame = 'data:{"error":{"message":"plan","type":"api_error","code":1005}}'
    exc = prov._account_error_from_frame(frame)
    assert exc is not None and exc.status_code == 403
    assert getattr(exc, "trae_sse_code", None) == 1005
    assert prov._is_entitlement_error(exc) is True


def test_gate_real_http_403_has_no_sse_code_so_it_still_cools_down():
    """真 403（HTTP 形态）不带原始码 → 走正常整账号冷却，不能被误当成 1005。"""
    frame = 'data:{"error":{"message":"forbidden","code":403}}'
    exc = prov._account_error_from_frame(frame)
    assert exc is not None and exc.status_code == 403
    assert getattr(exc, "trae_sse_code", None) is None
    assert prov._is_entitlement_error(exc) is False


def test_gate_still_passes_through_non_account_errors():
    """4001（通道级）仍放行（None），不受 1005 改动影响。"""
    assert prov._account_error_from_frame('data:{"error":{"code":4001,"message":"bad"}}') is None


# -- (账号, 模型) 跳过表 ---------------------------------------------------------


def test_entitlement_skip_is_model_scoped():
    prov._mark_entitlement_skip("u1", "GLM-5.4")  # 键按小写归一
    assert prov._entitlement_skipped("u1", "glm-5.4") is True
    assert prov._entitlement_skipped("u1", "glm-5.3") is False, "同账号别的模型不能被误伤"
    assert prov._entitlement_skipped("u2", "glm-5.4") is False, "别的账号不受影响"


def test_entitlement_skip_expires():
    with prov._entitlement_skip_lock:
        prov._entitlement_skip[("u1", "glm-5.4")] = time.time() - 1
    assert prov._entitlement_skipped("u1", "glm-5.4") is False
    assert ("u1", "glm-5.4") not in prov._entitlement_skip, "过期条目顺手清掉"


# -- forward 循环 ----------------------------------------------------------------


def test_1005_rotates_without_account_cooldown():
    """核心：1005 → 换下一个账号；当前账号**不**进冷却，只记本模型跳过。"""
    accounts = _seed("u1", "u2")
    p = TraeProvider()
    calls: list[str] = []

    async def fake_forward_once(body, protocol, original, requested_model,
                                prompt, messages, tools, native, stream, agent_mode, max_inject=False):
        acct = _CURRENT_WORK_ACCOUNT.get()
        calls.append(acct)
        if acct == accounts[0].id:
            raise _entitlement_exc()
        return _ok()

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await _forward(p)
    resp = asyncio.run(run())

    assert resp.status_code == 200
    assert calls == [accounts[0].id, accounts[1].id], "必须换到下一个账号"
    assert failover.cooldown_left(accounts[0].id)[0] == 0, "1005 不能整账号冷却"
    assert prov._entitlement_skipped(accounts[0].id, "glm-5.4") is True


def test_skipped_account_still_serves_other_models():
    """被 1005 跳过的账号，对别的模型照常可用（这是不整账号冷却的全部意义）。"""
    accounts = _seed("u1", "u2")
    prov._mark_entitlement_skip(accounts[0].id, "glm-5.4")
    p = TraeProvider()
    calls: list[str] = []

    async def fake_forward_once(*a, **k):
        calls.append(_CURRENT_WORK_ACCOUNT.get())
        return _ok()

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await _forward(p, model="glm-5.3")
    resp = asyncio.run(run())

    assert resp.status_code == 200
    assert accounts[0].id in calls, "glm-5.3 请求仍应能用到 u1"


def test_glm54_request_prefers_account_without_entitlement_skip():
    """打 glm-5.4 时，被标记的账号应被跳到后面，直接先试能服务的账号。"""
    accounts = _seed("u1", "u2")
    prov._mark_entitlement_skip(accounts[0].id, "glm-5.4")
    p = TraeProvider()
    calls: list[str] = []

    async def fake_forward_once(*a, **k):
        calls.append(_CURRENT_WORK_ACCOUNT.get())
        return _ok()

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await _forward(p, model="glm-5.4")
    resp = asyncio.run(run())

    assert resp.status_code == 200
    assert calls == [accounts[1].id], "u1 已被记为 glm-5.4 不可用，不该再白打一次"


def test_all_accounts_skipped_still_tries_one():
    """全被标记时至少留一个试（TTL 可能过期/套餐可能升级），不能误报「无可用账号」。"""
    accounts = _seed("u1", "u2")
    for a in accounts:
        prov._mark_entitlement_skip(a.id, "glm-5.4")
    p = TraeProvider()

    async def fake_forward_once(*a, **k):
        return _ok()

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await _forward(p, model="glm-5.4")
    resp = asyncio.run(run())

    assert resp.status_code == 200, "全被跳过时仍要放行重试，而不是 429/401"


def test_last_account_1005_is_reraised_not_swallowed():
    """最后一个账号也 1005：错误要如实抛出（客户端看到套餐不足），不能假成功。"""
    _seed("u1")
    p = TraeProvider()

    async def fake_forward_once(*a, **k):
        raise _entitlement_exc()

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await _forward(p)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(run())
    assert ei.value.status_code == 403
    assert prov._is_entitlement_error(ei.value) is True


def test_empty_requested_model_does_not_crash_in_selection():
    """回归（review 发现）：requested_model 为 None 时整条 forward 不能 AttributeError。

    ``forward`` 里是 ``body.get("model", "auto")``，``.get`` 默认值只在键缺失时生效，
    客户端显式发 ``"model": null`` 就会拿到 None；``map_model_for`` 对 None 原样透传，
    再 ``.lower()`` 会在**选号之前**（_max_inject / 选号过滤两处）炸掉整个 forward。
    统一走 ``_internal_model_key`` 兜底。
    """
    _seed("u1")
    p = TraeProvider()

    async def fake_forward_once(*a, **k):
        return _ok()

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await p.forward({"model": None, "messages": [{"role": "user", "content": "hi"}]},
                                   "openai")
    resp = asyncio.run(run())  # 不抛 AttributeError 即通过
    assert resp.status_code == 200


def test_internal_model_key_handles_none_empty_and_alias():
    """helper 本身：None / 空串不炸，大小写别名归一到内部小写键。"""
    assert prov._internal_model_key("cn", None) == ""
    assert prov._internal_model_key("cn", "") == ""
    assert prov._internal_model_key("cn", "GLM-5.4") == "glm-5.4"
    assert prov._internal_model_key("cn", "glm-5.4") == "glm-5.4"
