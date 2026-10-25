"""Database access layer for the web backend (read + delete queries).

The connection factory and schema live in `common.db`; the reader uses
`connect()` with foreign keys OFF (its two-step delete relies on no cascade).
"""
import threading

from common.bubble_assets import bubble_id_from_raw_data
from common.db import connect
from common.display_rules import hidden_message_ids
from common.owner import detect_owner
from common.paths import DB_PATH  # re-exported for backward compatibility


def get_db():
    return connect()


def _attach_bubble_ids(items):
    """给消息补上 bubble_id（旧库没有这一列，从 raw_data 里现取）。

    新采集的消息会在 raw_data 顶层写 bubble_id；旧消息翻 fields.f9。
    解析失败或没有装饰时补空串，前端照常画普通气泡。
    """
    for item in items:
        if item.get("bubble_id"):
            continue
        item["bubble_id"] = bubble_id_from_raw_data(item.get("raw_data"))
    return items


# 判定「显示条数」需要的列（见 common/display_rules.py）。
_DISPLAY_COLUMNS = ("msg_id, conv_id, sender_uid, msg_type, content, "
                    "timestamp, seq, raw_data")


def _load_conversation_rows(conn, conv_id):
    return [dict(row) for row in conn.execute(
        f"SELECT {_DISPLAY_COLUMNS} FROM messages WHERE conv_id = ? ORDER BY seq, msg_id",
        (conv_id,),
    )]


# ── 「显示条数」的内存缓存 ────────────────────────────────────────────────
#
# 为什么要缓存：算一次要把该会话**全部消息连同 raw_data** 读出来再逐条跑
# common/display_rules（raw_data 平均 1.8KB，实测一个 5 万条的会话要 30 秒以上）。
# 以前这件事发生在 GET /api/conversations 的请求里 —— 抓取器刚把
# conversations.display_count 置成 NULL，用户下一次打开查看器就要同步等这么久，
# 而前端那时还没有加载态，列表是空的，看着就是「暂无会话数据」（2026-10-25 排查）。
#
# 缓存键带一个「数据版本号」：抓取器改了数据就会把 display_count 置 NULL，
# 版本号跟着变，缓存自然失效，不需要谁去通知谁。
#: 会话超过这么多条就改成后台算显示条数。
#:
#: 阈值是按「读 raw_data 的代价」定的：raw_data 平均 1.8KB，几千条也就是几 MB、
#: 几十毫秒，当场算准反而更简单；上万条就上几百 MB、要几十秒，那种才必须挪到
#: 后台，绝不能让一个 HTTP 请求等着。
SYNC_DISPLAY_COUNT_LIMIT = 3000

_display_cache: dict[str, tuple[tuple, int]] = {}
#: 发送者分布也一起缓存（键是同一份数据版本，见 _display_stamp）。
_senders_cache: dict[str, tuple[tuple, list]] = {}
_display_cache_lock = threading.Lock()
#: 已经排进队列、还没算完的会话（同一会话只算一遍，别被并发的几个请求各算一遍）。
_display_warming: set[str] = set()
#: 待算队列 + 唯一的那个后台工作线程。
#:
#: 为什么只开一个线程：一个 5 万条的会话要把几百 MB 的 raw_data 从磁盘读出来，
#: 几个会话同时算就是几条这样的读流抢同一块盘，谁都更慢，还会把正常的消息请求
#: 一起拖慢。串行慢慢算，排在后面的会话只是晚几秒变准，不影响任何请求。
_display_queue: list[tuple[str, str]] = []
_display_worker: threading.Thread | None = None
#: 每清一次缓存就加一。排队时记下当时的代数，算完发现代数变了就丢弃结果 ——
#: 否则「清空之后重新排队」会被上一个回合遗留的后台任务顶掉（测试里切库时最明显）。
_display_generation = 0


#: 清空内存缓存（换库、跑测试时用）。还没算完的后台任务会作废。
def clear_display_cache():
    global _display_generation
    with _display_cache_lock:
        _display_cache.clear()
        _senders_cache.clear()
        _display_warming.clear()
        _display_queue.clear()
        _display_generation += 1


