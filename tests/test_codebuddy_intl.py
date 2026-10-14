"""codebuddy 海外版（codebuddyintl）测试：region 维度贯穿凭据 store / failover /
provider / intl 子类。CODEBUDDY_STATE_DIR 由 conftest autouse 隔离到 tmp_path。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from buddy_proxy.codebuddy_provider import credentials as creds
from buddy_proxy.codebuddy_provider import failover
from buddy_proxy.codebuddy_provider.forward import _resolve_auto
from buddy_proxy.codebuddy_provider.provider import (
    CodeBuddyProvider,
    _ensure_leading_system,
)
from buddy_proxy.codebuddy_provider.intl_provider import (
    CodeBuddyIntlProvider,
    intl_enabled,
)


def _cred(account_id: str, *, region: str = "cn", **over) -> dict:
    cred = {
        "account_id": account_id,
        "token": f"at-{account_id}",
        "refresh_token": f"rt-{account_id}",
        "expires_at_ms": int(time.time() * 1000) + 3600_000,
        "uid": f"uid-{account_id}",
        "nickname": f"nick-{account_id}",
        "enterprise_id": "ent-1",
        "department_info": "",
        "domain": "https://copilot.tencent.com",
        "machine_id": f"mach-{account_id}",
        "source": "state",
        "region": region,
    }
    cred.update(over)
    return cred


def _intl_cred(uid: str, **over) -> dict:
    """海外版 cred（真实登录形状：session_to_cred 不产 account_id，靠派生）。"""
    cred = _cred(f"placeholder-{uid}", region="global", uid=uid)
    cred["account_id"] = ""
    cred.update(over)
    return cred


# ---------------------------------------------------------------------------
# account_id 派生：海外版 intl- 前缀（防跨区 uid 撞车）
# ---------------------------------------------------------------------------

def test_derive_account_id_intl_prefix():
    assert creds.derive_account_id({"uid": "u123", "token": "t", "region": "global"}) == "intl-u123"
    # 无 uid：digest 兜底也带前缀
    assert creds.derive_account_id({"token": "abc", "region": "global"}) == "intl-acct-ba7816bf8f01"
    # CN 形状不变
    assert creds.derive_account_id({"uid": "u123", "token": "t"}) == "u123"
    # 已有合法 id 原样保留（含海外老格式）
    assert creds.derive_account_id({"account_id": "intl-x", "uid": "y", "region": "global"}) == "intl-x"


# ---------------------------------------------------------------------------
# endpoint(region)：CN / 海外双端点
# ---------------------------------------------------------------------------

def test_endpoint_by_region(monkeypatch):
    assert creds.endpoint() == "https://copilot.tencent.com"
    assert creds.endpoint("cn") == "https://copilot.tencent.com"
    assert creds.endpoint("global") == "https://www.codebuddy.ai"
    monkeypatch.setenv("CODEBUDDY_INTL_ENDPOINT", "https://intl.example.com")
    assert creds.endpoint("global") == "https://intl.example.com"
    assert creds.endpoint("cn") == "https://copilot.tencent.com"  # env 互不串


# ---------------------------------------------------------------------------
# 双区共存：同 uid 不并号、region 落索引
# ---------------------------------------------------------------------------

def test_cross_region_same_uid_not_merged():
    a = creds.save_account_cred(_cred("u100"))
    b = creds.save_account_cred(_intl_cred("uid-u100"))
    assert a.id == "u100" and b.id == "intl-uid-u100"
    accts = creds.list_accounts()
    assert len(accts) == 2, "同 uid 跨区不得互并"
    assert {(x.id, x.region) for x in accts} == {("u100", "cn"), ("intl-uid-u100", "global")}


def test_region_roundtrip_index():
    creds.save_account_cred(_cred("u100"))
    ref = creds.save_account_cred(_intl_cred("uid-u200"))
    assert ref.region == "global"
    # upsert（重登）不丢 region
    creds.save_account_cred(_intl_cred("uid-u200", token="at-new"))
    again = [a for a in creds.list_accounts() if a.id == ref.id][0]
    assert again.region == "global"


# ---------------------------------------------------------------------------
# failover：available_accounts / display_index / accounts_status 按区
# ---------------------------------------------------------------------------

def test_available_accounts_region_filter():
    creds.save_account_cred(_cred("u100"))
    creds.save_account_cred(_intl_cred("uid-u200"))
    assert sorted(a.id for a in failover.available_accounts()) == ["intl-uid-u200", "u100"]
    assert [a.id for a in failover.available_accounts("cn")] == ["u100"]
    assert [a.id for a in failover.available_accounts("global")] == ["intl-uid-u200"]


def test_display_index_region_scoped():
    creds.save_account_cred(_cred("u100"))
    creds.save_account_cred(_cred("u200"))
    creds.save_account_cred(_intl_cred("uid-u300"))
    # 全量：海外号排在 CN 之后（历史行为）
    assert failover.display_index() == {"u100": 1, "u200": 2, "intl-uid-u300": 3}
    # 区内：各区各自从 1 编号，与各自面板快照对得上
    assert failover.display_index("cn") == {"u100": 1, "u200": 2}
    assert failover.display_index("global") == {"intl-uid-u300": 1}


def test_accounts_status_region():
    creds.save_account_cred(_cred("u100"))
    creds.save_account_cred(_intl_cred("uid-u200"))
    cn = failover.accounts_status("cn")
    gl = failover.accounts_status("global")
    assert [a["id"] for a in cn["accounts"]] == ["u100"]
    assert [a["id"] for a in gl["accounts"]] == ["intl-uid-u200"]
    assert cn["accounts"][0]["region"] == "cn"
    assert gl["accounts"][0]["region"] == "global"
    assert cn["accounts"][0]["index"] == 1 and gl["accounts"][0]["index"] == 1


# ---------------------------------------------------------------------------
# reorder_accounts(region)：区内分治
# ---------------------------------------------------------------------------

def test_reorder_region_scoped():
    creds.save_account_cred(_cred("u100"))
    creds.save_account_cred(_cred("u200"))
    creds.save_account_cred(_cred("u300", region="global"))
    creds.save_account_cred(_cred("u400", region="global"))
    out = creds.reorder_accounts(["u400", "u300"], region="global")
    pri = {a.id: a.priority for a in out}
    assert pri["u400"] < pri["u300"], "global 区内部换序"
    assert pri["u100"] < pri["u200"], "CN 区相对顺位不动"
    # global 只占自己区提交的 id：提交 CN 区 id 会拒绝
    with pytest.raises(ValueError):
        creds.reorder_accounts(["u100"], region="global")
    with pytest.raises(ValueError):
        creds.reorder_accounts(["u100", "u200"], region="global")
    # region=None：历史全量契约不变
    with pytest.raises(ValueError):
        creds.reorder_accounts(["u100", "u200"])  # 缺 global 两号


# ---------------------------------------------------------------------------
# intl provider：intl_enabled / ensure_auth / 区内账号
# ---------------------------------------------------------------------------

def test_intl_enabled_and_provider_region():
    assert intl_enabled() is False
    p = CodeBuddyIntlProvider()
    assert p.id == "codebuddyintl" and p._region == "global"
    assert p._quota_tag == "CodeBuddy 海外版"
    models = p.models()
    assert [m["id"] for m in models] == [
        "codebuddyintl/hy4-preview",
        "codebuddyintl/gpt-5.6-sol",
        "codebuddyintl/gpt-5.6-terra",
        "codebuddyintl/gpt-5.6-luna",
        "codebuddyintl/gemini-3.5-flash",
        "codebuddyintl/glm-5.3",
        "codebuddyintl/kimi-k3",
    ]
    assert all(m["images"] and m["tool_call"] for m in models)
    # 海外目录虽公开，但只能用完整前缀路由；裸名不能抢默认 CN CodeBuddy。
    assert p.accepts_model("codebuddyintl/hy4-preview") is True
    assert p.accepts_model("hy4-preview") is False
    assert p.accepts_model("gpt-5.6-terra") is False
    assert _resolve_auto(
        SimpleNamespace(providers={"codebuddyintl": p}), "hy4-preview"
    ) is None
    assert {m["id"].split("/", 1)[1]: m["credits"] for m in models} == {
        "hy4-preview": "x0.00 credits",
        "gpt-5.6-sol": "x3.47 credits",
        "gpt-5.6-terra": "x1.39 credits",
        "gpt-5.6-luna": "x0.14 credits",
        "gemini-3.5-flash": "x0.99 credits",
        "glm-5.3": "x0.79 credits",
        "kimi-k3": "x1.62 credits",
    }
    assert not {"hy3", "gpt-5.5", "gpt-5.4", "gpt-5.3-codex",
                "glm-5.2", "kimi-k2.6"} & {
                    m["id"].split("/", 1)[1] for m in models
                }
    # 没有 global 账号：ensure_auth 拒绝启动
    creds.save_account_cred(_cred("u100"))
    with pytest.raises(HTTPException) as ei:
        p.ensure_auth()
    assert ei.value.status_code == 401
    assert "buddy login codebuddy --region global" in str(ei.value.detail)
    assert intl_enabled() is False
    creds.save_account_cred(_intl_cred("uid-u200"))
    assert intl_enabled() is True
    p.ensure_auth()  # 有海外号即通过
    assert [a.id for a in p._accounts_for_benefits()] == ["intl-uid-u200"]


# ---------------------------------------------------------------------------
# 海外版风控：messages 首条必须是 system（11128）
# ---------------------------------------------------------------------------

def test_ensure_leading_system_injects_when_missing():
    msgs = [{"role": "user", "content": "hi"}]
    out = _ensure_leading_system(msgs)
    assert out[0] == {"role": "system", "content": ""}
    assert out[1:] == msgs  # 原消息原样尾随


def test_ensure_leading_system_noop_when_present():
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "hi"}]
    assert _ensure_leading_system(msgs) is msgs  # 已有 system：原样返回不复制


def test_ensure_leading_system_edge_cases():
    assert _ensure_leading_system([]) == [{"role": "system", "content": ""}]
    assert _ensure_leading_system(None) is None  # 非 list 不碰


@pytest.mark.asyncio
async def test_intl_forward_injects_system(monkeypatch):
    """海外版 forward 必须在转发前给缺 system 的 body 补首块；CN 不动。"""
    from types import SimpleNamespace
    import buddy_proxy.codebuddy_provider.provider as prov
    from buddy_proxy.core import state as st

    monkeypatch.setattr(st, "proxy_state", SimpleNamespace(
        enable_desensitize=False, logger=None, write_log=lambda *a, **k: None))
    creds.save_account_cred(_intl_cred("uid-u200"))
    seen = {}

    async def fake_once(self, upstream_body, protocol, original, account_id, stream):
        from fastapi.responses import JSONResponse
        seen["messages"] = upstream_body["messages"]
        return JSONResponse(content={"ok": True})

    monkeypatch.setattr(prov.CodeBuddyProvider, "_forward_once", fake_once)

    p = CodeBuddyIntlProvider()
    await p.forward({"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
                    "openai")
    assert seen["messages"][0] == {"role": "system", "content": ""}

    # CN 通道：缺 system 也不注入
    creds.save_account_cred(_cred("u100"))
    seen.clear()
    cn = CodeBuddyProvider()
    await cn.forward({"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                     "openai")
    assert seen["messages"][0]["role"] == "user"


def test_refresh_hits_intl_endpoint(monkeypatch):
    ref = creds.save_account_cred(_intl_cred("uid-u200", expires_at_ms=1))  # 已过期 → 强制刷新
    calls = {}

    def fake_post(url, **kwargs):
        calls["url"] = url
        payload = {"data": {"accessToken": "at-new", "refreshToken": "rt-new",
                            "expiresAt": int(time.time() * 1000) + 3600_000}}
        import httpx as _hx
        return _hx.Response(200, json=payload)

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    fresh = creds.refresh_account_cred(creds.load_account_cred(ref.id))
    assert calls["url"].startswith("https://www.codebuddy.ai"), calls["url"]
    assert fresh["token"] == "at-new"
