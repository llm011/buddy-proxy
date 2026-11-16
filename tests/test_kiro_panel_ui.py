"""Kiro 前端面板测试（node 桩真跑 benefits_kiro.js）。

与 test_copilot_panel_ui.py 同构：把面板段抽出来用最小 DOM 桩真跑。盯这几件事：

- **两栏布局与组件复用**：外层使用 ``.pat-quota-grid``（多账号并排各占半宽，
  不撑成全宽单列），每账号一块 ``.pat-pkg``，按钮组复用 ``.ag-move`` +
  ``acctRenameButton`` / ``acctMoveButtons`` / ``acctDeleteButton``；
- **分组渲染**：后端 ``Kiro #N · <label> · Credit`` 拆组展示，卡头带跨账号
  合计（``quotaAccountTotal``）、折叠、负载均衡开关与刷新；
- **通道未注册**：providers 里没有 kiro 时整块不渲染；
- **顺位/删除/改名**：走公共 ``acctMove`` / ``acctDelete`` / ``acctRenamePrompt``；
- **特有入口保留**：卡头上保留「导入 auth 文件」与「浏览器授权」。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

_STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
BENEFITS_JS_KIRO = _STATIC / "benefits_kiro.js"
HELPERS_JS = _STATIC / "benefits_accounts.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑面板 JS（CI 有，本机可选）"
)

_STUB = """
let PANEL = {innerHTML: ''};        // #kiro-panel
globalThis.esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
globalThis.quotaItemHtml = it => '<div class="qitem">' + esc(it.label) +
  (typeof it.remaining === 'string' ? '<span class="qnotice">' + esc(it.remaining) + '</span>' : '') +
  '</div>';
globalThis.quotaItemsHtml = (items, key) =>
  '<div class="qbody" data-qfold="' + esc(key) + '">' +
  items.map(globalThis.quotaItemHtml).join('') + '</div>';
globalThis.quotaAccountTotal = (groups, sumItems) =>
  '<span class="acct-total">TOTAL(' + groups.size + ',' + !!sumItems + ')</span>';
