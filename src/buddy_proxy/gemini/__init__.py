"""Gemini CLI（Code Assist）通道。

上游是 Gemini 原生 ``v1internal`` 协议，不是 OpenAI 形态：请求与响应都要经
:mod:`buddy_proxy.gemini.convert` 双向转换。

模块导览：

- :mod:`~buddy_proxy.gemini.convert`     —— 双向转换 + usage 映射 + thoughtSignature 回环
- :mod:`~buddy_proxy.gemini.provider`    —— 通道实现（三协议入口、401 强刷重试）
- :mod:`~buddy_proxy.gemini.credentials` —— OAuth 凭证存取与刷新
- :mod:`~buddy_proxy.gemini.fingerprint` —— 对齐本机 CLI 的请求头指纹
- :mod:`~buddy_proxy.gemini.login` / :mod:`~buddy_proxy.gemini.setup` —— 登录与 onboarding
- :mod:`~buddy_proxy.gemini.cli_bridge`  —— 与本机 ``gemini`` CLI 互通登录态

注意：Antigravity 通道也 import 本包的 ``convert``（两者共用 Gemini 原生协议形状），
改这里时别忘了它。
"""
