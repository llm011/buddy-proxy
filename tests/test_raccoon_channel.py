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

def test_parse_catalog_real_structure():
    """解析上游**真实**目录形状：``data.categories[].models[]``。

    2026-11-23 实测踩过：我按 ``data`` 是 list 写解析器，一个模型都取不出来，
    于是 models.json 里手猜的名字被发出去，上游回
    ``100004 param model unsupport``（真实名字带版本后缀如 ``-1017``）。

    这条用真实响应结构锁住解析器。
    """
    payload = {"code": 0, "data": {"categories": [{
        "type": "chat", "default_model": "raccoon-090dd3",
        "models": [
            {"name": "raccoon-090dd3", "visible": False,
             "billing_effective_multiplier": 1},
            {"name": "sn-glm-5-3-flash", "visible": True,
             "billing_effective_multiplier": 0.2,
             "tags": ["general", "vision"],
             "params": {"context_window": 1000000, "max_tokens": 100000}},
            {"name": "sn-sensenova-6-8-flash-lite", "visible": True,
             "billing_effective_multiplier": 0,
             "tags": ["vision", "reasoning"],
             "params": {"context_window": 256000}},
        ],
    }]}}
    models = upstream.parse_catalog(payload)
    # visible=false 的内部代号不进对外列表
    assert [m["id"] for m in models] == [
        "sn-sensenova-6-8-flash-lite", "sn-glm-5-3-flash"]
    free = models[0]
    assert free["free"] is True and free["multiplier"] == 0.0
    assert free["context_window"] == 256000
    assert free["vision"] is True and free["reasoning"] is True
    paid = models[1]
    assert paid["free"] is False and paid["multiplier"] == 0.2
    assert paid["context_window"] == 1000000
    assert paid["max_output"] == 100000


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

# ---------------------------------------------------------------------------
# 9. 资源包展示：不许编上限（2026-11-23 用户实报回归）
# ---------------------------------------------------------------------------

def test_parse_bills_extracts_real_packs():
    """流水 → 资源包：每笔发放一个包，带金额与到期时间。

    上游 ``/points/v1/balance`` 只给汇总剩余量，看不出「有几个包」。
    真实包明细只在 ``/points/v2/bills?type=income`` 里。
    """
    payload = {"data": {"items": [
        {"event_name": "每日积分发放", "biz_type": "daily_grant", "points": 300,
         "grant_at": "2026-11-23T00:00:00+08:00",
         "expire_at": "2026-11-29T23:59:59+08:00"},
        {"event_name": "新人注册礼包", "biz_type": "reward_grant", "points": 3000,
         "grant_at": "2026-11-23T01:24:46+08:00"},
    ], "has_more": False}}
    bills = upstream.parse_bills(payload)
    assert len(bills) == 2
    # 快到期的排前面
    assert bills[0]["event"] == "每日积分发放"
    assert bills[0]["points"] == 300.0
    assert bills[0]["expire_at"].startswith("2026-11-29")
    assert bills[1]["event"] == "新人注册礼包"
    assert bills[1]["points"] == 3000.0
    assert bills[1]["expire_at"] == ""   # 无到期时间 = 永不过期


