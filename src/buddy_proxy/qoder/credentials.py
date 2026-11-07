"""Qoder 凭据：多账号存储、刷新、登录（device flow）。

结构照抄 kimi/antigravity（多账号 index + 每账号一份 cred 文件）::

    ~/.buddy-proxy/qoder/
    ├── index.json              # 账号清单：[{id, email, name, region, priority, added_at}]
    ├── index.lock              # fcntl 跨进程锁
    └── <account_id>.json       # 每账号一份 cred（0600）

``QODER_STATE_DIR`` 可覆盖状态目录（测试隔离点）。

device token（``dt-``）有效期约 30 天，配 ``drt-`` refresh token 可续；本模块
在 token 临近过期时自动刷新并回写该账号的文件。

> 为什么不去偷读 CN 版桌面的 ``auth.v1.dat``：新版 Electron safeStorage
> 在本机推不出密钥（PBKDF2 的 password 已非空串），而 device flow 对
> 全球版/CN 版通用、且不依赖桌面端在跑。safeStorage 只作为**尽力而为**的
> 便捷路径保留（历史单账号迁移来源之一）。

**多账号与区域**：qoder 的 CN / 全球版账号**不通用**（连错域 401），所以
多账号只做**同区** failover——混域账号登录进来可以用（转发/额度按账号自己
的 region 打），但请求级 failover 只在同区账号间轮转。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from buddy_proxy.core.account_store import (
    AccountStore,
    atomic_write_json,
    is_valid_account_id,
)
from buddy_proxy.core.paths import state_dir

from .config import CLIENT_ID, Region, resolve_region, with_cached_endpoints

log = logging.getLogger(__name__)

#: 状态目录名。
STATE_DIR_NAME = "qoder"

#: 历史单账号状态文件（迁移源；新装不再创建）。
LEGACY_AUTH_STATE_NAME = "qoder_auth.json"

#: device token 默认有效期兜底（服务端会给 expires_at）。
DEFAULT_TTL_MS = 30 * 24 * 3600 * 1000

#: 提前多久刷新（5 分钟）。
REFRESH_MARGIN_MS = 5 * 60 * 1000

#: device flow 轮询间隔与总时长。
POLL_INTERVAL_S = 5.0
POLL_TIMEOUT_S = 600.0

#: 账号数上限：``None`` = 不限制（历史值 8，2026-11 按用户要求放开）。
_MAX_ACCOUNTS: int | None = None

#: 这份凭据还能不能用的判据字段（读 / 自愈 / upsert 共用同一个）。
_CRED_REQUIRED_FIELD = "token"


class AuthError(RuntimeError):
    """凭据缺失、无效或刷新失败。"""


@dataclass
class Credential:
    """一条可用的 Qoder 凭据（单账号快照；多账号下由 cred dict 互转）。"""

    token: str
    uid: str = ""
    machine_id: str = ""
    refresh_token: str = ""
    expires_at_ms: int = 0
    name: str = ""
    email: str = ""
    region: str = "cn"
    plan: str = ""
    source: str = "state"  # state | env | desktop
    extra: dict = field(default_factory=dict)

    def is_expired(self, margin_ms: int = REFRESH_MARGIN_MS) -> bool:
        """是否已过期或即将过期（无 expires_at 时视为未过期）。"""
        if not self.expires_at_ms:
            return False
        return self.expires_at_ms - margin_ms <= int(time.time() * 1000)

    def describe(self) -> str:
        """脱敏描述（只进日志）。"""
        masked = f"{self.token[:6]}…{self.token[-3:]}" if self.token else ""
        return (
            f"region={self.region} source={self.source} uid={self.uid or '?'} "
            f"token={masked} plan={self.plan or '?'}"
        )


@dataclass(frozen=True)
class AccountRef:
    """账号清单条目（不含任何秘密，可直接进 UI/health）。"""

    id: str
    email: str
    name: str
    region: str
    priority: int
    added_at: int
    # 本地别名（管理页 ✎ 改）：只改显示名，原始凭据里的 name/email 不动；
    # 空串 = 未设置（显示回退 email/id）。upsert/重登不碰它。
    alias: str = ""


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def qoder_state_dir() -> Path:
    """多账号状态目录（``QODER_STATE_DIR`` 可覆盖；测试隔离点）。"""
    env = os.environ.get("QODER_STATE_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return state_dir() / STATE_DIR_NAME


def index_path() -> Path:
    return _STORE.index_path()


def account_cred_path(account_id: str) -> Path:
    """单账号 cred 文件路径。id 形状在生成处已校验，这里再防一道手滑。"""
    return _STORE.cred_path(account_id)


def legacy_auth_state_path() -> Path:
    """历史单账号状态文件路径（迁移源）。"""
    override = os.environ.get("QODER_AUTH_FILE")
    if override:
        return Path(override).expanduser()
    return state_dir() / LEGACY_AUTH_STATE_NAME


# ---------------------------------------------------------------------------
# 索引读写（全部在 _index_lock + 跨进程 flock 下进行）
# ---------------------------------------------------------------------------
#
# 这层已下沉到 ``core/account_store``（trae/qoder/codex 三处逐字重复）：0600 原子
# 写、进程内锁 + flock、自愈、priority 排序与重排校验只有一份实现。下面这些名
# 字是旧内部调用点（``provider.py`` 回填套餐名、测试）的兼容入口。

#: 账号索引 + 每账号 0600 凭据文件的共同底座。
_STORE = AccountStore(
    label=STATE_DIR_NAME,
    state_dir_fn=qoder_state_dir,
    max_accounts=_MAX_ACCOUNTS,
)

_index_lock = _STORE.lock
_index_file_lock = _STORE.file_lock
_atomic_write_json = atomic_write_json


def _read_index() -> dict[str, Any]:
    return _STORE.read_index()


# ---------------------------------------------------------------------------
# 历史单账号迁移（qoder_auth.json → 账号 #1）
# ---------------------------------------------------------------------------

_LEGACY_MIGRATE_FLAG = ".legacy-migrated"


def _migrate_legacy_unlocked() -> None:
    """把历史单账号 ``qoder_auth.json`` 迁移成账号 #1（幂等）。

    与 antigravity 同款：迁移是**追加**而不是替换——已有多账号的用户重跑不会
    把登录态重置回旧文件。只迁一次（落一个标记文件），避免状态文件后来又被
    写坏时反复把旧值顶回来。
    """
    flag = qoder_state_dir() / _LEGACY_MIGRATE_FLAG
    if flag.exists():
        return
    path = legacy_auth_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if isinstance(data, dict):
        token = str(data.get("token") or "").strip()
        if token:
            cred = {
                "token": token,
                "uid": str(data.get("uid") or ""),
                "machine_id": str(data.get("machine_id") or ""),
                "refresh_token": str(data.get("refresh_token") or ""),
                "expires_at_ms": int(data.get("expires_at_ms") or 0),
                "name": str(data.get("name") or ""),
                "email": str(data.get("email") or ""),
                "region": str(data.get("region") or "") or resolve_region().key,
                "plan": str(data.get("plan") or ""),
                "source": "state",
            }
            _save_account_cred_unlocked(cred)
            log.info("qoder: 已把历史单账号状态 %s 迁移为账号 %s",
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

    email 首选（qoder device flow 会带回邮箱，天然唯一且跨重登稳定）；拿不到
    退 uid；再退 ``acct-<sha256(token)[:12]>``——保证任何导入路径都落得了盘。
    """
    existing = str(cred.get("account_id") or "").strip()
    if existing and is_valid_account_id(existing):
        return existing
    email = str(cred.get("email") or "").strip()
    if email and is_valid_account_id(email):
        return email
    uid = str(cred.get("uid") or "").strip()
    if uid and is_valid_account_id(uid):
        return uid
    digest = hashlib.sha256(str(cred.get("token") or "").encode()).hexdigest()[:12]
    return f"acct-{digest}"


