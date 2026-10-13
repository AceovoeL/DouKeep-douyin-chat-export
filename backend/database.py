"""Database access layer for the web backend (read + delete queries).

The connection factory and schema live in `common.db`; the reader uses
`connect()` with foreign keys OFF (its two-step delete relies on no cascade).
"""
import sqlite3

from common.db import connect
from common.display_rules import display_count, sender_display_counts
from common.owner import detect_owner
from common.paths import DB_PATH  # re-exported for backward compatibility


def get_db():
    return connect()


# 判定「显示条数」需要的列（见 common/display_rules.py）。
_DISPLAY_COLUMNS = ("msg_id, conv_id, sender_uid, msg_type, content, "
                    "timestamp, seq, raw_data")


def _load_conversation_rows(conn, conv_id):
    return [dict(row) for row in conn.execute(
        f"SELECT {_DISPLAY_COLUMNS} FROM messages WHERE conv_id = ? ORDER BY seq, msg_id",
        (conv_id,),
    )]


def resolve_display_count(conn, conversation):
    """会话里「真正显示出来」的消息条数（算过就写进 conversations.display_count）。

    数据库里的原始行数和聊天窗口里画出来的条数不一样：抖音对同一次事件会向
    双方各发一份（互相关注 6 行只显示 2 行），另外还有空载荷、阅读端不认识
    的载荷等不显示的行。算一次之后缓存进库，`update_conversation_stats`
    在数据变化时把它清空重算。
    """
    stored = conversation.get("display_count")
    if stored is not None:
        return stored
    conv_id = conversation["conv_id"]
    value = display_count(_load_conversation_rows(conn, conv_id))
    try:
        conn.execute(
            "UPDATE conversations SET display_count = ? WHERE conv_id = ?",
            (value, conv_id),
        )
        conn.commit()
    except sqlite3.OperationalError:
        # 抓取进程正占着库时先返回算好的值，下次请求再补写。
        pass
    return value


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
        item["display_count"] = resolve_display_count(conn, item)
    conn.close()
    return items, total


def get_conversation(conv_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM conversations WHERE conv_id = ?", (conv_id,)
    ).fetchone()
    item = dict(row) if row else None
    if item:
        item["display_count"] = resolve_display_count(conn, item)
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
    conversation = conn.execute(
        "SELECT * FROM conversations WHERE conv_id = ?", (conv_id,)
    ).fetchone()
    if conversation:
        total = resolve_display_count(conn, dict(conversation))
    else:
        total = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conv_id = ?", (conv_id,)
        ).fetchone()[0]

    conn.close()
    return [dict(r) for r in rows], total


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
    return [dict(r) for r in rows]


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
    return [dict(r) for r in rows]


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
    """获取会话中的所有发送者 UID 及**显示出来的**消息数量。"""
    conn = get_db()
    counts = sender_display_counts(_load_conversation_rows(conn, conv_id))
    conn.close()
    return [{"sender_uid": uid, "msg_count": count}
            for uid, count in sorted(counts.items(), key=lambda kv: -kv[1])]


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
    if media_type == "image":
        clauses.append(f"m.msg_type = 3 AND NOT {video}")
    elif media_type == "video":
        clauses.append(video)
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
    return dict(row) if row else None


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
