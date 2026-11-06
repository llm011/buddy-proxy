"""Qoder COSY 签名（纯 Python 实现，零新增依赖）。

Qoder 的 ``/algo/**`` 面要求 ``Authorization: Bearer COSY.<payload>.<sig>``
外加一族 ``Cosy-*`` 头。网上能跑的第三方实现都得在运行时加载厂商的
``qoder_auth.wasm``（298KB）来算签名；本模块是**纯 Python 复刻**，不依赖
任何厂商二进制，也不需要 ``cryptography`` / ``pycryptodome``——AES 复用
``buddy_proxy.core.aes_pure``（与 Trae 共用的纯 Python 实现），RSA 用 ``pow()``
直接算（e=65537）。

线缆形态（逐字段实测校准，2026-09）：

1. **body 编码**  ``enc = rearrange(sub_b64(plaintext))``

   - ``b64 = base64(utf-8 body)``（标准字母表、带 ``=`` 填充）
   - ``sub_b64``：标准字母表逐位映射到自定义表（``A``→``_``、``B``→``d``…），
     ``=`` 映射成 ``$``
   - ``rearrange``：设 ``n = len``、``a = n // 3``，把字符串切成
     ``[0:a] [a:n-a] [n-a:n]`` 后重排为 ``[n-a:n] + [a:n-a] + [0:a]``

2. **身份载荷** ``info``：随机 16 字节 ASCII key 作为 AES-128-CBC 的
   **key 与 iv**（同值），加密身份 JSON 后 base64。

3. **``Cosy-Key``**：上面那 16 字节 key 用 RSA PKCS#1 v1.5 加密后 base64。

4. **签名**：

   .. code-block:: text

       sig = md5(payload_b64 \n cosy_key \n ts \n enc_body \n sig_path)

   其中 ``sig_path`` 是去掉 ``/algo`` 前缀的**纯路径**——带上 ``/algo``
   或不带 query 都会得到 ``403 {"code":"101","message":"Signature invalid"}``。
   这是最容易踩的坑。

5. **``Cosy-User`` 头必填**（缺了同样 403），值为 uid。

签名算法与 ``payload`` 结构对**全球版与 CN 版通用**，只有域名不同。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from buddy_proxy.core.aes_pure import _encrypt_block, _key_expansion

from .config import (
    COSY_RSA_PUBLIC_KEY_PEM,
    COSY_VERSION,
    CUSTOM_B64_ALPHABET,
    STD_B64_ALPHABET,
)

log = logging.getLogger(__name__)

#: 标准字母表 -> 自定义字母表的逐位映射（``=`` 单独映射到 ``$``）。
_B64_MAP = {STD_B64_ALPHABET[i]: CUSTOM_B64_ALPHABET[i] for i in range(64)}
_B64_MAP["="] = "$"
#: 自定义字母表 -> 标准字母表的反查（解码用，主要供调试与单测）。
_B64_UNMAP = {v: k for k, v in _B64_MAP.items()}


def _rsa_public_numbers(pem: str) -> tuple[int, int]:
    """从 PEM 里取出 ``(n, e)``（PKCS#1 v1.5 只需要这两个数）。"""
    body = "".join(
        line.strip() for line in pem.splitlines() if line and "-----" not in line
    )
    der = base64.b64decode(body)
    # SubjectPublicKeyInfo ::= SEQ { AlgorithmIdentifier, BIT STRING { RSAPublicKey } }
    # RSAPublicKey ::= SEQ { INTEGER n, INTEGER e }
    idx = 0

    def read_len(buf: bytes, pos: int) -> tuple[int, int]:
        length = buf[pos]
        pos += 1
        if length & 0x80:
            count = length & 0x7F
            length = int.from_bytes(buf[pos : pos + count], "big")
            pos += count
        return length, pos

    def read_tlv(buf: bytes, pos: int, tag: int) -> tuple[bytes, int]:
        if buf[pos] != tag:
            raise ValueError(f"DER 结构异常：期望 tag 0x{tag:02x}，实得 0x{buf[pos]:02x}")
        length, pos = read_len(buf, pos + 1)
        return buf[pos : pos + length], pos + length

    seq, _ = read_tlv(der, 0, 0x30)
    _, pos = read_tlv(seq, 0, 0x30)          # AlgorithmIdentifier：跳过
    bits, _ = read_tlv(seq, pos, 0x03)
    rsa = bits[1:]                            # BIT STRING 首个字节是 unused-bits 计数
    rsa_seq, _ = read_tlv(rsa, 0, 0x30)
    n_bytes, pos = read_tlv(rsa_seq, 0, 0x02)
    e_bytes, _ = read_tlv(rsa_seq, pos, 0x02)
    return int.from_bytes(n_bytes, "big"), int.from_bytes(e_bytes, "big")


_RSA_N, _RSA_E = _rsa_public_numbers(COSY_RSA_PUBLIC_KEY_PEM)
_RSA_BYTES = (_RSA_N.bit_length() + 7) // 8


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    pad = block - len(data) % block
    return data + bytes([pad]) * pad


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-CBC 加密（ECB 单块由 ``aes_pure`` 提供，这里做 CBC 链接 + PKCS#7）。"""
    round_keys = _key_expansion(key)
    padded = _pkcs7_pad(data)
    out = bytearray()
    prev = iv
    for i in range(0, len(padded), 16):
        block = bytes(a ^ b for a, b in zip(padded[i : i + 16], prev))
        enc = _encrypt_block(block, round_keys)
        out += enc
        prev = enc
    return bytes(out)


