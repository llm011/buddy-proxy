"""豆包 campaign v3 签到单测；全部使用假 CDP，不起 App、不连端口。"""
from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from buddy_proxy.doubao import checkin as doubao_checkin
from buddy_proxy.doubao.cdp_client import CDPDoubaoClient
from buddy_proxy.doubao.provider import DoubaoProvider


class FakeSocket:
    def __init__(self):
        self.commands: list[tuple[str, dict]] = []
        self.closed = False

    def cmd(self, method, params, timeout=20.0):
        self.commands.append((method, params))
        expression = params.get("expression", "")
        if "fetch(" in expression:
            return {"result": {"result": {"value": {"http": 500, "body": ""}}}}
        if expression == "location.href":
            return {"result": {"result": {"value": doubao_checkin._PAGE_URL}}}
        if expression == "document.readyState":
            return {"result": {"result": {"value": "complete"}}}
        if "performance.getEntriesByType" in expression:
            # 模拟页面 JS 已执行 delete('msToken') / delete('a_bogus') 后的结果。
            assert "url.searchParams.delete('msToken')" in expression
            assert "url.searchParams.delete('a_bogus')" in expression
            return {"result": {"result": {"value": "?aid=497858"}}}
        return {"result": {"result": {"value": None}}}

    def close(self):
        self.closed = True


class FakeClient:
    """按页面 fetch 的真实 URL + POST body 回放，另记录共享 target 命令。"""

    def __init__(self, responses=None, *, ready=True, existing_page=True):
        self.responses = responses or {}
        self.ready = ready
        self.existing_page = existing_page
        self.port = 9223
        self._ws_lock = threading.Lock()
        self._ws = self
        self._login_state: bool | None = None
        self._sockets: list[FakeSocket] = []
        self.created_targets = 0
        self.closed_targets: list[str] = []
        self.commands: list[str] = []
        self.requests: list[tuple[str, dict]] = []
        self.target = {
            "id": "user-signin", "type": "webview", "url": doubao_checkin._PAGE_URL,
            "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/page/user-signin",
        }

    @property
    def is_ready(self):
        return self.ready

    def login_state_sync(self, timeout=15.0):
        response = self.responses.get("POST /chat/completion", (0, ""))
        http, payload = response
        body = payload if isinstance(payload, str) else json.dumps(payload)
        data = None
        try:
            data = json.loads(body) if body else None
        except ValueError:
            pass
        return doubao_checkin.login_state_from_probe(http, data, body)

    def _fetch_targets(self):
        targets = [self.target] if self.existing_page else []
        targets.extend({"id": f"new-{i}", "type": "page",
                        "url": doubao_checkin._PAGE_URL,
                        "webSocketDebuggerUrl": f"ws://127.0.0.1:9223/devtools/page/new-{i}"}
                       for i in range(1, self.created_targets + 1))
        return targets

    def cmd(self, method, params, timeout=20.0):
        self.commands.append(method)
        if method == "Target.createTarget":
            self.created_targets += 1
            return {"result": {"targetId": f"new-{self.created_targets}"}}
        if method == "Target.closeTarget":
            self.closed_targets.append(params["targetId"])
            return {"result": {"success": True}}
        raise AssertionError(f"unexpected shared CDP command: {method}")


@pytest.fixture(autouse=True)
def _fake_page_websocket(monkeypatch):
    def _connect(_host, _port, _path):
        socket = FakeSocket()
        _fake_page_websocket.sockets.append(socket)
        return socket
    _fake_page_websocket.sockets = []
    monkeypatch.setattr(doubao_checkin, "_WS", _connect)


def _payload(*, checked=True, task_status=3, **overrides):
    daily = {
        "checked_in_today": checked, "continuous_days": 6, "total_days": 9,
        "last_checkin_date": "2026-11-01",
        "reward": {"reward_amount": 200, "reward_type": "quota_grant"},
        "reward_status": 3 if checked else 0,
    }
    daily.update(overrides)
    return {"code": 0, "data": {
        "campaign_code": "pc_incentive_task_mvp_v3",
        "example_cases": ["x" * 30000],
        "tasks": [
            {"campaign_code": "daily_signin", "campaign_group_code": "pc_incentive_task_mvp_v3",
             "task_kind": "daily_signin", "task_status": task_status,
             "reward_mode": 2, "daily_checkin": daily},
            {"campaign_code": "select_work_fields", "task_kind": "chat_activity",
             "task_status": 1},
        ],
    }}


