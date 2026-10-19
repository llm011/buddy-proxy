"""管理页设置持久化：默认启用模型、兜底通道覆盖等。

存放在 ``~/.buddy-proxy/settings.json``（可用 ``BUDDY_PROXY_SETTINGS`` 覆盖），
机器本地配置，不进仓库。启动时由 ``__main__`` 加载进 :class:`ProxyState`，
管理页 POST /ui/api/settings 修改后立即写回并热更新到运行态。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import tempfile
import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_DEFAULT_PATH = "~/.buddy-proxy/settings.json"
_SETTINGS_LOCK = threading.Lock()

# 时段判定统一用上海时区，与 trae/pat/cooldown._next_day_timestamp 的日界一致。
_SCHEDULE_TZ = ZoneInfo("Asia/Shanghai")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# 单模型最多允许的时间窗数量；防止 UI/配置写入超长列表。
_MAX_WINDOWS = 8
# 单模型最多允许的候选上游数量（model_order）；防止 UI/配置写入超长列表。
_MAX_ORDER_TARGETS = 8


def settings_path() -> pathlib.Path:
    return pathlib.Path(
        os.getenv("BUDDY_PROXY_SETTINGS", _DEFAULT_PATH)
    ).expanduser()


def load_settings() -> dict[str, Any]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _mtime_str(path: pathlib.Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001 - 纯展示字段，取不到就留空
        return ""


def settings_health() -> dict[str, Any]:
    """探测设置文件的健康状态，供管理页顶部告警条幅使用（只读，绝不抛异常）。

    :func:`load_settings` 对损坏文件是 ``except: return {}``——配置容错需要它这么写，
    但代价是「model_order / 停用 / 时段 / 默认模型四项一起失效却毫无提示」，而且
    :func:`save_settings` 的 ``{**previous, **update}`` 在 previous 为空时会把整个
    文件覆盖成只剩本次写入的那一项。两条叠加，用户只会看到「功能不见了」。
    2026-09-30 排查 model_order 时正是被这个静默失效带偏的，故把「读不出来」
    变成可展示的事实。

    ``ok=False`` 只在「文件存在但读不出 / 不是合法 JSON 对象」时给出：文件不存在
    是正常的首次启动形态（走默认值），不该报警。
    """
    path = settings_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"ok": True, "exists": False, "path": str(path)}
    except Exception as exc:  # noqa: BLE001 - 权限/编码异常：设置同样没生效
        return {"ok": False, "exists": True, "path": str(path),
                "error": f"{type(exc).__name__}: {exc}"}
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - 语法错误：把原始报错给用户定位
        return {"ok": False, "exists": True, "path": str(path),
                "error": str(exc), "size": len(raw), "mtime": _mtime_str(path)}
    if not isinstance(data, dict):
        return {"ok": False, "exists": True, "path": str(path),
                "error": f"顶层不是 JSON 对象（是 {type(data).__name__}）",
                "size": len(raw), "mtime": _mtime_str(path)}
    return {"ok": True, "exists": True, "path": str(path),
            "keys": sorted(data), "size": len(raw), "mtime": _mtime_str(path)}


def _backup_settings(path: pathlib.Path, previous: dict[str, Any]) -> None:
    """覆盖前把**上一版**留一份 ``<name>.bak``，让 UI 写操作可回滚。

    这是护栏而非版本管理：只留最近一份、原地覆盖。**失败语义由调用方兜底**
    （见 :func:`save_settings`）——本函数负责清理自己写了一半的临时文件后就上抛，
    让「备份失败」与「保存失败」两件事可以被分别断言。
    """
    if not previous:
        return  # 还没有设置文件（或读不出来）：没有可备份的「上一版」
    bak = path.with_name(path.name + ".bak")
    tmp = bak.with_name(f".{bak.name}.tmp")
    try:
        tmp.write_text(
            json.dumps(previous, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, bak)
    except Exception:
        # 清掉可能写了一半的临时文件，别在目录里留残渣
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def save_settings(update: dict[str, Any]) -> dict[str, Any]:
    """合并并原子写入设置，避免半写入或同进程更新互相覆盖。

    写入前会把上一版存一份 ``settings.json.bak``（见 :func:`_backup_settings`）。
    备份是**尽力而为**：它失败绝不能连累保存本身——管理页一次「改默认模型」因为
    备份路径写不出就 500，是把护栏变成了新故障点。
    """
    path = settings_path()
    with _SETTINGS_LOCK:
        previous = load_settings()
        merged = {
            **previous,
            **update,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _backup_settings(path, previous)
        except Exception:  # noqa: BLE001 - 见 docstring：备份失败不阻断保存
            pass
        tmp_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as tmp:
                tmp.write(json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_name = tmp.name
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        finally:
            if tmp_name:
                try:
                    pathlib.Path(tmp_name).unlink(missing_ok=True)
                except OSError:
                    pass
    return merged


#: 本项目**认得的**通道 id（含只在特定配置下才注册的 traepat）。
#:
#: 与 ``state.providers`` 的区别：那个是「本次启动实际注册了哪些」，会随
#: ``--zcode`` / ``--mimo`` 之类的开关变化；这里是「这些名字属于本项目的通道
#: 命名空间」。路由要用它区分两种 ``xxx/model``：
#:
#: - ``xxx`` 是通道名但这次没启用 → 该报「通道未启用」，而不是把整个
#:   ``xxx/model`` 漏给兜底通道（上游只会回一句「模型不存在」）；
#: - ``xxx`` 压根不是通道名（如 ``openrouter/...``）→ 才轮到兜底逻辑。
#:
#: ``workbuddy`` 是 ``codebuddy`` 的旧称，不单列：归一在 ``model_key`` 里做。
#: ``traeintl`` / ``qoderintl`` / ``codebuddyintl`` 与 ``traepat`` 同理：没有
#: 独立开关，只在有海外区账号时挂到 ``--trae`` / ``--qoder`` / codebuddy 主通道
#: 分支下注册。
KNOWN_PROVIDER_IDS = frozenset(
    {"codebuddy", "codebuddyintl", "zcode", "glm", "mimo", "qoder", "qoderintl",
     "doubao", "dumate", "trae", "traepat", "traeintl", "kimi", "codex"}
)

#: 通道没启用时，告诉用户**怎么启用**。值是要打印给用户看的短句。
#:
#: 单独一张表而不是拼 ``--{prefix}`` / ``{PREFIX}_ENABLED=1``：那套命名只对
#: 一半通道成立。``traepat`` 没有自己的开关——它挂在 ``--trae`` 分支里，由
#: ``trae.pat.config.pat_enabled()`` 决定（配了 ``TRAE_PAT_BEARER`` 或
#: ``TRAE_PAT_BEARER_PROFILES`` 才注册），拼出来的 ``--traepat`` 是不存在的
#: 参数，照着敲只会落到 usage。以后再加通道时，这张表逼着把真实开关写清楚。
PROVIDER_ENABLE_HINTS: dict[str, str] = {
    "zcode": "加 --zcode（或设 ZCODE_ENABLED=1）",
    "glm": "加 --glm（或设 GLM_ENABLED=1）",
    "mimo": "加 --mimo（或设 MIMO_ENABLED=1）",
    "qoder": "加 --qoder（或设 QODER_ENABLED=1）",
    "doubao": "加 --doubao（或设 DOUBAO_ENABLED=1）",
    "dumate": "加 --dumate（或设 DUMATE_ENABLED=1），并确保百度搭子 App 在运行",
    "trae": "加 --trae（或设 TRAE_ENABLED=1）",
    # traepat 没独立开关：先配 TRAE_PAT_BEARER(_PROFILES)，再开 --trae
    "traepat": "配置 TRAE_PAT_BEARER（或 TRAE_PAT_BEARER_PROFILES）后加 --trae",
    # 海外通道同样没独立开关：先登录海外区账号，再开 --trae / --qoder
    "traeintl": "先 `buddy login trae --region global` 登录海外账号，再加 --trae",
    "qoderintl": "先 `buddy login qoder --region global` 登录海外账号，再加 --qoder",
    "codebuddyintl": "先 `buddy login codebuddy --region global` 登录海外账号，重启 Buddy 自动注册",
    "kimi": "加 --kimi（或设 KIMI_ENABLED=1）",
    "codex": "加 --codex（或设 CODEX_ENABLED=1），先 `buddy login codex` 登录 ChatGPT 订阅账号",
    # codebuddy 是默认通道，进到这里只可能是「认得但没注册」的异常态
    "codebuddy": "检查启动参数（codebuddy 是默认通道，不应缺失）",
}


def provider_enable_hint(prefix: str) -> str:
    """``prefix`` 对应的启用方式短句；表里没有就退回通用措辞。"""
    return PROVIDER_ENABLE_HINTS.get(
        prefix, f"加 --{prefix}（或设 {prefix.upper()}_ENABLED=1）"
    )


def normalize_default_model(raw: str) -> str:
    """归一化默认模型字符串：去空白、provider 别名归一。"""
    value = (raw or "").strip()
    if "/" in value:
        prefix, model = value.split("/", 1)
        # workbuddy 是 codebuddy 的旧称
        prefix = {"workbuddy": "codebuddy"}.get(prefix, prefix)
        value = f"{prefix}/{model}"
    return value


def model_key(provider: str, model: str) -> str:
    """停用集合的规范键：``<provider>/<model>``，provider 缺省视为 codebuddy。"""
    provider = (provider or "codebuddy").strip()
    provider = {"workbuddy": "codebuddy"}.get(provider, provider)
    return f"{provider}/{(model or '').strip()}"


def normalize_order(raw: Any) -> list[str]:
    """校验并归一化候选上游列表 ``["zcode/glm-5.3", "traepat/glm-5.3"]``。

    - 每项形如 ``provider/model``（两侧均非空）或裸 ``model``；provider 别名归一
      （``workbuddy`` → ``codebuddy``）。
    - 去重**保序**（同一目标重复出现只留首次）。
    - 最多保留 ``_MAX_ORDER_TARGETS`` 个，超量截断。
    - 输入不是 list、项非法一律跳过，**绝不抛异常**（配置层容错，与
      :func:`normalize_windows` 同款）。
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        value = normalize_default_model(item)
        if not value:
            continue
        # `provider/` 两侧任一为空（如 "zcode/"、"/m1"）都视为非法
        if "/" in value:
            head, tail = value.split("/", 1)
            if not head.strip() or not tail.strip():
                continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= _MAX_ORDER_TARGETS:
            break
    return out


