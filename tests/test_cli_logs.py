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
                            no_color=True, follow=False)
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


# ---------------------------------------------------------------------------
# -f/--follow：增量读 + 回放 + 筛选
# ---------------------------------------------------------------------------


def _append(path, *rows):
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_follow_state_starts_at_end_and_reads_increments(tmp_path):
    path = tmp_path / "metrics.jsonl"
    _write(tmp_path, [_rec(model="old")])
    st = cli_logs._FollowState(path)
    # 第一次 read_new 只管从末尾开始：历史行不回放（回放走 collect）
    assert st.read_new() == []
    _append(path, _rec(model="new1"))
    assert [json.loads(x)["model"] for x in st.read_new()] == ["new1"]
    _append(path, _rec(model="new2"), _rec(model="new3"))
    assert [json.loads(x)["model"] for x in st.read_new()] == ["new2", "new3"]
    assert st.read_new() == []


def test_follow_state_waits_for_partial_line(tmp_path):
    path = tmp_path / "metrics.jsonl"
    path.write_text("", encoding="utf-8")
    st = cli_logs._FollowState(path)
    st.read_new()
    line = json.dumps(_rec(model="half"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)  # 写完内容但换行还没落盘：不能当成一整行
    assert st.read_new() == []
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    assert [json.loads(x)["model"] for x in st.read_new()] == ["half"]


def test_follow_state_follows_rotation(tmp_path):
    """按天轮转：旧文件里还没轮询到的尾巴不能丢，之后要跟到新文件。"""
    path = tmp_path / "metrics.jsonl"
    _write(tmp_path, [_rec(model="before")])
    st = cli_logs._FollowState(path)
    st.read_new()
    _append(path, _rec(model="tail-of-old"))
    path.rename(tmp_path / "metrics.jsonl.2026-11-16")
    path.write_text(json.dumps(_rec(model="in-new")) + "\n", encoding="utf-8")
    got = [json.loads(x)["model"] for x in st.read_new()]
    # 旧文件尾巴先补齐，再从新文件 0 偏移读：不丢也不重
    assert got == ["tail-of-old", "in-new"]
    _append(path, _rec(model="later"))
    assert [json.loads(x)["model"] for x in st.read_new()] == ["later"]


def test_follow_state_switches_to_new_inode_after_rotation(tmp_path):
    """轮转后钉在新 inode 上：老文件残留的字节不会再被读一遍。"""
    path = tmp_path / "metrics.jsonl"
    _write(tmp_path, [_rec(model="before")])
    st = cli_logs._FollowState(path)
    st.read_new()
    old = tmp_path / "metrics.jsonl.2026-11-16"
    path.rename(old)
    path.write_text(json.dumps(_rec(model="in-new")) + "\n", encoding="utf-8")
    assert [json.loads(x)["model"] for x in st.read_new()] == ["in-new"]
    _append(old, _rec(model="written-late-to-old"))
    assert st.read_new() == []
    _append(path, _rec(model="later"))
    assert [json.loads(x)["model"] for x in st.read_new()] == ["later"]


def test_follow_state_flushes_unterminated_line_on_rotation(tmp_path):
    """换行还没落盘就轮转：残行属于旧文件，按 tail 规矩吐出来，不带到新文件。

    常驻描述符仍绑在旧 inode 上，所以这行写不写换行都读得到；写了换行只是
    让它归到「残行」分支之外，验证的顺序仍是旧行在前、新文件行在后。
    """
    path = tmp_path / "metrics.jsonl"
    _write(tmp_path, [_rec(model="before")])
    st = cli_logs._FollowState(path)
    st.read_new()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_rec(model="tail-of-old")) + "\n")
    path.rename(tmp_path / "metrics.jsonl.2026-11-16")
    path.write_text(json.dumps(_rec(model="in-new")) + "\n", encoding="utf-8")
    got = [json.loads(x)["model"] for x in st.read_new()]
    assert got == ["tail-of-old", "in-new"]


