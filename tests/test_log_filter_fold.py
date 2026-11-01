"""请求日志页筛选区「最多一行、可展开」（用户 2026-11-01）。

模型一多，三组筛选（通道/模型/客户端）会折成好几行，把下面的表格挤下去。
需求：默认只露一行，多出来的裁掉并给「更多筛选 ▾」，点开铺满自然高度。

判定必须按**实测高度**（与额度折叠同款）：同一批按钮在窄屏 / 长模型名下
高度不同，按条数硬算会裁错。上限来自 CSS 变量 --lffold-max（一行高），
两边共用一个数。

这里用 node 起一个极简 DOM 桩跑 app_state.js 的真实切片。
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

APP_JS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src/buddy_proxy/web/static/app_state.js"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑前端 JS（CI 有，本机可选）"
)

# 从 renderLogFilters 到 toggleLogFilterFold 结束：折叠状态与渲染都在这一片。
FOLD_RE = re.compile(
    r"let LOG_FILTERS_OPEN[\s\S]*?function toggleLogFilterFold\(\) \{[\s\S]*?\n\}\n",
    re.S,
)


def _run(body: str, *, scroll_height: int = 51, limit: str = "24px") -> dict:
    text = APP_JS.read_text(encoding="utf-8")
    m = FOLD_RE.search(text)
    assert m, "app_state.js 里找不到筛选折叠切片（renderLogFilters…toggleLogFilterFold）"
    script = f"""
const clip = {{
  _cls: new Set(),
  scrollHeight: {scroll_height},
  classList: {{
    toggle(c, on) {{ if (on) clip._cls.add(c); else clip._cls.delete(c); }},
    contains(c) {{ return clip._cls.has(c); }},
  }},
  getBoundingClientRect() {{ return {{ height: {limit.replace('px','')} }}; }},
}};
const more = {{ hidden: true, textContent: '' }};
const filters = {{ innerHTML: '' }};
const document = {{
  getElementById: id => (id === 'log-filters' ? filters : null),
  querySelector: sel => (sel.includes('.lf-clip') ? clip : (sel.includes('.lf-more') ? more : null)),
}};
globalThis.document = document;
globalThis.getComputedStyle = () => ({{ getPropertyValue: () => '{limit}' }});
const RANGE_MODELS = [];
let RANGE_CLIENTS = [];
const pcolor = () => '#000';
const esc = s => String(s);
const LOG_FILTERS = {{ provider: new Set(), model: new Set(), client: new Set() }};

{m.group(0)}

{body}
console.log(JSON.stringify({{
  clipped: clip._cls.has('clipped'),
  open: clip._cls.has('open'),
  moreHidden: more.hidden,
  moreText: more.textContent,
  openFlag: LOG_FILTERS_OPEN,
  html: filters.innerHTML,
}}));
"""
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, f"node 执行失败:\n{r.stderr[:2000]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_fold_render_wraps_content_in_clip_and_more_button():
    """渲染后结构是 .lf-clip > .lf-main，外加一个默认隐藏的 .lf-more 按钮。"""
    out = _run("renderLogFilters();")
    assert 'data-lffold="log"' in out["html"]
    assert 'lf-main' in out["html"]
    assert 'lf-more' in out["html"]


def test_content_taller_than_one_line_is_clipped_with_button():
    """内容超过一行（scrollHeight 51 > 22）→ 加 clipped 类、按钮露出来。"""
    out = _run("renderLogFilters();")
    assert out["clipped"] is True, "超过一行应收起裁切"
    assert out["moreHidden"] is False, "被裁时「更多筛选」按钮要露出来"
    assert out["moreText"] == '更多筛选 ▾'


def test_content_within_one_line_not_clipped():
    """内容不超过一行（scrollHeight 20 ≤ 24）→ 不裁、不显示按钮（假按钮没意义）。"""
    out = _run("renderLogFilters();", scroll_height=20)
    assert out["clipped"] is False
    assert out["moreHidden"] is True


def test_single_row_at_real_height_24_is_not_clipped():
    """单行的**真实**高度恰是 24px（组名胶囊撑的，见 style.css 的 --lffold-max
    注释），不能被判成「超过一行」而裁掉。这是回归钉：早先 limit 写成按钮的
    22px，比真实单行矮 2px，于是单行也被裁、底下还多出一个点了没用的假按钮。"""
    out = _run("renderLogFilters();", scroll_height=24)
    assert out["clipped"] is False, "单行真实高度 24 不该被裁"
    assert out["moreHidden"] is True, "没被裁就不该露「更多筛选」按钮"


def test_toggle_expands_then_collapses():
    """点「更多筛选」→ 展开（open 类、箭头变收起）；再点回到裁切。"""
    out = _run("""
renderLogFilters();
toggleLogFilterFold();     // 展开
const openState = {ot: more.hidden, open: clip._cls.has('open'), t: more.textContent, flag: LOG_FILTERS_OPEN};
toggleLogFilterFold();     // 收回
const closedState = {open: clip._cls.has('open'), flag: LOG_FILTERS_OPEN};
globalThis.__seq = JSON.stringify({openState, closedState});
""")
    # 终态回到收起
    assert out["open"] is False
    assert out["openFlag"] is False
    assert out["clipped"] is True


def test_toggle_flips_flag_and_button_label():
    """展开态下按钮文案变「收起 ▴」、open 标志与 open 类都置真。"""
    out = _run("""
renderLogFilters();
toggleLogFilterFold();
""")
    assert out["open"] is True
    assert out["openFlag"] is True
    assert out["moreText"] == '收起 ▴'


def test_fold_state_persists_across_rerender():
    """展开后再 renderLogFilters（每 30s 重建 / 每次点按钮都重渲），
    展开态要保住——状态存模块变量、不能挂 DOM。"""
    out = _run("""
renderLogFilters();
toggleLogFilterFold();     // 用户展开
renderLogFilters();        // 重建
""")
    assert out["openFlag"] is True, "重渲后展开标志要保留"
    assert out["open"] is True, "重渲后 open 类要保留"
