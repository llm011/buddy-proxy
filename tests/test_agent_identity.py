"""竞品 Agent 身份声明脱敏（``core/agent_identity.py``）的跨通道防线。

锁住两类上游行为（同一个根因，两种表现）：

- **codebuddy**：``400 / code 11128``，秒回，真因不可见；
- **qoder**：**长时间无首字节**（软性挂起），客户端只看到自己卡死、没有任何
  报错。2026-11-27 的 dsh + hungerg 事故就卡在这里，排了一轮无效方向
  （工具数、上下文长度、max_tokens 逐个二分）才定位到 system prompt 里的一句话。

## 这些用例保护的是什么

不是「函数能替换字符串」——那太容易写对。是**判据的边界**：

1. **必须带逗号**。``You are Codex`` 上游放行，``You are Codex,`` 挂住。
   少了逗号，要么漏掉真实客户端（Codex CLI 发的就是带逗号那句），要么
   把大量正常 prompt 也改掉。
2. **大小写敏感**。全大写/全小写都放行，只有原样大小写会拦。写成
   ``.lower() in text`` 看起来「更宽容」，实际会把放行的请求也篡改。
3. **只扫首条 system/developer**。第二条 system、assistant、user 里出现同一
   签名实测放行，扩大扫描面就是平白篡改用户 prompt。
4. **不能变成「You are X,」通用规则**。实测 Cursor / Windsurf / Copilot /
   Kimi / Gemini / Qwen / Trae / OpenCode / Aider / Cline 一律 200 放行。
   写成通用正则会把这些正常客户端的 persona 全部改掉。

## 加新 marker 之前

对真实上游跑一遍矩阵（见 ``core/agent_identity.py`` docstring 里的表），
**只加实测确认会拦的**。未测过的名字加进来只会平白篡改用户 prompt——这正是
codebuddy 那边两次栽跟头的模式（「只覆盖了当时实测到的那一种形态」）。

运行：
    .venv/bin/python -m pytest tests/test_agent_identity.py -v
"""
from __future__ import annotations

import pytest

from buddy_proxy.core.agent_identity import (
    COMPETITOR_IDENTITY_MARKERS,
    IDENTITY_MASK,
    neutralize_agent_identity,
    neutralize_identity_text,
)

DSH = "You are an AI agent powered by DeepSeek Harness."
CLAUDE = "You are Claude Code, Anthropic's official CLI for Claude."
CLAUDE_SDK = ("You are Claude Code, Anthropic's official CLI for Claude, "
              "running within the Claude Agent SDK.")
CODEX = "You are Codex, an agent based on GPT-6."
KIMI = "You are Kimi Code CLI, an interactive general AI agent running on a user's computer."
MASK = IDENTITY_MASK


def _msgs(*pairs):
    return [{"role": r, "content": c} for r, c in pairs]


# ---------------------------------------------------------------------------
# 已知会被拦的客户端，各自都要命中
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("signature", [DSH, CLAUDE, CLAUDE_SDK, CODEX, KIMI])
def test_known_competitor_signatures_are_masked(signature):
    """dsh / Claude Code / Codex / Kimi Code CLI 的 system 首句都必须被换掉。

    Codex 与 Claude 是 2026-11-27 补测发现的：原先只挡 dsh 一句，于是这两个
    主流客户端的请求在 qoder 上**全部挂死**，且看起来像「模型坏了」。
    """
    out = neutralize_agent_identity(_msgs(("system", signature), ("user", "hi")))
    assert IDENTITY_MASK in out[0]["content"]
    for marker in COMPETITOR_IDENTITY_MARKERS:
        assert marker not in out[0]["content"]
    assert out[1]["content"] == "hi"


# ---------------------------------------------------------------------------
# 判据边界：逗号 + 大小写（改这两条之前先重跑上游矩阵）
# ---------------------------------------------------------------------------

