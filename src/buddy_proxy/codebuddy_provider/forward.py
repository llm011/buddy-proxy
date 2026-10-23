"""多 provider 路由入口：显式前缀 / 模型自动匹配 / 兜底通道 / 候选顺序换档。

所有路径统一经 :func:`observability._instrument` 记录请求指标（/ui 图表数据源）。

**候选顺序（``state.model_order``）的重试规则**：一个模型的候选上游按序尝试，但
**只有**当一次尝试在**向客户端提交任何字节之前**失败时才换下一档——即抛
``HTTPException(可换档状态码)``、抛传输层异常（``_RETRYABLE_EXC``），或拿到非 2xx 的
``JSONResponse``。一旦返回的是 ``StreamingResponse``，字节即将/已经开始流向客户端，
重放有重复计费风险，绝不换档（同 ``trae/pat/chat.py`` 的「首个语义事件提交后绝不重放」
不变量）。已知盲区：codebuddy 流式把上游错误做成了**带内错误 chunk**
（``pipeline.stream_upstream``），路由器无从察觉，故第一档为 codebuddy 时流式失败不换档。

反过来，**编程错误不换档**：``TypeError`` / ``KeyError`` 这类 bug 若被当成上游故障，
就会被冷却标记掩盖、客户端反而拿到 200，真因极难发现。
"""

from __future__ import annotations

import functools
import json
import pathlib
from typing import Any

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from buddy_proxy.core.state import (
    diagnostic,
    get_state,
)
from buddy_proxy.core import settings as settings_mod
from buddy_proxy.core import cooldown as cooldown_mod
from buddy_proxy.core.errors import is_local_dns_failure

from .observability import _instrument
from .provider import _default_codebuddy

#: 允许换档的 HTTP 状态码：上游不可用/过载，换个通道有意义。
#:
#: 刻意**不含** 400/401/403/404：那些是确定性错误（参数错、模型不存在、被停用、
#: 凭据无效），换谁都不会成功，重试只会拖时间并掩盖真因。
#:
#: 405 看着像确定性错误，实则相反：本代理只会 POST 聊天端点，上游给 405 就是
#: WAF/风控拦截——zcode-start 的 3012「request has been blocked due to unusual
#: activity」即以 405 透传（2026-10-06 /model 探测实测，日志 zcode-start/405
#: 后没有下文）。那是**通道级**封禁，换通道正是解法；不给换档的话 model_order
#: 配再多候选也只试第一个，用户看到的就是「明明还有渠道却报错」。
_RETRYABLE_STATUS = frozenset({405, 429, 500, 502, 503, 504})

#: 允许换档的**异常**类型：只认传输层故障，不认编程错误。
#:
#: 刻意**不用**裸 ``except Exception``：那会把 ``TypeError`` / ``KeyError`` 这类真 bug 也
#: 当成「上游故障」——打上冷却标记、静默换到下一档，客户端拿到 200，真因只剩一行
#: ``error=TypeError`` 日志（而且非 ``HTTPException`` 不会进 ``_instrument``，连指标都
#: 没有）。真 bug 被换档「治好」比直接报错更难排查。
#:
#: 这样收窄是安全的：本仓库的通道在各自 ``forward`` 里已把传输异常统一转成
#: ``HTTPException(502/504/429)``（见 zcode/trae pat 的做法），所以绝大多数失败走上面
#: 的 ``HTTPException`` 分支；这里兜的是漏网的上游连接层异常。
_RETRYABLE_EXC = (httpx.HTTPError, OSError)


@functools.lru_cache(maxsize=1)
def _codebuddy_static_ids() -> frozenset[str]:
    """CodeBuddy 静态表（models_config.json）的小写 id 集合（进程内缓存）。

    刻意**不用** ``model_list.load_models_from_local_config()``：那个函数会经
    ``diagnostic()`` 调 ``get_state()``（代理未初始化时抛 503），且它在路由热路径
    上每次都要读文件 + 归一化全表。这里只读一次原始 JSON 并缓存。

    兜底：读失败时返回空集——宁可不做这层保护，也不能让路由挂掉。
    """
    try:
        config = pathlib.Path(__file__).resolve().parent.parent / "web" / "models_config.json"
        data = json.loads(config.read_text("utf-8"))
        return frozenset(
            str(m["id"]).lower() for m in data.get("models", []) if m.get("id")
        )
    except Exception:  # noqa: BLE001 - 配置读不出来不该阻断路由
        return frozenset()


