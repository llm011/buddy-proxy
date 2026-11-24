"""周期标签（周额度 / 5 小时窗）的 CSS 契约。

这些约定只存在于 style.css 里，JS 层的测试拦不住——而它们恰恰是「加了标签
但不把重置时间挤进省略号」的全部依据。2026-11-25 用 headless Chrome 在
400~1100px 逐像素扫过：只要下面任何一条被改掉，带标签的条目就会在某个宽度
区间比「改动前」更早截断（全称标签：29 格变差；色点不绝对定位：14~29 格）。
"""

from __future__ import annotations

import pathlib
import re

STYLE_CSS = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static/style.css"


def _css_without_comments() -> str:
    # 注释里为了说明来龙去脉写了反例与实测表，不剥会先命中它们
    return re.sub(r"/\*.*?\*/", "", STYLE_CSS.read_text(encoding="utf-8"), flags=re.S)


def _container_block(max_width_px: int) -> str:
    """取 ``@container (max-width: Npx) { ... }`` 的整个块（含嵌套的规则）。"""
    css = _css_without_comments()
    start = css.find(f"@container (max-width: {max_width_px}px)")
    assert start >= 0, f"style.css 里找不到 @container (max-width: {max_width_px}px)"
    depth = 0
    for i in range(start, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start:i + 1]
    raise AssertionError("容器查询块没有闭合")


def test_qhead_is_the_container():
    """``.qhead`` 必须是 size 容器，否则下面的 ``@container`` 全部不生效。

    它还要 ``position: relative``：色点档的标签是绝对定位的，需要一个定位
    祖先，否则会相对更外层的卡片定位、飘到别的地方去。
    """
    css = _css_without_comments()
    m = re.search(r"\.qitem \.qhead\s*\{([^}]*)\}", css)
    assert m, "找不到 .qitem .qhead 的声明块"
    decl = m.group(1)
    assert re.search(r"container-type\s*:\s*inline-size", decl), ".qhead 必须是 inline-size 容器"
    assert re.search(r"position\s*:\s*relative", decl), (
        ".qhead 必须 position:relative——色点档标签绝对定位的参照系")


def test_three_tiers_exist_in_order():
    """三档阈值必须都在，且「色点档」比「单字档」更窄（否则永远到不了色点档）。"""
    css = _css_without_comments()
    widths = sorted(int(w) for w in re.findall(r"@container \(max-width:\s*(\d+)px\)", css)
                    if f"@container (max-width: {w}px)" in css and _block_touches_wtag(css, int(w)))
    assert len(widths) >= 2, f"周期标签的容器查询阈值不足两个: {widths}"
    narrow, wide = widths[0], widths[-1]
    assert narrow < wide, f"色点档阈值必须小于单字档: {narrow} !< {wide}"


def _block_touches_wtag(css: str, width_px: int) -> bool:
    start = css.find(f"@container (max-width: {width_px}px)")
    return start >= 0 and ".wtag" in css[start:start + 600]


def test_dot_tier_tag_is_absolutely_positioned():
    """色点档的 ``.wtag`` 必须绝对定位——这是「标签零宽度代价」的全部依据。

    实测：色点即使缩到 5px、去掉 padding，只要还在文字流里，就仍有 14 个
    (条目×宽度) 组合比基线更早截断；改成绝对定位后为 0。
    被改回 ``position: static``（或删掉这条）就是回归。
    """
    css = _css_without_comments()
    widths = sorted(int(w) for w in re.findall(r"@container \(max-width:\s*(\d+)px\)", css)
                    if _block_touches_wtag(css, int(w)))
    narrow_block = _container_block(widths[0])
    m = re.search(r"(?:^|\n)\s*\.wtag\s*\{([^}]*)\}", narrow_block)
    assert m, f"最窄档（{widths[0]}px）里找不到 .wtag 的声明块:\n{narrow_block}"
    decl = m.group(1)
    assert re.search(r"position\s*:\s*absolute", decl), (
        "色点档的 .wtag 必须绝对定位，否则它会挤占文字宽度、把重置时间推进省略号")
    # 不占流内宽度 = 没有外边距、边框与背景也让位
    assert re.search(r"margin\s*:\s*0\b", decl), "色点档 .wtag 必须 margin:0"
    assert re.search(r"border\s*:\s*0\b", decl), "色点档 .wtag 必须 border:0"


def test_dot_tier_reserves_space_for_the_dot():
    """色点浮在文字左侧，``.qlabel`` 必须补等量左内边距，否则色点压在池名第一个字上。"""
    css = _css_without_comments()
    widths = sorted(int(w) for w in re.findall(r"@container \(max-width:\s*(\d+)px\)", css)
                    if _block_touches_wtag(css, int(w)))
    narrow_block = _container_block(widths[0])
    assert re.search(r"\.qlabel\s*\{[^}]*padding-left\s*:\s*\d+px", narrow_block), (
        f"色点档必须给 .qlabel 加 padding-left 让位:\n{narrow_block}")


def test_default_tier_shows_full_text():
    """默认（容器查询不生效的浏览器）显示全称——降级方向是「多占一点」而非「看不懂」。"""
    css = _css_without_comments()
    base = css[:css.find("@container")]
    assert re.search(r"\.wtag \.wtag-short\s*,\s*\.wtag \.wtag-dot\s*\{[^}]*display\s*:\s*none", base), (
        "默认必须隐藏单字与色点")
    assert re.search(r"\.wtag \.wtag-long\s*\{[^}]*display\s*:\s*inline", base), "默认必须显示全称"
