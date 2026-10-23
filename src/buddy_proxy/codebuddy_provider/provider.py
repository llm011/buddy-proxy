"""CodeBuddy 默认上游 provider：认证、签到/额度/流水查询与转发分派。

复杂转发逻辑（SSE 解析、DSML、协议转换）在 ``pipeline``；本模块的
``CodeBuddyProvider.forward`` 做「多账号 failover + 认证 + 构造上游请求 +
分派流式/非流式」。

**多账号**（照 qoder/trae 同款，动机 2026-10-06：单账号额度耗尽 429 code 14018
即整通道挂）：``credentials`` 存每账号凭据、``failover`` 管冷却；forward 按
failover 顺位逐账号尝试，账号级错误（401/429）冷却当前号换下一个。流式场景
靠**首事件闸门**（见 ``_forward_once``）：``stream_upstream`` 撞非 200 会把
错误帧当首个 yield 吐出来再 return——下游视角是 200 的 SSE 假成功，必须在
吐给客户端之前把错误帧还原成 HTTPException 才能安全换号。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from buddy_proxy.providers.base import BaseProvider
from buddy_proxy.core.checkin import (
    SOURCE_INFERRED,
    SOURCE_UPSTREAM,
    daily_reset_within_season,
    next_daily_reset,
    parse_upstream_datetime,
)
from buddy_proxy.core.metrics import ACCOUNT_META
from buddy_proxy.core.state import diagnostic

from . import credentials as creds
from . import failover
from .observability import body_summary
from .pipeline import desensitize_body

log = logging.getLogger(__name__)

#: 多账号签到领取的账号间强制间隔（照 trae 同款）：上游活动接口对短窗口连打
#: 有频控（trae 实测零间隔必中「参与用户太多」），多账号逐个领时从第 2 个
#: 账号起先歇一拍，节奏像「人在操作」。
_CHECKIN_THROTTLE_S = 4.0


def get_state():
    """经包命名空间转发 ``state.get_state``。

    本模块各方法体内的 ``get_state()`` 全局查找都经由这里：测试以
    ``monkeypatch.setattr(codebuddy_provider, "get_state", ...)`` 打补丁时
    （见 test_workbuddy_usage_records），方法体取到的是补丁对象而非导入期
    绑定的原函数。未打补丁时经包 ``__getattr__`` 解析到 observability 里
    的原函数，行为与直接导入一致。
    """
    import buddy_proxy.codebuddy_provider as _pkg

    return _pkg.get_state()


def _normalize_tool_choice(tool_choice: Any) -> Any:
    """把各种 object 形式 tool_choice 一律转成上游接受的 string 形式。

    上游 CodeBuddy 后端（Go）的 Request.tool_choice 字段是 string 类型，任何
    object 形式都会触发 400：``cannot unmarshal object into Go struct field
    Request.tool_choice of type string``（上游错误码 11101 Unmarshal chat
    params failed）。实测该错误在 #35 之后仍在复现，因为当时只覆盖了
    ``{"type":"function",...}`` 一种形态，而 Anthropic 协议的
    ``{"type":"any"|"auto"|"none"}`` 会原样漏到上游（Anthropic 侧允许把
    tool_choice 写成这两种形式，Claude Code 会发 ``{"type":"any"}``）。

    归一规则（语义等价映射）：
      - ``{"type":"function","function":{"name":"X"}}`` → "X"（强制调用 X）
      - ``{"type":"function"}``（缺 name）              → "required"
      - ``{"type":"any"}``   / "any"                    → "required"（必须调工具）
      - ``{"type":"auto"}``  / "auto"                   → "auto"
      - ``{"type":"none"}``  / "none"                   → "none"
      - ``{"type":"tool","name":"X"}``（Anthropic 原生）→ "X"
      - 其余 dict（无法识别）                           → "auto"（兜底，绝不放 object 过去）
    """
    if not isinstance(tool_choice, dict):
        # 字符串形式原样透传；"any" 是 Anthropic 叫法，上游只认 "required"
        return "required" if tool_choice == "any" else tool_choice

    name = (tool_choice.get("function") or {}).get("name")
    if isinstance(name, str) and name:
        return name
    # Anthropic 原生 {"type":"tool","name":"X"}
    if tool_choice.get("type") == "tool":
        tool_name = tool_choice.get("name")
        if isinstance(tool_name, str) and tool_name:
            return tool_name

    kind = tool_choice.get("type")
    # {"type":"function"} 缺 name：沿用历史行为退化为 required（强制调用工具）
    if kind in ("function", "any", "required"):
        return "required"
    if kind in ("auto", "none"):
        return kind
    if kind is None:
        # 无 type 但有 name（非标准写法）：当作指定函数
        if isinstance(tool_choice.get("name"), str) and tool_choice.get("name"):
            return tool_choice["name"]
        return "auto"
    # 未知形态兜底：宁可退化成 auto，也不能把 object 发给上游（必然 400）
    return "auto"


def _ensure_leading_system(messages: Any) -> Any:
    """海外版风控要求 ``messages`` 第一条必须是 system role，否则整单被
    ``11128 first message is not system prompt`` 拦截（2026-10-15 实测，
    CN 版 copilot.tencent.com 不校验，仅 www.codebuddy.ai 新加）。

    首条已是 system 时原样返回；否则在最前插一个空 system 块——实测
    ``content:""`` 即可过闸，不给模型注入任何 persona，行为最贴近「用户没发
    system」。只在 :meth:`CodeBuddyIntlProvider`（region=global）的转发链上调用，
    CN 请求不碰（别给没发 system 的 CN 请求凭空加块）。
    """
    if not isinstance(messages, list):
        return messages
    first = messages[0] if messages else None
    if isinstance(first, dict) and first.get("role") == "system":
        return messages
    return [{"role": "system", "content": ""}, *messages]


def _is_account_error(e: HTTPException) -> bool:
    """该 HTTPException 是否「换下一个账号可能好转」。

    401 = 凭据失效（换号有意义）；429 = 额度/限流（如 code 14018 单账号额度
    耗尽，换号有意义）。502/504 是通道级（所有账号同网关，换号无意义）；
    其余业务 4xx（模型名不合法等）换号也无意义。
    """
    return e.status_code in (401, 429)


def _error_frame_to_exception(frame: bytes, protocol: str) -> HTTPException | None:
    """首个 yield 是否上游错误帧（``stream_upstream`` 非 200 时吐单个错误块
    再 return）；是则还原成带状态码的 HTTPException（failover 循环据此换号）。

    错误帧形状（pipeline.stream_upstream L203-226）：
    - anthropic: ``event: error\\ndata: {"type":"error","error":{"type":
      "api_error","message":"Upstream API error (HTTP 429): …"}}`` ——**没有**
      数字 code 字段，状态码只在 message 的 ``(HTTP nnn)`` 里，从那儿抠；
    - openai: ``data: {"error":{…,"code":429,…}}`` ——code 就是状态码。
    """
    try:
        text = bytes(frame).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — 解不出来的不是我们的错误帧
        return None
    data_part = ""
    if "data:" in text:
        data_part = text.split("data:", 1)[1].strip()
    try:
        payload = json.loads(data_part) if data_part else None
    except json.JSONDecodeError:
        payload = None

    if protocol == "anthropic":
        if not text.startswith("event: error"):
            return None
        msg = ""
        if isinstance(payload, dict):
            err = payload.get("error") or {}
            if isinstance(err, dict):
                msg = str(err.get("message") or "")
        m = re.search(r"HTTP (\d{3})", msg or text)
        try:
            status = int(m.group(1)) if m else 502
        except ValueError:
            status = 502
        return HTTPException(
            status_code=status,
            detail={"error": {"message": msg or text[:200],
                              "type": "upstream_error"}})

    # openai / responses：错误块是 {"error": {...}} JSON
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if not isinstance(err, dict) or not err:
        return None
    code = err.get("code")
    try:
        status = int(code) if code is not None else 502
    except (TypeError, ValueError):
        status = 502
    return HTTPException(
        status_code=status,
        detail={"error": {"message": str(err.get("message") or "upstream error"),
                          "type": "upstream_error",
                          "details": str(err.get("details") or "")}})


class CodeBuddyProvider(BaseProvider):
    """默认的 CodeBuddy 上游，实现 BaseProvider 接口。

    与豆包（DoubaoProvider）对称统一。CodeBuddy 的复杂转发逻辑
    （SSE 解析、DSML、工具调用、协议转换）仍由本模块的
    ``stream_upstream`` / ``collect_upstream`` / ``convert_nonstream``
    承担，本类只做「认证 + 构造上游请求 + 分派流式/非流式」。
    """

    id = "codebuddy"
    name = "CodeBuddy"
    # WorkBuddy/CodeBuddy IDE 提供每日签到（billing/meter，2026-09 从 IDE asar 反查）
    supports_checkin = True

    def __init__(self, region: str = "cn") -> None:
        # 账号归属区（cn/global）。CN 通道默认 cn；海外版（codebuddyintl）
        # 子类钉 global——两通道共用同一账号 store，但 failover/签到/额度
        # 各自只看自己区的账号，绝不互捞。
        self._region = region
        super().__init__()

    def models(self) -> list[dict[str, Any]]:
        # CodeBuddy 的模型列表由 /v1/models 统一从本地配置加载，
        # 此处返回空（不参与 provider 路由的模型合并，避免重复）。
        return []

    def ensure_auth(self) -> None:
        state = get_state()
        state.ensure_auth()

    # ---- 打卡 / 额度（/ui 管理页消费，均经 asyncio.to_thread 调用） ----
    # 端点来自 WorkBuddy IDE asar 反查（2026-09）：Desktop 走 /v2 前缀的 IDE 网关。
    # ⚠️ 签到状态必须用 checkin-activity-status（活动版）——不带 /v2 的
    # checkin-status 是另一个（web/cookie）变体，Bearer 调用只会返回全空数据。
    # 签到活动有档期（如「开学季」9/1-9/15），active=false 表示当前档期未开。

    #: 额度/签到分组标签的通道名（``CodeBuddy #N · ``）。
    _quota_tag = "CodeBuddy"

    def _accounts_for_benefits(self) -> list[Any]:
        """签到/额度遍历的账号列表：**本区全量**，不剔除冷却中的。

        冷却挡的是模型转发（quota/限流退避），签到的 billing 面与它无关；
        按冷却过滤会把在冷却的账号整条从签到卡里藏掉（2026-10-07 用户实报：
        双号只显示一个、「每日」只算一半）。全量枚举后位次天然与账号快照
        一致，display_index 的对位问题也随之消失。
        海外版（region=global）只取本区账号，与 CN 互不混用（#131）。
        """
        return [a for a in creds.list_accounts() if a.region == self._region]

    def _checkin_status_from_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把上游 checkin-activity-status envelope 收拢成统一签到状态。"""
        if payload.get("code") not in (0, None):
            raise RuntimeError(payload.get("msg") or f"code={payload.get('code')}")
        data = payload.get("data") or {}
        active = bool(data.get("active"))
        checked_in = bool(data.get("today_checked_in"))
        status: dict[str, Any] = {
            "checked_in": checked_in,
            "claimable": active and not checked_in,
            "inactive": not active,
            "streak_days": data.get("streak_days") or 0,
            "daily_credit": data.get("today_credit") or data.get("daily_credit") or 0,
            "checkin_dates": data.get("checkin_dates") or [],
            "activity_name": data.get("activity_name") or "",
            "message": payload.get("msg", ""),
        }
        # 上游只给**整个档期**（``start_time``/``end_time``，实测
        # ``2026-09-30 00:00:00`` ~ ``2026-10-15 23:59:59``），没有每日轮换
        # 字段。轮换时刻按 logs/checkin.jsonl 反推的本地零点算，并受档期约束
        # （档期最后一天之后就没有「下次」了）。因为是推断而非上游契约，
        # source 标 inferred，界面会注明。
        if active:
            next_ts = daily_reset_within_season(data.get("end_time"))
            if next_ts is not None:
                status["next_ts"] = next_ts
                status["next_ts_source"] = SOURCE_INFERRED
        else:
            # 档期未开：「下次」就是开打时刻，这个是上游给的、不用推断。
            # 档期已过时 start_time 在过去，自然算不出来（不显示，见 base 契约）。
            opens = parse_upstream_datetime(data.get("start_time"))
            if opens is not None and opens > time.time():
                status["next_ts"] = opens
                status["next_ts_source"] = SOURCE_UPSTREAM
        return status

    def _checkin_status_for_account(
            self, acct: Any, *, index: int, multi: bool) -> tuple[dict[str, Any] | None, str]:
        """单账号签到状态查询。返回 ``(status, error)``：查询失败时 status 为
        None、error 带截断后的原因（进 per-account 明细的 title，光「查询失败」
        说不清是 token 过期还是网络问题）。"""
        try:
            payload = creds.api_post_as(acct.id, "/v2/billing/meter/checkin-activity-status")
            st = self._checkin_status_from_payload(payload)
        except Exception as e:  # noqa: BLE001 — 单账号失败不该让整页 500
            log.warning("codebuddy 签到状态查询失败（%s）: %s", acct.id, e)
            return None, str(e)[:120]
        if multi:
            st["message"] = f"{self._quota_tag} #{index} · {st.get('message') or ''}".strip()
        return st, ""

    def checkin_status(self) -> dict[str, Any] | None:
        """查今日签到状态。多账号**都查**，聚合：任一账号可领→可领；
        全部已签→已签；个别账号失败只在日志记、不阻塞整页（与 trae/qoder 一致）。"""
        accounts = self._accounts_for_benefits()
        if not accounts:
            return {"checked_in": False, "claimable": False, "inactive": False,
                    "message": "暂无已登录账号；先在账号面板登录"}
        if len(accounts) == 1:
            # 单账号直通：不带 accounts 明细（徽标本身就是它的状态，逐账号
            # 展开纯属噪音；与 trae 单账号分支对称）
            st, err = self._checkin_status_for_account(accounts[0], index=1, multi=False)
            if st is None:
                return {"checked_in": False, "claimable": False,
                        "message": err or "查询失败"}
            return st

        # 多账号：逐账号查询（串行即可——管理页轮询频率低，没必要开线程池），
        # 聚合任一可领 / 全部已签；per-account 明细放 ``accounts``（管理页签到卡
        # 逐账号渲染「#N 已签 / 可领 / 查询失败」）。
        claimable_accts: list[str] = []
        signed_accts: list[str] = []
        failed_accts: list[str] = []
        acct_details: list[dict[str, Any]] = []
        enabled_any = False
        total_daily = 0.0  # 「每日 +X」chip 用：多账号显示总和（前端带「（N账号）」）
        # 展示序号一律取 failover.display_index(self._region)（与 /ui 账号
        # 快照同源；区内编号）。accounts 已是本区全量列表、与区内快照位次
        # 一致，但序号仍从同一来源取——别处的子集过滤（历史教训）不能再把
        # 位次对歪。
        didx = failover.display_index(self._region)
        for pos, acct in enumerate(accounts, 1):
            i = didx.get(acct.id, pos)
            # 显示名 alias 优先（管理页 ✎ 改的名）；id 一并下发——明细行的
            # ✎ 改名按钮要拿它定位账号。
            name = acct.alias or acct.nickname or acct.uid or acct.id
            st, err = self._checkin_status_for_account(acct, index=i, multi=True)
            if st is None:
                failed_accts.append(f"#{i}")
                acct_details.append({"index": i, "id": acct.id, "name": name,
                                     "error": err or "查询失败"})
                continue
            if not st.get("inactive"):
                enabled_any = True
            if isinstance(st.get("daily_credit"), (int, float)):
                total_daily += st["daily_credit"]
            if st.get("claimable"):
                claimable_accts.append(f"#{i}")
            elif st.get("checked_in"):
                signed_accts.append(f"#{i}")
            acct_details.append({
                "index": i,
                "id": acct.id,
                "name": name,
                "checked_in": bool(st.get("checked_in")),
                "claimable": bool(st.get("claimable")),
                "inactive": bool(st.get("inactive")),
                "daily_credit": st.get("daily_credit"),
            })
        status: dict[str, Any] = {
            "checked_in": bool(signed_accts) and not claimable_accts,
            "claimable": bool(claimable_accts),
            "inactive": not enabled_any and not claimable_accts and not signed_accts,
            "message": "",
            "accounts": acct_details,
        }
        if claimable_accts:
            status["message"] = f"账号 {'、'.join(claimable_accts)} 可领"
        elif signed_accts:
            status["message"] = f"账号 {'、'.join(signed_accts)} 已签"
        if failed_accts:
            status["message"] = (status["message"] + " " if status["message"] else "") + \
                f"（{len(failed_accts)}/{len(accounts)} 个账号查询失败）"
        if total_daily:
            status["daily_credit"] = total_daily
        if enabled_any:
            status["next_ts"] = next_daily_reset()
            status["next_ts_source"] = SOURCE_INFERRED
        return status

    def checkin_claim(self) -> dict[str, Any] | None:
        """领取今日签到积分。多账号**每个都领**（用户决策），逐个尝试、
        各自容错：单个账号失败（网络/token/已领过）不阻塞其它账号。汇总
        ``extra_credits`` 为各账号实领之和，``message`` 据实说明每个账号结果。
        多账号之间强制间隔 ``_CHECKIN_THROTTLE_S``（上游活动接口对短窗口连打
        有频控，照 trae 同款）。本方法经 benefits 层 ``asyncio.to_thread`` 跑，
        sleep 不阻塞事件循环。
        """
        accounts = self._accounts_for_benefits()
        if not accounts:
            # 枚举不剔除冷却，空列表=真没有账号，别把用户往冷却排查上带
            raise RuntimeError("暂无已登录账号；先在账号面板登录再打卡")
        if len(accounts) == 1:
            # 单账号直通：不带 accounts 明细（与 checkin_status 的单账号分支对称）
            acct = accounts[0]
            payload = creds.api_post_as(acct.id, "/v2/billing/meter/daily-checkin")
            if payload.get("code") not in (0, None):
                raise RuntimeError(payload.get("msg") or f"code={payload.get('code')}")
            data = payload.get("data") or {}
            return {
                "checked_in": True,
                "extra_credits": data.get("credit") or data.get("today_credit") or data.get("daily_credit"),
                "streak_days": data.get("streak_days") or 0,
                "message": payload.get("msg", ""),
            }

        didx = failover.display_index(self._region)
        total_credits: float = 0.0
        any_claimed = False
        messages: list[str] = []
        acct_details: list[dict[str, Any]] = []
        for pos, acct in enumerate(accounts, 1):
            i = didx.get(acct.id, pos)
            name = acct.alias or acct.nickname or acct.uid or acct.id
            tag = f"#{i}"
            # 串行节流：从第 2 个账号起先等一段再打（照 trae 同款——上游活动
            # 接口对短窗口连打有频控）。异常路径同样等：哪怕上一个账号失败，
            # 下一个也照常歇一拍，节奏一致才像「人在操作」。
            if pos > 1:
                time.sleep(_CHECKIN_THROTTLE_S)
            try:
                payload = creds.api_post_as(acct.id, "/v2/billing/meter/daily-checkin")
            except Exception as e:  # noqa: BLE001 — 单账号失败不阻塞其它账号
                log.warning("codebuddy 签到领取失败（%s）: %s", acct.id, e)
                messages.append(f"{tag} 失败：{str(e)[:60]}")
                acct_details.append({"index": i, "id": acct.id, "name": name,
                                     "ok": False, "message": str(e)[:60]})
                continue
            if payload.get("code") not in (0, None):
                msg = str(payload.get("msg") or f"code={payload.get('code')}")[:60]
                messages.append(f"{tag} 失败：{msg}")
                acct_details.append({"index": i, "id": acct.id, "name": name,
                                     "ok": False, "message": msg})
                continue
            data = payload.get("data") or {}
            granted = (data.get("credit") or data.get("today_credit")
                       or data.get("daily_credit") or 0)
            try:
                total_credits += float(granted)
            except (TypeError, ValueError):
                pass
            any_claimed = True
            messages.append(f"{tag} 已领 {granted or ''}".strip())
            acct_details.append({"index": i, "id": acct.id, "name": name,
                                 "ok": True, "credits": granted,
                                 "message": str(payload.get("msg") or "")[:60]})
        return {
            "checked_in": any_claimed,
            "extra_credits": total_credits if any_claimed else None,
            "message": "；".join(messages) or "没有可领取的账号",
            "accounts": acct_details,
        }

    def quota(self) -> dict[str, Any] | None:
        """查询积分资源包汇总（get-user-resource-summary）：每个包给周期额度，
        单位 credits；多账号逐个查，各账号条目带 ``CodeBuddy #N · `` 前缀供前端
        分组；个别账号失败插 ``query_failed`` 说明条（benefits 层认这个标记走
        短缓存）。

        ⚠️ 资源汇总走无前缀路径（/v2 下反而 404），与签到接口的前缀规则相反。
        """
        accounts = self._accounts_for_benefits()
        if not accounts:
            log.warning("codebuddy 额度查询失败: 无本区账号")
            return None
        multi = len(accounts) > 1
        # 展示序号取 failover.display_index(self._region)（与 /ui 账号快照
        # 同源，理由同 checkin_status：额度块按序号对上账号后 ✕ 删除/▲▼ 顺位
        # 拿到的才是同一个账号，删号是不可逆的）。
        didx = failover.display_index(self._region)
        items: list[dict[str, Any]] = []
        failed: list[str] = []
        level = None
        for pos, acct in enumerate(accounts, 1):
            i = didx.get(acct.id, pos)
            try:
                packs, lv = self._quota_one_account(acct)
            except Exception as e:  # noqa: BLE001 — 单账号失败不阻塞整页
                log.warning("codebuddy 额度查询失败（%s）: %s", acct.id, e)
                failed.append(f"#{i}")
                continue
            if level is None:
                level = lv
            prefix = f"{self._quota_tag} #{i} · " if multi else ""
            items.extend({"label": f"{prefix}{p['label']}", **{k: v for k, v in p.items() if k != "label"}}
                         for p in packs)
        if failed:
            items.insert(0, {
                "label": f"{self._quota_tag} 额度查询失败",
                "used": None, "total": None,
                "remaining": f"{len(failed)}/{len(accounts)} 个账号取不到额度"
                             f"（{'、'.join(failed)}）",
                "percent": None, "reset_ts": None,
                "query_failed": True,
            })
        return {"items": items, "sum_items": True, "level": level}

    def _quota_one_account(self, acct: Any) -> tuple[list[dict[str, Any]], str | None]:
        """单账号额度查询：``(packs, level)``。"""
        payload = creds.api_post_as(acct.id, "/billing/meter/get-user-resource-summary")
        if payload.get("code") not in (0, None):
            raise RuntimeError(payload.get("msg") or f"code={payload.get('code')}")
        data = payload.get("data") or {}
        sub_code = data.get("SubscriptionPackageCode") or ""
        expiry = self._package_expiry_map(acct)

        def to_float(v) -> float | None:
            try:
                return round(float(v), 2) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        packs = []
        for p in data.get("Packages") or []:
            used, total, remain = (to_float(p.get("CycleUsedCapacity")),
                                   to_float(p.get("CycleTotalCapacity")),
                                   to_float(p.get("CycleRemainCapacity")))
            if total is None:
                continue
            if used is None:
                used = 0.0
            if remain is None:
                remain = round(total - used, 2)
            # 明细这里**必须遍历全部包**：这个 break 早先写在循环里（原先还
            # 兼着做合计累加），于是一旦包多于 4 个，明细就在断点处结束，且
            # 标题行合计只加了前 4 个——显示的剩余比账号实际少一截，用户从
            # 界面上看不出还有包没算进来。现在合计交给前端（``sum_items``），
            # 但明细照样不能截断：后端砍掉的条目前端无从得知，几个包会被
            # 永久藏起来。
            #
            # 明细这里**不再截断**（原先 ``if len(packs) < 4``）：后端砍掉的
            # 条目前端无从得知，几个包被永久藏起来。展示条数交给前端折叠
            # （``benefits.js`` 的 quotaItemsHtml：只铺没花完的 + 超限收起），
            # 那里有展开入口、用户想看能看全；后端只要如实给数据。
            packs.append({
                "label": "订阅套餐" if p.get("PackageCode") == sub_code else "资源包",
                "used": used, "total": total, "remaining": remain,
                "percent": round(used / total * 100) if total else 0,
                # 上游 ``Packages[]`` 只有周期容量字段（CycleUsed/Total/
                # Remain/CapacityUnit），没有任何日期字段（2026-10-03 实测）。
                # 有效期（expire_ts）改从 ``get-user-resource`` 逐包聚合而来
                # （见 :meth:`_package_expiry_map`）；聚合接口失败时拿不到就
                # 恒 None，条目照出——有效期是补充信息，不能把它变成整卡
                # 失败的理由。重置时间依然无从谈起（周期按发放时刻滚动，
                # 各包互不相同，单一 reset_ts 表达不了）。
                "reset_ts": None,
                "expire_ts": expiry.get(p.get("PackageCode") or ""),
                "unit": "credit",
            })
        # 不再造一条「积分余额合计」明细：它是标题行的信息（前端 quotaHeadSum
        # 已能用 ``sum_items`` 把各包加总出来），多铺一行明细反而与其它通道
        # 不一致（用户 2026-10-03：「其它的都没有」）。声明 sum_items 让前端
        # 自己合计，明细就只剩真正的各资源包。
        return packs, ("pro" if data.get("IsPaidUser") else "free")

    #: ``get-user-resource`` 单页最大条数（裂变包一日一实例，41+ 条是常态，
    #: 两页拿全即可；超出按 TotalCount 继续翻）。
    _RESOURCE_PAGE_SIZE = 100
    #: 资源实例翻页的硬上限：超过按截断处理（有效期取已见实例的），不无限翻。
    _RESOURCE_MAX_ITEMS = 1000

    def _package_expiry_map(self, acct: Any) -> dict[str, int]:
        """逐包的资源包有效期（epoch 秒），键是 PackageCode；拿不到返回空表。

        汇总接口（get-user-resource-summary）没有日期字段，有效期要从
        ``/billing/meter/get-user-resource``（资源实例列表，WorkBuddy web
        「我的资源」同源，2026-10-23 实测 200）聚合：

        - 每条实例是一个订单周期（裂变包一天一条），带 ``CycleEndTime``
          （本周期作废时刻）与 ``Status``（0=生效中，3=已耗尽/已结束）。
        - 上游 summary 的 CycleTotal/Remain = **全部实例**（含已耗尽）的
          周期容量之和（实测对账一致），所以明细里那份额度是「账号当前的
          总盘子」。用户要的有效期是「现在还能用的这份额度到哪天作废」，
          口径取**生效中（Status=0）实例里最晚的 CycleEndTime**——它对
          订阅包等于月结周期结束、对裂变包等于最近一次发放的窗口结束。
          没有生效实例时退全实例最晚值（包还在盘子里，只是当前周期用完，
          界面照样该有个日期）。
        - ``ExpiredTime``（发放后固定过期）与这里无关：已耗尽实例的它
          全在过去，不解释当前余量还能用多久，不采用。
        - 任何失败（404/风控/形状变了）都吞成空表：summary 主数据照常
          出条目，只是没有「N 天后到期」。这是**第二数据源**，不能让它
          有把整卡额度打挂的能力。
        """
        try:
            expiry: dict[str, int] = {}
            all_ends: dict[str, list[int]] = {}
            offset = 0
            while offset < self._RESOURCE_MAX_ITEMS:
                payload = creds.api_post_as(
                    acct.id, "/billing/meter/get-user-resource",
                    {"Offset": offset, "Limit": self._RESOURCE_PAGE_SIZE})
                if payload.get("code") not in (0, None):
                    log.warning("codebuddy 资源实例查询失败（%s）: %s",
                                acct.id, payload.get("msg") or payload.get("code"))
                    return {}
                # 包门面 _unwrap 过的外层是 {data: Response.Data.Accounts}，
                # api_post_as 拿的是未解包 envelope，同 summary 的取法。
                resp = ((payload.get("data") or {}).get("Response") or {})
                d = resp.get("Data") or resp
                rows = d.get("Accounts") or []
                total = d.get("TotalCount") or 0
                for it in rows:
                    if not isinstance(it, dict):
                        continue
                    code = it.get("PackageCode") or ""
                    if not code:
                        continue
                    end = parse_upstream_datetime(it.get("CycleEndTime"))
                    if end is None:
                        continue
                    if it.get("Status") == 0:
                        cur = expiry.get(code)
                        if cur is None or end > cur:
                            expiry[code] = end
                    all_ends.setdefault(code, []).append(end)
                offset += len(rows)
                if not rows or offset >= total:
                    break
            # 没有生效实例的包退全实例最晚周期（见上）；全空的丢弃。
            for code, ends in all_ends.items():
                if code not in expiry and ends:
                    expiry[code] = max(ends)
            return expiry
        except Exception as e:  # noqa: BLE001 — 第二数据源失败不阻塞额度卡
            log.warning("codebuddy 资源包有效期查询失败（%s）: %s", acct.id, e)
            return {}

    def quota_epoch(self) -> str:
        """quota 缓存代：账号列表一变（登录新号/删号/换顺位）旧快照就该作废。"""
        try:
            accts = creds.list_accounts()
        except Exception:  # noqa: BLE001 - 拿不到就退回常量键
            return "unknown"
        return ",".join(f"{a.id}#{a.priority}" for a in accts) or "empty"

    # ---- 计费流水（WorkBuddy web「使用记录」同源接口，2026-09-06 实测） ----

    def usage_records(
        self,
        start: str = "",
        end: str = "",
        page_num: int = 1,
        page_size: int = 20,
        account_id: str = "",
    ) -> dict[str, Any]:
        """按请求粒度的积分消耗流水（/billing/meter/get-user-request-usage）。

        端点来自 WorkBuddy web（/profile/plans-usage 页 XHR 反查）。与签到/
        资源汇总同族：走 IDE 插件 Bearer 认证即可，**无需网页 cookie**；
        copilot.tencent.com 与 www.workbuddy.cn 同路径均实测 200。

        记录字段对应网页「使用记录」列：credit=实扣积分、model、client、
        requestTime、input（prompt 原文/截断）、requestId（crb-…）。
        start/end 格式 "YYYY-MM-DD HH:MM:SS"，缺省为最近 7 天。

        多账号：``account_id`` 给出时查指定账号；缺省查 failover 首账号
        （暂无 UI 消费方，管理接口 curl 即可查，暂不做跨账号聚合）。

        用途预留：管理页消费流水视图；或按 requestTime/model 把流水 credit
        回填 metrics，对上游 usage 缺失实扣的请求做对账。
        """
        if not account_id:
            accounts = creds.list_accounts()
            if not accounts:
                raise RuntimeError("codebuddy 没有任何账号（先 `buddy login codebuddy`）")
            account_id = accounts[0].id
        if not end:
            end = time.strftime("%Y-%m-%d %H:%M:%S")
        if not start:
            start = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(time.time() - 7 * 86400))
        body = {
            "startTime": start,
            "endTime": end,
            "pageNum": max(1, int(page_num)),
            "pageSize": min(100, max(1, int(page_size))),
        }
        payload = creds.api_post_as(account_id, "/billing/meter/get-user-request-usage", body)
        if payload.get("code") not in (0, None):
            raise RuntimeError(payload.get("msg") or f"code={payload.get('code')}")
        data = payload.get("data") or {}
        records = []
        for r in data.get("data") or []:
            if not isinstance(r, dict):
                continue
            records.append({
                "request_id": r.get("requestId"),
                "credit": r.get("credit"),
                "model": r.get("model"),
                "client": r.get("client"),
                "request_time": r.get("requestTime"),
                "input": (r.get("inputTrunc") or r.get("input") or "")[:200],
            })
        return {"total": data.get("total") or len(records), "records": records}

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        diagnostic("upstream_request", protocol=protocol, **body_summary(body))

        stream = bool(body.get("stream"))
        upstream_body = dict(body)

        # 归一化 tool_choice：object 形式 → 函数名字符串（上游只接受 string）
        if "tool_choice" in upstream_body:
            upstream_body["tool_choice"] = _normalize_tool_choice(upstream_body["tool_choice"])

        # 海外版风控：messages 第一条必须是 system，否则 11128 整单被拦
        # （2026-10-15 实测，仅 www.codebuddy.ai）。CN 不碰。
        if self._region == "global" and "messages" in upstream_body:
            upstream_body["messages"] = _ensure_leading_system(upstream_body["messages"])

        # 应用脱敏处理（脱敏开关是全局 state 的，与账号无关，循环外做一次）
        state = get_state()
        if state.enable_desensitize:
            upstream_body = desensitize_body(upstream_body, compact_harness=True)

        # 始终以流式方式请求上游（聚合或转发）
        upstream_body["stream"] = True
        upstream_body.setdefault("stream_options", {"include_usage": True})

        # ---- 多账号 failover：按顺位试可用账号，账号级错误冷却换号 ----
        accounts = failover.available_accounts(self._region)
        if not accounts:
            hint = "" if self._region == "cn" else "（海外版）"
            raise HTTPException(
                status_code=429,
                detail={"error": {
                    "message": (f"codebuddy{hint} 所有账号均在冷却中：{failover.cooldown_report()}"
                                "；额度冷却到点自动恢复"),
                    "type": "rate_limit_error"}})
        last_exc: BaseException | None = None
        try:
            for i, acct in enumerate(accounts):
                try:
                    return await self._forward_once(
                        upstream_body, protocol, original, acct.id, stream)
                except HTTPException as e:
                    last_exc = e
                    # 账号级错误（401 凭据失效 / 429 额度，如 code 14018）且还有
                    # 下一个账号：冷却换号；其余错误（502 通道级/业务 4xx）换号
                    # 无意义，直接透传。
                    if _is_account_error(e) and i < len(accounts) - 1:
                        failover.mark_cooldown(
                            acct.id, quota=e.status_code == 429,
                            reason=f"HTTP {e.status_code}: {str(e.detail)[:120]}")
                        continue
                    raise
        except HTTPException as e:
            # 走到这是「最后一个账号也失败」或「非账号级错误透传」。anthropic 协议
            # 的错误体用标准形状（Claude Code 等 SDK 靠它渲染错误）；openai 协议
            # 维持「抛 HTTPException → 外层 exception handler 转 JSON」不变。
            if protocol == "anthropic":
                return JSONResponse(
                    status_code=e.status_code,
                    content={
                        "type": "error",
                        "error": {"type": "api_error", "message": str(e.detail)},
                    },
                )
            raise
        # 全部账号失败（循环正常跑完不该到这，兜底）
        raise last_exc or HTTPException(status_code=429, detail=failover.cooldown_report())

    async def _forward_once(
        self,
        upstream_body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None,
        account_id: str,
        stream: bool,
    ) -> StreamingResponse | JSONResponse:
        """单账号实际转发（forward 的 failover 循环体）。"""
        # token 与 cred 同源拿取（headers 需要 uid/enterprise_id/machine_id 等
        # 账号级身份）；刷新是同步 httpx，放线程池避免阻塞事件循环。
        try:
            token, cred = await asyncio.to_thread(creds.ensure_account_token, account_id)
        except creds.AuthError as exc:
            raise HTTPException(
                status_code=401,
                detail={"error": {"message": f"codebuddy 账号 {account_id} 凭据不可用: {exc}",
                                  "type": "authentication_error"}}) from exc
        # metrics 账号归属（observability._instrument 预置的 holder dict；
        # 直接 forward 的调用方可能没有，判空跳过）
        meta = ACCOUNT_META.get()
        if isinstance(meta, dict):
            meta["account"] = account_id

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; Genie-IDE/1.0)",
            **creds.auth_headers_from_cred({**cred, "token": token}),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        url = creds.endpoint(str(cred.get("region") or "cn")) + "/v2/chat/completions"

        # 🔍 调试：输出实际发送的IDE识别headers
        state = get_state()
        if state.logger:
            ide_headers = {k: v for k, v in headers.items()
                           if k.startswith("X-IDE-") or k == "X-Product-Version" or k == "X-Machine-Id"}
            diagnostic("upstream_ide_headers", account=account_id, **ide_headers)

        if stream:
            # 流式：直接转发。经包命名空间延迟解析而非模块顶层导入：测试以
            # monkeypatch.setattr(codebuddy_provider, "stream_upstream", ...) 打
            # 补丁时（见 test_endpoints_smoke），这里必须取到补丁后的对象。
            from buddy_proxy.codebuddy_provider import stream_upstream

            raw_gen = stream_upstream(url, headers, upstream_body, protocol, original)
            # 首事件闸门：预驱动到第一个 yield 再决定放行还是换号。
            # stream_upstream 撞非 200 时**首个 yield 就是错误帧**（吐完即
            # return）——此刻 StreamingResponse 还没建、客户端一个字节都没收到，
            # 把错误帧还原成 HTTPException 抛回 failover 循环可安全换号（防假
            # 成功：若直接放行，下游收到的是 200 SSE + error 事件，且换号机会
            # 就此丢失）。见到正常首块即 committed：缓冲重放 + 续跑，绝不重试。
            try:
                first = await raw_gen.__anext__()
            except StopAsyncIteration:
                first = None
            if first is not None:
                err_exc = _error_frame_to_exception(first, protocol)
                if err_exc is not None:
                    await raw_gen.aclose()
                    raise err_exc

            async def _replay():
                if first is not None:
                    yield first
                async for chunk in raw_gen:
                    yield chunk

            return StreamingResponse(
                _replay(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "close"},
            )
        else:
            # 非流式：聚合后返回（延迟解析理由同上）。collect_upstream 撞非 200
            # 在返回前 raise HTTPException(status_code=…)，failover 循环接得住，
            # 这里**必须让它 raise**、不能吞成 JSONResponse——否则撞错的账号
            # 不会被冷却、也不会换下一个账号。
            from buddy_proxy.codebuddy_provider import collect_upstream, convert_nonstream

            collected = await collect_upstream(url, headers, upstream_body, protocol)
            return JSONResponse(content=convert_nonstream(collected, protocol, original))


# 默认 CodeBuddy provider 单例（供 forward_chat 默认路径调用）。
# 定义在 CodeBuddyProvider 类之后，实例化安全。
_default_codebuddy = CodeBuddyProvider()
