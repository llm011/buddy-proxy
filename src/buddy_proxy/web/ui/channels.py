"""管理 UI：各通道专属管理接口（qoder / traepat / codebuddy）。

原 ``web/ui.py`` 拆分：通道相关的接口都在这里，后续新通道的管理面板接口
（如 antigravity 账号状态）也加到本模块。
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import time
from typing import Any

from fastapi import HTTPException, Request

from buddy_proxy.core.state import app, get_state

from .common import _ensure_local


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
    """
    _ensure_local(request)
    from ...dumate import discovery

    if discovery.discover() is None:
        return {"enabled": False, "accounts": []}
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
