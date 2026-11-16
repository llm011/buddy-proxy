"""`buddy logs`：指标日志的统计视图（不是 tail）。

为什么默认给统计而不是 tail：`tail -f buddy-proxy.jsonl` 每次都在，但它回答不了
最常问的三个问题——「刚才那批请求慢不慢」「哪家通道在报错」「这个月花了多少
积分」。纯 tail 还会被服务自己的启动/重试噪音淹没。

所以默认布局是「总览 → 分通道/分账号 → 按天 → Top 模型 → 最近 N 条明细」。

要看**正在发生**的请求用 ``-f/--follow``：先打最近 N 条明细，再按文件偏移增量
读 ``metrics.jsonl`` 持续追加，``-p/-m/-c/-a`` 与统计视图同一个判据
（``log_matches``）。它跟的是请求记录而不是 ``buddy-proxy.jsonl`` 的服务事件流，
因为只有前者带通道/模型/账号这些能筛的字段；纯服务日志仍走
``buddy logs --raw``（wrapper 直接转给 ``proxy.sh logs`` 的 ``tail -F``）。

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
from buddy_proxy.core.metrics_protocol import log_matches

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


def _scan_filters(args: Any) -> dict[str, Any]:
    """``log_matches`` 的筛选参数：统计视图和 ``-f`` 共用这一份归一化。

    两处各写一份，最容易漂移的是 ``-a #2``（日志存 ``2``）和 ``--start/--end``
    写反这两种情况——同一个命令换个视图结果不同，用户只会觉得「日志在乱记」。
    """
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
    return {
        "start": start,
        "end": end,
        "providers": _split_list(getattr(args, "provider", None)),
        "models": _split_list(getattr(args, "model", None)),
        "clients": _split_list(getattr(args, "client", None)),
        "accounts": accounts,
    }


def collect(args: Any) -> dict[str, Any]:
    return build_collector(getattr(args, "state_dir", None)).logs_scan(
        **_scan_filters(args),
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


def _is_error(rec: dict[str, Any]) -> bool:
    """与 ``core.metrics._is_error`` 同口径：状态码 ≥400 或 error 字段有值。

    不复用那个私有函数（它只负责统计），但口径必须一致——follow 里标红的行
    和统计视图里的「错误 N」得是同一批，否则用户会问「为什么这行红了但错误数是 0」。
    """
    try:
        code = int(rec.get("status") if rec.get("status") is not None else 200)
    except (TypeError, ValueError):
        code = 200
    return code >= 400 or bool(rec.get("error"))


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


def _follow_line(r: dict[str, Any], palette: Palette) -> str:
    """一条请求记录 → 单行。列与统计视图的「最近 N 条」一致，额外带宽（到秒）。"""
    code = int(r.get("status") or 0)
    code_text = str(code) if code else "-"
    if _is_error(r):
        code_text = palette.bad(code_text)
    elif code >= 400:
        code_text = palette.warn(code_text)
    ts = r.get("ts")
    try:
        clock = time.strftime("%H:%M:%S", time.localtime(float(ts))) if ts else "--:--:--"
    except (TypeError, ValueError, OSError):
        clock = "--:--:--"
    fields = [
        clock,
        pad(r.get("provider") or "-", 12),
        pad(_short_account(r.get("account") or "-"), _ACCOUNT_WIDTH),
        pad(r.get("model") or "-", 26),
        pad(code_text, 3, "right"),
        pad(humanize_ms(r.get("duration_ms")), 7, "right"),
        pad(humanize_number(r.get("prompt_tokens")), 6, "right"),
        pad(humanize_number(r.get("completion_tokens")), 6, "right"),
    ]
    line = "  ".join(fields).rstrip()
    if r.get("error"):
        line += "  " + palette.bad((r.get("error") or "")[:60])
    return line


#: follow 轮询间隔。写一行是纯追加，半秒一次的 stat+读增量几乎无开销；
#: 再快也没意义——``tail -f`` 默认也要等 1 秒。
_FOLLOW_POLL_S = 0.5


def _replay_args(args: Any, limit: int) -> Any:
    """回放用的参数副本：``-f`` 下 ``-n`` 的含义是「先回放几条」，0 不回放。"""
    import argparse
    ns = argparse.Namespace(**vars(args))
    ns.limit = limit
    ns.follow = False
    return ns


class _FollowState:
    """``metrics.jsonl`` 的增量读取器（含按天轮转处理）。"""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self._fh: Any = None
        self._ino: Optional[int] = None
        self._buf = ""

    def _open(self) -> None:
        try:
            fh = self.path.open("r", encoding="utf-8", errors="replace")
            st = os.fstat(fh.fileno())
        except OSError:
            return
        self._fh = fh
        self._ino = st.st_ino
        fh.seek(0, os.SEEK_END)

    def read_new(self) -> list[str]:
        """返回自上次以来新出现的完整行（不含换行符）。"""
        if self._fh is None:
            if not self.path.exists():
                return []
            self._open()
            if self._fh is None:
                return []
        rotated = False
        try:
            ino = os.stat(self.path).st_ino
            rotated = ino != self._ino
        except OSError:
            rotated = True
        if rotated:
            # 先把旧文件读到尾（改名后新创建的路径已指向新 inode），再切过去
            tail = self._drain()
            # 旧的残行属于已轮转的文件：新文件沿用同一缓冲区会把两段拼成怪行
            self._buf = ""
            self._close()
            self._open()
            # _open() 会 seek 到新文件末尾——那会跳过轮转瞬间已写进新文件的行，
            # 所以这里收回来读一遍（最多一次轮转瞬间的量）
            if self._fh is None:
                return tail
            self._fh.seek(0)
            return tail + self._drain()
        return self._drain()

    def _drain(self) -> list[str]:
        fh = self._fh
        if fh is None:
            return []
        try:
            chunk = fh.read()
        except OSError:
            return []
        if not chunk:
            return []
        self._buf += chunk
        parts = self._buf.split("\n")
        self._buf = parts.pop()  # 最后一段不完整（或为空），留到下轮
        return parts

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
        self._fh = None
        self._ino = None


def _follow_header(palette: Palette, filters: dict[str, Any]) -> str:
    extra = [f"{k}={','.join(v)}" for k, v in (
        ("providers", filters["providers"]), ("models", filters["models"]),
        ("clients", filters["clients"]), ("accounts", filters["accounts"])) if v]
    scope = f"  筛选 {'  '.join(extra)}" if extra else ""
    return "  ".join([
        palette.dim("时间    "), pad(palette.dim("通道"), 12),
        pad(palette.dim("账号"), _ACCOUNT_WIDTH), pad(palette.dim("模型"), 26),
        pad(palette.dim("状态"), 3, "right"), pad(palette.dim("耗时"), 7, "right"),
        pad(palette.dim("入"), 6, "right"), pad(palette.dim("出"), 6, "right"),
    ]).rstrip() + palette.dim(scope)


def follow(args: Any, palette: Palette) -> int:
    """``buddy logs -f``：回放最近 N 条，然后持续输出新记录，直到 Ctrl-C。

    为什么按**字节偏移**轮询而不是每次重读整个文件：``metrics.jsonl`` 一天能到
    几十 MB，重读一遍既慢又会让非内存映射的读取器吃到半行。这里只读上次偏移
    之后的新增字节，遇到不完整的最后一行就留在缓冲区等下一个 tick。

    轮转：服务按天把当前文件改名成 ``metrics.jsonl.YYYY-MM-DD`` 再新建。检测到
    路径的 (inode, 大小) 变小或 inode 变化时，先把旧文件剩余部分读完再切到新文件——
    否则 0 点前后那几秒的请求会丢（它们写在旧文件末尾）。

    半行/坏行：写到一半的行不解析（留着等补全），解析失败的整行跳过——服务崩在
    ``write()`` 中间时最后一行是残的，不该让 follow 直接退出。
    """
    sd = getattr(args, "state_dir", None)
    base = pathlib.Path(sd).expanduser() if sd else metrics_dir()
    path = base / "metrics.jsonl"
    filters = _scan_filters(args)
    as_json = bool(getattr(args, "json", False))
    limit = max(0, int(getattr(args, "limit", 0) or 0))
    if getattr(args, "csv", False):
        # 常驻 + CSV 没有意义（谁会把 tail 重定向成表格？），明确拒绝而不是静默只出前 N 行
        print("--csv 与 -f 不能同时使用；要导出全量明细请去掉 -f", file=sys.stderr)
        return 2

    # 先回放：统计视图同一批行（当前文件 + 归档），取最近 limit 条，正序输出
    if limit:
        scan = collect(_replay_args(args, limit))
        rows = list(reversed(scan.get("rows") or []))
        if as_json:
            for r in rows:
                print(json.dumps(r, ensure_ascii=False, default=str), flush=True)
        else:
            print(_follow_header(palette, filters), flush=True)
            for r in rows:
                print(_follow_line(r, palette), flush=True)

    if not as_json:
        print(palette.dim(f"跟踪 {path}（Ctrl-C 退出）"), flush=True)

    state = _FollowState(path)
    try:
        while True:
            for raw_line in state.read_new():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or not log_matches(rec, **filters):
                    continue
                if as_json:
                    print(json.dumps(rec, ensure_ascii=False, default=str), flush=True)
                else:
                    print(_follow_line(rec, palette), flush=True)
            time.sleep(_FOLLOW_POLL_S)
    except KeyboardInterrupt:
        return 130


def run(args: Any) -> int:
    palette = Palette(False if getattr(args, "no_color", False) else None)
    if getattr(args, "follow", False):
        return follow(args, palette)
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