def normalize_order_key(raw: str) -> str:
    """把 ``model_order`` 的键归一成 :func:`model_key` 口径。

    **裸模型名原样保留**（不再按 codebuddy 兜底）。用户配的是「这个模型名走什么
    顺序」，归属通道是运行时才知道的实现细节——同一个名字哪个通道先认领它就归谁。
    早先把裸键改写成 ``codebuddy/<模型名>`` 是错的：它把这条件局限死在单个通道上，
    请求解析到别的通道时静默不触发（``forward`` 侧现在也认裸键，见那里的注释）。
    带 ``/`` 的键仍按 :func:`model_key` 归一（别名 ``workbuddy`` → ``codebuddy``）：
    历史配置里那些是「精确位置」，两种形态都支持。
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if "/" in value:
        return model_key(*value.split("/", 1))
    return value


def _to_minutes(hhmm: str) -> int | None:
    """``HH:MM`` → 当日分钟数（0..1439）；非法返回 None。"""
    m = _HHMM_RE.match((hhmm or "").strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def normalize_windows(raw: Any) -> list[list[str]]:
    """校验并归一化时间窗列表 ``[["HH:MM","HH:MM"], ...]``。

    - 每个窗口两个合法 ``HH:MM``；起==止视为无效（零长度）丢弃。
    - 起>止允许（跨零点，如 22:00~08:00）。
    - 最多保留 ``_MAX_WINDOWS`` 个，超量截断。
    - 输入不是 list、项非法一律跳过，绝不抛异常（配置层容错）。
    """
    if not isinstance(raw, list):
        return []
    out: list[list[str]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        start, end = _to_minutes(str(item[0])), _to_minutes(str(item[1]))
        if start is None or end is None or start == end:
            continue
        out.append([f"{start // 60:02d}:{start % 60:02d}",
                    f"{end // 60:02d}:{end % 60:02d}"])
        if len(out) >= _MAX_WINDOWS:
            break
    return out


def model_schedule_open(windows: Any, now: float | None = None) -> bool:
    """当前时刻是否落入任一允许窗口（上海时区）。

    ``windows`` 为 ``normalize_windows`` 归一后的列表。空列表 → 恒 False
    （无任何开放时段 = 始终挡）。跨零点窗口（起>止）判定为 ``t>=start or t<end``。
    """
    wins = windows if isinstance(windows, list) else []
    if not wins:
        return False
    ts = now if now is not None else time.time()
    current = datetime.fromtimestamp(ts, _SCHEDULE_TZ)
    minute = current.hour * 60 + current.minute
    for item in wins:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        start, end = _to_minutes(str(item[0])), _to_minutes(str(item[1]))
        if start is None or end is None or start == end:
            continue
        if start < end:
            if start <= minute < end:
                return True
        else:  # 跨零点
            if minute >= start or minute < end:
                return True
    return False


def format_windows(windows: Any) -> str:
    """人类可读窗口摘要，如 ``22:00~08:00, 12:00~14:00``；空 → ``无开放时段``。"""
    wins = windows if isinstance(windows, list) else []
    parts = [f"{item[0]}~{item[1]}" for item in wins
             if isinstance(item, (list, tuple)) and len(item) == 2]
    return ", ".join(parts) if parts else "无开放时段"