def test_used_derived_from_real_grants_not_invented():
    """「已用」必须由**上游真实数据**推出，不能按规则编。

    2026-11-23 用户实报：「我都没用过一点积分，不可能 2100 里只剩下 300」。
    我当时按「每天 300 × 7 天」推了个 2100 当上限，于是「剩 300」被算成
    「已用 1800 / 85.71%」——纯属编数。

    正确口径（两者都是上游给的）：

        已用 = Σ(未过期发放流水) − 当前可用积分

    用户账号实测：发放 300 + 3000 = 3300，balance 也 3300 → 已用 0，
    与其「一分没用」的自述一致。
    """
    from buddy_proxy.raccoon.provider import RaccoonProvider

    provider = RaccoonProvider()
    parsed = {"credits": 3300.0, "daily_points": 300.0,
              "reward_points": 3000.0, "monthly_points": 0.0, "topup_points": 0.0}
    bills = [
        {"event": "每日积分发放", "points": 300.0,
         "grant_at": "2026-11-23T00:00:00+08:00",
         "expire_at": "2026-11-29T23:59:59+08:00"},
        {"event": "新人注册礼包", "points": 3000.0, "grant_at": "", "expire_at": ""},
    ]
    items = provider._items_from_balance(parsed, "", bills)

    head = next(it for it in items if it["label"].endswith("积分总额"))
    assert head["total"] == 3300.0, "分母 = 未过期发放合计（上游流水之和）"
    assert head["remaining"] == 3300.0
    assert head["used"] == 0.0, "一分没用就必须是 0，不能是推算出来的 1800"
    assert head["percent"] == 0.0
    # 必须有 total+used 才能让现成组件画出进度条（head_only 会被跳过）
    assert not head.get("head_only"), "积分总额不能标 head_only，否则没有进度条"

    # 单包条目**不画进度条**：单笔发放没有可信上限
    packs = [it for it in items if it is not head]
    for it in packs:
        assert it.get("percent") is None, f"单包不该有百分比：{it}"
        assert it.get("total") is None, f"单包不该有分母：{it}"

    labels = [it["label"] for it in items]
    assert any("每日积分发放" in x for x in labels)
    assert any("新人注册礼包" in x for x in labels)


def test_used_reflects_actual_consumption():
    """真花了积分时，已用要跟着变（不是恒 0）。"""
    from buddy_proxy.raccoon.provider import RaccoonProvider

    provider = RaccoonProvider()
    # 发放 3300，只剩 2300 → 花了 1000
    parsed = {"credits": 2300.0}
    bills = [
        {"event": "每日积分发放", "points": 300.0, "grant_at": "", "expire_at": ""},
        {"event": "新人注册礼包", "points": 3000.0, "grant_at": "", "expire_at": ""},
    ]
    items = provider._items_from_balance(parsed, "", bills)
    head = next(it for it in items if it["label"].endswith("积分总额"))
    assert head["total"] == 3300.0
    assert head["used"] == 1000.0
    assert round(head["percent"], 1) == 30.3


def test_expired_grants_excluded_from_denominator():
    """已过期的发放不算进分母——否则「已用」会凭空变大。"""
    from buddy_proxy.raccoon.provider import RaccoonProvider

    provider = RaccoonProvider()
    parsed = {"credits": 3000.0}
    bills = [
        # 已过期（2020 年）
        {"event": "过期包", "points": 500.0, "grant_at": "",
         "expire_at": "2020-01-01T00:00:00+08:00"},
        {"event": "新人注册礼包", "points": 3000.0, "grant_at": "", "expire_at": ""},
    ]
    items = provider._items_from_balance(parsed, "", bills)
    head = next(it for it in items if it["label"].endswith("积分总额"))
    assert head["total"] == 3000.0, "过期包不进分母"
    assert head["used"] == 0.0


def test_no_bills_means_no_progress_bar():
    """拿不到流水时不画进度条（没有可信分母），只显示剩余量。"""
    from buddy_proxy.raccoon.provider import RaccoonProvider

    provider = RaccoonProvider()
    items = provider._items_from_balance({"credits": 3300.0}, "", [])
    for it in items:
        assert it.get("total") is None
        assert it.get("percent") is None
    assert any(it["remaining"] == 3300.0 for it in items)

