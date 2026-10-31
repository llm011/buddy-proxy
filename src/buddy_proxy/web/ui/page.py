"""管理 UI：页面与前端静态文件的 serve。

页面源码在 ``web/static/``：index.html（骨架）+ style.css + 前端 JS
（公共 helper / 图表 / 模型与顺序 / 通道面板 / 启动入口）。仍是零依赖、
无外链 CDN——拆成多文件只是为了源码可维护，serve 时一一显式路由（白名单，不做目录
遍历式的 StaticFiles mount）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from buddy_proxy.core.state import app

_STATIC = Path(__file__).resolve().parent.parent / "static"

# 白名单静态文件 → Content-Type。页面随代码更新，一律 no-cache。
_STATIC_FILES = {
    "style.css": "text/css; charset=utf-8",
    **{name: "text/javascript; charset=utf-8" for name in (
        "ui_format.js", "ui_api.js",
        "benefits_accounts.js", "benefits_checkin.js", "benefits_kimi.js",
        "benefits_codex.js", "benefits_panels.js", "benefits_antigravity.js",
        "benefits_qoder.js", "benefits_trae.js", "benefits_codebuddy.js",
        "benefits_quota.js", "benefits_nav.js", "benefits_dumate.js", "benefits.js",
        "chart_tooltips.js", "charts.js", "chart_models.js", "chart_credits.js",
        "chart_latency.js", "chart_log.js", "chart_mix.js",
        "app_state.js", "app_model_actions.js", "app_order.js", "app_order_rows.js",
        "app.js", "app_run_test.js",
    )},
}


# 二进制静态文件（图标）：一律原样 sendfile 语义回传 bytes，不做文本解码；
# 浏览器标签页会以各种尺寸取它，被 UTF-8 解码再编码只会白白多一次拷贝。
_STATIC_BYTES = {
    "favicon.svg": "image/svg+xml",
}


def _read_static(name: str) -> str:
    try:
        return (_STATIC / name).read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=404, detail={"error": {"message": f"{name} 不存在"}}) from exc


# 每次请求现读：CSS/JS 一直是这样，index.html 也照做——之前它只在 import 时读一次，
# 改完页面不重启就永远 serve 旧骨架，和下面 no-cache 的意图正好相反。
@app.get("/ui", response_class=HTMLResponse)
async def ui_page():
    # no-cache：页面随代码更新，别让浏览器拿旧缓存（管理页无性能顾虑）
    return HTMLResponse(content=_read_static("index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/ui/{name}")
async def ui_static(name: str):
    btype = _STATIC_BYTES.get(name)
    if btype is not None:
        try:
            blob = (_STATIC / name).read_bytes()
        except OSError as exc:
            raise HTTPException(
                status_code=404, detail={"error": {"message": f"{name} 不存在"}}) from exc
        # no-cache 同上：换图标后不用重启也能立即生效
        return Response(content=blob, media_type=btype, headers={"Cache-Control": "no-cache"})
    ctype = _STATIC_FILES.get(name)
    if ctype is None:
        raise HTTPException(status_code=404, detail={"error": {"message": "not found"}})
    return Response(
        content=_read_static(name),
        media_type=ctype,
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/")
async def ui_root():
    return RedirectResponse(url="/ui")
