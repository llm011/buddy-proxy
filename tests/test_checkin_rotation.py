"""轮换周期 ≠ 日历日时，「今天已打过」必须以上游实况为准。

实测（``logs/checkin.jsonl`` + qoder 活动接口，2026-09-30）暴露的问题：

- qoder 的活动窗口是 ``09-29 10:00 → 09-30 09:59``，**10 点轮换**；
- 自动打卡按 ``_today()``（日历日）去重，``09-30 00:01`` 的巡检看到上一轮
  还是 ``checked_in``，就补记了一条 ``date=2026-09-30, ok=True``；
- 于是 ``09-30 10:00`` 新活动开出、上游报 ``claimable`` 时，本地记录已经说
  「今天打过了」→ ``_tick`` 整天跳过，界面也显示「已签到」+ 按钮禁用，
  而那 100 Credits 就此领不到，界面看起来却一切正常。

（注：``logs/checkin.jsonl`` 里 qoder 至今两条记录的 message 都是「今日已
领取」，即都走「上游已签到 → 补记」写入、代理自己没领过——所以这是「会漏」
而非「已漏」。测试断言的是行为，与历史是否真漏过无关。）

零点轮换的通道（codebuddy / trae）不受影响，故这条规则对它们是 no-op：
上游说不可领时，本地记录照旧作数。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest import mock

from buddy_proxy.benefits import BenefitsManager, CheckinHistory
from buddy_proxy.core import settings as settings_mod
from buddy_proxy.core.metrics import MetricsCollector


class _RotatingCheckin:
    """上游按「非日历日」轮换的通道（qoder 的形态）。

    ``claimable`` 可由测试翻转，模拟 10:00 新活动开出。
    """

    id = "rotating"
    name = "Rotating"
    supports_checkin = True

    def __init__(self, claimable: bool = False):
        self.claimable = claimable
        self.checked_in = not claimable
        self.claim_calls = 0
        self.status_calls = 0

    def checkin_status(self):
        self.status_calls += 1
        return {"checked_in": self.checked_in, "claimable": self.claimable,
                "inactive": False, "streak_days": 1, "message": "ok",
                "next_ts": int(time.time()) + 3600, "next_ts_source": "upstream"}

    def checkin_claim(self):
        self.claim_calls += 1
        self.claimable = False
        self.checked_in = True
        return {"checked_in": True, "extra_credits": 100, "message": "ok"}

    def quota(self):
        return None


def _manager(tmp_path, monkeypatch, providers, **settings):
    """构造 BenefitsManager（顶掉默认 codebuddy 注入，避免碰真上游）。

    ``settings_mod.save_settings`` 会写 ``~/.buddy-proxy/settings.json``——
    不重定向就会把用户真实的 ``auto_checkin`` / ``model_order`` 覆盖掉，
    所以这里必须先沙箱化（``settings_path()`` 每次调用都读 env，无缓存）。
    """
    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(tmp_path / "settings.json"))
    providers = {"codebuddy": mock.MagicMock(supports_checkin=False,
                                             quota=lambda: None), **providers}
    client = mock.MagicMock()
    client.session = {}
    state = SimpleNamespace(
        client=client, providers=providers, mock_dir=None,
        started_at=time.time(), enable_desensitize=False,
        enable_optimize_context=False, verbose_llm=False,
        default_provider="codebuddy", default_model=None,
        metrics=MetricsCollector(None),
        write_log=mock.MagicMock(), ensure_auth=mock.MagicMock(),
        logger=mock.MagicMock(), json_logger=mock.MagicMock(),
        runtime_info={},
    )
    settings_mod.save_settings({"auto_checkin": True, "checkin_time": "00:00", **settings})
    mgr = BenefitsManager(tmp_path / "checkin.jsonl", state)
    state.benefits = mgr
    return state, mgr


def _predate_history(tmp_path, pid: str = "rotating") -> None:
    """写一条「今天已打过」的本地记录，复现 00:01 巡检留下的补记。"""
    CheckinHistory(tmp_path / "checkin.jsonl").append(pid, ok=True, message="今日已领取")


# --- 自动打卡循环 -----------------------------------------------------------


def test_tick_claims_when_upstream_says_claimable_despite_local_record(tmp_path, monkeypatch):
    """上游说可领 → 领，哪怕本地记录写着今天已打过。

    这是「会漏领」的直接复现：只有日历日记录、没有上游实况时，``_tick``
    会在 ``continue`` 里跳过一整天。
    """
    p = _RotatingCheckin(claimable=True)
    _predate_history(tmp_path)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    assert CheckinHistory(tmp_path / "checkin.jsonl").ok_dates_by_provider()["rotating"], \
        "前置条件：本地必须已有今天的成功记录"

    asyncio.run(mgr._tick())
    assert p.claim_calls == 1, "上游报 claimable 就必须领，不能被本地日历日记录挡住"


def test_tick_still_skips_when_local_record_and_upstream_quiet(tmp_path, monkeypatch):
    """上游没报可领 + 本地有今天记录 → 跳过（零点轮换通道的正常路径，不能回归）。"""
    p = _RotatingCheckin(claimable=False)
    _predate_history(tmp_path)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    asyncio.run(mgr._tick())
    assert p.claim_calls == 0


def test_tick_does_not_double_claim_within_same_round(tmp_path, monkeypatch):
    """领完之后再巡检一次不该重复领（幂等仍要保住）。"""
    p = _RotatingCheckin(claimable=True)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    asyncio.run(mgr._tick())
    assert p.claim_calls == 1
    # 领取后上游翻成 checked_in，且缓存已作废 → 重查也不会再领
    mgr._cache.clear()
    asyncio.run(mgr._tick())
    assert p.claim_calls == 1


def test_tick_reuses_snapshot_cache_instead_of_hitting_upstream_again(tmp_path, monkeypatch):
    """``_tick`` 走 ``_cached``：管理页开着时不该因为这次改动多打上游。"""
    p = _RotatingCheckin(claimable=False)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    asyncio.run(mgr.snapshot())
    calls_after_snapshot = p.status_calls
    assert calls_after_snapshot >= 1
    asyncio.run(mgr._tick())
    assert p.status_calls == calls_after_snapshot, "TTL 内应命中缓存，不再查上游"


def test_claim_invalidates_status_cache(tmp_path, monkeypatch):
    """领取后作废状态缓存：界面不必等满 TTL 才从「立即打卡」翻成「已签到」。"""
    p = _RotatingCheckin(claimable=True)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    asyncio.run(mgr.snapshot())
    assert mgr._cache.get("checkin:rotating") is not None
    asyncio.run(mgr.claim_now("rotating"))
    assert "checkin:rotating" not in mgr._cache, "领完必须清缓存，否则界面显示旧状态"


# --- 界面口径（snapshot）---------------------------------------------------


def test_snapshot_done_today_false_when_claimable(tmp_path, monkeypatch):
    """上游报可领 → ``done_today`` 必须为 False，按钮才不会被禁用。

    这条是「界面说谎」的直接断言：旧逻辑 ``today in done or checked_in`` 会把
    已补记的今天算成打过了，按钮 disabled，用户看不到自己还有 Credits 可领。
    """
    p = _RotatingCheckin(claimable=True)
    _predate_history(tmp_path)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    snap = asyncio.run(mgr.snapshot())
    ck = next(x["checkin"] for x in snap["providers"] if x["id"] == "rotating")
    assert ck["done_today"] is False
    assert ck["claimable"] is True


def test_snapshot_done_today_true_when_not_claimable(tmp_path, monkeypatch):
    """上游没报可领 + 今天有记录 → ``done_today`` 照旧为 True（不能回归）。"""
    p = _RotatingCheckin(claimable=False)
    _predate_history(tmp_path)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    snap = asyncio.run(mgr.snapshot())
    ck = next(x["checkin"] for x in snap["providers"] if x["id"] == "rotating")
    assert ck["done_today"] is True


def test_snapshot_done_today_survives_status_query_failure(tmp_path, monkeypatch):
    """上游查询失败（兜错结构）时退回本地记录，别把「打过了」显示成「没打」。

    失败结构里没有 ``claimable``，若被当成「不可领」无所谓；但也不能因为
    拿不到实况就把本地已完成的记录忽略掉。
    """
    class _Failing(_RotatingCheckin):
        def checkin_status(self):
            raise RuntimeError("gateway unreachable")

    p = _Failing(claimable=False)
    _predate_history(tmp_path)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    snap = asyncio.run(mgr.snapshot())
    ck = next(x["checkin"] for x in snap["providers"] if x["id"] == "rotating")
    assert ck["done_today"] is True
    assert "error" in ck


def test_snapshot_marks_query_failed_when_status_is_none(tmp_path, monkeypatch):
    """声明支持打卡却返回裸 ``None`` → 标 ``query_failed``，别退化成「未签到」。

    这是 dumate 报障的通用防线：拿不到状态（未登录/网络失败）时，前端只在
    ``error``/``query_failed`` 下才显示「状态未知」，否则会画成「未签到」+ 可点的
    「立即打卡」——用户看到的就是「今天还没打卡」，而实况是「查不到」。
    """
    class _NoneStatus(_RotatingCheckin):
        def checkin_status(self):
            self.status_calls += 1
            return None

    p = _NoneStatus(claimable=False)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    snap = asyncio.run(mgr.snapshot())
    ck = next(x["checkin"] for x in snap["providers"] if x["id"] == "rotating")
    assert ck["query_failed"] is True
    assert ck.get("message")
    assert ck["done_today"] is False
    # 失败结构按「查询失败」短 TTL 缓存（网络恢复/登录态出现后尽快自愈）
    from buddy_proxy.benefits import _is_failure
    assert _is_failure(ck) is True


def test_tick_does_not_claim_when_status_is_none(tmp_path, monkeypatch):
    """裸 ``None`` 时不该去 claim（拿不到状态就对上游打 claim 会刷失败记录）。"""
    class _NoneStatus(_RotatingCheckin):
        def checkin_status(self):
            self.status_calls += 1
            return None

    p = _NoneStatus(claimable=True)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    asyncio.run(mgr._tick())
    assert p.claim_calls == 0


def test_tick_does_not_claim_when_upstream_contradicts_itself(tmp_path, monkeypatch):
    """``checked_in`` 与 ``claimable`` 同时为真 → 按「已打」处理，不重复领。

    三家真实 provider 都保证这俩互斥（qoder 的 ``checked_in`` 就是
    ``claimed and not claimable``），同时为真只能是上游自相矛盾。此时宁可
    不打：claim 虽幂等，但每轮巡检都会多打一次上游。
    """
    class _Contradictory(_RotatingCheckin):
        def checkin_status(self):
            self.status_calls += 1
            return {"checked_in": True, "claimable": True, "inactive": False,
                    "message": "矛盾态"}

    p = _Contradictory(claimable=True)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    asyncio.run(mgr._tick())
    assert p.claim_calls == 0, "自相矛盾时不该领，免得每轮多打一次上游"

    snap = asyncio.run(mgr.snapshot())
    ck = next(x["checkin"] for x in snap["providers"] if x["id"] == "rotating")
    assert ck["done_today"] is True


def test_snapshot_cache_expires_at_the_rotation_it_advertises(tmp_path, monkeypatch):
    """倒计时数到「即将刷新」时，下一次轮询必须真拿到新状态。

    这是倒计时功能自己引入的失效场景：界面上那个「下次时间」就是 ``next_ts``，
    而它按契约正是状态翻转的时刻。可 ``_cached`` 原本只认固定 TTL（300s），
    翻转点若落在 TTL 之内，到点后的那次轮询仍会命中翻篇前的快照 —— 界面继续
    显示「已签到」+ 按钮禁用，要等满 5 分钟才自愈。qoder 的窗口错过即失效，
    这 5 分钟足够丢掉一整轮，倒计时那句「即将刷新」也就成了空话。

    所以把 ``next_ts`` 本身当作到期时刻。这里让翻转点落在 TTL **之内**：
    取 300 的话缓存到点本就自然过期，测的就不是本 bug 而是普通 TTL 过期
    （我第一版就是这么写错的，去掉修复照样「通过」）。
    """
    boundary = time.time() + 30          # 远小于 SNAPSHOT_TTL_S，却已到点
    assert boundary - time.time() < 300, "翻转点必须在 TTL 之内才有意义"

    class _Boundary(_RotatingCheckin):
        def checkin_status(self):
            self.status_calls += 1
            if time.time() >= boundary:
                self.claimable, self.checked_in = True, False
            return {"checked_in": self.checked_in, "claimable": self.claimable,
                    "inactive": False, "message": "ok",
                    "next_ts": int(boundary), "next_ts_source": "upstream"}

    p = _Boundary(claimable=False)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    def status_of():
        snap = asyncio.run(mgr.snapshot())
        return next(x["checkin"] for x in snap["providers"] if x["id"] == "rotating")

    before = status_of()
    assert before["done_today"] is True and before.get("claimable") is False
    calls_before = p.status_calls

    # 模拟倒计时归零：把 manager 看到的时钟推过 next_ts（缓存条目仍是 30s 内）
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: boundary + 1)
    after = status_of()
    monkeypatch.setattr(time, "time", real_time)

    assert p.status_calls > calls_before, "到点后必须重查上游，不能吃翻篇前的缓存"
    assert after.get("claimable") is True, "到点后界面应显示可领"
    assert after["done_today"] is False, "到点后按钮不该还是禁用的"


def test_state_flipped_predicate_ignores_junk():
    """``_state_flipped`` 拿不准一律判否——坏字段不该把缓存整个废掉。"""
    from buddy_proxy.benefits import FLIP_GRACE_S, _state_flipped

    now = 1_000_000.0
    assert _state_flipped({"next_ts": now - 1}, now) is True    # 刚过点
    assert _state_flipped({"next_ts": now}, now) is True        # 此刻即翻转
    assert _state_flipped({"next_ts": now + 1}, now) is False   # 还没到
    assert _state_flipped({}, now) is False                     # 无字段（额度结果）
    assert _state_flipped(None, now) is False
    assert _state_flipped("nope", now) is False
    assert _state_flipped({"next_ts": "1000"}, now) is False     # 字符串不认
    assert _state_flipped({"next_ts": True}, now) is False       # bool 不是时刻
    # 宽限窗口边界：窗口内认、出了窗口不认
    assert _state_flipped({"next_ts": now - FLIP_GRACE_S}, now) is True
    assert _state_flipped({"next_ts": now - FLIP_GRACE_S - 1}, now) is False


def test_stale_next_ts_does_not_defeat_the_cache(tmp_path, monkeypatch):
    """上游一直给**很早以前**的 ``next_ts`` 时，不能把缓存变成每次都打上游。

    这是给上面那条提前失效加的护栏：正常轮换过点后重查一次，上游就会给出
    下一个（未来的）时刻；若重查回来仍是老早的值（活动停了但字段没更新），
    再拿它当到期条件就等于永久废掉缓存——管理页 30s 轮询 + 后台巡检都走
    这里，会把上游打爆。
    """
    class _StaleNext(_RotatingCheckin):
        def checkin_status(self):
            self.status_calls += 1
            return {"checked_in": self.checked_in, "claimable": self.claimable,
                    "inactive": False, "message": "ok",
                    "next_ts": int(time.time()) - 3600,   # 早过点，且一直这样
                    "next_ts_source": "upstream"}

    p = _StaleNext(claimable=False)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    for _ in range(5):
        asyncio.run(mgr.snapshot())
    assert p.status_calls == 1, "过期的 next_ts 不该让缓存每次失效"


def test_claimable_now_predicate_ignores_junk():
    """``claimable_now`` 对非 dict / 兜错结构 / 缺字段一律判否，不抛。"""
    from buddy_proxy.benefits import claimable_now

    assert claimable_now(None) is False
    assert claimable_now("nope") is False
    assert claimable_now({}) is False
    assert claimable_now({"error": "boom", "claimable": True}) is False
    assert claimable_now({"claimable": True, "checked_in": True}) is False
    assert claimable_now({"claimable": True}) is True
    assert claimable_now({"claimable": True, "checked_in": False}) is True


def test_quota_cache_key_tracks_provider_epoch(tmp_path, monkeypatch):
    """quota 缓存键带 provider 声明的 ``quota_epoch``：代一变就重查，不回旧快照。

    实测（2026-10-03）：antigravity 加了账号 #2 后，benefits 的旧单账号快照
    还在缓存里顶满 TTL（300s），前端只渲染出一份额度，看起来就像多账号被
    合并了。benefits 层不认识「账号列表」，只能由 provider 自己声明缓存代
    （antigravity 用账号指纹；没声明这个方法的通道键保持原样，不受影响）。
    """
    calls = []

    class _Epoch:
        id = "epochy"
        name = "Epochy"
        supports_checkin = False
        n = 0

        def quota_epoch(self):
            return f"gen-{self.n}"

        def quota(self):
            calls.append(self.n)
            return {"items": [{"label": f"gen-{self.n}", "remaining": 1,
                               "total": 2, "used": None, "percent": None,
                               "reset_ts": None}]}

    p = _Epoch()
    state, mgr = _manager(tmp_path, monkeypatch, {"epochy": p})

    snap1 = asyncio.run(mgr.snapshot())
    asyncio.run(mgr.snapshot())                    # 同代 → 缓存命中，不重查
    assert calls == [0], "同一缓存代内不该重复打 quota"
    p.n = 1
    snap3 = asyncio.run(mgr.snapshot())            # 代变 → 重查新代
    assert calls == [0, 1], "缓存代变了必须重查，旧快照不能继续顶"

    q1 = next(x for x in snap1["providers"] if x["id"] == "epochy")["quota"]
    q3 = next(x for x in snap3["providers"] if x["id"] == "epochy")["quota"]
    assert q1["items"][0]["label"] == "gen-0"
    assert q3["items"][0]["label"] == "gen-1"
