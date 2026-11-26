"""Trae catalog discovery, metadata and generation convergence."""

from __future__ import annotations

from typing import Any, Sequence


class CatalogMixin:
    def models(self) -> Sequence[dict[str, Any]]:
        # 按区域取模型表：CN 与海外是两套几乎不重叠的目录（GLM/Doubao/Qwen vs
        # Claude/GPT/Gemini 系），必须各用各的——见 config.model_tables。
        from . import provider as hooks
        model_map, model_ids, _, supports_images = hooks.model_tables(self._region.key)
        result = []
        seen = set()
        # Max 模式全局开关现读一次：管理页热改后下一次 /v1/models 即生效。
        max_enabled = hooks.max_mode_mod.max_mode_enabled()

        def _entry(mid: str, **extra: Any) -> dict[str, Any]:
            info = self._discovered.get(mid) or {}
            # 别名（MODEL_MAP external -> internal）跟随内部名的 Max 能力与窗口：
            # 先解析到内部名（小写）再查 effective_window。
            internal = hooks.map_model_for(self._region.key, mid).lower()
            window, _inject = hooks.max_mode_mod.effective_window(
                self._region.key, internal, discovered=info, enabled=max_enabled)
            out = {
                "id": mid,
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                # 倍率走 credit_multiplier：刷新抄回的 consumption_rate 优先，
                # 其次静态底表（两者分工见 config.DISCOVERED_CREDITS 注释）
                "credits": hooks.credit_multiplier(self._region.key, mid),
                # 图片能力：与 CodeBuddy 通道同口径（供 /v1/models 的
                # input_modalities 判定），漏报会让客户端误剥图片
                "images": mid in supports_images or bool(info.get("multimodal")),
                "description": "Trae 模型",
            }
            # 展示名只在**真的不同**时给（比 id 时忽略大小写：上游
            # ``MiniMax-M3.1-Flash`` 只是官方排版，别让 /v1/models 多一个
            # 与 id 仅大小写不同的冗余字段）
            display = info.get("display_name")
            if display and display.lower() != mid.lower():
                out["name"] = display
            # 对外声明的上下文窗口由 Max 模式决策统一给出（开→max 窗，关→dev 窗，
            # 不支持→不声明）；守卫按此窗口预检，必须与转发注入的 is_max_mode 一致
            # （见 max_mode.py 模块 docstring 的强耦合不变量）。
            if window is not None:
                out["context_window"] = window
            out.update(extra)
            return out

        for m in model_ids:
            if m in seen:
                continue
            seen.add(m)
            result.append(_entry(m))
        # 加别名（外部名映射）——倍率与图片能力均跟随映射到的内部模型
        for external, internal in model_map.items():
            if external not in seen:
                seen.add(external)
                result.append(_entry(external, maps_to=internal,
                                     description=f"Trae 别名 -> {internal}"))
        # 目录发现的新名字排在底表之后（同 traeintl）：底表已有同名条目时
        # 上面的 _entry 已经带上了它的展示名/上下文/读图能力
        for name in sorted(self._discovered):
            if name not in seen:
                seen.add(name)
                result.append(_entry(name, description="Trae 模型（目录发现）"))
        # 代际收敛（与海外通道同一个函数）：同家族更高代际在场时旧代不再列出。
        # 静态底表是**人工确认可用**的清单，但没有理由把 glm-5.2 摆在新一代
        # 旁边（用户 2026-10-25 点名 glm-5.1 / glm-5.2 / qwen-3.7-plus /
        # minimax-m3 这批「没用」的老模型）。名字仍可点名调用，只是不进
        # /v1/models —— 与海外通道 2026-10-12 的口径一致。
        pool = {str(m.get("id") or "") for m in result}
        return [m for m in result
                if not hooks._is_old_generation(str(m.get("id") or ""), pool)]

    async def refresh_models(self, force: bool = False) -> list[dict[str, Any]]:
        """目录「重新拉取」：从上游目录端点拉全量候选 + 抄倍率，再剔失效。

        2026-10-25 改造（用户要求「拉取倍率」）：此前是纯探测——没有可拉的
        目录端点、只能逐模型 ping 然后剔 4001。实测 CN 网关的
        ``get_detail_param`` 返回完整目录，每条带 ``consumption_rate``（**就是
        客户端「模型倍率」面板显示的值**）与展示名/上下文/读图能力，于是：

        1. **拉目录**：CN 走 ``cn_catalog.fetch_cn_catalog``（IN 账号全量并集，
           不剔冷却——目录发现与冷却无关）；海外走继承的 ``intl_catalog``
           路径（``TraeIntlProvider.refresh_models`` 覆写，本方法到那里不执行）。
           单账号失败只 log 跳过；全失败抛错，旧快照保留（models_api 转 502）。
        2. **抄倍率**：写进 ``config.DISCOVERED_CREDITS``（运行时合成，不落盘。
           静态底表是人工核过的值，刷新不覆盖它，重启即回底表）。
        3. **验证可用性**：只对底表/发现里**没有**记录的**新名字**发一条真实
           请求（repo 铁律：上游收下名字 ≠ 能服务），4001/1005 拒收的不并入。
           已有模型不再逐个 ping——旧实现每点一次按钮就全表打一遍，既慢又会
           白付积分。
        4. **合并**：新名字进 ``self._discovered``（展示名/上下文/读图能力）。
        ``force`` 保留兼容通用刷新接口（本实现恒强拉）。"""
        from . import provider as hooks
        from .config import DISCOVERED_CREDITS
        from .credentials import ensure_account_token

        accounts = [a for a in hooks.list_accounts() if (a.region or "cn") == self._region.key]
        if not accounts:
            raise RuntimeError(f"trae({self._region.key}) 没有账号可用于刷新模型目录")

        from . import cn_catalog

        union: dict[str, dict[str, Any]] = {}
        succeeded = 0
        for acct in accounts:
            try:
                await hooks.asyncio.to_thread(ensure_account_token, acct.id)
                cred = await hooks.asyncio.to_thread(hooks.load_account_cred, acct.id)
                if not cred:
                    raise RuntimeError("凭据缺失或无 access_token")
                entries = await hooks.asyncio.to_thread(cn_catalog.fetch_cn_catalog, cred)
            except Exception as exc:  # noqa: BLE001 - 单账号失败不阻断并集
                hooks.log.warning("trae(%s) 目录刷新跳过账号 %s: %s",
                            self._region.key, acct.id, exc)
                continue
            succeeded += 1
            for e in entries:
                # 按**上游 config_name** 去重（跨账号同一条），元数据与倍率
                # 都是账号无关的，先到先得即可
                union.setdefault(e["name"], e)
        if not succeeded:
            raise RuntimeError(f"trae({self._region.key}) 目录刷新全部账号均未成功")

        _, model_ids, static_credits, _ = hooks.model_tables(self._region.key)
        # 对外 id 一律小写（cn_catalog 已归一），与底表 / model_order 同口径
        known = {m.lower() for m in model_ids} | set(self._discovered)
        # 上游真名 -> 对外 id（转发时由 map_model_for 还原大小写，见 MODEL_MAP）
        upstream_by_id = {e["id"]: e["name"] for e in union.values() if e.get("id")}
        verified = await self._verify_candidates(
            [e["id"] for e in union.values() if e.get("id") not in known])

        # 合并只看「这一轮到底进了目录的 id」：新名字须先过验证（verified），
        # 底表/上一版已有的 id 直接沿用。两段拼起来**不按上游顺序反而按
        # 「新 → 已有」**，是因为 ``discovered`` 只存元数据、最终展示顺序由
        # models() 决定（底表在前、发现项按字母序在后），这里顺序只影响
        # dict 的键序，纯为日志/调试时先看到新增的那几个。
        discovered: dict[str, dict[str, Any]] = {}
        seen_ids: set[str] = set()
        for mid in verified + [e["id"] for e in union.values() if e.get("id") in known]:
            if mid in seen_ids:
                continue
            e = union.get(upstream_by_id.get(mid) or "")
            if e is None:
                continue
            seen_ids.add(mid)
            discovered[mid] = {
                "display_name": e.get("display_name"),
                "context_window": e.get("context_window"),
                # max_mode / max_window：Max 模式真值（见 max_mode.py）。
                # 目录没下发（上游形状变化）时为 None，models() 退化到静态兜底。
                "max_mode": e.get("max_mode"),
                "max_window": e.get("max_window"),
                "multimodal": e.get("multimodal"),
                "upstream": upstream_by_id.get(mid),
            }
        upstream_seen = set(upstream_by_id)
        if succeeded == len(accounts):
            self._discovered = discovered
        else:
            self._discovered = {**self._discovered, **discovered}
        # 运行时倍率表与快照**同口径**维护：全号成功按本轮结果重建（上游下架
        # 的模型不能继续挂着倍率）；部分成功只覆盖本轮拉到的名字、其余留着。
        # 底表有的模型仍不抄（人工核过的活动折扣口径）。
        static_ids = {m.lower() for m in static_credits}
        if succeeded == len(accounts):
            DISCOVERED_CREDITS.clear()
        elif upstream_seen:
            for gone in upstream_seen:
                DISCOVERED_CREDITS.pop(gone, None)
        kept_ids = set(self._discovered)
        for e in union.values():
            # 只给**真的进了目录**的名字挂倍率：被 4001 拒收的候选不收录，
            # 也就不该在倍率表里留一条（否则 credit_multiplier 会给出一个
            # models() 根本不展示的模型的倍率）
            if (e.get("rate") is not None and e.get("id") in kept_ids
                    and e.get("id") not in static_ids):
                DISCOVERED_CREDITS[e["id"]] = f"x{float(e['rate']):.2f}"
        hooks.log.info("trae(%s) 目录刷新：上游 %d 条，新收录 %d 个，发现条目 %d 个%s",
                 self._region.key, len(union), len(verified),
                 len(self._discovered),
                 "" if succeeded == len(accounts) else "（部分账号失败，只增不删）")
        return list(self.models())

    async def _verify_candidates(self, names: list[str]) -> list[str]:
        """新名字逐个发一条最小真实请求，只留上游真能服务的。

        判定口径：``_probe_model`` 返回 False（4001 「参数非法」——名字/客户端
        版本不被接受）即不收；**能解析出错误码的其它拒绝**（1005 会员档位不足、
        4011 日额度等）说明名字本身有效、只是当前账号没权限，照收——它们与
        已收录模型同属「按账号诚实报错」的一类。网络/凭据瞬态按可用保留。
        """
        from . import provider as hooks
        ok_names: list[str] = []
        for name in names:
            try:
                ok = await hooks.asyncio.to_thread(self._probe_model, name)
            except Exception:  # noqa: BLE001 - 网络/凭据瞬态：按可用保留
                ok = True
            if ok:
                ok_names.append(name)
            else:
                hooks.log.info("trae(%s) 新模型 %s 被上游拒绝（4001），不收录",
                         self._region.key, name)
        return ok_names

    def _probe_model(self, model: str) -> bool:
        """单模型最小真实请求。返回 True=可用；False=上游明确拒绝（4001）。
        其它异常向上抛（调用方按瞬态处理，不剔除）。"""
        from . import provider as hooks
        work = hooks._load_work_cred()
        try:
            if work and work.get("access_token"):
                raw = hooks._send_trae_work_chat(
                    [{"role": "user", "content": [{"type": "text", "text": "ping"}]}],
                    model, False, work)
            else:
                raw = hooks._send_native_chat(
                    hooks._native_messages([{"role": "user", "content": "ping"}]),
                    model, False, None)
        except hooks.HTTPException as e:
            # 4001 只在 200+SSE error / 400 壳里出现；401/403/502 是凭据或
            # 网关问题，与模型无关——按可用保留（不剔除）
            if e.status_code == 400:
                return False
            raise
        # 200 + SSE：看首个事件是不是 error:4001（solo_work_lite 下被拒的形态）
        return not hooks._native_rejected(raw)


