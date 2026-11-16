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

from buddy_proxy.core.metrics import MetricsCollector, is_error
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
            _ttft_text(r),
            humanize_ms(r.get("duration_ms")),
            humanize_number(r.get("prompt_tokens")),
            humanize_number(r.get("completion_tokens")),
            _credit_text(r),
            (r.get("error") or "")[:40],
        ])
    out.extend("  " + ln for ln in table(
        ["时间", "通道", "账号", "模型", "状态", "首字", "耗时", "入", "出", "积分",
         "错误"],
        table_rows, palette,
        ["left", "left", "left", "left", "right", "right", "right", "right",
         "right", "right", "left"]))
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


#: 「首字」列的三种状态：有数、非流式本来就无首字、流式却缺数（异常）。
_TTFT_NA = "-"


def _ttft_text(r: dict[str, Any]) -> str:
    """首字列：非流式请求没有首字，记的是 null——补 ``-`` 而不是 ``—``。

    ``—`` 留给「本该有、却没拿到」的流式请求：这两种在终端里长得一样的话，
    偶发丢 TTFT 的行会被当成正常情况从眼皮底下滑过去。
    """
    ms = r.get("ttft_ms")
    if isinstance(ms, (int, float)) and not isinstance(ms, bool):
        return humanize_ms(ms)
    return _TTFT_NA if not r.get("stream") else humanize_ms(None)


def _credit_text(r: dict[str, Any]) -> str:
    """积分列：无数据 ``—``；估算值带 ``★``——和实扣混在一列里，不标出来就会
    被当成上游的真实账单。
    """
    text = humanize_number(r.get("credit"))
    if text != "—" and r.get("credit_estimated"):
        text += "★"
    return text


def _status_text(r: dict[str, Any]) -> str:
    code = int(r.get("status") or 0)
    return str(code) if code else "-"


def _error_style(r: dict[str, Any], palette: Palette) -> list[str]:
    """状态码/错误文本该用的颜色；空列表 = 原样。"""
    code = int(r.get("status") or 0)
    if is_error(r) or code >= 500:
        return ["bad"]
    if code >= 400:
        return ["warn"]
    return []


#: ``-f`` 的列宽。**固定宽度是这里的核心**：表格每次都要先量所有单元格才对齐，
#: 而实时流是一行一行来的、后面的行还看不见，没法那样量。所以跟 ``tail -f``
#: 同口径——列宽写死，再按显示宽度补齐。

#: 「首字」「积分」列宽：``999,999`` / ``1,234.56★`` 都放得下，右边数字齐出一条线。
_TTFT_WIDTH = 6
_CREDIT_WIDTH = 10
#: 账号列宽。账号 id 是 UUID（36 格）、邮箱或 ``acct-…``，原样排表能把整张表
#: 推到 100+ 列，右边几个数字全被挤到屏幕外；24 格足够认出是哪个号。
_ACCOUNT_WIDTH = 24

#: follow 表的列定义：``(表头, 宽度, 对齐, 取值)``。表头行、分隔线和数据行都由
#: 它生成——三处各写一份宽度正是上次「表头和数据错位」的来源。
_FOLLOW_COLUMNS: tuple[tuple[str, int, str, Any], ...] = (
    ("时间", 8, "left", lambda r: _clock(r)),
    ("通道", 11, "left", lambda r: r.get("provider") or "-"),
    ("账号", _ACCOUNT_WIDTH, "left", lambda r: r.get("account") or "-"),
    ("模型", 26, "left", lambda r: r.get("model") or "-"),
    ("状态", 3, "right", lambda r: _status_text(r)),
    ("首字", _TTFT_WIDTH, "right", lambda r: _ttft_text(r)),
    ("耗时", 7, "right", lambda r: humanize_ms(r.get("duration_ms"))),
    ("入", 6, "right", lambda r: humanize_number(r.get("prompt_tokens"))),
    ("出", 6, "right", lambda r: humanize_number(r.get("completion_tokens"))),
    ("积分", _CREDIT_WIDTH, "right", lambda r: _credit_text(r)),
)


def _clock(r: dict[str, Any]) -> str:
    ts = r.get("ts")
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts))) if ts else "--:--:--"
    except (TypeError, ValueError, OSError):
        return "--:--:--"


def _short_account(name: str) -> str:
    """账号超宽截断；表头/数据都按**显示宽**补格，宽字符才不会被多算一格。"""
    if display_width(name) <= _ACCOUNT_WIDTH:
        return name
    out = ""
    for ch in name:
        if display_width(out + ch) > _ACCOUNT_WIDTH - 1:
            break
        out += ch
    return out + "…"


