"""限时日常分享（aweType=805）的分类与标注。

抖音把「限时日常」作品分享成卡片时用 aweType=805：卡片一定带 itemId/cover_url，
但通常**没有标题**（content_title 为空）。抓取端、合并转发解析与导出端如果不认识
这一类，展示就会退化成笼统的 [分享] / [分享视频]。这里锁定三端一致的
[分享限时日常] 标注。
"""
import json

from backend.forwarded import _inline_row, _is_share_payload
from backend import database
from common.message_kinds import daily_share_text, is_daily_share
from extractor.exporter import _resolve_message
from extractor.web_scraper import _classify_json_message
from tests.conftest import insert_conversation, insert_message
from tools.backfill_daily_share import apply, plan, revert


# 用户报告的样本消息（content_json 关键字段；content 被截断，与库里一致；作者名与作品 id 已脱敏）。
DAILY_CJ = {
    "aweType": 805,
    "awemeType": 0,
    "content_name": "示例作者",
    "content_title": "",
    "cover_url": {"url_list": ["https://example.com/cover.jpeg"]},
    "itemId": "1000000000000000040",
    "is_hot_spot_video": True,
    "is_story": False,
}


def _row(cj, msg_type=0):
    return {
        "msg_type": msg_type,
        "content": '{"aweType": 805, "awemeType": 0, "content_name": ',
        "media_url": None,
        "media_local_path": None,
        "raw_data": json.dumps({"content_json": json.dumps(cj, ensure_ascii=False)},
                               ensure_ascii=False),
    }


def test_default_labels_for_other_shares_are_unchanged():
    assert _resolve_message(_row({"itemId": "42", "content_title": "T", "content_name": "A"}, 1),
                            {"itemId": "42", "content_title": "T", "content_name": "A"}, "/tmp")[0] == (
        "[分享视频] T | @A | https://www.douyin.com/video/42")


def test_exporter_labels_daily_share_without_title():
    content, chatlab_type, stats = _resolve_message(_row(DAILY_CJ), DAILY_CJ, "/tmp")
    assert chatlab_type == 24 and stats == {"share": 1}
    assert content == "[分享限时日常] @示例作者 | https://www.douyin.com/video/1000000000000000040"


def test_exporter_keeps_daily_share_title_when_present():
    cj = {"aweType": 805, "itemId": "99", "content_title": "和 @示例用户 一起 #合拍"}
    content, _, _ = _resolve_message(_row(cj, 1), cj, "/tmp")
    assert content == "[分享限时日常] 和 @示例用户 一起 #合拍 | https://www.douyin.com/video/99"


def test_scraper_classifies_daily_share_as_share():
    text, msg_type, image_src = _classify_json_message(DAILY_CJ)
    assert (text, msg_type) == ("[分享限时日常]", "share")
    assert image_src == "https://example.com/cover.jpeg"
    # 正文一律带 [分享限时日常] 标签（这样关键词可被检索），标题接在后面。
    assert _classify_json_message({"aweType": 805, "content_title": "标题"})[:2] == (
        "[分享限时日常] 标题", "share")
    assert _classify_json_message({"aweType": 805, "text": "正文"})[:2] == (
        "[分享限时日常] 正文", "share")
    # 抓取端不再把这些消息落成截断的 JSON 正文（"other"）。
    assert _classify_json_message(DAILY_CJ)[1] != "other"


def test_forwarded_daily_share_falls_back_to_its_label():
    assert _is_share_payload(DAILY_CJ, "805") is True
    row = _inline_row(
        {"server_message_id": 8051, "sender": 7, "conversation_id": "c1",
         "create_time": 1790351621000, "content": json.dumps(DAILY_CJ, ensure_ascii=False)},
        {"msg_id": 8051, "awe_type": 805, "create_time": 1790351621000},
        {"text": ""}, "srv_8051")
    assert row["msg_type"] == 4
    assert row["content"] == "[分享限时日常]"


def test_message_kinds_helpers():
    assert is_daily_share(DAILY_CJ) is True
    assert is_daily_share({"aweType": "805"}) is True
    assert is_daily_share({"aweType": 800}) is False
    assert is_daily_share(None) is False
    assert daily_share_text(DAILY_CJ) == "[分享限时日常]"
    assert daily_share_text({"aweType": 805, "content_title": " 和 @示例用户 一起 #合拍 "}) == (
        "[分享限时日常] 和 @示例用户 一起 #合拍")
    assert daily_share_text(None) == "[分享限时日常]"


def test_backfill_rewrites_truncated_json_content_and_revert(temp_db, tmp_path):
    conn = database.get_db()
    insert_conversation(conn, "daily", "会话")
    # 老数据：抓取端不认识 805，content 落成截断的 JSON。
    truncated = json.dumps(DAILY_CJ, ensure_ascii=False)[:200]
    insert_message(conn, "srv_daily", "daily", 1, content=truncated, msg_type=0,
                   raw_data=json.dumps({"content_json": json.dumps(DAILY_CJ, ensure_ascii=False)},
                                       ensure_ascii=False))
    # 已经正确的行不应被再次修改。
    insert_message(conn, "srv_ok", "daily", 2, content="[分享限时日常]", msg_type=4,
                   raw_data=json.dumps({"content_json": json.dumps(DAILY_CJ, ensure_ascii=False)},
                                       ensure_ascii=False))
    # 无关的 805 字样不应误伤：aweType 不是 805 的行必须原样保留。
    other = {"aweType": 800, "content_title": "标题805"}
    insert_message(conn, "srv_other", "daily", 3, content="[分享] 标题805", msg_type=4,
                   raw_data=json.dumps({"content_json": json.dumps(other, ensure_ascii=False)},
                                       ensure_ascii=False))
    conn.commit()

    rows = plan(conn)
    assert [r[0] for r in rows] == ["srv_daily"]
    undo = tmp_path / "undo.json"
    apply(conn, rows, undo_path=str(undo))
    contents = {m["msg_id"]: m["content"] for m in
                [dict(r) for r in conn.execute("SELECT msg_id, content FROM messages")]}
    assert contents["srv_daily"] == "[分享限时日常]"
    assert contents["srv_ok"] == "[分享限时日常]"
    assert contents["srv_other"] == "[分享] 标题805"

    # 回填后按关键词能搜到（搜索走的是 content 列与 content_json 字段）。
    found, total = database.search_messages("限时日常", conv_id="daily")
    assert total == 2 and {m["msg_id"] for m in found} == {"srv_daily", "srv_ok"}
    # 作者名仍可检索：回填把原来的 JSON 正文换掉了，作者改由 content_name 字段匹配。
    found, total = database.search_messages("示例作者", conv_id="daily")
    assert total == 2 and {m["msg_id"] for m in found} == {"srv_daily", "srv_ok"}

    # 幂等：再跑一次没有待改的行。
    assert plan(conn) == []
    # 回滚：恢复成截断的 JSON，标签关键词随即搜不到（作者仍走 content_name）。
    assert revert(conn, undo_path=str(undo)) == 1
    restored = conn.execute("SELECT content FROM messages WHERE msg_id='srv_daily'").fetchone()[0]
    assert restored == truncated
    assert database.search_messages("限时日常", conv_id="daily")[1] == 1
    assert database.search_messages("示例作者", conv_id="daily")[1] == 2
    conn.close()
