import asyncio
import json

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from extractor.forwarded import decode_resource, fetch_uploaded_bodies, OBJECT_URL_PATH

KEY = '12' * 32
IDS = [1000000000000000046, 1000000000000000047]


def body(sid):
    return {'server_message_id': sid, 'sender': 1234567890123456789,
            'content': json.dumps({'aweType': 700, 'text': '完整正文'})}


def resource(messages):
    nonce = bytes(range(12))
    return nonce + AESGCM(bytes.fromhex(KEY)).encrypt(
        nonce, json.dumps({'Messages': messages}).encode(), None)


def test_decrypt_preserves_ids_and_full_content():
    messages = decode_resource(resource([body(IDS[0])]), KEY)
    assert messages == [body(IDS[0])]
    assert str(messages[0]['server_message_id']) == '1000000000000000046'


def test_authentication_rejects_corruption_and_wrong_key():
    data = resource([body(IDS[0])])
    with pytest.raises(InvalidTag):
        decode_resource(data[:-1] + bytes([data[-1] ^ 1]), KEY)
    with pytest.raises(InvalidTag):
        decode_resource(data, '13' * 32)


@pytest.mark.parametrize('messages', [[], [body(IDS[0]), body(IDS[0])],
    [{'server_message_id': IDS[0], 'content': ''}],
    [{'server_message_id': float(IDS[0]), 'content': '{}'}]])
def test_invalid_body_is_not_accepted(messages):
    with pytest.raises(ValueError):
        decode_resource(resource(messages), KEY)


def test_multiple_resources_order_precision_and_access_chain():
    cj = {'skey': KEY, 'msg_ids': [{'msg_id': sid} for sid in IDS],
          'upload_key_list': json.dumps([
              {'key': f'douyin-im-merge-share/v01/{i}', 'count': 1} for i in [1, 0]])}
    chain = [{'msg_id': IDS[0], 'conv_id': 1234567890123456789}]
    calls = []

    async def request(path, payload):
        assert path == OBJECT_URL_PATH
        assert payload['access_chain'] == chain
        calls.append(payload)
        return {'status_code': 0, 'url': 'https://example.com/' + payload['uri'][-1]}

    async def download(url, limit):
        result = resource([body(IDS[int(url[-1])])])
        assert len(result) < limit
        return result

    actual = asyncio.run(fetch_uploaded_bodies(cj, request, download, access_chain=chain))
    assert actual == [body(sid) for sid in IDS]
    assert len(calls) == 2
    assert 'inline_content' not in cj


@pytest.mark.parametrize('failure', ['login', 'missing', 'wrong_id', 'count', 'http'])
def test_failed_fetch_never_publishes_partial_bodies(failure):
    cj = {'skey': KEY, 'msg_ids': [{'msg_id': sid} for sid in IDS],
          'upload_key_list': [{'key': 'douyin-im-merge-share/v01/0', 'count': 2}]}

    async def request(path, payload):
        return {'status_code': 8 if failure == 'login' else 0,
                'url': ('http://' if failure == 'http' else 'https://') + 'example.com/body'}

    async def download(url, limit):
        messages = [body(sid) for sid in IDS]
        if failure == 'missing':
            messages.pop()
            cj['upload_key_list'][0]['count'] = 1
        elif failure == 'wrong_id':
            messages[1] = body(999)
        elif failure == 'count':
            messages.pop()
        return resource(messages)

    with pytest.raises(ValueError):
        asyncio.run(fetch_uploaded_bodies(cj, request, download))
    assert 'inline_content' not in cj


@pytest.mark.parametrize('url', ['http://p11-aweme-im-merge-share-sign.byteimg.com/x',
    'https://localhost/x', 'https://p11-aweme-im-merge-share-sign.byteimg.com.evil.test/x',
    'https://user:password@p11-aweme-im-merge-share-sign.byteimg.com/x',
    'https://p11-aweme-im-merge-share-sign.byteimg.com:444/x'])
def test_resource_url_restriction(url):
    from extractor.forwarded import _check_resource_url
    with pytest.raises(ValueError):
        _check_resource_url(url)


