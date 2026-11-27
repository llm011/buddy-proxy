"""竞品 Agent 身份声明脱敏 —— 跨通道的统一防线。

## 为什么要有这个模块

上游渠道方（Qoder / CodeBuddy 等）会把「别的 Agent 客户端借我的额度跑」
挡在门外。手段分两类，**表现完全不同**，但根因是同一件事：

===========================================  ==================================
通道                                          命中竞品身份声明时
===========================================  ==================================
codebuddy                                     ``400 / code 11128``，秒回
qoder                                         **长时间无首字节**（软性挂起）
===========================================  ==================================

挂起那一类尤其难查：客户端只看到自己卡死、没有任何报错，curl 直连也是干等，
很容易误判成「模型慢」「网关挂了」「工具太多」。2026-11-27 就是这样排了一轮
无效方向（工具数、上下文长度、max_tokens 逐个二分），最后逐段回放真实请求体
才定位到 system prompt 里的那一句身份声明。

## 为什么不能只匹配「完整句式」

最初（codebuddy 那边）只匹配**完整**的一句：

    You are an AI agent powered by DeepSeek Harness.

2026-11-27 实测发现 qoder 的判据**宽得多**——触发点短到 ``You are Codex,``
（连逗号后面的内容都不需要），且大小写敏感。若沿用「完整句式」的思路，
Codex / Claude Code 这两个主流客户端的请求会**全部挂死**，而且看起来像
「模型坏了」。

同时实测确认它**不是**「You are X,」通用规则：Cursor / Windsurf / Copilot /
Kimi / Gemini / Qwen / Trae / OpenCode / Aider / Cline 等一律 200 放行，
只有一份**竞品名单**会被拦。所以既不能只匹配一条完整句（漏），也不能
匹配任意「You are X,」（误伤用户 prompt）。

## 实测边界（2026-11-27，qoder 通道，hungerg / qwen3.8-max，流式）

==================================================  ======
system prompt                                      结果
==================================================  ======
``You are an AI agent powered by DeepSeek Harness.``  挂住
``You are Claude Code, Anthropic's official CLI…``     挂住
``You are Claude Code,``                               挂住
``You are Codex,``                                     挂住
``You are Codex, an agent based on GPT-6.``            挂住
``You are Claude Code``（无逗号）                       200
``you are codex,``（全小写）                            200
``YOU ARE CLAUDE CODE, …``（全大写）                    200
``You are Cursor, an AI code editor.``                 200
``You are Kimi, an AI assistant.``                     200
``You are Gemini, an AI assistant.``                   200
==================================================  ======

两个关键结论：

1. **逗号是必要条件**。``You are Codex`` 放行、``You are Codex,`` 挂住。
   所以匹配串必须带逗号，否则会平白改写大量正常 prompt。
2. **大小写敏感**。全大写/全小写都放行，只有原样大小写会拦。故用精确匹配，
   ``.lower()`` 之类的「更宽容」写法反而会把放行的请求也改掉。

## 扫描范围

与 codebuddy 的 ``_neutralize_leading_system`` 不同，这里**只扫第一条**
system/developer 消息，且该条**整条**都扫（不是只扫首段）：

- 首条 system 里签名出现在开头 / 换行后 / 普通前言之后，都会挂住；
- 第二条 system、assistant、user 里出现同一签名，实测**放行**。

改这个范围前请重跑上面的矩阵。历史上这个仓库在 codebuddy 那边两次栽在
「只覆盖了当时实测到的那一种形态」，第二次让保护静默失效了一整天。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

#: 会触发上游拦截的竞品身份声明（精确、大小写敏感）。
#:
#: 每条都带逗号——见模块 docstring 的实测矩阵，逗号是必要条件。
#: **只加实测确认会拦的**：未测过的名字往里加，只会平白篡改用户的 prompt。
#: 加新条目之前先跑一遍 ``tests/test_agent_identity.py`` 里记录的探针方法。
COMPETITOR_IDENTITY_MARKERS: tuple[str, ...] = (
    # Qoder / CodeBuddy 的「官方客户端」判据，dsh 永远把它放在 system 首段。
    "You are an AI agent powered by DeepSeek Harness.",
    # Claude Code（两个变体：CLI 本体 / Agent SDK 内嵌）
    "You are Claude Code,",
    # Codex CLI
    "You are Codex,",
)

#: 命中处的替换文本。不含任何 Agent 品牌字样，也不会被上面的 marker 二次命中。
IDENTITY_MASK = "[redacted: client identity]"

#: 会触发拦截的角色。实测见模块 docstring；user / assistant 上游不扫。
IDENTITY_ROLES = frozenset({"system", "developer"})

#: 掩码之后要一并吃掉的「同一句」尾巴。
#:
#: marker 只到逗号为止（逗号是触发条件，见模块 docstring），所以直接替换会留下
#: 半句残留：
#:
#:     You are Claude Code, Anthropic's official CLI for Claude.
#:     → [redacted: client identity] Anthropic's official CLI for Claude.
#:
#: 残留本身不会再触发拦截，但它是一句**没有主语的自我描述**，会污染模型对自身
#: 身份的推断——实测模型在 thinking 里明确复述了这句残留（「weird developer
#: message ends "..."」）。掩码的目的是「不告诉上游是谁」，不是「留半句谜语」。
#:
#: 所以命中后，从 marker 结尾继续吃到本句句末（含标点）为止；只吃**同一句**
#: （遇到换行就停），避免误吞后面的真实指令。
_SENTENCE_END = "。．.!！?？\n"


def _mask_one(text: str, marker: str) -> str:
    """把 ``text`` 里所有 ``marker`` 换成掩码，并吃掉本句剩余部分。

    「剩余部分」到本句句末或换行为止——见 :data:`_SENTENCE_END` 的说明。

    **只有没收句的 marker 才需要吃尾巴**：DSH 那条自带句号
    （``...DeepSeek Harness.``），它后面本来就是新的一句，再吃就会把用户的
    真实指令吃掉。实测踩过：``...Harness. You must always be polite. Be brief.``
    被吞成 ``[redacted] Be brief.``，中间那句指令凭空消失——这比留半句谜语
    更糟，因为它**静默改变了模型行为**。
    """
    # marker 自己已经收句 → 没有「逗号之后那半句」的残留，原样替换即可。
    eats_tail = marker[-1:] not in _SENTENCE_END

    out: list[str] = []
    start = 0
    while True:
        idx = text.find(marker, start)
        if idx < 0:
            out.append(text[start:])
            break
        out.append(text[start:idx])
        out.append(IDENTITY_MASK)
        stop = idx + len(marker)
        if eats_tail:
            # 从 marker 结尾往后吃到本句句末；找不到句末（整段都是这一句）就吃到底。
            while stop < len(text) and text[stop] not in _SENTENCE_END:
                stop += 1
            # 句末标点本身也吃掉（它是原客户端那句话的一部分），但换行要留着——
            # 换行是结构，吃掉会把两段粘成一段。
            if stop < len(text) and text[stop] != "\n":
                stop += 1
        start = stop
    return "".join(out)


def neutralize_identity_text(text: str) -> tuple[str, str | None]:
    """把文本里的竞品身份声明换成中性占位。

    返回 ``(新文本, 命中的 marker)``；未命中时第二项为 ``None``，且**原样返回
    入参字符串**（调用方可靠 ``is`` 判断是否改动）。

    **逐条都试，不是命中一条就收工**：同一段 system 里出现两个不同客户端身份
    声明是可能的（用户把两个 CLI 的 prompt 拼在一起做对比、或某个编排层把
    subagent 的 persona 也塞进来）。命中一条就返回会让第二条漏网，而上游只
    要看到一条就拦——等于这次改动对那种请求完全无效，且日志里还写着「已中性化」，
    排查时会被这条日志误导。
    """
    if not text:
        return text, None
    hit_any: str | None = None
    for marker in COMPETITOR_IDENTITY_MARKERS:
        if marker in text:
            text = _mask_one(text, marker)
            hit_any = hit_any or marker
    return (text, hit_any) if hit_any else (text, None)


def _neutralize_content(content: Any) -> tuple[Any, str | None]:
    """处理一个 message 的 ``content``（字符串或 blocks 数组）。"""
    if isinstance(content, str):
        patched, hit = neutralize_identity_text(content)
        return (patched if hit else content), hit
    if not isinstance(content, list):
        return content, None
    changed = False
    hit_any: str | None = None
    blocks: list[Any] = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            patched, hit = neutralize_identity_text(block["text"])
            if hit:
                changed = True
                hit_any = hit_any or hit
                blocks.append({**block, "text": patched})
                continue
        blocks.append(block)
    return (blocks if changed else content), hit_any


def neutralize_agent_identity(messages: Any) -> Any:
    """把首条 system/developer 里的竞品身份声明中性化。

    未命中时返回**原对象**（同一引用，不复制）——绝大多数请求走这条路径，
    不该为一份低频的防御性改写给每条消息都做一次深拷贝。

    只处理首条 system/developer：实测第二条 system 里同一签名上游照常放行，
    多改只会篡改用户的 prompt。
    """
    if not isinstance(messages, list):
        return messages
    for idx, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") not in IDENTITY_ROLES:
            continue
        patched, hit = _neutralize_content(message.get("content"))
        if hit is None:
            # 首条 system/developer 干净 → 后续同类消息上游不扫，直接结束。
            return messages
        log.info("上游会拦这个客户端身份声明，已中性化：%r", hit[:48])
        out = list(messages)
        out[idx] = {**message, "content": patched}
        return out
    return messages
