"""账号冷却跨进程重启持久化（只对显式开启、且冷却够长的通道生效）。

## 为什么破例落盘

``core/account_failover.py`` 的原设计是「刻意不落盘」：冷却只是此刻的健康
状态，重启后重探一次的代价远小于带着不信任启动。对 60s / 5min 的短冷却
这个判断成立，本 PR **不改默认行为**。

但对**上游风控**（raccoon 200108「系统检测到当前账号存在异常调用」，冷却
30 分钟）它不成立——重探不是免费的，重探本身就是「异常调用」：

2026-11-26 01:50:41 重启后：raccoon 39 次请求 **0 次成功**，重启 +3 秒内
10 条风控冷却。冷却状态随进程消失 → 5 个号带着「已被标记」的状态立刻全部
重打上游 → 同一秒全部重新判风控、重新冷却 30 分钟。重启等于主动延长封禁。
（同日另 4 次重启都没触发：账号本来干净时重探无害。）

所以：**默认不落盘；通道显式开启后才落盘，且只落超过阈值的冷却**。
"""

from __future__ import annotations

import json
import time

import pytest

from buddy_proxy.core import account_failover as af
from buddy_proxy.core.account_failover import CooldownTracker


class _Store:
    """快照文件的薄封装（闭包式：类体里读不到外层局部变量，别用 class 属性）。"""

    def __init__(self, path):
        self.path = path

    def load(self):
        return af.load_persisted(self.path)

    def save(self, snap):
        af.persist_snapshot(self.path, snap)


@pytest.fixture()
def store(tmp_path):
    return _Store(tmp_path / "rac" / "cooldowns.json")


# -- 快照文件读写 ------------------------------------------------------------


def test_persist_then_load_round_trip(store):
    snap = {"a": (120.0, "account"), "b": (1800.0, "quota")}
    store.save(snap)
    got = store.load()
    assert set(got) == {"a", "b"}
    # 落盘的是绝对到期时刻（墙钟），读回时换算回剩余秒
    left_a, kind_a = got["a"]
    # 快照保留毫秒，舍入后可能比原值多出零点几毫秒：上界留余量，别把浮点精度写死
    assert 118 <= left_a <= 120.01
    assert kind_a == "account"
    assert got["b"][1] == "quota"


def test_load_missing_or_corrupt_file_is_empty(tmp_path):
    assert af.load_persisted(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert af.load_persisted(bad) == {}, "损坏文件不能让服务起不来"


def test_load_drops_expired_and_garbage_entries(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        "version": 1,
        "entries": {
            "gone": [time.time() - 5, "account"],      # 已过期
            "keep": [time.time() + 600, "quota"],
            "bad": "not-a-pair",                       # 形状不对
            "neg": [time.time() + 600, "quota", 1],   # 多余字段
        },
    }), encoding="utf-8")
    got = af.load_persisted(path)
    assert set(got) == {"keep"}


def test_load_rejects_unknown_version(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"version": 99, "entries": {"x": [time.time() + 99, "account"]}}),
                    encoding="utf-8")
    assert af.load_persisted(path) == {}, "未来版本的文件读不懂就当没有，别乱恢复"


def test_persisted_file_is_private(store):
    store.save({"a": (60.0, "account")})
    assert (store.path.stat().st_mode & 0o777) == 0o600, "0600，与账号凭据同口径"


# -- tracker 集成：默认不落盘 -----------------------------------------------


def test_default_tracker_does_not_persist(tmp_path):
    path = tmp_path / "d.json"
    tr = CooldownTracker(persist_path=path)
    tr.mark("a")
    assert not path.exists(), "默认（未开启）不落盘：保持原设计的内存语义"


def test_persist_only_when_enabled(tmp_path):
    path = tmp_path / "d.json"
    tr = CooldownTracker(persist_path=path, persist=True)
    tr.mark("a", kind="quota")
    assert path.exists(), "开启且超过阈值才落盘"
    path.unlink()
    # 同一个 tracker，未开启 → 不落盘（对照：确认落盘是 persist=True 的功劳）
    CooldownTracker(persist_path=path, persist=False).mark("a", kind="quota")
    assert not path.exists()