def test_expire_ts_is_epoch_seconds_not_iso_string():
    """``expire_ts`` 必须是 **epoch 秒**，不能是 ISO 字符串。

    2026-11-23 用户实报「每日积分发放 · NaN-NaN-NaN 到期」：前端
    ``fmtExpireMmdd`` 按 ``new Date(ts * 1000)`` 算，传 ISO 字符串会得到
    Invalid Date → 渲染成 NaN-NaN-NaN。

    这条锁死契约：所有条目的 ``expire_ts`` 要么是 ``None``，要么是数字。
    """
    from buddy_proxy.raccoon.provider import RaccoonProvider

    provider = RaccoonProvider()
    parsed = {"credits": 3300.0}
    bills = [
        {"event": "每日积分发放", "points": 300.0,
         "grant_at": "2026-11-23T00:00:00+08:00",
         "expire_at": "2026-11-29T23:59:59+08:00"},
        {"event": "新人注册礼包", "points": 3000.0, "grant_at": "", "expire_at": ""},
    ]
    items = provider._items_from_balance(parsed, "", bills)

    for it in items:
        exp = it.get("expire_ts")
        assert exp is None or isinstance(exp, (int, float)), (
            f"expire_ts 必须是 epoch 秒或 None，实际 {exp!r}（ISO 字符串会让前端"
            f"渲染成 NaN-NaN-NaN）：{it['label']}")

    # 有到期时间的那笔要转成正确的 epoch（2026-11-29 23:59:59 +08:00）
    pack = next(it for it in items if "每日积分发放" in it["label"])
    assert pack["expire_ts"] is not None
    import datetime
    dt = datetime.datetime.fromtimestamp(pack["expire_ts"],
                                        datetime.timezone(datetime.timedelta(hours=8)))
    assert dt.year == 2026 and dt.month == 11 and dt.day == 29

    # 无到期时间的包：expire_ts 必须是 None（组件据此不画到期）
    no_exp = next(it for it in items if "新人注册礼包" in it["label"])
    assert no_exp["expire_ts"] is None

# ---------------------------------------------------------------------------
# 10. 签到：以流水为准，金额进日历
# ---------------------------------------------------------------------------

def test_has_daily_grant_today_uses_bills_not_http():
    """今日是否签到只看流水里的 ``daily_grant``，不看接口返回码。

    上游对重复操作回 200 但余额不动（假成功），所以流水是唯一可信判据。
    """
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).replace(hour=10, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)

    bills = [
        {"biz_type": "daily_grant",
         "grant_at": today.isoformat(), "points": 300.0},
        {"biz_type": "reward_grant",
         "grant_at": today.isoformat(), "points": 3000.0},
    ]
    assert upstream.has_daily_grant_today(bills) is True

    # 只有昨天那笔 → 今天没签
    old = [{"biz_type": "daily_grant",
            "grant_at": yesterday.isoformat(), "points": 300.0}]
    assert upstream.has_daily_grant_today(old) is False

    # 有别的发放但没 daily_grant → 今天没签
    only_reward = [{"biz_type": "reward_grant",
                    "grant_at": today.isoformat(), "points": 3000.0}]
    assert upstream.has_daily_grant_today(only_reward) is False


def test_today_grant_points_comes_from_bill_not_hardcoded():
    """签到金额取流水里的 ``points``，**不硬编码 300**。

    上游调过面额（CodeArts 的活动从 1000 降到过 100），写死会与实际到账不符。
    """
    from datetime import datetime, timedelta, timezone

    from buddy_proxy.raccoon.provider import _today_daily_grant_points

    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).replace(hour=10, minute=0, second=0, microsecond=0)
    bills = [{"biz_type": "daily_grant", "grant_at": today.isoformat(),
              "points": 250.0}]
    assert _today_daily_grant_points(bills) == 250.0
    # 没有今天的发放 → None（不是 0，0 会把日历的合计糊掉）
    assert _today_daily_grant_points([]) is None


