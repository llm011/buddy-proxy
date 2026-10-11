"""Trae / Qoder 双区域（CN + 海外）基础设施与独立 intl provider 的单测。

覆盖计划里「region 解析、账号 region 落盘/读取、failover 同区过滤、额度
credits/dollar 渲染分叉、intl provider 注册条件」几项，全部离线不触网：

- ``trae.config`` 的 ``TraeRegion`` 取址（CN v2 / 海外 v1、签到只在 CN）；
- ``trae.credentials`` 的 ``_region_of`` 归一 + region 落盘/读取；
- ``trae.failover.available_accounts(region)`` 同区过滤；
- ``TraeProvider._quota_items`` 的 credits/dollar 量纲分叉（``unit`` 字段）；
- ``trae.benefits_api`` 海外签到**不发请求**直接回占位；
- ``TraeIntlProvider`` / ``QoderIntlProvider`` 的 id/region/签到开关；
- 两个 ``intl_enabled()`` 的注册条件（只看索引、按 region）；
- ``auth.login._pick_region`` 的 ``--region`` 别名归一。

conftest 已隔离 TRAE_WORK_STATE_DIR / QODER_STATE_DIR，落盘不碰真实账号。
"""
from __future__ import annotations

import time

import pytest

from buddy_proxy.auth.login import _pick_region
from buddy_proxy.trae import benefits_api, failover as trae_failover
from buddy_proxy.trae.config import (
    TRAE_REGIONS,
    model_tables,
    resolve_trae_region,
    work_function_override,
)
from buddy_proxy.trae.credentials import _region_of, save_account_cred
from buddy_proxy.trae.intl_provider import TraeIntlProvider
from buddy_proxy.trae.intl_provider import intl_enabled as trae_intl_enabled
from buddy_proxy.trae.provider import TraeProvider


def _trae_cred(uid: str, region: str = "cn") -> dict:
    return {
        "uid": uid, "nickname": uid.title(), "region": region,
        "access_token": f"at-{uid}", "refresh_token": f"rt-{uid}",
        "expires_at": int(time.time()) + 3600,
        "machine_id": "m", "device_id": "d", "api_host": "h",
        "enterprise_id": "",
    }


@pytest.fixture(autouse=True)
def _clear_cooldowns():
    trae_failover._cooldowns.clear()
    yield
    trae_failover._cooldowns.clear()


# ───────────────────────── TraeRegion 取址 ─────────────────────────


def test_cn_region_uses_v2_and_has_checkin():
    cn = resolve_trae_region("cn")
    assert cn.key == "cn" and cn.has_checkin and cn.billing == "credits"
    assert cn.usage_url().endswith("/trae/api/v2/pay/ide_user_ent_usage")
    assert "api.trae.cn" in cn.usage_url()


def test_global_region_uses_v1_and_no_checkin():
    gl = resolve_trae_region("global")
    assert gl.key == "global" and not gl.has_checkin and gl.billing == "dollar"
    # 海外额度是 v1（v2 在海外是 TLB 404），host 也是 growsg-normal
    assert gl.usage_url().endswith("/trae/api/v1/pay/ide_user_ent_usage")
    assert "growsg-normal.trae.ai" in gl.usage_url()


def test_global_oauth_and_auth_hosts():
    gl = TRAE_REGIONS["global"]
    assert gl.exchange_token_url().startswith("https://api.trae.ai/")
    assert gl.authorization_url() == "https://www.trae.ai/authorization"
    assert gl.package_type == "stable_i18n"


def test_resolve_region_env_override(monkeypatch):
    monkeypatch.setenv("TRAE_REGION", "global")
    assert resolve_trae_region(None).key == "global"
    # 显式 key 优先于 env
    assert resolve_trae_region("cn").key == "cn"
    # 非法值回落默认（配置层容错，不抛）
    assert resolve_trae_region("火星").key == "global"


def test_model_tables_region_switch():
    cn_map = model_tables("cn")[0]
    gl_map = model_tables("global")[0]
    assert cn_map is not gl_map
    # CN 表非空（历史已有目录）；海外表实测前**刻意留空**（见 intl_provider 注释）
    assert cn_map
    assert gl_map == {}


