"""RunningHub 渠道单元测试。"""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from unittest.mock import ANY, AsyncMock, patch

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
    # 605 是 ai-app/run 实测回的（官方码表没列），812 是企业版余额不足
    assert classify_billing_error(605, "NOT_ENOUGH_BALANCE") == "钱包余额"
    assert classify_billing_error("605", "") == "钱包余额"
    assert classify_billing_error(812, "CORPAPIKEY_INSUFFICIENT_FUNDS") == "钱包余额"
    assert classify_billing_error(None, "NOT_ENOUGH_COINS") == "RH 金币"
    assert classify_billing_error(500, "some other error") is None
    assert classify_billing_error(None, "") is None


def _wf_store_with(billing: str) -> dict:
    return store.upsert_workflow({
        "webappId": "2046794551444119554", "name": "Omnipotent G",
        "description": "", "billingType": billing, "nodeInfoList": [],
    })


def test_submit_task_reads_v2_task_envelope_error(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    """v2 /openapi/v2/run/... 回的是任务信封 {errorCode, errorMessage}，不是 {code, msg}。

    2026-11-27 实测：同一个 key 在 v2 上回
    {"taskId":"","errorCode":"605","errorMessage":"Your balance is insufficient..."}。
    不认这个形状就会误报成「未返回 taskId」，605 根因全丢。
    """
    from buddy_proxy.runninghub import client as rh_client

    class _Resp:
        status_code = 200

        def json(self):
            return {"taskId": "", "status": "", "errorCode": "605",
                    "errorMessage": "Your balance is insufficient. Please top up your account."}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(rh_client.httpx, "AsyncClient", lambda **kw: _Client())
    with pytest.raises(rh_client.RunningHubError) as ei:
        asyncio.run(rh_client.submit_task("k", "ai-app/2002675341464334337", {"nodeInfoList": []}))
    assert ei.value.code == "605"
    assert "balance is insufficient" in str(ei.value)
    assert rh_client.classify_billing_error(ei.value.code, str(ei.value)) == "钱包余额"


def test_fetch_account_status_returns_remain_point():
    """accountStatus 实测多回一个 remainPoint（API 可用额度），必须采回来。"""
    from buddy_proxy.runninghub import client as rh_client

    class _Resp:
        status_code = 200

        def json(self):
            return {"code": 0, "msg": "success", "data": {
                "remainCoins": "994", "currentTaskCounts": "0", "remainMoney": None,
                "remainPoint": 0.000, "currency": None, "apiType": "SHARED"}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _Resp()

    old = rh_client.httpx.AsyncClient
    rh_client.httpx.AsyncClient = lambda **kw: _Client()
    try:
        info = asyncio.run(rh_client.fetch_account_status("k"))
    finally:
        rh_client.httpx.AsyncClient = old
    assert info["remainPoint"] == 0.000
    assert info["remainCoins"] == "994"
    assert info["apiType"] == "SHARED"


def test_store_crud_and_permissions(isolated_store: Path):
    acc = store.upsert_account(
        api_key="rh-secret-token-123456",
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
    store.upsert_account(api_key="rh-secret-token-123456", label="test")
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
    store.upsert_account(api_key="rh-secret-token-123456", label="test")
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
