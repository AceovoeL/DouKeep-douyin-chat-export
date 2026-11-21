"""控制面板 HTML 导出接口的测试（不依赖真实数据库）。"""

import json
import os
import shutil
import zipfile

from fastapi.testclient import TestClient

import backend.main as main
from backend import control_panel


client = TestClient(main.app)


def test_export_html_requires_conversation():
    resp = client.post("/panel/api/export/html", json={})
    assert resp.status_code == 400


def test_export_html_rejects_invalid_range():
    resp = client.post(
        "/panel/api/export/html",
        json={"conversation": "不存在", "start_ts": 200, "end_ts": 100},
    )
    assert resp.status_code == 400


def test_export_estimate_requires_conversation():
    resp = client.post("/panel/api/export/html/estimate", json={})
    assert resp.status_code == 400


def test_export_html_missing_conversation_404(temp_db):
    resp = client.post(
        "/panel/api/export/html/estimate",
        json={"conversation": "__不存在__"},
    )
    assert resp.status_code == 404


def test_conversations_endpoint_shape(temp_db):
    resp = client.get("/panel/api/export/html/conversations")
    if resp.status_code == 200:
        data = resp.json()
        assert "items" in data
        for item in data["items"]:
            assert "conv_id" in item and "name" in item

def test_txt_daily_without_names_is_empty():
    resp = client.get("/panel/api/export/txt/daily")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_txt_estimate_requires_conversation():
    resp = client.post("/panel/api/export/txt/estimate", json={})
    assert resp.status_code == 400


def test_txt_estimate_rejects_invalid_range():
    resp = client.post(
        "/panel/api/export/txt/estimate",
        json={"names": ["会话A"], "start_ts": 200, "end_ts": 100},
    )
    assert resp.status_code == 400


def test_txt_estimate_counts_messages_in_range(temp_db):
    import extractor.models as models
    from tests.conftest import insert_conversation, insert_message

    conn = models.get_db()
    insert_conversation(conn, "c1", "会话A", participant_uids='["owner"]', last_message_time=2000)
    insert_message(conn, "m1", "c1", 1, content="一", timestamp=1000)
    insert_message(conn, "m2", "c1", 2, content="二二", timestamp=2000)
    conn.commit()
    conn.close()

    resp = client.post(
        "/panel/api/export/txt/estimate",
        json={"names": ["会话A"], "start_ts": 1500, "end_ts": 2500},
    )
    data = resp.json()
    assert data["messages"] == 1
    assert data["estimated_bytes"] > 0

    daily = client.get("/panel/api/export/txt/daily", params={"names": ["会话A"]}).json()
    assert len(daily["items"]) == 1