def test_web_request_keeps_64_bit_ids_in_json_string():
    from extractor.forwarded import _request_web_url, WEB_OBJECT_URL_PATH
    class Page:
        async def evaluate(self, script, args):
            assert args['path'] == WEB_OBJECT_URL_PATH
            assert '1000000000000000046' in args['body']
            assert isinstance(args['body'], str)
            return {'status': 200, 'body': '{"status_code":0,"url":"https://example.com"}'}
    result = asyncio.run(_request_web_url(Page(), OBJECT_URL_PATH,
                         {'access_chain': [{'msg_id': IDS[0]}]}))
    assert result['status_code'] == 0


def test_backfill_stores_bodies_preserves_original_and_reuses_backup(temp_db, monkeypatch):
    from backend import database
    from backend.forwarded import resolve_forward
    from extractor import forwarded
    from tests.conftest import insert_conversation, insert_message
    conn = database.get_db()
    insert_conversation(conn, 'conv', '测试')
    cj = {'aweType':13600, 'skey':KEY, 'msg_ids':[{'msg_id':sid} for sid in IDS],
          'upload_key_list':[{'key':'douyin-im-merge-share/v01/key', 'count':2}]}
    original = json.dumps({'content_json':json.dumps(cj), 'extra':'preserved'})
    insert_message(conn, 'srv_123', 'conv', 1, raw_data=original)
    conn.commit()
    calls=[]
    async def fetch(page, content, *, access_chain):
        calls.append(access_chain)
        return [body(sid) for sid in IDS]
    monkeypatch.setattr(forwarded, 'fetch_web_uploaded_bodies', fetch)
    stats=asyncio.run(forwarded.backfill_uploaded_forwards(None,conn,'conv','456'))
    assert stats == {'succeeded':1,'failed':0,'cached':0}
    row=dict(conn.execute('SELECT * FROM messages').fetchone())
    assert json.loads(row['raw_data'])['content_json'] == json.loads(original)['content_json']
    assert json.loads(row['raw_data'])['extra'] == 'preserved'
    assert resolve_forward(row,conn)['complete']
    assert calls == [[{'msg_id':123,'conv_id':456}]]
    # Full scrape refreshes the row, then restores valid resource bodies from snapshot.
    conn.execute('CREATE TEMP TABLE msg_backup AS SELECT * FROM messages')
    conn.execute('UPDATE messages SET raw_data=?', (original,));conn.commit()
    stats=asyncio.run(forwarded.backfill_uploaded_forwards(None,conn,'conv','456'))
    assert stats['cached'] == 1 and stats['succeeded'] == 0
    assert len(calls) == 1
    assert resolve_forward(dict(conn.execute('SELECT * FROM messages').fetchone()),conn)['complete']
    conn.close()


def test_backfill_failure_keeps_message_and_raw_data(temp_db, monkeypatch):
    from backend import database
    from extractor import forwarded
    from tests.conftest import insert_conversation, insert_message
    conn=database.get_db();insert_conversation(conn,'conv','测试')
    original=json.dumps({'content_json':json.dumps({'aweType':13600,'msg_ids':[{'msg_id':1}],
                       'upload_key_list':[{'key':'douyin-im-merge-share/v01/key','count':1}]})})
    insert_message(conn,'srv_123','conv',1,raw_data=original);conn.commit()
    async def fail(*args,**kwargs):raise ValueError('resource unavailable')
    monkeypatch.setattr(forwarded,'fetch_web_uploaded_bodies',fail)
    assert asyncio.run(forwarded.backfill_uploaded_forwards(None,conn,'conv','456'))['failed']==1
    assert conn.execute('SELECT raw_data FROM messages').fetchone()[0]==original
    conn.close()


@pytest.mark.parametrize('payload,expected', [
    ({'url':'https://p11-aweme-im-merge-share-sign.byteimg.com/x'},0),
    ({'status_code':5,'status_msg':'参数不合法'},5),
    ({'extra':{}},None),
])
def test_web_success_omits_status_code(payload,expected):
    from extractor.forwarded import _request_web_url
    class Page:
        async def evaluate(self,script,args):return {'status':200,'body':json.dumps(payload)}
    result=asyncio.run(_request_web_url(Page(),OBJECT_URL_PATH,{'uri':'test'}))
    assert result.get('status_code') == expected
