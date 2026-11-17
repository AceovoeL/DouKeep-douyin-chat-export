"""采集端保存火花天数/图标、记录第一次调用消息 API 的时间。

这里不启浏览器：用假 page 返回 DOM 读取的结果，锁住「读什么、落什么、失败怎么办」。
"""
import asyncio

import pytest

from common.db import connect, update_conversation_spark
from extractor.web_scraper import WebChatScraper
from tests.conftest import insert_conversation

FLAME_URL = (
    "https://lf3-static.bytednsdoc.com/obj/eden-cn/tp_upfbvk/"
    "ljhwZthlaukjlkulzlp/flame_icon/normal/blue_normal.png"
)
GRAY_URL = (
    "https://lf3-static.bytednsdoc.com/obj/eden-cn/tp_upfbvk/"
    "ljhwZthlaukjlkulzlp/flame_icon/normal/gray_normal.png"
)


class FakePage:
    def __init__(self, spark):
        self.spark = spark
        self.scripts = []

    async def evaluate(self, script, *args):
        self.scripts.append(script)
        return self.spark


def _scraper(conn, page):
    s = WebChatScraper()
    s._db_conn = conn
    s.page = page
    return s


def _row(conn):
    return conn.execute(
        "SELECT spark_days, spark_icon, collected_at FROM conversations WHERE conv_id='c1'"
    ).fetchone()


def test_saves_spark_days_and_icon(temp_db, monkeypatch):
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    conn.commit()
    monkeypatch.setattr(
        "extractor.web_scraper.save_spark_icon",
        lambda url: "spark_icons/blue_normal.png",
    )

    page = FakePage({"days": 101, "icon": FLAME_URL})
    asyncio.run(_scraper(conn, page)._extract_and_save_conv_spark("c1"))

    assert tuple(_row(conn)) == (101, "spark_icons/blue_normal.png", None)
    # 读取脚本里必须认这两个抖音的 class，否则真实页面读不到
    assert "commonStreakstreakContainer" in page.scripts[0]
    assert "commonStreaknormalText" in page.scripts[0]


def test_no_spark_clears_old_values(temp_db, monkeypatch):
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    update_conversation_spark(conn, "c1", 50, "spark_icons/old.png")
    conn.commit()

    asyncio.run(_scraper(conn, FakePage({"days": 0, "icon": ""}))
                ._extract_and_save_conv_spark("c1"))

    assert tuple(_row(conn)) == (0, None, None)


def test_missing_active_item_keeps_old_values(temp_db):
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    update_conversation_spark(conn, "c1", 50, "spark_icons/old.png")
    conn.commit()

    asyncio.run(_scraper(conn, FakePage(None))._extract_and_save_conv_spark("c1"))

    assert tuple(_row(conn)) == (50, "spark_icons/old.png", None)


def test_icon_failure_keeps_old_icon(temp_db, monkeypatch):
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    update_conversation_spark(conn, "c1", 50, "spark_icons/old.png")
    conn.commit()
    monkeypatch.setattr("extractor.web_scraper.save_spark_icon", lambda url: None)

    asyncio.run(_scraper(conn, FakePage({"days": 200, "icon": FLAME_URL}))
                ._extract_and_save_conv_spark("c1"))

    assert tuple(_row(conn)) == (200, "spark_icons/old.png", None)


def test_expiring_spark_keeps_note_instead_of_days(temp_db, monkeypatch):
    """「1 天后消失」是倒计时，不是 1 天火花：天数记 0，句子单独留下。"""
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    update_conversation_spark(conn, "c1", 50, "spark_icons/blue_normal.png")
    conn.commit()
    monkeypatch.setattr(
        "extractor.web_scraper.save_spark_icon",
        lambda url: "spark_icons/gray_normal.png",
    )

    page = FakePage({"days": 0, "icon": GRAY_URL, "note": "1 天后消失"})
    asyncio.run(_scraper(conn, page)._extract_and_save_conv_spark("c1"))

    row = conn.execute(
        "SELECT spark_days, spark_icon, spark_note FROM conversations WHERE conv_id='c1'"
    ).fetchone()
    assert tuple(row) == (0, "spark_icons/gray_normal.png", "1 天后消失")


def test_marks_first_api_call_time(temp_db):
    conn = connect(foreign_keys=True)
    insert_conversation(conn, "c1", "会话")
    conn.commit()

    _scraper(conn, FakePage(None))._mark_collected_at("c1", when=1760000000)

    assert _row(conn)[2] == 1760000000
