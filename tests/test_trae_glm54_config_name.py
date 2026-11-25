"""glm-5.4 在 trae 通道必须**携带** config_name。

## 背景（2026-11-26 纠正）

本文件此前锁的是相反的行为（省略 config_name），那是一个**只看「是否出流」**
得出的误判。真相是：

| config_name | 结果 |
|---|---|
| **省略** | HTTP 200 正常出流，**但网关认不出 glm-5.4，静默回落到默认模型 Seed-2.1-Turbo**：官方账单记成 Seed、按 Seed 计费、分词器指纹也是 Seed 的（同一 prompt 90 tokens，真 GLM 是 54） |
| `"glm-5.4"` | 路由到**真 GLM-5.4**。套餐含该模型的账号（付费 Ethan1）200；不含的（免费号）回 1005 套餐权益不足 |

所以省略 config_name 的代价是**所有账号**（含付费）都被偷换成 Seed——这正是用户
在官方消耗明细里看到「一直调 glm-5.4 却全是 Seed-2.1-Turbo」的根因。1005 不是
字段问题，是账号权益问题，由 forward 的 failover 按「账号+模型」跳过
（见 ``test_trae_glm54_entitlement_1005.py``），绝不靠去掉字段来「修好」。

## 验证口径（别再只看是否出流）

- 同一 prompt 比对 ``prompt_tokens``（真 GLM 54 / Seed 90）；
- 或查官方消耗明细（``/ui/api/trae/usage-logs``）里记的模型名与积分。

## 为什么保留 ``_omits_config_name`` 机制

它现在为空，但保留是为了：真遇到「带 config_name → 4001、去掉就好转」且**用上述
口径确认后端没被换**的模型时，仍有按模型省略的入口。两条路径（native
``_build_native_body`` / 文本协议 ``_build_chat_body``）必须共用同一份名单。
"""

from __future__ import annotations

from buddy_proxy.trae.native_tools import (
    _NATIVE_NO_CONFIG_NAME,
    _build_native_body,
    _omits_config_name,
)


def _msgs():
    return [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_glm54_body_carries_config_name():
    """核心断言：glm-5.4 的请求体必须带 config_name，否则被静默换成 Seed。"""
    body = _build_native_body(_msgs(), "glm-5.4", False, None)
    assert body["config_name"] == "glm-5.4", (
        "不带它网关认不出 glm-5.4，会静默回落到 Seed-2.1-Turbo（账单/计费/分词器全是 Seed）")
    assert body["model"] == "glm-5.4"


def test_glm54_production_combo_config_name_and_max_mode():
    """用户默认要 Max：native 生产路径要同时带 config_name + is_max_mode。

    2026-11-26 实测（付费号）：chat_v3 + config_name + is_max_mode → prompt_tokens=54，
    即真 GLM-5.4 且 Max 生效。缺任一字段都不是这个组合。
    """
    body = _build_native_body(_msgs(), "glm-5.4", True, None, max_mode=True)
    assert body["config_name"] == "glm-5.4"
    assert body.get("is_max_mode") is True


def test_glm53_still_carries_config_name():
    """既有模型不受影响：glm-5.3 系列靠这个字段，必须继续带。"""
    for model in ("glm-5.3", "glm-5.3-flash", "glm-5.3-flashx"):
        body = _build_native_body(_msgs(), model, False, None)
        assert body["config_name"] == model, f"{model} 必须继续带 config_name"


def test_omission_helper_false_for_glm54_all_spellings():
    """glm-5.4 任何大小写写法都**不再**被省略（大小写不敏感的判定本身保留）。"""
    for spelling in ("glm-5.4", "GLM-5.4", "Glm-5.4", " glm-5.4 "):
        assert _omits_config_name(spelling) is False, f"{spelling!r} 不该再被省略"


def test_omission_set_is_empty():
    """省略名单当前为空——glm-5.4 已移出。往里加模型前必须用分词器指纹/账单验证。"""
    assert "glm-5.4" not in _NATIVE_NO_CONFIG_NAME
    assert len(_NATIVE_NO_CONFIG_NAME) == 0


def test_unknown_model_keeps_config_name():
    """没实测过的模型维持原行为（带 config_name）——别拿老模型做实验。"""
    assert _omits_config_name("some-new-model") is False
    body = _build_native_body(_msgs(), "some-new-model", False, None)
    assert body["config_name"] == "some-new-model"


def test_tools_payload_unaffected():
    """带 config_name 不影响 tools 注入（两件事互不相关）。"""
    tools = [{"type": "function",
              "function": {"name": "bash", "description": "run",
                           "parameters": {"type": "object", "properties": {}}}}]
    body = _build_native_body(_msgs(), "glm-5.4", False, tools)
    assert body["config_name"] == "glm-5.4"
    assert body.get("tools"), "tools 必须照常带上"


def test_text_protocol_also_carries_config_name():
    """文本协议路径（native 4001 后的回落）同样必须带 config_name。

    两条路径共用 ``_omits_config_name``：只改一处会让回落路径继续被换成 Seed。
    实测 solo_work_lite / chat_v3 带 config_name 都拿到真 GLM（54）。
    """
    from buddy_proxy.trae.text_protocol import _build_chat_body

    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    body54 = _build_chat_body(msgs, "glm-5.4", False)
    assert body54["config_name"] == "glm-5.4", "回落路径也要带"
    assert body54["model"] == "glm-5.4"

    # 既有模型不受影响
    body53 = _build_chat_body(msgs, "glm-5.3", False)
    assert body53["config_name"] == "glm-5.3"


def test_glm_generation_floor_keeps_53_but_drops_52():
    """glm 家族代际下限 5.3：5.3 及以上并存，5.2 及以下照旧被收敛。

    这条同时防两种退化：
    1. 有人删掉 ``_GENERATION_FLOOR`` → 5.3 全家被 5.4 顶掉（用户明确不要）；
    2. 有人把缺省值写成 0.0（``version >= floor.get(family, 0.0)``）→ 所有家族
       都免于收敛，gpt-5.6 / deepseek-v4-pro 一起复活。
    """
    from buddy_proxy.trae.provider import (
        _GENERATION_FLOOR,
        _is_old_generation,
    )

    pool = {"glm-5.2", "glm-5.3", "glm-5.4"}
    assert _is_old_generation("glm-5.3", pool) is False, "5.3 必须保留"
    assert _is_old_generation("glm-5.4", pool) is False
    assert _is_old_generation("glm-5.2", pool) is True, "5.2 仍要被收敛"

    # 别的家族不受影响（缺省不是 0.0）
    assert _is_old_generation("gpt-5.6-sol", {"gpt-5.6-sol", "gpt-6.1-sol"}) is True
    assert _is_old_generation("deepseek-v4-pro", {"deepseek-v4-pro", "deepseek-v4.1-flash"}) is True

    assert _GENERATION_FLOOR.get("glm") == 5.3
