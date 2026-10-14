"""TRAE PAT 多账号 failover 回归测试（完全离线）。

所有凭证、缓存和上游响应均由 tmp_path/monkeypatch 提供；本文件不会读取真实
PAT 服务、用户主目录缓存、.env 或 .token.md。
"""
from __future__ import annotations

import asyncio
import errno
import io
import json
import os
import stat
import threading
import time
import urllib.error
from typing import Any

import pytest
from fastapi import HTTPException

from buddy_proxy.trae import pat
from buddy_proxy.trae import provider as provider_module
from buddy_proxy.trae.pat_provider import TraePatProvider
from buddy_proxy.trae.sse import _SSEDecoder


_OK = 'event: output\ndata: {"response":"ok"}\n\nevent: done\ndata: {}\n\n'


@pytest.fixture(autouse=True)
def _isolate_state_files(monkeypatch, tmp_path):
    """隔离真实状态文件：冷却/额度写入必须落在临时目录，不污染 ~/.buddy-proxy。"""
    monkeypatch.setenv("TRAE_PAT_TOKEN_FILE", str(tmp_path / "trae_pat_token.json"))
    monkeypatch.setenv("TRAE_WORK_CRED_PATH", str(tmp_path / "trae_work.json"))
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(pat, "_quota_code_hits", {})
    monkeypatch.setattr(pat, "_channel_exhausted", {})
    yield


def _profiles(*items: dict[str, Any]) -> str:
    return json.dumps(list(items), ensure_ascii=False)


