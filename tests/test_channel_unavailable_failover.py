"""「本通道自己用不了」必须换下一个候选，不能把整个模型判死。

起因（2026-11-28 用户报障）：Kimi 调 ``kimi-k3.1-pro`` 直接失败，返回

    qoder 无 Qoder CN 区域的可用账号（已有 3 个账号属于其它区域…）

而 ``model_order`` 里明明还配着 ``trae/``、``codebuddy/``、``qwenpc/`` 三个候选，
一次都没被尝试——因为 qoder 抛的是 400，而 400 不在 ``_RETRYABLE_STATUS`` 里，
换档循环把它当「确定性错误」直接上抛了。

真因：**「这条通道自己用不了」和「这个请求本身有问题」是两件事**。前者换一条
通道就能成功，后者换谁都没用。原来的实现只看状态码，分不开这两种。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from buddy_proxy.codebuddy_provider.provider import (
    _CHANNEL_UNAVAILABLE_TYPES,
    _exc_is_channel_block,
    _exc_is_channel_unavailable,
)


def _exc(status: int, err_type: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": {"message": "x", "type": err_type}},
    )


@pytest.mark.parametrize("err_type", sorted(_CHANNEL_UNAVAILABLE_TYPES))
def test_channel_unavailable_types_are_detected(err_type):
    """白名单里的每个 type 都要被认出来（否则又变成「一个候选都不试」）。"""
    assert _exc_is_channel_unavailable(_exc(400, err_type)) is True


def test_qoder_region_mismatch_fails_over():
    """qoder 跨区：400 + invalid_request，必须换档。

    这是用户实际踩到的场景：qoder 的账号都在 global 区域，CN 子通道没有可用
    账号，而 model_order 里后面还有 trae/codebuddy/qwenpc。
    """
    exc = _exc(400, "invalid_request")
    assert _exc_is_channel_unavailable(exc) is True
    # 它**不是**上游风控封禁，不该被打上整通道冷却标记
    assert _exc_is_channel_block(exc) is False


def test_trae_entitlement_fails_over():
    """trae 1005 权益不足：403 + 专属 type，要换档。

    账号没这个模型的权益 ≠ 请求有问题。实测：trae 排第二、codebuddy 排第四时，
    不换档就会停在 trae 的 403 上。
    """
    assert _exc_is_channel_unavailable(
        _exc(403, "model_not_available_in_channel")) is True


def test_copilot_real_403_is_not_treated_as_channel_unavailable():
    """copilot 把上游真实 403 也映射成 permission_error，**不能**被跳过。

    review 时发现：白名单第一版直接放了 ``permission_error``。但
    ``copilot/provider.py::_error_type`` 对 403 返回的就是这个 type——那可能是
    账号级故障（凭据/订阅问题），静默跳过会把真故障掩盖成「换档成功」。

    所以 trae 1005 用专属 type ``model_not_available_in_channel`` 表达，
    ``permission_error`` 一律保持原样上抛。
    """
    exc = _exc(403, "permission_error")
    assert _exc_is_channel_unavailable(exc) is False
    assert "permission_error" not in _CHANNEL_UNAVAILABLE_TYPES


@pytest.mark.parametrize("err_type", [
    "invalid_request_body",   # 请求体不合法：换谁都没用
    "invalid_request_error",  # 同上（OpenAI 口径）
    "upstream_error",         # 上游自己的毛病，走别的分支
    "rate_limit_error",       # 有专门的冷却路径，不该被这里抢走
    "authentication_error",   # 凭据问题，有专门的重登路径
])
def test_genuinely_deterministic_errors_still_raise(err_type):
    """这些**不能**进白名单——否则真 bug/真参数错会被换档掩盖。"""
    assert _exc_is_channel_unavailable(_exc(400, err_type)) is False


def test_string_detail_is_not_treated_as_channel_unavailable():
    """裸字符串 detail 读不出 type，保守地当作确定性错误（保持原行为）。"""
    assert _exc_is_channel_unavailable(
        HTTPException(status_code=400, detail="no text content")) is False


def test_malformed_detail_shapes_do_not_crash():
    """detail 形状异常时返回 False，不能抛异常（否则错误处理本身变成 500）。"""
    for detail in (None, [], "x", {"error": "not-a-dict"}, {"no_error": 1}):
        exc = HTTPException(status_code=400, detail=detail)
        assert _exc_is_channel_unavailable(exc) is False
