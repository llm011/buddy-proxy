"""RunningHub 不是模型通道，不该在额度页出现一张空卡。

用户 2026-11-29 报障：「RunningHub 不要在额度页中展示，这个之前不是让你改了吗，
怎么还在。它不应该出现在这个页面，它不是模型」。

根因不是前端排除表漏了它，而是**后端自报支持额度**：
``benefits.py`` 的 ``entry["quota"] = {"supported": True, **quota}`` 是
「provider 实现了 ``quota()`` 就当支持」——``RunningHubProvider.quota()`` 返回
不带 ``supported`` 的 dict，于是被当成支持，前端通用额度卡就给它渲染了一张
「RunningHub / 无额度数据」的空卡。

它的余额（钱包 $ / 过渡币）、每日积分、任务队列都在自己的「🎨 RunningHub」页，
额度页那张卡没有任何信息量。
"""

from __future__ import annotations

from buddy_proxy.runninghub.provider import RunningHubProvider


def test_runninghub_does_not_claim_quota_support():
    """``supported: False`` 是这条 bug 的唯一开关，必须显式声明。"""
    q = RunningHubProvider().quota()
    assert q.get("supported") is False, (
        "RunningHub 自报支持额度，额度页会多出一张空卡（用户已报障两次）")


def test_runninghub_quota_still_reports_accounts_for_login():
    """``accounts`` 要留着：``auth/login.py`` 登录流程靠它读余额。"""
    q = RunningHubProvider().quota()
    assert "accounts" in q
    assert isinstance(q["accounts"], int)


def test_benefits_spread_overrides_supported():
    """守住 ``{"supported": True, **quota}`` 的覆盖语义。

    如果哪天有人把这里改成先展开再赋值（``{**quota, "supported": True}``），
    上面那条 ``supported: False`` 就失效了——这个测试会红。
    """
    import inspect

    from buddy_proxy import benefits

    src = inspect.getsource(benefits)
    assert '{"supported": True, **quota}' in src, (
        "benefits.py 的 quota 合并顺序变了，provider 的 supported:False 会失效")
