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

import hashlib
import json
import random
import re
import threading
import time
from contextvars import ContextVar, Token
from typing import Any, Callable, Iterable, Mapping, Sequence

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


def session_rotation_enabled(state: Any, provider_id: str) -> bool:
    """该通道是否开启了「按会话在账号间轮转」。

    与 :func:`enabled` 一样直接读运行态；state 缺失时返回 False。
    """
    try:
        on = getattr(state, "session_rotation", None)
    except Exception:  # noqa: BLE001 - 无运行态：当没开
        return False
    return provider_id in (on or ())


def is_account_disabled(state: Any, provider_id: str, account_id: str) -> bool:
    """该账号是否被用户在管理页手动「临时下线」。

    键口径 ``provider_id:account_id``（与 settings.disabled_accounts 一致）。
    state 缺失/无禁用表时返回 False——离线/测试直连行为与历史一致。
    """
    try:
        disabled = getattr(state, "disabled_accounts", None)
    except Exception:  # noqa: BLE001 - 无运行态：当没禁用
        return False
    if not disabled:
        return False
    return f"{provider_id}:{account_id}" in disabled


def _filter_disabled(state: Any, provider_id: str, pool: list[Any]) -> list[Any]:
    """剔除被手动禁用的账号。无禁用表时原样返回（不制造无谓拷贝）。

    这是**用户政策开关**（同 load_balance/session_rotation），不是健康过滤：
    无论均衡/轮转开关开没开，被禁用的账号一律不参与转发。放在 reorder 入口
    统一处理，所有调用 reorder 的通道（含 kiro 直取 creds.list_accounts 的路径）
    自动生效，不必逐通道改 failover。
    """
    try:
        disabled = getattr(state, "disabled_accounts", None)
    except Exception:  # noqa: BLE001 - 无运行态：不过滤
        return pool
    if not disabled:
        return pool
    return [a for a in pool
            if f"{provider_id}:{getattr(a, 'id', '') or ''}" not in disabled]


def reorder(state: Any, provider_id: str, accounts: Sequence[Any],
            model: str | None = None, *, session_key: str | None = None) -> list[Any]:
    """按开关决定是否重排账号顺序；返回**新列表**（不修改入参）。

    入口先剔除被用户手动禁用的账号（``state.disabled_accounts``，政策开关，
    与均衡/轮转无关，始终生效）。剩余账号再按两条可选策略排序（开关互斥；
    同时出现时会话轮转优先）：
    1. **会话轮转**（``state.session_rotation``）：仅当请求带有显式会话标识
       （``session_key`` / ContextVar ``SESSION_KEY``）时生效——新会话按可用账号
       顺位轮转分配，同一会话在有效期内粘在同一个账号上。因为同一会话不中途
       换号，缓存按账号隔离的模型也照常参与轮转；未检测到会话标识时保持原顺位
       不变。
    2. **负载均衡**（``state.load_balance``）：按剩余额度加权随机抽样。命中
       「缓存按账号隔离」名单的模型仍保持原顺位，避免每次请求白丢缓存。

    健康/能力过滤（冷却 / region / 单模型限流 / per-account 模型支持）**必须**在
    调用本函数之前完成：那些决定「谁能试」且影响权重计算。手动禁用是用户政策
    开关（同均衡/轮转），故在本函数入口统一剔除，不属于上述健康过滤。
    """
    pool = _filter_disabled(state, provider_id, list(accounts or ()))
    if len(pool) < 2:
        return pool
    if session_rotation_enabled(state, provider_id):
        sid = current_session_key() if session_key is None else _normalize_session_id(session_key)
        if not sid:
            return pool
        return _SESSION_ROUTER.order(provider_id, sid, pool)
    if not enabled(state, provider_id):
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


# ---------------------------------------------------------------------------
# 会话轮转：显式会话标识提取 + 粘性顺位轮转
# ---------------------------------------------------------------------------

#: 当前请求的显式会话标识（由 web/routes.py 在请求入口设置，finally 还原）。
#: 用 ContextVar 避免改动 8 个 provider.forward 的函数签名。
SESSION_KEY: ContextVar[str] = ContextVar("buddy_session_key", default="")

#: 显式会话标识请求头，按优先级从高到低匹配（大小写不敏感）。
_SESSION_HEADERS = (
    "x-session-id",
    "session-id",
    "x-conversation-id",
    "conversation-id",
)

#: Claude Code 等 Anthropic 客户端常把会话 id 编在 metadata.user_id 里：
#: ``user_<hash>_account_<uuid>_session_<uuid>``。
_METADATA_SESSION_RE = re.compile(r"(?:^|_)session_([A-Za-z0-9._:-]+)$")

#: 会话 → 账号粘性映射的空闲过期时间（秒）与容量上限。
SESSION_TTL_S = 6 * 3600.0
MAX_SESSIONS = 4096


