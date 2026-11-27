"""RunningHub **网页端**会话：用账号密码登录拿 Bearer token，模拟站内「生成」按钮。

为什么要有这一层（2026-11-27 实测，别删）：

- RunningHub 有**两套账**：站内「过渡币 / RH 币 / 活动积分」只能在网页端花；
  OpenAPI（`/task/openapi/ai-app/run`）只认钱包余额 / 付费 API 额度。
- 同一个账号，apiType=SHARED 的 key 走 API 一律 605 NOT_ENOUGH_BALANCE；
  NORMAL（消费级）key 回 821「免费积分不接受」。两种 key 都花不了过渡币。
- 但网页端的「生成」按钮走 `POST /task/webapp/create`，带的是**登录态**
  （`Authorization: Bearer <access_token>`），它就能扣过渡币。

所以这里做的事和浏览器一模一样：`/uc/abroad/pwdLogin` 拿 JWT（30 天有效），
之后每个请求带 `client: WEB` + Bearer。**不是**调 OpenAPI，而是调站内接口。

登录态维护：
- token 落在账号记录的 ``web_session`` 里（0600 文件），不进日志。
- JWT 快过期或上游回 401/412 时用 refresh_token 续（``/uc/token/refresh``），
  续不动就用账号密码重登——调用方拿到的永远是「当下能用」的 token。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from .client import RunningHubError, _base_url

#: 登录/续期后提前多久判定「快过期」（秒）。JWT 实测 30 天，提前 1 天续。
REFRESH_MARGIN_SECONDS = 24 * 3600
#: 登录/续期失败后多久内不再重试（避免每次请求都去撞密码错误锁定）。
RELOGIN_COOLDOWN_SECONDS = 5 * 60

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


class WebSessionError(RunningHubError):
    """网页端会话异常（登录失败 / token 失效 / 上游业务错误）。"""

    def __init__(self, message: str, *, code: int | str | None = None, kind: str = "upstream"):
        super().__init__(message, status_code=502, code=code if isinstance(code, int) else None)
        self.raw_code = code
        self.kind = kind  # login | refresh | upstream | network


@dataclass
class WebSession:
    """一个账号的登录态。``token`` / ``refresh_token`` 都是 JWT。"""

    access_token: str
    refresh_token: str
    user_id: str = ""
    expires_at: float = 0.0  # unix 秒
    refreshed_at: float = 0.0
    #: 上次登录/续期失败的时间 + 原因，冷却期内不再重试
    last_error_at: float = 0.0
    last_error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_login(cls, data: dict[str, Any]) -> WebSession:
        tok = str(data.get("access_token") or "")
        if not tok:
            raise WebSessionError("登录成功但未返回 access_token", kind="login")
        claims = _jwt_claims(tok)
        return cls(
            access_token=tok,
            refresh_token=str(data.get("refresh_token") or ""),
            user_id=str(claims.get("sub") or ""),
            expires_at=float(claims.get("exp") or time.time() + 30 * 86400),
            refreshed_at=time.time(),
            extra={"identify": data.get("identify")},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "user_id": self.user_id,
            "expires_at": self.expires_at,
            "refreshed_at": self.refreshed_at,
            "last_error_at": self.last_error_at,
            "last_error": self.last_error,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WebSession | None:
        if not isinstance(d, dict) or not d.get("access_token"):
            return None
        return cls(
            access_token=str(d["access_token"]),
            refresh_token=str(d.get("refresh_token") or ""),
            user_id=str(d.get("user_id") or ""),
            expires_at=float(d.get("expires_at") or 0),
            refreshed_at=float(d.get("refreshed_at") or 0),
            last_error_at=float(d.get("last_error_at") or 0),
            last_error=str(d.get("last_error") or ""),
            extra=dict(d.get("extra") or {}),
        )

    def __repr__(self) -> str:  # 防止 f"{session}" / log.debug 把 JWT 带进日志
        return (f"WebSession(user_id={self.user_id!r}, expires_in={self.expires_at - time.time():.0f}s, "
                f"has_refresh={bool(self.refresh_token)}, cooldown={self.in_cooldown()})")

    def expiring(self, margin: float = REFRESH_MARGIN_SECONDS) -> bool:
        return self.expires_at > 0 and time.time() + margin >= self.expires_at

    def in_cooldown(self) -> bool:
        return (
            self.last_error_at > 0
            and time.time() - self.last_error_at < RELOGIN_COOLDOWN_SECONDS
        )


def _jwt_claims(token: str) -> dict[str, Any]:
    """解 JWT payload（不校验签名——我们只读 exp/sub 来决定要不要续期）。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _headers(token: str) -> dict[str, str]:
    # 这几个头是从 runninghub.ai 的 Nuxt bundle 里照抄的（axios 请求拦截器）：
    # client: WEB 标识站内调用；X-RH-Lang / User-Language 决定错误文案语言。
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "client": "WEB",
        "User-Agent": _UA,
        "Origin": _base_url(),
        "Referer": f"{_base_url()}/",
        "X-RH-Lang": "en",
        "User-Language": "en",
    }


