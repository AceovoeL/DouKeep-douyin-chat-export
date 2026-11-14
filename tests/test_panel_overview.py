"""控制面板「概览 → 数据总览」的接口测试。

这里覆盖 backend/overview.py 与 GET /panel/api/overview 的约定：
* 会话 / 消息 / 用户、今日 / 近 7 天 / 近 30 天、有消息天数、发言人数；
* 单聊 / 群聊：conv_type 不可信，按「有几个人发过言」兜底判断；
* 消息构成（含未知类型并入「其他」）与占比；
* 视频按真实载荷粗筛，实况图单独一组；
* 本地保存情况与语音转写；
* 数据库、媒体目录、导出/备份的大小（内部 JSON 不算导出）；
* 「数据统计截止至」：最近一次采集动作的时间，没有记录就不给；
* 缓存与刷新、读库失败时的回落、空库、后台统计状态。
"""
import json
import time

import pytest

from backend import control_panel as cp
from backend import overview
from common import paths
from common.db import connect
from tests.conftest import insert_conversation, insert_message


@pytest.fixture
def overview_env(temp_db, tmp_path, monkeypatch):
    """把 data / media / transcoded 指到临时目录，并清掉进程级缓存。"""
    data_dir = tmp_path / "data"
    media_dir = data_dir / "media"
    for name in ("images", "emoji", "voice", "avatars", "videos", "bubbles", "card_icons"):
        (media_dir / name).mkdir(parents=True, exist_ok=True)
    (data_dir / "transcoded" / "videos").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(paths, "MEDIA_DIR", str(media_dir))
    monkeypatch.setattr(paths, "TRANSCODE_DIR", str(data_dir / "transcoded"))
    overview.reset_cache()
    return data_dir, media_dir


