"""traeintl「↻ 重新拉取」模型目录：发现 / 过滤 / 代际收敛（2026-10-12）。

海外 SG 网关 ``get_detail_param`` 与 PAT plus 网关同端点（实测 48 条），实现
``refresh_models`` 后模型页自动出「重新拉取」按钮（``models_api`` 的
refreshable 机制）。三块行为在这里锁定：

- **fetch_intl_catalog 过滤**：剔 custom_model* 占位、内部功能配置
  （summary/fast_apply 等）、config_switch=False；
- **refresh_models 全量账号**：区内 list_accounts 不剔冷却（冷却只挡转发，
  目录发现被剔会让冷却号独有模型静默漏掉——benefits 枚举同款教训）；
  单账号失败跳过，全失败抛错保留旧快照；
- **代际收敛 + 黑名单**：glm-5.2 在 glm-5.3 在场时不再展示（用户拍板），
  kimi-k2.5 等点名件任何时候不进列表。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from test_checkin_ignores_cooldown import _trae_cred


def _global_cred(uid: str, nickname: str) -> dict:
    return {**_trae_cred(uid, nickname), "region": "global"}


def _entry(name: str, switch: bool = True, dev_ctx: int | None = 200000) -> dict:
    """上游 get_detail_param 的原始条目形状（fetch_intl_catalog 的输入）。"""
    ctx = {"dev": dev_ctx} if dev_ctx is not None else None
    return {"config_name": name, "config_switch": switch,
            "context_window_tokens": ctx}


def _catalog(name: str, ctx: int | None = 200000) -> dict:
    """fetch_intl_catalog 的输出条目形状（fake_fetch 的返回值）。"""
    return {"name": name, "context_window": ctx}


@pytest.fixture()
def fake_fetch(monkeypatch):
    """把 intl_catalog.fetch_intl_catalog 换成按账号回目录的假身。"""
    calls: list[str] = []

    def install(by_account: dict[str, list[dict]]):
        # ``fail_all`` 模式：by_account 为空 dict 时，所有账号一律失败
        #（{} 与「没配这个账号」不同——dict 布尔性会把空 dict 当成
        #「全失败开关」处理，见 _fetch 里对 fail_all 的判断）。
        def _fetch(cred: dict) -> list[dict]:
            uid = str(cred.get("uid") or "")
            calls.append(uid)
            fail_all = not by_account
            entries = None if fail_all else by_account.get(uid)
            if entries is None:
                raise RuntimeError(f"海外模型目录网络错误：account {uid}")
            return entries
        monkeypatch.setattr("buddy_proxy.trae.intl_catalog.fetch_intl_catalog", _fetch)
        return calls
    return install


def _save_globals(monkeypatch, creds: list[dict]):
    from buddy_proxy.trae.credentials import save_account_cred

    for c in creds:
        save_account_cred(c)


async def _make_provider():
    from buddy_proxy.trae.intl_provider import TraeIntlProvider

    p = TraeIntlProvider()
    # ensure_account_token 会想真刷 token（access_token 过期前直接放行），
    # 假 token 未过期，理论上不触网——但为防 fixture 时间边界，仍然打桩。
    return p


# --- fetch_intl_catalog 解析/过滤 -------------------------------------------


def test_fetch_intl_catalog_filters_and_parses(monkeypatch):
    from buddy_proxy.trae import intl_catalog

    payload = {
        "config_info_list": [
            _entry("glm-5.3"),
            _entry("custom_model_placeholder"),
            _entry("custom_model_gpt-6"),
            _entry("summary", dev_ctx=216000),
            _entry("fast_apply", dev_ctx=40000),
            _entry("input_optimization", dev_ctx=44096),
            _entry("title_generation", dev_ctx=44096),
            _entry("gpt-6-sol", switch=False),          # 开关关 = 下架
            {"config_name": ""},                        # 脏数据
            "not-a-dict",
            _entry("kimi-k3", dev_ctx=None),            # 无 ctx 也收，None 透传
        ],
    }

    def fake_urlopen(req, timeout=None):
        class R:
            def read(self):
                return json.dumps(payload).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return R()

    monkeypatch.setattr(intl_catalog.urllib.request, "urlopen", fake_urlopen)
    entries = intl_catalog.fetch_intl_catalog(_global_cred("u9", "U9"))
    assert [(e["name"], e["context_window"]) for e in entries] == [
        ("glm-5.3", 200000), ("kimi-k3", None),
    ]


def test_fetch_intl_catalog_rejects_cn_account():
    from buddy_proxy.trae.intl_catalog import fetch_intl_catalog

    with pytest.raises(RuntimeError, match="region"):
        fetch_intl_catalog({**_trae_cred("u1", "U1"), "region": "cn"})


def test_fetch_intl_catalog_empty_is_failure(monkeypatch):
    from buddy_proxy.trae import intl_catalog

    payload = {"config_info_list": [_entry("summary")]}  # 过滤后为空

    def fake_urlopen(req, timeout=None):
        class R:
            def read(self):
                return json.dumps(payload).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return R()

    monkeypatch.setattr(intl_catalog.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="为空"):
        intl_catalog.fetch_intl_catalog(_global_cred("u9", "U9"))


# --- refresh_models：并集 / 容错 / 不剔冷却 ----------------------------------


def test_refresh_unions_accounts_and_tolerates_failure(fake_fetch, monkeypatch):
    from buddy_proxy.trae import failover as trae_failover
    from buddy_proxy.trae.intl_provider import TraeIntlProvider

    _save_globals(monkeypatch, [
        _global_cred("u1", "U1"), _global_cred("u2", "U2"), _trae_cred("u3", "U3CN"),
    ])
    trae_failover._cooldowns.clear()
    # u1 挂冷却：目录发现不受影响（冷却只挡转发）
    import time as _t
    trae_failover._cooldowns["u1"] = (_t.time() + 600, "quota")
    calls = fake_fetch({
        "u1": [_catalog("glm-5.3")],
        "u2": [_catalog("gpt-6.1-sol")],  # u3 不在表里 = 拉取失败，被跳过
    })
    try:
        p = TraeIntlProvider()
        models = asyncio.run(p.refresh_models())
    finally:
        trae_failover._cooldowns.clear()
    assert set(calls) >= {"u1", "u2"}          # 冷却号照拉；CN 号不拉
    ids = {m["id"] for m in models}
    assert "glm-5.3" in ids and "gpt-6.1-sol" in ids
    # 静态底表仍在（models() = 底表 + 发现）
    assert "kimi-k3" in ids


def test_refresh_all_failed_keeps_old_snapshot(fake_fetch, monkeypatch):
    from buddy_proxy.trae.intl_provider import TraeIntlProvider

    _save_globals(monkeypatch, [_global_cred("u1", "U1")])
    calls = fake_fetch({"u1": [_catalog("glm-5.3")]})
    p = TraeIntlProvider()
    asyncio.run(p.refresh_models())
    assert "glm-5.3" in {m["id"] for m in p.models()}

    # 第二轮全失败：抛错且旧快照保留
    fake_fetch({})
    with pytest.raises(RuntimeError, match="全部账号均未成功"):
        asyncio.run(p.refresh_models())
    assert "glm-5.3" in {m["id"] for m in p.models()}


def test_refresh_no_global_accounts_raises():
    from buddy_proxy.trae.intl_provider import TraeIntlProvider

    # conftest 隔离的 state dir 里没有 global 账号（只放 cn 也一样空）
    from buddy_proxy.trae.credentials import save_account_cred
    save_account_cred(_trae_cred("u3", "U3CN"))
    p = TraeIntlProvider()
    with pytest.raises(RuntimeError, match="没有海外账号"):
        asyncio.run(p.refresh_models())


# --- 代际收敛 + 黑名单 --------------------------------------------------------


def test_old_generation_hidden_when_newer_exists(fake_fetch, monkeypatch):
    from buddy_proxy.trae.intl_provider import TraeIntlProvider

    _save_globals(monkeypatch, [_global_cred("u1", "U1")])
    fake_fetch({"u1": [_catalog("glm-5.3"), _catalog("gpt-6.1-sol")]})
    p = TraeIntlProvider()
    models = asyncio.run(p.refresh_models())
    ids = {m["id"] for m in models}
    # 底表的 glm-5.2 被发现的 glm-5.3 顶掉；gpt-5.6/6 系全被 6.1-sol 顶掉
    # （6-luna 同是 ('gpt', 6.0)——数字代际可比，新代在场旧代就让位）
    assert "glm-5.2" not in ids and "glm-5.3" in ids
    assert "gpt-5.6-sol" not in ids and "gpt-6.1-sol" in ids
    assert "gpt-6-luna" not in ids
    # 代号系（解析不出版本：minimax-m3）不参与代际收敛，永远保留
    assert "minimax-m3" in ids


def test_never_listed_models_always_hidden(fake_fetch, monkeypatch):
    from buddy_proxy.trae.intl_provider import TraeIntlProvider

    _save_globals(monkeypatch, [_global_cred("u1", "U1")])
    fake_fetch({"u1": [_catalog("kimi-k2.5"), _catalog("gpt-5.2"),
                       _catalog("minimax-m2.7"), _catalog("glm-5.3")]})
    p = TraeIntlProvider()
    ids = {m["id"] for m in asyncio.run(p.refresh_models())}
    for gone in ("kimi-k2.5", "gpt-5.2", "minimax-m2.7"):
        assert gone not in ids


def test_discovered_entry_shape(fake_fetch, monkeypatch):
    from buddy_proxy.trae.intl_provider import TraeIntlProvider

    _save_globals(monkeypatch, [_global_cred("u1", "U1")])
    fake_fetch({"u1": [_catalog("gpt-6.1-sol", ctx=272000)]})
    p = TraeIntlProvider()
    models = asyncio.run(p.refresh_models())
    entry = next(m for m in models if m["id"] == "gpt-6.1-sol")
    assert entry["owned_by"] == "traeintl"
    assert entry["context_window"] == 272000
    assert entry["images"] is False  # 未实测不声明读图