def _follow_line(r: dict[str, Any], palette: Palette) -> str:
    """一条请求记录 → 单行。宽度与 ``_follow_header`` 同源（见 ``_FOLLOW_COLUMNS``）。

    单元格先把**带颜色的**文本按显示宽度补齐（ANSI 不计宽），所以红/黄状态下
    那一列不会把右边的列整体推歪；错误文本跟在最后，不占列。
    """
    styles = _error_style(r, palette)
    cells = []
    for _head, width, align, getter in _FOLLOW_COLUMNS:
        text = str(getter(r) or "-")
        styled = palette(text, *styles) if styles and _head == "状态" else text
        cells.append(pad(styled, width, align))
    line = "  ".join(cells).rstrip()
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
    """``metrics.jsonl`` 的增量读取器（含按天轮转、copytruncate）。

    只读上次偏移之后的新字节，不重读整个文件——``metrics.jsonl`` 一天能到几十
    MB，每次从 0 读既慢又会吃掉半行。文件身份记 (dev, inode)：路径换成了新
    inode 就是轮转，从头读新文件。

    偏移按**字节**算，所以必须用 ``rb`` 打开：文本模式的 ``tell()`` 返回的其实是
    opaque cookie，再叠加 ``len(str)`` 判断残行长度，碰到中文（日志里的上游
    error 基本是中文）就会错位，拼出 ``{"{"model":…`` 这种怪行。
    """

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self._id: Optional[tuple[int, int]] = None
        self._offset = 0
        self._buf = ""
        # 常驻 fd：指向**当前正在跟的那个 inode**。轮转时它仍指向旧 inode，
        # 是唯一还能读到旧文件剩余字节的东西——按天轮转走 os.replace，旧路径
        # 随即消失，光靠路径重开只能打开新文件。
        self._fh: Any = None
        # 构造那一刻文件是否存在。决定「首次看到文件」时该不该把已有内容当历史
        # 跳过：构造时就在 → 是历史（回放已负责）；构造时不在 → 那个文件是之后
        # 新建的，内容全属于本轮。详见 read_new() 里的分支说明。
        self._existed_at_init = path.exists()

    def _stat(self) -> Optional[os.stat_result]:
        try:
            return os.stat(self.path)
        except OSError:
            return None

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
        self._fh = None

    def _open_at(self, offset: int) -> Any:
        try:
            # 以 ``os.open`` 打开再包成文件对象：``os.stat`` 按路径在看，
            # 而这里要的正是「上次那个 inode 的文件」——轮转后旧文件已改名，
            # 只有保留下来的描述符还能读到它（Linux 上改名后打开旧路径会
            # 直接命中新文件，残行就丢了）。
            fd = os.open(self.path, os.O_RDONLY)
        except OSError:
            return None
        fh = os.fdopen(fd, "rb")
        try:
            fh.seek(offset)
        except OSError:
            fh.close()
            return None
        return fh

    def _read_from(self, fh: Any, tail_mode: bool = False) -> list[str]:
        """读到文件尾，只把以换行结尾的整行交出去；残行留在缓冲。

        ``tail_mode``：旧文件已经不再是当前路径（轮转/删除），末尾那截没换行的
        残行就是它的全部内容了，吐出来，别留给一个再也不会被追加的文件。
        """
        chunk = fh.read()
        if not chunk:
            return []
        self._buf += chunk.decode("utf-8", errors="replace")
        parts = self._buf.split("\n")
        self._buf = parts.pop()  # 最后一段没有换行：还没写完，等下一轮
        if tail_mode and self._buf:
            parts.append(self._buf)
            self._buf = ""
        return parts

    def _drain_rotated(self) -> list[str]:
        """轮转前把**旧 inode** 读到尾。

        新路径此刻已指向新文件，按路径重开只能打开新文件，所以必须用常驻 fd
        （它仍绑定旧 inode）。末尾没换行的残行按 ``tail`` 的规矩吐出来——那是
        旧文件的最后一条，服务恰好崩在 write 中间才会这样，丢掉就永远看不到了。
        """
        if self._fh is None:
            return []
        try:
            self._fh.seek(self._offset)
            return self._read_from(self._fh, tail_mode=True)
        except OSError:
            return []

    def read_new(self) -> list[str]:
        """返回自上次以来新出现的完整行（不含换行符）。"""
        st = self._stat()
        if st is None:
            return []
        sig = (st.st_dev, st.st_ino)
        if self._id is None:
            # 首次看到这个文件。
            #
            # 两种情形必须分开，否则要么丢日志、要么重放历史：
            #
            #  · **构造时文件就在**（`follow()` 的常规路径）：历史行由回放
            #    （collect）负责，这里只记位置、不重放。末尾若正卡着一条写了一半
            #    的行，它属于已跳过的历史。
            #  · **构造时文件不在**：赶上了轮转中间态（旧文件已 rename、新文件
            #    还没建）。此刻出现的第一个文件是**新建**的，里面全是本轮之后
            #    写的内容，一条都不能当历史跳过——否则 0 点前后那几秒的请求会
            #    永久丢失，而且毫无提示，用户只会觉得「日志少了一截」。
            fresh = not self._existed_at_init
            self._existed_at_init = True
            if fresh:
                self._id, self._offset = sig, 0
                self._close()
                self._fh = self._open_at(0)
            else:
                self._id, self._offset = sig, st.st_size
                self._close()
                self._fh = self._open_at(self._offset)
                return []
        out: list[str] = []
        if sig != self._id:
            # 轮转：旧 inode 改名后已不在原路径上，只能靠「路径还是不是同一个
            # inode」判断。先把旧文件在偏移之后的部分读完（含没换行的残行，
            # 按 tail 的规矩吐出来，不能带到新文件的头上拼成怪行），再从 0 读
            # 新文件——轮转瞬间已写进新文件的行也就不会漏。
            out.extend(self._drain_rotated())
            self._buf = ""
            self._close()
            self._id, self._offset = sig, 0
            self._fh = self._open_at(0)
        elif st.st_size < self._offset:
            # copytruncate：文件被清零，停在旧偏移上会永远读不到新内容
            self._offset = 0
            self._buf = ""
            self._close()
            self._fh = self._open_at(0)
        if self._fh is None:
            self._fh = self._open_at(self._offset)
        if self._fh is None:
            return out
        lines = self._read_from(self._fh)
        self._offset = self._fh.tell()
        return out + lines


