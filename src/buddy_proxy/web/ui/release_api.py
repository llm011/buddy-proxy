"""管理 UI：版本检测与「一键更新」接口。

检测是只读 GET：页面每 5 分钟问一次本机，**浏览器不出网**。真正的
``git fetch`` 在服务端后台线程里跑（见 ``core/release.py``），这个接口
只读缓存，所以页面轮询再频繁也不会把 git 打穿。

更新是 POST：真的去跑 ``git pull --ff-only`` + ``uv sync`` + 重启，而不是只
刷浏览器（见 ``ui_release_update`` 的注释）。
"""

from __future__ import annotations

import time

from fastapi import HTTPException, Request

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


@app.post("/ui/api/release/update", status_code=202)
async def ui_release_update(request: Request):
    """真正把代码更新到最新版本，然后由服务端重启自己。

    ## 为什么必须脱离当前进程跑

    更新流程的最后一步是重启服务，而重启会 ``kill`` 掉当前进程。如果在这里
    同步执行，请求必然在写回响应前就断了——页面只会看到一个 network error，
    不知道是「成功但重启了」还是「失败了」。所以：先把更新脚本**脱离**本进程
    拉起（``start_new_session``，独立会话），立刻返回 202，页面自行轮询
    ``/ui/api/release`` 直到版本追上。

    ## 为什么不在服务内直接 subprocess 到 git/uv

    复用 ``buddy update`` 那一套（脏工作树中止、``--ff-only``、``uv sync``、
    重启与健康检查）比在这里重写一遍可靠。它还是唯一知道「launchd 托管还是
    proxy.sh 托管」的地方——两种托管方式的重启命令不同，不该在这里复制。

    ## 为什么要求工作树干净

    ``git pull --ff-only`` 遇到脏工作树会失败或被半截覆盖。这个仓库的主工作区
    经常有别人在途的改动，静默覆盖是**数据丢失**级别的事故。所以先检查，
    脏就 409 并把 ``git status --short`` 的原文回给页面，让用户自己决定。
    """
    _ensure_local(request)
    ok, status, payload = release_mod.start_update()
    if not ok:
        raise HTTPException(status_code=status,
                            detail={"error": {"message": payload.get("message", "")}})
    return payload
