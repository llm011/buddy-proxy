"""全局按钮 tooltip：原生 title 首次悬停迁成 data-tip 的前端契约测试。

小图标按钮（▲▼/✕/✎/📌）原来只有原生 ``title``：悬停约 1 秒才出、没有样式，
用户看不出操作。app.js 的全局 tooltip 现在应只接管 ``<button title>``，首次
mouseover 把 title 迁成 data-tip 并删掉 title（之后即时、有样式、不会双弹）；
普通 span/单元格的 title 保持原生行为。这里抽真实 IIFE 用事件桩跑，防退化。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

_STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
_APP_JS = _STATIC / "app.js"
_TOOLTIP_RE = re.compile(r"\(function initGlobalTooltip\(\) \{.*?\n\}\)\(\);", re.S)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑 app.js（CI 有，本机可选）"
)

_STUB = """
const LISTENERS = {};
const TIP = {
  id: '', textContent: '', style: {}, attrs: {}, offsetWidth: 80, offsetHeight: 20,
  setAttribute(k, v) { this.attrs[k] = String(v); },
  classList: {
    _s: new Set(),
    add(c) { this._s.add(c); },
    remove(c) { this._s.delete(c); },
    contains(c) { return this._s.has(c); },
  },
};
globalThis.__TIP = TIP;
globalThis.__APPENDED = [];
globalThis.document = {
  createElement: () => TIP,
  body: {appendChild: el => globalThis.__APPENDED.push(el)},
  addEventListener: (type, fn) => { LISTENERS[type] = fn; },
};
globalThis.window = {innerWidth: 800};
"""


def _run_js(body: str) -> str:
    text = _APP_JS.read_text(encoding="utf-8")
    match = _TOOLTIP_RE.search(text)
    assert match, "app.js 里找不到 initGlobalTooltip IIFE（边界被改了？）"
    script = (
        _STUB + match.group(0)
        + "\n(async () => {\n" + body
        + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def _last_json(out: str):
    return json.loads(out.strip().splitlines()[-1])


def test_button_title_becomes_data_tip_on_first_hover():
    """<button title> 首次悬停：迁成 data-tip、删 title、立即显示且不双弹。"""
    out = _run_js("""
function mkButton(attrs) {
  return {
    attrs, dataset: {},
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); },
    getAttribute(k) { return this.hasAttribute(k) ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    removeAttribute(k) { delete this.attrs[k]; },
    closest(sel) {
      if (this.hasAttribute('data-tip')) return this;
      if (sel.includes('button[title]') && this.hasAttribute('title')) return this;
      return null;
    },
    getBoundingClientRect() { return {left: 100, width: 40, top: 100, bottom: 120}; },
    contains(node) { return node === this; },
  };
}
const btn = mkButton({class: 'ghost', title: '置顶（移到顺位 #1）'});
LISTENERS.mouseover({target: btn});
const first = {
  attrs: Object.assign({}, btn.attrs),
  text: TIP.textContent,
  shown: TIP.classList.contains('show'),
  left: TIP.style.left, top: TIP.style.top,
};
LISTENERS.mouseover({target: btn});   // 第二次已走 data-tip，仍应正常
const second = {attrs: Object.assign({}, btn.attrs), shown: TIP.classList.contains('show')};
LISTENERS.mouseout({target: btn});
console.log(JSON.stringify({
  appended: __APPENDED.length, first, second,
  hiddenAfterOut: !TIP.classList.contains('show'),
}));
""")
    data = _last_json(out)
    assert data["appended"] == 1 and data["first"]["attrs"]["class"] == "ghost"
    assert data["first"]["attrs"]["data-tip"] == "置顶（移到顺位 #1）"
    assert data["first"]["attrs"]["aria-label"] == "置顶（移到顺位 #1）", "迁走 title 不能弄丢读屏名称"
    assert data["first"]["attrs"]["aria-describedby"] == "bp-tooltip"
    assert "title" not in data["first"]["attrs"], "title 必须删掉，否则原生/自定义会重复弹"
    assert data["first"]["text"] == "置顶（移到顺位 #1）"
    assert data["first"]["shown"], "首次悬停就该显示，不等原生 ~1s"
    assert data["second"]["attrs"] == data["first"]["attrs"], "第二次悬停不应重复迁移"
    assert data["second"]["shown"] and data["hiddenAfterOut"]


def test_non_button_title_and_existing_data_tip_are_not_clobbered():
    """只接管按钮：span 的 title 保持原生；已有 data-tip 优先，不被 title 覆盖。"""
    out = _run_js("""
function mkTarget(attrs, isButton) {
  return {
    attrs, isButton, dataset: {},
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); },
    getAttribute(k) { return this.hasAttribute(k) ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    removeAttribute(k) { delete this.attrs[k]; },
    closest(sel) {
      if (this.hasAttribute('data-tip')) return this;
      if (this.isButton && sel.includes('button[title]') && this.hasAttribute('title')) return this;
      return null;
    },
    getBoundingClientRect() { return {left: 10, width: 20, top: 30, bottom: 40}; },
    contains(node) { return node === this; },
  };
}
const cell = mkTarget({title: '补充信息，不是操作'}, false);
LISTENERS.mouseover({target: cell});
const cellAfter = Object.assign({}, cell.attrs);
const tipTextAfterCell = TIP.textContent;
const both = mkTarget({title: '按钮原生说明', 'data-tip': '已有即时说明'}, true);
LISTENERS.mouseover({target: both});
const bothAfter = Object.assign({}, both.attrs);
const child = {closest: () => mkTarget({title: '从子元素冒泡'}, true)};
LISTENERS.mouseover({target: child});
console.log(JSON.stringify({
  cellAfter, tipTextAfterCell, bothAfter,
  childTip: TIP.textContent,
}));
""")
    data = _last_json(out)
    assert data["cellAfter"] == {"title": "补充信息，不是操作"}, "非按钮 title 不该被迁移"
    assert data["tipTextAfterCell"] == "", "非按钮悬停不该触发自定义 tooltip"
    assert data["bothAfter"]["data-tip"] == "已有即时说明", "data-tip 优先"
    assert data["childTip"] == "从子元素冒泡", "按钮内子元素的 title 也应由冒泡接管"
