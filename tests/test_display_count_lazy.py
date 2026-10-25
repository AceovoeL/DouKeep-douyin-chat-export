"""会话列表的「显示条数」不能在请求线程里现算。

用户 2026-10-25 反馈：抓取跑完之后第一次打开聊天查看器，列表显示
「暂无会话数据」，刷新好几遍或者等很久才出数据，之后就一直正常。

根因是 `GET /api/conversations` 会为每个 `display_count IS NULL` 的会话
同步读整个会话（含 raw_data）再跑一遍显示规则 —— 抓取刚结束时每个抓过的
会话都是 NULL，于是这个请求要几十秒；而前端那时没有加载态，把「还没回来」
画成了「暂无会话数据」。

这里锁住修好之后的三条行为：
  1. 大会话的请求**不读消息表**，立刻返回（估计值）；
  2. 后台线程会把准确值算好，下一次请求就用上；
  3. 数据变了（抓取器把 display_count 置 NULL、条数/时间变了）缓存自动失效。
"""
import time

import pytest

from tests.conftest import insert_conversation, insert_message

import backend.database as database


def _seed(conv_id, count, *, hidden_every=0, last_message_time=10):
    """插 count 条消息；hidden_every>0 时每这么多条插一条「画不出来」的空系统消息。"""
    conn = database.get_db()
    insert_conversation(conn, conv_id, "会话", last_message_time=last_message_time)
    for i in range(1, count + 1):
        if hidden_every and i % hidden_every == 0:
            # 空载荷系统消息：msg_type=0 + content="{}" → 界面上不显示。
            insert_message(conn, f"{conv_id}-m{i}", conv_id, i,
                           content="{}", msg_type=0, timestamp=i)
        else:
            insert_message(conn, f"{conv_id}-m{i}", conv_id, i,
                           content=f"msg{i}", timestamp=i)
    conn.execute(
        "UPDATE conversations SET message_count = ?, display_count = NULL "
        "WHERE conv_id = ?",
        (count, conv_id),
    )
    conn.commit()
    conn.close()


def _wait_for_display(conv_id, expected, timeout=10.0):
    """等后台把 display_count 算好（返回拿到的值，超时返回 None）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conv = database.get_conversation(conv_id)
        if conv and conv["display_count"] == expected:
            return conv["display_count"]
        time.sleep(0.02)
    return None


def test_large_conversation_returns_immediately_with_estimate(temp_db, monkeypatch):
    """大会话：请求不等它算完，先给原始条数当估计值。"""
    # 阈值调小，几条消息就能造出「大会话」，免得测试里真插上万条。
    monkeypatch.setattr(database, "SYNC_DISPLAY_COUNT_LIMIT", 5)
    _seed("big", 20, hidden_every=4)

    started = time.monotonic()
    items, _ = database.get_conversations()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "大会话不能在请求里同步算"
    assert items[0]["display_count"] == 20, "算好之前先用原始条数顶上"
    assert database.display_count_pending() >= 1


def test_background_worker_fills_in_the_accurate_count(temp_db, monkeypatch):
    """后台算完之后，下一次请求拿到的是准确值（20 条里有 5 条不显示）。"""
    monkeypatch.setattr(database, "SYNC_DISPLAY_COUNT_LIMIT", 5)
    _seed("big", 20, hidden_every=4)

    items, _ = database.get_conversations()
    assert items[0]["display_count"] == 20, "先给估计值"

    # 后台线程只对「排队时那个库」生效（见 _warm_display_count 的 db_path），
    # 所以这里既不会算错库，也不会把结果漏到下一个用例里。
    assert _wait_for_display("big", 15) == 15
    items, _ = database.get_conversations()
    assert items[0]["display_count"] == 15
    assert database.display_count_pending() == 0


def test_small_conversation_is_accurate_on_the_first_request(temp_db):
    """小会话当场算准：不该让用户先看到一个偏大的数字再跳变。"""
    _seed("small", 10, hidden_every=5)

    items, _ = database.get_conversations()
    assert items[0]["display_count"] == 8
    assert database.display_count_pending() == 0


def test_stored_count_wins_and_is_reused(temp_db):
    """库里已经算过的，直接用，不必再读消息。"""
    _seed("c1", 10)
    conn = database.get_db()
    conn.execute("UPDATE conversations SET display_count = 7 WHERE conv_id = 'c1'")
    conn.commit()
    conn.close()

    items, _ = database.get_conversations()
    assert items[0]["display_count"] == 7


def test_cache_is_dropped_when_data_changes(temp_db):
    """抓取器把 display_count 置 NULL、改了条数之后，旧缓存不能再被用上。"""
    _seed("c1", 10)
    items, _ = database.get_conversations()
    assert items[0]["display_count"] == 10

    # 模拟抓取器刷新统计：条数变了、display_count 回到「待重算」。
    conn = database.get_db()
    insert_message(conn, "c1-new", "c1", 11, content="新的", timestamp=11)
    conn.execute(
        "UPDATE conversations SET message_count = 11, display_count = NULL, "
        "last_message_time = 11 WHERE conv_id = 'c1'"
    )
    conn.commit()
    conn.close()

    items, _ = database.get_conversations()
    assert items[0]["display_count"] == 11


def test_senders_are_cached_and_invalidate_with_the_same_stamp(temp_db):
    """发送者分布跟着同一份缓存走：没变过就不必重读整个会话。"""
    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    insert_message(conn, "m1", "c1", 1, sender_uid="u1", content="a", timestamp=1)
    insert_message(conn, "m2", "c1", 2, sender_uid="u2", content="b", timestamp=2)
    insert_message(conn, "m3", "c1", 3, sender_uid="u2", content="c", timestamp=3)
    conn.commit()
    conn.close()

    first = database.get_senders("c1")
    assert first == [
        {"sender_uid": "u2", "msg_count": 2},
        {"sender_uid": "u1", "msg_count": 1},
    ]
    # 再问一次：结果一样（走缓存）。
    assert database.get_senders("c1") == first

    # 数据变了之后必须重新统计，不能拿旧结果糊弄。
    conn = database.get_db()
    insert_message(conn, "m4", "c1", 4, sender_uid="u1", content="d", timestamp=4)
    conn.execute(
        "UPDATE conversations SET message_count = 4, display_count = NULL, "
        "last_message_time = 4 WHERE conv_id = 'c1'"
    )
    conn.commit()
    conn.close()

    # 两边都是 2 条：并列时谁在前取决于字典顺序（既有行为，不在这里锁定），
    # 这里只关心「确实重新统计过了」。
    counts = {row["sender_uid"]: row["msg_count"]
              for row in database.get_senders("c1")}
    assert counts == {"u1": 2, "u2": 2}


def test_list_endpoint_reports_pending_work(temp_db, monkeypatch):
    """接口要告诉前端「还有会话在后台算」，前端才好把那一栏画成统计中。"""
    from fastapi.testclient import TestClient

    import backend.main as main

    monkeypatch.setattr(database, "SYNC_DISPLAY_COUNT_LIMIT", 5)
    _seed("big", 20)

    with TestClient(main.app) as client:
        payload = client.get("/api/conversations?page_size=200").json()

    assert payload["total"] == 1
    assert payload["items"][0]["conv_id"] == "big"
    assert payload["display_count_pending"] >= 1
