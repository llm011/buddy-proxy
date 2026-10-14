"""Antigravity 模型目录加载与动态 id 归一化。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger(__name__)

_MODELS_JSON = Path(__file__).with_name("models.json")


def _load_models(path: Path = _MODELS_JSON) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("antigravity models.json 读取失败（%s），使用内置兜底表", exc)
        return [{
            "id": "gemini-3.8-flash",
            "group": "gemini",
            "description": "Gemini 3.8 Flash (fallback)",
        }]
    return [m for m in data.get("models") or [] if isinstance(m, dict) and m.get("id")] or [{
        "id": "gemini-3.8-flash",
        "group": "gemini",
        "description": "Gemini 3.8 Flash (fallback)",
    }]


def _load_blocked_prefixes(path: Path = _MODELS_JSON) -> tuple[str, ...]:
    """运行期刷新目录时要丢弃的旧代/内部基名前缀（见 models.json 注释）。

    fetchAvailableModels 仍会回传已淘汰的旧代模型（gemini-2.5/3-flash/3.1-flash/
    3.5 等），不拦就会被「重新拉取」灌回 /v1/models。只按前缀命中旧代，真·新模型
    （不在表里）照常被发现收录。读取失败时返回空表（宁可不拦，不误杀新模型）。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    raw = data.get("blocked_prefixes") or []
    # 统一小写存储：匹配时也小写化 base，与 _is_public_upstream_model 的大小写
    # 处理保持一致，防上游偶发混合大小写旧代名字漏网又被灌回目录。
    return tuple(str(p).lower() for p in raw if isinstance(p, str) and p)


MODELS: list[dict[str, Any]] = _load_models()
BLOCKED_PREFIXES: tuple[str, ...] = _load_blocked_prefixes()
DEFAULT_MODELS: dict[str, str] = {
    m["id"]: str(m.get("description") or m["id"]) for m in MODELS
}
_MODEL_BY_ID: dict[str, dict[str, Any]] = {m["id"]: m for m in MODELS}

# 只去掉已知 effort 后缀；thinking/image/agent 等是模型身份的一部分。
_EFFORT_SUFFIXES = ("-extra-low", "-low", "-medium", "-high", "-tiered")
_EFFORTS = tuple(suffix.removeprefix("-") for suffix in _EFFORT_SUFFIXES)
_INTERNAL_MODEL_PREFIXES = ("chat_", "tab_")


def _split_effort_suffix(name: str) -> tuple[str, str | None]:
    for suffix in _EFFORT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)], suffix.removeprefix("-")
    return name, None


def _strip_effort_suffix(name: str) -> str:
    return _split_effort_suffix(name)[0]


def _is_public_upstream_model(name: str) -> bool:
    return bool(name) and not name.lower().startswith(_INTERNAL_MODEL_PREFIXES)


def _is_blocked_base(base: str) -> bool:
    """运行期刷新目录时是否丢弃该（去 effort 后缀的）基名。

    命中 ``BLOCKED_PREFIXES`` 即旧代/内部模型，不收录；表为空则一律放行。
    curated（verified）模型不走这里判定——它们在 refresh 时被直接 seed 进目录，
    且前缀表经核对不与任何 verified 基名相交（见 models.json 注释）。
    """
    return bool(base) and base.lower().startswith(BLOCKED_PREFIXES)


def _default_effort(efforts: Sequence[str]) -> str:
    """为动态条目选择保守且可路由的默认档位。"""
    for preferred in ("medium", "tiered", "low", "high", "extra-low"):
        if preferred in efforts:
            return preferred
    return efforts[0]


def _group_for_model(model_id: str) -> str:
    return "claude-gpt" if model_id.startswith(("claude-", "gpt-")) else "gemini"