def _follow_header(palette: Palette, filters: dict[str, Any]) -> str:
    """表头 + 分隔线，列宽与 ``_follow_line`` 同源。

    注意先 ``pad`` 再上色：表头是中文（显示宽 4 格），若先套 ANSI 再按
    ``len()`` 补齐就会短一截，后面所有列跟着左移——正是要修的那种错位。
    """
    head = [
        pad(h, width, align)
        for h, width, align, _getter in _FOLLOW_COLUMNS
    ]
    head = [palette.head(cell) for cell in head]
    rule = "  ".join("─" * width for _h, width, _a, _g in _FOLLOW_COLUMNS)
    extra = [f"{k}={','.join(v)}" for k, v in (
        ("providers", filters["providers"]), ("models", filters["models"]),
        ("clients", filters["clients"]), ("accounts", filters["accounts"])) if v]
    scope = f"  筛选 {'  '.join(extra)}" if extra else ""
    return "\n".join([
        "  ".join(head).rstrip(),
        palette.dim(rule) + palette.dim(scope),
    ])


def follow(args: Any, palette: Palette) -> int:
    """``buddy logs -f``：回放最近 N 条，然后持续输出新记录，直到 Ctrl-C。

    为什么按**字节偏移**轮询而不是每次重读整个文件：``metrics.jsonl`` 一天能到
    几十 MB，重读一遍既慢又会让非内存映射的读取器吃到半行。这里只读上次偏移
    之后的新增字节，遇到不完整的最后一行就留在缓冲区等下一个 tick。

    轮转：服务按天把当前文件改成 ``metrics.jsonl.YYYY-MM-DD`` 再新建。路径的
    (dev, inode) 变了就是轮转——先靠常驻描述符把旧文件剩下的字节读完，再从 0 读
    新文件，0 点前后那几秒的请求不会丢。copytruncate（大小突然小于偏移）同理，
    从头读。

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

    # 表头不等回放：``-n 0`` 或回放为空时也得先告诉用户「在跟什么、筛了什么」
    if not as_json:
        print(_follow_header(palette, filters), flush=True)
        print(palette.dim(f"跟踪 {path}（Ctrl-C 退出）"), flush=True)

    # 回放：统计视图同一批行（当前文件 + 归档），取最近 limit 条，正序输出
    if limit:
        for r in reversed(collect(_replay_args(args, limit)).get("rows") or []):
            if as_json:
                print(json.dumps(r, ensure_ascii=False, default=str), flush=True)
            else:
                print(_follow_line(r, palette), flush=True)

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
