"""copilot 管理面板接口（accounts / order / delete / rename / import / import-gh / refresh）。

与 codex 同款契约：GET 返回 ``{"enabled", "accounts"}``，写接口返回同一份快照。
这里只盯**接口自身的校验与副作用**（400/404、token 校验、gh CLI 失败汇总、
模型刷新把上游目录真的换掉），前端交互另有 ``test_copilot_panel_ui.py``。

全部离线：``fetch_github_user`` / ``gh_token`` / ``fetch_models`` 在测试里被替掉，
凭据目录由 conftest 的 ``COPILOT_STATE_DIR`` 指到 tmp。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from buddy_proxy.copilot import credentials, failover
from buddy_proxy.copilot.catalog import ModelEntry
from buddy_proxy.copilot.credentials import list_accounts, load_account_cred
from buddy_proxy.web.ui import channels


@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    failover._cooldowns.clear()
    yield
    failover._cooldowns.clear()


@pytest.fixture
def client():
    return TestClient(channels.app)


def _seed(acct_id: str, *, source: str = "gh") -> None:
    credentials.save_account_cred({
        "account_id": acct_id,
        "access_token": f"gho_{acct_id}_token",
        "login": acct_id,
        "name": acct_id.title(),
        "source": source,
    })


def _fake_user(token: str, timeout: float = 15.0):
    login = token.split("_")[1]
    return {"login": login, "name": login.title()}


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------

def test_list_empty(client):
    r = client.get("/ui/api/copilot/accounts")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "accounts": []}


def test_list_accounts_exposes_no_secret(client):
    _seed("a")
    _seed("b", source="panel")
    body = client.get("/ui/api/copilot/accounts").json()
    assert body["enabled"] is True
    assert [a["id"] for a in body["accounts"]] == ["a", "b"]
    assert body["accounts"][0]["index"] == 1
    assert body["accounts"][1]["source"] == "panel"
    assert body["accounts"][0]["token"] == "ok"
    dumped = json.dumps(body)
    assert "gho_" not in dumped and "access_token" not in dumped


# ---------------------------------------------------------------------------
# order / delete / rename
# ---------------------------------------------------------------------------

def test_order_rewrites_priority(client):
    _seed("a")
    _seed("b")
    r = client.post("/ui/api/copilot/accounts/order", json={"ids": ["b", "a"]})
    assert r.status_code == 200
    assert [a["id"] for a in r.json()["accounts"]] == ["b", "a"]
    assert [a.id for a in list_accounts()] == ["b", "a"]
    # 展示编号与 failover.display_index 同源（✕ 删除靠它定位）
    assert failover.display_index() == {"b": 1, "a": 2}


@pytest.mark.parametrize("payload", [{"ids": ["a"]}, {"ids": "nope"}, {}])
def test_order_rejects_bad_payload(client, payload):
    _seed("a")
    _seed("b")
    assert client.post("/ui/api/copilot/accounts/order", json=payload).status_code == 400


def test_order_empty_while_empty_is_noop(client):
    r = client.post("/ui/api/copilot/accounts/order", json={"ids": []})
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "accounts": []}


def test_delete_removes_creds_and_cooldowns(client):
    _seed("a")
    _seed("b")
    failover.mark_cooldown("a", quota=True)
    failover.mark_model_cooldown("a", "gpt-5.6-terra", reason="test")
    assert failover.cooldown_left("a")[0] > 0
    assert failover.model_cooldown_left("a", "gpt-5.6-terra")[0] > 0

    r = client.post("/ui/api/copilot/accounts/delete", json={"id": "a"})
    assert r.status_code == 200
    assert [a["id"] for a in r.json()["accounts"]] == ["b"]
    assert load_account_cred("a") is None
    assert failover.cooldown_left("a")[0] == 0.0
    assert failover.model_cooldown_left("a", "gpt-5.6-terra")[0] == 0.0


def test_delete_unknown_is_404(client):
    r = client.post("/ui/api/copilot/accounts/delete", json={"id": "nope"})
    assert r.status_code == 404


def test_rename_sets_alias(client):
    _seed("jsongo")
    r = client.post("/ui/api/copilot/accounts/rename",
                    json={"id": "jsongo", "alias": "主力号"})
    assert r.status_code == 200
    assert r.json()["accounts"][0]["name"] == "主力号"
    assert r.json()["accounts"][0]["alias"] == "主力号"
    # 清空 = 恢复默认名
    r = client.post("/ui/api/copilot/accounts/rename", json={"id": "jsongo", "alias": ""})
    assert r.json()["accounts"][0]["name"] == "jsongo"


# ---------------------------------------------------------------------------
# import（粘贴 token）
# ---------------------------------------------------------------------------

def test_import_rejects_non_github_token(client):
    r = client.post("/ui/api/copilot/accounts/import", json={"token": "sk-123"})
    assert r.status_code == 400
    assert "gho_" in r.json()["detail"]["error"]["message"]


def test_import_rejects_empty(client):
    r = client.post("/ui/api/copilot/accounts/import", json={})
    assert r.status_code == 400


@pytest.fixture
def _patched_identity(monkeypatch):
    """``save_token`` 在``credentials`` 模块里直呼 ``fetch_github_user``，
    换成同模块的替身即可（``gh_token`` 由 import-gh 用例自行替换）。"""
    monkeypatch.setattr(credentials, "fetch_github_user", _fake_user)


def test_import_saves_account(client, _patched_identity):
    r = client.post("/ui/api/copilot/accounts/import", json={"token": "gho_alice_x"})
    assert r.status_code == 200
    body = r.json()
    assert [a["id"] for a in body["accounts"]] == ["alice"]
    assert body["accounts"][0]["source"] == "panel"
    cred = load_account_cred("alice")
    assert cred and cred["access_token"] == "gho_alice_x"
    # 落盘 0600
    assert oct(credentials.account_cred_path("alice").stat().st_mode & 0o777) == "0o600"


def test_import_surfaces_auth_error(client, monkeypatch):
    def _reject(token, timeout=15.0):
        raise credentials.AuthError("GitHub token 被拒（HTTP 401）")

    monkeypatch.setattr(credentials, "fetch_github_user", _reject)
    r = client.post("/ui/api/copilot/accounts/import", json={"token": "gho_bad_x"})
    assert r.status_code == 400
    assert "被拒" in r.json()["detail"]["error"]["message"]


# ---------------------------------------------------------------------------
# import-gh（本机 gh CLI）
# ---------------------------------------------------------------------------

def test_gh_accounts_lists_without_token(client, monkeypatch):
    monkeypatch.setattr(credentials, "gh_accounts", lambda timeout=15.0: ["jsongo"])
    assert client.get("/ui/api/copilot/gh-accounts").json() == {"accounts": ["jsongo"]}


def test_import_gh_one_account(client, monkeypatch):
    monkeypatch.setattr(credentials, "gh_token",
                        lambda user="", timeout=15.0: f"gho_{user}_tok")
    monkeypatch.setattr(credentials, "fetch_github_user", _fake_user)
    r = client.post("/ui/api/copilot/accounts/import-gh", json={"user": "bob"})
    assert r.status_code == 200
    assert [a["id"] for a in r.json()["accounts"]] == ["bob"]
    assert r.json()["accounts"][0]["source"] == "gh:bob"


def test_import_gh_all_collects_errors(client, monkeypatch):
    monkeypatch.setattr(credentials, "gh_accounts", lambda timeout=15.0: ["ok", "bad"])

    def _tok(user="", timeout=15.0):
        if user == "bad":
            raise credentials.AuthError("`gh auth token` 未取到 token")
        return f"gho_{user}_tok"

    monkeypatch.setattr(credentials, "gh_token", _tok)
    monkeypatch.setattr(credentials, "fetch_github_user", _fake_user)
    r = client.post("/ui/api/copilot/accounts/import-gh")
    assert r.status_code == 200
    body = r.json()
    assert [a["id"] for a in body["accounts"]] == ["ok"]
    assert body["errors"] and "bad" in body["errors"][0]


def test_import_gh_without_accounts_is_400(client, monkeypatch):
    monkeypatch.setattr(credentials, "gh_accounts", lambda timeout=15.0: [])
    r = client.post("/ui/api/copilot/accounts/import-gh")
    assert r.status_code == 400
    assert "gh auth login" in r.json()["detail"]["error"]["message"]


# ---------------------------------------------------------------------------
# models/refresh
# ---------------------------------------------------------------------------

def test_models_refresh_requires_provider(client, monkeypatch):
    class _State:
        providers: dict = {}

    monkeypatch.setattr(channels, "get_state", lambda: _State())
    r = client.post("/ui/api/copilot/models/refresh")
    assert r.status_code == 503


def test_models_refresh_replaces_catalog(client, monkeypatch):
    from buddy_proxy.copilot.provider import CopilotProvider

    provider = CopilotProvider()

    async def _refresh(force: bool = False) -> int:
        assert force is True, "面板按钮要强制刷新，不能吃 TTL 内的旧目录"
        provider._index.replace(
            [ModelEntry(id="m1", name="M1", vendor="openai",
                        endpoint="/responses", endpoints=("/responses",))],
            source="upstream")
        provider._refresh_error = ""
        return len(provider._index)

    monkeypatch.setattr(provider, "refresh_models", _refresh)

    class _State:
        providers = {"copilot": provider}

    monkeypatch.setattr(channels, "get_state", lambda: _State())
    body = client.post("/ui/api/copilot/models/refresh").json()
    assert body["ok"] is True and body["count"] == 1
    assert [m["id"] for m in body["models"]] == ["copilot/m1"]
