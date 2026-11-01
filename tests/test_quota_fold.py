"""额度列表折叠：只铺没消耗完的 + 限高裁切 + 展开后已用完的沉底。

背景（2026-10-03 用户反馈）：Trae 一次返回 28 条权益包，其中 12 条「签到奖励」
各自独立且大多已花光，界面上重复行铺满一屏，真正还有余额的几条被淹掉。用户
要求：默认只展示没消耗完的、超过最大高度就折叠、点击展开；展开时已消耗完的
排到底部；所有通道一致。

三块逻辑分开测：
1. **判定** ``quotaSpent``——哪些算「已用完」。判错方向会把「用量未知」的条目
   当成已用完收起来，正好藏掉最该看的那条。
2. **分组** ``quotaItemsHtml``——活动项在前、已用完沉底且默认 hidden。
3. **校准** ``syncQuotaFold``——按实际高度决定裁不裁、按钮露不露、记住展开态。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
BENEFITS_JS = STATIC / "benefits_quota.js"
BENEFITS_CORE_JS = STATIC / "benefits.js"
# 2026-10 前端按职责拆分（benefits.js 超 700 行）：面板段挪到 benefits_panels.js
# （traepat/antigravity/qoder）与 benefits_checkin.js（kimi）。额度折叠函数
# （quotaSpent/quotaItemsHtml/syncQuotaFold/quotaFoldToggle）在 benefits_quota.js。
PANELS_JS = [STATIC / name for name in (
    "benefits_panels.js", "benefits_antigravity.js", "benefits_qoder.js",
    "benefits_trae.js", "benefits_codebuddy.js", "benefits_kimi.js",
    "benefits_codex.js", "benefits_dumate.js",
)]
# DuMate 面板（benefits_dumate.js）单列：它有一条独一份的 status 驱动自重建，
# 额度明细也走 quotaItemsHtml，一并进接线检查。
DUMATE_JS = STATIC / "benefits_dumate.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑前端 JS（CI 有，本机可选）"
)

_FNS = ("quotaSpent", "quotaRemaining", "quotaMoreButton", "quotaItemsHtml", "syncQuotaFold",
        "quotaFoldToggle", "quotaItemHtml", "quotaProviderExhausted", "quotaProviderFold",
        "quotaProviderToggle")


def _extract_fn(text: str, name: str) -> str:
    match = re.search(rf"^function {name}\(.*?\n\}}\n", text, re.S | re.M)
    assert match, f"benefits.js 里找不到 {name}（被改名/挪走了？）"
    return match.group(0)


def _extract_const(text: str, name: str) -> str:
    match = re.search(rf"^const {name} = .*?;\n", text, re.S | re.M)
    assert match, f"benefits.js 里找不到 const {name}"
    return match.group(0)


#: syncQuotaFold 要的 DOM 面很窄（遍历 .qbody、量高度、开关按钮），这里手搓
#: 等价结构，比引 jsdom 轻得多。**测的是判定逻辑**：给一个自然高度，看它裁不
#: 裁、按钮露不露、文案对不对——而不是测浏览器怎么算 scrollHeight。
_DOM_STUB = """
globalThis.__boxes = [];
globalThis.__boxesOf = key => globalThis.__boxes.filter(b => b.dataset.qfold === key);

// mkBox(key, scrollHeight, spentCount, actCount) → {box, btn}；btn 是 box 的
// 下一个兄弟（真实 DOM 里 quotaItemsHtml 就是这么排的，见 test_fold_button_
// follows_body_in_markup）。actCount 默认 3——留出「还有没用完的」这个常见
// 情形，全用完的场景显式传 0。
globalThis.mkBox = (key, scrollHeight, spentCount, actCount) => {
  const act = actCount === undefined ? 3 : actCount;
  const cls = new Set();
  const spent = spentCount ? {hidden: false, count: spentCount} : null;
  const box = {
    dataset: {qfold: key},
    scrollHeight,
    classList: {
      toggle(c, force) { force ? cls.add(c) : cls.delete(c); },
      contains: c => cls.has(c),
    },
    _cls: cls,
    querySelector: sel => sel === '.qspent' ? spent : null,
    parentElement: {},
    _spent: spent,
  };
  const btn = {
    dataset: {qfold: key, act: String(act), spent: String(spentCount || 0),
              total: String(act + (spentCount || 0))},
    hidden: null, textContent: '',
    classList: {contains: c => c === 'qmore'},
  };
  box.nextElementSibling = btn;
  globalThis.__boxes.push(box);
  return {box, btn};
};