def test_comma_is_required():
    """``You are Codex`` 上游放行，``You are Codex,`` 才挂。

    少了逗号的匹配串会漏掉真实客户端（它们发的都带逗号），同时会把
    「You are Codex is a name」这类正常文本也改掉。
    """
    for text in ("You are Codex", "You are Claude Code"):
        out = neutralize_agent_identity(_msgs(("system", text)))
        assert out[0]["content"] == text, f"{text!r} 不该被改（上游放行）"


def test_case_sensitive():
    """全大写/全小写上游都放行，只有原样大小写会拦。

    用 ``.lower()`` 之类的「更宽容」写法，会把本来能通的请求也篡改——
    改 prompt 是有代价的（用户可能真的在讨论这些名字）。

    ⚠ 断言必须看 ``neutralize_identity_text`` 返回的 **hit 标记**，不能只看
    「内容没变」：一个「检测用小写、替换用原样」的实现（``marker.lower() in
    text.lower()`` 配 ``text.replace(marker, MASK)``）会报命中却什么都不替换，
    内容照样不变——只断言内容的话，这种实现能骗过测试。实测踩过。
    """
    variants = [
        "you are codex, an agent",
        "YOU ARE CODEX, AN AGENT",
        "you are claude code, anthropic's official cli for claude.",
        "YOU ARE CLAUDE CODE, ANTHROPIC'S OFFICIAL CLI FOR CLAUDE.",
        "you are an ai agent powered by deepseek harness.",
    ]
    for text in variants:
        patched, hit = neutralize_identity_text(text)
        assert hit is None, f"{text!r} 上游放行，不该被判为命中"
        assert patched == text
        # 整条链路也要保持一致：不该产生新对象。
        original = _msgs(("system", text))
        assert neutralize_agent_identity(original) is original


def test_generic_you_are_x_is_not_masked():
    """不能变成「You are X,」通用规则。

    实测这些**全部 200 放行**（2026-11-27，qoder/hungerg）。写成通用正则
    会把正常客户端的 persona 改掉，属于「防过头」。
    """
    benign = [
        "You are Cursor, an AI code editor.",
        "You are Windsurf, an AI coding assistant.",
        "You are Copilot, an AI assistant.",
        "You are Kimi, an AI assistant.",
        "You are Gemini, an AI assistant.",
        "You are Qwen, an AI assistant.",
        "You are Trae, an AI assistant.",
        "You are OpenCode, the best coding agent on the planet.",
        "You are Aider, a coding assistant.",
        "You are Cline, a coding agent.",
        "You are Foobar, a helpful assistant.",
    ]
    for text in benign:
        out = neutralize_agent_identity(_msgs(("system", text)))
        assert out[0]["content"] == text, f"{text!r} 不该被改（上游放行）"


# ---------------------------------------------------------------------------
# 扫描范围
# ---------------------------------------------------------------------------

def test_signature_after_preamble_is_masked():
    """签名不在首段也要命中：实测 ``# Role\\n\\n`` 之后同样挂住。

    codebuddy 那边的判据是「去掉前导空格后的**首段**」，套到这里会漏。
    """
    content = "# Role\n\nYou are a coding assistant.\n\n" + DSH + "\n\nBe nice."
    out = neutralize_agent_identity(_msgs(("system", content)))
    assert DSH not in out[0]["content"]
    assert MASK in out[0]["content"]
    assert out[0]["content"].startswith("# Role")
    assert out[0]["content"].endswith("Be nice.")


def test_second_system_block_is_left_alone():
    """第二条 system 上游照常放行（实测 200），改了就是平白篡改用户 prompt。"""
    original = _msgs(
        ("system", "You are helpful."),
        ("user", "hi"),
        ("system", DSH),
    )
    out = neutralize_agent_identity(original)
    assert out is original, "未命中首条时不该产生新对象"
    assert out[2]["content"] == DSH, (
        "第二条 system 上游照常放行（实测 200），扩大扫描面只会平白篡改用户 prompt"
    )


def test_user_and_assistant_messages_are_not_scanned():
    original = _msgs(
        ("system", "You are helpful."),
        ("user", DSH),
        ("assistant", DSH),
    )
    out = neutralize_agent_identity(original)
    assert out is original
    assert out[1]["content"] == DSH
    assert out[2]["content"] == DSH


