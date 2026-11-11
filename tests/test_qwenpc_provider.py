"""qwenpc（千问办公 MaaS）通道：模型名归一、协议路由、凭证链路。

## 为什么这些断言值得同时存在

这个通道的坑几乎都在「**看起来该报错，实际回的是 invalid_request**」上：

- 模型名大小写不对 → `invalid_request`（不提"模型不存在"）
- `accessKey` 直接调模型（没走 mint/exchange）→ `invalid_request`
- anthropic 请求用了转过形的 body → `400`

所以测试重点不是"能跑通"，而是**把这些静默失败点钉死**。
"""

from __future__ import annotations

import json

import pytest

from buddy_proxy.qwenpc import credentials as creds
from buddy_proxy.qwenpc.provider import (
    FALLBACK_MODELS,
    QwenpcProvider,
    provider_models,
    resolve_model,
)


# ---------------------------------------------------------------------------
# 模型名：必须全小写
# ---------------------------------------------------------------------------

def test_resolve_model_lowercases():
    """上游只认全小写；UI 显示的是 `Qwen3.8-Max`，用户会直接抄过来。"""
    assert resolve_model("Qwen3.8-Max") == "qwen3.8-max"
    assert resolve_model("QWEN3.8-MAX") == "qwen3.8-max"
    assert resolve_model("qwen3.8-max") == "qwen3.8-max"


def test_resolve_model_strips_provider_prefix():
    assert resolve_model("qwenpc/Qwen3.8-Max") == "qwen3.8-max"
    # 别的通道前缀不动（那是别人家的模型名）
    assert resolve_model("kiro/claude-x") == "kiro/claude-x"


def test_resolve_model_handles_empty():
    assert resolve_model("") == ""
    assert resolve_model(None) == ""  # type: ignore[arg-type]


def test_models_all_lowercase():
    for m in provider_models():
        assert m["id"] == m["id"].lower(), f"模型 id 必须全小写: {m['id']}"


def test_accepts_model_case_insensitive():
    """客户端写大写也该路由到本通道（内部会归一）。"""
    p = QwenpcProvider()
    assert p.accepts_model("qwen3.8-max")
    assert p.accepts_model("Qwen3.8-Max")
    assert p.accepts_model("qwenpc/Qwen3.8-Max")
    assert not p.accepts_model("claude-opus-5")


def test_fallback_models_shape():
    """兜底表得有上游真在卖的模型，且字段齐全。"""
    ids = {m["id"] for m in FALLBACK_MODELS}
    assert "qwen3.8-max" in ids, "主推模型必须在表里"
    for m in provider_models():
        assert m["object"] == "model"
        assert m["owned_by"] == "qwenpc"
        assert m["name"]


# ---------------------------------------------------------------------------
# 凭证链路
# ---------------------------------------------------------------------------

def test_jwt_expiry_parsed():
    """JWT 的 exp 要能解出来——它决定多久需要重新抓一次。"""
    # {"exp": 1794856587} 的 base64url（无签名，只要 payload 段能解）
    import base64
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": 1794856587}).encode()).decode().rstrip("=")
    jwt = f"header.{payload}.sig"
    assert creds._jwt_expiry(jwt) == 1794856587.0


def test_jwt_expiry_garbage_returns_zero():
    for bad in ("", "x", "a.b", "not.a.jwt"):
        assert creds._jwt_expiry(bad) == 0.0


def test_token_cache_roundtrip(tmp_path, monkeypatch):
    """缓存要能存能读，且带 base（不同账号 base 可能不同）。"""
    target = tmp_path / "tok.json"
    monkeypatch.setenv("QWENPC_TOKEN_FILE", str(target))
    creds._save_cache({"token": "sk-mm-v1.x", "expires_at": 1e12,
                       "base": "https://api9.qwenwork.cn"})
    cache = creds._load_cache()
    assert cache["token"] == "sk-mm-v1.x"
    assert cache["base"] == "https://api9.qwenwork.cn"
    # 文件权限 0600（凭据类文件不该被其它用户读）
    assert (target.stat().st_mode & 0o777) == 0o600


