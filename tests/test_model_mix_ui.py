"""饼图的错误态，以及「没有对比时隐藏整卡」（用户 2026-10-19 / 10-20 反馈）。

**事故一（错误态）**：proxy 进程是合并前启动的，路由表里没有 `/ui/api/model-mix`；
但 `page.py:_read_static` 是**每次请求现读磁盘**，于是浏览器拿到的新 `charts.js`
去请求不存在的接口，拿到 FastAPI 默认的 `{"detail":"Not Found"}`，页面只剩一句
没有指向的「加载失败：Not Found」。

**需求二（没有对比就隐藏）**：用户随后要求「如果检测出只有一种模型，其实也没必要
展示各模型使用占比的图表了，隐藏掉」，接着又补了一句「无数据的时候也不要保留卡片」。

这里用 node 起极简 DOM 桩，跑**真实的** charts.js 切片（不桩 renderModelMix，
否则等于不测卡片的显隐）。

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

# 从 MIX 状态声明一路到 loadModelMix 结束。起点必须是 MIX_FOCUS / mixHasNothingToCompare /
# setMixVisible / renderModelMix 这些**前面**的声明——从 MIX_BROKEN 开始会漏掉它们。
MIX_RE = re.compile(
    r"let MIX = null;.*?\nasync function loadModelMix\([\s\S]*?\n\}\n", re.S
)

NOT_FOUND = "() => Promise.reject(new Error('Not Found'))"
NETWORK = "() => Promise.reject(new Error('Failed to fetch'))"


def _resolver(payload: str) -> str:
    return f"() => Promise.resolve({payload})"


def _payload(models: list[dict], total: int | None = None, other=None) -> str:
    total = total if total is not None else sum(m["count"] for m in models)
    body = {
        "total": total,
        "errors": 0,
        "models": models,
        "other": other,
        "models_total": len(models),
        "partial": False,
    }
    return json.dumps(body, ensure_ascii=False)


MULTI = _payload([
    {"model": "glm-5.3-flash", "count": 62, "errors": 0,
     "provider": "trae", "providers": [{"provider": "trae", "count": 62, "errors": 0}]},
    {"model": "deepseek-v4.1-flash", "count": 38, "errors": 0,
     "provider": "codebuddy",
     "providers": [{"provider": "codebuddy", "count": 38, "errors": 0}]},
])

ONE = _payload([
    {"model": "glm-5.3-flash", "count": 40, "errors": 0,
     "provider": "trae", "providers": [{"provider": "trae", "count": 40, "errors": 0}]},
])

EMPTY = _payload([], total=0)


def _run(body: str, fetch_impl: str) -> dict:
    text = CHARTS_JS.read_text(encoding="utf-8")
    m = MIX_RE.search(text)
    assert m, "charts.js 里找不到饼图渲染/错误态切片"
    script = f"""
