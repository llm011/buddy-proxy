"""按剩余额度加权的账号轮转（provider 维度可选开启）。

默认行为不变：多账号通道永远从顺位第一开始（稳定主备）。用户在额度管理里
打开某通道的「负载均衡」开关后，转发改为在可用账号间按**剩余额度**加权随机
挑选，让额度多的账号被更多次选中、几份额度按比例一起消耗。

设计要点
--------

**只动顺序，不动过滤**。冷却、region 过滤、单模型限流这些"该不该试这个账号"
的判断仍由各通道的 ``failover.available_accounts`` 负责；本模块只负责把已经
可用的账号列表重排。因此开关关掉时逐字节回到旧行为。

**权重来自剩余额度，拿不到就退回等权**。额度快照是异步、带缓存、可能失败的
（见 ``benefits._cached``）。任何账号没有可用读数时那一份记为 0 权重；**全部**
都没有读数时退回均匀随机——「不知道额度」不该导致永远只用一个账号。

**加权随机而非严格比例**。严格按比例切分会把同一秒里的连续请求钉在同一个
账号上（额度大的那个），而缓存命中的收益在于"同一段前缀被反复用到"，两种
做法都不影响缓存。这里选加权随机是因为它天然带抖动：即使额度估算有偏差，
实际消耗也会向"剩余多的账号"收敛，而不是在额度读数边界上反复横跳。

**权重下限**。剩余额度为 0 的账号不会被排到前面，但仍保留一个很小的非零
权重（``MIN_WEIGHT``）：它们通常只是"这轮查询没取到数"，等真正耗尽时上游会
返回 429/14018，由既有的冷却逻辑接管。完全剔除会让"额度全是 0 的账号"永久
不可用，而 0 有时只是查询失败。

**同一次请求内不重复**。返回的是完整顺位列表（而不是只挑一个）：failover
循环仍要按序试后面的账号，所以这里做的是一次加权抽样的**无放回排列**。

**缓存隔离是按模型分的，不是按通道**（2026-11-11 实测）。开关虽然挂在通道上，
但同一通道内不同模型对「换账号还认不认前缀缓存」的表现完全不同：codebuddy 的
``glm-5.3-flash`` / ``kimi-k3.1-pro`` 换号后仍 100% 命中（缓存按内容前缀、
跨账号共享），而 ``deepseek-v4.1-flash`` 换号后 0 命中（按账号隔离）。对后者
开均衡等于**每个请求都白丢一次缓存**，成本可能翻倍。

所以 ``reorder`` 收一个 ``model``：命中「不共享缓存」名单的模型仍走原顺位。
名单见 :data:`ACCOUNT_SCOPED_MODELS` 与 :func:`is_account_scoped`。
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Iterable, Sequence

#: 权重下限：额度为 0 / 未知的账号仍保留一点被选中的机会。
#: 取 1.0 是相对"积分"这个量纲的经验值——剩余 300 积分与"未知"的差距不该是
#: 无穷大，否则一次额度查询失败就把账号永久踢出轮转。
MIN_WEIGHT = 1.0

#: 额度读数的新鲜度上限（秒）。比 benefits 的 SNAPSHOT_TTL_S（5 分钟）宽一点：
#: 慢一拍的额度分布仍然有参考价值，而过期读数只会让权重略有偏差，不值得为此
#: 在转发路径上同步查上游。
MAX_QUOTA_AGE_S = 900.0


class QuotaBoard:
    """进程内的「账号 → 剩余额度」读数表。

    转发路径**绝不**同步查上游（那会把 p99 拖到秒级，且每个请求都要多打一次
    计费接口）。额度由别处异步写入：

    - provider 的额度查询（``quota()``）顺手 ``update()`` 一轮，顺带覆盖面板
      上用户点「刷新」的情形；
    - ``set_zero()`` 是预留的:转发侧目前**没有**调用它。额度耗尽的账号由各通道
      既有的冷却过滤挡在 ``reorder`` 之前(见模块文档「权重下限」一节)。

    读数带时间戳；``weight_of`` 超过 ``MAX_QUOTA_AGE_S`` 视为未知。
    """

    def __init__(self) -> None:
        self._values: dict[str, tuple[float, float]] = {}  # id -> (remaining, ts)

    def update(self, account_id: str, remaining: float | None) -> None:
        """写入一条读数。``None``/非数视为未知（直接丢弃旧值，避免用陈旧数字）。"""
        if remaining is None:
            self._values.pop(account_id, None)
            return
        try:
            value = float(remaining)
        except (TypeError, ValueError):
            self._values.pop(account_id, None)
            return
        if value != value:  # NaN
            self._values.pop(account_id, None)
            return
        self._values[account_id] = (value, time.time())

    def update_many(self, mapping: dict[str, Any]) -> None:
        for account_id, remaining in (mapping or {}).items():
            self.update(str(account_id), remaining)

    def set_zero(self, account_id: str) -> None:
        """把某账号的剩余额度直接记 0（预留:当前转发侧未调用）。"""
        self._values[str(account_id)] = (0.0, time.time())

    def weight_of(self, account_id: str, now: float | None = None) -> float:
        """该账号的抽样权重（≥ ``MIN_WEIGHT``）。未知/过期/负数都只拿到下限。"""
        entry = self._values.get(str(account_id))
        if not entry:
            return MIN_WEIGHT
        remaining, ts = entry
        if (now if now is not None else time.time()) - ts > MAX_QUOTA_AGE_S:
            return MIN_WEIGHT
        if remaining != remaining or remaining <= 0:  # NaN / 已耗尽
            return MIN_WEIGHT
        return max(float(remaining), MIN_WEIGHT)

    def known(self, account_id: str, now: float | None = None) -> bool:
        """是否有**新鲜且非负**的读数（未知与"真的用完"要分开，供 UI 展示）。"""
        entry = self._values.get(str(account_id))
        if not entry:
            return False
        remaining, ts = entry
        if (now if now is not None else time.time()) - ts > MAX_QUOTA_AGE_S:
            return False
        return remaining == remaining and remaining >= 0

    def remaining_of(self, account_id: str, now: float | None = None) -> float | None:
        """读数原值（未知返回 None；0 与"查询失败"区分开）。"""
        if not self.known(account_id, now):
            return None
        return self._values[str(account_id)][0]

    def clear(self) -> None:
        """清空全部读数（测试隔离用）。"""
        self._values.clear()


def weighted_order(
    accounts: Sequence[Any],
    weight_of: Callable[[Any], float],
    *,
    rng: random.Random | None = None,
) -> list[Any]:
    """把账号列表按权重做一次无放回加权抽样，返回新的顺位。

    ``accounts`` 原样不修改。权重 ≤0 / 非数一律按 ``MIN_WEIGHT`` 处理，保证
    每个账号都有被选中的机会（否则"额度全 0"会把通道打死）。

    实现是 Efraimidis-Spirakis：对每个账号取 ``key = u ** (1/w)`` 后按 key
    降序排列（``u`` 为 (0,1] 均匀分布）。一次排序即得无放回抽样，且每个账号
    排到第 k 位的概率恰好正比于其权重——比"每次都按权重挑一个再从列表移除"
    的 O(n²) 写法更简单，也更容易保持确定性测试。
    """
    pool = list(accounts)
    if not pool:
        return []
    rnd = rng or random
    keyed: list[tuple[float, int, Any]] = []
    for i, acct in enumerate(pool):
        try:
            raw = float(weight_of(acct))
        except (TypeError, ValueError):
            raw = MIN_WEIGHT
        if raw != raw or raw <= 0:  # NaN / 非正
            raw = MIN_WEIGHT
        # u ∈ (0,1]：log(u) 恒负，除以权重后 u**(1/w) 落在 (0,1]。
        u = rnd.random()
        if u <= 0.0:
            u = 1e-12
        try:
            key = u ** (1.0 / raw)
        except (OverflowError, ValueError):
            key = 0.0
        keyed.append((key, i, acct))
    # 降序；key 完全相同时用原始下标兜底，保证结果可复现（测试与日志依赖）
    keyed.sort(key=lambda item: (-item[0], item[1]))
    return [acct for _key, _i, acct in keyed]


def equal_weights(accounts: Iterable[Any], _acct: Any = None) -> list[Any]:
    """均匀随机（额度读数全无时的退路）。"""
    pool = list(accounts)
    random.shuffle(pool)
    return pool


# ---------------------------------------------------------------------------
# provider 维度：读数表实例 + 开关 + 转发侧的统一入口
# ---------------------------------------------------------------------------
#
# 上面的 QuotaBoard/weighted_order 是通道无关的零件；这一段把它们接到
# ProxyState 上，让**每个多账号通道**用同一套开关与权重口径，而不是各自
# 复制一份 ``_QUOTA_BOARD`` / ``_load_balance_on``。

#: 各通道的读数表（键 = provider id）。进程内、不落盘；额度是上游的实时状态，
#: 重启后第一轮额度查询(面板或后台预热)就会把它填回来。
_BOARDS: dict[str, QuotaBoard] = {}


def board(provider_id: str) -> QuotaBoard:
    """取（必要时创建）某通道的读数表。"""
    key = str(provider_id or "")
    got = _BOARDS.get(key)
    if got is None:
        got = _BOARDS[key] = QuotaBoard()
    return got


def clear_boards() -> None:
    """清空所有通道读数（测试隔离用）。"""
    for b in _BOARDS.values():
        b.clear()


def enabled(state: Any, provider_id: str) -> bool:
    """该通道是否开启了「按剩余额度加权轮转」。

    读运行态而不是翻 settings.json：管理页开关是即时生效的，且转发是热路径
    （每请求一次），文件 I/O 不能放在这里。state 缺失（离线/测试直连）时
    返回 False，行为与历史一致。
    """
    try:
        on = getattr(state, "load_balance", None)
    except Exception:  # noqa: BLE001 - 无运行态：当没开
        return False
    return provider_id in (on or ())


def reorder(state: Any, provider_id: str, accounts: Sequence[Any],
            model: str | None = None) -> list[Any]:
    """按开关决定是否重排账号顺序；返回**新列表**（不修改入参）。

    - 开关关着、或候选不足两个：原样返回列表副本——调用方拿到的东西与历史
      行为逐字节一致，不必在两处写 ``if``。
    - 模型属于「缓存按账号隔离」那一类（见 :func:`is_account_scoped`）：
      同样原样返回。**这是本函数最重要的分支**：对这些模型轮转没好处，
      纯亏缓存。
    - 其余情况：按该通道读数表的权重做一次无放回加权抽样。

    过滤（冷却 / region / 单模型限流 / per-account 模型支持）**必须**在调用
    本函数之前完成：这里只决定「先试谁」，不决定「谁能试」。

    ``model`` 缺省（None）时按「未知」处理——**不做**隔离检查，照常均衡。
    理由：老调用方（或未来新通道忘了传）应当保持"开关打开就生效"的行为，
    而不是静默失效；宁可偶尔掉缓存，也不能让功能看起来失灵。
    """
    pool = list(accounts or ())
    if len(pool) < 2 or not enabled(state, provider_id):
        return pool
    if model and is_account_scoped(provider_id, model):
        return pool
    qb = board(provider_id)
    return weighted_order(pool, lambda a: qb.weight_of(str(getattr(a, "id", "") or "")))


def weights_from_items(provider_id: str, per_account: dict[str, Any]) -> None:
    """把「账号 → 剩余额度」写进该通道的读数表（额度查询/预热调用）。

    ``per_account`` 的值为 ``None`` 表示该账号本轮没取到数——按「未知」处理，
    不能当成 0（见 QuotaBoard.update 的语义）。
    """
    board(provider_id).update_many(per_account or {})


#: 额度条目标签里的账号前缀：「ZCode #2 · 月额度」「AG #1 · Gemini 组」「Kiro #3 · PRO」。
#: 通道名里可能带空格与中文（「CodeBuddy 海外版 #1」），所以不写死字符集——
#: 取「 · 」之前那段，再从中抽出末尾的 ``#N``。
_ACCT_PREFIX_RE = __import__("re").compile(r"#(\d+)$")


def remaining_by_account(items: Any) -> dict[str, float]:
    """从额度条目反推「账号 → 剩余」——用**展示序号**（#N）当键。

    各通道的 ``quota()`` 都把这些条目按账号分组下发（label 形如 ``X #N · …``），
    但条目里**没有账号 id**：序号是额度面板与账号快照之间唯一的对位口径
    （见各 failover 的 ``display_index``）。所以这里只能按序号聚合，再由调用方
    把序号映射回 id。

    口径与 :func:`buddy_proxy.web.static.quotaSumOf` 的 ``sum_items`` 分支一致：
    「有 remaining 且能当数字用」的条目相加，跳过 ``head_only``/说明条/量纲不可
    比的行——否则一个「重置券 1 张」会被算成 1 点额度，把权重带偏。

    单账号通道的 label 没有 ``#N`` 前缀，返回 ``{"": 剩余}``：调用方按「只有一个
    账号」处理即可。
    """
    totals: dict[str, float] = {}
    for it in items or ():
        if not isinstance(it, dict):
            continue
        if it.get("query_failed") or it.get("unreachable"):
            continue
        label = str(it.get("label") or "")
        group, sep, _rest = label.partition(" · ")
        m = _ACCT_PREFIX_RE.search(group) if sep else None
        key = m.group(1) if m else ""
        raw = it.get("remaining")
        if raw is None or isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value != value:  # NaN
            continue
        totals[key] = totals.get(key, 0.0) + value
    return totals


# ---------------------------------------------------------------------------
# 缓存隔离名单：哪些模型的缓存是「按账号」的
# ---------------------------------------------------------------------------
#
# 开关是通道维度的，缓存隔离却是**模型**维度的。这个名单回答：「开了均衡之后，
# 这个模型换账号还能不能吃到上一轮的缓存？」
#
# 名单只放**实测确认按账号隔离**的模型。没列到的默认「共享」——这是刻意的
# 保守方向：默认共享 → 均衡照常工作（功能可用）；默认隔离 → 用户开了开关却
# 发现只有一半模型在均衡，会以为坏了。而共享是更常见的情形（实测 3/4 共享）。
#
# 数据来源（2026-11-11，大 prompt 增长会话 + 从未见过的第三个账号）：
#   glm-5.3-flash       换号 100.0% 命中  → 共享
#   kimi-k3.1-pro       换号  99.9% 命中  → 共享
#   qwen3.8-max         换号 100.0% 命中  → 共享
#   deepseek-v4.1-flash 换号   0.0% 命中  → 隔离 ★
#
# 注意这些是**实测当时**的结论，上游随时可能改。所以除了这张表，还有
# :func:`observe_swap` 从真实流量里学：日志里换号前后的 prefix_reuse 能直接
# 证伪/证实。观测结果优先于静态表（见 :func:`is_account_scoped`）。
ACCOUNT_SCOPED_MODELS: frozenset[tuple[str, str]] = frozenset({
    ("codebuddy", "deepseek-v4.1-flash"),
})

#: 观测样本的最小条数。少于这个数不采信——一两次偶发的 0 命中可能是上游抖动、
#: 也可能是模型刚滚动（缓存还没写热），不足以推翻/建立结论。
OBSERVE_MIN_SAMPLES = 8

#: 判定「共享」的复用率门槛。换号后复用率中位数 >= 这个值就认为跨账号共享。
#: 取 0.5 而不是 0.9：上游按 64k 块命中（实测），部分命中很常见，
#: 0.5 已经能明确区分「共享（通常 >0.9）」与「隔离（通常 =0）」两座峰。
OBSERVE_SHARED_THRESHOLD = 0.5

#: (provider, model) -> [换号后的 prefix_reuse 样本]
_SWAP_SAMPLES: dict[tuple[str, str], list[float]] = {}


def record_swap_reuse(provider_id: str, model: str, reuse: float) -> None:
    """登记一次「换号后的前缀复用率」观测（转发层在检测到账号变化时调用）。

    只保留最近 ``OBSERVE_MIN_SAMPLES * 4`` 条：上游行为会变，样本太老会一直
    拽着结论不放。超出后按 FIFO 丢弃。
    """
    key = (str(provider_id or ""), str(model or ""))
    try:
        value = float(reuse)
    except (TypeError, ValueError):
        return
    if value != value or value < 0:  # NaN / 负数
        return
    bucket = _SWAP_SAMPLES.setdefault(key, [])
    bucket.append(min(value, 1.0))
    limit = OBSERVE_MIN_SAMPLES * 4
    if len(bucket) > limit:
        del bucket[:len(bucket) - limit]


def observed_verdict(provider_id: str, model: str) -> bool | None:
    """根据真实观测判断该模型是否「缓存按账号隔离」。

    返回 ``True``（隔离）/ ``False``（共享）/ ``None``（样本不足，无从判断）。
    中位数而不是均值：复用率是双峰的（≈1.0 与 ≈0.0），均值会给出 0.5 这种
    两边都不属于的假象（日志指标那边踩过同一个坑）。
    """
    vals = _SWAP_SAMPLES.get((str(provider_id or ""), str(model or "")))
    if not vals or len(vals) < OBSERVE_MIN_SAMPLES:
        return None
    ordered = sorted(vals)
    mid = ordered[len(ordered) // 2]
    return mid < OBSERVE_SHARED_THRESHOLD


def is_account_scoped(provider_id: str, model: str) -> bool:
    """该 (通道, 模型) 的缓存是否按账号隔离（= 不该参与账号轮转）。

    观测优先于静态表，但只在样本够的时候覆盖：静态表是「上次人工实测的结论」，
    观测是「线上真实流量」。上游改了行为时，观测会在几十个请求内纠正它，
    不需要发版。
    """
    seen = observed_verdict(provider_id, model)
    if seen is not None:
        return seen
    return (str(provider_id or ""), str(model or "")) in ACCOUNT_SCOPED_MODELS


def account_scoped_models(provider_id: str) -> list[str]:
    """该通道当前判定为「缓存按账号隔离」的模型清单（UI 提示用）。"""
    pid = str(provider_id or "")
    out = {m for p, m in ACCOUNT_SCOPED_MODELS if p == pid}
    for (p, m), vals in _SWAP_SAMPLES.items():
        if p != pid:
            continue
        verdict = observed_verdict(p, m)
        if verdict is True:
            out.add(m)
        elif verdict is False:
            out.discard(m)
    return sorted(out)


def clear_swap_samples() -> None:
    """清空观测样本（测试隔离用）。"""
    _SWAP_SAMPLES.clear()
