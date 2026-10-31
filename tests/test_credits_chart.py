"""总览积分图：后端积分聚合/回读与真实前端渲染回归。

前端测试由 node 执行 ``chart_credits.js`` 原文件（不是另写一份模拟实现），
保证日期过滤、跨模型聚合、两种图表及实扣/估算 tooltip 与浏览器一致。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import time

import pytest

from buddy_proxy.core.metrics import MetricsCollector, _date_str

_STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"


def _credit_rows(collector: MetricsCollector, days: int = 30) -> list[dict]:
    return collector.snapshot(days=days)["model_daily"]


def test_credit_real_and_estimate_are_separate_across_buckets():
    """同一渠道的不同模型先各自聚合，实扣和估算绝不混成一个可信度数。"""
    m = MetricsCollector(None)
    m.record("qoder", "a", credit=1.25)
    m.record("qoder", "a", credit=2.75, credit_estimated=True)
    m.record("qoder", "b", credit=0.5, credit_estimated=True)
    m.record("codebuddy", "a", credit=4)
    m.record("kimi", "c", prompt_tokens=1000, completion_tokens=100)  # 只有倍率，没有消耗

    rows = {(b["provider"], b["model"]): b for b in _credit_rows(m)}
    assert rows[("qoder", "a")]["credit_sum"] == pytest.approx(1.25)
    assert rows[("qoder", "a")]["credit_est_sum"] == pytest.approx(2.75)
    assert rows[("qoder", "b")]["credit_sum"] == 0
    assert rows[("qoder", "b")]["credit_est_sum"] == pytest.approx(0.5)
    assert rows[("codebuddy", "a")]["credit_sum"] == 4
    assert rows[("kimi", "c")]["credit_sum"] == rows[("kimi", "c")]["credit_est_sum"] == 0


def test_credit_aggregates_survive_restart_and_legacy_records(tmp_path):
    """旧记录缺 credit_estimated 时按上游实扣；重启不改变两档的数值。"""
    path = tmp_path / "metrics.jsonl"
    first = MetricsCollector(path)
    first.record("qoder", "a", credit=1.25)
    first.record("trae", "a", credit=0.75, credit_estimated=True)
    live = {(b["provider"], b["model"]): b for b in _credit_rows(first)}

    restarted = MetricsCollector(path)
    rows = {(b["provider"], b["model"]): b for b in _credit_rows(restarted)}
    for key, before in live.items():
        assert rows[key]["credit_sum"] == pytest.approx(before["credit_sum"])
        assert rows[key]["credit_est_sum"] == pytest.approx(before["credit_est_sum"])

    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text(json.dumps({
        "ts": time.time(), "provider": "qoder", "model": "old", "status": 200,
        "duration_ms": 1, "prompt_tokens": 1, "completion_tokens": 1,
        "credit": 2.5,
    }) + "\n", encoding="utf-8")
    row = next(b for b in _credit_rows(MetricsCollector(legacy)) if b["model"] == "old")
    assert row["credit_sum"] == 2.5
    assert row["credit_est_sum"] == 0


def test_yesterday_credit_archive_is_restored(tmp_path):
    """跨零点后积分趋势还能从昨日归档恢复，而不是只剩当前日志。"""
    ts = time.time() - 86400
    yesterday = _date_str(ts)
    path = tmp_path / "metrics.jsonl"
    archive = tmp_path / f"metrics.jsonl.{yesterday}"
    archive.write_text(json.dumps({
        "ts": ts, "provider": "qoder", "model": "a", "status": 200,
        "duration_ms": 1, "prompt_tokens": 1, "completion_tokens": 1,
        "credit": 3.5, "credit_estimated": True,
    }) + "\n", encoding="utf-8")
    row = next(b for b in _credit_rows(MetricsCollector(path)) if b["date"] == yesterday)
    assert row["credit_sum"] == 0
    assert row["credit_est_sum"] == 3.5


pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑积分图 JS（CI 有，本机可选）"
)

_STUB = r"""
const listeners = new Map();
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    innerHTML: '',
    addEventListener(type, fn) { listeners.set(id + ':' + type, fn); },
  });
  return elements.get(id);
}
globalThis.document = { getElementById: element };
let RANGE = {start: '2026-10-01', end: '2026-10-02', q: null};
function inRange(date) { return date >= RANGE.start && date <= RANGE.end; }
function rangeLabel() { return RANGE.start === RANGE.end ? '今天' : RANGE.start + ' → ' + RANGE.end; }
function pcolor(p) { return {qoder: '#111111', codebuddy: '#222222', trae: '#333333'}[p]; }
function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;'); }
function fmtCredit(v) { return Number(v).toFixed(2); }
let TOOLTIP = null;
function showChartTip(html) { TOOLTIP = html; }
function hideChartTip() { TOOLTIP = null; }
let STATS = {model_daily: [
  {date: '2026-09-30', provider: 'qoder', model: 'outside', credit_sum: 99, credit_est_sum: 0},
  {date: '2026-10-01', provider: 'qoder', model: 'a', credit_sum: 1.25, credit_est_sum: 0.25},
  {date: '2026-10-01', provider: 'qoder', model: 'b', credit_sum: 0.75, credit_est_sum: 0.5},
  {date: '2026-10-01', provider: 'codebuddy', model: 'a', credit_sum: 4, credit_est_sum: 0},
  {date: '2026-10-02', provider: 'trae', model: 'a', credit_sum: 0, credit_est_sum: 2.5},
  {date: '2026-10-02', provider: 'kimi', model: 'only-multiplier', credit_sum: 0, credit_est_sum: 0},
]};
"""


def _run_chart(body: str) -> dict:
    script = _STUB + (_STATIC / "chart_credits.js").read_text(encoding="utf-8") + "\n" + body
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_chart_filters_range_and_aggregates_models_per_provider():
    """今天只画今天；切回两天范围后，同渠道不同模型合计且不串实扣/估算。"""
    data = _run_chart("""