def test_next_daily_reset_is_next_midnight_cst():
    """下次发放时刻是次日 00:00（+08:00），且明确标 ``inferred``。

    上游没有「下次刷新」字段，是按「每天 00:00 发」的规则推的，界面要标注
    来源而不是说成上游契约。
    """
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz).replace(hour=15, minute=30, second=0, microsecond=0)
    nxt = upstream.next_daily_reset_epoch(now=now.timestamp())
    dt = datetime.fromtimestamp(nxt, tz)
    assert dt.hour == 0 and dt.minute == 0
    assert dt.date() == (now + timedelta(days=1)).date()


def test_checkin_status_and_claim_carry_credits():
    """签到状态/领取都要带 ``credits``，日历侧边栏靠它显示「加了多少」。

    2026-11-23 改成多账号聚合后，单账号分支仍直接返回 ``"credits": credits``，
    多账号分支则累加进 ``status["credits"]``——两条路都必须带上。
    """
    import inspect

    from buddy_proxy.raccoon.provider import RaccoonProvider

    assert getattr(RaccoonProvider, "supports_checkin", False) is True
    # 单账号直通分支在 _checkin_for_account / _claim_for_account 里
    assert '"credits": credits' in inspect.getsource(
        RaccoonProvider._checkin_for_account)
    assert '"credits": credits' in inspect.getsource(
        RaccoonProvider._claim_for_account)
    # 多账号聚合分支：累加后写入
    for fn in (RaccoonProvider.checkin_status, RaccoonProvider.checkin_claim):
        src = inspect.getsource(fn)
        assert 'status["credits"] = total_credits' in src, \
            f"{fn.__name__} 的多账号分支要累加 credits（日历取这个值）"


def test_checkin_dates_feed_the_calendar():
    """签到必须回 ``checkin_dates``，否则「打卡日历」里看不到小浣熊。

    2026-11-23 用户实报「打卡日历里也没有展示小浣熊的」。日历靠各通道返回的
    ``checkin_dates`` 合并历史（见 ``BenefitsManager.snapshot``），raccoon 原来
    一个都不给。上游没有签到历史接口，只能从流水里的 ``daily_grant`` 反推。
    """
    import inspect

    from buddy_proxy.raccoon.provider import (
        RaccoonProvider,
        _daily_grant_dates,
    )

    # 单账号与多账号两条路都要带上
    assert '"checkin_dates"' in inspect.getsource(
        RaccoonProvider._checkin_for_account)
    assert '"checkin_dates"' in inspect.getsource(
        RaccoonProvider._claim_for_account)
    assert '"checkin_dates": sorted(all_dates)' in inspect.getsource(
        RaccoonProvider.checkin_status)

    # 反推口径：只认 daily_grant，按 +08:00 取日期，去重排序
    bills = [
        {"biz_type": "daily_grant", "grant_at": "2026-11-23T00:00:05+08:00",
         "points": 300},
        {"biz_type": "daily_grant", "grant_at": "2026-11-22T00:00:03+08:00",
         "points": 300},
        {"biz_type": "daily_grant", "grant_at": "2026-11-23T00:00:05+08:00",
         "points": 300},   # 重复日期只留一条
        {"biz_type": "consume", "grant_at": "2026-11-21T10:00:00+08:00",
         "points": -2},    # 消费流水不是签到
    ]
    assert _daily_grant_dates(bills) == ["2026-11-22", "2026-11-23"]

# ---------------------------------------------------------------------------
# 11. 流式必须直通（不能过 gemini 转换器）
# ---------------------------------------------------------------------------