globalThis.document = {
  querySelectorAll: sel => sel === '.qbody[data-qfold]' ? globalThis.__boxes : [],
  getElementById: id => globalThis.__providerBodies && globalThis.__providerBodies[id] || null,
};
globalThis.getComputedStyle = () => ({getPropertyValue: () => '208px'});
"""

_STUB = """
globalThis.esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
globalThis.fmtNum = n => String(n);
"""


def _run(body: str) -> str:
    text = BENEFITS_JS.read_text(encoding="utf-8")
    src = "".join(_extract_fn(text, n) for n in _FNS)
    src += _extract_const(text, "QUOTA_FOLD")
    src += _extract_const(text, "PROVIDER_QUOTA_FOLD")
    proc = subprocess.run(
        ["node", "-e", _STUB + _DOM_STUB + src + "\n" + body],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def _last(out: str):
    return json.loads(out.strip().splitlines()[-1])


def _item(label: str, remaining=None, total=None, used=None, **extra) -> dict:
    """造一条额度条目（Python 字面量，靠 json.dumps 变成 JS——JSON 是 JS 的
    子集，比手搓 JS 文本少一层引号/大括号的坑）。"""
    it = {"label": label, "remaining": remaining, "total": total, "used": used,
          "percent": 0, "reset_ts": None, "expire_ts": None, "unit": "credit"}
    it.update(extra)
    return it


def _js(items) -> str:
    return json.dumps(items, ensure_ascii=False)


# ---------------------------------------------------------------------------
# quotaSpent：哪些算「已用完」
# ---------------------------------------------------------------------------

def test_spent_recognises_zero_and_negative():
    """余量 0 或负数算用完（负数是上游超额记账时会出现的值）。"""
    out = _run("""
console.log(JSON.stringify([
  quotaSpent({remaining: 0}),
  quotaSpent({remaining: -5}),
  quotaSpent({remaining: 1}),
]));
""")
    assert _last(out) == [True, True, False], out


def test_spent_accepts_numeric_string_zero():
    """``"0"`` 这类数字字符串也要认（上游把数额发成字符串是常事）。"""
    out = _run("""
console.log(JSON.stringify([quotaSpent({remaining: '0'}), quotaSpent({remaining: '0.0'})]));
""")
    assert _last(out) == [True, True], out


def test_spent_infers_from_used_and_total():
    """没有 remaining 但给了 used/total 时按同样的回填规则算——与
    ``quotaItemHtml`` 里那条 ``total - used`` 保持一致，否则同一张卡上标题
    说「用完」而条目区说「未知」。"""
    out = _run("""
console.log(JSON.stringify([
  quotaSpent({remaining: null, used: 200, total: 200}),
  quotaSpent({remaining: null, used: 50, total: 200}),
]));
""")
    assert _last(out) == [True, False], out


def test_unknown_remaining_is_not_spent():
    """**关键**：拿不到余量的一律不算用完——那是「未知」，不是「没花」。

    判反的代价很直接：``trae.pat.quota._failure_notice`` 的说明条
    （remaining 是「2/9 个账号查询失败」这类文案）和 ``reset_pending`` 的
    「用量待确认」都是这种形态，被当成已用完收进折叠区，正好把最该看见的
    那条藏起来。
    """
    out = _run("""
