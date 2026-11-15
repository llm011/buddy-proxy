"""AntigravityProvider 纯逻辑测试（models/health/quota，不发起网络请求）。"""

from __future__ import annotations


import pytest


@pytest.fixture
def provider():
    from buddy_proxy.antigravity.provider import AntigravityProvider

    return AntigravityProvider()


def test_models_table(provider):
    models = provider.models()
    assert models, "models 表为空"
    ids = {m["id"] for m in models}
    assert "gemini-3.1-pro" in ids and "claude-sonnet-4-6" in ids
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
            "gemini-3.1-pro-low": _quota(0.9, "2026-10-02T20:28:46Z"),
            "gemini-3.1-pro-high": _quota(0.8, "2026-10-02T20:28:46Z"),
            "gemini-3.6-flash-medium": _quota(0.5, "2026-10-02T20:28:46Z"),
            "gemini-3.8-flash-tiered": _quota(0.7),  # 缺 resetTime → 并回主池
            "claude-sonnet-4-6": _quota(1.0, "2026-10-02T20:30:52Z"),
            "claude-opus-4-6-thinking": _quota(1.0, "2026-10-02T20:30:52Z"),
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
    assert "gemini-3.6-flash" in gemini["note"]
    assert gemini["models_in_group"] == ["gemini-3.1-pro", "gemini-3.6-flash", "gemini-3.8-flash"]
    assert cgpt["models_in_group"] == ["claude-opus-4-6-thinking", "claude-sonnet-4-6", "gpt-oss-120b"]


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
            "gemini-3.1-pro-low": {"quotaInfo": {"remainingFraction": 1, "resetTime": "2026-10-04T21:40:18Z"}},
            "gemini-3.6-flash-medium": {"quotaInfo": {"remainingFraction": 1, "resetTime": "2026-10-04T21:40:18Z"}},
            "claude-sonnet-4-6": {"quotaInfo": {"remainingFraction": 1, "resetTime": "2026-10-04T21:40:18Z"}},
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
            "gemini-3.6-flash-medium": {"quotaInfo": {"remainingFraction": 0.5}},
            "gemini-3.1-pro-low": {"quotaInfo": {"remainingFraction": 0.9,
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
            "gemini-3.1-pro-low": {"quotaInfo": {"remainingFraction": 1.5,
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
            "gemini-3.1-pro-low": {"quotaInfo": {"remainingFraction": 0.9,
                                                 "resetTime": "2026-10-02T20:28:46Z"}},
            "claude-sonnet-4-6": {"quotaInfo": {"remainingFraction": 1.0}},
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
    assert by_id["gemini-3.8-flash"].get("upstream") == "gemini-3.8-flash-tiered"
    assert by_id["gpt-oss-120b"].get("upstream") == "gpt-oss-120b-medium"
    assert by_id["gemini-3.1-pro"].get("efforts") == ["low", "high"]
    assert by_id["gemini-3.6-flash"].get("default_effort") == "medium"
    # claude 系裸名可用，无 efforts
    assert "efforts" not in by_id["claude-sonnet-4-6"]
