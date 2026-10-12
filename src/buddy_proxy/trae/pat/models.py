"""PAT 模型目录：models_config.json 中 traepat 条目的热加载缓存。

按 mtime 热加载；plus（扩展目录）与 public（公网）分池，支持按模型覆盖
function（如 gpt-6-astra 仅 solo_agent）。模块导入时加载一次。
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any

# 接缝约定：函数体内对「测试可注入接缝」（monkeypatch 打在本包命名空间上的
# 名字，见包 __init__ 兼容约定）及包内共享状态经 _ns 调用期解析。
import buddy_proxy.trae.pat as _ns

log = logging.getLogger(__name__)

# ───────────────────────── 模型目录 ─────────────────────────

def _find_models_config() -> pathlib.Path:
    """向上定位 buddy_proxy 包内的 models_config.json。

    拆分教训（2026-09-09）：本文件从 trae/pat.py 移入 trae/pat/models.py 后，
    硬编码 parents[1] 错位到 trae/ 目录，模型目录静默加载为空（stat 失败
    被吞掉），traepat 全部模型 400。改为向上搜索，层级变化不再敏感。

    2026-09-11：models_config.json 迁入 web/ 子包后不再是本文件的直系祖先，
    故每级祖先同时探测 ``<parent>/web/models_config.json``。
    """
    for parent in pathlib.Path(__file__).resolve().parents:
        for candidate in (parent / "models_config.json", parent / "web" / "models_config.json"):
            if candidate.is_file():
                return candidate
    raise FileNotFoundError("models_config.json 未在包目录上游找到")


_MODEL_CONFIG_FILE = _find_models_config()
PAT_MODELS: dict[str, tuple[str, str]] = {}
PAT_PLUS_MODELS: dict[str, tuple[str, str]] = {}
PAT_PUBLIC_MODELS: dict[str, tuple[str, str]] = {}
# 模型 -> function 覆盖（个别模型只在特定 function 下开放，如 gpt-6-astra 仅
# solo_agent；缺省走 WB_TRAE_NATIVE_FUNCTION / chat_v3）
PAT_MODEL_FUNCTIONS: dict[str, str] = {}
# 模型 -> 上游频控窗口 (requests, window_s)，见 pacing；未声明的模型不节流
PAT_MODEL_RATE_LIMITS: dict[str, tuple[int, float]] = {}
_config_mtime: float | None = None


def _parse_rate_limit(spec: Any) -> tuple[int, float] | None:
    """``{"requests": N, "window_s": W}`` → (N, W)；缺失/非法返回 None（不节流）。"""
    if not isinstance(spec, dict):
        return None
    try:
        requests = int(spec.get("requests"))
        window = float(spec.get("window_s"))
    except (TypeError, ValueError):
        return None
    if requests <= 0 or window <= 0:
        return None
    return requests, window


def _reload_pat_models() -> None:
    global _config_mtime
    try:
        mtime = _MODEL_CONFIG_FILE.stat().st_mtime
    except OSError:
        return
    if _config_mtime == mtime:
        return
    plus: dict[str, tuple[str, str]] = {}
    public: dict[str, tuple[str, str]] = {}
    functions: dict[str, str] = {}
    rate_limits: dict[str, tuple[int, float]] = {}
    try:
        data = json.loads(_MODEL_CONFIG_FILE.read_text("utf-8"))
        for model in data.get("models", []):
            if model.get("provider") != "traepat":
                continue
            model_id = str(model.get("id") or "").strip()
            upstream = str(model.get("upstream_model") or model_id)
            config = str(model.get("config_name") or model_id)
            if model_id and upstream:
                (plus if model.get("gateway") == "plus" else public)[model_id] = (upstream, config)
            override = str(model.get("function") or "").strip()
            if model_id and override:
                functions[model_id] = override
            limit = _parse_rate_limit(model.get("rate_limit"))
            if model_id and limit:
                rate_limits[model_id] = limit
    except Exception as exc:
        log.warning("PAT 模型配置解析失败（沿用上次内容）: %s", type(exc).__name__)
        return
    PAT_PLUS_MODELS.clear()
    PAT_PLUS_MODELS.update(plus)
    PAT_PUBLIC_MODELS.clear()
    PAT_PUBLIC_MODELS.update(public)
    PAT_MODEL_FUNCTIONS.clear()
    PAT_MODEL_FUNCTIONS.update(functions)
    PAT_MODEL_RATE_LIMITS.clear()
    PAT_MODEL_RATE_LIMITS.update(rate_limits)
    PAT_MODELS.clear()
    PAT_MODELS.update({**plus, **public})
    _config_mtime = mtime
    log.info("PAT 模型目录已加载：扩展 %d + 公网 %d", len(plus), len(public))


def pat_model_names() -> list[str]:
    _ns._reload_pat_models()
    return list(PAT_MODELS)


def pat_model_meta() -> dict[str, dict[str, Any]]:
    _ns._reload_pat_models()
    try:
        data = json.loads(_MODEL_CONFIG_FILE.read_text("utf-8"))
        return {str(model.get("id")): model for model in data.get("models", [])
                if model.get("provider") == "traepat" and model.get("id")}
    except Exception:
        return {}


def pat_gateway_is_plus(model: str) -> bool:
    _ns._reload_pat_models()
    return model in _ns.PAT_PLUS_MODELS


def is_pat_model(model: str) -> bool:
    _ns._reload_pat_models()
    return model in _ns.PAT_MODELS


_reload_pat_models()
