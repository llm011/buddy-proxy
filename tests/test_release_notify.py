"""版本检测与右下角「有新版本」提示的契约测试。

覆盖三层，每层都对应一个会真实发生的退化：

1. **版本比较**：字符串比大小会让 ``2.0.10 < 2.0.9``，patch 过 9 之后再也
   检测不到新版本；
2. **检测逻辑**：git 失败（断网/墙/凭据过期）绝不能报「有新版本」；
3. **前端契约**：轮询间隔 5 分钟、点「不再提示」后**停掉轮询**、按版本号
   而非布尔值静音（否则忽略一次就永久静音）。
"""

from __future__ import annotations

import pathlib
import re

import pytest

from buddy_proxy.core import release as rel


_STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
JS = (_STATIC / "release_toast.js").read_text(encoding="utf-8")
HTML = (_STATIC / "index.html").read_text(encoding="utf-8")
CSS = (_STATIC / "style.css").read_text(encoding="utf-8")
WORKFLOW = (pathlib.Path(__file__).resolve().parents[1]
            / ".github/workflows/release.yml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 版本号比较
# ---------------------------------------------------------------------------
def test_version_tuple_handles_multi_digit_patch():
    """2.0.10 必须比 2.0.9 新——字典序会给出相反答案。"""
    assert rel.is_newer("2.0.10", "2.0.9") is True
    assert rel.is_newer("2.0.9", "2.0.10") is False


def test_is_newer_tolerates_different_lengths():
    assert rel.is_newer("2.1", "2.0.9") is True
    assert rel.is_newer("2.0.4", "2.0.4.0") is False
    assert rel.is_newer("", "2.0.4") is False


def test_version_tuple_ignores_non_numeric_suffix():
    # 开发态可能写成 2.0.4.dev1。``dev1`` 不以数字开头，整段记 0，于是
    # 2.0.4.dev1 与 2.0.4 比较不出「更新」——对「有没有新版本」这个用途是对的
    # （dev 后缀不该把版本判成更新），代价是两者视为相等，可以接受。
    assert rel._version_tuple("2.0.4.dev1") == (2, 0, 4, 0)
    assert rel.is_newer("2.0.4.dev1", "2.0.4") is False
    assert rel.is_newer("2.0.5.dev1", "2.0.4") is True


# ---------------------------------------------------------------------------
# 检测逻辑
# ---------------------------------------------------------------------------
def test_read_version_picks_project_version():
    text = '[project]\nname = "x"\nversion = "3.4.5"\n\n[tool.other]\nversion = "9.9"\n'
    assert rel._read_version(text) == "3.4.5"


def test_since_ref_prefers_version_tag(monkeypatch):
    """有 ``v<当前版本>`` 标签时用它锚定，而不是本地 HEAD。

    服务可能跑在功能分支上，那里的 HEAD 含着一堆没合进 main 的提交，
    用 HEAD 算差集会列出无关改动。
    """
    calls = []

    def fake_git(*args, **kw):
        calls.append(args)
        return ""          # rev-parse 成功（标签存在）

    monkeypatch.setattr(rel, "_run_git", fake_git)
    assert rel._since_ref("2.0.4") == "v2.0.4"


def test_since_ref_falls_back_to_head_without_tag(monkeypatch):
    def fake_git(*args, **kw):
        raise RuntimeError("unknown revision")

    monkeypatch.setattr(rel, "_run_git", fake_git)
    assert rel._since_ref("2.0.4") == "HEAD"


def test_notes_drop_release_commits(monkeypatch):
    """自动发版提交不该出现在「更新信息」里——那不是用户关心的改动。"""
    monkeypatch.setattr(rel, "_run_git", lambda *a, **kw: (
        "feat: 新功能\nchore(release): v2.0.5\nfix: 修个 bug\n"))
    notes = rel._git_log_notes("2.0.4")
    assert notes == ["feat: 新功能", "fix: 修个 bug"]


def test_check_reports_no_update_on_git_failure(monkeypatch):
    """fetch 失败（断网/墙）绝不能误报有新版本。"""
    def boom(*a, **kw):
        raise RuntimeError("git fetch 失败: could not resolve host")
    monkeypatch.setattr(rel, "_run_git", boom)
    out = rel.check(fetch=True)
    assert out["has_update"] is False
    assert out["error"]
    assert out["fetch_ok"] is False


def test_check_reports_update_when_remote_is_newer(monkeypatch):
    monkeypatch.setattr(rel, "installed_version", lambda: "2.0.4")
    monkeypatch.setattr(rel, "_run_git", lambda *a, **kw: 'version = "2.0.5"')
    monkeypatch.setattr(rel, "_git_count_ahead", lambda cur: 3)
    monkeypatch.setattr(rel, "_git_log_notes", lambda cur, limit=8: ["feat: 新功能"])
    out = rel.check(fetch=True)
    assert out["has_update"] is True
    assert out["current"] == "2.0.4" and out["latest"] == "2.0.5"
    assert out["behind"] == 3
    assert out["notes"] == ["feat: 新功能"]


def test_check_no_update_when_same_version(monkeypatch):
    monkeypatch.setattr(rel, "installed_version", lambda: "2.0.4")
    monkeypatch.setattr(rel, "_run_git", lambda *a, **kw: 'version = "2.0.4"')
    out = rel.check(fetch=True)
    assert out["has_update"] is False


def test_notes_are_capped(monkeypatch):
    """发布说明只列几点，不能把几十条 commit 全塞进弹窗。"""
    monkeypatch.setattr(rel, "installed_version", lambda: "2.0.4")
    monkeypatch.setattr(rel, "_run_git", lambda *a, **kw: 'version = "2.0.5"')
    monkeypatch.setattr(rel, "_git_count_ahead", lambda cur: 50)
    monkeypatch.setattr(rel, "_git_log_notes", lambda cur, limit=8: [f"c{i}" for i in range(limit)])
    out = rel.check(fetch=True)
    assert len(out["notes"]) == rel.MAX_NOTES


def test_refresh_throttles_within_interval(monkeypatch):
    """5 分钟窗口内重复请求只打一次 git——多个标签页同时开也不该打穿。"""
    calls = []

    def fake_check(**kw):
        calls.append(1)
        return {"current": "2.0.4", "latest": "2.0.4", "has_update": False,
                "behind": 0, "notes": [], "checked_at": int(rel.time.time()),
                "error": None, "fetch_ok": True}

    monkeypatch.setattr(rel, "check", fake_check)
    monkeypatch.setattr(rel, "_store", lambda r: None)  # 不落盘、不改 _last_at
    # 造一份「刚刚检测过」的缓存：第二次调用必须直接命中缓存
    monkeypatch.setattr(rel, "_cache", {"current": "2.0.4", "latest": "2.0.4",
                                        "has_update": False, "behind": 0,
                                        "notes": [], "checked_at": 1,
                                        "error": None, "fetch_ok": True})
    monkeypatch.setattr(rel, "_last_at", rel.time.time())

    rel.refresh(force=False)
    rel.refresh(force=False)
    assert calls == [], "5 分钟内不该重复检测"

    # force=True 必须穿透节流
    rel.refresh(force=True)
    assert len(calls) == 1


def test_refresh_after_interval_rechecks(monkeypatch):
    """超过 5 分钟必须重新检测，否则「有新版本」永远停在旧结论。"""
    calls = []
    monkeypatch.setattr(rel, "check", lambda **kw: (
        calls.append(1) or {"current": "2.0.4", "latest": "2.0.5",
                            "has_update": True, "behind": 1, "notes": ["x"],
                            "checked_at": int(rel.time.time()),
                            "error": None, "fetch_ok": True}))
    monkeypatch.setattr(rel, "_store", lambda r: None)
    monkeypatch.setattr(rel, "_cache", {"current": "2.0.4", "latest": "2.0.4",
                                        "has_update": False, "behind": 0,
                                        "notes": [], "checked_at": 1,
                                        "error": None, "fetch_ok": True})
    # 上次检测是很久以前（超过 POLL_INTERVAL_S）
    monkeypatch.setattr(rel, "_last_at", rel.time.time() - rel.POLL_INTERVAL_S - 1)
    out = rel.refresh(force=False)
    assert len(calls) == 1
    assert out["has_update"] is True


# ---------------------------------------------------------------------------
# 前端契约
# ---------------------------------------------------------------------------
def test_poll_interval_is_five_minutes():
    assert re.search(r"RELEASE_POLL_MS\s*=\s*5\s*\*\s*60\s*\*\s*1000", JS)


def test_mute_stops_polling_and_is_per_version():
    """「不再提示」必须停掉轮询，且按**版本号**记（不是布尔）。"""
    assert "clearInterval(RELEASE_TIMER)" in JS
    assert "RELEASE_MUTE_KEY" in JS
    assert "releaseMute(RELEASE_CURRENT_LATEST)" in JS
    # 布尔值会让「不再提示」永久静音，新版本也提示不出来
    assert "localStorage.setItem(RELEASE_MUTE_KEY, 'true')" not in JS
    assert "localStorage.setItem(RELEASE_MUTE_KEY, true)" not in JS


def test_browser_never_calls_github_directly():
    """检测必须走本机接口：浏览器直连 GitHub 在墙内又慢又不可靠。"""
    assert "'/ui/api/release'" in JS
    assert "github.com" not in JS
    assert "api.github.com" not in JS


def test_toast_dom_and_script_wired():
    assert 'id="release-toast"' in HTML
    assert "/ui/release_toast.js" in HTML
    assert "不再提示" in HTML
    assert ".release-toast" in CSS


def test_notes_are_html_escaped():
    """提交标题来自 git，含 < > & 时不能原样塞进 innerHTML。"""
    assert "releaseEsc(" in JS
    assert ".replace(/</g, '&lt;')" in JS


# ---------------------------------------------------------------------------
# CI 工作流契约
# ---------------------------------------------------------------------------
def test_workflow_bumps_patch_and_guards_loop():
    assert "uv version --bump patch" in WORKFLOW
    # 自动升版的提交本身也会触发 push，必须挡住，否则无限循环
    assert "startsWith(github.event.head_commit.message, 'chore(release):')" in WORKFLOW
    assert "permissions:" in WORKFLOW and "contents: write" in WORKFLOW


def test_workflow_skips_already_tagged_version():
    """人工改版本号并打过 tag 后，CI 不该再 bump 一次。"""
    assert 'git rev-parse "v$cur"' in WORKFLOW
