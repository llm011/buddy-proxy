"""`buddy usage`：按通道 / 账号列出套餐余量与到期时间。

数据源分两层，取舍很明确：

1. **额度明细只从正在跑的服务拿**（``GET /ui/api/benefits``）。每条额度的
   ``remaining/total/percent/reset_ts`` 都来自各家计费接口，CLI 自己重算一份
   就得把 BenefitsManager、各通道 provider 的 quota()、5 分钟缓存全拉进这个
   进程——那不只是重复实现，还会和面板打出**两倍**的计费请求。服务正常时面板
   有缓存，克隆一份读缓存是最省事的正确做法。
2. **服务没起时退化为本地账号清单**（各通道 ``failover.accounts_status()``，
   纯读磁盘不触网）。至少能回答「这台机器上登了哪些号、冷却到什么时候」，
   并提示怎么拿到余量——比一句「服务未运行」有用。

``--refresh`` 才会 `POST /ui/api/benefits/refresh` 真打上游；默认只读缓存，
因为多数人一天查十来次额度，每次都强刷会把自己的配额查光（上游额度接口本身
也计费/限流）。
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from .render import (
    Palette, bar, display_width, humanize_duration, humanize_number, humanize_ts,
    pad, table,
)

_DEFAULT_HOST = "http://127.0.0.1:8787"

#: 与 `benefits._ACCOUNT_LABEL_RE` 同形：只认行首的 ``#N ·``，不碰名称中间的 ``#``。
_ACCOUNT_RE = re.compile(r"^(?:.*?\s*)?#(?P<account>\d+)\s*·\s*(?P<label>.+)$")


def _split_account(label: str) -> tuple[str, str]:
    """``AG #2 · Gemini` → ``("#2", "Gemini")``；无账号前缀 → ``("", label)``。"""
    m = _ACCOUNT_RE.match(str(label or "").strip())
    if not m:
        return "", str(label or "")
    return f"#{m.group('account')}", m.group("label").strip()


def _uuid_prefix(name: str) -> str:
    m = re.match(r"^([0-9a-f]{8})-[0-9a-f]{4}-", str(name or "").strip(), re.I)
    return m.group(1) if m else str(name or "")


def account_prefix(label: str, provider: str = "",
                   names: Optional[dict[str, str]] = None) -> str:
    """``Foo #2 · 资源包`` → ``Foo``；前缀是通道名（或其缩短版）/ 缺失 → ``""``。

    后端拼接的标签前缀有几种来源，都**不是**账号昵称：通道名本身
    （``Trae #1``）、通道名的缩短版（``Qoder 海外版`` → ``Qoder``）、通道名的
    UUID 指纹（``Acme #4 · …`` 里写的是 uuid 前 8 位）。这些显示成空，让账号只露
    ``#N`` 和邮箱/昵称。

    字面上认不出来的前缀（机翻、和通道名毫无重合的别名）宁可按账号名保留：
    误抹掉真名字比多显示一个前缀更难发现，准确识别靠 ``failover`` 的本地昵称。
    """
    m = _ACCOUNT_RE.match(str(label or "").strip())
    # 截到 ``#`` 前一位：正则的前缀组把可选空格也吃了。
    prefix = m.group(0)[: m.start("account") - 1].strip() if m else ""
    if not prefix:
        return ""
    return "" if _looks_like_provider(prefix, provider, names) else prefix


def _looks_like_provider(prefix: str, provider: str,
                         names: Optional[dict[str, str]] = None) -> bool:
    """前缀是不是通道自己的名字（而不是账号名）。

    可比的只有字面重合：通道 id、去掉括号说明的短名、以及 ``names`` 表里的
    展示名。纯机翻前缀（``百度搭子`` vs ``dumate``）字面上无解，这种宁可留着
    也不能用「等于某个别的名字」去猜——误删真账号名比多显示一个前缀更糟。
    """
    low = str(prefix or "").lower()
    pid = str(provider or "").lower()
    if not low or not pid:
        return False
    stems = {pid, pid.split(" (")[0].strip()}
    names = _provider_names() if names is None else names
    stems.add(str(names.get(pid) or "").lower())
    if any(s and (low in s or s in low) for s in stems):
        return True
    # 前缀恰好是通道 id 的 uuid 前 8 位（大小写不敏感）→ 同一回事。
    return bool(re.match(r"^[0-9a-f]{8}$", low) and low == _uuid_prefix(pid))


