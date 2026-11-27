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


def _account_id_for_email(email: str) -> str:
    # 同一个邮箱换密码 / 换 key 不该生成第二条记录，所以 id 只看邮箱（小写归一）。
    # 前缀 e 和按 key 算的 id 区分开，老数据迁移时不会撞。
    return "e" + hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:11]


def _mask_key(api_key: str) -> str:
    if len(api_key) <= 8:
        return "****"
    return f"{api_key[:4]}...{api_key[-4:]}"


def _mask_email(email: str) -> str:
    email = (email or "").strip()
    if "@" not in email:
        return "****" if email else ""
    name, _, domain = email.partition("@")
    shown = name[:2] if len(name) > 2 else name[:1]
    return f"{shown}***@{domain}"


def _read_raw() -> dict[str, Any]:
    data = read_json_dict(_store_path())
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        return {"accounts": []}
    return data


def _write_raw(data: dict[str, Any]) -> None:
    atomic_write_json(_store_path(), data)


def _session_status(ws: dict[str, Any] | None) -> dict[str, Any]:
    """给 UI 看的登录态摘要：有没有、什么时候过期、上次为什么失败。"""
    if not isinstance(ws, dict) or not ws.get("access_token"):
        return {"logged_in": False}
    expires_at = float(ws.get("expires_at") or 0)
    return {
        "logged_in": True,
        "user_id": ws.get("user_id") or "",
        "expires_at": expires_at,
        "expires_in_days": round((expires_at - time.time()) / 86400, 1) if expires_at else None,
        "refreshed_at": ws.get("refreshed_at"),
        "last_error": str(ws.get("last_error") or ""),
        "last_error_at": ws.get("last_error_at"),
    }


def _public(acc: dict[str, Any]) -> dict[str, Any]:
    """脱敏后的账号视图：绝不带 password / refresh_token / 完整 access_token / api_key。"""
    out = dict(acc)
    out.pop("password", None)
    out.pop("api_key", None)
    out.pop("web_session", None)
    out["masked_email"] = _mask_email(str(acc.get("email") or ""))
    out["masked_key"] = _mask_key(str(acc.get("api_key") or ""))
    out["web_session_status"] = _session_status(acc.get("web_session"))
    # account_info 是上游 dict 整个存进来的，万一哪天接口多回一个 apiKey/token 字段，
    # 这里兜底抹掉——别指望上游永远不加字段
    info = out.get("account_info")
    if isinstance(info, dict):
        out["account_info"] = {k: v for k, v in info.items() if not _looks_secret(k)}
    return out


def _looks_secret(key: str) -> bool:
    k = key.lower()
    return k.endswith(("key", "token", "secret", "password")) or "apikey" in k


def list_accounts(*, include_secret: bool = False) -> list[dict[str, Any]]:
    """列出所有已保存的 RunningHub 账号。默认脱敏 email / api_key / 登录态。"""
    result: list[dict[str, Any]] = []
    for item in _read_raw().get("accounts", []):
        if not isinstance(item, dict):
            continue
        result.append(dict(item) if include_secret else _public(item))
    return result


def get_active_accounts() -> list[dict[str, Any]]:
    """返回能用的账号（按用户排序）：有邮箱密码（网页端）或 api_key（OpenAPI）都算。"""
    out: list[dict[str, Any]] = []
    for item in _read_raw().get("accounts", []):
        if not isinstance(item, dict):
            continue
        if (item.get("email") and item.get("password")) or item.get("api_key"):
            out.append(dict(item))
    return out


def get_account(account_id: str) -> dict[str, Any] | None:
    for item in _read_raw().get("accounts", []):
        if isinstance(item, dict) and item.get("account_id") == account_id:
            return dict(item)
    return None


