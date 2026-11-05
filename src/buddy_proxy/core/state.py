"""全局状态管理：ProxyState、FastAPI app 实例、以及跨模块共享的全局变量。

把 ``app`` / ``proxy_state`` 两个模块级全局统一放在这里，
其它模块（routes、codebuddy_provider、model_list、ui）通过
``from .state import ...`` 引用，避免拆分后出现循环导入。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException

from buddy_proxy.core.logging_setup import get_runtime_info
from buddy_proxy.providers.base import BaseProvider

# 本模块是 core 的汇聚点，**运行期不 import 通道包**：``codebuddy_provider`` 的包
# ``__init__`` 会 → ``observability`` → ``core.state``，反向依赖直接成环。此前这里用
# ``if TYPE_CHECKING:`` 挂着 ``CodeBuddyClient`` / ``BaseProvider`` 两个类型 import，
# 等于把「core 不依赖通道」的约定交给一个只影响类型检查的开关。现在：
#
# - ``BaseProvider`` 是真类型，可以运行期 import：它在 ``providers.base``，只依赖
#   标准库 + fastapi，且 ``providers/__init__.py`` 是空壳，不成环；
# - ``client`` 用 ``Any``：``codebuddy_provider.client`` 那条边无法绕开包初始化，
#   注解写清注释即可，不值得为它把循环导入请回来。
#
# 边界由 ``tests/test_module_boundaries.py`` 固化（core 模块级不得 import 通道包）。


_SENSITIVE_LOG_KEY_PARTS = (
    "body", "content", "message", "messages", "raw", "token", "bearer",
    "authorization", "api_key", "uid", "argument", "prompt", "response",
    "detail", "error",
)


def safe_log_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """返回可安全写日志的字段，敏感正文只保留长度和短哈希。

    这是 ``write_log`` 和 ``diagnostic`` 的最后一道防线。调用方仍应传递
    结构化摘要；该函数避免新调试代码意外把 body、凭证或上游错误正文落盘。
    """
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        key_lower = key.lower()
        # 已计算的长度/哈希是安全元数据；不要因名称中含 content/body
        # 而把它们再次哈希，避免损失有效可观测性。
        if key_lower.endswith((
            "_bytes", "_length", "_sha256", "_count", "_code", "_status",
            "_ms", "_tokens", "_detected",
        )):
            safe[key] = value
        elif any(part in key_lower for part in _SENSITIVE_LOG_KEY_PARTS):
            raw = str(value).encode("utf-8", errors="replace")
            safe[f"{key}_bytes"] = len(raw)
            safe[f"{key}_sha256"] = hashlib.sha256(raw).hexdigest()[:16]
        else:
            safe[key] = value
    return safe


class ProxyState:
    """管理 proxy 的全局状态：认证、日志、配置。"""

    def __init__(
        self,
        # CodeBuddyClient：见文件头「运行期不 import 通道包」的说明，这里用 Any。
        client: Any,
        mock_dir: Optional[Any],
        log_file: Optional[Any],
        enable_desensitize: bool = False,
        enable_optimize_context: bool = False,
        verbose_llm: bool = False,
        logger: Optional[logging.Logger] = None,
        json_logger: Optional[logging.Logger] = None,
        providers: Optional[dict[str, BaseProvider]] = None,
        default_provider: str = "codebuddy",
        default_model: Optional[str] = None,
        disabled_models: Optional[set[str]] = None,
        hidden_models: Optional[set[str]] = None,
        disabled_providers: Optional[set[str]] = None,
        model_schedules: Optional[dict[str, list]] = None,
        model_order: Optional[dict[str, list[str]]] = None,
        metrics: Optional[Any] = None,
        benefits: Optional[Any] = None,
        interactive_login: bool = True,
    ):
        self.client = client
        # 启动参数 --no-browser：为 False 时任何隐式补认证都不得拉起浏览器、
        # 也不得把登录 URL 打到 stdout。后台链路（如自动打卡轮询）会在启动
        # 后一秒内触发 ensure_auth，此前它走 ensure_authenticated() 默认值，
        # 无视了这个开关。只有显式 --login 才算「用户在等登录」。
        self.interactive_login = interactive_login
        # 多 provider 支持：除默认 CodeBuddy 外的其它上游源（按 provider.id 索引）
        self.providers: dict[str, BaseProvider] = providers or {}
        # 兜底通道：模型名未命中任何 provider 时转发到哪个通道
        # （"codebuddy" 走默认 CodeBuddy；或填已启用 provider 的 id，如 "trae"）
        self.default_provider = default_provider
        # 管理页设置的「默认启用模型」，形如 "zcode/glm-5.3" 或裸 "glm-5.3"。
        # 客户端请求未带 model 字段时用它补齐（settings.py 持久化，/ui 可改）
        self.default_model = default_model
        # 已停用的模型键集合，形如 "codebuddy/glm-4.7"（provider/model）。
        # 命中的 (provider, model) 组合在转发时直接失败（settings.py 持久化，/ui 可改）
        self.disabled_models: set[str] = set(disabled_models or ())
        # 从模型目录隐藏的模型键集合，形如 "qoder/qwen3.8-max"。隐藏只影响
        # /v1/models、管理页与选择器曝光，直接点名仍可调用；与 disabled_models
        # 的「调用直接失败」语义严格分开。settings.py 持久化，/ui 可改。
        self.hidden_models: set[str] = set(hidden_models or ())
        # 整通道停用的 provider id 集合（/ui 模型页 provider 标题栏的开关，用户
        # 2026-10-06 需求）。命中通道：转发 403、/v1/models 组置灰、额度页与告警
        # 不再展示、顺序页对应模型置灰。settings.py 持久化，/ui 可改。
        self.disabled_providers: set[str] = set(disabled_providers or ())
        # 限时可用模型：键 "provider/model" → 允许时间窗列表 [["HH:MM","HH:MM"], ...]。
        # 命中的模型仅在窗口内放行、窗口外 403（与 disabled_models 并存，disabled
        # 优先级更高）。settings.py 持久化，/ui 可改。空 dict = 所有模型不受时段限制。
        self.model_schedules: dict[str, list] = dict(model_schedules or {})
        # 每模型的候选上游顺序：键为 settings.model_key 口径的 "provider/model"，值为
        # 有序的 "provider/model" 目标列表。命中时按序尝试、**仅当未向客户端提交任何
        # 字节就失败**才换下一个（见 codebuddy_provider/forward.py 的重试规则）；失败
        # 的目标由 core/cooldown.py 打短期标记，后续请求自动跳过。空 dict = 不启用
        # 排序，路由行为与历史完全一致。settings.py 持久化，/ui 可改。
        self.model_order: dict[str, list[str]] = dict(model_order or {})
        # 请求指标收集器（metrics.MetricsCollector，供 /ui 图表聚合）
        self.metrics = metrics
        # 打卡/额度管理器（benefits.BenefitsManager，供 /ui 打卡日历与额度展示）
        self.benefits = benefits
        self.mock_dir = mock_dir
        self.log_file = log_file
        self.enable_desensitize = enable_desensitize
        self.enable_optimize_context = enable_optimize_context
        self.verbose_llm = verbose_llm
        self.logger = logger
        self.json_logger = json_logger
        self.runtime_info = get_runtime_info()
        self.started_at = time.time()

    def ensure_auth(self) -> None:
        """确保已认证；认证失败（token 过期/网络错误/登录未完成）返回结构化 401 而非 500。"""
        if self.mock_dir is not None:
            return
        try:
            self.client.ensure_authenticated(open_browser=self.interactive_login)
        except HTTPException:
            raise  # 已是结构化异常，原样透传（如 503 proxy not initialized）
        except Exception as exc:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": "认证失败：token 无效或已过期，请重新登录（--login）",
                        "type": "authentication_error",
                        "details": str(exc)[:200],
                    }
                },
            )

    def write_log(self, event: str, **kwargs) -> None:
        if self.json_logger is None:
            return
        try:
            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "event": event,
                **self.runtime_info,
                **safe_log_fields(kwargs),
            }
            self.json_logger.info(json.dumps(record, ensure_ascii=False))
        except Exception:
            pass

    def write_body_log(self, event: str, body: bytes, **kwargs) -> None:
        """记录 body 的不可逆摘要，绝不将原文写入日志。

        方法名为兼容旧调用保留；日志里只留长度和短哈希，供同一次故障
        的关联排查使用。请求、响应、token、UID 和工具参数原文均不得落盘。
        """
        if self.json_logger is None:
            return
        try:
            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "event": event,
                **self.runtime_info,
                "body_bytes": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest()[:16],
                **safe_log_fields(kwargs),
            }
            self.json_logger.info(json.dumps(record, ensure_ascii=False))
        except Exception:
            pass


# ============================================================================
# FastAPI 应用（模块级单例，路由模块通过 from .state import app 引用）
# ============================================================================

app = FastAPI(title="CodeBuddy Proxy (FastAPI)", version="2.0")

# 全局状态（在 main() 中初始化）
proxy_state: Optional[ProxyState] = None


def _get_state_or_none() -> Optional[ProxyState]:
    """get_state 的容错版本：未初始化时返回 None（用于日志场景）。"""
    return proxy_state


def get_state() -> ProxyState:
    if proxy_state is None:
        raise HTTPException(
            status_code=503,
            detail={"error": {"message": "proxy not initialized", "type": "internal_error"}},
        )
    return proxy_state


def diagnostic(event: str, **kwargs) -> None:
    """输出安全诊断日志，避免调试调用意外泄露原文。"""
    state = get_state()
    if state.logger:
        state.logger.info(f"{event}: {json.dumps(safe_log_fields(kwargs), ensure_ascii=False)}")


# 安全词检测统一关键词（中英混合）
_SAFETY_KEYWORDS = ("sensitive", "cannot respond", "敏感内容", "无法响应", "unable to")


def is_policy_blocked(text: str) -> bool:
    """检测文本是否包含安全策略拦截标记"""
    return any(marker in text.lower() for marker in _SAFETY_KEYWORDS)


def text_summary(value: str) -> dict[str, Any]:
    return {
        "content_length": len(value),
        "content_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
        "safety_message_detected": is_policy_blocked(value),
    }
