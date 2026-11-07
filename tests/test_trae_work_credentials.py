"""trae work 多账号凭据存储单测。

覆盖：derive_account_id 派生、save_account_cred upsert（按 uid/refresh_token）、
delete_account / reorder_accounts、list_accounts 排序与自愈、ensure_account_token
（含刷新）、ContextVar set_current_work_account 影响 _load_work_cred。
conftest 已隔离 TRAE_WORK_STATE_DIR / TRAE_WORK_CRED_PATH，不碰真实账号。
"""
from __future__ import annotations

import time

import pytest

from buddy_proxy.trae import credentials as creds
from buddy_proxy.trae.credentials import (
    AuthError,
    delete_account,
    derive_account_id,
    ensure_account_token,
    list_accounts,
    load_account_cred,
    reorder_accounts,
    save_account_cred,
    set_current_work_account,
    _load_work_cred,
)


def _cred(uid: str, nickname: str = "Test", **over):
    base = {
        "uid": uid,
        "nickname": nickname,
        "access_token": f"at-{uid}",
        "refresh_token": f"rt-{uid}",
        "expires_at": int(time.time()) + 3600,
        "machine_id": "m", "device_id": "d", "api_host": "h",
        "enterprise_id": "",
    }
    base.update(over)
    return base


# -- derive_account_id ---------------------------------------------------------


def test_derive_prefers_uid():
    assert derive_account_id({"uid": "u123", "nickname": "n", "access_token": "x"}) == "u123"


def test_derive_falls_back_to_nickname():
    assert derive_account_id({"nickname": "myname", "access_token": "x"}) == "myname"


def test_derive_falls_back_to_token_hash():
    out = derive_account_id({"access_token": "sometoken"})
    assert out.startswith("acct-")
    assert len(out) == len("acct-") + 12


def test_derive_keeps_existing_valid_id():
    assert derive_account_id({"account_id": "keep-me", "uid": "u"}) == "keep-me"


# -- save / list / upsert -------------------------------------------------------


def test_save_then_list_roundtrip():
    ref = save_account_cred(_cred("u1", "Alice"))
    accts = list_accounts()
    assert [a.id for a in accts] == [ref.id]
    assert ref.uid == "u1"
    assert ref.priority == 0


def test_save_appends_multiple_accounts_in_order():
    r1 = save_account_cred(_cred("u1", "Alice"))
    r2 = save_account_cred(_cred("u2", "Bob"))
    r3 = save_account_cred(_cred("u3", "Carol"))
    accts = list_accounts()
    assert [a.id for a in accts] == [r1.id, r2.id, r3.id]
    assert [a.priority for a in accts] == [0, 1, 2]


def test_relogin_same_uid_updates_not_appends():
    r1 = save_account_cred(_cred("u1", "Alice"))
    n_before = len(list_accounts())
    # 重登同号：uid 命中 → 更新凭据，不追加、priority 不变
    r2 = save_account_cred(_cred("u1", "Alice", access_token="at-NEW"))
    assert r2.id == r1.id
    assert r2.priority == r1.priority
    assert len(list_accounts()) == n_before
    assert load_account_cred(r1.id)["access_token"] == "at-NEW"


def test_relogin_same_refresh_token_updates():
    # 两个凭据 uid 都为空（让 id 从 nickname/token 派生），refresh_token 一致
    c1 = _cred("", "Alice")
    c1["refresh_token"] = "shared-rt"
    r1 = save_account_cred(c1)
    c2 = _cred("", "Alice")
    c2["refresh_token"] = "shared-rt"
    r2 = save_account_cred(c2)
    assert r2.id == r1.id
    assert len(list_accounts()) == 1


def test_no_account_cap_by_default():
    """历史 8 个账号上限已放开：默认不限制。"""
    for i in range(11):
        save_account_cred(_cred(f"u{i}", f"N{i}"))
    assert len(creds.list_accounts()) == 11


def test_load_account_cred_rejects_bad_id():
    assert load_account_cred("../etc/passwd") is None
    assert load_account_cred("no/slash") is None


def test_load_account_cred_missing_returns_none():
    assert load_account_cred("nonexistent") is None


# -- delete / reorder ------------------------------------------------------------


def test_delete_account_removes_entry_and_file():
    ref = save_account_cred(_cred("u1", "Alice"))
    assert delete_account(ref.id) is True
    assert list_accounts() == []
    assert load_account_cred(ref.id) is None


def test_delete_account_unknown_returns_false():
    assert delete_account("nonexistent") is False