#: 默认 CodeBuddy 通道的「名分」——静态表里有的名字优先归它。
#: 非默认通道用类属性 ``route_priority`` 声明自己的名分，低于这个值就在裸名
#: 撞车时让位（见 :func:`_resolve_auto`）。
_CODEBUDDY_ROUTE_PRIORITY = 100


def _is_codebuddy_model(model: str) -> bool:
    """该名字是否是 CodeBuddy 静态表里的模型 id。

    用途只有一个：在**别名轮**挡住「别的通道想用别名认领一个本该归 CodeBuddy
    的名字」——``auto`` 正是 CodeBuddy 的默认模型，Qoder 的档位模型也叫 ``auto``，
    不挡就会静默改道；CodeBuddy 是兜底通道、不在 ``providers`` 里，别名轮无从
    靠「有没有别的通道精确认领」判断归属，故直接查静态表。

    只按**原样**比对，不做小写化：静态表里的 id 本身就是小写，而 ``Qwen3.8-Max``
    / ``Kimi-K3`` 这类**官方显示名**虽然小写化后与静态表撞车，但它们确实是通道
    目录里真实存在的模型名（Qoder 的 display_name 就是这么写的）。把它们也挡掉
    会让「按官方文档写模型名」直接 502，代价远大于收益——真正需要防的是裸名
    ``auto`` 被别名改道，而那种输入本来就没有大小写变体。
    """
    return model in _codebuddy_static_ids()


def _resolved_model_id(provider: Any, model: str) -> str:
    """把请求里的模型名归一成**停用/时段配置使用的键名**。

    Qoder 之类的通道接受别名（显示名 ``Qwen3.8-Flash`` / 内部 key ``qfmodel``），
    两者必须落到同一把键上，否则「停用了却还能用别名调通」。通道没提供
    ``resolve_model`` 时原样返回（多数通道的 id 就是上游名）。
    """
    resolve = getattr(provider, "resolve_model", None)
    if callable(resolve):
        try:
            resolved = resolve(model)
            if isinstance(resolved, str) and resolved:
                return resolved
        except Exception:  # noqa: BLE001 - 归一失败不阻断转发
            pass
    return model


def _provider_disabled(state: Any, provider_id: str) -> bool:
    """通道是否被管理页开关整通道停用（disabled_providers，用户 2026-10-06 需求）。

    与模型级停用（disabled_models）并存、优先级更高：整通道都不用了，模型级的
    停用/时段配置不用逐条碰。
    """
    disabled = getattr(state, "disabled_providers", None)
    return bool(disabled) and provider_id in disabled


def _reject_if_disabled(state: Any, provider_id: str, model_id: Any) -> None:
    """命中管理页停用或「不在可用时段」的 (provider, model) 组合时返回 403 拒绝转发。

    判定优先级：整通道停用（disabled_providers）> 永久停用（disabled_models）
    > 限时窗口（model_schedules）。三者都未命中才放行。
    """
    # 整通道检查放在 isinstance guard 之前：通道停了就该拦，与请求带没带
    # 合法 model 名无关（review 2026-10-06）。
    if _provider_disabled(state, provider_id):
        diagnostic("provider_disabled_reject", provider=provider_id, model=str(model_id))
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "message": (f"通道 {provider_id} 已在管理页停用（模型页 provider "
                                f"开关），重新打开后再调用"),
                    "type": "provider_disabled",
                }
            },
        )
    if not isinstance(model_id, str):
        return
    # 键必须与配置加载/UI 保存同口径（settings.model_key）：legacy 的
    # workbuddy/* 归一成 codebuddy/*，否则 workbuddy 进来的请求查不到已保存
    # 的停用/时段配置，窗口外照样放行。
    key = settings_mod.model_key(provider_id, model_id)
    disabled = getattr(state, "disabled_models", None)
    if disabled and key in disabled:
        diagnostic("model_disabled_reject", provider=provider_id, model=model_id)
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "message": f"模型 {key} 已被停用，请在管理页 /ui 重新启用后再调用",
                    "type": "model_disabled",
                }
            },
        )
    schedules = getattr(state, "model_schedules", None)
    windows = schedules.get(key) if isinstance(schedules, dict) else None
    if windows is not None and not settings_mod.model_schedule_open(windows):
        diagnostic("model_scheduled_reject", provider=provider_id, model=model_id)
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "message": (f"模型 {key} 当前不在可用时段（开放："
                                f"{settings_mod.format_windows(windows)}），窗口外调用被拒绝"),
                    "type": "model_scheduled",
                }
            },
        )