def test_follow_state_can_emit_unparseable_half_written_line(tmp_path):
    """轮转吐出的残行可能是**坏的 JSON**，下游必须能跳过而不是炸掉。

    真实场景：服务正在 ``write`` 一条 JSON 的过程中跨天轮转（``os.replace``），
    留下的是 ``{"model": "half"`` 这种半截内容——它缺右括号，``json.loads``
    必然抛错。测试里如果只造"完整 JSON 缺换行"的残行，就测不出这条。

    ``follow()`` 里的 ``except ValueError: continue`` 负责兜住；这里验证两层：
    1. ``_FollowState`` 肯把这行交出去（不静默吞掉，否则用户永远不知道为什么少一条）
    2. 调用方能安全跳过
    """
    path = tmp_path / "metrics.jsonl"
    _write(tmp_path, [_rec(model="before")])
    st = cli_logs._FollowState(path)
    st.read_new()

    # 写「半截 JSON」：去掉最后的右括号，再轮转
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_rec(model="half"))[:-1])
    path.rename(tmp_path / "metrics.jsonl.2026-11-16")
    path.write_text(json.dumps(_rec(model="in-new")) + "\n", encoding="utf-8")

    raw = st.read_new()
    parsed, skipped = [], []
    for line in raw:
        try:
            parsed.append(json.loads(line.strip())["model"])
        except ValueError:
            skipped.append(line)
    assert skipped, "坏掉的残行应该被交出来（由调用方决定跳过）"
    assert parsed == ["in-new"], "坏行之后的正常记录不能被连带丢掉"


def test_follow_state_resumes_after_truncate(tmp_path):
    """copytruncate：文件被清零后要继续读，不能停在旧偏移上装死。"""
    path = tmp_path / "metrics.jsonl"
    _write(tmp_path, [_rec(model="old1"), _rec(model="old2")])
    st = cli_logs._FollowState(path)
    st.read_new()
    path.write_text(json.dumps(_rec(model="after-truncate")) + "\n",
                    encoding="utf-8")
    assert [json.loads(x)["model"] for x in st.read_new()] == ["after-truncate"]


def test_follow_state_handles_non_ascii_lines(tmp_path):
    """中文错误信息：偏移按字节算，不能按字符算，否则会重读/错位。"""
    path = tmp_path / "metrics.jsonl"
    path.write_text("", encoding="utf-8")
    st = cli_logs._FollowState(path)
    st.read_new()
    _append(path, _rec(model="m1", error="上游超时"))
    _append(path, _rec(model="m2"))
    got = [json.loads(x)["model"] for x in st.read_new()]
    assert got == ["m1", "m2"]


def _run_follow_once(tmp_path, monkeypatch, args, appended):
    """跑一轮 follow：回放 + 增量各行，下一轮抛 KeyboardInterrupt 退出。"""
    path = tmp_path / "metrics.jsonl"
    if not path.exists():
        path.write_text("", encoding="utf-8")
    ticks = {"n": 0}

    def fake_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] == 1:
            _append(path, *appended)
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_logs.time, "sleep", fake_sleep)
    monkeypatch.setattr(cli_logs, "_FOLLOW_POLL_S", 0)
    return cli_logs.run(args)


