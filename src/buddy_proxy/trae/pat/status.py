"""PAT 模型负载查询（plus 网关 get_detail_param，短 TTL 缓存）。"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from fastapi import HTTPException

# 接缝约定：函数体内对「测试可注入接缝」（monkeypatch 打在本包命名空间上的
# 名字，见包 __init__ 兼容约定）及包内共享状态经 _ns 调用期解析。
import buddy_proxy.trae.pat as _ns

from .config import _EXCHANGE_TIMEOUT_S, _PLUS_GATEWAY, ensure_pat_config
from .credentials import _pat_headers

# ───────────────────────── 模型负载查询 ─────────────────────────

_MODEL_STATUS_TTL_S = 600
_model_status_cache: dict[str, Any] = {"fetched_at": 0.0, "data": None}


def _default_function() -> str:
    return os.environ.get("WB_TRAE_NATIVE_FUNCTION", "chat_v3")


def _fetch_hot_by_config(plus: str, credentials: Any, function: str) -> dict[str, float | None]:
    """按单个 function 拉 get_detail_param，归一为 {config_name: workload(0-100%或None)}。

    上游错误（HTTP / 网络）在此抛出，由调用方决定是整体失败还是降级跳过该表。
    """
    request = urllib.request.Request(
        f"{plus}/api/ide/v1/get_detail_param",
        data=json.dumps({"function": function,
                         "need_prompt": False, "poly_prompt": False}).encode("utf-8"),
        method="POST",
        headers=_pat_headers(credentials, "*/*"),
    )
    with urllib.request.urlopen(request, timeout=_EXCHANGE_TIMEOUT_S) as response:
        raw = json.loads(response.read().decode("utf-8", errors="replace"))
    hot_by_config: dict[str, float | None] = {}
    for entry in raw.get("config_info_list") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("config_name")
        if not isinstance(name, str) or not name:
            continue
        hot = ((entry.get("display_config") or {}).get("hot_info") or {}).get("hot")
        hot_by_config[name] = (round(min(max(float(hot), 0.0), 100.0), 1)
                               if isinstance(hot, (int, float)) else None)
    return hot_by_config


def fetch_pat_model_status(force: bool = False, cached_only: bool = False) -> dict[str, Any]:
    """手动触发的模型负载查询（用首个账号凭证；结果短 TTL 缓存）。

    返回 {models: [{id, workload(0-100%或None), credits, max_input}], fetched_at, cached}；
    只有 plus 网关目录接口提供负载数据。workload 为 None 表示该模型无负载信息。

    cached_only=True 时只读缓存、绝不触网：有缓存返回之（cached=True），
    无缓存返回空壳（fetched_at=0），供页面默认展示、按钮再强制刷新。
    """
    now = time.time()
    cached = _model_status_cache.get("data")
    if cached_only:
        if cached:
            return {**cached, "cached": True}
        return {"models": [], "fetched_at": 0.0, "cached": False}
    if (not force and cached and now - float(_model_status_cache.get("fetched_at") or 0)
            < _MODEL_STATUS_TTL_S):
        return {**cached, "cached": True}
    plus = os.environ.get(_PLUS_GATEWAY, "").strip().rstrip("/")
    if not plus:
        raise HTTPException(status_code=503, detail="PAT 模型服务未配置")
    profile = ensure_pat_config()[0]
    credentials = _ns._get_profile_credentials(profile)

    # 负载表按 function 分表：个别模型只在非默认 function 下开放（gpt-6.1-sol /
    # gpt-6-astra 仅 solo_agent），默认 chat_v3 的 config_info_list 里根本没有它们的
    # config_name，只查一张表 → 这些模型永远 workload=None（面板显示「—」）。
    # 故查齐「默认 function ∪ 各 plus 模型自己的 function 覆盖」，每个模型用自己的
    # 表取负载。实测只需 chat_v3 + solo_agent 两张，多打一次请求。
    default_fn = _default_function()
    functions = {default_fn}
    for model_id in _ns.pat_model_names():
        if _ns.pat_gateway_is_plus(model_id):
            functions.add(_ns.PAT_MODEL_FUNCTIONS.get(model_id, default_fn))

    # 逐 function 拉表：首张（默认 function）失败视为整体不可用（沿用旧语义，向上
    # 报 502）；非默认表失败只降级为空表（其模型 workload=None），不拖垮其余模型。
    hot_by_fn: dict[str, dict[str, float | None]] = {}
    first = True
    for fn in [default_fn] + sorted(functions - {default_fn}):
        try:
            hot_by_fn[fn] = _ns._fetch_hot_by_config(plus, credentials, fn)
        except urllib.error.HTTPError as exc:
            try:
                exc.read()
            except Exception:
                pass
            if first:
                raise HTTPException(
                    status_code=502,
                    detail=f"PAT 模型负载查询失败（HTTP {exc.code}）") from None
            hot_by_fn[fn] = {}
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if first:
                raise HTTPException(
                    status_code=502,
                    detail=f"PAT 模型负载查询失败（网络错误：{reason}）") from None
            hot_by_fn[fn] = {}
        first = False

    meta = _ns.pat_model_meta()
    models: list[dict[str, Any]] = []
    for model_id in _ns.pat_model_names():
        if not _ns.pat_gateway_is_plus(model_id):
            continue
        info = meta.get(model_id) or {}
        _, config_name = _ns.PAT_PLUS_MODELS.get(model_id, ("", model_id))
        fn = _ns.PAT_MODEL_FUNCTIONS.get(model_id, default_fn)
        workload = hot_by_fn.get(fn, {}).get(config_name)
        # 兜底：模型声明的 function 没查到负载时回落默认表（上游偶发按 function
        # 收口 config，默认表往往仍带该 config）。
        if workload is None and fn != default_fn:
            workload = hot_by_fn.get(default_fn, {}).get(config_name)
        models.append({
            "id": model_id,
            "name": info.get("name") or model_id,
            "workload": workload,
            "credits": info.get("credits"),
            "max_input": info.get("max_input"),
            "reasoning": bool(info.get("reasoning")),
        })
    result = {"models": models, "fetched_at": now}
    _model_status_cache.update({"fetched_at": now, "data": result})
    return {**result, "cached": False}
