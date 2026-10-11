"""额度页侧边导航的静态几何断言：fixed 导航永远不许压到内容区。

真实的 bug（2026-10-11 用户截图）：``left`` 按「导航宽约 88px」预算留 104px，
但条目名来自通道 name 且 ``white-space: nowrap`` 又不限宽——
「Antigravity (Gemini/Claude/GPT free tier)」一条就把导航撑到 330px+，
右缘怼进 wrap 内容区 200px+，直接叠在打卡日历上。且 1280-1388px 窗口下
``left`` 被 clamp 到 12px，按真实宽度算同样必压内容——原断点本身就不成立。

修法三件套（本文件逐条盯住：改回去不报错，只是又叠上）：

1. **宽度钉死**：``.sidenav`` 声明固定 px 宽——几何不再随条目名漂移；
2. **条目省略**：``.sn-label`` overflow hidden + ellipsis——长名截断而不是撑宽；
3. **断点够宽**：min-width 断点下，(100vw-1180)/2 的侧边余量 ≥ clamp 左缘 +
   导航宽 + 间距，left 被 clamp 到最小值时右缘也在 wrap 内容区外——
   窄窗口宁可整块隐藏（display:none）。

参照 ``test_pat_panel_layout_css.py`` 的先例：布局回归靠静态断言盯，
不指望人眼复验。
"""

from __future__ import annotations

import pathlib
import re

STYLE_CSS = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static/style.css"
BENEFITS_JS = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static/benefits.js"


def _css() -> str:
    # 注释里为了说明来龙去脉会写「反例」数值，不剥会先命中它
    return re.sub(r"/\*.*?\*/", "", STYLE_CSS.read_text("utf-8"), flags=re.S)


def _sidenav_base() -> str:
    """媒体查询外的 .sidenav 声明块（display:none + 钉死的宽度）。"""
    m = re.search(r"\.sidenav\s*\{([^}]*)\}", _css())
    assert m, "style.css 里找不到 .sidenav 基础声明块（选择器改名了？）"
    return m.group(1)


def _sidenav_media() -> tuple[int, str]:
    """min-width 媒体查询里的 .sidenav 声明块，返回 (断点px, 声明块)。"""
    m = re.search(r"@media \(min-width:\s*(\d+)px\)\s*\{\s*\.sidenav\s*\{([^}]*)\}", _css())
    assert m, "找不到 .sidenav 的 min-width 媒体查询块"
    return int(m.group(1)), m.group(2)


def _sidenav_width() -> int:
    decl = _sidenav_base()
    m = re.search(r"width:\s*(\d+)px", decl)
    assert m, f".sidenav 没钉死宽度（width: Npx），实际: {decl!r}"
    return int(m.group(1))


def test_sidenav_width_is_pinned():
    """宽度必须钉死：条目名来自通道 name（长的 330px+），不限宽时导航
    实际宽度随内容漂移，left 的定位预算全部失效——这就是压到日历的根因。"""
    assert _sidenav_width() > 0


def test_sidenav_left_anchor_clears_content():
    """left 锚点的让位量 ≥ 导航宽 + 12px：未 clamp 时右缘恒在
    wrap 左缘（=(100vw-1180)/2）外至少 12px。"""
    _, media = _sidenav_media()
    m = re.search(
        r"left:\s*max\((\d+)px,\s*calc\(\(100vw\s*-\s*1180px\)\s*/\s*2\s*-\s*(\d+)px\)\)",
        media)
    assert m, f".sidenav 的 left 不是 max(钉值, calc((100vw-1180px)/2 - 让位)) 形态，实际: {media!r}"
    min_left, offset = int(m.group(1)), int(m.group(2))
    assert offset - _sidenav_width() >= 12, (
        f"left 让位 {offset}px 只够导航 {_sidenav_width()}px + "
        f"{offset - _sidenav_width()}px 间距，不足 12px——未 clamp 时右缘会贴上/压进内容区"
    )


def test_sidenav_breakpoint_leaves_room_when_clamped():
    """断点必须保证 clamp 生效（left 取最小钉值）时右缘也清出内容区：
    断点 ≥ 1180 + 2*(最小左缘 + 导航宽 + 10px)。原版断点 1280 只按
    88px 的想象宽度算过，实际宽度下 clamp 段全在压内容。"""
    bp, media = _sidenav_media()
    m = re.search(r"left:\s*max\((\d+)px,", media)
    assert m, "left 里没有 clamp 最小值"
    min_left = int(m.group(1))
    need = 1180 + 2 * (min_left + _sidenav_width() + 10)
    assert bp >= need, (
        f"断点 {bp}px 不够：clamp 到 {min_left}px 时导航右缘 "
        f"{min_left + _sidenav_width()}px 会压进内容区（左缘 {(bp - 1180) // 2}px），"
        f"按当前宽度断点至少要 {need}px"
    )


def test_sn_label_ellipsizes():
    """长条目名必须省略号截断而不是把锚点撑宽：.sn-label 是 flex:1 +
    min-width:0 + overflow:hidden + ellipsis 四件套，缺一个都会退化成
    「内容顶宽导航」。"""
    css = _css()
    m = re.search(r"\.sidenav a \.sn-label\s*\{([^}]*)\}", css)
    assert m, "style.css 里找不到 .sidenav a .sn-label 声明块"
    decl = m.group(1)
    for prop in ("min-width: 0", "overflow: hidden", "text-overflow: ellipsis"):
        assert prop in decl, f".sn-label 缺 {prop}，实际: {decl!r}"
    # 基础宽度钉死后，锚点们（.sidenav a）还留着 nowrap：允许，但必须由
    # .sn-label 截断兜底——这里顺带确认锚点规则没有自己的 width 撑开手段
    a_rule = re.search(r"\.sidenav a\s*\{([^}]*)\}", css)
    assert a_rule and "width" not in a_rule.group(1), "锚点自身不应再声明宽度"


def test_nav_labels_wired_through_sn_label():
    """JS/CSS 的类名契约：renderBenefitsNav 的条目模板必须把文字放进
    sn-label（否则省略号选择器落空，长名横向顶出 132px 导航），并带
    title 悬停补全名（rail 标签剥了括注）。"""
    src = BENEFITS_JS.read_text("utf-8")
    idx = src.find("nav.innerHTML = labels.map")
    assert idx >= 0, "benefits.js 里找不到 renderBenefitsNav 的条目模板"
    tmpl = src[idx: idx + 600]
    assert 'class="sn-label"' in tmpl, "条目模板没把标签文字包进 sn-label"
    assert "title=" in tmpl, "条目模板没挂 title（剥括注后全名悬停不可见）"
