"""打卡「下次时间」前端逻辑（index.html 内联 JS）的离网测试。

后端算得再对，前端渲染/定时器错了也是白搭，而这类 bug 在 Python 测试里
看不见。这里把 index.html 里那段 JS 原样抽出来，用最小 DOM 桩跑：

- **渲染**：``nextTimeHtml`` 的措辞（下次 / 截止）、「≈」只在推断值上出现、
  所有上游字段都过 ``esc``（``next_ts_source`` 是上游字符串，会进 title）
- **定时器生命周期**：只在有元素且页面可见时跑，离开即停且不重复创建

JS 由本机 ``node`` 执行；没有 node 时整文件跳过（不是失败）——CI 有 node，
本地开发也不该因为缺个可选工具就红。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
INDEX = STATIC / "index.html"
BENEFITS_JS = STATIC / "benefits.js"  # 「下次时间」那段 JS 拆到这里（2026-10-03）
STYLE_CSS = STATIC / "style.css"      # 布局/容器查询断言从这读
SECTION_RE = re.compile(r'// ---- 打卡「下次时间」----(.*?)\n// ---- 打卡日历', re.S)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑内联 JS（CI 有，本机可选）"
)


def _extract_section() -> str:
    text = BENEFITS_JS.read_text(encoding="utf-8")
    match = SECTION_RE.search(text)
    assert match, "benefits.js 里找不到「下次时间」那段 JS（函数被改名/挪走了？）"
    return match.group(1)


def _run_js(body: str) -> str:
    """跑一段 JS，返回 stdout。带最小 DOM 桩（只为跑纯函数/定时器逻辑）。"""
    stub = """
let INTERVALS = [], CLEARED = [];
globalThis.setInterval = (fn, ms) => { INTERVALS.push({id: INTERVALS.length + 1, ms}); return INTERVALS.length; };
globalThis.clearInterval = id => { CLEARED.push(id); };
let ELS = [];
const PAGE = { classList: { contains: () => true } };
globalThis.document = {
  addEventListener() {}, visibilityState: 'visible',
  getElementById: id => (id === 'page-checkin' ? PAGE : null),
  querySelector: sel => (sel === '[data-next-ts]' ? (ELS[0] || null) : null),
  querySelectorAll: sel => (sel === '[data-next-ts]' ? ELS : []),
};
globalThis.esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
globalThis.fmtCredit = v => String(v);
globalThis.__els = ELS;
// 冻结时钟：倒计时/日期措辞的断言必须确定性。不冻的话两次 Date.now()
// 之间过去几毫秒，``now+60`` 就会算成 59 秒，测试随机器负载飘。
globalThis.__freeze = ms => {
  const RealDate = Date;
  globalThis.Date = class extends RealDate {
    constructor(...a) { a.length === 0 ? super(ms) : super(...a); }
    static now() { return ms; }
  };
};
"""
    script = stub + _extract_section() + "\n" + body
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


# --- 渲染 -------------------------------------------------------------------


def test_renders_both_next_and_deadline_wording():
    """已领取说「下次」，还没领说「截止」——此刻该做的事不同。"""
    out = _run_js("""
