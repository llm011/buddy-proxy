"""CodeBuddy 资源包有效期（expire_ts）的离线测试。

上游额度汇总（get-user-resource-summary）的 ``Packages[]`` 没有任何日期字段
（2026-10-03 实测），明细条目的 ``expire_ts`` 因此恒 None——界面上的资源包
只剩数字、没有「到哪天作废」，用户看不出哪份额度先过期。

2026-10-23 起补了第二数据源 ``/billing/meter/get-user-resource``（资源实例
列表，实测 200）：每条实例是一个订单周期（裂变包一天一条），带
``CycleEndTime`` 与 ``Status``（0=生效中，3=已耗尽/已结束）。对账实测：
summary 的 CycleTotal/Remain = 全部实例（含已耗尽）周期容量之和。

这里守住四条口径（与 :meth:`CodeBuddyProvider._package_expiry_map` 一一对应）：

1. **生效中的实例才算有效期**：取 Status=0 里最晚的 CycleEndTime——订阅包
   等于月结周期结束、裂变包等于最近一次发放的窗口结束。已耗尽实例（其
   CycleEndTime 可能更早也可能更晚）不代表「还能用的这份」的作废时刻。
2. **没有生效实例时退全实例最晚周期**：包还在盘子里只是当前周期用完，
   界面照样该有个日期。
3. **第二数据源失败只丢有效期、不丢额度**：聚合接口 404/风控/形状变了都
   吞成空表，summary 主数据照常出条目（expire_ts=None），绝不让补充信息
   有把整卡额度打挂的能力。
4. **翻页拿全**：裂变包一日一实例，41+ 条是常态，只拿一页会把后面实例的
   周期漏掉（它们的 CycleEndTime 更晚——恰好就是该展示的那个）。

运行：
    .venv/bin/python -m pytest tests/test_codebuddy_package_expiry.py -v
"""
from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from buddy_proxy import codebuddy_provider as cbp  # noqa: E402


def _pack(code: str, total: float, used: float, remain: float | None = None) -> dict:
    p = {"PackageCode": code, "CycleTotalCapacity": total, "CycleUsedCapacity": used}
    if remain is not None:
        p["CycleRemainCapacity"] = remain
    return p


def _res(code: str, cycle_end: str, status: int = 0, remain: float = 100) -> dict:
    """get-user-resource 的一条实例（只带本测试关心的字段）。"""
    return {"PackageCode": code, "CycleEndTime": cycle_end,
            "Status": status, "CycleCapacityRemainPrecise": remain}


def _acct(idx: int = 0):
    return cbp.creds.AccountRef(id=f"uid-{idx}", uid=f"uid-{idx}", nickname=f"tester-{idx}",
                                priority=idx, added_at=1000 + idx)


def _patch(monkeypatch, packages, instances, accounts=None):
    """同时打两个上游接口的补丁，返回 quota() 的结果。"""
    summary = {
        "code": 0, "msg": "OK",
        "data": {"IsPaidUser": True, "SubscriptionPackageCode": "SUB",
                 "Packages": packages},
    }
    resource = {
        "code": 0, "msg": "OK",
        "data": {"Response": {"Data": {"TotalCount": len(instances),
                                        "Accounts": instances}}},
    }

    def fake_api_post_as(aid, path, body=None, **kw):
        if path.endswith("get-user-resource-summary"):
            return dict(summary)
        if path.endswith("get-user-resource"):
            return dict(resource)
        raise AssertionError(f"意外端点 {path}")

    accts = accounts if accounts is not None else [_acct()]
    monkeypatch.setattr(cbp.failover, "list_accounts", lambda: accts)
    monkeypatch.setattr(cbp.creds, "list_accounts", lambda: accts)
    monkeypatch.setattr(cbp.creds, "api_post_as", fake_api_post_as)
    return cbp.CodeBuddyProvider().quota()


def test_expire_ts_taken_from_active_instance(monkeypatch):
    """有效期取生效中（Status=0）实例里最晚的 CycleEndTime。

    已耗尽实例（Status=3）的 CycleEndTime 不参与：它描述的是旧周期，
    不是「现在还能用的这份额度」的作废时刻。
    """
    out = _patch(
        monkeypatch,
        [_pack("SUB", 4000, 0, 4000)],
        [_res("SUB", "2026-11-16 14:49:11", status=0),
         # 已耗尽但周期更晚的实例：不许把它当成有效期
         _res("SUB", "2026-12-31 00:00:00", status=3, remain=0),
         _res("SUB", "2026-10-01 00:00:00", status=3, remain=0)],
    )
    items = [i for i in out["items"] if i["total"] == 4000.0]
    assert len(items) == 1
    want = int(time.mktime(time.strptime("2026-11-16 14:49:11", "%Y-%m-%d %H:%M:%S")))
    assert items[0]["expire_ts"] == want, items[0]


