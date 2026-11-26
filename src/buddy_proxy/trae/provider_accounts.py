"""Trae Work account selection for check-in and quota administration."""

from __future__ import annotations

from typing import Any, Sequence


class AccountsMixin:
    def _work_accounts(self) -> list[Any]:
        """遍历用的 work 账号列表（按 failover 顺位，剔除冷却中的）。

        PAT 子类（``_pat_variant``）不经过 work 多账号体系——签到/额度是
        个人账号专属，PAT 的 ``supports_checkin=False`` 本就挡住 UI 调度，
        这里再兜一层：PAT 返回空列表，调用方各自退化为单账号（拿首个可用
        work 账号）或返回 None（与改造前 PAT 行为一致）。
        """
        from . import provider as hooks
        if self._is_pat_variant():
            return []
        # 只遍历本区账号——CN / 海外账号不通用（连错域 401），额度与签到请求
        # 打错区就是白付一次失败往返，还会把海外的美元 Usage 混进 CN 的积分卡。
        # PAT 子类的 region 恒为 cn（见 pat/chat.py 注释），过滤对它无影响。
        return hooks.failover.available_accounts(self._region.key)

    def _checkin_accounts(self) -> list[Any]:
        """签到遍历的账号列表：与 :meth:`_work_accounts` 唯一差别是**不剔除冷却**。

        冷却挡的是模型转发（quota/限流退避），签到的 billing 面与它无关；
        按冷却过滤会把在冷却的账号整条从签到卡里藏掉（2026-10-07 用户实报
        codebuddy 同病：双号只显示一个、「每日」只算一半）。同区过滤保留
        （CN/海外账号不通用）；PAT 子类照旧走各自的 checkin 覆写，不经这里。
        """
        from . import provider as hooks
        if self._is_pat_variant():
            return []
        region = self._region.key
        return [a for a in hooks.list_accounts() if getattr(a, "region", None) == region]

    def checkin_status(self) -> dict[str, Any] | None:
        """查今日签到状态。多账号**都查**，聚合：任一账号可领→可领；
        全部已签→已签；个别账号失败只在日志记、不阻塞整页（与 qoder 一致）。"""
        from . import provider as hooks
        accounts = self._checkin_accounts()
        if not accounts:
            # 单账号（legacy 迁移前 / PAT 兜底）：走首个可用账号
            try:
                data = hooks.fetch_checkin_status(region=self._region.key)
            except Exception as e:  # noqa: BLE001 — 状态查询失败不该让整页 500
                hooks.log.warning("trae 签到状态查询失败: %s", e)
                return {"checked_in": False, "claimable": False, "message": str(e)[:200]}
            return self._checkin_status_one(data, label="", multi=False)

        if len(accounts) == 1:
            st, _err = self._checkin_status_for_account(accounts[0], index=1, multi=False)
            return st

        # 多账号：并查，聚合任一可领 / 全部已签；per-account 明细放 ``accounts``
        # （管理页签到卡逐账号渲染「#N 已签 / 可领 / 查询失败」）。
        claimable_accts: list[str] = []
        signed_accts: list[str] = []
        failed_accts: list[str] = []
        acct_details: list[dict[str, Any]] = []
        enabled_any = False
        total_daily = 0.0  # 「每日 +X」chip 用：多账号显示总和（前端带「（N账号）」）
        # 展示序号一律取 failover.display_index()（与 /ui 账号快照同源）。
        # 不能用 enumerate 的位置：accounts 是「本区 + 未冷却」子集，位次与
        # 快照对不上，前端会把签到行对到别的账号上（✎ 改名改错人）。
        didx = hooks.failover.display_index()
        for pos, acct in enumerate(accounts, 1):
            i = didx.get(acct.id, pos)
            # 显示名 alias 优先（管理页 ✎ 改的名，签到卡与额度面板保持一致）；
            # id 一并下发——签到明细行的 ✎ 改名按钮要拿它定位账号。
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
            status["next_ts"] = hooks.next_daily_reset()
            status["next_ts_source"] = hooks.SOURCE_INFERRED
        return status

    def _checkin_status_for_account(
            self, acct: Any, *, index: int, multi: bool) -> tuple[dict[str, Any] | None, str]:
        """单账号签到状态查询。返回 ``(status, error)``：查询失败时 status 为
        None、error 带截断后的原因（进 per-account 明细的 title，光「查询失败」
        说不清是 token 过期还是网络问题）。"""
        from . import provider as hooks
        try:
            token, _cred = hooks.ensure_account_token(acct.id)
            data = hooks.fetch_checkin_status(token=token, account_id=acct.id,
                                       region=self._region.key)
        except Exception as e:  # noqa: BLE001 — 单账号失败不该让整页 500
            hooks.log.warning("trae 签到状态查询失败（%s）: %s", acct.id, e)
            return None, str(e)[:120]
        label = f"{self._quota_tag} #{index} · " if multi else ""
        return self._checkin_status_one(data, label=label, multi=multi), ""

    def _checkin_status_one(
            self, data: dict[str, Any], *, label: str, multi: bool) -> dict[str, Any]:
        """把上游 ``/ug/checkin_credits/status`` 原始响应收拢成统一签到状态。"""
        from . import provider as hooks
        checked_in = bool(data.get("checked_in"))
        enabled = bool(data.get("enable", True))
        status: dict[str, Any] = {
            "checked_in": checked_in,
            "claimable": enabled and not checked_in,
            "inactive": not enabled,
            "message": data.get("message", ""),
            # 每日可得 = credits + extra_credits（语义实测 2026-10-07：双账号
            # 卡用户期望日入 400 → 每账号 200 = 两字段各 100 的和；单看哪个
            # 都只有一半）。若哪天 chip 数字对不上账，先怀疑上游改了字段口径。
            "daily_credit": (data.get("credits") or 0) + (data.get("extra_credits") or 0),
        }
        # trae 的 ``/ug/checkin_credits/status`` 返回里**没有任何时间字段**
        # （实测只有 checked_in / enable / credits / message），连档期都不给。
        # 轮换时刻只能按 logs/checkin.jsonl 反推的本地零点算（09-27 01:56、
        # 02:20 这类凌晨领取也被记为新一天 → 零点轮换），故标 inferred。
        # 活动未开（enable=false）时不给：那会儿连有没有下一轮都不知道。
        if enabled:
            status["next_ts"] = hooks.next_daily_reset()
            status["next_ts_source"] = hooks.SOURCE_INFERRED
        return status

    def _fallback_daily_credit(self, token: str, account_id: str) -> float | None:
        """claim 响应不带金额（如上游省略字段或 replay）时，从 status 接口兜底读取今日签到积分。"""
        from . import provider as hooks
        try:
            st = hooks.fetch_checkin_status(token=token, account_id=account_id,
                                      region=self._region.key)
        except Exception:  # noqa: BLE001 — 兜底查询失败不影响签到主结果
            return None
        val = (st.get("credits") or 0) + (st.get("extra_credits") or 0)
        return float(val) if isinstance(val, (int, float)) and val > 0 else None

    def checkin_claim(self) -> dict[str, Any] | None:
        """领取今日签到积分。多账号**每个都领**（用户决策），逐个尝试、
        各自容错：单个账号失败（网络/token/已领过）不阻塞其它账号。汇总
        ``extra_credits`` 为各账号实领之和，``message`` 据实说明每个账号结果。

        多账号**串行 + 节流**（用户 2026-10-06 要求）：实测双账号背靠背连打，
        第二个必中「当前参与用户太多，请稍后再试」——上游把短窗口内的连续
        claim 识别为刷量限流。账号间强制间隔 ``_CHECKIN_THROTTLE_S``，命中
        限流文案再按 ``_CHECKIN_RETRY_DELAYS`` 退避重试。本方法经 benefits 层
        ``asyncio.to_thread`` 跑（``_call``），``time.sleep`` 不阻塞事件循环；
        手动点击的 HTTP 响应最坏多等约 15s（两次重试），前端 fetch 无超时，可接受。
        """
        from . import provider as hooks
        accounts = self._checkin_accounts()
        if not accounts:
            # 单账号 legacy 兜底（PAT 由 supports_checkin=False 挡住 UI 调度，
            # 走不到这里；即便走到也退回原单账号语义）
            data = hooks.claim_checkin_credits(region=self._region.key)
            if data.get("code") not in (0, None):
                raise RuntimeError(data.get("message") or hooks.json.dumps(data, ensure_ascii=False)[:200])
            return {
                "checked_in": True,
                "extra_credits": data.get("credits_granted", data.get("extra_credits")),
                "message": data.get("message", ""),
            }
        if len(accounts) == 1:
            # 单账号直通：不带 accounts 明细（与 checkin_status 的单账号分支对称，
            # 徽标本身就是它的状态，逐账号展开纯属噪音）
            acct = accounts[0]
            try:
                token, _cred = hooks.ensure_account_token(acct.id)
                data = hooks.claim_checkin_credits(token=token, account_id=acct.id,
                                            region=self._region.key)
            except Exception as e:  # noqa: BLE001 — 与多账号循环同款容错
                hooks.log.warning("trae 签到领取失败（%s）: %s", acct.id, e)
                raise RuntimeError(str(e)[:200]) from e
            if data.get("code") not in (0, None):
                raise RuntimeError(data.get("message") or hooks.json.dumps(data, ensure_ascii=False)[:200])
            granted = data.get("credits_granted", data.get("extra_credits"))
            if granted is None:
                granted = self._fallback_daily_credit(token, acct.id)
            return {
                "checked_in": True,
                "extra_credits": granted,
                "message": data.get("message", ""),
            }

        total_credits: float = 0.0
        any_claimed = False
        any_granted = False  # replay（已领过）拿不到金额：别把「未知」记成 0
        messages: list[str] = []
        acct_details: list[dict[str, Any]] = []
        # 展示序号取 failover.display_index()（与 /ui 账号快照同源，理由同
        # checkin_status）；**节流**用的是本轮循环位置 pos，两者必须分开：
        # 展示序号是「这账号在管理页排第几」（跨区、含冷却账号都占位），
        # 节流要的是「是不是本轮第一个打的」——拿展示序号判会把「快照里的
        # 第 3 个、但本轮第 1 个」也睡一拍（无谓等待），更糟的是若某轮只领
        # 快照第 2、3 号，`i > 1` 对两者都成立，第 1 个也白睡。
        didx = hooks.failover.display_index()
        for pos, acct in enumerate(accounts, 1):
            i = didx.get(acct.id, pos)
            # 显示名 alias 优先（同 checkin_status）；id 下发供明细行 ✎ 定位
            name = acct.alias or acct.nickname or acct.uid or acct.id
            tag = f"#{i}"
            # 串行节流：从第 2 个账号起先等一段再打（实测零间隔必中频控——
            # 见 _CHECKIN_THROTTLE_S 注释）。异常路径同样等：哪怕上一个账号
            # 是网络失败，下一个也照常歇一拍，节奏一致才像「人在操作」。
            if pos > 1:
                hooks.time.sleep(hooks._CHECKIN_THROTTLE_S)
            msg = ""
            ok = False
            granted: float | None = None
            last_rl = False
            device_rejected = False  # 本账号最终是不是指纹池尽（见失败分支）
            for attempt in range(len(hooks._CHECKIN_RETRY_DELAYS) + 1):
                last_rl = False
                try:
                    token, _cred = hooks.ensure_account_token(acct.id)
                    data = hooks.claim_checkin_credits(token=token, account_id=acct.id,
                                                region=self._region.key)
                except Exception as e:  # noqa: BLE001 — 单账号失败不阻塞其它账号
                    hooks.log.warning("trae 签到领取失败（%s）: %s", acct.id, e)
                    msg = str(e)[:60]
                    last_rl = hooks._claim_rate_limited(None, msg)
                    if last_rl and attempt < len(hooks._CHECKIN_RETRY_DELAYS):
                        hooks.time.sleep(hooks._CHECKIN_RETRY_DELAYS[attempt])
                        continue
                    break
                if data.get("code") in (0, None):
                    ok = True
                    g = data.get("credits_granted", data.get("extra_credits"))
                    if isinstance(g, (int, float)):
                        granted = float(g)
                    else:
                        granted = self._fallback_daily_credit(token, acct.id)
                    msg = str(data.get("message") or "")[:80]
                    break
                msg = str(data.get("message"))[:60]
                # 9074 池尽（device_rejected）= 指纹黑名单，换机都没救回来，
                # 5/10s 退避后再来也是同样的 4 台设备——按最终失败处理
                device_rejected = bool(data.get("device_rejected"))
                last_rl = (not device_rejected) and hooks._claim_rate_limited(data)
                if last_rl and attempt < len(hooks._CHECKIN_RETRY_DELAYS):
                    hooks.log.info("trae 签到限流（%s），%.0fs 后重试 %d/%d",
                             acct.id, hooks._CHECKIN_RETRY_DELAYS[attempt],
                             attempt + 1, len(hooks._CHECKIN_RETRY_DELAYS))
                    hooks.time.sleep(hooks._CHECKIN_RETRY_DELAYS[attempt])
                    continue
                break
            if ok:
                any_claimed = True
                if granted is not None:
                    total_credits += granted
                    any_granted = True
                messages.append(f"{tag} 已领 {granted or ''}".strip())
                acct_details.append({
                    "index": i, "id": acct.id, "name": name, "ok": True,
                    "credits": granted,
                    "message": msg,
                })
            else:
                # 重试耗尽仍限流：说明「稍后再试」——别写死「失败」，
                # message 保持上游原话，用户稍后手动再点一次即可补上。
                # 指纹池尽（device_rejected）则必须翻译：上游原话是「当前参与
                # 用户太多」的烟幕，照搬会把人带去查限流——2026-10-06 实锤真因
                # 是设备指纹黑名单（换满 4 台备选机都救不回来），告诉用户真实
                # 原因和自愈方式（稍后自动重试/查 devices.json）才对。
                if device_rejected:
                    msg = ("设备指纹被上游拒（已自动换 4 台设备仍失败），"
                           "稍后自动重试；持续失败删除 ~/.buddy-proxy/trae/"
                           "devices.json 对应条目重派")
                messages.append(f"{tag} 失败：{msg}")
                acct_details.append({"index": i, "id": acct.id, "name": name,
                                     "ok": False, "message": msg})
        return {
            "checked_in": any_claimed,
            "extra_credits": total_credits if any_granted else None,
            "message": "；".join(messages) or "没有可领取的账号",
            "accounts": acct_details,
        }

    def quota(self) -> dict[str, Any] | None:
        """查额度（``/ug/usage``），多账号并发。多账号时各账号条目带
        ``Trae #N · `` 前缀供前端分组；个别账号失败插 ``query_failed``
        说明条（benefits 层认这个标记走短缓存）。"""
        from . import provider as hooks
        # 遍历**本区全量**账号（与签到同口径），不剔除冷却中的：冷却挡的是
        # 模型转发（quota/限流退避），与额度查询的 billing 面无关。走
        # available_accounts 会把在冷却的账号整条从额度面板藏掉——2026-10-11
        # 用户实报：3 个 cn 号、2 个挂 quota 冷却，面板只剩 1 张卡。#130 修
        # 签到时漏了这条路径（当时注释「额度/转发路径不动」，转发该不动、
        # 额度不该）。display_index 的编号对全量位次天然一致，不引入错位。
        accounts = self._checkin_accounts()
        if not accounts:
            # 单账号 legacy 兜底（迁移前 / PAT）：保持原行为
            try:
                data = hooks.fetch_ent_usage(region=self._region.key)
            except Exception as e:  # noqa: BLE001 — 额度查询失败不阻塞整页
                hooks.log.warning("trae 额度查询失败: %s", e)
                return None
            items = self._quota_items(data, label_prefix="")
            return {"items": items, "level": None}

        multi = len(accounts) > 1
        # 展示序号取 failover.display_index()（与 /ui 账号快照同源）。这里曾经
        # 是 enumerate 的位置，失败说明条里还另用过 ``priority + 1``——三套编号
        # 各说各话：额度块按快照序号对上账号后，✕ 删除 / ▲▼ 顺位 / ✎ 改名拿到的
        # 是**别的账号**的 id，删号是不可逆的（凭据文件一并 unlink）。
        # display_index 用全量列表位次，快照与标签必然同源。
        didx = hooks.failover.display_index()
        if multi:
            pool = hooks._quota_executor()
            futures = [pool.submit(self._quota_one, a, didx.get(a.id, n + 1), multi=True)
                       for n, a in enumerate(accounts)]
            deadline = hooks.time.monotonic() + hooks._QUOTA_ROUND_DEADLINE_S
            items: list[dict[str, Any]] = []
            failed: list[str] = []
            for acct, fut in zip(accounts, futures):  # 按 failover 顺位收集，UI 顺序稳定
                name = f"#{didx.get(acct.id, 0) or '?'}"
                try:
                    its, ok = fut.result(timeout=max(deadline - hooks.time.monotonic(), 0.05))
                except Exception:  # noqa: BLE001 - 超时/异常账号都算失败
                    its, ok = [], False
                if not ok:
                    failed.append(name)
                else:
                    items.extend(its)
            if failed:
                items.insert(0, {
                    "label": "Trae 额度查询失败",
                    "used": None, "total": None,
                    "remaining": f"{len(failed)}/{len(accounts)} 个账号取不到额度"
                                 f"（{'、'.join(failed)}）",
                    "percent": None, "reset_ts": None,
                    "query_failed": True,
                })
        else:
            its, ok = self._quota_one(accounts[0], didx.get(accounts[0].id, 1), multi=False)
            items = its if ok else []

        return {"items": items, "level": None}

    def _quota_account_ids(self) -> dict[str, str]:
        """额度标签序号（``#N``，字符串）→ 账号 id，供负载均衡把权重落到人头上。

        与 :meth:`quota` 同源（``_checkin_accounts()``，本区全量、不剔除冷却）。
        """
        from . import provider as hooks
        try:
            accounts = self._checkin_accounts()
        except Exception:  # noqa: BLE001
            return {}
        return {str(i): str(getattr(a, "id", "") or "")
                for i, a in enumerate(accounts or (), start=1) if getattr(a, "id", "")}

    def quota_epoch(self) -> str:
        """quota 缓存代：账号列表一变（登录新号/删号/换顺位）旧快照就该作废。"""
        from . import provider as hooks
        return hooks.quota_epoch_from(hooks.list_accounts)

    def _quota_one(self, acct: Any, index: int, *, multi: bool
                   ) -> tuple[list[dict[str, Any]], bool]:
        """单账号额度查询：``(items, ok)``。同步跑在常驻线程池里。"""
        from . import provider as hooks
        try:
            token, _cred = hooks.ensure_account_token(acct.id)
            data = hooks.fetch_ent_usage(token=token, account_id=acct.id,
                                  region=self._region.key)
        except Exception as e:  # noqa: BLE001 — 单账号失败不阻塞整页
            hooks.log.warning("trae 额度查询失败（%s）: %s", acct.id, e)
            return [], False
        prefix = f"{self._quota_tag} #{index} · " if multi else ""
        return self._quota_items(data, label_prefix=prefix), True

    def _quota_items(self, data: dict[str, Any], *, label_prefix: str) -> list[dict[str, Any]]:
        """把上游 ``/ug/usage`` 原始响应收拢成统一额度条目列表。

        **计费口径分叉**：CN 是积分制（``unit="credit"``），海外是美元 Usage
        余额制（``unit="dollar"``）。判据用**响应自带的** ``is_dollar_usage_billing``
        flag，而不是 ``self._region.billing``——两区共用同一个
        ``ide_user_ent_usage`` 接口，flag 才是上游对「这份额度按什么计价」的
        权威声明；区域只作 flag 缺失时的兜底（老快照/字段改名都不至于把美元
        报成积分）。数字字段两侧同名（``total_amount``/``consumed_amount``/
        ``credits_limit``/``credits_amount``），所以只换量纲标签与展示单位。

        量纲标签不能沿用 ``credit``：``benefits._quota_low`` 对 credit 通道用
        **绝对值** 300 门槛判「余额告急」，$20 的海外套餐会被天天误报；改成
        ``dollar`` 后它走非 credit 分支，按剩余占比判定（与 day/count/permille
        同款），才是对的口径。
        """
        from . import provider as hooks
        dollar = bool(data.get("is_dollar_usage_billing")) or (
            not data.get("is_credits_billing") and self._region.billing == "dollar")
        unit = "dollar" if dollar else "credit"
        us = data.get("usage_summary", {})
        items: list[dict[str, Any]] = []
        total, consumed = us.get("total_amount"), us.get("consumed_amount")
        if total is not None:
            ratio = us.get("consumption_ratio")
            percent = round(ratio * 100) if isinstance(ratio, (int, float)) else None
            remaining = None
            if isinstance(total, (int, float)) and isinstance(consumed, (int, float)):
                remaining = round(total - consumed, 2)
            items.append({"label": f"{label_prefix}总额度", "used": consumed, "total": total,
                          "remaining": remaining, "percent": percent,
                          # 总额度是所有包的合计，没有单一到期日——到期告警
                          # 由下面各权益包自己承担，合计行不参与
                          "reset_ts": None, "expire_ts": None, "unit": unit,
                          # head_only：只在标题行「剩 X / Y」用它的数字，明细
                          # 列表不单列这一条（它是下面各权益包的合计，再铺一条
                          # 带进度条的明细行是重复——用户 2026-10-04 反馈）。
                          "head_only": True})
        # 权益包：每条都有额度与到期时间，全部展示（各条**不能相加**——它们
        # 是上面「总额度」的明细，加了就重复计算，故本通道不给 sum_items）。
        packs: list[dict[str, Any]] = []
        seen: set[tuple] = set()
        for p in data.get("user_entitlement_pack_list", []):
            eb = p.get("entitlement_base_info") or {}
            end_time = eb.get("end_time")
            if not end_time:
                continue
            desc = p.get("display_desc") or "权益包"
            quota = eb.get("quota") or {}
            # 去重按「名字 + 权益 id + 到期日」三元组，只防上游返回重复行。
            # **不能**只按名字去重：那样 25 条「签到奖励」会被合并成 1 条，
            # 12 条尚未消费的额度直接从界面上消失（2026-10-03 实测：升级前
            # 只显示前 3 条，其余 24 个包全被 `desc in seen` 加 `len>=3` 截掉）。
            key = (desc, str(eb.get("entitlement_id") or ""), int(end_time))
            if key in seen:
                continue
            seen.add(key)
            used: float | None = None
            limit_v: float | None = None
            rows: list[tuple[str, float | None, float | None, str]] = []
            if dollar and ("premium_model_fast_request_limit" in quota
                           or "basic_usage_limit" in quota):
                if p.get("is_hide"):
                    # 海外用 is_hide 标记「UI 不展示」的包（实测：Promo Code
                    # 包全 0 配额仍会出现在列表里）。只在海外结构下过滤——
                    # CN 快照没有这个字段，别让新过滤改变 CN 行为。
                    continue
                # 海外实测结构（2026-10-06 Pro plan 快照；2026-10-25 用户确认
                # **没有「Premium 快速请求」这种额度**，只有 Basic 用量）：
                # ``premium_model_fast_request_limit: 600`` 是「快速请求档位可用」
                # 的能力位，不是按月可花的额度；把它铺成一条额度行会让人以为
                # 还有 600 次可以烧，而实际上这种额度上游根本不统计（已用恒
                # 未知）。真按次数扣的只有一个计数池，在聊天 SSE 的
                # ``notify_usage.remain_usage`` 里（本次实测 0），额度接口这边
                # 拿不到已用量，故不再单列。
                # Basic 是美元（``basic_usage_limit: 20`` + 已用
                # ``usage.basic_usage_amount``）。原通用键表
                # （``_LIMIT_KEYS_DOLLAR`` 命中 credits_limit=0）在这里只能
                # 解析出 0/0 的空条目，这就是「海外版查不到额度」的根因。
                basic = hooks._first_num(quota, ("basic_usage_limit",))
                if basic is not None and basic > 0:
                    amt = hooks._first_num(p.get("usage") or {}, ("basic_usage_amount",))
                    rows.append(("", round(float(amt), 4) if amt is not None else None,
                                 float(basic), "dollar"))
                # bonus / advanced / slow / auto_completion 的 -1 是「无限」，
                # 不占条目——无限没有进度可画，铺出来只是噪声。
            limit = None if rows else hooks._first_num(
                quota, hooks._LIMIT_KEYS_DOLLAR if dollar else hooks._LIMIT_KEYS_CREDITS)
            if not rows and limit is not None and limit > 0:
                limit_v = float(limit)
                # ``*_amount`` 是**已用**不是剩余——2026-10-03 实测交叉
                # 校验：Σlimit=9500.0、Σamount=6257.1772，Σlimit-Σamount 与
                # 接口自报 remaining（3242.82）差 0.00，而「amount=剩余」的
                # 假设差 3014.36。算反会把「剩 3242」显示成「剩 6257」。
                #
                # ``usage`` 缺失按 0 已用算（= 没花），**不是**「未知」：有
                # 记录的包已用合计 6257.1772 恰好等于 usage_summary 的
                # consumed_amount，且消耗严格按到期日 FIFO（先扣 4000 的会员
                # 包、再扣最早到期的签到包），未出现的包都是到期更晚、还没轮到
                # 的——真·满额。这与 ``trae/pat/quota.py`` 的相反先例不是一回
                # 事：那里是网关偶发只回半拉数据（包容量回来了、用量没回来），
                # 按 0 算会把未知说成满血；这里是接口语义，值为 0 就是没消费。
                amount = hooks._first_num(p.get("usage") or {},
                                    hooks._AMOUNT_KEYS_DOLLAR if dollar else hooks._AMOUNT_KEYS_CREDITS)
                used = round(float(amount), 2) if amount is not None else 0.0
            if not rows:
                rows.append(("", used, limit_v, unit))
            for suffix, row_used, row_total, row_unit in rows:
                packs.append({
                    "label": f"{label_prefix}{desc}{(' · ' + suffix) if suffix else ''}",
                    "used": row_used,
                    "total": round(row_total, 2) if row_total else None,
                    "percent": (round(row_used / row_total * 100)
                                if row_used is not None and row_total else None),
                    "remaining": (round(row_total - row_used, 2)
                                  if row_used is not None and row_total else None),
                    # 权益包只有到期、没有周期性重置，故 reset_ts 恒 None
                    "reset_ts": None,
                    "expire_ts": int(end_time),
                    "unit": row_unit,
                })
        packs.sort(key=lambda it: it["expire_ts"] or 0)  # 先到期的排前面
        items.extend(packs)
        return items


def _claim_rate_limited(data: dict[str, Any] | None, err: str = "") -> bool:
    """该次 claim 响应是否命中上游频控（按文案判断——上游无独立错误码）。"""
    from . import provider as hooks
    text = err + str((data or {}).get("message") or "")
    return any(m in text for m in hooks._CHECKIN_RATE_LIMIT_MARKERS)


def _first_num(obj: dict[str, Any], keys: Sequence[str]) -> float | None:
    """按候选名依次取第一个数值字段；全不命中/非数值返回 None。"""
    from . import provider as hooks
    for k in keys:
        v = obj.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None
