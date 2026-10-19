"""codex 管理面板五个接口（accounts / order / delete / rename / import）。

与 kimi 同款契约：GET 返回 ``{"enabled", "accounts"}``，其余四个写接口返回
重排/删除/改名后的同一份快照。这里盯的是**接口自身的校验与副作用**（400/404、
导入后账号真的落盘、删除连带清冷却），前端交互另有
``test_codex_panel_ui.py`` 用 node 真跑面板 JS。

全部离线：httpx.MockTransport 不打上游；凭据目录由 conftest 的
``CODEX_STATE_DIR`` 指到 tmp。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from buddy_proxy.codex import failover
from buddy_proxy.codex.credentials import (
    list_accounts, load_account_cred, save_account_cred)
from buddy_proxy.codex.provider import CodexProvider
from buddy_proxy.web.ui import channels


@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    failover._cooldowns.clear()
    yield
    failover._cooldowns.clear()


@pytest.fixture
def client():
    return TestClient(channels.app)


def _seed(acct_id: str, *, email: str = "", plan: str = "plus") -> None:
    save_account_cred({
        "account_id": acct_id,
        "type": "codex",
        "access_token": f"at-{acct_id}",
        "refresh_token": f"rt-{acct_id}",
        "expired": "2099-01-01T00:00:00Z",
        "chatgpt_account_id": f"acc-{acct_id}",
        "email": email or f"{acct_id}@example.com",
        "plan_type": plan,
    })


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------

def test_list_empty(client):
    r = client.get("/ui/api/codex/accounts")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "accounts": []}


def test_list_accounts(client):
    _seed("a", email="a@x.y")
    _seed("b", email="b@x.y", plan="pro")
    r = client.get("/ui/api/codex/accounts")
    body = r.json()
    assert body["enabled"] is True
    assert [a["id"] for a in body["accounts"]] == ["a", "b"]
    assert body["accounts"][1]["plan_type"] == "pro"
    assert body["accounts"][0]["index"] == 1
    # 不含秘密
    assert "access_token" not in json.dumps(body)
    assert "refresh_token" not in json.dumps(body)


# ---------------------------------------------------------------------------
# order
# ---------------------------------------------------------------------------

def test_order_rewrites_priority(client):
    _seed("a")
    _seed("b")
    r = client.post("/ui/api/codex/accounts/order", json={"ids": ["b", "a"]})
    assert r.status_code == 200
    assert [a["id"] for a in r.json()["accounts"]] == ["b", "a"]
    assert [a.id for a in list_accounts()] == ["b", "a"]


def test_order_rejects_partial_list(client):
    _seed("a")
    _seed("b")
    r = client.post("/ui/api/codex/accounts/order", json={"ids": ["a"]})
    assert r.status_code == 400


def test_order_empty_while_empty_is_noop(client):
    """没有账号时提交空列表是幂等的空操作（前端空面板不会误报错）。"""
    r = client.post("/ui/api/codex/accounts/order", json={"ids": []})
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "accounts": []}


def test_order_requires_ids(client):
    r = client.post("/ui/api/codex/accounts/order", json={"ids": "nope"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def test_delete_removes_creds_and_cooldown(client):
    _seed("a")
    _seed("b")
    failover.mark_cooldown("a", quota=True)
    r = client.post("/ui/api/codex/accounts/delete", json={"id": "a"})
    assert r.status_code == 200
    assert [a["id"] for a in r.json()["accounts"]] == ["b"]
    assert [x.id for x in list_accounts()] == ["b"]
    assert failover.cooldown_left("a")[0] == 0.0


def test_delete_missing_404(client):
    r = client.post("/ui/api/codex/accounts/delete", json={"id": "ghost"})
    assert r.status_code == 404


def test_delete_requires_id(client):
    r = client.post("/ui/api/codex/accounts/delete", json={})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------

def test_rename_sets_alias(client):
    _seed("a", email="a@x.y")
    r = client.post("/ui/api/codex/accounts/rename", json={"id": "a", "alias": "主号"})
    assert r.status_code == 200
    acct = r.json()["accounts"][0]
    assert acct["alias"] == "主号"
    assert acct["name"] == "主号"


def test_rename_empty_clears_alias(client):
    _seed("a", email="a@x.y")
    client.post("/ui/api/codex/accounts/rename", json={"id": "a", "alias": "临时"})
    r = client.post("/ui/api/codex/accounts/rename", json={"id": "a", "alias": ""})
    assert r.status_code == 200
    assert not r.json()["accounts"][0]["alias"]
    assert r.json()["accounts"][0]["name"] == "a@x.y"


def test_rename_too_long(client):
    _seed("a")
    r = client.post("/ui/api/codex/accounts/rename", json={"id": "a", "alias": "x" * 65})
    assert r.status_code == 400


def test_rename_missing_404(client):
    r = client.post("/ui/api/codex/accounts/rename", json={"id": "ghost", "alias": "x"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def test_import_cli_auth_json(client):
    """粘贴 ~/.codex/auth.json 原生形态（含 tokens 包裹）。"""
    payload = {"tokens": {"access_token": "at1", "refresh_token": "rt1",
                          "account_id": "acc1"}, "last_refresh": "2026-10-01T00:00:00Z"}
    r = client.post("/ui/api/codex/accounts/import", json={"payload": json.dumps(payload)})
    assert r.status_code == 200
    accts = r.json()["accounts"]
    assert len(accts) == 1
    assert accts[0]["email"] == ""
    stored = load_account_cred(accts[0]["id"])
    assert stored["access_token"] == "at1"


def test_import_accepts_content_key(client):
    """面板也可能发 ``content``（文件内容整包），两个键都认。"""
    r = client.post("/ui/api/codex/accounts/import",
                    json={"content": {"access_token": "at", "refresh_token": "rt"}})
    assert r.status_code == 200
    assert len(r.json()["accounts"]) == 1


def test_import_same_rt_updates_in_place(client):
    _seed("a")
    client.post("/ui/api/codex/accounts/import", json={
        "payload": {"access_token": "at-new", "refresh_token": "rt-a"}})
    accts = list_accounts()
    assert len(accts) == 1
    assert load_account_cred(accts[0].id)["access_token"] == "at-new"


def test_import_bad_json_400(client):
    r = client.post("/ui/api/codex/accounts/import", json={"payload": "{not json"})
    assert r.status_code == 400


def test_import_missing_tokens_400(client):
    r = client.post("/ui/api/codex/accounts/import", json={"payload": {"access_token": "at"}})
    assert r.status_code == 400


def test_import_missing_payload_400(client):
    r = client.post("/ui/api/codex/accounts/import", json={})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 账号名解析（日志归属）
# ---------------------------------------------------------------------------

def test_account_name_source_registered():
    from buddy_proxy.web.ui import queries

    assert "codex" in queries._ACCOUNT_NAME_SOURCES


def test_account_name_map_resolves_alias(client):
    """改名后请求日志里的账号名走 alias 链（日志归属用）。"""
    from buddy_proxy.web.ui import queries

    _seed("a", email="a@x.y")
    client.post("/ui/api/codex/accounts/rename", json={"id": "a", "alias": "主号"})
    queries._acct_name_cache = (0.0, {})
    names = queries._account_name_map()
    assert names.get("a") == "主号"


# ---------------------------------------------------------------------------
# provider 仍能正常注册（接口与 provider 共享同一份凭据目录）
# ---------------------------------------------------------------------------

def test_provider_sees_api_imports(client):
    client.post("/ui/api/codex/accounts/import",
                json={"payload": {"access_token": "at", "refresh_token": "rt"}})
    prov = CodexProvider()
    assert prov.health()["accounts"] == 1
