"""``/media`` static mount with optional on-demand transcoding.

Plain ``StaticFiles`` is all the mount needs *except* for one flag: a request
carrying ``?tc=1`` means "the browser already failed to decode this, hand me a
playable rendition". The frontend sets that flag from the ``error`` handler of
the ``<video>`` element, once per element, so the common path (H.264 files, or
second play of an HEVC file) is still a plain file send with Range support.

The transcode itself lives in ``backend/media_transcode.py``; this module only
decides when to ask for it and what to serve when it fails.
"""
import logging
import os
import stat
from urllib.parse import parse_qs

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from . import media_transcode

log = logging.getLogger("app.media")


class MediaStaticFiles(StaticFiles):
    """StaticFiles that can convert a video on the way out."""

    async def get_response(self, path, scope):
        if _wants_transcode(scope):
            response = await self._transcoded_response(path)
            if response is not None:
                return response
        return await super().get_response(path, scope)

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
