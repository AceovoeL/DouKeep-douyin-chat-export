"""Merged-record resource decoding, based on Android ShareMergeDownloader.

Network/authentication is supplied by the caller: this module never copies
browser cookies to a different domain. See docs/merged-forward-records.md.
"""
import asyncio
import json
import re
from urllib.parse import urlsplit

import httpx

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

OBJECT_URL_PATH = "/aweme/v1/im_communication/merge_msg_card/get_object_url/"
WEB_OBJECT_URL_PATH = "/aweme/v1/web/im_communication/merge_msg_card/get_object_url/"
MAX_RESOURCE_BYTES = 16 * 1024 * 1024
MAX_MESSAGES = 1000


def _list(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("合并记录列表格式错误")
    return value


def _message_id(message):
    value = message.get("server_message_id")
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError("合并记录消息 ID 无效")
    return str(value)


def decode_resource(data: bytes, skey: str) -> list[dict]:
    """Decode nonce(12) + AES-GCM ciphertext/tag; skey is hexadecimal."""
    if not 28 <= len(data) <= MAX_RESOURCE_BYTES:
        raise ValueError("合并记录资源长度无效")
    if not isinstance(skey, str) or not re.fullmatch(
        r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{48}|[0-9a-fA-F]{64})", skey
    ):
        raise ValueError("合并记录密钥格式错误")
    payload = json.loads(AESGCM(bytes.fromhex(skey)).decrypt(data[:12], data[12:], None))
    messages = payload.get("Messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise ValueError("合并记录资源缺少 Messages 正文")
    ids = set()
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("合并记录正文格式错误")
        sid = _message_id(message)
        if sid in ids:
            raise ValueError("合并记录资源包含重复消息")
        ids.add(sid)
        content = message.get("content")
        if isinstance(content, str):
            content = json.loads(content)
        if not isinstance(content, dict) or not content:
            raise ValueError("合并记录消息正文缺失")
    return messages


async def fetch_uploaded_bodies(content, request_url, download, *, access_chain=None):
    """Fetch all resource parts; return bodies only after complete validation.

    request_url(path, JSON body) returns the parsed API response. download(url,
    max_bytes) returns bytes and must enforce its limit while streaming. The
    caller must restrict downloads to trusted resource hosts and omit cookies.
    Neither callback is provided until the caller has an authenticated transport.
    """
    uploads = _list(content.get("upload_key_list", []))
    if not uploads:
        return []
    if len(uploads) > MAX_MESSAGES:
        raise ValueError("合并记录资源数量过多")
    skey = content.get("skey")
    descriptors = _list(content.get("msg_ids", []))
    expected = [str(d["msg_id"]) for d in descriptors]
    if not expected or len(expected) > MAX_MESSAGES or len(set(expected)) != len(expected):
        raise ValueError("合并记录消息索引无效")
    messages = {}
    keys = set()
    for upload in uploads:
        if not isinstance(upload, dict):
            raise ValueError("合并记录资源索引无效")
        uri, count = upload.get("key"), upload.get("count")
        if (not isinstance(uri, str) or not uri.startswith("douyin-im-merge-share/")
                or uri in keys or type(count) is not int or not 1 <= count <= MAX_MESSAGES):
            raise ValueError("合并记录资源索引无效")
        keys.add(uri)
        body = {"uri": uri}
        if access_chain is not None:
            body["access_chain"] = access_chain
        response = await request_url(OBJECT_URL_PATH, body)
        if not isinstance(response, dict) or response.get("status_code") != 0:
            raise ValueError("合并记录资源接口未返回成功状态")
        url = response.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("合并记录资源链接无效")
        part = decode_resource(await download(url, MAX_RESOURCE_BYTES), skey)
        if len(part) != count:
            raise ValueError("合并记录资源条数不符")
        for message in part:
            sid = _message_id(message)
            if sid not in expected or sid in messages:
                raise ValueError("合并记录资源与消息索引不符")
            messages[sid] = message
    if set(messages) != set(expected):
        raise ValueError("合并记录资源正文不完整")
    return [messages[sid] for sid in expected]


async def _request_web_url(page, path, body):
    # Serialize in Python so access-chain IDs never round-trip through JS Number.
    result = await page.evaluate("""async ({path, body}) => {
        if (location.origin !== 'https://www.douyin.com')
            throw new Error('需要抖音网页登录上下文');
        const r = await fetch(path + '?aid=6383&device_platform=webapp', {
            method: 'POST', credentials: 'include',
            headers: {'Content-Type': 'application/json'}, body,
            signal: AbortSignal.timeout(15000),
        });
        return {status: r.status, body: await r.text()};
    }""", {"path": WEB_OBJECT_URL_PATH, "body": json.dumps(body)})
    if result.get("status") != 200:
        raise ValueError("合并记录资源接口请求失败")
    payload = json.loads(result["body"])
    # The web service omits status_code on success (verified with a real card).
    # Error responses do include it; do not mask explicit nonzero status codes.
    if isinstance(payload, dict) and "status_code" not in payload and payload.get("url"):
        payload["status_code"] = 0
    return payload


def _check_resource_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443)
            or not re.fullmatch(r"p[0-9]+-aweme-im-merge-share-sign\.byteimg\.com",
                                parsed.hostname or "")):
        raise ValueError("合并记录资源链接不是已验证的下载域名")