def test_openai_stream_is_passthrough_not_gemini_converter():
    """openai 出口必须**直通**上游 SSE，不能走 gemini 的转换器。

    2026-11-23 用户实报「日志里都看不到 token 消耗信息，只有耗时信息」。
    根因：上游本身就是 OpenAI SSE（``choices``/``usage``），却走了
    ``gemini._to_openai_stream``——它按 Gemini 形状（``candidates``/``parts``）
    解析，整条流被当成「没有语义事件」丢掉：客户端拿不到内容、
    metrics 里 prompt/completion 恒为 0。

    这条断言 forward 的流式分支用的是 ``_passthrough_sse`` 而不是
    ``_to_openai_stream``（anthropic 出口仍需转换，单独判断）。
    """
    import inspect

    from buddy_proxy.raccoon.provider import RaccoonProvider, _passthrough_sse

    src = inspect.getsource(RaccoonProvider.forward)
    assert "_passthrough_sse" in src, "openai 流式必须直通"
    # gemini 的 openai 转换器不能出现在**代码行**里（注释里提到它不算——
    # 解释「为什么不用它」是必要的）。所以逐行判、跳过注释。
    code_lines = [ln for ln in src.splitlines()
                  if not ln.lstrip().startswith("#")]
    offenders = [ln.strip() for ln in code_lines if "_to_openai_stream(" in ln]
    assert not offenders, (
        "openai 出口不能过 gemini 转换器——上游是 OpenAI 形态，转换会把"
        f"整条流当无语义事件丢掉：{offenders}")
    # anthropic 出口仍要转换
    assert "_to_anthropic_stream" in src
    assert callable(_passthrough_sse)


def test_passthrough_sse_keeps_usage_frame():
    """直通要保留 usage 帧（metrics 靠它记 token）。"""
    import asyncio

    from buddy_proxy.raccoon.provider import _passthrough_sse

    class _FakeStream:
        def __init__(self):
            self.closed = False
            self._lines = [
                'data: {"choices":[{"delta":{"content":"OK"}}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":33,'
                '"completion_tokens":16,"total_tokens":49}}',
                "data: [DONE]",
            ]

        async def aiter_lines(self):
            for line in self._lines:
                yield line

        async def aclose(self):
            self.closed = True

    async def run():
        stream = _FakeStream()
        out = []
        async for chunk in _passthrough_sse(stream):
            out.append(chunk)
        return out, stream

    out, stream = asyncio.run(run())
    joined = "".join(out)
    assert '"usage"' in joined, "usage 帧必须保留"
    assert '"prompt_tokens":33' in joined
    assert "[DONE]" in joined
    assert stream.closed, "流结束必须 aclose（释放并发 lease）"


def test_multiplier_label_for_model_page():
    """模型页 tag 位显示积分倍率，``x0`` 显式标「限免」。"""
    from buddy_proxy.web.ui.models_api import _multiplier_label

    assert _multiplier_label({"multiplier": 0}) == "x0 限免"
    assert _multiplier_label({"multiplier": 0.25}) == "x0.25"
    assert _multiplier_label({"multiplier": 1.0}) == "x1"
    # 没有倍率字段的通道不显示（不能编一个）
    assert _multiplier_label({}) is None
    assert _multiplier_label({"multiplier": True}) is None

# ---------------------------------------------------------------------------
# 12. 积分消耗进 metrics（按笔数×倍率，不是按 token 估算）
# ---------------------------------------------------------------------------

def test_credit_uses_balance_delta_not_multiplier():
    """积分必须用**余额差**结算，不能按倍率瞎算。

    2026-11-23 实测证伪了「1 × 倍率」：同一模型倍率固定，但短请求扣 2 分、
    中等扣 10、长扣 14——差 7 倍。说明它**按 token 量计费**，倍率只是单价
    系数。凭倍率算出来的数字与账单完全不符。

    正确口径：请求前后各读一次余额，差值就是本条消耗（与 antigravity 的
    池水位落差同一个思路）。账单流水虽记了 ``points=-2``，但不带模型名和
    请求 id，无法归属到具体请求。
    """
    from buddy_proxy.raccoon.provider import RaccoonProvider

    provider = RaccoonProvider()

    # 正常下降：差值就是消耗
    frame: dict = {}
    provider._settle_credit(frame, 3300.0, 3298.0)
    assert frame["ag_credit"] == 2.0
    assert frame["ag_credit_exact"] is True

    # 余额没动 / 反而涨了 → 不写（面板记 —，不拿 0 冒充免费）
    for before, after in ((3300.0, 3300.0), (3300.0, 3301.0)):
        f: dict = {}
        provider._settle_credit(f, before, after)
        assert "ag_credit" not in f, f"余额没降就不该记：{before}→{after}"

    # 读不到余额 → 不写
    for before, after in ((None, 3298.0), (3300.0, None)):
        f = {}
        provider._settle_credit(f, before, after)
        assert "ag_credit" not in f


