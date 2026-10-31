"""MiMo 上游端点、模型表与请求头常量。

端点从 MiMo Desktop v26.922.220226 的 app.asar 提取（2026-09-22）：

- SSO/免 key 通道：``https://mimo-server-cn.xiaomimimo.com/api/route/chat/completions``
  （注意是 ``/api/route``，不是 ``/api``；asar 里 ``_p() = hN() + "/route"``）
- API key 通道：``https://api.xiaomimimo.com/v1/chat/completions`` 或
  Token Plan 区域端点 ``https://token-plan-{cn,ams,sgp}.xiaomimimo.com/v1``
- 小米账号 SSO：``https://account.xiaomi.com/pass/serviceLogin?sid=mimopc``

模型 id 统一小写，便于 ``forward_chat`` 按 ``provider.models()`` 裸名匹配
（与 zcode 同约定）。
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 上游端点
# ---------------------------------------------------------------------------

#: SSO 通道（走小米账号 cookie，免 API key）
SSO_API_BASE = "https://mimo-server-cn.xiaomimimo.com/api"
#: asar 里的 ``_p()``：chat 等路由挂在 ``/api/route`` 下
SSO_ROUTE_PREFIX = "/route"

#: API key 计费通道（OpenAI 兼容 ``/v1``）
BILLING_API_BASE = "https://api.xiaomimimo.com/v1"

#: Token Plan 订阅通道（按区域）
TOKEN_PLAN_BASES: dict[str, str] = {
    "cn": "https://token-plan-cn.xiaomimimo.com/v1",
    "ams": "https://token-plan-ams.xiaomimimo.com/v1",
    "sgp": "https://token-plan-sgp.xiaomimimo.com/v1",
}

#: 小米账号 serviceLogin（换取 ``mimopc`` serviceToken 的 Phase 1 入口）
SSO_SERVICE_LOGIN = "https://account.xiaomi.com/pass/serviceLogin"
#: MiMo 上游使用的 sid（从 serviceLogin 302 的 callback 参数实测）
SSO_SID = "mimopc"

# ---------------------------------------------------------------------------
# 请求头
# ---------------------------------------------------------------------------

#: SSO 通道的来源标识（``wrappedFetch`` 强制写入，上游可能据此分流/风控）
X_SOURCE_SSO = "mimocode-cli-free"
#: API key 通道的来源标识
X_SOURCE_KEY = "mimocode-cli"
#: SSO 换票请求的 UA（asar: ``User-Agent: MiClaw/1.0``）
SSO_UA = "MiClaw/1.0"
#: chat 请求的 UA
CHAT_UA = "mimocode/0.1.0"

# ---------------------------------------------------------------------------
# 模型表
# ---------------------------------------------------------------------------

#: 小写 id → 展示名。**以客户端「模型倍率」面板为准**（用户 2026-11-01 截图）。
#:
#: MiMo 早已不是「只有自家两个模型」：桌面端的模型选择器是一个**第三方聚合
#: 面板**（DeepSeek / GLM / Kimi / Qwen / 自家 MiMo），请求体里的 ``model``
#: 原样发给同一个 chat 端点即可。所以这里平铺全部文本模型，不再只列自家两条。
#:
#: 上游确实有目录接口 ``GET /api/model/list``（2026-11-01 实测 200），返回 14
#: 条，其中 4 条 ASR/TTS（mimo-v2.5-asr/tts/tts-voiceclone/tts-voicedesign）、
#: 1 条图片（Doubao-Seedream-5.0-pro）——都不是 chat completions 能用的，剔除
#: 后剩下面 9 条，与面板逐条吻合（面板只比目录少 Seedream 与 ASR/TTS）。
#: ``/api/route/models`` 是 404，别把目录接口和 chat 路由混在一起猜。
#:
#: 模型名**大小写敏感**，一律按目录给出的原样（``qwen3.8-flash`` 而不是
#: ``Qwen3.8-Flash``）：目录里叫 ``deepseek-v4.1-flash`` 带点、``glm-5.3``
#: 带连字符，照抄即可；改名会 30012/404。
#:
#: 会员门（30012「未开通会员」，2026-11-01 本机实测）：**9 个第三方模型全部**
#: 回 403 membership_required，自家 mimo-v2.6-* 也一样——门是账号级的
#: （``/api/user/xiaomi/subscription/self`` 显示只有一个 status=EXPIRED 的
#: MiMo 入门包，2026-10-22 到期），不是模型级的。目录照实发布，能不能真正
#: 调用由上游按账号诚实裁决，这里不按本机会员状态裁剪。
#:
#: ``mimo-auto``/``mimo-pro``/``mimo-flash`` 是桌面端 asar 里的别名（会被
#: 改写成正式名），仍保留在目录里以便老客户端点名，但不再是主力。
DEFAULT_MODELS: dict[str, str] = {
    "mimo-v2.6-pro": "MiMo V2.6 Pro",
    "mimo-v2.6-flash": "MiMo V2.6 Flash",
    "deepseek-v4-pro": "DeepSeek V4 Pro",
    "deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
    "glm-5.3": "GLM 5.3",
    "glm-5.3-flash": "GLM 5.3 Flash",
    "kimi-k3": "Kimi K3",
    "qwen3.8-flash": "Qwen 3.8 Flash",
    "qwen3.8-max": "Qwen 3.8 Max",
    "mimo-pro": "MiMo Pro (别名)",
    "mimo-flash": "MiMo Flash (别名)",
    "mimo-auto": "MiMo Auto (自动路由)",
}

#: 小写 id → 上游正式模型名。``mimo-auto`` 按 asar 的 ``pN``/``l2`` 改写成
#: ``mimo-pro``（未显式偏好 flash 时）。
MODEL_NAME_CANONICAL: dict[str, str] = {
    "mimo-auto": "mimo-pro",
    "mimo-pro": "mimo-pro",
    "mimo-flash": "mimo-flash",
    "mimo-v2.6-pro": "mimo-v2.6-pro",
    "mimo-v2.6-flash": "mimo-v2.6-flash",
}


def sso_chat_url(base: str | None = None) -> str:
    """SSO 通道的 chat completions 完整 URL。"""
    root = (base or SSO_API_BASE).rstrip("/")
    return f"{root}{SSO_ROUTE_PREFIX}/chat/completions"


def key_chat_url(base: str) -> str:
    """API key 通道的 chat completions 完整 URL（base 以 ``/v1`` 结尾）。"""
    return f"{base.rstrip('/')}/chat/completions"


# ---------------------------------------------------------------------------
# 本机路径
# ---------------------------------------------------------------------------


def _default_cookie_db() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Xiaomi MiMo"
        / "Partitions"
        / "xiaomi-account"
        / "Cookies"
    )


def cookie_db_path() -> Path | None:
    """MiMo 桌面账号分区 cookie 库路径；``MIMO_COOKIE_DB`` 可覆盖。"""
    env = os.environ.get("MIMO_COOKIE_DB", "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.exists() else None
    p = _default_cookie_db()
    return p if p.exists() else None


def mimocode_auth_path() -> Path:
    """MiMo 自己的 auth.json（API key 模式）；``MIMO_AUTH_JSON`` 可覆盖。"""
    env = os.environ.get("MIMO_AUTH_JSON", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".mimocode" / "auth.json"


# ---------------------------------------------------------------------------
# 账号侧只读接口（/ui 额度 Tab 用；asar: Mse 的 gC 调用表）
# ---------------------------------------------------------------------------


def usage_url(base: str | None = None) -> str:
    """今日用量（percent/resetAt）。"""
    return f"{(base or SSO_API_BASE).rstrip('/')}/user/usage"


def subscription_url(base: str | None = None) -> str:
    """当前订阅（current/subscriptions；空表 = 未开通会员）。"""
    return f"{(base or SSO_API_BASE).rstrip('/')}/user/xiaomi/subscription/self"


def profile_url(base: str | None = None) -> str:
    """账号资料（userId/nickname），SSO 换票成功后可用来验活。"""
    return f"{(base or SSO_API_BASE).rstrip('/')}/user/xiaomi/me"
