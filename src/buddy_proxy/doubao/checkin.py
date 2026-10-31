"""豆包签到（豆包工作 App 内置 Chromium 的 CDP 通道）。

为什么走 CDP 而不是直连 HTTP
---------------------------
豆包的 ``/api/...`` 接口要求浏览器 cookie 登录态，而且请求里带着 ``a_bogus``
风控签名（由页面里的 ``window.bdms`` 脚本现算，签名算法的入参依赖当前页面
环境）。对话通道（``doubao/cdp_client.py``）之所以能在纯 stdlib 下跑通，就是
因为在种子页的 JS 环境里 ``fetch``，签名由页面自己注入——签到走同一条路：
先确保 CDP 会话就绪（复用登录态），再由本模块在页面里发 fetch。

请求形状（2026-10-31 本机实测，别再踩这三个坑）
-----------------------------------------------
1. **必须用绝对 URL** ``https://www.doubao.com/...``。种子页是
   ``chrome://doubaowork-chat/chat``，相对路径的 origin 就是这个 chrome://
   地址，``fetch("/api/...")`` 落到 NotFound，``fetch("/")`` 还会返回 200 +
   HTML（任何「把相对路径当接口」的写法都会把 SPA 首页当成功响应）。
2. **必须带对话通道同款查询参数**（``aid``/``device_platform``/``web_id``…，
   见 ``CDPDoubaoClient._build_query_params``）。只带 cookie 不带参数，接口
   一律回空体 401。
3. 登录态以服务端为准：``/api/chat/completion/chat`` 空 POST 回 **401 = 未登录**，
   回业务错误码才是已登录。**不能**拿 ``localStorage.flow_web_has_login``
   当判据——它是前端缓存，服务端会话过期后它仍然是 ``true``（本机就卡在
   这里：面板一直「状态未知」，实际是 App 里根本没登录）。

接口发现策略（「不猜」的落地方式）
---------------------------------
签到接口路径没有公开文档，且 App 版本间会变。这里的做法是：

- :func:`discover_paths`：已登录的页面里抓取 ``/static/js/`` 下的 JS 资源，
  正则扫出 ``/api|/alice|/biz`` 开头的路径字面量，只为**记录**（info 日志），
  不据此发请求——扫出来的多半是别的模块的路径。
- ``_CANDIDATES``：真实可验证的探测端点，按顺序 GET；只有「200 + ``code==0``
  + 形状能认出签到状态」的才算命中。
- 全部落空 → 返回 ``None``，provider 层转 ``query_failed``，界面显示
  「状态未知」，绝不画出可点的「立即打卡」。
- 登录态无效 → 直接返回 ``None`` 并记 WARNING（「请在豆包 App 里重新扫码
  登录」），不再拿一堆 401 的候选端点去碰运气。

上游接口一确认，把命中那条挪到 ``_CANDIDATES`` 第一位并补字段注释即可；
``doubao/provider.py`` 的 ``checkin_status`` / ``checkin_claim`` 只调本模块。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..core.checkin import SOURCE_INFERRED, next_daily_reset

log = logging.getLogger(__name__)

#: 接口统一走 www.doubao.com（种子页 chrome:// 的 origin 不是这个域名）。
_ORIGIN = "https://www.doubao.com"

#: 查询打卡状态：依次尝试的路径（相对 :data:`_ORIGIN`，调用时补查询参数）。
#:
#: 认得的响应形状见 :func:`_parse_status`（``data``/``result`` 里带
#: ``checked_in``/``signed``/``status``/日期列表之一）。
_CANDIDATES: tuple[str, ...] = (
    # 2026-10-31 在豆包工作 App 2.32.14 的页面请求记录里实测到此端点
    # （``performance.getEntriesByType('resource')``），排第一位。
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

#: 登录态探测：空 POST 回 401 即「未登录」。已在对话通道上实测多时，可靠。
_LOGIN_PROBE = "/api/chat/completion/chat"

#: 接口线索扫描：只记日志，不据此发请求。各页面资源前缀。
_DISCOVERY_PREFIXES = ("/static/js/", "static/js/")
_PATH_RE = re.compile(r"[\"'](/(?:api|alice|biz)/[A-Za-z0-9_/\-]{2,80})[\"']")
_DISCOVERY_KEYWORDS = ("check", "sign", "activity", "credit", "reward", "task", "bonus")

#: 页面里发请求的统一包装。返回 ``{"http": int|0, "body": str, "error": str}``。
#: 绝对 URL + ``credentials: include``：cookie 随请求带出，``bdms`` 在页面
#: 环境里自动算签名头（与对话通道同一条路）。
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


def _query_string(client: Any) -> str:
    """对话通道同款查询参数；客户端没提供就退回最小集。"""
    builder = getattr(client, "_build_query_params", None)
    try:
        params = builder() if callable(builder) else {}
    except Exception:  # noqa: BLE001 - 探测不该因为取参数失败而炸
        params = {}
    if not isinstance(params, dict) or not params:
        params = {"aid": "1044603", "device_platform": "web"}
    return "&".join(f"{k}={v}" for k, v in sorted(params.items()))


def _url(client: Any, path: str) -> str:
    return f"{_ORIGIN}{path}?{_query_string(client)}"


def _signed_url(client: Any, path: str, signed: dict[str, str] | None) -> str:
    """默认查询参数之上叠加签名参数。"""
    if not signed:
        return _url(client, path)
    return f"{_url(client, path)}&" + "&".join(f"{k}={v}" for k, v in signed.items())


def signed_query(client: Any, timeout: float = 20.0) -> dict[str, str]:
    """从页面里已发出的活动请求中借一组签名参数（``msToken`` + ``a_bogus``）。

    为什么必须借：``/api/activity/*`` 的页面请求没带签名时一律空体 401；而
    ``a_bogus`` 是 ``bdms`` 对**具体 URL** 算的签名，本模块不自己造签名，
    只能拿页面请求过的（同域名、同会话）。``msToken`` 同一会话内各端点复用，
    ``a_bogus`` 与 URL 绑定——调用方拿到后**逐个端点按端点拼 URL** 去试
    （不传活动业务参数，只带签名），命中哪个算哪个。

    取不到（页面没发过活动请求 / 没有 ``a_bogus``）返回 ``{}``：调用方回落到
    「不带签名试一次」，失败也算没查到，不猜。
    """
    expr = r"""
    (() => {
      const base = %s + '/';
      let best = null;
      for (const e of performance.getEntriesByType('resource')) {
        if (!e.name.startsWith(base + 'api/activity/')) continue;
        const u = new URL(e.name);
        const ms = u.searchParams.get('msToken'), ab = u.searchParams.get('a_bogus');
        if (!ms || !ab) continue;
        best = {msToken: ms, a_bogus: ab};  // 越靠后越新，直接覆盖
      }
      return best;
    })()
    """ % json.dumps(_ORIGIN)
    raw = _eval(client, expr, timeout)
    if not isinstance(raw, dict):
        return {}
    out = {k: raw[k] for k in ("msToken", "a_bogus") if isinstance(raw.get(k), str) and raw[k]}
    return out if "msToken" in out and "a_bogus" in out else {}


def _eval(client: Any, expr: str, timeout: float) -> Any:
    """在 CDP 页面里求值；客户端未就绪 / 抛错返回 ``None``。"""
    try:
        return client.evaluate(expr, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 签到是展示功能，绝不让上游异常冒到界面
        log.debug("doubao checkin evaluate failed: %s", type(exc).__name__)
        return None


def _request(client: Any, path: str, *, method: str = "GET",
             body: dict[str, Any] | None = None, timeout: float = 20.0,
             signed: dict[str, str] | None = None) -> tuple[int, Any]:
    """在页面里发一个请求，返回 ``(http_status, parsed_json_or_None)``。

    ``http_status`` 为 0 表示网络层失败（页面里抛异常 / 评估超时）。
    ``signed`` 非空时叠加 :func:`signed_query` 借来的签名参数。
    """
    expr = _FETCH_JS.format(
        path=_js_path(_signed_url(client, path, signed)),
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


def login_state_from_probe(http: Any, data: Any) -> bool | None:
    """由登录探针的 HTTP 状态 + 响应体做三态判定（``None`` = 探针没下结论）。

    - ``401`` = 服务端明确说未登录 → ``False``；豆包自家业务码 ``710012001``
      （登录已过期）同样是 ``False``；
    - ``200`` 且非上述 → ``True``：Chat 路由到了鉴权层才回参数错误，说明会话可用；
    - HTTP 0（页面求值失败 / 网络没打通）、非 200 的 404/5xx（过期会话下 Chat
      路由常先回「Page not found」）、非 JSON → ``None``：**不改写成已登录，
      也不冒充未登录**，调用方按「状态未知」处理。
    """
    if not isinstance(http, int) or http == 0:
        return None
    if http == 401:
        return False
    code = data.get("code") if isinstance(data, dict) else None
    try:
        if code is not None and int(code) == 710012001:  # 豆包「登录已过期」
            return False
    except (TypeError, ValueError):
        pass
    if http != 200:
        return None
    return True


def login_state(client: Any, timeout: float = 15.0) -> bool | None:
    """服务端登录态三态：``True`` 已登录 / ``False`` 明确未登录 / ``None`` 没探到。

    打 ``/api/chat/completion/chat`` 空 POST，判定口径见
    :func:`login_state_from_probe`。探针没打通时不能折成「已登录」——那会把
    「探针都没打通」显示成「已登录但接口没认出」，用户就被指向错误的方向。
    """
    http, data = _request(client, _LOGIN_PROBE, method="POST", body={}, timeout=timeout)
    return login_state_from_probe(http, data)


def discover_paths(client: Any, timeout: float = 20.0) -> list[str]:
    """从页面已加载的 JS 资源里扫出签到/活动相关的接口路径线索（仅供日志）。"""
    expr = r"""
    (async () => {
      const pats = %s;
      const files = new Set();
      for (const s of document.scripts) {
        if (!s.src) continue;
        const u = new URL(s.src, location.href);
        const p = u.pathname + u.search;
        if (pats.some(x => p.startsWith(x))) files.add(u.href);
      }
      const out = new Set();
      const pat = /["'](\/(?:api|alice|biz)\/[A-Za-z0-9_\/-]{2,80})["']/g;
      // 并发拉取 + 每个文件只留前 2MB：串行 await 大文件会把页面卡住，
      // 顺带拖慢同一页上的对话通道。每个文件用独立的 regExp，避免共享
      // lastIndex 在并发时互相干扰。
      const scan = async (f) => {
        try {
          const r = await fetch(f);
          const t = (await r.text()).slice(0, 2000000);
          const re = new RegExp(pat.source, "g");
          let m;
          while ((m = re.exec(t))) out.add(m[1]);
        } catch (e) {}
      };
      await Promise.all(Array.from(files).slice(0, 40).map(scan));
      return Array.from(out);
    })()
    """ % json.dumps(list(_DISCOVERY_PREFIXES))
    raw = _eval(client, expr, timeout)
    if not isinstance(raw, list):
        return []
    hits = [p for p in raw if isinstance(p, str)]
    return [p for p in hits if any(k in p for k in _DISCOVERY_KEYWORDS)]


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

    ``client`` 是已就绪的 ``CDPDoubaoClient``（由 provider 负责启动）。
    先探服务端登录态：明确未登录 / 探针没打通都直接返回 ``(None, "")`` 并记
    WARNING（界面显示「状态未知」，明确 401 时提示重新登录）；只有确认已登录
    才按候选端点查，全落空同样不猜「未签到」。
    业务成功但形状不认识的响应记一条 info（带响应前 300 字），上游换接口时
    从这里改 ``_parse_status`` 或 ``_CANDIDATES``。
    """
    logged_in = login_state(client, timeout=timeout)
    if logged_in is not True:
        if logged_in is False:
            log.warning(
                "doubao checkin: 豆包工作 App 登录态无效（服务端 401），"
                "请在豆包 App 里重新扫码登录后再试")
        else:
            log.warning("doubao checkin: 豆包登录态探针未打通，本轮不查候选端点")
        return None, ""

    # 活动接口只认带签名的请求：先借页面请求过的签名参数，再回落到不带签名
    # 的裸请求（老版本接口可能不校验）。两条都没命中就是「没查到」，不猜。
    signed = signed_query(client, timeout=timeout)
    for extra in ((signed,) if signed else ()) + (None,):
        for path in _ordered_candidates():
            status, data = _request(client, path, timeout=timeout, signed=extra)
            if not _is_business_ok(status, data):
                continue
            parsed = _parse_status(data)
            if parsed is None:
                log.info("doubao checkin: %s ok but unrecognised shape: %s",
                         path, json.dumps(data, ensure_ascii=False)[:300])
                continue
            log.info("doubao checkin status served by %s%s", path,
                     " (signed)" if extra else " (unsigned)")
            _HIT_PATH["path"] = path
            return parsed, path

    # 候选全落空：扫一遍页面 JS 里的活动接口线索，给下一次上线留证据。
    hints = discover_paths(client, timeout=timeout)
    if hints:
        log.info("doubao checkin: no candidate matched; JS path hints: %s",
                 ", ".join(hints[:20]))
    else:
        log.info("doubao checkin: no candidate matched; no JS path hints found")
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
