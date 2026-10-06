"""「我是谁」（common/owner.py）的推断规则，以及 /api/owner 接口。

推断只能靠数据本身：participant_uids → 私聊 conv_id 里谁横跨的会话最多 → 谁在最多
会话里发过消息。测试数据全是编出来的 uid 和昵称，真实账号信息不进仓库。
"""
from fastapi.testclient import TestClient

from common.db import connect
from common.owner import FALLBACK_NAME, detect_owner
from tests.conftest import insert_conversation, insert_message

ME = "uid-self-0001"
PEER_A = "uid-peer-a"
PEER_B = "uid-peer-b"
PEER_C = "uid-peer-c"


def add_user(conn, uid, nickname):
    conn.execute("INSERT OR REPLACE INTO users (uid, nickname) VALUES (?, ?)", (uid, nickname))
    conn.commit()


def direct(conn, peer, name="单聊"):
    """建一个 1 对 1 会话，conv_id 按采集端的样子写成 ``0:1:<对方>:<我>``。"""
    insert_conversation(conn, f"0:1:{peer}:{ME}", name)
    conn.commit()
    return f"0:1:{peer}:{ME}"


def group(conn, conv_id, name="群聊"):
    insert_conversation(conn, conv_id, name)
    conn.commit()
    return conv_id


# ── 推断规则 ──

def test_participant_uids_is_the_first_choice(temp_db):
    conn = connect()
    insert_conversation(conn, "0:1:x:y", "单聊", participant_uids=f'["{ME}"]')
    add_user(conn, ME, "本机账号昵称")
    conn.commit()
    assert detect_owner(conn) == (ME, "本机账号昵称")
    conn.close()


def test_participant_uids_wins_over_the_vote(temp_db):
    conn = connect()
    insert_conversation(conn, "0:1:x:y", "采集时的第一个会话", participant_uids=f'["{PEER_A}"]')
    add_user(conn, PEER_A, "participant 里的人")
    for peer in (PEER_B, PEER_C):
        direct(conn, peer)
    assert detect_owner(conn)[0] == PEER_A
    conn.close()


def test_the_account_in_most_direct_chats_wins(temp_db):
    conn = connect()
    for peer in (PEER_A, PEER_B, PEER_C):
        direct(conn, peer)
    add_user(conn, ME, "本机账号昵称")
    assert detect_owner(conn) == (ME, "本机账号昵称")
    conn.close()


def test_one_direct_chat_is_not_enough_to_guess(temp_db):
    """只有一个私聊时自己和对方各出现一次，分不出来 —— 宁可说不知道。"""
    conn = connect()
    direct(conn, PEER_A)
    add_user(conn, ME, "本机账号昵称")
    assert detect_owner(conn) == ("", "")
    conn.close()


def test_a_tie_is_not_guessed(temp_db):
    """三个人两两成会话时谁也赢不了，不要瞎猜一个。"""
    conn = connect()
    insert_conversation(conn, f"0:1:{PEER_A}:{PEER_B}", "单聊一")
    insert_conversation(conn, f"0:1:{PEER_A}:{PEER_C}", "单聊二")
    insert_conversation(conn, f"0:1:{PEER_B}:{PEER_C}", "单聊三")
    conn.commit()
    assert detect_owner(conn) == ("", "")
    conn.close()


def test_message_counts_are_the_last_resort(temp_db):
    """只有群聊（conv_id 里没有对方 uid）时，退回「谁在最多会话里发过消息」。"""
    conn = connect()
    for index in range(3):
        conv_id = group(conn, f"71234567890000{index}")
        insert_message(conn, f"m-{index}", conv_id, index, sender_uid=ME)
    conv_id = group(conn, "71234567890999")
    insert_message(conn, "m-peer", conv_id, 1, sender_uid=PEER_A)
    add_user(conn, ME, "本机账号昵称")
    assert detect_owner(conn) == (ME, "本机账号昵称")
    conn.close()


def test_missing_nickname_falls_back_to_the_placeholder(temp_db):
    conn = connect()
    for peer in (PEER_A, PEER_B):
        direct(conn, peer)
    uid, name = detect_owner(conn)
    assert uid == ME
    assert name == FALLBACK_NAME
    conn.close()


def test_empty_database_reports_nothing(temp_db):
    conn = connect()
    assert detect_owner(conn) == ("", "")
    conn.close()


# ── 接口 ──

def _client():
    import backend.main as main

    return TestClient(main.app)


def test_owner_endpoint_reports_the_account(temp_db):
    conn = connect()
    insert_conversation(conn, "0:1:x:y", "单聊", participant_uids=f'["{ME}"]')
    add_user(conn, ME, "本机账号昵称")
    conn.commit()
    conn.close()
    payload = _client().get("/api/owner").json()
    assert payload == {"uid": ME, "name": "本机账号昵称"}


def test_owner_endpoint_is_empty_when_it_cannot_tell(temp_db):
    payload = _client().get("/api/owner").json()
    assert payload == {"uid": "", "name": ""}
