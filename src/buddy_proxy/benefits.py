"""打卡 / 额度：打卡历史落盘 + 自动打卡后台任务（/ui 管理页消费）。

能力约定见 ``providers.BaseProvider``：provider 以 ``supports_checkin = True``
声明支持打卡，``checkin_status()/checkin_claim()/quota()`` 返回 None 表示
不支持对应能力。所有上游调用都是同步的，经 ``asyncio.to_thread`` 执行。

打卡历史逐行落盘 ``logs/checkin.jsonl``（手动 / 自动都走同一条路），管理页
的「打卡日历」由此渲染，自动打卡任务以此判断当天是否已完成。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from buddy_proxy.core import settings as settings_mod

# 自动打卡巡检周期；失败重试间隔
CHECK_INTERVAL_S = 600
RETRY_THROTTLE_S = 1800
# 状态/额度缓存 TTL：管理页 30s 自动刷新，不能每次都打上游
SNAPSHOT_TTL_S = 300
# 失败结果缓存 TTL：短得多，让网络抖动几秒恢复后下一轮就能自愈
FAILURE_TTL_S = 30
# 负载均衡的额度预热周期。取 10 分钟：额度是「积分」量纲、按请求消耗，
# 几分钟的偏差只会让权重略有倾斜，不影响均衡效果；比 SNAPSHOT_TTL_S（5 分钟）
# 长一倍，保证绝大多数轮次都是缓存命中，不额外打上游计费接口。
LOAD_BALANCE_WARM_INTERVAL_S = 600
# 启动后首次预热的延迟：等凭据/网络就绪，也避开「刚启动就打开管理页」那一轮。
LOAD_BALANCE_WARM_DELAY_S = 60
# 单次上游查询的硬超时：provider 实现各有各的 HTTP 超时，但「实现漏配/卡在
# 网络风暴」的病态挂起不该把整个管理页拖住（实测 mimo 单键挂过 930s）。
# 超时按失败处理走 FAILURE_TTL_S 短缓存，30s 后下一轮自然重试。
QUERY_TIMEOUT_S = 35.0
DEFAULT_CHECKIN_TIME = "09:30"
# 「翻转点已过 → 提前作废快照」只在这个宽限窗口内生效。正常轮换最多让缓存
# 早退这么多；超出说明上游给的 next_ts 已经不可信，退回按 TTL 过期。
FLIP_GRACE_S = 3600
# 到期告警：权益剩下的日子少于此值就上横幅；积分类还要剩得比它多才值得提醒
# （用户 2026-10-03 要求「大于 300 积分再提醒」——每天签到送的 200 分小包
# 一到 7 天内就刷屏，真正值钱的会员包反而被淹）。判定见 :func:`_expiring`。
EXPIRY_WARN_DAYS = 7
EXPIRY_WARN_MIN_CREDITS = 300
# 余量耗尽的条目（credit/points）一律不报到期：过期也带不走任何额度，报
# 「剩 0 · 2 天后到期」纯属噪音（2026-11-06 用户反馈 CodeBuddy 三个剩 0 的包
# 因为快到期被顶成临期告警：「太傻瓜了」）。
#
# 阈值不是 0 而是 **0.05**（判 ``remaining < 0.05`` 为空）：界面 ``fmtNum`` 是
# ``Math.round(x * 10) / 10``，不足 0.05 一律显示成「剩 0」。只认 ``<= 0`` 的话，
# ``remaining = 0.001`` 这类尾差（CodeBuddy 的 ``remain = round(total - used, 2)``
# 会给出）后端判定非空照报，用户看到的却仍是「剩 0」，等于没修。判空口径必须
# 对齐展示精度——凡是在界面上读作「剩 0」的，就不该出现在告警里。负数（超支）
# 落在阈值之下，同属带不走东西。
EXPIRY_EMPTY_REMAINING = 0.05
# 余额告急：通道积分类总额度剩得比它少（或剩余占比比它低）就上横幅（用户
# 2026-10-03 要求「不足 300 credits（或不足 8%）」）。与到期告警互补——
# 权益还早但量快烧干的通道，只等到期告警就要在烧干那天才被发现。判定见
# :func:`_quota_low`。
QUOTA_LOW_MIN_CREDITS = 300
QUOTA_LOW_MIN_PERCENT = 8
# 资源包「紧急到期」窗口：进入这个窗口的条目不再受「积分类余量 > 300」的
# 过滤（2026-11-04 用户要求「某个通道里的某个账号中的某个资源包，离到期
# 剩下 4 天也要告警」）。签到送的 200 分小包平时被 300 门槛挡掉是对的，
# 但还剩 4 天就作废时，它值不值得提醒已经不该再由余量决定——过期就彻底
# 没了。外层的 7 天窗口仍由 :data:`EXPIRY_WARN_DAYS` 控制。
EXPIRY_URGENT_DAYS = 4


def _state_flipped(data: Any, now: float) -> bool:
    """缓存里的快照是否「描述的状态已经翻篇」。

    ``checkin_status`` 会给 ``next_ts``——按 ``providers/base.py`` 的契约，它
    正是这个状态**翻转**的时刻（已签到 = 下一轮开始，未签到 = 本轮截止）。
    一旦它成为过去，这份快照描述的就不再是现状：界面刚把倒计时数到「即将
    刷新」，紧接着那次轮询却仍拿到翻篇前的旧状态，于是显示「已签到」+ 按钮
    禁用，要等满 TTL（5 分钟）才自愈。而 qoder 的窗口错过即失效，这 5 分钟
    足够错过一整轮——倒计时承诺的「即将刷新」也就成了空话。

    所以把 ``next_ts`` 本身当作这份缓存的到期时刻：它比任何固定 TTL 都准，
    且天然只在轮换点触发一次重查，不会带来额外轮询。

    ``next_ts`` 缺失或不是数字（额度结果、失败结构、上游给了怪值）时一律
    判否——拿不准就照旧走 TTL，别因为一个坏字段把缓存整个废掉。

    只认「刚过点」的 ``next_ts``：正常路径下越过翻转点后重查一次，上游就会
    给下一个（未来的）时刻，缓存自然恢复。若重查回来的**仍是**很久以前的值
    （活动已停但上游没更新字段之类），那说明这个字段已经不可信，再拿它当
    到期条件就会让缓存永久失效、每次轮询都打上游。故超过 ``FLIP_GRACE_S``
    就退回 TTL——反正 TTL 到了还会再查一次，不会漏掉恢复。
    """
    if not isinstance(data, dict):
        return False
    nxt = data.get("next_ts")
    if isinstance(nxt, bool) or not isinstance(nxt, (int, float)):
        return False
    return nxt <= now and now - nxt <= FLIP_GRACE_S


def _as_num(v: Any) -> float | None:
    """宽松取数：数字照收，数字字符串也收（上游偶发给 ``"500"``）。

    布尔**不收**——``True`` 是 1、``False`` 是 0，混进额度判断会变成
    「剩 1 分」这种假数据。前端 ``quotaHeadSum`` 有两道一模一样的剔除，
    来由相同（见那里的注释）。
    """
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


#: 多账号通道把账号编进 label 前缀：``CodeBuddy #2 · 资源包`` /
#: ``Trae #1 · 会员 Pro 连续包月``。横幅要按「通道 → 账号 → 资源包」两级
#: 展开，所以把这一段拆成结构化字段，别让前端各写一套正则。
_ACCOUNT_LABEL_RE = re.compile(r"^(?P<prefix>.*?)\s*(?P<account>#\d+)\s*·\s*(?P<label>.+)$")


def _split_account_label(label: Any) -> tuple[str, str]:
    """``label`` → ``(account, item_label)``；没有账号前缀时 account 为空串。

    只认行首的 ``#N ·``（账号前缀），不碰名称中间的 ``#``。返回的
    ``item_label`` 已去掉通道名与账号前缀（``CodeBuddy #2 · 资源包`` →
    ``资源包``），方便前端在「账号 #2」旁边只显示包名。
    """
    text = str(label or "").strip()
    if not text:
        return "", str(label or "")
    m = _ACCOUNT_LABEL_RE.match(text)
    if not m:
        return "", str(label or "")
    return m.group("account"), m.group("label").strip()


#: 前端 ``_expiryAmountOf`` 只认这两种量纲（它们才有「能白带走多少额度」的
#: 概念）。后端判断「是不是空壳」用同一份口径，两边别各写一套慢慢走偏。
_EXPIRY_AMOUNT_UNITS = ("credit", "points")


def _remaining_state(item: dict[str, Any]) -> str:
    """条目余量三态：``"empty"`` / ``"some"`` / ``"unknown"``。

    「空不空」只有按量计费的条目才判得出来：credit（积分）和 points（DuMate
    这类积分制）都有「还剩多少」的语义，0 就是真的一分不剩；负数是超支，同样
    带不走东西。其余量纲（day/count/permille）的 ``remaining`` 是「还剩几天」
    这类尺度，0 不代表空壳（千分制剩 0 反而是余额告急那边的事），按
    ``"unknown"`` 放行，维持原来的只看天数的行为。

    余量字段缺失、取不出数、或取到 ``nan`` 都归 ``"unknown"``——「不知道」不
    等于「是 0」。``nan`` 要单独拦：拿它跟阈值比大小恒为假，不拦会滑进
    ``"some"`` 被当成「确定还有额度」报出去。
    """
    if item.get("unit") not in _EXPIRY_AMOUNT_UNITS:
        return "unknown"
    remaining = _as_num(item.get("remaining"))
    if remaining is None or remaining != remaining:  # x != x 即 nan
        return "unknown"
    return "empty" if remaining < EXPIRY_EMPTY_REMAINING else "some"


def _expiring(provider_entries: list[dict[str, Any]], now: float | None = None) -> list[dict[str, Any]]:
    """挑出「快到期」的权益条目，供管理页顶部横幅展示（2026-10-03）。

    到期告警是**权益**的到期（``expire_ts``：套餐、加油包、签到积分、资源包
    这些领了就有保质期的东西），不是周期重置（``reset_ts``：5 小时窗口、
    weekly 池这类到点回满的）。两种时刻在前端是两个字段、两套文案，这里只认
    前者——把重充当到期会天天误报「你的额度快没了」（其实只是快刷新了）。

    两条过滤，缺一不可：

    1. **天数**：``days_left < EXPIRY_WARN_DAYS``（含已过期——最该提醒的正是
       这个）。
    2. **量**：``unit == "credit"`` 的条目还要 ``remaining >
       EXPIRY_WARN_MIN_CREDITS``。用户 2026-10-03 要求「大于 300 积分再提醒」：
       否则每天签到的 200 分小包一到 7 天内就刷一排横幅，真正值钱的（会员包、
       4000 分加油包）反而被淹没。非积分类（天数 / 次数 / 千分制）量纲不同、
       无法与 300 比较，只看天数——它们的 ``remaining`` 是「还剩几天」这类量，
       本来就该按天预警。

    **4 天内例外**（2026-11-04 用户要求）：``days_left <= EXPIRY_URGENT_DAYS``
    的条目跳过第 2 条量过滤，哪怕只象征性剩一点也报。用户明确说「某个通道
    里的某个账号中的某个资源包，离到期剩下 4 天也要告警出来」——这时它值不
    值得提醒已经不该由余量决定：4 天后这份额度就彻底作废了。``urgent`` 字段
    随条目下发，前端据此打「4 天内」标签。

    **但余量为 0 的不算**（2026-11-06 用户反馈）：已经烧干的包过期也带不走
    任何东西，报「剩 0 · 2 天后到期」是纯噪音，还占掉真正该看的那一行（用户
    原话：「剩余 0 的资源包，没必要因为日期快结束就告警吧，太傻瓜了吧」）。
    所以 4 天例外只豁免「量小」，不豁免「量空」——``_remaining_state`` 判定
    为 ``"empty"`` 的，无论哪天到期都不报。拿不到 ``remaining`` 的积分类条目
    平时跳过（无法判断是不是空壳），但受 4 天例外保护：那是「不知道剩多少」，
    不是「确定是 0」，漏报的代价更大，前端只是不显示余量。

    ``unit`` 缺失或为 ``None`` 时**不做量过滤**（宽松放行）：宁可多提醒一个，
    也别因为新通道忘了填 ``unit`` 而漏掉真到期。这是有意的取舍。

    每条还带 ``account`` / ``item_label``：多账号通道的 label 形如
    ``CodeBuddy #2 · 资源包``，拆开后前端才能把「通道 → 账号 → 资源包」
    两级列表渲染出来（``label`` 保留原文，兼容已有调用方）。

    纯函数（``now`` 可注入），便于测试；不触网、不改状态。
    """
    now = time.time() if now is None else now
    out: list[dict[str, Any]] = []
    for entry in provider_entries:
        if not isinstance(entry, dict):
            continue
        quota = entry.get("quota") or {}
        if not isinstance(quota, dict) or not quota.get("supported"):
            continue
        for it in quota.get("items") or []:
            if not isinstance(it, dict):
                continue
            expire_ts = _as_num(it.get("expire_ts"))
            if expire_ts is None or expire_ts <= 0:
                continue
            days_left = (expire_ts - now) / 86400.0
            if days_left >= EXPIRY_WARN_DAYS:
                continue
            # 已过期的也算「4 天内」（跳过 300 门槛），但它已经有「已过期」
            # 文案，不再挂「4 天内」标签，免得多余且读着别扭。
            within_urgent = days_left <= EXPIRY_URGENT_DAYS
            urgent = 0 <= days_left <= EXPIRY_URGENT_DAYS
            state = _remaining_state(it)
            # 剩 0 的包不管还剩几天都不报：过期也带不走任何额度，只会把真正
            # 要用的告警挤下去（2026-11-06 用户反馈）。
            if state == "empty":
                continue
            if it.get("unit") == "credit":
                raw_remaining = it.get("remaining")
                remaining = _as_num(raw_remaining)
                if remaining is None:
                    # 字段**缺失**（None）才算「拿不到余量」：平时不报（判不出
                    # 值不值得提醒），但 4 天内到期时照报——这时值不值得提醒
                    # 已经不由余量决定，漏报的代价更大；前端只是不显示余量。
                    # 显式给了但取不出数的（布尔/乱码）仍一律跳过，别把 `True`
                    # 当成「剩 1 分」报出去。
                    if not within_urgent or raw_remaining is not None:
                        continue
                elif not within_urgent and remaining <= EXPIRY_WARN_MIN_CREDITS:
                    continue
            raw_label = it.get("label")
            account, item_label = _split_account_label(raw_label)
            out.append({
                "provider": entry.get("id"),
                "provider_name": entry.get("name") or entry.get("id"),
                "label": raw_label,
                # 「通道 → 账号 → 资源包」两级展开用的结构化字段；没有账号
                # 前缀的通道 account 为空串，item_label 与 label 相同。
                "account": account,
                "item_label": item_label,
                "expire_ts": int(expire_ts),
                "days_left": round(days_left, 1),
                "remaining": it.get("remaining"),
                "unit": it.get("unit"),
                # 4 天内：前端给红色/「4 天内」标签，与普通临期区分开
                "urgent": urgent,
            })
    out.sort(key=lambda e: e["expire_ts"])
    return out


def _quota_low(provider_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """挑出「余额告急」的通道，供管理页顶部横幅展示（2026-10-03）。

    与 :func:`_expiring` 互补：那边管「权益什么时候到期」，这边管「还剩多少」
    ——用户要求「某个 provider 剩余不足 300 credits（或不足 8%）就上横幅」。

    **两套阈值按量纲分流**（用户同日澄清：8% 是说给不按 Credits 计费的渠道
    的——它们没有「credits」概念，只能按占比；Credits 渠道只看绝对值）：

    1. **credit 通道只看绝对值**：合计剩余 ``< QUOTA_LOW_MIN_CREDITS`` 才报。
       大包剩 950 绝对值还多，占比再低也不该报——早先把「或 8%」对 credit
       通道同时生效，CodeBuddy 剩 950/13200（7.2%）被报了警，用户指正。
    2. **非 credit 通道只看占比**：day（剩几天）/ count（次数）/ permille
       （千分制）跟 300 比大小毫无意义，改用剩余占比 ``<
       QUOTA_LOW_MIN_PERCENT``。

    加总口径与面板标题行（``quotaHeadSum``）一致——用户在面板上看到的
    「剩 X / Y」就是这里判定的输入：``sum_items`` 置真的通道把各条相加
    （Qoder 的并存额度），没置的只取第一条有数的——Trae 的明细包是「总额度」
    的拆解，相加会把同一份额度算两遍。恰好卡线（=300 / =8%）不算「不足」。

    ``remaining``/``total`` 拿不到数的条目跳过；通道内一条有数的都没有就不报
    ——无法判断的余额不告警。纯函数，不触网、不改状态。
    """
    out: list[dict[str, Any]] = []
    for entry in provider_entries:
        if not isinstance(entry, dict):
            continue
        quota = entry.get("quota") or {}
        if not isinstance(quota, dict) or not quota.get("supported"):
            continue
        rows: list[tuple[float, float, Any, Any]] = []
        for it in quota.get("items") or []:
            if not isinstance(it, dict):
                continue
            remaining = _as_num(it.get("remaining"))
            total = _as_num(it.get("total"))
            if remaining is None or total is None:
                continue  # 口径与 quotaHeadSum 的 usable 一致：两个数都得有
            rows.append((remaining, total, it.get("unit"), it.get("label")))
        if not rows:
            continue
        if quota.get("sum_items"):
            rem_sum = sum(r for r, _, _, _ in rows)
            tot_sum = sum(t for _, t, _, _ in rows)
            unit, label = rows[0][2], None   # 多条相加后 label 不再对应单一条目
        else:
            # 未声明可合计：第一条就是语义上的「总额度」，后面的不掺和
            rem_sum, tot_sum, unit, label = rows[0]
            # label 形如「Trae #1 · 总额度」，横幅上 provider_name 已经摆了
            # 「Trae」，再原样拼就是「Trae · 余额告急 · Trae #1 · …」两遍——
            # 剥掉通道名前缀，只留「#1 · 总额度」（多账号归属正是横幅要补的
            # 信息，用户 2026-10-06 反馈看不出告的是哪个号）。
            name = str(entry.get("name") or entry.get("id") or "")
            if label and name and label.startswith(name):
                label = label[len(name):].lstrip(" ·")
        percent_left = round(rem_sum / tot_sum * 100, 1) if tot_sum > 0 else None
        if unit == "credit":
            if rem_sum >= QUOTA_LOW_MIN_CREDITS:
                continue
        else:
            if percent_left is None or percent_left >= QUOTA_LOW_MIN_PERCENT:
                continue
        out.append({
            "provider": entry.get("id"),
            "provider_name": entry.get("name") or entry.get("id"),
            "label": label,
            "unit": unit,
            "remaining": round(rem_sum, 1),
            "percent_left": percent_left,
        })
    # 剩得最少的排最前；拿不到占比的（total 全缺）排最后
    out.sort(key=lambda e: (e["percent_left"] is None,
                            e["percent_left"] or 0.0,
                            e["remaining"]))
    return out


def _is_failure(data: Any) -> bool:
    """判断 provider 返回/兜错的结果是否算「查询失败」，决定用不用短 TTL。

    认三种形态：兜错字典（含 ``error``）、provider 直接在结果上标了失败
    （``query_failed``/``unreachable``）、以及 items 里**混进了**失败说明条
    （``trae.pat.quota._failure_notice`` 会把说明条插在首位，后面跟着各账号
    缓存数据——这种「部分失败」同样要短缓存，好让网络恢复后尽快自愈）。
    """
    if not isinstance(data, dict):
        return False
    if data.get("error") or data.get("query_failed") or data.get("unreachable"):
        return True
    items = data.get("items")
    if isinstance(items, list):
        return any(isinstance(i, dict) and i.get("query_failed") for i in items)
    return False


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def claimable_now(status: Any) -> bool:
    """上游此刻是否**有可领的签到**。

    用来盖过「今天已打过」的本地记录：历史按**日历日**去重，而轮换单位不一定
    是日历日。qoder 的活动窗口实测是「10:00 → 次日 09:59」，于是窗口内凌晨的
    巡检会把上一轮的 ``checked_in`` 补记成今天已打，等 10:00 新活动开出、上游
    报 ``claimable`` 时，光看本地记录就会整天跳过领取，界面也显示「已签到」+
    按钮禁用。（实测 ``logs/checkin.jsonl`` 里 qoder 至今两条记录的 message
    都是「今日已领取」，即都由「上游已签到 → 补记」写入、**代理自己没领过**，
    所以是「会漏」而非「已漏」——但界面确实已经在说「已签到」。）

    ``checked_in`` 与 ``claimable`` 同时为真时**以 checked_in 为准**：三家真实
    provider 都保证互斥（qoder 的 ``checked_in`` 就是 ``claimed and not
    claimable``），同时为真只能是上游自相矛盾或桩数据写错，此时宁可按「已打」
    处理——重复 claim 虽幂等，但每轮都会多打一次上游。
    """
    if not isinstance(status, dict):
        return False
    if status.get("error"):
        return False  # 查询失败：拿不到实况，不要据此推翻本地记录
    return bool(status.get("claimable")) and not status.get("checked_in")


async def _call(fn: Callable, *args):
    """调用 provider 的打卡/额度方法，同步异步都支持。

    provider 多为同步实现（urllib/httpx 同步），经 ``asyncio.to_thread`` 跑，
    避免阻塞事件循环；但 Qoder 这类通道只有异步 HTTP 客户端，``to_thread``
    拿到的是**协程对象**而不是结果（既不 await 就丢弃，返回值也不对），
    故按函数类型分派。
    """
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    return await asyncio.to_thread(fn, *args)


def read_checkin_settings() -> dict[str, Any]:
    saved = settings_mod.load_settings()
    raw_time = str(saved.get("checkin_time") or DEFAULT_CHECKIN_TIME)
    return {
        "auto_checkin": bool(saved.get("auto_checkin", True)),
        "checkin_time": raw_time if re.fullmatch(r"\d{1,2}:\d{2}", raw_time) else DEFAULT_CHECKIN_TIME,
    }


class CheckinHistory:
    """append-only 打卡历史（JSONL）。"""

    def __init__(self, path):
        self.path = path

    def append(self, provider: str, ok: bool, message: str = "",
               credits: Any = None, date: str | None = None) -> None:
        rec = {
            "ts": round(time.time(), 3),
            "date": date or _today(),
            "provider": provider,
            "ok": bool(ok),
            "message": (message or "")[:200],
            "credits": credits,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 历史落盘失败不影响打卡本身

    def entries(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        out = []
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def ok_dates_by_provider(self) -> dict[str, set[str]]:
        """provider -> 打卡成功的日期集合（含上游返回"已签到"的记录）。"""
        dates: dict[str, set[str]] = {}
        for rec in self.entries():
            if rec.get("ok"):
                dates.setdefault(rec.get("provider", ""), set()).add(rec.get("date", ""))
        return dates

    def calendar(self, days: int = 35) -> list[dict[str, Any]]:
        """最近 N 天的打卡日历，旧日期在前。

        每天一项：``{"date", "providers": [id...], "credits": {id: 积分}}``，
        credits 是当天实际领取的签到积分（上游签到历史合并进来的日子没有
        本地领取记录，只有 provider 名）。
        """
        by_date: dict[str, dict[str, Any]] = {}
        for rec in self.entries():
            if not (rec.get("ok") and rec.get("date")):
                continue
            info = by_date.setdefault(rec["date"], {"providers": [], "credits": {}})
            pid = rec.get("provider", "")
            if pid not in info["providers"]:
                info["providers"].append(pid)
            # 同日同通道有多条记录（自动轮询的 replay 每次都会落一行）：保留
            # 最大非零值——replay 拿不到金额（None/0），后到的 0 不能把当天
            # 真实领到的数糊成 +0。
            credit = rec.get("credits")
            if credit:
                prev = info["credits"].get(pid)
                info["credits"][pid] = max(prev or 0, credit)
        today = datetime.now().date()
        out = []
        for offset in range(days - 1, -1, -1):
            date = (today - timedelta(days=offset)).isoformat()
            info = by_date.get(date, {"providers": [], "credits": {}})
            out.append({
                "date": date,
                "providers": sorted(info["providers"]),
                "credits": info["credits"],
            })
        return out


def _feed_load_balance(provider_id: str, provider: Any, quota: Any) -> None:
    """把一轮额度结果喂给负载均衡的权重表（「账号 id → 剩余」）。

    放在 benefits 这一层而不是各 provider 的 ``quota()`` 里，是刻意的：
    额度条目已经由各通道统一成同一形状（label 带 ``#N`` 分组 + ``remaining``），
    在这里归并一次就能覆盖**所有**多账号通道，不必在 antigravity/kimi/qoder/
    trae/codex/kiro 各写一遍（写六遍就一定会有一两处忘掉，然后那个通道的
    权重永远是空的、静默退化成均匀轮转）。

    序号 → 账号 id 的映射**向 provider 要**（``_quota_account_ids``）：额度
    标签里的 ``#N`` 与账号快照的 ``index`` 同源，但各通道的账号列表函数签名
    不一致（有的带 region、有的不带），在这层自己拼会拼错——拼错就会把权重
    记到**别的账号**头上，轮转偏向错误方向，比没有权重更糟。

    失败一律吞掉：额度展示是主功能，权重只是它的副产品。
    """
    try:
        from buddy_proxy.core import load_balance as lb_mod

        totals = lb_mod.remaining_by_account((quota or {}).get("items"))
        if not totals:
            return
        by_index = getattr(provider, "_quota_account_ids", None)
        if not callable(by_index):
            return
        mapping = by_index()
        if not mapping:
            return
        mapped: dict[str, Any] = {}
        for key, remaining in totals.items():
            acct_id = mapping.get(key)
            if acct_id:
                mapped[acct_id] = remaining
        if mapped:
            lb_mod.weights_from_items(provider_id, mapped)
    except Exception:  # noqa: BLE001 - 权重是副产品，绝不能影响额度展示
        pass


class BenefitsManager:
    """打卡状态聚合 + 自动打卡后台循环。挂在 ``ProxyState.benefits`` 上。"""

    def __init__(self, history_path, state: Any):
        self.history = CheckinHistory(history_path)
        self._state = state
        self._task: asyncio.Task | None = None
        #: 负载均衡的额度预热循环（只有开着开关时才真的查上游）。
        self._lb_task: asyncio.Task | None = None
        self._last_attempt: dict[str, float] = {}
        self._startup_seen: set[str] = set()
        self._cache: dict[str, tuple[float, Any]] = {}
        # 后台刷新在飞任务（stale-while-revalidate 用）：同键去重
        self._refreshing: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # provider 能力
    # ------------------------------------------------------------------

    def _providers(self) -> dict[str, Any]:
        """默认 codebuddy 通道 + 已启用 provider（UI 面板按此顺序渲染，
        codebuddy 恒排首位，其余按注册序）。"""
        try:
            from buddy_proxy.codebuddy_provider import _default_codebuddy
            providers = {"codebuddy": _default_codebuddy}
        except Exception:
            providers = {}
        providers.update(getattr(self._state, "providers", {}) or {})
        return providers

    def provider_ids(self) -> set[str]:
        """返回当前已注册通道 id，供管理接口校验定向操作的目标。"""
        return set(self._providers())

    def checkin_providers(self) -> dict[str, Any]:
        return {
            pid: p for pid, p in self._providers().items()
            if getattr(p, "supports_checkin", False)
        }

    # ------------------------------------------------------------------
    # 快照（GET /ui/api/benefits）
    # ------------------------------------------------------------------

    async def snapshot(self, disabled_providers: set[str] | None = None) -> dict[str, Any]:
        checkin_cfg = read_checkin_settings()
        done = self.history.ok_dates_by_provider()
        today = _today()
        disabled = disabled_providers or set()

        # 通道间并发查：串行时整页耗时 = 各通道之和（实测 15-27s，qoder 一家
        # 的 8s deadline 就占去小半），而各通道互相独立、缓存键也各自独立，
        # gather 并发后整页 = 最慢一家（gather 保序，UI 顺序不变）。同通道的
        # checkin/quota 仍串行——同一家上游，别并发打。
        provider_entries = list(await asyncio.gather(
            *(self._provider_entry(pid, p, disabled, done, today)
              for pid, p in self._providers().items())
        ))
        # 上游直接给出的签到历史（如 CodeBuddy checkin_dates）合并进日历，
        # 这样代理没记录过的历史打卡天也能展示（无本地领取积分记录）
        day_map: dict[str, dict[str, Any]] = {}
        day_order: list[str] = []
        for d in self.history.calendar(35):
            day_map[d["date"]] = {
                "providers": set(d["providers"]),
                "credits": dict(d.get("credits") or {}),
            }
            day_order.append(d["date"])
        for entry in provider_entries:
            pid = entry["id"]
            for ds in entry["checkin"].get("checkin_dates") or []:
                if ds in day_map and pid not in day_map[ds]["providers"]:
                    day_map[ds]["providers"].add(pid)
        calendar_out = [
            {"date": d,
             "providers": sorted(day_map[d]["providers"]),
             "credits": day_map[d]["credits"]}
            for d in day_order
        ]

        return {
            "providers": provider_entries,
            "calendar": calendar_out,
            "auto_checkin": checkin_cfg["auto_checkin"],
            "checkin_time": checkin_cfg["checkin_time"],
            "checkin_enabled_providers": sorted(self.checkin_providers()),
            # 到期告警明细（见 _expiring）。放后端算而不是前端：阈值与「积分类
            # 才做量过滤」的规则集中一处，pytest 直接覆盖；前端只渲染不判规则，
            # 免得两边各写一份慢慢走偏。空列表＝无告警，前端据此隐藏横幅。
            #
            # 窗口天数一起下发：横幅汇总文案要写「将在 N 天内到期」，前端若
            # 自己存一份常量，改后端忘改前端就会出现「写着 7 天、实际 3 天」
            # 的文案（比不写更让人困惑）。下发之后只有一处定义。
            "expiry_warn_days": EXPIRY_WARN_DAYS,
            # 「4 天内」的紧急窗口也由后端下发：前端标签文案要写「4 天内」，
            # 前端自己存一份常量就会和后端阈值慢慢走偏（与 warn_days 同理）。
            "expiry_urgent_days": EXPIRY_URGENT_DAYS,
            # 告警过滤掉停用通道：整通道都不用了，它的权益到期/余额告急再上
            # 横幅就是骚扰（数据仍带在 providers 条目上，前端隐藏展示）
            "expiring": _expiring([e for e in provider_entries if not e["disabled"]]),
            # 余额告急明细（见 _quota_low）。与 expiring 同一套思路：规则集中
            # 后端、pytest 直接覆盖，前端只渲染不判规则。
            "low_quota": _quota_low([e for e in provider_entries if not e["disabled"]]),
        }

    async def _provider_entry(self, pid: str, p: Any, disabled: set[str],
                              done: dict[str, set[str]], today: str) -> dict[str, Any]:
        """单通道的快照条目（snapshot 的并发单元）。"""
        supports_checkin = bool(getattr(p, "supports_checkin", False))
        entry: dict[str, Any] = {
            "id": pid,
            "name": getattr(p, "name", pid),
            # 整通道停用标记（模型页 provider 开关）：前端额度卡/专属面板
            # 据此隐藏；数据照查照带，开关一开就能原样回来
            "disabled": pid in disabled,
            "checkin": {"supported": supports_checkin},
            "quota": {"supported": False},
        }
        if supports_checkin:
            status = await self._cached(f"checkin:{pid}", p.checkin_status)
            done_today = not claimable_now(status) and (
                today in done.get(pid, set())
                or bool(status and status.get("checked_in"))
            )
            # 状态里**不能被顶层覆盖**的键：
            # - ``supported``：上面的 ``{"supported": supports_checkin}`` 是前端
            #   ``p.checkin.supported`` 唯一的判据，被 provider 状态盖掉就整行
            #   打卡卡不渲染（qoder 状态里恰好也有个 supported）；
            # - ``checkin_dates``：这是**日历合并用的中间字段**（BenefitsManager
            #   自己消费后写进 calendar），provider 给的完整签到日期列表可达几十
            #   个日期，每个通道都塞进响应白占带宽；``sign_in_days`` 才是下发给
            #   界面的那份（多账号面板的签到列要用到，比如打卡日历里点某一天）。
            entry["checkin"].update({
                "done_today": done_today,
                **{k: v for k, v in (status or {}).items()
                   if k not in ("supported", "checkin_dates")},
            })
            # 声明支持打卡却查不出状态（返回裸 None / 非 dict）：必须标成
            # 「查询失败」而不是让它退化成「未签到」。缺了这个标记，前端只在
            # ``c.error``/``query_failed`` 下才显示「状态未知」，否则会画成
            # 「未签到」+ 可点的「立即打卡」——看起来像今天还没打，实际是
            # 拿不到状态（dumate 在无本机登录态时就是这条路径）。
            if not isinstance(status, dict):
                entry["checkin"]["query_failed"] = True
                entry["checkin"].setdefault(
                    "message", "签到状态查询失败（未登录/网络异常）")
        # 缓存键可带 provider 的「缓存代」：账号列表会变的通道（antigravity）
        # 声明 quota_epoch()，账号一变键就变，旧快照不再顶满 TTL
        epoch_fn = getattr(p, "quota_epoch", None)
        qkey = f"quota:{pid}" + (f":{epoch_fn()}" if callable(epoch_fn) else "")
        quota = await self._cached(qkey, p.quota)
        if quota is not None:
            entry["quota"] = {"supported": True, **quota}
            _feed_load_balance(pid, p, quota)
        return entry

    async def _cached(self, key: str, fn: Callable, *args):
        """TTL 缓存的 to_thread 调用；上游抛错时返回错误结构而不是 500。

        失败结果只短缓存（``FAILURE_TTL_S``）：额度/打卡这类查询失败往往是网络
        抖动（切 WiFi、VPN 重连），若按正常的 5 分钟缓存，用户会盯着一条不准确
        的「网关不可达 / 查询失败」警告好几分钟——哪怕几秒后网就恢复了。成功
        结果照常缓存 ``SNAPSHOT_TTL_S``。

        还有一条比 TTL 更早的失效条件：快照自己说的「翻转时刻」已过
        （见 :func:`_state_flipped`），此时状态必然已经变了，再拿它渲染就是
        在说谎。

        TTL 过期时走 stale-while-revalidate：**先回旧值**、后台再查。管理页
        不是常开的——每次隔几分钟再打开都正好赶上 TTL 失效，过去要等整串
        上游查完才见页面（串行时代实测 15-27s）；改后打开即得旧值，数据至多
        落后一轮刷新，卡片上的「↻/强刷」仍走 invalidate_* 删键直查，语义不变。
        只有真·冷缓存（键不存在）才同步等——所以首开仍受最慢通道约束
        （通道间已并发，见 snapshot）。

        **已翻转**（``_state_flipped``，next_ts 已过）与**失败结果过期**是两个
        不走 SWR 的例外：前者是界面倒计时正数到这个瞬间，用户等的就是状态
        翻新的这一拍——同步重查拿新值再回（先回旧值会让「即将刷新」的承诺
        落空：明明数到零了，按钮却还是灰的）；后者的旧值只是一条错误消息，
        「先回旧值」毫无价值，同步重查才兑现「短 TTL、30s 后自愈」的语义
        （``test_failure_result_uses_short_ttl`` 钉着这条）。
        """
        now = time.time()
        cached = self._cache.get(key)
        if cached:
            ttl = FAILURE_TTL_S if _is_failure(cached[1]) else SNAPSHOT_TTL_S
            flipped = _state_flipped(cached[1], now)
            if not flipped and now - cached[0] < ttl:
                return cached[1]
            if flipped or _is_failure(cached[1]):
                return await self._query(key, fn, *args)
            # 普通 TTL 过期（成功旧值）：先回旧值，后台刷新（同键去重）
            self._kick_refresh(key, fn, *args)
            return cached[1]
        return await self._query(key, fn, *args)

    def _kick_refresh(self, key: str, fn: Callable, *args) -> None:
        """过期键的后台刷新：同一时刻同键最多一个在飞，重复触发直接复用。"""
        if key in self._refreshing:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 理论上到不了（_cached 只在协程里被调）
            return
        task = loop.create_task(self._query(key, fn, *args))
        self._refreshing[key] = task
        task.add_done_callback(lambda _t, k=key: self._refreshing.pop(k, None))

    async def _query(self, key: str, fn: Callable, *args):
        """真查一次并写缓存；超时/异常都兜成错误结构（短缓存），绝不抛。

        ``wait_for`` 是给「provider 自己的超时失灵」兜底的：实测出现过单键
        挂 930s（网络风暴窗口期），没有外层硬限时的话整个管理页都会被它拖住。
        超时后底层线程可能还在跑（to_thread 取消不了线程），结果丢弃即可；
        失败按 ``FAILURE_TTL_S`` 短缓存，30s 后下一轮自然重试。
        """
        try:
            data = await asyncio.wait_for(_call(fn, *args), timeout=QUERY_TIMEOUT_S)
        except asyncio.TimeoutError:
            data = {"error": f"查询超时（>{QUERY_TIMEOUT_S:.0f}s）"}
        except Exception as exc:
            data = {"error": str(exc)[:300]}
        self._cache[key] = (time.time(), data)
        return data

    def invalidate_quota(self, provider_id: str) -> int:
        """作废某通道的额度缓存（管理页卡片「刷新」按钮的后端）。

        键带 ``quota_epoch`` 的通道（antigravity：账号列表指纹）键名不固定，
        所以按前缀删而不是精确删。返回删掉的条数（0 = 本来就没有缓存，下一轮
        查询照样真打上游，语义不变）。只动 quota，不动 checkin 状态——签到
        状态有自己的翻转判定（``_state_flipped``），不需要手动作废。
        """
        prefix = f"quota:{provider_id}"
        stale = [k for k in self._cache if k == prefix or k.startswith(prefix + ":")]
        for k in stale:
            del self._cache[k]
        return len(stale)

    def invalidate_checkin(self, provider_id: str) -> None:
        """作废某通道的签到状态缓存（管理页「强刷」按钮的后端）。

        与 :meth:`invalidate_quota` 分开：额度刷新不该连带打上游查签到
        （多账号通道是逐号真查），只有签到卡上的强刷明确要这个副作用。
        """
        self._cache.pop(f"checkin:{provider_id}", None)

    # ------------------------------------------------------------------
    # 手动 / 自动打卡
    # ------------------------------------------------------------------

    async def claim_now(self, provider_id: str) -> dict[str, Any]:
        provider = self.checkin_providers().get(provider_id)
        if provider is None:
            return {"ok": False, "error": f"provider {provider_id} 不支持打卡"}
        return await self._claim_and_record(provider_id, provider)

    async def _claim_and_record(self, provider_id: str, provider: Any) -> dict[str, Any]:
        # 领完就作废状态缓存：_tick 现在也走这份缓存，不清掉的话界面要等满
        # TTL（300s）才会从「立即打卡」翻成「已签到」。手动与自动共用这条路径。
        self._cache.pop(f"checkin:{provider_id}", None)
        try:
            status = await _call(provider.checkin_claim)
        except Exception as exc:
            message = str(exc)[:300]
            # 上游返回"已签到"类提示视为成功（幂等补记录）
            already = ("已签" in message) or ("already" in message.lower())
            self.history.append(provider_id, ok=already, message=message)
            return {"ok": already, "already": already, "message": message}
        # 金额取值链：实领（extra_credits）→ 上游回告（credits）→ 每日额
        # （daily_credit，dumate 是均值口径）。replay（重复轮询已领过）拿不到
        # 金额时保持 None——别把「未知」记成 0，日历会被 +0 糊掉真实值。
        credits = status.get("extra_credits")
        if credits is None:
            credits = status.get("credits")
        if credits is None:
            credits = status.get("daily_credit")
        self.history.append(provider_id, ok=True,
                            message=status.get("message", ""),
                            credits=credits)
        return {"ok": True, **(status or {})}

    # ------------------------------------------------------------------
    # 后台自动打卡循环
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())
        if self._lb_task is None or self._lb_task.done():
            self._lb_task = asyncio.get_running_loop().create_task(
                self._load_balance_warm_loop())

    async def stop(self) -> None:
        # 负载均衡预热循环先收：它只是给权重表送数，停掉不影响数据一致性
        if self._lb_task is not None:
            self._lb_task.cancel()
            try:
                await self._lb_task
            except asyncio.CancelledError:
                pass
            self._lb_task = None
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        # 后台刷新任务一并收掉，不留悬挂引用
        for t in self._refreshing.values():
            t.cancel()
        self._refreshing.clear()

    async def _load_balance_warm_loop(self) -> None:
        """负载均衡的额度预热：定期刷新各通道额度，喂权重表。

        为什么需要它：加权轮转的权重来自「账号 → 剩余积分」读数，而读数目前
        只在管理页打开额度页（``GET /ui/api/benefits``）时才被刷新。用户不开
        页面（最常见的情形——代理只当后台服务用）时，权重会一直停在未知，
        等于退化成均匀轮转，「按剩余积分均衡消耗」就名存实亡。

        只在**至少一个通道开着开关**时才真的查：关着开关的通道多打一次计费
        接口纯属浪费，而这是每 10 分钟一次的后台流量。

        失败一律吞掉：预热是优化，不该在日志里制造噪声，也不该拖垮任何东西。
        查询本身走 ``_cached``（TTL 300s）——与面板共用同一份缓存，管理页开着
        时这里绝大多数轮次都是缓存命中，不会额外打上游。
        """
        # 首轮稍等一会儿再跑：启动瞬间各通道的凭据/网络都还没热，且用户刚
        # 启动就打开管理页的路径已经会自己查一轮，没必要抢先撞上游。
        await asyncio.sleep(LOAD_BALANCE_WARM_DELAY_S)
        while True:
            try:
                enabled = set(getattr(self._state, "load_balance", None) or ())
                if enabled:
                    providers = self._providers()
                    for pid in sorted(enabled):
                        p = providers.get(pid)
                        if p is None:
                            continue
                        quota = await self._cached(f"quota:{pid}", p.quota)
                        # 面板打开时才由 _provider_entry 喂权重；这条后台路径
                        # 必须自己喂一次，否则「不开页面就永远没有权重」。
                        _feed_load_balance(pid, p, quota)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 预热失败不影响任何功能
            await asyncio.sleep(LOAD_BALANCE_WARM_INTERVAL_S)

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 自动打卡异常不影响代理转发
            await asyncio.sleep(CHECK_INTERVAL_S)

    async def _tick(self) -> None:
        cfg = read_checkin_settings()
        if not cfg["auto_checkin"]:
            return
        hh, mm = (int(x) for x in cfg["checkin_time"].split(":"))
        now = datetime.now()
        due_today = (now.hour, now.minute) >= (hh, mm)
        done = self.history.ok_dates_by_provider()
        for pid, provider in self.checkin_providers().items():
            first_run = pid not in self._startup_seen
            self._startup_seen.add(pid)
            # 启动后首轮视为补签窗口（不管是否到点）；之后只在到达设定时刻后打
            if not due_today and not first_run:
                continue
            if time.time() - self._last_attempt.get(pid, 0.0) < RETRY_THROTTLE_S:
                continue
            # 先查状态再决定是否领：避免对「已签到/当天无活动」的上游反复打 claim。
            # 走 _cached（与 snapshot 同一份，TTL 300s），管理页开着时这里几乎
            # 总是命中缓存，不会因本次调整而多打上游。
            status = await self._cached(f"checkin:{pid}", provider.checkin_status)
            if not isinstance(status, dict):
                continue
            # 查询失败（``_cached`` 把异常兜成 ``{"error": ...}``，不会抛出）时
            # 必须停在这里：拿不到实况就既不知道「打没打过」也不知道「有没有
            # 活动」，继续往下会对着一个查不通的通道反复 claim，把失败记录
            # 写进 checkin.jsonl 污染日历。失败结果只短缓存 30s，网络恢复后
            # 下一轮自然重试。
            # ``query_failed`` 是 provider 自己标的失败（如 dumate 无本机登录态
            # 时返回的失败结构，不经 ``_cached`` 兜错），与 ``error`` 同一语义。
            if status.get("error") or status.get("query_failed"):
                continue
            # 「今天已打过」要以上游实况为准（见 :func:`claimable_now`）：
            # 本地历史按日历日去重，而 qoder 这类通道按「10:00 窗口」轮换，
            # 光看本地记录会整天跳过领取。与 snapshot 共用同一个判据，避免
            # 两处口径漂移（一处认 claimable、另一处不认，就会界面说没打、
            # 后台也不领，或反过来）。
            if not claimable_now(status) and _today() in done.get(pid, set()):
                continue
            self._last_attempt[pid] = time.time()
            if status.get("checked_in"):
                # 上游已签到（如网页/客户端手动签过）→ 补记历史，当天不再重试
                self.history.append(pid, ok=True,
                                    message=status.get("message") or "已签到（上游记录）")
                continue
            if status.get("claimable") is False:
                continue  # 当天无签到活动（如 CodeBuddy 档期未开），保持轻量轮询
            await self._claim_and_record(pid, provider)
