"""小浣熊上游协议：请求构造、模型目录、积分余额。

## 协议形态

标准 OpenAI 兼容（``/api/web/llm/v2/chat/completions``），**无签名、无设备
指纹**——这是它比 qoder（私有 COSY 签名）/ trae（多套 IDE 头）都简单的地方。
``tool_calls`` 流式/非流式、``role=tool`` 回传、``include_usage`` 都实测通过。

认证只有一个 cookie 等价的头：``xhx_token: <JWT>``。

## 三个必须防的坑

1. **未知模型静默回落默认模型**：写错模型名，上游照样回 200，但换成默认
   模型了。请求前按目录校验（见 ``provider.forward``），回包首帧再比对
   ``model`` 回显——两层都要，只做一层仍会「问 A 答 B」。
2. **并发上限约 5**：第 6 个并发回 ``429 user_model_rate_limited``，且这是
   **账号×模型**的频率窗口，恢复中位数 25 分钟。所以要有每账号并发闸门
   （见 ``rate_limit``），不能靠上游 429 反馈。
3. **额度是积分不是 token**：每笔请求按模型倍率扣分（``0 = 限免``），与
   token 量无关。所以面板要显示积分与倍率，而不是 token 余额。

## 积分口径

上游分四个池（每日 / 奖励 / 月度 / 充值）。每日池：每天 00:00 发 300 分，
每份 7 天有效（最多累积 2100）。这是唯一会作废的余额，因此调度优先级要
看它——``daily_points`` 的剩余量决定这个号还能撑多久。
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

#: 上游端点（相对根地址）。
CHAT_PATH = "/api/web/llm/v2/chat/completions"
CATALOG_PATH = "/api/web/llm/v2/model_catalog"
BALANCE_PATH = "/api/web/points/v1/balance"
#: 积分流水（``type=income`` 只留发放 = 资源包明细；``all`` 会混消费）
BILLS_PATH = "/api/web/points/v2/bills"
SETTING_INFO_PATH = "/api/web/office/v3/setting_info"
USER_INFO_PATH = "/api/web/auth/v1/user_info"

#: 模型 id 的命名空间前缀：上游目录里叫 ``sn-sensenova-6-8-flash``，
#: 归一键会剥掉 ``sn-``。对外统一保留 ``sn-`` 前缀，避免与其它通道的
#: ``sensenova-*`` 撞名（同名不同上游会让 model_order 选错）。
MODEL_PREFIX = "sn-"

#: 积分倍率为 0 的模型是**限免**，调度要优先选。
FREE_MULTIPLIER = 0.0


def build_headers(access_token: str) -> dict[str, str]:
    """上游请求头。

    **认证要同时给两种形态**（缺一个就 401）：

    * ``Authorization: Bearer <jwt>`` —— 必须。只发 ``xhx_token`` 时，
      ``/api/web/points/v1/balance`` 与 ``/api/web/office/v3/setting_info``
      一律回 ``401 {"code":200001,"message":"authorization_empty_error"}``。
      2026-11-23 实测踩过：账号登录成功、额度却一直显示「查询失败」。
    * ``Cookie: xhx_token=<jwt>`` —— 网页会话形态。部分接口（页面启动序列、
      设备绑定）按 cookie 认会话，带上更贴近真实浏览器。

    ``Origin`` / ``Referer`` / ``User-Agent`` 也带上：上游对「看起来像浏览器」
    的请求更宽容，这是成本最低的一层识别伪装（与 trae 缺 UA 吃 4011 同类）。
    """
    from .login import endpoint

    base = endpoint()
    return {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
        "Origin": base,
        "Referer": f"{base}/",
        "Authorization": f"Bearer {access_token}",
        "xhx_token": access_token,
        "Cookie": f"xhx_token={access_token}",
    }


def build_chat_body(body: dict[str, Any], *, model: str, stream: bool) -> dict[str, Any]:
    """把客户端请求整理成上游 chat 体。

    只做**必要**的整形，不重写协议：上游本身就是 OpenAI 形态，多改一处就多
    一处「问 A 答 B」的风险。具体只做三件事：

    1. 强制 ``model`` 为目录里的真名（客户端可能带 ``raccoon/`` 前缀）；
    2. ``stream`` 按本次转发路径对齐（流式/非流式由调用方决定，不由客户端
       的 body 决定——两条路径的上游行为不同）；
    3. 流式时补 ``stream_options.include_usage``：上游默认不吐 usage，而
       积分/面板都要它。
    """
    out = dict(body)
    out["model"] = model
    out["stream"] = bool(stream)
    if stream:
        opts = dict(out.get("stream_options") or {})
        opts["include_usage"] = True
        out["stream_options"] = opts
    else:
        out.pop("stream_options", None)
    return out


def normalize_model_id(raw: str) -> str:
    """目录里的上游模型名 → 对外 id（统一带 ``sn-`` 前缀）。

    上游目录里已经是 ``sn-xxx`` 形状；少数条目没有前缀，补上以保证对外
    命名一致（``/v1/models`` 里不会一半带前缀一半不带）。
    """
    name = str(raw or "").strip()
    if not name:
        return ""
    return name if name.startswith(MODEL_PREFIX) else f"{MODEL_PREFIX}{name}"


def parse_catalog(payload: Any) -> list[dict[str, Any]]:
    """上游模型目录 → 本通道的模型条目列表。

    ## 真实结构（2026-11-23 实测，别再猜）

    ``/api/web/llm/v2/model_catalog`` 返回的是**按分类分组**的形状::

        {"code": 0, "data": {"categories": [
            {"type": "chat", "default_model": "raccoon-090dd3",
             "models": [
               {"name": "sn-sensenova-6-8-flash-lite",
                "model_name": "sn-sensenova-6-8-flash-lite",
                "billing_effective_multiplier": 0,     # 0 = 限免
                "visible": true,
                "tags": ["general", "vision", ...],
                "params": {"context_window": 1000000, "max_tokens": 100000},
                "description": "Raccoon-Work", "display_description": "..."},
               ...
             ]},
        ]}}

    早期我按 ``data`` 是 list 写解析器，一个模型都取不出来——于是
    ``models.json`` 里那几个**手猜的名字**（``sn-deepseek-v4-1-flash``）
    被当真发了出去，上游回 ``100004 param model unsupport``。真实名字带
    版本后缀（``sn-deepseek-v4-1-flash-1017``），猜不出来。

    所以现在：**只信目录**，解析器同时兼容 list / categories 两种形状；
    ``visible=false`` 的条目（内部代号 ``raccoon-*``）不进对外列表。

    每个条目保留调度与展示需要的字段：

    * ``id`` / ``upstream``：对外名与上游真名（当前两者相同）
    * ``multiplier``：积分倍率（``0`` = 限免，调度优先选）
    * ``context_window`` / ``max_output``：上下文与输出上限
    * ``vision`` / ``reasoning``：能力标记（从 ``tags`` 读）
    """
    items: list[Any] = []
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if isinstance(data, dict):
            cats = data.get("categories")
            if isinstance(cats, list):
                for cat in cats:
                    if isinstance(cat, dict) and isinstance(cat.get("models"), list):
                        items.extend(cat["models"])
            else:
                for key in ("models", "list", "catalog", "data"):
                    val = data.get(key)
                    if isinstance(val, list):
                        items = val
                        break
        elif isinstance(data, list):
            items = data
    elif isinstance(payload, list):
        items = payload
    if not isinstance(items, list):
        return []

    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        # 内部代号不对外：visible=false 的都是 raccoon-* 这类路由代号，
        # 用户选到它们只会拿到「unsupport」
        if it.get("visible") is False:
            continue
        raw_name = str(it.get("name") or it.get("model_name")
                       or it.get("model") or it.get("id") or "").strip()
        if not raw_name:
            continue
        mult = _first_number(it, ("billing_effective_multiplier", "multiplier",
                                  "billing_multiplier", "credit_multiplier", "rate"))
        if mult is None:
            mult = 1.0
        params = it.get("params") if isinstance(it.get("params"), dict) else {}
        tags = it.get("tags") if isinstance(it.get("tags"), list) else []
        tag_set = {str(t).lower() for t in tags}
        entry: dict[str, Any] = {
            "id": normalize_model_id(raw_name),
            "upstream": raw_name,
            "multiplier": float(mult),
            "free": float(mult) == FREE_MULTIPLIER,
            "description": str(it.get("display_description")
                               or it.get("description") or ""),
            "vision": "vision" in tag_set,
            "reasoning": "reasoning" in tag_set,
        }
        ctx = _first_number(params, ("context_window", "context_length", "max_context"))
        if ctx is None:
            ctx = _first_number(it, ("context_window", "context_length", "max_context"))
        if ctx:
            entry["context_window"] = int(ctx)
        max_out = _first_number(params, ("max_tokens", "max_output", "max_output_tokens"))
        if max_out is None:
            max_out = _first_number(it, ("max_output", "max_output_tokens"))
        if max_out:
            entry["max_output"] = int(max_out)
        out.append(entry)
    # 限免的排前面：调度按列表顺序取，省得每个调用点都再排一次
    out.sort(key=lambda e: (not e["free"], e["id"]))
    return out


def _first_number(obj: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """按候选键取第一个数值字段（跳过 bool：``True`` 不是倍率）。"""
    for key in keys:
        val = obj.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            try:
                return float(val)
            except ValueError:
                continue
    return None


def parse_balance(payload: Any) -> dict[str, Any]:
    """积分余额 → 面板用的结构。

    上游四个池（每日 / 奖励 / 月度 / 充值），其中**每日池**是唯一会作废的，
    调度要看它。返回::

        {
          "credits": 1234.5,          # 可用总积分
          "daily_points": 300.0,      # 每日池剩余（会过期的那部分）
          "daily_limit": 2100.0,      # 每日池上限（7 天 × 300）
          "expire_at": 1795...,       # 最近一次过期时刻（epoch 秒）
          "pools": [...],             # 原始分池明细（面板展示用）
        }

    拿不到余额时返回空 dict（而不是编 0）：0 会被读成「没额度了」，
    与「查不到」是两回事。
    """
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    out: dict[str, Any] = {}

    # 上游真实字段（2026-11-23 实测 /api/web/points/v1/balance）：
    #   available_points / daily_points / monthly_points / reward_points /
    #   topup_points / enterprise_topup_points / total_chargeable_points
    # 分池明细保留下来给面板展示——四个池的过期规则不同，合成一个数会误导。
    credits = _first_number(data, ("available_points", "credits", "total_credits",
                                   "balance", "available_credits", "points"))
    if credits is not None:
        out["credits"] = credits
    daily = _first_number(data, ("daily_points", "daily_credits", "daily_balance"))
    if daily is not None:
        out["daily_points"] = daily
    # 每日池上限：上游**不给**这个字段（只给当前剩余）。按官方规则「每天 300、
    # 每份 7 天有效」推上限 2100 —— 这是**规则推出来的**，不是瞎编的数，
    # 面板上会标注口径来源。
    limit = _first_number(data, ("daily_limit", "daily_max", "daily_total"))
    if limit is not None:
        out["daily_limit"] = limit
    for key, names in (
        ("monthly_points", ("monthly_points",)),
        ("reward_points", ("reward_points",)),
        ("topup_points", ("topup_points", "enterprise_topup_points")),
    ):
        val = _first_number(data, names)
        if val is not None:
            out[key] = val
    pools = data.get("pools") or data.get("windows")
    if isinstance(pools, list):
        out["pools"] = pools
    expire = data.get("expire_at") or data.get("next_expire_at")
    if isinstance(expire, (int, float)) and not isinstance(expire, bool):
        out["expire_at"] = float(expire)
    return out


def parse_bills(payload: Any) -> list[dict[str, Any]]:
    """积分发放流水 → 资源包列表（每笔独立到期）。

    这是**唯一能展示「我有几个资源包」的数据源**：``/points/v1/balance``
    只给各池的汇总剩余量，看不出包的数量、金额与到期时间。

    上游 ``/api/web/points/v2/bills?type=income`` 的条目形如::

        {
          "event_name": "每日积分发放",     # 人可读的事件名
          "biz_type": "daily_grant",        # 机器口径（daily_grant / reward_grant / …）
          "points": 300,                    # 这笔发了多少
          "created_at": "2026-11-23T01:24:58+08:00",
          "grant_at": "2026-11-23T00:00:00+08:00",
          "expire_at": "2026-11-29T23:59:59+08:00"   # 可能缺失（永不过期）
        }

    ``type=income`` 过滤掉消费流水——用 ``all`` 的话活跃账号的消费会把发放
    挤出条数窗口（参考实现实测过：60 条窗口里一条 grant 都看不到）。
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        points = _first_number(it, ("points", "amount", "value"))
        if points is None:
            continue
        out.append({
            "event": str(it.get("event_name") or it.get("biz_type") or "积分发放"),
            "biz_type": str(it.get("biz_type") or ""),
            "points": float(points),
            "grant_at": str(it.get("grant_at") or it.get("created_at") or ""),
            "expire_at": str(it.get("expire_at") or ""),
        })
    # 快到期的排前面（与「先烧将过期额度」的调度口径一致）
    out.sort(key=lambda e: (e["expire_at"] or "9999", e["grant_at"]))
    return out


