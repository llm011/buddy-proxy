"""书生·端砚（上海 AI Lab Intern AI）通道。

见 ``provider.py`` 的模块说明：上游是端砚的**私有**两段式协议（不是 OpenAI
兼容），鉴权复用端砚桌面端的 SSO 登录态（Electron safeStorage 加密，14 天
有效），额度走 Token Plan 的双窗口（5 小时 + 7 天）。
"""
