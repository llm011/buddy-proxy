"""Trae Work 通道重试逻辑单测（完全离线，mock 掉 urlopen，不访问上游）。

覆盖 _send_trae_work_chat 重试循环的边界：
- 前两次瞬态失败、第 3 次成功 → 最终成功且恰好 3 次调用
- 3 次全失败（非 HTTPError）→ 抛 HTTPException(502)，detail 带尝试次数与原始错误
- 非 HTTPError 异常（IncompleteRead / 超时）→ 视为瞬态，重试
- 上游 4xx（HTTPError，非瞬态）→ 不重试直接 502，只调用 1 次

运行：
    .venv/bin/python -m pytest tests/test_trae_work_retry.py -v
"""
from __future__ import annotations

import io
from http.client import IncompleteRead
from unittest import mock

import pytest
from fastapi import HTTPException

from buddy_proxy.trae_provider import _WORK_CHAT_MAX_ATTEMPTS, _send_trae_work_chat

_URL = "https://mchost.guru/api/agent/v3/llm_utils_chat"


def _ok_cm(raw: bytes = b"event: done\n") -> mock.MagicMock:
    """urlopen 成功返回的 context-manager 响应（with ... as resp: resp.read()）。"""
    cm = mock.MagicMock()
    cm.__enter__.return_value.read.return_value = raw
    return cm


def _http_error(code: int, body: bytes = b"upstream error") -> Exception:
    return __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
        _URL, code, "err", {}, io.BytesIO(body)
    )


def _run():
    work = {"access_token": "token-x", "uid": "u1"}
    return _send_trae_work_chat([{"role": "user", "content": "hi"}], "test-model", False, work)


def test_succeeds_on_third_attempt():
    """前两次瞬态失败、第 3 次成功 → 成功返回，恰好 3 次调用，退避 sleep 2 次。"""
    raw = b"event: output\ndata: {}\n"
    with mock.patch("urllib.request.urlopen") as up, mock.patch(
        "buddy_proxy.trae.transport.time.sleep"  # 拆分后 _send_trae_work_chat 在 transport 模块
    ) as sleep:
        up.side_effect = [IncompleteRead(b"part", 5), TimeoutError("read timed out"), _ok_cm(raw)]
        result = _run()
    assert result == raw.decode("utf-8")
    assert up.call_count == 3
    assert [c.args[0] for c in sleep.call_args_list] == [1, 2]


def test_all_attempts_fail_raises_502():
    """3 次全失败（非 HTTPError）→ HTTPException(502)，detail 带次数与原始错误。"""
    with mock.patch("urllib.request.urlopen") as up, mock.patch(
        "buddy_proxy.trae.transport.time.sleep"  # 拆分后 _send_trae_work_chat 在 transport 模块
    ):
        up.side_effect = [TimeoutError("t1"), ConnectionResetError("reset"), IncompleteRead(b"", 9)]
        with pytest.raises(HTTPException) as ei:
            _run()
    assert ei.value.status_code == 502
    assert f"after {_WORK_CHAT_MAX_ATTEMPTS} attempts" in ei.value.detail
    assert "IncompleteRead" in ei.value.detail
    assert up.call_count == _WORK_CHAT_MAX_ATTEMPTS


def test_non_http_error_is_retried():
    """非 HTTPError 异常（IncompleteRead，实测断流形态）→ 视为瞬态，重试后成功。"""
    raw = b"event: done\n"
    with mock.patch("urllib.request.urlopen") as up, mock.patch(
        "buddy_proxy.trae.transport.time.sleep"  # 拆分后 _send_trae_work_chat 在 transport 模块
    ) as sleep:
        up.side_effect = [IncompleteRead(b"half", 100), _ok_cm(raw)]
        result = _run()
    assert result == raw.decode("utf-8")
    assert up.call_count == 2
    assert sleep.call_count == 1


def test_http_error_4xx_not_retried():
    """上游 401（HTTPError，非瞬态）→ 不重试，只调用 1 次。

    2026-10-12 起凭据级错误透传真实状态码（401/403），不再包 502：failover
    的账号级闸门按形态码判「换号可能好转」，包成 502 会绕过冷却换号（实测
    账号 token 被服务端吊销后所有请求都先撞它一次）。其余 4xx 仍 502。"""
    with mock.patch("urllib.request.urlopen") as up, mock.patch(
        "buddy_proxy.trae.transport.time.sleep"  # 拆分后 _send_trae_work_chat 在 transport 模块
    ) as sleep:
        up.side_effect = _http_error(401, b'{"code": 401}')
        with pytest.raises(HTTPException) as ei:
            _run()
    assert ei.value.status_code == 401
    assert "401" in ei.value.detail
    assert up.call_count == 1
    assert sleep.call_count == 0

    # 非凭据级 4xx（如 422 参数错）维持 502 包壳：换号无意义，形态码保 502
    with mock.patch("urllib.request.urlopen") as up, mock.patch(
        "buddy_proxy.trae.transport.time.sleep"
    ) as sleep:
        up.side_effect = _http_error(422, b'{"code": 4001}')
        with pytest.raises(HTTPException) as ei:
            _run()
    assert ei.value.status_code == 502
    assert up.call_count == 1
    assert sleep.call_count == 0
