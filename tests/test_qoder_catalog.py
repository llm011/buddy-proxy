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
    """新模型必须有稳定对外 id，并在断网兜底时保留实测倍率与能力。"""
    from buddy_proxy.qoder.catalog import (
        Catalog, MODEL_IDS, is_hidden, public_model_id, to_openai_model,
    )

    by_key = {str(m.get("key")): m for m in Catalog.fallback()}
    expected = {
        "gmodel_latest": ("glm-5.4", "GLM-5.4", 0.8),
        "jmodel": ("step-5-preview", "Step-5-Preview", 0.3),
        "hmodel": ("hungerg", "HungerG", 0.0),
    }
    for key, (public_id, display_name, rate) in expected.items():
        assert MODEL_IDS[key] == public_id
        assert public_model_id(by_key[key]) == public_id
        assert not is_hidden(by_key[key])
        model = to_openai_model(by_key[key], "qoder")
        assert model["id"] == f"qoder/{public_id}"
        assert model["name"] == display_name
        assert model["credits"] == rate

    hunger = by_key["hmodel"]
    assert hunger["is_free"] is True
    assert hunger["is_reasoning"] is True
    assert hunger["is_vl"] is True
    assert hunger["max_input_tokens"] == 1000000
    from buddy_proxy.qoder.config import REGIONS

    catalog = Catalog(REGIONS["cn"])
    assert catalog.resolve_key("qoder/HungerG") == "hmodel"
    assert catalog.resolve_key("hungerg") == "hmodel"


def test_catalog_merges_app_and_cli_client_views(monkeypatch):
    """ct=0 补 HungerG/Fast，ct=5 补国际版旧模型，两路必须取并集。"""
    import asyncio

    from buddy_proxy.qoder.catalog import Catalog
    from buddy_proxy.qoder.config import REGIONS
    from buddy_proxy.qoder.credentials import Credential

    hunger = {"key": "hmodel", "display_name": "HungerG", "price_factor": 0.0,
              "is_free": True, "is_reasoning": True, "is_vl": True,
              "max_input_tokens": 1000000}
    payloads = {
        "0": {"assistant": [hunger], "chat": [
            {"key": "fast", "display_name": "Fast", "price_factor": 0.6},
            {"key": "qfmodel", "display_name": "Qwen3.8-Flash", "price_factor": 0.04},
        ]},
        "5": {"chat": [
            {"key": "cmodel", "display_name": "Cantus"},
            {"key": "qfmodel", "display_name": "Qwen3.8-Flash", "price_factor": 0.5},
        ]},
    }
    calls = []

    def fake_sign(*args, **kwargs):
        client_type = kwargs["client_type"]
        return "", {"X-Test-Client-Type": client_type}

    class Response:
        status_code = 200
        text = ""

        def __init__(self, client_type):
            self.client_type = client_type

        def json(self):
            return payloads[self.client_type]

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers):
            client_type = headers["X-Test-Client-Type"]
            calls.append(client_type)
            return Response(client_type)

    monkeypatch.setattr("buddy_proxy.qoder.catalog.sign", fake_sign)
    monkeypatch.setattr("buddy_proxy.qoder.catalog.httpx.AsyncClient", Client)
    entries = asyncio.run(Catalog(REGIONS["cn"])._fetch_remote(Credential(token="dt-test")))
    by_key = {entry["key"]: entry for entry in entries}

    assert calls == ["0", "5"]
    assert by_key["hmodel"] == hunger
    assert by_key["fast"]["price_factor"] == 0.6
    assert by_key["cmodel"]["display_name"] == "Cantus"
    assert by_key["qfmodel"]["price_factor"] == 0.04, "同 key 以 ct=0 的当前倍率为准"
