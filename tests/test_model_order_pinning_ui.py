"""模型顺序页置顶与全局按钮提示的静态行为契约。

置顶是展示偏好，不能混入 model_order 的候选路由顺序；提示层接管 button title
之后也必须保留键盘焦点用户的路径。
"""
from __future__ import annotations

import pathlib


STATIC = pathlib.Path(__file__).resolve().parents[1] / "src" / "buddy_proxy" / "web" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_order_pins_are_display_only_and_most_recent_first():
    js = _read("app_order.js")
    fn = js[js.index("function orderPinnedFirst("):js.index("async function orderTogglePin(")]
    assert "ORDER.pinned" in fn
    assert "rank.get" in fn, "置顶列表顺序必须决定展示优先级"
    assert "rows.map" in fn and ".map(({ row }) => row)" in fn, \
        "排序不能原地改写 model_order 返回行"
    assert "rows.indexOf" not in fn, "比较器不应在排序期间反复线性扫描"

    toggle = js[js.index("async function orderTogglePin("):js.index("// 某个模型名的配置")]
    assert "'/ui/api/model-order/pin'" in toggle
    assert "pinned: next" in toggle
    assert "ORDER.pinned" in toggle


def test_order_pin_card_has_reversible_control():
    js = _read("app_order.js")
    card = js[js.index("function orderPageCard("):js.index("function orderToggle(")]
    assert "order-pinned" in card
    assert "orderTogglePin" in card
    assert "已置顶，点击取消" in card
    assert "最近置顶的在最上" in card


def test_global_button_tooltip_has_pointer_and_keyboard_lifecycle():
    js = _read("app.js")
    tooltip = js[js.index("(function initGlobalTooltip()") : js.index("const RANGE_TABS")]
    assert "button[title]" in tooltip
    assert "target.removeAttribute('title')" in tooltip
    assert "tip.setAttribute('role', 'tooltip')" in tooltip
    assert "target.setAttribute('aria-label', t)" in tooltip, \
        "迁走 title 后图标按钮仍须有可访问名称"
    assert "aria-describedby" in tooltip and "function clearTip(" in tooltip, \
        "显示时关联语义 tooltip，离开时必须恢复既有描述"
    assert "document.addEventListener('mouseover'" in tooltip
    assert "document.addEventListener('mouseout'" in tooltip
    assert "document.addEventListener('focusin'" in tooltip
    assert "document.addEventListener('focusout'" in tooltip
    assert "target.contains(e.relatedTarget)" in tooltip, "按钮内部移动不应闪烁或提前隐藏提示"
