"""FastAPI 路由与请求处理。

所有 ``@app`` 装饰的路由统一放这里，通过 ``from .state import app`` 引用共享 app 实例。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from buddy_proxy.core.state import (
    app,
    _get_state_or_none,
    diagnostic,
    get_state,
)
from buddy_proxy.core import settings as settings_mod
from buddy_proxy.web.model_list import load_models_from_local_config, model_to_codex_format
from buddy_proxy.codebuddy_provider import (
    CLIENT_TAG,
    HAS_PROJECTION,
    anthropic_to_chat,
    body_summary,
    forward_chat,
    log_client_request,
    project_responses_chat_body,
    resolve_client_tag,
    responses_request_to_chat,
)

# 尝试导入高级功能模块（可选）
try:
    from buddy_proxy.core.desensitize import desensitize_body
    HAS_DESENSITIZE = True
except ImportError:
    HAS_DESENSITIZE = False

    def desensitize_body(body, **kwargs):
        return body


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """全局兜底异常日志：任何未捕获异常都记录（便于定位问题），再返回 500。"""
    try:
        state = _get_state_or_none()
        if state is not None:
            import traceback
            tb = traceback.format_exc()
            if state.logger:
                state.logger.error(
                    "unhandled_exception: %s %s -> %s\n%s",
                    request.method, request.url.path, exc, tb,
                )
            if state.json_logger:
                state.write_log(
                    "unhandled_exception",
                    method=request.method,
                    path=request.url.path,
                    error=str(exc),
                )
    except Exception:
        pass  # 日志失败不影响响应
    # 不回显 exc 原文（避免内部细节/路径信息暴露给客户端），详情在服务端日志里
    return JSONResponse(
        status_code=500,
        content={"error": {"message": "internal error", "type": "internal_error"}},
    )


@app.get("/health")
async def health():
    state = get_state()
    auth = {} if state.mock_dir is not None else (state.client.session.get("auth") or {})
    expires = int(auth.get("expiresAt") or 0)
    # 附加各 provider 的健康信息（兼容无 providers 属性的旧构造）。
    # 单个通道的 health() 抛异常只降级为该通道的 error 字段——2026-10-03
    # 排查「health 无响应」时发现探活方（curl -f）把 5xx 也当失败，任何
    # 一个通道炸掉都会让 buddy status 误报整个代理死了。
    providers_health: dict[str, Any] = {}
    for pid, p in getattr(state, "providers", {}).items():
        # 单个通道的 health() 可能是同步阻塞调用（读文件/探网络）。直接 await 会
        # 卡住整个 event loop，把所有请求（含 /health 自己）一起拖死——这正是
        # 「代理活着但 status 报无响应」的另一种成因。放到线程里跑并加超时。
        try:
            providers_health[pid] = await asyncio.wait_for(
                asyncio.to_thread(p.health), timeout=3.0
            )
        except asyncio.TimeoutError:
            providers_health[pid] = {"id": pid, "error": "health() 超时（>3s）"}
        except Exception as exc:  # noqa: BLE001 - 健康检查自身不能成为故障源
            providers_health[pid] = {"id": pid, "error": f"{type(exc).__name__}: {exc}"}
    # 资源水位自观测：FD 数（当前打开）与 RSS（进程峰值，ru_maxrss 只增不减，
    # 持续上涨即可判断泄漏趋势）。排查「用一段时间卡死」时不用再猜——
    # curl /health 就能看到是否 FD 耗尽/内存暴涨。
    import resource
    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = round(maxrss / (1024 * 1024), 1) if sys.platform == "darwin" else round(maxrss / 1024, 1)
    try:
        # macOS 自省用 /dev/fd，Linux 用 /proc/self/fd；都失败就不报该字段
        open_fds = len(os.listdir("/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"))
    except OSError:
        open_fds = -1
    return {
        "status": "ok",
        "authenticated": bool(auth.get("accessToken")),
        "token_valid": not expires or expires > int(time.time() * 1000),
        "uptime_seconds": int(time.time() - state.started_at),
        "rss_mb": rss_mb,
        "open_fds": open_fds,
        "providers": providers_health,
    }


@app.get("/v1/models")
async def list_models():
    state = get_state()
    # 从本地配置文件加载模型列表（离线可靠，无需认证）
    data = load_models_from_local_config()
    # 标记通道归属：静态表中的模型走默认 CodeBuddy 通道
    for m in data:
        m.setdefault("provider", "codebuddy")

    # 合并其它 provider 的模型（如豆包/Trae）。
    # provider 字段标识该模型由哪个上游通道提供（codebuddy/trae/doubao），
    # 与 owned_by（上游厂牌，如 zhipu）区分，便于客户端辨识。
    # 整通道停用的直接不列：客户端选了也只会 403，列表里就该当它不存在。
    disabled_providers = getattr(state, "disabled_providers", set()) or set()
    for provider in getattr(state, "providers", {}).values():
        if provider.id in disabled_providers:
            continue
        for m in provider.models():
            data.append({
                "id": m.get("id"),
                "name": m.get("name") or m.get("description") or m.get("id"),
                "vendor": provider.id,
                "owned_by": provider.id,
                "provider": provider.id,
                **({"credits": m["credits"]} if m.get("credits") is not None else {}),
                **({"max_input": m["max_input"]} if m.get("max_input") is not None else {}),
                **({"max_output": m["max_output"]} if m.get("max_output") is not None else {}),
                **({"reasoning": m["reasoning"]} if "reasoning" in m else {}),
                # 上游内部代号（如 Qoder 的 qfmodel）：对外 id 是人读的小写真实名
                # （qoder/qwen3.8-flash），排障时要能对回上游目录，故一并透传。
                **({"upstream_key": m["upstream_key"]} if m.get("upstream_key") else {}),
                # 能力字段必须透传：漏传会让 /v1/models 把这些通道的模型
                # 一律报成纯文本，客户端据此误剥图片（见列表下方 input_modalities）
                **({"images": bool(m["images"])} if "images" in m else {}),
                **({"tool_call": bool(m["tool_call"])} if "tool_call" in m else {}),
                # 目录条目自带的元数据必须透传：漏传时模型会被报成纯文本 +
                # 无上下文窗口（codex 目录从上游带回这两项）。
                **({"context_window": m["context_window"]}
                   if isinstance(m.get("context_window"), int) else {}),
                **({"images": True} if "image" in (m.get("input_modalities") or ())
                   else {}),
                # 生图模型（codex gpt-image-*）标记：调用方式与对话模型不同
                **({"type": "image"} if m.get("type") == "image" else {}),
            })

    # 用户隐藏项只影响目录曝光，不改变 provider 路由：从两个公开数组生成前统一
    # 过滤，保证 OpenAI data 与 Codex 扩展 models 完全一致。provider 目录条目的 id
    # 可能带自身前缀（qoder/qwen...），settings 键一律是 provider/裸名。
    hidden = getattr(state, "hidden_models", set()) or set()
    visible: list[dict[str, Any]] = []
    for m in data:
        pid = str(m.get("provider") or "codebuddy")
        mid = str(m.get("id") or "")
        prefix = f"{pid}/"
        bare = mid[len(prefix):] if mid.startswith(prefix) else mid
        if settings_mod.model_key(pid, bare) not in hidden:
            visible.append(m)
    data = visible

    # 记录模型列表请求
    diagnostic(
        "models_list_request",
        models_count=len(data),
        source="local_config"
    )

    # 标准 OpenAI 格式：/v1/models 的 data 数组（客户端按此解析）。
    # 能力字段（input_modalities / supports_images）同时放进 data：
    # 第三方客户端（agent/IDE）只会按标准字段解析，读不到下面的 models
    # 扩展数组，缺了能力信息就会自行猜测是否支持图片（典型做法是按模型名
    # 匹配关键词），导致支持读图的模型被判为纯文本、图片在客户端就被剥掉。
    # 这里显式声明，让客户端可直接读取。
    openai_models = [
        {
            "id": m.get("id", "unknown"),
            "object": "model",
            "created": 1720872952,
            "owned_by": m.get("vendor") or "codebuddy",
            # 通道归属：codebuddy（默认）/ trae / doubao ...
            "provider": m.get("provider", "codebuddy"),
            "name": m.get("name") or m.get("id", "unknown"),
            "display_name": m.get("name") or m.get("id", "unknown"),
            "credits": m.get("credits"),
            "tags": m.get("tags", []),
            # 能力：与 models 扩展数组同口径（model_to_codex_format 为唯一来源）
            "input_modalities": ["text", "image"] if m.get("images") else ["text"],
            "supports_images": bool(m.get("images")),
            "supports_tool_call": bool(m.get("tool_call")),
            # 上游内部代号（Qoder 的 qfmodel 等）：对外 id 是人读的小写真实名，
            # 排障时要能对回上游目录。无此概念的通道不出现该字段。
            **({"upstream_key": m["upstream_key"]} if m.get("upstream_key") else {}),
        }
        for m in data
    ]

    # 扩展：Codex 兼容的完整模型元数据（含上下文窗口、能力等）
    codex_models = [model_to_codex_format(m) for m in data]

    return {"object": "list", "data": openai_models, "models": codex_models}


def client_meta(request: Request) -> dict[str, str]:
    """提取客户端来源标识（自声明 X-Client-Name / UA / 来源 IP / key 指纹）。

    顺带把合成后的短标签写入 CLIENT_TAG ContextVar，供 _instrument 落 metrics
    （/ui 最近请求表展示）。X-Client-Name 优先于 UA 推断——UA 可被客户端
    伪装（如 pi 仍发 claude-cli），自声明头不会。
    """
    auth = request.headers.get("x-api-key") or request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        auth = auth[7:]
    user_agent = request.headers.get("user-agent", "-")
    client_name = request.headers.get("x-client-name", "")
    CLIENT_TAG.set(resolve_client_tag(user_agent, client_name, api_key=auth))
    return {
        "user_agent": user_agent,
        "client_ip": request.client.host if request.client else "-",
        # 不记录 API key 的明文或片段；resolve_client_tag 仅在内存中匹配本地映射。
        # 字段名刻意不含 "api_key"——safe_log_fields 会把含敏感词的键当正文哈希，
        # 布尔值会被替换成无意义的 *_bytes/*_sha256，丢失「是否带鉴权」的可观测性。
        "auth_present": bool(auth),
        "client_name": client_name,
    }


async def parse_request_body(request: Request) -> Any:
    """解析 JSON 请求体，兼容 GBK/cp936/latin-1 等非 UTF-8 编码。

    某些客户端（如 Pi）偶尔以 GBK 编码发送请求体，而 Starlette 的
    ``request.json()`` 内部是 ``json.loads(raw_bytes)``，默认按 UTF-8 解码，
    遇非 UTF-8 字节会直接抛 ``UnicodeDecodeError``（``ValueError`` 子类，
    非 ``JSONDecodeError``），导致 500。此处改为按
    utf-8 → gbk → cp936 → latin-1 依次解码再解析，彻底失败时返回 400。
    """
    raw = await request.body()
    for encoding in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    # latin-1 能解码任意字节，故 decode 不会失败；走到这里仅当 JSON 结构非法。
    from fastapi import HTTPException
    raise HTTPException(
        status_code=400,
        detail={
            "error": {
                "message": "请求体不是有效的 JSON（已尝试 utf-8/gbk/cp936/latin-1 解码）",
                "type": "invalid_request_body",
            }
        },
    )


@app.post("/api/agent/doubao")
async def agent_doubao(request: Request):
    """一次性 agent 任务 → 豆包工作（CDP 直连），SSE 事件流中转。

    请求体：``{"task": "...", "session_id": "...", "model": "doubao-auto"}``
    - task 必填；session_id 缺省续聊进程内默认会话，"new"/空串强制新建；
      model 可选（默认 doubao-auto，仅限 agent 管线模型）

    事件协议见 :meth:`DoubaoProvider.stream_agent_task`。启动/登录检查在
    流外完成：失败时以正常 HTTP 状态码返回可操作提示（如主 App 未开
    CDP 调试端口 → 提示先完全退出豆包重试；未安装 → 提示安装），
    而不是藏在 SSE 流里。
    """
    body = await parse_request_body(request)
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "请求体应为 JSON 对象", "type": "invalid_request_body"}},
        )
    task = str(body.get("task") or "").strip()
    if not task:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "task 不能为空", "type": "invalid_request"}},
        )
    provider = get_state().providers.get("doubao")
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"message": "doubao provider 未启用（需以 --doubao 启动）", "type": "not_found"}},
        )
    model, model_spec = provider.resolve_agent_model(body.get("model"))
    await provider.ensure_ready()
    return StreamingResponse(
        provider.stream_agent_task(task, body.get("session_id"), model, model_spec),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "close"},
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    state = get_state()
    body = await parse_request_body(request)

    log_client_request("POST", "/v1/chat/completions", body, **client_meta(request))
    diagnostic("request", protocol="openai", **body_summary(body))

    # 调试开关只增加安全摘要，绝不落请求原文、token、UID 或工具参数。
    if os.environ.get("WB_DEBUG_DUMP"):
        state.write_log("debug_request_summary", **body_summary(body))

    return await forward_chat(body, "openai")


@app.post("/v1/responses")
async def create_response(request: Request):
    state = get_state()
    body = await parse_request_body(request)

    log_client_request("POST", "/v1/responses", body, **client_meta(request))

    # 转换 Responses → Chat
    chat_body = responses_request_to_chat(body)

    # 消息压缩优化（如果启用）
    if state.enable_optimize_context and HAS_PROJECTION:
        chat_body, proj_stats = project_responses_chat_body(chat_body)
        diagnostic("projection_applied", protocol="responses", **proj_stats)

    # 过滤无效的工具定义
    tools = chat_body.get("tools", [])
    if tools:
        original_count = len(tools)
        filtered_tools = []
        filtered_names = []

        for tool in tools:
            # 1. 过滤非 function 类型
            if tool.get("type") != "function":
                filtered_names.append(f"{tool.get('type', 'unknown')} (非function类型)")
                continue

            # 2. 过滤空 parameters
            func = tool.get("function", {})
            params = func.get("parameters", {})
            if not params or not isinstance(params, dict) or len(params) == 0:
                filtered_names.append(f"{func.get('name', 'unknown')} (空parameters)")
                continue

            # 3. 检查 parameters 是否有 type 字段
            if "type" not in params:
                filtered_names.append(f"{func.get('name', 'unknown')} (缺少type)")
                continue

            filtered_tools.append(tool)

        chat_body["tools"] = filtered_tools

        if filtered_names:
            diagnostic("tools_filtered",
                      original=original_count,
                      kept=len(filtered_tools),
                      filtered=filtered_names)

    diagnostic("request", protocol="responses", **body_summary(chat_body))
    return await forward_chat(chat_body, "responses", original=body)


@app.post("/v1/messages")
async def create_message(request: Request):
    state = get_state()
    body = await parse_request_body(request)

    log_client_request("POST", "/v1/messages", body, **client_meta(request))

    # 转换 Anthropic → Chat
    chat_body = anthropic_to_chat(body)
    diagnostic("request", protocol="anthropic", **body_summary(chat_body))

    return await forward_chat(chat_body, "anthropic", original=body)
