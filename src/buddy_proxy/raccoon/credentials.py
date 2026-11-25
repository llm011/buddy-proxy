"""商汤小浣熊（SenseTime Raccoon）多账号凭据存储。

## 结构

照 ``antigravity`` / ``kimi`` 的既有形态（多账号 index + 每账号一份 cred 文件）：

    ~~/.buddy-proxy/raccoon/
    ├── index.json          # 账号清单：[{id, name, priority, added_at, alias}]
    ├── index.lock          # fcntl 跨进程锁
    └── <account_id>.json   # 每账号一份 cred（0600）

索引与凭据文件的读写统一走 ``core/account_store.AccountStore``（0600 原子写、
进程内锁 + flock、自愈、顺位排序/重排校验）；本模块只保留 raccoon 自己的语义：
id 怎么派生、哪些字段算同一账号、索引条目里存哪些非秘密字段。

## 凭据从哪来（这是本模块最要紧的部分）

小浣熊**没有 OAuth 设备码流程**，网页端是手机号+密码登录，会话 JWT 放在
``xhx_token`` cookie 里。开源实现（Coding2API / magpie 插件）都只做了「手工
从 DevTools 复制 cookie 再粘进来」——一次性、约 30 天到期，多账号管理极痛。

本模块支持三种导入方式，按可靠度排序：

1. **手机号 + 密码**（``phone`` / ``password``）：可随时重新登录换新会话，
   多账号可以批量自动续期，不必人工介入。密码用 AES-CFB 加密落盘
   （key ``senseraccoon2023``、16 字节随机 IV 前置、Base64），与上游网页端
   同款算法——这是**明文等价物**，安全性依赖 ``~/.buddy-proxy`` 的 0600/0700
   权限边界，与其它通道的 refresh_token 同级对待。
2. **直接粘 ``xhx_token``**（``access_token``）：最省事，30 天后需重取。
   也接受裸 JWT、整段 cookie 串、``{"access_token": "..."}``。
3. **读本机桌面客户端登录态**（只读）：小浣熊桌面 App 把会话存在
   ``auth.json``，路径见 :func:`desktop_auth_path`。只读不写，不动客户端
   的 refresh token。

## 账号 id 派生

``uid`` 首选（JWT 的 ``sub``，跨重新登录稳定）；拿不到退手机号；
再退 refresh/access token 哈希。**同 uid 重新导入会就地更新既有行**
（``identity_fields``），不会长出重复账号——重复行会让同一账号被当成两个号
并发，既翻倍统计又绕过节流器按账号算的并发上限。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import pathlib
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from buddy_proxy.core.account_store import (
    ACCOUNT_ID_RE,
    AccountStore,
    is_valid_account_id,
)
from buddy_proxy.core.paths import state_dir

log = logging.getLogger(__name__)

#: 这份凭据还能不能用的判据字段。
#:
#: 用 ``access_token`` 而不是 refresh_token：小浣熊**没有 OAuth refresh 流程**
#: （网页端靠会话 cookie），能刷新的只有「手机号+密码重新登录」。所以只要有
#: access_token 就能发请求；能不能续期是另一回事（见 ``has_relogin_cred``）。
_CRED_REQUIRED_FIELD = "access_token"

#: 账号数上限：``None`` = 不限制。
_MAX_ACCOUNTS: int | None = None

#: 兼容旧名：id 形状的唯一定义已下沉到 ``core/account_store``。
_ID_RE = ACCOUNT_ID_RE


class AuthError(RuntimeError):
    """凭据不可用（缺失/过期且无法续期/登录被拒）。"""


@dataclass(frozen=True)
class AccountRef:
    """账号清单条目（不含任何秘密，可直接进 UI/health）。"""

    id: str
    name: str
    priority: int
    added_at: int
    # 本地别名（管理页 ✎ 改）：只改显示名，cred 里的 nickname 不动；
    # 空串 = 未设置（显示回退 name/id）。
    alias: str = ""


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def raccoon_state_dir() -> pathlib.Path:
    """多账号状态目录（``RACCOON_STATE_DIR`` 可覆盖；测试隔离点）。"""
    env = os.environ.get("RACCOON_STATE_DIR", "").strip()
    if env:
        return pathlib.Path(env).expanduser()
    return state_dir() / "raccoon"


def index_path() -> pathlib.Path:
    return _STORE.index_path()


def account_cred_path(account_id: str) -> pathlib.Path:
    """单账号 cred 文件路径。id 形状在生成处已校验，这里再防一道手滑。"""
    return _STORE.cred_path(account_id)


#: 账号索引 + 每账号 0600 凭据文件的共同底座。
_STORE = AccountStore(
    label="raccoon",
    state_dir_fn=raccoon_state_dir,
    max_accounts=_MAX_ACCOUNTS,
)


# ---------------------------------------------------------------------------
# account_id 派生
# ---------------------------------------------------------------------------

def derive_account_id(cred: dict[str, Any]) -> str:
    """从 cred 派生稳定账号 id；已有合法 id 原样保留。

    ``uid`` 首选（JWT 的 ``sub``，如 ``Raccoon-pu-<uuid>``；同一账号重登稳定）；
    拿不到退手机号（人可读）；再退 token 哈希——保证任何导入路径都落得了盘。
    """
    existing = str(cred.get("account_id") or "").strip()
    if is_valid_account_id(existing):
        return existing
    uid = str(cred.get("uid") or "").strip()
    if uid:
        slug = re.sub(r"[^A-Za-z0-9._@-]", "-", uid)[:64]
        if is_valid_account_id(slug):
            return slug
    phone = str(cred.get("phone") or "").strip()
    if phone:
        slug = re.sub(r"[^A-Za-z0-9._@-]", "-", phone)[:64]
        if is_valid_account_id(slug):
            return slug
    seed = str(cred.get("access_token") or cred.get("refresh_token") or "")
    digest = hashlib.sha256(seed.encode()).hexdigest()[:12]
    return f"acct-{digest}"


# ---------------------------------------------------------------------------
# 多账号 API
# ---------------------------------------------------------------------------

def list_accounts() -> list[AccountRef]:
    """枚举全部账号（按 priority 稳定排序）。

    唯一的账号枚举入口：索引自愈（cred 文件被删/损坏的条目剔除——删文件即
    退出该账号）挂在这里，forward/quota/UI 走同一个口。

    被自愈剔除的账号，它的冷却也当场清掉（含「账号\x00模型」复合键）。
    管理页删账号走 ``failover.clear_cooldown``，手工删 cred 文件走这里的
    自愈——两个入口之前不一致，后者会在 ``cooldowns.json`` 留下幽灵条目，
    直到下次重启的对账才清掉。

    做法：**自愈前**先读一次索引快照（``_STORE.entries_locked()`` 要求持有
    guard 锁，这里自己拿），与自愈后的结果对比，差集就是刚消失的账号。
    注意不能在自愈**之后**再读索引——那时索引已经被改写成自愈后的样子，
    永远对比不出差异。

    热路径（每次转发都调）：正常情况索引与存活一致，差集为空，除了多一次
    内存读索引之外不做任何事；真的有账号消失才去清冷却。
    """
    with _STORE.guard():
        before = {str(e.get("id") or "") for e in _STORE.entries_locked()}
    entries = _STORE.list_entries(required_field=_CRED_REQUIRED_FIELD, logger=log)
    alive = {str(entry.get("id") or "") for entry in entries}
    gone = before - alive
    if gone:
        # 延迟 import：failover 在模块级 import 本模块，顶层互相 import 会循环
        from . import failover

        for account_id in gone:
            failover.clear_cooldown(account_id)
    return [_account_ref(entry) for entry in entries]


def _account_ref(entry: dict[str, Any]) -> AccountRef:
    """索引条目 → 只含非秘密字段的 ``AccountRef``（UI/health 直接用）。"""
    return AccountRef(
        id=str(entry.get("id") or ""),
        name=str(entry.get("name") or ""),
        priority=int(entry.get("priority") or 0),
        added_at=int(entry.get("added_at") or 0),
        alias=str(entry.get("alias") or ""),
    )


def load_account_cred(account_id: str) -> dict[str, Any] | None:
    """读单账号 cred；没有/损坏/缺 access_token 返回 None。"""
    return _STORE.load_cred(account_id, required_field=_CRED_REQUIRED_FIELD)


def save_account_cred(cred: dict[str, Any]) -> AccountRef:
    """落盘一个账号的凭据（幂等 upsert），返回账号引用。

    匹配顺序：account_id → uid / phone / refresh_token。命中即更新该账号
    （priority/added_at/alias 不变——重新导入不改变 failover 顺位），
    全不命中追加为新账号（priority 排到最后）。

    **同 uid 重新导入会就地更新**，这是多账号管理的关键：30 天换一次 token
    时不会把同一个号堆成两条记录。
    """
    with _STORE.guard():
        return _account_ref(_save_account_cred_unlocked(cred))


def _save_account_cred_unlocked(cred: dict[str, Any]) -> dict[str, Any]:
    """``save_account_cred`` 的锁内版本（返回索引条目）。"""
    return _STORE.upsert_locked(
        cred,
        required_field=_CRED_REQUIRED_FIELD,
        derive_id=derive_account_id,
        identity_fields=("uid", "phone", "refresh_token"),
        entry_fields=_entry_fields,
        error=AuthError,
    )


def _entry_fields(cred: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """upsert 时要写进索引条目的通道字段（``previous`` 为空表示新账号）。"""
    prev = previous or {}
    return {
        "name": str(cred.get("nickname") or cred.get("phone")
                    or prev.get("name") or ""),
        "uid": str(cred.get("uid") or prev.get("uid") or ""),
    }


def delete_account(account_id: str) -> bool:
    """移除一个账号（索引条目 + cred 文件）；返回是否真的删了。"""
    return _STORE.delete(account_id)


def reorder_accounts(ordered_ids: list[str]) -> list[AccountRef]:
    """按给定 id 顺序重写全部账号的 priority（failover 顺位）。

    入参必须是**完整**的当前账号 id 列表（少一个/多一个/重复即拒绝）。
    """
    list_accounts()  # 先走唯一的枚举入口：自愈完再重排，别在残缺索引上动刀
    _STORE.reorder(ordered_ids)
    return list_accounts()


def rename_account(account_id: str, alias: str) -> list[AccountRef]:
    """改一个账号的本地别名（管理页 ✎）。空串 = 清除别名。"""
    list_accounts()
    _STORE.rename(account_id, alias)
    return list_accounts()


def has_cred() -> bool:
    return bool(list_accounts())


# ---------------------------------------------------------------------------
# token 有效期
# ---------------------------------------------------------------------------

#: 提前这个量认为 token 将过期（主动重新登录）。
_EXPIRY_SKEW_S = 600.0


def token_expiry_epoch(cred: dict[str, Any]) -> float:
    """access_token 的到期时刻（epoch 秒）；拿不到返回 0。

    优先读 cred 里显式记的 ``expires_at``（登录时从 JWT ``exp`` 解出来）；
    没有就现解 JWT；再不行返回 0（当作「不知道」，交给上游 401 判死）。
    """
    raw = cred.get("expires_at")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    exp = jwt_expiry(str(cred.get("access_token") or ""))
    return float(exp) if exp else 0.0


def jwt_expiry(token: str) -> int:
    """解 JWT 的 ``exp``（不验签，只读载荷）；解析失败返回 0。

    只用来判断「本地这个 token 大概还能活多久」，真伪由上游说了算——
    验签需要公钥，而我们从不需要信任这个 token 的内容。
    """
    parts = token.split(".")
    if len(parts) < 2:
        return 0
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict):
        return 0
    exp = data.get("exp")
    return int(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else 0


def jwt_uid(token: str) -> str:
    """解 JWT 的账号标识（``sub`` 优先，其次 ``data.id``）；解析失败返回空串。

    小浣熊的 ``xhx_token`` 里 ``sub`` 形如 ``Raccoon-pu-<uuid>``；也见过把 id
    放在 ``data.id`` 的形状，两个都认。
    """
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("sub", "user_id", "uid"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    inner = data.get("data")
    if isinstance(inner, dict):
        for key in ("id", "uid", "sub"):
            val = inner.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def access_token_valid(cred: dict[str, Any]) -> bool:
    """access_token 是否还能用（含提前量）。

    **不知道到期时间时返回 True**：没有 ``exp`` 不等于过期，交给上游 401 判死
    比本地瞎猜更准（小浣熊的 token 也可能不带 exp）。
    """
    exp = token_expiry_epoch(cred)
    if exp <= 0:
        return True
    return time.time() < exp - _EXPIRY_SKEW_S


def has_relogin_cred(cred: dict[str, Any]) -> bool:
    """这份凭据能不能自动续期（有手机号+密码 = 能重新登录）。"""
    return bool(str(cred.get("phone") or "").strip()
                and str(cred.get("password") or "").strip())


def ensure_account_token(account_id: str, *, force_refresh: bool = False
                         ) -> tuple[str, dict[str, Any]]:
    """拿该账号可用的 access_token，返回 ``(token, cred 快照)``。

    token 与 cred 同源返回：转发要 uid/device_id（账号级身份），多账号下
    「token、cred 各读一次盘」会串号。

    过期时若有手机号+密码则自动重新登录（每账号一把锁，并发只登一次）；
    没有则抛 :class:`AuthError`，由 failover 换号并提示用户重新导入。
    """
    from .login import relogin_account  # 延迟导入：login 依赖本模块

    cred = load_account_cred(account_id)
    if cred is None:
        raise AuthError(f"raccoon 账号 {account_id} 的凭据不存在或已损坏")
    if not force_refresh and access_token_valid(cred):
        return str(cred["access_token"]), dict(cred)
    if not has_relogin_cred(cred):
        raise AuthError(
            f"raccoon 账号 {account_id} 的会话已过期，且没有手机号+密码可用于自动续期；"
            "请在管理页重新导入（或补上手机号与密码以便自动续期）"
        )
    with _relogin_lock(account_id):
        # 双检：别的线程可能刚登完落盘
        cred = load_account_cred(account_id) or cred
        if force_refresh or not access_token_valid(cred):
            cred = relogin_account(account_id)
        return str(cred["access_token"]), dict(cred)


_relogin_locks: dict[str, threading.Lock] = {}
_relogin_locks_guard = threading.Lock()


def _relogin_lock(account_id: str) -> threading.Lock:
    """每账号一把重新登录锁：并发请求同时发现过期时只有一个去登录。

    上游对登录有频率限制（且每天每账号最多自动登录一次的口径），
    并发登录既浪费又会把账号推向风控。
    """
    with _relogin_locks_guard:
        return _relogin_locks.setdefault(account_id, threading.Lock())
