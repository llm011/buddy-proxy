"""多账号通道标题行「跨账号合计」的前端测试。

用户 2026-11-02：「有的渠道会有多个账号，这种是不是可以在额度的标题那一行，
展示下总的有多少积分（把几个账号累加）」。子卡上的合计行只算**一个账号内**的
几份资源包，多账号通道里真正的总数只能自己拿计算器加。

三条口径（缺一条就会显示成假数字）：

1. 按区分别累加——CN 与 Global 是不同面板 / 不同 provider，绝不合并；
2. 每个账号取哪个数跟子卡合计一致（``quotaSumOf``），不另立口径；
3. 上游只给比例（``percent``）的账号**跳过**并明确标出「N 个未计入」，
   不拿百分比反推绝对量拼一个看起来完整的假数。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
QUOTA_JS = STATIC / "benefits_quota.js"
FUNC_RE = re.compile(r"function quotaAccountTotal\(groups, opts\) \{(.*?)\n\}\n", re.S)
#: quotaAccountTotal 依赖的两个纯函数（同一个文件里、纯 JS，不含模板字符串里的反引号）
_DEP_FNS = ("quotaSumOf", "quotaHeadSumHtml")


def _extract(text: str, name: str) -> str:
    match = re.search(rf"^function {name}\(.*?\n\}}\n", text, re.S | re.M)
    assert match, f"benefits_quota.js 里找不到 {name}（被改名/挪走了？）"
    return match.group(0)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑内联 JS（CI 有，本机可选）"
)


def _run_js(body: str) -> str:
    """抽 quotaSumOf / quotaHeadSumHtml / quotaAccountTotal 跑一遍（带最小桩）。"""
    text = QUOTA_JS.read_text(encoding="utf-8")
    match = FUNC_RE.search(text)
    assert match, "benefits_quota.js 里找不到 quotaAccountTotal（被改名/挪走了？）"
    src = "".join(_extract(text, n) for n in _DEP_FNS) + match.group(0)
    stub = """
globalThis.esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
globalThis.fmtNum = n => (Math.round(n * 100) / 100).toLocaleString('en-US');
"""
    proc = subprocess.run(
        ["node", "-e", stub + _extract(text, "quotaHeadSum")
         + src + "\n" + body],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def _render(expr: str) -> str:
    return _run_js(f"console.log({expr});").strip()


# Qoder 单账号的三份并存额度（真实返回值，见 test_quota_headline_ui.py）
_QODER_ONE = """[
  {label: '订阅额度', remaining: 1621, total: 2000, unit: 'credit'},
  {label: '加油包', remaining: 100, total: 400, unit: 'credit'},
  {label: 'Qwen 专属积分', remaining: 1030, total: 2000, unit: 'credit'},
]"""


def test_three_accounts_are_summed_with_sum_items():
    """三个账号各自 2751/4400，合计 8253/13200——这是用户真正想问的数。"""
    out = _render(f"""
quotaAccountTotal(new Map([
  ['Qoder #1', {_QODER_ONE}],
  ['Qoder #2', {_QODER_ONE}],
  ['Qoder #3', {_QODER_ONE}],
]), {{sumItems: true}})""")
    assert "8,253" in out, f"三个账号该加总成 8253，实际: {out}"
    assert "13,200" in out, f"总额该是 13200，实际: {out}"
    assert "3 账号合计" in out, out
    # 单账号那份合计（2751）不该被当成总数
    assert "剩 2,751" not in out, out


def test_single_account_panel_renders_nothing():
    """单账号不显示：子卡上已经有一行合计，标题再写一遍没有信息量。"""
    out = _render(f"quotaAccountTotal(new Map([['Qoder', {_QODER_ONE}]]), {{sumItems: true}})")
    assert out == '', f"单账号不该出 chip: {out}"
    assert _render(f"quotaAccountTotal(new Map(), {{sumItems: true}})") == ''


def test_percent_only_account_is_skipped_and_counted():
    """上游只给比例的账号不能反推绝对量：跳过，并在 chip 里写明「1 个未计入」。

    Kimi 在多账号下按账号分组；某账号额度只剩 ``percent`` 形态时，拿它的
    百分比去乘别人那 2000 分母，就是个凭空造出来的数。
    """
    out = _render("""
quotaAccountTotal(new Map([
  ['Kimi #1', [{label: '7 天池', remaining: 1200, total: 2000, unit: 'credit'}]],
  ['Kimi #2', [{label: '7 天池', remaining: 800, total: 2000, unit: 'credit'}]],
  ['Kimi #3', [{label: '7 天池', remaining: 45, total: 100, unit: 'percent'}]],
]))""")
    assert "2,000" in out and "4,000" in out, f"只该加两个绝对量账号: {out}"
    assert "2 账号合计" in out, out
    assert "1 个未计入" in out, f"跳过的账号必须明说: {out}"
    assert "未计入 1 个：Kimi #3" in out, f"悬停提示要点名哪个账号: {out}"


def test_trae_takes_the_total_row_not_the_pack_breakdown():
    """Trae 的「总额度」是各权益包的合计，按它累加；不能把权益包再加一遍。"""
    out = _render("""
