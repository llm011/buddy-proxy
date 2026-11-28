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


def test_refresh_does_not_deadlock_when_already_checking(monkeypatch):
    """已有一轮在跑时 refresh 必须立即返回，不能自锁死。

    原来的实现走到 ``if _checking`` 分支后调了 ``cached()``，而 ``cached()``
    也要拿同一把 ``threading.Lock``（不可重入）——于是持锁等锁，接口永久挂起，
    且此后每次请求都卡住。
    """
    import threading

    monkeypatch.setattr(rel, "_cache", None)
    monkeypatch.setattr(rel, "_checking", True)
    done = []
    t = threading.Thread(target=lambda: (rel.refresh(force=True), done.append(1)),
                         daemon=True)
    t.start()
    t.join(timeout=3)
    assert done, "refresh 在 _checking=True 时死锁了"


def test_installed_version_prefers_repo_pyproject(monkeypatch, tmp_path):
    """版本要读「正在跑的这份源码」，而不是 venv 里可能陈旧的 dist-info。

    ``git pull`` 后没 ``uv sync`` 时 dist-info 还停在旧版本，只信它就会
    「明明已是最新代码却一直提示有新版本」。
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "buddy-proxy"\nversion = "9.9.9"\n', encoding="utf-8")
    monkeypatch.setattr(rel, "_REPO_ROOT", tmp_path)
    assert rel.installed_version() == "9.9.9"


def test_installed_version_falls_back_to_metadata(monkeypatch, tmp_path):
    """源码树读不到时才退回 dist-info。"""
    monkeypatch.setattr(rel, "_REPO_ROOT", tmp_path)   # 目录里没有 pyproject.toml
    monkeypatch.setattr(rel, "_read_version", lambda text: "")
    import importlib.metadata as md
    monkeypatch.setattr(md, "version", lambda name: "1.2.3")
    assert rel.installed_version() == "1.2.3"


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
def test_update_button_calls_real_update():
    """「更新」必须真的更新，而不是只刷新浏览器。

    以前的实现是 ``onclick="location.reload()"``——刷新后服务端代码没变，
    ``/ui/api/release`` 依然报「有新版」，弹窗又出现，用户看到的就是
    「点了更新，弹窗还在」。所以这里锁住：按钮调的是会发 POST 的那个函数。
    """
    assert 'onclick="releaseRunUpdate(this)">更新</button>' in HTML
    assert 'onclick="location.reload()">更新</button>' not in HTML, (
        "按钮退回成只刷新页面了——那样服务端代码不会变，弹窗必然重现")
    assert "/ui/api/release/update" in JS
    assert "location.reload()" in JS, "更新完成后仍应自动刷新页面"
    assert "知道了" not in HTML
    assert "releaseDismissCurrent" not in JS
    assert "RELEASE_DISMISS_KEY" not in JS


def test_update_polls_until_version_catches_up():
    """点更新后要轮询等版本追平，不能固定 sleep 一下就 reload。

    更新是 git pull + uv sync + 重启，耗时不定；固定等待要么白等、要么在
    服务还没起来时就刷新，页面直接打不开。
    """
    assert "releaseWaitForRestart" in JS
    # 重启期间连不上是预期的，必须 continue 而不是放弃
    m = re.search(r"catch\s*\(\s*e\s*\)\s*\{\s*//[^\n]*\n\s*continue;", JS)
    assert m, "重启期间请求失败就把轮询中止了——那永远等不到新版本"
    assert "RELEASE_UPDATE_TIMEOUT_MS" in JS
    # 用版本号变化判断，而不是只看 has_update
    assert "RELEASE_CURRENT_VERSION" in JS


def test_workflow_retries_push_after_concurrent_merge():
    """两次合并靠得近时远端会前进，直接 push 会被拒——必须 rebase 重试。"""
    assert "git rebase origin/main" in WORKFLOW
    assert "git fetch origin main --quiet" in WORKFLOW


def test_workflow_tags_after_successful_push():
    """标签要在 push 成功之后打：rebase 会重写 sha，先打的标签会指向被丢弃的提交。"""
    push_at = WORKFLOW.index("if git push origin HEAD:main")
    tag_at = WORKFLOW.index('git tag "v$new"')
    assert tag_at > push_at, "git tag 出现在 push 成功之前，rebase 后会指向孤儿提交"


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
    """跳过条件必须按 **HEAD** 判断，不能按「同名 tag 是否存在」。

    按 tag 存在与否判断会让版本号永久卡死：自动发版完成后 main 上
    pyproject 已是 2.0.5、tag v2.0.5 也在，下次合并时两个条件依旧成立，
    于是永远 skip。实测 v2.0.5 之后正是这个状态。
    """
    assert "git tag --points-at HEAD" in WORKFLOW
    assert 'git rev-parse "v$cur"' not in WORKFLOW


def test_workflow_does_not_add_ignored_uv_lock():
    """uv.lock 被 .gitignore 忽略，git add 会直接失败并把发版判红。

    实测 v2.0.5 第一次发版就挂在这里：
    ``The following paths are ignored by one of your .gitignore files: uv.lock``
    """
    assert "git add -- pyproject.toml" in WORKFLOW
    assert "git add pyproject.toml uv.lock" not in WORKFLOW


def test_release_notes_step_survives_pipe_truncation():
    """生成 notes 不能因 SIGPIPE 把整次发版判成失败。

    ``git log | ... | head -40`` 里 head 读够就关管道，上游 sed/grep 收到
    SIGPIPE 后非 0 退出，``set -euo pipefail`` 下整个步骤失败（实测报
    ``grep: write error: Broken pipe`` / exit 2），而版本其实已经发出去了。
    """
    assert "continue-on-error: true" in WORKFLOW
    # 只看真正执行的那一行（注释里会提到 head -40 作为反面例子）
    assert "| sed -n '1,40p' |" in WORKFLOW
    assert not re.search(r"^\s*\|\s*head -40", WORKFLOW, re.MULTILINE)


# ---------------------------------------------------------------------------
# 一键更新（POST /ui/api/release/update）
#
# 这条链的关键不是「能不能更新」，而是「什么时候必须拒绝更新」。这个仓库的
# 主工作区经常有别人在途的改动，脏工作树上做 pull 是数据丢失级别的事故。
# ---------------------------------------------------------------------------
def test_start_update_refuses_dirty_tree(monkeypatch):
    """工作树脏必须拒绝，并把 git status 原文回给页面。

    只说「更新失败」等于让用户自己猜哪几个文件挡着；把
    ``git status --short`` 带回去，用户才知道该提交还是 stash。
    """
    monkeypatch.setattr(rel, "_run_git",
                        lambda *a: " M README.md\n?? scratch.py\n")
    ok, status, payload = rel.start_update()
    assert ok is False
    assert status == 409
    assert "README.md" in payload["message"]
    assert "scratch.py" in payload["message"]


def test_start_update_refuses_non_git_dir(monkeypatch, tmp_path):
    """不是 git 仓库时给明确原因，不是 500。"""
    monkeypatch.setattr(rel, "_REPO_ROOT", tmp_path)
    ok, status, payload = rel.start_update()
    assert ok is False
    assert status == 409
    assert "git" in payload["message"].lower()


def test_start_update_spawns_detached_process(monkeypatch, tmp_path):
    """更新进程必须脱离当前进程组。

    更新的最后一步会重启服务——也就 ``kill`` 掉调用它的这个进程。留在同一
    进程组里会被一起带走，更新做到一半就断。``start_new_session=True`` 是
    这条成立的前提，所以直接锁住它。
    """
    (tmp_path / ".git").mkdir()
    (tmp_path / "buddy").write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setattr(rel, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(rel, "_run_git", lambda *a: "")

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(rel.subprocess, "Popen", fake_popen)
    ok, status, payload = rel.start_update()
    assert ok is True and status == 202
    assert captured["start_new_session"] is True, (
        "没脱离进程组，重启会把更新进程一起杀掉")
    assert captured["cwd"] == str(tmp_path)
    # 传了 BUDDY_NO_OPEN_UI=1 —— 页面自己会刷新，不该再开新标签页
    assert captured["env"].get("BUDDY_NO_OPEN_UI") == "1"


def test_start_update_passes_no_open_ui_flag():
    """``BUDDY_NO_OPEN_UI`` 的名字要和 ``buddy`` 脚本里认的一致。

    两边对不上时没有任何报错，只是更新完又弹一个新标签页——静默的回归，
    只能靠测试锁。
    """
    script = (pathlib.Path(__file__).resolve().parents[1] / "buddy").read_text(
        encoding="utf-8")
    assert rel._NO_OPEN_UI_ENV == "BUDDY_NO_OPEN_UI"
    assert 'BUDDY_NO_OPEN_UI:-' in script, "buddy 脚本没认这个开关"
    # 开关要在真的调 opener 之前就返回
    fn_at = script.index("open_ui() {")
    guard_at = script.index("BUDDY_NO_OPEN_UI:-", fn_at)
    open_at = script.index('"$opener" "$URL"', fn_at) if '"$opener" "$URL"' in script else -1
    assert open_at == -1 or guard_at < open_at, "开关写在打开页面之后，等于没拦"


def test_start_update_blocks_double_trigger(monkeypatch):
    """冷却窗口内第二次请求必须被挡住。

    前端会禁用按钮，但**多开一个标签页**或直接 curl 挡不住。两个
    ``buddy update`` 并发跑会同时 ``uv sync``，可能写坏 venv——服务直接起不来。
    """
    monkeypatch.setattr(rel, "_update_started_at", 0.0)
    monkeypatch.setattr(rel, "_run_git", lambda *a: "")
    monkeypatch.setattr(rel, "_update_started_at", rel.time.time())
    ok, status, payload = rel.start_update()
    assert ok is False
    assert status == 429
    assert "秒后再试" in payload["message"]


def test_update_endpoint_status_codes(monkeypatch):
    """接口层状态码契约：202 已拉起 / 409 拒绝 / 429 重复。

    直接测 ``start_update()`` 不够——``release_api`` 把它包成 HTTPException
    时很容易把 202 写成 200（默认值），页面拿到 200 也照跑，但「已启动」
    与「已完成」就分不清了。
    """
    from fastapi.testclient import TestClient

    import buddy_proxy.web.ui.release_api  # noqa: F401 - 注册路由
    from buddy_proxy.core.state import app

    client = TestClient(app, client=("127.0.0.1", 51234))

    # 脏树 → 409，且原因原文回给页面
    monkeypatch.setattr(rel, "_run_git", lambda *a: " M a.py\n")
    monkeypatch.setattr(rel, "_update_started_at", 0.0)
    r = client.post("/ui/api/release/update")
    assert r.status_code == 409
    assert "a.py" in r.json()["detail"]["error"]["message"]

    # 干净树 → 202
    monkeypatch.setattr(rel, "_run_git", lambda *a: "")

    class _P:
        pid = 4321

    monkeypatch.setattr(rel.subprocess, "Popen", lambda argv, **kw: _P())
    r = client.post("/ui/api/release/update")
    assert r.status_code == 202
    assert r.json()["started"] is True

    # 立刻再来 → 429
    r = client.post("/ui/api/release/update")
    assert r.status_code == 429


def test_dirty_check_ignores_untracked_files(monkeypatch, tmp_path):
    """脏树判定必须排除未跟踪文件（``??``）。

    ``git pull --ff-only`` 不会被未跟踪文件挡住（除非远端新增同名文件，那时
    git 自己会拒绝并把原因写清楚）。把它们也算脏的代价是这个仓库常年挂着
    未跟踪的 scratch 目录，按钮会永远 409。这条锁住 ``--untracked-files=no``，
    改回去当场红。
    """
    (tmp_path / ".git").mkdir()
    (tmp_path / "buddy").write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setattr(rel, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(rel, "_update_started_at", 0.0)

    seen = []

    def fake_git(*args):
        seen.append(args)
        return ""

    monkeypatch.setattr(rel, "_run_git", fake_git)

    class _P:
        pid = 7

    monkeypatch.setattr(rel.subprocess, "Popen", lambda argv, **kw: _P())
    rel.start_update()

    status_calls = [a for a in seen if a and a[0] == "status"]
    assert status_calls, "没检查工作树状态就拉起更新了"
    assert any("--untracked-files=no" in a for a in status_calls), (
        "脏树判定又把未跟踪文件算进去了——这个仓库常年有 ?? 目录，按钮会一直 409")


def test_update_status_does_not_replace_foot_buttons():
    """更新进度必须写进独立的 `.rel-status`，不能替换 `.rel-foot`。

    `.rel-foot` 里装着「不再提示 / 更新」两个按钮。早先的实现直接
    `foot.innerHTML = ...`，按钮当场从 DOM 消失——超时后想恢复按钮都找不到
    它，加上 `if (RELEASE_UPDATING) return;` 就变成按钮永久失效、只能手动
    刷新页面。
    """
    assert 'class="rel-status"' in HTML
    assert "releaseSetStatus" in JS
    # 状态函数只能动 .rel-status
    fn_at = JS.index("function releaseSetStatus")
    body = JS[fn_at:JS.index("\n}", fn_at)]
    assert ".rel-status" in body
    assert "rel-foot" not in body, "又在改 .rel-foot 了，按钮会被一起干掉"


def test_update_timeout_restores_button():
    """超时必须把按钮恢复可用，否则用户没有重试入口。"""
    fn_at = JS.index("async function releaseWaitForRestart")
    body = JS[fn_at:JS.index("\nfunction releaseMuteCurrent", fn_at)]
    assert "RELEASE_UPDATING = false" in body, "超时后标志位没复位，按钮永久失效"
    assert "disabled = false" in body, "超时后按钮还禁用着"
    assert "重试" in body, "超时后没有给重试入口"


def test_update_polling_never_forces_git_fetch():
    """轮询里不能带 `refresh=1`。

    那会让服务端同步跑 `git fetch`（本机实测 4 秒左右），而这个接口是在
    `async def` 里直接调 subprocess——4 秒内整个事件循环被占住。轮询间隔
    1.5 秒比 fetch 还短，等于每轮都在堵，代理自己的请求也会跟着卡。
    """
    fn_at = JS.index("async function releaseWaitForRestart")
    body = JS[fn_at:JS.index("\nfunction releaseMuteCurrent", fn_at)]
    # 只看真的 `api(...)` 调用：注释里解释「为什么不能加 refresh=1」是正常的，
    # 直接 grep 全文会误伤自己写的说明。
    calls = re.findall(r"api\('([^']+)'", body)
    assert calls, "轮询里一个接口都没调？"
    assert not any("refresh=1" in c for c in calls), (
        f"轮询里又强制 fetch 了（{calls}），会阻塞事件循环")
