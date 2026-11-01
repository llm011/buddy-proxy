"""DuMate bceConsole cookie 解密（纯标准库 AES-256-GCM）。

签到 / 额度等管理接口走 ``https://console.bce.baidu.com/api/dumate/*``，用的是
**百度云登录 cookie**（不是 inapp key 通道）。cookie 存在
``~/Library/Application Support/qianfan-desktop-app/auth.json`` 的
``accountProfiles[N].encryptedCookies``，是 AES-256-GCM 密文：

    base64( iv(12) || authTag(16) || ciphertext )

密钥是同级 ``.cookie-key`` 的 32 字节。算法与 DuMate ``lib/crypto/cookie.js``
一致。这里用**纯标准库**实现 AES-256-GCM（不引 ``cryptography`` 新依赖）：
AES-256  ECB 原语 + GHASH 认证，AEAD 的拼接模式按 NIST SP 800-38D。

⚠️ 这套 cookie 是**登录态本身**：能签到、能查额度，等同半个账号。所以——

- 解密结果只在内存用，绝不写日志、绝不落盘；
- 拼 Cookie 请求头时才序列化，且只发往 ``console.bce.baidu.com``；
- csrftoken 取 ``bce-user-info`` cookie 的值（与 DuMate
  ``utils/cookie.js: extractCsrfToken`` 同一取法）。
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 纯标准库 AES-256-GCM（不引 cryptography）
# ---------------------------------------------------------------------------

def _aes256_encrypt_block(key: bytes, block: bytes) -> bytes:
    """AES-256 单块加密。标准库无 AES，这里用最小的 AES 实现（仅 ECB 原语）。"""
    # S-box
    _SBOX = [
        0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,
        0x76,0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,
        0x72,0xc0,0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,
        0xd8,0x31,0x15,0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,
        0xeb,0x27,0xb2,0x75,0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,
        0xb3,0x29,0xe3,0x2f,0x84,0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,
        0xbe,0x39,0x4a,0x4c,0x58,0xcf,0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,
        0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,
        0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,
        0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,0x60,0x81,0x4f,0xdc,0x22,0x2a,
        0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,0xe0,0x32,0x3a,0x0a,0x49,
        0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,0xe7,0xc8,0x37,0x6d,
        0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,0xba,0x78,0x25,
        0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,0x70,0x3e,
        0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,0xe1,
        0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
        0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,
        0x16,
    ]
    _RCON = [0x00,0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36]

    def _xtime(a: int) -> int:
        return ((a << 1) ^ 0x11b) & 0xff if a & 0x80 else (a << 1) & 0xff

    def _mul(a: int, b: int) -> int:
        r = 0
        while b:
            if b & 1:
                r ^= a
            a = _xtime(a)
            b >>= 1
        return r

    def _sub_word(w: int) -> int:
        return (_SBOX[(w >> 24) & 0xff] << 24 | _SBOX[(w >> 16) & 0xff] << 16
                | _SBOX[(w >> 8) & 0xff] << 8 | _SBOX[w & 0xff])

    def _rot_word(w: int) -> int:
        return ((w << 8) | (w >> 24)) & 0xffffffff

    nk, nb, nr = 8, 4, 14
    words = [int.from_bytes(key[4 * i:4 * i + 4], "big") for i in range(nk)]
    for i in range(nk, nb * (nr + 1)):
        t = words[i - 1]
        if i % nk == 0:
            t = _sub_word(_rot_word(t)) ^ (_RCON[i // nk] << 24)
        elif i % nk == 4:
            t = _sub_word(t)
        words.append(words[i - nk] ^ t)
    round_keys = [
        [words[nb * r + c] for c in range(nb)] for r in range(nr + 1)
    ]

    def _add_rk(s, rk):
        for c in range(4):
            w = rk[c]
            s[0][c] ^= (w >> 24) & 0xff
            s[1][c] ^= (w >> 16) & 0xff
            s[2][c] ^= (w >> 8) & 0xff
            s[3][c] ^= w & 0xff

    def _sub_bytes(s):
        for r in range(4):
            for c in range(4):
                s[r][c] = _SBOX[s[r][c]]

    def _shift_rows(s):
        for r in range(1, 4):
            s[r] = s[r][r:] + s[r][:r]

    def _mix_columns(s):
        for c in range(4):
            a0, a1, a2, a3 = s[0][c], s[1][c], s[2][c], s[3][c]
            s[0][c] = _mul(a0, 2) ^ _mul(a1, 3) ^ a2 ^ a3
            s[1][c] = a0 ^ _mul(a1, 2) ^ _mul(a2, 3) ^ a3
            s[2][c] = a0 ^ a1 ^ _mul(a2, 2) ^ _mul(a3, 3)
            s[3][c] = _mul(a0, 3) ^ a1 ^ a2 ^ _mul(a3, 2)

    s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
    _add_rk(s, round_keys[0])
    for rnd in range(1, nr):
        _sub_bytes(s)
        _shift_rows(s)
        _mix_columns(s)
        _add_rk(s, round_keys[rnd])
    _sub_bytes(s)
    _shift_rows(s)
    _add_rk(s, round_keys[nr])
    return bytes(s[r][c] for c in range(4) for r in range(4))


def _ghash(h: bytes, aad: bytes, ct: bytes) -> bytes:
    """GHASH 认证（标准 GCM 多项式）。"""
    def _mul_x(y: int, x: int) -> int:
        z = 0
        v = y
        for i in range(128):
            if (x >> (127 - i)) & 1:
                z ^= v
            if v & 1:
                v = (v >> 1) ^ 0xe1000000000000000000000000000000
            else:
                v >>= 1
        return z

    h_int = int.from_bytes(h, "big")
    y = 0
    def _feed(data: bytes):
        nonlocal y
        for i in range(0, len(data), 16):
            blk = data[i:i + 16].ljust(16, b"\x00")
            y = _mul_x(y ^ int.from_bytes(blk, "big"), h_int)
    _feed(aad)
    _feed(ct)
    _feed((len(aad) * 8).to_bytes(8, "big") + (len(ct) * 8).to_bytes(8, "big"))
    return y.to_bytes(16, "big")


def aes256_gcm_decrypt(key: bytes, iv: bytes, ct: bytes, tag: bytes,
                       aad: bytes = b"") -> bytes:
    """AES-256-GCM 解密 + 认证。认证失败抛 ValueError。"""
    h = _aes256_encrypt_block(key, b"\x00" * 16)
    # GCM 的 J0：96-bit IV 时 J0 = IV || 0x00000001（补 4 字节计数器初值）
    j0 = iv + b"\x00\x00\x00\x01"
    # CTR 解密（计数器从 inc32(J0) 开始，即末 4 字节 +1）
    ks = b""
    for i in range(0, len(ct), 16):
        ctr = (int.from_bytes(j0, "big") + 1 + i // 16).to_bytes(16, "big")
        ks += _aes256_encrypt_block(key, ctr)
    pt = bytes(a ^ b for a, b in zip(ct, ks))
    # 认证
    s = _ghash(h, aad, ct)
    expect_tag = bytes(a ^ b for a, b in zip(s, _aes256_encrypt_block(key, j0)))
    if expect_tag != tag:
        raise ValueError("AES-GCM auth tag mismatch")
    return pt


# ---------------------------------------------------------------------------
# DuMate auth.json cookie 解密
# ---------------------------------------------------------------------------

def _user_data_dir() -> str:
    """DuMate App 的用户数据目录（``DUMATE_USER_DATA_DIR`` 可覆盖；测试隔离点）。

    与其它通道的 ``*_STATE_DIR`` 同款：测试必须能把 auth.json/.cookie-key 指到
    tmp，否则任何触达 dumate 凭据的用例都会读真实 App 目录、甚至打上游（本机
    装了 App 时）。生产不带这个环境变量，照常走 App 的默认位置。
    """
    env = os.environ.get("DUMATE_USER_DATA_DIR", "").strip()
    if env:
        return env
    return os.path.expanduser(
        "~/Library/Application Support/qianfan-desktop-app"
    )


def _load_key() -> bytes | None:
    key_path = os.path.join(_user_data_dir(), ".cookie-key")
    try:
        with open(key_path, "rb") as f:
            key = f.read()
        return key if len(key) == 32 else None
    except OSError:
        return None


def decrypt_cookies() -> list[dict[str, Any]] | None:
    """解密 auth.json 的 encryptedCookies，返回 cookie 数组；失败返回 None。"""
    key = _load_key()
    if key is None:
        return None
    auth_path = os.path.join(_user_data_dir(), "auth.json")
    try:
        with open(auth_path, encoding="utf-8") as f:
            auth = json.load(f)
    except (OSError, ValueError):
        return None
    profiles = auth.get("accountProfiles") or []
    enc = ""
    active = auth.get("activeProfileId")
    for prof in profiles:
        if prof.get("profileId") == active:
            enc = prof.get("encryptedCookies") or ""
            break
    if not enc and profiles:
        enc = profiles[0].get("encryptedCookies") or ""
    if not enc:
        return None
    try:
        blob = base64.b64decode(enc)
        iv, tag, ct = blob[:12], blob[12:28], blob[28:]
        pt = aes256_gcm_decrypt(key, iv, ct, tag)
        cookies = json.loads(pt.decode("utf-8"))
        return cookies if isinstance(cookies, list) else None
    except Exception as exc:  # noqa: BLE001 - 解密失败按未登录处理
        log.debug("dumate cookie decrypt failed: %s", type(exc).__name__)
        return None


@dataclass
class BceConsoleAuth:
    """一份可直接用的 bceConsole 凭据（cookie header + csrftoken）。"""

    cookie_header: str
    csrf_token: str

    def headers(self) -> dict[str, str]:
        return {
            "Cookie": self.cookie_header,
            "X-Requested-With": "XMLHttpRequest",
            "csrftoken": self.csrf_token,
        }


def resolve_bceconsole_auth() -> BceConsoleAuth | None:
    """解密并组装 bceConsole 请求头；无可用登录态时返回 None。

    csrftoken = ``bce-user-info`` cookie 的值（去引号），与 DuMate
    ``extractCsrfToken`` 同口径。
    """
    cookies = decrypt_cookies()
    if not cookies:
        return None
    pairs: list[str] = []
    csrf = ""
    seen: set[str] = set()
    for c in cookies:
        name, value = c.get("name"), c.get("value")
        if not name or value is None or name in seen:
            continue  # 去重：auth.json 里同名 cookie 会出现多次（hostOnly 变体）
        seen.add(name)
        # 值里的 ; 会截断 Cookie 头，按规范转义成 %3B（服务器端取用时再解码）
        safe = str(value).replace(";", "%3B")
        pairs.append(f"{name}={safe}")
        if name == "bce-user-info":
            csrf = str(value).strip("'\"")
    if not pairs:
        return None
    return BceConsoleAuth(cookie_header="; ".join(pairs), csrf_token=csrf)
