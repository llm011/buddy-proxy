"""「本通道用不了」以 **JSONResponse** 返回时也必须换档。

2026-11-29 用户报障：Kimi CLI 在 ``kimi-k3.1-pro`` 上反复 ``provider.auth_error``
（statusCode 403）。metrics 里能看到 trae 连续 8 次 403 都直接返回给了客户端，
而 ``model_order`` 里排在后面的 codebuddy **一次都没被尝试**。

根因：trae 的 1005「无权益」不是抛异常，而是返回一个 403 的 ``JSONResponse``。
``_retryable_failure`` 只认 ``_RETRYABLE_STATUS``（405/429/500/502/503/504），
403 不在里面 → 返回 ``None`` → 换档循环把它当**成功**直接 ``return resp``。

之前那个 PR（#386）加 ``_exc_is_channel_unavailable`` 只覆盖了**异常**路径，
响应对象这条完全没走到——又一处「修一条漏一条」。
"""

from __future__ import annotations

import json

import pytest
from fastapi.responses import JSONResponse, StreamingResponse

from buddy_proxy.codebuddy_provider.forward import (
    _response_is_channel_unavailable,
    _retryable_failure,
)
from buddy_proxy.codebuddy_provider.provider import _CHANNEL_UNAVAILABLE_TYPES


def _resp(status: int, err_type: str, *, nested: bool = True) -> JSONResponse:
    body = {"error": {"message": "x", "type": err_type}}
    content = {"detail": body} if nested else body
    return JSONResponse(content=content, status_code=status)


@pytest.mark.parametrize("err_type", sorted(_CHANNEL_UNAVAILABLE_TYPES))
def test_channel_unavailable_response_is_retryable(err_type):
    """白名单里每个 type 都要让 403 变成「可换档」。"""
    assert _retryable_failure(_resp(403, err_type)) == 403


def test_trae_entitlement_403_now_fails_over():
    """trae 1005 的 403 必须换档——这是用户实际踩到的场景。"""
    resp = _resp(403, "model_not_available_in_channel")
    assert _response_is_channel_unavailable(resp) is True
    assert _retryable_failure(resp) == 403


def test_copilot_real_403_still_not_retryable():
    """copilot 的真实 403 仍不能换档（否则账号级故障被静默掩盖）。"""
    resp = _resp(403, "permission_error")
    assert _response_is_channel_unavailable(resp) is False
    assert _retryable_failure(resp) is None


def test_generic_4xx_still_not_retryable():
    """普通 400/404 仍是确定性错误。"""
    for status in (400, 404, 422):
        assert _retryable_failure(_resp(status, "invalid_request_error")) is None


def test_streaming_response_never_retryable():
    """流式已提交，任何情况都不换档（重放有重复计费风险）。"""
    stream = StreamingResponse(iter([b"x"]))
    assert _retryable_failure(stream) is None


def test_non_nested_error_body_is_recognized():
    """错误体可能不带 ``detail`` 外壳（直接 ``{"error": {...}}``）。"""
    assert _retryable_failure(_resp(403, "model_not_available_in_channel",
                                    nested=False)) == 403


@pytest.mark.parametrize("body", [
    b"not json", b"[]", b'"str"', b"{}", b'{"detail": "plain string"}',
    b'{"error": "not-a-dict"}',
])
def test_malformed_error_bodies_do_not_crash(body):
    """畸形错误体返回 False，不能抛异常（否则错误处理本身变 500）。"""
    resp = JSONResponse(content={}, status_code=403)
    resp.body = body
    assert _response_is_channel_unavailable(resp) is False
    assert _retryable_failure(resp) is None
