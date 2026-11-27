"""RunningHub 面板轮询不重建 DOM 的回归测试。"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

_STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑面板 JS（CI 有，本机可选）"
)


def _run_panel(script: str) -> dict:
    source = (_STATIC / "benefits_runninghub.js").read_text(encoding="utf-8")
    stub = r"""
class Element {
  constructor(id) {
    this.id = id;
    this.dataset = {};
    this.isConnected = true;
    this._innerHTML = '';
    this.writes = 0;
    this.textContent = '';
    this.className = '';
  }
  set innerHTML(value) { this._innerHTML = value; this.writes++; }
  get innerHTML() { return this._innerHTML; }
  querySelector(selector) {
    return selector.startsWith('#') ? PANEL_CHILDREN[selector.slice(1)] || null : null;
  }
  querySelectorAll() { return []; }
  addEventListener() {}
}
const PANEL = new Element('runninghub-page-box');
const PANEL_CHILDREN = {};
for (const id of [
  'rh-queue-body', 'rh-queue-count', 'rh-workflows-body', 'rh-accounts-body',
  'rh-gift-loop-tag', 'rh-wf-import-btn', 'rh-queue-refresh', 'rh-queue-archived', 'rh-queue-clear',
  'rh-claim-all-btn', 'rh-import-btn', 'rh-refresh-all-btn',
]) PANEL_CHILDREN[id] = new Element(id);
globalThis.window = globalThis;
globalThis.document = {
  getElementById(id) { return id === PANEL.id ? PANEL : null; },
  createElement() { return new Element('style'); },
  head: {appendChild() {}},
};
globalThis.ACCOUNTS = [];
globalThis.WORKFLOWS = [];
globalThis.fetch = async path => ({
  ok: true,
  async json() {
    if (path.includes('/accounts')) return {accounts: ACCOUNTS};
    if (path.includes('/workflows')) return {workflows: WORKFLOWS};
    if (path.includes('/daily-gift')) return {gift: {running: false, day: '', accounts: {}}};
    if (path.includes('/tasks')) return {tasks: []};
    throw new Error('unexpected path: ' + path);
  },
});
"""
    proc = subprocess.run(
        ["node", "-e", stub + source + "\n(async () => {\n" + script
         + "\n})().catch(e => { console.error(e); process.exit(1); });\n"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_repeat_render_keeps_shell_and_unchanged_data_dom():
    """30 秒全局轮询重复进入时，只拉数据，不能清空面板或重写相同卡片。"""
    result = _run_panel("""
await renderRunningHubPanel();
await renderRunningHubPanel();
console.log(JSON.stringify({
  panelWrites: PANEL.writes,
  accountWrites: PANEL_CHILDREN['rh-accounts-body'].writes,
  workflowWrites: PANEL_CHILDREN['rh-workflows-body'].writes,
  queueWrites: PANEL_CHILDREN['rh-queue-body'].writes,
  initialized: PANEL.dataset.rhInitialized,
}));
""")
    assert result == {
        "panelWrites": 1,
        "accountWrites": 1,
        "workflowWrites": 1,
        "queueWrites": 1,
        "initialized": "1",
    }


def test_changed_api_data_still_updates_cards():
    result = _run_panel("""
await renderRunningHubPanel();
ACCOUNTS = [{account_id: 'a1', label: '主号', account_info: {transitionCoins: 100}}];
WORKFLOWS = [{webappId: 'w1', name: '生图', nodeInfoList: []}];
await renderRunningHubPanel();
console.log(JSON.stringify({
  panelWrites: PANEL.writes,
  accountWrites: PANEL_CHILDREN['rh-accounts-body'].writes,
  workflowWrites: PANEL_CHILDREN['rh-workflows-body'].writes,
  accountUpdated: PANEL_CHILDREN['rh-accounts-body'].innerHTML.includes('主号'),
  workflowUpdated: PANEL_CHILDREN['rh-workflows-body'].innerHTML.includes('生图'),
}));
""")
    assert result["panelWrites"] == 1
    assert result["accountWrites"] == 2 and result["workflowWrites"] == 2
    assert result["accountUpdated"] and result["workflowUpdated"]


def test_combo_list_uses_real_options_and_preserves_current_value():
    """ComfyUI 的 ["COMBO", {options}] 不能把 COMBO 当成选项值提交。"""
    result = _run_panel("""
const data = JSON.stringify(['COMBO', {
  default: '1:1 (Square)',
  options: ['1:1 (Square)', '2:3 (Portrait Photo)', '9:16 (Portrait Widescreen)'],
}]);
const options = parseRunningHubListOptions(data, '9:16 (Portrait Widescreen)');
console.log(JSON.stringify({
  values: options.map(o => o.value),
  currentSelected: options.some(o => o.value === '9:16 (Portrait Widescreen)'),
}));
""")
    assert result == {
        "values": ["1:1 (Square)", "2:3 (Portrait Photo)", "9:16 (Portrait Widescreen)"],
        "currentSelected": True,
    }


def test_other_list_option_shapes_still_parse():
    result = _run_panel("""
const shapes = [
  [['a', 'b'], {default: 'a'}],
  ['a', 'b'],
  [{index: 1, value: 'a', name: 'A'}, {index: 2, value: 'b', name: 'B'}],
];
const values = shapes.map((data, i) => parseRunningHubListOptions(JSON.stringify(data), i === 2 ? '2' : 'b').map(o => o.value));
console.log(JSON.stringify({values}));
""")
    assert result["values"] == [["a", "b"], ["a", "b"], ["1", "2"]]
