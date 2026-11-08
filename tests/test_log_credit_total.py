"""日志页「积分合计」按钮：按需累加筛选后的全量积分。

## 需求（用户 2026-11-08）

在日志表的分页条上加一个按钮，点一下才把**当前筛选范围内**的积分累加起来。

## 为什么是按钮而不是常驻

合计要扫全量日志（最多 30 天归档），而表格是服务端分页的——前端只拿当前页
20 条，拿不到全量。两种做法：

- 常驻：每次翻页/轮询都得算一次全量，成本摊到每次交互上；
- 按需按钮：只在用户真的想看时才付出这份开销。

用户明确选择后者，所以这里锁住「按钮未点击时不展示数字」。

## 口径

``credit_total`` 与 ``rows`` 用**同一批过滤后的行**算，不会出现「列表和合计
对不上」。实扣与估算分开统计（与图表 ``credit_sum`` / ``credit_est_sum`` 一致），
前端展示总和并标注其中估算占多少——合成一个数会把「估的」当「真花的」。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

from buddy_proxy.core.metrics import MetricsCollector, _sum_credits

STATIC = pathlib.Path(__file__).resolve().parents[1] / "src" / "buddy_proxy" / "web" / "static"
CHART_LOG = STATIC / "chart_log.js"


# --- 后端聚合 ---------------------------------------------------------------


def test_sum_credits_splits_actual_and_estimated():
    out = _sum_credits([
        {"credit": 1.5},
        {"credit": 0.5, "credit_estimated": True},
        {"credit": 2.0},
    ])
    assert out["actual"] == 3.5
    assert out["estimated"] == 0.5
    assert out["total"] == 4.0
    assert out["count"] == 3


def test_sum_credits_skips_none_and_bools():
    """None / bool / 缺失都不算——bool 是 int 子类，混进来会变成 1。"""
    out = _sum_credits([
        {"credit": None},
        {"credit": True},
        {"credit": False},
        {},
        {"credit": 1.25},
    ])
    assert out["count"] == 1
    assert out["total"] == 1.25


def test_sum_credits_empty():
    assert _sum_credits([]) == {"count": 0, "actual": 0.0,
                                "estimated": 0.0, "total": 0.0}


def test_query_logs_returns_credit_total(tmp_path):
    """query_logs 必须带上 credit_total，且只统计**筛选后**的行。"""
    path = tmp_path / "metrics.jsonl"
    rows = [
        {"ts": 1794000000, "provider": "codebuddy", "model": "glm-5.3",
         "status": 200, "credit": 1.0},
        {"ts": 1794000001, "provider": "codebuddy", "model": "glm-5.3",
         "status": 200, "credit": 2.0, "credit_estimated": True},
        {"ts": 1794000002, "provider": "trae", "model": "other",
         "status": 200, "credit": 10.0},
    ]
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    m = MetricsCollector(path)
    out = m.query_logs(providers=["codebuddy"])

    assert out["total"] == 2, "筛选后只剩 codebuddy 两行"
    ct = out["credit_total"]
    assert ct["count"] == 2
    assert ct["actual"] == 1.0
    assert ct["estimated"] == 2.0
    assert ct["total"] == 3.0, "trae 那 10 分不该被算进来"


def test_credit_total_is_whole_range_not_current_page(tmp_path):
    """合计是「筛选范围内全量」，与 page/page_size 无关。"""
    path = tmp_path / "metrics.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for i in range(30):
            f.write(json.dumps({"ts": 1794000000 + i, "provider": "p",
                                "model": "m", "status": 200,
                                "credit": 1.0}) + "\n")
    m = MetricsCollector(path)

    first = m.query_logs(page=1, page_size=10)
    second = m.query_logs(page=2, page_size=10)

    assert len(first["rows"]) == 10 and len(second["rows"]) == 10
    assert first["credit_total"]["total"] == 30.0
    assert second["credit_total"] == first["credit_total"], "翻页不该改变合计"


# --- 前端按钮 ---------------------------------------------------------------


def test_button_only_shows_after_click():
    """未点击时不展示数字，只给按钮（用户明确要求的「按需」）。"""
    js = CHART_LOG.read_text(encoding="utf-8")
    fn = js[js.index("function creditTotalHtml()"):js.index("function toggleCreditTotal()")]
    assert "if (!CREDIT_SHOWN)" in fn
    assert "Σ 积分合计" in fn
    # 数字只在展开分支里出现
    shown = fn[fn.index("const c = RECENT_CREDIT;"):]
    assert "fmtCreditTotal(c.total)" in shown


def test_zero_credit_still_shows_button():
    """有行但没积分（count=0）时**仍要显示按钮**，展开说明「无积分记录」。

    2026-11-09 用户实报：加了筛选（yuntoken / claude-*）后按钮消失。原因是
    这些通道不返回积分消耗、count=0，而旧实现是 `!count → return ''`。用户
    的观感是「功能没了」——而筛完之后恰恰最想知道「这批请求花没花钱」。
    """
    js = CHART_LOG.read_text(encoding="utf-8")
    fn = js[js.index("function creditTotalHtml()"):js.index("function toggleCreditTotal()")]
    # 有行但零积分 → 给说明，不是空字符串
    assert "该范围内无积分记录" in fn
    # 只有连行都没有才隐藏
    assert "if (!c.count && !c.rows) return '';" in fn


def test_query_change_collapses_stale_total():
    """改了筛选就收起旧数字——挂着上一次的合计比不显示更误导。"""
    js = CHART_LOG.read_text(encoding="utf-8")
    assert "if (qs !== CREDIT_QS) { CREDIT_QS = qs; CREDIT_SHOWN = false; }" in js


def test_toggle_does_not_refetch():
    """点按钮只是切换显示，不重新请求（合计与行数据无关）。"""
    js = CHART_LOG.read_text(encoding="utf-8")
    fn = js[js.index("function toggleCreditTotal()"):]
    fn = fn[:fn.index("\nfunction recentGo")]
    assert "api(" not in fn, "切换显示不该触网"
    assert "renderRecent()" in fn, "容器缺失时才退回整表重绘"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 跑前端 JS")
def test_render_shows_button_and_toggles_to_number():
    """真跑：未点击出按钮，点击后出数字，且都不是倍率。"""
    js = CHART_LOG.read_text(encoding="utf-8")
    start = js.index("let RECENT_CREDIT = null;")
    end = js.index("function recentGo(")
    block = js[start:end]
    script = "\n".join([
        "globalThis.esc = s => String(s ?? '');",
        block,
        "RECENT_CREDIT = {count: 3, actual: 1.5, estimated: 0.5, total: 2};",
        "CREDIT_QS = 'qs';",
        "const before = creditTotalHtml();",
        "CREDIT_SHOWN = true;",
        "const after = creditTotalHtml();",
        "console.log(JSON.stringify({before, after}));",
    ])
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "c.js"
        f.write_text(script, encoding="utf-8")
        r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])

    assert "Σ 积分合计" in out["before"]
    assert "2" not in re.sub(r"[^0-9.]", "", out["before"]), "未点击不该露出数字"
    assert "2" in out["after"], "展开后要有合计"
    assert "估算" in out["after"], "有估算时要标注"
    assert "credits" not in out["after"], "不该出现倍率字样的单位"


def test_credit_total_carries_row_count(tmp_path):
    """credit_total 要带 ``rows``，前端才能区分「有行无积分」与「没数据」。"""
    import json

    path = tmp_path / "m.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 1794000000, "provider": "yuntoken",
                            "model": "claude-opus-5-5", "status": 200}) + "\n")
    m = MetricsCollector(path)
    out = m.query_logs(providers=["yuntoken"])
    ct = out["credit_total"]
    assert ct["count"] == 0, "这条没有 credit 字段"
    assert ct["rows"] == 1, "但要告诉前端「有一行」——否则按钮会被误判成无意义而隐藏"


def test_credit_total_rows_zero_when_no_match(tmp_path):
    """筛选后一行都没有时 rows=0，此时按钮隐藏才合理。"""
    import json

    path = tmp_path / "m.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 1794000000, "provider": "trae",
                            "model": "x", "status": 200, "credit": 1.0}) + "\n")
    m = MetricsCollector(path)
    out = m.query_logs(providers=["yuntoken"])
    assert out["credit_total"]["rows"] == 0
