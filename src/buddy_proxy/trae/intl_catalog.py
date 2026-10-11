"""Trae 海外版模型目录拉取（SG 网关 ``get_detail_param``，2026-10-12 实测）。

PAT 侧早就用同款端点做过「模型负载查询」（``trae/pat/status.py`` 的 plus 网关
``/api/ide/v1/get_detail_param``），海外 SG 网关也有同端点：work 凭证 +
``_work_headers`` 直接 POST 即通，返回 ``config_info_list``（实测 48 条）。
与 CN 侧手 probe 时代的发现一致——**目录 ≠ 实测可用**（``gpt-6-astra`` 曾在
目录里但当日 chat 网关 4001，2026-10-12 复测又通了），所以这里的定位是
「候选目录发现」：拉到的名字全量并入 ``/v1/models``（用户拍板），个别 4001
由上游诚实报错、反馈后移除；代际收敛与黑名单过滤在 provider 侧做
（``intl_provider.refresh_models``）。

响应里混着大量非对话配置，必须过滤（名字来自 2026-10-12 实测全量）：

- ``custom_model*``：用户自定义模型占位（``custom_model_placeholder`` /
  ``custom_model_200k`` / ``custom_model_gpt-6`` 等，十来条）；
- 内部功能配置：``summary`` / ``fast_apply`` / ``input_optimization`` /
  ``title_generation``（IDE 会话标题、补丁应用等内部 function，不是对话模型）。

``config_switch`` 是上游的启用开关（实测当前全 True）：False 的条目视为
已下架，不收——将来上游用开关灰度下线时，目录自动跟着缩。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .config import BASE_URL_SG
from .credentials import _work_headers

log = logging.getLogger(__name__)

_FETCH_TIMEOUT_S = 30

#: 内部功能配置（非对话模型）——IDE 的会话标题/补丁应用等内部 function。
_INTERNAL_CONFIG_NAMES = frozenset({
    "summary", "fast_apply", "input_optimization", "title_generation",
})


def fetch_intl_catalog(cred: dict[str, Any]) -> list[dict[str, Any]]:
    """用单个海外账号的 work 凭证拉取模型目录。

    返回 ``[{"name": str, "context_window": int | None}, ...]``（只含过滤后的
    对话模型，顺序随上游）。失败抛 ``RuntimeError``（带 HTTP 状态码或网络
    错误摘要）——调用方（provider.refresh_models）逐账号容错，单账号失败
    只收缩并集不拖垮整体。

    ``cred`` 是落盘的 work 凭据 dict（``load_account_cred`` 的返回值，含
    access_token / machine_id 等）；region 必须是 global——CN 网关同端点
    的目录是另一套池，这里不越权代拉。
    """
    region = str(cred.get("region") or "").strip().lower()
    if region != "global":
        raise RuntimeError(f"fetch_intl_catalog 只拉海外目录，账号 region={region!r}")
    headers = _work_headers(cred)
    headers["Accept"] = "application/json"
    body = json.dumps({
        # function 值无所谓（实测 chat_v3 与 solo_work_lite 返回同一目录），
        # 照 PAT 侧 status.py 的请求形状固定用 chat_v3。
        "function": "chat_v3",
        "need_prompt": False,
        "poly_prompt": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL_SG}/api/ide/v1/get_detail_param",
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
        raise RuntimeError(f"海外模型目录 HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"海外模型目录网络错误：{reason}") from None

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
        ctx = entry.get("context_window_tokens")
        dev_ctx = ctx.get("dev") if isinstance(ctx, dict) else None
        entries.append({
            "name": name,
            "context_window": dev_ctx if isinstance(dev_ctx, int) else None,
        })
    if not entries:
        # 目录为空大概率是上游改了响应形状（正常时至少 20+ 条），按失败处理
        # 比静默展示空目录好——models_api 会转成 502 并保留旧快照。
        raise RuntimeError("海外模型目录为空（上游响应形状可能已变化）")
    return entries
