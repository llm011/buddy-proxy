"""RunningHub 多账号凭据与额度信息存储。

凭据文件权限固定 0600，使用原子写 + flock 防止并发写坏。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from buddy_proxy.core.account_store import atomic_write_json, read_json_dict

_DEFAULT_STORE_PATH = Path.home() / ".buddy-proxy" / "runninghub_accounts.json"


def _store_path() -> Path:
    env = os.environ.get("RUNNINGHUB_ACCOUNTS_PATH", "").strip()
    return Path(env) if env else _DEFAULT_STORE_PATH


def _account_id_for_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _mask_key(api_key: str) -> str:
    if len(api_key) <= 8:
        return "****"
    return f"{api_key[:4]}...{api_key[-4:]}"


def _read_raw() -> dict[str, Any]:
    data = read_json_dict(_store_path())
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        return {"accounts": []}
    return data


def _write_raw(data: dict[str, Any]) -> None:
    atomic_write_json(_store_path(), data)


def list_accounts(*, include_secret: bool = False) -> list[dict[str, Any]]:
    """列出所有已保存的 RunningHub 账号。默认脱敏 api_key。"""
    accounts = _read_raw().get("accounts", [])
    result: list[dict[str, Any]] = []
    for item in accounts:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        raw_key = str(entry.get("api_key") or "")
        entry["masked_key"] = _mask_key(raw_key)
        if not include_secret:
            entry.pop("api_key", None)
        result.append(entry)
    return result


def get_active_accounts() -> list[dict[str, Any]]:
    """返回包含 api_key 的可用账号列表（按用户排序）。"""
    return [
        dict(item)
        for item in _read_raw().get("accounts", [])
        if isinstance(item, dict) and item.get("api_key")
    ]


def upsert_account(
    api_key: str,
    *,
    label: str | None = None,
    account_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """新增或更新一个 RunningHub 账号。"""
    key = api_key.strip()
    if not key:
        raise ValueError("api_key 不能为空")

    aid = _account_id_for_key(key)
    data = _read_raw()
    accounts: list[dict[str, Any]] = data["accounts"]
    now = int(time.time())

    existing = next((a for a in accounts if a.get("account_id") == aid), None)
    if existing is None:
        existing = {
            "account_id": aid,
            "api_key": key,
            "label": (label or "").strip() or f"rh-{aid[:6]}",
            "created_at": now,
        }
        accounts.append(existing)
    else:
        existing["api_key"] = key
        if label is not None and label.strip():
            existing["label"] = label.strip()

    if account_info is not None:
        existing["account_info"] = dict(account_info)
        existing["quota_updated_at"] = now

    existing["updated_at"] = now
    _write_raw(data)

    out = dict(existing)
    out["masked_key"] = _mask_key(key)
    out.pop("api_key", None)
    return out


def update_account_info(account_id: str, account_info: dict[str, Any]) -> dict[str, Any] | None:
    """更新指定账号的额度缓存信息。"""
    data = _read_raw()
    for item in data["accounts"]:
        if item.get("account_id") == account_id:
            item["account_info"] = dict(account_info)
            item["quota_updated_at"] = int(time.time())
            _write_raw(data)
            out = dict(item)
            out["masked_key"] = _mask_key(str(item.get("api_key") or ""))
            out.pop("api_key", None)
            return out
    return None


def delete_account(account_id: str) -> bool:
    data = _read_raw()
    before = len(data["accounts"])
    data["accounts"] = [a for a in data["accounts"] if a.get("account_id") != account_id]
    if len(data["accounts"]) == before:
        return False
    _write_raw(data)
    return True


def rename_account(account_id: str, label: str) -> bool:
    new_label = label.strip()
    if not new_label:
        return False
    data = _read_raw()
    for item in data["accounts"]:
        if item.get("account_id") == account_id:
            item["label"] = new_label
            item["updated_at"] = int(time.time())
            _write_raw(data)
            return True
    return False


def reorder_accounts(order: list[str]) -> list[dict[str, Any]]:
    data = _read_raw()
    by_id = {a.get("account_id"): a for a in data["accounts"] if a.get("account_id")}
    reordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for aid in order:
        if aid in by_id and aid not in seen:
            reordered.append(by_id[aid])
            seen.add(aid)
    for aid, item in by_id.items():
        if aid not in seen:
            reordered.append(item)
    data["accounts"] = reordered
    _write_raw(data)
    return list_accounts(include_secret=False)


# ---- 工作流（AI 应用）列表存储 ----

def _workflows_path() -> Path:
    env = os.environ.get("RUNNINGHUB_WORKFLOWS_PATH", "").strip()
    return Path(env) if env else (Path.home() / ".buddy-proxy" / "runninghub_workflows.json")


def _read_workflows_raw() -> dict[str, Any]:
    data = read_json_dict(_workflows_path())
    if not isinstance(data, dict) or not isinstance(data.get("workflows"), list):
        return {"workflows": []}
    return data


def list_workflows() -> list[dict[str, Any]]:
    return [dict(w) for w in _read_workflows_raw().get("workflows", []) if isinstance(w, dict)]


def upsert_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    wid = str(workflow.get("webappId") or "").strip()
    if not wid:
        raise ValueError("webappId 不能为空")
    data = _read_workflows_raw()
    items: list[dict[str, Any]] = data["workflows"]
    now = int(time.time())
    entry = dict(workflow)
    entry["webappId"] = wid
    entry["updated_at"] = now

    idx = next((i for i, w in enumerate(items) if str(w.get("webappId")) == wid), None)
    if idx is None:
        entry["created_at"] = now
        items.append(entry)
    else:
        entry["created_at"] = items[idx].get("created_at", now)
        items[idx] = entry
    atomic_write_json(_workflows_path(), data)
    return entry


def delete_workflow(webapp_id: str) -> bool:
    wid = str(webapp_id).strip()
    data = _read_workflows_raw()
    before = len(data["workflows"])
    data["workflows"] = [w for w in data["workflows"] if str(w.get("webappId")) != wid]
    if len(data["workflows"]) == before:
        return False
    atomic_write_json(_workflows_path(), data)
    return True


def rename_workflow(webapp_id: str, name: str, billing_type: str | None = None) -> bool:
    wid = str(webapp_id).strip()
    new_name = name.strip()
    if not wid or not new_name:
        return False
    data = _read_workflows_raw()
    for item in data["workflows"]:
        if str(item.get("webappId")) == wid:
            item["name"] = new_name
            if billing_type is not None and billing_type.strip():
                item["billingType"] = billing_type.strip()
            item["updated_at"] = int(time.time())
            atomic_write_json(_workflows_path(), data)
            return True
    return False