def display_count_pending():
    """还有多少个会话的显示条数在后台算（列表接口用它告诉前端「稍后会变准」）。"""
    with _display_cache_lock:
        return len(_display_warming)


def _display_stamp(conversation):
    """这份缓存对应的数据版本：库文件、条数、最后时间、以及库里的 display_count。

    抓取器刷新统计时会把后三样一起改（display_count 置 NULL），所以任何一次
    数据变动都会让旧缓存对不上号，不会读到过期的条数。库文件也进签名，是为了
    别让「换了一个库」（测试、导入数据后切换）读到上一个库的条数。
    """
    return (
        DB_PATH,
        conversation.get("message_count"),
        conversation.get("last_message_time"),
        conversation.get("display_count"),
    )


def _count_display_rows(conn, conv_id):
    """现算「真正显示出来的条数」（贵：要读整个会话的 raw_data）。"""
    rows = _load_conversation_rows(conn, conv_id)
    if not rows:
        return 0
    return len(rows) - len(hidden_message_ids(rows))


def _warm_display_count(conv_id, db_path=None, generation=None):
    """在后台线程里把某个会话的显示条数算好，塞进内存缓存。

    只写内存，**不写库**：这个函数不该因为抓取器正占着数据库而失败，也不该
    在阅读端留下写操作。算完就退出，下一份数据变动会换一个缓存键。

    ``db_path`` / ``generation`` 是排队那一刻的库文件和缓存代数：算的是当初那个
    库，而且只要中途清过缓存（换库、测试），这次的结果就作废。
    """
    if db_path is None:
        db_path = DB_PATH
    try:
        if db_path != DB_PATH:
            return  # 库已经换了：这次排队的活作废
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM conversations WHERE conv_id = ?", (conv_id,)
            ).fetchone()
            if not row:
                return
            conversation = dict(row)
            value = _count_display_rows(conn, conv_id)
        finally:
            conn.close()
        with _display_cache_lock:
            if db_path != DB_PATH:
                return
            if generation is not None and generation != _display_generation:
                return  # 中途清过缓存：这份结果不算数
            _display_cache[conv_id] = (_display_stamp(conversation), value)
    except Exception:  # noqa: BLE001 —— 预热失败不该影响任何请求
        pass


def _display_worker_loop():
    """一个后台线程，按排队顺序把待算的会话一个个算完。"""
    while True:
        with _display_cache_lock:
            if not _display_queue:
                global _display_worker
                _display_worker = None
                return
            conv_id, db_path, generation = _display_queue.pop(0)
        _warm_display_count(conv_id, db_path, generation)
        with _display_cache_lock:
            if generation == _display_generation:
                _display_warming.discard(conv_id)


def _start_warm(conv_id):
    global _display_worker
    with _display_cache_lock:
        if conv_id in _display_warming:
            return
        _display_warming.add(conv_id)
        _display_queue.append((conv_id, DB_PATH, _display_generation))
        if _display_worker is not None and _display_worker.is_alive():
            return
        _display_worker = threading.Thread(
            target=_display_worker_loop, name="display-count-worker", daemon=True,
        )
        _display_worker.start()


