"""模型页搜索：实时筛选、搜索态全展开、清空恢复折叠偏好且不写坏持久化。"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

CHARTS_JS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src/buddy_proxy/web/static/charts.js"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑前端 JS（CI 有，本机可选）"
)

# 两段切片：折叠/搜索状态与监听器（toggleGroup、setAllGroups 在内） + 渲染函数本体
STATE_RE = re.compile(
    r"let COLLAPSED = new Set\(\);.*?(?=\n// 打卡日历悬停 tooltip)", re.S
)
RENDER_RE = re.compile(r"function renderGroups\(\) \{.*?(?=\nlet RECENT_PAGE)", re.S)

MODELS = {
    "default_model": "gpt-6",
    "groups": [
        {"id": "trae", "name": "Trae", "models": [
            {"id": "gpt-6", "name": "GPT 6"},
            {"id": "claude-opus-4", "name": "Claude Opus 4"},
        ]},
        {"id": "qoder", "name": "Qoder Intl", "models": [
            {"id": "qwen-max", "name": "Qwen Max"},
        ]},
    ],
}


def _run_search_js(body: str, collapsed: list[str] | None = None) -> dict:
    text = CHARTS_JS.read_text(encoding="utf-8")
    state, render = STATE_RE.search(text), RENDER_RE.search(text)
    assert state and render, "charts.js 里找不到模型分组搜索/渲染切片"
    script = f"""