# ───────────────────────── 账号 region 落盘 / 读取 ─────────────────────────


def test_region_of_normalizes_and_falls_back():
    assert _region_of({"region": "global"}) == "global"
    assert _region_of({"region": "GLOBAL"}) == "global"
    # 缺字段/非法值 → 默认区域（历史账号全是 CN）
    assert _region_of({}) == "cn"
    assert _region_of({"region": "zzz"}) == "cn"
    # fallback（重登同号时保住旧 region）优先于默认
    assert _region_of({}, fallback="global") == "global"
    # cred 自带 region 优先于 fallback
    assert _region_of({"region": "cn"}, fallback="global") == "cn"


def test_save_and_list_account_persists_region():
    from buddy_proxy.trae.credentials import list_accounts

    save_account_cred(_trae_cred("u-cn", region="cn"))
    save_account_cred(_trae_cred("u-gl", region="global"))
    regions = {a.uid: a.region for a in list_accounts()}
    assert regions["u-cn"] == "cn"
    assert regions["u-gl"] == "global"


def test_available_accounts_filters_by_region():
    """跨区账号不参与 failover（CN / 海外账号不通用，连错域 401）。"""
    save_account_cred(_trae_cred("cn-a", region="cn"))
    save_account_cred(_trae_cred("gl-a", region="global"))
    save_account_cred(_trae_cred("cn-b", region="cn"))
    assert [a.uid for a in trae_failover.available_accounts("cn")] == ["cn-a", "cn-b"]
    assert [a.uid for a in trae_failover.available_accounts("global")] == ["gl-a"]
    # 不给 region → 全量（供「本区无账号但别区有」的 401 判定用）
    assert len(trae_failover.available_accounts()) == 3


# ───────────────────────── 额度 credits / dollar 分叉 ─────────────────────────


def test_quota_items_credits_unit_for_cn():
    data = {
        "is_credits_billing": True,
        "usage_summary": {"total_amount": 2000, "consumed_amount": 500,
                          "consumption_ratio": 0.25},
        "user_entitlement_pack_list": [],
    }
    items = TraeProvider()._quota_items(data, label_prefix="")
    assert items[0]["unit"] == "credit"
    assert items[0]["remaining"] == 1500


def test_quota_items_dollar_unit_when_flag_set():
    # 上游 flag 是权威判据：即便 provider region 是 cn，flag=dollar 也按美元
    data = {
        "is_dollar_usage_billing": True,
        "usage_summary": {"total_amount": 20.0, "consumed_amount": 5.0,
                          "consumption_ratio": 0.25},
        "user_entitlement_pack_list": [],
    }
    items = TraeProvider()._quota_items(data, label_prefix="")
    assert items[0]["unit"] == "dollar", \
        "$20 套餐必须标 dollar，否则 benefits._quota_low 会用绝对值 300 门槛天天误报告急"
    assert items[0]["remaining"] == 15.0


def test_quota_items_dollar_unit_by_region_fallback():
    # flag 缺失时，海外 region 的 billing=dollar 兜底（老快照/字段改名不至于报成积分）
    data = {
        "usage_summary": {"total_amount": 20.0, "consumed_amount": 5.0,
                          "consumption_ratio": 0.25},
        "user_entitlement_pack_list": [],
    }
    items = TraeIntlProvider()._quota_items(data, label_prefix="")
    assert items[0]["unit"] == "dollar"


# ───────────────────────── 海外签到不发请求 ─────────────────────────


def test_checkin_status_skips_network_for_global(monkeypatch):
    def _boom(*a, **k):  # 海外分支若发请求就炸——本测试断言它不发
        raise AssertionError("海外签到不应发请求")

    monkeypatch.setattr(benefits_api, "_post_ug", _boom)
    got = benefits_api.fetch_checkin_status(region="global")
    assert got["no_checkin"] is True
    assert got["enable"] is False and got["checked_in"] is True


