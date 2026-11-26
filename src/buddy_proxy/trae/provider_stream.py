"""Trae native/text SSE delivery, reader isolation and semantic timeout."""

from __future__ import annotations

from typing import Any, AsyncIterator


class StreamMixin:
    def _stream(
        self, messages: list[dict[str, Any]], model: str, sanitize: bool = True,
        tools: list[dict[str, Any]] | None = None,
        include_usage: bool = False,
        native: dict[str, Any] | None = None,
        max_inject: bool = False,
    ) -> AsyncIterator[str]:
        """把 Trae SSE 转成 OpenAI chat.completion.chunk 流。

        ``max_inject``：本次是否向上游注入 ``is_max_mode``（forward 决策后下传，
        与 models() 声明窗口一致，见 max_mode.py）。读线程内所有上游发送调用
        （native 增量 / native 非流式回落 / 文本协议兜底）都带同一值，保证
        failover 回落时窗口口径不漂移。
        """
        from . import provider as hooks
        request_id = f"chatcmpl-{hooks.uuid.uuid4().hex[:24]}"
        created = int(hooks.time.time())
        usage: dict[str, Any] | None = None
        stop_signal: hooks.threading.Event | None = None
        last_semantic_at = hooks.time.monotonic()

        def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
            payload = {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {hooks.json.dumps(payload, ensure_ascii=False)}\n\n"

        def usage_chunk() -> str:
            # OpenAI 流式协议：stream_options.include_usage 时，[DONE] 前须有一个
            # choices 为空、只带 usage 的收尾 chunk。下游客户端（如 ethan）靠它
            # 判定 is_final——缺失会导致 final chunk 永远不到、tool_calls 整体丢失
            payload = {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [],
                "usage": usage or {
                    "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                },
            }
            return f"data: {hooks.json.dumps(payload, ensure_ascii=False)}\n\n"

        def error_chunk(msg: str, code: Any = None) -> str:
            # 结构化错误 chunk（与 CodeBuddy 通道 stream_upstream 的错误格式一致）。
            # 不能塞进 content 文本——anthropic 客户端（Claude Code）会把
            # 伪正文当模型输出继续循环；包装层据此转成 event: error
            err: dict[str, Any] = {"message": msg, "type": "upstream_error"}
            if code is not None:
                err["code"] = code
            return f"data: {hooks.json.dumps({'error': err}, ensure_ascii=False)}\n\n"

        try:
            # 上游调用放独立读线程：send_trae_chat 是同步整段缓冲读（最长受
            # 上游 socket 180s 超时约束），原实现直接在生成器线程里阻塞读——
            # 生成期间客户端收不到任何字节，长生成（深度 review 大报告等）会
            # 触发下游单 chunk 超时（实测 Ethan _CHUNK_TIMEOUT=120s）→ 回合
            # 中止、落库空回复。拆成读线程 + 带 timeout 的队列等待后，等待
            # 间隙按 TRAE_HEARTBEAT_INTERVAL 发 reasoning 心跳：喂饱下游超时
            # 计时器（续命），也让下游 UI 知道中转还在等上游。
            _ev_q: hooks.queue.Queue = hooks.queue.Queue()
            _stop = hooks.threading.Event()
            stop_signal = _stop
            incremental = False

            def _read_upstream() -> None:
                try:
                    if native is not None:
                        # 惩罚窗内不碰流式（增量或整段都一样必败）：直接落到
                        # 下面的非流式缓冲分支，省一次注定失败的往返，也不
                        # 再喂养惩罚窗口。增量路径同样受这条 TTL 管辖。
                        event_source = (
                            None if (not self._keeps_native_error(model)
                                     and hooks._stream_rejected_recently(model))
                            else self._stream_native_events(
                                native["messages"], model, native["tools"], _stop,
                                max_mode=max_inject)
                        )
                        if event_source is not None:
                            try:
                                for upstream_event in event_source:
                                    if _stop.is_set():
                                        return
                                    _ev_q.put(("event", upstream_event))
                            except hooks._NativeStreamRejected:
                                # 上游整体拒绝流式且零产出：此时客户端一个字节
                                # 都还没收到，安全回落到下面的整段兼容路径
                                # （非流式 → 4001 节流重试 → 文本协议）。
                                # 已出过字的情况在事件源里就转成 error 事件透传，
                                # 不会走到这里——重放会让客户端看到重复内容。
                                hooks.log.warning(
                                    "trae native incremental stream rejected "
                                    "(4001), fall back to buffered path: model=%s",
                                    model)
                                hooks._debug_dump("trae_native_fallback", model=model,
                                            phase="incremental")
                                hooks._mark_stream_rejected(model)
                                hooks._pace_after_reject(model)
                            else:
                                _ev_q.put(("eof", None))
                                return
                        skip_stream = (not self._keeps_native_error(model)
                                       and hooks._stream_rejected_recently(model))
                        if skip_stream:
                            # TTL 内：跳过必败的流式尝试，直接非流式缓冲（省一次
                            # 注定失败的往返，也不再喂养惩罚窗口）；若最近刚有过
                            # 4001，先安静等到窗外
                            hooks.log.info(
                                "trae native stream rejected recently, "
                                "skip to nonstream: model=%s", model)
                            hooks._pace_after_reject(model)
                            raw_text = self._send_native_request(
                                native["messages"], model, stream=False,
                                tools=native["tools"], max_mode=max_inject)
                        else:
                            raw_text = self._send_native_request(
                                native["messages"], model, stream=True,
                                tools=native["tools"], max_mode=max_inject)
                        used = True
                        if hooks._native_rejected(raw_text) and not self._keeps_native_error(model):
                            # 上游会整体拒绝 chat_v3 流式请求（2026-10-07 实测：同
                            # body 非流式 200、流式 4001，且文本协议兜底对 GPT-6 系
                            # 的 solo_work_lite 也 4001）。先降级 native 非流式重试
                            # ——非流式响应本就是 SSE 帧形态，整段缓冲后走同一解析
                            # 路径。
                            # 注意每一跳前必须**节流**：上游对刚失败（4001）的账号
                            # 有快速重试惩罚，且窗口随连续失败拉长（~0.2s 必拒，
                            # 累积失败后 ~15-20s 才恢复）。固定短退避会在窗内连环
                            # 开火、越喂越长；改为记住最近 4001 时刻，安静等到窗外
                            # 再发——等待不产生失败，窗口只衰减不增长。读线程里睡，
                            # 心跳照常喂下游，客户端无感。
                            if not skip_stream:
                                hooks.log.warning(
                                    "trae native stream rejected (4001), "
                                    "retry nonstream: model=%s", model)
                                hooks._debug_dump("trae_native_fallback", model=model,
                                            phase="stream")
                                hooks._mark_stream_rejected(model)
                                hooks._pace_after_reject(model)
                                raw_text = self._send_native_request(
                                    native["messages"], model, stream=False,
                                    tools=native["tools"], max_mode=max_inject)
                            if hooks._native_rejected(raw_text):
                                # 非流式也被拒（落在惩罚窗内）：安静等到窗外原路
                                # 再试——文本协议对 GPT-6 系已死，能用非流式救回就
                                # 别落文本多喂一次必败失败
                                hooks.log.warning(
                                    "trae native nonstream rejected (4001), "
                                    "paced retry: model=%s", model)
                                hooks._note_reject(model)
                                hooks._pace_after_reject(model)
                                raw_text = self._send_native_request(
                                    native["messages"], model, stream=False,
                                    tools=native["tools"], max_mode=max_inject)
                            if hooks._native_rejected(raw_text):
                                hooks.log.warning(
                                    "trae native nonstream rejected twice (4001), "
                                    "fallback to text protocol: model=%s", model)
                                hooks._note_reject(model)
                                hooks._pace_after_reject(model)
                                raw_text = hooks.send_trae_chat(
                                    messages, model, stream=False,
                                    base_url=self._base_url, max_mode=max_inject)
                                used = False
                            else:
                                used = True
                        if used:
                            hooks._note_native_ok(model)
                        _ev_q.put(("raw", (raw_text, used)))
                    else:
                        _ev_q.put(("raw", (hooks.send_trae_chat(
                            messages, model, stream=True,
                            base_url=self._base_url, max_mode=max_inject), False)))
                except BaseException as e:  # noqa: BLE001 — 原样转主线程抛出
                    _ev_q.put(("error", e))

            # 读线程以请求上下文的副本运行：ContextVar 赋值不跨线程，PAT 子类的
            # 发送钩子靠这份副本读到请求级账号 holder（ACCOUNT_META）并上报账号
            _request_ctx = hooks.contextvars.copy_context()
            hooks.threading.Thread(target=_request_ctx.run, args=(_read_upstream,),
                             daemon=True, name="trae-sse-reader").start()
            raw: str | None = None
            used_native = native is not None
            _waited = 0
            pending_event: tuple[str, dict[str, Any]] | None = None
            while raw is None and pending_event is None:
                semantic_left = hooks.TRAE_SEMANTIC_TIMEOUT - (hooks.time.monotonic() - last_semantic_at)
                timeout = (min(hooks.TRAE_HEARTBEAT_INTERVAL, semantic_left)
                           if hooks.TRAE_HEARTBEAT_INTERVAL > 0 else semantic_left)
                try:
                    kind, payload = _ev_q.get(timeout=max(0.001, timeout))
                except hooks.queue.Empty:
                    idle_for = hooks.time.monotonic() - last_semantic_at
                    _waited += max(0, timeout)
                    if idle_for >= hooks.TRAE_SEMANTIC_TIMEOUT:
                        _stop.set()
                        raise hooks.HTTPException(
                            status_code=504,
                            detail=f"trae stream produced no content for {round(idle_for)}s")
                    hooks._debug_dump("trae_heartbeat", waited=round(_waited))
                    if hooks.TRAE_HEARTBEAT_INTERVAL > 0:
                        yield ": heartbeat\n\n"
                    continue
                if kind == "error":
                    raise payload
                if kind == "eof":
                    raise hooks.HTTPException(status_code=502, detail="trae stream returned no content")
                if kind == "event":
                    incremental = True
                    pending_event = payload
                else:
                    raw, used_native = payload
            acc = hooks._NativeToolAccumulator() if used_native else None
            cleaner = (
                hooks._StreamLeakCleaner() if sanitize and not used_native else None
            )
            splitter = (
                hooks._StreamToolCallSplitter(hooks._tool_names(tools))
                if tools and not used_native else None
            )
            dbg_parts: list[str] = []
            semantic_seen = False
            done_seen = False
            event_iter = iter((pending_event,)) if incremental else iter(hooks._parse_sse(raw or ""))
            while True:
                try:
                    item = next(event_iter)
                except StopIteration:
                    if not incremental:
                        break
                    semantic_left = hooks.TRAE_SEMANTIC_TIMEOUT - (hooks.time.monotonic() - last_semantic_at)
                    timeout = (min(hooks.TRAE_HEARTBEAT_INTERVAL, semantic_left)
                               if hooks.TRAE_HEARTBEAT_INTERVAL > 0 else semantic_left)
                    try:
                        kind, payload = _ev_q.get(timeout=max(0.001, timeout))
                    except hooks.queue.Empty:
                        idle_for = hooks.time.monotonic() - last_semantic_at
                        _waited += max(0, timeout)
                        if idle_for >= hooks.TRAE_SEMANTIC_TIMEOUT:
                            _stop.set()
                            raise hooks.HTTPException(
                                status_code=504,
                                detail=f"trae stream produced no content for {round(idle_for)}s")
                        hooks._debug_dump("trae_heartbeat", waited=round(_waited))
                        if hooks.TRAE_HEARTBEAT_INTERVAL > 0:
                            yield ": heartbeat\n\n"
                        continue
                    if kind == "error":
                        raise payload
                    if kind == "eof":
                        if not semantic_seen:
                            raise hooks.HTTPException(status_code=502, detail="trae stream returned no content")
                        if not done_seen:
                            raise hooks.HTTPException(status_code=502, detail="trae stream ended before completion")
                        break
                    if kind != "event":
                        raise RuntimeError("unexpected trae stream message")
                    item = payload
                event, data = item
                if event == "output" and (
                    data.get("reasoning_content") or data.get("response") or data.get("tool_calls")
                ):
                    semantic_seen = True
                    last_semantic_at = hooks.time.monotonic()
                    _waited = 0
                if event == "done":
                    done_seen = True
                if event == "error":
                    yield error_chunk(hooks._trae_error_text(data), (data or {}).get("code"))
                    yield "data: [DONE]\n\n"
                    return
                if event == "output":
                    if data.get("reasoning_content"):
                        yield chunk({"reasoning_content": data["reasoning_content"]})
                    if acc is not None:
                        if data.get("tool_calls"):
                            acc.feed(data["tool_calls"])
                        text = data.get("response") or ""
                        if text:
                            dbg_parts.append(text)
                            yield chunk({"role": "assistant", "content": text})
                    elif data.get("response"):
                        text = data["response"]
                        if cleaner is not None:
                            text = cleaner.feed(text)
                        elif splitter is not None:
                            text = splitter.feed(text)
                        if text:
                            dbg_parts.append(text)
                            yield chunk({"role": "assistant", "content": text})
                elif event == "token_usage":
                    u = data or {}
                    usage = hooks.normalize_token_usage(u)
                elif event == "done":
                    done_seen = True
                    break
            if not semantic_seen:
                raise hooks.HTTPException(status_code=502, detail="trae stream returned no content")
            if incremental and not done_seen:
                raise hooks.HTTPException(status_code=502, detail="trae stream ended before completion")
        except GeneratorExit:
            if stop_signal is not None:
                stop_signal.set()
            raise
        except hooks.HTTPException as e:
            if stop_signal is not None:
                stop_signal.set()
            yield error_chunk(f"trae error {e.status_code}: {e.detail}", e.status_code)
            yield "data: [DONE]\n\n"
            return
        except Exception as e:
            if stop_signal is not None:
                stop_signal.set()
            hooks.log.error("Trae stream error: %s", e)
            yield error_chunk(f"trae stream error: {e}")
            return

        if stop_signal is not None:
            stop_signal.set()
        finish = "stop"
        dbg_calls: list[dict[str, Any]] = []
        if used_native:
            calls = acc.finish()
            if calls:
                finish = "tool_calls"
                dbg_calls = calls
                # OpenAI 流式协议：tool_call 必须带 index（客户端靠它合并分片），
                # 首个 delta 带 role；不带 index 会被部分解析器直接丢弃
                yield chunk({"role": "assistant", "tool_calls": calls})
        elif cleaner is not None:
            tail = cleaner.flush()
            if tail:
                dbg_parts.append(tail)
                yield chunk({"role": "assistant", "content": tail})
        elif splitter is not None:
            tail, calls = splitter.flush()
            if tail:
                dbg_parts.append(tail)
                yield chunk({"role": "assistant", "content": tail})
            if calls:
                finish = "tool_calls"
                dbg_calls = calls
                # OpenAI 流式协议：tool_call 必须带 index（客户端靠它合并分片），
                # 首个 delta 带 role；不带 index 会被部分解析器直接丢弃
                yield chunk({"role": "assistant", "tool_calls": [
                    {"index": i, "id": c["id"], "type": "function",
                     "function": c["function"]}
                    for i, c in enumerate(calls)
                ]})
        hooks._debug_dump("debug_trae_response", model=model, stream=True,
                    content="".join(dbg_parts),
                    tool_calls=[
                        {"name": c["function"]["name"],
                         "arguments": c["function"]["arguments"]}
                        for c in dbg_calls
                    ])
        yield chunk({}, finish)
        if include_usage:
            yield usage_chunk()
        yield "data: [DONE]\n\n"
