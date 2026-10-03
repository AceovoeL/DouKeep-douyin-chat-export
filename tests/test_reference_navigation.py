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
