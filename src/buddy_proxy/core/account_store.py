"""多账号凭据存储的共同底座：账号索引、每账号 0600 凭据文件、跨进程锁。

六个多账号通道（trae / qoder / codex / kimi / antigravity / codebuddy）各自的
``credentials.py`` 里，「``~/.buddy-proxy/<ch>/index.json`` + ``<id>.json`` +
``index.lock``」这一层是逐字重复的：同样的 0600 原子写、同样的「进程内锁 +
flock」、同样的「cred 文件没了就从索引里剔除」自愈、同样的 priority 排序与重排
校验。重复的代价不是行数，而是**修一处漏五处**：原子写漏了 0600、自愈时把新加的
字段抹掉、重排少一道去重校验，都只会在其中一个通道里静默发生。

这里只收敛「与通道无关」的部分。通道语义留在各自的 ``credentials.py``：

- 账号 id 怎么派生（``derive_id``）；
- 哪些字段算同一账号（``identity_fields``，命中即更新而不是追加新顺位）；
- 索引条目里除 ``id`` / ``priority`` / ``added_at`` / ``alias`` 之外还要存什么
  （``entry_fields``），以及落盘前要不要回写 cred（``sync_cred``）。

**索引自愈不再重建条目**：``list_entries_locked`` 只剔除「id 非法 / cred 文件
缺失损坏」的条目，其余原样写回。改造前每个通道都从 ``AccountRef`` 重建一遍
``asdict``，漏一个字段就会在下次枚举时把它悄悄抹掉——这正是
``docs/architecture.md`` 里那条「新增索引字段必须同时改自愈代码」警告的来源。
保留原始条目以后，通道加字段不必再动自愈代码。

本模块是**叶子**：只 import 标准库。状态目录由调用方以 callable 传入
（``state_dir_fn``），所以各通道自己的环境变量覆盖、测试隔离点都留在通道里，
core 不反向依赖任何通道。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

#: account_id 形状：既是文件名（无 ``/``，天然防路径穿越）也是 UI 账号坐标。
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")

#: 索引条目的固定字段；其余字段由通道自己维护（自愈不碰它们）。
INDEX_FIELDS = ("id", "priority", "added_at", "alias")


class AuthError(RuntimeError):
    """凭据缺失、无效或刷新失败（通道可换成自己的同名异常类）。"""


def is_valid_account_id(account_id: object) -> bool:
    """``account_id`` 是否可安全用作文件名与 UI 坐标。"""
    return (
        isinstance(account_id, str)
        and ACCOUNT_ID_RE.fullmatch(account_id) is not None
    )


def validate_account_id(account_id: str) -> str:
    """校验账号 id，非法即抛 ``ValueError``（错误信息里带原值便于排查）。"""
    if not is_valid_account_id(account_id):
        raise ValueError(f"非法账号 id: {account_id!r}")
    return account_id


def atomic_write_json(path: Path, payload: Any) -> None:
    """0600 原子写（临时文件 + rename）。

    必须先用 ``os.open(..., 0o600)`` 建临时文件再写内容：``write_text`` 会以默认
    0644 建文件，凭据（token / refresh_token）在 chmod 之前的窗口里对同机其它
    用户可读。父目录不存在时顺手建出来。
    """
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


def read_json_dict(path: Path) -> dict[str, Any]:
    """读一个 JSON 对象；文件缺失/损坏/不是对象时返回 ``{}``（不抛）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


class FileLock:
    """跨进程文件锁（``fcntl.flock``，包在 ``index.lock`` 上）。

    进程内锁只挡同进程线程；网关进程与 CLI（``buddy login <ch>``、管理页导入是
    另一个进程入口）会同时读-改-写同一个 index.json，各自拿旧快照写回就会丢更新
    （面板显示幽灵账号、顺位错乱）。flock 让读-改-写整体成为跨进程临界区。

    flock 与进程内锁可组合：同一进程重复 flock 同一文件会被内核拒绝，所以必须
    先拿进程内锁再拿文件锁（见 ``AccountStore.guard``）。
    """

    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def __enter__(self) -> "FileLock":
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