def _provider_names() -> dict[str, str]:
    """``{通道 id: 展示名}``（id 统一小写）；名字表不在就返回空表。"""
    try:
        from .doctor import channel_names
        return {pid.lower(): name for pid, name in channel_names()}
    except Exception:  # noqa: BLE001 - 名字表是锦上添花，缺了按 id 判断
        return {}


def _prefer_local_name(local: Optional[str], upstream: Optional[str]) -> str:
    """账号展示名取本地昵称，本地没有才用额度标签里的前缀。

    标签前缀常是 UUID / 邮箱的残片（``CodeBuddy #2 · 资源包`` 之于
    ``56624506-3``），本地 ``accounts_status()`` 的 nickname 才是人认得的名字。
    """
    return str(local or upstream or "")


def _http(method: str, url: str, body: Optional[dict[str, Any]] = None,
          timeout: float = 30.0) -> Any:
    # 管理接口只允许本机来源（见 web/ui/common.py::_ensure_local），所以默认
    # 地址是 127.0.0.1 而不是 PROXY_HOST——后者常配成 0.0.0.0，直连会失败。
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 只打本机
        return json.loads(resp.read().decode() or "{}")


def _collect_local_accounts() -> list[dict[str, Any]]:
    """各通道的本地账号状态（不触网）。导入失败/未实现的通道跳过。"""
    from .doctor import local_channels  # 共用一份通道表，避免两处漂移

    out: list[dict[str, Any]] = []
    for pid, _name, status in local_channels():
        accounts = status.get("accounts") or []
        for a in accounts:
            out.append({
                "provider": pid,
                "index": a.get("index"),
                # 别名优先：CodeBuddy 国内号的上游昵称是打码的 ``566****6``，
                # 用户自己起的 alias 才是能对上号的；两者都没有才退到 id。
                "name": a.get("alias") or a.get("nickname") or a.get("id") or "",
                "token": a.get("token"),
                "hours_left": a.get("hours_left"),
                "cooling": a.get("cooling") or [],
            })
    return out


