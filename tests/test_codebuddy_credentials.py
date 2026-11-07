"""codebuddy 多账号凭据存储测试（CODEBUDDY_STATE_DIR / CODEBUDDY_LEGACY_SESSION
由 conftest autouse 隔离到 tmp_path，绝不碰真实 ~/.codebuddy-session.json）。"""

from __future__ import annotations

import json
import time

import pytest

from buddy_proxy.codebuddy_provider import credentials
from buddy_proxy.codebuddy_provider.credentials import (
    AuthError,
    account_cred_path,
    auth_headers_from_cred,
    delete_account,
    derive_account_id,
    ensure_account_token,
    index_path,
    legacy_session_path,
    list_accounts,
    load_account_cred,
    rename_account,
    reorder_accounts,
    save_account_cred,
    session_to_cred,
)


def _cred(account_id: str, **over) -> dict:
    cred = {
        "account_id": account_id,
        "token": f"at-{account_id}",
        "refresh_token": f"rt-{account_id}",
        "expires_at_ms": int(time.time() * 1000) + 3600_000,
        "uid": f"uid-{account_id}",
        "nickname": f"nick-{account_id}",
        "enterprise_id": "ent-1",
        "department_info": "",
        "domain": "https://copilot.tencent.com",
        "machine_id": f"mach-{account_id}",
        "source": "state",
    }
    cred.update(over)
    return cred


# ---------------------------------------------------------------------------
# 存取往返
# ---------------------------------------------------------------------------

def test_roundtrip_and_permissions():
    ref = save_account_cred(_cred("u100"))
    assert ref.id == "u100" and ref.priority == 0
    path = account_cred_path("u100")
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600  # token 落盘必须 0600
    loaded = load_account_cred("u100")
    assert loaded["refresh_token"] == "rt-u100"
    assert loaded["machine_id"] == "mach-u100"


def test_load_missing_or_corrupt_returns_none():
    assert load_account_cred("ghost") is None
    save_account_cred(_cred("u1"))
    account_cred_path("u1").write_text("{not json", encoding="utf-8")
    assert load_account_cred("u1") is None
    # 缺 token 也算损坏
    account_cred_path("u1").write_text(json.dumps({"token": ""}), encoding="utf-8")
    assert load_account_cred("u1") is None


def test_list_accounts_self_heals_when_cred_file_deleted():
    save_account_cred(_cred("u1"))
    save_account_cred(_cred("u2"))
    account_cred_path("u1").unlink()
    refs = list_accounts()
    assert [r.id for r in refs] == ["u2"], "cred 文件没了的账号要被剔除（删文件即退出）"


def test_derive_account_id_priority():
    # uid 优先；uid 形状非法退 token 哈希；已有合法 id 原样保留
    assert derive_account_id(_cred("u1")) == "u1"
    no_uid = _cred(account_id="", uid="")
    assert derive_account_id(no_uid).startswith("acct-")
    assert derive_account_id({"token": "x", "account_id": "u9"}) == "u9"


# ---------------------------------------------------------------------------
# upsert 语义
# ---------------------------------------------------------------------------

def test_upsert_keeps_priority_and_added_at():
    save_account_cred(_cred("u1"))
    p1 = (time.time(), list_accounts()[0].priority)
    time.sleep(0.01)
    save_account_cred(_cred("u1", token="at-new"))
    ref = list_accounts()[0]
    assert ref.priority == p1[1]
    assert load_account_cred("u1")["token"] == "at-new"
    assert len(list_accounts()) == 1


def test_upsert_matches_by_uid_and_refresh_token():
    save_account_cred(_cred("u1"))
    # 同 uid、没带 account_id（模拟从 session 摊平的 cred）→ 命中既有账号
    ref = save_account_cred(_cred(account_id="", token="at-rotated",
                                  refresh_token="rt-u1"))
    assert ref.id == "u1"
    assert len(list_accounts()) == 1
    assert load_account_cred("u1")["token"] == "at-rotated"


def test_upsert_preserves_alias():
    save_account_cred(_cred("u1"))
    rename_account("u1", "工作号")
    save_account_cred(_cred("u1", token="at-again"))  # 重登 upsert
    assert list_accounts()[0].alias == "工作号", "alias 是用户手起的，upsert 不能覆写"


def test_new_account_appends_with_next_priority():
    save_account_cred(_cred("u1"))
    save_account_cred(_cred("u2"))
    refs = list_accounts()
    assert [r.priority for r in refs] == [0, 1]


