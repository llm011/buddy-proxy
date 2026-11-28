"""管理 UI：/ui 页面与 /ui/api/* 管理接口。

功能：
- 按 provider 分组的模型列表，一键「设为默认启用模型」（settings.py 持久化）
- 每个模型一键测试：发一条 "hi"，返回延迟 / token / 回复预览
- 按 provider/模型维度聚合的请求统计（metrics.py），含近 14 天图表与最近请求
- provider 健康状态总览
- 打卡/额度（benefits）、各通道专属管理面板（qoder/traepat/codebuddy）

安全约定：/ui/api/* 仅允许本机（127.0.0.1 / ::1）访问；如确需从局域网打开
管理页操作，设置环境变量 ``BUDDY_PROXY_ADMIN_OPEN=1`` 放开（自担风险）。
/v1/* 代理端点不受此限制。

结构（2026-10-03 由单文件 ui.py 拆出，import 各子模块即完成路由注册）：

- ``common``      本机校验 / 错误文案 / 一键测试常量
- ``queries``     状态查询（总览/统计/日志/打卡额度）+ 打卡后台循环
- ``channels``    通道专属接口（qoder/traepat/codebuddy）
- ``models_api``  模型目录与停用/限时/顺序管理 + 一键测试
- ``settings_api`` settings.json 读写
- ``release_api``  版本检测（右下角「有新版本」提示）
- ``page``        页面与前端静态文件 serve
"""

from . import (  # noqa: F401
    channels, models_api, page, queries, release_api, settings_api,
)
