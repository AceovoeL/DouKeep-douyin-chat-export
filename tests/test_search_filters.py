import json

from fastapi.testclient import TestClient
from tests.conftest import insert_conversation, insert_message
from backend import database, main


def seed(temp_db):
    conn = database.get_db()
    insert_conversation(conn, 'c1', '当前会话')
    insert_conversation(conn, 'c2', '其他会话')
    for i in range(55):
        insert_message(conn, f'm{i:02}', 'c1', i, content='匹配', timestamp=100 + i)
    insert_message(conn, 'other', 'c2', 60, content='匹配', timestamp=120)
    insert_message(conn, 'image', 'c1', 70, msg_type=3, timestamp=120, raw_data='{bad')
    insert_message(conn, 'video', 'c1', 71, msg_type=5, timestamp=120)
    insert_message(conn, 'legacy-video', 'c1', 72, msg_type=3, media_local_path='videos/test.MP4', timestamp=120)
    insert_message(conn, 'json-video', 'c1', 73, msg_type=1, timestamp=120,
                   raw_data=json.dumps({'content_json': json.dumps({'video': {'vid': 'abc'}})}))
    insert_message(conn, 'forward-new', 'c1', 80, timestamp=200, sender_uid='me',
                   raw_data=json.dumps({'content_json': json.dumps({'aweType': 13600, 'title': '新记录'})}))
    insert_message(conn, 'forward-peer', 'c1', 90, timestamp=180, sender_uid='other',
                   content=json.dumps({'aweType': 13600, 'title': '对方记录'}))
    insert_message(conn, 'forward-old', 'c1', 75, timestamp=150, sender_uid='me',
                   raw_data=json.dumps({'content_json': json.dumps({'aweType': '13600', 'title': '旧记录'})}))
    insert_message(conn, 'literal', 'c1', 74, content='100%_\\', timestamp=120)
    conn.commit()
    conn.close()


def test_scoped_search_pagination_and_boundaries(temp_db):
    seed(temp_db)
    first, total = database.search_messages('匹配', conv_id='c1')
    second, total2 = database.search_messages('匹配', conv_id='c1', page=2)
    assert total == total2 == 55
    assert len(first) == 50 and len(second) == 5
    assert not ({m['msg_id'] for m in first} & {m['msg_id'] for m in second})
    rows, total = database.search_messages('匹配', conv_id='c1', start_time=110, end_time=120)
    assert total == 10
    assert all(110 <= m['timestamp'] < 120 for m in rows)


def test_media_search_legacy_and_bad_json(temp_db):
    seed(temp_db)
    rows, total = database.search_messages(conv_id='c1', media_type='image')
    assert total == 1 and rows[0]['msg_id'] == 'image'
    rows, total = database.search_messages(conv_id='c1', media_type='video')
    assert {m['msg_id'] for m in rows} == {'video', 'legacy-video', 'json-video'}
    assert total == 3
    assert database.search_messages(conv_id='c1', media_type='media')[1] == 4
    assert database.search_messages('%_\\', conv_id='c1')[1] == 1


def test_forward_search_newest_first(temp_db):
    seed(temp_db)
    rows, total = database.search_messages(conv_id='c1', media_type='forward')
    assert total == 3
    assert [m['msg_id'] for m in rows] == ['forward-new', 'forward-peer', 'forward-old']
    assert {m['sender_uid'] for m in rows} == {'me', 'other'}


def test_group_notice_search_reads_the_locale_template(temp_db):
    """群通知的正文在库里只是 "[系统消息]" 占位，句子在多语言模板里。
    搜模板里的词、或者被点名的人，都应该能找到这条消息。"""
    conn = database.get_db()
    insert_conversation(conn, 'g1', '群聊')
    insert_message(conn, 'join', 'g1', 1, msg_type=0, content='[系统消息]', timestamp=100,
                   raw_data=json.dumps({'content_json': json.dumps({
                       'aweType': 100140,
                       'active_users': [{'uid': 11, 'nickname': '小明'}],
                       'passive_users': [{'uid': 22, 'nickname': '小红'}],
                       'locale_resources': [{'lang': 'zh-Hans',
                                             'text': '{0}邀请{1}加入了群聊，新成员可查看历史消息'}],
                   }, ensure_ascii=False)}, ensure_ascii=False))
    insert_message(conn, 'chat', 'g1', 2, content='今天聊点别的', timestamp=110)
    conn.commit()
    conn.close()

    for query in ('邀请', '加入了群聊', '小明', '小红'):
        rows, total = database.search_messages(query, conv_id='g1')
        assert [m['msg_id'] for m in rows] == ['join'], query
        assert total == 1, query
    # 普通聊天不受影响，别的关键词也不会把通知捞出来
    assert database.search_messages('别的', conv_id='g1')[1] == 1
    assert database.search_messages('修改了群头像', conv_id='g1')[1] == 0


def test_search_http_validation(temp_db, monkeypatch):
    seed(temp_db)
    monkeypatch.setattr(main, '_get_password_hash', lambda: None)
    client = TestClient(main.app)
    assert client.get('/api/search', params={'conv_id': 'c1', 'media_type': 'image'}).json()['total'] == 1
    assert client.get('/api/search', params={'conv_id': 'c1', 'media_type': 'forward'}).json()['total'] == 3
    assert client.get('/api/search', params={'start_time': 20, 'end_time': 10}).status_code == 422
    assert client.get('/api/search', params={'media_type': 'invalid'}).status_code == 422
    assert client.get('/api/search', params={'page': 0}).status_code == 422
    assert client.get('/api/search').status_code == 422


def test_calendar_date_jump_can_request_just_first_message(temp_db, monkeypatch):
    seed(temp_db)
    monkeypatch.setattr(main, '_get_password_hash', lambda: None)
    client = TestClient(main.app)
    response = client.get('/api/conversations/c1/messages/by-date', params={'date': '1970-01-01', 'tz': 0, 'limit': 1})
    assert response.status_code == 200
    assert len(response.json()['items']) == 1
    assert response.json()['items'][0]['msg_id'] == 'm00'
    assert client.get('/api/conversations/c1/messages/by-date', params={'date': '1970-01-01', 'limit': 0}).status_code == 422


def test_search_complete_share_title_and_comment(temp_db):
    import json
    conn = database.get_db()
    insert_conversation(conn, 'full-share', '会话')
    insert_message(conn, 'share-full', 'full-share', 1, content='{truncated', msg_type=4,
                   raw_data=json.dumps({'content_json': json.dumps({'content_title': '完整标题', 'comment': '完整评论'})}))
    conn.commit(); conn.close()
    for keyword in ['完整标题', '完整评论']:
        rows, total = database.search_messages(keyword, conv_id='full-share')
        assert total == 1 and rows[0]['msg_id'] == 'share-full'


def test_search_dynamic_share_layout_title(temp_db):
    import json
    conn = database.get_db()
    insert_conversation(conn, 'dynamic-share', '会话')
    for index, layout in enumerate([json.dumps({'top_bottom_top': {'content': '动态完整标题'}}), '{bad']):
        insert_message(conn, f'dynamic-{index}', 'dynamic-share', index + 1, content='[分享]', msg_type=4,
                       raw_data=json.dumps({'content_json': json.dumps({'im_dynamic_patch': {'raw_data': layout}})}))
    conn.commit(); conn.close()
    rows, total = database.search_messages('动态完整标题', conv_id='dynamic-share')
    assert total == 1 and rows[0]['msg_id'] == 'dynamic-0'