def _client_id() -> str:
    """站内 clientId = md5("iframe0" + 毫秒时间戳)（见 bundle 里 clientIdGlobal 的生成）。"""
    return _md5(f"iframe0{int(time.time() * 1000)}{uuid.uuid4().hex[:6]}")


async def _post(
    path: str,
    body: dict[str, Any] | None = None,
    *,
    token: str | None = None,
    timeout: float = 30.0,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = _headers(token) if token else {
        "Content-Type": "application/json", "client": "WEB", "User-Agent": _UA,
        "Origin": _base_url(), "Referer": f"{_base_url()}/", "X-RH-Lang": "en", "User-Language": "en",
    }
    if extra_headers:
        headers.update(extra_headers)
    url = f"{_base_url()}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=body or {})
    if resp.status_code == 401:
        raise WebSessionError("登录态已失效 (HTTP 401)", code=401, kind="refresh")
    if resp.status_code >= 400:
        raise WebSessionError(f"网页端请求失败 {path} (HTTP {resp.status_code})", code=resp.status_code)
    try:
        data = resp.json()
    except Exception as exc:
        raise WebSessionError(f"网页端响应非 JSON: {path} ({exc.__class__.__name__})") from exc
    if not isinstance(data, dict):
        raise WebSessionError(f"网页端响应非对象: {path}")
    code = data.get("code")
    if code not in (0, None):
        # 站内约定：401/412 = token 失效；其余是业务错误，原文透传给用户
        if code in (401, 412):
            raise WebSessionError(f"登录态已失效 (code={code})", code=code, kind="refresh")
        raise WebSessionError(
            str(data.get("msg") or data.get("errorMessage") or f"未知错误 code={code}"),
            code=code,
            kind="upstream",
        )
    return data


# ---------------------------------------------------------------------------
# 登录 / 续期
# ---------------------------------------------------------------------------

async def login(email: str, password: str, *, timeout: float = 30.0) -> WebSession:
    """邮箱 + 密码登录（海外站 runninghub.ai），返回可用的会话。

    密码走 **MD5**（bundle 里 ``pV=e=>iO.MD5(e).toString()``），不是明文。
    失败几次后上游会要求验证码（code 1651/126）——这时只能让用户去网页登录，
    脚本无法过验证码；这里直接把上游文案抛出去。
    """
    email = email.strip()
    if not email or not password:
        raise WebSessionError("邮箱或密码为空", kind="login")
    try:
        data = await _post(
            "/uc/abroad/pwdLogin",
            {"email": email, "password": _md5(password)},
            timeout=timeout,
        )
    except WebSessionError as exc:
        exc.kind = "login"
        raise
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    return WebSession.from_login(inner)


async def refresh(session: WebSession, *, timeout: float = 30.0) -> WebSession:
    """用 refresh_token 续 access_token（站内 ``/uc/token/refresh``）。"""
    if not session.refresh_token:
        raise WebSessionError("没有 refresh_token，只能重新登录", kind="refresh")
    data = await _post(
        "/uc/token/refresh", {}, token=session.refresh_token, timeout=timeout
    )
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    new_tok = str(inner.get("access_token") or "")
    if not new_tok:
        raise WebSessionError("续期成功但未返回 access_token", kind="refresh")
    claims = _jwt_claims(new_tok)
    session.access_token = new_tok
    if inner.get("refresh_token"):
        session.refresh_token = str(inner["refresh_token"])
    session.user_id = session.user_id or str(claims.get("sub") or "")
    session.expires_at = float(claims.get("exp") or time.time() + 30 * 86400)
    session.refreshed_at = time.time()
    session.last_error_at = 0.0
    session.last_error = ""
    return session


async def ensure_session(
    session: WebSession | None,
    email: str,
    password: str,
    *,
    force: bool = False,
) -> WebSession:
    """返回一个「当下能用」的会话：没有就登录，快过期就续，续不动就重登。

    冷却：上次登录/续期失败后 ``RELOGIN_COOLDOWN_SECONDS`` 内不再重试（上游会
    锁密码），直接把上次错误抛出来。
    """
    if session and session.in_cooldown() and not force:
        raise WebSessionError(
            f"登录/续期刚失败过（{session.last_error}），{int(RELOGIN_COOLDOWN_SECONDS // 60)} 分钟内不重试",
            kind="login",
        )
    if session and not session.expiring() and session.access_token and not force:
        return session
    # session=None 时也要有地方记失败 → 调用方拿到的对象能用来冷却（否则每次都重新撞密码错）
    session = session or WebSession(access_token="", refresh_token="", expires_at=0.0)
    if session.refresh_token:
        try:
            return await refresh(session)
        except WebSessionError as exc:
            session.last_error_at = time.time()
            session.last_error = f"续期失败: {exc}"
    try:
        return await login(email, password)
    except WebSessionError as exc:
        session.last_error_at = time.time()
        session.last_error = f"登录失败: {exc}"
        raise


# ---------------------------------------------------------------------------
# 站内业务接口
# ---------------------------------------------------------------------------

async def get_user_info(token: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """``/uc/getUserInfo``：邮箱、昵称、apiKey、钱包、积分等。需要 ``userId`` 参数。"""
    claims = _jwt_claims(token)
    uid = str(claims.get("sub") or "")
    data = await _post("/uc/getUserInfo", {"userId": uid}, token=token, timeout=timeout)
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    inner["_userId"] = uid
    return inner


async def get_point_summary(token: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """``/uc/point/summary``：过渡币余额（``transitionCoinRemained``）、活动积分、最近过期时间。"""
    data = await _post("/uc/point/summary", {}, token=token, timeout=timeout)
    return data.get("data") if isinstance(data.get("data"), dict) else {}


async def get_coin_gain_list(token: str, *, timeout: float = 30.0) -> list[dict[str, Any]]:
    """``/api/product/coinGainList``：过渡币每一笔的获得时间 + **过期时间**。

    过渡币的有效期只有这里有——point/summary 的 ``nearestExpireTime`` 是**积分**的，
    不是币的（踩过：把它当成币的过期时间，报了「今天过期」其实是明年）。
    """
    data = await _post("/api/product/coinGainList", {"size": 10, "current": 1}, token=token, timeout=timeout)
    inner = data.get("data")
    recs = inner.get("records") if isinstance(inner, dict) else inner
    return [r for r in (recs or []) if isinstance(r, dict)]


async def claim_daily_gift(token: str, *, timeout: float = 30.0) -> bool | None:
    """``/api/welfare/daily-gift/claim``：领每日赠送的 30 积分（每天一次，站点时间 00:00 清零重置）。

    返回三态，调用方必须分开处理：
    - ``True``：这次真领到了
    - ``False``：上游说「今天已领过」（网页上领了 / 之前刷余额时顺手领了）——**也算今天完成**
    - ``None``：网络 / 上游错误，**不知道领没领**——自动领取循环要退避重试，不能当成「已领」

    2026-11-27 实测：新注册的号 ``activityPoint=0``、``dailyGift.granted=false``，直接
    跑应用会 414 NOT_ENOUGH_POWER_VALUE——不是没币，是**没领当天的积分**。网页端登录
    会自动触发（``loginCoinTriggered``），脚本登录不会，所以刷余额前先领一次。
    """
    try:
        data = await _post("/api/welfare/daily-gift/claim", {}, token=token, timeout=timeout)
        return bool(data.get("data"))
    except WebSessionError as exc:
        if exc.kind == "refresh":
            raise  # 401/412 让上层续期重试，别吞
        return None  # 其他（业务码非 0 / 网络）：交给调用方按「不知道领没领」处理


async def get_balance(
    token: str, *, timeout: float = 30.0, claim_gift: bool = True, include_welfare: bool = False
) -> dict[str, Any]:
    """把站内余额整理成固定字段，给 store / UI 用。

    - ``transitionCoins`` / ``transitionCoinExpireAt``：过渡币 + 最近一笔的过期时间（ISO，站内时间）
    - ``activityPoints`` / ``pointsExpireAt``：活动 / 每日赠送积分 + 过期时间（每日 00:00 站点时间清零）
    - ``paidPoints``：付费积分（不过期）
    - ``walletBalance`` / ``apiConsumableBalance``：钱包余额 / API 可用余额（USD）
    - ``freeImageLeft`` / ``freeVideoLeft``：站内福利的免费次数（另算，不扣币不扣分；只在
      ``include_welfare=True`` 时拉——submit 前不需要，省一个往返）

    ``claim_gift=True`` 时先尝试领当天赠送（已领过上游回 false，无副作用），再拉余额——
    顺序反了 UI 会显示 0 积分而运行时报 414。
    """
    if claim_gift:
        await claim_daily_gift(token, timeout=timeout)
    info, pts = await asyncio.gather(
        get_user_info(token, timeout=timeout), get_point_summary(token, timeout=timeout)
    )
    # 以下两个是「锦上添花」的字段，任何一个接口拉不到都不该让整个余额刷新失败
    try:
        coins = await get_coin_gain_list(token, timeout=timeout)
    except WebSessionError:
        coins = []
    welfare: dict[str, Any] = {}
    if include_welfare:
        try:
            w = await _post("/api/welfare/summary", {}, token=token, timeout=timeout)
            welfare = w.get("data") if isinstance(w.get("data"), dict) else {}
        except WebSessionError:
            welfare = {}
    # 时区自检：每日清零不是 16:00Z 说明站点换时区了，SITE_TZ_OFFSET_HOURS 要跟着改
    reset_hour = daily_gift_reset_hour_utc(pts)
    if reset_hour is not None and reset_hour != DAILY_GIFT_RESET_HOUR_UTC:
        logging.getLogger(__name__).warning(
            "runninghub 每日积分清零时间不是 %shZ 而是 %shZ，站点时区可能变了（SITE_TZ_OFFSET_HOURS=%s）",
            DAILY_GIFT_RESET_HOUR_UTC, reset_hour, SITE_TZ_OFFSET_HOURS,
        )
    wallet = info.get("walletInfo") if isinstance(info.get("walletInfo"), dict) else {}
    # 过渡币过期：取「还没过期」的记录里最早的一笔；拉失败 / 没记录就 None，不阻塞余额
    coin_expire = None
    try:
        alive = [c for c in coins if c.get("expiredTime") and not _is_past(c["expiredTime"])]
        coin_expire = min((c["expiredTime"] for c in alive), key=_parse_site_time) if alive else None
    except Exception:
        coin_expire = None
    free_img = welfare.get("freeImage") if isinstance(welfare.get("freeImage"), dict) else {}
    free_vid = welfare.get("freeVideo") if isinstance(welfare.get("freeVideo"), dict) else {}
    return {
        "transitionCoinExpireAt": coin_expire,
        "freeImageLeft": free_img.get("remaining"),
        "freeVideoLeft": free_vid.get("remaining"),
        "userId": info.get("_userId") or info.get("id"),
        "email": info.get("email"),
        "nickName": info.get("nickName"),
        "transitionCoins": _to_float(pts.get("transitionCoinRemained")),
        "activityPoints": _to_float(pts.get("activityPoint")),
        "paidPoints": _to_float(pts.get("paidPoint")),
        "balancePoints": _to_float(pts.get("balancePoint")),
        # 积分过期（point/summary 原字段）+ 一个好认的别名
        "nearestExpireTime": pts.get("nearestExpireTime"),
        "pointsExpireAt": pts.get("nearestExpireTime"),
        "walletBalance": _to_float(wallet.get("balance")),
        "apiConsumableBalance": _to_float(wallet.get("apiConsumableBalance")),
        "currency": wallet.get("currency"),
        # 刻意**不**带 apiKey / sharedApiKey：这个 dict 会整个写进 account_info 并回给前端。
        # 上传要 key 时走 get_api_keys() 现取（见 channels.ui_runninghub_upload）。
    }


async def get_api_keys(token: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """``/uc/apiKey/get``：normalApiKey / sharedApi（企业共享 key，回的是脱敏串）。"""
    data = await _post("/uc/apiKey/get", {}, token=token, timeout=timeout)
    return data.get("data") if isinstance(data.get("data"), dict) else {}


async def webapp_detail(webapp_id: str, token: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """``/api/webapp/detail``：站内表单 schema（``inputNodes``），比 apiCallDemo 的 nodeInfoList 多 fieldType。"""
    data = await _post("/api/webapp/detail", {"webappId": str(webapp_id)}, token=token, timeout=timeout)
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    return {
        "webappId": str(webapp_id),
        "name": inner.get("name") or f"ai-app-{webapp_id}",
        "description": inner.get("description") or "",
        "nodeInfoList": list(inner.get("inputNodes") or []),
        "instanceType": inner.get("instanceType"),
        "avgRunningSeconds": inner.get("avgRunningSeconds"),
        "runningSuccessRate": inner.get("runningSuccessRate"),
    }


async def price_estimate(
    webapp_id: str,
    inputs: list[dict[str, Any]],
    token: str,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """``/api/workflow/webapp-price-estimate``：**免费**，不扣任何东西。

    返回 ``totalPrice`` / ``currency`` / ``estimatedPoint`` / ``estimatedTransitionCoin``——
    这一步能直接看出站内会扣币还是扣钱。网页端带 ``userId`` 请求头，我们也带。
    """
    claims = _jwt_claims(token)
    data = await _post(
        "/api/workflow/webapp-price-estimate",
        {"webappId": str(webapp_id), "inputs": inputs},
        token=token,
        timeout=timeout,
        extra_headers={"userId": str(claims.get("sub") or "")},
    )
    return data.get("data") if isinstance(data.get("data"), dict) else {}


async def create_task(
    webapp_id: str,
    inputs: list[dict[str, Any]],
    token: str,
    *,
    instance_type: str | None = None,
    use_personal_queue: bool = False,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """``/task/webapp/create``：网页「生成」按钮打的那一发。返回 ``{taskId, taskStatus, promptTips}``。"""
    body: dict[str, Any] = {
        "webappId": str(webapp_id),
        "inputs": inputs,
        "clientId": _client_id(),
        "usePersonalQueue": bool(use_personal_queue),
    }
    if instance_type:
        body["instanceType"] = instance_type
    data = await _post("/task/webapp/create", body, token=token, timeout=timeout)
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    task_id = str(inner.get("taskId") or "")
    if not task_id:
        raise WebSessionError(f"站内提交未返回 taskId: {inner.get('promptTips') or data.get('msg')}")
    return {"taskId": task_id, "status": inner.get("taskStatus") or "RUNNING", "raw": inner}


async def task_list(
    token: str,
    *,
    webapp_id: str | None = None,
    task_id: str | None = None,
    size: int = 20,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """``/task/list``：站内任务列表，用来轮询状态（``taskStatus``）。"""
    body: dict[str, Any] = {"size": size, "current": 1, "taskType": ["WEBAPP"]}
    if webapp_id:
        body["webappId"] = str(webapp_id)
    data = await _post("/task/list", body, token=token, timeout=timeout)
    inner = data.get("data")
    recs = inner.get("records") if isinstance(inner, dict) else inner
    recs = recs if isinstance(recs, list) else []
    if task_id:
        recs = [r for r in recs if str(r.get("taskId")) == str(task_id)]
    return recs


async def output_history(
    token: str,
    *,
    webapp_id: str,
    task_id: str,
    size: int = 20,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """``/api/output/v2/history``：按 taskId 取产出文件（``fileUrl`` / ``outputType``）。"""
    data = await _post(
        "/api/output/v2/history",
        {"size": size, "current": 1, "taskType": ["WEBAPP"], "webappId": str(webapp_id)},
        token=token,
        timeout=timeout,
    )
    inner = data.get("data")
    recs = inner.get("records") if isinstance(inner, dict) else inner
    recs = recs if isinstance(recs, list) else []
    return [r for r in recs if str(r.get("taskId")) == str(task_id)]


def normalize_inputs(node_info_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """apiCallDemo 的 nodeInfoList 和站内 ``inputs`` 字段一样，但站内只要这三项。

    ``/task/webapp/create`` 报 803 APIKEY_INVALID_NODE_INFO 时，多半是多了字段——
    站内表单提交的就是精简版（见 bundle ``Tr()``）。
    """
    out: list[dict[str, Any]] = []
    for n in node_info_list or []:
        if not isinstance(n, dict):
            continue
        item = {
            "nodeId": str(n.get("nodeId")),
            "fieldName": str(n.get("fieldName")),
            "fieldValue": n.get("fieldValue") if n.get("fieldValue") is not None else "",
        }
        if n.get("nodeName"):
            item["nodeName"] = n["nodeName"]
        if n.get("description"):
            item["description"] = n["description"]
        out.append(item)
    return out


#: 站内「无时区」时间（``YYYY-MM-DD HH:MM:SS``）的时区。**反推**出来的，不是文档写的：
#: point/summary 的 ``nearestExpireTime`` 是 ``…T16:00:00Z``，而每日赠送在站点时间 00:00 清零
#: → 16:00Z 就是站点 00:00 → UTC+8。前端 ``parseSiteTime`` 里同一个假设（``+m[4] - 8``），要改一起改。
#: ``daily_gift_reset_hour_utc`` 用来在运行时自检：拿到的清零时间不是 16:00Z 就说明站点换时区了。
SITE_TZ_OFFSET_HOURS = 8
DAILY_GIFT_RESET_HOUR_UTC = 16


def _parse_site_time(s: str) -> float:
    """站内时间 → unix 秒。无时区的 ``YYYY-MM-DD HH:MM:SS`` 按 ``SITE_TZ_OFFSET_HOURS`` 算。"""
    from datetime import datetime, timezone, timedelta
    s = str(s or "").strip()
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone(timedelta(hours=SITE_TZ_OFFSET_HOURS))
        ).timestamp()
    except (ValueError, TypeError):
        return 0.0


def daily_gift_reset_hour_utc(pts: dict[str, Any]) -> int | None:
    """从 point/summary 的 nearestExpireTime 看每日清零是几点（UTC）。≠16 说明站点时区变了。"""
    t = _parse_site_time(pts.get("nearestExpireTime") or "")
    if t <= 0:
        return None
    from datetime import timezone
    return int(datetime_from_unix(t).astimezone(timezone.utc).hour)


def datetime_from_unix(t: float):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, tz=timezone.utc)


def _is_past(s: str) -> bool:
    t = _parse_site_time(s)
    return t > 0 and t < time.time()


def _to_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
