"""模型页目录刷新与隐藏操作的前端契约。"""

from __future__ import annotations

import pathlib


_STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
APP = (_STATIC / "app_model_actions.js").read_text(encoding="utf-8")
CHARTS = (_STATIC / "chart_log.js").read_text(encoding="utf-8")
FORMAT = (_STATIC / "ui_format.js").read_text(encoding="utf-8")
INDEX = (_STATIC / "index.html").read_text(encoding="utf-8")


def test_refresh_button_is_capability_gated_and_uses_generic_api():
    assert "g.refreshable ?" in CHARTS
    assert "refreshProviderModels" in CHARTS
    assert "/ui/api/models/refresh" in APP
    assert "↻ 重新拉取" in CHARTS


def test_refresh_button_does_not_toggle_group_and_has_loading_state():
    assert "event.stopPropagation();refreshProviderModels" in CHARTS
    assert "button.disabled = true" in APP
    assert "拉取中" in APP
    assert "await loadData(['models'], true)" in APP


def test_hide_and_restore_controls_explain_non_blocking_semantics():
    assert "/ui/api/model-hidden" in APP
    assert "hideModel" in CHARTS
    assert "openHiddenModelsModal" in INDEX
    assert "直接点名仍可调用" in APP
    assert "隐藏只是不展示，停用才会拒绝调用" in INDEX


def test_dynamic_model_values_use_safe_js_string_encoding():
    """上游模型 id 不可信，onclick 参数必须先 JSON 编码再 HTML escape。"""
    assert "function jsq(s)" in FORMAT
    assert "hideModel(${jsq(g.id)},${jsq(m.id)})" in CHARTS
    assert "refreshProviderModels(${jsq(g.id)},this)" in CHARTS
    assert "restoreHiddenModel(${jsq(m.provider)},${jsq(m.model)})" in APP
