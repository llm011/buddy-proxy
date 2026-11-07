"""请求日志「积分」列：没有真实消耗就留空，不再回退显示模型倍率。

2026-11-08 用户反馈：没有实扣积分的行会显示 ``x0.11 credits`` 这种东西。

那个值是 ``credits_map`` 里的**模型计价倍率**（每 1M token 的基准单价倍数），
不是本次请求的消耗，所以：

- 同一模型每一行都长一样，不携带行级信息；
- 位置在「积分」列里，极易被读成「这次花了 0.11 积分」；
- 用户明确要求：没有就不展示。

现在该列只有两种状态：真实消耗（上游实扣 / 本地估算）或 ``—``。
倍率仍保留在「模型顺序」页（那里的语义就是倍率，不会误读）。
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

STATIC = pathlib.Path(__file__).resolve().parents[1] / "src" / "buddy_proxy" / "web" / "static"
CHART_LOG = STATIC / "chart_log.js"
INDEX = STATIC / "index.html"


def test_no_credits_map_fallback_in_credit_column():
    """积分列不得再从 credits_map 回退取倍率字符串。"""
    js = CHART_LOG.read_text(encoding="utf-8")
    assert "const cmap" not in js, \
        "credits_map 的 cmap 变量已移除；若为别的用途加回来，别用在积分列"
    assert "cmap[" not in js, \
        "请求日志不该再读 credits_map——那个值是模型倍率，不是单次消耗"
    # 唯一取值口：只认 r.credit
    assert "const creditTxt = r.credit != null ? fmtCredit(r.credit) : null;" in js


def test_missing_credit_renders_dash():
    """没有真实消耗时渲染 —（带说明），不是倍率标签。"""
    js = CHART_LOG.read_text(encoding="utf-8")
    m = re.search(r"<td class=\"num mono\">\$\{creditTxt == null \? '([^']*)'", js)
    assert m, "积分列缺少 creditTxt == null 分支"
    assert "该上游未提供单次积分消耗" in m.group(1)


def test_header_hint_no_longer_mentions_multiplier_column():
    """表头 tooltip 不再宣称「灰标签 = 模型积分倍率」。"""
    html = INDEX.read_text(encoding="utf-8")
    i = html.index(">积分</th>")
    start = html.rindex("<th", 0, i)
    head = html[start:i]
    assert "灰标签" not in head, "倍率标签已不再出现在此列"
    assert "拿不到真实消耗时显示 —" in head
    assert "模型顺序" in head, "要告诉用户倍率去哪看"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 跑前端 JS")
def test_render_leaves_credit_cell_empty_without_real_cost():
    """真跑渲染：无 credit 的行给 —，有 credit 的行给数值，倍率绝不出现。"""
    js = CHART_LOG.read_text(encoding="utf-8")
    start = js.index("el.innerHTML = rows.map")
    end = js.index("}).join('');", start)
    assert end > start, "切片落空：join 的位置必须在该渲染体之后"
    body = js[start:end + len("}).join('');")]

    script = (
        "globalThis.STATS = {credits_map: {'codebuddy/glm-5.3': 'x0.11 credits'}};\n"
        "globalThis.esc = s => String(s ?? '');\n"
        "globalThis.fmtMs = n => n + 'ms';\n"
        "globalThis.fmtNum = n => String(n);\n"
        "globalThis.fmtTime = t => 'T';\n"
        "globalThis.fmtCredit = n => String(n);\n"
        "globalThis.fmtClient = c => String(c);\n"
        "globalThis.pcolor = () => '#000';\n"
        "const el = {innerHTML: ''};\n"
        "const rows = [\n"
        "  {ts: 1, provider: 'codebuddy', model: 'glm-5.3', status: 200,\n"
        "   credit: null, prompt_tokens: 1, completion_tokens: 1},\n"
        "  {ts: 2, provider: 'codebuddy', model: 'glm-5.3', status: 200,\n"
        "   credit: 0.42, prompt_tokens: 1, completion_tokens: 1},\n"
        "];\n"
        + body + "\n"
        "console.log(JSON.stringify({html: el.innerHTML}));\n"
    )
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "case.js"
        f.write_text(script, encoding="utf-8")
        out = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    import json
    html = json.loads(out.stdout.strip().splitlines()[-1])["html"]
    # 每行 9 列；积分是第 8 列（索引 7）。按行切再取该列。
    rows_html = re.findall(r"<tr>(.*?)</tr>", html, re.S)
    assert len(rows_html) == 2, (len(rows_html), html[:300])
    def credit_cell(row):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        return tds[7]
    cells = [credit_cell(r) for r in rows_html]
    assert "x0.11" not in html, "倍率绝不能出现在积分列"
    assert "credits" not in html, "倍率串也不该出现"
    assert "—" in cells[0], "无真实消耗应留 —"
    assert "0.42" in cells[1], "有真实消耗照常显示"


def test_model_order_page_still_shows_multiplier():
    """倍率没被删掉，只是不再进日志的积分列。"""
    rows = (STATIC / "app_order_rows.js").read_text(encoding="utf-8")
    assert "credits_map" in rows
    assert "积分倍率" in rows
