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
    """签到状态/领取都要带 ``credits``，日历侧边栏靠它显示「加了多少」。"""
    import inspect

    from buddy_proxy.raccoon.provider import RaccoonProvider

    assert getattr(RaccoonProvider, "supports_checkin", False) is True
    src_status = inspect.getsource(RaccoonProvider.checkin_status)
    src_claim = inspect.getsource(RaccoonProvider.checkin_claim)
    assert '"credits": credits' in src_status, "status 要带 credits"
    assert '"credits": credits' in src_claim, "claim 要带 credits（日历取这个值）"

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

def test_request_credit_is_multiplier_not_token_based():
    """每笔消耗 = ``1 × 模型倍率``，**与 token 量无关**。

    用户实报「日志里会有小浣熊的积分消息信息吧」——之前 credit 全是 None，
    因为我没接结算。小浣熊按**请求笔数**计费，走 token 估算会得出与账单完全
    不符的数字（它的倍率就是「这笔扣几分」）。
    """
    from buddy_proxy.raccoon.provider import RaccoonProvider

    assert RaccoonProvider._request_credit({"multiplier": 0.75}) == 0.75
    assert RaccoonProvider._request_credit({"multiplier": 0}) == 0.0
    assert RaccoonProvider._request_credit({"multiplier": 1.0}) == 1.0
    # 拿不到倍率时返回 None（面板记 —），不拿 0 冒充免费
    assert RaccoonProvider._request_credit({}) is None
    assert RaccoonProvider._request_credit({"multiplier": True}) is None


def test_credit_marked_exact_not_estimated():
    """写进 frame 时要标 ``ag_credit_exact``：这是精确规则，不是估算。

    不标的话 observability 会把它当 antigravity 那种水位落差估算，面板上
    多一个「估算」标记——而小浣熊的倍率是上游明文规则。
    """
    import inspect

    from buddy_proxy.raccoon.provider import RaccoonProvider

    src = inspect.getsource(RaccoonProvider.forward)
    assert 'frame["ag_credit"] = credit' in src
    assert 'frame["ag_credit_exact"] = True' in src
