"""共享多账号存储底座（``core/account_store.py``）的测试。

这层是从六个多账号通道（trae / qoder / codex / kimi / antigravity / codebuddy）
逐字重复的 ``credentials.py`` 里抽出来的，所以测试分两类：

1. 底座自己的契约（id 形状、0600 原子写、跨实例互斥、自愈、upsert 语义、
   重排校验、额度缓存代）；
2. **是否有通道真的在用**——否则一次「改回本地实现」的重构会让这份去重悄悄
   失效，而所有行为测试仍然全绿。
"""

from __future__ import annotations

import json
import threading

import pytest

from buddy_proxy.core.account_store import (
    ACCOUNT_ID_RE,
    AccountStore,
    atomic_write_json,
    is_valid_account_id,
    quota_epoch,
    quota_epoch_from,
    validate_account_id,
)


def _store(tmp_path, **over) -> AccountStore:
    return AccountStore(
        label="test", state_dir_fn=lambda: tmp_path / "ch", **over
    )


def _cred(account_id: str, **over) -> dict:
    cred = {
        "account_id": account_id,
        "token": f"tok-{account_id}",
        "uid": f"uid-{account_id}",
        "refresh_token": f"rt-{account_id}",
    }
    cred.update(over)
    return cred


def _upsert(store: AccountStore, cred: dict, **over) -> dict:
    """默认钩子：id 就是 account_id，身份看 uid/refresh_token，条目只存 uid。"""
    kwargs = {
        "required_field": "token",
        "derive_id": lambda c: str(c["refresh_token"]),
        "identity_fields": ("uid", "refresh_token"),
        "entry_fields": lambda c, prev: {
            "uid": str(c.get("uid") or (prev or {}).get("uid") or ""),
        },
        "error": ValueError,
    }
    kwargs.update(over)
    return store.upsert(cred, **kwargs)


# ---------------------------------------------------------------------------
# id 形状 / 0600 原子写
# ---------------------------------------------------------------------------


def test_account_id_shape_rejects_path_traversal():
    for good in ("a", "a@example.com", "acct-1b2c3d", "A_b.c@d-e"):
        assert is_valid_account_id(good)
        assert ACCOUNT_ID_RE.fullmatch(good)
    for bad in ("../etc/passwd", "a/b", "", ".hidden", "-lead", "a" * 65, None, 7):
        assert not is_valid_account_id(bad)
    with pytest.raises(ValueError):
        validate_account_id("../etc/passwd")


def test_cred_path_is_built_from_validated_id(tmp_path):
    store = _store(tmp_path)
    assert store.cred_path("a@b.c") == tmp_path / "ch" / "a@b.c.json"
    with pytest.raises(ValueError):
        store.cred_path("../x")


def test_atomic_write_is_0600_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "nested" / "index.json"
    atomic_write_json(target, {"version": 1, "accounts": []})
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert [p.name for p in target.parent.iterdir()] == ["index.json"]
    atomic_write_json(target, {"version": 1, "accounts": [{"id": "a"}]})
    assert json.loads(target.read_text(encoding="utf-8"))["accounts"] == [{"id": "a"}]


# ---------------------------------------------------------------------------
# 互斥
# ---------------------------------------------------------------------------


def test_guard_serializes_writers_sharing_a_dir(tmp_path):
    """两个 store 实例（模拟网关进程 + CLI）对同一目录互斥。"""
    first_store = _store(tmp_path)
    second_store = _store(tmp_path)
    order: list[str] = []
    first_inside = threading.Event()
    release_first = threading.Event()
    second_inside = threading.Event()

    def hold_first() -> None:
        with first_store.guard():
            order.append("first")
            first_inside.set()
            release_first.wait(5)

    def take_second() -> None:
        with second_store.guard():
            order.append("second")
            second_inside.set()

    t1 = threading.Thread(target=hold_first)
    t2 = threading.Thread(target=take_second)
    t1.start()
    assert first_inside.wait(5)
    t2.start()
    assert not second_inside.wait(0.3), "第一个写入者没释放，第二个不该进临界区"
    release_first.set()
    t1.join(5)
    t2.join(5)
    assert order == ["first", "second"]


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------


def test_upsert_appends_with_next_priority_and_0600(tmp_path):
    store = _store(tmp_path)
    first = _upsert(store, _cred("a"))
    second = _upsert(store, _cred("b"))
    assert (first["priority"], second["priority"]) == (0, 1)
    assert first["added_at"] > 0
    assert oct(store.cred_path("a").stat().st_mode & 0o777) == "0o600"
    assert oct(store.index_path().stat().st_mode & 0o777) == "0o600"


