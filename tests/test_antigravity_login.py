"""antigravity 登录 / onboarding / 凭据存储单元测试。

ANTIGRAVITY_STATE_DIR（多账号目录）与 ANTIGRAVITY_OAUTH_JSON（旧单账号
迁移源）都由 conftest autouse 指到 tmp，不碰真实凭据。
"""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error

import pytest


def _cred(**over):
    cred = {
        "access_token": "at",
        "refresh_token": "rt",
        "token_type": "Bearer",
        "scope": "s",
        "expiry": "2099-01-01T00:00:00+00:00",
        "email": "u@x.com",
    }
    cred.update(over)
    return cred


# ---------------------------------------------------------------------------
# credentials：多账号存储（index.json + 每账号文件 + 迁移）
# ---------------------------------------------------------------------------

def test_account_cred_roundtrip(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds

    ref = creds.save_account_cred(_cred())
    assert ref.id == "u@x.com" and ref.priority == 0
    path = creds.account_cred_path(ref.id)
    assert path.name == "u@x.com.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert creds.load_account_cred(ref.id)["refresh_token"] == "rt"
    assert creds.has_accounts()
    assert [a.id for a in creds.list_accounts()] == ["u@x.com"]

    path.write_text("{bad")
    assert creds.load_account_cred(ref.id) is None  # 损坏 → None
    path.write_text(json.dumps({"access_token": "x"}))
    assert creds.load_account_cred(ref.id) is None  # 缺 refresh_token → None


def test_legacy_migration_copies_as_first_account(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds

    legacy = tmp_path / "antigravity_oauth.json"
    legacy.write_text(json.dumps({**_cred(), "project_id": "proj-old"}))

    accounts = creds.list_accounts()  # 首次访问触发迁移
    assert [a.id for a in accounts] == ["u@x.com"]
    assert accounts[0].priority == 0
    cred = creds.load_account_cred("u@x.com")
    assert cred["project_id"] == "proj-old"
    assert cred["account_id"] == "u@x.com"
    assert legacy.exists()  # copy 语义：旧文件保留作备份


def test_migration_failure_falls_back_to_logged_out(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds

    (tmp_path / "antigravity_oauth.json").write_text("{bad")
    assert creds.list_accounts() == []  # 迁移源损坏 → 视为未登录，不抛


def test_save_upsert_overrides_same_email(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds

    creds.save_account_cred(_cred())
    creds.save_account_cred(_cred(access_token="at2", project_id="p1"))
    accounts = creds.list_accounts()
    assert len(accounts) == 1 and accounts[0].priority == 0  # 重新登录不改顺位
    assert creds.load_account_cred("u@x.com")["access_token"] == "at2"


def test_save_new_email_appends_with_higher_priority(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds

    creds.save_account_cred(_cred())
    creds.save_account_cred(_cred(email="v@y.com", refresh_token="rt2"))
    accounts = creds.list_accounts()
    assert [a.id for a in accounts] == ["u@x.com", "v@y.com"]
    assert [a.priority for a in accounts] == [0, 1]


def test_account_id_falls_back_to_token_hash(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds

    ref = creds.save_account_cred(_cred(email=""))
    assert ref.id == "acct-" + hashlib.sha256(b"rt").hexdigest()[:12]


def test_max_accounts_cap(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds

    for i in range(8):
        creds.save_account_cred(_cred(email=f"u{i}@x.com", refresh_token=f"rt{i}"))
    with pytest.raises(creds.AuthError, match="上限"):
        creds.save_account_cred(_cred(email="u9@x.com", refresh_token="rt9"))


def test_index_self_heals_when_cred_file_removed(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds

    creds.save_account_cred(_cred())
    creds.account_cred_path("u@x.com").unlink()
    assert creds.list_accounts() == []  # 删文件 = 退出该账号


def test_delete_account(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds

    creds.save_account_cred(_cred())
    assert creds.delete_account("u@x.com") is True
    assert creds.list_accounts() == []
    assert not creds.account_cred_path("u@x.com").exists()
    assert creds.delete_account("u@x.com") is False  # 再删一次 = 没这账号


def test_reorder_accounts_rewrites_priority(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds

    for i in range(3):
        creds.save_account_cred(_cred(email=f"u{i}@x.com", refresh_token=f"rt{i}"))
    refs = creds.reorder_accounts(["u2@x.com", "u0@x.com", "u1@x.com"])
    assert [r.id for r in refs] == ["u2@x.com", "u0@x.com", "u1@x.com"]
    assert [r.priority for r in refs] == [0, 1, 2]
    # added_at 是登录事实，不随顺位改写
    assert refs[0].added_at == creds.list_accounts()[0].added_at
    # 重启语义：重新读盘（新进程视角）也是新顺位
    assert [r.id for r in creds.list_accounts()] == ["u2@x.com", "u0@x.com", "u1@x.com"]


def test_reorder_accounts_runs_legacy_migration_first(tmp_path, monkeypatch):
    """重排前必须先走唯一枚举入口：否则升级后第一次操作会「看不见」迁移账号。

    改造前 ``reorder_accounts`` 开头调 ``list_accounts()``，迁移 + 自愈都发生在
    那里；只调底座 ``reorder`` 会对着空索引校验，报「当前 0 个」。
    """
    from buddy_proxy.antigravity import credentials as creds

    (tmp_path / "antigravity_oauth.json").write_text(json.dumps(_cred()))
    refs = creds.reorder_accounts(["u@x.com"])
    assert [(r.id, r.priority) for r in refs] == [("u@x.com", 0)]


def test_reorder_accounts_rejects_incomplete_or_duplicate(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds

    for i in range(2):
        creds.save_account_cred(_cred(email=f"u{i}@x.com", refresh_token=f"rt{i}"))
    with pytest.raises(ValueError, match="完整的账号"):
        creds.reorder_accounts(["u0@x.com"])  # 少一个
    with pytest.raises(ValueError, match="完整的账号"):
        creds.reorder_accounts(["u0@x.com", "u1@x.com", "ghost@x.com"])  # 多一个
    with pytest.raises(ValueError, match="完整的账号"):
        creds.reorder_accounts(["u0@x.com", "u0@x.com"])  # 重复
    # 拒绝后索引原封不动
    assert [r.id for r in creds.list_accounts()] == ["u0@x.com", "u1@x.com"]


def test_refresh_account_cred_updates_and_saves(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds

    creds.save_account_cred(_cred(expiry="2000-01-01T00:00:00+00:00"))
    monkeypatch.setattr(creds, "_token_request", lambda data, timeout=30.0: {
        "access_token": "fresh", "expires_in": 3600, "scope": "s2"})

    cred = creds.refresh_account_cred(creds.load_account_cred("u@x.com"))
    assert cred["access_token"] == "fresh"
    assert cred["scope"] == "s2"
    assert creds.load_account_cred("u@x.com")["access_token"] == "fresh"  # 已落盘
    assert len(creds.list_accounts()) == 1  # 刷新走 upsert，不产生新条目

    def _reject(data, timeout=30.0):
        raise creds.AuthError("Google token 接口拒绝: invalid_grant")

    monkeypatch.setattr(creds, "_token_request", _reject)
    with pytest.raises(creds.AuthError, match="invalid_grant"):
        creds.refresh_account_cred(cred)


def test_ensure_account_token_returns_paired_snapshot(tmp_path, monkeypatch):
    """token 与 cred 快照同源：刷新后 project_id 从同一份 cred 拿（防串号）。"""
    from buddy_proxy.antigravity import credentials as creds

    creds.save_account_cred(_cred(expiry="2000-01-01T00:00:00+00:00", project_id="proj-1"))
    monkeypatch.setattr(creds, "_token_request", lambda data, timeout=30.0: {
        "access_token": "fresh", "expires_in": 3600})

    token, snap = creds.ensure_account_token("u@x.com")
    assert token == "fresh"
    assert snap["project_id"] == "proj-1"
    assert creds.load_account_cred("u@x.com")["access_token"] == "fresh"

    # 有效期内直接用，不再请求 token 接口
    token, _ = creds.ensure_account_token("u@x.com")
    assert token == "fresh"

    # force_refresh：token 被上游拒后的强刷路径
    monkeypatch.setattr(creds, "_token_request", lambda data, timeout=30.0: {
        "access_token": "fresh2", "expires_in": 3600})
    token, _ = creds.ensure_account_token("u@x.com", force_refresh=True)
    assert token == "fresh2"

    with pytest.raises(creds.AuthError, match="不存在"):
        creds.ensure_account_token("ghost@x.com")


# ---------------------------------------------------------------------------
# OAuth URL / 回调
# ---------------------------------------------------------------------------

def test_build_auth_url_params():
    from buddy_proxy.antigravity.login import SCOPES, _build_auth_url

    url = _build_auth_url("http://127.0.0.1:51121/oauth-callback", "st", "ch")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=1071006060591-" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A51121%2Foauth-callback" in url
    assert "access_type=offline" in url and "prompt=consent" in url
    assert "code_challenge=ch" in url and "code_challenge_method=S256" in url
    assert "cclog" in url and "experimentsandconfigs" in url  # antigravity 多的两个 scope
    assert len(SCOPES) == 5


def test_pkce_pair_shape():
    from buddy_proxy.antigravity.login import _pkce_pair

    verifier, challenge = _pkce_pair()
    assert 40 < len(verifier) <= 128 and len(challenge) == 43


# ---------------------------------------------------------------------------
# _exchange / resume_onboarding：token 先落盘，onboarding 失败不白费授权
# ---------------------------------------------------------------------------

def test_exchange_keeps_token_when_onboarding_fails(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds
    from buddy_proxy.antigravity import login as ag_login

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    # login.py 是 from-import 绑定，mock 要打在 login 模块的引用上
    monkeypatch.setattr(ag_login, "_token_request", lambda data, timeout=30.0: {
        "access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    monkeypatch.setattr(ag_login, "fetch_user_email", lambda token, timeout=15.0: "u@x.com")

    def boom(token, project_id=""):
        raise ag_login.SetupError("INELIGIBLE: nope")

    monkeypatch.setattr(ag_login, "setup_code_assist", boom)
    with pytest.raises(ag_login.LoginError, match="INELIGIBLE"):
        ag_login._exchange("code", "http://127.0.0.1:1/oauth-callback", "ver")
    # token 已落盘：重试只需续跑 onboarding
    assert creds.load_cred()["access_token"] == "at"


def test_exchange_success(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds
    from buddy_proxy.antigravity import login as ag_login

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    monkeypatch.setattr(ag_login, "_token_request", lambda data, timeout=30.0: {
        "access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    monkeypatch.setattr(ag_login, "fetch_user_email", lambda token, timeout=15.0: "")
    monkeypatch.setattr(ag_login, "setup_code_assist", lambda token, project_id="": {
        "project_id": "proj-x", "tier": "free-tier", "tier_name": ""})

    cred = ag_login._exchange("code", "uri", "ver")
    assert cred["project_id"] == "proj-x"
    saved = creds.load_cred()
    assert saved["project_id"] == "proj-x"
    assert len(creds.list_accounts()) == 1  # 落在多账号目录，不再是裸单文件


def test_resume_onboarding(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds
    from buddy_proxy.antigravity import login as ag_login

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    creds.save_cred(_cred(expiry="2000-01-01T00:00:00+00:00"))
    monkeypatch.setattr(creds, "_token_request", lambda data, timeout=30.0: {
        "access_token": "fresh", "expires_in": 3600})
    monkeypatch.setattr(ag_login, "setup_code_assist", lambda token, project_id="": {
        "project_id": "p", "tier": "free-tier", "tier_name": ""})

    cred = ag_login.resume_onboarding()
    assert cred["project_id"] == "p" and cred["access_token"] == "fresh"


def test_resume_onboarding_picks_pending_account(tmp_path, monkeypatch):
    """多账号下 resume 定位「缺 project」的账号，而不是已完成的主账号。"""
    from buddy_proxy.antigravity import credentials as creds
    from buddy_proxy.antigravity import login as ag_login

    creds.save_account_cred(_cred(expiry="2000-01-01T00:00:00+00:00", project_id="done"))
    creds.save_account_cred(_cred(email="v@y.com", refresh_token="rt2",
                                  expiry="2000-01-01T00:00:00+00:00"))  # 缺 project
    monkeypatch.setattr(creds, "_token_request", lambda data, timeout=30.0: {
        "access_token": "fresh2", "expires_in": 3600})
    monkeypatch.setattr(ag_login, "setup_code_assist", lambda token, project_id="": {
        "project_id": "p2", "tier": "free-tier", "tier_name": ""})

    cred = ag_login.resume_onboarding()
    assert cred["project_id"] == "p2" and cred["email"] == "v@y.com"


# ---------------------------------------------------------------------------
# setup：loadCodeAssist / onboardUser（mock 端点）
# ---------------------------------------------------------------------------

class _FakeResp:
    """模拟 urlopen 返回：.status/.read()/上下文管理器。"""

    def __init__(self, status: int, payload: dict):
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeUrlopen:
    """按 URL 序列 canned 响应；记录请求体供断言。"""

    def __init__(self, responses: dict[str, list]):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []
        self.headers: list[dict] = []

    def __call__(self, req, timeout=30.0):
        url = req.full_url
        body = json.loads(req.data.decode()) if req.data else {}
        self.calls.append((url, body))
        self.headers.append({k.lower(): v for k, v in req.header_items()})
        key = next((k for k in self.responses if k in url), None)
        if key is None:
            raise urllib.error.URLError("no canned response")
        item = self.responses[key]
        if not item:
            raise urllib.error.URLError("exhausted")
        status, payload = item.pop(0)
        if status >= 400:
            # fp 必须是文件对象：HTTPError.read()/close() 都按文件接口走，
            # 传 iterator 会在 3.12 上 AttributeError
            raise urllib.error.HTTPError(url, status, "err", {},
                                         io.BytesIO(json.dumps(payload).encode()))
        return _FakeResp(status, payload)


def test_metadata_numeric_enum(monkeypatch):
    from buddy_proxy.antigravity import setup

    monkeypatch.setattr(setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup.platform, "machine", lambda: "arm64")
    meta = setup.core_metadata()
    assert meta == {"ideType": 9, "platform": 2, "pluginType": 2}  # ANTIGRAVITY / DARWIN_ARM64 / GEMINI


def test_setup_already_onboarded(monkeypatch):
    from buddy_proxy.antigravity import setup

    fake = _FakeUrlopen({"loadCodeAssist": [(200, {
        "currentTier": {"id": "free-tier", "name": "Free"},
        "cloudaicompanionProject": {"id": "genai-managed"},
    })]})
    monkeypatch.setattr(setup.urllib.request, "urlopen", fake)

    info = setup.setup_code_assist("tok")
    assert info["project_id"] == "genai-managed"
    url, body = fake.calls[0]
    assert "daily-cloudcode-pa" in url  # daily 优先
    assert body["metadata"]["ideType"] == 9  # 数字枚举不是字符串


def test_post_sends_real_bearer_token(monkeypatch):
    """Authorization 必须是真 token——占位指纹头展开顺序反了会覆盖成
    "Bearer "（空），上游 401 CREDENTIALS_MISSING（真实链路踩过）。"""
    from buddy_proxy.antigravity import setup

    fake = _FakeUrlopen({"loadCodeAssist": [(200, {
        "currentTier": {"id": "free-tier"},
        "cloudaicompanionProject": "proj-x"})]})
    monkeypatch.setattr(setup.urllib.request, "urlopen", fake)

    setup.setup_code_assist("tok-real")
    assert fake.headers, "没有请求被发出"
    for headers in fake.headers:
        assert headers.get("authorization") == "Bearer tok-real"
        assert headers.get("x-client-name") == "antigravity"  # 指纹头仍在


def test_setup_onboard_free_tier_lro(monkeypatch):
    from buddy_proxy.antigravity import setup

    fake = _FakeUrlopen({
        "loadCodeAssist": [(200, {"allowedTiers": [
            {"id": "free-tier", "isDefault": True}, {"id": "standard-tier"}]})],
        "onboardUser": [(200, {"done": False, "name": "ops/x"}),
                        (200, {"done": False, "name": "ops/x"}),
                        (200, {"done": True,
                               "response": {"cloudaicompanionProject": {"id": "proj-1"}}})],
        # GET operation 走 name 轮询，也吃 onboardUser key 之外的路由：
        "ops/x": [(200, {"done": True,
                         "response": {"cloudaicompanionProject": {"id": "proj-1"}}})],
    })
    monkeypatch.setattr(setup.urllib.request, "urlopen", fake)
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)

    info = setup.setup_code_assist("tok")
    assert info == {"project_id": "proj-1", "tier": "free-tier", "tier_name": ""}
    onboard_bodies = [b for u, b in fake.calls if "onboardUser" in u]
    assert onboard_bodies[0] == {"tierId": "free-tier",
                                 "metadata": {"ideType": 9, "platform": setup._platform_enum(),
                                              "pluginType": 2}}  # free 不带 project


def test_setup_fallback_to_prod_endpoint(monkeypatch):
    """daily 端点 5xx → prod 兜底成功。"""
    from buddy_proxy.antigravity import setup

    fake = _FakeUrlopen({"loadCodeAssist": [(500, {"error": "boom"}),
                                            (200, {"currentTier": {"id": "free-tier"},
                                                   "cloudaicompanionProject": "proj-2"})]})
    monkeypatch.setattr(setup.urllib.request, "urlopen", fake)

    info = setup.setup_code_assist("tok")
    assert info["project_id"] == "proj-2"
    urls = [u for u, _ in fake.calls]
    assert "daily-cloudcode-pa" in urls[0]
    assert urls[1].startswith("https://cloudcode-pa")  # 第二发打到的是 prod 端点


def test_setup_ineligible_diag(monkeypatch):
    """onboarding 走完却没项目：报错要带 ineligibleTiers 的真实拒绝原因。"""
    from buddy_proxy.antigravity import setup

    fake = _FakeUrlopen({
        "loadCodeAssist": [(200, {
            "ineligibleTiers": [{"tierId": "free-tier", "reasonCode": "UNSUPPORTED_LOCATION",
                                 "reasonMessage": "region not supported"}]})],
        "onboardUser": [(200, {"done": True, "response": {}})],
    })
    monkeypatch.setattr(setup.urllib.request, "urlopen", fake)
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)

    with pytest.raises(setup.SetupError, match="UNSUPPORTED_LOCATION"):
        setup.setup_code_assist("tok")


def test_setup_lro_error_surface(monkeypatch):
    from buddy_proxy.antigravity import setup

    fake = _FakeUrlopen({
        "loadCodeAssist": [(200, {"allowedTiers": [{"id": "free-tier", "isDefault": True}]})],
        "onboardUser": [(200, {"done": True, "error": {"code": 7, "message": "permission denied"}})],
    })
    monkeypatch.setattr(setup.urllib.request, "urlopen", fake)
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)

    with pytest.raises(setup.SetupError, match="permission denied"):
        setup.setup_code_assist("tok")


# ---------------------------------------------------------------------------
# auth/login.py 入口
# ---------------------------------------------------------------------------

def test_login_entry_antigravity_registered():
    from buddy_proxy.auth import login as auth_login

    assert "antigravity" in auth_login.KNOWN_PROVIDERS
    assert auth_login._DISPATCH["antigravity"] is auth_login._login_antigravity
