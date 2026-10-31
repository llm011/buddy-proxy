"""豆包签到（CDP 通道）单测。

锁定三件事：

1. **不猜**：候选端点全部落空（401 / 404 / 无名响应）时返回 ``None``，
   provider 层转成 ``query_failed``——界面显示「状态未知」，绝不画成
   「未签到 + 可点的立即打卡」。
2. **只认业务成功**：``code != 0``、非 200、非 JSON 一律算没查到；形状不认识
   的成功响应也当没查到（记日志等上游真机确认）。
3. **只对命中端点领**：``claim_checkin`` 必须拿到 ``fetch_checkin_status``
   返回的命中路径才发 POST，不对没验证过的端点盲打。

全部用假 CDP 客户端隔离：不起 App、不连端口。
"""
from __future__ import annotations

import json
import re

import pytest

from buddy_proxy.doubao import checkin as doubao_checkin
from buddy_proxy.doubao.cdp_client import CDPDoubaoClient
from buddy_proxy.doubao.provider import DoubaoProvider


class FakeClient:
    """按「方法 + 路径」回放的假 CDP 客户端（只看 JS 里嵌入的绝对 URL）。"""

    def __init__(self, responses: dict[str, tuple[int, object]], *, ready: bool = True,
                 origin: str = "https://www.doubao.com"):
        self.responses = responses
        self.ready = ready
        self.origin = origin
        self.calls: list[str] = []
        self.methods: list[str] = []
        self.exprs: list[str] = []
        self.hints: list[str] = []
        self.signature: dict[str, str] | None = None  # 页面里能借到的签名参数
        # 形如 /api/...（不含查询串），长的排前面：
        # /api/activity/check_in 是 /api/activity/check_in/status 的前缀
        self._known = sorted(
            [doubao_checkin._LOGIN_PROBE, *doubao_checkin._CANDIDATES,
             *doubao_checkin._CLAIM_SUFFIX.values()], key=len, reverse=True)

    @property
    def is_ready(self) -> bool:
        return self.ready

    def _login_state_sync(self, client):
        return doubao_checkin.login_state(client)

    def login_state_sync(self, timeout: float = 0) -> bool | None:
        """真客户端的三态登录探针；这里复用 evaluate 回放。"""
        return doubao_checkin.login_state(self)

    def _build_query_params(self) -> dict[str, str]:
        return {"aid": "1044603", "web_id": "w1"}

    def page_origin(self, timeout: float = 10.0) -> str:
        return self.origin

    def evaluate(self, expr: str, timeout: float = 20.0):
        if "performance.getEntriesByType" in expr:  # 借签名参数
            self.calls.append("signature")
            return self.signature
        if "document.scripts" in expr:  # 接口线索扫描
            self.calls.append("discover")
            self.methods.append("GET")
            return self.hints
        m = re.search(r'fetch\("(?:https://www\.doubao\.com)?(/[A-Za-z0-9_/-]+)\?', expr)
        url_path = m.group(1) if m else None
        path = next((c for c in self._known if c == url_path), url_path)
        self.calls.append(path or "?")
        method = "POST" if '"POST"' in expr else "GET"
        self.methods.append(method)
        self.exprs.append(expr)
        status, body = self.responses.get(f"{method} {path}", (404, "<html>404 page not found</html>"))
        text = body if isinstance(body, str) else json.dumps(body)
        return {"http": status, "body": text, "error": ""}


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


#: 默认登录探针回「已登录」：200 + 业务错误码（不是 401）。
_LOGGED_IN = {"POST /api/chat/completion/chat": (200, {"code": 10001, "msg": "param missing"})}


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

    # 记下命中端点：下一轮先打它
    client.calls.clear()
    doubao_checkin.fetch_checkin_status(client)
    # 第 0 条永远是登录探针，接着借签名，再按记住的端点先打
    assert [c for c in client.calls if c not in ("signature",)][1] == "/api/activity/sign/info"


def test_status_returns_none_when_nothing_parses():
    client = FakeClient({})  # 全部 404
    assert doubao_checkin.fetch_checkin_status(client) == (None, "")


def test_status_rejects_business_error_code():
    """``code != 0``（登录过期）不能当状态——否则会误报「未签到」。"""
    client = FakeClient({
        **_LOGGED_IN,
        **{f"GET {p}": (200, {"code": 710012001, "message": "login invalid"})
           for p in doubao_checkin._CANDIDATES},
    })
    assert doubao_checkin.fetch_checkin_status(client) == (None, "")


def test_status_stops_when_login_probe_unreachable():
    """探针本身没打通（HTTP 0）不等于已登录：直接收手，别挨个碰候选端点。"""
    client = FakeClient({"POST /api/chat/completion/chat": (0, "")})
    assert doubao_checkin.fetch_checkin_status(client) == (None, "")
    assert client.calls == ["/api/chat/completion/chat"]


@pytest.mark.parametrize("http, body, expected", [
    (401, None, False),                                        # 服务端明确未登录
    (200, {"code": 710012001, "msg": "登录已过期"}, False),      # 豆包自家过期码
    (200, {"code": 10001, "msg": "param missing"}, True),       # 路由到鉴权层 = 会话可用
    (0, None, None),                                            # 探针没打通
    (404, "<html>Page not found</html>", None),                 # 过期会话下 Chat 路由回 404 页
    (500, None, None),
])
def test_login_probe_only_concludes_on_clear_evidence(http, body, expected):
    """探针只下明确结论：404/5xx/没打通都不能被讲成「已登录」。"""
    assert doubao_checkin.login_state_from_probe(http, body) is expected


