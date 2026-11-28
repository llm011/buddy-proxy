"""Loomy 登录态：从桌面端 ``auth-session.json`` 读讯飞账号 session。

## 凭据从哪来（实测 2026-11-29，Loomy 0.9.39）

Loomy 桌面端把登录结果落盘在::

    ~/Library/Application Support/loomy/auth-session.json   (macOS)
    %APPDATA%/loomy/auth-session.json                       (Windows)

形状::

    {"session": "<32 hex>", "userid": "261129013242680405",
     "phone": "185****", "updatedAt": 1795887162698}

``session`` 就是讯飞账号的会话令牌，**有效期 14 天**
（登录接口 ``expire = 14*24*3600``）。过期后上游回 ``{"code":"100002",
"desc":"登录已失效，请重新登录"}``——注意是 **HTTP 200 + 业务码**，只看状态码
会把它当成成功。

## 为什么不做自动登录

自动登录要复刻讯飞账号的短信/微信/密码登录（``electron/xfyun/account-service.js``
里的 ``generateRequestHeaders`` 签名 + RSA 公钥加密密码），而 session 只要
14 天续一次。用户重登 Loomy 桌面端一次就够，成本远低于实现并维护一套签名
登录链路——**而且登录态本就该由用户在官方客户端里授权**。

## 一个真实踩坑：两套 baseURL

Loomy 安装包里 ``.env.prod`` 写的是 **测试环境**::

    LOOMY_POINTS_BASE_URL=https://ossptest.voicecloud.cn/loomy/integration
    -> baseURL = .../loomy/integration/api/v1

但正式版运行期用的是 **生产环境**（``~/.config/loomy-opencode/opencode.json``
里 ``provider.imodel.options.baseURL`` 才是在跑的那个）::

    https://loomyad.xunfei.cn/api/v1

拿测试地址去验 session，生产签发的 session 一律回 ``100002 登录已失效``——
会被误判成「登录过期」，实际是**地址错了**。所以本模块的 baseURL 解析顺序是：

1. 环境变量 ``LOOMY_BASE_URL``（显式覆盖，排查用）
2. 桌面端实际运行配置 ``~/.config/loomy-opencode/opencode.json`` 的
   ``provider.imodel.options.baseURL``（**最可信**：这就是客户端自己在用的）
3. 兜底常量 :data:`DEFAULT_BASE_URL`（生产地址）
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 生产网关（``loomyad.xunfei.cn``）。**不要**用安装包 ``.env.prod`` 里那个
#: ``ossptest.voicecloud.cn``——那是测试环境，见模块说明。
DEFAULT_BASE_URL = "https://loomyad.xunfei.cn/api/v1"

#: 积分/账单接口挂在根域名下（不带 ``/api/v1`` 前缀的那层由调用方拼）。
DEFAULT_ORIGIN = "https://loomyad.xunfei.cn"

#: Loomy 客户端版本，随请求头发 ``loomy-version``。
#:
#: 实测缺这个头不影响 chat 往返，但客户端自己每次都带；保留它能让上游的
#: 版本灰度/风控逻辑把我们当正常客户端，少一类「莫名其妙被降级」的麻烦。
DEFAULT_CLIENT_VERSION = "0.9.39"

#: 上游业务码：登录态失效。**HTTP 状态码是 200**，必须看 body。
AUTH_ERROR_CODE = "100002"

#: 上游业务码：成功。
OK_CODE = "000000"


class AuthError(Exception):
    """登录态缺失 / 失效。消息直接给用户看，要可操作。"""


@dataclass(frozen=True)
class LoomySession:
    """一份解析好的登录态。"""

    token: str
    user_id: str = ""
    phone: str = ""
    updated_at: int = 0

    @property
    def display(self) -> str:
        """日志里用的脱敏标识（**绝不打印 token 原文**）。"""
        if self.user_id:
            return f"user {self.user_id}"
        if self.phone:
            return f"phone {self.phone[:3]}****{self.phone[-2:]}"
        return "unknown"


def _app_support_dir() -> Path:
    """Loomy 的用户数据目录（Electron ``app.getPath('userData')``）。"""
    override = os.getenv("LOOMY_USER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "loomy"
    if sys.platform.startswith("win"):
        appdata = os.getenv("APPDATA", "").strip()
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "loomy"
    return Path.home() / ".config" / "loomy"


def session_file() -> Path:
    """``auth-session.json`` 的路径。"""
    return _app_support_dir() / "auth-session.json"


def opencode_config_file() -> Path:
    """Loomy 内嵌 OpenCode 的运行期配置（含真实 baseURL）。"""
    override = os.getenv("LOOMY_OPENCODE_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "loomy-opencode" / "opencode.json"


def load_session() -> LoomySession:
    """读登录态；缺失或字段不全时抛 :class:`AuthError`（带可操作提示）。"""
    path = session_file()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AuthError(
            f"未找到 Loomy 登录态（{path}）。"
            "请先启动 Loomy 桌面端并完成登录，再重启 buddy。"
        ) from None
    except OSError as exc:
        raise AuthError(f"读取 Loomy 登录态失败（{path}）：{exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuthError(f"Loomy 登录态不是合法 JSON（{path}）：{exc}") from exc
    if not isinstance(data, dict):
        raise AuthError(f"Loomy 登录态格式异常（{path}）：期望对象")

    token = str(data.get("session") or "").strip()
    if not token:
        raise AuthError(
            f"Loomy 登录态里没有 session（{path}）。"
            "请在 Loomy 桌面端重新登录后再试。"
        )
    return LoomySession(
        token=token,
        user_id=str(data.get("userid") or "").strip(),
        phone=str(data.get("phone") or "").strip(),
        updated_at=int(data.get("updatedAt") or 0),
    )


def _base_url_from_opencode_config() -> str:
    """从 Loomy 运行期配置里抠 ``provider.imodel.options.baseURL``。

    只认 ``useSessionAuth === true`` 的那个 provider——用户可能同时配了第三方
    （智谱/DeepSeek）provider，那些的 baseURL 指向别家，拿来当 Loomy 网关会
    404 或 401，极难排查。
    """
    path = opencode_config_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    providers = data.get("provider")
    if not isinstance(providers, dict):
        return ""

    # 优先 imodel 本体；再退回任意 useSessionAuth 的 provider
    candidates: list[dict[str, Any]] = []
    imodel = providers.get("imodel")
    if isinstance(imodel, dict):
        candidates.append(imodel)
    for pid, cfg in providers.items():
        if pid == "imodel" or not isinstance(cfg, dict):
            continue
        options = cfg.get("options")
        if isinstance(options, dict) and options.get("useSessionAuth") is True:
            candidates.append(cfg)

    for cfg in candidates:
        options = cfg.get("options")
        if not isinstance(options, dict):
            continue
        url = str(options.get("baseURL") or "").strip()
        if url:
            return url.rstrip("/")
    return ""


def resolve_base_url() -> str:
    """网关 baseURL，按模块说明的三级优先级解析。"""
    explicit = os.getenv("LOOMY_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    from_config = _base_url_from_opencode_config()
    if from_config:
        return from_config
    return DEFAULT_BASE_URL


def resolve_origin(base_url: str = "") -> str:
    """从 baseURL 推出根域名（积分接口挂在根下，不带 ``/api/v1``）。"""
    base = (base_url or resolve_base_url()).rstrip("/")
    # ``https://host/api/v1`` -> ``https://host``；保留非默认端口
    marker = "/api/"
    idx = base.find(marker)
    if idx > 0:
        return base[:idx]
    if base.endswith("/api"):
        return base[: -len("/api")]
    return base


def client_version() -> str:
    """客户端版本号（``loomy-version`` 头）。"""
    return os.getenv("LOOMY_CLIENT_VERSION", "").strip() or DEFAULT_CLIENT_VERSION
