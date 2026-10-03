"""ChatLab 远程数据源协议（Pull 模式）端点测试。

Covers discovery, the since/limit paging chain (including the lookback
offset baked into nextSince), the empty-increment response, and API-token auth.
"""
from fastapi.testclient import TestClient

import backend.main as main
from backend.chatlab_pull import PULL_LOOKBACK_SECONDS
from tests.conftest import insert_conversation, insert_message


CONV = "0:1:111:222"
T0 = 1_700_000_000


def _seed(temp_db, n=5, conv_type=1, monkeypatch=None):
    import extractor.models as models
    if monkeypatch is not None:
        # No panel password → endpoints are open; the auth test sets its own.
        monkeypatch.setattr(main, "_get_password_hash", lambda: None)
    conn = models.get_db()
    insert_conversation(conn, CONV, "示例会话", participant_uids='["222","111"]',
                        last_message_time=T0 + n, conv_type=conv_type)
    insert_conversation(conn, "empty", "空会话")
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('222','我')")
    conn.execute("INSERT INTO users (uid, nickname) VALUES ('111','示例会话')")
    for i in range(1, n + 1):
        insert_message(conn, f"srv_{i}", CONV, i, sender_uid="222" if i % 2 else "111",
                       content=f"消息{i}", msg_type=1, timestamp=T0 + i)
    conn.commit()
    conn.close()
    return TestClient(main.app)


def test_sessions_discovery_lists_only_conversations_with_messages(temp_db, monkeypatch):
    client = _seed(temp_db, monkeypatch=monkeypatch)
    r = client.get("/api/v1/sessions?format=chatlab")
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert [s["id"] for s in sessions] == [CONV]
    s = sessions[0]
    assert s["name"] == "与示例会话的对话" and s["platform"] == "douyin" and s["type"] == "private"
    assert s["messageCount"] == 5 and s["memberCount"] == 2 and s["lastMessageAt"] == T0 + 5

    assert client.get("/api/v1/sessions?keyword=不存在").json()["sessions"] == []
    assert len(client.get("/api/v1/sessions?keyword=示例会话").json()["sessions"]) == 1


def test_full_pull_pages_through_nextsince_chain(temp_db, monkeypatch):
    client = _seed(temp_db, n=5, monkeypatch=monkeypatch)
    seen = []
    since, pages = 0, 0
    while True:
        r = client.get(f"/api/v1/sessions/{CONV}/messages",
                       params={"format": "chatlab", "since": since, "limit": 2})
        assert r.status_code == 200
        body = r.json()
        pages += 1
        assert body["chatlab"]["version"] == "0.0.2"
        assert body["meta"] == {"name": "与示例会话的对话", "platform": "douyin",
                                "type": "private", "ownerId": "222"}
        assert {m["platformId"]: m["accountName"] for m in body["members"]} == {
            "222": "我", "111": "示例会话"}
        for m in body["messages"]:
            assert m["sender"] in ("222", "111") and m["timestamp"] > 0
            assert "replyTo" not in m  # exporter-only extra must not leak
        seen.extend(m["platformMessageId"] for m in body["messages"])
        sync = body["sync"]
        # nextSince carries the lookback offset so the next window starts right
        # after this page's last timestamp (no ties in this fixture)
        assert sync["nextSince"] == body["messages"][-1]["timestamp"] + 1 + PULL_LOOKBACK_SECONDS
        if not sync["hasMore"]:
            break
        since = sync["nextSince"]
        assert pages < 10
    assert seen == [f"srv_{i}" for i in range(1, 6)]
    assert pages == 3