def test_claim_checkin_skips_network_for_global(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("海外签到不应发请求")

    monkeypatch.setattr(benefits_api, "_post_ug", _boom)
    got = benefits_api.claim_checkin_credits(region="global")
    assert got["no_checkin"] is True


# ───────────────────────── TraeIntlProvider ─────────────────────────


def test_trae_intl_provider_identity():
    p = TraeIntlProvider()
    assert p.id == "traeintl" and p.name == "Trae 海外版"
    assert p.supports_checkin is False, "海外无签到端点，不该进 /ui 打卡列表"
    assert p._region.key == "global"
    assert p._base_url == TRAE_REGIONS["global"].chat_base
    # 签到方法显式返回 None（supports_checkin 已挡一层，这里再兜一层）
    assert p.checkin_status() is None and p.checkin_claim() is None


def test_trae_intl_benefits_snapshot_has_quota_and_unsupported_checkin(
        tmp_path, monkeypatch):
    """真实 TraeIntlProvider 的海外额度能进 snapshot，签到能力明确关闭。"""
    import asyncio
    from types import SimpleNamespace

    from buddy_proxy.benefits import BenefitsManager

    # snapshot 会读取全局设置；显式隔离，绝不读取用户真实 settings.json。
    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(tmp_path / "settings.json"))
    account = save_account_cred(_trae_cred("gl-snapshot", region="global"))
    calls = []

    monkeypatch.setattr(
        "buddy_proxy.trae.provider.ensure_account_token",
        lambda account_id: (f"tok-{account_id}", {"uid": account_id}),
    )

    def _fetch_usage(token="", account_id="", region=""):
        calls.append({"token": token, "account_id": account_id, "region": region})
        return _intl_usage_fixture()

    monkeypatch.setattr("buddy_proxy.trae.provider.fetch_ent_usage", _fetch_usage)

    provider = TraeIntlProvider()

    def _unexpected_checkin(*_args, **_kwargs):
        raise AssertionError("Trae 海外版不应查询或领取签到")

    monkeypatch.setattr(provider, "checkin_status", _unexpected_checkin)
    monkeypatch.setattr(provider, "checkin_claim", _unexpected_checkin)

    manager = BenefitsManager(
        tmp_path / "checkin.jsonl",
        SimpleNamespace(providers={"traeintl": provider}),
    )
    # 避免默认 CodeBuddy 参与快照并触发与本契约无关的上游查询。
    monkeypatch.setattr(manager, "_providers", lambda: {"traeintl": provider})

    snapshot = asyncio.run(manager.snapshot())
    assert len(snapshot["providers"]) == 1
    entry = snapshot["providers"][0]
    assert entry["id"] == "traeintl"
    assert entry["name"] == "Trae 海外版"
    assert entry["checkin"] == {"supported": False}
    assert snapshot["checkin_enabled_providers"] == []

    quota = entry["quota"]
    assert quota["supported"] is True
    assert quota["level"] is None
    head, fast, basic = quota["items"]
    assert head["head_only"] is True and head["unit"] == "dollar"
    assert fast["label"] == "Pro Plan · Premium 快速请求"
    assert fast["used"] is None and fast["total"] == 600
    assert fast["unit"] == "count"
    assert basic["label"] == "Pro Plan · Basic 用量"
    assert basic["used"] == 0.0001 and basic["total"] == 20
    assert basic["unit"] == "dollar"
    assert calls == [{
        "token": f"tok-{account.id}",
        "account_id": account.id,
        "region": "global",
    }]

    result = asyncio.run(manager.claim_now("traeintl"))
    assert result == {"ok": False, "error": "provider traeintl 不支持打卡"}


def test_intl_quota_label_tag_differs_from_cn(monkeypatch):
    """两个通道并行在线时，额度行标不能都叫 ``Trae #N``（分不清哪张是美元）。"""
    _seed_quota_stubs(monkeypatch)
    save_account_cred(_trae_cred("gl1", region="global"))
    save_account_cred(_trae_cred("gl2", region="global"))

    cn_labels = _quota_labels(TraeProvider())            # CN 通道查不到本区账号
    intl_labels = _quota_labels(TraeIntlProvider())
    assert all(lab.startswith("Trae 海外版 #") for lab in intl_labels), intl_labels
    assert not any(lab.startswith("Trae 海外版") for lab in cn_labels)


