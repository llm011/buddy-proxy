"""Raccoon 非流式上游等待期间的下游 SSE 保活。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from buddy_proxy.codebuddy_provider import forward as forward_mod


def _sse(*chunks: str) -> StreamingResponse:
    async def generate():
        for chunk in chunks:
            yield chunk

    return StreamingResponse(generate(), media_type="text/event-stream")


def test_keepalive_precedes_delayed_stream(monkeypatch):
    monkeypatch.setattr(forward_mod, "_RACCOON_KEEPALIVE_INTERVAL_S", 0.001)

    async def respond():
        await asyncio.sleep(0.004)
        return _sse("data: ok\n\n")

    async def run():
        resp = forward_mod._raccoon_keepalive_response(respond(), "openai")
        return [chunk async for chunk in resp.body_iterator]

    chunks = asyncio.run(run())
    assert chunks[0] == ": keepalive\n\n"
    assert chunks[-1] == "data: ok\n\n"
    assert all(not chunk.startswith("data: {") for chunk in chunks)


def test_keepalive_preserves_model_order_failover(monkeypatch):
    monkeypatch.setattr(forward_mod, "_RACCOON_KEEPALIVE_INTERVAL_S", 0.001)
    monkeypatch.setattr(forward_mod.cooldown_mod, "is_marked", lambda *args: False)
    monkeypatch.setattr(forward_mod.cooldown_mod, "mark_failed", lambda *args, **kwargs: None)
    monkeypatch.setattr(forward_mod, "_target_blocked", lambda *args: False)
    monkeypatch.setattr(forward_mod, "diagnostic", lambda *args, **kwargs: None)
    calls = []

    async def dispatch(state, provider_id, model, body, protocol, original):
        calls.append(provider_id)
        if provider_id == "raccoon":
            await asyncio.sleep(0.004)
            raise HTTPException(status_code=429, detail={"error": {"message": "limited"}})
        return _sse("data: fallback\n\n")

    monkeypatch.setattr(forward_mod, "_dispatch_once", dispatch)
    state = SimpleNamespace(
        providers={"raccoon": object(), "trae": object()},
        disabled_providers=set(), model_schedules={})

    async def run():
        forwarding = forward_mod._forward_with_order(
            state, "kimi", ["raccoon/kimi", "trae/kimi"], {"stream": True}, "openai", None)
        resp = forward_mod._raccoon_keepalive_response(forwarding, "openai")
        return [chunk async for chunk in resp.body_iterator]

    chunks = asyncio.run(run())
    assert calls == ["raccoon", "trae"], chunks
    assert chunks[0] == ": keepalive\n\n"
    assert chunks[-1] == "data: fallback\n\n"


def test_keepalive_reports_pre_response_error_in_band(monkeypatch):
    monkeypatch.setattr(forward_mod, "_RACCOON_KEEPALIVE_INTERVAL_S", 0.001)

    async def fail():
        await asyncio.sleep(0.003)
        raise HTTPException(
            status_code=502, detail={"error": {"message": "upstream unavailable"}})

    async def run():
        resp = forward_mod._raccoon_keepalive_response(fail(), "openai")
        return [chunk async for chunk in resp.body_iterator]

    chunks = asyncio.run(run())
    assert chunks[0] == ": keepalive\n\n"
    assert "upstream unavailable" in chunks[-2]
    assert chunks[-1] == "data: [DONE]\n\n"


def test_keepalive_cancels_pending_request_on_disconnect(monkeypatch):
    monkeypatch.setattr(forward_mod, "_RACCOON_KEEPALIVE_INTERVAL_S", 0.001)
    cancelled = asyncio.Event()

    async def respond():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def run():
        resp = forward_mod._raccoon_keepalive_response(respond(), "openai")
        stream = resp.body_iterator
        assert await anext(stream) == ": keepalive\n\n"
        await stream.aclose()
        await asyncio.wait_for(cancelled.wait(), timeout=0.1)

    asyncio.run(run())


def test_forward_chat_enables_keepalive_for_raccoon_model_order(monkeypatch):
    monkeypatch.setattr(forward_mod, "_RACCOON_KEEPALIVE_INTERVAL_S", 0.001)
    state = SimpleNamespace(
        providers={}, disabled_providers=set(), model_schedules={},
        model_order={"kimi": ["raccoon/kimi", "trae/kimi"]},
        default_model=None, default_provider="codebuddy",
    )
    monkeypatch.setattr(forward_mod, "get_state", lambda: state)
    monkeypatch.setattr(forward_mod, "diagnostic", lambda *args, **kwargs: None)

    async def ordered(*args):
        await asyncio.sleep(0.003)
        return _sse("data: routed\n\n")

    monkeypatch.setattr(forward_mod, "_forward_with_order", ordered)

    async def run():
        resp = await forward_mod.forward_chat(
            {"model": "kimi", "stream": True, "messages": []}, "openai")
        return [chunk async for chunk in resp.body_iterator]

    chunks = asyncio.run(run())
    assert chunks[0] == ": keepalive\n\n"
    assert chunks[-1] == "data: routed\n\n"
