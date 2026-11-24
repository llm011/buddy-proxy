"""RunningHub 渠道单元测试。"""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from buddy_proxy.runninghub import store
from buddy_proxy.runninghub.provider import RunningHubProvider


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "runninghub_accounts.json"
    monkeypatch.setenv("RUNNINGHUB_ACCOUNTS_PATH", str(p))
    return p


def test_store_crud_and_permissions(isolated_store: Path):
    acc = store.upsert_account(
        "rh-secret-token-123456",
        label="主账号",
        account_info={"remainCoins": "500", "currency": "CNY"},
    )
    assert acc["label"] == "主账号"
    assert "api_key" not in acc
    assert acc["masked_key"] == "rh-s...3456"
    assert acc["account_info"]["remainCoins"] == "500"

    mode = stat.S_IMODE(isolated_store.stat().st_mode)
    assert mode == 0o600

    assert store.rename_account(acc["account_id"], "备用号") is True
    listed = store.list_accounts()
    assert len(listed) == 1
    assert listed[0]["label"] == "备用号"

    assert store.delete_account(acc["account_id"]) is True
    assert store.list_accounts() == []


def test_provider_non_stream_image(isolated_store: Path):
    store.upsert_account("rh-secret-token-123456", label="test")
    provider = RunningHubProvider()

    with (
        patch("buddy_proxy.runninghub.provider.submit_task", new=AsyncMock(return_value={"taskId": "task-1"})),
        patch(
            "buddy_proxy.runninghub.provider.query_task",
            new=AsyncMock(
                return_value={
                    "status": "SUCCESS",
                    "results": [{"url": "https://cdn.example.com/out.png", "outputType": "image"}],
                }
            ),
        ),
    ):
        resp = asyncio.run(
            provider.forward(
                {
                    "model": "runninghub/text-to-image",
                    "messages": [{"role": "user", "content": "一只在月球上的猫"}],
                    "stream": False,
                },
                protocol="openai",
            )
        )

    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert "![generated](https://cdn.example.com/out.png)" in body["choices"][0]["message"]["content"]


def test_provider_stream_video(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RUNNINGHUB_POLL_INTERVAL", "0.01")
    store.upsert_account("rh-secret-token-123456", label="test")
    provider = RunningHubProvider()

    query_mock = AsyncMock(
        side_effect=[
            {"status": "RUNNING"},
            {
                "status": "SUCCESS",
                "results": [{"url": "https://cdn.example.com/out.mp4", "outputType": "video"}],
            },
        ]
    )

    async def _collect() -> str:
        resp = await provider.forward(
            {
                "model": "runninghub/text-to-video",
                "messages": [{"role": "user", "content": "海浪拍打沙滩"}],
                "stream": True,
            },
            protocol="openai",
        )
        chunks = [chunk async for chunk in resp.body_iterator]
        return "".join(chunks)

    with (
        patch("buddy_proxy.runninghub.provider.submit_task", new=AsyncMock(return_value={"taskId": "task-2"})),
        patch("buddy_proxy.runninghub.provider.query_task", new=query_mock),
    ):
        joined = asyncio.run(_collect())

    assert "[生成视频](https://cdn.example.com/out.mp4)" in joined
    assert "data: [DONE]" in joined
