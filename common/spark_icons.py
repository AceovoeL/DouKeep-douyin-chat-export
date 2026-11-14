"""会话列表 / 聊天标题上的「火花」图标缓存。

抖音把火花图标放在静态图床上，文件名本身就说明了颜色（例如
``blue_normal.png``）。这里**沿用服务器给的原文件名**把它存到
``data/media/spark_icons/``，和卡片图一样随聊天记录留在本机，链接过期也不怕。

只允许白名单图床：这个地址会被服务端直接请求，不能拿页面里的任意 URL 去打通内网。
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse

from common.paths import MEDIA_DIR

log = logging.getLogger("app.media")

#: 图标目录名（相对 data/media/）
SPARK_ICON_DIR_NAME = "spark_icons"
#: 单张火花图的体积上限（正常只有几 KB）。
MAX_SPARK_ICON_BYTES = 1024 * 1024

_ICON_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")

#: 抖音界面素材常走的图床（火焰图标在 bytednsdoc 这个域）。
_ALLOWED_ROOT_HOSTS = (
    "bytednsdoc.com",
    "bytedance.com",
    "douyinpic.com",
    "douyinstatic.com",
    "byteimg.com",
    "bytecdn.cn",
    "ibytedtos.com",
    "volces.com",
    "dailygn.com",
)
_ALLOWED_HOST_SUFFIXES = tuple(f".{host}" for host in _ALLOWED_ROOT_HOSTS)


def spark_icon_dir() -> str:
    return os.path.join(MEDIA_DIR, SPARK_ICON_DIR_NAME)


def is_allowed_icon_url(url: object) -> bool:
    """只允许 http(s) 的抖音图床链接（这个地址会变成服务端请求）。"""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    host = (urlparse(url).hostname or "").lower()
    if not _SAFE_HOST_RE.match(host):
        return False
    return host in _ALLOWED_ROOT_HOSTS or host.endswith(_ALLOWED_HOST_SUFFIXES)


def fnv1a_32(text: str) -> str:
    """链接 → 8 位十六进制哈希（只在文件名不安全时兜底用）。"""
    value = 0x811C9DC5
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 0x01000193) & 0xFFFFFFFF
    return f"{value:08x}"


def spark_icon_filename(url: str) -> str:
    """链接 → 本地文件名。

    正常情况直接沿用服务器返回的文件名（``blue_normal.png``）；只有名字里带
    奇怪字符、没有扩展名或带路径穿越时才换成哈希名。
    """
    name = os.path.basename(urlparse(url).path)
    stem, ext = os.path.splitext(name)
    if (
        stem
        and ext.lower() in _ICON_EXTS
        and _SAFE_NAME_RE.match(name)
        and ".." not in name
    ):
        # 严格用服务器给的文件名（大小写、.jpeg 都不改），方便对照
        return name
    return f"spark_{fnv1a_32(url)}.png"


def find_spark_icon(url: object) -> str | None:
    """本地已有这份图时返回相对 media 的路径（``spark_icons/<文件名>``）。"""
    if not is_allowed_icon_url(url):
        return None
    filename = spark_icon_filename(url)
    target = os.path.join(spark_icon_dir(), filename)
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return f"{SPARK_ICON_DIR_NAME}/{filename}"
    return None


def save_spark_icon(url: object) -> str | None:
    """下载并保存火花图标；返回相对路径，失败返回 None（不抛给采集流程）。"""
    if not is_allowed_icon_url(url):
        return None
    existing = find_spark_icon(url)
    if existing:
        return existing

    data = _fetch_icon(url)
    if not data:
        return None

    directory = spark_icon_dir()
    filename = spark_icon_filename(url)
    target = os.path.join(directory, filename)
    try:
        os.makedirs(directory, exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(data)
    except OSError as exc:
        log.info("火花图标保存失败: %s (%s)", _short(url), type(exc).__name__)
        return None
    return f"{SPARK_ICON_DIR_NAME}/{filename}"


def _fetch_icon(url: str) -> bytes | None:
    import urllib.error
    import urllib.request

    try:
        from common.tls import client_context

        context = client_context()
    except Exception:  # pragma: no cover - 证书上下文建不起来时退回系统默认
        context = None
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.douyin.com/",
    })
    try:
        with urllib.request.urlopen(request, timeout=15, context=context) as resp:
            data = resp.read(MAX_SPARK_ICON_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.info("火花图标下载失败: %s (%s)", _short(url), type(exc).__name__)
        return None
    if not data or len(data) > MAX_SPARK_ICON_BYTES:
        return None
    return data


def _short(url: str) -> str:
    return url.split("?", 1)[0][-60:]
