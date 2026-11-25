"""Trae Max 模式：目录解析、静态兜底、全局开关、请求注入。

上游 ``get_detail_param`` 目录（CN 与海外同构）每条模型带：

- ``display_config.max_mode``：bool，该模型是否支持 Max 模式；
- ``context_window_tokens.dev``：默认档输入窗口；
- ``context_window_tokens.max``：Max 档输入窗口（``max_mode=False`` 时通常缺失）。

请求级开关：转发时在请求体顶层加 ``is_max_mode: true``（参考社区实现
traework2api / TraeWorkAssistant 实测，2026-11-25 复核）即走 Max 档。

强耦合不变量（**窗口声明 与 is_max_mode 必须一致**）：

- 对外声明的 ``context_window`` 取自 context_guard，它按此窗口预检截断；
- 若声明 1M 但请求没开 ``is_max_mode``，守卫放行 1M 却打到上游 dev 墙
  （116k/200k）→ 上游 400 原样透传，比不声明更糟；
- 因此本模块一个决策点同时驱动两件事：``models()`` 声明哪个窗口、转发时
  注不注入 ``is_max_mode``。

决策：``supports_max_mode`` 的模型，**全局开关开启时默认走 Max**（声明 max
窗口、注入 ``is_max_mode``），关闭时走 dev（声明 dev 窗口、不注入）。不支持
的模型无论开关都不注入。全局开关可在管理页 / ``settings.json`` 关掉。

扣费说明：上游按实际输入 token 计费，不按窗口保底——开 Max 只抬天花板，不
会凭空多扣到 1M。客户端（如 DSH）若设了更紧的硬压缩上限（如 256k），实际
输入与扣费都被它锁死，开 Max 只是让这 256k 能完整通过 dev 墙。
"""

from __future__ import annotations

from typing import Any

#: settings.json 里的全局开关键；缺省 True（默认开 Max，见模块 docstring）。
SETTING_KEY = "trae_max_mode"

#: 该请求级字段名由上游契约决定（2026-11-25 复核社区实现），改了会让 Max 失效。
UPSTREAM_FIELD = "is_max_mode"

#: 静态兜底表（按区域）：模型对外 id（小写） -> (dev_window, max_window)。
#:
#: 只列 ``max_mode=true 且 max > dev`` 的模型——开 Max 才真正扩窗；
#: ``max == dev`` 的（如 gpt-5.2 / kimi-k2.5）开了只可能变贵不扩窗，一律不收录。
#: 目录刷新成功后 ``_discovered`` 覆盖这些值；这张表只为「还没刷新目录 / 离线
#: 启动」时让 /v1/models 有正确窗口 + 守卫能工作。
#:
#: 数据来源：2026-11-25 实测抓 CN/SG 网关 get_detail_param 全量 dump。
MAX_MODE_TABLES: dict[str, dict[str, tuple[int, int]]] = {
    "cn": {
        "deepseek-v4-pro-official": (116000, 1000000),
        "doubao-seed-2.1-pro": (256000, 1000000),
        "doubao-seed-evolving": (256000, 1000000),
        "deepseek-v4.1-flash": (116000, 1000000),
        "glm-5.2": (116000, 1000000),
        "glm-5.3": (116000, 1000000),
        "glm-5.3-flash": (116000, 1000000),
        "glm-5.3-flashx": (116000, 1000000),
        "glm-5.4": (116000, 1000000),
        "kimi-k3": (200000, 1000000),
        "kimi-k3.1-pro": (200000, 1000000),
        "kimi-k3.1-pro-highspeed": (200000, 1000000),
        "mimo-v2.6-flash": (200000, 981504),
        "mimo-v2.6-pro": (200000, 981504),
        "minimax-m3": (116000, 1000000),
        "minimax-m3.1-flash": (116000, 1000000),
        "qwen3.8-flash": (200000, 1000000),
        "qwen3.8-max": (200000, 1000000),
        "qwen4-max": (200000, 1000000),
        "step-5-preview": (116000, 1000000),
    },
    "global": {
        "deepseek-v4-flash-0731": (200000, 1000000),
        "deepseek-v4-pro-0813": (200000, 1000000),
        "deepseek-v4.1-flash": (200000, 1000000),
        "gemini-3-flash-premium": (200000, 1000000),
        "gemini-3.1-pro": (200000, 1000000),
        "gemini-4-neon": (968000, 1000000),
        "glm-5.2": (200000, 1000000),
        "gpt-5.4": (272000, 1000000),
        "gpt-5.5": (272000, 1000000),
        "gpt-5.6-luna": (272000, 1000000),
        "gpt-5.6-sol": (272000, 1000000),
        "gpt-5.6-terra": (272000, 1000000),
        "gpt-6-astra": (272000, 1000000),
        "gpt-6-luna": (272000, 1000000),
        "gpt-6-sol": (272000, 1000000),
        "gpt-6.1-sol": (272000, 1000000),
        "gpt-6.1-terra": (272000, 1000000),
        "gpt-6.2-astra": (272000, 1000000),
        "gpt-6.2-sol": (272000, 1000000),
        "kimi-k2.7-code": (200000, 1000000),
        "kimi-k3": (200000, 1000000),
        "minimax-m3": (200000, 1000000),
    },
}

