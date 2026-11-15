#!/usr/bin/env python3
"""Antigravity 直连实测：TTFT、总耗时、突发频率。

背景
----
`fetchAvailableModels` 的 `remainingFraction` 是**池级水位**，只随「接近配额上限」
下降，完全不反映瞬时排队：2026-11-15 实测 Gemini 组水位 ≈1.0 时
`gemini-3.8-flash-tiered` 仍要等 30~70 秒才出首字。所以「能不能用、快不快」
必须实测，不能看配额条。

本脚本绕过 buddy-proxy、直接打上游，测的是**上游真实表现**（网关自身的开销只有
毫秒级）。用法：

    uv run python tools/bench_antigravity.py ttft gemini-3.8-flash [--n 5] [--conc 1]
    uv run python tools/bench_antigravity.py burst gemini-2.5-flash --rate 10 --secs 5
    uv run python tools/bench_antigravity.py ttft gemini-4-hydrogen-xhigh --account eth

    # 一次跑完一组模型（每个模型各自串行，模型之间串行，避免互相抢池）
    uv run python tools/bench_antigravity.py sweep --models gemini-3.8-flash,gemini-4-hydrogen-xhigh

几个必须知道的坑
----------------
1. **模型名要用上游真名**。`gemini-3.8-flash` 裸名直连是 404，上游认
   `gemini-3.8-flash-tiered`；gemini-4-hydrogen 必须带档位后缀
   （-low/-medium/-high/-xhigh）。网关侧的别名映射见
   `src/buddy_proxy/antigravity/models.json`，这里不复刻映射，直接用上游名。
2. **maxOutputTokens 不能小**。这些模型默认开思考，额度小（512/64）时 thinking
   会把预算吃光，返回空 text + `finishReason: MAX_TOKENS` + `continuationToken`，
   看起来就像「模型不输出」。本脚本固定 4096。
3. 每次调用都消耗上游额度（免费层也是有配额的），`sweep` 默认每模型 5 次。

输出：每个模型的 HTTP 码分布、TTFT p50/p90/min/max、总耗时、最后一次 usage
（含 thoughtsTokenCount，能看出思考量）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402

from buddy_proxy.antigravity.credentials import (  # noqa: E402
    ensure_account_token,
    list_accounts,
)
from buddy_proxy.antigravity.fingerprint import auth_headers  # noqa: E402

HOSTS = [
    "https://daily-cloudcode-pa.googleapis.com",
    "https://cloudcode-pa.googleapis.com",
]
PROMPT = "用中文简单解释什么是 TCP 三次握手，100 字以内。"
MAX_OUTPUT = 4096


def make_body(project_id: str, model: str) -> dict:
    return {
        "project": project_id,
        "model": model,
        "request": {
            "contents": [{"role": "user", "parts": [{"text": PROMPT}]}],
            "generationConfig": {"maxOutputTokens": MAX_OUTPUT, "temperature": 0},
        },
        "userAgent": "antigravity",
        "requestType": "agent",
        "requestId": f"agent-{uuid.uuid4()}",
    }


async def call(client, token, project_id, model, sem) -> dict:
    if sem is not None:
        await sem.acquire()
    try:
        body = make_body(project_id, model)
        headers = auth_headers(token, model)
        headers["Accept"] = "text/event-stream"
        t0 = time.monotonic()
        ttft = None
        text = ""
        usage = None
        last: dict = {}
        for host in HOSTS:
            url = f"{host}/v1internal:streamGenerateContent?alt=sse"
            try:
                async with client.stream("POST", url, json=body, headers=headers) as r:
                    if r.status_code != 200:
                        raw = (await r.aread()).decode("utf-8", "replace")
                        last = {
                            "code": r.status_code,
                            "ttft": None,
                            "total": time.monotonic() - t0,
                            "text": "",
                            "usage": None,
                            "err": raw[:160].replace("\n", " "),
                        }
                        if host == HOSTS[0]:
                            continue  # 换兜底端点再试一次
                        return last
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload or payload == "[DONE]":
                            continue
                        try:
                            obj = json.loads(payload)
                        except ValueError:
                            continue
                        resp = obj.get("response") or obj
                        # usageMetadata 可能在事件顶层，也可能在 response 里。
                        meta = obj.get("usageMetadata") or resp.get("usageMetadata")
                        if meta:
                            usage = meta
                        for cand in resp.get("candidates") or []:
                            for part in (cand.get("content") or {}).get("parts") or []:
                                piece = part.get("text") or ""
                                if piece:
                                    if ttft is None:
                                        ttft = time.monotonic() - t0
                                    text += piece
                return {
                    "code": 200,
                    "ttft": ttft,
                    "total": time.monotonic() - t0,
                    "text": text,
                    "usage": usage,
                    "err": None,
                }
            except Exception as exc:  # noqa: BLE001 - 网络层什么都可能抛
                last = {
                    "code": 599,
                    "ttft": None,
                    "total": time.monotonic() - t0,
                    "text": "",
                    "usage": None,
                    "err": f"{type(exc).__name__}: {exc}",
                }
        return last
    finally:
        if sem is not None:
            sem.release()


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * q), len(ordered) - 1)]


def summary(tag: str, results: list[dict]) -> None:
    ok = [r for r in results if r["code"] == 200 and r["text"].strip()]
    codes: dict[int, int] = {}
    for r in results:
        codes[r["code"]] = codes.get(r["code"], 0) + 1
    print(f"[{tag}] n={len(results)} codes={codes}")
    if ok:
        ttft = [r["ttft"] for r in ok]
        total = [r["total"] for r in ok]
        print(
            f"  TTFT  p50={pct(ttft, 0.5):.2f}s p90={pct(ttft, 0.9):.2f}s "
            f"min={min(ttft):.2f}s max={max(ttft):.2f}s"
        )
        print(
            f"  TOTAL p50={pct(total, 0.5):.2f}s p90={pct(total, 0.9):.2f}s "
            f"min={min(total):.2f}s max={max(total):.2f}s"
        )
        usage = ok[-1]["usage"] or {}
        if usage:
            keys = (
                "promptTokenCount",
                "candidatesTokenCount",
                "thoughtsTokenCount",
                "totalTokenCount",
            )
            print(f"  usage={ {k: usage[k] for k in keys if k in usage} }")
    empty = [r for r in results if r["code"] == 200 and not r["text"].strip()]
    if empty:
        print(f"  ⚠ {len(empty)} 次 200 但正文为空（多半是 MAX_TOKENS 被思考吃光）")
    for r in [r for r in results if r["code"] != 200][:3]:
        print(f"  {r['code']} {r['err']}")
    print(flush=True)


def pick_account(prefix: str | None):
    accounts = list_accounts()
    for acct in accounts:
        if prefix is None or acct.id.startswith(prefix):
            return acct
    names = ", ".join(a.id for a in accounts) or "(none)"
    raise SystemExit(f"没有匹配 {prefix!r} 的账号，现有：{names}")


def make_client() -> httpx.AsyncClient:
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=64)
    return httpx.AsyncClient(limits=limits, timeout=240)


async def run_ttft(token, project_id, model, n, conc) -> None:
    sem = asyncio.Semaphore(conc)
    async with make_client() as client:
        results = await asyncio.gather(
            *[call(client, token, project_id, model, sem) for _ in range(n)]
        )
    summary(f"{model} n={n} conc={conc}", list(results))


async def run_burst(token, project_id, model, rate, secs) -> None:
    """按 rate 条/秒匀速发 total 条（不并发限流，探上游限频天花板）。"""
    total = int(rate * secs)
    async with make_client() as client:
        started = time.monotonic()
        tasks = []
        for i in range(total):
            due = i / rate
            tasks.append(asyncio.ensure_future(call(client, token, project_id, model, None)))
            now = time.monotonic() - started
            if due > now:
                await asyncio.sleep(due - now)
        results = await asyncio.gather(*tasks)
    summary(f"{model} burst rate={rate}/s total={total}", list(results))


async def main_async(args) -> None:
    acct = pick_account(args.account)
    token, cred = await asyncio.to_thread(ensure_account_token, acct.id)
    project_id = cred["project_id"]
    print(f"account={acct.id} project={project_id}", flush=True)

    if args.cmd == "sweep":
        models = [m.strip() for m in (args.models or "").split(",") if m.strip()]
        if not models:
            raise SystemExit("sweep 需要 --models a,b,c")
        for model in models:
            await run_ttft(token, project_id, model, args.n, args.conc)
        return
    if not args.model:
        raise SystemExit(f"{args.cmd} 需要模型名")
    if args.cmd == "ttft":
        await run_ttft(token, project_id, args.model, args.n, args.conc)
    else:
        await run_burst(token, project_id, args.model, args.rate, args.secs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity 上游 TTFT / 频率实测")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("ttft", "burst"):
        p = sub.add_parser(name)
        p.add_argument("model", nargs="?", help="上游真名，如 gemini-3.8-flash-tiered")
        p.add_argument("--account", help="账号 id 前缀，如 eth / slay")
        p.add_argument("--n", type=int, default=5, help="ttft: 请求数（默认 5）")
        p.add_argument("--conc", type=int, default=1, help="ttft: 并发（默认 1）")
        p.add_argument("--rate", type=float, default=1.0, help="burst: 每秒条数")
        p.add_argument("--secs", type=float, default=5.0, help="burst: 持续秒数")
    sweep = sub.add_parser("sweep", help="对一组模型依次跑 ttft")
    sweep.add_argument("--models", required=True, help="逗号分隔的上游模型名")
    sweep.add_argument("--account")
    sweep.add_argument("--n", type=int, default=5)
    sweep.add_argument("--conc", type=int, default=1)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