def _http_error(status: int, *, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return urllib.error.HTTPError(
        "https://offline.invalid/chat", status, "upstream", headers, io.BytesIO(b"secret upstream body")
    )


@pytest.fixture(autouse=True)
def isolated_pat_environment(tmp_path, monkeypatch):
    """隔离环境变量、模型目录和账号缓存，禁止测试碰真实服务或用户缓存。"""
    for name in (
        "TRAE_PAT_BEARER",
        "TRAE_PAT_BEARER_PROFILES",
        "TRAE_PAT_AUTH_URL",
        "TRAE_PAT_TOKEN_URL",
        "TRAE_PAT_PLUS_GATEWAY",
        "TRAE_PAT_TOKEN_FILE",
        "WB_TRAE_NATIVE_TOOLS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRAE_PAT_TOKEN_FILE", str(tmp_path / "pat-cache.json"))
    monkeypatch.setenv("TRAE_PAT_PLUS_GATEWAY", "https://offline.invalid")

    # 不读取/热加载仓库外配置；每个测试获得确定的高级/标准模型目录。
    monkeypatch.setattr(pat, "_reload_pat_models", lambda: None)
    monkeypatch.setattr(
        pat,
        "PAT_MODELS",
        {"advanced-model": ("upstream-advanced", "advanced-config"),
         "standard-model": ("upstream-standard", "standard-config")},
    )
    monkeypatch.setattr(
        pat, "PAT_PLUS_MODELS", {"advanced-model": ("upstream-advanced", "advanced-config")}
    )
    monkeypatch.setattr(
        pat, "PAT_PUBLIC_MODELS", {"standard-model": ("upstream-standard", "standard-config")}
    )
    # 避免不同测试共享刷新锁对象带来顺序依赖。
    monkeypatch.setattr(pat, "_refresh_locks", {})


def _configure_two(monkeypatch, *, first_priority: int = 0, second_priority: int = 1) -> None:
    monkeypatch.setenv(
        "TRAE_PAT_BEARER_PROFILES",
        _profiles(
            {"id": "primary", "bearer": "bearer-primary", "priority": first_priority},
            {"id": "backup", "bearer": "bearer-backup", "priority": second_priority},
        ),
    )


def _fake_credentials(profile: pat.PatProfile, **_: Any) -> pat.PatCredentials:
    return pat.PatCredentials(
        token=f"token-{profile.id}",
        uid=f"uid-{profile.id}",
        machine_id=f"machine-{profile.id}",
        device_id=f"device-{profile.id}",
    )


def _install_credentials(monkeypatch) -> None:
    monkeypatch.setattr(pat, "_get_profile_credentials", _fake_credentials)


# ---------------------------------------------------------------------------
# 严格 profiles 配置、默认值、旧 bearer 和脱敏错误
# ---------------------------------------------------------------------------
def test_profiles_optional_id_priority_defaults_and_stable_sort(monkeypatch):
    monkeypatch.setenv(
        "TRAE_PAT_BEARER_PROFILES",
        _profiles(
            {"bearer": "b-default"},
            {"id": "same-a", "bearer": "b-a", "priority": 7},
            {"id": "same-b", "bearer": "b-b", "priority": 7},
            {"id": "first", "bearer": "b-first", "priority": 1},
        ),
    )

    loaded = pat.ensure_pat_config()
    assert [(p.id, p.priority, p.index) for p in loaded] == [
        ("profile-0", 0, 0),
        ("first", 1, 3),
        ("same-a", 7, 1),
        ("same-b", 7, 2),
    ]


def test_legacy_bearer_only_used_when_profiles_absent(monkeypatch):
    monkeypatch.setenv("TRAE_PAT_BEARER", " legacy-bearer ")
    loaded = pat.ensure_pat_config()
    assert [(p.id, p.bearer, p.priority) for p in loaded] == [
        ("legacy", "legacy-bearer", 0)
    ]

    # profiles 只要存在就 fail closed，不能因其无效而偷偷回退旧 bearer。
    monkeypatch.setenv("TRAE_PAT_BEARER_PROFILES", "[]")
    with pytest.raises(HTTPException) as caught:
        pat.ensure_pat_config()
    assert caught.value.status_code == 503
    assert "legacy-bearer" not in str(caught.value.detail)


@pytest.mark.parametrize(
    "raw",
    [
        "not-json-super-secret",
        json.dumps({"bearer": "secret-not-array"}),
        "[]",
        _profiles({"id": "bad id", "bearer": "secret-id"}),
        _profiles({"id": "ok", "bearer": " secret-space"}),
        _profiles({"id": "ok", "bearer": "secret", "priority": True}),
        _profiles({"id": "ok", "bearer": "secret", "priority": 1001}),
        _profiles({"id": "ok", "bearer": "secret", "extra": "secret-extra"}),
        _profiles(
            {"id": "duplicate", "bearer": "secret-one"},
            {"id": "duplicate", "bearer": "secret-two"},
        ),
        _profiles(
            {"id": "one", "bearer": "same-secret"},
            {"id": "two", "bearer": "same-secret"},
        ),
    ],
)
def test_profiles_reject_invalid_or_duplicate_without_leaking_secrets(monkeypatch, raw):
    monkeypatch.setenv("TRAE_PAT_BEARER_PROFILES", raw)
    monkeypatch.setenv("TRAE_PAT_BEARER", "legacy-must-not-leak-or-fallback")

    with pytest.raises(HTTPException) as caught:
        pat.ensure_pat_config()

    assert caught.value.status_code == 503
    detail = str(caught.value.detail)
    assert "PAT 多账号配置无效" in detail
    for secret in (
        "not-json-super-secret", "secret-not-array", "secret-id", "secret-space",
        "secret-extra", "secret-one", "secret-two", "same-secret",
        "legacy-must-not-leak-or-fallback",
    ):
        assert secret not in detail


# ---------------------------------------------------------------------------
# 稳定主备、额度池隔离与 SSE allowlist
# ---------------------------------------------------------------------------
def test_priority_is_stable_primary_then_backup(monkeypatch):
    monkeypatch.setenv(
        "TRAE_PAT_BEARER_PROFILES",
        _profiles(
            {"id": "later", "bearer": "b-later", "priority": 9},
            {"id": "primary", "bearer": "b-primary", "priority": 1},
            {"id": "same-priority", "bearer": "b-same", "priority": 1},
        ),
    )
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def post(_url, _payload, credentials, _stream):
        calls.append(credentials.uid)
        if credentials.uid == "uid-primary":
            raise _http_error(403)
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    assert pat.send_pat_native([], "advanced-model", False) == _OK
    assert calls == ["uid-primary", "uid-same-priority"]


def test_advanced_and_standard_cooldowns_are_isolated(monkeypatch):
    _configure_two(monkeypatch)
    primary, backup = pat.ensure_pat_config()
    monkeypatch.setattr(pat.time, "time", lambda: 1_700_000_000.0)

    pat._mark_cooldown(primary, "advanced")

    assert [p.id for p in pat._ordered_available_profiles("advanced")] == [backup.id]
    assert [p.id for p in pat._ordered_available_profiles("standard")] == [primary.id, backup.id]
    state = pat._account_state(primary.cache_key)
    assert "advanced" in state["cooldowns"]
    assert "standard" not in state["cooldowns"]


@pytest.mark.parametrize(
    "raw",
    [
        'event: error\ndata: {"code":4008,"message":"quota"}\n\n',
        'event: error\ndata: {"code":"4227","message":"quota"}\n\n',
        '{"code": 4008, "message": "quota"}',
        '{"code": "4220", "message": "quota"}',
    ],
)
def test_sse_numeric_string_and_bare_json_allowlist_fail_over(monkeypatch, raw):
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def post(_url, _payload, credentials, _stream):
        calls.append(credentials.uid)
        return raw if credentials.uid == "uid-primary" else _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    assert pat.send_pat_native([], "standard-model", False) == _OK
    assert calls == ["uid-primary", "uid-backup"]


@pytest.mark.parametrize(
    "raw",
    [
        'event: error\ndata: {"code":3003,"message":"no switch"}\n\n',
        'event: error\ndata: {"code":"4225","message":"no switch"}\n\n',
        'event: error\ndata: {"code":987654,"message":"unknown"}\n\n',
        '{"code": 3003, "message": "no switch"}',
        '{"code": "4225", "message": "no switch"}',
        '{"code": 987654, "message": "unknown"}',
    ],
)
def test_non_allowlisted_sse_and_json_errors_do_not_switch(monkeypatch, raw):
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def post(_url, _payload, credentials, _stream):
        calls.append(credentials.uid)
        return raw

    monkeypatch.setattr(pat, "_post_chat", post)
    if raw.lstrip().startswith("{"):
        # 裸 JSON 错误体在 SSE 解析层认不出，保持"不切号"的同时必须显式报错，
        # 不能被吞成空响应。
        with pytest.raises(HTTPException) as caught:
            pat.send_pat_native([], "standard-model", False)
        assert caught.value.status_code == 502
        assert "code" in str(caught.value.detail)
    else:
        assert pat.send_pat_native([], "standard-model", False) == raw
    assert calls == ["uid-primary"]


# ---------------------------------------------------------------------------
# 增量 SSE 与真流式主备边界
# ---------------------------------------------------------------------------
def test_incremental_sse_decoder_handles_arbitrary_utf8_and_line_splits():
    raw = (
        'event: output\r\ndata: {"response":"你' + '好"}\r\n\r\n'
        'event: done\ndata: {}\n\n'
    ).encode("utf-8")
    decoder = _SSEDecoder()
    events = []
    for byte in raw:
        events.extend(decoder.feed(bytes([byte])))
    events.extend(decoder.finish())
    assert events == [("output", {"response": "你好"}), ("done", {})]


def test_stream_pat_native_emits_output_before_source_finishes(monkeypatch):
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    resumed = False

    def source(_url, _payload, _credentials, _stop):
        nonlocal resumed
        yield "output", {"response": "first"}
        resumed = True
        yield "done", {}

    monkeypatch.setattr(pat, "_stream_profile_events", source)
    stream = pat.stream_pat_native([], "standard-model")
    assert next(stream) == ("output", {"response": "first"})
    assert resumed is False
    assert list(stream) == [("done", {})]
    assert resumed is True


def test_stream_failover_only_before_semantic_commit(monkeypatch):
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def source(_url, _payload, credentials, _stop):
        calls.append(credentials.uid)
        if credentials.uid == "uid-primary":
            yield "error", {"code": 4008, "message": "quota"}
        else:
            yield "output", {"response": "backup"}
            yield "done", {}

    monkeypatch.setattr(pat, "_stream_profile_events", source)
    assert list(pat.stream_pat_native([], "standard-model")) == [
        ("output", {"response": "backup"}), ("done", {})]
    assert calls == ["uid-primary", "uid-backup"]


def test_stream_does_not_switch_after_semantic_commit(monkeypatch):
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def source(_url, _payload, credentials, _stop):
        calls.append(credentials.uid)
        yield "output", {"response": "partial"}
        yield "error", {"code": 4031, "message": "late"}

    monkeypatch.setattr(pat, "_stream_profile_events", source)
    assert list(pat.stream_pat_native([], "standard-model")) == [
        ("output", {"response": "partial"}),
        ("error", {"code": 4031, "message": "late"}),
    ]
    assert calls == ["uid-primary"]


@pytest.mark.parametrize(
    "events, detail",
    [
        ([("done", {})], "no content"),
        ([("token_usage", {"completion_tokens": 0}), ("done", {})], "no content"),
        ([("output", {"response": "partial"})], "before completion"),
    ],
)
def test_stream_rejects_empty_or_incomplete_success(monkeypatch, events, detail):
    """空 done / 未收尾：换号重试耗尽后仍必须显式报错，不能吞成空响应。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def source(*_args, credentials=None, **_kw):
        calls.append(credentials.uid if credentials is not None else "anon")
        return iter(events)

    monkeypatch.setattr(pat, "_stream_profile_events", source)
    stream = pat.stream_pat_native([], "standard-model")
    with pytest.raises(HTTPException) as caught:
        list(stream)
    assert caught.value.status_code == 502
    assert detail in str(caught.value.detail)
    if detail == "no content":
        # 空响应假成功：两个账号各重试一次（上限 2），仍未拿到内容才报错。
        assert len(calls) == 2
    else:
        # 已产出语义内容但缺 done：不能换号重放（防重复计费/副作用）。
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# 传输/HTTP/刷新边界：只对明确账号级信号切号
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError(OSError(errno.ECONNREFUSED, "offline")),
        TimeoutError("offline timeout"),
        _http_error(500),
        _http_error(503),
    ],
)
def test_connection_timeout_and_5xx_do_not_switch_accounts(monkeypatch, failure):
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def post(_url, _payload, credentials, _stream):
        calls.append(credentials.uid)
        raise failure

    monkeypatch.setattr(pat, "_post_chat", post)
    with pytest.raises(HTTPException) as caught:
        pat.send_pat_native([], "standard-model", False)
    assert caught.value.status_code == 502
    assert calls == ["uid-primary"]


@pytest.mark.parametrize("status", [403, 429])
def test_http_403_and_429_switch_to_backup(monkeypatch, status):
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def post(_url, _payload, credentials, _stream):
        calls.append(credentials.uid)
        if credentials.uid == "uid-primary":
            raise _http_error(status, retry_after="60")
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    assert pat.send_pat_native([], "advanced-model", False) == _OK
    assert calls == ["uid-primary", "uid-backup"]


# ---------------------------------------------------------------------------
# 请求级账号上报（metrics.ACCOUNT_META holder → 最近请求表格）
# ---------------------------------------------------------------------------
def test_send_and_stream_report_used_account(monkeypatch):
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    monkeypatch.setattr(pat, "_post_chat", lambda *_a, **_k: _OK)

    meta: dict[str, Any] = {}
    assert pat.send_pat_native([], "standard-model", False, meta=meta) == _OK
    assert meta == {"account": "primary"}


def test_stream_reports_used_account(monkeypatch):
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)

    def source(*_args, **_kwargs):
        yield ("output", {"response": "ok"})
        yield ("done", {})

    monkeypatch.setattr(pat, "_stream_profile_events", source)
    meta: dict[str, Any] = {}
    assert list(pat.stream_pat_native([], "standard-model", meta=meta)) == [
        ("output", {"response": "ok"}), ("done", {})]
    assert meta == {"account": "primary"}


def test_failover_reports_final_account(monkeypatch):
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)

    def post(_url, _payload, credentials, _stream):
        if credentials.uid == "uid-primary":
            raise _http_error(403, retry_after="60")
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    meta: dict[str, Any] = {}
    assert pat.send_pat_native([], "advanced-model", False, meta=meta) == _OK
    assert meta == {"account": "backup"}


def test_account_meta_is_optional(monkeypatch):
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    monkeypatch.setattr(pat, "_post_chat", lambda *_a, **_k: _OK)
    assert pat.send_pat_native([], "standard-model", False) == _OK


def test_401_refresh_service_failure_does_not_switch(monkeypatch):
    _configure_two(monkeypatch)
    credential_calls: list[tuple[str, bool]] = []
    post_calls: list[str] = []

    def credentials(profile, *, force_refresh=False, **_kwargs):
        credential_calls.append((profile.id, force_refresh))
        if force_refresh:
            raise HTTPException(status_code=502, detail="offline refresh failed")
        return _fake_credentials(profile)

    def post(_url, _payload, credentials, _stream):
        post_calls.append(credentials.uid)
        raise _http_error(401)

    monkeypatch.setattr(pat, "_get_profile_credentials", credentials)
    monkeypatch.setattr(pat, "_post_chat", post)
    with pytest.raises(HTTPException) as caught:
        pat.send_pat_native([], "standard-model", False)
    assert caught.value.status_code == 502
    assert credential_calls == [("primary", False), ("primary", True)]
    assert post_calls == ["uid-primary"]


def test_401_with_distinct_new_token_still_401_then_switches(monkeypatch):
    _configure_two(monkeypatch)
    credential_calls: list[tuple[str, bool, str | None]] = []
    post_calls: list[str] = []

    def credentials(profile, *, force_refresh=False, rejected_token=None):
        credential_calls.append((profile.id, force_refresh, rejected_token))
        token = "new-primary" if profile.id == "primary" and force_refresh else f"old-{profile.id}"
        return pat.PatCredentials(token, f"uid-{profile.id}", "machine", "device")

    def post(_url, _payload, credentials, _stream):
        post_calls.append(credentials.token)
        if credentials.uid == "uid-primary":
            raise _http_error(401)
        return _OK

    monkeypatch.setattr(pat, "_get_profile_credentials", credentials)
    monkeypatch.setattr(pat, "_post_chat", post)
    assert pat.send_pat_native([], "standard-model", False) == _OK
    assert post_calls == ["old-primary", "new-primary", "old-backup"]
    assert credential_calls == [
        ("primary", False, None),
        ("primary", True, "old-primary"),
        ("backup", False, None),
    ]


def test_payload_is_byte_identical_across_accounts(monkeypatch):
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    payloads: list[bytes] = []

    def post(_url, payload, credentials, _stream):
        payloads.append(payload)
        if credentials.uid == "uid-primary":
            raise _http_error(429)
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    messages = [{"role": "user", "content": [{"type": "text", "text": "逐字节一致"}]}]
    tools = [{"type": "function", "function": {
        "name": "lookup", "description": "离线", "parameters": {"type": "object"}
    }}]
    assert pat.send_pat_native(messages, "advanced-model", True, tools) == _OK
    assert len(payloads) == 2
    assert payloads[0] == payloads[1]


# ---------------------------------------------------------------------------
# provider 路由和缓存权限/账号隔离
# ---------------------------------------------------------------------------
def test_pat_uses_pat_hook_even_when_native_tools_disabled(monkeypatch):
    monkeypatch.setenv("TRAE_PAT_BEARER_PROFILES", _profiles({"bearer": "offline-bearer"}))
    monkeypatch.setenv("WB_TRAE_NATIVE_TOOLS", "0")
    monkeypatch.setattr(provider_module, "_NATIVE_TOOLS_ENABLED", False)
    calls: list[tuple[str, bool]] = []
    provider = TraePatProvider()

    def pat_hook(native_msgs, model, stream, tools):
        calls.append((model, stream))
        assert native_msgs[0]["content"][0]["text"] == "hello"
        return _OK

    monkeypatch.setattr(provider, "_send_native_request", pat_hook)
    assert provider._uses_native_mode() is True
    assert provider_module.TraeProvider()._uses_native_mode() is False

    response = asyncio.run(provider.forward(
        {"model": "standard-model", "stream": False,
         "messages": [{"role": "user", "content": "hello"}]},
        "openai",
    ))
    assert json.loads(response.body)["choices"][0]["message"]["content"] == "ok"
    assert calls == [("standard-model", False)]


def test_cache_is_0600_and_tokens_are_isolated_by_account(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TRAE_PAT_BEARER_PROFILES",
        _profiles(
            {"id": "account-a", "bearer": "bearer-A", "priority": 0},
            {"id": "account-b", "bearer": "bearer-B", "priority": 1},
        ),
    )
    now = 1_700_000_000.0
    monkeypatch.setattr(pat.time, "time", lambda: now)

    def exchange(bearer):
        assert bearer in {"bearer-A", "bearer-B"}
        suffix = bearer[-1]
        return f"cloud-token-{suffix}", f"uid-{suffix}", now + 86_400

    monkeypatch.setattr(pat, "_exchange", exchange)
    profiles = pat.ensure_pat_config()
    creds_a = pat._get_profile_credentials(profiles[0])
    creds_b = pat._get_profile_credentials(profiles[1])

    cache_path = tmp_path / "pat-cache.json"
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600
    document = json.loads(cache_path.read_text("utf-8"))
    assert set(document["profiles"]) == {profiles[0].cache_key, profiles[1].cache_key}
    assert document["profiles"][profiles[0].cache_key]["cloud_ide_token"] == "cloud-token-A"
    assert document["profiles"][profiles[1].cache_key]["cloud_ide_token"] == "cloud-token-B"
    assert creds_a.token == "cloud-token-A" and creds_b.token == "cloud-token-B"
    assert creds_a.machine_id != creds_b.machine_id
    assert creds_a.device_id != creds_b.device_id
    # bearer 本身不应落盘；cache key 只保留不可逆摘要。
    serialized = cache_path.read_text("utf-8")
    assert "bearer-A" not in serialized and "bearer-B" not in serialized


def test_initial_credential_failure_skips_to_next_account(monkeypatch):
    """单账号换不到凭据时跳过（不冷却、不重放），通道不因它整体失败。"""
    _configure_two(monkeypatch)
    calls: list[str] = []

    def credentials(profile, **_kwargs):
        calls.append(profile.id)
        if profile.id == "primary":
            raise HTTPException(status_code=502, detail="credential service unavailable")
        return pat.PatCredentials("token-backup", "u2", "m2", "d2")

    monkeypatch.setattr(pat, "_get_profile_credentials", credentials)
    monkeypatch.setattr(pat, "_post_chat", lambda *args, **kwargs: _OK)
    out = pat.send_pat_native([], "standard-model", False)
    assert "ok" in out
    assert calls == ["primary", "backup"]


def test_all_accounts_fail_credentials_reports_unavailable(monkeypatch):
    _configure_two(monkeypatch)
    calls: list[str] = []

    def credentials(profile, **_kwargs):
        calls.append(profile.id)
        raise HTTPException(status_code=502, detail="credential service unavailable")

    monkeypatch.setattr(pat, "_get_profile_credentials", credentials)
    with pytest.raises(HTTPException) as caught:
        pat.send_pat_native([], "standard-model", False)
    assert caught.value.status_code == 502
    assert "credential unavailable" in caught.value.detail
    assert calls == ["primary", "backup"]


def test_account_cooldown_applies_to_both_model_pools(monkeypatch):
    _configure_two(monkeypatch)
    primary, backup = pat.ensure_pat_config()
    monkeypatch.setattr(pat.time, "time", lambda: 1_700_000_000.0)

    pat._mark_account_cooldown(primary, retry_after="60")

    assert [p.id for p in pat._ordered_available_profiles("advanced")] == [backup.id]
    assert [p.id for p in pat._ordered_available_profiles("standard")] == [backup.id]


def test_profile_cache_key_survives_id_and_order_changes(monkeypatch):
    monkeypatch.setenv(
        "TRAE_PAT_BEARER_PROFILES",
        _profiles(
            {"id": "old-a", "bearer": "stable-A", "priority": 0},
            {"id": "old-b", "bearer": "stable-B", "priority": 1},
        ),
    )
    original = {profile.bearer: profile.cache_key for profile in pat.ensure_pat_config()}
    monkeypatch.setenv(
        "TRAE_PAT_BEARER_PROFILES",
        _profiles(
            {"id": "renamed-b", "bearer": "stable-B", "priority": 0},
            {"id": "renamed-a", "bearer": "stable-A", "priority": 1},
        ),
    )
    changed = {profile.bearer: profile.cache_key for profile in pat.ensure_pat_config()}
    assert changed == original


def test_retry_after_is_clamped_and_http_date_supported():
    now = 1_700_000_000.0
    assert pat._retry_after_seconds("999999999", now) == 7 * 86400
    assert pat._retry_after_seconds("0", now) == 1
    assert pat._retry_after_seconds("Wed, 15 Nov 2023 00:13:20 GMT", now) == 7200


# ───────────────────────── 凭证自愈（后台保活） ─────────────────────────


def test_keeper_round_refreshes_only_missing_or_expiring(monkeypatch):
    """环境可达时：健康账号不动，缺失/临期账号补签。"""
    _configure_two(monkeypatch)
    now = 1_700_000_000.0
    monkeypatch.setattr(pat.time, "time", lambda: now)
    monkeypatch.setattr(pat, "_exchange_env_ready", lambda: True)
    primary, backup = pat.ensure_pat_config()
    # primary：健康缓存（远未过期）；backup：无任何缓存
    pat._mutate_account(primary.cache_key, lambda st: st.update({
        "cloud_ide_token": "token-primary", "uid": "u1",
        "expires_at": now + 6 * 86400, "refreshed_at": now - 3600}))
    touched: list[str] = []

    def credentials(profile, **_kwargs):
        touched.append(profile.id)
        return _fake_credentials(profile)

    monkeypatch.setattr(pat, "_get_profile_credentials", credentials)
    result = pat._keeper_round()
    assert touched == ["backup"]
    assert result["env_ready"] is True
    assert result["refreshed"] == ["backup"]
    assert result["waiting"] == []


def test_keeper_round_short_circuits_when_env_unreachable(monkeypatch):
    """端点不可达时一轮内短路：不触发任何交换，账号进入 waiting。"""
    _configure_two(monkeypatch)
    monkeypatch.setattr(pat, "_exchange_env_ready", lambda: False)
    calls: list[str] = []

    def credentials(profile, **_kwargs):
        calls.append(profile.id)
        return _fake_credentials(profile)

    monkeypatch.setattr(pat, "_get_profile_credentials", credentials)
    result = pat._keeper_round()
    assert calls == []
    assert result["env_ready"] is False
    assert result["refreshed"] == []
    assert sorted(result["waiting"]) == ["backup", "primary"]


def test_keeper_round_heals_missing_account_after_recovery(monkeypatch):
    """离线期没换到 token 的账号，在环境恢复后的下一轮自动补上。"""
    _configure_two(monkeypatch)
    now = 1_700_000_000.0
    monkeypatch.setattr(pat.time, "time", lambda: now)
    monkeypatch.setattr(pat, "_exchange_env_ready", lambda: True)

    def exchange(bearer):
        return f"cloud-token-{bearer[-1]}", f"uid-{bearer[-1]}", now + 7 * 86400

    monkeypatch.setattr(pat, "_exchange", exchange)
    result = pat._keeper_round()
    assert sorted(result["refreshed"]) == ["backup", "primary"]
    profiles = {p.id: p for p in pat.ensure_pat_config()}
    assert pat._credentials_from_state(
        pat._account_state(profiles["primary"].cache_key)) is not None


def test_accounts_status_reports_token_cooldown_and_keeper(monkeypatch):
    _configure_two(monkeypatch)
    now = 1_700_000_000.0
    monkeypatch.setattr(pat.time, "time", lambda: now)
    primary, _ = pat.ensure_pat_config()
    pat._mutate_account(primary.cache_key, lambda st: st.update({
        "cloud_ide_token": "token-primary", "uid": "u1",
        "expires_at": now + 6 * 86400, "refreshed_at": now - 3600,
        "cooldowns": {"standard": now + 300}}))
    pat._keeper_last.update({"at": now, "env_ready": True, "refreshed": ["x"], "waiting": []})
    status = pat.accounts_status()
    by_id = {a["id"]: a for a in status["accounts"]}
    assert by_id["primary"]["display_index"] == 1
    assert by_id["backup"]["display_index"] == 2
    assert by_id["primary"]["token"] == "ok"
    assert by_id["primary"]["cooling"] == [{"kind": "standard", "minutes_left": 5}]
    assert by_id["backup"]["token"] == "missing"
    assert status["keeper"]["env_ready"] is True
    assert status["enabled"] is True


def _setup_status_catalog(monkeypatch):
    """注入一张含「仅 solo_agent 开放」模型的 plus 目录，供负载查询测试用。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    monkeypatch.setattr(pat, "_model_status_cache", {"fetched_at": 0.0, "data": None})
    monkeypatch.setattr(pat, "_reload_pat_models", lambda: None)
    monkeypatch.setattr(pat, "PAT_PLUS_MODELS", {
        "chatty": ("up-chatty", "chatty-cfg"),       # 默认 function
        "solo-only": ("up-solo", "solo-cfg"),        # 仅 solo_agent 开放
    })
    monkeypatch.setattr(pat, "PAT_MODEL_FUNCTIONS", {"solo-only": "solo_agent"})
    monkeypatch.setattr(pat, "pat_model_names",
                        lambda: ["chatty", "solo-only"])
    monkeypatch.setattr(pat, "pat_gateway_is_plus", lambda m: m in {"chatty", "solo-only"})
    monkeypatch.setattr(pat, "pat_model_meta", lambda: {
        "chatty": {"name": "Chatty", "max_input": 100},
        "solo-only": {"name": "Solo Only", "max_input": 200},
    })


def test_model_status_fetches_per_function_tables(monkeypatch):
    """仅在 solo_agent 下开放的模型，负载要从它自己的 function 表取，不是默认表。"""
    _setup_status_catalog(monkeypatch)
    calls: list[str] = []

    def fake_fetch(plus, creds, function):
        calls.append(function)
        if function == "chat_v3":
            return {"chatty-cfg": 42.0}          # 默认表没有 solo-cfg
        if function == "solo_agent":
            return {"solo-cfg": 88.0}            # 只有 solo 表有
        return {}

    monkeypatch.setattr(pat, "_fetch_hot_by_config", fake_fetch)
    out = pat.fetch_pat_model_status(force=True)
    by_id = {m["id"]: m for m in out["models"]}
    assert by_id["chatty"]["workload"] == 42.0
    assert by_id["solo-only"]["workload"] == 88.0   # 修复前这里会是 None
    assert set(calls) == {"chat_v3", "solo_agent"}  # 两张表都查了，各一次


def test_model_status_non_default_function_failure_degrades(monkeypatch):
    """非默认 function 查询失败只降级为该模型 workload=None，不拖垮其余模型。"""
    _setup_status_catalog(monkeypatch)

    def fake_fetch(plus, creds, function):
        if function == "chat_v3":
            return {"chatty-cfg": 42.0}
        raise urllib.error.URLError("solo table down")

    monkeypatch.setattr(pat, "_fetch_hot_by_config", fake_fetch)
    out = pat.fetch_pat_model_status(force=True)
    by_id = {m["id"]: m for m in out["models"]}
    assert by_id["chatty"]["workload"] == 42.0      # 默认表照常
    assert by_id["solo-only"]["workload"] is None   # solo 表挂了，降级 None


def test_model_status_default_function_failure_raises(monkeypatch):
    """默认 function 查询失败视为整体不可用，向上报 502（沿用旧语义）。"""
    _setup_status_catalog(monkeypatch)

    def fake_fetch(plus, creds, function):
        raise urllib.error.URLError("gateway down")

    monkeypatch.setattr(pat, "_fetch_hot_by_config", fake_fetch)
    with pytest.raises(HTTPException) as exc:
        pat.fetch_pat_model_status(force=True)
    assert exc.value.status_code == 502


def test_quota_items_distinguish_daily_model_families(monkeypatch):
    _configure_two(monkeypatch)
    profile = pat.ensure_pat_config()[0]
    data = {"user_entitlement_pack_list": [
        {"entitlement_base_info": {"entitlement_id": "free_weekly_x", "quota": {"basic_usage_limit": 300}}, "usage": {"basic_usage_amount": 1}},
        {"entitlement_base_info": {"entitlement_id": "free_daily_x_gpt_56_sol", "quota": {"basic_usage_limit": 58}}, "usage": {"basic_usage_amount": 2}},
        {"entitlement_base_info": {"entitlement_id": "free_daily_x_gpt_6", "quota": {"basic_usage_limit": 58}}, "usage": {"basic_usage_amount": 3}},
    ]}
    labels = [item["label"] for item in pat._quota_items(data, profile, True)]
    assert labels == [
        "PAT #1 · Orange 包",
        "PAT #1 · 日包（GPT-5.6 Sol）",
        "PAT #1 · 日包（GPT-6）",
    ]
    assert len(labels) == len(set(labels))


def test_quota_items_missing_usage_is_unknown_not_zero(monkeypatch):
    """只回了包容量、没回用量时，不能按 0 算。

    按 0 算会让 /ui 渲染成「剩 300 / 300 · 已用 0%」——把未知说成满血。
    正确口径：percent 留 None（UI 显示「已用未知」），remaining 仍给包容量。
    """
    _configure_two(monkeypatch)
    profile = pat.ensure_pat_config()[0]
    data = {"user_entitlement_pack_list": [
        {"entitlement_base_info": {"entitlement_id": "free_weekly_x",
                                   "quota": {"basic_usage_limit": 300}}},
    ]}
    (item,) = pat._quota_items(data, profile, True)
    assert item["total"] == 300
    assert item["remaining"] == 300
    assert item["used"] is None
    assert item["percent"] is None


def test_quota_items_zero_usage_is_a_real_zero(monkeypatch):
    """真回 0 时仍要显示 0%（别把「确实没花」也吞成未知）。"""
    _configure_two(monkeypatch)
    profile = pat.ensure_pat_config()[0]
    data = {"user_entitlement_pack_list": [
        {"entitlement_base_info": {"entitlement_id": "free_weekly_x",
                                   "quota": {"basic_usage_limit": 300}},
         "usage": {"basic_usage_amount": 0}},
    ]}
    (item,) = pat._quota_items(data, profile, True)
    assert item["used"] == 0
    assert item["remaining"] == 300
    assert item["percent"] == 0


def test_quota_items_end_time_is_a_reset_not_an_expiry(monkeypatch):
    """PAT 包的 ``end_time`` 是**周期重置**不是到期——走 reset_ts。

    weekly / daily 是到点回满的池（cooldown.py 实测：「日包按 00:00 重置」，
    4031 的 extra 分 daily/weekly 两池），本周期结束不等于包作废。判成到期
    的话，到期横幅会每天喊一次「PAT 日包要到期了」——其实只是又要刷新了。
    """
    _configure_two(monkeypatch)
    profile = pat.ensure_pat_config()[0]
    data = {"user_entitlement_pack_list": [
        {"entitlement_base_info": {"entitlement_id": "free_daily_x_gpt_6",
                                   "quota": {"basic_usage_limit": 58},
                                   "end_time": 1791046705},
         "usage": {"basic_usage_amount": 2}},
    ]}
    (item,) = pat._quota_items(data, profile, True)
    assert item["reset_ts"] == 1791046705, "周期重置时刻该给 reset_ts"
    assert item["expire_ts"] is None, "池到期会回满，不是权益作废，别给 expire_ts"


def test_model_function_override_used_in_payload(monkeypatch):
    """目录里带 function 覆盖的模型，payload 的 function 字段用覆盖值。"""
    _configure_two(monkeypatch)
    monkeypatch.setattr(
        pat, "PAT_MODELS",
        {"solo-model": ("solo-upstream", "solo-config"),
         "standard-model": ("upstream-standard", "standard-config")})
    monkeypatch.setattr(pat, "PAT_PLUS_MODELS", {"solo-model": ("solo-upstream", "solo-config")})
    monkeypatch.setattr(pat, "PAT_PUBLIC_MODELS",
                        {"standard-model": ("upstream-standard", "standard-config")})
    monkeypatch.setattr(pat, "PAT_MODEL_FUNCTIONS", {"solo-model": "solo_agent"})
    captured: dict[str, Any] = {}

    def post(url, payload, credentials, stream):
        captured["url"] = url
        captured["body"] = json.loads(payload.decode())
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    monkeypatch.setattr(pat, "_get_profile_credentials", _fake_credentials)
    pat.send_pat_native([{"role": "user", "content": "hi"}], "solo-model", False)
    assert captured["body"]["function"] == "solo_agent"
    pat.send_pat_native([{"role": "user", "content": "hi"}], "standard-model", False)
    assert captured["body"]["function"] == os.environ.get("WB_TRAE_NATIVE_FUNCTION", "chat_v3")


# ---------------------------------------------------------------------------
# 空响应假成功（2026-09-09 gpt-5.6-sol 实测）：换号重试兼容
# ---------------------------------------------------------------------------

_EMPTY_OK_STREAM = [("done", {}), ("token_usage", {"completion_tokens": 3})]
_GOOD_STREAM = [("output", {"response": "hello"}), ("done", {})]


def test_stream_empty_success_retries_next_account(monkeypatch):
    """账号A 空响应假成功 -> 换账号B 拿到正常内容；不标任何冷却。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def source(_url, _payload, credentials, _stop):
        calls.append(credentials.uid)
        return iter(_EMPTY_OK_STREAM if credentials.uid == "uid-primary" else _GOOD_STREAM)

    monkeypatch.setattr(pat, "_stream_profile_events", source)
    events = list(pat.stream_pat_native([], "standard-model"))
    assert ("output", {"response": "hello"}) in events
    assert calls == ["uid-primary", "uid-backup"]
    # 空响应不是额度故障：两个账号都不应进入冷却
    profiles = pat.ensure_pat_config()
    for p in profiles:
        assert pat._cooldown_until(p, "standard") <= time.time()


def test_stream_empty_success_all_accounts_raises_502(monkeypatch):
    """所有账号都空响应：显式 502（不是 401，避免误导客户端重登）。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    monkeypatch.setattr(pat, "_stream_profile_events",
                        lambda *_a: iter(_EMPTY_OK_STREAM))
    with pytest.raises(HTTPException) as caught:
        list(pat.stream_pat_native([], "standard-model"))
    assert caught.value.status_code == 502
    assert "no content" in str(caught.value.detail)


def test_stream_empty_success_budget_covers_every_account(monkeypatch):
    """≥3 账号时前几个账号各撞一次空响应假成功，预算必须覆盖全部账号——
    健康的后续账号要能被尝试，不能被全局计数提前 502 掐死。"""
    monkeypatch.setenv(
        "TRAE_PAT_BEARER_PROFILES",
        _profiles(
            {"id": "a", "bearer": "bearer-a", "priority": 0},
            {"id": "b", "bearer": "bearer-b", "priority": 1},
            {"id": "c", "bearer": "bearer-c", "priority": 2},
        ),
    )
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def source(_url, _payload, credentials, _stop):
        calls.append(credentials.uid)
        # a、b 各回一次空响应假成功；c 正常。
        return iter(_GOOD_STREAM if credentials.uid == "uid-c" else _EMPTY_OK_STREAM)

    monkeypatch.setattr(pat, "_stream_profile_events", source)
    events = list(pat.stream_pat_native([], "standard-model"))
    assert ("output", {"response": "hello"}) in events
    assert calls == ["uid-a", "uid-b", "uid-c"]


def test_stream_semantic_event_prevents_retry(monkeypatch):
    """已产出语义内容后收到空 done：绝不重放（防重复计费），正常收尾。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def source(_url, _payload, credentials, _stop):
        calls.append(credentials.uid)
        return iter(_GOOD_STREAM)

    monkeypatch.setattr(pat, "_stream_profile_events", source)
    events = list(pat.stream_pat_native([], "standard-model"))
    assert ("output", {"response": "hello"}) in events
    assert ("done", {}) in events
    assert calls == ["uid-primary"]  # 单账号完成，不重试


def test_send_native_empty_success_retries_next_account(monkeypatch):
    """非流式：账号A 返回零语义 SSE -> 换账号B 拿到正常响应。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    _EMPTY_RAW = 'event: done\ndata: {}\n\nevent: token_usage\ndata: {"completion_tokens": 1}\n\n'
    calls: list[str] = []

    def post(_url, _payload, credentials, _stream):
        calls.append(credentials.uid)
        return _EMPTY_RAW if credentials.uid == "uid-primary" else _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    assert pat.send_pat_native([], "standard-model", False) == _OK
    assert calls == ["uid-primary", "uid-backup"]


def test_send_native_error_event_is_not_empty_success(monkeypatch):
    """带 error 事件的非白名单响应不是假成功：不换号（保持旧行为）。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls: list[str] = []
    raw = 'event: error\ndata: {"code": 3003, "message": "no switch"}\n\n'

    def post(_url, _payload, credentials, _stream):
        calls.append(credentials.uid)
        return raw

    monkeypatch.setattr(pat, "_post_chat", post)
    assert pat.send_pat_native([], "standard-model", False) == raw
    assert calls == ["uid-primary"]


# ---------------------------------------------------------------------------
# 白名单额度码分级冷却：首次短冷却，反复撞码才升级到次日
# （4031 走通道级快速失败，不入本机制；此处用 4008 等账号级额度码）
# ---------------------------------------------------------------------------


def test_quota_code_first_hit_short_cooldown(monkeypatch):
    """首次撞账号级额度码（4008）：只短冷却（5分钟内恢复），不冷却到次日。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls = []

    def post(_url, _payload, credentials, _stream):
        calls.append(credentials.uid)
        return 'event: error\ndata: {"code": 4008, "message": "quota"}\n\n'

    monkeypatch.setattr(pat, "_post_chat", post)
    with pytest.raises(HTTPException):
        pat.send_pat_native([], "standard-model", False)
    profile = pat.ensure_pat_config()[0]
    left = pat._cooldown_until(profile, "standard") - time.time()
    assert left <= pat._ACCOUNT_COOLDOWN_S * 5 + 1  # 短冷却，不是到次日
    assert "standard" in json.dumps(json.load(open(os.environ["TRAE_PAT_TOKEN_FILE"]))["profiles"][profile.cache_key]["cooldowns"])


def test_quota_code_repeated_hits_escalate_to_next_day(monkeypatch):
    """同一账号 5 分钟内撞码 3 次：升级为冷却到次日。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    profile = pat.ensure_pat_config()[0]
    # 固定在日界之外，避免真实运行恰好跨过 Asia/Shanghai 午夜时测试误报。
    now = 1_700_000_000.0
    monkeypatch.setattr(pat.time, "time", lambda: now)
    for _ in range(pat._QUOTA_CODE_ESCALATE_HITS):
        pat._mark_cooldown(profile, "standard", code=4008)
    until = pat._cooldown_until(profile, "standard")
    # 到次日 = 冷却时刻就是次日零点。断言这个语义本身，而不是拍一个固定秒数：
    # 原先写 `left > 3600`，在每天 23:00 之后跑（距午夜不足 1 小时）必然误报。
    next_midnight = pat._next_day_timestamp(time.time())
    assert until == pytest.approx(next_midnight, abs=2)
    assert until > time.time()


def test_quota_code_hits_isolated_per_account_and_class(monkeypatch):
    """撞码计数按账号+额度类隔离：A 账号反复撞不影响 B 账号首次短冷却。"""
    _configure_two(monkeypatch)
    profiles = pat.ensure_pat_config()
    for _ in range(pat._QUOTA_CODE_ESCALATE_HITS):
        pat._mark_cooldown(profiles[0], "standard", code=4008)
    # B 账号首次撞码仍是短冷却
    pat._mark_cooldown(profiles[1], "standard", code=4008)
    left_b = pat._cooldown_until(profiles[1], "standard") - time.time()
    assert left_b <= pat._ACCOUNT_COOLDOWN_S * 5 + 1
    # 跨额度类也隔离
    left_a_adv = pat._cooldown_until(profiles[0], "advanced") - time.time()
    assert left_a_adv <= 0


# ---------------------------------------------------------------------------
# 通道级 4031 快速失败：全账号同窗口被拦时不再逐个探测
# ---------------------------------------------------------------------------

def test_channel_exhausted_4031_fast_fails_immediately_and_subsequent(monkeypatch):
    """4031 先跨账号 failover：所有账号都撞 4031 才升级为通道级快速失败。
    首个请求扫描全部账号（每个都短冷却）；标记通道级后，后续请求零上游调用、
    均 429 带重探提示；且绝不写次日级 standard 冷却。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def post(_url, _payload, credentials, _stream):
        calls.append(credentials.uid)
        return 'event: error\ndata: {"code": 4031, "message": ""}\n\n'

    monkeypatch.setattr(pat, "_post_chat", post)
    with pytest.raises(HTTPException) as first:
        pat.send_pat_native([], "standard-model", False)
    assert first.value.status_code == 429
    assert "4031" in str(first.value.detail)
    # 4031 逐账号 failover：两个账号都被尝试，都撞 4031
    assert calls == ["uid-primary", "uid-backup"]
    # 第二次请求：通道级标记生效，零新增上游调用
    with pytest.raises(HTTPException) as caught:
        pat.send_pat_native([], "standard-model", False)
    assert caught.value.status_code == 429
    assert "4031" in str(caught.value.detail)
    assert calls == ["uid-primary", "uid-backup"]  # 没有新增上游调用
    # 4031 只写 5 分钟短冷却，绝不升级为次日级（避免钉死账号一整天）
    now = time.time()
    for p in pat.ensure_pat_config():
        state = pat._account_state(p.cache_key)
        until = float((state.get("cooldowns") or {}).get("standard") or 0)
        assert 0 < until - now <= pat._ACCOUNT_COOLDOWN_S * 5 + 5


def test_4031_failover_uses_backup_when_primary_exhausted(monkeypatch):
    """关键修复：1 号账号 4031（日额度耗尽）时自动切到额度未耗尽的 2 号账号，
    而不是通道级快速失败——备用账号的日包相互独立。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def post(_url, _payload, credentials, _stream):
        calls.append(credentials.uid)
        if credentials.uid == "uid-primary":
            return 'event: error\ndata: {"code": 4031, "message": ""}\n\n'
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    assert pat.send_pat_native([], "standard-model", False) == _OK
    assert calls == ["uid-primary", "uid-backup"]
    # 未全部耗尽：不升级为通道级
    assert pat._channel_exhausted_until("standard") is None
    # 1 号被短冷却跳过；2 号无冷却
    now = time.time()
    profiles = pat.ensure_pat_config()
    p1 = float((pat._account_state(profiles[0].cache_key).get("cooldowns") or {}).get("standard") or 0)
    p2 = (pat._account_state(profiles[1].cache_key).get("cooldowns") or {}).get("standard")
    assert p1 > now and p2 is None


def test_channel_exhausted_4031_does_not_record_quota_hits(monkeypatch):
    """4031 不进入账号级撞码计数（_quota_code_hits），不触发次日升级路径。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)

    def post(_url, _payload, credentials, _stream):
        return 'event: error\ndata: {"code": 4031, "message": ""}\n\n'

    monkeypatch.setattr(pat, "_post_chat", post)
    with pytest.raises(HTTPException):
        pat.send_pat_native([], "standard-model", False)
    assert pat._quota_code_hits == {}


