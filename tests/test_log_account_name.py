"""请求日志「通道」列账号名解析：落库存稳定 id，展示端按当前 alias 链出名字。

用户反馈（2026-10-07）：日志里 ``codebuddy (46d852…)`` 这种 id 缩略看不出
是谁，要求交互可见处一律用账号名；且改名后要用**新**名字——所以名字不能
写死在日志里，由 ``/ui/api/logs`` 返回时实时解析（``account_name`` 字段），
改名后历史日志行跟着变。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from buddy_proxy.web.ui import queries


@pytest.fixture(autouse=True)
def _clean_cache():
    queries._reset_account_name_cache()
    yield
    queries._reset_account_name_cache()


def _fake_source(monkeypatch, rows, counter=None):
    """注册一个假的 ``buddy_proxy.fakeprov.failover`` 并指给解析表。"""
    def accounts_status():
        if counter is not None:
            counter.append(1)
        return {"accounts": rows}

    mod = SimpleNamespace(accounts_status=accounts_status)
    monkeypatch.setitem(sys.modules, "buddy_proxy.fakeprov.failover", mod)
    monkeypatch.setattr(queries, "_ACCOUNT_NAME_SOURCES", ("fakeprov",))
    # PAT 段在 _account_name_map 里是**无条件**读取的（不走 _ACCOUNT_NAME_SOURCES），
    # 而它读的是 TRAE_PAT_BEARER_PROFILES 环境变量。开发机上配了 10 个真实账号时，
    # 这些名字会混进断言里，测试就红了——必须显式清空，测试才对环境无依赖。
    from buddy_proxy.trae import pat as pat_mod
    monkeypatch.setattr(pat_mod, "accounts_status", lambda **_: {"accounts": []})


def test_account_name_map_resolves_alias_chain(monkeypatch):
    _fake_source(monkeypatch, [
        {"id": "46d852ab", "alias": "工作号", "nickname": "nb", "uid": "u1"},
        {"id": "uuid-2", "alias": "", "nickname": None, "name": "备用号"},
        {"id": "uuid-3", "alias": None, "email": "a@b.c"},
        {"id": "uuid-4", "alias": None},                       # 无名字 → 不进表
        {"id": "uuid-5", "alias": "uuid-5"},                   # 名字=id → 不进表
        "bad-row",
    ])
    m = queries._account_name_map()
    assert m == {"46d852ab": "工作号", "uuid-2": "备用号", "uuid-3": "a@b.c"}


def test_account_name_map_caches_within_ttl(monkeypatch):
    counter: list[int] = []
    _fake_source(monkeypatch, [{"id": "a1", "alias": "甲"}], counter)
    queries._account_name_map()
    queries._account_name_map()
    assert counter == [1], "60s TTL 内应命中缓存，不重复读各通道 index.json"


def test_account_name_map_swallows_broken_channel(monkeypatch):
    # 某通道 failover 抛错（如凭据目录损坏）不该拖垮整个解析表
    boom = SimpleNamespace(accounts_status=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setitem(sys.modules, "buddy_proxy.fakeprov.failover", boom)
    monkeypatch.setattr(queries, "_ACCOUNT_NAME_SOURCES", ("fakeprov",))
    # 同上：PAT 段无条件读环境变量，必须隔离，否则本机配了账号时这条必然红。
    from buddy_proxy.trae import pat as pat_mod
    monkeypatch.setattr(pat_mod, "accounts_status", lambda **_: {"accounts": []})
    assert queries._account_name_map() == {}


def test_account_name_map_includes_traepat_profile_names(monkeypatch):
    """PAT 没 alias，也要给通道列一个与额度卡一致的可读账号名。"""
    _fake_source(monkeypatch, [])
    import buddy_proxy.trae.pat as pat
    monkeypatch.setattr(
        pat, "accounts_status",
        lambda **_kwargs: {"accounts": [
            {"id": "sa_primary", "display_index": 2},
            {"id": "sa_backup", "display_index": 1},
        ]},
    )

    assert queries._account_name_map() == {
        "sa_primary": "PAT #2（sa_primary）",
        "sa_backup": "PAT #1（sa_backup）",
    }


def test_attach_account_names(monkeypatch):
    _fake_source(monkeypatch, [{"id": "a1", "alias": "工作号"}])
    rows = [
        {"account": "a1"},
        {"account": "unknown-id"},
        {"account": ""},
        {"account": None},
    ]
    import asyncio
    asyncio.run(queries._attach_account_names(rows))
    assert rows[0]["account_name"] == "工作号"
    for r in rows[1:]:
        assert "account_name" not in r, "解析不到就不加字段，前端原样显示"


def test_ui_logs_enriches_rows(monkeypatch):
    """端到端：/ui/api/logs 返回的行带 account_name。"""
    from buddy_proxy import __main__ as m
    from buddy_proxy.core import state as st
    from fastapi.testclient import TestClient

    _fake_source(monkeypatch, [{"id": "a1", "alias": "工作号"}])

    class FakeMetrics:
        def query_logs(self, *_a, **_k):
            return {"rows": [{"provider": "codebuddy", "account": "a1"}],
                    "total": 1, "page": 1, "page_size": 20, "pages": 1,
                    "clients": [], "from_disk": False}

    monkeypatch.setattr(st, "proxy_state", SimpleNamespace(metrics=FakeMetrics()))
    monkeypatch.setenv("BUDDY_PROXY_ADMIN_OPEN", "1")
    r = TestClient(m.app).get("/ui/api/logs")
    assert r.status_code == 200
    assert r.json()["rows"][0]["account_name"] == "工作号"


def test_charts_js_prefers_account_name():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "src" / "buddy_proxy" / "web"
          / "static" / "chart_log.js").read_text(encoding="utf-8")
    assert "r.account_name || r.account" in js, "展示优先用解析出的账号名"
    assert "账号 id：" in js, "有解析名时 hover 应展示原始 id（对账用）"
    assert 'class="muted acct-name"' in js, "长 alias（rename 允许 64 字符）要限宽省略"


def test_trae_writes_stable_id_to_log():
    """trae 历史上写的是当时的 alias——统一改成落库存 id（展示端解析当前名）。"""
    import inspect
    from buddy_proxy.trae.provider import TraeProvider

    # Follow the inherited implementation rather than pinning its source file;
    # a responsibility split must not weaken the stable-account-id assertion.
    src = inspect.getsource(TraeProvider.forward)
    assert 'meta["account"] = acct.id' in src
    assert 'meta["account"] = acct.alias' not in src


def test_raccoon_registered_as_account_name_source():
    """raccoon 落库的是稳定 id（``Raccoon-pu-<uuid>`` 派生），必须注册进解析表，
    否则日志「通道」列只会显示 ``pu-01a20449…`` 这种 id 缩略，看不出是谁。"""
    assert "raccoon" in queries._ACCOUNT_NAME_SOURCES


def test_raccoon_account_name_resolves_via_failover(monkeypatch):
    """端到端：raccoon 的 alias 链要能进日志账号名解析表。"""
    from buddy_proxy.raccoon import failover as rfailover

    monkeypatch.setattr(
        rfailover, "accounts_status",
        lambda: {"accounts": [
            {"id": "Raccoon-pu-01a20449-aaaa", "alias": "主号",
             "name": "旧昵称"},
            {"id": "Raccoon-pu-9b3f11cc-bbbb", "alias": "",
             "name": "备用号"},
        ]},
    )
    queries._reset_account_name_cache()
    names = queries._account_name_map()
    assert names.get("Raccoon-pu-01a20449-aaaa") == "主号"
    assert names.get("Raccoon-pu-9b3f11cc-bbbb") == "备用号"
