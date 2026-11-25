"""Trae Max 模式：窗口声明 / 开关 / is_max_mode 注入的端到端不变量。

核心不变量（见 buddy_proxy/trae/max_mode.py 模块 docstring）：
    对外声明的 context_window 与 转发时是否注入 is_max_mode 必须一致。

否则守卫按声明窗口放行，却打到上游 dev 墙 → 上游 400 原样透传，比不声明更糟。
本文件钉住这个不变量 + 三个分支（开关开/关、模型支持/不支持、目录发现覆盖）。

全离线：mock 发送函数记录请求体，不访问 Trae 上游。
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from buddy_proxy import __main__ as m
from buddy_proxy.core import state as st
from buddy_proxy.trae import provider as tp_impl
from buddy_proxy.trae import max_mode as max_mode_mod
from buddy_proxy.trae.credentials import save_account_cred
from buddy_proxy.trae.provider import TraeProvider
from buddy_proxy.trae.intl_provider import TraeIntlProvider


# ---------------------------------------------------------------------------
# 单元：effective_window 决策矩阵
# ---------------------------------------------------------------------------
def test_static_window_enabled_returns_max_and_inject():
    """开关开 + 静态兜底收录 → 声明 max 窗口、注入 is_max_mode。"""
    win, inject = max_mode_mod.effective_window("cn", "glm-5.4", enabled=True)
    assert win == 1_000_000
    assert inject is True


def test_static_window_disabled_returns_dev_no_inject():
    """开关关 → 声明 dev 窗口、不注入。"""
    win, inject = max_mode_mod.effective_window("cn", "glm-5.4", enabled=False)
    assert win == 116_000
    assert inject is False


def test_unsupported_model_never_declares_nor_injects():
    """不支持 Max 的模型（不在兜底表）→ (None, False)，守卫跳过、不注入。"""
    for enabled in (True, False):
        win, inject = max_mode_mod.effective_window(
            "cn", "doubao-seed-code", enabled=enabled)
        assert win is None
        assert inject is False


def test_discovered_overrides_static():
    """目录发现值优先于静态兜底：discovered 说不支持，静态说支持 → 不注入。"""
    discovered = {"max_mode": False, "context_window": 200_000, "max_window": 1_000_000}
    win, inject = max_mode_mod.effective_window(
        "cn", "glm-5.4", discovered=discovered, enabled=True)
    assert win == 200_000
    assert inject is False


def test_discovered_max_mode_true_injects():
    discovered = {"max_mode": True, "context_window": 200_000, "max_window": 1_000_000}
    win, inject = max_mode_mod.effective_window(
        "cn", "x", discovered=discovered, enabled=True)
    assert win == 1_000_000
    assert inject is True


def test_discovered_max_mode_true_but_switch_off_uses_dev():
    """discovered 支持但全局开关关 → 走 dev，不注入（开关是总闸）。"""
    discovered = {"max_mode": True, "context_window": 200_000, "max_window": 1_000_000}
    win, inject = max_mode_mod.effective_window(
        "cn", "x", discovered=discovered, enabled=False)
    assert win == 200_000
    assert inject is False


# ---------------------------------------------------------------------------
# 单元：请求体注入（native 与 text protocol 两条路都要带）
# ---------------------------------------------------------------------------
def test_native_body_injects_is_max_mode_when_flag():
    from buddy_proxy.trae.native_tools import _build_native_body

    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    body = _build_native_body(msgs, "glm-5.4", True, None, max_mode=True)
    assert body.get("is_max_mode") is True
    body_off = _build_native_body(msgs, "glm-5.4", True, None, max_mode=False)
    assert "is_max_mode" not in body_off


def test_text_protocol_body_injects_is_max_mode_when_flag():
    from buddy_proxy.trae.text_protocol import _build_chat_body

    msgs = [{"role": "user", "content": "hi"}]
    body = _build_chat_body(msgs, "glm-5.4", True, max_mode=True)
    assert body.get("is_max_mode") is True
    body_off = _build_chat_body(msgs, "glm-5.4", True, max_mode=False)
    assert "is_max_mode" not in body_off


# ---------------------------------------------------------------------------
# fixture：假 ProxyState + 捕获所有上游发送的请求体
# ---------------------------------------------------------------------------
SSE_OK = (
    'event: output\ndata: {"response": "hi"}\n\n'
    'event: token_usage\ndata: {"prompt_tokens": 1, "completion_tokens": 1, '
    '"total_tokens": 2}\n\n'
    "event: done\ndata: {}\n\n"
)


@pytest.fixture()
def capture_env(monkeypatch):
    """拦截 provider 内所有上游发送调用，记录 (path, body) 到 captured。

    path 标记是 native 还是 legacy（text protocol），body 是实际发出的 dict。
    所有调用返回一个合法 SSE，让 forward/collect 走通。
    """
    captured: list[tuple[str, dict]] = []

    def fake_native(native_msgs, model, stream, tools, **kwargs):
        # 直接构造 body 来还原 is_max_mode 是否被注入（与生产同路径）
        from buddy_proxy.trae.native_tools import _build_native_body
        body = _build_native_body(native_msgs, model, stream, tools,
                                  max_mode=kwargs.get("max_mode", False))
        captured.append(("native", body))
        return SSE_OK

    def fake_legacy(messages, model, stream, base_url=None, **kwargs):
        from buddy_proxy.trae.text_protocol import _build_chat_body
        body = _build_chat_body(messages, model, stream,
                                max_mode=kwargs.get("max_mode", False))
        captured.append(("legacy", body))
        return SSE_OK

    def fake_stream_events(native_msgs, model, tools, stop=None, **kwargs):
        from buddy_proxy.trae.native_tools import _build_native_body
        body = _build_native_body(native_msgs, model, True, tools,
                                  max_mode=kwargs.get("max_mode", False))
        captured.append(("native-stream", body))
        return None  # 走整段兼容路径，省得造事件迭代器

    monkeypatch.setattr(tp_impl, "_send_native_chat", fake_native)
    monkeypatch.setattr(tp_impl, "send_trae_chat", fake_legacy)
    monkeypatch.setattr(tp_impl, "_stream_native_chat_events", fake_stream_events)
    monkeypatch.setattr(tp_impl, "_auth", lambda: ("fake-token", "fake-uid"))

    provider = TraeProvider()
    state = SimpleNamespace(
        client=mock.MagicMock(),
        providers={"trae": provider},
        default_provider="codebuddy",
        mock_dir=None,
        started_at=time.time(),
        enable_desensitize=False,
        enable_optimize_context=False,
        verbose_llm=False,
        write_log=mock.MagicMock(),
        ensure_auth=mock.MagicMock(),
        logger=mock.MagicMock(),
        json_logger=mock.MagicMock(),
        runtime_info={"app_version": "test"},
    )
    monkeypatch.setattr(st, "proxy_state", state)

    save_account_cred({
        "uid": "fake-uid-1", "nickname": "Fake",
        "access_token": "fake-token", "refresh_token": "fake-rt",
        "expires_at": int(time.time()) + 99999, "region": "cn",
    })

    return SimpleNamespace(captured=captured, provider=provider, state=state)


def _post(client, model: str, stream: bool = False) -> dict:
    body = {"model": model, "stream": stream,
            "messages": [{"role": "user", "content": "hi"}]}
    if stream:
        body["stream_options"] = {"include_usage": True}
    return body


# ---------------------------------------------------------------------------
# 集成：forward → 所有发送调用都带一致的 is_max_mode
# ---------------------------------------------------------------------------
def test_forward_native_injects_when_switch_on(capture_env, monkeypatch):
    monkeypatch.setattr(max_mode_mod, "max_mode_enabled", lambda: True)
    client = TestClient(m.app)
    client.post("/v1/chat/completions", json=_post(client, "glm-5.4"))
    assert capture_env.captured, "至少应有一次上游调用"
    for path, body in capture_env.captured:
        assert body.get("is_max_mode") is True, (
            f"{path} 路径漏注入 is_max_mode：{body}")


def test_forward_no_inject_when_switch_off(capture_env, monkeypatch):
    monkeypatch.setattr(max_mode_mod, "max_mode_enabled", lambda: False)
    client = TestClient(m.app)
    client.post("/v1/chat/completions", json=_post(client, "glm-5.4"))
    assert capture_env.captured
    for path, body in capture_env.captured:
        assert "is_max_mode" not in body, (
            f"开关关时 {path} 不应注入：{body}")


def test_forward_unsupported_model_never_injects(capture_env, monkeypatch):
    """模型不支持 Max 时，即使开关开也不注入（且不声明大窗口）。"""
    monkeypatch.setattr(max_mode_mod, "max_mode_enabled", lambda: True)
    client = TestClient(m.app)
    client.post("/v1/chat/completions",
                json=_post(client, "doubao-seed-code"))
    assert capture_env.captured
    for path, body in capture_env.captured:
        assert "is_max_mode" not in body


def test_stream_forward_carries_max_mode(capture_env, monkeypatch):
    """流式路径同样传播：_stream 读线程内的发送调用带 max_mode。"""
    monkeypatch.setattr(max_mode_mod, "max_mode_enabled", lambda: True)
    client = TestClient(m.app)
    with client.stream("POST", "/v1/chat/completions",
                       json=_post(client, "glm-5.4", stream=True)) as r:
        r.read()
    assert capture_env.captured
    for path, body in capture_env.captured:
        assert body.get("is_max_mode") is True


# ---------------------------------------------------------------------------
# 集成：窗口声明 与 注入决策 的不变量（同开关、同模型下两者必须一致）
# ---------------------------------------------------------------------------
def test_window_declaration_matches_inject_decision(capture_env, monkeypatch):
    """models() 声明的 context_window == max_window 当且仅当 _max_inject 为真。

    这是本特性的核心不变量：声明大窗口就必须真开 Max，否则守卫放行 1M
    却打到上游 dev 墙 → 上游 400。逐个模型在开关开/关两态下校验。
    """
    p = capture_env.provider
    cn_models = [m["id"] for m in p.models()]
    assert "glm-5.4" in cn_models, "glm-5.4 应在 CN 目录（支持 Max）"

    for enabled in (True, False):
        monkeypatch.setattr(max_mode_mod, "max_mode_enabled", lambda e=enabled: e)
        # 同一次开关快照下，models() 与 _max_inject 都现读，必须一致
        for entry in p.models():
            mid = entry["id"]
            declared = entry.get("context_window")
            inject = p._max_inject(mid)
            if inject:
                assert declared is not None and declared > 0, (
                    f"{mid}: 要注入 Max 但没声明窗口")
                # 声明的窗口必须是 max 档（> dev）：对 glm-5.4 即 1M
                static = max_mode_mod.static_windows(p._region.key, mid)
                if static is not None:
                    assert declared == static[1], (
                        f"{mid}: 注入时声明的窗口应等于 max 档 {static[1]}，"
                        f"实际 {declared}")
            else:
                # 不注入时，若有静态兜底，声明的必须是 dev 档（不能是 max）
                static = max_mode_mod.static_windows(p._region.key, mid)
                if static is not None:
                    assert declared == static[0], (
                        f"{mid}: 不注入时声明的窗口应等于 dev 档 {static[0]}，"
                        f"实际 {declared}（开关 {enabled}）")


def test_invariant_holds_with_discovered_override(monkeypatch):
    """刷新目录后，discovered 真值覆盖静态表时，声明窗口仍须与注入一致。

    这是不变量最容易被改坏的分支：models() 合并 discovered 时若漏写字段，
    或 _max_inject 查 discovered 的 key 与 models() 不一致，就会出现
    「声明 max 窗口却不注入」。CN + intl 两通道、开关开/关两态全覆盖。
    """
    cases = [
        (TraeProvider, "cn", {
            "glm-5.4": {"max_mode": True, "context_window": 116000,
                        "max_window": 1000000, "display_name": "GLM-5.4"},
            "doubao-seed-code": {"max_mode": False, "context_window": 32000,
                                 "max_window": 32000},
        }),
        (TraeIntlProvider, "global", {
            "gpt-6.1-sol": {"max_mode": True, "context_window": 272000,
                            "max_window": 1000000, "display_name": "GPT-6.1-Sol"},
            "gemini-4-neon": {"max_mode": True, "context_window": 968000,
                              "max_window": 1000000},
            "kimi-k3": {"max_mode": False, "context_window": 200000,
                        "max_window": 200000},
        }),
    ]
    for cls, region, disc in cases:
        p = cls()
        p._discovered = disc
        for enabled in (True, False):
            monkeypatch.setattr(max_mode_mod, "max_mode_enabled",
                                lambda e=enabled: e)
            for entry in p.models():
                mid = entry["id"]
                declared = entry.get("context_window")
                inject = p._max_inject(mid)
                # 不变量：注入 ⟺ 声明的是 max 档（> dev 窗口）
                if inject:
                    assert declared is not None and declared > 0, (
                        f"{region} {mid}: 注入但未声明窗口 (enabled={enabled})")
                if declared is not None:
                    # 用 discovered 真值判断 dev/max：max_window>context 时，
                    # 注入应声明 max_window，不注入应声明 context(dev)
                    d = disc.get(mid) or {}
                    if d.get("max_window") and d["max_window"] > d.get("context_window", 0):
                        if inject:
                            assert declared == d["max_window"], (
                                f"{region} {mid}: 注入应声明 max {d['max_window']}，"
                                f"实际 {declared}")
                        else:
                            assert declared == d["context_window"], (
                                f"{region} {mid}: 不注入应声明 dev "
                                f"{d['context_window']}，实际 {declared}")
