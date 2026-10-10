"""导出时「一个会话都没勾」该怎么处理。

2026-10-10 的实测：面板上一个都没勾就点「导出」，后端把空列表当成「没指定」，
顺手导出了最近活跃的那一个会话（2.8 万条消息、509 MB），看着像"什么都没勾却导出了
一大堆"。这里的用例锁住新行为：拒绝并说明白，绝不替用户挑会话；整库导出不受勾选影响。
"""
import asyncio
import json
import os

from backend import control_panel as cp
from common import paths

PANEL_HTML = os.path.join(paths.REPO_ROOT, "backend", "panel", "static", "panel.html")


def _isolate(tmp_path, monkeypatch):
    """配置指到临时文件：这些用例不能碰真的 config/panel_config.json。"""
    monkeypatch.setattr(paths, "CONFIG_PATH", str(tmp_path / "panel_config.json"))
    monkeypatch.setitem(cp._export_state, "status", "idle")
    monkeypatch.setitem(cp._export_state, "file_path", None)
    monkeypatch.setitem(cp._export_state, "message", "")


def _record_export(monkeypatch):
    """把真正干活的那一步换成记账的：只关心「被要求导出什么」。"""
    calls = []

    def fake_do_export(fmt, filter_name, conversations):
        calls.append((fmt, filter_name, conversations))

    monkeypatch.setattr(cp, "_do_export", fake_do_export)
    return calls


def test_nothing_selected_is_refused(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = _record_export(monkeypatch)

    response = asyncio.run(cp.start_export(cp.ExportRequest(format="jsonl", conversations=[])))

    assert response.status_code == 400
    assert "勾选" in response.body.decode("utf-8")
    assert calls == []                              # 没有替用户挑会话
    assert cp._export_state["status"] == "idle"     # 也没把面板状态改成「正在导出」


def test_missing_selection_is_refused_too(tmp_path, monkeypatch):
    """老调用方只发 {"format": "jsonl"}：同样不许偷偷导出。"""
    _isolate(tmp_path, monkeypatch)
    calls = _record_export(monkeypatch)

    response = asyncio.run(cp.start_export(cp.ExportRequest(format="jsonl")))

    assert response.status_code == 400
    assert calls == []


def test_blank_names_do_not_count_as_a_selection(tmp_path, monkeypatch):
    """空串 / 空格不算「选了一个会话」（否则会去库里找一个名字为空的会话）。"""
    _isolate(tmp_path, monkeypatch)
    calls = _record_export(monkeypatch)

    response = asyncio.run(cp.start_export(cp.ExportRequest(format="txt", conversations=[" ", ""])))

    assert response.status_code == 400
    assert calls == []


def test_selected_conversations_are_exported(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = _record_export(monkeypatch)

    asyncio.run(cp.start_export(cp.ExportRequest(format="txt", conversations=[" 会话A ", "会话B"])))

    assert calls == [("txt", "", ["会话A", "会话B"])]   # 首尾空格顺手清掉


def test_database_export_ignores_the_selection(tmp_path, monkeypatch):
    """整库导出就是「全部聊天记录」，不看勾选 —— 这是唯一会导出所有会话的方式。"""
    _isolate(tmp_path, monkeypatch)
    calls = _record_export(monkeypatch)

    asyncio.run(cp.start_export(cp.ExportRequest(format="database", conversations=[])))

    assert calls == [("database", "", None)]


def test_selection_is_persisted(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _record_export(monkeypatch)

    asyncio.run(cp.start_export(cp.ExportRequest(format="jsonl", conversations=["会话A"])))

    with open(paths.CONFIG_PATH, encoding="utf-8") as handle:
        assert json.load(handle)["export_selected"] == ["会话A"]


def test_panel_html_blocks_export_until_something_is_checked():
    with open(PANEL_HTML, encoding="utf-8") as handle:
        html = handle.read()

    assert html.count("exportNeedConv:") == 2      # 中文、英文各一份
    assert "exportBlocked" in html
    assert "updateExportGate" in html
