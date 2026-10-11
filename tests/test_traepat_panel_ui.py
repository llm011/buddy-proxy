"""Trae PAT 面板结构回归：账号状态并入额度卡标题、负载独立两栏。"""

from pathlib import Path


_STATIC = Path(__file__).parents[1] / "src/buddy_proxy/web/static"


def test_account_status_is_backfilled_into_matching_quota_card():
    js = (_STATIC / "benefits_panels.js").read_text("utf-8")

    assert 'data-pat-idx="${idx}"' in js
    assert 'const idx = Number(a.display_index) || (i + 1)' in js
    assert 'card.querySelector(\'.pat-pkg-name\')' in js
    assert 'name.textContent = `PAT #${idx}（${a.id}）`' in js
    assert "tokenText(a.token)" in js
    assert "bits.push(`剩 ${a.hours_left}h`)" in js
    assert "P${a.priority}" not in js, "priority 对用户无意义，不应显示 P10 之类内部值"
    assert 'id="traepat-accounts"' not in js, "不应再在卡片尾部集中重复账号状态"


def test_token_refresh_button_is_in_panel_header():
    js = (_STATIC / "benefits_panels.js").read_text("utf-8")
    head_start = js.index('<div class="pat-head">')
    head_end = js.index('</div>', head_start)
    header = js[head_start:head_end]

    assert "refreshTraepatTokens(this)" in header
    assert "补签 Token" in header


def test_model_load_is_after_quota_and_uses_two_column_grid():
    panel = (_STATIC / "benefits_panels.js").read_text("utf-8")
    checkin = (_STATIC / "benefits_checkin.js").read_text("utf-8")
    css = (_STATIC / "style.css").read_text("utf-8")

    assert panel.index('class="pat-quota-grid"') < panel.index('class="pat-load-section"')
    assert 'class="pat-load-grid"' in checkin
    assert ".pat-load-grid" in css
    assert "repeat(2, minmax(0, 1fr))" in css


def test_pat_provider_has_no_probe_refresh():
    """traepat 不得继承 work 通道的探测式 refresh_models。

    继承版按 self._region（cn）取 work 模型表、拿 work 凭证逐模型发真实请求，
    再用探测结果过滤 PAT 的 models_config 静态列表——模型对不上还白付积分；
    模型页按钮按 callable 判定，置 None 即不出现「重新拉取」（review 2026-10-12）。
    """
    from buddy_proxy.trae.pat_provider import TraePatProvider

    assert TraePatProvider.refresh_models is None