def test_no_account_cap_by_default():
    """历史 8 个账号上限已放开：默认不限制，超过 8 个仍可继续登录。"""
    for i in range(12):
        save_account_cred(_cred(f"u{i}"))
    assert len(list_accounts()) == 12


# ---------------------------------------------------------------------------
# delete / reorder / rename
# ---------------------------------------------------------------------------

def test_delete_account():
    save_account_cred(_cred("u1"))
    assert delete_account("u1") is True
    assert list_accounts() == []
    assert delete_account("u1") is False  # 再删返回 False


def test_reorder_requires_full_id_list():
    save_account_cred(_cred("u1"))
    save_account_cred(_cred("u2"))
    with pytest.raises(ValueError, match="完整"):
        reorder_accounts(["u1"])  # 少一个
    refs = reorder_accounts(["u2", "u1"])
    assert [r.id for r in refs] == ["u2", "u1"]
    assert [r.priority for r in refs] == [0, 1]


def test_rename_account():
    save_account_cred(_cred("u1"))
    refs = rename_account("u1", "个人号")
    assert refs[0].alias == "个人号"
    # cred 文件里没有 alias——别名只住在 index.json
    assert "alias" not in load_account_cred("u1")
    with pytest.raises(ValueError, match="不存在"):
        rename_account("ghost", "x")


# ---------------------------------------------------------------------------
# legacy 单账号迁移
# ---------------------------------------------------------------------------

def _write_legacy_session(token="legacy-at", uid="legacy-uid"):
    legacy_session_path().write_text(json.dumps({
        "auth": {"accessToken": token, "refreshToken": "legacy-rt",
                 "expiresAt": int(time.time() * 1000) + 3600_000,
                 "domain": "https://copilot.tencent.com"},
        "account": {"uid": uid, "nickname": "老账号",
                    "enterpriseId": "ent-9", "departmentInfo": "dept"},
        "machineId": "legacy-mach",
    }), encoding="utf-8")


def test_legacy_session_migrates_to_account_one():
    _write_legacy_session()
    refs = list_accounts()
    assert [r.id for r in refs] == ["legacy-uid"]
    cred = load_account_cred("legacy-uid")
    assert cred["token"] == "legacy-at"
    assert cred["machine_id"] == "legacy-mach"
    assert cred["enterprise_id"] == "ent-9"


def test_legacy_migration_is_once_only():
    _write_legacy_session(token="first")
    list_accounts()  # 触发迁移
    # 迁移后 legacy 文件又变了（旧路径 demo CLI 还在写它）——不能把账号顶回去
    _write_legacy_session(token="second", uid="other-uid")
    refs = list_accounts()
    assert [r.id for r in refs] == ["legacy-uid"]
    assert load_account_cred("legacy-uid")["token"] == "first"


def test_reorder_runs_legacy_migration_first():
    """重排前要先走唯一枚举入口，否则升级后第一次操作「看不见」迁移账号。"""
    _write_legacy_session()
    refs = reorder_accounts(["legacy-uid"])
    assert [(r.id, r.priority) for r in refs] == [("legacy-uid", 0)]


def test_session_to_cred_flattens_shape():
    _write_legacy_session()
    cred = session_to_cred(json.loads(legacy_session_path().read_text(encoding="utf-8")))
    assert cred["token"] == "legacy-at"
    assert cred["uid"] == "legacy-uid"
    assert cred["department_info"] == "dept"
    assert cred["source"] == "state"


# ---------------------------------------------------------------------------
# 认证 headers
# ---------------------------------------------------------------------------

def test_auth_headers_carry_all_account_identity():
    headers = auth_headers_from_cred(_cred("u1"))
    assert headers["Authorization"] == "Bearer at-u1"
    assert headers["X-User-Id"] == "uid-u1"
    assert headers["X-Enterprise-Id"] == "ent-1"
    assert headers["X-Tenant-Id"] == "ent-1"
    assert headers["X-Machine-Id"] == "mach-u1"
    assert headers["X-Product-Code"] == "codebuddy"
    assert headers["X-Domain"] == "https://copilot.tencent.com"
    # IDE 识别头一个都不能少（上游风控的一部分）
    for k in ("X-IDE-Type", "X-IDE-Name", "X-IDE-Version", "X-Product-Version"):
        assert headers[k]