def fetch_snapshot(base_url: str, refresh: Optional[str] = None) -> Optional[dict[str, Any]]:
    """服务在跑就返回 benefits 快照，否则 None（调用方退化成本地账号清单）。"""
    try:
        if refresh:
            return _http("POST", f"{base_url}/ui/api/benefits/refresh", {"provider": refresh})
        return _http("GET", f"{base_url}/ui/api/benefits")
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def build_usage(
    snapshot: Optional[dict[str, Any]],
    *,
    providers: Optional[list[str]] = None,
    accounts: Optional[list[str]] = None,
    local_accounts: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """把服务快照 + 本地账号合并成 CLI 的结构化视图（渲染与 ``--json`` 共用）。

    合并而不是二选一：额度条目来自上游计费接口（``quota.items``），但**冷却
    状态和登录态只有本地知道**——一个号因 429 冷却 10 分钟，额度面板上还是
    「剩 80%」，单看额度会以为能随便用。所以按「通道 → 账号」归组时，把本地
    ``accounts_status()`` 里的 cooling/token 一起贴上去。
    """
    providers_filter = {p.lower() for p in (providers or [])}
    accounts_filter = {a.lower().lstrip("#") for a in (accounts or [])}
    local_accounts = local_accounts or []

    local_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    local_providers: set[str] = set()
    for a in local_accounts:
        local_by_key[(a["provider"], f"#{a.get('index')}")] = a
        local_providers.add(a["provider"])

    groups: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}

    snapshot = snapshot if isinstance(snapshot, dict) else {}
    for entry in snapshot.get("providers") or []:
        pid = str(entry.get("id") or "")
        if providers_filter and pid.lower() not in providers_filter:
            continue
        g = seen.get(pid)
        if g is None:
            g = {
                "provider": pid,
                "name": entry.get("name") or pid,
                "disabled": bool(entry.get("disabled")),
                "checkin": entry.get("checkin") or {},
                "quota_supported": bool((entry.get("quota") or {}).get("supported")),
                "accounts": [],
                "unassigned": [],
            }
            seen[pid] = g
            groups.append(g)
        quota = entry.get("quota") or {}
        by_account: dict[str, dict[str, Any]] = {}
        for item in quota.get("items") or []:
            acct, label = _split_account(str(item.get("label") or ""))
            if not acct:
                g["unassigned"].append({**item, "account": "", "item_label": label})
                continue
            if accounts_filter and acct.lstrip("#").lower() not in accounts_filter:
                continue
            acc = by_account.get(acct)
            if acc is None:
                local = local_by_key.get((pid, acct)) or {}
                acc = {
                    "account": acct,
                    "name": _prefer_local_name(
                        local.get("name"),
                        account_prefix(str(item.get("label") or ""), g["name"])),
                    "token": local.get("token"),
                    "hours_left": local.get("hours_left"),
                    "cooling": local.get("cooling") or [],
                    "items": [],
                }
                by_account[acct] = acc
                g["accounts"].append(acc)
            acc["items"].append({**item, "account": acct, "item_label": label})
        if quota.get("message") or quota.get("error"):
            g["quota_message"] = quota.get("message") or quota.get("error")
        g["accounts"].sort(key=lambda a: int(a["account"].lstrip("#") or 0))
        if accounts_filter and not g["accounts"]:
            # 按账号筛选时只留「筛出来的号」；本地账号清单在后面补回来，
            # 服务侧额度缺失不会让整行消失。
            groups.remove(g)
            del seen[pid]

    # 本地有号但快照里没有（服务没起 / 该通道无额度接口）：补成「仅本地」组，
    # 这样 `buddy usage` 至少能回答「登了哪些号、有没有在冷却」。
    for a in local_accounts:
        pid = a["provider"]
        if providers_filter and pid.lower() not in providers_filter:
            continue
        acct = f"#{a.get('index')}"
        if accounts_filter and acct.lstrip("#").lower() not in accounts_filter:
            continue
        g = seen.get(pid)
        if g is None:
            g = {"provider": pid, "name": pid, "disabled": False, "checkin": {},
                 "quota_supported": False, "accounts": [], "unassigned": [],
                 "local_only": True}
            seen[pid] = g
            groups.append(g)
        existing = next((x for x in g["accounts"] if x["account"] == acct), None)
        if existing is not None:
            # 走「服务快照 + 局部失败」：额度条目缺了、账号清单还在。名字以本地为准。
            if a.get("name"):
                existing["name"] = _prefer_local_name(a.get("name"), existing.get("name"))
            continue
        g["accounts"].append({
            "account": acct, "name": a.get("name") or "", "token": a.get("token"),
            "hours_left": a.get("hours_left"), "cooling": a.get("cooling") or [],
            "items": [],
        })
    for g in groups:
        g["accounts"].sort(key=lambda a: int(a["account"].lstrip("#") or 0))

    return {
        "source": "service" if snapshot else "local",
        "fetched_at": time.time(),
        "providers": groups,
        # 告警原样透传（后端已经按「停用通道不上横幅」过滤过）。按通道过滤时
        # CLI 自己再筛一遍，免得 `buddy usage -p kimi` 下面还列着别家的到期提醒。
        "expiring": [e for e in snapshot.get("expiring") or []
                     if not providers_filter
                     or str(e.get("provider") or "").lower() in providers_filter],
        "low_quota": [e for e in snapshot.get("low_quota") or []
                      if not providers_filter
                      or str(e.get("provider") or "").lower() in providers_filter],
        "expiry_warn_days": snapshot.get("expiry_warn_days"),
        "expiry_urgent_days": snapshot.get("expiry_urgent_days"),
        "local_accounts": len(local_accounts),
        "local_providers": sorted(local_providers),
    }


def _cooling_text(acc: dict[str, Any]) -> str:
    parts = []
    for c in acc.get("cooling") or []:
        mins = c.get("minutes_left")
        if mins:
            parts.append(f"{c.get('kind') or '冷却'} {mins:g}分")
    return "、".join(parts)


#: 账号展示名截断宽度。UUID / 邮箱做「账号名」时整行会被撑到 60+ 列，
#: 真正要看的「剩余 / 到期」被挤出屏幕——截断到 24 格，要看全名用 ``--json``。
_NAME_WIDTH = 24


