"""Copilot 上游：目录拉取、请求头、错误归一、额度查询。

本模块只放**不依赖 provider 实例**的纯函数（同步 urllib），provider 层负责任务
编排与协议转换。三条上游端点共用同一套请求头：

    POST https://api.githubcopilot.com/chat/completions   （OpenAI chat）
    POST https://api.githubcopilot.com/responses          （OpenAI Responses）
    POST https://api.githubcopilot.com/v1/messages        （Anthropic Messages）

**请求头为什么必须长得像 VS Code**：Copilot 网关按 ``copilot-integration-id``
与 editor/plugin 版本判是否放行，缺头会回 401 或「客户端不受支持」。版本号
与 gh-copilot / 主流开源代理保持一致（低于它一样被拒）。``X-Initiator`` 表示
这一轮由谁发起（用户输入 vs 模型续跑工具循环），网关据此计费与限流；它取自
请求体里最后一条消息的角色，见 ``classify_initiator``。

**``/copilot_internal/*`` 在 api.github.com 上**（不是 api.githubcopilot.com），
外加 GitHub REST 要求的 ``X-GitHub-Api-Version: 2022-11-28``。
"""

from __future__ import annotations

import datetime
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .credentials import AuthError, token_fingerprint

log = logging.getLogger(__name__)

#: Copilot 推理网关根。
API_BASE = "https://api.githubcopilot.com"
#: GitHub REST（``/copilot_internal/user`` 等）。
GITHUB_BASE = "https://api.github.com"

#: 上游路径。目录里 ``supported_endpoints`` 用的就是这几条，两处必须一致。
PATH_CHAT = "/chat/completions"
PATH_RESPONSES = "/responses"
PATH_MESSAGES = "/v1/messages"
PATH_MODELS = "/models"

#: 与上游校对过的客户端身份。改版本号前先确认上游仍认（过低会被拒）。
_INTEGRATION_ID = "vscode-chat"
_EDITOR_VERSION = "vscode/1.104.3"
_EDITOR_PLUGIN_VERSION = "copilot-chat/0.26.7"
_USER_AGENT = "GitHubCopilotChat/0.26.7"
_GITHUB_API_VERSION = "2025-04-01"
_GITHUB_REST_VERSION = "2022-11-28"
_ANTHROPIC_VERSION = "2023-06-01"


