"""qoder 多账号凭据存储测试（QODER_STATE_DIR 由 conftest autouse 隔离到 tmp_path）。"""

from __future__ import annotations

import json
import threading
import time

import pytest

from buddy_proxy.qoder import credentials
from buddy_proxy.qoder.credentials import (
    AuthError,
    account_cred_path,
    cred_to_credential,
    credential_to_cred,
    delete_account,
    derive_account_id,
    ensure_account_token,
    index_path,
    list_accounts,
    load_account_cred,
    reorder_accounts,
    save_account_cred,
)


def _cred(account_id: str, **over) -> dict:
    # account_id 用「裸名」形态（与 derive_account_id 的 email 首选不冲突）；
    # email 单独给，模拟 device flow 带回的真实邮箱。
    cred = {
        "account_id": account_id,
        "token": f"dt-{account_id}-real-token",
        "uid": f"uid-{account_id}",
        "machine_id": "m1",
        "refresh_token": f"rt-{account_id}",
        "expires_at_ms": int(time.time() * 1000) + 3600_000,
        "name": "",
        "email": f"{account_id}@qoder.example.com",
        "region": "cn",
        "plan": "",
        "source": "state",
    }
    cred.update(over)
    return cred


# ---------------------------------------------------------------------------
# 存取往返
# ---------------------------------------------------------------------------

def test_roundtrip_and_permissions():
    ref = save_account_cred(_cred("a@example.com"))
    assert ref.id == "a@example.com" and ref.priority == 0
    path = account_cred_path("a@example.com")
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = load_account_cred("a@example.com")
    assert loaded["refresh_token"] == "rt-a@example.com"
    assert loaded["region"] == "cn"


def test_load_missing_or_corrupt_returns_none():
    assert load_account_cred("ghost") is None
    save_account_cred(_cred("a@example.com"))
    account_cred_path("a@example.com").write_text("{broken", encoding="utf-8")
    assert load_account_cred("a@example.com") is None
    # 缺 token 也算损坏（没有它账号就调不通）
    account_cred_path("a@example.com").write_text(
        json.dumps({"refresh_token": "x"}), encoding="utf-8")
    assert load_account_cred("a@example.com") is None


# ---------------------------------------------------------------------------
# account_id 派生（三级优先）
# ---------------------------------------------------------------------------

def test_derive_account_id_priority():
    assert derive_account_id(_cred("mine")) == "mine"
    assert derive_account_id({"email": "e@x.com", "token": "t"}) == "e@x.com"
    assert derive_account_id({"uid": "u-9", "token": "t"}) == "u-9"
    got = derive_account_id({"token": "secret"})
    assert got.startswith("acct-") and len(got) == len("acct-") + 12


# ---------------------------------------------------------------------------
# upsert / 顺位 / 上限 / 自愈
# ---------------------------------------------------------------------------

def test_upsert_keeps_priority_and_added_at():
    ref1 = save_account_cred(_cred("a@example.com", name="旧名"))
    save_account_cred(_cred("b@example.com"))
    time.sleep(0.01)
    ref2 = save_account_cred(_cred("a@example.com", name="新名"))
    assert ref2.id == ref1.id
    assert ref2.priority == ref1.priority
    assert ref2.added_at == ref1.added_at
    assert list_accounts()[0].name == "新名"


def test_upsert_matches_by_email_and_refresh_token():
    """重登（RT 已滚动、account_id 也换了）按 email 命中原账号，不复制成两份。"""
    save_account_cred(_cred("acct-1", email="me@qoder.com", refresh_token="rt-OLD"))
    ref = save_account_cred(_cred("tmp-id", email="me@qoder.com", refresh_token="rt-NEW"))
    assert ref.id == "acct-1", "email 匹配优先"
    assert [r.id for r in list_accounts()] == ["acct-1"]
    assert load_account_cred("acct-1")["refresh_token"] == "rt-NEW"
    # 文件名跟索引 id 走：tmp-id 的 cred 文件不存在
    assert not account_cred_path("tmp-id").exists()


def test_new_account_appends_with_next_priority():
    save_account_cred(_cred("a@example.com"))
    save_account_cred(_cred("b@example.com"))
    save_account_cred(_cred("c@example.com"))
    refs = list_accounts()
    assert [(r.id, r.priority) for r in refs] == [
        ("a@example.com", 0), ("b@example.com", 1), ("c@example.com", 2)]


def test_no_account_cap_by_default():
    """历史 8 个账号上限已放开：默认不限制。"""
    for i in range(11):
        save_account_cred({"email": f"u{i}@example.com", "token": f"dt-token-{i}",
                           "refresh_token": f"rt-{i}"})
    assert len(list_accounts()) == 11


def test_index_self_heals_when_cred_file_deleted():
    save_account_cred(_cred("a@example.com"))
    save_account_cred(_cred("b@example.com"))
    account_cred_path("a@example.com").unlink()
    refs = list_accounts()
    assert [r.id for r in refs] == ["b@example.com"]
    index = json.loads(index_path().read_text(encoding="utf-8"))
    assert [e["id"] for e in index["accounts"]] == ["b@example.com"]


def test_delete_account():
    save_account_cred(_cred("a@example.com"))
    assert delete_account("a@example.com") is True
    assert list_accounts() == []
    assert delete_account("a@example.com") is False  # 幂等


