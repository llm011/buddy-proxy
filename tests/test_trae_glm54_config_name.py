"""glm-5.4 在 trae native 通道必须**省略** config_name。

## 背景

2026-11-24 用户指出 trae 的 Ethan1 是付费账号，「应该有 glm-5.4」。核实后确实
四个 CN 账号（含付费）全都能用——但网关一直报 1005「套餐权益不足」。

## 真因（二分实测）

同一个 Ethan1 账号，只改请求体一个字段：

| config_name | 结果 |
|---|---|
| `"glm-5.4"` | `event:error code 1005`（extra.plan=2）|
| `"GLM-5.4"` | `event:error code 4001 param is invalid` |
| **省略** | 正常出流 |

而 glm-5.3 / glm-5.3-flash 带 config_name 照常 200。

也就是说 5.4 走的是新路由，不认这个历史字段；给了它上游就把请求当成另一个
模型判成「套餐不支持」。这个坑极难从现象反推：1005 的官方文案是「套餐权益
不足」，看起来完全像账号权限问题，而实际**四个账号全中、且去掉字段立刻 200**。

## 为什么按模型名单而不是一律不带

`config_name` 是 Work 通道（traework2api）要求 model/config_name 成对的老约定，
glm-5.3 系列等既有模型都靠它。一律去掉等于拿老模型做实验。所以只对实测不吃
这个字段的模型省略。
"""

from __future__ import annotations

from buddy_proxy.trae.native_tools import (
    _NATIVE_NO_CONFIG_NAME,
    _build_native_body,
    _omits_config_name,
)


def _msgs():
    return [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_glm54_body_omits_config_name():
    """核心断言：glm-5.4 的请求体里不能有 config_name。"""
    body = _build_native_body(_msgs(), "glm-5.4", False, None)
    assert "config_name" not in body, (
        "带了它上游会回 1005（套餐不支持），四个 CN 账号全中")
    assert body["model"] == "glm-5.4", "model 字段本身照常带"


def test_glm53_still_carries_config_name():
    """既有模型不受影响：glm-5.3 系列靠这个字段，不能跟着去掉。"""
    for model in ("glm-5.3", "glm-5.3-flash", "glm-5.3-flashx"):
        body = _build_native_body(_msgs(), model, False, None)
        assert body["config_name"] == model, f"{model} 必须继续带 config_name"


def test_config_name_omission_is_case_insensitive():
    """上游模型名大小写不统一（MODEL_MAP 会还原），判断要跟着不敏感。"""
    for spelling in ("glm-5.4", "GLM-5.4", "Glm-5.4", " glm-5.4 "):
        assert _omits_config_name(spelling) is True


def test_unknown_model_keeps_config_name():
    """没实测过的模型维持原行为（带 config_name）——别拿老模型做实验。"""
    assert _omits_config_name("some-new-model") is False
    body = _build_native_body(_msgs(), "some-new-model", False, None)
    assert body["config_name"] == "some-new-model"


def test_glm54_registered_in_omission_set():
    assert "glm-5.4" in _NATIVE_NO_CONFIG_NAME


def test_tools_payload_unaffected():
    """省略 config_name 不影响 tools 注入（两件事互不相关）。"""
    tools = [{"type": "function",
              "function": {"name": "bash", "description": "run",
                           "parameters": {"type": "object", "properties": {}}}}]
    body = _build_native_body(_msgs(), "glm-5.4", False, tools)
    assert "config_name" not in body
    assert body.get("tools"), "tools 必须照常带上"