def test_page_cut_inside_same_second_completes_the_group(temp_db, monkeypatch):
    """Timestamp paging cannot split a second, so the page grows to the group end."""
    import extractor.models as models
    client = _seed(temp_db, n=1, monkeypatch=monkeypatch)
    conn = models.get_db()
    for i in (2, 3, 4):  # three messages in the same second
        insert_message(conn, f"srv_{i}", CONV, i, sender_uid="111", content=str(i),
                       msg_type=1, timestamp=T0 + 2)
    insert_message(conn, "srv_5", CONV, 5, sender_uid="222", content="5",
                   msg_type=1, timestamp=T0 + 3)
    conn.commit()
    conn.close()
    page1 = client.get(f"/api/v1/sessions/{CONV}/messages",
                       params={"since": 0, "limit": 2}).json()
    assert [m["platformMessageId"] for m in page1["messages"]] == ["srv_1", "srv_2", "srv_3", "srv_4"]
    assert page1["sync"] == {"hasMore": True, "nextSince": T0 + 2 + 1 + PULL_LOOKBACK_SECONDS}
    page2 = client.get(f"/api/v1/sessions/{CONV}/messages",
                       params={"since": page1["sync"]["nextSince"], "limit": 2}).json()
    assert [m["platformMessageId"] for m in page2["messages"]] == ["srv_5"]
    assert page2["sync"]["hasMore"] is False


def test_incremental_pull_after_cursor_reset_looks_back(temp_db, monkeypatch):
    """A wall-clock ``since`` (ChatLab's error path) must still return recent messages."""
    client = _seed(temp_db, n=5, monkeypatch=monkeypatch)
    wall_clock = T0 + 5 + 3600  # an hour after the newest message
    body = client.get(f"/api/v1/sessions/{CONV}/messages",
                      params={"since": wall_clock, "limit": 100}).json()
    assert [m["platformMessageId"] for m in body["messages"]] == [f"srv_{i}" for i in range(1, 6)]
    assert body["sync"] == {"hasMore": False, "nextSince": T0 + 5 + 1 + PULL_LOOKBACK_SECONDS}


def test_empty_increment_is_small_and_keeps_cursor(temp_db, monkeypatch):
    client = _seed(temp_db, n=3, monkeypatch=monkeypatch)
    since = T0 + 3 + PULL_LOOKBACK_SECONDS + 1  # window starts after the newest message
    r = client.get(f"/api/v1/sessions/{CONV}/messages", params={"since": since})
    body = r.json()
    assert body["messages"] == [] and "members" not in body
    assert body["sync"] == {"hasMore": False, "nextSince": since}
    assert len(r.content) < 1024


def test_images_are_labels_not_data_urls(temp_db, monkeypatch):
    import extractor.models as models
    client = _seed(temp_db, n=1, monkeypatch=monkeypatch)
    conn = models.get_db()
    insert_message(conn, "srv_img", CONV, 2, sender_uid="111", msg_type=3,
                   media_url="https://cdn/pic.jpg", timestamp=T0 + 2)
    conn.commit()
    conn.close()
    body = client.get(f"/api/v1/sessions/{CONV}/messages", params={"since": 0}).json()
    img = next(m for m in body["messages"] if m["platformMessageId"] == "srv_img")
    assert img == {"sender": "111", "accountName": "示例会话", "timestamp": T0 + 2,
                   "type": 1, "content": "[图片]", "platformMessageId": "srv_img"}


def test_unknown_conversation_and_bad_format(temp_db, monkeypatch):
    client = _seed(temp_db, monkeypatch=monkeypatch)
    assert client.get("/api/v1/sessions/nope/messages").status_code == 404
    assert client.get(f"/api/v1/sessions/{CONV}/messages?format=csv").status_code == 400


def test_api_token_authorizes_pull_endpoints(temp_db, monkeypatch):
    from common import config
    client = _seed(temp_db)
    monkeypatch.setattr(main, "_get_password_hash", lambda: "hash")
    monkeypatch.setattr(config, "get_api_token", lambda: "tok123")
    assert client.get("/api/v1/sessions").status_code == 401
    assert client.get("/api/v1/sessions",
                      headers={"Authorization": "Bearer tok123"}).status_code == 200
    assert client.get(f"/api/v1/sessions/{CONV}/messages",
                      headers={"Authorization": "Bearer tok123"}).status_code == 200
