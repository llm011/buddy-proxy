"""RunningHub OpenAPI 客户端。"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://www.runninghub.ai"


class RunningHubError(RuntimeError):
    """RunningHub 上游调用异常。"""

    def __init__(self, message: str, *, status_code: int = 502, code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _base_url() -> str:
    return (os.environ.get("RUNNINGHUB_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _resolve_submit_url(endpoint: str, base_url: str) -> str:
    ep = endpoint.strip().lstrip("/")
    if ep.startswith("openapi/v2/"):
        return f"{base_url}/{ep}"
    if ep.startswith("ai-app/"):
        return f"{base_url}/openapi/v2/run/{ep}"
    if ep.startswith("workflow/"):
        return f"{base_url}/openapi/v2/run/{ep}"
    return f"{base_url}/openapi/v2/{ep}"


async def fetch_account_status(
    api_key: str,
    *,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """查询 RunningHub 账号状态与剩余额度。"""
    url = f"{_base_url()}/uc/openapi/accountStatus"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            headers=_headers(api_key),
            json={"apikey": api_key},
        )
    if resp.status_code >= 400:
        raise RunningHubError(
            f"RunningHub 账号查询失败 (HTTP {resp.status_code})",
            status_code=resp.status_code,
        )
    data = resp.json()
    if not isinstance(data, dict) or data.get("code") not in (0, None):
        msg = data.get("msg") if isinstance(data, dict) else "响应格式异常"
        raise RunningHubError(f"RunningHub 账号查询失败: {msg}", status_code=400)
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    return {
        "remainCoins": payload.get("remainCoins"),
        "currentTaskCounts": payload.get("currentTaskCounts"),
        "remainMoney": payload.get("remainMoney"),
        "currency": payload.get("currency"),
        "apiType": payload.get("apiType"),
        "username": (
            payload.get("username")
            or payload.get("userName")
            or payload.get("nickName")
            or payload.get("accountName")
        ),
    }


async def submit_task(
    api_key: str,
    endpoint: str,
    payload: dict[str, Any],
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """提交生图/生视频任务，返回包含 taskId 的响应字典。"""
    base = _base_url()
    ep = endpoint.strip().lstrip("/")
    req_payload = dict(payload)
    if ep.startswith("ai-app/"):
        webapp_id = ep[len("ai-app/"):]
        url = f"{base}/task/openapi/ai-app/run"
        req_payload.setdefault("webappId", webapp_id)
        req_payload.setdefault("apiKey", api_key)
    else:
        url = _resolve_submit_url(endpoint, base)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=_headers(api_key), json=req_payload)
    if resp.status_code >= 400:
        raise RunningHubError(
            f"RunningHub 提交任务失败 (HTTP {resp.status_code})",
            status_code=resp.status_code,
        )
    data = resp.json()
    if not isinstance(data, dict):
        raise RunningHubError("RunningHub 提交任务响应非 JSON 对象")
    if data.get("code") not in (0, None):
        raise RunningHubError(
            f"RunningHub 提交任务失败: {data.get('msg') or data.get('errorMessage') or '未知错误'}",
            status_code=400,
            code=data.get("code"),
        )
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    task_id = inner.get("taskId") or inner.get("task_id") or data.get("taskId")
    if not task_id:
        raise RunningHubError(f"RunningHub 提交任务未返回 taskId (msg={data.get('msg')})")
    return {"taskId": str(task_id), "raw": inner}


async def query_task(
    api_key: str,
    task_id: str,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """查询任务状态与输出文件列表。"""
    url = f"{_base_url()}/openapi/v2/query"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            headers=_headers(api_key),
            json={"taskId": task_id},
        )
    if resp.status_code >= 400:
        raise RunningHubError(
            f"RunningHub 查询任务失败 (HTTP {resp.status_code})",
            status_code=resp.status_code,
        )
    data = resp.json()
    if not isinstance(data, dict):
        raise RunningHubError("RunningHub 查询任务响应非 JSON 对象")
    if data.get("code") not in (0, None):
        raise RunningHubError(
            f"RunningHub 查询任务失败: {data.get('msg') or '未知错误'}",
            status_code=400,
            code=data.get("code"),
        )
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    return inner


async def fetch_webapp_info(
    api_key: str,
    webapp_id: str,
    *,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """获取 AI 应用（ai-app）的名称、描述与可配置参数列表 nodeInfoList。"""
    url = f"{_base_url()}/api/webapp/apiCallDemo"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            url,
            params={"apiKey": api_key, "webappId": webapp_id},
        )
    if resp.status_code >= 400:
        raise RunningHubError(
            f"获取 AI 应用信息失败 (HTTP {resp.status_code})",
            status_code=resp.status_code,
        )
    data = resp.json()
    if not isinstance(data, dict) or data.get("code") not in (0, None):
        msg = data.get("msg") if isinstance(data, dict) else "响应格式异常"
        raise RunningHubError(f"获取 AI 应用信息失败: {msg}", status_code=400)
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    node_info_list = inner.get("nodeInfoList")
    if not isinstance(node_info_list, list):
        node_info_list = []

    charge_raw = str(
        inner.get("chargeType")
        or inner.get("consumeType")
        or inner.get("payType")
        or ""
    ).upper()
    if charge_raw in ("MONEY", "BALANCE", "CASH", "CNY", "USD") or inner.get("thirdParty"):
        billing_type = "钱包余额"
    elif charge_raw in ("COINS", "COIN", "RH_COIN"):
        billing_type = "RH 金币"
    else:
        billing_type = "RH 金币 / 余额"

    return {
        "webappId": str(webapp_id),
        "name": inner.get("webappName") or inner.get("name") or f"ai-app-{webapp_id}",
        "description": inner.get("description") or inner.get("webappDesc") or "",
        "billingType": billing_type,
        "nodeInfoList": node_info_list,
    }


async def upload_file(
    api_key: str,
    filename: str,
    content: bytes,
    *,
    content_type: str = "application/octet-stream",
    file_type: str = "image",
    timeout: float = 60.0,
) -> str:
    """上传本地文件（图片/音频/视频）到 RunningHub，返回服务端 fileName。"""
    url = f"{_base_url()}/task/openapi/upload"
    files = {"file": (filename or "upload.bin", content, content_type)}
    form_data = {"apiKey": api_key, "fileType": file_type}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            data=form_data,
            files=files,
        )
    if resp.status_code >= 400:
        raise RunningHubError(
            f"RunningHub 上传文件失败 (HTTP {resp.status_code})",
            status_code=resp.status_code,
        )
    data = resp.json()
    if not isinstance(data, dict) or data.get("code") not in (0, None):
        msg = data.get("msg") if isinstance(data, dict) else "未知错误"
        raise RunningHubError(f"RunningHub 上传文件失败: {msg}")
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    file_name = inner.get("fileName") or inner.get("file_name")
    if not file_name:
        raise RunningHubError("RunningHub 上传成功但未返回 fileName")
    return str(file_name)
