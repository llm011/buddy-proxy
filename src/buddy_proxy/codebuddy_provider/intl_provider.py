"""CodeBuddy 海外版独立 provider（``codebuddyintl``）——与 CN 通道分开路由/计额。

CodeBuddy 桌面端本就有 intl/CN 两份产品配置（product-ide.json vs
product-ide-cn.json），唯一差异是上游 host：海外版
``https://www.codebuddy.ai``，CN ``https://copilot.tencent.com``。2026-10
实测两边 ``/v2/plugin`` 协议逐字节同构（auth/state、token 轮询、chat 网关
同一套），海外账号只是**换域名登录/转发**，凭据形状完全一致——因此 CN 与
海外共用同一个账号 store（index.json + per-account cred 文件），账号落
``region`` 标（cn/global）区分归属。

照 ``traeintl`` / ``qoderintl`` 的先例：同上游家族的另一个变体用独立
provider id 的子类，客户端以 ``codebuddyintl/<模型>`` 前缀显式路由，
``/ui`` 管理页 CN 与海外各一张额度卡。``forward()``/``checkin``/``quota``
全部继承父类，父类按 ``self._region`` 只取本区账号。

模型目录来自海外客户端在线「模型倍率」面板（2026-10-10 截图）并逐项用真实
海外账号调用确认；同系列只保留用户指定的新一代模型。只发布带
``codebuddyintl/`` 前缀的模型，不参与裸名自动匹配，也绝不混进 CN 的
CodeBuddy 静态表。
"""

from __future__ import annotations

from typing import Any, Sequence

from fastapi import HTTPException

from .provider import CodeBuddyProvider


# 海外客户端在线倍率面板的 2026-10-10 快照；内部 id 均经 www.codebuddy.ai
# 真实调用验证。旧安装包 product-ide.json 的 Claude 3.7/4.0、GPT-5、Gemini 2.5
# 已全数返回 11102，不能作为目录兜底。同系列旧代按用户要求不展示：Hy3、GPT
# 5.5/5.4/5.3-Codex、GLM-5.2、Kimi-K2.6。``credits`` 保持项目既有格式，
# 尤其 x0.00 是有效免费倍率，不能用真值判断吞掉。
_INTL_MODELS: tuple[dict[str, Any], ...] = (
    {"id": "hy4", "name": "Hy4", "vendor": "tencent",
     "credits": "x0.00 credits", "tool_call": True, "images": True},
    {"id": "gpt-5.6-sol", "name": "GPT-5.6-Sol", "vendor": "openai",
     "credits": "x3.47 credits", "tool_call": True, "images": True},
    {"id": "gpt-5.6-terra", "name": "GPT-5.6-Terra", "vendor": "openai",
     "credits": "x1.39 credits", "tool_call": True, "images": True},
    {"id": "gpt-5.6-luna", "name": "GPT-5.6-Luna", "vendor": "openai",
     "credits": "x0.14 credits", "tool_call": True, "images": True},
    {"id": "gemini-3.5-flash", "name": "Gemini-3.5-Flash", "vendor": "google",
     "credits": "x0.99 credits", "tool_call": True, "images": True},
    {"id": "glm-5.3", "name": "GLM-5.3", "vendor": "zhipu",
     "credits": "x0.79 credits", "tool_call": True, "images": True},
    {"id": "kimi-k3", "name": "Kimi-K3", "vendor": "moonshot",
     "credits": "x1.62 credits", "tool_call": True, "images": True},
)


def intl_enabled() -> bool:
    """有没有海外版账号（``__main__`` 据此决定注册 ``codebuddyintl``）。

    条件注册的理由与 ``traeintl``/``qoderintl`` 同款：没登过海外账号时不挂
    通道，``codebuddyintl/`` 前缀走「通道未启用」的明确报错。只看索引不看
    冷却——全冷却时通道仍该注册，让转发侧 429 快速失败，而不是误报成「没开」。
    """
    try:
        from .credentials import list_accounts

        return any(a.region == "global" for a in list_accounts())
    except Exception:  # noqa: BLE001 - 索引读不动就当没登过
        return False


class CodeBuddyIntlProvider(CodeBuddyProvider):
    id = "codebuddyintl"
    name = "CodeBuddy 海外版"

    # 额度/签到分组标签：``CodeBuddy 海外版 #N · ``——与 CN 卡的
    # ``CodeBuddy #N`` 区分开，前端按前缀各渲染各的卡。
    _quota_tag = "CodeBuddy 海外版"

    def __init__(self) -> None:
        super().__init__(region="global")

    def models(self) -> Sequence[dict[str, Any]]:
        """海外版聊天模型（独立前缀，避免被默认 CodeBuddy 裸名认领）。"""
        return [
            {
                **model,
                "id": f"{self.id}/{model['id']}",
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "description": model["name"],
            }
            for model in _INTL_MODELS
        ]

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        """只认完整 ``codebuddyintl/<模型>``，裸名继续交给默认 CN 通道。

        基类会把目录 id 的 provider 前缀剥掉后匹配；这里若不覆写，发布海外目录
        便会让 ``hy4`` 这类与 CN 同名的请求静默改道到海外账号。
        """
        del aliases
        want = (model or "").strip()
        if not want.startswith(f"{self.id}/"):
            return False
        return any(model_info.get("id") == want for model_info in self.models())

    def ensure_auth(self) -> None:
        """启动校验：必须有海外版账号。

        父类走 state.ensure_auth（只看「有没有账号」）——只有 CN 账号时也会
        放行，海外通道拿 CN 账号打 www.codebuddy.ai 必 401，故自己查。
        口径与 ``intl_enabled()`` 对齐：只看索引不看冷却——账号全冷却时通道
        照样注册，让转发侧 429 快速失败，而不是误报「没登过海外账号」。
        """
        from .credentials import list_accounts

        if not any(a.region == "global" for a in list_accounts()):
            raise HTTPException(
                status_code=401,
                detail=("codebuddyintl 无海外版账号：请先 "
                        "`buddy login codebuddy --region global`"))
