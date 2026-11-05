"""管理 UI：页面与前端静态文件的 serve。

页面源码在 ``web/static/``：index.html（骨架）+ style.css + 前端 JS
（公共 helper / 图表 / 模型与顺序 / 通道面板 / 启动入口）。仍是零依赖、
无外链 CDN——拆成多文件只是为了源码可维护。

**白名单按目录派生，不再手抄清单。** 以前这里是一个 ``_STATIC_FILES`` 字典，
每加一个前端文件都要来登记一次，忘了就是 404——而且只在浏览器里才炸（``/ui``
本身照常返回 200，页面只是少一段脚本）。白名单能给出的保证其实只有后半句：
「只 serve ``static/`` 里真实存在、且后缀已知的文件」。前半句用目录判断就够，
不需要人肉同步：

- 只认 ``_TEXT_TYPES`` / ``_BINARY_TYPES`` 里登记的后缀（新增前缀要显式决定
  编码方式，不会因为放了个 ``.map`` 或编辑器临时文件就暴露出去）；
- 名字里出现路径分隔符或是 ``..`` 一律 404——不依赖 StaticFiles 的规范化行为；
- 文本按 UTF-8 读出（现读，配合 no-cache），图标按字节原样回传。

脚本**加载顺序**仍由 ``index.html`` 的 ``<script>`` 顺序决定（无构建工具下的
既定约定，见 aoci 里 index.html 的说明）；``tests/test_ui_admin.py`` 会校验
「页面引用的每个脚本都存在」且「每个前端脚本都被页面引用」，两侧漏一个都会红。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from buddy_proxy.core.state import app

_STATIC = Path(__file__).resolve().parent.parent / "static"

#: 文本后缀 → Content-Type。页面随代码更新，一律 no-cache。
_TEXT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
}

#: 二进制后缀（图标）：一律原样 sendfile 语义回传 bytes，不做文本解码；浏览器
#: 标签页会以各种尺寸取它，被 UTF-8 解码再编码只会白白多一次拷贝。
_BINARY_TYPES = {
    ".svg": "image/svg+xml",
}


def _read_static(name: str) -> str:
    try:
        return (_STATIC / name).read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=404, detail={"error": {"message": f"{name} 不存在"}}) from exc


def _static_path(name: str) -> Path:
    """把 URL 里的文件名映射成 ``static/`` 下的真实文件，否则 404。

    路由参数 ``{name}`` 本身不含 ``/``，但仍显式挡掉分隔符与 ``..``：别把
    「Starlette 会先做路径规范化」当成唯一防线（不同 ASGI 服务器/代理的解码
    时机不一样，``%2F``、``%2e%2e`` 是否已经还原取决于部署方式）。
    """
    if not name or "/" in name or "\\" in name or name.startswith(".") or ".." in name:
        raise HTTPException(status_code=404, detail={"error": {"message": "not found"}})
    path = _STATIC / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail={"error": {"message": f"{name} 不存在"}})
    return path


# 每次请求现读：CSS/JS 一直是这样，index.html 也照做——之前它只在 import 时读一次，
# 改完页面不重启就永远 serve 旧骨架，和下面 no-cache 的意图正好相反。
@app.get("/ui", response_class=HTMLResponse)
async def ui_page():
    # no-cache：页面随代码更新，别让浏览器拿旧缓存（管理页无性能顾虑）
    return HTMLResponse(content=_read_static("index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/ui/{name}")
async def ui_static(name: str):
    path = _static_path(name)
    btype = _BINARY_TYPES.get(path.suffix)
    if btype is not None:
        # 图标按字节回传；no-cache 同理：换图标后不用重启也能立即生效
        try:
            blob = path.read_bytes()
        except OSError as exc:
            raise HTTPException(
                status_code=404, detail={"error": {"message": f"{name} 不存在"}}) from exc
        return Response(content=blob, media_type=btype, headers={"Cache-Control": "no-cache"})
    ctype = _TEXT_TYPES.get(path.suffix)
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
