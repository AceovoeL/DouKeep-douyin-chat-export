"""没有标题的内容分享（视频/图片/动图/文章/评论）不再落库、也不再显示 "[分享]"。

用户报告的样本消息（已脱敏）：`aweType=800` 的分享视频，卡片带着封面和作者昵称（`content_name`），
但 `content_title` 是空的；老抓取端在这种情况下把正文写成了笼统的 "[分享]"，阅读界面
的分享卡标题行就显示 "[分享]"。现在这类没有标题的内容分享统一留空。
"""
import json

import pytest

from extractor.web_scraper import WebChatScraper, _classify_json_message

# 用户报告那条消息的 content_json（截取关键字段：没有 content_title；作者名与作品 id 已脱敏）。
REAL_VIDEO_SHARE = {
    "aweType": 800,
    "awemeType": 0,
    "content_title": "",
    "content_name": "示例作者",
    "cover_url": {"url_list": ["https://example.com/cover.jpeg"]},
    "itemId": "1000000000000000041",
}


def test_titleless_video_share_stores_empty_text():
    assert _classify_json_message(REAL_VIDEO_SHARE) == ("", "share", None)


@pytest.mark.parametrize("cj", [
    {"aweType": 801, "itemId": "42"},                        # 分享视频（另一号段）
    {"aweType": 803, "itemId": "42"},                        # 分享视频（另一号段）
    {"aweType": 11054, "itemId": "42"},                      # 分享视频
    {"aweType": 10500, "itemId": "42"},                      # 分享评论（没有 comment 字段）
])
def test_other_titleless_content_shares_also_store_empty_text(cj):
    text, msg_type, _ = _classify_json_message(cj)
    assert (text, msg_type) == ("", "share")


def test_shares_with_a_title_keep_their_text():
    # push_detail / comment 里有内容时照旧保留，只有"什么都没有"才留空。
    assert _classify_json_message({"aweType": 800, "push_detail": "视频标题"})[0] == "视频标题"
    assert _classify_json_message({"aweType": 11054, "push_detail": "视频标题"})[0] == "视频标题"
    assert _classify_json_message({"aweType": 10500, "comment": "评论内容"})[0] == "评论内容"
    # 限时日常（805）的 [分享限时日常] 标注不受影响。
    assert _classify_json_message({"aweType": 805, "itemId": "9"})[0] == "[分享限时日常]"


def test_empty_content_share_row_is_still_stored(temp_db):
    """空正文的分享卡不能像空载荷一样被跳过，否则这条消息会整条消失。"""
    from backend import database
    from tests.conftest import insert_conversation

    conn = database.get_db()
    insert_conversation(conn, "c1", "会话")
    scraper = object.__new__(WebChatScraper)
    scraper._db_conn = conn
    # 和 _convert_* 产出的那批字段一致（api 抓取 → converted_message）。
    msg = {
        "server_id": "1000000000000000042",
        "content": "",
        "msg_type": "share",
        "sender_uid": "u1",
        "sender_name": "",
        "is_self": False,
        "created_at": "2026-09-24T15:33:33Z",
        "order_high": 389409,
        "order_low": 3554691936,
        "content_json": json.dumps(REAL_VIDEO_SHARE, ensure_ascii=False),
    }
    inserted, ids, updated = scraper._store_messages([msg], "c1")
    assert inserted == 1
    assert updated == 0
    row = conn.execute(
        "SELECT msg_type, content FROM messages WHERE msg_id = ?", (ids[0],)
    ).fetchone()
    assert row["msg_type"] == 4
    assert not row["content"]
    # 空载荷（不是分享）照旧跳过。
    assert scraper._store_messages(
        [{**msg, "server_id": "1000000000000000043", "msg_type": "text"}], "c1"
    )[0] == 0
    conn.close()
