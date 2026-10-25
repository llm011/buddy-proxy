"""管理 UI / 指标收集 / 设置持久化 冒烟测试（完全离线，不访问远程）。

覆盖：
- /ui 页面与 /ui/api/* （overview/models/stats/settings/test）
- 默认模型设置：持久化、校验、forward_chat 自动补齐
- MetricsCollector：聚合、按模型统计、JSONL 落盘重启恢复、流式 chunk 计数

运行：
    .venv/bin/python -m pytest tests/test_ui_admin.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from buddy_proxy import __main__ as m
from buddy_proxy.core import state as st
from buddy_proxy.core import settings as settings_mod
from buddy_proxy.benefits import BenefitsManager, CheckinHistory
from buddy_proxy.core.metrics import MetricsCollector, SSEUsageExtractor, normalize_usage
from buddy_proxy.providers.base import BaseProvider
from buddy_proxy.providers.zcode import _quota_items


# ---------------------------------------------------------------------------
# 假 provider / 假 state
# ---------------------------------------------------------------------------
class FakeProvider(BaseProvider):
    id = "fakeprov"
    name = "Fake Provider"

    def __init__(self):
        self.last_body = None

    def models(self):
        return [{"id": "fake-model", "description": "Fake Model"}]

    def ensure_auth(self):
        pass

    async def forward(self, body, protocol, original=None):
        self.last_body = dict(body)
        return JSONResponse({
            "id": "chatcmpl-fake",
            "model": body.get("model"),
            "choices": [{"message": {"role": "assistant", "content": "pong"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })


class FakeRefreshProvider(FakeProvider):
    id = "fakerefresh"
    name = "Refreshable Fake Provider"

    def __init__(self):
        super().__init__()
        self._models = [{"id": "old-model", "description": "Old Model"}]
        self.refresh_calls = 0

    def models(self):
        return list(self._models)

    async def refresh_models(self, force=False):
        self.refresh_calls += 1
        self._models = [
            {"id": "old-model", "description": "Old Model"},
            {"id": "new-model", "description": "New Model"},
        ]
        return self.models()


class FakeStreamProvider(FakeProvider):
    id = "fakestream"

    def models(self):
        return [{"id": "fake-stream-model", "description": "Fake Stream Model"}]

    async def forward(self, body, protocol, original=None):
        async def gen():
            for i in range(3):
                yield f"data: {i}\n\n".encode()
        return StreamingResponse(gen(), media_type="text/event-stream")


class FakeTwinProvider(FakeProvider):
    """与 FakeProvider **同名**发布 ``fake-model``——复刻真机里 glm-5.3 的处境。

    真实例：``glm-5.3`` 在 zcode/codebuddy/qoder/trae 四个通道都发布。model_order
    因此必须按**裸模型名**分卡/落键，否则同一个名字会被拆成四份互不相干的配置。
    """

    id = "faketwin"


def _make_state(providers, tmp_path):
    client = mock.MagicMock()
    client.endpoint = "https://fake.endpoint.invalid"
    client.auth_headers.return_value = {}
    client.session = {"auth": {"accessToken": "t", "expiresAt": int((time.time() + 3600) * 1000)}}
    return SimpleNamespace(
        client=client,
        providers=providers,
        mock_dir=None,
        started_at=time.time(),
        enable_desensitize=False,
        enable_optimize_context=False,
        verbose_llm=False,
        default_provider="codebuddy",
        default_model=None,
        disabled_models=set(),
        hidden_models=set(),
        disabled_providers=set(),
        model_schedules={},
        model_order={},
        metrics=MetricsCollector(tmp_path / "metrics.jsonl"),
        write_log=mock.MagicMock(),
        ensure_auth=mock.MagicMock(),
        logger=mock.MagicMock(),
        json_logger=mock.MagicMock(),
        runtime_info={"app_version": "test", "system_version": "test",
                      "python_version": "test", "machine": "test"},
    )


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """本文件所有测试都隔离设置文件，绝不读写真实 ~/.buddy-proxy/settings.json。"""
    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(tmp_path / "settings.json"))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离的设置文件 + 假 state（含 codebuddy/fakeprov/fakestream 三个通道）。"""
    fake, fake_stream = FakeProvider(), FakeStreamProvider()
    state = _make_state({"fakeprov": fake, "fakestream": fake_stream}, tmp_path)
    monkeypatch.setattr(st, "proxy_state", state)
    client = TestClient(m.app)
    return SimpleNamespace(state=state, fake=fake, fake_stream=fake_stream, client=client)


@pytest.fixture()
def twin_env(tmp_path, monkeypatch):
    """两个通道**同名**发布 ``fake-model``（复刻 glm-5.3 那种跨通道同名模型）。

    单独一个 fixture 而不是塞进 :func:`env`：后者被大量测试断言了「目录里就是
    codebuddy/fakeprov/fakestream 三个通道」，多加一个会把它们全部带偏。
    """
    fake, twin = FakeProvider(), FakeTwinProvider()
    state = _make_state({"fakeprov": fake, "faketwin": twin}, tmp_path)
    monkeypatch.setattr(st, "proxy_state", state)
    client = TestClient(m.app)
    return SimpleNamespace(state=state, fake=fake, twin=twin, client=client)


# ---------------------------------------------------------------------------
# /ui 页面与只读 API
# ---------------------------------------------------------------------------
def _ui_source(client) -> str:
    """页面 HTML + 全部静态 JS 的合并文本。

    前端 JS 从 index.html 拆到 static/*.js 后（2026-10-03 拆分），原先
    「往 /ui 页面 HTML 里 grep 前端函数」的测试要跟到新文件；这个 helper
    把两处拼起来，保持测试语义不变。
    """
    text = client.get("/ui").text
    for name in ("benefits.js", "charts.js", "app.js"):
        r = client.get(f"/ui/{name}")
        assert r.status_code == 200, f"{name} 未正常 serve: {r.status_code}"
        text += "\n" + r.text
    return text


def test_ui_page_served(env):
    r = env.client.get("/ui")
    assert r.status_code == 200
    assert "Buddy Proxy 控制台" in r.text
    assert r.headers["content-type"].startswith("text/html")
    # 告警条幅的 DOM 在页面里、渲染函数在静态 JS 里（数据到了才 .show）
    assert 'id="alertbar"' in r.text
    assert "function renderAlert" in _ui_source(env.client)


def test_ui_static_files_served(env):
    """拆分后的静态 JS/CSS 都走白名单路由，且 no-cache。"""
    for name, ctype in (("app.js", "text/javascript"), ("benefits.js", "text/javascript"),
                        ("charts.js", "text/javascript"), ("style.css", "text/css")):
        r = env.client.get(f"/ui/{name}")
        assert r.status_code == 200, name
        assert r.headers["content-type"].startswith(ctype)
        assert r.headers.get("cache-control") == "no-cache"
    assert env.client.get("/ui/nope.txt").status_code == 404


def test_root_redirects_to_ui(env):
    assert env.client.get("/", follow_redirects=False).headers["location"] == "/ui"


def test_overview_shape(env):
    body = env.client.get("/ui/api/overview").json()
    assert body["uptime_seconds"] >= 0
    assert body["default_provider"] == "codebuddy"
    assert set(body["providers"]) == {"fakeprov", "fakestream"}
    # 设置健康度随 overview 下发（前端顶部告警条幅的数据源）
    assert "settings" in body


def test_overview_settings_health_ok(env):
    """设置文件正常时 health 为 ok——条幅不弹。"""
    settings_mod.save_settings({"default_provider": "codebuddy"})
    assert env.client.get("/ui/api/overview").json()["settings"]["ok"] is True


def test_overview_settings_health_reports_corruption(env):
    """设置文件语法错误时必须显式报告，而不是静默当作「没有设置」。

    load_settings() 为容错返回 {}，若不额外探测，用户只会看到
    「model_order / 停用 / 时段 / 默认模型一起消失」且毫无提示。
    """
    settings_mod.settings_path().write_text('{"a": [1,\n', encoding="utf-8")
    health = env.client.get("/ui/api/overview").json()["settings"]
    assert health["ok"] is False
    assert health["exists"] is True
    assert health["error"]                    # 原始报错给用户定位
    assert health["path"].endswith("settings.json")
    # 同时确认配置确实全都没生效（这正是要告警的原因）
    assert settings_mod.load_settings() == {}


def test_overview_settings_health_missing_file_is_ok(env):
    """文件不存在是正常首次启动，不该报警（会走默认值）。"""
    settings_mod.settings_path().unlink(missing_ok=True)
    health = env.client.get("/ui/api/overview").json()["settings"]
    assert health["ok"] is True
    assert health["exists"] is False


def test_models_grouped_by_provider(env):
    body = env.client.get("/ui/api/models").json()
    by_id = {g["id"]: g for g in body["groups"]}
    assert set(by_id) == {"codebuddy", "fakeprov", "fakestream"}
    assert any(m["id"] == "glm-5.3" for m in by_id["codebuddy"]["models"])
    assert any(m["id"] == "fake-model" for m in by_id["fakeprov"]["models"])


def test_only_dynamic_catalog_provider_is_refreshable(env):
    refreshable = FakeRefreshProvider()
    env.state.providers[refreshable.id] = refreshable
    groups = {g["id"]: g for g in env.client.get("/ui/api/models").json()["groups"]}
    assert groups["fakerefresh"]["refreshable"] is True
    assert groups["fakeprov"]["refreshable"] is False
    assert groups["codebuddy"]["refreshable"] is False


def test_generic_model_refresh_discovers_new_model(env):
    refreshable = FakeRefreshProvider()
    env.state.providers[refreshable.id] = refreshable
    r = env.client.post("/ui/api/models/refresh", json={"provider": "fakerefresh"})
    assert r.status_code == 200
    assert r.json()["added"] == ["new-model"]
    assert r.json()["count"] == 2
    assert refreshable.refresh_calls == 1
    groups = {g["id"]: g for g in env.client.get("/ui/api/models").json()["groups"]}
    assert {m["id"] for m in groups["fakerefresh"]["models"]} == {"old-model", "new-model"}
    assert "new-model" in {m["id"] for m in env.client.get("/v1/models").json()["data"]}


def test_generic_model_refresh_rejects_static_provider(env):
    r = env.client.post("/ui/api/models/refresh", json={"provider": "fakeprov"})
    assert r.status_code == 400
    assert "静态" in json.dumps(r.json(), ensure_ascii=False)