def test_short_cooldown_below_threshold_not_persisted(tmp_path):
    """60s 的短冷却重启清零无害（重探免费），不落盘——少一次磁盘写。"""
    path = tmp_path / "d.json"
    tr = CooldownTracker(persist_path=path, persist=True, persist_min_seconds=300.0)
    tr.mark("a", kind="account")
    assert not path.exists(), "60s 短冷却不触发写盘（重探免费）"
    tr.mark("b", kind="quota")
    assert path.exists(), "5min 额度冷却超过阈值，落盘"
    # 快照是「当前全部未过期冷却」：b 触发写盘时 a 仍在内存，会一并带上。
    # 这无害——恢复时按墙钟扣减，a 只剩 <60s、重启后基本已过期被丢弃；即使没
    # 过期，恢复它也只是如实延续。断言锁的是「短冷却不触发写盘」，不是「盘里
    # 没有短冷却」。
    assert "b" in json.loads(path.read_text(encoding="utf-8"))["entries"]


def test_restore_on_restart_skips_cooling_account(tmp_path):
    """核心场景：重启前账号在风控冷却里，重启后**不能**立刻再打它。"""
    path = tmp_path / "d.json"
    first = CooldownTracker(persist_path=path, persist=True, persist_min_seconds=300.0)
    first.mark("raccoon-1", kind="account", retry_after="1800")

    # 进程重启：新建 tracker，从同一份文件恢复
    second = CooldownTracker(persist_path=path, persist=True, persist_min_seconds=300.0)
    left, kind = second.left("raccoon-1")
    # 锁的是「按墙钟差扣减、不重置回满 30 分钟」：快照保留毫秒，舍入后可能比
    # 1800 多出零点几毫秒，上界留一点余量，别把浮点精度写死进断言。
    assert 1790 <= left <= 1800.01, "剩余冷却要按墙钟差扣减，不能重置成满 30 分钟"
    assert kind == "account"
    assert second.is_cooling("raccoon-1") is True


def test_clear_wipes_persisted_entry(tmp_path):
    path = tmp_path / "d.json"
    tr = CooldownTracker(persist_path=path, persist=True, persist_min_seconds=300.0)
    tr.mark("a", kind="quota")
    tr.clear("a")
    assert af.load_persisted(path) == {}


def test_persist_failure_never_breaks_mark(tmp_path, monkeypatch):
    """落盘是旁路：磁盘满/只读/权限问题都不能把 mark 搞挂。"""
    path = tmp_path / "ro" / "d.json"
    tr = CooldownTracker(persist_path=path, persist=True, persist_min_seconds=300.0)
    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(af, "persist_snapshot", boom)
    assert tr.mark("a", kind="quota") == af.DEFAULT_QUOTA_COOLDOWN_S, "mark 仍返回时长"
    assert tr.is_cooling("a") is True, "内存状态照常生效"


def test_clear_all_on_never_persisted_tracker_writes_nothing(tmp_path):
    """从未落过盘的 tracker 调 clear_all（测试隔离常用）不该凭空造出一个空文件。

    空快照读回就是 {}，无害；但 clear_all 在测试夹具里每条用例都调，等于每个
    测试白写一次盘。没有任何未过期冷却时直接跳过。
    """
    path = tmp_path / "d.json"
    tr = CooldownTracker(persist_path=path, persist=True, persist_min_seconds=300.0)
    tr.mark("a", kind="account")  # 60s，低于阈值，不落盘
    tr.clear_all()
    assert not path.exists(), "没有需要落盘的冷却时不该写文件"


def test_clear_last_long_cooldown_rewrites_file_as_empty(tmp_path):
    """对照：文件已存在时，清掉最后一个长冷却要回写成空快照——否则重启后
    那条冷却会「复活」。"""
    path = tmp_path / "d.json"
    tr = CooldownTracker(persist_path=path, persist=True, persist_min_seconds=300.0)
    tr.mark("a", kind="quota")  # 300s，落盘
    assert path.exists()
    tr.clear("a")
    assert path.exists(), "文件已存在必须回写"
    assert af.load_persisted(path) == {}, "清掉后重启不能复活这条冷却"
