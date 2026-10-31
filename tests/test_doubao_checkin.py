"""豆包签到（CDP 通道）单测。

锁定四件事：

1. **不猜**：候选端点全部落空（401 / 404 / 无名响应）时返回 ``None``，
   provider 层转成 ``query_failed``——界面显示「状态未知」，绝不画成
   「未签到 + 可点的立即打卡」。
2. **只认业务成功**：``code != 0``、非 200、非 JSON 一律算没查到；形状不认识
   的成功响应也当没查到（记日志等上游真机确认）。
3. **只对命中端点领**：``claim_checkin`` 必须拿到 ``fetch_checkin_status``
   返回的命中路径才发 POST，不对没验证过的端点盲打。
4. **请求形状**：一律绝对 URL ``https://www.doubao.com`` + 对话通道同款查询
   参数（相对路径落到 chrome:// 种子页 origin 只会拿回 SPA 首页）；
   登录态先探服务端 401，未登录时不再白打候选端点。

全部用假 CDP 客户端隔离：不起 App、不连端口。
"""
from __future__ import annotations

import json

import pytest

from buddy_proxy.doubao import checkin as doubao_checkin
from buddy_proxy.doubao.provider import DoubaoProvider


class FakeClient:
    """按路径回放的假 CDP 客户端（``evaluate`` 只看 JS 里嵌入的 path）。"""

    def __init__(self, responses: dict[str, tuple[int, object]], *, ready: bool = True):
        self.responses = responses
        self.ready = ready
        self.calls: list[str] = []
        self.methods: list[str] = []
        self.exprs: list[str] = []
        self.hints: list[str] = []

    @property
    def is_ready(self) -> bool:
        return self.ready

    async def wait_for_login(self, timeout: float = 0) -> bool:  # 供 _run_checkin 调用
        return self.ready

    def login_state_sync(self, timeout: float = 0) -> bool | None:
        """真客户端的三态登录探针；这里复用 evaluate 回放。"""
        return doubao_checkin.login_state(self)

    def _build_query_params(self) -> dict[str, str]:
        return {"aid": "1044603", "web_id": "w1"}

    def evaluate(self, expr: str, timeout: float = 20.0):
        if "document.scripts" in expr:  # 接口线索扫描
            self.calls.append("discover")
            self.methods.append("GET")
            return self.hints
        path = None
        for candidate in [doubao_checkin._LOGIN_PROBE] + list(
                doubao_checkin._CANDIDATES) + list(doubao_checkin._CLAIM_SUFFIX.values()):
            if f"{candidate}?" in expr:  # 真实请求带查询参数，按「路径?」匹配
                path = candidate
                break
        self.calls.append(path or "?")
        method = "POST" if '"POST"' in expr else "GET"
        self.methods.append(method)
        self.exprs.append(expr)
        status, body = self.responses.get(f"{method} {path}", (404, "<html>404 page not found</html>"))
        text = body if isinstance(body, str) else json.dumps(body)
        return {"http": status, "body": text, "error": ""}


#: 默认登录探针回「已登录」：空 POST 回业务错误码（不是 401）。
_LOGGED_IN = {"POST /api/chat/completion/chat": (200, {"code": 10001, "msg": "param missing"})}


@pytest.fixture(autouse=True)
def _reset_hit_path():
    doubao_checkin._HIT_PATH.clear()
    yield
    doubao_checkin._HIT_PATH.clear()


def _signed_payload():
    return {"code": 0, "data": {
        "checked_in": False,
        "sign_days": ["2026-10-28", "2026-10-29"],
    }}


def test_status_uses_first_parseable_candidate_and_remembers_it():
    client = FakeClient({
        **_LOGGED_IN,
        "GET /api/activity/check_in/status": (401, ""),
        "GET /api/activity/checkin/info": (200, {"code": 710012001, "msg": "login invalid"}),
        "GET /api/activity/task/list": (200, {"code": 0, "data": {}}),  # 形状不认识
        "GET /api/activity/sign/info": (200, _signed_payload()),
    })
    status, path = doubao_checkin.fetch_checkin_status(client)
    assert status is not None
    assert path == "/api/activity/sign/info"
    assert status["checked_in"] is False
    assert status["claimable"] is True
    assert isinstance(status["streak_days"], int)  # 日期相对今天，具体值随运行日变化
    assert status["next_ts_source"] == "inferred"

    # 记下命中端点：下一轮先打它（第 0 条永远是登录探针）
    client.calls.clear()
    doubao_checkin.fetch_checkin_status(client)
    assert client.calls[1] == "/api/activity/sign/info"


def test_status_returns_none_when_nothing_parses():
    client = FakeClient(dict(_LOGGED_IN))  # 已登录，但候选全部 404
    assert doubao_checkin.fetch_checkin_status(client) == (None, "")
    # 全落空时扫一遍页面 JS 找接口线索（只记日志，不发请求）
    assert "discover" in client.calls


