"""TRAE PAT 多账号凭证、缓存、配额隔离与稳定主备 failover。

``TRAE_PAT_BEARER_PROFILES`` 是严格 JSON 数组，每项必须包含 ``bearer``，
可选 ``id``、``priority``，不接受其他字段。配置存在时不会回退旧的
``TRAE_PAT_BEARER``；旧变量只在 profiles 完全未设置时兼容。

子模块（依赖方向自上而下，禁止反向）：

- ``config``       profiles 严格校验、通道开关、常量与状态文件路径
- ``models``       模型目录热加载（plus/public 分池 + function 覆盖）
- ``store``        账号状态 v2 原子 JSON 与每账号刷新锁
- ``credentials``  bearer 两步交换、账号级缓存刷新、请求头
- ``cooldown``     撞码分级冷却、通道级 4031 快速失败、可用账号序
- ``status``       模型负载查询（plus 网关，短 TTL 缓存）
- ``quota``        entitlement 额度查询与归一
- ``chat``         响应识别（错误码/假成功）与稳定主备流式/非流式转发
- ``keeper``       凭证自愈后台循环与账号状态面板

兼容约定：历史 ``buddy_proxy.trae.pat.<名字>`` 导入路径全部保持有效
（显式再导出 + ``__getattr__`` 兜底）。测试打在本包命名空间上的
monkeypatch 接缝（``_post_chat`` / ``_stream_profile_events`` /
``_get_profile_credentials`` / ``_exchange`` / ``_exchange_env_ready`` /
``_gateway_reachable`` / ``_reload_pat_models`` / ``_refresh_locks`` /
``_quota_code_hits`` / ``_channel_exhausted`` / ``PAT_MODELS`` /
``PAT_PLUS_MODELS`` / ``PAT_PUBLIC_MODELS`` / ``PAT_MODEL_FUNCTIONS``，
见 test_trae_pat_failover）
依然生效：各子模块函数体内对这些名字经 ``_ns``（本包命名空间）调用期解析。
"""

from __future__ import annotations

from .config import (  # noqa: F401
    PatCredentials,
    PatProfile,
    ensure_pat_config,
    pat_enabled,
)
from .models import (  # noqa: F401
    PAT_MODELS,
    PAT_MODEL_FUNCTIONS,
    PAT_MODEL_RATE_LIMITS,
    PAT_PLUS_MODELS,
    PAT_PUBLIC_MODELS,
    _reload_pat_models,
    is_pat_model,
    pat_gateway_is_plus,
    pat_model_meta,
    pat_model_names,
)
from .store import (  # noqa: F401
    _refresh_locks,
)
from .credentials import (  # noqa: F401
    _exchange,
    _get_profile_credentials,
    get_pat_credentials,
)
from .cooldown import (  # noqa: F401
    _channel_exhausted,
    _clear_standard_cooldowns,
    _record_standard_pool_4031,
    _standard_pool_items,
)
from .status import fetch_pat_model_status  # noqa: F401
from .quota import (  # noqa: F401
    _gateway_reachable,
    fetch_pat_ent_usage,
)
from .chat import (  # noqa: F401
    _post_chat,
    _stream_profile_events,
    send_pat_chat,
    send_pat_native,
    stream_pat_native,
)
from .keeper import (  # noqa: F401
    _exchange_env_ready,
    accounts_status,
    refresh_missing_tokens,
    start_token_keeper,
)

_SUBMODULES = ("config", "models", "store", "credentials", "cooldown", "status",
               "quota", "chat", "keeper")


def __getattr__(name: str):
    """兜底再导出：显式清单之外的历史名字（私有辅助/常量/第三方导入）
    从子模块解析。与 ``buddy_proxy.trae_provider`` / ``trae.text_toolcall``
    的兼容 shim 同一套约定：旧代码对本包任意历史顶层名字的读取不断链。
    """
    import importlib

    for sub in _SUBMODULES:
        mod = importlib.import_module(f"{__package__}.{sub}")
        if hasattr(mod, name):
            return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
