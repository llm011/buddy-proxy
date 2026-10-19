"""Codex 凭证：多账号存储、刷新、token 获取。

结构照抄 kimi / antigravity（多账号 index + 每账号一份 cred 文件）：::

    ~/.buddy-proxy/codex/
    ├── index.json              # 账号清单：[{id, name, priority, added_at, alias}]
    └── <account_id>.json       # 每账号一份 cred（0600）

单账号 cred 与 Codex CLI 的 ``~/.codex/auth.json`` 尽量同形，导入近乎透传::

    {
      "account_id": "acc-1b2c3d",        # 稳定坐标 = 文件名
      "type": "codex",
      "access_token": "...",             # 短期（约 1 小时）
      "refresh_token": "...",            # 长期，OpenAI 会轮换
      "id_token": "...",                 # JWT，读邮箱/套餐/account_id
      "expired": "2026-10-19T06:06:17+00:00",
      "last_refresh": 1730000000,
      "token_type": "Bearer",
      "email": "user@example.com",
      "plan_type": "plus",               # free/plus/pro/team/business
      "chatgpt_account_id": "acc-...",   # 请求头 Chatgpt-Account-Id
      "user_id": "user-...",             # 面板展示 + id 派生源
    }

``account_id`` 首选 chatgpt_account_id（同一账号重登稳定）；退 user_id；再退
refresh_token 哈希兜底，保证任何导入路径都落得了盘。

**refresh_token 轮换**：OpenAI 刷新会下发新的 refresh_token 并把旧的作废，
所以回写时「payload 带了就更新」，绝不能用旧值覆盖——这一条与 kimi 的防御性
策略相反，是 ChatGPT 后端的实测行为。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import pathlib
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from buddy_proxy.core.paths import state_dir

from . import oauth
from .upstream import cred_expired_epoch

log = logging.getLogger(__name__)

#: 提前这个量刷新 access_token（寿命约 1 小时，5 分钟提前量足够）。
_EXPIRY_SKEW = timedelta(minutes=5)

#: 账号数上限（与其它通道同量级）。
_MAX_ACCOUNTS = 8

#: account_id 形状：既是文件名（无 ``/``，天然防路径穿越）也是 UI 账号坐标。
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")


class AuthError(RuntimeError):
    """刷新失败（网络/被拒/refresh_token 作废）。"""


@dataclass(frozen=True)
class AccountRef:
    """账号清单条目（不含任何秘密，可直接进 UI/health）。"""

    id: str
    name: str
    priority: int
    added_at: int
    # 本地别名（管理页 ✎ 改）：只改显示名，cred 里的邮箱不动；
    # 空串 = 未设置（显示回退 name/id）。upsert/重登不碰它。
    alias: str = ""


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def codex_state_dir() -> pathlib.Path:
    """多账号状态目录（``CODEX_STATE_DIR`` 可覆盖；测试隔离点）。"""
    env = os.environ.get("CODEX_STATE_DIR", "").strip()
    if env:
        return pathlib.Path(env).expanduser()
    return state_dir() / "codex"


def index_path() -> pathlib.Path:
    return codex_state_dir() / "index.json"


def account_cred_path(account_id: str) -> pathlib.Path:
    """单账号 cred 文件路径。id 形状在生成处已校验，这里再防一道手滑。"""
    if not _ID_RE.fullmatch(account_id):
        raise ValueError(f"非法账号 id: {account_id!r}")
    return codex_state_dir() / f"{account_id}.json"


# ---------------------------------------------------------------------------
# 索引读写（全部在 _index_lock 下进行）
# ---------------------------------------------------------------------------

_index_lock = threading.Lock()


class _FileLock:
    """跨进程文件锁（fcntl.flock，包在 .lock 文件上）。

    ``_index_lock`` 只挡进程内——网关进程和 CLI（``buddy login codex`` /
    面板导入是另一个进程入口）会同时读-改-写同一个 index.json，各自拿旧
    快照写回就会丢更新（面板显示幽灵账号、顺位错乱）。flock 让读-改-写整体
    成为一个跨进程临界区；flock 与进程内锁可组合（同一进程重复 flock 同一
    文件会被内核拒绝，故必须先拿进程内锁再拿文件锁）。
    """

    def __init__(self, path: pathlib.Path):
        self._path = path
        self._fh = None

    def __enter__(self) -> "_FileLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a+")  # noqa: SIM115 - 句柄在 __exit__ 关
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> bool:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
        return False


def _index_file_lock() -> _FileLock:
    return _FileLock(codex_state_dir() / "index.lock")


def _atomic_write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    """0600 原子写（临时文件 + rename，与其它通道同款）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _read_index() -> dict[str, Any]:
    try:
        data = json.loads(index_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _index_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"version": 1, "accounts": entries}


