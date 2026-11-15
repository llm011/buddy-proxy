"""请求指纹：对齐 Antigravity 客户端的请求头形态。

antigravity-cli（agy，Go）不开源，指纹以社区验证过的 antigravity-claude-proxy /
CLIProxyAPI / gcli2api 的形态为基准（JSON REST 已被服务端大量用户验证接受），
版本号取值策略同参考实现：

1. 环境变量覆盖；
2. 本机安装的 Antigravity.app 的 product.json（``ideVersion`` 进 User-Agent、
   ``version`` 进 X-Client-Version）；
3. 写死 fallback（UA 2.32.1 / X-Client-Version 1.110.0）。

User-Agent **形态**是 Gemini 4 族的准入开关，不只是版本门槛：2026-11-15 实测
只有 ``antigravity/hub/<版本> (<元数据>)``（带字面 ``hub/`` 路径段 + 圆括号元数据
``aidev_client; os_type=darwin; arch=arm64``）能路由 ``gemini-4-*``；
``antigravity/<版本> <os>/<arch> <model>`` 旧形态对该族恒 404（其余模型正常）。
且 UA 版本 ≥ 2.23.0（CLIProxyAPI 注释同款，云码端低于该版本拒绝新模型），
故本地 product.json 版本低于门槛时仍抬到 fallback，避免装老客户端反而锁死新模型。

与 gemini 通道的关键差异：antigravity 多 ``X-Client-Name`` / ``X-Client-Version``
头，且 ``x-goog-api-client`` 模拟 Node 客户端环境（gl-node + fire + grpc）。
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

#: product.json 读不到时的兜底。
#: UA 版本是上游的**版本门槛**，不是装饰：2026-11-15 实测 antigravity/2.0.3
#: 及 2.5.0 请求 streamGenerateContent 恒返回单事件固定文案
#: 「This version of Antigravity is no longer supported.」（无 usageMetadata、
#: 无 finishReason，是网关短路，压根没进模型）；2.9.0 起才返回真回复。
#: 且 Gemini 4 族要求 ≥ 2.23.0（见模块 docstring），故取本机官方客户端实测
#: 最新版 2.32.1。只认 UA，不认 X-Client-Version——把后者从 1.110.0 单独提到
#: 2.0.0 仍得到提示文本（同一轮交叉实测），所以那个值保持不动。
FALLBACK_UA_VERSION = "2.32.1"
FALLBACK_CLIENT_VERSION = "1.110.0"

#: Gemini 4 族可用所需的最低 UA 版本（云码端低于该版本拒绝新模型）。
MIN_UA_VERSION = "2.23.0"

#: hub UA 的元数据段，必须带圆括号、``aidev_client`` 打头；形态错了新模型就 404。
HUB_METADATA_PREFIX = "aidev_client"

#: 模拟 Google Node.js 客户端环境的 x-goog-api-client（参考实现写死值）。
GOOG_API_CLIENT = "gl-node/18.18.2 fire/0.8.6 grpc/1.10.x"

_UA_VERSION: str | None = None
_CLIENT_VERSION: str | None = None


def _product_json_paths() -> list[Path]:
    app = Path("/Applications/Antigravity.app/Contents/Resources/app/product.json")
    user_app = Path.home() / "Applications/Antigravity.app/Contents/Resources/app/product.json"
    return [app, user_app]


def _version_tuple(text: str) -> tuple[int, ...] | None:
    # 截掉 ``2.32.1-beta`` / ``2.32.1+build`` 这类预发布/构建后缀，只比数字段；
    # 否则带后缀的本机真实版本会被判成不可解析，无谓地掉回 fallback。
    core = text.strip().split("-", 1)[0].split("+", 1)[0]
    try:
        return tuple(int(part) for part in core.split("."))
    except ValueError:
        return None


def _at_least(version: str, floor: str) -> bool:
    """版本串比较；任一方无法解析时返回 False（按不达标处理）。"""
    got, need = _version_tuple(version), _version_tuple(floor)
    if got is None or need is None:
        return False
    width = max(len(got), len(need))
    return got + (0,) * (width - len(got)) >= need + (0,) * (width - len(need))


def _detect_versions() -> tuple[str, str]:
    """(UA 版本, X-Client-Version)：env > 本机 product.json > fallback。

    UA 版本低于 ``MIN_UA_VERSION`` 时抬到 fallback：本机装了老客户端不代表
    该用老 UA——形态/版本不达标会直接丢掉 Gemini 4 族（见模块 docstring）。
    env 显式覆盖不做抬升，留给排障时压测旧版本。
    """
    ua = os.environ.get("ANTIGRAVITY_UA_VERSION", "").strip()
    client = os.environ.get("ANTIGRAVITY_CLIENT_VERSION", "").strip()
    if not (ua and client):
        try:
            for path in _product_json_paths():
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                ua = ua or str(data.get("ideVersion") or "").strip()
                client = client or str(data.get("version") or "").strip()
                if ua and client:
                    break
        except (OSError, json.JSONDecodeError):
            pass
    if os.environ.get("ANTIGRAVITY_UA_VERSION", "").strip():
        detected_ua = ua or FALLBACK_UA_VERSION
    else:
        detected_ua = ua if ua and _at_least(ua, MIN_UA_VERSION) else FALLBACK_UA_VERSION
    return detected_ua, client or FALLBACK_CLIENT_VERSION


def ua_version() -> str:
    global _UA_VERSION
    if _UA_VERSION is None:
        _UA_VERSION = _detect_versions()[0]
    return _UA_VERSION


def client_version() -> str:
    global _CLIENT_VERSION
    if _CLIENT_VERSION is None:
        _CLIENT_VERSION = _detect_versions()[1]
    return _CLIENT_VERSION


def user_agent(model: str = "") -> str:
    """``antigravity/hub/<version> (aidev_client; os_type=<os>; arch=<arch>)[ <model>]``。

    Gemini 4 族的准入取决于这个**形态**，不是版本号（见模块 docstring）。版本
    取 ``<_detect_versions>`` 的结果（≥ MIN_UA_VERSION）。``model`` 保留为可选
    尾巴：实测不影响路由，调用方按需附带，便于上游侧遥测对账。
    """
    plat = platform.system().lower()  # darwin / linux / windows
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    metadata = f"{HUB_METADATA_PREFIX}; os_type={plat}; arch={arch}"
    out = f"antigravity/hub/{ua_version()} ({metadata})"
    model = (model or "").strip()
    if model:
        out += f" {model}"
    return out


def auth_headers(access_token: str, model: str = "", stream: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": user_agent(model),
        "X-Client-Name": "antigravity",
        "X-Client-Version": client_version(),
        "x-goog-api-client": GOOG_API_CLIENT,
    }
    if stream:
        headers["Accept"] = "text/event-stream"
    return headers


def metadata_headers() -> dict[str, str]:
    """loadCodeAssist / onboardUser 等管理接口请求头（与对话请求同款）。"""
    return auth_headers(access_token="")
