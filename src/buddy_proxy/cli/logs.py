"""`buddy logs`：指标日志的统计视图（不是 tail）。

为什么默认给统计而不是 tail：`tail -f buddy-proxy.jsonl` 每次都在，但它回答不了
最常问的三个问题——「刚才那批请求慢不慢」「哪家通道在报错」「这个月花了多少
积分」。纯 tail 还会被服务自己的启动/重试噪音淹没。

所以默认布局是「总览 → 分通道/分账号 → 按天 → Top 模型 → 最近 N 条明细」，
tail 语义保留在 ``--raw``（直接转给 ``proxy.sh logs``，行为与以前一字不差）。

数据源是**磁盘上的 metrics.jsonl + 按天归档**（与 ``/ui/api/logs`` 同一批行），
所以服务挂了也能查历史——那通常正是最需要它的时刻。
"""

from __future__ import annotations

import csv
import io
import json
import os
import pathlib
import sys
import time
from typing import Any, Optional

from buddy_proxy.core.metrics import MetricsCollector

from .render import (
    Palette, display_width, humanize_number, humanize_ms, humanize_ts, pad, table,
)

#: ``metrics.jsonl`` 里 UI 需要、但导出 CSV 时不该出现的大对象。
_CSV_FIELDS = [
    "ts", "time", "provider", "account", "model", "protocol", "client",
    "status", "stream", "duration_ms", "ttft_ms", "prompt_tokens",
    "completion_tokens", "cached_tokens", "credit", "credit_estimated",
    "chunk_count", "error",
]


#: `~/code/life/buddy-proxy` 是主仓库的软链；服务默认就跑在那里。
_LINKED_LOGS = pathlib.Path.home() / "code" / "life" / "buddy-proxy" / "logs"


def _now() -> float:
    return time.time()


def _day_offset(days: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(_now() - days * 86400))


def metrics_dir() -> pathlib.Path:
    """指标文件所在目录，**与服务同口径**。

    服务把 metrics.jsonl 放在 ``--log-file`` 的**同级目录**
    （``__main__.py``: ``metrics = MetricsCollector(args.log_file.parent / "metrics.jsonl")``），
    默认 ``<项目根>/logs/``，可用 ``BUDDY_PROXY_LOG_FILE`` 覆盖。早先这里按
    state_dir（``~/.buddy-proxy``）找，扫出一个空列表还静默显示「没有数据」——
    目录猜错比报错更难查，所以推导方式与服务 ``__main__`` 保持逐字一致：
    以包内 ``core/paths.py`` 的位置向上两级定位项目根，不依赖 cwd（`buddy`
    可以在任意目录敲）。

    worktree 例外：worktree 里的 ``logs/`` 通常是空的（服务跑的是主仓库），
    这时退回 ``~/code/life/buddy-proxy/logs``（`buddy` 软链目标）——查日志
    的人要的是**真实跑着的服务**的数据，不是当前 checkout 的空壳。
    """
    env = os.getenv("BUDDY_PROXY_LOG_FILE")
    if env:
        return pathlib.Path(env).expanduser().parent
    root = pathlib.Path(__file__).resolve().parents[2]
    found = [d for d in (root / "logs", _LINKED_LOGS) if (d / "metrics.jsonl").exists()]
    if found:
        return found[0]
    return root / "logs"


def build_collector(sd: Optional[Any] = None) -> MetricsCollector:
    """只读收集器：把 log_path 指到目录/metrics.jsonl，不注册任何进程内钩子。

    不直接复用服务进程里的 collector——那个是单例、在服务的内存里，CLI 拿不到。
    这里构造一个读者的目的只是复用它的「读当前文件 + 归档 + 坏行跳过」逻辑，
    构造本身不写文件（record() 才会写）。
    """
    base = pathlib.Path(sd).expanduser() if sd else metrics_dir()
    return MetricsCollector(log_path=base / "metrics.jsonl")


def _split_list(values: Optional[list[str]]) -> Optional[list[str]]:
    if not values:
        return None
    out: list[str] = []
    for v in values:
        out.extend(x.strip() for x in str(v).split(",") if x.strip())
    return out or None


def collect(args: Any) -> dict[str, Any]:
    days = getattr(args, "days", None)
    start = getattr(args, "start", None)
    end = getattr(args, "end", None)
    if days:
        start = _day_offset(int(days) - 1)
        end = None
    if getattr(args, "today", False):
        start = end = time.strftime("%Y-%m-%d", time.localtime())
    if start and end and start > end:
        start, end = end, start
    accounts = _split_list(getattr(args, "account", None))
    if accounts:
        # 日志里的 account 存的是 failover 的账号 id；用户更习惯敲 ``#2``。
        # 两种都认，而不是要求用户先 `buddy usage` 去抄 id。
        accounts = list(dict.fromkeys(
            accounts + [a.lstrip("#") for a in accounts]))
    return build_collector(getattr(args, "state_dir", None)).logs_scan(
        start=start,
        end=end,
        providers=_split_list(getattr(args, "provider", None)),
        models=_split_list(getattr(args, "model", None)),
        clients=_split_list(getattr(args, "client", None)),
        accounts=accounts,
        detail_limit=max(0, int(getattr(args, "limit", 0) or 0)),
    )


