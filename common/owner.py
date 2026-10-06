"""「我是谁」：从库里推断本机账号（owner）的 uid。

阅读界面要把「自己发的消息」贴在右边、导出（ChatLab）也要写出 ``ownerId``，两处都得
知道哪一个是本机账号。库里没有任何一列写着「这是我」，只能从数据本身推：

1. ``conversations.participant_uids`` 的第一个 uid —— 采集时 ``curLoginUserInfo`` 排在最前；
2. 私聊 ``conv_id`` 里出现次数最多的 uid —— 私聊的 ``conv_id`` 形如
   ``0:1:<对方uid>:<某个uid>``，对方每换一个人就换一次，只有本机账号横跨几乎所有会话；
3. 兜底：在最多不同会话里发过消息的 ``sender_uid``（要扫全部消息，慢，能不用就不用）。

三条都认不出来就返回 ``("", "")``：界面退回「让用户自己选哪个是自己」。
"""
from __future__ import annotations

import json

#: 用「私聊里谁出现得最多」这一招至少要看到这么多个会话：只有一个会话时自己和对
#: 方各出现一次，根本分不出谁是谁 —— 那就不猜。
MIN_DIRECT_CHATS = 2

#: 认不出来时的占位昵称（导出沿用这个值，阅读界面会用它显示成「我」）
FALLBACK_NAME = "我"


def _nickname(conn, uid: str) -> str:
    if not uid:
        return ""
    row = conn.execute("SELECT nickname FROM users WHERE uid = ?", (uid,)).fetchone()
    return str(row[0]) if row and row[0] else ""


def _from_participants(conn) -> str:
    """策略 1：``participant_uids`` 的第一个 uid。"""
    row = conn.execute(
        "SELECT participant_uids FROM conversations "
        "WHERE participant_uids IS NOT NULL AND participant_uids NOT IN ('', '[]') LIMIT 1"
    ).fetchone()
    if not row:
        return ""
    try:
        uids = json.loads(row[0])
    except (TypeError, ValueError):
        return ""
    if isinstance(uids, list) and uids and str(uids[0] or "").strip():
        return str(uids[0]).strip()
    return ""


def _from_direct_chats(conn) -> str:
    """策略 2：私聊 ``conv_id`` 里出现次数最多的 uid。

    数的是「出现在多少个不同的会话里」，不是出现次数 —— 本机账号几乎每个私聊都在，
    任何一个对方只会出现在跟他的那一个会话里。并列（分不出谁更多）时直接放弃。
    """
    counts: dict[str, int] = {}
    for (conv_id,) in conn.execute("SELECT conv_id FROM conversations WHERE conv_id LIKE '%:%'"):
        # 只认最后两段：前面的 ``0:1`` 是会话类型，不是 uid
        pair = {part for part in str(conv_id or "").split(":")[-2:] if part}
        for uid in pair:
            counts[uid] = counts.get(uid, 0) + 1
    if not counts:
        return ""
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    uid, hits = ranked[0]
    if hits < MIN_DIRECT_CHATS:
        return ""
    if len(ranked) > 1 and ranked[1][1] == hits:
        return ""
    return uid


def _from_message_counts(conn) -> str:
    """策略 3：在最多不同会话里发过消息的 ``sender_uid``（要扫全表，慢）。"""
    row = conn.execute(
        """SELECT sender_uid FROM messages WHERE sender_uid != ''
           GROUP BY sender_uid ORDER BY COUNT(DISTINCT conv_id) DESC, sender_uid LIMIT 1"""
    ).fetchone()
    return str(row[0]) if row and row[0] else ""


def detect_owner(conn) -> tuple[str, str]:
    """推断本机账号，返回 ``(uid, 昵称)``；认不出来时是 ``("", "")``。

    昵称从 ``users`` 表取；表里还没有这个人（比如自己的资料没采到）时用「我」顶上，
    调用方可以直接把它当显示名。
    """
    uid = _from_participants(conn) or _from_direct_chats(conn) or _from_message_counts(conn)
    name = _nickname(conn, uid) if uid else ""
    return (uid, name or FALLBACK_NAME) if uid else ("", "")