def _login(*, http=200, code=710020202):
    return (http, {"code": code, "message": "common invalid param"})


def _responses(**kwargs):
    return {
        "POST /chat/completion": _login(),
        "POST /alice/ug/campaign/v3/detail/": (200, _payload(**kwargs)),
    }


class FakePage:
    """fetch 层假页面；target 隔离由下面的真实 CheckinPage 测试覆盖。"""

    def __init__(self, client, **kwargs):
        self.client = client

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def request(self, path, body, timeout, **kwargs):
        self.client.requests.append((path, body))
        status, payload = self.client.responses.get(
            f"POST {path}", (404, "<html>Page not found</html>"))
        return status, payload, payload if isinstance(payload, str) else json.dumps(payload)


@pytest.fixture
def fake_page(monkeypatch):
    monkeypatch.setattr(doubao_checkin, "CheckinPage", FakePage)


def test_status_uses_real_campaign_and_normalises_reward(fake_page):
    client = FakeClient(_responses(checked=True))
    status, path = doubao_checkin.fetch_checkin_status(client)
    assert path == doubao_checkin._DETAIL_PATH
    assert status["checked_in"] is True and status["claimable"] is False
    assert status["streak_days"] == 6 and status["total_times"] == 9
    assert status["daily_credit"] == 200
    assert client.requests[-1] == (
        doubao_checkin._DETAIL_PATH, {"campaign_group_code": "pc_incentive_task_mvp_v3"})


def test_status_never_guesses_unknown_or_other_task(fake_page):
    wrong_group = _payload()
    wrong_group["data"]["tasks"][0]["campaign_group_code"] = "other"
    for payload in (
        {"code": 0, "data": {}},
        {"code": 0, "data": {"campaign_code": "pc_incentive_task_mvp_v3", "tasks": []}},
        {"code": 0, "data": {"campaign_code": "pc_incentive_task_mvp_v3", "tasks": [
            {"task_kind": "daily_signin", "campaign_code": "daily_signin",
             "campaign_group_code": "pc_incentive_task_mvp_v3", "daily_checkin": {}},
            {"task_kind": "daily_signin", "campaign_code": "daily_signin",
             "campaign_group_code": "pc_incentive_task_mvp_v3", "daily_checkin": {}},
        ]}},
        wrong_group,
        {"code": 0, "data": {"campaign_code": "other", "tasks": _payload()["data"]["tasks"]}},
    ):
        client = FakeClient({"POST /chat/completion": _login(),
                             "POST /alice/ug/campaign/v3/detail/": (200, payload)})
        assert doubao_checkin.fetch_checkin_status(client) == (None, "")


def test_status_normalises_missing_counters_without_guessing_checkin(fake_page):
    client = FakeClient(_responses(checked=True, total_days=None, continuous_days=None))
    status, _ = doubao_checkin.fetch_checkin_status(client)
    assert status["checked_in"] is True
    assert status["total_times"] == 0 and status["streak_days"] == 0


@pytest.mark.parametrize("task_status", [1, 2, 4])
def test_only_unrewarded_daily_task_is_claimable(fake_page, task_status):
    client = FakeClient(_responses(checked=False, task_status=task_status))
    status, _ = doubao_checkin.fetch_checkin_status(client)
    assert status["checked_in"] is False and status["claimable"] is True
    client.responses["POST /alice/ug/campaign/v3/detail/"] = (
        200, _payload(checked=False, task_status=3))
    status, _ = doubao_checkin.fetch_checkin_status(client)
    # 即使 checked_in_today 与 task_status 矛盾，也不开放领取。
    assert status["claimable"] is False


