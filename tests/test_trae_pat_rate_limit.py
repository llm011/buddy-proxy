"""TRAE PAT 频控：限频码换号/429、窗口节流，以及 tool 结果图片挪位（完全离线）。"""
from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from fastapi import HTTPException

from buddy_proxy.trae import pat
from buddy_proxy.trae.native_tools import _native_messages
from buddy_proxy.trae.pat import pacing


_OK = 'event: output\ndata: {"response":"ok"}\n\nevent: done\ndata: {}\n\n'
_RATE_LIMITED = 'event: error\ndata: {"code":4050,"message":"queue full"}\n\n'


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    for name in ("TRAE_PAT_BEARER", "TRAE_PAT_BEARER_PROFILES", "TRAE_PAT_PACE_MAX_WAIT_S"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRAE_PAT_TOKEN_FILE", str(tmp_path / "pat-cache.json"))
    monkeypatch.setenv("TRAE_PAT_PLUS_GATEWAY", "https://offline.invalid")
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv(
        "TRAE_PAT_BEARER_PROFILES",
        json.dumps([{"id": "primary", "bearer": "b-primary", "priority": 0},
                    {"id": "backup", "bearer": "b-backup", "priority": 1}]))
    monkeypatch.setattr(pat, "_reload_pat_models", lambda: None)
    monkeypatch.setattr(pat, "PAT_MODELS", {"m": ("up-m", "cfg-m")})
    monkeypatch.setattr(pat, "PAT_PLUS_MODELS", {"m": ("up-m", "cfg-m")})
    monkeypatch.setattr(pat, "PAT_PUBLIC_MODELS", {})
    monkeypatch.setattr(pat, "_refresh_locks", {})
    monkeypatch.setattr(pat, "_quota_code_hits", {})
    monkeypatch.setattr(pat, "_channel_exhausted", {})
    monkeypatch.setattr(pat, "pat_model_meta", lambda: {})
    monkeypatch.setattr(
        pat, "_get_profile_credentials",
        lambda profile, **_: pat.PatCredentials(
            token=f"t-{profile.id}", uid=f"uid-{profile.id}",
            machine_id="m", device_id="d"))
    pacing._reset_for_tests()
    yield
    pacing._reset_for_tests()


def _limit(monkeypatch, requests: int, window_s: float) -> None:
    monkeypatch.setattr(pat, "pat_model_meta",
                        lambda: {"m": {"rate_limit": {"requests": requests, "window_s": window_s}}})


# ── 限频码：换号，全部撞上报 429（不是 502 带内错误） ──

def test_nonstream_rate_limit_code_switches_account(monkeypatch):
    calls: list[str] = []

    def post(_url, _payload, credentials, _stream):
        calls.append(credentials.uid)
        return _RATE_LIMITED if credentials.uid == "uid-primary" else _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    assert pat.send_pat_native([], "m", False) == _OK
    assert calls == ["uid-primary", "uid-backup"]


def test_nonstream_all_accounts_rate_limited_raises_429(monkeypatch):
    monkeypatch.setattr(pat, "_post_chat", lambda *_a: _RATE_LIMITED)
    with pytest.raises(HTTPException) as caught:
        pat.send_pat_native([], "m", False)
    assert caught.value.status_code == 429


def test_rate_limited_account_skipped_on_next_request(monkeypatch):
    monkeypatch.setenv("TRAE_PAT_PACE_MAX_WAIT_S", "0")
    calls: list[str] = []

    def post(_url, _payload, credentials, _stream):
        calls.append(credentials.uid)
        return _RATE_LIMITED if credentials.uid == "uid-primary" else _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    pat.send_pat_native([], "m", False)
    calls.clear()
    pat.send_pat_native([], "m", False)
    assert calls == ["uid-backup"]  # primary 仍在限频短冷却，不再白撞


def test_stream_rate_limit_before_commit_switches_and_all_raise_429(monkeypatch):
    def source(_url, _payload, credentials, _stop):
        if credentials.uid == "uid-primary":
            yield "error", {"code": 4050, "message": "queue full"}
            return
        yield "output", {"response": "ok"}
        yield "done", {}

    monkeypatch.setattr(pat, "_stream_profile_events", source)
    assert list(pat.stream_pat_native([], "m")) == [
        ("output", {"response": "ok"}), ("done", {})]

    pacing._reset_for_tests()

    def always(_url, _payload, _credentials, _stop):
        yield "error", {"code": 4051, "message": "busy"}

    monkeypatch.setattr(pat, "_stream_profile_events", always)
    with pytest.raises(HTTPException) as caught:
        list(pat.stream_pat_native([], "m"))
    assert caught.value.status_code == 429


def test_stream_rate_limit_after_commit_is_passed_through(monkeypatch):
    """首个语义事件之后绝不换号重放（防重复计费）：错误原样交给下游。"""
    def source(_url, _payload, _credentials, _stop):
        yield "output", {"response": "partial"}
        yield "error", {"code": 4050, "message": "queue full"}

    monkeypatch.setattr(pat, "_stream_profile_events", source)
    events = list(pat.stream_pat_native([], "m"))
    assert events[-1] == ("error", {"code": 4050, "message": "queue full"})


def test_rate_limit_cooldown_is_per_model(monkeypatch):
    profile = pat.ensure_pat_config()[0]
    pacing.mark_rate_limited(profile, "m", 4050)
    assert pacing.try_reserve(profile, "m") is False
    assert pacing.try_reserve(profile, "other") is True


# ── 窗口节流 ──

def test_pacing_spreads_across_accounts_then_429_when_exhausted(monkeypatch):
    _limit(monkeypatch, 1, 60)
    monkeypatch.setenv("TRAE_PAT_PACE_MAX_WAIT_S", "0")
    calls: list[str] = []

    def post(_url, _payload, credentials, _stream):
        calls.append(credentials.uid)
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    pat.send_pat_native([], "m", False)
    pat.send_pat_native([], "m", False)
    assert calls == ["uid-primary", "uid-backup"]
    with pytest.raises(HTTPException) as caught:
        pat.send_pat_native([], "m", False)
    assert caught.value.status_code == 429
    assert calls == ["uid-primary", "uid-backup"]  # 没有名额就不发请求


def test_pacing_waits_bounded_for_window_to_free(monkeypatch):
    _limit(monkeypatch, 1, 0.3)
    monkeypatch.setenv("TRAE_PAT_PACE_MAX_WAIT_S", "5")
    monkeypatch.setattr(pat, "_post_chat", lambda *_a: _OK)
    for _ in range(3):  # 2 账号各 1 名额，第 3 次需等窗口滑走
        assert pat.send_pat_native([], "m", False) == _OK


def test_wait_for_slot_honours_stop():
    _profiles = pat.ensure_pat_config()
    for p in _profiles:
        pacing.mark_rate_limited(p, "m")
    stop = threading.Event()
    stop.set()
    assert pacing.wait_for_slot(_profiles, "m", stop) is False


def test_invalid_rate_limit_config_disables_pacing(monkeypatch):
    monkeypatch.setattr(pat, "pat_model_meta",
                        lambda: {"m": {"rate_limit": {"requests": "x", "window_s": 0}}})
    profile = pat.ensure_pat_config()[0]
    assert all(pacing.try_reserve(profile, "m") for _ in range(20))


# ── tool 结果里的图片挪到随后的 user 消息 ──

def _img(url: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": url}}


def test_tool_result_images_moved_after_consecutive_tool_messages():
    msgs = [
        {"role": "user", "content": "look"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "Read", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": [_img("data:image/png;base64,AA")]},
        {"role": "tool", "tool_call_id": "b", "content": "text only"},
        {"role": "user", "content": "next"},
    ]
    out = _native_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "tool", "user", "user"]
    assert all(b["type"] != "image_url" for b in out[2]["content"])
    assert out[4]["content"][1:] == [_img("data:image/png;base64,AA")]
    assert out[5]["content"] == [{"type": "text", "text": "next"}]


def test_tool_result_image_at_end_of_history_flushed():
    msgs = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": [_img("u")]},
    ]
    out = _native_messages(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "user"]
    assert out[-1]["content"][-1] == _img("u")


def test_user_images_untouched():
    out = _native_messages([{"role": "user", "content": [_img("u"), {"type": "text", "text": "q"}]}])
    assert out == [{"role": "user", "content": [_img("u"), {"type": "text", "text": "q"}]}]
