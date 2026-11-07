"""签到卡「强刷」按钮：绕过 5 分钟快照缓存真打上游重查签到状态。

链路：benefits.js 强刷按钮 → POST /ui/api/benefits/refresh 带 ``checkin:
true`` → :meth:`BenefitsManager.invalidate_checkin` 作废 ``checkin:{pid}``
→ snapshot 重查。额度刷新（不带标记）不能连带作废签到缓存——多账号通道
是逐号真查上游，额度「↻」不该把这个代价捎上。
"""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "buddy_proxy" / "web" / "static"


# --- 后端 -------------------------------------------------------------------


def test_invalidate_checkin_pops_only_checkin_key():
    from buddy_proxy.benefits import BenefitsManager

    m = BenefitsManager.__new__(BenefitsManager)
    m._cache = {
        "checkin:trae": (1.0, {}),
        "checkin:qoder": (2.0, {}),
        "quota:trae": (3.0, {}),
    }
    m.invalidate_checkin("trae")
    assert "checkin:trae" not in m._cache
    assert "checkin:qoder" in m._cache, "别的通道不能误伤"
    assert "quota:trae" in m._cache, "额度缓存有自己的刷新入口，不动"
    m.invalidate_checkin("never-cached")  # 无缓存时也不该报错


def test_refresh_api_with_checkin_flag_also_invalidates_checkin(monkeypatch):
    import os
    from types import SimpleNamespace

    from buddy_proxy import __main__ as m
    from buddy_proxy.core import state as st
    from fastapi.testclient import TestClient

    calls: list[str] = []

    class FakeManager:
        def provider_ids(self):
            return {"trae"}

        def invalidate_quota(self, pid):
            calls.append(f"quota:{pid}")
            return 1

        def invalidate_checkin(self, pid):
            calls.append(f"checkin:{pid}")

        async def snapshot(self, disabled_providers=None):
            calls.append("snapshot")
            return {"providers": [], "refreshed": True}

    monkeypatch.setattr(st, "proxy_state", SimpleNamespace(benefits=FakeManager()))
    monkeypatch.setenv("BUDDY_PROXY_ADMIN_OPEN", "1")
    r = TestClient(m.app).post(
        "/ui/api/benefits/refresh", json={"provider": "trae", "checkin": True})
    assert r.status_code == 200
    assert r.json()["refreshed"] is True
    assert calls == ["checkin:trae", "quota:trae", "snapshot"]


def test_refresh_api_without_flag_keeps_checkin_cache(monkeypatch):
    """不带 checkin 标记（额度「↻」）只动作度缓存，不碰签到。"""
    from unittest import mock

    from types import SimpleNamespace

    from buddy_proxy import __main__ as m
    from buddy_proxy.core import state as st
    from fastapi.testclient import TestClient

    checkin_boom = mock.MagicMock(side_effect=AssertionError("不该动作废签到缓存"))

    async def snapshot(disabled_providers=None):
        return {"providers": []}

    monkeypatch.setattr(st, "proxy_state", SimpleNamespace(benefits=SimpleNamespace(
        provider_ids=lambda: {"trae"}, invalidate_quota=lambda pid: 1,
        invalidate_checkin=checkin_boom, snapshot=snapshot)))
    monkeypatch.setenv("BUDDY_PROXY_ADMIN_OPEN", "1")
    r = TestClient(m.app).post("/ui/api/benefits/refresh", json={"provider": "trae"})
    assert r.status_code == 200
    checkin_boom.assert_not_called()


# --- 前端契约 ---------------------------------------------------------------


def test_checkin_card_has_refresh_button_before_claim():
    """每张签到卡上「刷新」在「打卡」左边（卡片化后按钮收在 .ck-actions）。"""
    js = (STATIC / "benefits.js").read_text(encoding="utf-8")
    claim_at = js.index('onclick="claimNow(')
    start = js.rindex('<div class="ck-actions">', 0, claim_at)
    actions = js[start:js.index("</div>", claim_at)]
    assert 'onclick="refreshCheckin(' in actions, "刷新按钮缺失"
    assert ">刷新<" in actions
    assert actions.index("refreshCheckin") < actions.index("claimNow"), \
        "刷新要在「打卡」前面"


def test_refresh_checkin_posts_checkin_flag():
    js = (STATIC / "benefits_checkin.js").read_text(encoding="utf-8")
    assert "async function refreshCheckin(pid, btn)" in js
    fn = js[js.index("async function refreshCheckin"):]
    fn = fn[:fn.index("async function ", 10)]
    assert "'checkin': true" in fn or "checkin: true" in fn
    assert "/ui/api/benefits/refresh" in fn
    assert "loadData(['benefits']" in fn, "刷完要立刻重拉渲染"
    assert "刷新中…" in fn, "多账号逐号真查最坏十几秒，必须给反馈"
    assert "CKREFRESH[pid]" in fn, "在飞标记要真用上（30s 轮询重建 innerHTML 会把按钮态刷掉）"
    assert js.index("const CKREFRESH") < js.index("async function refreshCheckin"), \
        "在飞标记放模块级（QREFRESH 同款坑），不能挂在会被重建的 DOM/闭包里"