def test_upsert_matches_identity_and_keeps_priority_alias_and_added_at(tmp_path):
    store = _store(tmp_path)
    first = _upsert(store, _cred("a"))
    store.rename("a", "主号")
    # 换 id、换 refresh_token，但 uid 相同 → 还是同一个顺位
    again = _upsert(store, _cred("renamed", uid="uid-a", refresh_token="rt-new"))
    assert again["id"] == "a"
    assert again["priority"] == first["priority"]
    assert again["added_at"] == first["added_at"]
    assert again["alias"] == "主号"
    assert store.load_cred("a")["refresh_token"] == "rt-new"
    assert store.index_path().read_text(encoding="utf-8").count('"id"') == 1


def test_upsert_derives_id_when_missing_and_writes_it_back(tmp_path):
    store = _store(tmp_path)
    cred = {"token": "t", "uid": "u", "refresh_token": "rt-x"}
    entry = _upsert(store, cred)
    assert entry["id"] == "rt-x"
    assert cred["account_id"] == "rt-x"  # 就地写回，调用方拿到即可用


def test_upsert_enforces_max_accounts_with_caller_error_type(tmp_path):
    class QuotaError(RuntimeError):
        pass

    store = _store(tmp_path, max_accounts=2)
    _upsert(store, _cred("a"))
    _upsert(store, _cred("b"))
    with pytest.raises(QuotaError, match="账号数已达上限 2"):
        _upsert(store, _cred("c"), error=QuotaError)


def test_upsert_without_max_accounts_is_unbounded(tmp_path):
    """上限为 ``None``（各通道默认值）时不限制账号数——历史 8 个上限已放开。"""
    store = _store(tmp_path, max_accounts=None)
    for i in range(20):
        _upsert(store, _cred(f"u{i}"))
    assert len(store.entries_locked()) == 20


def test_upsert_zero_max_accounts_means_unlimited(tmp_path):
    """``0`` 与 ``None`` 同义：都是不限制。"""
    store = _store(tmp_path, max_accounts=0)
    for i in range(12):
        _upsert(store, _cred(f"z{i}"))
    assert len(store.entries_locked()) == 12


def test_upsert_entry_fields_can_prefer_existing_value(tmp_path):
    """``previous`` 让通道实现「已有值优先」（region/name 这类合并语义）。"""
    store = _store(tmp_path)
    _upsert(store, _cred("a"), entry_fields=lambda c, prev: {"region": "global"})
    entry = _upsert(
        store,
        _cred("a", region=""),
        entry_fields=lambda c, prev: {
            "region": str(c.get("region") or (prev or {}).get("region") or "cn")
        },
    )
    assert entry["region"] == "global"


def test_upsert_sync_cred_runs_before_files_are_written(tmp_path):
    """``sync_cred`` 的改写要落到 cred 文件里（trae 用它把解析后的 region 写回）。"""
    store = _store(tmp_path)
    _upsert(store, _cred("a"), sync_cred=lambda c, entry: c.update(region="set-by-sync"))
    assert store.load_cred("a", required_field="token")["region"] == "set-by-sync"


def test_upsert_identity_scope_keeps_regions_apart(tmp_path):
    """``identity_scope`` 限定身份匹配的条目集：同 uid 跨区不得合并。

    codebuddy 的 CN / 海外账号体系独立，uid 可能撞车；合并会拿一个区的凭据
    顶掉另一个区的顺位。按 region 隔离后，两边各占一个条目。
    """
    store = _store(tmp_path)

    def upsert(cred, region):
        return store.upsert(
            cred,
            required_field="token",
            derive_id=lambda c: str(c["token"]),
            identity_fields=("uid",),
            identity_scope=lambda entry: str(entry.get("region") or "cn") == region,
            entry_fields=lambda c, prev: {
                "region": str((prev or {}).get("region") or region)
            },
            error=ValueError,
        )

    cn = upsert({"token": "cn-1", "uid": "same"}, "cn")
    global_ = upsert({"token": "gl-1", "uid": "same"}, "global")
    assert (cn["id"], global_["id"]) == ("cn-1", "gl-1")
    assert (cn["priority"], global_["priority"]) == (0, 1)
    # 同区内再登录仍然命中原条目，不会追加第三个顺位
    again = upsert({"token": "cn-2", "uid": "same"}, "cn")
    assert again["id"] == "cn-1"
    assert [e["id"] for e in store.list_entries()] == ["cn-1", "gl-1"]


def test_load_cred_requires_field_when_declared(tmp_path):
    """``required_field`` 是「这份凭据还能不能用」的判据：缺了当不存在。"""
    store = _store(tmp_path)
    _upsert(store, _cred("a"))
    assert store.load_cred("a", required_field="token") is not None
    store.write_cred("a", {"account_id": "a", "token": ""})
    assert store.load_cred("a", required_field="token") is None