def upstream_headers(
    token: str,
    *,
    initiator: str = "user",
    vision: bool = False,
    stream: bool = False,
) -> dict[str, str]:
    """构造 Copilot 网关请求头（不含 ``Content-Type``，由调用方按需补）。

    ``initiator``：``user``（用户发起）/ ``agent``（模型续跑工具循环）。
    ``vision``：请求里有图片时置 ``Copilot-Vision-Request: true``——实测缺它
    时图片模型会把图当纯文本（或直接 400）。``stream`` 只决定 ``Accept``。
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "copilot-integration-id": _INTEGRATION_ID,
        "editor-version": _EDITOR_VERSION,
        "editor-plugin-version": _EDITOR_PLUGIN_VERSION,
        "user-agent": _USER_AGENT,
        "x-github-api-version": _GITHUB_API_VERSION,
        "x-vscode-user-agent-library-version": "electron-fetch",
        "X-Initiator": "agent" if initiator == "agent" else "user",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    if vision:
        headers["Copilot-Vision-Request"] = "true"
    return headers


def anthropic_headers(token: str, *, initiator: str = "user",
                      stream: bool = False) -> dict[str, str]:
    """``/v1/messages`` 额外要求 ``anthropic-version``。"""
    headers = upstream_headers(token, initiator=initiator, stream=stream)
    headers["anthropic-version"] = _ANTHROPIC_VERSION
    return headers


def github_headers(token: str) -> dict[str, str]:
    """api.github.com 的请求头（REST 版本与 Copilot 网关不同）。"""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _GITHUB_REST_VERSION,
        "User-Agent": _USER_AGENT,
    }


# ---------------------------------------------------------------------------
# X-Initiator
# ---------------------------------------------------------------------------

def classify_initiator(messages: Any) -> str:
    """按最后一条消息的角色判断这一轮由谁发起。

    ``user`` → 用户新输入；``tool`` / 带 ``tool_calls`` 的 assistant → 模型
    续跑（agent）。网关用它区分「人类回合」与「自动续跑」，计费口径不同；
    一律报 ``user`` 会让长工具循环被当成人连续提问，限流更早触发。

    ``messages`` 不是列表（例如 Responses 的 ``input`` 字符串）时按 ``user``。
    """
    if not isinstance(messages, list) or not messages:
        return "user"
    last = messages[-1]
    if not isinstance(last, dict):
        return "user"
    role = str(last.get("role") or "")
    if role == "tool":
        return "agent"
    if role == "assistant" and last.get("tool_calls"):
        return "agent"
    return "user"


def body_has_image(messages: Any) -> bool:
    """请求体里有没有图片块（决定要不要带 ``Copilot-Vision-Request``）。

    只认 OpenAI chat 的多段 content（``{"type": "image_url"}``）与 Anthropic 的
    ``image`` 块。深搜而非只看最后一条：多轮对话里图片可能在任何位置。
    """
    stack: list[Any] = list(messages) if isinstance(messages, list) else [messages]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            kind = node.get("type")
            if kind in ("image_url", "image", "input_image"):
                return True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


# ---------------------------------------------------------------------------
# 错误归一
# ---------------------------------------------------------------------------

#: 上游错误体里可能承载错误码/文案的位置（chat / responses / 各自的嵌套）。
_ERROR_PATHS = (
    ("error",),
    ("error", "message"),
    ("error", "code"),
    ("message",),
    ("code",),
    ("detail",),
)


def _dig(payload: Any, path: tuple[str, ...]) -> str:
    node: Any = payload
    for key in path:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    return str(node) if isinstance(node, (str, int, float)) else ""


def extract_error(payload: Any) -> tuple[str, str]:
    """从上游错误体里取 ``(code, message)``；取不到返回 ``("", "")``。

    三种形状都要认：

    - chat：``{"error": {"message": "...", "code": "..."}}``
    - 400 模型拒绝（实测）：``{"error": {"message": "model not supported ...",
      "code": "model_not_supported"}}``
    - Anthropic 透出的错误：``{"type": "error", "error": {"type": "...",
      "message": "..."}}``——``type`` 才是码。

    ``message`` 只在顶层 ``error`` 下找不到时才去别的路径找，避免把内层
    ``code`` 当成文案回给用户。
    """
    if not isinstance(payload, dict):
        return "", ""
    code = _dig(payload, ("error", "code")) or _dig(payload, ("error", "type")) \
        or _dig(payload, ("code",))
    message = _dig(payload, ("error", "message"))
    if not message:
        for path in _ERROR_PATHS:
            if path[0] == "error":
                continue
            found = _dig(payload, path)
            if found:
                message = found
                break
    return code, message


# ---------------------------------------------------------------------------
# 同步 HTTP（目录 / 额度）
# ---------------------------------------------------------------------------


def _request_json(url: str, headers: dict[str, str], *, timeout: float,
                  method: str = "GET", body: Any = None,
                  auth_hint: str = "") -> Any:
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, headers=headers, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        snippet = ""
        try:
            snippet = exc.read()[:400].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 读错误体失败不影响主判定
            pass
        if exc.code in (401, 403):
            fingerprint = token_fingerprint(
                headers.get("Authorization", "").removeprefix("Bearer "))
            raise AuthError(_auth_hint(auth_hint, exc.code, fingerprint)) from exc
        raise AuthError(f"上游 {url} 返回 HTTP {exc.code}: {snippet}") from exc
    except Exception as exc:  # noqa: BLE001 - urllib 报错种类多
        raise AuthError(f"无法连接 {url}：{exc}") from exc


def _auth_hint(auth_hint: str, status: int, fingerprint: str) -> str:
    """401/403 的可操作提示。

    ``auth_hint`` 由调用方按场景给原因：目录接口的 401 更多是「这个账号没有
    Copilot 订阅」，而推理接口的 401 才是 token 失效——两者的下一步动作不同，
    一句通用的「重新登录」会把人支去做无用功。
    """
    if auth_hint:
        return auth_hint
    return (
        f"GitHub 拒绝了该 token（HTTP {status}，指纹 {fingerprint}）："
        f"登录态可能已被撤销或过期，请重新 `gh auth login --user <账号>` "
        f"或 `buddy login copilot`"
    )


def fetch_models(token: str, *, timeout: float = 20.0) -> Any:
    """``GET /models`` 原始 payload（解析交给 ``catalog.parse_catalog``）。"""
    return _request_json(
        f"{API_BASE}{PATH_MODELS}", github_headers(token), timeout=timeout,
        auth_hint=(
            f"Copilot 目录接口拒绝了该 token（指纹 {token_fingerprint(token)}）："
            f"该 GitHub 账号可能没有 Copilot 订阅，或登录态已失效——"
            f"请确认该账号已开通 Copilot，再重新 `buddy login copilot`"
        ),
    )


def fetch_user_usage(token: str, *, timeout: float = 20.0) -> dict[str, Any]:
    """``GET /copilot_internal/user``：套餐 + 配额快照（premium 交互数等）。

    401/403 在这里**不是**「token 失效」——``/user`` 能过就说明 token 是好的；
    这个接口拒更多是「该账号没有 Copilot 订阅」或接口口径变了。所以错误提示
    单独写，别把人支去重新登录（额度面板拿不到数据不该让人去改登录态）。
    """
    try:
        data = _request_json(f"{GITHUB_BASE}/copilot_internal/user",
                             github_headers(token), timeout=timeout)
    except AuthError as exc:
        raise AuthError(
            f"Copilot 额度接口 /copilot_internal/user 不可用（{exc}）；"
            f"该账号可能没有 Copilot 订阅，或接口口径已变"
        ) from exc
    return data if isinstance(data, dict) else {}


def parse_user_usage(data: dict[str, Any]) -> list[dict[str, Any]]:
    """``/copilot_internal/user`` → 额度条目（``quota()`` 的 ``items``）。

    只挑**有上限**的项（``unlimited: true`` 的 chat/completions 不画——无限
    额度的进度条只会是 0% 常驻；它们的信息并进 premium 那条的说明里）。
    ``quota_reset_at`` 是周期重置时刻 → ``reset_ts``（不是权益到期）。

    单位是「交互次数」（premium requests），前端显示 ``unit="次"``。
    """
    snapshots = data.get("quota_snapshots")
    if not isinstance(snapshots, dict):
        return []
    fallback_reset_ts = quota_reset_ts(data)
    labels = {
        "premium_interactions": "Premium 交互",
        "chat": "Chat",
        "completions": "Completions",
    }
    items: list[dict[str, Any]] = []
    for key, label in labels.items():
        snap = snapshots.get(key)
        if not isinstance(snap, dict):
            continue
        if snap.get("unlimited"):
            continue
        total = _number(snap.get("entitlement"))
        remaining = _number(snap.get("remaining"))
        if remaining is None:
            remaining = _number(snap.get("quota_remaining"))
        if total is None and remaining is None:
            continue
        used = None
        if total is not None and remaining is not None:
            used = max(total - remaining, 0)
        # percent 是**已用**百分比（``providers/base.py`` 的契约，进度条按
        # 它填色：≥85% 红、≥60% 黄）。上游给的是 ``remaining``/``entitlement``，
        # 这里按剩余算会把「还剩 99%」画成满格红条——用户 2026-11-16 截图反馈
        # 的正是这个：剩 1,491 / 1,500 却显示「已用 99.4%」。
        # ``used`` 拿不到时 ``percent`` 留 None（面板显示「已用未知」），不按
        # 0 已用算——上游只回包容量、还没回用量时，按 0 算会画出满格绿条，
        # 把「未知」说成「一点没用」，比不显示更误导（同 trae/pat/quota.py）。
        percent = round(used / total * 100, 1) if total and used is not None else None
        reset_ts = _parse_reset_ts(snap.get("quota_reset_at")) or fallback_reset_ts
        items.append({
            "label": label,
            "used": used,
            "total": total,
            "remaining": remaining,
            "percent": percent,
            "reset_ts": reset_ts,
            "expire_ts": None,
            "unit": "次",
        })
    return items


def _parse_reset_ts(value: Any) -> float | None:
    """单个重置时间字段 → epoch 秒（0 / 负数占位值返回 None）。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value <= 0:
            return None
        return float(value) / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        # 纯日期串（如 "2026-12-01"）按 UTC 零点算，与 quota_reset_date_utc 口径一致。
        if "T" not in text and " " not in text and len(text) == 10:
            try:
                day = datetime.date.fromisoformat(text)
                return datetime.datetime.combine(
                    day, datetime.time.min, tzinfo=datetime.timezone.utc
                ).timestamp()
            except ValueError:
                return None
        try:
            dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    return None