def test_trae_intl_enabled_follows_index():
    assert trae_intl_enabled() is False, "没有海外账号时不注册 traeintl"
    save_account_cred(_trae_cred("cn-only", region="cn"))
    assert trae_intl_enabled() is False, "只有 CN 账号仍不注册"
    save_account_cred(_trae_cred("gl-one", region="global"))
    assert trae_intl_enabled() is True


def test_trae_intl_enabled_ignores_cooldown():
    """全冷却时仍注册（转发侧自然 429），别误报成「通道没开」。"""
    ref = save_account_cred(_trae_cred("gl-one", region="global"))
    trae_failover.mark_cooldown(ref.id)
    assert trae_intl_enabled() is True


def test_trae_intl_ensure_auth_requires_same_region():
    from fastapi import HTTPException

    p = TraeIntlProvider()
    # 只有 CN 账号 → 海外通道 ensure_auth 报 401（不能拿 CN 账号蒙混过关）
    save_account_cred(_trae_cred("cn-only", region="cn"))
    with pytest.raises(HTTPException) as exc:
        p.ensure_auth()
    assert exc.value.status_code == 401
    assert "global" in str(exc.value.detail)
    # 有海外账号 → 放行
    save_account_cred(_trae_cred("gl-one", region="global"))
    p.ensure_auth()  # 不抛即通过


# ───────────────────────── 展示序号同源（防删错账号） ─────────────────────────
#
# 管理页按「序号」把额度块对上账号快照，账号名 / ▲▼ 顺位 / ✕ 删除 / ✎ 改名
# 全拿它对出来的 id 操作——✕ 会 unlink 凭据文件，不可逆。所以额度标签的
# ``Trae #N`` 必须与 ``accounts_status()`` 的 ``index`` 同一套编号。
#
# 两侧遍历的列表不同：标签侧是「本区 + 未冷却」子集，快照侧是全量。子集位次
# 与全量位次一旦错位，✕ 就删到别人头上。错位有两个来源（冷却是改造前就存在
# 的老坑，跨区是双区域引入的新坑），故两个都要钉住。


def _seed_quota_stubs(monkeypatch):
    """把额度查询打桩成「每账号都成功返回同一份数据」，只验编号不触网。"""
    monkeypatch.setattr(
        "buddy_proxy.trae.provider.fetch_ent_usage",
        lambda token="", account_id="", region="": {
            "is_credits_billing": True,
            "usage_summary": {"total_amount": 100, "consumed_amount": 10,
                              "consumption_ratio": 0.1},
            "user_entitlement_pack_list": []})
    monkeypatch.setattr(
        "buddy_proxy.trae.provider.ensure_account_token",
        lambda aid: ("tok", {"uid": aid}))


def _quota_labels(provider) -> list[str]:
    """额度条目里的 ``Trae #N`` 组名（前端就是按它匹配账号快照的）。"""
    items = provider.quota()["items"]
    return sorted({it["label"].split(" · ")[0] for it in items
                   if " · " in it["label"]})


def _snapshot_index_by_uid() -> dict[str, int]:
    return {a["uid"]: a["index"]
            for a in trae_failover.accounts_status()["accounts"]}


def test_display_index_matches_accounts_status():
    """``display_index()`` 与快照 ``index`` 必须逐项相等（唯一权威）。"""
    save_account_cred(_trae_cred("u1"))
    save_account_cred(_trae_cred("u2"))
    didx = trae_failover.display_index()
    snap = {a["uid"]: a["index"]
            for a in trae_failover.accounts_status()["accounts"]}
    assert {uid: didx[uid] for uid in snap} == snap


