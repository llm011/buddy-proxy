"""copilot 通道的接线契约（不碰网络、不打上游）。

钉住几件一旦改坏就会静默退化的事：

- provider 已注册成 ``copilot``，并尊重 ``--copilot`` / ``COPILOT_ENABLED``；
- 上游只打 ``api.githubcopilot.com`` + ``api.github.com`` 两个域名（凭据只
  该往这两处送）；
- 必需头齐全（``copilot-integration-id`` / editor 版本 / ``X-Initiator``），
  缺了上游会当非官方客户端拒；
- 失败分类：模型级拒绝只冷却模型、403 也认；账号级的 401/403 提示里给的是
  GitHub login（不是哈希派生的 ``acct-xxxx``）；
- 积分换算 ``total_nano_aiu`` → ``credit`` 能被 ``normalize_usage`` 读到。
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from buddy_proxy.copilot import failover
from buddy_proxy.copilot import upstream
from buddy_proxy.copilot.credentials import AccountRef
from buddy_proxy.copilot.provider import CopilotProvider, _auth_detail
from buddy_proxy.core import settings as settings_mod
from buddy_proxy.core.metrics import normalize_usage


@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    failover._cooldowns.clear()
    yield
    failover._cooldowns.clear()


def _acct(login: str = "jsongo") -> AccountRef:
    return AccountRef(id=login, name=login, priority=0, added_at=0)


# ---------------------------------------------------------------------------
# 注册 / 开关
# ---------------------------------------------------------------------------

def test_provider_identity():
    p = CopilotProvider()
    assert p.id == "copilot"
    assert p.route_priority == 40


def test_copilot_is_a_known_provider():
    assert "copilot" in settings_mod.KNOWN_PROVIDER_IDS
    assert settings_mod.PROVIDER_ENABLE_HINTS["copilot"]


def test_cli_flag_and_env_default():
    """``--copilot`` 的默认值只认 ``COPILOT_ENABLED=1``（关着就别偷偷起通道）。"""
    src = (pathlib.Path(__file__).parents[1]
           / "src/buddy_proxy/__main__.py").read_text("utf-8")
    assert 'COPILOT_ENABLED", "") == "1"' in src
    assert "if args.copilot:" in src


# ---------------------------------------------------------------------------
# 上游域名 / 请求头
# ---------------------------------------------------------------------------

def test_upstream_hosts_are_gateway_and_github():
    assert upstream.API_BASE == "https://api.githubcopilot.com"
    assert upstream.GITHUB_BASE == "https://api.github.com"
    for path in (upstream.PATH_CHAT, upstream.PATH_RESPONSES,
                 upstream.PATH_MESSAGES, upstream.PATH_MODELS):
        assert path.startswith("/")


def test_upstream_headers_carry_required_identity():
    h = upstream.upstream_headers("tok", initiator="agent", vision=True,
                                  stream=True)
    assert h["Authorization"] == "Bearer tok"
    assert h["copilot-integration-id"] == "vscode-chat"
    assert h["editor-version"].startswith("vscode/")
    assert h["editor-plugin-version"].startswith("copilot-chat/")
    assert h["X-Initiator"] == "agent"
    assert h["Copilot-Vision-Request"] == "true"
    assert h["Accept"] == "text/event-stream"


def test_anthropic_headers_add_version():
    assert upstream.anthropic_headers("tok")["anthropic-version"] \
        == "2023-06-01"


def test_github_headers_use_rest_version():
    h = upstream.github_headers("tok")
    assert h["X-GitHub-Api-Version"] != upstream.upstream_headers("tok").get(
        "x-github-api-version")


# ---------------------------------------------------------------------------
# 失败分类
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code,err,msg", [
    (400, "model_not_supported", ""),
    (400, "no_available_model_endpoints", ""),
    (400, "", "Model not supported for this account"),
    (400, "", "no model endpoints available"),
    (403, "", "no model endpoints available"),
])
def test_model_reject_variants(code, err, msg):
    assert failover.is_model_reject(code, err, msg)


@pytest.mark.parametrize("code,err", [(401, ""), (403, ""), (429, ""),
                                      (500, "model_not_supported")])
def test_not_model_reject(code, err):
    assert not failover.is_model_reject(code, err)


def test_quota_signal():
    assert failover.is_quota_signal(429)
    assert failover.is_quota_signal(400, "rate_limit_exceeded")
    assert not failover.is_quota_signal(401)


def test_provider_forwards_402_to_exhausted_marker(monkeypatch):
    """转发路径接线：402 走 mark_exhausted（标记下线），不是 5 分钟 quota 冷却。

    退化护栏：若有人把 402 分支改回 mark_cooldown(quota=True)，耗尽标记不再
    写入、账号又开始每 5 分钟白撞——本测试当场红。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "src/buddy_proxy/copilot/provider.py").read_text(encoding="utf-8")
    assert "is_exhausted_signal(resp.status_code, code, message)" in src
    assert "failover.mark_exhausted(" in src
    # 额度查询回来时解除标记
    assert "clear_exhausted_if_recovered" in src
    assert "usage_has_quota(data)" in src


