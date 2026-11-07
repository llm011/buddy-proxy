"""codebuddy 多账号 failover 测试：冷却 / 展示序号 / forward 换号循环 / 首事件闸门。"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi import HTTPException

import buddy_proxy.codebuddy_provider as cbp
from buddy_proxy.codebuddy_provider import credentials as creds
from buddy_proxy.codebuddy_provider import failover
from buddy_proxy.codebuddy_provider.provider import (
    CodeBuddyProvider,
    _error_frame_to_exception,
    _is_account_error,
)


def _acct(aid: str, uid: str | None = None) -> creds.AccountRef:
    return creds.AccountRef(id=aid, uid=uid or f"uid-{aid}", nickname=aid,
                            priority=0, added_at=1000)


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    failover._cooldowns.clear()
    yield
    failover._cooldowns.clear()


@pytest.fixture(autouse=True)
def _fake_global_state(monkeypatch):
    """forward 里 diagnostic()/get_state() 读的是 core.state.proxy_state——
    给个最小假对象替换掉，不拉起全局初始化（那会读真实配置目录、未初始化
    时直接 503 proxy not initialized）。"""
    from types import SimpleNamespace
    from buddy_proxy.core import state as st

    monkeypatch.setattr(st, "proxy_state", SimpleNamespace(
        enable_desensitize=False, logger=None,
        write_log=lambda *a, **k: None))


# ---------------------------------------------------------------------------
# 冷却状态机薄壳
# ---------------------------------------------------------------------------

def test_mark_and_clear_cooldown():
    failover.mark_cooldown("a", quota=True)
    left, kind = failover.cooldown_left("a")
    assert left > 0 and kind == "quota"
    failover.clear_cooldown("a")
    assert failover.cooldown_left("a") == (0.0, "")


def test_retry_after_overrides_default():
    secs = failover.mark_cooldown("a", retry_after="3600")
    assert secs == 3600.0
    # 离谱值被钳到 7d
    secs = failover.mark_cooldown("b", retry_after="999999999")
    assert secs <= 7 * 86400.0


def test_available_accounts_filters_cooling():
    accounts = [_acct("a"), _acct("b")]
    failover.mark_cooldown("a")
    with unittest_mock(accounts):
        assert [x.id for x in failover.available_accounts()] == ["b"]
        failover.clear_cooldown("a")
        assert [x.id for x in failover.available_accounts()] == ["a", "b"]


def test_display_index_is_full_list_position():
    accounts = [_acct("a"), _acct("b"), _acct("c")]
    failover.mark_cooldown("a")
    with unittest_mock(accounts):
        idx = failover.display_index()
        assert idx == {"a": 1, "b": 2, "c": 3}, "序号是全量列表位次，冷却账号也占位"


def unittest_mock(accounts):
    import unittest.mock as mock
    return mock.patch.object(failover, "list_accounts", lambda: accounts)


def test_cooldown_report_mentions_only_cooling():
    accounts = [_acct("a"), _acct("b")]
    with unittest_mock(accounts):
        failover.mark_cooldown("a", quota=True, reason="429")
        report = failover.cooldown_report()
        assert "a" in report and "额度冷却" in report and "b" not in report


def test_accounts_status_shape():
    accounts = [_acct("u1"), _acct("u2", "uid-two")]
    with unittest_mock(accounts):
        st = failover.accounts_status()
    assert st["enabled"] is True
    assert [a["index"] for a in st["accounts"]] == [1, 2]
    a1 = st["accounts"][0]
    assert a1["id"] == "u1" and a1["nickname"] == "u1" and a1["token"] == "missing"


def test_accounts_status_hours_left_is_hours_not_ms():
    """expires_at_ms 是毫秒——算小时必须除 3_600_000，别把 trae 的秒口径抄过来。"""
    cred = {
        "token": "t", "refresh_token": "rt",
        "expires_at_ms": int((time.time() + 2 * 3600) * 1000),
    }
    accounts = [_acct("u1")]
    import unittest.mock as mock

    with unittest_mock(accounts), \
         mock.patch.object(failover, "load_account_cred", lambda aid: cred):
        st = failover.accounts_status()
    hours = st["accounts"][0]["hours_left"]
    assert 1.5 < hours <= 2.0, f"hours_left={hours}，毫秒当秒会爆到几百万"


# ---------------------------------------------------------------------------
# 账号错误判定
# ---------------------------------------------------------------------------

def test_is_account_error():
    assert _is_account_error(HTTPException(401, "x"))
    assert _is_account_error(HTTPException(429, "x"))
    assert not _is_account_error(HTTPException(400, "x"))
    assert not _is_account_error(HTTPException(502, "x"))


# ---------------------------------------------------------------------------
# 错误帧还原（首事件闸门的判据）
# ---------------------------------------------------------------------------

def test_error_frame_openai_carries_code():
    frame = (b'data: {"error":{"message":"Upstream API error (HTTP 429)",'
             b'"type":"upstream_error","code":429,"details":"quota"}}\n\n')
    exc = _error_frame_to_exception(frame, "openai")
    assert exc is not None and exc.status_code == 429


def test_error_frame_anthropic_parses_status_from_message():
    """anthropic 错误事件没有 code 字段——状态码只能从 message 的 (HTTP nnn) 抠。"""
    frame = (b'event: error\ndata: {"type":"error","error":{"type":"api_error",'
             b'"message":"Upstream API error (HTTP 401): bad token"}}\n\n')
    exc = _error_frame_to_exception(frame, "anthropic")
    assert exc is not None and exc.status_code == 401
    assert "bad token" in str(exc.detail)


def test_normal_first_chunk_is_not_an_error_frame():
    frame = b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
    assert _error_frame_to_exception(frame, "openai") is None
    assert _error_frame_to_exception(frame, "anthropic") is None


# ---------------------------------------------------------------------------
# forward 换号循环
# ---------------------------------------------------------------------------

def _setup_two_accounts(monkeypatch):
    a, b = _acct("acct-a"), _acct("acct-b")
    monkeypatch.setattr(failover, "list_accounts", lambda: [a, b])
    monkeypatch.setattr(creds, "list_accounts", lambda: [a, b])

    def _token(aid, **kw):
        return f"tok-{aid}", {"account_id": aid, "token": f"tok-{aid}",
                              "refresh_token": "rt", "uid": f"uid-{aid}",
                              "nickname": aid, "machine_id": "m",
                              "expires_at_ms": int(time.time() * 1000) + 3600_000}
    monkeypatch.setattr(creds, "ensure_account_token", _token)
    return a, b


OPENAI_OK_FRAMES = [
    b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n',
    b"data: [DONE]\n\n",
]


def _fake_stream_factory(fail_ids: set[str], seen: list[str]):
    """按账号 uid（headers X-User-Id）区分行为：fail_ids 吐 429 错误帧（形状随
    协议走——anthropic 是 event: error 帧不带 code，openai 是 error 块带 code），
    其余正常。"""

    def _stream(url, headers, body, protocol, original):
        uid = headers.get("X-User-Id", "")
        seen.append(uid)

        async def _gen():
            if uid in fail_ids:
                if protocol == "anthropic":
                    yield (b'event: error\ndata: {"type":"error","error":{'
                           b'"type":"api_error","message":"Upstream API error '
                           b'(HTTP 429): code 14018 quota exhausted"}}\n\n')
                else:
                    yield (b'data: {"error":{"message":"Upstream API error (HTTP 429)",'
                           b'"type":"upstream_error","code":429,"details":"14018"}}\n\n')
                return
            for f in OPENAI_OK_FRAMES:
                yield f
        return _gen()

    return _stream


async def _collect(agen) -> bytes:
    out = b""
    async for chunk in agen:
        out += chunk
    return out


async def _forward_and_collect(payload, protocol="openai"):
    """forward + 消费 body_iterator 必须在同一个事件循环里——asyncio.run 退出
    时 shutdown_asyncgens 会 aclose 挂起的 raw_gen，拆两次 run 第二帧就丢了
    （生产上 uvicorn 也是同一循环消费的）。"""
    resp = await CodeBuddyProvider().forward(payload, protocol, None)
    if hasattr(resp, "body_iterator"):
        return await _collect(resp.body_iterator)
    return resp


def test_forward_stream_failover_skips_exhausted_account(monkeypatch):
    """流式：账号 A 429 → 冷却换 B 正常；客户端只收到 B 的正常块，绝不吐错误帧。"""
    a, b = _setup_two_accounts(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(cbp, "stream_upstream", _fake_stream_factory({"uid-acct-a"}, seen))

    body = asyncio.run(_forward_and_collect(
        {"model": "kimi-k3", "stream": True, "messages": [{"role": "user", "content": "hi"}]}))
    assert b'"content":"hi"' in body
    assert b"data: [DONE]" in body, "换号后后续帧也要完整吐完（不是只重放首帧）"
    assert b'"error"' not in body, "错误帧不能漏给客户端（那会变成 200 假成功）"
    assert seen == ["uid-acct-a", "uid-acct-b"]
    # A 被记了额度冷却
    left, kind = failover.cooldown_left(a.id)
    assert left > 0 and kind == "quota"


def test_forward_stream_committed_after_first_real_chunk(monkeypatch):
    """首块正常后才出错：已 committed，不换号、不重放——错误原样透传给客户端。"""
    a, b = _setup_two_accounts(monkeypatch)
    seen: list[str] = []

    async def _stream(url, headers, body, protocol, original):
        seen.append(headers.get("X-User-Id", ""))
        yield OPENAI_OK_FRAMES[0]  # 正常首块 → 闸门放行
        yield b'data: {"error":{"message":"mid-stream boom","code":"x"}}\n\n'

    monkeypatch.setattr(cbp, "stream_upstream", _stream)
    body = asyncio.run(_forward_and_collect(
        {"model": "kimi-k3", "stream": True, "messages": [{"role": "user", "content": "hi"}]}))
    assert seen == ["uid-acct-a"], "首块放行后绝不换号（防重复计费）"
    assert b"mid-stream boom" in body


def test_forward_nonstream_failover(monkeypatch):
    """非流式：collect_upstream 撞 429 会 raise（返回前），循环冷却换号重试。"""
    a, b = _setup_two_accounts(monkeypatch)
    seen: list[str] = []
    from fastapi import HTTPException as _HE

    async def _collect(url, headers, body, protocol):
        uid = headers.get("X-User-Id", "")
        seen.append(uid)
        if uid == "uid-acct-a":
            raise _HE(status_code=429, detail={"error": {"message": "quota"}})
        return {"id": "x", "object": "chat.completion", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"},
             "finish_reason": "stop"}]}

    monkeypatch.setattr(cbp, "collect_upstream", _collect)
    resp = asyncio.run(CodeBuddyProvider().forward(
        {"model": "kimi-k3", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
        "openai", None))
    assert resp.body is not None
    assert seen == ["uid-acct-a", "uid-acct-b"]
    left, kind = failover.cooldown_left(a.id)
    assert left > 0 and kind == "quota"


def test_forward_all_accounts_exhausted_gives_429(monkeypatch):
    """全部账号 429：最后一个的错误透传（openai 协议抛 HTTPException）。"""
    a, b = _setup_two_accounts(monkeypatch)
    monkeypatch.setattr(cbp, "stream_upstream", _fake_stream_factory(
        {"uid-acct-a", "uid-acct-b"}, []))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(CodeBuddyProvider().forward(
            {"model": "kimi-k3", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            "openai", None))
    assert ei.value.status_code == 429


def test_forward_anthropic_protocol_wraps_final_error(monkeypatch):
    """anthropic 协议全账号失败：错误体转标准 {type: error} JSONResponse。"""
    _setup_two_accounts(monkeypatch)
    monkeypatch.setattr(cbp, "stream_upstream", _fake_stream_factory(
        {"uid-acct-a", "uid-acct-b"}, []))
    resp = asyncio.run(CodeBuddyProvider().forward(
        {"model": "kimi-k3", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        "anthropic", None))
    assert resp.status_code == 429
    payload = json.loads(bytes(resp.body))
    assert payload["type"] == "error"
    assert "error" in payload


def test_forward_no_accounts_429(monkeypatch):
    monkeypatch.setattr(failover, "list_accounts", lambda: [])
    with pytest.raises(HTTPException) as ei:
        asyncio.run(CodeBuddyProvider().forward(
            {"model": "kimi-k3", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            "openai", None))
    assert ei.value.status_code == 429
    assert "冷却" in str(ei.value.detail)


def test_quota_epoch_changes_with_accounts(monkeypatch):
    p = CodeBuddyProvider()
    monkeypatch.setattr(creds, "list_accounts",
                        lambda: [_acct("a"), creds.AccountRef(
                            id="b", uid="uid-b", nickname="b", priority=1, added_at=2)])
    e1 = p.quota_epoch()
    monkeypatch.setattr(creds, "list_accounts",
                        lambda: [_acct("a")])
    assert p.quota_epoch() != e1, "账号列表一变 epoch 必须变（旧额度快照作废）"


# ---------------------------------------------------------------------------
# 渠道风控拦截（400 + 11128）与上游读超时（2026-11-07 dsh 事故）
# ---------------------------------------------------------------------------

async def _agen(frames: list[bytes]):
    """把帧列表包装成 ``stream_upstream`` 那样的异步生成器。"""
    for frame in frames:
        yield frame


def _channel_block_frames(details: str) -> list[bytes]:
    """``pipeline.stream_upstream`` 撞非 200 时吐的那个错误块（openai 协议）。"""
    chunk = {"error": {"message": "Upstream API error (HTTP 400)",
                       "type": "upstream_error", "code": 400,
                       "details": details}}
    return [f"data: {json.dumps(chunk)}\n\n".encode()]


def test_forward_stream_channel_block_marks_channel(monkeypatch):
    """流式请求撞 400 + 11128：抛 HTTPException(400) 且整条通道被打上渠道封禁标记。

    首事件闸门（``_forward_once``）负责把错误帧还原成异常；本测试锁住「还原出来的
    异常要能被识别成渠道拦截并标记」——model_order 靠这个标记跳过整条通道。
    """
    from buddy_proxy.core import cooldown as cooldown_mod

    cooldown_mod._reset_for_tests()
    _setup_two_accounts(monkeypatch)
    details = json.dumps({"detail": {"error": {
        "code": 11128, "msg": "Illegal API invocation from an unapproved channel"}}})
    monkeypatch.setattr(cbp, "stream_upstream",
                        lambda *a, **k: _agen(_channel_block_frames(details)))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(CodeBuddyProvider().forward(
            {"model": "deepseek-v4.1-flash", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]}, "openai", None))
    assert ei.value.status_code == 400
    assert cooldown_mod.is_marked("codebuddy", "*"), "渠道封禁必须打整通道标记"
    assert cooldown_mod.is_marked("codebuddy", "deepseek-v4.1-flash") is False
    cooldown_mod._reset_for_tests()


def test_forward_stream_channel_block_with_escaped_string_details(monkeypatch):
    """线上真实形状：11128 被序列化成**转义字符串**塞进 ``details``，也要认出来。

    ``stream_upstream`` 的 ``details`` 是 ``error_text[:500]``，外层再 ``json.dumps``
    一次 → 引号变成 ``\\"``。第一版修复只解了外层，把 ``code=400``（HTTP 状态本身）
    当成业务码，于是流式路径既不认封禁、也没给整条通道打标记（线上 08:26:53 那次
    codebuddy 仍然每请求白撞一次、客户端拿到原样 400）。
    """
    from buddy_proxy.core import cooldown as cooldown_mod

    cooldown_mod._reset_for_tests()
    _setup_two_accounts(monkeypatch)
    details = json.dumps({"code": 11128,
                          "msg": "Illegal API invocation from an unapproved channel"})
    monkeypatch.setattr(cbp, "stream_upstream",
                        lambda *a, **k: _agen(_channel_block_frames(details)))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(CodeBuddyProvider().forward(
            {"model": "deepseek-v4.1-flash", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]}, "openai", None))
    assert ei.value.status_code == 400
    assert cooldown_mod.is_marked("codebuddy", "*"), "整条通道必须进冷却"
    cooldown_mod._reset_for_tests()


def test_forward_stream_channel_block_truncated_details_still_detected(monkeypatch):
    """上游错误体被 500 字节截断（没有闭合括号）时仍要认出 11128。"""
    from buddy_proxy.core import cooldown as cooldown_mod

    cooldown_mod._reset_for_tests()
    _setup_two_accounts(monkeypatch)
    raw = ('{"code":11128,"msg":"Illegal API invocation from an unapproved '
           'channel","extra":"' + "x" * 600)
    details = raw[:500]  # 与 stream_upstream 的 error_text[:500] 一致：尾部被截断
    monkeypatch.setattr(cbp, "stream_upstream",
                        lambda *a, **k: _agen(_channel_block_frames(details)))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(CodeBuddyProvider().forward(
            {"model": "deepseek-v4.1-flash", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]}, "openai", None))
    assert ei.value.status_code == 400
    assert cooldown_mod.is_marked("codebuddy", "*")
    cooldown_mod._reset_for_tests()


def test_forward_stream_channel_block_logs_upstream_code(monkeypatch):
    """命中封禁时 ``channel_blocked`` 诊断里带 ``upstream_code=11128``，便于排查。"""
    from types import SimpleNamespace
    from buddy_proxy.core import cooldown as cooldown_mod
    from buddy_proxy.core import state as st

    cooldown_mod._reset_for_tests()
    _setup_two_accounts(monkeypatch)
    lines: list[str] = []
    monkeypatch.setattr(st, "proxy_state", SimpleNamespace(
        enable_desensitize=False,
        logger=SimpleNamespace(info=lines.append),
        write_log=lambda *a, **k: None))
    details = json.dumps({"code": 11128, "msg": "Illegal API invocation"})
    monkeypatch.setattr(cbp, "stream_upstream",
                        lambda *a, **k: _agen(_channel_block_frames(details)))
    with pytest.raises(HTTPException):
        asyncio.run(CodeBuddyProvider().forward(
            {"model": "deepseek-v4.1-flash", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]}, "openai", None))
    hits = [ln for ln in lines if ln.startswith("channel_blocked")]
    assert hits, lines
    assert "11128" in hits[0]
    cooldown_mod._reset_for_tests()


def test_upstream_error_code_unwraps_nested_and_lowercase():
    """错误码提取：多层转义字符串、键名大小写、截断 JSON 都要认。"""
    from buddy_proxy.codebuddy_provider.provider import _upstream_error_code

    inner = json.dumps({"code": 11128, "msg": "x"})
    assert _upstream_error_code(json.dumps({"details": inner})) == 11128
    assert _upstream_error_code(json.dumps({"details": inner}, ensure_ascii=False)) == 11128
    assert _upstream_error_code('{"Code": 11128}') == 11128
    assert _upstream_error_code('{"code":11128,"msg":"' + "y" * 700) == 11128
    assert _upstream_error_code("bad request") is None
    # 错误帧外壳的 code=400 是 HTTP 状态码，不能遮住 details 里的业务码 11128
    assert _upstream_error_code(
        json.dumps({"error": {"code": 400, "details": inner}})) == 11128


def test_json_loads_tolerant_truncated_at_half_escape():
    """截断点正好落在半个转义上（``\\"m`` 这种）仍能解出结构。

    500 字节的截断边界是不可控的：末尾可能是 ``abc``、也可能是反斜杠 + 引号的
    半截。补闭合符时「先补引号」会把被截断的转义引号当成字符串结束，这时
    「不补引号、只补括号」那条路要能救回来。
    """
    from buddy_proxy.codebuddy_provider.provider import _upstream_error_code

    raw = '{"code":11128,"msg":"Illegal API invocation \\"from an unapproved'
    assert _upstream_error_code(raw) == 11128
    assert _upstream_error_code(raw.replace("\\\"", "\\", 1)) == 11128
    # 结尾是单个反斜杠（转义刚写一半）也不能崩
    assert _upstream_error_code('{"code":11128,"msg":"abc\\') == 11128


def test_json_loads_tolerant_does_not_invent_object_for_quote_leading_text():
    """以引号开头但不含任何已知键的文本，不得被硬包装成对象。"""
    from buddy_proxy.codebuddy_provider.provider import _json_loads_tolerant

    assert _json_loads_tolerant('"just a quoted fragment') is None
    assert _json_loads_tolerant('"codebutnotakey":1') is None


def test_note_channel_block_omits_upstream_code_when_unknown(monkeypatch):
    """抠不出业务码时不写 ``upstream_code``，别让日志读成「码是 None」。"""
    from types import SimpleNamespace
    from fastapi import HTTPException
    from buddy_proxy.core import cooldown as cooldown_mod
    from buddy_proxy.core import state as st
    from buddy_proxy.codebuddy_provider.provider import _note_channel_block

    cooldown_mod._reset_for_tests()
    lines: list[str] = []
    monkeypatch.setattr(st, "proxy_state", SimpleNamespace(
        enable_desensitize=False,
        logger=SimpleNamespace(info=lines.append),
        write_log=lambda *a, **k: None))
    _note_channel_block("codebuddy", HTTPException(400, "plain bad request"))
    assert cooldown_mod.is_marked("codebuddy", "*")
    hits = [ln for ln in lines if ln.startswith("channel_blocked")]
    assert hits, lines
    assert "upstream_code" not in hits[0]
    cooldown_mod._reset_for_tests()


def test_forward_plain_400_does_not_mark_channel(monkeypatch):
    """普通 400（没有 11128 业务码）不得打渠道封禁标记——那是参数错，不关通道的事。"""
    from buddy_proxy.core import cooldown as cooldown_mod

    cooldown_mod._reset_for_tests()
    _setup_two_accounts(monkeypatch)
    monkeypatch.setattr(cbp, "stream_upstream",
                        lambda *a, **k: _agen(_channel_block_frames('{"code":11152}')))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(CodeBuddyProvider().forward(
            {"model": "deepseek-v4.1-flash", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]}, "openai", None))
    assert ei.value.status_code == 400
    assert cooldown_mod.is_marked("codebuddy", "*") is False
    cooldown_mod._reset_for_tests()


def test_upstream_read_timeout_is_long_enough_for_slow_first_token():
    """读超时不再卡在 300 秒：dsh 长上下文的首 token 可以更久，慢启动归总时长管。"""
    from buddy_proxy.codebuddy_provider.pipeline import (
        UPSTREAM_CONNECT_TIMEOUT_S, _upstream_timeout,
    )

    timeout = _upstream_timeout()
    assert timeout.read == pytest.approx(1800)
    assert timeout.read > 300
    assert timeout.connect == pytest.approx(UPSTREAM_CONNECT_TIMEOUT_S)
    assert timeout.connect == pytest.approx(30)


def test_upstream_read_timeout_env_override(monkeypatch):
    """``BUDDY_UPSTREAM_READ_TIMEOUT`` 可覆盖；非法/非正数回落到 1800。"""
    from buddy_proxy.codebuddy_provider import pipeline

    monkeypatch.setenv("BUDDY_UPSTREAM_READ_TIMEOUT", "600")
    assert pipeline._env_read_timeout() == 600
    monkeypatch.setenv("BUDDY_UPSTREAM_READ_TIMEOUT", "abc")
    assert pipeline._env_read_timeout() == 1800
    monkeypatch.setenv("BUDDY_UPSTREAM_READ_TIMEOUT", "0")
    assert pipeline._env_read_timeout() == 1800