RANGE = {start: '2026-10-01', end: '2026-10-01', q: 'today'};
const today = creditsByProvider();
RANGE = {start: '2026-10-01', end: '2026-10-02'};
const bothDays = creditsByProvider();
console.log(JSON.stringify({today, bothDays}));
""")
    assert data["today"] == [
        {"provider": "codebuddy", "real": 4, "est": 0},
        {"provider": "qoder", "real": 2, "est": 0.75},
    ]
    assert data["bothDays"] == [
        {"provider": "codebuddy", "real": 4, "est": 0},
        {"provider": "qoder", "real": 2, "est": 0.75},
        {"provider": "trae", "real": 0, "est": 2.5},
    ]


def test_daily_and_provider_charts_keep_real_and_estimate_visible():
    """真函数出图：两天的堆叠顺序、跨模型合计、含估算标签都正确。"""
    data = _run_chart("""
renderCreditsDaily();
renderCreditsBars();
const daily = CREDITS_VIEW.map(d => ({date: d.date, total: d.total,
  providers: [...d.by_provider.entries()]}));
console.log(JSON.stringify({daily,
  dailyHtml: element('chart-credits-daily').innerHTML,
  providerHtml: element('chart-credits-providers').innerHTML}));
""")
    assert data["daily"] == [
        {"date": "2026-10-01", "total": 6.75,
         "providers": [["qoder", {"real": 2, "est": 0.75}], ["codebuddy", {"real": 4, "est": 0}]]},
        {"date": "2026-10-02", "total": 2.5, "providers": [["trae", {"real": 0, "est": 2.5}]]},
    ]
    assert 'fill="#222222" opacity="1"' in data["dailyHtml"]
    assert 'fill="#111111" opacity="0.45"' in data["dailyHtml"]
    assert "09-30" not in data["dailyHtml"] and "outside" not in data["dailyHtml"]
    assert "only-multiplier" not in data["providerHtml"] and "kimi" not in data["providerHtml"]
    assert "2.75（含估算） 积分" in data["providerHtml"]
    assert "≈2.50 积分" in data["providerHtml"]
    assert 'background:#111111;opacity:.45' in data["providerHtml"]


def test_tooltips_show_separate_real_and_estimated_amounts():
    """悬停读真实聚合状态，按天/按渠道均明确列出两种数值，而非只给混加总数。"""
    data = _run_chart("""
renderCreditsDaily();
renderCreditsBars();
listeners.get('chart-credits-daily:mousemove')({target: {closest: () => ({
  dataset: {i: '0'}, getBoundingClientRect: () => ({}),
})}});
const dailyTip = TOOLTIP;
listeners.get('chart-credits-providers:mousemove')({target: {closest: () => ({
  dataset: {ci: '1'}, getBoundingClientRect: () => ({}),
})}});
const providerTip = TOOLTIP;
console.log(JSON.stringify({dailyTip, providerTip}));
""")
    for tip in data.values():
        assert "上游实扣" in tip and "本地估算" in tip
        assert "2.00 积分" in tip and "≈0.75 积分" in tip
    assert "qoder" in data["dailyTip"] and "codebuddy" in data["dailyTip"]
    assert "2026-10-01" in data["dailyTip"]
    assert "2026-10-01 → 2026-10-02" in data["providerTip"]


def test_empty_range_does_not_fabricate_credit_usage():
    """没有积分上报的范围明确显示空态；倍率不会当成积分画出来。"""
    data = _run_chart("""
STATS = {model_daily: [{date: '2026-10-01', provider: 'kimi', model: 'a', credit_sum: 0, credit_est_sum: 0}]};
renderCreditsDaily();
renderCreditsBars();
console.log(JSON.stringify({daily: element('chart-credits-daily').innerHTML,
  providers: element('chart-credits-providers').innerHTML}));
""")
    assert "暂无积分消耗数据" in data["daily"]
    assert "暂无积分消耗数据" in data["providers"]
    assert "0.00 积分" not in data["providers"]