def test_app_internal_page_uses_same_origin_relative_url():
    """App 聊天页（chrome://）里必须发相对路径：绝对 URL 跨域不带 cookie。"""
    client = FakeClient({**_LOGGED_IN, "GET /api/activity/sign/info": (200, _signed_payload())},
                        origin="chrome://doubaowork-chat")
    status, path = doubao_checkin.fetch_checkin_status(client)
    assert path == "/api/activity/sign/info"
    assert status is not None
    # 相对路径同样带上对话通道查询参数（同源即 App 同款上下文）；
    # 借签名那一步是 performance 扫描，不算请求
    req = [e for e in client.exprs if "fetch(" in e]
    assert all('"https://www.doubao.com' not in e for e in req)
    assert req and all('"/api/' in e and "aid=1044603&web_id=w1" in e for e in req)


def test_doubao_domain_page_keeps_absolute_url():
    """豆包域名页里绝对 URL 才是同源，别被改成相对路径。"""
    client = FakeClient({**_LOGGED_IN, "GET /api/activity/sign/info": (200, _signed_payload())})
    doubao_checkin.fetch_checkin_status(client)
    assert all('"https://www.doubao.com/api/' in e for e in client.exprs)


def test_relative_and_absolute_urls_differ_only_by_origin():
    client = FakeClient({}, origin="chrome://doubaowork-chat")
    signed = {"msToken": "m1", "a_bogus": "b1"}
    rel = doubao_checkin._fetch_target(client, "/api/activity/sign/info", signed)
    abs_ = doubao_checkin._signed_url(client, "/api/activity/sign/info", signed)
    assert rel == abs_[len("https://www.doubao.com"):]


def test_is_app_internal_page_tolerates_missing_or_broken_origin():
    class _NoOrigin:
        pass

    class _Broken:
        def page_origin(self):
            raise RuntimeError("no target")

    assert doubao_checkin._is_app_internal_page(_NoOrigin()) is False
    assert doubao_checkin._is_app_internal_page(_Broken()) is False
    assert doubao_checkin._is_app_internal_page(FakeClient({}, origin="")) is False
    assert doubao_checkin._is_app_internal_page(
        FakeClient({}, origin="chrome://doubaowork-chat")) is True


def test_login_probe_html_page_is_unknown_not_logged_in():
    """200 + SPA HTML（跨域被落回首页）不是 API 应答，不能读成「已登录」。"""
    assert doubao_checkin.login_state_from_probe(
        200, None, "<!DOCTYPE html><html><body>app</body></html>") is None
    assert doubao_checkin.login_state_from_probe(200, None, "  \n<HTML></HTML>") is None


def test_login_probe_html_only_downgrades_unparsed_200():
    """明确证据优先：401 仍是未登录，200 + JSON 业务码仍按业务码判。"""
    assert doubao_checkin.login_state_from_probe(401, None, "<html>unauth</html>") is False
    assert doubao_checkin.login_state_from_probe(
        200, {"code": 10001}, "<html>ignored</html>") is True
    assert doubao_checkin.login_state_from_probe(
        200, {"code": 710012001}, None) is False


def test_cdp_login_state_reuses_probe_rules():
    """CDP 同步探针与 checkin.login_state 同一口径（两处判定不能漂）。"""
    client = CDPDoubaoClient.__new__(CDPDoubaoClient)
    client._ready = True
    client._web_id = "w1"
    for http, body, expected in [(200, '{"code":10001}', True), (401, "", False),
                                 (404, "<html>nf</html>", None), (0, "boom", None)]:
        probe = {"http": http, "body": body}
        client._evaluate = lambda expr, timeout=0, _p=probe: _p
        assert client.login_state_sync() is expected
    # 200 + SPA HTML（跨域不带 cookie 被落回首页）同样只算「没下结论」
    probe = {"http": 200, "body": "<!DOCTYPE html><html><body>app</body></html>"}
    client._evaluate = lambda expr, timeout=0, _p=probe: _p
    assert client.login_state_sync() is None


def test_status_tries_signed_request_before_unsigned():
    """活动接口只认带签名的请求：先借页面签名，命中后记为 signed。"""
    client = FakeClient({**_LOGGED_IN,
                         "GET /api/activity/check_in/status": (200, {"code": 0, "data": {"signed": True}})})
    client.signature = {"msToken": "m1", "a_bogus": "ab1"}
    status, path = doubao_checkin.fetch_checkin_status(client)
    assert path == "/api/activity/check_in/status" and status["checked_in"] is True
    expr = next(e for e in client.exprs if "check_in/status" in e)
    assert "msToken=m1&a_bogus=ab1" in expr  # 默认参数之后追加签名
    assert "signature" in client.calls


def test_status_falls_back_to_unsigned_when_no_signature():
    client = FakeClient({**_LOGGED_IN,
                         "GET /api/activity/sign/info": (200, _signed_payload())})
    status, path = doubao_checkin.fetch_checkin_status(client)
    assert path == "/api/activity/sign/info"
    assert "msToken" not in next(e for e in client.exprs if "sign/info" in e)


def test_signed_query_requires_both_params():
    client = FakeClient({})
    client.signature = {"msToken": "m1"}  # 缺 a_bogus：不完整就不借
    assert doubao_checkin.signed_query(client) == {}


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
    assert "POST" in [m for c, m in zip(client.calls, client.methods)
                      if c == "/api/activity/check_in"]


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
    # 只为查询和登录探针各发了一次 POST，没有对领取端点盲打
    assert [c for c, m in zip(client.calls, client.methods) if m == "POST"] == \
        ["/api/chat/completion/chat"] * 2
