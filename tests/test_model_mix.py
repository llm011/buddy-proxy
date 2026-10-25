"""请求日志页头部「各模型使用占比」饼图的数据层（ETHA-42）。

用户诉求：日志页头部加个饼图看各模型今天的使用比例（hover 有次数），并关心
**这个统计对 server 性能的影响**。因此饼图不复用 ``query_logs``（那条链路每次
都要全量扫 metrics.jsonl + 30 天归档，是整套 UI 里最慢的接口），而是单开一张
内存计数表 ``_model_counts``：``record()`` 时顺手 +1，查询时只按日期过滤内存，
不碰磁盘。这些测试钉住三件事：

1. 计数正确（含错误数），且不依赖落盘（log_path=None 也能用）；
2. 重启后整读当前文件 + 30 天归档重建（不跟 2MB 尾部走），与日志表口径一致；
3. top-N + 「其他」合并、日期/通道/模型过滤、覆盖度标记（partial）语义。

运行：
    .venv/bin/python -m pytest tests/test_model_mix.py -v
"""

from __future__ import annotations

import json
import pathlib
import time

from buddy_proxy.core.metrics import MetricsCollector, _date_str


def _today() -> str:
    return _date_str(time.time())


def test_counts_group_by_model_name_across_providers(tmp_path):
    """饼图按「模型名」切扇区，同模型跨通道合并（通道明细留 providers）。

    用户问的是「各**模型**今天使用的比例」：glm-5.3-flash 在 codebuddy /
    trae / zcode 各画一扇会让图例出现四个同名行，读不出模型的真实占比。
    """
    c = MetricsCollector(tmp_path / "metrics.jsonl")
    c.record("codebuddy", "glm-5.3")
    c.record("codebuddy", "glm-5.3")
    c.record("trae", "glm-5.3")
    c.record("trae", "deepseek-v4", status=500)
    c.record("trae", "deepseek-v4", error="boom")

    dist = c.model_distribution()
    assert dist["total"] == 5
    assert dist["errors"] == 2, "status>=400 与带 error 字段都算错误"
    assert dist["models_total"] == 2
    by_model = {m["model"]: m for m in dist["models"]}
    glm = by_model["glm-5.3"]
    assert glm["count"] == 3
    assert glm["provider"] == "codebuddy", "主通道 = 次数最多那个（给图例角标用）"
    # providers 按次数降序，供 tooltip 展开明细
    assert glm["providers"] == [
        {"provider": "codebuddy", "count": 2, "errors": 0},
        {"provider": "trae", "count": 1, "errors": 0},
    ]
    assert by_model["deepseek-v4"]["errors"] == 2


def test_works_without_disk(tmp_path):
    """log_path=None（测试/内存部署）也要能出饼图——不能只在有文件时可信。"""
    c = MetricsCollector(None)
    c.record("codebuddy", "a")
    assert c.model_distribution()["total"] == 1


def test_sorted_desc_and_other_bucket(tmp_path):
    c = MetricsCollector(tmp_path / "metrics.jsonl")
    for _ in range(5):
        c.record("codebuddy", "big")
    for _ in range(3):
        c.record("codebuddy", "mid")
    c.record("codebuddy", "small-1")
    c.record("codebuddy", "small-2")

    dist = c.model_distribution(limit=2)
    assert [m["model"] for m in dist["models"]] == ["big", "mid"]
    assert dist["other"] is not None
    assert dist["other"]["count"] == 2
    assert {m["model"] for m in dist["other"]["models"]} == {"small-1", "small-2"}
    # 「其他」只是展示层折叠，models_total 仍是真实模型数
    assert dist["models_total"] == 4
    # 一块都不丢：top + other = total
    assert dist["total"] == sum(m["count"] for m in dist["models"]) + dist["other"]["count"]


def test_no_other_when_everything_fits(tmp_path):
    c = MetricsCollector(tmp_path / "metrics.jsonl")
    c.record("codebuddy", "only")
    assert c.model_distribution(limit=12)["other"] is None


def test_date_range_filter(tmp_path):
    path = tmp_path / "metrics.jsonl"
    yesterday = _date_str(time.time() - 86400)
    path.write_text(
        json.dumps({"ts": time.time() - 86400, "provider": "codebuddy",
                    "model": "old", "status": 200}) + "\n"
        + json.dumps({"ts": time.time(), "provider": "codebuddy",
                      "model": "new", "status": 200}) + "\n",
        encoding="utf-8",
    )
    c = MetricsCollector(path)
    today = _today()
    only_today = c.model_distribution(today, today)
    assert [m["model"] for m in only_today["models"]] == ["new"]
    assert only_today["total"] == 1
    both = c.model_distribution(yesterday, today)
    assert both["total"] == 2


def test_partial_flag_when_window_precedes_loaded_history(tmp_path):
    """30 天内的归档都会进计数表；比最早有数据的日期更早的窗口必须标 partial。"""
    path = tmp_path / "metrics.jsonl"
    path.write_text(json.dumps({"ts": time.time(), "provider": "p", "model": "m"}) + "\n",
                    encoding="utf-8")
    c = MetricsCollector(path)
    today = _today()
    assert c.model_distribution(today, today)["partial"] is False
    assert c.model_distribution("2026-01-01", today)["partial"] is True


