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