def test_no_multiplier_based_credit_remains():
    """确认「按倍率算积分」的实现已经彻底删掉（防止回退）。"""
    import inspect

    from buddy_proxy.raccoon import provider as raccoon_provider

    src = inspect.getsource(raccoon_provider)
    assert "_request_credit" not in src, (
        "按倍率算积分的 _request_credit 必须删掉——它与真实账单不符")


def test_credit_marked_exact_not_estimated():
    """写进 frame 时要标 ``ag_credit_exact``：这是精确规则，不是估算。

    不标的话 observability 会把它当 antigravity 那种水位落差估算，面板上
    多一个「估算」标记——而小浣熊的倍率是上游明文规则。
    """
    import inspect

    from buddy_proxy.raccoon.provider import RaccoonProvider

    src = inspect.getsource(RaccoonProvider._settle_credit)
    assert 'frame["ag_credit"] = delta' in src
    assert 'frame["ag_credit_exact"] = True' in src

def test_anthropic_stream_emits_content_blocks():
    """anthropic 出口必须真的产出 content_block，不能只有 start/stop。

    2026-11-23 实测：最初走了 ``gemini._to_anthropic_stream``（按 Gemini 事件
    形状解析），结果整条流只有 ``message_start`` 紧跟 ``message_stop``，
    **没有一个 content_block**——客户端拿到空回复。这与 openai 出口踩的是
    同一个坑（上游是 OpenAI 形态，不能过 Gemini 的转换器）。

    改用 ``AnthropicStreamConverter``（mimo/copilot/ark 同款，本来就吃
    OpenAI chunk）。
    """
    import asyncio

    from buddy_proxy.raccoon.provider import _to_anthropic_openai_stream

    class _FakeStream:
        def __init__(self):
            self.closed = False

        async def aiter_lines(self):
            for line in [
                'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}',
                'data: {"choices":[{"delta":{"content":"OK"}}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":15,'
                '"completion_tokens":3}}',
                "data: [DONE]",
            ]:
                yield line

        async def aclose(self):
            self.closed = True

    async def run():
        stream = _FakeStream()
        out = []
        async for ev in _to_anthropic_openai_stream(stream, "sn-glm-5-3-flash"):
            out.append(ev)
        return out, stream

    events, stream = asyncio.run(run())
    joined = "".join(events)
    assert "message_start" in joined
    assert "content_block_start" in joined, "必须有内容块（否则客户端拿到空回复）"
    assert "content_block_delta" in joined
    assert "OK" in joined, "正文内容要真的转出来"
    assert "content_block_stop" in joined
    assert "message_stop" in joined
    # usage 要透传（message_delta 里带 input/output）
    assert '"input_tokens": 15' in joined or '"input_tokens":15' in joined
    assert stream.closed, "流结束必须 aclose（释放并发 lease）"


def test_anthropic_stream_never_uses_gemini_converter():
    """无论哪个出口，都不能再引入 gemini 的流转换器（同一个坑）。"""
    import inspect

    from buddy_proxy.raccoon.provider import _to_anthropic_openai_stream

    src = inspect.getsource(_to_anthropic_openai_stream)
    assert "AnthropicStreamConverter" in src
    code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("_to_anthropic_stream(" in ln for ln in code_lines), (
        "gemini 的 _to_anthropic_stream 按 Gemini 形状解析，用在这里会产出空流")