def test_channel_exhausted_ttl_expires(monkeypatch):
    """TTL 过期后恢复探测（自愈路径）。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)

    def post(_url, _payload, credentials, _stream):
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    expired = time.time() - 1
    pat._channel_exhausted["standard"] = expired
    assert pat.send_pat_native([], "standard-model", False) == _OK


def test_channel_exhausted_stream_all_4031_escalates(monkeypatch):
    """流式 4031：先跨账号 failover，全部撞 4031 才升级为通道级快速失败（429）；
    每个账号写 5 分钟短冷却，绝不升级为次日级，也不进撞码计数。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def source(_url, _payload, credentials, _stop):
        calls.append(credentials.uid)
        yield "error", {"code": 4031, "message": ""}

    monkeypatch.setattr(pat, "_stream_profile_events", source)
    with pytest.raises(HTTPException) as caught:
        list(pat.stream_pat_native([], "standard-model"))
    assert caught.value.status_code == 429
    assert "4031" in str(caught.value.detail)
    assert calls == ["uid-primary", "uid-backup"]
    assert pat._channel_exhausted_until("standard") is not None
    now = time.time()
    for p in pat.ensure_pat_config():
        state = pat._account_state(p.cache_key)
        until = float((state.get("cooldowns") or {}).get("standard") or 0)
        assert 0 < until - now <= pat._ACCOUNT_COOLDOWN_S * 5 + 5
    assert pat._quota_code_hits == {}


