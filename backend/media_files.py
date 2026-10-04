"""``/media`` static mount with optional on-demand transcoding.

Plain ``StaticFiles`` is all the mount needs *except* for one flag: a request
carrying ``?tc=1`` means "the browser already failed to decode this, hand me a
playable rendition". The frontend sets that flag from the ``error`` handler of
the ``<video>`` element, once per element, so the common path (H.264 files, or
second play of an HEVC file) is still a plain file send with Range support.

The transcode itself lives in ``backend/media_transcode.py``; this module only
decides when to ask for it and what to serve when it fails.

卡片图（``/media/card_icons/<文件>?url=<原链接>``）走另一条特殊路径：本地没有
存档、链接又还没过期时顺手补拉一份（见 ``common/card_icons.py``）。抖音给的卡片图
是带签名的临时链接，光靠采集时存的那一份，老库里没存过的就永远是碎图。
"""
import logging
import os
import stat
from urllib.parse import parse_qs

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from common import card_icons

from . import media_transcode

log = logging.getLogger("app.media")


class MediaStaticFiles(StaticFiles):
    """StaticFiles that can convert a video on the way out."""

    async def get_response(self, path, scope):
        icon = await self._card_icon_response(path, scope)
        if icon is not None:
            return icon
        if _wants_transcode(scope):
            response = await self._transcoded_response(path)
            if response is not None:
                return response
        return await super().get_response(path, scope)

    async def _card_icon_response(self, path, scope):
        """卡片图：本地有就发本地那份，没有就补拉一次再发；失败返回 None（照旧 404）。"""
        if not str(path).startswith(card_icons.CARD_ICON_DIR_NAME + "/"):
            return None
        source = _card_icon_source(scope)
        if not source:
            return None
        existing = await run_in_threadpool(card_icons.find_card_icon, source)
        if not existing:
            existing = await run_in_threadpool(card_icons.save_card_icon, source)
        if not existing:
            return None
        target = os.path.join(card_icons.card_icon_dir(), os.path.basename(existing))
        if not os.path.isfile(target):
            return None
        # 同一个链接对应的文件是固定的，交给浏览器缓存一天；文件本身不会变。
        return FileResponse(target, headers={"Cache-Control": "public, max-age=86400"})


def _card_icon_source(scope) -> str:
    """从 ``?url=`` 取出原链接（只接受抖音图床的 http(s) 地址）。"""
    query = scope.get("query_string") or b""
    if not query:
        return ""
    try:
        params = parse_qs(query.decode("latin-1"), keep_blank_values=True)
    except (UnicodeDecodeError, ValueError):
        return ""
    raw = (params.get("url") or [""])[0]
    if not raw:
        return ""
    try:
        url = raw.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        url = raw
    return url if card_icons.is_allowed_icon_url(url) else ""

    async def _transcoded_response(self, path):
        """Serve a cached H.264 rendition, or ``None`` to fall through."""
        try:
            full_path, stat_result = await run_in_threadpool(self.lookup_path, path)
        except (HTTPException, OSError, ValueError):
            return None
        if stat_result is None or not stat.S_ISREG(stat_result.st_mode):
            return None

        result = await run_in_threadpool(media_transcode.ensure_transcoded, full_path)
        if not result.get("ok"):
            # Unsupported codec we cannot fix, ffmpeg missing, too long… —
            # serving the original is still better than a hard failure.
            log.warning("转码失败 %s: %s", full_path, result.get("error"))
            return None

        rendition = result.get("path")
        if not rendition or os.path.samefile(rendition, full_path):
            return None

        if result.get("converted"):
            log.info("已转码 %s → %s (%.2fs, %s)",
                     os.path.basename(full_path), os.path.basename(rendition),
                     result.get("seconds", 0.0), result.get("encoder", "?"))
        return FileResponse(rendition, media_type="video/mp4")


def _wants_transcode(scope) -> bool:
    """True when the request carries a ``tc`` query parameter."""
    query = scope.get("query_string") or b""
    if not query:
        return False
    try:
        params = parse_qs(query.decode("latin-1"), keep_blank_values=True)
    except (UnicodeDecodeError, ValueError):
        return False
    return "tc" in params
