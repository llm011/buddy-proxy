"""最小 WebSocket 客户端（纯 stdlib，RFC6455）—— CDP 专用。

2026-10 自 ``cdp_client.py`` 拆出；``_WS`` 名字保持不变，旧路径
``buddy_proxy.doubao.cdp_client`` 保持 re-export 兼容。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
from typing import Any


class WSClosed(RuntimeError):
    """WebSocket 连接已断开（对端关闭 / 管道破裂 / 超时）。

    2026-10-31：豆包 App 被关掉或刷新页面时，底层 socket 会以
    ``BrokenPipeError`` / ``ConnectionResetError`` / 超时等形式失效。原先这些
    异常未经包装就冒泡到 FastAPI 请求处理层，**直接杀死整个进程**（launchd
    ``KeepAlive=1`` 于是每 5-8 分钟重启一次，正在等待上游的其它渠道请求
    ——含 qoder——被一起切断，表现为「渠道测试不能」）。

    现在 socket 层的致命错误统一转成本异常，让调用方能识别「连接断了、
    该重连」并给出干净的渠道级错误，而不是拖垮整个网关。
    """


class _WS:
    """最小 WebSocket 客户端（纯 stdlib，RFC6455）。"""

    def __init__(self, host: str, port: int, path: str, timeout: float = 15.0):
        self._s = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._s.sendall(req.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            data += self._s.recv(4096)
        self._id = 0
        self._timeout = timeout

    def send(self, payload: bytes | str, opcode: int = 0x1) -> None:
        if isinstance(payload, str):
            payload = payload.encode()
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        frame = bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self._s.sendall(frame)
        except OSError as e:
            # 对端已关闭（豆包 App 退出/刷新）：BrokenPipeError 是 OSError 子类。
            # 必须转成 WSClosed 而不是裸抛，否则会一路冒泡杀掉网关进程。
            raise WSClosed(f"websocket send failed: {e}") from e

    def _read_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            c = self._s.recv(n - len(data))
            if not c:
                raise EOFError("websocket closed")
            data += c
        return data

    def recv_frame(self) -> tuple[int, bytes]:
        b1 = self._read_exact(2)
        length = b1[1] & 0x7F
        opcode = b1[0] & 0x0F
        masked = (b1[1] & 0x80) != 0
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def cmd(self, method: str, params: dict[str, Any] | None = None,
            timeout: float = 20.0) -> dict[str, Any]:
        self._id += 1
        msg: dict[str, Any] = {"id": self._id, "method": method}
        if params:
            msg["params"] = params
        try:
            self._s.settimeout(timeout)
            self.send(json.dumps(msg))
            while True:
                op, payload = self.recv_frame()
                if op == 0x9:  # ping
                    self.send(payload, opcode=0xA)  # pong
                    continue
                if op == 0x1:  # text
                    m = json.loads(payload)
                    if m.get("id") == self._id:
                        return m
        except OSError as e:
            # 读侧同样可能失效：超时(socket.timeout 是 OSError 子类)、
            # ConnectionReset 等。统一成 WSClosed，交给上层重连。
            raise WSClosed(f"websocket {method} failed: {e}") from e
        except EOFError as e:
            raise WSClosed(f"websocket closed during {method}") from e

    def close(self) -> None:
        try:
            self._s.close()
        except Exception:
            pass

