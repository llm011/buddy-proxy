"""/v1/models 对 DSH（dsh-llm-deepseek）的能力契约测试。

DSH 的 listing 解析（``dsh-llm-deepseek/src/listing.ts``）只读 ``/v1/models``
响应的 ``data`` 数组，每个 entry 必须满足：

1. ``api_capabilities.anthropic_messages`` 是对象（缺了直接判 ``usable:false``，
   整条模型被丢弃，回退到默认窗口/输出）；
2. ``context_window`` 是正整数（输入窗口）；
3. ``max_output_tokens`` 是正整数（输出预留）。

第 3 条是压缩崩溃的根因：DSH 压缩器（``dsh-compaction-basic``）按
``messageBudget = contextWindow - reservedCompletionTokens`` 算消息预算，
``reservedCompletionTokens`` 取 listing 声明的 ``max_output_tokens``；缺了就
回退到连接默认 256000（或 route 推断成窗口本身），导致
``reservedCompletionTokens >= contextWindow`` → 预算 ≤ 0 → 抛
``compaction-basic: ... reserves N completion tokens of its N-token context window,
leaving no message budget``。

所以这里锁死两条不变量：
- ``data`` 里每个模型都声明 ``context_window`` / ``max_output_tokens``；
- ``max_output_tokens < context_window``（留足 headroom 65536 + 余量）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from buddy_proxy import __main__ as m
from buddy_proxy.core import state as st
from buddy_proxy import codebuddy_provider as cbp
from buddy_proxy.web.model_list import dsh_capability_fields


def _make_state():
    return SimpleNamespace(
        client=mock.MagicMock(),
        providers={},
        mock_dir=None,
        started_at=1.0,
        enable_desensitize=False,
        enable_optimize_context=False,
        verbose_llm=False,
        write_log=mock.MagicMock(),
        ensure_auth=mock.MagicMock(),
        logger=mock.MagicMock(),
        json_logger=mock.MagicMock(),
        runtime_info={"app_version": "t", "system_version": "t",
                       "python_version": "t", "machine": "t"},
        model_order={},
        disabled_providers=set(),
        hidden_models=set(),
    )


@pytest.fixture()
def client(monkeypatch):
    state = _make_state()
    monkeypatch.setattr(st, "proxy_state", state)
    acct = cbp.creds.AccountRef(id="uid-test", uid="uid-test", nickname="t",
                                priority=0, added_at=1)
    monkeypatch.setattr(cbp.failover, "list_accounts", lambda: [acct])
    cbp.failover._cooldowns.clear()
    monkeypatch.setattr(
        cbp.creds, "ensure_account_token",
        lambda aid, **kw: ("t", {"account_id": aid, "token": "t", "refresh_token": "t",
                                 "uid": "uid-test", "nickname": "t", "machine_id": "m",
                                 "expires_at_ms": 9999999999, "source": "state"}))
    return TestClient(m.app)


# DSH 压缩器默认 headroom 65536（dsh-compaction-basic）；pressure budget =
# (context_window - max_output_tokens) - 65536 必须 > 0，即
# max_output_tokens < context_window - 65536。
_DSH_HEADROOM = 65536


def test_data_entries_declare_dsh_capability_contract(client):
    body = client.get("/v1/models").json()
    assert body["data"], "data 数组不应为空"
    for entry in body["data"]:
        mid = entry["id"]
        # 1. api_capabilities.anthropic_messages 必须是对象
        api_caps = entry.get("api_capabilities")
        assert isinstance(api_caps, dict), f"{mid}: 缺 api_capabilities"
        am = api_caps.get("anthropic_messages")
        assert isinstance(am, dict), f"{mid}: api_capabilities.anthropic_messages 必须是对象"

        # 2. context_window 正整数
        cw = entry.get("context_window")
        assert isinstance(cw, int) and cw > 0, f"{mid}: context_window 必须是正整数，得 {cw!r}"

        # 3. max_output_tokens 正整数，且严格小于 context_window - headroom
        #    （否则 DSH 压缩器 pressure budget ≤ 0 → 崩溃）
        mo = entry.get("max_output_tokens")
        assert isinstance(mo, int) and mo > 0, f"{mid}: max_output_tokens 必须是正整数，得 {mo!r}"
        assert mo < cw - _DSH_HEADROOM, (
            f"{mid}: max_output_tokens({mo}) 必须 < context_window({cw}) - {_DSH_HEADROOM}，"
            f"否则 DSH 压缩预算 ≤ 0 崩溃")


def test_declared_max_output_is_honored_and_safe():
    """配置里写了 max_output 的模型（如 glm-5.4=48000）应被尊重，且被钳制到安全区。"""
    fields = dsh_capability_fields({"context_window": 1_000_000, "max_output": 48000})
    assert fields["context_window"] == 1_000_000
    assert fields["max_output_tokens"] == 48000  # 声明值在安全区内，原样尊重


def test_missing_max_output_gets_safe_default_below_window():
    """未声明 max_output 的模型（如 gpt-5.6-terra）推导安全默认，绝不 ≥ 窗口。"""
    fields = dsh_capability_fields({"context_window": 272000})
    assert fields["context_window"] == 272000
    assert fields["max_output_tokens"] < 272000 - _DSH_HEADROOM, (
        f"默认输出预留 {fields['max_output_tokens']} 必须 < {272000 - _DSH_HEADROOM}，"
        f"否则 DSH pressure budget ≤ 0")
    assert fields["max_output_tokens"] > 0


def test_max_output_clamped_when_declared_too_large():
    """配置误写 max_output ≥ 窗口时，钳制到安全区，防止崩溃。"""
    fields = dsh_capability_fields({"context_window": 272000, "max_output": 272000})
    assert fields["max_output_tokens"] < 272000 - _DSH_HEADROOM


def test_context_window_falls_back_to_max_input():
    """context_window 缺失但 max_input 存在时（聚合后常见），用 max_input 兜底。"""
    fields = dsh_capability_fields({"max_input": 200000, "max_output": 48000})
    assert fields["context_window"] == 200000


def test_glm54_in_data_has_contract_fields(client):
    body = client.get("/v1/models").json()
    glm = next((e for e in body["data"] if e["id"] == "glm-5.4"), None)
    assert glm is not None
    assert isinstance(glm["context_window"], int) and glm["context_window"] > 0
    assert isinstance(glm["max_output_tokens"], int) and glm["max_output_tokens"] > 0
    assert glm["max_output_tokens"] <= 48000  # 配置声明的输出上限