def rsa_encrypt(plain: bytes) -> bytes:
    """RSA PKCS#1 v1.5 加密（公钥只有 n/e，直接模幂即可）。"""
    if len(plain) > _RSA_BYTES - 11:
        raise ValueError("RSA 明文过长")
    # EB = 0x00 || 0x02 || 非零随机填充 || 0x00 || M
    pad_len = _RSA_BYTES - 3 - len(plain)
    pad = bytes(secrets.randbelow(255) + 1 for _ in range(pad_len))
    eb = b"\x00\x02" + pad + b"\x00" + plain
    m = int.from_bytes(eb, "big")
    return pow(m, _RSA_E, _RSA_N).to_bytes(_RSA_BYTES, "big")


def encode_body(plaintext: str) -> str:
    """COSY body 编码：``rearrange(sub_b64(plaintext))``。"""
    std = base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
    mapped = "".join(_B64_MAP[c] for c in std)
    n = len(mapped)
    a = n // 3
    return mapped[n - a :] + mapped[a : n - a] + mapped[:a]


def decode_body(encoded: str) -> str:
    """``encode_body`` 的逆运算（主要供单测与排障）。"""
    n = len(encoded)
    a = n // 3
    p3 = encoded[: a]           # 原 [n-a:n]
    p2 = encoded[a : n - a]     # 原 [a:n-a]
    p1 = encoded[n - a :]       # 原 [0:a]
    mapped = p1 + p2 + p3
    std = "".join(_B64_UNMAP.get(c, c) for c in mapped)
    pad = len(std) % 4
    if pad:
        std += "=" * (4 - pad)
    return base64.b64decode(std).decode("utf-8")


def signature_path(url: str) -> str:
    """签名用的路径：**去掉 ``/algo`` 前缀**，且不带 query。

    带 ``/algo`` 会被服务端拒绝（403 code 101）——这是实测确认的根因。
    """
    path = urlparse(url).path
    return path[5:] if path.startswith("/algo") else path


def build_payload(info_b64: str, cosy_version: str = COSY_VERSION) -> str:
    """构造 ``Authorization`` 里的 payload JSON（键序与官方一致）。"""
    return json.dumps(
        {
            "version": "v1",
            "requestId": str(uuid.uuid4()),
            "info": info_b64,
            "cosyVersion": cosy_version,
            "ideVersion": "",
        },
        separators=(",", ":"),
    )


def build_identity(
    uid: str,
    token: str,
    name: str = "",
    email: str = "",
    extra: dict[str, Any] | None = None,
) -> tuple[str, bytes]:
    """加密身份信息，返回 ``(info_b64, aes_key)``。"""
    key = uuid.uuid4().hex[:16].encode("ascii")
    identity: dict[str, Any] = {
        "uid": uid,
        "security_oauth_token": token,
        "name": name,
        "aid": "",
        "email": email,
    }
    if extra:
        identity.update(extra)
    raw = json.dumps(identity, separators=(",", ":"), ensure_ascii=False)
    cipher = aes_cbc_encrypt(key, key, raw.encode("utf-8"))
    return base64.b64encode(cipher).decode("ascii"), key


def sign(
    url: str,
    body: str,
    uid: str,
    token: str,
    machine_id: str,
    *,
    name: str = "",
    email: str = "",
    cosy_version: str = COSY_VERSION,
    client_type: str = "5",
    model_key: str | None = None,
    model_source: str = "system",
    encode: bool = True,
) -> tuple[str, dict[str, str]]:
    """对一次请求签名。

    参数：
    - ``url``：完整请求 URL（签名会用它推导 ``sig_path``）
    - ``body``：**明文** JSON 字符串（本函数内部编码）
    - ``uid`` / ``token`` / ``machine_id``：身份三要素
    - ``encode=False``：``/algo`` 的 GET 类请求，body 原样为空字符串

    返回 ``(encoded_body, headers)``——``encoded_body`` 需要作为请求体原样发出。
    """
    info_b64, aes_key = build_identity(uid, token, name=name, email=email)
    cosy_key = base64.b64encode(rsa_encrypt(aes_key)).decode("ascii")
    payload_b64 = base64.b64encode(build_payload(info_b64, cosy_version).encode()).decode(
        "ascii"
    )

    ts = str(int(time.time()))
    enc_body = encode_body(body) if encode else ""
    sig = hashlib.md5(
        "\n".join([payload_b64, cosy_key, ts, enc_body, signature_path(url)]).encode()
    ).hexdigest()

    headers = {
        "Authorization": f"Bearer COSY.{payload_b64}.{sig}",
        "Content-Type": "application/json",
        "Cosy-Version": cosy_version,
        "Cosy-ClientType": client_type,
        "Cosy-Data-Policy": "agree",
        "Cosy-Date": ts,
        "Cosy-Key": cosy_key,
        "Cosy-MachineId": machine_id,
        "Cosy-MachineToken": machine_id,
        "Cosy-MachineType": client_type,
        "Cosy-Scene": "assistant",
        "Cosy-User": uid,
        "Cosy-Business-Product": "cli",
        "Cosy-Business-Type": "agent",
        "Login-Version": "v2",
    }
    if model_key:
        headers["X-Model-Key"] = model_key
        headers["X-Model-Source"] = model_source
    return enc_body, headers
