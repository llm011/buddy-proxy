"""``buddy logs`` 统计视图 + 共享筛选判据的测试（纯离线）。

锁住三件事：

1. **统计数字对得上**：分位数、成功率、缓存命中率、积分拆分各有一条手工可算
   的小样本，算法换成插值或把估算积分并进实扣，这里就会红。
2. **统计与列表用同一判据**：``logs_scan`` 与 ``query_logs`` 共用
   ``log_matches``；``-a #2`` 这种「带井号」的写法也得能命中。
3. **账号分组的通道列**：跨通道的同一 id 要能看出来，纯 UUID 行不能把表撑爆。
"""
from __future__ import annotations

import argparse
import json
import time

import pytest

from buddy_proxy.cli import logs as cli_logs
from buddy_proxy.cli.render import Palette
from buddy_proxy.core.metrics import MetricsCollector
from buddy_proxy.core.metrics_protocol import log_matches

# 固定时刻，永远落在「今天」之外：按日归档/日期裁剪的用例不该受跑测试的日期影响
_TS = time.mktime((2026, 1, 5, 12, 0, 0, 0, 0, -1))


def _rec(**kw):
    rec = {
        "ts": _TS,
        "provider": "codebuddy",
        "model": "m1",
        "protocol": "openai",
        "status": 200,
        "duration_ms": 100,
        "ttft_ms": 50,
        "stream": True,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "credit": None,
        "credit_estimated": False,
        "client": "c1",
        "account": "acct-1",
        "error": "",
    }
    rec.update(kw)
    return rec


def _write(tmp_path, rows):
    path = tmp_path / "metrics.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return MetricsCollector(log_path=path)


def _scan(collector, **kw):
    return collector.logs_scan(**kw)


# ---------------------------------------------------------------------------
# 统计口径
# ---------------------------------------------------------------------------


def test_percentiles_use_nearest_rank(tmp_path):
    """分位数取「实际出现过的那一条」，不插值。

    durations 1..10 → p50=5, p90=9, p95=10, p99=10, max=10（最近秩法）。
    插值实现会给出 p90=9.1 这种没发生过的耗时。
    """
    rows = [_rec(duration_ms=d) for d in range(1, 11)]
    scan = _scan(_write(tmp_path, rows))
    d = scan["duration_ms"]
    assert (d["p50"], d["p90"], d["p95"], d["p99"], d["max"]) == (5, 9, 10, 10, 10)
    assert d["avg"] == 6  # round(55/10)=6


def test_success_rate_counts_500_and_error_field(tmp_path):
    rows = [
        _rec(),
        _rec(),
        _rec(status=500),
        _rec(status=200, error="read timeout"),  # 状态 200 但带 error 也算失败
    ]
    scan = _scan(_write(tmp_path, rows))
    assert scan["total"] == 4
    assert scan["errors"] == 2
    assert scan["success_rate"] == 0.5


