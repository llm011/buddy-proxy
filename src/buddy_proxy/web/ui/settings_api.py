"""管理 UI：settings.json 的读写接口（默认模型/通道、打卡设置）。

原 ``web/ui.py`` 拆分。模型引用的校验/归一 helper 在 ``models_api``。
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException, Request

from buddy_proxy.core import settings as settings_mod
from buddy_proxy.core.state import app, get_state
from buddy_proxy.benefits import read_checkin_settings

from .common import _ensure_local
from .models_api import _model_exists_anywhere, _normalize_model_ref, _validate_model


@app.get("/ui/api/settings")
async def ui_settings_get(request: Request):
    _ensure_local(request)
    state = get_state()
    cfg = read_checkin_settings()
    return {
        "default_model": getattr(state, "default_model", None),
        "default_provider": getattr(state, "default_provider", "codebuddy"),
        "auto_checkin": cfg["auto_checkin"],
        "checkin_time": cfg["checkin_time"],
        "load_balance": sorted(getattr(state, "load_balance", set()) or ()),
        "session_rotation": sorted(getattr(state, "session_rotation", set()) or ()),
        "disabled_accounts": sorted(getattr(state, "disabled_accounts", set()) or ()),
        # 负载均衡的「缓存隔离」提示：每个通道里哪些模型换号会掉缓存。
        # 前端拿去在开关旁边显示一行小字，让用户知道「开了均衡，但这个模型
        # 不会跟着轮转」。数据来自 core.load_balance（静态实测表 + 线上观测）。
        "load_balance_excluded": _load_balance_excluded(state),
        "path": str(settings_mod.settings_path()),
    }


@app.post("/ui/api/settings")
async def ui_settings_post(request: Request):
    _ensure_local(request)
    state = get_state()
    body = await request.json()

    update: dict[str, Any] = {}
    if "default_model" in body:
        raw = settings_mod.normalize_default_model(body.get("default_model") or "")
        if raw:
            if "/" in raw:
                provider_id, model_id = raw.split("/", 1)
                provider_id, model_id = _normalize_model_ref(provider_id, model_id, state)
                _validate_model(provider_id, model_id, state)
                # 指定了通道的默认模型顺带把兜底通道对齐（显式前缀路由优先级一致）
                update["default_provider"] = provider_id
            elif not _model_exists_anywhere(raw, state):
                raise HTTPException(
                    status_code=400,
                    detail={"error": {"message": f"裸模型 id {raw} 未命中任何已启用通道的模型列表"}},
                )
            update["default_model"] = raw
        else:
            # 清空默认模型：恢复「按客户端请求原样路由」
            update["default_model"] = ""
    if body.get("default_provider"):
        pid = body["default_provider"]
        if pid != "codebuddy" and pid not in getattr(state, "providers", {}):
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": f"provider {pid} 未启用"}},
            )
        update["default_provider"] = pid
    if "auto_checkin" in body:
        update["auto_checkin"] = bool(body["auto_checkin"])
    if "checkin_time" in body:
        raw_time = str(body["checkin_time"] or "").strip()
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", raw_time):
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": f"打卡时间格式应为 HH:MM，收到 {raw_time!r}"}},
            )
        update["checkin_time"] = raw_time
    # 负载均衡 & 会话轮转开关（provider 维度的**增量**写入，两者互斥）：
    # body 里给 ``load_balance: {"codebuddy": true}`` 或
    # ``session_rotation: {"codebuddy": true}`` 就只改对应通道，并自动关掉同一
    # 通道上互斥的另一项。整表覆盖会让多面板并发点击把别的通道悄悄关掉。
    providers = getattr(state, "providers", {}) or {}
    lb_patch = _provider_bool_patch(body.get("load_balance"), providers, field="load_balance")
    sr_patch = _provider_bool_patch(body.get("session_rotation"), providers, field="session_rotation")
    both_on = sorted(pid for pid, on in lb_patch.items() if on and sr_patch.get(pid) is True)
    if both_on:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": f"通道 {', '.join(both_on)} 不能同时开启负载均衡与会话轮转"}},
        )
    if lb_patch or sr_patch:
        cur_lb = set(getattr(state, "load_balance", set()) or ())
        cur_sr = set(getattr(state, "session_rotation", set()) or ())
        lb_changed = bool(lb_patch)
        sr_changed = bool(sr_patch)
        for pid, on in lb_patch.items():
            if on:
                cur_lb.add(pid)
                if pid in cur_sr:
                    cur_sr.discard(pid)
                    sr_changed = True
            else:
                cur_lb.discard(pid)
        for pid, on in sr_patch.items():
            if on:
                cur_sr.add(pid)
                if pid in cur_lb:
                    cur_lb.discard(pid)
                    lb_changed = True
            else:
                cur_sr.discard(pid)
        # 排序写盘：settings.json 是给人看的，集合的随机序会让每次保存都产生
        # 无意义的 diff（也会让「设置文件变了」的健康检查误报）。
        if lb_changed:
            update["load_balance"] = sorted(cur_lb)
        if sr_changed:
            update["session_rotation"] = sorted(cur_sr)

    # 账号「临时下线」开关（账号维度的**增量**写入）：body 里给
    # ``disabled_accounts: {"antigravity:u@x.com": true}`` 只改这一个账号。
    # 键口径 provider_id:account_id；provider 必须是已启用通道（codebuddy 恒认）。
    da_patch = _disabled_accounts_patch(body.get("disabled_accounts"), providers)
    if da_patch:
        cur_da = set(getattr(state, "disabled_accounts", set()) or ())
        for key, on in da_patch.items():
            if on:
                cur_da.add(key)
            else:
                cur_da.discard(key)
        update["disabled_accounts"] = sorted(cur_da)

    if not update:
        raise HTTPException(status_code=400, detail={"error": {"message": "没有可更新的设置字段"}})
    saved = settings_mod.save_settings(update)
    # 热更新运行态（"" 与 None 都视为未设置）
    if "default_model" in update:
        state.default_model = update["default_model"] or None
    if "default_provider" in update:
        state.default_provider = update["default_provider"]
    if "load_balance" in update:
        state.load_balance = set(update["load_balance"])
    if "session_rotation" in update:
        state.session_rotation = set(update["session_rotation"])
    if "disabled_accounts" in update:
        state.disabled_accounts = set(update["disabled_accounts"])
    return {
        "ok": True,
        "settings": {
            k: saved.get(k) for k in (
                "default_model", "default_provider", "load_balance",
                "session_rotation", "disabled_accounts",
            )
        },
        "load_balance_excluded": _load_balance_excluded(state),
    }


def _load_balance_excluded(state: Any) -> dict[str, list[str]]:
    """已开启均衡的通道里，哪些模型因「缓存按账号隔离」不参与轮转。

    只返回**开着开关**的通道：没开的通道不轮转，告诉用户「哪些模型被排除」
    没有意义（他也没在均衡）。返回 ``{provider_id: [model, ...]}``，前端在
    开关旁显示「已跳过 N 个不共享缓存的模型」并在浮层里列出来。
    """
    try:
        from buddy_proxy.core import load_balance as lb_mod

        out: dict[str, list[str]] = {}
        for pid in sorted(getattr(state, "load_balance", set()) or ()):
            models = lb_mod.account_scoped_models(pid)
            if models:
                out[pid] = models
        return out
    except Exception:  # noqa: BLE001 - 提示信息，失败就不显示
        return {}


def _provider_bool_patch(
    raw: Any,
    providers: dict[str, Any],
    *,
    field: str = "load_balance",
) -> dict[str, bool]:
    """校验并归一通道开关增量：``{provider_id: bool}``。

    只接受已注册的通道 id（``codebuddy`` 恒认，它不在 ``providers`` 字典里而是
    默认通道）。非法条目**整条拒绝**而不是静默跳过：静默跳过会让用户以为打开了，
    实际下一次重启才发现没生效（与 ``model_order`` 的容错取向相反——那个是配置文件
    手写，这个是 UI 点击，写错属于 bug 而非用户输入）。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": f"{field} 应为 {{provider: bool}} 对象"}},
        )
    out: dict[str, bool] = {}
    for key, value in raw.items():
        pid = str(key or "").strip()
        if not pid:
            continue
        if pid != "codebuddy" and pid not in providers:
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": f"provider {pid} 未启用"}},
            )
        if not isinstance(value, bool):
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": f"{field}.{pid} 应为 true/false"}},
            )
        out[pid] = value
    return out


