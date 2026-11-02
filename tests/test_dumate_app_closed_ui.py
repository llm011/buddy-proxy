"""关掉 DuMate 桌面端时，额度面板仍然要能认出账号、看到额度。

2026-11-02 用户报障：没开百度搭子时，额度卡只剩一行「App 未运行」——账号名退化成
「本机账号」、没有「已登录」、额度明细整块消失，看着像这个通道废了。实际：

- 已装 + 已登录时，首次转发/首次查额度都会自动拉起独立 ``dumate-main-server``，
  「App 未运行」不是故障，只是「待命」；
- 账号身份（bceUserId / displayName）与额度、签到一样，读的是本机
  ``auth.json`` + bceConsole cookie，跟本地进程在不在毫无关系。

这里锁三处：

1. ``discovery.describe_state()`` 在未运行但可自动拉起时给出 ``auto_start=True``
   与「自动拉起」的 hint，并**照样**带出账号身份；
2. ``/ui/api/dumate/accounts`` 不再因 ``discover() is None`` 返回空列表；
3. ``benefits_dumate.js`` 未就绪但``auto_start`` 时仍铺额度明细、圆点说「待命」。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from unittest import mock

from buddy_proxy.dumate import discovery
from buddy_proxy.dumate import standalone as st

_STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"


def _fake_app(tmp_path: pathlib.Path) -> None:
    """造一个「装了 App + 已登录」的本机环境（不碰真实 auth.json）。"""
    res = tmp_path / "DuMate.app" / "Contents" / "Resources"
    res.joinpath(*st._BIN_REL).parent.mkdir(parents=True)
    res.joinpath(*st._BIN_REL).write_text("x")
    res.joinpath(*st._CONFIG_REL).parent.mkdir(parents=True)
    res.joinpath(*st._CONFIG_REL).write_text("x")
    (tmp_path / "auth.json").write_text(json.dumps({
        "activeProviderType": "bce",
        "activeProfileId": "p1",
        "accountProfiles": [{
            "profileId": "p1", "bceUserId": "uid-1", "bceAccountId": "acc-1",
            "displayName": "jsongo",
        }],
    }), encoding="utf-8")


# --- describe_state ---------------------------------------------------------


def test_describe_state_auto_start_and_identity_when_app_closed(tmp_path):
    """未运行但可自动拉起：auto_start=True + 账号身份照旧带出来。"""
    from buddy_proxy.dumate import cookies as dumate_cookies

    _fake_app(tmp_path)
    with mock.patch.object(st, "_APP_CANDIDATES", (str(tmp_path / "DuMate.app"),)), \
         mock.patch.object(dumate_cookies, "_user_data_dir", return_value=str(tmp_path)), \
         mock.patch.object(discovery, "discover", return_value=None), \
         mock.patch.object(discovery, "is_app_installed", return_value=True), \
         mock.patch.dict("os.environ", {}, clear=True):
        info = discovery.describe_state()

    assert info["ready"] is False and info["running"] is False
    assert info["auto_start"] is True, "已装 + 已登录 + 开关开 → 应标可自动拉起"
    assert "自动拉起" in info["hint"]
    # 进程没在跑≠不知道是谁：面板要靠这两位画标题与「已登录」
    assert info["account_id"] == "uid-1"
    assert info["display_name"] == "jsongo"


def test_describe_state_auto_start_false_when_standalone_disabled(tmp_path):
    """关掉兜底（BUDDY_DUMATE_STANDALONE=off）→ auto_start=False，前端不该说「待命」。"""
    from buddy_proxy.dumate import cookies as dumate_cookies

    _fake_app(tmp_path)
    with mock.patch.object(st, "_APP_CANDIDATES", (str(tmp_path / "DuMate.app"),)), \
         mock.patch.object(dumate_cookies, "_user_data_dir", return_value=str(tmp_path)), \
         mock.patch.object(discovery, "discover", return_value=None), \
         mock.patch.object(discovery, "is_app_installed", return_value=True), \
         mock.patch.dict("os.environ", {"BUDDY_DUMATE_STANDALONE": "off"}, clear=True):
        info = discovery.describe_state()

    assert info["auto_start"] is False
    assert "请先打开" in info["hint"]


def test_can_auto_start_shares_one_predicate(tmp_path):
    """``can_auto_start`` 与 hint、ensure_auth、forward 用同一处判据（别再各写一份）。"""
    from buddy_proxy.dumate import cookies as dumate_cookies

    _fake_app(tmp_path)
    with mock.patch.object(st, "_APP_CANDIDATES", (str(tmp_path / "DuMate.app"),)), \
         mock.patch.object(dumate_cookies, "_user_data_dir", return_value=str(tmp_path)), \
         mock.patch.dict("os.environ", {}, clear=True):
        assert discovery.can_auto_start() is True
    # 未登录（auth.json 没 user_id）→ 拉不起来
    (tmp_path / "auth.json").write_text("{}", encoding="utf-8")
    with mock.patch.object(st, "_APP_CANDIDATES", (str(tmp_path / "DuMate.app"),)), \
         mock.patch.object(dumate_cookies, "_user_data_dir", return_value=str(tmp_path)), \
         mock.patch.dict("os.environ", {}, clear=True):
        assert discovery.can_auto_start() is False


# --- /ui/api/dumate/accounts ------------------------------------------------


def test_accounts_endpoint_works_without_local_process(monkeypatch):
    """本地进程没起时，accounts 仍返回本机账号（此前直接返回空列表）。"""
    from buddy_proxy.web.ui import channels

    monkeypatch.setenv("BUDDY_PROXY_ADMIN_OPEN", "1")
    monkeypatch.setattr(discovery, "discover", lambda: None)
    monkeypatch.setattr(channels, "_dumate_account_info_cached", lambda: {
        "bceconsole_authenticated": True,
        "account_id": "uid-1",
        "display_name": "jsongo",
        "checkin_total_points": 1000,
        "checkin_total_times": 2,
    })

    r = TestClient(channels.app).get("/ui/api/dumate/accounts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert [a["display_name"] for a in body["accounts"]] == ["jsongo"]


# --- 面板 JS ----------------------------------------------------------------


def _run(js: str) -> dict:
    src = (_STATIC / "benefits_dumate.js").read_text(encoding="utf-8")
    seg = src[src.index("// ---- DUMATE 面板"):src.index("function loadDumateAccounts()")]
    script = _STUB + seg + js
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


_STUB = """
globalThis.fmtNum = n => String(n);
globalThis.syncQuotaFold = () => {};
globalThis.PANEL = {innerHTML: ''};
globalThis.document = {getElementById: id => (id === 'dumate-panel' ? PANEL : null)};
globalThis.esc = s => String(s == null ? '' : s);
globalThis.acctFor = () => null;
globalThis.acctSubHtml = () => '';
globalThis.acctDeleteButton = () => '<button class="del"></button>';
globalThis.acctRowButtons = () => '<span class="rowbtns"></span>';
globalThis.providerRefreshButton = () => '';
globalThis.loadDumateAccounts = () => Promise.resolve();
globalThis.quotaItemHtml = it => '<div class="qitem">' + esc(it.label) + '</div>';
globalThis.quotaItemsHtml = (items, key) =>
  '<div class="qbody" data-qfold="' + esc(key) + '">' + items.map(globalThis.quotaItemHtml).join('') + '</div>';