class AccountStore:
    """一个通道的账号索引 + 凭据文件。

    ``state_dir_fn`` 是 callable 而不是路径：环境变量覆盖与测试隔离点在通道里，
    且必须**每次调用时**解析（测试在本进程内 monkeypatch 环境变量）。
    """

    def __init__(
        self,
        *,
        label: str,
        state_dir_fn: Callable[[], Path],
        max_accounts: int = 8,
        lock_name: str = "index.lock",
    ) -> None:
        #: 通道名，只用于错误/告警文案（如 ``trae 账号数已达上限 8``）。
        self.label = label
        self.max_accounts = max_accounts
        #: 进程内锁。持有顺序固定：先它，再 :meth:`file_lock`。
        self.lock = threading.Lock()
        self._state_dir = state_dir_fn
        self._lock_name = lock_name

    # -- 路径 ---------------------------------------------------------------

    def state_dir(self) -> Path:
        return self._state_dir()

    def index_path(self) -> Path:
        return self.state_dir() / "index.json"

    def cred_path(self, account_id: str) -> Path:
        """单账号 cred 文件路径。id 形状在生成处已校验，这里再防一道手滑。"""
        return self.state_dir() / f"{validate_account_id(account_id)}.json"

    def file_lock(self) -> FileLock:
        return FileLock(self.state_dir() / self._lock_name)

    @contextlib.contextmanager
    def guard(self) -> Iterator[None]:
        """索引读-改-写临界区：进程内锁 + 跨进程 flock。

        不可重入（``threading.Lock`` + flock 都不重入）：临界区内部的辅助函数一律
        用 ``*_locked`` 后缀的版本，它们假定调用方已经持有本守卫。
        """
        with self.lock, self.file_lock():
            yield

    # -- 索引 ---------------------------------------------------------------

    def read_index(self) -> dict[str, Any]:
        return read_json_dict(self.index_path())

    def entries_locked(self) -> list[dict[str, Any]]:
        """索引里的原始条目（未自愈、未排序）。调用方须持有 :meth:`guard`。"""
        raw = self.read_index().get("accounts") or []
        return [e for e in raw if isinstance(e, dict)]

    def write_index(self, entries: list[dict[str, Any]]) -> None:
        atomic_write_json(self.index_path(), {"version": 1, "accounts": entries})

    # -- 凭据文件 -----------------------------------------------------------

    def load_cred(self, account_id: str, *, required_field: str = "") -> dict[str, Any] | None:
        """读单账号 cred；不存在/损坏/缺 ``required_field`` 返回 ``None``。

        ``required_field`` 是「这份凭据还能不能用」的判据（trae 的
        ``access_token``、codex 的 ``refresh_token``…）。
        """
        if not is_valid_account_id(account_id):
            return None
        try:
            data = json.loads(self.cred_path(account_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        if required_field and not str(data.get(required_field) or "").strip():
            return None
        return data

    def write_cred(self, account_id: str, cred: dict[str, Any]) -> None:
        atomic_write_json(self.cred_path(account_id), dict(cred))

    def remove_cred(self, account_id: str) -> None:
        try:
            self.cred_path(account_id).unlink()
        except (OSError, ValueError):
            pass

    # -- 枚举 ---------------------------------------------------------------

    def list_entries_locked(
        self,
        *,
        required_field: str = "",
        logger: Any = None,
    ) -> list[dict[str, Any]]:
        """自愈 + 按 ``(priority, added_at, id)`` 排序后的索引条目。

        自愈只剔除两类条目（非法 id / cred 文件缺失损坏），其余字段原样保留：
        删 cred 文件即等于退出该账号，这是管理页与 CLI 共用的一条隐式契约。
        """
        kept: list[dict[str, Any]] = []
        stale: list[str] = []
        for entry in self.entries_locked():
            account_id = str(entry.get("id") or "")
            if not is_valid_account_id(account_id):
                stale.append(account_id or "<empty>")
                continue
            if self.load_cred(account_id, required_field=required_field) is None:
                stale.append(account_id)
                continue
            kept.append(entry)
        if stale:
            self.write_index(kept)
            if logger is not None:
                for account_id in stale:
                    logger.warning(
                        "%s: 账号 %s 的凭据文件缺失或损坏，已从账号清单剔除",
                        self.label, account_id,
                    )
        kept.sort(key=_priority_key)
        return kept

    def list_entries(self, *, required_field: str = "", logger: Any = None) -> list[dict[str, Any]]:
        with self.guard():
            return self.list_entries_locked(required_field=required_field, logger=logger)

    # -- 写入 ---------------------------------------------------------------

    def upsert(self, cred: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """幂等落盘一个账号，返回索引条目（含固定字段 + 通道字段）。

        见 :meth:`upsert_locked`。
        """
        with self.guard():
            return self.upsert_locked(cred, **kwargs)

    def upsert_locked(
        self,
        cred: dict[str, Any],
        *,
        required_field: str = "",
        derive_id: Callable[[dict[str, Any]], str],
        identity_fields: tuple[str, ...] = (),
        entry_fields: Callable[
            [dict[str, Any], dict[str, Any] | None], dict[str, Any]
        ] | None = None,
        sync_cred: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
        error: type[Exception] = AuthError,
    ) -> dict[str, Any]:
        """幂等 upsert，调用方须持有 :meth:`guard`。

        匹配顺序：``account_id`` → ``identity_fields`` 中任一字段相等。命中即更新
        该条目（``priority`` / ``added_at`` / ``alias`` 保持不变——重新登录不改
        failover 顺位，也不清用户起的别名），全不命中追加为新账号（顺位排最后）。

        ``entry_fields(cred, previous)`` 返回要写进索引条目的通道字段，
        ``previous`` 是命中条目的旧值（新账号为 ``None``，用于「已有序优先」类
        合并逻辑）。``sync_cred(cred, entry)`` 在落盘前统一 cred 与条目（trae 把
        解析后的 region 写回 cred）。``cred["account_id"]`` 会就地补全，调用方
        拿到即可用。
        """
        entries = self.entries_locked()
        account_id = str(cred.get("account_id") or "")
        previous = _find_by_id(entries, account_id)
        if previous is None and identity_fields:
            previous = self._find_by_identity(
                entries, cred, identity_fields, required_field
            )
        if previous is None and not is_valid_account_id(account_id):
            # cred 没带合法 account_id（首次登录就是这种）时现场派生。派生用的
            # email/uid 是稳定字段，可能得到与既有账号相同的 id——补一次按派生
            # id 的查找，否则同一 account_id 会被追加成第二个顺位。
            cred["account_id"] = derive_id(cred)
            account_id = str(cred["account_id"])
            previous = _find_by_id(entries, account_id)

        if previous is not None:
            account_id = str(previous["id"])
            cred["account_id"] = account_id
            target = previous
        else:
            if not is_valid_account_id(account_id):
                cred["account_id"] = derive_id(cred)
                account_id = str(cred["account_id"])
            if len(entries) >= self.max_accounts:
                raise error(
                    f"{self.label} 账号数已达上限 {self.max_accounts}，"
                    f"请先在管理页删除不用的账号"
                )
            target = {
                "id": account_id,
                "priority": max(
                    (int(e.get("priority") or 0) for e in entries), default=-1
                ) + 1,
                "added_at": int(time.time()),
                "alias": "",
            }
            entries.append(target)

        if entry_fields is not None:
            target.update(entry_fields(cred, previous))
        if sync_cred is not None:
            sync_cred(cred, target)
        self.write_cred(account_id, cred)
        self.write_index(entries)
        return target

    def _find_by_identity(
        self,
        entries: list[dict[str, Any]],
        cred: dict[str, Any],
        fields: tuple[str, ...],
        required_field: str,
    ) -> dict[str, Any] | None:
        wanted = [(f, str(cred.get(f) or "").strip()) for f in fields]
        wanted = [(f, v) for f, v in wanted if v]
        if not wanted:
            return None
        for entry in entries:
            other = self.load_cred(
                str(entry.get("id") or ""), required_field=required_field
            ) or {}
            for field, value in wanted:
                if str(other.get(field) or "").strip() == value:
                    return entry
        return None

    # -- 账号操作 -----------------------------------------------------------

    def delete(self, account_id: str) -> bool:
        """移除一个账号（索引条目 + cred 文件）；返回是否真的删了。"""
        with self.guard():
            entries = self.entries_locked()
            kept = [e for e in entries if str(e.get("id") or "") != account_id]
            if len(kept) == len(entries):
                return False
            self.write_index(kept)
        self.remove_cred(account_id)
        return True

    def reorder(self, ordered_ids: list[str]) -> list[dict[str, Any]]:
        """按给定 id 顺序重写全部账号的 priority（failover 顺位）。

        入参必须是**完整**的当前账号 id 列表（少一个/多一个/重复即拒绝）；
        ``priority`` 重写为 0..n-1，``added_at`` 原样保留。重排前先自愈，避免在
        残缺索引上动刀。
        """
        with self.guard():
            entries = self.list_entries_locked()
            current = [str(e.get("id") or "") for e in entries]
            if sorted(ordered_ids) != sorted(current) or len(set(ordered_ids)) != len(
                ordered_ids
            ):
                raise ValueError(
                    f"重排必须提交完整的账号 id 列表（当前 {len(current)} 个，"
                    f"收到 {len(ordered_ids)} 个）"
                )
            rank = {aid: i for i, aid in enumerate(ordered_ids)}
            for entry in entries:
                entry["priority"] = rank[str(entry.get("id") or "")]
            self.write_index(entries)
        return list_entries_sorted(entries)

    def rename(self, account_id: str, alias: str) -> None:
        """改一个账号的本地别名（管理页 ✎）。

        只改 index.json 条目的 ``alias``：cred 文件（原始凭据）与
        priority/added_at 都不动。``alias`` 去首尾空白；空串 = 清除别名，显示
        回退默认名。账号不存在抛 ``ValueError``。
        """
        target_id = str(account_id or "")
        text = str(alias or "").strip()
        with self.guard():
            entries = self.list_entries_locked()
            target = next(
                (e for e in entries if str(e.get("id") or "") == target_id), None
            )
            if target is None:
                raise ValueError(f"账号 {target_id} 不存在")
            target["alias"] = text
            self.write_index(entries)


def _priority_key(entry: dict[str, Any]) -> tuple[int, int, str]:
    return (
        int(entry.get("priority") or 0),
        int(entry.get("added_at") or 0),
        str(entry.get("id") or ""),
    )


def list_entries_sorted(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 failover 顺位排序（``priority, added_at, id``）。"""
    return sorted(entries, key=_priority_key)


def _find_by_id(
    entries: Iterable[dict[str, Any]], account_id: str
) -> dict[str, Any] | None:
    if not account_id:
        return None
    return next(
        (e for e in entries if str(e.get("id") or "") == account_id), None
    )


def quota_epoch(accounts: Iterable[Any]) -> str:
    """账号集合指纹（``id#priority`` 逗号串）——额度缓存代。

    benefits 层的额度缓存键默认只有 provider id（TTL 300s）：加了新账号后旧的
    单账号快照还会顶满 5 分钟，前端看到的还是「一份额度」。把账号指纹拼进键里，
    账号列表一变（登录/删号/换顺位）键就变，旧快照自然失效。

    ``accounts`` 只要求元素有 ``id`` / ``priority``（各通道的 ``AccountRef`` 都
    满足），空列表给 ``"empty"``（与「拿不到账号」的 ``"unknown"`` 区分开）。
    """
    return ",".join(f"{a.id}#{a.priority}" for a in accounts) or "empty"


def quota_epoch_from(list_accounts: Callable[[], Iterable[Any]]) -> str:
    """:func:`quota_epoch` 的容错版：枚举账号抛错时返回 ``"unknown"``。

    拿不到账号列表时宁可多查一次上游，也不要用旧快照硬撑——额度面板的错值比
    一次多余查询更贵。
    """
    try:
        accounts = list_accounts()
    except Exception:  # noqa: BLE001 - 拿不到就退回常量键
        return "unknown"
    return quota_epoch(accounts)
