"""额度卡标题行「剩 X / Y」汇总的前端测试。

Qoder 的额度面板有三份**并存**的额度（订阅额度 / 加油包 / 专属积分），加起来
才是账号总量，但标题行原先一律取 ``items[0]``，于是只显示 1621/2000，把另
外两份（100 + 1030）漏掉了——用户看到的数比实际少一截。

修法是后端用 ``sum_items`` 显式声明「这些条目可合计」，前端只认这个标记：
各家 items 语义不同（Trae 的权益包是总额度的明细，加了就重复计算；ZCode /
MiMo 各项量纲不同），一律相加会把它们全算错，所以必须由数据源声明而不是猜。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
# quotaHeadSum 在 static/benefits_quota.js；调用方留在 static/benefits.js。
BENEFITS_JS = STATIC / "benefits.js"
QUOTA_JS = STATIC / "benefits_quota.js"
FUNC_RE = re.compile(r"function quotaHeadSum\(q\) \{(.*?)\n\}\n", re.S)
#: quotaHeadSum 只剩两行转发——真正取数是这两个纯函数（跨账号合计也共用）
_HELPERS = ("quotaSumOf", "quotaHeadSumHtml")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑内联 JS（CI 有，本机可选）"
)


def _run_js(body: str) -> str:
    """抽出 quotaHeadSum 跑一遍（带最小 DOM 无关的桩）。"""
    text = QUOTA_JS.read_text(encoding="utf-8")
    match = FUNC_RE.search(text)
    assert match, "benefits.js 里找不到 quotaHeadSum（被改名/挪走了？）"
    helpers = "".join(_extract_fn(text, n) for n in _HELPERS)
    stub = """
globalThis.esc = s => String(s);
globalThis.fmtNum = n => (Math.round(n * 100) / 100).toLocaleString('en-US');
"""
    proc = subprocess.run(
        ["node", "-e", stub + helpers + match.group(0) + "\n" + body],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


QODER = """{
  sum_items: true,
  items: [
    {label: '订阅额度', remaining: 1621, total: 2000},
    {label: '加油包', remaining: 100, total: 400},
    {label: 'Qwen 专属积分', remaining: 1030, total: 2000},
  ],
}"""


def test_sum_items_adds_every_bucket_up():
    """置了 sum_items 就要把三份额度**全加起来**，这才是账号真实剩余。

    数字取自真实返回值（2026-09-29 抓取）：三份合计 2751/4400，已用 37.48%；
    上游自己的 ``totalUsagePercentage`` 是 0.38，正好对得上——这是「相加才
    是正确口径」的独立佐证。
    """
    out = _run_js(f"console.log(quotaHeadSum({QODER}));")
    assert "2,751" in out, f"应显示三份合计 2751，实际: {out}"
    assert "4,400" in out, f"应显示合计总额 4400，实际: {out}"
    # 旧 bug 的特征值：只显示第一份
    assert "1,621" not in out, f"不该只显示订阅额度那一份: {out}"
    assert "3 项合计" in out, "多于一项时要说清这是几项的合计"


def test_without_the_flag_it_keeps_the_old_single_item_behaviour():
    """没声明 sum_items 的通道必须维持旧行为（取第一条）。

    否则就会把 Trae 这类「总额度 + 权益包明细」的通道算重复。这条是防回归的
    关键：修复不能变成「所有通道都相加」。
    """
    out = _run_js(f"""console.log(quotaHeadSum({{
      items: [{{label: '总额度', remaining: 500, total: 1000}},
              {{label: '权益包', remaining: 300, total: 300}}],
    }}));""")
    assert "500" in out and "1,000" in out, f"应取第一条: {out}"
    assert "800" not in out, f"不该把权益包加进来（那是明细，会重复计算）: {out}"
    assert "合计" not in out, "单条不该标「合计」"


def test_single_item_sum_does_not_say_jihe():
    """只有一条时不必标「1 项合计」——读着啰嗦。"""
    out = _run_js("console.log(quotaHeadSum({sum_items: true, items: [{remaining: 5, total: 9}]}));")
    assert "5" in out and "9" in out
    assert "合计" not in out, f"单项不用标合计: {out}"


def test_items_without_numbers_are_skipped_and_empty_renders_nothing():
    """没有 remaining/total 的条目不参与合计；一条可用都没有就别渲染。

    漏掉「全不可用」这步的话会渲染成「剩 0 / 0」，看着像额度真的用光了。
    """
    out = _run_js("""console.log(JSON.stringify({
      partial: quotaHeadSum({sum_items: true, items: [
        {label: 'A', remaining: 10, total: 20},
        {label: '查询失败', remaining: '网络不可达'},      // 文案，不是数
        {label: 'B', remaining: 5, total: 10},
      ]}),
      bridge: quotaHeadSum({sum_items: true, items: [
        {label: '只给 total', total: 300},
      ]}),
      none: quotaHeadSum({sum_items: true, items: []}),
      noitems: quotaHeadSum({sum_items: true}),
    }));""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "15" in data["partial"] and "30" in data["partial"], data["partial"]
    assert "网络不可达" not in data["partial"]
    assert data["bridge"] == "", f"只有 total 不算有数: {data['bridge']}"
    assert data["none"] == "", "空列表不该渲染"
    assert data["noitems"] == "", "没有 items 字段也不该炸"


