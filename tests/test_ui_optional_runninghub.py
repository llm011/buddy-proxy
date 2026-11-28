"""RunningHub 页签由本地界面设置控制，默认不展示。"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from buddy_proxy import __main__ as main
from buddy_proxy.benefits import BenefitsManager
from buddy_proxy.core import settings as settings_mod
from buddy_proxy.core import state as state_mod

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_STATIC = _ROOT / "src/buddy_proxy/web/static"
_APP = (_STATIC / "app.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑前端 JS（CI 有，本机可选）"
)


def _run_settings(saved: str | None = None) -> dict:
    theme = re.search(
        r"function setTheme\(theme\) \{.*?(?=\nlet showRunningHub)", _APP, re.S
    )
    prefs = re.search(
        r"let showRunningHub = false;.*?(?=\n\(function initGlobalTooltip)", _APP, re.S
    )
    switch = re.search(
        r"function switchTab\(name\) \{.*?(?=\ndocument\.querySelectorAll\('\.tab'\))",
        _APP,
        re.S,
    )
    assert theme and prefs and switch
    script = r"""
globalThis.window = globalThis;
class ClassList {
  constructor() { this.values = new Set(); }
  toggle(name, force) { if (force) this.values.add(name); else this.values.delete(name); }
  add(name) { this.values.add(name); }
  remove(name) { this.values.delete(name); }
  contains(name) { return this.values.has(name); }
}
class Element {
  constructor(id, tab) {
    this.id = id;
    this.dataset = tab ? {tab} : {};
    this.hidden = false;
    this.classList = new ClassList();
    this.textContent = '';
    this.innerHTML = '';
  }
  appendChild() {}
}
const tabs = ['overview', 'runninghub'].map(name => new Element('', name));
tabs[0].classList.add('active');
const pages = ['overview', 'runninghub'].map(name => new Element('page-' + name));
pages[0].classList.add('active');
const elements = {};
for (const id of ['modal-title', 'modal-body', 'overlay', 'rangebar']) elements[id] = new Element(id);
globalThis.document = {
  documentElement: {
    theme: 'light',
    getAttribute(name) { return name === 'data-theme' ? this.theme : null; },
    setAttribute(name, value) { if (name === 'data-theme') this.theme = value; },
  },
  getElementById(id) { return elements[id] || null; },
  querySelector(selector) {
    if (selector === '.tab.active') return tabs.find(el => el.classList.contains('active')) || null;
    if (selector === '.tab[data-tab="runninghub"]') return tabs[1];
    return null;
  },
  querySelectorAll(selector) { return selector === '.tab' ? tabs : selector === '.page' ? pages : []; },
};
const storage = new Map();
const initial = process.env.INITIAL_RUNNINGHUB;
if (initial) storage.set('bp-show-runninghub', initial);
globalThis.localStorage = {
  getItem(key) { return storage.has(key) ? storage.get(key) : null; },
  setItem(key, value) { storage.set(key, value); },
};
globalThis.location = {href: 'https://example.test/ui'};
globalThis.history = {replaceState() {}};
globalThis.currentTab = () => {
  const active = tabs.find(el => el.classList.contains('active'));
  return active ? active.dataset.tab : 'overview';
};
globalThis.refreshStickyVars = () => {};
globalThis.loadData = () => Promise.resolve();
globalThis.dataForTab = () => ['stats'];
globalThis.setModalFoot = () => {};
globalThis.api = async path => {
  if (path !== '/ui/api/settings') throw new Error('unexpected API: ' + path);
  return {providers: [
    {id: 'codebuddy', name: 'CodeBuddy', enabled: true},
    {id: 'trae', name: 'Trae', enabled: true},
    {id: 'mimo', name: 'Xiaomi MiMo', enabled: false},
  ]};
};
globalThis.esc = value => String(value);
globalThis.jsq = value => JSON.stringify(value);
const providerChanges = [];
globalThis.toggleProvider = (id, enabled) => providerChanges.push([id, enabled]);
const TABS = ['overview', 'log', 'models', 'order', 'checkin', 'benefits', 'runninghub'];
const RANGE_TABS = new Set(['overview', 'log']);
""" + theme.group(0) + prefs.group(0) + switch.group(0) + r"""
async function run() {
  const initiallyHidden = tabs[1].hidden;
  const initiallyEnabled = showRunningHub && !initiallyHidden;
  switchTab('runninghub');
  const blocked = currentTab() === 'overview' && !pages[1].classList.contains('active');
  await openUiSettings();
  const html = elements['modal-body'].innerHTML;
  const dialogShowsOption = elements['modal-title'].textContent === '界面设置'
    && html.includes('id="show-runninghub"')
    && html.includes('id="ui-theme"');
  const providerOptions = html.includes('Xiaomi MiMo')
    && html.includes('onchange=\'toggleProvider("mimo", this.checked)\'')
    && html.includes('disabled')
    && !html.includes('id="runninghub"');
  const handler = html.match(/onchange='([^']*toggleProvider\("mimo"[^']*)'/)[1];
  new Function(handler).call({checked: true});
  setTheme('dark');
  const themeWorks = document.documentElement.theme === 'dark' && storage.get('bp_theme') === 'dark';
  setRunningHubVisible(true);
  switchTab('runninghub');
  const enabled = !tabs[1].hidden && currentTab() === 'runninghub'
    && storage.get('bp-show-runninghub') === '1';
  setRunningHubVisible(false);
  console.log(JSON.stringify({
    initiallyHidden, blocked, dialogShowsOption, providerOptions, themeWorks, enabled,
    providerChanges,
    disabled: tabs[1].hidden && currentTab() === 'overview'
      && storage.get('bp-show-runninghub') === '0',
    initiallyEnabled,
  }));
}
run().catch(error => { console.error(error); process.exitCode = 1; });
"""
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "INITIAL_RUNNINGHUB": saved or ""},
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_default_hidden_and_checkbox_controls_visibility_and_routes():
    assert _run_settings() == {
        "initiallyHidden": True,
        "blocked": True,
        "dialogShowsOption": True,
        "providerOptions": True,
        "themeWorks": True,
        "enabled": True,
        "providerChanges": [["mimo", True]],
        "disabled": True,
        "initiallyEnabled": False,
    }


def test_saved_preference_restores_runninghub_tab():
    assert _run_settings("1")["initiallyEnabled"] is True


def test_settings_button_is_next_to_theme_and_tab_is_hidden_by_default():
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    assert re.search(
        r'id="theme-toggle"[^>]*>.*?</button>\s*'
        r'<button[^>]*id="ui-settings-toggle"[^>]*onclick="openUiSettings\(\)"',
        html,
        re.S,
    )
    assert re.search(r'data-tab="runninghub"[^>]*\shidden(?:\s|>)', html)


def test_first_upgrade_enables_only_codebuddy_and_trae():
    registered = {"codebuddy", "trae", "mimo", "zcode", "deepseek", "runninghub"}
    assert settings_mod.configured_disabled_providers({}, registered) == {
        "mimo", "zcode", "deepseek",
    }
    assert settings_mod.configured_disabled_providers(
        {"disabled_providers": []}, registered,
    ) == set()


def test_disabled_provider_skips_quota_and_checkin_queries():
    class Provider:
        name = "Disabled"
        supports_checkin = True

        def quota(self):
            raise AssertionError("停用通道不应查询额度")

        def checkin_status(self):
            raise AssertionError("停用通道不应查询签到")

    manager = BenefitsManager("/dev/null", SimpleNamespace(providers={}))
    entry = asyncio.run(manager._provider_entry(
        "disabled", Provider(), {"disabled"}, {}, "2026-11-28",
    ))
    assert entry["disabled"] is True
    assert entry["checkin"] == {"supported": True, "inactive": True}
    assert entry["quota"] == {"supported": False}


def test_settings_api_returns_provider_switches_and_toggle_updates_runtime(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(tmp_path / "settings.json"))
    state = SimpleNamespace(
        providers={
            "trae": SimpleNamespace(name="Trae"),
            "mimo": SimpleNamespace(name="Xiaomi MiMo"),
            "runninghub": SimpleNamespace(name="RunningHub"),
        },
        disabled_providers={"mimo"},
        default_model=None,
        default_provider="codebuddy",
        load_balance=set(),
        session_rotation=set(),
        disabled_accounts=set(),
    )
    monkeypatch.setattr(state_mod, "proxy_state", state)
    client = TestClient(main.app)

    response = client.get("/ui/api/settings")
    assert response.status_code == 200
    assert response.json()["providers"] == [
        {"id": "codebuddy", "name": "CodeBuddy", "enabled": True},
        {"id": "trae", "name": "Trae", "enabled": True},
        {"id": "mimo", "name": "Xiaomi MiMo", "enabled": False},
    ]

    response = client.post("/ui/api/provider-toggle", json={
        "provider": "mimo", "disabled": False,
    })
    assert response.status_code == 200
    assert state.disabled_providers == set()
    assert settings_mod.load_settings()["disabled_providers"] == []
    assert client.get("/ui/api/settings").json()["providers"][-1]["enabled"] is True
