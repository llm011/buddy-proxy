"""上游 SSE 行切分（对话与生图共用）。

为什么不直接用 ``resp.aiter_lines()``：它在 ``aiter_raw()`` 外面包了
``aiter_text()``，后者把断流 / 读超时**捕获**后转成 ``raise_for_status()``，
于是：

- 网络故障被静默当成「流正常结束」——生图报「返回图片前断流」、非流式
  聚合报「没有 response 对象」，看不出是 502/超时还是上游真空响应；
- 异常的种类（超时 vs 连接重置）也丢了，无法据此决定冷却/换号。

``resp.aiter_bytes()`` 用起来更直白：默认读取会**直接向上抛** ``httpx`` 异常。
这里在其上做增量 UTF-8 解码（TCP 分片不会把中文劈坏）与按行切分，识别
``\\r\\n`` / ``\\r`` / ``\\n`` 三种行尾，与 httpx ``iter_lines()`` 同口径。
"""

from __future__ import annotations

import codecs
from typing import AsyncIterator

import httpx

#: 行尾候选（httpx 的 ``iter_lines()`` 同样认这三种）
_LINE_BREAKS = ("\r\n", "\r", "\n")


async def iter_sse_lines(resp: httpx.Response) -> AsyncIterator[str]:
    """按行产出响应体文本；网络异常原样抛出（超时 / 断流可区分）。"""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    async for chunk in resp.aiter_bytes():
        pending += decoder.decode(chunk)
        while True:
            cut, width = -1, 0
            for br in _LINE_BREAKS:
                pos = pending.find(br)
                if pos >= 0 and (cut < 0 or pos < cut):
                    cut, width = pos, len(br)
            if cut < 0:
                break
            # 行尾正好落在缓冲区末尾的 "\r"：可能是被切开的 "\r\n"，等下一块
            if cut == len(pending) - 1 and pending[cut] == "\r":
                break
            line, pending = pending[:cut], pending[cut + width:]
            yield line
    pending += decoder.decode(b"", True)
    if not pending:
        return
    for br in _LINE_BREAKS:
        pending = pending.replace(br, "\n")
    for line in pending.split("\n"):
        yield line
