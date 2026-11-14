"""概览页「数据总览」：把整库的关键数字算好，带缓存、后台刷新。

面板「概览」页以前只有会话 / 消息 / 用户三个数字，跟着 5 秒一次的运行状态
轮询走。现在要加上「今日 / 近 7 天消息」「消息构成」「覆盖范围」「本地保存」
「磁盘占用」这些统计：它们要扫一遍 messages 表，库上 GB、消息多的时候冷读一次
可能要几十秒 —— 绝不能挂在 HTTP 请求里让页面等。

所以这里的策略是「先返回、后台算」：

* collect() 立刻返回：有缓存就给缓存；缓存过期也给旧数字，同时开一个后台线程重算；
* 一次都没有缓存时返回 computing=True，面板显示「正在统计…」，每 3 秒再来问；
* 同一时刻只有一个后台线程在算（谁先来谁触发，其余请求直接拿旧数据）；
* 后台算失败只打日志、保留旧缓存；连旧缓存都没有才 available=False。

几个数字的口径要特别说明（面板文案也按这个写，别再让人猜）：

* **单聊 / 群聊不读 conversations.conv_type**：采集端当年没写这个字段，所有会话
  都是默认的 1，按它算群聊永远是 0。可靠依据是会话 ID 的形状：单聊是
  ``0:1:<uid>:<uid>``（带冒号），群聊是纯数字 ID（不带冒号）。
* **小火人 AI 不算「发言人数」**：它的消息带 awe_type=716，是单独一个账号；
  不排除的话「你 + 对方 + 小火人」的 1:1 会被数成 3 个人。
* **视频**：普通视频在库里不一定是 msg_type=5，按类型数会得到 0。改用与
  extractor/video_downloader.py 一致的粗筛（msg_type=5 或载荷带 tkey+poster），
  并排除实况图的小视频；实况图单独一行。
* **导出/备份文件**：只算用户能看到的导出文件（json/jsonl/html/zip）和数据库
  备份（*.db），不算 conversations_list.json、回滚记录这些内部小文件。

返回结构是与前端约定的，键名改了就同步改 backend/panel/static/panel.html 和
tests/test_panel_overview.py。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta

from backend import database
from common import paths
from extractor.im_media import cenc_video, live_photo_cenc

#: 缓存多久（秒）。概览页每 60 秒轮询一次；120 秒意味着最多每两分钟在后台
#: 重算一次，热缓存下几秒。数字不需要秒级新鲜，别把磁盘读满。
CACHE_TTL = 120.0

#: 数据库 msg_type → 前端标签键。见 extractor/web_scraper.py 的
#: msg_type_map：0=other / 1=text / 2=emoji / 3=image / 4=share / 5=video。
#: 0 里混着语音、系统提示、卡片等，前端统一叫「其他」，不硬拆。
_TYPE_KEYS = {0: "other", 1: "text", 2: "emoji", 3: "image", 4: "share", 5: "video"}
#: 固定显示顺序（数量为 0 的前端会跳过，但接口仍把这一项给全）。
_TYPE_ORDER = ("text", "image", "emoji", "video", "share", "other")

#: 媒体目录里要单独列出来的子目录（键与前端 i18n 的 storageXxx 对应）。
_MEDIA_DIRS = (
    ("images", "images"),
    ("emoji", "emoji"),
    ("voice", "voice"),
    ("avatars", "avatars"),
    ("videos", "videos"),
)
#: 媒体目录里归到「其他」的子目录：气泡装饰、分享卡封面、转码缓存。
_MEDIA_OTHER_DIRS = ("bubbles", "card_icons")
#: 「小火人 AI」的消息特征：载荷里的 awe_type=716。它不是真人，不能算进
#: 「发言人数」里，别让 AI 冒充一个真人。
_BOT_RAW_PATTERN = '%"awe_type": 716%'

#: 数据目录根下这些文件属于数据库，不算「导出与备份」。
_DB_FILE_NAMES = ("chat.db", "chat.db-wal", "chat.db-shm")
#: 算「导出/备份」的后缀：导出文件 + 导入前自动留的数据库备份。
_EXPORT_SUFFIXES = (".zip", ".json", ".jsonl", ".html", ".htm", ".db")
#: 这些 .json 是面板内部状态，不是用户导出的东西。
_INTERNAL_DATA_FILES = ("conversations_list.json",)

_lock = threading.Lock()
#: (时间戳, 数据库统计, 磁盘统计)
_cache: tuple[float, dict, dict] | None = None
#: 后台线程是否正在算。
_refreshing = False


def _int(value) -> int:
    """SQLite 的 SUM 在空表上是 None，统一折成 0。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _local_day_start(days_ago: int = 0) -> int:
    """本机时区里「今天 0 点」往前推 days_ago 天的 Unix 时间戳。"""
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    start -= timedelta(days=days_ago)
    return int(start.timestamp())