def test_auth_detail_prefers_login():
    detail = _auth_detail(401, "Bad credentials",
                          AccountRef(id="acct-1a2b", name="jsongo",
                                     priority=0, added_at=0))
    assert "gh auth login --user jsongo" in detail
    assert "acct-1a2b" not in detail


def test_auth_detail_falls_back_to_id():
    detail = _auth_detail(403, "", AccountRef(id="acct-1a2b", name="",
                                              priority=0, added_at=0))
    assert "acct-1a2b" in detail


# ---------------------------------------------------------------------------
# 积分 / token
# ---------------------------------------------------------------------------

def test_credit_and_cache_tokens_reach_normalize_usage():
    usage = upstream.normalize_billing_usage({
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        "copilot_usage": {"total_nano_aiu": 2_500_000_000},
        "prompt_tokens_details": {"cached_tokens": 4},
        # Copilot 把缓存写入记在 copilot_usage.token_details 里，不是顶层的
        # cache_creation_input_tokens——normalize_billing_usage 要负责搬过来。
        "copilot_usage": {
            "total_nano_aiu": 2_500_000_000,
            "token_details": [
                {"token_type": "input", "token_count": 10},
                {"token_type": "cache_write", "token_count": 3},
            ],
        },
    })
    assert usage is not None
    norm = normalize_usage(usage)
    assert norm["credit"] == pytest.approx(2.5)
    # 全量输入 = 未命中 10 + 命中 4（prompt 含缓存，OpenAI 口径）+ 写入 3
    assert norm["prompt_tokens"] == 10 + 4 + 3
    assert norm["completion_tokens"] == 5
    assert norm["cached_tokens"] == 4


def test_missing_copilot_usage_reports_zero_credit():
    usage = upstream.normalize_billing_usage({"prompt_tokens": 1,
                                              "completion_tokens": 1})
    assert usage["credit"] == 0.0


# ---------------------------------------------------------------------------
# 额度面板
# ---------------------------------------------------------------------------

def test_parse_user_usage_builds_quota_rows():
    rows = upstream.parse_user_usage({
        "copilot_plan": "individual",
        "quota_reset_date_utc": "2026-12-01T00:00:00.000Z",
        "quota_snapshots": {
            "premium_interactions": {
                "entitlement": 300, "remaining": 120, "percent_remaining": 40.0,
                "quota_id": "premium_interactions",
            },
            "completions": {"unlimited": True},
        },
    })
    assert [r["label"] for r in rows] == ["Premium 交互"]  # 无限额度不画进度条
    assert rows[0]["remaining"] == 120
    assert rows[0]["total"] == 300
    assert rows[0]["used"] == 180
    assert rows[0]["unit"] == "次"
    # ``percent`` 是**已用**（进度条 / 颜色按它分档），上游只给 remaining。
    # 按剩余算就会「剩得越多越红」，正好反了。
    assert rows[0]["percent"] == 60.0
    # 顶层 quota_reset_date_utc 必须落在 reset_ts 上（不能被 snapshot 里的 0 占位遮掉）
    assert rows[0]["reset_ts"] == pytest.approx(1796083200.0)


