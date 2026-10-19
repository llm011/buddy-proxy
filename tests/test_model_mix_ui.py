"""饼图接口不可用时的错误态（用户 2026-10-19 反馈「加载失败：Not Found」）。

真实事故：proxy 进程是在合并前启动的，路由表里没有 `/ui/api/model-mix`；
但 `page.py:_read_static` 是**每次请求现读磁盘**，于是浏览器拿到的新 `charts.js`
去请求一个不存在的接口，拿到 FastAPI 默认的 `{"detail":"Not Found"}`，
页面上就只剩一句没有任何指引的「加载失败：Not Found」。

两件事要钉住：

1. **错误文案要说人话**：404/Not Found 必须被识别成「后端没有这个接口 →
   重启 proxy 进程」，而不是把后端原话直接铺给用户。
2. **失败后不要每轮白打**：404 类错误标 `MIX_BROKEN` 后，非强制刷新不再发请求；
   「重试」按钮（force）必须能绕过它。

这里用 node 起一个极简 DOM 桩来跑真实的 charts.js 切片，不依赖浏览器。

运行：
    .venv/bin/python -m pytest tests/test_model_mix_ui.py -v
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

CHARTS_JS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src/buddy_proxy/web/static/charts.js"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑前端 JS（CI 有，本机可选）"
)

# 从 MIX_BROKEN 声明到 loadModelMix 结束：错误态与重试节流都在这一片里
MIX_RE = re.compile(
    r"let MIX_BROKEN = false;.*?\nasync function loadModelMix\([\s\S]*?\n\}\n", re.S
)

# fetch 桩统一是「无参函数，返回 Promise」——写成函数体而不是裸 Promise，
# 否则 Promise.reject 会在模块加载时就求值成未处理拒绝。
NOT_FOUND = "() => Promise.reject(new Error('Not Found'))"
NETWORK = "() => Promise.reject(new Error('Failed to fetch'))"


def _run(body: str, fetch_impl: str) -> dict:
    text = CHARTS_JS.read_text(encoding="utf-8")
    m = MIX_RE.search(text)
    assert m, "charts.js 里找不到饼图错误态切片"
    script = f"""
const els = {{
  'mix-donut': {{ innerHTML: '' }},
  'mix-sub': {{ textContent: '' }},
  'mix-legend': {{ innerHTML: '' }},
}};
const document = {{ getElementById: id => els[id] || null }};
const RANGE = {{ start: '2026-10-01', end: '2026-10-20' }};
const LOG_FILTERS = {{ provider: new Set(), model: new Set() }};
let requests = 0;
function currentTab() {{ return 'log'; }}
function rangeLabel() {{ return '14 天'; }}
function esc(s) {{ return String(s ?? ''); }}
function renderModelMix() {{ els['mix-sub'].textContent = rangeLabel() + ' · 共 ' + (MIX.total||0) + ' 次'; }}
let MIX = null, MIX_QS = null, MIX_SEQ = 0;
const api = (path) => {{ requests++; return ({fetch_impl})(); }};

{m.group(0)}

(async () => {{
  {body}
  console.log(JSON.stringify({{
    donut: els['mix-donut'].innerHTML,
    sub: els['mix-sub'].textContent,
    requests,
  }}));
}})().catch(e => {{ console.error(e && e.stack || e); process.exit(1); }});
"""
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, f"node 执行失败:\n{r.stderr[:2000]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_not_found_explains_restart_instead_of_raw_text():
    """404 必须翻译成「要重启 proxy」，而不是把后端原话丢给用户。"""
    out = _run("await loadModelMix();", NOT_FOUND)
    html = out["donut"]
    assert "重启" in html, "错误态必须告诉用户要重启进程"
    assert "proxy" in html
    assert "HTTP 404" in html
    # 原始后端文案保留，但只是作为附注出现
    assert "Not Found" in html
    # 副标题不能在失败后还挂着「加载中…」
    assert "加载中" not in out["sub"]
    assert "不可用" in out["sub"]


def test_404_is_not_retried_every_poll():
    """确认接口不存在后，非强制刷新不再发请求（否则每 30s 白打一次）。"""
    out = _run(
        """
      await loadModelMix();
      await loadModelMix();   // 模拟下一轮轮询
      await loadModelMix();
    """,
        NOT_FOUND,
    )
    assert out["requests"] == 1, f"404 后不该重复请求，实际 {out['requests']} 次"


def test_force_retry_bypasses_the_giveup_flag():
    """「重试」按钮走 force=true，必须能真的再打一次。"""
    out = _run(
        """
      await loadModelMix();
      await loadModelMix(true);   // 用户点「重试」
    """,
        NOT_FOUND,
    )
    assert out["requests"] == 2


def test_recovery_clears_the_flag():
    """接口修好之后（重启进程）要能自动恢复，而不是永久卡在错误态。"""
    # 第 1 次 404，之后都成功
    fetch = """() => {
      if (requests === 1) return Promise.reject(new Error('Not Found'));
      return Promise.resolve({total: 5,
        models: [{model:'m', count:5, provider:'p', providers:[]}],
        other: null, models_total: 1, partial: false});
    }"""
    out = _run(
        """
      await loadModelMix();        // 404 -> 标记 broken
      await loadModelMix(true);    // 用户重试 -> 成功，清掉标记并记下 qs
      await loadModelMix();        // 同 query：命中缓存，不该重发（这是对的）
    """,
        fetch,
    )
    assert out["requests"] == 2
    assert "不可用" not in out["sub"], "成功后副标题要恢复正常"


def test_after_recovery_a_new_range_refetches():
    """恢复后换日期范围（qs 变了）必须能重新拉——确认 broken 标记真的解除了。"""
    fetch = """() => {
      if (requests === 1) return Promise.reject(new Error('Not Found'));
      return Promise.resolve({total: 5,
        models: [{model:'m', count:5, provider:'p', providers:[]}],
        other: null, models_total: 1, partial: false});
    }"""
    out = _run(
        """
      await loadModelMix();        // 404 -> broken
      RANGE.start = '2026-09-01'; // 换个范围 = 换 qs
      await loadModelMix(true);    // 重试 -> 成功
      RANGE.start = '2026-08-01';
      await loadModelMix();        // 新范围，应真的再请求
    """,
        fetch,
    )
    assert out["requests"] == 3
    assert "不可用" not in out["sub"]


def test_network_error_stays_retryable():
    """非 404（网络抖/5xx）不该被打上「永久坏掉」标记。"""
    out = _run(
        """
      await loadModelMix();
      await loadModelMix();
    """,
        NETWORK,
    )
    assert out["requests"] == 2, "网络类错误应允许每轮自动重试"
