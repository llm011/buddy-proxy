"""RunningHub 每日积分自动领取。

锁住的行为：
- 「站点今天」= UTC 16:00 之后算第二天（积分在那个点清零）
- 已领过站点今天的号不再打上游；上游回 false 也记成「已领」，否则每轮都来问
- 单号失败不影响其他号，且有退避；手动 claim_now 不受退避限制、已领过也照问
- 领完顺手刷余额，账号卡立刻显示 30 分
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from buddy_proxy.runninghub import dailygift, runner, store, websession
from buddy_proxy.runninghub.websession import WebSession, WebSessionError

from test_runninghub_web import _Recorder, _fake_jwt
from buddy_proxy.runninghub.runner import WebAccountSession


@pytest.fixture
def iso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "runninghub_accounts.json"
    monkeypatch.setenv("RUNNINGHUB_ACCOUNTS_PATH", str(p))
    monkeypatch.setenv("RUNNINGHUB_WORKFLOWS_PATH", str(tmp_path / "runninghub_workflows.json"))
    monkeypatch.setenv("RUNNINGHUB_TASKS_PATH", str(tmp_path / "runninghub_tasks.json"))
    monkeypatch.setenv("RUNNINGHUB_GIFT_STATE_PATH", str(tmp_path / "runninghub_gift.json"))
    monkeypatch.setattr(dailygift, "_manager", None)
    return p


# ---------------------------------------------------------------------------
# site_day_key
# ---------------------------------------------------------------------------

def test_site_day_key_flips_at_16utc():
    def ts(s: str) -> float:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
    assert dailygift.site_day_key(ts("2026-11-27T15:59:59")) == "2026-11-26", "清零前还算昨天"
    assert dailygift.site_day_key(ts("2026-11-27T16:00:00")) == "2026-11-27", "清零那一刻翻到今天"
    assert dailygift.site_day_key(ts("2026-11-28T00:00:01")) == "2026-11-27"


# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------

def _acct(email: str, sub: str) -> dict:
    acc = store.upsert_account(email=email, password="pw", label=email)
    store.update_session(acc["account_id"], WebSession(access_token=_fake_jwt(sub=sub), refresh_token="r").to_dict())
    return store.get_account(acc["account_id"])


def _sub_of(tok: str) -> str:
    import base64
    part = tok.split(".")[1]; part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))["sub"]


def _install_routes(monkeypatch: pytest.MonkeyPatch, claim_by_sub: dict[str, object]):
    """按 JWT sub 决定 /claim 的结果：True / False / 抛 500。"""
    rec = _Recorder({})
    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None, **kw):
            tok = (headers or {}).get("Authorization", "").replace("Bearer ", "")
            path = url.split("runninghub.ai", 1)[-1]
            sub = _sub_of(tok) if tok.count(".") == 2 else ""
            rec.calls.append((path, sub))
            if path == "/api/welfare/daily-gift/claim":
                r = claim_by_sub.get(sub, False)
                if isinstance(r, Exception):
                    return type("R", (), {"status_code": 500, "json": lambda self=None: {"code": 500, "msg": "boom"}})()
                return type("R", (), {"status_code": 200, "json": lambda self=None, r=r: {"code": 0, "data": r}})()
            if path == "/uc/getUserInfo":
                return type("R", (), {"status_code": 200, "json": lambda self=None: {"code": 0, "data": {"id": sub, "walletInfo": {"balance": 0}}}})()
            if path == "/uc/point/summary":
                return type("R", (), {"status_code": 200, "json": lambda self=None: {"code": 0, "data": {"transitionCoinRemained": "1000", "activityPoint": 30.0}}})()
            if path == "/api/product/coinGainList":
                return type("R", (), {"status_code": 200, "json": lambda self=None: {"code": 0, "data": []}})()
            if path == "/api/welfare/summary":
                return type("R", (), {"status_code": 200, "json": lambda self=None: {"code": 0, "data": {}}})()
            return type("R", (), {"status_code": 200, "json": lambda self=None: {"code": 404, "msg": path}})()
    monkeypatch.setattr(websession.httpx, "AsyncClient", lambda **kw: _Client())
    return rec


def test_tick_claims_unclaimed_and_skips_already_claimed(iso: Path, monkeypatch: pytest.MonkeyPatch):
    a, b = _acct("a@x.com", "1"), _acct("b@x.com", "2")
    rec = _install_routes(monkeypatch, {"1": True, "2": False})  # b 上游回 false = 已领过
    m = dailygift.DailyGiftManager()
    claimed = asyncio.run(m.tick())
    assert claimed == [a["account_id"]], "只有真领到的算 claimed"
    # 两个号都记成「站点今天已领」
    assert m.is_claimed_today(a["account_id"]) and m.is_claimed_today(b["account_id"])
    st = m.status()["accounts"]
    assert st[b["account_id"]].get("error") is None
    # 落盘了，新 manager 也认
    assert dailygift.DailyGiftManager().is_claimed_today(a["account_id"])
    # 再巡一轮：一个上游都不打
    n = len(rec.calls)
    assert asyncio.run(m.tick()) == []
    assert [c for c in rec.calls[n:] if c[0] == "/api/welfare/daily-gift/claim"] == [], "已领过不该再打 claim"
    # 领完顺手刷了余额：a 的 account_info 里积分是 30
    assert store.get_account(a["account_id"])["account_info"]["activityPoints"] == 30.0


def test_tick_failure_isolated_and_backed_off(iso: Path, monkeypatch: pytest.MonkeyPatch):
    a, bad, c = _acct("a@x.com", "1"), _acct("bad@x.com", "2"), _acct("c@x.com", "3")
    boom = Exception("boom")
    _install_routes(monkeypatch, {"1": True, "2": boom, "3": True})
    m = dailygift.DailyGiftManager()
    claimed = asyncio.run(m.tick())
    assert sorted(claimed) == sorted([a["account_id"], c["account_id"]]), "bad 失败不影响 a / c"
    assert not m.is_claimed_today(bad["account_id"])
    st = m.status()["accounts"][bad["account_id"]]
    assert st["error"], "失败要有原因可显示"
    assert st["next_try_in"] and st["next_try_in"] >= dailygift.BACKOFF_BASE_S - 2, "失败要退避"
    # 退避期内再巡：bad 不再打上游，但别的照常（都领过了所以也没有）
    # 把 a / c 标成没领，验证 bad 在退避期内被跳过而 a 会再打
    m._state.pop(a["account_id"])
    m._last_attempt.clear()
    asyncio.run(m.tick())
    assert not m.is_claimed_today(bad["account_id"]), "退避期内不该重试 bad"


def test_site_day_rollover_reclaims(iso: Path, monkeypatch: pytest.MonkeyPatch):
    """过了 UTC 16:00 清零点，昨天领过的号今天要再领。"""
    a = _acct("a@x.com", "1")
    rec = _install_routes(monkeypatch, {"1": True})
    m = dailygift.DailyGiftManager()
    # 伪装成「昨天领的」
    m._state[a["account_id"]] = {"day": "2000-01-01", "at": 0}
    m._last_attempt.clear()
    assert asyncio.run(m.tick()) == [a["account_id"]]
    assert [c for c in rec.calls if c[0] == "/api/welfare/daily-gift/claim"]


def test_claim_now_forces_and_reports(iso: Path, monkeypatch: pytest.MonkeyPatch):
    a, b, bad = _acct("a@x.com", "1"), _acct("b@x.com", "2"), _acct("bad@x.com", "3")
    _install_routes(monkeypatch, {"1": True, "2": False, "3": Exception("nope")})
    m = dailygift.DailyGiftManager()
    # 先标成今天都领过了
    for x in (a, b, bad):
        m._state[x["account_id"]] = {"day": dailygift.site_day_key(), "at": time.time()}
        m._next_try[x["account_id"]] = time.time() + 9999  # 还在退避里
    out = asyncio.run(m.claim_now())
    assert out["claimed"] == [a["account_id"]]
    assert out["already"] == [b["account_id"]]
    assert out["failed"][bad["account_id"]], "失败要把原因带回前端"
    # 只领一个
    out1 = asyncio.run(m.claim_now(b["account_id"]))
    assert out1["already"] == [b["account_id"]] and out1["claimed"] == []


def test_manager_lifecycle(iso: Path, monkeypatch: pytest.MonkeyPatch):
    a = _acct("a@x.com", "1")
    _install_routes(monkeypatch, {"1": True})
    monkeypatch.setattr(dailygift, "CHECK_INTERVAL_S", 3600)  # 一轮后别再跑
    m = dailygift.get_manager()
    async def go():
        m.start()
        assert m.status()["running"] is True
        await asyncio.sleep(0.1)
        await m.stop()
        assert m.status()["running"] is False
    asyncio.run(go())
    assert m.is_claimed_today(a["account_id"]), "start 后首轮就领"
    assert dailygift.get_manager() is m


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def test_ui_daily_gift_endpoints(iso: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from buddy_proxy import __main__ as mod
    monkeypatch.setenv("BUDDY_PROXY_ADMIN_OPEN", "1")
    a = _acct("a@x.com", "1")
    _install_routes(monkeypatch, {"1": True})
    with TestClient(mod.app) as c:
        st = c.get("/ui/api/runninghub/daily-gift").json()["gift"]
        assert st["running"] is True, "TestClient 的 lifespan 应该把循环起起来"
        assert st["day"] == dailygift.site_day_key()
        r = c.post("/ui/api/runninghub/daily-gift/claim", json={}).json()
        assert r["claimed"] == [a["account_id"]]
        assert r["gift"]["accounts"][a["account_id"]]["day"] == dailygift.site_day_key()
        assert "password" not in json.dumps(r) and "access_token" not in json.dumps(r)


def test_refresh_balance_does_not_claim_behind_manager(iso: Path, monkeypatch: pytest.MonkeyPatch):
    """领积分收口到 manager：refresh_balance 默认不 claim，否则徽标显示「待领取」而积分已领。"""
    a = _acct("a@x.com", "1")
    rec = _install_routes(monkeypatch, {"1": True})
    acc = store.get_account(a["account_id"])
    asyncio.run(runner.refresh_balance(WebAccountSession(acc)))
    assert [c for c in rec.calls if c[0] == "/api/welfare/daily-gift/claim"] == [], "refresh_balance 不该自己 claim"
    asyncio.run(dailygift.DailyGiftManager().tick())
    assert [c for c in rec.calls if c[0] == "/api/welfare/daily-gift/claim"], "manager 才是领的那条路"
