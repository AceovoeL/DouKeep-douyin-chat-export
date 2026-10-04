"""「N 条消息」显示几：Python 端与前端逐条对齐。

样例文件放在前端目录（`frontend/src/lib/displayCountCases.json`），
前端 `displayCount.test.js` 用的是同一份，两边规则不一致就会各自报错。
"""
import json
import os

from common import display_rules

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES_PATH = os.path.join(_REPO_ROOT, "frontend", "src", "lib", "displayCountCases.json")


def load_cases():
    with open(CASES_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def test_display_rules_match_the_frontend_cases():
    for sample in load_cases():
        hidden = display_rules.hidden_message_ids(sample["messages"])
        assert sorted(hidden) == sorted(sample["hidden"]), sample["name"]
        assert display_rules.display_count(sample["messages"]) == sample["display"], sample["name"]


def test_sender_display_counts_split_by_sender():
    conn_hidden = {
        "msg_id": "x1", "conv_id": "0:1:1:2", "sender_uid": "u1", "msg_type": 0,
        "content": "{}", "timestamp": 10, "seq": 1, "raw_data": None,
    }
    rows = [
        {"msg_id": "x2", "conv_id": "0:1:1:2", "sender_uid": "u1", "msg_type": 1,
         "content": "你好", "timestamp": 11, "seq": 2, "raw_data": None},
        conn_hidden,
        {"msg_id": "x3", "conv_id": "0:1:1:2", "sender_uid": "u2", "msg_type": 1,
         "content": "你好呀", "timestamp": 12, "seq": 3, "raw_data": None},
    ]
    assert display_rules.sender_display_counts(rows) == {"u1": 1, "u2": 1}
    assert display_rules.display_count(rows) == 2


def test_empty_conversation_has_no_displayed_messages():
    assert display_rules.display_count([]) == 0
    assert display_rules.hidden_message_ids([]) == set()


def test_group_notice_locale_template_counts_as_displayed():
    """群通知落库时 content 只是 "[系统消息]" 占位，句子在 locale_resources 里，
    仍然算「会显示出来」，不能因为占位符而漏算。"""
    row = {
        "msg_id": "n1", "conv_id": "0:1:1:2", "sender_uid": "u1", "msg_type": 0,
        "content": "[系统消息]", "timestamp": 10, "seq": 1,
        "raw_data": json.dumps({"content_json": json.dumps({
            "aweType": 100115,
            "active_users": [{"uid": 1, "nickname": "示例昵称"}],
            "locale_resources": [{"lang": "zh-Hans", "text": "{0}修改了群头像"}],
        }, ensure_ascii=False)}, ensure_ascii=False),
    }
    assert display_rules.render_system_msg_text(row) == "示例昵称修改了群头像"
    assert display_rules.hidden_message_ids([row]) == set()
    assert display_rules.display_count([row]) == 1


def _row(msg_id, cj, msg_type=0, content=""):
    return {
        "msg_id": msg_id, "conv_id": "0:1:1:2", "sender_uid": "u1", "msg_type": msg_type,
        "content": content, "timestamp": 10, "seq": 1,
        "raw_data": json.dumps({"content_json": json.dumps(cj, ensure_ascii=False)}, ensure_ascii=False),
    }


def test_monster_emoji_519_counts_as_emoji_not_as_a_system_line():
    """小火人（aweType=519）在阅读端是表情图片，正文「笑死」不是提示文字。
    镜像要跟 frontend/src/lib/douyinMessage.js 的 LOOSE_EMOJI_AWES 一致。"""
    row = _row("e1", {
        "aweType": 519, "display_name": "笑死", "image_id": 1010,
        "url": {"url_list": ["https://cdn/1010.webp"]},
    }, content="笑死")
    assert display_rules.is_loose_emoji(row) is True
    assert display_rules.should_show(row) is True
    assert display_rules.hidden_message_ids([row]) == set()


def test_group_invite_card_is_always_displayed():
    """群邀请卡：阅读端画成卡片（群名 + 谁添加你进群），不该被当成空系统提示藏掉。"""
    row = _row("i1", {
        "title": "示例群名", "desc": "某某 添加你进群", "type_desc": "群聊邀请",
        "icon": {"url_list": ["https://cdn/group.webp"]},
        "event": {"param": {"conversation_id": "1234567890123456789"}},
        "aweme_invite_card": {"group_name": "示例群名", "conversation_id": "1234567890123456789"},
    })
    card = display_rules.get_invite_card(row)
    assert card == {
        "groupName": "示例群名",
        "convId": "1234567890123456789",
        "icon": "https://cdn/group.webp",
    }
    assert display_rules.should_show(row) is True
    assert display_rules.display_count([row]) == 1


def test_doubao_music_card_is_always_displayed():
    """豆包卡（aweType=6001）：画成卡片（封面 + 标题 + 豆包），不靠正文。"""
    row = _row("m1", {
        "aweType": 6001, "source_title": "豆包", "title": "《音乐公开课》",
        "icon": {"url_list": ["https://cdn/doubao.jpeg"]},
        "open_url": "https://v.douyin.com/xxx/",
    })
    assert display_rules.get_music_card(row) == {
        "title": "《音乐公开课》", "cover": "https://cdn/doubao.jpeg", "source": "豆包",
    }
    assert display_rules.should_show(row) is True


def test_card_parsers_do_not_claim_other_payloads():
    """邀请卡要有群会话 id，豆包卡要有标题或封面 —— 别的卡片一律不认。"""
    assert display_rules.get_invite_card(_row("x1", {"type_desc": "群聊邀请", "title": "群"})) is None
    assert display_rules.get_music_card(_row("x2", {"aweType": 6001})) is None
    assert display_rules.get_music_card(_row("x3", {"aweType": 805, "itemId": "9"})) is None
