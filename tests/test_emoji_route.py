"""``/emoji`` 静态目录：前端一律按 ``<名字>.webp`` 问，磁盘上可能是 PNG。

抖音的表情里有一张（[加功德]）官方只给 PNG，下载时按真实格式存成了 ``加功德.png``
（见 common/emoji_pack.py），而前端地址是写死的 ``/emoji/加功德.webp`` —— 所以这里
必须能把 ``.webp`` 的请求对到 ``.png`` 那份文件上，并且 Content-Type 说实话。
没有就照旧 404（前端靠它把图片换回文字记号）。
"""
import asyncio
import os

import pytest
from starlette.exceptions import HTTPException

from backend.emoji_files import EmojiStaticFiles

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 200
WEBP = b"RIFF" + (1000).to_bytes(4, "little") + b"WEBP" + b"x" * 200


def _scope() -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": "/emoji/加功德.webp",
        "headers": [],
        "query_string": b"",
    }


def test_webp_request_finds_the_png_file(tmp_path):
    (tmp_path / "加功德.png").write_bytes(PNG)
    files = EmojiStaticFiles(directory=str(tmp_path))

    response = asyncio.run(files.get_response("加功德.webp", _scope()))

    assert response.status_code == 200
    assert os.path.basename(response.path) == "加功德.png"
    assert response.media_type == "image/png", "别把 png 说成 webp 发出去"


def test_webp_request_still_serves_webp(tmp_path):
    (tmp_path / "微笑.webp").write_bytes(WEBP)
    files = EmojiStaticFiles(directory=str(tmp_path))

    response = asyncio.run(files.get_response("微笑.webp", _scope()))

    assert os.path.basename(response.path) == "微笑.webp"
    assert response.media_type == "image/webp"


def test_missing_file_is_still_404(tmp_path):
    """资源包还没下载时就是这个 404：前端据此换回文字记号。"""
    files = EmojiStaticFiles(directory=str(tmp_path))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(files.get_response("微笑.webp", _scope()))

    assert exc.value.status_code == 404


def test_other_extensions_are_not_probed(tmp_path):
    """只有那几种图片扩展名才换来换去找，别的路径直接 404，不瞎猜。"""
    (tmp_path / "secret.png").write_bytes(PNG)
    files = EmojiStaticFiles(directory=str(tmp_path))

    with pytest.raises(HTTPException):
        asyncio.run(files.get_response("secret.txt", _scope()))
