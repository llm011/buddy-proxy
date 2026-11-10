"""负载均衡后台预热:缓存键与面板同源、过期时喂的是新值。

review 里发现的两个问题:

1. 预热自己拼了 ``quota:{pid}``,而面板(``_provider_entry``)的键带
   ``quota_epoch`` 后缀,缓存里躺两份,开着管理页时每轮预热多打一次计费接口。
2. 预热走 ``_cached`` 的 stale-while-revalidate:过期后先回旧值,而预热间隔
   (10 分钟)比 TTL(5 分钟)长,每轮喂进权重表的都是上一轮的数。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from buddy_proxy import benefits
from buddy_proxy.core import load_balance as lb


class _Prov:
    id = "kimi"

    def __init__(self, epoch: str | None = None):
        self.calls = 0
        self.rem = {"1": 100.0, "2": 900.0}
        if epoch is not None:
            self.quota_epoch = lambda: epoch

    def quota(self):
        self.calls += 1
        return {"items": [
            {"label": "Kimi #1 · 额度", "remaining": self.rem["1"]},
            {"label": "Kimi #2 · 额度", "remaining": self.rem["2"]},
        ]}

    def _quota_account_ids(self):
        return {"1": "a", "2": "b"}


@pytest.fixture(autouse=True)
def _clean_boards():
    lb.clear_boards()
    yield
    lb.clear_boards()


def _mgr(tmp_path: Path, prov: _Prov, enabled=("kimi",)):
    state = SimpleNamespace(load_balance=set(enabled), providers={"kimi": prov})
    return benefits.BenefitsManager(tmp_path / "checkin.jsonl", state)


def test_quota_key_carries_epoch_when_declared():
    assert benefits.BenefitsManager._quota_key("kimi", _Prov("e1")) == "quota:kimi:e1"
    assert benefits.BenefitsManager._quota_key("kimi", _Prov()) == "quota:kimi"


def test_warm_round_reuses_the_panels_cache_entry(tmp_path):
    prov = _Prov(epoch="e1")
    mgr = _mgr(tmp_path, prov)

    async def run():
        # 面板先按自己的键查过一次
        await mgr._cached(mgr._quota_key("kimi", prov), prov.quota)
        await mgr._warm_load_balance_once()

    asyncio.run(run())
    assert prov.calls == 1, "预热应复用面板刚查的缓存,而不是再打一次上游"
    assert [k for k in mgr._cache if k.startswith("quota:kimi")] == ["quota:kimi:e1"]
    assert lb.board("kimi").weight_of("b") == 900.0


def test_warm_round_requeries_when_cache_expired(tmp_path):
    prov = _Prov()
    mgr = _mgr(tmp_path, prov)

    async def run():
        await mgr._warm_load_balance_once()
        assert lb.board("kimi").weight_of("a") == 100.0
        # 上游额度翻转,且缓存已过期(模拟 10 分钟后的下一轮)
        prov.rem = {"1": 900.0, "2": 100.0}
        key = mgr._quota_key("kimi", prov)
        ts, data = mgr._cache[key]
        mgr._cache[key] = (ts - benefits.SNAPSHOT_TTL_S - 1, data)
        await mgr._warm_load_balance_once()

    asyncio.run(run())
    assert prov.calls == 2
    assert lb.board("kimi").weight_of("a") == 900.0, "权重应是新值,不是上一轮的旧数"
    assert lb.board("kimi").weight_of("b") == 100.0


def test_warm_round_is_noop_without_any_switch(tmp_path):
    prov = _Prov()
    mgr = _mgr(tmp_path, prov, enabled=())
    asyncio.run(mgr._warm_load_balance_once())
    assert prov.calls == 0
    assert not mgr._cache
