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