def test_developer_role_is_scanned():
    """dsh 0.2.2 起把 system prompt 发成 developer，同样会被拦。"""
    out = neutralize_agent_identity(_msgs(("developer", DSH)))
    assert out[0]["content"] == MASK


# ---------------------------------------------------------------------------
# content blocks
# ---------------------------------------------------------------------------

def test_content_blocks_only_text_is_masked():
    img = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}
    out = neutralize_agent_identity([
        {"role": "system", "content": [
            {"type": "text", "text": DSH},
            img,
        ]},
        {"role": "user", "content": "hi"},
    ])
    blocks = out[0]["content"]
    assert blocks[0]["text"] == MASK
    assert blocks[1] == img


def test_content_blocks_signature_in_later_block():
    """命中的块排在干净块之后，仍必须被找到。

    这是 blocks 分支最容易写错的地方：按「取第一个 text 块判一下、不中就
    返回」（字符串分支的直觉）来写，签名在第二块会静默漏掉，上游照样挂住，
    而单测全绿。
    """
    out = neutralize_agent_identity([
        {"role": "system", "content": [
            {"type": "text", "text": "preamble"},
            {"type": "text", "text": DSH},
        ]},
    ])
    blocks = out[0]["content"]
    assert blocks[0]["text"] == "preamble", "干净块不该被改动"
    assert blocks[1]["text"] == MASK


# ---------------------------------------------------------------------------
# 零拷贝 / 健壮性
# ---------------------------------------------------------------------------

def test_clean_messages_return_same_object():
    """未命中的常见路径必须零拷贝：这条防线不该给每个请求加开销。"""
    original = _msgs(("system", "You are helpful."), ("user", "hi"))
    assert neutralize_agent_identity(original) is original


def test_non_list_input_passes_through():
    for value in (None, "str", 42, {"role": "system"}):
        assert neutralize_agent_identity(value) is value


def test_empty_messages():
    empty: list = []
    assert neutralize_agent_identity(empty) is empty


def test_no_system_message_at_all():
    original = _msgs(("user", DSH))
    assert neutralize_agent_identity(original) is original


def test_idempotent():
    """重复执行结果不变（重试循环会多次经过同一条 body）。"""
    once = neutralize_agent_identity(_msgs(("system", DSH + "\n\nrest")))
    twice = neutralize_agent_identity(once)
    assert once[0]["content"] == twice[0]["content"]
    assert twice is once, "第二次不该再产生新对象"


def test_duplicate_signature_replaced_everywhere():
    out = neutralize_agent_identity(_msgs(("system", DSH + "\n\n" + DSH)))
    assert DSH not in out[0]["content"]
    assert out[0]["content"].count(MASK) == 2


def test_neutralize_identity_text_returns_hit_marker():
    """返回命中的 marker，供调用方记日志时说明「挡的是哪一条」。"""
    patched, hit = neutralize_identity_text(DSH)
    assert patched == MASK
    assert hit == DSH

    same, none = neutralize_identity_text("nothing here")
    assert none is None
    assert same == "nothing here"


# ---------------------------------------------------------------------------
# 掩码不能留「半句谜语」
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("signature", [CLAUDE, CLAUDE_SDK, CODEX, DSH])
def test_no_trailing_fragment_after_mask(signature):
    """替换后不能剩下半句自我描述。

    marker 只到逗号（逗号是触发条件），直接 ``text.replace`` 会留下：

        You are Claude Code, Anthropic's official CLI for Claude.
        → [redacted: client identity] Anthropic's official CLI for Claude.

    残留本身不再触发拦截，但它是一句没有主语的自我描述，会污染模型对自身身份
    的推断——实测 /v1/messages 的 thinking 里模型明确复述了这句残留
    （「weird developer message ends "..."」）。掩码的目的是「不告诉上游是谁」，
    不是「留半句谜语」。

    断言方式：掩码之后除了空白不该剩下任何东西（这四条都是「整条 system 就
    这一句」的形态）。
    """
    patched, _ = neutralize_identity_text(signature)
    assert patched.strip() == MASK, f"掩码后残留了 {patched.strip()[len(MASK):]!r}"


