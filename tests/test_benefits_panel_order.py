"""额度页特殊通道卡片的视觉顺序契约。

顺序由 index.html 里各 ``*-panel`` 占位 div 的 DOM 先后决定（各 render*Panel()
只往自己固定 id 的容器里写），所以这里直接对 HTML 断言。

2026-10-08 用户需求（issue ETHA-39，含追评）：
- 个人账号通道里百度搭子最后、Antigravity 倒数第 2、Qoder 海外版倒数第 3；
- codebuddy / trae 整体上提；
- 每个通道的 CN 与国际版并排（先 CN 后国际版）；
- Trae PAT 是服务账号运维卡，固定收在整个额度页最后。
"""

import re
from pathlib import Path


_INDEX = Path(__file__).parents[1] / "src/buddy_proxy/web/static/index.html"

# 期望的展示顺序（自上而下）。新增通道时同步这里，等于同步一份「用户看到的顺序」。
EXPECTED_ORDER = [
    "codebuddy-panel",
    "codebuddyintl-panel",
    "trae-panel",
    "traeintl-panel",
    "kimi-panel",
    "codex-panel",
    "qoder-panel",
    "qoderintl-panel",
    "antigravity-panel",
    "dumate-panel",
    "traepat-panel",
]


def _panel_order(html):
    return re.findall(r'<div id="([a-z]+-panel)"', html)


def test_panel_dom_order_matches_expected():
    assert _panel_order(_INDEX.read_text("utf-8")) == EXPECTED_ORDER


def test_personal_tail_then_traepat_last():
    """个人账号尾部三卡顺序不变，运维性质的 Trae PAT 永远收在最末。"""
    order = _panel_order(_INDEX.read_text("utf-8"))
    assert order[-1] == "traepat-panel"
    assert order[-2] == "dumate-panel"
    assert order[-3] == "antigravity-panel"
    assert order[-4] == "qoderintl-panel"


def test_cn_panel_precedes_its_intl_twin():
    """CN 与国际版放一起：每对相邻，且 CN 在前。"""
    order = _panel_order(_INDEX.read_text("utf-8"))
    for cn, intl in (("codebuddy-panel", "codebuddyintl-panel"),
                     ("trae-panel", "traeintl-panel"),
                     ("qoder-panel", "qoderintl-panel")):
        assert order.index(intl) == order.index(cn) + 1, f"{cn} 后应紧跟 {intl}"
    # Kimi / Antigravity / 百度搭子没有国际版，仍是单卡。
    assert "kimiintl-panel" not in order


def test_kimi_sits_between_trae_and_qoder():
    """Kimi 保留在 Trae 组与 Qoder 组之间。"""
    order = _panel_order(_INDEX.read_text("utf-8"))
    assert order.index("traeintl-panel") < order.index("kimi-panel") < order.index("qoder-panel")
