"""codex 登录/导入测试（离线；不打真实端点、不监听 1455）。"""

from __future__ import annotations

import base64
import json

import pytest

from buddy_proxy.codex import credentials, login, oauth
from buddy_proxy.codex.callback_server import CallbackResult, make_handler


def _jwt(claims: dict) -> str:
    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


# ---------------------------------------------------------------------------
# 凭据导入
# ---------------------------------------------------------------------------

def test_cred_from_cli_auth_json():
    """Codex CLI 原生 ~/.codex/auth.json 形态。"""
    payload = {
        "tokens": {
            "access_token": "at", "refresh_token": "rt",
            "id_token": _jwt({"email": "u@x.y",
                              "https://api.openai.com/auth": {
                                  "chatgpt_account_id": "acc-1",
                                  "chatgpt_plan_type": "pro"}}),
            "account_id": "acc-1",
        },
        "last_refresh": "2026-10-01T00:00:00Z",
    }
    cred = login.cred_from_payload(payload)
    assert cred["type"] == "codex"
    assert cred["access_token"] == "at"
    assert cred["refresh_token"] == "rt"
    assert cred["chatgpt_account_id"] == "acc-1"
    assert cred["email"] == "u@x.y"
    assert cred["plan_type"] == "pro"


def test_cred_from_flat_payload():
    cred = login.cred_from_payload({
        "access_token": "at", "refresh_token": "rt", "account_id": "acc-9"})
    assert cred["chatgpt_account_id"] == "acc-9"
    assert cred["type"] == "codex"


def test_cred_requires_both_tokens():
    with pytest.raises(login.LoginError):
        login.cred_from_payload({"access_token": "at"})
    with pytest.raises(login.LoginError):
        login.cred_from_payload({"refresh_token": "rt"})
    with pytest.raises(login.LoginError):
        login.cred_from_payload({"tokens": {"access_token": "at"}})


def test_cred_rejects_disabled():
    with pytest.raises(login.LoginError):
        login.cred_from_payload(
            {"access_token": "a", "refresh_token": "r", "disabled": True})


def test_import_roundtrip():
    out = login.import_cred_payload(json.dumps(
        {"access_token": "at", "refresh_token": "rt", "account_id": "acc-5",
         "email": "e@x.y"}))
    assert out["account_id"]
    accounts = credentials.list_accounts()
    assert len(accounts) == 1
    stored = credentials.load_account_cred(accounts[0].id)
    assert stored["access_token"] == "at"


def test_import_rejects_bad_json():
    with pytest.raises(login.LoginError):
        login.import_cred_payload("{not json")


def test_import_rejects_empty():
    with pytest.raises(login.LoginError):
        login.import_cred_payload("   ")


def test_import_account_limit_surfaces_as_login_error():
    """账号数到上限：面板入口只认 LoginError（AuthError 会漏成 HTTP 500）。"""
    for i in range(credentials._MAX_ACCOUNTS):
        login.import_cred_payload(
            {"access_token": "at", "refresh_token": f"rt-{i}",
             "chatgpt_account_id": f"acc-limit-{i}"})
    with pytest.raises(login.LoginError):
        login.import_cred_payload(
            {"access_token": "at", "refresh_token": "rt-overflow",
             "chatgpt_account_id": "acc-limit-overflow"})


def test_cred_from_token_response():
    tok = oauth.TokenResponse(
        access_token="at", refresh_token="rt",
        id_token=_jwt({"email": "n@x.y",
                       "https://api.openai.com/auth": {
                           "chatgpt_account_id": "acc-n",
                           "chatgpt_plan_type": "team"}}),
        expires_in=3600)
    cred = login.cred_from_token_response(tok)
    assert cred["email"] == "n@x.y"
    assert cred["plan_type"] == "team"
    assert cred["chatgpt_account_id"] == "acc-n"
    assert cred["expired"]


# ---------------------------------------------------------------------------
# login_interactive（全打桩，不碰网络/端口）
# ---------------------------------------------------------------------------

def test_login_interactive_with_manual_paste(monkeypatch):
    """回调服务起不来时退化为手动粘贴。"""
    monkeypatch.setattr(login, "start_callback_server", lambda *a, **k: None)
    monkeypatch.setattr(login.oauth, "exchange_code",
                        lambda code, pkce, timeout=30.0: oauth.TokenResponse(
                            access_token="at", refresh_token="rt",
                            id_token=_jwt({"email": "p@x.y",
                                           "https://api.openai.com/auth": {
                                               "chatgpt_account_id": "acc-p",
                                               "chatgpt_plan_type": "plus"}}),
                            expires_in=3600))
    monkeypatch.setattr(login, "_prompt_manual_callback",
                        lambda: ("the-code", ""))

    cred = login.login_interactive(open_browser=False, timeout=1)
    assert cred["access_token"] == "at"
    assert cred["email"] == "p@x.y"
    assert len(credentials.list_accounts()) == 1