def test_account_risk_control_is_account_level_not_model_level():
    """``200108`` 账号风控必须整号长冷却，不能降级成模型级。

    2026-11-23 实测：账号已被风控时，我的代码把它当模型级限流处理，结果
    同一秒打出 4 条 429（每个模型都去撞一次）——持续给上游发「异常调用」
    信号，风控窗口越拉越长。

    正确行为：整号冷却 30 分钟，所有模型都不再尝试。
    """
    from buddy_proxy.raccoon import failover

    # 识别
    assert failover.is_account_risk_control(
        429, "200108", "系统检测到当前账号存在异常调用") is True
    assert failover.is_account_risk_control(
        429, "", "200108 系统检测到当前账号存在异常调用") is True
    # 模型级限流不能被误判成账号风控
    assert failover.is_account_risk_control(
        429, "user_model_rate_limited", "") is False

    # 冷却时长：30 分钟（不是模型级那种 60 秒）
    assert failover._RISK_CONTROL_COOLDOWN_S == 1800.0


# ---------------------------------------------------------------------------
# 7. lease 泄漏：客户端断开时必须释放并发名额
# ---------------------------------------------------------------------------

def test_replay_stream_releases_lease_when_settlement_is_cancelled():
    """客户端断开（cancel）时 lease 必须释放——否则名额被永久吃掉。

    2026-11-23 用户实报「小浣熊越来越慢，最后全是 429」的根因：

    ``_ReplayStream.aclose`` 原来是「先 await 余额结算（网络 IO），再在
    finally 里释放 lease」。客户端中途断开时 Starlette 会 cancel 这个生成器，
    ``CancelledError`` 正好打在结算那个 await 上——而它继承 ``BaseException``，
    ``except Exception`` 兜不住，于是跳过了 ``lease.release()``。

    每断开一次漏一个名额（每账号上限 2），漏满就**永久**拿不到，全通道停摆。
    症状极具误导性：上游完全正常（直连 1.9s），本地却排队 90s 报「并发排队
    超时」，看起来像账号被风控。

    这条测试锁「cancel 打到结算上，名额仍要还回来」。实现要点：结算必须
    卡在一个**可被取消的 async 点**上（``asyncio.Event().wait()``）。早先
    版本用 ``time.sleep`` 在线程池里模拟，取消时线程照跑不误、``finally``
    仍会执行，于是测试在**有 bug 的实现上也绿**——反向验证过才发现的。
    """
    from buddy_proxy.raccoon.provider import _ReplayStream

    limiter = AccountConcurrencyLimiter(max_concurrent=2, queue_timeout_s=0.1)
    gate = asyncio.Event()          # 永不 set：结算就停在这里等取消

    class _Resp:
        is_closed = False

        async def aclose(self):
            self.is_closed = True

    class _Provider:
        def _balance_now(self, token):
            return 1.0

        def _settle_credit(self, *a, **k):
            pass

    async def run():
        lease = await limiter.acquire("acct")
        assert lease is not None
        stream = _ReplayStream(
            _Resp(), [], lines=None, lease=lease, provider=_Provider(),
            access_token="tok", balance_before=1.0,
        )
        # 把结算的 await 换成永不完成的 gate（真实的 to_thread 无法被 cancel，
        # 用它测不出「取消打在 await 上」这个形态）。
        async def _blocked():
            await gate.wait()

        stream._settle_and_close = _blocked          # type: ignore[method-assign]
        task = asyncio.create_task(stream.aclose())
        await asyncio.sleep(0.05)                    # 让它进到 gate.wait()
        task.cancel()                                # 客户端断开
        try:
            await task
        except asyncio.CancelledError:
            pass
        return limiter.inflight("acct")

    left = asyncio.run(run())
    assert left == 0, (
        f"cancel 后仍有 {left} 个名额没还——lease 泄漏了，"
        f"漏满 max_concurrent 后整个通道会永久 429"
    )