def test_status_skips_candidates_when_server_says_logged_out():
    """服务端 401 = 未登录：不再白打候选端点，也不扫 JS。"""
    client = FakeClient({"POST /api/chat/completion/chat": (401, "")})
    assert doubao_checkin.fetch_checkin_status(client) == (None, "")
    assert client.calls == ["/api/chat/completion/chat"]


def test_requests_use_absolute_url_and_query_params():
    """相对路径会落到 chrome:// 种子页 origin（拿回 SPA 首页），必须绝对 URL。"""
    client = FakeClient({**_LOGGED_IN,
                         "GET /api/activity/check_in/status": (200, _signed_payload())})
    doubao_checkin.fetch_checkin_status(client)
    request = next(e for e in client.exprs if "check_in/status" in e)
    assert "https://www.doubao.com/api/activity/check_in/status?aid=1044603&web_id=w1" in request


def test_discover_paths_logs_only_matching_hints():
    client = FakeClient(dict(_LOGGED_IN))
    client.hints = ["/api/activity/sign/info", "/api/unrelated/thing"]
    assert doubao_checkin.discover_paths(client) == ["/api/activity/sign/info"]


def test_status_rejects_business_error_code():
    """``code != 0``（登录过期）不能当状态——否则会误报「未签到」。"""
    client = FakeClient({
        **_LOGGED_IN,
        **{p: (200, {"code": 710012001, "message": "login invalid"})
           for p in doubao_checkin._CANDIDATES},
    })
    assert doubao_checkin.fetch_checkin_status(client) == (None, "")


def test_claim_only_after_status_hit():
    payload = {"code": 0, "data": {"signed": False}}
    client = FakeClient({
        **_LOGGED_IN,
        "GET /api/activity/check_in/status": (200, payload),
        "POST /api/activity/check_in": (200, {"code": 0, "data": {}}),
    })
    status, path = doubao_checkin.fetch_checkin_status(client)
    assert path == "/api/activity/check_in/status"
    # 领完再查：换成已签
    client.responses["GET /api/activity/check_in/status"] = (
        200, {"code": 0, "data": {"signed": True}})
    result = doubao_checkin.claim_checkin(client, path)
    assert result is not None and result["checked_in"] is True
    assert "/api/activity/check_in" in client.calls
    assert "POST" in client.methods


def test_claim_refuses_unverified_path():
    client = FakeClient({})
    assert doubao_checkin.claim_checkin(client, "/api/nope") is None
    assert client.calls == []


def test_parse_status_recognises_common_shapes():
    assert doubao_checkin._parse_status({"code": 0, "data": {"is_signed": 1}})["checked_in"] is True
    assert doubao_checkin._parse_status({"data": {"status": "signed"}})["checked_in"] is True
    assert doubao_checkin._parse_status({"result": {"sign_status": 0}}) is None  # 见的少，不瞎认
    assert doubao_checkin._parse_status({"data": {}}) is None
    assert doubao_checkin._parse_status({"data": None}) is None


# --- provider 层 -----------------------------------------------------------


def _provider_with(monkeypatch, responses, *, ready=True):
    p = DoubaoProvider()
    client = FakeClient({**_LOGGED_IN, **responses}, ready=ready)
    p._client = client

    async def _started():
        p._started = True
    monkeypatch.setattr(p, "_ensure_started", _started)
    monkeypatch.setattr(p, "_started", True, raising=False)
    return p, client


def test_provider_declares_checkin():
    assert DoubaoProvider.supports_checkin is True


def test_provider_status_is_query_failed_not_unsigned(monkeypatch):
    p, _ = _provider_with(monkeypatch, {})
    status = p.checkin_status()
    assert isinstance(status, dict)
    assert status["query_failed"] is True
    assert status["claimable"] is False
    assert status["checked_in"] is False
    # 已登录但接口没认出：提示说「接口未能识别」，别误报成没登录
    assert "未登录" not in status["message"]


def test_provider_reports_logged_out(monkeypatch):
    p, _ = _provider_with(monkeypatch, {
        "POST /api/chat/completion/chat": (401, ""),
    })
    status = p.checkin_status()
    assert status["query_failed"] is True
    assert "重新扫码登录" in status["message"]


def test_status_probe_failure_is_not_reported_as_logged_in():
    """探针本身失败（HTTP 0）时三态是 None，不能折成「已登录」误导用户。"""
    client = FakeClient({"POST /api/chat/completion/chat": (0, "")})
    assert doubao_checkin.login_state(client) is None


def test_provider_status_ok(monkeypatch):
    p, _ = _provider_with(monkeypatch, {
        "GET /api/activity/check_in/status": (200, {"code": 0, "data": {"signed": True}}),
    })
    status = p.checkin_status()
    assert status["checked_in"] is True and "query_failed" not in status


def test_provider_claim_refuses_when_already_signed(monkeypatch):
    p, client = _provider_with(monkeypatch, {
        "GET /api/activity/check_in/status": (200, {"code": 0, "data": {"signed": True}}),
    })
    state = p.checkin_claim()
    assert state is not None and "已签到" in state.get("message", "")
    assert "POST /api/chat/completion/chat" not in client.calls