def test_login_interactive_via_callback(monkeypatch):
    """回调服务收到 code 的路径。"""

    class _FakeServer:
        def shutdown(self):
            pass

        def server_close(self):
            pass

    def _fake_start(result, **kwargs):
        result.set_code("cb-code", "st")
        return _FakeServer()

    monkeypatch.setattr(login, "start_callback_server", _fake_start)
    monkeypatch.setattr(login.oauth, "exchange_code",
                        lambda code, pkce, timeout=30.0: oauth.TokenResponse(
                            access_token="at2", refresh_token="rt2",
                            id_token="", expires_in=3600))
    cred = login.login_interactive(open_browser=False, timeout=2)
    assert cred["access_token"] == "at2"


def test_login_interactive_surfaces_callback_error(monkeypatch):
    class _FakeServer:
        def shutdown(self):
            pass

        def server_close(self):
            pass

    def _fake_start(result, **kwargs):
        result.set_error("access_denied")
        return _FakeServer()

    monkeypatch.setattr(login, "start_callback_server", _fake_start)
    with pytest.raises(login.LoginError) as exc:
        login.login_interactive(open_browser=False, timeout=2)
    assert "access_denied" in str(exc.value)


def test_login_interactive_timeout(monkeypatch):
    class _FakeServer:
        def shutdown(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(login, "start_callback_server",
                        lambda result, **kw: _FakeServer())
    with pytest.raises(login.LoginError) as exc:
        login.login_interactive(open_browser=False, timeout=0.05)
    assert "超时" in str(exc.value)


def test_login_interactive_non_tty_stdin(monkeypatch):
    """非交互 stdin（管道/SSH 无 tty）：粘贴步骤要报 LoginError 而不是抛 EOFError。"""
    monkeypatch.setattr(login, "start_callback_server", lambda *a, **k: None)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(EOFError()))
    with pytest.raises(login.LoginError) as exc:
        login.login_interactive(open_browser=False, timeout=1)
    assert "标准输入" in str(exc.value)


def test_login_interactive_exchange_failure(monkeypatch):
    class _FakeServer:
        def shutdown(self):
            pass

        def server_close(self):
            pass

    def _fake_start(result, **kwargs):
        result.set_code("bad-code", "")
        return _FakeServer()

    def _boom(code, pkce, timeout=30.0):
        raise oauth.OAuthError("invalid_grant")

    monkeypatch.setattr(login, "start_callback_server", _fake_start)
    monkeypatch.setattr(login.oauth, "exchange_code", _boom)
    with pytest.raises(login.LoginError) as exc:
        login.login_interactive(open_browser=False, timeout=2)
    assert "invalid_grant" in str(exc.value)


# ---------------------------------------------------------------------------
# CLI 注册
# ---------------------------------------------------------------------------

def test_cli_dispatch_registered():
    from buddy_proxy.auth import login as cli_login

    assert "codex" in cli_login.KNOWN_PROVIDERS
    assert "codex" in cli_login._DISPATCH


def test_login_handler_signature():
    """handler 必须能吃 open_browser / region（dispatch 统一调用）。"""
    import inspect

    from buddy_proxy.auth import login as cli_login

    handler = cli_login._DISPATCH["codex"]
    sig = inspect.signature(handler)
    assert "open_browser" in sig.parameters
    assert "region" not in sig.parameters  # 由 **_kwargs 吞掉


# ---------------------------------------------------------------------------
# 回调服务 Handler（不监听端口，直接调 do_GET 逻辑）
# ---------------------------------------------------------------------------

def _drive_handler(result, path):
    """构造 handler 实例并触发 do_GET（绕过 socket）。"""
    import io

    cls = make_handler(result)

    class _FakeHandler(cls):
        def __init__(self):  # noqa: D107 - 绕开 BaseHTTPRequestHandler.__init__
            self.path = path
            self.wfile = io.BytesIO()
            self._status = None
            self._headers: list[tuple[str, str]] = []
            self.command = "GET"

        def send_response(self, code, message=None):
            self._status = code

        def send_header(self, keyword, value):
            self._headers.append((keyword, value))

        def end_headers(self):
            pass

        def log_message(self, fmt, *args):
            pass

    h = _FakeHandler()
    h.do_GET()
    return h, result


def test_callback_handler_success():
    result = CallbackResult()
    h, _ = _drive_handler(result, "/auth/callback?code=abc&state=xyz")
    assert h._status == 200
    assert result.wait(0.1)
    assert result.code == "abc"
    assert result.state == "xyz"


def test_callback_handler_error_param():
    result = CallbackResult()
    h, _ = _drive_handler(result, "/auth/callback?error=access_denied")
    assert h._status == 400
    assert result.wait(0.1)
    assert "access_denied" in result.error


def test_callback_handler_unknown_path():
    result = CallbackResult()
    h, _ = _drive_handler(result, "/nope")
    assert h._status == 404
    assert not result.event.is_set()
