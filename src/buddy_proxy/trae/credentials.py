"""Trae Work 凭证与请求头：多账号存储、刷新、两种请求头。

服务端按 X-Ide-Version-Code 门控模型能力（见 config._TRAE_IDE_VERSION_CODE 注释），
两个头构建函数必须使用同一版本码。

**多账号**（2026-10，照 qoder/kimi 同款）::

    ~/.buddy-proxy/trae/
    ├── index.json          # 账号清单：[{id, uid, nickname, priority, added_at, region}]
    ├── index.lock          # fcntl 跨进程锁
    └── <account_id>.json   # 每账号一份 work cred（0600，含 region）

``region``（``cn`` / ``global``）在登录时写入；缺失按 ``cn``（历史账号全是
国内版）。两区账号互不通用，failover 只在同区内轮转。

``TRAE_WORK_STATE_DIR`` 可覆盖状态目录（测试隔离点）。首访问自动把历史单账号
``trae_work.json`` 迁移为账号 #1。``account_id`` 首选 uid（trae work 登录必带回）。

刷新：access_token 临近过期时用 refresh_token 走 ``auth.trae_work_login.exchange_token``
换新（上游会滚动 refresh_token，一并回写）。
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .auth_storage import _extract_auth_fields, find_auth_data
from .config import (
    TRAE_REGIONS,
    X_APP_ID,
    _TRAE_APP_VERSION,
    _TRAE_APP_VERSION_CODE,
    _TRAE_IDE_VERSION_CODE,
    resolve_trae_region,
)
from ..core.account_store import (
    AccountStore,
    atomic_write_json,
    is_valid_account_id,
)
from ..core.paths import state_dir, state_file

log = logging.getLogger(__name__)

#: 状态目录名。
STATE_DIR_NAME = "trae"

#: 历史单账号状态文件（迁移源；新装不再创建）。
LEGACY_WORK_CRED_NAME = "trae_work.json"

#: 提前多久刷新 access_token（5 分钟）。
REFRESH_MARGIN_S = 5 * 60

#: 账号数上限（与 qoder/kimi 同值）。
_MAX_ACCOUNTS = 8

#: 这份凭据还能不能用的判据字段（读 / 自愈 / upsert 共用同一个）。
_CRED_REQUIRED_FIELD = "access_token"


class AuthError(RuntimeError):
    """凭据缺失、无效或刷新失败。"""


@dataclass(frozen=True)
class AccountRef:
    """账号清单条目（不含任何秘密，可直接进 UI/health）。

    ``region`` 缺省 ``cn``：迁移前的历史账号全是国内版，索引里没有这个字段时
    按 CN 处理，行为与改造前一致（``list_accounts`` / ``save_account_cred``
    都兜这一层）。两区账号不通用——failover 只在同区间轮转（见 ``failover``）。
    """

    id: str
    uid: str
    nickname: str
    priority: int
    added_at: int
    # 本地别名（管理页 ✎ 改）：只改显示名，cred 里的 nickname 不动；
    # 空串 = 未设置（显示回退 nickname/uid/id）。upsert/重登不碰它。
    alias: str = ""
    region: str = "cn"


# ---------------------------------------------------------------------------
# 请求头（原实现保留，签名不变）
# ---------------------------------------------------------------------------

def _build_headers(
    token: str,
    user_id: str,
    *,
    machine_id: str | None = None,
    device_id: str | None = None,
) -> dict[str, str]:
    """构建 SOLO 完整请求头（traework2api headers.go 实测值）。

    PAT 多账号调用方会传入账号级稳定设备指纹；个人账号的旧调用未传时仍保持
    原来的逐请求随机行为。设备指纹不从 bearer/token 推导，避免凭据侧信道。
    """
    machine_id = machine_id or uuid.uuid4().hex
    device_id = device_id or hashlib.sha256(machine_id.encode()).hexdigest()[:32]
    return {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": f"Trae/{_TRAE_APP_VERSION}",
        "Authorization": f"Cloud-IDE-JWT {token}",
        "X-Cloudide-Token": token,
        "X-Ide-Token": token,
        "X-Uid": user_id or "",
        "X-App-Id": X_APP_ID,
        "X-App-Version": "default",
        "X-Ide-Version": _TRAE_APP_VERSION,
        "X-Ide-Version-Code": _TRAE_IDE_VERSION_CODE,
        "X-App-Version-Code": _TRAE_APP_VERSION_CODE,
        "X-Ide-Version-Type": "stable",
        "X-Device-Type": "windows",
        "X-OS-Version": "Windows 11 Pro",
        "X-Device-Brand": "83DG",
        "Request-Traffic-Type": "prod",
        "X-Machine-Id": machine_id,
        "X-Device-Id": device_id,
    }


def _work_headers(work: dict[str, Any]) -> dict[str, str]:
    """Work (SOLO) 通道完整请求头（traework2api headers.go 实测值）。

    关键：必须带 User-Agent: Trae/<ver> + X-Ide-Token 等 SOLO 专属头，
    缺 UA 会被服务端当异常客户端限流（4011）。
    """
    machine_id = work.get("machine_id") or uuid.uuid4().hex
    device_id = work.get("device_id") or hashlib.sha256(machine_id.encode()).hexdigest()[:32]
    return {
        "Content-Type": "application/json",
        "User-Agent": f"Trae/{_TRAE_APP_VERSION}",
        "Authorization": f"Cloud-IDE-JWT {work['access_token']}",
        "X-Cloudide-Token": work["access_token"],
        "X-Ide-Token": work["access_token"],
        "X-Uid": work.get("uid") or "",
        "X-App-Id": X_APP_ID,
        "X-App-Version": "default",
        "X-Ide-Version": _TRAE_APP_VERSION,
        "X-Ide-Version-Code": _TRAE_IDE_VERSION_CODE,
        "X-App-Version-Code": _TRAE_APP_VERSION_CODE,
        "X-Ide-Version-Type": "stable",
        "X-Device-Type": "windows",
        "X-OS-Version": "Windows 11 Pro",
        "X-Device-Brand": "83DG",
        "Request-Traffic-Type": "prod",
        "X-Machine-Id": machine_id,
        "X-Device-Id": device_id,
    }


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def trae_state_dir() -> Path:
    """多账号状态目录（``TRAE_WORK_STATE_DIR`` 可覆盖；测试隔离点）。"""
    env = os.environ.get("TRAE_WORK_STATE_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return state_dir() / STATE_DIR_NAME


def index_path() -> Path:
    return _STORE.index_path()


def account_cred_path(account_id: str) -> Path:
    """单账号 cred 文件路径。id 形状在生成处已校验，这里再防一道手滑。"""
    return _STORE.cred_path(account_id)


def legacy_work_cred_path() -> Path:
    """历史单账号状态文件路径（迁移源）。"""
    configured = os.environ.get("TRAE_WORK_CRED_PATH", "")
    if configured:
        return Path(configured)
    return state_file(LEGACY_WORK_CRED_NAME, legacy=LEGACY_WORK_CRED_NAME)


#: 兼容旧名（外部/shim 引用 WORK_CRED_PATH / _WORK_CRED_PATH）。
WORK_CRED_PATH = legacy_work_cred_path()
_WORK_CRED_PATH = WORK_CRED_PATH


# ---------------------------------------------------------------------------
# 索引读写（全部在 _index_lock + 跨进程 flock 下进行）
# ---------------------------------------------------------------------------
#
# 这层已下沉到 ``core/account_store``（trae/qoder/codex 三处逐字重复）：0600 原子
# 写、进程内锁 + flock、自愈、priority 排序与重排校验只有一份实现。下面这些名
# 字是旧内部调用点（本包其它模块与测试）的兼容入口。

#: 账号索引 + 每账号 0600 凭据文件的共同底座。
_STORE = AccountStore(
    label=STATE_DIR_NAME,
    state_dir_fn=trae_state_dir,
    max_accounts=_MAX_ACCOUNTS,
)

_index_lock = _STORE.lock
_index_file_lock = _STORE.file_lock
_atomic_write_json = atomic_write_json


def _read_index() -> dict[str, Any]:
    return _STORE.read_index()


# ---------------------------------------------------------------------------
# 历史单账号迁移
# ---------------------------------------------------------------------------

_LEGACY_MIGRATE_FLAG = ".legacy-migrated"


def _migrate_legacy_unlocked() -> None:
    """把历史单账号 ``trae_work.json`` 迁移成账号 #1（幂等）。

    与 qoder 同款：只迁一次（落标记文件），追加而非替换——已有多账号的用户重跑
    不会把登录态重置回旧文件。
    """
    flag = trae_state_dir() / _LEGACY_MIGRATE_FLAG
    if flag.exists():
        return
    path = legacy_work_cred_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if isinstance(data, dict) and str(data.get("access_token") or "").strip():
        cred = dict(data)
        cred["source"] = "state"
        # 历史单账号文件只可能出自 CN 登录（改造前没有区域概念），显式钉死
        # cn——别让迁移那一刻碰巧设着的 TRAE_REGION=global 把它改判成海外。
        cred.setdefault("region", "cn")
        _save_account_cred_unlocked(cred)
        log.info("trae: 已把历史单账号状态 %s 迁移为账号 %s",
                 path, cred.get("account_id"))
    try:
        flag.touch()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# account_id 派生
# ---------------------------------------------------------------------------


def _region_of(cred: dict[str, Any], fallback: str = "") -> str:
    """cred 里的 region（登录时写入）；缺省/非法值归一到默认区域。

    ``fallback`` 供「更新已有账号」时用：老索引里的 region 优先于默认值，
    免得一次没带 region 的凭据刷新把海外账号改判成 CN。
    """
    raw = str(cred.get("region") or "").strip().lower()
    if raw in TRAE_REGIONS:
        return raw
    if fallback and fallback in TRAE_REGIONS:
        return fallback
    return resolve_trae_region().key


def derive_account_id(cred: dict[str, Any]) -> str:
    """从 cred 派生稳定账号 id；已有合法 id 原样保留。

    uid 首选（trae work 登录必带回，跨重登稳定）；拿不到退 nickname；
    再退 ``acct-<sha256(access_token)[:12]>``。
    """
    existing = str(cred.get("account_id") or "").strip()
    if existing and is_valid_account_id(existing):
        return existing
    uid = str(cred.get("uid") or "").strip()
    if uid and is_valid_account_id(uid):
        return uid
    nickname = str(cred.get("nickname") or "").strip()
    if nickname and is_valid_account_id(nickname):
        return nickname
    digest = hashlib.sha256(str(cred.get("access_token") or "").encode()).hexdigest()[:12]
    return f"acct-{digest}"


# ---------------------------------------------------------------------------
# 多账号 API
# ---------------------------------------------------------------------------


def list_accounts() -> list[AccountRef]:
    """枚举全部账号（按 priority 稳定排序）。

    唯一枚举入口：历史单账号迁移 + 索引自愈（cred 文件被删/损坏的条目剔除）
    挂在这里，login/forward/quota/UI 走同一个口。
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
        # 索引缺 region（迁移前的老账号）按 CN——与 AccountRef 默认一致
        region=str(entry.get("region") or "cn"),
    )