def _parse_generation(name: str) -> tuple[str, float] | None:
    """模型名 -> (家族, 版本数)；解析不出返回 None（不参与代际收敛）。

    - ``glm-5.2`` → ("glm", 5.2)；``gpt-5.6-sol`` → ("gpt", 5.6)
    - ``qwen3.8-max`` → ("qwen", 3.8)：家族名后的数字算家族一部分，
      所以 qwen3.8 与 qwen-3.7 同族，能比出代际
    - ``deepseek-v3.2`` → ("deepseek", 3.2)——家族后缀 ``v`` 由正则吞掉；
      ``deepseek-v4-flash-0731`` → ("deepseek", 4.0)，flash/日期尾巴归进
      代号、不影响代际
    - ``minimax-m3.1-flash`` → ("minimax-m", 3.1)；``kimi-k3`` → ("kimi-k", 3.0)
      ——代号字母进家族名（minimax-m 与 minimax 是两个家族，kimi-k 同理），
      这样 m3 遇 m3.1 能收敛、kimi-k3.1-pro 也能顶掉 kimi-k3
    """
    from . import provider as hooks
    text = name.strip().lower()
    m = hooks._GENERATION_RE.match(text) or hooks._GENERATION_CODENAME_RE.match(text)
    if not m:
        return None
    family = m.group("family")
    if "code" in m.groupdict() and m.group("code"):
        family = f"{family}-{m.group('code')}"
    minor = m.group("minor")
    version = float(m.group("major")) + (float(minor) / 10 if minor else 0.0)
    return family, version