def _resolve_auto(state: Any, requested_model: str) -> Any:
    """模型 id 自动匹配：**先全部按 id 精确匹配**（含剥前缀裸名），都未命中再退回别名。

    分两轮是必须的——多个通道可能"认识"同一个名字（qoder 的显示名 GLM-5.3 与
    zcode 的模型名 glm-5.3 撞车），若让别名与精确匹配同等参与、按注册序先到
    先得，注册靠前的通道就会把别人的模型抢走。别名只能是兜底，不能是抢占。

    别名轮（``allow_aliases=True``）额外跳过「名字就是 CodeBuddy 静态表 id」的
    情况：CodeBuddy 是兜底通道、不在 ``providers`` 里，别名轮无从靠「别的通道
    能否精确认领」判断归属。``auto`` 正是 CodeBuddy 的默认模型，而 Qoder 本地
    合成的档位模型（TIER_MODELS）也叫 ``auto``，不挡住就会静默改道。

    ⚠️ 只挡别名轮，不挡精确轮：精确轮是各通道按自己发布的 id 认领，trae/zcode
    等通道的模型名可能与静态表重合（两边都真有这个模型），让他们照常先认领，
    与既有路由行为一致。
    """
    providers = getattr(state, "providers", {}) or {}
    for allow_aliases in (False, True):
        if allow_aliases and _is_codebuddy_model(requested_model):
            break  # 归 CodeBuddy 静态表，别名不得认领
        for p in providers.values():
            try:
                if p.accepts_model(requested_model, aliases=allow_aliases):
                    # 裸名与 CodeBuddy 静态表撞车时，真身是 CodeBuddy 的
                    # （deepseek-v4.1-flash 两边都叫这个名）：精确轮也把归属
                    # 让给兜底通道，否则加了 --ark 就静默改道。真要对方舟这
                    # 条，用 ark/<id> 前缀，那条路径不经过这里。
                    if (not allow_aliases and _is_codebuddy_model(requested_model)
                            and hasattr(p, "route_priority")
                            and p.route_priority < _CODEBUDDY_ROUTE_PRIORITY):
                        continue
                    return p
            except Exception:  # noqa: BLE001 - 单个通道判断失败不该阻断路由
                continue
    return None


async def _dispatch_once(
    state: Any,
    provider_id: str,
    model: str,
    body: dict[str, Any],
    protocol: str,
    original: dict[str, Any] | None,
) -> StreamingResponse | JSONResponse:
    """把一次请求派发到**已解析**的 ``(provider_id, model)``。

    只做单次派发：解析 provider 对象 → 停用/时段闸门 → ``ensure_auth`` → ``_instrument``。
    **不**读 ``model_order``、不排序、不做换档——那些都在 :func:`forward_chat` 外层。
    **不吞任何异常**（``HTTPException`` 原样上抛），由调用方决定是否换下一档。

    ``body`` / ``model`` 必须是**已剥前缀**的：前缀剥离、trae/traepat 交叉校验、
    ``_resolved_model_id`` 都属于**解析**，留在 ``forward_chat`` 里。
    """
    # 不变量：派发到的是**已解析**的 (provider_id, model)，上游认的是这个 model，
    # 不是外层请求里的别名。model_order 换档时 body["model"] 仍是原始别名
    # （如 openrouter-4o-max），provider.forward 会把它当上游模型名发出去 →
    # 上游不认 → 挂起/超时（前缀路径在 forward_chat 里已 body={**body,"model":real_model}，
    # 唯独 model_order 路径漏了）。这里统一兜住：body["model"] 一律对齐到已解析的 model。
    if body.get("model") != model:
        body = {**body, "model": model}
    providers = getattr(state, "providers", {}) or {}
    if provider_id == "codebuddy" or provider_id not in providers:
        # 默认 CodeBuddy 通道（不在 providers 里），对称封装，与其它 provider 一致
        _reject_if_disabled(state, "codebuddy", model)
        resp = await _instrument(
            state, _default_codebuddy.forward(body, protocol, original),
            provider_id="codebuddy", model_id=model, protocol=protocol,
            stream=bool(body.get("stream")),
        )
        # The UI test endpoint reports the routed channel without adding a public header.
        resp._buddy_provider_id = "codebuddy"
        return resp
    provider = providers[provider_id]
    # 非默认 provider（Trae/豆包等）：由各自 forward 决定协议支持范围。
    # Trae 已支持 anthropic 协议（/v1/messages 客户端如 Claude Code 可直连）；
    # 豆包等仅 openai 协议透传（doubao2api 只支持 OpenAI chat completions）。
    # 停用/时段键按 provider 归一后的 id 生成，与转发链路口径一致
    # （别名请求如「Qwen3.8-Flash」也要能被停用规则命中）。
    gate_model = _resolved_model_id(provider, model)
    _reject_if_disabled(state, provider.id, gate_model)
    diagnostic("provider_route", provider=provider.id, model=model, protocol=protocol)
    provider.ensure_auth()
    resp = await _instrument(
        state, provider.forward(body, protocol, original),
        provider_id=provider.id, model_id=model, protocol=protocol,
        stream=bool(body.get("stream")),
    )
    resp._buddy_provider_id = provider.id
    return resp


