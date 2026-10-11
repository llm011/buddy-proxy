"""qoder per-account 模型支持（覆盖表）+ 目录并集 + 倍率 0.0 测试。

覆盖三块（对应 2026-10-06 线上三 bug）：

- **覆盖表白名单**（qoder/models.json）：加载容错、id 三写法归一、
  ``account_supports``/``unsupported_models_for`` 反查；
- **forward per-account 门控**：受限账号被跳过（不冷却、不白打上游）、
  全不支持快速 404；
- **目录并集**（refresh_models 逐账号）与覆盖表缺失模型合成；
- **倍率 0.0**：credits_map 真值判断吞掉免费模型倍率的回归。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from buddy_proxy.core.metrics import ACCOUNT_META
from buddy_proxy.qoder import catalog as qoder_catalog
from buddy_proxy.qoder import failover
from buddy_proxy.qoder.catalog import (
    FALLBACK_MODELS,
    account_supports,
    override_accounts,
    override_missing_entries,
    to_openai_model,
    unsupported_models_for,
)
from buddy_proxy.qoder.credentials import save_account_cred
from buddy_proxy.qoder.provider import QoderProvider


# ---------------------------------------------------------------------------
# 桩（照 test_qoder_provider_failover.py 自包含一份，避免测试文件互相耦合）
# ---------------------------------------------------------------------------

def _write_account(acct_id: str, *, region: str = "cn") -> None:
    save_account_cred({
        "account_id": acct_id,
        "token": f"dt-{acct_id}",
        "uid": f"uid-{acct_id}",
        "machine_id": f"m-{acct_id}",
        "refresh_token": f"rt-{acct_id}",
        "expires_at_ms": 9999999999000,
        "name": "",
        "email": f"{acct_id}@qoder.example.com",
        "region": region,
        "plan": "",
        "source": "state",
    })


class _ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class _Upstream:
    """按 ``Cosy-User``（uid）路由 canned 响应；记录每次命中的 uid。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.routes: dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        uid = request.headers.get("Cosy-User", "").strip()
        self.calls.append(uid)
        route = self.routes.get(uid)
        if route is None:
            return httpx.Response(500, json={"error": {"message": f"no route {uid}"}})
        return route(request)


def _patch_token(monkeypatch) -> list[tuple[str, bool]]:
    calls: list[tuple[str, bool]] = []

    def fake(account_id, *, force_refresh=False):
        calls.append((account_id, force_refresh))
        return (f"dt-{account_id}", {
            "account_id": account_id,
            "token": f"dt-{account_id}",
            "uid": f"uid-{account_id}",
            "machine_id": f"m-{account_id}",
            "refresh_token": f"rt-{account_id}",
            "expires_at_ms": 9999999999000,
            "email": f"{account_id}@qoder.example.com",
            "region": "cn",
            "plan": "",
            "source": "state",
        })

    monkeypatch.setattr("buddy_proxy.qoder.provider.ensure_account_token", fake)
    return calls


def _cosy_frame(inner: dict) -> bytes:
    envelope = {"headers": {}, "body": json.dumps(inner)}
    return f"data: {json.dumps(envelope)}\n\n".encode()


def _text_stream(text: str) -> httpx.Response:
    chunks = [
        _cosy_frame({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}),
        _cosy_frame({"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}),
        _cosy_frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}),
        b"data: [DONE]\n\n",
    ]
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          stream=_ChunkedBody(chunks))


def _run(monkeypatch, upstream_routes: dict[str, Any], body: dict):
    up = _Upstream()
    up.routes.update(upstream_routes)
    token_calls = _patch_token(monkeypatch)
    prov = QoderProvider()
    meta: dict[str, Any] = {}
    holder = ACCOUNT_META.set(meta)
    try:
        async def _go():
            return await prov.forward(body, "openai")

        real_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(up)
            kwargs.pop("timeout", None)
            return real_client(*args, **kwargs)

        monkeypatch.setattr("buddy_proxy.qoder.provider.httpx.AsyncClient", client_factory)
        resp = asyncio.run(_go())
        return resp, up.calls, token_calls, meta
    finally:
        ACCOUNT_META.reset(holder)


@pytest.fixture
def _override(monkeypatch):
    """把覆盖表换成测试受控形状（(原始写法, 支持账号集合) 字典）。"""
    def _set(mapping):
        monkeypatch.setattr(qoder_catalog, "MODEL_OVERRIDE", mapping)
    return _set


