import json
from backend import database
from extractor.web_scraper import WebChatScraper
from tests.conftest import insert_conversation, insert_message


def test_incremental_order_mixes_compact_sequences_and_new_server_keys(temp_db):
    conn=database.get_db();insert_conversation(conn,'c','会话')
    insert_message(conn,'srv_old','c',1,timestamp=100,raw_data=json.dumps({'order_high':1,'order_low':20}))
    insert_message(conn,'srv_forward','c',2,timestamp=300,raw_data=json.dumps({'created_at_us':'4294967356'}))
    insert_message(conn,'srv_middle','c',4294967336,timestamp=200,raw_data=json.dumps({'order_high':1,'order_low':40}))
    insert_message(conn,'srv_later_same_second','c',3,timestamp=300,raw_data=json.dumps({'order_high':1,'order_low':70}))
    conn.commit()
    scraper=object.__new__(WebChatScraper);scraper._db_conn=conn
    for _ in range(2):
        assert scraper._normalize_message_order('c')==4
        assert [r[0] for r in conn.execute('SELECT msg_id FROM messages ORDER BY seq')]==[
            'srv_old','srv_middle','srv_forward','srv_later_same_second']
    conn.close()