def _split_target(target: str) -> tuple[str, str]:
    """候选目标 ``"provider/model"`` → ``(provider, model)``；裸名 → codebuddy。"""
    if "/" in target:
        provider_id, model = target.split("/", 1)
        return provider_id, model
    return "codebuddy", target


def _target_gated_out(state: Any, provider_id: str, model: str) -> bool:
    """候选目标是否被停用/不在可用时段（此时跳过它、继续下一个）。

    与 :func:`_reject_if_disabled` 同一套判定，但**返回布尔而非抛 403**：候选里放着
    一个今天不想用的通道时，不该把整个模型的请求打成 403（决策：跳过并继续）。
    ``_reject_if_disabled`` 仍然保留，用于非候选路径（未配顺序时行为不变）。
    """
    if _provider_disabled(state, provider_id):
        return True
    key = settings_mod.model_key(provider_id, model)
    disabled = getattr(state, "disabled_models", None)
    if disabled and key in disabled:
        return True
    schedules = getattr(state, "model_schedules", None)
    windows = schedules.get(key) if isinstance(schedules, dict) else None
    return windows is not None and not settings_mod.model_schedule_open(windows)


def _retryable_failure(resp: StreamingResponse | JSONResponse) -> int | None:
    """本次派发结果是否算「未提交即失败」，可否安全换下一个候选。

    可换：非 2xx 的 ``JSONResponse``（zcode/mimo 的 ``_upstream_error_response``、
    qoder/doubao 的 4xx/5xx 错误体），且状态码在 ``_RETRYABLE_STATUS`` 内。

    不可换：一切 ``StreamingResponse``——响应对象已经交给 ASGI，字节即将/已经开始流向
    客户端，重放有重复计费风险（见模块 docstring 的不变量）。codebuddy 流式的带内错误
    chunk 也走这条分支，属于已知盲区。
    """
    if isinstance(resp, StreamingResponse):
        return None
    status = getattr(resp, "status_code", 200)
    return status if status in _RETRYABLE_STATUS else None


