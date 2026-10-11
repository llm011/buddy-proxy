"""trae_work_login 回调链路的纯函数/落盘契约测试。

完全离线，不访问上游——只锁住 2026-09-23 那次「浏览器已显示登录成功、
CLI 却一直等着」踩出来的三个契约：

1. nonce 必须放在回调 URL 的 **path** 里（放 query 会被上游 `?` 拼接冲掉）
2. 回调服务成功/失败**都要**写 RESULT_PATH，CLI 才能立刻知道结果
3. 访问日志要留痕且脱敏（以前 log_message 是 pass，回调失败完全无法排障）
"""

from __future__ import annotations

import json
import urllib.parse

from buddy_proxy.auth.trae_work_login_server import _extract_nonce, _redact_url, _write_result


class TestExtractNonce:
    def test_login_trace_id_camel_is_main_channel(self):
        """主通道：授权页回调时重建 query，只保留 loginTraceID（驼峰）。

        实测回调形态（2026-09-23）：/authorize?isRedirect=true&scope=solo&data=..
        &refreshToken=..&loginTraceID=<我们发的那串>&host=..&userJwt=..。
        `state` 会被丢掉，所以 nonce 只能靠 login_trace_id 承载。
        """
        qs = urllib.parse.parse_qs(
            "isRedirect=true&scope=solo&refreshToken=abc&loginTraceID=cafebabe"
        )
        got = _extract_nonce("/authorize?isRedirect=true&loginTraceID=cafebabe", qs)
        assert got == "cafebabe"

    def test_snake_case_also_accepted(self):
        """我们发出去用下划线，回来是驼峰——两种写法都要认。"""
        qs = urllib.parse.parse_qs("login_trace_id=cafebabe")
        assert _extract_nonce("/authorize?login_trace_id=cafebabe", qs) == "cafebabe"

    def test_state_is_not_trusted_as_main_channel(self):
        """`state` 实测会被授权页丢弃——它只能当兜底，不能当主通道。"""
        qs = urllib.parse.parse_qs("state=cafebabe")
        assert _extract_nonce("/authorize?state=cafebabe", qs) == "cafebabe", (
            "仍要能读（兼容/兜底），但主通道必须是 loginTraceID"
        )
        # 真正要锁的是：回调里两者都有时，以 loginTraceID 为准
        qs2 = urllib.parse.parse_qs("loginTraceID=real&state=stale")
        assert _extract_nonce("/authorize?loginTraceID=real&state=stale", qs2) == "real"

    def test_legacy_path_nonce_still_accepted(self):
        """兼容老链接 /authorize/<nonce>。"""
        qs = urllib.parse.parse_qs("refreshToken=abc")
        assert _extract_nonce("/authorize/deadbeef?refreshToken=abc", qs) == "deadbeef"

    def test_nonce_falls_back_to_query_nonce(self):
        """兼容老回调格式 /authorize?nonce=..."""
        qs = urllib.parse.parse_qs("nonce=0123456789abcdef")
        assert _extract_nonce("/authorize?nonce=0123456789abcdef", qs) == "0123456789abcdef"

    def test_login_trace_id_outranks_path(self):
        """loginTraceID 是主通道，优先级高于 path 段。

        （历史备注：老实现把 nonce 放 path 是为了防上游用 `?` 硬拼冲掉 query；
        实测发现授权页回调时干脆**重建整个 query**，只保留 loginTraceID，
        所以现在以它为第一优先，path 只作兼容。）
        """
        path = "/authorize/STALENONCE?loginTraceID=REALNONCE"
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        assert _extract_nonce(path, qs) == "REALNONCE"

    def test_missing_nonce_is_empty(self):
        assert _extract_nonce("/authorize", {}) == ""
        assert _extract_nonce("/authorize?foo=1", {"foo": ["1"]}) == ""


