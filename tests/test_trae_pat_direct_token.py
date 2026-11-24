"""traepat 直传 cloud_ide_token 模式测试（跳过 bearer 两步交换）。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import HTTPException

from buddy_proxy.trae import pat
from buddy_proxy.trae.pat.config import _load_profiles
from buddy_proxy.trae.pat.credentials import _get_profile_credentials


def test_direct_cloud_ide_token_profile_skips_exchange(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    token_file = tmp_path / "trae_pat_token.json"
    monkeypatch.setenv("TRAE_PAT_TOKEN_FILE", str(token_file))
    monkeypatch.setenv(
        "TRAE_PAT_BEARER_PROFILES",
        json.dumps([
            {
                "id": "desktop-cn",
                "cloud_ide_token": "jwt-desktop-123",
                "uid": "lingyibin.jason",
                "expires_at": 1999999999,
            }
        ]),
    )

    def _forbid_exchange(_bearer: str) -> tuple[str, str, float]:
        raise AssertionError("直传 cloud_ide_token 模式不应调用 _exchange")

    monkeypatch.setattr(pat, "_exchange", _forbid_exchange)

    profiles = _load_profiles()
    assert len(profiles) == 1
    assert profiles[0].id == "desktop-cn"
    assert profiles[0].bearer == ""
    assert profiles[0].cloud_ide_token == "jwt-desktop-123"

    creds = _get_profile_credentials(profiles[0])
    assert creds.token == "jwt-desktop-123"
    assert creds.uid == "lingyibin.jason"

    # 被 401 拒绝后，直传模式无法重签，必须直接报 401 让上层换号
    with pytest.raises(HTTPException) as exc_info:
        _get_profile_credentials(
            profiles[0], force_refresh=True, rejected_token="jwt-desktop-123"
        )
    assert exc_info.value.status_code == 401


def test_import_from_ide_creates_and_updates_profile(monkeypatch, tmp_path):
    from buddy_proxy.trae import auth_storage
    from buddy_proxy.trae.pat.config import _PatConfigError, _load_profiles, import_from_ide

    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\nTRAE_PAT_BEARER_PROFILES='[{\"id\":\"p1\",\"bearer\":\"b1\"}]'\n", encoding="utf-8")
    monkeypatch.setenv("TRAE_PAT_BEARER_PROFILES", '[{"id":"p1","bearer":"b1"}]')

    monkeypatch.setattr(auth_storage, "find_auth_data", lambda ed: {"ok": ed} if ed == "cn" else None)
    monkeypatch.setattr(
        auth_storage,
        "_extract_auth_fields",
        lambda _d: {
            "token": "jwt-from-ide-1",
            "userId": "lingyibin.jason",
            "expiredAt": "2027-11-02T11:41:14+08:00",
        },
    )

    res1 = import_from_ide(env_path=env_file)
    assert res1["id"] == "desktop-cn"
    assert res1["updated"] is False
    assert res1["total"] == 2
    profiles = _load_profiles()
    assert [p.id for p in profiles] == ["p1", "desktop-cn"]
    assert profiles[1].cloud_ide_token == "jwt-from-ide-1"
    assert "jwt-from-ide-1" in env_file.read_text(encoding="utf-8")

    # 再次导入应原地更新而非追加第 3 个
    monkeypatch.setattr(
        auth_storage,
        "_extract_auth_fields",
        lambda _d: {
            "token": "jwt-from-ide-2",
            "userId": "lingyibin.jason",
            "expiredAt": "2027-12-01T00:00:00+08:00",
        },
    )
    res2 = import_from_ide(env_path=env_file)
    assert res2["updated"] is True
    assert res2["total"] == 2
    assert _load_profiles()[1].cloud_ide_token == "jwt-from-ide-2"

    # 无登录态时应明确报错
    monkeypatch.setattr(auth_storage, "find_auth_data", lambda _ed: None)
    with pytest.raises(_PatConfigError):
        import_from_ide(env_path=env_file)