async def _forward_with_order(
    state: Any,
    key: str,
    targets: list[str],
    body: dict[str, Any],
    protocol: str,
    original: dict[str, Any] | None,
) -> StreamingResponse | JSONResponse:
    """按 ``targets`` 顺序尝试，未被提交即失败就换下一档。

    跳过两类候选：当前被 ``core.cooldown`` 标记的、被停用/不在可用时段的（决策：跳过并
    继续，而不是把整个模型打成 403）。

    全部候选都不可用/都失败时抛**最后一个**可换档失败（保留最有信息量的错误），而不是
    笼统报 502——客户端要能看到上游真因。
    """
    last_exc: HTTPException | None = None
    last_note = ""
    for target in targets:
        provider_id, model = _split_target(target)
        if cooldown_mod.is_marked(provider_id, model):
            diagnostic("model_order_skip", model=key, target=target, reason="cooldown")
            continue
        if _target_gated_out(state, provider_id, model):
            diagnostic("model_order_skip", model=key, target=target, reason="gated")
            continue
        try:
            resp = await _dispatch_once(state, provider_id, model, body, protocol, original)
        except HTTPException as exc:
            if exc.status_code not in _RETRYABLE_STATUS:
                raise  # 确定性错误（400/401/403/404）：换谁都没用，原样上抛
            last_exc = exc
            last_note = f"HTTP {exc.status_code}"
            # 本机 DNS 解析失败是**机器级**故障（对所有通道同时生效、通常秒级自愈），
            # 不是这个上游的毛病。打 5 分钟冷却会把一次抖动放大成整模型不可用，
            # 且期间连「再探一次」的机会都没有——照常换下一档，但**不标记**。
            # 各 provider 会把 httpx 异常转成 HTTPException(502)，所以必须沿
            # __cause__ 链判断（见 core.errors.is_local_dns_failure）。
            dns_failure = is_local_dns_failure(exc)
            if not dns_failure:
                cooldown_mod.mark_failed(provider_id, model, status=exc.status_code)
            diagnostic("model_order_failover", model=key, target=target,
                       status=exc.status_code, cooldown_skipped=dns_failure)
            continue
        except _RETRYABLE_EXC as exc:  # httpx 传输层 / OSError：上游不可达
            # 注意：asyncio.CancelledError 是 BaseException 子类，不被这里捕获——
            # 客户端断连必须原样向上传播，不能当成可换档失败重试。
            # 其余 Exception（TypeError/KeyError 等）同样不在这里捕获，原样上抛成 500：
            # 那是代码 bug，不该被换档掩盖（见 _RETRYABLE_EXC 注释）。
            last_note = f"{type(exc).__name__}"
            # 与上面的 HTTPException 分支同理：本机 DNS 故障是机器级的，不打标记。
            # 通道没把 httpx 异常包装成 HTTPException 时走的就是这条路（见
            # core.errors.is_local_dns_failure）。
            dns_failure = is_local_dns_failure(exc)
            if not dns_failure:
                cooldown_mod.mark_failed(provider_id, model)
            diagnostic("model_order_failover", model=key, target=target,
                       error=type(exc).__name__, cooldown_skipped=dns_failure)
            continue
        status = _retryable_failure(resp)
        if status is None:
            return resp  # 成功，或已提交语义的流（不换档）
        last_note = f"HTTP {status}"
        cooldown_mod.mark_failed(provider_id, model, status=status)
        diagnostic("model_order_failover", model=key, target=target, status=status)

    if last_exc is not None:
        raise last_exc
    raise HTTPException(
        status_code=502,
        detail={
            "error": {
                "message": (f"模型 {key} 的候选上游全部不可用"
                            + (f"（最后失败：{last_note}）" if last_note else
                               "（均处于冷却或被停用/时段限制）")),
                "type": "upstream_unavailable",
            }
        },
    )