def _write(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _client():
    from fastapi.testclient import TestClient

    import backend.main as main

    return TestClient(main.app)


def _seed(monkeypatch):
    """造 2 个会话、4 个用户资料、3 个发送者、10 条消息和 3 条转写记录。

    时间点：今天 / 7 天窗口里 / 30 天窗口里 / 很早以前 / 没有时间戳。
    c2 的 conv_type=2，用来覆盖「明确标记成群聊」的情况；
    群聊的「按发言人数判断」另有用例。
    """
    now = int(time.time())
    week = overview._local_day_start(6) + 3600
    month = overview._local_day_start(29) + 3600
    old = overview._local_day_start(60) + 3600

    conn = connect(foreign_keys=True)
    try:
        insert_conversation(conn, "0:1:111:222", "单聊", conv_type=1)
        insert_conversation(conn, "7026615456521781774", "群聊", conv_type=2)
        conn.executemany(
            "INSERT INTO users (uid, nickname) VALUES (?, ?)",
            [("u1", "甲"), ("u2", "乙"), ("u3", "丙"), ("u4", "丁")],
        )
        rows = [
            # msg_id, conv_id, seq, msg_type, media, live, timestamp, sender, raw
            ("m1", "0:1:111:222", 1, 1, None, None, now, "u1", None),
            ("m2", "0:1:111:222", 2, 1, None, None, week, "u2", None),
            ("m3", "0:1:111:222", 3, 3, "media/images/a.jpg", None, month, "u1", None),
            ("m4", "7026615456521781774", 4, 3, None, None, old, "u3", None),
            ("m5", "7026615456521781774", 5, 2, "media/emoji/a.gif", None, now, "u2", None),
            ("m6", "0:1:111:222", 6, 5, "media/videos/a.mp4", None, now, "u1",
             json.dumps({"content_json": json.dumps({"video": {"tkey": "tok2", "skey": "0022"}})})),
            ("m7", "7026615456521781774", 7, 4, None, None, 0, "u3", None),
            ("m8", "0:1:111:222", 8, 0, None, None, week, "u1", None),
            ("m9", "7026615456521781774", 9, 1, None, None, 0, "u2", None),
            (
                "m10", "0:1:111:222", 10, 0, "media/images/live.jpg",
                "media/videos/live.mp4", now, "u2",
                json.dumps({"content_json": json.dumps({
                    "live_photo_video": {"tkey": "tok", "skey": "0011"}})})
            ),
        ]
        for msg_id, conv_id, seq, msg_type, media, live, ts, sender, raw in rows:
            insert_message(
                conn, msg_id, conv_id, seq, sender_uid=sender, sender_name=sender,
                content="内容-" + msg_id, msg_type=msg_type, media_local_path=media,
                live_video_path=live, timestamp=ts, raw_data=raw,
            )
        conn.executemany(
            "INSERT INTO voice_transcriptions (msg_id, message_id, text_result, status, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                ("m8", "m8", "识别结果", "success", now),
                ("m7", "m7", "", "failed", now),
                ("m3", "m3", "", "pending", now),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setitem(cp._scheduler_state, "enabled", True)
    monkeypatch.setitem(cp._scheduler_state, "next_run", 1700000000)


def test_overview_counts_types_media_and_storage(overview_env, monkeypatch):
    data_dir, media_dir = overview_env
    _seed(monkeypatch)
    _write(media_dir / "images" / "a.jpg", 100)
    _write(media_dir / "emoji" / "a.gif", 200)
    _write(media_dir / "voice" / "a.m4a", 10)
    _write(media_dir / "avatars" / "a.png", 50)
    _write(media_dir / "videos" / "a.mp4", 300)
    _write(media_dir / "bubbles" / "b.png", 20)
    _write(media_dir / "card_icons" / "c.png", 5)
    _write(data_dir / "transcoded" / "videos" / "d.mp4", 7)
    _write(data_dir / "export.zip", 1000)
    _write(data_dir / "chat_database_before_import_test.db", 300)
    # 内部状态文件不是「导出」，不该算进导出/备份
    _write(data_dir / "conversations_list.json", 500)
    _write(data_dir / "backfill_daily_share_undo.json", 400)

    # refresh=1 同步重算：测试里不依赖后台线程的时序
    body = _client().get("/panel/api/overview?refresh=1").json()

    assert body["available"] is True
    assert body["stale"] is False
    assert body["totals"] == {
        "conversations": 2,
        "messages": 10,
        "users": 4,
        "singles": 1,
        "groups": 1,
        "avg_messages": 5.0,
    }
    activity = body["activity"]
    assert activity["today"] == 4
    assert activity["last7"] == 6
    assert activity["last30"] == 7
    assert activity["active_days"] == 4
    assert activity["senders"] == 3
    assert activity["first_message_time"] < activity["last_message_time"]

    types = {item["key"]: item for item in body["types"]}
    assert [item["key"] for item in body["types"]] == [
        "text", "image", "emoji", "video", "share", "other",
    ]
    assert types["text"]["count"] == 3
    assert types["image"]["count"] == 2
    assert types["emoji"]["count"] == 1
    assert types["video"]["count"] == 1
    assert types["share"]["count"] == 1
    assert types["other"]["count"] == 2
    assert types["image"]["saved"] == 1
    assert types["emoji"]["saved"] == 1
    assert types["text"]["percent"] == 30.0
    assert types["other"]["percent"] == 20.0
    assert abs(sum(item["percent"] for item in body["types"]) - 100.0) <= 0.2

    media = body["media"]
    assert media["images"] == {"total": 2, "saved": 1}
    assert media["emoji"] == {"total": 1, "saved": 1}
    # 视频不按 msg_type=5 数：m6 是普通视频，m10 是实况图，分开算
    assert media["videos"] == {"total": 1, "saved": 1}
    assert media["live_photo"] == {"total": 1, "saved": 1}
    assert body["voice"] == {"success": 1, "failed": 1, "pending": 1, "total": 3}

    storage = body["storage"]
    detail = {item["key"]: item for item in storage["detail"]}
    assert detail["images"]["bytes"] == 100
    assert detail["emoji"]["bytes"] == 200
    assert detail["voice"]["bytes"] == 10
    assert detail["avatars"]["bytes"] == 50
    assert detail["videos"]["bytes"] == 300
    assert detail["other"]["bytes"] == 32  # 气泡 20 + 卡片封面 5 + 转码缓存 7
    assert storage["media_bytes"] == 692
    assert storage["media_files"] == 8
    # 只算 export.zip + 导入前的 .db 备份；两个内部 JSON 不算
    assert storage["exports_bytes"] == 1300
    assert storage["db_bytes"] > 0
    assert storage["total_bytes"] == storage["db_bytes"] + 692 + 1300

    assert body["schedule"] == {"enabled": True, "next_run": 1700000000}


def test_group_chat_id_is_recognised(overview_env):
    """群聊靠纯数字会话 ID 认出来，不靠发言人数。"""
    conn = connect(foreign_keys=True)
    try:
        insert_conversation(conn, "7026615456521781774", "群聊", conv_type=1)
        insert_message(conn, "gm1", "7026615456521781774", 1, sender_uid="u1",
                       msg_type=1, timestamp=1001)
        insert_message(conn, "gm2", "7026615456521781774", 2, sender_uid="u2",
                       msg_type=1, timestamp=1002)
        conn.commit()
    finally:
        conn.close()

    data = overview.collect(force=True)
    assert data["totals"]["conversations"] == 1
    assert data["totals"]["groups"] == 1
    assert data["totals"]["singles"] == 0


def test_group_chat_with_firebot_stays_a_group(overview_env):
    """群聊里开了小火人 AI，也还是群聊；AI 只是不算发言人数。"""
    conn = connect(foreign_keys=True)
    try:
        insert_conversation(conn, "7678635735741645370", "群聊", conv_type=1)
        insert_message(conn, "gm1", "7678635735741645370", 1, sender_uid="u1",
                       msg_type=1, timestamp=1001)
        insert_message(conn, "gm2", "7678635735741645370", 2, sender_uid="u2",
                       msg_type=1, timestamp=1002)
        insert_message(conn, "gm3", "7678635735741645370", 3, sender_uid="u_bot",
                       msg_type=0, timestamp=1003, raw_data='{"awe_type": 716}')
        conn.commit()
    finally:
        conn.close()

    data = overview.collect(force=True)
    assert data["totals"]["groups"] == 1
    assert data["totals"]["singles"] == 0
    assert data["activity"]["senders"] == 2


def test_firebot_ai_is_not_counted_as_a_person(overview_env):
    """单聊里的小火人 AI 不算发言人数，也不能把会话顶成群聊。"""
    conn = connect(foreign_keys=True)
    try:
        insert_conversation(conn, "0:1:111:222", "单聊", conv_type=1)
        insert_message(conn, "m1", "0:1:111:222", 1, sender_uid="u1", msg_type=1, timestamp=100)
        insert_message(conn, "m2", "0:1:111:222", 2, sender_uid="u2", msg_type=1, timestamp=101)
        # 小火人的消息：一条带 716 标记，一条不带 —— 排除要按账号，不是按单条消息
        insert_message(conn, "m3", "0:1:111:222", 3, sender_uid="u_bot", msg_type=0,
                       timestamp=102, raw_data='{"awe_type": 716}')
        insert_message(conn, "m4", "0:1:111:222", 4, sender_uid="u_bot", msg_type=0, timestamp=103)
        conn.commit()
    finally:
        conn.close()

    data = overview.collect(force=True)
    assert data["activity"]["senders"] == 2
    assert data["totals"]["groups"] == 0
    assert data["totals"]["singles"] == 1


def test_overview_cache_and_forced_refresh(overview_env, monkeypatch):
    _seed(monkeypatch)
    client = _client()
    overview.collect(force=True)   # 先把缓存算好，后面默认请求都走缓存

    first = client.get("/panel/api/overview").json()
    conn = connect(foreign_keys=True)
    try:
        insert_message(conn, "m11", "0:1:111:222", 11, sender_uid="u1", content="新消息",
                       msg_type=1, timestamp=int(time.time()))
        conn.commit()
    finally:
        conn.close()

    cached = client.get("/panel/api/overview").json()
    assert cached["totals"]["messages"] == first["totals"]["messages"]
    assert cached["activity"]["today"] == first["activity"]["today"]

    fresh = client.get("/panel/api/overview?refresh=1").json()
    assert fresh["totals"]["messages"] == first["totals"]["messages"] + 1
    assert fresh["activity"]["today"] == first["activity"]["today"] + 1


def test_overview_falls_back_to_cache_when_db_read_fails(overview_env, monkeypatch):
    _seed(monkeypatch)
    client = _client()
    overview.collect(force=True)
    first = client.get("/panel/api/overview").json()

    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(overview, "_read_stats", boom)
    stale = client.get("/panel/api/overview?refresh=1").json()
    assert stale["available"] is True
    assert stale["stale"] is True
    assert stale["totals"] == first["totals"]

    overview.reset_cache()
    empty = client.get("/panel/api/overview?refresh=1").json()
    assert empty["available"] is False
    assert empty["totals"]["messages"] == 0
    assert empty["types"][0]["count"] == 0


def test_overview_empty_database(overview_env):
    body = _client().get("/panel/api/overview?refresh=1").json()
    assert body["available"] is True
    assert body["totals"]["messages"] == 0
    assert body["totals"]["avg_messages"] == 0.0
    assert body["activity"]["first_message_time"] is None
    assert body["activity"]["last_message_time"] is None
    assert [item["count"] for item in body["types"]] == [0, 0, 0, 0, 0, 0]
    assert body["voice"]["total"] == 0
    assert body["storage"]["media_bytes"] == 0
    assert body["storage"]["exports_bytes"] == 0


def test_overview_first_request_says_computing(overview_env, monkeypatch):
    """从没算过时立刻返回 computing，不让页面等后台扫库。"""
    monkeypatch.setattr(overview, "_start_refresh", lambda: None)
    overview.reset_cache()
    body = _client().get("/panel/api/overview").json()
    assert body["available"] is False
    assert body["computing"] is True
    assert body["totals"]["messages"] == 0


def test_overview_reports_latest_collection_time(overview_env):
    """概览最上方的「数据统计截止至」取最近一次采集动作的时间（各会话取最大值）。

    跟聊天查看器那一行同一个字段：采集时记下的 collected_at，不是最后一条消息的时间。
    """
    conn = connect(foreign_keys=True)
    try:
        insert_conversation(conn, "0:1:111:222", "单聊")
        insert_conversation(conn, "7026615456521781774", "群聊")
        conn.execute(
            "UPDATE conversations SET collected_at = ? WHERE conv_id = ?",
            (1763100000, "0:1:111:222"),
        )
        conn.execute(
            "UPDATE conversations SET collected_at = ? WHERE conv_id = ?",
            (1763200000, "7026615456521781774"),
        )
        conn.commit()
    finally:
        conn.close()

    body = _client().get("/panel/api/overview?refresh=1").json()
    assert body["collected_at"] == 1763200000


def test_overview_hides_cutoff_without_collection_record(overview_env):
    """老库 / 还没采集过：没有采集记录就不显示，绝不拿最后一条消息的时间顶替。"""
    conn = connect(foreign_keys=True)
    try:
        insert_conversation(conn, "0:1:111:222", "单聊")
        insert_message(conn, "m1", "0:1:111:222", 1, sender_uid="u1",
                       msg_type=1, timestamp=1763100000)
        conn.commit()
    finally:
        conn.close()

    body = _client().get("/panel/api/overview?refresh=1").json()
    assert body["collected_at"] is None
    assert body["activity"]["last_message_time"] == 1763100000


def test_overview_route_has_a_readable_log_label():
    from backend.access_log import route_label

    assert route_label("/panel/api/overview") == "控制面板：数据总览"
