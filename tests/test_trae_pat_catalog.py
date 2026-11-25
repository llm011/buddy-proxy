"""Trae PAT 静态模型目录的公开 ID 与上游映射契约。"""

from __future__ import annotations

import json
from pathlib import Path

import buddy_proxy.trae.pat as pat
import pytest

from buddy_proxy.core.context_guard import _window_from_models
from buddy_proxy.trae.pat_provider import TraePatProvider
from buddy_proxy.web.model_list import dsh_capability_fields


_CONFIG = Path(__file__).parents[1] / "src/buddy_proxy/web/models_config.json"


def _traepat_rows() -> dict[str, dict]:
    data = json.loads(_CONFIG.read_text("utf-8"))
    return {
        str(row["id"]): row
        for row in data.get("models", [])
        if row.get("provider") == "traepat"
    }


def test_new_public_ids_are_lowercase_and_map_to_exact_upstream_names():
    """客户端只见小写 ID；大小写敏感的上游拼写留在边界映射。"""
    expected = {
        "deepseek-v4.1-flash": ("deepseek-v4.1-flash", "deepseek-v4.1-flash"),
        "glm-5.3-flashx": ("glm-5.3-flashx", "glm-5.3-flashx"),
        "kimi-k2.8-preview": ("kimi-k2.8-preview", "kimi-k2.8-preview"),
        "step-5-preview": ("step-5-preview", "step-5-preview"),
        "qwen3.8-flash": ("qwen3.8-flash", "qwen3.8-flash"),
        "doubao-seed-evolving": ("Doubao-Seed-Evolving", "Doubao-Seed-Evolving"),
    }
    rows = _traepat_rows()

    assert all(model_id == model_id.lower() for model_id in expected)
    assert {
        model_id: (rows[model_id]["upstream_model"], rows[model_id]["config_name"])
        for model_id in expected
    } == expected

    pat._reload_pat_models()
    assert {model_id: pat.PAT_PUBLIC_MODELS[model_id] for model_id in expected} == expected


def test_retired_kimi_k26_is_absent_from_static_and_runtime_catalogs():
    rows = _traepat_rows()
    pat._reload_pat_models()

    assert "kimi-k2.6" not in rows
    assert "kimi-k2.6" not in pat.PAT_MODELS


@pytest.mark.parametrize("model_id,upstream,window,output", [
    ("gpt-6.2-sol", "gpt-6.2-sol__dev", 404000, 32000),
    ("gpt-6.2-sol-max", "gpt-6.2-sol__max", 1000000, 64000),
])
def test_gpt62_catalog_routes_exact_variants_without_inventing_billing_or_capabilities(
    monkeypatch, model_id, upstream, window, output,
):
    rows = _traepat_rows()
    row = rows[model_id]
    assert row["gateway"] == "plus"
    assert row["upstream_model"] == upstream
    assert row["config_name"] == "gpt-6.2-sol"
    assert row["function"] == "chat_v3"
    assert (row["max_input"], row["max_output"]) == (window, output)
    assert row["quota_pack"] == "扣费归属待验证"
    assert not {"credit_rate", "reasoning", "images", "tool_call", "rate_limit"} & row.keys()

    pat._reload_pat_models()
    assert pat.PAT_PLUS_MODELS[model_id] == (upstream, "gpt-6.2-sol")
    assert model_id not in pat.PAT_PUBLIC_MODELS
    assert model_id not in pat.PAT_MODEL_CREDIT_RATES
    # 两种 function 的上游目录均开放；固定已核实的 chat_v3，不随全局覆盖漂移。
    monkeypatch.setenv("WB_TRAE_NATIVE_FUNCTION", "solo_work_lite")
    messages = [{"role": "user", "content": "offline fixture"}]
    body = pat._build_pat_body(messages, *pat.PAT_MODELS[model_id], False, None,
                               model_id=model_id)
    assert body["model"] == upstream
    assert body["config_name"] == "gpt-6.2-sol"
    assert body["function"] == "chat_v3"
    assert "is_max_mode" not in body  # PAT 按 __dev / __max 路由，不借个人通道开关。

    monkeypatch.setenv("TRAE_PAT_BEARER_PROFILES", '[{"bearer":"offline-bearer"}]')
    provider = TraePatProvider()
    listed = {m["id"]: m for m in provider.models()}
    assert "max_output" not in listed["gpt-6.1-sol"]  # 未声明的旧模型保持原契约。
    assert (listed[model_id]["max_input"], listed[model_id]["max_output"]) == (window, output)
    assert listed[model_id]["quota_pack"] == "扣费归属待验证"
    assert listed[model_id]["credit_rate"] is None
    assert _window_from_models(provider, model_id) == window
    capabilities = dsh_capability_fields(listed[model_id])
    assert capabilities["context_window"] == window
    assert capabilities["max_output_tokens"] == output
    assert provider.refresh_models is None