def test_status_ignores_boolean_counters(fake_page):
    client = FakeClient(_responses(checked=True, total_days=True, continuous_days=False))
    status, _ = doubao_checkin.fetch_checkin_status(client)
    assert status["checked_in"] is True
    assert status["total_times"] == 0 and status["streak_days"] == 0


def test_login_expired_or_unknown_never_reads_status(fake_page):
    for response in ((401, ""), (200, {"code": 710012001}), (0, ""), (200, "<html>SPA</html>")):
        client = FakeClient({"POST /chat/completion": response})
        assert doubao_checkin.fetch_checkin_status(client) == (None, "")
        assert [path for path, _ in client.requests] == ["/chat/completion"]


@pytest.mark.parametrize("http, body, expected", [
    (401, None, False), (200, {"code": 710012001}, False),
    (200, {"code": 710020202}, True), (0, None, None),
    (404, "<html>Page not found</html>", None), (500, None, None),
])
def test_login_probe_only_concludes_on_clear_evidence(http, body, expected):
    assert doubao_checkin.login_state_from_probe(http, body) is expected


def test_login_probe_html_is_unknown_not_logged_in():
    assert doubao_checkin.login_state_from_probe(
        200, None, "<!DOCTYPE html><html><body>app</body></html>") is None
    assert doubao_checkin.login_state_from_probe(200, None, "not json") is None
    assert doubao_checkin.login_state_from_probe(200, []) is None
    assert doubao_checkin.login_state_from_probe(401, None, "<html>unauth</html>") is False


def test_login_probe_reads_real_sse_business_code():
    body = ('id: 0\nevent: SSE_HEARTBEAT\ndata: {}\n\n'
            'id: 0\nevent: STREAM_ERROR\n'
            'data: {"error_code":710020202,"error_msg":"common invalid param"}\n\n')
    assert doubao_checkin.login_state_from_probe(200, None, body) is True
    expired = body.replace("710020202", "710012001")
    assert doubao_checkin.login_state_from_probe(200, None, expired) is False


def test_cdp_login_probe_uses_real_chat_route(monkeypatch):
    client = CDPDoubaoClient.__new__(CDPDoubaoClient)
    client._ready = True
    client._web_id = "w1"
    monkeypatch.setattr(client, "_is_app_internal_page", lambda: True)
    seen = []
    for http, body, expected in [(200, '{"code":710020202}', True), (401, "", False),
                                 (404, "<html>nf</html>", None), (0, "boom", None)]:
        probe = {"http": http, "body": body}
        def _evaluate(expr, timeout=0, _p=probe):
            seen.append(expr)
            return _p
        monkeypatch.setattr(client, "_evaluate", _evaluate)
        assert client.login_state_sync() is expected
    assert all("/chat/completion?" in expr for expr in seen)
    assert all("/api/chat/completion/chat" not in expr for expr in seen)


def test_claim_uses_daily_checkin_and_rechecks(fake_page, monkeypatch):
    client = FakeClient(_responses(checked=False, task_status=1))
    client.responses["POST /alice/ug/campaign/landing/checkin"] = (
        200, {"code": 0, "data": {"checked_in": True, "reward_status": 3,
                                  "continuous_days": 7, "idempotent": False}})
    calls = 0
    original = FakePage.request
    def _request(page, path, body, timeout, **kwargs):
        nonlocal calls
        if path == doubao_checkin._DETAIL_PATH:
            calls += 1
            if calls == 2:
                page.client.responses[f"POST {path}"] = (200, _payload(checked=True))
        return original(page, path, body, timeout, **kwargs)
    monkeypatch.setattr(FakePage, "request", _request)
    result = doubao_checkin.claim_checkin(client, doubao_checkin._DETAIL_PATH)
    assert result["checked_in"] is True and result["claimable"] is False
    assert client.requests[-2] == (
        "/alice/ug/campaign/landing/checkin", {"scene": "pc_incentive_task_mvp_v3"})
    assert all("campaign_item_code" not in body and "task_id" not in body
               for _, body in client.requests)