def test_tail_eating_stops_at_sentence_end():
    """只吃**同一句**，后面的真实指令必须原样保留。

    Codex 的 system 是「身份句 + 工作区说明」连在一起的：
    ``You are Codex, an agent based on GPT-6. You and the user share a workspace.``
    吃成整段就把工作区说明也删了，模型行为会变。
    """
    text = "You are Codex, an agent based on GPT-6. You and the user share a workspace."
    patched, _ = neutralize_identity_text(text)
    assert patched == MASK + " You and the user share a workspace."


def test_tail_eating_stops_at_newline():
    """换行是结构，不能被吃掉——否则两段会粘成一段。"""
    text = DSH + "\n\n# Role\n\nYou are a coding assistant."
    patched, _ = neutralize_identity_text(text)
    assert patched == MASK + "\n\n# Role\n\nYou are a coding assistant."


def test_tail_eating_does_not_touch_following_sentence():
    """签名在中间时，前文和后文都保留。"""
    text = "Preamble.\n" + CLAUDE + "\nSuffix instructions."
    patched, _ = neutralize_identity_text(text)
    assert patched == "Preamble.\n" + MASK + "\nSuffix instructions."


def test_self_terminated_marker_does_not_eat_next_sentence():
    """marker 自带句号时，**不能**再吃尾巴——否则会静默删掉真实指令。

    DSH 那条是 ``...DeepSeek Harness.``，自带句号，后面本来就是新的一句。
    实现里若无条件「吃到本句句末」，会把下一句也吞掉：

        You are an AI agent powered by DeepSeek Harness. You must always be polite.
        → [redacted: client identity]          ← 「polite」那句凭空消失

    这比留半句谜语更糟：它**静默改变模型行为**，而且没有任何日志能看出来。
    实测踩过（第一次写 tail-eating 时没区分「marker 是否已收句」）。
    """
    text = DSH + " You must always be polite. Be brief."
    patched, _ = neutralize_identity_text(text)
    assert patched == MASK + " You must always be polite. Be brief."
    assert "polite" in patched, "真实指令被吞掉了"


def test_self_terminated_marker_alone_is_fully_masked():
    """marker 自带句号且后面没内容时，掩码后不该剩下任何东西。"""
    patched, _ = neutralize_identity_text(DSH)
    assert patched == MASK


def test_multiple_distinct_markers_in_one_text():
    """同一段 system 里两个**不同** marker，都必须被换掉。

    命中一条就 ``return`` 的实现会漏掉第二条（实测踩过：``for marker: if in
    text: return _mask_one(...)``）。上游只要看到一条就拦，所以那种实现让整个
    改动对这种请求**完全无效**——而且日志里还写着「已中性化」，排查时会被
    这条日志误导。

    真实场景：用户把两个 CLI 的 prompt 拼在一起做对比，或编排层把 subagent
    的 persona 也塞进同一条 system。
    """
    text = CLAUDE + "\n\n" + CODEX
    patched, hit = neutralize_identity_text(text)
    assert patched == MASK + "\n\n" + MASK
    assert hit is not None
    for marker in COMPETITOR_IDENTITY_MARKERS:
        assert marker not in patched


def test_multiple_distinct_markers_end_to_end():
    """整条链路也要覆盖：不能只测纯函数。"""
    original = _msgs(("system", CLAUDE + "\n\n" + CODEX))
    out = neutralize_agent_identity(original)
    assert out is not original
    assert out[0]["content"] == MASK + "\n\n" + MASK


# ---------------------------------------------------------------------------
# 跨通道生效：这是本次重构的**核心目的**
# ---------------------------------------------------------------------------