# ---------------------------------------------------------------------------
# account_id 派生
# ---------------------------------------------------------------------------

def derive_account_id(cred: dict[str, Any]) -> str:
    """从 cred 派生稳定账号 id；已有合法 id 原样保留。

    chatgpt_account_id 首选（同一 ChatGPT 账号重登/换设备都稳定）；退 user_id；
    再退 refresh_token 哈希——保证任何导入路径都落得了盘。
    """
    existing = str(cred.get("account_id") or "").strip()
    if existing and _ID_RE.fullmatch(existing):
        return existing
    for key in ("chatgpt_account_id", "user_id"):
        value = str(cred.get(key) or "").strip()
        if value:
            slug = re.sub(r"[^A-Za-z0-9._@-]", "-", value)[:56]
            candidate = f"acct-{slug}"
            if _ID_RE.fullmatch(candidate[:64]):
                return candidate[:64]
    digest = hashlib.sha256(str(cred.get("refresh_token") or "").encode()).hexdigest()[:12]
    return f"acct-{digest}"


# ---------------------------------------------------------------------------
# 多账号 API
# ---------------------------------------------------------------------------

def list_accounts() -> list[AccountRef]:
    """枚举全部账号（按 priority 稳定排序）。

    唯一的账号枚举入口：索引自愈（cred 文件被删/损坏的条目剔除——删文件即
    退出该账号）挂在这里，login/forward/quota/UI 走同一个口。
    """
    with _index_lock, _index_file_lock():
        entries = [e for e in (_read_index().get("accounts") or []) if isinstance(e, dict)]
        kept: list[AccountRef] = []
        changed = False
        for e in entries:
            aid = str(e.get("id") or "")
            if not _ID_RE.fullmatch(aid):
                changed = True
                continue
            if load_account_cred(aid) is None:
                changed = True
                log.warning("codex: 账号 %s 的凭据文件缺失或损坏，已从账号清单剔除", aid)
                continue
            kept.append(AccountRef(
                id=aid,
                name=str(e.get("name") or ""),
                priority=int(e.get("priority") or 0),
                added_at=int(e.get("added_at") or 0),
                alias=str(e.get("alias") or ""),
            ))
        if changed:
            _atomic_write_json(index_path(), _index_payload([asdict(a) for a in kept]))
        kept.sort(key=lambda a: (a.priority, a.added_at, a.id))
        return kept


