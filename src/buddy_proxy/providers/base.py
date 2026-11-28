"""Provider 抽象层。

buddy-proxy 现在支持多个上游源（CodeBuddy、豆包等），每个源是一个
Provider 实现。Provider 负责两件事：

1. 认证（各自的登录态管理，互不干扰）
2. 转发 chat 请求到上游并解析响应

接口设计原则：
- ``forward`` 接收标准 OpenAI chat 请求体（已做 tool_choice 归一化等预处理），
  返回 ``StreamingResponse``（流式）或 ``JSONResponse``（非流式），与现有
  ``__main__.py`` 的 ``forward_chat`` 语义对齐。
- 每个 provider 自持 HTTP 客户端与认证状态，不共享。
- 模型列表由 provider 各自声明，``/v1/models`` 合并输出。
"""

from __future__ import annotations

import abc
from typing import Any, Sequence

from fastapi.responses import JSONResponse, StreamingResponse


class BaseProvider(abc.ABC):
    """上游模型源的统一抽象。"""

    #: provider 唯一标识（用于日志、路由、模型归属）。
    #:
    #: 命名约定：只允许小写字母、数字和连字符 ``-``（形如 ``doubao``、
    #: ``doubao-pro``、``codebuddy``）。**禁止使用斜杠 ``/``**——因为 provider
    #: id 会作为 ``owned_by`` 暴露在 ``/v1/models`` 中，且可能被上层配置
    #: （如 ethan-ai 的 ``provider:`` 字段）直接引用，斜杠会与「provider/模型」
    #: 的层级分隔符产生歧义。
    id: str = "base"
    #: 人类可读名称。
    name: str = "Base Provider"

    @abc.abstractmethod
    def models(self) -> Sequence[dict[str, Any]]:
        """返回本 provider 提供的模型列表（OpenAI /v1/models 格式元素）。"""

    @abc.abstractmethod
    def ensure_auth(self) -> None:
        """确保已认证；失败时抛 ``HTTPException``（401）。"""

    @abc.abstractmethod
    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        """转发 chat 请求到上游并返回响应。

        参数与 ``__main__.forward_chat`` 一致：
        - ``body``: 标准 OpenAI chat 请求体（已归一化、已设 stream）
        - ``protocol``: 客户端协议（"openai" / "responses" / "anthropic"），
          供 provider 决定是否做额外转换
        - ``original``: 原始请求体（协议转换前的，用于 responses/anthropic 还原）
        """

    def health(self) -> dict[str, Any]:
        """返回 provider 健康状态（合并进 /health）。"""
        return {"id": self.id, "name": self.name}

    def has_model_capacity(self, model: str) -> bool:
        """本地状态是否允许尝试此模型；账号冷却等由通道自行判断。"""
        return True

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        """客户端传来的 ``model`` 是否属于本通道（用于自动路由）。

        ``aliases=False`` 时只认 ``models()`` 里的 id 精确匹配（含剥掉
        ``provider/`` 前缀的裸名）；``aliases=True`` 时允许把**别名**也算进来。

        基类没有别名概念，两个取值等价。目录里存在别名的通道（如 Qoder 同时
        接受显示名 ``Qwen3.8-Flash`` 与内部 key ``qfmodel``）应覆写本方法，并
        在 ``aliases=False`` 时**只**做 id 匹配——路由侧会先用 ``False`` 跑一遍
        全部通道，都没命中才用 ``True`` 兜底。这样别名永远不会抢走别的通道
        按 id 精确匹配就能认领的模型。
        """
        want = (model or "").strip()
        if not want:
            return False
        prefix = f"{self.id}/"
        for m in self.models():
            mid = m.get("id")
            if not isinstance(mid, str):
                continue
            if mid == want:
                return True
            if mid.startswith(prefix) and mid[len(prefix):] == want:
                return True
        return False

    # ------------------------------------------------------------------
    # 可选能力：打卡 / 额度（/ui 管理页消费；不支持时返回 None）
    # 全部为同步方法（上游是 urllib/httpx 同步调用），调用方用
    # asyncio.to_thread 包装，避免阻塞事件循环。
    # ------------------------------------------------------------------

    def checkin_status(self) -> dict[str, Any] | None:
        """查询今日签到状态。返回 ``{"checked_in": bool, "claimable": bool,
        "inactive": bool, "streak_days": int, "message": str}``（claimable/
        inactive 缺省视为 True/False）；不支持签到时返回 None。

        可选再带两个字段说明「下次什么时候能再打」，管理页据此显示倒计时：

        - ``next_ts``：当前状态翻转时刻的 epoch 秒。已签到时是下一轮开始，
          未签到时正好也是本轮截止，所以一个字段够用。**它不只是给界面看的**：
          ``BenefitsManager`` 拿它当这份状态快照的到期时刻（见
          ``benefits._state_flipped``），到点即作废缓存重查，免得倒计时数到
          「即将刷新」之后那次轮询还命中翻篇前的旧状态。所以这个值要**如实**
          填——填一个已经过去的时刻会让缓存提前失效（超过一小时的过期值会被
          当作字段不可信而忽略，见 ``FLIP_GRACE_S``）。
        - ``next_ts_source``：``"upstream"``（上游给了时间窗）或
          ``"inferred"``（上游没有每日轮换字段、按实测的零点轮换推断）。
          界面用它决定要不要标注「推断」，别把猜出来的时刻说成上游契约。

        算不出下次时刻（活动已结束/档期已过）就不要给这两个字段，界面自然
        不显示——比显示一个已经过期的时刻诚实。辅助函数见
        :mod:`buddy_proxy.core.checkin`。"""
        return None

    def checkin_claim(self) -> dict[str, Any] | None:
        """领取今日签到积分。返回格式同 checkin_status；
        失败时抛异常（由调用方转成错误信息）；不支持时返回 None。"""
        return None

    def quota(self) -> dict[str, Any] | None:
        """查询套餐额度。返回 ``{"items": [{label, used, total, remaining,
        percent, reset_ts, expire_ts, unit}], "level": str|None}``
        （remaining/percent 无法计算时为 None）；不支持时返回 None。

        ``reset_ts`` 与 ``expire_ts`` 是**两个不同的东西**，别混：

        - ``reset_ts``：**周期性重置**时刻，到了这天额度回满、周期重来
          （ZCode 的 5 小时窗口、antigravity 的 weekly 池、MiMo 的周额）。
        - ``expire_ts``：**权益到期**时刻，到了这天这份额度就作废、不会回来
          （Qoder 的套餐/加油包、Trae 的权益包、MiMo 的套餐有效期）。

        早先两种语义都塞在 ``reset_ts`` 里，前端一律渲染成「… 重置」——
        Qoder 的「10-30 到期」在界面上就显示成「10-30 重置」，用户以为到期日
        没被记录。现在按字段分开渲染（「到期」/「重置」），到期告警横幅
        （见 :mod:`buddy_proxy.benefits` 的 ``_expiring``）只认 ``expire_ts``。
        拿不准是哪种就别填——填错会误报「快到期了」。

        ``unit``（可选）：``remaining`` 的量纲，到期告警据它决定要不要做
        额度量过滤（``"credit"`` 且剩余 ≤ ``EXPIRY_WARN_MIN_CREDITS`` 时不
        提醒，避免为一点零头刷屏；``"day"`` / ``"count"`` / ``"permille"``
        等只看天数）。不填（``None``）＝不做量过滤。

        ``sum_items``（可选，默认 ``False``）：置 ``True`` 表示 ``items``
        各项是**并存的份额**，管理页标题行应把它们相加作为账号总量。
        只在确实可加时置位——Trae 的权益包是总额度的明细（加了就重复计算），
        ZCode / MiMo 的各项量纲不同（5 小时窗口 vs 月窗口、百分比 vs 天数），
        相加无意义。默认为假，免得新通道被默默算错。

        它同时是**顺序契约**：未置位时管理页取 ``items[0]`` 当标题行，所以
        各通道要自己把最该先看到的那档放前面——ZCode 是窗口由小到大（5 小时
        在前），Trae 是总额度在前，CodeBuddy 是它自己算好的合计在前。"""
        return None