def _is_old_generation(name: str, pool: set[str]) -> bool:
    """同家族有更高代际在 pool 里时，name 视为旧代（如 glm-5.2 遇 glm-5.3）。

    只在**双方都能解析出版本、且家族/代号都相同**时比较：目录里出现
    glm-5.3 后 glm-5.2 就没必要再展示（用户 2026-10-12；2026-10-25 用户再次
    点名 glm-5.1 / glm-5.2 / qwen-3.7-plus / minimax-m3 / kimi-k3 这批老模型
    没用——「有了 kimi-k3.1-pro 就不需要 kimi-k3」）。纯代号尾巴（gpt-6-sol
    的 sol）不进家族名，永不因此淘汰。

    例外是 :data:`_GENERATION_FLOOR`：某些家族有「保留底线」，代际不低于它的
    一律不被淘汰（见那个常量的说明）。
    """
    from . import provider as hooks
    parsed = hooks._parse_generation(name)
    if parsed is None:
        return False
    family, version = parsed
    # 家族保留底线：不低于它的代际不参与淘汰。glm-5.4 上线时靠这一条保住
    # 整个 5.3 家族（用户 2026-11-24 明确要求两代并存）。
    #
    # ⚠ 必须用 ``in`` 判断再取值，不能写 ``version >= _GENERATION_FLOOR.get(family, 0.0)``
    # ——缺省 0.0 会让**所有**家族的版本都 >= 0，等于把整个代际收敛关掉
    # （踩过：gpt-5.6 / deepseek-v4-pro 全都重新冒出来，三条测试当场红）。
    floor = hooks._GENERATION_FLOOR.get(family)
    if floor is not None and version >= floor:
        return False
    for other in pool:
        if other == name:
            continue
        op = hooks._parse_generation(other)
        if op is not None and op[0] == family and op[1] > version:
            return True
    return False
