"""AntigravityProvider 纯逻辑测试（models/health/quota，不发起网络请求）。"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def provider():
    from buddy_proxy.antigravity.provider import AntigravityProvider

    return AntigravityProvider()


def test_models_table(provider):
    models = provider.models()
    assert models, "models 表为空"
    ids = {m["id"] for m in models}
    # 2026-11-15 实测可用的一批（3.1-pro/3.6-flash 404、claude-4-6 空壳已下架）
    assert {"gemini-3.8-flash", "gemini-4-hydrogen", "gemini-4-argon",
            "claude-opus-5-5", "claude-sonnet-5-5", "gpt-oss-120b"} <= ids
    assert not {"gemini-3.1-pro", "gemini-3.6-flash",
                "claude-opus-4-6-thinking", "claude-sonnet-4-6"} & ids
    for m in models:
        assert m["owned_by"] == "antigravity"
        assert m["object"] == "model"


def test_health(provider, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    assert provider.health()["configured"] is False

    from buddy_proxy.antigravity import credentials as creds

    creds.save_cred({"access_token": "a", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00",
                     "email": "u@x.com", "project_id": "p", "tier": "free-tier"})
    health = provider.health()
    assert health["configured"] is True
    assert health["email"] == "u@x.com"
    assert health["project_id"] == "p"


def test_quota_without_cred(provider, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    assert provider.quota() is None


def test_quota_epoch_tracks_account_list(provider):
    """缓存代随账号列表变化——benefits 的 quota 缓存键带上它，加/删号后旧快照立刻失效。

    实测过的问题（2026-10-03）：缓存键原来只有 provider id（TTL 300s），加了
    账号 #2 后界面还顶着单账号旧快照，看起来就像多账号额度被合并成一份。
    """
    from buddy_proxy.antigravity import credentials as creds

    assert provider.quota_epoch() == "empty"

    creds.save_account_cred({"access_token": "a", "refresh_token": "r",
                             "expiry": "2099-01-01T00:00:00+00:00",
                             "email": "u@x.com", "project_id": "p1"})
    one = provider.quota_epoch()
    assert one == "u@x.com#0", one  # priority 从 0 起算（前端 index 才是 +1）

    creds.save_account_cred({"access_token": "b", "refresh_token": "r2",
                             "expiry": "2099-01-01T00:00:00+00:00",
                             "email": "v@y.com", "project_id": "p2"})
    two = provider.quota_epoch()
    assert two != one, "加了账号缓存代必须变，否则旧快照继续顶满 TTL"
    assert "u@x.com#0" in two and "v@y.com#1" in two, two


def test_quota_falls_back_to_note_on_fetch_failure(provider, tmp_path, monkeypatch):
    """fetchAvailableModels 失败 → 退化为静态说明，不画假进度条。"""
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    from buddy_proxy.antigravity import credentials as creds

    creds.save_cred({"access_token": "a", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00", "email": "u@x.com",
                     "project_id": "p"})
    monkeypatch.setattr(provider, "_fetch_available_models",
                        lambda account_id=None: (_ for _ in ()).throw(RuntimeError("down")))
    quota = provider.quota()
    assert quota is not None and len(quota["items"]) == 1
    assert quota["items"][0]["percent"] is None  # 拿不到数据就不画进度条


def test_quota_aggregates_groups(provider, tmp_path, monkeypatch):
    """fetchAvailableModels 有数据 → 按组聚合（upstream 前缀匹配变体名，组内取最小）。

    percent=已用（前端进度条语义），remaining/total 千分制；reset_ts 取组内**任一
    带 resetTime** 的模型的值（不强制最紧那个，见下方断言）。
    """
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    from buddy_proxy.antigravity import credentials as creds

    creds.save_cred({"access_token": "a", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00", "email": "u@x.com",
                     "project_id": "p"})

    def _quota(frac, reset=None):
        info = {"remainingFraction": frac}
        if reset:
            info["resetTime"] = reset
        return {"quotaInfo": info}

    async def _fake_fetch(account_id=None):
        # 真实响应形态：models.<变体名>.quotaInfo.remainingFraction。
        # 同池模型共享同一 resetTime（2026-11-15 实测），这里照实刻画；
        # 个别变体缺 resetTime 也应并回主池，不能碎成单模型条目。
        return {"models": {
            "gemini-3.8-flash-tiered": _quota(0.9, "2026-10-02T20:28:46Z"),
            "gemini-4-hydrogen-high": _quota(0.5, "2026-10-02T20:28:46Z"),
            "gemini-4-argon-medium": _quota(0.8, "2026-10-02T20:28:46Z"),
            "gemini-3.8-flash-low": _quota(0.7),  # 缺 resetTime → 并回主池
            "claude-sonnet-5-5-medium": _quota(1.0, "2026-10-02T20:30:52Z"),
            "claude-opus-5-5-medium": _quota(1.0, "2026-10-02T20:30:52Z"),
            "gpt-oss-120b-medium": _quota(0.99, "2026-10-02T20:30:52Z"),
            "chat_20706": _quota(0.1),  # 内部条目不该被算进来
        }}

    # _fetch 被 quota() 包进 _run_sync（asyncio.run），monkeypatch 掉外层拿原始数据
    import buddy_proxy.antigravity.provider as prov

    monkeypatch.setattr(provider, "_fetch_available_models", _fake_fetch)
    monkeypatch.setattr(prov, "_run_sync", lambda factory: prov.asyncio.run(factory()))

    quota = provider.quota()
    assert len(quota["items"]) == 2
    by_label = {i["label"]: i for i in quota["items"]}
    gemini = by_label["Gemini 组"]
    cgpt = by_label["Claude/GPT 组"]
    # 池内跨模型取最小：gemini 组 min(0.9,0.8,0.5,0.7)=0.5；claude-gpt 组 min(1.0,1.0,0.99)=0.99
    assert gemini["remaining"] == 500.0 and gemini["total"] == 1000
    assert gemini["percent"] == 50.0  # 已用
    assert gemini["used"] == 500.0    # 已用千分制（与 remaining 同量纲，1-worst 反推）
    assert cgpt["remaining"] == 990.0 and cgpt["percent"] == 1.0
    assert cgpt["used"] == 10.0
    # reset_ts = 池内任一 resetTime（不强制最紧那个）：gemini 主池是 20:28:46Z。
    assert gemini["reset_ts"] == prov._iso_to_epoch("2026-10-02T20:28:46Z")
    assert cgpt["reset_ts"] == prov._iso_to_epoch("2026-10-02T20:30:52Z")
    # reset_note 讲清「这只是 5 小时窗口滚动刷新点」
    assert "5 小时" in gemini["reset_note"]
    # note 里点名最紧模型 + 列出池内模型清单
    # models_in_group 按名字排序；最紧的是谁看 note。
    assert gemini["models_in_group"] == ["gemini-3.8-flash", "gemini-4-argon",
                                         "gemini-4-hydrogen"]
    assert "gemini-4-hydrogen" in gemini["note"]  # 最紧（0.5）的点名
    assert sorted(cgpt["models_in_group"]) == sorted(
        ["claude-sonnet-5-5", "claude-opus-5-5", "gpt-oss-120b"])
    # 缺 resetTime 的变体不能单独成池：reset_ts 只反映「有 resetTime 的多数派」
    assert prov._iso_to_epoch("2026-10-02T20:28:46Z") == gemini["reset_ts"]
    assert not any("tiered" in i["label"] for i in quota["items"])


def test_quota_pool_split_ignores_missing_reset_in_fixture():
    """``_exact_pools`` 单测：缺 resetTime 并回主池，其余 resetTime 精确各成一池。"""
    import buddy_proxy.antigravity.provider as prov

    fracs = [
        (0.9, prov._iso_to_epoch("2026-11-15T12:28:46Z"), "gemini-3.8-flash-tiered"),
        (0.9, prov._iso_to_epoch("2026-11-15T12:28:46Z"), "gemini-4-hydrogen-high"),
        (0.5, 0.0, "gemini-no-reset"),
        (0.8, prov._iso_to_epoch("2026-11-22T08:12:41Z"), "gemini-4-argon-high"),
    ]
    pools = prov._exact_pools(fracs)
    assert len(pools) == 2
    assert [n for _f, _r, n in pools[0]] == [
        "gemini-3.8-flash-tiered", "gemini-4-hydrogen-high", "gemini-no-reset"]
    assert [n for _f, _r, n in pools[1]] == ["gemini-4-argon-high"]

    # 全部缺 resetTime / 空输入：不分裂，也不抛异常
    assert prov._exact_pools([]) == []
    only_missing = [(0.4, 0.0, "a"), (0.3, 0.0, "b")]
    assert len(prov._exact_pools(only_missing)) == 1

    # 只差几分钟的 resetTime 也是独立池（同池实测严格相等，容差并池是误伤）
    near = [
        (0.9, prov._iso_to_epoch("2026-11-15T12:28:46Z"), "gemini-3.8-flash-tiered"),
        (0.9, prov._iso_to_epoch("2026-11-15T12:28:46Z"), "gemini-4-hydrogen-high"),
        (0.6, prov._iso_to_epoch("2026-11-15T12:33:46Z"), "gemini-other"),
    ]
    near_pools = prov._exact_pools(near)
    assert len(near_pools) == 2 and [n for _f, _r, n in near_pools[1]] == ["gemini-other"]


def test_quota_splits_argon_weekly_pool(provider, tmp_path, monkeypatch):
    """argon 的周池 resetTime 比其余 Gemini 晚一周 → 单独一条，不被 hydrogen 掩盖。

    2026-11-15 实测（两个账号一致）：hydrogel/3.8/旧代共享 5 小时池（resetTime 当日），
    gemini-4-argon 三档属另一周池（resetTime 落在一周后）。旧实现按 group 合并、
    组内取 min，argon 独立水位被 hydrogen 掩盖，界面只能看到一个混合数。
    """
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    from buddy_proxy.antigravity import credentials as creds

    creds.save_cred({"access_token": "a", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00", "email": "u@x.com",
                     "project_id": "p"})

    async def _fake_fetch(account_id=None):
        return {"models": {
            "gemini-3.8-flash-tiered": {"quotaInfo": {"remainingFraction": 0.90,
                                                      "resetTime": "2026-11-15T12:28:46Z"}},
            "gemini-4-hydrogen-high": {"quotaInfo": {"remainingFraction": 0.90,
                                                     "resetTime": "2026-11-15T12:28:46Z"}},
            "gemini-4-argon-high": {"quotaInfo": {"remainingFraction": 0.80,
                                                  "resetTime": "2026-11-22T08:12:41Z"}},
            "gemini-4-argon-low": {"quotaInfo": {"remainingFraction": 0.80,
                                                 "resetTime": "2026-11-22T08:12:41Z"}},
        }}

    import buddy_proxy.antigravity.provider as prov

    monkeypatch.setattr(provider, "_fetch_available_models", _fake_fetch)
    monkeypatch.setattr(prov, "_run_sync", lambda factory: prov.asyncio.run(factory()))
    # 模拟 refresh_models 后目录里已有这些模型（argon 是动态发现的，静态表里没有）。
    provider._models = [
        {"id": "gemini-3.8-flash", "upstream": "gemini-3.8-flash-tiered", "group": "gemini"},
        {"id": "gemini-4-hydrogen", "group": "gemini"},
        {"id": "gemini-4-argon", "group": "gemini"},
    ]

    items = provider.quota()["items"]
    labels = [i["label"] for i in items]
    # 两条 Gemini 池（其余模型 + Argon 周池），各自独立读数
    assert "Gemini 组（其余 Gemini 模型 5 小时池）" in labels
    assert "Gemini 组（Gemini 4 Argon 周池）" in labels
    by_label = {i["label"]: i for i in items}
    shared = by_label["Gemini 组（其余 Gemini 模型 5 小时池）"]
    argon = by_label["Gemini 组（Gemini 4 Argon 周池）"]
    assert shared["remaining"] == 900.0
    assert argon["remaining"] == 800.0  # 之前会被 min(0.9,0.8)=800 混在一起
    assert argon["models_in_group"] == ["gemini-4-argon"]
    assert shared["models_in_group"] == ["gemini-3.8-flash", "gemini-4-hydrogen"]
    assert argon["reset_ts"] == prov._iso_to_epoch("2026-11-22T08:12:41Z")
    assert shared["reset_ts"] == prov._iso_to_epoch("2026-11-15T12:28:46Z")


def test_quota_pool_labels_stay_unique_when_pools_unrecognized(provider, tmp_path, monkeypatch):
    """两个独立池都不含 argon → 通用后缀会撞车，兜底加重置时刻保证 label 唯一。"""
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    from buddy_proxy.antigravity import credentials as creds

    creds.save_cred({"access_token": "a", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00", "email": "u@x.com",
                     "project_id": "p"})

    async def _fake_fetch(account_id=None):
        return {"models": {
            "gemini-3.8-flash-tiered": {"quotaInfo": {"remainingFraction": 0.9,
                                                      "resetTime": "2026-11-15T12:28:46Z"}},
            "gemini-4-hydrogen-high": {"quotaInfo": {"remainingFraction": 0.9,
                                                     "resetTime": "2026-11-15T12:28:46Z"}},
            "gemini-5-neo-high": {"quotaInfo": {"remainingFraction": 0.6,
                                                "resetTime": "2026-11-21T03:00:00Z"}},
        }}

    import buddy_proxy.antigravity.provider as prov

    monkeypatch.setattr(provider, "_fetch_available_models", _fake_fetch)
    monkeypatch.setattr(prov, "_run_sync", lambda factory: prov.asyncio.run(factory()))
    provider._models = [
        {"id": "gemini-3.8-flash", "upstream": "gemini-3.8-flash-tiered", "group": "gemini"},
        {"id": "gemini-4-hydrogen", "group": "gemini"},
        {"id": "gemini-5-neo", "group": "gemini"},
    ]

    labels = [i["label"] for i in provider.quota()["items"]]
    assert len(labels) == 2
    assert len(set(labels)) == 2  # 不撞车
    assert any("重置" in lab for lab in labels)


def test_quota_full_quota_reads_as_full_not_zero(provider, tmp_path, monkeypatch):
    """上游满额（所有 remainingFraction=1）时，读数必须是「满额」而不是被读成 0。

    实测背景（2026-10-04，两个 g1-pro-tier 账号）：上游对每个模型都只回
    remainingFraction=1 + resetTime=当下+5h，且只在逼近池上限时才下调。旧实现把
    percent（已用）留成 0、进度条画成空的，用户把「已用 0%」读成「额度是 0」。
    这里锁住：满额时 remaining==total、used==0，进度条语义（percent=已用）为 0。
    """
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    from buddy_proxy.antigravity import credentials as creds

    creds.save_cred({"access_token": "a", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00", "email": "u@x.com",
                     "project_id": "p"})

    async def _fake_fetch(account_id=None):
        return {"models": {
            "gemini-3.8-flash-tiered": {"quotaInfo": {"remainingFraction": 1, "resetTime": "2026-10-04T21:40:18Z"}},
            "gemini-4-hydrogen-xhigh": {"quotaInfo": {"remainingFraction": 1, "resetTime": "2026-10-04T21:40:18Z"}},
            "claude-sonnet-5-5-medium": {"quotaInfo": {"remainingFraction": 1, "resetTime": "2026-10-04T21:40:18Z"}},
            "gpt-oss-120b-medium": {"quotaInfo": {"remainingFraction": 1, "resetTime": "2026-10-04T21:40:18Z"}},
        }}

    import buddy_proxy.antigravity.provider as prov

    monkeypatch.setattr(provider, "_fetch_available_models", _fake_fetch)
    monkeypatch.setattr(prov, "_run_sync", lambda factory: prov.asyncio.run(factory()))

    quota = provider.quota()
    assert len(quota["items"]) == 2
    for it in quota["items"]:
        assert it["remaining"] == 1000.0 and it["total"] == 1000
        assert it["used"] == 0.0 and it["percent"] == 0.0
        assert "满额" in it["note"]
    assert quota["level"] == "free-tier"  # 凭据没写 tier_name 时的兜底


def test_quota_reset_falls_back_when_tightest_has_no_resettime(provider, tmp_path, monkeypatch):
    """最紧的那个模型恰好没带 resetTime 时，仍用组内别的模型补上——不整组丢 reset。

    上游可以只给部分变体回 resetTime（实测内部条目 chat_*/tab_* 就没有）。若
    reset_ts 硬取「最紧那个」，而它恰好没带，整组就不显示重置时间——信息白丢。
    """
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    from buddy_proxy.antigravity import credentials as creds

    creds.save_cred({"access_token": "a", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00", "email": "u@x.com",
                     "project_id": "p"})

    async def _fake_fetch(account_id=None):
        return {"models": {
            # 最紧的（0.5）没有 resetTime，另一个有
            "gemini-4-hydrogen-high": {"quotaInfo": {"remainingFraction": 0.5}},
            "gemini-3.8-flash-tiered": {"quotaInfo": {"remainingFraction": 0.9,
                                                      "resetTime": "2026-10-02T20:28:46Z"}},
        }}

    import buddy_proxy.antigravity.provider as prov

    monkeypatch.setattr(provider, "_fetch_available_models", _fake_fetch)
    monkeypatch.setattr(prov, "_run_sync", lambda factory: prov.asyncio.run(factory()))

    quota = provider.quota()
    it = next(i for i in quota["items"] if i["label"] == "Gemini 组")
    assert it["remaining"] == 500.0          # 水位仍按最紧的算
    assert it["reset_ts"] == prov._iso_to_epoch("2026-10-02T20:28:46Z")  # 但 reset 没丢


def test_quota_clamps_out_of_range_fraction(provider, tmp_path, monkeypatch):
    """上游发 >1 的 remainingFraction（余量）时钳到 100%，不渲染「剩 1,500 / 1,000」。"""
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    from buddy_proxy.antigravity import credentials as creds

    creds.save_cred({"access_token": "a", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00", "email": "u@x.com",
                     "project_id": "p"})

    async def _fake_fetch(account_id=None):
        return {"models": {
            "gemini-3.8-flash-tiered": {"quotaInfo": {"remainingFraction": 1.5,
                                                      "resetTime": "2026-10-02T20:28:46Z"}},
        }}

    import buddy_proxy.antigravity.provider as prov

    monkeypatch.setattr(provider, "_fetch_available_models", _fake_fetch)
    monkeypatch.setattr(prov, "_run_sync", lambda factory: prov.asyncio.run(factory()))

    it = next(i for i in provider.quota()["items"] if i["label"] == "Gemini 组")
    assert it["remaining"] == 1000.0 and it["used"] == 0.0 and it["percent"] == 0.0
    assert "满额" in it["note"]


def test_models_json_load_fallback(tmp_path, monkeypatch):
    """models.json 损坏时兜底单模型，不崩。"""
    import buddy_proxy.antigravity.provider as prov

    monkeypatch.setattr(prov, "_MODELS_JSON",
                        tmp_path / "broken.json")  # 不存在 → OSError 分支
    models = prov._load_models()
    assert models and models[0]["id"] == "gemini-3.8-flash"
    (tmp_path / "broken.json").write_text("{bad")
    assert prov._load_models()[0]["id"] == "gemini-3.8-flash"


def test_endpoint_order_matches_reference():
    """daily 优先、prod 兜底（参考实现同序），写死防手滑改反。"""
    from buddy_proxy.antigravity.provider import ENDPOINTS

    assert ENDPOINTS[0].startswith("https://daily-cloudcode-pa")
    assert ENDPOINTS[1] == "https://cloudcode-pa.googleapis.com"


def test_iso_to_epoch():
    from buddy_proxy.antigravity.provider import _iso_to_epoch

    assert _iso_to_epoch("2026-10-02T20:28:46Z") > 0
    assert _iso_to_epoch("2026-10-02T20:28:46+08:00") > 0
    assert _iso_to_epoch("") == 0.0
    assert _iso_to_epoch(None) == 0.0
    assert _iso_to_epoch("garbage") == 0.0


def test_quota_multi_account_labels_and_failure_notice(provider, tmp_path, monkeypatch):
    """多账号：并发查询、label 带 AG #N 前缀、失败账号不拖垮整页只插说明条。"""
    import buddy_proxy.antigravity.provider as prov
    from buddy_proxy.antigravity import credentials as creds

    creds.save_account_cred({"access_token": "a", "refresh_token": "r",
                             "expiry": "2099-01-01T00:00:00+00:00",
                             "email": "u@x.com", "project_id": "p"})
    creds.save_account_cred({"access_token": "a2", "refresh_token": "r2",
                             "expiry": "2099-01-01T00:00:00+00:00",
                             "email": "v@y.com", "project_id": "p2"})
    monkeypatch.setattr(provider, "_fetch_available_models", lambda account_id: (
        (_ for _ in ()).throw(RuntimeError("down")) if account_id == "v@y.com"
        else {"models": {
            "gemini-3.8-flash-tiered": {"quotaInfo": {"remainingFraction": 0.9,
                                                      "resetTime": "2026-10-02T20:28:46Z"}},
            "claude-sonnet-5-5-medium": {"quotaInfo": {"remainingFraction": 1.0}},
        }}))
    # _quota_one 的工厂直跑（绕开临时事件循环，mock 是普通函数不是 coroutine）
    monkeypatch.setattr(prov, "_run_sync", lambda factory: factory())

    quota = provider.quota()
    items = quota["items"]
    assert items[0]["query_failed"] is True  # 说明条插首位（benefits 认标记走短缓存）
    assert "1/2 个账号查询失败" in items[0]["remaining"]
    ag1 = [i for i in items if i["label"].startswith("AG #1 · ")]
    assert len(ag1) == 2  # u@x.com 的 Gemini/Claude 两组
    # 按 label 取而不是按位置：分池后条目顺序由 resetTime 决定，位置不再稳定。
    gemini = next(i for i in ag1 if i["label"] == "AG #1 · Gemini 组")
    assert gemini["remaining"] == 900.0 and gemini["percent"] == 10.0
    assert gemini["reset_ts"] == prov._iso_to_epoch("2026-10-02T20:28:46Z")
    assert not any(i["label"].startswith("AG #2") for i in items)  # 失败账号无条目


