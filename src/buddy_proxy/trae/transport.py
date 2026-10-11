"""Trae HTTP 传输层：legacy 文本协议的端点回退与 Work 通道重试发送。"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from fastapi import HTTPException

from .config import (
    BASE_URL_CN,
    ENDPOINTS,
    _WORK_CHAT_MAX_ATTEMPTS,
    _debug_dump,
    work_function_override,
    map_model_for,
    resolve_trae_region,
    resolve_trae_region_by_chat_base,
)
from .credentials import _auth, _build_headers, _load_work_cred, _work_headers
from .text_protocol import _build_chat_body

log = logging.getLogger(__name__)

def send_trae_chat(
    messages: list[dict[str, Any]],
    model: str,
    stream: bool,
    base_url: str = BASE_URL_CN,
) -> str:
    """直连 Trae API，返回原始 SSE 文本。

    3 级端点回退。认证失败抛 HTTPException(401)，其余抛 RuntimeError。
    若使用 Work 凭证（~/.ethan/trae_work.json），自动走 Work 通道
    （function=solo_work_lite + api.trae.com.cn）。
    """
    work = _load_work_cred()
    if work and work.get("access_token"):
        return _send_trae_work_chat(messages, model, stream, work)

    token, user_id = _auth()
    # legacy 单账号分支（TRAE_TOKEN / 本机登录态）没有账号级 region 字段，
    # 以 base_url 为准反查区域——海外网关走海外模型表，避免把海外的 Claude/GPT
    # 名拿去查 CN 的 GLM 映射表（查不到会原样透传，但分级/倍率会全错）。
    region = resolve_trae_region_by_chat_base(base_url)
    trae_model = map_model_for(region.key, model)
    body = _build_chat_body(messages, trae_model, stream)
    headers = _build_headers(token, user_id)

    last_error: Exception | None = None
    for endpoint in ENDPOINTS:
        # URL 仍用调用方传入的 base_url（测试注入点/自定义网关原样生效）；
        # region 只用来选模型映射表，不改这里的 host。
        url = base_url + endpoint
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = resp.read().decode("utf-8", errors="replace")
                log.info("Trae endpoint %s OK (status=%s)", endpoint, resp.status)
                return data
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            log.warning("Trae endpoint %s failed %s: %s", endpoint, e.code, detail)
            if e.code in (401, 403):
                raise HTTPException(status_code=401, detail=f"trae auth failed: {detail}")
            last_error = RuntimeError(f"{endpoint}: {e.code} {detail}")
        except Exception as e:
            log.warning("Trae endpoint %s error: %s", endpoint, e)
            last_error = e
    raise HTTPException(status_code=502, detail=f"trae all endpoints failed: {last_error}")

def _intl_content_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """海外端点要求 ``messages[].content`` 为内容块数组，纯字符串要包装。

    实测（2026-10-06）：global 网关的 Go 侧把 content 反序列化成
    ``[]*idecopilot.LLMRawMessageContent``，纯字符串直接 400
    ``cannot unmarshal string into Go struct field``；CN 两种都收。
    只包装字符串形态（tool_result 等本来就是块列表的消息原样放行），
    结构不对时让上游诚实地报错，好过在这里猜格式洗一遍。
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, dict) and isinstance(m.get("content"), str):
            out.append({**m, "content": [{"type": "text", "text": m["content"]}]})
        else:
            out.append(m)
    return out


def _send_trae_work_chat(
    messages: list[dict[str, Any]],
    model: str,
    stream: bool,
    work: dict[str, Any],
) -> str:
    """Trae Work 通道：function=solo_work_lite + 完整 SOLO headers。

    参考 traework2api（Go）的 Work 通道实现：
    - headers 必须带 User-Agent: Trae/<ver> + X-Ide-Token 等 SOLO 专属头
      （缺 UA 会被服务端当异常客户端限流 4011）
    - host 用 chat 网关（CN 是 mchost.guru），签到/额度才走 UG 域

    网关按**账号自己的 region** 选（cred 落盘时写入，见 credentials.save_account_cred）：
    两区账号不通用，failover 已保证这里拿到的是同区账号，所以直接用它的 region
    就是对的——不需要调用方再传一次，也就不会出现「provider 说海外、请求打到
    CN」这类两处分叉。
    """
    region = resolve_trae_region(work.get("region"))
    trae_model = map_model_for(region.key, model)
    body = _build_chat_body(_intl_content_blocks(messages) if region.key == "global" else messages,
                            trae_model, stream)
    # 绝大多数模型走 solo_work_lite；少数在该 function 下 4001，需改走
    # chat_v3（实测）。覆盖表**按区域取**：两区的 function 绑定互不通用
    # （CN glm-5.1 / 海外 gpt-5.6 系是各自的 chat_v3 名单）。
    body["function"] = work_function_override(region.key).get(trae_model, "solo_work_lite")

    headers = {
        **_work_headers(work),
        "Accept": "text/event-stream" if stream else "application/json",
    }

    # 聊天网关走 chat_base，**不要**改用凭证里的 work["api_host"]：
    # 那是 OAuth 域，对该路径返回 404（2026-09 实测）。
    url = f"{region.chat_base}/api/agent/v3/llm_utils_chat"
    # 上游断流多为瞬态（实测：长响应读到一半连接被关 → http.client.IncompleteRead，
    # 曾被流式层当正文吐成 "[Error: IncompleteRead(...)]"）。读失败后丢弃半截响应，
    # 同端点退避重试，避免一次网络抖动作废整轮长生成。
    payload = json.dumps(body).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(1, _WORK_CHAT_MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            url,
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                _debug_dump(
                    "debug_trae_upstream",
                    model=trae_model,
                    function=body["function"],
                    raw=raw[:20000],
                )
                return raw
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            # 401/403 透传真实状态码，理由同 native_tools._send_native_chat：
            # 包成 502 会绕过 failover 的账号级闸门（形态码丢了），坏账号
            # 永远不被冷却跳过。
            raise HTTPException(
                status_code=e.code if e.code in (401, 403) else 502,
                detail=f"trae work chat failed: {e.code} {detail}")
        except Exception as e:
            # HTTPError 之外一律视为瞬态网络错误（IncompleteRead / 连接重置 / 超时等），
            # 保留原始异常重试；非瞬态错误重试 3 次也会快速失败，代价可接受。
            last_error = e
            if attempt < _WORK_CHAT_MAX_ATTEMPTS:
                log.warning(
                    "Trae Work chat attempt %d/%d failed (%s: %s), retrying",
                    attempt, _WORK_CHAT_MAX_ATTEMPTS, type(e).__name__, e,
                )
                time.sleep(attempt)
            else:
                log.error(
                    "Trae Work chat failed after %d attempts: %s: %s",
                    _WORK_CHAT_MAX_ATTEMPTS, type(e).__name__, e,
                )
    raise HTTPException(
        status_code=502,
        detail=f"trae work chat failed after {_WORK_CHAT_MAX_ATTEMPTS} attempts: {last_error}",
    )