def test_reorder_requires_full_id_list():
    for aid in ("a@example.com", "b@example.com", "c@example.com"):
        save_account_cred(_cred(aid))
    refs = reorder_accounts(["c@example.com", "a@example.com", "b@example.com"])
    assert [(r.id, r.priority) for r in refs] == [
        ("c@example.com", 0), ("a@example.com", 1), ("b@example.com", 2)]
    with pytest.raises(ValueError, match="完整"):
        reorder_accounts(["a@example.com", "b@example.com"])
    with pytest.raises(ValueError, match="完整"):
        reorder_accounts(["a@example.com", "b@example.com", "c@example.com", "d@example.com"])
    with pytest.raises(ValueError, match="完整"):
        reorder_accounts(["a@example.com", "a@example.com", "c@example.com"])


# ---------------------------------------------------------------------------
# 历史单账号迁移
# ---------------------------------------------------------------------------

def test_legacy_single_state_migrates_to_account_one(tmp_path):
    """旧的 ~/.buddy-proxy/qoder_auth.json 首次访问自动迁移成账号 #1。"""
    from buddy_proxy.qoder.credentials import legacy_auth_state_path

    target = tmp_path / "qoder_auth.json"
    target.write_text(json.dumps({
        "token": "dt-legacy-token", "uid": "uid-legacy",
        "refresh_token": "rt-legacy", "expires_at_ms": 9999999999000,
        "email": "legacy@example.com", "region": "cn",
    }), encoding="utf-8")
    monkey = pytest.MonkeyPatch()
    monkey.setenv("QODER_AUTH_FILE", str(target))
    try:
        refs = list_accounts()
    finally:
        monkey.undo()
    assert len(refs) == 1
    assert refs[0].email == "legacy@example.com"
    loaded = load_account_cred(refs[0].id)
    assert loaded["token"] == "dt-legacy-token"
    assert loaded["refresh_token"] == "rt-legacy"


# ---------------------------------------------------------------------------
# ensure_account_token（转发主路径，打桩刷新不触网）
# ---------------------------------------------------------------------------

def test_ensure_token_skips_refresh_when_valid(monkeypatch):
    save_account_cred(_cred("a@example.com"))
    calls: list[int] = []

    def fake_refresh(cred, region=None):
        calls.append(1)
        return dict(cred, token="dt-fresh")

    monkeypatch.setattr(credentials, "refresh_account_cred", fake_refresh)
    token, got = ensure_account_token("a@example.com")
    assert token == "dt-a@example.com-real-token"
    assert got["uid"] == "uid-a@example.com"  # token 与 cred 同源
    assert calls == []


class _FakeResp:
    """打 httpx.post 的最小响应桩（refresh 端点返回新 token）。"""

    status_code = 200
    text = ""

    def __init__(self, token: str):
        self._token = token

    def json(self) -> dict:
        return {"token": self._token, "expires_in": 3600}


def _patch_refresh_post(monkeypatch, calls: list[str]) -> None:
    """把 credentials 模块里的 httpx.post 换成只认 refresh 端点的桩。"""
    import httpx as _httpx

    real_post = _httpx.post

    def fake_post(url, **kwargs):
        if "deviceToken/refresh" in str(url):
            calls.append(str(url))
            return _FakeResp("dt-fresh")
        return real_post(url, **kwargs)

    monkeypatch.setattr(credentials.httpx, "post", fake_post)


def test_ensure_token_refreshes_when_expired(monkeypatch):
    save_account_cred(_cred("a@example.com", expires_at_ms=1000))
    calls: list[str] = []
    _patch_refresh_post(monkeypatch, calls)
    token, _ = ensure_account_token("a@example.com")
    assert token == "dt-fresh"
    token2, _ = ensure_account_token("a@example.com")  # 已落盘：不再刷
    assert token2 == "dt-fresh"
    assert len(calls) == 1


def test_ensure_token_concurrent_single_refresh(monkeypatch):
    """并发同时发现过期：每账号锁 + 锁内重读盘，refresh 只发一次。"""
    save_account_cred(_cred("a@example.com", expires_at_ms=1000))
    calls: list[str] = []
    _patch_refresh_post(monkeypatch, calls)
    tokens: list[str] = []
    errors: list[Exception] = []

    def worker():
        try:
            tokens.append(ensure_account_token("a@example.com")[0])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(calls) == 1, "每账号刷新只发一次（锁内重读盘双检）"
    assert tokens == ["dt-fresh"] * 4


def test_ensure_token_missing_account_raises():
    from buddy_proxy.qoder.credentials import AuthError as _AuthError

    with pytest.raises(_AuthError, match="不存在"):
        ensure_account_token("ghost")


# ---------------------------------------------------------------------------
# Credential <-> cred dict 互转
# ---------------------------------------------------------------------------

def test_credential_roundtrip():
    cred = _cred("a@example.com")
    snapshot = cred_to_credential(credential_to_cred(cred_to_credential(cred)))
    assert snapshot.token == cred["token"]
    assert snapshot.region == cred["region"]
    assert snapshot.is_expired() is False


def test_index_file_lock_is_process_safe():
    """索引读-改-写有跨进程锁（flock 套在 index.lock 上）。"""
    assert credentials._index_file_lock is not None
    with credentials._index_file_lock():
        pass