console.log(JSON.stringify([
  quotaSpent({remaining: null}),
  quotaSpent({remaining: null, used: null, total: null}),
  quotaSpent({remaining: null, used: 5, total: null}),
  quotaSpent({remaining: '2/9 个账号查询失败'}),
  quotaSpent({remaining: ''}),
  quotaSpent({remaining: true}),
]));
""")
    assert _last(out) == [False] * 6, out


# ---------------------------------------------------------------------------
# quotaItemsHtml：活动项在前，已用完沉底且默认隐藏
# ---------------------------------------------------------------------------

def test_spent_items_hidden_by_default():
    """默认只铺没消耗完的：已用完的区块带 ``hidden``，活动项照常渲染。"""
    items = [_item("签到奖励", 0.0, 200, 200),
             _item("会员 Pro 连续包月", 0.0, 4000, 4000),
             _item("每月登录赠送", 500.0, 500)]
    out = _run("""
const html = quotaItemsHtml(%s, 'trae');
console.log(JSON.stringify({
  hidden: /class="qspent" hidden/.test(html),
  hasActive: html.includes('每月登录赠送'),
  spentBlock: html.includes('已用完 2 项'),
  activeBeforeSpent: html.indexOf('每月登录赠送') < html.indexOf('已用完 2 项'),
}));
""" % _js(items))
    assert _last(out) == {"hidden": True, "hasActive": True,
                          "spentBlock": True, "activeBeforeSpent": True}, out


def test_expanded_shows_spent_at_bottom():
    """展开后已用完的可见，且排在活动项**之后**——它们是历史，不是当前状态。"""
    items = [_item("签到奖励", 0.0, 200, 200), _item("每月登录赠送", 500.0, 500)]
    out = _run("""
QUOTA_FOLD.open['trae'] = true;
const html = quotaItemsHtml(%s, 'trae');
console.log(JSON.stringify({
  hidden: /class="qspent" hidden/.test(html),
  order: html.indexOf('每月登录赠送') < html.indexOf('签到奖励'),
}));
""" % _js(items))
    assert _last(out) == {"hidden": False, "order": True}, out


def test_all_spent_group_gets_placeholder():
    """一条都不剩时给占位文案——否则收起状态下整组看着是空的，像加载失败。"""
    out = _run("""
const html = quotaItemsHtml(%s, 'trae');
console.log(JSON.stringify({empty: html.includes('权益已全部用完')}));
""" % _js([_item("签到奖励", 0.0, 200, 200)]))
    assert _last(out) == {"empty": True}, out


def test_head_only_item_not_listed_but_kept_for_headline():
    """head_only（Trae「总额度」）只供标题行数字，明细列表不单列。

    总额度是各权益包的合计，再有自己一条带进度条的明细就是重复
    （用户 2026-10-04 反馈）。明细里不该出现它，但标题行仍要用它的数字——
    这里只锁「明细不渲染 + 不参与折叠计数」，标题行口径见 test_quota_headline_ui。
    """
    items = [dict(_item("总额度", 4000.0, 9500), head_only=True),
             _item("会员 Pro 连续包月", 4000.0, 4000.0),
             _item("签到奖励", 0.0, 200.0, 200.0)]
    out = _run("""
const html = quotaItemsHtml(%s, 'trae');
console.log(JSON.stringify({
  hasTotal: html.includes('总额度'),
  hasPack: html.includes('会员 Pro 连续包月'),
  spentCount: (html.match(/已用完 (\\d+) 项/) || [])[1],
  total: (html.match(/data-total="(\\d+)"/) || [])[1],
}));
""" % _js(items))
    assert _last(out) == {"hasTotal": False, "hasPack": True,
                          "spentCount": "1", "total": "2"}, out


def test_only_head_only_items_renders_nothing():
    """整组只有 head_only 条（不该发生，但空 render 好过渲染个空壳）。"""
    out = _run("""
