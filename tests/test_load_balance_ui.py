"""额度页的负载均衡开关：**每个多账号通道都要有**，且默认全关。

用户 2026-11-10 要求：只有 codebuddy 有按钮不行，其它渠道也要展示，默认不开。

这是一组静态断言（读 JS 源码），因为各面板的渲染都是拼 HTML 字符串，逐个用
node 桩跑完整 DOM 成本高、且各文件已有自己的 node 用例覆盖渲染本身。这里只钉
「开关被接进每个面板」与「接线用的账号数表达式确实存在于该作用域」两件事——
后者是实际踩过的坑：直接写 ``multi`` 而该面板里没有这个变量，node 一跑就
ReferenceError，页面整个面板渲染失败。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[1] / "src" / "buddy_proxy" / "web" / "static"

#: 每个通道 → 它所在的面板文件。值里的正则用来在**同一函数体内**确认
#: 「账号数」表达式有定义，避免 ``multi`` 这种跨作用域引用。
_PANELS = {
    "codebuddy": ("benefits_codebuddy.js", r"const multi = n > 1"),
    "codebuddyintl": ("benefits_codebuddy.js", r"groups\.size"),
    "antigravity": ("benefits_antigravity.js", r"const n = Math\.max"),
    "kimi": ("benefits_kimi.js", r"const multi = n > 1"),
    "qoder": ("benefits_qoder.js", r"const multi = n > 1"),
    "qoderintl": ("benefits_qoder.js", r"groups\.size"),
    "codex": ("benefits_codex.js", r"const multi = n > 1"),
    "trae": ("benefits_trae.js", r"const multi = n > 1"),
    "traeintl": ("benefits_trae.js", r"groups\.size"),
    "kiro": ("benefits_kiro.js", r"accts\.length"),
    "dumate": ("benefits_dumate.js", r"_dumate_multi"),
}


@pytest.mark.parametrize("pid", sorted(_PANELS))
def test_panel_has_load_balance_toggle(pid):
    fname, _count_expr = _PANELS[pid]
    src = (_STATIC / fname).read_text(encoding="utf-8")
    assert f"providerLoadBalanceToggle('{pid}'" in src, (
        f"{pid} 的面板没有接负载均衡开关——用户要求「所有渠道都展示」")


@pytest.mark.parametrize("pid", sorted(_PANELS))
def test_toggle_account_count_expr_is_in_scope(pid):
    """接线的账号数表达式必须在同一文件里有定义（否则 node 渲染直接抛）。"""
    fname, count_expr = _PANELS[pid]
    src = (_STATIC / fname).read_text(encoding="utf-8")
    assert re.search(count_expr, src), (
        f"{pid} 的开关用了 {count_expr!r}，但文件里找不到它的定义")


def test_generic_quota_card_has_toggle():
    """通用额度卡（zcode/glm/ark 等没有专属面板的通道）也要有开关。"""
    src = (_STATIC / "benefits.js").read_text(encoding="utf-8")
    assert "providerLoadBalanceToggle(p.id" in src
    assert "function _quota_account_count" in src


def test_toggle_helper_defaults_to_off():
    """开关状态默认空 = 全关：不读 settings 时不许显示成已开启。"""
    src = (_STATIC / "benefits_accounts.js").read_text(encoding="utf-8")
    assert "let LOAD_BALANCE_ON = [];" in src


def test_toggle_is_incremental_write():
    """写接口必须是**增量**（只发这一个通道），否则会关掉别的通道。"""
    src = (_STATIC / "benefits_accounts.js").read_text(encoding="utf-8")
    assert "load_balance: {[pid]: !!on}" in src, (
        "切换开关必须只提交目标通道，整表覆盖会把其它通道的开关悄悄关掉")


def test_ui_shows_skipped_models():
    """开关旁要能看出「有模型被跳过」——否则用户会以为均衡对全通道生效。"""
    src = (_STATIC / "benefits_accounts.js").read_text(encoding="utf-8")
    assert "LOAD_BALANCE_EXCLUDED" in src
    assert "已跳过" in src
    assert "showLoadBalanceSkipped" in src


def test_ui_reads_excluded_list_from_settings():
    src = (_STATIC / "benefits_accounts.js").read_text(encoding="utf-8")
    assert "load_balance_excluded" in src, "排除表要由 /ui/api/settings 下发"