def quota_reset_ts(data: dict[str, Any]) -> float | None:
    """周期重置时刻：快照内非零 ``quota_reset_at`` 优先，否则取顶层重置日期。

    实测 GitHub ``/copilot_internal/user`` 在 ``quota_snapshots.*`` 里把
    ``quota_reset_at`` 填成占位 ``0``，真正的下周期重置时刻放在顶层
    ``quota_reset_date_utc``（``2026-12-01T00:00:00.000Z``）和
    ``quota_reset_date``（``2026-12-01``）。旧逻辑把 ``0`` 当有效 epoch
    直接返回，导致前端 ``it.reset_ts ? ...`` 判假，整栏不显示重置时间。
    """
    snapshots = data.get("quota_snapshots")
    if isinstance(snapshots, dict):
        for snap in snapshots.values():
            if not isinstance(snap, dict):
                continue
            parsed = _parse_reset_ts(snap.get("quota_reset_at"))
            if parsed is not None:
                return parsed
    for key in ("quota_reset_date_utc", "quota_reset_at", "quota_reset_date"):
        parsed = _parse_reset_ts(data.get(key))
        if parsed is not None:
            return parsed
    return None


def _reset_ts(snapshots: dict[str, Any]) -> float | None:
    """兼容旧单测/调用点：仅从 snapshots 抽非零 ``quota_reset_at``。"""
    return quota_reset_ts({"quota_snapshots": snapshots})


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def plan_label(data: dict[str, Any]) -> str:
    """套餐名（``copilot_plan`` / ``access_type_sku``）；取不到返回空串。"""
    for key in ("copilot_plan", "access_type_sku"):
        text = str(data.get(key) or "").strip()
        if text:
            return text.capitalize()
    return ""


