"""官方消耗明细：三通道的解析函数与 UI 端点。

对应 ``/ui/api/{trae,raccoon,codebuddy}/usage-logs``。这些接口**只做展示**，
绝不回写 metrics——上游粒度是会话/请求级，与我们日志的请求级对不上，回写会
重复记账（见各路由 docstring）。这里盯住：

- **解析**：上游真实形状 → 归一化行；points 带符号（消耗为负）取绝对值；
  ISO 时间按 +08:00 解析（上游带偏移；缺失时补 +08:00，**不落本机时区**）。
- **上游约束固化**（实测踩过，改了就是 9004）：窗口 ≤ 30 天、page_size ≤ 50
  （trae）、请求头是浏览器形态且**不带** IDE 指纹。
- **cursor vs offset**：raccoon 上游是 cursor 分页，解析函数必须返回
  ``(rows, has_more, next_cursor)``，不能假装有 total。
- **端点容错**：上游失败/账号不存在 → ``{"error": ...}``（HTTP 200），让弹窗
  显示原话，不把整个面板拖挂；缺 ``id`` → 400。
- **只读**：端点全是 GET，不碰 metrics。

全离线：网络层打桩 ``trae.pat.net.open_direct``（它就是为此留的注入点）与
``raccoon`` 的 ``core_net.client``。
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import urllib.error
from contextlib import contextmanager
from unittest import mock

import pytest
from fastapi import HTTPException

from buddy_proxy.raccoon import upstream as raccoon_upstream
from buddy_proxy.trae import benefits_api as trae_api
from buddy_proxy.trae import config as trae_config
from buddy_proxy.trae.pat import net as trae_net


# ---------------------------------------------------------------------------
# raccoon：/points/v2/bills?type=expense
# ---------------------------------------------------------------------------

_RACCOON_BILLS = {
    "data": {
        "items": [
            {"points": -12, "created_at": "2026-11-25T10:30:00+08:00",
             "event_name": "桌面端 LLM 对话消耗", "client_type": "IDE",
             "bill_id": "b1", "biz_id": "z1"},
            {"amount": -1.5, "created_at": "2026-11-25T11:00:00+08:00",
             "biz_type": "task", "client_type": "web"},
            {"points": "not-a-number", "created_at": "garbage"},
        ],
        "has_more": True,
        "next_cursor": "CUR+abc",
    }
}


def test_raccoon_parse_usage_logs_normalises_rows_and_cursor():
    rows, has_more, cursor = raccoon_upstream.parse_usage_logs(_RACCOON_BILLS)
    assert has_more is True and cursor == "CUR+abc", "raccoon 是 cursor 分页"
    assert len(rows) == 3
    r0 = rows[0]
    assert r0["credits"] == 12, "points 带符号（消耗为负），展示取绝对值"
    assert r0["currency"] == "credits"
    assert r0["event"] == "桌面端 LLM 对话消耗" and r0["client"] == "IDE"
    assert r0["ts"] is not None
    # 不带用户 prompt：用事件名占位，别空着
    assert r0["preview"] == "桌面端 LLM 对话消耗"
    # 兼容 amount 字段；biz_type 兜底 event
    assert rows[1]["credits"] == 1.5 and rows[1]["event"] == "task"
    # 脏数据不抛：非数字积分 → None，坏时间 → None
    assert rows[2]["credits"] is None and rows[2]["ts"] is None
    assert rows[2]["time_raw"] == "garbage", "原始串保留供 hover 核对"


def test_raccoon_iso_to_epoch_uses_plus8_when_offset_missing():
    """上游偏移缺失时按 +08:00 补，**不落本机时区**——否则海外机器上时间全偏。

    必须在**非 +08:00 的时区**下验证：在 +08:00 的机器上，「补 +08:00」和
    「用本机时区」结果一模一样，测试照样绿但什么都没拦住（变异测试踩过）。
    这里用子进程把 ``TZ`` 固定成 UTC，断言不依赖跑测试的机器在哪。
    """
    import datetime as _dt
    import os
    import subprocess
    import sys
    code = ("from buddy_proxy.raccoon import upstream; "
            "print(upstream._iso_to_epoch('2026-11-25T10:30:00'))")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, check=True, env={**os.environ, "TZ": "UTC"})
    got = float(proc.stdout.strip())
    # 用绝对值核对，不借本机时区：10:30+08:00 == 02:30 UTC
    want = _dt.datetime(2026, 11, 25, 2, 30, 0, tzinfo=_dt.timezone.utc).timestamp()
    assert got == want, f"偏移缺失应按 +08:00 解析：得到 {got}，应为 {want}"
    # 对照：同一时刻带偏移量写出来，结果相同（两种写法指向同一时间点）
    assert raccoon_upstream._iso_to_epoch("2026-11-25T10:30:00+08:00") == want
    for bad in ("", "   ", "not-a-date", None):
        assert raccoon_upstream._iso_to_epoch(bad) is None, "解析不了返回 None，别抛"


def test_raccoon_parse_usage_logs_tolerates_garbage_payload():
    assert raccoon_upstream.parse_usage_logs(None) == ([], False, None)
    assert raccoon_upstream.parse_usage_logs("x") == ([], False, None)
    assert raccoon_upstream.parse_usage_logs({"data": "oops"}) == ([], False, None)
    assert raccoon_upstream.parse_usage_logs({"data": {"items": [1, "x", None]}}) == ([], False, None)


# ---------------------------------------------------------------------------
# trae：官方 dashboard#usage
# ---------------------------------------------------------------------------

_TRAE_SESSIONS = {
    "code": 0,
    "total": 2,
    "user_usage_group_by_sessions": [
        {"session_id": "s1", "usage_time": 1764000000, "model_name": "GLM-5.4",
         "credits_float": 1.5256, "mode": "", "use_max_mode": True,
         "user_input_preview": "帮我看看这个报错",
         "extra_info": {"input_token": 10, "output_token": 20,
                        "cache_read_token": 3, "cache_write_token": 4}},
        {"session_id": "s2", "amount_float": 2, "mode": "Max", "use_max_mode": False},
    ],
}


def _stub_trae_net(payload, *, capture: dict | None = None, raise_exc=None):
    @contextmanager
    def fake_open(req, timeout=20):
        if raise_exc is not None:
            raise raise_exc
        if capture is not None:
            capture["url"] = req.full_url
            capture["headers"] = dict(req.header_items())
            capture["body"] = json.loads(req.data.decode("utf-8"))
        yield io.BytesIO(json.dumps(payload).encode("utf-8"))
    return fake_open


def test_trae_fetch_usage_logs_builds_browser_shaped_request():
    """请求头必须是浏览器 Web 形态且**不带** IDE 指纹——带了上游 9004。"""
    cap: dict = {}
    with mock.patch.object(trae_net, "open_direct", _stub_trae_net(_TRAE_SESSIONS, capture=cap)):
        out = trae_api.fetch_usage_logs(token="tok", region="cn", page_num=2, page_size=30)
    assert out["total"] == 2 and out["page"] == 2 and out["page_size"] == 30
    assert out["billing"] == trae_config.resolve_trae_region("cn").billing
    assert cap["url"] == trae_config.resolve_trae_region("cn").usage_logs_url()
    h = {k.lower(): v for k, v in cap["headers"].items()}
    assert h["authorization"] == "Cloud-IDE-JWT tok"
    assert "mozilla" in h["user-agent"].lower(), "浏览器 UA"
    assert h["origin"].startswith("https://") and h["referer"].startswith("https://")
    for ide_header in ("x-device-id", "x-device-brand", "package-type"):
        assert ide_header not in h, f"不能带 IDE 指纹头 {ide_header}"
    assert cap["body"]["usage_type"] == [7], "usage_type=7 才是 Cloud-IDE 会话消耗"
    assert cap["body"]["page_num"] == 2


def test_trae_fetch_clamps_window_and_page_size():
    """窗口 ≤ 30 天、page_size ≤ 50：超了上游 9004，必须在本地收窄。"""
    cap: dict = {}
    end = 1764000000
    with mock.patch.object(trae_net, "open_direct", _stub_trae_net(_TRAE_SESSIONS, capture=cap)):
        trae_api.fetch_usage_logs(token="t", region="cn", start_ts=end - 90 * 86400,
                                  end_ts=end, page_size=999)
    assert cap["body"]["end_time"] - cap["body"]["start_time"] <= 30 * 86400
    assert cap["body"]["page_size"] <= 50


def test_trae_normalize_row_merges_max_mode_and_defends_missing_fields():
    rows = [trae_api._normalize_usage_log_row(s)
            for s in _TRAE_SESSIONS["user_usage_group_by_sessions"]]
    a, b = rows
    assert a["credits"] == 1.5256 and a["model"] == "GLM-5.4"
    assert a["mode"] == "Max", "use_max_mode=true 且 mode 为空 → 标 Max"
    assert a["input_tokens"] == 10 and a["cache_read_tokens"] == 3
    assert b["credits"] == 2, "credits_float 缺失时退到 amount_float"
    assert b["mode"] == "Max", "mode 已有就不覆盖"
    # 全缺：不抛，字段给空/None
    empty = trae_api._normalize_usage_log_row({})
    assert empty["session_id"] == "" and empty["credits"] is None and empty["preview"] == ""


def test_trae_fetch_raises_on_upstream_reject_or_http_error():
    """9004 应用级错误（HTTP 可能 200）与 HTTP 错误都要抛 RuntimeError，由端点转空态。"""
    with mock.patch.object(trae_net, "open_direct",
                           _stub_trae_net({"code": 9004, "message": "param invalid"})):
        with pytest.raises(RuntimeError, match="9004"):
            trae_api.fetch_usage_logs(token="t", region="cn")
    with mock.patch.object(trae_net, "open_direct", _stub_trae_net(
            None, raise_exc=urllib.error.HTTPError(
                "u", 500, "err", {}, io.BytesIO(b"boom")))):
        with pytest.raises(RuntimeError, match="500"):
            trae_api.fetch_usage_logs(token="t", region="cn")


def test_trae_usage_logs_url_is_v1_not_ug_version():
    """会话明细走 v1；别复用额度接口的 ug_usage_version（海外额度 v1、明细 v2 是 404）。"""
    for region in ("cn", "global"):
        assert "/trae/api/v1/pay/query_user_usage_group_by_session" \
            in trae_config.resolve_trae_region(region).usage_logs_url()


# ---------------------------------------------------------------------------
# UI 端点
# ---------------------------------------------------------------------------


def _call(fn, **params):
    """直接调端点函数（比 TestClient 轻）：补上 ``client.host`` 过本机校验。"""
    req = mock.Mock()
    req.query_params = params
    req.client = mock.Mock(host="127.0.0.1")  # _ensure_local 只认本机
    return asyncio.run(fn(req))


def test_endpoint_missing_id_is_400():
    from buddy_proxy.web.ui import channels as ch
    for fn in (ch.ui_trae_usage_logs, ch.ui_raccoon_usage_logs, ch.ui_codebuddy_usage_logs):
        with pytest.raises(HTTPException) as ei:
            _call(fn, id="")
        assert ei.value.status_code == 400


@pytest.mark.parametrize("params", [
    {"id": "a", "page": "abc"},
    {"id": "a", "page_size": "abc"},
])
def test_usage_log_endpoints_reject_non_integer_pagination(params):
    """三条路由都不能让 int() 的 ValueError 漏成 500。"""
    from buddy_proxy.web.ui import channels as ch

    fns = [ch.ui_trae_usage_logs, ch.ui_raccoon_usage_logs,
           ch.ui_codebuddy_usage_logs]
    if "page" in params:
        fns.remove(ch.ui_raccoon_usage_logs)  # cursor 分页本来就没有 page 参数
    for fn in fns:
        with pytest.raises(HTTPException) as ei:
            _call(fn, **params)
        assert ei.value.status_code == 400
        assert "正整数" in str(ei.value.detail)


def test_trae_endpoint_returns_error_object_not_5xx(monkeypatch):
    """账号不存在 / 上游拒绝 → {"error":...}（HTTP 200），弹窗显示原话。"""
    from buddy_proxy.web.ui import channels as ch

    def boom(aid):
        raise RuntimeError("账号不存在: nope")
    monkeypatch.setattr("buddy_proxy.trae.credentials.ensure_account_token", boom)
    out = _call(ch.ui_trae_usage_logs, id="nope", page="1", page_size="30")
    assert "账号不存在" in out["error"] and out["rows"] == []
    # 三路统一叫 currency（前端一套格式化通吃），不叫 billing
    assert "billing" not in out


def test_trae_endpoint_renames_billing_to_currency(monkeypatch):
    from buddy_proxy.web.ui import channels as ch
    monkeypatch.setattr("buddy_proxy.trae.credentials.ensure_account_token",
                        lambda aid: ("tok", {"region": "cn"}))
    monkeypatch.setattr(trae_api, "fetch_usage_logs",
                        lambda **kw: {"billing": "credits", "rows": [1], "total": 1,
                                      "page": kw["page_num"], "page_size": kw["page_size"]})
    out = _call(ch.ui_trae_usage_logs, id="a", page="3", page_size="20")
    assert out["currency"] == "credits" and out["page"] == 3
    # 必须 pop 掉 billing：只 get 不 pop 的话 currency 照样对，但 billing 漏进
    # 响应——前端按 currency 渲染、不认 billing，两套字段并存就是隐患（变异踩过）。
    assert "billing" not in out


def test_raccoon_endpoint_forwards_cursor_and_clamps_page_size(monkeypatch):
    from buddy_proxy.web.ui import channels as ch
    monkeypatch.setattr("buddy_proxy.raccoon.credentials.ensure_account_token",
                        lambda aid: ("tok", {"device_id": "dev"}))
    seen: dict = {}

    class _Resp:
        status_code = 200
        def json(self):
            return _RACCOON_BILLS

    class _Client:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url, headers=None, params=None):
            seen.update(url=url, headers=headers, params=params)
            return _Resp()

    import buddy_proxy.core.net as core_net
    monkeypatch.setattr(core_net, "client", lambda timeout=20: _Client())
    out = _call(ch.ui_raccoon_usage_logs, id="a", page_size="500", cursor="CUR+1")
    assert seen["params"]["cursor"] == "CUR+1" and seen["params"]["type"] == "expense"
    assert seen["params"]["paging.limit"] == 100, "page_size 上限 100"
    assert out["has_more"] is True and out["next_cursor"] == "CUR+abc"
    assert "total" not in out, "cursor 分页没有 total，别编一个"


def test_all_usage_log_routes_are_get():
    from buddy_proxy.web.ui import channels as ch
    for fn in (ch.ui_trae_usage_logs, ch.ui_raccoon_usage_logs, ch.ui_codebuddy_usage_logs):
        methods = {m for r in ch.app.routes
                   if getattr(r, "endpoint", None) is fn for m in getattr(r, "methods", ())}
        assert methods == {"GET"}, f"{fn.__name__} 必须是只读 GET，实际 {methods}"


def test_local_str_to_epoch_parses_both_formats():
    from buddy_proxy.web.ui.channels import _local_str_to_epoch
    full = _local_str_to_epoch("2026-11-25 01:05:00")
    assert full == time.mktime(time.strptime("2026-11-25 01:05:00", "%Y-%m-%d %H:%M:%S"))
    assert _local_str_to_epoch("2026-11-25 01:05") is not None
    for bad in ("", "yesterday", None, "2026-13-45 99:99:99"):
        assert _local_str_to_epoch(bad) is None
