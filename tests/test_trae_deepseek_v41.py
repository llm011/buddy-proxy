"""trae 通道 deepseek-v4.1-flash 接入回归测试（离线，不访问上游）。

背景（2026-09-30 实测）：``deepseek-v4.1-flash`` 在 trae 通道可用（native
chat_v3 直通：聊天 / 读图 / 原生工具调用全通），但 ``MODEL_IDS`` 原先只收了
上一代 ``DeepSeek-V4-Flash`` / ``DeepSeek-V4-Pro``——与 glm-5.3-flashx 同一批
「上游已放行、目录漏收」的隐状态。

**本文件存在的核心价值是把「凭什么说它是 V4.1 而不是 V4-Flash」的判定方法
留下来。**名字被上游接受 ≠ 服务的真是这个模型（上游完全可能把名字别名到
别的权重），而 self-report 不可靠——实测对照组 deepseek-v4-flash 自称
"deepseek-chat"，V4.1-Flash 反而诚实说 "unknown"。所以只能靠**能力指纹**：

V4-Flash 无视觉（config.py 早有记载，本次对照实验复核），V4.1-Flash 有。
各打 5 张纯色 1x1 PNG 问颜色：

- ``deepseek-v4.1-flash``：**5/5 全对**（红/绿/蓝/黄/青），并能读图答题；
- ``deepseek-v4-flash``（对照）：**1/5**，唯一"对"的一次是把固定幻觉
  "蓝色"撞上了蓝图；无图时也答"蓝色"，直接问则承认「无图」。

若这个名字背后真是 V4-Flash 权重，视觉表现应与对照组一样烂——它没有。
这比 self-report 强得多，但仍属推断而非上游自证（没有任何端点回传真实
后端模型名）；上游哪天换权重，这里的断言不会报警，知悉此局限。

名字层面是**精确匹配**：``deepseek-v4.1`` / ``deepseek-v41-flash`` / 大写
``DeepSeek-V4.1-Flash`` / ``deepseek-v4.1-flashx`` / ``deepseek-v4.1-pro``
全部 4001——独立路由，非别名模糊匹配。故 MODEL_MAP 不得加映射（会把请求
静默换成别的模型）。
"""
from __future__ import annotations

from buddy_proxy.core.credit_estimate import estimate_credit
from buddy_proxy.trae.config import (
    MODEL_CREDITS,
    MODEL_IDS,
    MODEL_MAP,
    MODEL_SUPPORTS_IMAGES,
    _map_model,
    _WORK_FUNCTION_OVERRIDE,
)
from buddy_proxy.trae.provider import TraeProvider


def test_v41_flash_listed_by_trae_catalog():
    """目录里要有 v4.1-flash（/v1/models 与前端模型选择器都读它）。"""
    ids = [m["id"] for m in TraeProvider().models()]
    assert "deepseek-v4.1-flash" in ids
    # 保留的 V4-Pro 对外名也统一小写；用户决定移除旧 V4-Flash。
    assert "deepseek-v4-pro" in ids
    assert "deepseek-v4-flash" not in ids
    assert all(model_id == model_id.lower() for model_id in ids)


def test_work_catalog_ids_are_lowercase_and_map_upstream_names():
    """对外目录和 model_order 统一用小写，转发时还原大小写敏感的上游名。"""
    from buddy_proxy.trae.config import map_model_for

    ids = {m["id"] for m in TraeProvider().models()}
    assert all(model_id == model_id.lower() for model_id in ids)
    assert map_model_for("cn", "doubao-seed-evolving") == "Doubao-Seed-Evolving"
    assert map_model_for("cn", "doubao-seed-2.1-pro") == "Doubao-Seed-2.1-Pro"
    assert map_model_for("cn", "deepseek-v4-pro") == "DeepSeek-V4-Pro"
    # step-5-preview 上游只认全小写（2026-10-07 实测），原样透传不进映射表
    assert map_model_for("cn", "step-5-preview") == "step-5-preview"
    # qwen-3.7-plus 2026-10-12 实测收录（上游只认带连字符名）；无连字符的
    # qwen3.7-plus / qwen-3.8-max 仍是被拒变体
    # glm-5.2 2026-10-25 实测收录（目录可见 + 200 出流），从「旧模型」名单移除；
    # 上游 -Official 后缀的两条走 MODEL_MAP 还原大写，对外 id 保持小写
    assert not {"kimi-k2.7-code", "deepseek-v4-flash",
                "glm-5", "glm-5-turbo", "qwen3.7-plus", "qwen-3.8-max"} & ids
    assert {"qwen-3.7-plus", "glm-5.2",
            "deepseek-v4-flash-official", "deepseek-v4-pro-official"} <= ids
    assert map_model_for("cn", "deepseek-v4-pro-official") == "DeepSeek-V4-Pro-Official"


