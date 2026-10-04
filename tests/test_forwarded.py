import json
from backend import database
from backend.forwarded import resolve_forward
from tests.conftest import insert_conversation, insert_message


def forward(cj):
    return {'msg_id': 'srv_parent', 'raw_data': json.dumps({'content_json': json.dumps({'aweType': 13600, **cj})})}


def test_inline_beyond_three_and_precision(temp_db):
    conn = database.get_db()
    ids = [7600000000000000001 + i for i in range(5)]
    message = forward({'msg_ids': [{'msg_id': i} for i in ids],
                       'list_content': [{'msgid': i, 'text': '摘要'} for i in ids[:3]],
                       'inline_content': [{'server_message_id': i, 'sender': 7100000000000000001,
                                           'create_time': 1700000000000000,
                                           'content': json.dumps({'aweType': 700, 'text': f'正文{i}'})} for i in ids]})
    result = resolve_forward(message, conn)
    assert result['complete'] and result['available'] == result['total'] == 5
    assert result['items'][-1]['content'] == f'正文{ids[-1]}'
    assert result['items'][0]['sender_uid'] == '7100000000000000001'
    assert result['items'][0]['timestamp'] == 1700000000
    assert result['items'][0]['msg_id'] != result['items'][1]['msg_id']
    conn.close()


def test_old_forward_local_recovery_and_missing(temp_db):
    conn = database.get_db()
    insert_conversation(conn, 'c1', '会话')
    insert_message(conn, 'srv_123', 'c1', 1, msg_type=3, content='[图片]', media_local_path='images/test.jpg')
    result = resolve_forward(forward({'msg_ids': [{'msg_id': 123}, {'msg_id': 124}],
                                     'list_content': [{'msgid': 124, 'nick_name': '测试', 'text': '摘要'}]}), conn)
    assert result['available'] == result['missing'] == 1
    assert not result['complete']
    assert result['items'][0]['media_local_path'] == 'images/test.jpg'
    assert result['items'][1]['detail_missing'] and result['items'][1]['content'] == '摘要'
    conn.close()


def test_nested_cycle_and_malformed(temp_db):
    conn = database.get_db()
    insert_conversation(conn, 'c1', '会话')
    recursive = forward({'msg_ids': [{'msg_id': 123}]})
    insert_message(conn, 'srv_123', 'c1', 1, raw_data=recursive['raw_data'])
    result = resolve_forward(recursive, conn)
    child = result['items'][0]['forward_detail']['items'][0]['forward_detail']
    assert child['items'] == [] and not child['complete']
    assert resolve_forward({'raw_data': '{bad'}, conn) is None
    assert resolve_forward(forward({'msg_ids': [None, 'bad']}), conn)['items'] == []
    conn.close()


def test_broken_body_does_not_count_as_full_detail(temp_db):
    conn = database.get_db()
    result = resolve_forward(forward({'msg_ids': [{'msg_id': 42}],
                                     'inline_content': [{'server_message_id': 42, 'content': '{bad'}]}), conn)
    assert not result['complete'] and result['available'] == 0
    assert result['items'][0]['detail_missing']
    conn.close()


def test_inline_monster_emoji_and_doubao_card(temp_db):
    """合并转发里的「小火人」（aweType=519）也是表情；豆包卡（6001）是卡片正文。"""
    conn = database.get_db()
    ids = [7700000000000000001, 7700000000000000002]
    message = forward({
        'msg_ids': [{'msg_id': i} for i in ids],
        'inline_content': [
            {'server_message_id': ids[0], 'sender': 1, 'create_time': 1700000000,
             'content': json.dumps({'aweType': 519, 'display_name': '笑死',
                                    'url': {'url_list': ['https://cdn/1010.webp']}})},
            {'server_message_id': ids[1], 'sender': 1, 'create_time': 1700000001,
             'content': json.dumps({'aweType': 6001, 'source_title': '豆包',
                                    'title': '《音乐公开课》',
                                    'icon': {'url_list': ['https://cdn/cover.jpeg']}})},
        ],
    })
    items = resolve_forward(message, conn, fetch_media=False)['items']
    assert items[0]['msg_type'] == 2
    assert items[0]['media_url'] == 'https://cdn/1010.webp'
    assert items[0]['content'] == '笑死'
    assert items[1]['msg_type'] == 1
    assert items[1]['content'] == '《音乐公开课》'
    conn.close()