# ---------------------------------------------------------------------------
# 覆盖表加载与判定
# ---------------------------------------------------------------------------

def test_override_loads_from_packaged_models_json():
    """随包 models.json 必须能加载出合法覆盖表。

    2026-10-12 起 models.json 的 models 数组为空：受限号的权益分桶已恢复
    全量（目录 14 条 + GLM/Kimi/MiniMax 实调真实计费），旧白名单整体过时。
    空表语义 = 所有账号支持所有模型（_load_model_override 的空表约定）。
    这里只钉「文件存在且能被解析成空表」，防止文件再次携带限制时静默失效。
    """
    assert isinstance(qoder_catalog.MODEL_OVERRIDE, dict)
    assert qoder_catalog.MODEL_OVERRIDE == {}, (
        "models.json 又带上了账号限制？若某号再被风控收窄，按 2026-10-06 的"
        "形态回填条目，并同步更新本断言"
    )


def test_override_missing_or_corrupt_file(tmp_path, monkeypatch, caplog):
    """文件缺失/损坏都回落空表（= 全无限制），不炸服务。"""
    monkeypatch.setattr(qoder_catalog, "_OVERRIDE_JSON", tmp_path / "nope.json")
    assert qoder_catalog._load_model_override() == {}

    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(qoder_catalog, "_OVERRIDE_JSON", bad)
    assert qoder_catalog._load_model_override() == {}


