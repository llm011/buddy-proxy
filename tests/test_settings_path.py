"""settings.json 必须落在状态目录里，隔离运行才真的隔离。

回归背景（2026-11-06）：本地用 ``BUDDY_PROXY_STATE_DIR`` 起第二个实例做冒烟验证，
账号、指标、日志都进了新目录，只有 ``settings.json`` 还读写 ``~/.buddy-proxy``——
隔离实例改设置会改到主配置；HOME 只读（沙箱/CI）时管理页「改默认模型」直接 500。
``paths.state_file("settings.json")`` 的约定本来就是状态目录，这里是唯一漏网的
状态文件，故补上迁移（copy 而非 move）与优先级测试。
"""

from __future__ import annotations

import json
import stat

from buddy_proxy.core import paths
from buddy_proxy.core import settings as settings_mod


def _no_explicit_settings(monkeypatch):
    monkeypatch.delenv("BUDDY_PROXY_SETTINGS", raising=False)


def test_settings_follow_state_dir(monkeypatch, tmp_path):
    _no_explicit_settings(monkeypatch)
    state = tmp_path / "state"
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(state))

    path = settings_mod.settings_path()
    assert path == state / "settings.json"
    assert path == paths.state_file("settings.json")

    settings_mod.save_settings({"default_model": "glm-5.3"})
    assert json.loads(path.read_text(encoding="utf-8"))["default_model"] == "glm-5.3"


def test_explicit_settings_env_wins_over_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(tmp_path / "explicit.json"))
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(tmp_path / "state"))
    assert settings_mod.settings_path() == tmp_path / "explicit.json"


def test_legacy_settings_migrate_into_state_dir(monkeypatch, tmp_path):
    """只设状态目录的老用户：升级后第一次访问把旧 settings.json 复制过来。"""
    _no_explicit_settings(monkeypatch)
    home = tmp_path / "home"
    legacy = home / ".buddy-proxy" / "settings.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"default_model": "legacy/model"}', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(tmp_path / "state"))

    migrated = settings_mod.settings_path()
    assert migrated == tmp_path / "state" / "settings.json"
    assert json.loads(migrated.read_text(encoding="utf-8"))["default_model"] == "legacy/model"
    # 凭证目录里只留 0600；旧文件保留作回滚（copy 而非 move）
    assert stat.S_IMODE(migrated.stat().st_mode) == 0o600
    assert legacy.exists()


def test_without_state_dir_settings_stay_in_state_root(monkeypatch, tmp_path):
    """没设状态目录时行为不变：仍是 ``<state_dir>/settings.json``。"""
    _no_explicit_settings(monkeypatch)
    monkeypatch.delenv("BUDDY_PROXY_STATE_DIR", raising=False)
    assert settings_mod.settings_path() == paths.state_dir() / "settings.json"