globalThis.quotaProviderFold = () => ({badge: '', toggle: '', bodyStart: '', bodyEnd: ''});
globalThis.BENEFITS = {};
globalThis.__SYNC_CALLS = 0;
globalThis.syncQuotaFold = () => { globalThis.__SYNC_CALLS++; };
const MODAL = {};
globalThis.MODAL = MODAL;
globalThis.__FOOT = '';
globalThis.__CLOSED = 0;
globalThis.setModalFoot = html => { globalThis.__FOOT = html; };
globalThis.closeModal = () => { globalThis.__CLOSED++; };
globalThis.document = {
  querySelector: sel => {
    if (sel === '#kiro-panel .pat-pkg-name') return PANEL.__names ? PANEL.__names[0] : null;
    const m = sel.match(/^#kiro-panel \\[data-kiro-idx="(\\d+)"\\]$/);
    if (m && PANEL.__names) return PANEL.__names[Number(m[1]) - 1] || null;
    return null;
  },
  querySelectorAll: () => [],
  getElementById: id => {
    if (id === 'kiro-panel') return PANEL;
    if (id === 'modal-title' || id === 'modal-body' || id === 'overlay') {
      if (!MODAL[id]) MODAL[id] = {textContent: '', innerHTML: '', className: '',
        classList: {_s: new Set(),
                    add(c) { this._s.add(c); },
                    remove(c) { this._s.delete(c); },
                    contains(c) { return this._s.has(c); }}};
      return MODAL[id];
    }
    return null;
  },
};
"""


def _run_js(body: str) -> str:
    script = (
        _STUB
        + HELPERS_JS.read_text(encoding="utf-8")
        + "\n"
        + BENEFITS_JS_KIRO.read_text(encoding="utf-8")
        + "\n(async () => {\n" + body
        + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def test_kiro_panel_uses_pat_quota_grid_and_shared_account_components():
    items = [
        {"label": "Kiro #1 · team1@example.com · Credit", "used": 0, "total": 1000,
         "remaining": 1000, "percent": 0, "unit": "credit"},
        {"label": "Kiro #2 · team8@example.com · Credit", "used": 159.88, "total": 10000,
         "remaining": 9840.12, "percent": 2, "unit": "credit"},
    ]
    out = _run_js(f"""
globalThis.BENEFITS = {{providers: [{{
  id: 'kiro', name: 'Kiro (CodeWhisperer)',
  quota: {{supported: true, sum_items: true, level: 'KIRO PRO', items: {json.dumps(items)}}},
}}]}};
globalThis.api = async () => ({{enabled: true, accounts: [
  {{id: 'c-1', label: 'team1@example.com', region: 'us-east-1', provider: 'Enterprise', index: 1, has_refresh_token: true}},
  {{id: 'u-2', label: 'team8@example.com', region: 'us-east-1', provider: 'Enterprise', index: 2, has_refresh_token: true}},
]}});
globalThis.toast = () => {{}};
globalThis.refreshAll = () => {{}};
renderKiroPanel();
await new Promise(r => setTimeout(r, 10));
const html = PANEL.innerHTML;
console.log(JSON.stringify({{
  hasGrid: html.includes('class="pat-quota-grid"'),
  pkgCount: (html.match(/class="pat-pkg"/g) || []).length,
  agMoveCount: (html.match(/class="ag-move"/g) || []).length,
  hasTotal: html.includes('TOTAL(2,true)'),
  hasMove: html.includes("acctMove('kiro'"),
  hasRename: html.includes("acctRenamePrompt('kiro','c-1')"),
  hasDel: html.includes("acctDelete('kiro','u-2')"),
  hasImport: html.includes('kiroOpenImport()'),
  hasLogin: html.includes('kiroStartLogin()'),
  noRawIdLine: !html.includes('账号 id（排障用）'),
}}));
""")
    data = json.loads(out)
    assert data == {
        "hasGrid": True,
        "pkgCount": 2,
        "agMoveCount": 2,
        "hasTotal": True,
        "hasMove": True,
        "hasRename": True,
        "hasDel": True,
        "hasImport": True,
        "hasLogin": True,
        "noRawIdLine": True,
    }


def test_kiro_panel_acct_move_and_delete_dispatch():
    items = [
        {"label": "Kiro #1 · a@x.com · Credit", "used": 0, "total": 1000,
         "remaining": 1000, "percent": 0, "unit": "credit"},
        {"label": "Kiro #2 · b@x.com · Credit", "used": 10, "total": 1000,
         "remaining": 990, "percent": 1, "unit": "credit"},
    ]
    out = _run_js(f"""
const calls = [];
globalThis.BENEFITS = {{providers: [{{
  id: 'kiro', name: 'Kiro (CodeWhisperer)',
  quota: {{supported: true, sum_items: true, items: {json.dumps(items)}}},
}}]}};
globalThis.api = async (path, opts) => {{
  calls.push({{path, method: (opts && opts.method) || 'GET', body: opts && opts.body}});
  if (path === '/ui/api/kiro/accounts') {{
    return {{enabled: true, accounts: [
      {{id: 'id-1', label: 'a@x.com', index: 1}},
      {{id: 'id-2', label: 'b@x.com', index: 2}},
    ]}};
  }}
  if (path === '/ui/api/kiro/accounts/order') {{
    return {{ok: true, accounts: [{{id: 'id-2', label: 'b@x.com'}}, {{id: 'id-1', label: 'a@x.com'}}]}};
  }}
  return {{ok: true}};
}};
globalThis.toast = () => {{}};
globalThis.refreshAll = () => {{}};
renderKiroPanel();
await new Promise(r => setTimeout(r, 10));
await acctMove('kiro', 1, 1, 'id-1');
const delP = acctDelete('kiro', 'id-2');
await new Promise(r => setTimeout(r, 5));
globalThis.__acctDelYes();
await delP;
console.log(JSON.stringify(calls));
""")
    calls = json.loads(out)
    assert any(c["path"] == "/ui/api/kiro/accounts/order" and json.loads(c["body"]) == {"ids": ["id-2", "id-1"]} for c in calls)
    assert any(c["path"] == "/ui/api/kiro/accounts/id-2" and c["method"] == "DELETE" for c in calls)
