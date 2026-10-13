import json

import pytest
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
    # 实况图：封面是一张普通图片，小视频已经下到本地（live_video_path 非空）。
    insert_message(conn, 'live', 'c1', 76, msg_type=3, timestamp=130,
                   media_local_path='images/live.webp', live_video_path='videos/live.mp4')
    # 实况图但小视频还没下：这张退化成普通图片，不该出现在实况图里。
    insert_message(conn, 'live-pending', 'c1', 77, msg_type=3, timestamp=131,
                   media_local_path='images/pending.webp')
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


def test_search_count_stops_at_the_limit(temp_db):
    """条数很多时不必数到底：数到上限就停，并说明这个数是「至少这么多」。
    精确数字要几秒，而用户只需要知道「比上限多」。"""
    seed(temp_db)
    rows, total, capped = database.search_messages(
        '匹配', conv_id='c1', count_limit=10, with_total_capped=True)
    assert total == 10 and capped is True
    assert len(rows) == 50  # 截断的是总数，不是这一页要显示的内容


def test_search_count_is_exact_below_the_limit(temp_db):
    """结果没到上限时给的就是准确数字，且不标成「1000+」。"""
    seed(temp_db)
    rows, total, capped = database.search_messages(
        '匹配', conv_id='c1', count_limit=1000, with_total_capped=True)
    assert total == 55 and capped is False
    # count_limit=0 表示数到底、要准确数字，供需要精确值的调用方使用。
    assert database.search_messages('匹配', conv_id='c1', count_limit=0)[1] == 55


def test_paged_search_can_skip_the_count(temp_db):
    """翻页时总数不会变，可以不数；这时总量返回 None，数据照常给。"""
    seed(temp_db)
    rows, total = database.search_messages('匹配', conv_id='c1', page=2, with_total=False)
    assert total is None
    assert len(rows) == 5


def test_search_results_do_not_change_when_the_count_is_capped(temp_db):
    """截断只影响那个数字，搜到的内容、顺序、分页边界必须一模一样。"""
    seed(temp_db)
    capped, _, _ = database.search_messages(
        '匹配', conv_id='c1', count_limit=10, with_total_capped=True)
    exact, _ = database.search_messages('匹配', conv_id='c1', count_limit=0)
    assert [m['msg_id'] for m in capped] == [m['msg_id'] for m in exact]


def test_search_http_reports_a_capped_total(temp_db, monkeypatch):
    seed(temp_db)
    monkeypatch.setattr(main, '_get_password_hash', lambda: None)
    monkeypatch.setattr(database, 'SEARCH_COUNT_LIMIT', 10)
    client = TestClient(main.app)
    body = client.get('/api/search', params={'conv_id': 'c1', 'q': '匹配'}).json()
    assert body['total'] == 10 and body['total_capped'] is True
    # 老字段名一个都不能少，前端还按它们取值。
    assert set(body) >= {'items', 'total', 'page', 'page_size'}
    # 翻页不再重算总数（返回 None），第一页的数字仍然有效。
    second = client.get('/api/search', params={'conv_id': 'c1', 'q': '匹配', 'page': 2}).json()
    assert second['total'] is None
    assert len(second['items']) == 5


def test_media_search_legacy_and_bad_json(temp_db):
    seed(temp_db)
    rows, total = database.search_messages(conv_id='c1', media_type='image')
    # 图片档不含实况图（'live' 那张是小视频下好了的实况图，'live-pending' 那种
    # 小视频没下好的退化成普通图片，仍然算图片）。
    assert {m['msg_id'] for m in rows} == {'image', 'live-pending'}
    assert total == 2
    rows, total = database.search_messages(conv_id='c1', media_type='video')
    assert {m['msg_id'] for m in rows} == {'video', 'legacy-video', 'json-video'}
    assert total == 3
    assert database.search_messages(conv_id='c1', media_type='media')[1] == 6
    assert database.search_messages('%_\\', conv_id='c1')[1] == 1


