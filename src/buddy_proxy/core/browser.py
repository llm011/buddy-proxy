"""打开外部 URL（登录授权页）的统一入口。

stdlib ``webbrowser`` 失败时**不抛异常、只静默返回 ``False``**（macOS 走
osascript ``open location``，SSH/无 GUI 会话等环境必失败）。调用方若不看
返回值，授权链接就凭空消失——用户只看到「浏览器将打开授权页」的横幅，
然后干等回调直到 15 分钟超时（buddy login trae 实测踩坑）。

统一策略：先 ``webbrowser``；macOS 上失败再退 ``open`` 命令（同样走
LaunchServices 但绕开 osascript，异常环境下成功率高一截）；仍失败返回
``False``，由调用方把链接打出来让人手动开。
"""

from __future__ import annotations

import subprocess
import sys
import webbrowser


def open_external_url(url: str) -> bool:
    """在用户默认浏览器打开 url；确实打不开返回 False（不抛异常）。"""
    try:
        if webbrowser.open(url):
            return True
    except Exception:  # noqa: BLE001 - 浏览器打不开不该炸掉登录流程
        pass
    if sys.platform == "darwin":
        try:
            # stderr 留着不吞：open 报错时（如无 GUI 会话）调用方能在终端看到原因
            subprocess.run(["open", url], check=True, timeout=10)
            return True
        except Exception:  # noqa: BLE001
            pass
    return False
