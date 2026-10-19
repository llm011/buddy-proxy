"""对话模型目录的**动态**发现（模型页「↻ 重新拉取」的数据源）。

Codex 后端的模型表是**按 client_version 分代**的：同一个账号用
``GET /backend-api/codex/models?client_version=<ver>`` 查询，旧版本号只看得见
旧代（0.142 → 1 个模型，0.154 → 6 个，0.159+ → 9 个）。所以这里固定用一个
足够新的版本号去问，再把上游结果和 ``models.json`` 里手写的说明合并：

- 上游条目是**权威列表**（slug / context_window / display_name / description）
- ``models.json`` 只提供上游没有的语义：中文名、默认模型（第一条）、
  以及 ``profile`` 之类的本地标注

筛选口径（与 Codex CLI 展示一致）：``visibility == "list"`` 且
``supported_in_api != false``。``hide`` 的 ``gpt-reserve`` /
``codex-auto-review`` 是内部用途，不进列表；想看它们直接点名也仍可转发
（``accepts_model`` 只认目录内模型，所以这里干脆不收）。

图片模型**不在这张表里**（见 :mod:`buddy_proxy.codex.images`），刷新时保持
原样不删。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .upstream import build_headers, normalize_base_url

log = logging.getLogger(__name__)

#: 查询用的客户端版本。0.159 起才有 gpt-6.1-sol；再往上没有新增可见模型，
#: 但**不要**填过高的伪造值——上游对没发布的版本可能返回空表。
CATALOG_CLIENT_VERSION = "0.159.0"

#: 上游没有的本地语义（中文说明 / 展示名）。仅用于把 slug 翻成面板文案，
#: 不在表里的新 slug 直接回退上游 description。
_LOCAL_NOTES: dict[str, str] = {
    "gpt-6.1-sol": "GPT-6.1 Sol — Codex 后端当前最强档（默认）",
    "gpt-6-astra": "GPT-6 Astra — 最难的推理/设计任务",
    "gpt-6-sol": "GPT-6 Sol — 上一代主力档",
    "gpt-6-luna": "GPT-6 Luna — 轻量快档",
    "gpt-5.6-sol": "GPT-5.6 Sol — 前沿编码 / 研究模型",
    "gpt-5.6-terra": "GPT-5.6 Terra — 均衡档",
    "gpt-5.6-luna": "GPT-5.6 Luna — 快速档",
}

#: models.json 里的第一条是默认模型；刷新后如果它还在就仍然排第一位。
_DEFAULT_MODEL = "gpt-6.1-sol"


def catalog_url(base_url: str, *, client_version: str = CATALOG_CLIENT_VERSION) -> str:
    """``.../codex`` base + ``/models?client_version=`` 的完整 URL。"""
    return f"{normalize_base_url(base_url)}/models?client_version={client_version}"


def parse_catalog(payload: Any) -> list[dict[str, Any]]:
    """上游 ``{"models": [...]}`` → 可用的对话模型条目（按上游顺序）。

    只做**结构性**筛选（可见 + 支持 API + 有 slug），不猜语义。
    重复 slug 保留第一条（上游理论上不会重复，防脏数据）。
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("models")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or item.get("id") or "").strip()
        if not slug or slug in seen:
            continue
        if str(item.get("visibility") or "list") != "list":
            continue
        if item.get("supported_in_api") is False:
            continue
        seen.add(slug)
        out.append(item)
    return out


#: 上游目录里没有、只存在于本地 ``models.json`` 的能力旗标。刷新时按 slug
#: 从旧目录抄回来——否则一次刷新就把 ``reasoning`` 之类丢掉，``/v1/models``
#: 的 ``supported_reasoning_levels`` 随之变空。
_LOCAL_FLAGS = ("reasoning", "tool_call", "interleaved_thinking")


