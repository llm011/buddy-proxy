"""模型路由回归：带前缀 id 的通道（Qoder 形态）与别名兜底。

本文件锁定三处**静默失效**类 Bug 的修复（2026-09 联调中发现，均非新增功能
引入，而是 Qoder 的带前缀 id 第一次让它们显形）：

1. **停用键双前缀**：管理页把带前缀的 ``id``（``qoder/qfmodel``）当 ``model``
   回传，与 ``provider`` 再拼一次得到 ``qoder/qoder/qfmodel``，而转发侧用的是
   剥前缀后的裸名 —— 写入的键永远命中不了，「停用」显示成功但完全失效。
2. **别名请求掉进兜底通道**：自动路由只比 ``m["id"] == model``；目录带前缀、
   客户端发裸名时漏配，请求掉进兜底通道被上游拒为 11102。
3. **别名抢走别的通道按 id 命中的模型**：别名若与精确匹配同轮参与、按注册序
   先到先得，注册靠前的通道会把别人的模型抢走（``glm-5.3`` 曾被 qoder 从
   zcode 抢走）。故路由分两轮：先全部按 id 精确匹配，再退回别名兜底。

运行：
    .venv/bin/python -m pytest tests/test_model_routing.py -v
"""
from __future__ import annotations

import inspect
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from buddy_proxy import __main__ as m
from buddy_proxy.core import settings as settings_mod
from buddy_proxy.core import state as st
from buddy_proxy.core.metrics import MetricsCollector
from buddy_proxy.providers.base import BaseProvider


def _make_state(providers, tmp_path):
    client = mock.MagicMock()
    client.endpoint = "https://fake.endpoint.invalid"
    client.auth_headers.return_value = {}
    client.session = {"auth": {"accessToken": "t",
                               "expiresAt": int((time.time() + 3600) * 1000)}}
    return SimpleNamespace(
        client=client,
        providers=providers,
        mock_dir=None,
        started_at=time.time(),
        enable_desensitize=False,
        enable_optimize_context=False,
        verbose_llm=False,
        default_provider="codebuddy",
        default_model=None,
        disabled_models=set(),
        model_schedules={},
        metrics=MetricsCollector(tmp_path / "metrics.jsonl"),
        write_log=mock.MagicMock(),
        ensure_auth=mock.MagicMock(),
        logger=mock.MagicMock(),
        json_logger=mock.MagicMock(),
        runtime_info={"app_version": "test", "system_version": "test",
                      "python_version": "test", "machine": "test"},
    )


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """隔离设置文件，绝不读写真实 ~/.buddy-proxy/settings.json。"""
    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(tmp_path / "settings.json"))