def resolve_display_count(conn, conversation, *, warm=False):
    """会话里「真正显示出来」的消息条数。

    取值顺序：
      1. 库里的 ``conversations.display_count`` —— 抓取器算过就是它，最省事；
      2. 内存缓存 —— 这次运行算过的（含后台预热算好的）；
      3. 都没有就现算。**小会话当场算准**（几百条只是一瞬间的事，测试和
         日常小库都靠这条）；只有大会话才退回「先给个估计值 + 后台算」
         （``warm=True`` 时），绝不在这条路径上同步读几百 MB 的 raw_data。

    Args:
        warm: 允许大会话改为后台重算。列表接口传 True；只读单个会话的接口
            也传 True（用户点开的就是它，后台算好之后下一次就准了）。

    Returns:
        条数。只有「大会话 + ``warm=True`` + 还没算出来」这一种情况下，
        返回的是估计值（原始条数）。
    """
    stored = conversation.get("display_count")
    if stored is not None:
        with _display_cache_lock:
            _display_cache[conversation["conv_id"]] = (
                _display_stamp(conversation), stored,
            )
        return stored

    conv_id = conversation["conv_id"]
    stamp = _display_stamp(conversation)
    with _display_cache_lock:
        cached = _display_cache.get(conv_id)
    if cached and cached[0] == stamp:
        return cached[1]

    raw = conversation.get("message_count")
    if raw is None:
        raw = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conv_id = ?", (conv_id,)
        ).fetchone()[0]
    # 小会话直接算准：读几百条 raw_data 是毫秒级，没必要为了它绕一圈后台，
    # 也让「打开会话看到的条数」立刻是对的（截图、导出、测试都依赖这个）。
    if raw <= SYNC_DISPLAY_COUNT_LIMIT:
        value = _count_display_rows(conn, conv_id)
        with _display_cache_lock:
            _display_cache[conv_id] = (stamp, value)
        return value

    if warm:
        _start_warm(conv_id)

    # 大会话还没算出来：先按原始条数顶上（界面上是「N 条消息」，比显示 0 条
    # 更像话），后台算完的准确值会在下一次请求生效。
    return raw


def find_referenced_video(msg_id):
    """A related_share_video contains a video ID, not a message ID.

    Locate the nearest preceding original share in the same conversation.
    """
    from .forwarded import content_json
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM messages WHERE msg_id=?", (msg_id,)).fetchone()
        if not row:
            return None
        source = dict(row)
        video = content_json(source).get("related_share_video") or {}
        item_id = str(video.get("itemId") or "")
        if not item_id.isdigit():
            return None
        rows = conn.execute(
            "SELECT * FROM messages WHERE conv_id=? AND seq<? "
            "AND raw_data LIKE ? ORDER BY seq DESC",
            (source["conv_id"], source["seq"], f"%{item_id}%"),
        )
        for candidate in rows:
            message = dict(candidate)
            cj = content_json(message)
            if str(cj.get("itemId") or "") == item_id and not cj.get("related_share_video"):
                return message
        return None
    finally:
        conn.close()


def get_conversations(search=None, page=1, page_size=50):
    conn = get_db()
    offset = (page - 1) * page_size

    if search:
        rows = conn.execute(
            """SELECT * FROM conversations
               WHERE name LIKE ?
               ORDER BY last_message_time DESC
               LIMIT ? OFFSET ?""",
            (f"%{search}%", page_size, offset),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE name LIKE ?",
            (f"%{search}%",),
        ).fetchone()[0]
    else:
        rows = conn.execute(
            """SELECT * FROM conversations
               ORDER BY last_message_time DESC
               LIMIT ? OFFSET ?""",
            (page_size, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]

    items = [dict(r) for r in rows]
    for item in items:
        item["display_count"] = resolve_display_count(conn, item, warm=True)
    conn.close()
    return items, total


def get_conversation(conv_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM conversations WHERE conv_id = ?", (conv_id,)
    ).fetchone()
    item = dict(row) if row else None
    if item:
        item["display_count"] = resolve_display_count(conn, item, warm=True)
    conn.close()
    return item


def get_messages(conv_id, page_size=100, before_seq=None, after_seq=None):
    conn = get_db()
    message_select = """SELECT m.*,
                              vt.text_result AS voice_transcription,
                              vt.status AS voice_transcription_status,
                              vt.error AS voice_transcription_error
                       FROM messages m
                       LEFT JOIN voice_transcriptions vt ON vt.msg_id = m.msg_id
                       WHERE m.conv_id = ?"""

    if before_seq:
        # 加载更早的消息（向上滚动时调用）
        rows = conn.execute(
            message_select + " AND m.seq < ? ORDER BY m.seq DESC LIMIT ?",
            (conv_id, before_seq, page_size),
        ).fetchall()
        rows = list(reversed(rows))
    elif after_seq is not None:
        # 从指定 seq 开始向后加载（跳到开头时调用，after_seq=0 即从头）
        rows = conn.execute(
            message_select + " AND m.seq > ? ORDER BY m.seq ASC LIMIT ?",
            (conv_id, after_seq, page_size),
        ).fetchall()
    else:
        # 初始加载：最新的100条
        rows = conn.execute(
            message_select + " ORDER BY m.seq DESC LIMIT ?",
            (conv_id, page_size),
        ).fetchall()
        rows = list(reversed(rows))

    # total 是「界面上真正显示出来的条数」，不是数据库里的原始行数
    # （原始行数含抖音给双方各发一份的镜像 / 空载荷，见 common/display_rules.py）。
    # 跟会话列表走同一份缓存：这个数字没变过就不必为它重读整个会话。
    conversation = conn.execute(
        "SELECT * FROM conversations WHERE conv_id = ?", (conv_id,)
    ).fetchone()
    if conversation:
        total = resolve_display_count(conn, dict(conversation), warm=True)
    else:
        total = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conv_id = ?", (conv_id,)
        ).fetchone()[0]

    conn.close()
    return _attach_bubble_ids([dict(r) for r in rows]), total


