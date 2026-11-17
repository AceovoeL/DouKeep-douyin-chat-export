import json
from backend import database
from tests.conftest import insert_conversation, insert_message


def test_video_reference_matches_nearest_prior_share_in_same_conversation(temp_db):
    c=database.get_db()
    insert_conversation(c,'a','A');insert_conversation(c,'b','B')
    def put(mid,conv,seq,cj):
        insert_message(c,mid,conv,seq,raw_data=json.dumps({'content_json':json.dumps(cj)}))
    vid='1000000000000000045'
    put('srv_old','a',1,{'aweType':800,'itemId':vid})
    put('srv_nearest','a',3,{'aweType':800,'itemId':vid})
    put('srv_comment','a',4,{'aweType':700,'related_share_video':{'itemId':vid}})
    put('srv_future','a',6,{'aweType':800,'itemId':vid})
    put('srv_other','b',4,{'aweType':800,'itemId':vid})
    put('srv_missing','a',7,{'aweType':700,'related_share_video':{'itemId':'999'}})
    c.commit();c.close()
    assert database.find_referenced_video('srv_comment')['msg_id']=='srv_nearest'
    assert database.find_referenced_video('srv_missing') is None
    assert database.find_referenced_video('absent') is None

def test_video_reference_finds_share_sent_after_the_comment(temp_db):
    """抖音把分享卡片排在评论后面（同一秒内两条消息），也要能找到。"""
    c=database.get_db()
    insert_conversation(c,'a','A')
    def put(mid,seq,cj):
        insert_message(c,mid,'a',seq,raw_data=json.dumps({'content_json':json.dumps(cj)}))
    vid='1000000000000000046'
    put('srv_comment',10,{'aweType':700,'related_share_video':{'itemId':vid}})
    put('srv_card',11,{'aweType':800,'itemId':vid})
    c.commit();c.close()
    assert database.find_referenced_video('srv_comment')['msg_id']=='srv_card'


def test_video_reference_checks_five_back_before_five_forward(temp_db):
    """先回看 5 条、再看前 5 条：回看窗口里有就先取它。"""
    c=database.get_db()
    insert_conversation(c,'a','A')
    def put(mid,seq,cj):
        insert_message(c,mid,'a',seq,raw_data=json.dumps({'content_json':json.dumps(cj)}))
    vid='1000000000000000047'
    put('srv_back',7,{'aweType':800,'itemId':vid})
    put('srv_comment',10,{'aweType':700,'related_share_video':{'itemId':vid}})
    put('srv_forward',11,{'aweType':800,'itemId':vid})
    c.commit();c.close()
    assert database.find_referenced_video('srv_comment')['msg_id']=='srv_back'


def test_video_reference_widens_window_until_found(temp_db):
    """前 5 条没有就继续往后扩窗，一直找到会话边界为止。"""
    c=database.get_db()
    insert_conversation(c,'a','A')
    def put(mid,seq,cj):
        insert_message(c,mid,'a',seq,raw_data=json.dumps({'content_json':json.dumps(cj)}))
    vid='1000000000000000048'
    put('srv_comment',20,{'aweType':700,'related_share_video':{'itemId':vid}})
    put('srv_far',30,{'aweType':800,'itemId':vid})
    c.commit();c.close()
    assert database.find_referenced_video('srv_comment')['msg_id']=='srv_far'

