"""记录「补不下来的图 / 视频」，下次别再白试一遍。

为什么要有它：抖音给的下载地址带签名、会过期。链接一旦过期（403 / 404），再试多少次
都是白试。以前每轮采集收尾都会把全库「缺图」重新扫一遍，于是每次都要为几千条早就过期
的消息各发一次请求，采集结束前白白多等好几分钟。

这里把失败的条目记在库里（表 media_retry）：

* 明确「地址没了」的失败（403 / 404 / 410）—— 记成永久失败，同一个地址以后不再试；
* 其它失败（断网、超时、CDN 重置）—— 记次数并冷却：第 1 次 3 天、第 2 次 7 天、
  第 3 次起 30 天以后再给一次机会，网络真恢复了还能补回来。

地址换了（重新抓到消息、载荷里换成新签名的链接）或手动点了「重新补」，记录就作废，
照常再试。

连接得由调用方给（sqlite 连接不能跨线程），这里所有函数都**不自己开连接**；
note_failure / note_success 不提交事务，交给调用方跟补图那批写入一起 commit。
"""
from __future__ import annotations

import sqlite3
import time
import urllib.error

#: 冷却天数，按累计失败次数取；次数再多也按最后一档来。
_COOLDOWN_DAYS = (3, 7, 30)

#: 这几个状态码表示地址 / 文件已经彻底没了，同一个地址不用再试。
_PERMANENT_STATUS = frozenset({403, 404, 410, 451})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_retry (
    msg_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    signature TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_at INTEGER NOT NULL DEFAULT 0,
    permanent INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    PRIMARY KEY (msg_id, kind)
)
"""


def ensure_schema(conn) -> None:
    """老库上也能用：表不存在就建一张（DDL，和调用方的事务一起提交）。"""
    try:
        conn.execute(_SCHEMA)
    except sqlite3.Error:
        pass


def _http_status(exc):
    """异常里能看出来的 HTTP 状态码（urllib 把它挂在 HTTPError.code 上）。"""
    current = exc
    for _ in range(3):
        code = getattr(current, "code", None)
        if isinstance(code, int):
            return code
        inner = getattr(current, "reason", None)
        if inner is None or inner is current:
            break
        current = inner
    return None


def classify(exc) -> tuple[bool, str]:
    """一次下载失败是不是「彻底没救了」。返回 (永久?, 给人看的一句原因)。"""
    if exc is None:
        return False, "下载失败"
    status = _http_status(exc)
    if status in _PERMANENT_STATUS:
        return True, f"链接已失效（HTTP {status}）"
    if isinstance(exc, urllib.error.HTTPError):
        return False, (f"下载失败（HTTP {status}）" if status else "下载失败")
    return False, f"下载失败（{type(exc).__name__}）"


def should_skip(conn, msg_id, kind, signature: str = "") -> str | None:
    """这条现在还要不要试：返回原因字符串表示跳过，None 表示可以试。

    signature 是「这次会拿哪个地址去试」；和记下来的不一样，说明地址换过了，
    那就重新试一次。
    """
    if conn is None or not msg_id:
        return None
    try:
        row = conn.execute(
            "SELECT signature, attempts, last_at, permanent, reason "
            "FROM media_retry WHERE msg_id=? AND kind=?",
            (str(msg_id), str(kind)),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    saved_signature = str(row[0] or "")
    if signature and saved_signature and signature != saved_signature:
        return None
    if row[3]:
        return str(row[4] or "链接已失效")
    attempts = int(row[1] or 0)
    days = _COOLDOWN_DAYS[min(max(attempts, 1), len(_COOLDOWN_DAYS)) - 1]
    last_at = float(row[2] or 0)
    if time.time() - last_at < days * 86400:
        return f"上次没补下来，{days} 天内的采集不再试"
    return None


def note_failure(conn, msg_id, kind, *, signature: str = "", exc=None,
                 permanent: bool = False, reason: str = "") -> None:
    """记一次失败（不提交事务）。exc 能给的话，永久 / 临时会自动判断。"""
    if conn is None or not msg_id:
        return
    ensure_schema(conn)
    why = reason
    if exc is not None:
        auto_permanent, auto_reason = classify(exc)
        permanent = permanent or auto_permanent
        why = why or auto_reason
    why = why or "下载失败"
    try:
        row = conn.execute(
            "SELECT attempts, signature FROM media_retry WHERE msg_id=? AND kind=?",
            (str(msg_id), str(kind)),
        ).fetchone()
        attempts = 1
        if row is not None:
            same_link = not (signature and row[1] and signature != row[1])
            attempts = (int(row[0] or 0) + 1) if same_link else 1
        conn.execute(
            "INSERT INTO media_retry "
            "(msg_id, kind, signature, attempts, last_at, permanent, reason) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(msg_id, kind) DO UPDATE SET "
            "signature=excluded.signature, attempts=excluded.attempts, "
            "last_at=excluded.last_at, "
            "permanent=MAX(media_retry.permanent, excluded.permanent), "
            "reason=excluded.reason",
            (
                str(msg_id), str(kind), signature or "", attempts,
                int(time.time()), 1 if permanent else 0, why,
            ),
        )
    except sqlite3.Error:
        pass


def note_success(conn, msg_id, kind) -> None:
    """补成功了：把以前的失败记录删掉（不提交事务）。"""
    if conn is None or not msg_id:
        return
    try:
        conn.execute(
            "DELETE FROM media_retry WHERE msg_id=? AND kind=?",
            (str(msg_id), str(kind)),
        )
    except sqlite3.Error:
        pass


def clear(conn, kinds=None) -> int:
    """忘掉失败记录（手动「重新补」时用；kinds 为空表示全部），返回删了几条。"""
    ensure_schema(conn)
    try:
        if kinds:
            marks = ",".join("?" for _ in kinds)
            cursor = conn.execute(
                f"DELETE FROM media_retry WHERE kind IN ({marks})", tuple(kinds)
            )
        else:
            cursor = conn.execute("DELETE FROM media_retry")
        conn.commit()
    except sqlite3.Error:
        return 0
    return cursor.rowcount or 0


def count(conn, kind: str | None = None) -> int:
    """记了多少条「补不下来」的（日志 / 面板用）。"""
    try:
        if kind:
            row = conn.execute(
                "SELECT COUNT(*) FROM media_retry WHERE kind=?", (kind,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM media_retry").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] or 0) if row else 0