def build_model_entries(
    upstream: list[dict[str, Any]],
    *,
    image_ids: list[str] | None = None,
    local: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """上游条目 → ``models.json`` 的 ``models`` 数组（默认模型排第一）。

    ``local`` 是刷新前的本地目录：只用来补上游不提供的本地标注
    （:data:`_LOCAL_FLAGS`，以及本地有、上游没给描述时的中文说明）。
    """
    previous = {
        str(m.get("id")): m for m in (local or [])
        if isinstance(m, dict) and m.get("id") and m.get("type") != "image"
    }
    entries: list[dict[str, Any]] = []
    for item in upstream:
        slug = str(item.get("slug") or "").strip()
        old = previous.get(slug) or {}
        note = _LOCAL_NOTES.get(slug) or _text_like(old.get("description"))
        if not note:
            display = str(item.get("display_name") or slug).strip()
            desc = str(item.get("description") or "").strip()
            note = f"{display} — {desc}" if desc else display
        entry: dict[str, Any] = {"id": slug, "description": note}
        ctx = item.get("context_window")
        if isinstance(ctx, int) and ctx > 0:
            entry["context_window"] = ctx
        elif isinstance(old.get("context_window"), int):
            entry["context_window"] = old["context_window"]
        if item.get("input_modalities"):
            entry["input_modalities"] = list(item["input_modalities"])
        elif old.get("input_modalities"):
            entry["input_modalities"] = list(old["input_modalities"])
        for key in _LOCAL_FLAGS:
            if key in old:
                entry[key] = old[key]
        entries.append(entry)

    # 默认模型置顶（它代表 `body` 不带 model 时的落点，见 provider.forward）
    if _DEFAULT_MODEL in {e["id"] for e in entries}:
        entries.sort(key=lambda e: 0 if e["id"] == _DEFAULT_MODEL else 1)

    for mid in image_ids or []:
        entries.append(_image_entry(mid))
    return entries


def _text_like(value: Any) -> str:
    """本地 description 若是字符串则原样用（上游描述可能只是英文展示名）。"""
    return value.strip() if isinstance(value, str) else ""


_IMAGE_NOTES = {
    "gpt-image-2.5": "GPT Image 2.5（生图，默认）",
    "gpt-image-2.5-sunburst": "GPT Image 2.5 Sunburst",
    "gpt-image-2.5-flare": "GPT Image 2.5 Flare",
    "gpt-image-2": "GPT Image 2",
    "gpt-image-1.5": "GPT Image 1.5",
}


def _image_entry(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "description": _IMAGE_NOTES.get(model_id, model_id),
        "type": "image",
    }


async def fetch_catalog(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    access_token: str,
    account_id: str,
    client_version: str = CATALOG_CLIENT_VERSION,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """带上真实账号 token 拉一次上游目录；失败抛 :class:`CatalogError`。"""
    url = catalog_url(base_url, client_version=client_version)
    headers = build_headers(access_token, account_id=account_id)
    headers["Accept"] = "application/json"
    try:
        resp = await client.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise CatalogError(f"网络错误: {exc}") from exc
    if resp.status_code != 200:
        body = ""
        try:
            body = resp.text[:200]
        except Exception:  # noqa: BLE001
            pass
        raise CatalogError(f"HTTP {resp.status_code}{(': ' + body) if body else ''}")
    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise CatalogError("上游返回的不是 JSON") from exc
    models = parse_catalog(payload)
    if not models:
        # 空表不能当成功覆盖目录——否则一次风控抽风就把通道清空
        raise CatalogError("上游目录为空（未返回任何可见模型）")
    return models


class CatalogError(RuntimeError):
    """目录拉取失败（由调用方逐账号重试并保留旧快照）。"""


def write_catalog(path: str | Path, entries: list[dict[str, Any]]) -> None:
    """原子写回 ``models.json``（保留 ``source`` 说明）。"""
    p = Path(path)
    payload = {
        "source": (
            "运行期从 GET /backend-api/codex/models?client_version="
            f"{CATALOG_CLIENT_VERSION} 拉取（模型页「↻ 重新拉取」），"
            f"更新于 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}；"
            "普通对话模型按 visibility=list 筛选，图片模型来自 image_models.json"
        ),
        "models": entries,
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(p)