def test_cache_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPC_TOKEN_FILE", str(tmp_path / "nope.json"))
    assert creds._load_cache() == {}


def test_cache_corrupt_returns_empty(tmp_path, monkeypatch):
    target = tmp_path / "bad.json"
    target.write_text("not json")
    monkeypatch.setenv("QWENPC_TOKEN_FILE", str(target))
    assert creds._load_cache() == {}


def test_protocol_paths_cover_both():
    """两条协议都必须有上游路径——原生支持是我们选它的理由。"""
    from buddy_proxy.qwenpc.provider import _PROTOCOL_PATHS
    assert set(_PROTOCOL_PATHS) == {"openai", "anthropic"}
    assert _PROTOCOL_PATHS["openai"] == "/v1/chat/completions"
    assert _PROTOCOL_PATHS["anthropic"] == "/v1/messages"


def test_responses_protocol_rejected():
    """上游没有 /v1/responses，要回 501 并说清改走哪条。"""
    import asyncio
    from fastapi import HTTPException

    p = QwenpcProvider()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(p.forward({"model": "qwen3.8-max"}, "responses"))
    assert exc.value.status_code == 501
    msg = json.dumps(exc.value.detail, ensure_ascii=False)
    assert "openai" in msg and "anthropic" in msg


def test_ensure_auth_does_not_launch_app():
    """启动阶段不能抓 JWT——那会把千问 App 拉起来，副作用太重。"""
    p = QwenpcProvider()
    assert p.ensure_auth() is None   # 不抛、不连 App


