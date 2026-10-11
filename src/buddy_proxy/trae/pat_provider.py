"""PAT 独立 provider：服务账号的扩展模型目录（与个人 trae 通道分开展示/计额）。

复用 TraeProvider 的 forward 机制（SSE 解析、流式转换、native 拦截路由），
仅替换身份与目录：
- ``id = "traepat"``：客户端以 ``traepat/<模型>`` 指定，与个人 ``trae/`` 区分；
- ``models()`` 只含 PAT 扩展模型（未配置 TRAE_PAT_BEARER 时空列表）；
- ``quota()`` 只含 PAT 周包/日包，不再混入个人额度；
- 打卡/积分/用量记录均为个人账号专属功能，这里一律不支持。
"""

from __future__ import annotations

from typing import Any, Sequence

from fastapi import HTTPException

from ..core.metrics import ACCOUNT_META
from .pat import ensure_pat_config, pat_enabled, pat_model_names, fetch_pat_ent_usage
from .provider import TraeProvider


class TraePatProvider(TraeProvider):
    id = "traepat"
    name = "Trae PAT (服务账号直连)"
    # 打卡/积分是个人账号专属，服务账号不支持（避免出现在 /ui 打卡列表里）
    supports_checkin = False
    # PAT 子类标记：provider.forward 的多账号 failover 只服务个人 work 通道。
    # TraePatProvider 覆写了发送路径且有自己的账号体系，必须单账号直通——
    # 否则 forward 循环会拿 work 账号列表去跑 PAT 请求（串号 + 错误账号标记）。
    _pat_variant = True

    def models(self) -> Sequence[dict[str, Any]]:
        if not pat_enabled():
            return []
        from .pat import pat_model_meta
        meta = pat_model_meta()
        out: list[dict[str, Any]] = []
        for m in pat_model_names():
            info = meta.get(m, {})
            out.append({
                "id": m,
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "tier": "PAT",
                "name": info.get("name") or m,
                "credits": info.get("credits"),
                "max_input": info.get("max_input"),
                "reasoning": bool(info.get("reasoning")),
                # 图片能力来自 models_config.json 的 images 字段（与 CodeBuddy
                # 通道同口径）。未声明即按不支持处理，避免 /v1/models 漏报
                # input_modalities 导致客户端误剥图片。
                "images": bool(info.get("images")),
                "description": info.get("name") or "Trae PAT 扩展模型",
            })
        return out

    # 显式置 None：模型页的「重新拉取」按钮按 callable 判定（models_api.refresh），
    # 继承的 TraeProvider.refresh_models 对 PAT 是错的——它按 self._region（cn）
    # 取 work 模型表、拿 work 凭证逐模型发真实请求，再用探测结果过滤 PAT 的
    # 静态列表（models_config），模型对不上还白付积分。PAT 目录是静态配置，
    # 没有「可拉取」语义。
    refresh_models = None  # type: ignore[assignment]

    def ensure_auth(self) -> None:
        # 启动/健康检查只做本地严格配置校验；token 交换延迟到真实请求，避免阻塞。
        ensure_pat_config()
        # 顺带拉起后台凭证自愈循环（幂等）：离线期间缺失/临期的 token 在网络
        # 恢复后自动补齐，不靠第一个撞上的请求去踩 502。
        from .pat import start_token_keeper
        start_token_keeper()

    def _send_native_request(self, native_msgs, model, stream, tools):
        from .pat import send_pat_native
        # 请求级账号 holder：调用方线程带请求上下文副本（读线程经 copy_context、
        # _collect 经 asyncio.to_thread），未设置（直连调用/测试）时为 None
        return send_pat_native(native_msgs, model, stream, tools,
                               meta=ACCOUNT_META.get())

    def _keeps_native_error(self, model: str) -> bool:
        return True

    def _stream_native_events(self, native_msgs, model, tools, stop):
        from .pat import stream_pat_native
        return stream_pat_native(native_msgs, model, tools, stop=stop,
                                 meta=ACCOUNT_META.get())

    def _uses_native_mode(self) -> bool:
        # PAT 只有独立原生传输路径；不受个人通道开关影响，杜绝凭证穿透。
        return True

    async def forward(self, body, protocol, original=None):
        # 防穿透守卫：非 PAT 目录模型绝不经由本 provider 转发——否则会穿透到
        # 继承的个人通道逻辑，拿用户个人凭证调用、消耗个人额度（已发生过的 bug）
        model = str(body.get("model", "") or "")
        from .pat import is_pat_model
        if model and not is_pat_model(model):
            raise HTTPException(
                status_code=400,
                detail=f"模型 {model} 不在 traepat 目录（PAT 通道仅含扩展模型与"
                       f"注册过的个人目录模型）；如需个人账号额度请改用 trae/{model}")
        return await super().forward(body, protocol, original)

    def quota(self) -> dict[str, Any] | None:
        try:
            return {"items": fetch_pat_ent_usage()}
        except Exception as e:
            # 额度查询失败不抛——UI 展示为查询错误，不影响其它 provider
            return {"items": [{"label": "PAT 额度查询失败", "used": None, "total": None,
                               "remaining": str(e)[:120], "percent": None, "reset_ts": None}]}

    # —— 打卡/积分/用量记录均为个人账号专属：显式覆盖为 None，防止继承的
    # TraeProvider 实现拿个人凭证调用（supports_checkin=False 已挡住 UI 调度）——

    def checkin_status(self) -> dict[str, Any] | None:
        return None

    def checkin_claim(self) -> dict[str, Any] | None:
        return None

    def usage_records(self, start: str, end: str, page_num: int = 1,
                      page_size: int = 20) -> dict[str, Any] | None:
        return None
