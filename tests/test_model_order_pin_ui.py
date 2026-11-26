"""模型顺序页置顶的真实前端行为：排序、按钮状态与 POST 接线。"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


_STATIC = Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
_APP = (_STATIC / "app_order.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑前端 JS（CI 有，本机可选）"
)


def _run_js(body: str) -> dict:
    """加载完整 app_order.js，用最小 DOM 桩执行，避免只做源码字符串断言。"""
    script = r"""
const elements = {
  'order-list': {innerHTML: '', querySelectorAll: () => []},
  'order-summary': {textContent: ''},
};
const document = {getElementById: id => elements[id] || null};
let ORDER = {
  items: ['alpha', 'beta', 'gamma'].map(model => ({model, targets: [], marks: []})),
  pinned: ['gamma', 'beta'],
};
let MODELS = {groups: []};
let ORDER_PROVIDER_LIST = ['fakeprov'];
let ORDER_OPTIONS = {};
const requests = [];
const toasts = [];
function esc(value) {
  return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function jsq(value) { return esc(JSON.stringify(String(value ?? ''))); }
async function api(path, options) {
  const payload = JSON.parse(options.body);
  requests.push({path, options, payload});
  const rest = ORDER.pinned.filter(name => name !== payload.model);
  return {pinned_list: payload.pinned ? [payload.model, ...rest] : rest};
}
function toast(message, error = false) { toasts.push({message, error}); }
""" + _APP + "\n(async () => {\n" + body + r"""
})().catch(error => { console.error(error && error.stack || error); process.exit(1); });
"""
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr[:3000]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_page_rows_put_recent_pins_first_and_keep_unpinned_stable():
    out = _run_js(r"""
const before = orderRows().map(row => row.model);
const sorted = pageRows().map(row => row.m);
ORDER_DRAFT.delta = [];
const withDraft = pageRows().map(row => row.m);
renderOrderPage();
const html = elements['order-list'].innerHTML;
console.log(JSON.stringify({
  before, sorted, withDraft, html,
  renderedKeys: Array.from(html.matchAll(/data-key="([^"]+)"/g), match => match[1]),
  summary: elements['order-summary'].textContent,
  draftHead: html.split('data-key="delta"')[1].split('<div class="order-body">')[0],
}));
""")
    assert out["before"] == ["alpha", "beta", "gamma"], "展示排序不得改写接口原数组"
    assert out["sorted"] == ["gamma", "beta", "alpha"]
    assert out["withDraft"] == ["gamma", "beta", "alpha", "delta"], (
        "未保存草稿不能因置顶排序消失或跑到已置顶项之前")
    assert out["renderedKeys"] == ["gamma", "beta", "alpha", "delta"]
    assert out["summary"] == "4 个模型已配顺序"
    assert "order-pin" not in out["draftHead"], "未保存草稿不能提供必定 404 的置顶动作"

    html = out["html"]
    gamma_card = re.search(r'<div class="card order-card order-pinned" data-key="gamma">', html)
    assert gamma_card, "已置顶卡片必须有可辨识样式"
    assert re.search(
        r'class="ghost order-pin active"[^>]*'
        r'onclick="event\.stopPropagation\(\);orderTogglePin\(&quot;gamma&quot;\)"'
        r'>📌 已置顶</button>', html), "已置顶按钮要显示状态并使用安全编码后的模型键"
    assert re.search(
        r'class="ghost order-pin"[^>]*>📌 置顶</button>', html), "未置顶卡片要有置顶按钮"


def test_pin_button_posts_toggles_updates_state_and_rerenders():
    out = _run_js(r"""
await orderTogglePin('alpha');
const afterPin = {
  pinned: ORDER.pinned.slice(),
  keys: pageRows().map(row => row.m),
  active: elements['order-list'].innerHTML.includes(
    'class="ghost order-pin active"'),
};
await orderTogglePin('alpha');
const afterUnpin = {
  pinned: ORDER.pinned.slice(),
  keys: pageRows().map(row => row.m),
};
console.log(JSON.stringify({requests, toasts, afterPin, afterUnpin}));
""")
    assert [request["path"] for request in out["requests"]] == [
        "/ui/api/model-order/pin", "/ui/api/model-order/pin"]
    assert [request["payload"] for request in out["requests"]] == [
        {"model": "alpha", "pinned": True},
        {"model": "alpha", "pinned": False},
    ]
    assert all(request["options"]["method"] == "POST" for request in out["requests"])
    assert out["afterPin"] == {
        "pinned": ["alpha", "gamma", "beta"],
        "keys": ["alpha", "gamma", "beta"],
        "active": True,
    }
    assert out["afterUnpin"] == {
        "pinned": ["gamma", "beta"],
        "keys": ["gamma", "beta", "alpha"],
    }
    assert [item["message"] for item in out["toasts"]] == [
        "alpha 已置顶", "alpha 已取消置顶"]
    assert all(not item["error"] for item in out["toasts"])
