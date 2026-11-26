def test_qwen4_max_comes_from_the_upstream_catalog():
    """``qwen4-max`` 由上游目录的 ``qmodel_latest`` 映射而来。

    2026-11-12 上游目录里还没有它，当时靠 ``LOCAL_EXTRA_MODELS`` 补一条；
    2026-11-17 上游把 ``qmodel_latest`` 原地改名成 ``Qwen4-Max``，正常映射
    就够了。本地补充那条如果留着，会在目录里多出一条缺上下文字段的同名
    条目，所以必须清掉。
    """
    from buddy_proxy.qoder.catalog import LOCAL_EXTRA_MODELS, MODEL_IDS, public_model_id

    assert MODEL_IDS["qmodel_latest"] == "qwen4-max"
    assert public_model_id({"key": "qmodel_latest", "display_name": "Qwen4-Max"}) == "qwen4-max"
    keys = [str(m.get("key") or "") for m in LOCAL_EXTRA_MODELS]
    assert "qwen4-max" not in keys


def test_qwen4_max_preview_maps_but_is_hidden():
    """预览档用户点名不要：照常映射成对外 id，但由 ``HIDDEN_KEYS`` 摘掉。

    隐藏 ≠ 停用——直接点名 ``qwen4-max-preview`` 仍解析得到上游 key。
    """
    from buddy_proxy.qoder.catalog import HIDDEN_KEYS, MODEL_IDS

    assert MODEL_IDS["qmodel_latest_preview"] == "qwen4-max-preview"
    assert "qmodel_latest_preview" in HIDDEN_KEYS


def test_local_extra_models_do_not_invent_metadata():
    """补充条目不编造上游没给的能力字段。

    上游没声明 max_input / 是否支持图片，就不该填一个数字——客户端会当真，
    长上下文请求可能被误判。
    """
    from buddy_proxy.qoder.catalog import LOCAL_EXTRA_MODELS

    for m in LOCAL_EXTRA_MODELS:
        assert not m.get("max_input_tokens"), f"{m.get('key')} 不该编造上下文窗口"
        assert not m.get("is_vl"), f"{m.get('key')} 不该断言支持图片"


def test_fallback_catalog_knows_qwen4_max():
    """兜底目录（上游拉不到时用）也要是当前一代，别还挂着 Qwen3.7-Max 的名字。"""
    from buddy_proxy.qoder.catalog import FALLBACK_MODELS, public_model_id

    by_key = {str(m.get("key")): m for m in FALLBACK_MODELS}
    assert public_model_id(by_key["qmodel_latest"]) == "qwen4-max"
    assert by_key["qmodel_latest"]["price_factor"] == 0.4


def test_new_models_have_stable_public_ids_and_measured_rates():
    """GLM-5.4 / Step-5-Preview 必须有稳定对外 id，并在断网兜底时保留倍率。"""
    from buddy_proxy.qoder.catalog import (
        Catalog, MODEL_IDS, is_hidden, public_model_id, to_openai_model,
    )

    by_key = {str(m.get("key")): m for m in Catalog.fallback()}
    expected = {
        "gmodel_latest": ("glm-5.4", "GLM-5.4", 0.8),
        "jmodel": ("step-5-preview", "Step-5-Preview", 0.3),
    }
    for key, (public_id, display_name, rate) in expected.items():
        assert MODEL_IDS[key] == public_id
        assert public_model_id(by_key[key]) == public_id
        assert not is_hidden(by_key[key])
        model = to_openai_model(by_key[key], "qoder")
        assert model["id"] == f"qoder/{public_id}"
        assert model["name"] == display_name
        assert model["credits"] == rate
