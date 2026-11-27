"""qoder 上游「竞品身份声明」中性化（``provider._neutralize_agent_signature``）。

锁住一个**表现极不直观**的上游行为（2026-11-27 实测）：

    system prompt 里带 dsh 的 ``You are an AI agent powered by DeepSeek Harness.``
    → 上游不报错、不返 4xx，而是**长时间无首字节**（实测 200s 仍无响应）；
    把这一句换成中性占位后，同一个请求 12.6s 正常出流。

为什么必须单独立测试而不是并进 codebuddy 那份：两个通道对**同一个签名**的
反应不同（那边 400 / code 11128 快速拒，这边软性挂起），而且**扫描范围也不同**
——codebuddy 只查「每个 text 块的首段」，qoder 实测是「**首条** system/developer
整条都扫，后续 system 不管」。照抄 codebuddy 的判据会漏掉「签名出现在
``# Role\\n\\n`` 之后」这类形态，那正是 dsh 真实发出的形状。

⚠ 改动扫描范围前请先对真实上游重跑矩阵（见下方 ``实测矩阵``），不要凭
「看起来更严格」推理——历史上 codebuddy 那边两次都栽在「只覆盖了当时实测到
的那一种形态」。

运行：
    .venv/bin/python -m pytest tests/test_qoder_agent_signature.py -v
"""
from __future__ import annotations

import pytest

from buddy_proxy.qoder.provider import (
    _AGENT_SIGNATURE_MASK,
    _neutralize_agent_signature,
)

DSH = "You are an AI agent powered by DeepSeek Harness."
MASK = _AGENT_SIGNATURE_MASK

#: 实测矩阵（2026-11-27，qoder 通道，hungerg / qwen3.8-max，流式）：
#:
#: ==========================================================  ======
#: 签名所在位置                                                结果
#: ==========================================================  ======
#: 首条 system，签名在开头                                     挂住
#: 首条 system，签名在 ``# Role\\n\\n`` 之后                     挂住
#: 首条 system，签名在普通前言之后                              挂住
#: 首条 developer                                              挂住
#: 第二条 system（首条干净）                                    200
#: assistant / user 消息                                       200
#: ==========================================================  ======


def _msgs(*pairs):
    return [{"role": r, "content": c} for r, c in pairs]


def test_signature_at_start_is_masked():
    """dsh 真实形状：签名永远是 system prompt 的第一段。"""
    out = _neutralize_agent_signature(_msgs(("system", DSH), ("user", "hi")))
    assert out[0]["content"] == MASK
    assert out[1]["content"] == "hi"


def test_signature_after_preamble_is_masked():
    """codebuddy 那边只查首段，这条形态在 qoder 必须也命中。

    实测上游对「签名不在首段」同样挂住——照抄 codebuddy 的
    ``_FIRST_SEGMENT_LEAD`` 首段判据会漏掉它，而 dsh 的 system prompt
    恰好是「一段身份声明 + 若干行为指令」，签名在首段，但用户自定义
    persona 时可能被顶到后面。
    """
    content = "# Role\n\nYou are a coding assistant.\n\n" + DSH + "\n\nBe nice."
    out = _neutralize_agent_signature(_msgs(("system", content), ("user", "hi")))
    assert DSH not in out[0]["content"]
    assert MASK in out[0]["content"]
    # 前后文必须原样保留——掩码不是整段替换。
    assert out[0]["content"].startswith("# Role")
    assert out[0]["content"].endswith("Be nice.")


def test_second_system_block_is_left_alone():
    """第二条 system 上游照常放行，改了就是平白篡改用户 prompt。"""
    original = _msgs(
        ("system", "You are helpful."),
        ("user", "hi"),
        ("system", DSH),
    )
    out = _neutralize_agent_signature(original)
    assert out is original, "未命中首条时不该产生新对象"
    assert out[2]["content"] == DSH


def test_user_and_assistant_messages_are_not_scanned():
    """user / assistant 里出现签名上游放行（实测 200），不该动。"""
    original = _msgs(
        ("system", "You are helpful."),
        ("user", DSH),
        ("assistant", DSH),
    )
    out = _neutralize_agent_signature(original)
    assert out is original
    assert out[1]["content"] == DSH
    assert out[2]["content"] == DSH


def test_developer_role_is_scanned():
    """dsh 0.2.2 起把 system prompt 发成 developer，同样会挂住。"""
    out = _neutralize_agent_signature(_msgs(("developer", DSH), ("user", "hi")))
    assert out[0]["content"] == MASK


def test_content_blocks_only_text_is_masked():
    """content blocks 形态：只改 text 块，图片块原样保留。"""
    img = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}
    out = _neutralize_agent_signature([
        {"role": "system", "content": [
            {"type": "text", "text": DSH},
            img,
        ]},
        {"role": "user", "content": "hi"},
    ])
    blocks = out[0]["content"]
    assert blocks[0]["text"] == MASK
    assert blocks[1] == img


def test_clean_messages_return_same_object():
    """未命中的常见路径必须零拷贝：这条防线不该给每个请求加开销。"""
    original = _msgs(("system", "You are helpful."), ("user", "hi"))
    assert _neutralize_agent_signature(original) is original


def test_non_list_input_passes_through():
    """防御性：messages 不是 list 时原样返回，不抛异常。"""
    for value in (None, "str", 42, {"role": "system"}):
        assert _neutralize_agent_signature(value) is value


def test_empty_messages():
    empty: list = []
    assert _neutralize_agent_signature(empty) is empty


def test_no_system_message_at_all():
    """只有 user 消息的请求不该被误改。"""
    original = _msgs(("user", DSH))
    assert _neutralize_agent_signature(original) is original


def test_build_upstream_applies_mask():
    """端到端：``_build_upstream`` 必须真的把掩码写进出站体。

    这条是防「函数写好了但没接上」——接漏了的表现就是「什么都没发生」，
    而单元测试全绿。
    """
    from buddy_proxy.qoder.catalog import Catalog
    from buddy_proxy.qoder.config import REGIONS
    from buddy_proxy.qoder.provider import QoderProvider

    body = {
        "model": "hungerg",
        "messages": [{"role": "system", "content": DSH},
                     {"role": "user", "content": "hi"}],
    }
    provider = QoderProvider.__new__(QoderProvider)
    upstream = provider._build_upstream(body, "hungerg", Catalog(REGIONS["cn"]))
    assert DSH not in upstream["messages"][0]["content"]
    assert MASK in upstream["messages"][0]["content"]


def test_build_upstream_masks_after_developer_demotion():
    """developer 被降级成 system 之后仍要过掩码。

    两个修法顺序不能反：``_normalize_message`` 先把 developer 改写成
    system，掩码再看。若掩码排在前面，它看到的还是 developer —— 虽然
    本实现两种 role 都扫，结果一样，但这条测试锁住「接在改写之后」这个
    位置，防止后人把两行对调后，在只扫 system 的将来版本里静默失效。
    """
    from buddy_proxy.qoder.catalog import Catalog
    from buddy_proxy.qoder.config import REGIONS
    from buddy_proxy.qoder.provider import QoderProvider

    body = {
        "model": "hungerg",
        "messages": [{"role": "developer", "content": DSH},
                     {"role": "user", "content": "hi"}],
    }
    provider = QoderProvider.__new__(QoderProvider)
    upstream = provider._build_upstream(body, "hungerg", Catalog(REGIONS["cn"]))
    assert upstream["messages"][0]["role"] == "system"
    assert DSH not in upstream["messages"][0]["content"]
    assert MASK in upstream["messages"][0]["content"]
