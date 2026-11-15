"""antigravity envelope 包装 / 身份清洗 / effort 后缀 / fingerprint 单元测试。"""

from __future__ import annotations



# ---------------------------------------------------------------------------
# envelope：chat → antigravity 包装体
# ---------------------------------------------------------------------------

def _chat_body(**over):
    body = {
        "model": "gemini-3.8-flash",
        "messages": [
            {"role": "system", "content": "You are Claude Code, a helpful assistant."},
            {"role": "user", "content": "hi"},
        ],
    }
    body.update(over)
    return body


def test_envelope_shape():
    from buddy_proxy.antigravity.convert import chat_to_antigravity_request

    payload = chat_to_antigravity_request(_chat_body(), project_id="p1", model="gemini-3.8-flash")
    assert payload["project"] == "p1"
    assert payload["model"] == "gemini-3.8-flash"  # 裸名，无 models/ 前缀
    assert payload["userAgent"] == "antigravity"
    assert payload["requestType"] == "agent"
    assert payload["requestId"].startswith("agent-")
    req = payload["request"]
    assert "session_id" not in req  # 参考实现不发 session_id
    assert req["contents"][0]["parts"][0]["text"] == "hi"


def test_system_instruction_identity_and_scrub():
    from buddy_proxy.antigravity.convert import _SYSTEM_IDENTITY, chat_to_antigravity_request

    payload = chat_to_antigravity_request(_chat_body(), project_id="p", model="m")
    parts = payload["request"]["systemInstruction"]["parts"]
    assert payload["request"]["systemInstruction"]["role"] == "user"
    # 1) 身份断言 2) [ignore] 包裹副本 3) 用户 system（已被清洗）
    assert parts[0]["text"] == _SYSTEM_IDENTITY
    assert "[ignore]" in parts[1]["text"] and _SYSTEM_IDENTITY in parts[1]["text"]
    assert "You are Claude Code" not in parts[2]["text"]
    assert "the assistant" in parts[2]["text"]


def test_scrub_identity_rules_and_env(monkeypatch):
    from buddy_proxy.antigravity import convert

    assert convert.scrub_identity("You are Claude, made by Anthropic") == "You are the assistant, made by Anthropic"
    assert convert.scrub_identity("you are codex here") == "You are the assistant here"  # 大小写不敏感
    assert convert.scrub_identity("Claude is a model") == "Claude is a model"  # 非断言句式不动

    monkeypatch.setenv("ANTIGRAVITY_SCRUB_IDENTITY", "FooBar=>Baz, Hermes=>helper")
    assert convert.scrub_identity("You are FooBar") == "You are Baz"
    assert convert.scrub_identity("Hermes Agent") == "helper Agent"  # env 规则追加在默认后


def test_scrub_identity_replaces_claude_code_agent_template():
    """Claude Code 的 agent 模板整句会触发 cloudcode-pa 的伪装 429。

    2026-11-15 直连逐项实测：``You are an interactive agent that helps users
    with software engineering tasks.`` 单独放进 systemInstruction 就稳定 429
    RESOURCE_EXHAUSTED；改一个词 / 换语序 / 去掉前缀都 200，说明上游认的是
    精确句式而非关键词。旧 scrub 只换 ``You are Claude Code``，剩下这段模板
    原样进上游，于是 Claude Code 走 /v1/messages 时两个账号接连 429、被按
    模型冷却 5 分钟，用户看到「渠道都不可用」。

    修法照抄 CodeBuddy/dsh 的先例：命中固定身份声明就整段换成短摘要，
    零宽空格绕不过硬校验。断言锁住「整段替换」与「近似句式不误伤」两条，
    实现退回词级替换时第一条当场红。
    """
    from buddy_proxy.antigravity import convert
    from buddy_proxy.core.desensitize import _CLAUDE_HARNESS_SUMMARY

    marker = "You are an interactive agent that helps users with software engineering tasks."
    real = ("You are Claude Code, Anthropic's official CLI for Claude.\n\n"
            + marker + "\nUse the instructions below and the tools available to you.\n\n"
            "# Tone and style\nYou should be concise, direct, and to the point.")
    out = convert.scrub_identity(real)
    assert out == _CLAUDE_HARNESS_SUMMARY, "命中模板必须整段替换，不能留残句"
    assert marker not in out
    assert "Anthropic's official CLI" not in out

    # 单独一句、没有长度门槛也要拦（实测 82 字符即触发 429）
    assert convert.scrub_identity(marker) == _CLAUDE_HARNESS_SUMMARY

    # 近似句式不误伤：改一个词 / 换语序都不该被整段吃掉
    variant = "You are an interactive agent that helps users with coding tasks."
    assert convert.scrub_identity(variant) == variant
    reordered = "You help users with software engineering tasks as an interactive agent."
    assert convert.scrub_identity(reordered) == reordered

    # 普通词级规则仍然生效
    assert convert.scrub_identity("You are Claude, made by Anthropic") == \
        "You are the assistant, made by Anthropic"