console.log(JSON.stringify({html: quotaItemsHtml(%s, 'x')}));
""" % _js([dict(_item("总额度", 4000.0, 9500), head_only=True)]))
    assert _last(out) == {"html": ""}, out


def test_unknown_items_stay_visible_not_folded():
    """端到端守住「未知不折叠」：说明条留在可见区、不进已用完区块。"""
    items = [{"label": "PAT 额度网关不可达", "remaining": "10/10 个账号查询失败",
              "total": None, "used": None, "percent": None, "reset_ts": None,
              "expire_ts": None, "unit": None}]
    out = _run("""
const html = quotaItemsHtml(%s, 'traepat');
console.log(JSON.stringify({
  visible: html.includes('查询失败'),
  noSpentBlock: !html.includes('qspent'),
}));
""" % _js(items))
    assert _last(out) == {"visible": True, "noSpentBlock": True}, out


def test_fold_state_is_keyed_per_group():
    """展开态按组各记一份：展开 trae 不该把 qoder 也撑开。"""
    out = _run("""
QUOTA_FOLD.open['trae'] = true;
const a = quotaItemsHtml(%s, 'trae');
const b = quotaItemsHtml(%s, 'qoder');
console.log(JSON.stringify({
  traeOpen: !/class="qspent" hidden/.test(a),
  qoderClosed: /class="qspent" hidden/.test(b),
}));
""" % (_js([_item("A", 0.0, 200, 200)]), _js([_item("B", 0.0, 200, 200)])))
    assert _last(out) == {"traeOpen": True, "qoderClosed": True}, out


def test_fold_button_follows_body_in_markup():
    """按钮必须是 ``.qbody`` 的**紧邻下一个兄弟**。

    ``syncQuotaFold`` 靠 ``box.nextElementSibling`` 找按钮，中间插任何元素
    （比如以后有人加个说明行）都会让它静默失效——按钮永远不显示、也不报错。
    """
    out = _run("""
const html = quotaItemsHtml(%s, 'trae');
console.log(JSON.stringify({
  adjacent: /<\\/div><button class="qmore"/.test(html),
}));
""" % _js([_item("签到奖励", 0.0, 200, 200)]))
    assert _last(out) == {"adjacent": True}, out


def test_fold_button_is_the_only_button():
    """整组只有**一枚**按钮：主钮。不做第二枚浮钮。

    2026-10-31 曾加过一枚视口浮钮（绝对定位，想接住被 ``overflow-y:auto``
    推出滚动视口的主钮），它和主钮同文案、同状态。连出三次同一类事故——收起
    态两枚「展开」(2026-11-01 首图) → 浮钮包含块退化铺成整页通栏(第二图) →
    展开态两枚「收起」(2026-11-01 第三图)。为了根除，浮钮整个去掉：独一的
    按钮不可能再出现「两枚」。这里盯住数量恒为 1、属性拼得完整、带 data-qfold。
    """
    out = _run("""
const html = quotaItemsHtml(%s, 'mimo');
const btns = html.match(/<button[^>]*>/g) || [];
console.log(JSON.stringify({
  n: btns.length,
  allHaveKey: btns.every(b => /data-qfold="mimo"/.test(b)),
  clean: btns.every(b => !/undefined|=""/.test(b)),
  handlers: btns.every(b => b.includes('onclick="quotaFoldToggle(this)"')),
}));
""" % _js([_item("周窗口", 80.0, 100), _item("5 小时窗口", 0.0, 100)]))
    assert _last(out) == {"n": 1, "allHaveKey": True,
                          "clean": True, "handlers": True}, out


def test_quota_fold_has_no_float_artifacts():
    """折叠逻辑里不能再留 qfold-float 的蛛丝马迹（按钮/查找/校准全清）。

    去掉浮钮时最容易漏掉的是 syncQuotaFold 里那段「按兄弟扫浮钮」——留着
    它不报错、也找不到浮钮，白算一遍还占着一条「浮钮存在过」的暗线。这里
    直接锁死源码里不再出现 qfold-float / mkFloat 依赖。
    """
    text = BENEFITS_JS.read_text(encoding="utf-8")
    assert "qfold-float" not in text, "benefits_quota.js 还留着 qfold-float"
    style = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "qfold-float" not in style, "style.css 还留着 .qfold-float 规则"


# ---------------------------------------------------------------------------
# syncQuotaFold：按实际高度校准
# ---------------------------------------------------------------------------

def test_sync_hides_button_when_nothing_to_show():
    """内容没超高、底下也没有已用完的 → 按钮藏起来。

    留着的话是个点了没有任何变化的假按钮——用户点了会以为界面坏了。
    """
    out = _run("""