class TestRedactUrl:
    def test_masks_refresh_token(self):
        out = _redact_url("/authorize/abc?refreshToken=SUPERSECRETVALUE&x=1")
        assert "SUPERSECRETVALUE" not in out
        assert "redacted" in out
        assert "x=1" in out, "非敏感参数要原样保留，否则日志没法排障"

    def test_masks_user_jwt(self):
        jwt = urllib.parse.quote('{"RefreshToken":"SECRET"}')
        out = _redact_url(f"/authorize?userJwt={jwt}")
        assert "SECRET" not in out
        assert "RefreshToken" not in out

    def test_masks_credential(self):
        """credential 内含 refreshToken 明文，日志里绝不能出现。"""
        cred = urllib.parse.quote(json.dumps({"userJwt": {"RefreshToken": "LEAKED"}}))
        out = _redact_url(f"/authorize?credential={cred}&state=abc")
        assert "LEAKED" not in out
        assert "RefreshToken" not in out
        assert "redacted" in out

    def test_masks_user_info_pii(self):
        """userInfo 是 URL-encoded JSON，含手机号/邮箱/昵称/UserID，不能进日志。"""
        info = urllib.parse.quote(json.dumps({
            "ScreenName": "tester",
            "UserID": "1234567890123456789",
            "NonPlainTextMobile": "138******00",
        }))
        out = _redact_url(f"/authorize?userInfo={info}&scope=solo")
        assert "tester" not in out
        assert "1234567890123456789" not in out
        assert "scope=solo" in out, "非敏感参数要保留"

    def test_masks_data_blob(self):
        out = _redact_url("/authorize?data=SIGNATUREBLOB&isRedirect=true")
        assert "SIGNATUREBLOB" not in out
        assert "isRedirect=true" in out

    def test_keeps_nonce_and_path(self):
        out = _redact_url("/authorize/deadbeef?state=cafebabe&login_trace_id=ff")
        assert "/authorize/deadbeef" in out
        assert "state=cafebabe" in out
        assert "login_trace_id=ff" in out

    def test_path_only_unchanged(self):
        assert _redact_url("/authorize/deadbeef") == "/authorize/deadbeef"


class TestWriteResult:
    """终态文件契约：CLI 靠它立刻拿到成功/失败，不能只看 STATE 是否被删。"""

    def _run(self, tmp_path, monkeypatch, **kwargs):
        from buddy_proxy.auth import trae_work_login_server as srv

        target = tmp_path / "result.json"
        monkeypatch.setattr(srv, "RESULT_PATH", target)
        _write_result(**kwargs)
        return json.loads(target.read_text())

    def test_success_result(self, tmp_path, monkeypatch):
        data = self._run(
            tmp_path, monkeypatch,
            ok=True, message="登录成功", nonce="abc", uid="42", nickname="n", expires_at=1,
        )
        assert data["ok"] is True
        assert data["message"] == "登录成功"
        assert data["uid"] == "42"
        assert isinstance(data["at"], int)

    def test_failure_result(self, tmp_path, monkeypatch):
        data = self._run(
            tmp_path, monkeypatch,
            ok=False, message="回调缺少 refreshToken", nonce="abc",
        )
        assert data["ok"] is False
        assert "refreshToken" in data["message"]

    def test_result_carries_nonce_tag(self):
        """结果要带 nonce：CLI 只认跟本轮对得上的，免得上一轮迟到结果误杀本轮。"""
        import inspect

        from buddy_proxy.auth.trae_work_login_server import Handler

        assert "nonce" in inspect.signature(_write_result).parameters
        assert "nonce" in inspect.signature(Handler._reject).parameters

    def test_failure_message_is_plain_text(self):
        """RESULT 文案会被 CLI 直接打印，不能糊 HTML 标签进去。"""
        from buddy_proxy.auth.trae_work_login_server import Handler

        # _reject(page_html, reason, ...)：页面用 HTML，结果用纯文本
        import inspect

        params = list(inspect.signature(Handler._reject).parameters)
        assert params[:3] == ["self", "page_html", "reason"], (
            "必须把「给浏览器的 HTML」和「给 CLI 的纯文本」分开传"
        )


class TestBuildLoginUrl:
    """参数契约：必须与 Trae CN 客户端 handleHandoffExternalSso 完全对齐。"""

    def _build(self, tmp_path, monkeypatch):
        from buddy_proxy.auth import trae_work_login as mod

        monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(mod, "RESULT_PATH", tmp_path / "result.json")
        url, machine_id, device_id, port = mod.build_login_url()
        return mod, url, tmp_path, port

    def test_plugin_version_is_handoff_sentinel(self, tmp_path, monkeypatch):
        """plugin_version 必须是 trae-handoff-1.0。

        传数字形态（如 "2.3.6"）会被授权页当版本号规范化成 "2.3.62834" 并
        **丢掉 auth_callback_url 等全部附加参数**——页面照常显示「登录成功」，
        却永不回跳本机（2026-09-23 用真浏览器实测：t0→t20 后 callback 参数消失）。
        这是「CLI 干等」的真正根因。
        """
        _, url, _, _ = self._build(tmp_path, monkeypatch)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert qs["plugin_version"] == ["trae-handoff-1.0"], (
            "只能用 trae-handoff-1.0；换回版本号会被授权页改写并丢弃回调参数"
        )

    def test_callback_path_matches_client_constant(self, tmp_path, monkeypatch):
        """回调路径必须是 /authorize（客户端 lf.AUTHORIZE），不能带 nonce 段。"""
        _, url, _, _ = self._build(tmp_path, monkeypatch)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        callback = urllib.parse.urlparse(qs["auth_callback_url"][0])
        assert callback.path == "/authorize", "路径要与客户端契约一致（无 path 段）"
        assert not callback.query, "回调 URL 的 query 要留空，免得被上游改写"

    def test_nonce_travels_in_login_trace_id(self, tmp_path, monkeypatch):
        """nonce 必须走 login_trace_id：唯一能穿过授权页并原样回传的字段。

        `state` 会被授权页丢弃（实测回调 query 里没有它），所以不能再靠它。
        """
        _, url, _, _ = self._build(tmp_path, monkeypatch)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        state_file_nonce = json.loads((tmp_path / "state.json").read_text())["nonce"]
        assert qs["login_trace_id"] == [state_file_nonce], (
            "nonce 要放在 login_trace_id 里——`state` 会被授权页丢掉"
        )

    def test_redirect_is_zero_like_client(self, tmp_path, monkeypatch):
        """redirect=0 与客户端一致；服务端必须回 CORS 头（回调是跨源 fetch）。"""
        _, url, _, _ = self._build(tmp_path, monkeypatch)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert qs["redirect"] == ["0"]

    def test_stale_result_is_cleared(self, tmp_path, monkeypatch):
        """新一轮登录要清掉上一轮终态，否则 CLI 一启动就吃到旧结果。"""
        mod, _, _, _ = self._build(tmp_path, monkeypatch)
        stale = tmp_path / "result.json"
        stale.write_text('{"ok": true, "message": "旧的"}')
        mod.build_login_url()
        assert not stale.exists()

    def test_state_contains_fingerprint_pair(self, tmp_path, monkeypatch):
        _, url, _, port = self._build(tmp_path, monkeypatch)
        state = json.loads((tmp_path / "state.json").read_text())
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        # machine_id/device_id 必须与 state 一致：每请求随机换会触发风控
        assert qs["machine_id"] == [state["machine_id"]]
        assert qs["device_id"] == [state["device_id"]]
        # 回调端口要落进 state，供 server 复用（客户端用随机端口，我们固定 18080）
        assert state["port"] == port == 18080