const now = Math.floor(Date.now() / 1000);
console.log(JSON.stringify({
  claimed: nextTimeHtml({next_ts: now + 3600, next_ts_source: 'upstream',
                         checked_in: true, claimable: false, done_today: true}),
  pending: nextTimeHtml({next_ts: now + 3600, next_ts_source: 'upstream',
                         claimable: true, done_today: false}),
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "下次" in data["claimed"], data["claimed"]
    assert "截止" in data["pending"], data["pending"]


def test_inferred_source_is_marked_but_upstream_is_not():
    """推断值必须有可辨识的标记——那是从打卡记录反推的，不是上游契约。

    标记从「贴在末尾的 ≈」换成了 ``.inferred`` 类（虚线框 + 斜体）：贴尾巴上
    读着像错字，而且说不清「哪里不确定」。断言仍要盯住「用户能不能看出来」，
    所以这里查类名而不是某个具体字形。
    """
    out = _run_js("""
const now = Math.floor(Date.now() / 1000);
console.log(JSON.stringify({
  inferred: nextTimeHtml({next_ts: now + 3600, next_ts_source: 'inferred', done_today: true}),
  upstream: nextTimeHtml({next_ts: now + 3600, next_ts_source: 'upstream', done_today: true}),
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "br-next inferred" in data["inferred"], "推断值必须标出来"
    assert "inferred" not in data["upstream"], "上游给的不该标推测"
    assert "本地零点" in data["inferred"], "推断值的 tooltip 要说明依据"


def test_missing_next_ts_renders_nothing():
    """算不出下次（活动结束/档期未开）→ 不渲染任何东西，别给个空壳 tag。"""
    out = _run_js("""
console.log(JSON.stringify({
  none: nextTimeHtml({checked_in: true, done_today: true}),
  nullish: nextTimeHtml({next_ts: null, done_today: true}),
  empty: nextTimeHtml(null),
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["none"] == ""
    assert data["nullish"] == ""
    assert data["empty"] == ""


def test_countdown_tail_is_not_rendered():
    """「21 小时 33 分后」的倒计时尾巴不再出现在卡上——用户反馈第一行太挤，
    绝对时刻（明天 00:00）已够用。fmtCountdown 与 .cd 样式也已一并删除。"""
    out = _run_js("""
const now = Math.floor(Date.now() / 1000);
console.log(nextTimeHtml({next_ts: now + 3600 * 21 + 33 * 60,
                          next_ts_source: 'upstream', done_today: true}));
""")
    assert "data-next-cd" not in out, "倒计时尾巴已按反馈移除，别又渲染回来"
    assert "小时" not in out and "分后" not in out, f"相对时长不该出现: {out}"
    assert "data-next-clock" in out, "绝对时刻仍要保留"


def test_upstream_source_never_reaches_the_dom():
    """``next_ts_source`` 只被当**判据**用，绝不整串拼进 HTML。

    曾经的写法容易是 ``title="${esc(c.next_ts_source)}"``——那样一旦上游给个
    奇怪的值就多一处注入面。这里断言它根本没进输出：无论传什么，
    ``title`` 都只能是代码里写死的那两句。
    """
    out = _run_js("""
console.log(nextTimeHtml({next_ts: 1, next_ts_source: '"><img src=x onerror=alert(1)>',
                          done_today: true}));
""")
    assert "onerror" not in out and "<img" not in out, f"上游字符串漏进 DOM: {out}"
    assert 'title="时刻来自上游返回的时间窗"' in out, f"未知 source 应退回上游措辞: {out}"

    inferred = _run_js("""
console.log(nextTimeHtml({next_ts: 1, next_ts_source: 'inferred', done_today: true}));
""")
    assert 'title="上游未提供每日轮换时刻，按打卡记录推断为本地零点"' in inferred


def test_countdown_function_is_gone():
    """fmtCountdown 已随倒计时尾巴一起删掉：留着只会让人以为还有地方在展示。"""
    assert "function fmtCountdown" not in _extract_section(), \
        "fmtCountdown 应已删除（倒计时尾巴撤了，死代码不留）"


# --- 定时器生命周期 ---------------------------------------------------------


def test_timer_starts_once_and_stops_when_no_elements():
    """有元素才起、不重复创建；元素没了就停（不空转）。"""
    out = _run_js("""
ELS.push({dataset: {nextTs: String(Math.floor(Date.now()/1000) + 3600)},
          querySelector: () => null});
syncNextTimeAuto(); syncNextTimeAuto(); syncNextTimeAuto();
const started = INTERVALS.length, ms = INTERVALS[0] && INTERVALS[0].ms;
ELS.length = 0;
syncNextTimeAuto();
console.log(JSON.stringify({started, ms, cleared: CLEARED.length}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["started"] == 1, "重复调用不该创建多个定时器"
    assert data["ms"] == 1000, "倒计时要每秒走一格"
    assert data["cleared"] == 1, "元素消失后必须停掉"


def test_timer_stops_when_page_hidden():
    """标签页切走即停——后台每秒钟重排 DOM 纯属浪费。"""
    out = _run_js("""
ELS.push({dataset: {nextTs: '1'}, querySelector: () => null});
syncNextTimeAuto();
document.visibilityState = 'hidden';
syncNextTimeAuto();
console.log(JSON.stringify({started: INTERVALS.length, cleared: CLEARED.length}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["started"] == 1
    assert data["cleared"] == 1, "页面隐藏后必须停掉"


def test_timer_restarts_after_returning():
    """切回来要能重新起（停掉后不留「已死」的全局状态）。"""
    out = _run_js("""
ELS.push({dataset: {nextTs: '1'}, querySelector: () => null});
syncNextTimeAuto();
ELS.length = 0; syncNextTimeAuto();          // 停
ELS.push({dataset: {nextTs: '1'}, querySelector: () => null});
syncNextTimeAuto();                          // 再起
console.log(JSON.stringify({total: INTERVALS.length, cleared: CLEARED.length}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["total"] == 2, "回来后应重新创建定时器"
    assert data["cleared"] == 1


def test_render_updates_text_without_touching_dataset():
    """tick 只改文案，不改 ``data-next-ts``。倒计时撤了，只剩时钟要刷。"""
    out = _run_js("""
const ts = Math.floor(Date.now()/1000) + 3600;
const clock = {textContent: ''};
let asked = [];
ELS.push({dataset: {nextTs: String(ts)},
          querySelector: sel => { asked.push(sel); return clock; }});
renderNextTimes();
console.log(JSON.stringify({tsAfter: ELS[0].dataset.nextTs, clock: clock.textContent,
                            orig: String(ts), asked}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["tsAfter"] == data["orig"], "不该改写 data-next-ts"
    assert data["clock"], "时钟文案要被刷新"
    assert data["asked"] == ["[data-next-clock]"], "不该再找倒计时元素"


# --- 布局（静态断言，无需浏览器）--------------------------------------------


def test_narrow_layout_keeps_button_off_the_meta_row():
    """窄卡片的容器查询里，按钮与状态必须**错开行**，否则会重叠。

    实测过的 bug：曾把按钮写成 ``grid-area: 1 / 2 / 3 / 3``（跨两行居中），
    而 ``.br-meta`` 是 ``2 / 1 / 2 / -1`` 横跨整行——两者在第 2 行右半区相交，
    「22 小时 18 分后」被压在按钮底下（用户截图报的重叠就是这个）。
    grid 不做重叠检测，重叠了也不报错，所以只能用静态断言盯住行号。
    """
    text = STYLE_CSS.read_text(encoding="utf-8")
    block = re.search(r"@container \(max-width: 640px\)\s*\{(.*?)\n  \}", text, re.S)
    assert block, "找不到窄卡片那段容器查询（改过选择器？）"
    # 必须剥掉注释再匹配：那段块的注释里为了说明来龙去脉，恰好写了
    # `.br-meta{grid-column:1/-1}` 这种**反例**写法，不剥注释就会先命中它，
    # 断言于是去检查一句注释——真实声明反而没被看见。
    css = re.sub(r"/\*.*?\*/", "", block.group(1), flags=re.S)

    act = re.search(r"\.br-act\s*\{([^}]*)\}", css)
    meta = re.search(r"\.br-meta\s*\{([^}]*)\}", css)
    assert act and meta, "容器查询里应同时定位 .br-act 与 .br-meta"

    def row_span(decl: str) -> tuple[int, int]:
        """把 ``grid-area`` 解析成行区间 ``[start, end)``。

        ``grid-area`` 的四个值是 ``行起 / 列起 / 行止 / 列止``，所以行止是
        第 3 个值，不是第 2 个。

        ``1 / 2``（只给行起和列起）是合法且在本例里正常的写法——按钮本来只要
        占一行，行止省略即跨一行。所以这里默认按跨一行处理，只把**看不出行
        跨度**的写法判失败：没写 ``grid-area``、行止是 ``-1`` 这类负值。

        注意 ``1 / 2`` 与 ``1 / 2 / 2 / 3`` 在本例**同义**（都占一行），别以为
        省略行止和明写行止会被区别对待。反过来说：``.br-meta`` 那种要**横跨
        整行**的规则必须明写行止（``2 / 1 / 2 / -1``），照「省略即跨一行」的
        直觉省掉它就会踩空。
        """
        m = re.search(r"grid-area:\s*([^;}]+)", decl)
        assert m, f"这段规则里没写 grid-area，行跨度无从判断（容易又重叠）: {decl.strip()!r}"
        parts = [p.strip() for p in m.group(1).split("/")]
        start = int(parts[0])                    # 行起
        end = start + 1                          # 没写行止 = 跨一行（合法）
        if len(parts) >= 3:
            tail = parts[2]                      # 行止（第 3 个值）
            if tail.startswith("span"):
                end = start + int(re.findall(r"\d+", tail)[0])
            elif tail.startswith("-"):
                raise AssertionError(
                    f"行止写成负值（-1 = 最后一行）没法比大小，"
                    f"请改成明确行号: grid-area: {m.group(1)!r}"
                )
            else:
                end = int(tail)
        if end == start:
            # 行起 == 行止（如 ``2 / 1 / 2 / -1``）：浏览器**不是**当成零高，
            # 而是照第 start 行放一行。实测过，别按直觉改：
            # getComputedStyle 仍是 row-start:2/row-end:2，但和显示写 3 的
            # 对照组渲染位置完全一致（都在 top 21，第 1 行 17px + 4px gap 之后）。
            end = start + 1
        assert end > start, f"解析不出行区间: grid-area: {m.group(1)!r}"
        return start, end

    act_rows, meta_rows = row_span(act.group(1)), row_span(meta.group(1))
    overlap = act_rows[0] < meta_rows[1] and meta_rows[0] < act_rows[1]
    assert not overlap, (
        f"按钮行 {act_rows} 与状态行 {meta_rows} 相交 → 窄屏下会重叠"
    )


def test_checkin_grid_is_self_adapting():
    """签到区改成自适应卡片网格，不再依赖容器查询。

    2026-11-07 卡片化：行式列表靠 ``.rows-card`` 的 container query 在窄栏折行；
    改成 ``auto-fill`` 网格后列数自己随宽度变，折行逻辑不再需要（原 bug
    「窄栏把按钮挤走」从结构上消失）。这里锁住「网格按最小卡宽自适应」，
    以及窄屏兜底成单列。
    """
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert re.search(r"\.ck-grid\s*\{[^}]*grid-template-columns:\s*repeat\(auto-fill,\s*minmax\(330px,\s*1fr\)\)",
                     css), ".ck-grid 要按最小卡宽自适应列数"
    assert re.search(r"@media \(max-width: 640px\)\s*\{.*?\.ck-grid\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\)",
                     css, re.S), "窄屏要兜底成单列，否则卡片被压扁"
    html = INDEX.read_text(encoding="utf-8")
    assert 'class="ck-grid" id="checkin-rows"' in html, "签到区没挂 .ck-grid"
    assert 'id="page-checkin"' in html, "签到没独立成页"