def test_display_index_survives_delete_holes():
    """删号后 priority 会留空洞（0,2,3），位次编号不受影响。

    用 ``priority + 1`` 当序号的老写法在这里就对不上快照了（快照按位次
    enumerate），故编号必须走 ``display_index()``。
    """
    from buddy_proxy.trae.credentials import delete_account, list_accounts

    a = save_account_cred(_trae_cred("u1"))
    save_account_cred(_trae_cred("u2"))
    save_account_cred(_trae_cred("u3"))
    delete_account(a.id)

    prio = {x.uid: x.priority for x in list_accounts()}
    assert sorted(prio.values()) == [1, 2], "删号不重排 priority，留下空洞"
    # 位次是连续的 1..n，与快照同源
    assert sorted(trae_failover.display_index().values()) == [1, 2]
    assert sorted(trae_failover.accounts_status()["accounts"],
                  key=lambda x: x["index"])[0]["index"] == 1


def test_quota_labels_align_with_snapshot_when_account_cooling(monkeypatch):
    """额度枚举不再剔冷却账号（2026-10-11 修），但编号仍与快照逐一对上。

    旧行为（#130 时代）是「冷却号被额度枚举跳过」，断言的是跳过后不错位；
    全量枚举后防错位防线不变——display_index 用全量位次，冷却号也有自己
    的 #N 且指向**自己**，✕ / ▲▼ 不会指到别人（删错凭据的老坑）。
    """
    _seed_quota_stubs(monkeypatch)
    a1 = save_account_cred(_trae_cred("cn1"))
    save_account_cred(_trae_cred("cn2"))
    save_account_cred(_trae_cred("cn3"))
    trae_failover.mark_cooldown(a1.id, quota=True)

    snap = _snapshot_index_by_uid()
    labels = _quota_labels(TraeProvider())
    assert labels, "多账号应带 Trae #N 前缀"
    matched = [uid for lab in labels for uid, i in snap.items()
               if i == int(lab.split("#")[1])]
    # 冷却中的 cn1 现在也出现在额度里（#1 仍是它自己，不错位）
    assert sorted(matched) == ["cn1", "cn2", "cn3"], \
        f"额度块必须对上自己的编号（冷却中也不许错位/消失）：{matched}"


def test_quota_labels_align_with_snapshot_across_regions(monkeypatch):
    """海外账号排在前时，CN 通道的额度块不会错指到海外号。

    这是双区域引入的新错位来源：``_work_accounts()`` 只回本区账号，而快照是
    全量列表。错配的后果是管理页 ✕ 删掉**海外账号**的凭据文件。
    """
    _seed_quota_stubs(monkeypatch)
    save_account_cred(_trae_cred("gl1", region="global"))
    save_account_cred(_trae_cred("gl2", region="global"))
    save_account_cred(_trae_cred("cn1"))
    save_account_cred(_trae_cred("cn2"))

    snap = _snapshot_index_by_uid()
    labels = _quota_labels(TraeProvider())          # CN 通道
    matched = [uid for lab in labels for uid, i in snap.items()
               if i == int(lab.split("#")[1])]
    assert sorted(matched) == ["cn1", "cn2"], \
        f"CN 额度块指到了别的账号（可能是海外号，✕ 会删错凭据）：{matched}"
    assert not ({"gl1", "gl2"} & set(matched))


def test_quota_failure_notice_uses_same_numbering(monkeypatch):
    """失败说明条里的 ``#N`` 也要同一套编号（曾另用 ``priority + 1``）。"""
    save_account_cred(_trae_cred("gl1", region="global"))
    a1 = save_account_cred(_trae_cred("cn1"))
    save_account_cred(_trae_cred("cn2"))

    def _boom(token="", account_id="", region=""):
        raise RuntimeError("上游挂了")

    monkeypatch.setattr("buddy_proxy.trae.provider.fetch_ent_usage", _boom)
    monkeypatch.setattr(
        "buddy_proxy.trae.provider.ensure_account_token",
        lambda aid: ("tok", {"uid": aid}))

    snap = _snapshot_index_by_uid()
    items = TraeProvider().quota()["items"]
    notice = next(it for it in items if it.get("query_failed"))
    nums = [int(x.lstrip("#")) for x in notice["remaining"].split("（")[1]
            .rstrip("）").split("、")]
    # 说明条点到的序号，在快照里必须正好是这两个 CN 账号
    assert sorted(snap[u] for u in ("cn1", "cn2")) == sorted(nums)
    assert snap["gl1"] not in nums