def test_failure_notices_never_get_summed_into_the_headline():
    """「查询失败 / 网关不可达」说明条不能被当数字求和。

    ``trae.pat.quota._failure_notice`` 把说明条插在 items 首位，``remaining``
    是**文案**（「2/9 个账号本轮未取到新数据」）而不是数。只要过滤条件写成
    ``remaining != null``，文案就会过筛，``Number(...)`` 得 NaN，标题行渲染成
    「剩 NaN / NaN」——比不显示还糟。

    同时要**保住数字字符串**（``"2000"``）：上游发 zcode 的 unit/number 就
    用过字符串，额度字段也可能这么来；用 ``Number.isFinite`` 直接筛会把这种
    能算的值误杀。
    """
    out = _run_js("""console.log(JSON.stringify({
      notice: quotaHeadSum({sum_items: true, items: [
        {label: 'PAT 额度网关不可达', remaining: '2/9 个账号本轮未取到新数据（网络不可达）',
         total: null, unreachable: true, query_failed: true},
        {label: '账号 A', remaining: 100, total: 200}]}),
      allnotice: quotaHeadSum({sum_items: true, items: [
        {label: '查询失败', remaining: '9/9 个账号查询失败', total: null}]}),
      numeric_string: quotaHeadSum({sum_items: true, items: [
        {label: 'A', remaining: '2000', total: '4000'}]}),
      bools: quotaHeadSum({sum_items: true, items: [
        {label: 'A', remaining: true, total: true}]}),
    }));""")
    data = json.loads(out.strip().splitlines()[-1])

    assert "NaN" not in data["notice"], f"说明条被算进去了: {data['notice']}"
    assert "100" in data["notice"] and "200" in data["notice"], data["notice"]
    assert "个账号" not in data["notice"], "文案不该出现在标题行"
    assert data["allnotice"] == "", f"全是说明条就该不显示: {data['allnotice']}"
    assert "2,000" in data["numeric_string"] and "4,000" in data["numeric_string"], \
        f"数字字符串是可算的，不该被滤掉: {data['numeric_string']}"
    assert data["bools"] == "", f"true/false 不是额度: {data['bools']}"


def test_the_render_path_uses_the_helper_not_a_bare_find():
    """渲染处必须调 ``quotaHeadSum``，不能再自己 ``find`` 第一条。

    这条盯的是「改了个函数但没接上去」——helper 写得再对，渲染处若仍留着
    ``.find(it => ...)``，界面就还是老样子（这个 bug 的本质就是渲染处的取值）。
    """
    text = BENEFITS_JS.read_text(encoding="utf-8")
    assert "quotaHeadSum(q)" in text, "渲染处没有调用 quotaHeadSum"
    # 只扫渲染那段（quota-list 的赋值处），不能全文件扫：helper 自己内部
    # 就写着 ``const head = usable[0]``（未声明可合计时的兜底），全局扫会误报
    render = text[text.index("getElementById('quota-list')"):]
    render = render[:render.index("renderTraepatPanel")]
    assert "quotaHeadSum(q)" in render, "渲染处没有调用 quotaHeadSum"
    assert ".find(it => it.remaining" not in render, \
        "渲染处仍留着直接 find 第一条的写法（helper 没接上）"


# ---------------------------------------------------------------------------
# 到期告警横幅（2026-10-03）
# ---------------------------------------------------------------------------

#: 横幅渲染要碰 DOM（读 #expirybar、写 className/innerHTML），这里用最小桩
#: 顶上。桩把每次写入记下来，跑完 console.log 出去供断言——这样测的是
#: 「renderExpiryBanner 到底往元素上写了什么」，而不是「函数内部长什么样」。
_DOM_STUB = """
globalThis.__el = {className: '', innerHTML: '', open: false};
globalThis.__writes = [];
globalThis.document = {getElementById: id => id === 'expirybar' ? globalThis.__el : null};
function __snap() {
  globalThis.__writes.push({className: globalThis.__el.className,
                            innerHTML: globalThis.__el.innerHTML});
  return globalThis.__el.className + '\\u0000' + globalThis.__el.innerHTML;
}
"""

