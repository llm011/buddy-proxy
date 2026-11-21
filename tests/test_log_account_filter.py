"""请求日志页「账号」筛选项测试：选中通道时动态返回账号候选，并支持按账号过滤。"""

from __future__ import annotations

from buddy_proxy.core.metrics import MetricsCollector


def test_query_logs_accounts_candidate_and_filter(tmp_path):
    m = MetricsCollector(log_path=tmp_path / "metrics.jsonl")
    m.record(provider="antigravity", model="gemini-4-hydrogen", status=200, account="acct-a")
    m.record(provider="antigravity", model="gemini-4-hydrogen", status=200, account="acct-b")
    m.record(provider="antigravity", model="gemini-4-hydrogen", status=200, account="acct-b")
    m.record(provider="trae", model="deepseek-v4.1-flash", status=200, account="acct-t")

    # 未选通道时：不返回 accounts 候选
    res_no_prov = m.query_logs()
    assert res_no_prov["accounts"] == []
    assert res_no_prov["total"] == 4

    # 选中通道 antigravity：动态返回该通道下出现过的账号（按出现次数降序）
    res_prov = m.query_logs(providers=["antigravity"])
    assert res_prov["accounts"] == ["acct-b", "acct-a"]
    assert res_prov["total"] == 3

    # 选中通道 + 指定账号过滤：只返回该账号记录，且 accounts 候选不收缩
    res_acct = m.query_logs(providers=["antigravity"], accounts=["acct-a"])
    assert res_acct["total"] == 1
    assert res_acct["rows"][0]["account"] == "acct-a"
    assert res_acct["accounts"] == ["acct-b", "acct-a"]
