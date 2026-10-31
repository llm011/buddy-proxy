"""请求指标收集：内存聚合 + JSONL 落盘，供 /ui 管理页按 provider/模型维度聚合。

每次 /v1/chat/completions、/v1/responses、/v1/messages 请求完成（或失败）时，
``codebuddy_provider.forward_chat`` 经由 :meth:`MetricsCollector.record` 记一条：

- 内存里按 ``(日期, provider, model)`` 聚合（请求数 / 错误数 / 耗时 / token），
  另保留最近 N 条明细供「最近请求」表格展示；
- 另按 ``(日期, provider, model)`` 维护一张「模型占比」计数表（见
  :meth:`MetricsCollector.model_distribution`），供请求日志页头部的饼图使用——
  饼图只关心次数，不需要 token/耗时，单开一张轻表后不用等磁盘扫描；
- 同时追加一行 JSON 到 ``logs/metrics.jsonl``，按天滚动归档（保留 KEEP_DAYS 天，
  与其他日志同口径）；重启后回读昨日归档 + 当前文件尾部恢复聚合，
  保证图表跨重启不丢历史。
"""

from __future__ import annotations

import collections
import contextvars
import json
import os
import pathlib
import threading
import time
from typing import Any, Optional

# 内存里保留的最近请求明细条数（「最近请求」表格用）
MAX_RECENT = 200
#: 模型页「平均耗时」取最近多少次实测（用户 2026-10-25 要求：近三次）。
RECENT_LATENCY_N = 3
# 启动时回读 metrics.jsonl 的尾部字节数（单条约 200B，约万条请求）
_TAIL_BYTES = 2_000_000
# 聚合只保留最近 N 天，更早的启动回读时丢弃
KEEP_DAYS = 30

# 请求级账号 holder（traepat 多账号 failover 用；其余 provider 单账号不写）。
# 请求开始时 _instrument 放入空 dict，pat.py 选定账号后在深处写入 account id。
# 之所以是「ContextVar 持有可变 dict」而非直接赋值：流式路径的写入发生在
# SSE 读线程/to_thread worker 里，ContextVar 赋值不跨线程传播，但同一 dict
# 对象的变更全局可见，_instrument 落库时从同一对象读回。
ACCOUNT_META: contextvars.ContextVar[Optional[dict[str, Any]]] = contextvars.ContextVar(
    "account_meta", default=None)


