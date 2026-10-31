"""豆包签到（豆包工作 App 内置 Chromium 的 CDP 通道）。

为什么走 CDP 而不是直连 HTTP
---------------------------
豆包的 ``/api/...`` 接口要求浏览器 cookie 登录态，而且请求里带着 ``a_bogus``
风控签名（由页面里的 ``window.bdms`` 脚本现算，签名算法的入参依赖当前页面
环境）。对话通道（``doubao/cdp_client.py``）之所以能在纯 stdlib 下跑通，就是
因为在种子页的 JS 环境里 ``fetch``，签名由页面自己注入——签到走同一条路：
先确保 CDP 会话就绪（复用登录态），再由本模块在页面里发 fetch。

接口发现状态（重要，别把「没验过」当「验过」）
--------------------------------------------
2026-10-31 在本机实测：

- 豆包工作 App 的 CDP 端口在线，种子页可连，但**该机器的豆包登录态已过期**
  （``/alice/user/config/pull`` 回 ``710012001 登录已过期``），页面侧接口
  用匿名身份访问全部落空（``/api/...`` 一律空体 401，``/alice/...`` 404），
  拿不到真实签到接口的响应形状。
- 因此下面的路径/字段来自**豆包 Web 端活动模块的结构**（``/biz/...``、
  ``/alice/...`` 走 ``www.doubao.com``，匿名可访问的端点如
  ``/alice/basic/launch`` 回 ``{"code":0,...}``），属于**候选口径**。

防护措施（保证「猜错」时界面是「状态未知」而不是假的「未签到」）：

1. ``_CANDIDATES`` 按顺序试；只认 HTTP 200 且 body 是 ``{"code": 0, ...}``
   的响应——豆包的业务错误（401 / 404 页面 / ``login invalid``）一律不算
   「查到了」。
2. 所有候选都没拿到可解析状态 → 返回 ``None``，provider 层转成
   ``query_failed`` 失败结构，界面显示「状态未知」，不会画出可点的「立即打卡」。
3. 账号确认后，把**实际生效的查询端点**写进日志（info），上游换接口时能
   从日志里直接看到该挪哪条。

登录态 / 接口一确认可用，把 ``_CANDIDATES`` 里命中那条挪到第一位并补上
字段注释即可，不需要动别处（``doubao/provider.py`` 的 ``checkin_status`` /
``checkin_claim`` 只调本模块）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..core.checkin import SOURCE_INFERRED, next_daily_reset

log = logging.getLogger(__name__)

#: 查询打卡状态：依次尝试的路径（相对种子页 origin）。
#:
#: 每一项形如 ``(path, 解析函数名)``；解析函数把上游 body 归一成状态 dict。
#: 目前三家的响应形状都按「活动中心」常见结构兜底（``data`` 里带
#: ``checked_in``/``signed``/``status``/签到记录列表之一），见 :func:`_parse_status`。
_CANDIDATES: tuple[str, ...] = (
    "/api/activity/check_in/status",
    "/api/activity/checkin/info",
    "/api/activity/task/list",
    "/api/activity/sign/info",
)

#: 领取：路径与候选查询一一对应（只对「查询命中」的那个活动前缀发领）。
_CLAIM_SUFFIX = {
    "/api/activity/check_in/status": "/api/activity/check_in",
    "/api/activity/checkin/info": "/api/activity/checkin/claim",
    "/api/activity/task/list": "/api/activity/task/claim",
    "/api/activity/sign/info": "/api/activity/sign/claim",
}

#: 页面里发请求的统一包装。返回 ``{"http": int|0, "body": str, "error": str}``。
#: ``fetch`` 用相对路径，自然带上 origin cookie；页面自己的 fetch 包装会注入
#: ``a_bogus`` 等签名头（与对话通道同一条路）。
_FETCH_JS = """
(async () => {{
  try {{
    const r = await fetch({path}, {{
      method: {method},
      credentials: "include",
      headers: {{"Content-Type": "application/json"}},
      {body}
    }});
    const t = await r.text();
    return {{http: r.status, body: t.slice(0, 20000), error: ""}};
  }} catch (e) {{
    return {{http: 0, body: "", error: String(e && e.message || e).slice(0, 300)}};
  }}
}})()
"""


def _js_path(path: str) -> str:
    """把路径安全地嵌进 JS 字符串字面量。"""
    return json.dumps(path)


def _eval(client: Any, expr: str, timeout: float) -> Any:
    """在 CDP 页面里求值；客户端未就绪 / 抛错返回 ``None``。"""
    try:
        return client.evaluate(expr, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 签到是展示功能，绝不让上游异常冒到界面
        log.debug("doubao checkin evaluate failed: %s", type(exc).__name__)
        return None


def _request(client: Any, path: str, *, method: str = "GET",
             body: dict[str, Any] | None = None, timeout: float = 20.0) -> tuple[int, Any]:
    """在页面里发一个请求，返回 ``(http_status, parsed_json_or_None)``。

    ``http_status`` 为 0 表示网络层失败（页面里抛异常 / 评估超时）。
    """
    expr = _FETCH_JS.format(
        path=_js_path(path),
        method=json.dumps(method),
        body=("body: " + json.dumps(json.dumps(body or {}))) if method != "GET" else "",
    )
    raw = _eval(client, expr, timeout)
    if not isinstance(raw, dict):
        return 0, None
    try:
        status = int(raw.get("http") or 0)
    except (TypeError, ValueError):
        status = 0
    text = raw.get("body") or ""
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = None
    return status, data


def _is_business_ok(status: int, data: Any) -> bool:
    """只认「200 + code 0」的成功响应；401/404/业务错码一律视为没查到。"""
    if status != 200 or not isinstance(data, dict):
        return False
    code = data.get("code")
    if code is None:
        # 有些端点不回 code（裸数据体）；至少要求是 dict 且有 data/result
        return isinstance(data.get("data", data.get("result")), (dict, list))
    try:
        return int(code) == 0
    except (TypeError, ValueError):
        return False


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _pick_date_list(*values: Any) -> list[str]:
    """从多个可能字段里挑出「YYYY-MM-DD 列表」，去重升序。"""
    for value in values:
        if not isinstance(value, (list, tuple)):
            continue
        days = sorted({
            str(v).strip()[:10]
            for v in value
            if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", v.strip())
        })
        if days:
            return days
    return []


def _parse_status(payload: Any) -> dict[str, Any] | None:
    """把活动接口的响应归一成状态 dict；形状不认识返回 ``None``。

    认得的形状（``data`` 或 ``result`` 之下）：
    - ``checked_in`` / ``signed_in`` / ``signed`` / ``is_signed``：布尔
    - ``status``：数字/字符串，「1」视为已签（活动中心常见 ``sign_status``）
    - ``sign_days`` / ``signed_days`` / ``records`` / ``sign_in_days``：日期或记录列表

    一个都认不出（比如只回了 ``{}`` / ``null``）就当**没查到**——不能把
    「看不懂的响应」猜成「今天没签」。
    """
    if not isinstance(payload, dict):
        return None
    node = payload.get("data")
    if not isinstance(node, dict):
        node = payload.get("result")
    if not isinstance(node, dict):
        # 活动列表类接口把活动数组直接放在 data 下，挑第一个带签到字段的
        arr = payload.get("data")
        node = None
        if isinstance(arr, list):
            node = next(
                (it for it in arr if isinstance(it, dict)
                 and any(k in it for k in ("checked_in", "signed_in", "signed", "is_signed"))),
                None,
            )
        if not isinstance(node, dict):
            return None

    checked: bool | None = None
    for key in ("checked_in", "signed_in", "signed", "is_signed", "today_signed"):
        if key in node:
            checked = _as_bool(node.get(key))
            if checked is not None:
                break
    if checked is None and "status" in node:
        raw_status = node.get("status")
        if isinstance(raw_status, (str, int)):
            text = str(raw_status).strip().lower()
            if text in ("1", "signed", "done", "checked_in", "claimed"):
                checked = True
            elif text in ("0", "unsigned", "todo", "not_signed", "unclaimed"):
                checked = False
    if checked is None:
        return None

    days = _pick_date_list(
        node.get("sign_days"), node.get("signed_days"),
        node.get("sign_in_days"), node.get("records"), node.get("checkin_dates"),
    )
    claimable_raw = node.get("claimable")
    claimable = _as_bool(claimable_raw) if claimable_raw is not None else (not checked)
    return {
        "checked_in": checked,
        "claimable": bool(claimable) and not checked,
        "total_times": len(days),
        "sign_in_days": days,
        "checkin_dates": days,
        "streak_days": _streak(days),
        "next_ts": next_daily_reset(),
        "next_ts_source": SOURCE_INFERRED,
    }


def _streak(days: list[str]) -> int:
    """签到日期列表 → 截至今天/昨天的连续天数（今天没签从昨天往前数）。"""
    from datetime import date, datetime, timedelta

    stamp: set[date] = set()
    for raw in days:
        try:
            stamp.add(datetime.strptime(raw[:10], "%Y-%m-%d").date())
        except ValueError:
            continue
    if not stamp:
        return 0
    cursor = date.today()
    if cursor not in stamp:
        cursor -= timedelta(days=1)
    streak = 0
    while cursor in stamp:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def fetch_checkin_status(client: Any, timeout: float = 20.0) -> tuple[dict[str, Any] | None, str]:
    """查询打卡状态；返回 ``(状态 dict 或 None, 命中的查询端点或 "")``。

    ``client`` 是已登录就绪的 ``CDPDoubaoClient``（由 provider 负责启动）。
    候选端点全都没拿到可解析状态时返回 ``(None, "")``——**不猜**「未签到」。
    业务成功但形状不认识的响应记一条 info（带响应前 300 字），上游换接口时
    从这里改 ``_parse_status`` 或 ``_CANDIDATES``。
    """
    for path in _ordered_candidates():
        status, data = _request(client, path, timeout=timeout)
        if not _is_business_ok(status, data):
            continue
        parsed = _parse_status(data)
        if parsed is None:
            log.info("doubao checkin: %s ok but unrecognised shape: %s",
                     path, json.dumps(data, ensure_ascii=False)[:300])
            continue
        log.info("doubao checkin status served by %s", path)
        _HIT_PATH["path"] = path
        return parsed, path
    return None, ""


#: 进程内记住上次命中的查询端点：命中过就别每轮再从第一条试起。
_HIT_PATH: dict[str, str] = {}


def _ordered_candidates() -> tuple[str, ...]:
    hit = _HIT_PATH.get("path")
    if hit and hit in _CANDIDATES:
        return (hit,) + tuple(p for p in _CANDIDATES if p != hit)
    return _CANDIDATES


def claim_checkin(client: Any, status_path: str, timeout: float = 20.0) -> dict[str, Any] | None:
    """领取今日打卡；成功后再查一次状态。失败 / 未确认返回 ``None``。

    领之前必须先用 :func:`fetch_checkin_status` 拿到**命中的** ``status_path``，
    只对它对应的领取端点发 POST——不对没验证过的端点盲打。
    """
    claim_path = _CLAIM_SUFFIX.get(status_path)
    if claim_path is None:
        return None
    status, data = _request(client, claim_path, method="POST", body={}, timeout=timeout)
    if not _is_business_ok(status, data):
        return None
    fresh, _ = fetch_checkin_status(client, timeout=timeout)
    if fresh is not None:
        return fresh
    return {
        "checked_in": True,
        "claimable": False,
        "next_ts": next_daily_reset(),
        "next_ts_source": SOURCE_INFERRED,
    }
