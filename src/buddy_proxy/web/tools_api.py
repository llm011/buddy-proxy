"""DeepSeek 官方 Search / Fetch 工具端点（``/v1/tools/*``）。

**这两个路由固定指向 DeepSeek 官方 API，不做多通道路由。** 理由：

- ``/v1/tools/search`` 与 ``/v1/tools/fetch`` 是 DeepSeek 独有的**私有**接口
  （2026-11 官方开放），不在 OpenAI / Anthropic 任何标准里，也没有第二个渠道
  实现它——本仓库所有其它通道（CodeBuddy / Trae / Qoder / Kimi …）都只有对话
  端点。所以「路由到别家」这件事在物理上不存在，写死官方反而消除了
  「模型名一样、行为不一样」这类误配。
- 与 ``/v1/chat/completions`` 等**按模型名路由**的端点不同，这两个端点**没有
  模型名**参数（只收 query / url），本来就没有可路由的维度。

上游契约（2026-11-01 实测 + 官方文档）：

- ``POST {base}/v1/tools/search``  body ``{"query", "count", "ori_question",
  "localization", "need_content"}`` → ``{"results": [...]}``
- ``POST {base}/v1/tools/fetch``   body ``{"url"}`` → ``{"title", "content"}``

计费：**按成功请求次数**（$0.75 / 1000 次），不按 token；失败、空结果、超时
都不计费。所以这里**不加本地缓存**——缓存会改变计费与「实时性」的对应关系，
而这条链路的价值恰恰是实时；要省钱的正确做法是客户端少调，不是在网关悄悄
复用旧结果。

凭据与 :mod:`buddy_proxy.providers.deepseek` **共用**：同一个
``DEEPSEEK_API_KEY`` / 同一个 key 文件、同一个基址（``DEEPSEEK_BASE_URL``）。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from buddy_proxy.core.state import app, diagnostic, get_state
from buddy_proxy.providers.deepseek import (
    auth_headers,
    resolve_api_key,
    resolve_base_url,
)
from buddy_proxy.codebuddy_provider import log_client_request
from buddy_proxy.web.routes import client_meta, parse_request_body

log = logging.getLogger(__name__)

#: Search 查询串的上限（官方：1~128 Unicode 字符，不能全是空白）。
_MAX_QUERY_CHARS = 128

#: 结果条数的官方区间（1~50，默认 10）。
_MIN_COUNT, _MAX_COUNT = 1, 50

_TIMEOUT = httpx.Timeout(connect=20.0, read=120.0, write=30.0, pool=15.0)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT)


def _search_diag_fields(body: dict[str, Any]) -> dict[str, Any]:
    """search 的脱敏诊断字段（只记结构，不记 query 原文）。

    **字段名必须避开日志红线的敏感词**：``safe_log_fields`` 会把键名里含
    ``content`` / ``body`` / ``message`` / ``token`` / ``prompt`` … 的值一律当
    正文哈希成 ``*_bytes`` / ``*_sha256``。布尔值经它一换就只剩两个无意义的
    哈希，可观测性静默消失（``auth_present`` 当年正是踩了 ``api_key`` 这处）。
    所以这里叫 ``chars`` / ``want_page_text`` 而不是 ``query_len`` 之外的
    直觉名——改名前先跑一遍 ``safe_log_fields`` 确认原值能活下来。
    """
    query = body.get("query")
    return {
        "chars": len(query) if isinstance(query, str) else 0,
        "want_page_text": bool(body.get("need_content")),
        "results": body.get("count"),
    }


async def _post_tool(path: str, payload: dict[str, Any]) -> JSONResponse:
    """把请求转发到固定官方端点并原样回传响应。

    错误处理遵循官方文档：**只对 408 / 429 / 5xx 建议重试**，其余 4xx
    （400 参数错 / 401 key 无效 / 402 余额不足 / 403 策略拒绝 / 404 抓不到内容）
    是确定性的，重试没有意义，原样透传给客户端让它自己决策。

    上游错误体本身就是 OpenAI 兼容形状，透传即可——客户端能按熟悉的那套解析。
    """
    key = resolve_api_key()
    if not key:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": (
                        "deepseek 未配置认证：请设置 DEEPSEEK_API_KEY 或写 "
                        "~/.buddy-proxy/deepseek_api_key（DeepSeek 开放平台 → "
                        "API Keys 签发，platform.deepseek.com）"
                    ),
                    "type": "authentication_error",
                }
            },
        )
    url = f"{resolve_base_url()}{path}"
    try:
        async with _client() as client:
            resp = await client.post(url, json=payload, headers=auth_headers(key))
    except httpx.TimeoutException as exc:
        log.warning("deepseek tools timeout (%s): %s", path, exc)
        raise HTTPException(status_code=504, detail={
            "error": {"message": "deepseek tools upstream timeout", "type": "timeout"}}
        ) from exc
    except httpx.HTTPError as exc:
        log.warning("deepseek tools upstream error (%s): %s", path, exc)
        raise HTTPException(status_code=502, detail={
            "error": {"message": "deepseek tools upstream error", "type": "bad_gateway"}}
        ) from exc

    try:
        payload_out = resp.json()
    except Exception:
        payload_out = {"error": {"message": resp.text[:500], "type": "upstream_error"}}
    return JSONResponse(status_code=resp.status_code, content=payload_out)


@app.post("/v1/tools/search")
async def tools_search(request: Request):
    """官方联网检索（固定 DeepSeek API）。

    请求体：``{"query": "...", "count": 10, "need_content": false,
    "ori_question": "...", "localization": ""}``，只有 ``query`` 必填。

    本地**只做轻校验**：空 query / 超长 query / count 越界这三条，官方也会
    自己拒（400），但提前拦掉能省一次计费链路往返、也给出更具体的中文提示。
    其余字段原样透传——官方明确「未知字段会被忽略」，本地不必替它裁剪。
    """
    body = await parse_request_body(request)
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "请求体应为 JSON 对象", "type": "invalid_request_body"}},
        )
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail={
            "error": {"message": "query 不能为空", "type": "invalid_request_error"}})
    if len(query) > _MAX_QUERY_CHARS:
        raise HTTPException(status_code=400, detail={
            "error": {"message": f"query 不能超过 {_MAX_QUERY_CHARS} 个字符（当前 {len(query)}）",
                      "type": "invalid_request_error"}})
    count = body.get("count")
    if count is not None and not isinstance(count, bool):
        if not isinstance(count, int) or not (_MIN_COUNT <= count <= _MAX_COUNT):
            raise HTTPException(status_code=400, detail={
                "error": {"message": f"count 必须是 {_MIN_COUNT}~{_MAX_COUNT} 的整数",
                          "type": "invalid_request_error"}})

    meta = client_meta(request)
    log_client_request("POST", "/v1/tools/search", body, **meta)
    diagnostic("tools_request", tool="search", **_search_diag_fields(body))
    return await _post_tool("/v1/tools/search", body)


@app.post("/v1/tools/fetch")
async def tools_fetch(request: Request):
    """抓取单个网页并返回 Markdown（固定 DeepSeek API）。

    请求体：``{"url": "https://..."}``。

    抓不到内容时官方回 **404**（不是 200 空体），本路由原样透传——客户端据此
    区分「没这条路由」和「这一页抓不到」。
    """
    body = await parse_request_body(request)
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "请求体应为 JSON 对象", "type": "invalid_request_body"}},
        )
    url = body.get("url")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(status_code=400, detail={
            "error": {"message": "url 不能为空", "type": "invalid_request_error"}})

    meta = client_meta(request)
    log_client_request("POST", "/v1/tools/fetch", body, **meta)
    diagnostic("tools_request", tool="fetch", url_chars=len(url))
    return await _post_tool("/v1/tools/fetch", body)


def _tool_status() -> dict[str, Any]:
    """给 /health 用的工具端点状态（是否配了 key、指向哪个 base）。"""
    return {
        "configured": bool(resolve_api_key()),
        "base_url": resolve_base_url(),
    }