def test_checkin_status_accounts_use_snapshot_index(monkeypatch):
    """签到明细行的 index 也要对得上快照（✎ 改名按它定位账号）。"""
    save_account_cred(_trae_cred("gl1", region="global"))
    save_account_cred(_trae_cred("cn1"))
    save_account_cred(_trae_cred("cn2"))
    monkeypatch.setattr(
        "buddy_proxy.trae.provider.fetch_checkin_status",
        lambda token="", account_id="", region="": {
            "checked_in": True, "enable": True, "message": "ok"})

    snap = _snapshot_index_by_uid()
    rows = TraeProvider().checkin_status()["accounts"]
    assert sorted(r["index"] for r in rows) == sorted(snap[u] for u in ("cn1", "cn2"))
    assert all(r["id"] for r in rows), "明细行必须带 id 供改名/删除定位"


def test_checkin_claim_throttle_uses_loop_position(monkeypatch):
    """节流按**本轮位置**而不是展示序号（否则第一个也白睡一拍）。"""
    import buddy_proxy.trae.provider as mod

    save_account_cred(_trae_cred("gl1", region="global"))
    save_account_cred(_trae_cred("cn1"))
    save_account_cred(_trae_cred("cn2"))
    monkeypatch.setattr(
        "buddy_proxy.trae.provider.claim_checkin_credits",
        lambda token="", account_id="", region="": {"code": 0, "message": "ok",
                                                    "credits_granted": 5})
    monkeypatch.setattr(
        "buddy_proxy.trae.provider.ensure_account_token",
        lambda aid: ("tok", {"uid": aid}))
    slept: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))

    out = TraeProvider().checkin_claim()
    # 本轮第一个账号不睡，第二个睡一次
    assert len(slept) == 1, f"两个账号只该节流一次，实际 {slept}"
    assert out["checked_in"] is True


def test_checkin_claim_accounts_use_snapshot_index(monkeypatch):
    """打卡结果明细的 index 也要对得上快照（✎ 改名按它定位账号）。"""
    save_account_cred(_trae_cred("gl1", region="global"))
    save_account_cred(_trae_cred("cn1"))
    save_account_cred(_trae_cred("cn2"))
    monkeypatch.setattr(
        "buddy_proxy.trae.provider.claim_checkin_credits",
        lambda token="", account_id="", region="": {"code": 0, "message": "ok",
                                                    "credits_granted": 5})
    monkeypatch.setattr(
        "buddy_proxy.trae.provider.ensure_account_token",
        lambda aid: ("tok", {"uid": aid}))

    snap = _snapshot_index_by_uid()
    out = TraeProvider().checkin_claim()
    assert out["checked_in"] is True
    # 只领了本区的两个 CN 号，且明细行的 index 与快照逐项一致
    assert sorted(d["index"] for d in out["accounts"]) == \
        sorted(snap[u] for u in ("cn1", "cn2"))
    assert {d["id"] for d in out["accounts"]}
    assert all(d["ok"] for d in out["accounts"])


# ───────────────────────── QoderIntlProvider ─────────────────────────


def test_qoder_intl_provider_identity():
    from buddy_proxy.qoder.config import REGIONS
    from buddy_proxy.qoder.intl_provider import QoderIntlProvider

    p = QoderIntlProvider()
    assert p.id == "qoderintl" and p.name == "Qoder 海外版"
    assert p._region.key == "global"
    assert p._region is REGIONS["global"] or p._region.key == REGIONS["global"].key
    # 海外签到**保留**（有无活动由上游实测决定，inactive 时调度自动跳过）
    assert p.supports_checkin is True


