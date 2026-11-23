"""code 11158（账号套餐不支持该模型）要换号，但只冷却「账号 + 模型」这一格。

## 背景

2026-11-24 ``glm-5.4`` 上线后，7 个 CodeBuddy CN 账号逐个直连上游实测：

    400 {"code":11158,"msg":"model [glm-5.4] is not allowed for user type
         [personal_free_user]"}

6 个 ``personal_free_user`` 账号全回这个码，只有付费账号（``0731``）返回 200。

## 为什么这是个真 bug

``_is_account_error`` 只认 401/429，11158 是 400 → 不换号 → 直接透传给客户端。
于是**只要付费账号没排在第一位**（顺位由登录顺序决定，实测它是第 8 个），裸名
``glm-5.4`` 就永远 400，用户看到的是「模型明明在目录里，却一直报账号类型不支持」。
这不是模型不存在，也不是请求写错——同一个请求换个账号就能过。

## 为什么只冷却这一格

11158 否定的是「该账号 + 该模型」的授权，账号本身完全健康：同账号的
``glm-5.3`` / ``deepseek-v4.1-flash`` 实测照常 200。按**账号**冷却会重演 6004
那次事故——6 个号全冷 5 分钟，连别的模型都用不了。

与 11128 的区别：11128 是「这条通道不接受这个客户端」（换号无用、整通道该歇），
11158 是「这个账号没有这个模型的权限」（换号正是解法）。
"""

from __future__ import annotations

import pytest

from buddy_proxy.codebuddy_provider import failover
from buddy_proxy.codebuddy_provider.provider import (
    _MODEL_NOT_ALLOWED_CODES,
    _is_account_error,
    _is_model_not_allowed,
)


@pytest.fixture(autouse=True)
def _clean():
    failover._tracker.clear_all()
    yield
    failover._tracker.clear_all()


def _exc(status: int, detail):
    from fastapi import HTTPException
    return HTTPException(status_code=status, detail=detail)


def _err_11158(model: str = "glm-5.4") -> str:
    """按 pipeline.stream_upstream 的真实形状构造错误 detail。

    openai 协议下错误帧是 ``{"error": {... "code": 400, "details": "<上游原文>"}}``
    ——外层 ``code`` 是 HTTP 状态码，业务码 11158 藏在**已转义**的 details 里。
    这个形状必须照抄，否则测的就不是真实链路（`_upstream_error_code` 的整套
    解包逻辑正是为它写的）。
    """
    import json
    return json.dumps({
        "error": {
            "message": "Upstream API error (HTTP 400)",
            "type": "upstream_error",
            "code": 400,
            "details": json.dumps({
                "code": 11158,
                "msg": f"model [{model}] is not allowed for user type [personal_free_user]",
            }),
        }
    })


# --- 错误码识别 -------------------------------------------------------------


def test_11158_is_model_not_allowed():
    e = _exc(400, _err_11158())
    assert _is_model_not_allowed(e) is True
    # 关键：400 本身不算账号级错误——换号由 11158 这条独立规则负责
    assert _is_account_error(e) is False


def test_11158_code_registered():
    assert 11158 in _MODEL_NOT_ALLOWED_CODES


def test_11158_only_on_400():
    """只认 400：别的状态码带着 11158 字样不该被当成换号信号。"""
    for status in (401, 429, 500):
        assert _is_model_not_allowed(_exc(status, _err_11158())) is False


def test_other_400_is_not_model_not_allowed():
    """普通 400（参数写错）换号没用，必须原样上抛。"""
    import json
    e = _exc(400, json.dumps({
        "error": {"message": "bad request", "type": "upstream_error", "code": 400,
                  "details": json.dumps({"code": 11101, "msg": "invalid param"})},
    }))
    assert _is_model_not_allowed(e) is False


def test_11128_channel_block_is_not_model_not_allowed():
    """渠道风控 11128 是另一类：换号无用、整通道该歇，别混进这条规则。"""
    import json
    e = _exc(400, json.dumps({
        "error": {"message": "Upstream API error (HTTP 400)", "type": "upstream_error",
                  "code": 400,
                  "details": json.dumps({"code": 11128,
                                         "msg": "Illegal API invocation"})},
    }))
    assert _is_model_not_allowed(e) is False


# --- 冷却语义 ---------------------------------------------------------------


def test_11158_cools_only_the_account_model_pair():
    """只冷却 (账号, 模型)：账号本身和同账号的其它模型都不受影响。"""
    acct, model = "acct-free", "glm-5.4"
    failover.mark_model_cooldown(acct, model, reason="HTTP 400 账号套餐不支持该模型")

    assert failover.model_cooldown_left(acct, model)[0] > 0
    # 账号级冷却必须是 0——否则会连带封掉同账号的 glm-5.3 / deepseek
    assert failover.cooldown_left(acct)[0] == 0
    assert failover.model_cooldown_left(acct, "glm-5.3")[0] == 0