def test_reorder_accounts_rewrites_priority():
    r1 = save_account_cred(_cred("u1", "Alice"))
    r2 = save_account_cred(_cred("u2", "Bob"))
    r3 = save_account_cred(_cred("u3", "Carol"))
    reordered = reorder_accounts([r3.id, r1.id, r2.id])
    assert [a.id for a in reordered] == [r3.id, r1.id, r2.id]
    assert [a.priority for a in reordered] == [0, 1, 2]


def test_reorder_accounts_rejects_partial_list():
    r1 = save_account_cred(_cred("u1", "Alice"))
    save_account_cred(_cred("u2", "Bob"))
    with pytest.raises(ValueError):
        reorder_accounts([r1.id])  # 少一个


def test_reorder_accounts_rejects_duplicates():
    r1 = save_account_cred(_cred("u1", "Alice"))
    save_account_cred(_cred("u2", "Bob"))
    with pytest.raises(ValueError):
        reorder_accounts([r1.id, r1.id])


# -- ensure_account_token ---------------------------------------------------------


def test_ensure_account_token_returns_fresh_token_and_cred():
    ref = save_account_cred(_cred("u1", "Alice"))
    token, snap = ensure_account_token(ref.id)
    assert token == "at-u1"
    assert snap["uid"] == "u1"
    assert snap["account_id"] == ref.id


def test_ensure_account_token_unknown_raises():
    with pytest.raises(AuthError):
        ensure_account_token("nonexistent")


def test_ensure_account_token_expired_triggers_refresh(monkeypatch):
    ref = save_account_cred(_cred("u1", "Alice", expires_at=int(time.time()) - 10))
    calls = []

    def fake_refresh(cred):
        calls.append(cred["uid"])
        out = dict(cred)
        out["access_token"] = "at-REFRESHED"
        out["expires_at"] = int(time.time()) + 3600
        # 真实 refresh_account_cred 会回写盘；fake 也回写，模拟第二次 ensure
        # 直接读到新 token、不再触发刷新的行为。
        creds._atomic_write_json(creds.account_cred_path(cred["account_id"]), out)
        return out

    monkeypatch.setattr(creds, "refresh_account_cred", fake_refresh)
    token, snap = ensure_account_token(ref.id)
    assert calls == ["u1"]
    assert token == "at-REFRESHED"
    # 回写落盘：下次 ensure 直接读到新 token，不再触发刷新
    token2, _ = ensure_account_token(ref.id)
    assert token2 == "at-REFRESHED"
    assert len(calls) == 1


def test_ensure_account_token_force_refresh(monkeypatch):
    ref = save_account_cred(_cred("u1", "Alice"))  # 未过期
    calls = []

    def fake_refresh(cred):
        calls.append(cred["uid"])
        out = dict(cred)
        out["access_token"] = "at-FORCED"
        out["expires_at"] = int(time.time()) + 3600
        creds._atomic_write_json(creds.account_cred_path(cred["account_id"]), out)
        return out

    monkeypatch.setattr(creds, "refresh_account_cred", fake_refresh)
    token, _ = ensure_account_token(ref.id, force_refresh=True)
    assert calls == ["u1"]
    assert token == "at-FORCED"


# -- ContextVar 当前账号 ----------------------------------------------------------


def test_contextvar_selects_account_for_load_work_cred():
    r1 = save_account_cred(_cred("u1", "Alice"))
    r2 = save_account_cred(_cred("u2", "Bob"))

    # 未设置时回落顺位 #1
    set_current_work_account(None)
    assert _load_work_cred()["uid"] == "u1"

    # 设置后读对应账号
    set_current_work_account(r2.id)
    assert _load_work_cred()["uid"] == "u2"

    # 复位再读 #1
    set_current_work_account(None)
    assert _load_work_cred()["uid"] == "u1"
    _ = r1  # noqa: F841 — 保留引用表意


# -- legacy 迁移 --------------------------------------------------------------------


def test_legacy_migration_creates_account_one(tmp_path, monkeypatch):
    legacy = tmp_path / "trae_work.json"
    legacy.write_text(__import__("json").dumps(_cred("legacy-u", "Legacy")))
    monkeypatch.setattr(creds, "legacy_work_cred_path", lambda: legacy)
    # 清掉可能已落的迁移标记，强制本次迁移
    flag = creds.trae_state_dir() / creds._LEGACY_MIGRATE_FLAG
    flag.unlink(missing_ok=True)

    accts = list_accounts()  # 触发迁移
    assert any(a.id == "legacy-u" for a in accts)
    # 幂等：再列一次不重复
    accts2 = list_accounts()
    assert [a.id for a in accts2].count("legacy-u") == 1
