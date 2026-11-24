"""Tests for ZCode Coding Plan reset ticket client."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from buddy_proxy.providers.zcode_reset import (
    fetch_reset_status,
    parse_reset_status_payload,
    resolve_zcode_reset_credentials,
    use_reset_ticket,
)


def test_parse_reset_status_filters_expired_tickets():
    now_ms = 2_000_000_000_000
    payload = {
        "code": 0,
        "msg": "success",
        "data": {
            "available_five_hour_resets": [
                {"expire_at": now_ms - 1000},  # expired
                {"expire_at": now_ms + 50000},
            ],
            "available_week_resets": [
                {"expire_at": now_ms + 90000},
                {"expire_at": now_ms + 20000},
            ],
            "latest_five_hour_reset_history": {"used_at": 1_700_000_000_000},
            "latest_week_reset_history": None,
            "has_unread_history": True,
            "server_time": 1_795_545_018,
        },
    }
    res = parse_reset_status_payload(payload, now_ms=now_ms)
    assert res["supported"] is True
    assert res["five_hour_count"] == 1
    assert res["five_hour_earliest_expire_at"] == now_ms + 50000
    assert res["week_count"] == 2
    assert res["week_earliest_expire_at"] == now_ms + 20000
    assert res["total_count"] == 3
    assert res["latest_five_hour_used_at"] == 1_700_000_000_000
    assert res["latest_week_used_at"] is None
    assert res["has_unread_history"] is True


def test_resolve_credentials_prefers_cli_jwt_and_v2_bigmodel_token(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ZCODE_JWT_TOKEN", raising=False)
    monkeypatch.delenv("ZCODE_BIGMODEL_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    cli_dir = tmp_path / ".zcode" / "cli"
    v2_dir = tmp_path / ".zcode" / "v2"
    cli_dir.mkdir(parents=True)
    v2_dir.mkdir(parents=True)

    (cli_dir / "config.json").write_text(
        json.dumps({"provider": {"bigmodel-start-plan": {"kind": "anthropic", "options": {"apiKey": "cli-jwt"}}}}),
        encoding="utf-8",
    )
    (v2_dir / "credentials.json").write_text(
        json.dumps({"zcodejwttoken": "old-v2-jwt", "oauth:bigmodel:access_token": "bm-token"}),
        encoding="utf-8",
    )

    zcode_jwt, bm_tok = resolve_zcode_reset_credentials()
    assert zcode_jwt == "cli-jwt"
    assert bm_tok == "bm-token"


def test_fetch_reset_status_missing_credentials():
    res = asyncio.run(fetch_reset_status(zcode_jwt="", bigmodel_token=""))
    assert res["supported"] is False
    assert res["reason"] == "missing_credentials"
    assert res["total_count"] == 0


def test_use_reset_ticket_validates_type_and_posts_payload():
    async def _run():
        with pytest.raises(ValueError, match="不支持的重置券类型"):
            await use_reset_ticket("MONTHLY", zcode_jwt="j", bigmodel_token="b")

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"code": 0, "msg": "success", "data": {"used": True}})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await use_reset_ticket(
                "WEEK",
                idempotency_key="idem-123",
                zcode_jwt="jwt-abc",
                bigmodel_token="bm-xyz",
                client=client,
            )

        assert res["ok"] is True
        assert res["reset_type"] == "WEEK"
        assert res["idempotency_key"] == "idem-123"
        assert captured["url"] == "https://zcode.z.ai/api/v1/coding-plan/reset/use"
        assert captured["headers"]["authorization"] == "Bearer jwt-abc"
        assert captured["headers"]["x-bigmodel-authorization"] == "bm-xyz"
        assert captured["headers"]["bigmodel-target-type"] == "PERSONAL"
        assert captured["body"] == {"idempotency_key": "idem-123", "reset_type": "WEEK"}

    asyncio.run(_run())


def test_use_reset_ticket_raises_on_api_error():
    async def _run():
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"code": 3101, "msg": "coding plan is required"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(RuntimeError, match="code=3101"):
                await use_reset_ticket(
                    "FIVE_HOUR",
                    zcode_jwt="jwt-abc",
                    bigmodel_token="bm-xyz",
                    client=client,
                )

    asyncio.run(_run())


def test_glm_provider_quota_does_not_attach_zcode_reset_tickets():
    from buddy_proxy.providers.glm import GlmProvider

    prov = GlmProvider(api_key="dummy")

    class FakeResp:
        status_code = 200

        def json(self):
            return {"success": True, "data": {"limits": [], "level": "pro"}}

    with (
        patch("buddy_proxy.providers.zcode.httpx.Client") as mock_client,
        patch("buddy_proxy.providers.zcode_reset.fetch_reset_status_sync") as mock_reset,
    ):
        mock_client.return_value.__enter__.return_value.get.return_value = FakeResp()
        res = prov.quota()

    assert res is not None
    assert "reset_tickets" not in res
    mock_reset.assert_not_called()
