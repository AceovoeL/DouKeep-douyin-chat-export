"""Shared SQLite storage core: connection factory, schema, and writer helpers.

Previously `backend/database.py` and `extractor/models.py` each defined their
own `DB_PATH` + `get_db`, with *divergent* pragmas — the reader (backend) left
foreign keys OFF (its two-step delete relies on no cascade), while the writer
(extractor) enabled WAL + foreign_keys. That divergence is preserved here
explicitly via `connect(foreign_keys=..., wal=...)` rather than being papered
over.

The DB path is read from `common.paths.DB_PATH` at call time so tests can
repoint it at a temp file with a single monkeypatch.
"""
import json
import os
import sqlite3
import time

from common import paths


def connect(*, foreign_keys: bool = False, wal: bool = False) -> sqlite3.Connection:
    """Open a connection to the chat DB.

    Args:
        foreign_keys: enable PRAGMA foreign_keys=ON (writer side).
        wal: enable PRAGMA journal_mode=WAL (writer side).
    """
    conn = sqlite3.connect(paths.DB_PATH)
    conn.row_factory = sqlite3.Row
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(paths.DB_PATH), exist_ok=True)
    conn = connect(foreign_keys=True, wal=True)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            uid TEXT PRIMARY KEY,
            nickname TEXT,
            avatar_url TEXT,
            unique_id TEXT
        );

        CREATE TABLE IF NOT EXISTS conversations (
            conv_id TEXT PRIMARY KEY,
            conv_type INTEGER DEFAULT 1,
            name TEXT,
            participant_uids TEXT DEFAULT '[]',
            last_message_time INTEGER DEFAULT 0,
            message_count INTEGER DEFAULT 0,
            display_count INTEGER
        );

        CREATE TABLE IF NOT EXISTS messages (
            msg_id TEXT PRIMARY KEY,
            conv_id TEXT NOT NULL,
            sender_uid TEXT,
            sender_name TEXT,
            content TEXT,
            msg_type INTEGER DEFAULT 1,
            media_url TEXT,
            media_local_path TEXT,
            -- 实况图（会动的图）里那段小视频；静态封面照旧存在 media_local_path，
            -- 两列分开才不会互相覆盖（见 extractor/video_downloader.py）。
            live_video_path TEXT,
            timestamp INTEGER,
            seq INTEGER DEFAULT 0,
            raw_data TEXT,
            ref_msg TEXT,
            FOREIGN KEY (conv_id) REFERENCES conversations(conv_id)
        );

        CREATE TABLE IF NOT EXISTS voice_transcriptions (
            msg_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            text_result TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'success',
            error TEXT,
            updated_at INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (msg_id) REFERENCES messages(msg_id) ON DELETE CASCADE
        );

        -- 消息原始数据包：抓取时把抖音返回的整包 protobuf 字段原样留档。
        -- 抖音会复用同一个字段表达不同行为（第 11 号字段既做撤回时间、又做编辑时间、
        -- 又做表情快捷回复时间），只挑固定字段解析会丢掉区分它们的信息，
        -- 所以整包字段单独存一张表，messages.raw_data 只放轻量摘要。
        CREATE TABLE IF NOT EXISTS message_packets (
            msg_id TEXT PRIMARY KEY,
            conv_id TEXT,
            captured_at INTEGER NOT NULL DEFAULT 0,
            size INTEGER NOT NULL DEFAULT 0,
            packet TEXT,
            fields TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_messages_seq ON messages(conv_id, seq);
        CREATE INDEX IF NOT EXISTS idx_messages_content ON messages(content);
        CREATE INDEX IF NOT EXISTS idx_voice_transcriptions_status
            ON voice_transcriptions(status);
        CREATE INDEX IF NOT EXISTS idx_message_packets_conv
            ON message_packets(conv_id);
    """)
    # 迁移：为旧数据库添加 ref_msg 列
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN ref_msg TEXT")
    except sqlite3.OperationalError:
        pass  # 列已存在
    # 迁移：为旧数据库添加 messages.live_video_path 列（实况图的小视频）
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN live_video_path TEXT")
    except sqlite3.OperationalError:
        pass  # 列已存在
    # 迁移：为旧数据库添加 conversations.avatar_url 列
    try:
        conn.execute("ALTER TABLE conversations ADD COLUMN avatar_url TEXT")
    except sqlite3.OperationalError:
        pass  # 列已存在
    # 迁移：为旧数据库添加 conversations.display_count 列
    # NULL 表示「还没算过」，阅读端（后端）取数据时会补算并写回。
    try:
        conn.execute("ALTER TABLE conversations ADD COLUMN display_count INTEGER")
    except sqlite3.OperationalError:
        pass  # 列已存在
    conn.commit()
    conn.close()


def upsert_user(conn, uid, nickname=None, avatar_url=None, unique_id=None):
    conn.execute(
        """INSERT INTO users (uid, nickname, avatar_url, unique_id)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(uid) DO UPDATE SET
             nickname=COALESCE(excluded.nickname, nickname),
             avatar_url=COALESCE(excluded.avatar_url, avatar_url),
             unique_id=COALESCE(excluded.unique_id, unique_id)""",
        (uid, nickname, avatar_url, unique_id),
    )


def upsert_conversation(conn, conv_id, conv_type=1, name=None, participant_uids=None, avatar_url=None):
    participants = json.dumps(participant_uids or [])
    conn.execute(
        """INSERT INTO conversations (conv_id, conv_type, name, participant_uids, avatar_url)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(conv_id) DO UPDATE SET
             conv_type=COALESCE(excluded.conv_type, conv_type),
             name=COALESCE(excluded.name, name),
             participant_uids=COALESCE(excluded.participant_uids, participant_uids),
             avatar_url=COALESCE(excluded.avatar_url, avatar_url)""",
        (conv_id, conv_type, name, participants, avatar_url),
    )


def update_conversation_stats(conn, conv_id):
    """刷新会话的原始条数与最后消息时间，并把 display_count 标为待重算。

    display_count（真正显示出来的条数，见 common/display_rules.py）由阅读端
    按需计算后写回；这里改成 NULL 就表示"数据变了，之前算的不作数了"。
    """
    conn.execute(
        """UPDATE conversations SET
             message_count = (SELECT COUNT(*) FROM messages WHERE conv_id = ?),
             last_message_time = (SELECT MAX(timestamp) FROM messages WHERE conv_id = ?),
             display_count = NULL
           WHERE conv_id = ?""",
        (conv_id, conv_id, conv_id),
    )


def upsert_voice_transcription(conn, msg_id, message_id, text_result="",
                               status="success", error=None, updated_at=None):
    """Insert or replace the native Douyin voice-recognition result.

    ``text_result`` is allowed to be empty on a successful recognition (for
    example, a silent recording), so callers must use ``status`` rather than
    the text value to decide whether a message needs another request.
    """
    if updated_at is None:
        updated_at = int(time.time())
    conn.execute(
        """INSERT INTO voice_transcriptions
           (msg_id, message_id, text_result, status, error, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(msg_id) DO UPDATE SET
             message_id=excluded.message_id,
             text_result=excluded.text_result,
             status=excluded.status,
             error=excluded.error,
             updated_at=excluded.updated_at""",
        (str(msg_id), str(message_id), text_result or "", str(status), error,
         int(updated_at)),
    )
