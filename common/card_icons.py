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

例外是「精灵」那套素材（``*.dailygn.com`` 图床，如喂食卡的背景图和食物图）：抖音给它们
起的文件名本身就是它的名字（``like2.png`` / ``hotstick_icon.png``），存档时**沿用官方
原名**（见 :func:`icon_filename`），打开目录一眼就知道哪张是哪张。
"""
from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import urlparse

from common.paths import MEDIA_DIR

log = logging.getLogger("app.media")

CARD_ICON_DIR_NAME = "card_icons"
#: 「会被落成普通消息」的表情 aweType：抖音给的载荷和别的表情一模一样（display_name
#: 是「笑死」「续火花」这类文字、图在 ``url.url_list[0]``），但采集端以前不在名单里，
#: 于是落库成了普通消息（msg_type=0）、图也没下。
#:
#: 挑出来单列是因为补图用：这些老数据只能靠载荷里的 ``aweType`` 粗筛出来
#: （见 ``extractor/media_backfill.py``），别的表情落库就是 msg_type=2，不用它。
#: 515 / 517 / 519 / 520 与阅读端 ``LOOSE_EMOJI_AWES`` 是同一批；519 是「小火人」
#: （monster emoji），520 是精灵自己发的表情（用户 2026-10-11 反馈它一直没被存下来）。
LOOSE_EMOJI_AWE_TYPES = frozenset({"515", "517", "519", "520"})

#: 表情包 / 贴纸的 aweType（采集端、转发记录、阅读端都用这一份名单）。
#: 比较时统一用 str(aweType)，因为接口有时给数字、有时给字符串。
EMOJI_AWE_TYPES = frozenset({
    "500", "501", "507", "508", "510", "514", "516",
}) | LOOSE_EMOJI_AWE_TYPES
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
         ".bytedance.com", ".bytecdn.cn", ".ibytedtos.com", ".volces.com",
         # 精灵（小火人 / 合养精灵）那套素材走这个图床，喂食卡的背景和食物图都在上面。
         ".dailygn.com")
    ) or host in ("douyinpic.com", "byteimg.com", "douyin.com", "dailygn.com")


def icon_extension(url: str, content_type: str = "") -> str:
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext == ".jpeg":
        return ".jpg"
    if ext in _ICON_EXTS:
        return ext
    mapped = _EXT_FROM_CONTENT_TYPE.get((content_type or "").split(";")[0].strip().lower())
    return mapped or ".webp"


def icon_filename(url: str, content_type: str = "") -> str:
    """链接 → 存档文件名。

    两种命名：
    * **官方原名**（精灵素材，见 :func:`official_asset_name`）：``like2.png`` 就存成
      ``like2.png``，和抖音自己叫的一样，翻目录时一眼认得出。
    * 其余一律按链接哈希去重（同一个人被反复拉进同一个群，群头像只存一份）。
    """
    official = official_asset_name(url)
    if official:
        return official
    return fnv1a_32(url) + icon_extension(url, content_type)


def official_asset_name(url: str) -> str:
    """精灵素材的官方文件名；不是这套素材则返回空串。

    抖音给「小火人 / 合养精灵」那套素材起的名字本身就是它的身份（喂食卡的背景
    ``like2.png``、食物 ``hotstick_icon.png``），所以这几个图床上的图**沿用官方原名**
    存盘（用户 2026-10-11 要求）。别的图还是走哈希，免得两个卡片用同一个 <img> 名。
    """
    if not isinstance(url, str):
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not (host == "dailygn.com" or host.endswith(".dailygn.com")):
        return ""
    name = os.path.basename(parsed.path)
    if not name or os.path.splitext(name)[1].lower() not in _ICON_EXTS:
        return ""
    # 只认「目录/名字」这种干净的样子，别把带奇怪字符的东西当文件名写进磁盘。
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return ""
    return name


def icon_proxy_url(url: object) -> str:
    """卡片图应该用的地址：本地有档案就命中，没有时后端会补拉一次。"""
    if not is_allowed_icon_url(url):
        return ""
    return f"/media/{CARD_ICON_DIR_NAME}/{icon_filename(url)}"


# ── 哪张卡片的图该存下来 ────────────────────────────────────────────────────
#: 豆包分享卡（见 frontend/src/lib/cardKinds.js 的 getMusicCard）
MUSIC_CARD_AWE_TYPE = 6001
INVITE_TYPE_DESCS = ("群聊邀请", "群邀请")

#: 「聊天火花」关系卡（更改为姐妹 / 兄弟 / 伴侣 / 死党火花）的两个类型号。
#: 这几种**共用同一张卡片外观**（卡片底纹 + 底部「聊天火花」条，由客户端画，载荷里没有），
#: 只有上面那颗火花图标不一样 —— 图标就在载荷里，所以按卡片类型把它存下来。
#: 别的小火人 / 合养精灵卡是另一个页脚（「合养精灵」），各有各的外观，不在这里。
FIRE_CARD_KEY = "msg_fire_invitation"
FIRE_CARD_TYPES = (
    "im_msg_fire_invitation_send",           # 邀请更改为姐妹/兄弟/伴侣/死党火花
    "im_msg_fire_invitation_accept_memory",  # 你们已更改为 XX 火花（纪念卡）
)

#: 「喂精灵」卡（aweType=110274）。它的封面由**两张图叠出来**：一张背景（小火人 + 爱心
#: 光晕）加一张食物图居中盖在上面，下面是项目自带的 assets/feed-card.png。两张图都在
#: 载荷里，链接过期就没了，所以两张都要存（见 :func:`feed_card_image_urls`）。
FEED_CARD_TYPES = ("im_msg_fire_invitation_feeding",)

#: 「陪伴天数」卡（aweType=110270 · card_key=msg_daily · card_type=im_msg_daily_fire）。
#: 这张卡整张由前端自己画，它自己不带封面图；载荷里唯一那张图是中间那颗**火花图标**
#: （``content_top`` 组件列表里的 im-image）。
#: 项目自带一份 ``assets/fire.png``，所以这张图**不是非存不可** —— 但存下来没坏处：
#: 万一以后自带素材要换，本地这份还是当时的原图（见 :func:`card_icon_url`）。
DAILY_FIRE_AWE_TYPE = 110270
DAILY_FIRE_CARD_TYPES = ("im_msg_daily_fire",)


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


def is_fire_card(cj) -> bool:
    """「聊天火花」关系卡（更改为姐妹 / 兄弟 / 伴侣 / 死党火花）。"""
    if not isinstance(cj, dict):
        return False
    patch = cj.get("im_dynamic_patch")
    if not isinstance(patch, dict):
        return False
    if str(patch.get("card_key")) != FIRE_CARD_KEY:
        return False
    return str(patch.get("card_type")) in FIRE_CARD_TYPES


def _fire_layout(cj) -> dict | None:
    """关系卡的排版（``im_dynamic_patch.raw_data``，可能是再次编码的字符串）。"""
    patch = cj.get("im_dynamic_patch") if isinstance(cj, dict) else None
    raw = patch.get("raw_data") if isinstance(patch, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw if isinstance(raw, dict) else None


def _fire_icon_url(cj) -> str:
    """关系卡上那颗火花的原图链接。

    邀请卡在 ``top.content``（字符串）；接受纪念卡在 ``top@1.content.flame_icon``，
    老载荷会把它放在 ``top.content.flame_icon``。
    """
    layout = _fire_layout(cj)
    if not layout:
        return ""
    top1 = layout.get("top@1")
    content = top1.get("content") if isinstance(top1, dict) else None
    if isinstance(content, dict):
        url = _image_url(content.get("flame_icon")) or _image_url(content.get("icon"))
        if url:
            return url
    top = layout.get("top")
    if isinstance(top, dict):
        direct = top.get("content")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        nested = top.get("top_content")
        if isinstance(nested, dict):
            url = _image_url(nested.get("flame_icon")) or _image_url(nested.get("content"))
            if url:
                return url
        content = top.get("content")
        if isinstance(content, dict):
            url = _image_url(content.get("flame_icon")) or _image_url(content.get("icon"))
            if url:
                return url
    return ""


def is_feed_card(cj) -> bool:
    """「喂精灵」卡（aweType=110274 / card_type=im_msg_fire_invitation_feeding）。"""
    if not isinstance(cj, dict):
        return False
    patch = cj.get("im_dynamic_patch")
    if not isinstance(patch, dict):
        return False
    if str(patch.get("card_key")) != FIRE_CARD_KEY:
        return False
    return str(patch.get("card_type")) in FEED_CARD_TYPES


def _whole_card_ui(layout: dict) -> dict:
    ui = layout.get("whole_card") if isinstance(layout, dict) else None
    ui = ui.get("ui_info") if isinstance(ui, dict) else None
    return ui if isinstance(ui, dict) else {}


def feed_card_image_urls(cj) -> tuple[str, str]:
    """喂食卡封面的两张图 ``(背景图, 食物图)``；缺哪张哪张是空串。

    背景在 ``whole_card.ui_info.background_image@light``（``@dark`` 是深色模式那张，
    优先用 ``@light``）；食物在 ``top.content`` —— 卡片里那根辣条就是它，居中盖在
    背景上（摆放见 ``frontend/src/lib/feedCard.js``）。
    """
    if not is_feed_card(cj):
        return "", ""
    layout = _fire_layout(cj) or {}
    ui = _whole_card_ui(layout)
    background = (
        _image_url(ui.get("background_image@light"))
        or _image_url(ui.get("background_image@dark"))
        or _image_url(ui.get("background_image"))
    )
    top = layout.get("top")
    food = _image_url(top.get("content")) if isinstance(top, dict) else ""
    return background, food


def is_daily_fire_card(cj) -> bool:
    """「陪伴天数」卡（aweType=110270 / card_type=im_msg_daily_fire）。

    和「获得火花见面礼」（aweType=110408）共用 ``card_key=msg_daily``，靠 aweType 区分；
    两者载荷里都有 card_type，认哪个都行，但老样本偶尔缺 card_type，所以一并认 aweType。
    """
    if not isinstance(cj, dict):
        return False
    patch = cj.get("im_dynamic_patch")
    card_type = str(patch.get("card_type")) if isinstance(patch, dict) else ""
    return card_type in DAILY_FIRE_CARD_TYPES or str(cj.get("aweType")) == str(DAILY_FIRE_AWE_TYPE)


def daily_fire_icon_url(cj) -> str:
    """「陪伴天数」卡上那颗火花图（``content_top`` 组件列表里的 im-image）。

    这一行固定是 ``[前文文字] [火花图] [数字滚轮 天数] [后文文字]``，所以从组件列表里
    找第一个 im-image 就是它。找不到返回空串（前端会退回项目自带的 assets/fire.png，
    卡片照常显示）。
    """
    if not is_daily_fire_card(cj):
        return ""
    layout = _fire_layout(cj) or {}
    content = layout.get("content_top")
    content = content.get("content") if isinstance(content, dict) else None
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict) or str(item.get("type")) != "im-image":
            continue
        url = _image_url(item.get("content"))
        if url:
            return url
    return ""


def card_icon_urls(cj) -> list[str]:
    """这张卡要存下来的**所有**图（大多数卡只有一张，喂食卡有两张）。"""
    if isinstance(cj, dict) and is_feed_card(cj):
        return [url for url in feed_card_image_urls(cj) if url]
    single = card_icon_url(cj)
    return [single] if single else []


def card_icon_url(cj) -> str:
    """卡片上那张图的原始链接；不是认识的卡片则返回空串。

    群邀请卡用 ``icon``（``aweme_invite_card.group_icon`` 是同一张），
    豆包卡用 ``icon``（没有时退回 ``cover_url``），
    聊天火花卡用那颗火花图标（``top`` / ``top@1`` 里的图片），
    喂食卡用它的背景图（另一张食物图见 :func:`feed_card_image_urls`）。
    """
    if not isinstance(cj, dict):
        return ""
    if is_invite_card(cj):
        card = cj.get("aweme_invite_card")
        card = card if isinstance(card, dict) else {}
        return _image_url(cj.get("icon")) or _image_url(card.get("group_icon"))
    if is_music_card(cj):
        return _image_url(cj.get("icon")) or _image_url(cj.get("cover_url"))
    if is_fire_card(cj):
        return _fire_icon_url(cj)
    if is_feed_card(cj):
        return feed_card_image_urls(cj)[0]
    if is_daily_fire_card(cj):
        return daily_fire_icon_url(cj)
    return ""


def find_card_icon(url: object) -> str | None:
    """本地已有这份图时返回相对 media 的路径（``card_icons/<文件>``）。"""
    if not is_allowed_icon_url(url):
        return None
    directory = card_icon_dir()
    if not os.path.isdir(directory):
        return None
    # 精灵素材按官方原名找；别的按哈希前缀找（存的时候也可能带了响应头猜出来的扩展名）。
    official = official_asset_name(url)
    if official:
        target = os.path.join(directory, official)
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            return f"{CARD_ICON_DIR_NAME}/{official}"
        return None
    base = fnv1a_32(url)
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
    # 精灵素材沿用官方原名；链接里看不出格式（没有扩展名 / 是 .image 这类）时按响应头猜。
    name = official_asset_name(url) or f"{fnv1a_32(url)}{icon_extension(url, content_type)}"
    rel = f"{CARD_ICON_DIR_NAME}/{name}"
    target = os.path.join(MEDIA_DIR, rel)
    # 重名不再覆盖：同一个官方文件名就意味着同一份素材，直接用它下载好的那份。
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return rel
    with open(target, "wb") as handle:
        handle.write(data)
    return rel


def save_card_icons(cj) -> list[str]:
    """把这张卡要存的图**全部**下载存好，返回相对路径（失败的那些跳过）。

    大多数卡只有一张图，喂食卡有两张（背景 + 食物）—— 两张都是带签名的临时链接，
    所以两张都得落地，少存一张就等着过期变碎图。
    """
    saved = []
    for url in card_icon_urls(cj):
        try:
            rel = save_card_icon(url)
        except OSError:
            rel = None
        if rel:
            saved.append(rel)
    return saved


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