def _short_name(name: str) -> str:
    if display_width(name) <= _NAME_WIDTH:
        return name
    out = ""
    for ch in name:
        if display_width(out + ch) > _NAME_WIDTH - 1:
            break
        out += ch
    return out + "…"


def _unit_suffix(unit: Optional[str]) -> str:
    if unit == "permille":
        # 千分制是「剩 983.9/1000」这种刻度，用户看的是比例不是千分号。
        return "%"
    if unit == "currency":
        return ""  # DeepSeek 的 CNY 余额写进标签，数字后面再挂 currency 只会让人误读
    return str(unit) if unit else ""


def _amount(value: Any, unit: Optional[str]) -> str:
    """``(983.9, "permille")`` → ``98.4%``；``(78.12, "credit")`` → ``78.12credit``。"""
    if value is None or isinstance(value, str):
        return humanize_number(value)
    n = humanize_number(value)
    if unit == "permille":
        try:
            n = humanize_number(round(float(value) / 10, 1))
        except (TypeError, ValueError):
            pass
    return f"{n}{_unit_suffix(unit)}"


def _is_message_item(it: dict[str, Any]) -> bool:
    """``remaining`` 是文字提示的条目（如「Copilot 未登录…」）——不是数字，别进表。"""
    return isinstance(it.get("remaining"), str)


#: 每个账号最多展开这么多行明细，其余收成「还有 N 项」。Trae 把每天签到的
#: 200 分拆成独立条目（一个号 30+ 行），DuMate 每天发一个 500 分小包。
_MAX_ITEM_ROWS = 12


def _detail_key(it: dict[str, Any]) -> tuple:
    """除「到期时间」以外完全一样的条目视为同一类包（逐日签到包）。"""
    return (it.get("item_label") or it.get("label"), it.get("unit"),
            it.get("remaining"), it.get("total"), it.get("percent"))