mkBox('trae', 120, 0);
syncQuotaFold();
const box = __boxesOf('trae')[0];
console.log(JSON.stringify({hidden: box.nextElementSibling.hidden,
                            clipped: box._cls.has('clipped')}));
""")
    assert _last(out) == {"hidden": True, "clipped": False}, out


def test_sync_clips_and_labels_when_over_limit():
    """超高就裁，按钮文案要写清下面还压着多少条。"""
    out = _run("""
mkBox('trae', 900, 12);
syncQuotaFold();
const box = __boxesOf('trae')[0];
console.log(JSON.stringify({
  clipped: box._cls.has('clipped'), hidden: box.nextElementSibling.hidden,
  text: box.nextElementSibling.textContent, spentHidden: box._spent.hidden,
}));
""")
    assert _last(out) == {"clipped": True, "hidden": False,
                          "text": "展开全部 15 项 ▾", "spentHidden": True}, out


def test_sync_offers_expand_when_only_spent_items_remain():
    """没超高、但底下压着已用完的 → 仍要给按钮，文案点明是「已用完」。

    全用完的通道（trae 那种 12 条签到包全花光）就靠这个入口，否则用户看不到
    它们，会以为额度凭空消失了。
    """
    out = _run("""
mkBox('trae', 100, 12, 0);   // act=0：一条都没剩，全压在已用完里
syncQuotaFold();
const box = __boxesOf('trae')[0];
console.log(JSON.stringify({clipped: box._cls.has('clipped'),
                            hidden: box.nextElementSibling.hidden,
                            text: box.nextElementSibling.textContent}));
""")
    assert _last(out) == {"clipped": False, "hidden": False,
                          "text": "展开 12 项已用完 ▾"}, out


def test_toggle_reveals_spent_and_offers_collapse():
    """点一下：已用完的显示出来、不再裁切，按钮变「收起」；再点一下收回。"""
    out = _run("""
const {box, btn} = mkBox('trae', 900, 12);
syncQuotaFold();
quotaFoldToggle(btn);
const opened = {spentShown: !box._spent.hidden, clipped: box._cls.has('clipped'),
                text: btn.textContent, hidden: btn.hidden};
quotaFoldToggle(btn);
const closed = {spentShown: !box._spent.hidden, clipped: box._cls.has('clipped'),
                text: btn.textContent};
console.log(JSON.stringify({opened, closed}));
""")
    assert _last(out) == {
        "opened": {"spentShown": True, "clipped": False,
                   "text": "收起 ▴", "hidden": False},
        "closed": {"spentShown": False, "clipped": True, "text": "展开全部 15 项 ▾"},
    }, out


def test_open_state_survives_rerender():
    """整页每 30s 重建一次 innerHTML，展开态得活下来。

    状态若挂在 DOM 元素上（比如读 box.classList），下一次轮询重建后就丢了
    ——用户刚点开，30 秒后自己收回去。所以存 QUOTA_FOLD。
    """
    out = _run("""
