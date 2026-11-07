"""CodeBuddy 凭据：多账号存储、刷新（结构照 qoder/trae/kimi 同款）。

    ~/.buddy-proxy/codebuddy/
    ├── index.json              # 账号清单：[{id, uid, nickname, priority, added_at, alias, region}]
    ├── index.lock              # fcntl 跨进程锁
    └── <account_id>.json       # 每账号一份 cred（0600）

``CODEBUDDY_STATE_DIR`` 可覆盖状态目录（测试隔离点）。

索引与凭据文件的读写统一走 ``core/account_store.AccountStore``（0600 原子写、
进程内锁 + flock、自愈、顺位排序/重排校验）；本模块只保留 codebuddy 自己的
语义：id 怎么派生、哪些字段算同一账号（并按 region 隔离）、索引条目里存什么。

**历史单账号迁移**：老用户只有一个 ``~/.codebuddy-session.json``（CodeBuddyClient
的 session 文件）。首次触达多账号 API 时把它迁为账号 #1——**追加**而不是替换，
且只迁一次（落 ``.legacy-migrated`` 标记），之后 legacy 文件再变（demo CLI/
`buddy login` 旧路径还在写它）也不会把账号清单顶回去。

**没有 region 维度**：codebuddy 只有腾讯云一套端点，所有账号同域，failover
不需要分区过滤（qoder/trae 的 ``available_accounts(region)`` 在这里退化为全量）。

token 刷新走 ``/v2/plugin/auth/token/refresh``（X-Refresh-Token 头），与
CodeBuddyClient.refresh 同一上游契约；区别是这里按账号读写各自的 cred 文件，
并带每账号刷新锁 + 锁内重读盘双检（并发请求同时发现过期时只有一个去刷新，
等锁的直接复用别人刚刷完的结果）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from buddy_proxy.core.account_store import (
    ACCOUNT_ID_RE,
    AccountStore,
    is_valid_account_id,
)
from buddy_proxy.core.paths import state_dir

log = logging.getLogger(__name__)

#: 状态目录名。
STATE_DIR_NAME = "codebuddy"

#: 默认上游端点（与 CodeBuddyClient / --endpoint 的默认一致）。
DEFAULT_ENDPOINT = "https://copilot.tencent.com"

#: 海外版端点。CodeBuddy 桌面端 product-ide.json（intl）与 product-ide-cn.json
#: 的唯一差异就是这个 host（2026-10 实测两边 /v2/plugin 协议逐字节同构），
#: 海外账号只是换域名登录/转发，凭据形状与 CN 完全一致。
INTL_ENDPOINT = "https://www.codebuddy.ai"

#: 历史 CodeBuddyClient session 文件（迁移源；新装不再创建）。
LEGACY_SESSION_NAME = ".codebuddy-session.json"

#: access token 过期兜底（上游给 expiresAt，毫秒；缺失视为长期有效）。
DEFAULT_TTL_MS = 30 * 24 * 3600 * 1000

#: 提前多久刷新（5 分钟），与 client.ensure_authenticated 的 60s 余量相比更宽。
REFRESH_MARGIN_MS = 5 * 60 * 1000

#: 账号数上限：``None`` = 不限制（历史值 8，2026-11 按用户要求放开）。
_MAX_ACCOUNTS: int | None = None

#: 这份凭据还能不能用的判据字段（读 / 自愈 / upsert 共用同一个）。
_CRED_REQUIRED_FIELD = "token"

#: 兼容旧名：id 形状的唯一定义已下沉到 ``core/account_store``。
_ID_RE = ACCOUNT_ID_RE


class AuthError(RuntimeError):
    """凭据缺失、无效或刷新失败。"""


@dataclass(frozen=True)
class AccountRef:
    """账号清单条目（不含任何秘密，可直接进 UI/health）。"""

    id: str
    uid: str
    nickname: str
    priority: int
    added_at: int
    # 本地别名（管理页 ✎ 改）：只改显示名，原始凭据里的 nickname 不动；
    # 空串 = 未设置（显示回退 nickname/uid/id）。upsert/重登不碰它。
    alias: str = ""
    # 账号归属区：cn（国内版，copilot.tencent.com）/ global（海外版，
    # www.codebuddy.ai）。历史条目无该字段一律视为 cn（存量不动）。
    region: str = "cn"


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def codebuddy_state_dir() -> Path:
    """多账号状态目录（``CODEBUDDY_STATE_DIR`` 可覆盖；测试隔离点）。"""
    env = os.environ.get("CODEBUDDY_STATE_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return state_dir() / STATE_DIR_NAME


def index_path() -> Path:
    return _STORE.index_path()


def account_cred_path(account_id: str) -> Path:
    """单账号 cred 文件路径。id 形状在生成处已校验，这里再防一道手滑。"""
    return _STORE.cred_path(account_id)


def legacy_session_path() -> Path:
    """历史单账号 session 文件路径（迁移源；``CODEBUDDY_LEGACY_SESSION`` 可覆盖）。"""
    override = os.environ.get("CODEBUDDY_LEGACY_SESSION", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / LEGACY_SESSION_NAME


def endpoint(region: str = "cn") -> str:
    """上游端点（与 CodeBuddyClient 默认一致）。

    ``cn`` 走 ``CODEBUDDY_ENDPOINT`` / DEFAULT_ENDPOINT；``global`` 走
    ``CODEBUDDY_INTL_ENDPOINT`` / INTL_ENDPOINT。缺省 cn——全部历史调用方
    （client/老测试）不用感知海外版的存在。
    """
    if region == "global":
        return os.environ.get("CODEBUDDY_INTL_ENDPOINT", "").strip() or INTL_ENDPOINT
    return os.environ.get("CODEBUDDY_ENDPOINT", "").strip() or DEFAULT_ENDPOINT


# ---------------------------------------------------------------------------
# 索引读写（全部在 _STORE.guard() 下进行）
# ---------------------------------------------------------------------------
#
# 这层已下沉到 ``core/account_store``：0600 原子写、进程内锁 + 跨进程 flock、
# 自愈、priority 排序与重排校验只有一份实现。下面这些名字是旧内部调用点
# （本包其它模块与测试）的兼容入口。

#: 账号索引 + 每账号 0600 凭据文件的共同底座。
_STORE = AccountStore(
    label=STATE_DIR_NAME,
    state_dir_fn=codebuddy_state_dir,
    max_accounts=_MAX_ACCOUNTS,
)

_index_lock = _STORE.lock
_index_file_lock = _STORE.file_lock


# ---------------------------------------------------------------------------
# 历史单账号迁移（~/.codebuddy-session.json → 账号 #1）
# ---------------------------------------------------------------------------

_LEGACY_MIGRATE_FLAG = ".legacy-migrated"


def session_to_cred(session: dict[str, Any]) -> dict[str, Any]:
    """把 CodeBuddyClient 的 session（{auth, account, machineId}）摊平成 cred dict。"""
    session = session if isinstance(session, dict) else {}
    auth = session.get("auth") if isinstance(session.get("auth"), dict) else {}
    account = session.get("account") if isinstance(session.get("account"), dict) else {}
    return {
        "token": str(auth.get("accessToken") or ""),
        "refresh_token": str(auth.get("refreshToken") or ""),
        "expires_at_ms": int(auth.get("expiresAt") or 0),
        "domain": str(auth.get("domain") or ""),
        "uid": str(account.get("uid") or ""),
        "nickname": str(account.get("nickname") or ""),
        "enterprise_id": str(account.get("enterpriseId") or ""),
        "department_info": str(account.get("departmentInfo") or ""),
        "machine_id": str(session.get("machineId") or ""),
        "source": "state",
    }


def _migrate_legacy_unlocked() -> None:
    """把历史单账号 session 迁移成账号 #1（幂等、只迁一次）。

    与 qoder/antigravity 同款：迁移是**追加**而不是替换；只迁一次（落标记
    文件），避免 legacy 文件后来又被旧路径写坏时反复把旧值顶回来。
    """
    flag = codebuddy_state_dir() / _LEGACY_MIGRATE_FLAG
    if flag.exists():
        return
    path = legacy_session_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if isinstance(data, dict):
        cred = session_to_cred(data)
        if str(cred.get("token") or "").strip():
            _save_account_cred_unlocked(cred)
            log.info("codebuddy: 已把历史单账号 session %s 迁移为账号 %s",
                     path, cred.get("account_id"))
    try:
        flag.touch()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# account_id 派生
# ---------------------------------------------------------------------------


def derive_account_id(cred: dict[str, Any]) -> str:
    """从 cred 派生稳定账号 id；已有合法 id 原样保留。

    uid 首选（登录轮询 ``/v2/plugin/login/account`` 必带回，天然唯一且跨重登
    稳定）；拿不到退 ``acct-<sha256(token)[:12]>``——保证任何导入路径都落得了盘。

    海外版（region=global）的 uid 可能与 CN 账号撞车（两家账号体系独立），
    一律加 ``intl-`` 前缀隔离，CN 账号的 id 形状保持不变（存量零迁移）。
    """
    existing = str(cred.get("account_id") or "").strip()
    if is_valid_account_id(existing):
        return existing
    prefix = "" if str(cred.get("region") or "cn") == "cn" else "intl-"
    uid = str(cred.get("uid") or "").strip()
    if uid and is_valid_account_id(prefix + uid):
        return prefix + uid
    digest = hashlib.sha256(str(cred.get("token") or "").encode()).hexdigest()[:12]
    return f"{prefix}acct-{digest}"


def _is_expired(cred: dict[str, Any], margin_ms: int = REFRESH_MARGIN_MS) -> bool:
    """token 是否已过期或即将过期（无 expires_at 时视为未过期）。"""
    exp = int(cred.get("expires_at_ms") or 0)
    if not exp:
        return False
    return exp - margin_ms <= int(time.time() * 1000)


# ---------------------------------------------------------------------------
# 多账号 API
# ---------------------------------------------------------------------------


def list_accounts() -> list[AccountRef]:
    """枚举全部账号（按 priority 稳定排序）。

    唯一的账号枚举入口：历史单账号迁移 + 索引自愈（cred 文件被删/损坏的
    条目剔除——删文件即退出该账号）挂在这里，login/forward/quota/UI 走同一个口。
    """
    with _STORE.guard():
        _migrate_legacy_unlocked()
        entries = _STORE.list_entries_locked(
            required_field=_CRED_REQUIRED_FIELD, logger=log
        )
    return [_account_ref(entry) for entry in entries]


def _account_ref(entry: dict[str, Any]) -> AccountRef:
    """索引条目 → 只含非秘密字段的 ``AccountRef``（UI/health 直接用）。"""
    return AccountRef(
        id=str(entry.get("id") or ""),
        uid=str(entry.get("uid") or ""),
        nickname=str(entry.get("nickname") or ""),
        priority=int(entry.get("priority") or 0),
        added_at=int(entry.get("added_at") or 0),
        alias=str(entry.get("alias") or ""),
        region=str(entry.get("region") or "cn"),
    )


def load_account_cred(account_id: str) -> dict[str, Any] | None:
    """读单账号 cred；没有/损坏/缺 token 返回 None。"""
    return _STORE.load_cred(account_id, required_field=_CRED_REQUIRED_FIELD)


def save_account_cred(cred: dict[str, Any]) -> AccountRef:
    """落盘一个账号的凭据（幂等 upsert），返回账号引用。

    匹配顺序：account_id → uid → refresh_token，且身份匹配限定在同一 ``region``
    内（CN 与海外的账号体系独立，uid/refresh_token 撞车也不能互并）。命中即更新
    该账号（priority/added_at 不变——重新登录不改变 failover 顺位），全不命中
    追加为新账号（priority 排到最后）。cred 缺 account_id 时现场生成并写回原 dict。
    """
    with _STORE.guard():
        return _account_ref(_save_account_cred_unlocked(cred))


def _save_account_cred_unlocked(cred: dict[str, Any]) -> dict[str, Any]:
    """``save_account_cred`` 的锁内版本（历史迁移也走这里，返回索引条目）。"""
    region = str(cred.get("region") or "cn")
    return _STORE.upsert_locked(
        cred,
        required_field=_CRED_REQUIRED_FIELD,
        derive_id=derive_account_id,
        identity_fields=("uid", "refresh_token"),
        identity_scope=lambda entry: str(entry.get("region") or "cn") == region,
        entry_fields=_entry_fields,
        error=AuthError,
    )


def _entry_fields(cred: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """upsert 时要写进索引条目的通道字段（``previous`` 为空表示新账号）。

    region 命中既有条目时以条目为准（存量条目视为 cn，不被一次没带 region 的
    重登覆盖）；新账号才用 cred 的 region。
    """
    prev = previous or {}
    return {
        "uid": str(cred.get("uid") or prev.get("uid") or ""),
        "nickname": str(cred.get("nickname") or prev.get("nickname") or ""),
        "region": str(prev.get("region") or cred.get("region") or "cn"),
    }


def delete_account(account_id: str) -> bool:
    """移除一个账号（索引条目 + cred 文件）；返回是否真的删了。"""
    return _STORE.delete(account_id)


def reorder_accounts(ordered_ids: list[str],
                     region: str | None = None) -> list[AccountRef]:
    """按给定 id 顺序重写账号的 priority（failover 顺位）。

    ``region=None``：入参必须是**完整**的当前账号 id 列表（少一个/多一个/
    重复即拒绝）——历史契约，全 CN 时代的行为不变。

    ``region`` 给定（"cn"/"global"）：入参只须覆盖**该区**的全部账号 id，
    其余区的账号保持现有相对顺位不动。双区共用一个 index.json，CN 面板
    拿不到（也不该拿到）海外账号的 id，故按区分治。
    priority 重写后全量仍保持 0..n-1 连续，added_at 原样保留。
    返回重排后的 list_accounts()。
    """
    list_accounts()  # 先走唯一的枚举入口：迁移/自愈完再重排，别在残缺索引上动刀
    if region is None:
        _STORE.reorder(ordered_ids)
        return list_accounts()
    with _STORE.guard():
        entries = _STORE.list_entries_locked()
        in_region = [e for e in entries if str(e.get("region") or "cn") == region]
        in_ids = [str(e.get("id") or "") for e in in_region]
        if sorted(ordered_ids) != sorted(in_ids) or len(set(ordered_ids)) != len(ordered_ids):
            raise ValueError(
                f"重排必须提交 {region} 区完整的账号 id 列表"
                f"（该区当前 {len(in_ids)} 个，收到 {len(ordered_ids)} 个）")
        # 只在该区自己的位置段内重排：把新序按序填回该区原本占据的坑位
        slots = set(in_ids)
        it = iter(ordered_ids)
        new_order = [next(it) if str(e.get("id") or "") in slots
                     else str(e.get("id") or "") for e in entries]
        rank = {aid: i for i, aid in enumerate(new_order)}
        for e in entries:
            e["priority"] = rank[str(e.get("id") or "")]
        _STORE.write_index(entries)
    return list_accounts()


def rename_account(account_id: str, alias: str) -> list[AccountRef]:
    """改一个账号的本地别名（管理页 ✎）。

    只改 index.json 条目的 ``alias`` 字段：cred 文件（原始凭据）与
    priority/added_at 都不动。``alias`` 去首尾空白；空串 = 清除别名。
    账号不存在抛 ``ValueError``。返回改后的 list_accounts()。
    """
    list_accounts()  # 先走唯一的枚举入口：迁移/自愈完再改，别在残缺索引上动刀
    _STORE.rename(account_id, alias)
    return list_accounts()


def has_cred() -> bool:
    return bool(list_accounts())


# ---------------------------------------------------------------------------
# 认证 headers（从 cred 构造，替代单账号 client.auth_headers）
# ---------------------------------------------------------------------------


def auth_headers_from_cred(cred: dict[str, Any]) -> dict[str, str]:
    """按账号 cred 构造与 CodeBuddyClient.auth_headers 同形状的认证 headers。

    字段一一对应（IDE 识别头是上游风控的一部分，一个都不能少）：
    X-User-Id/X-Enterprise-Id/X-Tenant-Id/X-Department-Info 来自 account，
    Authorization/X-Domain 来自 auth，X-Machine-Id 来自 machineId。
    """
    machine_id = str(cred.get("machine_id") or "")
    if not machine_id:
        # cred 缺 machine_id（旧迁移源没带）时现场补一个稳定的并回写，
        # 与 CodeBuddyClient._get_machine_id 同款语义。
        import platform
        import uuid

        seed = f"{platform.node()}-{os.environ.get('USER') or 'unknown'}"
        machine_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
        try:
            cred["machine_id"] = machine_id
            with _STORE.guard():
                current = load_account_cred(str(cred.get("account_id") or ""))
                if current is not None and not current.get("machine_id"):
                    current["machine_id"] = machine_id
                    _STORE.write_cred(str(cred["account_id"]), current)
        except Exception:  # noqa: BLE001 - 回写失败不挡转发
            pass
    headers: dict[str, str] = {
        "X-Product-Code": "codebuddy",
        "X-IDE-Type": "vscode",
        "X-IDE-Name": "Visual Studio Code",
        "X-IDE-Version": "1.70.2",
        "X-Product-Version": "4.10.33259736",
        "X-Machine-Id": machine_id,
    }
    uid = str(cred.get("uid") or "")
    if uid:
        headers["X-User-Id"] = uid
    token = str(cred.get("token") or "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    enterprise_id = str(cred.get("enterprise_id") or "")
    if enterprise_id:
        headers["X-Enterprise-Id"] = enterprise_id
        headers["X-Tenant-Id"] = enterprise_id
    department_info = str(cred.get("department_info") or "")
    if department_info:
        headers["X-Department-Info"] = department_info
    domain = str(cred.get("domain") or "")
    if domain:
        headers["X-Domain"] = domain
    return headers


# ---------------------------------------------------------------------------
# 刷新与获取（转发主路径）
# ---------------------------------------------------------------------------


def _unwrap(payload: Any) -> Any:
    """CodeBuddy envelope 解包（与 CodeBuddyClient._unwrap 同规则）。"""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        nested = payload["data"]
        if "data" in nested:
            return nested["data"]
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def refresh_account_cred(cred: dict[str, Any]) -> dict[str, Any]:
    """用 refresh token 换新 access token，更新并写回该账号的文件。

    并发场景下**先抢每账号锁再重读盘**：等锁的请求进来时，前面那个多半已经
    刷新完并落盘了，此时直接复用新 token 返回，不再拿旧 refresh_token 打第二次
    （重放会被上游判重）。锁内覆盖「刷新 + 回写」整段：同步 IO 持锁阻塞无碍
    （调用方在 ``asyncio.to_thread`` 里跑，不卡事件循环）。

    刷新后回写前再重读盘：账号已被删就别把它"复活"；发现 RT 已被换掉（刚导入
    的新凭据）就整份放弃回写，免得拿旧快照把 uid/machine_id 全退回旧值。
    """
    aid = str(cred.get("account_id") or "")
    lock = _refresh_lock(aid)
    with lock:
        fresh = _reload_if_rotated(cred)
        if fresh is not None:
            return fresh
        rt = str(cred.get("refresh_token") or "")
        if not rt:
            raise AuthError("缺少 refresh_token，无法刷新；请重新 `buddy login codebuddy`")
        headers = auth_headers_from_cred(cred)
        headers.pop("Authorization", None)
        headers["X-Refresh-Token"] = rt
        headers["X-Auth-Refresh-Source"] = "plugin"
        try:
            resp = httpx.post(
                f"{endpoint(str(cred.get('region') or 'cn'))}/v2/plugin/auth/token/refresh",
                json={},
                headers={"Accept": "application/json", **headers},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"刷新请求失败: {exc}") from exc
        if resp.status_code != 200:
            raise AuthError(f"刷新失败 HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = _unwrap(resp.json())
        except ValueError as exc:
            raise AuthError(f"刷新响应不是 JSON: {resp.text[:200]}") from exc
        if not isinstance(payload, dict) or not payload.get("accessToken"):
            raise AuthError(f"刷新响应缺少 accessToken: {resp.text[:200]}")
        new_cred = dict(cred)
        new_cred["token"] = str(payload["accessToken"])
        new_cred["refresh_token"] = str(payload.get("refreshToken") or rt)
        new_cred["expires_at_ms"] = int(payload.get("expiresAt") or 0) or (
            int(time.time() * 1000) + DEFAULT_TTL_MS)
        new_cred["domain"] = str(payload.get("domain") or cred.get("domain") or "")
        # 刷新是网络往返（最长 30 秒），期间用户可能在面板删了这个号或导入了
        # 新凭据——回写前重读盘，别复活已删账号、也别用旧快照回滚新导入。
        with _STORE.guard():
            current = load_account_cred(aid)
            if current is None:
                log.debug("codebuddy: 刷新期间账号 %s 已被删除，放弃回写", aid)
                return new_cred
            if current.get("refresh_token") != cred.get("refresh_token"):
                log.debug("codebuddy: 刷新期间账号 %s 凭据已变更，放弃整份回写", aid)
                return new_cred
            _STORE.write_cred(aid, new_cred)
    log.info("codebuddy token 已刷新 (account=%s uid=%s)", aid, cred.get("uid") or "?")
    return new_cred


def _reload_if_rotated(cred: dict[str, Any]) -> dict[str, Any] | None:
    """锁内检查：该账号文件里的 token 已被别的请求换过就复用它。

    判据是 token 本身不同且新 token 未过期——只有真的换过才走这条捷径，
    否则（等锁期间没人动过）返回 ``None``，调用方照常发刷新请求。
    """
    aid = str(cred.get("account_id") or "")
    state = load_account_cred(aid)
    if state is None:
        return None
    token = str(state.get("token") or "")
    if not token or token == cred.get("token"):
        return None
    if _is_expired(state):
        return None
    log.debug("codebuddy token 已由并发请求刷新，复用新值 (account=%s)", aid)
    state["account_id"] = aid
    return state


_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _refresh_lock(account_id: str) -> threading.Lock:
    """每账号一把刷新锁：并发请求同时发现 token 过期时只有一个去刷新。"""
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(account_id, threading.Lock())


def ensure_account_token(account_id: str, *, force_refresh: bool = False
                         ) -> tuple[str, dict[str, Any]]:
    """拿该账号可用的 access token，返回 ``(token, cred 快照 dict)``。

    cred 与 token 同源是本函数存在的理由：转发要 uid/enterprise_id/machine_id
    （认证 headers 的账号级身份），多账号下「token、cred 各读一次盘」会串号。
    刷新在每账号锁内做并重读盘双检——别的线程刚刷完落盘就不再刷。
    """
    cred = load_account_cred(account_id)
    if cred is None:
        raise AuthError(f"codebuddy 账号 {account_id} 的凭据不存在或已损坏")
    cred["account_id"] = account_id
    if not force_refresh and not _is_expired(cred):
        return str(cred["token"]), dict(cred)
    refreshed = refresh_account_cred(cred)
    return str(refreshed["token"]), dict(refreshed)


def api_post_as(account_id: str, path: str, body: Any = None, *,
                timeout: float = 30) -> dict[str, Any]:
    """以指定账号身份调管理类接口（签到/额度/流水），返回**未解包** envelope。

    与 CodeBuddyClient.api_post 同契约（原始 {code, msg, requestId, data}，
    由调用方按业务语义解包），凭据换成多账号 store 里的该账号。同步 httpx；
    benefits 层经 ``asyncio.to_thread`` 调用，不卡事件循环。
    """
    token, cred = ensure_account_token(account_id)
    base = endpoint(str(cred.get("region") or "cn"))
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Genie-IDE/1.0)",
        "Accept": "application/json",
        "Accept-Language": "zh",
        "Content-Type": "application/json",
        **auth_headers_from_cred({**cred, "token": token}),
    }
    resp = httpx.post(f"{base}{path}",
                      json={} if body is None else body,
                      headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise AuthError(f"HTTP {resp.status_code} {path}: {resp.text[:200]}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise AuthError(f"{path} 返回的不是 JSON: {resp.text[:200]!r}") from exc
    return payload if isinstance(payload, dict) else {}
