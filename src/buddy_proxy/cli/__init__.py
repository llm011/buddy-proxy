"""`buddy` 的本地只读子命令（usage / logs / doctor）。

与 ``buddy_proxy.__main__``（服务进程入口）分开：这些命令要能在**服务没起**
的时候用，不能 import 会把 uvicorn/provider 一并拉起来的模块。

入口：``uv run python -m buddy_proxy.cli <子命令> [参数]``，由仓库根目录的
``buddy`` 脚本转进来。
"""

from __future__ import annotations
