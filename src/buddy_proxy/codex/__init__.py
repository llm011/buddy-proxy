"""OpenAI Codex（ChatGPT 订阅）通道。

上游是 ChatGPT 后端 **Responses** 协议（``/backend-api/codex/responses``），
与仓库其它通道的 Chat Completions 不同，因此本包自带一层
:mod:`buddy_proxy.codex.projection` 做双向投影。

模块导览：

- :mod:`~buddy_proxy.codex.oauth`      —— OAuth2 + PKCE 协议层（纯 HTTP）
- :mod:`~buddy_proxy.codex.callback_server` —— 本地 1455 回调服务
- :mod:`~buddy_proxy.codex.credentials` —— 多账号存储与 token 刷新
- :mod:`~buddy_proxy.codex.failover`   —— 账号冷却与顺位
- :mod:`~buddy_proxy.codex.login`      —— 交互式登录 / 凭据导入
- :mod:`~buddy_proxy.codex.upstream`   —— 上游请求构造与额度解析
- :mod:`~buddy_proxy.codex.projection` —— Chat ⇄ Responses 双向投影
- :mod:`~buddy_proxy.codex.provider`   —— 通道实现
"""
