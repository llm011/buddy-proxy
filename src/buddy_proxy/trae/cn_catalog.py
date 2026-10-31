"""Trae CN 模型目录拉取（``get_detail_param``，2026-10-25 实测）。

与海外（``intl_catalog``）同一个端点、同一套 work 凭证，区别只在网关：
CN 是 ``trae-api-cn.mchost.guru``。返回的 ``config_info_list`` 每条带

- ``config_name``：上游真名（大小写敏感，Doubao-/DeepSeek- 大写前缀）；
- ``display_config.display_name``：客户端面板上的展示名；
- ``display_config.multimodal``：是否读图；
- ``display_config.is_invisible_to_user``：True = 客户端不展示（内部/下架件）；
- ``display_contact_config.consumption_rate.data.rate``：**客户端「模型倍率」
  面板显示的就是这个值**（实测 kimi-k3 1.83 / minimax-m3.1-flash 0.09 /
  qwen3.8-max 1.5，与面板截图逐条吻合）。活动折扣另在 ``activity_discount``
  里，``consumption_rate`` 是**当前实付**倍率（如 Doubao-Seed-2.1-Pro 限时
  1 折时 0.08、活动到期后回到 0.8）；
- ``context_window_tokens.dev``：上下文窗口。

过滤口径与海外一致：``custom_model*``（用户自定义占位）与
``summary`` / ``fast_apply`` / ``title_generation`` / ``input_optimization``
（IDE 内部 function）不是对话模型，一律丢弃；``config_switch`` 为 False 的
视为已下架；``is_invisible_to_user`` 为 True 的不进目录（曾经手 probe 收录过
的 glm-5.1 / kimi-k2.7-code 现在就藏在这个标记后）；``display_config`` 带
``is_custom_model`` / ``builder`` 的是用户自建模型，同样不收。这条判据只有
两个字段撑着（2026-10-25 实测：这类条目 ``config_name`` 不带前缀、
``is_invisible_to_user`` 是 None 而非 True，除此之外与正常条目没有区别）——
上游形状一变，用户自建模型就会重新漏进 ``/v1/models`` 并被拿去探测，届时
要在 ``display_config`` 里重新找新的标记字段。

**目录 ≠ 可调**：``kimi-k3.1-pro`` 在目录里可见，但本机两个 CN 账号实测都回
``code:1005``（``extra.plan=1``，会员档位不足），所以拉取侧只负责「发现 +
抄倍率」，可用性仍由 provider 的真请求探测裁决。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .config import BASE_URL_CN
from .credentials import _work_headers

log = logging.getLogger(__name__)

_FETCH_TIMEOUT_S = 30

#: 非对话模型（IDE 内部 function），与 intl_catalog 同名同口径。
_INTERNAL_CONFIG_NAMES = frozenset({
    "summary", "fast_apply", "fast_apply_new", "title_generation",
    "input_optimization",
})


def _as_obj(value: Any) -> dict[str, Any]:
    """``display_*_config`` 有时是 JSON 字符串、有时已是对象（实测两种都出现）。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _rate_of(entry: dict[str, Any]) -> float | None:
    """取当前实付倍率；缺字段/形状变化返回 None（不猜）。"""
    contact = _as_obj(entry.get("display_contact_config"))
    rate = ((contact.get("consumption_rate") or {}).get("data") or {}).get("rate")
    try:
        return float(rate)
    except (TypeError, ValueError):
        return None


def fetch_cn_catalog(cred: dict[str, Any]) -> list[dict[str, Any]]:
    """用单个 CN 账号的 work 凭证拉候选目录。

    返回 ``[{"name", "display_name", "rate", "context_window", "multimodal"}]``
    （只含对用户可见的对话模型，顺序随上游）。失败抛 ``RuntimeError``——
    调用方（``provider.refresh_models``）逐账号容错，单账号失败只收缩并集。
    """
    region = str(cred.get("region") or "cn").strip().lower()
    if region == "global":
        raise RuntimeError("fetch_cn_catalog 只拉 CN 目录，海外走 intl_catalog")
    headers = {**_work_headers(cred), "Accept": "application/json"}
    body = json.dumps({
        # function 取值不影响目录内容（与 intl_catalog 同款实测结论）。
        "function": "chat_v3",
        "need_prompt": False,
        "poly_prompt": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL_CN}/api/ide/v1/get_detail_param",
        data=body, headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_S) as response:
            raw = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        except Exception:  # noqa: BLE001 - 排空响应体即可
            pass
        raise RuntimeError(f"CN 模型目录 HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"CN 模型目录网络错误：{reason}") from None

    entries: list[dict[str, Any]] = []
    for entry in raw.get("config_info_list") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("config_name")
        if not isinstance(name, str) or not name:
            continue
        if name.startswith("custom_model") or name in _INTERNAL_CONFIG_NAMES:
            continue
        if entry.get("config_switch") is False:
            continue
        if entry.get("is_invisible_to_user") is True:
            continue
        display = _as_obj(entry.get("display_config"))
        # 用户自己在 IDE 里加的自定义模型（实测：BC-GLM-5.3-Flash /
        # BC-Kimi-K3 / gpt-5.5 …）：``config_name`` 不带 custom_model 前缀、
        # ``is_invisible_to_user`` 是 None 而不是 True，只能靠 display_config
        # 里的 ``is_custom_model`` / ``builder`` 认出来。这类名字是**别人
        # 账号的私有配置**，既不该发布进 /v1/models，也不该拿去探测。
        if display.get("is_custom_model") or display.get("builder"):
            continue
        ctx = entry.get("context_window_tokens")
        dev_ctx = ctx.get("dev") if isinstance(ctx, dict) else None
        entries.append({
            "name": name,
            # 对外 id 统一小写（与 MODEL_IDS / model_order 口径一致）；上游
            # 原样名仍按原大小写发给网关（见 config.MODEL_MAP 的映射约定）
            "id": name.lower(),
            "display_name": display.get("display_name") or name,
            "rate": _rate_of(entry),
            "context_window": dev_ctx if isinstance(dev_ctx, int) else None,
            "multimodal": bool(display.get("multimodal")),
        })
    if not entries:
        # 正常至少 20+ 条；空表大概率是响应形状变了，按失败处理保住旧快照。
        raise RuntimeError("CN 模型目录为空（上游响应形状可能已变化）")
    return entries