def _tz_modifier() -> str:
    """SQLite date() 的时区修饰符，例如 "+28800 seconds"。"""
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return f"{int(offset.total_seconds()):+d} seconds"


def _empty_stats() -> dict:
    """空库 / 还没算完时的零值结构，保证前端拿到的东西形状一致。"""
    return {
        "totals": {
            "conversations": 0, "messages": 0, "users": 0,
            "singles": 0, "groups": 0, "avg_messages": 0.0,
        },
        "activity": {
            "today": 0, "last7": 0, "last30": 0, "active_days": 0,
            "senders": 0, "first_message_time": None, "last_message_time": None,
        },
        "types": [
            {"key": key, "count": 0, "saved": 0, "live_saved": 0, "percent": 0.0}
            for key in _TYPE_ORDER
        ],
        "media": {
            "images": {"total": 0, "saved": 0},
            "emoji": {"total": 0, "saved": 0},
            "videos": {"total": 0, "saved": 0},
            "live_photo": {"total": 0, "saved": 0},
        },
        "voice": {"success": 0, "failed": 0, "pending": 0, "total": 0},
    }


def _empty_disk() -> dict:
    return {
        "db_bytes": 0,
        "media_bytes": 0,
        "media_files": 0,
        "exports_bytes": 0,
        "total_bytes": 0,
        "detail": [
            {"key": key, "bytes": 0, "files": 0}
            for key in ("images", "emoji", "voice", "avatars", "videos", "other")
        ],
    }


def _video_kind(raw_data, msg_type=None) -> str | None:
    """和 extractor/video_downloader.py 同口径：video / live / None。

    只看 msg_type 会把普通视频算成 0（老数据落成了别的类型），所以这里读
    载荷里的 video / live_photo_video 密钥；msg_type=5 但没载荷的极老数据
    才按类型兜底成普通视频。
    """
    try:
        root = json.loads(raw_data) if raw_data else {}
        cj = json.loads(root.get("content_json") or "{}") if isinstance(root, dict) else {}
    except (TypeError, ValueError):
        cj = {}
    if not isinstance(cj, dict):
        cj = {}
    if cenc_video(cj):
        return "video"
    if live_photo_cenc(cj):
        return "live"
    if msg_type == 5:
        return "video"
    return None