def test_qoder_intl_enabled_follows_index():
    from buddy_proxy.qoder.credentials import save_account_cred as qoder_save
    from buddy_proxy.qoder.intl_provider import intl_enabled as qoder_intl_enabled

    assert qoder_intl_enabled() is False
    qoder_save(_qoder_cred("cn-a", region="cn"))
    assert qoder_intl_enabled() is False, "只有 CN 账号不注册 qoderintl"
    qoder_save(_qoder_cred("gl-a", region="global"))
    assert qoder_intl_enabled() is True


def _qoder_cred(account_id: str, region: str = "cn") -> dict:
    return {
        "account_id": account_id,
        "token": f"dt-{account_id}-real-token",
        "uid": f"uid-{account_id}",
        "machine_id": "m1",
        "refresh_token": f"rt-{account_id}",
        "expires_at_ms": int(time.time() * 1000) + 3600_000,
        "name": "", "email": f"{account_id}@qoder.example.com",
        "region": region, "plan": "", "source": "state",
    }


# ───────────────────────── login --region 别名 ─────────────────────────


def test_pick_region_aliases():
    assert _pick_region("Trae", "global", default_key="cn") == "global"
    assert _pick_region("Trae", "海外", default_key="cn") == "global"
    assert _pick_region("Trae", "国际版", default_key="cn") == "global"
    assert _pick_region("Trae", "cn", default_key="cn") == "cn"
    assert _pick_region("Trae", "国内", default_key="cn") == "cn"
    # 未知值回落 default_key（qoder 的默认可能是 global）
    assert _pick_region("Qoder", "火星", default_key="global") == "global"
    assert _pick_region("Qoder", "火星", default_key="cn") == "cn"


def test_pick_region_noninteractive_uses_default(monkeypatch):
    """非交互终端（管道/CI）不阻塞，直接用默认区域。"""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _pick_region("Trae", None, default_key="cn") == "cn"
    assert _pick_region("Qoder", None, default_key="global") == "global"


# ───────────────────────── 海外模型目录（2026-10-06 probe 填表） ─────────────────────────


def test_intl_model_catalog():
    """probe 实测收录 9 模型；用户点名不要的老模型 / 实测 4001 的反例别补回来。"""
    _, ids, credits, images = model_tables("global")
    assert set(ids) == {"gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol",
                        "gpt-5.6-terra", "gpt-5.6-luna", "kimi-k3",
                        "gpt-5.4", "glm-5.2", "minimax-m3"}
    # 实测通但用户决定不收录（kimi-k2.7-code 等 2026-10-06：太老；
    # gpt-5.2 2026-10-07）——别当漏收补回来
    for gone in ("kimi-k2.7-code", "kimi-k2.5", "minimax-m2.7", "gpt-5.2"):
        assert gone not in ids
    # 实测三种 function 全 4001 的反例——报进目录只会让客户端撞墙
    for dead in ("gpt-6-astra", "glm-5.3", "glm-5.3-flash", "deepseek-v4.1-flash"):
        assert dead not in ids
    assert credits == {}, "海外次数制，无 CN 的积分倍率"
    assert images == set(), "图片能力未实测（probe 只发过纯文本），不声明读图"


def test_work_function_override_by_region():
    """两区 function 绑定互不通用，同名模型的绑定也可能不同。"""
    intl = work_function_override("global")
    cn = work_function_override("cn")
    assert intl["gpt-5.6-sol"] == "chat_v3"
    assert intl["minimax-m3"] == "chat_v3"
    assert cn["glm-5.1"] == "chat_v3"
    # 同名模型两区绑定不同：glm-5.2 / minimax-m3 海外必须 chat_v3，CN 不需要
    assert "glm-5.2" in intl and "glm-5.2" not in cn
    assert "minimax-m3" in intl and "minimax-m3" not in cn
    assert "glm-5.1" not in intl
    # 未收录的模型走 solo_work_lite 默认（表里没有条目即可，调用侧 .get）
    assert "kimi-k3" not in intl
    # region 缺省按 CN
    assert work_function_override("") == cn


