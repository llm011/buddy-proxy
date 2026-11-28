"""首帧闸门还原出的异常必须是 **OpenAI 口径**，否则下游判定全部失效。

2026-11-29 用户报障的最后一层：Kimi CLI 流式请求反复 ``provider.auth_error``
（403）。前几个 PR 修了非流式错误体形状和 JSONResponse 换档，但**流式走的是
另一条路**——``_gate_first_event`` → ``_account_error_from_frame``，这里还原出的
``HTTPException`` 的 ``detail`` 一直是**裸字符串**。

后果：所有按 ``error.type`` 的判定都读不到东西——

```python
>>> exc = _account_error_from_frame('data: {"error":{"message":"...","code":1005}}')
>>> exc.status_code, type(exc.detail)
(403, str)
>>> _exc_is_channel_unavailable(exc)
False          # ← 1005「本通道无权益」被当成确定性错误，直接上抛
```

于是 trae 的 403 一路返回给客户端，``model_order`` 里排在后面的 codebuddy
一次都没被尝试。**这是同一个 bug 的第五层**（#384/#387/#388/#389 之后）。
"""

from __future__ import annotations

import json

import pytest

from buddy_proxy.codebuddy_provider.provider import (
    _exc_is_channel_block,
    _exc_is_channel_unavailable,
)
from buddy_proxy.trae.provider_policy import _account_error_from_frame


def _frame(**err) -> str:
    return "data: " + json.dumps({"error": err})


def test_entitlement_frame_detail_is_object():
    """1005 还原出的 detail 必须是对象，且 type 能被换档逻辑识别。"""
    exc = _account_error_from_frame(_frame(message="no entitlement", code=1005))
    assert exc is not None
    assert exc.status_code == 403
    assert isinstance(exc.detail, dict), (
        "detail 是裸字符串时，所有读 error.type 的下游判定都会失效")
    assert exc.detail["error"]["type"] == "model_not_available_in_channel"
    assert _exc_is_channel_unavailable(exc) is True, (
        "1005 必须能被识别为「通道不可用」才会换档")


def test_entitlement_frame_is_not_channel_block():
    """1005 不该被当成上游风控封禁（那会整条通道冷却）。"""
    exc = _account_error_from_frame(_frame(message="x", code=1005))
    assert _exc_is_channel_block(exc) is False


def test_context_overflow_frame_keeps_its_own_type():
    """4026 与 1005 都是 403，但客户端动作相反，type 必须不同。"""
    exc = _account_error_from_frame(_frame(message="too long", code=4026))
    assert exc.detail["error"]["type"] == "context_length_exceeded"
    assert _exc_is_channel_unavailable(exc) is False


@pytest.mark.parametrize("code,expected_type", [
    (401, "authentication_error"),
    (429, "rate_limit_error"),
    (4008, "rate_limit_error"),   # 上游额度码 → 429
])
def test_http_shaped_codes_keep_their_type(code, expected_type):
    exc = _account_error_from_frame(_frame(message="m", code=code))
    assert exc is not None
    assert exc.detail["error"]["type"] == expected_type


def test_detail_message_survives():
    """改形状不能把原始文案弄丢——排查全靠它。"""
    exc = _account_error_from_frame(
        _frame(message="Trae plan 权益不足", code=1005))
    assert "权益不足" in exc.detail["error"]["message"]


def test_model_overflow_error_uses_context_length_type():
    """「全员 4026 上下文超限」也必须用标准形状。

    review 时发现这是漏掉的一处：``provider_forward`` 里这条仍写裸字符串。
    客户端读到裸字符串只能显示「没权限」，不会去压缩上下文——而这条错误的
    正确处置恰恰就是压缩后重试。
    """
    import inspect

    from buddy_proxy.trae import provider_forward

    src = inspect.getsource(provider_forward)
    assert '"type": "context_length_exceeded"' in src, (
        "4026 溢出错误没用 context_length_exceeded，客户端不会压缩上下文")
