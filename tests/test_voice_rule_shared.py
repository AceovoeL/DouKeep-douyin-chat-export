"""两份实现必须一致：前端的 isVoiceMsg 和后端搜索用的 SQL 条件。

「哪些消息算语音」这套规则同时写在前端（frontend/src/lib/douyinMessage.js）和后端
（backend/database.py 的 _voice_clause）。两边只要有一点不一样，就会出现「聊天窗口
里画成语音、搜索却搜不到」这种怪事，所以用例集中放在 tests/fixtures/voice_cases.json，
Python 和 JS 各自拿同一份数据跑一遍（JS 那份见 frontend/src/lib/voiceCases.test.js）。
改规则时先改 JSON，再让两边都通过。
"""

import json
import os

import pytest

from backend import database

CASES_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "voice_cases.json")


def load_cases():
    with open(CASES_PATH, encoding="utf-8") as handle:
        return json.load(handle)["cases"]


@pytest.fixture
def voice_db(temp_db):
    """按用例建库，返回查出来的语音 msg_id 集合。"""
    from tests.conftest import insert_conversation, insert_message

    conn = database.get_db()
    insert_conversation(conn, "cases", "用例")
    for index, case in enumerate(load_cases()):
        insert_message(
            conn, case["name"], "cases", index + 1, msg_type=case["msg_type"],
            raw_data=json.dumps({"content_json": json.dumps(case["content_json"])}),
        )
    conn.commit()
    conn.close()
    rows, _ = database.search_messages(conv_id="cases", media_type="voice", page_size=200)
    return {row["msg_id"] for row in rows}


def test_backend_voice_rule_matches_the_shared_cases(voice_db):
    for case in load_cases():
        assert (case["name"] in voice_db) is case["voice"], case["name"]