# ---------------------------------------------------------------------------
# Credential <-> cred dict 互转
# ---------------------------------------------------------------------------


def credential_to_cred(cred: Credential) -> dict[str, Any]:
    """``Credential`` 快照 -> 落盘 cred dict。"""
    return {
        "account_id": str(getattr(cred, "account_id", "") or ""),
        "token": cred.token,
        "uid": cred.uid,
        "machine_id": cred.machine_id,
        "refresh_token": cred.refresh_token,
        "expires_at_ms": cred.expires_at_ms,
        "name": cred.name,
        "email": cred.email,
        "region": cred.region,
        "plan": cred.plan,
        "source": cred.source,
    }


def cred_to_credential(data: dict[str, Any]) -> Credential:
    """cred dict -> ``Credential`` 快照。"""
    return Credential(
        token=str(data.get("token") or ""),
        uid=str(data.get("uid") or ""),
        machine_id=str(data.get("machine_id") or ""),
        refresh_token=str(data.get("refresh_token") or ""),
        expires_at_ms=int(data.get("expires_at_ms") or 0),
        name=str(data.get("name") or ""),
        email=str(data.get("email") or ""),
        region=str(data.get("region") or "") or "cn",
        plan=str(data.get("plan") or ""),
        source=str(data.get("source") or "state"),
    )


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
        email=str(entry.get("email") or ""),
        name=str(entry.get("name") or ""),
        region=str(entry.get("region") or "") or "cn",
        priority=int(entry.get("priority") or 0),
        added_at=int(entry.get("added_at") or 0),
        alias=str(entry.get("alias") or ""),
    )


