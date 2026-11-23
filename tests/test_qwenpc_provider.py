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
# 倍率：与客户端面板 & 网关 /api/desktop/chat-modes 对齐（2026-11-17）
# ---------------------------------------------------------------------------

def test_credit_rates_match_panel_and_catalog():
    """倍率与读图标志必须与面板/chat-modes 核对值一致。"""
    from buddy_proxy.qwenpc.provider import FALLBACK_MODELS

    expected = {
        "qwen4-max": (1.6, True),
        "qwen3.8-max": (1.8, True),
        "qwen3.8-flash": (0.1, True),
        "ling-3.1-flash": (0.1, False),
        "deepseek-v4.1-flash": (0.1, True),
        "deepseek-v4-pro": (0.2, False),
        "glm-5.3": (1.2, False),
        "glm-5.3-flash": (0.2, False),
        "kimi-k3.1-pro": (2.4, True),
        "minimax-m3.1-flash": (0.2, True),
        "step-5-preview": (0.7, True),
    }
    actual = {m["id"]: (m["credits"], bool(m.get("images"))) for m in FALLBACK_MODELS}
    assert actual == expected, f"模型/倍率与面板不符: {actual}"


def test_new_models_accepts_and_context_window():
    """2026-11-17 新增模型均已收录，且全部带 1M context_window。"""
    p = QwenpcProvider()
    for mid in ("qwen4-max", "ling-3.1-flash", "deepseek-v4.1-flash",
                "minimax-m3.1-flash", "step-5-preview"):
        assert p.accepts_model(mid), mid
        assert p.accepts_model(f"qwenpc/{mid}"), mid
    for legacy in ("qwen3.8-max-preview", "deepseek-v4-flash",
                   "kimi-k3.1-pro-highspeed"):
        assert p.accepts_model(legacy)
        assert not p.accepts_model(legacy, aliases=False)
    by_id = {m["id"]: m for m in p.models()}
    assert "deepseek-v4-flash" not in by_id
    assert "kimi-k3.1-pro-highspeed" not in by_id
    assert all(m.get("context_window") == 1_000_000 for m in by_id.values())
    assert by_id["kimi-k3.1-pro"]["credits"] == max(
        m["credits"] for m in by_id.values()
    )

# 大请求的积分：上界必须随 token 数增长
# ---------------------------------------------------------------------------
#
# 真实扣费**不是固定量级**，它随请求规模线性增长（同一模型实测）：
#
#     输入 token    扣费     每千 token
#     2 088       0.118    0.057
#     10 088      1.353    0.134
#     40 089     15.929    0.397
#
# 旧版把上界写死 5.0，于是 4 万 token 的请求（真实扣 15.9）被当成抖动丢掉，
# 日志里只剩 None —— 这正是用户「qwenpc 日志里没有积分」的直接原因。

def test_max_sane_credit_scales_with_tokens():
    from buddy_proxy.qwenpc.provider import _max_sane_credit

    small = _max_sane_credit(100)
    large = _max_sane_credit(40_089)
    assert large > small, "上界必须随 token 数增长"
    assert large > 15.9, f"4 万 token 的真实扣费是 15.9，上界要覆盖它（实际 {large}）"
    # 无 token 数据时退回兜底常量
    assert _max_sane_credit(None) == _max_sane_credit(0)


def test_spent_credit_accepts_large_request():
    """4 万 token 的真实扣费（15.9）不能被当成异常丢掉。"""
    from buddy_proxy.qwenpc.provider import _spent_credit

    got = _spent_credit(276.4676, 260.539, tokens=40_089)
    assert got is not None, "大请求的合理扣费被判成异常了"
    assert 15.0 < got < 16.5, f"应约 15.93，实际 {got}"


def test_jitter_is_filtered_by_quota_after_not_by_magnitude():
    """抖动靠 `_quota_after` 的「取最大值」过滤，不是靠 magnitude 阈值。

    为什么不能靠 magnitude：上界随 token 数放宽后（4 万 token → 49），一个
    -5 的抖动差值在数值上「完全合理」，没法用大小区分。真正可靠的判据是
    **抖动的方向**——它只会「先低后高」，所以同一段采样取最大值就避开了它。

    这条测试锁住分工：`_spent_credit` 只做最终合理性（非正/超上界），
    抖动识别是 `_quota_after` 的职责。两者混在一起会让放宽上界时误伤。
    """
    from buddy_proxy.qwenpc.provider import _is_jitter, _spent_credit

    # 抖动特征（-5）能被识别出来
    assert _is_jitter(271.4, 276.4) is True
    # 但 _spent_credit 对它不设防——这是设计取舍，不是遗漏
    assert _spent_credit(276.4, 271.4, tokens=40_089) == 5.0


# --- 常量唯一性 -------------------------------------------------------------


def test_sane_credit_constants_defined_exactly_once():
    """``_MAX_SANE_CREDIT`` / ``_CREDIT_PER_1K_TOKENS_CAP`` 只能各定义一次。

    2026-11-24 实测发现：`#235` 想把上界从 5.0 降到 1.0，但**只加了一行新的、
    没删旧的**，于是源码里出现两个 ``_MAX_SANE_CREDIT``（5.0 在前、1.0 在后）。
    Python 后定义覆盖前定义，行为恰好正确——所以测试全绿、没人发现。但读代码的
    人会以为生效的是 5.0，改注释/改逻辑时按错的假设走。

    这类「赋值重复」单测本来抓不到（行为不变），只能静态数源码里的赋值次数。
    允许注释里提到名字（那份说明本身有价值），所以按**行首赋值**匹配。
    """
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "src/buddy_proxy/qwenpc/provider.py").read_text(encoding="utf-8")
    for name in ("_MAX_SANE_CREDIT", "_CREDIT_PER_1K_TOKENS_CAP", "_JITTER_DROPS"):
        hits = re.findall(rf"^{name}\s*=", src, flags=re.M)
        assert len(hits) == 1, f"{name} 被定义了 {len(hits)} 次（应恰好 1 次）"


def test_sane_credit_floor_is_one_not_five():
    """兜底上界必须是 1.0。

    历史：旧版写死 5.0，4 万 token 的请求（真实扣 15.9）被当成抖动丢掉，
    日志里只剩 None。降到 1.0 后，大请求靠 ``_max_sane_credit`` 按 token 数
    动态放大（实测 40k token → 49.1），小请求仍受 1.0 兜底约束。
    """
    from buddy_proxy.qwenpc.provider import _MAX_SANE_CREDIT, _max_sane_credit

    assert _MAX_SANE_CREDIT == 1.0
    assert _max_sane_credit(None) == 1.0, "拿不到 token 数时用兜底值"
    # 大请求必须被放大，否则会重演「真实消耗被当抖动丢掉」
    assert _max_sane_credit(40089) > 15.929, "4 万 token 的真实消耗 15.9 必须能通过"