def test_tokens_and_cache_hit_rate(tmp_path):
    rows = [
        _rec(**{"prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 80}),
        _rec(**{"prompt_tokens": 300, "completion_tokens": 20, "cached_tokens": 20}),
    ]
    tok = _scan(_write(tmp_path, rows))["tokens"]
    assert (tok["prompt"], tok["completion"], tok["cached"]) == (400, 30, 100)
    assert tok["cache_hit_rate"] == 0.25


def test_credit_splits_actual_and_estimated(tmp_path):
    rows = [
        _rec(credit=1.5),
        _rec(credit=2.5, credit_estimated=True),
    ]
    credit = _scan(_write(tmp_path, rows))["credit"]
    assert (credit["actual"], credit["estimated"], credit["total"]) == (1.5, 2.5, 4.0)


def test_empty_scan_has_no_rates(tmp_path):
    scan = _scan(_write(tmp_path, []))
    assert scan["total"] == 0
    assert scan["success_rate"] is None
    assert scan["duration_ms"]["p50"] is None
    assert scan["tokens"]["cache_hit_rate"] is None


def test_by_account_carries_providers(tmp_path):
    """同一 id 在两个通道下都要能标出来；``(unknown)`` 行也带通道清单。"""
    rows = [
        _rec(provider="codebuddy", account="same-id"),
        _rec(provider="qoder", account="same-id"),
        _rec(provider="kimi", account=""),
    ]
    scan = _scan(_write(tmp_path, rows))
    by = {g["name"]: g for g in scan["by_account"]}
    assert by["same-id"]["providers"] == ["codebuddy", "qoder"]
    assert by["same-id"]["count"] == 2
    assert by["(unknown)"]["providers"] == ["kimi"]


def test_bad_lines_are_skipped(tmp_path):
    path = tmp_path / "metrics.jsonl"
    path.write_text(
        json.dumps(_rec()) + "\n{not json\n\n" + json.dumps(_rec()) + "\n",
        encoding="utf-8",
    )
    scan = _scan(MetricsCollector(log_path=path))
    assert scan["total"] == 2


# ---------------------------------------------------------------------------
# 筛选：统计与列表同一判据
# ---------------------------------------------------------------------------


def test_log_matches_date_and_fields():
    rec = _rec()
    day = time.strftime("%Y-%m-%d", time.localtime(_TS))
    assert log_matches(rec, start=day, end=day)
    assert not log_matches(rec, start="2026-01-06")
    assert not log_matches(rec, end="2026-01-04")
    assert log_matches(rec, providers=["codebuddy"])
    assert not log_matches(rec, providers=["qoder"])
    # 组内 OR、组间 AND
    assert not log_matches(rec, providers=["codebuddy"], models=["m2"])
    # 缺字段的行只在没筛选时通过
    assert log_matches({})
    assert not log_matches({}, providers=["codebuddy"])


def test_scan_and_query_agree_on_account_filter(tmp_path):
    rows = [_rec(account="a"), _rec(account="b"), _rec(account="b")]
    col = _write(tmp_path, rows)
    scan = _scan(col, accounts=["b"])
    page = col.query_logs(accounts=["b"], page_size=50)
    assert scan["total"] == page["total"] == 2


def _args(**kw):
    ns = argparse.Namespace(days=None, start=None, end=None, today=False,
                            account=None, provider=None, model=None, client=None,
                            limit=0, state_dir=None, csv=False, json=False,
                            no_color=True)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_cli_account_filter_accepts_hash_prefix(tmp_path, monkeypatch):
    """用户敲 ``-a #2``，日志里存的是 ``2``；两种都要能命中且不重复。"""
    monkeypatch.setenv("BUDDY_PROXY_LOG_FILE", str(tmp_path / "x.jsonl"))
    _write(tmp_path, [_rec(account="2"), _rec(account="#2"), _rec(account="3")])
    scan = cli_logs.collect(_args(account=["#2"]))
    assert scan["total"] == 2
    assert scan["filters"]["accounts"] == ["#2", "2"]


def test_cli_days_window(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDY_PROXY_LOG_FILE", str(tmp_path / "x.jsonl"))
    now = time.time()
    rows = [_rec(ts=now), _rec(ts=now - 10 * 86400)]
    # 走内存/单文件路径时按 ts 过滤，日期窗口之外的旧行不该计入
    _write(tmp_path, rows)
    scan = cli_logs.collect(_args(days=3))
    assert scan["total"] == 1


def test_cli_start_end_swapped(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDY_PROXY_LOG_FILE", str(tmp_path / "x.jsonl"))
    _write(tmp_path, [_rec(ts=time.time())])
    f = cli_logs.collect(_args(start="2026-12-31", end="2026-01-01"))["filters"]
    assert (f["start"], f["end"]) == ("2026-01-01", "2026-12-31")


def test_metrics_dir_honours_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDY_PROXY_LOG_FILE", str(tmp_path / "logs" / "buddy.jsonl"))
    assert cli_logs.metrics_dir() == tmp_path / "logs"


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _scan_with(rows, **kw):
    return {
        "total": len(rows),
        "errors": 0,
        "success_rate": 1.0 if rows else None,
        "duration_ms": {"avg": 1, "p50": 1, "p90": 1, "p95": 1, "p99": 1, "max": 1},
        "ttft_ms": {"avg": 1, "p50": 1, "p95": 1},
        "tokens": {"prompt": 0, "completion": 0, "cached": 0, "cache_hit_rate": None},
        "credit": {"count": 0, "actual": 0.0, "estimated": 0.0, "total": 0.0},
        "by_provider": [], "by_model": [], "by_client": [],
        "by_account": rows, "by_status": [], "by_day": [], "rows": [],
        "filters": {"start": None, "end": None, "providers": [], "models": [],
                    "clients": [], "accounts": []},
        "from_disk": True,
        **kw,
    }


def test_account_table_truncates_long_ids():
    rows = [{"name": "46d8527f-c817-4113-b2fc-347148a4e4e7", "providers": ["codebuddy"],
             "count": 10, "errors": 0, "avg_ms": 1200}]
    text = "\n".join(cli_logs._account_table(rows, Palette(False)))
    assert "46d8527f-c817-4113-b2fc…" in text
    assert "347148a4e4e7" not in text
    assert "通道" not in text  # 全部单通道时不显示通道列


def test_account_table_shows_channel_only_when_cross_channel():
    rows = [
        {"name": "same-id", "providers": ["codebuddy", "qoder"], "count": 3,
         "errors": 1, "avg_ms": 100},
        {"name": "only", "providers": ["codebuddy"], "count": 1, "errors": 0,
         "avg_ms": 100},
    ]
    text = "\n".join(cli_logs._account_table(rows, Palette(False)))
    assert "通道" in text
    assert "codebuddy, qoder" in text


def test_render_text_empty_scan():
    out = "\n".join(cli_logs.render_text(_scan_with([]), Palette(False)))
    assert "没有匹配的请求记录" in out
    assert "总览" not in out


def test_render_csv_header_and_rows():
    scan = _scan_with([{"name": "a"}])
    scan["rows"] = [_rec()]
    text = cli_logs.render_csv(scan)
    head, first = text.splitlines()[:2]
    assert head.startswith("ts,time,provider,account,model")
    assert "2026-01-05 12:00" in first


@pytest.mark.parametrize("flag", ["json", "csv"])
def test_run_json_and_csv_are_machine_readable(tmp_path, monkeypatch, capsys, flag):
    monkeypatch.setenv("BUDDY_PROXY_LOG_FILE", str(tmp_path / "x.jsonl"))
    _write(tmp_path, [_rec()])
    assert cli_logs.run(_args(**{flag: True})) == 0
    out = capsys.readouterr().out
    if flag == "json":
        assert json.loads(out)["total"] == 1
    else:
        assert out.startswith("ts,time,provider")