def test_stream_4031_failover_uses_backup_when_primary_exhausted(monkeypatch):
    """流式：1 号账号 4031 时切到 2 号成功，不升级为通道级。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def source(_url, _payload, credentials, _stop):
        calls.append(credentials.uid)
        if credentials.uid == "uid-primary":
            yield "error", {"code": 4031, "message": ""}
        else:
            yield "output", {"response": "backup"}
            yield "done", {}

    monkeypatch.setattr(pat, "_stream_profile_events", source)
    events = list(pat.stream_pat_native([], "standard-model"))
    assert calls == ["uid-primary", "uid-backup"]
    assert pat._channel_exhausted_until("standard") is None
    assert events == [("output", {"response": "backup"}), ("done", {})]


def test_channel_exhausted_only_for_4031(monkeypatch):
    """非 4031 白名单码（如 4008）不触发通道级标记。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)
    calls: list[str] = []

    def post(_url, _payload, credentials, _stream):
        calls.append(credentials.uid)
        if credentials.uid == "uid-primary":
            return 'event: error\ndata: {"code": 4008, "message": ""}\n\n'
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    assert pat.send_pat_native([], "standard-model", False) == _OK
    assert pat._channel_exhausted_until("standard") is None
    assert calls == ["uid-primary", "uid-backup"]


