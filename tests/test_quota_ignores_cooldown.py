"""额度枚举也不剔除冷却账号：同 PR #130 的签到同病，签到修了、额度漏了。

2026-10-11 用户实报：trae 3 个 cn 号、2 个挂 quota 冷却，额度面板只剩 1
张卡。根因与 #130 相同——``quota()`` 沿用 ``available_accounts()``（剔除
冷却），而冷却挡的是模型转发，与额度查询的 billing 面无关。trae/qoder 的
quota 遍历改为全量（同区过滤保留）；转发路径的冷却过滤不动（那是它的本职）。
"""

from __future__ import annotations

import time

from test_checkin_ignores_cooldown import _trae_cred


def test_trae_quota_includes_cooling_account(monkeypatch):
    """trae quota：冷却中的账号照常出现在额度条目里（label 带 Trae #N）。"""
    from buddy_proxy.trae import failover as trae_failover
    from buddy_proxy.trae.credentials import save_account_cred as trae_save
    from buddy_proxy.trae.provider import TraeProvider

    trae_save(_trae_cred("q1", "Q1"))
    trae_save(_trae_cred("q2", "Q2"))
    trae_failover._cooldowns.clear()
    trae_failover._cooldowns["q1"] = (time.time() + 600, "quota")
    try:
        # fetch_ent_usage 按账号回执：记下被查询的账号并回最小 usage 结构
        called = []

        def fake_fetch(token="", account_id="", region=""):
            called.append(account_id)
            return {"list": []}  # _quota_items 对空列表返回空条目，足够断言枚举

        monkeypatch.setattr("buddy_proxy.trae.provider.fetch_ent_usage", fake_fetch)
        q = TraeProvider().quota()
    finally:
        trae_failover._cooldowns.clear()
    # 冷却中的账号仍被查询：两个都在，不因冷却被藏掉
    assert sorted(called) == ["q1", "q2"]
    # display_index 全量位次编号：冷却账号也有自己的 #N 组
    assert q is not None


def test_qoder_quota_includes_cooling_account(monkeypatch):
    """qoder quota：区内全量枚举，不因 quota 冷却藏掉账号。"""
    from types import SimpleNamespace

    from buddy_proxy.qoder import failover as qoder_failover
    from buddy_proxy.qoder import provider as qoder_provider
    from buddy_proxy.qoder.provider import QoderProvider

    first = SimpleNamespace(id="a1", priority=0, alias="", name="First",
                            email="first@test", region="cn")
    second = SimpleNamespace(id="a2", priority=1, alias="", name="Second",
                            email="second@test", region="cn")
    monkeypatch.setattr(qoder_provider, "list_accounts", lambda: [first, second])
    # 模拟「全部账号都在冷却」：available_accounts 返回空——额度不该跟着空
    monkeypatch.setattr(qoder_failover, "available_accounts", lambda *_: [])

    called = []

    def fake_quotas(self, acct, index, *, multi=True):
        # 真身是同步函数（跑在线程池里）；fake 也必须同步，async 版提交进
        # 线程池只会得到没人 await 的 coroutine
        called.append(acct.id)
        return [], True

    monkeypatch.setattr(QoderProvider, "_quota_one", fake_quotas)
    # quota 尾部取 level 时会读凭据（ensure_account_token）：测试账号是
    # SimpleNamespace 没有落盘凭据，mock 掉免得 AuthError
    monkeypatch.setattr(qoder_provider, "ensure_account_token",
                        lambda aid: ("tk", {"plan": "test"}))
    monkeypatch.setattr(qoder_provider, "cred_to_credential",
                        lambda d: SimpleNamespace(plan="test"))
    q = asyncio_run(QoderProvider().quota())
    assert sorted(called) == ["a1", "a2"]
    assert q is not None


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)
