"""跨 provider 的目标级冷却标记：失败换档后短时间内跳过该 (provider, model)。

与 ``trae/pat/cooldown.py`` 的 ``_channel_exhausted`` 同构：**内存** TTL 字典 +
``threading.Lock``，到期惰性自动恢复；短窗口内反复失败升级为长冷却。

刻意**不落盘**（不写 ``~/.buddy-proxy/``）：

1. 要照抄的先例 ``_channel_exhausted`` 就是内存态，PAT 那边把理由写明了——「重启后
   重新探测一次的代价远小于误冷却」；
2. 进程重启常常正是因为用户刚修好上游（换 key、改订阅），启动就带着对某个目标的不信任
   比多探测一次更糟；
3. 标记是高 churn 写入，持久化意味着每次失败都 fsync，与 settings.json 争同一条原子写
   路径；
4. ``settings.py`` 拥有的是「用户意图」（停用/时段/顺序），冷却只是**此刻的健康状态**，
   不是用户意图。

本模块是**叶子**模块：只 import 标准库，因此从任何地方 import 都不会造成循环导入
（这条「叶子模块不得反向依赖」的约定由 ``tests/test_module_boundaries.py`` 守，
见 ``docs/architecture.md`` 的 *Module boundaries*）。
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

#: 首档冷却时长（秒）。与 PAT 的 ``_CHANNEL_EXHAUSTED_TTL_S`` 同量级：上游抖动
#: 通常在分钟级自愈，5 分钟足够换开、又不会把偶尔抖一下的通道长期拉黑。
_TARGET_COOLDOWN_S = 300
#: 升级计数的滑动窗口（秒）。
_ESCALATE_WINDOW_S = 300
#: 窗口内第 N 次失败 → 升级为长冷却。
_ESCALATE_HITS = 3
#: 升级档上限（秒）= 1 小时。刻意**不**照搬 PAT 的「冷却到次日」：模型级失败多为上游
#: 抖动而非当日额度耗尽，1 小时足够且误伤小。
_ESCALATE_COOLDOWN_S = 3600

#: 机器级故障的判定窗口（秒）与触发通道数：窗口内 **≥N 个不同通道**对同一模型失败
#: → 判为机器级故障（本机网络/代理抖动），见 :func:`mark_failed`。2026-10-02 晚间
#: 事故实录：本机 Clash 隧道中断，qoder/codebuddy/trae 三通道在 50 秒内全灭
#: （trae 直接报 ``Tunnel connection failed: 502``），全模型锁死 5 分钟——而故障
#: 本身约 5 分钟后自愈，冷却时长恰好与中断时长重叠。识别出这类突发后缩成短冷却，
#: 让「网络刚恢复就放行」成为可能。
_BURST_WINDOW_S = 60
_BURST_PROVIDERS = 2
#: 机器级故障的短冷却（秒）。
_BURST_COOLDOWN_S = 30
#: 429（限频/账号额度）的冷却（秒），不累计升级。上游频控多为分钟级滑动窗口，
#: 5 分钟起步、3 次升 1 小时会让多会话并发场景下目标几乎一直不可用；额度真
#: 耗尽时，每分钟一次快速 429 再换档的代价也很小。
_RATE_LIMIT_COOLDOWN_S = 60

#: 「空内容」的软退避阶梯（秒）。空内容**不是账号故障**，是上游对这次请求没
#: 产出语义事件（``finishReason=n/a`` 断流 / ``MAX_TOKENS`` 预算吃光）。账号
#: 本身完全健康——2026-11-21 21:45 实测：同一会话 687k token，21:45:10 成功、
#: 21:45:18 空流，中间只隔 8 秒，两次请求体几乎一样大。
#:
#: 旧行为把它当普通 502 打 5 分钟冷却，等于「一次偶发空流 = 整条候选下线 5
#: 分钟」，这才是用户看到「候选上游全部不可用」的直接原因。改成阶梯：
#:
#: * 第 1、2 次：**不冷却**（立刻可重试，同一会话下一次请求大概率正常）；
#: * 第 3 次起：1s → 2s → 4s → 8s → 16s → 32s → 60s 封顶（按窗口内次数翻倍）。
#:
#: 为什么还要退避而不是完全不冷却：连续空流说明这个 (通道, 模型) 此刻对这类
#: 请求真的不行，无间隔硬撞只会把上游频控和本机连接池一起拖垮；指数退避既让
#: 偶发抖动零代价，又让持续故障快速让位给下一个候选。封顶 60s 保证恢复后
#: 最迟一分钟就重新试探（远短于原来 5 分钟）。
_EMPTY_BACKOFF_STEPS = (0, 0, 1, 2, 4, 8, 16, 32, 60)

#: ``(provider_id, model_id)`` → 解禁时刻（epoch 秒）。
_marks: dict[tuple[str, str], float] = {}
#: ``(provider_id, model_id)`` → 窗口内失败时间戳列表。
_hits: dict[tuple[str, str], list[float]] = {}
#: ``model_id`` → 窗口内 ``(时间戳, provider_id)`` 列表（机器级突发判定用）。
_burst: dict[str, list[tuple[float, str]]] = {}
_lock = threading.Lock()


def _prune_burst(model_id: str, now: float) -> list[tuple[float, str]]:
    """返回突发窗口内的 ``(时间戳, provider_id)``（顺带丢弃过期的）。调用方须持有 ``_lock``。"""
    entries = [e for e in _burst.get(model_id, []) if now - e[0] < _BURST_WINDOW_S]
    if entries:
        _burst[model_id] = entries
    else:
        _burst.pop(model_id, None)
    return entries


def _shorten_to_burst_locked(model_id: str, provider_ids: set[str], now: float) -> None:
    """把窗口内其它通道对该模型的长冷却缩到突发短档，并清掉它们的升级计数。

    突发判定的不对称问题：第一个失败的目标落标记时还没有第二通道的证据，只能先按
    普通故障记；等第二个通道也失败、机器级判定成立时，回头把「同样是机器级故障
    受害者」的它一起缩掉——否则最先失败的目标反而被锁得最久。升级计数一并清空：
    机器级失败不是这个上游的错，不该累计到「反复失败升级 1 小时」上。

    调用方须持有 ``_lock``。
    """
    for pid in provider_ids:
        k = (pid, model_id)
        if _marks.get(k, 0.0) > now + _BURST_COOLDOWN_S:
            _marks[k] = now + _BURST_COOLDOWN_S
        _hits.pop(k, None)


def _prune_hits(key: tuple[str, str], now: float) -> list[float]:
    """返回窗口内的失败时间戳（顺带丢弃过期的）。调用方须持有 ``_lock``。"""
    hits = [t for t in _hits.get(key, []) if now - t < _ESCALATE_WINDOW_S]
    if hits:
        _hits[key] = hits
    else:
        # 别留空列表：没有窗口内失败时把键删掉，dict 保持有界（否则每个失败过的目标
        # 都留一条空记录，虽然键空间受 model_order 限制、不会真无界，但没必要留着）
        _hits.pop(key, None)
    return hits


def _drop_expired(now: float) -> None:
    """惰性回收：``_marks`` 已过期的条目连同其升级计数一并删除。

    两条路径必须**一致**：显式 ``clear()`` 本来就同时清 ``_marks`` 与 ``_hits``，
    而自然到期若只清 ``_marks``，就会出现「同样的好了又坏，走 UI 清冷却与等它自己过期
    得到的升级计数起点不同」的不一致。这里让到期等价于「这个目标恢复了」，计数归零。

    调用方须持有 ``_lock``。
    """
    for key in [k for k, until in _marks.items() if until <= now]:
        _marks.pop(key, None)
        _hits.pop(key, None)


def mark_failed(provider_id: str, model_id: str, *, status: int | None = None) -> float:
    """标记一次失败，返回解禁时刻（epoch 秒）。

    两种时长，按失败形态分流：

    - **机器级突发**：突发窗口内已有 ``_BURST_PROVIDERS`` 个**不同通道**对同一模型
      发生**网络类失败**（传输层异常，或 ≥500——隧道层 502 这类；429 配额/限流
      **不算**，多通道同时限流是上游/账号自身的状态，缩成短冷却等于帮它解除武装）
      → 多半是本机网络/代理在抖，不是哪个上游的毛病。
      本次与窗口内已标记的通道一律只冷却 ``_BURST_COOLDOWN_S``，且不累计升级
      ——网络秒级自愈后立即放行，不再重演「故障早好了、冷却还在锁」。
      各通道的隧道层失败与上游自身 502 无法逐条区分（响应体都是从隧道里回来的），
      所以用「多通道同时失败」这个统计特征判定，接受少量误判（两个上游真同时坏，
      也只是冷却转 30 秒轮换，换档仍正常工作）。
    - **限频 429**：固定 ``_RATE_LIMIT_COOLDOWN_S``，不进突发窗口、不计升级——
      频控是分钟级滑动窗口，长冷却只会让并发场景下目标长期不可用。
    - **普通失败**：短窗口内累计达到 ``_ESCALATE_HITS`` 次升级为
      ``_ESCALATE_COOLDOWN_S``，否则 ``_TARGET_COOLDOWN_S``。始终取 ``max``，
      短冷却不得缩短已有的长冷却（机器级路径例外——见上，突发缩档是**有意**回写）。

    只记 provider / model / status，**绝不记录上游响应体**（隐私红线，见
    docs/architecture.md）。
    """
    key = (provider_id, model_id)
    now = time.time()
    with _lock:
        if status is None or status >= 500:  # 网络类失败才进突发窗口（见上）
            burst = _prune_burst(model_id, now)
            burst.append((now, provider_id))
            _burst[model_id] = burst
            window_providers = {p for _, p in burst}
            machine = len(window_providers) >= _BURST_PROVIDERS
        else:
            window_providers = set()
            machine = False
        if status == 429:
            span = _RATE_LIMIT_COOLDOWN_S
            escalated = False
            _marks[key] = max(_marks.get(key, 0.0), now + span)
        elif machine:
            span = _BURST_COOLDOWN_S
            escalated = False
            # 本次走机器级短档，不累计升级计数；窗口内其它通道回头一起缩（见上）。
            _hits.pop(key, None)
            _shorten_to_burst_locked(model_id, window_providers - {provider_id}, now)
            _marks[key] = now + span
        else:
            # 注意：_prune_hits 返回的是**新列表**（且空时会删键），append 后必须写回 dict，
            # 否则本次失败不会被计入，升级永远触发不了。
            hits = _prune_hits(key, now)
            hits.append(now)
            _hits[key] = hits
            escalated = len(hits) >= _ESCALATE_HITS
            span = _ESCALATE_COOLDOWN_S if escalated else _TARGET_COOLDOWN_S
            until = max(_marks.get(key, 0.0), now + span)
            _marks[key] = until
    if machine:
        log.warning(
            "目标 %s/%s 换档失败，疑似机器级故障（%d 秒内 %d 个通道同时失败），"
            "冷却 %d 秒（触发状态=%s）",
            provider_id, model_id, _BURST_WINDOW_S, len(window_providers),
            span, status if status is not None else "unknown",
        )
    elif status == 429:
        log.warning("目标 %s/%s 换档失败（限频/额度 429），冷却 %d 秒（不升级）",
                    provider_id, model_id, span)
    else:
        log.warning(
            "目标 %s/%s 换档失败，冷却 %d 分钟%s（触发状态=%s，窗口内第%d次）",
            provider_id, model_id, span // 60,
            "（已升级）" if escalated else "（首次，未升级）",
            status if status is not None else "unknown", len(hits),
        )
    return _marks[key]


def mark_empty_content(provider_id: str, model_id: str) -> float:
    """「上游 200 但没产出语义事件」的**软退避**标记，返回解禁时刻（epoch 秒）。

    与 :func:`mark_failed` 的区别是**不把它当通道故障**：空内容说明上游这次没
    给出真负载，账号/通道本身是好的（2026-11-21 21:45 实测：同会话 687k token，
    8 秒前刚成功、8 秒后空流）。因此：

    * 前两次**不冷却**（``_EMPTY_BACKOFF_STEPS[:2] == (0, 0)``），返回当前时刻，
      下一次请求立刻可以再试同一个目标——偶发抖动对用户零代价；
    * 第 3 次起按窗口内次数取阶梯 ``1→2→4→8→16→32→60`` 秒封顶，让「持续空流」
      快速让位给下一个候选，又不至于像旧逻辑那样锁死 5 分钟；
    * 不写 ``_marks`` 的 0 秒档（等于没标），也不参与机器级突发窗口——空内容
      不是网络层故障，把它算进「多通道同时失败」会误判成机器级并缩短别的目标。

    计数与普通失败**共用** ``_hits`` 窗口（``_ESCALATE_WINDOW_S``），但只在成功
    时由 :func:`note_success` 清零：连续空流才升级，中间成功一次就回到「不冷却」。
    """
    key = (provider_id, model_id)
    now = time.time()
    with _lock:
        hits = _prune_hits(key, now)
        hits.append(now)
        _hits[key] = hits
        idx = min(len(hits) - 1, len(_EMPTY_BACKOFF_STEPS) - 1)
        span = _EMPTY_BACKOFF_STEPS[idx]
        if span <= 0:
            # 前两枪不冷却：清掉可能残留的旧标记（比如上一轮升级出来的长冷却），
            # 否则「不冷却」会被旧标记推翻，用户照样看到 cooldown skip。
            _marks.pop(key, None)
            until = now
        else:
            until = max(_marks.get(key, 0.0), now + span)
            _marks[key] = until
        count = len(hits)
    if span <= 0:
        log.warning(
            "目标 %s/%s 上游空内容（第%d次，未冷却，立即可重试）",
            provider_id, model_id, count,
        )
    else:
        log.warning(
            "目标 %s/%s 上游连续空内容（窗口内第%d次），软退避 %d 秒",
            provider_id, model_id, count, span,
        )
    return until


def note_success(provider_id: str, model_id: str) -> None:
    """一次**真负载**成功后调用：清空该目标的失败计数与冷却标记。

    成功就是「这个目标现在能用」的最强证据，因此连冷却标记一起清掉：
    * 计数不清的话，「上午失败两次、下午再失败一次」会被算成连续三次而进入
      退避档，把早已恢复的目标又冷却几秒；
    * 标记不清的话，第 3 次空内容留下的 1~60 秒退避会继续挡着——而这个目标
      刚刚明明成功了。既然成功已经证明它健康，没有理由再等。
    """
    key = (provider_id, model_id)
    with _lock:
        _hits.pop(key, None)
        _marks.pop(key, None)


def is_marked(provider_id: str, model_id: str) -> bool:
    """该目标当前是否处于冷却中（过期条目顺带清掉，保持 dict 有界）。"""
    return remaining(provider_id, model_id) > 0


def remaining(provider_id: str, model_id: str) -> int:
    """剩余冷却秒数；未标记或已过期返回 0。"""
    key = (provider_id, model_id)
    now = time.time()
    with _lock:
        if _marks.get(key, 0.0) <= now:
            _drop_expired(now)
            return 0
        return max(0, int(round(_marks[key] - now)))


def clear(provider_id: str | None = None, model_id: str | None = None) -> int:
    """清除标记，返回被清除的目标数。

    - 两者都给 → 只清该目标（含其升级计数）；
    - 只给 ``provider_id`` → 清该通道下所有目标（含只残留计数的键）；
    - 都不给 → 全清（供 UI「全部重试」与测试隔离用）。

    清标记**一律连带清升级计数**与突发窗口里的记录（否则清完冷却，下一枪失败还会
    被窗口里残留的旧失败顶成「机器级」），与自然到期（:func:`_drop_expired`）保持
    同语义：两者都表示「这个目标恢复了」。
    """
    with _lock:
        if provider_id is None:
            removed = len(_marks)
            _marks.clear()
            _hits.clear()
            _burst.clear()
            return removed
        if model_id is not None:
            removed = 1 if _marks.pop((provider_id, model_id), None) is not None else 0
            _hits.pop((provider_id, model_id), None)
            _burst[model_id] = [e for e in _burst.get(model_id, [])
                                if e[1] != provider_id]
            return removed
        # 同时扫 _hits：该通道下可能只留有升级计数、标记已自然过期（那些键不在 _marks 里）
        keys = {k for k in _marks if k[0] == provider_id}
        keys |= {k for k in _hits if k[0] == provider_id}
        for k in keys:
            _marks.pop(k, None)
            _hits.pop(k, None)
        for mid in list(_burst):
            _burst[mid] = [e for e in _burst[mid] if e[1] != provider_id]
        return len(keys)


def snapshot() -> dict[str, int]:
    """当前所有生效中的标记：``"provider/model"`` → 剩余秒数（供 /ui 展示）。"""
    now = time.time()
    out: dict[str, int] = {}
    with _lock:
        _drop_expired(now)
        for (provider_id, model_id), until in _marks.items():
            out[f"{provider_id}/{model_id}"] = max(0, int(round(until - now)))
    return out


def _reset_for_tests() -> None:
    """清空全部模块级状态。

    标记是**模块级单例**（刻意不挂在 ProxyState 上，免得四个 SimpleNamespace 假 state
    都要跟着扩），因此打路由的测试之间必须显式清空——同
    ``test_trae_pat_failover`` 里 monkeypatch ``_channel_exhausted`` 的做法。
    """
    clear()
