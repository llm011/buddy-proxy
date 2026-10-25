"""Trae CN 目录刷新：拉目录 + 抄倍率 + 只验证新名字（2026-10-25 改造）。

改造前「↻ 重新拉取」是纯探测（逐模型 ping、4001 剔除），既发现不了新模型
也不更新倍率。现在：

1. ``cn_catalog.fetch_cn_catalog`` 从 ``get_detail_param`` 拉候选目录，
   ``display_contact_config.consumption_rate.data.rate`` 就是客户端「模型倍率」
   面板显示的值，抄进 ``config.DISCOVERED_CREDITS``（不覆盖静态底表）；
2. ``refresh_models`` 只对**新名字**发一条真实请求验证（repo 铁律：名字被
   接受 ≠ 能被服务），4001 拒收的不并入；
3. 静态底表仍在前，发现的新名字排在后面。

全离线：上游用 monkeypatch 替身，不触碰真实账号。
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest

from buddy_proxy.trae import config as trae_config
from buddy_proxy.trae import cn_catalog


def _entry(name, rate=None, *, invisible=False, switch=True, multimodal=False,
           display=None, ctx=116000):
    contact = {"access": {"data": {"identity_list": [0, 1, 2, 3, 100]}}}
    if rate is not None:
        contact["consumption_rate"] = {"enable": True, "data": {"rate": rate}}
    return {
        "config_name": name,
        "config_switch": switch,
        "is_invisible_to_user": invisible,
        "context_window_tokens": {"dev": ctx},
        "display_config": {"display_name": display or name,
                           "multimodal": multimodal},
        # 实测两种形态都出现：字符串 / 对象
        "display_contact_config": json.dumps(contact),
    }


def _fake_urlopen(payload):
    def opener(req, timeout=None):
        class R:
            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R()

    return opener


# --- 目录拉取与过滤 ----------------------------------------------------------


def test_fetch_filters_internal_and_invisible(monkeypatch):
    payload = {"config_info_list": [
        _entry("kimi-k3", 1.83, multimodal=True, ctx=200000),
        _entry("minimax-m3.1-flash", 0.09),
        _entry("glm-5.1", 0.83, invisible=True),          # 用户不可见：不收
        _entry("custom_model_200k", 0.1),                 # 自定义占位
        _entry("summary", 0.1),                           # IDE 内部 function
        _entry("old-model", 0.5, switch=False),           # 已下架
    ]}
    monkeypatch.setattr(cn_catalog.urllib.request, "urlopen",
                        _fake_urlopen(payload))
    rows = cn_catalog.fetch_cn_catalog({"access_token": "t", "uid": "u"})
    assert [r["name"] for r in rows] == ["kimi-k3", "minimax-m3.1-flash"]
    assert [r["id"] for r in rows] == ["kimi-k3", "minimax-m3.1-flash"]
    assert rows[0]["rate"] == 1.83 and rows[0]["multimodal"] is True
    assert rows[0]["context_window"] == 200000


def test_fetch_filters_client_custom_models(monkeypatch):
    """用户自建模型（实测 BC-GLM-5.3-Flash / gpt-5.5）不进目录、不被探测。

    这类条目 ``config_name`` 不带 custom_model 前缀、``is_invisible_to_user``
    是 None 而非 True，只能靠 display_config 的 is_custom_model / builder 认。
    """
    custom = _entry("BC-GLM-5.3-Flash", None, display="BC-GLM-5.3-Flash")
    custom["display_config"]["is_custom_model"] = True
    builder = _entry("gpt-5.5", None, display="gpt-5.5")
    builder["display_config"]["builder"] = "user"
    monkeypatch.setattr(cn_catalog.urllib.request, "urlopen", _fake_urlopen(
        {"config_info_list": [custom, builder, _entry("glm-5.3", 0.39)]}))
    rows = cn_catalog.fetch_cn_catalog({"access_token": "t"})
    assert [r["id"] for r in rows] == ["glm-5.3"]


def test_fetch_accepts_object_shaped_display_config(monkeypatch):
    """display_contact_config 有时是对象而非字符串（实测两种都有）。"""
    entry = _entry("glm-5.3", 0.39)
    entry["display_contact_config"] = {
        "consumption_rate": {"enable": True, "data": {"rate": 0.39}}}
    monkeypatch.setattr(cn_catalog.urllib.request, "urlopen",
                        _fake_urlopen({"config_info_list": [entry]}))
    assert cn_catalog.fetch_cn_catalog({"access_token": "t"})[0]["rate"] == 0.39


def test_fetch_empty_is_failure(monkeypatch):
    monkeypatch.setattr(cn_catalog.urllib.request, "urlopen",
                        _fake_urlopen({"config_info_list": [_entry("summary")]}))
    with pytest.raises(RuntimeError, match="为空"):
        cn_catalog.fetch_cn_catalog({"access_token": "t"})


def test_fetch_refuses_global_account():
    with pytest.raises(RuntimeError, match="海外"):
        cn_catalog.fetch_cn_catalog({"access_token": "t", "region": "global"})


# --- refresh_models：新名字验证 / 倍率抄回 / 底表不动 --------------------------


@pytest.fixture()
def trae_env(monkeypatch):
    """隔离账号清单 + 清掉跨用例的运行时倍率快照。"""
    from buddy_proxy.trae import credentials

    accounts = [types.SimpleNamespace(id="1234567890", region="cn", priority=0)]
    import buddy_proxy.trae.provider as provider_mod

    # provider 模块在导入时直接绑定了一批 credentials 函数，两条都要打
    for mod in (credentials, provider_mod):
        monkeypatch.setattr(mod, "list_accounts", lambda: list(accounts))
    monkeypatch.setattr(credentials, "ensure_account_token",
                        lambda aid: ("tok", {"access_token": "tok"}))
    monkeypatch.setattr(credentials, "load_account_cred",
                        lambda aid: {"access_token": "tok", "region": "cn"})
    # provider 模块在导入时把 load_account_cred 绑成了本地名，两条都要打
    monkeypatch.setattr(provider_mod, "load_account_cred",
                        lambda aid: {"access_token": "tok", "region": "cn"})
    monkeypatch.setattr(trae_config, "DISCOVERED_CREDITS", {})
    return types.SimpleNamespace(accounts=accounts, credentials=credentials)


def _patch_catalog(monkeypatch, rows):
    """替身直接给「已解析」形态（与 cn_catalog 的返回形状一致）。"""
    resolved = [{
        "name": r["config_name"],
        "id": r["config_name"].lower(),
        "display_name": r["display_config"]["display_name"],
        "rate": (json.loads(r["display_contact_config"])
                 .get("consumption_rate") or {}).get("data", {}).get("rate"),
        "context_window": r["context_window_tokens"]["dev"],
        "multimodal": r["display_config"]["multimodal"],
    } for r in rows]
    monkeypatch.setattr(cn_catalog, "fetch_cn_catalog", lambda cred: list(resolved))


def test_refresh_copies_rate_and_adds_new_names(monkeypatch, trae_env):
    from buddy_proxy.trae.provider import TraeProvider

    _patch_catalog(monkeypatch, [
        _entry("Kimi-K3", 1.83, display="Kimi-K3", ctx=200000),
        _entry("Kimi-K3.1-Pro", 1.2, display="Kimi-K3.1-Pro", ctx=200000),
    ])
    verified = []
    monkeypatch.setattr(TraeProvider, "_probe_model",
                        lambda self, m: verified.append(m) or True)
    p = TraeProvider()
    models = asyncio.run(p.refresh_models(force=True))
    ids = [m["id"] for m in models]

    # 只有**缓存里没有**的新名字被验证；``kimi-k3.1-pro`` 已进静态底表，
    # 所以这里一次上游请求都不该发（旧实现是整表逐条 ping）
    assert verified == []
    # 名字与倍率进目录；大小写按上游 config_name 保留
    new = next(m for m in models if m["id"] == "kimi-k3.1-pro")
    assert new["credits"] == "x1.20"
    # 展示名与 id 相同时不重复塞 name 字段（客户端用 id 即可）
    assert "name" not in new
    assert new.get("context_window") == 200000
    # kimi-k3.1-pro 在场后 kimi-k3 不再列出（代际收敛），但底表值没被刷新改写
    assert "kimi-k3" not in ids
    assert trae_config.credit_multiplier("cn", "kimi-k3") == "x1.83"
    assert "kimi-k3.1-pro" in ids, "静态底表里的新条目仍在（mock 目录没给）"


def test_refresh_rejects_4001_candidates(monkeypatch, trae_env):
    from buddy_proxy.trae.provider import TraeProvider

    _patch_catalog(monkeypatch, [_entry("bogus-model", 0.5)])
    monkeypatch.setattr(TraeProvider, "_probe_model",
                        lambda self, m: False)   # 上游 4001 拒绝
    p = TraeProvider()
    ids = {m["id"] for m in asyncio.run(p.refresh_models(force=True))}
    assert "bogus-model" not in ids
    assert "x0.50" not in trae_config.DISCOVERED_CREDITS.values()


def test_refresh_keeps_rate_activities_out_of_static_table(monkeypatch, trae_env):
    """静态底表有人工核过的口径（活动折扣备注），刷新只补 **底表没有** 的模型。"""
    from buddy_proxy.trae.provider import TraeProvider

    _patch_catalog(monkeypatch, [_entry("glm-5.3", 0.07)])   # 上游促销价
    monkeypatch.setattr(TraeProvider, "_probe_model", lambda self, m: True)
    p = TraeProvider()
    models = asyncio.run(p.refresh_models(force=True))
    assert next(m for m in models if m["id"] == "glm-5.3")["credits"] == "x0.39"
    assert trae_config.DISCOVERED_CREDITS == {}


def test_refresh_all_success_prunes_delisted_models(monkeypatch, trae_env):
    """全号成功时快照按本轮结果替换：上游下架的名字与倍率一起消失。

    旧实现只 ``update``，下架的模型会一直挂在 /v1/models 到重启为止。
    """
    from buddy_proxy.trae.provider import TraeProvider

    p = TraeProvider()
    _patch_catalog(monkeypatch, [_entry("newm", 0.5)])
    monkeypatch.setattr(TraeProvider, "_probe_model", lambda self, m: True)
    asyncio.run(p.refresh_models(force=True))
    assert "newm" in {m["id"] for m in p.models()}
    assert trae_config.DISCOVERED_CREDITS.get("newm") == "x0.50"

    _patch_catalog(monkeypatch, [_entry("other", 0.1)])
    models = asyncio.run(p.refresh_models(force=True))
    assert "newm" not in {m["id"] for m in models}
    assert "newm" not in trae_config.DISCOVERED_CREDITS
    assert trae_config.DISCOVERED_CREDITS.get("other") == "x0.10"


def test_refresh_all_accounts_failed_keeps_old_snapshot(monkeypatch, trae_env):
    from buddy_proxy.trae.provider import TraeProvider

    def boom(cred):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(cn_catalog, "fetch_cn_catalog", boom)
    p = TraeProvider()
    with pytest.raises(RuntimeError, match="全部账号均未成功"):
        asyncio.run(p.refresh_models(force=True))
    assert {m["id"] for m in p.models()} >= {"glm-5.3", "kimi-k3.1-pro"}


def test_refresh_partial_failure_unions_success(monkeypatch, trae_env):
    from buddy_proxy.trae.provider import TraeProvider

    trae_env.accounts.append(
        types.SimpleNamespace(id="9876543210", region="cn", priority=1))

    # 第二个账号的凭据读不出来（模拟单号失败）；目录替身直接给「已解析」
    # 形状（与 cn_catalog 返回一致），注意别再调 _patch_catalog 覆盖它
    resolved = [{
        "name": "brand-new-model", "id": "brand-new-model",
        "display_name": "brand-new-model", "rate": 0.33,
        "context_window": 116000, "multimodal": False,
    }]
    monkeypatch.setattr(cn_catalog, "fetch_cn_catalog", lambda cred: list(resolved))
    # provider 模块在导入时把 load_account_cred 绑成本地名，这条也得打；
    # ensure_account_token 更是**函数内**才 import，只能打在 credentials 上
    # （见上面的 trae_env fixture）
    from buddy_proxy.trae import provider as provider_mod
    monkeypatch.setattr(provider_mod, "load_account_cred",
                        lambda aid: (None if aid == "9876543210"
                                     else {"access_token": "tok", "region": "cn"}))
    monkeypatch.setattr(TraeProvider, "_probe_model", lambda self, m: True)
    p = TraeProvider()
    p._discovered["kept-from-before"] = {"display_name": None,
                                         "context_window": None,
                                         "multimodal": False, "upstream": None}
    models = asyncio.run(p.refresh_models(force=True))
    assert "brand-new-model" in {m["id"] for m in models}
    # 部分成功只增不删：上一版发现的名字不能因单号抖动被下架
    assert "kept-from-before" in {m["id"] for m in models}


# --- 倍率取值口 --------------------------------------------------------------


def test_credit_multiplier_prefers_discovered_then_static(monkeypatch):
    monkeypatch.setattr(trae_config, "DISCOVERED_CREDITS",
                        {"brand-new": "x0.33", "glm-5.3": "x9.99"})
    assert trae_config.credit_multiplier("cn", "brand-new") == "x0.33"
    # 静态底表优先：人工核过的值不被运行时抄回值顶掉
    assert trae_config.credit_multiplier("cn", "glm-5.3") == "x0.39"
    # 别名按映射到内部名取同一倍率
    assert trae_config.credit_multiplier("cn", "deepseek-v4-pro-official") == "x0.72"
    # 海外没有倍率概念
    assert trae_config.credit_multiplier("global", "kimi-k3") is None


def test_static_catalog_lists_new_models():
    """2026-10-25 面板/目录核对后补进底表的条目（含 panel 灰锁的 k3.1-pro）。"""
    for mid, mult in [("kimi-k3.1-pro", "x1.20"),
                      ("minimax-m3.1-flash", "x0.09"),
                      ("glm-5.2", "x0.78"),
                      ("deepseek-v4-flash-official", "x0.20"),
                      ("deepseek-v4-pro-official", "x0.72")]:
        assert mid in trae_config.MODEL_IDS, mid
        assert trae_config.credit_multiplier("cn", mid) == mult, mid