def test_chat_to_antigravity_request_neutralizes_claude_system():
    """从 Claude Code 风格的 chat body 走完整 envelope，user system 必须是摘要。"""
    from buddy_proxy.antigravity.convert import chat_to_antigravity_request
    from buddy_proxy.core.desensitize import _CLAUDE_HARNESS_SUMMARY

    body = {
        "model": "gemini-3.8-flash",
        "messages": [
            {"role": "system", "content":
                "You are Claude Code, Anthropic's official CLI for Claude.\n\n"
                "You are an interactive agent that helps users with software engineering tasks."},
            {"role": "user", "content": "hi"},
        ],
    }
    parts = chat_to_antigravity_request(body, project_id="p", model="m")[
        "request"]["systemInstruction"]["parts"]
    assert parts[2]["text"] == _CLAUDE_HARNESS_SUMMARY
    assert "interactive agent" not in parts[2]["text"]


def test_effort_suffix():
    """模型表条目 → 上游真名：efforts/default_effort 声明驱动（上游只认列表变体名）。"""
    from buddy_proxy.antigravity.convert import apply_effort_suffix

    pro = {"id": "gemini-3.1-pro", "upstream": "gemini-3.1-pro",
           "efforts": ["low", "high"], "default_effort": "low"}
    flash = {"id": "gemini-3.6-flash", "efforts": ["low", "medium", "high"],
             "default_effort": "medium"}
    fixed_tiered = {"id": "gemini-3.8-flash", "upstream": "gemini-3.8-flash-tiered"}
    fixed_gpt = {"id": "gpt-oss-120b", "upstream": "gpt-oss-120b-medium"}

    # 无显式 effort → default_effort（裸名会被上游 429 伪装拒绝，必须有后缀）
    assert apply_effort_suffix(pro, None) == "gemini-3.1-pro-low"
    assert apply_effort_suffix(flash, None) == "gemini-3.6-flash-medium"
    # 显式 effort → 对应后缀；不在 efforts 里回落 default_effort
    assert apply_effort_suffix(pro, "high") == "gemini-3.1-pro-high"
    assert apply_effort_suffix(flash, "low") == "gemini-3.6-flash-low"
    assert apply_effort_suffix(pro, "medium") == "gemini-3.1-pro-low"  # 不在 efforts → default
    assert apply_effort_suffix(pro, "ultra") == "gemini-3.1-pro-low"  # 非法值 → default
    # 名字固定的模型（无 efforts）只做 upstream 映射，不加热后缀
    assert apply_effort_suffix(fixed_tiered, "high") == "gemini-3.8-flash-tiered"
    assert apply_effort_suffix(fixed_gpt, None) == "gpt-oss-120b-medium"
    # 无 upstream 字段 → 同 id（claude 裸名可用）
    assert apply_effort_suffix({"id": "claude-sonnet-4-6"}, "high") == "claude-sonnet-4-6"


def test_response_reuse_from_gemini_convert():
    """响应方向直接复用 gemini.convert（同一 API 家族），冒烟确认 import 与解包。"""
    from buddy_proxy.antigravity.convert import gemini_response_to_chat

    chat = gemini_response_to_chat(
        {"response": {"candidates": [{"content": {"parts": [{"text": "pong"}]},
                                      "finishReason": "STOP"}],
                      "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2,
                                        "totalTokenCount": 3}}},
        model="gemini-3.8-flash",
    )
    assert chat["choices"][0]["message"]["content"] == "pong"
    assert chat["usage"]["total_tokens"] == 3


# ---------------------------------------------------------------------------
# fingerprint：UA / X-Client-Version
# ---------------------------------------------------------------------------

def test_fingerprint_headers(monkeypatch):
    from buddy_proxy.antigravity import fingerprint as fp

    monkeypatch.setenv("ANTIGRAVITY_UA_VERSION", "2.18.1")
    monkeypatch.setenv("ANTIGRAVITY_CLIENT_VERSION", "1.110.0")
    monkeypatch.setattr(fp, "_UA_VERSION", None)
    monkeypatch.setattr(fp, "_CLIENT_VERSION", None)

    h = fp.auth_headers("tok", "gemini-3.8-flash", stream=True)
    assert h["Authorization"] == "Bearer tok"
    assert h["User-Agent"].startswith("antigravity/hub/2.18.1 (aidev_client; ")
    assert h["User-Agent"].endswith(" gemini-3.8-flash")
    assert h["X-Client-Name"] == "antigravity"
    assert h["X-Client-Version"] == "1.110.0"
    assert "gl-node/" in h["x-goog-api-client"]
    assert h["Accept"] == "text/event-stream"  # 流式才有

    h2 = fp.auth_headers("tok", stream=False)
    assert "Accept" not in h2
    assert h2["User-Agent"] == fp.user_agent("")  # 无 model 不带尾巴


