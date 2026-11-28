"""Loomy provider 测试（完全离线，不访问讯飞上游）。

覆盖：
- 登录态读取与「缺失/空 session」的可操作报错
- baseURL 三级优先级（env → 桌面端运行配置 → 生产兜底）
- ``/models`` 三种 envelope 形状解析 + 倍率抽取（全角/半角括号）
- 目录缓存：空列表不覆盖旧值（上游灰度返回空表时不能把目录清空）
- envelope 业务码：HTTP 200 + code=100002 必须识别成登录失效
- pet-work 档位判定：保守口径，不重复领已领档位
- 积分日志：``usage.points_consumed`` 与倍率都进日志
- 路由归属：``accepts_model`` 认 id / 显示名 / 去倍率名，不误抢别人的模型
- forward：非流式 openai 直通、anthropic 反向转换、登录失效映射成 401

运行：
    .venv/bin/python -m pytest tests/test_loomy_provider.py -v
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from buddy_proxy.loomy import catalog as cat
from buddy_proxy.loomy import credentials as cred
from buddy_proxy.loomy import points as pts
from buddy_proxy.loomy.provider import (
    LoomyProvider,
    _extract_prompt,
    _log_points_usage,
    _raise_for_body,
    _strip_prefix,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

#: 真实 ``/models`` 返回的裁剪版（2026-11-29 生产网关实测）。
MODELS_PAYLOAD: dict[str, Any] = {
    "code": None,
    "data": {
        "data": [
            {
                "id": "spark-x2.6",
                "name": "Spark-X2.6 正式版(x2.5)",
                "type": "chat",
                "protocol": "openai_chat",
                "context_length": 1048576,
                "max_output_tokens": 131072,
                "is_default_model": True,
                "is_default_small_model": True,
                "reasoning_efforts": ["none", "low", "medium", "high", "xhigh"],
                "default_reasoning_effort": "low",
                "capabilities": {
                    "reasoning": True,
                    "vision": False,
                    "function_calling": True,
                    "streaming": True,
                    "input_modalities": ["text"],
                },
            },
            {
                "id": "GLM-5.3-Flash",
                "name": "GLM 5.3 Flash(x0.8)",
                "type": "chat",
                "protocol": "openai_chat",
                "context_length": 1048576,
                "max_output_tokens": 131072,
                "capabilities": {
                    "reasoning": True,
                    "function_calling": True,
                    "input_modalities": ["text"],
                },
            },
            {
                # 全角括号 + 全角字母：实测上游就这么写
                "id": "qwen3.8-flash",
                "name": "qwen 3.8 flash（x0.8）",
                "type": "chat",
                "protocol": "openai_chat",
                "context_length": 1000000,
                "capabilities": {"function_calling": True},
            },
            {
                "id": "Hy4-Flash",
                "name": "Hy4 Flash(x0.5)",
                "type": "chat",
                "protocol": "openai_chat",
                "context_length": 1000000,
                "capabilities": {"function_calling": True},
            },
            {
                # 无倍率的显示名：倍率必须是 None，不能默认 1.0
                "id": "seedance-2-0",
                "name": "Seedance 2.0",
                "type": "chat",
                "protocol": "openai_chat",
            },
            {
                "id": "Hy-Image-3.5-preview",
                "name": "Hy image 3.5 preview",
                "type": "image",
                "protocol": "openai_chat",
            },
        ]
    },
}


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """隔离 Loomy 数据目录与运行期配置路径。"""
    support = tmp_path / "loomy"
    support.mkdir()
    monkeypatch.setenv("LOOMY_USER_DATA_DIR", str(support))
    monkeypatch.delenv("LOOMY_BASE_URL", raising=False)
    monkeypatch.delenv("LOOMY_CLIENT_VERSION", raising=False)
    cfg = tmp_path / "opencode.json"
    monkeypatch.setenv("LOOMY_OPENCODE_CONFIG", str(cfg))
    return support, cfg


def _write_session(support: Path, token: str = "abc123", userid: str = "u1") -> None:
    (support / "auth-session.json").write_text(
        json.dumps({"session": token, "userid": userid, "phone": "185****", "updatedAt": 1}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------
def test_load_session_missing_gives_actionable_error(isolated_home):
    with pytest.raises(cred.AuthError) as excinfo:
        cred.load_session()
    message = str(excinfo.value)
    assert "Loomy" in message
    # 必须告诉用户「怎么办」，不能只是「文件不存在」
    assert "登录" in message


def test_load_session_empty_token_rejected(isolated_home):
    support, _ = isolated_home
    (support / "auth-session.json").write_text(
        json.dumps({"session": "", "userid": "u1"}), encoding="utf-8"
    )
    with pytest.raises(cred.AuthError):
        cred.load_session()


def test_load_session_reads_fields(isolated_home):
    support, _ = isolated_home
    _write_session(support, token="deadbeef", userid="42")
    session = cred.load_session()
    assert session.token == "deadbeef"
    assert session.user_id == "42"
    # 日志标识绝不能包含 token 原文
    assert "deadbeef" not in session.display


def test_base_url_priority_env_over_config(isolated_home, monkeypatch):
    _, cfg = isolated_home
    cfg.write_text(
        json.dumps(
            {"provider": {"imodel": {"options": {"baseURL": "https://from-config/api/v1"}}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOOMY_BASE_URL", "https://from-env/api/v1")
    assert cred.resolve_base_url() == "https://from-env/api/v1"


def test_base_url_prefers_runtime_config_over_default(isolated_home):
    """关键回归：安装包里 .env.prod 写的是测试环境地址，实际在跑的是生产地址。

    拿测试地址验 session 会恒回 100002，被误判成登录过期。
    """
    _, cfg = isolated_home
    cfg.write_text(
        json.dumps(
            {
                "provider": {
                    "imodel": {
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {
                            "baseURL": "https://loomyad.xunfei.cn/api/v1",
                            "useSessionAuth": True,
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert cred.resolve_base_url() == "https://loomyad.xunfei.cn/api/v1"


def test_base_url_ignores_third_party_provider(isolated_home):
    """用户自配的智谱 provider 也是 useSessionAuth=false，不能拿来当网关。"""
    _, cfg = isolated_home
    cfg.write_text(
        json.dumps(
            {
                "provider": {
                    "bigmodel": {"options": {"baseURL": "https://open.bigmodel.cn/api/paas/v4"}},
                    "imodel": {"options": {"baseURL": "https://real-loomy/api/v1"}},
                }
            }
        ),
        encoding="utf-8",
    )
    assert cred.resolve_base_url() == "https://real-loomy/api/v1"


def test_base_url_falls_back_to_production(isolated_home):
    assert cred.resolve_base_url() == cred.DEFAULT_BASE_URL


def test_resolve_origin_strips_api_prefix():
    assert cred.resolve_origin("https://loomyad.xunfei.cn/api/v1") == "https://loomyad.xunfei.cn"
    assert cred.resolve_origin("https://host:8443/api/v1") == "https://host:8443"


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------
def test_parse_multiplier_half_and_full_width():
    assert cat.parse_multiplier("GLM 5.3 Flash(x0.8)") == 0.8
    assert cat.parse_multiplier("qwen 3.8 flash（x0.8）") == 0.8
    assert cat.parse_multiplier("Spark-X2.6 正式版(x2.5)") == 2.5
    assert cat.parse_multiplier("DeepSeek V4.1 Flash(X2.0)") == 2.0
    # 没有倍率必须是 None——默认 1.0 会让「未标倍率」和「倍率=1」无法区分
    assert cat.parse_multiplier("Seedance 2.0") is None
    assert cat.parse_multiplier("") is None


def test_strip_multiplier():
    assert cat.strip_multiplier("GLM 5.3 Flash(x0.8)") == "GLM 5.3 Flash"
    assert cat.strip_multiplier("qwen 3.8 flash（x0.8）") == "qwen 3.8 flash"
    assert cat.strip_multiplier("Seedance 2.0") == "Seedance 2.0"


def test_parse_catalog_double_data_envelope():
    models = cat.parse_catalog(MODELS_PAYLOAD)
    ids = [m.id for m in models]
    assert ids[0] == "spark-x2.6"
    assert "GLM-5.3-Flash" in ids
    assert len(models) == 6

    glm = next(m for m in models if m.id == "GLM-5.3-Flash")
    assert glm.multiplier == 0.8
    assert glm.context_length == 1048576
    assert glm.max_output_tokens == 131072
    assert glm.function_calling is True
    assert glm.display_name == "GLM 5.3 Flash"

    seedance = next(m for m in models if m.id == "seedance-2-0")
    assert seedance.multiplier is None

    image = next(m for m in models if m.id == "Hy-Image-3.5-preview")
    assert image.is_image is True


def test_parse_catalog_single_and_bare_shapes():
    single = {"data": [{"id": "a", "name": "A(x1)"}]}
    bare = [{"id": "b", "name": "B"}]
    assert [m.id for m in cat.parse_catalog(single)] == ["a"]
    assert [m.id for m in cat.parse_catalog(bare)] == ["b"]
    assert cat.parse_catalog({"unexpected": 1}) == []


def test_parse_catalog_dedupes_ids():
    payload = {"data": [{"id": "x", "name": "X"}, {"id": "x", "name": "X2"}]}
    assert len(cat.parse_catalog(payload)) == 1


def test_catalog_cache_does_not_clobber_with_empty():
    """上游灰度返回空表时，空列表不能覆盖已有目录。"""
    cache = cat.CatalogCache(ttl_s=60)
    cache.store(cat.parse_catalog(MODELS_PAYLOAD), now=100.0)
    before = [m.id for m in cache.models]
    cache.store([], now=110.0)
    assert [m.id for m in cache.models] == before


def test_catalog_cache_ttl():
    cache = cat.CatalogCache(ttl_s=60)
    assert cache.is_fresh(now=0.0) is False
    cache.store(cat.parse_catalog(MODELS_PAYLOAD), now=100.0)
    assert cache.is_fresh(now=130.0) is True
    assert cache.is_fresh(now=200.0) is False


# ---------------------------------------------------------------------------
# points
# ---------------------------------------------------------------------------
def test_check_envelope_auth_code():
    with pytest.raises(cred.AuthError):
        pts._check_envelope({"code": "100002", "desc": "登录已失效"}, "test")


def test_check_envelope_other_error():
    with pytest.raises(RuntimeError):
        pts._check_envelope({"code": "500123", "desc": "内部错误"}, "test")


def test_check_envelope_success():
    assert pts._check_envelope({"code": "000000", "data": {"a": 1}}, "test") == {"a": 1}


def test_check_envelope_missing_token_is_not_auth_failure():
    """``100002`` 是复合码：``缺少 token`` 是**我们发错头**，不该让用户去重登。

    实测（2026-11-29）：``/models`` 只认 ``token`` 头，用 Bearer 去打就回
    ``code=100002 desc=缺少 token``。早先按码一律报「登录态已失效」，用户
    明明能用却被告知重登桌面端。
    """
    with pytest.raises(RuntimeError) as exc:
        pts._check_envelope({"code": "100002", "desc": "缺少 token"}, "test")
    msg = str(exc.value)
    assert "登录态已失效" not in msg
    assert "鉴权头" in msg or "token" in msg


def test_is_auth_failure_distinguishes_desc():
    assert cred.is_auth_failure("100002", "登录已失效，请重新登录") is True
    assert cred.is_auth_failure("100002", "缺少 token") is False
    assert cred.is_auth_failure("000000", "登录已失效") is False
    assert cred.is_auth_failure("", "") is False


@pytest.mark.parametrize(
    ("desc", "expected"),
    [
        # 上游文案会变，判据是语义（主语 + 状态）而不是固定短语
        ("登录已失效，请重新登录", True),
        ("登录已过期", True),
        ("凭证失效", True),
        ("token 已过期", True),
        ("token 无效", True),
        # 同一个码但不是登录问题：我们发错了鉴权头
        ("缺少 token", False),
        ("", False),
        ("内部错误", False),
        ("ok", False),
    ],
)
def test_is_auth_failure_semantic_not_phrase_list(desc, expected):
    assert cred.is_auth_failure("100002", desc) is expected


def test_describe_auth_code_two_meanings():
    auth = cred.describe_auth_code("100002", "登录已失效，请重新登录")
    assert "重新登录 Loomy 桌面端" in auth
    header = cred.describe_auth_code("100002", "缺少 token", "/models")
    assert "重新登录 Loomy 桌面端" not in header
    assert "不是登录态过期" in header


def _status(
    score: int, rewarded: tuple[str, ...] = (), capped: bool = False, source: str = "local"
) -> pts.PetWorkStatus:
    """构造 pet-work 状态。

    **``score`` 是档位分数（0–200），不是积分**——两者量纲不同，混用会算错档位。
    """
    config = pts.PetWorkConfig(
        version="pet-work-v1",
        daily_max_points=1000,
        tiers=(
            pts.PetWorkTier("tier_1", 60),
            pts.PetWorkTier("tier_2", 100),
            pts.PetWorkTier("tier_3", 150),
        ),
    )
    return pts.PetWorkStatus(
        business_date="2026-11-29",
        earned_today=0,
        rewarded_tier_ids=rewarded,
        capped=capped,
        config=config,
        score=score,
        score_source=source,
    )


def test_pet_work_highest_unclaimed_tier():
    """档位比的是**分数**：60/100/150，不是积分。"""
    assert _status(0).highest_unclaimed_tier() is None
    assert _status(59).highest_unclaimed_tier() is None
    assert _status(60).highest_unclaimed_tier().id == "tier_1"
    assert _status(99).highest_unclaimed_tier().id == "tier_1"
    assert _status(100).highest_unclaimed_tier().id == "tier_2"
    assert _status(150).highest_unclaimed_tier().id == "tier_3"
    assert _status(200).highest_unclaimed_tier().id == "tier_3"


def test_pet_work_never_reclaims_rewarded_tier():
    """已领的档位不能重复领——服务端会拒，还会留一条失败日志。"""
    status = _status(200, rewarded=("tier_1", "tier_2", "tier_3"))
    assert status.highest_unclaimed_tier() is None


def test_pet_work_unknown_score_never_claims():
    """读不到分数时不能猜——宁可少领，也不拿错的分数去撞服务端校验。"""
    status = _status(0, source="none")
    assert status.score_source == "none"
    assert status.highest_unclaimed_tier() is None
    assert status.next_tier() is None


def test_pet_work_next_tier():
    assert _status(0).next_tier().id == "tier_1"
    assert _status(60).next_tier().id == "tier_2"
    assert _status(120).next_tier().id == "tier_3"
    assert _status(200).next_tier() is None


def test_local_pet_work_reads_same_business_date_only(tmp_path, monkeypatch):
    """本地状态必须是同一个业务日——昨天的分数不能拿来领今天的奖。"""
    import json as _json

    state = tmp_path / "pet-work.json"
    state.write_text(
        _json.dumps(
            {
                "petWork": {
                    "users": {
                        "u1": {
                            "daily": {
                                "businessDate": "2026-11-28",
                                "petWorkScore": 150,
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOOMY_PET_WORK_FILE", str(state))
    local = pts._local_pet_work_daily()
    assert local["businessDate"] == "2026-11-28"
    assert local["petWorkScore"] == 150


def test_local_pet_work_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOMY_PET_WORK_FILE", str(tmp_path / "nope.json"))
    assert pts._local_pet_work_daily() == {}


def test_describe_points_usage_includes_points_and_reasoning():
    text = pts.describe_points_usage(
        {
            "prompt_tokens": 17,
            "completion_tokens": 20,
            "total_tokens": 37,
            "completion_tokens_details": {"reasoning_tokens": 20},
            "points_consumed": 1,
        }
    )
    assert "points=1" in text
    assert "reasoning=20" in text


# ---------------------------------------------------------------------------
# provider：路由归属 / envelope / 转发
# ---------------------------------------------------------------------------
def _patch_catalog(provider: LoomyProvider) -> None:
    provider._catalog.store(cat.parse_catalog(MODELS_PAYLOAD))


def test_accepts_model_by_id_and_display_name(isolated_home):
    support, _ = isolated_home
    _write_session(support)
    provider = LoomyProvider()
    _patch_catalog(provider)

    assert provider.accepts_model("GLM-5.3-Flash")
    assert provider.accepts_model("loomy/GLM-5.3-Flash")
    # 显示名带倍率（用户从截图里抄的可能就是这样）
    assert provider.accepts_model("GLM 5.3 Flash(x0.8)")
    assert provider.accepts_model("GLM 5.3 Flash")
    assert provider.accepts_model("qwen 3.8 flash（x0.8）")
    # 别人的模型不能抢
    assert not provider.accepts_model("gpt-5")
    assert not provider.accepts_model("claude-sonnet-4")


def test_strip_prefix():
    assert _strip_prefix("loomy/abc", "loomy") == "abc"
    assert _strip_prefix("abc", "loomy") == "abc"


def test_raise_for_body_auth_code_becomes_401():
    with pytest.raises(HTTPException) as excinfo:
        _raise_for_body({"code": "100002", "desc": "登录已失效"})
    assert excinfo.value.status_code == 401
    # 提示必须可操作
    assert "重新登录" in str(excinfo.value.detail)


def test_raise_for_body_allows_normal_completion():
    _raise_for_body({"choices": [{"message": {"content": "hi"}}]})


def test_metrics_reads_points_consumed_as_credit():
    """metrics 的 credit 列必须能读到 Loomy 的 points_consumed。

    不识别这个字段名，Loomy 的用量日志 credit 恒为 null——面板上只剩 token
    没有花费，用户看不出「这次花了多少积分」。
    """
    from buddy_proxy.core.metrics_protocol import normalize_usage

    usage = normalize_usage(
        {
            "prompt_tokens": 17,
            "completion_tokens": 20,
            "total_tokens": 37,
            "points_consumed": 3,
        }
    )
    assert usage["credit"] == 3.0
    assert usage["prompt_tokens"] == 17
    assert usage["completion_tokens"] == 20


def test_log_points_usage_includes_multiplier(caplog):
    import logging

    caplog.set_level(logging.INFO)
    _log_points_usage(
        {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "points_consumed": 2,
            }
        },
        "GLM-5.3-Flash",
        0.8,
    )
    text = caplog.text
    assert "points=2" in text
    # 日志里要能看出「为什么贵」：倍率来自模型显示名，模型 id 本身没有倍率
    assert "x0.8" in text


def test_health_reports_catalog_size(isolated_home):
    support, _ = isolated_home
    _write_session(support, token="secret-token-value", userid="u9")
    provider = LoomyProvider()
    _patch_catalog(provider)
    provider._session = cred.load_session()
    provider._base_url = "https://example.test/api/v1"
    health = provider.health()
    assert health["id"] == "loomy"
    assert health["models"] == 6
    # token 绝不进 health（只允许 user id 这类脱敏标识）
    assert "abc123" not in json.dumps(health)
    assert health["account"] == "user u9"


def test_forward_openai_non_stream_passthrough(isolated_home, monkeypatch):
    """非流式 openai 直通：响应体原样返回，model 前缀被剥掉。"""
    support, _ = isolated_home
    _write_session(support)

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "model": "GLM-5.3-Flash",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "points_consumed": 1},
            },
        )

    provider = LoomyProvider()
    _patch_catalog(provider)
    provider._session = cred.load_session()
    provider._base_url = "https://example.test/api/v1"
    provider._origin = "https://example.test"

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    response = asyncio.run(
        provider.forward(
            {"model": "loomy/GLM-5.3-Flash", "messages": [{"role": "user", "content": "hi"}]},
            "openai",
        )
    )
    payload = json.loads(response.body)
    assert payload["choices"][0]["message"]["content"] == "hi"
    assert captured["body"]["model"] == "GLM-5.3-Flash"
    assert captured["url"].endswith("/chat/completions")
    # 两个鉴权头都要带（chat 端点两种都收，客户端自己也两个都带）
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers.get("token") == "abc123"
    assert headers.get("authorization") == "Bearer abc123"
    # 调用方没给 max_tokens 时兜底给足，避免思考链吃光预算导致空 content
    assert captured["body"]["max_tokens"] >= 4096


def test_forward_respects_explicit_max_tokens(isolated_home, monkeypatch):
    """调用方显式给了 max_tokens 就尊重——偷偷放大改变计费预期。"""
    support, _ = isolated_home
    _write_session(support)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = LoomyProvider()
    _patch_catalog(provider)
    provider._session = cred.load_session()
    provider._base_url = "https://example.test/api/v1"

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    asyncio.run(
        provider.forward(
            {"model": "GLM-5.3-Flash", "messages": [], "max_tokens": 32}, "openai"
        )
    )
    assert captured["body"]["max_tokens"] == 32


def test_forward_anthropic_converts_response(isolated_home, monkeypatch):
    """anthropic 协议：chat 响应必须反向转成 Anthropic Message。"""
    support, _ = isolated_home
    _write_session(support)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "model": "GLM-5.3-Flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    provider = LoomyProvider()
    _patch_catalog(provider)
    provider._session = cred.load_session()
    provider._base_url = "https://example.test/api/v1"

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    response = asyncio.run(
        provider.forward({"model": "GLM-5.3-Flash", "messages": []}, "anthropic")
    )
    payload = json.loads(response.body)
    # Anthropic Message 的形状：type=message + content 块数组
    assert payload.get("type") == "message"
    assert any(
        block.get("type") == "text" and block.get("text") == "hello"
        for block in payload.get("content", [])
    )


def test_forward_auth_failure_maps_to_401(isolated_home, monkeypatch):
    support, _ = isolated_home
    _write_session(support)

    def handler(request: httpx.Request) -> httpx.Response:
        # 网关恒回 200，业务码才是真相
        return httpx.Response(200, json={"code": "100002", "desc": "登录已失效，请重新登录"})

    provider = LoomyProvider()
    _patch_catalog(provider)
    provider._session = cred.load_session()
    provider._base_url = "https://example.test/api/v1"

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            provider.forward({"model": "GLM-5.3-Flash", "messages": []}, "openai")
        )
    assert excinfo.value.status_code == 401
    assert "重新登录" in str(excinfo.value.detail)


# ---------------------------------------------------------------------------
# 生图
# ---------------------------------------------------------------------------
def test_extract_prompt_from_top_level():
    assert _extract_prompt({"prompt": "a cat"}) == "a cat"
    assert _extract_prompt({"prompt": "   "}) == ""


def test_extract_prompt_from_last_user_message():
    body = {
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "draw a dog"},
        ]
    }
    assert _extract_prompt(body) == "draw a dog"


def test_extract_prompt_from_multimodal_content():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "draw "},
                    {"type": "text", "text": "a bird"},
                    {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                ],
            }
        ]
    }
    assert _extract_prompt(body) == "draw a bird"


def test_image_models_from_catalog(isolated_home):
    support, _ = isolated_home
    _write_session(support)
    provider = LoomyProvider()
    _patch_catalog(provider)
    assert provider.image_models() == ["Hy-Image-3.5-preview"]


def test_is_image_model(isolated_home):
    support, _ = isolated_home
    _write_session(support)
    provider = LoomyProvider()
    _patch_catalog(provider)
    assert provider._is_image_model("Hy-Image-3.5-preview") is True
    assert provider._is_image_model("GLM-5.3-Flash") is False


def test_generate_image_uses_bearer_only(isolated_home, monkeypatch):
    """生图端点**只认 Bearer**：带 token 头会回「缺少 token」（实测）。

    三个端点三种脾气：chat 两个都收、/models 只认 token、生图只认 Bearer。
    """
    support, _ = isolated_home
    _write_session(support, token="img-token")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.content.decode())
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "created": 1795889468,
                "data": [{"url": "https://example.test/img.png"}],
                "points_consumed": 110,
            },
        )

    provider = LoomyProvider()
    _patch_catalog(provider)
    provider._session = cred.load_session()
    provider._base_url = "https://example.test/api/v1"

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    result = asyncio.run(
        provider.generate_image("a cat", model="Hy-Image-3.5-preview", size="1024x1024")
    )
    assert result["points_consumed"] == 110
    assert captured["url"].endswith("/images/generations")
    headers = captured["headers"]
    assert headers.get("authorization") == "Bearer img-token"
    # 生图不能带 token 头（与 chat 相反）
    assert "token" not in headers
    assert captured["body"]["prompt"] == "a cat"
    assert captured["body"]["model"] == "Hy-Image-3.5-preview"


def test_forward_image_model_wraps_as_chat(isolated_home, monkeypatch):
    """生图模型走 chat 协议时，结果要包成 chat completion（客户端只认 chat）。"""
    support, _ = isolated_home
    _write_session(support)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/images/generations")
        return httpx.Response(
            200,
            json={
                "created": 1,
                "data": [{"url": "https://example.test/a.png"}],
                "points_consumed": 110,
            },
        )

    provider = LoomyProvider()
    _patch_catalog(provider)
    provider._session = cred.load_session()
    provider._base_url = "https://example.test/api/v1"

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    response = asyncio.run(
        provider.forward(
            {
                "model": "loomy/Hy-Image-3.5-preview",
                "messages": [{"role": "user", "content": "a cat"}],
            },
            "openai",
        )
    )
    payload = json.loads(response.body)
    content = payload["choices"][0]["message"]["content"]
    assert "https://example.test/a.png" in content
    assert "110" in content
    # 积分塞进 usage 才能被 normalize_usage 读成 credit
    assert payload["usage"]["points_consumed"] == 110


def test_image_points_reach_metrics():
    """生图的 points_consumed 必须能被 metrics 读成 credit。"""
    from buddy_proxy.core.metrics_protocol import normalize_usage

    assert normalize_usage({"points_consumed": 110})["credit"] == 110.0
