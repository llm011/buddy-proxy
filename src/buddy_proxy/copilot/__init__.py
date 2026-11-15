"""GitHub Copilot 通道：把本地已登录的 Copilot 订阅模型代理成 OpenAI/Anthropic 兼容 API。

与其它通道的根本差别：**上游没有 token 刷新这一步**。GitHub OAuth token
（``gho_``）本身就是长期凭据，直接作 Bearer 调 ``api.githubcopilot.com``；
``/copilot_internal/v2/token`` 的老式换票接口对个人订阅已返回 403，不再走。
所以 ``credentials.py`` 里没有 refresh_token / expiry 那套，只有「凭据好使
不好使」（401 时提示重新 ``gh auth login`` 或 device flow 登录）。

上游支持三条端点（``/chat/completions``、``/responses``、``/v1/messages``），
具体某个模型能用哪条由目录里的 ``supported_endpoints`` 决定，见 ``catalog.py``。
"""