def get_message_page_bounds(conv_id, items):
    """Return whether messages exist on either side of a loaded page."""
    seqs = [int(item["seq"]) for item in items if item.get("seq") is not None]
    if not seqs:
        return {"has_older": False, "has_newer": False}
    conn = get_db()
    row = conn.execute(
        """SELECT
               EXISTS(SELECT 1 FROM messages WHERE conv_id = ? AND seq < ?) AS has_older,
               EXISTS(SELECT 1 FROM messages WHERE conv_id = ? AND seq > ?) AS has_newer""",
        (conv_id, min(seqs), conv_id, max(seqs)),
    ).fetchone()
    conn.close()
    return {"has_older": bool(row[0]), "has_newer": bool(row[1])}



def get_messages_by_date(conv_id, date_str, tz_hours=8, limit=5000):
    """某个自然日（按 tz_hours 时区界定）内的全部消息，按 seq 升序。

    date_str: "YYYY-MM-DD"。limit 是防御性上限，正常单日消息量远低于它。
    """
    offset_sec = int(tz_hours * 3600)
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM messages
           WHERE conv_id = ?
             AND date(timestamp + ?, 'unixepoch') = ?
           ORDER BY seq ASC
           LIMIT ?""",
        (conv_id, offset_sec, date_str, limit),
    ).fetchall()
    conn.close()
    return _attach_bubble_ids([dict(r) for r in rows])


def get_messages_range(conv_id, start_seq, end_seq, limit=1000):
    """闭区间 [start_seq, end_seq] 的消息，按 seq 升序。"""
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM messages
           WHERE conv_id = ? AND seq >= ? AND seq <= ?
           ORDER BY seq ASC
           LIMIT ?""",
        (conv_id, start_seq, end_seq, limit),
    ).fetchall()
    conn.close()
    return _attach_bubble_ids([dict(r) for r in rows])


