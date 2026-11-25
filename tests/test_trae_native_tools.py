"""Trae 原生 function calling 通道测试（完全离线，不访问 Trae 上游）。

覆盖 buddy_proxy.trae.native_tools 的完整链路：
- 非流式 / 流式：原生 tool_calls SSE → OpenAI chat.completion（含真实 usage）
- 多轮结构化历史回放：assistant.tool_calls / role:tool 原样上报（不改写为文本）
- 4001 参数拒绝 → 流式先降级 native 非流式重试，再回落文本协议（send_trae_chat），换号……换通道不影响结果
- 非 4001 上游错误 → 不回落，原样上报
- 工具载荷形状：parameters 序列化为 JSON 字符串（上游 Go schema 要求）

运行：
    PYTHONPATH=src python3 -m pytest tests/test_trae_native_tools.py -v
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
from buddy_proxy.trae.credentials import save_account_cred
from buddy_proxy.trae.provider import TraeProvider


@pytest.fixture(autouse=True)
def _clear_trae_cooldowns():
    """额度类上游错误（4031/4008…）会落 5 分钟冷却；不清理会污染同进程后续用例。"""
    from buddy_proxy.trae import failover
    from buddy_proxy.trae import provider as trae_provider

    failover._cooldowns.clear()
    trae_provider._stream_reject_until.clear()
    trae_provider._last_reject_at.clear()
    yield
    failover._cooldowns.clear()
    trae_provider._stream_reject_until.clear()
    trae_provider._last_reject_at.clear()


# ---------------------------------------------------------------------------
# 上游 SSE fixture：原生通道响应形态（event:output 携带结构化 tool_calls）
# ---------------------------------------------------------------------------
def _native_sse(outputs: list[dict], usage: bool = True) -> str:
    parts = []
    for o in outputs:
        parts.append("event: output")
        parts.append("data: " + json.dumps(o, ensure_ascii=False))
        parts.append("")
    if usage:
        parts += [
            "event: token_usage",
            'data: {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}',
            "",
        ]
    parts += ["event: done", "data: {}", "", ""]
    return "\n".join(parts)


SSE_NATIVE_TOOL_CALL = _native_sse([
    {"reasoning_content": "我来查一下天气"},
    {"response": "", "tool_calls": [
        {"index": 0, "id": "call_native_01", "type": "function",
         "function_call": {"name": "get_weather", "arguments": "{\"city\": \"北京\"}",
                           "partial_arguments": None, "namespace": None}},
    ]},
])

SSE_NATIVE_REJECTED = (
    'event: error\ndata: {"code": 4001, "message": "param is invalid"}\n\n'
    "event: done\ndata: {}\n\n"
)

SSE_NATIVE_ERROR_4031 = (
    'event: error\ndata: {"code": 4031, "message": ""}\n\n'
    "event: done\ndata: {}\n\n"
)

SSE_LEGACY_TOOL_CALL = (
    'event: output\ndata: {"response": "<tool_call>\\n{\\"name\\": \\"get_weather\\", '
    '\\"arguments\\": {\\"city\\": \\"北京\\"}}\\n</tool_call>"}\n\n'
    "event: done\ndata: {}\n\n"
)


def _chat_body(stream: bool = False, tools: bool = True, multi_turn: bool = False) -> dict:
    messages = [
        {"role": "user", "content": "北京天气如何？"},
    ]
    if multi_turn:
        messages = [
            {"role": "user", "content": "北京天气如何？"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_hist_01", "type": "function",
                 "function": {"name": "get_weather", "arguments": "{\"city\": \"北京\"}"}}]},
            {"role": "tool", "tool_call_id": "call_hist_01", "name": "get_weather",
             "content": "{\"temp_c\": 25, \"condition\": \"sunny\"}"},
            {"role": "user", "content": "总结一下"},
        ]
    body: dict = {"model": "glm-5.3-flash", "stream": stream, "messages": messages}
    if stream:
        body["stream_options"] = {"include_usage": True}
    if tools:
        body["tools"] = [{
            "type": "function",
            "function": {"name": "get_weather", "description": "查询城市天气",
                         "parameters": {"type": "object",
                                        "properties": {"city": {"type": "string"}},
                                        "required": ["city"]}}},
        ]
    return body


# ---------------------------------------------------------------------------
# fixture：假 ProxyState + mock 原生/legacy 上游
# ---------------------------------------------------------------------------
@pytest.fixture()
def native_env(monkeypatch):
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

    native_calls: list[dict] = []
    legacy_calls: list[dict] = []

    def fake_native(native_msgs, model, stream, tools, **kwargs):
        idx = len(native_calls)
        native_calls.append({"messages": native_msgs, "model": model,
                             "stream": stream, "tools": tools,
                             "max_mode": kwargs.get("max_mode", False)})
        if getattr(state, "_native_fail_first", 0) > idx:
            return SSE_NATIVE_REJECTED
        if stream and getattr(state, "_native_reject_stream", False):
            return SSE_NATIVE_REJECTED
        if getattr(state, "_native_sse", None) is not None:
            return state._native_sse
        return SSE_NATIVE_TOOL_CALL

    def fake_legacy(messages, model, stream, base_url=None, **kwargs):
        legacy_calls.append({"messages": messages, "model": model,
                             "stream": stream,
                             "max_mode": kwargs.get("max_mode", False)})
        if stream and getattr(state, "_legacy_reject_streaming", False):
            return SSE_NATIVE_REJECTED
        return getattr(state, "_legacy_sse", None) or SSE_LEGACY_TOOL_CALL

    # 4001 兜底链节流时会 time.sleep 等到惩罚窗外：记录调用并跳过真实等待
    sleeps: list[float] = []
    state._sleeps = sleeps
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    monkeypatch.setattr(tp_impl, "_send_native_chat", fake_native)
    monkeypatch.setattr(tp_impl, "send_trae_chat", fake_legacy)

    # 增量事件源：默认返回 None，让存量用例继续覆盖「整段兼容路径」
    # （非流式缓冲 → 4001 节流重试 → 文本协议）。需要测真流式的用例把
    # state._incremental_events 设成事件列表，或设 _incremental_reject=True
    # 模拟零产出 4001。
    def fake_stream_events(native_msgs, model, tools, stop=None, **kwargs):
        if getattr(state, "_incremental_events", None) is None and not getattr(
                state, "_incremental_reject", False):
            return None
        native_calls.append({"messages": native_msgs, "model": model,
                             "stream": "incremental", "tools": tools})

        def gen():
            if getattr(state, "_incremental_reject", False):
                from buddy_proxy.trae.native_tools import _NativeStreamRejected
                raise _NativeStreamRejected('{"code":4001,"message":"param is invalid"}')
            for ev in state._incremental_events:
                if getattr(state, "_incremental_fail_after", None) is not None:
                    if state._incremental_yielded >= state._incremental_fail_after:
                        raise RuntimeError("socket broke mid-flight")
                    state._incremental_yielded += 1
                yield ev
        return gen()

    state._incremental_yielded = 0
    monkeypatch.setattr(tp_impl, "_stream_native_chat_events", fake_stream_events)
    monkeypatch.setattr(tp_impl, "_auth", lambda: ("fake-token", "fake-uid"))

    # 注入一个假 work 账号（conftest 已隔离 trae state 目录，这里落假 cred）。
    # 多账号 failover 后，forward 走 failover.available_accounts() → list_accounts()
    # 取当前账号 cred（不再读 _auth 单账号缓存）；没有账号会 429（通道耗尽）。
    save_account_cred({
        "uid": "fake-uid-1", "nickname": "Fake",
        "access_token": "fake-token", "refresh_token": "fake-rt",
        "expires_at": int(time.time()) + 3600,
        "machine_id": "m", "device_id": "d", "api_host": "h",
        "enterprise_id": "",
    })
    return state, native_calls, legacy_calls


@pytest.fixture()
def client(native_env):
    return TestClient(m.app)


def _parse_sse_events(text: str) -> list[tuple[str, dict | str]]:
    events, cur_event, cur_data = [], "", []

    def flush():
        if cur_data:
            data_str = "\n".join(cur_data)
            events.append((cur_event, "[DONE]" if data_str == "[DONE]" else json.loads(data_str)))
            cur_data.clear()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            flush(); cur_event = ""
        elif line.startswith("event:"):
            cur_event = line[6:].strip()
        elif line.startswith("data:"):
            cur_data.append(line[5:].strip())
    flush()
    return events


# ---------------------------------------------------------------------------
# 非流式 / 流式
# ---------------------------------------------------------------------------
def test_native_nonstream_tool_calls(client, native_env):
    state, native_calls, _ = native_env
    r = client.post("/v1/chat/completions", json=_chat_body(stream=False))
    assert r.status_code == 200
    resp = r.json()
    ch = resp["choices"][0]
    assert ch["finish_reason"] == "tool_calls"
    tc = ch["message"]["tool_calls"]
    assert len(tc) == 1
    assert tc[0]["id"] == "call_native_01"
    assert tc[0]["function"]["name"] == "get_weather"
    assert json.loads(tc[0]["function"]["arguments"]) == {"city": "北京"}
    # 原生通道带真实 usage
    assert resp["usage"]["prompt_tokens"] == 10
    assert len(native_calls) == 1


def test_native_stream_tool_calls(client, native_env):
    state, native_calls, _ = native_env
    r = client.post("/v1/chat/completions", json=_chat_body(stream=True))
    assert r.status_code == 200
    events = _parse_sse_events(r.text)
    assert events[-1] == ("", "[DONE]")
    tool_deltas = []
    finish = None
    usage = None
    for ev, data in events:
        if not isinstance(data, dict):
            continue
        if "usage" in data:
            usage = data["usage"]
        for ch in data.get("choices", []):
            if (ch.get("delta") or {}).get("tool_calls"):
                tool_deltas.extend(ch["delta"]["tool_calls"])
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    assert finish == "tool_calls"
    assert len(tool_deltas) == 1
    assert tool_deltas[0]["index"] == 0
    assert tool_deltas[0]["function"]["name"] == "get_weather"
    assert usage is not None and usage["total_tokens"] == 30


# ---------------------------------------------------------------------------
# 多轮结构化历史回放：不改写为文本协议
# ---------------------------------------------------------------------------
def test_native_history_replay_structured(client, native_env):
    state, native_calls, _ = native_env
    r = client.post("/v1/chat/completions", json=_chat_body(stream=False, multi_turn=True))
    assert r.status_code == 200
    sent = native_calls[0]["messages"]
    # assistant.tool_calls 保持结构化 function_call 形状
    asst = [m for m in sent if m.get("role") == "assistant"]
    assert asst and asst[0]["tool_calls"][0]["function_call"]["name"] == "get_weather"
    assert asst[0]["tool_calls"][0]["id"] == "call_hist_01"
    # role:tool 保持 tool 角色 + tool_call_id，不转 user + [tool_result] 前缀
    tool = [m for m in sent if m.get("role") == "tool"]
    assert tool and tool[0]["tool_call_id"] == "call_hist_01"
    assert tool[0]["content"][0]["text"] == "{\"temp_c\": 25, \"condition\": \"sunny\"}"
    # 不注入工具教学/压制指令
    sys_msgs = [m for m in sent if m.get("role") == "system"]
    assert not sys_msgs or "tool_call" not in json.dumps(sys_msgs, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 工具载荷形状：parameters 序列化为 JSON 字符串
# ---------------------------------------------------------------------------
def test_native_tools_payload_string_params(client, native_env):
    """参数 stringify 发生在 _build_native_body 内部，直接单测该转换。"""
    from buddy_proxy.trae.native_tools import _native_tools_payload

    state, native_calls, _ = native_env
    client.post("/v1/chat/completions", json=_chat_body(stream=False))
    # 请求链路上 tools 原样传入 native 发送器
    assert len(native_calls[0]["tools"]) == 1
    payload = _native_tools_payload(_chat_body()["tools"])
    fn = payload[0]["function"]
    assert fn["name"] == "get_weather"
    assert isinstance(fn["parameters"], str)
    assert json.loads(fn["parameters"])["required"] == ["city"]


# ---------------------------------------------------------------------------
# 4001 → 自动回落文本协议
# ---------------------------------------------------------------------------
def _assert_paced(native_env, n: int) -> None:
    """断言发生 n 次节流等待：每次安静睡到 15s 窗外（略小于 15.0，扣掉了耗时）。"""
    state = native_env[0]
    quiet = tp_impl._REJECT_QUIET_S
    assert len(state._sleeps) == n
    assert all(quiet - 1.0 < s <= quiet for s in state._sleeps), state._sleeps


def test_native_4001_falls_back_to_legacy(client, native_env):
    state, native_calls, legacy_calls = native_env
    state._native_sse = SSE_NATIVE_REJECTED
    r = client.post("/v1/chat/completions", json=_chat_body(stream=False))
    assert r.status_code == 200
    resp = r.json()
    ch = resp["choices"][0]
    # 文本协议路径解析出同样的调用
    assert ch["finish_reason"] == "tool_calls"
    assert ch["message"]["tool_calls"][0]["function"]["name"] == "get_weather"
    # 非流式先节流重试一次，两次都拒才落文本协议
    assert len(native_calls) == 2 and len(legacy_calls) == 1
    _assert_paced(native_env, 2)


def test_native_4001_stream_retries_native_nonstream(client, native_env):
    """上游整体拒绝 chat_v3 流式（2026-10-07 实测）：先降级 native 非流式重试，
    不动文本协议，客户端仍拿到 SSE tool_calls。"""
    state, native_calls, legacy_calls = native_env
    state._native_reject_stream = True
    r = client.post("/v1/chat/completions", json=_chat_body(stream=True))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse_events(r.text)
    finish = [d.get("choices", [{}])[0].get("finish_reason")
              for ev, d in events if isinstance(d, dict) and d.get("choices")]
    assert "tool_calls" in finish
    # 两次 native 调用：流式被拒 → 非流式成功；文本协议未动用
    assert [c["stream"] for c in native_calls] == [True, False]
    assert legacy_calls == []
    # 重试前先节流（上游对刚失败的账号有快速重试惩罚）
    _assert_paced(native_env, 1)


def test_native_4001_stream_skip_ttl_second_request(client, native_env):
    """流式被拒后 TTL 内的后续流式请求直接跳过必败尝试，straight to 非流式。"""
    state, native_calls, legacy_calls = native_env
    state._native_reject_stream = True
    r1 = client.post("/v1/chat/completions", json=_chat_body(stream=True))
    assert r1.status_code == 200
    # 第一次：流式被拒 → 非流式重试成功（上一用例已断言细节）
    assert [c["stream"] for c in native_calls] == [True, False]
    native_calls.clear()
    state._sleeps.clear()

    r2 = client.post("/v1/chat/completions", json=_chat_body(stream=True))
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse_events(r2.text)
    finish = [d.get("choices", [{}])[0].get("finish_reason")
              for ev, d in events if isinstance(d, dict) and d.get("choices")]
    assert "tool_calls" in finish
    # 第二次：跳过流式尝试，仅一次非流式 native 调用；r1 的非流式成功已清掉
    # 节流时间戳（门是开的），所以不再陪等安静窗，无文本协议
    assert [c["stream"] for c in native_calls] == [False]
    assert legacy_calls == []
    assert state._sleeps == []


def test_native_4001_stream_full_fallback_to_legacy(client, native_env):
    """native 非流式重试也被拒（如 GPT-6 系连 solo_work_lite 一并 4001 的上游）：
    落文本协议兜底，仍用非流式上游请求，客户端照常收 SSE。"""
    state, native_calls, legacy_calls = native_env
    state._native_sse = SSE_NATIVE_REJECTED
    state._legacy_reject_streaming = True
    r = client.post("/v1/chat/completions", json=_chat_body(stream=True))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse_events(r.text)
    finish = [d.get("choices", [{}])[0].get("finish_reason")
              for ev, d in events if isinstance(d, dict) and d.get("choices")]
    assert "tool_calls" in finish
    assert [c["stream"] for c in native_calls] == [True, False, False]
    assert len(legacy_calls) == 1 and legacy_calls[0]["stream"] is False
    # 三级兜底各节流一次：流式→非流式→安静重试→文本
    _assert_paced(native_env, 3)


def test_native_4001_nonstream_quiet_retry_recovers(client, native_env):
    """惩罚窗内的非流式拒绝：安静退避后原路重试成功，不落文本协议。"""
    state, native_calls, legacy_calls = native_env
    state._native_reject_stream = True   # 流式恒拒（上游门控）
    state._native_fail_first = 2         # 前两次 native（含首次非流式）落在惩罚窗内
    r = client.post("/v1/chat/completions", json=_chat_body(stream=True))
    assert r.status_code == 200
    events = _parse_sse_events(r.text)
    finish = [d.get("choices", [{}])[0].get("finish_reason")
              for ev, d in events if isinstance(d, dict) and d.get("choices")]
    assert "tool_calls" in finish
    # 流式被拒 → 非流式(窗内被拒) → 节流后原路重试成功；文本协议未动用
    assert [c["stream"] for c in native_calls] == [True, False, False]
    assert legacy_calls == []
    _assert_paced(native_env, 2)


# ---------------------------------------------------------------------------
# 非 4001 上游错误 → 不回落，原样上报
# ---------------------------------------------------------------------------
def test_native_non_4001_error_no_fallback(client, native_env):
    state, native_calls, legacy_calls = native_env
    state._native_sse = SSE_NATIVE_ERROR_4031
    r = client.post("/v1/chat/completions", json=_chat_body(stream=False))
    # 4031 是账号级额度码（2026-10-06 起 → 429 进 failover 分类），不再一律 502
    assert r.status_code == 429
    assert "4031" in r.json().get("detail", "") or "4031" in r.text
    assert legacy_calls == []


# ---------------------------------------------------------------------------
# 原生通道瞬态重试（对齐 Work 通道语义）
# ---------------------------------------------------------------------------
def test_native_transient_retry(monkeypatch):
    """上游瞬态断流（IncompleteRead）→ 同端点退避重试，第二次成功。"""
    import http.client
    from buddy_proxy.trae import native_tools as nt

    attempts = {"n": 0}

    class _Resp:
        def __init__(self):
            self._read = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read1(self, n):
            # 生产路径走 read1（总时长插断需要分块读）；EOF 返回 b""
            if self._read:
                return b""
            self._read = True
            return b"event: done\ndata: {}\n\n"

    def flaky_urlopen(req, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise http.client.IncompleteRead(b"partial")
        return _Resp()

    monkeypatch.setattr(nt, "_load_work_cred",
                        lambda: {"access_token": "fake", "uid": "u"})
    monkeypatch.setattr("urllib.request.urlopen", flaky_urlopen)
    monkeypatch.setattr(nt.time, "sleep", lambda sec: None)

    raw = nt._send_native_chat(
        [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "glm-5.3", False,
        [{"type": "function", "function": {"name": "f", "description": "",
                                           "parameters": "{}"}}])
    assert "event: done" in raw
    assert attempts["n"] == 2


def test_native_shim_getattr_compat():
    """历史任意名字经 shim __getattr__ 从子包解析（旧代码不断链）。"""
    import buddy_proxy.trae_provider as tp_shim
    from buddy_proxy.trae import leak_guard

    assert tp_shim._WORK_CHAT_MAX_ATTEMPTS == 3
    # 历史私有常量/辅助函数经 __getattr__ 从子包解析（与旧单体同名同值）
    assert tp_shim._LEAK_TAG_NAMES == leak_guard._LEAK_TAG_NAMES
    assert callable(tp_shim._gate_tool_name)
    with pytest.raises(AttributeError):
        tp_shim._definitely_not_a_name


# ---------------------------------------------------------------------------
# 纯聊天（无 tools）也走原生通道：无 guard 注入、无 tools 键
# ---------------------------------------------------------------------------
def test_native_pure_chat_no_guard(client, native_env):
    state, native_calls, legacy_calls = native_env
    state._native_sse = _native_sse([{"response": "你好！"}])
    r = client.post("/v1/chat/completions", json=_chat_body(stream=False, tools=False))
    assert r.status_code == 200
    resp = r.json()
    assert resp["choices"][0]["finish_reason"] == "stop"
    assert "你好" in resp["choices"][0]["message"]["content"]
    assert len(native_calls) == 1 and legacy_calls == []
    # chat_v3 无 agent 预设：不注入 guard 压制指令
    assert all("Command" not in json.dumps(msg, ensure_ascii=False)
               for msg in native_calls[0]["messages"])
    # 纯聊天不带 tools 键
    assert native_calls[0]["tools"] == []


def test_native_pure_chat_4001_falls_back_with_guard(client, native_env):
    """纯聊天 4001 回落文本协议时，legacy prompt 需带 guard（防预设泄漏）。"""
    state, native_calls, legacy_calls = native_env
    state._native_sse = SSE_NATIVE_REJECTED
    state._legacy_sse = (
        'event: output\ndata: {"response": "你好！"}\n\n'
        "event: done\ndata: {}\n\n")
    r = client.post("/v1/chat/completions", json=_chat_body(stream=False, tools=False))
    assert r.status_code == 200
    assert "你好" in r.json()["choices"][0]["message"]["content"]
    # 非流式先安静重试一次，两次都拒才落文本协议
    assert len(native_calls) == 2 and len(legacy_calls) == 1
    # 回落路径注入 guard 压制指令（solo_work_lite 服务端预设会漏 Command 语法）
    assert any("Command" in json.dumps(msg, ensure_ascii=False)
               for msg in legacy_calls[0]["messages"])


# ───────────────────── 非流式总时长插断（与 PAT 通道同款防护） ─────────────────────

def test_native_nonstream_read_bounded_cuts_off_dribble():
    import time as _time

    from buddy_proxy.trae import native_tools as nt

    class Dribble:
        def read1(self, n):
            return b"x"  # 永远有数据 → 无 deadline 就会无限读

    with pytest.raises(nt._NonStreamTimeout):
        nt._read_all_bounded(Dribble(), max_s=0.05)
    assert _time.monotonic() > 0  # 占位：确保异常路径先于断言失败


# ───────────── 个人通道真流式（增量事件源，2026-11-23） ─────────────
#
# 背景：`_send_native_chat` 用 `_read_all_bounded` 把整个响应读完才返回，
# provider 再切块「重放」。上游本身是真流式，整段缓冲让客户端 TTFT 等于
# 总耗时（metrics 里 TTFT/总耗时 中位数 trae 0.998 vs codebuddy 0.308）。
# 下面三条锁住「增量路径真的被走」「4001 零产出能安全回落」「已出字后断连
# 不重放」。

def test_incremental_stream_is_preferred_and_streams(client, native_env):
    """有增量事件源时走它，且事件按到达顺序增量下发（不是整段重放）。"""
    state, native_calls, legacy_calls = native_env
    state._incremental_events = [
        ("output", {"reasoning_content": "想一下"}),
        ("output", {"response": "你"}),
        ("output", {"response": "好"}),
        ("token_usage", {"prompt_tokens": 7, "completion_tokens": 2,
                         "total_tokens": 9}),
        ("done", {}),
    ]
    r = client.post("/v1/chat/completions", json=_chat_body(stream=True, tools=False))
    assert r.status_code == 200
    assert "你" in r.text and "好" in r.text
    # 走了增量源：native_calls 里那条的 stream 标记为 "incremental"
    assert any(c["stream"] == "incremental" for c in native_calls), \
        "流式请求没有走增量事件源（退回了整段缓冲路径）"
    # 增量成功就不该再走整段 native 或文本兜底
    assert not any(c["stream"] is True or c["stream"] is False for c in native_calls)
    assert legacy_calls == []


def test_incremental_4001_zero_output_falls_back_to_buffered(client, native_env):
    """增量路径零产出就 4001：安全回落到整段路径，客户端仍拿到内容。

    这是「可回落」的边界条件——一个字节都没发出去，重放不会造成重复内容。
    """
    state, native_calls, legacy_calls = native_env
    state._incremental_reject = True      # 增量一上来就 4001
    state._native_sse = _native_sse([{"response": "兜底成功"}])
    r = client.post("/v1/chat/completions", json=_chat_body(stream=True, tools=False))
    assert r.status_code == 200
    assert "兜底成功" in r.text
    # 回落路径确实被走了（增量失败后有一次整段 native 调用）
    assert any(c["stream"] is True or c["stream"] is False for c in native_calls)
    # 且被记进惩罚窗：TTL 内后续请求不再尝试流式
    from buddy_proxy.trae import provider as trae_provider
    assert trae_provider._stream_rejected_recently("glm-5.3-flash")


def test_incremental_mid_flight_break_does_not_replay(client, native_env):
    """已出字后断连：只报错、不重放，避免客户端看到重复内容。"""
    state, native_calls, legacy_calls = native_env
    state._incremental_events = [
        ("output", {"response": "前半段"}),
        ("output", {"response": "后半段"}),
        ("done", {}),
    ]
    state._incremental_fail_after = 1     # 吐完第一条就断
    r = client.post("/v1/chat/completions", json=_chat_body(stream=True, tools=False))
    # 已经出过字，不能再回落到整段路径重放
    assert not any(c["stream"] is True or c["stream"] is False for c in native_calls), \
        "已出字后仍回落重放，客户端会看到重复内容"
    assert legacy_calls == []