# ---------------------------------------------------------------------------
# 4031 extra 被动采集（standard 池唯一信号源；2026-09-10 实测为账号级分桶，
# daily/weekly 双池并存，成功流不带账单——只有撞码才回 extra）
# ---------------------------------------------------------------------------
_EXTRA_4031 = {
    "code": 4031, "message": "",
    "extra": {"used": 58, "quota": 58, "dimension": "daily",
              "next_flash": int((time.time() + 3600) * 1000)},
}


def test_4031_extra_captured_to_standard_pool(monkeypatch):
    """非流式 4031：extra（used/quota/next_flash）被动采集写入**撞码账号自己**的
    quota["standard"]（账号级分桶），fetch_pat_ent_usage 汇总带出供 UI 按账号展示。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)

    def post(_url, _payload, credentials, _stream):
        if credentials.uid == "uid-primary":
            payload = json.dumps(_EXTRA_4031)
            return f"event: error\ndata: {payload}\n\n"
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    assert pat.send_pat_native([], "standard-model", False) == _OK
    items = pat._standard_pool_items()
    assert len(items) == 1
    item = items[0]
    # 多账号时 label 带 PAT #N 前缀，UI 按账号分组
    assert item["label"] == "PAT #1 · standard daily 池"
    assert item["used"] == 58 and item["total"] == 58
    assert item["percent"] == 100
    assert item["reset_ts"] is not None and item["reset_ts"] > time.time()
    # 写在撞码账号（primary）自己名下；backup 没撞码、无记录
    profiles = pat.ensure_pat_config()
    state = pat._account_state(profiles[0].cache_key)
    assert isinstance(state.get("quota", {}).get("standard"), dict)
    state2 = pat._account_state(profiles[1].cache_key)
    assert not (state2.get("quota") or {}).get("standard")


def test_4031_extra_capture_stream(monkeypatch):
    """流式 4031：error 事件携带的 extra 同样被动采集。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)

    def source(_url, _payload, credentials, _stop):
        if credentials.uid == "uid-primary":
            yield "error", {"code": 4031, "message": "",
                            "extra": _EXTRA_4031["extra"]}
        else:
            yield "output", {"response": "backup"}
            yield "done", {}

    monkeypatch.setattr(pat, "_stream_profile_events", source)
    events = list(pat.stream_pat_native([], "standard-model"))
    assert events == [("output", {"response": "backup"}), ("done", {})]
    items = pat._standard_pool_items()
    assert len(items) == 1
    assert items[0]["used"] == 58 and items[0]["total"] == 58