def test_provider_model_whitelist(tmp_path):
    """通道/模型白名单与日志表同语义（组内 OR），让饼图跟着筛选一起收缩。"""
    c = MetricsCollector(tmp_path / "metrics.jsonl")
    for _ in range(3):
        c.record("codebuddy", "glm-5.3")
    c.record("trae", "glm-5.3")
    c.record("trae", "deepseek-v4")

    only_trae = c.model_distribution(providers=["trae"])
    assert only_trae["total"] == 2
    assert {m["model"] for m in only_trae["models"]} == {"glm-5.3", "deepseek-v4"}
    assert only_trae["models"][0]["providers"] == [{"provider": "trae", "count": 1, "errors": 0}]

    only_model = c.model_distribution(models=["glm-5.3"])
    assert only_model["total"] == 4
    assert only_model["models"][0]["count"] == 4

    assert c.model_distribution(providers=["zcode"])["total"] == 0


def test_backfill_counts_older_archives(tmp_path):
    """日期范围可拉到 30 天，饼图不能只看到今天——更早的归档也要计进来。

    日志表的「共 N 条」是扫全部归档的，饼图少了历史就对不上（用户 2026-10-18
    在「30 天」视图下看到的正是这种差异）。
    """
    import time as _t
    today = _t.time()
    day = lambda n: _t.strftime("%Y-%m-%d", _t.localtime(today - n * 86400))
    (tmp_path / f"metrics.jsonl.{day(3)}").write_text(
        json.dumps({"ts": today - 3 * 86400, "provider": "codebuddy",
                    "model": "archived", "status": 200}) + "\n",
        encoding="utf-8")
    (tmp_path / "metrics.jsonl").write_text(
        json.dumps({"ts": today, "provider": "codebuddy",
                    "model": "live", "status": 200}) + "\n",
        encoding="utf-8")

    c = MetricsCollector(tmp_path / "metrics.jsonl")
    dist = c.model_distribution(day(3), day(0))
    assert {m["model"] for m in dist["models"]} == {"archived", "live"}
    # 窗口完全落在有数据的区间内 → 不标待确认
    assert dist["partial"] is False
    # 窗口拉到比最早归档还早一周 → 那几天根本没扫到，必须标（前端加「≈」）
    assert c.model_distribution(day(7), day(0))["partial"] is True


def test_backfill_skips_duplicate_yesterday(tmp_path):
    """昨日归档不能被数两遍（否则翻倍）。

    早期实现里 ``_load_tail`` 计昨日归档 + backfill 再按日期跳过，靠日期集合
    推断「谁已经计过」，跨零点/连续两天没写入时就对不上；现在一律整读重建。
    """
    import time as _t
    yesterday = _t.time() - 86400
    stamp = _t.strftime("%Y-%m-%d", _t.localtime(yesterday))
    (tmp_path / f"metrics.jsonl.{stamp}").write_text(
        json.dumps({"ts": yesterday, "provider": "codebuddy",
                    "model": "m", "status": 200}) + "\n",
        encoding="utf-8")
    c = MetricsCollector(tmp_path / "metrics.jsonl")
    assert c.model_distribution()["total"] == 1


def test_backfill_counts_whole_oversized_files(tmp_path):
    """超过 2MB 的昨日归档 / 当日文件也要整份计数，不能只算尾部。

    `_load_tail` 是尾部截断（2MB），只供内存里最近的表格/聚合；饼图的计数表
    若跟着它走，一天里前半段的请求就静默丢失，且与日志表「共 N 条」对不上。
    """
    import time as _t
    now = _t.time()
    today = _date_str(now)
    stamp = _t.strftime("%Y-%m-%d", _t.localtime(now - 86400))
    row = '{{"ts": {ts}, "provider": "p", "model": "m", "status": 200, "padding": "{pad}"}}\n'
    pad = "x" * 100
    (tmp_path / f"metrics.jsonl.{stamp}").write_text(
        "".join(row.format(ts=now - 86400, pad=pad) for _ in range(12000)), encoding="utf-8")
    (tmp_path / "metrics.jsonl").write_text(
        "".join(row.format(ts=now, pad=pad) for _ in range(15000)), encoding="utf-8")
    assert (tmp_path / f"metrics.jsonl.{stamp}").stat().st_size > 2_000_000
    assert (tmp_path / "metrics.jsonl").stat().st_size > 2_000_000

    c = MetricsCollector(tmp_path / "metrics.jsonl")
    dist = c.model_distribution()
    assert dist["total"] == 27000
    per_day = {d: sum(v[0] for k, v in c._model_counts.items() if k[0] == d)
               for d in (stamp, today)}
    assert per_day == {stamp: 12000, today: 15000}


def test_rebuild_from_archive_matches_live_counts(tmp_path):
    """重启后计数要能恢复（否则饼图每次重启就清零）。"""
    path = pathlib.Path(tmp_path / "metrics.jsonl")
    c = MetricsCollector(path)
    c.record("codebuddy", "glm-5.3", prompt_tokens=10, completion_tokens=2)
    c.record("trae", "glm-5.3", status=500)
    live = c.model_distribution()

    reloaded = MetricsCollector(path)
    again = reloaded.model_distribution()
    assert again["total"] == live["total"] == 2
    assert again["models"] == live["models"]


def test_recent_ms_ring_keeps_last_three(tmp_path):
    """模型页「平均耗时」= 近三次实测（用户 2026-10-25）：环形窗只留 3 条。"""
    m = MetricsCollector(tmp_path / "metrics.jsonl")
    for ms in (100, 200, 300, 400, 500):
        m.record(provider="zcode", model="glm-5.3", status=200, duration_ms=ms)
    # 没耗时的请求不进窗（0 会把近三次均值拉偏）
    m.record(provider="zcode", model="glm-5.3", status=200, duration_ms=0)
    row = next(r for r in m.snapshot()["models"] if r["model"] == "glm-5.3")
    assert row["recent_ms"] == [500, 400, 300]      # 新→旧
    assert row["recent_avg_ms"] == 400
    assert row["avg_ms"] == 250                     # 全窗口均值照旧保留（0 也计入次数）