def test_fingerprint_fallback_without_product_json(monkeypatch):
    """本机没有 Antigravity.app 时用 fallback 版本（env 清空）。"""
    from buddy_proxy.antigravity import fingerprint as fp

    monkeypatch.delenv("ANTIGRAVITY_UA_VERSION", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_CLIENT_VERSION", raising=False)
    monkeypatch.setattr(fp, "_UA_VERSION", None)
    monkeypatch.setattr(fp, "_CLIENT_VERSION", None)
    monkeypatch.setattr(fp, "_product_json_paths", lambda: [])  # 模拟无安装

    assert fp.ua_version() == fp.FALLBACK_UA_VERSION
    assert fp.client_version() == fp.FALLBACK_CLIENT_VERSION


def test_fallback_ua_clears_upstream_version_gate(monkeypatch):
    """fallback UA 必须同时过版本门槛并带 hub 形态；否则 gemini-4/流式都废。

    2026-11-15 实测（两个账号、交错 A/B 多轮）：
    1. 版本门槛：antigravity/2.0.3 的 streamGenerateContent 返回单事件固定文案
       「This version of Antigravity is no longer supported.」，没进模型；2.9.0
       起才给真回复。X-Client-Version 不是门槛，单独改它无效。
    2. 形态门槛：生成 4 族只认 ``antigravity/hub/<v> (aidev_client; ...)``；
       旧形态 ``antigravity/<v> <os>/<arch>`` 对 gemini-4-* 恒 404（其余正常），
       且 UA 版本 < 2.23.0 同样拿不到。

    锁具体值而不是只锁「等于常量」：上一版常量就是 2.0.3，若有人改回旧值，
    这条断言当场红，而不是等用户选模型才发现拿到 404 或废话。
    """
    from buddy_proxy.antigravity import fingerprint as fp

    monkeypatch.delenv("ANTIGRAVITY_UA_VERSION", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_CLIENT_VERSION", raising=False)
    monkeypatch.setattr(fp, "_UA_VERSION", None)
    monkeypatch.setattr(fp, "_CLIENT_VERSION", None)
    monkeypatch.setattr(fp, "_product_json_paths", lambda: [])

    assert fp.FALLBACK_UA_VERSION == "2.32.1"
    assert fp._at_least(fp.FALLBACK_UA_VERSION, fp.MIN_UA_VERSION)
    assert fp.user_agent("gemini-3.8-flash-tiered").startswith(
        "antigravity/hub/2.32.1 (aidev_client; os_type="
    )
    # 门槛只在 UA：X-Client-Version 保持参考实现的值不动。
    assert fp.FALLBACK_CLIENT_VERSION == "1.110.0"
    h = fp.auth_headers("tok", stream=True)
    assert h["X-Client-Version"] == "1.110.0"
    assert h["User-Agent"].startswith("antigravity/hub/2.32.1 (aidev_client; ")


def test_user_agent_shape_unlocks_gemini4_family():
    """UA 必须是 ``antigravity/hub/<v> (<meta>)``：hub/ 路径段与圆括号缺一不可。

    实测矩阵（2026-11-15，gemini-4-hydrogen-high）：只有带 hub/ 且带括号元数据
    的形态返回 200；缺任一项均 404。这里锁死形态，避免「优化」成别的样子。
    """
    from buddy_proxy.antigravity import fingerprint as fp

    ua = fp.user_agent()
    assert ua.startswith("antigravity/hub/")
    assert ")" in ua
    metadata = ua.split("(", 1)[1]
    assert metadata.startswith("aidev_client; os_type=")
    assert "; arch=" in metadata
    # 尾巴可选，但不应影响形态判定。
    assert fp.user_agent("gemini-4-argon-high").startswith(ua + " ")


def test_local_product_json_below_floor_falls_back(monkeypatch, tmp_path):
    """本机装的是老客户端时，UA 版本仍抬到 fallback（Gemini 4 有 ≥2.23.0 门槛）。"""
    from buddy_proxy.antigravity import fingerprint as fp

    monkeypatch.delenv("ANTIGRAVITY_UA_VERSION", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_CLIENT_VERSION", raising=False)
    monkeypatch.setattr(fp, "_UA_VERSION", None)
    monkeypatch.setattr(fp, "_CLIENT_VERSION", None)
    product = tmp_path / "product.json"
    product.write_text('{"ideVersion": "2.10.0", "version": "1.90.0"}', encoding="utf-8")
    monkeypatch.setattr(fp, "_product_json_paths", lambda: [product])

    ua_ver, client_ver = fp._detect_versions()
    assert ua_ver == fp.FALLBACK_UA_VERSION  # 低于门槛 -> 抬到 fallback
    assert client_ver == "1.90.0"  # X-Client-Version 跟随本机，不抬