# ---------------------------------------------------------------------------
# 12. 设备绑定（200108 风控的真正根因）
# ---------------------------------------------------------------------------

def test_bind_device_body_uses_upstream_field_names():
    """绑定请求体字段名/枚举值都是上游契约，写错就绑不上（= 迟早被风控）。

    2026-11-23 用户问「这个异常是由什么引发的，得定位出来、修掉，不然新号
    还是会被风控」。挖出来的根因：原来发的是 ``{"device_id": ...}``，而上游
    要 ``client_device_id``——绑定请求**一直 400**，设备从未绑上，``user_info``
    永远 ``trusted: false / reason: session_without_bound_device``。所有模型
    调用都来自未绑定设备的会话，累积后整号风控。

    这条测试锁住那串逐个试出来的字段（少一个上游就回 params_empty_error）。
    """
    from buddy_proxy.raccoon.upstream import bind_device_body

    body = bind_device_body("dev-abc")
    required = {
        "client_device_id", "client_device_id_source", "client_platform",
        "client_version", "device_name", "os", "os_version", "application",
        "push_permission",
    }
    missing = required - set(body)
    assert not missing, f"绑定请求体缺字段（上游会回 params_empty_error）：{missing}"
    assert body["client_device_id"] == "dev-abc"
    # 枚举值的坑：只认 denied，且 os_version 必须是版本号格式
    assert body["push_permission"] == "denied", \
        "push_permission 只认 denied，default/granted 会 invalid_push_endpoint"
    assert body["os_version"][0].isdigit(), \
        "os_version 必须是版本号格式，写 'web' 会 format invalid"


def test_chat_headers_carry_device_id():
    """模型调用必须带设备头，否则「已绑定设备 + 陌生会话」仍被判异常。

    绑定只是第一步：绑定成功后上游按设备认会话，聊天请求却不声明自己属于
    那台设备，等于未绑定设备在调模型——风控照样触发。
    """
    from buddy_proxy.raccoon.upstream import build_headers

    bare = build_headers("tok")
    assert "X-Client-Device-ID" not in bare, "不给 device_id 时不带设备头（兼容旧调用）"

    full = build_headers("tok", "dev-xyz")
    assert full["X-Client-Device-ID"] == "dev-xyz"
    for key in ("X-Client-Platform", "X-Client-Version", "X-Raccoon-Language"):
        assert key in full, f"聊天请求缺浏览器指纹头 {key}"
    # 认证双形态不能被设备头挤掉
    assert full["Authorization"] == "Bearer tok"
    assert "xhx_token=tok" in full["Cookie"]


def test_page_boot_and_chat_share_the_same_device_fingerprint():
    """签到（页面启动）与聊天的设备头必须一致。

    两边不一致上游会当成两台设备，等于自己把绑定作废。
    """
    from buddy_proxy.raccoon.upstream import build_headers, page_boot_requests

    chat = build_headers("tok", "dev-xyz")
    boot = page_boot_requests("tok", "dev-xyz", "https://example.com")
    boot_headers = boot[0][2]
    for key in ("X-Client-Device-ID", "X-Client-Platform", "X-Client-Version",
                "X-Raccoon-Language", "Accept-Language"):
        assert chat.get(key) == boot_headers.get(key), \
            f"{key} 在聊天与页面启动之间不一致（上游会当两台设备）"


def test_ensure_device_bound_is_best_effort(monkeypatch):
    """绑定失败不能阻断转发——它是加固动作，不是前置条件。"""
    from buddy_proxy.raccoon import provider as prov

    prov._bound_ok.clear()
    prov._bind_retry_after.clear()

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(prov.httpx, "Client", boom)
    # 不抛异常
    prov._ensure_device_bound("acct-x", "tok")
    assert "acct-x" not in prov._bound_ok, "失败不该标记为已绑定"
    assert "acct-x" in prov._bind_retry_after, "失败要记重试时刻，别每个请求都撞"