def _load_balance_patch(raw: Any, providers: dict[str, Any]) -> dict[str, bool]:
    return _provider_bool_patch(raw, providers, field="load_balance")


def _disabled_accounts_patch(raw: Any, providers: dict[str, Any]) -> dict[str, bool]:
    """校验并归一账号禁用增量：``{"provider:account_id": bool}``。

    键必须含 ``:``，provider 段必须是已启用通道（codebuddy 恒认）；account_id
    段非空。非法条目整条拒绝（同 _provider_bool_patch 的取向：UI 点击写错属 bug）。
    保留原始键字符串（account_id 可能含 @ . - 等，不做改写）。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "disabled_accounts 应为 {provider:account_id: bool} 对象"}},
        )
    out: dict[str, bool] = {}
    for key, value in raw.items():
        full = str(key or "").strip()
        if not full:
            continue
        if ":" not in full:
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": f"disabled_accounts 键 {full!r} 应为 provider:account_id"}},
            )
        pid, account_id = full.split(":", 1)
        pid = pid.strip()
        if not account_id.strip():
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": f"disabled_accounts 键 {full!r} 缺少 account_id"}},
            )
        if pid != "codebuddy" and pid not in providers:
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": f"provider {pid} 未启用"}},
            )
        if not isinstance(value, bool):
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": f"disabled_accounts.{full} 应为 true/false"}},
            )
        out[full] = value
    return out
