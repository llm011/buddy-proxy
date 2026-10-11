"""渠道标题整体额度刷新与 TraeIntl 展示回归。"""

from __future__ import annotations

import pathlib


_STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
CORE = (_STATIC / "benefits.js").read_text(encoding="utf-8")
PANELS = (_STATIC / "benefits_panels.js").read_text(encoding="utf-8")
CHECKIN = (_STATIC / "benefits_checkin.js").read_text(encoding="utf-8")
ACCOUNTS = (_STATIC / "benefits_accounts.js").read_text(encoding="utf-8")


def test_every_dedicated_panel_header_has_provider_refresh():
    """专属面板的 ↻ 全在渠道标题，一次刷新整个 provider。"""
    combined = CORE + PANELS + CHECKIN
    for provider in (
        "traepat", "antigravity", "qoder", "qoderintl", "trae", "traeintl",
        "codebuddy", "codebuddyintl", "kimi", "dumate",
    ):
        assert f"providerRefreshButton('{provider}')" in combined, provider

    assert "providerRefreshButton(p.id)" in CORE, "通用额度卡标题也要可刷新"


def test_account_rows_do_not_duplicate_quota_refresh():
    """账号级按钮只负责改名/排序/删除，不重复摆渠道刷新。"""
    helper = ACCOUNTS[ACCOUNTS.index("function acctRowButtons"):]
    helper = helper[:helper.index("function groupAccountsByPrefix")]
    assert "refreshProviderQuota" not in helper
    assert "${refreshBtn}" not in PANELS
    assert "${refreshBtn}" not in CHECKIN


def test_refresh_uses_returned_snapshot_without_second_get():
    """refresh POST 已给完整快照时直接重绘；旧服务响应才回退强制 GET。"""
    fn = PANELS[PANELS.index("async function refreshProviderQuota"):]
    fn = fn[:fn.index("function renderAntigravityPanel")]
    assert "Array.isArray(snapshot.providers)" in fn
    assert "BENEFITS = snapshot" in fn
    assert "renderBenefits()" in fn
    assert "await loadData(['benefits'], true)" in fn


def test_traeintl_has_dedicated_quota_panel_and_explains_no_checkin():
    """海外额度走独立面板，签到区明确说明不支持且不提供领取按钮。"""
    index = (_STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="traeintl-panel"' in index
    assert "renderTraeIntlPanel()" in CORE
    assert "p.id !== 'traeintl'" in CORE, "专属面板存在时通用额度卡必须排除，避免重复"

    panel = PANELS[PANELS.index("function renderTraeIntlPanel"):]
    panel = panel[:panel.index("// ---- CODEBUDDY 面板")]
    assert "p.id === 'traeintl'" in panel
    assert "providerRefreshButton('traeintl')" in panel
    assert "海外版无每日签到" in panel


def test_checkin_rows_skip_unsupported_providers():
    """签到区只列 supported 的通道：traeintl（上游无签到端点）整行不出现，
    挂着一行「无签到」只会让人奇怪（2026-10-12 用户反馈）。它不渲染任何
    形态——包括说明行——保证想找它信息的人去额度卡看面板内的说明。"""
    core = CORE
    # 过滤器不再为 traeintl 开后门：一律按 supported 判
    assert "p.id === 'traeintl'" not in core[core.index("const rows = (BENEFITS.providers"):core.index("document.getElementById('checkin-rows')")]
    # 旧的 traeintl 特殊说明行已删
    assert "海外版无每日签到" not in core
    # 专属额度面板里的说明仍在（展示职责移到那里）
    panel = PANELS[PANELS.index("function renderTraeIntlPanel"):]
    panel = panel[:panel.index("// ---- CODEBUDDY 面板")]
    assert "海外版无每日签到" in panel