def test_parse_user_usage_ignores_zero_quota_reset_at_placeholder():
    """真实响应里 quota_snapshots.*.quota_reset_at 常给 0，不能盖掉顶层重置日期。"""
    rows = upstream.parse_user_usage({
        "quota_reset_date": "2026-12-01",
        "quota_reset_date_utc": "2026-12-01T00:00:00.000Z",
        "quota_snapshots": {
            "chat": {"unlimited": True, "quota_reset_at": 0},
            "premium_interactions": {
                "entitlement": 1500, "remaining": -49, "quota_reset_at": 0,
            },
        },
    })
    assert rows[0]["reset_ts"] == pytest.approx(1796083200.0)


def test_parse_user_usage_falls_back_to_quota_reset_date():
    """仅有顶层 quota_reset_date（YYYY-MM-DD）时按 UTC 零点解析。"""
    rows = upstream.parse_user_usage({
        "quota_reset_date": "2026-12-01",
        "quota_snapshots": {
            "premium_interactions": {"entitlement": 300, "remaining": 200},
        },
    })
    assert rows[0]["reset_ts"] == pytest.approx(1796083200.0)


def test_parse_user_usage_percent_is_used_not_remaining():
    """剩 1491 / 1500（用户 2026-11-16 截图）：已用 0.6%，进度条应几乎空。"""
    rows = upstream.parse_user_usage({
        "quota_snapshots": {
            "premium_interactions": {"entitlement": 1500, "remaining": 1491},
        },
    })
    assert rows[0]["percent"] == 0.6
    assert rows[0]["used"] == 9


def test_parse_user_usage_percent_unknown_without_remaining():
    """只回了容量、没回用量：percent 留 None，别当成「一点没用」。"""
    rows = upstream.parse_user_usage({
        "quota_snapshots": {"premium_interactions": {"entitlement": 300}},
    })
    assert rows[0]["total"] == 300
    assert rows[0]["percent"] is None


def test_health_surfaces_catalog_error():
    p = CopilotProvider()
    p._refresh_error = "boom"
    assert p.health()["catalog_error"] == "boom"


# ---------------------------------------------------------------------------
# 转发：403 只冷却模型，不钉住整号
# ---------------------------------------------------------------------------

def test_403_model_reject_cools_only_model(monkeypatch):
    p = CopilotProvider()
    acct = _acct()
    import buddy_proxy.copilot.provider as prov_mod
    # 账号表/登录态都替身掉：本用例只验「403 落到哪一档冷却」。
    p.ensure_auth = lambda: None
    monkeypatch.setattr(prov_mod.failover, "available_accounts",
                        lambda m: [acct])

    from buddy_proxy.copilot.catalog import ModelEntry
    entry = ModelEntry(id="m1", name="M1", vendor="openai",
                       endpoint=upstream.PATH_RESPONSES,
                       endpoints=(upstream.PATH_RESPONSES,))
    monkeypatch.setattr(p, "_entry_for", lambda m: entry)

    def _tok(acct_id):  # 同步函数：provider 走 asyncio.to_thread 调
        return "tok", {}

    monkeypatch.setattr(prov_mod, "ensure_account_token", _tok)

    class _Resp:
        status_code = 403
        headers: dict = {}
        _detail = {"error": {"message": "no model endpoints available",
                             "code": "no_available_model_endpoints"}}

        async def aclose(self):
            return None

    class _Client:
        # httpx 里 build_request 是同步、send 是协程，照抄签名别改。
        def build_request(self, *a, **kw):
            return object()

        async def send(self, *a, **kw):
            return _Resp()

    async def _client():
        return _Client()

    monkeypatch.setattr(p, "_get_client", _client)

    async def _read_error(resp):
        return resp._detail

    async def _no_models():
        return None

    monkeypatch.setattr(p, "ensure_models", _no_models)

    monkeypatch.setattr(prov_mod, "_read_error", _read_error)

    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        asyncio.run(p.forward({"model": "copilot/m1", "messages": []},
                              "openai", None))

    assert failover.model_cooldown_left(acct.id, "m1")[0] > 0
    assert failover.cooldown_left(acct.id)[0] == 0.0