def _normalize_session_id(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    val = raw.strip()
    if not val:
        return ""
    if len(val) > 256:
        return "sha256:" + hashlib.sha256(val.encode("utf-8", errors="replace")).hexdigest()[:32]
    return val


def _extract_metadata_session(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    uid = metadata.get("user_id")
    if not isinstance(uid, str):
        return ""
    text = uid.strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                sid = _normalize_session_id(parsed.get("session_id") or parsed.get("sessionId"))
                if sid:
                    return sid
        except (ValueError, TypeError):
            pass
    m = _METADATA_SESSION_RE.search(text)
    if m:
        return _normalize_session_id(m.group(1))
    return ""


def extract_session_key(
    headers: Mapping[str, str] | None,
    body: Any = None,
) -> str:
    """从请求头与请求体里提取显式会话标识；未检测到返回空串。

    只认第一级显式标识（请求头 ``x-session-id`` / ``session-id`` 等、请求体
    ``prompt_cache_key`` / ``session_id`` / ``conversation_id`` / ``metadata.user_id``
    中的 session 段）。不做对话内容指纹猜测，确保未带会话标识的客户端行为与历史
    完全一致。
    """
    if headers:
        lowered = {str(k).lower(): v for k, v in headers.items() if isinstance(v, str)}
        for name in _SESSION_HEADERS:
            sid = _normalize_session_id(lowered.get(name))
            if sid:
                return sid
    if isinstance(body, dict):
        for field in ("prompt_cache_key", "session_id", "conversation_id"):
            sid = _normalize_session_id(body.get(field))
            if sid:
                return sid
        sid = _extract_metadata_session(body.get("metadata"))
        if sid:
            return sid
    return ""


def current_session_key() -> str:
    return SESSION_KEY.get()


def set_session_key(key: str) -> Token[str]:
    return SESSION_KEY.set(_normalize_session_id(key))


def reset_session_key(token: Token[str]) -> None:
    SESSION_KEY.reset(token)


class SessionRouter:
    """按通道维护「会话 → 账号」粘性绑定与轮转游标（线程安全）。

    - 新会话按可用账号列表做严格轮转（第 1 个会话先试账号 1，第 2 个会话先试账号 2…）；
    - 同一会话在 TTL 内再次请求时固定把原绑定账号排在首位（保持前缀缓存命中，
      同时保留其余账号在后面作 failover 兜底）；
    - 原绑定账号因冷却/停用不在当前候选池时，顺位轮转到下一个可用账号并更新绑定。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (provider_id, session_key) -> (account_id, last_used_ts)
        self._bindings: dict[tuple[str, str], tuple[str, float]] = {}
        # provider_id -> next round-robin index
        self._cursors: dict[str, int] = {}

    def _prune_locked(self, now: float) -> None:
        expired = [
            k for k, (_acct, ts) in self._bindings.items()
            if (now - ts) > SESSION_TTL_S
        ]
        for k in expired:
            del self._bindings[k]
        if len(self._bindings) >= MAX_SESSIONS:
            # 超过容量上限时淘汰最久未活跃的会话，避免内存无界增长
            excess = len(self._bindings) - MAX_SESSIONS + 1
            oldest = sorted(self._bindings.items(), key=lambda kv: kv[1][1])[:excess]
            for k, _ in oldest:
                self._bindings.pop(k, None)

    def order(
        self,
        provider_id: str,
        session_key: str,
        accounts: Sequence[Any],
        *,
        now: float | None = None,
    ) -> list[Any]:
        pool = list(accounts or ())
        if len(pool) < 2 or not session_key:
            return pool
        ts = now if now is not None else time.time()
        pid = str(provider_id or "")
        bkey = (pid, session_key)

        with self._lock:
            entry = self._bindings.get(bkey)
            if entry is not None:
                bound_id, last_ts = entry
                if (ts - last_ts) <= SESSION_TTL_S:
                    for idx, acct in enumerate(pool):
                        if str(getattr(acct, "id", "") or "") == bound_id:
                            self._bindings[bkey] = (bound_id, ts)
                            return pool[idx:] + pool[:idx]
                else:
                    self._bindings.pop(bkey, None)

            self._prune_locked(ts)
            cur = self._cursors.get(pid, 0)
            idx = cur % len(pool)
            self._cursors[pid] = cur + 1
            chosen_id = str(getattr(pool[idx], "id", "") or "")
            if chosen_id:
                self._bindings[bkey] = (chosen_id, ts)
            return pool[idx:] + pool[:idx]

    def clear(self) -> None:
        with self._lock:
            self._bindings.clear()
            self._cursors.clear()


_SESSION_ROUTER = SessionRouter()


def session_router() -> SessionRouter:
    return _SESSION_ROUTER


def clear_sessions() -> None:
    """清空所有会话绑定与轮转游标（测试隔离用）。"""
    _SESSION_ROUTER.clear()