async def forward_chat(
    body: dict[str, Any],
    protocol: str,
    original: dict[str, Any] | None = None,
) -> StreamingResponse | JSONResponse:
    """转发 chat 请求到上游，支持流式和非流式。

    多 provider 路由（优先级从高到低）：
    1. 显式前缀：``model: "<provider-id>/<model-name>"``，如
       ``trae/DeepSeek-V4-Flash``、``doubao/doubao-think``、
       ``codebuddy/glm-5.3``；前缀命中已启用 provider（或 codebuddy）时，
       剥掉前缀再转发，强制走指定通道。
    2. **候选顺序**：``state.model_order`` 里为该模型配了候选列表时，按序尝试，
       未提交即失败就换下一档（见 :func:`_forward_with_order` 与模块 docstring）。
    3. 自动匹配：模型 id 命中某个非默认 provider 的 models() 列表
       （如豆包/Trae 模型），走该 provider 的 forward。
    4. 兜底通道：都未命中时，走 ``state.default_provider``（默认 codebuddy，
       可通过 --default-provider / PROXY_DEFAULT_PROVIDER 改为 trae 等）。

    请求未带 ``model`` 字段时，用管理页设置的默认启用模型
    （``state.default_model``，settings.py 持久化）补齐后再路由。
    所有路径统一经 :func:`_instrument` 记录请求指标（/ui 图表数据源）。
    """
    state = get_state()

    # ---- 默认模型：客户端未指定 model 时补齐 ----
    requested_model = body.get("model")
    if not requested_model:
        default_model = getattr(state, "default_model", None)
        if default_model:
            body = {**body, "model": default_model}
            requested_model = default_model

    # ---- 多 provider 路由 ----
    providers = getattr(state, "providers", {}) or {}
    provider = None

    # 1) 显式 provider 前缀：provider/model
    if isinstance(requested_model, str) and "/" in requested_model:
        prefix, real_model = requested_model.split("/", 1)
        if prefix in providers:
            # 严格分离：扩展目录模型（仅 traepat 有）写 trae/ 前缀时直接报错提示，
            # 不做静默克底；个人目录模型两边都有，trae/ 前缀仍走个人账号
            if prefix == "trae" and "traepat" in providers:
                from buddy_proxy.trae.pat import pat_gateway_is_plus
                if pat_gateway_is_plus(real_model):
                    raise HTTPException(
                        status_code=400,
                        detail=f"模型 {real_model} 属于 traepat 通道（服务账号），"
                               f"请使用 model: traepat/{real_model}")
            provider = providers[prefix]
            # 剥掉前缀后再转发（上游不认识 "trae/" 前缀）
            body = {**body, "model": real_model}
            requested_model = real_model
        elif prefix == "codebuddy":
            # 显式强制走默认 CodeBuddy 通道，跳过 provider 自动匹配与兜底
            body = {**body, "model": real_model}
            diagnostic("provider_route", provider="codebuddy", model=real_model,
                       protocol=protocol, via="prefix")
            return await _dispatch_once(state, "codebuddy", real_model, body, protocol, original)
        elif prefix in settings_mod.KNOWN_PROVIDER_IDS:
            # 认得出是通道名、但该通道这次没启用（启动没加 --mimo 之类）。
            # 必须在这里报错——否则整个 name 会原样漏到兜底通道，被上游
            # 拒成 11102「service info not found」，看起来像模型名拼错了，
            # 真因（通道没开）完全看不见。
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": f"通道 {prefix} 未启用（当前请求带 {prefix}/ 前缀）。"
                                   f"请在启动时{settings_mod.provider_enable_hint(prefix)}"
                                   f"，然后重启网关。",
                        "type": "provider_disabled",
                    }
                },
            )

    # 2) 候选顺序：为该模型配了 model_order 时按序尝试（未配则整段跳过，行为与历史一致）
    if provider is None:
        order = getattr(state, "model_order", None) or {}
        if isinstance(order, dict):
            owner = _resolve_auto(state, requested_model)
            # 键口径：优先「请求实际解析到的归属」；解析不到时回落到默认通道
            owner_id = owner.id if owner is not None else "codebuddy"
            owner_model = (_resolved_model_id(owner, requested_model)
                           if owner is not None else requested_model)
            # **裸名键优先**：UI 现在写的是裸名（一个模型名一条配置，对所有发布它的
            # 通道都生效），用户手写配置也常用这个形态。归属通道是运行时才知道的实现
            # 细节（同一个名字哪个通道先认领它就归谁），让用户预先写死既反直觉、又会在
            # 解析结果变化时静默失效。
            #
            # 顺序不能反：老配置里可能同时留着 `<通道>/<名>` 的旧键和一版新写的裸键，
            # 先查旧键会让它把新配置**整个遮住**（用户改了顺序却毫无效果）。
            # 按 `requested_model` 与解析后的裸名各试一次。
            bare_keys = []
            for cand in (str(requested_model), owner_model):
                cand = (cand or "").strip()
                if cand and "/" not in cand and cand not in bare_keys:
                    bare_keys.append(cand)
            # 候选键（本函数唯一决定 model_order 用哪条记录的地方）：
            # 先裸名，再退回 `<归属通道>/<名>`（客户端已带前缀时 owner 已被剥）
            keys = [*bare_keys,
                    settings_mod.model_key(owner_id, owner_model),
                    settings_mod.model_key(owner_id, str(requested_model))]
            targets: Any = None
            key = keys[0]
            for candidate in dict.fromkeys(k for k in keys if k):
                hit = order.get(candidate)
                if hit:
                    targets, key = hit, candidate
                    break
            if targets:
                diagnostic("model_order_try", model=key, targets=len(targets))
                return await _forward_with_order(
                    state, key, list(targets), body, protocol, original)

    # 3) 自动匹配模型 id（仅在未显式指定前缀时；前缀路由的结果不能被覆盖）
    if provider is None:
        provider = _resolve_auto(state, requested_model)

    if provider is not None:
        return await _dispatch_once(
            state, provider.id, requested_model, body, protocol, original)

    # 4) 兜底通道：未命中任何 provider 模型列表时，按配置的默认通道转发
    default_provider_id = getattr(state, "default_provider", "codebuddy")
    if default_provider_id in providers:
        diagnostic("provider_route", provider=default_provider_id,
                   model=requested_model, protocol=protocol, via="default")
        return await _dispatch_once(
            state, default_provider_id, requested_model, body, protocol, original)

    # 默认 CodeBuddy 路径（对称封装，与其它 provider 一致）
    return await _dispatch_once(
        state, "codebuddy", requested_model, body, protocol, original)
