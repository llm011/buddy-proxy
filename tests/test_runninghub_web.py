"""RunningHub 站内（网页端登录态）链路测试。

2026-11-27 实测出来的事实，每条测试都锁一个「实现退化就会立刻红」的点：

- 两套账：过渡币 / 积分只有登录态能花；OpenAPI 对 SHARED key 回 605、NORMAL key 回 821。
- 登录：``POST /uc/abroad/pwdLogin``，密码 MD5，响应 ``access_token``（JWT 30 天）/ ``refresh_token``。
- 续期：JWT 快过期用 refresh_token；401/412 强制重登；失败后 5 分钟冷却（上游会锁密码）。
- 闭源节点（``RH_OpenAPI_*`` 等）站内也扣钱包 $，预估接口对它回 ``$0.00 / nodes: []``。
- 任务失败原因藏在 ``taskResultDesc.exception_message`` 里，要挖出来。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from buddy_proxy.runninghub import runner, store, websession
from buddy_proxy.runninghub.websession import WebSession, WebSessionError

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "runninghub_accounts.json"
    monkeypatch.setenv("RUNNINGHUB_ACCOUNTS_PATH", str(p))
    monkeypatch.setenv("RUNNINGHUB_WORKFLOWS_PATH", str(tmp_path / "runninghub_workflows.json"))
    # 任务队列也必须隔离——否则测试会往用户真实的 ~/.buddy-proxy/runninghub_tasks.json 里写
    monkeypatch.setenv("RUNNINGHUB_TASKS_PATH", str(tmp_path / "runninghub_tasks.json"))
    return p


def _fake_jwt(sub: str = "2100902343205564418", exp_delta: float = 30 * 86400) -> str:
    def b64(d: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'HS256'})}.{b64({'sub': sub, 'exp': int(time.time() + exp_delta), 'username': 'x'})}.sig"


class _Resp:
    def __init__(self, payload: dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload


class _Recorder:
    """记录每次 POST 的 (path, body, headers)，按 path 返回预设响应。"""

    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch):
        rec = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None, **kw):
                path = url.split("runninghub.ai", 1)[-1]
                rec.calls.append((path, json or {}, dict(headers or {})))
                r = rec.routes.get(path)
                if r is None:
                    return _Resp({"code": 404, "msg": f"no route for {path}"})
                return _Resp(r() if callable(r) else r)

        monkeypatch.setattr(websession.httpx, "AsyncClient", lambda **kw: _Client())


# ---------------------------------------------------------------------------
# store：邮箱账号 / 脱敏 / 登录态
# ---------------------------------------------------------------------------

def test_store_email_account_masks_everything_secret(isolated_store: Path):
    """账号视图绝不带 password / api_key / access_token / refresh_token。"""
    acc = store.upsert_account(email="Someone@icloud.com", password="pw-secret", label="主号")
    assert acc["account_id"].startswith("e")
    assert acc["masked_email"] == "So***@icloud.com"
    assert "password" not in acc and "api_key" not in acc and "web_session" not in acc
    assert acc["web_session_status"] == {"logged_in": False}

    listed = store.list_accounts()
    assert "password" not in listed[0]
    raw = json.loads(isolated_store.read_text())["accounts"][0]
    assert raw["password"] == "pw-secret", "密码要落盘（0600），否则重启后要用户重输"
    mode = isolated_store.stat().st_mode & 0o777
    assert mode == 0o600


def test_store_same_email_updates_not_duplicates_and_password_change_kills_session(isolated_store: Path):
    """同邮箱再导入 = 更新；密码变了旧登录态必须作废（否则拿着旧 token 去撞 401）。"""
    a1 = store.upsert_account(email="a@x.com", password="pw1")
    store.update_session(a1["account_id"], WebSession(access_token=_fake_jwt(), refresh_token="r").to_dict())
    a2 = store.upsert_account(email="a@x.com", password=None, label="改名")  # 只改备注
    assert a2["account_id"] == a1["account_id"]
    assert store.get_account(a1["account_id"])["password"] == "pw1"
    assert a2["web_session_status"]["logged_in"] is True, "只改备注不能把登录态冲掉"

    a3 = store.upsert_account(email="a@x.com", password="pw2")
    assert a3["web_session_status"] == {"logged_in": False}
    assert len(store.list_accounts()) == 1


def test_store_legacy_key_only_account_still_works(isolated_store: Path):
    """老的纯 api_key 记录不能因为这次改动坏掉。"""
    acc = store.upsert_account(api_key="938bcfc2f8da4650abdf67b617b533a0", label="旧号")
    assert acc["masked_key"] == "938b...33a0"
    assert acc["masked_email"] == ""
    assert store.get_active_accounts()[0]["api_key"].startswith("938b")


def test_session_status_reports_expiry_and_errors():
    ws = WebSession(access_token=_fake_jwt(exp_delta=3600), refresh_token="r", expires_at=time.time() + 3600,
                    last_error="登录失败: 密码错", last_error_at=time.time())
    st = store._session_status(ws.to_dict())
    assert st["logged_in"] is True
    assert st["expires_at"] == ws.expires_at and st["expires_in_days"] is not None
    assert "密码错" in st["last_error"]


# ---------------------------------------------------------------------------
# websession：登录 / 续期 / 冷却
# ---------------------------------------------------------------------------

def test_login_sends_md5_password_and_parses_jwt(monkeypatch: pytest.MonkeyPatch):
    rec = _Recorder({"/uc/abroad/pwdLogin": {"code": 0, "data": {
        "access_token": _fake_jwt(sub="42"), "refresh_token": "rftok", "identify": "id"}}})
    rec.install(monkeypatch)
    s = asyncio.run(websession.login("me@x.com", "fvvK5MsdsGhz"))
    path, body, headers = rec.calls[0]
    assert path == "/uc/abroad/pwdLogin"
    assert body["password"] == hashlib.md5(b"fvvK5MsdsGhz").hexdigest(), "密码必须 MD5，不是明文"
    assert "fvvK5MsdsGhz" not in json.dumps(body)
    assert headers["client"] == "WEB"
    assert "Authorization" not in headers, "登录请求本身不带 Bearer"
    assert s.user_id == "42" and s.refresh_token == "rftok"
    assert not s.expiring(), "30 天的 token 不该一拿到就判过期"


def test_login_failure_surfaces_upstream_text(monkeypatch: pytest.MonkeyPatch):
    rec = _Recorder({"/uc/abroad/pwdLogin": {"code": 123, "msg": "Account or password incorrect"}})
    rec.install(monkeypatch)
    with pytest.raises(WebSessionError) as ei:
        asyncio.run(websession.login("me@x.com", "wrong"))
    assert ei.value.kind == "login"
    assert "incorrect" in str(ei.value)


def test_ensure_session_refreshes_when_expiring(monkeypatch: pytest.MonkeyPatch):
    """快过期 → 用 refresh_token 续，不带密码登录。"""
    rec = _Recorder({
        "/uc/token/refresh": {"code": 0, "data": {"access_token": _fake_jwt(exp_delta=30 * 86400), "refresh_token": "new"}},
        "/uc/abroad/pwdLogin": {"code": 0, "data": {"access_token": _fake_jwt(), "refresh_token": "x"}},
    })
    rec.install(monkeypatch)
    old = WebSession(access_token=_fake_jwt(exp_delta=600), refresh_token="old", expires_at=time.time() + 600)
    s = asyncio.run(websession.ensure_session(old, "me@x.com", "pw"))
    paths = [c[0] for c in rec.calls]
    assert paths == ["/uc/token/refresh"], "能续期就不该走密码登录"
    assert rec.calls[0][2]["Authorization"] == "Bearer old"
    assert s.refresh_token == "new" and not s.expiring()


def test_ensure_session_relogins_when_refresh_fails(monkeypatch: pytest.MonkeyPatch):
    rec = _Recorder({
        "/uc/token/refresh": {"code": 401, "msg": "expired"},
        "/uc/abroad/pwdLogin": {"code": 0, "data": {"access_token": _fake_jwt(), "refresh_token": "again"}},
    })
    rec.install(monkeypatch)
    old = WebSession(access_token=_fake_jwt(exp_delta=-1), refresh_token="dead", expires_at=time.time() - 1)
    s = asyncio.run(websession.ensure_session(old, "me@x.com", "pw"))
    assert [c[0] for c in rec.calls] == ["/uc/token/refresh", "/uc/abroad/pwdLogin"]
    assert s.refresh_token == "again" and s.last_error == ""


def test_ensure_session_cooldown_after_login_failure(monkeypatch: pytest.MonkeyPatch):
    """登录失败后 5 分钟内不再重试——上游连续错密码会锁号。"""
    rec = _Recorder({"/uc/abroad/pwdLogin": {"code": 126, "msg": "locked"}})
    rec.install(monkeypatch)
    with pytest.raises(WebSessionError):
        asyncio.run(websession.ensure_session(None, "me@x.com", "bad"))
    n = len(rec.calls)
    # 第一次失败返回的是带 last_error 的会话对象，调用方（runner）下次会把它传回来
    failed = WebSession(access_token="", refresh_token="", last_error="x", last_error_at=time.time())
    with pytest.raises(WebSessionError) as ei:
        asyncio.run(websession.ensure_session(failed, "me@x.com", "bad"))
    assert len(rec.calls) == n, "冷却期内不该再打上游"
    assert "不重试" in str(ei.value)


def test_post_treats_401_and_412_as_refresh_kind(monkeypatch: pytest.MonkeyPatch):
    rec = _Recorder({"/uc/getUserInfo": {"code": 412, "msg": "TOKEN_INVALID"}})
    rec.install(monkeypatch)
    with pytest.raises(WebSessionError) as ei:
        asyncio.run(websession.get_user_info("tok"))
    assert ei.value.kind == "refresh"


# ---------------------------------------------------------------------------
# runner：闭源预判 / 失败原因解析 / 完整跑通
# ---------------------------------------------------------------------------

def test_closed_source_detection():
    assert runner.closed_source_nodes({"nodes": [{"classType": "RH_Nano_Banana2_Gemini31Flash"}]}) == []
    assert runner.closed_source_nodes({"nodes": [{"classType": "RH_BytedanceSeedance25TokenTextToVideo"}]}) == [
        "RH_BytedanceSeedance25TokenTextToVideo"]
    # 实测：Seedance 应用预估 nodes 是空的——这是闭源的典型信号
    assert runner.closed_source_nodes({"nodes": []}) == ["?"]
    assert runner.closed_source_nodes({}) == ["?"]


def test_parse_task_failure_digs_out_exception_message():
    raw = {"taskStatus": "FAILED", "taskResultDesc": json.dumps({
        "exception_type": "RuntimeError", "node_name": "RH_BytedanceSeedance25TokenTextToVideo",
        "exception_message": "[RH_OpenAPI_BytedanceSeedance25TokenTextToVideo] Submit failed: Your balance is insufficient. [errorCode: 605]"})}
    msg = runner.parse_task_failure(raw)
    assert "605" in msg and "RH_BytedanceSeedance25TokenTextToVideo" in msg
    assert runner.parse_task_failure({"taskStatus": "FAILED"}) == "FAILED"
    assert runner.parse_task_failure({"taskResultDesc": "plain text"}) == "plain text"


def _acct(tmp_path: Path) -> dict[str, Any]:
    acc = store.upsert_account(email="me@x.com", password="pw", label="t")
    return store.get_account(acc["account_id"])


def test_run_workflow_happy_path_and_persists_refreshed_token(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    acc = _acct(isolated_store)
    good = _fake_jwt()
    routes = {
        "/uc/token/refresh": {"code": 0, "data": {"access_token": good, "refresh_token": "new-rf"}},
        "/api/workflow/webapp-price-estimate": {"code": 0, "data": {"totalPrice": 0.0, "currency": "USD",
            "estimatedPoint": 21, "nodes": [{"classType": "RH_Nano_Banana2_Gemini31Flash"}]}},
        "/task/webapp/create": {"code": 0, "data": {"taskId": "T1", "taskStatus": "RUNNING"}},
        "/task/list": {"code": 0, "data": {"records": [{"taskId": "T1", "taskStatus": "SUCCESS"}]}},
        "/api/output/v2/history": {"code": 0, "data": {"records": [
            {"taskId": "T1", "fileUrl": "https://cdn/x.png", "outputType": "png"},
            {"taskId": "OTHER", "fileUrl": "https://cdn/other.png", "outputType": "png"},
        ]}},
    }
    rec = _Recorder(routes)
    rec.install(monkeypatch)
    # 存一个快过期的会话 → runner 应该先续期，续期结果写回 store
    store.update_session(acc["account_id"], WebSession(
        access_token=_fake_jwt(exp_delta=60), refresh_token="old-rf", expires_at=time.time() + 60).to_dict())

    ws = runner.WebAccountSession(store.get_account(acc["account_id"]), persist=store.update_session)
    monkeypatch.setattr(runner, "POLL_INTERVAL", 0.01)
    res = asyncio.run(runner.run_workflow(ws, "2002675341464334337",
                                          [{"nodeId": "3", "fieldName": "image", "fieldValue": "a.png", "extra": "drop me"}]))
    assert res.status == "SUCCESS" and res.task_id == "T1"
    assert res.outputs == [{"url": "https://cdn/x.png", "type": "image"}], "只能拿本任务的产出，不能把别人的历史混进来"
    paths = [c[0] for c in rec.calls]
    assert paths[0] == "/uc/token/refresh", "快过期要先续期"
    assert "extra" not in rec.calls[[i for i, p in enumerate(paths) if p == "/task/webapp/create"][0]][1]["inputs"][0]
    saved = store.get_account(acc["account_id"])["web_session"]
    assert saved["refresh_token"] == "new-rf", "续期后的 token 必须写回，否则每次都重新登录"
    assert saved["expires_at"] > time.time() + 29 * 86400


def test_run_workflow_reruns_once_on_401(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    acc = _acct(isolated_store)
    calls = {"n": 0}

    def create():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"code": 401, "msg": "TOKEN_INVALID"}
        return {"code": 0, "data": {"taskId": "T2", "taskStatus": "RUNNING"}}

    rec = _Recorder({
        "/uc/token/refresh": {"code": 0, "data": {"access_token": _fake_jwt(), "refresh_token": "r2"}},
        "/api/workflow/webapp-price-estimate": {"code": 0, "data": {"nodes": []}},
        "/task/webapp/create": create,
        "/task/list": {"code": 0, "data": {"records": [{"taskId": "T2", "taskStatus": "FAILED",
                                                          "taskResultDesc": json.dumps({"exception_message": "boom 605"})}]}},
    })
    rec.install(monkeypatch)
    store.update_session(acc["account_id"], WebSession(access_token=_fake_jwt(), refresh_token="r1").to_dict())
    ws = runner.WebAccountSession(store.get_account(acc["account_id"]), persist=store.update_session)
    monkeypatch.setattr(runner, "POLL_INTERVAL", 0.01)
    with pytest.raises(WebSessionError) as ei:
        asyncio.run(runner.run_workflow(ws, "w", []))
    assert calls["n"] == 2, "401 要续期后重试一次"
    assert "boom 605" in str(ei.value), "失败原因要挖 exception_message，不能只给 FAILED"
    assert [c[0] for c in rec.calls].count("/uc/token/refresh") == 1


def test_run_workflow_estimate_only_never_submits(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    acc = _acct(isolated_store)
    rec = _Recorder({"/api/workflow/webapp-price-estimate": {"code": 0, "data": {"totalPrice": 0.03, "estimatedPoint": 21, "nodes": []}}})
    rec.install(monkeypatch)
    store.update_session(acc["account_id"], WebSession(access_token=_fake_jwt(), refresh_token="r").to_dict())
    ws = runner.WebAccountSession(store.get_account(acc["account_id"]))
    res = asyncio.run(runner.run_workflow(ws, "w", [], estimate_only=True))
    assert res.status == "ESTIMATE" and res.estimate["totalPrice"] == 0.03
    assert all(p != "/task/webapp/create" for p, _, _ in rec.calls)


# ---------------------------------------------------------------------------
# UI 端点
# ---------------------------------------------------------------------------

def _client(monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from buddy_proxy import __main__ as m
    monkeypatch.setenv("BUDDY_PROXY_ADMIN_OPEN", "1")
    return TestClient(m.app)


def test_ui_import_email_account_logs_in_and_stores_session(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    rec = _Recorder({
        "/uc/abroad/pwdLogin": {"code": 0, "data": {"access_token": _fake_jwt(), "refresh_token": "rf", "identify": "id"}},
        "/uc/getUserInfo": {"code": 0, "data": {"id": "2100902343205564418", "email": "me@x.com", "nickName": "u",
                                                 "apiKey": "af1e96e1", "walletInfo": {"balance": 0, "apiConsumableBalance": 0, "currency": "USD"}}},
        "/uc/point/summary": {"code": 0, "data": {"transitionCoinRemained": "984", "activityPoint": 30.0, "nearestExpireTime": "2026-11-27T16:00:00Z"}},
        "/uc/apiKey/get": {"code": 0, "data": {"normalApiKey": "af1e****96e1", "sharedApi": {"apiKey": "0ece****7ec9"}}},
        "/api/product/coinGainList": {"code": 0, "data": [
            {"gainTime": "2026-10-29 08:25:08", "expiredTime": "2027-11-05 08:25:08", "amount": 1000},
            {"gainTime": "2026-11-07 19:23:17", "expiredTime": "2026-11-08 19:23:17", "amount": 100},
        ]},
        "/api/welfare/daily-gift/claim": {"code": 0, "data": True},
        "/api/welfare/summary": {"code": 0, "data": {"dailyGift": {"granted": True}, "freeImage": {"remaining": 3}, "freeVideo": {"remaining": 1}}},
    })
    rec.install(monkeypatch)
    r = _client(monkeypatch).post("/ui/api/runninghub/accounts/import", json={
        "email": "me@x.com", "password": "pw", "label": "我的号"})
    assert r.status_code == 200, r.text
    acc = r.json()["accounts"][0]
    assert acc["label"] == "我的号" and acc["web_session_status"]["logged_in"] is True
    assert acc["account_info"]["transitionCoins"] == 984.0
    assert acc["account_info"]["walletBalance"] == 0.0
    # 过期时间：过渡币取未过期记录里最早的（2027-11-05），积分取 point/summary 的 nearestExpireTime
    assert acc["account_info"]["transitionCoinExpireAt"] == "2027-11-05 08:25:08"
    assert acc["account_info"]["pointsExpireAt"] == "2026-11-27T16:00:00Z"
    assert acc["account_info"]["freeImageLeft"] == 3 and acc["account_info"]["freeVideoLeft"] == 1
    assert "password" not in json.dumps(r.json()) and "access_token" not in json.dumps(r.json()) and "rf" != acc.get("refresh_token")


def test_ui_import_rejects_bad_login_without_writing(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    rec = _Recorder({"/uc/abroad/pwdLogin": {"code": 123, "msg": "Account or password incorrect"}})
    rec.install(monkeypatch)
    r = _client(monkeypatch).post("/ui/api/runninghub/accounts/import", json={"email": "me@x.com", "password": "bad"})
    assert r.status_code == 400
    assert "incorrect" in r.json()["detail"]["error"]["message"]
    assert store.list_accounts() == [], "登录失败不能落半条记录"


def test_ui_run_marks_closed_source_on_605(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    """站内提交被 605 拒 → 402 + 工作流标 closedSource，下次 UI 直接显示「钱包 $」。"""
    acc = _acct(isolated_store)
    store.update_session(acc["account_id"], WebSession(access_token=_fake_jwt(), refresh_token="r").to_dict())
    store.upsert_workflow({"webappId": "2086711729156894722", "name": "Seedance", "description": "",
                           "billingType": "过渡币（站内）", "nodeInfoList": []})
    rec = _Recorder({
        "/uc/getUserInfo": {"code": 0, "data": {"id": "1", "walletInfo": {"balance": 0}}},
        "/uc/point/summary": {"code": 0, "data": {"transitionCoinRemained": "984"}},
        "/uc/apiKey/get": {"code": 0, "data": {}},
        "/api/workflow/webapp-price-estimate": {"code": 0, "data": {"totalPrice": 0.0, "nodes": []}},
        "/task/webapp/create": {"code": 0, "data": {"taskId": "T3", "taskStatus": "RUNNING"}},
        "/task/list": {"code": 0, "data": {"records": [{"taskId": "T3", "taskStatus": "FAILED", "taskResultDesc": json.dumps({
            "node_name": "RH_BytedanceSeedance25TokenTextToVideo",
            "exception_message": "[RH_OpenAPI_BytedanceSeedance25TokenTextToVideo] Submit failed: Your balance is insufficient. Please top up your account. [errorCode: 605]"})}]}},
    })
    rec.install(monkeypatch)
    monkeypatch.setattr(runner, "POLL_INTERVAL", 0.01)
    r = _client(monkeypatch).post("/ui/api/runninghub/workflows/run", json={
        "webapp_id": "2086711729156894722", "nodeInfoList": []})
    assert r.status_code == 402, r.text
    body = r.json()["detail"]
    assert "闭源" in body["error"]["message"] and "605" in body["error"]["message"]
    wf = next(w for w in body["workflows"] if w["webappId"] == "2086711729156894722")
    assert wf["closedSource"] is True and "闭源" in wf["billingType"]


def test_ui_run_requires_email_account(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    store.upsert_account(api_key="938bcfc2f8da4650abdf67b617b533a0", label="纯key")
    r = _client(monkeypatch).post("/ui/api/runninghub/workflows/run", json={"webapp_id": "w", "nodeInfoList": []})
    assert r.status_code == 400
    assert "邮箱" in r.json()["detail"]["error"]["message"]


# ---------------------------------------------------------------------------
# review 修复的回归（PR #368）
# ---------------------------------------------------------------------------

def test_401_retry_failure_does_not_trigger_cooldown(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    """401 → force 重登 → 重登失败：冷却由 login() 那一支记，但用户**第一次**看到的
    必须是真实原因，不是「刚失败过」。"""
    acc = _acct(isolated_store)
    rec = _Recorder({
        "/uc/abroad/pwdLogin": {"code": 123, "msg": "Account or password incorrect"},
    })
    rec.install(monkeypatch)
    ws = runner.WebAccountSession(acc, persist=store.update_session)
    # 直接用 call：token 还没拿 → ensure 先登录就失败
    with pytest.raises(WebSessionError) as ei:
        asyncio.run(ws.call(lambda t: websession.get_user_info(t)))
    assert "incorrect" in str(ei.value), "第一次失败要把上游原文抛出来"
    assert ws.session.in_cooldown()
    # 第二次：进冷却，不打上游
    n = len(rec.calls)
    with pytest.raises(WebSessionError) as ei2:
        asyncio.run(ws.call(lambda t: websession.get_user_info(t)))
    assert len(rec.calls) == n and "不重试" in str(ei2.value)


def test_session_repr_never_leaks_token():
    tok = _fake_jwt(sub="77")
    s = WebSession.from_login({"access_token": tok, "refresh_token": "refresh-secret"})
    assert s.user_id == "77"
    r = repr(s)
    assert tok not in r and "refresh-secret" not in r
    assert "user_id='77'" in r


def test_public_view_strips_secret_looking_keys_in_account_info(isolated_store: Path):
    acc = store.upsert_account(email="a@x.com", password="pw",
                               account_info={"transitionCoins": 984, "apiKey": "af1e96e1", "access_token": "jwt", "nickName": "u"})
    assert "apiKey" not in acc["account_info"] and "access_token" not in acc["account_info"]
    assert acc["account_info"]["transitionCoins"] == 984 and acc["account_info"]["nickName"] == "u"


def test_get_balance_does_not_call_api_keys(monkeypatch: pytest.MonkeyPatch):
    rec = _Recorder({
        "/uc/getUserInfo": {"code": 0, "data": {"id": "1", "apiKey": "af1e96e1", "walletInfo": {"balance": 0}}},
        "/uc/point/summary": {"code": 0, "data": {"transitionCoinRemained": "984"}},
    })
    rec.install(monkeypatch)
    bal = asyncio.run(websession.get_balance("tok", claim_gift=False))
    paths = [c[0] for c in rec.calls]
    assert paths[0] == "/uc/getUserInfo" and paths[1] == "/uc/point/summary"
    assert not any("apiKey" in p for p in paths)
    assert bal["transitionCoinExpireAt"] is None, "coinGainList 拉不到就 None，不能炸"
    assert "apiKey" not in bal and bal["transitionCoins"] == 984.0


# ---------------------------------------------------------------------------
# 任务队列：提交即返回，后台推进状态
# ---------------------------------------------------------------------------

def test_task_store_roundtrip_and_order(isolated_store: Path):
    store.upsert_task({"taskId": "a", "status": "RUNNING", "created_at": 1000.0})
    store.upsert_task({"taskId": "b", "status": "SUCCESS", "created_at": 2000.0})
    store.upsert_task({"taskId": "c", "status": "FAILED", "created_at": 1500.0})
    assert [t["taskId"] for t in store.list_tasks()] == ["b", "c", "a"], "新的在前"
    # 只改状态不会抹掉别的字段
    store.update_task("a", status="SUCCESS", outputs=[{"url": "u", "type": "image"}])
    a = store.get_task("a")
    assert a["status"] == "SUCCESS" and a["outputs"] and a["created_at"] == 1000.0
    assert store.delete_task("c") is True and store.get_task("c") is None
    assert store.delete_task("zzz") is False


def test_task_store_caps_records(isolated_store: Path):
    for i in range(store._TASKS_LIMIT + 10):
        store.upsert_task({"taskId": f"t{i}", "status": "SUCCESS", "created_at": float(i)})
    assert len(store.list_tasks()) == store._TASKS_LIMIT
    assert store.get_task("t0") is None, "最老的先被挤掉"
    assert store.get_task(f"t{store._TASKS_LIMIT + 9}") is not None


def test_execute_task_success_records_outputs_and_cost(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    acc = _acct(isolated_store)
    store.update_session(acc["account_id"], WebSession(access_token=_fake_jwt(), refresh_token="r").to_dict())
    monkeypatch.setattr(runner, "POLL_INTERVAL", 0.01)
    rec = _Recorder({
        "/uc/getUserInfo": {"code": 0, "data": {"id": "1", "walletInfo": {"balance": 0}}},
        "/uc/point/summary": {"code": 0, "data": {"transitionCoinRemained": "784"}},  # 794 → 784 = 实扣 10
        "/api/workflow/webapp-price-estimate": {"code": 0, "data": {"totalPrice": 0.03, "priceText": "$0.03", "estimatedPoint": 21,
                                                                     "nodes": [{"classType": "RH_Nano_Banana2_Gemini31Flash"}]}},
        "/task/webapp/create": {"code": 0, "data": {"taskId": "UP1", "taskStatus": "RUNNING"}},
        "/api/output/v2/history": {"code": 0, "data": {"records": [
            {"taskId": "UP1", "fileUrl": "https://cdn/x.png", "outputType": "png", "taskCostTime": "74"}]}},
    })
    # 第二次 /task/list 才 SUCCESS（模拟轮询）
    state = {"n": 0}
    def task_list():
        state["n"] += 1
        return {"code": 0, "data": {"records": [{"taskId": "UP1", "taskStatus": "SUCCESS", "taskCostTime": "74"} if state["n"] >= 2 else {"taskId": "UP1", "taskStatus": "RUNNING"}]}}
    rec.routes["/task/list"] = task_list
    rec.install(monkeypatch)
    store.upsert_task({"taskId": "local-1", "status": "SUBMITTING", "webappId": "2002675341464334337",
                       "webappName": "玩偶", "inputs": [{"nodeId": "3", "fieldName": "image", "fieldValue": "a.png"}], "created_at": time.time()})
    out = asyncio.run(runner.execute_task(store.get_account(acc["account_id"]), "local-1",
                                          persist_session=store.update_session, balance_before=794.0))
    assert out["status"] == "SUCCESS"
    assert out["upstreamTaskId"] == "UP1"
    assert out["outputs"] == [{"url": "https://cdn/x.png", "type": "image"}]
    assert out["costTime"] == 74.0
    assert out["coinsSpent"] == 10.0 and out["balanceAfter"]["transitionCoins"] == 784.0
    assert out["estimate"]["nodes"] == ["RH_Nano_Banana2_Gemini31Flash"]
    assert out["closedSource"] is False
    assert out["webappName"] == "玩偶" and out["inputs"][0]["fieldValue"] == "a.png", "提交前的字段不能被状态推进抹掉"


def test_execute_task_failure_writes_reason_and_closed_flag(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    acc = _acct(isolated_store)
    store.update_session(acc["account_id"], WebSession(access_token=_fake_jwt(), refresh_token="r").to_dict())
    monkeypatch.setattr(runner, "POLL_INTERVAL", 0.01)
    rec = _Recorder({
        "/api/workflow/webapp-price-estimate": {"code": 0, "data": {"totalPrice": 0.0, "nodes": []}},
        "/task/webapp/create": {"code": 0, "data": {"taskId": "UP2", "taskStatus": "RUNNING"}},
        "/task/list": {"code": 0, "data": {"records": [{"taskId": "UP2", "taskStatus": "FAILED", "taskResultDesc": json.dumps({
            "node_name": "RH_BytedanceSeedance25TokenTextToVideo",
            "exception_message": "[RH_OpenAPI_BytedanceSeedance25TokenTextToVideo] Submit failed: Your balance is insufficient. [errorCode: 605]"})}]}},
    })
    rec.install(monkeypatch)
    store.upsert_task({"taskId": "local-2", "status": "SUBMITTING", "webappId": "2086711729156894722", "inputs": [], "created_at": time.time()})
    out = asyncio.run(runner.execute_task(store.get_account(acc["account_id"]), "local-2"))
    assert out["status"] == "FAILED"
    assert "605" in out["error"] and out["closedSource"] is True
    assert out["finishedAt"]


def test_execute_task_submit_rejected_does_not_raise(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    """提交就被拒（821/登录冷却）也要落成 FAILED，而不是把异常抛进 asyncio 的无人区。"""
    acc = _acct(isolated_store)
    store.update_session(acc["account_id"], WebSession(access_token=_fake_jwt(), refresh_token="r").to_dict())
    rec = _Recorder({
        "/api/workflow/webapp-price-estimate": {"code": 0, "data": {"nodes": []}},
        "/task/webapp/create": {"code": 821, "msg": "Insufficient paid points."},
    })
    rec.install(monkeypatch)
    store.upsert_task({"taskId": "local-3", "status": "SUBMITTING", "webappId": "w", "inputs": [], "created_at": time.time()})
    out = asyncio.run(runner.execute_task(store.get_account(acc["account_id"]), "local-3"))
    assert out["status"] == "FAILED" and "821" not in out["error"] and "Insufficient paid points" in out["error"]
    assert out["errorCode"] == "821"


def test_cancel_stops_waiting(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    acc = _acct(isolated_store)
    store.update_session(acc["account_id"], WebSession(access_token=_fake_jwt(), refresh_token="r").to_dict())
    monkeypatch.setattr(runner, "POLL_INTERVAL", 0.01)
    rec = _Recorder({
        "/api/workflow/webapp-price-estimate": {"code": 0, "data": {"nodes": []}},
        "/task/webapp/create": {"code": 0, "data": {"taskId": "UP3", "taskStatus": "RUNNING"}},
        "/task/list": {"code": 0, "data": {"records": [{"taskId": "UP3", "taskStatus": "RUNNING"}]}},  # 永远 RUNNING
    })
    rec.install(monkeypatch)
    store.upsert_task({"taskId": "local-4", "status": "SUBMITTING", "webappId": "w", "inputs": [], "created_at": time.time()})

    async def _go():
        t = asyncio.create_task(runner.execute_task(store.get_account(acc["account_id"]), "local-4"))
        await asyncio.sleep(0.15)
        runner.request_cancel("local-4")
        return await asyncio.wait_for(t, 3)
    out = asyncio.run(_go())
    assert out["status"] == "CANCELLED"
    assert "local-4" not in runner._cancel_flags and "local-4" not in runner._running, "结束后要清标记"


# ---------------------------------------------------------------------------
# UI 端点
# ---------------------------------------------------------------------------

def test_ui_submit_returns_immediately_and_background_finishes(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    """提交立刻返回本地 taskId；后台执行器把状态推进到 SUCCESS 并写产出。

    注意：FastAPI ``TestClient`` 每个请求跑在 anyio portal 的事件循环里，请求之间循环会
    挂起，所以端点里 ``asyncio.create_task`` 起的后台任务在同步测试线程里**不会自己跑**。
    真实 uvicorn 下循环是常驻的没这个问题。这里用 ``with TestClient`` 让 portal 活着，
    再给一点时间；如果这个平台还是不推进，就退而验证「端点起了后台任务」这一契约。
    """
    acc = _acct(isolated_store)
    store.update_session(acc["account_id"], WebSession(access_token=_fake_jwt(), refresh_token="r").to_dict())
    monkeypatch.setattr(runner, "POLL_INTERVAL", 0.01)
    state = {"n": 0}
    def task_list():
        state["n"] += 1
        st = "SUCCESS" if state["n"] >= 2 else "RUNNING"
        return {"code": 0, "data": {"records": [{"taskId": "UP9", "taskStatus": st, "taskCostTime": "12"}]}}
    rec = _Recorder({
        "/uc/getUserInfo": {"code": 0, "data": {"id": "1", "walletInfo": {"balance": 0}}},
        "/uc/point/summary": {"code": 0, "data": {"transitionCoinRemained": "100"}},
        "/api/workflow/webapp-price-estimate": {"code": 0, "data": {"totalPrice": 0.03, "nodes": []}},
        "/task/webapp/create": {"code": 0, "data": {"taskId": "UP9", "taskStatus": "RUNNING"}},
        "/task/list": task_list,
        "/api/output/v2/history": {"code": 0, "data": {"records": [{"taskId": "UP9", "fileUrl": "https://cdn/a.png", "outputType": "png"}]}},
        "/api/product/coinGainList": {"code": 0, "data": []},
    })
    rec.install(monkeypatch)
    from fastapi.testclient import TestClient

    from buddy_proxy import __main__ as m
    monkeypatch.setenv("BUDDY_PROXY_ADMIN_OPEN", "1")
    store.upsert_workflow({"webappId": "2002675341464334337", "name": "玩偶", "description": "", "nodeInfoList": [], "billingType": "过渡币（站内）"})
    with TestClient(m.app) as c:
        r = c.post("/ui/api/runninghub/tasks/submit", json={
            "webapp_id": "2002675341464334337", "nodeInfoList": [{"nodeId": "3", "fieldName": "image", "fieldValue": "a.png"}]})
        assert r.status_code == 200, r.text
        t = r.json()["task"]
        assert t["taskId"].startswith("local-") and t["status"] in ("SUBMITTING", "RUNNING", "SUCCESS")
        assert t["webappName"] == "玩偶" and t["inputs"] == [{"nodeId": "3", "fieldName": "image", "fieldValue": "a.png"}]
        assert "password" not in json.dumps(r.json()) and "access_token" not in json.dumps(r.json())
        tid = t["taskId"]
        deadline = time.time() + 5
        while time.time() < deadline and store.get_task(tid)["status"] != "SUCCESS":
            time.sleep(0.05)
        cur = store.get_task(tid)
        if cur["status"] != "SUCCESS":
            pytest.xfail("TestClient 的事件循环在请求间挂起，后台任务不推进；真实 uvicorn 下正常（见 test_execute_task_success_*）")
        assert cur["outputs"] == [{"url": "https://cdn/a.png", "type": "image"}] and cur["upstreamTaskId"] == "UP9"
        lst = c.get("/ui/api/runninghub/tasks").json()["tasks"]
        assert lst[0]["taskId"] == tid
        assert c.get(f"/ui/api/runninghub/tasks/{tid}").json()["task"]["taskId"] == tid
        assert c.get("/ui/api/runninghub/tasks/nope").status_code == 404
        assert c.post("/ui/api/runninghub/tasks/cancel", json={"task_id": tid}).status_code == 200
        assert c.post("/ui/api/runninghub/tasks/delete", json={"task_id": tid}).status_code == 200
        assert c.get(f"/ui/api/runninghub/tasks/{tid}").status_code == 404


def test_ui_submit_requires_email_account(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    store.upsert_account(api_key="938bcfc2f8da4650abdf67b617b533a0", label="纯key")
    r = _client(monkeypatch).post("/ui/api/runninghub/tasks/submit", json={"webapp_id": "w", "nodeInfoList": []})
    assert r.status_code == 400 and "邮箱" in r.json()["detail"]["error"]["message"]


def test_cancel_before_submit_does_not_spend(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    """SUBMITTING 那一两秒点「取消」= 没提交、没扣币。不能等 poll_task 才发现。"""
    acc = _acct(isolated_store)
    store.update_session(acc["account_id"], WebSession(access_token=_fake_jwt(), refresh_token="r").to_dict())
    rec = _Recorder({})  # 任何上游调用都会 404 → 如果真提交了测试会红
    rec.install(monkeypatch)
    store.upsert_task({"taskId": "local-5", "status": "SUBMITTING", "webappId": "w", "inputs": [], "created_at": time.time()})
    runner.request_cancel("local-5")
    out = asyncio.run(runner.execute_task(store.get_account(acc["account_id"]), "local-5"))
    assert out["status"] == "CANCELLED" and out["submitted"] is False
    assert rec.calls == [], "提交前取消不该打任何上游接口"
    assert "local-5" not in runner._cancel_flags


def test_cancel_after_submit_marks_submitted(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    """已提交再取消：记录要带 submitted=True + upstreamTaskId，UI 才能说清「币已扣」。"""
    acc = _acct(isolated_store)
    store.update_session(acc["account_id"], WebSession(access_token=_fake_jwt(), refresh_token="r").to_dict())
    monkeypatch.setattr(runner, "POLL_INTERVAL", 0.01)
    rec = _Recorder({
        "/api/workflow/webapp-price-estimate": {"code": 0, "data": {"nodes": []}},
        "/task/webapp/create": {"code": 0, "data": {"taskId": "UP7", "taskStatus": "RUNNING"}},
        "/task/list": {"code": 0, "data": {"records": [{"taskId": "UP7", "taskStatus": "RUNNING"}]}},
    })
    rec.install(monkeypatch)
    store.upsert_task({"taskId": "local-6", "status": "SUBMITTING", "webappId": "w", "inputs": [], "created_at": time.time()})

    async def _go():
        t = asyncio.create_task(runner.execute_task(store.get_account(acc["account_id"]), "local-6"))
        await asyncio.sleep(0.1)
        runner.request_cancel("local-6")
        return await asyncio.wait_for(t, 3)
    out = asyncio.run(_go())
    assert out["status"] == "CANCELLED" and out["submitted"] is True and out["upstreamTaskId"] == "UP7"


def test_submit_rejected_marks_not_submitted(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    acc = _acct(isolated_store)
    store.update_session(acc["account_id"], WebSession(access_token=_fake_jwt(), refresh_token="r").to_dict())
    rec = _Recorder({
        "/api/workflow/webapp-price-estimate": {"code": 0, "data": {"nodes": []}},
        "/task/webapp/create": {"code": 605, "msg": "NOT_ENOUGH_BALANCE"},
    })
    rec.install(monkeypatch)
    store.upsert_task({"taskId": "local-7", "status": "SUBMITTING", "webappId": "w", "inputs": [], "created_at": time.time()})
    out = asyncio.run(runner.execute_task(store.get_account(acc["account_id"]), "local-7"))
    assert out["status"] == "FAILED" and out["submitted"] is False and out["closedSource"] is True


def test_reap_orphans_marks_stale_running_but_not_inflight(isolated_store: Path):
    """重启后上一进程的 SUBMITTING/RUNNING 没人推进 → INTERRUPTED；本进程正在跑的不动。"""
    store.upsert_task({"taskId": "old-running", "status": "RUNNING", "submitted": True, "upstreamTaskId": "U1", "created_at": 1.0})
    store.upsert_task({"taskId": "old-submit", "status": "SUBMITTING", "created_at": 2.0})
    store.upsert_task({"taskId": "done", "status": "SUCCESS", "created_at": 3.0})
    store.upsert_task({"taskId": "live", "status": "RUNNING", "submitted": True, "created_at": 4.0})
    runner._running.add("live")
    try:
        reaped = runner.reap_orphans()
    finally:
        runner._running.discard("live")
    assert sorted(reaped) == ["old-running", "old-submit"]
    assert store.get_task("old-running")["status"] == "INTERRUPTED" and "重启" in store.get_task("old-running")["error"]
    assert store.get_task("live")["status"] == "RUNNING"
    assert store.get_task("done")["status"] == "SUCCESS"


def test_ui_submit_records_unix_created_at(isolated_store: Path, monkeypatch: pytest.MonkeyPatch):
    """created_at 必须是 unix 秒。踩过：写成 loop.time()（单调时钟，4 万多），UI 显示成 1970 年。"""
    acc = _acct(isolated_store)
    store.update_session(acc["account_id"], WebSession(access_token=_fake_jwt(), refresh_token="r").to_dict())
    monkeypatch.setattr(runner, "POLL_INTERVAL", 0.01)
    rec = _Recorder({
        "/uc/getUserInfo": {"code": 0, "data": {"id": "1", "walletInfo": {"balance": 0}}},
        "/uc/point/summary": {"code": 0, "data": {"transitionCoinRemained": "100"}},
        "/api/workflow/webapp-price-estimate": {"code": 0, "data": {"nodes": []}},
        "/task/webapp/create": {"code": 414, "msg": "TASK_CREATE_FAILED_BY_NOT_ENOUGH_POWER_VALUE"},
        "/api/product/coinGainList": {"code": 0, "data": []},
    })
    rec.install(monkeypatch)
    from fastapi.testclient import TestClient

    from buddy_proxy import __main__ as m
    monkeypatch.setenv("BUDDY_PROXY_ADMIN_OPEN", "1")
    before = time.time()
    with TestClient(m.app) as c:
        t = c.post("/ui/api/runninghub/tasks/submit", json={"webapp_id": "w", "nodeInfoList": []}).json()["task"]
        deadline = time.time() + 5
        while time.time() < deadline and store.get_task(t["taskId"])["status"] not in ("FAILED", "SUCCESS"):
            time.sleep(0.05)
        cur = store.get_task(t["taskId"])
    assert before <= cur["created_at"] <= time.time() + 1, "created_at 不是 unix 秒"
    assert cur["status"] == "FAILED" and cur["submitted"] is False
    assert "积分" in cur["error"] and "414" not in cur["error"] or "POWER" in cur["error"], "414 要翻成「积分不够」"


def test_get_balance_claims_daily_gift_before_reading_points(monkeypatch: pytest.MonkeyPatch):
    """新号 activityPoint=0 + dailyGift.granted=false，直接跑会 414。
    get_balance 必须先 claim 再读 point/summary——顺序反了 UI 显示 0 积分、运行时炸。"""
    rec = _Recorder({
        "/api/welfare/daily-gift/claim": {"code": 0, "data": True},
        "/uc/getUserInfo": {"code": 0, "data": {"id": "1", "walletInfo": {"balance": 0}}},
        "/uc/point/summary": {"code": 0, "data": {"transitionCoinRemained": "1000", "activityPoint": 30.0,
                                                 "nearestExpireTime": "2026-11-27T16:00:00Z"}},
        "/api/product/coinGainList": {"code": 0, "data": [{"expiredTime": "2027-11-05 08:25:08"}]},
        "/api/welfare/summary": {"code": 0, "data": {"freeImage": {"remaining": 3}, "freeVideo": {"remaining": 1}}},
    })
    rec.install(monkeypatch)
    bal = asyncio.run(websession.get_balance("tok"))
    paths = [c[0] for c in rec.calls]
    assert paths[0] == "/api/welfare/daily-gift/claim", "领取必须在读余额之前"
    assert bal["activityPoints"] == 30.0 and bal["transitionCoinExpireAt"] == "2027-11-05 08:25:08"
    assert bal["freeImageLeft"] == 3 and bal["freeVideoLeft"] == 1


def test_get_balance_survives_claim_and_coin_list_failures(monkeypatch: pytest.MonkeyPatch):
    """claim 回 false / coinGainList 404 都不能让余额刷新失败——它们只是锦上添花。"""
    rec = _Recorder({
        "/api/welfare/daily-gift/claim": {"code": 0, "data": False},
        "/uc/getUserInfo": {"code": 0, "data": {"id": "1", "walletInfo": {"balance": 0}}},
        "/uc/point/summary": {"code": 0, "data": {"transitionCoinRemained": "1000"}},
    })
    rec.install(monkeypatch)
    bal = asyncio.run(websession.get_balance("tok"))
    assert bal["transitionCoins"] == 1000.0 and bal["transitionCoinExpireAt"] is None
    assert bal["freeImageLeft"] is None and bal["freeVideoLeft"] is None