def test_live_photo_search_filters(temp_db):
    """实况图档只认「小视频已经下到本地」的那些（live_video_path 非空）。

    「实况图」= 一张静态封面 + 一段两三秒的小视频，两段都在本地才算数；小视频
    还没下好的那张图会退化成普通图片（出现在图片档里），不能混进实况图档。
    """
    seed(temp_db)
    rows, total = database.search_messages(conv_id='c1', media_type='live')
    assert total == 1
    assert [m['msg_id'] for m in rows] == ['live']
    # 列表里要拿到小视频地址才能跳到聊天窗口后播放，这一列不能缺。
    assert rows[0]['live_video_path'] == 'videos/live.mp4'
    # 实况图不会同时出现在图片档或视频档里（三个勾选框各自独立）。
    for media_type in ('image', 'video'):
        ids = {m['msg_id'] for m in database.search_messages(conv_id='c1', media_type=media_type)[0]}
        assert 'live' not in ids
    # 未知类型仍然报错，别把打错的参数当成"没有筛选"。
    with pytest.raises(ValueError):
        database.search_messages(conv_id='c1', media_type='livephoto')


def test_live_photo_search_over_http(temp_db, monkeypatch):
    """接口白名单里要有 live，否则前端勾了「实况图」只会收到 422。"""
    seed(temp_db)
    monkeypatch.setattr(main, '_get_password_hash', lambda: None)
    client = TestClient(main.app)
    body = client.get('/api/search', params={'conv_id': 'c1', 'media_type': 'live'}).json()
    assert body['total'] == 1
    assert [m['msg_id'] for m in body['items']] == ['live']


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
    assert client.get('/api/search', params={'conv_id': 'c1', 'media_type': 'image'}).json()['total'] == 2
    assert client.get('/api/search', params={'conv_id': 'c1', 'media_type': 'live'}).json()['total'] == 1
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


def _voice_row(conn, msg_id, seq, payload, timestamp=100, msg_type=0, **kw):
    """塞一条正文是 ``payload`` 的消息（payload 会当作 content_json 存）。"""
    insert_message(conn, msg_id, 'voice-conv', seq, msg_type=msg_type, timestamp=timestamp,
                   raw_data=json.dumps({'content_json': json.dumps(payload, ensure_ascii=False)},
                                       ensure_ascii=False), **kw)


def _seed_voice(conn):
    insert_conversation(conn, 'voice-conv', '语音会话')
    # 正常语音：带下载地址和时长的。
    _voice_row(conn, 'v-url', 1, {'resource_url': {'url_list': ['http://x/1'], 'duration': '3000'}},
               timestamp=100)
    # 只留了时长、没有地址的。
    _voice_row(conn, 'v-duration', 2, {'resource_url': {}, 'duration': 5000}, timestamp=200)
    # 只留了 tkey 标记的老数据。
    _voice_row(conn, 'v-tkey', 3, {'tkey': 'abc', 'resource_url': {'uri': 'u'}}, timestamp=300)
    # 老数据存成 msg_type=1：有语音标记仍算语音。
    _voice_row(conn, 'v-legacy-voice', 4, {'voice_wave': [1, 2], 'duration': 1000,
                                           'resource_url': {'uri': 'u'}},
               timestamp=400, msg_type=1)
    # 不是语音的：纯文字、图片、视频（带 vid）、aweType 2702 的视频、
    # 只有时长却没写 resource_url 的（跟前端 isVoiceMsg 一样不算语音）。
    _voice_row(conn, 'v-text', 5, {'text': '普通文字'}, timestamp=500)
    _voice_row(conn, 'v-image', 6, {'resource_url': {'url_list': ['http://x/2'], 'is_image': True}},
               timestamp=600, msg_type=3)
    _voice_row(conn, 'v-video', 7, {'resource_url': {'url_list': ['http://x/3'], 'duration': '4000'},
                                    'video': {'vid': 'vid-1'}}, timestamp=700, msg_type=1)
    _voice_row(conn, 'v-awe2702', 8, {'aweType': 2702, 'resource_url': {'url_list': ['http://x/4']}},
               timestamp=800, msg_type=0)
    _voice_row(conn, 'v-bare-duration', 10, {'duration': 7000}, timestamp=1000)
    # 坏 JSON 不能把搜索打挂，也不能算成语音。
    insert_message(conn, 'v-bad', 'voice-conv', 9, msg_type=0, timestamp=900, raw_data='{bad')
    conn.commit()