def test_4031_extra_daily_and_weekly_coexist(monkeypatch):
    """daily/weekly 双池并存：同账号先撞 daily 再撞 weekly，两条记录共存；
    同维度再次撞码时覆盖更新而不是追加。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)

    def _clear_cooldowns():
        for p in pat.ensure_pat_config():
            pat._mutate_account(p.cache_key,
                                 lambda s: s.get("cooldowns", {}).clear())

    def post_daily(_url, _payload, credentials, _stream):
        if credentials.uid == "uid-primary":
            payload = json.dumps(_EXTRA_4031)
            return f"event: error\ndata: {payload}\n\n"
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post_daily)
    assert pat.send_pat_native([], "standard-model", False) == _OK
    assert len(pat._standard_pool_items()) == 1

    _clear_cooldowns()
    weekly = dict(_EXTRA_4031)
    weekly["extra"] = {"used": 160, "quota": 160, "dimension": "weekly",
                       "next_flash": int((time.time() + 86400 * 4) * 1000)}

    def post_weekly(_url, _payload, credentials, _stream):
        if credentials.uid == "uid-primary":
            return f"event: error\ndata: {json.dumps(weekly)}\n\n"
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post_weekly)
    assert pat.send_pat_native([], "standard-model", False) == _OK
    items = pat._standard_pool_items()
    labels = [i["label"] for i in items]
    assert labels == ["PAT #1 · standard daily 池", "PAT #1 · standard weekly 池"]
    by_dim = {i["label"]: i for i in items}
    assert by_dim["PAT #1 · standard weekly 池"]["total"] == 160


def test_4031_extra_missing_or_malformed_is_tolerated(monkeypatch):
    """extra 缺失/非 dict/坏 JSON 字符串：采集静默放弃，failover 不受影响。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)

    def _clear_cooldowns():
        # 4031 会给撞码账号打 5 分钟 standard 冷却；子用例间清掉，保证每个
        # 子用例都真正从 #1 起步（不是测冷却逻辑，是被测路径的前提）。
        for p in pat.ensure_pat_config():
            pat._mutate_account(p.cache_key,
                                 lambda s: s.get("cooldowns", {}).clear())

    def _clear_standard_quota():
        # 畸形数据子用例要验证「不写入」而不是「不清空旧值」：先把上一子
        # 用例的合法记录抹掉再验空。
        for p in pat.ensure_pat_config():
            def drop(s):
                (s.get("quota") or {}).pop("standard", None)
            pat._mutate_account(p.cache_key, drop)

    def post(_url, _payload, credentials, _stream):
        if credentials.uid == "uid-primary":
            # 4031 但不带 extra（历史形态，见旧测试 fixture）
            return 'event: error\ndata: {"code": 4031, "message": ""}\n\n'
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    assert pat.send_pat_native([], "standard-model", False) == _OK
    assert pat._standard_pool_items() == []

    # extra 为字符串 JSON（真实上游出现过）：应解析成功
    def post2(_url, _payload, credentials, _stream):
        if credentials.uid == "uid-primary":
            body = dict(_EXTRA_4031)
            body["extra"] = json.dumps(body["extra"])
            return f"event: error\ndata: {json.dumps(body)}\n\n"
        return _OK

    _clear_cooldowns()
    monkeypatch.setattr(pat, "_post_chat", post2)
    assert pat.send_pat_native([], "standard-model", False) == _OK
    items = pat._standard_pool_items()
    assert len(items) == 1 and items[0]["used"] == 58

    # 畸形数据：非数字 used 静默丢弃
    def post3(_url, _payload, credentials, _stream):
        if credentials.uid == "uid-primary":
            return 'event: error\ndata: {"code": 4031, "extra": {"used": "x", "quota": 58}}\n\n'
        return _OK

    _clear_cooldowns()
    _clear_standard_quota()
    monkeypatch.setattr(pat, "_post_chat", post3)
    assert pat.send_pat_native([], "standard-model", False) == _OK
    assert pat._standard_pool_items() == []


def test_4031_extra_json_error_body_captured(monkeypatch):
    """裸 JSON 错误体（非 SSE 包装）同样提取 extra。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)

    def post(_url, _payload, credentials, _stream):
        if credentials.uid == "uid-primary":
            return json.dumps(_EXTRA_4031)
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    assert pat.send_pat_native([], "standard-model", False) == _OK
    items = pat._standard_pool_items()
    assert len(items) == 1 and items[0]["total"] == 58


def test_standard_pool_expired_reset_drops_stale_usage(monkeypatch):
    """已过 reset_ts 的 standard 池不再展示陈旧用量。

    4031 只在池**已耗尽**时出现，故被动采集写进来的必然接近满额；池重置后账号
    恢复正常、成功流不带账单事件，永远没有新数据覆盖它——缓存里会长期留着一份
    「满额」快照。这里断言过期后只保留 reset_ts 与待确认标记，不再把 100% 当现状。
    """
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)

    expired = dict(_EXTRA_4031)
    expired["extra"] = {"used": 58, "quota": 58, "dimension": "daily",
                        "next_flash": int((time.time() - 60) * 1000)}  # 已过期 1 分钟

    def post(_url, _payload, credentials, _stream):
        if credentials.uid == "uid-primary":
            return f"event: error\ndata: {json.dumps(expired)}\n\n"
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    assert pat.send_pat_native([], "standard-model", False) == _OK

    # 采集本身照常落盘（快照仍要留档，只是不再当现状展示）
    profiles = pat.ensure_pat_config()
    raw = pat._account_state(profiles[0].cache_key)["quota"]["standard"]["items"][0]
    assert raw["used"] == 58 and raw["total"] == 58

    items = pat._standard_pool_items()
    assert len(items) == 1
    it = items[0]
    assert it["label"] == "PAT #1 · standard daily 池"
    assert it["reset_pending"] is True
    assert it["reset_ts"] and it["reset_ts"] < time.time()
    # 陈旧用量必须消失：否则 UI 会一直显示「已用 100%」
    for k in ("used", "total", "percent", "remaining"):
        assert k not in it, f"{k} 不应出现在已过期的条目里"


def test_standard_pool_unexpired_keeps_usage(monkeypatch):
    """未过 reset_ts 的条目照常展示用量（只有过期才降级）。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)

    def post(_url, _payload, credentials, _stream):
        if credentials.uid == "uid-primary":
            return f"event: error\ndata: {json.dumps(_EXTRA_4031)}\n\n"
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    assert pat.send_pat_native([], "standard-model", False) == _OK
    it = pat._standard_pool_items()[0]
    assert it["used"] == 58 and it["total"] == 58 and it["percent"] == 100
    assert "reset_pending" not in it


