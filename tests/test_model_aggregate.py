"""/v1/models 去重聚合的口径测试。"""

from __future__ import annotations

from buddy_proxy.web.model_aggregate import (
    PLACEHOLDER_IDS,
    _strip_known_prefix,
    aggregate_models,
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


def test_capability_bool_intersection_without_failover():
    """没有候选链 = 只打首选通道，没兜底，任一不支持就不能声明支持。"""
    out = _agg([
        {"id": "m", "provider": "codebuddy", "images": True},
        {"id": "qoder/m", "provider": "qoder", "images": False},
    ])
    assert out[0]["failover"] is False
    assert out[0]["images"] is False


def test_capability_bool_union_with_failover():
    """有候选链时取并集：不支持的那个通道失败后自动换到支持的通道。

    实测反例（2026-11-04）：deepseek-v4.1-flash 只有 codebuddy 报图片，
    取交集后目录说 supports_images=false，客户端在本地把图剥了，
    可裸名发图上游答得很好。
    """
    out = _agg(
        [
            {"id": "deepseek-v4.1-flash", "provider": "codebuddy", "images": True},
            {"id": "qoder/deepseek-v4.1-flash", "provider": "qoder", "images": False},
        ],
        order={"deepseek-v4.1-flash": ["codebuddy/deepseek-v4.1-flash",
                                       "qoder/deepseek-v4.1-flash"]},
    )
    assert out[0]["failover"] is True
    assert out[0]["images"] is True


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


def test_strip_known_prefix_only_strips_known_channels():
    assert _strip_known_prefix("qoder/glm-5.3", KNOWN) == "glm-5.3"
    assert _strip_known_prefix("glm-5.3", KNOWN) == "glm-5.3"
    # 不是已知通道的前缀不能切（模型可能自带斜杠）
    assert _strip_known_prefix("meta-llama/Llama-4", KNOWN) == "meta-llama/Llama-4"


# ---- 显式窗口档位：不被小窗口通道拉低 ----


def test_window_tier_overrides_min_across_channels():
    """实测 deepseek-v4.1-flash：codebuddy 报 1M、qoder 报 180k。

    取最小值会把目录压成 180k，用户明明跑得动长上下文，客户端却按 18 万
    就开始压缩。配置里写了 window_tier 就该按它对外表述。
    """
    out = _agg([{
        "id": "deepseek-v4.1-flash", "provider": "codebuddy",
        "max_input": 1000000, "window_tier": 600000,
    }, {
        "id": "qoder/deepseek-v4.1-flash", "provider": "qoder",
        "max_input": 180000,
    }])
    assert out[0]["max_input"] == 600000
    assert out[0]["context_window"] == 600000


def test_without_window_tier_min_still_applies():
    """没写 tier 时保持原口径：取最小值兜底。"""
    out = _agg([{
        "id": "m", "provider": "codebuddy", "max_input": 1000000,
    }, {
        "id": "qoder/m", "provider": "qoder", "max_input": 180000,
    }])
    assert out[0]["max_input"] == 180000


def test_window_tier_ignored_when_invalid():
    for bad in (0, -1, True, "600k", None):
        out = _agg([{
            "id": "m", "provider": "codebuddy",
            "max_input": 1000000, "window_tier": bad,
        }, {
            "id": "qoder/m", "provider": "qoder", "max_input": 180000,
        }])
        assert out[0]["max_input"] == 180000, f"tier={bad!r} 不该生效"


# ---- 端到端：window_tier 必须活着走到聚合出口 ----
#
# 第一版就栽在这：tier 写进了 models_config.json，但 normalize_model_format
# 构造新 dict 时把它丢了，聚合里 preferred.get("window_tier") 恒为 None，
# 实测 /v1/models 仍是 180k。这类「字段在中途被丢」的 bug 光测聚合函数
# 抓不到，必须把 normalize 串进来。


def test_window_tier_survives_normalization():
    from buddy_proxy.web.model_list import normalize_model_format

    norm = normalize_model_format({
        "id": "deepseek-v4.1-flash",
        "maxInputTokens": 1000000,
        "window_tier": 600000,
    })
    assert norm["window_tier"] == 600000, "normalize 丢了 window_tier，配置等于没写"


def test_window_tier_end_to_end_through_normalize_and_aggregate():
    from buddy_proxy.web.model_list import normalize_model_format

    raw = [
        normalize_model_format({"id": "deepseek-v4.1-flash",
                                "maxInputTokens": 1000000,
                                "window_tier": 600000}),
        # 通道目录条目：小窗口，没 tier
        {"id": "qoder/deepseek-v4.1-flash", "provider": "qoder",
         "max_input": 180000},
    ]
    out = aggregate_models(
        raw,
        order={"deepseek-v4.1-flash": ["codebuddy/deepseek-v4.1-flash",
                                       "qoder/deepseek-v4.1-flash"]},
        known_providers=KNOWN,
    )
    assert out[0]["context_window"] == 600000
    assert out[0]["max_input"] == 600000
