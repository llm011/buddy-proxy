"""codex 多账号存储与刷新测试（离线；CODEX_STATE_DIR 由 conftest 隔离）。"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from buddy_proxy.codex import credentials, oauth


def _future(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _cred(**over) -> dict:
    base = {
        "type": "codex",
        "access_token": "at",
        "refresh_token": "rt",
        "id_token": "",
        "expired": _future(),
        "chatgpt_account_id": "acc-1",
        "user_id": "user-1",
        "email": "a@b.c",
        "plan_type": "plus",
    }
    base.update(over)
    # refresh_token / user_id 默认随 chatgpt_account_id 走：不同账号必须是不同
    # 凭据，否则 upsert 的「按 refresh_token / 身份字段认同一账号」规则会把
    # 两个本该独立的账号合并成一个（那是正确行为，不该在测试里被撞出来）。
    acc = str(base.get("chatgpt_account_id") or "")
    if "refresh_token" not in over:
        base["refresh_token"] = f"rt-{acc}"
    if acc and "user_id" not in over and base.get("user_id") == "user-1":
        base["user_id"] = f"user-{acc}"
    return base


def test_state_dir_honors_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_STATE_DIR", str(tmp_path / "custom"))
    assert credentials.codex_state_dir() == tmp_path / "custom"


def test_save_then_list_roundtrip():
    ref = credentials.save_account_cred(_cred())
    assert ref.id == "acct-acc-1"
    assert ref.name == "a@b.c"
    accounts = credentials.list_accounts()
    assert [a.id for a in accounts] == [ref.id]
    assert credentials.has_cred()


def test_cred_file_mode_is_0600():
    ref = credentials.save_account_cred(_cred())
    path = credentials.account_cred_path(ref.id)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    index = credentials.index_path()
    assert oct(index.stat().st_mode & 0o777) == "0o600"


def test_upsert_keeps_priority_and_alias():
    first = credentials.save_account_cred(_cred(refresh_token="rt-1"))
    second = credentials.save_account_cred(_cred(
        chatgpt_account_id="acc-2", refresh_token="rt-2", email="b@c.d"))
    credentials.rename_account(first.id, "主号")
    # 重登同一账号（新 RT）：必须命中原顺位
    ref = credentials.save_account_cred(_cred(refresh_token="rt-1-new"))
    assert ref.id == first.id
    assert ref.priority == 0
    assert ref.alias == "主号"
    assert [a.id for a in credentials.list_accounts()] == [first.id, second.id]


def test_reimport_same_account_via_identity():
    """refresh_token 已轮换但 chatgpt_account_id 相同 → 认成同一账号。"""
    first = credentials.save_account_cred(_cred(refresh_token="rt-old"))
    again = credentials.save_account_cred(_cred(
        refresh_token="rt-brand-new", chatgpt_account_id="acc-1"))
    assert again.id == first.id
    assert len(credentials.list_accounts()) == 1


def test_legacy_auth_json_shape_derives_id():
    """导入 Codex CLI 的 auth.json 片段（无 account_id）也能落盘。"""
    cred = {
        "access_token": "at", "refresh_token": "rt-x", "id_token": "",
        "expired": _future(), "chatgpt_account_id": "acc-legacy",
    }
    ref = credentials.save_account_cred(cred)
    assert ref.id == "acct-acc-legacy"
    assert cred["account_id"] == ref.id


def test_derive_id_falls_back_to_hash():
    aid = credentials.derive_account_id({"refresh_token": "only-rt"})
    assert aid.startswith("acct-")
    assert len(aid) > 5


def test_load_missing_or_broken_returns_none(tmp_path):
    assert credentials.load_account_cred("nope") is None
    assert credentials.load_account_cred("../etc/passwd") is None
    ref = credentials.save_account_cred(_cred())
    path = credentials.account_cred_path(ref.id)
    path.write_text("{not json", encoding="utf-8")
    assert credentials.load_account_cred(ref.id) is None


def test_load_requires_refresh_token():
    ref = credentials.save_account_cred(_cred())
    path = credentials.account_cred_path(ref.id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["refresh_token"] = ""
    path.write_text(json.dumps(data), encoding="utf-8")
    assert credentials.load_account_cred(ref.id) is None


def test_list_accounts_self_heals_when_cred_file_removed():
    ref = credentials.save_account_cred(_cred())
    credentials.account_cred_path(ref.id).unlink()
    assert credentials.list_accounts() == []
    # 自愈后索引也应清干净
    assert credentials._read_index().get("accounts") == []


def test_delete_account():
    ref = credentials.save_account_cred(_cred())
    assert credentials.delete_account(ref.id) is True
    assert credentials.list_accounts() == []
    assert credentials.delete_account(ref.id) is False


def test_reorder_accounts():
    a = credentials.save_account_cred(_cred(chatgpt_account_id="acc-a"))
    b = credentials.save_account_cred(_cred(chatgpt_account_id="acc-b"))
    ordered = credentials.reorder_accounts([b.id, a.id])
    assert [x.id for x in ordered] == [b.id, a.id]


def test_reorder_rejects_incomplete_list():
    a = credentials.save_account_cred(_cred(chatgpt_account_id="acc-a"))
    credentials.save_account_cred(_cred(chatgpt_account_id="acc-b"))
    with pytest.raises(ValueError):
        credentials.reorder_accounts([a.id])
    with pytest.raises(ValueError):
        credentials.reorder_accounts([a.id, a.id])


def test_rename_account():
    ref = credentials.save_account_cred(_cred())
    out = credentials.rename_account(ref.id, "  工作号  ")
    assert out[0].alias == "工作号"
    # 清空别名
    out = credentials.rename_account(ref.id, "")
    assert out[0].alias == ""


def test_rename_unknown_account_raises():
    credentials.save_account_cred(_cred())
    with pytest.raises(ValueError):
        credentials.rename_account("ghost", "x")


def test_account_limit():
    for i in range(credentials._MAX_ACCOUNTS):
        credentials.save_account_cred(_cred(chatgpt_account_id=f"acc-{i}",
                                            refresh_token=f"rt-{i}"))
    with pytest.raises(credentials.AuthError):
        credentials.save_account_cred(_cred(chatgpt_account_id="acc-over",
                                            refresh_token="rt-over"))


def test_access_token_valid_skew():
    assert credentials.access_token_valid(_cred(expired=_future(3600))) is True
    # 2 分钟后就过期：落在 5 分钟提前量内 → 视为无效
    assert credentials.access_token_valid(_cred(expired=_future(120))) is False
    assert credentials.access_token_valid(_cred(expired="garbage")) is False


def _stub_refresh(monkeypatch, *, new_rt="rt-new", expires_in=3600):
    calls: list[str] = []

    def _fake(rt, timeout=30.0):
        calls.append(rt)
        return oauth.TokenResponse(
            access_token="at-new", refresh_token=new_rt, id_token="",
            expires_in=expires_in, token_type="Bearer", scope="openid")

    monkeypatch.setattr(oauth, "refresh", _fake)
    return calls


def test_refresh_rotates_refresh_token(monkeypatch):
    """OpenAI 轮换 RT：新值必须落盘，旧值作废。"""
    calls = _stub_refresh(monkeypatch)
    ref = credentials.save_account_cred(_cred(refresh_token="rt-old"))
    cred = credentials.load_account_cred(ref.id)
    credentials.refresh_account_cred(cred)
    reloaded = credentials.load_account_cred(ref.id)
    assert reloaded["access_token"] == "at-new"
    assert reloaded["refresh_token"] == "rt-new"
    assert calls == ["rt-old"]


def test_refresh_keeps_old_rt_when_upstream_omits_it(monkeypatch):
    calls = _stub_refresh(monkeypatch, new_rt="")
    ref = credentials.save_account_cred(_cred(refresh_token="rt-old"))
    cred = credentials.load_account_cred(ref.id)
    credentials.refresh_account_cred(cred)
    assert credentials.load_account_cred(ref.id)["refresh_token"] == "rt-old"


def test_refresh_failure_raises_autherror(monkeypatch):
    def _boom(rt, timeout=30.0):
        raise oauth.OAuthError("invalid_grant")

    monkeypatch.setattr(oauth, "refresh", _boom)
    ref = credentials.save_account_cred(_cred())
    cred = credentials.load_account_cred(ref.id)
    with pytest.raises(credentials.AuthError):
        credentials.refresh_account_cred(cred)


def test_refresh_does_not_resurrect_deleted_account(monkeypatch):
    _stub_refresh(monkeypatch)
    ref = credentials.save_account_cred(_cred())
    cred = credentials.load_account_cred(ref.id)
    credentials.delete_account(ref.id)
    credentials.refresh_account_cred(cred)
    assert credentials.list_accounts() == []


def test_ensure_account_token_reuses_valid_token(monkeypatch):
    calls = _stub_refresh(monkeypatch)
    ref = credentials.save_account_cred(_cred())
    token, cred = credentials.ensure_account_token(ref.id)
    assert token == "at"
    assert cred["chatgpt_account_id"] == "acc-1"
    assert calls == []


def test_ensure_account_token_refreshes_when_expired(monkeypatch):
    _stub_refresh(monkeypatch)
    ref = credentials.save_account_cred(_cred(expired=_future(10)))
    token, __ = credentials.ensure_account_token(ref.id)
    assert token == "at-new"


def test_ensure_account_token_force_refresh(monkeypatch):
    calls = _stub_refresh(monkeypatch)
    ref = credentials.save_account_cred(_cred())
    token, __ = credentials.ensure_account_token(ref.id, force_refresh=True)
    assert token == "at-new"
    assert calls == ["rt-acc-1"]


def test_ensure_account_token_missing_account():
    with pytest.raises(credentials.AuthError):
        credentials.ensure_account_token("ghost")


def test_concurrent_refresh_only_once(monkeypatch):
    """并发请求同时发现过期时，只有一次真的打上游。"""
    calls: list[str] = []

    def _slow(rt, timeout=30.0):
        calls.append(rt)
        import time as _t
        _t.sleep(0.05)
        return oauth.TokenResponse(
            access_token="at-new", refresh_token="rt-new", id_token="",
            expires_in=3600)

    monkeypatch.setattr(oauth, "refresh", _slow)
    ref = credentials.save_account_cred(_cred(expired=_future(1)))
    results: list[str] = []

    def _worker():
        token, __ = credentials.ensure_account_token(ref.id)
        results.append(token)

    threads = [threading.Thread(target=_worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert set(results) == {"at-new"}


def test_claims_backfilled_on_refresh(monkeypatch):
    """刷新带回的 id_token 要回填邮箱/套餐/account_id。"""
    import base64

    def _seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    id_token = ".".join([
        _seg({"alg": "none"}),
        _seg({"email": "new@x.y",
              "https://api.openai.com/auth": {
                  "chatgpt_account_id": "acc-new",
                  "chatgpt_plan_type": "pro",
                  "chatgpt_user_id": "u-new"}}),
        "sig",
    ])

    def _fake(rt, timeout=30.0):
        return oauth.TokenResponse(
            access_token="at-new", refresh_token="rt-new",
            id_token=id_token, expires_in=3600)

    monkeypatch.setattr(oauth, "refresh", _fake)
    ref = credentials.save_account_cred(_cred())
    cred = credentials.load_account_cred(ref.id)
    credentials.refresh_account_cred(cred)
    reloaded = credentials.load_account_cred(ref.id)
    assert reloaded["email"] == "new@x.y"
    assert reloaded["plan_type"] == "pro"
    assert reloaded["chatgpt_account_id"] == "acc-new"


def test_cred_expired_epoch_handles_z_suffix():
    assert credentials.cred_expired_epoch({"expired": "2030-01-01T00:00:00Z"}) > 0
    assert credentials.cred_expired_epoch({"expired": "2030-01-01T00:00:00"}) > 0
    assert credentials.cred_expired_epoch({"expired": "nope"}) == 0.0
    assert credentials.cred_expired_epoch({}) == 0.0


def test_index_file_is_not_world_readable_after_rewrite():
    ref = credentials.save_account_cred(_cred())
    credentials.reorder_accounts([ref.id])
    assert oct(credentials.index_path().stat().st_mode & 0o777) == "0o600"
    assert oct(os.stat(credentials.codex_state_dir()).st_mode & 0o777) <= "0o755"
