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
    "kiro": ("benefits_kiro.js", r"const multi = accts\.length > 1"),
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
    assert "let SESSION_ROTATION_ON = [];" in src


def test_toggle_is_incremental_write():
    """写接口必须是**增量**（只发这一个通道），否则会关掉别的通道。"""
    src = (_STATIC / "benefits_accounts.js").read_text(encoding="utf-8")
    assert "load_balance: {[pid]: !!on}" in src, (
        "切换开关必须只提交目标通道，整表覆盖会把其它通道的开关悄悄关掉")
    assert "session_rotation: {[pid]: !!on}" in src, (
        "会话轮转开关也必须只提交目标通道")


def test_ui_has_session_rotation_toggle():
    src = (_STATIC / "benefits_accounts.js").read_text(encoding="utf-8")
    assert "会话轮转" in src
    assert "setProviderSessionRotation" in src


def test_ui_shows_skipped_models():
    """开关旁要能看出「有模型被跳过」——否则用户会以为均衡对全通道生效。"""
    src = (_STATIC / "benefits_accounts.js").read_text(encoding="utf-8")
    assert "LOAD_BALANCE_EXCLUDED" in src
    assert "已跳过" in src
    assert "showLoadBalanceSkipped" in src


def test_ui_reads_excluded_list_from_settings():
    src = (_STATIC / "benefits_accounts.js").read_text(encoding="utf-8")
    assert "load_balance_excluded" in src, "排除表要由 /ui/api/settings 下发"


# ---------------------------------------------------------------------------
# 账号「临时下线」按钮
# ---------------------------------------------------------------------------
def test_account_disable_helper_and_state():
    src = (_STATIC / "benefits_accounts.js").read_text(encoding="utf-8")
    assert "let DISABLED_ACCOUNTS = [];" in src, "禁用表默认空 = 全启用"
    assert "function acctDisableButton(" in src
    assert "function setAccountDisabled(" in src
    # 增量写入：只提交这一个账号键
    assert "disabled_accounts: {[key]: !!on}" in src
    # 状态由 settings 下发
    assert "DISABLED_ACCOUNTS = r.disabled_accounts" in src


def test_account_disable_button_wired_in_panels():
    """**每个**多账号面板都要接下线按钮。

    这条测试以前是手写清单（copilot/codex/kiro/qoder/trae/codebuddy），
    于是 antigravity 的首屏整卡渲染路径、kimi、traeintl 三个都漏了——用户
    2026-11-22 实报「多账号的额度子卡片里为什么没有下线按钮，你很多没加全」。
    清单式断言只能保证「写进清单的没退化」，保证不了「新增的没漏」。

    现在改成从 ``_PANELS``（面板注册表）派生：凡是登记为多账号面板的通道，
    都必须在**它自己的文件**里出现 ``acctDisableButton('<pid>'``。新增通道
    往 ``_PANELS`` 加一行，这条测试自动覆盖，漏接当场红。
    """
    missing: list[str] = []
    for pid, (fname, _count_expr) in sorted(_PANELS.items()):
        if pid == "dumate":
            # 单账号本地 App、不走 reorder，禁用对它无效 → 不挂假控件
            src = (_STATIC / fname).read_text(encoding="utf-8")
            assert "acctDisableButton('dumate'" not in src, (
                "dumate 是单账号通道，禁用不生效，不该挂假控件")
            continue
        src = (_STATIC / fname).read_text(encoding="utf-8")
        if f"acctDisableButton('{pid}'" not in src:
            missing.append(f"{pid} ({fname})")
    assert not missing, (
        "以下多账号面板没接「下线」按钮：" + "、".join(missing) +
        "。禁用对这些通道经 reorder 真实生效，必须有入口")


def test_account_disable_button_covers_first_paint_path():
    """antigravity 的**首屏整卡渲染**也要带下线按钮（不只是回填路径）。

    ``fillAntigravityAccounts`` 走 ``acctRowButtons``（带第 4 参 acct），
    但首屏是 ``renderAntigravityPanel`` 内联拼 ``rowBtns``——只修回填路径时
    用户看到的仍是「没有下线按钮」。
    """
    src = (_STATIC / "benefits_antigravity.js").read_text(encoding="utf-8")
    # 两处都要有：acctRowButtons 的第 4 参 + 内联 rowBtns
    assert "acctRowButtons('antigravity'," in src and ", delBtn, a)" in src
    inline = re.search(
        r"const rowBtns = `<span class=\"ag-move\">.*?`;", src, re.S)
    assert inline, "找不到 antigravity 首屏的内联 rowBtns"
    assert "acctDisableButton('antigravity'" in inline.group(0), (
        f"首屏 rowBtns 缺下线按钮：{inline.group(0)!r}")


def test_traeintl_card_has_disable_button():
    """traeintl（海外版）卡片要有下线按钮，且 pid 用 'traeintl'。

    海外版与 CN 共用 work 账号池、按 region=global 过滤，``TraeIntlProvider``
    继承父类 forward → ``lb_mod.reorder(..., self.id, ...)``（self.id 即
    'traeintl'），所以键是 'traeintl:<id>'，禁用真实生效。
    """
    src = (_STATIC / "benefits_trae.js").read_text(encoding="utf-8")
    assert "acctDisableButton('traeintl'" in src, (
        "海外版卡片缺下线按钮")
    # 不能拿 CN 号充数：分组名无 #N 时按 region=global 兜底，且只在唯一海外号时给
    assert "a.region === 'global'" in src, (
        "海外面板兜底取账号必须按 region=global 过滤，否则会把 CN 号的下线按钮"
        "画到海外卡上，点下去停用的是另一个区的账号")


def test_account_disable_button_is_always_visible():
    """下线按钮必须**常驻可见**，不能靠 hover 才亮。

    2026-11-21 用户实报「界面上也没有这个操作」：旧样式是
    ``.ag-move { opacity: .35 }`` + ``.pat-pkg:hover .ag-move { opacity: 1 }``，
    再叠加 10px 纯图标灰字紧挨着「✕ 删除」——功能等于被藏起来。
    功能入口不该靠悬停发现，这条测试防止样式被「优化」回隐藏态。
    """
    css = (_STATIC / "style.css").read_text(encoding="utf-8")
    m = re.search(r"\.ag-move\s*\{[^}]*\}", css)
    assert m, "找不到 .ag-move 样式"
    block = m.group(0)
    assert "opacity: 1" in block, (
        f"账号按钮组必须常驻可见，实际样式 {block!r}")
    assert "opacity: .35" not in block, (
        "不要再把按钮调成半透明——用户会以为没有这个操作")


def test_account_disable_button_has_text_label():
    """按钮要带文字（「下线」/「恢复」），不能只有 ⏸/▶ 图标。

    纯图标在 10px 灰字里不可辨，且与相邻的「✕ 删除」无法区分——误点删除
    会清掉凭据（不可撤销），误点下线只是暂停（可恢复）。
    """
    src = (_STATIC / "benefits_accounts.js").read_text(encoding="utf-8")
    m = re.search(r"function acctDisableButton\([^)]*\)\s*\{.*?\n\}", src, re.S)
    assert m, "找不到 acctDisableButton"
    body = m.group(0)
    assert "下线</button>" in body, "下线态按钮要带「下线」文字"
    assert "恢复</button>" in body, "恢复态按钮要带「恢复」文字"
