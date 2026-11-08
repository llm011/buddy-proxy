"""页签角标无数据时不能渲染成一个空心小圆点（2026-11-08 用户反馈）。

现象：切到「模型」「模型顺序」页签前，标题后面各挂着一个灰黑色小圆点，
看着像「有个 0」或者「加载失败」。根因是角标 ``.tab .cnt`` 是个带
border + padding 的胶囊，JS 数据未加载时把 ``textContent`` 置空——
胶囊本身还在，于是渲染成一颗孤零零的点。

这里锁两件事：
1. CSS 有 ``.tab .cnt:empty { display: none; }``；
2. 三处角标更新（模型/顺序/签到/额度）在没有数据时**真的**留下空节点
   （``textContent = ''``），而不是写 ``'0'`` 或占位符——否则上面的 CSS
   永远命不中。
"""

from __future__ import annotations

import pathlib

STATIC = pathlib.Path(__file__).resolve().parents[1] / "src" / "buddy_proxy" / "web" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_empty_badge_is_hidden_by_css():
    css = _read("style.css")
    assert ".tab .cnt:empty { display: none; }" in css, (
        "空角标必须整块隐藏，否则 border+padding 会渲染成一个小圆点"
    )


def test_badges_blank_out_instead_of_showing_zero():
    """数据未加载/无内容时置空串——这是 `:empty` 生效的前提。

    写成 ``'0'`` 或 ``'—'`` 会显示成有内容的角标，被这条测试挡住。
    """
    app_js = _read("app.js")
    assert "document.getElementById('cnt-models').textContent =" in app_js
    assert "document.getElementById('cnt-order').textContent" in app_js
    # 两处都留了 `|| ''` / `: ''` 的兜底（模型数是 reduce 的 0，顺序是空数组）
    assert "|| '';" in app_js

    benefits_js = _read("benefits.js")
    assert "el.textContent = ''; el.className = 'cnt';" in benefits_js, "签到角标无数据要置空"
    assert "el.textContent = q.length ? String(q.length) : '';" in benefits_js, \
        "额度角标无数据要置空而不是写 0"


def test_badges_are_inside_the_tab_buttons():
    """角标必须挂在页签按钮里（`.tab .cnt` 选择器才命中）。"""
    html = _read("index.html")
    for tid in ("cnt-models", "cnt-order", "cnt-checkin", "cnt-benefits"):
        idx = html.index(f'id="{tid}"')
        # 往前找最近的 <button，且中间没有闭合 </button>
        head = html[:idx]
        assert head.rfind("<button") > head.rfind("</button>"), f"{tid} 不在页签按钮内"
        assert 'class="cnt"' in html[idx - 40:idx], f"{tid} 缺少 .cnt 类"
