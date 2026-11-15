"""OpenAI chat → Antigravity（v1internal）请求包装。

内层 ``request`` 就是标准 Gemini generateContent 体，OpenAI → Gemini 的
messages/tools/schema 转换全部复用 :mod:`buddy_proxy.gemini.convert`（同一
API 家族，转换规则一致）；本模块只做 antigravity 特有的三件事：

1. **envelope 不同**：``{project, model, request, userAgent, requestType,
   requestId}``——model 是裸名（不带 ``models/`` 前缀），无 ``user_prompt_id``。
2. **systemInstruction 注入**：社区共识方案（CLIProxyAPI/gcli2api 同款）——
   两个 part：身份断言 + ``[ignore]`` 包裹的同一份（让模型读到 Antigravity
   身份过服务端风控，又指示它别自称 Antigravity，fix 参考实现 issue #76）；
   用户自己的 system 追加在后面。
3. **身份清洗**：用户 system 里「You are Claude/Codex/…」式断言会触发
   cloudcode-pa 的 429 RESOURCE_EXHAUSTED（Adrian De Vera 逆向实测），替换
   成中性表述。Claude Code 的 agent 模板另有一条精确句式指纹，命中必须整段
   换成短摘要（见 :func:`scrub_identity`）；dsh（DeepSeek Harness）的固定
   身份声明同样整段替换（2026-11-15 实测：单独一句即 429，换成中性句即 200）。
   ``ANTIGRAVITY_SCRUB_IDENTITY``
   （``term=>replacement,…``）可加规则。

响应方向（Gemini → OpenAI / SSE 解析）与 gemini 完全同构，直接复用。
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any

from ..core.desensitize import (
    _CLAUDE_HARNESS_MARKERS,
    _CLAUDE_HARNESS_SUMMARY,
    _DEEPSEEK_HARNESS_MARKER,
    _DEEPSEEK_HARNESS_SUMMARY,
)
from ..gemini.convert import gemini_response_to_chat, iter_gemini_sse_payloads  # noqa: F401 - re-export 供 provider 用
from ..gemini.convert import chat_to_gemini_request

#: 服务端风控要求请求自称 Antigravity；[ignore] 段让模型别真信（issue #76）。
_SYSTEM_IDENTITY = (
    "You are Antigravity, a powerful agentic AI coding assistant designed by "
    "the Google Deepmind team working on Advanced Agentic Coding."
    "You are pair programming with a USER to solve their coding task."
    " The task may require creating a new codebase, modifying or debugging an"
    " existing codebase, or simply answering a question."
    "**Absolute paths only****Proactiveness**"
)

#: ``[ignore]`` 包裹副本（参考实现 buildCloudCodeRequest 同款双 part）。
_IGNORE_WRAP = "Please ignore the following [ignore]{text}[/ignore]"

#: 默认清洗规则：竞品身份断言 → 中性表述（触发 429 的已知模式）。
_DEFAULT_SCRUB_RULES: list[tuple[str, str]] = [
    ("You are Claude Code", "You are the assistant"),
    ("You are Claude", "You are the assistant"),
    ("You are Codex", "You are the assistant"),
    ("You are ChatGPT", "You are the assistant"),
    ("You are GPT", "You are the assistant"),
    # dsh（DeepSeek Harness）TUI 的固定身份声明。CodeBuddy 通道早就按它整单
    # 400（见 core.desensitize），antigravity 则是伪装成 429 的同一套硬校验：
    # 2026-11-15 直连实测，只把这一句塞进 systemInstruction 就稳定
    # RESOURCE_EXHAUSTED；换成 "You are the assistant." 同一份 4KB dsh 模板
    # 立刻 200，其余一个字没动。
    ("You are an AI agent powered by DeepSeek Harness.", "You are the assistant."),
]


def _parse_env_rules(raw: str) -> list[tuple[str, str]]:
    rules: list[tuple[str, str]] = []
    for pair in raw.split(","):
        idx = pair.find("=>")
        if idx > 0:
            term, repl = pair[:idx].strip(), pair[idx + 2:].strip()
            if term:
                rules.append((term, repl))
    return rules


def _scrub_rules() -> list[tuple[str, str]]:
    env = os.environ.get("ANTIGRAVITY_SCRUB_IDENTITY", "").strip()
    return _DEFAULT_SCRUB_RULES + (_parse_env_rules(env) if env else [])


_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))


def _strip_zero_width(text: str) -> str:
    """去掉零宽字符，只用于身份声明匹配（不改动发出去的正文）。"""
    return text.translate(_ZERO_WIDTH)


def scrub_identity(text: str) -> str:
    """清洗 system 文本里的竞品身份断言（大小写不敏感的整词替换）。

    Claude Code 2.x 的 system 模板里有一句**精确句式指纹**：
    ``You are an interactive agent that helps users with software engineering
    tasks.``。2026-11-15 直连 cloudcode-pa 逐项实测：这句单独放进
    systemInstruction 就稳定 429 ``RESOURCE_EXHAUSTED``（伪装成配额错误），
    改一个词、换语序、去掉 ``You are`` 前缀都 200；零宽空格也绕不过
    （与 CodeBuddy 的 dsh 身份声明同款硬校验）。旧逻辑只把
    ``You are Claude Code`` 换成 ``You are the assistant``，剩下整段模板
    原样保留，于是 Claude Code 走 /v1/messages 时两个账号接连 429、被按模型
    冷却 5 分钟，用户看到「渠道都不可用」。

    所以命中这条指纹时**整段换成短摘要**（复用 core.desensitize 里已为
    CodeBuddy 通道写好的同一份契约），而不是继续做词级替换。用精确子串
    匹配整句，不用长度阈值——实测这句单独 82 字符就足以触发 429，
    加 >=1000 的门槛会让短模板漏网；反过来改一个词就 200，说明上游认的
    就是这句话本身，精确匹配不会误伤普通业务指令。
    """
    if _CLAUDE_HARNESS_MARKERS[0] in text:
        return _CLAUDE_HARNESS_SUMMARY
    # dsh 走整段替换而不是词级替换：下游模板（工具约定、exit code 契约等）
    # 都是上游放行的普通指令，只有那句身份声明被拦；复用 CodeBuddy 那份
    # 已经过实测的短摘要，保持两条通道对同一客户端的行为一致。
    # 匹配前先抹掉零宽字符：客户端把这句话拆开（词间塞 U+200B 之类）上游照样拦，
    # 所以大小写和零宽都要容忍，宁可多换一次也不能漏。
    if _DEEPSEEK_HARNESS_MARKER in _strip_zero_width(text).lower():
        return _DEEPSEEK_HARNESS_SUMMARY
    for term, repl in _scrub_rules():
        text = re.sub(re.escape(term), repl, text, flags=re.IGNORECASE)
    return text


def apply_effort_suffix(entry: dict[str, Any], reasoning_effort: Any) -> str:
    """模型表条目 → 上游真名：upstream 覆盖 + effort 后缀解析。

    antigravity 上游只认 fetchAvailableModels 列表里的名字（2026-10-02 实测：
    gemini 3 系裸名 429、claude 裸名可用、3.8 只有 -tiered、gpt-oss 只有
    -medium），所以后缀策略由模型表逐模型声明（``efforts`` / ``default_effort``），
    不按名字前缀猜。显式 effort 不在 ``efforts`` 里时回落 ``default_effort``
    （用户拿到一个能用的档位总比 400 好）。名字固定的模型（无 ``efforts``）
    只做 upstream 映射。
    """
    base = str(entry.get("upstream") or entry["id"])
    efforts = [str(e).strip().lower() for e in entry.get("efforts") or [] if str(e).strip()]
    if not efforts:
        return base
    effort = str(reasoning_effort or "").strip().lower()
    if effort not in efforts:
        effort = str(entry.get("default_effort") or efforts[0]).strip().lower()
    return f"{base}-{effort}"


def chat_to_antigravity_request(
    body: dict[str, Any],
    *,
    project_id: str,
    model: str,
) -> dict[str, Any]:
    """OpenAI chat 请求 → antigravity envelope（流式/非流式同体，SSE 由 URL 决定）。"""
    inner = chat_to_gemini_request(
        body, project_id=project_id, model=model, stream=bool(body.get("stream"))
    )
    request = inner["request"]
    request.pop("session_id", None)  # 参考实现不发 session_id

    system_parts: list[dict[str, Any]] = [
        {"text": _SYSTEM_IDENTITY},
        {"text": _IGNORE_WRAP.format(text=_SYSTEM_IDENTITY)},
    ]
    user_si = request.get("systemInstruction")
    if isinstance(user_si, dict):
        for part in user_si.get("parts") or []:
            if isinstance(part, dict) and part.get("text"):
                system_parts.append({"text": scrub_identity(str(part["text"]))})
    # 参考实现形态：systemInstruction 带 role: "user"，置于顶层 request
    request["systemInstruction"] = {"role": "user", "parts": system_parts}

    return {
        "project": project_id,
        "model": model,
        "request": request,
        "userAgent": "antigravity",
        "requestType": "agent",
        "requestId": f"agent-{uuid.uuid4()}",
    }
