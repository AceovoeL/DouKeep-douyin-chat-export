"""Download and decrypt Douyin IM attachments (images, emoji, voice, CENC keys).

Chat-media URLs are independent of merge-share JSON. Images/emoji/voice use the
payload's own origin URL + AES-GCM skey (or a plain CDN URL). Self-recorded
videos need a logged-in batch_play_info lookup — see video_downloader.
"""
import json
import logging
import os
import re

from common.paths import EMOJI_DIR, IMAGES_DIR, MEDIA_DIR, VIDEOS_DIR, VOICE_DIR
from common.tls import client_context

media_log = logging.getLogger("app.media")


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


_HEIF_BRANDS = {b"heic", b"heix", b"mif1", b"msf1", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"}
_MP4_BRANDS = {b"mp42", b"mp41", b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"avc1", b"M4V ", b"qt  "}
_EMOJI_TYPES = {"500", "501", "507", "508", "510", "514", "516", "519"}
_IMAGE_TYPES = {"2702", "2703", "2704"}
_MEDIA_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mpeg", ".m4a")
DIRECT_FETCH_BUDGET = 40

# 表情里 519 是「小火人」（monster emoji，display_name 写着「笑死」「续火花」），
# 载荷形状和别的表情一样：url.url_list[0] 就是那张动图，直接存就行。


def _detect_media_format(data):
    """Return (kind, ext) from magic bytes. kind is image, video, or heif."""
    if data[:3] == b"\xff\xd8\xff":
        return ("image", ".jpg")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ("image", ".png")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("image", ".webp")
    if data[:3] == b"GIF":
        return ("image", ".gif")
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in _HEIF_BRANDS:
            return ("heif", ".heic")
        if brand in _MP4_BRANDS:
            return ("video", ".mp4")
        return ("video", ".mp4")
    return ("image", ".jpg")


def _heic_to_jpeg(heic_bytes):
    import io
    from PIL import Image
    import pillow_heif

    pillow_heif.register_heif_opener()
    im = Image.open(io.BytesIO(heic_bytes))
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90)
    return out.getvalue()


def _fetch(url, timeout=20):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.douyin.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=client_context()) as resp:
        return resp.read()


def _server_image_name(url):
    """链接里那截文件名，当存档名用（用户 2026-10-11 要求）。

    抖音给的地址尾巴就是它在服务端的名字，例如
    ``.../images/food/hotstick_card.png`` → ``hotstick_card.png``；
    ``.../obj/tos-cn-o-0812/owxAKAFKAmAQCAwqEANFE2TCICKfjArQegDGyd`` →
    ``owxAKAFKAmAQCAwqEANFE2TCICKfjArQegDGyd``。名字里可能带 ``~tplv-x-get:large.image``
    这种图片处理后缀，砍掉它才是真名字。

    只接受 ``字母数字._-`` 组成、且不含 ``..`` 的名字（要拼进本地路径）；给不出这样的
    名字时返回空串，调用方退回原来的哈希命名。
    """
    import re
    from urllib.parse import unquote, urlparse

    if not isinstance(url, str) or not url:
        return ""
    base = unquote(urlparse(url).path.rsplit("/", 1)[-1]).strip()
    if not base:
        return ""
    # 砍掉图片处理后缀（上传的图都是 `xxx~tplv-xxx:yyy.image` 这种形状）
    if "~" in base:
        base = base.split("~", 1)[0]
    if not base or base in (".", "..") or ".." in base:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", base):
        return ""
    return base


def _has_image_ext(name):
    """名字末尾本来就是图片扩展名时不要再拼一次。"""
    return os.path.splitext(name)[1].lower() in (
        ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".mp4")


def _existing_media(directory, prefix, names, exts=(".jpg", ".png", ".webp", ".gif", ".mp4")):
    """目录里已经躺着这几个（不带扩展名的）名字时，返回它的相对路径。"""
    for name in names:
        if not name:
            continue
        for ext in exts:
            path = os.path.join(directory, f"{name}{ext}")
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return f"{prefix}/{name}{ext}"
    return None


