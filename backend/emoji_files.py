"""``/emoji`` 这个静态目录：前端只按名字问，扩展名可能对不上。

前端的表情图片地址统一写成 ``/emoji/<名字>.webp``（见 frontend/src/lib/emojiAssets.js），
但抖音给的表情里有一张是 PNG（[加功德]），下载时按真实格式存成了 ``加功德.png``
（见 common/emoji_pack.py）。所以这里在找不到那个文件时，换成同名的其它图片扩展名
再找一次 —— 找到之后 Starlette 按真实文件名给 Content-Type，浏览器不会收到一张
「说是 webp、其实是 png」的图。

真的没有就照旧 404：资源包还没下载时，前端靠这个把图片换回原来的文字记号。

这里只包 ``get_response``（各版本 starlette 都是 async），不去改 ``lookup_path`` ——
那个函数在旧版是 async、新版是 sync，跟着改会在换依赖版本时炸。
"""
import os

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from common.emoji_pack import IMAGE_EXTS


class EmojiStaticFiles(StaticFiles):
    """按名字找图：请求的扩展名对不上时，换成同名的其它图片扩展名再找一次。"""

    async def get_response(self, path: str, scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise

        name, ext = os.path.splitext(path)
        ext = ext.lower()
        if ext not in IMAGE_EXTS:
            raise HTTPException(status_code=404)
        for other in IMAGE_EXTS:
            if other == ext:
                continue
            try:
                return await super().get_response(name + other, scope)
            except HTTPException:
                continue
        raise HTTPException(status_code=404)
