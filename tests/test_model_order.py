"""`model_order`（候选上游顺序 + 失败换档 + 冷却标记）回归测试。

锁住四件事：

1. **换档只发生在「未提交即失败」时**——抛可重试异常、或拿到非 2xx 的 JSONResponse
   才换下一档；已开始流式返回的响应绝不重放（防重复计费，同 trae/pat 的「首个语义
   事件提交后绝不重放」不变量）。
2. 确定性错误（403 停用 / 400 参数）不换档，原样上抛。
3. 冷却标记被跳过、会升级、会过期；被停用/不在时段的目标跳过而不是把整个模型打成 403。
4. **未配 ``model_order`` 时路由行为与历史完全一致**（最重要的回归护栏）。

运行：
    .venv/bin/python -m pytest tests/test_model_order.py -v
"""
from __future__ import annotations

import socket
import time
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from buddy_proxy import __main__ as m
from buddy_proxy.core import cooldown as cooldown_mod
from buddy_proxy.core import settings as settings_mod
from buddy_proxy.core import state as st
from buddy_proxy.core.metrics import MetricsCollector
from buddy_proxy.providers.base import BaseProvider


def _make_state(providers, tmp_path, **overrides):
    client = mock.MagicMock()
    client.endpoint = "https://fake.endpoint.invalid"
    client.auth_headers.return_value = {}
    client.session = {"auth": {"accessToken": "t",
                               "expiresAt": int((time.time() + 3600) * 1000)}}
    state = SimpleNamespace(
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
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """隔离设置文件 + 清空模块级冷却标记（单例，必须逐个测试清）。"""
    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(tmp_path / "settings.json"))
    cooldown_mod._reset_for_tests()
    yield
    cooldown_mod._reset_for_tests()