async def _download_resource(url, max_bytes):
    _check_resource_url(url)
    # Signed resource URL is sufficient. Never send login cookies to the CDN.
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ValueError("合并记录资源超过下载大小限制")
            return bytes(data)


async def fetch_web_uploaded_bodies(page, content, *, access_chain=None):
    async def request(path, body):
        return await _request_web_url(page, path, body)
    return await fetch_uploaded_bodies(
        content, request, _download_resource, access_chain=access_chain,
    )


def _object(value):
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


async def _materialize_forward_media(page, bodies, msg_id):
    from extractor.im_media import collect_cenc_jobs, materialize_bodies
    materialize_bodies(bodies)
    jobs = collect_cenc_jobs(bodies=bodies)
    if not jobs:
        return
    from extractor.video_downloader import save_cenc_jobs
    video_stats = await save_cenc_jobs(page, jobs)
    if video_stats.get("ok") or video_stats.get("fail"):
        print(f"  [forward] 视频补全 {msg_id}: {video_stats}")


def _complete(content, bodies):
    if not isinstance(bodies, list):
        return False
    try:
        expected = {str(d["msg_id"]) for d in _list(content.get("msg_ids", []))}
        return bool(expected) and expected == {_message_id(b) for b in bodies}
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


async def backfill_uploaded_forwards(page, conn, conv_id, short_id):
    """Fill archived resources after scraping; failed retrieval leaves rows intact."""
    stats = {"succeeded": 0, "failed": 0, "cached": 0}
    backup = conn.execute(
        "SELECT 1 FROM sqlite_temp_master WHERE type='table' AND name='msg_backup'"
    ).fetchone()
    rows = conn.execute(
        "SELECT msg_id, raw_data FROM messages WHERE conv_id=? "
        "AND raw_data LIKE '%upload_key_list%' ORDER BY timestamp DESC", (conv_id,),
    ).fetchall()
    for row in rows:
        msg_id, original_raw = row[0], row[1]
        try:
            raw = _object(original_raw)
            content = _object(raw.get("content_json"))
            if str(content.get("aweType")) != "13600" or not content.get("upload_key_list"):
                continue
            if _complete(content, content.get("inline_content")) or _complete(content, raw.get("forwarded_bodies")):
                stats["cached"] += 1
                try:
                    await _materialize_forward_media(
                        page, content.get("inline_content") or raw.get("forwarded_bodies"), msg_id,
                    )
                except Exception as media_exc:
                    print(f"  [forward] 媒体补全失败 {msg_id}: {type(media_exc).__name__}")
                continue
            bodies = None
            if backup:
                old = conn.execute("SELECT raw_data FROM temp.msg_backup WHERE msg_id=?", (msg_id,)).fetchone()
                if old:
                    old_raw = _object(old[0])
                    if _object(old_raw.get("content_json")) == content and _complete(content, old_raw.get("forwarded_bodies")):
                        bodies = old_raw["forwarded_bodies"]
                        stats["cached"] += 1
            if bodies is None:
                chain = [{"msg_id": int(msg_id.removeprefix("srv_")), "conv_id": int(short_id)}]
                async with asyncio.timeout(60):
                    bodies = await fetch_web_uploaded_bodies(page, content, access_chain=chain)
                if not bodies:
                    continue
                stats["succeeded"] += 1
            raw["forwarded_bodies"] = bodies
            # Preserve content_json and avoid overwriting a concurrently changed row.
            conn.execute("UPDATE messages SET raw_data=? WHERE msg_id=? AND raw_data=?",
                         (json.dumps(raw, ensure_ascii=False), msg_id, original_raw))
            conn.commit()
            try:
                await _materialize_forward_media(page, bodies, msg_id)
            except Exception as media_exc:
                print(f"  [forward] 媒体补全失败 {msg_id}: {type(media_exc).__name__}")
        except Exception as exc:
            stats["failed"] += 1
            # Exception messages can contain a signed URL/key. Log only the type.
            print(f"  [forward] 正文补全失败 {msg_id}: {type(exc).__name__}")
    return stats