def test_health_reports_cache_state(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPC_TOKEN_FILE", str(tmp_path / "t.json"))
    creds._save_cache({"token": "x", "expires_at": 1e12, "base": "b"})
    h = QwenpcProvider().health()
    assert h["cached_token"] is True
    assert h["token_expires_in_s"] is not None


# ---------------------------------------------------------------------------
# 免调起 App 的路径（用户明确要求）
# ---------------------------------------------------------------------------
#
# 用户的诉求：千问不运行时也能正常请求，**只在 JWT 过期（~6 天）时才调起一次**。
# 所以 `auth_token()` 的取用顺序必须严格是：
#
#   token 有效 → 直接用            （不碰 App）
#   token 过期 → 自刷新            （不碰 App）
#   JWT 有效   → mint             （不碰 App）
#   JWT 也过期 → CDP 抓 JWT        （这时才调起 App）
#
# 下面几条把「哪一步不该碰 App」钉死——退化成「每次都抓 JWT」会让 App 被反复
# 拉起，用户会立刻感知到。

def test_valid_token_never_touches_app(tmp_path, monkeypatch):
    """缓存 token 有效时，连网络都不该请求——直接返回。"""
    monkeypatch.setenv("QWENPC_TOKEN_FILE", str(tmp_path / "t.json"))
    creds._save_cache({"token": "sk-mm-v1.CACHED", "expires_at": 1e12,
                       "base": "https://api9.qwenwork.cn"})

    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("不该走到这里")

    monkeypatch.setattr(creds, "capture_jwt", boom)
    monkeypatch.setattr(creds, "mint_token", boom)
    monkeypatch.setattr(creds, "renew_token", boom)

    assert creds.auth_token() == "sk-mm-v1.CACHED"
    assert called["n"] == 0


def test_expired_token_renews_without_app(tmp_path, monkeypatch):
    """token 过期但能自刷新时，不该调起 App。"""
    monkeypatch.setenv("QWENPC_TOKEN_FILE", str(tmp_path / "t.json"))
    creds._save_cache({"token": "sk-mm-v1.OLD", "expires_at": 1000.0,
                       "base": "https://api9.qwenwork.cn"})

    monkeypatch.setattr(creds, "renew_token", lambda t, b: {
        "token": "sk-mm-v1.NEW", "expires_at": 1e12, "base": b})
    monkeypatch.setattr(creds, "capture_jwt",
                        lambda *a, **k: pytest.fail("不该调起 App"))
    assert creds.auth_token() == "sk-mm-v1.NEW"


def test_renew_failure_falls_back_to_mint_not_app(tmp_path, monkeypatch):
    """自刷新失败但 JWT 还有效 → 用 JWT mint，仍然不调起 App。"""
    monkeypatch.setenv("QWENPC_TOKEN_FILE", str(tmp_path / "t.json"))
    import time as _t
    creds._save_cache({"token": "sk-mm-v1.DEAD", "expires_at": 1000.0,
                       "base": "https://api9.qwenwork.cn",
                       "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjQxMDI0NDQ4MDB9.x",
                       "jwt_expires_at": _t.time() + 86400})

    def bad_renew(t, b):
        raise creds.CredentialError("401")

    monkeypatch.setattr(creds, "renew_token", bad_renew)
    monkeypatch.setattr(creds, "mint_token", lambda j: {
        "token": "sk-mm-v1.MINTED", "expires_at": 1e12,
        "base": "https://api9.qwenwork.cn"})
    monkeypatch.setattr(creds, "capture_jwt",
                        lambda *a, **k: pytest.fail("不该调起 App"))
    assert creds.auth_token() == "sk-mm-v1.MINTED"


def test_expired_jwt_is_the_only_path_that_captures(tmp_path, monkeypatch):
    """只有 JWT 也过期时才抓（= 调起 App）。"""
    monkeypatch.setenv("QWENPC_TOKEN_FILE", str(tmp_path / "t.json"))
    import time as _t
    creds._save_cache({"token": "sk-mm-v1.DEAD", "expires_at": 1000.0,
                       "base": "https://api9.qwenwork.cn",
                       "jwt": "old", "jwt_expires_at": _t.time() - 10})

    monkeypatch.setattr(creds, "renew_token",
                        lambda t, b: (_ for _ in ()).throw(creds.CredentialError("x")))
    monkeypatch.setattr(creds, "capture_jwt", lambda *a, **k: "eyJNEW")
    monkeypatch.setattr(creds, "mint_token", lambda j: {
        "token": "sk-mm-v1.NEW", "expires_at": 1e12,
        "base": "https://api9.qwenwork.cn"})
    assert creds.auth_token() == "sk-mm-v1.NEW"


def test_mint_result_is_cached_with_jwt(tmp_path, monkeypatch):
    """mint 成功后要把 JWT 一起存下来——否则下次又得抓一次。"""
    monkeypatch.setenv("QWENPC_TOKEN_FILE", str(tmp_path / "t.json"))
    monkeypatch.setattr(creds, "capture_jwt", lambda *a, **k: "eyJJWT")
    monkeypatch.setattr(creds, "mint_token", lambda j: {
        "token": "sk-mm-v1.T", "expires_at": 1e12, "base": "https://b"})
    creds.auth_token()
    cache = creds._load_cache()
    assert cache["jwt"] == "eyJJWT"
    assert cache["jwt_expires_at"] > 0
    assert cache["token"] == "sk-mm-v1.T"


# ---------------------------------------------------------------------------
# 积分反推：靠额度差，且要跨过上游的结算抖动
# ---------------------------------------------------------------------------
#
# 上游**不提供**单次计费字段，只能「调用前后各查一次额度、取差值」。而它的
# 额度接口有个固定怪癖（实测反复确认，三轮完全一致）：
#
#     调用前        384.1131
#     +0.0s        364.1130   ← 比真实值低整整 20（结算中间值）
#     +0.4s        384.0773   ← 真实值
#     +0.8s        383.7585   ← 还会再降一点（二次结算）
#
# 所以：after 要跳过「低 20」的抖动值、取非抖动采样里的最小值；before 也
# 可能踩到抖动值（那时差值是 `-20 + 真实消耗`），用固定幅度修正回来。

def test_spent_credit_normal():
    from buddy_proxy.qwenpc.provider import _spent_credit
    assert _spent_credit(389.88, 389.84) == 0.04


def test_spent_credit_corrects_jittered_baseline():
    """before 踩了抖动值（偏小 20）时，差值约 -20，要加回来。"""
    from buddy_proxy.qwenpc.provider import _spent_credit
    got = _spent_credit(369.86, 389.83)
    assert got is not None and 0.01 < got < 0.10, f"应修正为真实消耗，实际 {got}"


def test_spent_credit_rejects_insane():
    from buddy_proxy.qwenpc.provider import _spent_credit
    assert _spent_credit(389.88, 300.0) is None      # 差 89
    assert _spent_credit(None, 389.0) is None
    assert _spent_credit(389.0, None) is None
    assert _spent_credit(389.0, 389.0) is None       # 无消耗


def test_is_jitter():
    from buddy_proxy.qwenpc.provider import _is_jitter
    assert _is_jitter(364.113, 384.113) is True      # 低 20
    assert _is_jitter(384.077, 384.113) is False     # 正常消耗
    assert _is_jitter(None, 384.0) is False


def test_quota_method_shape():
    """额度卡：只报剩余量。

    ⚠ **不要写 `used`/`total` 键**（哪怕值是 None）。前端 ``quotaRemaining()``
    的判定是「这两个键存在但不是数字 → 当作查不到额度」，会让卡片挂上
    「无剩余额度」红标、且画不出进度条（用户 2026-11-11 报障用）。
    语义上「上游没有这个字段」就该**不出现**，而不是出现一个 null。
    """
    from buddy_proxy.qwenpc.provider import QwenpcProvider
    q = QwenpcProvider().quota()
    if q is None:
        pytest.skip("上游不可达")
    assert "items" in q and q["items"]
    item = q["items"][0]
    assert isinstance(item["remaining"], (int, float))
    assert item["remaining"] > 0
    assert "used" not in item, "used 键存在会让前端判成查不到额度"
    assert "total" not in item, "total 键存在会让前端判成查不到额度"
    assert "percent" not in item, "没有分母就不该给百分比"
    assert item["unit"] == "credit"


def test_credit_chunk_is_valid_sse():
    """补发的 usage 帧要符合 OpenAI 流式规范（choices 为空）。"""
    import json as _json
    from buddy_proxy.qwenpc.provider import _credit_chunk
    raw = _credit_chunk(0.0374).decode()
    assert raw.startswith("data: ") and raw.endswith("\n\n")
    payload = _json.loads(raw[len("data: "):].strip())
    assert payload["choices"] == []
    assert payload["usage"]["credit"] == 0.0374


# ---------------------------------------------------------------------------
# 倍率：实测标定值（不是估的）
# ---------------------------------------------------------------------------
#
# 上游没有任何「模型单价」接口，只能靠调用前后的额度差反推。反推有两个坑，
# 标定时必须控住：
#
# 1. **缓存命中大幅打折**：同一 prompt 第二次（cached_tokens 命中 87%）
#    扣费只有第一次的 1/3.7。不控住的话测出来的数字偏低且不稳定。
# 2. **结算抖动**：调用刚结束时读到的是临时值（低 5 或低 20）。
#
# 标定方法：固定 1600 字随机输入、max_tokens=1、每模型 2 次取均值、
# 只留 cached_tokens == 0 的样本。

def test_credit_rates_match_calibration():
    """倍率必须与实测标定值一致（防止有人凭感觉改）。"""
    from buddy_proxy.qwenpc.provider import FALLBACK_MODELS

    expected = {
        "qwen3.8-max": 1.0,
        "qwen3.8-flash": 0.12,
        "glm-5.3": 0.52,
        "glm-5.3-flash": 0.04,
        "deepseek-v4-pro": 0.42,
        "deepseek-v4-flash": 0.05,
        "kimi-k3.1-pro": 2.31,
        "kimi-k3.1-pro-highspeed": 4.63,
    }
    actual = {m["id"]: m["credits"] for m in FALLBACK_MODELS}
    assert actual == expected, f"倍率与标定不符: {actual}"


def test_highspeed_is_most_expensive():
    """kimi-k3.1-pro-highspeed 是最贵的（4.63），别被名字骗了当成免费档。"""
    from buddy_proxy.qwenpc.provider import FALLBACK_MODELS

    by_id = {m["id"]: m["credits"] for m in FALLBACK_MODELS}
    assert by_id["kimi-k3.1-pro-highspeed"] == max(by_id.values())