def test_claim_warns_when_accepted_but_recheck_is_unconfirmed(fake_page, monkeypatch, caplog):
    client = FakeClient(_responses(checked=False, task_status=1))
    client.responses["POST /alice/ug/campaign/landing/checkin"] = (
        200, {"code": 0, "data": {"checked_in": True}})
    original = FakePage.request
    def _request(page, path, body, timeout, **kwargs):
        if path == doubao_checkin._DETAIL_PATH and any(
                request_path == doubao_checkin._CLAIM_PATH
                for request_path, _ in page.client.requests):
            return 0, None, ""
        return original(page, path, body, timeout, **kwargs)
    monkeypatch.setattr(FakePage, "request", _request)
    with caplog.at_level(logging.WARNING):
        assert doubao_checkin.claim_checkin(client, doubao_checkin._DETAIL_PATH) is None
    assert "accepted but recheck unconfirmed" in caplog.text


def test_claim_already_done_does_not_post(fake_page):
    client = FakeClient(_responses(checked=True))
    result = doubao_checkin.claim_checkin(client, doubao_checkin._DETAIL_PATH)
    assert result["checked_in"] is True
    assert [path for path, _ in client.requests] == [doubao_checkin._DETAIL_PATH]


def test_claim_refuses_unverified_path_or_unconfirmed_result(fake_page):
    client = FakeClient()
    assert doubao_checkin.claim_checkin(client, "/api/nope") is None
    assert client.requests == []
    client = FakeClient(_responses(checked=False, task_status=1))
    client.responses["POST /alice/ug/campaign/landing/checkin"] = (200, {"code": 0, "data": {}})
    assert doubao_checkin.claim_checkin(client, doubao_checkin._DETAIL_PATH) is None


def test_detail_projection_handles_large_example_cases():
    client = FakeClient()
    with doubao_checkin.CheckinPage(client) as page:
        expr = None
        original = page._ws.cmd
        def _capture(method, params, timeout=20.0):
            nonlocal expr
            if "fetch(" in params.get("expression", ""):
                expr = params["expression"]
            return original(method, params, timeout)
        page._ws.cmd = _capture
        page.request(doubao_checkin._DETAIL_PATH, {"campaign_group_code": "x"}, 1,
                     daily_task_only=True)
    assert "data.data.tasks.find" in expr
    assert "t.task_kind === 'daily_signin'" in expr
    assert "example_cases" not in expr.split("return {http:")[-1]


def test_reuses_user_webview_without_touching_chat_target():
    client = FakeClient(_responses())
    with doubao_checkin.CheckinPage(client) as page:
        assert page.query == "?aid=497858"  # 借页面业务参数，但剥掉旧签名
        assert page.evaluate("location.href") == doubao_checkin._PAGE_URL
    assert client.commands == []
    assert client.created_targets == 0 and client.closed_targets == []
    assert client._ws is client  # 共享聊天 socket 没被替换或关闭


def test_creates_and_closes_only_its_background_page():
    client = FakeClient()
    # 已有页面是聊天页，不是签到页：新建独立后台页，最后只关闭 new-1。
    client.target["url"] = "https://www.doubao.com/chat/"
    with doubao_checkin.CheckinPage(client):
        pass
    assert client.commands == ["Target.createTarget", "Target.closeTarget"]
    assert client.closed_targets == ["new-1"]
    assert client.target["id"] == "user-signin"  # 用户聊天页 id 仅作标识，不被关闭


def test_open_failure_closes_created_page_and_preserves_shared_socket(monkeypatch):
    client = FakeClient()
    client.target["url"] = "https://www.doubao.com/chat/"
    original = FakeSocket.cmd
    def _wrong_href(socket, method, params, timeout=20.0):
        if params.get("expression") == "location.href":
            return {"result": {"result": {"value": "about:blank"}}}
        return original(socket, method, params, timeout)
    monkeypatch.setattr(FakeSocket, "cmd", _wrong_href)
    ticks = iter(range(100))
    monkeypatch.setattr(doubao_checkin.time, "monotonic", lambda: next(ticks, 100.0))
    monkeypatch.setattr(doubao_checkin.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="did not finish loading"):
        with doubao_checkin.CheckinPage(client):
            pass
    assert client.closed_targets == ["new-1"]
    assert client._ws is client