def test_voice_filter_keeps_only_voices(temp_db):
    """筛语音认的是正文里的语音字段，不是 msg_type（语音大多存成 0 号普通消息）。"""
    conn = database.get_db()
    _seed_voice(conn)
    conn.close()
    rows, total = database.search_messages(conv_id='voice-conv', media_type='voice')
    assert total == 4
    assert [m['msg_id'] for m in rows] == ['v-legacy-voice', 'v-tkey', 'v-duration', 'v-url']


def test_voice_filter_excludes_lookalikes(temp_db):
    """带地址的图片、带 vid 的视频、aweType 2702 的视频都长得像语音，别混进来。"""
    conn = database.get_db()
    _seed_voice(conn)
    conn.close()
    rows, _ = database.search_messages(conv_id='voice-conv', media_type='voice')
    found = {m['msg_id'] for m in rows}
    assert not found & {'v-text', 'v-image', 'v-video', 'v-awe2702', 'v-bad', 'v-bare-duration'}


def test_voice_list_is_newest_first(temp_db):
    """语音列表从上到下是从新到旧。"""
    conn = database.get_db()
    _seed_voice(conn)
    conn.close()
    rows, _ = database.search_messages(conv_id='voice-conv', media_type='voice')
    stamps = [m['timestamp'] for m in rows]
    assert stamps == sorted(stamps, reverse=True)
    assert stamps == [400, 300, 200, 100]
    # 同一秒发的按 seq 还原聊天先后（seq 大的更靠后，也就是更新，排在前面）。
    conn = database.get_db()
    _voice_row(conn, 'v-same-a', 20, {'resource_url': {'duration': '1000'}}, timestamp=999)
    _voice_row(conn, 'v-same-b', 21, {'resource_url': {'duration': '1000'}}, timestamp=999)
    conn.commit(); conn.close()
    rows, _ = database.search_messages(conv_id='voice-conv', media_type='voice', page_size=2)
    assert [m['msg_id'] for m in rows] == ['v-same-b', 'v-same-a']


def test_voice_search_http_and_keyword(temp_db, monkeypatch):
    """接口认 voice，且语音还能叠加关键词（搜转写文字）。"""
    conn = database.get_db()
    _seed_voice(conn)
    conn.close()
    monkeypatch.setattr(main, '_get_password_hash', lambda: None)
    client = TestClient(main.app)
    body = client.get('/api/search', params={'conv_id': 'voice-conv', 'media_type': 'voice'}).json()
    assert body['total'] == 4
    assert {item['msg_id'] for item in body['items']} == {'v-url', 'v-duration', 'v-tkey', 'v-legacy-voice'}
    # 语音也要带时长和发送者，列表上要显示这两项。
    assert all('sender_display_name' in item for item in body['items'])
    # 点进去要能定位到原文，靠的是 seq（阅读端按 seq 加载并滚动到那一条）。
    assert all(isinstance(item.get('seq'), int) for item in body['items'])
    assert {item['seq'] for item in body['items']} == {1, 2, 3, 4}
    # 关键词与语音筛选可以叠加。
    assert client.get('/api/search', params={
        'conv_id': 'voice-conv', 'media_type': 'voice', 'q': '不存在的词'}).json()['total'] == 0
    assert client.get('/api/search', params={'media_type': 'audio'}).status_code == 422
