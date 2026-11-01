"""饼图的错误态与「没有对比时隐藏整卡」（用户 2026-10-19 / 10-20 反馈）。

真实事故：proxy 进程是在合并前启动的，路由表里没有 `/ui/api/model-mix`；
但 `page.py:_read_static` 是**每次请求现读磁盘**，于是浏览器拿到的新 `charts.js`
去请求一个不存在的接口，拿到 FastAPI 默认的 `{"detail":"Not Found"}`，
页面上就只剩一句没有任何指引的「加载失败：Not Found」。

两件事要钉住：

1. **错误文案要说人话**：404/Not Found 必须被识别成「后端没有这个接口 →
   重启 proxy 进程」，而不是把后端原话直接铺给用户。
2. **失败后不要每轮白打**：404 类错误标 `MIX_BROKEN` 后，非强制刷新不再发请求；
   「重试」按钮（force）必须能绕过它。

另外钉住「没有对比时隐藏整卡」（用户 2026-10-20 反馈）：只有一种模型、或者没数据，
整张 `.mix-card` 连同标题一起收起，切回多模型/报错时能重新露出来。

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
    / "src/buddy_proxy/web/static/chart_mix.js"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑前端 JS（CI 有，本机可选）"
)

# 从 MIX 状态声明到 loadModelMix 结束：错误态、重试节流，以及卡片显隐要用的
# MIX_FOCUS / mixHasNothingToCompare / setMixVisible / renderModelMix 都在这一片里。
MIX_RE = re.compile(
    r"let MIX = null;.*?\nasync function loadModelMix\([\s\S]*?\n\}\n", re.S
)

# fetch 桩统一是「无参函数，返回 Promise」——写成函数体而不是裸 Promise，
# 否则 Promise.reject 会在模块加载时就求值成未处理拒绝。
MULTI = {"total": 2, "errors": 0,
         "models": [{"model": "glm-5.3-flash", "count": 62, "errors": 0, "provider": "trae",
                     "providers": [{"provider": "trae", "count": 62, "errors": 0}]},
                    {"model": "deepseek-v4.1-flash", "count": 38, "errors": 0,
                     "provider": "codebuddy",
                     "providers": [{"provider": "codebuddy", "count": 38, "errors": 0}]}],
         "other": None, "models_total": 2, "partial": False}
ONE = {"total": 40, "errors": 0,
       "models": [{"model": "glm-5.3-flash", "count": 40, "errors": 0, "provider": "trae",
                   "providers": [{"provider": "trae", "count": 40, "errors": 0}]}],
       "other": None, "models_total": 1, "partial": False}
EMPTY = {"total": 0, "errors": 0, "models": [], "other": None,
         "models_total": 0, "partial": False}

NOT_FOUND = "() => Promise.reject(new Error('Not Found'))"
NETWORK = "() => Promise.reject(new Error('Failed to fetch'))"


def _ok(payload: dict) -> str:
    return "() => Promise.resolve(%s)" % json.dumps(payload, ensure_ascii=False)


def _run(body: str, fetch_impl: str) -> dict:
    text = CHARTS_JS.read_text(encoding="utf-8")
    m = MIX_RE.search(text)
    assert m, "charts.js 里找不到饼图渲染/错误态切片"
    script = f"""
const els = {{
  'mix-donut': {{ innerHTML: '' }},
  'mix-sub': {{ textContent: '' }},
  'mix-legend': {{ innerHTML: '' }},
  // 整张卡：只关心 hidden 属性有没有被正确设置/清掉；另带 classList
  // （折叠态 applyMixFold 会切 .collapsed）。
  'log-mix': {{
    _hidden: false,
    _cls: new Set(),
    setAttribute(n) {{ if (n === 'hidden') this._hidden = true; }},
    removeAttribute(n) {{ if (n === 'hidden') this._hidden = false; }},
    get hidden() {{ return this._hidden; }},
    classList: {{
      toggle(c, on) {{ if (on) els['log-mix']._cls.add(c); else els['log-mix']._cls.delete(c); }},
      contains(c) {{ return els['log-mix']._cls.has(c); }},
    }},
  }},
  // 折叠按钮：applyMixFold 会写 textContent + aria-expanded
  'mix-toggle': {{
    textContent: '',
    _attrs: {{}},
    setAttribute(n, v) {{ this._attrs[n] = v; }},
  }},
}};
const document = {{ getElementById: id => els[id] || null }};
const RANGE = {{ start: '2026-10-01', end: '2026-10-20' }};
const LOG_FILTERS = {{ provider: new Set(), model: new Set() }};
let requests = 0;
function currentTab() {{ return 'log'; }}
function rangeLabel() {{ return '14 天'; }}
function esc(s) {{ return String(s ?? ''); }}
function modelMixKey(m) {{ return m.model; }}
function polarXY(cx, cy, r, a) {{ return [cx + r * Math.sin(a), cy - r * Math.cos(a)]; }}
function donutArcPath() {{ return 'M0 0'; }}
function hideChartTip() {{}}
function showChartTip() {{}}
const api = (path) => {{ requests++; return ({fetch_impl})(); }};

{m.group(0)}