const first = mkBox('trae', 900, 12);
syncQuotaFold();
quotaFoldToggle(first.btn);              // 用户点开
globalThis.__boxes = [];                 // 模拟轮询整页重建：旧元素全丢
const second = mkBox('trae', 900, 12);   // 新元素，滚动高度同前
syncQuotaFold();
console.log(JSON.stringify({
  stillOpen: !second.box._spent.hidden,
  notClipped: !second.box._cls.has('clipped'),
  text: second.btn.textContent,
}));
""")
    assert _last(out) == {"stillOpen": True, "notClipped": True,
                          "text": "收起 ▴"}, out


def test_sync_tolerates_borderline_height():
    """卡在边界上（略高于上限）不裁。

    ``scrollHeight`` 受 ``overflow:hidden`` 影响会多算首尾两条的外边距
    （headless Chrome 实测差 18px），所以判定留 1px 容差：正好等于上限、
    或刚超一两像素时不该裁——那种高度裁掉也只是把最后一条切一半。
    """
    out = _run("""
console.log(JSON.stringify([
  (mkBox('a', 208, 0), syncQuotaFold(), __boxesOf('a')[0]._cls.has('clipped')),
  (mkBox('b', 209, 0), syncQuotaFold(), __boxesOf('b')[0]._cls.has('clipped')),
  (mkBox('c', 260, 0), syncQuotaFold(), __boxesOf('c')[0]._cls.has('clipped')),
]));
""")
    assert _last(out) == [False, False, True], out


def test_sync_reads_limit_from_css_variable():
    """上限只写在 CSS 变量里、由 JS 读它。两边各写一个 208，改了 CSS 忘了 JS
    就会出现「已经裁了但按钮不露」的死角。这里换掉变量值验证真的读了。"""
    out = _run("""
globalThis.getComputedStyle = () => ({getPropertyValue: () => '500px'});
mkBox('trae', 400, 0);   // 400 < 500 → 不该裁
syncQuotaFold();
const box = __boxesOf('trae')[0];
console.log(JSON.stringify({clipped: box._cls.has('clipped'),
                            hidden: box.nextElementSibling.hidden}));
""")
    assert _last(out) == {"clipped": False, "hidden": True}, out


# ---------------------------------------------------------------------------
# 接线：三处渲染都得用上，不能只有 trae 变好看
# ---------------------------------------------------------------------------

def test_all_three_panels_use_the_fold_helper():
    """主列表及各专属额度面板（含 Trae 海外版）都要走 quotaItemsHtml。

    只改主列表的话，PAT 和 antigravity 还是老样子——用户看到的仍是「其它通道
    没变」，等于没修。kimi/qoder/trae 面板（复用 antigravity 的分组结构）也要跟上。"""
    text = BENEFITS_CORE_JS.read_text(encoding="utf-8")
    assert "quotaItemsHtml(q.items || [], p.id)" in text, "主额度列表没接上折叠"
    # 面板段分布在拆出去的文件（pat/ag/kimi/qoder/trae/codebuddy）——trae 面板
    # 2026-10-06 加回（额度挪到底部专属面板，用户反馈「额度面板看不到」）；
    # codebuddy 面板同期随多账号改造加入。
    panels = "".join(p.read_text(encoding="utf-8") for p in PANELS_JS)
    calls = re.findall(r"quotaItemsHtml\(its, '(\w+):' \+ grp\)", panels)
    assert sorted(calls) == ["ag", "codebuddy", "codebuddyintl", "codex", "kimi", "pat", "qoder", "qoderintl", "trae", "traeintl"], f"各面板没接上折叠: {calls}"


def test_render_benefits_syncs_after_rebuild():
    """重建完要调一次校准——不调的话 clipped / 按钮状态永远停在初始值。"""
    text = BENEFITS_CORE_JS.read_text(encoding="utf-8")
    body = text[text.index("function renderBenefits(opts)"):]
    body = body[:body.index("\n}\n")]
    assert "syncQuotaFold()" in body, "renderBenefits 没调 syncQuotaFold"


def test_dumate_self_rerender_recalibrates_fold():
    """DuMate 面板重渲自己后必须补一次 syncQuotaFold。

    它有一条别处没有的 status 驱动自重建：``loadDumateStatus`` 拉到本机就绪态
    **变化**后会在 await 回调里再调一次 ``renderDumatePanel()`` 换整块 DOM
    （圆点/版本/已登录）。renderBenefits 末尾那次 syncQuotaFold 早已跑完、看不到
    这次重渲后的高度，而这条路径又不经 ``acctLoad``（那里尾部有校准）——于是
    重渲出来的 .qbody 停在模板原样：未裁切、按钮 hidden。Dumate 明细 31 条
    （29 条有余额）全铺开且没有「收起」按钮（用户 2026-11-01 报障）。

    断言 **重渲调用之后** 紧跟着一次校准；只查「文件里有 syncQuotaFold」不够
    ——acctLoad 那条路径已经贡献了调用，漏的正是这条。
    """
    src = DUMATE_JS.read_text(encoding="utf-8")
    body = src[src.index("async function loadDumateStatus()"):]
    call = body.index("renderDumatePanel();")
    after = body[call:call + 800]
    assert "syncQuotaFold()" in after, \
        "DuMate status 驱动重渲后没补 syncQuotaFold——重渲出的 .qbody 不会裁切、按钮不露"


# ---------------------------------------------------------------------------
# unit=currency：预付费余额（DeepSeek 官方）
# ---------------------------------------------------------------------------

def test_currency_balance_renders_as_amount_not_water_level():
    """预付费余额要显示成「多少钱可用」，不能套「剩 X · 已用未知」。

    DeepSeek 官方是按 token 从**钱包**扣费的，/user/balance 只回「还能花多少
    钱」——没有总量分母。掉进通用分支会渲染成「剩 34.92 · 已用未知」，读起来
    像少了一个分母；用户会以为额度统计坏了。单独一条分支明确写「可用」。
    """
    out = _run("""
