"""trae 上游错误必须序列化成 **OpenAI 口径** 的错误体。

起因（2026-11-28 用户报障）：Kimi 侧调 raccoon 时报 ``Error: [provider.auth_error]``，
实际拿到的是::

    {"detail":"Trae plan 权益不足（当前账号/模型组合无权限，可检查会员套餐或换模型） (code: 1005)"}

``detail`` 是**裸字符串**，既不是 OpenAI 的 ``{"error": {...}}`` 也不是 Anthropic
的形状。客户端只能靠 HTTP 状态码猜——Kimi 把 403 归成 ``provider.auth_error``，
弹出「请重新登录」，而真因是「这个模型该账号没权益」，和登录毫无关系。
"""

from __future__ import annotations

import pytest

from buddy_proxy.trae.provider_policy import _sse_error_exception


def test_entitlement_error_uses_openai_error_shape():
    """1005 权益不足：403 + permission_error，不能再是裸字符串。"""
    exc = _sse_error_exception({"code": 1005, "message": "no entitlement"})
    assert exc.status_code == 403
    assert isinstance(exc.detail, dict), (
        "detail 必须是对象；裸字符串会被序列化成 {\"detail\": \"...\"}，"
        "客户端读不到 error.type")
    assert "error" in exc.detail
    err = exc.detail["error"]
    assert err["type"] == "permission_error", (
        "403 应归 permission_error；用 authentication_error 会让客户端弹重新登录")
    assert "权益不足" in err["message"]


def test_quota_error_maps_to_rate_limit_type():
    exc = _sse_error_exception({"code": 4008, "message": "quota"})
    assert exc.status_code == 429
    assert exc.detail["error"]["type"] == "rate_limit_error"


def test_context_overflow_gets_its_own_type():
    """4026 上下文超限：403，但 type 必须是 context_length_exceeded。

    与 1005（权益不足）同为 403，语义却相反：4026 客户端该**压缩上下文重试**，
    1005 压缩也没用。都报 permission_error 的话客户端只会显示「没权限」，
    不会去压缩——用户看到的就是反复失败。
    """
    exc = _sse_error_exception({"code": 4026, "message": "too long"})
    assert exc.status_code == 403
    assert exc.detail["error"]["type"] == "context_length_exceeded"
    # 和权益不足要能区分开
    entitlement = _sse_error_exception({"code": 1005, "message": "no"})
    assert entitlement.detail["error"]["type"] != exc.detail["error"]["type"]


@pytest.mark.parametrize("status,expected", [
    (401, "authentication_error"),
    (403, "permission_error"),
    (429, "rate_limit_error"),
    (402, "insufficient_quota"),
    (400, "invalid_request_error"),
    (404, "invalid_request_error"),
    (504, "timeout"),
    (502, "api_error"),
])
def test_openai_error_type_mapping(status, expected):
    from buddy_proxy.trae.sse import _openai_error_type

    assert _openai_error_type(status) == expected


def test_detail_is_json_serializable_under_fastapi():
    """FastAPI 会把 detail 原样放进 {"detail": ...}——必须是可 JSON 化的对象。"""
    import json

    exc = _sse_error_exception({"code": 1005, "message": "x"})
    body = json.dumps({"detail": exc.detail}, ensure_ascii=False)
    parsed = json.loads(body)
    assert parsed["detail"]["error"]["type"] == "permission_error"