def upsert_account(
    *,
    email: str | None = None,
    password: str | None = None,
    api_key: str | None = None,
    label: str | None = None,
    account_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """新增或更新一个 RunningHub 账号。

    主键优先按邮箱（网页端登录态），没有邮箱才按 api_key（纯 OpenAPI 账号）。
    ``password`` 传 ``None`` 表示不改密码（避免只改备注时把密码冲掉）；传非空
    则覆盖并**作废旧登录态**——密码都换了，旧 token 随时会被踢。
    """
    email = (email or "").strip()
    api_key = (api_key or "").strip()
    if not email and not api_key:
        raise ValueError("email 或 api_key 至少要有一个")
    if email and password is not None and not password:
        raise ValueError("有邮箱的账号 password 不能为空")

    aid = _account_id_for_email(email) if email else _account_id_for_key(api_key)
    data = _read_raw()
    accounts: list[dict[str, Any]] = data["accounts"]
    now = int(time.time())

    existing = next((a for a in accounts if a.get("account_id") == aid), None)
    if existing is None:
        existing = {"account_id": aid, "created_at": now, "email": email, "api_key": api_key}
        accounts.append(existing)
    else:
        if email:
            existing["email"] = email
        if api_key:
            existing["api_key"] = api_key
    if password:
        existing["password"] = password
        existing.pop("web_session", None)
    if label is not None and label.strip():
        existing["label"] = label.strip()
    elif not existing.get("label"):
        existing["label"] = email or f"rh-{api_key[-4:]}"
    if account_info is not None:
        existing["account_info"] = dict(account_info)
        existing["quota_updated_at"] = now
    existing["updated_at"] = now
    _write_raw(data)
    return _public(existing)


def update_session(account_id: str, session: dict[str, Any]) -> dict[str, Any] | None:
    """写入/更新登录态（``WebSession.to_dict()``）。"""
    data = _read_raw()
    for item in data["accounts"]:
        if item.get("account_id") == account_id:
            item["web_session"] = dict(session)
            item["session_updated_at"] = int(time.time())
            _write_raw(data)
            return _public(item)
    return None


def clear_session(account_id: str) -> bool:
    data = _read_raw()
    for item in data["accounts"]:
        if item.get("account_id") == account_id:
            item.pop("web_session", None)
            _write_raw(data)
            return True
    return False


def update_account_info(account_id: str, account_info: dict[str, Any]) -> dict[str, Any] | None:
    """更新指定账号的额度缓存信息。"""
    data = _read_raw()
    for item in data["accounts"]:
        if item.get("account_id") == account_id:
            item["account_info"] = dict(account_info)
            item["quota_updated_at"] = int(time.time())
            _write_raw(data)
            return _public(item)
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


# ---- 任务队列存储 ----
#
# 一次站内运行 = 一条记录：提交即落盘（status=SUBMITTING），后台执行器负责把状态往前推
# （RUNNING → SUCCESS / FAILED / TIMEOUT / CANCELLED），UI 轮询列表就能看到进度，
# 不用像以前那样在弹窗里挂着一个 HTTP 请求等到出图。
#
# 记录里**不放**账号密码 / token，只放 account_id；参数原样存（nodeInfoList 是用户填的
# 表单值，没有敏感信息），失败原因存解析后的 exception_message（截断）。

_TASKS_LIMIT = 200


def _tasks_path() -> Path:
    env = os.environ.get("RUNNINGHUB_TASKS_PATH", "").strip()
    return Path(env) if env else (Path.home() / ".buddy-proxy" / "runninghub_tasks.json")


def _read_tasks_raw() -> dict[str, Any]:
    data = read_json_dict(_tasks_path())
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        return {"tasks": []}
    return data


def _write_tasks_raw(data: dict[str, Any]) -> None:
    atomic_write_json(_tasks_path(), data)


def list_tasks(*, limit: int | None = None, archived: bool = False) -> list[dict[str, Any]]:
    """按创建时间倒序（新的在前）。``limit`` 为 None 时全量。"""
    items = [
        dict(t) for t in _read_tasks_raw().get("tasks", [])
        if isinstance(t, dict) and bool(t.get("archived")) is archived
    ]
    items.sort(key=lambda t: float(t.get("created_at") or 0), reverse=True)
    return items if limit is None else items[:limit]


def get_task(task_id: str) -> dict[str, Any] | None:
    for t in _read_tasks_raw().get("tasks", []):
        if isinstance(t, dict) and str(t.get("taskId")) == str(task_id):
            return dict(t)
    return None


def upsert_task(task: dict[str, Any]) -> dict[str, Any]:
    """新增或按 ``taskId`` 更新一条任务记录；只保留最近 ``_TASKS_LIMIT`` 条。"""
    tid = str(task.get("taskId") or "").strip()
    if not tid:
        raise ValueError("taskId 不能为空")
    data = _read_tasks_raw()
    items: list[dict[str, Any]] = data["tasks"]
    now = time.time()
    entry = dict(task)
    entry["taskId"] = tid
    entry["updated_at"] = now

    idx = next((i for i, t in enumerate(items) if str(t.get("taskId")) == tid), None)
    if idx is None:
        entry.setdefault("created_at", now)
        items.append(entry)
    else:
        entry["created_at"] = items[idx].get("created_at", now)
        # 展示用的字段（名字 / 缩略图 / 成本）累计合并，别被一次只改状态的写入抹掉
        for k, v in items[idx].items():
            entry.setdefault(k, v)
        items[idx] = entry
    if len(items) > _TASKS_LIMIT:
        items.sort(key=lambda t: float(t.get("created_at") or 0), reverse=True)
        del items[_TASKS_LIMIT:]
    _write_tasks_raw(data)
    return entry


def update_task(task_id: str, **fields: Any) -> dict[str, Any] | None:
    """只改几个字段（状态推进时用），其余保留。"""
    cur = get_task(task_id)
    if cur is None:
        return None
    cur.update(fields)
    return upsert_task(cur)


def delete_task(task_id: str) -> bool:
    data = _read_tasks_raw()
    before = len(data["tasks"])
    data["tasks"] = [t for t in data["tasks"] if str(t.get("taskId")) != str(task_id)]
    if len(data["tasks"]) == before:
        return False
    _write_tasks_raw(data)
    return True
