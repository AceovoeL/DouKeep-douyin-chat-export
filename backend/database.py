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


def search_messages(query="", page=1, page_size=50, *, conv_id=None,
                    start_time=None, end_time=None, media_type=None):
    """Search text/transcripts with optional conversation, time and media filters.

    Time bounds are [start_time, end_time), so adjacent dates never overlap.
    """
    raw = "CASE WHEN json_valid(m.raw_data) THEN m.raw_data ELSE '{}' END"
    content = f"json_extract({raw}, '$.content_json')"
    cj = f"CASE WHEN json_valid({content}) THEN {content} ELSE '{{}}' END"
    patch = f"json_extract({cj}, '$.im_dynamic_patch.raw_data')"
    layout = f"CASE WHEN json_valid({patch}) THEN {patch} ELSE '{{}}' END"
    clauses, params = [], []
    if query:
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        fields = ["m.content", "vt.text_result", *[
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
        clauses.append("(" + " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for field in fields) + ")")
        params.extend([pattern] * len(fields))
    if conv_id is not None:
        clauses.append("m.conv_id = ?")
        params.append(conv_id)
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
    elif media_type is not None:
        raise ValueError("未知媒体类型")
    where = " AND ".join(clauses) or "1=1"
    order_by = (
        "m.timestamp DESC, m.seq DESC, m.msg_id DESC"
        if media_type == "forward"
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
        total = conn.execute(f"SELECT COUNT(*) {joins} WHERE {where}", params).fetchone()[0]
        return [dict(r) for r in rows], total
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
