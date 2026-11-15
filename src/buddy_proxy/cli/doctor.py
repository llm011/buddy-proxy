"""`buddy doctor`：一次性回答「现在哪儿不对」。

命令的用途是**排查前置**，所以每条结论都必须来自实测（端口、进程、文件
inode/mtime、磁盘占用），不做「应该没问题」式的推断：

- 服务/健康：认端口监听 + ``/health``，与 ``buddy status`` 同口径；
- 配置：state 目录里的设置文件能不能解析（settings.json 语法错会整份静默失效，
  这是最常见的「改了没生效」根因）；
- 通道：每个通道本地登了几个号、有几个在冷却、凭证还有多久过期；
- 日志：度量文件多大、最后一次写入是什么时候（不涨 = 指标落盘坏了）；
- 磁盘：logs/ 目录各文件按大小排前几名（``buddy cleanup`` 之前先看是谁在长）。

这里刻意**不 import 服务进程模块**（``buddy_proxy.__main__``）——doctor 最常
在服务起不来的时候跑，把服务拉起链路上的依赖全拽进来只会让 doctor 自己也挂。
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from .render import (
    Palette, humanize_bytes, humanize_duration, humanize_ts, pad, table,
)

#: 有额度面板 / 多账号 failover 的通道。order 大致按用户心智（主用在前）。
#: 加新通道时只需在这里补一行：`buddy usage` 与 `buddy doctor` 都从这张表取。
_CHANNELS: list[tuple[str, str, str]] = [
    # (provider_id, 展示名, failover 模块路径)
    ("codebuddy", "CodeBuddy", "buddy_proxy.codebuddy_provider.failover"),
    ("trae", "Trae 国内版", "buddy_proxy.trae.failover"),
    ("qoder", "Qoder", "buddy_proxy.qoder.failover"),
    ("kimi", "Kimi", "buddy_proxy.kimi.failover"),
    ("copilot", "GitHub Copilot", "buddy_proxy.copilot.failover"),
    ("codex", "Codex", "buddy_proxy.codex.failover"),
    ("antigravity", "Antigravity", "buddy_proxy.antigravity.failover"),
]


def channel_names() -> list[tuple[str, str]]:
    return [(pid, name) for pid, name, _mod in _CHANNELS]


def local_channels() -> list[tuple[str, str, dict[str, Any]]]:
    """逐个通道读本地账号状态。导入/调用失败的通道**不抛**，返回空状态。

    为什么吞异常：doctor/usage 的价值在于「还能跑」，某个通道的凭据文件被手改
    坏了，不该让整条命令崩掉——恰恰那种时候最需要看其它通道和服务的状态。
    """
    out: list[tuple[str, str, dict[str, Any]]] = []
    for pid, name, mod in _CHANNELS:
        status: dict[str, Any] = {"enabled": False, "accounts": []}
        try:
            m = importlib.import_module(mod)
            fn = getattr(m, "accounts_status", None)
            if callable(fn):
                got = fn()
                if isinstance(got, dict):
                    status = got
        except Exception as exc:  # noqa: BLE001 - 单个通道的坏状态不该拖垮命令
            status = {"enabled": False, "accounts": [],
                      "error": f"{type(exc).__name__}: {exc}"}
        out.append((pid, name, status))
    return out


def _http_json(url: str, timeout: float = 5.0) -> Optional[Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode() or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _settings_report(state_dir: pathlib.Path) -> list[list[str]]:
    """state 目录里所有 ``*.json`` 设置文件的解析结果。"""
    rows = []
    for p in sorted(state_dir.glob("*.json")):
        if p.name.startswith("metrics"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            rows.append([p.name, "读取失败", str(exc)])
            continue
        if not text.strip():
            rows.append([p.name, "正常", "空文件（等价于默认值）"])
            continue
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            # 这是**最贵的**错误：settings.json 语法错会被整份忽略，用户改的
            # 开关全部失效且没有任何提示，只能靠这里暴露。
            rows.append([p.name, "语法错误", f"第 {exc.lineno} 行: {exc.msg}"])
            continue
        rows.append([p.name, "正常", humanize_bytes(p.stat().st_size)])
    return rows


def _log_files(state_dir: pathlib.Path,
               logs_dir: Optional[pathlib.Path] = None) -> tuple[list[list[str]], int]:
    """state 目录 + 指标日志目录里按大小降序的文件（含归档）。

    指标目录必须由调用方按**服务同口径**给出（``logs.metrics_dir()``）：
    早先这里自己推 ``state_dir.parent / "logs"``，而服务其实把 metrics.jsonl
    写在项目根的 ``logs/`` 下——两处都能推出一个存在的目录，于是 doctor 认真
    列了一堆无关文件、报告「没有 metrics.jsonl」，比不报还误导。
    """
    roots = [state_dir]
    if logs_dir is not None and logs_dir.is_dir() and logs_dir != state_dir:
        roots.append(logs_dir)
    entries: list[tuple[int, pathlib.Path, float]] = []
    for root in roots:
        try:
            for p in root.iterdir():
                if not p.is_file():
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                entries.append((st.st_size, p, st.st_mtime))
        except OSError:
            continue
    entries.sort(key=lambda e: -e[0])
    total = sum(e[0] for e in entries)
    rows = [[p.name, humanize_bytes(size), humanize_ts(mtime)]
            for size, p, mtime in entries[:10]]
    return rows, total


def _metrics_report(state_dir: pathlib.Path,
                   logs_dir: Optional[pathlib.Path] = None) -> dict[str, Any]:
    path = (logs_dir or state_dir) / "metrics.jsonl"
    if not path.is_file():
        return {"path": str(path), "exists": False}
    st = path.stat()
    archives = sorted(path.parent.glob("metrics.jsonl.*"))
    return {
        "path": str(path),
        "exists": True,
        "bytes": st.st_size,
        "mtime": st.st_mtime,
        "lines": _count_lines(path),
        "archives": len(archives),
        "archive_bytes": sum(p.stat().st_size for p in archives if p.is_file()),
    }


def _count_lines(path: pathlib.Path, limit: int = 5_000_000) -> Optional[int]:
    """粗略行数（最多数 limit 行，超了返回 None——doctor 不该为计数读几百 MB）。"""
    n = 0
    try:
        with path.open("rb") as fh:
            for _ in fh:
                n += 1
                if n >= limit:
                    return None
    except OSError:
        return None
    return n


def collect(state_dir: Optional[pathlib.Path] = None,
            port: Optional[int] = None) -> dict[str, Any]:
    from buddy_proxy.core.paths import state_dir as _state_dir

    state_dir = state_dir or _state_dir()
    port = port or int(os.environ.get("PROXY_PORT") or 8787)
    base = f"http://127.0.0.1:{port}"

    listening = _port_listening("127.0.0.1", port)
    health = _http_json(f"{base}/health") if listening else None
    settings = _settings_report(state_dir)

    channels = []
    for pid, name, status in local_channels():
        accounts = status.get("accounts") or []
        cooling = sum(1 for a in accounts if a.get("cooling"))
        channels.append({
            "provider": pid, "name": name, "enabled": bool(status.get("enabled")),
            "accounts": len(accounts), "cooling": cooling,
            "error": status.get("error"),
            "items": accounts,
        })

    # 延迟导入：doctor 刻意不拖服务模块（见模块 docstring），而 cli.logs 只依赖
    # core.metrics 的读取逻辑，不碰服务启动链，这里局部导入不会引入风险。
    from .logs import metrics_dir
    logs_dir = metrics_dir()
    log_rows, log_total = _log_files(state_dir, logs_dir)
    return {
        "port": port,
        "listening": listening,
        "health": health,
        "state_dir": str(state_dir),
        "settings": settings,
        "channels": channels,
        "metrics": _metrics_report(state_dir, logs_dir),
        "logs": log_rows,
        "logs_total_bytes": log_total,
        "checked_at": time.time(),
    }


def render(report: dict[str, Any], palette: Palette) -> list[str]:
    ok, warn, bad, dim = palette.ok, palette.warn, palette.bad, palette.dim
    lines: list[str] = []
    problems: list[str] = []

    # ---- 服务 ----
    lines.append(palette.head("服务"))
    if report["listening"]:
        if isinstance(report["health"], dict):
            lines.append(f"  {ok('●')} 端口 {report['port']} 监听中，/health OK")
            # /health 的 providers 是 {id: {...}} 字典（每项带 configured 等字段），
            # 不是带账号数的列表——别再自己编一个 ``accounts`` 字段出来。
            providers = report["health"].get("providers")
            if isinstance(providers, dict) and providers:
                usable = [str(p.get("id") or key) for key, p in providers.items()
                          if isinstance(p, dict) and p.get("configured", True)]
                lines.append(f"    已配置通道 {len(usable)} 个: " + "  ".join(usable[:12])
                             + ("  …" if len(usable) > 12 else ""))
            elif isinstance(providers, list):
                lines.append("    通道: " + "  ".join(
                    str(p.get("id") or p.get("name")) for p in providers
                    if isinstance(p, dict)))
        else:
            lines.append(f"  {warn('●')} 端口 {report['port']} 有进程，但 /health 无响应")
            problems.append("端口在但 /health 不通：看 buddy logs --raw 里的启动报错")
    else:
        lines.append(f"  {dim('○')} 端口 {report['port']} 未监听（服务没起？buddy start）")
        problems.append("服务未运行：buddy usage 只能显示本地账号状态")

    # ---- 配置 ----
    lines.append("")
    lines.append(palette.head("配置" + dim(f"  {report['state_dir']}")))
    if report["settings"]:
        for name, state, detail in report["settings"]:
            mark = ok("正常") if state == "正常" else bad(state)
            lines.append(f"  {pad(name, 28)} {mark:<12} {detail}")
            if state != "正常":
                problems.append(f"{name} {state}：整份设置会静默失效，修好再 buddy restart")
    else:
        lines.append(dim("  （state 目录里没有设置文件，全部走默认值）"))

    # ---- 通道 ----
    lines.append("")
    lines.append(palette.head("通道账号"))
    rows = []
    for c in report["channels"]:
        if c["error"]:
            state_text = bad("读取失败")
        elif not c["enabled"] or not c["accounts"]:
            state_text = dim("未登录")
        elif c["cooling"]:
            state_text = warn(f"{c['cooling']} 个冷却中")
        else:
            state_text = ok("就绪")
        rows.append([c["provider"], c["name"], str(c["accounts"]), state_text])
        if c["error"]:
            problems.append(f"{c['provider']} 账号状态读取失败: {c['error']}")
    lines.extend("  " + ln for ln in table(
        ["通道", "名称", "账号数", "状态"], rows, palette,
        ["left", "left", "right", "left"]))
    cooling_items = [a for c in report["channels"] for a in c["items"] if a.get("cooling")]
    for a in cooling_items[:5]:
        cd = "、".join(f"{x.get('kind') or '冷却'} {x.get('minutes_left', 0):g}分"
                      for x in a.get("cooling") or [])
        lines.append(f"    {dim(a.get('provider') or '')} #{a.get('index')} "
                     f"{a.get('nickname') or a.get('id') or ''}：{cd}")
    soon = [a for c in report["channels"] for a in c["items"]
            if a.get("hours_left") is not None and 0 < a["hours_left"] < 24]
    if soon:
        lines.append("  " + warn("凭证 24 小时内过期（该通道下次请求会自动续期，失败再 buddy login）："))
        for a in soon[:5]:
            lines.append(f"    {a.get('nickname') or a.get('id')}："
                         f"{humanize_duration(a['hours_left'] * 3600)}后")

    # ---- 指标落盘 ----
    lines.append("")
    lines.append(palette.head("指标落盘"))
    m = report["metrics"]
    if not m.get("exists"):
        lines.append(f"  {dim('○')} 还没有 metrics.jsonl（一次请求之后才会生成）")
    else:
        age = time.time() - (m.get("mtime") or 0)
        count = ">500万" if m.get("lines") is None else f"{m['lines']:,}"
        lines.append(f"  {humanize_bytes(m['bytes'])}，{count} 行，"
                     f"最后写入 {humanize_ts(m.get('mtime'))}（{humanize_duration(age)}前）")
        if m.get("archives"):
            lines.append(dim(f"  归档 {m['archives']} 个，共 {humanize_bytes(m.get('archive_bytes'))}"))
        if report["listening"] and age > 86400 and m.get("bytes"):
            lines.append("  " + warn("一天没新行：服务在跑却没记录指标？检查 logs 里的 record 报错"))

    # ---- 磁盘 ----
    lines.append("")
    lines.append(palette.head(f"日志文件（合计 {humanize_bytes(report['logs_total_bytes'])}）"))
    if report["logs"]:
        lines.extend("  " + ln for ln in table(
            ["文件", "大小", "修改时间"], report["logs"], palette,
            ["left", "right", "left"]))
        if report["logs_total_bytes"] > 500 * 1024 * 1024:
            problems.append("日志合计超过 500MB：buddy cleanup 清掉保留窗口外的归档")
    else:
        lines.append(dim("  （没有日志文件）"))

    lines.append("")
    if problems:
        lines.append(palette.head("建议"))
        lines.extend(f"  · {p}" for p in problems)
    else:
        lines.append(ok("没发现明显问题。"))
    return lines


def run(args: Any) -> int:
    palette = Palette(False if getattr(args, "no_color", False) else None)
    report = collect(port=getattr(args, "port", None))
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        for ln in render(report, palette):
            print(ln)
    if not report["listening"]:
        return 1
    if any(s[1] != "正常" for s in report["settings"]):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - 手动调试用
    from .__main__ import main
    sys.exit(main(["doctor", *sys.argv[1:]]))
