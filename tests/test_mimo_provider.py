"""MiMo provider 测试（完全离线，不访问小米上游）。

覆盖：
- SSO 两阶段换票的纯函数（client_sign / Phase1 body 解析 / Set-Cookie 解析）
- cookie 库读取（明文 value）
- 凭据解析优先级（API key → 桌面 SSO）
- 模型名改写（mimo-auto → mimo-pro）
- MimoProvider.models / health / ensure_auth / forward（含 SSO 401 重试）

运行：
    .venv/bin/python -m pytest tests/test_mimo_provider.py -v
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from buddy_proxy.mimo import config
from buddy_proxy.mimo import credentials as cred
from buddy_proxy.mimo import sso
from buddy_proxy.mimo.provider import (
    MimoProvider,
    _is_auth_rejection,
    _normalize_model,
)


# ---------------------------------------------------------------------------
# fixture：隔离状态目录 / 环境变量
# ---------------------------------------------------------------------------
@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "buddy-state"
    d.mkdir()
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(d))
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.delenv("MIMO_BASE_URL", raising=False)
    monkeypatch.delenv("MIMO_COOKIE_DB", raising=False)
    monkeypatch.delenv("MIMO_AUTH_JSON", raising=False)
    return d


def _make_cookie_db(path: Path, rows: list[tuple[str, str]]) -> Path:
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE cookies (name TEXT, value TEXT, encrypted_value BLOB)"
        )
        con.executemany(
            "INSERT INTO cookies (name, value, encrypted_value) VALUES (?, ?, ?)",
            [(n, v, b"") for n, v in rows],
        )
        con.commit()
    finally:
        con.close()
    return path


# ---------------------------------------------------------------------------
# client_sign（asar: sha1("nonce=" + nonce + "&" + ssecurity) → base64 → urlencode）
# ---------------------------------------------------------------------------
class TestClientSign:
    def test_with_ssecurity(self):
        # 向量由 asar 公式独立手算：raw = "nonce=1234567890&abcdefghijklmnopqrstuvwxyz012345"
        assert (
            sso.client_sign("1234567890", "abcdefghijklmnopqrstuvwxyz012345")
            == "uKn9SBIeLHP8YuGV4rsdVHv2XOU%3D"
        )

    def test_empty_ssecurity_no_ampersand(self):
        # ssecurity 为空时**不能**拼出 "nonce=42&"，否则 sha1 结果全错
        assert sso.client_sign("42", "") == "5rJxSj8sEKwN%2BJH3nlm4hExMh7U%3D"
        assert sso.client_sign("42", "   ") == "5rJxSj8sEKwN%2BJH3nlm4hExMh7U%3D"

    def test_ssecurity_is_raw_appended(self):
        # 关键差异：是 "&" + ssecurity 本身，不是 "&ssecurity=" + ssecurity
        a = sso.client_sign("1", "SS")
        raw_wrong = "nonce=1&ssecurity=SS"
        import base64
        import hashlib
        import urllib.parse

        wrong = urllib.parse.quote(
            base64.b64encode(hashlib.sha1(raw_wrong.encode()).digest()).decode(),
            safe="",
        )
        assert a != wrong


# ---------------------------------------------------------------------------
# Phase 1 body 解析
# ---------------------------------------------------------------------------
class TestParsePhase1:
    def test_strips_start_prefix(self):
        body = '&&&START&&&{"code":0,"loc":"https://x/step2","ssecurity":"s","nonce":99}'
        data = sso._parse_phase1_body(body)
        assert data["code"] == 0
        assert data["loc"] == "https://x/step2"
        assert data["nonce"] == 99

    def test_nonce_regex_fallback_on_broken_json(self):
        # nonce 是数字字面量；JSON 坏掉时也要能抠出来
        body = '&&&START&&&{"code":0,"nonce":1234567890123,'
        data = sso._parse_phase1_body(body)
        assert data["nonce"] == 1234567890123

    def test_plain_json_without_prefix(self):
        data = sso._parse_phase1_body('{"code":0,"nonce":7}')
        assert data["nonce"] == 7


# ---------------------------------------------------------------------------
# Set-Cookie 解析
# ---------------------------------------------------------------------------
class TestParseSetCookie:
    def _headers(self, cookies: list[str]) -> httpx.Headers:
        return httpx.Headers([("set-cookie", c) for c in cookies])

    def test_plain_service_token(self):
        h = self._headers(["serviceToken=abc123; Path=/; HttpOnly"])
        token, extra = sso._parse_set_cookie_service_token(h, "mimopc")
        assert token == "abc123"
        assert "serviceToken" not in extra

    def test_sid_prefixed_service_token(self):
        h = self._headers(
            ["mimopc_serviceToken=tok456; Path=/", "other=1; Path=/"]
        )
        token, extra = sso._parse_set_cookie_service_token(h, "mimopc")
        assert token == "tok456"
        assert extra == {"other": "1"}

    def test_missing_token(self):
        h = self._headers(["foo=bar; Path=/"])
        token, extra = sso._parse_set_cookie_service_token(h, "mimopc")
        assert token == ""
        assert extra == {"foo": "bar"}


# ---------------------------------------------------------------------------
# cookie 库读取
# ---------------------------------------------------------------------------
class TestLoadAccountCookies:
    def test_reads_plaintext_value(self, state_dir, tmp_path):
        db = _make_cookie_db(
            tmp_path / "Cookies",
            [
                ("passToken", "PT-SECRET"),
                ("userId", "6267360"),
                ("cUserId", "c-uid"),
                ("uLocale", "zh_CN"),
                ("empty", ""),
            ],
        )
        account = sso.load_account_cookies(db)
        assert account is not None
        assert account.pass_token == "PT-SECRET"
        assert account.user_id == "6267360"
        assert account.c_user_id == "c-uid"
        assert account.phase1_cookie() == "passToken=PT-SECRET; userId=6267360; cUserId=c-uid"

    def test_missing_pass_token(self, state_dir, tmp_path):
        db = _make_cookie_db(tmp_path / "Cookies", [("userId", "1")])
        assert sso.load_account_cookies(db) is None

    def test_missing_db(self, state_dir, tmp_path):
        assert sso.load_account_cookies(tmp_path / "nope.db") is None


# ---------------------------------------------------------------------------
# 凭据解析
# ---------------------------------------------------------------------------
class TestResolveApiKey:
    def test_env_wins(self, state_dir, monkeypatch):
        monkeypatch.setenv("MIMO_API_KEY", "env-key")
        monkeypatch.setenv("MIMO_BASE_URL", "https://example.test/v1")
        key, base = cred.resolve_api_key()
        assert key == "env-key"
        assert base == "https://example.test/v1"

    def test_mimocode_auth_json(self, state_dir, tmp_path, monkeypatch):
        auth = tmp_path / "auth.json"
        auth.write_text(
            json.dumps(
                {
                    "xiaomi": {
                        "type": "api",
                        "key": "file-key",
                        "metadata": {"base_url": "https://token-plan-cn.xiaomimimo.com/v1"},
                    }
                }
            )
        )
        monkeypatch.setenv("MIMO_AUTH_JSON", str(auth))
        key, base = cred.resolve_api_key()
        assert key == "file-key"
        assert base == "https://token-plan-cn.xiaomimimo.com/v1"

    def test_state_file_fallback(self, state_dir, monkeypatch):
        monkeypatch.setattr(
            cred, "mimocode_auth_path", lambda: state_dir / "missing-auth.json"
        )
        (state_dir / "mimo_api_key.json").write_text(
            json.dumps({"api_key": "state-key", "base_url": ""})
        )
        key, base = cred.resolve_api_key()
        assert key == "state-key"
        assert base == config.BILLING_API_BASE

    def test_nothing_configured(self, state_dir, monkeypatch):
        monkeypatch.setattr(
            cred, "mimocode_auth_path", lambda: state_dir / "missing-auth.json"
        )
        assert cred.resolve_api_key() == ("", "")


class TestResolveUpstream:
    def test_key_mode(self, state_dir, monkeypatch):
        monkeypatch.setenv("MIMO_API_KEY", "k-123")
        monkeypatch.setenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
        up = asyncio.run(cred.resolve_upstream())
        assert up.mode == "key"
        assert up.chat_url == "https://api.xiaomimimo.com/v1/chat/completions"
        assert up.headers["Authorization"] == "Bearer k-123"
        assert up.headers["X-Mimo-Source"] == config.X_SOURCE_KEY
        assert up.headers["User-Agent"] == config.CHAT_UA

    def test_sso_mode(self, state_dir, monkeypatch):
        monkeypatch.setattr(
            cred, "mimocode_auth_path", lambda: state_dir / "missing-auth.json"
        )
        account = sso.AccountCookies(pass_token="pt", user_id="1", c_user_id="c")
        token = sso.ServiceToken(sid="mimopc", token="st-1", obtained_at=1.0)
        monkeypatch.setattr(cred, "load_account_cookies", lambda: account)

        async def fake_ensure(**kwargs):
            return token

        monkeypatch.setattr(cred, "ensure_service_token", fake_ensure)
        up = asyncio.run(cred.resolve_upstream())
        assert up.mode == "sso"
        assert up.chat_url == "https://mimo-server-cn.xiaomimimo.com/api/route/chat/completions"
        assert "serviceToken=st-1" in up.headers["Cookie"]
        assert "mimopc_serviceToken=st-1" in up.headers["Cookie"]
        assert up.headers["X-Mimo-Source"] == config.X_SOURCE_SSO

    def test_no_credentials(self, state_dir, monkeypatch):
        monkeypatch.setattr(
            cred, "mimocode_auth_path", lambda: state_dir / "missing-auth.json"
        )
        monkeypatch.setattr(cred, "load_account_cookies", lambda: None)
        with pytest.raises(cred.AuthError):
            asyncio.run(cred.resolve_upstream())


# ---------------------------------------------------------------------------
# URL / 模型名
# ---------------------------------------------------------------------------
class TestConfig:
    def test_sso_chat_url(self):
        assert (
            config.sso_chat_url()
            == "https://mimo-server-cn.xiaomimimo.com/api/route/chat/completions"
        )

    def test_key_chat_url(self):
        assert (
            config.key_chat_url("https://api.xiaomimimo.com/v1")
            == "https://api.xiaomimimo.com/v1/chat/completions"
        )

    def test_normalize_model(self):
        assert _normalize_model("mimo-auto") == "mimo-pro"
        assert _normalize_model("MiMo-Auto") == "mimo-pro"
        assert _normalize_model("mimo-v2.6-pro") == "mimo-v2.6-pro"
        assert _normalize_model("mimo-flash") == "mimo-flash"
        # 未知模型原样透传（交给上游报错，别自己吞掉）
        assert _normalize_model("gpt-4o") == "gpt-4o"


# ---------------------------------------------------------------------------
# MimoProvider
# ---------------------------------------------------------------------------
def _resp(status: int, json_body: dict | None = None, text: str = "") -> httpx.Response:
    req = httpx.Request("POST", "https://upstream.test/chat/completions")
    if json_body is not None:
        return httpx.Response(status, json=json_body, request=req)
    return httpx.Response(status, text=text, request=req)


class TestMimoProvider:
    def test_models_are_lowercase(self):
        p = MimoProvider()
        ids = [m["id"] for m in p.models()]
        assert ids
        assert all(i == i.lower() for i in ids)
        assert "mimo-auto" in ids
        assert all(m["owned_by"] == "mimo" for m in p.models())

    def test_models_cover_third_party_aggregator(self):
        """MiMo 是聚合面板（2026-11-01）：目录要平铺第三方模型，别只列自家两条。

        依据是上游 ``GET /api/model/list`` 的 14 条目录剔掉 ASR/TTS/图片后的
        9 条文本模型，与客户端「模型倍率」面板逐条吻合。模型名大小写敏感，
        必须与目录原样一致（``qwen3.8-flash`` 而非 ``Qwen3.8-Flash``）。
        """
        ids = {m["id"] for m in MimoProvider().models()}
        for mid in ("mimo-v2.6-pro", "mimo-v2.6-flash", "deepseek-v4-pro",
                    "deepseek-v4.1-flash", "glm-5.3", "glm-5.3-flash",
                    "kimi-k3", "qwen3.8-flash", "qwen3.8-max"):
            assert mid in ids, mid
        # ASR/TTS/图片模型不是 chat completions 能用的，不许混进目录
        for mid in ("mimo-v2.5-asr", "mimo-v2.5-tts", "doubao-seedream-5.0-pro"):
            assert mid not in ids, mid

    def test_ensure_auth_raises_without_credentials(self, state_dir, monkeypatch):
        monkeypatch.setattr(
            cred, "mimocode_auth_path", lambda: state_dir / "missing-auth.json"
        )
        monkeypatch.setattr(sso, "load_account_cookies", lambda: None)
        with pytest.raises(HTTPException) as ei:
            MimoProvider().ensure_auth()
        assert ei.value.status_code == 401

    def test_health_modes(self, state_dir, monkeypatch):
        p = MimoProvider()
        monkeypatch.setattr(
            cred, "mimocode_auth_path", lambda: state_dir / "missing-auth.json"
        )
        monkeypatch.setattr(sso, "load_account_cookies", lambda: None)
        assert p.health()["auth_mode"] == "none"
        monkeypatch.setenv("MIMO_API_KEY", "k")
        assert p.health()["auth_mode"] == "key"
        monkeypatch.delenv("MIMO_API_KEY")
        monkeypatch.setattr(
            sso,
            "load_account_cookies",
            lambda: sso.AccountCookies("pt", "1"),
        )
        assert p.health()["auth_mode"] == "sso"

    def test_forward_non_stream_rewrites_model(self, state_dir, monkeypatch):
        monkeypatch.setenv("MIMO_API_KEY", "k")
        p = MimoProvider()
        sent: dict = {}

        async def fake_send(client, up, body, stream):
            sent["body"] = body
            sent["stream"] = stream
            sent["url"] = up.chat_url
            return _resp(200, json_body={"id": "c1", "choices": []})

        monkeypatch.setattr(p, "_send", fake_send)
        out = asyncio.run(
            p.forward({"model": "mimo-auto", "messages": [], "stream": False}, "openai")
        )
        assert out.status_code == 200
        assert sent["body"]["model"] == "mimo-pro"
        assert sent["stream"] is False
        assert sent["url"].endswith("/chat/completions")

    def test_forward_strips_internal_fields(self, state_dir, monkeypatch):
        monkeypatch.setenv("MIMO_API_KEY", "k")
        p = MimoProvider()
        sent: dict = {}

        async def fake_send(client, up, body, stream):
            sent["body"] = body
            return _resp(200, json_body={"ok": True})

        monkeypatch.setattr(p, "_send", fake_send)
        asyncio.run(
            p.forward(
                {"model": "mimo-pro", "messages": [], "_trace": "x", "stream": False},
                "openai",
            )
        )
        assert "_trace" not in sent["body"]

    def test_forward_upstream_error_passthrough(self, state_dir, monkeypatch):
        monkeypatch.setenv("MIMO_API_KEY", "k")
        p = MimoProvider()

        async def fake_send(client, up, body, stream):
            return _resp(429, json_body={"error": {"message": "quota"}})

        monkeypatch.setattr(p, "_send", fake_send)
        out = asyncio.run(p.forward({"model": "mimo-pro", "messages": []}, "openai"))
        assert out.status_code == 429
        assert json.loads(out.body)["error"]["message"] == "quota"

    def test_sso_401_triggers_refresh_retry(self, state_dir, monkeypatch):
        """SSO 被拒时强制重换 serviceToken 再试一次（对齐 wrappedFetch）。"""
        monkeypatch.delenv("MIMO_API_KEY", raising=False)
        monkeypatch.setattr(
            cred, "mimocode_auth_path", lambda: state_dir / "missing-auth.json"
        )
        account = sso.AccountCookies("pt", "1")
        # 两个模块**都要**打，因为两条路径的取值方式不同：
        #   - provider.ensure_auth() 里是函数内 `from .sso import load_account_cookies`
        #     → 打 sso 模块才生效
        #   - credentials.resolve_upstream() 用的是模块级 `from .sso import ...`
        #     → 打 cred 模块才生效
        # 只打一个会回落到真实机器状态：CI 上没登录 MiMo 桌面就 401（已踩）。
        monkeypatch.setattr(sso, "load_account_cookies", lambda: account)
        monkeypatch.setattr(cred, "load_account_cookies", lambda: account)

        calls: list[dict] = []

        async def fake_ensure(force: bool = False, **kwargs):
            calls.append({"force": force})
            return sso.ServiceToken(
                sid="mimopc",
                token=f"st-{len(calls)}",
                obtained_at=1.0,
            )

        monkeypatch.setattr(cred, "ensure_service_token", fake_ensure)

        p = MimoProvider()
        sends: list[bool] = []

        async def fake_send(client, up, body, stream):
            sends.append(up.service_token.token if up.service_token else "")
            if len(sends) == 1:
                return _resp(401, json_body={"error": {"message": "expired"}})
            return _resp(200, json_body={"id": "ok"})

        monkeypatch.setattr(p, "_send", fake_send)
        out = asyncio.run(p.forward({"model": "mimo-pro", "messages": []}, "openai"))
        assert out.status_code == 200
        assert calls == [{"force": False}, {"force": True}]
        assert sends == ["st-1", "st-2"]

    def test_api_key_401_does_not_retry(self, state_dir, monkeypatch):
        monkeypatch.setenv("MIMO_API_KEY", "k")
        p = MimoProvider()
        sends: list[int] = []

        async def fake_send(client, up, body, stream):
            sends.append(1)
            return _resp(401, json_body={"error": {"message": "bad key"}})

        monkeypatch.setattr(p, "_send", fake_send)
        out = asyncio.run(p.forward({"model": "mimo-pro", "messages": []}, "openai"))
        assert out.status_code == 401
        assert len(sends) == 1

    def test_sso_membership_403_does_not_retry(self, state_dir, monkeypatch):
        """「未开通会员」是业务拒绝（30012），不能当鉴权失败去重换票。"""
        monkeypatch.delenv("MIMO_API_KEY", raising=False)
        monkeypatch.setattr(
            cred, "mimocode_auth_path", lambda: state_dir / "missing-auth.json"
        )
        # 同上：sso 与 cred 两个模块都要打（两条 import 路径不同）
        account = sso.AccountCookies("pt", "1")
        monkeypatch.setattr(sso, "load_account_cookies", lambda: account)
        monkeypatch.setattr(cred, "load_account_cookies", lambda: account)
        calls: list[dict] = []

        async def fake_ensure(force: bool = False, **kwargs):
            calls.append({"force": force})
            return sso.ServiceToken(sid="mimopc", token="st", obtained_at=1.0)

        monkeypatch.setattr(cred, "ensure_service_token", fake_ensure)

        p = MimoProvider()
        sends: list[int] = []

        async def fake_send(client, up, body, stream):
            sends.append(1)
            return _resp(
                403,
                json_body={
                    "error": {
                        "message": "未开通会员或会员已到期，请订阅后使用",
                        "type": "permission_error",
                        "code": "membership_required",
                        "biz_code": 30012,
                    }
                },
            )

        monkeypatch.setattr(p, "_send", fake_send)
        out = asyncio.run(p.forward({"model": "mimo-v2.6-pro", "messages": []}, "openai"))
        assert out.status_code == 403
        assert len(sends) == 1, "业务权限错误不应触发重试"
        assert calls == [{"force": False}]


class TestAuthRejection:
    def test_membership_required_is_business(self):
        r = _resp(
            403,
            json_body={
                "error": {
                    "message": "未开通会员或会员已到期",
                    "type": "permission_error",
                    "code": "membership_required",
                    "biz_code": 30012,
                }
            },
        )
        assert _is_auth_rejection(r) is False

    def test_unauthorized_is_auth(self):
        r = _resp(401, json_body={"error": {"message": "invalid or missing token"}})
        assert _is_auth_rejection(r) is True

    def test_unparseable_body_treated_as_auth(self):
        r = _resp(403, text="<html>gateway</html>")
        assert _is_auth_rejection(r) is True


class TestQuotaShape:
    """quota() 归一化：上游 percent 是**剩余**，条目形态要对上 quotaItemHtml。"""

    def test_usage_percent_is_remaining(self):
        """方向别搞反：上游 98.7 = 剩 98.7% = 只用了 1.3%。

        实测佐证：刚买完套餐就是 ~98.7（不可能买套餐瞬间烧掉 98.7%）；
        asar i18n ``remainingPercent:"剩余 {{percent}}%"``。
        """
        from buddy_proxy.mimo.provider import _usage_item

        it = _usage_item({"percent": 98.7, "resetAt": 1790700138}, subscribed=True)
        assert it["remaining"] == 98.7, "上游 percent = 剩余"
        assert it["used"] == 1.3
        assert it["percent"] == 1.3, "quotaItemHtml 的 percent 是已用（进度条+文案）"
        assert it["total"] == 100.0
        assert it["reset_ts"] == 1790700138.0, "reset_ts 必须是秒（前端 *1000）"

    def test_usage_percent_not_multiplied_by_100(self):
        """仍是 0~100 的百分数，不能当 0~1 小数再乘 100。"""
        from buddy_proxy.mimo.provider import _usage_item

        it = _usage_item({"percent": 50.0}, subscribed=True)
        assert it["remaining"] == 50.0
        assert it["percent"] == 50.0
        assert 0.0 <= it["percent"] <= 100.0

    def test_usage_percent_zero_means_exhausted(self):
        """未开会员时实测 ``percent: 0.0`` —— 剩 0% = 已用尽，不是「一点没用」。"""
        from buddy_proxy.mimo.provider import _usage_item

        it = _usage_item({"percent": 0}, subscribed=False)
        assert it["remaining"] == 0.0
        assert it["used"] == 100.0
        assert it["percent"] == 100.0
        assert "未开通" in it["label"]

    def test_usage_unsubscribed_label(self):
        from buddy_proxy.mimo.provider import _usage_item

        it = _usage_item({"percent": 40}, subscribed=False)
        assert "未开通" in it["label"]

    def test_usage_clamps_out_of_range(self):
        from buddy_proxy.mimo.provider import _usage_item

        assert _usage_item({"percent": -5}, subscribed=True)["remaining"] == 0.0
        assert _usage_item({"percent": 150}, subscribed=True)["remaining"] == 100.0

    def test_period_item_shape(self):
        from buddy_proxy.mimo.provider import _period_item

        items = _period_item(
            {
                "title": "MiMo 入门",
                "startTime": "2026-09-22T16:42:18Z",
                "endTime": "2026-10-22T16:42:18Z",
            }
        )
        assert len(items) == 1
        it = items[0]
        assert it["total"] == 30.0, "起止 30 天"
        assert it["reset_ts"] is None, "到期不是重置，不给 reset_ts（免得前端拼「重置」）"
        assert 0.0 <= (it["percent"] or 0) <= 100.0
        assert "有效期" in it["label"] and "天" in it["label"]

    def test_period_item_skipped_without_end(self):
        from buddy_proxy.mimo.provider import _period_item

        assert _period_item({"title": "x"}) == []

    def test_to_ts_accepts_iso_and_unix(self):
        from buddy_proxy.mimo.provider import _to_ts

        assert _to_ts(1790095338) == 1790095338.0
        assert _to_ts(1790095338000) == 1790095338.0, "毫秒要折成秒"
        assert abs(_to_ts("2026-09-22T16:42:18Z") - 1790095338.0) < 1
        assert _to_ts("not-a-date") is None
        assert _to_ts(None) is None


class TestAnthropicProtocol:
    """``/v1/messages``（Claude Code）支持——mimo 上游没有 Anthropic 端点，
    响应必须由本 provider 自己转回 Anthropic 形态。

    2026-09-23 实测踩过：只做 OpenAI 透传时，Claude Code 报
    「0 stream events received」/「body is JSON but not a Message」。
    """

    def _sse(self, chunks: list[dict]) -> bytes:
        import json as _json

        body = b""
        for c in chunks:
            body += b"data:" + _json.dumps(c).encode() + b"\n\n"
        return body + b"data: [DONE]\n\n"

    def _chunk(self, delta: dict, finish=None) -> dict:
        # 关键：MiMo 每个 chunk 都显式带 tool_calls/reasoning_content = null
        return {
            "id": "x", "object": "chat.completion.chunk", "model": "mimo-v2.6-flash",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def _run(self, chunks):
        import asyncio

        import httpx

        from buddy_proxy.mimo.provider import _to_anthropic_stream

        async def _go():
            resp = httpx.Response(
                200,
                content=self._sse(chunks),
                headers={"content-type": "text/event-stream"},
                request=httpx.Request("POST", "https://x/"),
            )
            out = []
            async for piece in _to_anthropic_stream(resp, "mimo-v2.6-flash"):
                out.append(piece)
            return "".join(out)

        text = asyncio.run(_go())
        import re

        return text, re.findall(r"^event: (.+)$", text, re.M)

    def test_explicit_null_tool_calls_does_not_crash(self):
        """每个 chunk 都带 ``tool_calls: null`` —— 迭代 None 会 TypeError
        （用 .get(k, []) 的默认值对显式 null 无效，必须 or []）。"""
        text, events = self._run([
            self._chunk({"content": "", "role": "assistant",
                         "tool_calls": None, "reasoning_content": None}),
            self._chunk({"content": "在线", "role": None,
                         "tool_calls": None, "reasoning_content": None}),
            self._chunk({"content": None, "role": None,
                         "tool_calls": None, "reasoning_content": None}, "stop"),
        ])
        for need in ("message_start", "content_block_delta", "message_stop"):
            assert need in events, f"缺事件 {need}；事件={events}"
        assert "在线" in text

    def test_reasoning_is_thinking_block(self):
        text, events = self._run([
            self._chunk({"content": None, "reasoning_content": "让我想想"}),
            self._chunk({"content": "在线"}, "stop"),
        ])
        assert "thinking" in text, "reasoning_content 要转成 thinking 块"
        assert "在线" in text and "message_stop" in events

    def test_tool_calls_become_tool_use(self):
        text, events = self._run([
            self._chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                         "function": {"name": "get_weather",
                                                      "arguments": "{}"}}]}),
            self._chunk({}, "tool_use"),
        ])
        assert '"type": "tool_use"' in text.replace("'", '"') or "tool_use" in text
        assert "get_weather" in text
        assert "message_stop" in events


class TestAnthropicStreamAbort:
    """畸形 chunk 不能让整条流「半截断掉」。

    转换器 ``feed_chunk`` 对畸形 delta 会抛（实测 ``delta`` 是 str 时
    ``AttributeError``）。不兜的话客户端收到的是「内容块悬空、没有
    message_stop」的残流——比直接报错更难排查。修法是异常时先
    ``close_open_blocks()`` 再补一个 error 事件。
    """

    def _sse(self, chunks):
        import json as _json

        body = b""
        for c in chunks:
            body += b"data:" + _json.dumps(c).encode() + b"\n\n"
        return body + b"data: [DONE]\n\n"

    def _run(self, chunks):
        import asyncio

        import httpx

        from buddy_proxy.mimo.provider import _to_anthropic_stream

        async def _go():
            resp = httpx.Response(
                200,
                content=self._sse(chunks),
                headers={"content-type": "text/event-stream"},
                request=httpx.Request("POST", "https://x/"),
            )
            out = []
            async for piece in _to_anthropic_stream(resp, "mimo-auto"):
                out.append(piece)
            return "".join(out)

        return asyncio.run(_go())

    def test_malformed_delta_yields_error_not_broken_stream(self):
        text = self._run([
            {"choices": [{"index": 0, "delta": {"content": "开头"}, "finish_reason": None}]},
            # delta 是 str —— feed_chunk 会抛 AttributeError
            {"choices": [{"index": 0, "delta": "NOT_A_DICT", "finish_reason": None}]},
        ])
        assert "event: error" in text, "畸形数据要给客户端一个 error 事件"
        assert "AttributeError" in text, "错误信息要带异常类型，便于排查"
        # 已开出的文本块必须收尾，不能悬空
        assert "content_block_stop" in text, "已开的内容块要 close 掉"

    def test_normal_stream_unaffected_by_guard(self):
        text = self._run([
            {"choices": [{"index": 0, "delta": {"content": "在线"}, "finish_reason": "stop"}]},
        ])
        assert "message_start" in text and "message_stop" in text
        assert "event: error" not in text, "正常流不该被兜底逻辑影响"