def test_hide_model_filters_catalog_but_does_not_block_direct_call(env):
    r = env.client.post("/ui/api/model-hidden", json={
        "provider": "fakeprov", "model": "fake-model", "hidden": True,
    })
    assert r.status_code == 200
    assert r.json() == {"ok": True, "model": "fakeprov/fake-model", "hidden": True}
    assert settings_mod.load_settings()["hidden_models"] == ["fakeprov/fake-model"]

    body = env.client.get("/ui/api/models").json()
    group = next(g for g in body["groups"] if g["id"] == "fakeprov")
    assert group["models"] == []
    assert body["hidden_models"] == [{
        "provider": "fakeprov", "model": "fake-model", "name": "Fake Model",
    }]
    assert "fake-model" not in {
        m["id"] for m in env.client.get("/v1/models").json()["data"]
        if m.get("provider") == "fakeprov"
    }
    options = env.client.get("/ui/api/model-order/options").json()["groups"]
    assert not any(g["provider"] == "fakeprov" for g in options)

    # 隐藏不是停用：显式点名照常路由。
    call = env.client.post("/v1/chat/completions", json={
        "model": "fakeprov/fake-model",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert call.status_code == 200

    restored = env.client.post("/ui/api/model-hidden", json={
        "provider": "fakeprov", "model": "fake-model", "hidden": False,
    })
    assert restored.status_code == 200
    assert settings_mod.load_settings()["hidden_models"] == []
    group = next(g for g in env.client.get("/ui/api/models").json()["groups"]
                 if g["id"] == "fakeprov")
    assert [m["id"] for m in group["models"]] == ["fake-model"]


def test_hidden_model_stays_hidden_after_catalog_refresh(env):
    refreshable = FakeRefreshProvider()
    env.state.providers[refreshable.id] = refreshable
    env.client.post("/ui/api/model-hidden", json={
        "provider": "fakerefresh", "model": "old-model", "hidden": True,
    })
    env.client.post("/ui/api/models/refresh", json={"provider": "fakerefresh"})
    group = next(g for g in env.client.get("/ui/api/models").json()["groups"]
                 if g["id"] == "fakerefresh")
    assert [m["id"] for m in group["models"]] == ["new-model"]


def test_restore_missing_hidden_key_is_allowed(env):
    env.state.hidden_models = {"fakeprov/removed-model"}
    r = env.client.post("/ui/api/model-hidden", json={
        "provider": "fakeprov", "model": "removed-model", "hidden": False,
    })
    assert r.status_code == 200
    assert env.state.hidden_models == set()


def test_codebuddyintl_models_are_an_independent_group(tmp_path, monkeypatch):
    """海外产品表只能归 codebuddyintl，不能混入默认 CodeBuddy 裸名目录。"""
    from buddy_proxy.codebuddy_provider.intl_provider import CodeBuddyIntlProvider
    from buddy_proxy.web.ui.models_api import _model_groups

    state = _make_state({"codebuddyintl": CodeBuddyIntlProvider()}, tmp_path)
    monkeypatch.setattr(st, "proxy_state", state)
    by_id = {g["id"]: g for g in _model_groups(state)}
    intl_ids = {m["id"] for m in by_id["codebuddyintl"]["models"]}
    cn_ids = {m["id"] for m in by_id["codebuddy"]["models"]}

    assert "codebuddyintl/gpt-5.6-sol" in intl_ids
    assert "codebuddyintl/gemini-3.5-flash" in intl_ids
    assert "gpt-5.6-sol" not in cn_ids
    assert "gemini-3.5-flash" not in cn_ids


def test_stats_empty(env):
    body = env.client.get("/ui/api/stats").json()
    assert body["models"] == []
    assert body["model_daily"] == []
    assert len(body["daily"]) == 30


# ---------------------------------------------------------------------------
# 设置：默认模型
# ---------------------------------------------------------------------------
def test_settings_set_and_clear_default_model(env):
    r = env.client.post("/ui/api/settings",
                        json={"default_model": "fakeprov/fake-model"})
    assert r.status_code == 200
    assert env.state.default_model == "fakeprov/fake-model"

    # 落盘 + 重启恢复
    saved = settings_mod.load_settings()
    assert saved["default_model"] == "fakeprov/fake-model"

    r = env.client.post("/ui/api/settings", json={"default_model": ""})
    assert r.status_code == 200
    assert env.state.default_model is None


def test_settings_rejects_unknown_model(env):
    r = env.client.post("/ui/api/settings",
                        json={"default_model": "fakeprov/nope"})
    assert r.status_code == 400
    r = env.client.post("/ui/api/settings",
                        json={"default_model": "nosuch/x"})
    assert r.status_code == 400


def test_default_model_fills_missing_model_field(env):
    env.client.post("/ui/api/settings", json={"default_model": "fakeprov/fake-model"})
    r = env.client.post("/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    # 前缀被剥掉后转发给 provider
    assert env.fake.last_body["model"] == "fake-model"


# ---------------------------------------------------------------------------
# 模型停用/启用
# ---------------------------------------------------------------------------
def test_model_toggle_disable_and_enable(env):
    # 停用
    r = env.client.post("/ui/api/model-toggle",
                        json={"provider": "fakeprov", "model": "fake-model", "disabled": True})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "model": "fakeprov/fake-model", "disabled": True}
    assert "fakeprov/fake-model" in env.state.disabled_models
    # 落盘
    assert settings_mod.load_settings()["disabled_models"] == ["fakeprov/fake-model"]
    # /ui/api/models 反映停用标记
    models = env.client.get("/ui/api/models").json()["groups"]
    fp = next(g for g in models if g["id"] == "fakeprov")
    assert next(m for m in fp["models"] if m["id"] == "fake-model")["disabled"] is True

    # 启用
    r = env.client.post("/ui/api/model-toggle",
                        json={"provider": "fakeprov", "model": "fake-model", "disabled": False})
    assert r.status_code == 200 and r.json()["disabled"] is False
    assert "fakeprov/fake-model" not in env.state.disabled_models


def test_model_toggle_defaults_to_flip(env):
    r1 = env.client.post("/ui/api/model-toggle",
                         json={"provider": "fakeprov", "model": "fake-model"})
    assert r1.json()["disabled"] is True
    r2 = env.client.post("/ui/api/model-toggle",
                         json={"provider": "fakeprov", "model": "fake-model"})
    assert r2.json()["disabled"] is False


def test_model_toggle_rejects_unknown(env):
    r = env.client.post("/ui/api/model-toggle",
                        json={"provider": "fakeprov", "model": "nope", "disabled": True})
    assert r.status_code == 400


def test_disabled_model_call_fails(env):
    env.client.post("/ui/api/model-toggle",
                    json={"provider": "fakeprov", "model": "fake-model", "disabled": True})
    r = env.client.post("/v1/chat/completions",
                        json={"model": "fakeprov/fake-model",
                              "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403
    assert "停用" in json.dumps(r.json(), ensure_ascii=False)
    # 启用后恢复正常
    env.client.post("/ui/api/model-toggle",
                    json={"provider": "fakeprov", "model": "fake-model", "disabled": False})
    r = env.client.post("/v1/chat/completions",
                        json={"model": "fakeprov/fake-model",
                              "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 通道停用/启用（模型页 provider 标题栏开关，2026-10-06）
# ---------------------------------------------------------------------------
def test_provider_toggle_disable_and_enable(env):
    r = env.client.post("/ui/api/provider-toggle",
                        json={"provider": "fakeprov", "disabled": True})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "provider": "fakeprov", "disabled": True}
    assert "fakeprov" in env.state.disabled_providers
    assert settings_mod.load_settings()["disabled_providers"] == ["fakeprov"]
    # /ui/api/models 反映组级停用标记（前端据此置灰）
    models = env.client.get("/ui/api/models").json()["groups"]
    fp = next(g for g in models if g["id"] == "fakeprov")
    assert fp["disabled"] is True
    other = next(g for g in models if g["id"] == "fakestream")
    assert other["disabled"] is False

    r = env.client.post("/ui/api/provider-toggle",
                        json={"provider": "fakeprov", "disabled": False})
    assert r.status_code == 200 and r.json()["disabled"] is False
    assert "fakeprov" not in env.state.disabled_providers


def test_provider_toggle_defaults_to_flip(env):
    r1 = env.client.post("/ui/api/provider-toggle", json={"provider": "fakeprov"})
    assert r1.json()["disabled"] is True
    r2 = env.client.post("/ui/api/provider-toggle", json={"provider": "fakeprov"})
    assert r2.json()["disabled"] is False


def test_provider_toggle_rejects_codebuddy(env):
    """codebuddy 是默认兜底通道，停了整个代理就没有兜底。"""
    r = env.client.post("/ui/api/provider-toggle",
                        json={"provider": "codebuddy", "disabled": True})
    assert r.status_code == 400


def test_provider_toggle_rejects_unknown(env):
    r = env.client.post("/ui/api/provider-toggle",
                        json={"provider": "nope", "disabled": True})
    assert r.status_code == 404


def test_disabled_provider_call_fails(env):
    """整通道停用：该通道所有模型调用 403（provider_disabled），/v1/models 不再列出。"""
    env.client.post("/ui/api/provider-toggle",
                    json={"provider": "fakeprov", "disabled": True})
    r = env.client.post("/v1/chat/completions",
                        json={"model": "fakeprov/fake-model",
                              "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403
    assert "通道" in json.dumps(r.json(), ensure_ascii=False)
    # 公开模型列表不再列出停用通道的模型（客户端选了也只会 403）
    ids = [m["id"] for m in env.client.get("/v1/models").json()["data"]]
    assert not any(i.startswith("fakeprov") or "/fake-model" in i for i in ids)
    # 重新打开后恢复
    env.client.post("/ui/api/provider-toggle",
                    json={"provider": "fakeprov", "disabled": False})
    r = env.client.post("/v1/chat/completions",
                        json={"model": "fakeprov/fake-model",
                              "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200


def test_disabled_provider_skipped_in_order_candidates(env):
    """候选顺序里放着停用通道：跳过它继续下一档，不把整条请求打成 403。"""
    env.state.model_order = {"fake-model": ["fakeprov/fake-model", "fakestream/fake-model"]}
    env.client.post("/ui/api/provider-toggle",
                    json={"provider": "fakeprov", "disabled": True})
    r = env.client.post("/v1/chat/completions",
                        json={"model": "fake-model",
                              "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, "停用通道应被跳过、请求落到下一档"


def test_disabled_provider_rejects_even_without_valid_model(env):
    """整通道检查排在 isinstance guard **之前**：通道停用后 model 非 str 也拦。

    review 2026-10-06：检查原本放在 guard 之后，请求没带合法 model 名时
    停用通道会被静默放过去。
    """
    from buddy_proxy.codebuddy_provider.forward import _reject_if_disabled

    env.state.disabled_providers = {"fakeprov"}
    with pytest.raises(HTTPException) as caught:
        _reject_if_disabled(env.state, "fakeprov", None)
    assert caught.value.status_code == 403
    # 未停用通道 + model 非 str：照旧静默放行（交给后续链路判定）
    env.state.disabled_providers = set()
    _reject_if_disabled(env.state, "fakeprov", None)


def test_benefits_refresh_keeps_disabled_marker(env, tmp_path):
    """refresh 端点的快照也要带 disabled 标记并过滤告警。

    review 2026-10-06：ui_benefits 带了 disabled_providers 但 refresh 还是
    无参调用，额度卡「↻」一刷，停用通道的卡片就又回来了。
    """
    env.state.providers["fakeprov"].quota = lambda: {
        "items": [{"label": "总额度", "used": 30, "total": 100,
                   "remaining": 70, "percent": 30, "reset_ts": None}],
        "level": "pro"}
    env.state.benefits = BenefitsManager(tmp_path / "c.jsonl", env.state)
    env.client.post("/ui/api/provider-toggle",
                    json={"provider": "fakeprov", "disabled": True})
    snap = env.client.post("/ui/api/benefits/refresh",
                           json={"provider": "fakeprov"}).json()
    fp = next(e for e in snap["providers"] if e["id"] == "fakeprov")
    assert fp["disabled"] is True
    # 告警（余额告急/到期）不收停用通道
    assert all(e["id"] != "fakeprov" for e in snap["low_quota"])


# ---------------------------------------------------------------------------
# 限时可用时段
# ---------------------------------------------------------------------------
def test_model_schedule_open_same_day():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Shanghai")
    at = lambda h, mi: datetime(2026, 9, 10, h, mi, tzinfo=tz).timestamp()
    w = [["12:00", "14:00"]]
    assert settings_mod.model_schedule_open(w, at(13, 0)) is True
    assert settings_mod.model_schedule_open(w, at(11, 59)) is False
    assert settings_mod.model_schedule_open(w, at(14, 0)) is False  # 上界开区间


def test_model_schedule_open_cross_midnight():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Shanghai")
    at = lambda h, mi: datetime(2026, 9, 10, h, mi, tzinfo=tz).timestamp()
    w = [["22:00", "08:00"]]
    assert settings_mod.model_schedule_open(w, at(23, 0)) is True
    assert settings_mod.model_schedule_open(w, at(3, 0)) is True
    assert settings_mod.model_schedule_open(w, at(8, 0)) is False
    assert settings_mod.model_schedule_open(w, at(12, 0)) is False


def test_model_schedule_open_empty_and_multi():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Shanghai")
    at = lambda h, mi: datetime(2026, 9, 10, h, mi, tzinfo=tz).timestamp()
    assert settings_mod.model_schedule_open([], at(12, 0)) is False
    w = [["22:00", "08:00"], ["12:00", "14:00"]]
    assert settings_mod.model_schedule_open(w, at(13, 0)) is True
    assert settings_mod.model_schedule_open(w, at(16, 0)) is False


def test_normalize_windows():
    assert settings_mod.normalize_windows([["22:00", "08:00"], ["12:00", "14:00"]]) == \
        [["22:00", "08:00"], ["12:00", "14:00"]]
    # 非法 HH:MM / 零长度 / 结构错误一律丢弃
    assert settings_mod.normalize_windows(
        [["25:00", "08:00"], ["9:5", "10:00"], ["12:00", "12:00"], "x", ["a", "b"]]) == []
    # 非 list 输入
    assert settings_mod.normalize_windows(None) == []
    # 超量截断到 8
    assert len(settings_mod.normalize_windows(
        [[f"0{i}:00", f"0{i}:30"] for i in range(1, 10)])) == 8


def test_model_schedule_set_and_clear(env):
    r = env.client.post("/ui/api/model-schedule",
                        json={"provider": "fakeprov", "model": "fake-model",
                              "windows": [["22:00", "08:00"]]})
    assert r.status_code == 200
    assert r.json()["windows"] == [["22:00", "08:00"]]
    assert env.state.model_schedules["fakeprov/fake-model"] == [["22:00", "08:00"]]
    # 持久化格式：每键 {"windows": [...]}
    saved = settings_mod.load_settings()["model_schedules"]
    assert saved["fakeprov/fake-model"] == {"windows": [["22:00", "08:00"]]}
    # models API 透出 schedule
    fp = next(g for g in env.client.get("/ui/api/models").json()["groups"]
              if g["id"] == "fakeprov")
    assert next(m for m in fp["models"] if m["id"] == "fake-model")["schedule"]["windows"] \
        == [["22:00", "08:00"]]
    # 清空窗口 → 删除键
    r = env.client.post("/ui/api/model-schedule",
                        json={"provider": "fakeprov", "model": "fake-model", "windows": []})
    assert r.status_code == 200
    assert "fakeprov/fake-model" not in env.state.model_schedules


def test_model_schedule_rejects_unknown(env):
    r = env.client.post("/ui/api/model-schedule",
                        json={"provider": "fakeprov", "model": "nope",
                              "windows": [["22:00", "08:00"]]})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 候选上游顺序（model_order）
# ---------------------------------------------------------------------------
def test_model_order_set_persist_and_expose(env):
    """设置顺序：落**裸名键**、写盘（直接存字符串数组）、并在 /ui/api/models 透出。"""
    r = env.client.post("/ui/api/model-order",
                        json={"provider": "fakeprov", "model": "fake-model",
                              "targets": [{"provider": "fakeprov", "model": "fake-model"},
                                          {"provider": "fakestream",
                                           "model": "fake-stream-model"}]})
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "fake-model"
    assert r.json()["targets"] == ["fakeprov/fake-model", "fakestream/fake-stream-model"]
    # 键是**裸模型名**（提交里的 provider 只用于校验，不参与落键）：一个模型名一份
    # 配置，运行时对所有发布它的通道都生效。
    assert env.state.model_order["fake-model"] == [
        "fakeprov/fake-model", "fakestream/fake-stream-model"]
    # 存储形状与手写配置一致：直接是数组，不包一层
    saved = settings_mod.load_settings()["model_order"]
    assert saved["fake-model"] == [
        "fakeprov/fake-model", "fakestream/fake-stream-model"]

    fp = next(g for g in env.client.get("/ui/api/models").json()["groups"]
              if g["id"] == "fakeprov")
    got = next(m for m in fp["models"] if m["id"] == "fake-model")["order"]
    assert got["targets"] == ["fakeprov/fake-model", "fakestream/fake-stream-model"]
    assert got["marks"] == []


def test_model_order_key_is_bare_and_shared_across_channels(twin_env):
    """一个模型名 = 一条配置 = 一张卡片，**不按通道拆键**。

    真机现象：用户在同一模型名下能看到 4 张几乎一样的卡片（glm-5.3 在
    zcode/codebuddy/qoder/trae 都发布），因为键里带了归属通道。而用户写配置时
    表达的是「指定 ``deepseek-v4.1-flash`` 时按这个顺序选通道」，一条就够——
    运行时裸键对所有发布它的通道都生效（转发侧 ``forward`` 也认裸键）。

    所以：提交带哪个 provider 都不落进键里；同名模型在任意通道的目录里都应能
    看到同一条 order；清空/改一次就是改全部。
    """
    # fake-model 同时被 fakeprov / faketwin 发布，提交里故意写「另一个」通道
    r = twin_env.client.post(
        "/ui/api/model-order",
        json={"provider": "faketwin", "model": "fake-model",
              "targets": [{"provider": "faketwin", "model": "fake-model"},
                          {"provider": "fakeprov", "model": "fake-model"}]})
    assert r.status_code == 200, r.text
    # 键是裸名，与提交里写的通道无关
    assert r.json()["model"] == "fake-model"
    assert set(twin_env.state.model_order) == {"fake-model"}

    # 两个通道的目录里看到的是**同一条**配置（前端据此只画一张卡）
    groups = twin_env.client.get("/ui/api/models").json()["groups"]
    seen = []
    for g in groups:
        for m in g["models"]:
            if m["id"].split("/")[-1] == "fake-model" and m.get("order"):
                seen.append((g["id"], m["order"]["targets"]))
    assert {k for k, _ in seen} == {"fakeprov", "faketwin"}, f"两个通道都应看到这条顺序，实际 {seen}"
    assert len({tuple(v) for _, v in seen}) == 1, "两个通道必须看到同一份 targets"

    # 同一名字改一次（不带通道前缀）就是改这一条，不会分裂出第二个键
    twin_env.client.post("/ui/api/model-order",
                         json={"model": "fake-model",
                               "targets": [{"provider": "fakeprov", "model": "fake-model"}]})
    assert set(twin_env.state.model_order) == {"fake-model"}
    assert twin_env.state.model_order["fake-model"] == ["fakeprov/fake-model"]


def test_model_order_accepts_prefixed_model_and_stores_bare(env):
    """提交里带着 ``<通道>/<名>`` 也不落前缀：那是老配置的键形态，得能原地保存。

    真机场景：老配置里键是 ``qoder/glm-5.3``，顺序页照着配置原样展示（标题就是
    那个串），用户改完点保存，提交回来的是带前缀的名字。若照单全收，落下的键会
    变成 ``qoder/qoder/glm-5.3``——转发侧永远查不到的死键；若拿它去目录里校验，
    则直接 400「模型 qoder/glm-5.3 不在 … 的模型列表中」。
    """
    # fakeprov 不在 KNOWN_PROVIDER_IDS 里（那是**真实**通道名表），所以这里用
    # 别名形态：workbuddy 是 codebuddy 的旧称，会被归一后走前缀拆分那条路。
    r = env.client.post(
        "/ui/api/model-order",
        json={"model": "fakeprov/fake-model",
              "targets": [{"provider": "fakeprov", "model": "fake-model"}]})
    # fakeprov 不是已知通道名 → 不拆前缀 → 按「没有通道发布这个名字」拒绝。
    # 这条同时说明了「见斜杠就拆」是错的：普通模型 id 里合法地带斜杠。
    assert r.status_code == 400
    assert "fakeprov/fake-model" in r.text

    # 而真实通道名做前缀时正常剥掉并保存
    r = env.client.post(
        "/ui/api/model-order",
        json={"model": "codebuddy/fake-model",
              "targets": [{"provider": "fakeprov", "model": "fake-model"}]})
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "fake-model", "键必须只剩裸名"
    assert list(env.state.model_order) == ["fake-model"]


def test_model_order_save_collapses_stale_prefixed_sibling(env):
    """保存一个模型时，把它的遗留 ``<通道>/<名>`` 旧键一起收掉。

    老配置里同一个模型可能两条键都在（用户手改过、或从旧版本升上来）。留着的话
    页面上就是两张卡、配置里两条规则，而哪条生效取决于转发侧的查找顺序——用户
    改了一条却发现行为没变。保存即收敛成一条。
    """
    # 先造出两条：老式带前缀的（前缀是**真实通道名**，老 UI 落的就是这个形态），
    # 和新的裸名
    env.state.model_order = {
        "codebuddy/fake-model": ["fakeprov/fake-model"],
        "fake-model": ["fakestream/fake-stream-model"],
        # 不同模型的带前缀键不该被误伤
        "codebuddy/other-model": ["codebuddy/other-model"],
    }
    settings_mod.save_settings({"model_order": env.state.model_order})

    r = env.client.post("/ui/api/model-order",
                        json={"provider": "fakeprov", "model": "fake-model",
                              "targets": [{"provider": "fakeprov", "model": "fake-model"}]})
    assert r.status_code == 200, r.text
    assert "codebuddy/fake-model" not in env.state.model_order, "同名的遗留旧键应收掉"
    assert env.state.model_order["fake-model"] == ["fakeprov/fake-model"]
    assert "codebuddy/other-model" in env.state.model_order, "别的模型不受影响"
    # 落盘的也要是收好的那一份
    saved = settings_mod.load_settings()["model_order"]
    assert "codebuddy/fake-model" not in saved


def test_model_order_prefixed_only_for_known_channels(env):
    """只有**认得的通道名**才当前缀剥；``openrouter/xxx`` 这类普通模型 id 原样保留。

    见不得斜杠就拆会把合法模型名拆坏——``provider/model`` 本身就是一种合法的
    plain model id（routing 那边有同样的规则）。
    """
    r = env.client.post(
        "/ui/api/model-order",
        json={"model": "openrouter/gpt-5",
              "targets": [{"provider": "fakeprov", "model": "fake-model"}]})
    assert r.status_code == 400
    # 400 的理由是「没有通道发布这个名字」，而不是「fakeprov/gpt-5 不存在」——
    # 说明它没被当成通道前缀拆开（拆了就会去找 gpt-5）
    assert "没有通道发布名为 openrouter/gpt-5 的模型" in r.text


def test_model_order_clear_works_without_provider(env):
    """省略 provider 也能清：路由/前端都不该被迫知道「归属通道」。"""
    env.client.post("/ui/api/model-order",
                    json={"model": "fake-model",
                          "targets": [{"provider": "fakeprov", "model": "fake-model"}]})
    assert "fake-model" in env.state.model_order
    r = env.client.post("/ui/api/model-order", json={"model": "fake-model", "targets": []})
    assert r.status_code == 200, r.text
    assert "fake-model" not in env.state.model_order


def test_model_order_rejects_model_no_channel_publishes(env):
    """裸名键也得是**真实存在**的模型名，否则 400（避免写入永不生效的死键）。"""
    r = env.client.post("/ui/api/model-order",
                        json={"model": "no-such-model",
                              "targets": [{"provider": "fakeprov", "model": "fake-model"}]})
    assert r.status_code == 400
    assert "没有通道发布名为 no-such-model 的模型" in r.text


def test_model_order_get_lists_exactly_the_config_keys(env):
    """顺序页的数据源 = ``model_order`` 本身：配几个键就列几条，不多不少。

    真机现象：页面上同一模型名出现四张重复卡片。根因是渲染源走的是通道目录
    （``/ui/api/models``），同一个名字被四个通道各标一份 order。修法是把渲染源
    换成这个接口——它原样返回配置里的键，所以「配置什么就展示什么」。

    这条同时钉死「**不按通道展开**」：一个键只出现在一条 item 里。
    """
    env.client.post("/ui/api/model-order",
                    json={"model": "fake-model",
                          "targets": [{"provider": "fakeprov", "model": "fake-model"},
                                      {"provider": "fakestream",
                                       "model": "fake-stream-model"}]})
    body = env.client.get("/ui/api/model-order").json()
    assert [it["model"] for it in body["items"]] == ["fake-model"]
    assert body["items"][0]["targets"] == ["fakeprov/fake-model",
                                           "fakestream/fake-stream-model"]
    assert body["items"][0]["marks"] == []

    # 「+ 新增模型」选完还没保存时，配置里没有这个键 → 接口也不该凭空列出来
    env.client.post("/ui/api/model-order", json={"model": "fake-model", "targets": []})
    assert env.client.get("/ui/api/model-order").json()["items"] == []


def test_model_order_get_reports_cooldown_marks(env):
    """冷却状态随同一个接口下发，页面据此显示 ⏸（不改变键的集合）。"""
    from buddy_proxy.core import cooldown as cooldown_mod

    cooldown_mod._reset_for_tests()
    try:
        env.client.post("/ui/api/model-order",
                        json={"model": "fake-model",
                              "targets": [{"provider": "fakeprov", "model": "fake-model"},
                                          {"provider": "fakestream",
                                           "model": "fake-stream-model"}]})
        cooldown_mod.mark_failed("fakestream", "fake-stream-model", status=502)
        items = env.client.get("/ui/api/model-order").json()["items"]
        assert [it["model"] for it in items] == ["fake-model"]
        assert [m["target"] for m in items[0]["marks"]] == ["fakestream/fake-stream-model"]
        assert items[0]["marks"][0]["cooldown_s"] > 0
    finally:
        cooldown_mod._reset_for_tests()


def test_order_page_renders_from_config_not_channel_catalog(env):
    """顺序页的渲染源必须是配置接口，**不能**再遍历通道目录。

    这是「页面里全是重复的」那次的根因：卡片来自 `MODELS.groups` 的每个 通道+模型
    组合，同名模型在 N 个通道就被画 N 张。这条把渲染源钉在 `/ui/api/model-order`。
    """
    ui = _ui_source(env.client)
    pstart = ui.index("function pageRows()")
    pbody = ui[pstart:ui.index("\n}", pstart)]
    assert "orderRows()" in pbody, "卡片应来自 model_order 的键"
    assert "MODELS" not in pbody, "渲染源不能再碰通道目录（那会按通道拆成重复卡片）"
    # orderRows 读的是新接口
    rstart = ui.index("function orderRows()")
    rbody = ui[rstart:ui.index("\n}", rstart)]
    assert "ORDER" in rbody and "items" in rbody
    # 不再有「遍历目录建卡」的旧函数
    assert "function orderedModels()" not in ui, "按通道展开的旧渲染源应已删除"


def test_model_order_exposes_cooldown_marks(env):
    """被冷却的目标要在 /ui/api/models 的 order.marks 里可见（供前端显示 ⏸）。"""
    from buddy_proxy.core import cooldown as cooldown_mod

    cooldown_mod._reset_for_tests()
    try:
        env.client.post("/ui/api/model-order",
                        json={"provider": "fakeprov", "model": "fake-model",
                              "targets": [{"provider": "fakeprov", "model": "fake-model"},
                                          {"provider": "fakestream",
                                           "model": "fake-stream-model"}]})
        cooldown_mod.mark_failed("fakestream", "fake-stream-model", status=502)
        fp = next(g for g in env.client.get("/ui/api/models").json()["groups"]
                  if g["id"] == "fakeprov")
        got = next(m for m in fp["models"] if m["id"] == "fake-model")["order"]
        assert [k["target"] for k in got["marks"]] == ["fakestream/fake-stream-model"]
        assert got["marks"][0]["cooldown_s"] > 0
    finally:
        cooldown_mod._reset_for_tests()


def test_model_order_clear_removes_key_and_marks(env):
    """targets 为空 → 删键 + 清该模型的冷却标记。"""
    from buddy_proxy.core import cooldown as cooldown_mod

    cooldown_mod._reset_for_tests()
    try:
        env.client.post("/ui/api/model-order",
                        json={"provider": "fakeprov", "model": "fake-model",
                              "targets": [{"provider": "fakestream",
                                           "model": "fake-stream-model"}]})
        cooldown_mod.mark_failed("fakestream", "fake-stream-model")
        r = env.client.post("/ui/api/model-order",
                            json={"provider": "fakeprov", "model": "fake-model",
                                  "targets": []})
        assert r.status_code == 200
        assert r.json()["marks_cleared"] == 1
        assert "fake-model" not in env.state.model_order
        assert "fake-model" not in (settings_mod.load_settings()
                                    .get("model_order") or {})
        assert cooldown_mod.is_marked("fakestream", "fake-stream-model") is False
    finally:
        cooldown_mod._reset_for_tests()


def test_model_order_rejects_unknown_target(env):
    """目标必须真实存在于该通道的模型列表，否则 400（避免写入死键）。"""
    r = env.client.post("/ui/api/model-order",
                        json={"provider": "fakeprov", "model": "fake-model",
                              "targets": [{"provider": "fakeprov", "model": "nope"}]})
    assert r.status_code == 400
    assert "不在 fakeprov 通道的模型列表中" in r.text


def test_model_order_rejects_unknown_provider_target(env):
    r = env.client.post("/ui/api/model-order",
                        json={"provider": "fakeprov", "model": "fake-model",
                              "targets": [{"provider": "ghost", "model": "m"}]})
    assert r.status_code == 400


def test_model_order_mark_clear_endpoint(env):
    """mark-clear：清该模型候选列表里全部目标的冷却标记，不动顺序。"""
    from buddy_proxy.core import cooldown as cooldown_mod

    cooldown_mod._reset_for_tests()
    try:
        env.client.post("/ui/api/model-order",
                        json={"provider": "fakeprov", "model": "fake-model",
                              "targets": [{"provider": "fakeprov", "model": "fake-model"},
                                          {"provider": "fakestream",
                                           "model": "fake-stream-model"}]})
        cooldown_mod.mark_failed("fakeprov", "fake-model")
        cooldown_mod.mark_failed("fakestream", "fake-stream-model")
        r = env.client.post("/ui/api/model-order/mark-clear",
                            json={"provider": "fakeprov", "model": "fake-model"})
        assert r.status_code == 200
        assert r.json()["marks_cleared"] == 2
        assert cooldown_mod.snapshot() == {}
        # 顺序不受影响
        assert env.state.model_order["fake-model"]
    finally:
        cooldown_mod._reset_for_tests()


def test_model_order_options_lists_selectable_targets(env):
    """选择器数据源：每个通道给出可选的模型名，且是「可写进 model_order 的规范名」。

    这是「添加目标时能选」的后端支撑——前端下拉/以数据列表渲染它。
    """
    body = env.client.get("/ui/api/model-order/options").json()
    by_provider = {g["provider"]: g["models"] for g in body["groups"]}
    assert set(by_provider) == {"codebuddy", "fakeprov", "fakestream"}

    fake = by_provider["fakeprov"][0]
    assert fake["id"] == "fake-model"
    # target 必须是 settings.model_key 口径（provider/model），否则前端拼错写进去就是死条目
    assert fake["target"] == settings_mod.model_key("fakeprov", "fake-model")
    assert "fake-stream-model" in [m["id"] for m in by_provider["fakestream"]]

    # 名称与 id 不同才带 label（给人看的名）；fake-model 的 description 是 "Fake Model"
    assert fake.get("label") == "Fake Model"


def test_model_order_options_targets_are_accepted_by_setter(env):
    """闭环：选择器给出的 target 拆开后喂给 POST /ui/api/model-order 必须被接受。

    这条防止两个接口漂移——如果 options 返回的名字不是保存接口认的名字，
    用户「选了却保存失败」（或更糟：静默写进一个永不生效的键）。
    """
    groups = env.client.get("/ui/api/model-order/options").json()["groups"]
    targets = []
    for g in groups:
        for m in g["models"]:
            target = m["target"]
            assert target.startswith(g["provider"] + "/")
            targets.append({"provider": target.split("/", 1)[0],
                            "model": target.split("/", 1)[1]})
    assert targets, "选择器不该是空的"
    # 选择器会列出全部模型（可能上百个），而 model_order 单键上限 _MAX_ORDER_TARGETS。
    # 这里只取前几个验证「选出来的名字保存接口认」——超量截断是 normalize_order 的事，
    # 已由 tests/test_model_order.py 覆盖。
    targets = targets[: settings_mod._MAX_ORDER_TARGETS]
    r = env.client.post("/ui/api/model-order",
                        json={"provider": "fakeprov", "model": "fake-model",
                              "targets": targets})
    assert r.status_code == 200, r.text
    # 后端返回规范化后的目标，供前端提示「已规范化」
    assert r.json()["targets"] == [t["provider"] + "/" + t["model"] for t in targets]


class FakeAliasProvider(FakeProvider):
    """模拟 qoder：**对外发布的 id** 与 ``resolve_model`` 的转发期映射不是一回事。

    真实例：目录发布 ``glm-5.3``，而 ``resolve_model("glm-5.3") -> "gmodel"``（上游内部
    key）。保存候选顺序时必须按发布名校验，否则「选择器/``/v1/models`` 都确认存在」的
    目标会被 400 拒掉。
    """

    id = "fakealias"
    name = "Fake Alias Provider"

    def models(self):
        return [{"id": "fakealias/glm-5.3", "description": "GLM 5.3"}]

    def resolve_model(self, model):
        return "gmodel" if model == "glm-5.3" else model


@pytest.fixture()
def alias_env(tmp_path, monkeypatch):
    """带「发布名 ≠ 转发期 key」通道的假 state（复现 qoder 的 400）。"""
    alias = FakeAliasProvider()
    state = _make_state({"fakealias": alias}, tmp_path)
    monkeypatch.setattr(st, "proxy_state", state)
    return SimpleNamespace(state=state, alias=alias, client=TestClient(m.app))


def test_model_order_accepts_published_name_not_resolve_model_output(alias_env):
    """回归：候选目标按**对外发布名**校验，而不是通道 ``resolve_model`` 的返回值。

    真机现象（PR #46 合并后实测）：``POST /ui/api/model-order`` 带
    ``{"provider": "qoder", "model": "glm-5.3"}`` 返回 400「模型 gmodel 不在 qoder
    通道的模型列表中」——``_normalize_model_ref`` 先按转发期映射把名字换成上游内部
    key，再去比对按发布名登记的目录，必然失败。选择器给出的、``/v1/models`` 也确认
    存在的目标必须能存进去。
    """
    r = alias_env.client.post(
        "/ui/api/model-order",
        json={"provider": "fakealias", "model": "glm-5.3",
              "targets": [{"provider": "fakealias", "model": "glm-5.3"}]})
    assert r.status_code == 200, r.text
    # 目标存的是发布名（转发侧会自己做同样的归一），不是 gmodel
    assert r.json()["targets"] == ["fakealias/glm-5.3"]
    # 键是裸名（与提交的 model 一致）
    assert r.json()["model"] == "glm-5.3"
    assert alias_env.state.model_order["glm-5.3"] == ["fakealias/glm-5.3"]

    # 键也要能与 /ui/api/models 暴露的键对上（否则前端徽标永远不显示）
    models = alias_env.client.get("/ui/api/models").json()["groups"]
    ids = {m["id"] for g in models if g["id"] == "fakealias" for m in g["models"]}
    assert "fakealias/glm-5.3" in ids
    # 清标记端点同一口径：能查到刚存的键
    r = alias_env.client.post("/ui/api/model-order/mark-clear",
                              json={"provider": "fakealias", "model": "glm-5.3"})
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "fakealias/glm-5.3"


def test_mark_clear_does_not_validate_against_catalog(alias_env):
    """清冷却是纯缓存操作，目录里没有这个名字也不该 400。

    真机场景：qoder 的标记可能落在上游内部 key 上（``resolve_model`` 的产物），
    而``_canonical_order_target`` 只认对外发布名。若这里跟着 400，用户点「清冷却」
    会看到「模型不在通道的模型列表中」——像是保存失败，其实什么都没坏。
    """
    from buddy_proxy.core import cooldown as cooldown_mod

    cooldown_mod._reset_for_tests()
    try:
        # fakealias 目录里只有 glm-5.3，没有 gmodel（它是 resolve_model 的产物）
        cooldown_mod.mark_failed("fakealias", "gmodel")
        r = alias_env.client.post("/ui/api/model-order/mark-clear",
                                  json={"provider": "fakealias", "model": "gmodel"})
        assert r.status_code == 200, r.text
        assert r.json()["marks_cleared"] == 1
        assert cooldown_mod.snapshot() == {}
        # 未知通道同理：清不到就返回 0，不报错
        r = alias_env.client.post("/ui/api/model-order/mark-clear",
                                  json={"provider": "ghost", "model": "m"})
        assert r.status_code == 200
        assert r.json()["marks_cleared"] == 0
    finally:
        cooldown_mod._reset_for_tests()


def test_order_provider_list_not_from_MODELS(env):
    """顺序页的通道下拉必须来自选项接口，不能依赖 MODELS。

    MODELS 只在「模型」标签页加载过才有值；若从这里取通道列表，直接进顺序页
    时下拉框会是空的（下拉即不可用），等于「添加时不能选」。这条把它钉死。
    """
    ui = _ui_source(env.client)
    start = ui.index("async function ensureOrderOptions(")
    end = ui.index("\n}", start)
    body = ui[start:end]
    assert "Object.keys(ORDER_OPTIONS)" in body, "通道列表应优先来自选项接口"
    # 兜底可以读 MODELS，但必须在 ORDER_OPTIONS 为空之后，且带 null 守卫
    assert "MODELS && MODELS.groups" in body
    # 选项接口本身要有数据，否则下拉依旧是空的
    opts = env.client.get("/ui/api/model-order/options").json()["groups"]
    assert {g["provider"] for g in opts} == {"codebuddy", "fakeprov", "fakestream"}


def test_model_table_has_no_inline_order_entry(env):
    """模型表里不该再有行内「顺序」按钮或它的弹窗。

    用户报告过：功能做在那张表的操作列里，行多列窄，根本找不到（而且表里的
    配置项一多，「顺序·3」这种短标签也读不出是什么）。编辑入口统一到
    「模型顺序」页签，这里只留一个徽标说明「已配几档」。
    """
    ui = _ui_source(env.client)
    assert "openOrderModal" not in ui, "行内顺序弹窗应已删除"
    assert "async function saveOrder(" not in ui, "弹窗的保存函数应已删除"
    # 徽标保留：模型表仍要知道这个模型配了几档、几档在冷却
    assert "ordTag" in ui and "⇄ " in ui, "「已配顺序」的徽标不该一起删掉"
    assert "clearOrderMarks" in ui, "「清冷却」按钮是这张表唯一的顺序相关操作，应保留"


def test_order_page_cards_have_independent_model_test_button(env):
    """模型顺序卡片可直接测试裸模型名，按钮点击不应触发展开/折叠。"""
    ui = _ui_source(env.client)
    start = ui.index("function orderPageCard(r)")
    body = ui[start:ui.index("\n}", start)]
    assert "class=\"primary order-test\" onclick=\"runTest('','${esc(key)}')\"" in body
    assert '<div class="order-head-main" onclick="orderToggle(' in body
    assert '<div class="order-head" onclick="orderToggle(' not in body


def test_order_page_renders_pending_draft_cards(env):
    """顺序页的渲染源必须**包含**「还没保存过、正在编辑」的模型。

    真机现象：用户「+ 新增模型」选 kimi-k3 完全没反应。`orderAddModelPick` 确实
    写好了 `ORDER_OPEN` / `ORDER_DRAFT`，但页面只从 `orderedModels()`（= 只收
    `m["order"]` 存在的、**已配置**的模型）渲染，未曾保存过的模型卡片永远建不
    出来，看起来就是点了没反应。新增的 `pageRows()` 把草稿里的键并进渲染源。
    """
    ui = _ui_source(env.client)
    # 页面渲染走 pageRows，而不是只认已配置的 orderedModels
    start = ui.index("function renderOrderPage()")
    body = ui[start:ui.index("\n}", start)]
    assert "pageRows()" in body, "渲染源要用 pageRows（已配置 ∪ 编辑中的草稿）"
    # pageRows 必须并入 ORDER_DRAFT 的键
    pstart = ui.index("function pageRows()")
    pbody = ui[pstart:ui.index("\n}", pstart)]
    assert "ORDER_DRAFT" in pbody, "pageRows 要把草稿里的键并进来"
    # 选中即展开（否则用户还得再点一下才知道加上了）
    astart = ui.index("function orderAddModelPick(")
    abody = ui[astart:ui.index("\n}", astart)]
    assert "ORDER_OPEN.add(key)" in abody, "选中的模型应自动展开"
    assert "renderOrderPage()" in abody, "选完要重绘（否则卡片不出现）"


def test_order_save_wont_post_empty_targets_when_dom_desynced(env):
    """保存必须区分「用户真的清空了」和「界面状态不可信」，后者绝不发空 targets。

    真机现象：`orderPageSync` 找不到 `.order-rows` 容器时直接 return，折叠卡片
    上那时残留的 `[]`（刷新后 orderPage 的 not-open 分支会把它当占位写回草稿）
    就被当成用户输入 POST 出去，服务端配置**静默抹平**——实测
    qoder/qwen3.8-max 的 `["qoder/qwen3.8-max","trae/qwen3.8-max"]` 就这样没了。
    """
    ui = _ui_source(env.client)
    start = ui.index("function orderPageIntendedItems(")
    body = ui[start:ui.index("\n}", start)]
    # 折叠态：只有草稿存在才认它（那是这轮编辑的结果），没有草稿就是不可信
    assert "ORDER_DRAFT[key] ? ORDER_DRAFT[key].filter" in body, "折叠态要区分「编辑过」与「没展开过」"
    assert "return null" in body, "不可信时必须返回 null，不能悄悄当成空列表"
    sstart = ui.index("async function orderPageSave(")
    sbody = ui[sstart:ui.index("\n}", sstart)]
    assert "orderPageIntendedItems(key)" in sbody and "=== null" in sbody, (
        "保存要拦住不可信状态，而不是发出去"
    )
    # 「清空」是显式意图：绕开依赖 DOM 的 orderPageSync，且不可逆、要先问一句
    cstart = ui.index("async function orderPageClear(")
    cbody = ui[cstart:ui.index("\n}", cstart)]
    assert "orderPageSync" not in cbody, "清空应显式表达意图，不靠 DOM 反写"
    assert "confirm(" in cbody, "清空是不可逆操作，得先问一句"


def test_scheduled_model_call_blocked_outside_window(env, monkeypatch):
    # 设一个「此刻一定不在」的窗口：固定判定时刻为 12:00，窗口设 03:00~04:00
    from datetime import datetime
    from zoneinfo import ZoneInfo
    noon = datetime(2026, 9, 10, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    monkeypatch.setattr(settings_mod.time, "time", lambda: noon)
    env.client.post("/ui/api/model-schedule",
                    json={"provider": "fakeprov", "model": "fake-model",
                          "windows": [["03:00", "04:00"]]})
    r = env.client.post("/v1/chat/completions",
                        json={"model": "fakeprov/fake-model",
                              "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403
    assert r.json()["detail"]["error"]["type"] == "model_scheduled"
    # 改成覆盖此刻的窗口 → 放行
    env.client.post("/ui/api/model-schedule",
                    json={"provider": "fakeprov", "model": "fake-model",
                          "windows": [["11:00", "13:00"]]})
    r = env.client.post("/v1/chat/completions",
                        json={"model": "fakeprov/fake-model",
                              "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200


def test_disabled_takes_priority_over_schedule(env, monkeypatch):
    # 同时停用 + 设一个覆盖此刻的开放窗口：停用优先，仍 403 model_disabled
    from datetime import datetime
    from zoneinfo import ZoneInfo
    noon = datetime(2026, 9, 10, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    monkeypatch.setattr(settings_mod.time, "time", lambda: noon)
    env.client.post("/ui/api/model-schedule",
                    json={"provider": "fakeprov", "model": "fake-model",
                          "windows": [["11:00", "13:00"]]})
    env.client.post("/ui/api/model-toggle",
                    json={"provider": "fakeprov", "model": "fake-model", "disabled": True})
    r = env.client.post("/v1/chat/completions",
                        json={"model": "fakeprov/fake-model",
                              "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403
    assert r.json()["detail"]["error"]["type"] == "model_disabled"


# ---------------------------------------------------------------------------
# 一键测试
# ---------------------------------------------------------------------------
def test_ui_test_ok(env):
    body = env.client.post("/ui/api/test",
                           json={"provider": "fakeprov", "model": "fake-model"}).json()
    assert body["ok"] is True
    assert body["provider"] == "fakeprov"
    assert body["content"] == "pong"
    assert body["usage"]["prompt_tokens"] == 3
    assert env.fake.last_body["model"] == "fake-model"


def test_ui_test_strips_doubled_channel_prefix(env):
    """管理页回传带前缀的 model + 单独的 provider（海外通道列表就是这形状）：
    不能拼成 fakeprov/fakeprov/fake-model 发给上游（上游见双前缀 → 11102）。"""
    body = env.client.post("/ui/api/test",
                           json={"provider": "fakeprov",
                                 "model": "fakeprov/fake-model"}).json()
    assert body["ok"] is True
    assert env.fake.last_body["model"] == "fake-model"


def test_ui_test_reports_provider_selected_by_model_order(env):
    env.state.model_order = {"fake-model": ["fakeprov/fake-model"]}
    body = env.client.post("/ui/api/test",
                           json={"provider": "", "model": "fake-model"}).json()
    assert body["ok"] is True
    assert body["provider"] == "fakeprov"
    assert body["model"] == "fake-model"


def test_model_test_result_shows_provider_with_reply_model(env):
    ui = _ui_source(env.client)
    assert "const replyProvider = String(r.provider || provider || '')" in ui
    assert "!replyModel.startsWith(replyProvider + '/')" in ui
    assert "${esc(replyTarget)}" in ui


def test_ui_test_provider_error(env):
    class ErrProvider(FakeProvider):
        id = "fakeerr"

        async def forward(self, body, protocol, original=None):
            raise HTTPException(status_code=401, detail={"error": {"message": "token 过期"}})

    env.state.providers["fakeerr"] = ErrProvider()
    body = env.client.post("/ui/api/test",
                           json={"provider": "fakeerr", "model": "fake-model"}).json()
    assert body["ok"] is False
    assert body["status"] == 401
    assert "token 过期" in body["error"]


# ---------------------------------------------------------------------------
# 指标收集
# ---------------------------------------------------------------------------
def test_metrics_recorded_on_nonstream(env):
    env.client.post("/ui/api/test", json={"provider": "fakeprov", "model": "fake-model"})
    snap = env.state.metrics.snapshot()
    top = snap["models"][0]
    assert (top["provider"], top["model"]) == ("fakeprov", "fake-model")
    assert top["count"] == 1
    assert top["prompt_tokens"] == 3 and top["completion_tokens"] == 2
    assert snap["summary"]["total_24h"] == 1


def test_metrics_recorded_on_stream(env):
    r = env.client.post("/v1/chat/completions",
                        json={"model": "fakestream/fake-stream-model",
                              "messages": [{"role": "user", "content": "hi"}],
                              "stream": True})
    assert "text/event-stream" in r.headers["content-type"]
    snap = env.state.metrics.snapshot()
    top = snap["models"][0]
    assert (top["provider"], top["model"]) == ("fakestream", "fake-stream-model")
    # 最近请求里能看到 chunk 计数
    recent = snap["recent"][0]
    assert recent["stream"] is True and recent["chunk_count"] == 3


def test_metrics_error_recorded(env):
    env.client.post("/ui/api/test", json={"provider": "fakeerr", "model": "x"}) \
        if "fakeerr" in env.state.providers else None
    # 未启用 provider 的测试 → forward_chat 路由兜底到 codebuddy 会失败，这里只验证
    # MetricsCollector 自身的错误聚合
    env.state.metrics.record(provider="fakeprov", model="fake-model",
                             status=500, error="boom", duration_ms=5)
    snap = env.state.metrics.snapshot()
    m = next(x for x in snap["models"] if x["model"] == "fake-model")
    assert m["errors"] >= 1


def test_metrics_persist_and_reload(tmp_path):
    path = tmp_path / "metrics.jsonl"
    m1 = MetricsCollector(path)
    m1.record(provider="zcode", model="glm-5.3", protocol="openai",
              status=200, duration_ms=120, prompt_tokens=7, completion_tokens=9,
              ttft_ms=45, cached_tokens=3, credit=0.5)
    m1.record(provider="trae", model="glm-5.2", protocol="openai",
              status=429, duration_ms=30, error="rate limited")
    # 模拟重启：重新加载同一个文件
    m2 = MetricsCollector(path)
    snap = m2.snapshot()
    by_model = {(x["provider"], x["model"]): x for x in snap["models"]}
    assert by_model[("zcode", "glm-5.3")]["count"] == 1
    assert by_model[("zcode", "glm-5.3")]["completion_tokens"] == 9
    assert by_model[("trae", "glm-5.2")]["errors"] == 1
    # TTFT / 缓存 / 积分 在最近请求明细里
    rec = next(r for r in snap["recent"] if r["model"] == "glm-5.3")
    assert rec["ttft_ms"] == 45 and rec["cached_tokens"] == 3 and rec["credit"] == 0.5


def test_metrics_record_account_and_default_empty(env):
    # traepat 多账号：record 带 account，最近请求里可见；其它通道缺省为空串
    env.state.metrics.record(provider="traepat", model="gpt-6", protocol="openai",
                             status=200, duration_ms=10, account="primary")
    env.state.metrics.record(provider="codebuddy", model="glm-5.3",
                             status=200, duration_ms=10)
    recent = env.state.metrics.snapshot()["recent"]
    by_provider = {r["provider"]: r for r in recent}
    assert by_provider["traepat"]["account"] == "primary"
    assert by_provider["codebuddy"]["account"] == ""


# ---------------------------------------------------------------------------
# 流式 usage 提取（SSE）
# ---------------------------------------------------------------------------
def test_sse_usage_extractor_openai():
    """CodeBuddy/OpenAI 形态：usage 在末尾 chunk 一次性给。"""
    ex = SSEUsageExtractor()
    ex.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
    ex.feed(b'data: {"choices":[],"usage":{"prompt_tokens":18,"completion_tokens":2,'
            b'"prompt_tokens_details":{"cached_tokens":9},"credit":0.35}}\n\n')
    ex.feed(b"data: [DONE]\n\n")
    assert ex.usage == {"prompt_tokens": 18, "completion_tokens": 2,
                        "cached_tokens": 9, "credit": 0.35}


def test_sse_usage_extractor_anthropic_and_split_lines():
    """Anthropic 形态（input/output 分事件）+ usage 行跨 chunk 断开。

    口径统一：Anthropic 的 input_tokens 不含缓存，提取层要加总成
    OpenAI 口径（prompt = input + cache_read + cache_creation），
    zcode 直通流才能与 codebuddy/trae 横向对比。
    """
    ex = SSEUsageExtractor()
    ex.feed(b'event: message_start\ndata: {"message":{"usage":{"input_tokens":120,'
            b'"cache_read_input_tokens":30}}}\n\n')
    # 故意把 usage 行从中间劈开
    ex.feed(b'event: message_delta\ndata: {"usage":{"output_tok')
    ex.feed(b'ens": 55}}\n\n')
    assert ex.usage["prompt_tokens"] == 150
    assert ex.usage["completion_tokens"] == 55
    assert ex.usage["cached_tokens"] == 30


def test_sse_usage_extractor_str_chunks():
    """Trae 流产出 str chunk（codebuddy 是 bytes）——两者都必须能喂。"""
    ex = SSEUsageExtractor()
    ex.feed('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
    ex.feed('data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,'
            '"total_tokens":10}}\n\n')
    ex.feed("data: [DONE]\n\n")
    assert ex.usage["prompt_tokens"] == 7 and ex.usage["completion_tokens"] == 3


def test_sse_error_extractor_openai_and_anthropic_shapes():
    """带内错误提取：OpenAI ``data: {"error":...}`` 与 Anthropic ``event: error``。

    2026-10-02 假绿灯回归：codebuddy 把上游错误做成正常 SSE 事件，生成器正常
    结束，指标里却是 status=200 / error=""。提取器是那条路径的唯一线索。
    """
    from buddy_proxy.core.metrics import SSEErrorExtractor

    # OpenAI 形态：error 在 data 行里
    ex = SSEErrorExtractor()
    ex.feed(b'data: {"error":{"message":"upstream overloaded","type":"api_error"}}\n\n')
    assert ex.message == "upstream overloaded"

    # Anthropic 形态：先 event: error 占位，紧随其后的 data 行给出真消息
    ex2 = SSEErrorExtractor()
    ex2.feed(b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error",'
             b'"message":"boom"}}\n\n')
    assert ex2.message == "boom"

    # 只有 event: error 没有 data（被截断）也不能是空串
    ex3 = SSEErrorExtractor()
    ex3.feed(b"event: error\n\n")
    assert ex3.message == "in-band error event"


def test_sse_error_extractor_ignores_normal_stream():
    """正常内容里出现 "error" 字样（代码片段、字段名）不得误报。"""
    from buddy_proxy.core.metrics import SSEErrorExtractor

    ex = SSEErrorExtractor()
    ex.feed(b'data: {"choices":[{"delta":{"content":"the error handling code"}}]}\n\n')
    ex.feed(b'data: {"choices":[{"delta":{"content":"def on_error(e): ..."}}]}\n\n')
    ex.feed(b"data: [DONE]\n\n")
    assert ex.message == ""

    # 带 error 字段但不是错误（如 finish_reason 里的普通字符串）也不报
    ex2 = SSEErrorExtractor()
    ex2.feed(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
             b'"note":"no error here"}\n\n')
    assert ex2.message == ""


def test_sse_error_extractor_cross_chunk_and_first_wins():
    """跨 chunk 断行要能重组；多个错误只留第一个（真因）。"""
    from buddy_proxy.core.metrics import SSEErrorExtractor

    ex = SSEErrorExtractor()
    ex.feed(b'data: {"error":{"mess')
    ex.feed(b'age":"first"}}\n\n')
    assert ex.message == "first"

    ex.feed(b'data: {"error":{"message":"second"}}\n\n')
    assert ex.message == "first"

    # 占位文案要让位给后来的真消息（event: error 先到、data 后到的情形）
    ex2 = SSEErrorExtractor()
    ex2.feed(b"event: error\n")
    ex2.feed(b'data: {"error":{"message":"real cause"}}\n\n')
    assert ex2.message == "real cause"


def test_sse_error_extractor_error_word_split_across_chunks():
    """"error" 一词被 chunk 边界从中间劈开也不能漏——两半都不含完整单词。

    review 发现：快路径 `b"error" not in chunk` 在这种劈法下两半都直接 return，
    错误整个丢失（假绿灯复发）。先缓冲、后判断才正确。
    """
    from buddy_proxy.core.metrics import SSEErrorExtractor

    ex = SSEErrorExtractor()
    ex.feed(b'data: {"err')                    # 前半不含完整 "error"
    ex.feed(b'or":{"message":"boom"}}\n\n')    # 后半同样不含
    assert ex.message == "boom"

    # 对照：一个不含任何 "error" 字样的正常流，message 保持空
    ex2 = SSEErrorExtractor()
    ex2.feed(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
    ex2.feed(b"data: [DONE]\n\n")
    assert ex2.message == ""


def test_metrics_stream_records_in_band_error_as_500(env):
    """带内错误的流必须记成失败——否则就是「客户端零字节、指标 200」的假绿灯。"""
    from buddy_proxy.codebuddy_provider.observability import _metrics_stream

    async def inner():
        # 复刻事故里 codebuddy 的实际产出：仅 54 字节的错误体，正常结束
        yield b'data: {"error":{"message":"upstream returned 503","type":"api_error"}}\n\n'

    async def drain():
        chunks = []
        async for c in _metrics_stream(
                inner(), env.state.metrics, provider_id="codebuddy", model_id="glm-5.3",
                protocol="openai", started=time.time()):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(drain())
    # 透传不改变字节
    assert len(chunks) == 1 and b"upstream returned 503" in chunks[0]
    rec = env.state.metrics.snapshot()["recent"][0]
    assert rec["status"] == 500
    assert "in-band error" in rec["error"] and "upstream returned 503" in rec["error"]


def test_metrics_record_ttft_and_credit_in_recent(env):
    env.state.metrics.record(provider="fakeprov", model="fake-model",
                             stream=True, status=200, duration_ms=900,
                             ttft_ms=180, prompt_tokens=50, completion_tokens=20,
                             cached_tokens=10, credit=1.5)
    snap = env.state.metrics.snapshot()
    recent = snap["recent"][0]
    assert recent["ttft_ms"] == 180
    assert recent["cached_tokens"] == 10
    assert recent["credit"] == 1.5


def test_normalize_usage_shapes():
    n = normalize_usage({"prompt_tokens": 10, "completion_tokens": 5,
                         "prompt_tokens_details": {"cached_tokens": 4}, "credit": 0.2})
    assert n == {"prompt_tokens": 10, "completion_tokens": 5,
                 "cached_tokens": 4, "credit": 0.2}
    a = normalize_usage({"input_tokens": 8, "output_tokens": 3,
                         "cache_read_input_tokens": 2})
    # Anthropic 形态统一为 OpenAI 口径：prompt = input + cache_read
    assert a == {"prompt_tokens": 10, "completion_tokens": 3,
                 "cached_tokens": 2, "credit": None}
    # Anthropic 形态含缓存创建（5m/1h 写入）也要计入全量输入
    aw = normalize_usage({"input_tokens": 8, "output_tokens": 3,
                          "cache_creation_input_tokens": 5,
                          "cache_read_input_tokens": 2})
    assert aw == {"prompt_tokens": 15, "completion_tokens": 3,
                  "cached_tokens": 2, "credit": None}
    # Responses API 形态：input_tokens_details.cached_tokens
    r = normalize_usage({"input_tokens": 9, "output_tokens": 1,
                         "input_tokens_details": {"cached_tokens": 7}, "credit": 0.3})
    assert r["cached_tokens"] == 7 and r["credit"] == 0.3


def test_normalize_usage_accepts_plural_credits():
    """上游积分字段拼写不统一：CodeBuddy 给 ``credit``，Qoder 给 ``credits``。

    只认单数会把 Qoder 的积分整条丢掉——实测它的 usage 长这样（字段就在眼皮
    底下，``original_credits``/``billable`` 一起摆着，但之前一直记成 null）：

        {"billable": true, "credits": 0.008510259999999999,
         "original_credits": 0.008510259999999999,
         "completion_tokens": 87, "prompt_tokens": 35,
         "prompt_tokens_details": {"cached_tokens": 0}}
    """
    q = normalize_usage({
        "billable": True,
        "prompt_tokens": 35,
        "completion_tokens": 87,
        "credits": 0.008510259999999999,
        "original_credits": 0.008510259999999999,
        "prompt_tokens_details": {"cached_tokens": 0},
    })
    assert q["credit"] == 0.008510259999999999, "Qoder 的 credits（复数）必须被认出来"

    # 只有 original_credits 时也兜住（上游折扣场景下 credits 可能是 0 或缺省）
    assert normalize_usage({"original_credits": 0.5})["credit"] == 0.5

    # 两个都在时以 credit 为准（CodeBuddy 是实际扣费口径）
    assert normalize_usage({"credit": 0.2, "credits": 0.9})["credit"] == 0.2

    # 布尔不是数字：True 不能被当成 1 积分
    assert normalize_usage({"credit": True})["credit"] is None
    assert normalize_usage({})["credit"] is None


def test_responses_converter_carries_credit_and_cache():
    """Responses 流式转换完成事件要带真实 cached_tokens 与 credit。"""
    from buddy_proxy.protocols.responses_adapter import ResponsesStreamConverter

    conv = ResponsesStreamConverter(model="glm-5.3-flash")
    for chunk in (
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34,
                   "prompt_tokens_details": {"cached_tokens": 28}, "credit": 0.9}},
    ):
        conv.feed_chunk(chunk)
    events = conv.finish()
    completed = [d for n, d in events if n == "response.completed"][0]
    usage = completed["response"]["usage"]
    assert usage["input_tokens"] == 30
    assert usage["input_tokens_details"]["cached_tokens"] == 28
    assert usage["credit"] == 0.9

    # 指标层能从转出的 responses 事件流里提取到同一组数据
    ex = SSEUsageExtractor()
    for name, data in events:
        ex.feed(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
    assert ex.usage["credit"] == 0.9
    assert ex.usage["cached_tokens"] == 28


def test_trae_models_carry_credits():
    """Trae 模型应带积分倍率（知识库《模型及成本整理》2026-09-05 版）。"""
    from buddy_proxy.trae_provider import TraeProvider
    models = {m["id"]: m for m in TraeProvider().models()}
    assert models["glm-5.3"]["credits"] == "x0.39"  # 2026-10-12 面板跟价（会员5折徽章）
    assert models["glm-5.3-flash"]["credits"] == "x0.06"
    assert models["deepseek-v4.1-flash"]["credits"] == "x0.08"
    assert models["doubao-seed-evolving"]["credits"] == "x0.08"
    # kimi-k3 已被 kimi-k3.1-pro 顶掉（代际收敛），倍率仍按底表可取
    assert models["kimi-k3.1-pro"]["credits"] == "x1.20"
    assert not {"kimi-k3", "deepseek-v4-pro"} & models.keys()
    # 用户可见模型 ID 统一小写，旧模型不再出现在目录里。
    assert all(model_id == model_id.lower() for model_id in models)
    # qwen-3.7-plus（带连字符）2026-10-12 实测收录；qwen3.7-plus（无连字符）
    # 才是被上游拒的变体名
    # glm-5.2 已在 2026-10-25 实测收录（目录可见 + 200 出流），不再属于「旧模型」
    assert not {"deepseek-v4-flash", "glm-5", "glm-5-turbo",
                "kimi-k2.7-code", "qwen3.7-plus"} & models.keys()
    # 底表收录了 qwen-3.7-plus / glm-5.2，但同家族新一代在场时不再列出
    assert "qwen-3.7-plus" not in models
    assert models["glm-5.3"]["credits"] == "x0.39"
    assert models["kimi-k3.1-pro"]["credits"] == "x1.20"
    assert models["minimax-m3.1-flash"]["credits"] == "x0.09"


def test_metrics_daily_series_zero_filled(tmp_path):
    m = MetricsCollector(tmp_path / "metrics.jsonl")
    m.record(provider="zcode", model="glm-5.3", status=200, duration_ms=10)
    snap = m.snapshot(days=14)
    assert len(snap["daily"]) == 14
    assert snap["daily"][-1]["total"] == 1
    assert snap["daily"][0]["total"] == 0
    assert snap["daily"][-1]["by_provider"] == {"zcode": 1}


def test_query_logs_pagination_reads_disk(tmp_path):
    """服务端分页直接读磁盘日志，突破进程内 recent 200 条上限。"""
    m = MetricsCollector(tmp_path / "metrics.jsonl")
    for i in range(250):
        m.record(provider="zcode", model="glm-5.3", status=200, duration_ms=i)
    # 磁盘上有全部 250 条（进程内 recent 只留 200）
    r1 = m.query_logs(page=1, page_size=20)
    assert r1["total"] == 250          # 全量，非 200 上限
    assert r1["pages"] == 13           # ceil(250/20)
    assert len(r1["rows"]) == 20
    assert r1["from_disk"] is True
    # 第 13 页可达（旧客户端分页最多 10 页）
    r13 = m.query_logs(page=13, page_size=20)
    assert len(r13["rows"]) == 10
    # 越界页收敛到末页
    assert m.query_logs(page=999, page_size=20)["page"] == 13
    # 倒序：第 1 页首条 ts 最大
    assert r1["rows"][0]["ts"] >= r1["rows"][-1]["ts"]


def test_query_logs_date_range_filter(tmp_path):
    """按 YYYY-MM-DD 范围过滤（含端点）。"""
    m = MetricsCollector(tmp_path / "metrics.jsonl")
    m.record(provider="zcode", model="glm-5.3", status=200, duration_ms=1)
    today = time.strftime("%Y-%m-%d", time.localtime())
    # 未来日期起点：范围内应无记录
    future = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
    assert m.query_logs(start=future)["total"] == 0
    # 含今天：命中
    assert m.query_logs(start=today, end=today)["total"] == 1


def test_query_logs_memory_fallback():
    """无落盘（log_path=None）时退回内存 recent。"""
    m = MetricsCollector(None)
    m.record(provider="zcode", model="glm-5.3", status=200, duration_ms=1)
    r = m.query_logs(page=1, page_size=20)
    assert r["total"] == 1
    assert r["from_disk"] is False


def test_query_logs_provider_model_filter(tmp_path):
    """通道/模型白名单过滤：组内 OR、组间 AND；空 = 不筛。"""
    m = MetricsCollector(tmp_path / "metrics.jsonl")
    m.record(provider="zcode", model="glm-5.3", status=200, duration_ms=1)
    m.record(provider="traepat", model="openrouter-3o-max", status=200, duration_ms=1)
    m.record(provider="traepat", model="glm-4.7", status=200, duration_ms=1)

    assert m.query_logs()["total"] == 3                          # 不筛
    assert m.query_logs(providers=["traepat"])["total"] == 2     # 单通道
    assert m.query_logs(providers=["zcode", "traepat"])["total"] == 3  # 通道 OR
    assert m.query_logs(models=["glm-5.3", "glm-4.7"])["total"] == 2   # 模型 OR
    assert m.query_logs(providers=["traepat"], models=["glm-4.7"])["total"] == 1  # AND
    assert m.query_logs(providers=["traepat"], models=["glm-5.3"])["total"] == 0  # AND 无交集
    assert m.query_logs(providers=["不存在的通道"])["total"] == 0
    # 过滤结果内容正确（只含 traepat/glm-4.7）
    rows = m.query_logs(providers=["traepat"], models=["glm-4.7"])["rows"]
    assert all(r["provider"] == "traepat" and r["model"] == "glm-4.7" for r in rows)


def test_query_logs_client_filter_and_candidates(tmp_path):
    """客户端白名单过滤 + 候选列表：组内 OR、与其它组 AND；候选不随筛选收缩。"""
    m = MetricsCollector(tmp_path / "metrics.jsonl")
    m.record(provider="zcode", model="glm-5.3", status=200, client="claude-code")
    m.record(provider="zcode", model="glm-5.3", status=200, client="claude-code")
    m.record(provider="traepat", model="glm-4.7", status=200, client="curl")
    m.record(provider="traepat", model="glm-4.7", status=200)  # 旧记录无 client

    # 候选：只收非空值、按出现次数降序，无筛选时也返回
    assert m.query_logs()["clients"] == ["claude-code", "curl"]
    # 白名单过滤：组内 OR；client 为空的记录不命中任何候选
    assert m.query_logs(clients=["curl"])["total"] == 1
    assert m.query_logs(clients=["claude-code", "curl"])["total"] == 3
    assert m.query_logs(clients=["不存在的客户端"])["total"] == 0
    # 与通道组 AND
    assert m.query_logs(providers=["traepat"], clients=["curl"])["total"] == 1
    assert m.query_logs(providers=["zcode"], clients=["curl"])["total"] == 0
    # 候选在白名单过滤**之前**收集：筛掉 claude-code 后它仍在候选里
    assert m.query_logs(clients=["curl"])["clients"] == ["claude-code", "curl"]
    # 过滤结果内容正确
    rows = m.query_logs(clients=["claude-code"])["rows"]
    assert all(r["client"] == "claude-code" for r in rows)


# ---------------------------------------------------------------------------
# settings 模块
# ---------------------------------------------------------------------------
def test_settings_roundtrip_and_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(tmp_path / "s.json"))
    assert settings_mod.load_settings() == {}
    settings_mod.save_settings({"default_model": "workbuddy/glm-5.3"})
    # workbuddy 别名归一为 codebuddy
    assert settings_mod.normalize_default_model("workbuddy/glm-5.3") == "codebuddy/glm-5.3"
    assert settings_mod.load_settings()["default_model"] == "workbuddy/glm-5.3"


# ---------------------------------------------------------------------------
# 打卡 / 额度
# ---------------------------------------------------------------------------
class FakeCheckinProvider(FakeProvider):
    """支持打卡与额度的假 provider。"""
    id = "fakecheckin"
    name = "Fake Checkin"
    supports_checkin = True

    def __init__(self):
        super().__init__()
        self.claim_calls = 0

    def checkin_status(self):
        return {"checked_in": self.claim_calls > 0, "claimable": True,
                "inactive": False, "streak_days": 1, "message": "success"}

    def checkin_claim(self):
        self.claim_calls += 1
        return {"checked_in": True, "credits": 250, "extra_credits": 50,
                "message": "success"}

    def quota(self):
        return {"items": [{"label": "总额度", "used": 30, "total": 100,
                           "remaining": 70, "percent": 30, "reset_ts": None}],
                "level": "pro"}


def _benefits_state(tmp_path, providers):
    client = mock.MagicMock()
    client.session = {}
    state = SimpleNamespace(
        client=client, providers=providers, mock_dir=None,
        started_at=time.time(), enable_desensitize=False,
        enable_optimize_context=False, verbose_llm=False,
        default_provider="codebuddy", default_model=None,
        metrics=MetricsCollector(None),
        write_log=mock.MagicMock(), ensure_auth=mock.MagicMock(),
        logger=mock.MagicMock(), json_logger=mock.MagicMock(),
        runtime_info={},
    )
    manager = BenefitsManager(tmp_path / "checkin.jsonl", state)
    state.benefits = manager
    return state, manager


def test_checkin_history_and_manager(tmp_path):
    state, manager = _benefits_state(tmp_path, {"fakecheckin": FakeCheckinProvider()})
    result = asyncio.run(manager.claim_now("fakecheckin"))
    assert result["ok"] is True
    # 历史落盘 + 日历今天有记录（含当日领取积分）
    history = CheckinHistory(tmp_path / "checkin.jsonl")
    dates = history.ok_dates_by_provider()
    assert len(dates["fakecheckin"]) == 1
    cal = history.calendar(7)
    assert cal[-1]["providers"] == ["fakecheckin"]
    assert cal[-1]["credits"] == {"fakecheckin": 50}
    assert cal[0]["providers"] == []
    # snapshot：done_today 且额度展示出来
    snap = asyncio.run(manager.snapshot())
    entry = next(p for p in snap["providers"] if p["id"] == "fakecheckin")
    assert entry["checkin"]["supported"] is True
    assert entry["checkin"]["done_today"] is True
    assert entry["quota"]["supported"] is True
    assert entry["quota"]["items"][0]["percent"] == 30
    assert entry["quota"]["items"][0]["remaining"] == 70


def test_checkin_calendar_keeps_max_nonzero_credits(tmp_path):
    """同日多笔记录（自动轮询的 replay 拿不到金额）不能把真实领取糊成 +0。

    2026-10-07 用户 hover 日历看到 trae +0：后到的 replay 记录 credits=0.0
    把早前真实金额覆盖了。聚合保留最大非零值。
    """
    state, manager = _benefits_state(tmp_path, {"fakecheckin": FakeCheckinProvider()})
    asyncio.run(manager.claim_now("fakecheckin"))                   # 实领 50
    manager.history.append("fakecheckin", ok=True, credits=0.0)     # replay 0
    manager.history.append("fakecheckin", ok=True, credits=None)    # replay 未知
    cal = CheckinHistory(tmp_path / "checkin.jsonl").calendar(7)
    assert cal[-1]["credits"] == {"fakecheckin": 50}


def test_checkin_record_falls_back_to_daily_credit(tmp_path):
    """上游不报实领金额（dumate 只有每日额均值口径）时退回 daily_credit。"""

    class AvgOnlyProvider(FakeCheckinProvider):
        id = "fakeavgcheckin"

        def checkin_claim(self):
            return {"checked_in": True, "message": "ok", "daily_credit": 120}

    state, manager = _benefits_state(tmp_path, {"fakeavgcheckin": AvgOnlyProvider()})
    asyncio.run(manager.claim_now("fakeavgcheckin"))
    cal = CheckinHistory(tmp_path / "checkin.jsonl").calendar(7)
    assert cal[-1]["credits"] == {"fakeavgcheckin": 120}


def test_checkin_claim_upstream_error_recorded(tmp_path):
    class ErrCheckinProvider(FakeCheckinProvider):
        id = "fakeerrcheckin"

        def checkin_claim(self):
            raise RuntimeError("boom")

    state, manager = _benefits_state(tmp_path, {"fakeerrcheckin": ErrCheckinProvider()})
    result = asyncio.run(manager.claim_now("fakeerrcheckin"))
    assert result["ok"] is False
    history = CheckinHistory(tmp_path / "checkin.jsonl")
    assert history.entries()[-1]["ok"] is False
    assert "boom" in history.entries()[-1]["message"]


def test_auto_checkin_tick_claims_once(tmp_path):
    state, manager = _benefits_state(tmp_path, {"fakecheckin": FakeCheckinProvider()})
    import asyncio
    asyncio.run(manager._tick())
    provider = state.providers["fakecheckin"]
    assert provider.claim_calls == 1
    # 当天已完成 → 再次巡检不会重复打卡
    asyncio.run(manager._tick())
    assert provider.claim_calls == 1
    # auto_checkin 关闭 → 不打
    settings_mod.save_settings({"auto_checkin": False})
    asyncio.run(manager._tick())
    assert provider.claim_calls == 1


def test_auto_checkin_skips_inactive_provider(tmp_path):
    """上游当天无签到活动（claimable=False）→ 不 claim、不留失败噪音。"""
    class InactiveCheckinProvider(FakeCheckinProvider):
        id = "fakeinactive"

        def checkin_status(self):
            return {"checked_in": False, "claimable": False, "inactive": True,
                    "streak_days": 0, "message": "活动未开始"}

    state, manager = _benefits_state(tmp_path, {"fakeinactive": InactiveCheckinProvider()})
    import asyncio
    asyncio.run(manager._tick())
    provider = state.providers["fakeinactive"]
    assert provider.claim_calls == 0
    assert CheckinHistory(tmp_path / "checkin.jsonl").entries() == []


def test_auto_checkin_records_upstream_checked_in(tmp_path):
    """上游已签到（历史没有记录）→ 补记 ok，且不再 claim。"""
    class AlreadyCheckinProvider(FakeCheckinProvider):
        id = "fakealready"

        def checkin_status(self):
            return {"checked_in": True, "claimable": False, "inactive": False,
                    "streak_days": 3, "message": "已签到"}

    state, manager = _benefits_state(tmp_path, {"fakealready": AlreadyCheckinProvider()})
    import asyncio
    asyncio.run(manager._tick())
    assert state.providers["fakealready"].claim_calls == 0
    history = CheckinHistory(tmp_path / "checkin.jsonl")
    assert history.entries()[-1]["ok"] is True
    assert "已签到" in history.entries()[-1]["message"]


def test_checkin_disabled_provider_not_touched(tmp_path):
    # 用假的 codebuddy 顶掉 _default_codebuddy 注入（真实 codebuddy 现在支持打卡）
    state, manager = _benefits_state(tmp_path, {"codebuddy": FakeProvider(),
                                                "fakeprov": FakeProvider()})
    assert manager.checkin_providers() == {}
    import asyncio
    asyncio.run(manager._tick())  # 不应抛异常
    assert CheckinHistory(tmp_path / "checkin.jsonl").entries() == []


def test_zcode_quota_items_are_ordered_smallest_window_first():
    """排序必须按**窗口宽度**，不是按「距重置多久」。

    真实的偶然性：实测那份数据里 5 小时档**没有** ``nextResetTime``
    （取 0），按重置时间升序排它反而落到最前——两条策略在真实数据上
    **恰好同解**，所以只断言「5 小时档在最前」是测不出排序差别的
    （变异测试里把 sort 换回按时间排，测试照样全绿）。

    要真分家，得让**大**窗口没有重置时间、**小**窗口有：这样按时间排会
    把大窗口顶到最前，按宽度排才把小窗口放前面。这个 fixture 就是按
    「两种排法必须给出相反顺序」挑的。
    """
    now_ms = int(time.time() * 1000)
    data = {"limits": [
        # 月档：**没有** nextResetTime，按时间排（当 0）会跑到最前
        {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "usage": 10000,
         "currentValue": 1, "remaining": 9999, "percentage": 1},
        # 5 小时档：有几个月后的重置时间，按时间排会排到后面
        {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 2000,
         "currentValue": 0, "remaining": 2000, "percentage": 0,
         "nextResetTime": now_ms + 120 * 86400 * 1000},
    ]}
    labels = [i["label"] for i in _quota_items(data)]
    assert labels == ["5 小时窗口", "月窗口"], (
        f"小窗口必须在前（按窗口宽度排）；按 nextResetTime 排会得到 "
        f"['月窗口', '5 小时窗口']，正是老的 bug: {labels}"
    )


def test_zcode_quota_items_normalization():
    """窗口名按上游的 unit/number 算，排序按窗口由小到大。

    用**真实返回值的形状**（2026-09-30 抓取）：两条 ``CREDIT_LIMIT``，
    unit=3/number=5 是 5 小时档且**不给 nextResetTime**；unit=6/number=1
    是月档，nextResetTime 在几天之后。
    """
    month_reset = int(time.time() * 1000) + 4 * 86400 * 1000   # 4 天后重置（月档）
    data = {"limits": [
        # 故意把月档写在前面：排序不能靠上游给的顺序
        {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "usage": 10000,
         "currentValue": 4242, "remaining": 5757, "percentage": 42,
         "nextResetTime": month_reset},
        {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 2000,
         "currentValue": 1249, "remaining": 750, "percentage": 62},
    ], "level": "lite"}
    items = _quota_items(data)
    # 小窗口在前：5 小时档必须排第一，标题行才会显示更紧迫的那档
    assert items[0]["label"] == "5 小时窗口", items
    assert items[0]["percent"] == 62 and items[0]["used"] == 1249
    assert items[0]["remaining"] == 750
    # 月档不能因为「重置还有 4 天」就被叫成每周窗口
    assert items[1]["label"] == "月窗口", items
    assert items[1]["percent"] == 42 and items[1]["remaining"] == 5757


def test_zcode_quota_window_name_beats_distance_to_reset():
    """回归：窗口名以 unit/number 为准，不许再按「距重置多久」猜。

    老实现把「离重置还有 4 天」当成「每周」，而这条其实是一个 30 天的月档；
    同时 5 小时档因为上游不给 nextResetTime、又排在末位，标签退化成裸的
    ``CREDIT_LIMIT``。于是标题行（取 items[0]）显示出一个既无名、又只是
    5 小时档的 2000，压过月档的 10000。
    """
    reset_ts = int(time.time() * 1000) + 4 * 86400 * 1000
    data = {"limits": [
        {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "usage": 10000,
         "currentValue": 1, "remaining": 9999, "percentage": 1,
         "nextResetTime": reset_ts},
        {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 2000,
         "currentValue": 0, "remaining": 2000, "percentage": 0},
    ]}
    labels = [i["label"] for i in _quota_items(data)]
    assert labels == ["5 小时窗口", "月窗口"], labels
    # 一个都不能再是裸的 type 名
    assert "CREDIT_LIMIT" not in labels


def test_zcode_quota_window_label_has_no_ugly_floats():
    """展示名不该出现「10.0 天窗口」「0.0166667 小时窗口」这种。

    ``span / 86400`` 在恰好 10 天时是 ``10.0``，``span / 3600`` 遇到分钟级
    窗口会铺出一长串小数。都是能算出来的边界，顺手格式化掉。
    """
    def label(unit, number):
        (item,) = _quota_items({"limits": [
            {"type": "CREDIT_LIMIT", "unit": unit, "number": number, "usage": 100,
             "currentValue": 0, "remaining": 100, "percentage": 0}]})
        return item["label"]

    assert label(4, 10) == "10 天窗口"          # 不是「10.0 天窗口」
    assert label(3, 24) == "24 小时窗口"         # 恰好一天走小时档，整数
    assert label(3, 5) == "5 小时窗口"
    assert "." not in label(4, 10)
    # 分钟级窗口能显示，且不糊一长串
    minute = label(3, 1 / 60)
    assert minute.endswith("小时窗口")
    assert len(minute) <= len("0.01667 小时窗口"), minute
    assert minute.count("6") <= 3, minute       # 不是 0.016666666666666666


def test_zcode_quota_tolerates_junk_unit_and_number():
    """``unit``/``number`` 当不可信输入：给成字符串也不能把面板搞崩。

    上游真发 ``"5"``（字符串）的话，``3600 * "5"`` 在 Python 里是字符串
    重复、**不报错**，然后拿它去比较就抛 ``TypeError`` —— 额度面板整块
    挂掉。收类型时必须按「不行就当没给」处理，退回旧的三档推断。
    """
    def _lim(**kw):
        return {"type": "CREDIT_LIMIT", "usage": 2000, "currentValue": 0,
                "remaining": 2000, "percentage": 0, **kw}

    # 数字用字符串送来：能干净地收成数，那就照常算出 5 小时档
    for unit, number in ((3, "5"), ("3", 5), (3, 5.0)):
        (item,) = _quota_items({"limits": [_lim(unit=unit, number=number)]})
        assert item["label"] == "5 小时窗口", (unit, number, item)

    # 收不干净（缺 / 零 / 负 / 类型不对）：退回推断，**不能**猜成小时档
    for kw in ({"unit": 3, "number": None}, {"unit": 3}, {"unit": 3, "number": 0},
               {"unit": 3, "number": -5}, {"unit": [3], "number": 5},
               {"unit": 3, "number": {"v": 5}}, {"unit": None, "number": 5}):
        (item,) = _quota_items({"limits": [_lim(**kw)]})   # 不抛异常是底线
        assert item["label"] == "CREDIT_LIMIT", (kw, item)
        assert item["remaining"] == 2000, kw


def test_zcode_quota_unknown_unit_falls_back_without_lying():
    """unit 没见过时退回按剩余时间推断，且**不能**瞎猜成小时。

    上游的 unit 是自定枚举。真遇到没见过的取值，宁可走旧的三档兜底，
    也不能按「3 像分钟所以是 3 分钟、6 像小时所以是 6 小时」猜——猜错会把
    月档写成「6 小时窗口」。
    """
    far_reset = int(time.time() * 1000) + 4 * 86400 * 1000
    items = _quota_items({"limits": [
        {"type": "CREDIT_LIMIT", "unit": 99, "number": 7, "usage": 500,
         "currentValue": 5, "remaining": 495, "percentage": 1,
         "nextResetTime": far_reset},
    ]})
    assert items[0]["label"] == "每周窗口", items
    assert "99" not in items[0]["label"] and "7" not in items[0]["label"]


def test_ui_checkin_endpoint(env, tmp_path):
    env.state.providers["fakecheckin"] = FakeCheckinProvider()
    env.state.benefits = BenefitsManager(tmp_path / "c.jsonl", env.state)
    body = env.client.post("/ui/api/checkin",
                           json={"provider": "fakecheckin"}).json()
    assert body["ok"] is True
    r = env.client.post("/ui/api/checkin", json={"provider": "fakeprov"}).json()
    assert r["ok"] is False  # 未声明 supports_checkin


def test_qoderintl_inherits_checkin_ui_route(env, tmp_path, monkeypatch):
    """Qoder Intl 注册后进入通用签到 provider 列表并可经 UI 端点领取。"""
    from buddy_proxy.qoder.intl_provider import QoderIntlProvider

    provider = QoderIntlProvider()

    async def checkin_claim():
        return {"message": "intl claimed", "extra_credits": 12}

    monkeypatch.setattr(provider, "checkin_claim", checkin_claim)
    env.state.providers["qoderintl"] = provider
    env.state.benefits = BenefitsManager(tmp_path / "c.jsonl", env.state)

    assert "qoderintl" in env.state.benefits.checkin_providers()
    result = env.client.post("/ui/api/checkin", json={"provider": "qoderintl"}).json()
    assert result["ok"] is True
    assert result["message"] == "intl claimed"


def test_settings_auto_checkin_validation(env):
    r = env.client.post("/ui/api/settings",
                        json={"auto_checkin": False, "checkin_time": "08:05"})
    assert r.status_code == 200
    saved = settings_mod.load_settings()
    assert saved["auto_checkin"] is False and saved["checkin_time"] == "08:05"
    r = env.client.post("/ui/api/settings", json={"checkin_time": "99:00"})
    assert r.status_code == 400


def test_lifespan_starts_and_stops_benefits_loop(tmp_path, monkeypatch):
    """app 启动时拉起自动打卡循环，关闭时停止（回归：曾因未导入符号启动即崩）。"""
    state, manager = _benefits_state(tmp_path, {"fakecheckin": FakeCheckinProvider()})
    manager.start = mock.MagicMock()
    manager.stop = mock.MagicMock()
    monkeypatch.setattr(st, "proxy_state", state)
    with TestClient(m.app):
        manager.start.assert_called_once()
    manager.stop.assert_called_once()


def test_zcode_retries_transient_disconnect():
    """上游在返回任何字节前断连 → 原地重发一次，成功即正常返回。"""
    import asyncio
    import httpx
    from buddy_proxy.providers.zcode import ZcodeProvider

    p = ZcodeProvider(api_key="k", base_url="https://open.bigmodel.cn/api/anthropic")
    calls = {"n": 0}

    class FakeResp:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "pong"}}]}

    class FlakyClient:
        def build_request(self, *a, **k):
            return object()
        async def send(self, req, stream=False):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            return FakeResp()

    async def fake_get_client():
        return FlakyClient()
    p._get_client = fake_get_client

    resp = asyncio.run(p.forward({"model": "glm-5.3-flash",
                                  "messages": [{"role": "user", "content": "hi"}]}, "openai"))
    assert calls["n"] == 2
    assert b"pong" in resp.body


# ---------------------------------------------------------------------------
# .env 多行 JSON 解析：串内括号不计深度（token 含 } 不能截断值）
# ---------------------------------------------------------------------------
def test_load_dotenv_multiline_json_with_brace_in_string(tmp_path, monkeypatch):
    """TRAE_PAT_BEARER_PROFILES 值的 bearer 串里含 } 时，旧词法会提前判闭合
    截断 JSON → json.loads 失败整个 PAT 通道 503。词法必须串感知。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释行\n"
        "PLAIN=value\n"
        'TRAE_PAT_BEARER_PROFILES=[\n'
        '  {"id": "a", "bearer": "head}tail{mix", "priority": 0},\n'
        '  {"id": "b", "bearer": "plain", "priority": 1}\n'
        ']\n'
        "AFTER=still-parsed\n",
        encoding="utf-8")

    saved = {k: os.environ.get(k) for k in
             ("TRAE_PAT_BEARER_PROFILES", "PLAIN", "AFTER")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        m._load_dotenv(env_file)
        assert os.environ["PLAIN"] == "value"
        assert os.environ["AFTER"] == "still-parsed"
        profiles = json.loads(os.environ["TRAE_PAT_BEARER_PROFILES"])
        # 串内的 } { 没有截断值：两个账号都完整解析
        assert [p["id"] for p in profiles] == ["a", "b"]
        assert profiles[0]["bearer"] == "head}tail{mix"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

def test_settings_write_is_atomic_and_private(tmp_path, monkeypatch):
    """设置落盘用原子替换，且最终文件保持仅当前用户可读。"""
    path = tmp_path / "nested" / "settings.json"
    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(path))
    settings_mod.save_settings({"default_model": "glm-5.3"})
    settings_mod.save_settings({"default_provider": "trae"})
    assert settings_mod.load_settings()["default_model"] == "glm-5.3"
    assert settings_mod.load_settings()["default_provider"] == "trae"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob(".settings.json.*.tmp"))


def test_reject_if_disabled_uses_model_key_alias(env, monkeypatch):
    """停用/时段键经 settings.model_key 归一：legacy workbuddy/* 进来的请求
    也要命中以 codebuddy/* 保存的配置，不能因别名拼键而漏拦。"""
    from buddy_proxy.codebuddy_provider.forward import _reject_if_disabled

    state = env.state
    # 窗口全闭 → workbuddy 别名请求必须被拦
    state.model_schedules = {"codebuddy/some-model": []}
    state.disabled_models = set()
    with pytest.raises(HTTPException) as caught:
        _reject_if_disabled(state, "workbuddy", "some-model")
    assert caught.value.status_code == 403

    # 同样适用于停用集合
    state.model_schedules = {}
    state.disabled_models = {"codebuddy/other-model"}
    with pytest.raises(HTTPException) as caught:
        _reject_if_disabled(state, "workbuddy", "other-model")
    assert caught.value.status_code == 403




def test_model_stats_use_bare_model_name(alias_env):
    """模型表的请求数必须按**裸名**查得到。

    埋点记的 model_id 来自转发链路（`glm-5.3`），而通道目录里的 `m["id"]` 带
    通道前缀（`qoder/deepseek-v4.1-flash`）。拿带前缀的 id 去查 stat_map，这一
    列就几乎恒为 0——用户反馈「14d 请求数字一直很小」的真因（实测 qoder 的
    deepseek-v4.1-flash 真实 2234 次、表里显示 1）。
    """
    state = alias_env.state
    state.metrics.record(provider="fakealias", model="glm-5.3", protocol="openai",
                         status=200, duration_ms=10.0)
    data = alias_env.client.get("/ui/api/models").json()
    grp = next(g for g in data["groups"] if g["id"] == "fakealias")
    row = next(m for m in grp["models"] if m["id"].endswith("glm-5.3"))
    assert row["stats"]["count"] == 1, (
        f"带前缀 id 查不到裸名口径的指标：{row['id']} → {row['stats']}"
    )


def test_model_groups_pair_zcode_start_after_zcode(monkeypatch):
    """模型页分组：zcode-start 的组排到 zcode 旁边（同一家两档套餐，隔家远
    不好对照——与额度页 benefits.js 的同款处理一致）。"""
    from types import SimpleNamespace

    from buddy_proxy.core import state as st
    from buddy_proxy.web.ui.models_api import _model_groups

    def prov(pid):
        return SimpleNamespace(id=pid, name=pid, health=lambda: {}, models=lambda: [])

    state = SimpleNamespace(
        mock_dir=None,
        client=SimpleNamespace(session={"auth": {"accessToken": "t",
                                                 "expiresAt": int((time.time() + 3600) * 1000)}}),
        providers={"glm": prov("glm"), "mimo": prov("mimo"),
                   "zcode": prov("zcode"), "zcode-start": prov("zcode-start")},
        logger=mock.MagicMock(), json_logger=mock.MagicMock(),
        write_log=mock.MagicMock(), enable_desensitize=False,
    )
    monkeypatch.setattr(st, "proxy_state", state)  # diagnostic → get_state 要有归属
    ids = [g["id"] for g in _model_groups(state)]
    assert ids.index("zcode-start") == ids.index("zcode") + 1, f"组序: {ids}"