def load_account_cred(account_id: str) -> dict[str, Any] | None:
    """读单账号 cred；没有/损坏/缺 access_token 返回 None。"""
    return _STORE.load_cred(account_id, required_field=_CRED_REQUIRED_FIELD)


def save_account_cred(cred: dict[str, Any]) -> AccountRef:
    """落盘一个账号的凭据（幂等 upsert），返回账号引用。

    匹配顺序：account_id → uid → refresh_token。命中即更新（priority/added_at
    不变——重新登录不改变 failover 顺位），全不命中追加为新账号。
    """
    with _STORE.guard():
        return _account_ref(_save_account_cred_unlocked(cred))


def _save_account_cred_unlocked(cred: dict[str, Any]) -> dict[str, Any]:
    """``save_account_cred`` 的锁内版本（历史迁移也走这里，返回索引条目）。"""
    return _STORE.upsert_locked(
        cred,
        required_field=_CRED_REQUIRED_FIELD,
        derive_id=derive_account_id,
        identity_fields=("uid", "refresh_token"),
        entry_fields=_entry_fields,
        sync_cred=_sync_cred_region,
        error=AuthError,
    )


def _entry_fields(cred: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """upsert 时要写进索引条目的通道字段（``previous`` 为空表示新账号）。"""
    prev = previous or {}
    return {
        "uid": str(cred.get("uid") or prev.get("uid") or ""),
        "nickname": str(cred.get("nickname") or prev.get("nickname") or ""),
        # 重登同号：**已有账号的 region 优先**（别让一次没带 region 的刷新把
        # 海外账号改判成 CN）；cred 显式带了才覆盖。
        "region": _region_of(cred, fallback=str(prev.get("region") or "")),
    }


def _sync_cred_region(cred: dict[str, Any], entry: dict[str, Any]) -> None:
    """cred 文件也带 region：transport/native_tools 按「当前账号」构造请求，
    读 cred 才能决定连哪个 chat 网关（index 只给枚举，不在请求路径上）。"""
    cred["region"] = entry["region"]


def delete_account(account_id: str) -> bool:
    """移除一个账号（索引条目 + cred 文件）；返回是否真的删了。"""
    return _STORE.delete(account_id)


def reorder_accounts(ordered_ids: list[str]) -> list[AccountRef]:
    """按给定 id 顺序重写全部账号的 priority（failover 顺位）。

    入参必须是**完整**的当前账号 id 列表（少一个/多一个/重复即拒绝）。
    """
    list_accounts()  # 先走唯一的枚举入口：迁移/自愈完再重排，别在残缺索引上动刀
    _STORE.reorder(ordered_ids)
    return list_accounts()


def rename_account(account_id: str, alias: str) -> list[AccountRef]:
    """改一个账号的本地别名（管理页 ✎）。

    只改 index.json 条目的 ``alias`` 字段：cred 文件（原始凭据）与
    priority/added_at 都不动。``alias`` 去首尾空白；空串 = 清除别名，
    显示回退默认名。账号不存在抛 ``ValueError``。返回改后的 list_accounts()。
    """
    list_accounts()  # 先走唯一的枚举入口：迁移/自愈完再改，别在残缺索引上动刀
    _STORE.rename(account_id, alias)
    return list_accounts()


def has_cred() -> bool:
    return bool(list_accounts())


# ---------------------------------------------------------------------------
# 刷新与获取（转发主路径）
# ---------------------------------------------------------------------------


def _cred_expired(cred: dict[str, Any]) -> bool:
    """access_token 是否已过期或临近过期（无 expires_at 视为未过期）。"""
    exp = int(cred.get("expires_at") or 0)
    if not exp:
        return False
    return exp - REFRESH_MARGIN_S <= int(time.time())


def refresh_account_cred(cred: dict[str, Any]) -> dict[str, Any]:
    """用 refresh_token 换新 access_token，更新并写回该账号的文件。

    每账号刷新锁 + 锁内重读盘双检：并发只有一个真发刷新（上游 RT 滚动，重放
    可能作废整条链）。锁内覆盖「刷新 + 回写」整段。
    """
    aid = str(cred.get("account_id") or "")
    rt = str(cred.get("refresh_token") or "")
    if not rt:
        raise AuthError("缺少 refresh_token，无法刷新；请重新 `buddy login trae`")
    lock = _refresh_lock(aid)
    with lock:
        fresh = _reload_if_rotated(cred)
        if fresh is not None:
            return fresh
        from buddy_proxy.auth.trae_work_login import exchange_token

        # 刷新必须打该账号所属区域的 OAuth 域：CN/海外 ExchangeToken 端点不同，
        # 拿海外 refresh_token 去连 CN 会 401。cred 落盘时写了 region（老 CN
        # 账号缺字段 → _region_of 回退默认 cn，与改造前行为一致）。
        try:
            result = exchange_token(rt, region=_region_of(cred))
        except Exception as exc:  # noqa: BLE001 - 换不动就报凭据失效
            raise AuthError(f"刷新失败: {exc}") from exc
        new_cred = dict(cred)
        new_cred["access_token"] = result["access_token"]
        new_cred["refresh_token"] = result.get("refresh_token") or rt
        new_cred["expires_at"] = int(result.get("expires_at") or 0)
        # 刷新是网络往返，期间用户可能在面板删了这个号或导入了新凭据——回写前
        # 重读盘，别复活已删账号、也别用旧快照回滚新导入。
        with _STORE.guard():
            current = load_account_cred(aid)
            if current is None:
                return new_cred
            if current.get("refresh_token") != cred.get("refresh_token"):
                log.debug("trae: 刷新期间账号 %s 凭据已变更，放弃整份回写", aid)
                return new_cred
            _STORE.write_cred(aid, new_cred)
    log.info("trae token 已刷新 (uid=%s)", cred.get("uid"))
    return new_cred


def _reload_if_rotated(cred: dict[str, Any]) -> dict[str, Any] | None:
    """锁内检查：该账号文件里的 token 已被别的请求换过就复用它。"""
    aid = str(cred.get("account_id") or "")
    state = load_account_cred(aid)
    if state is None:
        return None
    token = str(state.get("access_token") or "")
    if not token or token == cred.get("access_token"):
        return None
    if _cred_expired(state):
        return None
    state["account_id"] = aid
    return state


_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _refresh_lock(account_id: str) -> threading.Lock:
    """每账号一把刷新锁。"""
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(account_id, threading.Lock())


def ensure_account_token(account_id: str, *, force_refresh: bool = False
                         ) -> tuple[str, dict[str, Any]]:
    """拿该账号可用的 access_token，返回 ``(token, work cred 快照 dict)``。

    cred 与 token 同源：转发要 uid/machine_id/device_id（账号级身份），多账号
    下「token、cred 各读一次盘」会串号。
    """
    cred = load_account_cred(account_id)
    if cred is None:
        raise AuthError(f"trae 账号 {account_id} 的凭据不存在或已损坏")
    if not force_refresh and not _cred_expired(cred):
        return cred["access_token"], dict(cred)
    refreshed = refresh_account_cred(cred)
    return refreshed["access_token"], dict(refreshed)


def ensure_account_work(account_id: str, *, force_refresh: bool = False
                        ) -> dict[str, Any]:
    """``ensure_account_token`` 的 cred 形态（transport/native_tools 用）。"""
    _, cred = ensure_account_token(account_id, force_refresh=force_refresh)
    return cred


# ---------------------------------------------------------------------------
# 兼容薄壳（旧单账号调用方 / monkeypatch 点）
# ---------------------------------------------------------------------------


#: 请求级「当前 work 账号」holder（多账号 failover 用）。``provider.forward``
#: 每次 failover 尝试前 set 当前账号 id；``_load_work_cred`` 优先读它取对应 cred。
#: ContextVar 让读线程（``contextvars.copy_context()`` 副本）也能读到正确账号，
#: 不污染其它请求。PAT 子类走自己的发送路径（不经过 work transport），无影响。
_CURRENT_WORK_ACCOUNT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trae_current_work_account", default=None)


def set_current_work_account(account_id: str | None) -> None:
    """failover 循环选定账号后写入 holder（``None`` 复位）。"""
    _CURRENT_WORK_ACCOUNT.set(account_id)


def _load_work_cred() -> dict[str, Any] | None:
    """取**当前 work 账号**的 cred（多账号 failover 用）。

    优先读 :data:`_CURRENT_WORK_ACCOUNT`（failover 循环已选定账号）；未设置时
    退回顺位 #1 账号（单账号语义，兼容旧调用方与测试 monkeypatch 点）。
    transport/native_tools 走这里构造请求头——它们不需感知 failover，只读「当前账号」。
    """
    current = _CURRENT_WORK_ACCOUNT.get()
    if current:
        try:
            _, cred = ensure_account_token(current)
            return cred
        except AuthError as exc:
            log.warning("trae 当前账号 %s 凭据不可用: %s", current, exc)
            return None
    accounts = list_accounts()
    if not accounts:
        return None
    return load_account_cred(accounts[0].id)


def _auth() -> tuple[str, str]:
    """获取 (token, user_id)：优先首个可用 Work 账号，其次环境变量，最后解密。

    兼容旧签名（单账号语义 = 取顺位 #1）。多账号转发请走 ``ensure_account_token``。
    """
    # 0) 多账号 Work 凭证（首个可用账号）
    accounts = list_accounts()
    if accounts:
        try:
            token, cred = ensure_account_token(accounts[0].id)
            log.info("Trae auth loaded via Work account %s (uid=%s)", accounts[0].id,
                     cred.get("uid"))
            return token, str(cred.get("uid") or "")
        except AuthError as exc:
            log.warning("trae 首个账号凭据不可用，尝试下一个/其它来源: %s", exc)

    # 1) 环境变量（TRAE_TOKEN / TRAE_USER_ID）
    token = os.environ.get("TRAE_TOKEN", "")
    user_id = os.environ.get("TRAE_USER_ID", "")
    if token:
        return token, user_id

    # 2) 解密 Trae 本地存储
    for edition in ("cn", "sg", "solo"):
        try:
            data = find_auth_data(edition)
            token, user_id = _extract_auth_fields(data)
            if token:
                log.info("Trae auth loaded via %s decrypt", edition)
                return token, user_id
        except Exception as e:  # noqa: BLE001
            log.debug("Trae %s auth failed: %s", edition, e)

    raise HTTPException(status_code=401, detail="trae not authenticated - 请先运行 buddy login trae 或登录 Trae IDE")