console.log(JSON.stringify(quotaItemHtml(
  {label: '账户余额 (CNY)', remaining: 34.92, unit: 'currency'})));
""")
    html = _last(out)
    assert "34.92" in html, html
    assert "可用" in html, f"余额没渲染成「可用」: {html}"
    assert "已用未知" not in html, f"余额不该套用量未知文案: {html}"
    assert "qbar" not in html, "没有分母就不该画水位进度条"


def test_currency_without_amount_falls_back_to_notice_text():
    """上游没给数字时走说明条（字符串 remaining），不能当 0 渲染。"""
    out = _run("""
console.log(JSON.stringify(quotaItemHtml(
  {label: '账户余额 (USD)', remaining: '余额未知（USD）', unit: 'currency'})));
""")
    html = _last(out)
    assert "余额未知（USD）" in html, html
    assert "可用" not in html, f"说明条不该被当成数字渲染: {html}"


def test_currency_balance_is_not_treated_as_spent():
    """余额是活动条目，不能被折叠进「已用完」——0.00 余额才是用完。"""
    out = _run("""
console.log(JSON.stringify([
  quotaSpent({remaining: 34.92, unit: 'currency'}),
  quotaSpent({remaining: 0, unit: 'currency'}),
]));
""")
    assert _last(out) == [False, True]


# ---------------------------------------------------------------------------
# provider 级默认折叠：未知不等于 0；两层折叠状态不能串
# ---------------------------------------------------------------------------

def test_provider_exhaustion_requires_every_item_to_be_measured_zero():
    out = _run("""
console.log(JSON.stringify([
  quotaProviderExhausted([]),
  quotaProviderExhausted([{remaining: 0}, {remaining: '0'}]),
  quotaProviderExhausted([{remaining: 0}, {used: 100, total: 100}]),
  quotaProviderExhausted([{remaining: 0}, {remaining: 1}]),
  quotaProviderExhausted([{remaining: 0}, {remaining: null}]),
  quotaProviderExhausted([{unit: 'percent', percent: 100}]),
  quotaProviderExhausted([{unit: 'percent', percent: 99}]),
  quotaProviderExhausted([{head_only: true, remaining: 0}]),
]));
""")
    assert _last(out) == [False, True, True, False, False, True, False, True], out


def test_provider_exhaustion_rejects_notices_free_and_reset_credit():
    out = _run("""
