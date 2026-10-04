"""卡片自带图片的落地缓存（群邀请卡的群头像、豆包卡的封面）。

抖音给这些图的是一条**带签名的临时链接**（`x-expires` 一般 180 天），群邀请卡那张
更是 http 的 byteimg 链接。归档时如果只顾着存消息、没把图拉下来，等签名过期，
卡片上就只剩文字 —— 用户 2026-10-04 报的豆包卡封面就是这样（5 月的消息、10 月再看
图已经 403）。

所以这类图按「链接的哈希」存到 ``data/media/card_icons/``：

* 采集端碰到卡片就顺手下一份（``extractor/web_scraper.py`` 调 :func:`save_card_icon`）；
* 阅读端用 :func:`icon_proxy_url` 走 ``/media/card-icons/...`` 取图，后端发现本地没有
  并且链接还没过期时会补拉一次（``backend/media_files.py``）—— 于是老库里那些
  还没过期的链接也能补回来。

存档键用哈希而不是 server_id：同一个人被反复拉进同一个群，群头像只该存一份。
"""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse

from common.paths import MEDIA_DIR

log = logging.getLogger("app.media")

CARD_ICON_DIR_NAME = "card_icons"
#: 表情包 / 贴纸的 aweType（采集端、转发记录、阅读端都用这一份名单）。
#: 519 是「小火人」（monster emoji）：载荷里 display_name 是「笑死」「续火花」，
#: 图在 url.url_list[0]，卡片形状和别的表情一模一样。比较时统一用 str(aweType)，
#: 因为接口有时给数字、有时给字符串。
EMOJI_AWE_TYPES = frozenset({"500", "501", "507", "508", "510", "514", "516", "519"})
#: 只认这几种扩展名，别的当 webp 处理（抖音的群头像基本是 webp）。
_ICON_EXTS = (".jpg", ".png", ".webp", ".gif", ".bmp")
#: 单张卡片图的体积上限，超过就别往库里塞（正常几 KB ~ 200KB）。
MAX_CARD_ICON_BYTES = 2 * 1024 * 1024
_EXT_FROM_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}
_SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")


def fnv1a_32(text: str) -> str:
    """链接 → 8 位十六进制哈希。

    必须和前端 ``iconSrc()``（frontend/src/lib/media.js）算出来的一样：阅读端拿
    ``/media/card_icons/<哈希>.<扩展名>?url=…`` 取图，采集端存的是同一个名字，
    两边对不上就会白拉一次网。前端没有同步的 md5，所以这边也用 FNV-1a。
    """
    value = 0x811C9DC5
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 0x01000193) & 0xFFFFFFFF
    return f"{value:08x}"


def card_icon_dir() -> str:
    return os.path.join(MEDIA_DIR, CARD_ICON_DIR_NAME)


def is_allowed_icon_url(url: object) -> bool:
    """只允许 http(s) 的抖音图床链接 —— 这个接口会把 URL 变成服务端请求。"""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    host = urlparse(url).hostname or ""
    if not _SAFE_HOST_RE.match(host):
        return False
    return host.endswith(
        (".douyinpic.com", ".byteimg.com", ".douyin.com", ".douyinstatic.com",
         ".bytedance.com", ".bytecdn.cn", ".ibytedtos.com", ".volces.com")
    ) or host in ("douyinpic.com", "byteimg.com", "douyin.com")


def icon_extension(url: str, content_type: str = "") -> str:
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext == ".jpeg":
        return ".jpg"
    if ext in _ICON_EXTS:
        return ext
    mapped = _EXT_FROM_CONTENT_TYPE.get((content_type or "").split(";")[0].strip().lower())
    return mapped or ".webp"


def icon_filename(url: str) -> str:
    """链接 → 存档文件名（哈希去重，同图不同链接也能共用一份）。"""
    return fnv1a_32(url) + icon_extension(url)


def icon_proxy_url(url: object) -> str:
    """卡片图应该用的地址：本地有档案就命中，没有时后端会补拉一次。"""
    if not is_allowed_icon_url(url):
        return ""
    return f"/media/{CARD_ICON_DIR_NAME}/{icon_filename(url)}"


