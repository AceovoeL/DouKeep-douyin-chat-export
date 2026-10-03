"""ChatLab 远程数据源协议（Pull 模式）端点。

ChatLab 可以把本服务添加为「远程数据源」，自己定时来拉取增量消息：
https://docs.chatlab.fun/cn/standard/chatlab-pull

    GET /api/v1/sessions                         会话发现
    GET /api/v1/sessions/{conv_id}/messages      ChatLab Format + sync 分页

路径前缀是 ``/api/v1`` 而不是 ``/api/chatlab``：ChatLab 客户端会把用户填写的数据源
地址强制补上 ``/api/v1``（它把任何数据源都当成另一台 ChatLab 的 API 布局），所以用户
在 ChatLab 里只需填本服务根地址 ``http://host:8000``。
只读 GET，沿用永久 API token 鉴权（见 backend.main.auth_middleware）。
消息转换逻辑与文件导出共用 extractor.exporter，图片不内嵌 base64（用 [图片] 标签）。

游标语义（重要）
----------------
ChatLab 的游标是"上次拉到的最新消息时间戳"。但它的拉取引擎在**出错**时会把游标
写成当前墙钟时间；本服务是每天批量采集、消息时间戳早于采集时间，这会让出错前
尚未采集到的一段消息永远不再被请求。为此这里把 ``since`` 当作"水位线 + 回看窗口"：

    窗口起点 = since - PULL_LOOKBACK_SECONDS   （since<=0 表示全量）
    nextSince = 本页最后一条时间戳 + 1 + PULL_LOOKBACK_SECONDS

ChatLab 原样回传 nextSince，所以正常情况下下一页 / 下一次增量恰好从上一页末尾
继续（ChatLab 自身再减 60 秒重叠）；只有游标被重置成墙钟时间时，才会真的回看
PULL_LOOKBACK_SECONDS，重复的消息由 ChatLab 按 platformMessageId 去重。
"""
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from backend import database
from common import paths
from extractor.exporter import (
    _detect_owner,
    build_chatlab_header,
    build_chatlab_message,
    conv_display_name,
    sender_display_name,
)

chatlab_router = APIRouter(prefix="/api/v1", tags=["chatlab"])

# 回看窗口：要大于「采集可能中断的最长时间」。改小会让 ChatLab 已保存的游标向后跳、
# 产生漏洞，所以做成常量而不是配置项。
PULL_LOOKBACK_SECONDS = 7 * 86400

DEFAULT_PAGE_SIZE = 1000
MAX_PAGE_SIZE = 5000

_MESSAGE_SELECT = """SELECT m.*,
                      vt.text_result AS voice_transcription,
                      vt.status AS voice_transcription_status,
                      vt.error AS voice_transcription_error
               FROM messages m
               LEFT JOIN voice_transcriptions vt ON vt.msg_id = m.msg_id"""


def _load_users_map(conn) -> dict:
    return {
        u["uid"]: u["nickname"]
        for u in conn.execute("SELECT uid, nickname FROM users").fetchall()
        if u["uid"] and u["nickname"]
    }


def _conversation(conn, conv_id: str):
    return conn.execute(
        "SELECT conv_id, conv_type, name FROM conversations WHERE conv_id = ?",
        (conv_id,),
    ).fetchone()


def _members(conn, conv_id: str, conv_type: int, conv_name: str | None,
             users_map: dict, owner_uid: str, owner_name: str) -> list[dict]:
    """Every distinct sender in the conversation, named by the exporter's rule.

    MIN(seq) makes SQLite pick sender_name from each sender's earliest message,
    matching the file exporter's "first occurrence" behavior.
    """
    rows = conn.execute(
        "SELECT sender_uid, sender_name, MIN(seq) FROM messages "
        "WHERE conv_id = ? AND sender_uid != '' GROUP BY sender_uid",
        (conv_id,),
    ).fetchall()
    return [
        {
            "platformId": r["sender_uid"],
            "accountName": sender_display_name(
                r["sender_uid"], r["sender_name"], users_map=users_map,
                owner_uid=owner_uid, owner_name=owner_name,
                conv_type=conv_type, conv_name=conv_name,
            ),
        }
        for r in rows
    ]