def get_daily_stats(conv_id, tz_hours=8):
    """按自然日（tz_hours 时区）统计消息量：[{date, count}, ...] 升序。"""
    offset_sec = int(tz_hours * 3600)
    conn = get_db()
    rows = conn.execute(
        """SELECT date(timestamp + ?, 'unixepoch') AS date, COUNT(*) AS count
           FROM messages
           WHERE conv_id = ?
           GROUP BY date
           ORDER BY date ASC""",
        (offset_sec, conv_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_senders(conv_id):
    """获取会话中的所有发送者 UID 及**显示出来的**消息数量。

    这个统计和 display_count 走的是同一套「读整个会话 + 跑显示规则」，同样很贵
    （大群聊实测 1.6～2.5 秒）。它跟着 display_count 的缓存一起记：条数没变过就
    直接复用，不必每次打开会话都重算一遍。
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM conversations WHERE conv_id = ?", (conv_id,)
        ).fetchone()
        conversation = dict(row) if row else None
        stamp = _display_stamp(conversation) if conversation is not None else None
        if stamp is not None:
            with _display_cache_lock:
                cached = _senders_cache.get(conv_id)
            if cached and cached[0] == stamp:
                return cached[1]
        rows = _load_conversation_rows(conn, conv_id)
    finally:
        conn.close()

    hidden = hidden_message_ids(rows)
    counts = {}
    for row in rows:
        if row.get("msg_id") in hidden:
            continue
        uid = row.get("sender_uid")
        counts[uid] = counts.get(uid, 0) + 1
    result = [{"sender_uid": uid, "msg_count": count}
              for uid, count in sorted(counts.items(), key=lambda kv: -kv[1])]
    if stamp is not None:
        with _display_cache_lock:
            _senders_cache[conv_id] = (stamp, result)
    return result


def get_owner():
    """本机账号是谁：``{"uid": ..., "name": ...}``，认不出来时两项都是空串。

    查看器拿它当「我」的默认值（见 common/owner.py），「设置我」弹窗也靠它把本机
    账号列进去 —— 群聊里自己可能一条消息都没发过，光看发送者名单是找不到自己的。
    """
    conn = get_db()
    try:
        uid, name = detect_owner(conn)
    finally:
        conn.close()
    return {"uid": uid, "name": name}


# 数「一共找到多少条」时最多数这些条。搜索要匹配的字段里有几个是
# raw_data 里现拆出来的 JSON（这一列在真实库里能到几百 MB），精确数一遍
# 要几秒钟；只要知道「比 1000 条多」就够了，于是让 SQLite 数够就停。
#
# 顺带说明为什么搜索本身没法再快多少：判断一行是否匹配，必须把那行的
# raw_data 解析成 JSON 才能比较，而 raw_data 是几百 MB 的大文本。一个词
# 越少见，要检查的行就越多（实测很少见的词要 3～4 秒，常见词 0.1 秒就够）。
# 真正根治要把可搜索文本单独存成一列或建全文索引 —— 那要改数据库结构，
# 不在本次改动范围内。
SEARCH_COUNT_LIMIT = 1000

#: 语音消息在库里没有专属的 msg_type：绝大多数存成 0（普通消息），少数老数据存成 1。
#: 真正的判据是正文 JSON 里的语音字段，所以「筛选语音」要按下面这几条来认，规则和
#: 前端 frontend/src/lib/douyinMessage.js 的 isVoiceMsg、阅读端的
#: common/display_rules.py::is_voice_msg 保持一致（三处改动要同步）。
#:
#: 判断分三步，顺序不能换（前端也是这个顺序，换了结果就不一样）：
#:   0. 正文里得有 resource_url，这是语音的前提；
#:   1. 排除「长得像语音其实是别的」：aweType 2702/2703/2704 的卡片、带 video.vid
#:      的视频。这两种只要没有语音标记（tkey / voice_wave / is_voice）就直接否掉 ——
#:      注意是直接否掉，不能因为「有时长」又把它算回语音，视频也带时长；
#:   2. 剩下的按「有地址 / 有时长 / 有语音标记」任一条认；
#:      但如果这行的 msg_type 不是 0/空（老数据存成了 1），判据收紧成
#:      「有时长或语音标记」，光有地址不算（图片也有地址）。
_VOICE_MARKER = (
    "COALESCE(json_extract({cj}, '$.tkey'), '') <> ''"
    " OR COALESCE(json_extract({cj}, '$.voice_wave'), '') <> ''"
    " OR COALESCE(json_extract({cj}, '$.resource_url.is_voice'), '') <> ''"
)


def _voice_clause(cj):
    """「这一行是语音」的 SQL 条件（三步判据见 _VOICE_MARKER 上面的说明）。"""
    marker = _VOICE_MARKER.format(cj=cj)
    # duration 可能是数字也可能是字符串，两种都认；0 和空串不算。
    dur = "$.duration"
    res_dur = "$.resource_url.duration"
    duration = (
        f"((json_extract({cj}, '{dur}') IS NOT NULL"
        f" AND json_extract({cj}, '{dur}') <> '' AND json_extract({cj}, '{dur}') <> 0)"
        f" OR (json_extract({cj}, '{res_dur}') IS NOT NULL"
        f" AND json_extract({cj}, '{res_dur}') <> '' AND json_extract({cj}, '{res_dur}') <> 0))"
    )
    # 「有下载地址」只看 resource_url 对象里的 url_list。resource_url 本身是字符串的
    # 情况不算地址 —— 前端也只认数组形式的 url_list，两边必须一样。
    res = "$.resource_url"
    url = (
        f"(json_type({cj}, '{res}') = 'object'"
        f" AND json_array_length({cj}, '{res}.url_list') > 0)"
    )
    # 第 0 步：没有 resource_url 的不可能是语音。
    has_resource = f"json_type({cj}, '{res}') IN ('object', 'text')"
    # 第 1 步：视频卡 / 视频消息，除非带语音标记，否则一律不算。
    # aweType 缺字段时是 NULL，而 NULL IN (...) 得到的是 NULL 不是假 —— 拿它去
    # NOT 一下还是 NULL，整行就被判掉了。所以用 IFNULL 兜成空串再比。
    video_like = (
        f"IFNULL(CAST(json_extract({cj}, '$.aweType') AS TEXT), '')"
        f" IN ('2702', '2703', '2704')"
        f" OR COALESCE(json_extract({cj}, '$.video.vid'), '') <> ''"
    )
    # 第 2 步：宽松判据（msg_type 为 0/空时用）与严格判据（其余情况用）。
    loose = f"({url} OR {duration} OR ({marker}))"
    strict = f"({duration} OR ({marker}))"
    stored_voice = "m.msg_type = 0 OR m.msg_type = 'other' OR m.msg_type IS NULL"
    return (
        "json_valid(m.raw_data) AND "
        f"{has_resource} AND "
        f"(NOT ({video_like}) OR ({marker})) AND "
        f"CASE WHEN {stored_voice} THEN {loose} ELSE {strict} END"
    )


def search_messages(query="", page=1, page_size=50, *, conv_id=None,
                    start_time=None, end_time=None, media_type=None,
                    count_limit=None, with_total=True,
                    with_total_capped=False):
    """Search text/transcripts with optional conversation, time and media filters.

    Time bounds are [start_time, end_time), so adjacent dates never overlap.

    Args:
        count_limit: 最多数到多少条就停。默认取模块级的 ``SEARCH_COUNT_LIMIT``
            （在函数里读，这样它还能被测试或配置改写）；显式传 ``0`` 表示数到底、
            要准确数字。
        with_total: 为 False 时只取这一页数据、不数总数（翻页用；翻页时总数
            本来就不会变，没必要为了一个不变的数字再等几秒）。
        with_total_capped: 为 True 时返回值多带一个布尔值，表示上面的总数是
            不是被 ``count_limit`` 截断过（上限本身就是那个数，两者相等时分不清）。

    Returns:
        ``([消息...], 总数)``；``with_total_capped=True`` 时是
        ``([消息...], 总数, 是否截断)``。不传这个开关时返回值形状与以前一致。
    """
    if count_limit is None:
        count_limit = SEARCH_COUNT_LIMIT

    def _result(rows, total, capped):
        _attach_bubble_ids(rows)
        if with_total_capped:
            return rows, total, capped
        return rows, total

    raw = "CASE WHEN json_valid(m.raw_data) THEN m.raw_data ELSE '{}' END"
    content = f"json_extract({raw}, '$.content_json')"
    cj = f"CASE WHEN json_valid({content}) THEN {content} ELSE '{{}}' END"
    patch = f"json_extract({cj}, '$.im_dynamic_patch.raw_data')"
    layout = f"CASE WHEN json_valid({patch}) THEN {patch} ELSE '{{}}' END"
    clauses, params = [], []
    # conv_id 写进 WHERE 最前面：它是等值条件，能把候选行缩到一个会话。
    # （实测写前写后耗时基本一样 —— SQLite 自己会用 idx_messages_seq 定位会话，
    # 这里放前面只是为了读起来顺，不代表它带来提速。）
    if conv_id is not None:
        clauses.append("m.conv_id = ?")
        params.append(conv_id)
    if query:
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        # 普通列：读出来就能比，不花钱。
        plain_fields = ["m.content", "vt.text_result"]
        # JSON 字段：每一个都要先把该行整包 raw_data 解析成 JSON 才能比。
        # raw_data 在真实库里能到几百 MB，逐行解析就是搜索卡顿的根源。
        json_fields = [*[
            f"json_extract({cj}, '$.{key}')"
            # content_name is the share card's author; without it, searching an
            # author's name only worked by accident (their name happened to sit
            # in the stored content blob) and broke once that blob was rewritten.
            for key in ("content_title", "aweme_title", "content_name", "comment", "text")
        ], *[f"json_extract({layout}, '$.{key}.content')"
             for key in ("top_bottom_top", "content_top")],
            # 群通知（谁改了群名 / 群头像、谁被拉进群、谁开播了……）的句子只在
            # 多语言模板 locale_resources 里，落库正文是 "[系统消息]" 这个占位。
            # 所以要连模板原文和它点到的人名一起搜，否则搜「邀请」什么都搜不到。
            f"json_extract({cj}, '$.locale_resources')",
            f"json_extract({cj}, '$.active_users')",
            f"json_extract({cj}, '$.passive_users')"]
        # 便宜的列放前面只是让 SQL 好读；SQLite 不会为 OR 短路，两边都会被求值。
        # 曾试过在 OR 前面加一条 `m.raw_data LIKE ?` 把「整包文本里没这个词」
        # 的行提前排除掉（实测能快 5 倍），但那会漏搜：raw_data 里的中文绝大多数
        # 是 \uXXXX 转义的，而且嵌套层数不同、转义的反斜杠个数也不一样，
        # 没有任何一条固定模式能可靠命中（见 tests 里那两条分享卡标题的用例）。
        # 宁可慢也不能搜不到，所以不做这层粗筛。
        fields = plain_fields + json_fields
        clauses.append("(" + " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for field in fields) + ")")
        params.extend([pattern] * len(fields))
    if start_time is not None:
        clauses.append("m.timestamp >= ?")
        params.append(start_time)
    if end_time is not None:
        clauses.append("m.timestamp < ?")
        params.append(end_time)
    # Legacy video rows were stored as images/text. Inspect the preserved JSON
    # as well as the local file; malformed/truncated JSON must not break search.
    video = f"""(m.msg_type = 5 OR COALESCE(lower(m.media_local_path) LIKE '%.mp4', 0)
                OR (m.msg_type IN (1, 3) AND json_extract({cj}, '$.video.vid') IS NOT NULL))"""
    # 实况图（会动的图）= 一张静态封面 + 一段两三秒的小视频。封面存在
    # media_local_path 里、小视频存在 live_video_path 里，只有小视频真的下到
    # 本地了这一列才有值（见 extractor/video_downloader.py），所以它就是
    # 「这张图是实况图」的唯一可靠依据 —— 前端 getLivePhotoVideo 看的也是它。
    # 小视频没下下来时这张图退化成普通图片，跟图片筛选里一起出现。
    live = "COALESCE(m.live_video_path, '') <> ''"
    if media_type == "image":
        clauses.append(f"m.msg_type = 3 AND NOT {video} AND NOT {live}")
    elif media_type == "video":
        clauses.append(f"{video} AND NOT {live}")
    elif media_type == "live":
        clauses.append(live)
    elif media_type == "media":
        clauses.append(f"(m.msg_type = 3 OR {video})")
    elif media_type == "forward":
        awe = f"CAST(json_extract({cj}, '$.aweType') AS TEXT)"
        content_awe = (
            "CAST(json_extract(CASE WHEN json_valid(m.content) THEN m.content ELSE '{}' END, "
            "'$.aweType') AS TEXT)"
        )
        clauses.append(f"({awe} = '13600' OR {content_awe} = '13600')")
    elif media_type == "voice":
        clauses.append(_voice_clause(cj))
    elif media_type is not None:
        raise ValueError("未知媒体类型")
    where = " AND ".join(clauses) or "1=1"
    # 语音列表要「时间从新到旧」，所以按 timestamp 排；同一秒内的多条用 seq 还原
    # 聊天里的先后（seq 是采集顺序，比 msg_id 可靠）。
    order_by = (
        "m.timestamp DESC, m.seq DESC, m.msg_id DESC"
        if media_type in ("forward", "voice")
        else "m.seq DESC, m.msg_id DESC"
    )
    joins = """FROM messages m
               JOIN conversations c ON m.conv_id = c.conv_id
               LEFT JOIN users u ON m.sender_uid = u.uid
               LEFT JOIN voice_transcriptions vt ON vt.msg_id = m.msg_id"""
    conn = get_db()
    try:
        rows = conn.execute(
            f"""SELECT m.*, c.name as conv_name,
                       COALESCE(u.nickname, m.sender_name, '') as sender_display_name,
                       vt.text_result AS voice_transcription,
                       vt.status AS voice_transcription_status,
                       vt.error AS voice_transcription_error
                {joins} WHERE {where}
                ORDER BY {order_by} LIMIT ? OFFSET ?""",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        if not with_total:
            # 翻页：总数在第一次搜索时就给过用户了，翻页时它不会变。
            return _result([dict(r) for r in rows], None, False)
        if not count_limit:
            # 明确要求准确数字（count_limit=0）时的老路径：数到底。
            total = conn.execute(f"SELECT COUNT(*) {joins} WHERE {where}", params).fetchone()[0]
            return _result([dict(r) for r in rows], total, False)
        # 数够 count_limit 条就不再往下数。用子查询 + LIMIT，SQLite 找到足够
        # 行数就停；只有结果本来就不足上限时才会真的数到底，那时也便宜。
        total = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 {joins} WHERE {where} LIMIT ?)",
            [*params, count_limit],
        ).fetchone()[0]
        return _result([dict(r) for r in rows], total, total >= count_limit)
    finally:
        conn.close()


def get_message(msg_id):
    conn = get_db()
    row = conn.execute(
        """SELECT m.*,
                  vt.text_result AS voice_transcription,
                  vt.status AS voice_transcription_status,
                  vt.error AS voice_transcription_error
           FROM messages m
           LEFT JOIN voice_transcriptions vt ON vt.msg_id = m.msg_id
           WHERE m.msg_id = ?""",
        (msg_id,),
    ).fetchone()
    conn.close()
    item = dict(row) if row else None
    return _attach_bubble_ids([item])[0] if item else None


def get_user(uid):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_users():
    conn = get_db()
    rows = conn.execute("SELECT * FROM users").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats():
    conn = get_db()
    stats = {
        "conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
    }
    conn.close()
    return stats


def delete_conversation_messages(conv_id):
    """Delete all messages for a conversation (keep the conversation row)."""
    conn = get_db()
    conn.execute(
        """DELETE FROM voice_transcriptions
           WHERE msg_id IN (SELECT msg_id FROM messages WHERE conv_id = ?)""",
        (conv_id,),
    )
    cur = conn.execute("DELETE FROM messages WHERE conv_id = ?", (conv_id,))
    deleted = cur.rowcount
    conn.execute(
        """UPDATE conversations
           SET message_count = 0, display_count = 0, last_message_time = 0
           WHERE conv_id = ?""",
        (conv_id,),
    )
    conn.commit()
    conn.close()
    return deleted


def delete_conversation(conv_id):
    """Delete a conversation and all its messages."""
    conn = get_db()
    conn.execute(
        """DELETE FROM voice_transcriptions
           WHERE msg_id IN (SELECT msg_id FROM messages WHERE conv_id = ?)""",
        (conv_id,),
    )
    msg_cur = conn.execute("DELETE FROM messages WHERE conv_id = ?", (conv_id,))
    msg_deleted = msg_cur.rowcount
    conv_cur = conn.execute("DELETE FROM conversations WHERE conv_id = ?", (conv_id,))
    conv_deleted = conv_cur.rowcount
    conn.commit()
    conn.close()
    return {"conversation_deleted": conv_deleted, "messages_deleted": msg_deleted}
