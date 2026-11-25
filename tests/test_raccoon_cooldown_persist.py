"""raccoon 账号冷却跨重启持久化：风控（200108）不能被重启「赦免」。

## 场景（2026-11-26 01:50:41 实测）

raccoon 5 个账号全部在 30 分钟风控冷却中 → 服务重启 → 冷却状态随进程消失
→ 新进程 +3 秒内把 5 个号全部重打上游 → 同一秒全部重新判 200108、重新冷却
30 分钟。该区间 39 次请求 0 次成功。重启等于主动延长封禁。

``core/account_failover`` 默认不落盘（短冷却重探免费）；raccoon 是唯一显式开启
的通道——它的风控冷却 30 分钟，且重探本身就是「异常调用」。
"""

from __future__ import annotations

import time

import pytest

from buddy_proxy.core import account_failover as af
from buddy_proxy.raccoon import failover


def _risk_control(acct: str) -> float:
    return failover.mark_cooldown(
        acct, retry_after="1800", reason="账号风控: {\"error\":{\"code\":200108}}")


def _swap_tracker(monkeypatch):
    """换上一个新的持久化 tracker，并把模块函数用的全局名与活引用一起换掉。

    ``mark_cooldown`` 等模块函数在调用时解析全局 ``_tracker``；而 ``_cooldowns``
    是另一个全局名（测试用的活引用），只换前者的话断言读到的还是旧 dict。
    """
    tr = failover._new_tracker()
    monkeypatch.setattr(failover, "_tracker", tr)
    monkeypatch.setattr(failover, "_cooldowns", tr._cooldowns)
    return tr


def test_risk_control_cooldown_survives_restart(tmp_path, monkeypatch):
    """重启前在风控冷却里，重启后不能立刻再打它，且剩余时长按墙钟差扣减。"""
    monkeypatch.setenv("RACCOON_STATE_DIR", str(tmp_path))
    _swap_tracker(monkeypatch)
    _risk_control("pu-a")
    assert "pu-a" in failover._cooldowns

    # 进程重启：重新构造 tracker，从同一份文件恢复
    restarted = failover._new_tracker()
    left, kind = restarted.left("pu-a")
    assert 1790 <= left <= 1800.01, "按墙钟差扣减，不能重置回满 30 分钟"
    assert kind == "account"
    assert restarted.is_cooling("pu-a") is True


def test_short_cooldown_does_not_hit_disk(tmp_path, monkeypatch):
    """60s 的账号级短冷却重启清零无害，不落盘——保持默认的内存语义。

    注意「账号 × 模型」的频率窗口（``mark_model_cooldown``）**会**落盘：它
    走 quota 档、不传 ``retry_after`` 时是 300s，正好到阈值。这是对的——
    上游那个窗口恢复中位数 25 分钟，重启后立刻重打只会再撞一次 429。
    （``_MODEL_RATE_COOLDOWN_S = 60`` 是个没人引用的死常量，别被它误导。）
    """
    monkeypatch.setenv("RACCOON_STATE_DIR", str(tmp_path))
    # 统一走模块级 tracker：mark_model_cooldown / model_cooldown_left 解析的是
    # 模块全局 _tracker，与局部新建的 tracker 不是同一个状态。
    _swap_tracker(monkeypatch)
    failover.mark_cooldown("pu-b")  # 默认 60s
    path = tmp_path / "cooldowns.json"
    assert not path.exists(), "60s 短冷却不触发写盘"

    failover.mark_model_cooldown("pu-a", "sn-deepseek-v4-1-flash-1017")
    assert path.exists(), "模型级频率窗口（300s）够长，要落盘"
    left, _ = failover.model_cooldown_left("pu-a", "sn-deepseek-v4-1-flash-1017")
    assert 290 < left <= 300.01, "模型级冷却实际是 300s（quota 档默认），不是 60s"


def test_deleted_account_keys_are_purged_on_restart(tmp_path, monkeypatch):
    """已删账号的键（含 ``账号\\x00模型`` 复合键）重启时剔除，不留幽灵条目。

    持久化前这无关紧要（进程重启自然清空）；落盘后它会跨重启一直活着，
    ``clear(账号id)`` 又清不掉复合键（键是拼接的，不等于账号 id）。
    """
    monkeypatch.setenv("RACCOON_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(failover, "list_accounts",
                        lambda: [type("A", (), {"id": "pu-alive"})()])
    # 模拟旧进程留下的快照：一个活账号、一个已删账号、一个已删账号的模型级键
    from buddy_proxy.core import account_failover as af
    af.persist_snapshot(tmp_path / "cooldowns.json", {
        "pu-alive": (900.0, "account"),
        "pu-gone": (900.0, "account"),
        "pu-gone\x00sn-deepseek-v4-1-flash-1017": (900.0, "quota"),
    })
    tr = failover._new_tracker()
    assert tr.is_cooling("pu-alive") is True, "活账号的冷却要保留"
    assert tr.is_cooling("pu-gone") is False, "已删账号的键要剔除"
    assert tr.is_cooling("pu-gone\x00sn-deepseek-v4-1-flash-1017") is False, \
        "已删账号的模型级复合键也要剔除"


def test_persist_file_lives_in_raccoon_state_dir(tmp_path, monkeypatch):
    """落在 raccoon 自己的状态目录：与凭据同目录，RACCOON_STATE_DIR 可隔离。"""
    monkeypatch.setenv("RACCOON_STATE_DIR", str(tmp_path / "sub"))
    tr = failover._new_tracker()
    tr.mark("pu-a", kind="account", retry_after="1800")
    assert (tmp_path / "sub" / "cooldowns.json").exists()


def test_clear_all_wipes_persisted_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RACCOON_STATE_DIR", str(tmp_path))
    tr = failover._new_tracker()
    tr.mark("pu-a", kind="account", retry_after="1800")
    assert (tmp_path / "cooldowns.json").exists()
    tr.clear_all()
    # 文件保留但内容为空快照（不删文件：别的进程/下次启动读到损坏的半截文件
    # 比读到空快照更糟）。关键是重启后这条冷却不能复活。
    assert af.load_persisted(tmp_path / "cooldowns.json") == {}, "清空后盘上不能残留"


def test_clear_cooldown_also_clears_model_composite_keys(tmp_path, monkeypatch):
    """删账号时管理页调的是 ``clear_cooldown(账号id)``，必须连「账号\\x00模型」
    复合键一起清。

    持久化前漏掉无所谓（进程重启清空）；落盘后复合键会一直活着——账号删了、
    它的模型级冷却还在盘上，下次重启被恢复成一个谁也对不上的幽灵条目
    （对账要等重启才跑，删账号当下的进程里它一直占着）。
    """
    monkeypatch.setenv("RACCOON_STATE_DIR", str(tmp_path))
    _swap_tracker(monkeypatch)
    model = "sn-deepseek-v4-1-flash-1017"
    failover.mark_cooldown("pu-a", retry_after="1800")          # 账号键
    failover.mark_model_cooldown("pu-a", model)                  # 复合键
    failover.mark_model_cooldown("pu-b", model)                  # 别的账号，不能误伤

    failover.clear_cooldown("pu-a")

    assert failover.cooldown_left("pu-a")[0] == 0
    assert failover.model_cooldown_left("pu-a", model)[0] == 0, "复合键要一起清"
    assert failover.model_cooldown_left("pu-b", model)[0] > 0, "别的账号不受影响"
    # 盘上也不能残留（否则重启后复合键复活）
    assert set(af.load_persisted(tmp_path / "cooldowns.json")) == {f"pu-b\x00{model}"}
