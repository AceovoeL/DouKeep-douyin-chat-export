"""aweType=10401 是双用途的：商品分享卡片 与 「仅看一次」文本消息。

抓取端、合并转发解析与导出端必须一致地把后者当纯文本处理，
否则正文会被渲染/导出成 [分享]/[分享内容]。
"""
import json

from backend.forwarded import _is_share_payload, _inline_row, resolve_forward
from common.message_kinds import is_view_once
from extractor.exporter import _resolve_message
from extractor.web_scraper import _classify_json_message
from tests.conftest import insert_conversation, insert_message


# 用户报告的两条「仅看一次」消息（content_json 结构；正文已脱敏）。
VIEW_ONCE_TEXTS = ["示例文本一", "示例文本二"]


def _view_once_row(text):
    return {
        "msg_type": 1,
        "content": "{truncated",
        "media_url": None,
        "media_local_path": None,
        "raw_data": json.dumps({"content_json": json.dumps(
            {"text": text, "richTextInfos": [], "ai_ext": "{}", "scene": "",
             "aweType": 10401, "related_share_video": {}, "mention_users": []},
            ensure_ascii=False)}, ensure_ascii=False),
    }


def test_is_view_once_requires_text_and_no_card_fields():
    for text in VIEW_ONCE_TEXTS:
        assert is_view_once({"aweType": 10401, "text": text}) is True
    # 商品卡片同 aweType，但带卡片字段
    assert is_view_once({"aweType": 10401, "content_title": "商品名称"}) is False
    assert is_view_once({"aweType": 10401, "text": "名称", "itemId": "42"}) is False
    assert is_view_once({"aweType": 10401, "text": "   "}) is False
    assert is_view_once({"aweType": 700, "text": "普通评论"}) is False
    assert is_view_once(None) is False


def test_scraper_classifies_view_once_as_text():
    for text in VIEW_ONCE_TEXTS:
        cj = {"aweType": 10401, "text": text, "related_share_video": {}, "mention_users": []}
        assert _classify_json_message(cj) == (text, "text", None)
    # 商品卡片仍归类为分享
    assert _classify_json_message({"aweType": 10401, "content_title": "商品名称"})[1] == "share"
    # 分享视频/评论等原有分支不受影响
    assert _classify_json_message({"aweType": 11054, "push_detail": "视频"})[1] == "share"
    assert _classify_json_message({"aweType": 10500, "comment": "评论"})[:2] == ("评论", "share")
    assert _classify_json_message({"aweType": 700, "text": "评论"})[1] == "text"


def test_forwarded_inline_view_once_body_is_plain_text():
    # 之前 10401 在 _SHARE_AWETYPES 里，仅看一次正文会被当成卡片标题。
    assert _is_share_payload({"aweType": "10401", "text": VIEW_ONCE_TEXTS[0]}, "10401") is False
    assert _is_share_payload({"aweType": "10401", "content_title": "商品"}, "10401") is True
    row = _inline_row(
        {"server_message_id": 1001, "sender": 7,
         "content": json.dumps({"aweType": 10401, "text": VIEW_ONCE_TEXTS[1]})},
        {"msg_id": 1001, "awe_type": 10401}, {}, "srv_parent",
    )
    assert row["msg_type"] == 1
    assert row["content"] == VIEW_ONCE_TEXTS[1]


def test_forwarded_view_once_keeps_body_and_reports_no_title(temp_db):
    conn = __import__("backend.database", fromlist=["x"]).get_db()
    insert_conversation(conn, "c10401", "会话")
    insert_message(conn, "srv_1001", "c10401", 1, content="[分享]")
    conn.commit()

    def forward(cj):
        return {"msg_id": "srv_parent", "raw_data": json.dumps({"content_json": json.dumps(cj)})}

    for index, text in enumerate(VIEW_ONCE_TEXTS):
        sid = 2000 + index
        message = forward({
            "aweType": 13600, "title": "聊天记录",
            "msg_ids": [{"msg_id": sid, "awe_type": 10401}],
            "inline_content": [{"server_message_id": sid, "sender": 7,
                                "content": json.dumps({"aweType": 10401, "text": text})}],
        })
        items = resolve_forward(message, conn)["items"]
        assert [m["content"] for m in items] == [text]
        assert items[0]["msg_type"] == 1
    conn.close()


def test_exporter_keeps_view_once_text_for_legacy_share_rows():
    for text in VIEW_ONCE_TEXTS:
        cj = {"aweType": 10401, "text": text}
        content, chatlab_type, stats = _resolve_message(_view_once_row(text), cj, "/tmp")
        assert content == text
        assert chatlab_type == 0  # TEXT，不是 SHARE(24)
        assert stats == {}
    # 商品卡片仍按分享导出
    product = {"aweType": 10401, "content_title": "商品名称", "itemId": "42"}
    content, chatlab_type, _ = _resolve_message(_msg(product), product, "/tmp")
    assert chatlab_type == 24 and "商品名称" in content


def _msg(cj):
    return {"msg_type": 4, "content": "{truncated", "media_url": None,
            "media_local_path": None,
            "raw_data": json.dumps({"content_json": json.dumps(cj)})}