def _save_emoji(url, emoji_dir):
    import hashlib
    from urllib.parse import urlparse

    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".png"
    # 优先用链接里那截名字（owxAKAF...）；给不出才退回路径哈希（老行为）。
    # 名字里本来就带扩展名时直接用，别再拼一次。
    named = _server_image_name(url)
    base = named or hashlib.md5(path.encode("utf-8")).hexdigest()[:16]
    filename = base if (named and _has_image_ext(base)) else f"{base}{ext}"
    target = os.path.join(emoji_dir, filename)
    rel = f"emoji/{filename}"
    if os.path.exists(target):
        return rel
    data = _fetch(url)
    if len(data) < 100:
        return None
    os.makedirs(emoji_dir, exist_ok=True)
    with open(target, "wb") as f:
        f.write(data)
    return rel


def _save_image(origin_url, skey_hex, server_id, img_dir, video_dir=None):
    """Download AES-GCM ciphertext and store the decrypted image or mp4.

    存档名优先用链接里那截名字（见 :func:`_server_image_name`），给不出才退回消息号；
    视频仍按消息号存（阅读端靠这个约定把实况图的封面和小视频配起来）。
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if video_dir is None:
        video_dir = os.path.join(os.path.dirname(img_dir), "videos")
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(video_dir, exist_ok=True)

    named = _server_image_name(origin_url)
    names = (named, server_id)
    # 链接里那截名字的文件已经在本地就直接用它，不用再下一次。
    existing = _existing_media(img_dir, "images", names)
    if existing:
        return existing

    cipher = _fetch(origin_url)
    if len(cipher) < 28:
        return None
    key = bytes.fromhex(skey_hex)
    iv = cipher[:12]
    body = cipher[12:]
    plain = AESGCM(key).decrypt(iv, body, None)
    kind, ext = _detect_media_format(plain)
    if kind == "heif":
        plain = _heic_to_jpeg(plain)
        kind, ext = "image", ".jpg"
    target_dir = video_dir if kind == "video" else img_dir
    prefix = "videos" if kind == "video" else "images"
    # 视频文件名保持「消息号」（见 docstring），只有图片用链接里的名字。
    # 链接里的名字本来就带扩展名时（hotstick_card.png）直接用它，别再拼一次。
    if kind == "video":
        filename = f"{server_id}{ext}"
    elif named:
        filename = named if _has_image_ext(named) else f"{named}{ext}"
    else:
        filename = f"{server_id}{ext}"
    target = os.path.join(target_dir, filename)
    # 重名不覆盖：同名的另一张图（罕见）加一小截链接哈希区分开。
    if os.path.exists(target) and os.path.getsize(target) > 0:
        with open(target, "rb") as handle:
            if handle.read() == plain:
                return f"{prefix}/{filename}"
        stem, dot, suffix = filename.rpartition(".")
        import hashlib
        digest = hashlib.md5(origin_url.encode("utf-8")).hexdigest()[:6]
        filename = f"{stem}-{digest}{dot}{suffix}" if dot else f"{filename}-{digest}"
        target = os.path.join(target_dir, filename)
        if os.path.exists(target):
            return f"{prefix}/{filename}"
    with open(target, "wb") as f:
        f.write(plain)
    return f"{prefix}/{filename}"


def media_file_id(server_id):
    sid = str(server_id or "").removeprefix("srv_")
    if "/" in sid:
        sid = sid.rsplit("/", 1)[-1].removeprefix("srv_")
    if not re.fullmatch(r"[1-9][0-9]*", sid):
        return None
    return sid


def find_local_media(server_id, url=None):
    """Return a relative media path if a previously decrypted file exists.

    图片现在按链接里那截名字存（见 :func:`_server_image_name`），所以调用方把原链接
    一起传进来时也按那个名字找一遍；老数据（按消息号存）照旧能找得到。
    """
    sid = media_file_id(server_id)
    named = _server_image_name(url)
    if not sid and not named:
        return None
    names = tuple(n for n in (named, sid, f"srv_{sid}" if sid else "") if n)
    folders = (
        (IMAGES_DIR, "images"),
        (VIDEOS_DIR, "videos"),
        (EMOJI_DIR, "emoji"),
        (VOICE_DIR, "voice"),
    )
    for folder, prefix in folders:
        if not os.path.isdir(folder):
            continue
        for name in names:
            for ext in _MEDIA_EXTS:
                path = os.path.join(folder, f"{name}{ext}")
                if os.path.isfile(path) and os.path.getsize(path) > 0:
                    return f"{prefix}/{name}{ext}"
    return None


def body_content(body):
    return _object(body.get("content") if isinstance(body, dict) else {})


def _first_url(value):
    if isinstance(value, str) and value.startswith("http"):
        return value
    if isinstance(value, dict):
        urls = value.get("url_list") or value.get("origin_url_list") or []
        if urls and isinstance(urls[0], str):
            return urls[0]
        url = value.get("url") or value.get("uri")
        if isinstance(url, str) and url.startswith("http"):
            return url
    if isinstance(value, list) and value and isinstance(value[0], str):
        return value[0]
    return None


def cenc_video(cj):
    video = cj.get("video") if isinstance(cj, dict) else None
    if not isinstance(video, dict):
        return None
    tkey, skey = video.get("tkey"), video.get("skey")
    if tkey and skey:
        return {"tkey": tkey, "skey": skey}
    return None


def live_photo_cenc(cj):
    """实况图（aweType=2704）里那段小视频的加密密钥。

    实况图 = 一张静态封面 + 一段两三秒的小视频：封面在 ``resource_url``（还是
    按图片那套 AES-GCM 解密），小视频在 ``live_photo_video`` 里，字段形状和普通
    聊天视频的 ``video`` 一模一样（tkey/skey/vid）。所以下载与解密直接复用视频那套
    流程（见 ``extractor/video_downloader.py``），这里只负责把密钥挑出来。
    """
    video = cj.get("live_photo_video") if isinstance(cj, dict) else None
    if not isinstance(video, dict):
        return None
    tkey, skey = video.get("tkey"), video.get("skey")
    if tkey and skey:
        return {"tkey": tkey, "skey": skey}
    return None


def _is_voice_payload(cj, resource):
    if cj.get("voice_wave") or resource.get("is_voice"):
        return True
    if cenc_video(cj) or str(cj.get("aweType", "")) in _IMAGE_TYPES:
        return False
    return bool(cj.get("tkey") and not cenc_video(cj))


def download_direct_media(cj, server_id):
    """Fetch attachments that do not need the story play-info API.

    Encrypted origin URLs (images / in-chat mp4) are decrypted with resource_url.skey.
    Emoji and voice use plaintext CDN URLs. Returns a relative path or None.
    """
    resource = cj.get("resource_url") if isinstance(cj.get("resource_url"), dict) else {}
    origin = _first_url(resource.get("origin_url_list"))
    # 表情走 url，图片走 resource_url.origin_url_list；本地已有的话按链接里那截名字找。
    hint = _first_url(cj.get("url")) or origin
    existing = find_local_media(server_id, hint)
    if existing:
        return existing
    sid = media_file_id(server_id)
    if not sid or not isinstance(cj, dict):
        return None
    awe = str(cj.get("aweType", ""))
    try:
        if awe in _EMOJI_TYPES:
            url = _first_url(cj.get("url"))
            path = _save_emoji(url, EMOJI_DIR) if url else None
            if path:
                media_log.info("表情 %s → %s", sid, path)
            return path
        skey = resource.get("skey")
        if awe in _IMAGE_TYPES and skey and origin:
            path = _save_image(origin, skey, sid, IMAGES_DIR, VIDEOS_DIR)
            if path:
                media_log.info("解密媒体 %s → %s", sid, path)
            else:
                media_log.warning("解密媒体失败 %s（密文过短或无法识别）", sid)
            return path
        if _is_voice_payload(cj, resource):
            voice_url = _first_url(resource)
            if not voice_url:
                return None
            os.makedirs(VOICE_DIR, exist_ok=True)
            target = os.path.join(VOICE_DIR, f"{sid}.mpeg")
            rel = f"voice/{sid}.mpeg"
            if os.path.isfile(target) and os.path.getsize(target) > 0:
                return rel
            data = _fetch(voice_url)
            if len(data) <= 100:
                media_log.warning("语音下载为空 %s", sid)
                return None
            with open(target, "wb") as f:
                f.write(data)
            media_log.info("语音 %s → %s", sid, rel)
            return rel
    except Exception as exc:
        media_log.warning("补拉媒体失败 %s: %s", sid, type(exc).__name__)
        return None
    return None


def iter_message_bodies(raw):
    raw = _object(raw)
    content = _object(raw.get("content_json"))
    bodies = raw.get("forwarded_bodies")
    if not isinstance(bodies, list):
        bodies = content.get("inline_content")
    if not isinstance(bodies, list):
        return
    for body in bodies:
        if isinstance(body, dict):
            yield body


def collect_cenc_jobs(raw=None, bodies=None):
    jobs = []
    seen = set()
    for body in (bodies if bodies is not None else iter_message_bodies(raw)):
        sid = media_file_id(body.get("server_message_id"))
        cj = body_content(body)
        video = cenc_video(cj)
        if not sid or not video or sid in seen:
            continue
        local = find_local_media(sid)
        if local and str(local).lower().endswith(".mp4"):
            continue
        seen.add(sid)
        jobs.append({"file_id": sid, "tkey": video["tkey"], "skey": video["skey"],
                     "msg_id": f"srv_{sid}"})
    return jobs


def find_live_photo_video(cj, server_id):
    """实况图那段小视频如果在本地，返回它的相对路径（``videos/<消息id>.mp4``）。

    实况图的小视频和普通视频一样放在 ``data/media/videos/``，文件名就是消息 id ——
    跟图片、语音同一套约定。合并转发里的内嵌消息没有自己的数据库行（``live_video_path``
    那列写不上），阅读端只能靠这个约定把「静态封面 + 会动的那段」配起来。
    """
    if not live_photo_cenc(cj):
        return None
    sid = media_file_id(server_id)
    if not sid:
        return None
    for name in (sid, f"srv_{sid}"):
        path = os.path.join(VIDEOS_DIR, f"{name}.mp4")
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return f"videos/{name}.mp4"
    return None


def collect_live_photo_jobs(raw=None, bodies=None):
    """实况图小视频的下载任务，供合并转发（或用整包载荷）批量入队。

    跟 :func:`collect_cenc_jobs` 只有两点不同：密钥取 ``live_photo_video``，
    以及**已经躺在本地的不再入队** —— 实况图的静态封面也在 ``images/``，所以不能像
    普通视频那样用 :func:`find_local_media` 判断（它先看图片，永远看不到 mp4）。
    """
    jobs = []
    seen = set()
    for body in (bodies if bodies is not None else iter_message_bodies(raw)):
        sid = media_file_id(body.get("server_message_id"))
        cj = body_content(body)
        video = live_photo_cenc(cj)
        if not sid or not video or sid in seen:
            continue
        if find_live_photo_video(cj, sid):
            continue
        seen.add(sid)
        jobs.append({"file_id": sid, "kind": "live", "tkey": video["tkey"],
                     "skey": video["skey"], "msg_id": f"srv_{sid}"})
    return jobs


def materialize_bodies(bodies, *, fetch=True, budget=None):
    """Decrypt direct attachments for merged-record bodies. CENC videos are skipped."""
    budget = budget if budget is not None else [DIRECT_FETCH_BUDGET]
    paths = {}
    for body in bodies or []:
        if budget[0] <= 0:
            break
        sid = media_file_id((body or {}).get("server_message_id"))
        if not sid:
            continue
        path = find_local_media(sid)
        if not path and fetch:
            budget[0] -= 1
            path = download_direct_media(body_content(body), sid)
        if path:
            paths[sid] = path
    return paths


def ensure_row_media(row, conn=None, *, fetch=True, budget=None):
    """Attach a local file to a message row; optionally persist it on srv_* rows."""
    if not row:
        return row
    current = row.get("media_local_path")
    if current:
        abs_path = os.path.join(MEDIA_DIR, current) if not os.path.isabs(str(current)) else current
        if os.path.isfile(abs_path) and os.path.getsize(abs_path) > 0:
            return row
    sid = media_file_id(row.get("msg_id") or row.get("server_message_id"))
    cj = _object(_object(row.get("raw_data")).get("content_json")) or _object(row.get("content"))
    path = find_local_media(sid)
    if not path and fetch and (budget is None or budget[0] > 0):
        if budget is not None:
            budget[0] -= 1
        path = download_direct_media(cj, sid)
    if path:
        row["media_local_path"] = path
        msg_id = str(row.get("msg_id") or "")
        if conn is not None and msg_id.startswith("srv_") and "/" not in msg_id:
            conn.execute(
                "UPDATE messages SET media_local_path=? WHERE msg_id=? "
                "AND (media_local_path IS NULL OR media_local_path='')",
                (path, msg_id),
            )
    return row