def test_11158_does_not_block_other_accounts(monkeypatch):
    """一个免费号被标记后，付费号仍要能被选中——这正是修复的目的。"""
    from buddy_proxy.codebuddy_provider.credentials import AccountRef

    accts = [AccountRef(id="free", uid="u1", nickname="free", priority=0, added_at=1),
             AccountRef(id="paid", uid="u2", nickname="paid", priority=1, added_at=2)]
    monkeypatch.setattr(failover, "list_accounts", lambda: accts)
    failover.mark_model_cooldown("free", "glm-5.4")

    got = [a.id for a in failover.available_accounts("cn", model="glm-5.4")]
    assert got == ["paid"], "免费号被跳过，付费号必须留下"

    # 换模型：两个都可用（免费号只是没这个模型的权限）
    got = [a.id for a in failover.available_accounts("cn", model="glm-5.3")]
    assert got == ["free", "paid"]


# --- forward 换号循环（真跑一遍，不是只测判定函数）-------------------------


def _setup_free_then_paid(monkeypatch):
    """账号顺位：免费号在前、付费号在后——正是线上出事的那种排法。"""
    from buddy_proxy.codebuddy_provider import credentials as creds

    free = creds.AccountRef(id="free", uid="uid-free", nickname="free",
                            priority=0, added_at=1000)
    paid = creds.AccountRef(id="paid", uid="uid-paid", nickname="paid",
                            priority=1, added_at=2000)
    monkeypatch.setattr(failover, "list_accounts", lambda: [free, paid])
    monkeypatch.setattr(creds, "list_accounts", lambda: [free, paid])

    def _token(aid, **kw):
        return f"tok-{aid}", {"account_id": aid, "token": f"tok-{aid}",
                              "refresh_token": "rt", "uid": f"uid-{aid}",
                              "nickname": aid, "machine_id": "m",
                              "expires_at_ms": 10**13}
    monkeypatch.setattr(creds, "ensure_account_token", _token)
    return free, paid


def test_forward_fails_over_from_free_account_to_paid(monkeypatch):
    """免费号 11158 → 换到付费号正常出流，客户端一个错误字节都看不到。

    这条是本次修复的核心断言：没有它，``glm-5.4`` 在线上永远 400——付费号
    排在免费号后面，而 400 不触发换号。
    """
    import asyncio
    import json as _json

    import buddy_proxy.codebuddy_provider as cbp
    from buddy_proxy.codebuddy_provider.provider import CodeBuddyProvider
    from types import SimpleNamespace
    from buddy_proxy.core import state as st

    monkeypatch.setattr(st, "proxy_state", SimpleNamespace(
        enable_desensitize=False, logger=None, write_log=lambda *a, **k: None))

    free, paid = _setup_free_then_paid(monkeypatch)
    seen: list[str] = []

    def _stream(url, headers, body, protocol, original):
        uid = headers.get("X-User-Id", "")
        seen.append(uid)

        async def _gen():
            if uid == "uid-free":
                details = _json.dumps({"code": 11158, "msg": (
                    "model [glm-5.4] is not allowed for user type [personal_free_user]")})
                frame = {"error": {"message": "Upstream API error (HTTP 400)",
                                   "type": "upstream_error", "code": 400,
                                   "details": details}}
                yield f"data: {_json.dumps(frame)}\n\n".encode()
                return
            yield b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
            yield b"data: [DONE]\n\n"
        return _gen()

    monkeypatch.setattr(cbp, "stream_upstream", _stream)

    async def _run():
        resp = await CodeBuddyProvider().forward(
            {"model": "glm-5.4", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]},
            "openai", None)
        out = b""
        async for chunk in resp.body_iterator:
            out += chunk
        return out

    body = asyncio.run(_run())
    assert seen == ["uid-free", "uid-paid"], "免费号被拒后必须换到付费号"
    assert b'"content":"hi"' in body
    assert b"11158" not in body, "错误帧不能漏给客户端"
    # 只冷却这一格，账号本身不算坏
    assert failover.model_cooldown_left("free", "glm-5.4")[0] > 0
    assert failover.cooldown_left("free")[0] == 0


# --- 目录收录的边界 ---------------------------------------------------------