# ---------------------------------------------------------------------------
# ensure_account_token / 刷新
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload, status=200, text=None):
        self._payload = payload
        self.status_code = status
        self.text = text or json.dumps(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _patch_refresh(monkeypatch, payload, capture=None, status=200):
    def _post(url, **kw):
        if capture is not None:
            capture.append({"url": url, "headers": kw.get("headers")})
        return _FakeResp(payload, status=status)
    monkeypatch.setattr(credentials.httpx, "post", _post)


def test_ensure_token_skips_refresh_when_valid(monkeypatch):
    save_account_cred(_cred("u1"))
    called = []
    _patch_refresh(monkeypatch, {}, capture=called)
    token, cred = ensure_account_token("u1")
    assert token == "at-u1" and cred["uid"] == "uid-u1"
    assert called == [], "未过期不该发刷新请求"
    # cred 与 token 同源：headers 需要的账号级身份都在
    assert cred["machine_id"] == "mach-u1"


def test_ensure_token_refreshes_when_expired(monkeypatch):
    save_account_cred(_cred("u1", expires_at_ms=int(time.time() * 1000) - 1000))
    capture = []
    _patch_refresh(monkeypatch, {"data": {"accessToken": "at-new", "refreshToken": "rt-new",
                                          "expiresAt": int(time.time() * 1000) + 3600_000}},
                   capture=capture)
    token, cred = ensure_account_token("u1")
    assert token == "at-new"
    assert cred["refresh_token"] == "rt-new"
    assert capture and "token/refresh" in capture[0]["url"]
    headers = capture[0]["headers"]
    assert headers["X-Refresh-Token"] == "rt-u1"
    assert headers["X-Auth-Refresh-Source"] == "plugin"
    assert "Authorization" not in headers, "刷新请求不该带旧 Bearer"
    # 刷新结果落盘
    assert load_account_cred("u1")["token"] == "at-new"


def test_ensure_token_concurrent_single_refresh(monkeypatch):
    """两线程同时发现过期：只有一个去打刷新接口，另一个等锁后经
    _reload_if_rotated 复用盘上新 token——不发第二次刷新请求（重放会被
    上游判重）。"""
    import threading

    save_account_cred(_cred("u1", expires_at_ms=int(time.time() * 1000) - 1000))
    calls: list[str] = []

    def _post(url, **kw):
        calls.append(url)
        time.sleep(0.05)  # 拉长刷新窗口，让另一线程稳定撞上「已有人在刷」
        return _FakeResp({"data": {"accessToken": "at-new", "refreshToken": "rt-new"}})

    monkeypatch.setattr(credentials.httpx, "post", _post)
    start = threading.Barrier(2)
    out: list[str] = []
    errors: list[Exception] = []

    def _run():
        try:
            start.wait()
            out.append(ensure_account_token("u1")[0])
        except Exception as exc:  # noqa: BLE001 - 收进 errors 让断言报
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert out == ["at-new", "at-new"]
    assert len(calls) == 1, "并发同时发现过期只允许打一次刷新接口"


def test_refresh_discards_write_when_account_deleted(monkeypatch):
    """刷新是网络往返（最长 30s），期间账号被删 → 放弃回写，别把它复活。"""
    save_account_cred(_cred("u1", expires_at_ms=int(time.time() * 1000) - 1000))
    _patch_refresh(monkeypatch, {"data": {"accessToken": "at-new", "refreshToken": "rt-new"}})
    cred = load_account_cred("u1")
    delete_account("u1")
    out = credentials.refresh_account_cred(cred)
    assert out["token"] == "at-new"  # 调用方仍拿到新 token（本次请求能用）
    assert load_account_cred("u1") is None, "已删账号不许被刷新回写复活"


def test_refresh_discards_write_when_rotated(monkeypatch):
    """刷新期间 RT 已被换（刚导入新凭据）→ 整份放弃回写，别拿旧快照回滚。"""
    save_account_cred(_cred("u1", expires_at_ms=int(time.time() * 1000) - 1000))
    _patch_refresh(monkeypatch, {"data": {"accessToken": "at-new", "refreshToken": "rt-new"}})
    cred = load_account_cred("u1")
    save_account_cred(_cred("u1", refresh_token="rt-imported"))  # 模拟导入新凭据
    credentials.refresh_account_cred(cred)
    assert load_account_cred("u1")["refresh_token"] == "rt-imported"
    assert load_account_cred("u1")["token"] == "at-u1", "新导入的凭据不被旧快照覆盖"


def test_ensure_token_missing_account_raises():
    with pytest.raises(AuthError, match="不存在"):
        ensure_account_token("ghost")


def test_refresh_failure_raises_auth_error(monkeypatch):
    save_account_cred(_cred("u1", expires_at_ms=int(time.time() * 1000) - 1000))
    _patch_refresh(monkeypatch, {}, status=500)
    with pytest.raises(AuthError, match="HTTP 500"):
        ensure_account_token("u1")
