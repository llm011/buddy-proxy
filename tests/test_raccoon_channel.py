"""raccoon 通道核心不变量。

这个通道最容易踩的四个坑，每个都用一条测试钉住：

1. **密码加密格式**：必须与上游网页端同款（AES-CFB + IV 前置 + Base64）。
   格式错了 = 自动续期永远失败，而那正是本通道相对开源实现的主要增量。
2. **未知模型不能静默回落**：上游对未知模型名回 200 但换默认模型，
   必须请求前拦截 + 首帧比对回显，两层缺一不可。
3. **模型级 429 只挡一个模型**：上游的 ``user_model_rate_limited`` 是
   「账号×模型」窗口（恢复中位数 25 分钟），把整号摘掉会让同账号其它模型
   白白不可用。
4. **并发闸门**：上游并发上限约 5，超了停摆 25 分钟，必须本地卡住。
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from buddy_proxy.core.aes_pure import aes_cbc_encrypt
from buddy_proxy.raccoon import credentials as creds
from buddy_proxy.raccoon import failover
from buddy_proxy.raccoon import login as raccoon_login
from buddy_proxy.raccoon import upstream
from buddy_proxy.raccoon.rate_limit import AccountConcurrencyLimiter


# ---------------------------------------------------------------------------
# 1. 密码加密格式
# ---------------------------------------------------------------------------

def _reference_cfb_encrypt(password: str, iv: bytes) -> bytes:
    """独立实现的 AES-CFB128 参考（不复用被测代码的循环）。"""
    key = b"senseraccoon2023"
    plain = password.encode("utf-8")
    out = b""
    prev = iv
    for i in range(0, len(plain), 16):
        chunk = plain[i:i + 16]
        keystream = aes_cbc_encrypt(key, b"\x00" * 16, prev)[:len(chunk)]
        enc = bytes(chunk[j] ^ keystream[j] for j in range(len(chunk)))
        out += enc
        prev = (enc + b"\x00" * 16)[:16]
    return out


def test_password_encrypt_matches_upstream_format():
    """密文必须是「16 字节 IV 前置 + AES-CFB 密文」的 Base64。

    这条锁的是**格式**（不是随机性）：IV 是随机的，所以用「解出来的明文
    正确」+「结构与参考实现一致」两条一起判。
    """
    for password in ("12345678", "a" * 16, "b" * 17, "中文密码测试12345"):
        blob = raccoon_login.encrypt_password(password)
        raw = base64.b64decode(blob)
        iv, cipher = raw[:16], raw[16:]
        assert len(iv) == 16, "IV 必须 16 字节且前置"
        assert len(cipher) == len(password.encode()), "CFB 是流模式，密文长度等于明文长度"
        # 用同一 IV 独立重算，必须逐字节相同
        assert cipher == _reference_cfb_encrypt(password, iv)
        # 自洽解密
        assert raccoon_login.decrypt_password(blob) == password


def test_password_encrypt_uses_random_iv():
    """每次加密用新 IV：同一密码两次密文必须不同（否则 IV 复用是密码学错误）。"""
    a = raccoon_login.encrypt_password("samepassword")
    b = raccoon_login.encrypt_password("samepassword")
    assert a != b
    assert raccoon_login.decrypt_password(a) == raccoon_login.decrypt_password(b)


def test_short_password_rejected_before_network():
    """密码 < 8 位本地拒绝（上游要求），省一次网络往返。"""
    with pytest.raises(raccoon_login.AuthError) as ei:
        raccoon_login.login_with_password("13800000000", "short")
    assert "8" in str(ei.value)


# ---------------------------------------------------------------------------
# 2. 会话解析与身份
# ---------------------------------------------------------------------------

def _fake_jwt(uid: str = "Raccoon-pu-abc", exp: int = 0) -> str:
    """造一个形状合法的 JWT（不验签，只测解析）。"""
    import json

    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": uid, "exp": exp}).encode()).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.sig"


def test_parse_session_accepts_all_paste_shapes():
    """粘 token 的四种形态都要认：裸 JWT / JSON / cookie 串 / 带 Bearer。"""
    token = _fake_jwt()
    assert raccoon_login._parse_session_blob(token) == token
    assert raccoon_login._parse_session_blob(f"Bearer {token}") == token
    assert raccoon_login._parse_session_blob('{"access_token": "%s"}' % token) == token
    assert raccoon_login._parse_session_blob(f"xhx_token={token}; other=1") == token


def test_parse_session_rejects_garbage():
    assert raccoon_login._parse_session_blob("not-a-token") == ""
    assert raccoon_login._parse_session_blob("") == ""


def test_jwt_uid_and_expiry():
    token = _fake_jwt(uid="Raccoon-pu-xyz", exp=1800000000)
    assert creds.jwt_uid(token) == "Raccoon-pu-xyz"
    assert creds.jwt_expiry(token) == 1800000000
    assert creds.jwt_uid("garbage") == ""
    assert creds.jwt_expiry("garbage") == 0


def test_token_without_exp_is_treated_as_valid():
    """没有 exp 不等于过期——交给上游 401 判死比本地瞎猜准。"""
    assert creds.access_token_valid({"access_token": _fake_jwt(exp=0)}) is True


# ---------------------------------------------------------------------------
# 3. 模型：不静默回落
# ---------------------------------------------------------------------------

def test_unknown_model_is_rejected_not_fallback():
    """未知模型必须 400，不能发出去让上游静默换模型。"""
    from fastapi import HTTPException

    from buddy_proxy.raccoon.provider import RaccoonProvider

    provider = RaccoonProvider()
    with pytest.raises(HTTPException) as ei:
        provider._resolve_model("no-such-model")
    assert ei.value.status_code == 400
    assert "未知模型" in str(ei.value.detail)


def test_known_model_resolves_with_or_without_prefix():
    from buddy_proxy.raccoon.provider import RaccoonProvider

    provider = RaccoonProvider()
    first = provider.models()[0]["id"]
    assert provider._resolve_model(first)["id"] == first
    # 不带 sn- 前缀也认（客户端手打时常见）
    bare = first.removeprefix("sn-")
    assert provider._resolve_model(bare)["id"] == first


def test_model_echo_mismatch_detected():
    """首帧 model 回显不一致必须被识别（这是「问 A 答 B」的唯一硬证据）。"""
    assert upstream.model_echo_matches({"model": "sn-glm-5-3"}, "sn-glm-5-3") is True
    assert upstream.model_echo_matches({"model": "sn-other"}, "sn-glm-5-3") is False
    # 缺失回显不阻断：不是所有版本都回显
    assert upstream.model_echo_matches({}, "sn-glm-5-3") is True


# ---------------------------------------------------------------------------
# 4. 模型级限流只挡一个模型
# ---------------------------------------------------------------------------

def test_model_rate_limit_only_blocks_that_model(monkeypatch):
    """模型级 429 只挡「账号×模型」，同账号其它模型仍可用。"""
    failover._clear_all_cooldowns()
    monkeypatch.setattr(failover, "list_accounts",
                        lambda: [creds.AccountRef(id="acct-1", name="a",
                                                  priority=0, added_at=0)])

    assert [a.id for a in failover.available_accounts("sn-glm-5-3")] == ["acct-1"]

    failover.mark_model_cooldown("acct-1", "sn-glm-5-3", reason="429")
    # 被限流的模型：摘掉
    assert failover.available_accounts("sn-glm-5-3") == []
    # 其它模型：不受影响
    assert [a.id for a in failover.available_accounts("sn-kimi-k3")] == ["acct-1"]
    # 不指定模型（通道级判断）：账号本身没被冷却，仍算可用
    assert [a.id for a in failover.available_accounts()] == ["acct-1"]


def test_credit_exhaustion_cools_whole_account(monkeypatch):
    """积分耗尽（402）是账号级：整号停摆，不分模型。"""
    failover._clear_all_cooldowns()
    monkeypatch.setattr(failover, "list_accounts",
                        lambda: [creds.AccountRef(id="acct-2", name="b",
                                                  priority=0, added_at=0)])
    failover.mark_cooldown("acct-2", quota=True, reason="积分耗尽")
    assert failover.available_accounts("sn-glm-5-3") == []
    assert failover.available_accounts("sn-kimi-k3") == []


def test_rate_limit_signal_classification():
    assert failover.is_model_rate_limited(429, "user_model_rate_limited", "") is True
    assert failover.is_credit_exhausted(402, "", "") is True
    assert failover.is_credit_exhausted(429, "user_model_rate_limited", "") is False


# ---------------------------------------------------------------------------
# 5. 并发闸门
# ---------------------------------------------------------------------------

def test_concurrency_limiter_blocks_beyond_limit():
    """超过上限必须排队，不能直接打到上游（停摆 25 分钟的代价太高）。"""
    limiter = AccountConcurrencyLimiter(max_concurrent=2, queue_timeout_s=0.3)

    async def run():
        a = await limiter.acquire("acct")
        b = await limiter.acquire("acct")
        assert a is not None and b is not None
        # 第三个：排队超时后返回 None
        c = await limiter.acquire("acct")
        assert c is None
        # 释放一个后又能拿到
        a.release()
        d = await limiter.acquire("acct")
        assert d is not None
        b.release()
        d.release()

    asyncio.run(run())
    assert limiter.snapshot()["inflight"] == {}


def test_concurrency_limiter_per_account():
    """限流按账号隔离：一个账号占满不影响另一个。"""
    limiter = AccountConcurrencyLimiter(max_concurrent=1, queue_timeout_s=0.2)

    async def run():
        a = await limiter.acquire("acct-a")
        assert a is not None
        b = await limiter.acquire("acct-b")
        assert b is not None, "另一个账号不该被 A 的占满挡住"
        a.release()
        b.release()

    asyncio.run(run())


def test_lease_release_is_idempotent():
    """lease 重复 release 不能把计数减成负数（流式收尾路径会重复调）。"""
    limiter = AccountConcurrencyLimiter(max_concurrent=2, queue_timeout_s=0.2)

    async def run():
        lease = await limiter.acquire("acct")
        lease.release()
        lease.release()
        lease.release()
        assert limiter.inflight("acct") == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 6. 目录与积分解析
# ---------------------------------------------------------------------------

def test_parse_catalog_extracts_multiplier_and_free_flag():
    """倍率 0 = 限免，且限免的排在前面（调度优先烧限免额度）。"""
    payload = {"data": [
        {"model": "sn-paid", "billing_effective_multiplier": 1.0},
        {"model": "sn-free", "billing_effective_multiplier": 0,
         "context_window": 128000, "vision": True},
    ]}
    models = upstream.parse_catalog(payload)
    assert [m["id"] for m in models] == ["sn-free", "sn-paid"]
    free = models[0]
    assert free["free"] is True and free["multiplier"] == 0.0
    assert free["context_window"] == 128000 and free["vision"] is True
    assert models[1]["free"] is False


def test_parse_catalog_missing_multiplier_defaults_to_paid():
    """倍率缺失按 1.0（付费）——不能默认当免费，否则会优先烧错额度。"""
    models = upstream.parse_catalog({"data": [{"model": "sn-x"}]})
    assert models[0]["multiplier"] == 1.0
    assert models[0]["free"] is False


def test_parse_balance_returns_empty_on_garbage():
    """拿不到余额返回空 dict，不能编 0（0 会被读成「没额度了」）。"""
    assert upstream.parse_balance(None) == {}
    assert upstream.parse_balance("nope") == {}
    assert upstream.parse_balance({"unrelated": 1}) == {}


def test_parse_balance_reads_daily_pool():
    parsed = upstream.parse_balance({"data": {
        "credits": 1234.5, "daily_points": 300.0, "daily_limit": 2100.0,
        "expire_at": 1795000000,
    }})
    assert parsed["credits"] == 1234.5
    assert parsed["daily_points"] == 300.0
    assert parsed["daily_limit"] == 2100.0
    assert parsed["expire_at"] == 1795000000


# ---------------------------------------------------------------------------
# 7. 凭据去重（多账号管理的关键）
# ---------------------------------------------------------------------------

def test_same_uid_upsert_updates_instead_of_duplicating(tmp_path, monkeypatch):
    """同 uid 重新导入必须就地更新——否则 30 天换一次 token 会堆出重复账号。

    重复行会让同一账号被当成两个号并发：既翻倍统计，又绕过按账号算的并发
    上限（正是把账号推向风控的路径）。
    """
    monkeypatch.setenv("RACCOON_STATE_DIR", str(tmp_path / "raccoon"))
    monkeypatch.setattr(creds, "_STORE",
                        creds.AccountStore(label="raccoon-test",
                                           state_dir_fn=creds.raccoon_state_dir,
                                           max_accounts=None))
    token = _fake_jwt(uid="Raccoon-pu-dup")
    first = creds.save_account_cred({"access_token": token, "uid": "Raccoon-pu-dup"})
    second = creds.save_account_cred({"access_token": token + "x", "uid": "Raccoon-pu-dup"})
    assert first.id == second.id
    assert len(creds.list_accounts()) == 1
    # priority/added_at 不变（重导不改变 failover 顺位）
    assert second.priority == first.priority

# ---------------------------------------------------------------------------
# 8. 登录请求体必须与上游口径一致（100004 回归）
# ---------------------------------------------------------------------------

def test_login_body_matches_upstream_spec():
    """登录请求体三处必须对齐，错一处就是 ``100004 params_unsupport_error``。

    2026-11-23 实测踩过：只加密密码、手机号传明文 + 用旧接口，上游直接回
    ``400 100004 params_unsupport_error``。三处分别是：

    1. 走 IAM 接口 ``/api/iam/v1/auth/login/password/phone``（不是旧
       ``login_with_password``——旧接口不再补发当天积分）；
    2. **手机号也要加密**（与密码同一个加密函数）；
    3. ``nation_code`` 带 ``+`` 前缀。
    """
    import base64
    import json as _json
    from unittest import mock

    def _jwt(uid="Raccoon-pu-x", exp=1797000000):
        h = base64.urlsafe_b64encode(_json.dumps({"alg": "none"}).encode()).rstrip(b"=")
        p = base64.urlsafe_b64encode(
            _json.dumps({"sub": uid, "exp": exp}).encode()).rstrip(b"=")
        return f"{h.decode()}.{p.decode()}.sig"

    captured = []

    class _Resp:
        def __init__(self, code, payload):
            self.status_code = code
            self._p = payload
            self.text = _json.dumps(payload)

        def json(self):
            return self._p

    def _fake_post(self, url, json=None, headers=None):
        captured.append({"url": url, "body": json, "headers": headers or {}})
        if "iam/confirm" in url:
            return _Resp(200, {"code": 0, "data": {}})
        return _Resp(200, {"code": 0, "data": {"access_token": _jwt()}})

    with mock.patch("httpx.Client.post", _fake_post):
        raccoon_login.login_with_password("+86 138-0000-1111", "testpass1234")

    login_req = captured[0]
    assert login_req["url"].endswith("/api/iam/v1/auth/login/password/phone"), \
        "必须走 IAM 接口：旧 login_with_password 不再补发当天每日积分"
    assert login_req["body"]["nation_code"] == "+86", "nation_code 要带 + 前缀"
    # 手机号必须是密文（明文会被上游判「参数不受支持」），且解出来是纯数字
    assert raccoon_login.decrypt_password(login_req["body"]["phone"]) == "13800001111"
    assert "+86" not in login_req["body"]["phone"]
    # 密码不得明文上网
    assert "testpass1234" not in login_req["body"]["password"]
    # IAM 确认是必需第二步：不确认的会话打 /api/web/* 全 400
    confirm = captured[1]
    assert confirm["url"].endswith("/api/web/auth/v1/iam/confirm")
    assert "xhx_token" in confirm["headers"]


def test_phone_normalization():
    """手机号归一：去分隔符与 +86/86 前缀（上游要纯数字，国家码单独传）。"""
    for raw in ("+86 138-0000-1111", "8613800001111", "138 0000 1111",
                "(138) 0000-1111", "13800001111"):
        assert raccoon_login.normalize_phone(raw) == "13800001111", raw


def test_sealed_password_is_not_decryptable_with_public_key():
    """落盘密文**不能**用上游那个公开 key 解开——否则等于没加密。

    这是「落盘用本机密钥」的核心断言：开源代码里能查到的 ``senseraccoon2023``
    必须解不开磁盘上的密文，只有本机 ``.secret`` 能解。
    """
    sealed = raccoon_login.seal_password("mysecretpassword")
    # 用上游公开 key 解：必须失败（解出乱码或抛错，不能等于原文）
    try:
        wrong = raccoon_login.decrypt_password(sealed)
    except Exception:  # noqa: BLE001 - 解不开是预期结果之一
        wrong = ""
    assert wrong != "mysecretpassword"
    # 用本机 key 解：必须正确
    assert raccoon_login.unseal_password(sealed) == "mysecretpassword"