def max_mode_enabled() -> bool:
    """全局开关是否开启（默认 True）。每次现读 settings.json，管理页热改即时生效。"""
    try:
        from buddy_proxy.core import settings as _settings

        return bool(_settings.load_settings().get(SETTING_KEY, True))
    except Exception:  # noqa: BLE001 - 读设置失败不阻断转发，按默认开处理
        return True


def static_windows(region_key: str, model: str) -> tuple[int, int] | None:
    """静态兜底窗口 (dev, max)；不支持或未收录返回 None。

    ``model`` 由调用方先按 MODEL_MAP 解析到内部名（小写）再传入——别名跟随
    内部名的 Max 能力，解析在 provider 层做，避免本模块镜像一份 MODEL_MAP
    （解耦，也防两张表漂移）。
    """
    key = (model or "").strip().lower()
    if not key:
        return None
    table = MAX_MODE_TABLES.get((region_key or "cn").strip().lower()) or {}
    pair = table.get(key)
    return pair if pair else None


def supports_max_mode(region_key: str, model: str) -> bool:
    """该模型是否支持 Max 模式（静态兜底口径；目录发现的运行时值走 discovered）。"""
    return static_windows(region_key, model) is not None


def effective_window(
    region_key: str,
    model: str,
    *,
    discovered: dict[str, Any] | None = None,
    enabled: bool | None = None,
) -> tuple[int, bool]:
    """决定本模型对外声明的上下文窗口 与 是否注入 is_max_mode。

    返回 ``(context_window, inject_max_mode)``：

    - 目录发现（``discovered``）优先：若带 ``max_mode`` 字段，按它裁决。
      ``max_mode=True`` 且有 ``max_window`` → 开关注入、声明 max；关或无 max
      → 声明 dev / 不注入。
    - 否则查静态兜底表。
    - 都不命中 → 返回 ``(None, False)``（不声明窗口、不注入；守卫跳过，让上游
      自己裁决，与改动前行为一致，不引入新故障）。

    ``enabled`` 允许调用方注入开关值（测试 / 避免重复读文件），缺省现读。
    """
    if enabled is None:
        enabled = max_mode_enabled()

    # 1. 目录发现值优先（运行时真值）
    info = discovered or {}
    disc_max_mode = info.get("max_mode")
    if disc_max_mode is not None:
        dev_w = info.get("context_window")
        max_w = info.get("max_window")
        if disc_max_mode and isinstance(max_w, int) and max_w > 0 and enabled:
            return (max_w, True)
        if isinstance(dev_w, int) and dev_w > 0:
            return (dev_w, False)
        return (None, False)

    # 2. 静态兜底
    static = static_windows(region_key, model)
    if static is not None:
        dev_w, max_w = static
        if enabled and isinstance(max_w, int) and max_w > dev_w:
            return (max_w, True)
        return (dev_w, False)

    return (None, False)