def test_glm54_and_glm53_both_stay_in_trae_catalog():
    """glm-5.4 与 glm-5.3 家族必须**并存**（用户 2026-11-24 明确要求）。

    早期版本这里是反的：当时以为 trae 侧 glm-5.4 没权限（1005），
    写进 ``MODEL_IDS`` 只会触发代际收敛、把能用的 glm-5.3 挤掉。后来查明
    1005 是请求体带 ``config_name`` 导致的（见 ``test_trae_glm54_config_name``），
    5.4 实际可用，于是改为收录 + 给 glm 家族设代际下限
    （``_GENERATION_FLOOR``）保住 5.3。

    这条测试锁住「两代都在」，防止有人顺手把下限删了。
    """
    from buddy_proxy.trae.config import MODEL_IDS
    from buddy_proxy.trae_provider import TraeProvider

    assert "glm-5.3" in MODEL_IDS, "能用的 glm-5.3 必须在底表里"
    assert "glm-5.4" in MODEL_IDS, "5.4 实测可用，也要在底表里"

    ids = {m["id"] for m in TraeProvider().models()}
    assert "glm-5.4" in ids, "5.4 不能被自己的代际收敛藏掉"
    for kept in ("glm-5.3", "glm-5.3-flash", "glm-5.3-flashx"):
        assert kept in ids, f"{kept} 被 5.4 顶掉了（_GENERATION_FLOOR 失效）"
    # 底线之下的老代际仍照旧被收敛（用户 2026-10-25 点名过 glm-5.1/5.2 没用）
    assert "glm-5.2" not in ids and "glm-5.1" not in ids



def test_all_accounts_denied_reports_model_not_allowed_not_cooldown(monkeypatch):
    """全部账号都没权限时，报错要说明「模型不可用」，不能说成「冷却中」。

    这条是 review 时发现的：``cooldown_report()`` 只查**账号级**键，而 11158
    打的是 ``(账号,模型)`` 复合键 → report 是空串 → 旧文案输出

        「所有账号均在冷却中：；额度冷却到点自动恢复」

    空理由 + 错误指引。用户会以为等一会儿就好，可这是**授权**问题，等到天亮
    也不会恢复。glm-5.4 上线后免费账号第二次请求必踩这条路。
    """
    import asyncio

    import buddy_proxy.codebuddy_provider as cbp
    from buddy_proxy.codebuddy_provider.provider import CodeBuddyProvider
    from types import SimpleNamespace
    from buddy_proxy.core import state as st

    monkeypatch.setattr(st, "proxy_state", SimpleNamespace(
        enable_desensitize=False, logger=None, write_log=lambda *a, **k: None))

    _setup_free_then_paid(monkeypatch)
    # 两个账号都已因 11158 被标记（等价于「上一次请求把号都试过了」）
    failover.mark_model_cooldown("free", "glm-5.4")
    failover.mark_model_cooldown("paid", "glm-5.4")
    assert failover.available_accounts("cn", model="glm-5.4") == []

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        asyncio.run(CodeBuddyProvider().forward(
            {"model": "glm-5.4", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]},
            "openai", None))

    detail = ei.value.detail["error"]
    assert detail["type"] == "model_not_allowed", "不能再说成 rate_limit"
    assert "11158" in detail["message"]
    assert "冷却" not in detail["message"].split("这不是")[0] or "不是额度冷却" in detail["message"]
    assert "；额度冷却到点自动恢复" not in detail["message"], "别给会等错方向的指引"


def test_mixed_denied_and_cooldown_reports_both(monkeypatch):
    """混合场景（付费号额度耗尽 + 免费号无权限）两种原因都要说。

    review 提的点：只报冷却会让用户以为「充钱/等待」能解决，可权限那部分
    永远好不了。glm-5.4 上线后这是常态——免费号第一次请求就被打成复合冷却，
    付费号又常常额度见底。
    """
    import asyncio

    from fastapi import HTTPException
    from types import SimpleNamespace
    from buddy_proxy.core import state as st
    from buddy_proxy.codebuddy_provider.provider import CodeBuddyProvider

    monkeypatch.setattr(st, "proxy_state", SimpleNamespace(
        enable_desensitize=False, logger=None, write_log=lambda *a, **k: None))

    _setup_free_then_paid(monkeypatch)
    failover.mark_model_cooldown("free", "glm-5.4")   # 权限
    failover.mark_cooldown("paid", quota=True)         # 额度耗尽

    with pytest.raises(HTTPException) as ei:
        asyncio.run(CodeBuddyProvider().forward(
            {"model": "glm-5.4", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]},
            "openai", None))

    msg = ei.value.detail["error"]["message"]
    assert "11158" in msg, "权限原因必须出现"
    assert "额度冷却" in msg, "额度原因也必须出现（不能只报一种）"
    assert ei.value.detail["error"]["type"] == "model_not_allowed"
