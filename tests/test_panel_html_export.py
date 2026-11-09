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