def _read_stats() -> dict:
    """一次连接、三组查询：总量 / 活跃度，消息构成 + 视频口径，单聊 / 群聊。

    前两组查询要扫 messages 表（大库冷读一次很慢），所以只在后台线程里跑，
    并且结果缓存 120 秒；单聊 / 群聊只看 conversations，很轻。
    """
    today_start = _local_day_start(0)
    week_start = _local_day_start(6)     # 含今天在内的 7 个自然日
    month_start = _local_day_start(29)   # 含今天在内的 30 个自然日
    tz_modifier = _tz_modifier()

    conn = database.get_db()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS messages,
                      MIN(NULLIF(timestamp, 0)) AS first_ts,
                      MAX(timestamp) AS last_ts,
                      SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END) AS today,
                      SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END) AS last7,
                      SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END) AS last30,
                      COUNT(DISTINCT CASE WHEN sender_uid IN (
                                SELECT DISTINCT sender_uid FROM messages
                                WHERE raw_data LIKE ?)
                            THEN NULL ELSE NULLIF(sender_uid, '') END) AS senders,
                      COUNT(DISTINCT CASE WHEN timestamp > 0
                            THEN date(timestamp, 'unixepoch', ?) END) AS active_days
               FROM messages""",
            (today_start, week_start, month_start, _BOT_RAW_PATTERN, tz_modifier),
        ).fetchone()
        conv = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        groups_row = conn.execute(
            # 群聊的 conv_id 是纯数字（例如 19 位），单聊是 0:1:<uid>:<uid>。
            # 这条只看 conversations，不用扫消息表；也不受小火人 AI 参与影响。
            "SELECT COUNT(*) FROM conversations "
            "WHERE instr(conv_id, ':') = 0 OR conv_type = 2"
        ).fetchone()
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        type_rows = conn.execute(
            """SELECT msg_type, COUNT(*) AS n,
                      SUM(CASE WHEN COALESCE(media_local_path, '') <> ''
                               THEN 1 ELSE 0 END) AS saved,
                      SUM(CASE WHEN COALESCE(live_video_path, '') <> ''
                               THEN 1 ELSE 0 END) AS live_saved
               FROM messages GROUP BY msg_type"""
        ).fetchall()
        # 视频 / 实况图不能只看 msg_type：普通视频在库里可能落成 0/1/4 类型，
        # 必须按载荷里的 video / live_photo_video 密钥判断（与下载端同一套规则）。
        video_rows = conn.execute(
            "SELECT msg_type, raw_data, media_local_path, live_video_path "
            "FROM messages WHERE msg_type = 5 OR raw_data LIKE '%tkey%'"
        ).fetchall()
        voice_rows = conn.execute(
            "SELECT status, COUNT(*) FROM voice_transcriptions GROUP BY status"
        ).fetchall()
    finally:
        conn.close()

    messages = _int(row["messages"])
    conversations = _int(conv[0])
    # conv_type 在采集端一直没写对：所有会话都是 1。可靠依据是 conv_id 的形状：
    # 群聊是纯数字 ID（没有冒号），单聊是 0:1:<uid>:<uid>；conv_type=2 也直接当群聊。
    groups = min(conversations, _int(groups_row[0]))
    singles = max(0, conversations - groups)

    # 固定顺序、未知类型并入「其他」：老库里出现过别的数字，不能让它们消失。
    buckets = {
        key: {"key": key, "count": 0, "saved": 0, "live_saved": 0, "percent": 0.0}
        for key in _TYPE_ORDER
    }
    for item in type_rows:
        key = _TYPE_KEYS.get(item["msg_type"], "other")
        buckets[key]["count"] += _int(item["n"])
        buckets[key]["saved"] += _int(item["saved"])
        buckets[key]["live_saved"] += _int(item["live_saved"])
    for item in buckets.values():
        item["percent"] = round(item["count"] * 100.0 / messages, 1) if messages else 0.0

    video_total = video_saved = live_total = live_saved = 0
    for item in video_rows:
        kind = _video_kind(item["raw_data"], item["msg_type"])
        if kind == "video":
            video_total += 1
            if str(item["media_local_path"] or "").lower().endswith(".mp4"):
                video_saved += 1
        elif kind == "live":
            live_total += 1
            if item["live_video_path"]:
                live_saved += 1
    # 极老的库里可能出现「有 mp4 但载荷认不出来」：别让已保存大过总数。
    video_total = max(video_total, video_saved)
    live_total = max(live_total, live_saved)

    success = failed = 0
    for item in voice_rows:
        count = _int(item[1])
        status = str(item[0] or "").lower()
        if status == "success":
            success += count
        elif status in ("failed", "fail", "error"):
            failed += count
    voice_total = sum(_int(item[1]) for item in voice_rows)

    return {
        "totals": {
            "conversations": conversations,
            "messages": messages,
            "users": _int(users),
            "singles": singles,
            "groups": groups,
            "avg_messages": round(messages / conversations, 1) if conversations else 0.0,
        },
        "activity": {
            "today": _int(row["today"]),
            "last7": _int(row["last7"]),
            "last30": _int(row["last30"]),
            "active_days": _int(row["active_days"]),
            "senders": _int(row["senders"]),
            "first_message_time": _int(row["first_ts"]) or None,
            "last_message_time": _int(row["last_ts"]) or None,
        },
        "types": [buckets[key] for key in _TYPE_ORDER],
        "media": {
            "images": {
                "total": buckets["image"]["count"],
                "saved": buckets["image"]["saved"],
            },
            "emoji": {
                "total": buckets["emoji"]["count"],
                "saved": buckets["emoji"]["saved"],
            },
            "videos": {"total": video_total, "saved": video_saved},
            "live_photo": {"total": live_total, "saved": live_saved},
        },
        "voice": {
            "success": success,
            "failed": failed,
            "pending": max(0, voice_total - success - failed),
            "total": voice_total,
        },
    }


def _path_size(path: str) -> tuple[int, int]:
    """递归统计一个目录的 (字节数, 文件数)；不存在 / 无权限都按 0。

    不走 os.walk：它会把符号链接当目录跟进去。这里显式跳过链接，
    并且单个条目失败只跳过它，不让整个统计挂掉。
    """
    total = 0
    files = 0
    if not os.path.isdir(path):
        return 0, 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path) if os.path.isfile(path) else 0
    except OSError:
        return 0


def _is_export_file(name: str) -> bool:
    """data/ 根目录下算「导出/备份」的文件。

    只认导出格式和 .db 备份；conversations_list.json、backfill_*_undo.json
    这些是面板自己的状态文件，算进去只会让用户以为「导出占了 132 KB」。
    """
    lower = name.lower()
    if lower in _DB_FILE_NAMES or lower in _INTERNAL_DATA_FILES:
        return False
    if lower.startswith("backfill_") and lower.endswith("_undo.json"):
        return False
    return lower.endswith(_EXPORT_SUFFIXES)


def _read_disk() -> dict:
    """数据库 + 媒体目录 + data/ 根目录里导出/备份文件的大小。"""
    media_dir = paths.MEDIA_DIR
    detail = []
    media_bytes = 0
    media_files = 0
    for key, name in _MEDIA_DIRS:
        size, count = _path_size(os.path.join(media_dir, name))
        detail.append({"key": key, "bytes": size, "files": count})
        media_bytes += size
        media_files += count

    other_bytes = 0
    other_files = 0
    for name in _MEDIA_OTHER_DIRS:
        size, count = _path_size(os.path.join(media_dir, name))
        other_bytes += size
        other_files += count
    size, count = _path_size(getattr(paths, "TRANSCODE_DIR", ""))
    other_bytes += size
    other_files += count
    detail.append({"key": "other", "bytes": other_bytes, "files": other_files})
    media_bytes += other_bytes
    media_files += other_files

    db_bytes = _file_size(paths.DB_PATH) + _file_size(paths.DB_PATH + "-wal")

    exports_bytes = 0
    try:
        with os.scandir(paths.DATA_DIR) as entries:
            for entry in entries:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if not _is_export_file(entry.name):
                        continue
                    exports_bytes += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        pass

    return {
        "db_bytes": db_bytes,
        "media_bytes": media_bytes,
        "media_files": media_files,
        "exports_bytes": exports_bytes,
        "total_bytes": db_bytes + media_bytes + exports_bytes,
        "detail": detail,
    }


def _build(stamp: float, stats: dict, disk: dict, *, stale: bool) -> dict:
    return {
        "available": True,
        "computing": False,
        "stale": stale,
        "generated_at": int(stamp),
        **stats,
        "storage": disk,
    }


def _build_empty(*, computing: bool) -> dict:
    return {
        "available": False,
        "computing": computing,
        "stale": False,
        "generated_at": None,
        **_empty_stats(),
        "storage": _empty_disk(),
    }


def _refresh_worker() -> None:
    """后台线程：重新算一份，成功才替换缓存。"""
    global _cache, _refreshing
    try:
        stats = _read_stats()
        try:
            disk = _read_disk()
        except Exception as exc:  # noqa: BLE001 —— 目录读不到就沿用旧的
            print(f"[overview] 磁盘统计读取失败：{exc}", flush=True)
            disk = _cache[2] if _cache is not None else _empty_disk()
        with _lock:
            _cache = (time.time(), stats, disk)
    except Exception as exc:  # noqa: BLE001 —— 读库失败保留旧缓存，绝不把页面打挂
        print(f"[overview] 数据库统计读取失败：{exc}", flush=True)
    finally:
        with _lock:
            _refreshing = False


def _start_refresh() -> None:
    """没有正在跑的刷新线程时才起一个（同一时刻只算一份）。"""
    global _refreshing
    with _lock:
        if _refreshing:
            return
        _refreshing = True
    threading.Thread(target=_refresh_worker, name="overview-refresh", daemon=True).start()


def _read_collected_at() -> int | None:
    """「数据统计截止至」：整库最近一次采集动作的时间。

    每个会话采集时都会单独记下 collected_at（聊天查看器那行用的也是它）；概览看的是
    整库，取其中最新的一个。一条记录都没有（老库 / 还没采集过）就返回 None，
    面板上整行不显示 —— 和查看器的规则一致，绝不拿最后一条消息的发送时间顶替。
    """
    try:
        conn = database.get_db()
    except Exception:  # noqa: BLE001 —— 读不到就当没有记录，页面照常显示
        return None
    try:
        row = conn.execute("SELECT MAX(collected_at) FROM conversations").fetchone()
    except Exception:  # noqa: BLE001 —— 老库没有这一列时同样按「没有记录」处理
        return None
    finally:
        conn.close()
    return _int(row[0]) or None


def collect(force: bool = False) -> dict:
    """返回一份「数据总览」，并带上「数据统计截止至」。

    截止时间不进缓存：刚采集完应当马上反映出来，这一条查询本身也很轻。
    """
    data = _collect(force)
    data["collected_at"] = _read_collected_at()
    return data


def _collect(force: bool = False) -> dict:
    """返回一份「数据总览」。

    默认路径永不阻塞：有缓存给缓存；过期也先给旧数字并在后台重算；从没算过
    就返回 computing=True，让面板显示「正在统计…」。

    force=True 时同步重算（测试和以后的手动刷新用），失败则退回旧缓存。
    """
    global _cache

    if force:
        try:
            stats = _read_stats()
            disk = _read_disk()
        except Exception as exc:  # noqa: BLE001
            print(f"[overview] 强制刷新失败：{exc}", flush=True)
            with _lock:
                cache = _cache
            if cache is not None:
                return _build(cache[0], cache[1], cache[2], stale=True)
            return _build_empty(computing=False)
        stamp = time.time()
        with _lock:
            _cache = (stamp, stats, disk)
        return _build(stamp, stats, disk, stale=False)

    with _lock:
        cache = _cache
        now = time.time()

    if cache is not None and (now - cache[0]) < CACHE_TTL:
        return _build(cache[0], cache[1], cache[2], stale=False)
    if cache is not None:
        _start_refresh()
        return _build(cache[0], cache[1], cache[2], stale=True)

    _start_refresh()
    return _build_empty(computing=True)


def reset_cache() -> None:
    """清掉进程级缓存（测试在换临时库时必须调，否则会读到上一个库的数字）。"""
    global _cache
    with _lock:
        _cache = None
