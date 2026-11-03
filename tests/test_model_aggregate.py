"""/v1/models 去重聚合的口径测试。"""

from __future__ import annotations

from buddy_proxy.web.model_aggregate import (
    PLACEHOLDER_IDS,
    aggregate_models,
    bare_model_id,
)

KNOWN = frozenset({"codebuddy", "qoder", "ark", "traepat"})


def _agg(entries, order=None):
    return aggregate_models(entries, order=order or {}, known_providers=KNOWN)


def test_same_model_across_channels_collapses_to_one_entry():
    out = _agg([
        {"id": "deepseek-v4.1-flash", "provider": "codebuddy"},
        {"id": "qoder/deepseek-v4.1-flash", "provider": "qoder"},
        {"id": "ark/deepseek-v4.1-flash", "provider": "ark"},
    ])
    assert len(out) == 1
    assert out[0]["id"] == "deepseek-v4.1-flash"
    assert out[0]["providers"] == ["codebuddy", "qoder", "ark"]


def test_output_id_never_carries_channel_prefix():
    out = _agg([{"id": "qoder/glm-5.3", "provider": "qoder"}])
    assert out[0]["id"] == "glm-5.3"


def test_model_with_own_slash_is_not_mangled():
    """``meta-llama/Llama-4`` 自带的斜杠不是通道前缀，不能切。"""
    out = _agg([{"id": "meta-llama/Llama-4", "provider": "codebuddy"}])
    assert out[0]["id"] == "meta-llama/Llama-4"


def test_placeholders_are_dropped():
    out = _agg([
        {"id": "auto", "provider": "codebuddy"},
        {"id": "default", "provider": "codebuddy"},
        {"id": "glm-5.3", "provider": "codebuddy"},
    ])
    assert [m["id"] for m in out] == ["glm-5.3"]
    assert "auto" in PLACEHOLDER_IDS


def test_capability_bool_is_intersection():
    """任一通道不支持就不能声明支持——裸名会 failover 过去。"""
    out = _agg([
        {"id": "m", "provider": "codebuddy", "images": True},
        {"id": "qoder/m", "provider": "qoder", "images": False},
    ])
    assert out[0]["images"] is False


def test_capability_bool_all_true_stays_true():
    out = _agg([
        {"id": "m", "provider": "codebuddy", "images": True},
        {"id": "qoder/m", "provider": "qoder", "images": True},
    ])
    assert out[0]["images"] is True


def test_capability_bool_absent_everywhere_is_omitted():
    out = _agg([{"id": "m", "provider": "codebuddy"}])
    assert "images" not in out[0]
    assert "reasoning" not in out[0]


def test_capability_bool_ignores_channels_that_simply_did_not_report():
    """某通道没报这个字段 ≠ 它不支持，取交集时忽略缺失项。"""
    out = _agg([
        {"id": "m", "provider": "codebuddy", "images": True},
        {"id": "qoder/m", "provider": "qoder"},  # 没报 images
    ])
    assert out[0]["images"] is True


def test_numeric_fields_take_minimum():
    out = _agg([
        {"id": "m", "provider": "codebuddy", "max_input": 1000000},
        {"id": "qoder/m", "provider": "qoder", "max_input": 200000},
    ])
    assert out[0]["max_input"] == 200000


def test_failover_flag_true_only_when_order_names_the_model():
    out = _agg(
        [
            {"id": "deepseek-v4.1-flash", "provider": "codebuddy"},
            {"id": "qoder/deepseek-v4.1-flash", "provider": "qoder"},
            {"id": "glm-5.3", "provider": "codebuddy"},
        ],
        order={"deepseek-v4.1-flash": ["qoder/deepseek-v4.1-flash",
                                       "codebuddy/deepseek-v4.1-flash"]},
    )
    by_id = {m["id"]: m for m in out}
    assert by_id["deepseek-v4.1-flash"]["failover"] is True
    assert by_id["glm-5.3"]["failover"] is False


def test_providers_order_follows_model_order():
    """候选通道顺序即 failover 顺序，排障时要看得出先打谁。"""
    out = _agg(
        [
            {"id": "codebuddy/m", "provider": "codebuddy"},
            {"id": "qoder/m", "provider": "qoder"},
            {"id": "ark/m", "provider": "ark"},
        ],
        order={"m": ["qoder/m", "codebuddy/m"]},
    )
    assert out[0]["providers"][:2] == ["qoder", "codebuddy"]


def test_display_metadata_comes_from_preferred_channel():
    out = _agg(
        [
            {"id": "deepseek-v4.1-flash", "provider": "codebuddy",
             "name": "codebuddy-name", "credits": "x0.11"},
            {"id": "qoder/deepseek-v4.1-flash", "provider": "qoder",
             "name": "qoder-name", "credits": "x0.99"},
        ],
        order={"deepseek-v4.1-flash": ["qoder/deepseek-v4.1-flash"]},
    )
    assert out[0]["name"] == "qoder-name"
    assert out[0]["credits"] == "x0.99"


def test_failover_models_sort_first():
    out = _agg(
        [
            {"id": "zzz", "provider": "codebuddy"},
            {"id": "aaa", "provider": "codebuddy"},
        ],
        order={"zzz": ["codebuddy/zzz"]},
    )
    assert [m["id"] for m in out] == ["zzz", "aaa"]


def test_empty_and_blank_ids_are_skipped():
    out = _agg([{"id": "", "provider": "codebuddy"},
                {"provider": "codebuddy"},
                {"id": "ok", "provider": "codebuddy"}])
    assert [m["id"] for m in out] == ["ok"]


def test_bare_model_id_helper():
    assert bare_model_id("qoder/glm-5.3") == "glm-5.3"
    assert bare_model_id("glm-5.3") == "glm-5.3"
