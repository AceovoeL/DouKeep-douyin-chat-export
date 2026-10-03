import asyncio
import json
import pytest
from extractor.exporter import ChatLabExporter, _resolve_message, _build_reply_to
from backend import database
from tests.conftest import insert_conversation, insert_message


def raw(cj, **extra):return json.dumps({'content_json':json.dumps(cj),**extra})

def test_new_formats_and_group_json_jsonl(temp_db,tmp_path):
    c=database.get_db();insert_conversation(c,'wrong','small-group');insert_conversation(c,'g','group',conv_type=2)
    def put(mid,seq,cj,**kw):insert_message(c,mid,'g',seq,raw_data=raw(cj,**kw),content='{truncated',sender_uid='u2',sender_name='成员')
    put('share',1,{'aweType':800,'itemId':'1000000000000000045','content_title':'视频'})
    put('reply',2,{'aweType':700,'text':'评论','related_share_video':{'itemId':'1000000000000000045'}})
    put('card',3,{'name':'名片昵称','secUID':'sec-test','desc':'简介'})
    put('emoji',4,{'aweType':501,'display_name':'猫咪'})
    put('forward',5,{'aweType':13600,'title':'记录','msg_ids':[{'msg_id':7600000000000000001},{'msg_id':7600000000000000002}]},forwarded_bodies=[{'server_message_id':7600000000000000001,'sender':123,'content':json.dumps({'aweType':700,'text':'下载的完整正文'})}])
    c.commit();c.close()
    outputs=[]
    for fmt in ['json','jsonl']:
        p=tmp_path/('out.'+fmt);ChatLabExporter('group',fmt).export(p)
        if fmt=='json':outputs.append(json.loads(p.read_text(encoding='utf-8')))
        else:
            lines=[json.loads(l) for l in p.read_text(encoding='utf-8').splitlines()];outputs.append({'meta':lines[0]['meta'],'messages':[{k:v for k,v in l.items() if k!='_type'} for l in lines if l['_type']=='message']})
    assert outputs[0]['messages']==outputs[1]['messages']
    assert outputs[0]['meta']['type']=='group' and outputs[0]['meta']['groupId']=='g'
    msgs={m['platformMessageId']:m for m in outputs[0]['messages']}
    assert msgs['reply']['replyToMessageId']=='share'
    assert '评论' in msgs['reply']['content']
    assert msgs['card']['type']==27 and '名片昵称' in msgs['card']['content']
    assert msgs['emoji']['type']==5 and msgs['emoji']['content']=='[猫咪]'
    assert msgs['forward']['type']==26 and '下载的完整正文' in msgs['forward']['content']
    assert '1/2' in msgs['forward']['content'] and '正文未取得' in msgs['forward']['content']
    assert 'skey' not in msgs['forward']['content']


def test_image_prefers_local_plaintext_over_encrypted_url(tmp_path):
    (tmp_path/'image.png').write_bytes(b'\x89PNG\r\n\x1a\nexample')
    msg={'msg_type':3,'content':'','media_local_path':'image.png','media_url':'https://cdn/encrypted'}
    cj={'aweType':2702,'resource_url':{'skey':'secret'}}
    text,typ,_=_resolve_message(msg,cj,str(tmp_path))
    assert typ==1 and text.startswith('data:image/png;base64,')
    msg['media_local_path']=None
    text,typ,_=_resolve_message(msg,cj,str(tmp_path))
    assert typ==0 and text=='[图片未下载]'


def test_legacy_video_and_malformed_reply(tmp_path):
    msg={'msg_type':3,'content':'','media_local_path':'a.mp4','media_url':None}
    assert _resolve_message(msg,{'video':None},str(tmp_path))[0]=='[视频]'
    assert _build_reply_to('[]') is None


def test_startup_migrates_old_database_before_reader(temp_db,monkeypatch):
    import backend.main as main
    c=database.get_db();insert_conversation(c,'g','群');insert_message(c,'m','g',1,content='消息')
    c.execute('DROP TABLE voice_transcriptions');c.commit();c.close()
    with pytest.raises(Exception,match='voice_transcriptions'):database.get_messages('g')
    monkeypatch.setattr(main.config,'ensure_api_token',lambda:None)
    async def no_schedule():pass
    monkeypatch.setattr(main,'restore_schedule_on_startup',no_schedule)
    asyncio.run(main.startup())
    items,total=database.get_messages('g');assert total==1 and items[0]['content']=='消息'


def test_product_and_legacy_system_payloads(tmp_path):
    msg={'msg_type':1,'content':'{truncated','media_local_path':None,'media_url':None}
    product={'aweType':11029,'im_dynamic_patch':{'raw_data':json.dumps({'content_top':{'content':'商品名称'},'whole_card':{'action_info':[{'params':{'schema':'sslocal://goods?commodity_id=123'}}]}})}}
    text,typ,_=_resolve_message(msg,product,str(tmp_path));assert typ==24 and '商品名称' in text and '/product/123' in text
    assert _resolve_message(msg,{'aweType':126,'tips':'{{1}}关注了你','template':[{'key':1,'name':'小明'}]},str(tmp_path))[0]=='[系统] 小明关注了你'
