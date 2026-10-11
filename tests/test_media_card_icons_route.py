"""``/media/card_icons/...`` 这条"没有就补拉一次"的路。

前端把卡片图统一写成 ``/media/card_icons/<哈希><扩展名>?url=<原链接>``（见
frontend/src/lib/media.js 的 iconSrc）。这里直接调 ``MediaStaticFiles`` 的响应逻辑，
不起整个服务：本地存过就发本地那份，没存过就拿 ``?url=`` 去补拉，补不到就交给静态
文件处理（最后是 404，前端把碎图藏起来）。
"""
import os

import pytest

from backend.media_files import MediaStaticFiles, _card_icon_source
from common import card_icons

from tests.test_card_icons import DOUBAO_COVER, INVITE_ICON


@pytest.fixture()
def media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(card_icons, "MEDIA_DIR", str(tmp_path))
    (tmp_path / card_icons.CARD_ICON_DIR_NAME).mkdir(parents=True)
    return tmp_path


def _scope(path: str, url: str = "") -> dict:
    from urllib.parse import urlencode

    query = urlencode({"url": url}).encode("latin-1") if url else b""
    return {
        "type": "http",
        "path": f"/media/{path}",
        "query_string": query,
        "headers": [],
    }


def test_serves_the_archived_file(media_dir):
    target = media_dir / card_icons.CARD_ICON_DIR_NAME / "e5e49455.webp"
    target.write_bytes(b"RIFF....WEBP" + b"x" * 100)

    files = MediaStaticFiles(directory=str(media_dir))
    response = _run(files._card_icon_response("card_icons/e5e49455.webp", _scope("card_icons/e5e49455.webp", INVITE_ICON)))
    assert response is not None
    assert os.path.basename(response.path) == "e5e49455.webp"


def test_windows_style_backslash_path_is_recognised(media_dir):
    """Starlette 传进来的 path 在 Windows 上是 ``card_icons\\x.webp``（反斜杠）。

    以前这里只认正斜杠，于是 Windows 上「本地没有就补拉一次」这条分支**从来没执行过** ——
    采集时没存下的卡片图直接 404。用户 2026-10-11 报的火花图标取不到就是这个原因。
    """
    target = media_dir / card_icons.CARD_ICON_DIR_NAME / "e5e49455.webp"
    target.write_bytes(b"RIFF....WEBP" + b"x" * 100)

    files = MediaStaticFiles(directory=str(media_dir))
    scope = _scope("card_icons/whatever.webp", INVITE_ICON)
    # 名字对不上也没关系：按 ?url= 的哈希去找本地那份
    response = _run(files._card_icon_response("card_icons\\whatever.webp", scope))
    assert response is not None
    assert os.path.basename(response.path) == "e5e49455.webp"
    # 反斜杠路径走「已存好」这条路，不该触发下载
    assert _run(files._card_icon_response("card_icons\\nope.webp",
                                         _scope("card_icons/nope.webp", DOUBAO_COVER))) is None


def test_fetches_once_when_missing_then_serves_it(media_dir, monkeypatch):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return b"RIFF....WEBP" + b"y" * 500, "image/webp"

    monkeypatch.setattr(card_icons, "_fetch_icon", fake_fetch)
    files = MediaStaticFiles(directory=str(media_dir))
    scope = _scope("card_icons/e5e49455.webp", INVITE_ICON)
    first = _run(files._card_icon_response("card_icons/e5e49455.webp", scope))
    assert first is not None and calls == [INVITE_ICON]
    # 第二次直接命中本地文件，不再上网
    second = _run(files._card_icon_response("card_icons/e5e49455.webp", scope))
    assert second is not None and calls == [INVITE_ICON]


def test_gives_up_when_the_link_expired(media_dir, monkeypatch):
    monkeypatch.setattr(card_icons, "_fetch_icon", lambda url: (None, ""))
    files = MediaStaticFiles(directory=str(media_dir))
    assert _run(files._card_icon_response(
        "card_icons/86ef82f2.jpg", _scope("card_icons/86ef82f2.jpg", DOUBAO_COVER))) is None


def test_other_paths_are_left_to_static_files(media_dir):
    files = MediaStaticFiles(directory=str(media_dir))
    assert _run(files._card_icon_response("images/1.jpg", _scope("images/1.jpg", INVITE_ICON))) is None
    assert _run(files._card_icon_response("images\\1.jpg", _scope("images/1.jpg", INVITE_ICON))) is None


def test_rejects_urls_that_are_not_douyin_images():
    assert _card_icon_source(_scope("card_icons/x.webp", "http://127.0.0.1:8000/api/stats")) == ""
    assert _card_icon_source(_scope("card_icons/x.webp", "http://evil.example.com/x.webp")) == ""
    assert _card_icon_source(_scope("card_icons/x.webp")) == ""
    assert _card_icon_source(_scope("card_icons/x.webp", INVITE_ICON)) == INVITE_ICON


# 精灵那套素材（喂食卡的封面背景 / 食物）走 *.dailygn.com 这个图床，
# 文件名沿用官方原名（like2.png / hotstick_icon.png），所以这里两种都钉一下。
FEED_BACKGROUND = (
    "https://lf3-creative.dailygn.com/obj/dino/pet_elf/images/feed_card/like2.png")


def test_petelf_asset_host_is_allowed():
    """dailygn.com 是抖音精灵素材的图床，得放行，否则喂食卡封面永远补不回来。"""
    assert _card_icon_source(_scope("card_icons/x.png", FEED_BACKGROUND)) == FEED_BACKGROUND
    assert card_icons.is_allowed_icon_url(FEED_BACKGROUND) is True
    # 伪装域名不能过（后缀必须是真域名，不是拿它拼出来的假域名）
    assert card_icons.is_allowed_icon_url("https://dailygn.com.evil.com/x.png") is False
    assert card_icons.is_allowed_icon_url("http://127.0.0.1/x.png") is False


def test_petelf_asset_is_served_under_its_official_name(media_dir):
    """存盘时沿用官方文件名；前端还是按哈希名请求，后端靠 ?url= 找到那一份。"""
    target = media_dir / card_icons.CARD_ICON_DIR_NAME / "like2.png"
    target.write_bytes(b"\x89PNG" + b"x" * 100)

    files = MediaStaticFiles(directory=str(media_dir))
    # 前端算出来的请求路径是哈希名（见 frontend/src/lib/media.js 的 iconSrc），
    # 本地存的却是官方名 —— 这条 ?url= 兜底路径就是让两边对上的地方。
    request_path = f"{card_icons.CARD_ICON_DIR_NAME}/{card_icons.fnv1a_32(FEED_BACKGROUND)}.png"
    response = _run(files._card_icon_response(request_path, _scope(request_path, FEED_BACKGROUND)))
    assert response is not None
    assert os.path.basename(response.path) == "like2.png"


def _run(coro):
    import asyncio

    return asyncio.run(coro)
