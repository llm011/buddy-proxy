"""DuMate 脱离桌面端独立运行（``dumate/standalone.py``）单测。

锁定 2026-11-01 用户报障的修复：关掉 DuMate.app 后代理 dumate 通道不该整体
挂掉——本机装了 App 且已登录时，代理自己拉起 ``dumate-main-server``。

关键行为（全部实证）：

1. **登录标识读取**：从 ``auth.json`` 按 ``activeProfileId`` 命中 profile，取
   ``activeProviderType`` / ``bceUserId`` / ``bceAccountId``；缺文件/缺字段时
   返回空串，不抛。
2. **探活必须带 inapp key**：服务端开 ``InappKeyAuth`` 后 ``/health`` 也鉴权，
   不带 header 一律 401——早先不带 key 探活被误判成「未就绪」，spawn 15s 超时。
3. **凭证缺失不抛异常**：spawn 返回 ``ok=False`` + 可读 reason。
4. **已有实例则复用**：``ensure_running`` 在 discover 命中时返回 None，不重复拉。
5. **关停清理**：``shutdown_standalone`` 终止本进程拉起的子进程。

全部 mock：不真起进程、不读真实 auth.json。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from buddy_proxy.dumate import standalone as st


# --- 安装/支持判定 -----------------------------------------------------------


def test_app_resources_root_none_when_not_installed(tmp_path):
    """未装 App：找不到资源根，返回 None。"""
    with mock.patch.object(st, "_APP_CANDIDATES", (str(tmp_path / "Nope.app"),)):
        assert st.app_resources_root() is None
        assert st.standalone_supported() is False


def test_app_resources_root_found_when_binary_present(tmp_path):
    res = tmp_path / "DuMate.app" / "Contents" / "Resources"
    bin_path = res.joinpath(*st._BIN_REL)
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("#!/bin/sh\n")
    (res.joinpath(*st._CONFIG_REL)).parent.mkdir(parents=True)
    (res.joinpath(*st._CONFIG_REL)).write_text("web: {}\n")
    with mock.patch.object(st, "_APP_CANDIDATES", (str(tmp_path / "DuMate.app"),)):
        assert st.app_resources_root() == res
        assert st.standalone_supported() is True


# --- 开关 / 常驻位 -----------------------------------------------------------


def test_is_enabled_default_on():
    with mock.patch.dict("os.environ", {}, clear=True):
        assert st.is_enabled() is True
        assert st.is_persistent() is False


@pytest.mark.parametrize("val", ["off", "0", "false", "FALSE", "no", " OFF "])
def test_is_enabled_can_be_disabled(val):
    with mock.patch.dict("os.environ", {"BUDDY_DUMATE_STANDALONE": val}):
        assert st.is_enabled() is False


def test_is_persistent():
    with mock.patch.dict("os.environ", {"BUDDY_DUMATE_STANDALONE": "persist"}):
        assert st.is_persistent() is True
        assert st.is_enabled() is True


# --- 登录标识读取 ------------------------------------------------------------


def _write_auth(tmp_path: Path, payload: dict) -> Path:
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(payload), encoding="utf-8")
    return auth


def test_read_login_context_picks_active_profile(tmp_path):
    """按 activeProfileId 命中 profile（多 profile 时不能拿错账号）。"""
    from buddy_proxy.dumate import cookies as dumate_cookies

    _write_auth(tmp_path, {
        "activeProviderType": "bce",
        "activeProfileId": "p2",
        "accountProfiles": [
            {"profileId": "p1", "bceUserId": "u1", "bceAccountId": "a1"},
            {"profileId": "p2", "bceUserId": "u2", "bceAccountId": "a2"},
        ],
    })
    with mock.patch.object(dumate_cookies, "_user_data_dir", return_value=str(tmp_path)):
        ctx = st.read_login_context()
    assert ctx == {"login_mode": "bce", "user_id": "u2", "account_id": "a2"}


def test_read_login_context_enterprise_mode(tmp_path):
    """企业（飞书）账号：activeProviderType=enterprise 也要原样读出。"""
    from buddy_proxy.dumate import cookies as dumate_cookies

    _write_auth(tmp_path, {
        "activeProviderType": "enterprise",
        "activeProfileId": "p1",
        "accountProfiles": [{"profileId": "p1", "bceUserId": "u", "bceAccountId": "a"}],
    })
    with mock.patch.object(dumate_cookies, "_user_data_dir", return_value=str(tmp_path)):
        ctx = st.read_login_context()
    assert ctx["login_mode"] == "enterprise"


def test_read_login_context_empty_when_missing(tmp_path):
    from buddy_proxy.dumate import cookies as dumate_cookies

    with mock.patch.object(dumate_cookies, "_user_data_dir", return_value=str(tmp_path)):
        ctx = st.read_login_context()
    assert ctx == {"login_mode": "", "user_id": "", "account_id": ""}


# --- spawn：探活必须带 inapp key --------------------------------------------


def test_health_probe_sends_inapp_key(tmp_path):
    """``_probe_health`` 必须把 inapp key 放进请求头。

    服务端开 ``InappKeyAuth`` 后 ``/health`` 也鉴权；不带 header 返回 401，
    探活循环永远判「未就绪」（实测踩坑）。这里断言 header 真的带上了。
    """
    captured = {}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = req.headers if hasattr(req, "headers") else {}
        captured["url"] = req.full_url
        return _Resp()

    with mock.patch.object(st.urllib.request, "urlopen", side_effect=_fake_urlopen):
        ok = st._probe_health(52414, "deadbeef")

    assert ok is True
    assert captured["url"].endswith("/health")
    # urllib 会把 header 名规范化成 X-dumate-inapp-key；取值大小写不敏感
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers.get("x-dumate-inapp-key") == "deadbeef"


# --- spawn：凭证缺失不抛 -----------------------------------------------------


def test_spawn_no_login_returns_not_ok(tmp_path):
    res_root = tmp_path / "DuMate.app" / "Contents" / "Resources"
    res_root.joinpath(*st._BIN_REL).parent.mkdir(parents=True)
    res_root.joinpath(*st._BIN_REL).write_text("x")
    res_root.joinpath(*st._CONFIG_REL).parent.mkdir(parents=True)
    res_root.joinpath(*st._CONFIG_REL).write_text("x")
    res = st.spawn(res_root, login={"login_mode": "", "user_id": "", "account_id": ""})
    assert res["ok"] is False
    assert "登录态" in res["reason"]


def test_spawn_missing_binary_returns_not_ok(tmp_path):
    res = st.spawn(tmp_path, login={"login_mode": "bce", "user_id": "u", "account_id": "a"})
    assert res["ok"] is False
    assert res["reason"]


# --- ensure_running：已有实例则复用 ------------------------------------------


def test_ensure_running_returns_none_when_instance_exists():
    """discover 命中（App 或任何在跑的实例）→ 复用，返回 None，不 spawn。"""
    from buddy_proxy.dumate import discovery

    with mock.patch.object(discovery, "discover", return_value=mock.Mock()), \
         mock.patch.object(st, "spawn") as sp:
        assert st.ensure_running() is None
    sp.assert_not_called()


def test_ensure_running_spawns_when_none_running():
    """discover 返回 None → 拉一个。"""
    from buddy_proxy.dumate import discovery

    sentinel = {"ok": True, "port": 1, "pid": 2}
    with mock.patch.object(discovery, "discover", return_value=None), \
         mock.patch.object(st, "spawn", return_value=sentinel) as sp:
        assert st.ensure_running() == sentinel
    sp.assert_called_once()


# --- spawn 成功路径 + 关停清理 -----------------------------------------------


def test_spawn_success_records_child_and_shutdown_kills_it(tmp_path):
    """spawn 成功后关停会 terminate 子进程（会话内模式不留孤儿）。"""
    res_root = tmp_path / "DuMate.app" / "Contents" / "Resources"
    res_root.joinpath(*st._BIN_REL).parent.mkdir(parents=True)
    res_root.joinpath(*st._BIN_REL).write_text("x")
    res_root.joinpath(*st._CONFIG_REL).parent.mkdir(parents=True)
    res_root.joinpath(*st._CONFIG_REL).write_text("x")

    proc = mock.Mock()
    proc.poll.return_value = None       # 一直存活
    proc.pid = 4242
    proc.returncode = None

    with mock.patch.object(st.subprocess, "Popen", return_value=proc), \
         mock.patch.object(st, "_probe_health", return_value=True), \
         mock.patch.dict("os.environ", {}, clear=True):  # 非 persist
        res = st.spawn(res_root, login={"login_mode": "bce", "user_id": "u", "account_id": "a"})

    assert res == {"ok": True, "port": mock.ANY, "pid": 4242}
    assert st._inapp_key and len(st._inapp_key) == 64

    st.shutdown_standalone()
    proc.terminate.assert_called_once()
    assert st._child is None and st._inapp_key is None


def test_spawn_exit_immediately_returns_not_ok(tmp_path):
    """子进程启动即退出（poll 返回码）→ ok=False，不挂死等满超时。"""
    res_root = tmp_path / "DuMate.app" / "Contents" / "Resources"
    res_root.joinpath(*st._BIN_REL).parent.mkdir(parents=True)
    res_root.joinpath(*st._BIN_REL).write_text("x")
    res_root.joinpath(*st._CONFIG_REL).parent.mkdir(parents=True)
    res_root.joinpath(*st._CONFIG_REL).write_text("x")

    proc = mock.Mock()
    proc.poll.return_value = 3
    proc.returncode = 3

    with mock.patch.object(st.subprocess, "Popen", return_value=proc), \
         mock.patch.object(st, "_probe_health", return_value=False):
        res = st.spawn(res_root, login={"login_mode": "bce", "user_id": "u", "account_id": "a"})

    assert res["ok"] is False
    assert "exit=3" in res["reason"]


def test_persistent_mode_does_not_kill_on_shutdown(tmp_path):
    """persist 模式：关停不杀子进程（用户要它常驻）。"""
    res_root = tmp_path / "DuMate.app" / "Contents" / "Resources"
    res_root.joinpath(*st._BIN_REL).parent.mkdir(parents=True)
    res_root.joinpath(*st._BIN_REL).write_text("x")
    res_root.joinpath(*st._CONFIG_REL).parent.mkdir(parents=True)
    res_root.joinpath(*st._CONFIG_REL).write_text("x")

    proc = mock.Mock()
    proc.poll.return_value = None
    proc.pid = 99
    proc.returncode = None

    state_dir = tmp_path / "state"

    def _fake_dir():
        return state_dir / "dumate-standalone"

    with mock.patch.object(st.subprocess, "Popen", return_value=proc), \
         mock.patch.object(st, "_probe_health", return_value=True), \
         mock.patch.object(st, "_pid_dir", side_effect=_fake_dir), \
         mock.patch.dict("os.environ", {"BUDDY_DUMATE_STANDALONE": "persist"}):
        res = st.spawn(res_root, login={"login_mode": "bce", "user_id": "u", "account_id": "a"})
        assert res["ok"] is True
        # 落了登记文件（文件名=owner pid，标 persistent）
        rec = _fake_dir() / f"{os.getpid()}.json"
        assert rec.is_file()
        data = json.loads(rec.read_text(encoding="utf-8"))
        assert data["pid"] == 99 and data["persistent"] is True
        st.shutdown_standalone()

    proc.terminate.assert_not_called()
    # persist 不清登记文件（留给用户/launchd；reap_orphans 也会跳过它）
    assert (_fake_dir() / f"{os.getpid()}.json").is_file()


# --- 状态提示与自动拉起一致 ---------------------------------------------------


def test_describe_state_hint_says_auto_spawn_when_capable(tmp_path):
    """装了 App 但没在跑 + 可独立拉起 + 已登录 → 提示「会自动拉起」，不是「请先打开」。

    用户 2026-11-01：不该必须开着桌面端。若 hint 仍写「请先打开百度搭子桌面端」，
    面板就把一个无需的动作当成前置，与 ``forward`` 的实际行为相反。
    """
    from buddy_proxy.dumate import cookies as dumate_cookies
    from buddy_proxy.dumate import discovery

    res_root = tmp_path / "DuMate.app" / "Contents" / "Resources"
    res_root.joinpath(*st._BIN_REL).parent.mkdir(parents=True)
    res_root.joinpath(*st._BIN_REL).write_text("x")
    res_root.joinpath(*st._CONFIG_REL).parent.mkdir(parents=True)
    res_root.joinpath(*st._CONFIG_REL).write_text("x")
    _write_auth(tmp_path, {
        "activeProviderType": "bce",
        "activeProfileId": "p1",
        "accountProfiles": [{"profileId": "p1", "bceUserId": "u", "bceAccountId": "a"}],
    })

    with mock.patch.object(st, "_APP_CANDIDATES", (str(tmp_path / "DuMate.app"),)), \
         mock.patch.object(dumate_cookies, "_user_data_dir", return_value=str(tmp_path)), \
         mock.patch.object(discovery, "discover", return_value=None), \
         mock.patch.object(discovery, "is_app_installed", return_value=True), \
         mock.patch.dict("os.environ", {}, clear=True):
        info = discovery.describe_state()

    assert info["ready"] is False and info["installed"] is True
    assert "自动拉起" in info["hint"], f"应提示会自动拉起: {info['hint']}"
    assert "请先打开" not in info["hint"], "不该再要求用户手动开桌面端"


def test_describe_state_hint_falls_back_when_standalone_disabled(tmp_path):
    """关掉兜底（BUDDY_DUMATE_STANDALONE=off）时，提示回退成「请先打开」。"""
    from buddy_proxy.dumate import cookies as dumate_cookies
    from buddy_proxy.dumate import discovery

    res_root = tmp_path / "DuMate.app" / "Contents" / "Resources"
    res_root.joinpath(*st._BIN_REL).parent.mkdir(parents=True)
    res_root.joinpath(*st._BIN_REL).write_text("x")
    res_root.joinpath(*st._CONFIG_REL).parent.mkdir(parents=True)
    res_root.joinpath(*st._CONFIG_REL).write_text("x")
    _write_auth(tmp_path, {
        "activeProviderType": "bce",
        "activeProfileId": "p1",
        "accountProfiles": [{"profileId": "p1", "bceUserId": "u", "bceAccountId": "a"}],
    })

    with mock.patch.object(st, "_APP_CANDIDATES", (str(tmp_path / "DuMate.app"),)), \
         mock.patch.object(dumate_cookies, "_user_data_dir", return_value=str(tmp_path)), \
         mock.patch.object(discovery, "discover", return_value=None), \
         mock.patch.object(discovery, "is_app_installed", return_value=True), \
         mock.patch.dict("os.environ", {"BUDDY_DUMATE_STANDALONE": "off"}, clear=True):
        info = discovery.describe_state()

    assert "请先打开" in info["hint"], f"兜底关闭时应回退旧提示: {info['hint']}"


# --- 孤儿回收 -----------------------------------------------------------------


def _write_record(d, *, owner: int, child: int, persistent: bool = False) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{owner}.json"
    p.write_text(json.dumps({
        "pid": child, "port": 1234, "persistent": persistent, "owner": owner,
    }), encoding="utf-8")
    return p


def test_reap_orphans_kills_when_owner_dead(tmp_path):
    """owner 已死 + 非 persist + 子进程确实还是 dumate-main-server → 回收。"""
    d = tmp_path / "dumate-standalone"
    _write_record(d, owner=0, child=4242)

    with mock.patch.object(st, "_pid_dir", return_value=d), \
         mock.patch.object(st, "_owner_alive", return_value=False), \
         mock.patch.object(st, "_is_main_server", return_value=True), \
         mock.patch.object(st, "_terminate_pid") as term:
        n = st.reap_orphans()

    assert n == 1
    term.assert_called_once_with(4242)
    # 登记文件被清（回收完不留残条）
    assert not list(d.glob("*.json"))


def test_reap_orphans_keeps_when_owner_alive(tmp_path):
    """owner 还活着（并存代理实例）→ 不回收它拉起的实例。"""
    d = tmp_path / "dumate-standalone"
    _write_record(d, owner=777, child=4242)

    with mock.patch.object(st, "_pid_dir", return_value=d), \
         mock.patch.object(st, "_owner_alive", return_value=True), \
         mock.patch.object(st, "_is_main_server", return_value=True), \
         mock.patch.object(st, "_terminate_pid") as term:
        n = st.reap_orphans()

    assert n == 0
    term.assert_not_called()


def test_reap_orphans_skips_persistent(tmp_path):
    """persist 条目：常驻语义，归用户/launchd 管，一律跳过。"""
    d = tmp_path / "dumate-standalone"
    _write_record(d, owner=0, child=4242, persistent=True)

    with mock.patch.object(st, "_pid_dir", return_value=d), \
         mock.patch.object(st, "_owner_alive", return_value=False), \
         mock.patch.object(st, "_is_main_server", return_value=True), \
         mock.patch.object(st, "_terminate_pid") as term:
        n = st.reap_orphans()

    assert n == 0
    term.assert_not_called()
    assert (d / "0.json").is_file()  # persist 的登记文件保留


def test_reap_orphans_ignores_pid_reuse(tmp_path):
    """登记里的 pid 已不再是 dumate-main-server（pid 复用）→ 不杀，别误伤无关进程。"""
    d = tmp_path / "dumate-standalone"
    _write_record(d, owner=0, child=4242)

    with mock.patch.object(st, "_pid_dir", return_value=d), \
         mock.patch.object(st, "_owner_alive", return_value=False), \
         mock.patch.object(st, "_is_main_server", return_value=False), \
         mock.patch.object(st, "_terminate_pid") as term:
        n = st.reap_orphans()

    assert n == 0
    term.assert_not_called()


def test_reap_orphans_noop_when_dir_missing(tmp_path):
    with mock.patch.object(st, "_pid_dir", return_value=tmp_path / "nope"):
        assert st.reap_orphans() == 0