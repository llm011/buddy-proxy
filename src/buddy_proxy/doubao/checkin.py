"""豆包每日签到：在独立的签到页面 session 中调用 campaign v3 接口。

2026-11-01 在 DoubaoWork 2.34.12 的签到 webview 实测：每日签到属于
``pc_incentive_task_mvp_v3``，任务 ``daily_signin``，不是旧实现猜测的
``/api/activity/*``。查询和领取都是 POST，body 必须带活动标识；只带 cookie
或沿用聊天接口的 aid 会得到参数错误。详情接口不需要 ``msToken`` /
``a_bogus``（同一请求去掉这两个参数仍返回 ``200 + code: 0``）。

签到页是 ``https://www.doubao.com/pc/agent-explore`` 的独立 target。这里复用
用户已经打开的签到页，或用 ``Target.createTarget`` 新建后台页；无论哪种都
不切换共享聊天 target，也不关闭用户页面。新建页只在本模块创建的范围内关闭。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from ..core.checkin import SOURCE_INFERRED, next_daily_reset
from .ws import _WS

log = logging.getLogger(__name__)

_ORIGIN = "https://www.doubao.com"
_PAGE_PATH = "/pc/agent-explore"
_PAGE_URL = _ORIGIN + _PAGE_PATH + "?scroll_to=tasks"
_DETAIL_PATH = "/alice/ug/campaign/v3/detail/"
_CLAIM_PATH = "/alice/ug/campaign/landing/checkin"
_LOGIN_PROBE = "/chat/completion"
_CAMPAIGN_GROUP = "pc_incentive_task_mvp_v3"
_TASK = "daily_signin"

#: 手动领取与后台刷新在不同线程运行；复用同一 target 时，不能让两个 CDP
#: session 的页面加载 / 请求 / 关闭交叉。RLock 允许领取入口先查状态再领取。
_PAGE_LOCK = threading.RLock()

#: 页面还没有留下 detail 资源时的已验证参数集。不能用聊天通道的 aid=1044603。
_PAGE_QUERY = (
    "?version_code=20800&language=zh&device_platform=web"
    "&doubao_device_platform=web&aid=497858&real_aid=497858"
    "&pkg_type=release_version&pc_version=2.34.12&region=CN&sys_region=CN"
    "&samantha_web=1&web_platform=work_desktop&use-olympus-account=1&runtime=web"
)

_DETAIL_QUERY_JS = """
(() => {
  const entries = performance.getEntriesByType('resource').filter(
    e => e.name.includes('/alice/ug/campaign/v3/detail/'));
  if (!entries.length) return '';
  const url = new URL(entries[entries.length - 1].name);
  url.searchParams.delete('msToken');
  url.searchParams.delete('a_bogus');
  return url.search;
})()
"""

_FETCH_JS = """
(async () => {
  try {
    const r = await fetch(%s, {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/json'}, body: %s,
    });
    const text = await r.text();
    let data;
    try { data = JSON.parse(text); %s } catch (e) {}
    return {http: r.status, body: data === undefined
      ? text.slice(0, 2000) : JSON.stringify(data).slice(0, 20000)};
  } catch (e) {
    return {http: 0, body: '', error: String(e && e.message || e).slice(0, 300)};
  }
})()
"""


class CheckinPage:
    """一个与聊天 WebSocket 隔离的签到页求值上下文（同步，线程内使用）。"""

    def __init__(self, client: Any, *, reuse_existing: bool = True):
        self._client = client
        self._reuse_existing = reuse_existing
        self._ws: _WS | None = None
        self._target_id = ""
        self._created = False
        self.query = _PAGE_QUERY

    def __enter__(self) -> CheckinPage:
        _PAGE_LOCK.acquire()
        try:
            self._open()
        except Exception:
            # __enter__ 失败时 Python 不会补调 __exit__，临时页必须在这里收掉。
            self.__exit__()
            raise
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._created:
            # 只收自己创建的标签；绝不动用户已经打开的签到 webview。
            try:
                self._shared_cmd("Target.closeTarget", {"targetId": self._target_id})
            except Exception as exc:  # noqa: BLE001 - 清理失败不能覆盖查询结果
                log.debug("doubao checkin: close temporary page failed: %s", type(exc).__name__)
        if self._ws is not None:
            self._ws.close()
            self._ws = None
        _PAGE_LOCK.release()

    def _shared_cmd(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        ws = self._client._ws
        if ws is None:
            raise RuntimeError("CDP not connected")
        with self._client._ws_lock:
            return ws.cmd(method, params)

    def _connect(self, target: dict[str, Any]) -> None:
        ws_url = target.get("webSocketDebuggerUrl", "")
        marker = f":{self._client.port}"
        if not self._client.is_ready or marker not in ws_url:
            raise RuntimeError("checkin page has no usable CDP websocket")
        self._ws = _WS("127.0.0.1", self._client.port, ws_url.split(marker, 1)[1])
        self._target_id = str(target.get("id") or "")

    def _open(self) -> None:
        targets = self._client._fetch_targets()
        target = None
        if self._reuse_existing:
            target = next((t for t in targets if _is_checkin_target(t)), None)
        if target is None:
            result = self._shared_cmd(
                "Target.createTarget", {"url": _PAGE_URL, "background": True})
            target_id = (result.get("result") or {}).get("targetId")
            if not target_id:
                raise RuntimeError("could not create isolated checkin page")
            self._created = True
            self._target_id = target_id
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                target = next((t for t in self._client._fetch_targets()
                               if t.get("id") == target_id and t.get("webSocketDebuggerUrl")), None)
                if target is not None:
                    break
                time.sleep(0.2)
        if target is None:
            raise RuntimeError("new checkin page did not appear")
        self._connect(target)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            href = self.evaluate("location.href")
            ready = self.evaluate("document.readyState")
            if isinstance(href, str) and _is_checkin_url(href) and ready == "complete":
                query = self.evaluate(_DETAIL_QUERY_JS)
                if isinstance(query, str) and query.startswith("?"):
                    self.query = query
                return
            time.sleep(0.3)
        raise RuntimeError("checkin page did not finish loading")

    def evaluate(self, expr: str, timeout: float = 20.0) -> Any:
        if self._ws is None:
            raise RuntimeError("checkin page not connected")
        response = self._ws.cmd("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
        }, timeout=timeout)
        result = response.get("result", {})
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            description = (details.get("exception", {}).get("description")
                           or details.get("text"))
            return {"ERROR": str(description)[:500]}
        return result.get("result", {}).get("value")

    def request(self, path: str, body: dict[str, Any], timeout: float,
                *, daily_task_only: bool = False) -> tuple[int, Any, str]:
        # detail 的 example_cases 远超 20KB；只在页面内取签到任务，不能截断整份 JSON。
        select = (
            "const task = data.data && Array.isArray(data.data.tasks) "
            "? data.data.tasks.find(t => t.task_kind === 'daily_signin' "
            "&& t.campaign_code === 'daily_signin') : null; "
            "data = {code: data.code, data: {campaign_code: data.data "
            "&& data.data.campaign_code, tasks: task ? [task] : []}};"
        ) if daily_task_only else ""
        expr = _FETCH_JS % (
            json.dumps(_ORIGIN + path + self.query),
            json.dumps(json.dumps(body, separators=(",", ":"))),
            select,
        )
        raw = self.evaluate(expr, timeout=timeout)
        if not isinstance(raw, dict):
            return 0, None, ""
        try:
            status = int(raw.get("http") or 0)
        except (TypeError, ValueError):
            status = 0
        text = raw.get("body") or ""
        try:
            data = json.loads(text) if isinstance(text, str) and text else None
        except ValueError:
            data = None
        return status, data, text if isinstance(text, str) else ""


def _is_checkin_url(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.netloc == "www.doubao.com" \
        and parsed.path.rstrip("/") == _PAGE_PATH


def _is_checkin_target(target: Any) -> bool:
    return (isinstance(target, dict) and target.get("type") in ("page", "webview")
            and bool(target.get("webSocketDebuggerUrl")) and _is_checkin_url(target.get("url")))


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _is_business_ok(status: int, data: Any) -> bool:
    if status != 200 or not isinstance(data, dict):
        return False
    try:
        return int(data.get("code")) == 0
    except (TypeError, ValueError):
        return False


def _is_html(body: Any) -> bool:
    if not isinstance(body, str):
        return False
    head = body.lstrip()[:15].lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def login_state_from_probe(http: Any, data: Any, body: Any = None) -> bool | None:
    """聊天探针三态：401/明确过期码为未登录；200 + API JSON/SSE 为已登录。

    200 + HTML 只是 SPA 页面，不能证明登录；0、404、5xx、非 JSON 都是未知。
    """
    if not isinstance(http, int) or http == 0:
        return None
    if http == 401:
        return False
    if http == 200 and body is not None and data is None and _is_html(body):
        return None
    payloads = [data] if isinstance(data, dict) else []
    if isinstance(body, str) and not _is_html(body):
        # /chat/completion 是 SSE：200 的首包 heartbeat 后才是业务 error_code。
        candidates = [body, *(line[5:].strip() for line in body.splitlines()
                             if line.startswith("data:"))]
        for text in candidates:
            try:
                item = json.loads(text)
            except ValueError:
                continue
            if isinstance(item, dict):
                payloads.append(item)
    for item in payloads:
        code = item.get("code", item.get("error_code"))
        try:
            if code is not None and int(code) == 710012001:
                return False
        except (TypeError, ValueError):
            pass
    if http != 200 or not payloads:
        return None
    return True


def login_state(client: Any, timeout: float = 15.0) -> bool | None:
    """在豆包同源页面里探真实聊天路由，不使用 ``/api/chat/completion/chat``。"""
    try:
        with CheckinPage(client) as page:
            status, data, body = page.request(_LOGIN_PROBE, {}, timeout)
    except Exception as exc:  # noqa: BLE001 - 三态探针失败就是未知，不能冒到面板
        log.debug("doubao checkin login probe failed: %s", type(exc).__name__)
        return None
    return login_state_from_probe(status, data, body)


def _daily_task(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("campaign_code") != _CAMPAIGN_GROUP:
        return None
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return None
    matches = [task for task in tasks if isinstance(task, dict)
               and task.get("task_kind") == _TASK and task.get("campaign_code") == _TASK]
    if len(matches) != 1:
        return None
    return matches[0]


def _parse_status(payload: Any) -> dict[str, Any] | None:
    """只解析实测的 ``daily_signin.daily_checkin``，不从任务状态数字猜签到。"""
    task = _daily_task(payload)
    if task is None or task.get("campaign_group_code") != _CAMPAIGN_GROUP:
        return None
    daily = task.get("daily_checkin")
    if not isinstance(daily, dict):
        return None
    checked = _as_bool(daily.get("checked_in_today"))
    if checked is None:
        return None
    # 只在明确「今天未签 + 待领取」时开放 claim；未认识的 reward_status 不猜。
    # 页面代码：daily 未完成即可签到；reward_status 的其它数字不作猜测。
    claimable = not checked and task.get("task_status") in (1, 2, 4)
    days = [daily["last_checkin_date"]] if isinstance(daily.get("last_checkin_date"), str) else []
    reward = daily.get("reward")
    amount = reward.get("reward_amount") if isinstance(reward, dict) else None
    status = {
        "checked_in": checked,
        "claimable": claimable,
        "total_times": daily.get("total_days")
        if isinstance(daily.get("total_days"), int)
        and not isinstance(daily.get("total_days"), bool) else 0,
        "streak_days": daily.get("continuous_days")
        if isinstance(daily.get("continuous_days"), int)
        and not isinstance(daily.get("continuous_days"), bool) else 0,
        "sign_in_days": days,
        "checkin_dates": days,
        "next_ts": next_daily_reset(),
        "next_ts_source": SOURCE_INFERRED,
    }
    if isinstance(amount, (int, float)) and not isinstance(amount, bool):
        status["daily_credit"] = amount
    return status


def _record_login_state(client: Any, state: bool | None) -> None:
    """同进程 provider 的面板提示可复用刚测得的登录态，不另发聊天页探针。"""
    try:
        client._login_state = state
    except Exception:  # noqa: BLE001 - 状态旁路失败不能影响真实签到查询
        pass


def fetch_checkin_status(client: Any, timeout: float = 20.0) -> tuple[dict[str, Any] | None, str]:
    """返回 ``(状态, detail 路径)``；失败保持 ``(None, "")``，不伪造未签到。"""
    try:
        with CheckinPage(client) as page:
            logged_in = login_state_from_probe(*page.request(_LOGIN_PROBE, {}, timeout))
            _record_login_state(client, logged_in)
            if logged_in is not True:
                if logged_in is False:
                    log.warning("doubao checkin: 豆包登录态无效，请在 App 里重新登录")
                else:
                    log.warning("doubao checkin: 登录探针未下结论，本轮不查签到")
                return None, ""
            status, data, body = page.request(
                _DETAIL_PATH, {"campaign_group_code": _CAMPAIGN_GROUP}, timeout,
                daily_task_only=True)
    except Exception as exc:  # noqa: BLE001 - 签到不可影响聊天和网关
        log.warning("doubao checkin: campaign page failed: %s", type(exc).__name__)
        return None, ""
    if not _is_business_ok(status, data):
        log.info("doubao checkin: campaign detail failed: http=%s body=%s", status, body[:300])
        return None, ""
    parsed = _parse_status(data)
    if parsed is None:
        log.info("doubao checkin: campaign detail has no recognisable daily_signin")
        return None, ""
    return parsed, _DETAIL_PATH


def claim_checkin(client: Any, status_path: str, timeout: float = 20.0) -> dict[str, Any] | None:
    """只领 ``daily_signin``，并以领取后的 detail 已签状态作为成功凭据。"""
    if status_path != _DETAIL_PATH:
        return None
    try:
        with CheckinPage(client) as page:
            status, data, _ = page.request(
                _DETAIL_PATH, {"campaign_group_code": _CAMPAIGN_GROUP}, timeout,
                daily_task_only=True)
            current = _parse_status(data) if _is_business_ok(status, data) else None
            if current is None:
                return None
            if current["checked_in"]:
                return current
            if not current["claimable"]:
                return None
            status, data, body = page.request(_CLAIM_PATH, {"scene": _CAMPAIGN_GROUP}, timeout)
            if not _is_business_ok(status, data):
                log.info("doubao checkin: claim failed: http=%s body=%s", status, body[:300])
                return None
            result = data.get("data")
            if not isinstance(result, dict) or result.get("checked_in") is not True:
                return None
            status, data, _ = page.request(
                _DETAIL_PATH, {"campaign_group_code": _CAMPAIGN_GROUP}, timeout,
                daily_task_only=True)
            fresh = _parse_status(data) if _is_business_ok(status, data) else None
            if fresh is not None and fresh["checked_in"] and not fresh["claimable"]:
                return fresh
            log.warning("doubao checkin: claim accepted but recheck unconfirmed")
    except Exception as exc:  # noqa: BLE001 - 领取未确认，交给上层报错
        log.warning("doubao checkin: claim request failed: %s", type(exc).__name__)
    return None