class PrefixedAliasProvider(BaseProvider):
    """模拟 Qoder：目录 id 带前缀，且接受显示名/大小写别名。"""

    id = "prefixed"
    name = "Prefixed Alias Provider"

    #: 归一键 -> 内部 key（与真实 catalog 的别名表同构：显示名与 key 都指向 key）
    _ALIASES = {
        "flashx": "flashx", "mockqwen99": "flashx",
        "glm53": "glm53", "fakeglm99": "glm53",
    }

    def __init__(self):
        self.calls = []

    def models(self):
        return [
            {"id": "prefixed/flashx", "description": "MockQwen-9.9"},
            {"id": "prefixed/glm53", "description": "FakeGlm-9.9"},
        ]

    def ensure_auth(self):
        pass

    def resolve_model(self, model: str) -> str:
        norm = "".join(ch for ch in str(model).lower() if ch.isalnum())
        return self._ALIASES.get(norm, model)

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        if super().accepts_model(model):
            return True
        if not aliases:
            return False
        want = (model or "").strip()
        return bool(want) and self.resolve_model(want) != want

    async def forward(self, body, protocol, original=None):
        self.calls.append(dict(body))
        return JSONResponse({
            "id": "chatcmpl-prefixed", "model": body.get("model"),
            "choices": [{"message": {"role": "assistant", "content": "pong"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


@pytest.fixture()
def prefixed_env(tmp_path, monkeypatch):
    prov = PrefixedAliasProvider()
    state = _make_state({"prefixed": prov}, tmp_path)
    monkeypatch.setattr(st, "proxy_state", state)
    return SimpleNamespace(state=state, provider=prov, client=TestClient(m.app))


# --- 停用/时段键的口径 ------------------------------------------------------


def test_prefixed_toggle_key_has_single_prefix(prefixed_env):
    """管理页回传带前缀 id 时，落键必须只有一层前缀。

    回归：曾写出 ``prefixed/prefixed/flashx``，与转发侧 ``prefixed/flashx``
    对不上，停用静默失效。
    """
    r = prefixed_env.client.post(
        "/ui/api/model-toggle",
        json={"provider": "prefixed", "model": "prefixed/flashx", "disabled": True})
    assert r.status_code == 200
    assert r.json()["model"] == "prefixed/flashx"
    assert prefixed_env.state.disabled_models == {"prefixed/flashx"}
    assert settings_mod.load_settings()["disabled_models"] == ["prefixed/flashx"]


def test_prefixed_toggle_accepts_bare_name(prefixed_env):
    """裸名同样落同一把键（客户端实际发的形态）。"""
    r = prefixed_env.client.post(
        "/ui/api/model-toggle",
        json={"provider": "prefixed", "model": "flashx", "disabled": True})
    assert r.status_code == 200
    assert r.json()["model"] == "prefixed/flashx"


@pytest.mark.parametrize("model", ["flashx", "prefixed/flashx", "MockQwen-9.9",
                                   "mockqwen-9.9", "MOCKQWEN-9.9"])
def test_prefixed_aliases_auto_route(prefixed_env, model):
    """别名不该掉进兜底通道：必须命中 prefixed 通道。"""
    r = prefixed_env.client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, (model, r.text)
    assert prefixed_env.provider.calls, f"{model} 未被路由到 prefixed 通道"


@pytest.mark.parametrize("model", ["flashx", "prefixed/flashx", "MockQwen-9.9",
                                   "mockqwen-9.9"])
def test_prefixed_disable_blocks_all_aliases(prefixed_env, model):
    """停用后所有别名写法一律 403（别名绕过会让人以为停用失效）。"""
    prefixed_env.client.post(
        "/ui/api/model-toggle",
        json={"provider": "prefixed", "model": "prefixed/flashx", "disabled": True})
    r = prefixed_env.client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403, (model, r.status_code, r.text)


def test_prefixed_disable_does_not_leak_to_other_models(prefixed_env):
    prefixed_env.client.post(
        "/ui/api/model-toggle",
        json={"provider": "prefixed", "model": "prefixed/flashx", "disabled": True})
    r = prefixed_env.client.post(
        "/v1/chat/completions",
        json={"model": "FakeGlm-9.9", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200


@pytest.mark.parametrize("model", ["glm53", "FakeGlm-9.9", "prefixed/glm53"])
def test_prefixed_schedule_blocks_all_aliases(prefixed_env, monkeypatch, model):
    """限时窗口同样要按归一后的键命中所有别名写法。"""
    prefixed_env.state.model_schedules = {"prefixed/glm53": [["00:00", "00:01"]]}
    monkeypatch.setattr(settings_mod, "model_schedule_open", lambda w: False)
    r = prefixed_env.client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403, (model, r.status_code, r.text)


# --- accepts_model 契约 -----------------------------------------------------


def test_accepts_model_default_rejects_unknown(prefixed_env):
    """accepts_model 不能把无关模型也认领走。"""
    assert prefixed_env.provider.accepts_model("totally-other-model") is False
    assert prefixed_env.provider.accepts_model("") is False


def test_alias_does_not_steal_exact_match_from_other_channel():
    """别名只做兜底，绝不能抢走别的通道按 id 精确匹配就能认领的模型。

    回归（真实场景）：qoder 目录里 GLM-5.3 的显示名与 zcode 的模型名
    ``glm-5.3`` 撞车，且 qoder 注册更靠前。若别名与精确匹配同轮参与，
    ``glm-5.3`` 会被 qoder 抢走、路由归属静默改变。
    """

    class AliasProvider(BaseProvider):
        """只通过别名认识 glm-5.3（自己的 id 是别的名字）。"""

        id = "aliasprov"
        name = "Alias Provider"

        def models(self):
            return [{"id": "aliasprov/internal-glm"}]

        def ensure_auth(self):
            pass

        def resolve_model(self, model):
            return "internal-glm" if model.strip().lower() == "glm-5.3" else model

        def accepts_model(self, model, aliases=True):
            if super().accepts_model(model):
                return True
            return aliases and self.resolve_model(model) != model

        async def forward(self, body, protocol, original=None):  # pragma: no cover
            raise NotImplementedError

    class ExactProvider(BaseProvider):
        """按 id 精确提供 glm-5.3 的通道。"""

        id = "exactprov"
        name = "Exact Provider"

        def models(self):
            return [{"id": "glm-5.3"}]

        def ensure_auth(self):
            pass

        async def forward(self, body, protocol, original=None):  # pragma: no cover
            raise NotImplementedError

    alias, exact = AliasProvider(), ExactProvider()
    assert alias.accepts_model("glm-5.3", aliases=False) is False
    assert exact.accepts_model("glm-5.3", aliases=False) is True
    assert alias.accepts_model("glm-5.3", aliases=True) is True


def test_base_accepts_model_handles_bare_and_prefixed():
    """基类默认实现：id 精确匹配 + 剥前缀裸名，不认别名（两个 aliases 取值等价）。"""

    class P(BaseProvider):
        id = "p"
        name = "P"

        def models(self):
            return [{"id": "p/m1"}, {"id": "bare2"}]

        def ensure_auth(self):
            pass

        async def forward(self, body, protocol, original=None):  # pragma: no cover
            raise NotImplementedError

    p = P()
    assert p.accepts_model("p/m1") is True
    assert p.accepts_model("m1") is True
    assert p.accepts_model("bare2") is True
    assert p.accepts_model("nope") is False
    # 基类没有别名概念：aliases 不影响结果
    assert p.accepts_model("nope", aliases=False) is False
    assert p.accepts_model("bare2", aliases=False) is True


def test_two_round_routing_prefers_exact_match_over_alias(tmp_path, monkeypatch):
    """整链验证：精确匹配轮先跑完，别名轮才兜底。

    用两个通道复刻 ``glm-5.3`` 撞车场景：别名通道注册在前，精确通道在后。
    注册靠前不该让别名抢走结果。
    """

    class AliasOnly(BaseProvider):
        id = "aliasonly"
        name = "Alias Only"

        def models(self):
            return [{"id": "aliasonly/other"}]

        def ensure_auth(self):
            pass

        def resolve_model(self, model):
            return "other" if model.strip().lower() == "glm-5.3" else model

        def accepts_model(self, model, aliases=True):
            if super().accepts_model(model):
                return True
            return aliases and self.resolve_model(model) != model

        async def forward(self, body, protocol, original=None):
            return JSONResponse({
                "id": "chatcmpl-alias", "model": body.get("model"),
                "choices": [{"message": {"role": "assistant", "content": "ALIAS"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    class Exact(BaseProvider):
        id = "exact"
        name = "Exact"

        def models(self):
            return [{"id": "glm-5.3"}]

        def ensure_auth(self):
            pass

        async def forward(self, body, protocol, original=None):
            return JSONResponse({
                "id": "chatcmpl-exact", "model": body.get("model"),
                "choices": [{"message": {"role": "assistant", "content": "EXACT"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    # 别名通道注册在前，模拟 qoder 早于 zcode
    state = _make_state({"aliasonly": AliasOnly(), "exact": Exact()}, tmp_path)
    monkeypatch.setattr(st, "proxy_state", state)
    client = TestClient(m.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "EXACT"


# --- 别名不得抢走 CodeBuddy 静态表的模型名 ---------------------------------


def test_codebuddy_static_name_not_claimed_by_alias():
    """``auto`` 是 CodeBuddy 的默认模型，别名通道不得认领。

    回归：Qoder 的档位模型（本地合成的 TIER_MODELS）也叫 ``auto``，别名表因此
    命中。``auto`` 又正是 CodeBuddy 的默认模型名（models_config.json 的
    isDefault），没带 model 的请求会被 forward 用 default_model=auto 补齐——
    一旦被别名截走，这些请求会在 Qoder 通道上静默改道。
    """
    from buddy_proxy.codebuddy_provider.forward import _is_codebuddy_model

    assert _is_codebuddy_model("auto") is True
    assert _is_codebuddy_model("not-a-codebuddy-model") is False
    assert _is_codebuddy_model("") is False


@pytest.mark.parametrize("name", ["Auto", "AUTO", "Qwen3.8-Max", "GLM-5.3", "Kimi-K3"])
def test_codebuddy_guard_does_not_block_display_names(name):
    """守卫只按**原样**比对，不挡通道目录里的官方显示名。

    回归：守卫曾做小写化比对，于是 ``Qwen3.8-Max``/``Kimi-K3`` 这些小写化后与
    静态表撞车的**官方 display_name** 被一并挡掉，别名轮不再认领 → 请求掉进
    CodeBuddy 兜底通道 → 上游 502「model [Qwen3.8-Max] service info not found」。
    按官方文档写模型名是正常用法，不该失败。

    需要防的是裸名 ``auto`` 被别名改道，而那种输入本来就没有大小写变体。
    """
    from buddy_proxy.codebuddy_provider.forward import _is_codebuddy_model

    assert _is_codebuddy_model(name) is False


def test_codebuddy_guard_applies_only_to_alias_round():
    """保护只挡别名轮，不挡精确轮——否则会改变 trae/zcode 等既有路由行为。

    精确轮是各通道按自己发布的 id 认领，模型名与静态表重合是正常的（两边都真
    有这个模型），此时让该通道照常先认领。

    断言对象是 ``_resolve_auto`` 而非 ``forward_chat``：两轮匹配逻辑已作为纯重构
    搬进该 helper（``forward_chat`` 与「为取 model_order 键而解析归属」共用它），
    语义完全不变。真正锁行为的是下面 ``test_real_qoder_*`` 那几条。
    """
    from buddy_proxy.codebuddy_provider import forward as fwd

    for fn in (fwd._resolve_auto, fwd.forward_chat):
        src = inspect.getsource(fn)
        if fn is fwd._resolve_auto:
            # 守卫必须在 allow_aliases 为真的分支里
            assert "if allow_aliases and _is_codebuddy_model(requested_model):" in src
        else:
            # forward_chat 必须调用它，不能把两轮逻辑再复制一份
            assert "_resolve_auto(state, requested_model)" in src


class _CollidingProvider(BaseProvider):
    """模拟方舟：对外名直接配成静态表里也有的公共模型名。"""

    id = "collide"
    name = "Colliding Provider"

    def __init__(self, model: str, priority: int | None):
        self._model = model
        if priority is not None:
            # 实例属性也能被 getattr 取到；真身份在类属性上，这里实例化两份互不影响
            self.route_priority = priority

    def models(self):
        return [{"id": self._model}]

    def ensure_auth(self):
        pass

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        # 与真实 provider 同口径：带前缀的名字按剥前缀后的 id 认领
        want = (model or "").strip().removeprefix(f"{self.id}/")
        return want == self._model

    async def forward(self, body, protocol, original=None):  # pragma: no cover
        raise AssertionError("不该路由到这里")


_COLLIDING_NAME = "deepseek-v4.1-flash"


def test_route_priority_yields_bare_name_to_codebuddy():
    """``route_priority`` 低于 CodeBuddy 时，撞车的裸名归兜底通道。

    回归：``ark.json`` 的对外名常直接写成公共模型名，而 CodeBuddy 静态表里
    也有同名条目；精确轮先到先得，于是加了 ``--ark`` 之后原本走 CodeBuddy 的
    裸名请求被静默改道，并按方舟计费。
    """
    from buddy_proxy.codebuddy_provider.forward import _is_codebuddy_model, _resolve_auto

    assert _is_codebuddy_model(_COLLIDING_NAME), "前提：这个名字在静态表里"
    p = _CollidingProvider(_COLLIDING_NAME, priority=50)
    assert _resolve_auto(SimpleNamespace(providers={"collide": p}), _COLLIDING_NAME) is None
    # 前缀形式不受影响：那条路径不经过 _resolve_auto
    assert p.accepts_model(f"collide/{_COLLIDING_NAME}") is True


def test_route_priority_above_codebuddy_still_claims():
    """高于 100（或压根没声明）的通道照常认领——不能让这个机制误伤既有通道。"""

    from buddy_proxy.codebuddy_provider.forward import _resolve_auto

    high = _CollidingProvider(_COLLIDING_NAME, priority=150)
    assert _resolve_auto(
        SimpleNamespace(providers={"collide": high}), _COLLIDING_NAME) is high
    plain = _CollidingProvider(_COLLIDING_NAME, priority=None)
    assert _resolve_auto(
        SimpleNamespace(providers={"collide": plain}), _COLLIDING_NAME) is plain


def test_route_priority_only_shields_static_table_names():
    """不撞静态表的裸名照常由低优先级通道认领（让位只针对撞车）。"""

    from buddy_proxy.codebuddy_provider.forward import _resolve_auto

    p = _CollidingProvider("my-private-flash", priority=50)
    assert _resolve_auto(
        SimpleNamespace(providers={"collide": p}), "my-private-flash") is p


# --- 真实通道：Qoder 不得在精确轮抢走属于别人的名字 -------------------------
#
# 上面几组用的都是**替身**通道（AliasOnly / Exact / PrefixedAliasProvider），
# 正好漏掉了真正出问题的那条路径：真实 QoderProvider 的 ``models()`` 里带着
# ``qoder/auto`` / ``qoder/glm-5.3`` 这类 id，基类 ``accepts_model`` 会把它们
# 剥前缀后当成"精确匹配"，于是 **精确轮** 就命中——而别名轮的 CodeBuddy 守卫
# 根本管不到精确轮。这些测试用真实 provider 把这条路径锁死。


def _real_qoder():
    from buddy_proxy.qoder.config import REGIONS
    from buddy_proxy.qoder.provider import QoderProvider

    return QoderProvider(next(iter(REGIONS.values())))


def test_real_qoder_yields_auto_to_codebuddy():
    """``auto`` 必须在**两个轮次**都让开，归 CodeBuddy。

    回归（2026-09-28 实测）：Qoder 的档位模型 id 就是 ``auto``，基类
    ``accepts_model`` 在精确轮就认领了它，请求被静默改道到 qoder
    （``provider_route: {"provider": "qoder", "model": "Auto"}`` 是实测日志）。
    ``auto`` 是 CodeBuddy 的默认模型名，没带 model 的请求会被补成它——改道会
    让一整类请求跑到别的通道、按别的通道计费。
    """
    q = _real_qoder()
    for name in ("auto", "Auto", "AUTO"):
        assert q.accepts_model(name, aliases=False) is False, f"{name} 不该在精确轮被 qoder 认领"
        assert q.accepts_model(name, aliases=True) is False, f"{name} 不该在别名轮被 qoder 认领"


def test_real_qoder_and_zcode_overlap_is_resolved_by_registration_order():
    """``glm-5.3`` 恢复由 qoder 精确认领（2026-10-05 目录恢复）。

    2026-10-03 曾实测三方模型整批 enable=false，当时 qoder 在精确轮让路、
    裸名 ``glm-5.3`` 完全归 zcode；2026-10-05 复测目录恢复（权益收回是
    账号级分桶，全量账号 enable=true），qoder 真的又能服务这个模型，恢复
    精确认领。与 zcode 的重叠不再靠「让路」解决，而是由 **model_order**
    仲裁（用户配置里 glm-5.3 的候选序 zcode 前置、qoder 兜底）。
    """
    q = _real_qoder()
    for name in ("glm-5.3", "glm-5.3-flash"):
        assert q.accepts_model(name, aliases=False) is True, (
            f"{name} 目录已恢复，qoder 应在精确轮认领（顺序由 model_order 定）"
        )


def test_real_qoder_still_exactly_claims_its_own_ids():
    """修完不能过度收紧：qoder 自己仍可用的 id 仍要在精确轮被认领。"""
    q = _real_qoder()
    for name in ("qwen3.8-flash", "qwen3.8-max", "qwen3.7-max"):
        assert q.accepts_model(name, aliases=False) is True, f"{name} 应被 qoder 精确认领"


def test_real_qoder_still_claims_official_display_names_via_alias():
    """官方显示名仍走别名轮兜底（按官方文档写模型名是正常用法）。

    只断言仍可调的 Qwen 系——三方模型的显示名（Kimi-K3 等）随目录收回
    一并失效，见上一条。
    """
    q = _real_qoder()
    for name in ("Qwen3.8-Max", "Qwen3.8-Flash"):
        assert q.accepts_model(name, aliases=False) is False
        assert q.accepts_model(name, aliases=True) is True


def test_real_qoder_glm_ids_route_to_zcode_not_qoder(tmp_path, monkeypatch):
    """整链验证：真实 qoder + zcode 并存时，``glm-5.3`` 归 zcode。"""
    from buddy_proxy.codebuddy_provider.forward import _is_codebuddy_model as fwd_is_cb
    from buddy_proxy.providers.zcode import ZcodeProvider

    z, q = ZcodeProvider(), _real_qoder()
    state = _make_state({z.id: z, q.id: q}, tmp_path)
    monkeypatch.setattr(st, "proxy_state", state)

    def _route(model):
        """复刻 forward_chat 的两轮匹配（不含前缀路由）。"""
        for allow_alias in (False, True):
            if allow_alias and fwd_is_cb(model):
                return "codebuddy"
            for p in state.providers.values():
                if p.accepts_model(model, aliases=allow_alias):
                    return p.id
        return "codebuddy"

    assert _route("glm-5.3") == "zcode"
    assert _route("glm-5.3-flash") == "zcode"
    assert _route("auto") == "codebuddy"
    assert _route("Auto") == "codebuddy"
    assert _route("qwen3.8-flash") == "qoder"