def _overview(scan: dict[str, Any], palette: Palette) -> list[str]:
    total = scan["total"]
    errors = scan["errors"]
    rate = scan.get("success_rate")
    if total == 0:
        return [palette.dim("没有匹配的请求记录。")]
    parts = [f"请求 {humanize_number(total)}"]
    if rate is not None:
        err_text = f"错误 {humanize_number(errors)}（成功率 {rate * 100:.1f}%）"
        parts.append(palette.bad(err_text) if errors else palette.ok(err_text))
    d = scan["duration_ms"]
    if d.get("p50") is not None:
        parts.append(f"p50 {humanize_ms(d['p50'])}  p90 {humanize_ms(d['p90'])}  "
                     f"p95 {humanize_ms(d['p95'])}  p99 {humanize_ms(d['p99'])}  "
                     f"max {humanize_ms(d['max'])}")
    t = scan["ttft_ms"]
    if t.get("p50") is not None:
        parts.append(f"首字 p50 {humanize_ms(t['p50'])} / p95 {humanize_ms(t['p95'])}")
    tok = scan["tokens"]
    hit = tok.get("cache_hit_rate")
    hit_text = f"  缓存命中 {hit * 100:.1f}%" if hit is not None else ""
    parts.append(f"token 入 {humanize_number(tok['prompt'])} / 出 "
                 f"{humanize_number(tok['completion'])} / 命中 "
                 f"{humanize_number(tok['cached'])}{hit_text}")
    c = scan["credit"]
    if c.get("total"):
        parts.append(f"积分 {c['total']:,.2f}（实扣 {c['actual']:,.2f} + "
                     f"估算 {c['estimated']:,.2f}）")
    return [f"  {p}" for p in parts]


#: 账号列宽。账号 id 是 UUID（36 格）、邮箱或 ``acct-…``，原样排表能把整张表
#: 推到 100+ 列，右边四个数字全被挤到屏幕外；24 格足够认出是哪个号。
_ACCOUNT_WIDTH = 24


def _short_account(name: str) -> str:
    if display_width(name) <= _ACCOUNT_WIDTH:
        return name
    out = ""
    for ch in name:
        if display_width(out + ch) > _ACCOUNT_WIDTH - 1:
            break
        out += ch
    return out + "…"


def _account_table(rows: list[dict[str, Any]], palette: Palette) -> list[str]:
    """账号分组：``providers`` 是同一 id 在多个通道下的出现（failover 换通道）。

    只按 account 分组会把「codebuddy 的 46d8…」和「qoder 的 46d8…」当成同一行
    ——两者的冷却、额度、限流池都是分开的，合在一起看错误率只会误导，所以
    多带一列「通道」。

    通道列只在**真的跨通道**时才显示：绝大多数行只有一个通道，每人一行重复
    写 ``codebuddy`` 等于白占 10 格。跨通道的行合并成 ``codebuddy, qoder``，
    超过 3 个只列前 3 个加计数——``(unknown)`` 行曾经一口气列了 13 个通道，
    把表撑得没法看，而那一行的意思本来就是「没记到账号」。
    """
    def channels(r: dict[str, Any]) -> list[str]:
        return sorted(set(r.get("providers") or [])) or [r.get("provider") or "(未知)"]

    show_channel = any(len(channels(r)) > 1 for r in rows)
    table_rows = []
    for r in rows:
        cols = []
        if show_channel:
            provs = channels(r)
            text = ", ".join(provs[:3])
            if len(provs) > 3:
                text += f" 等 {len(provs)} 个"
            cols.append(text)
        cols.extend([
            _short_account(r.get("full_name") or r.get("name") or "(未知账号)"),
            humanize_number(r.get("count")),
            humanize_number(r.get("errors")) if r.get("errors") else "-",
            humanize_ms(r.get("avg_ms")),
        ])
        table_rows.append(cols)
    headers = ["通道", "账号", "请求", "错误", "均耗时"] if show_channel \
        else ["账号", "请求", "错误", "均耗时"]
    aligns = ["left", "right", "right", "right"] if show_channel else ["left"] + [
        "right"] * 3
    return table(headers, table_rows, palette, aligns)