def _date_str(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _credit_field(u: dict[str, Any]) -> Any:
    """取单次请求的积分消耗，兼容两种拼写。

    **CodeBuddy 给 ``credit``（单数），Qoder 给 ``credits``（复数）**——只读一个
    就会把另一家的数据整条丢掉（Qoder 的积分此前一直记成 null 就是这个原因，
    连 ``original_credits``/``billable`` 一起摆在 usage 里没人取）。
    两个都在时以 ``credit`` 为准（CodeBuddy 的实际扣费口径）。
    """
    for key in ("credit", "credits", "original_credits"):
        value = u.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def normalize_usage(u: dict[str, Any]) -> dict[str, Any]:
    """把 OpenAI / CodeBuddy / Anthropic 三种 usage 形态归一化。

    - prompt/completion：Anthropic 叫 input/output_tokens
    - 缓存：OpenAI 在 prompt_tokens_details.cached_tokens（prompt 含缓存），
      Anthropic 是 cache_read_input_tokens（input 不含缓存，单列）——
      统一输出 OpenAI 口径：prompt_tokens = input + cache_read + cache_creation
    - credit：单次积分消耗。上游命名不统一（见 :func:`_credit_field`），
      都没有 → None（由上层决定是否估算）
    """
    details = (u.get("prompt_tokens_details")
               or u.get("input_tokens_details") or {})
    credit = _credit_field(u)
    prompt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    cached = int(u.get("cached_tokens") or details.get("cached_tokens")
                 or u.get("cache_read_input_tokens") or 0)
    cache_write = int(u.get("cache_creation_input_tokens") or 0)
    if u.get("cache_read_input_tokens") is not None or cache_write:
        # Anthropic 官方口径：input_tokens 不含缓存命中/创建，缓存单列——
        # 统一成 OpenAI 口径（prompt 含 cached），UI 各通道才能横向对比。
        # OpenAI/Responses 形态没有这两个顶层字段，prompt 本就含缓存，勿重复累加
        prompt += cached + cache_write
    return {
        "prompt_tokens": prompt,
        "completion_tokens": int(u.get("completion_tokens") or u.get("output_tokens") or 0),
        "cached_tokens": cached,
        "credit": float(credit) if credit is not None else None,
    }


class SSEUsageExtractor:
    """从上游 SSE 字节流里轻量提取 token usage 与 credit。

    兼容 OpenAI（usage 在末尾 chunk 一次性给）与 Anthropic
    （message_start 给 input、message_delta 累计 output）两种形态；
    跨 chunk 断行由内部缓冲处理；只对包含 "usage" 的行做 JSON 解析，
    高吞吐流的开销可忽略。
    """

    def __init__(self) -> None:
        self._buf = b""
        self.usage: dict[str, Any] = {}

    def feed(self, chunk: bytes | str) -> None:
        # 各 provider 产出类型不一：codebuddy/zcode 流是 bytes，trae 流是 str
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        if b"usage" not in chunk and not self._buf:
            return
        self._buf += chunk
        lines = self._buf.split(b"\n")
        self._buf = lines.pop()  # 最后一段可能是残行，留给下一个 chunk
        for line in lines:
            line = line.strip()
            if b'"usage"' not in line:
                continue
            if line.startswith(b"data:"):
                line = line[5:].strip()
            try:
                payload = json.loads(line)
            except Exception:
                continue
            u = payload.get("usage")
            if not isinstance(u, dict):
                u = (payload.get("message") or {}).get("usage")
            if not isinstance(u, dict):
                # Responses 协议：response.completed 事件的 usage 在
                # payload["response"]["usage"]
                u = (payload.get("response") or {}).get("usage")
            if not isinstance(u, dict):
                continue
            for k, v in normalize_usage(u).items():
                # Anthropic 的 output_tokens 逐事件累计，取 max 即最终值
                cur = self.usage.get(k)
                self.usage[k] = v if cur is None else max(cur, v or 0)


class SSEErrorExtractor:
    """从上游 SSE 字节流里提取**带内错误**（provider 用正常 SSE 事件报的错）。

    **Why:** codebuddy 的 ``pipeline.stream_upstream`` 把上游错误做成了带内事件
    （``{"error": {...}}`` / ``event: error``）而不是抛异常——流因此**正常结束**，
    ``_metrics_stream`` 看不到任何异常、按 200 记账。2026-10-02 实测就是这样：
    一条 codebuddy 请求实际只发了 54 字节错误体、客户端什么都没收到，
    指标里却是 ``status=200, chunk_count=3, error=""`` 的漂亮记录（假绿灯）。
    UI 的失败率、模型可用性判断全建立在这份数据上，假绿灯会让排查彻底跑偏。

    只认**明确的错误信号**，不猜语义：Anthropic/OpenAI 的 ``error`` 事件、
    Responses 的 ``type: error``、doubao 的 ``{"type": "error"}``。正常内容里
    出现 "error" 这个词不算。
    """

    def __init__(self) -> None:
        self._buf = b""
        self.message: str = ""

    def feed(self, chunk: bytes | str) -> None:
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        # 必须先无条件缓冲再判断：「error」一词可能被 chunk 边界从中间劈开
        # （b'data: {"err' + b'or":...），先判后缓冲会让两半都漏掉。
        # 缓冲区只留最后一个残行，大小有界（与 SSEUsageExtractor 同一套纪律）。
        self._buf += chunk
        if b"error" not in self._buf:
            # 无错误的流残行也留不住太多字节：有换行就清掉已扫过的部分
            lines = self._buf.rsplit(b"\n", 1)
            self._buf = lines[-1] if len(lines) == 2 else self._buf
            return
        lines = self._buf.split(b"\n")
        self._buf = lines.pop()  # 残行留给下一个 chunk
        for line in lines:
            line = line.strip()
            # 行级过滤与缓冲区判断保持**同样区分大小写**：上游错误键按协议都是
            # 小写（"error" / event: error），大小写混判只会造成「缓冲区漏过、
            # 行级才认得」的静默不一致。
            if not line or b"error" not in line:
                continue
            if line.startswith(b"data:"):
                line = line[5:].strip()
            elif line.startswith(b"event:"):
                # Anthropic 的 `event: error` 只是**声明**下面 data 行是错误事件，
                # 真正的原因在 data 里。这里只做兜底标记（data 行解析出消息时会
                # 覆盖它），让「有 event 行但 data 解析失败」的情况也不至于漏报。
                if b"error" in line and not self.message:
                    self.message = "in-band error event"
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            err = payload.get("error")
            if err is None and payload.get("type") == "error":
                err = payload.get("message") or payload
            if err is None:
                continue
            if isinstance(err, dict):
                err = err.get("message") or err.get("type") or "in-band error"
            # 第一个错误即真因，后续不覆盖；但兜底占位文案要让位给真实消息。
            # **不能**在这里提前 return：feed() 会被逐 chunk 反复调用以重组断行，
            # 中途跳出会丢字节。
            text = str(err)[:300]
            if not self.message or self.message == "in-band error event":
                self.message = text


class MetricsCollector:
    """线程安全的请求指标收集器。"""

    def __init__(self, log_path: Optional[os.PathLike | str] = None):
        self.log_path = pathlib.Path(log_path) if log_path else None
        self._lock = threading.Lock()
        # (date, provider, model) -> 聚合桶
        self._daily: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._recent: collections.deque = collections.deque(maxlen=MAX_RECENT)
        # (provider, model) -> 最近 N 次耗时（新→旧的 deque）。模型页那张表里的
        # 「平均耗时」要的是**近三次实测**：14 天均值在模型换代/上游调参后
        # 和当下的体感差得太远（用户 2026-10-25 反馈）。
        self._recent_ms: dict[tuple[str, str], collections.deque] = {}
        # 请求日志页饼图用：(date, provider, model) -> [次数, 错误数]。
        # 与 _daily 分开放：饼图按天*全量*重算，不需要 token/耗时，独立计数表
        # 更薄、更好整表裁剪（见 model_distribution）。
        self._model_counts: dict[tuple[str, str, str], list[int]] = {}
        # 计数表覆盖的最早日期（启动回填后确定）；None = 还没数据/内存模式
        self._mix_floor: Optional[str] = None
        # 当前写入文件所属日期（跨天首次写入时触发滚动归档）
        self._rot_date = _date_str(time.time())
        self._load_tail()

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------

    def record(
        self,
        provider: str,
        model: str,
        protocol: str = "",
        status: int = 200,
        duration_ms: int = 0,
        stream: bool = False,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error: str = "",
        chunk_count: int = 0,
        ttft_ms: Optional[int] = None,
        cached_tokens: int = 0,
        credit: Optional[float] = None,
        credit_estimated: bool = False,
        client: str = "",
        account: str = "",
    ) -> None:
        ts = time.time()
        rec = {
            "ts": round(ts, 3),
            "provider": provider or "codebuddy",
            "model": model or "(unknown)",
            "protocol": protocol,
            "status": int(status),
            "duration_ms": int(duration_ms),
            "ttft_ms": int(ttft_ms) if ttft_ms is not None else None,
            "stream": bool(stream),
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "cached_tokens": int(cached_tokens or 0),
            "credit": credit,
            "credit_estimated": bool(credit_estimated),
            "chunk_count": int(chunk_count or 0),
            "error": (error or "")[:300],
            "client": (client or "")[:40],
            "account": (account or "")[:40],
        }
        with self._lock:
            self._recent.append(rec)
            self._bucket_add(self._bucket(_date_str(ts), rec["provider"], rec["model"]), rec)
            self._count_add(rec)
            self._recent_add(rec)
        self._append_log(rec)

    def _recent_add(self, rec: dict[str, Any]) -> None:
        """近三次耗时环 +1（调用方须持有 self._lock 或处于构造期）。"""
        if int(rec.get("duration_ms") or 0) <= 0:
            return
        key = (rec.get("provider") or "codebuddy", rec.get("model") or "(unknown)")
        ring = self._recent_ms.get(key)
        if ring is None:
            ring = self._recent_ms[key] = collections.deque(maxlen=RECENT_LATENCY_N)
        ring.appendleft(int(rec["duration_ms"]))

    def _count_add(self, rec: dict[str, Any]) -> None:
        """饼图计数表 +1（调用方须持有 self._lock 或处于构造期）。"""
        key = (_date_str(rec.get("ts") or time.time()),
               rec.get("provider") or "codebuddy",
               rec.get("model") or "(unknown)")
        slot = self._model_counts.get(key)
        if slot is None:
            slot = self._model_counts[key] = [0, 0]
        slot[0] += 1
        if rec.get("status", 200) >= 400 or rec.get("error"):
            slot[1] += 1

    def _bucket(self, date: str, provider: str, model: str) -> dict[str, Any]:
        key = (date, provider, model)
        b = self._daily.get(key)
        if b is None:
            b = {
                "count": 0,
                "errors": 0,
                "duration_ms_sum": 0,
                "duration_ms_max": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "last_ts": 0.0,
            }
            self._daily[key] = b
        return b

    @staticmethod
    def _bucket_add(b: dict[str, Any], rec: dict[str, Any]) -> None:
        is_error = rec["status"] >= 400 or bool(rec["error"])
        b["count"] += 1
        b["errors"] += 1 if is_error else 0
        b["duration_ms_sum"] += rec["duration_ms"]
        b["duration_ms_max"] = max(b["duration_ms_max"], rec["duration_ms"])
        b["prompt_tokens"] += rec["prompt_tokens"]
        b["completion_tokens"] += rec["completion_tokens"]
        b["last_ts"] = max(b["last_ts"], rec.get("ts", 0))

    def _dated_name(self, date: str) -> pathlib.Path:
        """归档文件路径：metrics.jsonl -> metrics.jsonl.YYYY-MM-DD。"""
        return self.log_path.parent / (self.log_path.name + "." + date)

    def _rotate(self, today: str) -> None:
        """跨天滚动（与其他日志同口径：按天分片，保留 KEEP_DAYS 天）。

        昨日数据改名归档；顺带清理超期归档。写入前调用，每天至多触发一次。
        """
        prev_date = self._rot_date
        self._rot_date = today
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > 0:
                target = self._dated_name(prev_date)
                if target.exists():
                    # 同名归档已存在（时钟回拨等同日二次滚动）：并入而非覆盖，避免丢数据
                    with open(self.log_path, encoding="utf-8") as src, \
                            open(target, "a", encoding="utf-8") as dst:
                        dst.write(src.read())
                    self.log_path.unlink()
                else:
                    os.replace(self.log_path, target)
        except OSError:
            pass
        cutoff = time.time() - KEEP_DAYS * 86400
        for p in self.log_path.parent.glob(self.log_path.name + ".*"):
            suffix = p.name[len(self.log_path.name) + 1:]
            try:
                ts = time.mktime(time.strptime(suffix, "%Y-%m-%d"))
            except ValueError:
                continue  # 非日期后缀的文件不动
            if ts < cutoff:
                try:
                    p.unlink()
                except OSError:
                    pass

    def _append_log(self, rec: dict[str, Any]) -> None:
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            today = _date_str(time.time())
            if self._rot_date != today:
                self._rotate(today)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 指标落盘失败不影响转发

    # ------------------------------------------------------------------
    # 启动恢复：回读文件尾部
    # ------------------------------------------------------------------

    def _read_tail(self, path: pathlib.Path) -> list[dict[str, Any]]:
        """回读单个文件尾部（最多 _TAIL_BYTES 字节）的 JSONL 记录。"""
        if not path.exists():
            return []
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - _TAIL_BYTES))
                blob = f.read().decode("utf-8", errors="replace")
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for line in blob.splitlines()[1 if size > _TAIL_BYTES else 0:]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def _load_tail(self) -> None:
        if self.log_path is None:
            return
        # 昨日归档 + 当前文件都读：跨天滚动后，最近记录可能分布在两个文件
        yesterday = self._dated_name(_date_str(time.time() - 86400))
        cutoff = time.time() - KEEP_DAYS * 86400
        with self._lock:
            for path in (yesterday, self.log_path):
                for rec in self._read_tail(path):
                    if rec.get("ts", 0) < cutoff:
                        continue
                    self._recent.append(rec)
                    # 「近 3 次耗时」也靠回读才有内容：只在 record() 里灌环的话，
                    # 进程一重启整列就空到下一次同模型请求（模型页最常看的恰是
                    # 刚重启后的状态）。_read_tail 按文件顺序给的是旧→新，
                    # appendleft 正好让最新的落在最前，与实时路径口径一致。
                    self._recent_add(rec)
                    normalized = {
                        "status": rec.get("status", 200),
                        "duration_ms": rec.get("duration_ms", 0),
                        "prompt_tokens": rec.get("prompt_tokens", 0),
                        "completion_tokens": rec.get("completion_tokens", 0),
                        "error": rec.get("error", ""),
                    }
                    self._bucket_add(
                        self._bucket(_date_str(rec["ts"]), rec.get("provider", ""), rec.get("model", "")),
                        normalized,
                    )
        self._backfill_mix_archives()

    def _rebuild_mix_counts(self) -> None:
        """整读所有在期文件，重建饼图计数表（调用方须持有 self._lock）。

        ``_load_tail`` 的 ``_recent`` / ``_daily`` 只能读尾部（2MB 截断，且每次
        请求都会 append，没法续读），所以计数表不跟着尾部走，而是启动时从文件
        整读一遍：当前文件 + KEEP_DAYS 内的每个归档，各读一次。

        用「整文件」而非「尾部」是为了和下面日志表的「共 N 条」同口径——归档
        超过 2MB 时尾部只会少算前半段，且没有任何标记能提示；重复读同一份文件
        则会翻倍，所以这里一律先清空再重建。
        """
        self._model_counts.clear()
        if self.log_path is None:
            return
        cutoff = time.time() - KEEP_DAYS * 86400
        for path in (self.log_path, *sorted(self.log_path.parent.glob(self.log_path.name + ".*"))):
            for rec in self._read_all(path):
                if rec.get("ts", 0) < cutoff:
                    continue
                self._count_add(rec)
        self._mix_floor = min((k[0] for k in self._model_counts), default=None)

    def _backfill_mix_archives(self) -> None:
        """启动时从文件重建饼图计数表（只建 ``_model_counts``，见 :meth:`_rebuild_mix_counts`）。

        ``_load_tail`` 只回读昨日归档 + 当前文件尾部（供内存最近的表格/聚合），
        但请求日志页的日期范围可以拉到 30 天——只靠那份回读的话，切到「30 天」
        饼图就只剩今天，跟下面的日志表（服务端会扫全部归档）对不上；而且尾部
        截断在归档超过 2MB 时会静默少算前半段。

        只做**计次**，不复原 token/耗时聚合（那些仍只跟尾部走），所以代价仅是
        一次逐行 json 解析：几十万行级，启动多花百毫秒量级，换每次查询零成本——
        **运行时（30s 轮询）仍不碰磁盘**，这才是用户担心的那部分。
        """
        with self._lock:
            self._rebuild_mix_counts()

    # ------------------------------------------------------------------
    # 查询（/ui/api/logs）：按日期范围分页读日志文件（含 30 天归档）
    # ------------------------------------------------------------------

    def _read_all(self, path: pathlib.Path) -> list[dict[str, Any]]:
        """整读单个 JSONL 文件（非尾部截断，供分页查询用）。"""
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict):
                        out.append(rec)
        except OSError:
            return out
        return out

    def query_logs(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
        providers: Optional[list[str]] = None,
        models: Optional[list[str]] = None,
        clients: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """按日期范围（YYYY-MM-DD，含端点）分页读日志文件，按 ts 倒序返回。

        数据源：当前 metrics.jsonl + 落在 [start, end] 的按天归档文件。未落盘
        （log_path=None）时退回内存 recent。范围缺省为最近 KEEP_DAYS 天。
        providers/models/clients：非空时按白名单过滤（组内 OR，供 UI 快速筛选）。
        响应额外带 ``clients``：日期范围内出现过的非空客户端值（按出现次数
        降序），供前端生成「客户端」筛选按钮——候选不随已选筛选收缩。
        """
        page_size = max(1, min(int(page_size or 20), 200))
        page = max(1, int(page or 1))

        if self.log_path is None:
            with self._lock:
                rows = list(reversed(self._recent))
        else:
            # 当前文件的日期 + 所有归档日期，取落在 [start, end] 的文件整读
            today = _date_str(time.time())
            files: list[pathlib.Path] = [self.log_path]
            for p in self.log_path.parent.glob(self.log_path.name + ".*"):
                suffix = p.name[len(self.log_path.name) + 1:]
                try:
                    time.strptime(suffix, "%Y-%m-%d")
                except ValueError:
                    continue  # 非日期后缀的文件跳过
                if (start and suffix < start) or (end and suffix > end):
                    continue
                files.append(p)
            # 当前文件写的是「今天」，若范围不含今天则不必读它
            if end and today > end:
                files = [f for f in files if f is not self.log_path]
            rows = []
            for path in files:
                rows.extend(self._read_all(path))
            rows.sort(key=lambda r: r.get("ts", 0), reverse=True)

        # 「客户端」筛选候选：日期范围内出现过的非空 client 值，按出现次数
        # 降序（与 UI 通道/模型候选「按流量排序」的口径一致）。必须在白名单
        # 过滤**之前**收集，否则一选中某个筛选，其余候选按钮就消失了。
        # 旧记录 client 为空串，跳过（它们无法也不必参与筛选）。
        client_counts: dict[str, int] = {}
        for r in rows:
            d = _date_str(r.get("ts", 0))
            if (start and d < start) or (end and d > end):
                continue
            c = (r.get("client") or "").strip()
            if c:
                client_counts[c] = client_counts.get(c, 0) + 1
        client_list = [c for c, _ in
                       sorted(client_counts.items(), key=lambda kv: (-kv[1], kv[0]))]

        # 逐条按日期范围 + 通道/模型/客户端白名单过滤（内存 recent 分支也走这里）
        if start or end or providers or models or clients:
            def _in(rec: dict[str, Any]) -> bool:
                d = _date_str(rec.get("ts", 0))
                if (start and d < start) or (end and d > end):
                    return False
                if providers and rec.get("provider", "") not in providers:
                    return False
                if models and rec.get("model", "") not in models:
                    return False
                if clients and rec.get("client", "") not in clients:
                    return False
                return True
            rows = [r for r in rows if _in(r)]

        total = len(rows)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        lo = (page - 1) * page_size
        return {
            "rows": rows[lo:lo + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
            "clients": client_list,
            "from_disk": self.log_path is not None,
        }

    # ------------------------------------------------------------------
    # 查询（请求日志页头部饼图）：按日期范围聚合各模型请求次数
    # ------------------------------------------------------------------

    def model_distribution(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 12,
        providers: Optional[list[str]] = None,
        models: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """按日期范围（YYYY-MM-DD，含端点）汇总**各模型**的请求次数。

        口径：扇区按模型名（同模型名跳通道合并），每行附 ``providers`` 供悬浮框
        展开各通道明细。providers/models 与 :meth:`query_logs` 同语义（非空时
        按白名单过滤、组内 OR），前端用它让饼图跟着日志表的筛选一起收缩。

        只读内存里的 ``_model_counts``（构造时已从 30 天内的归档 + 当前文件
        重建）：**不扫磁盘**，所以 30 秒轮询时开销与 /ui/api/stats 同量级。
        仍可能不全的情形：归档文件超过 KEEP_DAYS/被手动删除，或窗口起点早于
        计数表下沿（此时 ``partial=True``，前端在副标题标「≈」）。

        limit：返回前 limit 大的模型（按次数降序），其余并入 ``other``
        （前端画成一块灰色扇区，tooltip 里看明细次数），避免扇区多到无法辨认。
        """
        limit = max(1, int(limit or 12))
        with self._lock:
            # 先按「裸模型名」归并（用户问的是「各**模型**占比」，同一个
            # glm-5.3-flash 在 codebuddy/trae/zcode 各占一行会把饼图切成
            # 四个同名扇区，读不出「这个模型到底用了多少」）；同时保留
            # 按通道的明细（providers）供 tooltip 展开。
            per: dict[str, dict[str, Any]] = {}
            for (date, provider, model), (count, errors) in self._model_counts.items():
                if (start and date < start) or (end and date > end):
                    continue
                if providers and provider not in providers:
                    continue
                if models and model not in models:
                    continue
                row = per.get(model)
                if row is None:
                    row = per[model] = {"model": model, "count": 0, "errors": 0,
                                        "providers": {}}
                row["count"] += count
                row["errors"] += errors
                slot = row["providers"].get(provider)
                if slot is None:
                    slot = row["providers"][provider] = [0, 0]
                slot[0] += count
                slot[1] += errors

        rows = list(per.values())
        for row in rows:
            row["providers"] = [
                {"provider": p, "count": c, "errors": e}
                for p, (c, e) in sorted(row["providers"].items(),
                                        key=lambda kv: (-kv[1][0], kv[0]))
            ]
            # 主通道：次数最多的那个（图例上显示成小角标）
            row["provider"] = row["providers"][0]["provider"] if row["providers"] else ""
        rows.sort(key=lambda r: (-r["count"], r["model"]))
        total = sum(r["count"] for r in rows)
        total_errors = sum(r["errors"] for r in rows)

        # 计数表下沿：比这个日期更早的窗口天数根本没扫过，次数会缺——
        # 前端在副标题标「≈」提醒对不上日志表的「共 N 条」。
        # 无数据 / 无落盘（内存模式）时没有「缺」可言，不标。
        partial = bool(self._mix_floor and start and start < self._mix_floor)

        top = rows[:limit]
        rest = rows[limit:]
        other = None
        if rest:
            other = {
                "count": sum(r["count"] for r in rest),
                "errors": sum(r["errors"] for r in rest),
                "models": [{"provider": r["provider"], "model": r["model"],
                            "count": r["count"]} for r in rest],
            }
        return {
            "start": start,
            "end": end,
            "total": total,
            "errors": total_errors,
            "models": top,
            "other": other,
            "models_total": len(rows),
            "partial": partial,
        }

    # ------------------------------------------------------------------
    # 查询（/ui/api/stats）

    def snapshot(self, days: int = 30) -> dict[str, Any]:
        """返回按模型聚合 + 按天序列 + 逐天×逐模型明细 + 最近请求明细。"""
        now = time.time()
        with self._lock:
            daily = {k: dict(v) for k, v in self._daily.items()}
            recent = list(self._recent)

        # ---- 按模型聚合（全部保留窗口内） ----
        per_model: dict[tuple[str, str], dict[str, Any]] = {}
        for (date, provider, model), b in daily.items():
            agg = per_model.setdefault(
                (provider, model),
                {
                    "provider": provider,
                    "model": model,
                    "count": 0,
                    "errors": 0,
                    "duration_ms_sum": 0,
                    "duration_ms_max": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "last_ts": 0.0,
                },
            )
            agg["count"] += b["count"]
            agg["errors"] += b["errors"]
            agg["duration_ms_sum"] += b["duration_ms_sum"]
            agg["duration_ms_max"] = max(agg["duration_ms_max"], b["duration_ms_max"])
            agg["prompt_tokens"] += b["prompt_tokens"]
            agg["completion_tokens"] += b["completion_tokens"]
            agg["last_ts"] = max(agg["last_ts"], b["last_ts"])
        # 近 N 次耗时：随本次 snapshot 一起快照，供模型页「平均耗时」列用
        with self._lock:
            recent_ms = {k: list(v) for k, v in self._recent_ms.items()}
        models = []
        for agg in per_model.values():
            count = agg["count"]
            agg["avg_ms"] = round(agg.pop("duration_ms_sum") / count) if count else 0
            ring = recent_ms.get((agg["provider"], agg["model"]))
            agg["recent_count"] = len(ring) if ring else 0
            if ring:
                # 「近三次」保留明细分值，前端的 tooltip 能逐条显示
                agg["recent_ms"] = ring
                agg["recent_avg_ms"] = round(sum(ring) / len(ring))
            else:
                # 空环时留 None：前端按 recent_count 判「没有近几次样本」显示 —，
                # 不能悄悄退回 14 天均值——那正是用户嫌「跟近期差太大」的数值。
                agg["recent_avg_ms"] = None
            models.append(agg)
        models.sort(key=lambda m: -m["count"])

        # ---- 按天序列（零填充，图表连续）+ 逐天×逐模型明细 ----
        # model_daily 直接导出 self._daily 桶（前端按所选日期范围重算耗时/Top10）；
        # 只保留落在近 days 天窗口内的桶，与 day_list 同口径。
        day_list = []
        for offset in range(days - 1, -1, -1):
            day_start = now - offset * 86400
            date = _date_str(day_start)
            day_list.append({"date": date, "total": 0, "errors": 0,
                             "duration_ms_sum": 0, "by_provider": {}})
        by_date = {d["date"]: d for d in day_list}
        model_daily = []
        for (date, provider, model), b in daily.items():
            d = by_date.get(date)
            if d is None:
                continue
            d["total"] += b["count"]
            d["errors"] += b["errors"]
            d["duration_ms_sum"] += b["duration_ms_sum"]
            d["by_provider"][provider] = d["by_provider"].get(provider, 0) + b["count"]
            model_daily.append({
                "date": date,
                "provider": provider,
                "model": model,
                "count": b["count"],
                "errors": b["errors"],
                "duration_ms_sum": b["duration_ms_sum"],
                "duration_ms_max": b["duration_ms_max"],
                "prompt_tokens": b["prompt_tokens"],
                "completion_tokens": b["completion_tokens"],
                "last_ts": b["last_ts"],
            })

        # ---- 24h 汇总 + 最近请求 ----
        # 口径限制：24h 汇总基于 recent deque（上限 MAX_RECENT 条）。24h 内请求
        # 超过上限时会少算——「最近请求」表格同样只基于这 MAX_RECENT 条。对单机自用
        # 够用；如需精确按小时分桶，需要额外状态，暂不做。
        total_24h = errors_24h = 0
        dur_sum_24h = 0
        for rec in recent:
            if rec.get("ts", 0) >= now - 86400:
                total_24h += 1
                dur_sum_24h += rec.get("duration_ms", 0)
                if rec.get("status", 200) >= 400 or rec.get("error"):
                    errors_24h += 1

        return {
            "models": models,
            "daily": day_list,
            "model_daily": model_daily,
            "recent": list(reversed(recent)),
            "summary": {
                "total_24h": total_24h,
                "errors_24h": errors_24h,
                "avg_ms_24h": round(dur_sum_24h / total_24h) if total_24h else 0,
            },
        }