# ── 哪张卡片的图该存下来 ────────────────────────────────────────────────────
#: 豆包分享卡（见 frontend/src/lib/cardKinds.js 的 getMusicCard）
MUSIC_CARD_AWE_TYPE = 6001
INVITE_TYPE_DESCS = ("群聊邀请", "群邀请")


def image_url(value) -> str:
    """抖音各种图片字段的统一取图方式：字符串，或 ``{url_list:[...]}``。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        urls = value.get("url_list")
        if isinstance(urls, list) and urls and isinstance(urls[0], str):
            return urls[0].strip()
    return ""


def _image_url(value) -> str:
    return image_url(value)


def _invite_conv_id(cj: dict) -> str:
    card = cj.get("aweme_invite_card")
    card = card if isinstance(card, dict) else {}
    for key in ("conversation_id", "conversation_short_id"):
        value = card.get(key)
        if value not in (None, ""):
            return str(value)
    event = cj.get("event")
    params = event.get("param") if isinstance(event, dict) else None
    if isinstance(params, dict) and params.get("conversation_id"):
        return str(params["conversation_id"])
    return ""


def is_invite_card(cj) -> bool:
    """群邀请卡：type_desc 写着「群聊邀请」并且带着目标群的会话 id。"""
    if not isinstance(cj, dict):
        return False
    desc = cj.get("type_desc")
    desc = desc.strip() if isinstance(desc, str) else ""
    return bool(_invite_conv_id(cj)) and (desc in INVITE_TYPE_DESCS or (desc and "群" in desc))


def is_music_card(cj) -> bool:
    """豆包分享卡（aweType=6001）。"""
    if not isinstance(cj, dict):
        return False
    return str(cj.get("aweType")) == str(MUSIC_CARD_AWE_TYPE)


def card_icon_url(cj) -> str:
    """卡片上那张图的原始链接；不是认识的卡片则返回空串。

    群邀请卡用 ``icon``（``aweme_invite_card.group_icon`` 是同一张），
    豆包卡用 ``icon``（没有时退回 ``cover_url``）。
    """
    if not isinstance(cj, dict):
        return ""
    if is_invite_card(cj):
        card = cj.get("aweme_invite_card")
        card = card if isinstance(card, dict) else {}
        return _image_url(cj.get("icon")) or _image_url(card.get("group_icon"))
    if is_music_card(cj):
        return _image_url(cj.get("icon")) or _image_url(cj.get("cover_url"))
    return ""


def find_card_icon(url: object) -> str | None:
    """本地已有这份图时返回相对 media 的路径（``card_icons/<文件>``）。"""
    if not is_allowed_icon_url(url):
        return None
    base = fnv1a_32(url)
    directory = card_icon_dir()
    if not os.path.isdir(directory):
        return None
    for name in os.listdir(directory):
        if name.startswith(base) and os.path.getsize(os.path.join(directory, name)) > 0:
            return f"{CARD_ICON_DIR_NAME}/{name}"
    return None


def save_card_icon(url: object) -> str | None:
    """下载并保存卡片图；返回相对路径。失败（一般是签名过期 403）返回 None。"""
    if not is_allowed_icon_url(url):
        return None
    existing = find_card_icon(url)
    if existing:
        return existing

    data, content_type = _fetch_icon(url)
    if not data:
        return None
    directory = card_icon_dir()
    os.makedirs(directory, exist_ok=True)
    # 链接里看不出格式（没有扩展名 / 是 .image 这类）时按响应头猜。
    rel = f"{CARD_ICON_DIR_NAME}/{fnv1a_32(url)}{icon_extension(url, content_type)}"
    with open(os.path.join(MEDIA_DIR, rel), "wb") as handle:
        handle.write(data)
    return rel


def _fetch_icon(url: str) -> tuple[bytes | None, str]:
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
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read(MAX_CARD_ICON_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.info("卡片图补拉失败（多半是签名过期）: %s (%s)", _short(url), type(exc).__name__)
        return None, ""
    if not data or len(data) > MAX_CARD_ICON_BYTES:
        return None, ""
    return data, content_type


def _short(url: str) -> str:
    return url.split("?", 1)[0][-60:]
