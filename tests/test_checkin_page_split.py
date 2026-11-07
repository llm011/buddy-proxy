"""签到独立成页（2026-11-07 用户要求）。

背景：原先签到和额度挤在同一个「打卡 & 额度」页，十来张额度卡把签到区压在
最上面，滚两屏就看不见；而签到是每天都要看一眼的动作，额度只是偶尔查。
现在拆成两个页签——「每日签到」与「额度」——但**共用同一份
``/ui/api/benefits`` 快照**：拆页只是展示层的事，不多打一次上游。

这里锁四件事：
1. 页签与页面 DOM 真的分成两个（``page-checkin`` / ``page-benefits``）；
2. 签到页不再渲染额度列表，额度页不再渲染签到行；
3. 打卡完成后只重绘签到区（不再 ``refreshAll()`` 整页重刷）；
4. 一键领取按通道串行、复用同一个 claim 接口。
"""

from __future__ import annotations

import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parents[1] / "src" / "buddy_proxy" / "web" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


# --- 页签结构 ---------------------------------------------------------------


def test_tabs_are_split_into_checkin_and_quota():
    html = _read("index.html")
    assert 'data-tab="checkin"' in html and '每日签到' in html
    assert 'data-tab="benefits"' in html and '额度' in html
    # 老的合并标题不该再出现
    assert "打卡 &amp; 额度" not in html


def test_pages_own_their_sections():
    html = _read("index.html")
    checkin = html[html.index('id="page-checkin"'):html.index('id="page-benefits"')]
    quota = html[html.index('id="page-benefits"'):]
    assert 'id="checkin-rows"' in checkin, "签到卡片网格要在签到页"
    assert 'id="cal"' in checkin, "打卡日历要在签到页"
    assert 'id="ck-claim-all"' in checkin, "一键领取要在签到页"
    assert 'id="quota-list"' not in checkin, "签到页不该再渲染额度列表"
    assert 'id="quota-list"' in quota, "额度页要保留额度列表"
    assert 'id="checkin-rows"' not in quota, "额度页不该再有签到行"


def test_both_tabs_share_the_same_dataset():
    """两页共用 benefits 快照，拆页不新增请求。"""
    js = _read("app.js")
    assert "if (name === 'checkin') return ['stats', 'benefits'];" in js
    assert "if (name === 'benefits') return ['stats', 'benefits'];" in js
    assert "'checkin'" in js[js.index("const TABS"):js.index("const TABS") + 120], \
        "checkin 要进 TABS，否则 switchTab 会把它当非法值弹回总览"


def test_render_dispatch_has_checkin_branch():
    js = _read("app.js")
    assert "tab === 'checkin'" in js and "renderCheckin()" in js


# --- 渲染职责拆分 -----------------------------------------------------------


def test_render_checkin_does_not_render_quota():
    js = _read("benefits.js")
    fn = js[js.index("function renderCheckin("):js.index("function checkinCardHtml(")]
    assert "quota-list" not in fn, "签到页不该写额度列表"
    assert "checkin-rows" in fn and "ck-hero-value" in fn


def test_render_benefits_does_not_render_checkin():
    js = _read("benefits.js")
    fn = js[js.index("function renderBenefits("):js.index("function refreshCheckinBadge(")]
    assert "checkin-rows" not in fn, "额度页不该再写签到行"
    assert "quota-list" in fn


def test_badges_are_independent():
    js = _read("benefits.js")
    assert "cnt-checkin" in js, "签到角标要有自己的挂点"
    assert "cnt-benefits" in js, "额度角标要有自己的挂点"
    ck = js[js.index("function refreshCheckinBadge("):js.index("function refreshBenefitsBadge(")]
    # 签到角标按账号数算，与「一键领取」实际会打的数量一致
    assert "checkinPendingAccounts" in ck


# --- 打卡后的刷新范围 -------------------------------------------------------


def test_claim_now_only_refreshes_checkin_region():
    js = _read("benefits_checkin.js")
    fn = js[js.index("async function claimNow("):js.index("async function saveCheckinSettings(")]
    assert "refreshCheckinOnly()" in fn, "打卡后应只刷新签到区"
    # 只认真正的调用语句（注释里会提到旧实现，别被它绊倒）
    assert not re.search(r"^\s*refreshAll\(\)\s*;", fn, re.M), \
        "不能再整页重刷（会连带 stats/额度/账号面板）"


def test_force_refresh_does_not_redraw_quota_panels():
    js = _read("benefits_checkin.js")
    fn = js[js.index("async function refreshCheckin("):js.index("async function claimNow(")]
    assert "renderCheckin({quiet: true})" in fn
    assert "renderBenefits({quiet: true})" not in fn, \
        "签到页的刷新不该去重建额度页各通道面板"


# --- 一键领取 ---------------------------------------------------------------


def test_claim_all_iterates_claimable_channels():
    js = _read("benefits.js")
    fn = js[js.index("async function claimAllCheckin("):js.index("async function refreshCheckinOnly(")]
    assert "checkinPendingAccounts(p) > 0" in fn, "只该对还有可领账号的通道发请求"
    assert "await api('/ui/api/checkin'" in fn, "复用同一个 claim 接口"
    # 串行：失败一个不影响后面的通道
    assert "try {" in fn and "catch (e)" in fn


def test_claim_all_reports_partial_failure():
    js = _read("benefits.js")
    fn = js[js.index("async function claimAllCheckin("):js.index("async function refreshCheckinOnly(")]
    assert "failed.push" in fn and "toast(" in fn, "部分失败要说清是哪个通道"


# --- 视觉层 -----------------------------------------------------------------


def test_checkin_cards_have_state_classes():
    js = _read("benefits.js")
    fn = js[js.index("function checkinCardHtml("):js.index("function renderBenefits(")]
    for cls in ("ck-card", "ck-status", "ck-card-accounts", "ck-card-foot", "ck-actions"):
        assert cls in fn, f"卡片缺少 {cls}"
    css = _read("style.css")
    for state in ("ck-done", "ck-pending", "ck-unknown"):
        assert f".ck-status.{state}" in css, f"状态样式缺 {state}"


def test_unknown_state_cannot_be_claimed_from_card():
    js = _read("benefits.js")
    fn = js[js.index("function checkinCardHtml("):js.index("function renderBenefits(")]
    assert "const disabled = unknown || pending === 0;" in fn
    assert "${disabled ? 'disabled' : ''}" in fn


def test_checkin_grid_is_self_adapting():
    css = _read("style.css")
    assert re.search(r"\.ck-grid\s*\{[^}]*repeat\(auto-fill,\s*minmax\(330px", css, re.S)
    assert re.search(r"\.ck-grid\s*\{[^}]*gap:", css, re.S)