@chatlab_router.get("/sessions")
def list_sessions(
    keyword: str | None = Query(None),
    limit: int | None = Query(None, ge=1),
    format: str | None = Query(None),
):
    """阶段一：会话发现。只列出至少有一条消息的会话，单页返回（不分页）。"""
    conn = database.get_db()
    try:
        stats = {
            r["conv_id"]: r
            for r in conn.execute(
                "SELECT conv_id, COUNT(*) AS n, COUNT(DISTINCT sender_uid) AS members, "
                "MAX(timestamp) AS last_ts FROM messages GROUP BY conv_id"
            ).fetchall()
        }
        convs = conn.execute(
            "SELECT conv_id, conv_type, name FROM conversations ORDER BY last_message_time DESC"
        ).fetchall()
    finally:
        conn.close()

    sessions = []
    for c in convs:
        st = stats.get(c["conv_id"])
        if not st or not st["n"]:
            continue
        name = conv_display_name(c["name"] or c["conv_id"], c["conv_type"])
        if keyword and keyword.lower() not in name.lower() and keyword not in c["conv_id"]:
            continue
        sessions.append({
            "id": c["conv_id"],
            "name": name,
            "platform": "douyin",
            "type": "group" if c["conv_type"] == 2 else "private",
            "messageCount": st["n"],
            "memberCount": st["members"],
            "lastMessageAt": st["last_ts"] or 0,
        })
    if limit:
        sessions = sessions[:limit]
    return {"sessions": sessions}


@chatlab_router.get("/sessions/{conv_id}/messages")
def session_messages(
    conv_id: str,
    since: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    format: str | None = Query(None),
    offset: int = Query(0, ge=0),
):
    """阶段二/三：按 since + limit 分页返回 ChatLab Format，附 sync 块。

    ``offset`` 只为兼容旧版 ChatLab 的 offset 续拉；本服务始终返回 nextSince，
    正常情况下不会收到 offset。
    """
    if format and format != "chatlab":
        raise HTTPException(400, "only format=chatlab is supported")

    window_start = 0 if since <= 0 else max(0, since - PULL_LOOKBACK_SECONDS)

    conn = database.get_db()
    try:
        conv = _conversation(conn, conv_id)
        if not conv:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        conv_type = conv["conv_type"]
        conv_name = conv["name"]

        rows = conn.execute(
            _MESSAGE_SELECT + """
               WHERE m.conv_id = ? AND m.timestamp >= ?
               ORDER BY m.timestamp ASC, m.seq ASC
               LIMIT ? OFFSET ?""",
            (conv_id, window_start, limit + 1, offset),
        ).fetchall()
        has_more = len(rows) > limit
        if has_more and rows[limit]["timestamp"] == rows[limit - 1]["timestamp"]:
            # 页被切在同一秒的一组消息中间：把这一秒补完整，否则 since 时间戳链
            # 在「同秒消息数 ≥ limit」时会原地打转。
            last = rows[limit - 1]
            rows = rows[:limit] + conn.execute(
                _MESSAGE_SELECT + """
                   WHERE m.conv_id = ? AND m.timestamp = ? AND m.seq > ?
                   ORDER BY m.seq ASC""",
                (conv_id, last["timestamp"], last["seq"]),
            ).fetchall()
            has_more = conn.execute(
                "SELECT 1 FROM messages WHERE conv_id = ? AND timestamp > ? LIMIT 1",
                (conv_id, last["timestamp"]),
            ).fetchone() is not None
        else:
            rows = rows[:limit]

        owner_uid, owner_name = _detect_owner(conn)
        body = build_chatlab_header(conv_id, conv_name, conv_type, owner_uid)
        if rows:
            users_map = _load_users_map(conn)
            body["members"] = _members(
                conn, conv_id, conv_type, conv_name, users_map, owner_uid, owner_name
            )
            previous_shares: dict = {}
            messages = []
            for msg in rows:
                if not (msg["timestamp"] or 0) > 0 or not msg["sender_uid"]:
                    continue  # ChatLab rejects the whole batch on ts<=0 / empty sender
                chatlab_msg, _stats = build_chatlab_message(
                    msg, conn, paths.MEDIA_DIR, users_map=users_map,
                    owner_uid=owner_uid, owner_name=owner_name,
                    conv_type=conv_type, conv_name=conv_name,
                    previous_shares=previous_shares, embed_images=False,
                )
                chatlab_msg.pop("replyTo", None)  # exporter-only extra, not in the standard
                messages.append(chatlab_msg)
            body["messages"] = messages
            # 页尾总是落在整秒边界上，下一窗口从下一秒开始。
            next_since = rows[-1]["timestamp"] + 1 + PULL_LOOKBACK_SECONDS
        else:
            # 空页保持 <1KB：ChatLab 对小空响应会短暂重试后正常结束，不触发导入。
            body["messages"] = []
            next_since = since
    finally:
        conn.close()

    body["sync"] = {"hasMore": has_more, "nextSince": next_since}
    return JSONResponse(body, headers={"Cache-Control": "no-store"})
