"""管理 UI：各通道专属管理接口（qoder / traepat / codebuddy）。

原 ``web/ui.py`` 拆分：通道相关的接口都在这里，后续新通道的管理面板接口
（如 antigravity 账号状态）也加到本模块。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import time
from typing import Any

from fastapi import HTTPException, Request

from buddy_proxy.core.state import app, get_state

from .common import _ensure_local

log = logging.getLogger(__name__)


def _query_int(request: Request, name: str, default: int, *, maximum: int | None = None) -> int:
    """解析正整数查询参数；非法输入给 400，而不是漏成未捕获的 ValueError。"""
    raw = request.query_params.get(name)
    try:
        value = int(raw or default)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"error": {
            "message": f"{name} 必须是正整数"}}) from exc
    value = max(1, value)
    return min(value, maximum) if maximum is not None else value


def _local_str_to_epoch(value: str) -> float | None:
    """``YYYY-MM-DD HH:MM:SS``（无时区、按本机时间）→ epoch 秒；解析不了返回 None。

    CodeBuddy 的 ``requestTime`` 就是这种形态：上游按账号所在时区出数、不带
    偏移量。本机时区与账号时区一致时才准——不一致会有偏差，但比丢掉时间强，
    hover 仍展示原始串（``time_raw``）供核对。
    """
    import time as _t
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return _t.mktime(_t.strptime(text, fmt))
        except ValueError:
            continue
    return None


@app.get("/ui/api/qoder/auth")
async def ui_qoder_auth(request: Request):
    """Qoder 登录/鉴权状态 + 账号信息（供管理页鉴权面板展示）。

    纯本地读取，不触网：返回区域、首个账号的 token 过期时间与来源，前端据此
    提示「未登录 / 即将过期 / 正常」，并可给出重新登录的命令。多账号明细走
    ``/ui/api/qoder/accounts``。
    """
    _ensure_local(request)
    state = get_state()
    provider = getattr(state, "providers", {}).get("qoder")
    if provider is None:
        return {"enabled": False, "authenticated": False}

    try:
        from ...qoder.credentials import (
            cred_to_credential,
            list_accounts,
            load_account_cred,
            qoder_state_dir,
        )
        from ...qoder.config import REGIONS

        region = provider.region()
        info: dict[str, Any] = {
            "enabled": True,
            "region": region.key,
            "region_label": region.label,
            "regions": sorted(REGIONS),
            "endpoint": region.infer_base,
            "state_dir": str(qoder_state_dir()),
            "login_command": "buddy login qoder",
        }
    except Exception as exc:  # noqa: BLE001 - 状态面板不该因读取失败而 500
        return {"enabled": True, "authenticated": False, "error": str(exc)[:200]}

    accounts = list_accounts()
    first = (load_account_cred(accounts[0].id) if accounts else None) or {}
    first_cred = cred_to_credential(first)
    now_ms = int(time.time() * 1000)
    expires_at = first_cred.expires_at_ms
    info.update({
        "authenticated": bool(accounts),
        "uid": first_cred.uid,
        "name": first_cred.name,
        "email": first_cred.email,
        "plan": first_cred.plan,
        "source": first_cred.source,
        "expires_at_ms": expires_at or None,
        "expires_in_days": (
            round((expires_at - now_ms) / 86400000, 1) if expires_at else None
        ),
        "expired": bool(expires_at and expires_at <= now_ms),
        "has_refresh_token": bool(first_cred.refresh_token),
        "account_count": len(accounts),
    })
    return info


@app.get("/ui/api/qoder/accounts")
async def ui_qoder_accounts(request: Request):
    """qoder 各账号本地凭证/冷却状态（纯本地不触网，不含秘密）。"""
    _ensure_local(request)
    try:
        from ...qoder import failover
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "qoder 通道不可用"}})
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/qoder/accounts/order")
async def ui_qoder_accounts_order(request: Request):
    """调整 qoder 账号的 failover 顺位（管理页上移/下移按钮）。

    提交完整账号 id 顺序列表，重写 index.json 的 priority；返回重排后的账号
    状态（与 GET 同构）。quota 缓存键带 priority（``quota_epoch``），重排后旧
    额度快照自动失效。
    """
    _ensure_local(request)
    try:
        from ...qoder import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "qoder 通道不可用"}})
    body = await request.json()
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 ids（账号 id 的完整顺序列表）"}})
    try:
        await asyncio.to_thread(creds.reorder_accounts, ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    from ...qoder import failover
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/qoder/accounts/rename")
async def ui_qoder_accounts_rename(request: Request):
    """改一个 qoder 账号的本地别名（管理页 ✎）。

    只改 index.json 的 ``alias`` 字段（显示名），原始凭据与顺位不动；
    空串 = 清除别名、回退默认显示名。返回改后的账号状态（与 GET 同构）。
    """
    _ensure_local(request)
    try:
        from ...qoder import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "qoder 通道不可用"}})
    body = await request.json()
    aid, alias = body.get("id"), body.get("alias")
    if not isinstance(aid, str) or not aid.strip() or not isinstance(alias, str):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id 或 alias（要改的账号 id 与新别名）"}})
    if len(alias.strip()) > 64:
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "别名过长（≤64 字符）"}})
    try:
        await asyncio.to_thread(creds.rename_account, aid.strip(), alias)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": {"message": str(exc)}})
    from ...qoder import failover
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/qoder/accounts/delete")
async def ui_qoder_accounts_delete(request: Request):
    """删除一个 qoder 账号（索引条目 + cred 文件 + 冷却标记）。

    不再使用或凭据作废的账号从轮换里摘掉——留着每轮 failover 白打一次上游。
    返回删除后的账号状态（与 GET 同构，前端直接重渲染）。
    """
    _ensure_local(request)
    try:
        from ...qoder import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "qoder 通道不可用"}})
    body = await request.json()
    aid = body.get("id")
    if not isinstance(aid, str) or not aid.strip():
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id（要删除的账号 id）"}})
    aid = aid.strip()
    removed = await asyncio.to_thread(creds.delete_account, aid)
    if not removed:
        raise HTTPException(status_code=404,
                            detail={"error": {"message": f"账号不存在: {aid}"}})
    from ...qoder import failover
    await asyncio.to_thread(failover.clear_cooldown, aid)
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/qoder/models/refresh")
async def ui_qoder_models_refresh(request: Request):
    """从上游刷新 Qoder 模型目录（管理页「刷新模型」按钮）。"""
    _ensure_local(request)
    state = get_state()
    provider = getattr(state, "providers", {}).get("qoder")
    if provider is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "qoder 通道未启用"}})
    models = await provider.refresh_models(force=True)
    return {"ok": True, "count": len(models), "models": models}


@app.get("/ui/api/traepat/model-status")
async def ui_traepat_model_status_cached(request: Request):
    """读取 traepat 模型负载缓存（纯本地，不触网）；无缓存返回空壳供页面默认展示。"""
    _ensure_local(request)
    try:
        from ...trae.pat import fetch_pat_model_status
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "traepat 通道不可用"}})
    return await asyncio.to_thread(fetch_pat_model_status, False, True)


@app.post("/ui/api/traepat/model-status")
async def ui_traepat_model_status(request: Request):
    """手动触发 traepat 模型负载查询（10 分钟内重复触发走缓存，避免频打上游）。"""
    _ensure_local(request)
    try:
        from ...trae.pat import fetch_pat_model_status
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "traepat 通道不可用"}})
    return await asyncio.to_thread(fetch_pat_model_status)


@app.get("/ui/api/traepat/accounts")
async def ui_traepat_accounts(request: Request):
    """traepat 各账号本地凭证/冷却状态 + 后台自愈循环最近一轮结果（纯本地，不触网）。"""
    _ensure_local(request)
    try:
        from ...trae.pat import accounts_status
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "traepat 通道不可用"}})
    return await asyncio.to_thread(accounts_status)


@app.get("/ui/api/antigravity/accounts")
async def ui_antigravity_accounts(request: Request):
    """antigravity 各账号本地凭证/冷却状态（纯本地，不触网，不含秘密）。"""
    _ensure_local(request)
    try:
        from ...antigravity import failover
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "antigravity 通道不可用"}})
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/antigravity/accounts/order")
async def ui_antigravity_accounts_order(request: Request):
    """调整 antigravity 账号的 failover 顺位（管理页上移/下移按钮）。

    提交完整的账号 id 顺序列表，重写 index.json 的 priority；返回重排后的
    账号状态（与 GET 同构，前端直接重渲染）。quota 缓存键带 priority
    （``quota_epoch``），重排后旧额度快照自动失效、下一轮刷新即换新顺位。
    """
    _ensure_local(request)
    try:
        from ...antigravity import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "antigravity 通道不可用"}})
    body = await request.json()
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 ids（账号 id 的完整顺序列表）"}})
    try:
        await asyncio.to_thread(creds.reorder_accounts, ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    from ...antigravity import failover
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/antigravity/accounts/delete")
async def ui_antigravity_accounts_delete(request: Request):
    """删除一个 antigravity 账号（索引条目 + cred 文件 + 冷却标记）。

    给「能登录但被 Google 拉黑（403 Verify your account，面板副标题显示
    疑似拉黑）」或不再使用的账号准备——留在轮换里只会每轮 failover 白打
    一次。返回删除后的账号状态（与 GET 同构，前端直接重渲染）。
    """
    _ensure_local(request)
    try:
        from ...antigravity import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "antigravity 通道不可用"}})
    body = await request.json()
    aid = body.get("id")
    if not isinstance(aid, str) or not aid.strip():
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id（要删除的账号 id）"}})
    aid = aid.strip()
    removed = await asyncio.to_thread(creds.delete_account, aid)
    if not removed:
        raise HTTPException(status_code=404,
                            detail={"error": {"message": f"账号不存在: {aid}"}})
    from ...antigravity import failover
    await asyncio.to_thread(failover.clear_cooldown, aid)
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/antigravity/accounts/rename")
async def ui_antigravity_accounts_rename(request: Request):
    """改一个 antigravity 账号的本地别名（管理页 ✎）。

    只改 index.json 的 ``alias`` 字段（显示名），原始凭据与顺位不动；
    空串 = 清除别名、回退默认显示名。返回改后的账号状态（与 GET 同构）。
    """
    _ensure_local(request)
    try:
        from ...antigravity import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "antigravity 通道不可用"}})
    body = await request.json()
    aid, alias = body.get("id"), body.get("alias")
    if not isinstance(aid, str) or not aid.strip() or not isinstance(alias, str):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id 或 alias（要改的账号 id 与新别名）"}})
    if len(alias.strip()) > 64:
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "别名过长（≤64 字符）"}})
    try:
        await asyncio.to_thread(creds.rename_account, aid.strip(), alias)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": {"message": str(exc)}})
    from ...antigravity import failover
    return await asyncio.to_thread(failover.accounts_status)


@app.get("/ui/api/trae/accounts")
async def ui_trae_accounts(request: Request):
    """trae work 各账号本地凭证/冷却状态（纯本地不触网，不含秘密）。"""
    _ensure_local(request)
    try:
        from ...trae import failover
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "trae 通道不可用"}})
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/trae/accounts/order")
async def ui_trae_accounts_order(request: Request):
    """调整 trae work 账号的 failover 顺位（管理页上移/下移按钮）。

    提交完整账号 id 顺序列表，重写 index.json 的 priority；返回重排后的账号
    状态（与 GET 同构）。quota 缓存键带 priority（``quota_epoch``），重排后旧
    额度快照自动失效。
    """
    _ensure_local(request)
    try:
        from ...trae import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "trae 通道不可用"}})
    body = await request.json()
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 ids（账号 id 的完整顺序列表）"}})
    try:
        await asyncio.to_thread(creds.reorder_accounts, ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    from ...trae import failover
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/trae/accounts/delete")
async def ui_trae_accounts_delete(request: Request):
    """删除一个 trae work 账号（索引条目 + cred 文件 + 冷却标记）。

    不再使用或凭据作废的账号从轮换里摘掉——留着每轮 failover 白打一次上游。
    返回删除后的账号状态（与 GET 同构，前端直接重渲染）。
    """
    _ensure_local(request)
    try:
        from ...trae import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "trae 通道不可用"}})
    body = await request.json()
    aid = body.get("id")
    if not isinstance(aid, str) or not aid.strip():
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id（要删除的账号 id）"}})
    aid = aid.strip()
    removed = await asyncio.to_thread(creds.delete_account, aid)
    if not removed:
        raise HTTPException(status_code=404,
                            detail={"error": {"message": f"账号不存在: {aid}"}})
    from ...trae import failover
    await asyncio.to_thread(failover.clear_cooldown, aid)
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/trae/accounts/rename")
async def ui_trae_accounts_rename(request: Request):
    """改一个 trae work 账号的本地别名（管理页 ✎）。

    只改 index.json 的 ``alias`` 字段（显示名），原始凭据与顺位不动；
    空串 = 清除别名、回退默认显示名。返回改后的账号状态（与 GET 同构）。
    """
    _ensure_local(request)
    try:
        from ...trae import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "trae 通道不可用"}})
    body = await request.json()
    aid, alias = body.get("id"), body.get("alias")
    if not isinstance(aid, str) or not aid.strip() or not isinstance(alias, str):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id 或 alias（要改的账号 id 与新别名）"}})
    if len(alias.strip()) > 64:
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "别名过长（≤64 字符）"}})
    try:
        await asyncio.to_thread(creds.rename_account, aid.strip(), alias)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": {"message": str(exc)}})
    from ...trae import failover
    return await asyncio.to_thread(failover.accounts_status)


@app.get("/ui/api/trae/usage-logs")
async def ui_trae_usage_logs(request: Request):
    """拉取 trae 官方会话级消耗明细（官网 dashboard#usage 背后的接口）。

    仅用于**展示**，不回写 metrics——上游粒度是会话级（一个会话含多次请求），
    与我们日志的请求级对不上，1:1 认领会重复记账。账号面板点「官方明细」按钮
    时调用，弹窗外部分页。

    查询参数：``id``（账号 id，必填）、``page``（默认 1）、``page_size``（默认
    30，上限 50）。窗口固定最近 30 天（接口上限）。

    上游不可达 / token 失效 / 参数被拒时返回 ``{"error": "..."}``（HTTP 200，
    不抛 5xx）——让弹窗渲染友好空态，而不是把整个面板拖挂。
    """
    _ensure_local(request)
    aid = request.query_params.get("id", "").strip()
    if not aid:
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id（账号 id）"}})
    page = _query_int(request, "page", 1)
    page_size = _query_int(request, "page_size", 30, maximum=50)

    try:
        from ...trae import credentials as creds
        from ...trae import benefits_api
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "trae 通道不可用"}})

    def _fetch() -> dict[str, Any]:
        token, cred = creds.ensure_account_token(aid)
        region = (cred.get("region") or "").strip() or "cn"
        out = benefits_api.fetch_usage_logs(
            token=token, account_id=aid, region=region,
            page_size=page_size, page_num=page)
        # 与 raccoon / codebuddy 路由对齐：统一叫 currency（credits | dollar），
        # 前端一套格式化逻辑通吃三路
        out["currency"] = out.pop("billing", "credits")
        return out

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        # 凭据不存在 / token 刷新失败 / 上游拒绝（9004）/ 网络——都归到 error 字段，
        # 前端弹窗显示原话，不让单账号失败拖垮面板。
        return {"error": str(exc)[:400], "rows": [], "total": 0, "page": page,
                "page_size": page_size}


@app.get("/ui/api/raccoon/usage-logs")
async def ui_raccoon_usage_logs(request: Request):
    """拉取小浣熊官方积分消耗流水（``/api/web/points/v2/bills?type=expense``）。

    与 trae 一样**只展示不回写**：expense 流水是**会话/任务级**（biz_id 是会话
    级聚合 id），不是我们日志的请求级，1:1 认领会重复。流水本身也不带模型名与
    用户 prompt（只有 event_name / client_type），所以预览列显示事件名。

    上游用 **cursor 分页**（``has_more`` / ``next_cursor``，无 total），前端按
    前后翻页（不跳页）。查询参数：``id``（必填）、``page_size``（默认 30，上限
    100）、``cursor``（下一页游标，缺省=首页）。
    """
    _ensure_local(request)
    aid = request.query_params.get("id", "").strip()
    if not aid:
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id（账号 id）"}})
    page_size = _query_int(request, "page_size", 30, maximum=100)
    cursor = request.query_params.get("cursor", "") or None

    try:
        from ...raccoon import credentials as creds
        from ...raccoon import login as rlogin
        from ...raccoon.upstream import BILLS_PATH, build_headers, parse_usage_logs
        from buddy_proxy.core import net as core_net
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "raccoon 通道不可用"}})

    def _fetch() -> dict[str, Any]:
        token, cred = creds.ensure_account_token(aid)
        device_id = str(cred.get("device_id") or "")
        params: dict[str, Any] = {"paging.limit": page_size, "type": "expense"}
        if cursor:
            params["cursor"] = cursor
        headers = build_headers(token, device_id)
        headers["Accept"] = "application/json"
        with core_net.client(timeout=20) as c:
            resp = c.get(f"{rlogin.endpoint()}{BILLS_PATH}", headers=headers, params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"raccoon bills HTTP {resp.status_code}")
        rows, has_more, next_cursor = parse_usage_logs(resp.json())
        return {"rows": rows, "currency": "credits",
                "page_size": page_size, "has_more": has_more,
                "next_cursor": next_cursor, "cursor": cursor}

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        return {"error": str(exc)[:400], "rows": [], "page_size": page_size,
                "has_more": False}


@app.get("/ui/api/codebuddy/usage-logs")
async def ui_codebuddy_usage_logs(request: Request):
    """拉取 CodeBuddy 官方请求级消耗流水（``/billing/meter/get-user-request-usage``）。

    来源是 WorkBuddy web「使用记录」页同源接口，字段最齐：credit（实扣积分）、
    model、client、requestTime、input（用户首条消息截断）。**请求级**粒度，与
    我们日志行天然对得上，但这里仍只做展示（不回写 metrics，避免与转发时本地
    记的 usage 重复；如要对账另开）。

    CN 与海外版（codebuddyintl）共用本路由：``id`` 带 ``intl-`` 前缀时
    ``api_post_as`` 自动路由到海外 base。offset 分页（有 total），可跳页。
    窗口默认最近 30 天（接口约 31 天上限，按 30 切分留余量）。
    """
    _ensure_local(request)
    aid = request.query_params.get("id", "").strip()
    if not aid:
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id（账号 id）"}})
    page = _query_int(request, "page", 1)
    page_size = _query_int(request, "page_size", 30, maximum=100)

    try:
        from ...codebuddy_provider import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "codebuddy 通道不可用"}})

    def _fetch() -> dict[str, Any]:
        import time as _t
        end = _t.strftime("%Y-%m-%d %H:%M:%S")
        start = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(_t.time() - 30 * 86400))
        payload = creds.api_post_as(
            aid, "/billing/meter/get-user-request-usage",
            {"startTime": start, "endTime": end, "pageNum": page, "pageSize": page_size})
        code = payload.get("code")
        if code not in (0, None):
            raise RuntimeError(payload.get("msg") or f"code={code}")
        data = payload.get("data") or {}
        rows = []
        for r in data.get("data") or []:
            if not isinstance(r, dict):
                continue
            inp = str(r.get("inputTrunc") or r.get("input") or "").strip()
            time_raw = str(r.get("requestTime") or "")
            rows.append({
                "credits": r.get("credit"),
                "currency": "credits",
                "model": r.get("model") or "",
                "client": r.get("client") or "",
                "time_raw": time_raw,
                # requestTime 形如 "2026-11-25 01:05:00"，无时区、按本地时间记
                # （上游按账号所在时区出数），按本机时区转 epoch 给前端统一格式化
                "ts": _local_str_to_epoch(time_raw),
                "request_id": r.get("requestId") or "",
                "preview": inp if inp and inp != "-" else "",
            })
        return {"rows": rows, "total": int(data.get("total") or len(rows)),
                "page": page, "page_size": page_size, "currency": "credits"}

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        return {"error": str(exc)[:400], "rows": [], "total": 0, "page": page,
                "page_size": page_size}


@app.get("/ui/api/kimi/accounts")
async def ui_kimi_accounts(request: Request):
    """kimi 各账号本地凭证/冷却状态（纯本地，不触网，不含秘密）。"""
    _ensure_local(request)
    try:
        from ...kimi import failover
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "kimi 通道不可用"}})
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/kimi/accounts/order")
async def ui_kimi_accounts_order(request: Request):
    """调整 kimi 账号的 failover 顺位（管理页上移/下移按钮）。

    与 antigravity 同款：提交完整账号 id 顺序列表，重写 index.json 的
    priority；返回重排后的账号状态（与 GET 同构）。quota 缓存键带 priority
    （``quota_epoch``），重排后旧额度快照自动失效。
    """
    _ensure_local(request)
    try:
        from ...kimi import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "kimi 通道不可用"}})
    body = await request.json()
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 ids（账号 id 的完整顺序列表）"}})
    try:
        await asyncio.to_thread(creds.reorder_accounts, ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    from ...kimi import failover
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/kimi/accounts/delete")
async def ui_kimi_accounts_delete(request: Request):
    """删除一个 kimi 账号（索引条目 + cred 文件 + 冷却标记）。

    refresh_token 作废/不再使用的账号从轮换里摘掉——留着每轮 failover 白打
    一次上游。返回删除后的账号状态（与 GET 同构，前端直接重渲染）。
    """
    _ensure_local(request)
    try:
        from ...kimi import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "kimi 通道不可用"}})
    body = await request.json()
    aid = body.get("id")
    if not isinstance(aid, str) or not aid.strip():
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id（要删除的账号 id）"}})
    aid = aid.strip()
    removed = await asyncio.to_thread(creds.delete_account, aid)
    if not removed:
        raise HTTPException(status_code=404,
                            detail={"error": {"message": f"账号不存在: {aid}"}})
    from ...kimi import failover
    await asyncio.to_thread(failover.clear_cooldown, aid)
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/kimi/accounts/rename")
async def ui_kimi_accounts_rename(request: Request):
    """改一个 kimi 账号的本地别名（管理页 ✎）。

    只改 index.json 的 ``alias`` 字段（显示名），原始凭据与顺位不动；
    空串 = 清除别名、回退默认显示名。返回改后的账号状态（与 GET 同构）。
    """
    _ensure_local(request)
    try:
        from ...kimi import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "kimi 通道不可用"}})
    body = await request.json()
    aid, alias = body.get("id"), body.get("alias")
    if not isinstance(aid, str) or not aid.strip() or not isinstance(alias, str):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id 或 alias（要改的账号 id 与新别名）"}})
    if len(alias.strip()) > 64:
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "别名过长（≤64 字符）"}})
    try:
        await asyncio.to_thread(creds.rename_account, aid.strip(), alias)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": {"message": str(exc)}})
    from ...kimi import failover
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/kimi/accounts/import")
async def ui_kimi_accounts_import(request: Request):
    """导入 kimi cli 导出的 token JSON（面板「导入账号」入口，CLI 共用）。

    body ``{"payload": <str|dict>}``：粘贴的 JSON 文本或已解析对象。同
    refresh_token 更新原账号（顺位不变），新的追加为备用号。补充信息
    （/v1/me）失败不白费导入。返回导入后的账号状态（与 GET 同构）。
    """
    _ensure_local(request)
    try:
        from ...kimi import failover, login
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "kimi 通道不可用"}})
    body = await request.json()
    payload = body.get("payload")
    if payload is None:
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 payload（kimi cli 导出的 token JSON）"}})
    try:
        await asyncio.to_thread(login.import_cred_payload, payload)
    except login.LoginError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    return await asyncio.to_thread(failover.accounts_status)


@app.get("/ui/api/codex/accounts")
async def ui_codex_accounts(request: Request):
    """codex 各账号本地凭证/冷却状态（纯本地，不触网，不含秘密）。"""
    _ensure_local(request)
    try:
        from ...codex import failover
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "codex 通道不可用"}})
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/codex/accounts/order")
async def ui_codex_accounts_order(request: Request):
    """调整 codex 账号的 failover 顺位（管理页上移/下移按钮）。

    与其它通道同款：提交完整账号 id 顺序列表，重写 index.json 的 priority；
    返回重排后的账号状态（与 GET 同构）。quota 缓存键带 priority
    （``quota_epoch``），重排后旧额度快照自动失效。
    """
    _ensure_local(request)
    try:
        from ...codex import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "codex 通道不可用"}})
    body = await request.json()
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 ids（账号 id 的完整顺序列表）"}})
    try:
        await asyncio.to_thread(creds.reorder_accounts, ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    from ...codex import failover
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/codex/accounts/delete")
async def ui_codex_accounts_delete(request: Request):
    """删除一个 codex 账号（索引条目 + cred 文件 + 冷却标记）。"""
    _ensure_local(request)
    try:
        from ...codex import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "codex 通道不可用"}})
    body = await request.json()
    aid = body.get("id")
    if not isinstance(aid, str) or not aid.strip():
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id（要删除的账号 id）"}})
    aid = aid.strip()
    removed = await asyncio.to_thread(creds.delete_account, aid)
    if not removed:
        raise HTTPException(status_code=404,
                            detail={"error": {"message": f"账号不存在: {aid}"}})
    from ...codex import failover
    await asyncio.to_thread(failover.clear_cooldown, aid)
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/codex/accounts/rename")
async def ui_codex_accounts_rename(request: Request):
    """改一个 codex 账号的本地别名（管理页 ✎）。

    只改 index.json 的 ``alias`` 字段（显示名），原始凭据与顺位不动；
    空串 = 清除别名、回退默认显示名。
    """
    _ensure_local(request)
    try:
        from ...codex import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "codex 通道不可用"}})
    body = await request.json()
    aid, alias = body.get("id"), body.get("alias")
    if not isinstance(aid, str) or not aid.strip() or not isinstance(alias, str):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id 或 alias（要改的账号 id 与新别名）"}})
    if len(alias.strip()) > 64:
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "别名过长（≤64 字符）"}})
    try:
        await asyncio.to_thread(creds.rename_account, aid.strip(), alias)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": {"message": str(exc)}})
    from ...codex import failover
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/codex/accounts/import")
async def ui_codex_accounts_import(request: Request):
    """导入凭据 JSON（面板「导入账号」入口，CLI 共用）。

    body ``{"payload": <str|dict>}``。认两种形态：Codex CLI 原生的
    ``{"tokens": {...}}``，以及扁平 ``{"access_token": ..., "refresh_token": ...}``。
    同 refresh_token / 同 chatgpt_account_id 更新原账号（顺位不变），新的追加
    为备用号。**不需要本机安装 Codex CLI**——没有 auth.json 时用
    ``buddy login codex`` 直接授权即可。
    """
    _ensure_local(request)
    try:
        from ...codex import failover, login
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "codex 通道不可用"}})
    body = await request.json()
    payload = body.get("payload", body.get("content"))
    if payload is None:
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 payload（~/.codex/auth.json 或同构 JSON）"}})
    try:
        await asyncio.to_thread(login.import_cred_payload, payload)
    except login.LoginError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/traepat/refresh-tokens")
async def ui_traepat_refresh_tokens(request: Request):
    """立即补签 traepat 缺失/临期 Token；健康账号不强刷。"""
    _ensure_local(request)
    try:
        from ...trae.pat import refresh_missing_tokens
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "traepat 通道不可用"}})
    return await asyncio.to_thread(refresh_missing_tokens)


@app.post("/ui/api/traepat/import-ide")
async def ui_traepat_import_ide(request: Request):
    """从本机 Trae IDE 登录态一次性导入 Cloud-IDE-JWT 到 traepat。"""
    _ensure_local(request)
    try:
        from ...trae.pat import import_from_ide, refresh_missing_tokens
        from ...trae.pat.config import _PatConfigError
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "traepat 通道不可用"}})
    try:
        res = await asyncio.to_thread(import_from_ide)
    except _PatConfigError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    await asyncio.to_thread(refresh_missing_tokens)
    return res


@app.get("/ui/api/dumate/status")
async def ui_dumate_status(request: Request):
    """DuMate（百度搭子）本地代理状态（供管理页通道面板展示）。

    进程发现纯本地（pgrep/ps）秒回；bceConsole 登录态 + 签到累计走
    :func:`_dumate_account_info_cached`（30s 缓存，防 700ms 级 bce RTT
    拖住面板首渲）。
    """
    _ensure_local(request)
    from ...dumate import discovery

    info = await asyncio.to_thread(discovery.describe_state)
    ep = await asyncio.to_thread(discovery.discover)
    if ep is not None:
        info["base_url"] = ep.base_url
        info["chat_path"] = discovery.PROXY_CHAT_PATH
    info["login_command"] = "buddy login dumate"
    info.update(await asyncio.to_thread(_dumate_account_info_cached))
    return info


# 账号信息短 TTL 缓存：bceConsole RTT 实测 ~700ms（解密 cookie + 一次公网 GET），
# 状态接口又是面板首渲的同步依赖——每 30s 才允许一次真请求，其余命中缓存。
# 登录/签到变更由签到调度器（它走 BenefitsManager 自己的重试）兜住，不依赖这里的时效。
_ACCOUNT_INFO_TTL = 30.0
_account_info_cache: dict[str, Any] = {"ts": 0.0, "info": {}}


def _dumate_account_info_cached() -> dict[str, Any]:
    now = time.monotonic()
    if now - _account_info_cache["ts"] < _ACCOUNT_INFO_TTL:
        return _account_info_cache["info"]
    info = _dumate_account_info()
    _account_info_cache.update(ts=now, info=info)
    return info


def _dumate_account_info() -> dict[str, Any]:
    """bceConsole 登录态 + 签到累计（只读查询，不领取）。

    让面板显示「已登录 · 累计签到 N 分」而非只有布尔态。解密失败（未登录/
    App 未装）时 bceconsole_authenticated=False，签到字段缺省。账号 id 用稳定的
    bceUserId（auth.json 里），作为 dumate 单账号的固定标识。
    """
    from ...dumate import cookies as dumate_cookies

    info: dict[str, Any] = {"bceconsole_authenticated": False}
    auth = dumate_cookies.resolve_bceconsole_auth()
    if auth is None:
        return info
    info["bceconsole_authenticated"] = True

    base = os.path.expanduser(
        "~/Library/Application Support/qianfan-desktop-app"
    )
    try:
        auth_json = json.loads(
            (pathlib.Path(base) / "auth.json").read_text("utf-8")
        )
        info["account_id"] = (
            auth_json.get("accountProfiles") or [{}]
        )[0].get("bceUserId") or ""
        info["display_name"] = (
            auth_json.get("accountProfiles") or [{}]
        )[0].get("displayName") or ""
    except Exception:  # noqa: BLE001 - 账号元信息缺失不影响登录态/签到展示
        pass

    from ...dumate import checkin as dumate_checkin

    # 面板状态接口是高频只读：签到累计只是其中一字段，网络慢/断时给短超时
    # 快速失败（返回 None 字段缺省），别让 700ms+ 的 bce 查询拖住整页首渲
    status = dumate_checkin.fetch_checkin_status(timeout=3.0)
    if status:
        info["checkin_total_points"] = status.get("total_points", 0)
        info["checkin_total_times"] = status.get("total_times", 0)
        # 账号卡的签到列数据源（benefits_accounts.js 的 acctLoad → cfg.subExtra
        # 要读这几个字段）。**直接复用上面这次查询的结果**，不再打第二次上游：
        # 这里是高频只读接口，多一次 bce 往返就是几十到几百毫秒的白等。
        info["checkin_status"] = {
            "checked_in": bool(status.get("checked_in")),
            "claimable": bool(status.get("claimable")),
            "streak_days": status.get("streak_days") or 0,
            "daily_credit": status.get("daily_credit") or 0,
        }

    # 今日消耗（真实流水合计，不是估算）：records/usage 区间查「今天 0 点 → 现在」。
    # 同样短超时快速失败，拿不到就缺省（前端不显示这一位）。limit=1：合计走
    # consumedPoints/totalCount 字段，不需要明细列表。
    import datetime as _dt

    now_dt = _dt.datetime.now()
    now = int(now_dt.timestamp())
    day_start = int(now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    usage = dumate_checkin.fetch_usage_records(
        start_ts=day_start, end_ts=now, page=1, limit=1, timeout=3.0,
    )
    if usage is not None:
        info["today_consumed"] = usage.get("consumed_points", 0.0)
        info["today_calls"] = usage.get("total_count", 0)
    return info


@app.get("/ui/api/dumate/accounts")
async def ui_dumate_accounts(request: Request):
    """dumate 账号列表（多账号卡片数据源，纯本地只读）。

    dumate 是单账号（复用本机 App 登录态），这里返回恰好一个账号条目，走
    benefits_accounts.js 的通用卡片渲染（标题=displayName、副标题=累计签到）。

    **不看本地进程在不在**：账号身份与签到累计全来自 ``auth.json`` + bceConsole
    登录态，和 ``dumate-main-server`` 跑没跑无关。早先这里 ``discover() is None``
    就返回空列表，于是桌面端一关，卡片标题就退化成「本机账号」、签到/今日消耗
    也整块消失（用户 2026-11-02 报障）。
    """
    _ensure_local(request)
    info = await asyncio.to_thread(_dumate_account_info_cached)
    if not info.get("bceconsole_authenticated"):
        return {"enabled": True, "accounts": []}
    return {
        "enabled": True,
        "accounts": [{
            "index": 1,
            "id": info.get("account_id") or "dumate",
            "display_name": info.get("display_name") or "百度搭子",
            "checkin_total_points": info.get("checkin_total_points"),
            "checkin_total_times": info.get("checkin_total_times"),
            # 签到列：勾/未勾 + 连续天数。上游查询失败（未登录/网络）时整个
            # dict 缺省——前端据此显示「状态未知」而不是画成「未签到」。
            "checkin_status": info.get("checkin_status"),
            "today_consumed": info.get("today_consumed"),
            "today_calls": info.get("today_calls"),
        }],
    }


@app.get("/ui/api/dumate/usage-records")
async def ui_dumate_usage_records(
    request: Request, days: int = 7, page: int = 1, limit: int = 20
):
    """DuMate 积分消耗流水（bceConsole records/usage 同源，真实逐笔扣减）。

    暂无 UI 消费方，先以管理接口形式备用（curl 即可查），参数：
    days（默认 7）、page、limit（≤100）。返回 ``{total_count, consumed_points, records[]}``。
    """
    _ensure_local(request)
    from ...dumate import checkin as dumate_checkin

    limit = max(1, min(int(limit or 20), 100))
    page = max(1, int(page or 1))
    days = max(1, min(int(days or 7), 90))

    def _fetch():
        end = int(time.time())
        start = end - days * 86400
        return dumate_checkin.fetch_usage_records(
            start_ts=start, end_ts=end, page=page, limit=limit, timeout=15.0,
        )

    try:
        result = await asyncio.to_thread(_fetch)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail={"error": {"message": str(exc)[:300]}}
        )
    if result is None:
        raise HTTPException(
            status_code=503,
            detail={"error": {"message": "dumate 未登录或消耗流水查询失败"}},
        )
    return result


@app.post("/ui/api/dumate/accounts/delete")
async def ui_dumate_accounts_delete(request: Request):
    """dumate 无多账号 failover，删除是 no-op（返回当前账号列表，前端据此刷新）。"""
    _ensure_local(request)
    return await ui_dumate_accounts(request)


@app.post("/ui/api/dumate/accounts/order")
async def ui_dumate_accounts_order(request: Request):
    """dumate 单账号，重排无意义（no-op，返回当前账号列表）。"""
    _ensure_local(request)
    return await ui_dumate_accounts(request)


@app.get("/ui/api/codebuddy/accounts")
async def ui_codebuddy_accounts(request: Request):
    """codebuddy（CN 区）各账号本地凭证/冷却状态（纯本地不触网，不含秘密）。

    只下发 region=cn 的账号，``index`` 按区内位次——海外版账号由
    ``/ui/api/codebuddyintl/accounts`` 单独下发，两张卡的序号各数各的。
    """
    _ensure_local(request)
    try:
        from ...codebuddy_provider import failover
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "codebuddy 通道不可用"}})
    return await asyncio.to_thread(failover.accounts_status, "cn")


@app.get("/ui/api/codebuddyintl/accounts")
async def ui_codebuddyintl_accounts(request: Request):
    """codebuddyintl（海外版）各账号本地凭证/冷却状态（纯本地不触网，不含秘密）。

    与 CN 卡共用账号 store，按 region=global 过滤；字段与 CN 卡同构，
    前端照 qoderintl 的先例渲染独立的海外版额度卡。
    """
    _ensure_local(request)
    try:
        from ...codebuddy_provider import failover
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "codebuddy 通道不可用"}})
    return await asyncio.to_thread(failover.accounts_status, "global")


@app.post("/ui/api/codebuddy/accounts/order")
async def ui_codebuddy_accounts_order(request: Request):
    """调整 codebuddy 账号的 failover 顺位（管理页上移/下移按钮）。

    提交完整账号 id 顺序列表，重写 index.json 的 priority；返回重排后的账号
    状态（与 GET 同构）。quota 缓存键带 priority（``quota_epoch``），重排后旧
    额度快照自动失效。
    """
    _ensure_local(request)
    try:
        from ...codebuddy_provider import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "codebuddy 通道不可用"}})
    body = await request.json()
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 ids（账号 id 的完整顺序列表）"}})
    region = body.get("region")
    if region is not None and region not in ("cn", "global"):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": f"非法 region: {region!r}"}})
    try:
        if region is None:
            await asyncio.to_thread(creds.reorder_accounts, ids)
        else:
            await asyncio.to_thread(creds.reorder_accounts, ids, region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    from ...codebuddy_provider import failover
    return await asyncio.to_thread(failover.accounts_status, region or "cn")


@app.post("/ui/api/codebuddy/accounts/rename")
async def ui_codebuddy_accounts_rename(request: Request):
    """改一个 codebuddy 账号的本地别名（管理页 ✎）。

    只改 index.json 的 ``alias`` 字段（显示名），原始凭据与顺位不动；
    空串 = 清除别名、回退默认显示名。返回改后的账号状态（与 GET 同构）。
    """
    _ensure_local(request)
    try:
        from ...codebuddy_provider import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "codebuddy 通道不可用"}})
    body = await request.json()
    aid, alias = body.get("id"), body.get("alias")
    if not isinstance(aid, str) or not aid.strip() or not isinstance(alias, str):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id 或 alias（要改的账号 id 与新别名）"}})
    if len(alias.strip()) > 64:
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "别名过长（≤64 字符）"}})
    try:
        accounts = await asyncio.to_thread(creds.rename_account, aid.strip(), alias)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": {"message": str(exc)}})
    # 回包按被改账号自己的 region 下发：海外卡 ✎ 与 CN 卡共用本端点，
    # 回 CN 区列表会让海外卡快照（只含 global 账号）对不上号。
    region = next((a.region for a in accounts if a.id == aid.strip()), "cn")
    from ...codebuddy_provider import failover
    return await asyncio.to_thread(failover.accounts_status, region)


@app.post("/ui/api/codebuddy/accounts/delete")
async def ui_codebuddy_accounts_delete(request: Request):
    """删除一个 codebuddy 账号（索引条目 + cred 文件 + 冷却标记）。

    不再使用或凭据作废的账号从轮换里摘掉——留着每轮 failover 白打一次上游。
    返回删除后的账号状态（与 GET 同构，前端直接重渲染）。
    """
    _ensure_local(request)
    try:
        from ...codebuddy_provider import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "codebuddy 通道不可用"}})
    body = await request.json()
    aid = body.get("id")
    if not isinstance(aid, str) or not aid.strip():
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id（要删除的账号 id）"}})
    aid = aid.strip()
    removed = await asyncio.to_thread(creds.delete_account, aid)
    if not removed:
        raise HTTPException(status_code=404,
                            detail={"error": {"message": f"账号不存在: {aid}"}})
    from ...codebuddy_provider import failover
    await asyncio.to_thread(failover.clear_cooldown, aid)
    return await asyncio.to_thread(failover.accounts_status, "cn")


@app.get("/ui/api/codebuddy/usage-records")
async def ui_codebuddy_usage_records(
    request: Request, days: int = 7, page: int = 1, page_size: int = 20
):
    """CodeBuddy 按请求积分消耗流水（WorkBuddy「使用记录」同源，实扣口径）。

    暂无 UI 消费方，先以管理接口形式备用（curl 即可查），参数：
    days（默认 7）、page、page_size（≤100）。
    """
    _ensure_local(request)
    state = get_state()
    provider = (getattr(state, "providers", {}) or {}).get("codebuddy")
    if provider is None:
        # 未显式启用 codebuddy 通道时回退默认实例（与 BenefitsManager 同口径）
        from buddy_proxy.codebuddy_provider import _default_codebuddy
        provider = _default_codebuddy

    def _fetch():
        end = time.strftime("%Y-%m-%d %H:%M:%S")
        start = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(time.time() - days * 86400))
        return provider.usage_records(start, end, page_num=page, page_size=page_size)

    try:
        return await asyncio.to_thread(_fetch)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": {"message": str(exc)[:300]}})


# ---------------------------------------------------------------------------
# kiro：账号管理 + 导入 auth 文件 + 设备码登录
# ---------------------------------------------------------------------------


def _kiro_state_payload() -> dict[str, Any]:
    """kiro 账号快照（纯本地，不触网）。"""
    from ...kiro import credentials as kiro_creds

    accounts = kiro_creds.list_accounts()
    out = []
    for index, acct in enumerate(accounts, 1):
        cred = kiro_creds.load_account_cred(acct.id) or {}
        expires_at = cred.get("expires_at")
        out.append({
            "id": acct.id,
            "label": acct.label,
            "region": acct.region,
            "provider": acct.provider,
            "index": index,
            "has_refresh_token": bool(cred.get("refresh_token")),
            "expires_at": expires_at,
            "needs_refresh": kiro_creds.needs_refresh(cred) if cred else True,
        })
    return {"enabled": True, "accounts": out}


@app.get("/ui/api/kiro/accounts")
async def ui_kiro_accounts(request: Request):
    """kiro 账号列表（本地凭据状态，不含秘密）。"""
    _ensure_local(request)
    return await asyncio.to_thread(_kiro_state_payload)


@app.get("/ui/api/kiro/auth-files")
async def ui_kiro_auth_files(request: Request):
    """本机可导入的 auth 文件候选（Kiro IDE 的 sso cache）。"""
    _ensure_local(request)
    from ...kiro import login as kiro_login

    return {"files": await asyncio.to_thread(kiro_login.detected_auth_files)}


@app.post("/ui/api/kiro/import")
async def ui_kiro_import(request: Request):
    """导入 auth 文件（body: ``{path?, label?}``），并探活确认可用。"""
    _ensure_local(request)
    from ...kiro import login as kiro_login

    body = await request.json() if await request.body() else {}
    path = (body.get("path") or "").strip() or None
    label = (body.get("label") or "").strip()
    try:
        account = await asyncio.to_thread(
            kiro_login.import_auth_file, path, label=label)
    except Exception as exc:  # noqa: BLE001 - 统一转成可读 400
        raise HTTPException(status_code=400, detail={"error": {
            "message": str(exc)[:300], "type": "import_failed"}}) from exc
    return {"ok": True, "account": {
        "id": account.id, "label": account.label, "region": account.region}}


@app.post("/ui/api/kiro/login/start")
async def ui_kiro_login_start(request: Request):
    """发起设备码登录，返回验证地址与用户码（前端展示后轮询 poll）。"""
    _ensure_local(request)
    from ...kiro import login as kiro_login

    body = await request.json() if await request.body() else {}
    region = (body.get("region") or "").strip() or None
    try:
        result = await asyncio.to_thread(kiro_login.start_device_login,
                                         region or "us-east-1")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail={"error": {
            "message": str(exc)[:300], "type": "login_failed"}}) from exc
    return {"ok": True, **result}


@app.post("/ui/api/kiro/login/poll")
async def ui_kiro_login_poll(request: Request):
    """轮询设备码登录状态（``status``: pending/slow_down/expired/ok）。"""
    _ensure_local(request)
    from ...kiro import login as kiro_login

    try:
        return await asyncio.to_thread(kiro_login.poll_device_login)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail={"error": {
            "message": str(exc)[:300], "type": "login_failed"}}) from exc


@app.post("/ui/api/kiro/accounts/order")
async def ui_kiro_accounts_order(request: Request):
    """调整 kiro 账号的 failover 顺位。"""
    _ensure_local(request)
    from ...kiro import credentials as kiro_creds

    body = await request.json()
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=400, detail={"error": {
            "message": "ids 必须是账号 id 的字符串数组"}})
    try:
        accounts = await asyncio.to_thread(kiro_creds.reorder_accounts, ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {
            "message": str(exc)}}) from exc
    return {"ok": True, "accounts": [
        {"id": a.id, "label": a.label} for a in accounts]}


@app.delete("/ui/api/kiro/accounts/{account_id}")
async def ui_kiro_account_delete(account_id: str, request: Request):
    """删除 kiro 账号（不可逆：凭据文件一并删除，需重新登录）。"""
    _ensure_local(request)
    from ...kiro import credentials as kiro_creds

    removed = await asyncio.to_thread(kiro_creds.delete_account, account_id)
    if not removed:
        raise HTTPException(status_code=404, detail={"error": {
            "message": f"账号不存在: {account_id}"}})
    return {"ok": True}


@app.post("/ui/api/kiro/accounts/rename")
async def ui_kiro_account_rename(request: Request):
    """改 kiro 账号显示名（只改索引里的 label，不动凭据）。"""
    _ensure_local(request)
    from ...kiro import credentials as kiro_creds

    body = await request.json()
    account_id = str(body.get("id") or "").strip()
    alias = str(body.get("alias") or "").strip()
    if not account_id:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 id"}})
    try:
        accounts = await asyncio.to_thread(
            kiro_creds.rename_account, account_id, alias)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": {
            "message": str(exc)}}) from exc
    return {"ok": True, "accounts": [
        {"id": a.id, "label": a.label, "alias": a.label} for a in accounts]}


# ---------------------------------------------------------------------------
# copilot（GitHub Copilot 订阅通道）
# ---------------------------------------------------------------------------

def _copilot_modules():
    try:
        from ...copilot import credentials as creds
        from ...copilot import failover
    except Exception:  # noqa: BLE001 - 通道文件缺失/导入失败
        raise HTTPException(status_code=503,
                            detail={"error": {"message": "copilot 通道不可用"}}) from None
    return creds, failover


@app.get("/ui/api/copilot/accounts")
async def ui_copilot_accounts(request: Request):
    """copilot 各账号本地状态（纯本地，不触网，不含 token）。"""
    _ensure_local(request)
    _creds, failover = _copilot_modules()
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/copilot/accounts/order")
async def ui_copilot_accounts_order(request: Request):
    """调整 copilot 账号 failover 顺位（提交完整 id 顺序）。"""
    _ensure_local(request)
    creds, failover = _copilot_modules()
    body = await request.json()
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 ids（账号 id 的完整顺序列表）"}})
    try:
        await asyncio.to_thread(creds.reorder_accounts, ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/copilot/accounts/delete")
async def ui_copilot_accounts_delete(request: Request):
    """删除一个 copilot 账号（索引条目 + cred 文件 + 冷却标记）。"""
    _ensure_local(request)
    creds, failover = _copilot_modules()
    body = await request.json()
    aid = body.get("id")
    if not isinstance(aid, str) or not aid.strip():
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id（要删除的账号 id）"}})
    aid = aid.strip()
    removed = await asyncio.to_thread(creds.delete_account, aid)
    if not removed:
        raise HTTPException(status_code=404,
                            detail={"error": {"message": f"账号不存在: {aid}"}})
    await asyncio.to_thread(failover.clear_cooldown, aid)
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/copilot/accounts/rename")
async def ui_copilot_accounts_rename(request: Request):
    """改一个 copilot 账号的本地别名（只改显示名，顺位与凭据不动）。"""
    _ensure_local(request)
    creds, failover = _copilot_modules()
    body = await request.json()
    aid, alias = body.get("id"), body.get("alias")
    if not isinstance(aid, str) or not aid.strip() or not isinstance(alias, str):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id 或 alias"}})
    if len(alias.strip()) > 64:
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "别名过长（≤64 字符）"}})
    try:
        await asyncio.to_thread(creds.rename_account, aid.strip(), alias)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": {"message": str(exc)}})
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/copilot/accounts/import")
async def ui_copilot_accounts_import(request: Request):
    """导入一把 GitHub token（面板「导入账号」入口，与 CLI 共用落盘逻辑）。

    body ``{"token": "gho_..."}``。token 只经 ``/user`` 探测身份后落盘 0600，
    不回显、不写日志。**不走 keychain**：能拿到 token 的受支持路径只有
    ``gh auth token``（``buddy login copilot --gh``）或这里手工粘贴。
    """
    _ensure_local(request)
    creds, failover = _copilot_modules()
    body = await request.json()
    token = body.get("token", body.get("access_token"))
    token = token.strip() if isinstance(token, str) else ""
    if not token:
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 token（GitHub token，gho_ 开头）"}})
    if not creds.is_gho_token(token):
        raise HTTPException(status_code=400, detail={"error": {
            "message": "这不像 GitHub token（应以 gho_/ghp_/ghu_ 开头）"}})
    try:
        await asyncio.to_thread(creds.save_token, token, source="panel")
    except creds.AuthError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}}) from exc
    return await asyncio.to_thread(failover.accounts_status)


@app.get("/ui/api/copilot/gh-accounts")
async def ui_copilot_gh_accounts(request: Request):
    """本机 gh CLI 已登录的 GitHub 账号（供面板「从 gh 导入」直接点选）。

    只解析 ``gh auth status`` 的账号名，不读 token。
    """
    _ensure_local(request)
    creds, _failover = _copilot_modules()
    return {"accounts": await asyncio.to_thread(creds.gh_accounts)}


@app.post("/ui/api/copilot/accounts/import-gh")
async def ui_copilot_accounts_import_gh(request: Request):
    """用 ``gh auth token --user`` 导入一个（或全部）本机已登录账号。"""
    _ensure_local(request)
    creds, failover = _copilot_modules()
    body = await request.json() if await request.body() else {}
    user = body.get("user") if isinstance(body, dict) else None
    users = [user] if isinstance(user, str) and user.strip() else await asyncio.to_thread(
        creds.gh_accounts)
    if not users:
        raise HTTPException(status_code=400, detail={"error": {
            "message": "本机 gh CLI 没有已登录的账号：请先 `gh auth login`，"
                       "或在下方直接粘贴 GitHub token"}})
    errors: list[str] = []
    for name in users:
        try:
            await asyncio.to_thread(creds.save_token,
                                    await asyncio.to_thread(creds.gh_token, name),
                                    source=f"gh:{name}")
        except creds.AuthError as exc:
            errors.append(f"{name}: {exc}")
    status = await asyncio.to_thread(failover.accounts_status)
    if errors:
        status["errors"] = errors
    return status


@app.post("/ui/api/copilot/models/refresh")
async def ui_copilot_models_refresh(request: Request):
    """强制从上游刷新 copilot 模型目录（管理页「刷新模型」按钮）。"""
    _ensure_local(request)
    provider = getattr(get_state(), "providers", {}).get("copilot")
    if provider is None:
        raise HTTPException(status_code=503,
                            detail={"error": {"message": "copilot 通道未启用"}})
    count = await provider.refresh_models(force=True)
    return {"ok": True, "count": count, "models": list(provider.models()),
            "error": provider.health().get("catalog_error", "")}


# ---------------------------------------------------------------------------
# 商汤小浣熊（raccoon）账号管理
# ---------------------------------------------------------------------------
#
# 与其它多账号通道相比，这里多两个入口：**手机号+密码登录**与**桌面登录态
# 复用**。原因是小浣熊没有 OAuth 设备码流程——网页端只有手机号+密码，
# 会话 JWT 约 30 天到期。只做「粘 token」的话，多账号每月都要人工换一遍；
# 存了密码就能自动续期（见 raccoon.login.relogin_account）。


@app.get("/ui/api/raccoon/accounts")
async def ui_raccoon_accounts(request: Request):
    """raccoon 各账号本地凭据/冷却状态（纯本地，不触网，不含秘密）。

    每条带 ``auto_renew``：有手机号+密码 = True，表示 token 过期会自动重新
    登录；False 则到期后需要用户重新导入。面板据此显示不同提示。
    """
    _ensure_local(request)
    try:
        from ...raccoon import failover
    except Exception:
        raise HTTPException(status_code=503,
                            detail={"error": {"message": "raccoon 通道不可用"}})
    status = await asyncio.to_thread(failover.accounts_status)
    # 桌面客户端登录态是否可用（只读探测，供「从桌面端导入」按钮显隐）
    try:
        from ...raccoon.login import desktop_auth_path

        status["desktop_available"] = desktop_auth_path() is not None
    except Exception:  # noqa: BLE001 - 探测失败不影响面板
        status["desktop_available"] = False
    return status


@app.post("/ui/api/raccoon/accounts/import")
async def ui_raccoon_accounts_import(request: Request):
    """导入一个 raccoon 账号。支持三种输入：

    * ``{"session": "<xhx_token 或整段 cookie>"}`` —— 粘会话；
    * ``{"phone": "...", "password": "..."}`` —— 手机号+密码（可自动续期）；
    * ``{"from_desktop": true}`` —— 读本机桌面客户端登录态（只读）。

    ``password`` 加密落盘、不回显、不导出（见 ``raccoon.login``）。
    """
    _ensure_local(request)
    try:
        from ...raccoon import login as raccoon_login
        from ...raccoon.credentials import AuthError
    except Exception:
        raise HTTPException(status_code=503,
                            detail={"error": {"message": "raccoon 通道不可用"}})
    body = await request.json()
    from_desktop = bool(body.get("from_desktop"))
    session = str(body.get("session") or "").strip()
    phone = str(body.get("phone") or "").strip()
    password = str(body.get("password") or "")

    try:
        if from_desktop:
            data = await asyncio.to_thread(raccoon_login.read_desktop_auth)
            if not data:
                raise HTTPException(status_code=400, detail={"error": {
                    "message": "没找到本机小浣熊桌面客户端的登录态"
                               "（请先在客户端登录，或改用粘 token / 手机号密码）"}})
            token = raccoon_login._parse_session_blob(json.dumps(data, ensure_ascii=False))
            if not token:
                raise HTTPException(status_code=400, detail={"error": {
                    "message": "桌面端登录态里没有可用的会话 token"}})
            ref = await asyncio.to_thread(raccoon_login.import_session, token)
        elif phone and password:
            result = await asyncio.to_thread(
                raccoon_login.login_with_password, phone, password)
            token = result["access_token"]
            info = await asyncio.to_thread(raccoon_login.fetch_user_info, token)
            nick = str(info.get("nickname") or info.get("screen_name") or "").strip()
            ref = await asyncio.to_thread(
                raccoon_login.import_session, token,
                phone=phone, password=password, nickname=nick or phone)
        elif session:
            ref = await asyncio.to_thread(raccoon_login.import_session, session)
        else:
            raise HTTPException(status_code=400, detail={"error": {
                "message": "请提供 session（xhx_token）、phone+password，"
                           "或 from_desktop=true"}})
    except AuthError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})

    from ...raccoon import failover

    status = await asyncio.to_thread(failover.accounts_status)
    status["imported"] = {"id": ref.id, "name": ref.name or ref.id}
    return status


@app.post("/ui/api/raccoon/accounts/order")
async def ui_raccoon_accounts_order(request: Request):
    """调整 raccoon 账号的 failover 顺位（管理页上移/下移按钮）。"""
    _ensure_local(request)
    try:
        from ...raccoon import credentials as creds
    except Exception:
        raise HTTPException(status_code=503,
                            detail={"error": {"message": "raccoon 通道不可用"}})
    body = await request.json()
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=400, detail={
            "error": {"message": "缺少 ids（账号 id 的完整顺序列表）"}})
    try:
        await asyncio.to_thread(creds.reorder_accounts, ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    from ...raccoon import failover

    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/raccoon/accounts/delete")
async def ui_raccoon_accounts_delete(request: Request):
    """删除一个 raccoon 账号（索引条目 + cred 文件 + 冷却标记）。"""
    _ensure_local(request)
    try:
        from ...raccoon import credentials as creds
    except Exception:
        raise HTTPException(status_code=503,
                            detail={"error": {"message": "raccoon 通道不可用"}})
    body = await request.json()
    aid = body.get("id")
    if not isinstance(aid, str) or not aid.strip():
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id（要删除的账号 id）"}})
    aid = aid.strip()
    removed = await asyncio.to_thread(creds.delete_account, aid)
    if not removed:
        raise HTTPException(status_code=404,
                            detail={"error": {"message": f"账号不存在: {aid}"}})
    from ...raccoon import failover

    await asyncio.to_thread(failover.clear_cooldown, aid)
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/raccoon/accounts/rename")
async def ui_raccoon_accounts_rename(request: Request):
    """改一个 raccoon 账号的本地别名（管理页 ✎）。"""
    _ensure_local(request)
    try:
        from ...raccoon import credentials as creds
    except Exception:
        raise HTTPException(status_code=503,
                            detail={"error": {"message": "raccoon 通道不可用"}})
    body = await request.json()
    aid = str(body.get("id") or "").strip()
    alias = str(body.get("alias") or "").strip()
    if not aid:
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id"}})
    try:
        await asyncio.to_thread(creds.rename_account, aid, alias)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    from ...raccoon import failover

    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/raccoon/accounts/relogin")
async def ui_raccoon_accounts_relogin(request: Request):
    """用落盘的手机号+密码强制重新登录一个账号（手动续期按钮）。

    正常路径不需要它——token 过期时转发会自动重登。这个入口给「想立刻
    刷新」或「自动重登失败后手动再试一次」用。
    """
    _ensure_local(request)
    try:
        from ...raccoon import login as raccoon_login
        from ...raccoon.credentials import AuthError
    except Exception:
        raise HTTPException(status_code=503,
                            detail={"error": {"message": "raccoon 通道不可用"}})
    body = await request.json()
    aid = str(body.get("id") or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 id"}})
    try:
        await asyncio.to_thread(raccoon_login.relogin_account, aid)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    from ...raccoon import failover

    await asyncio.to_thread(failover.clear_cooldown, aid)
    return await asyncio.to_thread(failover.accounts_status)


@app.get("/ui/api/runninghub/accounts")
async def ui_runninghub_accounts(request: Request):
    _ensure_local(request)
    from ...runninghub import store as rh_store

    return {"accounts": rh_store.list_accounts(include_secret=False)}


@app.post("/ui/api/runninghub/accounts/import")
async def ui_runninghub_accounts_import(request: Request):
    """导入账号：**邮箱 + 密码**（走站内，能花过渡币）；api_key 可选（纯 OpenAPI 备用）。

    导入即登录一次，拿到的登录态和站内余额一起落盘；登录失败直接把上游原文
    抛出去（密码错 / 要验证码），不写入半条记录。
    """
    _ensure_local(request)
    from ...runninghub import store as rh_store
    from ...runninghub import websession
    from ...runninghub.runner import WebAccountSession, refresh_balance

    body = await request.json()
    email = str(body.get("email") or "").strip()
    password = str(body.get("password") or "")
    api_key = str(body.get("api_key") or "").strip() or None
    label = str(body.get("label") or "").strip() or None
    if not email and not api_key:
        raise HTTPException(status_code=400, detail={"error": {"message": "email/password 或 api_key 至少要有一个"}})
    if email and not password:
        raise HTTPException(status_code=400, detail={"error": {"message": "邮箱账号必须填密码"}})

    if not email:
        # 纯 OpenAPI 账号（旧模式）：只验 key 能不能查余额
        from ...runninghub import client as rh_client
        try:
            info = await rh_client.fetch_account_status(api_key)
        except rh_client.RunningHubError as exc:
            raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
        acc = rh_store.upsert_account(api_key=api_key, label=label, account_info=info)
        return {"accounts": rh_store.list_accounts(include_secret=False)}

    try:
        session = await websession.login(email, password)
    except websession.WebSessionError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": f"登录失败：{exc}"}})

    if not label:
        label = email.split("@")[0]
    acc = rh_store.upsert_account(email=email, password=password, api_key=api_key, label=label)
    rh_store.update_session(acc["account_id"], session.to_dict())
    # 站内余额（过渡币/积分/钱包）——拉失败不阻塞导入
    try:
        balance = await refresh_balance(
            WebAccountSession(rh_store.get_account(acc["account_id"]), persist=rh_store.update_session),
            include_welfare=True,
        )
        rh_store.update_account_info(acc["account_id"], balance)
    except websession.WebSessionError:
        pass
    return {"accounts": rh_store.list_accounts(include_secret=False)}


@app.post("/ui/api/runninghub/accounts/refresh")
async def ui_runninghub_accounts_refresh(request: Request):
    """刷新额度：邮箱账号拉站内余额（顺带续登录态）；纯 key 账号走 accountStatus。"""
    _ensure_local(request)
    from ...runninghub import client as rh_client
    from ...runninghub import store as rh_store
    from ...runninghub import websession
    from ...runninghub.runner import WebAccountSession, refresh_balance

    body = await request.json() if (await request.body()) else {}
    target_id = str(body.get("account_id") or "").strip()

    async def _refresh_one(acc: dict) -> None:
        aid = acc.get("account_id")
        try:
            if acc.get("email") and acc.get("password"):
                # 账号卡刷新才拉免费次数（welfare/summary）；submit 前不拉，省一个往返
                balance = await refresh_balance(
                    WebAccountSession(acc, persist=rh_store.update_session), include_welfare=True
                )
                rh_store.update_account_info(aid, balance)
            elif acc.get("api_key"):
                info = await rh_client.fetch_account_status(acc["api_key"])
                rh_store.update_account_info(aid, info)
        except (rh_client.RunningHubError, websession.WebSessionError):
            pass

    targets = [a for a in rh_store.get_active_accounts() if not target_id or a.get("account_id") == target_id]
    # 并行：5 个号串行刷要 5 × 5 个往返，gather 一下快 5 倍；update_account_info 是原子写 + flock
    await asyncio.gather(*(_refresh_one(a) for a in targets))
    return {"accounts": rh_store.list_accounts(include_secret=False)}


@app.get("/ui/api/runninghub/daily-gift")
async def ui_runninghub_daily_gift_status(request: Request):
    """自动领积分的状态：循环在不在跑、每个号站点今天领没领、下次重试时间。"""
    _ensure_local(request)
    from ...runninghub import dailygift

    return {"gift": dailygift.get_manager().status()}


@app.post("/ui/api/runninghub/daily-gift/claim")
async def ui_runninghub_daily_gift_claim(request: Request):
    """手动「立即领取」：不走退避、已领过也照问一次上游；不传 account_id 就是全部。"""
    _ensure_local(request)
    from ...runninghub import dailygift
    from ...runninghub import store as rh_store

    body = await request.json() if (await request.body()) else {}
    aid = str(body.get("account_id") or "").strip() or None
    try:
        out = await dailygift.get_manager().claim_now(aid)
    except Exception as exc:  # noqa: BLE001 — 把上游原文带回前端，别 500
        raise HTTPException(status_code=502, detail={"error": {"message": str(exc)[:300]}})
    return {
        "claimed": out["claimed"],
        "already": out["already"],
        "failed": out["failed"],
        "gift": dailygift.get_manager().status(),
        "accounts": rh_store.list_accounts(include_secret=False),
    }


async def _rh_start_daily_gift_loop() -> None:
    """自动领积分循环随应用启停（uvicorn 生命周期）。启动失败只记日志，不阻断代理。"""
    try:
        from ...runninghub import dailygift
        dailygift.get_manager().start()
    except Exception as exc:  # noqa: BLE001
        log.warning("runninghub 每日积分自动领取循环启动失败: %s", exc)


async def _rh_stop_daily_gift_loop() -> None:
    try:
        from ...runninghub import dailygift
        await dailygift.get_manager().stop()
    except Exception:  # noqa: BLE001
        pass


app.router.on_startup.append(_rh_start_daily_gift_loop)
app.router.on_shutdown.append(_rh_stop_daily_gift_loop)


@app.post("/ui/api/runninghub/accounts/relogin")
async def ui_runninghub_accounts_relogin(request: Request):
    """强制重新登录（用户在网页改了密码 / 登录态坏了）。"""
    _ensure_local(request)
    from ...runninghub import store as rh_store
    from ...runninghub import websession
    from ...runninghub.runner import WebAccountSession

    body = await request.json()
    aid = str(body.get("account_id") or "").strip()
    acc = rh_store.get_account(aid)
    if not acc or not (acc.get("email") and acc.get("password")):
        raise HTTPException(status_code=404, detail={"error": {"message": "账号不存在或不是邮箱账号"}})
    try:
        session = await websession.login(acc["email"], acc["password"])
        rh_store.update_session(aid, session.to_dict())
    except websession.WebSessionError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": f"登录失败：{exc}"}})
    return {"accounts": rh_store.list_accounts(include_secret=False)}


@app.post("/ui/api/runninghub/accounts/order")
async def ui_runninghub_accounts_order(request: Request):
    _ensure_local(request)
    from ...runninghub import store as rh_store

    body = await request.json()
    order = body.get("order")
    if not isinstance(order, list):
        raise HTTPException(status_code=400, detail={"error": {"message": "order 必须是数组"}})
    return {"accounts": rh_store.reorder_accounts([str(x) for x in order])}


@app.post("/ui/api/runninghub/accounts/rename")
async def ui_runninghub_accounts_rename(request: Request):
    _ensure_local(request)
    from ...runninghub import store as rh_store

    body = await request.json()
    aid = str(body.get("account_id") or "").strip()
    label = str(body.get("label") or "").strip()
    if not aid or not label:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 account_id 或 label"}})
    if not rh_store.rename_account(aid, label):
        raise HTTPException(status_code=404, detail={"error": {"message": "账号不存在"}})
    return {"accounts": rh_store.list_accounts(include_secret=False)}


@app.post("/ui/api/runninghub/accounts/delete")
async def ui_runninghub_accounts_delete(request: Request):
    _ensure_local(request)
    from ...runninghub import store as rh_store

    body = await request.json()
    aid = str(body.get("account_id") or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 account_id"}})
    if not rh_store.delete_account(aid):
        raise HTTPException(status_code=404, detail={"error": {"message": "账号不存在"}})
    return {"accounts": rh_store.list_accounts(include_secret=False)}


@app.get("/ui/api/runninghub/workflows")
async def ui_runninghub_workflows(request: Request):
    _ensure_local(request)
    from ...runninghub import store as rh_store

    return {"workflows": rh_store.list_workflows()}


@app.post("/ui/api/runninghub/workflows/import")
async def ui_runninghub_workflows_import(request: Request):
    """导入 AI 应用：优先用登录态拿 ``/api/webapp/detail``（inputNodes 带 fieldType）。

    这个接口**不需要登录**就能拿 schema，所以没有账号也能导入；但闭源节点判断
    （``closedSource``）要靠价格预估，那一步需要登录态——有账号就顺手做，没有就
    标「未知」，等运行时再学。
    """
    _ensure_local(request)
    from ...runninghub import store as rh_store
    from ...runninghub import websession
    from ...runninghub.runner import WebAccountSession, closed_source_nodes

    body = await request.json()
    webapp_id = str(body.get("webapp_id") or "").strip()
    custom_name = str(body.get("name") or "").strip()
    if not webapp_id:
        raise HTTPException(status_code=400, detail={"error": {"message": "webapp_id 不能为空"}})

    # schema：公开接口，不带 token 也行
    try:
        data = await websession._post("/api/webapp/detail", {"webappId": webapp_id})
    except websession.WebSessionError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": f"获取应用失败：{exc}"}})
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    if not inner:
        raise HTTPException(status_code=404, detail={"error": {"message": f"应用不存在: {webapp_id}"}})
    info: dict = {
        "webappId": webapp_id,
        "name": inner.get("name") or f"ai-app-{webapp_id}",
        "description": inner.get("description") or "",
        "nodeInfoList": list(inner.get("inputNodes") or []),
        "instanceType": inner.get("instanceType"),
        "avgRunningSeconds": inner.get("avgRunningSeconds"),
        "runningSuccessRate": inner.get("runningSuccessRate"),
        "runMode": "web",
        "billingType": "过渡币（站内）",
    }
    if custom_name:
        info["name"] = custom_name

    # 有账号就顺手预估一次（免费）：能看出闭源节点和参考价
    accounts = [a for a in rh_store.get_active_accounts() if a.get("email") and a.get("password")]
    if accounts:
        try:
            acc = WebAccountSession(accounts[0], persist=rh_store.update_session)
            inputs = websession.normalize_inputs(info["nodeInfoList"])
            est, _ = await acc.call(lambda t: websession.price_estimate(webapp_id, inputs, t))
            info["estimate"] = {
                "totalPrice": est.get("totalPrice"), "currency": est.get("currency"),
                "priceText": est.get("priceText"), "estimatedPoint": est.get("estimatedPoint"),
                "estimatedTransitionCoin": est.get("estimatedTransitionCoin"),
                "nodes": [n.get("classType") for n in (est.get("nodes") or []) if isinstance(n, dict)],
            }
            closed = closed_source_nodes(est)
            # 预估 nodes 为空 → ["?"]：实测闭源应用就是这样，但开源应用偶尔也为空，
            # 所以只在「明确列出闭源节点」时才标死，"?" 留给运行时学。
            if any(c != "?" for c in closed):
                info["closedSource"] = True
                info["billingType"] = "钱包余额（闭源）"
        except websession.WebSessionError:
            pass

    if body.get("preview"):
        return {"workflow": info}
    old = next((w for w in rh_store.list_workflows() if str(w.get("webappId")) == webapp_id), None)
    if old:
        for k in ("verifiedCost", "verifiedNote", "outputType", "closedSource", "verifiedAt"):
            if old.get(k) is not None and k not in info:
                info[k] = old[k]
    rh_store.upsert_workflow(info)
    return {"workflows": rh_store.list_workflows()}


@app.post("/ui/api/runninghub/workflows/delete")
async def ui_runninghub_workflows_delete(request: Request):
    _ensure_local(request)
    from ...runninghub import store as rh_store

    body = await request.json()
    webapp_id = str(body.get("webapp_id") or "").strip()
    if not webapp_id:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 webapp_id"}})
    if not rh_store.delete_workflow(webapp_id):
        raise HTTPException(status_code=404, detail={"error": {"message": "工作流不存在"}})
    return {"workflows": rh_store.list_workflows()}


@app.post("/ui/api/runninghub/workflows/rename")
async def ui_runninghub_workflows_rename(request: Request):
    _ensure_local(request)
    from ...runninghub import store as rh_store

    body = await request.json()
    webapp_id = str(body.get("webapp_id") or "").strip()
    name = str(body.get("name") or "").strip()
    billing_type = body.get("billing_type")
    if not webapp_id or not name:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 webapp_id 或 name"}})
    if not rh_store.rename_workflow(
        webapp_id,
        name,
        str(billing_type).strip() if billing_type is not None else None,
    ):
        raise HTTPException(status_code=404, detail={"error": {"message": "工作流不存在"}})
    return {"workflows": rh_store.list_workflows()}


@app.post("/ui/api/runninghub/workflows/run")
async def ui_runninghub_workflows_run(request: Request):
    """站内运行一个 AI 应用：登录态 → 预估 → ``/task/webapp/create`` → 轮询 → 取结果。

    走的是**网页端同一条路**，花的是站内过渡币 / 积分。OpenAPI 那条路（要 api_key +
    钱包余额）实测对这个账号是 605/821，所以不再作为主路径；``runMode="api"`` 时
    才走 ``provider`` 那套，留给以后有钱包余额的号。
    """
    _ensure_local(request)
    from ...runninghub import runner as rh_runner
    from ...runninghub import store as rh_store
    from ...runninghub import websession

    body = await request.json()
    webapp_id = str(body.get("webapp_id") or "").strip()
    node_info_list = body.get("nodeInfoList")
    estimate_only = bool(body.get("estimate_only"))
    if not webapp_id:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 webapp_id"}})
    if not isinstance(node_info_list, list):
        raise HTTPException(status_code=400, detail={"error": {"message": "nodeInfoList 必须是数组"}})

    accounts = [a for a in rh_store.get_active_accounts() if a.get("email") and a.get("password")]
    if not accounts:
        raise HTTPException(status_code=400, detail={"error": {"message": "请先导入一个 RunningHub 邮箱账号（需要账号密码才能走站内运行）"}})
    acc = rh_runner.WebAccountSession(accounts[0], persist=rh_store.update_session)
    aid = accounts[0].get("account_id", "")
    wf = next((w for w in rh_store.list_workflows() if str(w.get("webappId")) == webapp_id), None)

    # 顺手刷余额（登录态在 ensure 里续过已经写回）
    try:
        balance = await rh_runner.refresh_balance(acc)
        rh_store.update_account_info(aid, balance)
    except websession.WebSessionError:
        balance = {}

    try:
        res = await rh_runner.run_workflow(acc, webapp_id, node_info_list, estimate_only=estimate_only)
    except websession.WebSessionError as exc:
        # 任务失败：把 taskResultDesc 里的 exception_message 挖出来（之前只显示 FAILED）
        msg = str(exc)
        status_code = 400 if exc.kind == "login" else 502
        closed = False
        if "605" in msg or "NOT_ENOUGH_BALANCE" in msg or "balance is insufficient" in msg.lower():
            closed = True
            msg = ("该应用含闭源模型节点（Seedance/Kling/MAGI 等），站内也只扣钱包余额，过渡币跑不了；"
                   f"上游原文：{msg}")
            status_code = 402
        if wf and closed and not wf.get("closedSource"):
            updated = dict(wf)
            updated["closedSource"] = True
            updated["billingType"] = "钱包余额（闭源）"
            updated["billingLearnedError"] = msg[:160]
            rh_store.upsert_workflow(updated)
        raise HTTPException(
            status_code=status_code,
            detail={"error": {"message": msg}, "workflows": rh_store.list_workflows()},
        )

    # 成功：记下实扣（如果有余额前后差）+ 清掉旧的拒绝上下文
    if not estimate_only and wf:
        updated = dict(wf)
        updated.pop("billingLearnedFrom", None)
        updated.pop("billingLearnedError", None)
        updated["closedSource"] = False
        updated["lastRun"] = {"taskId": res.task_id, "at": int(asyncio.get_running_loop().time())}
        rh_store.upsert_workflow(updated)

    out = {
        "taskId": res.task_id,
        "status": res.status,
        "outputs": res.outputs,
        "estimate": {
            "totalPrice": res.estimate.get("totalPrice"),
            "currency": res.estimate.get("currency"),
            "priceText": res.estimate.get("priceText"),
            "estimatedPoint": res.estimate.get("estimatedPoint"),
            "nodes": [n.get("classType") for n in (res.estimate.get("nodes") or []) if isinstance(n, dict)],
        },
        "closedSourceNodes": rh_runner.closed_source_nodes(res.estimate),
        "balance": balance,
        "workflows": rh_store.list_workflows(),
    }
    return out


def _rh_email_account():
    """拿第一个有邮箱密码的账号；没有就抛 400（站内运行必须有登录态）。"""
    from ...runninghub import store as rh_store

    accounts = [a for a in rh_store.get_active_accounts() if a.get("email") and a.get("password")]
    if not accounts:
        raise HTTPException(status_code=400, detail={"error": {"message": "请先导入一个 RunningHub 邮箱账号（需要账号密码才能走站内运行）"}})
    return accounts[0]


def _new_local_task_id() -> str:
    import time as _t
    import uuid as _u
    return f"local-{int(_t.time() * 1000)}-{_u.uuid4().hex[:6]}"


# 启动时把上一进程留下的 SUBMITTING / RUNNING 标成 INTERRUPTED，别让 UI 永远转圈。
# 放在 import 时跑而不是 on_event("startup")：后者 FastAPI 已标 deprecated（-W error 直接炸），
# 而 reap 只是同步文件 I/O，本模块每个进程只 import 一次，此刻 _running 必然为空，语义正好。
# 注意：测试套件 import 这个模块时跑的是**真实**的 tasks 路径（fixture 的 env 还没生效）——
# 只会把上一进程留下的进行中记录标成 INTERRUPTED，不会误伤（此时没有在跑的）。
try:
    from ...runninghub import runner as _rh_runner_for_reap
    _rh_runner_for_reap.reap_orphans()
except Exception:  # noqa: BLE001 — 辅助功能不能炸掉整个 UI 模块
    pass


@app.post("/ui/api/runninghub/tasks/submit")
async def ui_runninghub_tasks_submit(request: Request):
    """提交一个站内任务到队列，**立即返回**本地 taskId；后台执行器负责推进状态。

    以前 ``workflows/run`` 是同步阻塞到出图（70s 视频 250s+），弹窗只能干等。现在
    UI 提交后直接去队列页看进度，关掉页面任务也照跑（进程内）。
    """
    _ensure_local(request)
    from ...runninghub import runner as rh_runner
    from ...runninghub import store as rh_store
    from ...runninghub import websession

    body = await request.json()
    webapp_id = str(body.get("webapp_id") or "").strip()
    node_info_list = body.get("nodeInfoList")
    if not webapp_id:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 webapp_id"}})
    if not isinstance(node_info_list, list):
        raise HTTPException(status_code=400, detail={"error": {"message": "nodeInfoList 必须是数组"}})
    account = _rh_email_account()
    wf = next((w for w in rh_store.list_workflows() if str(w.get("webappId")) == webapp_id), None)

    # 先刷一次余额，拿「提交前」的过渡币数，完成后相减就是实扣
    balance_before: float | None = None
    try:
        acc = rh_runner.WebAccountSession(account, persist=rh_store.update_session)
        bal = await rh_runner.refresh_balance(acc)
        rh_store.update_account_info(account["account_id"], bal)
        balance_before = bal.get("transitionCoins")
    except websession.WebSessionError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": f"登录态不可用：{exc}"}})

    task_id = _new_local_task_id()
    # 必须是 unix 秒，不是 loop.time()——后者是进程启动起的单调时钟（4 万多的数），
    # UI 的 fmtWhen 拿它 new Date(ts*1000) 会显示成 1970 年
    now = time.time()
    rh_store.upsert_task({
        "taskId": task_id,
        "status": "SUBMITTING",
        "webappId": webapp_id,
        "webappName": (wf or {}).get("name") or webapp_id,
        "accountId": account["account_id"],
        "inputs": rh_runner.websession.normalize_inputs(node_info_list),
        "created_at": now,
        "upstreamTaskId": None,
    })
    # 后台跑；进程重启会丢——模块 import 时 reap_orphans() 把这类标成 INTERRUPTED
    asyncio.create_task(rh_runner.execute_task(
        account, task_id, persist_session=rh_store.update_session, balance_before=balance_before,
    ))
    return {"task": rh_store.get_task(task_id), "balance": bal}


@app.get("/ui/api/runninghub/tasks")
async def ui_runninghub_tasks(request: Request):
    """任务列表（新的在前）。``?limit=N`` 限制条数，默认 100。"""
    _ensure_local(request)
    from ...runninghub import store as rh_store

    try:
        limit = int(request.query_params.get("limit") or 100)
    except ValueError:
        limit = 100
    return {"tasks": rh_store.list_tasks(limit=limit)}


@app.get("/ui/api/runninghub/tasks/{task_id}")
async def ui_runninghub_task_detail(request: Request, task_id: str):
    _ensure_local(request)
    from ...runninghub import store as rh_store

    t = rh_store.get_task(task_id)
    if not t:
        raise HTTPException(status_code=404, detail={"error": {"message": "任务不存在"}})
    return {"task": t}


@app.post("/ui/api/runninghub/tasks/cancel")
async def ui_runninghub_tasks_cancel(request: Request):
    """取消等待。RH 没给网页端取消接口，所以只是「不再等它」，已扣的币不会退。"""
    _ensure_local(request)
    from ...runninghub import runner as rh_runner
    from ...runninghub import store as rh_store

    body = await request.json()
    tid = str(body.get("task_id") or "").strip()
    t = rh_store.get_task(tid) if tid else None
    if not t:
        raise HTTPException(status_code=404, detail={"error": {"message": "任务不存在"}})
    if t.get("status") in ("SUCCESS", "FAILED", "TIMEOUT", "CANCELLED"):
        return {"task": t}
    rh_runner.request_cancel(tid)
    # 执行器下一轮轮询会看到标记并写 CANCELLED；这里先把意图落下来，UI 立刻能反馈
    rh_store.update_task(tid, cancelRequested=True)
    return {"task": rh_store.get_task(tid)}


@app.post("/ui/api/runninghub/tasks/delete")
async def ui_runninghub_tasks_delete(request: Request):
    _ensure_local(request)
    from ...runninghub import store as rh_store

    body = await request.json()
    tid = str(body.get("task_id") or "").strip()
    if not rh_store.delete_task(tid):
        raise HTTPException(status_code=404, detail={"error": {"message": "任务不存在"}})
    return {"tasks": rh_store.list_tasks()}


@app.post("/ui/api/runninghub/upload")
async def ui_runninghub_upload(request: Request):
    """上传本地图片/音频/视频，返回站内 LoadImage 节点能用的 fileName。

    走官方 ``/openapi/v2/media/upload/binary``——它**只要 api_key**（不要登录态），
    且实测同一个 key 在 .ai 域名能传、在 .cn 回「API Key不存在」。站内自己的
    ``/upload/image`` 要 ComfyUI 的 ``Rh-Comfy-Auth``（编辑器 iframe 里才发），拿不到。
    api_key 来源：账号记录里的，没有就用登录态 ``getUserInfo.apiKey`` 顺出来的。
    """
    _ensure_local(request)
    from ...runninghub import client as rh_client
    from ...runninghub import store as rh_store
    from ...runninghub import websession
    from ...runninghub.runner import WebAccountSession

    accounts = rh_store.get_active_accounts()
    if not accounts:
        raise HTTPException(status_code=400, detail={"error": {"message": "请先导入至少一个 RunningHub 账号"}})

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少上传文件 file"}})
    file_type = str(form.get("file_type") or "image").strip().lower()
    content = await upload.read()
    if not content:
        raise HTTPException(status_code=400, detail={"error": {"message": "上传文件为空"}})
    if len(content) > 30 * 1024 * 1024:
        raise HTTPException(status_code=400, detail={"error": {"message": "文件大小不能超过 30MB"}})

    acc = accounts[0]
    api_key = str(acc.get("api_key") or "")
    if not api_key and acc.get("email") and acc.get("password"):
        # 没存 key：用登录态拿站内展示的 normalApiKey，顺手写回账号记录
        try:
            # /uc/apiKey/get 的 normalApiKey 是脱敏串（af1e****96e1）不能用来请求；
            # getUserInfo.apiKey 才是完整的。只在真要上传时才取，不往 account_info 里放。
            info, _ = await WebAccountSession(acc, persist=rh_store.update_session).call(
                lambda t: websession.get_user_info(t)
            )
            api_key = str(info.get("apiKey") or "")
            if api_key:
                rh_store.upsert_account(email=acc["email"], api_key=api_key)
        except websession.WebSessionError as exc:
            raise HTTPException(status_code=400, detail={"error": {"message": f"拿不到 api_key：{exc}"}})
    if not api_key:
        raise HTTPException(status_code=400, detail={"error": {"message": "账号没有 api_key，无法上传；请在导入时填 api_key"}})

    try:
        file_name = await rh_client.upload_file(
            api_key,
            getattr(upload, "filename", "upload.bin") or "upload.bin",
            content,
            content_type=getattr(upload, "content_type", "application/octet-stream") or "application/octet-stream",
            file_type=file_type,
        )
        return {"fileName": file_name}
    except rh_client.RunningHubError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"error": {"message": str(exc)}})
