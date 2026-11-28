"""管理 UI：版本检测接口（右下角「有新版本」提示的数据源）。

只暴露一个只读 GET：页面每 5 分钟问一次本机，**浏览器不出网**。真正的
``git fetch`` 在服务端后台线程里跑（见 ``core/release.py``），这个接口
只读缓存，所以页面轮询再频繁也不会把 git 打穿。
"""

from __future__ import annotations

import time

from fastapi import Request

from buddy_proxy.core import release as release_mod
from buddy_proxy.core.state import app

from .common import _ensure_local


@app.get("/ui/api/release")
async def ui_release(request: Request):
    """当前版本 / 是否有新版 / 发布说明。

    ``refresh=1`` 时强制立刻检测一次（「立即检查」按钮用）；默认只回缓存，
    缓存过期则**在后台**刷新并在本次响应里标注 ``stale``——接口本身绝不
    等待 ``git fetch``，否则一次网络抖动就会让页面转圈十几秒。
    """
    _ensure_local(request)
    force = request.query_params.get("refresh") in ("1", "true", "yes")
    if force:
        return release_mod.refresh(force=True)
    data = release_mod.cached()
    # checked_at==0 表示进程刚起、还没检测过：立刻排一次后台检测
    stale = not data.get("checked_at") or (
        time.time() - float(data.get("checked_at") or 0)
        > release_mod.POLL_INTERVAL_S
    )
    if stale:
        release_mod.refresh_async()
    data["stale"] = bool(stale)
    return data