def test_expiry_without_active_instances_falls_back_to_latest_cycle(monkeypatch):
    """没有生效实例的包退全实例最晚周期：包还在盘子里，界面该有个日期。"""
    out = _patch(
        monkeypatch,
        [_pack("PKG", 1000, 1000, 0)],
        [_res("PKG", "2026-10-01 00:00:00", status=3, remain=0),
         _res("PKG", "2026-11-20 08:00:00", status=3, remain=0)],
    )
    items = [i for i in out["items"] if i["total"] == 1000.0]
    assert len(items) == 1
    want = int(time.mktime(time.strptime("2026-11-20 08:00:00", "%Y-%m-%d %H:%M:%S")))
    assert items[0]["expire_ts"] == want, items[0]


def test_expiry_failure_keeps_quota_items(monkeypatch):
    """第二数据源失败只丢有效期：额度条目照出、expire_ts=None。

    有效期是补充信息，不能让它有把整卡额度打挂的能力——原先只有 summary
    一个数据源时额度卡就是好的，加一个数据源不该变差。
    """
    summary = {
        "code": 0, "msg": "OK",
        "data": {"IsPaidUser": True, "SubscriptionPackageCode": "SUB",
                 "Packages": [_pack("SUB", 4000, 0, 4000)]},
    }

    def fake_api_post_as(aid, path, body=None, **kw):
        if path.endswith("get-user-resource-summary"):
            return dict(summary)
        raise RuntimeError("HTTP 404")

    accts = [_acct()]
    monkeypatch.setattr(cbp.failover, "list_accounts", lambda: accts)
    monkeypatch.setattr(cbp.creds, "list_accounts", lambda: accts)
    monkeypatch.setattr(cbp.creds, "api_post_as", fake_api_post_as)

    out = cbp.CodeBuddyProvider().quota()
    assert out is not None
    assert len(out["items"]) == 1, out["items"]
    assert out["items"][0]["total"] == 4000.0
    assert out["items"][0]["expire_ts"] is None
    assert not out["items"][0].get("query_failed"), "有效期失败不是额度查询失败"


def test_expiry_paginates_past_first_page(monkeypatch):
    """实例数超过一页时翻页拿全：后面的实例 CycleEndTime 更晚，正是要展示的。

    裂变包一日一实例，41+ 条是常态；只拿一页会把最新发放的窗口漏掉，
    显示出来的有效期比真实的早好几天。
    """
    page_size = cbp.CodeBuddyProvider._RESOURCE_PAGE_SIZE
    # 铺满一整页再溢出一条，溢出的那条是唯一生效中的实例——只拿一页时
    # expiry 表里压根没有这个包，翻页后才有。
    instances = [_res("SUB", f"2026-10-{d:02d} 00:00:00", status=3, remain=0)
                 for d in range(1, 10)] * (page_size // 9 + 1)
    instances = instances[:page_size]
    instances += [_res("SUB", "2026-12-20 00:00:00", status=0)]  # 只在第二页
    pages: list[list[dict]] = [instances[:page_size], instances[page_size:]]
    calls: list[dict] = []

    def fake_api_post_as(aid, path, body=None, **kw):
        assert path.endswith("get-user-resource"), path
        body = body or {}
        calls.append(body)
        offset = int(body.get("Offset") or 0)
        page = pages[offset // page_size] if offset // page_size < len(pages) else []
        return {"code": 0, "msg": "OK",
                "data": {"Response": {"Data": {"TotalCount": len(instances),
                                                "Accounts": page}}}}

    summary = {"code": 0, "msg": "OK",
               "data": {"IsPaidUser": True, "SubscriptionPackageCode": "SUB",
                        "Packages": [_pack("SUB", 4000, 0, 4000)]}}

    def fake_summary(aid, path, body=None, **kw):
        if path.endswith("get-user-resource-summary"):
            return dict(summary)
        return fake_api_post_as(aid, path, body, **kw)

    accts = [_acct()]
    monkeypatch.setattr(cbp.failover, "list_accounts", lambda: accts)
    monkeypatch.setattr(cbp.creds, "list_accounts", lambda: accts)
    monkeypatch.setattr(cbp.creds, "api_post_as", fake_summary)

    out = cbp.CodeBuddyProvider().quota()
    assert len(calls) >= 2, f"没翻页（{len(calls)} 次调用）"
    want = int(time.mktime(time.strptime("2026-12-20 00:00:00", "%Y-%m-%d %H:%M:%S")))
    assert out["items"][0]["expire_ts"] == want, out["items"][0]


def test_multi_account_prefixes_unchanged(monkeypatch):
    """有效期不加进 label：多账号「CodeBuddy #N · 」前缀分组不受影响。

    前端按 label 前缀分组（benefits_panels.js），有效期走结构化
    expire_ts 字段、由 quotaItemHtml 渲染成「MM-DD 到期」——label 一旦
    变形状，分组就错位。
    """
    out = _patch(
        monkeypatch,
        [_pack("SUB", 4000, 0, 4000)],
        [_res("SUB", "2026-11-16 14:49:11", status=0)],
        accounts=[_acct(0), _acct(1)],
    )
    labels = [i["label"] for i in out["items"]]
    assert labels == ["CodeBuddy #1 · 订阅套餐", "CodeBuddy #2 · 订阅套餐"], labels
    assert all(i["expire_ts"] is not None for i in out["items"])
