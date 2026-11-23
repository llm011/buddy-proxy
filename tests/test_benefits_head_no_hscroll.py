"""额度面板标题行**不整行横向滚动**：长信息自己滚，按钮永远可见。

## 用户实报（2026-11-24）

「每个 provider 卡片第一行信息有的比较多，会把整行变成左右滚动，这时最右侧的
按钮有可能就看不到了。我想要的不是这样的交互，按钮等这些还是要的，我只是想让
中间的那种文件比较长的限制一定宽度，超出就左右滚动。」

## 旧实现为什么必然这样

    .pat-head { display: flex; overflow-x: auto; white-space: nowrap; }
    .pat-head > * { flex-shrink: 0; }

整行是滚动容器、且**所有**子元素都不许收缩。于是中间那段一旦变长（多账号合计
chip、kiro 的「402/429/403 自动切换」、dumate 的登录态串），整行就成了一条横向
滚动带，右侧的 ↻ / 负载均衡开关 / 导入按钮被推出可视区——**滚动条出现的同时，
按钮就不在视野里了**。

## 现在的结构

    .pat-head            纯布局容器，**不滚**（没有 overflow-x: auto）
    .pat-head-mid        中间段：flex:1 + min-width:0，超出时**自己**滚
    .pat-head-actions    右侧按钮组：flex:0 0 auto，永远贴右可见

## 为什么用静态断言

这类问题在单元测试里跑不出来（没有真实排版引擎），而一旦有人把
``overflow-x: auto`` 加回 ``.pat-head``、或把 ``pat-head-mid`` 的
``min-width: 0`` 删掉，浏览器里就会退回旧行为，单测却全绿。所以直接盯 CSS
规则和面板模板的结构。
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
STYLE_CSS = ROOT / "src/buddy_proxy/web/static/style.css"
STATIC = ROOT / "src/buddy_proxy/web/static"


def _rule(selector: str) -> str:
    """取 style.css 里某个选择器的声明块（剥掉注释，免得命中注释里的反例）。"""
    css = re.sub(r"/\*.*?\*/", "", STYLE_CSS.read_text(encoding="utf-8"), flags=re.S)
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"style.css 里找不到 {selector} 的声明块"
    return m.group(1)


# --- CSS 层 ---------------------------------------------------------------


def test_head_container_does_not_scroll():
    """``.pat-head`` 本身绝不能是滚动容器——那是按钮被挤走的直接原因。"""
    rule = _rule(".pat-head")
    assert "overflow-x: auto" not in rule, (
        ".pat-head 又变成横向滚动容器了：整行会滚，最右侧按钮被推出可视区")
    assert "overflow" not in rule or "overflow-x: auto" not in rule


def test_mid_segment_is_the_only_shrinker():
    """中间段必须可收缩（min-width: 0）且自己滚。"""
    rule = _rule(".pat-head .pat-head-mid")
    assert "min-width: 0" in rule, "缺 min-width:0 时 flex 子项不会收缩到内容以下"
    assert "overflow-x: auto" in rule, "中间段要自己滚，否则长文本撑开整行"


def test_actions_never_shrink():
    """右侧按钮组不收缩，永远可见。"""
    rule = _rule(".pat-head .pat-head-actions")
    assert "flex: 0 0 auto" in rule, "按钮组必须 flex:0 0 auto，否则会被压缩/挤出"
    assert "overflow-x: auto" not in rule, "按钮组自己不该滚动"


# --- 面板模板层 -----------------------------------------------------------

#: 所有会渲染 ``.pat-head`` 的面板文件
_PANEL_FILES = [
    "benefits_antigravity.js",
    "benefits_codebuddy.js",
    "benefits_codex.js",
    "benefits_copilot.js",
    "benefits_dumate.js",
    "benefits_kimi.js",
    "benefits_kiro.js",
    "benefits_qoder.js",
    "benefits_raccoon.js",
    "benefits_trae.js",
    "benefits_panels.js",
]


def _head_blocks(text: str) -> list[str]:
    """切出每个 ``pat-head`` 容器**内部**的 HTML（按 div 嵌套配平，不是固定窗口）。

    固定窗口会越过标题行、把下一段的 ``.pat-col-head`` 也框进来（那里合法地
    用着 ``.grow``），于是断言误报。必须真的配平。
    """
    out = []
    for m in re.finditer(r'<div class="pat-head"[^>]*>', text):
        i = m.end()
        depth = 1
        while i < len(text) and depth:
            nxt_open = text.find("<div", i)
            nxt_close = text.find("</div>", i)
            if nxt_close == -1:
                break
            if nxt_open != -1 and nxt_open < nxt_close:
                depth += 1
                i = nxt_open + 4
            else:
                depth -= 1
                i = nxt_close + 6
        out.append(text[m.end():i])
    return out


@pytest.mark.parametrize("name", _PANEL_FILES)
def test_every_panel_wraps_actions_in_actions_span(name: str):
    """每个面板的按钮都必须落在 ``.pat-head-actions`` 里。

    漏一个，那个面板的按钮就还是会被长信息挤出去——这类「改了一批漏一个」
    正是本仓库踩过的老问题（多账号合计当初也漏过海外版）。
    """
    text = (STATIC / name).read_text(encoding="utf-8")
    blocks = _head_blocks(text)
    assert blocks, f"{name}: 没找到 pat-head"
    for block in blocks:
        has_button = ("<button" in block or "providerRefreshButton(" in block
                      or "providerLoadBalanceToggle(" in block)
        if not has_button:
            continue  # 加载中/空态标题行没有按钮，不需要 actions 容器
        assert 'class="pat-head-actions"' in block, (
            f"{name}: 有个 pat-head 有按钮却没包进 .pat-head-actions")


@pytest.mark.parametrize("name", _PANEL_FILES)
def test_no_panel_uses_legacy_grow_spacer_in_head(name: str):
    """旧的 ``<span class="grow">`` 弹性占位在标题行里已经不该出现。

    它靠「占满剩余空间」把按钮推到右边，但配合旧的 ``flex-shrink: 0`` 时，
    中间内容一长就直接撑破整行。现在改由 ``.pat-head-mid`` 承担。
    """
    text = (STATIC / name).read_text(encoding="utf-8")
    for block in _head_blocks(text):
        assert 'class="grow"' not in block, (
            f"{name}: 标题行里还有 legacy .grow 占位，请改用 .pat-head-mid")