def test_override_all_and_missing_accounts_variants(tmp_path, monkeypatch):
    """accounts 缺省/"all"/字符串单值都按语义展开。"""
    cfg = tmp_path / "m.json"
    cfg.write_text(json.dumps({"models": [
        {"id": "model-a"},                      # 缺省 = 全支持 → 不进表
        {"id": "model-b", "accounts": "all"},   # 同上
        {"id": "model-c", "accounts": "acct-1"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(qoder_catalog, "_OVERRIDE_JSON", cfg)
    table = qoder_catalog._load_model_override()
    assert set(table) == {qoder_catalog._normalize("model-c")}
    assert table[qoder_catalog._normalize("model-c")] == ("model-c", {"acct-1"})


def test_account_supports_id_variants(_override):
    """支持判定对上游 key / public id / display_name 三写法都归一命中。"""
    _override({"glm53": ("glm-5.3", frozenset({"acct-b"}))})
    allowed_entry = {"key": "gmodel", "display_name": "GLM-5.3"}
    assert account_supports(allowed_entry, "acct-b")
    assert not account_supports(allowed_entry, "acct-a")
    # 未登记的模型：全账号支持
    other = {"key": "qfmodel", "display_name": "Qwen3.8-Flash"}
    assert account_supports(other, "acct-a")
    assert override_accounts(other) is None


def test_unsupported_models_for_reverse_lookup(_override):
    _override({"glm53": ("glm-5.3", frozenset({"acct-b"})),
               "kimi53": ("kimi-k3", frozenset({"acct-b"}))})
    assert unsupported_models_for("acct-a") == ["glm-5.3", "kimi-k3"]
    assert unsupported_models_for("acct-b") == []


# ---------------------------------------------------------------------------
# forward per-account 门控
# ---------------------------------------------------------------------------

def test_forward_skips_unsupported_account(monkeypatch, _override):
    """受限账号被跳过：不打上游、不走 token、不进冷却，请求落到支持账号。"""
    _write_account("acct-a")
    _write_account("acct-b")
    _override({"glm53": ("glm-5.3", frozenset({"acct-b"}))})
    routes = {"uid-acct-b": lambda req: _text_stream("from-b")}
    resp, calls, token_calls, _meta = _run(
        monkeypatch, routes,
        {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}],
         "stream": False})
    assert resp.status_code == 200
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == "from-b"
    assert calls == ["uid-acct-b"], "受限账号不该产生上游调用"
    assert all(a != "acct-a" for a, _f in token_calls), "受限账号不该被取 token"
    assert failover.cooldown_left("acct-a")[0] <= 0, "跳过≠失败，不该进冷却"


def test_forward_all_unsupported_fast_404(monkeypatch, _override):
    """全账号都不支持 → 快速 404（带模型名），不白打上游。"""
    _write_account("acct-a")
    _write_account("acct-b")
    _override({"glm53": ("glm-5.3", frozenset({"acct-other"}))})
    with pytest.raises(HTTPException) as ei:
        _run(monkeypatch, {},  # 上游不路由任何账号：命中即失败
             {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}],
              "stream": False})
    assert ei.value.status_code == 404
    assert "glm" in str(ei.value.detail).lower()


def test_forward_unrestricted_model_reaches_all_accounts(monkeypatch, _override):
    """未登记模型不受门控影响（回归：白名单别把别的模型挡住）。"""
    _write_account("acct-a")
    _override({"glm53": ("glm-5.3", frozenset({"acct-b"}))})
    routes = {"uid-acct-a": lambda req: _text_stream("ok")}
    resp, calls, _tc, _meta = _run(
        monkeypatch, routes,
        {"model": "qwen3.8-flash", "messages": [{"role": "user", "content": "hi"}],
         "stream": False})
    assert resp.status_code == 200
    assert calls == ["uid-acct-a"]


# ---------------------------------------------------------------------------
# 目录并集（refresh_models 逐账号）
# ---------------------------------------------------------------------------

def _patch_fetch(monkeypatch, per_uid: dict[str, object]) -> None:
    """打桩 Catalog.fetch：按凭据 uid 返回 canned 目录；Exception 则抛。"""
    async def fake_fetch(self, cred, *, force=False):
        data = per_uid[cred.uid]
        if isinstance(data, Exception):
            raise data
        return list(data)
    monkeypatch.setattr("buddy_proxy.qoder.provider.Catalog.fetch", fake_fetch)


_QWEN_ONLY = [
    {"key": "qmodel_38max", "display_name": "Qwen3.8-Max", "enable": True,
     "price_factor": 0.2, "is_free": True, "max_input_tokens": 180000},
    {"key": "qfmodel", "display_name": "Qwen3.8-Flash", "enable": True,
     "price_factor": 0.0, "is_free": True, "max_input_tokens": 180000},
]
_FULL_EXTRA = [
    {"key": "gmodel", "display_name": "GLM-5.3", "enable": True,
     "price_factor": 0.8, "max_input_tokens": 180000},
    {"key": "kmodel_latest", "display_name": "Kimi-K3", "enable": True,
     "price_factor": 1.4, "max_input_tokens": 180000},
]


def test_refresh_models_union_across_accounts(monkeypatch, _override):
    """受限号目录少不该抹掉全量号的模型：并集 = 至少一个账号支持。"""
    _write_account("acct-a")
    _write_account("acct-b")
    _patch_fetch(monkeypatch, {
        "uid-acct-a": _QWEN_ONLY,
        "uid-acct-b": _QWEN_ONLY + _FULL_EXTRA,
    })
    prov = QoderProvider()
    models = asyncio.run(prov.refresh_models())
    ids = {m["id"] for m in models}
    assert "qoder/glm-5.3" in ids, "全量号的三方模型必须在并集里"
    assert "qoder/qwen3.8-flash" in ids
    # 并集落盘，后续同步 models()/accepts_model() 同视角
    assert any(m["id"] == "qoder/kimi-k3" for m in prov.models())
    assert prov.accepts_model("glm-5.3", aliases=False)


def test_refresh_models_hides_retired_catalog_entries(monkeypatch, _override):
    """动态上游目录里的旧模型也不应重新出现在模型列表。"""
    _write_account("acct-a")
    _patch_fetch(monkeypatch, {
        "uid-acct-a": _QWEN_ONLY + [
            {"key": "gm51model", "display_name": "GLM-5.2", "enable": True},
            {"key": "kmodel", "display_name": "Kimi-K2.8-Preview", "enable": True},
            {"key": "mmodel", "display_name": "MiniMax-M2.7", "enable": True},
        ],
    })
    prov = QoderProvider()
    ids = {m["id"] for m in asyncio.run(prov.refresh_models())}
    assert {"qoder/qwen3.8-max", "qoder/qwen3.8-flash"} <= ids
    assert not {"qoder/glm-5.2", "qoder/kimi-k2.8-preview", "qoder/minimax-m2.7"} & ids

    catalog = prov._catalog_for(prov._region.key)
    assert catalog.resolve_key("qoder/glm-5.2") == "gm51model"
    assert catalog.resolve_key("qoder/kimi-k2.8-preview") == "kmodel"
    assert catalog.resolve_key("qoder/minimax-m2.7") == "mmodel"


def test_refresh_models_tolerates_single_account_failure(monkeypatch, _override):
    """单账号刷新失败只收缩并集，不拖垮整体；全失败回落兜底表。"""
    _write_account("acct-a")
    _write_account("acct-b")
    _patch_fetch(monkeypatch, {
        "uid-acct-a": RuntimeError("boom"),
        "uid-acct-b": _QWEN_ONLY + _FULL_EXTRA,
    })
    prov = QoderProvider()
    models = asyncio.run(prov.refresh_models())
    assert {m["id"] for m in models} >= {"qoder/glm-5.3", "qoder/qwen3.8-flash"}


def test_override_missing_model_synthesized(monkeypatch, _override):
    """覆盖表登记、目录暂缺的模型补最小条目（可显示可点名）。"""
    _override({"minimaxm27": ("minimax-m2.7", frozenset({"acct-b"}))})
    entries = override_missing_entries(_QWEN_ONLY)
    assert [e["key"] for e in entries] == ["minimax-m2.7"]
    # 已存在（按 public id 归一判定）不重复合成
    assert override_missing_entries(
        _QWEN_ONLY + [{"key": "mmodel", "display_name": "MiniMax-M2.7"}]) == []


# ---------------------------------------------------------------------------
# 兜底表 + support 画像 + accounts_status
# ---------------------------------------------------------------------------

def test_fallback_models_restored_with_measured_prices():
    """兜底表恢复三方模型，实测倍率修正到位（0.0 免费档必须保留）。"""
    by_key = {m["key"]: m for m in FALLBACK_MODELS}
    for key in ("gmodel", "gfmodel", "dmodel", "dfmodel", "kmodel_latest",
                "kmodel", "gm51model", "mmodel"):
        assert key in by_key, f"兜底表缺三方模型 {key}"
    assert by_key["qfmodel"]["price_factor"] == 0.0
    assert by_key["qmodel_latest"]["price_factor"] == 0.5
    assert by_key["q37fmodel"]["price_factor"] == 0.1
    assert by_key["kmodel_latest"]["price_factor"] == 1.4
    # 0.0 倍率必须进 OpenAI 元数据（credits 字段曾因真值判断丢失）
    m = to_openai_model(by_key["qfmodel"], "qoder")
    assert m["credits"] == 0.0


def test_model_support_map_and_provider_models_passthrough(monkeypatch, _override):
    """support 画像只收受限模型；models_api 透传给前端。"""
    _write_account("acct-a")
    _write_account("acct-b")
    from buddy_proxy.qoder import failover as qf
    monkeypatch.setattr("buddy_proxy.qoder.provider.failover", qf)
    _override({"glm53": ("glm-5.3", frozenset({"acct-b"}))})
    prov = QoderProvider()
    # 并集手工灌入（绕过网络），gmodel 对外 id = glm-5.3
    prov._union = {"gmodel": {"key": "gmodel", "display_name": "GLM-5.3",
                              "enable": True, "price_factor": 0.8}}
    sup = prov.model_support_map()
    assert set(sup) == {"glm-5.3"}
    assert sup["glm-5.3"]["limited"] is True
    assert len(sup["glm-5.3"]["accounts"]) == 1
    assert sup["glm-5.3"]["accounts"][0].startswith("acct-b"), "显示名取 accounts_status 的 name 链"

    from buddy_proxy.web.ui.models_api import _provider_models
    items = _provider_models(prov)
    hit = next(m for m in items if (m["id"] or "").endswith("glm-5.3"))
    assert hit["support"] == sup["glm-5.3"]


def test_accounts_status_carries_models_limited(monkeypatch, _override):
    _write_account("acct-a")
    _override({"glm53": ("glm-5.3", frozenset({"acct-other"}))})
    items = failover.accounts_status()["accounts"]
    assert items[0]["models_limited"] == ["glm-5.3"]


def test_startup_warmup_loop_refreshes_then_sleeps(monkeypatch):
    """预热循环：先刷一次再 sleep；刷新抛异常不退出循环体（只 log）。"""
    from buddy_proxy import __main__ as main_mod

    calls = {"n": 0}

    class _Prov:
        async def refresh_models(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return []

    sleeps = {"n": 0}

    async def fake_sleep(_s):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            raise asyncio.CancelledError()

    import buddy_proxy.qoder.catalog as cat
    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    monkeypatch.setattr(cat, "CACHE_TTL_S", 0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main_mod._qoder_model_refresh_loop(_Prov()))
    assert calls["n"] == 2, "异常后应继续下一轮刷新"
