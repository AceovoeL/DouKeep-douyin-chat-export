"""表情包同步端点：某个人发过的表情包目录 + 图片本身（只读）。

    GET /api/stickers?sender_uid=&conv_id=&min_count=&samples=   目录
    GET /api/stickers/{sticker_id}                               图片

给外部程序（比如用聊天记录做人格分身的服务）同步「这个人自己发过的表情包」用。
只看已经下载到本地的表情包（msg_type=2，media_local_path 在 media/emoji/ 下）；
没有本地文件的只计数（missing_files），这里不去抖音下载。

同一张表情包在 CDN 上可能有好几个 URL（本地按 URL 路径哈希存成了好几个文件），
所以目录按**文件内容 MD5** 去重，sticker_id 就是这个 MD5。哈希按 (路径, 大小, mtime)
缓存在进程内存里，重复请求不会重读整个 emoji 目录。

只读 GET，走永久 API token 鉴权（见 backend.main.auth_middleware）。
"""
import hashlib
import os
import re
import threading
from collections import Counter, defaultdict

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from backend import database
from common import paths
from extractor.exporter import _decode_sticker_name

stickers_router = APIRouter(prefix="/api/stickers", tags=["stickers"])

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_EXT_TYPES = {
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

_lock = threading.Lock()
_by_path: dict[str, tuple[int, int, str, str]] = {}  # 绝对路径 -> (size, mtime_ns, md5, content_type)
_by_md5: dict[str, str] = {}  # md5 -> 绝对路径


def _emoji_root() -> str:
    return os.path.realpath(os.path.join(paths.MEDIA_DIR, "emoji"))


def _resolve(rel: str | None) -> str | None:
    """media_local_path → 绝对路径；不在 media/emoji/ 下或文件不存在返回 None。"""
    if not rel:
        return None
    root = _emoji_root()
    full = os.path.realpath(os.path.join(paths.MEDIA_DIR, rel))
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        return None
    return full


def _sniff(head: bytes, path: str) -> str:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return _EXT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def _file_info(full: str) -> tuple[int, int, str, str]:
    """(size, mtime_ns, md5, content_type)，按 (路径, 大小, mtime) 缓存。"""
    st = os.stat(full)
    with _lock:
        cached = _by_path.get(full)
    if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
        return cached
    h = hashlib.md5()
    with open(full, "rb") as f:
        head = f.read(16)
        h.update(head)
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    info = (st.st_size, st.st_mtime_ns, h.hexdigest(), _sniff(head, full))
    with _lock:
        _by_path[full] = info
        _by_md5[info[2]] = full
    return info


def _spread(values: list[int], k: int) -> list[int]:
    """从排好序的 values 里均匀取 k 个（含首尾）。"""
    n = len(values)
    if k <= 0 or n == 0:
        return []
    if k >= n:
        return list(values)
    if k == 1:
        return [values[n // 2]]
    idx = sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})
    return [values[i] for i in idx]


@stickers_router.get("")
def list_stickers(
    sender_uid: str = Query(""),
    conv_id: str | None = Query(None),
    min_count: int = Query(1, ge=1),
    samples: int = Query(0, ge=0, le=50),
):
    if not sender_uid.strip():
        raise HTTPException(400, "缺少 sender_uid")
    sql = "SELECT media_local_path, media_url, timestamp FROM messages WHERE sender_uid=? AND msg_type=2"
    args: list = [sender_uid]
    if conv_id:
        sql += " AND conv_id=?"
        args.append(conv_id)
    conn = database.get_db()
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()

    missing = 0
    uses: dict[str, list[int]] = defaultdict(list)
    names: dict[str, Counter] = defaultdict(Counter)
    info: dict[str, tuple[int, str]] = {}  # md5 -> (size, content_type)
    resolved: dict[str, str | None] = {}
    for rel, url, ts in rows:
        if rel not in resolved:
            resolved[rel] = _resolve(rel)
        full = resolved[rel]
        if not full:
            missing += 1
            continue
        size, _, md5, ctype = _file_info(full)
        uses[md5].append(int(ts or 0))
        info[md5] = (size, ctype)
        name = _decode_sticker_name(url or "")
        if name:
            names[md5][name] += 1

    stickers = []
    for md5, ts_list in uses.items():
        if len(ts_list) < min_count:
            continue
        ts_list.sort()
        top = names[md5].most_common(1)
        stickers.append({
            "id": md5,
            "count": len(ts_list),
            "first_ts": ts_list[0],
            "last_ts": ts_list[-1],
            "name": top[0][0] if top else None,
            "content_type": info[md5][1],
            "size": info[md5][0],
            "sample_ts": _spread(ts_list, samples),
        })
    stickers.sort(key=lambda s: (-s["count"], s["id"]))
    return {
        "sender_uid": sender_uid,
        "conv_id": conv_id,
        "total_uses": len(rows) - missing,
        "missing_files": missing,
        "stickers": stickers,
    }


def _find(sticker_id: str) -> tuple[str, str] | None:
    """md5 → (绝对路径, content_type)。缓存里没有就把 emoji 目录哈希一遍再找。"""
    with _lock:
        full = _by_md5.get(sticker_id)
    if full and _resolve(os.path.relpath(full, paths.MEDIA_DIR)):
        size, _, md5, ctype = _file_info(full)
        if md5 == sticker_id:
            return full, ctype
    root = _emoji_root()
    if not os.path.isdir(root):
        return None
    for entry in os.scandir(root):
        if entry.is_file() and not entry.name.startswith("."):
            _, _, md5, ctype = _file_info(os.path.realpath(entry.path))
            if md5 == sticker_id:
                return os.path.realpath(entry.path), ctype
    return None


@stickers_router.get("/{sticker_id}")
def get_sticker(sticker_id: str):
    if not _ID_RE.match(sticker_id):
        raise HTTPException(404, "表情包不存在")
    found = _find(sticker_id)
    if not found:
        raise HTTPException(404, "表情包不存在")
    full, ctype = found
    return FileResponse(
        full,
        media_type=ctype,
        headers={"Cache-Control": "private, max-age=86400", "ETag": f'"{sticker_id}"'},
    )