def test_intl_content_blocks_wraps_plain_strings_only():
    """海外 Go 侧要求 content 为内容块数组；只包装字符串形态，其余原样。"""
    from buddy_proxy.trae.transport import _intl_content_blocks

    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "blocks"}]},
        {"role": "tool", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "x"}]},
        "not-a-dict",
    ]
    out = _intl_content_blocks(msgs)
    # 纯字符串 → [{"type": "text", "text": ...}]，其余键保留
    assert out[0] == {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    # 块列表消息（tool_result 等）与非 dict 原样放行
    assert out[1] is msgs[1] and out[2] is msgs[2] and out[3] is msgs[3]
    # 包装是拷贝不是原地改——入参不动
    assert msgs[0]["content"] == "hi" and len(msgs) == 4


# ───────────────────────── 海外额度（次数制 + 美元混量纲） ─────────────────────────


def _intl_usage_fixture() -> dict:
    """海外 Pro plan 快照的实测结构（2026-10-06）：一个包混两种量纲。"""
    return {
        "is_dollar_usage_billing": True,
        "usage_summary": {"total_amount": 20.00013, "consumed_amount": 0.00013,
                          "consumption_ratio": 0.0000065},
        "user_entitlement_pack_list": [
            {"display_desc": "Pro Plan",
             "entitlement_base_info": {
                 "entitlement_id": 1, "end_time": 1795000000,
                 "quota": {"premium_model_fast_request_limit": 600,
                           "premium_model_slow_request_limit": -1,
                           "basic_usage_limit": 20}},
             "usage": {"basic_usage_amount": 0.00013}},
            {"display_desc": "Promo Code", "is_hide": True,
             "entitlement_base_info": {
                 "entitlement_id": 2, "end_time": 1795000000,
                 "quota": {"premium_model_fast_request_limit": 50,
                           "basic_usage_limit": 3}},
             "usage": {}},
        ],
    }


def test_intl_quota_dollar_packs_split_by_unit():
    """海外 Pro 包拆成 count/dollar 两行；is_hide 垃圾包被过滤。"""
    items = TraeIntlProvider()._quota_items(_intl_usage_fixture(), label_prefix="")
    head = items[0]
    assert head["unit"] == "dollar" and head["head_only"] is True
    labels = [it["label"] for it in items[1:]]
    # Promo Code（is_hide，UI 不展示）被跳过；通用键表在海外结构下本来
    # 只能解析出 0/0 空条目——这正是「海外查不到额度」的根因
    assert labels == ["Pro Plan · Premium 快速请求", "Pro Plan · Basic 用量"], labels
    fast, basic = items[1], items[2]
    # 次数制：上游不给已用次数 → used=None（好过拿美元已用配次数上限撒谎）
    assert fast["total"] == 600 and fast["used"] is None
    assert fast["unit"] == "count" and fast["percent"] is None
    # 美元制：已用取 usage.basic_usage_amount
    assert basic["used"] == 0.0001 and basic["total"] == 20
    assert basic["unit"] == "dollar"
    # slow 的 -1（无限）不占条目
    assert len(items) == 3


def test_quota_cn_credits_branch_unchanged():
    """CN 积分制不回归：无 premium/basic 键 → 走通用分支，unit=credit。"""
    data = {
        "is_credits_billing": True,
        "usage_summary": {"total_amount": 100, "consumed_amount": 10,
                          "consumption_ratio": 0.1},
        "user_entitlement_pack_list": [
            {"display_desc": "会员包",
             "entitlement_base_info": {
                 "entitlement_id": 9, "end_time": 1795000000,
                 "quota": {"credits_limit": 4000}},
             "usage": {"credits_amount": 100}},
        ],
    }
    items = TraeProvider()._quota_items(data, label_prefix="")
    assert items[0]["unit"] == "credit"
    pack = items[1]
    assert pack["label"] == "会员包"
    assert pack["used"] == 100.0 and pack["total"] == 4000.0
    assert pack["unit"] == "credit"
    assert pack["percent"] == round(pack["used"] / pack["total"] * 100)
