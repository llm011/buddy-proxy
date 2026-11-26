from unittest import mock

from buddy_proxy.core.metrics import MetricsCollector
from buddy_proxy.core.metrics_protocol import ACCOUNT_META
from buddy_proxy.trae import native_tools
from buddy_proxy.trae.telemetry import finish_attempt, mark_final_success, start_attempt
from buddy_proxy.trae.transport import _send_trae_work_chat


def test_attempts_are_physical_and_only_success_sets_final_ids():
    meta = {"account": "u1"}
    token = ACCOUNT_META.set(meta)
    try:
        first = start_attempt(transport="native", request_id="r", session_id="s",
                              model="glm-5.4", max_mode=True, endpoint="chat")
        finish_attempt(first, outcome="fallback")
        second = start_attempt(transport="text", request_id="r2", session_id="s2",
                               model="glm-5.4", max_mode=True, endpoint="chat")
        finish_attempt(second, outcome="response")
        mark_final_success(second)
    finally:
        ACCOUNT_META.reset(token)

    assert [item["outcome"] for item in meta["trae_attempts"]] == ["fallback", "response"]
    assert meta["trae_final_transport"] == "text"
    assert meta["trae_request_id"] == "r2"
    assert meta["trae_session_id"] == "s2"
    assert all("started" not in item for item in meta["trae_attempts"])
    assert all("duration_ms" in item for item in meta["trae_attempts"])


def test_telemetry_without_holder_is_noop():
    token = ACCOUNT_META.set(None)
    try:
        attempt = start_attempt(transport="text", request_id="r", session_id="s",
                                model="glm-5.4", max_mode=False)
        finish_attempt(attempt, outcome="response")
        mark_final_success(attempt)
    finally:
        ACCOUNT_META.reset(token)
    assert attempt is None


def test_work_http_200_4001_is_not_final_success():
    """HTTP 成功只代表网关接受；4001 SSE 正文仍须让上层回退。"""
    meta = {"account": "u1"}
    response = mock.MagicMock()
    response.read.return_value = b'data: {"code": 4001}\n\n'
    response.status = 200
    cm = mock.MagicMock()
    cm.__enter__.return_value = response
    token = ACCOUNT_META.set(meta)
    try:
        with mock.patch("urllib.request.urlopen", return_value=cm):
            raw = _send_trae_work_chat(
                [{"role": "user", "content": "hi"}], "glm-5.4", False,
                {"access_token": "test", "uid": "u1"}, max_mode=True,
            )
    finally:
        ACCOUNT_META.reset(token)

    assert '"code": 4001' in raw
    assert [item["outcome"] for item in meta["trae_attempts"]] == ["fallback"]
    assert "trae_final_transport" not in meta


def test_native_empty_stream_never_sets_final_success(monkeypatch):
    """空 EOF 会被 provider 拒绝为 502，不能提前污染为最终成功。"""
    class Response:
        def read1(self, _size):
            return b""

        def close(self):
            pass

    monkeypatch.setattr(native_tools, "_load_work_cred", lambda: {"access_token": "test"})
    monkeypatch.setattr(native_tools, "resolve_trae_region", lambda _region: mock.Mock(
        key="global", chat_base="https://offline.invalid"))
    monkeypatch.setattr(native_tools, "map_model_for", lambda _region, model: model)
    monkeypatch.setattr(native_tools, "_work_headers", lambda _work: {})
    monkeypatch.setattr(native_tools.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(native_tools, "_WORK_CHAT_MAX_ATTEMPTS", 1)
    meta: dict[str, object] = {"account": "u1"}
    token = ACCOUNT_META.set(meta)
    try:
        assert list(native_tools._stream_native_chat_events([], "glm-5.4", None)) == []
    finally:
        ACCOUNT_META.reset(token)

    assert [item["outcome"] for item in meta["trae_attempts"]] == ["upstream_error"]
    assert "trae_final_transport" not in meta
    assert "trae_request_id" not in meta
    assert "trae_session_id" not in meta


def test_metrics_attempts_are_json_safe_and_bounded(tmp_path):
    """外部调用者传入非 JSON 值也不能让 _append_log 静默吞掉整条指标。"""
    path = tmp_path / "metrics.jsonl"
    metrics = MetricsCollector(path)
    metrics.record(
        provider="trae", model="glm-5.4",
        trae_attempts=[{
            "endpoint": object(), "request_id": "x" * 200,
            "duration_ms": object(), "max_mode": "yes", "secret": "do-not-persist",
        }],
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    import json
    record = json.loads(lines[0])
    attempt = record["trae_attempts"][0]
    assert set(attempt) == {
        "request_id", "session_id", "transport", "model", "max_mode",
        "account", "endpoint", "outcome", "duration_ms",
    }
    assert len(attempt["request_id"]) == 80
    assert attempt["duration_ms"] == 0
    assert attempt["max_mode"] is True