def _sections(scan: dict[str, Any], palette: Palette) -> list[str]:
    out: list[str] = []
    head = ["通道", "请求", "错误", "均耗时"]
    aligns = ["left", "right", "right", "right"]
    for title, key in (("按通道", "by_provider"), ("按账号", "by_account"),
                       ("按客户端", "by_client")):
        rows = scan.get(key) or []
        # 全空（只有未知）的分组没有信息量：旧记录 client 为空串，按客户端
        # 分组就只剩一行 (unknown)，单独排版只会白占一屏。
        if not rows or all(not (r.get("name") or "").strip("(unknown)")
                           for r in rows):
            continue
        if key == "by_account":
            out.append(palette.head("按账号（跨通道同一 id 会分开计）"))
            out.append("  " + palette.dim(
                "想按通道+账号看：buddy logs -p <通道>；拿账号 id：buddy usage --json"))
            out.extend("  " + ln for ln in _account_table(rows, palette))
            out.append("")
            continue
        out.append(palette.head(title))
        out.extend("  " + ln for ln in table(head, [
            [r["name"], humanize_number(r["count"]),
             humanize_number(r["errors"]) if r["errors"] else "-",
             humanize_ms(r["avg_ms"])] for r in rows], palette, aligns))
        out.append("")
    status = scan.get("by_status") or []
    if status:
        out.append(palette.head("按状态码"))
        out.append("  " + "  ".join(f"{s['name']}: {s['count']}" for s in status))
        out.append("")
    days = scan.get("by_day") or []
    if len(days) > 1:
        out.append(palette.head("按天"))
        rows = []
        for d in days:
            avg = round(d["duration_ms_sum"] / d["count"]) if d["count"] else 0
            rows.append([d["date"], humanize_number(d["count"]),
                         humanize_number(d["errors"]) if d["errors"] else "-",
                         humanize_ms(avg)])
        out.extend("  " + ln for ln in table(
            ["日期", "请求", "错误", "均耗时"], rows, palette,
            ["left", "right", "right", "right"]))
        out.append("")
    models = scan.get("by_model") or []
    if models:
        out.append(palette.head("Top 模型"))
        out.extend("  " + ln for ln in table(
            ["模型", "请求", "错误", "均耗时"],
            [[m["name"], humanize_number(m["count"]),
              humanize_number(m["errors"]) if m["errors"] else "-",
              humanize_ms(m["avg_ms"])] for m in models], palette, aligns))
        out.append("")
    return out


def _detail(scan: dict[str, Any], palette: Palette) -> list[str]:
    rows = scan.get("rows") or []
    if not rows:
        return []
    out = [palette.head(f"最近 {len(rows)} 条")]
    table_rows = []
    for r in rows:
        code = int(r.get("status") or 0)
        code_text = str(code) if code else "-"
        if code >= 500 or r.get("error"):
            code_text = palette.bad(code_text)
        elif code >= 400:
            code_text = palette.warn(code_text)
        acct = r.get("account") or ""
        table_rows.append([
            humanize_ts(r.get("ts"))[5:],
            r.get("provider") or "",
            _short_account(acct) if acct else "",
            r.get("model") or "",
            code_text,
            humanize_ms(r.get("duration_ms")),
            humanize_number(r.get("prompt_tokens")),
            humanize_number(r.get("completion_tokens")),
            (r.get("error") or "")[:40],
        ])
    out.extend("  " + ln for ln in table(
        ["时间", "通道", "账号", "模型", "状态", "耗时", "入", "出", "错误"],
        table_rows, palette,
        ["left", "left", "left", "left", "right", "right", "right", "right",
         "left"]))
    return out


def render_text(scan: dict[str, Any], palette: Palette) -> list[str]:
    f = scan.get("filters") or {}
    scope = f"{f.get('start') or '最早'} ~ {f.get('end') or '现在'}"
    extra = []
    for key in ("providers", "models", "clients", "accounts"):
        if f.get(key):
            extra.append(f"{key}={','.join(f[key])}")
    title = f"范围 {scope}" + (f"  筛选 {'  '.join(extra)}" if extra else "")
    lines = [palette.head(title), ""]
    if scan.get("total") == 0:
        lines.append(palette.dim("没有匹配的请求记录。"))
    else:
        lines.append(palette.head("总览"))
        lines.extend(_overview(scan, palette))
        lines.append("")
        lines.extend(_sections(scan, palette))
        lines.extend(_detail(scan, palette))
    if not scan.get("from_disk"):
        lines.append("")
        lines.append(palette.warn("未指定日志文件，只看了内存里的最近记录。"))
    return lines


def render_csv(scan: dict[str, Any]) -> str:
    """导出**筛选后全量**明细（不是当前页），方便丢进表格/脚本算账。"""
    rows = scan.get("rows") or []
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_CSV_FIELDS)
    for r in rows:
        out = dict(r)
        out["time"] = humanize_ts(r.get("ts")) if r.get("ts") else ""
        w.writerow([out.get(k, "") for k in _CSV_FIELDS])
    return buf.getvalue()


def run(args: Any) -> int:
    palette = Palette(False if getattr(args, "no_color", False) else None)
    scan = collect(args)
    if getattr(args, "csv", False):
        sys.stdout.write(render_csv(scan))
        return 0
    if getattr(args, "json", False):
        print(json.dumps(scan, ensure_ascii=False, indent=2, default=str))
        return 0
    for ln in render_text(scan, palette):
        print(ln)
    if scan.get("total"):
        print(palette.dim(
            "导出明细: buddy logs --csv > logs.csv    原始 tail: buddy logs --raw"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    pass
