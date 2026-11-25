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
    monkeypatch.setenv("RUNNINGHUB_WORKFLOWS_PATH", str(tmp_path / "runninghub_workflows.json"))
    return p


# ---------------------------------------------------------------------------
# 计费类型：apiCallDemo 不给字段，只能从上游运行时错误里学
# ---------------------------------------------------------------------------

def test_classify_billing_error_maps_upstream_codes():
    """416 / NOT_ENOUGH_BALANCE = 钱包余额；NOT_ENOUGH_COINS = 金币；其余判不出。

    2026-11-25 实报：工作流标「RH 金币 / 余额」（未知），账号金币 994、钱包 null，
    跑起来上游回 NOT_ENOUGH_BALANCE。官方错误码表里 416 是
    TASK_CREATE_FAILED_BY_NOT_ENOUGH_WALLET——「钱包」不是「金币」。
    """
    from buddy_proxy.runninghub.client import classify_billing_error

    assert classify_billing_error(416, "") == "钱包余额"
    assert classify_billing_error(None, "RunningHub 提交任务失败: NOT_ENOUGH_BALANCE") == "钱包余额"
    assert classify_billing_error("416", "TASK_CREATE_FAILED_BY_NOT_ENOUGH_WALLET") == "钱包余额"
    assert classify_billing_error(None, "NOT_ENOUGH_COINS") == "RH 金币"
    assert classify_billing_error(500, "some other error") is None
    assert classify_billing_error(None, "") is None


def _wf_store_with(billing: str) -> dict:
    return store.upsert_workflow({
        "webappId": "2046794551444119554", "name": "Omnipotent G",
        "description": "", "billingType": billing, "nodeInfoList": [],
    })


def test_run_learns_wallet_billing_from_upstream_reject(monkeypatch: pytest.MonkeyPatch):
    """标「未知」的工作流被上游以余额不足拒掉 → 改标「钱包余额」、402、带回最新列表。

    这是计费类型**唯一**可靠的来源：apiCallDemo 没有计费字段，导入时只能标未知；
    真跑一次被拒才知道。学完写回本地，下次预检能在打上游前拦住。
    """
    from unittest.mock import AsyncMock

    from buddy_proxy import __main__ as m
    from buddy_proxy.runninghub import client as rh_client
    from fastapi.testclient import TestClient

    store.upsert_account("rh-secret-token-123456", label="t")
    _wf_store_with("RH 金币 / 余额")

    submit = AsyncMock(side_effect=rh_client.RunningHubError(
        "RunningHub 提交任务失败: NOT_ENOUGH_BALANCE", status_code=400, code=7))
    monkeypatch.setattr(rh_client, "fetch_account_status", AsyncMock(return_value={
        # 实测形态：金币有、钱包是 null（不是 "0"）
        "remainCoins": "994", "remainMoney": None, "apiType": "SHARED",
    }))
    monkeypatch.setattr(rh_client, "submit_task", submit)
    monkeypatch.setenv("BUDDY_PROXY_ADMIN_OPEN", "1")

    r = TestClient(m.app).post("/ui/api/runninghub/workflows/run", json={
        "webapp_id": "2046794551444119554", "nodeInfoList": []})

    assert r.status_code == 402, r.text
    msg = r.json()["detail"]["error"]["message"]
    assert "钱包余额" in msg and "remainMoney" in msg
    # 本地标签已改写
    learned = store.list_workflows()[0]["billingType"]
    assert learned == "钱包余额"
    # 错误体顺带带回最新列表，前端据此刷新卡片
    assert r.json()["detail"]["workflows"][0]["billingType"] == "钱包余额"


def test_run_preflight_blocks_wallet_workflow_without_hitting_upstream(monkeypatch: pytest.MonkeyPatch):
    """已标「钱包余额」+ 钱包为 null/0 → 预检 402，**一次上游都不打**。"""
    from unittest.mock import AsyncMock

    from buddy_proxy import __main__ as m
    from buddy_proxy.runninghub import client as rh_client
    from fastapi.testclient import TestClient

    store.upsert_account("rh-secret-token-123456", label="t")
    _wf_store_with("钱包余额")

    submit = AsyncMock(return_value={"taskId": "x"})
    monkeypatch.setattr(rh_client, "fetch_account_status", AsyncMock(return_value={
        "remainCoins": "994", "remainMoney": None,
    }))
    monkeypatch.setattr(rh_client, "submit_task", submit)
    monkeypatch.setenv("BUDDY_PROXY_ADMIN_OPEN", "1")

    r = TestClient(m.app).post("/ui/api/runninghub/workflows/run", json={
        "webapp_id": "2046794551444119554", "nodeInfoList": []})

    assert r.status_code == 402
    assert "金币 994 不能用于此工作流" in r.json()["detail"]["error"]["message"]
    submit.assert_not_called()


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