def test_forward_http_contract(temp_db, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import main
    monkeypatch.setattr(main, '_get_password_hash', lambda: None)
    conn = database.get_db()
    insert_conversation(conn, 'c1', '会话')
    insert_message(conn, 'normal', 'c1', 1, content='文本')
    insert_message(conn, 'merged', 'c1', 2, raw_data=forward({'msg_ids': [{'msg_id': 42}]})['raw_data'])
    conn.commit()
    conn.close()
    client = TestClient(main.app)
    assert client.get('/api/messages/not-found/forward').status_code == 404
    assert client.get('/api/messages/normal/forward').status_code == 422
    response = client.get('/api/messages/merged/forward')
    assert response.status_code == 200
    assert response.json()['missing'] == 1


def test_inline_share_types_match_main_preview(temp_db):
    conn = database.get_db()
    ids = [11, 12, 13, 14]
    payloads = [
        {'aweType': 11054, 'content_title': '视频标题', 'text': '说明', 'itemId': '42'},
        {'aweType': 10500, 'comment': '评论内容', 'content_title': '视频标题'},
        {'aweType': 68, 'awemeType': 68, 'content_title': '图文标题',
         'im_dynamic_patch': {'raw_data': '{}'}},
        {'aweType': 700, 'text': '普通评论'},
    ]
    message = forward({
        'msg_ids': [{'msg_id': i} for i in ids],
        'inline_content': [
            {'server_message_id': i, 'content': json.dumps(payloads[n])}
            for n, i in enumerate(ids)
        ],
    })
    items = resolve_forward(message, conn)['items']
    assert [m['msg_type'] for m in items] == [4, 4, 4, 1]
    assert items[0]['content'] == '视频标题'
    assert items[1]['content'] == '评论内容'
    assert items[2]['content'] == '图文标题'
    assert items[3]['content'] == '普通评论'
    conn.close()


def test_flattened_forward_shares_use_descriptor_awe_type(temp_db):
    conn = database.get_db()
    message = forward({
        'msg_ids': [
            {'msg_id': 21, 'awe_type': 11054},
            {'msg_id': 22, 'awe_type': 10500},
            {'msg_id': 23, 'awe_type': 11054},
        ],
        'inline_content': [
            {'server_message_id': 21, 'content': json.dumps(
                {'aweType': 0, 'text': '[分享视频] 视频标题', 'content_title': '视频标题'})},
            {'server_message_id': 22, 'content': json.dumps(
                {'aweType': 0, 'text': '评论内容'})},
            {'server_message_id': 23, 'content': json.dumps(
                {'aweType': 0, 'text': '[分享图文] 图文标题', 'awemeType': 68,
                 'content_title': '图文标题'})},
        ],
    })
    items = resolve_forward(message, conn)['items']
    assert [m['msg_type'] for m in items] == [4, 4, 4]
    assert items[0]['content'] == '视频标题'
    assert items[1]['content'] == '评论内容'
    assert items[2]['content'] == '图文标题'
    video_cj = json.loads(json.loads(items[0]['raw_data'])['content_json'])
    comment_cj = json.loads(json.loads(items[1]['raw_data'])['content_json'])
    assert video_cj['aweType'] == 11054
    assert not video_cj.get('comment')
    assert comment_cj['aweType'] == 10500
    assert comment_cj['comment'] == '评论内容'
    conn.close()


def test_local_text_row_yields_to_inline_share(temp_db):
    conn = database.get_db()
    insert_conversation(conn, 'c1', '会话')
    insert_message(conn, 'srv_31', 'c1', 1, msg_type=1, content='[分享视频] 视频标题',
                   media_local_path='covers/31.jpg')
    message = forward({
        'msg_ids': [{'msg_id': 31, 'awe_type': 11054}],
        'inline_content': [{'server_message_id': 31, 'content': json.dumps(
            {'aweType': 11054, 'content_title': '视频标题', 'itemId': '42', 'text': '说明'})}],
    })
    item = resolve_forward(message, conn)['items'][0]
    assert item['msg_type'] == 4
    assert item['content'] == '视频标题'
    assert item['media_local_path'] == 'covers/31.jpg'
    conn.close()


def test_preview_names_follow_sender_beyond_first_three(temp_db):
    conn = database.get_db()
    ids = [101, 102, 103, 104, 105]
    message = forward({'msg_ids': [{'msg_id': i, 'uid': 20 if i % 2 else 30} for i in ids],
        'list_content': [{'msgid': 101, 'nick_name': '乙'}, {'msgid': 102, 'nick_name': '丙'}],
        'inline_content': [{'server_message_id': i, 'sender': 20 if i % 2 else 30,
                            'content': json.dumps({'text': '正文'})} for i in ids]})
    detail = resolve_forward(message, conn)
    assert [m['sender_name'] for m in detail['items']] == ['乙', '丙', '乙', '丙', '乙']
    conn.close()