def load_account_cred(account_id: str) -> dict[str, Any] | None:
    """读单账号 cred；没有/损坏/缺 token 返回 None。"""
    return _STORE.load_cred(account_id, required_field=_CRED_REQUIRED_FIELD)


def save_account_cred(cred: dict[str, Any]) -> AccountRef:
    """落盘一个账号的凭据（幂等 upsert），返回账号引用。

    匹配顺序：account_id → email → refresh_token。命中即更新该账号
    （priority/added_at 不变——重新登录不改变 failover 顺位），全不命中追加为
    新账号（priority 排到最后）。cred 缺 account_id 时现场生成并写回原 dict。
    """
    with _STORE.guard():
        return _account_ref(_save_account_cred_unlocked(cred))


def _save_account_cred_unlocked(cred: dict[str, Any]) -> dict[str, Any]:
    """``save_account_cred`` 的锁内版本（历史迁移也走这里，返回索引条目）。"""
    return _STORE.upsert_locked(
        cred,
        required_field=_CRED_REQUIRED_FIELD,
        derive_id=derive_account_id,
        identity_fields=("email", "refresh_token"),
        entry_fields=_entry_fields,
        error=AuthError,
    )


def _entry_fields(cred: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """upsert 时要写进索引条目的通道字段（``previous`` 为空表示新账号）。"""
    prev = previous or {}
    region = str(cred.get("region") or "")
    if previous is None:
        # 新账号：没带 region 就按当前解析出的默认区（与改造前一致）
        region = region or resolve_region().key
    else:
        # 重登同号：已有账号的 region 优先，别让一次没带 region 的刷新把
        # 海外账号改判成 CN
        region = region or str(prev.get("region") or "") or "cn"
    return {
        "email": str(cred.get("email") or prev.get("email") or ""),
        "name": str(cred.get("name") or prev.get("name") or ""),
        "region": region,
    }


def delete_account(account_id: str) -> bool:
    """移除一个账号（索引条目 + cred 文件）；返回是否真的删了。"""
    return _STORE.delete(account_id)


def reorder_accounts(ordered_ids: list[str]) -> list[AccountRef]:
    """按给定 id 顺序重写全部账号的 priority（failover 顺位）。

    入参必须是**完整**的当前账号 id 列表（少一个/多一个/重复即拒绝）；
    priority 重写为 0..n-1，added_at 原样保留。返回重排后的 list_accounts()。
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
# 桌面端 credential 复用（尽力而为）——历史迁移/兜底来源
# ---------------------------------------------------------------------------

#: 桌面端凭据文件（Electron safeStorage 加密）。
_DESKTOP_AUTH_RELPATH = "Library/Application Support/{bundle}/auth.v1.dat"

#: 各区域的桌面端 bundle id。
_DESKTOP_BUNDLES = {"global": "com.qoder.app.stable", "cn": "com.qodercn.app.stable"}

#: 候选解密口令（Electron safeStorage 在各平台的默认值）。
_SAFESTORAGE_PASSWORDS = (b"", b"peanuts", b"Chrome Safe Storage")


def _decrypt_safestorage(blob: bytes) -> bytes | None:
    """尝试解 Electron safeStorage（macOS：PBKDF2-HMAC-SHA1 + AES-128-CBC）。

    失败返回 ``None``——新版 Electron 的口令不再是空串，本机可能解不开，
    调用方需容忍（这正是 device flow 存在的意义）。
    """
    try:
        from buddy_proxy.core.aes_pure import aes_cbc_decrypt
    except ImportError:  # pragma: no cover - 环境异常
        return None
    for password in _SAFESTORAGE_PASSWORDS:
        try:
            key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
            out = aes_cbc_decrypt(key, b" " * 16, blob)
        except Exception:  # noqa: BLE001 - 解密失败属预期
            continue
        if out[:1] in (b"{", b'"'):
            return out
    return None


def _from_desktop(region: Region) -> Credential | None:
    """从 Qoder 桌面端登录态里取 device token（解不开就返回 None）。"""
    bundle = _DESKTOP_BUNDLES.get(region.key)
    if not bundle:
        return None
    path = Path.home() / _DESKTOP_AUTH_RELPATH.format(bundle=bundle)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    # "v10" magic 后紧跟密文；有的版本第 4 字节是密文首字节，故两种偏移都试。
    for offset in (3, 4):
        plain = _decrypt_safestorage(raw[offset:])
        if plain is None:
            continue
        # PKCS#7 去填充
        pad = plain[-1]
        if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
            plain = plain[:-pad]
        try:
            data = json.loads(plain.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        token = _find_token(data)
        if not token:
            continue
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        return Credential(
            token=token,
            uid=str(user.get("id") or user.get("uid") or ""),
            machine_id=_machine_id(region),
            refresh_token=str(data.get("refreshToken") or ""),
            expires_at_ms=int(data.get("expiresAt") or 0),
            name=str(user.get("name") or ""),
            email=str(user.get("email") or ""),
            region=region.key,
            source="desktop",
        )
    return None


#: 明显不是访问令牌的键：头像/缩略图一类的 URL 也可能以 ``dt-`` 开头
#: （桌面端把头像存在 ``dt-avataruid-thumbnail`` 这类资源名上），按值前缀
#: 盲扫会把它们当成 token 写进状态文件，表现为「登录了但一调就 401」。
_TOKEN_KEY_DENYLIST = (
    "avatar", "thumbnail", "icon", "image", "photo", "logo", "url",
    "pic", "gravatar", "head", "face", "banner", "cover",
)

#: device token ``dt-`` 之后的**最少**字符数。真实令牌是不透明长串；比这短的
#: （如 ``dt-1``、``dt-x``）几乎一定是别的东西。取 8 是**保守**值——宁可放进
#: 一个可疑值让后续 401 暴露问题，也不要卡掉真令牌（`dt-real-token` 就是 10）。
_MIN_TOKEN_BODY = 8


def _looks_like_token(value: str) -> bool:
    """粗判 ``dt-`` 开头的串是否**像**一个访问令牌。

    键名黑名单挡不住全部——换成 ``userPic`` / ``gravatar`` 这类没进名单的
    键名就漏了。所以再按值的形态筛一道：令牌是单段不透明编码，而头像/资源名
    要么带文件后缀（``-thumbnail.png``）、要么带路径或空格。

    注意 ``-`` **不算**否定信号：``dt-avataruid-thumbnail`` 和
    ``dt-real-token`` 都含 ``-``，只能靠键名黑名单区分——所以真正的防线是
    黑名单，这里只兜「明显是路径/文件名/过短」这几种。
    """
    if not value.startswith("dt-"):
        return False
    body = value[3:]
    if len(body) < _MIN_TOKEN_BODY:
        return False
    # 文件后缀/路径或空白 → 是 URL/资源名，不是令牌
    if "." in body or "/" in body or any(c.isspace() for c in body):
        return False
    return True


def _find_token(node: object, _depth: int = 0) -> str:
    """在嵌套结构里找第一个像访问令牌的 ``dt-`` 字符串。

    两道筛：键名黑名单（跳过头像/图片类字段）+ 形态判断
    （:func:`_looks_like_token`）。
    """
    if _depth > 6:
        return ""
    if isinstance(node, str):
        return node if _looks_like_token(node) else ""
    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key).lower()
            if any(bad in name for bad in _TOKEN_KEY_DENYLIST):
                continue
            found = _find_token(value, _depth + 1)
            if found:
                return found
        return ""
    if isinstance(node, list):
        for value in node:
            found = _find_token(value, _depth + 1)
            if found:
                return found
    return ""


# ---------------------------------------------------------------------------
# machine_id
# ---------------------------------------------------------------------------


def machine_id(region: Region) -> str:
    """读该区域的 machine_id（桌面端/CLI 都会写，缺失则现造一个）。"""
    return _machine_id(region)


def _machine_id(region: Region) -> str:
    path = Path.home() / region.config_dirname / ".auth" / "machine_id"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    return _generate_machine_id()


def _generate_machine_id() -> str:
    """按桌面端同款风格造一个 machine_id（分组 UUID 形态）。"""
    import uuid

    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 刷新与获取（转发主路径）
# ---------------------------------------------------------------------------


def _parse_token_response(body: dict, base: Credential | None = None) -> Credential:
    """把 deviceToken 各端点的响应统一成 ``Credential``。"""
    body = body if isinstance(body, dict) else {}
    token = str(body.get("token") or body.get("device_token") or body.get("access_token") or "")
    expires_at_ms = _to_ms(body.get("expires_at") or body.get("expiresAt"))
    if not expires_at_ms:
        expires_in = body.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            expires_at_ms = int(time.time() * 1000) + int(expires_in) * 1000
        else:
            expires_at_ms = int(time.time() * 1000) + DEFAULT_TTL_MS
    return Credential(
        token=token,
        uid=str(body.get("user_id") or body.get("uid") or (base.uid if base else "")),
        machine_id=str(body.get("machine_id") or (base.machine_id if base else "")),
        refresh_token=str(
            body.get("refresh_token") or body.get("refreshToken") or (base.refresh_token if base else "")
        ),
        expires_at_ms=expires_at_ms,
        name=str(body.get("name") or (base.name if base else "")),
        email=str(body.get("email") or (base.email if base else "")),
        region=str((base.region if base else "") or ""),
        source=str(base.source if base else "state"),
    )


def _to_ms(value: object) -> int:
    """把时间戳（秒/毫秒/RFC3339）统一成毫秒。"""
    if isinstance(value, (int, float)) and value > 0:
        # 10 位数是秒，13 位是毫秒
        return int(value * 1000) if value < 1e11 else int(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def refresh_account_cred(cred: dict[str, Any], region: Region | None = None) -> dict[str, Any]:
    """用 refresh token 换新 device token，更新并写回该账号的文件。

    并发场景下**先抢每账号锁再重读盘**：等锁的请求进来时，前面那个多半已经
    刷新完并落盘了，此时直接复用新 token 返回，不再拿旧 refresh_token 打第二次
    （重放会被上游判重）。锁内覆盖「刷新 + 回写」整段：同步 IO 持锁阻塞无碍
    （调用方在 ``asyncio.to_thread`` 里跑，不卡事件循环）。

    刷新后回写前再重读盘：账号已被删就别把它"复活"；发现 RT 已被换掉（刚导入
    的新凭据）就整份放弃回写，免得拿旧快照把 uid/machine_id 全退回旧值。
    """
    aid = str(cred.get("account_id") or "")
    lock = _refresh_lock(aid)
    # 刷新 + 回写整体在锁内：同步 IO，持锁期间本线程阻塞（调用方在
    # asyncio.to_thread 里跑，不会卡事件循环）。回写若在锁外，并发第二个
    # 线程进锁时第一个还没落盘，双检会漏、refresh 重放。
    with lock:
        fresh = _reload_if_rotated(cred)
        if fresh is not None:
            return fresh
        reg = with_cached_endpoints(region or resolve_region(cred.get("region")))
        rt = str(cred.get("refresh_token") or "")
        if not rt:
            raise AuthError("缺少 refresh_token，无法刷新；请重新 `buddy login qoder`")
        payload = {"refresh_token": rt}
        try:
            resp = httpx.post(
                reg.device_refresh_url(),
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"刷新请求失败: {exc}") from exc
        if resp.status_code != 200:
            raise AuthError(f"刷新失败 HTTP {resp.status_code}: {resp.text[:200]}")
        data = _parse_token_response(resp.json(), cred_to_credential(cred))
        data.source = "state"
        data.region = reg.key
        new_cred = credential_to_cred(data)
        new_cred["account_id"] = aid
        # 刷新是网络往返（最长 30 秒），期间用户可能在面板删了这个号或导入了
        # 新凭据——回写前重读盘，别复活已删账号、也别用旧快照回滚新导入。
        with _index_lock, _index_file_lock():
            current = load_account_cred(aid)
            if current is None:
                log.debug("qoder: 刷新期间账号 %s 已被删除，放弃回写", aid)
                new_cred["refresh_token"] = new_cred["refresh_token"] or rt
                return new_cred
            if current.get("refresh_token") != cred.get("refresh_token"):
                log.debug("qoder: 刷新期间账号 %s 凭据已变更，放弃整份回写", aid)
                new_cred["refresh_token"] = new_cred["refresh_token"] or rt
                return new_cred
            _atomic_write_json(account_cred_path(aid), new_cred)
    log.info("qoder token 已刷新 (%s)", data.describe())
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
    fresh_cred = cred_to_credential(state)
    if fresh_cred.is_expired():
        return None
    log.debug("qoder token 已由并发请求刷新，复用新值 (%s)", fresh_cred.describe())
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
    """拿该账号可用的 device token，返回 ``(token, cred 快照 dict)``。

    cred 与 token 同源是本函数存在的理由：转发要 uid/machine_id（COSY 签名
    的账号级身份），多账号下「token、cred 各读一次盘」会串号。刷新在每账号
    锁内做并重读盘双检——别的线程刚刷完落盘就不再刷。
    """
    cred = load_account_cred(account_id)
    if cred is None:
        raise AuthError(f"qoder 账号 {account_id} 的凭据不存在或已损坏")
    if not force_refresh and not cred_to_credential(cred).is_expired():
        return cred["token"], dict(cred)
    refreshed = refresh_account_cred(cred)
    return refreshed["token"], dict(refreshed)


def ensure_account_credential(account_id: str, *, force_refresh: bool = False) -> Credential:
    """``ensure_account_token`` 的 ``Credential`` 形态（campaigns/catalog 用）。"""
    _, cred = ensure_account_token(account_id, force_refresh=force_refresh)
    return cred_to_credential(cred)


# ---------------------------------------------------------------------------
# userinfo：账号身份（name/email）回填
# ---------------------------------------------------------------------------

#: userinfo 回填互斥锁（per 进程）：只保护 done 标记的读写，网络往返在锁外。
_userinfo_lock = threading.Lock()
#: 本进程内已回填过身份的账号（并发去重；重启后重查一次也无害）。
_userinfo_done: set[str] = set()


def fetch_userinfo(account_id: str) -> dict[str, Any]:
    """查该账号的 userinfo（``/api/v1/userinfo``），返回脱敏后的身份字段。

    deviceToken 各端点的回包里**没有** name/email（实测 2026-10-08），登录链
    路因此一直拿不到账号身份——config 里早定义的 userinfo 端点从没人调用。
    返回 ``{"name": ..., "email": ..., "username": ...}``；HTTP/解析失败抛
    AuthError，调用方决定吞还是记。

    端点按**账号自己的 region** 解析：resolve_region() 不带参落默认区（CN），
    海外账号的 token 打到 CN 域 401 TOKEN_EXPIRE——token 有效，域不对
    （#132 合并后真机验证暴露）。
    """
    raw = load_account_cred(account_id) or {}
    # 从**原始 dict** 判空：cred_to_credential 会把空 region 归一成 "cn"，
    # 经它转一手就永远看不见空值了。
    region = str(raw.get("region") or "").strip()
    if not region:
        # 不静默回退默认区：region 为空多半是凭据损坏，按默认区打只会
        # 换一种 401，排查时还被误导。显式抛错，日志里能看出是哪个号。
        raise AuthError(f"qoder 账号 {account_id[:8]} 没有 region 字段，userinfo 无法定位端点")
    reg = with_cached_endpoints(resolve_region(region))
    token, _ = ensure_account_token(account_id)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        resp = httpx.get(reg.userinfo_url(), headers=headers, timeout=20)
    except httpx.HTTPError as exc:
        raise AuthError(f"userinfo 查询失败: {exc}") from exc
    if resp.status_code != 200:
        raise AuthError(f"userinfo 查询失败 HTTP {resp.status_code}: {resp.text[:160]}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise AuthError("userinfo 响应不是 JSON") from exc
    if not isinstance(body, dict):
        raise AuthError("userinfo 响应结构异常")
    return {
        "name": str(body.get("name") or ""),
        "email": str(body.get("email") or ""),
        "username": str(body.get("username") or ""),
    }


def backfill_identity(account_id: str, *, only_if_missing: bool = True) -> dict[str, Any] | None:
    """拉 userinfo 并回填该账号的 ``name``/``email``（索引 + cred 文件）。

    ``only_if_missing``：已有 name 或 email 的账号不动（避免每次查询都打
    上游；改名走 alias，原始字段回填一次就够）。返回回填的身份 dict，
    已有身份/查询失败返回 None（失败只 log——身份回填不值得让签到/额度
    查询跟着失败）。

    互斥锁只包「已回填？」的 check-and-mark（`_userinfo_done` 进程内标记），
    网络往返在锁外——多账号并发懒回填时各自独立打上游，不串行排队
    （一次 20s 超时 × N 个号叠在锁里会把整轮快照拖到分钟级）。
    """
    with _userinfo_lock:
        if account_id in _userinfo_done and only_if_missing:
            return None
        cred = load_account_cred(account_id)
        if cred is None:
            return None
        if only_if_missing and (str(cred.get("name") or "") or str(cred.get("email") or "")):
            _userinfo_done.add(account_id)
            return None
    try:
        info = fetch_userinfo(account_id)
    except AuthError as exc:
        log.debug("qoder userinfo 回填跳过（%s）: %s", account_id, exc)
        return None
    name, email = info.get("name") or "", info.get("email") or ""
    if not name and not email:
        return None
    fresh = load_account_cred(account_id)
    if fresh is not None:
        fresh["name"] = name or str(fresh.get("name") or "")
        fresh["email"] = email or str(fresh.get("email") or "")
        save_account_cred(fresh)
    log.info("qoder 账号 %s 身份已回填: name=%s", account_id[:8], name or "(空)")
    with _userinfo_lock:
        _userinfo_done.add(account_id)
    return info


# ---------------------------------------------------------------------------
# 登录（device flow / PKCE）
# ---------------------------------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    """生成 (verifier, challenge)；challenge = base64url(sha256(verifier))。"""
    # 官方用 43~128 位、字符集为 base64 字母表的 verifier。
    length = 43 + secrets.randbelow(86)
    verifier = "".join(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"[
            secrets.randbelow(64)
        ]
        for _ in range(length)
    )
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


@dataclass
class DeviceFlow:
    """一次待授权的 device flow。"""

    auth_url: str
    verifier: str
    nonce: str
    machine_id: str
    region: str

    def format(self) -> str:
        return self.auth_url


def start_device_flow(region: Region | None = None) -> DeviceFlow:
    """构造授权 URL（用户在自己的 Qoder 账号下打开并确认）。"""
    reg = with_cached_endpoints(region or resolve_region())
    verifier, challenge = _pkce_pair()
    nonce = _uuid()
    mid = _machine_id(reg)
    params = urllib.parse.urlencode(
        {
            "challenge": challenge,
            "challenge_method": "S256",
            "nonce": nonce,
            "machine_id": mid,
            "client_id": CLIENT_ID,
        }
    )
    return DeviceFlow(
        auth_url=f"{reg.auth_url()}?{params}",
        verifier=verifier,
        nonce=nonce,
        machine_id=mid,
        region=reg.key,
    )


def poll_device_flow(
    flow: DeviceFlow,
    *,
    timeout_s: float = POLL_TIMEOUT_S,
    interval_s: float = POLL_INTERVAL_S,
    on_tick: object = None,
) -> Credential:
    """轮询直到用户在浏览器里完成授权（纯同步，登录 CLI 在线程/进程里跑）。

    服务端语义：``404`` = 尚未授权（继续等），``200`` = 成功并带回 token。
    成功后返回 ``Credential``（**不**落盘——落盘走 :func:`save_account_cred`，
    由 login 层决定账号坐标与提示）。
    """
    reg = with_cached_endpoints(resolve_region(flow.region))
    params = urllib.parse.urlencode(
        {"nonce": flow.nonce, "verifier": flow.verifier, "challenge_method": "S256"}
    )
    url = f"{reg.device_poll_url()}?{params}"
    deadline = time.monotonic() + timeout_s

    with httpx.Client(timeout=30) as client:
        while time.monotonic() < deadline:
            try:
                resp = client.get(url, headers={"Accept": "application/json"})
            except httpx.HTTPError as exc:
                log.debug("qoder device poll 网络异常，重试: %s", exc)
                time.sleep(interval_s)
                continue
            if resp.status_code == 200:
                data = _parse_token_response(resp.json())
                data.machine_id = data.machine_id or flow.machine_id
                data.region = reg.key
                data.source = "state"
                return data
            if resp.status_code != 404:
                raise AuthError(f"授权轮询失败 HTTP {resp.status_code}: {resp.text[:200]}")
            if callable(on_tick):
                on_tick()
            time.sleep(interval_s)

    raise AuthError("授权超时（10 分钟）：请重新执行 `buddy login qoder`")


def _uuid() -> str:
    import uuid

    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 辅助：从 PAT（pt-）换 device token
# ---------------------------------------------------------------------------


def exchange_personal_token(personal_token: str, region: Region | None = None) -> Credential:
    """用个人访问令牌（``pt-``）换 device token，并落盘为账号。"""
    reg = with_cached_endpoints(region or resolve_region())
    mid = _machine_id(reg)
    payload = {
        "personal_token": personal_token,
        "machine_id": mid,
        "machine_token": mid,
        "machine_type": 5,
    }
    try:
        resp = httpx.post(
            reg.job_token_exchange_url(),
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise AuthError(f"PAT 换取失败: {exc}") from exc
    if resp.status_code != 200:
        raise AuthError(f"PAT 换取失败 HTTP {resp.status_code}: {resp.text[:200]}")
    cred = _parse_token_response(resp.json())
    cred.machine_id = cred.machine_id or mid
    cred.region = reg.key
    cred.source = "state"
    return cred


# ---------------------------------------------------------------------------
# 兼容薄壳：旧单账号模块级名字（web/ui/channels.py 等少量引用点）
# ---------------------------------------------------------------------------


def auth_state_path() -> Path:
    """兼容：历史单账号状态文件路径（迁移源）。"""
    return legacy_auth_state_path()