def parse_setting_balance(payload: Any) -> dict[str, Any]:
    """从 ``setting_info`` 兜底读积分（``/points/v1/balance`` 回 403 时用）。

    这条兜底路径读不到 ``daily_points``——上游在那里不给分池信息。所以只
    返回 ``credits``，面板只显示总积分、不显示每日池（而不是编一个）。
    """
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    out: dict[str, Any] = {}
    credits = _first_number(data, ("credits", "points", "balance",
                                   "available_credits"))
    if credits is not None:
        out["credits"] = credits
    return out


def model_echo_matches(chunk: dict[str, Any], expected: str) -> bool:
    """流式首帧的 ``model`` 回显是否与请求一致（防「静默回落默认模型」）。

    上游对未知模型不报错，直接换默认模型回 200——这是最阴的一类失败：
    客户端以为拿到了 A 的答案，实际是 B 答的。所以首帧必须比对。

    回显缺失时返回 ``True``（不阻断）：不是所有版本都回显 model，缺失不代表
    换了模型；有回显且不一致才是硬证据。
    """
    if not isinstance(chunk, dict):
        return True
    got = str(chunk.get("model") or "").strip()
    if not got:
        return True
    exp = str(expected or "").strip()
    if not exp:
        return True
    # 上游可能回显带前缀/不带前缀两种形态，统一归一后比较
    return normalize_model_id(got).lower() == normalize_model_id(exp).lower()


def sse_lines(text: str) -> list[str]:
    """把一段 SSE 文本切成 data 行（调试/测试用）。"""
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            out.append(line[5:].strip())
    return out


def parse_sse_json(line: str) -> dict[str, Any] | None:
    """解析一行 SSE data；不是合法 JSON 对象返回 None。"""
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None