def test_all_providers_get_masked_via_forward_chat():
    """脱敏必须在 ``forward_chat`` 公共入口，而不是某个 provider 内部。

    这是本次改动的全部意义：2026-11-27 之前只有 codebuddy 有防护，qoder 没有，
    于是**同一个 dsh 会话在两个通道上一个报 400、一个挂死**。逐个 provider
    加必然漏（新通道会忘、老通道会漂移），所以断言公共入口真的调用了它。

    用 monkeypatch 直接观察 ``forward_chat`` 的入参，不依赖任何具体 provider
    的实现细节——这样将来新增通道也自动被这条测试覆盖。
    """
    import asyncio
    from unittest.mock import patch

    from buddy_proxy.codebuddy_provider import forward as fwd

    seen: dict = {}

    async def fake_dispatch(state, provider_id, model, body, protocol, original):
        seen["body"] = body
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True})

    body = {
        "model": "hungerg",
        "messages": [{"role": "system", "content": CLAUDE},
                     {"role": "user", "content": "hi"}],
    }
    with patch.object(fwd, "_dispatch_once", fake_dispatch), \
         patch.object(fwd, "_forward_with_order", fake_dispatch), \
         patch.object(fwd, "get_state", lambda: type("S", (), {
             "providers": {}, "model_order": None, "default_provider": "codebuddy",
             "default_model": None,
         })()):
        asyncio.run(fwd.forward_chat(body, "openai"))

    assert seen, "forward_chat 没走到派发"
    sent = seen["body"]["messages"][0]["content"]
    assert CLAUDE not in sent, "公共入口没有做身份脱敏"
    assert MASK in sent


# ---------------------------------------------------------------------------
# 三种协议的转换结果都要能被公共层看到
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("protocol,convert,payload", [
    (
        "anthropic",
        lambda: __import__(
            "buddy_proxy.protocols.anthropic_adapter", fromlist=["x"]
        ).anthropic_request_to_chat,
        {"model": "m", "system": CLAUDE, "messages": [{"role": "user", "content": "hi"}]},
    ),
    (
        "responses",
        lambda: __import__(
            "buddy_proxy.protocols.responses_adapter", fromlist=["x"]
        ).responses_request_to_chat,
        {"model": "m", "instructions": CLAUDE, "input": [{"role": "user", "content": "hi"}]},
    ),
])
def test_other_protocols_expose_signature_as_first_system(protocol, convert, payload):
    """``/v1/messages`` 与 ``/v1/responses`` 转换后，签名必须落在**首条 system**。

    公共层只扫 `body["messages"]` 的首条 system/developer。这两条协议把身份
    放在别处（Anthropic 的 `system` 字段、Responses 的 `instructions` 字段），
    全靠 adapter 转成置顶的 system message 才能被看到。

    这条测试的价值不在「现在能过」——在于**adapter 改字段名时会立刻红**。
    漏掉的后果是上游挂死（无首字节、无报错），不是报错，线上极难定位；
    而现有测试只断言了公共入口调用了脱敏，没有断言转换后的形状。

    实测三条协议都通（2026-11-27，qoder/hungerg）：
    chat/completions 5.0s、messages 12.7s、responses 2.5s，均 200。
    """
    converted = convert()(payload)
    messages = converted["messages"]
    assert messages[0]["role"] == "system", (
        f"{protocol} adapter 没把身份放成首条 system，公共层会扫不到"
    )
    assert CLAUDE in messages[0]["content"]

    # 转换结果交给公共层，必须被换掉。
    out = neutralize_agent_identity(messages)
    assert CLAUDE not in out[0]["content"]
    assert MASK in out[0]["content"]


# ---------------------------------------------------------------------------
# Kimi：触发判据与另外三条**不同**（尾随空格，不是逗号）
# ---------------------------------------------------------------------------

def test_kimi_real_cli_prompt_is_masked():
    """Kimi Code CLI 2.1.1 的真实 system 首句必须被换掉。

    这条是 2026-11-28 抓真实请求拿到的（让 kimi 指向本地捕获端点，落盘
    ``/v1/chat/completions`` 的 body，见 PR 说明）。**不要**凭印象改成
    ``You are Kimi,``——Kimi 那句里根本没有逗号。

    回放验证：真实 35K system prompt 掩码前挂死 90s 无首字节，掩码后 4.8s 200。
    """
    patched, hit = neutralize_identity_text(KIMI)
    assert hit is not None
    assert "Kimi" not in patched
    assert patched == MASK

    out = neutralize_agent_identity(_msgs(("system", KIMI), ("user", "hi")))
    assert out[0]["content"] == MASK
    assert out[1]["content"] == "hi"


