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
        # glm-5.4 的 images=True 是实测结论：网关目录 is_vl 写着 false，
        # 但红/绿/黄/蓝四种纯色图全部认对；同门 glm-5.3 传图全 400。
        "glm-5.4": (1.2, True),
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


# ---------------------------------------------------------------------------
# 流式透传：chunk 是 bytes，正则必须是 bytes 模式
# ---------------------------------------------------------------------------

class _FakeResp:
    """最小的 httpx.Response 替身：只实现 ``aiter_bytes`` / ``aclose``。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c

    async def aclose(self):
        self.closed = True


def test_passthrough_handles_bytes_chunks(monkeypatch):
    """流式 chunk 是 bytes，``_USAGE_TOKENS_RE`` 必须能直接 search 它。

    2026-11-12~24 线上事故：正则写成了 str 模式，上游一发 usage 帧就抛
    ``TypeError: cannot use a string pattern on a bytes-like object``，
    qwenpc **所有模型**的流式请求 500（实测 glm-5.4 流式 7/7 挂、非流式全好）。
    这里喂真实的 bytes（usage 被**拆在两个 chunk**里，模拟 TCP 分片），
    旧代码当场红。

    用 ``asyncio.run`` 而非 ``pytest.mark.asyncio``：仓库没装 pytest-asyncio，
    与 test_raccoon_channel 的写法保持一致。
    """
    import asyncio

    from buddy_proxy.qwenpc import provider as mod

    async def _no_quota(_before):
        return None

    monkeypatch.setattr(mod, "_quota_after", _no_quota)
    resp = _FakeResp([
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":17,"completion_tok',
        b'ens":3,"total_tokens":20}}\n\n',
        b'data: [DONE]\n\n',
    ])

    async def run():
        return [c async for c in
                mod.QwenpcProvider()._passthrough(None, resp, before=1.0)]

    out = asyncio.run(run())

    # 1) 没抛异常；4 个原始 chunk（含 usage 帧被拆成的两个半截）全部原样透传，
    #    拼回来是 3 个完整的 data: 帧——注意别按 chunk 数去数 data:，分片会让
    #    两个 chunk 才凑成一个帧（我第一版断言就是这么数错的）。
    assert len(out) == 4
    assert all(isinstance(c, bytes) for c in out)
    joined = b"".join(out)
    assert joined.count(b"data: ") == 3
    assert b'"total_tokens":20' in joined, "usage 帧的两个半截必须都透传、不丢字节"
    # 2) 跨 chunk 分片的 total_tokens 也能读出来——bytes 模式才不炸
    assert mod._USAGE_TOKENS_RE.search(b'"total_tokens":20}') is not None
    # 3) 上游流被关掉，不漏连接
    assert resp.closed is True


# ---------------------------------------------------------------------------
# credit_derived：余额差推算的 credit 必须被标成「估算」（面板带 ≈）
# ---------------------------------------------------------------------------

def test_normalize_usage_passes_credit_derived_only_when_true():
    """``credit_derived`` 只有字面 True 才透传——它是本地推算的标记，不是上游字段。"""
    from buddy_proxy.core.metrics_protocol import normalize_usage

    base = {"prompt_tokens": 1, "completion_tokens": 1, "credit": 0.5}
    assert normalize_usage({**base, "credit_derived": True}).get("credit_derived") is True
    # 没标 / 标成脏值 → 不透传，按接口实扣处理
    assert "credit_derived" not in normalize_usage(dict(base))
    assert "credit_derived" not in normalize_usage({**base, "credit_derived": "false"})
    assert "credit_derived" not in normalize_usage({**base, "credit_derived": 1})
    # 没有 credit 时标记无意义，不透传
    assert "credit_derived" not in normalize_usage(
        {"prompt_tokens": 1, "credit_derived": True})


def test_credit_or_estimate_marks_derived_credit_as_estimated():
    """provider 往 usage 里塞推算值时，observability 必须记成估算。

    这是「不是接口返回的积分一律带 ≈」在代码里的落点：provider 标了
    ``credit_derived`` 的 credit 不能冒充上游实扣。
    """
    from buddy_proxy.codebuddy_provider.observability import _credit_or_estimate

    norm = {"prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 0,
            "credit": 0.0031, "credit_derived": True}
    credit, estimated = _credit_or_estimate("qwenpc", "glm-5.4", norm)
    assert credit == 0.0031
    assert estimated is True, "余额差推算的 credit 必须标估算（面板带 ≈）"

    # 对照：接口真返回的 credit（kiro/qoder 形态）不带标记 → 精确
    exact = {"prompt_tokens": 10, "completion_tokens": 2, "credit": 0.0031}
    assert _credit_or_estimate("qoder", "x", exact) == (0.0031, False)


def test_sse_usage_extractor_merges_credit_derived_as_flag():
    """SSE 多帧合并时 ``credit_derived`` 按「或」合并，不能被 max 抹掉。

    qwenpc 流式：上游先发自己的 usage 帧（无标记），我们收尾再补一帧带
    ``credit_derived`` 的。按数值 ``max(cur, v or 0)`` 合并会让后到的 True
    被吞掉（cur 为 False/None 时 max 退化为 0），面板就冒充实扣了。
    """
    from buddy_proxy.core.metrics_protocol import SSEUsageExtractor

    ex = SSEUsageExtractor()
    ex.feed(b'data: {"choices":[{"delta":{"content":"a"}}],"usage":{"prompt_tokens":5,"completion_tokens":1,"total_tokens":6}}\n\n')
    assert ex.usage.get("credit_derived") is not True
    ex.feed(b'data: {"choices":[],"usage":{"credit":0.0031,"credit_derived":true}}\n\n')
    assert ex.usage.get("credit_derived") is True, "后到的 credit_derived 标记不能被合并逻辑抹掉"
    assert ex.usage.get("credit") == 0.0031

    # 反向：先带标记、后到无标记 → 标记必须保留
    ex2 = SSEUsageExtractor()
    ex2.feed(b'data: {"choices":[],"usage":{"credit":0.0031,"credit_derived":true}}\n\n')
    ex2.feed(b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\n')
    assert ex2.usage.get("credit_derived") is True


def test_qwenpc_usage_credit_is_derived():
    """qwenpc 写进 usage 的 credit 必须同时带 ``credit_derived``（非流式 + 流式）。"""
    from buddy_proxy.qwenpc import provider as mod

    # 流式补帧
    import json as _json
    frame = _json.loads(mod._credit_chunk(0.0031).decode()[len("data: "):])
    assert frame["usage"]["credit_derived"] is True

    # 非流式：走 _fill_credit 的写回路径
    class _P(mod.QwenpcProvider):
        pass

    async def _fake_after(_before):
        return 10.0 - 0.0031

    import asyncio
    mod._quota_after = _fake_after
    try:
        payload = {"usage": {"prompt_tokens": 20, "completion_tokens": 3}}
        asyncio.run(_P()._fill_credit(payload, before=10.0))
    finally:
        del mod._quota_after
    assert payload["usage"]["credit"] == 0.0031
    assert payload["usage"]["credit_derived"] is True


# ---------------------------------------------------------------------------
# 从上游 /api/desktop/chat-modes 刷新目录
#
# 这条链的坑全在「目录长得不像模型表」上：catalog 里混着 flash/pro 这种模式
# 档位，key 和对外 id 对不上（qwen3.8-max-preview vs qwen3.8-max），展示名在
# i18n 里而不是 key，is_vl 还会写错。任何一条漏处理，刷新一次就把用户已有的
# model_order 配置打散。
# ---------------------------------------------------------------------------

#: 上游 catalog 的真实形状（2026-11-29 实测，截取有代表性的几条）。
_CATALOG_SAMPLE = [
    {"key": "flash", "i18n": {"display_name": {"zh": "标准", "en": "Standard"}},
     "price_factor": 0, "is_vl": True, "enable": True, "max_input_tokens": 1000000},
    {"key": "pro", "i18n": {"display_name": {"zh": "高级", "en": "Pro"}},
     "price_factor": 1, "is_vl": False, "enable": True, "max_input_tokens": 1000000},
    {"key": "qwen3.8-max-preview",
     "i18n": {"display_name": {"zh": "Qwen3.8-Max", "en": "Qwen3.8-Max"}},
     "price_factor": 1.8, "is_vl": True, "enable": True, "max_input_tokens": 1000000},
    {"key": "deepseek-flash",
     "i18n": {"display_name": {"zh": "DeepSeek-V4.1-Flash"}},
     "price_factor": 0.1, "is_vl": True, "enable": True, "max_input_tokens": 1000000},
    {"key": "glm-5.4", "i18n": {"display_name": {"zh": "GLM-5.4"}},
     "price_factor": 1.2, "is_vl": False, "enable": True, "max_input_tokens": 1000000},
    {"key": "disabled-model", "i18n": {"display_name": {"zh": "已停用"}},
     "price_factor": 0.5, "is_vl": False, "enable": False, "max_input_tokens": 1000000},
]


def test_catalog_maps_internal_key_to_public_id():
    """``qwen3.8-max-preview`` 必须映射回 ``qwen3.8-max``。

    不映射的后果不是「多一个模型」，而是**原有配置集体失效**：``model_order``
    里写的是 ``qwenpc/qwen3.8-max``，刷新后这个 id 从目录消失，那条路由直接
    失去候选。
    """
    from buddy_proxy.qwenpc.provider import models_from_catalog

    ids = {m["id"] for m in models_from_catalog(_CATALOG_SAMPLE)}
    assert "qwen3.8-max" in ids
    assert "qwen3.8-max-preview" not in ids, "内部 key 泄漏到对外 id 了"
    assert "deepseek-v4.1-flash" in ids
    assert "deepseek-flash" not in ids


def test_catalog_uses_i18n_display_name_not_key():
    """展示名取 ``i18n.display_name.zh``。

    ``flash`` / ``pro`` 这种 key 直接当名字显示，用户在模型列表里看到的是
    「flash」「pro」，根本不知道那是什么。
    """
    from buddy_proxy.qwenpc.provider import models_from_catalog

    by_id = {m["id"]: m for m in models_from_catalog(_CATALOG_SAMPLE)}
    assert by_id["flash"]["name"] == "标准"
    assert by_id["pro"]["name"] == "高级"


def test_catalog_drops_disabled_entries():
    """``enable: false`` 不能进目录——选了就报错，比不显示更糟。"""
    from buddy_proxy.qwenpc.provider import models_from_catalog

    ids = {m["id"] for m in models_from_catalog(_CATALOG_SAMPLE)}
    assert "disabled-model" not in ids


def test_catalog_keeps_zero_price_factor():
    """``price_factor: 0`` 是**真实免费**（会员限免），不是缺省值。

    写成 ``if pf:`` 会把 0 当假值丢掉，免费模型显示成「倍率未知」。
    """
    from buddy_proxy.qwenpc.provider import models_from_catalog

    by_id = {m["id"]: m for m in models_from_catalog(_CATALOG_SAMPLE)}
    assert by_id["flash"]["credits"] == 0.0
    # 0 必须是「有这个键且值为 0」，不是「键缺失」——两者在下游表现不同：
    # 缺键时界面显示「倍率未知」，有键才是「免费」。
    assert "credits" in by_id["flash"]


def test_catalog_fixes_glm_54_images():
    """``glm-5.4`` 目录写 ``is_vl: false``，实测能读图——必须修正成 True。

    不修正的代价：pi-ai 按 ``input:[text]`` 把图片剥掉，用户发图**直接丢内容
    还不报错**，比报错难查得多。
    """
    from buddy_proxy.qwenpc.provider import models_from_catalog

    by_id = {m["id"]: m for m in models_from_catalog(_CATALOG_SAMPLE)}
    assert by_id["glm-5.4"]["images"] is True


def test_catalog_ignores_garbage_entries():
    """上游给脏数据（非 dict / 无 key）时跳过，不能整个刷新失败。"""
    from buddy_proxy.qwenpc.provider import models_from_catalog

    out = models_from_catalog([None, {}, {"key": ""}, {"key": "ok-model"}])
    assert [m["id"] for m in out] == ["ok-model"]


def test_refresh_models_replaces_directory(monkeypatch):
    """刷新成功后 ``models()`` 换成新目录。"""
    from buddy_proxy.qwenpc import provider as prov

    monkeypatch.setattr(prov.creds, "chat_modes", lambda *a, **k: _CATALOG_SAMPLE)
    p = QwenpcProvider()
    before = {m["id"] for m in p.models()}
    assert "flash" not in before, "静态表里不该有模式档位"

    import asyncio
    models = asyncio.run(p.refresh_models())
    assert "flash" in {m["id"] for m in models}
    assert {m["id"] for m in p.models()} == {m["id"] for m in models}


def test_refresh_models_failure_keeps_old_directory(monkeypatch):
    """拉取失败必须**保留旧目录**，不能清空。

    目录是 ``model_order`` 的路由依据，清空等于所有请求同时失去候选——比
    刷新失败本身严重得多。
    """
    from buddy_proxy.qwenpc import provider as prov

    def _boom(*a, **k):
        raise prov.creds.CredentialError("JWT 过期")

    p = QwenpcProvider()
    # 先成功刷一次，拿到「旧目录」
    monkeypatch.setattr(prov.creds, "chat_modes", lambda *a, **k: _CATALOG_SAMPLE)
    import asyncio
    asyncio.run(p.refresh_models())
    good = [m["id"] for m in p.models()]

    monkeypatch.setattr(prov.creds, "chat_modes", _boom)
    with pytest.raises(RuntimeError, match="JWT"):
        asyncio.run(p.refresh_models())
    assert [m["id"] for m in p.models()] == good, "刷新失败把目录清空了"


def test_refresh_models_empty_catalog_raises(monkeypatch):
    """上游全 disable 时抛错，而不是把目录刷成空表。"""
    from buddy_proxy.qwenpc import provider as prov

    monkeypatch.setattr(prov.creds, "chat_modes",
                        lambda *a, **k: [{"key": "x", "enable": False}])
    p = QwenpcProvider()
    import asyncio
    with pytest.raises(RuntimeError, match="没有可用条目"):
        asyncio.run(p.refresh_models())


def test_provider_is_marked_refreshable():
    """``/ui/api/models`` 靠 ``refresh_models`` 可调用来决定是否显示按钮。

    这个断言锁的是「模型 tab 里有重新拉取按钮」这件事本身——方法名改了或
    被删了，按钮会静默消失，没人会发现。
    """
    p = QwenpcProvider()
    assert callable(getattr(p, "refresh_models", None))