const listeners = new Map();   // id:type -> [fn]（同一元素同一事件可多个）
const elements = new Map();
function element(id) {{
  if (!elements.has(id)) elements.set(id, {{
    value: '', innerHTML: '', textContent: '', hidden: false, disabled: false,
    addEventListener(type, fn) {{
      const k = id + ':' + type;
      if (!listeners.has(k)) listeners.set(k, []);
      listeners.get(k).push(fn);
    }},
    focus() {{ this.focused = true; }},
  }});
  return elements.get(id);
}}
function fire(id, type, ev) {{
  (listeners.get(id + ':' + type) || []).forEach(fn => fn(ev));
}}
globalThis.document = {{
  getElementById: element,
  querySelector: () => null,
  querySelectorAll: () => [],
}};
globalThis.CSS = {{ escape: s => s }};
const lsWrites = [];
globalThis.localStorage = {{
  getItem: () => {json.dumps(json.dumps(collapsed or []))},
  setItem: (k, v) => lsWrites.push([k, v]),
}};
globalThis.esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
globalThis.jsq = s => JSON.stringify(s);
globalThis.bareModelId = (id, provider) => {{
  const p = String(provider || ''), s = String(id || '');
  return p && s.startsWith(p + '/') ? s.slice(p.length + 1) : s;
}};
globalThis.fmtMs = ms => ms + 'ms';
globalThis.fmtNum = n => String(n);
globalThis.refreshStickyVars = () => {{}};
globalThis.MODELS = {json.dumps(MODELS)};
{state.group(0)}
{render.group(0)}
globalThis.lsWrites = lsWrites;
globalThis.fire = fire;
globalThis.getEl = element;
// 渲染快照：分组及其折叠态、可见模型 id、空态标记、汇总文案
globalThis.snapshot = () => {{
  const html = getEl('groups').innerHTML;
  return {{
    groups: [...html.matchAll(/class="group( collapsed)?" data-gid="([^"]+)"/g)]
      .map(m => ({{gid: m[2], collapsed: !!m[1]}})),
    models: [...html.matchAll(/<span class="mono">([^<]*)<\\/span>/g)].map(m => m[1]),
    empty: /class="empty"/.test(html),
    hasClearBtn: html.includes('data-clear-model-search'),
    summary: getEl('model-summary').textContent,
  }};
}};
(async () => {{
{body}
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_provider_match_keeps_whole_group_and_clear_restores():
    """通道名命中 → 整组保留；清空后回到全量视图。"""
    data = _run_search_js("""
fire('model-search', 'input', {target: {value: 'trae'}});
const r1 = snapshot();
fire('model-search', 'input', {target: {value: ''}});
const r2 = snapshot();
console.log(JSON.stringify({r1, r2, clearHidden: getEl('model-search-clear').hidden}));
""")
    r1, r2 = data["r1"], data["r2"]
    assert [g["gid"] for g in r1["groups"]] == ["trae"]
    assert r1["models"] == ["gpt-6", "claude-opus-4"]  # 通道命中 → 组内全保留
    assert "命中 1 个通道 · 2 个模型" in r1["summary"]
    assert [g["gid"] for g in r2["groups"]] == ["trae", "qoder"]
    assert data["clearHidden"] is True


def test_model_match_filters_rows():
    """仅模型命中 → 组内只留命中行，未命中的组整组消失。"""
    data = _run_search_js("""
fire('model-search', 'input', {target: {value: 'opus'}});
console.log(JSON.stringify(snapshot()));
""")
    r = data
    assert [g["gid"] for g in r["groups"]] == ["trae"]
    assert r["models"] == ["claude-opus-4"]
    assert not r["empty"]


def test_case_and_whitespace_insensitive():
    """大小写与首尾空白不影响匹配。"""
    data = _run_search_js("""
fire('model-search', 'input', {target: {value: '  TRAE '}});
const r1 = snapshot();
fire('model-search', 'input', {target: {value: '  OpUs '}});
const r2 = snapshot();
console.log(JSON.stringify({r1, r2}));
""")
    assert [g["gid"] for g in data["r1"]["groups"]] == ["trae"]
    assert data["r2"]["models"] == ["claude-opus-4"]


def test_no_result_state_offers_clear():
    """无命中显示空态 + 清空入口；汇总显示命中 0。"""
    data = _run_search_js("""
fire('model-search', 'input', {target: {value: '不存在的模型'}});
const r = snapshot();
// 空态里的清空按钮走 #groups 的点击代理
fire('groups', 'click', {target: {closest: s => s === '[data-clear-model-search]' ? {} : null}});
console.log(JSON.stringify({r, after: snapshot(), inputValue: getEl('model-search').value}));
""")
    assert data["r"]["empty"] is True
    assert data["r"]["hasClearBtn"] is True
    assert data["r"]["groups"] == []
    assert "命中 0 个通道" in data["r"]["summary"]
    assert data["after"]["groups"], "点空态的清空按钮应恢复全量列表"
    assert data["inputValue"] == ""


def test_search_expands_without_writing_prefs():
    """搜索态强制展开（无视 COLLAPSED），toggle/全部按钮均为空操作，不写 bp-collapsed。"""
    data = _run_search_js("""
renderGroups();
toggleGroup('qoder');          // 搜索前的折叠操作（应生效并持久化一次）
const writesBefore = lsWrites.length;
fire('model-search', 'input', {target: {value: 'qwen'}});
const r = snapshot();
toggleGroup('trae');            // 搜索中点组头 → 空操作
setAllGroups(true);             // 搜索中点全部折叠 → 空操作
const collapsedNow = [...COLLAPSED].sort();
const btnState = [getEl('models-expand').disabled, getEl('models-collapse').disabled];
console.log(JSON.stringify({r, collapsedNow, btnState, writes: lsWrites.slice(writesBefore)}));
""", collapsed=[])
    r = data["r"]
    assert [g["gid"] for g in r["groups"]] == ["qoder"]
    assert r["models"] == ["qwen-max"]
    assert r["groups"][0]["collapsed"] is False, "COLLAPSED 含 qoder 但搜索态应强制展开"
    assert data["collapsedNow"] == ["qoder"], "搜索中的折叠操作不应改写 COLLAPSED"
    assert data["btnState"] == [True, True], "搜索中应禁用全部展开/折叠按钮"
    assert data["writes"] == [], "搜索期间不得写 bp-collapsed"


def test_clear_restores_mixed_collapse_state():
    """Esc 清空后恢复搜索前的展开/折叠组合。"""
    data = _run_search_js("""
renderGroups();                // 初始即 trae 展开 / qoder 折叠（来自 bp-collapsed）
fire('model-search', 'input', {target: {value: 'a'}});
const searching = snapshot();   // 'a' 命中 trae（通道）+ qoder（qwen-max），两组且全展开
fire('model-search', 'keydown', {key: 'Escape', preventDefault() {}});
const restored = snapshot();
console.log(JSON.stringify({searching, restored, focused: !!getEl('model-search').focused,
                            inputValue: getEl('model-search').value}));
""", collapsed=["qoder"])
    assert [g["collapsed"] for g in data["searching"]["groups"]] == [False, False]
    restored = {g["gid"]: g["collapsed"] for g in data["restored"]["groups"]}
    assert restored == {"trae": False, "qoder": True}, "清空应恢复原折叠偏好"
    assert data["focused"] is True, "Esc 清空后焦点留在输入框"
    assert data["inputValue"] == ""


def test_rerender_preserves_search():
    """30s 轮询触发的重渲染不丢搜索条件（搜索仍是过滤视图）。"""
    data = _run_search_js("""
fire('model-search', 'input', {target: {value: 'opus'}});
const r1 = snapshot();
renderGroups();                 // 模拟轮询重渲染
const r2 = snapshot();
console.log(JSON.stringify({r1, r2}));
""")
    assert data["r1"] == data["r2"]
    assert data["r2"]["models"] == ["claude-opus-4"]
