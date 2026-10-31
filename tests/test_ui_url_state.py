"""Request-log filters and shared date ranges persist in URL query parameters."""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

APP_JS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src/buddy_proxy/web/static/app_state.js"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑前端 JS（CI 有，本机可选）"
)

STATE_RE = re.compile(
    r"function ymd\(d\).*?(?=\n// 由 STATS\.model_daily)", re.S
)


def _run_state_js(url: str, body: str) -> dict:
    text = APP_JS.read_text(encoding="utf-8")
    match = STATE_RE.search(text)
    assert match, "app.js 里找不到日期与日志筛选状态切片"
    script = f"""
let currentUrl = new URL({json.dumps(url)});
globalThis.location = {{
  get href() {{ return currentUrl.href; }},
  get search() {{ return currentUrl.search; }},
  get hash() {{ return currentUrl.hash; }},
}};
globalThis.history = {{ replaceState(_state, _title, href) {{ currentUrl = new URL(href, currentUrl); }} }};
const listeners = new Map();
const elements = new Map();
function element(id) {{
  if (!elements.has(id)) elements.set(id, {{
    value: '', innerHTML: '', min: '', max: '',
    addEventListener(type, fn) {{ listeners.set(id + ':' + type, fn); }},
  }});
  return elements.get(id);
}}
globalThis.document = {{ getElementById: element }};
globalThis.localStorage = {{ setItem() {{}}, getItem() {{ return null; }} }};
globalThis.STATS = null;
globalThis.RECENT_PAGE = 1;
globalThis.RANGE_MODELS = [];
globalThis.RANGE_CLIENTS = [];
globalThis.renderRecent = () => {{}};
globalThis.esc = s => String(s);
globalThis.pcolor = () => '#000';
{match.group(0)}
(async () => {{
{body}
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_url_query_restores_filters_and_date_and_tracks_changes():
    """刷新恢复 URL 中的状态；筛选、快捷日期和自定义日期改动 URL。"""
    data = _run_state_js(
        "https://example.test/ui?keep=1&range=yest&provider=alpha,beta&client=cli-x#log",
        """
const initial = {
  range: RANGE.q,
  provider: [...LOG_FILTERS.provider].sort(),
  client: [...LOG_FILTERS.client],
};
listeners.get('log-filters:click')({
  target: { closest: () => ({ dataset: {kind: 'provider', val: 'alpha'} }) },
});
const afterFilter = new URL(location.href);
listeners.get('rb-quick:click')({
  target: { closest: () => ({ dataset: {q: 'today'} }) },
});
const afterQuick = new URL(location.href);
element('rb-start').value = '2026-09-30';
element('rb-end').value = '2026-10-02';
listeners.get('rb-start:change')();
console.log(JSON.stringify({
  initial,
  filter: afterFilter.searchParams.get('provider'),
  keep: afterFilter.searchParams.get('keep'),
  hashAfterFilter: afterFilter.hash,
  quick: afterQuick.searchParams.get('range'),
  quickStart: afterQuick.searchParams.get('start'),
  customStart: new URL(location.href).searchParams.get('start'),
  customEnd: new URL(location.href).searchParams.get('end'),
  customRange: new URL(location.href).searchParams.get('range'),
  finalHash: location.hash,
}));
""",
    )
    assert data == {
        "initial": {"range": "yest", "provider": ["alpha", "beta"], "client": ["cli-x"]},
        "filter": "beta",
        "keep": "1",
        "hashAfterFilter": "#log",
        "quick": "today",
        "quickStart": None,
        "customStart": "2026-09-30",
        "customEnd": "2026-10-02",
        "customRange": None,
        "finalHash": "#log",
    }
