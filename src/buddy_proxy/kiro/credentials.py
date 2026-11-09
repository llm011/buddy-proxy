"""Kiro 凭证：多账号存储、OIDC 刷新、本地 auth 文件导入。

## 上游是什么

Kiro（原 AWS CodeWhisperer）的 IDE 客户端走两条链路：

1. **鉴权**：AWS SSO OIDC（``POST https://oidc.{region}.amazonaws.com/token``），
   body 是 **JSON**（不是 form-urlencoded——实测 form 会 ``invalid_request``）。
2. **推理**：``POST https://q.{region}.amazonaws.com/generateAssistantResponse``，
   请求头 ``x-amz-target: AmazonCodeWhispererStreamingService.GenerateAssistantResponse``，
   响应是 AWS EventStream 二进制流（**不是 SSE**，见 :mod:`.stream`）。

## 两个实测出来的硬约束（踩过）

- **``User-Agent`` 必须带 ``Kiro/<version>``**。少这个头，上游直接
  ``403 Your subscription does not support this application``——看起来像「token
  失效」或「订阅不支持」，实际只是客户端身份没对上。排查时极容易误判成账号问题。
- **请求体不能带 ``editorState``**。带了回 ``400 REQUEST_BODY_INVALID``；去掉就
  200。``profileArn`` 可带可不带（实测两者都通），这里按「有就带上」处理。

## 存储

与 antigravity / trae 同构，走 :class:`~buddy_proxy.core.account_store.AccountStore`：

    ~/.buddy-proxy/kiro/
    ├── index.json          # [{id, label, provider, priority, added_at, ...}]
    ├── index.lock
    └── <account_id>.json   # 每账号一份完整凭据（0600）

历史/外部来源的 auth 文件（Kiro IDE 自己写的
``~/.aws/sso/cache/kiro-auth-token.json``）可以**一键导入**：见
:func:`import_auth_file`。

## 账号坐标（account_id）

优先用 ``profileArn`` 里的 profile id（``arn:...:profile/KGVMAMAMGGDK`` →
``kgvmamamggdk``）——同一账号重新登录不会换坐标。没有 profileArn 时退到
``clientIdHash``，再退到 accessToken 哈希。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..core.account_store import AccountStore, atomic_write_json, read_json_dict

log = logging.getLogger(__name__)

#: 默认区域。Kiro 目前主要部署在 us-east-1；cred 里带 ``region`` 时以它为准。
DEFAULT_REGION = "us-east-1"

#: token 提前这么多久刷新。与其它通道一致，避免慢往返撞上过期瞬间。
REFRESH_BUFFER_S = 300.0

#: 上游拒绝的 User-Agent 形态（见模块头）。请求时统一拼上。
KIRO_UA_VERSION = "1.4.121"

#: Kiro IDE 自己写 auth 的位置（导入用）。
IDE_TOKEN_PATH = pathlib.Path.home() / ".aws" / "sso" / "cache" / "kiro-auth-token.json"
IDE_CLIENT_GLOB = "*.json"
SSO_CACHE_DIR = pathlib.Path.home() / ".aws" / "sso" / "cache"


def state_dir() -> pathlib.Path:
    """本通道的状态目录（``BUDDY_KIRO_STATE_DIR`` 可覆盖，测试用）。"""
    override = os.environ.get("BUDDY_KIRO_STATE_DIR")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".buddy-proxy" / "kiro"


_STORE = AccountStore(label="kiro", state_dir_fn=state_dir)
#: 兼容导出：测试直接 ``credentials._cooldowns`` 造过期条目。
_cooldowns: dict[str, tuple[float, str]] = {}

#: 每账号一把刷新锁（并发请求同时发现过期时只刷一次）。
_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _refresh_lock(account_id: str) -> threading.Lock:
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(account_id, threading.Lock())


@dataclass(frozen=True)
class KiroAccount:
    """账号清单条目（不含秘密，可直接进 UI 快照）。"""

    id: str
    label: str
    region: str
    priority: int
    added_at: int
    provider: str = "Enterprise"
    profile_arn: str = ""


# ---------------------------------------------------------------------------
# 坐标与时间
# ---------------------------------------------------------------------------


def _parse_expiry(value: Any) -> float:
    """把 ``expiresAt`` 解析成 epoch 秒；解析不出来返回 0（视为已过期）。

    上游给过两种形状：``2026-11-08T19:57:49.063Z`` 与
    ``2026-06-03T02:39:26.911969+00:00``，都按 ISO 处理（``Z`` 换成 ``+00:00``）。
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # 有些来源直接给毫秒时间戳
        return float(value) / 1000.0 if value > 1e12 else float(value)
    if not isinstance(value, str) or not value.strip():
        return 0.0
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _account_id(*, profile_arn: str, client_id_hash: str, access_token: str) -> str:
    """稳定账号坐标：profileArn → clientIdHash → accessToken 哈希。

    用 profileArn 的**尾部段**（``.../profile/KGVMAMAMGGDK``）：同一账号重新
    登录后 clientIdHash 会变（每次注册新 client），但 profile id 不变，所以
    用它当坐标才能让「重新登录」覆盖同一个账号，而不是堆积重复条目。
    """
    if profile_arn:
        tail = profile_arn.rstrip("/").rsplit("/", 1)[-1]
        cleaned = re.sub(r"[^a-z0-9]", "", tail.lower())
        if cleaned:
            return f"p-{cleaned}"
    if client_id_hash:
        cleaned = re.sub(r"[^a-z0-9]", "", client_id_hash.lower())
        if cleaned:
            return f"c-{cleaned[:20]}"
    digest = hashlib.sha256((access_token or "").encode()).hexdigest()[:20]
    return f"t-{digest}"