def _condense(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把逐日发放的相同小包合并成一个「区间」条目，并压低总行数。

    Trae 的「签到奖励 200/200」会连着 30 天各一条；如果按原样列，额度表会在
    一个号里就排出一屏，真正该看的「总额度」被顶出视野。合并后保留**最近**
    到期那一条的时间（first_expire_ts / last_expire_ts），表格里显示成区间。
    """
    out: list[dict[str, Any]] = []
    groups: dict[tuple, dict[str, Any]] = {}
    for it in items:
        k = _detail_key(it)
        if k not in groups:
            copy = dict(it)
            copy["_expires"] = [it["expire_ts"]] if it.get("expire_ts") else []
            groups[k] = copy
            out.append(copy)
            continue
        g = groups[k]
        g["_count"] = g.get("_count", 1) + 1
        if it.get("expire_ts"):
            g["_expires"].append(it["expire_ts"])
    for g in out:
        exp = g.pop("_expires", [])
        if len(exp) > 1:
            g["expire_ts"] = max(exp)
            g["expire_first_ts"] = min(exp)
    # 有总额度的行排前面（它是「还剩多少」的答案），逐日小包等细化项在后。
    out.sort(key=lambda it: (bool(it.get("_count")), -(it.get("total") or 0)))
    if len(out) <= _MAX_ITEM_ROWS:
        return out
    head, rest = out[:_MAX_ITEM_ROWS - 1], out[_MAX_ITEM_ROWS - 1:]
    summary = {"item_label": f"还有 {len(rest)} 项", "label": "", "unit": None,
               "remaining": None, "total": None, "percent": None,
               "reset_ts": None, "expire_ts": None, "_summary": True}
    return head + [summary]


def _expiry_text(it: dict[str, Any]) -> str:
    first, last = it.get("expire_first_ts"), it.get("expire_ts")
    if first and last and first != last:
        return f"{humanize_ts(first)[5:10]}~{humanize_ts(last)[5:]}"
    return humanize_ts(last)[5:] if last else "—"


def _used_percent(it: dict[str, Any]) -> Optional[float]:
    """已用百分比：优先用后端给的 ``percent``，缺失时按 used/total 推。

    进度条与颜色分档（``render.bar``）都按「已用」理解百分比，但各家
    provider 是自己算 ``percent`` 的，迟早有通道漏给或算反（Copilot 曾按剩余
    算，导致剩 99% 画成满格红条）。有条目同时给了 used/total、偏偏没给
    percent 时，CLI 自己推一份，至少不会整列显示「—」。
    """
    percent = it.get("percent")
    if isinstance(percent, (int, float)) and not isinstance(percent, bool):
        return float(percent)
    used, total = it.get("used"), it.get("total")
    if isinstance(used, (int, float)) and isinstance(total, (int, float)) \
            and not isinstance(used, bool) and not isinstance(total, bool) and total:
        return round(used / total * 100, 1)
    return None


def _item_rows(acc: dict[str, Any], palette: Palette, *, show_name: bool) -> list[list[str]]:
    rows = []
    out = []
    for it in _condense([x for x in (acc.get("items") or []) if not _is_message_item(x)]):
        label = str(it.get("item_label") or it.get("label") or "")
        if it.get("_count", 1) > 1:
            label += f" ×{it['_count']}"
        if it.get("_summary"):
            row = ["—", "—", "—", "", "—", "—"]
        else:
            unit = it.get("unit")
            percent = _used_percent(it)
            pct_text = (f"{float(percent):.0f}%"
                        if isinstance(percent, (int, float)) else "—")
            row = [
                _amount(it.get("remaining"), unit),
                _amount(it.get("total"), unit),
                pct_text,
                bar(percent, 8, palette),
                humanize_ts(it.get("reset_ts")) if it.get("reset_ts") else "—",
                _expiry_text(it),
            ]
        if show_name:
            row.insert(0, label)
        out.append(row)
    return out


def _items_table(items: list[dict[str, Any]], palette: Palette) -> list[str]:
    items = [it for it in items if not _is_message_item(it)]
    if not items:
        return []
    labels = [str(it.get("item_label") or it.get("label") or "") for it in items]
    # 所有条目同名（CodeBuddy 的「资源包」、单账号通道里的「周额用量」）时这一列
    # 没有任何区分度，只白占宽度；有差异（加油包 / 订阅额度）才显示。
    show_name = len(set(labels)) > 1
    head = (["额度项"] if show_name else []) + ["剩余", "总量", "已用", "", "重置", "到期"]
    aligns = (["left"] if show_name else []) + [
        "right", "right", "right", "left", "left", "left"]
    return table(head, _item_rows({"items": items}, palette, show_name=show_name),
                 palette, aligns)


def _message_items(g: dict[str, Any]) -> list[str]:
    out = [str(it.get("remaining")) for it in g.get("items") or []
           if _is_message_item(it) and it.get("remaining")]
    if g.get("quota_message"):
        out.append(str(g["quota_message"]))
    return out


def render_text(view: dict[str, Any], palette: Palette, *, show_empty: bool = False) -> list[str]:
    lines: list[str] = []
    if view["source"] != "service":
        lines.append(palette.warn(
            "服务未运行（或 /ui/api/benefits 不可达），以下只有本地账号状态；"
            "余量/到期时间需要服务在跑：buddy start 后重试。"))
        lines.append("")
    groups = view.get("providers") or []
    if not groups:
        lines.append("没有匹配的通道。可用 `buddy usage --list-providers` 看有哪些。")
        return lines

    hidden = 0
    for g in groups:
        # 「通道表里有、本地没号、快照也没额度」的通道整个藏掉（`--all` 才列）：
        # 34 个通道 id 逐个打印「无额度信息」只会淹没有数据的那些。
        empty = not g["accounts"] and not _message_items(g) and not g.get("quota_supported")
        if empty and not show_empty:
            hidden += 1
            continue
        title = g["name"]
        if g.get("disabled"):
            title += "（已停用）"
        if g.get("local_only"):
            title += palette.dim(" · 只列出本地账号，余量要看服务在跑（buddy start）")
        elif not g.get("quota_supported"):
            title += palette.dim(" · 无额度信息")
        elif not g["accounts"]:
            title += palette.dim(" · 本地未见账号记录，以下为额度接口返回")
        lines.append(palette.head(title))
        for msg in _message_items(g):
            lines.append("  " + palette.dim(msg))
        for acc in g["accounts"]:
            label = f"  账号 {acc['account']}"
            if acc.get("name"):
                label += " " + _short_name(acc["name"])
            if acc.get("token") == "missing":
                label += " " + palette.bad("（登录态缺失，需 buddy login）")
            cooling = _cooling_text(acc)
            if cooling:
                label += " " + palette.warn(f"（冷却中：{cooling}）")
            elif (isinstance(acc.get("hours_left"), (int, float))
                  and acc.get("token") != "missing" and acc["hours_left"] < 24):
                # 额度本身是周期性的，凭证才是「会不会突然用不了」——剩不到
                # 一天才值得占位（各家都会自动续，失败的才需要 login）。
                # <1 小时的说法是秒/分钟级，截到「0分钟」等于没说。
                left = "不到1小时" if acc["hours_left"] * 3600 < 3600 else \
                    humanize_duration(acc["hours_left"] * 3600)
                label += " " + palette.warn(f"（凭证剩 {left}）")
            lines.append(label)
            rendered = _items_table(acc.get("items") or [], palette)
            if rendered:
                lines.extend("  " + ln for ln in rendered)
            elif not g.get("local_only") and g.get("quota_supported"):
                lines.append("  " + palette.dim("（这个号没有返回额度条目）"))
        if g.get("unassigned"):
            lines.append("  " + palette.dim("未分账号："))
            rendered = _items_table(g["unassigned"], palette)
            if rendered:
                lines.extend("  " + ln for ln in rendered)
        lines.append("")
    if hidden:
        lines.append(palette.dim(f"另有 {hidden} 个通道未登录/无额度接口，--all 可列出。"))
        lines.append("")

    lines.extend(_render_warnings(view, palette))
    return lines


#: 告警最多逐条列这么多行，其余收成一句汇总。Trae 每天发 100 分签到包，
#: 7 天内到期的能有十几条——全列出来会把表格顶出屏幕，而名字一模一样的
#: 「签到奖励」列十几遍对决策没有任何帮助。
_WARN_LINES = 6


def _days_text(days: float) -> str:
    if days < 0:
        return f"已过期 {humanize_duration(-days * 86400)}"
    return f"{humanize_duration(days * 86400)}"


def _render_warnings(view: dict[str, Any], palette: Palette) -> list[str]:
    """到期 / 余额告急提示。判定规则在后端（见 ``benefits._expiring``），这里只排版。"""
    out: list[str] = []
    expiring = view.get("expiring") or []
    if expiring:
        warn_days = view.get("expiry_warn_days") or 7
        urgent = view.get("expiry_urgent_days") or 4
        out.append(palette.warn(f"将在 {warn_days} 天内到期（{urgent} 天内加急）："))
        # 紧急的排前面：后端按到期时间排，已过期/今天到期的不该被一堆「提醒」压下去。
        ranked = sorted(expiring, key=lambda e: (e.get("days_left") or 0))
        # 同名同额同通道的条目合成一行：Trae 每天发一个 100 credit 的签到包，
        # 逐条列就是四行一模一样的「签到奖励」，用户看不出「一共多少会过期」。
        merged: dict[tuple, dict[str, Any]] = {}
        for e in ranked:
            key = (e.get("provider"), e.get("label"), e.get("remaining"), e.get("unit"))
            m = merged.get(key)
            ts = e.get("expire_ts")
            if m is None:
                # 区间两端都从这一条起步；之后的条目各取 min/max，而不是「后到覆盖」——
                # ranked 只按 days_left 排，同一通道内 expire_ts 的先后不定，
                # 靠顺序定右端会把区间写反。
                merged[key] = {**e, "count": 1, "first_ts": ts, "last_ts": ts}
            else:
                m["count"] += 1
                if ts:
                    lo = m.get("first_ts")
                    if lo is None or ts < lo:
                        m["first_ts"] = ts
                    if ts > (m.get("last_ts") or 0):
                        m["last_ts"] = ts
        picked = list(merged.values())[:_WARN_LINES]
        for m in picked:
            days = m.get("days_left") or 0
            tag = palette.bad("紧急") if days <= urgent else palette.warn("提醒")
            amount = m.get("remaining")
            amt = _amount(amount, m.get("unit")) if amount is not None else ""
            when = humanize_ts(m.get("expire_ts"))
            if m.get("count", 1) > 1:
                # 区间而不是单点：N 个包到期时间不同，写最早那个会让人以为只有一份。
                label = f"{m.get('label')} ×{m['count']}"
                first_ts = m.get("first_ts") or m.get("expire_ts")
                last = m.get("last_ts")
                head = humanize_ts(first_ts)[5:]
                tail = humanize_ts(last)[5:] if last else "?"
                # 几份包恰好同一时刻到期（Trae 会这样）时写成 ``11-17 ~ 11-17`` 反而像出错了。
                when = head if head == tail else f"{head} ~ {tail}"

            else:
                label = m.get("label") or "额度"
            out.append(f"  {tag} {m.get('provider_name') or m.get('provider')} · "
                       f"{label}  剩 {amt}  {_days_text(days)}起（{when}）"
                       if m.get("count", 1) > 1 else
                       f"  {tag} {m.get('provider_name') or m.get('provider')} · "
                       f"{label}  剩 {amt}  {_days_text(days)}（{when}）")
        rest = list(merged.values())[_WARN_LINES:]
        if rest:
            by_provider: dict[str, int] = {}
            for m in rest:
                name = m.get("provider_name") or m.get("provider") or "?"
                by_provider[name] = by_provider.get(name, 0) + m.get("count", 1)
            detail = "、".join(f"{k} {v} 条" for k, v in by_provider.items())
            out.append(palette.dim(f"  另有 {sum(by_provider.values())} 条（{detail}），"
                                   "完整清单见面板或 buddy usage --json"))
        out.append("")
    low = view.get("low_quota") or []
    if low:
        out.append(palette.warn("余额告急："))
        for e in low:
            amt = _amount(e.get("remaining"), e.get("unit"))
            pct = e.get("percent_left")
            pct_text = f"（剩 {pct:g}%）" if isinstance(pct, (int, float)) else ""
            text = (f"  {e.get('provider_name') or e.get('provider')} · "
                    f"{e.get('label') or '合计'}  剩 {amt}{pct_text}")
            if isinstance(pct, (int, float)) and pct >= 50:
                # 后端按「单条额度项」判低额，而部分通道的 ``items[0]`` 是短窗口
                # （Kimi 的 5 小时窗口），窗口一重置就又满——这种不是真告急，
                # 不必给人红色警报，标注一下口径即可。
                text += "  " + palette.dim("（该窗口周期短，按总量看更准）")
            out.append(text)
        out.append("")
    return out


def compute_exit_code(view: dict[str, Any]) -> int:
    """有紧急项（到期加急 / 余额告急）时返回 2，方便脚本里 ``if buddy usage``。

    0 = 一切正常；1 留给真正的执行失败（参数错、异常），2 = 「查到了，但要你
    注意」。不分这两个码，监控脚本就分不清「网关挂了」和「额度快没了」。
    """
    urgent = view.get("expiry_urgent_days") or 4
    for e in view.get("expiring") or []:
        if (e.get("days_left") or 99) <= urgent:
            return 2
    if view.get("low_quota"):
        return 2
    return 0


def run(args: Any) -> int:
    palette = Palette(None if not getattr(args, "no_color", False) else False)
    base_url = getattr(args, "base_url", "") or _DEFAULT_HOST

    from .doctor import channel_names

    providers = list(getattr(args, "provider", None) or [])
    if getattr(args, "list_providers", False):
        for pid, name in channel_names():
            print(f"{pad(pid, 14)} {name}")
        return 0
    known = {pid for pid, _ in channel_names()}
    unknown = [p for p in providers if p.lower() not in known]
    if unknown:
        print(f"未知通道: {', '.join(unknown)}（buddy usage --list-providers 查看）",
              file=sys.stderr)
        return 1

    snapshot = fetch_snapshot(base_url, getattr(args, "refresh", None))
    view = build_usage(
        snapshot,
        providers=providers,
        accounts=getattr(args, "account", None),
        local_accounts=_collect_local_accounts(),
    )
    if getattr(args, "json", False):
        print(json.dumps(view, ensure_ascii=False, indent=2, default=str))
    else:
        lines = render_text(view, palette, show_empty=getattr(args, "all", False))
        for ln in lines:
            print(ln)
        if view["source"] == "service":
            print(palette.dim(
                f"数据时间 {humanize_ts(view['fetched_at'])}"
                "（面板缓存；强刷上游用 buddy usage --refresh [通道]）"))
    return compute_exit_code(view)