def test_v41_flash_precedes_legacy_v4_pro():
    """目录顺序沿用上游展示序：新一代 v4.1-flash 排在上一代 V4-Pro 之前。"""
    assert MODEL_IDS.index("deepseek-v4.1-flash") < MODEL_IDS.index("deepseek-v4-pro")


def test_v41_flash_is_passed_through_verbatim():
    """模型名原样透传，不做映射——上游精确匹配的是小写字面量。

    若加了 MODEL_MAP（哪怕映射到 DeepSeek-V4-Pro 这种「看似等价」的名字），
    请求会被静默换模型；且大写 ``DeepSeek-V4.1-Flash`` 实测 4001，映射过去
    反而把能用的名字改死。宁可红。
    """
    assert "deepseek-v4.1-flash" not in MODEL_MAP
    assert _map_model("deepseek-v4.1-flash") == "deepseek-v4.1-flash"


def test_v41_flash_declares_image_support():
    """必须声明读图能力（实测 5/5），且**只**给这一个 deepseek。

    双向都害人：漏报 → /v1/models 报纯文本，客户端（按名字猜能力的典型
    做法）误剥图片，「明明支持却表现为不支持」；多报 → 客户端盲发图片，
    上游 4001。上一代 V4-Flash / V4-Pro 确认读不了图，不得进表。
    """
    assert "deepseek-v4.1-flash" in MODEL_SUPPORTS_IMAGES
    assert not {"deepseek-v4-pro", "deepseek-v4-flash"} & MODEL_SUPPORTS_IMAGES
    entry = next(m for m in TraeProvider().models()
                 if m["id"] == "deepseek-v4.1-flash")
    assert entry["images"] is True
    # 旧 V4-Flash 已从可用目录移除，不再暴露为客户端模型。


def test_v41_flash_credit_multiplier():
    """目录展示倍率 x0.08，积分估算走实测计价（口径不同，同 flashx）。

    x0.08 来自 2026-09-30 官方倍率面板，带「会员5折」徽章（未折价 0.16——
    对账数据更接近按 0.16 计费，5 折疑似账单外补偿，未证实）。注意与本表
    ``DeepSeek-V4-Flash`` 的 x0.08 数值撞车但来源不同（旧值出自 2026-09-05
    知识库）。估算走 ``MEASURED_CREDIT_RATES``：(30000,100) ≈
    30000×3.41e-5 + 100×1.39e-4 ≈ 1.04。
    """
    assert MODEL_CREDITS["deepseek-v4.1-flash"] == "x0.08"
    entry = next(m for m in TraeProvider().models()
                 if m["id"] == "deepseek-v4.1-flash")
    assert entry["credits"] == "x0.08"
    assert estimate_credit("trae", "deepseek-v4.1-flash", 30000, 100) == 1.04


def test_v41_flash_needs_no_function_override():
    """不要凭猜测给 v4.1-flash 加 ``_WORK_FUNCTION_OVERRIDE``。

    实测全部走默认主路径 native chat_v3（聊天/读图/工具全通），Work 通道
    （solo_work_lite）从未触发、未实测。真在那里失败时应该让 4001 冒出来。
    """
    assert "deepseek-v4.1-flash" not in _WORK_FUNCTION_OVERRIDE


def test_v41_flash_auto_matches_trae_channel():
    """裸名要能被 trae 通道认领（forward_chat 自动路由；路由侧已剥前缀）。"""
    assert TraeProvider().accepts_model("deepseek-v4.1-flash")