console.log(JSON.stringify([
  quotaProviderExhausted([{remaining: '余额未知'}]),
  quotaProviderExhausted([{remaining: 0, query_failed: true}]),
  quotaProviderExhausted([{remaining: 0, unreachable: true}]),
  quotaProviderExhausted([{remaining: 0, query_empty: true}]),
  quotaProviderExhausted([{remaining: 0, reset_pending: true}]),
  quotaProviderExhausted([{remaining: 0, unit: 'reset_credit'}]),
  quotaProviderExhausted([{remaining: 0, used: null, total: null}]),
]));
""")
    assert _last(out) == [False] * 7, out


def test_provider_fold_defaults_manual_override_and_recovery():
    out = _run("""
const body = {hidden: false};
const btn = {dataset: {providerFold: 'trae'},
  getAttribute: n => n === 'aria-expanded' ? 'false' : 'provider-quota-trae',
  setAttribute() {}, title: '', textContent: ''};
const first = quotaProviderFold([{remaining: 0}], 'trae');
const initiallyHidden = first.bodyStart.includes(' hidden');
const initiallyBadge = first.badge.includes('无剩余额度');
quotaProviderToggle(btn);
const manuallyOpen = quotaProviderFold([{remaining: 0}], 'trae').bodyStart;
const replenished = quotaProviderFold([{remaining: 10}], 'trae');
const restored = quotaProviderFold([{remaining: 0}], 'trae').bodyStart;
console.log(JSON.stringify({initiallyHidden, initiallyBadge, manuallyOpen,
  replenished, restored, toggleGone: replenished.toggle === ''}));
""")
    r = _last(out)
    assert r["initiallyHidden"] is True and r["initiallyBadge"] is True
    assert " hidden" not in r["manuallyOpen"] and " hidden" not in r["restored"]
    assert " hidden" not in r["replenished"]["bodyStart"] and r["toggleGone"] is True


def test_provider_fold_markup_uses_stable_escaped_id_not_item_fold():
    out = _run("""
const f = quotaProviderFold([{remaining: 0}], 'traeintl');
console.log(JSON.stringify({html: f.toggle + f.bodyStart + f.bodyEnd,
  itemStateIndependent: Object.keys(QUOTA_FOLD.open).length === 0}));
""")
    r = _last(out)
    assert 'aria-controls="provider-quota-traeintl"' in r["html"]
    assert 'id="provider-quota-traeintl"' in r["html"]
    assert "data-qfold" not in r["html"] and "qbody" not in r["html"]
    assert r["itemStateIndependent"] is True


def test_every_provider_panel_uses_outer_fold_helper():
    core = BENEFITS_CORE_JS.read_text(encoding="utf-8")
    assert "quotaProviderFold(q.items || [], p.id)" in core
    panels = "".join(p.read_text(encoding="utf-8") for p in PANELS_JS)
    expected = {"traepat", "antigravity", "qoder", "qoderintl", "trae", "traeintl",
                "codebuddy", "codebuddyintl", "kimi", "codex", "dumate"}
    calls = set(re.findall(r"quotaProviderFold\([^;]+?, '(\w+)'\)", panels))
    assert calls == expected, f"专属通道外层折叠接线不全: {calls}"


def test_log_refresh_lives_inside_log_page_not_topbar():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    topbar = html[html.index('<div class="topbar">'):html.index('</div>\n\n<!-- 设置文件')]
    log = html[html.index('<section class="page" id="page-log">'):html.index('<section class="page" id="page-models">')]
    assert "global-refresh" not in topbar
    assert log.count('id="global-refresh"') == 1
    assert 'onclick="loadAll(true)"' in log
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "右上角" not in app
    assert "请求日志" in app and "↻ 刷新" in app