# ---------------------------------------------------------------------------
# 计费换算
# ---------------------------------------------------------------------------

#: Copilot 用量单位：``total_nano_aiu`` 是 AIU 的十亿分之一。
_NANO = 1e9


def credit_from_usage(usage: Any) -> float | None:
    """从上游 ``usage`` 里换算本次消耗的 AIU 积分。

    chat 与 responses 两条端点都会在 ``usage`` 旁边给 ``copilot_usage``::

        {"token_details": [{..., "token_count": 123, "token_type": "input"}],
         "total_nano_aiu": 42000000}

    ``total_nano_aiu / 1e9`` 即 AIU（积分）。没有 ``copilot_usage``（老模型/
    企业网关不回）返回 **0.0** 而不是 ``None``：Copilot 订阅走的是**固定月费**
    （premium 请求按次数扣、token 本身不单独计费），token 开销换算成「元」
    只能是 0；返回 None 会让日志的积分列空着，读起来像「没记账」。

    **绝不按 token 数硬估**：估出来的数字会和真实账单对不上，积分列就失去意义。
    """
    if not isinstance(usage, dict):
        return None
    cu = usage.get("copilot_usage")
    if not isinstance(cu, dict):
        return 0.0 if usage else None
    value = cu.get("total_nano_aiu")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value) / _NANO, 9)


def normalize_billing_usage(usage: Any) -> dict[str, Any] | None:
    """把上游 ``usage`` 整理成 metrics 认的形态（只做**加法**，不改原字段）。

    - 顶层补 ``credit``（见 :func:`credit_from_usage`），``normalize_usage``
      的 ``_credit_field`` 会取它；
    - ``copilot_usage`` 里的 ``cache_write`` 计数挪到
      ``cache_creation_input_tokens``——Copilot 的 ``cache_read`` 已经含在
      ``prompt_tokens`` 里（OpenAI 口径），只有 **写入** 是 Anthropic 口径下
      要另算的；不挪的话 ``normalize_usage`` 只能看到 ``cached``，缓存创建
      的 token 在日志里直接消失。

    ``credit`` 恒有值（无 ``copilot_usage`` 时为 0.0，理由见
    :func:`credit_from_usage`）；其余字段不改写、不臆造。
    """
    if not isinstance(usage, dict):
        return None
    cu = usage.get("copilot_usage")
    out = dict(usage)
    credit = credit_from_usage(usage)
    if credit is not None:
        out["credit"] = credit
    if isinstance(cu, dict):
        write = 0
        for detail in cu.get("token_details") or []:
            if not isinstance(detail, dict):
                continue
            if str(detail.get("token_type") or "") != "cache_write":
                continue
            count = detail.get("token_count")
            if isinstance(count, int) and not isinstance(count, bool):
                write += count
        if write:
            out["cache_creation_input_tokens"] = write
    return out
