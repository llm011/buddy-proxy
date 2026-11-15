"""GitHub Copilot 账号凭据：多账号存储 + 用户名/GitHub 身份探测。

多账号存储沿用全仓统一底座（``core/account_store.AccountStore``）：

    ~/.buddy-proxy/copilot/
    ├── index.json              # 账号清单：[{id, login, priority, added_at, alias}]
    ├── index.lock              # fcntl 跨进程锁
    └── <account_id>.json       # 每账号一份 cred（0600）

单账号 cred 文件格式：

    {
      "account_id": "jsongo",
      "access_token": "gho_...",     # GitHub OAuth token，直接作 Copilot Bearer
      "login": "jsongo",             # GitHub 用户名（派生 id、UI 展示）
      "name": "凌艺宾",               # 显示名（可空）
      "source": "gh",                # gh CLI / device flow，仅作排查线索
      "added_at": 1730000000
    }

**刻意不做 token 刷新**：``gho_`` token 长期有效，``/copilot_internal/v2/token``
换票接口对个人订阅已 403。401 只有一个含义——token 被撤销或过期，此时任何
自动刷新都是幻觉，只能让用户重新登录。所以 ``ensure_account_token`` 只做
「读盘 + 校验存在」，不做网络请求。

**为什么 id 用 GitHub login**：登录名是人可读的稳定坐标（改名不改 id 的
场景罕见，真发生了 device flow 重新登录会派生新 id，旧条目自愈剔除）。
``gh auth token --user X`` 拿到的 token 通过 ``/user`` 回查得到 login，
所以「用哪个 gh 账号登录」与「我们记哪个 id」由同一个事实源决定。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from buddy_proxy.core.account_store import AccountStore, is_valid_account_id
from buddy_proxy.core.paths import state_dir

log = logging.getLogger(__name__)

#: GitHub OAuth token 是长期凭据；这里只定义「哪些字段算同一账号」。
#: （全仓其它通道用 refresh_token / email；copilot 用 login / token 哈希。）
_CRED_REQUIRED_FIELD = "access_token"

#: 账号数上限：``None`` = 不限制（与其它通道口径一致）。
_MAX_ACCOUNTS: int | None = None

#: public GitHub REST API；只用来把 token 换成身份（login / name）。
_API_BASE = "https://api.github.com"
_API_USER_AGENT = "buddy-proxy-copilot/1"

#: gh CLI 可选路径覆盖：非交互 shell 的 PATH 通常不带 homebrew bin 目录。
_GH_BIN = "gh"


class AuthError(RuntimeError):
    """凭据缺失或无效（401 被拒、token 格式不对、无法确定 GitHub 身份）。"""


@dataclass(frozen=True)
class AccountRef:
    """账号清单条目（不含秘密，可直接进 UI/health）。

    字段口径对齐全仓（``name`` 是账号本身的名字、``alias`` 是本地起的别名）：
    UI/``quota_epoch``/``diagnose_available`` 都只认 ``id``/``name``/``alias``。
    """

    id: str
    name: str
    priority: int
    added_at: int
    #: 本地别名（管理页 ✎ 改）：空串 = 未设置，显示回退 name/id。
    alias: str = ""

    @property
    def display(self) -> str:
        return self.alias or self.name or self.id


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def copilot_state_dir() -> pathlib.Path:
    """多账号状态目录（``COPILOT_STATE_DIR`` 可覆盖；测试隔离点）。"""
    env = os.environ.get("COPILOT_STATE_DIR", "").strip()
    if env:
        return pathlib.Path(env).expanduser()
    return state_dir() / "copilot"


_STORE = AccountStore(
    label="copilot",
    state_dir_fn=copilot_state_dir,
    max_accounts=_MAX_ACCOUNTS,
)


def index_path() -> pathlib.Path:
    return _STORE.index_path()


def account_cred_path(account_id: str) -> pathlib.Path:
    return _STORE.cred_path(account_id)


# ---------------------------------------------------------------------------
# account_id 派生
# ---------------------------------------------------------------------------

def _derive_account_id(cred: dict[str, Any]) -> str:
    """从 cred 派生稳定账号 id；已有合法 id 原样保留。

    login 是首选（人可读）；GitHub 登录名本来就只含字母数字与 ``-``，
    直接可用。拿不到 login（``/user`` 失败）退回 token 哈希——token 不轮换，
    同一账号哈希稳定。
    """
    existing = str(cred.get("account_id") or "").strip()
    if is_valid_account_id(existing):
        return existing
    login = str(cred.get("login") or "").strip().lower()
    if login:
        slug = re.sub(r"[^a-z0-9._@-]", "_", login)[:64]
        if is_valid_account_id(slug):
            return slug
        return "u_" + slug
    digest = hashlib.sha256(str(cred.get("access_token") or "").encode()).hexdigest()[:12]
    return f"acct-{digest}"


def token_fingerprint(token: str) -> str:
    """token 的短指纹（日志/错误提示里标识「哪一把钥匙」用，不可逆）。"""
    return hashlib.sha256(str(token or "").encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# 多账号 API
# ---------------------------------------------------------------------------

def list_accounts() -> list[AccountRef]:
    """枚举全部账号（按 priority 稳定排序）。

    唯一枚举入口：索引自愈（cred 文件缺/损坏的条目剔除——删文件即退出该
    账号）挂在这里，login / forward / quota / UI 走同一个口。
    """
    entries = _STORE.list_entries(required_field=_CRED_REQUIRED_FIELD, logger=log)
    return [_account_ref(entry) for entry in entries]


def _account_ref(entry: dict[str, Any]) -> AccountRef:
    return AccountRef(
        id=str(entry.get("id") or ""),
        name=str(entry.get("login") or ""),
        priority=int(entry.get("priority") or 0),
        added_at=int(entry.get("added_at") or 0),
        alias=str(entry.get("alias") or ""),
    )


def load_account_cred(account_id: str) -> dict[str, Any] | None:
    """读单账号 cred；没有/损坏/缺 access_token 返回 None。"""
    return _STORE.load_cred(account_id, required_field=_CRED_REQUIRED_FIELD)


def save_account_cred(cred: dict[str, Any]) -> AccountRef:
    """落盘一个账号的凭据（幂等 upsert），返回账号引用。

    匹配顺序：account_id → login → access_token。命中即更新该账号
    （priority/added_at 不变——重新登录不改变 failover 顺位），全不命中追加
    为新账号。cred 缺 account_id 时现场派生并写回原 dict。
    """
    login = str(cred.get("login") or "").strip()
    if login:
        cred["login"] = login
    entry = _STORE.upsert(
        cred,
        required_field=_CRED_REQUIRED_FIELD,
        derive_id=_derive_account_id,
        identity_fields=("login", "access_token"),
        entry_fields=_entry_fields,
        error=AuthError,
    )
    return _account_ref(entry)


def _entry_fields(cred: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    prev = previous or {}
    return {"login": str(cred.get("login") or prev.get("login") or "")}


def delete_account(account_id: str) -> bool:
    return _STORE.delete(account_id)


def reorder_accounts(ordered_ids: list[str]) -> list[AccountRef]:
    """按给定 id 顺序重写全部账号的 priority（failover 顺位）。"""
    list_accounts()  # 先自愈再重排，别在残缺索引上动刀
    _STORE.reorder(ordered_ids)
    return list_accounts()


def rename_account(account_id: str, alias: str) -> list[AccountRef]:
    """改一个账号的本地别名（管理页 ✎）。账号不存在抛 ``ValueError``。"""
    list_accounts()
    _STORE.rename(account_id, alias)
    return list_accounts()


def primary_cred() -> dict[str, Any] | None:
    accounts = list_accounts()
    return load_account_cred(accounts[0].id) if accounts else None


def has_accounts() -> bool:
    return bool(list_accounts())


def ensure_account_token(account_id: str, *, force_refresh: bool = False) -> tuple[str, dict[str, Any]]:
    """拿该账号的 GitHub token，返回 ``(token, cred 快照)``。

    ``force_refresh`` 只为与其它通道的调用签名对齐：copilot 没有可刷新的
    东西，这个参数被忽略（401 后「强刷一次」在这里等价于复用同一把钥匙，
    由调用方走登录引导）。cred 快照与 token 同源，避免「各读一次盘」串号。
    """
    cred = load_account_cred(account_id)
    if cred is None:
        raise AuthError(
            f"copilot 账号 {account_id} 的凭据不存在或已损坏："
            f"请重新 `buddy login copilot`"
        )
    return str(cred["access_token"]), dict(cred)


# ---------------------------------------------------------------------------
# GitHub 身份探测（登录与 `--user` 解析共用）
# ---------------------------------------------------------------------------

def fetch_github_user(token: str, timeout: float = 15.0) -> dict[str, Any]:
    """``GET /user``：返回 GitHub 账号信息（``login`` / ``name`` / ``plan``）。

    token 无效抛 :class:`AuthError`（401，可操作的重新登录提示）；
    网络失败抛 :class:`AuthError`（``source`` 标 ``network``，调用方可区分）。
    """
    req = urllib.request.Request(
        f"{_API_BASE}/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": _API_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthError(
                f"GitHub token 被拒（HTTP {exc.code}，token 指纹 "
                f"{token_fingerprint(token)}）：请重新 `gh auth login --user <账号>` "
                f"或 `buddy login copilot` 重新登录"
            ) from exc
        raise AuthError(f"GitHub /user 返回 HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001 - urllib 报错种类多
        raise AuthError(f"无法连接 GitHub API：{exc}") from exc
    if not isinstance(data, dict) or not data.get("login"):
        raise AuthError("GitHub /user 响应缺少 login 字段")
    return data


def cred_from_token(token: str, *, source: str = "manual") -> dict[str, Any]:
    """把一把 GitHub token 变成可落盘的 cred（含 login / name / id）。

    login 由 ``/user`` 决定（token 本身不带身份），失败即抛——没有 login
    就只能靠哈希派生 id，那样同一个人换 token 会变成两个顺位。
    """
    token = str(token or "").strip()
    if not token:
        raise AuthError("GitHub token 为空")
    user = fetch_github_user(token)
    login = str(user.get("login") or "")
    cred: dict[str, Any] = {
        "access_token": token,
        "login": login,
        "name": str(user.get("name") or ""),
        "source": source,
    }
    cred["account_id"] = _derive_account_id(cred)
    return cred


def save_token(token: str, *, source: str = "manual") -> AccountRef:
    """探测身份并落盘（``buddy login copilot --token ...`` 的落点）。"""
    return save_account_cred(cred_from_token(token, source=source))


# ---------------------------------------------------------------------------
# gh CLI 集成
# ---------------------------------------------------------------------------

def _gh_cmd() -> str:
    return os.environ.get("COPILOT_GH_BIN", "").strip() or _GH_BIN


def gh_accounts(timeout: float = 15.0) -> list[str]:
    """本机 gh CLI 已登录的 GitHub 账号（``gh auth status`` 解析，失败返回空）。

    只读本地配置，**不打印、不读取任何 token**；state 不是 ``success`` 的
    账号（token 失效 / 缺 scope）也列出来——交给后续 ``gh auth token`` 与
    ``/user`` 两步判定能不能用，这里不做预判。
    """
    gh = _gh_cmd()
    try:
        proc = subprocess.run(
            [gh, "auth", "status"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("copilot: gh auth status 不可用（%s）", exc)
        return []
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    seen: list[str] = []
    for line in out.splitlines():
        m = re.search(r"Logged in to \S+ account (\S+)", line)
        if not m:
            continue
        user = m.group(1).strip().strip("()")
        if user and user not in seen:
            seen.append(user)
    return seen


def gh_token(user: str = "", timeout: float = 15.0) -> str:
    """``gh auth token``：取 gh CLI 已存的 GitHub token。

    这是**唯一**受支持的「复用本机 Copilot 登录态」路径：token 只进内存，
    由调用方立刻交给 ``/user`` 探测身份并落盘（0600）。``gh`` 不在 PATH 上
    （非交互 shell 常见）时抛 :class:`AuthError`，提示设置
    ``COPILOT_GH_BIN`` 或改用 device flow。
    """
    gh = _gh_cmd()
    cmd = [gh, "auth", "token", "--hostname", "github.com"]
    if user:
        cmd += ["--user", user]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthError(
            f"无法执行 `{gh} auth token`（{exc}）：请确认 gh CLI 已安装并登录，"
            f"或用环境变量 COPILOT_GH_BIN 指定 gh 路径"
        ) from exc
    token = (proc.stdout or "").strip()
    if proc.returncode != 0 or not token:
        detail = (proc.stderr or "").strip().splitlines()
        hint = detail[0] if detail else f"退出码 {proc.returncode}"
        raise AuthError(
            f"`gh auth token` 未取到 token（{hint}）：请先 `gh auth login"
            f"{f' --user {user}' if user else ''}`"
        )
    return token


def is_gho_token(token: str) -> bool:
    """形如 ``gho_xxx`` / ``ghp_xxx`` 的 GitHub token（排除误传其它凭据）。"""
    return bool(re.match(r"^gh[opsu]_[A-Za-z0-9]", str(token or "")))


# ---------------------------------------------------------------------------
# GitHub device flow（没装 gh / 不想动本机 gh 登录态时的兜底）
# ---------------------------------------------------------------------------

#: GitHub OAuth App 的 client_id。这里用的是 **GitHub CLI 自己的公开 client_id**
#: ——它不是秘密（出现在 gh 源码与文档里），用户授权页会显示「GitHub CLI」。
#: 不用自建 App：Copilot 需要的 scope 与 gh 相同，换一个 App id 只会让用户
#: 在授权页上看到更多陌生应用。
GH_CLIENT_ID = os.environ.get("COPILOT_GH_CLIENT_ID", "").strip() or "Iv1.b507a08c87ecfe98"

#: 申请 device code 与轮询换票的端点。
_DEVICE_CODE_URL = "https://github.com/login/device/code"
_DEVICE_TOKEN_URL = "https://github.com/login/oauth/access_token"

#: device flow 的总时长上限（用户得去浏览器点确认）。
DEVICE_FLOW_TIMEOUT_S = 600.0


def _device_post(url: str, data: dict[str, str], timeout: float = 20.0) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": _API_USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise AuthError(f"GitHub device flow 请求失败（HTTP {exc.code}）") from exc
    except Exception as exc:  # noqa: BLE001 - urllib 报错种类多
        raise AuthError(f"无法连接 GitHub device flow：{exc}") from exc


def device_flow_login(open_browser: bool = True, *,
                      timeout_s: float = DEVICE_FLOW_TIMEOUT_S) -> dict[str, Any]:
    """GitHub OAuth device flow → 返回可落盘的 cred（不打印 token）。"""
    import time as _time

    from buddy_proxy.core.browser import open_external_url

    flow = _device_post(_DEVICE_CODE_URL, {"client_id": GH_CLIENT_ID})
    device_code = str(flow.get("device_code") or "")
    user_code = str(flow.get("user_code") or "")
    verify = str(flow.get("verification_uri") or "https://github.com/login/device")
    complete = str(flow.get("verification_uri_complete") or "")
    if not device_code or not user_code:
        raise AuthError("GitHub 没有返回 device code，稍后重试或改用 `buddy login copilot --gh`")

    print(f"\n[Copilot] 请在浏览器中打开 {verify} 并输入授权码: {user_code}\n")
    if open_browser:
        open_external_url(complete or verify)

    interval = max(float(flow.get("interval") or 5), 1.0)
    expires = float(flow.get("expires_in") or 900)
    deadline = _time.monotonic() + min(expires, timeout_s)
    while _time.monotonic() < deadline:
        _time.sleep(interval)
        result = _device_post(_DEVICE_TOKEN_URL, {
            "client_id": GH_CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        error = str(result.get("error") or "")
        if not error and result.get("access_token"):
            return cred_from_token(str(result["access_token"]), source="device-flow")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5.0
            continue
        if error in ("expired_token", "access_denied"):
            raise AuthError(f"GitHub 授权未完成（{error}），请重新执行 `buddy login copilot`")
        raise AuthError(f"GitHub device flow 失败：{error or result}")
    raise AuthError("GitHub device flow 等待授权超时，请重新执行 `buddy login copilot`")
