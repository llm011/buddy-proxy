"""Trae 模型探测与验证工具。

提供模型可用性探测、候选名单验证等通用功能，供各 Trae Provider 子类复用。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

from fastapi import HTTPException

from ..credentials import _load_work_cred
from ..native_tools import _send_native_chat, _native_rejected, _native_messages
from ..transport import _send_trae_work_chat

log = logging.getLogger(__name__)


async def verify_candidates(
    names: list[str],
    probe_fn: callable,
) -> list[str]:
    """验证候选模型列表，只保留上游真正可用的。

    判定口径：probe_fn 返回 False（4001「参数非法」）即不收；能解析出错误码的
    其它拒绝（1005 会员档位不足、4011 日额度等）说明名字本身有效，照收。
    网络/凭据瞬态按可用保留。

    Args:
        names: 待验证的模型名称列表
        probe_fn: 单模型探测函数，返回 bool

    Returns:
        通过验证的模型名称列表
    """
    ok_names: list[str] = []
    for name in names:
        try:
            ok = await asyncio.to_thread(probe_fn, name)
        except Exception:  # noqa: BLE001 - 网络/凭据瞬态：按可用保留
            ok = True
        if ok:
            ok_names.append(name)
        else:
            log.info("trae 新模型 %s 被上游拒绝（4001），不收录", name)
    return ok_names


def probe_model_single(
    model: str,
    work_cred: dict[str, Any] | None = None,
    use_native: bool = True,
) -> bool:
    """单个模型的最小真实请求探测。

    Args:
        model: 模型名称
        work_cred: 工作账号凭据，None 时使用原生模式
        use_native: 是否使用原生传输

    Returns:
        True=可用；False=上游明确拒绝（4001）
    """
    try:
        if work_cred and work_cred.get("access_token"):
            raw = _send_trae_work_chat(
                [{"role": "user", "content": [{"type": "text", "text": "ping"}]}],
                model, False, work_cred)
        else:
            raw = _send_native_chat(
                _native_messages([{"role": "user", "content": "ping"}]),
                model, False, None)
    except HTTPException as e:
        # 4001 只在 200+SSE error / 400 壳里出现；401/403/502 是凭据或网关问题
        if e.status_code == 400:
            return False
        raise
    return not _native_rejected(raw)
