"""签到卡「减负」契约（用户 2026-10-07 反馈，静态断言）。

三件事：① 通道名旁的聚合状态徽标在带逐账号明细行时不再重复；② 上游
campaign key（act-20260930-551 这类）不再当 chip 展示——对用户纯噪音；
③ 「每日 +X」保留。

渲染函数整体依赖 BENEFITS/DOM，离网 JS harness 的桩成本与这几行不成比例，
故对 benefits.js 做静态断言；改成结构性重构时这里要跟着挪。
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

BENEFITS_JS = (pathlib.Path(__file__).resolve().parents[1]
               / "src/buddy_proxy/web/static/benefits.js")


def test_top_level_badge_hidden_when_account_rows_exist():
    text = BENEFITS_JS.read_text(encoding="utf-8")
    assert "const badge = (c.accounts && c.accounts.length && !c.error) ? '' : st;" in text, \
        "有明细行时第一行徽标要去掉（顶层查询失败除外）"
    assert "${badge}${meta}" in text, "br-meta 应渲染 badge 而非裸 st"


def test_activity_name_chip_is_gone():
    text = BENEFITS_JS.read_text(encoding="utf-8")
    assert "chips.push(c.activity_name)" not in text, \
        "上游 campaign key 不再上卡（用户反馈看不懂）"


def test_daily_credit_chip_carries_account_count():
    """多账号的「每日 +X」是总和，带（N账号）说明口径（用户 2026-10-07 要求）。"""
    text = BENEFITS_JS.read_text(encoding="utf-8")
    assert "（${n}账号）" in text, "总和 chip 要带账号数后缀"


def _fn(text: str, name: str) -> str:
    match = re.search(rf"^function {name}\(.*?\n\}}\n", text, re.S | re.M)
    assert match, f"找不到 {name}"
    return match.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 跑前端 JS")
def test_account_details_show_real_fields_and_escape_messages():
    text = BENEFITS_JS.read_text(encoding="utf-8")
    script = """
globalThis.esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
globalThis.fmtCredit = n => String(n);
""" + _fn(text, "checkinAccountState") + _fn(text, "checkinAccountDetails") + """
const html = checkinAccountDetails({accounts: [
  {index: 1, name: '甲<admin>', ok: true, message: '<b>不要执行</b>', daily_credit: 100},
  {index: 2, error: '查询超时', claimable: true},
  {index: 3, unavailable: true, message: 'Free 无活动'},
]});
console.log(JSON.stringify(html));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    html = proc.stdout.strip()
    assert '甲&lt;admin&gt;' in html and '&lt;b&gt;不要执行&lt;/b&gt;' in html
    assert '每日 +100' in html and '查询失败' in html and '查询超时' in html
    assert '<b>不要执行</b>' not in html
    assert html.count('ck-account-card') == 3


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 跑前端 JS")
def test_detail_toggle_survives_rerender_and_does_not_touch_other_actions():
    text = BENEFITS_JS.read_text(encoding="utf-8")
    script = _fn(text, "checkinDetailToggle") + """
const CHECKIN_DETAIL_FOLD = {open: {}};
const body = {hidden: true};
const btn = {dataset: {checkinFold: "tr'ae"}, attrs: {'aria-expanded': 'false'}, textContent: '',
  setAttribute(n, v) { this.attrs[n] = v; }};
globalThis.document = {getElementById: () => body};
checkinDetailToggle(btn);
const opened = {hidden: body.hidden, aria: btn.attrs['aria-expanded'], text: btn.textContent};
// 模拟 30 秒重绘：新 DOM 按 JS 状态重建，而不是读旧 DOM。
const newBody = {hidden: !CHECKIN_DETAIL_FOLD.open["tr'ae"]};
checkinDetailToggle(btn);
console.log(JSON.stringify({opened, redrawHidden: newBody.hidden,
  closed: body.hidden, aria: btn.attrs['aria-expanded']}));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    import json
    result = json.loads(proc.stdout)
    assert result["opened"] == {"hidden": False, "aria": "true", "text": "收起明细 ▴"}
    assert result["redrawHidden"] is False
    assert result["closed"] is True and result["aria"] == "false"


def test_only_multiple_accounts_get_detail_controls():
    text = BENEFITS_JS.read_text(encoding="utf-8")
    assert "const multiAccounts = (c.accounts || []).length > 1;" in text
    assert "const detailToggle = multiAccounts" in text
    assert "checkinAccountDetails(c)" in text
    assert 'class="ck-account-grid"' in text
    # 折叠入口是独立按钮，不包住改名/强刷/打卡，也不需要事件冒泡补丁。
    assert 'onclick="checkinDetailToggle(this)"' in text
    assert "checkinDetailToggle('${esc(p.id)}')" not in text
    assert "stopPropagation" not in text
