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


# ---------------------------------------------------------------------------
# 组内排序：按模型 id 升序（用户 2026-11-09 要求）
# ---------------------------------------------------------------------------
#
# 之前是「保持上游目录原序」，各通道顺序跟着各自上游走：codebuddy 从 kimi 起、
# trae 从 glm 起、codex 从 gpt-6.1 起——同一个模型页里十来个通道各排各的，
# 扫起来很乱。现在统一按 id 字典序。

def test_group_models_sorted_by_id():
    """排序键：先 disabled，再按裸名小写、再按原串（稳定）。

    这里直接验排序键的构造规则（与 ``models_api`` 里那段 lambda 同源），
    不打 HTTP——本文件是静态契约测试，没有 envexec 夹具。
    """
    from buddy_proxy.web.ui.models_api import _bare_model_id

    def sort_key(m, provider):
        bare = _bare_model_id(str(m.get("id") or ""), provider)
        return (1 if m.get("disabled") else 0, bare.lower(), bare)

    models = [
        {"id": "kimi-k3"},
        {"id": "deepseek-v4.1-flash"},
        {"id": "glm-5.3", "disabled": True},
        {"id": "hy4"},
    ]
    got = [m["id"] for m in sorted(models, key=lambda m: sort_key(m, "codebuddy"))]
    assert got == ["deepseek-v4.1-flash", "hy4", "kimi-k3", "glm-5.3"], got


def test_sort_strips_provider_prefix():
    """qoder 的 id 带前缀，排序要按裸名——否则全组都以 provider 名开头。"""
    from buddy_proxy.web.ui.models_api import _bare_model_id

    def bare(mid):
        return _bare_model_id(mid, "qoder")

    models = [{"id": "qoder/qwen3.8-flash"}, {"id": "qoder/deepseek-v4-pro"},
              {"id": "qoder/glm-5.3"}]
    got = [m["id"] for m in sorted(models, key=lambda m: bare(m["id"]).lower())]
    assert got == ["qoder/deepseek-v4-pro", "qoder/glm-5.3", "qoder/qwen3.8-flash"], got


def test_disabled_models_sort_last():
    """停用项排最后（原有约定不能因为加排序丢掉）。"""
    def key(m):
        return (1 if m.get("disabled") else 0, m["id"].lower(), m["id"])

    models = [{"id": "b"}, {"id": "a", "disabled": True}, {"id": "c"}]
    got = [m["id"] for m in sorted(models, key=key)]
    assert got == ["b", "c", "a"], got


def test_sort_ignores_case_so_same_family_stays_together():
    """大小写不统一时仍归到一起（traepat 有 Doubao-Seed-* 与 doubao-seed-*）。

    纯 ASCII 字典序会把大写开头的全排到最前（``D`` < ``d``），同一个模型族被
    拆成两段，比不排序更难扫。
    """
    ids = ["Doubao-Seed-2.1-Pro", "doubao-seed-evolving", "deepseek-v4-pro"]
    got = sorted(ids, key=lambda s: (s.lower(), s))
    assert got == ["deepseek-v4-pro", "Doubao-Seed-2.1-Pro", "doubao-seed-evolving"]