# ---------------------------------------------------------------------------
# OIDC 刷新
# ---------------------------------------------------------------------------


def client_secret_path(client_id_hash: str) -> pathlib.Path:
    """AWS SSO 缓存里的 client 注册文件（含 clientId/clientSecret）。"""
    return SSO_CACHE_DIR / f"{client_id_hash}.json"


def _load_client(client_id_hash: str) -> tuple[str, str]:
    """读 ``clientId`` / ``clientSecret``；读不到抛 :class:`RuntimeError`。

    这两个值由 Kiro 首次 ``login`` 时注册并缓存，刷新 token 必须用它们。
    文件缺失说明本机没登录过 Kiro，或者缓存被清了——提示要重新登录。
    """
    path = client_secret_path(client_id_hash)
    data = read_json_dict(path)
    cid = str(data.get("clientId") or "").strip()
    secret = str(data.get("clientSecret") or "").strip()
    if not cid or not secret:
        raise RuntimeError(
            f"缺少 Kiro client 凭据（{path}）。请先在 Kiro IDE / kiro-cli 里登录一次，"
            "或用「导入 auth 文件」带入完整凭据")
    return cid, secret


def refresh_tokens(cred: dict[str, Any]) -> dict[str, Any]:
    """用 refreshToken 换新 accessToken，返回更新后的 cred。

    body 必须是 **JSON**（form 会 400 ``invalid_request``，见模块头）。
    失败抛 :class:`RuntimeError`，文案区分两种根因：

    - ``invalid_grant``：refreshToken 本身失效（过期/被吊销）→ 要重新登录；
    - 其它：clientId/Secret 与 refreshToken 不配对，或网络问题。
    """
    region = str(cred.get("region") or DEFAULT_REGION)
    refresh_token = str(cred.get("refresh_token") or "").strip()
    if not refresh_token:
        raise RuntimeError("Kiro 凭据缺少 refreshToken，请重新登录")
    client_hash = str(cred.get("client_id_hash") or "").strip()
    if not client_hash:
        raise RuntimeError("Kiro 凭据缺少 clientIdHash，请重新登录")
    cid, secret = _load_client(client_hash)

    payload = json.dumps({
        "clientId": cid,
        "clientSecret": secret,
        "grantType": "refresh_token",
        "refreshToken": refresh_token,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://oidc.{region}.amazonaws.com/token",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 - 读错误体失败不影响判定
            pass
        if "invalid_grant" in body:
            raise RuntimeError(
                "Kiro refreshToken 已失效（invalid_grant）。"
                "请在 Kiro IDE 里重新登录，或用「导入 auth 文件」带入新凭据") from exc
        raise RuntimeError(f"Kiro token 刷新失败: HTTP {exc.code} {body}") from exc
    except Exception as exc:  # noqa: BLE001 - 网络/解析异常统一成可读原因
        raise RuntimeError(f"Kiro token 刷新失败: {type(exc).__name__}") from exc

    access = str(data.get("accessToken") or "").strip()
    if not access:
        raise RuntimeError("Kiro token 刷新返回里没有 accessToken")
    expires_in = data.get("expiresIn")
    try:
        lifetime = float(expires_in) if expires_in else 3600.0
    except (TypeError, ValueError):
        lifetime = 3600.0
    updated = dict(cred)
    updated["access_token"] = access
    updated["expires_at"] = time.time() + lifetime
    # 上游有时会轮换 refreshToken（给了就用新的，没给保留原值）
    rotated = str(data.get("refreshToken") or "").strip()
    if rotated:
        updated["refresh_token"] = rotated
    updated["refreshed_at"] = time.time()
    return updated


def needs_refresh(cred: dict[str, Any], *, now: float | None = None) -> bool:
    """token 是否该刷新了（提前 :data:`REFRESH_BUFFER_S` 秒）。"""
    now = time.time() if now is None else now
    expires = cred.get("expires_at")
    if not isinstance(expires, (int, float)) or isinstance(expires, bool):
        return True
    return float(expires) - REFRESH_BUFFER_S <= now


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


def _normalize_cred(raw: dict[str, Any], *, account_id: str) -> dict[str, Any]:
    """把任意来源的 auth 文件规范成本通道的 cred 形状。

    兼容 Kiro IDE 的 ``kiro-auth-token.json``（camelCase）与本通道的落盘格式
    （snake_case）。
    """
    def pick(*names: str) -> str:
        for name in names:
            value = raw.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    access = pick("accessToken", "access_token")
    refresh = pick("refreshToken", "refresh_token")
    client_hash = pick("clientIdHash", "client_id_hash")
    region = pick("region") or DEFAULT_REGION
    provider = pick("provider", "authMethod") or "Enterprise"
    profile_arn = pick("profileArn", "profile_arn")
    expires_at = raw.get("expires_at")
    if expires_at is None:
        expires_at = _parse_expiry(raw.get("expiresAt"))
    else:
        try:
            expires_at = float(expires_at)
        except (TypeError, ValueError):
            expires_at = _parse_expiry(raw.get("expiresAt"))
    return {
        "account_id": account_id,
        # label 必须显式带过来：_normalize_cred 是白名单式重建，漏掉的字段
        # 会在落盘时消失（踩过一次：导入时传的「主号」变成了一串 id）
        "label": str(raw.get("label") or "").strip(),
        "access_token": access,
        "refresh_token": refresh,
        "client_id_hash": client_hash,
        "region": region,
        "provider": provider,
        "profile_arn": profile_arn,
        "expires_at": float(expires_at or 0.0),
        "saved_at": raw.get("saved_at") or time.time(),
    }


def _entry_from_cred(cred: dict[str, Any]) -> dict[str, Any]:
    """cred → 索引条目（只放非秘密字段）。"""
    arn = str(cred.get("profile_arn") or "")
    label = str(cred.get("label") or "").strip()
    if not label:
        tail = arn.rsplit("/", 1)[-1] if arn else ""
        label = tail or str(cred.get("account_id") or "kiro")
    return {
        "id": str(cred.get("account_id") or ""),
        "label": label,
        "region": str(cred.get("region") or DEFAULT_REGION),
        "provider": str(cred.get("provider") or "Enterprise"),
        "profile_arn": arn,
        "priority": int(cred.get("priority") or 0),
        "added_at": int(cred.get("added_at") or time.time()),
    }


def load_account_cred(account_id: str) -> dict[str, Any] | None:
    """读单账号完整凭据；不存在/损坏返回 None。"""
    try:
        path = _STORE.cred_path(account_id)
    except Exception:  # noqa: BLE001 - 非法 id 视作不存在
        return None
    data = read_json_dict(path)
    if not data:
        return None
    data.setdefault("account_id", account_id)
    return data


def save_account_cred(cred: dict[str, Any]) -> KiroAccount:
    """写入/更新一个账号的凭据，返回账号快照。

    以 :func:`_account_id` 派生坐标：同一账号重新登录会覆盖同一条，不新增。
    """
    account_id = str(cred.get("account_id") or "").strip() or _account_id(
        profile_arn=str(cred.get("profile_arn") or ""),
        client_id_hash=str(cred.get("client_id_hash") or ""),
        access_token=str(cred.get("access_token") or ""),
    )
    normalized = _normalize_cred(cred, account_id=account_id)
    with _STORE.guard():
        entries = _STORE.entries_locked()
        existing = next((e for e in entries if str(e.get("id")) == account_id), None)
        if existing is not None:
            # 保留原优先位与加入时间：重新登录不改顺位
            normalized["priority"] = int(existing.get("priority") or 0)
            normalized["added_at"] = int(existing.get("added_at") or time.time())
        else:
            normalized["priority"] = int(cred.get("priority") or len(entries))
            normalized["added_at"] = int(time.time())
        atomic_write_json(_STORE.cred_path(account_id), normalized)
        entry = _entry_from_cred(normalized)
        entries = [e for e in entries if str(e.get("id")) != account_id]
        entries.append(entry)
        entries.sort(key=lambda e: (
            int(e.get("priority") or 0), int(e.get("added_at") or 0),
            str(e.get("id") or "")))
        _STORE.write_index(entries)
    log.info("kiro 账号已保存: %s (%s)", entry["label"], account_id)
    return _to_account(entry)


def _to_account(entry: dict[str, Any]) -> KiroAccount:
    return KiroAccount(
        id=str(entry.get("id") or ""),
        label=str(entry.get("label") or entry.get("id") or ""),
        region=str(entry.get("region") or DEFAULT_REGION),
        priority=int(entry.get("priority") or 0),
        added_at=int(entry.get("added_at") or 0),
        provider=str(entry.get("provider") or "Enterprise"),
        profile_arn=str(entry.get("profile_arn") or ""),
    )


def list_accounts() -> list[KiroAccount]:
    """全部账号（按 priority 稳定排序）。"""
    with _STORE.guard():
        entries = _STORE.entries_locked()
    entries.sort(key=lambda e: (
        int(e.get("priority") or 0), int(e.get("added_at") or 0),
        str(e.get("id") or "")))
    return [_to_account(e) for e in entries]


def delete_account(account_id: str) -> bool:
    """删除账号（凭据文件 + 索引条目）；不存在返回 False。"""
    with _STORE.guard():
        entries = _STORE.entries_locked()
        kept = [e for e in entries if str(e.get("id")) != account_id]
        if len(kept) == len(entries):
            return False
        _STORE.write_index(kept)
    try:
        _STORE.cred_path(account_id).unlink()
    except OSError:
        pass
    return True


def reorder_accounts(ordered_ids: list[str]) -> list[KiroAccount]:
    """按给定顺序重写 priority（failover 顺位）。"""
    with _STORE.guard():
        entries = _STORE.entries_locked()
        ids = [str(e.get("id") or "") for e in entries]
        if sorted(ordered_ids) != sorted(ids) or len(set(ordered_ids)) != len(ordered_ids):
            raise ValueError(
                f"重排必须提交完整的账号 id 列表（当前 {len(ids)} 个，收到 {len(ordered_ids)} 个）")
        rank = {aid: i for i, aid in enumerate(ordered_ids)}
        for entry in entries:
            entry["priority"] = rank[str(entry.get("id") or "")]
        _STORE.write_index(entries)
    return list_accounts()


def rename_account(account_id: str, alias: str) -> list[KiroAccount]:
    """改账号显示名（只改索引，不动凭据）。"""
    with _STORE.guard():
        entries = _STORE.entries_locked()
        hit = next((e for e in entries if str(e.get("id")) == account_id), None)
        if hit is None:
            raise ValueError(f"账号不存在: {account_id}")
        hit["label"] = alias.strip() or str(hit.get("id") or "")
        _STORE.write_index(entries)
    return list_accounts()


def ensure_account_token(account_id: str, *, force_refresh: bool = False
                         ) -> tuple[str, dict[str, Any]]:
    """取该账号可用的 accessToken，必要时刷新并落盘。返回 ``(token, cred)``。

    每账号一把锁 + 双检：并发请求同时发现过期时只有第一个去刷新，其余等它
    落盘后直接读新值，避免 refreshToken 被并发用掉（有的 OIDC 实现会轮换）。
    """
    cred = load_account_cred(account_id)
    if cred is None:
        raise RuntimeError(f"kiro 账号 {account_id} 的凭据不存在或已损坏")
    if not force_refresh and not needs_refresh(cred):
        return str(cred.get("access_token") or ""), cred
    with _refresh_lock(account_id):
        fresh = load_account_cred(account_id) or cred
        if not force_refresh and not needs_refresh(fresh):
            return str(fresh.get("access_token") or ""), fresh
        updated = refresh_tokens(fresh)
        atomic_write_json(_STORE.cred_path(account_id), updated)
        return str(updated.get("access_token") or ""), updated


# ---------------------------------------------------------------------------
# 导入本地 auth 文件
# ---------------------------------------------------------------------------


def _guess_client_hash(token_path: pathlib.Path, raw: dict[str, Any]) -> str:
    """确定 clientIdHash：优先文件里自带的，否则找同目录最近修改的 client 文件。"""
    declared = str(raw.get("clientIdHash") or raw.get("client_id_hash") or "").strip()
    if declared:
        return declared
    # 没有声明时：在 sso cache 里找「含 clientId/clientSecret」且最新的文件
    best: tuple[float, str] = (0.0, "")
    for path in SSO_CACHE_DIR.glob(IDE_CLIENT_GLOB):
        if path == token_path:
            continue
        data = read_json_dict(path)
        if not (data.get("clientId") and data.get("clientSecret")):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > best[0]:
            best = (mtime, path.stem)
    return best[1]


def import_auth_file(path: str | pathlib.Path | None = None, *,
                     label: str = "") -> KiroAccount:
    """把一份本地 Kiro auth 文件导入本通道。

    ``path`` 缺省用 Kiro IDE 的标准位置。文件可以是：

    - IDE 的 ``kiro-auth-token.json``（camelCase，可能没有 profileArn）；
    - 其它代理导出的 auth json（snake_case 也认）。

    缺 ``clientIdHash`` 时自动从 ``~/.aws/sso/cache/`` 里挑最近注册的 client
    文件——IDE 换号时会写新 client，取最新的通常就是当前账号的。
    """
    target = pathlib.Path(path) if path else IDE_TOKEN_PATH
    raw = read_json_dict(target)
    if not raw:
        raise RuntimeError(f"auth 文件不存在或无法解析: {target}")
    access = str(raw.get("accessToken") or raw.get("access_token") or "").strip()
    refresh = str(raw.get("refreshToken") or raw.get("refresh_token") or "").strip()
    if not access and not refresh:
        raise RuntimeError(f"auth 文件里没有 accessToken/refreshToken: {target}")

    client_hash = _guess_client_hash(target, raw)
    if not client_hash:
        raise RuntimeError(
            f"找不到与 {target.name} 配对的 client 凭据（~/.aws/sso/cache/*.json）。"
            "请先在 Kiro IDE 里登录一次")
    merged = dict(raw)
    merged["clientIdHash"] = client_hash
    if label:
        merged["label"] = label
    account_id = _account_id(
        profile_arn=str(merged.get("profileArn") or merged.get("profile_arn") or ""),
        client_id_hash=client_hash,
        access_token=access,
    )
    merged["account_id"] = account_id
    if label:
        merged["label"] = label
    return save_account_cred(merged)


def set_label(account_id: str, label: str) -> None:
    """只改索引里的显示名（不动凭据）。

    与 :func:`rename_account` 的区别：那个是用户手动的「改名」入口，空串表示
    恢复默认；这个由程序调用（如探活时从上游拿到邮箱），只在 label 还是
    **自动生成的占位**（``c-xxxxxxxx`` / ``p-xxxx`` 形态）时才覆盖，免得把
    用户手动设的名字冲掉。
    """
    clean = (label or "").strip()
    if not clean:
        return
    with _STORE.guard():
        entries = _STORE.entries_locked()
        hit = next((e for e in entries if str(e.get("id")) == account_id), None)
        if hit is None:
            return
        current = str(hit.get("label") or "")
        # 用户手动改过的名字优先保留：只覆盖「看起来是自动生成的」那种
        auto_like = (not current) or current == str(hit.get("id") or "") \
            or re.fullmatch(r"[cp]-[a-z0-9]{8,}", current) is not None
        if not auto_like:
            return
        hit["label"] = clean
        _STORE.write_index(entries)