class TestExtractRefreshToken:
    """令牌抽取优先级必须与客户端 handleHandoffExternalSso 一致。"""

    def test_plain_refresh_token(self):
        from buddy_proxy.auth.trae_work_login import extract_refresh_token

        assert extract_refresh_token("/authorize?refreshToken=RT") == "RT"

    def test_user_jwt_json(self):
        from buddy_proxy.auth.trae_work_login import extract_refresh_token

        blob = urllib.parse.quote(json.dumps({"RefreshToken": "FROMJWT"}))
        assert extract_refresh_token(f"/authorize?userJwt={blob}") == "FROMJWT"

    def test_credential_json_wins_over_lower_priority(self):
        """credential 优先级最高——即便同时带了 refreshToken 也以它为准。"""
        from buddy_proxy.auth.trae_work_login import extract_refresh_token

        cred = urllib.parse.quote(json.dumps({"userJwt": {"RefreshToken": "FROMCRED"}}))
        got = extract_refresh_token(
            f"/authorize?credential={cred}&refreshToken=LOWPRIORITY"
        )
        assert got == "FROMCRED"

    def test_credential_form_encoded(self):
        """credential 也可能是 form 串而非 JSON。"""
        from buddy_proxy.auth.trae_work_login import extract_refresh_token

        cred = urllib.parse.quote("refreshToken=FROMFORM&host=x")
        assert extract_refresh_token(f"/authorize?credential={cred}") == "FROMFORM"

    def test_missing_returns_empty(self):
        from buddy_proxy.auth.trae_work_login import extract_refresh_token

        assert extract_refresh_token("/authorize?state=abc") == ""


class TestSecretFilePermissions:
    """STATE / RESULT 都含 nonce，落盘必须 0600。

    以前用 ``write_text`` 落成 0644，而路径在 /tmp（0777+sticky）——
    同机任何进程都能读到 nonce，防伪造就形同虚设。
    """

    def test_state_file_is_0600(self, tmp_path, monkeypatch):
        import stat

        from buddy_proxy.auth import trae_work_login as mod

        target = tmp_path / "state.json"
        monkeypatch.setattr(mod, "STATE_PATH", target)
        monkeypatch.setattr(mod, "RESULT_PATH", tmp_path / "result.json")
        mod.build_login_url()

        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"STATE 含 nonce，权限要 0600，实际 {oct(mode)}"

    def test_result_file_is_0600(self, tmp_path, monkeypatch):
        import stat

        from buddy_proxy.auth import trae_work_login_server as srv

        target = tmp_path / "result.json"
        monkeypatch.setattr(srv, "RESULT_PATH", target)
        srv._write_result(True, "登录成功", nonce="abc")

        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"RESULT 含 nonce，权限要 0600，实际 {oct(mode)}"

    def test_secret_write_is_valid_json(self, tmp_path):
        import json

        from buddy_proxy.auth.trae_work_login import _write_secret

        target = tmp_path / "s.json"
        _write_secret(target, {"nonce": "abc", "中文": "值"})
        assert json.loads(target.read_text()) == {"nonce": "abc", "中文": "值"}
