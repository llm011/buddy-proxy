"""Trae request orchestration, regional account failover and protocol dispatch."""

from __future__ import annotations

from typing import Any
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse


class ForwardMixin:
    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        from . import provider as hooks
        requested_model = body.get("model", "auto")
        messages = body.get("messages", [])
        stream = bool(body.get("stream", False))
        # coding agent 请求：不注入 guard、不清洗——下游自己解析工具调用语法
        agent_mode = hooks._looks_like_agent_request(messages, body)
        hooks._debug_dump(
            "debug_trae_route",
            model=requested_model,
            agent_mode=agent_mode,
            tools_count=len(body.get("tools") or []),
            stream=stream,
            message_count=len(messages),
        )

        tools = body.get("tools") or []
        # 原生通道：全部请求（含纯聊天）默认走 chat_v3 直通——该通道无 agent
        # 预设，纯聊天不再需要 guard 注入与泄漏清洗。上游 4001 拒绝时自动回落
        # 文本协议（prompt 始终照算，它就是兜底路径的输入：纯聊天带 guard、
        # 工具请求带教学）。
        native_mode = self._uses_native_mode()
        prompt = hooks._extract_prompt(
            messages, guard=not agent_mode, tools=tools if agent_mode else None
        )
        if not prompt:
            raise hooks.HTTPException(status_code=400, detail="no text content")
        native = (
            {"messages": hooks._native_messages(messages), "tools": tools}
            if native_mode else None
        )
        # Max 模式：本请求是否向上游注入 is_max_mode。在 forward 顶部算一次
        # （failover 换号时模型不变，决策也不变），统一下传给 _stream/_collect
        # 里所有上游发送调用——保证窗口声明（models()）与实际注入一致，
        # 见 max_mode.py 模块 docstring 的强耦合不变量。
        max_inject = self._max_inject(requested_model)

        # ---- 多账号 failover：按顺位试可用 work 账号，账号级错误冷却换号 ----
        # PAT 子类（TraePatProvider）覆写了发送路径且有自己的账号体系，不该被卷
        # 进 work 多账号循环——直接单次转发（保持它原有的单账号语义）。
        if self._is_pat_variant():
            hooks.set_current_work_account(None)
            try:
                return await self._forward_once(
                    body, protocol, original, requested_model, prompt,
                    messages, tools, native, stream, agent_mode, max_inject,
                )
            finally:
                hooks.set_current_work_account(None)

        # 只在本区账号间 failover——CN / 海外账号不通用（连错域 401）。
        # **不做** ``or available_accounts()`` 全量兜底：那会让 CN 通道在只有海外
        # 账号时捞到海外账号（反之亦然），每轮白付一次 401。现有账号的 region
        # 已由 list_accounts 归一成 "cn"（credentials.py），过滤本身就覆盖到它们。
        # 空列表时区分「本区无账号但别区有」（401，配置问题）与「全在冷却」（429）。
        accounts = hooks.failover.available_accounts(self._region.key)
        # 负载均衡（额度页开关）：只改「先试谁」，region/冷却过滤已完成。
        accounts = hooks.lb_mod.reorder(hooks._lb_state(), self.id, accounts,
                                  model=str(requested_model or "") or None)
        # 剔除对本模型「套餐权益不足」(1005) 的账号（见 _mark_entitlement_skip）。
        # 至少保留一个：全被跳过时宁可重试（TTL 可能已过期 / 套餐可能已升级），
        # 也不落进下面「无可用账号」的 429/401 误报。
        # requested_model 可能为 None（见 _internal_model_key），统一走 helper 兜底。
        entitlement_model = hooks._internal_model_key(self._region.key, requested_model)
        # 本模型近期全员 4026（上下文太大）：TTL 内不再 fan-out，直接抛让客户端压缩。
        # 放在选号过滤之前——跳过表全满时过滤会回退全量，每个请求都白打 N 次。
        if hooks._model_overflow_recently(entitlement_model):
            # detail 用 OpenAI 形状：客户端靠 error.type 决定**动作**。裸字符串
            # 只能让它看到一句「没权限」，不会去压缩上下文——而这条错误的正确
            # 处置恰恰就是压缩后重试。
            raise hooks._attach_sse_code(
                hooks.HTTPException(status_code=403, detail={"error": {
                    "message": ("Trae 错误: 本模型近期所有账号均上下文超限（4026），"
                                "请压缩上下文后重试"),
                    "type": "context_length_exceeded",
                }}), hooks._CONTEXT_OVERFLOW_SSE_CODE)
        if len(accounts) > 1:
            eligible = [a for a in accounts
                        if not hooks._entitlement_skipped(a.id, entitlement_model)]
            if eligible:
                accounts = eligible
        if not accounts:
            other = hooks.failover.available_accounts()
            if other:
                raise hooks.HTTPException(
                    status_code=401,
                    detail={"error": {
                        "message": (f"trae 无 {self._region.label} 区域的可用账号"
                                    f"（已有 {len(other)} 个账号属于其它区域，"
                                    f"跨区账号间不自动切换）"),
                        "type": "authentication_error"}})
            raise hooks.HTTPException(
                status_code=429,
                detail={"error": {
                    "message": (f"trae work 所有账号均在冷却中：{hooks.failover.cooldown_report()}"
                                "；额度冷却到点自动恢复"),
                    "type": "rate_limit_error"}})
        last_exc: BaseException | None = None
        try:
            for i, acct in enumerate(accounts):
                hooks.set_current_work_account(acct.id)
                # metrics 账号归属（/ui 请求日志「通道」列的账号后缀）：qoder/kimi/
                # antigravity 同款——选号即写，failover 后被覆盖为最终账号。holder
                # 是 codebuddy observability._instrument 放入的 dict，离线/测试等
                # 未经 _instrument 的场景为 None，直接跳过。PAT 子类不走这里
                # （pat/chat.py 自己写 meta["account"]）。
                # 落库统一写稳定 id，名字由展示端按当前 alias 链解析
                # （web/ui/queries._account_name_map）——写死名字的话改名后
                # 历史日志行跟不上。
                meta = hooks.ACCOUNT_META.get()
                if meta is not None:
                    meta["account"] = acct.id
                try:
                    return await self._forward_once(
                        body, protocol, original, requested_model, prompt,
                        messages, tools, native, stream, agent_mode, max_inject,
                    )
                except hooks.HTTPException as e:
                    last_exc = e
                    # 账号级错误（401 凭据失效 / 429 额度——含上游额度码映射
                    # 而来的 429，如 4008/4011/4021/4031）：**先冷却再决定换不
                    # 换**。此前只在前头还有账号时才冷却，最后一个账号失败不落
                    # 冷却——下一轮请求还会先打它、再白吃一次同样的失败才轮到
                    # 别人。其余错误（502 通道级/业务 4xx）换号无意义，直接透传。
                    if self._is_account_error(e):
                        if hooks._is_entitlement_error(e) or hooks._is_context_overflow(e):
                            # 1005 / 4026 都是「账号+模型」级：换号有意义，但只记
                            # 本模型跳过、**不**整账号冷却——该账号对其他模型仍可
                            # 服务（免费号不能用 glm-5.4，glm-5.3 照常；4026 则是
                            # 这次上下文太长，短请求照样能过）。
                            hooks._mark_entitlement_skip(
                                acct.id, entitlement_model,
                                ttl=hooks._CONTEXT_OVERFLOW_SKIP_TTL_S if hooks._is_context_overflow(e) else None)
                        else:
                            hooks.failover.mark_cooldown(
                                acct.id, quota=e.status_code == 429,
                                reason=f"HTTP {e.status_code}: {str(e.detail)[:120]}")
                        if i < len(accounts) - 1:
                            continue
                    raise
                finally:
                    hooks.set_current_work_account(None)
        except hooks.HTTPException as e:
            # 走到这是「最后一个账号也失败」或「非账号级错误透传」。anthropic 协议
            # 的错误体用标准形状（Claude Code 等 SDK 靠它渲染错误）；openai 协议
            # 维持「抛 HTTPException → 外层 exception handler 转 JSON」不变。
            # 上下文超限（4026）打到**最后一个账号还在拒**：本模型此刻对所有账号
            # 都太长，记模型级短标记——TTL 内后续请求直接抛，不再挨个 fan-out
            # （上下文太大时每个账号都注定 4026，重复打只是白付往返）。
            if hooks._is_context_overflow(e):
                hooks._mark_model_overflow(entitlement_model)
            if protocol == "anthropic":
                return hooks.JSONResponse(
                    status_code=e.status_code,
                    content={
                        "type": "error",
                        "error": {"type": "api_error", "message": str(e.detail)},
                    },
                )
            raise
        # 全部账号失败（循环正常跑完不该到这，兜底）
        raise last_exc or hooks.HTTPException(status_code=429, detail=hooks.failover.cooldown_report())

    def _is_pat_variant(self) -> bool:
        """PAT 子类标记：TraePatProvider 覆写 ``_pat_variant=True``（见
        pat_provider.py）。work 多账号 failover 只服务个人 work 通道。"""
        from . import provider as hooks
        return bool(getattr(self, "_pat_variant", False))

    @staticmethod
    def _is_account_error(e: HTTPException) -> bool:
        """该 HTTPException 是否「换下一个账号可能好转」。

        401 = 凭据失效（换号有意义）；403/429 = 额度/权益/限流（换号有意义
        ——transport/native_tools 透传上游 403 后这里必须接住，否则它穿透
        failover 直接冒给客户端且坏账号永不被冷却；与 failover.py 模块注释
        的「401 与 403/429」口径一致）。502/504 是通道级（换号无意义，所有
        账号同网关）。其余业务 4xx 换号也无意义。
        """
        from . import provider as hooks
        return e.status_code in (401, 403, 429)

    async def _forward_once(
        self, body: dict[str, Any], protocol: str, original: dict[str, Any] | None,
        requested_model: str, prompt: Any, messages: list[dict[str, Any]],
        tools: list[dict[str, Any]], native: dict[str, Any] | None,
        stream: bool, agent_mode: bool, max_inject: bool,
    ) -> StreamingResponse | JSONResponse:
        """单账号实际转发（forward 的 failover 循环体）。"""
        from . import provider as hooks
        if stream:
            include_usage = bool(
                (body.get("stream_options") or {}).get("include_usage"))
            # anthropic 协议没有 stream_options 字段，但 message_delta 需要
            # usage（Claude Code 靠它统计 token），这里强制向 _stream 索取
            if protocol == "anthropic":
                include_usage = True
            raw_gen = self._stream(prompt, requested_model,
                                   sanitize=not agent_mode, tools=tools or None,
                                   include_usage=include_usage, native=native,
                                   max_inject=max_inject)
            # 首事件闸门：预驱动**原始** _stream 到第一个非心跳事件再决定放行
            # 还是换号。trae 的 _stream 在等上游时会先吐「: heartbeat」保活——
            # 那是给下游超时看的，此刻 StreamingResponse 还没建、客户端一个字节
            # 都没收到，丢掉心跳无影响；第一个语义事件前的账号级错误（读线程透
            # 传）此刻抛出可安全换号（还没向客户端吐过内容，防重复计费）。见到
            # 语义事件即 committed，绝不重放。anthropic 包装在闸门之后（包住
            # 「缓冲 + 续跑」的拼接流）。
            # 闸门会同步调用 next(_stream)，而 _stream 首事件前可能等上游很久。
            # 不能在唯一的 asyncio loop 上预驱动，否则一次 Trae 慢响应就拖住
            # 管理页和其它通道的所有请求。
            gated = await hooks._gate_first_event_async(raw_gen)
            if isinstance(gated, BaseException):
                raise gated
            replay = hooks._replay_prefixed(gated.buffered, gated.gen)
            gen = hooks._wrap_anthropic_stream(replay, requested_model) \
                if protocol == "anthropic" else replay
            return hooks.StreamingResponse(
                hooks._sync_to_async_iter(gen),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "close"},
            )
        # 非流式聚合内部是同步 urllib 调用（最长 180s），放线程池执行，
        # 避免阻塞事件循环拖垮所有并发请求。
        # 注意：这里的 HTTPException **必须 raise**（不能 return JSONResponse）——
        # forward 的 failover 循环靠捕获它来判断账号级错误（401/429）冷却换号；
        # 若在这里吞成返回值，撞错的账号不会被冷却、也不会换下一个账号。
        collected = await hooks.asyncio.to_thread(
            self._collect, prompt, requested_model, not agent_mode, tools or None,
            native, max_inject,
        )
        if protocol == "anthropic":
            collected = hooks.chat_completion_to_anthropic_message(collected, original)
        return hooks.JSONResponse(content=collected)