def load_account_cred(account_id: str) -> dict[str, Any] | None:
    """读单账号 cred；没有/损坏/缺 refresh_token 返回 None。"""
    if not _ID_RE.fullmatch(account_id):
        return None
    try:
        data = json.loads(account_cred_path(account_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not str(data.get("refresh_token") or "").strip():
        return None
    return data


def save_account_cred(cred: dict[str, Any]) -> AccountRef:
    """落盘一个账号的凭据（幂等 upsert），返回账号引用。

    匹配顺序：account_id → refresh_token → chatgpt_account_id/user_id。
    命中即更新该账号（priority/added_at 不变——重新导入不改变 failover
    顺位），全不命中追加为新账号（priority 排到最后）。
    cred 缺 account_id 时现场生成并写回原 dict（调用方拿到即可用）。
    """
    with _index_lock, _index_file_lock():
        return _save_account_cred_unlocked(cred)


def _save_account_cred_unlocked(cred: dict[str, Any]) -> AccountRef:
    entries = [e for e in (_read_index().get("accounts") or []) if isinstance(e, dict)]
    aid = str(cred.get("account_id") or "")

    def _find_existing() -> dict[str, Any] | None:
        """按「账号身份」找回索引里的既有条目。

        ``refresh_token``（同一次授权的凭据，最强信号）与
        ``chatgpt_account_id`` / ``user_id``（跨登录稳定）。少了后者，
        「重新登录」这条最常见的路径会落成新 id → 同一账号占两个顺位：
        旧那个（死 RT）每轮 failover 白打一次上游，真正能用的还排在末尾。
        """
        by_id = next((e for e in entries if str(e.get("id") or "") == aid), None)
        if by_id is not None:
            return by_id
        rt = str(cred.get("refresh_token") or "")
        identities = {str(cred.get(k) or "").strip()
                      for k in ("chatgpt_account_id", "user_id")}
        identities.discard("")
        if not rt and not identities:
            return None
        for e in entries:
            other = load_account_cred(str(e.get("id") or "")) or {}
            if rt and other.get("refresh_token") == rt:
                return e
            if identities and (identities & {
                    str(other.get(k) or "").strip()
                    for k in ("chatgpt_account_id", "user_id")}):
                return e
        return None

    target = _find_existing()
    if target is None and not _ID_RE.fullmatch(aid):
        cred["account_id"] = derive_account_id(cred)  # 就地写回，调用方可见
        aid = str(cred["account_id"])
        target = next((e for e in entries if str(e.get("id") or "") == aid), None)
    if target is not None:
        aid = str(target["id"])
        cred["account_id"] = aid
    else:
        if not _ID_RE.fullmatch(aid):
            cred["account_id"] = derive_account_id(cred)  # 就地写回，调用方可见
            aid = str(cred["account_id"])
        if len(entries) >= _MAX_ACCOUNTS:
            raise AuthError(
                f"codex 账号数已达上限 {_MAX_ACCOUNTS}，"
                f"请先在管理页删除不用的账号")
        ref = AccountRef(
            id=aid,
            name=str(cred.get("email") or cred.get("name") or ""),
            priority=max((int(e.get("priority") or 0) for e in entries), default=-1) + 1,
            added_at=int(time.time()),
        )
        entries.append(asdict(ref))
        target = asdict(ref)
    target["name"] = str(cred.get("email") or target.get("name") or "")
    # alias 不在 upsert 覆写之列：它是用户手起的本地别名，重登不改
    ref = AccountRef(id=aid, name=target["name"],
                     priority=int(target.get("priority") or 0),
                     added_at=int(target.get("added_at") or 0),
                     alias=str(target.get("alias") or ""))
    _atomic_write_json(account_cred_path(ref.id), dict(cred))
    _atomic_write_json(index_path(), _index_payload(entries))
    return ref


def delete_account(account_id: str) -> bool:
    """移除一个账号（索引条目 + cred 文件）；返回是否真的删了。"""
    with _index_lock, _index_file_lock():
        entries = [e for e in (_read_index().get("accounts") or []) if isinstance(e, dict)]
        kept = [e for e in entries if str(e.get("id") or "") != account_id]
        if len(kept) == len(entries):
            return False
        _atomic_write_json(index_path(), _index_payload(kept))
    try:
        account_cred_path(account_id).unlink()
    except OSError:
        pass
    return True


def reorder_accounts(ordered_ids: list[str]) -> list[AccountRef]:
    """按给定 id 顺序重写全部账号的 priority（failover 顺位）。

    入参必须是**完整**的当前账号 id 列表（少一个/多一个/重复即拒绝）；
    priority 重写为 0..n-1，added_at 原样保留。返回重排后的 list_accounts()。
    """
    list_accounts()  # 先走唯一的枚举入口：自愈完再重排，别在残缺索引上动刀
    with _index_lock, _index_file_lock():
        entries = [e for e in (_read_index().get("accounts") or []) if isinstance(e, dict)]
        current = [str(e.get("id") or "") for e in entries]
        if sorted(ordered_ids) != sorted(current) or len(set(ordered_ids)) != len(ordered_ids):
            raise ValueError(
                f"重排必须提交完整的账号 id 列表（当前 {len(current)} 个，"
                f"收到 {len(ordered_ids)} 个）")
        rank = {aid: i for i, aid in enumerate(ordered_ids)}
        for e in entries:
            e["priority"] = rank[str(e.get("id") or "")]
        _atomic_write_json(index_path(), _index_payload(entries))
    return list_accounts()


def rename_account(account_id: str, alias: str) -> list[AccountRef]:
    """改一个账号的本地别名（管理页 ✎）。

    只改 index.json 条目的 ``alias`` 字段：cred 文件（原始凭据）与
    priority/added_at 都不动。``alias`` 去首尾空白；空串 = 清除别名，
    显示回退默认名。账号不存在抛 ``ValueError``。返回改后的 list_accounts()。
    """
    aid = str(account_id or "")
    text = str(alias or "").strip()
    list_accounts()  # 先走唯一的枚举入口：自愈完再改，别在残缺索引上动刀
    with _index_lock, _index_file_lock():
        entries = [e for e in (_read_index().get("accounts") or []) if isinstance(e, dict)]
        target = next((e for e in entries if str(e.get("id") or "") == aid), None)
        if target is None:
            raise ValueError(f"账号 {aid} 不存在")
        target["alias"] = text
        _atomic_write_json(index_path(), _index_payload(entries))
    return list_accounts()


def has_cred() -> bool:
    return bool(list_accounts())


# ---------------------------------------------------------------------------
# token 刷新与获取（转发主路径）
# ---------------------------------------------------------------------------

def _expiry_iso(expires_in: Any) -> str:
    """payload ``expires_in`` → ISO UTC 绝对时刻（写进 cred 的 ``expired``）。"""
    try:
        seconds = float(expires_in)
    except (TypeError, ValueError):
        seconds = 3600.0
    if seconds <= 0:
        seconds = 3600.0
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def access_token_valid(cred: dict[str, Any]) -> bool:
    """access_token 是否还在有效期内（含 5 分钟提前量）。"""
    exp = cred_expired_epoch(cred)
    if exp <= 0:
        return False
    return time.time() < exp - _EXPIRY_SKEW.total_seconds()


def refresh_account_cred(cred: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """用 refresh_token 换新 access_token，更新并写回该账号的文件。

    返回更新后的 cred（原 dict 就地更新）。写回走 :func:`save_account_cred`
    的幂等 upsert——同 account_id/refresh_token 必然命中原账号，priority
    不变。**OpenAI 轮换 refresh_token**：payload 带了就必须更新，否则旧值
    作废、下次刷新必失败。刷新失败抛 :class:`AuthError`。
    """
    try:
        payload = oauth.refresh(str(cred["refresh_token"]), timeout=timeout)
    except oauth.OAuthError as exc:
        raise AuthError(str(exc)) from exc

    # 刷新是网络往返（最长 timeout 秒），期间用户可能在面板删了这个号或
    # 导入了新凭据。先重读盘：账号已被删就别把它"复活"（save_account_cred
    # 会当成新账号追加）；发现 RT 已被换掉（刚导入的新凭据）就整份放弃回写。
    with _index_lock, _index_file_lock():
        current = load_account_cred(str(cred.get("account_id") or ""))
        if current is None or current.get("refresh_token") != cred.get("refresh_token"):
            log.debug("codex: 刷新期间账号已被删除或凭据已变更，放弃回写: %s",
                      cred.get("account_id"))
            cred["access_token"] = payload.access_token
            cred["expired"] = _expiry_iso(payload.expires_in)
            cred["last_refresh"] = int(time.time())
            return cred

    cred["access_token"] = payload.access_token
    cred["expired"] = _expiry_iso(payload.expires_in)
    cred["last_refresh"] = int(time.time())
    cred["token_type"] = payload.token_type or "Bearer"
    if payload.scope:
        cred["scope"] = payload.scope
    # 轮换：新 RT 必须落盘（不判断「为空则保留」——那会把已作废旧值写回去）
    if payload.refresh_token:
        cred["refresh_token"] = payload.refresh_token
    if payload.id_token:
        cred["id_token"] = payload.id_token
        claims = oauth.decode_id_token_claims(payload.id_token)
        if claims:
            _apply_claims(cred, claims)
    save_account_cred(cred)
    return cred


def _apply_claims(cred: dict[str, Any], claims: dict[str, Any]) -> None:
    """把 ID token 声明回填进 cred（只填拿得到的，不覆盖已有值以外的字段）。"""
    account_id = oauth.account_id_from_claims(claims)
    if account_id:
        cred["chatgpt_account_id"] = account_id
    email = oauth.email_from_claims(claims)
    if email:
        cred["email"] = email
    user_id = oauth.user_id_from_claims(claims)
    if user_id:
        cred["user_id"] = user_id
    plan = oauth.plan_type_from_claims(claims)
    if plan:
        cred["plan_type"] = plan


_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _refresh_lock(account_id: str) -> threading.Lock:
    """每账号一把刷新锁：并发请求同时发现 token 过期时只有一个去刷新。"""
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(account_id, threading.Lock())


def ensure_account_token(account_id: str, *, force_refresh: bool = False) -> tuple[str, dict[str, Any]]:
    """拿该账号可用的 access_token，返回 ``(token, cred 快照)``。

    cred 与 token 同源是本函数存在的理由：转发要 chatgpt_account_id（账号级
    身份），多账号下「token、cred 各读一次盘」会串号。刷新在每账号锁内做
    并重读盘双检——别的线程刚刷完落盘就不再刷。
    """
    cred = load_account_cred(account_id)
    if cred is None:
        raise AuthError(f"codex 账号 {account_id} 的凭据不存在或已损坏")
    if not force_refresh and access_token_valid(cred):
        return cred["access_token"], dict(cred)
    with _refresh_lock(account_id):
        cred = load_account_cred(account_id) or cred  # 别的线程可能刚刷完落盘
        if force_refresh or not access_token_valid(cred):
            refresh_account_cred(cred)
        return cred["access_token"], dict(cred)
