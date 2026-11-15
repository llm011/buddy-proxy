"""终端渲染小工具：颜色、中英文对齐宽度、表格、条形图。

只用 ANSI 转义、不引第三方库：CLI 要在 `buddy install` 装到的任何环境里跑
（用户的 python3 不一定跟仓库 venv 一致），多一个依赖就多一处装不上的可能。

三条贯穿全模块的约定：

- **颜色走 stdout 是否 tty 判断**，不是只看 ``NO_COLOR``：`buddy usage | tee`
  时管道不是 tty，带颜色只会把日志文件写脏。``FORCE_COLOR=1`` 可以强行开
  （CI 截图/录屏用），``NO_COLOR``（任何值）优先于一切。
- **宽度按东亚宽字符算**，不是 ``len()``：``codebuddy`` 11 格、``通道`` 4 格，
  用 ``len("通道")==2`` 排表，中英混排的列永远歪。对齐错了表格就没法读，
  这不是美化问题。
- **窄终端不截断数据**：表格列宽随内容收缩，超出终端宽度的行原样输出让它
  自然折行——把模型名截成 ``gpt-4o-2…`` 后再去查就查不到了。
"""

from __future__ import annotations

import os
import sys
import unicodedata
from typing import Any, Iterable, Optional, Sequence

_CODES = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}


def color_enabled(stream: Any = None) -> bool:
    stream = stream if stream is not None else sys.stdout
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR") in ("1", "true", "yes"):
        return True
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


class Palette:
    """按需着色的包装。``Palette(enabled=False)`` 时所有方法原样返回。"""

    def __init__(self, enabled: Optional[bool] = None) -> None:
        self.enabled = color_enabled() if enabled is None else bool(enabled)

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        prefix = "".join(_CODES.get(s, "") for s in styles)
        return f"{prefix}{text}{_CODES['reset']}"

    def bold(self, text: str) -> str:
        return self(text, "bold")

    def dim(self, text: str) -> str:
        return self(text, "dim")

    def ok(self, text: str) -> str:
        return self(text, "green")

    def warn(self, text: str) -> str:
        return self(text, "yellow")

    def bad(self, text: str) -> str:
        return self(text, "red")

    def head(self, text: str) -> str:
        return self(text, "bold", "cyan")


def display_width(text: str) -> int:
    """终端显示宽度（东亚宽/全角字符算 2 格）。"""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度补空格（``text`` 里的 ANSI 必须在调用前套好）。"""
    gap = width - display_width(text)
    if gap <= 0:
        return text
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    palette: Palette,
    aligns: Optional[Sequence[str]] = None,
) -> list[str]:
    """渲染固定宽度表格，返回逐行字符串（调用方自己 print）。

    空行列表 → 返回 ``[]``：调用方据此打印「无数据」而不必再判一次。
    """
    rows = [["" if c is None else str(c) for c in r] for r in rows]
    if not rows:
        return []
    aligns = list(aligns or ["left"] * len(headers))
    widths = [display_width(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], display_width(cell))
    out = []
    head_cells = [palette.head(pad(h, widths[i])) for i, h in enumerate(headers)]
    out.append("  ".join(c.rstrip() if i == len(headers) - 1 else c
                         for i, c in enumerate(head_cells)).rstrip())
    out.append("  ".join("─" * w for w in widths))
    for r in rows:
        cells = []
        for i, w in enumerate(widths):
            cell = r[i] if i < len(r) else ""
            cells.append(pad(cell, w, aligns[i] if i < len(aligns) else "left")
                         if not cell.startswith("\033") else cell)
        out.append("  ".join(cells).rstrip())
    return out


def bar(percent: Optional[float], width: int = 20, palette: Optional[Palette] = None) -> str:
    """按「已用百分比」画进度条；``percent`` 为 None 时返回占位符。

    ``percent`` 沿用上游语义：0 = 全新，100 = 用完。>100（超支）画满并标红。
    """
    if percent is None:
        return "─" * width
    try:
        pct = float(percent)
    except (TypeError, ValueError):
        return "─" * width
    filled = max(0, min(width, round(pct / 100 * width)))
    body = "█" * filled + "░" * (width - filled)
    if palette is not None:
        if pct >= 90:
            body = palette.bad(body)
        elif pct >= 70:
            body = palette.warn(body)
    return body


def humanize_bytes(n: Optional[float]) -> str:
    if not n or n < 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def humanize_ts(ts: Optional[float]) -> str:
    """epoch 秒 → 本地 ``YYYY-MM-DD HH:MM``；无效/0 返回 ``—``。"""
    import time

    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "—"


def humanize_duration(seconds: Optional[float]) -> str:
    """剩余秒数 → ``3天2小时`` / ``4小时12分`` / ``8分钟``。"""
    if seconds is None:
        return "—"
    seconds = max(0.0, float(seconds))
    days = int(seconds // 86400)
    hours = int(seconds % 86400 // 3600)
    minutes = int(seconds % 3600 // 60)
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分钟"


def humanize_number(value: Any) -> str:
    """整数带千分位、小数保留最多 2 位；None → ``—``。"""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,.2f}"
    return str(value)


def humanize_ms(ms: Any) -> str:
    if ms is None:
        return "—"
    try:
        value = float(ms)
    except (TypeError, ValueError):
        return "—"
    if value >= 1000:
        return f"{value / 1000:.1f}s"
    return f"{value:.0f}ms"
