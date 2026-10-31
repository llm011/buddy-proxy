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
BENEFITS_JS = STATIC / "benefits.js"
# 2026-10 前端按职责拆分（benefits.js 超 700 行）：面板段挪到 benefits_panels.js
# （traepat/antigravity/qoder）与 benefits_checkin.js（kimi）。额度折叠函数
# （quotaSpent/quotaItemsHtml/syncQuotaFold/quotaFoldToggle）仍在 benefits.js。
PANELS_JS = [STATIC / "benefits_panels.js", STATIC / "benefits_checkin.js"]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑前端 JS（CI 有，本机可选）"
)

_FNS = ("quotaSpent", "quotaItemsHtml", "syncQuotaFold", "quotaFoldToggle", "quotaItemHtml")


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
    text = BENEFITS_JS.read_text(encoding="utf-8")
    assert "quotaItemsHtml(q.items || [], p.id)" in text, "主额度列表没接上折叠"
    # 面板段分布在拆出去的文件（pat/ag/kimi/qoder/trae/codebuddy）——trae 面板
    # 2026-10-06 加回（额度挪到底部专属面板，用户反馈「额度面板看不到」）；
    # codebuddy 面板同期随多账号改造加入。
    panels = "".join(p.read_text(encoding="utf-8") for p in PANELS_JS)
    calls = re.findall(r"quotaItemsHtml\(its, '(\w+):' \+ grp\)", panels)
    assert sorted(calls) == ["ag", "codebuddy", "codebuddyintl", "codex", "kimi", "pat", "qoder", "qoderintl", "trae", "traeintl"], f"各面板没接上折叠: {calls}"


def test_render_benefits_syncs_after_rebuild():
    """重建完要调一次校准——不调的话 clipped / 按钮状态永远停在初始值。"""
    text = BENEFITS_JS.read_text(encoding="utf-8")
    body = text[text.index("function renderBenefits(opts)"):]
    body = body[:body.index("\n}\n")]
    assert "syncQuotaFold()" in body, "renderBenefits 没调 syncQuotaFold"
