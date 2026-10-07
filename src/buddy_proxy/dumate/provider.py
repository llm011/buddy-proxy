"""百度搭子（DuMate）provider。

直连本机 DuMate.app 内置的本地 OpenAI 兼容代理（见 ``dumate/discovery.py``），
把请求原样透传给上游 ``dumate-svc.baidu.com`` 网关，无需云端 token。

协议与路由（与 zcode/doubao provider 的多协议约定一致）：
- openai（/v1/chat/completions）→ 透传本地代理，SSE/JSON 原样回传；
- responses（/v1/responses）→ 经通用链路（responses_adapter 转成 chat 后
  落到 openai 路径）；
- anthropic（/v1/messages）→ 网关只认 OpenAI chat 形状，路由层已把 anthropic
  请求体转成 chat 传进来；这里把 OpenAI 响应包一层转回 anthropic（流式经
  ``_to_anthropic_stream`` / 非流式经 ``chat_completion_to_anthropic_message``，
  kimi/mimo 同款），Claude Code 等 /v1/messages 客户端可直接使用。

模型：上游无 /v1/models 列表接口，模型名从 DuMate 客户端真实流量里抓包
实测（见下方 ``_MODELS`` 的注释）。``dm-auto-model/text.L0`` 是搭子的智能路由
档，``glm-5`` / ``qwen3.5-35b-a3b`` 是具体模型，``model-text`` 是内部工具模型。

额度：桌面端「积分」面板同口径走 bceConsole
``GET /api/dumate/points/quota_overview``（数字余额 + 积分包明细）；本地代理的
``/api/dumate/points/remaining`` 只回布尔、作为未登录兜底。
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..providers.base import BaseProvider
from ..core.errors import describe_exception
from . import discovery

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)

# 上游实测可用的模型名（2026-10 抓包 DuMate 客户端真实流量确认）。
# id 一律小写，与全站 models_config.json 命名一致，便于 forward_chat 的
# 「自动匹配 provider.models()」命中本通道，不至于让请求漏到 codebuddy 兜底。
# - dm-auto-model/text.L0：搭子「自动」智能路由档（按请求自动选底层模型），
#   是 App 默认对话模型；对应客户端配置里的 model-text / QianfanPersonalQuota。
# - glm-5 / qwen3.5-35b-a3b：直连具体模型（抓包确认网关接受这些 model 字段）。
# - model-artifact-validate：产物校验工具模型（DuMate opencode.json 内置）。
#
# 注意：id 里**故意保留** ``/``（如 dm-auto-model/text.L0）。BaseProvider
# 约定 provider id 不含 ``/``，但**模型名**不受此限；客户端请求时用
# ``dumate/dm-auto-model/text.L0`` 或让 model_order 解析，剥掉 ``dumate/``
# 前缀后剩下的 ``dm-auto-model/text.L0`` 会原样透传给上游。
_MODELS: dict[str, dict[str, Any]] = {
    "dm-auto-model/text.L0": {
        "name": "百度搭子自动（智能路由）",
        "desc": "DuMate 自动档：按请求智能路由底层模型，App 默认对话模型",
        "reasoning": True,
        "tool_call": True,
    },
    # 2026-10-05 实测确认可路由（抓包 + 逐个试）：deepseek-v4-flash 在网关模型
    # 表里但 401（需内部授权，本账号未开通），deepseek-v4.1 / glm-5.3 / kimi-k2.7
    # 等新款均 invalid_model——DuMate 当前模型池就是 GLM + Qwen + Kimi 这批。
    # 同档模型只保留最新款（用户 2026-10-05：有新模型旧模型就不配上）。
    "kimi-k3": {
        "name": "Kimi K3",
        "desc": "Moonshot Kimi K3（DuMate 直连）",
        "reasoning": True,
        "tool_call": True,
    },
    "qwen3.8-max": {
        "name": "Qwen3.8-Max",
        "desc": "通义千问 3.8 Max（DuMate 直连）",
        "reasoning": True,
        "tool_call": True,
    },
}


class DumateProvider(BaseProvider):
    id = "dumate"
    name = "百度搭子 (DuMate 本地代理)"
    supports_checkin = True  # 每日签到（bceConsole 通道，自动打卡循环消费）

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # 上次成功发现/探测的端点（health 展示用，不做强缓存——key 每次
        # App 重启都轮换，缓存了反而会拿旧 key 打新进程）。
        self._last_ep: discovery.DumateEndpoint | None = None

    # ------------------------------------------------------------------
    # BaseProvider 接口
    # ------------------------------------------------------------------

    def models(self) -> Sequence[dict[str, Any]]:
        return [
            {
                "id": mid,
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "name": spec["name"],
                "description": spec["desc"],
                "reasoning": spec["reasoning"],
                "tool_call": spec.get("tool_call", False),
                # 上游 opencode.json 标注的上下文/输出上限（192k / 128k）
                "max_input": 192000,
                "max_output": 128000,
            }
            for mid, spec in _MODELS.items()
        ]

    def ensure_auth(self) -> None:
        """确保 DuMate 本地代理可用；失败抛 401/503 带可操作提示。

        与 doubao 一样，这里做**软检查**（只发现、不发请求），真正的连通性由
        ``forward`` 发首个字节前的探测确认，避免在同步 ensure_auth 里阻塞。
        """
        ep = discovery.discover()
        if ep is None:
            state = discovery.describe_state()
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": (
                            "dumate 通道不可用：" + (state.get("hint") or "未找到运行中的 DuMate 本地代理")
                        ),
                        "type": "authentication_error",
                        "hint": state.get("hint", ""),
                        "installed": state.get("installed", False),
                    }
                },
            )
        self._last_ep = ep

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        if protocol == "anthropic":
            # DuMate 网关只认 OpenAI chat 形状，但路由层已把 anthropic 请求
            # 转成 chat body 传进来——这里按 kimi/mimo 同款把 OpenAI 响应
            # 包一层转回 anthropic（流式 / 非流式都支持），Claude Code 等
            # /v1/messages 客户端即可直接使用。
            pass

        ep = discovery.discover()
        if ep is None:
            # App 可能刚被关掉：清掉残留端点，给客户端可操作提示
            self._last_ep = None
            state = discovery.describe_state()
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "message": "dumate 本地代理不可达：" + (state.get("hint") or "DuMate 未运行"),
                        "type": "upstream_unavailable",
                    }
                },
            )
        self._last_ep = ep

        stream = bool(body.get("stream", False))
        # 剥掉下划线开头字段（内部保留键），模型名透传（上游接受 glm-5 /
        # qwen3.5-35b-a3b / dm-auto-model/text.L0 等，见 _MODELS 注释）
        upstream_body = {k: v for k, v in body.items() if not k.startswith("_")}
        headers = {
            **ep.headers(),
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }

        client = await self._get_client()
        try:
            resp = await client.post(ep.chat_url(), json=upstream_body, headers=headers)
        except httpx.TimeoutException as exc:
            log.warning("dumate upstream timeout: %s", describe_exception(exc))
            raise HTTPException(status_code=504, detail={
                "error": {"message": "dumate upstream timeout", "type": "timeout"}
            }) from exc
        except httpx.HTTPError as exc:
            log.warning("dumate upstream error: %s", describe_exception(exc))
            raise HTTPException(status_code=502, detail={
                "error": {"message": "dumate upstream error", "type": "bad_gateway"}
            }) from exc

        if resp.status_code >= 400:
            if stream:
                # 先把错误体读完再关流：aclose() 会丢弃未读 body，之后拿不到上游
                # 真实错误（复用 zcode 同套防坑；dumate 上游是同一类 Go 网关）。
                await resp.aread()
            try:
                return _upstream_error_response(resp)
            finally:
                await resp.aclose()

        if stream:
            if protocol == "anthropic":
                return StreamingResponse(
                    _to_anthropic_stream(resp, str(body.get("model") or "")),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "close"},
                )
            return StreamingResponse(
                _pass_through_stream(resp),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "close"},
            )
        try:
            payload = resp.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail={
                "error": {"message": "dumate upstream returned non-JSON", "type": "bad_gateway"}
            }) from exc
        if protocol == "anthropic":
            from ..protocols.anthropic_adapter import chat_completion_to_anthropic_message

            payload = chat_completion_to_anthropic_message(payload, original)
        return JSONResponse(content=payload)

    def health(self) -> dict[str, Any]:
        state = discovery.describe_state()
        out: dict[str, Any] = {"id": self.id, "name": self.name, **state}
        if self._last_ep is not None and state.get("ready"):
            out["base_url"] = self._last_ep.base_url
        return out

    # ------------------------------------------------------------------
    # 额度：/ui 管理页消费（同步 httpx，调用方经 asyncio.to_thread 包装）
    # ------------------------------------------------------------------

    def quota(self) -> dict[str, Any] | None:
        """查 DuMate 积分余额（数字，照 antigravity 进度条语义）。

        桌面端「积分」面板同口径：``GET /api/dumate/points/quota_overview``
        （bceConsole cookie 通道，非本地代理；实测抓包 2026-10-05）。返回
        used/total/remaining（积分单位）+ percent（已用%）。未登录 / App 未装 /
        查询失败时退回本地代理的布尔 ``hasRemainingPoints`` 翻译成的百分制条目，
        至少别让面板空着；百分制 unit 标 ``"percent"``，数字条目标 ``"points"``。
        """
        from . import checkin as dumate_checkin

        # 优先：bceConsole 数字余额（quota_overview）
        q = dumate_checkin.fetch_quota_overview(timeout=10.0)
        if q is not None:
            remaining = q["remaining_points"]
            total = q["total_points"]
            used = q["used_points"]
            # 「订阅积分」= quota_overview 的 totalPoints（subscription 付费主池 +
            # incremental 赠送包的合计），留在明细首位当账号卡的总结行（用户
            # 2026-10-05：卡片上要有「剩 X / Y · 已用 Z%」总结，与 antigravity 账号卡
            # 同款；下面各池明细跟在后面，口径与总结行对得上）。
            items = [{
                "label": "订阅积分",
                "used": round(used, 2),
                "total": round(total, 2),
                "remaining": round(remaining, 2),
                "percent": round(used / total * 100, 2) if total > 0 else 0.0,
                "reset_ts": None,
                "expire_ts": None,
                "unit": "points",
            }]
            # 展开各池明细：subscription 付费主池（plan_pro 等，按 packageType 命名）
            # + incremental 赠送包（login_bonus 等，按 source 命名）。都有过期时间。
            for p in q.get("packages") or []:
                exp = p.get("expire_ts")
                ptype = p.get("package_type") or ""
                src = p.get("source") or "grant"
                # 付费主池标「订阅套餐（plan_pro）」，赠送包标「积分包（login_bonus）」
                label = (f"订阅套餐（{ptype}）" if ptype == "plan_pro"
                         else f"积分包（{src}）" if src == "login_bonus"
                         else f"积分池（{ptype or src}）")
                items.append({
                    "label": label,
                    "used": round(p["used_points"], 2),
                    "total": round(p["total_points"], 2),
                    "remaining": round(max(p["total_points"] - p["used_points"], 0.0), 2),
                    "percent": (round(p["used_points"] / p["total_points"] * 100, 2)
                                if p["total_points"] > 0 else 0.0),
                    "reset_ts": None,
                    "expire_ts": exp,
                    "unit": "points",
                })
            return {"items": items, "level": "百度搭子"}

        # 兜底：本地代理布尔态 → 百分制（保持原有「至少显示有/无」的行为）
        ep = discovery.discover()
        if ep is None:
            raise RuntimeError("dumate 本地代理不可达（DuMate 未运行）")
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(ep.points_url(), headers=ep.headers())
        except httpx.HTTPError as exc:
            raise RuntimeError(f"dumate 额度查询失败: {describe_exception(exc)}") from exc
        if resp.status_code != 200:
            raise RuntimeError(f"dumate 额度查询 HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError("dumate 额度返回非 JSON") from exc

        has = bool(data.get("hasRemainingPoints"))
        return {
            "items": [
                {
                    "label": "可用额度",
                    "used": 0 if has else 100,
                    "total": 100,
                    "remaining": 100 if has else 0,
                    "percent": 0.0 if has else 100.0,
                    # 布尔态额度：没有「周期性重置」概念，也不做到期告警
                    "reset_ts": None,
                    "expire_ts": None,
                    "unit": "percent",
                }
            ],
            "level": "百度搭子",
        }

    # ------------------------------------------------------------------
    # 签到（/ui 自动打卡循环消费；同步 httpx，调用方经 asyncio.to_thread 包装）
    # ------------------------------------------------------------------

    def checkin_status(self) -> dict[str, Any] | None:
        """今日签到状态。

        查询走 bceConsole cookie（``dumate/checkin.fetch_checkin_status``）——
        这条链路**依赖本机 DuMate 的登录态**（``~/Library/Application Support/
        qianfan-desktop-app`` 里的 ``.cookie-key`` + ``auth.json``）。所以查不到
        有两种截然不同的原因，必须分开说：

        - **本机未登录 / App 没装**：上一条签到是**另一台机器**打的，本机这份
          cookie 根本不存在。上游 ``hasIssued`` 是「这个登录态今天领没领」的
          口径，别的机器领过不影响本机 cookie——若这里直接回 ``None``，面板会
          把它渲染成「未签到」+ 可点的「立即打卡」，看起来像今天还没打
          （用户实测报障：另一台已自动签到，本机却显示未打卡）。
        - **网络 / 上游失败**：同理不能谎报「未签到」。

        两种情况都返回带 ``query_failed`` 的失败结构（与 codebuddy/trae 一致，
        见 ``benefits._is_failure``）：面板显示「状态未知」而不是「未签到」，
        ``benefits`` 也会用短 TTL 缓存、网络恢复后自愈。**不返回裸 ``None``**
        ——裸 ``None`` 在 ``snapshot`` 里等于「没有状态」，会静默退化成
        「未签到」的可点状态，正是这次报障的成因。
        """
        from . import checkin as dumate_checkin

        status = dumate_checkin.fetch_checkin_status()
        if isinstance(status, dict):
            return status
        # 区分「本机根本没有登录态」与「有登录态但查询失败」——前者给用户
        # 可操作提示（去装/登录另一台机？本机装 App），后者是临时网络问题。
        if dumate_checkin.resolve_bceconsole_auth() is None:
            msg = "本机无 DuMate 登录态（未安装/未登录），签到状态未知——若在别的机器打过卡，本机不会同步"
        else:
            msg = "DuMate 签到状态查询失败（网络或上游异常）"
        return {
            "checked_in": False,
            "claimable": False,
            "inactive": False,
            "query_failed": True,
            "message": msg,
        }

    def checkin_claim(self) -> dict[str, Any] | None:
        from . import checkin as dumate_checkin

        return dumate_checkin.claim_checkin()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client


async def _pass_through_stream(resp: httpx.Response) -> AsyncIterator[bytes]:
    """把上游 SSE/字节流原样泵给客户端；结束后确保连接释放。"""
    try:
        async for chunk in resp.aiter_bytes():
            if chunk:
                yield chunk
    finally:
        await resp.aclose()


async def _to_anthropic_stream(resp: httpx.Response, model: str) -> AsyncIterator[str]:
    """上游 OpenAI SSE → Anthropic 事件流（``/v1/messages`` 客户端要的形状）。

    DuMate 本地代理只回 OpenAI chat chunk；复用 ``anthropic_adapter.
    AnthropicStreamConverter``（kimi/mimo 同款），``reasoning_content`` →
    thinking 块、``tool_calls`` → tool_use 块。转换途中任何异常先收尾再抛
    （不兜的话客户端拿到「内容块悬空、没有 message_stop」的残流，更难排查）。
    """
    from ..protocols.anthropic_adapter import AnthropicStreamConverter

    converter = AnthropicStreamConverter(model)

    def _emit(event_name: str, payload: dict[str, Any]) -> str:
        return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _close_open() -> list[str]:
        """把已开出的内容块收尾（best-effort：收尾本身再炸也不能盖掉原错误）。"""
        try:
            return [_emit(n, p) for n, p in converter.close_open_blocks()]
        except Exception:  # noqa: BLE001
            return []

    def _abort(msg: str) -> list[str]:
        """收尾 + 补一个 error 事件，让客户端拿到结构完整的结束。"""
        out = _close_open()
        out.append(_emit("error", {
            "type": "error",
            "error": {"type": "api_error", "message": msg},
        }))
        return out

    try:
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                err = chunk["error"]
                msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
                for event in _abort(msg):
                    yield event
                return
            for name, payload in converter.feed_chunk(chunk):
                yield _emit(name, payload)
        for name, payload in converter.finish():
            yield _emit(name, payload)
    except Exception as exc:  # noqa: BLE001 — 上游畸形数据不该让客户端只收到半截流
        log.warning("dumate anthropic stream aborted: %s: %s", type(exc).__name__, exc)
        for event in _abort(f"{type(exc).__name__}: {exc}"):
            yield event
        return
    yield "data: [DONE]\n\n"


def _upstream_error_response(resp: httpx.Response) -> JSONResponse:
    """把上游错误转成客户端错误响应（透传状态码与错误体）。"""
    try:
        payload = resp.json()
    except Exception:
        payload = {"error": {"message": resp.text[:500], "type": "upstream_error"}}
    return JSONResponse(status_code=resp.status_code, content=payload)