@pytest.mark.parametrize("text", [
    "You are Kimi",          # 无尾随空格 → 放行
    "You are Kimi.",         # 句号 → 放行
    "You are Kimi\n\nhi",    # 换行 → 放行
    "You are Kimi123",       # 数字紧跟 → 放行
    # 制表符 ≠ 空格。这条**不是凑数的**：它防的是把判据放宽成
    # `re.match(r"You are Kimi\s", text)` —— 那样 `You are Kimi\n\n# Role`
    # 这种真实用户 prompt 里的正常排版就会被误改（实测 `You are Kimi\n` 200 放行）。
    "You are Kimi\tagent",
    "you are Kimi Code CLI",  # 小写 you → 放行
])
def test_kimi_marker_boundaries_are_exact(text):
    """**尾随空格是必需的**，这是实测出来的，不是猜的。

    2026-11-28 在 qoder/hungerg 上逐条二分的结果：

        You are Kimi            → 200（放行）
        You are Kimi.           → 200
        You are Kimi\\n          → 200
        You are Kimi123         → 200
        You are Kimi\\t          → 200
        You are Kimi<space>     → **挂住**
        you are Kimi Code CLI   → 200（大小写敏感）

    所以既不能放宽成「Kimi 后跟任意字符」（会误改用户 prompt），也不能收紧成
    「必须带逗号」（Kimi 那句没有逗号，会漏掉真实客户端）。改这里之前请重跑矩阵。
    """
    patched, hit = neutralize_identity_text(text)
    assert hit is None, f"{text!r} 上游放行（实测 200），不该被判为命中"
    assert patched == text


def test_kimi_masked_anywhere_in_prompt():
    """Kimi 的身份句在 system 里不是首段也能命中（真实 prompt 就是这种形态）。

    真实 system prompt 35349 字符，身份句在开头，但后面还有 ``Your primary
    goal is ...`` 等段落。掩码只该吃掉身份那一句，后面全部保留。
    """
    text = KIMI + "\n\nYour primary goal is to help users with software engineering tasks."
    patched, _ = neutralize_identity_text(text)
    assert patched == MASK + "\n\nYour primary goal is to help users with software engineering tasks."
    assert "primary goal" in patched, "真实指令被误吞了"


def test_kimi_marker_keeps_trailing_space():
    """尾随空格是这个 marker 的**判据本体**，不是排版噪声。

    去尾随空格的工具链会静默删掉它：编辑器保存钩子、`prettier`、
    某些 ruff/black 配置里的 W291 自动修复。删掉之后：

    - Python 语法完全合法，ruff 不报错，CI 的 formatter 检查也不会红；
    - `You are Kimi` 实测 200 放行，所以不会立刻出问题；
    - 但 Kimi CLI 的请求**重新开始挂死**（无首字节、无报错）。

    也就是说这是典型的「静默失效」——`test_kimi_marker_boundaries_are_exact`
    能抓到，但它的报错是「`You are Kimi` 不该被判为命中」，读的人得先反推出
    「哦 marker 被改了」才知道根因。这条断言直接把 marker 钉死，报错一眼可见。
    """
    kimi_markers = [m for m in COMPETITOR_IDENTITY_MARKERS if "Kimi" in m]
    assert kimi_markers == ["You are Kimi "], (
        "Kimi 的 marker 必须以**空格**结尾（不是逗号、不是句号、不是省略）。"
        "实测 `You are Kimi` / `You are Kimi.` / `You are Kimi\\n` 都 200 放行，"
        "只有 `You are Kimi ` 会挂住上游。"
    )
    # 显式断言「最后一个字符是空格」，让报错信息本身就说清问题。
    assert kimi_markers[0].endswith(" "), "尾随空格被去掉了"
    assert not kimi_markers[0].endswith("  "), "多了一个空格（复制粘贴常见）"