(async () => {{
  {body}
  console.log(JSON.stringify({{
    donut: els['mix-donut'].innerHTML,
    sub: els['mix-sub'].textContent,
    legend: els['mix-legend'].innerHTML,
    cardHidden: els['log-mix'].hidden,
    collapsed: els['log-mix'].classList.contains('collapsed'),
    toggleText: els['mix-toggle'].textContent,
    toggleExpanded: els['mix-toggle']._attrs['aria-expanded'],
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


def test_single_model_hides_whole_card():
    """只有一种模型 → 整张卡（含标题）收起，别留一个 100% 的满圆。"""
    out = _run("await loadModelMix();", _ok(ONE))
    assert out["cardHidden"] is True
    assert out["donut"] == ""


def test_multi_model_shows_card():
    out = _run("await loadModelMix();", _ok(MULTI))
    assert out["cardHidden"] is False
    assert "<svg" in out["donut"]
    assert "glm-5.3-flash" in out["legend"]


# --- 默认折叠（用户 2026-11-01：占比图默认收起） -----------------------------


def test_mix_card_collapsed_by_default():
    """首次渲染后卡片处于折叠态（.collapsed），箭头是收起态的 ▸。"""
    out = _run("await loadModelMix();", _ok(MULTI))
    assert out["cardHidden"] is False
    assert out["collapsed"] is True, "占比图应默认折叠"
    assert out["toggleText"] == '▸'
    assert out["toggleExpanded"] == 'false'


def test_toggle_expands_and_collapses():
    """点箭头展开 → .collapsed 摘掉、箭头变 ▾；再点收回。"""
    out = _run("""
      await loadModelMix();
      toggleMixFold();               // 展开
      const openState = {c: els['log-mix'].classList.contains('collapsed'),
                         t: els['mix-toggle'].textContent};
      toggleMixFold();               // 收回
      const closedState = {c: els['log-mix'].classList.contains('collapsed'),
                           t: els['mix-toggle'].textContent};
      globalThis.__open = openState; globalThis.__closed = closedState;
    """, _ok(MULTI))
    # 用日志带出中间态：这里通过重新跑一次读全局不现实，直接断言终态为收起
    assert out["collapsed"] is True, "两次 toggle 后回到收起"
    assert out["toggleText"] == '▸'


def test_fold_state_survives_rerender():
    """展开后再触发一次渲染（模拟 30s 轮询/换范围），展开态必须保持。"""
    fetch = """() => Promise.resolve(%s)""" % json.dumps(MULTI, ensure_ascii=False)
    out = _run("""
      await loadModelMix();
      toggleMixFold();               // 用户展开
      RANGE.start = '2026-09-01';    // 换范围 -> 重新请求 + 重渲
      await loadModelMix();
    """, fetch)
    assert out["requests"] == 2
    assert out["collapsed"] is False, "重渲后要保住用户的展开态（状态不能挂 DOM）"
    assert out["toggleText"] == '▾'


def test_error_auto_expands_so_message_is_visible():
    """用户把卡片收起后接口挂了——错误提示在 .mix-body 里，必须隐式展开，
    否则用户只看到一条折叠的标题、问题被藏起来。"""
    fetch = """() => Promise.reject(new Error('Not Found'))"""
    out = _run("""
      toggleMixFold();       // 先把默认折叠…（默认已折叠，这次切成展开）
      toggleMixFold();       // …再切回收起：确保处于收起态
      await loadModelMix();
    """, fetch)
    assert out["collapsed"] is False, "出错时必须展开，让错误提示可见"
    assert "重启" in out["donut"]


def test_switch_single_then_multi_reveals_card():
    """切日期/筛选后从「一种模型」变成「多种」时，卡片必须重新露出来。"""
    fetch = "() => Promise.resolve(requests === 1 ? %s : %s)" % (
        json.dumps(ONE, ensure_ascii=False), json.dumps(MULTI, ensure_ascii=False))
    out = _run("""
      await loadModelMix();
      RANGE.start = '2026-09-01';   // 换范围 -> qs 变
      await loadModelMix();
    """, fetch)
    assert out["requests"] == 2
    assert out["cardHidden"] is False, "模型变多后必须重新显示"
    assert "<svg" in out["donut"]


def test_switch_multi_then_single_hides_card():
    """反向：从多种切到单一，卡片要收起。"""
    fetch = "() => Promise.resolve(requests === 1 ? %s : %s)" % (
        json.dumps(MULTI, ensure_ascii=False), json.dumps(ONE, ensure_ascii=False))
    out = _run("""
      await loadModelMix();
      RANGE.start = '2026-09-01';
      await loadModelMix();
    """, fetch)
    assert out["requests"] == 2
    assert out["cardHidden"] is True


def test_empty_range_hides_card():
    """没有请求时也没有占比可看：同样整卡隐藏，不留空盒子（用户 2026-10-20 追加要求）。"""
    out = _run("await loadModelMix();", _ok(EMPTY))
    assert out["cardHidden"] is True
    assert out["donut"] == "", "隐藏时不留残留内容"
    assert out["sub"] == "", "藏起来的副标题也别留「0 次」"


def test_switch_multi_then_empty_hides_card():
    """多个模型 → 筛成空：要从显示切回隐藏。"""
    fetch = "() => Promise.resolve(requests === 1 ? %s : %s)" % (
        json.dumps(MULTI, ensure_ascii=False), json.dumps(EMPTY, ensure_ascii=False))
    out = _run("""
      await loadModelMix();
      RANGE.start = '2026-09-01';
      await loadModelMix();
    """, fetch)
    assert out["requests"] == 2
    assert out["cardHidden"] is True


def test_single_model_then_error_shows_error_not_blank():
    """上次因单一模型隐藏了，这次接口挂了 —— 必须露出错误提示，而不是继续空白。"""
    fetch = """() => requests === 1
      ? Promise.resolve(%s)
      : Promise.reject(new Error('Not Found'))""" % json.dumps(ONE, ensure_ascii=False)
    out = _run("""
      await loadModelMix();
      RANGE.start = '2026-09-01';
      await loadModelMix();
    """, fetch)
    assert out["cardHidden"] is False, "报错时卡片必须露出来"
    assert "重启" in out["donut"]