globalThis.quotaHeadSum = q => (q && q.items ? '<span class="headsum">HEADSUM</span>' : '');
// 通道级折叠（benefits_quota.js）。本用例只关心额度区画没画，给个恒展开的桩。
globalThis.quotaProviderFold = () => ({exhausted: false, badge: '', toggle: '',
                                        bodyStart: '<div class="provider-quota-body">', bodyEnd: '</div>'});
globalThis.api = async () => globalThis.__STATUS;
globalThis.BENEFITS = {providers: [{id: 'dumate', quota: {supported: true, items: [
  {label: '订阅积分', used: 160.04, total: 1000, remaining: 839.96, percent: 16.0,
   reset_ts: null, expire_ts: null, unit: 'points', head_only: true},
]}}]};
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 跑面板 JS")
def test_panel_shows_quota_when_standby_auto_start():
    """未运行但可自动拉起（auto_start）→ 仍铺额度明细，状态写「待命」而非光秃秃「App 未运行」。"""
    data = _run("""
globalThis.__STATUS = {ready: false, installed: true, running: false, auto_start: true,
                       bceconsole_authenticated: true, display_name: 'jsongo',
                       hint: '已安装 DuMate.app，当前未运行——首次转发时代理会自动拉起独立实例，无需手动打开。'};
// 首渲时 status 还没到（「检测中…」），loadDumateStatus 到手后会自重建
renderDumatePanel();
await new Promise(r => setTimeout(r, 30));
const html = PANEL.innerHTML;
console.log(JSON.stringify({
  standby: html.includes('待命'),
  quota: html.includes('HEADSUM') && html.includes('qbody'),
  login: html.includes('已登录'),
  hint: html.includes('无需手动打开'),
}));
""")
    assert data["standby"] is True, "圆点文案要说「待命 · 首次转发自动拉起」"
    assert data["quota"] is True, "待命态也要铺额度明细（额度走 cookie，与进程无关）"
    assert data["login"] is True, "已登录徽标不该依赖进程在跑"
    assert data["hint"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 跑面板 JS")
def test_panel_keeps_hint_when_truly_unavailable():
    """未装 / 无从拉起 → 仍回到说明条（这时额度确实查不到）。"""
    data = _run("""
globalThis.__STATUS = {ready: false, installed: false, running: false, auto_start: false,
                       hint: '未检测到 DuMate.app。'};
renderDumatePanel();
await new Promise(r => setTimeout(r, 30));
const html = PANEL.innerHTML;
console.log(JSON.stringify({
  quota: html.includes('qbody'),
  hint: html.includes('未检测到 DuMate.app'),
  standby: html.includes('待命'),
}));
""")
    assert data["quota"] is False and data["hint"] is True
    assert data["standby"] is False