def test_follow_replays_then_streams_with_filters(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BUDDY_PROXY_LOG_FILE", str(tmp_path / "x.jsonl"))
    _write(tmp_path, [_rec(model="m1"), _rec(model="m2"), _rec(model="m1")])
    code = _run_follow_once(
        tmp_path, monkeypatch,
        _args(follow=True, limit=2, provider=["codebuddy"], model=["m1"]),
        [_rec(model="m2"), _rec(model="m1", account="acct-9"),
         _rec(model="m1", provider="qoder")])
    assert code == 130
    out = capsys.readouterr().out
    # 回放 2 条 m1（正序）+ 新流里的 m1/codebuddy 1 条；m2、qoder 被筛掉
    assert out.count("  m1 ") == 3
    assert "m2" not in out
    assert "qoder" not in out
    assert "acct-9" in out
    assert "Ctrl-C" in out


def test_follow_survives_copytruncate_between_ticks(tmp_path, monkeypatch, capsys):
    """服务轮转时有「并入既有归档再删当前文件」的分支：文件消失一轮不能退化成
    从末尾接着跟——那样紧接着写进新文件的那条就漏了。"""
    monkeypatch.setenv("BUDDY_PROXY_LOG_FILE", str(tmp_path / "x.jsonl"))
    path = tmp_path / "metrics.jsonl"
    path.write_text(json.dumps(_rec(model="before")) + "\n", encoding="utf-8")
    ticks = {"n": 0}

    def fake_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] == 1:
            path.unlink()  # 这一刻日志文件不存在
        elif ticks["n"] == 2:
            _append(path, _rec(model="after-gap"))
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_logs.time, "sleep", fake_sleep)
    monkeypatch.setattr(cli_logs, "_FOLLOW_POLL_S", 0)
    assert cli_logs.run(_args(follow=True, limit=0)) == 130
    assert "after-gap" in capsys.readouterr().out


def test_follow_json_lines_are_machine_readable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BUDDY_PROXY_LOG_FILE", str(tmp_path / "x.jsonl"))
    _write(tmp_path, [_rec(model="m1")])
    code = _run_follow_once(tmp_path, monkeypatch,
                            _args(follow=True, limit=1, json=True),
                            [_rec(model="m2"), _rec(model="m3", provider="qoder")])
    assert code == 130
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x]
    assert [r["model"] for r in lines] == ["m1", "m2", "m3"]


def test_follow_today_limits_replay(tmp_path, monkeypatch, capsys):
    """``--today`` 回放时不该把昨天的明细也吐出来（新行本身都是今天的）。"""
    monkeypatch.setenv("BUDDY_PROXY_LOG_FILE", str(tmp_path / "x.jsonl"))
    _write(tmp_path, [_rec(model="yesterday", ts=time.time() - 86400),
                      _rec(model="today")])
    code = _run_follow_once(tmp_path, monkeypatch,
                            _args(follow=True, limit=20, today=True), [])
    assert code == 130
    out = capsys.readouterr().out
    assert "today" in out
    assert "yesterday" not in out


def test_follow_rejects_csv(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BUDDY_PROXY_LOG_FILE", str(tmp_path / "x.jsonl"))
    assert cli_logs.run(_args(follow=True, csv=True)) == 2
    assert "不能同时使用" in capsys.readouterr().err


def test_follow_prints_header_without_replay(tmp_path, monkeypatch, capsys):
    """``-n 0`` 不回放，但也得先打出「在跟哪个文件、筛了什么」，否则看着像卡住。"""
    monkeypatch.setenv("BUDDY_PROXY_LOG_FILE", str(tmp_path / "x.jsonl"))
    code = _run_follow_once(tmp_path, monkeypatch,
                            _args(follow=True, limit=0, provider=["codebuddy"],
                                  account=["acct-1"]), [_rec(model="live")])
    assert code == 130
    out = capsys.readouterr().out
    assert "Ctrl-C" in out
    assert "providers=codebuddy" in out and "accounts=acct-1" in out
    assert "live" in out


def test_follow_line_marks_errors():
    line = cli_logs._follow_line(_rec(status=502, error="upstream 502"),
                                 Palette(False))
    assert "502" in line
    assert line.endswith("upstream 502")


def test_follow_line_without_tokens_uses_dash():
    """历史行可能没有 token 字段：补 ``—`` 而不是 ``0``，别编数据。"""
    rec = _rec()
    del rec["prompt_tokens"], rec["completion_tokens"]
    line = cli_logs._follow_line(rec, Palette(False))
    assert "—" in line
