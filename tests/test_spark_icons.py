"""会话火花：图标按官方文件名落地 + 数据库字段迁移。

用户 2026-11-14 的要求：会话列表和聊天标题显示抖音同款火花图标与天数；
采集时本地没有这张图就自动下载，文件名要用服务器返回的那个（如 blue_normal.png）。
这里锁住三件事：
1. 只允许白名单图床（这个地址会变成服务端请求）；
2. 文件名保留服务器原名，不安全的名字才回退哈希；
3. 旧数据库启动时能自动补上 spark_days / spark_icon / collected_at 三列。
"""
import sqlite3

import pytest

from common import paths, spark_icons

FLAME_URL = (
    "https://lf3-static.bytednsdoc.com/obj/eden-cn/tp_upfbvk/"
    "ljhwZthlaukjlkulzlp/flame_icon/normal/blue_normal.png"
)


@pytest.fixture()
def icon_dir(tmp_path, monkeypatch):
    """把 media 目录指到 tmp，别把测试图写进真实的 data/media。"""
    monkeypatch.setattr(spark_icons, "MEDIA_DIR", str(tmp_path))
    return tmp_path / spark_icons.SPARK_ICON_DIR_NAME


def test_keeps_server_filename():
    assert spark_icons.spark_icon_filename(FLAME_URL) == "blue_normal.png"


def test_only_allowed_image_hosts():
    assert spark_icons.is_allowed_icon_url(FLAME_URL) is True
    assert spark_icons.is_allowed_icon_url("https://p3.douyinpic.com/x.png") is True
    for bad in (
        "http://127.0.0.1:8000/api/stats",
        "http://localhost/x.png",
        "http://evil.example.com/x.png",
        "https://bytednsdoc.com.evil.com/x.png",
        "file:///etc/passwd",
        "",
        None,
    ):
        assert spark_icons.is_allowed_icon_url(bad) is False


def test_unsafe_filename_falls_back_to_hash():
    weird = "https://lf3-static.bytednsdoc.com/flame/..%2Fevil.png"
    name = spark_icons.spark_icon_filename(weird)
    assert name.startswith("spark_")
    assert name.endswith(".png")
    assert "/" not in name and ".." not in name
    # 同样一个链接每次算出来都一样
    assert spark_icons.spark_icon_filename(weird) == name


def test_save_writes_once_and_reuses(icon_dir, monkeypatch):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return b"\x89PNG" + b"x" * 200

    monkeypatch.setattr(spark_icons, "_fetch_icon", fake_fetch)
    rel = spark_icons.save_spark_icon(FLAME_URL)
    assert rel == "spark_icons/blue_normal.png"
    assert (icon_dir / "blue_normal.png").read_bytes().startswith(b"\x89PNG")
    assert spark_icons.find_spark_icon(FLAME_URL) == rel
    # 再遇到同一个图标直接用存好的那份，不再下载
    assert spark_icons.save_spark_icon(FLAME_URL) == rel
    assert calls == [FLAME_URL]


def test_failed_fetch_returns_none(icon_dir, monkeypatch):
    monkeypatch.setattr(spark_icons, "_fetch_icon", lambda url: None)
    assert spark_icons.save_spark_icon(FLAME_URL) is None
    assert spark_icons.find_spark_icon(FLAME_URL) is None


def test_init_db_adds_columns_to_old_database(tmp_path, monkeypatch):
    from common.db import init_db

    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE conversations (
               conv_id TEXT PRIMARY KEY,
               conv_type INTEGER DEFAULT 1,
               name TEXT,
               participant_uids TEXT DEFAULT '[]',
               last_message_time INTEGER DEFAULT 0,
               message_count INTEGER DEFAULT 0
           )"""
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(paths, "DB_PATH", str(db_path))
    init_db()
    init_db()  # 幂等：第二遍不能再报错

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
    conn.close()
    assert {"spark_days", "spark_icon", "collected_at"} <= columns


def test_db_helpers_write_and_keep_icon(temp_db):
    from common.db import (
        clear_conversation_spark,
        connect,
        mark_conversation_collected,
        update_conversation_spark,
    )
    from tests.conftest import insert_conversation

    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    update_conversation_spark(conn, "c1", 101, "spark_icons/blue_normal.png")
    # icon 传 None 时保留上一次的图，只更新天数
    update_conversation_spark(conn, "c1", 102, None)
    row = conn.execute(
        "SELECT spark_days, spark_icon FROM conversations WHERE conv_id='c1'"
    ).fetchone()
    assert tuple(row) == (102, "spark_icons/blue_normal.png")

    clear_conversation_spark(conn, "c1")
    row = conn.execute(
        "SELECT spark_days, spark_icon FROM conversations WHERE conv_id='c1'"
    ).fetchone()
    assert tuple(row) == (0, None)

    mark_conversation_collected(conn, "c1", when=1760000000)
    value = conn.execute(
        "SELECT collected_at FROM conversations WHERE conv_id='c1'"
    ).fetchone()[0]
    assert value == 1760000000
    conn.commit()
    conn.close()


def test_get_conversations_includes_spark_fields(temp_db):
    import backend.database as database
    from common.db import connect, update_conversation_spark
    from tests.conftest import insert_conversation

    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    update_conversation_spark(conn, "c1", 101, "spark_icons/blue_normal.png")
    conn.commit()
    conn.close()

    items, _ = database.get_conversations()
    assert items[0]["spark_days"] == 101
    assert items[0]["spark_icon"] == "spark_icons/blue_normal.png"


def test_reader_exposes_collected_at(temp_db):
    """数据洞察的「数据统计截至」要能从接口拿到，不能只剩最后消息时间。

    列表页点进来的会话对象直接带 collected_at；搜索结果只带 id 时前端会补
    一次 /api/conversations/{id}，所以两个读取口都得带上这个字段。
    """
    import backend.database as database
    from common.db import connect, mark_conversation_collected
    from tests.conftest import insert_conversation

    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话", last_message_time=1763000000)
    mark_conversation_collected(conn, "c1", when=1763100000)
    conn.commit()
    conn.close()

    items, _ = database.get_conversations()
    assert items[0]["collected_at"] == 1763100000

    detail = database.get_conversation("c1")
    assert detail["collected_at"] == 1763100000
    # 采集时间与最后消息时间互不顶替
    assert detail["last_message_time"] == 1763000000