const els = {{
  'mix-donut': {{ innerHTML: '' }},
  'mix-sub': {{ textContent: '' }},
  'mix-legend': {{ innerHTML: '' }},
  // 整张卡：只关心 hidden 属性有没有被正确设置/清掉
  'log-mix': {{
    _hidden: false,
    setAttribute(n) {{ if (n === 'hidden') this._hidden = true; }},
    removeAttribute(n) {{ if (n === 'hidden') this._hidden = false; }},
    get hidden() {{ return this._hidden; }},
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
    requests,
  }}));
}})().catch(e => {{ console.error(e && e.stack || e); process.exit(1); }});
"""
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, f"node 执行失败:\n{r.stderr[:2000]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------- 错误态

def test_not_found_explains_restart_instead_of_raw_text():
    """404 必须翻译成「要重启 proxy」，而不是把后端原话丢给用户。"""
    out = _run("await loadModelMix();", NOT_FOUND)
    html = out["donut"]
    assert "重启" in html, "错误态必须告诉用户要重启进程"
    assert "proxy" in html
    assert "HTTP 404" in html
    assert "Not Found" in html
    assert "加载中" not in out["sub"]
    assert "不可用" in out["sub"]
    # 出错时卡片必须可见，否则用户只会看到一片空白
    assert out["cardHidden"] is False


def test_404_is_not_retried_every_poll():
    """确认接口不存在后，非强制刷新不再发请求（否则每 30s 白打一次）。"""
    out = _run("""
      await loadModelMix();
      await loadModelMix();
      await loadModelMix();
    """, NOT_FOUND)
    assert out["requests"] == 1, f"404 后不该重复请求，实际 {out['requests']} 次"


def test_force_retry_bypasses_the_giveup_flag():
    """「重试」按钮走 force=true，必须能真的再打一次。"""
    out = _run("""
      await loadModelMix();
      await loadModelMix(true);
    """, NOT_FOUND)
    assert out["requests"] == 2


def test_network_error_stays_retryable():
    """非 404（网络抖/5xx）不该被打上「永久坏掉」标记。"""
    out = _run("""
      await loadModelMix();
      await loadModelMix();
    """, NETWORK)
    assert out["requests"] == 2, "网络类错误应允许每轮自动重试"


def test_recovery_clears_the_flag():
    """接口修好之后要能自动恢复，而不是永久卡在错误态。"""
    fetch = """() => requests === 1
      ? Promise.reject(new Error('Not Found'))
      : Promise.resolve(%s)""" % MULTI
    out = _run("""
      await loadModelMix();
      await loadModelMix(true);
    """, fetch)
    assert out["requests"] == 2
    assert "不可用" not in out["sub"], "成功后副标题要恢复正常"
    assert out["cardHidden"] is False


# ------------------------------------------------- 只有一种模型时隐藏整张卡

def test_single_model_hides_whole_card():
    """用户要求：只有一种模型时没必要展示占比图 —— 整张卡（含标题）收起。"""
    out = _run("await loadModelMix();", _resolver(ONE))
    assert out["cardHidden"] is True, "单一模型必须隐藏整张卡"
    # 不能只清空内容：否则日志页顶部会留一个空盒子
    assert out["donut"] == "", "隐藏时不应再画圆环"
    assert out["legend"] == ""


def test_multi_model_shows_card():
    """多种模型时正常显示，别误伤。"""
    out = _run("await loadModelMix();", _resolver(MULTI))
    assert out["cardHidden"] is False
    assert "<svg" in out["donut"]
    assert "glm-5.3-flash" in out["legend"]


def test_switch_single_then_multi_reveals_card():
    """切日期/筛选后从「一种模型」变成「多种」时，卡片必须重新露出来。"""
    fetch = """() => Promise.resolve(requests === 1 ? %s : %s)""" % (ONE, MULTI)
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
    fetch = """() => Promise.resolve(requests === 1 ? %s : %s)""" % (MULTI, ONE)
    out = _run("""
      await loadModelMix();
      RANGE.start = '2026-09-01';
      await loadModelMix();
    """, fetch)
    assert out["requests"] == 2
    assert out["cardHidden"] is True


def test_empty_range_hides_card():
    """没有请求时也没有占比可看：同样整卡隐藏，不留空盒子。

    用户 2026-10-20 追加要求「无数据的时候也不要保留卡片」。日志页紧接着
    就是「最近请求」，空结果用户从表里就能看出来，不需要再挂一张空卡。
    """
    out = _run("await loadModelMix();", _resolver(EMPTY))
    assert out["cardHidden"] is True, "无数据时整张卡收起"
    assert out["donut"] == "", "隐藏时不留残留内容"
    assert out["sub"] == "", "藏起来的副标题也别留「0 次」"


def test_switch_multi_then_empty_hides_card():
    """多个模型 → 筛成空：要从显示切回隐藏。"""
    fetch = """() => Promise.resolve(requests === 1 ? %s : %s)""" % (MULTI, EMPTY)
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
      : Promise.reject(new Error('Not Found'))""" % ONE
    out = _run("""
      await loadModelMix();
      RANGE.start = '2026-09-01';
      await loadModelMix();
    """, fetch)
    assert out["requests"] == 2
    assert out["cardHidden"] is False, "出错时必须露出卡片，否则用户只看到空白"
    assert "404" in out["donut"]