quotaAccountTotal(new Map([
  ['Trae #1', [{label: '总额度', remaining: 500, total: 1000, unit: 'credit', head_only: true},
               {label: '权益包', remaining: 300, total: 300, unit: 'credit'}]],
  ['Trae #2', [{label: '总额度', remaining: 700, total: 1000, unit: 'credit', head_only: true},
               {label: '权益包', remaining: 200, total: 200, unit: 'credit'}]],
]))""")
    assert "1,200" in out and "2,000" in out, f"两个总额度相加 1200/2000: {out}"
    assert "1,700" not in out, f"不该把权益包明细重复加进去: {out}"


def test_trae_intl_dollar_bucket_is_labelled():
    """Trae 海外版是美元余额，chip 里点名量纲，免得被当成积分数。"""
    out = _render("""
quotaAccountTotal(new Map([
  ['Trae 海外版 #1', [{label: '总额度', remaining: 12, total: 20, unit: 'dollar'}]],
  ['Trae 海外版 #2', [{label: '总额度', remaining: 8, total: 20, unit: 'dollar'}]],
]))""")
    assert "20" in out and "40" in out, out
    assert "美元" in out, f"美元量纲该点名: {out}"


def test_mixed_units_are_never_merged():
    """万一两个量纲挤进同一个面板，也必须各出一个 chip，不能相加。"""
    out = _render("""
quotaAccountTotal(new Map([
  ['A', [{label: '总额度', remaining: 500, total: 1000, unit: 'credit'}]],
  ['B', [{label: '总额度', remaining: 12, total: 20, unit: 'dollar'}]],
]))""")
    assert out.count('class="mono"') == 2, f"该拆成两个 chip: {out}"
    assert "1,500" not in out and "512" not in out, f"跨量纲不能相加: {out}"


def test_notice_rows_are_not_counted_as_accounts():
    """查询失败的说明条不是「跳过的账号」：它压根没数字，也不该污染合计。"""
    out = _render("""
quotaAccountTotal(new Map([
  ['PAT #1', [{label: 'Orange 包', remaining: 400, total: 800, unit: 'credit'}]],
  ['PAT #2', [{label: 'Orange 包', remaining: 600, total: 800, unit: 'credit'}]],
  ['PAT #3', [{label: '查询失败', remaining: '账号额度查询失败（已缓存）',
               unit: 'credit', query_failed: true}]],
]))""")
    assert "1,000" in out and "1,600" in out, out
    # 失败的那个跟子卡标题行一样：没有数字就不参与（既不进合计，也不标「未计入」）
    assert "2 账号合计" in out, out
    assert "未计入" not in out, out
    assert "查询失败" not in out, f"说明文案不该混进 chip: {out}"


def test_antigravity_permille_is_labelled():
    """Antigravity 的 total 恒为 1000（千分制水位），加出来不是积分数。"""
    out = _render("""
quotaAccountTotal(new Map([
  ['AG #1', [{label: 'Gemini 组', remaining: 900, total: 1000, unit: 'permille'}]],
  ['AG #2', [{label: 'Gemini 组', remaining: 800, total: 1000, unit: 'permille'}]],
]))""")
    assert "1,700" in out and "2,000" in out, out
    assert "千分制水位" in out, f"千分制要说明，别当成积分: {out}"


def test_all_accounts_failed_renders_nothing():
    """一个能算的都没有时不出空 chip（否则是「剩 0 / 0」）。"""
    out = _render("""
quotaAccountTotal(new Map([
  ['A', [{label: 'x', remaining: '查询失败', query_failed: true}]],
  ['B', [{label: 'x', remaining: '未登录', query_failed: true}]],
]))""")
    assert out == '', out


def test_head_sum_output_is_unchanged_after_the_extraction():
    """``quotaHeadSum`` 拆成纯函数 + 渲染以后，输出必须逐字节不变。"""
    out = _run_js(f"console.log(quotaHeadSum({{sum_items: true, items: {_QODER_ONE}}}))")
    assert "2,751" in out and "4,400" in out, out
    assert "已用 37%" in out, out
    assert "3 项合计" in out, out
    single = _run_js("console.log(quotaHeadSum({items: [{remaining: 652, total: 2000}]}))")
    assert "652" in single and "2,000" in single and "已用 67%" in single, single
    assert "合计" not in single, single


#: 多账号面板文件 → 应有的接入处数（codebuddy / trae / qoder 各有 CN + 海外两张卡）
_MULTI_ACCOUNT_PANELS = {
    "benefits_kimi.js": 1,
    "benefits_qoder.js": 2,
    "benefits_antigravity.js": 1,
    "benefits_panels.js": 1,      # traepat
    "benefits_codebuddy.js": 2,
    "benefits_trae.js": 2,
}


@pytest.mark.parametrize(("name", "want"), sorted(_MULTI_ACCOUNT_PANELS.items()))
def test_every_multi_account_panel_wires_the_total_in(name: str, want: int):
    """每个多账号面板都要接上（含海外版的两张卡）——漏一个就是「有的通道没有」。"""
    text = (STATIC / name).read_text(encoding="utf-8")
    assert text.count("quotaAccountTotal(groups") == want, f"{name} 接入处数不对"
    # 每处都必须紧跟在面板标题行的 ``grow`` 之后（往前 200 字内能找到 ``.pat-head``）
    for m in re.finditer(r"quotaAccountTotal\(groups", text):
        window = text[max(0, m.start() - 200):m.start()]
        assert 'class="pat-head"' in window, f"{name}: chip 被挤出面板标题行"
