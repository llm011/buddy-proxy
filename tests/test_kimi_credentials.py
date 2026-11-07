"""kimi 多账号凭据存储测试（KIMI_STATE_DIR 由 conftest autouse 隔离到 tmp_path）。"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from buddy_proxy.kimi import credentials, oauth
from buddy_proxy.kimi.credentials import (
    AuthError,
    account_cred_path,
    delete_account,
    derive_account_id,
    ensure_account_token,
    index_path,
    list_accounts,
    load_account_cred,
    refresh_account_cred,
    reorder_accounts,
    save_account_cred,
)


def _cred(acct_id: str, **over):
    cred = {
        "account_id": acct_id,
        "type": "kimi",
        "access_token": f"at-{acct_id}",
        "refresh_token": f"rt-{acct_id}",
        "expired": "2099-01-01T00:00:00Z",
        "base_url": "https://api.kimi.com/coding",
        "oauth_host": "https://auth.kimi.com",
        "device_id": f"dev-{acct_id}",
    }
    cred.update(over)
    return cred


# ---------------------------------------------------------------------------
# 存取往返
# ---------------------------------------------------------------------------

def test_roundtrip_and_permissions():
    ref = save_account_cred(_cred("u1"))
    assert ref.id == "u1" and ref.priority == 0
    path = account_cred_path("u1")
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600  # 凭据文件必须 0600
    loaded = load_account_cred("u1")
    assert loaded["refresh_token"] == "rt-u1"
    assert loaded["base_url"] == "https://api.kimi.com/coding"


def test_load_missing_or_corrupt_returns_none():
    assert load_account_cred("ghost") is None
    save_account_cred(_cred("u1"))
    account_cred_path("u1").write_text("{broken", encoding="utf-8")
    assert load_account_cred("u1") is None
    # 缺 refresh_token 也算损坏（没有它账号就救不活）
    account_cred_path("u1").write_text(json.dumps({"access_token": "x"}), encoding="utf-8")
    assert load_account_cred("u1") is None


# ---------------------------------------------------------------------------
# account_id 派生（四级优先）
# ---------------------------------------------------------------------------

def test_derive_account_id_priority():
    # 1. 已有合法 id 原样保留
    assert derive_account_id(_cred("mine")) == "mine"
    # 2. user_id（同一账号重登/换设备仍稳定）
    assert derive_account_id({"user_id": "u-9", "refresh_token": "rt"}) == "u-9"
    # 3. kimi-<device_id slug>
    got = derive_account_id({"device_id": "8f0b9a52-6a83-4b39-9d0a-47fbb0f34ea1",
                             "refresh_token": "rt"})
    assert got == "kimi-8f0b9a52-6a83-4b39-9d0a-47fbb0f34ea1"
    # 4. refresh_token 哈希兜底（任何导入路径都落得了盘）
    got = derive_account_id({"refresh_token": "secret"})
    assert got.startswith("acct-") and len(got) == len("acct-") + 12


# ---------------------------------------------------------------------------
# upsert / 顺位 / 上限 / 自愈
# ---------------------------------------------------------------------------

def test_upsert_keeps_priority_and_added_at():
    ref1 = save_account_cred(_cred("u1", nickname="旧名"))
    save_account_cred(_cred("u2"))
    time.sleep(0.01)
    # 同 account_id 重存（重新导入）：priority/added_at 不变，仅 name 更新
    ref2 = save_account_cred(_cred("u1", nickname="新名"))
    assert ref2.id == ref1.id
    assert ref2.priority == ref1.priority
    assert ref2.added_at == ref1.added_at
    assert list_accounts()[0].name == "新名"
    # 同 refresh_token、不同 account_id：命中原账号（kimi 没有天然唯一键）
    stranger = _cred("someone-else", refresh_token="rt-u1", nickname="rt 匹配")
    ref3 = save_account_cred(stranger)
    assert ref3.id == "u1"
    assert not account_cred_path("someone-else").exists()  # 文件名跟索引 id 走
    assert stranger["account_id"] == "u1"  # cred 里的 id 被写回对齐


def test_new_account_appends_with_next_priority():
    save_account_cred(_cred("u1"))
    save_account_cred(_cred("u2"))
    save_account_cred(_cred("u3"))
    refs = list_accounts()
    assert [(r.id, r.priority) for r in refs] == [("u1", 0), ("u2", 1), ("u3", 2)]


def test_no_account_cap_by_default():
    """历史 8 个账号上限已放开：默认不限制。"""
    for i in range(11):
        save_account_cred({"user_id": f"user{i}", "refresh_token": f"rt-{i}"})
    assert len(list_accounts()) == 11


def test_index_self_heals_when_cred_file_deleted():
    save_account_cred(_cred("u1"))
    save_account_cred(_cred("u2"))
    account_cred_path("u1").unlink()  # 直接删文件 = 退出该账号
    refs = list_accounts()
    assert [r.id for r in refs] == ["u2"]
    index = json.loads(index_path().read_text(encoding="utf-8"))
    assert [e["id"] for e in index["accounts"]] == ["u2"]  # 索引已重写


def test_delete_account():
    save_account_cred(_cred("u1"))
    assert delete_account("u1") is True
    assert list_accounts() == []
    assert delete_account("u1") is False  # 幂等


def test_reorder_requires_full_id_list():
    for aid in ("u1", "u2", "u3"):
        save_account_cred(_cred(aid))
    refs = reorder_accounts(["u3", "u1", "u2"])
    assert [(r.id, r.priority) for r in refs] == [("u3", 0), ("u1", 1), ("u2", 2)]
    # 少一个 / 多一个 / 重复：全量校验拒绝
    with pytest.raises(ValueError, match="完整"):
        reorder_accounts(["u1", "u2"])
    with pytest.raises(ValueError, match="完整"):
        reorder_accounts(["u1", "u2", "u3", "u4"])
    with pytest.raises(ValueError, match="完整"):
        reorder_accounts(["u1", "u1", "u3"])


# ---------------------------------------------------------------------------
# 刷新（oauth.refresh_token 打桩，不触网）
# ---------------------------------------------------------------------------

def test_refresh_keeps_refresh_token_when_payload_lacks_it(monkeypatch):
    """kimi 是否轮换 refresh_token 未确证：payload 没带就保留旧值。"""
    save_account_cred(_cred("u1", refresh_token="rt-old"))

    def fake_refresh(oauth_host, refresh_token, *, device_id="", timeout=30.0):
        assert oauth_host == "https://auth.kimi.com"
        assert refresh_token == "rt-old"
        return {"access_token": "at-new", "expires_in": 900,
                "token_type": "Bearer", "scope": "kimi-code"}

    monkeypatch.setattr(oauth, "refresh_token", fake_refresh)
    got = refresh_account_cred(load_account_cred("u1"))
    assert got["access_token"] == "at-new"
    assert got["refresh_token"] == "rt-old"
    assert load_account_cred("u1")["access_token"] == "at-new"  # 回写落盘


def test_refresh_rolls_refresh_token_when_payload_has_one(monkeypatch):
    save_account_cred(_cred("u1"))
    monkeypatch.setattr(oauth, "refresh_token", lambda *a, **k: {
        "access_token": "at-new", "refresh_token": "rt-new", "expires_in": 900})
    got = refresh_account_cred(load_account_cred("u1"))
    assert got["refresh_token"] == "rt-new"
    assert load_account_cred("u1")["refresh_token"] == "rt-new"


def test_refresh_uses_account_own_oauth_host(monkeypatch):
    """多账号混用 cn/global 域：刷新永远打 cred 自己的 host。"""
    save_account_cred(_cred("u1", oauth_host="https://auth.kimi.ai"))
    seen = {}

    def fake_refresh(oauth_host, *a, **k):
        seen["host"] = oauth_host
        return {"access_token": "at", "expires_in": 900}

    monkeypatch.setattr(oauth, "refresh_token", fake_refresh)
    refresh_account_cred(load_account_cred("u1"))
    assert seen["host"] == "https://auth.kimi.ai"


# ---------------------------------------------------------------------------
# ensure_account_token（转发主路径）
# ---------------------------------------------------------------------------

def test_ensure_token_skips_refresh_when_valid(monkeypatch):
    save_account_cred(_cred("u1"))
    calls: list[int] = []

    def fake(*a, **k):
        calls.append(1)
        return {"access_token": "x", "expires_in": 900}

    monkeypatch.setattr(oauth, "refresh_token", fake)
    token, cred = ensure_account_token("u1")
    assert token == "at-u1"
    assert cred["base_url"] == "https://api.kimi.com/coding"  # token 与 cred 同源
    assert calls == []


def test_ensure_token_refreshes_when_expired(monkeypatch):
    save_account_cred(_cred("u1"))
    stale = load_account_cred("u1")
    stale["expired"] = "2020-01-01T00:00:00Z"
    save_account_cred(stale)
    calls: list[int] = []

    def fake(*a, **k):
        calls.append(1)
        return {"access_token": "at-fresh", "expires_in": 900}

    monkeypatch.setattr(oauth, "refresh_token", fake)
    token, _ = ensure_account_token("u1")
    assert token == "at-fresh"
    token, _ = ensure_account_token("u1")  # 刚刷完落盘：双检命中，不再刷
    assert token == "at-fresh"
    assert len(calls) == 1


def test_ensure_token_concurrent_single_refresh(monkeypatch):
    """并发同时发现过期：每账号锁 + 锁内重读盘，refresh 只发一次。"""
    save_account_cred(_cred("u1"))
    stale = load_account_cred("u1")
    stale["expired"] = "2020-01-01T00:00:00Z"
    save_account_cred(stale)
    calls: list[int] = []
    guard = threading.Lock()

    def fake(*a, **k):
        with guard:
            calls.append(1)
        return {"access_token": "at-fresh", "expires_in": 900}

    monkeypatch.setattr(oauth, "refresh_token", fake)
    tokens: list[str] = []
    errors: list[Exception] = []

    def worker():
        try:
            tokens.append(ensure_account_token("u1")[0])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(calls) == 1
    assert tokens == ["at-fresh"] * 4


def test_force_refresh_bypasses_validity(monkeypatch):
    save_account_cred(_cred("u1"))
    monkeypatch.setattr(
        oauth, "refresh_token", lambda *a, **k: {"access_token": "at-forced", "expires_in": 900})
    token, _ = ensure_account_token("u1", force_refresh=True)
    assert token == "at-forced"


def test_access_token_valid_skew():
    # 提前 2 分钟就算过期（15 分钟寿命 + 刷新开销）
    soon = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
    assert credentials.access_token_valid({"expired": soon}) is False
    comfy = (datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat()
    assert credentials.access_token_valid({"expired": comfy}) is True
    assert credentials.access_token_valid({}) is False


# ---------------------------------------------------------------------------
# review 修复：重新登录不复制账号 / 慢刷新不撤销删除导入 / 跨进程索引锁
# ---------------------------------------------------------------------------

def test_relogin_reuses_slot_when_refresh_token_rotated(monkeypatch):
    """重新登录（RT 已滚动）不该把同一账号复制成两份。

    典型路径：老 cred 刷新后 RT 换成 rt-NEW → 用户再跑一次 login，新的 cred
    既没 user_id（等 /v1/me）、device_id 又是新生成的。按 account_id 匹配会
    落成新 id，同一账号占两个顺位：死 RT 那个每轮 failover 白打一次上游，
    真正能用的那个还排在末尾。
    """
    # 盘上账号：user_id 已回填（/v1/me 给的稳定 id）
    save_account_cred(_cred("u1", refresh_token="rt-OLD", user_id="cv9abc"))
    time.sleep(0.01)
    # 重登：RT 已滚动、device_id 换了、account_id 也不同，但 user_id 相同
    ref = save_account_cred(_cred("tmp-id-not-in-index", refresh_token="rt-NEW",
                                 device_id="dev-fresh", user_id="cv9abc"))
    assert ref.id == "u1", "user_id 匹配优先于 account_id 匹配/派生"
    assert [r.id for r in list_accounts()] == ["u1"], "同一账号不能占两个顺位"
    assert load_account_cred("u1")["refresh_token"] == "rt-NEW"
    assert load_account_cred("u1")["device_id"] == "dev-fresh", "设备指纹跟着更新"


def test_relikey_mismatch_makes_new_account():
    """user_id/RT 都对不上就是真新账号：追加到末尾，不复用别人的顺位。"""
    save_account_cred(_cred("u1", refresh_token="rt-1", user_id="cv9abc"))
    ref = save_account_cred(_cred("u2", refresh_token="rt-2", user_id="cv9xyz"))
    assert ref.id == "u2" and ref.priority == 1
    assert [r.id for r in list_accounts()] == ["u1", "u2"]


def test_reimport_export_json_without_account_id_reuses_slot():
    """cli 导出 JSON 不带 account_id，重导入同一账号不得复制成两份。

    真机踩过：Downloads 里这份导出没有 account_id 字段（只有 device_id/
    refresh_token 等），导入后 old cred 的 RT 已被后续刷新滚动，_find_existing
    用空 aid 查不到、RT 又对不上——但它现场用 device_id 派生出的 id 和索引里
    那条**一模一样**（kimi-<device_id>）。没补按派生 id 的二次查找时，同一
    account_id 被追加成第二个顺位：面板显示 Kimi #1 与 Kimi #3 各一条。
    """
    dev = "98c7f045-7754-4ade-8f6e-1cc1c8fada76"
    # 首导（无 account_id，落成 kimi-<device_id>）
    first = {k: v for k, v in _cred("x", device_id=dev).items() if k != "account_id"}
    ref1 = save_account_cred(dict(first))
    assert ref1.id == f"kimi-{dev}"
    time.sleep(0.01)
    # 重导：RT 已滚动、仍无 account_id、device_id 不变
    again = {k: v for k, v in _cred("y", device_id=dev,
                                    refresh_token="rt-rolled").items()
             if k != "account_id"}
    ref2 = save_account_cred(dict(again))
    assert ref2.id == ref1.id, "派生出的 id 与既有账号相同，必须复用顺位"
    assert [r.id for r in list_accounts()] == [f"kimi-{dev}"], "同一账号不能占两个顺位"
    assert load_account_cred(ref1.id)["refresh_token"] == "rt-rolled"


def test_slow_refresh_does_not_revive_deleted_account(monkeypatch):
    """刷新往返期间账号被删：回写不能让它复活。

    面板返回「删除成功」后，慢刷新拿旧快照回写会把它当成新账号追加回来，
    每轮 failover 又会先选中这个坏号。
    """
    save_account_cred(_cred("u1", refresh_token="rt-1", expired="2000-01-01T00:00:00Z"))

    def _slow_refresh(*a, **k):
        # 网络往返期间用户点了删除
        assert delete_account("u1") is True
        return {"access_token": "at-new", "expires_in": 900, "refresh_token": "rt-2"}

    monkeypatch.setattr(oauth, "refresh_token", _slow_refresh)
    token, _cred_out = ensure_account_token("u1", force_refresh=True)
    assert token == "at-new"  # 调用方拿到新 token（内存里有效）
    assert [r.id for r in list_accounts()] == [], "但落盘不能把它加回来"
    assert not account_cred_path("u1").exists()


def test_slow_refresh_does_not_roll_back_new_import(monkeypatch):
    """刷新往返期间用户导入了新凭据：旧快照整份回写会把它无声抹掉。"""
    save_account_cred(_cred("u1", refresh_token="rt-1", expired="2000-01-01T00:00:00Z"))
    cred = load_account_cred("u1")

    def _slow_refresh(*a, **k):
        # 往返期间面板导入了 global 区的新凭据（同账号，RT 已换）
        save_account_cred(_cred("u1", refresh_token="rt-NEW-IMPORT",
                                base_url="https://api.kimi.ai/coding",
                                oauth_host="https://auth.kimi.ai",
                                device_id="dev-global"))
        return {"access_token": "at-new", "expires_in": 900}

    monkeypatch.setattr(oauth, "refresh_token", _slow_refresh)
    ensure_account_token("u1", force_refresh=True)
    stored = load_account_cred("u1")
    assert stored["base_url"] == "https://api.kimi.ai/coding", "导入不能被旧快照退回"
    assert stored["device_id"] == "dev-global"
    assert stored["refresh_token"] == "rt-NEW-IMPORT"


def test_index_file_lock_is_process_safe():
    """索引读-改-写有跨进程锁（flock 套在 index.lock 上）。

    网关进程与 CLI（buddy login kimi / 面板导入）是两个进程，共用同一个
    index.json；只有进程内锁时各自拿旧快照写回会丢更新。
    """
    assert credentials._index_file_lock is not None
    with credentials._index_file_lock():
        pass  # 能获取/释放即可（真跨进程语义靠 fcntl，同进程重入会死锁）
