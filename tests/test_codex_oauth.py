"""codex OAuth 协议层测试（离线，不打真实 OpenAI 端点）。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import urllib.error
import urllib.parse

import pytest

from buddy_proxy.codex import oauth


def _b64len_ok(value: str) -> bool:
    """base64url 且无 padding。"""
    return value and "=" not in value and "+" not in value and "/" not in value


def test_generate_pkce_shape():
    pkce = oauth.generate_pkce()
    # 96 字节 → base64 128 字符
    assert len(pkce.verifier) == 128
    assert _b64len_ok(pkce.verifier)
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(pkce.verifier.encode()).digest()).decode().rstrip("=")
    assert pkce.challenge == expected
    assert _b64len_ok(pkce.challenge)


def test_generate_pkce_is_random():
    assert oauth.generate_pkce().verifier != oauth.generate_pkce().verifier


def test_build_authorize_url_has_all_params():
    pkce = oauth.PKCECodes(verifier="v" * 43, challenge="c" * 43)
    url = oauth.build_authorize_url(pkce, "state-123")
    assert url.startswith("https://auth.openai.com/oauth/authorize?")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["client_id"] == [oauth.CLIENT_ID]
    assert q["client_id"] == ["app_EMoamEEZ73f0CkXaXp7hrann"]
    assert q["response_type"] == ["code"]
    assert q["redirect_uri"] == ["http://localhost:1455/auth/callback"]
    assert q["scope"] == ["openid email profile offline_access"]
    assert q["state"] == ["state-123"]
    assert q["code_challenge"] == ["c" * 43]
    assert q["code_challenge_method"] == ["S256"]
    # 三个固定附加参数缺一个都可能换不到 code
    assert q["prompt"] == ["login"]
    assert q["id_token_add_organizations"] == ["true"]
    assert q["codex_cli_simplified_flow"] == ["true"]


def test_build_authorize_url_prompt_not_duplicated():
    """``prompt`` 只能出现一次（重复参数服务端取哪个不确定）。"""
    pkce = oauth.PKCECodes(verifier="v" * 43, challenge="c" * 43)
    q = urllib.parse.parse_qs(
        urllib.parse.urlparse(oauth.build_authorize_url(pkce, "s")).query)
    assert q["prompt"] == ["login"]


def test_build_authorize_url_force_reauth_off():
    pkce = oauth.PKCECodes(verifier="v" * 43, challenge="c" * 43)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(
        oauth.build_authorize_url(pkce, "s", force_reauth=False)).query)
    assert "prompt" not in q


@pytest.mark.parametrize("raw, want", [
    ("http://localhost:1455/auth/callback?code=abc&state=xyz", ("abc", "xyz")),
    ("code=abc&state=xyz", ("abc", "xyz")),
    ("  http://localhost:1455/auth/callback?code=abc  ", ("abc", "")),
    ("abc", ("abc", "")),
])
def test_parse_callback_input(raw, want):
    assert oauth.parse_callback_input(raw) == want


def test_parse_callback_input_rejects_empty():
    with pytest.raises(oauth.OAuthError):
        oauth.parse_callback_input("   ")


def test_parse_callback_input_rejects_no_code():
    with pytest.raises(oauth.OAuthError):
        oauth.parse_callback_input("http://localhost:1455/auth/callback?state=xyz")
    # 只有 query、没有 URL 前缀的形态同样要拦
    with pytest.raises(oauth.OAuthError):
        oauth.parse_callback_input("state=xyz")


def test_parse_callback_input_surfaces_error_param():
    """失败回调（用户点了拒绝/授权被拒）要把上游原因带出来，而不是当裸 code 送上去。"""
    with pytest.raises(oauth.OAuthError) as exc:
        oauth.parse_callback_input(
            "http://localhost:1455/auth/callback?error=access_denied"
            "&error_description=user+denied")
    assert "access_denied" in str(exc.value)


def _stub_post(monkeypatch, payload: dict, *, status: int = 200, capture=None):
    """把 ``urllib.request.urlopen`` 换成返回固定 JSON 的桩。"""

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake(req, timeout=None):
        if capture is not None:
            capture.append(urllib.parse.parse_qs(req.data.decode()))
        body = json.dumps(payload).encode()
        if status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, status, "err", {}, io.BytesIO(body))
        return _Resp(body)

    monkeypatch.setattr(oauth.urllib.request, "urlopen", _fake)


def test_exchange_code_request_shape(monkeypatch):
    capture: list[dict] = []
    _stub_post(monkeypatch, {
        "access_token": "at", "refresh_token": "rt", "id_token": "it",
        "expires_in": 3600,
    }, capture=capture)
    pkce = oauth.PKCECodes(verifier="verifier-xyz", challenge="ch")
    tok = oauth.exchange_code("the-code", pkce)

    assert tok.access_token == "at"
    assert tok.refresh_token == "rt"
    assert tok.expires_in == 3600
    sent = capture[0]
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["client_id"] == [oauth.CLIENT_ID]
    assert sent["code"] == ["the-code"]
    assert sent["redirect_uri"] == [oauth.REDIRECT_URI]
    assert sent["code_verifier"] == ["verifier-xyz"]


def test_refresh_request_shape(monkeypatch):
    capture: list[dict] = []
    _stub_post(monkeypatch, {"access_token": "at2", "expires_in": 3600},
               capture=capture)
    tok = oauth.refresh("rt-old")
    assert tok.access_token == "at2"
    sent = capture[0]
    assert sent["grant_type"] == ["refresh_token"]
    assert sent["refresh_token"] == ["rt-old"]
    assert sent["scope"] == ["openid profile email"]


def test_exchange_surfaces_error_description(monkeypatch):
    _stub_post(monkeypatch, {
        "error": "invalid_grant", "error_description": "code expired",
    }, status=400)
    with pytest.raises(oauth.OAuthError) as exc:
        oauth.exchange_code("bad", oauth.PKCECodes(verifier="v", challenge="c"))
    assert "400" in str(exc.value)
    assert "code expired" in str(exc.value)


def test_exchange_rejects_missing_access_token(monkeypatch):
    _stub_post(monkeypatch, {"refresh_token": "rt"})
    with pytest.raises(oauth.OAuthError):
        oauth.exchange_code("c", oauth.PKCECodes(verifier="v", challenge="c"))


def _jwt(claims: dict) -> str:
    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


def test_decode_id_token_claims_reads_nested_auth():
    claims = {
        "email": "a@b.c",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acc-1",
            "chatgpt_plan_type": "plus",
            "chatgpt_user_id": "user-1",
        },
    }
    parsed = oauth.decode_id_token_claims(_jwt(claims))
    assert oauth.email_from_claims(parsed) == "a@b.c"
    assert oauth.account_id_from_claims(parsed) == "acc-1"
    assert oauth.plan_type_from_claims(parsed) == "plus"
    assert oauth.user_id_from_claims(parsed) == "user-1"


def test_claim_helpers_degrade_gracefully():
    assert oauth.decode_id_token_claims("") == {}
    assert oauth.decode_id_token_claims("not-a-jwt") == {}
    assert oauth.account_id_from_claims({}) == ""
    assert oauth.email_from_claims({}) == ""
    # 缺 plan_type 时按 free（与官方默认一致）
    assert oauth.plan_type_from_claims({}) == "free"
    # 缺 chatgpt_account_id 时退回 sub
    assert oauth.account_id_from_claims({"sub": "s-1"}) == "s-1"