class _Provider(BaseProvider):
    """可编排 forward 行为的假通道：记录调用次数与收到的 body。"""

    def __init__(self, pid, models, behavior="ok"):
        self.id = pid
        self.name = f"Fake {pid}"
        self._models = models
        self.behavior = behavior
        self.calls: list[dict] = []

    def models(self):
        return [{"id": mid} for mid in self._models]

    def ensure_auth(self):
        pass

    async def forward(self, body, protocol, original=None):
        self.calls.append(dict(body))
        if self.behavior == "ok":
            return JSONResponse({
                "id": f"chatcmpl-{self.id}", "model": body.get("model"),
                "choices": [{"message": {"role": "assistant",
                                         "content": f"from-{self.id}"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2}})
        if self.behavior == "raise502":
            raise HTTPException(status_code=502, detail="upstream boom")
        if self.behavior == "bug_typeerror":
            # 真编程错误（不是上游故障）：换档**不得**掩盖它
            raise TypeError("cannot unpack non-sequence NoneType")
        if self.behavior == "raise_httpx":
            # 传输层异常：换档应当接住（通道没自己转成 HTTPException 的漏网情形）
            raise httpx.ConnectError("connection refused")
        if self.behavior == "dns_502":
            # 复刻 2026-10-02 事故的真实形态：provider 把 DNS 解析失败
            # （httpx.ConnectError → httpcore.ConnectError → socket.gaierror）
            # 转成 HTTPException(502) 再抛给换档层。
            gai = socket.gaierror(8, "nodename nor servname provided, or not known")
            cause = httpx.ConnectError(str(gai))
            cause.__cause__ = gai
            raise HTTPException(status_code=502,
                                detail={"error": {"message": f"upstream error: {cause}",
                                                  "type": "upstream_error"}}) from cause
        if self.behavior == "dns_httpx":
            # 同一故障未被 provider 包装、直接以 httpx 异常冒到换档层的形态
            gai = socket.gaierror(8, "nodename nor servname provided, or not known")
            exc = httpx.ConnectError(str(gai))
            exc.__cause__ = gai
            raise exc
        if self.behavior == "raise400":
            raise HTTPException(status_code=400, detail="bad params")
        if self.behavior == "json429":
            return JSONResponse(status_code=429,
                                content={"error": {"message": "rate limited"}})
        if self.behavior == "json405":
            # zcode-start 风控拦截的真实形态（2026-10-06 /model 探测实测）：
            # 上游 405 + 3012「unusual activity」原样透传
            return JSONResponse(status_code=405,
                                content={"code": 3012,
                                         "msg": "request has been blocked due to unusual activity."})
        if self.behavior == "stream_then_break":
            # 生成器里抛的异常是在**响应已返回、首个 chunk 已流出**之后才发生的，
            # 路由器完全看不到——这正是「已提交即不换档」要覆盖的场景。
            async def gen():
                yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                raise RuntimeError("mid-stream explosion")
            return StreamingResponse(gen(), media_type="text/event-stream")
        if self.behavior == "stream_ok":
            async def gen_ok():
                yield b'data: {"choices":[{"delta":{"content":"streamed"}}]}\n\n'
            return StreamingResponse(gen_ok(), media_type="text/event-stream")
        raise AssertionError(f"unknown behavior {self.behavior}")


def _client_env(tmp_path, monkeypatch, order, a_behavior="ok", b_behavior="ok"):
    """两档候选 a→b，模型名 m1；返回 (client, state, a, b)。"""
    a = _Provider("pa", ["m1"], a_behavior)
    b = _Provider("pb", ["m1"], b_behavior)
    state = _make_state({"pa": a, "pb": b}, tmp_path,
                        model_order={"pa/m1": order} if order else {})
    monkeypatch.setattr(st, "proxy_state", state)
    return TestClient(m.app), state, a, b


def _post(client, model="m1", stream=False):
    return client.post("/v1/chat/completions",
                       json={"model": model, "stream": stream,
                             "messages": [{"role": "user", "content": "hi"}]})


# --- 1. 未提交即失败 → 换档 ---------------------------------------------------


def test_failover_on_raised_http_exception(tmp_path, monkeypatch):
    """第一档抛 502（zcode/trae 传输失败的形态）→ 落到第二档，且第二档只调一次。"""
    client, _, a, b = _client_env(tmp_path, monkeypatch,
                                  ["pa/m1", "pb/m1"], a_behavior="raise502")
    r = _post(client)
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "from-pb"
    assert len(a.calls) == 1 and len(b.calls) == 1


def test_failover_on_non_2xx_json_response(tmp_path, monkeypatch):
    """第一档返回非 2xx 的 JSONResponse（zcode/mimo 的 _upstream_error_response）→ 换档。"""
    client, _, a, b = _client_env(tmp_path, monkeypatch,
                                  ["pa/m1", "pb/m1"], a_behavior="json429")
    r = _post(client)
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "from-pb"
    assert len(b.calls) == 1


def test_failover_on_405_risk_control_block(tmp_path, monkeypatch):
    """**承重测试**：405 风控拦截必须换档，不能当成确定性错误直接回给客户端。

    2026-10-06 事故：``/model glm-5.3-flash`` 撞 zcode-start 3012「unusual
    activity」（HTTP 405），``_RETRYABLE_STATUS`` 不含 405 → 第一档的 405
    被原样返回，model_order 里配的 zcode / codebuddy 兜底一次都没试——
    用户看到「明明还有渠道却报错」。405 从聊天上游回来只可能是 WAF 拦截
    （本代理只会 POST），换通道正是解法。
    """
    client, _, a, b = _client_env(tmp_path, monkeypatch,
                                  ["pa/m1", "pb/m1"], a_behavior="json405")
    r = _post(client)
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "from-pb"
    assert len(a.calls) == 1 and len(b.calls) == 1
    # 拦截它的通道进冷却，后续请求不再白试它
    assert cooldown_mod.is_marked("pa", "m1") is True


def test_failover_marks_failed_target(tmp_path, monkeypatch):
    """换档会给失败目标打冷却标记。"""
    client, _, a, _ = _client_env(tmp_path, monkeypatch,
                                  ["pa/m1", "pb/m1"], a_behavior="raise502")
    _post(client)
    assert cooldown_mod.is_marked("pa", "m1") is True
    assert cooldown_mod.remaining("pa", "m1") > 0


def test_all_candidates_fail_raises_last_error(tmp_path, monkeypatch):
    """全部候选都失败 → 抛最后一个可换档失败（保留上游真因，而非笼统 502）。"""
    client, _, a, b = _client_env(tmp_path, monkeypatch,
                                  ["pa/m1", "pb/m1"],
                                  a_behavior="raise502", b_behavior="raise502")
    r = _post(client)
    assert r.status_code == 502
    assert len(a.calls) == 1 and len(b.calls) == 1


# --- 2. 已提交 / 确定性错误 → 不换档 ------------------------------------------


def test_no_failover_after_streaming_committed(tmp_path, monkeypatch):
    """**承重测试**：第一档返回的是 StreamingResponse，第二档绝不能被执行。

    这条锁的是整个特性的安全边界：一旦响应对象交给 ASGI、字节开始流向客户端，
    重放就可能让上游计费两次、客户端收到两段拼接的流。注意断言方式是直接检查
    ``pb.calls`` 为空——即使第一档的流后续会炸，路由器也不该也看不到、更不能换档。
    """
    client, _, a, b = _client_env(tmp_path, monkeypatch,
                                  ["pa/m1", "pb/m1"], a_behavior="stream_ok")
    r = _post(client, stream=True)
    assert r.status_code == 200
    assert "streamed" in r.text
    assert len(a.calls) == 1
    assert b.calls == [], "已提交语义后不得换档"


def test_streaming_attempt_does_not_mark_cooldown(tmp_path, monkeypatch):
    """流式尝试成功时不该打冷却标记（标记只属于换档失败）。"""
    client, _, a, b = _client_env(tmp_path, monkeypatch,
                                  ["pa/m1", "pb/m1"], a_behavior="stream_ok")
    _post(client, stream=True)
    assert cooldown_mod.is_marked("pa", "m1") is False


def test_non_retryable_http_exception_propagates(tmp_path, monkeypatch):
    """400（确定性错误）不换档，原样上抛——换谁都不会成功。"""
    client, _, a, b = _client_env(tmp_path, monkeypatch,
                                  ["pa/m1", "pb/m1"], a_behavior="raise400")
    r = _post(client)
    assert r.status_code == 400
    assert b.calls == []
    assert cooldown_mod.is_marked("pa", "m1") is False, "确定性错误不该打冷却标记"


def test_programming_error_is_not_masked_as_failover(tmp_path, monkeypatch):
    """**承重测试**：真编程错误必须原样暴露，不得被当成上游故障换档掩盖。

    裸 ``except Exception`` 会把 ``TypeError`` 之类判成「可换档失败」：打冷却标记、
    换到下一档，客户端拿到 200，真因只剩一行日志（且非 ``HTTPException`` 不进
    ``_instrument``，指标里也看不到）。真 bug 被换档「治好」比直接报错更难排查。
    """
    client, _, a, b = _client_env(tmp_path, monkeypatch,
                                  ["pa/m1", "pb/m1"], a_behavior="bug_typeerror")
    with pytest.raises(TypeError):
        _post(client)
    assert len(a.calls) == 1
    assert b.calls == [], "编程错误不得换档"
    assert cooldown_mod.is_marked("pa", "m1") is False, "编程错误不该把通道打成冷却"


def test_transport_error_still_fails_over(tmp_path, monkeypatch):
    """收窄 ``except`` 后，传输层异常仍要能换档（通道没自己包成 HTTPException 时）。"""
    client, _, a, b = _client_env(tmp_path, monkeypatch,
                                  ["pa/m1", "pb/m1"], a_behavior="raise_httpx")
    r = _post(client)
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "from-pb"
    assert len(a.calls) == 1 and len(b.calls) == 1
    assert cooldown_mod.is_marked("pa", "m1") is True


def test_disabled_target_is_skipped_not_fatal(tmp_path, monkeypatch):
    """候选里被停用的目标跳过并继续（决策：跳过而非把整个模型打成 403）。"""
    client, state, a, b = _client_env(tmp_path, monkeypatch, ["pa/m1", "pb/m1"])
    state.disabled_models = {"pa/m1"}
    r = _post(client)
    assert r.status_code == 200, r.text
    assert a.calls == [], "被停用的候选不该被调用"
    assert len(b.calls) == 1


def test_scheduled_out_target_is_skipped(tmp_path, monkeypatch):
    """不在可用时段的目标同样跳过并继续。"""
    client, state, a, b = _client_env(tmp_path, monkeypatch, ["pa/m1", "pb/m1"])
    state.model_schedules = {"pa/m1": [["00:00", "00:01"]]}
    monkeypatch.setattr(settings_mod, "model_schedule_open", lambda w: False)
    r = _post(client)
    assert r.status_code == 200, r.text
    assert a.calls == []
    assert len(b.calls) == 1


def test_marked_target_is_skipped(tmp_path, monkeypatch):
    """已被标记的目标直接跳过，不发起请求。"""
    client, _, a, b = _client_env(tmp_path, monkeypatch, ["pa/m1", "pb/m1"])
    cooldown_mod.mark_failed("pa", "m1", status=502)
    r = _post(client)
    assert r.status_code == 200, r.text
    assert a.calls == []
    assert len(b.calls) == 1


def test_all_targets_marked_or_gated_reports_unavailable(tmp_path, monkeypatch):
    """全部候选被标记/停用 → 502 且文案说明原因（不是笼统 internal error）。"""
    client, state, a, b = _client_env(tmp_path, monkeypatch, ["pa/m1", "pb/m1"])
    cooldown_mod.mark_failed("pa", "m1")
    cooldown_mod.mark_failed("pb", "m1")
    r = _post(client)
    assert r.status_code == 502
    assert "候选上游全部不可用" in r.text
    assert a.calls == [] and b.calls == []


def test_dns_502_does_not_mark_cooldown(tmp_path, monkeypatch):
    """本机 DNS 解析失败**不**打冷却标记（2026-10-02 事故回归）。

    事故形态：一次约 1 秒的本机 DNS 抖动让三个候选通道同时被打上 5 分钟冷却，
    此后客户端每次重试都只命中 ``model_order_skip reason=cooldown``，一个上游
    请求都没发出去，整个模型锁死 5 分钟。

    这里锁住两点：失败的那档不被打标记；同一次请求里的下一档仍被正常尝试
    （而不是因为前者的冷却被跳过）。
    """
    client, _, a, b = _client_env(tmp_path, monkeypatch,
                                  ["pa/m1", "pb/m1"], a_behavior="dns_502")
    r = _post(client)
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "from-pb"
    assert len(a.calls) == 1 and len(b.calls) == 1
    # 关键断言：DNS 失败不留下冷却标记
    assert cooldown_mod.is_marked("pa", "m1") is False
    assert cooldown_mod.remaining("pa", "m1") == 0


def test_dns_error_not_marked_even_when_all_candidates_fail(tmp_path, monkeypatch):
    """所有候选都因 DNS 失败 → 仍返回 502，但**一个标记都不留**。

    全部失败时返回 502 是对的（客户端确实拿不到结果）；要防的是「留下冷却导致
    接下来的重试连试都不试」。DNS 是机器级故障，几秒后自愈，必须允许立即重试。
    """
    client, _, a, b = _client_env(tmp_path, monkeypatch, ["pa/m1", "pb/m1"],
                                  a_behavior="dns_502", b_behavior="dns_502")
    r = _post(client)
    assert r.status_code == 502
    assert cooldown_mod.is_marked("pa", "m1") is False
    assert cooldown_mod.is_marked("pb", "m1") is False
    assert cooldown_mod.snapshot() == {}

    # 紧接着重试：两个候选都还要被真正调一次（没有被冷却挡掉）
    a.calls.clear()
    b.calls.clear()
    _post(client)
    assert len(a.calls) == 1 and len(b.calls) == 1


def test_dns_httpx_form_also_not_marked(tmp_path, monkeypatch):
    """同一故障未被 provider 包装（异常直接冒到换档层）时同样不打标记。"""
    client, _, a, b = _client_env(tmp_path, monkeypatch,
                                  ["pa/m1", "pb/m1"], a_behavior="dns_httpx")
    r = _post(client)
    assert r.status_code == 200, r.text
    assert cooldown_mod.is_marked("pa", "m1") is False


def test_non_dns_transport_failure_still_marks_cooldown(tmp_path, monkeypatch):
    """非 DNS 的传输层失败照常打标记——收窄识别范围，别把冷却功能整个废掉。"""
    client, _, a, b = _client_env(tmp_path, monkeypatch,
                                  ["pa/m1", "pb/m1"], a_behavior="raise_httpx")
    r = _post(client)
    assert r.status_code == 200, r.text
    assert cooldown_mod.is_marked("pa", "m1") is True


def test_is_local_dns_failure_walks_exception_chain():
    """识别沿 ``__cause__`` 链下钻——provider 边界会把顶层类型换掉。"""
    from buddy_proxy.core.errors import is_local_dns_failure

    gai = socket.gaierror(8, "nodename nor servname provided, or not known")

    # 真实链：httpx.ConnectError → httpcore.ConnectError → socket.gaierror
    inner = httpx.ConnectError(str(gai))
    inner.__cause__ = gai
    outer = httpx.ConnectError(str(gai))
    outer.__cause__ = inner
    assert is_local_dns_failure(outer) is True

    # 被 provider 转成 HTTPException 之后（事故里的真实形态）
    he = HTTPException(status_code=502, detail="upstream error")
    he.__cause__ = outer
    assert is_local_dns_failure(he) is True

    # 非 DNS 的失败一律 False，别把冷却功能废掉
    assert is_local_dns_failure(httpx.ConnectError("connection refused")) is False
    assert is_local_dns_failure(httpx.ReadTimeout("read timed out")) is False
    assert is_local_dns_failure(HTTPException(status_code=502, detail="boom")) is False
    assert is_local_dns_failure(TypeError("bad arg")) is False
    assert is_local_dns_failure(None) is False


def test_describe_exception_reads_through_empty_message():
    """空消息异常要能沿链读出真因（zcode「upstream error: 」空日志的回归）。"""
    from buddy_proxy.core.errors import describe_exception

    # httpcore 映射出的 httpx 异常自身 str() 为空——直接打日志就是一行空话
    assert str(httpx.ReadError("")) == ""
    assert describe_exception(httpx.ReadError("")) == "ReadError"

    # 真因在下层：要能读出来
    gai = socket.gaierror(8, "nodename nor servname provided, or not known")
    exc = httpx.ReadError("")
    exc.__cause__ = gai
    desc = describe_exception(exc)
    assert "gaierror" in desc and "nodename nor servname" in desc

    # 顶层有内容时只用顶层，不铺开整条链（避免日志噪音）
    top = ValueError("clear message")
    top.__cause__ = RuntimeError("inner noise")
    assert describe_exception(top) == "clear message"

    assert describe_exception(None) == "unknown error"
    assert len(describe_exception(ValueError("x" * 1000))) == 300


# --- 2.5 机器级突发（多通道同时失败 → 短冷却）---------------------------------


class _FakeClock:
    """cooldown 模块内 time.time() 的替身：手动拨针，精确控制突发窗口滑动。"""

    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t


@pytest.fixture
def fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(cooldown_mod, "time", clock)
    yield clock


def test_two_channels_failing_together_get_short_cooldown(tmp_path, monkeypatch):
    """两个通道 60 秒内先后失败 → 机器级突发，双双只冷却 30 秒。

    2026-10-02 晚间事故回归：本机代理隧道中断，qoder/codebuddy/trae 在 50 秒内
    全灭，各自被按「上游自己的毛病」锁 5 分钟——而故障约 5 分钟后自愈，冷却时长
    恰好与中断时长重叠。机器级突发该用短冷却，让「网络刚恢复就放行」成为可能。
    """
    client, _, a, b = _client_env(tmp_path, monkeypatch, ["pa/m1", "pb/m1"],
                                  a_behavior="raise502", b_behavior="raise502")
    r = _post(client)
    assert r.status_code == 502
    # pa 先失败时没有第二个通道的证据、按普通故障记 5 分钟；pb 失败坐实突发后，
    # pa 要被**回头**缩短——否则最先失败的目标反而被锁得最久。
    assert 0 < cooldown_mod.remaining("pa", "m1") <= 30
    assert 0 < cooldown_mod.remaining("pb", "m1") <= 30


def test_single_channel_failure_keeps_long_cooldown(tmp_path, monkeypatch):
    """只有一个通道失败（另一个正常接住）→ 不算突发，照常 5 分钟。"""
    client, _, a, b = _client_env(tmp_path, monkeypatch,
                                  ["pa/m1", "pb/m1"], a_behavior="raise502")
    r = _post(client)
    assert r.status_code == 200, r.text
    assert cooldown_mod.remaining("pa", "m1") > 240


def test_burst_window_slides_and_recovers(fake_clock):
    """窗口滑动：60 秒内两个通道算突发；窗口滑走后回到普通长冷却判定。"""
    c = cooldown_mod
    c.mark_failed("pa", "m1", status=502)      # t0：第一个，普通 5 分钟
    assert c.remaining("pa", "m1") > 240
    fake_clock.t += 18                          # 第二个通道 18 秒后也失败 → 突发成立
    c.mark_failed("pb", "m1", status=502)
    assert 0 < c.remaining("pb", "m1") <= 30
    assert 0 < c.remaining("pa", "m1") <= 30
    fake_clock.t += 30                          # 短冷却到期：双双解禁
    assert not c.is_marked("pa", "m1") and not c.is_marked("pb", "m1")
    fake_clock.t += 45                          # 距前两次失败均已超 60 秒，窗口滑走
    c.mark_failed("pc", "m1", status=502)
    assert c.remaining("pc", "m1") > 240        # 只剩它自己失败 → 普通 5 分钟


def test_burst_never_escalates_even_with_repeated_failures(fake_clock):
    """持续故障期间反复失败也只 30 秒，绝不升级到 1 小时。

    升级机制是为「反复失败的**真上游**」准备的；机器级失败不是上游的错，
    不能让它吃到升级档，否则一次 5 分钟的网络抖动又变回 1 小时锁死。
    """
    c = cooldown_mod
    for i in range(6):                          # 普通失败 3 次就该升级了
        if i:
            fake_clock.t += 31                  # 冷却过期、突发窗口（60 秒）还在
        c.mark_failed("pa", "m1", status=502)
        c.mark_failed("pb", "m1", status=502)
    assert 0 < c.remaining("pa", "m1") <= 30
    assert 0 < c.remaining("pb", "m1") <= 30


def test_burst_is_per_model(fake_clock):
    """不同模型的失败不互相触发：突发按模型分窗，两个模型各坏各的不算机器级。"""
    c = cooldown_mod
    c.mark_failed("pa", "m1", status=502)
    c.mark_failed("pb", "m2", status=502)
    assert c.remaining("pa", "m1") > 240
    assert c.remaining("pb", "m2") > 240


def test_quota_429_failures_do_not_trigger_burst(fake_clock):
    """429 不进突发窗口：双通道同时限流走固定的限频短冷却，而不是突发 30 秒档。"""
    c = cooldown_mod
    c.mark_failed("pa", "m1", status=429)
    c.mark_failed("pb", "m1", status=429)
    assert 30 < c.remaining("pa", "m1") <= c._RATE_LIMIT_COOLDOWN_S
    assert 30 < c.remaining("pb", "m1") <= c._RATE_LIMIT_COOLDOWN_S


def test_429_short_cooldown_never_escalates(fake_clock):
    """限频是分钟级滑动窗口：反复 429 也只冷却 _RATE_LIMIT_COOLDOWN_S，不升 1 小时。"""
    c = cooldown_mod
    for _ in range(6):
        c.mark_failed("pa", "m1", status=429)
        assert c.remaining("pa", "m1") <= c._RATE_LIMIT_COOLDOWN_S
        fake_clock.t += c._RATE_LIMIT_COOLDOWN_S + 1
        assert not c.is_marked("pa", "m1")


def test_429_does_not_shorten_existing_long_cooldown(fake_clock):
    """已有的长冷却（如 502 升级档）不会被随后的 429 短档缩短。"""
    c = cooldown_mod
    for _ in range(3):
        c.mark_failed("pa", "m1", status=502)
    assert c.remaining("pa", "m1") > 3000
    c.mark_failed("pa", "m1", status=429)
    assert c.remaining("pa", "m1") > 3000


def test_429_does_not_pollute_burst_window(fake_clock):
    """夹在 5xx 之间的 429 不参与突发计数：突发只看网络类失败。"""
    c = cooldown_mod
    c.mark_failed("pa", "m1", status=502)      # 网络类，进窗口（此时独自一人）
    c.mark_failed("pb", "m1", status=429)      # 限流，不进窗口 → 限频短冷却
    assert 30 < c.remaining("pb", "m1") <= c._RATE_LIMIT_COOLDOWN_S
    fake_clock.t += 20
    c.mark_failed("pc", "m1", status=502)      # 与 pa 同窗 → 突发成立
    assert 0 < c.remaining("pc", "m1") <= 30
    assert 0 < c.remaining("pa", "m1") <= 30   # pa 被回头缩短
    assert c.remaining("pb", "m1") > 30        # 429 的 pb 不受突发缩档影响


def test_clear_wipes_burst_evidence(fake_clock):
    """清冷却连带清突发窗口：否则残留的旧失败会把下一个受害者误判成机器级。"""
    c = cooldown_mod
    c.mark_failed("pa", "m1", status=502)
    c.clear("pa", "m1")
    c.mark_failed("pb", "m1", status=502)
    assert c.remaining("pb", "m1") > 240


# --- 3. 冷却标记语义 ----------------------------------------------------------


def test_mark_escalates_after_repeated_failures(monkeypatch):
    """短窗口内反复失败 → 升级为长冷却（1 小时上限）。"""
    now = [1_000_000.0]
    monkeypatch.setattr(cooldown_mod.time, "time", lambda: now[0])
    first = cooldown_mod.mark_failed("p", "m")
    assert cooldown_mod.remaining("p", "m") == cooldown_mod._TARGET_COOLDOWN_S

    for _ in range(cooldown_mod._ESCALATE_HITS - 1):
        now[0] += 1  # 仍在窗口内
        last = cooldown_mod.mark_failed("p", "m")
    assert cooldown_mod.remaining("p", "m") == cooldown_mod._ESCALATE_COOLDOWN_S
    assert last > first


def test_mark_expires_and_short_mark_never_shortens_long(monkeypatch):
    """冷却到期自动恢复；且短冷却不得缩短已有的长冷却。"""
    now = [1_000_000.0]
    monkeypatch.setattr(cooldown_mod.time, "time", lambda: now[0])
    for _ in range(cooldown_mod._ESCALATE_HITS - 1):
        cooldown_mod.mark_failed("p", "m")
        now[0] += 1
    # 此刻还没升级（未达阈值）
    assert cooldown_mod.remaining("p", "m") <= cooldown_mod._TARGET_COOLDOWN_S
    # 第 _ESCALATE_HITS 次触发升级
    cooldown_mod.mark_failed("p", "m")
    escalated_at = now[0]
    assert cooldown_mod.remaining("p", "m") == cooldown_mod._ESCALATE_COOLDOWN_S
    # 一次新的短冷却不能把它缩短
    cooldown_mod.mark_failed("p", "m")
    assert cooldown_mod.remaining("p", "m") >= cooldown_mod._ESCALATE_COOLDOWN_S
    # 时间推进到解禁之后自动恢复（升级档 = 1 小时）
    now[0] = escalated_at + cooldown_mod._ESCALATE_COOLDOWN_S + 1
    assert cooldown_mod.is_marked("p", "m") is False
    assert cooldown_mod.remaining("p", "m") == 0


def test_clear_and_snapshot():
    cooldown_mod.mark_failed("pa", "m1")
    cooldown_mod.mark_failed("pb", "m2")
    assert set(cooldown_mod.snapshot()) == {"pa/m1", "pb/m2"}
    assert cooldown_mod.clear("pa", "m1") == 1
    assert set(cooldown_mod.snapshot()) == {"pb/m2"}
    assert cooldown_mod.clear("pb") == 1
    assert cooldown_mod.snapshot() == {}


def test_natural_expiry_also_clears_escalation_count(monkeypatch):
    """自然到期与显式 clear 语义一致：都算「目标恢复了」，升级计数一并归零。

    否则会出现「同样的好了又坏，走 UI 清冷却与等它自己过期，升级起点不同」的怪事。
    """
    now = [1_000_000.0]
    monkeypatch.setattr(cooldown_mod.time, "time", lambda: now[0])
    cooldown_mod.mark_failed("p", "m")
    assert cooldown_mod._hits.get(("p", "m"))

    # 推进到标记过期之后，读一次触发惰性回收
    now[0] += cooldown_mod._TARGET_COOLDOWN_S + 1
    assert cooldown_mod.remaining("p", "m") == 0
    assert ("p", "m") not in cooldown_mod._marks
    assert ("p", "m") not in cooldown_mod._hits, "自然到期应连带清掉升级计数"

    # 之后一次失败重新从第 1 次起算，而不是接着旧的计数直接升级
    now[0] += 1
    cooldown_mod.mark_failed("p", "m")
    assert cooldown_mod.remaining("p", "m") == cooldown_mod._TARGET_COOLDOWN_S
    assert len(cooldown_mod._hits[("p", "m")]) == 1


def test_clear_by_provider_also_drops_orphan_hit_counters(monkeypatch):
    """按通道清理时，只残留升级计数（标记已过期）的键也要清掉。"""
    now = [1_000_000.0]
    monkeypatch.setattr(cooldown_mod.time, "time", lambda: now[0])
    cooldown_mod.mark_failed("pa", "m1")
    # 手工制造「标记没了、计数还在」的状态（模拟清理只动 _marks 的旧行为）
    cooldown_mod._marks.pop(("pa", "m1"), None)
    assert ("pa", "m1") in cooldown_mod._hits
    cooldown_mod.clear("pa")
    assert ("pa", "m1") not in cooldown_mod._hits


# --- 4. 未配顺序时行为不变（回归护栏）----------------------------------------


def test_unset_order_preserves_prefix_routing(tmp_path, monkeypatch):
    """未配 model_order：显式前缀路由不变。"""
    client, _, a, b = _client_env(tmp_path, monkeypatch, order=None)
    r = _post(client, model="pb/m1")
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "from-pb"
    assert a.calls == []


def test_unset_order_preserves_auto_match(tmp_path, monkeypatch):
    """未配 model_order：模型 id 自动匹配不变（裸名落到拥有它的通道）。"""
    client, _, a, b = _client_env(tmp_path, monkeypatch, order=None)
    r = _post(client, model="m1")
    assert r.status_code == 200
    # pa 注册在前，自动匹配命中它；行为与改动前一致
    assert r.json()["choices"][0]["message"]["content"] == "from-pa"
    assert len(a.calls) == 1 and b.calls == []


def test_unset_order_preserves_default_provider_fallback(tmp_path, monkeypatch):
    """未配 model_order：目标模型不在任何通道目录时走 default_provider 兜底。"""
    a = _Provider("pa", ["m1"])
    b = _Provider("pb", ["m2"])
    state = _make_state({"pa": a, "pb": b}, tmp_path,
                        model_order={}, default_provider="pb")
    monkeypatch.setattr(st, "proxy_state", state)
    client = TestClient(m.app)
    r = _post(client, model="unknown-model")
    assert r.status_code == 200
    assert len(b.calls) == 1 and a.calls == []


def test_explicit_prefix_bypasses_order(tmp_path, monkeypatch):
    """显式 ``provider/model`` 前缀是明确指令，优先于候选顺序——不参与换档。

    这是**有意的**优先级（``forward_chat`` 第 1 步命中前缀即返回），但很容易被误当成
    bug 或误改：用户在界面上配了顺序，却用带前缀的 model 调，就看不到任何换档。把它钉住，
    免得将来有人「顺手」让前缀也走顺序（那会让「我只想用这个通道」失去表达方式）。
    """
    client, _, a, b = _client_env(tmp_path, monkeypatch,
                                  ["pb/m1", "pa/m1"], a_behavior="raise502")
    # 带前缀点名 pa，即使 pa 是第一档失败目标，也不换到 pb
    r = _post(client, model="pa/m1")
    assert r.status_code == 502
    assert len(a.calls) == 1 and b.calls == []
    assert cooldown_mod.is_marked("pa", "m1") is False, "前缀直连不走换档，也就不该打冷却"


def test_bare_key_drives_routing(tmp_path, monkeypatch):
    """**裸名键**要真的生效 —— 用户配的是「这个模型名走什么顺序」。

    真机现象：用户在 settings.json 写 ``"kimi-k3": [...]``，加载时被归一成
    ``codebuddy/kimi-k3``，而请求解析到别的通道时键对不上、顺序**静默不触发**。
    用户的原话是「配置一个裸名的模型名，当我指定这个模型时，它其实是按后面的
    顺序调用」——裸键必须能直接驱动换档。

    这里让 pa 排在注册序首位（裸名 m1 会先被它认领），却给**裸键**配了
    pb → pa 的顺序：第一档就该是 pb，而不是被解析结果 pa 抢先。
    """
    a = _Provider("pa", ["m1"], "ok")
    b = _Provider("pb", ["m1"], "ok")
    state = _make_state({"pa": a, "pb": b}, tmp_path,
                        model_order={"m1": ["pb/m1", "pa/m1"]})
    monkeypatch.setattr(st, "proxy_state", state)
    client = TestClient(m.app)
    r = _post(client, model="m1")
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "from-pb", (
        "裸键配的第一档是 pb，就该先用 pb——不该被自动匹配的 pa 抢先")
    assert len(b.calls) == 1 and len(a.calls) == 0


def test_bare_key_still_fails_over(tmp_path, monkeypatch):
    """裸键命中的第一档失败时，照常换到下一档（不是只认第一档就完事）。"""
    a = _Provider("pa", ["m1"], "ok")
    b = _Provider("pb", ["m1"], "raise502")
    state = _make_state({"pa": a, "pb": b}, tmp_path,
                        model_order={"m1": ["pb/m1", "pa/m1"]})
    monkeypatch.setattr(st, "proxy_state", state)
    client = TestClient(m.app)
    r = _post(client, model="m1")
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "from-pa"
    assert len(b.calls) == 1 and len(a.calls) == 1


def test_bare_key_wins_over_stale_prefixed_key(tmp_path, monkeypatch):
    """同一模型既有旧键 ``<通道>/<名>`` 又有新裸键时，**裸键必须优先**。

    真机场景：老配置留下的 ``pa/m1`` 还在文件里，用户后来按新口径加了
    ``"m1"`` 并改了顺序。若查找先命中旧键，用户改的那条就**完全没效果**——
    他会看到配置明明改了、请求却还走老路子，而且毫无提示。

    这里两个键指向不同顺序，断言生效的是裸键那份。
    """
    a = _Provider("pa", ["m1"], "ok")
    b = _Provider("pb", ["m1"], "ok")
    state = _make_state({"pa": a, "pb": b}, tmp_path,
                        model_order={"pa/m1": ["pa/m1"],
                                     "m1": ["pb/m1", "pa/m1"]})
    monkeypatch.setattr(st, "proxy_state", state)
    client = TestClient(m.app)
    r = _post(client, model="m1")
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "from-pb", (
        "裸键是用户当前维护的那份，必须盖过遗留的 <通道>/<名> 旧键")
    assert len(b.calls) == 1 and a.calls == []


def test_order_does_not_apply_to_other_models(tmp_path, monkeypatch):
    """顺序只对它自己那把键生效，不影响别的模型。

    m2 只有 pb 认识、且没有配顺序 → 走原有自动匹配落到 pb；pa 不得被牵动。
    """
    a = _Provider("pa", ["m1"], "raise502")
    b = _Provider("pb", ["m1", "m2"], "ok")
    state = _make_state({"pa": a, "pb": b}, tmp_path,
                        model_order={"pa/m1": ["pa/m1", "pb/m1"]})
    monkeypatch.setattr(st, "proxy_state", state)
    client = TestClient(m.app)
    r = _post(client, model="m2")
    assert r.status_code == 200, r.text
    assert a.calls == [], "m2 与 pa 无关，不该被牵动"
    assert len(b.calls) == 1


def test_order_target_rewrites_body_model(tmp_path, monkeypatch):
    """换档到 ``provider/other-model`` 时，上游收到的必须是**目标模型名**，不是请求别名。

    真机事故（2026-10-14）：请求 ``openrouter-4o-max`` 经 model_order 映射到
    ``traepat/gpt-5.6-sol``，诊断日志打的是解析后的目标名，但 ``body["model"]`` 从未被
    改写——provider.forward 把原始别名当上游模型名发出去，上游不认 → 挂起/超时 40~90s；
    而直连 ``gpt-5.6-sol`` 2s 返回。前缀路径在 forward_chat 里会 body={**body,"model":real}，
    唯独 model_order 路径漏了。钉住：裸名别名 → 目标模型，body["model"] 必须对齐目标。
    """
    a = _Provider("pa", ["gpt-5.6-sol"], "ok")
    state = _make_state({"pa": a}, tmp_path,
                        model_order={"openrouter-4o-max": ["pa/gpt-5.6-sol"]})
    monkeypatch.setattr(st, "proxy_state", state)
    client = TestClient(m.app)
    r = _post(client, model="openrouter-4o-max")
    assert r.status_code == 200, r.text
    assert len(a.calls) == 1
    assert a.calls[0]["model"] == "gpt-5.6-sol", (
        "上游必须收到解析后的目标模型名，而不是请求里的别名 openrouter-4o-max")


# --- 5. normalize_order 纯单元 -------------------------------------------------


@pytest.mark.parametrize("raw", [
    None, "not-a-list", 42, {"a": 1}, [1, 2, 3], [None], [""], ["  "],
    ["pa/"], ["/m1"], [{}], [[]],
])
def test_normalize_order_drops_garbage_without_raising(raw):
    """垃圾输入一律返回干净列表，绝不抛异常（配置层容错）。"""
    assert settings_mod.normalize_order(raw) == []


def test_normalize_order_dedups_keeps_order_and_normalizes_alias():
    out = settings_mod.normalize_order(
        ["zcode/glm-5.3", "workbuddy/glm-5.3", "zcode/glm-5.3", "traepat/glm-5.3"])
    assert out == ["zcode/glm-5.3", "codebuddy/glm-5.3", "traepat/glm-5.3"]


def test_normalize_order_truncates():
    raw = [f"p{i}/m" for i in range(settings_mod._MAX_ORDER_TARGETS + 5)]
    assert len(settings_mod.normalize_order(raw)) == settings_mod._MAX_ORDER_TARGETS


def test_normalize_order_key_keeps_bare_name():
    """裸模型名**原样保留**，不再按 codebuddy 兜底。

    早先归一到 ``codebuddy/<模型名>`` 是错的：用户配的是「这个模型名走什么顺序」，
    归属通道由运行时解析决定（同一个名字哪个通道先认领就归谁）。归死到 codebuddy
    会让请求解析到别的通道时**静默不触发**——用户手写 ``"kimi-k3"`` 却只对
    codebuddy 那一条路径生效，正是这个坑。
    """
    assert settings_mod.normalize_order_key("glm-5.3") == "glm-5.3"
    assert settings_mod.normalize_order_key("zcode/glm-5.3") == "zcode/glm-5.3"
    assert settings_mod.normalize_order_key("workbuddy/glm-5.3") == "codebuddy/glm-5.3"
    assert settings_mod.normalize_order_key("  glm-5.3  ") == "glm-5.3"
    assert settings_mod.normalize_order_key("") == ""


# --- 5. 「认得的通道名但没启用」必须当场报错 ------------------------------------


def test_disabled_known_channel_prefix_fails_loudly(tmp_path, monkeypatch):
    """``mimo/xxx`` 而 mimo 这次没启用 → 报「通道未启用」，不许漏给兜底通道。

    这是 ``/model`` 那个报错的根因：前缀解析只在通道**已注册**时命中，否则
    整个 ``mimo/xxx`` 原样往下走，最后落到兜底通道，被上游回一句
    ``model [mimo/xxx] service info not found``——看起来像模型名写错了，
    真正的原因（这个通道没开）完全看不见。
    """
    a = _Provider("pa", ["m1"])
    state = _make_state({"pa": a}, tmp_path, default_provider="pa")
    monkeypatch.setattr(st, "proxy_state", state)
    client = TestClient(m.app)
    r = _post(client, model="mimo/mimo-v2.6-pro")
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["error"]["type"] == "provider_disabled"
    assert "mimo" in detail["error"]["message"]
    assert "--mimo" in detail["error"]["message"], "要告诉用户怎么开"
    assert a.calls == [], "不许把整个带前缀的名字漏给兜底通道"


def test_disabled_traepat_hint_names_real_switch(tmp_path, monkeypatch):
    """``traepat`` 没启用时的提示必须指向**真实存在**的开关。

    ``traepat`` 没有自己的 ``--traepat``：它挂在 ``--trae`` 分支里，由
    ``pat_enabled()``（``TRAE_PAT_BEARER`` / ``_PROFILES`` 有配）决定注册。
    早期实现按 ``--{prefix}`` / ``{PREFIX}_ENABLED=1`` 拼短语，对 traepat 拼出
    一个不存在的参数——用户照着敲只会落到 usage。
    """
    a = _Provider("pa", ["m1"])
    state = _make_state({"pa": a}, tmp_path, default_provider="pa")
    monkeypatch.setattr(st, "proxy_state", state)
    client = TestClient(m.app)
    r = _post(client, model="traepat/glm-5.3")
    assert r.status_code == 400, r.text
    msg = r.json()["detail"]["error"]["message"]
    assert "--traepat" not in msg, "这个参数根本不存在，不能推荐"
    assert "TRAE_PAT_BEARER" in msg, "要指向真正的启用方式"
    assert "--trae" in msg
    assert a.calls == []


def test_unknown_non_channel_prefix_still_falls_back(tmp_path, monkeypatch):
    """但 ``openrouter/xxx`` 这类**不是本项目的通道名** → 维持原有兜底行为。

    ``provider/model`` 形态的 id 不只本项目通道在用（用户可能拿它当普通模型名
    转发给某个上游），拦下来会砸掉既有用法。只有「认得出的通道名」才报错。
    """
    a = _Provider("pa", ["m1"])
    state = _make_state({"pa": a}, tmp_path, default_provider="pa")
    monkeypatch.setattr(st, "proxy_state", state)
    client = TestClient(m.app)
    r = _post(client, model="openrouter/some-model")
    assert r.status_code == 200, r.text
    assert len(a.calls) == 1, "非通道前缀照旧走兜底"
    assert a.calls[0]["model"] == "openrouter/some-model", "模型名原样透传"


def test_enabled_channel_prefix_still_routes(tmp_path, monkeypatch):
    """已启用的通道带前缀调用不受影响（回归护栏）。"""
    a = _Provider("pa", ["m1"])
    state = _make_state({"pa": a}, tmp_path)
    monkeypatch.setattr(st, "proxy_state", state)
    client = TestClient(m.app)
    r = _post(client, model="pa/m1")
    assert r.status_code == 200, r.text
    assert a.calls[0]["model"] == "m1", "前缀要被剥掉再转发"
