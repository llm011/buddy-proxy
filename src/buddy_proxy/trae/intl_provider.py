"""Trae 海外版独立 provider（``traeintl``）——与个人 CN 通道分开展示/计额/路由。

照 ``TraePatProvider`` 的先例：同上游家族的另一个变体用**独立 provider id 的
子类**，而不是给 ``TraeProvider`` 加运行期开关。这样：

- 客户端用 ``traeintl/<模型>`` 前缀显式路由到海外网关（``forward_chat`` 的
  前缀逻辑已支持），与 CN 的 ``trae/`` 互不干扰；
- ``/ui`` 管理页 CN 与海外各一张额度卡，各自计费口径（CN 积分 / 海外美元
  Usage）——由继承的 ``_quota_items`` 按 ``self._region.billing`` 自动分叉；
- 海外**无每日签到**（上游 ``checkin_credits/*`` 实测应用级 404），故
  ``supports_checkin=False``，自动签到调度直接跳过它，不会天天打一串 404
  进 ``checkin.jsonl`` 污染日历。

与 ``TraePatProvider`` 的关键差异：PAT 有**独立账号体系**（bearer 服务账号，
``_pat_variant=True`` 走单账号直通）；海外版**共用** work 多账号池，只是按
``region="global"`` 过滤——所以它不设 ``_pat_variant``，照常走父类的同区
failover（``forward`` / ``_work_accounts`` 已按 ``self._region.key`` 过滤）。

海外模型目录（``config.MODEL_*_INTL``）是手 probe 的静态底表；本 provider 另
实现 ``refresh_models``（模型页「↻ 重新拉取」按钮）：从 SG 网关
``get_detail_param`` 拉候选目录全量并入（发现逻辑见 ``intl_catalog.py``），
代际收敛 + 用户黑名单过滤在 ``_converge_catalog`` 做。静态表照旧兜底——
``models()`` 底部注释有完整语义。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .provider import TraeProvider

log = logging.getLogger(__name__)

#: 用户点名永不收录的旧模型（2026-10-06「太老」/ 2026-10-07 点名移除）。
#: 刷新拉到它们也直接丢弃，别当漏收补回来。
_INTL_NEVER_LIST = frozenset({
    "kimi-k2.7-code", "kimi-k2.5", "minimax-m2.7", "gpt-5.2",
})

#: 「家族名 + 数字版本」解析（glm-5.2 → ("glm", 5.2)；gpt-5.6-sol →
#: ("gpt", 5.6)——代号尾巴不影响代际比较）。解析不出的（minimax-m3 的 m3
#: 不是纯数字版本……实际是，m 是家族；见 _parse_generation）返回 None。
_VERSION_RE = re.compile(r"^(?P<family>[a-z]+)[-_]v?(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:[-_].*)?$")


def _parse_generation(name: str) -> tuple[str, float] | None:
    """模型名 -> (家族, 版本数)；解析不出返回 None（不参与代际收敛）。

    - ``glm-5.2`` → ("glm", 5.2)；``gpt-5.6-sol`` → ("gpt", 5.6)
    - ``deepseek-v3.2`` → ("deepseek", 3.2)——家族后缀 ``v`` 由正则吞掉；
      ``deepseek-v4-flash-0731`` → ("deepseek", 4.0)，flash/日期尾巴归进
      代号、不影响代际
    - ``minimax-m3`` → None（m 不是数字，代号系不比代际，m2.7/m3 由黑名单/
      静态表管）；``kimi-k3`` → None（同理）
    """
    m = _VERSION_RE.match(name.strip().lower())
    if not m:
        return None
    minor = m.group("minor")
    version = float(m.group("major")) + (float(minor) / 10 if minor else 0.0)
    return m.group("family"), version


def _is_old_generation(name: str, pool: set[str]) -> bool:
    """同家族有更高代际在 pool 里时，name 视为旧代（如 glm-5.2 遇 glm-5.3）。

    只在**双方都能解析出版本**时比较：目录里出现 glm-5.3 后，glm-5.2 就
    没必要再展示（用户 2026-10-12）；代号系（gpt-6-sol 等）永不因此淘汰。
    """
    parsed = _parse_generation(name)
    if parsed is None:
        return False
    family, version = parsed
    for other in pool:
        if other == name:
            continue
        op = _parse_generation(other)
        if op is not None and op[0] == family and op[1] > version:
            return True
    return False


def intl_enabled() -> bool:
    """有没有海外区账号（``--trae`` 分支据此决定注册 ``traeintl``）。

    与 ``traepat`` 的 ``pat_enabled()`` 同款条件注册：没登过海外账号时不挂
    这个通道，``/v1/models`` 不会多出一个空目录、``traeintl/`` 前缀也会走
    「通道未启用」的明确报错（而不是注册了却每个请求都 401）。

    只看索引（``list_accounts``，纯本地），**不看**冷却状态——账号全在冷却
    时通道仍该注册，转发侧自然 429 快速失败；否则「所有海外号都冷却」会被
    误报成「通道没开」，方向完全错。
    """
    try:
        from .credentials import list_accounts

        return any(a.region == "global" for a in list_accounts())
    except Exception:  # noqa: BLE001 - 索引读不动就当没登过
        return False


class TraeIntlProvider(TraeProvider):
    id = "traeintl"
    name = "Trae 海外版"
    # 海外上游没有签到端点（实测 404）——不进 /ui 打卡列表、不被自动签到调度。
    supports_checkin = False
    # 额度卡的分组标签用自己的通道名（见 TraeProvider._quota_tag）：与 CN 通道
    # 并行在线时，两卡的行标不能都叫 ``Trae #N``，否则分不清哪张是美元口径。
    _quota_tag = "Trae 海外版"

    def __init__(self, base_url: str | None = None):
        # 区域钉死 global：chat 网关、模型目录、failover 选账号、额度口径全按它。
        super().__init__(base_url=base_url, region="global")
        # 目录发现的快照（name -> {"name", "context_window"}）：refresh_models
        # 成功后原子替换；models() 里与静态底表合并展示。内存态、重启即空——
        # 与 qoder 的 Catalog 缓存同款生命周期，拉一次即回。
        self._discovered: dict[str, dict[str, Any]] = {}

    def models(self) -> list[dict[str, Any]]:
        """静态底表在前 + 目录发现的新名字在后，再过代际收敛/黑名单。

        代际收敛（用户 2026-10-12）：同家族更高代际在场时旧代不再展示——
        底表里的 glm-5.2 在发现 glm-5.3 后从列表消失（点名仍可调，只是不
        列出来）；黑名单（kimi-k2.5 等「太老」点名件）任何时候不出。
        """
        base = list(super().models())
        base_ids = {str(m.get("id") or "") for m in base}
        for name, entry in sorted(self._discovered.items()):
            if name in base_ids:
                continue
            base.append({
                "id": name,
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                # 海外是次数制没有积分倍率；图片能力未实测不声明（同静态表口径）
                "credits": None,
                "images": False,
                "context_window": entry.get("context_window"),
                "description": "Trae 海外版模型（目录发现）",
            })
        pool = {str(m.get("id") or "") for m in base}
        return [m for m in base
                if str(m.get("id") or "") not in _INTL_NEVER_LIST
                and not _is_old_generation(str(m.get("id") or ""), pool)]

    async def refresh_models(self, force: bool = False) -> list[dict[str, Any]]:
        """逐海外账号拉目录取并集，供模型页「↻ 重新拉取」与启动预热用。

        - 账号用**区内全量** ``list_accounts``（不剔冷却——目录发现与冷却
          无关，同 benefits 枚举的教训，qoder/antigravity 同款）；冷却号
          独有的模型不该被静默漏掉。
        - 逐账号 ``ensure_account_token`` + ``fetch_intl_catalog``（阻塞 IO
          放 ``asyncio.to_thread``，照 qoder.refresh_models）；单账号失败
          只 log 跳过，不拖垮整体。
        - 并集结果**原子替换** ``_discovered``；全账号失败抛 RuntimeError
          ——models_api 转 502、旧快照保留（models() 仍能出静态底表）。
        ``force`` 形参保留兼容通用刷新接口（本实现无 TTL 缓存，恒强拉）。
        """
        del force
        from . import intl_catalog
        from .credentials import ensure_account_token, list_accounts, load_account_cred

        accounts = [a for a in list_accounts() if a.region == "global"]
        if not accounts:
            raise RuntimeError("traeintl 没有海外账号可用于刷新模型目录")
        union: dict[str, dict[str, Any]] = {}
        succeeded = 0
        for acct in accounts:
            try:
                await asyncio.to_thread(ensure_account_token, acct.id)
                cred = await asyncio.to_thread(load_account_cred, acct.id)
                if not cred:
                    raise RuntimeError("凭据缺失或无 access_token")
                entries = await asyncio.to_thread(intl_catalog.fetch_intl_catalog, cred)
            except Exception as exc:  # noqa: BLE001 - 单账号失败不阻断并集
                log.warning("traeintl 目录刷新跳过账号 %s: %s", acct.id, exc)
                continue
            succeeded += 1
            for e in entries:
                union.setdefault(str(e.get("name") or ""), {
                    "name": e.get("name"),
                    "context_window": e.get("context_window"),
                })
        if succeeded == 0:
            raise RuntimeError("traeintl 模型目录刷新失败：全部账号均未成功")
        if union:
            # 只增不删的例外（见 models() docstring）：代际收敛在展示层做，
            # _discovered 本身保留上游全量，上游把旧代撤下后快照里自然消失。
            self._discovered = union
        return self.models()

    def ensure_auth(self) -> None:
        # 父类 _auth() 取的是**首个可用 work 账号**（可能是 CN 的），对海外通道
        # 没有意义——这里改成校验本区确实有账号，没有就明确报「未登录海外版」，
        # 免得健康检查拿 CN 账号蒙混过关、真发请求时才 401。
        from . import failover

        if not failover.available_accounts(self._region.key):
            from fastapi import HTTPException

            raise HTTPException(
                status_code=401,
                detail=(f"traeintl 无海外版可用账号：请先 "
                        f"`buddy login trae --region global`"))

    # —— 签到为个人 CN 账号专属；海外无端点，显式覆盖为 None ——
    # （supports_checkin=False 已挡住 UI 调度，这里再兜一层，防止继承的
    #  TraeProvider 实现对海外账号发 checkin 请求。）

    def checkin_status(self) -> dict[str, Any] | None:
        return None

    def checkin_claim(self) -> dict[str, Any] | None:
        return None
