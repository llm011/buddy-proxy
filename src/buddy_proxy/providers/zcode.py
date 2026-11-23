"""ZCode / BigModel (GLM) provider —— Anthropic 兼容端点直通转发。

上游为智谱 Z.ai / BigModel 的 Anthropic 兼容 coding 端点（与 ZCode CLI 的
bigmodel-coding-plan 通道同款）：

- BigModel（国内）: https://open.bigmodel.cn/api/anthropic  （/v1/messages）
- Z.AI（国际）:     https://api.z.ai/api/anthropic

协议与路由（与 trae/豆包 provider 的多协议约定一致）：
- anthropic（/v1/messages）→ 直通上游 ``{base}/v1/messages``，model 名透传，
  零转换开销；响应（流式/非流式）原样回传。
- openai（/v1/chat/completions）→ GLM Coding Plan 的 OpenAI 端点
  ``https://open.bigmodel.cn/api/coding/paas/v4/chat/completions``（注意 /coding
  前缀：标准 /api/paas/v4 按普通余额计费，Coding Plan 账户 429 1113），请求体透传；
  响应（含 SSE）原样回传。
- responses（/v1/responses）→ 走通用链路（anthropic_adapter 转成 chat 后
  到这里，即 openai 协议路径）。

认证（凭据来源优先级）：
1. 环境变量 ``ZCODE_API_KEY``
2. 本项目的 key 文件 ``~/.buddy-proxy/zcode_api_key``（``buddy login zcode`` 会打印）
3. 本机 ZCode CLI 配置 ``~/.zcode/v2/config.json`` 中已启用的
   ``builtin:bigmodel-coding-plan`` / ``builtin:zai`` 等 provider 的 apiKey
   （格式 ``<apiKey>.<secretKey>``，即智谱官网 coding-plan API Key）

安全：API key 只在服务端使用，绝不明文进日志；状态目录 0700、文件 0600。

套餐权限（2026-09-19 实测）：模型出现在上游 ``/models`` 列表 ≠ 当前订阅可用。
``glm-5.3-flashx`` 已在端点模型表中，但本机订阅调它被拒为
``429 code 1311「当前订阅套餐暂未开放GLM-5.3-FlashX权限」``；同一把 key 打
``glm-5.3`` / ``glm-5.3-flash`` / ``glm-5-turbo`` 均 200，可见是套餐授权缺口
而非 key 失效。因此 flashx 只作**预备接入**：订阅开通后无需改代码即可使用。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .base import BaseProvider
from ..core.errors import describe_exception
from ..core.paths import state_file

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量与默认模型表
# ---------------------------------------------------------------------------

BIGMODEL_ANTHROPIC_BASE = "https://open.bigmodel.cn/api/anthropic"
ZAI_ANTHROPIC_BASE = "https://api.z.ai/api/anthropic"
# Coding Plan 的 openai 端点带 /coding 前缀（资源包只覆盖它）；标准 /api/paas/v4
# 按普通 API 余额计费，Coding Plan 账户打过去 429 code=1113「余额不足或无可用
# 资源包，请充值」（2026-09 实测）。ZCODE_OPENAI_BASE 可整体覆盖。
BIGMODEL_OPENAI_BASE = "https://open.bigmodel.cn/api/coding/paas/v4"
ZAI_OPENAI_BASE = "https://api.z.ai/api/coding/paas/v4"


def _openai_base_for(anthropic_base: str) -> str:
    """由 anthropic base 推导同域 openai 兼容端点（Coding Plan 的 /coding 变体）。

    key 从 ZCode CLI 配置解析、且启用的是 zai（国际 api.z.ai）通道时，
    key 是 z.ai 域的——openai/responses 请求若还打 open.bigmodel.cn 必然
    401。anthropic 请求走 self._base 没问题，openai 端点必须同域跟随。
    ZCODE_OPENAI_BASE 环境变量可整体覆盖（如官方未来调整端点）。
    """
    override = os.environ.get("ZCODE_OPENAI_BASE", "").strip().rstrip("/")
    if override:
        return override
    host = urllib.parse.urlparse(anthropic_base).netloc.lower()
    return ZAI_OPENAI_BASE if "z.ai" in host else BIGMODEL_OPENAI_BASE

# 与 ZCode CLI 内置模型表一致（~/.zcode/v2/config.json）。
# id 用**小写**（与全站 models_config.json / Claude Code 侧配置一致），
# 这样 forward_chat 的「自动匹配 provider.models()」能命中 zcode，
# 不至于让 glm-* 请求错误落到 codebuddy 兜底通道。
#
# ``max_tokens`` 上游上限 **131072**，超了回
# ``400 {"code":1210,"message":"max_tokens参数非法：限制数值范围[1,131072]"}``
# （2026-11-23 实测）。客户端传更大值时要提前夹住，否则用户看到的是一个
# 跟模型能力无关的参数报错。注意这**不是**上下文窗口（那个约 1M，见下）。
DEFAULT_MODELS: dict[str, str] = {
    # 5.4 只有裸名一个：``glm-5.4-flash`` / ``-flashx`` / ``-turbo`` 上游一律
    # 回 1211「模型不存在」（2026-11-23 实测，``/models`` 列表里也只有
    # ``glm-5.4``）。别照 5.3 的形状顺手补变体，那是凭空的。
    #
    # 上下文窗口是**二分实测**的：1037500 token 通过、1053125 被
    # ``1261 Prompt exceeds max length`` 拒 → 约 1M，与 5.3 同档。
    "glm-5.4": "GLM-5.4 (thinking, multimodal, 1M ctx)",
    "glm-5.3": "GLM-5.3 (thinking, 1M ctx)",
    "glm-5.3-flash": "GLM-5.3-Flash (thinking, multimodal, 1M ctx)",
    "glm-5.3-flashx": "GLM-5.3-FlashX (thinking, 1M ctx)",
    "glm-5-turbo": "GLM-5-Turbo (200K ctx)",
}

#: glm-5.4 是 2026-11-23 随 Coding Plan 上线的旗舰（实测上游 openai 端点
#: ``/models`` 已列出，created=1795406400；anthropic 端点的 ``/v1/models``
#: **还没跟上**，列表止于 glm-5.3-flashx-legal）。所以这张表必须手写补上：
#: 本 provider 的目录是静态表，等上游列表自动出现等于永远看不到新模型。
#:
#: 实测口径（2026-11-23，本机 lite 套餐）：
#: - 上下文：1,048,576（"hello "×1,000,000 通过；×1,100,000 报
#:   ``1261 Prompt exceeds max length``，中间档撞 5 小时额度上限没能细切，
#:   故取 1M 整）
#: - 输出：max_tokens 131072 合法、131073 起报 ``1210 限制数值范围[1,131072]``
#: - 视觉：anthropic 端点带 base64 图片 200 且答对了颜色
#: - 工具调用：openai 端点 ``finish_reason=tool_calls``，arguments 是合法 JSON
#: - ``glm-5.4-flash`` / ``glm-5.4-flashx`` 均 1211 模型不存在，别按 5.3 的
#:   flash 家族类推。
GLM_54_MAX_INPUT = 1_048_576
GLM_54_MAX_OUTPUT = 131_072

#: 小写 id → ``models()`` 要额外带出的能力/窗口字段。
#:
#: 只登记**实测过**的模型：这张表是给客户端的承诺（图片会不会被本地剥掉、
#: 上下文判断多少），没测过的模型不写，聚合时自然退回其它通道或「未知」。
#: ``glm-5.3`` 系列不在这里——它们由 codebuddy/trae/qoder 等多个通道一起报，
#: 聚合时取并集更宽松，单在这里声明反而会把能力收窄。
#:
#: 这些字段**只是目录元数据**，本仓库里没有任何代码拿 ``max_output`` 去夹请求：
#: ``web/ui/models_api.py`` 分组时只挑 ``context_window``，``web/static/*.js``
#: 连 ``context_window`` 都没读（纯 Codex/OpenAI 目录扩展项）。所以客户端传
#: 200k ``max_tokens`` 时，上游那条 ``1210 限制数值范围[1,131072]`` 仍然会原样
#: 冒出来——夹住它是调用方的事。别以为改了这里的数字就能防住那个报错。
MODEL_CAPABILITIES: dict[str, dict[str, Any]] = {
    "glm-5.4": {
        "images": True,
        "tool_call": True,
        "reasoning": True,
        "max_input": GLM_54_MAX_INPUT,
        "max_output": GLM_54_MAX_OUTPUT,
    },
}

# 小写 id → 上游正式模型名（ZCode/智谱侧的大小写字面量）。
# anthropic 直通时若上游对 model 名大小写敏感，用它归一化后再转发。
# 实测（2026-09-19）：该端点的 model 名**大小写不敏感**——glm-5.3-flashx /
# GLM-5.3-FlashX / GLM-5.3-flashx 四种写法都解析到同一个 GLM-5.3-FlashX
# （乱名才报 1211 模型不存在）。保留映射只为与既有约定一致、日志可读。
MODEL_NAME_CANONICAL: dict[str, str] = {
    "glm-5.4": "GLM-5.4",
    "glm-5.3": "GLM-5.3",
    "glm-5.3-flash": "GLM-5.3-Flash",
    "glm-5.3-flashx": "GLM-5.3-FlashX",
    "glm-5-turbo": "glm-5-turbo",
}

# ---------------------------------------------------------------------------
# 积分估算（GLM Coding Plan 官方抵扣系数）
# ---------------------------------------------------------------------------

#: 模型（小写 id）→ (Input, Cached Input, Output) 抵扣系数。官方公式
#: （docs.bigmodel.cn/cn/coding-plan/overview「积分抵扣计算方式」，2026-09-30 版）：
#:
#:   模型消耗积分数 = (输入 Token×Input + 缓存命中 Token×Cached Input
#:                    + 输出 Token×Output) / 10000
#:
#: 上游 API（anthropic / openai 兼容端点实测）不回单次积分，这是唯一的官方
#: 口径。注意两个上游语义差异都已被 metrics.normalize_usage 归一：prompt 是
#: OpenAI 口径（**含**缓存命中），估算前先减掉 cached 得到未命中输入。
#: GLM-5-Turbo / GLM-4.7 上游自动切换为 GLM-5.3-Flash，按 Flash 系数抵扣；
#: glm-5.3-flashx 套餐未开放（调用即被拒），不配系数 → 不出估算值。
#:
#: ``glm-5.4`` 的系数官方文档还没更新（docs 仍停在 5.3 那版，2026-11-23 查），
#: 所以是**实测标定**的，方法：拿真实 key 发已知 token 量的请求，用
#: ``/api/monitor/usage/quota/limit`` 的 currentValue 差值反解
#: ``(Δ积分×10000 - 已知项) / 未知 token 数``。三个系数分开测，各自放大信号：
#:
#:   * output 16.0 —— 小 prompt + 长输出（3 次共 16124 输出 token，Δ=26 分）
#:   * input  4.60 —— 30 万 token 唯一输入（避免缓存）+ 3 token 输出，Δ=138 分
#:     （另用 52 万 token + ``max_tokens=1`` 零输出复核得 4.603）
#:   * cached 1.10 —— 同 body 连发两次，第二次 cached=400000，Δ=44 分
#:
#: **精度警告**：这个计数器只记到**整数分**，单请求误差 ±0.5 分。所以要测某个
#: 系数就把该 token 量放大到几百分，别用小请求——实测 304 输出 token 的请求
#: 额度变化是 **0**，据此反解会算出负数。cached 那组 44 分的样本对应约
#: ±0.01 的系数误差，够用但不算宽裕；官方更新文档后应回来核对一次。
#:
#: 比例与 5.3 的 (6.9, 1.7, 24.0) 同构（约 2/3），但**不能拿 2/3 顶替**：
#: 按 2/3 算 5.4 是 (4.6, 1.13, 16.0)，cached 实测 1.10，估算会偏。
CREDIT_COEFFS: dict[str, tuple[float, float, float]] = {
    "glm-5.4": (4.6, 1.1, 16.0),
    "glm-5.3": (6.9, 1.7, 24.0),
    "glm-5.3-flash": (2.3, 0.56, 8.0),
    "glm-5-turbo": (2.3, 0.56, 8.0),
}

#: 时段折扣判断统一用北京时间（官方高峰口径按 UTC+8 定义，与机器时区无关）。
BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))

#: 全时段 5 折的活动区间（UTC+8 日期，含端点）。活动期连工作日高峰也按
#: 非高峰计，故单独列表而非并进 credit_discount_now 的工作日规则；
#: 到期后从表里删掉即可。
PROMO_ALL_OFFPEAK: tuple[tuple[str, str], ...] = (
    ("2026-09-25", "2026-10-07"),  # 庆双节活动
)


def credit_discount_now(ts: float | None = None) -> float:
    """给定时刻的积分抵扣倍率：高峰 1×，非高峰 0.5×。

    高峰时段 = 每周一至周五 14:00–18:00（UTC+8），其余（夜间/周末/节假日
    活动期）一律 0.5×。官方口径按请求发生时刻计，与额度计数器的批量聚合
    无关。
    """
    t = time.time() if ts is None else ts
    bj = datetime.datetime.fromtimestamp(t, BEIJING_TZ)
    for lo, hi in PROMO_ALL_OFFPEAK:
        d = bj.strftime("%Y-%m-%d")
        if lo <= d <= hi:
            return 0.5
    if bj.weekday() < 5 and 14 <= bj.hour < 18:
        return 1.0
    return 0.5

_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0)


#: ``unit`` 字段的取值 -> 时间单位（秒），以及该单位在展示名里怎么说。
#:
#: 上游的 ``limits`` 是**同一个 ``type`` 可按不同时长切多档**，靠 ``unit`` +
#: ``number`` 表达窗口宽度（实测 unit=3/number=5 是 5 小时档、
#: unit=6/number=1 是月档）。``unit`` 的取值是上游自定枚举，这里只落地
#: 在真实返回值里见过的两个；其余一律走兜底，**不要**凭「单位像分钟/小时」
#: 的直觉猜——猜错了会把 5 小时说成 5 秒。
_UNIT_SECONDS: dict[int, int] = {
    3: 3600,        # 小时
    4: 86400,       # 天
    6: 86400 * 30,  # 月（按 30 天算，够用来标注「月窗口」）
}


def _window_span(unit: Any, number: Any) -> float | None:
    """把 ``unit``/``number`` 换算成窗口宽度（秒）；算不出来返回 ``None``。

    两个字段都当**不可信输入**处理：上游若是给成字符串（``"5"``），
    ``3600 * "5"`` 在 Python 里是字符串重复、不会报错，接着拿它去比较就
    抛 ``TypeError``——额度面板整块崩掉。所以先把类型收干净，不行就当
    「没给」，退回按剩余时间推断。

    ``unit`` 的取值是上游自定枚举，只认真实返回值里见过的；不认识的
    **不猜**——凭「3 像小时、6 像天」的直觉猜，猜错会把月档写成「6 天窗口」。
    """
    try:
        mult = _UNIT_SECONDS.get(int(unit))
    except (TypeError, ValueError):
        return None
    if mult is None:
        return None
    try:
        n = float(number)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return mult * n


def _window_label(
    reset_ts: float | None,
    ltype: str | None,
    span: float | None = None,
) -> str:
    """限额窗口的展示名。

    ``span``（窗口宽度，秒）由 :func:`_window_span` 从 ``unit``/``number``
    算出；给了就**以它为准**，为 ``None`` 才退回按「距下次重置还有多久」推断。

    原先只用后者，会错得离谱：它把「离重置时间的远近」当成窗口大小，于是
    一个 *月* 档（重置还在 4 天后，但窗口本身是 30 天）被写成「每周窗口」；
    而 5 小时档因为上游压根不给 ``nextResetTime``，label 直接退化成裸的
    ``CREDIT_LIMIT`` —— 两档的展示名就这么一错一空。更糟的是
    ``_quota_items`` 又按 ``nextResetTime`` 排序，没有重置时间的那档被排到
    末位，于是标题行（取 ``items[0]``）显示的恰好是它：一个既叫不出名字、
    又只是 5 小时档的 2000，压过了月档的 10000。
    """
    if ltype == "TIME_LIMIT":
        return "MCP 调用（月）"
    if span:
        if span <= 86400:
            return f"{_fmt_num(span / 3600)} 小时窗口"
        if span <= 86400 * 10:
            return f"{_fmt_num(span / 86400)} 天窗口"
        return "月窗口"
    if reset_ts:
        # 兜底：上游没给 unit 时，只能拿「距重置还有多久」凑一个大致档位。
        # 注意它量的是**剩余**不是窗口宽度，所以只能给很粗的三档。
        delta = reset_ts - time.time()
        if delta < 86400 * 2:
            return "5 小时窗口"
        if delta < 86400 * 10:
            return "每周窗口"
        return f"周期窗口（{time.strftime('%m-%d', time.localtime(reset_ts))} 重置）"
    return ltype or "用量窗口"


def _fmt_num(value: float) -> str:
    """整数就不带小数点（``5`` 而不是 ``5.0``），非整数最多给 4 位有效小数。

    ``span / 86400`` 在恰好 10 天时是 ``10.0``，直接插进 f-string 会渲染成
    「10.0 天窗口」；而 ``span / 3600`` 遇到不足 1 小时的窗口（上游给分钟级）
    会铺出 ``0.0166667`` 这种一长串。展示名是给人看的，两种都修掉。
    """
    if value == int(value):
        return str(int(value))
    return f"{value:.4g}"


def _quota_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    """把 /api/monitor/usage/quota/limit 的 limits 归一化为管理页额度条目。

    **顺序即语义**：ZCode 没有 ``sum_items``（两档是各自独立的额度，不能相加），
    所以管理页标题行取 ``items[0]``。这里按窗口**由小到大**排——小的（5 小时）
    在前、大的（月）在后，用户与标题行先看到最容易撞到的那一档。

    **不要**改回按 ``nextResetTime`` 排：5 小时档上游不给这个字段（值恒为 0），
    按时间排会把它甩到末位，标题行转而去显示月档——一个不常撞到的数。

    CREDIT_LIMIT：usage=窗口总额度，currentValue=已用；TIME_LIMIT 单位为次数。
    """
    limits = [l for l in (data.get("limits") or []) if isinstance(l, dict)]

    def _span(l: dict[str, Any]) -> float:
        """排序键：窗口宽度。算不出宽度的排最后（当它最大，不抢标题行）。"""
        span = _window_span(l.get("unit"), l.get("number"))
        return span if span is not None else float("inf")

    limits.sort(key=_span)
    items: list[dict[str, Any]] = []
    for l in limits:
        reset_ms = l.get("nextResetTime")
        reset_ts = reset_ms / 1000 if reset_ms else None
        span = _window_span(l.get("unit"), l.get("number"))
        items.append({
            "label": _window_label(reset_ts, l.get("type"), span),
            "used": l.get("currentValue"),
            "total": l.get("usage"),
            "remaining": l.get("remaining"),
            "percent": l.get("percentage"),
            # 窗口周期重置（5 小时 / 月），不做到期告警——给了 expire_ts
            # 会让横幅把「每 5 小时重置一次」误报成「快到期了」
            "reset_ts": reset_ts,
            "expire_ts": None,
            "unit": "count",
        })
    return items


# ---------------------------------------------------------------------------
# 凭证解析
# ---------------------------------------------------------------------------

def secret_file_path() -> Path:
    """本项目自己的 key 文件：``~/.buddy-proxy/zcode_api_key``。

    与其它状态文件同目录（``core.paths.state_file``），可用
    ``BUDDY_PROXY_STATE_DIR`` 整体挪走。不读任何外部工具/其它 agent 的
    secrets 目录——那些文件不归本项目管，混读会让「谁该写这个文件」变得
    说不清。

    对外公开（``_login_zcode`` 要把它打印给用户看），所以不带下划线。
    """
    return state_file("zcode_api_key")


def _load_secret_file_impl(path: Path) -> str:
    """读 key 文件（``path`` 由调用方给），支持 ``name=value`` 或裸 value。

    只取首行、且按行切分：文件若真有多行，把整块文本当 key 发给上游必然
    401（单行文件不受影响）。按行解析天然规避该问题。

    拆出 path 参数版（``_load_secret_file_impl``）是给 glm 渠道复用的——
    凭据读法相同、文件名不同。
    """
    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line:
                continue
            return line.split("=", 1)[1].strip() if "=" in line else line
    except Exception:
        pass
    return ""


def _load_secret_file() -> str:
    return _load_secret_file_impl(secret_file_path())


def _load_zcode_config_key() -> tuple[str, str]:
    """从本机 ZCode CLI 配置读取已启用 provider 的 apiKey 与 baseURL。

    返回 (api_key, anthropic_base)；enabled 通道优先，其次按配置顺序。
    """
    try:
        cfg = json.loads((Path.home() / ".zcode" / "v2" / "config.json").read_text())
    except Exception:
        return "", ""
    providers = cfg.get("provider") or {}
    ordered = sorted(
        providers.items(),
        key=lambda kv: bool((kv[1] or {}).get("enabled")),
        reverse=True,
    )
    for _pid, p in ordered:
        if not isinstance(p, dict) or p.get("kind") != "anthropic":
            continue
        opts = p.get("options") or {}
        key = opts.get("apiKey") or ""
        base = opts.get("baseURL") or ""
        if key:
            return key, base
    return "", ""


def resolve_credentials() -> tuple[str, str]:
    """解析 (api_key, anthropic_base_url)。来源优先级见模块 docstring。"""
    key = os.environ.get("ZCODE_API_KEY", "").strip()
    if not key:
        key = _load_secret_file()
    base = ""
    if not key:
        key, base = _load_zcode_config_key()
    if not base:
        base = BIGMODEL_ANTHROPIC_BASE
    return key, base.rstrip("/")


def _auth_headers(api_key: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """上游是 Anthropic 兼容端点，用 x-api-key（与 Anthropic 官方协议一致）。"""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


# ---------------------------------------------------------------------------
# 上游响应处理（直通模式：SSE 原样回传，非流式 JSON 原样回传）
# ---------------------------------------------------------------------------

async def _pass_through_stream(
    client: httpx.AsyncClient,
    response: httpx.Response,
) -> AsyncIterator[bytes]:
    """把上游 SSE/字节流原样泵给客户端；结束后确保连接释放。"""
    try:
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk
    finally:
        await response.aclose()


def _upstream_error_response(resp: httpx.Response) -> JSONResponse:
    """把上游错误转成客户端错误响应（透传状态码与错误体，隐藏 key 痕迹）。"""
    try:
        payload = resp.json()
    except Exception:
        payload = {"error": {"message": resp.text[:500], "type": "upstream_error"}}
    return JSONResponse(status_code=resp.status_code, content=payload)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class ZcodeProvider(BaseProvider):
    id = "zcode"
    name = "ZCode (BigModel GLM, Anthropic 直通)"

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        if api_key or base_url:
            self._api_key = api_key or ""
            self._base = (base_url or BIGMODEL_ANTHROPIC_BASE).rstrip("/")
        else:
            key, base = resolve_credentials()
            self._api_key = key
            self._base = base
        self._client: httpx.AsyncClient | None = None

    # ---- BaseProvider 接口 ----

    def models(self) -> Sequence[dict[str, Any]]:
        return [
            {
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "description": desc,
                # 能力/窗口字段只有实测过的模型才带（见 MODEL_CAPABILITIES）。
                # 必须带：``glm-5.4`` 目前只有本通道提供，聚合时拿不到别的通道
                # 的并集——这里不声明 images，客户端就会把图片本地剥掉再发，
                # 而 5.4 明明能看图（实测 200 且答对颜色）。
                **MODEL_CAPABILITIES.get(model_id, {}),
            }
            for model_id, desc in DEFAULT_MODELS.items()
        ]

    def ensure_auth(self) -> None:
        if not self._api_key:
            self._api_key, self._base = resolve_credentials()
        if not self._api_key:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": (
                            "zcode 未配置认证：请设置 ZCODE_API_KEY / "
                            "~/.buddy-proxy/zcode_api_key，或在本机 ZCode CLI "
                            "登录 coding-plan（凭据存于 ~/.zcode/v2/config.json，"
                            "本 provider 会自动读取）"
                        ),
                        "type": "authentication_error",
                    }
                },
            )

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        self.ensure_auth()
        stream = bool(body.get("stream", False))

        if protocol == "anthropic":
            # 直通：/v1/messages 的原始请求体（original）就是 anthropic 格式，
            # 原样转发（tools/system/tool_result 等零损耗）；routes 传入的 body
            # 是转换后的 chat 格式，只取它上面已剥过 provider 前缀的 model 名。
            source = original if isinstance(original, dict) and original else body
            upstream_body = {k: v for k, v in source.items() if not k.startswith("_")}
            if isinstance(original, dict) and original and body.get("model"):
                # 路由层可能剥过 "zcode/" 前缀/归一化过大小写，这里统一映射回
                # 上游正式模型名（如 glm-5.3 → GLM-5.3，ZCode CLI 验证过的格式）
                upstream_body["model"] = MODEL_NAME_CANONICAL.get(body["model"], body["model"])
            url = f"{self._base}/v1/messages"
            headers = _auth_headers(self._api_key, {"Accept": "text/event-stream"})
        else:
            # openai / responses：转投 GLM 原生 OpenAI 兼容端点（model 透传）
            upstream_body = {k: v for k, v in body.items() if not k.startswith("_")}
            url = f"{_openai_base_for(self._base)}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if stream else "application/json",
            }

        client = await self._get_client()

        async def _send():
            req = client.build_request("POST", url, json=upstream_body, headers=headers)
            return await client.send(req, stream=stream)

        try:
            try:
                resp = await _send()
            except httpx.RemoteProtocolError as exc:
                # 上游偶发在返回任何响应字节前断连（Server disconnected）——
                # 请求尚未被处理，原地重发一次是安全的；仍失败才对外报 502
                log.warning("%s upstream disconnected before response, retrying once: %s", self.id, exc)
                try:
                    resp = await _send()
                except httpx.HTTPError as exc2:
                    raise HTTPException(status_code=502, detail={
                        "error": {"message": f"{self.id} upstream error", "type": "bad_gateway"}}
                    ) from exc2
        except httpx.TimeoutException as exc:
            log.warning("%s upstream timeout: %s", self.id, exc)
            raise HTTPException(status_code=504, detail={
                "error": {"message": f"{self.id} upstream timeout", "type": "timeout"}}
            ) from exc
        except httpx.HTTPError as exc:
            # 用 describe_exception 而非裸 %s：httpcore 会把底层异常映射成
            # httpx.ReadError() 这类**自身 str() 为空**的对象，直接打日志只剩
            # 「zcode upstream error: 」一行空话，真因在下层链里看不到。
            log.warning("%s upstream error: %s", self.id, describe_exception(exc))
            raise HTTPException(status_code=502, detail={
                "error": {"message": f"{self.id} upstream error", "type": "bad_gateway"}}
            ) from exc

        if resp.status_code >= 400:
            if stream:
                # 先把错误体读完再关流：aclose() 会丢弃未读的 body，
                # 之后 _upstream_error_response 的 json()/text() 拿不到
                # 上游真实错误（429 配额 / 401 key 无效都会变成空错误体
                # 甚至 500）。错误体一般很小，先 aread() 成本可忽略。
                await resp.aread()
            try:
                return _upstream_error_response(resp)
            finally:
                await resp.aclose()

        if stream:
            return StreamingResponse(
                _pass_through_stream(client, resp),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "close"},
            )
        try:
            payload = resp.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail={
                "error": {"message": f"{self.id} upstream returned non-JSON", "type": "bad_gateway"}}
            ) from exc
        return JSONResponse(content=payload)

    # ---- 内部 ----

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client

    def health(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "configured": bool(self._api_key),
            "base_url": self._base,
            "models": list(DEFAULT_MODELS),
        }

    # ---- 额度查询（/ui 管理页消费；同步 httpx，调用方经 asyncio.to_thread 包装） ----

    def quota(self) -> dict[str, Any] | None:
        """查询 GLM Coding Plan 用量窗口（5 小时 / 每周等 CREDIT/TIME 限额）。

        端点由 anthropic base 推导同源 host（open.bigmodel.cn / api.z.ai），
        认证复用 coding-plan API key（Authorization 原样携带，无 Bearer 前缀）。
        """
        key = self._api_key or resolve_credentials()[0]
        if not key:
            raise RuntimeError("zcode 未配置 API key，无法查询额度")
        origin = self._base.split("/api/")[0]
        url = f"{origin}/api/monitor/usage/quota/limit"
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, headers={"Authorization": key, "Content-Type": "application/json"})
        if resp.status_code != 200:
            raise RuntimeError(f"quota HTTP {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        if not payload.get("success"):
            raise RuntimeError(payload.get("msg") or "quota query failed")
        data = payload.get("data") or {}
        return {"items": _quota_items(data), "level": data.get("level")}


# ---------------------------------------------------------------------------
# 冒烟自测：python -m buddy_proxy.providers.zcode
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    key, base = resolve_credentials()
    if not key:
        print("no api key found (env ZCODE_API_KEY / secrets / ~/.zcode/v2/config.json)")
        raise SystemExit(1)
    print(f"key: {key[:6]}***{key[-4:]}  base: {base}")
    with httpx.Client(timeout=60) as client:
        r = client.post(
            f"{base}/v1/messages",
            headers=_auth_headers(key),
            json={
                "model": "GLM-5.3-Flash",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "只回复两个字：pong"}],
            },
        )
        print(f"status: {r.status_code}")
        print(r.text[:600])