def test_standard_pool_no_reset_ts_keeps_usage(monkeypatch):
    """extra 不带 next_flash（reset_ts 为 None）：无过期判据，保持原样展示。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)

    no_reset = dict(_EXTRA_4031)
    no_reset["extra"] = {"used": 30, "quota": 58, "dimension": "daily"}

    def post(_url, _payload, credentials, _stream):
        if credentials.uid == "uid-primary":
            return f"event: error\ndata: {json.dumps(no_reset)}\n\n"
        return _OK

    monkeypatch.setattr(pat, "_post_chat", post)
    assert pat.send_pat_native([], "standard-model", False) == _OK
    it = pat._standard_pool_items()[0]
    assert it["used"] == 30 and it["reset_ts"] is None
    assert "reset_pending" not in it


def test_standard_pool_not_returned_when_never_4031(monkeypatch):
    """从未撞过 4031：fetch_pat_ent_usage 不带 standard 池条目（不能把
    「无数据」伪装成 0%——那是误导）。"""
    _configure_two(monkeypatch)
    _install_credentials(monkeypatch)

    monkeypatch.setattr(
        pat, "_post_chat", lambda _u, _p, _c, _s: _OK)
    assert pat.send_pat_native([], "standard-model", False) == _OK
    assert pat._standard_pool_items() == []


def test_clear_standard_cooldowns_idempotent(monkeypatch):
    """迁移清理：只删旧逻辑误写的次日级 standard 冷却；现行 5 分钟短冷却与
    advanced 池都保留；二次调用无副作用。"""
    _configure_two(monkeypatch)
    profiles = pat.ensure_pat_config()
    # 账号0：次日级 standard 冷却（旧误写形态）+ advanced 冷却
    future = time.time() + 20 * 3600

    def store(state):
        cds = state.setdefault("cooldowns", {})
        cds["standard"] = future
        cds["advanced"] = future

    pat._mutate_account(profiles[0].cache_key, store)
    # 账号1：现行 5 分钟短冷却（_mark_quota_exhausted 形态），不得被误清
    short = time.time() + 300

    def store_short(state):
        state.setdefault("cooldowns", {})["standard"] = short

    pat._mutate_account(profiles[1].cache_key, store_short)
    cleared = pat._clear_standard_cooldowns()
    assert cleared == 1
    state = pat._account_state(profiles[0].cache_key)
    assert "standard" not in state["cooldowns"]
    assert "advanced" in state["cooldowns"]  # 只清 standard
    # 现行短冷却保留（刚生效的正常限流不能被迁移清空）
    assert pat._account_state(profiles[1].cache_key)["cooldowns"]["standard"] == short
    # 幂等：已无可清条目，二次调用清理 0 个
    assert pat._clear_standard_cooldowns() == 0


def test_send_native_three_accounts_empty_never_returns_fake_200(monkeypatch):
    """≥3 账号全空：重试上限耗尽必须 502，不能从循环中途 return 空 SSE 假 200。"""
    monkeypatch.setenv(
        "TRAE_PAT_BEARER_PROFILES",
        _profiles(
            {"id": "primary", "bearer": "bearer-primary", "priority": 0},
            {"id": "backup", "bearer": "bearer-backup", "priority": 1},
            {"id": "third", "bearer": "bearer-third", "priority": 2},
        ),
    )
    monkeypatch.setattr(pat, "_get_profile_credentials", _fake_credentials)
    empty = 'event: done\ndata: {}\n\n'
    calls: list[str] = []

    def post(_url, _payload, credentials, _stream):
        calls.append(credentials.uid)
        return empty

    monkeypatch.setattr(pat, "_post_chat", post)
    with pytest.raises(HTTPException) as caught:
        pat.send_pat_native([], "standard-model", False)
    assert caught.value.status_code == 502
    assert "no content" in str(caught.value.detail)
    # A/B 各消耗一次重试，C 第三次空响应触发上限并直接 502。
    assert calls == ["uid-primary", "uid-backup", "uid-third"]


@pytest.mark.parametrize(
    "raw",
    [
        '{"response":"ok"}',
        '{"reasoning_content":"thinking"}',
        '{"tool_calls":[{"id":"call-1"}]}',
        '{"data":{"response":"ok"}}',
        '{"data":{"tool_calls":[{"id":"call-1"}]}}',
        '{"choices":[{"message":{"reasoning_content":"thinking"}}]}',
    ],
)
def test_bare_json_native_semantic_fields_are_not_empty(raw):
    """裸 JSON 的 Trae 原生字段也算语义内容，不应触发空响应换号。"""
    assert pat._sse_has_semantic_content(raw) is True


def test_stream_read_timeout_exceeds_semantic_timeout(monkeypatch):
    """流式 read timeout 必须晚于外层语义超时，避免 30~180s 排队首字被误报 502。"""
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def raise_for_status(self): pass
        def iter_bytes(self):
            yield b'event: output\ndata: {"response":"late but valid"}\n\n'
            yield b'event: done\ndata: {}\n\n'

    class FakeClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def stream(self, *_args, **_kwargs): return FakeResponse()

    monkeypatch.setattr(pat.chat.httpx, "Client", FakeClient)
    monkeypatch.setattr(pat.chat, "TRAE_SEMANTIC_TIMEOUT", 90)
    creds = pat.PatCredentials("token", "uid", "machine", "device")
    events = list(pat.chat._stream_profile_events(
        "https://offline.invalid/chat", b"{}", creds, __import__("threading").Event()))
    assert ("output", {"response": "late but valid"}) in events
    assert captured["timeout"].read == 95.0
    assert captured["timeout"].read > pat.chat.TRAE_SEMANTIC_TIMEOUT


# ───────────────────── 非流式总时长插断（防上游滴字拖死） ─────────────────────

def test_nonstream_read_bounded_cuts_off_dribble():
    """上游滴字（read1 永远有数据）时，总时长到点必须主动插断而非无限等。

    背景：urlopen 的 timeout 只管单次 socket 阻塞，实测挂过 17min 才 502。
    """
    from buddy_proxy.trae.pat import chat as pat_chat

    class Dribble:
        def read1(self, n):
            time.sleep(0.005)
            return b"x"  # 永远有下一个字节

    t0 = time.monotonic()
    with pytest.raises(pat_chat._NonStreamTimeout):
        pat_chat._read_all_bounded(Dribble(), max_s=0.05)
    assert time.monotonic() - t0 < 2


def test_nonstream_read_bounded_completes_normally():
    """正常响应（读到 EOF）原样返回且 utf-8 解码。"""
    from buddy_proxy.trae.pat import chat as pat_chat

    class Once:
        def __init__(self):
            self.done = False

        def read1(self, n):
            if self.done:
                return b""
            self.done = True
            return "中文ok".encode("utf-8")

    assert pat_chat._read_all_bounded(Once(), max_s=5) == "中文ok"


# ---------------------------------------------------------------------------
# 额度查询不得拖垮 /ui 额度页（2026-09-24 实测：网关 DNS 不可达时，
# 10 账号串行 × 20s 超时 + 每账号一次 ~5s 的 http.client 连接重试退避
# = 245s 白屏，而纯本地页面毫秒级正常）
# ---------------------------------------------------------------------------

def test_quota_skips_requests_when_gateway_unreachable(monkeypatch):
    """网关不可达 → 不逐账号撞超时，直接回缓存；耗时远小于一次真实请求。"""
    _configure_two(monkeypatch)
    pat._gateway_probe_cache.clear()
    monkeypatch.setattr(pat, "_gateway_reachable", lambda *a, **k: False)

    calls: list[str] = []

    def boom(*a, **k):
        calls.append("urlopen")
        raise AssertionError("网关不可达时不应发起请求")

    monkeypatch.setattr(pat.urllib.request, "urlopen", boom)

    t0 = time.monotonic()
    items = pat.fetch_pat_ent_usage()
    elapsed = time.monotonic() - t0

    assert calls == []
    assert elapsed < 2.0
    # 无缓存 + 全部失败 → 仍给出可读的说明条目，而不是空列表或 502
    assert len(items) == 1
    assert items[0]["unreachable"] is True
    assert "网络不可达" in items[0]["remaining"]


def test_quota_reports_failure_count_when_gateway_ok(monkeypatch):
    """网关可达但账号全查失败 → 说明条区分「查询失败」（非 unreachable）。"""
    _configure_two(monkeypatch)
    pat._gateway_probe_cache.clear()
    monkeypatch.setattr(pat, "_gateway_reachable", lambda *a, **k: True)
    _install_credentials(monkeypatch)

    def boom(*a, **k):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(pat.urllib.request, "urlopen", boom)
    items = pat.fetch_pat_ent_usage()
    assert items[0]["query_failed"] is True
    assert items[0]["unreachable"] is False
    assert "2/2" in items[0]["remaining"]


def test_quota_uses_per_account_cache_when_gateway_unreachable(monkeypatch):
    """网关不可达时各账号缓存照常展示（带 ·缓存 标记），并保留原账号顺序。"""
    _configure_two(monkeypatch)
    pat._gateway_probe_cache.clear()
    monkeypatch.setattr(pat, "_gateway_reachable", lambda *a, **k: False)
    profiles = pat.ensure_pat_config()
    for i, profile in enumerate(profiles):
        def store(state, _i=i):
            state.setdefault("quota", {})["advanced"] = {
                "items": [{"label": f"PAT #{_i + 1} · Orange 包", "used": 1.0,
                           "total": 100.0, "remaining": 99.0, "percent": 1,
                           "reset_ts": None}],
                "fetched_at": time.time(),
            }
        pat._mutate_account(profile.cache_key, store)

    items = pat.fetch_pat_ent_usage()
    labels = [it["label"] for it in items]
    assert labels[0].startswith("PAT 额度网关不可达")
    assert "PAT #1 · Orange 包·缓存" in labels
    assert "PAT #2 · Orange 包·缓存" in labels


def test_quota_probe_result_is_cached_between_calls(monkeypatch):
    """探测结论短 TTL 缓存：连续两轮刷新只探测一次，避免轮询反复探网。"""
    _configure_two(monkeypatch)
    pat._gateway_probe_cache.clear()
    probes: list[str] = []

    def probe(plus, *a, **k):
        probes.append(plus)
        return True

    monkeypatch.setattr(pat, "_gateway_reachable", probe)
    _install_credentials(monkeypatch)
    monkeypatch.setattr(pat.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("x")))

    pat.fetch_pat_ent_usage()
    pat.fetch_pat_ent_usage()
    assert len(probes) == 1


def test_quota_accounts_queried_concurrently(monkeypatch):
    """账号并发查询：6 账号各自阻塞 0.4s，总耗时远小于串行 2.4s。"""
    monkeypatch.setenv(
        "TRAE_PAT_BEARER_PROFILES",
        _profiles(*[{"id": f"a{i}", "bearer": f"b{i}", "priority": i} for i in range(6)]),
    )
    pat._gateway_probe_cache.clear()
    monkeypatch.setattr(pat, "_gateway_reachable", lambda *a, **k: True)
    _install_credentials(monkeypatch)

    payload = json.dumps({
        "user_entitlement_pack_list": [
            {"entitlement_base_info": {"entitlement_id": "free_weekly_x",
                                       "quota": {"basic_usage_limit": 300}},
             "usage": {"basic_usage_amount": 1}},
        ]
    }).encode()

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            time.sleep(0.4)
            return payload

    monkeypatch.setattr(pat.urllib.request, "urlopen", lambda *a, **k: Resp())

    t0 = time.monotonic()
    items = pat.fetch_pat_ent_usage()
    elapsed = time.monotonic() - t0

    assert len(items) == 6
    assert elapsed < 1.6, f"疑似串行执行：{elapsed:.2f}s"
    # 顺序稳定：结果按 profiles 顺序拼接
    assert [it["label"] for it in items] == [f"PAT #{i + 1} · Orange 包" for i in range(6)]


def test_quota_single_request_timeout_is_short(monkeypatch):
    """单账号请求超时必须短：展示用途不该等 20s（额度页白屏的主因之一）。"""
    assert pat.quota._QUOTA_TIMEOUT_S <= 8.0


def test_gateway_probe_respects_total_budget(monkeypatch):
    """探测是「总预算」而非每地址超时：多地址各自跑满不能把探测变成新的慢点。"""
    import socket as _socket

    from buddy_proxy.trae.pat import quota as pat_quota

    # 三个地址，每个 connect 都挂满超时窗口
    monkeypatch.setattr(
        pat_quota, "_resolve",
        lambda host, port: [(2, 1, 6, "", ("10.0.0.1", 443)),
                            (2, 1, 6, "", ("10.0.0.2", 443)),
                            (2, 1, 6, "", ("10.0.0.3", 443))],
    )

    def slow_connect(addr, timeout=None):
        time.sleep(timeout or 0)
        raise OSError("unreachable")

    monkeypatch.setattr(_socket, "create_connection", slow_connect)
    t0 = time.monotonic()
    assert pat_quota._gateway_reachable("https://gw.invalid", budget=0.3) is False
    elapsed = time.monotonic() - t0
    # 总预算 0.3s：三个地址即便各跑满也只能用掉约 0.3s，不会到 0.9s
    assert elapsed < 0.6, f"探测未按总预算收口：{elapsed:.2f}s"


# ---------------------------------------------------------------------------
# 失败结果短缓存：网络抖动（切 WiFi / VPN 重连）几秒恢复后，下一轮就该自愈，
# 不能把一条不准确的「网关不可达」警告挂满 5 分钟
# ---------------------------------------------------------------------------

def test_failure_result_uses_short_ttl(monkeypatch):
    """失败结果只用 FAILURE_TTL_S；成功结果仍用 SNAPSHOT_TTL_S。"""
    import asyncio

    from buddy_proxy.benefits import (
        SNAPSHOT_TTL_S, FAILURE_TTL_S, BenefitsManager, _is_failure,
    )

    assert FAILURE_TTL_S < SNAPSHOT_TTL_S

    class _State:
        providers: dict = {}

    mgr = BenefitsManager.__new__(BenefitsManager)
    mgr.history = None
    mgr._state = _State()
    mgr._task = None
    mgr._last_attempt = {}
    mgr._startup_seen = set()
    mgr._cache = {}
    mgr._refreshing = {}

    calls: list[int] = []

    def boom():
        calls.append(1)
        raise RuntimeError("offline")

    # 第一次失败 → 进缓存
    assert asyncio.run(mgr._cached("k", boom))["error"].startswith("offline")
    assert len(calls) == 1
    # FAILURE_TTL_S 内命中缓存，不再调用
    assert asyncio.run(mgr._cached("k", boom))["error"]
    assert len(calls) == 1
    # 把缓存时间往前拨到超过 FAILURE_TTL_S、但没到 SNAPSHOT_TTL_S → 必须重试
    ts, data = mgr._cache["k"]
    mgr._cache["k"] = (ts - (FAILURE_TTL_S + 1), data)
    assert asyncio.run(mgr._cached("k", boom))["error"]
    assert len(calls) == 2, "失败结果没走短 TTL，网络恢复后要等 5 分钟才自愈"

    # 成功结果：过了 FAILURE_TTL_S 仍应命中缓存（用满 SNAPSHOT_TTL_S）
    ok_calls: list[int] = []

    def good():
        ok_calls.append(1)
        return {"items": [{"label": "PAT #1", "used": 1, "total": 100}]}

    assert asyncio.run(mgr._cached("ok", good))["items"]
    assert len(ok_calls) == 1
    ts, data = mgr._cache["ok"]
    mgr._cache["ok"] = (ts - (FAILURE_TTL_S + 1), data)
    assert asyncio.run(mgr._cached("ok", good))["items"]
    assert len(ok_calls) == 1, "成功结果不该被短 TTL 影响"
    assert _is_failure(data) is False


def test_partial_quota_failure_marks_as_failure():
    """网关不可达时 advanced 返回「说明条 + 各账号缓存」——这种部分失败也要
    被认成失败，否则会按成功缓 5 分钟。"""
    from buddy_proxy.benefits import _is_failure

    real = {"items": [{"query_failed": True, "unreachable": True, "label": "PAT 额度网关不可达"},
                      {"label": "PAT #1 · Orange 包·缓存", "used": 1, "total": 300}]}
    assert _is_failure(real) is True
    healthy = {"items": [{"label": "PAT #1 · Orange 包", "used": 1, "total": 300}]}
    assert _is_failure(healthy) is False


def test_quota_round_deadline_caps_latency(monkeypatch):
    """整轮上限：网关可达但请求全挂时，不能让页面等满 ceil(N/w)×timeout。"""
    from buddy_proxy.trae.pat import quota as pat_quota

    monkeypatch.setenv(
        "TRAE_PAT_BEARER_PROFILES",
        _profiles(*[{"id": f"a{i}", "bearer": f"b{i}", "priority": i} for i in range(10)]),
    )
    pat_quota._gateway_probe_cache.clear()
    monkeypatch.setattr(pat, "_gateway_reachable", lambda *a, **k: True)
    _install_credentials(monkeypatch)
    monkeypatch.setattr(pat_quota, "_QUOTA_TIMEOUT_S", 30.0)
    monkeypatch.setattr(pat_quota, "_QUOTA_ROUND_DEADLINE_S", 0.4)

    def hang(*a, **k):
        time.sleep(10)
        raise urllib.error.URLError("hang")

    monkeypatch.setattr(pat_quota.urllib.request, "urlopen", hang)

    t0 = time.monotonic()
    items = pat.fetch_pat_ent_usage()
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0, f"整轮上限没生效：{elapsed:.2f}s（应受 deadline 约束）"
    assert items[0]["query_failed"] is True


def test_quota_pool_is_reused_not_recreated(monkeypatch):
    """常驻线程池：连续慢轮不得累积线程（每轮新建 + wait=False 会线性涨）。"""
    from buddy_proxy.trae.pat import quota as pat_quota

    monkeypatch.setenv(
        "TRAE_PAT_BEARER_PROFILES",
        _profiles(*[{"id": f"a{i}", "bearer": f"b{i}", "priority": i} for i in range(10)]),
    )
    pat_quota._gateway_probe_cache.clear()
    monkeypatch.setattr(pat, "_gateway_reachable", lambda *a, **k: True)
    _install_credentials(monkeypatch)
    monkeypatch.setattr(pat_quota, "_QUOTA_TIMEOUT_S", 30.0)
    monkeypatch.setattr(pat_quota, "_QUOTA_ROUND_DEADLINE_S", 0.3)
    monkeypatch.setattr(pat_quota, "_quota_pool_obj", None)

    def hang(*a, **k):
        time.sleep(5)
        raise urllib.error.URLError("hang")

    monkeypatch.setattr(pat_quota.urllib.request, "urlopen", hang)

    before = threading.active_count()
    for _ in range(3):
        pat.fetch_pat_ent_usage()
    after = threading.active_count()

    # 每轮新建会留 6 个线程/轮 → 3 轮涨 18 个；常驻池只多一次固定开销
    assert after - before <= 8, f"线程在累积：{before} → {after}"
    assert pat_quota._quota_pool() is pat_quota._quota_pool()