def test_health_multi_account_lists_accounts(provider, tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds

    creds.save_account_cred({"access_token": "a", "refresh_token": "r",
                             "expiry": "2099-01-01T00:00:00+00:00",
                             "email": "u@x.com", "project_id": "p", "tier": "free-tier"})
    creds.save_account_cred({"access_token": "a2", "refresh_token": "r2",
                             "expiry": "2099-01-01T00:00:00+00:00",
                             "email": "v@y.com", "project_id": "p2"})
    health = provider.health()
    assert health["configured"] is True
    assert health["email"] == "u@x.com" and health["project_id"] == "p"  # 主账号语义兼容
    assert [a["email"] for a in health["accounts"]] == ["u@x.com", "v@y.com"]
    assert health["accounts"][1]["project_id"] == "p2"


def test_models_table_upstream_names():
    """模型表的 upstream 真名必须是 fetchAvailableModels 实测可用的形态。"""
    import buddy_proxy.antigravity.provider as prov

    by_id = {m["id"]: m for m in prov.MODELS}
    # upstream 是「裸基名」，档位由 efforts 后缀拼：gemini-3.8-flash + tiered。
    # 曾写成 upstream=gemini-3.8-flash-tiered，结果拼出 -tiered-tiered（404）。
    assert by_id["gemini-3.8-flash"].get("upstream") == "gemini-3.8-flash"
    from buddy_proxy.antigravity.convert import apply_effort_suffix

    assert apply_effort_suffix(by_id["gemini-3.8-flash"], None) == "gemini-3.8-flash-tiered"
    assert apply_effort_suffix(by_id["gemini-4-argon"], "high") == "gemini-4-argon-high"
    assert apply_effort_suffix(by_id["claude-opus-5-5"], None) == "claude-opus-5-5-medium"
    assert by_id["gpt-oss-120b"].get("upstream") == "gpt-oss-120b-medium"
    assert by_id["gemini-4-hydrogen"].get("efforts") == ["low", "medium", "high", "xhigh"]
    assert by_id["gemini-3.8-flash"].get("default_effort") == "tiered"
    assert by_id["gemini-4-argon"].get("default_effort") == "medium"
    # claude 系裸名可用，无 upstream 覆盖
    assert "upstream" not in by_id["claude-sonnet-5-5"]


# ---------------------------------------------------------------------------
# 单次消耗估算（水位落差）：PoolKey / _credit_probe / _ReplayStream 结算
# ---------------------------------------------------------------------------

def _iso(text: str) -> float:
    import buddy_proxy.antigravity.provider as prov

    return prov._iso_to_epoch(text)


def _ag_quota(frac, reset=None):
    info = {"remainingFraction": frac}
    if reset:
        info["resetTime"] = reset
    return {"quotaInfo": info}


def test_pool_key_maps_model_to_its_pool(provider):
    """上游模型名 → 池键（组|resetTime），与面板分池同口径；读不到就空串。"""
    reset = "2026-10-02T20:28:46Z"
    data = {"models": {
        "gemini-3.8-flash-tiered": _ag_quota(0.99, reset),
        "gemini-4-hydrogen-low": _ag_quota(0.98, reset),
        "claude-sonnet-5-5-medium": _ag_quota(1.0, "2026-10-02T20:30:52Z"),
    }}
    import buddy_proxy.antigravity.provider as prov
    from buddy_proxy.antigravity.convert import apply_effort_suffix

    epoch = int(prov._iso_to_epoch(reset))
    # 运行期传的是**上游真名**（apply_effort_suffix 拼过档位），不是裸基名。
    tid = apply_effort_suffix(provider._model_by_id["gemini-3.8-flash"], None)
    assert provider._pool_key(data, tid) == f"gemini|{epoch}"
    assert provider._pool_key(data, "gemini-9-nope") == ""


def test_credit_probe_uses_watermark_drop(provider, monkeypatch):
    """第一笔只铺基线（None）；水位下降后第二笔按落差 × 系数估算。"""
    from buddy_proxy.core import credit_estimate

    credit_estimate._QUOTA_MARKS.clear()
    reset = "2026-10-02T20:28:46Z"
    fracs = iter([0.9990, 0.9950])

    async def _fake_fetch(account_id):
        return {"models": {"gemini-3.8-flash-tiered": _ag_quota(next(fracs), reset)}}

    monkeypatch.setattr(provider, "_fetch_watermark", _fake_fetch)
    async def _run():
        first = await provider._credit_probe("u@x.com", "gemini-3.8-flash-tiered", tokens=120)
        assert first == (None, None)  # 没有上一条水位可比
        return await provider._credit_probe("u@x.com", "gemini-3.8-flash-tiered", tokens=120)

    second, observed_ts = asyncio.run(_run())
    expected = round((0.9990 - 0.9950) * credit_estimate._ANTIGRAVITY_CREDITS_PER_WATERMARK, 2)
    assert second == expected
    assert observed_ts  # 本次观测时刻（诊断用）


def test_credit_probe_returns_none_when_flat_or_unreadable(provider, monkeypatch):
    """水位没降 / 查询失败 → None（记 —），不拿 0 冒充免费。"""
    from buddy_proxy.core import credit_estimate

    credit_estimate._QUOTA_MARKS.clear()
    reset = "2026-10-02T20:28:46Z"

    async def _flat(account_id):
        return {"models": {"gemini-3.8-flash-tiered": _ag_quota(1.0, reset)}}

    monkeypatch.setattr(provider, "_fetch_watermark", _flat)

    async def _flat_twice():
        await provider._credit_probe("u@x.com", "gemini-3.8-flash-tiered", tokens=120)
        return await provider._credit_probe("u@x.com", "gemini-3.8-flash-tiered", tokens=120)

    credit, observed_ts = asyncio.run(_flat_twice())
    assert credit is None  # 水位没降 → 记 —
    assert observed_ts  # 账本仍在，只是没跌

    async def _boom(account_id):
        raise RuntimeError("network down")

    monkeypatch.setattr(provider, "_fetch_watermark", _boom)

    async def _fail():
        return await provider._credit_probe("u@x.com", "gemini-3.8-flash-tiered", tokens=120)

    assert asyncio.run(_fail()) == (None, None)


def test_replay_stream_reports_usage_to_on_close():
    """转换器 aclose 收尾时把流里最后的 token 数交给 on_close，且只回调一次。"""
    import buddy_proxy.antigravity.provider as prov

    class _Resp:
        async def aclose(self):
            self.closed = True

    seen: list[tuple[int | None, int | None]] = []

    async def _on_close(p, c):
        seen.append((p, c))
        return 1.23

    resp = _Resp()
    replay = prov._ReplayStream(
        resp, ["data: {\"usageMetadata\": {\"promptTokenCount\": 157}}"],
        on_close=_on_close,
    )
    async def _drive():
        lines = [ln async for ln in replay.aiter_lines()]
        assert lines  # 缓冲行照常透传
        await replay.aclose()
        await replay.aclose()  # 幂等：只回调一次

    asyncio.run(_drive())
    assert seen == [(157, None)]


def test_settle_frame_books_single_request(provider, monkeypatch):
    """固定连接上水位逐条下降：每条收尾的落差就是这条请求的账。

    实测依据见 ``antigravity.provider._fetch_watermark``：同一连接上连发 4 条，
    收尾读数逐条单调下降（0.7562798 → 0.75617707 → …），落差与该条体量对得上。
    """
    from buddy_proxy.core import credit_estimate

    credit_estimate._QUOTA_MARKS.clear()
    reset = "2026-10-02T20:28:46Z"
    # 第一条铺基线，第二条跌 0.004，第三条再跌 0.001。
    fracs = iter([0.9990, 0.9950, 0.9940])

    async def _fake_fetch(account_id):
        return {"models": {"gemini-3.8-flash-tiered": _ag_quota(next(fracs), reset)}}

    monkeypatch.setattr(provider, "_fetch_watermark", _fake_fetch)
    first: dict = {"account": "u@x.com"}
    second: dict = {"account": "u@x.com"}
    third: dict = {"account": "u@x.com"}

    async def _run():
        c1 = await provider._settle_frame(
            "u@x.com", "gemini-3.8-flash-tiered", first, 100, 20)
        c2 = await provider._settle_frame(
            "u@x.com", "gemini-3.8-flash-tiered", second, 100, 20)
        c3 = await provider._settle_frame(
            "u@x.com", "gemini-3.8-flash-tiered", third, 100, 20)
        return c1, c2, c3

    c1, c2, c3 = asyncio.run(_run())
    unit = credit_estimate._ANTIGRAVITY_CREDITS_PER_WATERMARK
    assert c1 is None  # 第一条只铺基线
    assert c2 == round(0.004 * unit, 2) == 4.0
    assert c3 == round(0.001 * unit, 2) == 1.0
    assert "ag_credit" not in first
    assert second["ag_credit"] == 4.0
    assert third["ag_credit"] == 1.0


def test_settle_frame_skips_placeholder_frame(provider, monkeypatch):
    """没埋点（frame 不是 dict）时照常推进水位，但不往任何地方写。"""
    from buddy_proxy.core import credit_estimate

    credit_estimate._QUOTA_MARKS.clear()
    reset = "2026-10-02T20:28:46Z"
    fracs = iter([0.9990, 0.9950])

    async def _fake_fetch(account_id):
        return {"models": {"gemini-3.8-flash-tiered": _ag_quota(next(fracs), reset)}}

    monkeypatch.setattr(provider, "_fetch_watermark", _fake_fetch)

    async def _run():
        await provider._settle_frame("u@x.com", "gemini-3.8-flash-tiered", None, 100, 20)
        return await provider._settle_frame("u@x.com", "gemini-3.8-flash-tiered", None, 100, 20)

    # 第二条照样能结算出数值（给调用方返回），只是没有帧可写。
    assert asyncio.run(_run()) == round(
        0.004 * credit_estimate._ANTIGRAVITY_CREDITS_PER_WATERMARK, 2)


def test_record_quota_mark_rebases_on_rise(provider):
    """水位回升（换了连接快照 / 上游重算）：本次记 —，且基线跟到新读数。

    不跟的话，下一次真跌到 0.994 时拿 0.999 的旧基线比，落差会虚增一倍。
    """
    from buddy_proxy.core import credit_estimate

    credit_estimate._QUOTA_MARKS.clear()
    credit_estimate.record_quota_mark("u@x.com", "gemini|1", 0.9990)
    credit, _ = credit_estimate.record_quota_mark("u@x.com", "gemini|1", 0.9995)
    assert credit is None
    credit, _ = credit_estimate.record_quota_mark("u@x.com", "gemini|1", 0.9955)
    # 0.9995 → 0.9955 = 0.004，而不是 0.999 → 0.9955 的 0.0035。
    assert credit == 4.0


def test_record_quota_mark_keeps_baseline_when_unreadable(provider):
    """读不到水位时不动基线：下次观测仍是「与最后一条真实读数」的落差。"""
    from buddy_proxy.core import credit_estimate

    credit_estimate._QUOTA_MARKS.clear()
    credit_estimate.record_quota_mark("u@x.com", "gemini|1", 0.9990)
    credit, observed_ts = credit_estimate.record_quota_mark("u@x.com", "gemini|1", None)
    assert credit is None
    assert observed_ts  # 返回上一条真实读数的时刻，只是不结算
    credit, _ = credit_estimate.record_quota_mark("u@x.com", "gemini|1", 0.9960)
    assert credit == 3.0


def test_record_quota_mark_isolates_accounts(provider):
    """多账号各记各的基线，互不污染。"""
    from buddy_proxy.core import credit_estimate

    credit_estimate._QUOTA_MARKS.clear()
    credit_estimate.record_quota_mark("a@x.com", "gemini|1", 0.9990)
    credit_estimate.record_quota_mark("b@x.com", "gemini|1", 0.5000)
    a, _ = credit_estimate.record_quota_mark("a@x.com", "gemini|1", 0.9970)
    b, _ = credit_estimate.record_quota_mark("b@x.com", "gemini|1", 0.5000)
    assert a == 2.0
    assert b is None


def test_fetch_watermark_pins_connection(provider, monkeypatch):
    """积分探测必须复用同一个 client：换了连接就读到另一份快照（实测会跳）。"""
    import httpx

    import buddy_proxy.antigravity.provider as prov

    created: list[httpx.AsyncClient] = []

    class _Resp:
        status_code = 200
        request = None

        def json(self):
            return {"models": {"gemini-3.8-flash-tiered": _ag_quota(0.75, "2026-10-02T20:28:46Z")}}

        async def aclose(self):
            pass

    class _Client:
        is_closed = False

        def __init__(self, *a, **kw):
            created.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, req, stream=False):
            return _Resp()

        def build_request(self, *a, **kw):
            return None

    monkeypatch.setattr(prov.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(prov, "ensure_account_token", lambda aid: ("tok", {}))

    async def _run():
        await provider._fetch_watermark("u@x.com")
        await provider._fetch_watermark("u@x.com")
        # 面板路径仍然每次新建，两条路径不能混用同一个 client。
        await provider._fetch_available_models("u@x.com")

    asyncio.run(_run())
    assert len(created) == 2  # 探测 1 条（复用）+ 面板 1 条（新建）


def test_frame_credit_reads_settlement():
    """observability 从请求帧读结算值，读不到记 —。"""
    from buddy_proxy.codebuddy_provider.observability import _frame_credit

    meta: dict = {"ag_credit": 0.42}
    assert _frame_credit(meta) == 0.42
    assert _frame_credit({"ag_credit": None}) is None
    assert _frame_credit({}) is None
    assert _frame_credit(None) is None
