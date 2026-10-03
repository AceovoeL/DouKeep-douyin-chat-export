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
_EMOJI_TYPES = {"500", "501", "507", "508", "510", "514", "516"}
_IMAGE_TYPES = {"2702", "2703", "2704"}
_MEDIA_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mpeg", ".m4a")
DIRECT_FETCH_BUDGET = 40


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


def _save_emoji(url, emoji_dir):
    import hashlib
    from urllib.parse import urlparse

    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".png"
    h = hashlib.md5(path.encode("utf-8")).hexdigest()[:16]
    filename = f"{h}{ext}"
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
    """Download AES-GCM ciphertext and store the decrypted image or mp4."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if video_dir is None:
        video_dir = os.path.join(os.path.dirname(img_dir), "videos")
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(video_dir, exist_ok=True)

    for d, prefix in ((img_dir, "images"), (video_dir, "videos")):
        for ext in (".jpg", ".png", ".webp", ".gif", ".mp4"):
            t = os.path.join(d, f"{server_id}{ext}")
            if os.path.exists(t):
                return f"{prefix}/{server_id}{ext}"

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
    filename = f"{server_id}{ext}"
    with open(os.path.join(target_dir, filename), "wb") as f:
        f.write(plain)
    return f"{prefix}/{filename}"


def media_file_id(server_id):
    sid = str(server_id or "").removeprefix("srv_")
    if "/" in sid:
        sid = sid.rsplit("/", 1)[-1].removeprefix("srv_")
    if not re.fullmatch(r"[1-9][0-9]*", sid):
        return None
    return sid


def find_local_media(server_id):
    """Return a relative media path if a previously decrypted file exists."""
    sid = media_file_id(server_id)
    if not sid:
        return None
    names = (sid, f"srv_{sid}")
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
    existing = find_local_media(server_id)
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
        resource = cj.get("resource_url") if isinstance(cj.get("resource_url"), dict) else {}
        origin = _first_url(resource.get("origin_url_list"))
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