#: renderExpiryBanner 走两级分组渲染，依赖链要一起抽
_EXPIRY_FNS = (
    "fmtExpireDate", "fmtDaysLeft", "_expiryAmountOf", "_expiryItemName",
    "_expiryLowText", "_acctNum", "buildAlertGroups", "renderAlertGroupBody",
    "renderAlertGroup", "bindExpiryFold", "renderExpiryBanner",
)


def _extract_fn(text: str, name: str) -> str:
    """抽一个顶层函数（``function name(...) { ... }`` 到独占行的 ``}``）。"""
    m = re.search(rf"^function {name}\(.*?\n\}}\n", text, re.S | re.M)
    assert m, f"benefits_quota.js 里找不到 {name}"
    return m.group(0)


def _run_fn(names: tuple[str, ...], body: str) -> str:
    """抽若干顶层函数在 node 里跑一段脚本（附 DOM 桩）。"""
    text = BENEFITS_JS.read_text(encoding="utf-8") + QUOTA_JS.read_text(encoding="utf-8")
    src = "".join(_extract_fn(text, n) for n in names)
    stub = """
globalThis.esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
globalThis.fmtNum = n => {
  if (n == null) return '—';
  const x = Number(n);
  if (!isFinite(x)) return String(n);
  return x >= 100 ? Math.round(x).toLocaleString() : String(Math.round(x * 10) / 10);
};
"""
    proc = subprocess.run(
        ["node", "-e", "var EXPIRY_FOLD = {open: {}};\n" + stub + _DOM_STUB + src + "\n" + body],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def _render_banner(benefits_js: str) -> str:
    return _run_fn(_EXPIRY_FNS, f"""
globalThis.BENEFITS = {benefits_js};
renderExpiryBanner();
console.log(__snap());
""")


def test_expiry_banner_renders_summary_and_rows():
    """有告警时：横幅显示、汇总行说清有几项、明细列出通道/名称/日期/天数。"""
    out = _render_banner("""{
      expiring: [
        {provider: 'trae', provider_name: 'trae', label: '会员 Pro 连续包月',
         expire_ts: 1792692336, days_left: 1.2, remaining: 4000, unit: 'credit'},
        {provider: 'qoder', provider_name: 'qoder', label: '加油包',
         expire_ts: 1793212321, days_left: -2.0, remaining: 950, unit: 'credit'},
      ],
    }""")
    assert 'expirybar show expired' in out, f"含已过期项该转红: {out}"
    assert '有 2 项权益已过期或即将到期' in out, out
    assert '会员 Pro 连续包月' in out and '加油包' in out, out
    assert '剩 1 天' in out, out
    assert '已过期 2 天' in out, out
    assert '剩 4,000' in out, f"积分类该带余量: {out}"


def test_expiry_banner_warns_without_expired_items():
    """都是临期、没有已过期时用黄档（不带 expired 类），文案也不说「已过期」。"""
    out = _render_banner("""{
      expiry_warn_days: 7,
      expiring: [{provider: 'trae', provider_name: 'trae', label: '签到奖励',
                  expire_ts: 1792692336, days_left: 2.0, remaining: 500, unit: 'credit'}],
    }""")
    assert 'expirybar show' in out and 'expired' not in out, f"该是黄档: {out}"
    assert '将在 7 天内到期' in out, out


def test_expiry_banner_takes_window_from_response():
    """汇总文案的「N 天」以后端下发为准，不是前端写死的 7。

    写死的话，后端把窗口调成 3 天时文案还说 7 天——用户看到「有 1 项将在 7 天
    内到期」点开发现是 3 天后，比不写更困惑。
    """
    out = _render_banner("""{
      expiry_warn_days: 3,
      expiring: [{provider: 'trae', provider_name: 'trae', label: '加油包',
                  expire_ts: 1792692336, days_left: 2.0, remaining: 900, unit: 'credit'}],
    }""")
    assert '将在 3 天内到期' in out, out
    assert '7 天' not in out, f"不该再出现写死的 7: {out}"


def test_banner_window_days_is_not_hardcoded_in_js():
    """前端不该自带天数常量——窗口只在 benefits.EXPIRY_WARN_DAYS 一处定义。"""
    text = BENEFITS_JS.read_text(encoding="utf-8")
    assert "EXPIRY_WARN_DAYS" not in text, \
        "benefits.js 又出现了自带的天数常量（应改为读后端下发的 expiry_warn_days）"


def test_expiry_banner_hides_when_no_alerts():
    """expiring 为空 → 横幅隐藏且清空内容（不留上一次的残留）。"""
    out = _render_banner("{expiring: []}")
    assert 'expirybar' in out and 'show' not in out, f"该隐藏: {out}"
    assert '⏳' not in out, f"内容该清空（上次的告警不能留在页面上）: {out}"


def test_expiry_banner_survives_missing_field():
    """响应里没有 expiring 字段时不能炸（前端先于后端更新的场景）。"""
    out = _render_banner("{}")
    assert 'show' not in out, out


def test_expiry_banner_non_credit_hides_amount():
    """非积分类不铺余量：mimo 的 19.5 是「还剩几天」，跟 300 积分门槛无关，
    铺出来会让人以为那是积分。"""
    out = _render_banner("""{
      expiring: [{provider: 'mimo', provider_name: 'mimo',
                  label: '套餐有效期（天）', expire_ts: 1792692336,
                  days_left: 3.0, remaining: 19.5, unit: 'day'}],
    }""")
    assert '套餐有效期（天）' in out, out
    assert '19.5' not in out, f"非积分类不该显示余量: {out}"


def test_expiry_banner_folds_same_channel_batches_into_one_row():
    """同一通道的多个包只占 1 个外层行，展开才是逐包一行（用户 2026-11-04）。

    2026-11-03 那版把同类包合并成一条「×N · 合计剩 M」——通道只占一行做到了，
    但账号归属和每个包的到期日都没了。现在改成两级折叠：外层一行，点开逐包铺。
    用例仍是 DuMate 每天一个 login_bonus 小包那个形状。
    """
    items = ",".join(
        f"{{provider: 'dumate', provider_name: '百度搭子', label: '积分包（login_bonus）',"
        f" expire_ts: {1793200000 + i * 86400}, days_left: {2 + i},"
        f" remaining: 500, unit: 'credit'}}"
        for i in range(6)
    )
    out = _render_banner("{expiring: [" + items + "]}")
    assert '有 6 项权益将在 7 天内到期' in out, out
    assert out.count('<details class="eb-group"') == 1, f"同通道只该占 1 个外层行: {out}"
    assert '6 项临期' in out, out
    assert out.count('积分包（login_bonus）') == 6, f"展开后仍是逐包一行: {out}"
    assert 'data-eb-group="dumate" open' not in out, "默认应折叠"


def test_expiry_banner_keeps_different_kinds_apart():
    """合并只针对同类：同通道的「订阅套餐」与「积分包」是两码事，不能被并掉。"""
    out = _render_banner("""{
      expiring: [
        {provider: 'dumate', provider_name: '百度搭子', label: '订阅套餐（plan_pro）',
         expire_ts: 1793200000, days_left: 3.0, remaining: 5000, unit: 'points'},
        {provider: 'dumate', provider_name: '百度搭子', label: '积分包（login_bonus）',
         expire_ts: 1793300000, days_left: 4.0, remaining: 800, unit: 'points'},
      ],
    }""")
    assert '有 2 项权益将在 7 天内到期' in out, out
    assert '订阅套餐（plan_pro）' in out and '积分包（login_bonus）' in out, out


def test_expiry_banner_drops_fully_spent_batches():
    """余量已空、且不在 4 天内的积分条目不铺出来：到期也带不走任何额度。

    用例造的是真实形状（DuMate 2026-11-03）：一批包全空 + 一条还有剩。
    空的那批要消失，有剩的那条必须留着。日期都放在 4 天窗口外——4 天内的
    空包同样不报（见 test_banner_drops_spent_items_even_when_urgent）。
    """
    out = _render_banner("""{
      expiring: [
        {provider: 'dumate', provider_name: '百度搭子', label: '积分包（login_bonus）',
         expire_ts: 1793200000, days_left: 5.0, remaining: 0, unit: 'points'},
        {provider: 'dumate', provider_name: '百度搭子', label: '积分包（login_bonus）',
         expire_ts: 1793286400, days_left: 6.0, remaining: 0, unit: 'points'},
        {provider: 'trae', provider_name: 'Trae', label: '加油包',
         expire_ts: 1793300000, days_left: 4.0, remaining: 900, unit: 'credit'},
      ],
    }""")
    assert '有 1 项权益将在 7 天内到期' in out, f"空包组该整组剔除: {out}"
    assert '百度搭子' not in out, out
    assert '加油包' in out, out


def test_expiry_banner_folds_low_and_expiry_into_one_row():
    """同一通道既到期又告急时仍只占一行，展开后两种明细都在（用户 2026-11-04）。

    旧版是「余额告急直接把到期行顶掉」，结果 4 天内到期的资源包被藏了——正是
    这次要修的。现在改成折叠：外层一行挂两个徽标，明细里两种都在。
    """
    out = _render_banner("""{
      expiring: [{provider: 'dumate', provider_name: '百度搭子',
                  label: '积分包（login_bonus）', expire_ts: 1793200000,
                  days_left: 2.0, remaining: 800, unit: 'credit'}],
      low_quota: [{provider: 'dumate', provider_name: '百度搭子',
                   label: '订阅积分', unit: 'points', remaining: 500, percent_left: 1.1}],
    }""")
    assert out.count('data-eb-group="dumate"') == 1, out
    assert '有 1 项权益将在 7 天内到期' in out, out
    assert '余额告急 · 订阅积分 · 剩 1.1%' in out, out
    assert '1 项临期' in out, out
    assert '积分包（login_bonus）' in out, "4 天内到期的资源包不能被余额告急顶掉"


def test_expiry_banner_keeps_expiry_for_other_providers():
    """只在同通道上去重：别的通道的到期行照常显示。"""
    out = _render_banner("""{
      expiring: [{provider: 'trae', provider_name: 'Trae', label: '加油包',
                  expire_ts: 1793200000, days_left: 2.0, remaining: 900, unit: 'credit'}],
      low_quota: [{provider: 'dumate', provider_name: '百度搭子',
                   label: '订阅积分', unit: 'points', remaining: 500, percent_left: 1.1}],
    }""")
    assert '有 1 项权益将在 7 天内到期' in out, out
    assert '加油包' in out and '订阅积分' in out, out


def test_expiry_banner_does_not_drop_batches_without_amounts():
    """拿不到余量的条目保持宽松放行——不能因为「判不出」就替用户吞掉告警。"""
    out = _render_banner("""{
      expiring: [{provider: 'trae', provider_name: 'Trae', label: '会员 Pro',
                  expire_ts: 1793200000, days_left: 2.0, remaining: null, unit: 'credit'}],
    }""")
    assert '会员 Pro' in out, out


def test_banner_drops_spent_items_even_when_urgent():
    """4 天内到期但余量为 0 也不铺出来（用户 2026-11-06 推翻 11-04 那条）。

    用户实报：CodeBuddy 三个剩 0 的包（资源包×2、订阅套餐）因为 2 天后到期
    被顶成「3 项临期（3 项 4 天内）」「太傻瓜了吧」。烧干的包过期也带不走任何
    额度，4 天例外只该豁免「余量小」，不豁免「余量为空」。
    """
    out = _render_banner("""{
      expiry_warn_days: 7, expiry_urgent_days: 4,
      expiring: [{provider: 'codebuddy', provider_name: 'CodeBuddy', account: '#1',
                  item_label: '资源包', label: 'CodeBuddy #1 · 资源包',
                  expire_ts: 1793932708, days_left: 1.8, remaining: 0,
                  unit: 'credit', urgent: true},
                 {provider: 'codebuddy', provider_name: 'CodeBuddy', account: '#1',
                  item_label: '订阅套餐', label: 'CodeBuddy #1 · 订阅套餐',
                  expire_ts: 1793932708, days_left: 1.8, remaining: 0,
                  unit: 'credit', urgent: true}],
    }""")
    assert '资源包' not in out and '订阅套餐' not in out, f"空包不该告警: {out}"
    assert '临期' not in out and '4 天内' not in out, out
    assert 'expirybar show' not in out, f"横幅不该出现: {out}"


def test_banner_keeps_urgent_items_with_small_amount():
    """4 天内有剩（哪怕很少）的包照报，并挂「4 天内」标签。

    这是 2026-11-04 那条规则的保留部分：额度过几天就作废，值不值得提醒不该
    再由 300 门槛决定。
    """
    out = _render_banner("""{
      expiry_warn_days: 7, expiry_urgent_days: 4,
      expiring: [{provider: 'codebuddy', provider_name: 'CodeBuddy', account: '#2',
                  item_label: '资源包', label: 'CodeBuddy #2 · 资源包',
                  expire_ts: 1793932708, days_left: 1.8, remaining: 40,
                  unit: 'credit', urgent: true}],
    }""")
    assert '资源包' in out, out
    assert '4 天内' in out, out
    assert '有 1 项权益将在 7 天内到期' in out, out


def test_banner_derives_urgency_when_backend_is_older():
    """老后端没 `urgent` 字段时按 days_left 自己判，徽标和标签才有得用。

    静态资源先更新、服务还没重启（或浏览器缓存了旧接口快照）时就是这条路径
    ——此时 `urgent` 和 `expiry_urgent_days` 都不在响应里。余量为 0 的仍要
    剔掉，别因为走的是老响应路径就把空包放回来。
    """
    out = _render_banner("""{
      expiring: [
        {provider: 'dumate', provider_name: '百度搭子', label: '积分包（login_bonus）',
         expire_ts: 1793894399, days_left: 1.3, remaining: 60, unit: 'points'},
        {provider: 'dumate', provider_name: '百度搭子', label: '积分包（login_bonus）',
         expire_ts: 1794326399, days_left: 6.3, remaining: 0, unit: 'points'},
      ],
    }""")
    assert out.count('积分包（login_bonus）') == 1, f"只该留有余量那条: {out}"
    assert '1 项临期（1 项 4 天内）' in out, out


def test_banner_groups_expiry_by_channel_into_collapsed_rows():
    """两级列表：同一通道的多个资源包只占 1 个外层行，默认收起。

    用户 2026-11-04：DuMate 每天一个小包，旧版平铺会刷一整列；现在同一
    通道折叠成一行，点开才逐包铺「账号 / 包名 / 到期日 / 天数」。
    """
    out = _render_banner("""{
      expiry_warn_days: 7, expiry_urgent_days: 4,
      expiring: [
        {provider: 'dumate', provider_name: '百度搭子', account: '',
         item_label: '积分包（login_bonus）', label: '积分包（login_bonus）',
         expire_ts: 1793807999, days_left: 0.3, remaining: 120, unit: 'points',
         urgent: true},
        {provider: 'dumate', provider_name: '百度搭子', account: '',
         item_label: '积分包（login_bonus）', label: '积分包（login_bonus）',
         expire_ts: 1793894399, days_left: 1.3, remaining: 300, unit: 'points',
         urgent: true},
        {provider: 'traeintl', provider_name: 'Trae 海外版', account: '',
         item_label: 'Pro plan', label: 'Pro plan',
         expire_ts: 1794158347, days_left: 4.4, remaining: 0, unit: 'dollar',
         urgent: false},
      ],
    }""")
    assert out.count('<details class="eb-group"') == 2, out
    assert out.count('data-eb-group="dumate"') == 1, out
    assert '百度搭子' in out and '2 项临期（2 项 4 天内）' in out, out
    assert 'Trae 海外版' in out and '4.4' not in out, out
    # 明细在 eb-sub 里，不在 summary 行；默认没有 open 属性
    assert 'eb-sub' in out and '积分包（login_bonus）' in out, out
    assert 'data-eb-group="dumate" open' not in out, "默认应折叠"


def test_banner_same_channel_low_and_expiry_is_one_row():
    """同一通道同时命中余额告急与临期时仍只占一行，展开后两种明细都在。

    旧版「同通道与余额告急去重」会把 4 天内到期的资源包藏掉；现在改成
    折叠而不是顶掉（用户 2026-11-04）。
    """
    out = _render_banner("""{
      expiry_warn_days: 7, expiry_urgent_days: 4,
      expiring: [{provider: 'dumate', provider_name: '百度搭子', account: '',
                  item_label: '积分包（login_bonus）',
                  label: '积分包（login_bonus）', expire_ts: 1794153599,
                  days_left: 3.3, remaining: 80, unit: 'points', urgent: true}],
      low_quota: [{provider: 'dumate', provider_name: '百度搭子',
                   label: '订阅积分', unit: 'points', remaining: 999.5,
                   percent_left: 2.1}],
    }""")
    assert out.count('data-eb-group="dumate"') == 1, out
    assert '余额告急 · 订阅积分 · 剩 2.1%' in out, out
    assert '1 项临期（1 项 4 天内）' in out, out
    assert '积分包（login_bonus）' in out, "资源包明细不能被余额告急顶掉"


def test_banner_dedupes_low_quota_rows_by_channel():
    """同一个通道多条余额告急（防御旧响应/降级数据）也只渲染一行。"""
    out = _render_banner("""{
      low_quota: [
        {provider: 'trae', provider_name: 'Trae', label: '#1 · 总额度',
         unit: 'credit', remaining: 200, percent_left: 5.0},
        {provider: 'trae', provider_name: 'Trae', label: '#2 · 总额度',
         unit: 'credit', remaining: 100, percent_left: 2.0},
      ],
    }""")
    assert out.count('data-eb-group="trae"') == 1, out
    assert '1 个通道余额告急' in out, out
    assert '#2 · 总额度' in out and '2%' in out, "summary 取最紧的一条"


def test_banner_renders_account_before_resource_pack():
    """多账号通道的明细行要能看出「哪个账号里的哪个包」。"""
    out = _render_banner("""{
      expiry_warn_days: 7, expiry_urgent_days: 4,
      expiring: [{provider: 'codebuddy', provider_name: 'CodeBuddy',
                  account: '#2', item_label: '资源包',
                  label: 'CodeBuddy #2 · 资源包', expire_ts: 1793932708,
                  days_left: 1.8, remaining: 260, unit: 'credit', urgent: true}],
    }""")
    assert 'CodeBuddy' in out, out
    assert '资源包' in out, out
    assert '#2' in out, out
    assert '1.8' not in out, out
    assert '4 天内' in out, "后端下发的紧急窗口标签要出现"


def test_quota_item_renders_percent_only_window_with_bar():
    """Codex /wham/usage 只给已用百分比，不给 used/total 的绝对量。

    这种条目仍应显示「剩 75% · 已用 25%」并画 25% 进度条；不能因 total=null
    降级成「剩 75 · 已用未知」且整条进度条消失。
    """
    out = _run_fn(("quotaItemHtml",), """
console.log(quotaItemHtml({
  label: '5 小时窗口', used: null, total: null, remaining: 75,
  percent: 25, reset_ts: 1792525266, unit: 'percent',
}));
""")
    assert '剩 75%' in out, out
    assert '已用 25%' in out, out
    assert '已用未知' not in out, out
    assert 'class="qbar"' in out, out
    assert 'width:25%' in out, out


def test_quota_item_percent_only_derives_remaining_when_missing():
    """百分比窗口若没显式给 remaining，也能从已用百分比推出剩余。"""
    out = _run_fn(("quotaItemHtml",), """
console.log(quotaItemHtml({
  label: '周窗口', used: null, total: null, remaining: null,
  percent: 17, reset_ts: null, unit: 'percent',
}));
""")
    assert '剩 83%' in out, out
    assert '已用 17%' in out, out
    assert 'class="qbar"' in out, out


def test_quota_item_renders_expire_not_reset():
    """条目上的日期后缀要分「到期」与「重置」，不能一律说成重置。

    这条盯的正是用户报的问题：Qoder 的 ``expiresAt`` 被塞进 ``reset_ts``，
    界面上显示成「10-30 重置」，用户以为到期日没被记录。
    """
    out = _run_fn(("quotaItemHtml",), """
console.log(quotaItemHtml({
  label: '订阅额度', used: 100, total: 2000, remaining: 1900, percent: 5,
  reset_ts: null, expire_ts: 1793289600, unit: 'credit',
}) + '|' + quotaItemHtml({
  label: '5 小时窗口', used: 10, total: 100, remaining: 90, percent: 10,
  reset_ts: 1792692336, expire_ts: null, unit: 'count',
}));
""")
    expire_part, reset_part = out.split('|')
    assert '到期' in expire_part and '重置' not in expire_part, expire_part
    assert '重置' in reset_part and '到期' not in reset_part, reset_part


def test_render_benefits_calls_the_banner():
    """renderBenefits 必须真的调 renderExpiryBanner——helper 写得再对，
    没接上去界面就还是老样子（横幅永远不出现）。"""
    text = BENEFITS_JS.read_text(encoding="utf-8")
    body = text[text.index("function renderBenefits(opts)"):]
    body = body[:body.index("\n}\n")]
    assert "renderExpiryBanner()" in body, "renderBenefits 没调用横幅渲染"


# ---------------------------------------------------------------------------
# 余额告急横幅（2026-10-03，与到期告警同横幅、两种段拼接）
# ---------------------------------------------------------------------------

def test_banner_renders_low_quota_rows_alongside_expiry():
    """两种告警齐发：head 用「 · 」拼两段；credits 通道铺绝对值（剩余 credits
    是它们的告警依据），其余量纲铺占比 + 窗口名；日期列用 — 占位，转红。"""
    out = _render_banner("""{
      expiry_warn_days: 7,
      expiring: [{provider: 'trae', provider_name: 'trae', label: '签到奖励',
                  expire_ts: 1792692336, days_left: 2.0, remaining: 500, unit: 'credit'}],
      low_quota: [{provider: 'qoder', provider_name: 'Qoder',
                   label: null, unit: 'credit', remaining: 280, percent_left: 6.4},
                  {provider: 'zcode', provider_name: 'ZCode',
                   label: '5 小时窗口', unit: 'count', remaining: 5, percent_left: 5.0}],
    }""")
    assert '有 1 项权益将在 7 天内到期 · 2 个通道余额告急' in out, out
    assert '余额告急 · 剩 280 credits' in out, out
    assert '余额告急 · 5 小时窗口 · 剩 5%' in out, out
    assert '<span class="eb-days over">' in out, f"占比该转红: {out}"
    # 到期行照旧渲染，两种明细同列
    assert '签到奖励' in out, out


def test_banner_low_quota_only_has_no_expiry_segment():
    """只有余额告急时：head 只有低余额段，「最近一项」提示（到期专属）不出现。"""
    out = _render_banner("""{
      low_quota: [{provider: 'codebuddy', provider_name: 'CodeBuddy',
                   label: null, unit: 'credit', remaining: 120, percent_left: 2.0}],
    }""")
    assert '1 个通道余额告急' in out, out
    assert '项权益' not in out, f"不该出现到期段文案: {out}"
    assert '最近一项' not in out, f"没有到期项不该有「最近一项」: {out}"
    assert 'expirybar show' in out, "横幅要显示"
    assert 'expired' not in out, out


def test_banner_low_quota_without_percent_says_insufficient():
    """credits 通道占比拿不到（total 全缺）也不能渲染成「剩 —%」：绝对值是
    它的告警依据，铺「剩 0 credits」，右列兜底「告急」。（非 credits 通道
    percent 为 null 后端就不报，前端的「余额不足」分支只是防御。）"""
    out = _render_banner("""{
      low_quota: [{provider: 'weird', provider_name: 'weird',
                   label: null, unit: 'credit', remaining: 0, percent_left: null}],
    }""")
    assert '余额告急 · 剩 0 credits' in out, out
    assert '剩 —' not in out, f"不能渲染成「剩 —%」: {out}"
    assert '告急<' in out, f"右列该有兜底文案: {out}"


def test_quota_item_note_renders_as_visible_line_not_label_title():
    """后端的解释性附注（note）渲染成 label 下方一行小字，**不塞进 label 文本**。

    演进：早先 note 只挂 label 的 title（怕挤折行），但面板是窄卡、悬停提示基本
    看不到——antigravity 用户实际反馈「额度都是 0、看不明白」就是因为没看到这层
    解释。现在改成独立一行 `.muted` 小字：既不挤 label 标题行（日期/数字不被
    挤折行），又能真正被读到。
    """
    out = _run_fn(("quotaItemHtml",), """
console.log(quotaItemHtml({
  label: 'AG #1 · Gemini 组', note: '组内共享 weekly + 5h 双池，取组内最紧水位',
  used: 0, total: 1000, remaining: 1000, percent: 0,
  reset_ts: 1791056048, expire_ts: null, unit: 'permille',
}));
""")
    # note 作为可见文本出现（独立说明行）
    assert '组内共享 weekly + 5h 双池，取组内最紧水位' in out, out
    # 但**不**拼进 label 标题行——label span 里只该有标签本身
    assert '<span class="qlabel">AG #1 · Gemini 组' in out, out
    # 重置时间用「按同一时区格式化出来的期望值」比对，不硬编码 CST 墙钟——
    # CI 跑 UTC，硬编码 10/04 03:34 会飘（真踩过：本地过、CI 挂）。
    assert '重置' in out, out


def test_quota_item_reset_note_goes_to_title():
    """reset_note 挂 reset 的 title（解释这个时间点是什么），不进正文。

    antigravity 的 resetTime 只是下一个 5 小时窗口的滚动刷新点，不是整组/每周池
    重置——这句解释走 title，正文只留「MM-DD HH:MM 重置」。
    """
    out = _run_fn(("quotaItemHtml",), """
console.log(quotaItemHtml({
  label: 'Gemini 组', used: 0, total: 1000, remaining: 1000, percent: 0,
  reset_ts: 1791056048, reset_note: '下一个 5 小时窗口刷新点（滚动），不代表每周池重置',
  expire_ts: null, unit: 'permille',
}));
""")
    assert 'title="（下一个 5 小时窗口刷新点（滚动），不代表每周池重置）"' in out, out
    assert '重置</span>' in out, out


def test_quota_item_without_note_has_no_title():
    """没有 note 的条目（trae/zcode 等绝大多数）不渲染空 title、不渲染空说明行。"""
    out = _run_fn(("quotaItemHtml",), """
console.log(quotaItemHtml({
  label: '5 小时窗口', used: 10, total: 100, remaining: 90, percent: 10,
  reset_ts: 1792692336, expire_ts: null, unit: 'count',
}));
""")
    assert 'qlabel' in out, out
    assert 'title=' not in out, f"不该有空的 title 属性: {out}"
    # 没有 note 就只该有一层 qhead + bar，不该多出说明行
    assert out.count('<div class="qitem">') == 1


def test_headline_shows_used_percent_beside_the_total():
    """标题行在「剩 X / Y」后带「已用 N%」（用户 2026-10-03：参考明细行写法）。

    可合计通道用合计算比例，单条通道用那一条算——都能算，因为是比例不是量纲。
    """
    out = _run_js("""console.log(JSON.stringify({
      summed: quotaHeadSum({sum_items: true, items: [
        {label: 'A', remaining: 1621, total: 2000},
        {label: 'B', remaining: 100, total: 400},
        {label: 'C', remaining: 1030, total: 2000}]}),
      single: quotaHeadSum({items: [
        {label: '5 小时窗口', remaining: 652, total: 2000}]}),
      no_total: quotaHeadSum({items: [{label: 'X', remaining: 100, total: 0}]}),
    }));""")
    data = json.loads(out.strip().splitlines()[-1])
    # 合计 2751/4400 → 已用 37.48% → 四舍五入 37%
    assert '2,751' in data["summed"] and '已用 37%' in data["summed"], data["summed"]
    assert '已用 67%' in data["single"], f"652/2000 → 67%: {data['single']}"
    assert '已用' not in data["no_total"], f"total=0 不能算出比例: {data['no_total']}"