def _tracked_page(monkeypatch):
    """保留真实页面锁和清理，只替换页面打开与 HTTP 请求。"""
    active = 0
    maximum = [0]
    state_lock = threading.Lock()

    class TrackedPage(doubao_checkin.CheckinPage):
        def __init__(self, client, **kwargs):
            super().__init__(client, **kwargs)
            self.client = client
            self.requests = client.requests
            self.responses = client.responses

        def _open(self):
            nonlocal active
            with state_lock:
                active += 1
                maximum[0] = max(maximum[0], active)
            time.sleep(0.05)

        def __exit__(self, *args):
            nonlocal active
            try:
                time.sleep(0.05)
                return super().__exit__(*args)
            finally:
                with state_lock:
                    active -= 1

        def request(self, path, body, timeout, **kwargs):
            return FakePage.request(self, path, body, timeout, **kwargs)

    monkeypatch.setattr(doubao_checkin, "CheckinPage", TrackedPage)
    return maximum


def _join_threads(threads):
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_status_calls_do_not_overlap_on_a_shared_checkin_page(monkeypatch):
    client = FakeClient(_responses())
    maximum = _tracked_page(monkeypatch)
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        doubao_checkin.fetch_checkin_status(client))) for _ in range(2)]
    _join_threads(threads)
    assert len(results) == 2 and all(status is not None for status, _ in results)
    assert maximum[0] == 1


def test_claim_call_serialises_status_preflight_and_claim(monkeypatch):
    client = FakeClient(_responses(checked=True))
    maximum = _tracked_page(monkeypatch)
    threads = [threading.Thread(
        target=lambda: doubao_checkin.claim_checkin(client, doubao_checkin._DETAIL_PATH))
        for _ in range(2)]
    _join_threads(threads)
    assert maximum[0] == 1


def _provider_with(monkeypatch, responses):
    provider = DoubaoProvider()
    client = FakeClient(responses)
    provider._client = client
    async def _started():
        provider._started = True
    monkeypatch.setattr(provider, "_ensure_started", _started)
    return provider, client


def test_provider_status_ok(monkeypatch, fake_page):
    provider, client = _provider_with(monkeypatch, _responses(checked=True))
    status = provider.checkin_status()
    assert status["checked_in"] is True and "query_failed" not in status
    assert status["daily_credit"] == 200
    assert client._login_state is True


def test_provider_status_is_query_failed_not_unsigned(monkeypatch, fake_page):
    provider, _ = _provider_with(monkeypatch, {})
    status = provider.checkin_status()
    assert status["query_failed"] is True and status["claimable"] is False
    assert status["checked_in"] is False


def test_provider_claim_refuses_when_already_signed(monkeypatch, fake_page):
    provider, client = _provider_with(monkeypatch, _responses(checked=True))
    state = provider.checkin_claim()
    assert "已签到" in state["message"]
    assert all(path != doubao_checkin._CLAIM_PATH for path, _ in client.requests)
    assert [path for path, _ in client.requests] == [
        doubao_checkin._LOGIN_PROBE, doubao_checkin._DETAIL_PATH]


def test_provider_checkin_uses_only_the_isolated_page_probe(monkeypatch, fake_page):
    provider, client = _provider_with(monkeypatch, _responses(checked=True))
    monkeypatch.setattr(client, "login_state_sync",
                        lambda *args, **kwargs: pytest.fail("聊天页不应重复探测"))
    assert provider.checkin_status()["checked_in"] is True


def test_provider_failure_message_uses_probe_result(monkeypatch, fake_page):
    provider, client = _provider_with(monkeypatch, {"POST /chat/completion": (401, "")})
    assert provider.checkin_status()["message"].startswith("豆包未登录")
    assert client._login_state is False
    provider, client = _provider_with(monkeypatch, {"POST /chat/completion": (0, "")})
    assert "登录态未确认" in provider.checkin_status()["message"]
    assert client._login_state is None