# ---------------------------------------------------------------------------
# 自愈 / 删除 / 重排 / 改名
# ---------------------------------------------------------------------------


def test_list_entries_self_heals_and_preserves_unknown_fields(tmp_path):
    store = _store(tmp_path)
    _upsert(store, _cred("a"))
    _upsert(store, _cred("b"))
    # 给索引条目加一个通道自己的字段（模拟未来新增的索引字段）
    index = json.loads(store.index_path().read_text(encoding="utf-8"))
    index["accounts"][0]["future_field"] = "keep-me"
    store.write_index(index["accounts"])
    # b 的凭据文件被删 → 下次枚举自动剔除，同时保留 a 的新字段
    store.cred_path("b").unlink()
    entries = store.list_entries(required_field="token")
    assert [e["id"] for e in entries] == ["a"]
    assert entries[0]["future_field"] == "keep-me"
    assert [e["id"] for e in store.entries_locked()] == ["a"]


def test_list_entries_drops_invalid_id_entries(tmp_path):
    store = _store(tmp_path)
    _upsert(store, _cred("a"))
    store.write_index([{"id": "../evil", "priority": 0}, {"id": "ghost", "priority": 1}])
    assert store.list_entries() == []


def test_delete_removes_entry_and_cred_file(tmp_path):
    store = _store(tmp_path)
    _upsert(store, _cred("a"))
    assert store.delete("a") is True
    assert store.list_entries() == []
    assert not store.cred_path("a").exists()
    assert store.delete("a") is False


def test_reorder_rewrites_priority_and_requires_complete_unique_list(tmp_path):
    store = _store(tmp_path)
    _upsert(store, _cred("a"))
    _upsert(store, _cred("b"))
    ordered = store.reorder(["b", "a"])
    assert [e["id"] for e in ordered] == ["b", "a"]
    assert [e["priority"] for e in ordered] == [0, 1]
    with pytest.raises(ValueError):
        store.reorder(["b"])
    with pytest.raises(ValueError):
        store.reorder(["a", "a"])


def test_rename_trims_clears_and_rejects_unknown(tmp_path):
    store = _store(tmp_path)
    _upsert(store, _cred("a"))
    store.rename("a", "  工作号  ")
    assert store.list_entries()[0]["alias"] == "工作号"
    # 改名只动索引条目，不往 cred 文件里塞字段
    assert "alias" not in store.load_cred("a")
    store.rename("a", "")
    assert store.list_entries()[0]["alias"] == ""
    with pytest.raises(ValueError):
        store.rename("ghost", "x")


# ---------------------------------------------------------------------------
# 额度缓存代
# ---------------------------------------------------------------------------


class _Ref:
    def __init__(self, account_id: str, priority: int):
        self.id = account_id
        self.priority = priority


def test_quota_epoch_fingerprint_tracks_accounts_and_order():
    assert quota_epoch([]) == "empty"
    assert quota_epoch([_Ref("a", 0), _Ref("b", 1)]) == "a#0,b#1"
    assert quota_epoch([_Ref("b", 0), _Ref("a", 1)]) != quota_epoch(
        [_Ref("a", 0), _Ref("b", 1)]
    )


def test_quota_epoch_from_falls_back_to_unknown_on_error():
    assert quota_epoch_from(lambda: [_Ref("a", 0)]) == "a#0"

    def broken():
        raise RuntimeError("state dir unreadable")

    assert quota_epoch_from(broken) == "unknown"


# ---------------------------------------------------------------------------
# 通道是否真的在用
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [
        "trae",
        "qoder",
        "codex",
        "kimi",
        "antigravity",
        "codebuddy_provider",
    ],
)
def test_all_multi_account_channels_use_the_shared_store(module):
    """六个多账号通道的 credentials 必须持有同一个底座实例，而不是各写一份。"""
    import importlib

    credentials = importlib.import_module(f"buddy_proxy.{module}.credentials")
    assert isinstance(credentials._STORE, AccountStore), (
        f"{module}/credentials.py 没走 core.account_store："
        "索引/凭据文件的原子写与 flock 会再次分叉"
    )
    # 通道里的锁必须是同一个对象，否则调用方绕开 guard 时不再互斥
    assert credentials._index_lock is credentials._STORE.lock
    with credentials._index_file_lock():
        pass
    # 文件锁必须落在同一个 state dir 的 index.lock 上（绑定方法不是同一个对象，
    # 所以比路径而不是比 is）
    assert credentials._STORE.state_dir() / "index.lock" == (
        credentials._index_file_lock()._path
    )
